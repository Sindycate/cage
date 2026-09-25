import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_CODEX = ROOT / "entrypoint-codex.sh"


class CodexEntrypointTests(unittest.TestCase):
    def entrypoint_python(self):
        src = ENTRYPOINT_CODEX.read_text()
        match = re.search(
            r"CODEX_CONFIG_PATH=\"\$CODEX_DIR/config\.toml\" python3 -I - <<'PY'\n(.*?)\nPY",
            src,
            re.S,
        )
        self.assertIsNotNone(match)
        return match.group(1)

    def test_bridged_stdio_mcp_servers_get_explicit_bridge_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_CONFIG_PATH": str(config_path),
                    "CAGE_MCP_SERVERS": '{"databricks-uc":60776}',
                    "MCP_BRIDGE_HOST": "host.docker.internal",
                    "MCP_BRIDGE_PORT_DATABRICKS_UC": "60776",
                    "MCP_BRIDGE_TOKEN": "b" * 64,
                }
            )

            result = subprocess.run(
                ["python3", "-c", self.entrypoint_python()],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = config_path.read_text()

        self.assertIn('[mcp_servers."databricks-uc"]', generated)
        self.assertIn('command = "mcp-relay"', generated)
        self.assertIn('args = ["databricks-uc"]', generated)
        self.assertIn('[mcp_servers."databricks-uc".env]', generated)
        self.assertIn('MCP_BRIDGE_HOST = "host.docker.internal"', generated)
        self.assertIn('MCP_BRIDGE_PORT_DATABRICKS_UC = "60776"', generated)
        self.assertIn('MCP_BRIDGE_TOKEN = "' + "b" * 64 + '"', generated)

    def test_oauth_http_mcp_servers_include_resource_and_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_CONFIG_PATH": str(config_path),
                    "CAGE_REMOTE_MCP_SERVERS": (
                        '[{"name":"dash0","type":"http",'
                        '"url":"https://api.eu-central-1.aws.dash0.com/mcp",'
                        '"auth":"oauth",'
                        '"oauth_resource":"https://api.eu-central-1.aws.dash0.com",'
                        '"oauth_scopes":["*"]}]'
                    ),
                }
            )

            result = subprocess.run(
                ["python3", "-c", self.entrypoint_python()],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = config_path.read_text()

        self.assertIn('mcp_oauth_credentials_store = "file"', generated)
        self.assertNotIn('rmcp_client', generated)
        self.assertNotIn('experimental_use_rmcp_client', generated)
        self.assertIn('[mcp_servers."dash0"]', generated)
        self.assertIn('url = "https://api.eu-central-1.aws.dash0.com/mcp"', generated)
        self.assertIn('oauth_resource = "https://api.eu-central-1.aws.dash0.com"', generated)
        self.assertIn('scopes = ["*"]', generated)

    def test_unsupported_rmcp_features_are_removed_without_selected_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[features]\n"
                "experimental_use_rmcp_client = true\n"
                "rmcp_client = false\n"
                "mcp_oauth_refresh_coordination = true\n"
                "web_search_request = true\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["CODEX_CONFIG_PATH"] = str(config_path)
            env.pop("CAGE_MCP_SERVERS", None)
            env.pop("CAGE_REMOTE_MCP_SERVERS", None)

            result = subprocess.run(
                ["python3", "-c", self.entrypoint_python()],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = config_path.read_text(encoding="utf-8")

        self.assertNotIn("rmcp_client", generated)
        self.assertIn("web_search_request = true", generated)
        self.assertNotIn("experimental_use_rmcp_client", generated)
        self.assertNotIn("mcp_oauth_refresh_coordination", generated)

    def test_unsupported_rmcp_feature_is_removed_when_mcp_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "[features]\nrmcp_client = false\n", encoding="utf-8"
            )
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_CONFIG_PATH": str(config_path),
                    "CAGE_REMOTE_MCP_SERVERS": (
                        '[{"name":"docs","type":"http",'
                        '"url":"https://docs.example/mcp","auth":"none"}]'
                    ),
                }
            )

            result = subprocess.run(
                ["python3", "-c", self.entrypoint_python()],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            generated = config_path.read_text(encoding="utf-8")

        self.assertNotIn("rmcp_client", generated)
        self.assertNotIn("experimental_use_rmcp_client", generated)
        self.assertIn('[mcp_servers."docs"]', generated)


if __name__ == "__main__":
    unittest.main()
