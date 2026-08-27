import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cage_core import monitor


FINGERPRINT = {
    "name": "codex-state-demo",
    "driver": "local",
    "scope": "local",
    "created_at": "2026-08-27T00:00:00Z",
    "label_identity": "",
}


class MonitorStateTests(unittest.TestCase):
    def test_connection_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = monitor.MonitorConnection(
                "https://monitor.example.test/base/",
                "secret-value",
                300,
            )
            monitor.save_connection(root, connection)
            path = root / "monitor" / "connection.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                monitor.load_connection(root),
                monitor.MonitorConnection("https://monitor.example.test/base", "secret-value", 300),
            )
            monitor.disable_connection(root)
            self.assertIsNone(monitor.load_connection(root))

    def test_http_hub_is_private_only(self):
        self.assertEqual(
            monitor.normalize_hub_url("http://127.0.0.1:17321/"),
            "http://127.0.0.1:17321",
        )
        for value in (
            "http://monitor.example.test",
            "https://user:pass@monitor.example.test",
            "https://monitor.example.test/?secret=1",
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.normalize_hub_url(value)

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
            self.assertNotEqual(desktop.device_id, first.device_id)
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
            with monitor.try_volume_lock(root, logical_id) as acquired:
                self.assertTrue(acquired)
                with monitor.try_volume_lock(root, logical_id) as nested:
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

    def test_collector_mounts_only_existing_session_subpaths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = monitor.VolumeRegistration(
                logical_id="a" * 32,
                device_id="cage-" + "a" * 29,
                volume_name="codex-state-demo",
                target="container",
                repository="/work/demo",
                display_name="Cage: demo (Container)",
                fingerprint=FINGERPRINT,
                registered_at="now",
            )
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                if command[-1] == "true":
                    if "volume-subpath=sessions" in " ".join(command):
                        return type("Result", (), {"returncode": 0, "stderr": ""})()
                    if "volume-subpath=archived_sessions" in " ".join(command):
                        return type("Result", (), {"returncode": 1, "stderr": "volume-subpath archived_sessions does not exist"})()
                if command[1:3] == ["run", "--rm"]:
                    output_mount = next(item for item in command if "dst=/out/summary.json" in item)
                    output = Path(output_mount.split("src=", 1)[1].split(",", 1)[0])
                    output.write_text(
                        json.dumps({
                            "deviceId": state.device_id,
                            "trackedClients": ["codex"],
                            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
                            "today": {"totalTokens": 1},
                            "month": {"totalTokens": 2},
                            "allTime": {"totalTokens": 3},
                        }),
                        encoding="utf-8",
                    )
                    return type("Result", (), {"returncode": 0, "stderr": ""})()
                raise AssertionError(command)

            with patch("cage_core.monitor._subpath_available", side_effect=[True, False]), patch(
                "cage_core.monitor.subprocess.run", side_effect=fake_run
            ):
                result = monitor._run_collector("docker", "cage-token-monitor:dev", state, root, uid=os.getuid(), gid=os.getgid())
            self.assertEqual(result["allTime"]["totalTokens"], 3)
            collector_command = commands[-1]
            joined = " ".join(collector_command)
            self.assertIn("volume-subpath=sessions", joined)
            self.assertNotIn("volume-subpath=archived_sessions", joined)

    def test_empty_account_limits_may_have_probe_timestamp(self):
        payload = {
            "deviceId": "cage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "trackedClients": ["codex"],
            "limits": {
                "updatedAt": "2026-08-27T10:51:30.341Z",
                "refreshMs": 300000,
                "providers": [],
            },
            "today": {"totalTokens": 0},
            "month": {"totalTokens": 0},
            "allTime": {"totalTokens": 0},
        }
        self.assertEqual(
            monitor._validate_summary(payload, payload["deviceId"])["limits"]["providers"],
            [],
        )

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

    def test_volume_name_cannot_inject_mount_options(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.volume_fingerprint("docker", "state,dst=/escape")
        with self.assertRaises(monitor.MonitorError):
            monitor.ensure_codex_volume_labels(
                "docker", "state,dst=/escape", logical_id="a" * 32
            )

    def test_monitor_state_does_not_follow_directory_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (root / "monitor").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(monitor.MonitorError):
                monitor.save_connection(
                    root, monitor.MonitorConnection("https://hub.example", "secret")
                )
            self.assertFalse((outside / "connection.json").exists())


if __name__ == "__main__":
    unittest.main()
