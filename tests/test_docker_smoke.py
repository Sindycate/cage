import os
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETGATE = ROOT / "netgate-proxy.py"
ENTRYPOINT_CODEX = ROOT / "entrypoint-codex.sh"
ENTRYPOINT_CLAUDE = ROOT / "entrypoint.sh"


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(
    os.environ.get("CAGE_RUN_DOCKER_SMOKE") == "1",
    "set CAGE_RUN_DOCKER_SMOKE=1 to run local Docker integration smoke tests",
)
class DockerSmokeTests(unittest.TestCase):
    def test_codex_entrypoint_preserves_volume_owned_history(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            host_codex = temp_path / "host-codex"
            volume_codex = temp_path / "volume-codex"
            fake_bin.mkdir()
            host_codex.mkdir()
            volume_codex.mkdir()
            control_config = temp_path / "config.toml"
            control_config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "codex"\n',
                encoding="utf-8",
            )
            request = temp_path / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": hashlib.sha256(control_config.read_bytes()).hexdigest(),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "codex", "net": "gate"},
                }],
            }), encoding="utf-8")
            saved = subprocess.run(
                [sys.executable, str(ROOT / "cage-config.py"), "--config", str(control_config),
                 "ui-commit", "--repo", str(temp_path), "--request", str(request)],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            (host_codex / "config.toml").write_text(
                'model = "host-config"\n',
                encoding="utf-8",
            )
            (host_codex / "rules").mkdir()
            (host_codex / "rules" / "host.rules").write_text(
                'prefix_rule(pattern=["git", "status"], decision="allow")\n',
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
                (host_codex / name).write_bytes(("host:" + name).encode())
                (volume_codex / name).write_bytes(("volume:" + name).encode())
            for name in runtime_directories:
                (host_codex / name).mkdir()
                (host_codex / name / "host-only.bin").write_bytes(b"host")
                (volume_codex / name).mkdir()
                (volume_codex / name / "volume.bin").write_bytes(
                    ("volume:" + name).encode()
                )

            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "codex",
                "#!/bin/sh\n"
                "grep -q 'host-config' \"$HOME/.codex/config.toml\" && "
                "grep -q 'git' \"$HOME/.codex/rules/host.rules\" && "
                "for name in history.jsonl session_index.jsonl state_5.sqlite "
                "state_5.sqlite-wal logs_2.sqlite memories_1.sqlite goals_1.sqlite; do "
                "cmp -s \"$HOME/.codex/$name\" \"/volume-codex-source/$name\" || exit 1; "
                "done && "
                "for name in sessions archived_sessions log memories cache shell_snapshots; do "
                "cmp -s \"$HOME/.codex/$name/volume.bin\" "
                "\"/volume-codex-source/$name/volume.bin\" || exit 1; "
                "[ ! -e \"$HOME/.codex/$name/host-only.bin\" ] || exit 1; "
                "done\n",
            )

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "--mount",
                    f"type=bind,src={host_codex},dst=/host-codex-source,readonly",
                    "--mount",
                    f"type=bind,src={volume_codex},dst=/volume-codex-source,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "-e",
                    "CODEX_COPY_AUTH=0",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "cp -R /host-codex-source /host-codex && "
                    "chown -R 21001:21001 /host-codex && "
                    "mkdir -p /workspace /home/codex && "
                    "cp -R /volume-codex-source /home/codex/.codex && "
                    "/entrypoint.sh --version && /entrypoint.sh --version",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_entrypoint_preserves_session_tree_across_config_save_and_two_launches(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            fake_bin = temp_path / "bin"
            fake_bin.mkdir()
            control_config = temp_path / "config.toml"
            control_config.write_text(
                'version = 1\ndefault_preset = "main"\n\n[presets.main]\ntool = "claude"\n',
                encoding="utf-8",
            )
            request = temp_path / "request.json"
            request.write_text(json.dumps({
                "expected_sha256": hashlib.sha256(control_config.read_bytes()).hexdigest(),
                "operations": [{
                    "action": "upsert", "collection": "presets", "name": "main",
                    "value": {"tool": "claude", "net": "gate", "session_sync": True},
                }],
            }), encoding="utf-8")
            saved = subprocess.run(
                [sys.executable, str(ROOT / "cage-config.py"), "--config", str(control_config),
                 "ui-commit", "--repo", str(temp_path), "--request", str(request)],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(saved.returncode, 0, saved.stderr)
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\nuser=$1\nshift\nexec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(fake_bin / "jq", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "claude",
                "#!/bin/sh\n"
                "grep -q volume-session \"$HOME/.claude/projects/repo/session.jsonl\" && "
                "grep -q volume-memory \"$HOME/.claude/projects/repo/memory/MEMORY.md\"\n",
            )

            result = subprocess.run(
                [
                    "docker", "run", "--rm", "--cap-drop", "ALL",
                    "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                    "--cap-add", "SETGID", "--cap-add", "SETUID",
                    "--mount", f"type=bind,src={ENTRYPOINT_CLAUDE},dst=/entrypoint.sh,readonly",
                    "--mount", f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e", "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e", "HOME=/home/claude", "-e", "HOST_UID=22001", "-e", "HOST_GID=22001",
                    "-e", "WORKSPACE_DIR=/workspace", "python:3.12-slim", "sh", "-c",
                    "groupadd -g 22000 claude && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh claude && "
                    "mkdir -p /workspace /home/claude/.claude/projects/repo/memory && "
                    "printf 'volume-session\\n' > /home/claude/.claude/projects/repo/session.jsonl && "
                    "printf 'volume-memory\\n' > /home/claude/.claude/projects/repo/memory/MEMORY.md && "
                    "bash /entrypoint.sh --version && bash /entrypoint.sh --version",
                ],
                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_sensitive_modes_are_set_by_the_remapped_owner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "codex",
                "#!/bin/sh\n"
                "mode=$(stat -c %a \"$HOME/.codex/.credentials.json\")\n"
                "owner=$(stat -c %u \"$HOME/.codex/.credentials.json\")\n"
                "[ \"$mode:$owner\" = 600:22001 ] || { "
                "echo \"unexpected credential state: $mode:$owner\" >&2; exit 1; }\n",
            )

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "mkdir -p /workspace /home/codex/.codex && "
                    "printf '{\\\"test\\\":true}\\n' > /home/codex/.codex/.credentials.json && "
                    "chmod 640 /home/codex/.codex/.credentials.json && "
                    "chown 22001:22001 /home/codex/.codex/.credentials.json && "
                    "exec /entrypoint.sh --version",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_sensitive_mode_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            fake_bin = Path(temp_dir) / "bin"
            fake_bin.mkdir()
            write_executable(
                fake_bin / "gosu",
                "#!/bin/sh\n"
                "user=$1\n"
                "shift\n"
                "exec setpriv --reuid \"$(id -u \"$user\")\" "
                "--regid \"$(id -g \"$user\")\" --init-groups \"$@\"\n",
            )
            write_executable(fake_bin / "git", "#!/bin/sh\nexit 0\n")
            write_executable(fake_bin / "codex", "#!/bin/sh\nexit 0\n")

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--cap-drop",
                    "ALL",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETGID",
                    "--cap-add",
                    "SETUID",
                    "--mount",
                    f"type=bind,src={ENTRYPOINT_CODEX},dst=/entrypoint.sh,readonly",
                    "--mount",
                    f"type=bind,src={fake_bin},dst=/test-bin,readonly",
                    "-e",
                    "PATH=/test-bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "-e",
                    "HOME=/home/codex",
                    "-e",
                    "HOST_UID=22001",
                    "-e",
                    "HOST_GID=22001",
                    "-e",
                    "WORKSPACE_DIR=/workspace",
                    "python:3.12-slim",
                    "sh",
                    "-c",
                    "groupadd -g 22000 codex && "
                    "useradd -u 22000 -g 22000 -M -s /bin/sh codex && "
                    "mkdir -p /workspace /home/codex/.codex && "
                    "printf 'host file\\n' > /workspace/victim && "
                    "chmod 644 /workspace/victim && "
                    "chown 22001:22001 /workspace/victim && "
                    "ln -s /workspace/victim /home/codex/.codex/.credentials.json && "
                    "/entrypoint.sh --version; "
                    "status=$?; "
                    "[ \"$status\" -ne 0 ] && "
                    "[ \"$(stat -c %a /workspace/victim)\" = 644 ] && "
                    "[ \"$(cat /workspace/victim)\" = 'host file' ]",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "refusing unsafe sensitive Codex state .credentials.json",
                result.stderr,
            )

    def test_nested_read_only_file_overlay_protects_repository_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            repo = temp_path / "repo"
            repo.mkdir()
            source = repo / ".mcp.json"
            overlay = temp_path / "overlay.json"
            source.write_text('{"source":"repository"}\n', encoding="utf-8")
            overlay.write_text('{"source":"cage-overlay"}\n', encoding="utf-8")
            overlay.chmod(0o600)

            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{repo}:{repo}",
                    "-v",
                    f"{overlay}:{repo / '.mcp.json'}:ro",
                    "alpine:3",
                    "sh",
                    "-c",
                    "grep -q cage-overlay \"$1/.mcp.json\" && "
                    "! (printf x >> \"$1/.mcp.json\" 2>/dev/null)",
                    "sh",
                    str(repo),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                source.read_text(encoding="utf-8"),
                '{"source":"repository"}\n',
            )

    def test_netgate_accepts_container_client_but_rejects_malformed_target(self):
        with tempfile.TemporaryDirectory() as config_dir:
            auth_token = "b" * 64
            proxy_env = os.environ.copy()
            proxy_env["CAGE_NETGATE_AUTH_TOKEN"] = auth_token
            proxy = subprocess.Popen(
                [
                    sys.executable,
                    str(NETGATE),
                    "--project-hash",
                    "smoketest",
                    "--container-name",
                    "cage-netgate-smoke",
                    "--config-dir",
                    config_dir,
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proxy_env,
            )
            try:
                self.assertIsNotNone(proxy.stdout)
                port_line = proxy.stdout.readline().strip()
                ready_line = proxy.stdout.readline().strip()
                startup_stderr = ""
                if not port_line.startswith("PORT=") or ready_line != "READY":
                    self.assertIsNotNone(proxy.stderr)
                    startup_stderr = proxy.stderr.read()
                self.assertTrue(
                    port_line.startswith("PORT="),
                    f"stdout={port_line!r}/{ready_line!r}; stderr={startup_stderr}",
                )
                self.assertEqual(
                    ready_line,
                    "READY",
                    f"stdout={port_line!r}/{ready_line!r}; stderr={startup_stderr}",
                )
                port = port_line.split("=", 1)[1]
                auth = base64.b64encode(f"cage:{auth_token}".encode()).decode()

                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--add-host",
                        "host.docker.internal:host-gateway",
                        "alpine:3",
                        "sh",
                        "-c",
                        "{ printf 'GET / HTTP/1.1\\r\\nHost: invalid\\r\\n"
                        f"Proxy-Authorization: Basic {auth}\\r\\n"
                        "Connection: close\\r\\n\\r\\n'; sleep 1; } "
                        "| nc -w 3 host.docker.internal \"$1\"",
                        "sh",
                        port,
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("400 Expected an absolute HTTP URL", result.stdout)
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
