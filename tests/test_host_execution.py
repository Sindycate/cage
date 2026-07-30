"""Adversarial regression tests for host-native Codex CLI execution target.

Covers:
- Host mode invokes pinned fake Codex without Docker
- CODEX_HOME, cwd, and forwarded arguments are correct
- Claude host mode is rejected
- Selected Codex profiles, MCP packs, and default-registry skill packs are
  translated into process-local host arguments
- Remaining container-only capabilities fail closed in host mode
- host+yolo never implicitly opens networking (gate rejection)
- --container overrides a saved host target
- --host and --container conflict is rejected
- TUI summaries and risk review state Docker isolation is absent
- TUI preflight surfaces all incompatibilities
- Selected identity is process-scoped (env vars, not config mutation)
- Custom host_agents_dir is rejected in host mode
- Repository-controlled fake codex earlier in PATH is rejected
- Cancellation remains a no-op (no Docker, no Codex)
- Config schema validates target field
- Existing container launches remain unchanged
"""

import curses
import fcntl
import importlib.util
import json
import os
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"

SPEC = importlib.util.spec_from_file_location("cage_config", ROOT / "cage-config.py")
cage_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = cage_config
SPEC.loader.exec_module(cage_config)

TUI_SPEC = importlib.util.spec_from_file_location("cage_tui", ROOT / "cage-tui.py")
cage_tui = importlib.util.module_from_spec(TUI_SPEC)
assert TUI_SPEC.loader is not None
sys.modules[TUI_SPEC.name] = cage_tui
TUI_SPEC.loader.exec_module(cage_tui)


class FakeScreen:
    """Minimal fake curses screen for TUI tests."""
    def __init__(self, keys=(), height=24, width=80):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.writes = []
        self.moves = []
    def keypad(self, _): pass
    def erase(self): pass
    def getmaxyx(self): return self.height, self.width
    def addnstr(self, row, col, text, count, attr=None):
        self.writes.append((row, col, str(text)[:count], attr))
    def move(self, row, col): self.moves.append((row, col))
    def clrtoeol(self): pass
    def refresh(self): pass
    def getch(self):
        if not self.keys: raise AssertionError("no keys left")
        return self.keys.pop(0)
    def get_wch(self): return self.getch()


