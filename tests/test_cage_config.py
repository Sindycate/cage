import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cage_config", ROOT / "cage-config.py")
cage_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = cage_config
SPEC.loader.exec_module(cage_config)


class CageConfigTests(unittest.TestCase):
    def base_config(self):
        return {
            "version": 1,
            "default_preset": "codex-main",
            "defaults": {"net": "gate", "session_sync": True},
            "auth": {
                "codex-oauth": {
                    "tool": "codex",
                    "host_codex_dir": "~/.codex-work",
                    "copy_auth": True,
                },
                "codex-proxy": {
                    "tool": "codex",
                    "host_codex_dir": "~/.codex-proxy",
                    "copy_auth": False,
                    "env": ["OPENAI_BASE_URL", "COMPANY_API_KEY"],
                },
            },
            "identities": {
                "work": {
                    "git_user_name": "A User",
                    "git_user_email": "a@example.com",
                    "gh_auth": True,
                }
            },
            "mcp_packs": {
                "linear": {
                    "env": ["LINEAR_API_KEY"],
                    "servers": [
                        {
                            "name": "linear",
                            "type": "http",
                            "url": "https://mcp.linear.app/mcp",
                            "bearer_token_env_var": "LINEAR_API_KEY",
                        }
                    ],
                },
                "local": {
                    "servers": [
                        {
                            "name": "jira",
                            "type": "stdio",
                            "command": "npx -y @company/jira-mcp",
                        }
                    ]
                },
            },
            "presets": {
                "codex-main": {
                    "tool": "codex",
                    "auth": "codex-oauth",
                    "identity": "work",
                    "mcp_packs": ["linear", "local"],
                },
                "codex-proxy": {
                    "tool": "codex",
                    "auth": "codex-proxy",
                    "identity": "work",
                    "mcp_packs": ["linear"],
                    "net": "open",
                },
            },
            "projects": {
                "/tmp/project-a": "codex-main",
                "/tmp/project-a/debug": "codex-proxy",
            },
        }

    def resolve(self, data, repo="/tmp/project-a"):
        return cage_config.resolve_config(data, Path("/tmp/config.toml"), repo)

    def test_resolves_project_preset_and_mcp_packs(self):
        resolved = self.resolve(self.base_config(), "/tmp/project-a/src")

        self.assertEqual(resolved.preset_name, "codex-main")
        self.assertEqual(resolved.tool, "codex")
        self.assertEqual(resolved.net, "gate")
        self.assertEqual(resolved.session_sync, "1")
        self.assertEqual(resolved.git_user_email, "a@example.com")
        self.assertEqual(resolved.codex_copy_auth, "1")
        self.assertEqual(resolved.stdio_mcp[0]["name"], "jira")
        self.assertEqual(resolved.remote_mcp[0]["name"], "linear")
        self.assertEqual(resolved.extra_env, ["LINEAR_API_KEY"])

    def test_longest_project_prefix_wins(self):
        resolved = self.resolve(self.base_config(), "/tmp/project-a/debug/subdir")

        self.assertEqual(resolved.preset_name, "codex-proxy")
        self.assertEqual(resolved.net, "open")
        self.assertEqual(resolved.codex_copy_auth, "0")
        self.assertIn("COMPANY_API_KEY", resolved.extra_env)

    def test_explicit_tool_must_match_preset(self):
        data = self.base_config()
        with self.assertRaises(cage_config.ConfigError):
            cage_config.resolve_config(
                data,
                Path("/tmp/config.toml"),
                "/tmp/project-a",
                preset_name="codex-main",
                explicit_tool="claude",
            )

    def test_duplicate_mcp_names_are_rejected(self):
        data = self.base_config()
        data["mcp_packs"]["dupe"] = {
            "servers": [
                {
                    "name": "linear",
                    "type": "stdio",
                    "command": "npx something",
                }
            ]
        }
        data["presets"]["codex-main"]["mcp_packs"] = ["linear", "dupe"]

        with self.assertRaises(cage_config.ConfigError):
            self.resolve(data)

    def test_missing_auth_reference_is_rejected(self):
        data = self.base_config()
        data["presets"]["codex-main"]["auth"] = "missing"

        with self.assertRaises(cage_config.ConfigError):
            self.resolve(data)

    def test_shell_output_is_quoted_and_json_encoded(self):
        resolved = self.resolve(self.base_config())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            # Check the JSON value that would be carried through the shell.
            remote = json.dumps(resolved.remote_mcp, separators=(",", ":"))
            path.write_text(remote)
            loaded = json.loads(path.read_text())

        self.assertEqual(loaded[0]["bearer_token_env_var"], "LINEAR_API_KEY")
        self.assertIn(" ", cage_config.shell_assign("GIT_USER_NAME", resolved.git_user_name))

    def test_replace_projects_section(self):
        text = "version = 1\n\n[projects]\n\"/old\" = \"a\"\n\n[presets.a]\ntool = \"codex\"\n"
        updated = cage_config.replace_projects_section(text, {"/new": "b"})

        self.assertIn('"/new" = "b"', updated)
        self.assertNotIn('"/old" = "a"', updated)
        self.assertIn("[presets.a]", updated)


if __name__ == "__main__":
    unittest.main()
