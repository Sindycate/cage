import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import cli, monitor
from cage_core.monitoring import (
    collector as collector_api,
    identity as identity_api,
    locks as locks_api,
    snapshots as snapshots_api,
    volumes as volumes_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorRegistryTests(MonitorTestCase):
    def test_replacement_requires_explicit_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="main",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
            )
            changed = dict(FINGERPRINT, created_at="2026-08-28T00:00:00Z")
            with self.assertRaisesRegex(monitor.MonitorError, "explicit adoption"):
                monitor.register_volume(
                    root,
                    "docker",
                    volume_name="codex-state-demo",
                    repository="/work/demo",
                    target="container",
                    preset="main",
                    display_name="Cage: demo (Container)",
                    fingerprint=changed,
                )
            self.assertEqual(monitor.load_registry(root)[0].status, "needs-adoption")
            adopted = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="main",
                display_name="Cage: demo (Container)",
                fingerprint=changed,
                allow_replacement=True,
            )
            self.assertEqual(adopted.device_id, first.device_id)
            self.assertEqual(adopted.status, "active")

    def test_logical_target_identity_deduplicates_parallel_container_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="company-readonly",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
            )
            parallel = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="company-yolo",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
            )
            desktop = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-desktop",
                repository="/work/demo",
                target="desktop",
                preset="company-readonly",
                display_name="Cage: demo (Desktop)",
                fingerprint=dict(FINGERPRINT, name="codex-state-desktop"),
            )
            self.assertEqual(parallel.device_id, first.device_id)
            self.assertEqual(desktop.device_id, first.device_id)
            self.assertNotEqual(
                monitor.project_id_for(root, desktop.logical_id),
                monitor.project_id_for(root, first.logical_id),
            )
            self.assertEqual(len(monitor.load_registry(root)), 2)

    def test_one_volume_cannot_be_registered_to_two_active_devices(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="company-readonly",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
            )
            with self.assertRaisesRegex(monitor.MonitorError, "already registered"):
                monitor.register_volume(
                    root,
                    "docker",
                    volume_name="codex-state-demo",
                    repository="/work/other",
                    target="container",
                    preset="company-readonly",
                    display_name="Cage: other (Container)",
                    fingerprint=FINGERPRINT,
                    allow_replacement=True,
                )

    def test_parallel_volume_lock_is_non_reentrant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logical_id = "a" * 32
            with locks_api.try_volume_lock(root, logical_id) as acquired:
                self.assertTrue(acquired)
                with locks_api.try_volume_lock(root, logical_id) as nested:
                    self.assertFalse(nested)

    def test_mismatched_volume_label_requires_explicit_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mismatched = dict(FINGERPRINT, label_identity="b" * 32)
            with self.assertRaisesRegex(monitor.MonitorError, "explicitly"):
                monitor.register_volume(
                    root,
                    "docker",
                    volume_name="codex-state-demo",
                    repository="/work/demo",
                    target="container",
                    preset="main",
                    display_name="Cage: demo (Container)",
                    fingerprint=mismatched,
                )
            adopted = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="main",
                display_name="Cage: demo (Container)",
                fingerprint=mismatched,
                allow_replacement=True,
            )
            self.assertEqual(adopted.status, "active")

    def test_forget_requires_local_registration_and_leaves_tombstone_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("http://127.0.0.1:17321", "hub-secret")
            )
            record = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-demo",
                repository="/work/demo",
                target="container",
                preset="main",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
            )
            project_state = snapshots_api._project_state_path(root, record)
            events = []

            def delete(connection, device_id):
                events.append(monitor.load_registry(root)[0].status)
                raise monitor.MonitorError("hub unavailable")

            with patch.object(cli.monitor, "delete_device", side_effect=delete):
                output = io.StringIO()
                error = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    result = cli._run_monitor(
                        ["forget", record.device_id, "--yes"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.31.2",
                    )
            self.assertEqual(result, 1)
            self.assertEqual(events, ["disabled"])
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")
            self.assertTrue(project_state.exists())
            self.assertIn("remains disabled", error.getvalue())

            with patch.object(cli.monitor, "delete_device") as delete:
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    result = cli._run_monitor(
                        ["forget", "cage-unregistered", "--yes"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.31.2",
                    )
            self.assertEqual(result, 1)
            delete.assert_not_called()
            self.assertIn("monitor device was not found", error.getvalue())

            project_state.mkdir(parents=True, exist_ok=True)
            with patch.object(cli.monitor, "delete_device") as delete:
                result = cli._run_monitor(
                    ["forget", record.device_id, "--yes"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.31.2",
                )
            self.assertEqual(result, 0)
            delete.assert_called_once()
            self.assertFalse(project_state.exists())
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")

    def test_v1_registry_upgrades_to_one_device_and_keeps_exact_legacy_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_api.host_install_id(root)
            registry = root / "monitor" / "registry.json"
            legacy = {
                "logical_id": "a" * 32,
                "device_id": "cage-old-device",
                "volume_name": "codex-state-demo",
                "target": "container",
                "repository": "/work/demo",
                "display_name": "Cage: demo (Container)",
                "fingerprint": FINGERPRINT,
                "status": "active",
                "registered_at": "now",
                "last_scan_at": "",
                "last_success_at": "",
                "last_error": "",
            }
            registry.write_text(json.dumps({"version": 1, "registrations": [legacy]}), encoding="utf-8")
            os.chmod(registry, 0o600)

            record = monitor.load_registry(root)[0]
            self.assertEqual(record.device_id, monitor.host_device_id(root))
            self.assertEqual(record.legacy_device_id, "cage-old-device")
            monitor.save_registry(root, [record])
            self.assertEqual(json.loads(registry.read_text())["version"], 2)

    def test_discovery_lists_all_codex_state_volumes_without_adopting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registered = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-registered",
                "container",
                "/work/registered",
                "Cage: registered (Container)",
                dict(FINGERPRINT, name="codex-state-registered"),
            )
            monitor.save_registry(root, [registered])
            result = type(
                "DockerResult",
                (),
                {
                    "returncode": 0,
                    "stdout": "codex-state-unregistered\ncache\ncodex-state-registered\n",
                    "stderr": "",
                },
            )()

            def fingerprint(_docker, name):
                return dict(FINGERPRINT, name=name)

            with patch.object(collector_api.subprocess, "run", return_value=result), patch.object(
                volumes_api, "volume_fingerprint", side_effect=fingerprint
            ):
                discovered = monitor.discover_codex_volumes("docker", root)

            self.assertEqual(
                [item["volume_name"] for item in discovered],
                ["codex-state-registered", "codex-state-unregistered"],
            )
            self.assertTrue(discovered[0]["registered"])
            self.assertFalse(discovered[1]["registered"])
            self.assertEqual(monitor.load_registry(root), [registered])

    def test_recovered_volume_adoption_uses_exact_name_and_synthetic_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = dict(FINGERPRINT, name="codex-state-recovered")
            with patch.object(volumes_api, "volume_fingerprint", return_value=fingerprint):
                record = monitor.register_recovered_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                )

            self.assertEqual(record.repository, "/__cage_recovered__/codex-state-recovered")
            self.assertEqual(record.display_name, "Cage: Recovered recovered")
            self.assertEqual(record.target, "container")
            self.assertEqual(monitor.load_registry(root), [record])

    def test_normal_launch_reuses_an_exact_recovered_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fingerprint = dict(FINGERPRINT, name="codex-state-recovered")
            with patch.object(volumes_api, "volume_fingerprint", return_value=fingerprint):
                recovered = monitor.register_recovered_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                )

            historical = self._summary(
                recovered.device_id,
                {
                    "codex:historical": self._session(
                        "historical", total=42, input_tokens=32, output_tokens=10
                    )
                },
            )
            snapshots_api._save_volume_snapshot(root, recovered, historical)

            reused = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-recovered",
                repository="/work/recovered",
                target="container",
                preset="main",
                display_name="Cage: recovered (Container)",
                fingerprint=fingerprint,
                reuse_recovered=True,
            )

            self.assertEqual(reused.logical_id, recovered.logical_id)
            self.assertEqual(
                monitor.project_id_for(root, reused.logical_id),
                monitor.project_id_for(root, recovered.logical_id),
            )
            self.assertEqual(reused.device_id, recovered.device_id)
            self.assertEqual(reused.volume_name, recovered.volume_name)
            self.assertEqual(reused.fingerprint, recovered.fingerprint)
            self.assertEqual(reused.display_name, "Cage: recovered (Container)")
            stored = monitor.load_registry(root)[0]
            self.assertEqual(stored.logical_id, recovered.logical_id)
            self.assertEqual(stored.display_name, "Cage: recovered (Container)")
            self.assertEqual(stored.repository, recovered.repository)
            self.assertEqual(stored.fingerprint, recovered.fingerprint)
            self.assertEqual(monitor.load_volume_snapshot(root, reused), historical)
            aggregate, aggregate_status = monitor.aggregate_summaries(
                root, [(reused, historical)]
            )
            self.assertEqual(aggregate["allTime"]["totalTokens"], 42)
            self.assertEqual(aggregate_status["total_tokens"], 42)
            self.assertEqual(
                aggregate["today"]["sessions"]["codex:historical"]["projectLabel"],
                "Cage: recovered (Container)",
            )

    def test_normal_launch_does_not_reuse_a_replaced_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovered_fingerprint = dict(FINGERPRINT, name="codex-state-recovered")
            with patch.object(
                volumes_api, "volume_fingerprint", return_value=recovered_fingerprint
            ):
                monitor.register_recovered_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                )

            changed = dict(recovered_fingerprint, created_at="2026-08-28T00:00:00Z")
            with self.assertRaisesRegex(monitor.MonitorError, "already registered"):
                monitor.register_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                    repository="/work/recovered",
                    target="container",
                    preset="main",
                    display_name="Cage: recovered (Container)",
                    fingerprint=changed,
                    reuse_recovered=True,
                )

            conflicting_label = dict(recovered_fingerprint, label_identity="b" * 32)
            with self.assertRaisesRegex(monitor.MonitorError, "different logical target"):
                monitor.register_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                    repository="/work/recovered",
                    target="container",
                    preset="main",
                    display_name="Cage: recovered (Container)",
                    fingerprint=conflicting_label,
                    reuse_recovered=True,
                )

            real = monitor.register_volume(
                root,
                "docker",
                volume_name="codex-state-other",
                repository="/work/other",
                target="container",
                preset="main",
                display_name="Cage: other (Container)",
                fingerprint=dict(FINGERPRINT, name="codex-state-other"),
            )
            self.assertEqual(real.status, "active")

    def test_remove_device_state_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(monitor.MonitorError):
                monitor.remove_device_state(root, "../../outside")

    def test_remove_device_state_rejects_symlink_redirect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            devices = root / "monitor" / "devices"
            devices.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            (devices / "cage-redirect").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(monitor.MonitorError):
                monitor.remove_device_state(root, "cage-redirect")
            self.assertTrue((outside / "keep.txt").exists())