def write_fake_docker_failing(path: Path) -> None:
    """Docker that fails loudly if called — proves Docker was never invoked."""
    path.write_text(
        '#!/bin/sh\necho "FAKE_DOCKER_CALLED $*" >&2\nexit 1\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_fake_docker_success(path: Path) -> None:
    """Docker that succeeds (for container-mode tests)."""
    path.write_text(
        '#!/bin/sh\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    type=bind,src=*,dst=/out) out=${arg#type=bind,src=}; out=${out%,dst=/out}; '
        'printf \'%s\\n\' \'{"credentials":{"exists":false,"raw_sha256":null,"mode":null},'
        '"state":{"exists":false,"raw_sha256":null,"mode":null}}\' > "$out/manifest.json"; exit 0 ;;\n'
        '  esac\n'
        'done\n'
        'case "$1" in\n'
        '  ps) exit 0 ;;\n'
        '  image) exit 0 ;;\n'
        '  run) echo "DOCKER_RUN $*"; exit 0 ;;\n'
        '  build|pull|tag|volume) exit 0 ;;\n'
        'esac\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_fake_codex(path: Path) -> None:
    """Codex that reports its environment for assertion.

    Also answers the launch-time MCP inventory query (`mcp list --json`) with
    a controllable inventory so the authoritative MCP selection logic can run
    during resolve. Defaults to an empty inventory; tests inject inherited
    servers through CAGE_FAKE_CODEX_MCP_LIST_JSON.
    """
    path.write_text(
        '#!/bin/sh\n'
        'for _cage_arg in "$@"; do\n'
        '  if [ "$_cage_arg" = "list" ]; then\n'
        '    printf \'%s\\n\' "${CAGE_FAKE_CODEX_MCP_LIST_JSON:-[]}"\n'
        '    exit 0\n'
        '  fi\n'
        'done\n'
        'echo "CODEX_HOME=$CODEX_HOME"\n'
        'echo "CWD=$(pwd)"\n'
        'echo "ARGS=$*"\n'
        'i=0\n'
        'for arg in "$@"; do echo "ARG_${i}=$arg"; i=$((i + 1)); done\n'
        'echo "GIT_CONFIG_COUNT=${GIT_CONFIG_COUNT:-unset}"\n'
        'echo "GIT_CONFIG_KEY_0=${GIT_CONFIG_KEY_0:-unset}"\n'
        'echo "GIT_CONFIG_VALUE_0=${GIT_CONFIG_VALUE_0:-unset}"\n'
        'echo "GIT_CONFIG_KEY_1=${GIT_CONFIG_KEY_1:-unset}"\n'
        'echo "GIT_CONFIG_VALUE_1=${GIT_CONFIG_VALUE_1:-unset}"\n'
        'echo "GIT_CONFIG_KEY_2=${GIT_CONFIG_KEY_2:-unset}"\n'
        'echo "GIT_CONFIG_VALUE_2=${GIT_CONFIG_VALUE_2:-unset}"\n'
        'echo "GIT_USER_NAME=$(git config user.name 2>/dev/null || true)"\n'
        'echo "GIT_USER_EMAIL=$(git config user.email 2>/dev/null || true)"\n'
        'echo "GIT_SSH_COMMAND=${GIT_SSH_COMMAND:-unset}"\n'
        'echo "CAGE_SELECTED_SSH_KEY=${CAGE_SELECTED_SSH_KEY:-unset}"\n'
        'echo "GH_TOKEN=${GH_TOKEN:-unset}"\n'
        'exit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def make_env(tmp_path: Path, bin_dir: Path, home: Path, xdg: Path) -> dict:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(xdg)
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # Remove ambient tokens that could leak into tests
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def setup_host_test(
    config_text: str,
    extra_args: list[str] | None = None,
    *,
    tool_args: list[str] | None = None,
    fake_gh: str = "",
    env_overrides: dict[str, str] | None = None,
    host_skills: dict[str, str] | None = None,
    codex_files: dict[str, str] | None = None,
):
    """Set up a complete isolated environment for a cage launch test."""
    temporary = tempfile.TemporaryDirectory(dir=ROOT)
    tmp_path = Path(temporary.name)
    xdg = tmp_path / "xdg"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    cage_dir = xdg / "cage"
    repo = tmp_path / "repo"
    bin_dir.mkdir(parents=True)
    cage_dir.mkdir(parents=True)
    home.mkdir(parents=True)
    repo.mkdir()
    write_fake_docker_failing(bin_dir / "docker")
    write_fake_codex(bin_dir / "codex")
    for name, body in (host_skills or {}).items():
        skill_dir = home / ".agents" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    for name, body in (codex_files or {}).items():
        codex_file = home / ".codex" / name
        codex_file.parent.mkdir(parents=True, exist_ok=True)
        codex_file.write_text(body, encoding="utf-8")
    if fake_gh:
        gh_body = (
            '#!/bin/sh\nprintf "fake-token:%s\\n" "$*"\n'
            if fake_gh == "token"
            else "#!/bin/sh\nexit 1\n"
        )
        (bin_dir / "gh").write_text(gh_body, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
    (cage_dir / "config.toml").write_text(config_text, encoding="utf-8")
    env = make_env(tmp_path, bin_dir, home, xdg)
    env.update(env_overrides or {})
    args = [str(CAGE)] + (extra_args or []) + [str(repo)] + (tool_args or [])
    result = subprocess.run(
        args, cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return result, repo, tmp_path, temporary


HOST_CONFIG = '\n'.join([
    "version = 1",
    'default_preset = "main"',
    "[presets.main]",
    'tool = "codex"',
    'target = "host"',
    'net = "open"',
    "",
])


class TestHostModeLaunches(unittest.TestCase):
    """Host mode invokes pinned Codex without Docker."""

    def test_host_mode_invokes_codex_without_docker(self):
        result, repo, _, tmp = self._launch(HOST_CONFIG)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_HOME=", result.stdout)
        self.assertNotIn("FAKE_DOCKER_CALLED", result.stderr)
        self.assertIn("no Docker isolation", result.stderr)

    def test_host_mode_rejects_passthrough_mcp_config(self):
        result, _, _, tmp = setup_host_test(
            HOST_CONFIG,
            tool_args=["--config=mcp_servers.injected.command=\"echo\""],
        )
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not override mcp_servers", result.stderr)
        self.assertNotIn("CODEX_HOME=", result.stdout)

    def test_host_mode_rejects_inventory_shaping_passthrough_arguments(self):
        attempts = [
            ["-p", "evil"],
            ["--profile=evil"],
            ["-pevil"],
            ["-C", "/other"],
            ["--cd=/other"],
            ["-C/other"],
            ["--enable", "plugins"],
            ["--disable=plugins"],
            ["--remote", "ws://other.example"],
            ["--remote-auth-token-env=TOKEN"],
            ["exec", "--ignore-user-config"],
            ["-c", "features.plugins=true"],
            ["--config=plugins.example.enabled=true"],
            ["-c", "unknown_future_layer=true"],
        ]
        for tool_args in attempts:
            with self.subTest(tool_args=tool_args):
                result, _, _, temporary = setup_host_test(
                    HOST_CONFIG,
                    tool_args=tool_args,
                )
                self.addCleanup(temporary.cleanup)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("CODEX_HOME=", result.stdout)

    def test_host_mode_allows_safe_arguments_and_delimited_payload(self):
        result, _, _, temporary = setup_host_test(
            HOST_CONFIG,
            tool_args=[
                "prompt text",
                "--model",
                "o3",
                "-c",
                'sandbox_mode="read-only"',
                "--",
                "-p",
                "--profile=prompt-text",
            ],
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'ARGS=prompt text --model o3 -c sandbox_mode="read-only" -- -p '
            "--profile=prompt-text",
            result.stdout,
        )

    def test_host_profile_allows_delimited_payload_that_looks_like_profile(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'codex_profile = "saved"', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            tool_args=["--", "-p", "--profile=prompt-text"],
            codex_files={"saved.config.toml": ""},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "ARGS=--profile saved -- -p --profile=prompt-text",
            result.stdout,
        )

    def test_host_flag_overrides_container_preset(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ])
        result, _, _, tmp = self._launch(config, ["--host"])
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_HOME=", result.stdout)
        self.assertNotIn("FAKE_DOCKER_CALLED", result.stderr)

    def test_container_flag_overrides_saved_host_preset(self):
        """--container forces container execution for a saved host preset."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"', 'net = "open"', "",
        ])
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        write_fake_docker_success(bin_dir / "docker")
        write_fake_codex(bin_dir / "codex")
        (cage_dir / "config.toml").write_text(config, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        result = subprocess.run(
            [str(CAGE), "--container", str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DOCKER_RUN", result.stdout)
        self.assertNotIn("CODEX_HOME=", result.stdout)

    def test_host_and_container_flags_conflict(self):
        result, _, _, tmp = self._launch(HOST_CONFIG, ["--host", "--container"])
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)

    def test_codex_home_cwd_and_args(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[auth.myauth]", 'tool = "codex"',
            'host_codex_dir = "/tmp/test-codex-XXXXX"',
            "[presets.main]", 'tool = "codex"', 'auth = "myauth"',
            'target = "host"', 'net = "open"', "",
        ])
        result, repo, _, tmp = self._launch(config)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODEX_HOME=/tmp/test-codex-XXXXX", result.stdout)
        self.assertIn(f"CWD={repo}", result.stdout)

    def test_arguments_forwarded(self):
        result, repo, _, tmp = self._launch(HOST_CONFIG)
        self.addCleanup(tmp.cleanup)
        # Re-run with tool args after repo path
        temporary2 = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary2.cleanup)
        # Use the same setup but with args
        tmp2 = Path(temporary2.name)
        xdg, home, bin_dir = tmp2 / "xdg", tmp2 / "home", tmp2 / "bin"
        cage_dir, repo2 = xdg / "cage", tmp2 / "repo"
        for d in (bin_dir, cage_dir, home, repo2):
            d.mkdir(parents=True)
        write_fake_docker_failing(bin_dir / "docker")
        write_fake_codex(bin_dir / "codex")
        (cage_dir / "config.toml").write_text(HOST_CONFIG, encoding="utf-8")
        env = make_env(tmp2, bin_dir, home, xdg)
        r = subprocess.run(
            [str(CAGE), str(repo2), "do something", "--model", "o3"],
            cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS=do something --model o3", r.stdout)

    def test_named_codex_profile_is_forwarded_in_host_mode(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'codex_profile = "provider-preview"', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            codex_files={"provider-preview.config.toml": 'model = "provider-model"\n'},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARG_0=--profile", result.stdout)
        self.assertIn("ARG_1=provider-preview", result.stdout)
        self.assertIn("Profile:    provider-preview", result.stderr)

    def test_missing_named_codex_profile_fails_closed(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'codex_profile = "missing"', "",
        ])
        result, _, _, temporary = setup_host_test(config)
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selected Codex profile is missing", result.stderr)

    def test_preset_and_explicit_codex_profiles_conflict(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'codex_profile = "saved"', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            tool_args=["--profile", "other"],
            codex_files={"saved.config.toml": ""},
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may not override the profile", result.stderr)

    def test_yolo_forwarded_with_explicit_net_open(self):
        """Yolo is forwarded when net is explicitly open."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', "yolo = true", "",
        ])
        result, _, _, tmp = self._launch(config)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARGS=--yolo", result.stdout)

    def test_container_mode_unchanged(self):
        """Default container mode still invokes docker."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ])
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        write_fake_docker_success(bin_dir / "docker")
        (cage_dir / "config.toml").write_text(config, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        result = subprocess.run(
            [str(CAGE), str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DOCKER_RUN", result.stdout)

    def test_named_codex_profile_is_forwarded_in_container_mode(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"',
            'codex_profile = "company"', "",
        ])
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for directory in (bin_dir, cage_dir, home, repo, home / ".codex"):
            directory.mkdir(parents=True, exist_ok=True)
        write_fake_docker_success(bin_dir / "docker")
        (home / ".codex" / "company.config.toml").write_text('model = "gpt-5"\n')
        (cage_dir / "config.toml").write_text(config, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        result = subprocess.run(
            [str(CAGE), str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CAGE_CODEX_PROFILE=company", result.stdout)
        self.assertNotIn("--profile company", result.stdout)

    def test_missing_named_codex_profile_fails_before_container_side_effects(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"',
            'codex_profile = "missing"', "",
        ])
        result, _, _, temporary = setup_host_test(config)
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selected Codex profile is missing", result.stderr)
        self.assertNotIn("FAKE_DOCKER_CALLED", result.stderr)

    def test_container_profile_mcp_duplicate_fails_before_docker(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.linear]",
            'servers = [{ name = "linear", type = "http", url = "https://mcp.linear.app/mcp" }]',
            "[presets.main]", 'tool = "codex"', 'net = "open"',
            'codex_profile = "company"', 'mcp_packs = ["linear"]', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            codex_files={
                "company.config.toml":
                    '[mcp_servers.linear]\nurl = "https://other.example/mcp"\n'
            },
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exist in Codex config layer", result.stderr)
        self.assertNotIn("FAKE_DOCKER_CALLED", result.stderr)

    def _launch(self, config, extra_args=None):
        return setup_host_test(config, extra_args)


class TestHostYoloNetworkDivergence(unittest.TestCase):
    """host+yolo must never implicitly turn gate into open."""

    def test_yolo_defaults_to_gate_and_host_rejects_it(self):
        """Yolo without explicit net defaults to gate; host rejects gate."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            "yolo = true", "",
        ])
        result, _, _, tmp = setup_host_test(config)
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot enforce network mode", result.stderr)
        self.assertIn("--net open", result.stderr)

    def test_yolo_with_cli_host_and_no_net_rejects(self):
        """--host + saved yolo (no explicit net) rejects, not silently opens."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', "yolo = true", "",
        ])
        result, _, _, tmp = setup_host_test(config, ["--host"])
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot enforce network mode", result.stderr)

    def test_yolo_host_with_explicit_net_open_succeeds(self):
        """Explicit --net open acknowledges unrestricted networking."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', "yolo = true", "",
        ])
        result, _, _, tmp = setup_host_test(config, ["--host", "--net", "open"])
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ARGS=--yolo", result.stdout)
        self.assertIn("no Docker isolation", result.stderr)


