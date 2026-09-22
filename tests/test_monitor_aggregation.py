import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import (
    collector as collector_api,
    identity as identity_api,
    pricing as pricing_api,
    state as state_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorAggregationTests(MonitorTestCase):
    def test_public_aggregation_reads_one_identity_and_pricing_snapshot_for_all_periods(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = self._registered_monitor_projects(root, "openai", "zllm")
            installation = identity_api.read_identity(root)
            monitor.save_pricing(root, {
                "openai-api:gpt-test": {"input_per_million": 1, "output_per_million": 4},
                "zllm:gpt-test": {"input_per_million": 2, "output_per_million": 8},
            })
            summaries = []
            for record, provider, base_total in zip(records, ("openai", "zllm"), (100, 200)):
                summary = self._summary(record.device_id, {})
                for period, factor in zip(("today", "month", "allTime"), (1, 2, 3)):
                    total = base_total * factor
                    session = self._session(
                        provider, provider=provider, total=total,
                        input_tokens=total * 4 // 5, output_tokens=total // 5,
                    )
                    summary[period] = self._summary(
                        record.device_id, {f"codex:{provider}": session}
                    )[period]
                summaries.append((record, summary))

            timestamp = "2026-09-22T12:34:56Z"
            for aggregate in (monitor.aggregate_summaries, monitor.aggregate_provider_summaries):
                with self.subTest(aggregate=aggregate.__name__), patch.object(
                    pricing_api, "load_pricing", wraps=pricing_api.load_pricing
                ) as load_pricing, patch.object(
                    identity_api, "host_install_id", wraps=identity_api.host_install_id
                ) as load_identity, patch.object(
                    state_api, "_now", return_value=timestamp
                ) as now:
                    payloads, status = aggregate(root, summaries)

                load_pricing.assert_called_once_with(root)
                load_identity.assert_called_once_with(root)
                now.assert_called_once_with()
                self.assertEqual(status["updated_at"], timestamp)
                self.assertEqual(status["device_id"], installation.host_device_id)
                if aggregate is monitor.aggregate_provider_summaries:
                    self.assertEqual(set(payloads), {"openai-api", "zllm"})
                    cases = [
                        (payloads["openai-api"], records[0], "openai", 100, .00016),
                        (payloads["zllm"], records[1], "zllm", 200, .00064),
                    ]
                    for provider, (payload, _) in payloads.items():
                        self.assertEqual(payload["deviceId"], installation.provider_device_id(
                            provider, {"openai-api", "zllm"}
                        ))
                else:
                    cases = [((payloads, status), records[0], "openai", 300, .0008)]

                for (payload, stream_status), record, session_id, tokens, cost in cases:
                    self.assertEqual(payload["updatedAt"], timestamp)
                    self.assertEqual(stream_status["updated_at"], timestamp)
                    self.assertEqual(
                        payload["today"]["sessions"][f"codex:{session_id}"]["projectId"],
                        installation.project_id(record.logical_id),
                    )
                    for period, factor in zip(("today", "month", "allTime"), (1, 2, 3)):
                        self.assertEqual(payload[period]["totalTokens"], tokens * factor)
                        self.assertAlmostEqual(payload[period]["costUsd"], cost * factor)
                        self.assertTrue(stream_status["period_pricing"][period]["cost_complete"])
                for period, factor in zip(("today", "month", "allTime"), (1, 2, 3)):
                    self.assertEqual(status["period_pricing"][period]["total_tokens"], 300 * factor)
                    self.assertAlmostEqual(status["period_pricing"][period]["cost_usd"], .0008 * factor)

    def test_aggregate_deduplicates_identical_and_monotonic_session_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device_id = monitor.host_device_id(root)
            first = monitor.VolumeRegistration(
                "a" * 32, device_id, "codex-state-a", "container", "/work/a",
                "Cage: a (Container)", FINGERPRINT,
            )
            second = monitor.VolumeRegistration(
                "b" * 32, device_id, "codex-state-b", "container", "/work/b",
                "Cage: b (Container)", dict(FINGERPRINT, name="codex-state-b"),
            )
            old = self._session("shared", total=100, input_tokens=70, output_tokens=30)
            new = self._session("shared", total=120, input_tokens=80, output_tokens=40)
            unique = self._session("unique", total=50, input_tokens=40, output_tokens=10)
            payload, status = monitor.aggregate_summaries(
                root,
                [
                    (first, self._summary(device_id, {"codex:shared": old, "codex:unique": unique})),
                    (second, self._summary(device_id, {"codex:shared": new})),
                ],
            )
            self.assertEqual(payload["allTime"]["totalTokens"], 170)
            self.assertEqual(status["duplicate_sessions"], 1)
            self.assertEqual(
                payload["today"]["sessions"]["codex:shared"]["projectLabel"],
                "Cage: Unattributed",
            )
            self.assertEqual(
                payload["today"]["sessions"]["codex:unique"]["projectLabel"],
                "Cage: a (Container)",
            )

    def test_aggregate_rejects_incompatible_session_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    value * 32, device_id, f"codex-state-{value}", "container",
                    f"/work/{value}", f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for value in ("a", "b")
            ]
            left = self._session("shared", total=100, input_tokens=70, output_tokens=30)
            right = self._session("shared", total=100, input_tokens=60, output_tokens=40)
            with self.assertRaisesRegex(monitor.MonitorError, "conflicting copies"):
                monitor.aggregate_summaries(
                    root,
                    [(records[0], self._summary(device_id, {"codex:shared": left})),
                     (records[1], self._summary(device_id, {"codex:shared": right}))],
                )

    def test_provider_aggregation_deduplicates_before_partitioning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    value * 32,
                    device_id,
                    f"codex-state-{value}",
                    "container",
                    f"/work/{value}",
                    f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for value in ("a", "b")
            ]
            openai = self._session(
                "openai-session", total=80, input_tokens=60, output_tokens=20
            )
            zllm = self._session(
                "zllm-session",
                total=20,
                input_tokens=15,
                output_tokens=5,
                provider="zllm",
            )
            duplicate = self._session(
                "openai-session", total=80, input_tokens=60, output_tokens=20
            )
            streams, manifest = monitor.aggregate_provider_summaries(
                root,
                [
                    (
                        records[0],
                        self._summary(
                            device_id,
                            {
                                "codex:openai-session": openai,
                                "codex:zllm-session": zllm,
                            },
                        ),
                    ),
                    (
                        records[1],
                        self._summary(
                            device_id,
                            {"codex:openai-session": duplicate},
                        ),
                    ),
                ],
            )

            self.assertEqual(set(streams), {"openai-api", "zllm"})
            self.assertEqual(
                streams["openai-api"][0]["deviceId"],
                monitor.provider_device_id(root, "openai-api"),
            )
            self.assertEqual(streams["openai-api"][0]["allTime"]["totalTokens"], 80)
            self.assertEqual(streams["zllm"][0]["allTime"]["totalTokens"], 20)
            self.assertEqual(manifest["total_tokens"], 100)
            self.assertEqual(manifest["duplicate_sessions"], 1)
            self.assertEqual(
                manifest["device_ids"],
                [
                    monitor.provider_device_id(root, "openai-api"),
                    monitor.provider_device_id(root, "zllm"),
                ],
            )

    def test_multi_provider_session_is_kept_in_unattributed_stream(self):
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
            session = self._session(
                "mixed",
                total=100,
                input_tokens=80,
                output_tokens=20,
            )
            session["providers"] = {"openai": 60, "zllm": 40}
            streams, manifest = monitor.aggregate_provider_summaries(
                root,
                [(record, self._summary(record.device_id, {"codex:mixed": session}))],
            )

            self.assertEqual(set(streams), {"unattributed"})
            self.assertEqual(
                streams["unattributed"][0]["deviceId"],
                monitor.provider_device_id(root, "unattributed"),
            )
            self.assertEqual(manifest["total_tokens"], 100)
            self.assertEqual(
                streams["unattributed"][1]["missing_prices"],
                ["unattributed:gpt-test"],
            )

    def test_private_provider_labels_are_counted_without_becoming_hub_data(self):
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
            private_provider = "internal-account-alias"
            session = self._session(
                "private-provider",
                total=100,
                input_tokens=80,
                output_tokens=20,
                provider=private_provider,
            )
            streams, manifest = monitor.aggregate_provider_summaries(
                root,
                [
                    (
                        record,
                        self._summary(
                            record.device_id,
                            {"codex:private-provider": session},
                        ),
                    )
                ],
            )

            self.assertEqual(set(streams), {"unattributed"})
            payload = streams["unattributed"][0]
            self.assertNotIn(private_provider, json.dumps(payload, sort_keys=True))
            self.assertEqual(
                payload["today"]["sessions"]["codex:private-provider"]["providers"],
                {"unattributed": 100},
            )
            self.assertEqual(manifest["providers"]["unattributed"]["total_tokens"], 100)
            with self.assertRaisesRegex(monitor.MonitorError, "provider identity"):
                monitor.provider_device_id(root, private_provider)

    def test_provider_qualified_pricing_does_not_cross_provider_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.set_model_pricing(
                root,
                "gpt-test",
                input_per_million=9,
                output_per_million=9,
                cache_read_per_million=None,
            )
            monitor.set_model_pricing(
                root,
                "zllm:gpt-test",
                input_per_million=1,
                output_per_million=2,
                cache_read_per_million=None,
            )
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            openai = self._session(
                "openai",
                total=100,
                input_tokens=80,
                output_tokens=20,
            )
            zllm = self._session(
                "zllm",
                total=100,
                input_tokens=80,
                output_tokens=20,
                provider="zllm",
            )
            streams, _ = monitor.aggregate_provider_summaries(
                root,
                [
                    (
                        record,
                        self._summary(
                            record.device_id,
                            {"codex:openai": openai, "codex:zllm": zllm},
                        ),
                    )
                ],
            )

            self.assertEqual(streams["openai-api"][1]["cost_usd"], 0.0009)
            self.assertEqual(streams["zllm"][1]["cost_usd"], 0.00012)
            self.assertEqual(streams["openai-api"][1]["priced_tokens"], 100)
            self.assertEqual(streams["zllm"][1]["priced_tokens"], 100)

    def test_non_openai_upstream_cost_is_not_treated_as_verified_price(self):
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
            zllm = self._session(
                "zllm",
                total=100,
                input_tokens=80,
                output_tokens=20,
                provider="zllm",
                cost=99,
            )
            streams, manifest = monitor.aggregate_provider_summaries(
                root,
                [(record, self._summary(record.device_id, {"codex:zllm": zllm}))],
            )

            self.assertEqual(streams["zllm"][1]["cost_usd"], 0.0)
            self.assertEqual(streams["zllm"][1]["priced_tokens"], 0)
            self.assertEqual(streams["zllm"][1]["unpriced_tokens"], 100)
            self.assertEqual(manifest["cost_usd"], 0.0)

    def test_multi_model_session_keeps_tokens_but_stays_unpriced_without_evidence(self):
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
            session = self._session(
                "multi",
                total=100,
                input_tokens=80,
                output_tokens=20,
                cost=3,
            )
            session["models"] = {"gpt-a": 60, "gpt-b": 40}
            session["modelCosts"] = {}
            payload, status = monitor.aggregate_summaries(
                root,
                [(record, self._summary(record.device_id, {"codex:multi": session}))],
            )

            self.assertEqual(payload["allTime"]["totalTokens"], 100)
            self.assertEqual(payload["allTime"]["costUsd"], 0.0)
            self.assertEqual(payload["allTime"]["modelCosts"], {})
            self.assertEqual(status["priced_tokens"], 0)
            self.assertEqual(status["unpriced_tokens"], 100)
            self.assertEqual(status["missing_models"], ["gpt-a", "gpt-b"])

    def test_aggregate_accepts_empty_period_without_session_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            summary = self._summary(device_id, {})
            for period_name in ("today", "month", "allTime"):
                summary[period_name].pop("sessions")

            payload, status = monitor.aggregate_summaries(root, [(record, summary)])

            self.assertEqual(payload["allTime"]["totalTokens"], 0)
            self.assertEqual(status["price_coverage_percent"], 100.0)
            self.assertEqual(status["missing_models"], [])

    def test_aggregate_rejects_missing_session_details_for_nonempty_period(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            summary = self._summary(
                device_id,
                {"codex:session": self._session(
                    "session", total=10, input_tokens=8, output_tokens=2
                )},
            )
            summary["allTime"].pop("sessions")

            with self.assertRaisesRegex(
                monitor.MonitorError, "collector did not provide complete session details"
            ):
                monitor.aggregate_summaries(root, [(record, summary)])

    def test_custom_pricing_is_private_and_reports_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.set_model_pricing(
                root,
                "gpt-private",
                input_per_million=1.0,
                output_per_million=2.0,
                cache_read_per_million=0.1,
            )
            pricing_path = root / "monitor" / "pricing.json"
            self.assertEqual(pricing_path.stat().st_mode & 0o777, 0o600)
            record = monitor.VolumeRegistration(
                "a" * 32, monitor.host_device_id(root), "codex-state-a", "container",
                "/work/a", "Cage: a (Container)", FINGERPRINT,
            )
            priced = self._session(
                "priced", total=75, input_tokens=50, output_tokens=25, model="gpt-private"
            )
            missing = self._session(
                "missing", total=25, input_tokens=20, output_tokens=5, model="gpt-unknown"
            )
            _, status = monitor.aggregate_summaries(
                root,
                [(record, self._summary(record.device_id, {"codex:priced": priced, "codex:missing": missing}))],
            )
            self.assertEqual(status["price_coverage_percent"], 75.0)
            self.assertEqual(status["missing_models"], ["gpt-unknown"])
            self.assertEqual(status["cost_usd"], 0.0001)

    def test_archive_uses_period_window_after_repricing_refreshes_shared_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()

            def archived_session(session_id, day, total):
                return {
                    "client": "codex",
                    "sessionId": session_id,
                    "periods": {
                        "today": {"totalTokens": total},
                        "month": {"totalTokens": total},
                        "allTime": {"totalTokens": total},
                    },
                    "periodWindows": {
                        "today": {"day": day},
                        "month": {"month": "2026-08"},
                        "allTime": {},
                    },
                    # Upstream changes this shared marker when another
                    # period is refreshed, such as after repricing.
                    "day": "2026-08-28",
                    "month": "2026-08",
                }

            archive = {
                "version": 1,
                "sessions": {
                    "codex:old": archived_session("old", "2026-08-27", 10),
                    "codex:new": archived_session("new", "2026-08-28", 20),
                },
            }
            (state / "session-usage-archive.json").write_text(
                json.dumps(archive), encoding="utf-8"
            )
            payload = {
                "periodWindows": {
                    "today": {"key": "2026-08-28"},
                    "month": {"key": "2026-08"},
                },
                "today": {"totalTokens": 20},
                "month": {"totalTokens": 30},
                "allTime": {"totalTokens": 30},
                "sessionDetailsOmitted": {"today": 1},
            }

            collector_api._archive_sessions_for_payload(state, payload)

            self.assertEqual(set(payload["today"]["sessions"]), {"codex:new"})
            self.assertEqual(
                set(payload["month"]["sessions"]), {"codex:old", "codex:new"}
            )
            self.assertNotIn("sessionDetailsOmitted", payload)
