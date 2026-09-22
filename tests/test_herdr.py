"""Herdr hints must reach the host agent process without changing global state."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cage_core.models import LaunchRequest, ResolvedConfig
from cage_core.planning import agent_process_environment, build_launch_plan


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def plan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config_root = tmp_path / "config"
    config_root.mkdir()
    return build_launch_plan(
        LaunchRequest(repo_operand=str(repo)),
        ResolvedConfig(
            config_path=config_root / "config.toml",
            repo_path=str(repo),
            preset_name="selected",
            preset_source="flag",
            tool="codex",
            net="open",
        ),
        cage_version="0.37.2",
        config_root=config_root,
        install_root=ROOT,
    ).plan


@pytest.mark.parametrize("target", ["container", "host", "desktop"])
@pytest.mark.parametrize("environment", [
    {},
    {"HERDR_ENV": "0"},
    {"HERDR_ENV": "1"},
    {"HERDR_ENV": "1", "HERDR_AGENT": "claude"},
    {"HERDR_ENV": "1", "HERDR_AGENT": ""},
])
def test_hint_is_scoped_and_preserves_explicit_values(plan, target, environment):
    original = dict(environment)
    result = agent_process_environment(replace(plan, target=target), environment)
    expected = dict(original)
    if target != "desktop" and original == {"HERDR_ENV": "1"}:
        expected["HERDR_AGENT"] = "codex"
    assert result == expected
    assert environment == original


@pytest.mark.parametrize("tool", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("mode", ["exec", "supervised", "oauth"])
def test_container_launch_exposes_hint_on_host_child(tmp_path, tool, mode):
    # Exercise real exec/spawn environments, process groups and exit status in
    # all launch paths, without Docker, credentials or an installed Herdr.
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'hint': os.environ.get('HERDR_AGENT'), "
        "'pgid': os.getpgrp(), 'args': sys.argv[1:]}))\n"
        "sys.exit(7)\n"
    )
    docker.chmod(0o755)
    program = """
import os, sys
from types import SimpleNamespace
from cage_core.lifecycle import LifecycleCoordinator
from cage_core.targets.container import _run_ordinary

tool, mode, docker = sys.argv[1:]
lifecycle = LifecycleCoordinator()
if mode != 'exec':
    lifecycle.register('fixture cleanup', lambda: 0)
runtime = SimpleNamespace(
    docker=docker,
    plan=SimpleNamespace(tool=tool, target='container', image='fixture-image'),
    prepared=SimpleNamespace(request=SimpleNamespace(tool_arguments=('--resume',))),
    lifecycle=lifecycle,
    oauth_connection=SimpleNamespace(poll=lambda: None) if mode == 'oauth' else None,
)
sys.exit(_run_ordinary(runtime, ['--rm', '-it'], []))
"""
    environment = dict(os.environ, HERDR_ENV="1")
    environment.pop("HERDR_AGENT", None)
    result = subprocess.run(
        [sys.executable, "-c", program, tool, mode, str(docker)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 7, result.stderr
    observed = json.loads(result.stdout)
    assert observed["hint"] == tool
    assert observed["pgid"] == os.getpgrp()
    assert observed["args"] == ["run", "--rm", "-it", "fixture-image", "--resume"]