class TestHostCapabilities(unittest.TestCase):
    """Supported host capabilities are scoped; unsupported ones fail closed."""

    def _assert_rejected(self, config, expected_fragment):
        result, _, _, tmp = setup_host_test(config)
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn(expected_fragment, combined)

    def test_claude_host_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "claude"', 'target = "host"', "",
        ])
        self._assert_rejected(config, "only supported for Codex")

    def test_claude_host_flag_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "claude"', "",
        ])
        result, _, _, tmp = setup_host_test(config, ["--host"])
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only supported for Codex", result.stderr)

    def test_stdio_mcp_is_applied_with_pinned_executable_and_argument_boundaries(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.local]",
            'env = ["JIRA_TOKEN"]',
            'servers = [{ name = "jira", type = "stdio", command = "codex --serve \\"two words\\"" }]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["local"]', "",
        ])
        result, _, tmp_path, temporary = setup_host_test(
            config,
            env_overrides={"JIRA_TOKEN": "secret"},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f'ARG_1=mcp_servers.jira.command="{tmp_path / "bin" / "codex"}"',
            result.stdout,
        )
        self.assertIn('ARG_3=mcp_servers.jira.args=["--serve","two words"]', result.stdout)
        self.assertIn('ARG_5=mcp_servers.jira.env_vars=["JIRA_TOKEN"]', result.stdout)
        self.assertIn("selected packs applied for this process", result.stderr)

    def test_remote_mcp_is_applied_process_locally(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.linear]", 'env = ["LINEAR_API_KEY"]',
            'servers = [{ name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" }]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["linear"]', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            env_overrides={"LINEAR_API_KEY": "secret"},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'mcp_servers.linear.url="https://mcp.linear.app/mcp"',
            result.stdout,
        )
        self.assertIn(
            'mcp_servers.linear.bearer_token_env_var="LINEAR_API_KEY"',
            result.stdout,
        )

    def test_duplicate_base_config_mcp_server_is_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.linear]",
            'servers = [{ name = "linear", type = "http", url = "https://mcp.linear.app/mcp" }]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["linear"]', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            codex_files={
                "config.toml": '[mcp_servers.linear]\nurl = "https://other.example/mcp"\n'
            },
        )
        self.addCleanup(temporary.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exist in Codex config layer", result.stderr)

    def test_oauth_mcp_resolves_public_client_id_without_persisting_it(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.dash0]",
            "servers = [",
            '  { name = "dash0", type = "http", url = "https://example.test/mcp", '
            'auth = "oauth", oauth_resource = "https://example.test", '
            'oauth_client_id_env_var = "DASH0_CLIENT_ID", oauth_scopes = ["read"] },',
            "]",
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["dash0"]', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            env_overrides={"DASH0_CLIENT_ID": "public-client-id"},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mcp_servers.dash0.oauth_resource="https://example.test"', result.stdout)
        self.assertIn('mcp_servers.dash0.scopes=["read"]', result.stdout)
        self.assertIn('mcp_servers.dash0.oauth.client_id="public-client-id"', result.stdout)
        self.assertIn('mcp_oauth_credentials_store="file"', result.stdout)
        self.assertNotIn("mcp_servers.dash0.auth", result.stdout)

    def test_host_commands_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[host_commands.ztoken]", 'command = "ztoken"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'host_commands = ["ztoken"]', "",
        ])
        self._assert_rejected(config, "container execution")

    def test_extra_mounts_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'extra_mounts = ["/tmp"]', "",
        ])
        self._assert_rejected(config, "container execution")

    def test_skill_packs_filter_default_host_registry_process_locally(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[skill_packs.sp]", 'source = "~/.agents"', 'skills = ["myskill"]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'skill_packs = ["sp"]', "",
        ])
        result, _, _, temporary = setup_host_test(
            config,
            host_skills={"myskill": "# Selected", "other": "# Other"},
        )
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skills.config=[", result.stdout)
        self.assertIn("myskill/SKILL.md\",enabled=true", result.stdout)
        self.assertIn("other/SKILL.md\",enabled=false", result.stdout)
        self.assertIn("selected pack filter applied", result.stderr)

    def test_skill_pack_outside_default_registry_rejected(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        skill_dir = tmp_path / "agents" / "skills" / "myskill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill")
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[skill_packs.sp]", f'source = "{tmp_path / "agents"}"',
            'skills = ["myskill"]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'skill_packs = ["sp"]', "",
        ])
        result, _, _, _ = setup_host_test(config)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("default host registry", result.stdout + result.stderr)

    def test_net_gate_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "gate"', "",
        ])
        self._assert_rejected(config, "cannot enforce network mode")

    def test_net_off_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "off"', "",
        ])
        self._assert_rejected(config, "cannot enforce network mode")

    def test_custom_host_agents_dir_rejected(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[auth.myauth]", 'tool = "codex"',
            'host_agents_dir = "/custom/agents"',
            "[presets.main]", 'tool = "codex"', 'auth = "myauth"',
            'target = "host"', 'net = "open"', "",
        ])
        self._assert_rejected(config, "host_agents_dir")


