import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import monitor
from cage_core.monitoring import (
    collector as collector_api,
    constants as constants_api,
    hub as hub_api,
    migrations as migrations_api,
    providers as providers_api,
    publication as publication_api,
    service as service_api,
    state as state_api,
    volumes as volumes_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorMigrationsTests(MonitorTestCase):
    def test_provider_label_migration_reuses_named_device_and_removes_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            monitor.save_connection(root, connection)
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            label = "approved-provider"
            monitor.approve_provider_label(root, label)
            session = self._session(
                "approved", total=100, input_tokens=80, output_tokens=20, provider=label
            )
            residual = self._session(
                "residual",
                total=25,
                input_tokens=20,
                output_tokens=5,
                provider="still-unapproved",
            )
            summaries = [
                (
                    record,
                    self._summary(
                        record.device_id,
                        {"codex:approved": session, "codex:residual": residual},
                    ),
                )
            ]
            named_device = monitor.provider_device_id(
                root, label, include_approved=True
            )
            unattributed_device = monitor.provider_device_id(root, "unattributed")
            before = {
                "devices": [
                    {
                        "deviceId": named_device,
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                    {
                        "deviceId": unattributed_device,
                        "periods": {"allTime": {"totalTokens": 125}},
                    },
                ],
                "periods": {},
            }
            after = {
                "devices": [
                    {
                        "deviceId": named_device,
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                    {
                        "deviceId": unattributed_device,
                        "periods": {"allTime": {"totalTokens": 25}},
                    },
                ],
                "periods": {},
            }
            uploaded = []

            def upload(_connection, payload, *, config_root):
                self.assertEqual(config_root, root)
                uploaded.append(payload)

            with patch.object(
                service_api, "_collect_registered_summaries", return_value=summaries
            ), patch.object(
                hub_api, "_hub_stats", side_effect=[before, after, after]
            ), patch.object(hub_api, "upload_summary", side_effect=upload), patch.object(
                hub_api, "delete_device"
            ) as delete:
                status = monitor.migrate_provider_label(
                    root,
                    "docker",
                    Path("/work/cage"),
                    label,
                    version="0.36.2",
                    storage_policy=object(),
                )

            self.assertEqual(len(uploaded), 1)
            self.assertEqual(uploaded[0]["deviceId"], unattributed_device)
            self.assertEqual(uploaded[0]["allTime"]["totalTokens"], 25)
            self.assertNotEqual(uploaded[0]["deviceId"], named_device)
            self.assertNotIn(label, json.dumps(uploaded, sort_keys=True))
            delete.assert_not_called()
            self.assertEqual(monitor.provider_label_status(root)["active"], [label])
            self.assertIsNone(monitor.load_provider_label_migration(root))
            self.assertFalse(monitor.provider_label_migration_pending(root))
            self.assertEqual(
                set(monitor.load_split_status(root)["device_ids"]),
                {named_device, unattributed_device},
            )
            self.assertEqual(status["providers"][label]["total_tokens"], 100)
            self.assertEqual(status["providers"]["unattributed"]["total_tokens"], 25)
            generation, payloads = publication_api._previous_generation(root, status)
            self.assertEqual(generation, status["generation"])
            self.assertEqual(set(payloads), {label, "unattributed"})
            future_payloads, _ = monitor.aggregate_provider_summaries(root, summaries)
            self.assertEqual(set(future_payloads), {label, "unattributed"})

    def test_provider_label_migration_refuses_hub_mismatch_without_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
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
            monitor.save_registry(root, [record])
            label = "approved-provider"
            monitor.approve_provider_label(root, label)
            session = self._session(
                "approved", total=100, input_tokens=80, output_tokens=20, provider=label
            )
            summaries = [
                (record, self._summary(record.device_id, {"codex:approved": session}))
            ]
            hub_stats = {
                "devices": [
                    {
                        "deviceId": monitor.provider_device_id(
                            root, label, include_approved=True
                        ),
                        "periods": {"allTime": {"totalTokens": 99}},
                    },
                    {
                        "deviceId": monitor.provider_device_id(root, "unattributed"),
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                ],
                "periods": {},
            }
            with patch.object(
                service_api, "_collect_registered_summaries", return_value=summaries
            ), patch.object(hub_api, "_hub_stats", return_value=hub_stats), patch.object(
                hub_api, "upload_summary"
            ) as upload:
                with self.assertRaisesRegex(monitor.MonitorError, "named provider stream"):
                    monitor.migrate_provider_label(
                        root,
                        "docker",
                        Path("/work/cage"),
                        label,
                        version="0.36.2",
                        storage_policy=object(),
                    )

            upload.assert_not_called()
            self.assertEqual(monitor.provider_label_status(root)["active"], [])
            self.assertTrue(monitor.provider_label_migration_pending(root))

    def test_provider_label_migration_recovers_an_old_named_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            monitor.save_connection(root, connection)
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            label = "approved-provider"
            monitor.approve_provider_label(root, label)
            session = self._session(
                "approved", total=100, input_tokens=80, output_tokens=20, provider=label
            )
            summaries = [
                (record, self._summary(record.device_id, {"codex:approved": session}))
            ]
            named_device = monitor.provider_device_id(
                root, label, include_approved=True
            )
            old_generation = "a" * 32
            old_directory = publication_api._generation_directory(root, old_generation)
            old_directory.mkdir(mode=0o700)
            state_api._write_json(
                old_directory / "generation.json",
                {
                    "version": constants_api.UPLOAD_STATE_VERSION,
                    "generation": old_generation,
                    "providers": {label: {"device_id": named_device}},
                },
            )
            state_api._write_json(
                old_directory / f"{label}.json",
                self._summary(record.device_id, {"codex:approved": session})
                | {"deviceId": named_device},
            )
            state_api._write_json(
                monitor.monitor_root(root) / constants_api.AGGREGATE_STATUS_FILE,
                {
                    "version": constants_api.STATE_VERSION,
                    "last_good_generation": old_generation,
                    "providers": {label: {"device_id": named_device}},
                },
            )
            before = {
                "devices": [
                    {
                        "deviceId": named_device,
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                    {
                        "deviceId": monitor.provider_device_id(root, "unattributed"),
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                ],
                "periods": {},
            }
            after = {
                "devices": [
                    {
                        "deviceId": named_device,
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                    {
                        "deviceId": monitor.provider_device_id(root, "unattributed"),
                        "periods": {"allTime": {"totalTokens": 0}},
                    },
                ],
                "periods": {},
            }
            with patch.object(
                service_api, "_collect_registered_summaries", return_value=summaries
            ), patch.object(
                hub_api, "_hub_stats", side_effect=[before, after, after]
            ), patch.object(hub_api, "upload_summary"):
                status = monitor.migrate_provider_label(
                    root,
                    "docker",
                    Path("/work/cage"),
                    label,
                    version="0.36.2",
                    storage_policy=object(),
                )

            self.assertEqual(status["providers"][label]["total_tokens"], 100)
            self.assertEqual(monitor.provider_label_status(root)["active"], [label])

    def test_provider_label_migration_resumes_after_unattributed_repartition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection("https://hub.example", "secret")
            monitor.save_connection(root, connection)
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            label = "approved-provider"
            monitor.approve_provider_label(root, label)
            monitor.save_provider_label_migration(
                root,
                migrations_api._provider_label_migration_record(
                    label,
                    state="prepared",
                    baseline_unattributed_tokens=100,
                    label_tokens=100,
                    residual_unattributed_tokens=0,
                ),
            )
            # Simulate an interruption after the named and residual hub
            # streams were verified and the local label was activated, but
            # before the complete replacement generation was committed.
            providers_api._activate_provider_label(root, label)
            monitor.save_upload_state(
                root,
                publication_api._upload_state_for_generation(
                    generation="b" * 32,
                    previous_generation="",
                    provider_ids={
                        label: monitor.provider_device_id(root, label),
                        "unattributed": monitor.provider_device_id(
                            root, "unattributed"
                        ),
                    },
                    attempted=[],
                    state="pending",
                ),
            )
            session = self._session(
                "approved", total=100, input_tokens=80, output_tokens=20, provider=label
            )
            summaries = [
                (record, self._summary(record.device_id, {"codex:approved": session}))
            ]
            republished = {
                "devices": [
                    {
                        "deviceId": monitor.provider_device_id(
                            root, label, include_approved=True
                        ),
                        "periods": {"allTime": {"totalTokens": 100}},
                    },
                    {
                        "deviceId": monitor.provider_device_id(root, "unattributed"),
                        "periods": {"allTime": {"totalTokens": 0}},
                    },
                ],
                "periods": {},
            }
            with patch.object(
                service_api, "_collect_registered_summaries", return_value=summaries
            ), patch.object(
                hub_api, "_hub_stats", side_effect=[republished, republished]
            ), patch.object(hub_api, "upload_summary") as upload:
                monitor.migrate_provider_label(
                    root,
                    "docker",
                    Path("/work/cage"),
                    label,
                    version="0.36.2",
                    storage_policy=object(),
                )

            upload.assert_not_called()
            self.assertEqual(monitor.provider_label_status(root)["active"], [label])
            self.assertIsNone(monitor.load_provider_label_migration(root))
            self.assertIsNone(monitor.load_upload_state(root))

    def test_split_migration_reconciles_hub_totals_before_deleting_old_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
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
            monitor.save_registry(root, [record])

            def collect(_docker, _image, _record, _root, **_kwargs):
                session = self._session(
                    "session", total=100, input_tokens=80, output_tokens=20
                )
                return self._summary(device_id, {"codex:session": session})

            provider_id = monitor.provider_device_id(root, "openai-api")
            old_stats = {
                "devices": [
                    {
                        "deviceId": device_id,
                        "periods": {"allTime": {"totalTokens": 100}},
                    }
                ],
                "periods": {},
            }
            new_stats = {
                "devices": [
                    {
                        "deviceId": provider_id,
                        "periods": {"allTime": {"totalTokens": 100}},
                    }
                ],
                "periods": {},
            }
            with patch.object(
                volumes_api, "volume_fingerprint", return_value=FINGERPRINT
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ), patch.object(
                hub_api, "_hub_request", side_effect=[old_stats, new_stats]
            ), patch.object(hub_api, "upload_summary") as upload, patch.object(
                hub_api, "delete_device"
            ) as delete:
                count = monitor.migrate_legacy_devices(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.34.0",
                    storage_policy=object(),
                )

            self.assertEqual(count, 1)
            upload.assert_called_once()
            delete.assert_called_once_with(
                monitor.load_connection(root), device_id
            )
            self.assertTrue(monitor.load_split_status(root)["complete"])

    def test_legacy_migration_is_verified_exact_and_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
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
                    legacy_device_id=f"cage-old-{value}",
                )
                for value in ("a", "b")
            ]
            monitor.save_registry(root, records)
            stats = {"devices": [], "periods": {}}
            manifest = {"device_ids": [], "total_tokens": 0}

            def fail_second(_connection, legacy_id):
                if legacy_id == "cage-old-b":
                    raise monitor.MonitorError("hub unavailable")

            monitor.save_split_status(root, {"complete": True, "device_ids": []})
            with patch.object(
                service_api,
                "scan_all_registrations",
                return_value=(records, manifest),
            ), patch.object(
                hub_api, "_hub_request", return_value=stats
            ), patch.object(hub_api, "delete_device", side_effect=fail_second):
                with self.assertRaisesRegex(monitor.MonitorError, "hub unavailable"):
                    monitor.migrate_legacy_devices(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.32.0",
                        storage_policy=object(),
                    )
            migrated = monitor.load_registry(root)
            self.assertEqual(migrated[0].legacy_device_id, "")
            self.assertEqual(migrated[1].legacy_device_id, "cage-old-b")

            with patch.object(
                service_api,
                "scan_all_registrations",
                return_value=(records, manifest),
            ), patch.object(
                hub_api, "_hub_request", return_value=stats
            ), patch.object(hub_api, "delete_device") as delete:
                count = monitor.migrate_legacy_devices(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.32.0",
                    storage_policy=object(),
                )
            self.assertEqual(count, 1)
            delete.assert_called_once_with(
                monitor.load_connection(root), "cage-old-b"
            )
            self.assertEqual(monitor.load_registry(root)[1].legacy_device_id, "")
