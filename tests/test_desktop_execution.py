"""Regression coverage for ChatGPT Desktop SSH-backed Cage targets."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


desktop = load_module("cage_desktop", "cage-desktop.py")
cage_config = load_module("cage_config_desktop", "cage-config.py")
cage_tui = load_module("cage_tui_desktop", "cage-tui.py")
remote = load_module("codex_remote", "codex-remote.py")


class DesktopIdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_and_preset_isolated(self):
        repo = Path("/Users/example/repo")
        first = desktop.target_id(repo, "provider")
        self.assertEqual(first, desktop.target_id(repo, "provider"))
        self.assertNotEqual(first, desktop.target_id(repo, "openai"))
        self.assertRegex(first, r"^[0-9a-f]{16}$")

    def test_parser_uses_public_positional_repository_interface(self):
        parser = desktop.build_parser()
        args = parser.parse_args(
            [
                "--config-dir",
                "/tmp/config",
                "--launcher",
                "/opt/cage",
                "start",
                "--preset",
                "provider",
                "--no-open",
                "/tmp/repo",
            ]
        )
        self.assertEqual(args.repo, "/tmp/repo")
        self.assertTrue(args.no_open)

    def test_supervisor_parser_preserves_internal_command(self):
        args = desktop.build_parser().parse_args(
            [
                "--config-dir",
                "/tmp/config",
                "--launcher",
                "/opt/cage",
                "supervise",
                "0123456789abcdef",
                "--",
                "/opt/cage",
                "--preset",
                "provider",
                "/tmp/repo",
            ]
        )
        if args.separator == "--":
            command = args.command
        else:
            command = ([args.separator] if args.separator else []) + args.command
        self.assertEqual(command[0], "/opt/cage")
        self.assertIn("--preset", command)

    def test_heartbeat_update_preserves_bind_source_inode(self):
        with tempfile.TemporaryDirectory() as raw:
            heartbeat = Path(raw).resolve() / "heartbeat"
            desktop.atomic_write(heartbeat, b"starting\n")
            before = heartbeat.stat().st_ino
            desktop.update_in_place(heartbeat, b"alive\n")
            self.assertEqual(heartbeat.stat().st_ino, before)
            self.assertEqual(heartbeat.read_bytes(), b"alive\n")

    def test_three_target_overrides_are_mutually_exclusive(self):
        result = subprocess.run(
            [str(ROOT / "cage"), "--host", "--desktop", str(ROOT)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)

    def test_no_open_is_rejected_outside_desktop(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_dir = root / "config" / "cage"
            home = root / "home"
            config_dir.mkdir(parents=True)
            home.mkdir()
            (config_dir / "config.toml").write_text(
                'version = 1\ndefault_preset = "main"\n'
                '[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                HOME=str(home),
                XDG_CONFIG_HOME=str(root / "config"),
            )
            result = subprocess.run(
                [str(ROOT / "cage"), "--no-open", str(ROOT)],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only valid for desktop", result.stderr)

    def test_desktop_adds_only_the_openssh_chroot_capability(self):
        launcher = (ROOT / "cage").read_text(encoding="utf-8")
        desktop_block = launcher[
            launcher.index('if [ "$EXEC_TARGET" = "desktop" ]; then', launcher.index("DOCKER_ARGS=(")):
            launcher.index("# Git/SSH mounts and env vars", launcher.index("DOCKER_ARGS=("))
        ]
        self.assertIn("--cap-add SYS_CHROOT", desktop_block)
        self.assertNotIn("--cap-add FOWNER", desktop_block)

        entrypoint = (ROOT / "entrypoint-codex.sh").read_text(encoding="utf-8")
        self.assertIn("DisableForwarding yes", entrypoint)
        self.assertIn("AllowStreamLocalForwarding no", entrypoint)


class DesktopConfigTests(unittest.TestCase):
    def test_desktop_target_is_codex_only(self):
        data = {
            "defaults": {"preset": "main"},
            "presets": {"main": {"tool": "codex", "target": "desktop"}},
        }
        resolved = cage_config.resolve_config(
            data, Path("/tmp/config.toml"), "/tmp/repo", "main"
        )
        self.assertEqual(resolved.target, "desktop")

        data["presets"]["main"]["tool"] = "claude"
        with self.assertRaises(cage_config.ConfigError):
            cage_config.resolve_config(
                data, Path("/tmp/config.toml"), "/tmp/repo", "main"
            )

    def test_shell_output_carries_target_and_saved_source(self):
        resolved = cage_config.ResolvedConfig(
            config_path=Path("/tmp/config.toml"),
            repo_path="/tmp/repo",
            preset_name="provider",
            preset_source="project:/tmp/repo",
            tool="codex",
            target="desktop",
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            cage_config.emit_shell(resolved)
        self.assertIn("CAGE_EXEC_TARGET=desktop", output.getvalue())
        self.assertIn("CAGE_PRESET_SOURCE=project:/tmp/repo", output.getvalue())

    def test_tui_surfaces_persistent_ssh_risks(self):
        controller = object.__new__(cage_tui.Controller)
        controller.target_override = ""
        controller.yolo_override = ""
        controller.net_override = ""
        controller.snapshot = {
            "config": {
                "defaults": {},
                "identities": {},
                "mcp_packs": {},
            }
        }
        risks = controller.risks(
            {"tool": "codex", "target": "desktop", "net": "gate"}
        )
        self.assertTrue(any("remains running" in item for item in risks))
        self.assertTrue(any("SSH alias" in item for item in risks))
        self.assertEqual(
            cage_tui.execution_target_label("desktop"),
            "Desktop via Cage container",
        )


class DesktopSshSetupTests(unittest.TestCase):
    def test_setup_adds_one_include_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            config_root = home / ".config" / "cage"
            ssh = home / ".ssh"
            install = root / "install"
            bin_dir = root / "bin"
            for path in (ssh, install, bin_dir):
                path.mkdir(parents=True, exist_ok=True)
            source_launcher = root / "source-cage"
            installed_launcher = install / "cage"
            docker = bin_dir / "docker"
            helper = install / "cage-desktop.py"
            (install / ".cage-install").write_text("0.26.0\n", encoding="utf-8")
            for launcher in (source_launcher, installed_launcher):
                launcher.write_text(
                    "#!/bin/sh\necho 'cage 0.26.0'\n", encoding="utf-8"
                )
                launcher.chmod(0o755)
            docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            docker.chmod(0o755)
            helper.write_text("# helper\n", encoding="utf-8")
            config = ssh / "config"
            original = b"# user comment\nHost work\n  HostName example.com\n"
            config.write_bytes(original)
            config.chmod(0o640)
            args = argparse.Namespace(
                config_dir=str(config_root), launcher=str(source_launcher)
            )

            def which(name):
                return {
                    "cage": str(installed_launcher),
                    "docker": str(docker),
                }.get(name)

            with (
                patch.object(desktop.Path, "home", return_value=home),
                patch.object(desktop.sys, "platform", "darwin"),
                patch.object(desktop.shutil, "which", side_effect=which),
            ):
                self.assertEqual(desktop.command_setup(args), 0)
                self.assertEqual(desktop.command_setup(args), 0)

            text = config.read_text(encoding="utf-8")
            include = f"Include {config_root.resolve() / 'desktop' / 'ssh_config'}"
            self.assertEqual(text.count(include), 1)
            self.assertIn(original.decode(), text)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
            self.assertEqual(
                stat.S_IMODE(
                    (config_root / "desktop" / "setup.json").stat().st_mode
                ),
                0o600,
            )

    def test_setup_preserves_ssh_config_symlink_and_target_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            ssh = home / ".ssh"
            ssh.mkdir(parents=True)
            target = ssh / "config.real"
            target.write_text("# preserved\n", encoding="utf-8")
            target.chmod(0o640)
            config = ssh / "config"
            config.symlink_to(target.name)
            before = os.lstat(config)
            args = argparse.Namespace(config_dir=str(home / ".config" / "cage"))

            with patch.object(desktop.Path, "home", return_value=home):
                desktop.ensure_ssh_include(args)

            after = os.lstat(config)
            self.assertTrue(config.is_symlink())
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertTrue(target.read_text(encoding="utf-8").startswith("Include "))
            self.assertIn("# preserved\n", target.read_text(encoding="utf-8"))

    def test_alias_conflict_is_found_in_included_ssh_config(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            included = home / ".ssh" / "conf.d" / "work"
            included.parent.mkdir(parents=True)
            (home / ".ssh" / "config").write_text(
                "Include conf.d/*\n", encoding="utf-8"
            )
            included.write_text(
                "Host cage-provider-repo-deadbeef\n  HostName example.com\n",
                encoding="utf-8",
            )
            with patch.object(desktop.Path, "home", return_value=home):
                self.assertTrue(
                    desktop.base_ssh_alias_conflict(
                        "cage-provider-repo-deadbeef"
                    )
                )
                self.assertFalse(desktop.base_ssh_alias_conflict("cage-other"))

    def test_replaced_client_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            root.mkdir(exist_ok=True)
            private, public = desktop.generate_client_key(root)
            metadata = {
                "client_public_key_sha256": desktop.hashlib.sha256(
                    desktop.read_bounded(public)
                ).hexdigest()
            }
            desktop.validate_client_key(root, metadata, private, public)
            public.write_text("ssh-ed25519 AAAA replaced\n", encoding="utf-8")
            with self.assertRaises(desktop.DesktopError):
                desktop.validate_client_key(root, metadata, private, public)


class DesktopLifecycleSafetyTests(unittest.TestCase):
    def test_fingerprint_uses_effective_overrides_not_cli_spelling(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.toml").write_text(
                'version = 1\n[presets.provider]\ntool = "codex"\n',
                encoding="utf-8",
            )
            setup = {"helper": "/installed/cage-desktop.py"}
            inherited = argparse.Namespace(
                config_dir=str(root),
                launcher="/installed/cage",
                net="",
                yolo=None,
                rebuild=False,
            )
            explicit = argparse.Namespace(
                config_dir=str(root),
                launcher="/installed/cage",
                net="gate",
                yolo=False,
                rebuild=True,
            )
            assignments = {
                "CAGE_NET_MODE": "gate",
                "CAGE_YOLO": "",
                "CAGE_CODEX_PROFILE": "",
                "CAGE_HOST_CODEX_PAYLOAD": '{"skills":[],"env_names":[]}',
            }
            with (
                patch.object(
                    desktop,
                    "resolved_preset_assignments",
                    return_value=assignments,
                ),
                patch.object(desktop, "current_version", return_value="0.26.0"),
            ):
                inherited_hash = desktop.config_fingerprint(
                    inherited, setup, root, "provider"
                )
                explicit_hash = desktop.config_fingerprint(
                    explicit, setup, root, "provider"
                )
            self.assertEqual(inherited_hash, explicit_hash)

    def test_fingerprint_changes_when_selected_provider_value_rotates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "config.toml").write_text(
                'version = 1\n[presets.provider]\ntool = "codex"\n',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config_dir=str(root),
                launcher="/installed/cage",
                net="gate",
                yolo=False,
            )
            setup = {"helper": "/installed/cage-desktop.py"}
            assignments = {
                "CAGE_NET_MODE": "gate",
                "CAGE_YOLO": "",
                "CAGE_CODEX_PROFILE": "",
                "CAGE_HOST_CODEX_PAYLOAD": (
                    '{"skills":[],"env_names":["PROVIDER_TEST_TOKEN"]}'
                ),
            }
            with (
                patch.object(
                    desktop,
                    "resolved_preset_assignments",
                    return_value=assignments,
                ),
                patch.object(desktop, "current_version", return_value="0.26.0"),
                patch.dict(os.environ, {"PROVIDER_TEST_TOKEN": "first"}, clear=True),
            ):
                first = desktop.config_fingerprint(args, setup, root, "provider")
            with (
                patch.object(
                    desktop,
                    "resolved_preset_assignments",
                    return_value=assignments,
                ),
                patch.object(desktop, "current_version", return_value="0.26.0"),
                patch.dict(os.environ, {"PROVIDER_TEST_TOKEN": "second"}, clear=True),
            ):
                second = desktop.config_fingerprint(args, setup, root, "provider")
            self.assertNotEqual(first, second)

    def test_stale_recovery_refuses_reused_container_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metadata = {
                "container_name": "cage-desktop-0123456789abcdef",
                "target_id": "0123456789abcdef",
            }
            inspected = subprocess.CompletedProcess(
                args=["docker", "inspect"],
                returncode=0,
                stdout="different-target\n",
                stderr="",
            )
            with patch.object(desktop.subprocess, "run", return_value=inspected) as run:
                with self.assertRaisesRegex(
                    desktop.DesktopError, "occupied by a non-matching container"
                ):
                    desktop.recover_stale_target(
                        {"docker": "/usr/bin/docker"}, root, metadata
                    )
            self.assertEqual(run.call_count, 1)

    def test_remove_requires_tty_or_explicit_yes(self):
        metadata = {"alias": "cage-test", "volume_name": "cage-volume"}
        with patch.object(desktop.sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(desktop.DesktopError, "requires a TTY"):
                desktop.confirm_remove(metadata, False)
        desktop.confirm_remove(metadata, True)


class RemoteLauncherTests(unittest.TestCase):
    def test_remote_launcher_injects_only_allowlisted_file_values(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env_path = root / "env.json"
            launch_path = root / "launch.json"
            env_path.write_text(
                json.dumps({"PROVIDER_API_KEY": "token", "HTTP_PROXY": "proxy"}),
                encoding="utf-8",
            )
            launch_path.write_text(
                json.dumps({"profile": "provider", "yolo": True}),
                encoding="utf-8",
            )
            captured = {}

            def fake_execve(path, argv, environment):
                captured.update(path=path, argv=argv, environment=dict(environment))
                raise RuntimeError("captured")

            with (
                patch.object(remote, "ENV_PATH", env_path),
                patch.object(remote, "LAUNCH_PATH", launch_path),
                patch.object(remote, "REAL_CODEX", "/real/codex"),
                patch.object(remote.os, "execve", side_effect=fake_execve),
                patch.object(remote.sys, "argv", ["codex", "app-server"]),
                patch.dict(os.environ, {"UNRELATED_SECRET": "no"}, clear=True),
            ):
                with self.assertRaisesRegex(RuntimeError, "captured"):
                    remote.launch()

            self.assertEqual(
                captured["argv"],
                ["/real/codex", "--profile", "provider", "--yolo", "app-server"],
            )
            self.assertEqual(
                captured["environment"]["PROVIDER_API_KEY"], "token"
            )
            self.assertNotIn("UNRELATED_SECRET", captured["environment"])

    def test_image_has_inetd_ssh_and_no_published_port(self):
        dockerfile = (ROOT / "Dockerfile.codex").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        launcher = (ROOT / "cage").read_text(encoding="utf-8")
        entrypoint = (ROOT / "entrypoint-codex.sh").read_text(encoding="utf-8")
        self.assertIn("openssh-server", dockerfile)
        self.assertIn("COPY codex-remote.py /usr/local/bin/codex", dockerfile)
        self.assertIn("!codex-remote.py", dockerignore)
        self.assertIn('"/usr/sbin/sshd",', (ROOT / "cage-desktop.py").read_text())
        self.assertNotIn("-p 127.0.0.1", launcher)
        self.assertIn("AllowTcpForwarding no", entrypoint)
        self.assertIn("PermitTunnel no", entrypoint)


if __name__ == "__main__":
    unittest.main()
