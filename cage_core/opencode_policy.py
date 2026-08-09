"""Fail-closed OpenCode passthrough policy for frozen Cage launches."""

from __future__ import annotations


class PolicyError(ValueError):
    """Raised when caller arguments can invalidate the prepared runtime."""


_ALLOWED_COMMANDS = {
    "agent",
    "auth",
    "export",
    "import",
    "mcp",
    "models",
    "providers",
    "run",
    "session",
    "stats",
}
_FORBIDDEN_FLAGS = {
    "--attach",
    "--auto",
    "--cors",
    "--dir",
    "--hostname",
    "--mdns",
    "--mdns-domain",
    "--port",
    "--pure",
}
_GLOBAL_VALUE_FLAGS = {
    "--agent",
    "--log-level",
    "--model",
    "--prompt",
    "--replay-limit",
    "--session",
    "-m",
    "-s",
}


def reject_unsafe_passthrough_args(
    argv: list[str], *, selected_mcp_names: set[str]
) -> None:
    """Reject arguments that can switch projects, config layers, or authority."""

    if not argv:
        return
    for argument in argv:
        root = argument.partition("=")[0]
        if root in _FORBIDDEN_FLAGS:
            if root == "--auto":
                raise PolicyError(
                    "OpenCode --auto is controlled by Cage; use cage -y/--yolo"
                )
            if root == "--pure":
                raise PolicyError(
                    "OpenCode --pure is controlled by the preset's opencode_plugins setting"
                )
            if root == "--dir":
                raise PolicyError(
                    "OpenCode working-directory overrides are not allowed through Cage"
                )
            raise PolicyError(
                "OpenCode live-server and attach overrides are not allowed through Cage"
            )

    command = ""
    index = 0
    while index < len(argv):
        argument = argv[index]
        root, separator, _value = argument.partition("=")
        if argument == "--":
            break
        if root in _GLOBAL_VALUE_FLAGS:
            if not separator:
                index += 1
                if index >= len(argv):
                    raise PolicyError(f"OpenCode option {root!r} requires a value")
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        command = argument
        break

    if command and command not in _ALLOWED_COMMANDS:
        raise PolicyError(
            f"OpenCode command or alternate project {command!r} is not managed by Cage"
        )
    if not command and any(item == "--" for item in argv):
        raise PolicyError("OpenCode alternate project arguments are not allowed through Cage")
    if command != "mcp":
        return

    try:
        command_index = argv.index("mcp")
    except ValueError:
        return
    tail = argv[command_index + 1 :]
    if not tail:
        return
    action = tail[0]
    if action == "add":
        raise PolicyError(
            "OpenCode MCP definitions are controlled by Cage mcp_packs; mcp add is disabled"
        )
    if action not in {"auth", "debug", "logout", "list", "ls"}:
        raise PolicyError(f"OpenCode MCP action {action!r} is not managed by Cage")
    if action in {"list", "ls"}:
        return
    name = next((item for item in tail[1:] if not item.startswith("-")), "")
    if not name:
        raise PolicyError(
            f"OpenCode mcp {action} requires an explicitly selected server name"
        )
    if name not in selected_mcp_names:
        raise PolicyError(
            f"OpenCode MCP server {name!r} is not selected by this Cage preset"
        )
