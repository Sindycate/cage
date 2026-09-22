import contextlib
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from cage_core import cli, monitor
from cage_core.monitoring import (
    publication as publication_api,
    registry as registry_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorSecretPromptTests(MonitorTestCase):
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


class MonitorStateTests(MonitorTestCase):
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

    def test_status_shows_scan_and_upload_repair_causes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = monitor.register_volume(
                root, "docker", volume_name="codex-state-demo",
                repository="/work/demo", target="container", preset="main",
                display_name="Cage: demo (Container)", fingerprint=FINGERPRINT,
            )
            registry_api._record_scan_error(root, record, "provider upload repair failed")
            monitor.save_upload_state(root, publication_api._upload_state_for_generation(
                generation="a" * 32, previous_generation="b" * 32,
                provider_ids={"openai-api": monitor.provider_device_id(root, "openai-api")},
                attempted=["openai-api"], state="repair_pending",
                last_error="Token Monitor hub response is too large",
            ))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli._monitor_status(root)
            self.assertIn("Last scan error: provider upload repair failed", output.getvalue())
            self.assertIn("Last upload error: Token Monitor hub response is too large", output.getvalue())

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

    def test_provider_cli_allow_is_local_only_and_status_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with patch.object(cli.storage, "docker_command") as docker_command, contextlib.redirect_stdout(output):
                self.assertEqual(
                    cli._run_monitor(
                        ["provider", "allow", "approved-provider"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.36.2",
                    ),
                    0,
                )
            docker_command.assert_not_called()
            self.assertIn("private monitor state", output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    cli._run_monitor(
                        ["provider", "status", "--json"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.36.2",
                    ),
                    0,
                )
            self.assertEqual(json.loads(output.getvalue())["pending"], ["approved-provider"])

    def test_provider_cli_migrate_requires_confirmation_and_dispatches_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(
                    cli._run_monitor(
                        ["provider", "migrate", "approved-provider"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.36.2",
                    ),
                    1,
                )
            self.assertIn("provider migrate LABEL --yes", error.getvalue())
            output = io.StringIO()
            with patch.object(cli.storage, "docker_command", return_value="docker"), patch.object(
                cli.monitor,
                "migrate_provider_label",
                return_value={"updated_at": "now"},
            ) as migrate, contextlib.redirect_stdout(output):
                self.assertEqual(
                    cli._run_monitor(
                        ["provider", "migrate", "approved-provider", "--yes"],
                        config_root=root,
                        install_root=Path("/work/cage"),
                        cage_version="0.36.2",
                    ),
                    0,
                )
            self.assertEqual(migrate.call_args.args[3], "approved-provider")
            self.assertIn("named stream was preserved", output.getvalue())

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

    def test_monitor_add_auth_adopts_an_opaque_host_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (root / "config.toml").write_text(
                "\n".join(
                    [
                        "version = 1",
                        '[auth.shared]',
                        'tool = "codex"',
                        f"host_codex_dir = {json.dumps(str(source))}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            output = io.StringIO()

            def scan(_root, _docker, _install, record, **_kwargs):
                return record, {}

            with patch.object(cli.storage, "docker_command", return_value="docker"), patch.object(
                cli.monitor, "scan_registration", side_effect=scan
            ), contextlib.redirect_stdout(output):
                result = cli._run_monitor(
                    ["add", "--auth", "shared", "--json"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.36.0",
                )

            self.assertEqual(result, 0)
            rendered = output.getvalue()
            self.assertNotIn(str(source), rendered)
            public = json.loads(rendered)
            self.assertEqual(public["target"], "host")
            self.assertTrue(public["project_id"].startswith("cage-project-"))

    def test_monitor_disable_auth_restores_direct_host_routing_without_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "config.toml").write_text("", encoding="utf-8")
            (root / "config.toml").write_text(
                "\n".join(
                    [
                        "version = 1",
                        '[auth.shared]',
                        'tool = "codex"',
                        f"host_codex_dir = {json.dumps(str(source))}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            record = monitor.register_host_source(
                root, source, copy_auth=False, allow_replacement=True
            )
            output = io.StringIO()
            with patch.object(
                cli.storage,
                "docker_command",
                side_effect=AssertionError("disable must not require Docker"),
            ), contextlib.redirect_stdout(output):
                result = cli._run_monitor(
                    ["disable", "--auth", "shared", "--json"],
                    config_root=root,
                    install_root=Path("/work/cage"),
                    cage_version="0.36.0",
                )

            self.assertEqual(result, 0)
            rendered = json.loads(output.getvalue())
            self.assertEqual(rendered["logical_id"], record.logical_id)
            self.assertEqual(rendered["status"], "disabled")
            self.assertIsNone(monitor.registered_host_source(root, source))
            self.assertTrue(monitor.host_source_home(root, record).is_dir())
