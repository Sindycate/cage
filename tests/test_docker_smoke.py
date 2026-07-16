import os
import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETGATE = ROOT / "netgate-proxy.py"


@unittest.skipUnless(
    os.environ.get("CAGE_RUN_DOCKER_SMOKE") == "1",
    "set CAGE_RUN_DOCKER_SMOKE=1 to run local Docker integration smoke tests",
)
class DockerSmokeTests(unittest.TestCase):
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