class TestHostIdentityProcessScoped(unittest.TestCase):
    """Git identity, SSH, GH auth are process-scoped env vars."""

    def test_git_identity_forwarded_via_git_config_env(self):
        """Git identity uses GIT_CONFIG_COUNT/KEY/VALUE for full parity."""
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            'git_user_name = "Test User"',
            'git_user_email = "test@example.com"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(config)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GIT_CONFIG_COUNT=2", result.stdout)
        self.assertIn("GIT_CONFIG_KEY_0=user.name", result.stdout)
        self.assertIn("GIT_CONFIG_VALUE_0=Test User", result.stdout)
        self.assertIn("GIT_CONFIG_KEY_1=user.email", result.stdout)
        self.assertIn("GIT_CONFIG_VALUE_1=test@example.com", result.stdout)
        self.assertIn("GIT_USER_NAME=Test User", result.stdout)
        self.assertIn("GIT_USER_EMAIL=test@example.com", result.stdout)

    def test_git_identity_appends_inherited_process_config(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            'git_user_name = "Test User"',
            'git_user_email = "test@example.com"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(
            config,
            env_overrides={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.editor",
                "GIT_CONFIG_VALUE_0": "true",
            },
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GIT_CONFIG_COUNT=3", result.stdout)
        self.assertIn("GIT_CONFIG_KEY_0=core.editor", result.stdout)
        self.assertIn("GIT_CONFIG_KEY_1=user.name", result.stdout)
        self.assertIn("GIT_CONFIG_KEY_2=user.email", result.stdout)

    def test_ssh_key_forwarded_as_git_ssh_command(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        key_dir = tmp_path / "keys with spaces"
        key_dir.mkdir()
        ssh_key = key_dir / "id_$(not-a-command)"
        ssh_key.write_text("fake key")
        ssh_key.chmod(0o600)
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            f'ssh_key = "{ssh_key}"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, _ = setup_host_test(config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GIT_SSH_COMMAND=ssh -i "$CAGE_SELECTED_SSH_KEY"', result.stdout)
        self.assertIn(f"CAGE_SELECTED_SSH_KEY={ssh_key}", result.stdout)
        self.assertIn("IdentitiesOnly=yes", result.stdout)

    def test_ssh_key_missing_fails_closed(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            'ssh_key = "/nonexistent/key"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(config)
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH key does not exist", result.stderr)

    def test_ssh_host_rejected_in_host_mode(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            'ssh_host = "myhost"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(config)
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ssh_host", result.stderr)

    def test_github_account_token_is_forwarded(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            "gh_auth = true",
            'gh_account = "work-account"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(config, fake_gh="token")
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GH_TOKEN=fake-token:auth token -u work-account", result.stdout)

    def test_requested_github_auth_fails_closed_without_token(self):
        config = '\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]",
            "gh_auth = true",
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ])
        result, _, _, tmp = setup_host_test(config, fake_gh="fail")
        self.addCleanup(tmp.cleanup)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub authentication was requested", result.stderr)


class TestCodexExecutablePinning(unittest.TestCase):
    """Repository-controlled fake codex earlier in PATH is rejected."""

    def test_codex_inside_repo_rejected(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        write_fake_docker_failing(bin_dir / "docker")
        # Put a fake codex INSIDE the repo
        repo_codex = repo / "codex"
        repo_codex.write_text("#!/bin/sh\necho EVIL\n")
        repo_codex.chmod(0o755)
        # Put real fake codex in bin_dir too, but repo is earlier in PATH
        write_fake_codex(bin_dir / "codex")
        (cage_dir / "config.toml").write_text(HOST_CONFIG, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        # Put repo first in PATH so its codex would be found first
        env["PATH"] = f"{repo}{os.pathsep}{env['PATH']}"
        result = subprocess.run(
            [str(CAGE), str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cage-writable", result.stderr)
        self.assertNotIn("EVIL", result.stdout)

    def test_missing_codex_rejected(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        write_fake_docker_failing(bin_dir / "docker")
        # No codex in bin_dir — but keep system PATH for basic tools
        (cage_dir / "config.toml").write_text(HOST_CONFIG, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        # Filter out any directory containing a real codex binary
        import shutil as _shutil
        system_paths = [
            p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and not (Path(p) / "codex").exists()
        ]
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.pathsep.join(system_paths)}"
        result = subprocess.run(
            [str(CAGE), str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found in PATH", result.stderr)


class TestConfigSchema(unittest.TestCase):
    """Config schema validates target field."""

    def test_default_target_is_container(self):
        data = {"version": 1, "default_preset": "m", "presets": {"m": {"tool": "codex"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        self.assertEqual(resolved.target, "container")

    def test_host_target_resolves(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "target": "host"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        self.assertEqual(resolved.target, "host")

    def test_invalid_target_rejected(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "target": "remote"}}}
        with self.assertRaises(cage_config.ConfigError) as ctx:
            cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        self.assertIn("target", str(ctx.exception))

    def test_claude_host_rejected_in_resolver(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "claude", "target": "host"}}}
        with self.assertRaises(cage_config.ConfigError) as ctx:
            cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        self.assertIn("only supported for Codex", str(ctx.exception))

    def test_schema_validates_unused_preset_target(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex"}, "bad": {"tool": "codex", "target": "x"}}}
        with self.assertRaises(cage_config.ConfigError):
            cage_config.validate_schema(data)

    def test_named_codex_profile_resolves(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "codex_profile": "provider-preview_1"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        self.assertEqual(resolved.codex_profile, "provider-preview_1")

    def test_invalid_named_codex_profile_is_rejected(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "codex_profile": "../escape"}}}
        with self.assertRaisesRegex(cage_config.ConfigError, "codex_profile"):
            cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")

    def test_named_codex_profile_is_rejected_for_claude(self):
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "claude", "codex_profile": "company"}}}
        with self.assertRaisesRegex(cage_config.ConfigError, "only supported for Codex"):
            cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")

    def test_codex_layer_duplicate_check_is_size_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            layer = Path(temporary) / "config.toml"
            layer.write_text("[mcp_servers.x]\nurl='x'\n")
            with (
                patch.object(cage_config, "MAX_CODEX_CONFIG_BYTES", 8),
                self.assertRaisesRegex(cage_config.ConfigError, "exceeds 8 bytes"),
            ):
                cage_config.selected_mcp_names_in_file(layer, {"x"})

    def test_resolved_json_includes_target(self):
        import io
        from unittest.mock import patch
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "target": "host"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cage_config.emit_resolved_json(resolved)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["resolved_config"]["target"], "host")

    def test_explain_shows_host_target_and_network_honesty(self):
        import io
        from unittest.mock import patch
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "target": "host", "net": "open"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cage_config.explain(resolved)
        output = buf.getvalue()
        self.assertIn("host", output)
        self.assertIn("no docker isolation", output.lower())
        self.assertIn("unrestricted host networking", output.lower())

    def test_doctor_host_yolo_no_net_fails(self):
        """target=host + yolo=true + no explicit net → doctor returns nonzero."""
        import io
        from unittest.mock import patch
        data = {"version": 1, "default_preset": "m",
                "presets": {"m": {"tool": "codex", "target": "host", "yolo": True}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cage_config.explain(resolved, doctor=True)
        self.assertEqual(rc, 1)
        self.assertIn("cannot enforce network mode", buf.getvalue())

    def test_doctor_validates_host_mcp_executable(self):
        import io
        from unittest.mock import patch
        data = {"version": 1, "default_preset": "m",
                "mcp_packs": {"l": {"servers": [{"name": "j", "type": "stdio", "command": "x"}]}},
                "presets": {"m": {"tool": "codex", "target": "host", "mcp_packs": ["l"]}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cage_config.explain(resolved, doctor=True)
        self.assertEqual(rc, 1)
        self.assertIn("executable is not available", buf.getvalue())

    def test_doctor_rejects_custom_agents_dir(self):
        import io
        from unittest.mock import patch
        data = {"version": 1, "default_preset": "m",
                "auth": {"a": {"tool": "codex", "host_agents_dir": "/custom/agents"}},
                "presets": {"m": {"tool": "codex", "target": "host", "auth": "a"}}}
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cage_config.explain(resolved, doctor=True)
        self.assertEqual(rc, 1)
        self.assertIn("host_agents_dir", buf.getvalue())

    def test_doctor_rejects_host_ssh_alias(self):
        import io
        from unittest.mock import patch
        data = {
            "version": 1,
            "default_preset": "m",
            "identities": {"work": {"ssh_host": "alias=github.com"}},
            "presets": {
                "m": {
                    "tool": "codex",
                    "target": "host",
                    "net": "open",
                    "identity": "work",
                }
            },
        }
        resolved = cage_config.resolve_config(data, Path("/f.toml"), "/tmp/r")
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = cage_config.explain(resolved, doctor=True)
        self.assertEqual(rc, 1)
        self.assertIn("ssh_host", buf.getvalue())


class TestTuiEffectiveState(unittest.TestCase):
    """TUI and launcher show identical effective state."""

    def _make_controller(self, config_text, target_override=""):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        config = root / "config.toml"
        result = root / "result.json"
        result.touch(mode=0o600)
        config.write_text(config_text, encoding="utf-8")
        controller = cage_tui.Controller(
            ROOT / "cage-config.py", config, root, result,
            target_override=target_override,
        )
        self.addCleanup(tmp.cleanup)
        return controller

    def test_risks_include_no_docker_and_unrestricted_networking(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"', 'net = "open"', "",
        ]))
        _, preset = controller.effective_preset()
        risks = controller.risks(preset)
        self.assertTrue(any("NO Docker isolation" in r for r in risks))
        self.assertTrue(any("unrestricted networking" in r for r in risks))
        self.assertFalse(any("container has unrestricted" in r for r in risks))

    def test_named_codex_profiles_are_discovered_from_selected_auth_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex-home"
            codex_home.mkdir()
            (codex_home / "provider-preview.config.toml").write_text(
                'model = "provider-model"\n'
            )
            (codex_home / "bad.name.config.toml").write_text('model = "ignored"\n')
            controller = object.__new__(cage_tui.Controller)
            controller.snapshot = {
                "config": {
                    "auth": {
                        "work": {
                            "tool": "codex",
                            "host_codex_dir": str(codex_home),
                        }
                    }
                }
            }

            discovered_home, names = controller.codex_profiles({"auth": "work"})

        self.assertEqual(discovered_home, codex_home)
        self.assertEqual(names, ["provider-preview"])

    def test_target_override_appears_in_risks(self):
        """--host override shows host risks even for a container preset."""
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ]), target_override="host")
        _, preset = controller.effective_preset()
        risks = controller.risks(preset)
        self.assertTrue(any("NO Docker isolation" in r for r in risks))

    def test_yolo_command_overrides_drive_effective_network(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ]))
        controller.data["presets"]["main"].pop("net")
        controller.yolo_override = "on"
        _, preset = controller.effective_preset()
        self.assertEqual(controller.effective_exec_state(preset), ("container", True, "gate"))

        preset["yolo"] = True
        controller.yolo_override = "off"
        self.assertEqual(controller.effective_exec_state(preset), ("container", False, "open"))

    def test_preflight_surfaces_missing_host_mcp_executable(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.l]", 'servers = [{ name = "j", type = "stdio", command = "x" }]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["l"]', "",
        ]))
        _, preset = controller.effective_preset()
        warnings = controller.preflight(preset)
        self.assertTrue(any("Stdio MCP executable is not available" in w for w in warnings))

    def test_host_mcp_and_skill_risks_are_explicit(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[mcp_packs.l]", 'servers = [{ name = "local", type = "stdio", command = "codex" }]',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'mcp_packs = ["l"]', 'skill_packs = []', "",
        ]))
        _, preset = controller.effective_preset()
        risks = controller.risks(preset)
        self.assertTrue(any("host-user authority" in risk for risk in risks))

    def test_preflight_surfaces_net_gate_incompatibility(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "gate"', "",
        ]))
        _, preset = controller.effective_preset()
        warnings = controller.preflight(preset)
        self.assertTrue(any("cannot be enforced" in w for w in warnings))

    def test_preflight_surfaces_yolo_gate_incompatibility(self):
        """Yolo defaults to gate; preflight warns for host mode."""
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            "yolo = true", "",
        ]))
        _, preset = controller.effective_preset()
        warnings = controller.preflight(preset)
        self.assertTrue(any("cannot be enforced" in w or "network" in w.lower() for w in warnings))

    def test_preflight_surfaces_custom_agents_dir(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[auth.a]", 'tool = "codex"', 'host_agents_dir = "/custom/agents"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'auth = "a"', "",
        ]))
        _, preset = controller.effective_preset()
        warnings = controller.preflight(preset)
        self.assertTrue(any("host_agents_dir" in w for w in warnings))

    def test_preflight_surfaces_ssh_host_rejection(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[identities.work]", 'ssh_host = "alias=github.com"',
            "[presets.main]", 'tool = "codex"', 'target = "host"',
            'net = "open"', 'identity = "work"', "",
        ]))
        _, preset = controller.effective_preset()
        warnings = controller.preflight(preset)
        self.assertTrue(any("ssh_host" in warning for warning in warnings))

    def test_preflight_missing_codex_controlled_path(self):
        """Missing codex is detected with a controlled PATH."""
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"', 'net = "open"', "",
        ]))
        _, preset = controller.effective_preset()
        # Temporarily break PATH to simulate missing codex
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = "/nonexistent_dir_for_test"
        try:
            warnings = controller.preflight(preset)
        finally:
            os.environ["PATH"] = old_path
        self.assertTrue(any("codex command not found" in w for w in warnings))

    def test_preset_summary_shows_host(self):
        stub = type("S", (), {
            "repo": Path("/tmp/x"), "tool_override": "", "net_override": "",
            "yolo_override": "", "target_override": "",
            "data": {"presets": {}, "defaults": {}, "auth": {},
                     "identities": {}, "mcp_packs": {}, "skill_packs": {},
                     "host_commands": {}},
        })()
        view = cage_tui.CursesView(FakeScreen(), stub)
        summary = view._preset_summary({"tool": "codex", "target": "host"})
        combined = " ".join(summary)
        self.assertIn("Host CLI", combined)
        self.assertIn("no docker boundary", combined.lower())

    def test_preset_summary_shows_container_default(self):
        stub = type("S", (), {
            "repo": Path("/tmp/x"), "tool_override": "", "net_override": "",
            "yolo_override": "", "target_override": "",
            "data": {"presets": {}, "defaults": {}, "auth": {},
                     "identities": {}, "mcp_packs": {}, "skill_packs": {},
                     "host_commands": {}},
        })()
        view = cage_tui.CursesView(FakeScreen(), stub)
        summary = view._preset_summary({"tool": "codex"})
        combined = " ".join(summary)
        self.assertIn("Container", combined)


