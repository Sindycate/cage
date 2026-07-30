"""Authoritative MCP pack selection.

These tests cover the launch-time MCP inventory and the selected-only
suppression applied across Codex container/host/Desktop argument paths and
Claude reconciliation. The invariant under test: the effective enabled MCP set
equals the resolved preset's selected packs on every launch.
"""

import importlib.util
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

SPEC = importlib.util.spec_from_file_location("cage_config", ROOT / "cage-config.py")
cage_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = cage_config
SPEC.loader.exec_module(cage_config)

ConfigError = cage_config.ConfigError


def write_inventory_codex(bin_dir: Path, json_text: str, exit_code: int = 0) -> None:
    """Fake codex that answers `mcp list --json` with a fixed inventory."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "inventory.json").write_text(json_text, encoding="utf-8")
    script = (
        "#!/bin/sh\n"
        'cat "$(dirname "$0")/inventory.json"\n'
        f"exit {exit_code}\n"
    )
    (bin_dir / "codex").write_text(script, encoding="utf-8")
    (bin_dir / "codex").chmod(0o755)


@contextmanager
def path_front(bin_dir: Path):
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old}"
    try:
        yield
    finally:
        os.environ["PATH"] = old


def entry(name: str, enabled: bool = True) -> dict:
    return {"name": name, "enabled": enabled}


def make_data(codex_home: Path, packs: dict, selected: list, profile: str = "") -> dict:
    # The host-side inventory primitive is used for target = "host" launches;
    # container/Desktop inventory runs inside the launching runtime instead.
    preset = {"tool": "codex", "auth": "a", "target": "host", "net": "open", "mcp_packs": selected}
    if profile:
        preset["codex_profile"] = profile
    return {
        "version": 1,
        "default_preset": "p",
        "auth": {
            "a": {
                "tool": "codex",
                "host_codex_dir": str(codex_home),
                "copy_auth": False,
            }
        },
        "mcp_packs": packs,
        "presets": {"p": preset},
    }


HTTP_PACK = {
    "linear": {
        "servers": [
            {"name": "linear", "type": "http", "url": "https://mcp.linear.app/mcp"}
        ]
    }
}


class InventorySuppressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.codex_home = self.root / "codexhome"
        self.codex_home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.bin = self.root / "bin"

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, data, inventory=True):
        return cage_config.resolve_config(
            data, self.root / "config.toml", str(self.repo), mcp_inventory=inventory
        )

    def test_zero_selected_packs_suppresses_every_inherited_mcp(self):
        write_inventory_codex(
            self.bin, json.dumps([entry("node_repl"), entry("foo")])
        )
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(resolved.mcp_suppressed, ["foo", "node_repl"])
        self.assertEqual(
            resolved.mcp_disable_overrides,
            [
                "mcp_servers.foo.enabled=false",
                "mcp_servers.node_repl.enabled=false",
            ],
        )

    def test_selected_http_and_stdio_remain_enabled(self):
        write_inventory_codex(
            self.bin, json.dumps([entry("node_repl"), entry("user_extra")])
        )
        packs = {
            "linear": HTTP_PACK["linear"],
            "tools": {
                "servers": [
                    {"name": "mytool", "type": "stdio", "command": "printf ignored"}
                ]
            },
        }
        data = make_data(self.codex_home, packs, ["linear", "tools"])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(resolved.mcp_suppressed, ["node_repl", "user_extra"])
        self.assertNotIn("linear", resolved.mcp_suppressed)
        self.assertNotIn("mytool", resolved.mcp_suppressed)

    def test_profile_and_project_layers_are_captured(self):
        # `mcp list` only reports the user-level server; the profile and project
        # layers are merged in from direct TOML parsing.
        write_inventory_codex(self.bin, json.dumps([entry("user_level")]))
        (self.codex_home / "myprof.config.toml").write_text(
            '[mcp_servers.prof_only]\ncommand = "echo"\n', encoding="utf-8"
        )
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "config.toml").write_text(
            '[mcp_servers.proj_only]\ncommand = "echo"\n', encoding="utf-8"
        )
        data = make_data(self.codex_home, {}, [], profile="myprof")
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(
            resolved.mcp_suppressed, ["prof_only", "proj_only", "user_level"]
        )

    def test_disabled_entries_are_not_suppressed(self):
        write_inventory_codex(
            self.bin, json.dumps([entry("already_off", enabled=False)])
        )
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(resolved.mcp_suppressed, [])

    def test_unusual_quoted_names_are_quoted_in_overrides(self):
        write_inventory_codex(
            self.bin, json.dumps([entry("weird name"), entry("with.dot")])
        )
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertIn('mcp_servers."weird name".enabled=false', resolved.mcp_disable_overrides)
        self.assertIn('mcp_servers."with.dot".enabled=false', resolved.mcp_disable_overrides)

    def test_malformed_inventory_fails_closed(self):
        write_inventory_codex(self.bin, "this is not json")
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            with self.assertRaises(ConfigError):
                self.resolve(data)

    def test_semantically_malformed_inventory_fails_closed(self):
        data = make_data(self.codex_home, {}, [])
        malformed_entries = [
            [entry("valid"), "not-an-object"],
            [{"name": "missing-enabled"}],
            [{"name": "wrong-enabled-type", "enabled": "true"}],
            [{"name": "", "enabled": True}],
        ]
        for entries in malformed_entries:
            with self.subTest(entries=entries):
                write_inventory_codex(self.bin, json.dumps(entries))
                with path_front(self.bin):
                    with self.assertRaises(ConfigError):
                        self.resolve(data)

    def test_nonzero_inventory_exit_fails_closed(self):
        write_inventory_codex(self.bin, "[]", exit_code=1)
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            with self.assertRaises(ConfigError):
                self.resolve(data)

    def test_missing_codex_binary_fails_closed_when_codex_home_exists(self):
        empty_bin = self.root / "emptybin"
        empty_bin.mkdir()
        data = make_data(self.codex_home, {}, [])
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(empty_bin)
        try:
            with self.assertRaises(ConfigError):
                self.resolve(data)
        finally:
            os.environ["PATH"] = old_path

    def test_absent_codex_home_inventories_against_temporary_home(self):
        # A missing user CODEX_HOME does not mean no inherited MCPs: system and
        # plugin layers can still exist. Inventory runs against an empty temporary
        # home (capturing whatever the binary reports) instead of being skipped.
        write_inventory_codex(self.bin, json.dumps([entry("system_plugin")]))
        missing_home = self.root / "no-such-codex-home"
        data = make_data(missing_home, {}, [])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(resolved.mcp_suppressed, ["system_plugin"])

    def test_unreadable_project_layer_fails_closed(self):
        write_inventory_codex(self.bin, json.dumps([entry("node_repl")]))
        project_dir = self.repo / ".codex"
        project_dir.mkdir()
        # A directory where the config file should be is present-but-unreadable.
        (project_dir / "config.toml").mkdir()
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            with self.assertRaises(ConfigError):
                self.resolve(data)

    def test_repo_controlled_codex_is_rejected(self):
        # A codex binary inside the writable repository must never be executed.
        write_inventory_codex(self.repo, json.dumps([entry("evil")]))
        data = make_data(self.codex_home, {}, [])
        with path_front(self.repo):
            with self.assertRaises(ConfigError) as ctx:
                self.resolve(data)
        self.assertIn("Cage-writable", str(ctx.exception))

    def test_reported_launch_only_selected_active_node_repl_suppressed(self):
        write_inventory_codex(
            self.bin,
            json.dumps(
                [
                    entry("google-workspace"),
                    entry("linear"),
                    entry("sunrise"),
                    entry("node_repl"),
                ]
            ),
        )
        packs = {
            "gw": {"servers": [{"name": "google-workspace", "type": "http", "url": "https://gw.example/mcp"}]},
            "linear": {"servers": [{"name": "linear", "type": "http", "url": "https://mcp.linear.app/mcp"}]},
            "sunrise": {"servers": [{"name": "sunrise", "type": "http", "url": "https://sunrise.example/mcp"}]},
        }
        data = make_data(self.codex_home, packs, ["gw", "linear", "sunrise"])
        with path_front(self.bin):
            resolved = self.resolve(data)
        self.assertEqual(resolved.mcp_suppressed, ["node_repl"])

    def test_inventory_never_mutates_host_or_project_config(self):
        write_inventory_codex(self.bin, json.dumps([entry("node_repl")]))
        host_config = self.codex_home / "config.toml"
        host_config.write_text('[mcp_servers.node_repl]\ncommand = "echo"\n', encoding="utf-8")
        (self.repo / ".codex").mkdir()
        project_config = self.repo / ".codex" / "config.toml"
        project_config.write_text('[mcp_servers.proj]\ncommand = "echo"\n', encoding="utf-8")
        before_host = host_config.read_bytes()
        before_project = project_config.read_bytes()
        data = make_data(self.codex_home, {}, [])
        with path_front(self.bin):
            self.resolve(data)
        self.assertEqual(host_config.read_bytes(), before_host)
        self.assertEqual(project_config.read_bytes(), before_project)


class HostAndContainerArgPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.codex_home = self.root / "codexhome"
        self.codex_home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_host_arg_lines_append_disable_overrides_last(self):
        payload = {
            "profile": "",
            "stdio": [],
            "remote": [{"name": "linear", "type": "http", "url": "https://mcp.linear.app/mcp"}],
            "skills": [],
            "env_names": [],
            "disable_mcp": ["node_repl", "weird name"],
            "disable_mcp_overrides": [
                "mcp_servers.node_repl.enabled=false",
                'mcp_servers."weird name".enabled=false',
            ],
        }
        args = cage_config.host_codex_arg_lines(payload, self.repo, self.codex_home)
        # The disable overrides must be present and ordered last (highest precedence).
        self.assertEqual(args[-4:], [
            "-c", "mcp_servers.node_repl.enabled=false",
            "-c", 'mcp_servers."weird name".enabled=false',
        ])

    def test_validate_codex_layers_rejects_unsafe_disable_names(self):
        payload = {"profile": "", "stdio": [], "remote": [], "disable_mcp": ["bad\nname"]}
        with self.assertRaises(ConfigError):
            cage_config.validate_codex_layers(payload, self.repo, self.codex_home)

    def test_duplicate_selected_name_in_layer_fails_closed(self):
        (self.codex_home / "config.toml").write_text(
            '[mcp_servers.linear]\ncommand = "echo"\n', encoding="utf-8"
        )
        payload = {
            "profile": "",
            "stdio": [],
            "remote": [{"name": "linear", "type": "http", "url": "https://mcp.linear.app/mcp"}],
            "disable_mcp": [],
        }
        with self.assertRaises(ConfigError):
            cage_config.validate_codex_layers(payload, self.repo, self.codex_home)

    def test_inventory_shaping_passthrough_arguments_are_rejected(self):
        attempts = [
            ["-c", "mcp_servers.extra.enabled=true"],
            ["--config=mcp_servers.extra.command=\"echo\""],
            ["-c=mcp_servers.extra.url=\"https://example.test/mcp\""],
            ["-cmcp_servers.extra.command=\"echo\""],
            ["--config", '"mcp\\u005fservers".extra.enabled=true'],
            ["--config", "'mcp_servers'.extra.enabled=true"],
            ["-p", "evil"],
            ["--profile", "evil"],
            ["--profile=evil"],
            ["-p=evil"],
            ["-pevil"],
            ["-C", "/other"],
            ["--cd", "/other"],
            ["--cd=/other"],
            ["-C=/other"],
            ["-C/other"],
            ["--enable", "plugins"],
            ["--enable=plugins"],
            ["--disable", "plugins"],
            ["--disable=plugins"],
            ["--remote", "ws://other.example"],
            ["--remote=ws://other.example"],
            ["--remote-auth-token-env", "TOKEN"],
            ["--remote-auth-token-env=TOKEN"],
            ["exec", "--ignore-user-config"],
            ["-c", "features.plugins=true"],
            ["--config", 'projects."/repo".trust_level="trusted"'],
            ["-c", "plugins.example.enabled=true"],
            ["-c", "future_inventory_layer=true"],
            ["-c", "not an assignment"],
        ]
        for argv in attempts:
            with self.subTest(argv=argv), self.assertRaises(ConfigError):
                cage_config.reject_unsafe_codex_passthrough_args(argv)
        cage_config.reject_unsafe_codex_passthrough_args(
            [
                "prompt text",
                "--model",
                "gpt-test",
                "--sandbox",
                "read-only",
                "-c",
                'model="gpt-test"',
                "--config=sandbox_mode=\"read-only\"",
            ]
        )
        cage_config.reject_unsafe_codex_passthrough_args(
            ["exec", "--", "-p", "--profile=evil", "-C/other", "--enable=plugins"]
        )

    def test_direct_only_suppression_uses_transport_complete_inert_stubs(self):
        overrides = cage_config.mcp_disable_plan(
            ["project_http", "project_stdio"],
            set(),
            {"project_http": "http", "project_stdio": "stdio"},
        )
        self.assertEqual(
            overrides,
            [
                'mcp_servers.project_http.url="https://invalid.invalid/mcp"',
                "mcp_servers.project_http.enabled=false",
                'mcp_servers.project_stdio.command="/usr/bin/false"',
                "mcp_servers.project_stdio.enabled=false",
            ],
        )


@unittest.skipUnless(shutil.which("codex"), "real codex binary required")
class RealCodexSelectionTests(unittest.TestCase):
    """Assert the effective enabled set equals the selected packs, using the
    real codex binary's `mcp list --json`."""

    def test_enabled_names_equal_selected_pack_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codexhome"
            codex_home.mkdir()
            repo = root / "repo"
            repo.mkdir()
            # Inherited user-level MCPs that must be suppressed.
            (codex_home / "config.toml").write_text(
                '[mcp_servers.node_repl]\ncommand = "echo"\n'
                '[mcp_servers.user_extra]\ncommand = "echo"\n',
                encoding="utf-8",
            )
            packs = {
                "linear": {
                    "servers": [
                        {"name": "linear", "type": "http", "url": "https://mcp.linear.app/mcp"}
                    ]
                }
            }
            data = make_data(codex_home, packs, ["linear"])
            resolved = cage_config.resolve_config(
                data, root / "config.toml", str(repo), mcp_inventory=True
            )
            self.assertEqual(resolved.mcp_suppressed, ["node_repl", "user_extra"])

            # Build the full process-local launch args (selected + suppression).
            payload = cage_config.host_codex_payload_for(resolved)
            args = cage_config.host_codex_arg_lines(payload, repo, codex_home)

            env = dict(os.environ)
            env["CODEX_HOME"] = str(codex_home)
            completed = subprocess.run(
                [shutil.which("codex"), *args, "mcp", "list", "--json"],
                cwd=str(repo),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0)
            entries = json.loads(completed.stdout.decode("utf-8"))
            enabled = sorted(e["name"] for e in entries if e.get("enabled") is True)
            self.assertEqual(enabled, ["linear"])

    def test_untrusted_project_transport_is_disabled_without_invalid_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codexhome"
            codex_home.mkdir()
            repo = root / "repo"
            (repo / ".codex").mkdir(parents=True)
            (repo / ".codex" / "config.toml").write_text(
                '[mcp_servers.project_server]\ncommand = "echo"\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["CAGE_INV_CODEX_HOME"] = str(codex_home)
            env["CAGE_INV_WORK_DIR"] = str(repo)
            env["CAGE_INV_PROFILE"] = ""
            env["CODEX_HOME"] = str(codex_home)
            inventory = subprocess.run(
                [sys.executable, "-I", "-c", entrypoint_inventory_block()],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            self.assertEqual(inventory.returncode, 0, inventory.stderr)
            overrides = [
                line for line in inventory.stdout.splitlines() if line.strip()
            ]
            self.assertEqual(
                overrides,
                [
                    'mcp_servers.project_server.command="/usr/bin/false"',
                    "mcp_servers.project_server.enabled=false",
                ],
            )

            args = []
            for override in overrides:
                args += ["-c", override]
            untrusted = subprocess.run(
                [shutil.which("codex"), *args, "mcp", "list", "--json"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            self.assertEqual(untrusted.returncode, 0, untrusted.stderr)

            (codex_home / "config.toml").write_text(
                f'[projects.{json.dumps(str(repo))}]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            trusted = subprocess.run(
                [shutil.which("codex"), *args, "mcp", "list", "--json"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            self.assertEqual(trusted.returncode, 0, trusted.stderr)
            entries = json.loads(trusted.stdout)
            project = next(
                item for item in entries if item["name"] == "project_server"
            )
            self.assertFalse(project["enabled"])
            self.assertEqual(project["transport"]["command"], "/usr/bin/false")



def entrypoint_inventory_block() -> str:
    """Execute the same packaged policy helper used by the entrypoint."""
    policy = ROOT / "cage_core" / "codex_runtime.py"
    return (
        "import os,runpy,sys;"
        f"p={str(policy)!r};"
        "sys.argv=[p,'runtime-overrides','--codex-bin','codex',"
        "'--codex-home',os.environ['CAGE_INV_CODEX_HOME'],"
        "'--repo',os.environ['CAGE_INV_WORK_DIR'],"
        "'--profile',os.environ.get('CAGE_INV_PROFILE',''),"
        "'--selected-stdio-json',os.environ.get('CAGE_MCP_SERVERS',''),"
        "'--selected-remote-json',os.environ.get('CAGE_REMOTE_MCP_SERVERS','')];"
        "runpy.run_path(p,run_name='__main__')"
    )


def entrypoint_arg_guard_block() -> str:
    policy = ROOT / "cage_core" / "codex_runtime.py"
    return (
        "import runpy,sys;"
        f"p={str(policy)!r};"
        "sys.argv=[p,'validate-argv','--',*sys.argv[1:]];"
        "runpy.run_path(p,run_name='__main__')"
    )


class EntrypointContainerInventoryTests(unittest.TestCase):
    """The container entrypoint inventories the image runtime and suppresses
    every inherited server the preset did not select."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.codex_home = self.root / "codexhome"
        self.codex_home.mkdir()
        self.work_dir = self.root / "repo"
        self.work_dir.mkdir()
        self.bin = self.root / "bin"

    def tearDown(self):
        self._tmp.cleanup()

    def run_inventory(self, inventory_json, bridged=None, remote=None, profile=""):
        write_inventory_codex(self.bin, inventory_json)
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["CAGE_INV_CODEX_HOME"] = str(self.codex_home)
        env["CAGE_INV_WORK_DIR"] = str(self.work_dir)
        env["CAGE_INV_PROFILE"] = profile
        env.pop("CAGE_MCP_SERVERS", None)
        env.pop("CAGE_REMOTE_MCP_SERVERS", None)
        if bridged is not None:
            env["CAGE_MCP_SERVERS"] = bridged
        if remote is not None:
            env["CAGE_REMOTE_MCP_SERVERS"] = remote
        return subprocess.run(
            [sys.executable, "-I", "-c", entrypoint_inventory_block()],
            env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_inherited_server_suppressed_selected_retained(self):
        result = self.run_inventory(
            json.dumps([entry("node_repl"), entry("linear")]),
            remote=json.dumps([{"name": "linear", "url": "https://mcp.linear.app/mcp"}]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mcp_servers.node_repl.enabled=false", result.stdout)
        self.assertNotIn("linear", result.stdout)
        self.assertIn("MCP policy: selected packs only", result.stderr)
        self.assertIn("node_repl", result.stderr)

    def test_bridged_selected_server_retained(self):
        result = self.run_inventory(
            json.dumps([entry("mytool"), entry("ghost")]),
            bridged=json.dumps({"mytool": 41000}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mcp_servers.ghost.enabled=false", result.stdout)
        self.assertNotIn("mytool", result.stdout)

    def test_project_layer_captured_and_suppressed(self):
        (self.work_dir / ".codex").mkdir()
        (self.work_dir / ".codex" / "config.toml").write_text(
            '[mcp_servers.proj_only]\ncommand = "echo"\n', encoding="utf-8"
        )
        result = self.run_inventory(json.dumps([entry("node_repl")]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'mcp_servers.proj_only.command="/usr/bin/false"',
            result.stdout,
        )
        self.assertIn("mcp_servers.proj_only.enabled=false", result.stdout)
        self.assertIn("mcp_servers.node_repl.enabled=false", result.stdout)

    def test_unreadable_project_layer_fails_closed(self):
        (self.work_dir / ".codex").mkdir()
        (self.work_dir / ".codex" / "config.toml").mkdir()  # dir, not file
        result = self.run_inventory(json.dumps([entry("node_repl")]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreadable project layer", result.stderr)

    def test_selected_name_duplicated_in_untrusted_project_fails_closed(self):
        (self.work_dir / ".codex").mkdir()
        (self.work_dir / ".codex" / "config.toml").write_text(
            '[mcp_servers.linear]\nurl = "https://project.example/mcp"\n',
            encoding="utf-8",
        )
        result = self.run_inventory(
            json.dumps([entry("linear")]),
            remote=json.dumps(
                [{"name": "linear", "url": "https://central.example/mcp"}]
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exist in a profile/project layer", result.stderr)

    def test_inventory_failure_fails_closed(self):
        result = self.run_inventory("not json")
        self.assertNotEqual(result.returncode, 0)

    def test_semantically_malformed_inventory_fails_closed(self):
        for entries in (
            [entry("valid"), "not-an-object"],
            [{"name": "missing-enabled"}],
            [{"name": "wrong-enabled-type", "enabled": "true"}],
        ):
            with self.subTest(entries=entries):
                result = self.run_inventory(json.dumps(entries))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trustworthy MCP inventory", result.stderr)

    def test_entrypoint_rejects_inventory_shaping_arguments_after_policy(self):
        attempts = [
            ["--config=mcp_servers.injected.command=\"echo\""],
            ["--profile=evil"],
            ["-pevil"],
            ["--cd=/other"],
            ["-C/other"],
            ["--enable=plugins"],
            ["--disable", "plugins"],
            ["--remote=ws://other.example"],
            ["--remote-auth-token-env", "TOKEN"],
            ["exec", "--ignore-user-config"],
            ["-c", "features.plugins=true"],
            ["--config=plugins.example.enabled=true"],
        ]
        for argv in attempts:
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        entrypoint_arg_guard_block(),
                        *argv,
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_entrypoint_allows_safe_arguments_and_delimited_payload(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                entrypoint_arg_guard_block(),
                "--model",
                "gpt-test",
                "-c",
                'sandbox_mode="read-only"',
                "--",
                "-p",
                "--profile=prompt-text",
                "-C/also-prompt-text",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entrypoint_injects_only_cage_owned_profile_after_guard(self):
        entrypoint = (ROOT / "entrypoint-codex.sh").read_text(encoding="utf-8")
        self.assertIn(
            'CAGE_CODEX_PROFILE_ARGS+=(--profile "$CAGE_CODEX_PROFILE")',
            entrypoint,
        )
        final_exec = entrypoint.rsplit("exec gosu", 1)[1]
        self.assertLess(
            final_exec.index("CAGE_CODEX_PROFILE_ARGS"),
            final_exec.index("CAGE_MCP_DISABLE_ARGS"),
        )
        self.assertLess(
            final_exec.index("CAGE_MCP_DISABLE_ARGS"),
            final_exec.index('"$@"'),
        )


class CodexRemoteInventoryTests(unittest.TestCase):
    """Desktop re-inventories the live runtime on every connection."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("codex_remote", ROOT / "codex-remote.py")
        cls.mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.mod)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self._tmp.name)
        self.codex_home = self.root / "codexhome"
        self.codex_home.mkdir()
        self.work_dir = self.root / "repo"
        self.work_dir.mkdir()
        self.bin = self.root / "bin"
        self._real_codex = self.mod.REAL_CODEX
        self._codex_home_const = self.mod.CODEX_HOME
        self.mod.CODEX_HOME = str(self.codex_home)

    def tearDown(self):
        self.mod.REAL_CODEX = self._real_codex
        self.mod.CODEX_HOME = self._codex_home_const
        self._tmp.cleanup()

    def inventory(self, inventory_json, profile=""):
        write_inventory_codex(self.bin, inventory_json)
        self.mod.REAL_CODEX = str(self.bin / "codex")
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
        }
        return self.mod.inventory_enabled(profile, str(self.work_dir), env)

    def test_every_runtime_delegates_to_the_shared_passthrough_policy(self):
        self.assertIs(
            self.mod.codex_policy,
            cage_config.codex_policy,
        )
        entrypoint = (ROOT / "entrypoint-codex.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/usr/local/lib/cage/cage_core/codex_runtime.py",
            entrypoint,
        )
        accepted = ["--model", "gpt-test", "-c", 'sandbox_mode="read-only"']
        rejected = ["--profile=evil"]
        cage_config.reject_unsafe_codex_passthrough_args(accepted)
        self.mod.reject_unsafe_codex_passthrough_args(accepted)
        with self.assertRaises(cage_config.ConfigError):
            cage_config.reject_unsafe_codex_passthrough_args(rejected)
        with self.assertRaises(RuntimeError):
            self.mod.reject_unsafe_codex_passthrough_args(rejected)

    def test_inherited_server_present_for_suppression(self):
        enabled, runtime_enabled, direct = self.inventory(
            json.dumps([entry("node_repl"), entry("linear")])
        )
        self.assertEqual(enabled, {"node_repl", "linear"})
        self.assertEqual(runtime_enabled, enabled)
        self.assertEqual(direct, {})

    def test_live_project_mcp_discovered_per_connection(self):
        # A project MCP added after supervisor start is still discovered because
        # the inventory runs on every connection.
        (self.work_dir / ".codex").mkdir()
        (self.work_dir / ".codex" / "config.toml").write_text(
            '[mcp_servers.late_addition]\ncommand = "echo"\n', encoding="utf-8"
        )
        enabled, runtime_enabled, direct = self.inventory(
            json.dumps([entry("node_repl")])
        )
        self.assertIn("late_addition", enabled)
        self.assertNotIn("late_addition", runtime_enabled)
        self.assertEqual(direct["late_addition"], "stdio")

    def test_disable_override_quoting(self):
        self.assertEqual(self.mod.disable_override("node_repl"), "mcp_servers.node_repl.enabled=false")
        self.assertEqual(self.mod.disable_override("weird name"), 'mcp_servers."weird name".enabled=false')
        with self.assertRaises(RuntimeError):
            self.mod.disable_override("bad\nname")

    def test_inventory_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.inventory("not json")

    def test_semantically_malformed_inventory_raises(self):
        for entries in (
            [entry("valid"), "not-an-object"],
            [{"name": "missing-enabled"}],
            [{"name": "wrong-enabled-type", "enabled": "true"}],
        ):
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                self.inventory(json.dumps(entries))


if __name__ == "__main__":
    unittest.main()
