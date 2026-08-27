import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener as urllib_build_opener
from unittest.mock import patch

from cage_core import cli, monitor


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
        self.assertEqual(
            monitor.normalize_hub_url("http://10.0.0.5:17321"),
            "http://10.0.0.5:17321",
        )
        for value in (
            "http://monitor.example.test",
            "http://localhost",
            "http://monitor.local",
            "http://0.0.0.0:17321",
            "https://user:pass@monitor.example.test",
            "https://monitor.example.test/?secret=1",
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.normalize_hub_url(value)

    def test_hub_request_uses_auth_and_a_real_redirect_handler(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b'{"ok":true}'

        seen = {}

        class Opener:
            def open(self, request, timeout):
                seen["request"] = request
                seen["timeout"] = timeout
                return Response()

        connection = monitor.MonitorConnection(
            "https://monitor.example.test", "hub-secret"
        )
        with patch("cage_core.monitor.build_opener", return_value=Opener()) as build:
            self.assertEqual(
                monitor._hub_request(connection, "GET", "/api/stats"),
                {"ok": True},
            )
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, HTTPRedirectHandler)
        self.assertEqual(seen["request"].headers["Authorization"], "Bearer hub-secret")
        self.assertEqual(seen["timeout"], 30)
        urllib_build_opener(monitor._NoRedirect())
        with self.assertRaisesRegex(monitor.MonitorError, "redirect refused"):
            monitor._NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://other.example"
            )

    def test_hub_http_errors_do_not_persist_response_body(self):
        secret = "reflected-hub-secret"
        error = HTTPError(
            "https://monitor.example.test/api/stats",
            500,
            "server error",
            {},
            io.BytesIO(secret.encode()),
        )

        class Opener:
            def open(self, _request, timeout):
                raise error

        with patch("cage_core.monitor.build_opener", return_value=Opener()):
            with self.assertRaises(monitor.MonitorError) as raised:
                monitor._hub_request(
                    monitor.MonitorConnection("https://monitor.example.test", secret),
                    "GET",
                    "/api/stats",
                )
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(str(raised.exception), "Token Monitor hub returned HTTP 500")

    def test_connection_verification_requires_real_hub_shapes(self):
        connection = monitor.MonitorConnection(
            "https://monitor.example.test", "hub-secret"
        )
        with patch(
            "cage_core.monitor._hub_request",
            side_effect=[{"ok": True, "role": "hub"}, {"devices": [], "periods": {}}],
        ) as request:
            monitor.verify_connection(connection)
        self.assertEqual(request.call_args_list[0].args[0].secret, "unused")
        self.assertEqual(request.call_args_list[1].args[0].secret, "hub-secret")

        with patch(
            "cage_core.monitor._hub_request",
            side_effect=[{"ok": True, "role": "hub"}, {"unexpected": True}],
        ):
            with self.assertRaisesRegex(monitor.MonitorError, "authentication check failed"):
                monitor.verify_connection(connection)

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
            self.assertIn("TOKEN_MONITOR_OPENCODE_AMBIENT=0", joined)
            self.assertIn("TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS=0", joined)
            self.assertIn("TOKEN_MONITOR_WSL_SCAN=0", joined)

    def test_unsupported_volume_subpath_fails_closed(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": 'invalid mount config for type "volume": volume-subpath is not supported',
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result):
            with self.assertRaisesRegex(monitor.MonitorError, "does not support volume-subpath"):
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "sessions"
                )

    def test_missing_volume_subpath_is_only_the_empty_directory_case(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": "invalid mount config: volume-subpath archived_sessions does not exist",
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result):
            self.assertFalse(
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "archived_sessions"
                )
            )

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
            device_state = monitor.monitor_root(root) / monitor.DEVICE_DIR / record.device_id
            device_state.mkdir(parents=True, exist_ok=True)
            events = []

            def delete(connection, device_id):
                events.append(monitor.load_registry(root)[0].status)
                raise monitor.MonitorError("hub unavailable")

            with patch.object(monitor, "delete_device", side_effect=delete):
                output = io.StringIO()
                error = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    result = cli._run_monitor(
                        ["forget", record.device_id, "--yes"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.31.1",
                    )
            self.assertEqual(result, 1)
            self.assertEqual(events, ["disabled"])
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")
            self.assertTrue(device_state.exists())
            self.assertIn("remains disabled", error.getvalue())

            with patch.object(monitor, "delete_device") as delete:
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    result = cli._run_monitor(
                        ["forget", "cage-unregistered", "--yes"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.31.1",
                    )
            self.assertEqual(result, 1)
            delete.assert_not_called()
            self.assertIn("monitor device was not found", error.getvalue())

            device_state.mkdir(parents=True, exist_ok=True)
            with patch.object(monitor, "delete_device") as delete:
                result = cli._run_monitor(
                    ["forget", record.device_id, "--yes"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.31.1",
                )
            self.assertEqual(result, 0)
            delete.assert_called_once()
            self.assertFalse(device_state.exists())
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")

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

    def test_summary_rejects_unexpected_wire_fields_and_source_paths(self):
        payload = {
            "deviceId": "cage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "trackedClients": ["codex"],
            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
            "today": {"totalTokens": 0},
            "month": {"totalTokens": 0},
            "allTime": {"totalTokens": 0},
        }
        for field in ("nativeSessions", "sentinel"):
            with self.subTest(field=field):
                candidate = dict(payload)
                candidate[field] = {"value": "unexpected"}
                with self.assertRaisesRegex(monitor.MonitorError, "unexpected fields"):
                    monitor._validate_summary(candidate, payload["deviceId"])

        candidate = dict(payload)
        candidate["today"] = {"totalTokens": 0, "source": "/Users/example/.codex"}
        with self.assertRaisesRegex(monitor.MonitorError, "source path"):
            monitor._validate_summary(candidate, payload["deviceId"])

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
