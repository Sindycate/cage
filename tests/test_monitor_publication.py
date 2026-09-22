import io
import json
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
