from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cage_core import config, opencode_policy
from cage_core.models import LaunchRequest, ResolvedConfig
from cage_core.opencode import (
    OpenCodeError,
    create_launch_snapshot,
    write_runtime_environment,
)
from cage_core.planning import build_launch_plan
from cage_core.state.oauth import _canonical_json
from cage_core.state.oauth import SyncError
from cage_core.state.opencode import OpenCodeStateReconciler
from cage_core.targets import container as container_target


class OpenCodeConfigTests(unittest.TestCase):
    def test_resolves_container_image_state_plugins_and_mcp(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            config_dir = root / "opencode-config"
            data_dir = root / "opencode-data"
            config_dir.mkdir()
            data_dir.mkdir()
            agents = root / "agents"
            skill = agents / "skills" / "safe-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# safe\n", encoding="utf-8")
            data = {
                "version": 1,
                "default_preset": "main",
                "auth": {
                    "oc": {
                        "tool": "opencode",
                        "host_opencode_config_dir": str(config_dir),
                        "host_opencode_data_dir": str(data_dir),
                        "copy_auth": True,
                    }
                },
                "skill_packs": {
                    "safe": {"source": str(agents), "skills": ["safe-skill"]}
                },
                "mcp_packs": {
                    "selected": {
                        "servers": [
                            {"name": "local", "type": "stdio", "command": "tool --mcp"},
                            {
                                "name": "remote",
                                "type": "http",
                                "url": "https://example.test/mcp",
                                "auth": "oauth",
                                "oauth_scopes": ["read", "write"],
                            },
                        ]
                    }
                },
                "presets": {
                    "main": {
                        "tool": "opencode",
                        "auth": "oc",
                        "skill_packs": ["safe"],
                        "mcp_packs": ["selected"],
                        "opencode_plugins": True,
                    }
                },
            }
            config.validate_schema(data)
            resolved = config.resolve_config(data, root / "config.toml", str(repo))
            self.assertEqual(resolved.tool, "opencode")
            self.assertEqual(resolved.host_opencode_config_dir, str(config_dir))
            self.assertEqual(resolved.host_opencode_data_dir, str(data_dir))
            self.assertEqual(resolved.opencode_copy_auth, "1")
            self.assertEqual(resolved.opencode_plugins, "1")
            self.assertEqual([item["name"] for item in resolved.skill_mounts], ["safe-skill"])

            prepared = build_launch_plan(
                LaunchRequest(repo_operand=str(repo)),
                resolved,
                cage_version="0.28.0",
                config_root=root,
                install_root=Path(__file__).resolve().parents[1],
            )
            plan = prepared.plan
            self.assertEqual(plan.image, "opencode:0.28.0")
            self.assertEqual(plan.dockerfile, "Dockerfile.opencode")
            self.assertEqual(plan.container_home, "/home/opencode")
            self.assertTrue(plan.volume_name.startswith("opencode-state-"))
            self.assertIn("opencode-plugins", plan.capabilities)

    def test_only_opencode_receives_executable_tmpfs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            config_root = root / "config"
            repo.mkdir()
            config_root.mkdir()

            def tmpfs_for(tool: str) -> str:
                resolved = ResolvedConfig(
                    config_path=config_root / "config.toml",
                    repo_path=str(repo),
                    preset_name=tool,
                    preset_source="test",
                    tool=tool,
                    target="container",
                )
                prepared = build_launch_plan(
                    LaunchRequest(repo_operand=str(repo)),
                    resolved,
                    cage_version="0.28.0",
                    config_root=config_root,
                    install_root=Path(__file__).resolve().parents[1],
                )
                runtime = container_target.ContainerRuntime(
                    prepared=prepared,
                    install_root=Path(__file__).resolve().parents[1],
                    config_root=config_root,
                    docker="docker",
                )
                if tool == "opencode":
                    runtime.opencode_snapshot = root / "snapshot"
                arguments = container_target._base_docker_arguments(runtime)
                return arguments[arguments.index("--tmpfs") + 1]

            self.assertEqual(
                tmpfs_for("opencode"),
                "/tmp:rw,nosuid,nodev,exec,mode=1777",
            )
            self.assertEqual(tmpfs_for("claude"), "/tmp")
            self.assertEqual(tmpfs_for("codex"), "/tmp")

    def test_callback_ports_are_fixed_scoped_and_readiness_checked(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            config_root = root / "config"
            repo.mkdir()
            config_root.mkdir()

            def arguments_for(tool_arguments: tuple[str, ...]) -> list[str]:
                resolved = ResolvedConfig(
                    config_path=config_root / "config.toml",
                    repo_path=str(repo),
                    preset_name="opencode",
                    preset_source="test",
                    tool="opencode",
                    target="container",
                    remote_mcp=[
                        {
                            "name": "remote",
                            "type": "http",
                            "url": "https://mcp.example.test",
                            "auth": "oauth",
                        }
                    ],
                )
                prepared = build_launch_plan(
                    LaunchRequest(
                        repo_operand=str(repo),
                        tool_arguments=tool_arguments,
                    ),
                    resolved,
                    cage_version="0.28.0",
                    config_root=config_root,
                    install_root=Path(__file__).resolve().parents[1],
                )
                runtime = container_target.ContainerRuntime(
                    prepared=prepared,
                    install_root=Path(__file__).resolve().parents[1],
                    config_root=config_root,
                    docker="docker",
                    opencode_snapshot=root / "snapshot",
                )
                return container_target._base_docker_arguments(runtime)

            ordinary = arguments_for(())
            self.assertNotIn("-p", ordinary)
            mcp = arguments_for(("mcp", "auth", "remote"))
            self.assertIn("127.0.0.1:19876:19876", mcp)
            self.assertNotIn("127.0.0.1:1455:1455", mcp)
            provider = arguments_for(("auth", "login"))
            self.assertIn("127.0.0.1:1455:1455", provider)
            self.assertIn("127.0.0.1:19876:19876", provider)
            self.assertFalse(
                any(
                    value.startswith("0.0.0.0:")
                    for value in (*ordinary, *mcp, *provider)
                )
            )

            entrypoint = (
                Path(__file__).resolve().parents[1] / "entrypoint-opencode.sh"
            ).read_text(encoding="utf-8")
            self.assertIn("CAGE_OPENCODE_CALLBACK_READY", entrypoint)
            self.assertIn("OpenCode callback relay failed to start", entrypoint)

    def test_sensitive_environment_uses_private_handoff_not_docker_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            config_root = root / "config"
            snapshot = root / "snapshot"
            repo.mkdir()
            config_root.mkdir()
            snapshot.mkdir(mode=0o700)
            resolved = ResolvedConfig(
                config_path=config_root / "config.toml",
                repo_path=str(repo),
                preset_name="opencode",
                preset_source="test",
                tool="opencode",
                target="container",
                git_user_name="Private User",
                git_user_email="private@example.test",
                extra_env=["OPENCODE_TEST_TOKEN"],
            )
            prepared = build_launch_plan(
                LaunchRequest(repo_operand=str(repo)),
                resolved,
                cage_version="0.28.0",
                config_root=config_root,
                install_root=Path(__file__).resolve().parents[1],
            )
            runtime = container_target.ContainerRuntime(
                prepared=prepared,
                install_root=Path(__file__).resolve().parents[1],
                config_root=config_root,
                docker="docker",
                opencode_snapshot=snapshot,
            )
            with patch.dict(
                os.environ,
                {"OPENCODE_TEST_TOKEN": "selected-secret-value"},
                clear=False,
            ):
                arguments = container_target._base_docker_arguments(runtime)
            container_target._append_environment_argument(
                runtime,
                arguments,
                "MCP_BRIDGE_TOKEN",
                "bridge-secret-value",
            )
            rendered = "\n".join(arguments)
            for forbidden in (
                "selected-secret-value",
                "bridge-secret-value",
                "Private User",
                "private@example.test",
            ):
                self.assertNotIn(forbidden, rendered)

            handoff = snapshot / "runtime-env.json"
            write_runtime_environment(handoff, runtime.opencode_environment)
            self.assertEqual(os.stat(handoff).st_mode & 0o777, 0o600)
            payload = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual(payload["OPENCODE_TEST_TOKEN"], "selected-secret-value")
            self.assertEqual(payload["MCP_BRIDGE_TOKEN"], "bridge-secret-value")
            entrypoint = (
                Path(__file__).resolve().parents[1] / "entrypoint-opencode.sh"
            ).read_text(encoding="utf-8")
            self.assertIn("runtime-env.json", entrypoint)
            self.assertIn("mapfile -d '' -t PRIVATE_ENV", entrypoint)

    def test_offline_snapshot_omits_unreachable_stdio_bridge(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            config_root = root / "config"
            repo.mkdir()
            config_root.mkdir()
            resolved = ResolvedConfig(
                config_path=config_root / "config.toml",
                repo_path=str(repo),
                preset_name="opencode",
                preset_source="test",
                tool="opencode",
                target="container",
                net="off",
                stdio_mcp=[
                    {"name": "local", "type": "stdio", "command": "ignored"}
                ],
            )
            prepared = build_launch_plan(
                LaunchRequest(repo_operand=str(repo)),
                resolved,
                cage_version="0.28.0",
                config_root=config_root,
                install_root=Path(__file__).resolve().parents[1],
            )
            runtime = container_target.ContainerRuntime(
                prepared=prepared,
                install_root=Path(__file__).resolve().parents[1],
                config_root=config_root,
                docker="docker",
            )
            container_target._create_opencode_snapshot(runtime)
            assert runtime.opencode_snapshot is not None
            manifest = json.loads(
                (runtime.opencode_snapshot / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["selected_mcp"]["stdio"], [])

    def test_rejects_non_container_session_sync_and_oauth_resource(self):
        base = {
            "version": 1,
            "default_preset": "main",
            "auth": {"oc": {"tool": "opencode"}},
            "presets": {"main": {"tool": "opencode", "auth": "oc"}},
        }
        for key, value, message in (
            ("target", "host", "only supported for Codex"),
            ("session_sync", True, "not supported for OpenCode"),
        ):
            data = json.loads(json.dumps(base))
            data["presets"]["main"][key] = value
            with self.assertRaisesRegex(config.ConfigError, message):
                config.resolve_config(data, Path("config.toml"), "/tmp")

        data = json.loads(json.dumps(base))
        data["mcp_packs"] = {
            "oauth": {
                "servers": [
                    {
                        "name": "remote",
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "auth": "oauth",
                        "oauth_resource": "https://example.test",
                    }
                ]
            }
        }
        data["presets"]["main"]["mcp_packs"] = ["oauth"]
        with self.assertRaisesRegex(config.ConfigError, "cannot represent faithfully"):
            config.resolve_config(data, Path("config.toml"), "/tmp")

    def test_rejects_opencode_auth_fields_on_other_tools_and_codex_alias(self):
        data = {
            "version": 1,
            "default_preset": "main",
            "auth": {
                "wrong": {
                    "tool": "codex",
                    "host_opencode_data_dir": "/tmp/opencode",
                }
            },
            "presets": {"main": {"tool": "codex", "auth": "wrong"}},
        }
        with self.assertRaisesRegex(config.ConfigError, "only supported for OpenCode"):
            config.resolve_config(data, Path("config.toml"), "/tmp")

        data["auth"]["wrong"] = {
            "tool": "opencode",
            "codex_copy_auth": False,
        }
        data["presets"]["main"]["tool"] = "opencode"
        with self.assertRaisesRegex(config.ConfigError, "use copy_auth for OpenCode"):
            config.resolve_config(data, Path("config.toml"), "/tmp")


class OpenCodePolicyTests(unittest.TestCase):
    def test_forbidden_passthrough_and_selected_mcp_auth(self):
        selected = {"remote"}
        for argv in (
            ["--auto"],
            ["run", "--attach", "http://127.0.0.1:4096"],
            ["--dir", "/tmp"],
            ["--port", "8080"],
            ["serve"],
            ["/tmp/other-project"],
            ["mcp", "add"],
            ["plugin", "install"],
        ):
            with self.assertRaises(opencode_policy.PolicyError):
                opencode_policy.reject_unsafe_passthrough_args(
                    list(argv), selected_mcp_names=selected
                )
        opencode_policy.reject_unsafe_passthrough_args(
            ["mcp", "auth", "remote"], selected_mcp_names=selected
        )
        with self.assertRaisesRegex(opencode_policy.PolicyError, "not selected"):
            opencode_policy.reject_unsafe_passthrough_args(
                ["mcp", "auth", "other"], selected_mcp_names=selected
            )
        with self.assertRaisesRegex(opencode_policy.PolicyError, "requires"):
            opencode_policy.reject_unsafe_passthrough_args(
                ["mcp", "auth"], selected_mcp_names=selected
            )
        opencode_policy.reject_unsafe_passthrough_args(
            ["-m", "provider/model"], selected_mcp_names=selected
        )
        opencode_policy.reject_unsafe_passthrough_args(
            ["run", "--", "ordinary prompt"], selected_mcp_names=selected
        )


class OpenCodeSnapshotTests(unittest.TestCase):
    def test_snapshot_is_private_bounded_and_does_not_modify_repo(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            host = root / "host"
            repo = root / "repo"
            host.mkdir()
            repo.mkdir()
            (host / "opencode.jsonc").write_text("{\"model\":\"test\"}\n", encoding="utf-8")
            project = repo / ".opencode"
            project.mkdir()
            (project / "commands.json").write_text("{}\n", encoding="utf-8")
            project_skill = repo / ".agents" / "skills" / "project-skill"
            project_skill.mkdir(parents=True)
            (project_skill / "SKILL.md").write_text(
                "---\nname: project-skill\ndescription: test\n---\nproject\n",
                encoding="utf-8",
            )
            selected_skill = root / "selected-skill"
            selected_skill.mkdir()
            (selected_skill / "SKILL.md").write_text(
                "---\nname: selected-skill\ndescription: test\n---\nselected\n",
                encoding="utf-8",
            )
            (repo / "AGENTS.md").write_text("# frozen guidance\n", encoding="utf-8")
            (repo / "opencode.jsonc").write_text("{\"permission\":{}}\n", encoding="utf-8")
            before = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
            destination = root / "snapshot"
            create_launch_snapshot(
                destination=destination,
                host_config_directory=host,
                repository=repo,
                stdio_mcp=[{"name": "local", "command": "ignored"}],
                remote_mcp=[],
                skill_mounts=[
                    {"name": "selected-skill", "path": str(selected_skill)}
                ],
            )
            after = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
            self.assertEqual(before, after)
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(destination / "manifest.json").st_mode & 0o777, 0o600)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["selected_mcp"]["stdio"][0]["name"], "local")
            self.assertEqual(manifest["selected_skill_names"], ["selected-skill"])
            self.assertTrue(
                (destination / "project-agents-skills/project-skill/SKILL.md").is_file()
            )
            self.assertTrue(
                (destination / "selected-skills/selected-skill/SKILL.md").is_file()
            )
            self.assertEqual(
                (destination / "project-instruction/AGENTS.md").read_text(),
                "# frozen guidance\n",
            )
            (repo / "AGENTS.md").write_text("# changed after resolution\n", encoding="utf-8")
            self.assertEqual(
                (destination / "project-instruction/AGENTS.md").read_text(),
                "# frozen guidance\n",
            )

    def test_snapshot_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            host = root / "host"
            repo = root / "repo"
            host.mkdir()
            repo.mkdir()
            target = root / "target"
            target.write_text("{}", encoding="utf-8")
            (host / "linked.json").symlink_to(target)
            with self.assertRaisesRegex(OpenCodeError, "symlink"):
                create_launch_snapshot(
                    destination=root / "snapshot-one",
                    host_config_directory=host,
                    repository=repo,
                    stdio_mcp=[],
                    remote_mcp=[],
                )
            (host / "linked.json").unlink()
            os.link(target, host / "hard.json")
            with self.assertRaisesRegex(OpenCodeError, "hard-linked"):
                create_launch_snapshot(
                    destination=root / "snapshot-two",
                    host_config_directory=host,
                    repository=repo,
                    stdio_mcp=[],
                    remote_mcp=[],
                )

    def test_pure_snapshot_ignores_untrusted_plugin_dependency_tree(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            host = root / "host"
            repo = root / "repo"
            host.mkdir()
            repo.mkdir()
            (host / "opencode.json").write_text("{}\n", encoding="utf-8")
            plugins = host / "plugins"
            plugins.mkdir()
            (plugins / "escape.js").symlink_to(root / "outside.js")
            node_modules = host / "node_modules"
            node_modules.mkdir()
            (node_modules / "package").symlink_to(root)

            destination = root / "pure-snapshot"
            create_launch_snapshot(
                destination=destination,
                host_config_directory=host,
                repository=repo,
                stdio_mcp=[],
                remote_mcp=[],
            )
            self.assertTrue((destination / "global/opencode.json").is_file())
            self.assertFalse((destination / "global/plugins").exists())
            self.assertFalse((destination / "global/node_modules").exists())

            with self.assertRaisesRegex(OpenCodeError, "symlink"):
                create_launch_snapshot(
                    destination=root / "plugin-snapshot",
                    host_config_directory=host,
                    repository=repo,
                    stdio_mcp=[],
                    remote_mcp=[],
                    plugins_enabled=True,
                )


class FakeOpenCodeState(OpenCodeStateReconciler):
    def __init__(self, *args, volume_auth=None, volume_mcp=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.volume_auth = volume_auth
        self.volume_mcp = volume_mcp

    def _export(self, stage):
        manifest = {}
        for key, value in (("auth", self.volume_auth), ("mcp", self.volume_mcp)):
            if value is None:
                manifest[key] = {"exists": False, "sha256": None}
            else:
                raw = _canonical_json(value)
                (stage / f"{key}.raw").write_bytes(raw)
                manifest[key] = {"exists": True, "sha256": "unused"}
        return manifest

    def _apply(self, stage, manifest, auth, mcp):
        self.volume_auth = json.loads(auth) if auth is not None else None
        self.volume_mcp = json.loads(mcp) if mcp is not None else None


class OpenCodeStateTests(unittest.TestCase):
    def _state(self, root, **kwargs):
        host = root / "host-data"
        config_root = root / "cage-config"
        config_root.mkdir()
        return FakeOpenCodeState(
            volume_name="volume",
            image="opencode:test",
            host_directory=host,
            config_directory=config_root,
            selected_mcp_names={"selected"},
            copy_auth=True,
            selected_mcp_urls={"selected": "https://mcp.example.test"},
            **kwargs,
        )

    def test_exact_provider_and_selected_mcp_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(
                root,
                volume_auth={"old": {"type": "api", "key": "stale"}},
                volume_mcp={"unselected": {"tokens": {"accessToken": "stale"}}},
            )
            state.host_directory.mkdir()
            (state.host_directory / "auth.json").write_text(
                '{"provider":{"type":"api","key":"host"}}\n', encoding="utf-8"
            )
            (state.host_directory / "mcp-auth.json").write_text(
                '{"selected":{"serverUrl":"https://mcp.example.test","tokens":{"accessToken":"host"}},"other":{"tokens":{"accessToken":"keep"}}}\n',
                encoding="utf-8",
            )
            state.sync_in()
            self.assertEqual(
                state.volume_auth, {"provider": {"type": "api", "key": "host"}}
            )
            self.assertEqual(
                state.volume_mcp,
                {
                    "selected": {
                        "serverUrl": "https://mcp.example.test",
                        "tokens": {"accessToken": "host"},
                    }
                },
            )
            state.volume_auth = {
                "provider": {"type": "api", "key": "rotated"}
            }
            state.volume_mcp = {
                "selected": {
                    "serverUrl": "https://mcp.example.test",
                    "tokens": {"accessToken": "rotated"},
                }
            }
            state.sync_out()
            self.assertEqual(
                json.loads((state.host_directory / "auth.json").read_text()),
                state.volume_auth,
            )
            self.assertEqual(
                json.loads((state.host_directory / "mcp-auth.json").read_text()),
                {
                    "selected": {
                        "serverUrl": "https://mcp.example.test",
                        "tokens": {"accessToken": "rotated"},
                    },
                    "other": {"tokens": {"accessToken": "keep"}},
                },
            )

    def test_copy_auth_false_removes_volume_without_host_writeback(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(
                root,
                volume_auth={"old": {"type": "api", "key": "stale"}},
                volume_mcp={},
            )
            state.copy_auth = False
            state.host_directory.mkdir()
            ignored = '{"duplicate":1,"duplicate":2}\n'
            (state.host_directory / "auth.json").write_text(
                ignored, encoding="utf-8"
            )
            state.sync_in()
            self.assertIsNone(state.volume_auth)
            state.volume_auth = {"new": {"type": "api", "key": "volume"}}
            state.sync_out()
            self.assertIsNone(state.volume_auth)
            self.assertEqual(
                (state.host_directory / "auth.json").read_text(encoding="utf-8"),
                ignored,
            )

    def test_no_selected_host_state_does_not_create_host_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(
                root,
                volume_auth={"old": {"type": "api", "key": "stale"}},
                volume_mcp={"unselected": {"tokens": {"accessToken": "stale"}}},
            )
            state.copy_auth = False
            state.selected_mcp_names = set()
            state.selected_mcp_urls = {}
            state.sync_in()
            self.assertFalse(state.host_directory.exists())
            self.assertIsNone(state.volume_auth)
            self.assertEqual(state.volume_mcp, {})
            state.volume_auth = {"new": {"type": "api", "key": "ephemeral"}}
            state.volume_mcp = {
                "unselected": {"tokens": {"accessToken": "ephemeral"}}
            }
            state.sync_out()
            self.assertFalse(state.host_directory.exists())
            self.assertIsNone(state.volume_auth)
            self.assertEqual(state.volume_mcp, {})

    def test_provider_logout_removes_host_file_and_two_sided_change_conflicts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(root, volume_auth={}, volume_mcp={})
            state.host_directory.mkdir()
            auth_path = state.host_directory / "auth.json"
            auth_path.write_text(
                '{"provider":{"type":"api","key":"one"}}\n', encoding="utf-8"
            )
            state.sync_in()
            state.volume_auth = None
            state.sync_out()
            self.assertFalse(auth_path.exists())

            auth_path.write_text(
                '{"provider":{"type":"api","key":"base"}}\n', encoding="utf-8"
            )
            state.sync_in()
            auth_path.write_text(
                '{"provider":{"type":"api","key":"host"}}\n', encoding="utf-8"
            )
            state.volume_auth = {
                "provider": {"type": "api", "key": "volume"}
            }
            with self.assertRaisesRegex(SyncError, "both host and volume"):
                state.sync_out()

    def test_host_state_rejects_malformed_and_hardlinked_json(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(root, volume_auth={}, volume_mcp={})
            state.host_directory.mkdir()
            auth_path = state.host_directory / "auth.json"
            auth_path.write_text('{"duplicate":1,"duplicate":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(SyncError, "duplicate JSON object key"):
                state.sync_in()
            auth_path.unlink()
            source = root / "source.json"
            source.write_text("{}\n", encoding="utf-8")
            os.link(source, auth_path)
            with self.assertRaisesRegex(SyncError, "hard-linked"):
                state.sync_in()

    def test_rejects_provider_schema_and_mcp_state_for_another_url(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(root, volume_auth={}, volume_mcp={})
            state.host_directory.mkdir()
            auth_path = state.host_directory / "auth.json"
            auth_path.write_text(
                '{"provider":{"type":"unknown","key":"secret"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SyncError, "unsupported provider auth type"):
                state.sync_in()
            auth_path.write_text(
                '{"https://company.example":{"type":"wellknown","key":"token","token":"secret"}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SyncError, "remote managed configuration"):
                state.sync_in()
            auth_path.unlink()
            (state.host_directory / "mcp-auth.json").write_text(
                '{"selected":{"serverUrl":"https://other.example.test","tokens":{"accessToken":"secret"}}}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SyncError, "not tied"):
                state.sync_in()

    def test_reconciliation_lock_rejects_hardlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = self._state(root, volume_auth={}, volume_mcp={})
            descriptor = state._lock()
            os.close(descriptor)
            lock = next(state.config_directory.glob(".opencode-sync-*.lock"))
            lock.unlink()
            source = root / "hardlink-source"
            source.write_text("lock\n", encoding="utf-8")
            os.link(source, lock)
            with self.assertRaisesRegex(SyncError, "unsafe.*lock"):
                state._lock()


if __name__ == "__main__":
    unittest.main()
