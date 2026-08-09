#!/usr/bin/env python3
"""Shared selected-only Codex argument and MCP suppression policy."""

from __future__ import annotations

import json
import re
import tomllib


MAX_INVENTORY_BYTES = 4 * 1024 * 1024
INVENTORY_TIMEOUT = 60.0
INERT_STDIO_COMMAND = "/usr/bin/false"
INERT_HTTP_URL = "https://invalid.invalid/mcp"
SAFE_PASSTHROUGH_CONFIG_ROOTS = {
    "approval_policy",
    "model",
    "model_auto_compact_token_limit",
    "model_context_window",
    "model_provider",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "personality",
    "sandbox_mode",
    "sandbox_permissions",
    "sandbox_workspace_write",
    "shell_environment_policy",
    "web_search",
}
DESKTOP_APP_SERVER_CODE_MODE_HOST = {"features": {"code_mode_host": True}}


class PolicyError(ValueError):
    pass


def key_segment(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name)


def config_override_root(expression: str) -> str | None:
    quote = ""
    escaped = False
    assignment = -1
    for index, character in enumerate(expression):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = ""
            continue
        if quote == "'":
            if character == "'":
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "=":
            assignment = index
            break
    if assignment < 0:
        return None
    key = expression[:assignment].strip()
    try:
        parsed = tomllib.loads(f"{key}=0")
    except tomllib.TOMLDecodeError:
        return None
    if len(parsed) != 1:
        return None
    return next(iter(parsed))


def _app_server_index_after_config_overrides(argv: list[str]) -> int | None:
    """Return the app-server index when only config flags precede it.

    ChatGPT Desktop currently places its required code-mode-host override
    before ``app-server``. Keep this parser deliberately narrow so an
    arbitrary positional command cannot opt into the Desktop-only exception.
    """
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "app-server":
            return index
        if argument == "--":
            return None
        if argument in {"-c", "--config"}:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if (
            argument.startswith("--config=")
            or argument.startswith("-c=")
            or (argument.startswith("-c") and len(argument) > 2)
        ):
            index += 1
            continue
        return None
    return None


def _is_desktop_code_mode_host_override(expression: str) -> bool:
    try:
        return tomllib.loads(expression) == DESKTOP_APP_SERVER_CODE_MODE_HOST
    except tomllib.TOMLDecodeError:
        return False


def reject_unsafe_passthrough_args(
    argv: list[str], *, allow_desktop_code_mode_host: bool = False
) -> None:
    desktop_app_server_index = (
        _app_server_index_after_config_overrides(argv)
        if allow_desktop_code_mode_host
        else None
    )
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            break
        if (
            argument in {"-p", "--profile"}
            or argument.startswith("--profile=")
            or argument.startswith("-p=")
            or (argument.startswith("-p") and len(argument) > 2)
        ):
            raise PolicyError(
                "Codex launch arguments may not override the profile; select "
                "codex_profile in the Cage preset"
            )
        if (
            argument in {"-C", "--cd"}
            or argument.startswith("--cd=")
            or argument.startswith("-C=")
            or (argument.startswith("-C") and len(argument) > 2)
        ):
            raise PolicyError(
                "Codex launch arguments may not override the working directory; "
                "the Cage repository path is authoritative"
            )
        if (
            argument in {"--enable", "--disable"}
            or argument.startswith("--enable=")
            or argument.startswith("--disable=")
        ):
            raise PolicyError(
                "Codex launch arguments may not change feature flags because "
                "features can change MCP/plugin discovery"
            )
        if (
            argument in {"--remote", "--remote-auth-token-env"}
            or argument.startswith("--remote=")
            or argument.startswith("--remote-auth-token-env=")
        ):
            raise PolicyError(
                "Codex launch arguments may not select a remote app-server "
                "runtime because Cage did not inventory that runtime"
            )
        if argument == "--ignore-user-config":
            raise PolicyError(
                "Codex launch arguments may not ignore user configuration after "
                "Cage inventories that layer"
            )
        expression: str | None = None
        if argument in {"-c", "--config"}:
            if index + 1 < len(argv):
                expression = argv[index + 1]
                index += 1
        elif argument.startswith("--config="):
            expression = argument[len("--config=") :]
        elif argument.startswith("-c="):
            expression = argument[3:]
        elif argument.startswith("-c") and len(argument) > 2:
            expression = argument[2:]
        if expression is not None:
            root = config_override_root(expression)
            if root == "mcp_servers":
                raise PolicyError(
                    "Codex launch arguments may not override mcp_servers; define "
                    "the server in a central Cage mcp_pack and select that pack"
                )
            is_desktop_code_mode_host = (
                root == "features"
                and desktop_app_server_index is not None
                and index < desktop_app_server_index
                and _is_desktop_code_mode_host_override(expression)
            )
            if (
                root not in SAFE_PASSTHROUGH_CONFIG_ROOTS
                and not is_desktop_code_mode_host
            ):
                raise PolicyError(
                    f"Codex config override root {root!r} is not safe after MCP "
                    "inventory; move it to the selected Cage-owned Codex "
                    "configuration layer"
                )
        index += 1


