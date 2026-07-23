import curses
import fcntl
import importlib.util
import json
import os
import pty
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import time
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


class FakeScreen:
    def __init__(self, keys=(), height: int = 12, width: int = 60):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.writes: list[tuple[int, int, str, int | None]] = []
        self.moves: list[tuple[int, int]] = []

    def keypad(self, _enabled):
        pass

    def erase(self):
        pass

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, row, column, text, count, attribute=None):
        self.writes.append((row, column, str(text)[:count], attribute))

    def move(self, row, column):
        self.moves.append((row, column))

    def clrtoeol(self):
        pass

    def refresh(self):
        pass

    def _next_key(self):
        if not self.keys:
            raise AssertionError("fake screen ran out of keys")
        key = self.keys.pop(0)
        if isinstance(key, tuple):
            _, self.height, self.width, key = key
        return key

    def getch(self):
        return self._next_key()

    def get_wch(self):
        return self._next_key()


class StubController:
    def __init__(self):
        self.repo = Path("/tmp/example")
        self.tool_override = ""
        self.net_override = ""
        self.yolo_override = ""
        self.data = {
            "auth": {},
            "identities": {},
            "mcp_packs": {},
            "skill_packs": {},
            "host_commands": {},
            "presets": {},
            "defaults": {},
        }
        self.previews: list[list[dict]] = []
        self.commits: list[list[dict]] = []
        self.results: list[dict] = []

    def preview(self, operations):
        self.previews.append(operations)
        return {}

    def commit(self, operations):
        self.commits.append(operations)
        return {}

    def effective_preset(self):
        return "__cage_project_example", {"tool": "codex"}

    def write_result(self, value):
        self.results.append(value)

    def risks(self, _preset):
        return []

    def preflight(self, _preset):
        return []


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

    def test_prompt_escape_cancels_without_enter(self):
        view = cage_tui.CursesView(FakeScreen(["\x1b"]), StubController())

        self.assertIsNone(view.prompt("Environment", "Variable names", "TOKEN"))

    def test_prompt_can_erase_prefilled_value_and_submit_empty(self):
        view = cage_tui.CursesView(FakeScreen(["\x15", "\n"]), StubController())

        self.assertEqual(view.prompt("Environment", "Variable names", "TOKEN"), "")

    def test_prompt_supports_cursor_editing_unicode_delete_and_resize(self):
        keys = [
            ("resize", 10, 24, curses.KEY_RESIZE),
            curses.KEY_LEFT,
            curses.KEY_BACKSPACE,
            "é",
            curses.KEY_END,
            "Z",
            "\n",
        ]
        view = cage_tui.CursesView(FakeScreen(keys, height=8, width=20), StubController())

        self.assertEqual(view.prompt("Edit", "Value", "abc"), "aécZ")

    def test_prompt_horizontally_scrolls_long_prefill_and_honors_home_end(self):
        screen = FakeScreen(
            [curses.KEY_HOME, "X", curses.KEY_END, "\n"],
            height=12,
            width=20,
        )
        view = cage_tui.CursesView(screen, StubController())

        self.assertEqual(
            view.prompt("Edit", "Value", "abcdefghijklmnopqrstuvwxyz"),
            "Xabcdefghijklmnopqrstuvwxyz",
        )
        self.assertTrue(any(
            row == 10 and text.startswith("> klmnop")
            for row, _, text, _ in screen.writes
        ))
        self.assertTrue(all(column <= 18 for _, column in screen.moves))

    def test_confirmation_has_separate_input_row_and_accepts_case_insensitive_yes(self):
        screen = FakeScreen([" ", "Y", "E", "S", " ", "\n"], height=10, width=42)
        view = cage_tui.CursesView(screen, StubController())

        self.assertTrue(view.confirm("Review", ["Risk review", "• A long and important risk explanation"]))
        self.assertTrue(any(row == 8 and "YES" in text for row, _, text, _ in screen.writes))
        self.assertFalse(any(row == 8 and "risk" in text.lower() for row, _, text, _ in screen.writes))

    def test_confirmation_escape_cancels_without_enter(self):
        view = cage_tui.CursesView(FakeScreen(["\x1b"]), StubController())

        self.assertFalse(view.confirm("Review", ["Risk review", "• Host integration"]))

    def test_confirmation_wraps_and_page_scrolls_review_details(self):
        screen = FakeScreen(
            [curses.KEY_NPAGE, "Y", "e", "s", "\n"],
            height=8,
            width=24,
        )
        view = cage_tui.CursesView(screen, StubController())

        self.assertTrue(view.confirm("Review", [f"detail-{index}" for index in range(12)]))
        self.assertTrue(any(text == "detail-5" for _, _, text, _ in screen.writes))
        self.assertTrue(any(row == 6 and text.startswith("> Yes") for row, _, text, _ in screen.writes))

    def test_real_curses_escape_cancels_review_without_writing_result(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            before = config.read_bytes()
            master, slave = pty.openpty()

            def controlling_terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "cage-tui.py"),
                    "--backend", str(ROOT / "cage-config.py"),
                    "--config", str(config),
                    "--repo", str(root),
                    "--result", str(result),
                ],
                cwd=root,
                env={**os.environ, "TERM": "xterm-256color"},
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=controlling_terminal,
            )
            os.close(slave)

            def read_until(needle: bytes, timeout: float = 8) -> bytes:
                output = bytearray()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if not readable:
                        continue
                    try:
                        output.extend(os.read(master, 4096))
                    except OSError:
                        break
                    if needle in output:
                        return bytes(output)
                raise AssertionError(f"did not observe {needle!r}; output={bytes(output)!r}")

            try:
                read_until(b"Launch with this configuration")
                os.write(master, b"\r")
                read_until(b"Review before launch/save")
                os.write(master, b"\x1b")
                read_until(b"Launch with this configuration")
                self.assertIsNone(process.poll())
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(result.read_bytes(), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                os.close(master)

            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(result.read_bytes(), b"")

    def test_menu_starts_on_current_key_and_keeps_scrolled_selection_visible(self):
        options = [(f"item-{index}", f"Item {index}") for index in range(12)]
        screen = FakeScreen([10], height=6, width=30)
        view = cage_tui.CursesView(screen, StubController())

        self.assertEqual(view.menu("Long menu", options, initial_key="item-10"), "item-10")
        self.assertTrue(any(
            1 <= row <= 4 and text.strip() == "Item 10" and attribute == curses.A_REVERSE
            for row, _, text, attribute in screen.writes
        ))

    def test_menu_handles_resize_page_navigation_and_home_end(self):
        options = [(f"item-{index}", f"Item {index}") for index in range(20)]
        screen = FakeScreen(
            [
                ("resize", 6, 30, curses.KEY_RESIZE),
                curses.KEY_NPAGE,
                curses.KEY_PPAGE,
                curses.KEY_END,
                curses.KEY_HOME,
                curses.KEY_NPAGE,
                10,
            ],
            height=7,
            width=40,
        )
        view = cage_tui.CursesView(screen, StubController())

        self.assertEqual(view.menu("Long menu", options, initial_key="item-10"), "item-3")

    def test_select_value_starts_on_current_value(self):
        view = cage_tui.CursesView(FakeScreen([10]), StubController())

        self.assertEqual(view.select_value("Network", ["gate", "open", "off"], "gate"), "gate")

    def test_checkbox_cursor_stays_on_toggled_item(self):
        keys = [
            curses.KEY_DOWN, ord(" "),
            curses.KEY_DOWN, ord(" "),
            curses.KEY_END, 10,
        ]
        view = cage_tui.CursesView(FakeScreen(keys), StubController())

        self.assertEqual(view.choose_names("Packs", ["a", "b", "c"], []), ["b", "c"])

    def test_edit_preset_clears_environment_names_without_comma_workaround(self):
        controller = StubController()
        view = cage_tui.CursesView(FakeScreen(), controller)
        choices = iter(["env", "done"])
        view.menu = lambda *_args, **_kwargs: next(choices)
        view.prompt = lambda *_args, **_kwargs: ""

        edited = view.edit_preset({"tool": "codex", "env": ["OLD_TOKEN"]})

        self.assertNotIn("env", edited)

    def test_edit_preset_can_restore_inherited_session_sync(self):
        controller = StubController()
        view = cage_tui.CursesView(FakeScreen(), controller)
        choices = iter(["sync", "default", "done"])
        view.menu = lambda *_args, **_kwargs: next(choices)

        edited = view.edit_preset({"tool": "codex", "session_sync": False})

        self.assertNotIn("session_sync", edited)

    def test_launch_actions_highlights_remember_and_persists_yolo(self):
        controller = StubController()
        view = cage_tui.CursesView(FakeScreen(), controller)

        def choose(_title, _options, **kwargs):
            self.assertEqual(kwargs["initial_key"], "remember")
            return "remember"

        view.menu = choose
        view.risk_review = lambda *_args, **_kwargs: True

        self.assertTrue(view.launch_actions({"tool": "codex", "yolo": True}))
        self.assertTrue(controller.commits[0][0]["value"]["yolo"])
        self.assertEqual(controller.results, [{"action": "preset", "preset_name": "__cage_project_example"}])

    def test_named_save_overwrite_requires_explicit_review(self):
        controller = StubController()
        controller.data["presets"]["existing"] = {"tool": "codex"}
        view = cage_tui.CursesView(FakeScreen(), controller)
        view.menu = lambda *_args, **_kwargs: "save"
        view.prompt = lambda *_args, **_kwargs: "existing"
        captured = {}

        def reject(_preset, **kwargs):
            captured.update(kwargs)
            return False

        view.risk_review = reject

        self.assertFalse(view.launch_actions({"tool": "codex", "yolo": True}))
        self.assertIn("will be overwritten", captured["notices"][0])
        self.assertEqual(controller.commits, [])
        self.assertEqual(controller.results, [])

    def test_launch_once_writes_decision_without_changing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            result = root / "result.json"
            result.touch(mode=0o600)
            self.write_config(config)
            before = config.read_bytes()
            controller = cage_tui.Controller(ROOT / "cage-config.py", config, root, result)
            view = cage_tui.CursesView(FakeScreen(), controller)
            view.menu = lambda *_args, **_kwargs: "once"
            view.risk_review = lambda *_args, **_kwargs: True

            self.assertTrue(view.launch_actions({"tool": "codex", "yolo": True}))
            self.assertEqual(config.read_bytes(), before)
            decision = json.loads(result.read_text())
            self.assertEqual(decision["action"], "launch_once")
            self.assertTrue(decision["preset"]["yolo"])

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
                "value": {"tool": "codex", "auth": "codex", "net": "gate", "yolo": True},
            }])

            mapped = controller.data["projects"][str(root.resolve())]
            self.assertTrue(mapped.startswith("__cage_project_"))
            self.assertEqual(controller.data["presets"][mapped]["net"], "gate")
            self.assertTrue(controller.data["presets"][mapped]["yolo"])
            self.assertTrue(controller.snapshot["effective"]["yolo"])
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

    def test_cancelled_bare_launcher_stops_before_all_launch_side_effects(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            xdg = root / "xdg" / "cage"
            launcher = root / "launcher"
            bin_dir = root / "bin"
            for directory in (repo, home, xdg, launcher, bin_dir):
                directory.mkdir(parents=True)
            config = xdg / "config.toml"
            self.write_config(config)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'servers = [{ name = "company", type = "http", url = "https://example.test/mcp", bearer_token_env_var = "COMPANY_TOKEN" }]',
                    'servers = [{ name = "company", type = "http", url = "https://example.test/mcp", bearer_token_env_var = "COMPANY_TOKEN" }, { name = "local", type = "stdio", command = "fake-mcp" }]',
                ),
                encoding="utf-8",
            )
            before_config = config.read_bytes()
            codex_credentials = home / ".codex" / ".credentials.json"
            codex_credentials.parent.mkdir()
            codex_credentials.write_bytes(b'{"sentinel":"oauth"}\n')
            claude_session = home / ".claude" / "projects" / "-sentinel" / "session.jsonl"
            claude_session.parent.mkdir(parents=True)
            claude_session.write_bytes(b'{"sentinel":"session"}\n')
            shutil.copy2(ROOT / "cage", launcher / "cage")
            shutil.copy2(ROOT / "cage-config.py", launcher / "cage-config.py")
            executable(launcher / "cage-tui.py", "import sys\nsys.exit(1)\n")
            markers = {
                "docker": root / "docker-called",
                "mcp": root / "mcp-bridge-called",
                "host_command": root / "host-command-bridge-called",
                "netgate": root / "netgate-called",
            }
            executable(bin_dir / "docker", f"#!/bin/sh\ntouch {str(markers['docker'])!r}\nexit 0\n")
            for script, marker_name in (
                ("mcp-bridge.py", "mcp"),
                ("host-cmd-bridge.py", "host_command"),
                ("netgate-proxy.py", "netgate"),
            ):
                executable(
                    launcher / script,
                    f"from pathlib import Path\nPath({str(markers[marker_name])!r}).touch()\n",
                )
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
            self.assertFalse(any(marker.exists() for marker in markers.values()))
            self.assertEqual(config.read_bytes(), before_config)
            self.assertEqual(list(xdg.glob(".tui-result.*")), [])
            self.assertEqual(codex_credentials.read_bytes(), b'{"sentinel":"oauth"}\n')
            self.assertEqual(claude_session.read_bytes(), b'{"sentinel":"session"}\n')

if __name__ == "__main__":
    unittest.main()
