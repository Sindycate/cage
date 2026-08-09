from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from cage_core.opencode import create_launch_snapshot, write_runtime_environment


@unittest.skipUnless(
    os.environ.get("CAGE_RUN_OPENCODE_DOCKER_CONTRACT") == "1",
    "set CAGE_RUN_OPENCODE_DOCKER_CONTRACT=1 for the real-Docker contract",
)
class OpenCodeProxyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        shared_parent = Path.cwd() if sys.platform == "darwin" else None
        self.root = Path(
            tempfile.mkdtemp(prefix=".cage-opencode-proxy-", dir=shared_parent)
        )
        self.repo = self.root / "repo"
        self.config = self.root / "config"
        self.repo.mkdir()
        self.config.mkdir()
        self.requests: list[dict[str, object]] = []
        expected = "Basic " + base64.b64encode(b"cage:contract").decode("ascii")
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def record(self) -> None:
                requests.append(
                    {
                        "method": self.command,
                        "target": self.path,
                        "authenticated": self.headers.get("Proxy-Authorization")
                        == expected,
                    }
                )
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            do_CONNECT = record
            do_GET = record
            do_POST = record

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

    def _snapshot(
        self,
        name: str,
        remote: list[dict[str, str]],
        *,
        skill_mounts: list[dict[str, str]] | None = None,
    ) -> Path:
        destination = self.root / name
        create_launch_snapshot(
            destination=destination,
            host_config_directory=self.config,
            repository=self.repo,
            stdio_mcp=[],
            remote_mcp=remote,
            skill_mounts=skill_mounts,
            host_agents_directory=self.root / "no-agents",
        )
        port = self.server.server_address[1]
        proxy = f"http://cage:contract@host.docker.internal:{port}"
        write_runtime_environment(
            destination / "runtime-env.json",
            {
                "HTTP_PROXY": proxy,
                "HTTPS_PROXY": proxy,
                "http_proxy": proxy,
                "https_proxy": proxy,
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            },
        )
        return destination

    def _run(
        self,
        snapshot: Path,
        arguments: list[str],
        *,
        network_none: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "docker",
            "run",
            "--rm",
        ]
        if network_none:
            command.extend(("--network", "none"))
        else:
            command.extend(("--add-host", "host.docker.internal:host-gateway"))
        command.extend([
            "-v",
            f"{self.repo}:{self.repo}",
            "-v",
            "/home/opencode/.cage-state",
            "-v",
            f"{snapshot}:/cage-opencode-snapshot:ro",
            "-e",
            f"WORKSPACE_DIR={self.repo}",
            "-e",
            f"HOST_UID={os.getuid()}",
            "-e",
            f"HOST_GID={os.getgid()}",
        ])
        command.extend(("opencode:smoke", *arguments))
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )

    def test_remote_mcp_and_provider_present_authenticated_proxy_credentials(self) -> None:
        inherited_marker = self.repo / "inherited-mcp-started"
        (self.config / "opencode.json").write_text(
            json.dumps(
                {
                    "provider": {
                        "contract": {
                            "npm": "@ai-sdk/openai-compatible",
                            "name": "Proxy contract",
                            "options": {
                                "baseURL": "http://opencode-provider-contract.invalid/v1",
                                "apiKey": "contract-key",
                            },
                            "models": {"test": {"name": "Proxy test"}},
                        }
                    },
                    "model": "contract/test",
                    "mcp": {
                        "inherited": {
                            "type": "local",
                            "command": [
                                "/bin/sh",
                                "-c",
                                f"touch {inherited_marker}",
                            ],
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        snapshot = self._snapshot(
            "mcp-snapshot",
            [
                {
                    "name": "remote",
                    "type": "http",
                    "url": "http://opencode-mcp-contract.invalid/mcp",
                }
            ],
        )
        result = self._run(snapshot, ["mcp", "list"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(
                item["authenticated"] is True
                and item["target"] == "http://opencode-mcp-contract.invalid/mcp"
                for item in self.requests
            ),
            self.requests,
        )
        self.assertFalse(inherited_marker.exists())

        self.requests.clear()
        provider_snapshot = self._snapshot("provider-snapshot", [])
        self._run(provider_snapshot, ["run", "-m", "contract/test", "proxy contract"])
        self.assertTrue(
            any(
                item["authenticated"] is True
                and item["method"] == "POST"
                and str(item["target"]).startswith(
                    "http://opencode-provider-contract.invalid/v1/"
                )
                for item in self.requests
            ),
            self.requests,
        )

    def test_live_project_config_change_cannot_replace_snapshot(self) -> None:
        project_config = self.repo / "opencode.json"
        project_config.write_text(
            json.dumps({"model": "snapshot/provider"}) + "\n",
            encoding="utf-8",
        )
        snapshot = self._snapshot("frozen-project-snapshot", [])
        project_config.write_text(
            json.dumps({"model": "live/provider"}) + "\n",
            encoding="utf-8",
        )
        result = self._run(snapshot, ["debug", "config"])
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = json.loads(result.stdout)
        self.assertEqual(resolved.get("model"), "snapshot/provider")

    def test_selected_and_project_skills_are_discovered_from_snapshot(self) -> None:
        project_skill = self.repo / ".agents" / "skills" / "project-contract"
        project_skill.mkdir(parents=True)
        (project_skill / "SKILL.md").write_text(
            "---\nname: project-contract\ndescription: project contract\n---\nproject\n",
            encoding="utf-8",
        )
        selected_skill = self.root / "selected-contract"
        selected_skill.mkdir()
        (selected_skill / "SKILL.md").write_text(
            "---\nname: selected-contract\ndescription: selected contract\n---\nselected\n",
            encoding="utf-8",
        )
        snapshot = self._snapshot(
            "skills-snapshot",
            [],
            skill_mounts=[
                {"name": "selected-contract", "path": str(selected_skill)}
            ],
        )
        result = self._run(snapshot, ["debug", "skill"])
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {item["name"] for item in json.loads(result.stdout)}
        self.assertIn("selected-contract", names)
        self.assertIn("project-contract", names)

    def test_network_none_runs_the_real_opencode_entrypoint(self) -> None:
        snapshot = self._snapshot("offline-snapshot", [])
        result = self._run(snapshot, ["--version"], network_none=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