class TestTuiTargetOverride(unittest.TestCase):
    """--host/--container as TUI command override — behavioral tests."""

    def _make_controller(self, config_text, target_override=""):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        config = root / "config.toml"
        result = root / "result.json"
        result.touch(mode=0o600)
        config.write_text(config_text, encoding="utf-8")
        controller = cage_tui.Controller(
            ROOT / "cage-config.py", config, root, result,
            target_override=target_override,
        )
        self.addCleanup(tmp.cleanup)
        return controller

    def test_override_labels_rendered_in_edit_preset(self):
        """edit_preset shows (command override) when target_override is set."""
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ]), target_override="host")
        screen = FakeScreen([curses.KEY_END, 10])
        view = cage_tui.CursesView(screen, controller)
        edited = view.edit_preset({"tool": "codex", "net": "open"})

        rendered = " ".join(text for _, _, text, _ in screen.writes)
        self.assertIn("Host CLI", rendered)
        self.assertIn("command override", rendered)
        self.assertNotIn("target", edited)
        self.assertEqual(controller.snapshot["effective"]["target"], "container")

    def test_saved_preset_not_mutated_by_override(self):
        """Remember/Save with --host does not write target=host to config."""
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ]), target_override="host")
        view = cage_tui.CursesView(FakeScreen(), controller)
        view.menu = lambda *_args, **_kwargs: "remember"
        view.risk_review = lambda *_args, **_kwargs: True

        self.assertTrue(view.launch_actions({"tool": "codex", "net": "open"}))
        mapped = controller.data["projects"][str(controller.repo)]
        self.assertNotIn("target", controller.data["presets"][mapped])

    def test_container_override_does_not_replace_saved_host_target(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'target = "host"', 'net = "open"', "",
        ]), target_override="container")
        view = cage_tui.CursesView(FakeScreen(), controller)
        view.menu = lambda *_args, **_kwargs: "remember"
        view.risk_review = lambda *_args, **_kwargs: True

        self.assertTrue(view.launch_actions({"tool": "codex", "target": "host", "net": "open"}))
        mapped = controller.data["projects"][str(controller.repo)]
        self.assertEqual(controller.data["presets"][mapped]["target"], "host")

    def test_saved_launch_writes_reviewed_target_without_mutating_preset(self):
        controller = self._make_controller('\n'.join([
            "version = 1", 'default_preset = "main"',
            "[presets.main]", 'tool = "codex"', 'net = "open"', "",
        ]), target_override="host")
        view = cage_tui.CursesView(FakeScreen(), controller)
        view.menu = lambda *_args, **_kwargs: "launch"
        view.risk_review = lambda *_args, **_kwargs: True

        self.assertEqual(view.run(), 0)
        decision = json.loads(controller.result.read_text(encoding="utf-8"))
        self.assertEqual(decision["action"], "launch_once")
        self.assertEqual(decision["preset"]["target"], "host")
        self.assertNotIn("target", controller.data["presets"]["main"])