def parse_toml_transports(
    raw: bytes, *, maximum_bytes: int = MAX_INVENTORY_BYTES
) -> dict[str, str] | None:
    if len(raw) > maximum_bytes:
        return None
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return None
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return None
    transports: dict[str, str] = {}
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            return None
        has_command = isinstance(conf.get("command"), str) and bool(
            conf["command"]
        )
        has_url = isinstance(conf.get("url"), str) and bool(conf["url"])
        if has_command == has_url:
            return None
        if conf.get("enabled") is not False:
            transports[str(name)] = "stdio" if has_command else "http"
    return transports


def merge_transports(
    target: dict[str, str], incoming: dict[str, str]
) -> None:
    for name, transport in incoming.items():
        previous = target.get(name)
        if previous is not None and previous != transport:
            raise PolicyError(
                f"conflicting direct transport types for MCP server {name!r}"
            )
        target[name] = transport


def parse_inventory_output(
    output: bytes,
    *,
    maximum_bytes: int = MAX_INVENTORY_BYTES,
) -> set[str]:
    try:
        if len(output) > maximum_bytes:
            raise PolicyError(
                "cannot build a trustworthy MCP inventory: output too large"
            )
        entries = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError(
            f"cannot build a trustworthy MCP inventory: {exc}"
        ) from exc
    if not isinstance(entries, list):
        raise PolicyError(
            "cannot build a trustworthy MCP inventory: expected a JSON list"
        )
    runtime_enabled: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise PolicyError(
                "cannot build a trustworthy MCP inventory: every entry must "
                "be a JSON object"
            )
        name = entry.get("name")
        enabled = entry.get("enabled")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(enabled, bool)
        ):
            raise PolicyError(
                "cannot build a trustworthy MCP inventory: every entry must "
                "contain a non-empty string name and boolean enabled flag"
            )
        if enabled:
            runtime_enabled.add(name)
    return runtime_enabled


def suppression_overrides(
    suppressed: list[str],
    runtime_enabled: set[str],
    direct_transports: dict[str, str],
) -> list[str]:
    overrides: list[str] = []
    for name in suppressed:
        if not name or "\n" in name or "\r" in name:
            raise PolicyError(f"unsafe MCP server name: {name!r}")
        key = f"mcp_servers.{key_segment(name)}"
        if name not in runtime_enabled:
            transport = direct_transports.get(name)
            if transport == "stdio":
                overrides.append(
                    f"{key}.command={json.dumps(INERT_STDIO_COMMAND)}"
                )
            elif transport == "http":
                overrides.append(
                    f"{key}.url={json.dumps(INERT_HTTP_URL)}"
                )
            else:
                raise PolicyError(
                    f"no loaded or direct transport for MCP server {name!r}"
                )
        overrides.append(f"{key}.enabled=false")
    return overrides


def selected_names(stdio_json: str, remote_json: str) -> set[str]:
    selected: set[str] = set()
    if stdio_json:
        try:
            stdio = json.loads(stdio_json)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"invalid selected stdio MCP JSON: {exc}") from exc
        if not isinstance(stdio, (dict, list)):
            raise PolicyError("selected stdio MCP JSON has an invalid shape")
        selected.update(str(name) for name in stdio)
    if remote_json:
        try:
            remote = json.loads(remote_json)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"invalid selected remote MCP JSON: {exc}") from exc
        if not isinstance(remote, list):
            raise PolicyError("selected remote MCP JSON has an invalid shape")
        for server in remote:
            if isinstance(server, dict) and server.get("name"):
                selected.add(str(server["name"]))
    return selected


def resolve_suppression(
    *,
    selected: set[str],
    enabled: set[str],
    runtime_enabled: set[str],
    direct_transports: dict[str, str],
) -> tuple[list[str], list[str]]:
    duplicates = sorted(selected & set(direct_transports))
    if duplicates:
        rendered = " ".join(
            json.dumps(name, ensure_ascii=True) for name in duplicates
        )
        raise PolicyError(
            "selected MCP server(s) already exist in a profile/project layer: "
            + rendered
        )
    suppressed = sorted(enabled - selected)
    return (
        suppression_overrides(
            suppressed,
            runtime_enabled,
            direct_transports,
        ),
        suppressed,
    )
