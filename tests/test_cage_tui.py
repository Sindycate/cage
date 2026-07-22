import fcntl
import importlib.util
import json
import os
import pty
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cage_tui", ROOT / "cage-tui.py")
cage_tui = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = cage_tui
SPEC.loader.exec_module(cage_tui)


def executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class CageTuiTests(unittest.TestCase):
    def write_config(self, path: Path) -> None:
        path.write_text(
            """version = 1
default_preset = "main"

[auth.codex]
tool = "codex"
copy_auth = false

[identities.work]
gh_auth = true

[mcp_packs.company]
env = ["COMPANY_TOKEN"]
servers = [{ name = "company", type = "http", url = "https://example.test/mcp", bearer_token_env_var = "COMPANY_TOKEN" }]

[host_commands.token]
command = "token"

[presets.main]
tool = "codex"
auth = "codex"
identity = "work"
mcp_packs = ["company"]
host_commands = ["token"]
net = "open"
yolo = true
extra_mounts = [{ path = "/tmp/output", mode = "rw" }]
""",
            encoding="utf-8",
        )

    def test_controller_exposes_effective_configuration_and_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)

            name, preset = controller.effective_preset()
            risks = controller.risks(preset)

            self.assertEqual(name, "main")
            self.assertTrue(controller.snapshot["effective"]["yolo"])
            self.assertTrue(any("permission prompts" in item for item in risks))
            self.assertTrue(any("unrestricted network" in item for item in risks))
            self.assertTrue(any("Read-write" in item for item in risks))
            self.assertTrue(any("Host commands" in item for item in risks))
            self.assertTrue(any("GitHub credentials" in item for item in risks))
            self.assertTrue(any("MCP integration" in item for item in risks))

    def test_controller_commit_remembers_hidden_project_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)

            controller.commit([{
                "action": "remember_project", "path": str(root),
                "value": {"tool": "codex", "auth": "codex", "net": "gate"},
            }])

            mapped = controller.data["projects"][str(root.resolve())]
            self.assertTrue(mapped.startswith("__cage_project_"))
            self.assertEqual(controller.data["presets"][mapped]["net"], "gate")
            self.assertEqual(len(list((root / "backups").glob("config-*.toml"))), 1)

    def test_cli_risk_overrides_are_included_in_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            controller = cage_tui.Controller(
                ROOT / "cage-config.py", config, root, result,
                net_override="open", yolo_override="on",
            )

            risks = controller.risks({"tool": "codex", "net": "gate", "yolo": False})

            self.assertTrue(any("permission prompts" in item for item in risks))
            self.assertTrue(any("unrestricted network" in item for item in risks))

    def test_controller_discovers_codex_oauth_actions_from_saved_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)
            server = controller.data["mcp_packs"]["company"]["servers"][0]
            server.pop("bearer_token_env_var")
            server["auth"] = "oauth"

            targets = controller.oauth_targets()

            self.assertEqual(targets, [("main", "company", "https://example.test/mcp")])

    def test_result_artifact_remains_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o666)
            self.write_config(config)
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)

            controller.write_result({"action": "preset", "preset_name": "main"})

            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(result.read_text())["preset_name"], "main")

    def test_result_artifact_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            target = root / "target.json"
            result = root / "result.json"
            target.write_text("preserve", encoding="utf-8")
            result.symlink_to(target)
            self.write_config(config)
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)

            with self.assertRaisesRegex(cage_tui.UiError, "regular file"):
                controller.write_result({"action": "preset", "preset_name": "main"})

            self.assertEqual(target.read_text(), "preserve")

    def test_config_save_does_not_touch_codex_or_claude_session_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            state_files = [
                root / "codex-volume" / "sessions" / "session.jsonl",
                root / "codex-volume" / "history.jsonl",
                root / "codex-volume" / "state_5.sqlite-wal",
                root / "claude-volume" / "projects" / "project" / "session.jsonl",
                root / "claude-host" / "projects" / "project" / "memory" / "MEMORY.md",
            ]
            for index, path in enumerate(state_files):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"state-{index}".encode())
            before = {path: path.read_bytes() for path in state_files}
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)

            controller.commit([{
                "action": "upsert", "collection": "presets", "name": "main",
                "value": {"tool": "codex", "auth": "codex", "net": "gate"},
            }])

            self.assertEqual({path: path.read_bytes() for path in state_files}, before)

    def test_cancelled_bare_launcher_never_invokes_docker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            xdg = root / "xdg" / "cage"
            launcher = root / "launcher"
            bin_dir = root / "bin"
            for directory in (repo, home, xdg, launcher, bin_dir):
                directory.mkdir(parents=True)
            self.write_config(xdg / "config.toml")
            shutil.copy2(ROOT / "cage", launcher / "cage")
            shutil.copy2(ROOT / "cage-config.py", launcher / "cage-config.py")
            executable(launcher / "cage-tui.py", "import sys\nsys.exit(1)\n")
            marker = root / "docker-called"
            executable(bin_dir / "docker", f"#!/bin/sh\ntouch {str(marker)!r}\nexit 0\n")
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "xdg"),
                "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            })
            master, slave = pty.openpty()

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            try:
                completed = subprocess.run(
                    [str(launcher / "cage")], cwd=repo, env=env,
                    stdin=slave, stdout=slave, stderr=slave,
                    preexec_fn=controlling_terminal, timeout=10,
                )
            finally:
                os.close(master)
                os.close(slave)

            self.assertEqual(completed.returncode, 1)
            self.assertFalse(marker.exists())
            self.assertEqual(list(xdg.glob(".tui-result.*")), [])

if __name__ == "__main__":
    unittest.main()
