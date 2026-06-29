import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


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
            "host_commands": {
                "ztoken": {"command": "ztoken token -n codex"},
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

    def test_resolves_oauth_http_mcp_server(self):
        data = self.base_config()
        data["mcp_packs"]["dash0"] = {
            "servers": [
                {
                    "name": "dash0",
                    "type": "http",
                    "url": "https://api.eu-central-1.aws.dash0.com/mcp",
                    "auth": "oauth",
                    "oauth_resource": "https://api.eu-central-1.aws.dash0.com/mcp",
                    "oauth_client_id_env_var": "DASH0_OAUTH_CLIENT_ID",
                    "oauth_scopes": ["read:metrics", "read:logs"],
                }
            ],
        }
        data["presets"]["codex-main"]["mcp_packs"] = ["dash0"]

        resolved = self.resolve(data)

        self.assertEqual(resolved.remote_mcp[0]["name"], "dash0")
        self.assertEqual(resolved.remote_mcp[0]["auth"], "oauth")
        self.assertEqual(
            resolved.remote_mcp[0]["oauth_resource"],
            "https://api.eu-central-1.aws.dash0.com/mcp",
        )
        self.assertEqual(resolved.remote_mcp[0]["oauth_client_id_env_var"], "DASH0_OAUTH_CLIENT_ID")
        self.assertEqual(resolved.remote_mcp[0]["oauth_scopes"], ["read:metrics", "read:logs"])
        self.assertIn("DASH0_OAUTH_CLIENT_ID", resolved.extra_env)

    def test_oauth_mcp_rejects_stdio_server(self):
        data = self.base_config()
        data["mcp_packs"]["bad"] = {
            "servers": [
                {
                    "name": "bad",
                    "type": "stdio",
                    "auth": "oauth",
                    "command": "npx bad",
                }
            ]
        }
        data["presets"]["codex-main"]["mcp_packs"] = ["bad"]

        with self.assertRaisesRegex(cage_config.ConfigError, "must use type"):
            self.resolve(data)

    def test_oauth_mcp_rejects_bearer_token(self):
        data = self.base_config()
        data["mcp_packs"]["bad"] = {
            "servers": [
                {
                    "name": "bad",
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "auth": "oauth",
                    "bearer_token_env_var": "TOKEN",
                }
            ]
        }
        data["presets"]["codex-main"]["mcp_packs"] = ["bad"]

        with self.assertRaisesRegex(cage_config.ConfigError, "cannot combine OAuth"):
            self.resolve(data)

    def test_oauth_mcp_is_codex_only(self):
        data = self.base_config()
        data["auth"]["claude-bedrock"] = {
            "tool": "claude",
            "mode": "bedrock",
        }
        data["mcp_packs"]["dash0"] = {
            "servers": [
                {
                    "name": "dash0",
                    "type": "http",
                    "url": "https://api.eu-central-1.aws.dash0.com/mcp",
                    "auth": "oauth",
                }
            ]
        }
        data["presets"]["claude-bedrock"] = {
            "tool": "claude",
            "auth": "claude-bedrock",
            "mcp_packs": ["dash0"],
        }

        with self.assertRaisesRegex(cage_config.ConfigError, "Codex presets only"):
            cage_config.resolve_config(data, Path("/tmp/config.toml"), "/tmp/project-a", "claude-bedrock")

    def test_mcp_login_invokes_codex_with_oauth_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_dir = tmp_path / ".codex-dash0"
            config = tmp_path / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'default_preset = "codex-dash0"',
                        "[auth.codex-dash0]",
                        'tool = "codex"',
                        f'host_codex_dir = "{codex_dir}"',
                        "[mcp_packs.dash0]",
                        "servers = [",
                        '  { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", auth = "oauth", oauth_resource = "https://api.eu-central-1.aws.dash0.com/mcp", oauth_client_id_env_var = "DASH0_OAUTH_CLIENT_ID", oauth_scopes = ["read:metrics"] },',
                        "]",
                        "[presets.codex-dash0]",
                        'tool = "codex"',
                        'auth = "codex-dash0"',
                        'mcp_packs = ["dash0"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"DASH0_OAUTH_CLIENT_ID": "client-public-id"}),
                patch.object(cage_config.subprocess, "call", return_value=0) as call,
            ):
                result = cage_config.command_mcp_login(
                    SimpleNamespace(config=config, preset=None, name="dash0", repo="/tmp/project-a")
                )

        self.assertEqual(result, 0)
        args, kwargs = call.call_args
        self.assertEqual(args[0][:2], ["codex", "-c"])
        self.assertIn('mcp_oauth_credentials_store="file"', args[0])
        self.assertIn('mcp_servers.dash0.url="https://api.eu-central-1.aws.dash0.com/mcp"', args[0])
        self.assertIn('mcp_servers.dash0.oauth_resource="https://api.eu-central-1.aws.dash0.com/mcp"', args[0])
        self.assertIn('mcp_servers.dash0.oauth.client_id="client-public-id"', args[0])
        self.assertEqual(args[0][-5:], ["mcp", "login", "--scopes", "read:metrics", "dash0"])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(codex_dir))

    def test_mcp_login_requires_selected_oauth_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        "version = 1",
                        'default_preset = "codex-main"',
                        "[auth.codex-main]",
                        'tool = "codex"',
                        "[presets.codex-main]",
                        'tool = "codex"',
                        'auth = "codex-main"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(cage_config.ConfigError, "not selected"):
                cage_config.command_mcp_login(
                    SimpleNamespace(config=config, preset=None, name="dash0", repo="/tmp/project-a")
                )

    def test_replace_projects_section(self):
        text = "version = 1\n\n[projects]\n\"/old\" = \"a\"\n\n[presets.a]\ntool = \"codex\"\n"
        updated = cage_config.replace_projects_section(text, {"/new": "b"})

        self.assertIn('"/new" = "b"', updated)
        self.assertNotIn('"/old" = "a"', updated)
        self.assertIn("[presets.a]", updated)

    def test_interactive_selection_resolves_existing_blocks(self):
        data = self.base_config()
        original = json.dumps(data, sort_keys=True)
        selections = cage_config.InteractiveSelections(
            tool="codex",
            auth_name="codex-proxy",
            identity_name="work",
            mcp_pack_names=["linear", "local"],
            host_command_names=["ztoken"],
            net="gate",
        )

        resolved = cage_config.resolve_interactive_selection(
            data,
            Path("/tmp/config.toml"),
            "/tmp/project-a",
            selections,
        )

        self.assertEqual(resolved.preset_name, "interactive")
        self.assertEqual(resolved.preset_source, "interactive")
        self.assertEqual(resolved.tool, "codex")
        self.assertEqual(resolved.auth_name, "codex-proxy")
        self.assertEqual(resolved.identity_name, "work")
        self.assertEqual([server["name"] for server in resolved.remote_mcp], ["linear"])
        self.assertEqual([server["name"] for server in resolved.stdio_mcp], ["jira"])
        self.assertEqual(resolved.host_commands[0]["name"], "ztoken")
        self.assertIn("COMPANY_API_KEY", resolved.extra_env)
        self.assertEqual(json.dumps(data, sort_keys=True), original)

    def test_interactive_explicit_tool_must_match_selection(self):
        data = self.base_config()
        selections = cage_config.InteractiveSelections(
            tool="codex",
            auth_name="codex-oauth",
            net="open",
        )

        with self.assertRaises(cage_config.ConfigError):
            cage_config.resolve_interactive_selection(
                data,
                Path("/tmp/config.toml"),
                "/tmp/project-a",
                selections,
                explicit_tool="claude",
            )

    def test_interactive_selection_works_without_default_preset(self):
        data = {
            "version": 1,
            "auth": {
                "codex-oauth": {
                    "tool": "codex",
                    "copy_auth": True,
                },
            },
        }
        selections = cage_config.InteractiveSelections(
            tool="codex",
            auth_name="codex-oauth",
            net="open",
        )

        resolved = cage_config.resolve_interactive_selection(
            data,
            Path("/tmp/config.toml"),
            "/tmp/project-a",
            selections,
        )

        self.assertEqual(resolved.preset_name, "interactive")
        self.assertEqual(resolved.tool, "codex")
        self.assertEqual(resolved.codex_copy_auth, "1")

    def test_config_edit_splits_editor_with_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("version = 1\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"EDITOR": "code -w"}),
                patch.object(cage_config.subprocess, "call", return_value=0) as call,
            ):
                result = cage_config.command_edit(SimpleNamespace(config=config))

        self.assertEqual(result, 0)
        call.assert_called_once_with(["code", "-w", str(config)])

    def test_config_edit_rejects_invalid_editor_quoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text("version = 1\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"EDITOR": "'code -w"}),
                patch.object(cage_config.subprocess, "call") as call,
            ):
                with self.assertRaisesRegex(cage_config.ConfigError, "invalid EDITOR value"):
                    cage_config.command_edit(SimpleNamespace(config=config))

        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
