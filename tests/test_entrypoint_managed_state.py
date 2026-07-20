import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_CLAUDE = ROOT / "entrypoint.sh"
ENTRYPOINT_CODEX = ROOT / "entrypoint-codex.sh"


def extract_python(path, invocation):
    source = path.read_text()
    match = re.search(
        re.escape(invocation) + r"\n(.*?)\nPY",
        source,
        re.S,
    )
    if not match:
        raise AssertionError("embedded Python block not found in %s" % path)
    return match.group(1)


def clean_env():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("CAGE_") or key.startswith("MCP_BRIDGE_"):
            env.pop(key)
    return env


class EntrypointManagedStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.claude_python = extract_python(
            ENTRYPOINT_CLAUDE,
            'CLAUDE_PREFS_STORE="$PREFS_STORE" '
            'CLAUDE_MCP_MANIFEST="$MCP_MANIFEST" python3 -I - <<\'PY\'',
        )
        cls.codex_python = extract_python(
            ENTRYPOINT_CODEX,
            'CODEX_CONFIG_PATH="$CODEX_DIR/config.toml" python3 -I - <<\'PY\'',
        )

        source = ENTRYPOINT_CODEX.read_text()
        match = re.search(r"(reconcile_codex_auth\(\) \{\n.*?\n\})", source, re.S)
        if not match:
            raise AssertionError("reconcile_codex_auth function not found")
        cls.auth_function = match.group(1)

        match = re.search(
            r"(copy_host_codex_entry\(\) \{\n.*?\n\}\n\n"
            r"copy_host_codex_directory\(\) \{\n.*?\n\}\n\n"
            r"import_host_codex_state\(\) \{\n.*?\n\})",
            source,
            re.S,
        )
        if not match:
            raise AssertionError("Codex host-state import functions not found")
        cls.host_import_functions = match.group(1)

    def run_python(self, code, env):
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_claude_replaces_managed_mcp_state_and_preserves_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = root / ".claude.json"
            manifest = root / ".cage-managed-mcp.json"
            host_config = root / "host-claude.json"
            prefs.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "mcpServers": {
                            "manual": {"command": "manual-server"},
                            "stale": {
                                "type": "http",
                                "headers": {"Authorization": "Bearer old-token"},
                            },
                        },
                    }
                )
            )
            manifest.write_text(
                json.dumps({"version": 1, "mcp_server_names": ["stale"]})
            )
            host_config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "host": {
                                "type": "http",
                                "url": "https://host.example/mcp",
                                "headers": {"Authorization": "Bearer ${HOST_TOKEN}"},
                            }
                        }
                    }
                )
            )

            env = clean_env()
            env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(host_config),
                    "HOST_TOKEN": "host-current",
                    "REMOTE_TOKEN": "remote-current",
                    "CAGE_REMOTE_MCP_SERVERS": json.dumps(
                        [
                            {
                                "name": "remote",
                                "url": "https://remote.example/mcp",
                                "bearer_token_env_var": "REMOTE_TOKEN",
                            }
                        ]
                    ),
                    "CAGE_MCP_SERVERS": json.dumps({"bridge": 12345}),
                }
            )
            first = self.run_python(self.claude_python, env)
            self.assertEqual(first.returncode, 0, first.stderr)

            current = json.loads(prefs.read_text())
            servers = current["mcpServers"]
            self.assertEqual(current["theme"], "dark")
            self.assertIn("manual", servers)
            self.assertNotIn("stale", servers)
            self.assertEqual(
                servers["host"]["headers"]["Authorization"],
                "Bearer host-current",
            )
            self.assertEqual(
                servers["remote"]["headers"]["Authorization"],
                "Bearer remote-current",
            )
            self.assertEqual(servers["bridge"]["command"], "mcp-relay")
            self.assertEqual(
                set(json.loads(manifest.read_text())["mcp_server_names"]),
                {"host", "remote", "bridge"},
            )
            self.assertEqual(stat.S_IMODE(prefs.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

            # A later preset with no MCP sources removes only Cage-owned state.
            second_env = clean_env()
            second_env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                }
            )
            second = self.run_python(self.claude_python, second_env)
            self.assertEqual(second.returncode, 0, second.stderr)
            current = json.loads(prefs.read_text())
            self.assertEqual(current["theme"], "dark")
            self.assertEqual(
                current["mcpServers"],
                {"manual": {"command": "manual-server"}},
            )
            self.assertEqual(
                json.loads(manifest.read_text())["mcp_server_names"],
                [],
            )

    def test_claude_managed_state_refuses_persistent_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / "workspace-victim.json"
            victim.write_text('{"must":"remain"}\n', encoding="utf-8")
            prefs = root / ".claude.json"
            prefs.symlink_to(victim)
            manifest = root / ".cage-managed-mcp.json"
            env = clean_env()
            env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                }
            )

            result = self.run_python(self.claude_python, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing non-regular or symlinked JSON state", result.stderr)
            self.assertTrue(prefs.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), '{"must":"remain"}\n')

    def test_entrypoint_instructions_use_atomic_non_following_replacement(self):
        claude = ENTRYPOINT_CLAUDE.read_text(encoding="utf-8")
        codex = ENTRYPOINT_CODEX.read_text(encoding="utf-8")

        self.assertIn('mv -fT -- "$_claude_instructions_tmp" "$CLAUDE_DIR/CLAUDE.md"', claude)
        self.assertIn('mv -fT -- "$_codex_instructions_tmp" "$CODEX_DIR/instructions.md"', codex)
        self.assertNotIn('cat > "$CLAUDE_DIR/CLAUDE.md"', claude)
        self.assertNotIn('cat > "$CODEX_DIR/instructions.md"', codex)
        self.assertIn('rm -rf -- "$destination"', codex)

    def test_claude_unset_token_does_not_retain_previous_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = root / ".claude.json"
            manifest = root / ".cage-managed-mcp.json"
            remote = json.dumps(
                [
                    {
                        "name": "token-server",
                        "url": "https://token.example/mcp",
                        "bearer_token_env_var": "ROTATING_TOKEN",
                    }
                ]
            )
            base_env = clean_env()
            base_env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                    "CAGE_REMOTE_MCP_SERVERS": remote,
                    "ROTATING_TOKEN": "first-token",
                }
            )
            first = self.run_python(self.claude_python, base_env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("first-token", prefs.read_text())

            base_env.pop("ROTATING_TOKEN")
            second = self.run_python(self.claude_python, base_env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("first-token", prefs.read_text())
            self.assertNotIn(
                "token-server",
                json.loads(prefs.read_text()).get("mcpServers", {}),
            )
            self.assertIn("unset env var", second.stderr)

    def test_claude_restores_user_server_shadowed_by_managed_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = root / ".claude.json"
            manifest = root / ".cage-managed-mcp.json"
            prefs.write_text(
                json.dumps({"mcpServers": {"shared": {"command": "user-server"}}})
            )
            active_env = clean_env()
            active_env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                    "CAGE_REMOTE_MCP_SERVERS": json.dumps(
                        [{"name": "shared", "url": "https://managed.example/mcp"}]
                    ),
                }
            )

            active = self.run_python(self.claude_python, active_env)
            self.assertEqual(active.returncode, 0, active.stderr)
            self.assertEqual(
                json.loads(prefs.read_text())["mcpServers"]["shared"]["url"],
                "https://managed.example/mcp",
            )

            inactive_env = clean_env()
            inactive_env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                }
            )
            inactive = self.run_python(self.claude_python, inactive_env)
            self.assertEqual(inactive.returncode, 0, inactive.stderr)
            self.assertEqual(
                json.loads(prefs.read_text())["mcpServers"]["shared"],
                {"command": "user-server"},
            )

    def test_claude_does_not_overwrite_malformed_persistent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = root / ".claude.json"
            manifest = root / ".cage-managed-mcp.json"
            malformed = "{not-json\n"
            prefs.write_text(malformed)
            env = clean_env()
            env.update(
                {
                    "CLAUDE_PREFS_STORE": str(prefs),
                    "CLAUDE_MCP_MANIFEST": str(manifest),
                    "CLAUDE_HOST_CONFIG_PATH": str(root / "missing-host.json"),
                }
            )

            result = self.run_python(self.claude_python, env)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid Claude preferences", result.stderr)
            self.assertEqual(prefs.read_text(), malformed)

    def test_codex_managed_block_is_idempotent_and_removed_when_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                'model = "gpt-test"\n\n'
                '[mcp_servers.manual]\n'
                'url = "https://manual.example/mcp"\n'
            )
            env = clean_env()
            env.update(
                {
                    "CODEX_CONFIG_PATH": str(config),
                    "CAGE_MCP_SERVERS": json.dumps({"bridge": 45678}),
                    "MCP_BRIDGE_HOST": "host.docker.internal",
                    "MCP_BRIDGE_PORT_BRIDGE": "45678",
                    "MCP_BRIDGE_TOKEN": "b" * 64,
                }
            )

            for _ in range(2):
                result = self.run_python(self.codex_python, env)
                self.assertEqual(result.returncode, 0, result.stderr)

            generated = config.read_text()
            self.assertEqual(generated.count("# BEGIN CAGE MANAGED MCP SERVERS"), 1)
            self.assertEqual(generated.count('[mcp_servers."bridge"]'), 1)
            self.assertIn("[mcp_servers.manual]", generated)
            self.assertEqual(
                set(tomllib.loads(generated)["mcp_servers"]),
                {"manual", "bridge"},
            )
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

            inactive_env = clean_env()
            inactive_env["CODEX_CONFIG_PATH"] = str(config)
            result = self.run_python(self.codex_python, inactive_env)
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = config.read_text()
            self.assertNotIn("CAGE MANAGED MCP SERVERS", generated)
            self.assertNotIn('[mcp_servers."bridge"]', generated)
            self.assertIn("[mcp_servers.manual]", generated)

    def test_codex_auth_is_reconciled_instead_of_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / "host"
            codex = root / "codex"
            host.mkdir()
            codex.mkdir()

            def reconcile(copy_auth):
                script = (
                    self.auth_function
                    + '\nreconcile_codex_auth "$1" "$2" "$3"\n'
                )
                return subprocess.run(
                    ["bash", "-c", script, "bash", str(host), str(codex), copy_auth],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            (codex / "auth.json").write_text('{"token":"stale"}')
            result = reconcile("0")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((codex / "auth.json").exists())

            (host / "auth.json").write_text('{"token":"current"}')
            result = reconcile("1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((codex / "auth.json").read_text(), '{"token":"current"}')
            self.assertEqual(stat.S_IMODE((codex / "auth.json").stat().st_mode), 0o600)

            (host / "auth.json").unlink()
            result = reconcile("1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((codex / "auth.json").exists())

    def test_codex_host_import_preserves_volume_owned_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / "host"
            codex = root / "codex"
            host.mkdir()
            codex.mkdir()

            (host / "config.toml").write_text('model = "host"\n', encoding="utf-8")
            (host / ".credentials.json").write_text(
                '{"oauth":"host"}\n',
                encoding="utf-8",
            )
            (host / "work.config.toml").write_text(
                'model = "profile"\n',
                encoding="utf-8",
            )
            (host / "AGENTS.md").write_text("host guidance\n", encoding="utf-8")

            (codex / "config.toml").write_text('model = "volume"\n', encoding="utf-8")
            (codex / ".credentials.json").write_text(
                '{"oauth":"volume"}\n',
                encoding="utf-8",
            )

            runtime_files = (
                "history.jsonl",
                "session_index.jsonl",
                "state_5.sqlite",
                "state_5.sqlite-wal",
                "logs_2.sqlite",
                "memories_1.sqlite",
                "goals_1.sqlite",
            )
            runtime_directories = (
                "sessions",
                "archived_sessions",
                "log",
                "memories",
                "cache",
                "shell_snapshots",
            )
            for name in runtime_files:
                (host / name).write_bytes(("host:" + name).encode())
                (codex / name).write_bytes(("volume:" + name).encode())
            for name in runtime_directories:
                (host / name).mkdir()
                (host / name / "host-only.bin").write_bytes(b"host")
                (codex / name).mkdir()
                (codex / name / "volume.bin").write_bytes(
                    ("volume:" + name).encode()
                )

            script = (
                self.host_import_functions
                + '\nCODEX_DIR="$1"\nimport_host_codex_state "$2"\n'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(codex), str(host)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((codex / "config.toml").read_text(), 'model = "host"\n')
            self.assertEqual(
                (codex / ".credentials.json").read_text(),
                '{"oauth":"host"}\n',
            )
            self.assertEqual(
                (codex / "work.config.toml").read_text(),
                'model = "profile"\n',
            )
            self.assertEqual((codex / "AGENTS.md").read_text(), "host guidance\n")
            for name in runtime_files:
                self.assertEqual(
                    (codex / name).read_bytes(),
                    ("volume:" + name).encode(),
                )
            for name in runtime_directories:
                self.assertEqual(
                    (codex / name / "volume.bin").read_bytes(),
                    ("volume:" + name).encode(),
                )
                self.assertFalse((codex / name / "host-only.bin").exists())

    def test_codex_host_import_helpers_reject_runtime_names_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "codex"
            source_file = root / "source.jsonl"
            source_directory = root / "source-sessions"
            codex.mkdir()
            source_file.write_text("host history\n", encoding="utf-8")
            source_directory.mkdir()
            (source_directory / "host.jsonl").write_text(
                "host session\n",
                encoding="utf-8",
            )
            (codex / "history.jsonl").write_text(
                "volume history\n",
                encoding="utf-8",
            )
            (codex / "sessions").mkdir()
            (codex / "sessions" / "volume.jsonl").write_text(
                "volume session\n",
                encoding="utf-8",
            )

            file_script = (
                self.host_import_functions
                + '\nCODEX_DIR="$1"\ncopy_host_codex_entry "$2" history.jsonl\n'
            )
            file_result = subprocess.run(
                ["bash", "-c", file_script, "bash", str(codex), str(source_file)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(file_result.returncode, 0)
            self.assertIn("refusing unsupported", file_result.stderr)
            self.assertEqual(
                (codex / "history.jsonl").read_text(),
                "volume history\n",
            )

            escaped_destination = root / "escaped.config.toml"
            escaped_destination.write_text("volume profile\n", encoding="utf-8")
            traversal_script = (
                self.host_import_functions
                + '\nCODEX_DIR="$1"\ncopy_host_codex_entry "$2" ../escaped.config.toml\n'
            )
            traversal_result = subprocess.run(
                ["bash", "-c", traversal_script, "bash", str(codex), str(source_file)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(traversal_result.returncode, 0)
            self.assertIn("refusing unsafe", traversal_result.stderr)
            self.assertEqual(
                escaped_destination.read_text(),
                "volume profile\n",
            )

            directory_script = (
                self.host_import_functions
                + '\nCODEX_DIR="$1"\ncopy_host_codex_directory "$2" sessions\n'
            )
            directory_result = subprocess.run(
                [
                    "bash",
                    "-c",
                    directory_script,
                    "bash",
                    str(codex),
                    str(source_directory),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(directory_result.returncode, 0)
            self.assertIn("refusing unsupported", directory_result.stderr)
            self.assertEqual(
                (codex / "sessions" / "volume.jsonl").read_text(),
                "volume session\n",
            )
            self.assertFalse((codex / "sessions" / "host.jsonl").exists())

    def test_embedded_python_uses_isolated_import_mode(self):
        self.assertNotIn("python3 - <<'PY'", ENTRYPOINT_CLAUDE.read_text())
        self.assertNotIn("python3 - <<'PY'", ENTRYPOINT_CODEX.read_text())
        self.assertIn("python3 -I - <<'PY'", ENTRYPOINT_CLAUDE.read_text())
        self.assertIn("python3 -I - <<'PY'", ENTRYPOINT_CODEX.read_text())


if __name__ == "__main__":
    unittest.main()
