from cage_core.bridge import parse_command


def test_bridge_command_parser_preserves_quoted_mcp_command_args():
    command = (
        'npx -y mcp-remote https://example.com/mcp '
        '--header "Authorization: Bearer $(databricks auth token)"'
    )

    assert parse_command(
        command,
        env_hint="preset/MCP env list",
    ) == [
        "npx",
        "-y",
        "mcp-remote",
        "https://example.com/mcp",
        "--header",
        "Authorization: Bearer $(databricks auth token)",
    ]
