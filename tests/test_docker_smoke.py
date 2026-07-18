import os
import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETGATE = ROOT / "netgate-proxy.py"
ENTRYPOINT_CODEX = ROOT / "entrypoint-codex.sh"


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(
    os.environ.get("CAGE_RUN_DOCKER_SMOKE") == "1",
    "set CAGE_RUN_DOCKER_SMOKE=1 to run local Docker integration smoke tests",
)
class DockerSmokeTests(unittest.TestCase):
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
