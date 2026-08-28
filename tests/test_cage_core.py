"""Behavioral contracts for the modular Python host launcher."""

from __future__ import annotations

import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cage_core.lifecycle import (
    LifecycleCoordinator,
    terminate_process,
    wait_for_line,
)
from cage_core.models import (
    ContractError,
    LaunchPlan,
    LaunchRequest,
    ResolvedConfig,
)
from cage_core.planning import PlanError, build_launch_plan


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"


class LaunchPlanContractTests(unittest.TestCase):
    def make_plan(self, root: Path) -> LaunchPlan:
        repo = root / "repo"
        config_root = root / "config"
        repo.mkdir()
        config_root.mkdir()
        resolved = ResolvedConfig(
            config_path=config_root / "config.toml",
            repo_path=str(repo),
            preset_name="main",
            preset_source="flag",
            tool="codex",
            target="container",
            aws_profile="aws-staging.ReadOnly",
            aws_access="host-cli",
            extra_env=["TOKEN_NAME"],
            stdio_mcp=[
                {
                    "name": "local",
                    "command": "tool --token secret-command-value",
                    "env": {"TOKEN_NAME": "secret-env-value"},
                }
            ],
            remote_mcp=[
                {
                    "name": "remote",
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "secret-header-value"},
                }
            ],
            host_commands=[
                {
                    "name": "token-tool",
                    "command": "token-tool secret-command-value",
                }
            ],
        )
        prepared = build_launch_plan(
            LaunchRequest(
                repo_operand=str(repo),
                tool_arguments=("secret-prompt-value", "--another"),
            ),
            resolved,
            cage_version="0.26.4",
            config_root=config_root,
            install_root=ROOT,
        )
        return prepared.plan

    def test_public_contract_is_versioned_and_redacts_runtime_values(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self.make_plan(Path(raw))
            payload = plan.public_dict()
            serialized = json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["schema"], "cage.launch-plan")
        self.assertEqual(payload["schema_version"], 3)
        self.assertIn("aws-host-cli", payload["selected_capabilities"]["runtime"])
        self.assertEqual(
            payload["storage"],
            {
                "warn_free_gib": 20,
                "critical_free_gib": 5,
                "min_build_free_gib": 20,
                "keep_versions": 2,
                "dangling_min_age_hours": 24,
                "ephemeral_min_age_hours": 168,
            },
        )
        self.assertEqual(
            payload["execution"]["passthrough_argument_count"],
            2,
        )
        for forbidden in (
            "secret-prompt-value",
            "secret-command-value",
            "secret-env-value",
            "secret-header-value",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("TOKEN_NAME", serialized)
        self.assertEqual(
            LaunchPlan.validate_public_dict(payload),
            payload,
        )

    def test_public_contract_rejects_unknown_top_level_and_nested_fields(self):
        with tempfile.TemporaryDirectory() as raw:
            payload = self.make_plan(Path(raw)).public_dict()
        payload["secret"] = "unexpected"
        with self.assertRaisesRegex(ContractError, "unknown launch-plan fields"):
            LaunchPlan.validate_public_dict(payload)
        del payload["secret"]
        payload["execution"]["unknown"] = True
        with self.assertRaisesRegex(
            ContractError, "unknown launch-plan execution fields"
        ):
            LaunchPlan.validate_public_dict(payload)
        del payload["execution"]["unknown"]
        payload["mounts"][0]["unknown"] = True
        with self.assertRaisesRegex(
            ContractError, "unknown launch-plan mount fields"
        ):
            LaunchPlan.validate_public_dict(payload)

    def test_runtime_inputs_are_frozen_and_return_definition_copies(self):
        with tempfile.TemporaryDirectory() as raw:
            plan = self.make_plan(Path(raw))
        with self.assertRaises(FrozenInstanceError):
            plan.runtime_config.git_user_name = "replacement"
        first = plan.runtime_config.stdio_mcp
        first[0]["name"] = "mutated"
        self.assertEqual(plan.runtime_config.stdio_mcp[0]["name"], "local")

    def test_profile_pinned_aws_host_cli_requires_network(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            config_root = root / "config"
            repo.mkdir()
            config_root.mkdir()
            resolved = ResolvedConfig(
                config_path=config_root / "config.toml",
                repo_path=str(repo),
                preset_name="aws",
                preset_source="test",
                tool="codex",
                target="container",
                net="off",
                aws_profile="aws-prod.ReadOnly",
                aws_access="host-cli",
            )
            with self.assertRaisesRegex(PlanError, "--net off"):
                build_launch_plan(
                    LaunchRequest(repo_operand=str(repo)),
                    resolved,
                    cage_version="0.30.2",
                    config_root=config_root,
                    install_root=ROOT,
                )


class LifecycleCoordinatorTests(unittest.TestCase):
    def test_cleanup_is_reverse_order_and_primary_failure_wins(self):
        observed: list[str] = []
        lifecycle = LifecycleCoordinator()
        lifecycle.register("first", lambda: observed.append("first") or 7)
        lifecycle.register("second", lambda: observed.append("second") or 8)
        self.assertEqual(lifecycle.cleanup(23), 23)
        self.assertEqual(observed, ["second", "first"])
        self.assertEqual(lifecycle.cleanup(23), 23)

    def test_first_cleanup_failure_is_returned_without_primary_failure(self):
        lifecycle = LifecycleCoordinator()
        lifecycle.register("oldest", lambda: 9)
        lifecycle.register("newest", lambda: 4)
        self.assertEqual(lifecycle.cleanup(), 4)

    def test_readiness_stops_on_process_failure_and_times_out(self):
        process = Mock()
        process.poll.return_value = 17
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "readiness"
            self.assertFalse(
                wait_for_line(
                    path,
                    "READY",
                    process,
                    timeout_seconds=1,
                    interval_seconds=0.001,
                )
            )
            process.poll.return_value = None
            self.assertFalse(
                wait_for_line(
                    path,
                    "READY",
                    process,
                    timeout_seconds=0.01,
                    interval_seconds=0.001,
                )
            )
            path.write_text("PORT=123\nREADY\n", encoding="utf-8")
            self.assertTrue(
                wait_for_line(
                    path,
                    "READY",
                    process,
                    timeout_seconds=1,
                    interval_seconds=0.001,
                )
            )

    def test_process_shutdown_uses_term_grace_then_kill(self):
        process = Mock()
        process.pid = 1234
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("process", 0.01),
            0,
        ]
        with patch("cage_core.lifecycle.os.killpg") as killpg:
            terminate_process(process, grace_seconds=0.01)
        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(1234, signal.SIGTERM),
                unittest.mock.call(1234, signal.SIGKILL),
            ],
        )

    def test_desktop_dependency_loss_returns_fail_closed_status(self):
        from cage_core.targets.desktop import run_desktop

        class Runtime:
            plan = SimpleNamespace(
                image="codex:test",
                volume_name="desktop-volume",
            )
            install_root = ROOT
            config_root = ROOT
            container_name = "desktop-container"
            lifecycle = LifecycleCoordinator()
            dependency_processes = [
                ("MCP bridge", SimpleNamespace(poll=lambda: 1))
            ]

            @staticmethod
            def run(arguments, **_kwargs):
                if arguments[0] == "run":
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout="container-id\n", stderr=""
                    )
                return subprocess.CompletedProcess(arguments, 0, stdout="")

            @staticmethod
            def output(_arguments, **_kwargs):
                return "true"

        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / "secret"
            secret.write_text("{}", encoding="utf-8")
            with (
                patch(
                    "cage_core.targets.desktop._write_runtime_environment",
                    return_value=([], secret),
                ),
                patch(
                    "cage_core.targets.desktop.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                patch("cage_core.targets.desktop._unlink", return_value=0),
                patch("cage_core.targets.desktop.time.sleep"),
                patch.dict(
                    os.environ,
                    {
                        "CAGE_DESKTOP_TARGET_ID": "target",
                        "CAGE_DESKTOP_FINGERPRINT": "0" * 64,
                    },
                ),
            ):
                self.assertEqual(run_desktop(Runtime(), []), 70)


class IsolatedBootstrapTests(unittest.TestCase):
    def test_python_311_is_rejected_before_core_execution(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            root = Path(raw)
            binary = root / "bin"
            binary.mkdir()
            python = binary / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -I ] && [ \"$2\" = -c ]; then\n"
                "  case \"$3\" in\n"
                "    *'sys.version_info >= (3, 12)'*) exit 1 ;;\n"
                "    *) printf 'unexpected Python version probe\\n' >&2; exit 97 ;;\n"
                "  esac\n"
                "fi\n"
                "printf 'core executed unexpectedly\\n' >&2\n"
                "exit 98\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{binary}{os.pathsep}{environment['PATH']}"
            result = subprocess.run(
                [str(CAGE), "--version"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires Python 3.12 or newer", result.stderr)
        self.assertNotIn("unexpected Python version probe", result.stderr)
        self.assertNotIn("core executed unexpectedly", result.stderr)

    def test_python_entrypoint_independently_rejects_python_311(self):
        code = (
            "import runpy, sys; "
            "sys.version_info = (3, 11, 15); "
            f"runpy.run_path({str(ROOT / 'cage-main.py')!r}, run_name='cage_main_test')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires Python 3.12 or newer", result.stderr)

    def test_hostile_cwd_and_pythonpath_cannot_shadow_core(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            root = Path(raw)
            hostile = root / "hostile"
            package = hostile / "cage_core"
            package.mkdir(parents=True)
            sentinel = root / "imported"
            (package / "__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).touch()\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(hostile)
            result = subprocess.run(
                [str(CAGE), "--version"],
                cwd=hostile,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "cage 0.34.1")
        self.assertFalse(sentinel.exists())

    def test_symlinked_core_package_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            root = Path(raw)
            install = root / "install"
            install.mkdir()
            shutil.copy2(CAGE, install / "cage")
            shutil.copy2(ROOT / "cage-main.py", install / "cage-main.py")
            (install / "cage_core").symlink_to(ROOT / "cage_core")
            result = subprocess.run(
                [str(install / "cage"), "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("core package path is unsafe", result.stderr)

    def test_resolve_json_has_no_launch_side_effects_or_raw_prompt(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            root = Path(raw)
            home = root / "home"
            xdg = root / "xdg"
            repo = root / "repo"
            binary = root / "bin"
            for path in (home, xdg / "cage", repo, binary):
                path.mkdir(parents=True)
            sentinel = root / "docker-called"
            docker = binary / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                f"touch {str(sentinel)!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            docker.chmod(
                docker.stat().st_mode
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
            (xdg / "cage" / "config.toml").write_text(
                "version = 1\n"
                'default_preset = "main"\n'
                "[presets.main]\n"
                'tool = "codex"\n'
                'net = "open"\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                HOME=str(home),
                XDG_CONFIG_HOME=str(xdg),
                PATH=f"{binary}{os.pathsep}{environment['PATH']}",
            )
            result = subprocess.run(
                [
                    str(CAGE),
                    "resolve-json",
                    str(repo),
                    "private-prompt-value",
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["execution"]["passthrough_argument_count"],
            1,
        )
        self.assertNotIn("private-prompt-value", result.stdout)
        self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