class TestCancellationNoOp(unittest.TestCase):
    """Cancellation produces no Docker or Codex invocation."""

    def test_tui_review_cancel_no_docker_no_codex_no_mutation(self):
        """PTY-driven review cancellation leaves launch and config state untouched."""
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        write_fake_docker_failing(bin_dir / "docker")
        write_fake_codex(bin_dir / "codex")
        config_path = cage_dir / "config.toml"
        config_text = HOST_CONFIG
        config_path.write_text(config_text, encoding="utf-8")
        config_before = config_path.read_text(encoding="utf-8")
        result_path = cage_dir / ".tui-result.test"
        result_path.touch(mode=0o600)
        env = make_env(tmp_path, bin_dir, home, xdg)
        env["TERM"] = "xterm"
        # Use PTY to give cage a real TTY for curses
        master, slave = os.openpty()
        output = bytearray()

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        def read_until(needle: bytes, timeout: float = 8) -> None:
            current = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                output.extend(chunk)
                current.extend(chunk)
                if needle in current:
                    return
            raise AssertionError(f"did not observe {needle!r}; output={bytes(current)!r}")

        proc = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "cage-tui.py"),
                    "--backend", str(ROOT / "cage-config.py"),
                    "--config", str(config_path),
                    "--repo", str(repo),
                    "--result", str(result_path),
                    "--target-override", "host",
                ],
                cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, preexec_fn=controlling_terminal,
            )
            os.close(slave)
            read_until(b"Launch with this configuration")
            os.write(master, b"\r")
            read_until(b"Review before launch/save")
            os.write(master, b"\x1b")
            read_until(b"Launch with this configuration")
            self.assertIsNone(proc.poll())
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            os.close(master)
        # Verify no Docker, no Codex, no config mutation
        output_text = bytes(output).decode("utf-8", errors="replace")
        self.assertIn("Review before launch/save", output_text)
        self.assertNotIn("interactive mode requires a TTY", output_text)
        self.assertNotIn("FAKE_DOCKER_CALLED", output_text)
        self.assertNotIn("CODEX_HOME=", output_text)
        config_after = config_path.read_text(encoding="utf-8")
        self.assertEqual(config_before, config_after)
        self.assertEqual(result_path.read_bytes(), b"")



