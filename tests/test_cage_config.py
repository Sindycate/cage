import importlib.util
import io
import json
import os
import pty
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

    def test_transport_name_normalization_collisions_are_rejected(self):
        data = self.base_config()
        data["mcp_packs"]["collision"] = {
            "servers": [
                {"name": "JIRA", "type": "stdio", "command": "jira-mcp"},
            ]
        }
        data["presets"]["codex-main"]["mcp_packs"] = ["local", "collision"]

        with self.assertRaisesRegex(cage_config.ConfigError, "relay normalization"):
            self.resolve(data)

    def test_authenticated_http_mcp_requires_https(self):
        data = self.base_config()
        data["mcp_packs"]["linear"]["servers"][0]["url"] = "http://mcp.example.test/mcp"

        with self.assertRaisesRegex(cage_config.ConfigError, "must use https"):
            self.resolve(data)

    def test_sensitive_header_requires_environment_reference(self):
        with self.assertRaisesRegex(cage_config.ConfigError, "literal secret"):
            cage_config.validate_headers(
                {"Authorization": "Bearer hard-coded-token"},
                "server.headers",
            )

    def test_claude_header_environment_reference_is_forwarded_automatically(self):
        data = {
            "version": 1,
            "default_preset": "claude-main",
            "auth": {"claude-main": {"tool": "claude", "mode": "api-key"}},
            "mcp_packs": {
                "custom": {
                    "servers": [
                        {
                            "name": "custom",
                            "type": "http",
                            "url": "https://mcp.example.test/mcp",
                            "headers": {"Authorization": "Bearer ${CUSTOM_TOKEN}"},
                        }
                    ]
                }
            },
            "presets": {
                "claude-main": {
                    "tool": "claude",
                    "auth": "claude-main",
                    "mcp_packs": ["custom"],
                }
            },
        }

        resolved = cage_config.resolve_config(
            data, Path("/tmp/config.toml"), "/tmp/project"
        )

        self.assertEqual(resolved.extra_env, ["CUSTOM_TOKEN"])
        self.assertEqual(
            resolved.remote_mcp[0]["headers"]["Authorization"],
            "Bearer ${CUSTOM_TOKEN}",
        )

    def test_codex_custom_headers_fail_instead_of_being_silently_ignored(self):
        data = self.base_config()
        data["mcp_packs"]["linear"]["servers"][0]["headers"] = {
            "X-Tenant": "example"
        }

        with self.assertRaisesRegex(cage_config.ConfigError, "not supported for Codex"):
            self.resolve(data)

    def test_name_validators_reject_final_newline(self):
        for validator, value in [
            (cage_config.require_name, "server\n"),
            (cage_config.require_skill_name, "skill\n"),
            (cage_config.require_env_name, "TOKEN\n"),
        ]:
            with self.subTest(validator=validator.__name__):
                with self.assertRaises(cage_config.ConfigError):
                    validator(value, "test")

    def test_load_config_rejects_unknown_security_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(
                "\n".join(
                    [
                        "version = 1",
                        "[auth.work]",
                        'tool = "codex"',
                        "copy_aut = false",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(cage_config.ConfigError, "copy_aut"):
                cage_config.load_config(config)

    def test_schema_rejects_unknown_inline_keys_in_unused_preset(self):
        data = {
            "version": 1,
            "presets": {
                "unused": {
                    "tool": "codex",
                    "host_commands": [
                        {"name": "token", "command": "token", "unexpected": True}
                    ],
                }
            },
        }

        with self.assertRaisesRegex(cage_config.ConfigError, "unexpected"):
            cage_config.validate_schema(data)

    def test_schema_rejects_newline_in_unused_extra_mount(self):
        data = {
            "version": 1,
            "presets": {
                "unused": {
                    "tool": "codex",
                    "extra_mounts": ["/tmp/first\n/tmp/second"],
                }
            },
        }

        with self.assertRaisesRegex(cage_config.ConfigError, "without newlines"):
            cage_config.validate_schema(data)

    def test_resolves_selected_skill_packs(self):
        data = self.base_config()
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".agents"
            for name in ["agents-best-practices", "linear-ticket-flow", "dash0-dashboard-flow"]:
                skill_dir = agents / "skills" / name
                skill_dir.mkdir(parents=True)
                skill_dir.joinpath("SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\n\n# Test\n",
                    encoding="utf-8",
                )
            data["skill_packs"] = {
                "agent-basics": {
                    "source": str(agents),
                    "skills": ["agents-best-practices"],
                },
                "external-systems": {
                    "source": str(agents),
                    "skills": ["linear-ticket-flow", "dash0-dashboard-flow"],
                },
            }
            data["presets"]["codex-main"]["skill_packs"] = ["agent-basics", "external-systems"]

            resolved = self.resolve(data)

        self.assertEqual(resolved.skill_pack_names, ["agent-basics", "external-systems"])
        self.assertEqual(
            [skill["name"] for skill in resolved.skill_mounts],
            ["agents-best-practices", "linear-ticket-flow", "dash0-dashboard-flow"],
        )

    def test_missing_skill_pack_is_rejected(self):
        data = self.base_config()
        data["presets"]["codex-main"]["skill_packs"] = ["missing"]

        with self.assertRaisesRegex(cage_config.ConfigError, "skill pack not found"):
            self.resolve(data)

    def test_missing_skill_md_is_rejected(self):
        data = self.base_config()
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".agents"
            (agents / "skills" / "linear-ticket-flow").mkdir(parents=True)
            data["skill_packs"] = {
                "external-systems": {
                    "source": str(agents),
                    "skills": ["linear-ticket-flow"],
                },
            }
            data["presets"]["codex-main"]["skill_packs"] = ["external-systems"]

            with self.assertRaisesRegex(cage_config.ConfigError, "missing SKILL.md"):
                self.resolve(data)

    def test_duplicate_skill_names_are_rejected(self):
        data = self.base_config()
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".agents"
            skill_dir = agents / "skills" / "linear-ticket-flow"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: linear-ticket-flow\ndescription: test\n---\n",
                encoding="utf-8",
            )
            data["skill_packs"] = {
                "a": {"source": str(agents), "skills": ["linear-ticket-flow"]},
                "b": {"source": str(agents), "skills": ["linear-ticket-flow"]},
            }
            data["presets"]["codex-main"]["skill_packs"] = ["a", "b"]

            with self.assertRaisesRegex(cage_config.ConfigError, "duplicate skill name"):
                self.resolve(data)

    def test_explain_shows_selected_skill_packs(self):
        data = self.base_config()
        with tempfile.TemporaryDirectory() as tmp:
            agents = Path(tmp) / ".agents"
            skill_dir = agents / "skills" / "linear-ticket-flow"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: linear-ticket-flow\ndescription: test\n---\n",
                encoding="utf-8",
            )
            data["skill_packs"] = {
                "external-systems": {
                    "source": str(agents),
                    "skills": ["linear-ticket-flow"],
                },
            }
            data["presets"]["codex-main"]["skill_packs"] = ["external-systems"]
            resolved = self.resolve(data)
            out = io.StringIO()
            with (
                patch("sys.stdout", out),
                patch.object(cage_config.shutil, "which", return_value="/usr/bin/docker"),
            ):
                result = cage_config.explain(resolved, doctor=True)

        self.assertEqual(result, 0)
        self.assertIn("Skill packs: external-systems", out.getvalue())
        self.assertIn("linear-ticket-flow", out.getvalue())
        self.assertIn("Capabilities:", out.getvalue())
        self.assertIn("host execution: enabled", out.getvalue())
        self.assertIn("Doctor: ok", out.getvalue())

    def test_doctor_warns_when_host_command_embeds_fixed_arguments(self):
        data = self.base_config()
        data["presets"]["codex-main"]["host_commands"] = ["ztoken"]
        resolved = self.resolve(data)
        out = io.StringIO()
        with (
            patch("sys.stdout", out),
            patch.object(cage_config.shutil, "which", return_value="/usr/bin/tool"),
        ):
            result = cage_config.explain(resolved, doctor=True)

        self.assertEqual(result, 0)
        self.assertIn("host command 'ztoken' embeds fixed arguments", out.getvalue())
        self.assertIn("de-duplicates an identical caller suffix", out.getvalue())
        self.assertIn("Doctor: ok with warnings", out.getvalue())

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

    def test_resolves_claude_oauth_http_mcp_server(self):
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
                    "oauth_resource": "https://api.eu-central-1.aws.dash0.com/mcp",
                    "oauth_client_id_env_var": "DASH0_OAUTH_CLIENT_ID",
                    "oauth_scopes": ["read:metrics"],
                }
            ]
        }
        data["presets"]["claude-bedrock"] = {
            "tool": "claude",
            "auth": "claude-bedrock",
            "mcp_packs": ["dash0"],
        }

        resolved = cage_config.resolve_config(
            data, Path("/tmp/config.toml"), "/tmp/project-a", "claude-bedrock"
        )

        self.assertEqual(resolved.tool, "claude")
        self.assertEqual(resolved.remote_mcp[0]["name"], "dash0")
        self.assertEqual(resolved.remote_mcp[0]["auth"], "oauth")
        self.assertEqual(resolved.remote_mcp[0]["oauth_client_id_env_var"], "DASH0_OAUTH_CLIENT_ID")
        self.assertEqual(resolved.remote_mcp[0]["oauth_scopes"], ["read:metrics"])
        self.assertIn("DASH0_OAUTH_CLIENT_ID", resolved.extra_env)

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

    def test_replace_projects_section_preserves_comment_and_array_table(self):
        text = (
            "version = 1\n\n"
            "[projects] # project routing\n"
            "# This comment should survive.\n"
            '"/old" = "a"\n\n'
            "[[widgets]]\n"
            'name = "keep"\n'
        )

        updated = cage_config.replace_projects_section(text, {"/new": "b"})
        parsed = cage_config.tomllib.loads(updated)

        self.assertIn("[projects] # project routing", updated)
        self.assertIn("# This comment should survive.", updated)
        self.assertEqual(parsed["projects"], {"/new": "b"})
        self.assertEqual(parsed["widgets"], [{"name": "keep"}])

    def test_starter_config_is_minimal_and_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            cage_config.command_init(SimpleNamespace(config=config, force=False))
            data = cage_config.load_config(config)
            resolved = cage_config.resolve_config(data, config, "/tmp/example")
            config_mode = config.stat().st_mode & 0o777
            out = io.StringIO()
            with patch("sys.stdout", out):
                cage_config.explain(resolved)

        self.assertEqual(resolved.preset_name, "codex-local")
        self.assertEqual(resolved.tool, "codex")
        self.assertEqual(resolved.mcp_pack_names, [])
        self.assertEqual(resolved.skill_pack_names, [])
        self.assertEqual(resolved.host_commands, [])
        self.assertEqual(config_mode, 0o600)
        self.assertNotIn("Claude session sync", out.getvalue())

    def test_atomic_write_preserves_config_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "dotfiles-config.toml"
            link = root / "config.toml"
            target.write_text("version = 1\n", encoding="utf-8")
            link.symlink_to(target)

            cage_config.atomic_write_text(link, "version = 1\n# updated\n")

            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(), "version = 1\n# updated\n")

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

    def test_open_tty_uses_nonseekable_device(self):
        master_fd, slave_fd = pty.openpty()
        read_fd = os.dup(slave_fd)
        write_fd = os.dup(slave_fd)

        try:
            with patch.object(cage_config.os, "open", side_effect=[read_fd, write_fd]):
                with cage_config.open_tty() as tty:
                    tty.write("hello")
                    tty.flush()
                    self.assertEqual(os.read(master_fd, 5), b"hello")

                    os.write(master_fd, b"answer\n")
                    self.assertEqual(tty.readline(), "answer\n")
        finally:
            os.close(master_fd)
            os.close(slave_fd)

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

    def test_preset_yolo_resolves_as_explicit_capability(self):
        data = self.base_config()
        data["presets"]["codex-main"]["yolo"] = True

        resolved = self.resolve(data)

        self.assertEqual(resolved.yolo, "1")

    def test_ui_summary_shows_yolo_network_default_as_gate(self):
        data = {
            "version": 1,
            "default_preset": "main",
            "presets": {"main": {"tool": "codex", "yolo": True}},
        }

        summary = cage_config.ui_summary(data, Path("/tmp/config.toml"), "/tmp/project")

        self.assertEqual(summary["effective"]["net"], "gate")

    def test_ui_rename_updates_all_references_atomically(self):
        data = self.base_config()

        updated = cage_config.apply_ui_operations(data, [{
            "action": "rename",
            "collection": "mcp_packs",
            "name": "linear",
            "new_name": "company-linear",
        }])

        self.assertNotIn("linear", updated["mcp_packs"])
        self.assertIn("company-linear", updated["mcp_packs"])
        self.assertEqual(
            updated["presets"]["codex-main"]["mcp_packs"],
            ["company-linear", "local"],
        )
        self.assertEqual(data["presets"]["codex-main"]["mcp_packs"][0], "linear")

    def test_ui_delete_referenced_object_is_blocked(self):
        with self.assertRaisesRegex(cage_config.ConfigError, "referenced by"):
            cage_config.apply_ui_operations(self.base_config(), [{
                "action": "delete",
                "collection": "auth",
                "name": "codex-oauth",
            }])

    def test_ui_semantically_validates_each_affected_preset(self):
        data = self.base_config()
        operations = [{
            "action": "upsert", "collection": "presets", "name": "codex-main",
            "value": {
                "tool": "codex", "auth": "codex-oauth",
                "mcp_packs": ["linear", "linear"],
            },
        }]
        updated = cage_config.apply_ui_operations(data, operations)

        with self.assertRaisesRegex(cage_config.ConfigError, "duplicate MCP server"):
            cage_config.validate_affected_presets(
                data, updated, operations, Path("/tmp/config.toml"), "/tmp/project-a"
            )

    def test_ui_render_preserves_untouched_comments_and_tables(self):
        original = """# top comment
version = 1
default_preset = "main"

[auth.work]
# edited comment may be canonicalized
tool = "codex"

[identities.personal]
# this exact block must remain untouched
git_user_name = "Somebody"

[presets.main]
tool = "codex"
auth = "work"
"""
        before = cage_config.tomllib.loads(original)
        after = cage_config.apply_ui_operations(before, [{
            "action": "upsert",
            "collection": "auth",
            "name": "work",
            "value": {"tool": "codex", "copy_auth": False},
        }])

        rendered = cage_config.render_config_changes(original, before, after)

        untouched = "[identities.personal]\n# this exact block must remain untouched\ngit_user_name = \"Somebody\""
        self.assertIn("# top comment", rendered)
        self.assertIn(untouched, rendered)
        self.assertEqual(cage_config.tomllib.loads(rendered), after)

    def test_ui_render_canonicalizes_edited_array_table_object(self):
        original = """version = 1
default_preset = "main"

[mcp_packs.local]
[[mcp_packs.local.servers]]
name = "local"
type = "stdio"
command = "old-command"

[presets.main]
tool = "codex"
mcp_packs = ["local"]
"""
        before = cage_config.tomllib.loads(original)
        after = cage_config.apply_ui_operations(before, [{
            "action": "upsert", "collection": "mcp_packs", "name": "local",
            "value": {"servers": [{"name": "local", "type": "stdio", "command": "new-command"}]},
        }])

        rendered = cage_config.render_config_changes(original, before, after)

        self.assertNotIn("[[mcp_packs.local.servers]]", rendered)
        self.assertEqual(cage_config.tomllib.loads(rendered), after)

    def test_ui_commit_detects_concurrent_change_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            opening = cage_config.sha256_text(config.read_text(encoding="utf-8"))
            request = root / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": opening,
                "operations": [{"action": "set_default", "name": "main"}],
            }), encoding="utf-8")
            config.write_text(config.read_text(encoding="utf-8") + "# concurrent\n", encoding="utf-8")

            with self.assertRaisesRegex(cage_config.ConfigError, "changed since"):
                cage_config.command_ui_commit(SimpleNamespace(
                    config=config, request=request, repo=str(root)
                ))

            self.assertFalse((root / "backups").exists())

    def test_ui_commit_preserves_mode_and_writes_private_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            config.chmod(0o640)
            request = root / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": cage_config.sha256_text(config.read_text(encoding="utf-8")),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "codex", "net": "gate"},
                }],
            }), encoding="utf-8")

            with patch("sys.stdout", new=io.StringIO()):
                cage_config.command_ui_commit(SimpleNamespace(
                    config=config, request=request, repo=str(root)
                ))

            self.assertEqual(config.stat().st_mode & 0o777, 0o640)
            backups = list((root / "backups").glob("config-*.toml"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(cage_config.tomllib.loads(config.read_text())["presets"]["main"]["net"], "gate")

    def test_hidden_project_preset_name_is_stable_and_path_specific(self):
        first = cage_config.hidden_project_preset_name("/tmp/example")
        self.assertEqual(first, cage_config.hidden_project_preset_name("/tmp/example"))
        self.assertNotEqual(first, cage_config.hidden_project_preset_name("/other/example"))
        self.assertTrue(first.startswith("__cage_project_example_"))

    def test_remember_project_does_not_overwrite_hidden_name_collision(self):
        data = self.base_config()
        path = "/tmp/collision-project"
        reserved = cage_config.hidden_project_preset_name(path)
        data["presets"][reserved] = {"tool": "claude"}

        updated = cage_config.apply_ui_operations(data, [{
            "action": "remember_project", "path": path,
            "value": {"tool": "codex", "auth": "codex-oauth"},
        }])

        mapped = updated["projects"][str(Path(path).resolve())]
        self.assertEqual(updated["presets"][reserved], {"tool": "claude"})
        self.assertEqual(mapped, reserved + "_2")

    def test_ui_commit_preserves_config_symlink_and_target_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "real-config.toml"
            config = root / "config.toml"
            target.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            target.chmod(0o640)
            config.symlink_to(target)
            request = root / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": cage_config.sha256_text(target.read_text()),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "codex", "yolo": True},
                }],
            }), encoding="utf-8")

            with patch("sys.stdout", new=io.StringIO()):
                cage_config.command_ui_commit(SimpleNamespace(
                    config=config, request=request, repo=str(root)
                ))

            self.assertTrue(config.is_symlink())
            self.assertEqual(config.resolve(), target.resolve())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertTrue(cage_config.tomllib.loads(target.read_text())["presets"]["main"]["yolo"])

    def test_ui_request_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "request-target.json"
            request = root / "request.json"
            target.write_text("{}", encoding="utf-8")
            request.symlink_to(target)

            with self.assertRaisesRegex(cage_config.ConfigError, "regular file"):
                cage_config.load_ui_request(request)

    def test_ui_backups_are_pruned_to_ten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            request = root / "request.json"
            config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            for index in range(12):
                request.write_text(json.dumps({
                    "expected_sha256": cage_config.sha256_text(config.read_text()),
                    "operations": [{
                        "action": "upsert", "collection": "presets", "name": "main",
                        "value": {"tool": "codex", "net": "gate" if index % 2 else "off"},
                    }],
                }), encoding="utf-8")
                with patch("sys.stdout", new=io.StringIO()):
                    cage_config.command_ui_commit(SimpleNamespace(
                        config=config, request=request, repo=str(root)
                    ))

            self.assertEqual(len(list((root / "backups").glob("config-*.toml"))), 10)


if __name__ == "__main__":
    unittest.main()
