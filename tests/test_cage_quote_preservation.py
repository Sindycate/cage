import re
import subprocess
from pathlib import Path


def test_trim_preserving_quotes_keeps_quoted_mcp_command_args():
    src = Path("cage").read_text()
    match = re.search(r"^_trim_preserving_quotes\(\) \{\n.*?^}\n", src, re.M | re.S)
    assert match

    command = (
        'databricks-uc=npx -y mcp-remote https://example.com/mcp '
        '--header "Authorization: Bearer $(databricks auth token)"'
    )
    script = (
        match.group(0)
        + "\n"
        + f"line='  {command}  '\n"
        + 'printf "%s" "$(_trim_preserving_quotes "$line")"\n'
    )

    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout == command
