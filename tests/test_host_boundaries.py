import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CAGE = ROOT / "cage"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_fake_docker(path: Path) -> None:
    write_executable(
        path,
        """#!/bin/sh
case "$1" in
  ps|image|build|pull|tag|volume) exit 0 ;;
  run)
    shift
    : > "$DOCKER_RUN_ARGS"
    for arg in "$@"; do
      printf '%s\\0' "$arg" >> "$DOCKER_RUN_ARGS"
      case "$arg" in
        *"/.mcp.json:ro")
          source_path=${arg%%:*}
          cp "$source_path" "$MCP_CAPTURE"
          ;;
      esac
    done
    exit 0
    ;;
esac
exit 0
""",
    )


def write_mcp_config(path: Path, extra_mount: Path | None = None) -> None:
    lines = [
                "version = 1",
                'default_preset = "codex-test"',
                "[auth.codex-test]",
                'tool = "codex"',
                "copy_auth = false",
                "[mcp_packs.local]",
                "servers = [",
                '  { name = "local", type = "stdio", command = "printf ignored" },',
                "]",
                "[presets.codex-test]",
                'tool = "codex"',
                'auth = "codex-test"',
                'mcp_packs = ["local"]',
                'net = "open"',
    ]
    if extra_mount is not None:
        lines.append(
            "extra_mounts = [{ path = "
            + json.dumps(str(extra_mount))
            + ', mode = "rw" }]'
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_fake_mcp_bridge(path: Path) -> None:
    path.write_text(
        """import signal
import sys
import time

index = 1
port = 41000
while index < len(sys.argv):
    if sys.argv[index] == "--server":
        print("SERVER:%s=PORT:%d" % (sys.argv[index + 1], port), flush=True)
        port += 1
        index += 3
    else:
        index += 1
print("READY", flush=True)

def stop(_signal, _frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )


class HostBoundaryTests(unittest.TestCase):
    def launch_with_mcp(
        self,
        root: Path,
        repo: Path,
        *,
        xdg: Path | None = None,
        extra_mount: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess, Path, Path]:
        xdg = xdg or root / "xdg"
        home = root / "home"
        bin_dir = root / "bin"
        capture = root / "container-mcp.json"
        docker_args = root / "docker-run-args"
        (xdg / "cage").mkdir(parents=True)
        home.mkdir(parents=True)
        bin_dir.mkdir(parents=True)
        write_fake_docker(bin_dir / "docker")
        write_mcp_config(xdg / "cage" / "config.toml", extra_mount)
        launcher_dir = root / "launcher"
        launcher_dir.mkdir()
        shutil.copy2(CAGE, launcher_dir / "cage")
        shutil.copy2(ROOT / "cage-config.py", launcher_dir / "cage-config.py")
        write_fake_mcp_bridge(launcher_dir / "mcp-bridge.py")

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(xdg)
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["MCP_CAPTURE"] = str(capture)
        env["DOCKER_RUN_ARGS"] = str(docker_args)
        env.pop("OPENAI_API_KEY", None)
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)

        result = subprocess.run(
            [str(launcher_dir / "cage"), str(repo)],
            cwd=repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        return result, capture, docker_args

    def test_project_mcp_overlay_handles_quote_and_newline_path_without_host_write(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo'quoted\nline"
            repo.mkdir()
            project_mcp = repo / ".mcp.json"
            import_sentinel = root / "launcher-imported-repo-json"
            (repo / "json.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(import_sentinel)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            original = json.dumps(
                {
                    "mcpServers": {
                        "local": {"command": "host-only-command", "args": ["--original"]},
                        "other": {"command": "project-command"},
                    }
                },
                indent=2,
            ) + "\n"
            project_mcp.write_text(original, encoding="utf-8")

            result, capture, docker_args = self.launch_with_mcp(root, repo)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(import_sentinel.exists())
            self.assertEqual(project_mcp.read_text(encoding="utf-8"), original)
            generated = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(
                generated["mcpServers"]["local"],
                {"type": "stdio", "command": "mcp-relay", "args": ["local"]},
            )
            self.assertEqual(
                generated["mcpServers"]["other"],
                {"command": "project-command"},
            )
            run_args = [os.fsdecode(value) for value in docker_args.read_bytes().split(b"\0") if value]
            token_arg = next(value for value in run_args if value.startswith("MCP_BRIDGE_TOKEN="))
            self.assertRegex(token_arg, r"^MCP_BRIDGE_TOKEN=[0-9a-f]{64}$")
            overlay_mount = next(value for value in run_args if value.endswith("/.mcp.json:ro"))
            overlay_source = Path(overlay_mount.split(":", 1)[0])
            self.assertFalse(overlay_source.exists())
            self.assertFalse(overlay_source.is_relative_to(repo))
            self.assertEqual(overlay_source.parent, (root / "xdg" / "cage").resolve())

    def test_private_stage_rejects_writable_repository_alias(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()

            result, capture, _ = self.launch_with_mcp(root, repo, xdg=repo / "xdg")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inside the writable repo mount", result.stderr)
            self.assertFalse(capture.exists())

    def test_private_stage_rejects_read_write_extra_mount_alias(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            shared = root / "shared"
            repo.mkdir()
            shared.mkdir()

            result, capture, _ = self.launch_with_mcp(
                root,
                repo,
                xdg=shared / "xdg",
                extra_mount=shared,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inside read-write extra mount", result.stderr)
            self.assertFalse(capture.exists())

    def test_symlinked_project_mcp_fails_closed_without_restore_write(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            victim = root / "victim.json"
            original = '{"mcpServers":{"local":{"command":"must-stay"}}}\n'
            victim.write_text(original, encoding="utf-8")
            (repo / ".mcp.json").symlink_to(victim)

            result, capture, _ = self.launch_with_mcp(root, repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing symlinked project MCP configuration", result.stderr)
            self.assertTrue((repo / ".mcp.json").is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), original)
            self.assertFalse(capture.exists())

    def test_oversized_project_mcp_fails_before_host_json_parse(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            project_mcp = repo / ".mcp.json"
            with project_mcp.open("wb") as handle:
                handle.truncate(16 * 1024 * 1024 + 1)

            result, capture, _ = self.launch_with_mcp(root, repo)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exceeds the 16 MiB limit", result.stderr)
            self.assertEqual(project_mcp.stat().st_size, 16 * 1024 * 1024 + 1)
            self.assertFalse(capture.exists())

    def test_netgate_helpers_ignore_cwd_json_shadow_and_handle_quoted_home(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home'quoted\nline"
            sentinel = root / "cwd-json-imported"
            repo.mkdir()
            home.mkdir()
            (repo / "json.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)

            add = subprocess.run(
                [str(CAGE), "netgate", "allow", "example.com"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            listed = subprocess.run(
                [str(CAGE), "netgate", "list", str(repo)],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

            self.assertEqual(add.returncode, 0, add.stderr)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertFalse(sentinel.exists())
            project_files = list((home / ".claude" / "netgate").glob("project-*.json"))
            self.assertEqual(len(project_files), 1)
            self.assertEqual(json.loads(project_files[0].read_text()), {"domains": ["example.com"]})
            self.assertIn("allow  example.com", listed.stdout)

    def test_inline_host_python_is_isolated_and_has_no_restore_path(self):
        netgate = (ROOT / "cage-netgate.sh").read_text(encoding="utf-8")
        launcher = CAGE.read_text(encoding="utf-8")

        self.assertNotIn("python3 -c", netgate)
        self.assertEqual(netgate.count("python3 -I -c"), 4)
        self.assertNotIn("MCP_JSON_BACKUP", launcher)
        self.assertNotIn("with open('$REPO_PATH", launcher)
        self.assertIn("python3 -I -c", launcher)

    def test_host_control_plane_scripts_ignore_pythonpath_from_repository(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            repo = root / "repo"
            xdg = root / "xdg"
            sentinel = root / "pythonpath-imported-repository-json"
            repo.mkdir()
            (xdg / "cage").mkdir(parents=True)
            (repo / "json.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            (xdg / "cage" / "config.toml").write_text(
                "\n".join(
                    [
                        "version = 1",
                        'default_preset = "codex-test"',
                        "[auth.codex-test]",
                        'tool = "codex"',
                        "copy_auth = false",
                        "[presets.codex-test]",
                        'tool = "codex"',
                        'auth = "codex-test"',
                        'net = "open"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(xdg)
            env["PYTHONPATH"] = str(repo)

            result = subprocess.run(
                [str(CAGE), "config", "list"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())
            launcher = CAGE.read_text(encoding="utf-8")
            self.assertNotIn('python3 "$SCRIPT_DIR/', launcher)


if __name__ == "__main__":
    unittest.main()
