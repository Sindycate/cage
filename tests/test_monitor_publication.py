import io
import json
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import (
    constants as constants_api,
    hub as hub_api,
    identity as identity_api,
    publication as publication_api,
    service as service_api,
    state as state_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorPublicationTests(MonitorTestCase):
    @staticmethod
    def _five_provider_totals():
        return {"openai-api": 5, "openai-compatible": 6, "zllm": 8,
                "unattributed": 9, "demo-provider": 10}

    def _published_fixture(self, root):
        connection = monitor.MonitorConnection("https://hub.example", "secret")
        record = self._registered_monitor_projects(root, "current")[0]
        summary = self._period_summary(record, "current", 5, "2026-09-03")
        payloads, status = monitor.aggregate_provider_summaries(root, [(record, summary)])
        status["split_complete"] = True
        with patch.object(hub_api, "upload_summary"):
            published = publication_api._publish_provider_payloads(
                root, connection, payloads, status, None,
            )
        return connection, payloads, published

    def _provider_payloads(self, root, totals, timestamp):
        extra = sorted(set(totals) - constants_api.PUBLIC_PROVIDER_IDS)
        monitor.save_provider_labels(root, {
            "version": constants_api.PROVIDER_LABELS_VERSION,
            "approved": extra, "active": extra,
        })
        record = self._registered_monitor_projects(root, "current")[0]
        summary = self._summary(record.device_id, {
            f"codex:session-{provider}": self._session(
                f"session-{provider}", total=total, input_tokens=total,
                output_tokens=0, provider=provider,
            )
            for provider, total in totals.items()
        }, period_windows=self._period_windows("2026-09-03"))
        with patch.object(state_api, "_now", return_value=timestamp):
            payloads, status = monitor.aggregate_provider_summaries(root, [(record, summary)])
        self.assertEqual(set(payloads), set(totals))
        status["split_complete"] = True
        return payloads, status

    def _published_provider_fixture(self, root, totals=None):
        connection = monitor.MonitorConnection("https://hub.example", "secret")
        payloads, status = self._provider_payloads(
            root, totals if totals is not None else {"openai-api": 5, "zllm": 8},
            "2026-09-03T00:00:00Z",
        )
        with patch.object(hub_api, "upload_summary"):
            published = publication_api._publish_provider_payloads(
                root, connection, payloads, status, None,
            )
        return connection, payloads, published

    def test_final_changed_stream_preserves_a_complete_composite_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            totals = self._five_provider_totals()
            connection, first, previous = self._published_provider_fixture(root, totals)
            totals["zllm"] += 1
            candidate, status = self._provider_payloads(root, totals, "2026-09-03T00:01:00Z")
            # Private evidence and fresh clocks alone do not require a POST.
            candidate["openai-api"][0]["today"]["sessions"]["codex:session-openai-api"]["providerTokenUsage"] = {"private": {}}
            untouched_candidate = deepcopy(candidate)
            with patch.object(hub_api, "upload_summary") as upload:
                published = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous, skip_unchanged=True,
                )
            upload.assert_called_once_with(connection, candidate["zllm"][0], config_root=root)
            self.assertEqual(candidate, untouched_candidate)
            composite = publication_api._load_generation_payloads(root, published["last_good_generation"])
            self.assertEqual(set(composite), set(totals))
            for provider in totals:
                self.assertEqual(composite[provider], candidate[provider][0] if provider == "zllm" else first[provider][0])
            self.assertEqual(published["providers"], status["providers"])
            self.assertEqual(published["updated_at"], "2026-09-03T00:01:00Z")

            # A later change must compare against the composite, not the first
            # generation or the previous scan's unsent observation clocks.
            totals["openai-api"] += 1
            second, second_status = self._provider_payloads(root, totals, "2026-09-03T00:02:00Z")
            with patch.object(hub_api, "upload_summary") as upload:
                second_published = publication_api._publish_provider_payloads(
                    root, connection, second, second_status, published, skip_unchanged=True,
                )
            upload.assert_called_once_with(connection, second["openai-api"][0], config_root=root)
            second_composite = publication_api._load_generation_payloads(root, second_published["generation"])
            for provider in totals:
                self.assertEqual(second_composite[provider], second[provider][0] if provider == "openai-api" else composite[provider])

            same, same_status = self._provider_payloads(root, totals, "2026-09-03T00:03:00Z")
            with patch.object(hub_api, "upload_summary") as upload:
                same_published = publication_api._publish_provider_payloads(
                    root, connection, same, same_status, second_published, skip_unchanged=True,
                )
            upload.assert_not_called()
            self.assertEqual(same_published["generation"], second_published["generation"])
            self.assertEqual(same_published["updated_at"], same_status["updated_at"])

    def test_final_partial_failure_rolls_back_only_the_attempted_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, previous_payloads, previous = self._published_provider_fixture(root)
            candidate, status = self._provider_payloads(root, {"openai-api": 5, "zllm": 9}, "2026-09-03T00:01:00Z")
            with patch.object(hub_api, "upload_summary", side_effect=[monitor.MonitorError("upload failed"), None]) as upload:
                with self.assertRaisesRegex(monitor.MonitorError, "upload failed"):
                    publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_unchanged=True,
                    )
            self.assertEqual([call.args[1] for call in upload.call_args_list],
                             [candidate["zllm"][0], previous_payloads["zllm"][0]])
            self.assertEqual(monitor.load_aggregate_status(root), previous)
            self.assertIsNone(monitor.load_upload_state(root))

    def test_second_changed_upload_failure_preserves_unchanged_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            totals = self._five_provider_totals()
            connection, first, previous = self._published_provider_fixture(root, totals)
            totals["openai-api"] += 1
            totals["zllm"] += 1
            candidate, status = self._provider_payloads(root, totals, "2026-09-03T00:01:00Z")
            with patch.object(hub_api, "upload_summary", side_effect=[
                None, monitor.MonitorError("second upload failed"), None, None,
            ]) as upload, patch.object(
                publication_api, "save_upload_state", wraps=publication_api.save_upload_state,
            ) as journal:
                with self.assertRaisesRegex(monitor.MonitorError, "second upload failed"):
                    publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_unchanged=True,
                    )
            self.assertEqual([call.args[1] for call in upload.call_args_list], [
                candidate["openai-api"][0], candidate["zllm"][0],
                first["openai-api"][0], first["zllm"][0],
            ])
            attempted = {"openai-api", "zllm"}
            for call in journal.call_args_list:
                self.assertTrue(set(call.args[1]["attempted"]).issubset(attempted))
            self.assertEqual(set(journal.call_args_list[-1].args[1]["attempted"]), attempted)
            self.assertEqual(monitor.load_aggregate_status(root), previous)
            self.assertIsNone(monitor.load_upload_state(root))

    def test_final_commit_crash_repairs_attempted_stream_then_rewrites_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, previous_payloads, previous = self._published_provider_fixture(root)
            candidate, status = self._provider_payloads(root, {"openai-api": 5, "zllm": 9}, "2026-09-03T00:01:00Z")
            with patch.object(hub_api, "upload_summary") as upload, patch.object(
                publication_api, "_save_published_status", side_effect=OSError("commit failed"),
            ):
                with self.assertRaisesRegex(OSError, "commit failed"):
                    publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_unchanged=True,
                    )
            upload.assert_called_once()
            self.assertEqual(monitor.load_aggregate_status(root), previous)
            pending = monitor.load_upload_state(root)
            self.assertEqual(pending["attempted"], ["zllm"])
            self.assertEqual(set(pending["provider_ids"]), set(candidate))
            prepared = publication_api._load_generation_payloads(root, pending["generation"])
            self.assertEqual(prepared["openai-api"], previous_payloads["openai-api"][0])
            with patch.object(hub_api, "upload_summary") as upload:
                published = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous, skip_unchanged=True,
                )
            self.assertEqual([call.args[1] for call in upload.call_args_list],
                             [previous_payloads["zllm"][0], candidate["openai-api"][0], candidate["zllm"][0]])
            self.assertIsNone(monitor.load_upload_state(root))
            committed = publication_api._load_generation_payloads(root, published["generation"])
            self.assertEqual(committed, {provider: payload for provider, (payload, _) in candidate.items()})

    def test_explicit_verified_skip_keeps_its_candidate_in_composite_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, first, previous = self._published_provider_fixture(
                root, {"openai-api": 5, "openai-compatible": 6, "zllm": 8},
            )
            candidate, status = self._provider_payloads(
                root, {"openai-api": 6, "openai-compatible": 6, "zllm": 9}, "2026-09-03T00:01:00Z",
            )
            with patch.object(hub_api, "upload_summary") as upload:
                published = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous,
                    skip_upload_for={"zllm"}, skip_unchanged=True,
                )
            upload.assert_called_once_with(connection, candidate["openai-api"][0], config_root=root)
            composite = publication_api._load_generation_payloads(root, published["generation"])
            self.assertEqual(composite["zllm"], candidate["zllm"][0])
            self.assertEqual(composite["openai-compatible"], first["openai-compatible"][0])

    def test_explicit_verified_clocks_commit_even_when_all_usage_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, first, previous = self._published_provider_fixture(root)
            candidate, status = self._provider_payloads(root, {"openai-api": 5, "zllm": 8}, "2026-09-03T00:01:00Z")
            with patch.object(hub_api, "upload_summary") as upload:
                published = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous,
                    skip_upload_for={"zllm"}, skip_unchanged=True,
                )
            upload.assert_not_called()
            self.assertNotEqual(published["generation"], previous["generation"])
            composite = publication_api._load_generation_payloads(root, published["generation"])
            self.assertEqual(composite["zllm"], candidate["zllm"][0])
            self.assertEqual(composite["openai-api"], first["openai-api"][0])
            self.assertEqual(published["providers"], status["providers"])
            self.assertIsNone(monitor.load_upload_state(root))

    def test_provider_set_changes_require_a_complete_rewrite(self):
        for change in ("added", "removed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                totals = self._five_provider_totals()
                connection, _, previous = self._published_provider_fixture(root, totals)
                if change == "added":
                    totals["second-demo-provider"] = 11
                else:
                    totals.pop("openai-compatible")
                candidate, status = self._provider_payloads(root, totals, "2026-09-03T00:01:00Z")
                with patch.object(hub_api, "upload_summary") as upload:
                    published = publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_unchanged=True,
                    )
                self.assertEqual([call.args[1] for call in upload.call_args_list],
                                 [candidate[provider][0] for provider in sorted(candidate)])
                committed = publication_api._load_generation_payloads(root, published["generation"])
                self.assertEqual(committed, {provider: payload for provider, (payload, _) in candidate.items()})

    def test_retired_zero_stream_is_sent_once_and_then_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, first, previous = self._published_provider_fixture(root)
            candidate, status = self._provider_payloads(root, {"openai-api": 5}, "2026-09-03T00:01:00Z")
            record = monitor.load_registry(root)[0]
            summary = self._summary(record.device_id, {}, period_windows=self._period_windows("2026-09-03"))
            with patch.object(state_api, "_now", return_value="2026-09-03T00:01:00Z"):
                service_api._add_previous_provider_payloads(root, [(record, summary)], candidate, status, previous)
            self.assertEqual(candidate["zllm"][0]["allTime"]["totalTokens"], 0)
            with patch.object(hub_api, "upload_summary") as upload:
                published = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous, skip_unchanged=True,
                )
            upload.assert_called_once_with(connection, candidate["zllm"][0], config_root=root)
            composite = publication_api._load_generation_payloads(root, published["generation"])
            self.assertEqual(composite["openai-api"], first["openai-api"][0])
            repeated, repeated_status = self._provider_payloads(root, {"openai-api": 5}, "2026-09-03T00:02:00Z")
            with patch.object(state_api, "_now", return_value="2026-09-03T00:02:00Z"):
                service_api._add_previous_provider_payloads(root, [(record, summary)], repeated, repeated_status, published)
            with patch.object(hub_api, "upload_summary") as upload:
                republished = publication_api._publish_provider_payloads(
                    root, connection, repeated, repeated_status, published, skip_unchanged=True,
                )
            upload.assert_not_called()
            self.assertEqual(republished["generation"], published["generation"])

    def test_partial_comparison_preserves_significant_fields_and_period_windows(self):
        for change in ("session-time", "message-count", "agent-version", "hostname", "cost", "period-window"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                connection, _, previous = self._published_provider_fixture(root)
                candidate, status = self._provider_payloads(root, {"openai-api": 5, "zllm": 8}, "2026-09-03T00:01:00Z")
                payload = candidate["zllm"][0]
                session = payload["today"]["sessions"]["codex:session-zllm"]
                if change == "session-time":
                    session["lastUsedAt"] = "2026-09-03T00:02:00Z"
                elif change == "message-count":
                    session["messageCount"] += 1
                elif change == "agent-version":
                    payload["agentVersion"] = "test-new-version"
                elif change == "hostname":
                    payload["hostname"] = "Cage (changed label)"
                elif change == "cost":
                    payload["today"]["costUsd"] = 0.25
                else:
                    for current, _ in candidate.values():
                        current["periodWindows"] = self._period_windows("2026-09-04")
                with patch.object(hub_api, "upload_summary") as upload:
                    publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_unchanged=True,
                    )
                expected = sorted(candidate) if change == "period-window" else ["zllm"]
                self.assertEqual([call.args[1] for call in upload.call_args_list],
                                 [candidate[provider][0] for provider in expected])

    def test_equivalent_outward_generation_refreshes_local_status_without_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, payloads, previous = self._published_fixture(root)
            candidate = deepcopy(payloads)
            payload = candidate["openai-api"][0]
            payload["updatedAt"] = "2026-09-03T00:01:00Z"
            payload["limits"]["updatedAt"] = "2026-09-03T00:01:00Z"
            payload["today"]["sessions"]["codex:current"]["providerTokenUsage"] = {"private": {}}
            status = deepcopy(previous)
            status["updated_at"] = "2026-09-03T00:01:00Z"
            status["project_count"] += 1
            with patch.object(hub_api, "upload_summary") as upload:
                refreshed = publication_api._publish_provider_payloads(
                    root, connection, candidate, status, previous, skip_unchanged=True,
                )

            upload.assert_not_called()
            self.assertEqual(refreshed["generation"], previous["generation"])
            self.assertEqual(refreshed["last_good_generation"], previous["last_good_generation"])
            self.assertEqual(refreshed["updated_at"], status["updated_at"])
            self.assertEqual(refreshed["project_count"], status["project_count"])
            self.assertEqual(monitor.load_aggregate_status(root), refreshed)
            self.assertEqual(publication_api._load_generation_payloads(root, refreshed["generation"])["openai-api"], payloads["openai-api"][0])

    def test_equivalence_retains_usage_timestamps_metadata_and_reporting_windows(self):
        for change in ("session-time", "message-count", "agent-version", "hostname", "period-window"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                connection, payloads, previous = self._published_fixture(root)
                candidate = deepcopy(payloads)
                payload = candidate["openai-api"][0]
                if change == "session-time":
                    payload["today"]["sessions"]["codex:current"]["lastUsedAt"] = "2026-09-03T00:02:00Z"
                elif change == "message-count":
                    payload["today"]["sessions"]["codex:current"]["messageCount"] += 1
                elif change == "agent-version":
                    payload["agentVersion"] = "test-new-version"
                elif change == "hostname":
                    payload["hostname"] = "Cage (updated label)"
                else:
                    payload["periodWindows"] = self._period_windows("2026-09-04")
                with patch.object(hub_api, "upload_summary") as upload:
                    refreshed = publication_api._publish_provider_payloads(
                        root, connection, candidate, deepcopy(previous), previous,
                        skip_unchanged=True,
                    )
                upload.assert_called_once()
                self.assertNotEqual(refreshed["generation"], previous["generation"])

    def test_connection_and_incomplete_baselines_require_publication(self):
        for change in ("legacy-context", "hub", "credential", "failed-status", "missing-last-good"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                totals = self._five_provider_totals()
                connection, _, previous = self._published_provider_fixture(root, totals)
                totals["zllm"] += 1
                payloads, status = self._provider_payloads(root, totals, "2026-09-03T00:01:00Z")
                old_context = previous["publication_context"]
                if change == "legacy-context":
                    previous.pop("publication_context")
                elif change == "hub":
                    connection = monitor.MonitorConnection("https://other-hub.example", "secret")
                elif change == "credential":
                    connection = monitor.MonitorConnection("https://hub.example", "different-secret")
                elif change == "failed-status":
                    previous["upload_state"] = "failed"
                else:
                    previous.pop("last_good_generation")
                with patch.object(hub_api, "upload_summary") as upload:
                    refreshed = publication_api._publish_provider_payloads(
                        root, connection, payloads, status, previous,
                        skip_unchanged=True,
                    )
                self.assertEqual([call.args[1] for call in upload.call_args_list],
                                 [payloads[provider][0] for provider in sorted(payloads)])
                self.assertNotEqual(refreshed["generation"], previous["generation"])
                if change in ("hub", "credential"):
                    self.assertNotEqual(refreshed["publication_context"], old_context)
                encoded = json.dumps(refreshed, sort_keys=True)
                self.assertNotIn(connection.hub_url, encoded)
                self.assertNotIn(connection.secret, encoded)

    def test_comparison_uses_last_good_generation_instead_of_current_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, payloads, previous = self._published_fixture(root)
            candidate = deepcopy(payloads)
            candidate["openai-api"][0]["hostname"] = "Cage (changed metadata)"
            different_generation = publication_api._write_generation_payloads(root, candidate)
            previous["generation"] = different_generation
            with patch.object(hub_api, "upload_summary") as upload:
                refreshed = publication_api._publish_provider_payloads(
                    root, connection, candidate, deepcopy(previous), previous,
                    skip_unchanged=True,
                )
            upload.assert_called_once()
            self.assertNotEqual(refreshed["generation"], different_generation)

    def test_equivalent_pending_generation_repairs_then_publishes_complete_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, payloads, previous = self._published_provider_fixture(root, self._five_provider_totals())
            pending_generation = publication_api._write_generation_payloads(root, payloads)
            monitor.save_upload_state(root, publication_api._upload_state_for_generation(
                generation=pending_generation,
                previous_generation=previous["generation"],
                provider_ids={provider: monitor.provider_device_id(root, provider) for provider in payloads},
                attempted=["zllm"], state="repair_pending",
            ))
            with patch.object(hub_api, "upload_summary") as upload:
                refreshed = publication_api._publish_provider_payloads(
                    root, connection, payloads, deepcopy(previous), previous,
                    skip_unchanged=True,
                )
            self.assertEqual([call.args[1] for call in upload.call_args_list],
                             [payloads["zllm"][0]] + [payloads[provider][0] for provider in sorted(payloads)])
            self.assertNotEqual(refreshed["generation"], previous["generation"])
            self.assertIsNone(monitor.load_upload_state(root))

    def test_default_publication_republishes_even_an_equivalent_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection, payloads, previous = self._published_fixture(root)
            with patch.object(hub_api, "upload_summary") as upload:
                refreshed = publication_api._publish_provider_payloads(
                    root, connection, payloads, deepcopy(previous), previous,
                )
            upload.assert_called_once()
            self.assertNotEqual(refreshed["generation"], previous["generation"])

    def test_default_publication_rewrites_unchanged_peers_and_preserves_explicit_skips(self):
        for explicit in (set(), {"zllm"}):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                connection, _, previous = self._published_provider_fixture(root)
                candidate, status = self._provider_payloads(root, {"openai-api": 5, "zllm": 9}, "2026-09-03T00:01:00Z")
                with patch.object(hub_api, "upload_summary") as upload:
                    published = publication_api._publish_provider_payloads(
                        root, connection, candidate, status, previous, skip_upload_for=explicit,
                    )
                self.assertEqual([call.args[1] for call in upload.call_args_list],
                                 [candidate[provider][0] for provider in sorted(set(candidate) - explicit)])
                committed = publication_api._load_generation_payloads(root, published["generation"])
                self.assertEqual(committed, {provider: payload for provider, (payload, _) in candidate.items()})

    def test_previous_private_provider_stream_is_not_republished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            public_session = self._session(
                "public-provider",
                total=100,
                input_tokens=80,
                output_tokens=20,
            )
            payloads, status = monitor.aggregate_provider_summaries(
                root,
                [
                    (
                        record,
                        self._summary(
                            record.device_id,
                            {"codex:public-provider": public_session},
                        ),
                    )
                ],
            )
            private_provider = "internal-account-alias"
            service_api._add_previous_provider_payloads(
                root,
                [(record, self._summary(record.device_id, {}))],
                payloads,
                status,
                {
                    "providers": {
                        private_provider: {
                            "device_id": "cage-internal-account-alias-mac-deadbeef"
                        }
                    }
                },
            )

            self.assertEqual(set(payloads), {"openai-api"})
            self.assertNotIn(private_provider, json.dumps(status, sort_keys=True))

    def test_legacy_private_generation_allows_a_sanitized_replacement_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            legacy_provider = "legacy-account-alias"
            legacy_device = (
                f"cage-{legacy_provider}-{identity_api._platform_slug()}-"
                f"{identity_api.host_install_id(root)[:8]}"
            )
            legacy_generation = "a" * 32
            legacy_directory = publication_api._generation_directory(root, legacy_generation)
            legacy_directory.mkdir(mode=0o700)
            state_api._write_json(
                legacy_directory / "generation.json",
                {
                    "version": constants_api.UPLOAD_STATE_VERSION,
                    "generation": legacy_generation,
                    "providers": {legacy_provider: {"device_id": legacy_device}},
                },
            )
            state_api._write_json(
                legacy_directory / f"{legacy_provider}.json",
                self._summary(
                    legacy_device,
                    {
                        "codex:legacy": self._session(
                            "legacy",
                            total=10,
                            input_tokens=8,
                            output_tokens=2,
                            provider=legacy_provider,
                        )
                    },
                ),
            )

            with self.assertRaisesRegex(
                monitor.MonitorError, "generation provider is invalid"
            ):
                publication_api._load_generation_payloads(root, legacy_generation)
            self.assertEqual(
                publication_api._load_generation_payloads(
                    root, legacy_generation, allow_legacy_private=True
                ),
                {},
            )

            current_summary = self._summary(
                record.device_id,
                {
                    "codex:current": self._session(
                        "current",
                        total=20,
                        input_tokens=15,
                        output_tokens=5,
                        provider=legacy_provider,
                    )
                },
            )
            payloads, status = monitor.aggregate_provider_summaries(
                root, [(record, current_summary)]
            )
            status["split_complete"] = True

            with patch.object(hub_api, "_hub_request", return_value={}) as hub_request:
                published = publication_api._publish_provider_payloads(
                    root,
                    connection,
                    payloads,
                    status,
                    {"last_good_generation": legacy_generation},
                )

            self.assertIn("generation", published)
            self.assertEqual(hub_request.call_count, 1)
            self.assertEqual(hub_request.call_args.args[1:3], ("POST", "/api/ingest"))
            outbound = json.loads(hub_request.call_args.args[3])
            self.assertEqual(
                outbound["deviceId"],
                monitor.provider_device_id(root, "unattributed"),
            )
            self.assertNotIn(legacy_provider, json.dumps(outbound, sort_keys=True))

    def test_legacy_generation_rejects_a_forged_private_provider_device_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_provider = "legacy-account-alias"
            generation = "a" * 32
            directory = publication_api._generation_directory(root, generation)
            directory.mkdir(mode=0o700)
            state_api._write_json(
                directory / "generation.json",
                {
                    "version": constants_api.UPLOAD_STATE_VERSION,
                    "generation": generation,
                    "providers": {
                        legacy_provider: {
                            "device_id": monitor.provider_device_id(root, "openai-api")
                        }
                    },
                },
            )

            with self.assertRaisesRegex(
                monitor.MonitorError, "generation provider is invalid"
            ):
                publication_api._load_generation_payloads(
                    root, generation, allow_legacy_private=True
                )

    def test_repair_skips_exact_legacy_private_previous_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            legacy_provider = "legacy-account-alias"
            legacy_device = (
                f"cage-{legacy_provider}-{identity_api._platform_slug()}-"
                f"{identity_api.host_install_id(root)[:8]}"
            )
            legacy_generation = "a" * 32
            directory = publication_api._generation_directory(root, legacy_generation)
            directory.mkdir(mode=0o700)
            state_api._write_json(
                directory / "generation.json",
                {
                    "version": constants_api.UPLOAD_STATE_VERSION,
                    "generation": legacy_generation,
                    "providers": {legacy_provider: {"device_id": legacy_device}},
                },
            )
            monitor.save_upload_state(
                root,
                publication_api._upload_state_for_generation(
                    generation="b" * 32,
                    previous_generation=legacy_generation,
                    provider_ids={
                        "unattributed": monitor.provider_device_id(root, "unattributed")
                    },
                    attempted=["unattributed"],
                    state="repair_pending",
                ),
            )

            with patch.object(hub_api, "upload_summary") as upload:
                publication_api._repair_pending_upload(root, connection)

            upload.assert_not_called()
            self.assertIsNone(monitor.load_upload_state(root))

    def test_provider_upload_partial_failure_preserves_last_good_and_repairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            device_id = monitor.host_device_id(root)
            record = monitor.VolumeRegistration(
                "a" * 32,
                device_id,
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            first_summary = self._summary(
                device_id,
                {
                    "codex:openai": self._session(
                        "openai", total=80, input_tokens=60, output_tokens=20
                    ),
                    "codex:zllm": self._session(
                        "zllm", total=20, input_tokens=15, output_tokens=5, provider="zllm"
                    ),
                },
            )
            second_summary = self._summary(
                device_id,
                {
                    "codex:openai": self._session(
                        "openai", total=100, input_tokens=75, output_tokens=25
                    ),
                    "codex:zllm": self._session(
                        "zllm", total=30, input_tokens=20, output_tokens=10, provider="zllm"
                    ),
                },
            )
            first_payloads, first_status = monitor.aggregate_provider_summaries(
                root, [(record, first_summary)]
            )
            second_payloads, second_status = monitor.aggregate_provider_summaries(
                root, [(record, second_summary)]
            )
            first_status["split_complete"] = True
            second_status["split_complete"] = True
            with patch.object(hub_api, "upload_summary"):
                good = publication_api._publish_provider_payloads(
                    root, connection, first_payloads, first_status, None
                )
            old_generation = good["generation"]
            old_status = monitor.load_aggregate_status(root)

            with patch.object(
                hub_api,
                "upload_summary",
                side_effect=[
                    None,
                    monitor.MonitorError("new provider upload failed"),
                    monitor.MonitorError("rollback failed"),
                ],
            ) as upload, patch.object(hub_api, "delete_device") as delete:
                with self.assertRaisesRegex(monitor.MonitorError, "repair is pending"):
                    publication_api._publish_provider_payloads(
                        root, connection, second_payloads, second_status, old_status
                    )
            self.assertEqual(
                monitor.load_aggregate_status(root)["generation"], old_generation
            )
            pending = monitor.load_upload_state(root)
            self.assertIsNotNone(pending)
            self.assertEqual(pending["state"], "repair_pending")
            delete.assert_not_called()
            self.assertEqual(
                {call.args[1]["deviceId"] for call in upload.call_args_list},
                {
                    monitor.provider_device_id(root, "openai-api"),
                    monitor.provider_device_id(root, "zllm"),
                },
            )

            large_reply = json.dumps({"ok": True, "stats": {"padding": "x" * constants_api.MAX_OUTPUT_BYTES}}).encode()
            with patch.object(hub_api, "build_opener") as build:
                build.return_value.open.side_effect = lambda *_args, **_kwargs: io.BytesIO(large_reply)
                repaired_status = publication_api._publish_provider_payloads(
                    root, connection, second_payloads, second_status, old_status
                )
            self.assertNotEqual(repaired_status["generation"], old_generation)
            self.assertIsNone(monitor.load_upload_state(root))
            self.assertEqual(build.return_value.open.call_count, 4)