class TestAuthoritativeMcpSelection(unittest.TestCase):
    """Selected-only MCP suppression reaches the tool across launch targets."""

    def test_inherited_mcp_suppressed_in_host_args(self):
        config = "\n".join([
            "version = 1", "default_preset = \"main\"",
            "[auth.myauth]", "tool = \"codex\"",
            "[presets.main]", "tool = \"codex\"", "target = \"host\"",
            "auth = \"myauth\"", "net = \"open\"", "",
        ])
        inventory = json.dumps([
            {"name": "node_repl", "enabled": True},
            {"name": "user_extra", "enabled": True},
        ])
        result, _, _, tmp = setup_host_test(
            config,
            codex_files={"config.toml": "[mcp_servers.node_repl]\ncommand = \"echo\"\n"},
            env_overrides={"CAGE_FAKE_CODEX_MCP_LIST_JSON": inventory},
        )
        self.addCleanup(tmp.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The selected-only overrides reach the codex process arguments.
        self.assertIn("mcp_servers.node_repl.enabled=false", result.stdout)
        self.assertIn("mcp_servers.user_extra.enabled=false", result.stdout)
        # Launch output discloses the policy and the suppressed servers.
        self.assertIn("MCP policy: selected packs only", result.stderr)
        self.assertIn("node_repl", result.stderr)

    def test_container_suppression_is_delegated_to_the_runtime(self):
        """Container suppression is computed inside the image, not on the host.

        A host-side inventory can reference servers that do not exist in the
        image (which would fail Codex config load), so the launcher must not
        apply host-derived disable overrides to a container launch. The
        entrypoint inventories the container runtime and applies them there.
        """
        config = "\n".join([
            "version = 1", "default_preset = \"main\"",
            "[auth.myauth]", "tool = \"codex\"", "copy_auth = false",
            "[presets.main]", "tool = \"codex\"", "auth = \"myauth\"",
            "net = \"open\"", "",
        ])
        inventory = json.dumps([{"name": "node_repl", "enabled": True}])
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        xdg, home, bin_dir = tmp_path / "xdg", tmp_path / "home", tmp_path / "bin"
        cage_dir, repo = xdg / "cage", tmp_path / "repo"
        for d in (bin_dir, cage_dir, home, repo):
            d.mkdir(parents=True)
        (home / ".codex").mkdir()
        (home / ".codex" / "config.toml").write_text(
            "[mcp_servers.node_repl]\ncommand = \"echo\"\n", encoding="utf-8"
        )
        write_fake_docker_success(bin_dir / "docker")
        write_fake_codex(bin_dir / "codex")
        (cage_dir / "config.toml").write_text(config, encoding="utf-8")
        env = make_env(tmp_path, bin_dir, home, xdg)
        env["CAGE_FAKE_CODEX_MCP_LIST_JSON"] = inventory
        result = subprocess.run(
            [str(CAGE), str(repo)], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DOCKER_RUN", result.stdout)
        # The host-derived override must NOT be injected into the container
        # command; suppression is enforced by the in-container inventory.
        self.assertNotIn("mcp_servers.node_repl.enabled=false", result.stdout)

if __name__ == "__main__":
    unittest.main()
