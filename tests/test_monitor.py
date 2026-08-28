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


class MonitorSecretPromptTests(unittest.TestCase):
    class _InteractiveInput:
        def isatty(self):
            return True

    class _Tty:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def test_unwritable_tty_retries_with_stderr_prompt(self):
        tty = self._Tty()
        with patch("cage_core.cli.open", return_value=tty), patch(
            "cage_core.cli.getpass.getpass",
            side_effect=[OSError("not writable"), "hub-secret"],
        ) as getpass_call:
            secret = cli._monitor_secret_from_terminal(
                stdin_stream=self._InteractiveInput()
            )

        self.assertEqual(secret, "hub-secret")
        self.assertEqual(getpass_call.call_args_list[0].kwargs["stream"], tty)
        self.assertIs(getpass_call.call_args_list[1].kwargs["stream"], cli.sys.stderr)
        self.assertTrue(tty.closed)

    def test_unavailable_prompt_gives_secret_stdin_guidance(self):
        with patch("cage_core.cli.open", side_effect=OSError("not writable")), patch(
            "cage_core.cli.getpass.getpass", side_effect=OSError("not writable")
        ):
            with self.assertRaisesRegex(cli.CliError, "use --secret-stdin"):
                cli._monitor_secret_from_terminal(
                    stdin_stream=self._InteractiveInput()
                )


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

    def test_status_reports_one_device_and_registered_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli._monitor_status(root, as_json=True), 0)
            status = json.loads(output.getvalue())
            self.assertEqual(status["device_id"], record.device_id)
            self.assertEqual(len(status["projects"]), 1)
            self.assertTrue(status["projects"][0]["project_id"].startswith("cage-project-"))

    def test_pricing_cli_set_status_and_remove(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_monitor(
                    ["pricing", "set", "gpt-private", "--input", "1.5", "--output", "8"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.32.0",
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                monitor.load_pricing(root)["gpt-private"],
                {"input_per_million": 1.5, "output_per_million": 8.0},
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    cli._run_monitor(
                        ["pricing", "status", "--json"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.32.0",
                    ),
                    0,
                )
            self.assertIn("gpt-private", json.loads(output.getvalue())["models"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli._run_monitor(
                        ["pricing", "remove", "gpt-private"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.32.0",
                    ),
                    0,
                )
            self.assertEqual(monitor.load_pricing(root), {})

    def test_split_dry_run_cli_is_reachable_and_does_not_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = {
                "total_tokens": 100,
                "cost_usd": 0.1,
                "providers": {
                    "zllm": {
                        "provider_label": "ZLLM",
                        "total_tokens": 100,
                        "cost_usd": 0.1,
                        "device_id": "cage-zllm-mac-aaaaaaaa",
                    }
                },
                "missing_prices": [],
            }
            output = io.StringIO()
            with patch.object(
                cli.monitor, "preview_provider_split", return_value=manifest
            ) as preview, patch.object(
                cli.storage, "docker_command", return_value="docker"
            ), contextlib.redirect_stdout(output):
                result = cli._run_monitor(
                    ["split", "--dry-run"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.34.0",
                )

            self.assertEqual(result, 0)
            preview.assert_called_once()
            self.assertIn("Dry-run provider split: 100 tokens", output.getvalue())

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
            self.assertIn("volume-nocopy", joined)
            self.assertNotIn("volume-subpath=archived_sessions", joined)
            self.assertIn("TOKEN_MONITOR_OPENCODE_AMBIENT=0", joined)
            self.assertIn("TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS=0", joined)
            self.assertIn("TOKEN_MONITOR_WSL_SCAN=0", joined)
            self.assertIn("TOKSCALE_CONFIG_DIR=/state/tokscale", joined)
            self.assertIn(
                f"/scan/codex:rw,noexec,nosuid,nodev,size=32m,uid={os.getuid()},gid={os.getgid()},mode=700",
                joined,
            )
            pricing = (
                monitor._project_state_path(root, state)
                / "tokscale"
                / "custom-pricing.json"
            )
            self.assertEqual(json.loads(pricing.read_text()), {"models": {}})

    def test_unsupported_volume_subpath_fails_closed(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": 'invalid mount config for type "volume": volume-subpath is not supported',
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(monitor.MonitorError, "does not support volume-subpath"):
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "sessions"
                )
        self.assertIn("volume-nocopy", " ".join(run.call_args.args[0]))

    def test_missing_volume_subpath_is_only_the_empty_directory_case(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": "invalid mount config: volume-subpath archived_sessions does not exist",
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result) as run:
            self.assertFalse(
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "archived_sessions"
                )
            )
        self.assertIn("volume-nocopy", " ".join(run.call_args.args[0]))

    def test_missing_volume_subpath_daemon_lstat_error_is_empty_directory_case(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": (
                    "docker: Error response from daemon: cannot access path "
                    "/var/lib/docker/volumes/codex-state-demo/_data/archived_sessions: "
                    "lstat /var/lib/docker/volumes/codex-state-demo/_data/archived_sessions: "
                    "no such file or directory"
                ),
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result):
            self.assertFalse(
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", "codex-state-demo", "archived_sessions"
                )
            )

    def test_missing_volume_subpath_long_daemon_error_classifies_before_display_truncation(self):
        volume_name = "codex-state-" + ("x" * 180)
        volume_path = f"/var/lib/docker/volumes/{volume_name}/_data/archived_sessions"
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stderr": (
                    f"docker: Error response from daemon: cannot access path {volume_path}: "
                    f"lstat {volume_path}: no such file or directory"
                ),
            },
        )()
        with patch("cage_core.monitor.subprocess.run", return_value=result):
            self.assertFalse(
                monitor._subpath_available(
                    "docker", "cage-token-monitor:dev", volume_name, "archived_sessions"
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
            project_state = monitor._project_state_path(root, record)
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
                        cage_version="0.31.2",
                    )
            self.assertEqual(result, 1)
            self.assertEqual(events, ["disabled"])
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")
            self.assertTrue(project_state.exists())
            self.assertIn("remains disabled", error.getvalue())

            with patch.object(monitor, "delete_device") as delete:
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
            with patch.object(monitor, "delete_device") as delete:
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
            monitor.host_install_id(root)
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

    @staticmethod
    def _session(
        session_id,
        *,
        total,
        input_tokens,
        output_tokens,
        model="gpt-test",
        cost=0,
        provider="openai",
    ):
        return {
            "client": "codex",
            "sessionId": session_id,
            "totalTokens": total,
            "costUsd": cost,
            "messageCount": 1,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "reasoningTokens": 0,
            "startedAt": "2026-08-27T00:00:00Z",
            "lastUsedAt": "2026-08-27T00:01:00Z",
            "projectId": "",
            "projectLabel": "",
            "models": {model: total},
            "modelCosts": ({model: cost} if cost else {}),
            "providers": {provider: total},
        }

    @staticmethod
    def _summary(device_id, sessions):
        total = sum(item["totalTokens"] for item in sessions.values())
        cost = sum(item["costUsd"] for item in sessions.values())
        period = {"totalTokens": total, "costUsd": cost, "sessions": sessions}
        return {
            "deviceId": device_id,
            "trackedClients": ["codex"],
            "limits": {"updatedAt": "", "refreshMs": 0, "providers": []},
            "today": dict(period),
            "month": dict(period),
            "allTime": dict(period),
        }

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

    def test_scan_clears_a_provider_stream_that_has_no_remaining_sessions(self):
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
            old_provider_id = monitor.provider_device_id(root, "zllm")
            monitor.save_split_status(
                root,
                {"complete": True, "device_ids": [old_provider_id]},
            )
            monitor._write_json(
                monitor.monitor_root(root) / monitor.AGGREGATE_STATUS_FILE,
                {
                    "version": monitor.STATE_VERSION,
                    "device_id": device_id,
                    "device_ids": [old_provider_id],
                    "providers": {
                        "zllm": {
                            "device_id": old_provider_id,
                            "provider": "zllm",
                            "provider_label": "ZLLM",
                            "total_tokens": 100,
                            "cost_usd": 0,
                        }
                    },
                    "updated_at": "2026-08-27T00:00:00Z",
                    "project_count": 1,
                    "duplicate_sessions": 0,
                    "total_tokens": 100,
                    "cost_usd": 0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 100,
                    "price_coverage_percent": 0,
                    "missing_models": ["gpt-test"],
                    "missing_prices": ["zllm:gpt-test"],
                },
            )

            def collect(_docker, _image, _record, _root, **_kwargs):
                return self._summary(device_id, {})

            with patch.object(
                monitor, "volume_fingerprint", return_value=FINGERPRINT
            ), patch.object(
                monitor, "ensure_collector_image", return_value="collector"
            ), patch.object(
                monitor, "_run_collector", side_effect=collect
            ), patch.object(
                monitor, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(monitor, "upload_summary") as upload:
                _updated, manifest = monitor.scan_all_registrations(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.34.0",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                )

            upload.assert_called_once()
            cleared = upload.call_args.args[1]
            self.assertEqual(cleared["deviceId"], old_provider_id)
            self.assertEqual(cleared["allTime"]["totalTokens"], 0)
            self.assertEqual(manifest["device_ids"], [old_provider_id])
            self.assertEqual(manifest["providers"]["zllm"]["total_tokens"], 0)

    def test_normal_scan_stops_when_legacy_unsplit_device_is_present(self):
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
            stats = {
                "devices": [{"deviceId": monitor.host_device_id(root)}],
                "periods": {},
            }
            with patch.object(monitor, "_hub_request", return_value=stats):
                with self.assertRaisesRegex(monitor.MonitorError, "split migration is pending"):
                    monitor.scan_all_registrations(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.34.0",
                        storage_policy=object(),
                        allow_build=False,
                        force=True,
                    )

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
                monitor, "volume_fingerprint", return_value=FINGERPRINT
            ), patch.object(
                monitor, "ensure_collector_image", return_value="collector"
            ), patch.object(
                monitor, "_run_collector", side_effect=collect
            ), patch.object(
                monitor, "_hub_request", side_effect=[old_stats, new_stats]
            ), patch.object(monitor, "upload_summary") as upload, patch.object(
                monitor, "delete_device"
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

            with patch.object(monitor.subprocess, "run", return_value=result), patch.object(
                monitor, "volume_fingerprint", side_effect=fingerprint
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
            with patch.object(monitor, "volume_fingerprint", return_value=fingerprint):
                record = monitor.register_recovered_volume(
                    root,
                    "docker",
                    volume_name="codex-state-recovered",
                )

            self.assertEqual(record.repository, "/__cage_recovered__/codex-state-recovered")
            self.assertEqual(record.display_name, "Cage: Recovered recovered")
            self.assertEqual(record.target, "container")
            self.assertEqual(monitor.load_registry(root), [record])

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

            monitor._archive_sessions_for_payload(state, payload)

            self.assertEqual(set(payload["today"]["sessions"]), {"codex:new"})
            self.assertEqual(
                set(payload["month"]["sessions"]), {"codex:old", "codex:new"}
            )
            self.assertNotIn("sessionDetailsOmitted", payload)

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
                monitor,
                "scan_all_registrations",
                return_value=(records, manifest),
            ), patch.object(
                monitor, "_hub_request", return_value=stats
            ), patch.object(monitor, "delete_device", side_effect=fail_second):
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
                monitor,
                "scan_all_registrations",
                return_value=(records, manifest),
            ), patch.object(
                monitor, "_hub_request", return_value=stats
            ), patch.object(monitor, "delete_device") as delete:
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

    def test_scan_all_collects_each_project_and_uploads_provider_snapshot(self):
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
                )
                for value in ("a", "b")
            ]
            monitor.save_registry(root, records)

            def collect(_docker, _image, record, _root, **_kwargs):
                session = self._session(
                    record.logical_id,
                    total=100,
                    input_tokens=80,
                    output_tokens=20,
                )
                return self._summary(
                    device_id,
                    {f"codex:{record.logical_id}": session},
                )

            with patch.object(
                monitor,
                "volume_fingerprint",
                side_effect=lambda _docker, name: next(
                    item.fingerprint for item in records if item.volume_name == name
                ),
            ), patch.object(
                monitor, "ensure_collector_image", return_value="collector"
            ), patch.object(
                monitor, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                monitor,
                "_hub_request",
                return_value={"devices": [], "periods": {}},
            ), patch.object(monitor, "upload_summary") as upload:
                updated, manifest = monitor.scan_all_registrations(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.32.0",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                )
            self.assertEqual(len(updated), 2)
            self.assertEqual(collector.call_count, 2)
            upload.assert_called_once()
            uploaded = upload.call_args.args[1]
            self.assertEqual(
                uploaded["deviceId"], monitor.provider_device_id(root, "openai-api")
            )
            self.assertEqual(uploaded["allTime"]["totalTokens"], 200)
            self.assertEqual(manifest["device_ids"], [uploaded["deviceId"]])
            self.assertEqual(manifest["providers"]["openai-api"]["total_tokens"], 200)

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

    def test_legacy_collector_archive_is_secured_without_following_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.json"
            archive.write_text("{}", encoding="utf-8")
            os.chmod(archive, 0o644)
            monitor._secure_collector_file(archive, max_bytes=1024)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            os.chmod(target, 0o644)
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(monitor.MonitorError, "unsafe collector"):
                monitor._secure_collector_file(link, max_bytes=1024)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

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
