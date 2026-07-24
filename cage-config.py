#!/usr/bin/env python3
"""Central TOML configuration helper for cage.

The shell launcher stays responsible for Docker/process orchestration. This
helper owns parsing, validating, explaining, and resolving user configuration
into shell-safe assignments.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on host Python
    print("ERROR: cage central config requires Python 3.11+ with tomllib.", file=sys.stderr)
    sys.exit(2)


ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")
TRANSPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HEADER_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
TABLE_RE = re.compile(r"^\s*\[\[?[^\]\r\n]+\]\]?\s*(?:#.*)?$")
PROJECTS_TABLE_RE = re.compile(r"^\s*\[projects\]\s*(?:#.*)?$")

TOP_LEVEL_KEYS = {
    "version",
    "default_preset",
    "defaults",
    "auth",
    "identities",
    "mcp_packs",
    "skill_packs",
    "host_commands",
    "presets",
    "projects",
}
DEFAULT_KEYS = {"default_preset", "net", "session_sync"}
AUTH_KEYS = {
    "tool",
    "env",
    "mode",
    "aws_profile",
    "aws_region",
    "host_codex_dir",
    "host_agents_dir",
    "copy_auth",
    "codex_copy_auth",
}
IDENTITY_KEYS = {
    "git_user_name",
    "git_user_email",
    "ssh_key",
    "ssh_host",
    "gh_auth",
    "gh_account",
}
MCP_PACK_KEYS = {"env", "servers"}
MCP_SERVER_KEYS = {
    "name",
    "type",
    "command",
    "url",
    "auth",
    "bearer_token_env_var",
    "oauth_resource",
    "oauth_scopes",
    "oauth_client_id",
    "oauth_client_id_env_var",
    "headers",
    "env",
}
SKILL_PACK_KEYS = {"source", "skills"}
HOST_COMMAND_KEYS = {"command"}
PRESET_KEYS = {
    "tool",
    "auth",
    "identity",
    "target",
    "net",
    "session_sync",
    "env",
    "extra_env",
    "claude_auth",
    "aws_profile",
    "aws_region",
    "mcp_packs",
    "skill_packs",
    "host_commands",
    "extra_mounts",
    "yolo",
}

VALID_EXEC_TARGETS = {"container", "host"}

EDITABLE_COLLECTIONS = {
    "auth",
    "identities",
    "mcp_packs",
    "skill_packs",
    "host_commands",
    "presets",
}


class ConfigError(Exception):
    pass


@dataclass
class ResolvedConfig:
    config_path: Path
    repo_path: str
    preset_name: str
    preset_source: str
    tool: str
    auth_name: str = ""
    identity_name: str = ""
    mcp_pack_names: list[str] = field(default_factory=list)
    skill_pack_names: list[str] = field(default_factory=list)
    net: str = ""
    session_sync: str = ""
    yolo: str = ""
    target: str = "container"
    claude_auth: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    host_codex_dir: str = ""
    host_agents_dir: str = ""
    codex_copy_auth: str = ""
    git_user_name: str = ""
    git_user_email: str = ""
    ssh_key: str = ""
    ssh_host: str = ""
    gh_auth: str = ""
    gh_account: str = ""
    extra_env: list[str] = field(default_factory=list)
    stdio_mcp: list[dict[str, Any]] = field(default_factory=list)
    remote_mcp: list[dict[str, Any]] = field(default_factory=list)
    skill_mounts: list[dict[str, str]] = field(default_factory=list)
    host_commands: list[dict[str, str]] = field(default_factory=list)
    extra_mounts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class InteractiveSelections:
    tool: str
    auth_name: str = ""
    identity_name: str = ""
    mcp_pack_names: list[str] = field(default_factory=list)
    skill_pack_names: list[str] = field(default_factory=list)
    host_command_names: list[str] = field(default_factory=list)
    net: str = ""
    session_sync: bool | None = None
    yolo: bool | None = None


def default_config_path() -> Path:
    config_dir = os.environ.get("CAGE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "cage" / "config.toml"
    return Path.home() / ".config" / "cage" / "config.toml"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_config_text(text, path)


def parse_config_text(text: str, path: Path | str = "config") -> dict[str, Any]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("top-level config must be a TOML table")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")
    validate_schema(data)
    return data


def as_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list")
    return value


def as_str_list(value: Any, label: str) -> list[str]:
    out: list[str] = []
    for item in as_list(value, label):
        if not isinstance(item, str):
            raise ConfigError(f"{label} must contain only strings")
        out.append(item)
    return out


def require_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    if not NAME_RE.fullmatch(value):
        raise ConfigError(f"{label} has invalid characters: {value!r}")
    return value


def require_skill_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    if not SKILL_NAME_RE.fullmatch(value):
        raise ConfigError(f"{label} must contain only lowercase letters, digits, and hyphens: {value!r}")
    return value


def require_env_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ENV_RE.fullmatch(value):
        raise ConfigError(f"{label} must be an environment variable name")
    return value


def require_transport_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty string")
    if not TRANSPORT_NAME_RE.fullmatch(value):
        raise ConfigError(f"{label} must contain only letters, digits, underscores, and hyphens")
    return value


def transport_key(name: str) -> str:
    return name.upper().replace("-", "_")


def is_sensitive_header_name(name: str) -> bool:
    lower_name = name.lower()
    return (
        lower_name in {"authorization", "proxy-authorization", "cookie", "x-api-key"}
        or "token" in lower_name
        or "secret" in lower_name
        or lower_name.endswith("-key")
    )


def validate_headers(value: Any, label: str) -> tuple[dict[str, str], list[str], bool]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a table")
    output: dict[str, str] = {}
    env_names: list[str] = []
    has_sensitive = False
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not HTTP_HEADER_NAME_RE.fullmatch(raw_name):
            raise ConfigError(f"{label} contains an invalid HTTP header name: {raw_name!r}")
        if not isinstance(raw_value, str) or "\n" in raw_value or "\r" in raw_value:
            raise ConfigError(f"{label}.{raw_name} must be a single-line string")
        refs = list(HEADER_ENV_REF_RE.finditer(raw_value))
        looks_sensitive = is_sensitive_header_name(raw_name)
        has_sensitive = has_sensitive or looks_sensitive
        if looks_sensitive and not refs:
            raise ConfigError(
                f"{label}.{raw_name} must reference an environment variable instead of a literal secret"
            )
        for match in refs:
            env_names.append(require_env_name(match.group(1), f"{label}.{raw_name}"))
            if looks_sensitive and match.group(2) not in (None, ""):
                raise ConfigError(
                    f"{label}.{raw_name} cannot contain a literal fallback for a sensitive header"
                )
        output[raw_name] = raw_value
    return output, env_names, has_sensitive


def reject_unknown_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        joined = ", ".join(repr(key) for key in unknown)
        raise ConfigError(f"unknown key(s) in {label}: {joined}")


def validate_named_table(
    data: dict[str, Any],
    table_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    table = as_table(data, table_name)
    for name, value in table.items():
        require_name(name, f"{table_name} name")
        if not isinstance(value, dict):
            raise ConfigError(f"{table_name}.{name} must be a table")
        reject_unknown_keys(value, allowed_keys, f"{table_name}.{name}")
    return table


def validate_schema(data: dict[str, Any]) -> None:
    reject_unknown_keys(data, TOP_LEVEL_KEYS, "top-level config")
    defaults = as_table(data, "defaults")
    reject_unknown_keys(defaults, DEFAULT_KEYS, "defaults")
    validate_named_table(data, "auth", AUTH_KEYS)
    validate_named_table(data, "identities", IDENTITY_KEYS)
    mcp_packs = validate_named_table(data, "mcp_packs", MCP_PACK_KEYS)
    validate_named_table(data, "skill_packs", SKILL_PACK_KEYS)
    host_commands = validate_named_table(data, "host_commands", HOST_COMMAND_KEYS)
    presets = validate_named_table(data, "presets", PRESET_KEYS)
    as_table(data, "projects")

    for pack_name, pack in mcp_packs.items():
        for index, server in enumerate(as_list(pack.get("servers"), f"mcp_packs.{pack_name}.servers")):
            if not isinstance(server, dict):
                raise ConfigError(f"mcp_packs.{pack_name}.servers entries must be tables")
            reject_unknown_keys(server, MCP_SERVER_KEYS, f"mcp_packs.{pack_name}.servers[{index}]")
            require_transport_name(server.get("name"), f"mcp_packs.{pack_name}.servers[{index}].name")
            if server.get("headers") is not None:
                validate_headers(
                    server["headers"],
                    f"mcp_packs.{pack_name}.servers[{index}].headers",
                )

    for name, command in host_commands.items():
        require_transport_name(name, "host command name")
        value = command.get("command")
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise ConfigError(f"host_commands.{name}.command must be a non-empty single line")

    for preset_name, preset in presets.items():
        for index, item in enumerate(
            as_list(preset.get("host_commands"), f"presets.{preset_name}.host_commands")
        ):
            label = f"presets.{preset_name}.host_commands[{index}]"
            if isinstance(item, str):
                require_transport_name(item, label)
            elif isinstance(item, dict):
                reject_unknown_keys(item, {"name", "command"}, label)
                require_transport_name(item.get("name"), f"{label}.name")
                command = item.get("command")
                if not isinstance(command, str) or not command.strip() or "\n" in command:
                    raise ConfigError(f"{label}.command must be a non-empty single line")
            else:
                raise ConfigError(f"{label} must be a string or table")

        for index, item in enumerate(
            as_list(preset.get("extra_mounts"), f"presets.{preset_name}.extra_mounts")
        ):
            label = f"presets.{preset_name}.extra_mounts[{index}]"
            if isinstance(item, str):
                if not item or "\n" in item:
                    raise ConfigError(f"{label} must be a non-empty path without newlines")
            elif isinstance(item, dict):
                reject_unknown_keys(item, {"path", "mode"}, label)
                mount_path = item.get("path")
                if not isinstance(mount_path, str) or not mount_path or "\n" in mount_path:
                    raise ConfigError(f"{label}.path must be a non-empty path without newlines")
                if item.get("mode", "ro") not in {"ro", "rw"}:
                    raise ConfigError(f"{label}.mode must be ro or rw")
            else:
                raise ConfigError(f"{label} must be a string or table")

        target = preset.get("target", "container")
        if not isinstance(target, str) or target not in VALID_EXEC_TARGETS:
            raise ConfigError(
                f"presets.{preset_name}.target must be one of: {', '.join(sorted(VALID_EXEC_TARGETS))}"
            )


def bool_to_flag(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be true or false")
    return "1" if value else "0"


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def expand_path_string(value: str) -> str:
    return str(Path(value).expanduser())


def normalize_project_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def select_project_preset(projects: dict[str, Any], repo_path: str) -> tuple[str, str]:
    best_path = ""
    best_preset = ""
    for raw_path, preset in projects.items():
        if not isinstance(raw_path, str) or not isinstance(preset, str):
            raise ConfigError("[projects] must map path strings to preset names")
        candidate = normalize_project_path(raw_path)
        if repo_path == candidate or repo_path.startswith(candidate.rstrip("/") + "/"):
            if len(candidate) > len(best_path):
                best_path = candidate
                best_preset = preset
    return best_preset, best_path


def collect_env(target: list[str], value: Any, label: str) -> None:
    for env_name in as_str_list(value, label):
        target.append(require_env_name(env_name, label))


def optional_str(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    return value


def selected_seed_preset(data: dict[str, Any], repo: str) -> tuple[str, dict[str, Any]]:
    repo_path = normalize_project_path(repo)
    presets = as_table(data, "presets")
    projects = as_table(data, "projects")
    defaults = as_table(data, "defaults")
    preset_name, _ = select_project_preset(projects, repo_path)
    if not preset_name:
        preset_name = data.get("default_preset") or defaults.get("default_preset") or ""
    if isinstance(preset_name, str):
        preset = presets.get(preset_name)
        if isinstance(preset, dict):
            return preset_name, preset
    return "", {}


def preset_tool(data: dict[str, Any], preset: dict[str, Any]) -> str:
    tool = preset.get("tool")
    if tool in {"claude", "codex"}:
        return str(tool)
    auth_name = preset.get("auth")
    if isinstance(auth_name, str):
        auth = as_table(data, "auth").get(auth_name, {})
        if isinstance(auth, dict) and auth.get("tool") in {"claude", "codex"}:
            return str(auth["tool"])
    return ""


def str_list_or_empty(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def valid_named_tables(table: dict[str, Any]) -> list[str]:
    return sorted(name for name, value in table.items() if isinstance(name, str) and isinstance(value, dict))


def matching_auth_names(data: dict[str, Any], tool: str) -> list[str]:
    auths = as_table(data, "auth")
    names: list[str] = []
    for name in valid_named_tables(auths):
        auth_tool = auths[name].get("tool")
        if auth_tool in (None, "", tool):
            names.append(name)
    return names


def default_net(data: dict[str, Any], seed_preset: dict[str, Any], yolo: bool) -> str:
    defaults = as_table(data, "defaults")
    net = seed_preset.get("net") or defaults.get("net") or ("gate" if yolo else "open")
    return str(net) if net in {"open", "gate", "off"} else ("gate" if yolo else "open")


def default_session_sync(data: dict[str, Any], seed_preset: dict[str, Any]) -> bool:
    defaults = as_table(data, "defaults")
    value = seed_preset.get("session_sync", defaults.get("session_sync"))
    return value if isinstance(value, bool) else True


def open_tty():
    try:
        read_fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            write_fd = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            os.close(read_fd)
            raise
    except OSError as exc:
        raise ConfigError("interactive mode requires a TTY") from exc

    @contextmanager
    def tty_context():
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            with os.fdopen(write_fd, "w", buffering=1, encoding="utf-8") as writer:
                yield TtyIO(reader, writer)

    return tty_context()


class TtyIO:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    def write(self, value: str) -> int:
        return self.writer.write(value)

    def flush(self) -> None:
        self.writer.flush()

    def readline(self) -> str:
        return self.reader.readline()


def prompt_single(tty, title: str, choices: list[tuple[str, str]], default_value: str) -> str:
    if not choices:
        raise ConfigError(f"no choices available for {title.lower()}")
    default_index = 0
    for i, (value, _) in enumerate(choices):
        if value == default_value:
            default_index = i
            break

    while True:
        print("", file=tty)
        print(title, file=tty)
        for i, (_, label) in enumerate(choices, start=1):
            print(f"  {i}) {label}", file=tty)
        print(f"Choice [{default_index + 1}]: ", end="", file=tty, flush=True)
        answer = tty.readline()
        if answer == "":
            raise ConfigError("interactive input aborted")
        answer = answer.strip()
        if not answer:
            return choices[default_index][0]
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][0]
        print("Invalid choice.", file=tty)


def prompt_multi(
    tty,
    title: str,
    choices: list[tuple[str, str]],
    default_values: list[str],
) -> list[str]:
    if not choices:
        return []
    value_to_index = {value: i for i, (value, _) in enumerate(choices)}
    default_indices = [value_to_index[value] for value in default_values if value in value_to_index]
    default_text = ",".join(str(i + 1) for i in default_indices) if default_indices else "none"

    while True:
        print("", file=tty)
        print(title, file=tty)
        print("  0) none", file=tty)
        for i, (_, label) in enumerate(choices, start=1):
            print(f"  {i}) {label}", file=tty)
        print(f"Selection [{default_text}]: ", end="", file=tty, flush=True)
        answer = tty.readline()
        if answer == "":
            raise ConfigError("interactive input aborted")
        answer = answer.strip().lower()
        if not answer:
            return [choices[i][0] for i in default_indices]
        if answer in {"0", "none", "no"}:
            return []
        if answer == "all":
            return [value for value, _ in choices]
        parts = [part for part in re.split(r"[\s,]+", answer) if part]
        if not parts or any(not part.isdigit() or int(part) < 1 or int(part) > len(choices) for part in parts):
            print("Invalid selection.", file=tty)
            continue
        selected: list[str] = []
        for part in parts:
            value = choices[int(part) - 1][0]
            if value not in selected:
                selected.append(value)
        return selected


def prompt_yes_no(tty, title: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        print("", file=tty)
        print(f"{title} [{suffix}]: ", end="", file=tty, flush=True)
        answer = tty.readline()
        if answer == "":
            raise ConfigError("interactive input aborted")
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Enter yes or no.", file=tty)


def mcp_pack_label(name: str, pack: dict[str, Any]) -> str:
    servers = []
    raw_servers = pack.get("servers")
    if isinstance(raw_servers, list):
        for server in raw_servers:
            if isinstance(server, dict) and isinstance(server.get("name"), str):
                servers.append(server["name"])
    return f"{name} ({', '.join(servers)})" if servers else name


def skill_pack_label(name: str, pack: dict[str, Any]) -> str:
    skills = str_list_or_empty(pack.get("skills"))
    return f"{name} ({', '.join(skills)})" if skills else name


def host_command_label(name: str, command_def: dict[str, Any]) -> str:
    command = command_def.get("command")
    return f"{name} ({command})" if isinstance(command, str) and command else name


def build_interactive_preset(selections: InteractiveSelections) -> dict[str, Any]:
    if selections.tool not in {"claude", "codex"}:
        raise ConfigError("interactive selection must choose tool 'claude' or 'codex'")
    preset: dict[str, Any] = {"tool": selections.tool}
    if selections.auth_name:
        preset["auth"] = selections.auth_name
    if selections.identity_name:
        preset["identity"] = selections.identity_name
    if selections.mcp_pack_names:
        preset["mcp_packs"] = selections.mcp_pack_names
    if selections.skill_pack_names:
        preset["skill_packs"] = selections.skill_pack_names
    if selections.host_command_names:
        preset["host_commands"] = selections.host_command_names
    if selections.net:
        preset["net"] = selections.net
    if selections.session_sync is not None:
        preset["session_sync"] = selections.session_sync
    if selections.yolo is not None:
        preset["yolo"] = selections.yolo
    return preset


def resolve_interactive_selection(
    data: dict[str, Any],
    config_path: Path,
    repo: str,
    selections: InteractiveSelections,
    explicit_tool: str = "",
) -> ResolvedConfig:
    if explicit_tool and selections.tool != explicit_tool:
        raise ConfigError(
            f"interactive selection chose {selections.tool}, but command requested {explicit_tool}"
        )
    interactive_data = copy.deepcopy(data)
    raw_presets = interactive_data.get("presets")
    if raw_presets is None:
        raw_presets = {}
        interactive_data["presets"] = raw_presets
    if not isinstance(raw_presets, dict):
        raise ConfigError("[presets] must be a table")
    presets = raw_presets
    presets["interactive"] = build_interactive_preset(selections)
    resolved = resolve_config(
        interactive_data,
        config_path,
        repo,
        preset_name="interactive",
        explicit_tool=explicit_tool,
    )
    resolved.preset_source = "interactive"
    return resolved


def interactive_select(
    data: dict[str, Any],
    repo: str,
    explicit_tool: str = "",
    explicit_net: str = "",
    yolo_override: bool | None = None,
) -> InteractiveSelections:
    seed_name, seed_preset = selected_seed_preset(data, repo)
    seed_tool = preset_tool(data, seed_preset)
    seed_yolo = seed_preset.get("yolo") is True
    yolo = yolo_override if yolo_override is not None else seed_yolo
    seed_for_tool = seed_preset if not seed_tool or seed_tool == (explicit_tool or seed_tool) else {}

    with open_tty() as tty:
        print("Interactive cage launch", file=tty)
        if seed_name:
            print(f"Defaults seeded from preset: {seed_name}", file=tty)

        if explicit_tool:
            tool = explicit_tool
            print(f"Tool: {tool}", file=tty)
        else:
            tool_default = seed_tool if seed_tool else "codex"
            tool = prompt_single(
                tty,
                "Select tool",
                [("codex", "codex"), ("claude", "claude")],
                tool_default,
            )
            seed_for_tool = seed_preset if not seed_tool or seed_tool == tool else {}

        auth_names = matching_auth_names(data, tool)
        auth_choices = [("", "none/default")] + [(name, name) for name in auth_names]
        seed_auth = seed_for_tool.get("auth")
        if not isinstance(seed_auth, str) or seed_auth not in auth_names:
            seed_auth = auth_names[0] if len(auth_names) == 1 else ""
        auth_name = prompt_single(tty, "Select auth", auth_choices, seed_auth)

        identities = as_table(data, "identities")
        identity_names = valid_named_tables(identities)
        identity_choices = [("", "none")] + [(name, name) for name in identity_names]
        seed_identity = seed_for_tool.get("identity")
        if not isinstance(seed_identity, str) or seed_identity not in identity_names:
            seed_identity = ""
        identity_name = prompt_single(tty, "Select identity", identity_choices, seed_identity)

        mcp_packs = as_table(data, "mcp_packs")
        mcp_choices = [(name, mcp_pack_label(name, mcp_packs[name])) for name in valid_named_tables(mcp_packs)]
        mcp_pack_names = prompt_multi(
            tty,
            "Select MCP packs (comma-separated, 0 for none, all for every pack)",
            mcp_choices,
            str_list_or_empty(seed_for_tool.get("mcp_packs")),
        )

        skill_pack_names: list[str] = []
        if tool == "codex":
            skill_packs = as_table(data, "skill_packs")
            skill_choices = [
                (name, skill_pack_label(name, skill_packs[name]))
                for name in valid_named_tables(skill_packs)
            ]
            skill_pack_names = prompt_multi(
                tty,
                "Select skill packs (comma-separated, 0 for none, all for every pack)",
                skill_choices,
                str_list_or_empty(seed_for_tool.get("skill_packs")),
            )

        host_command_defs = as_table(data, "host_commands")
        host_command_choices = [
            (name, host_command_label(name, host_command_defs[name]))
            for name in valid_named_tables(host_command_defs)
        ]
        host_command_names = prompt_multi(
            tty,
            "Select host commands (comma-separated, 0 for none, all for every command)",
            host_command_choices,
            str_list_or_empty(seed_for_tool.get("host_commands")),
        )

        if explicit_net:
            net = explicit_net
            print(f"Network: {net}", file=tty)
        else:
            net = prompt_single(
                tty,
                "Select network mode",
                [("gate", "gate (domain-gated)"), ("open", "open"), ("off", "off")],
                default_net(data, seed_preset, yolo),
            )

        session_sync = None
        if tool == "claude":
            session_sync = prompt_yes_no(
                tty,
                "Enable Claude session history sync",
                default_session_sync(data, seed_for_tool),
            )

    return InteractiveSelections(
        tool=tool,
        auth_name=auth_name,
        identity_name=identity_name,
        mcp_pack_names=mcp_pack_names,
        skill_pack_names=skill_pack_names,
        host_command_names=host_command_names,
        net=net,
        session_sync=session_sync,
        yolo=yolo,
    )


def resolve_config(
    data: dict[str, Any],
    config_path: Path,
    repo: str,
    preset_name: str = "",
    explicit_tool: str = "",
) -> ResolvedConfig:
    repo_path = normalize_project_path(repo)
    defaults = as_table(data, "defaults")
    presets = as_table(data, "presets")
    auths = as_table(data, "auth")
    identities = as_table(data, "identities")
    mcp_packs = as_table(data, "mcp_packs")
    skill_packs = as_table(data, "skill_packs")
    host_command_defs = as_table(data, "host_commands")
    projects = as_table(data, "projects")

    preset_source = "flag"
    matched_project = ""
    if not preset_name:
        preset_name, matched_project = select_project_preset(projects, repo_path)
        preset_source = f"project:{matched_project}" if preset_name else "default"
    if not preset_name:
        preset_name = data.get("default_preset") or defaults.get("default_preset") or ""
    if not isinstance(preset_name, str) or not preset_name:
        raise ConfigError("no preset selected; set default_preset, add a [projects] mapping, or pass --preset")
    require_name(preset_name, "preset name")

    preset = presets.get(preset_name)
    if not isinstance(preset, dict):
        raise ConfigError(f"preset not found: {preset_name}")

    auth_name = preset.get("auth", "")
    if auth_name is not None and not isinstance(auth_name, str):
        raise ConfigError(f"presets.{preset_name}.auth must be a string")
    if auth_name and auth_name not in auths:
        raise ConfigError(f"auth not found: {auth_name}")
    auth = auths.get(auth_name, {}) if auth_name else {}
    if auth_name and not isinstance(auth, dict):
        raise ConfigError(f"auth {auth_name!r} must be a table")

    tool = preset.get("tool") or auth.get("tool") or ""
    if not isinstance(tool, str) or tool not in {"claude", "codex"}:
        raise ConfigError(f"preset {preset_name!r} must resolve to tool 'claude' or 'codex'")
    if explicit_tool and explicit_tool != tool:
        raise ConfigError(
            f"preset {preset_name!r} is for {tool}, but command requested {explicit_tool}; "
            "choose a matching preset"
        )
    if auth.get("tool") and auth.get("tool") != tool:
        raise ConfigError(f"auth {auth_name!r} is for {auth.get('tool')}, but preset uses {tool}")

    resolved = ResolvedConfig(
        config_path=config_path,
        repo_path=repo_path,
        preset_name=preset_name,
        preset_source=preset_source,
        tool=tool,
        auth_name=auth_name or "",
    )

    resolved.net = str(preset.get("net") or defaults.get("net") or "")
    if resolved.net and resolved.net not in {"open", "gate", "off"}:
        raise ConfigError(f"invalid net mode for preset {preset_name!r}: {resolved.net}")
    resolved.session_sync = bool_to_flag(
        preset.get("session_sync", defaults.get("session_sync")),
        f"presets.{preset_name}.session_sync",
    )
    resolved.yolo = bool_to_flag(preset.get("yolo"), f"presets.{preset_name}.yolo")

    target = preset.get("target", "container")
    if not isinstance(target, str) or target not in VALID_EXEC_TARGETS:
        raise ConfigError(
            f"presets.{preset_name}.target must be one of: {', '.join(sorted(VALID_EXEC_TARGETS))}"
        )
    resolved.target = target
    if target == "host" and tool != "codex":
        raise ConfigError(
            f"preset {preset_name!r}: host execution is only supported for Codex, not {tool!r}"
        )

    identity_name = preset.get("identity", "")
    if identity_name is not None and not isinstance(identity_name, str):
        raise ConfigError(f"presets.{preset_name}.identity must be a string")
    if identity_name and identity_name not in identities:
        raise ConfigError(f"identity not found: {identity_name}")
    identity = identities.get(identity_name, {}) if identity_name else {}
    if identity_name and not isinstance(identity, dict):
        raise ConfigError(f"identity {identity_name!r} must be a table")
    resolved.identity_name = identity_name or ""

    env: list[str] = []
    collect_env(env, auth.get("env"), f"auth.{auth_name}.env")
    collect_env(env, preset.get("env"), f"presets.{preset_name}.env")
    collect_env(env, preset.get("extra_env"), f"presets.{preset_name}.extra_env")

    if tool == "claude":
        mode = auth.get("mode") or preset.get("claude_auth") or "bedrock"
        if mode not in {"bedrock", "api-key"}:
            raise ConfigError(f"unsupported Claude auth mode for {auth_name or preset_name!r}: {mode}")
        resolved.claude_auth = mode
        resolved.aws_profile = str(auth.get("aws_profile") or preset.get("aws_profile") or "")
        resolved.aws_region = str(auth.get("aws_region") or preset.get("aws_region") or "")
    else:
        if auth.get("host_codex_dir"):
            resolved.host_codex_dir = expand_path_string(str(auth["host_codex_dir"]))
        if auth.get("host_agents_dir"):
            resolved.host_agents_dir = expand_path_string(str(auth["host_agents_dir"]))
        copy_auth = auth.get("copy_auth", auth.get("codex_copy_auth"))
        resolved.codex_copy_auth = bool_to_flag(copy_auth, f"auth.{auth_name}.copy_auth")

    resolved.git_user_name = str(identity.get("git_user_name") or "")
    resolved.git_user_email = str(identity.get("git_user_email") or "")
    resolved.ssh_key = str(identity.get("ssh_key") or "")
    resolved.ssh_host = str(identity.get("ssh_host") or "")
    gh_auth = identity.get("gh_auth")
    resolved.gh_auth = bool_to_flag(gh_auth, f"identities.{identity_name}.gh_auth")
    resolved.gh_account = str(identity.get("gh_account") or "")

    pack_names = as_str_list(preset.get("mcp_packs"), f"presets.{preset_name}.mcp_packs")
    seen_servers: set[str] = set()
    seen_server_keys: dict[str, str] = {}
    for pack_name in pack_names:
        require_name(pack_name, "mcp pack name")
        pack = mcp_packs.get(pack_name)
        if not isinstance(pack, dict):
            raise ConfigError(f"mcp pack not found: {pack_name}")
        resolved.mcp_pack_names.append(pack_name)
        collect_env(env, pack.get("env"), f"mcp_packs.{pack_name}.env")
        for server in as_list(pack.get("servers"), f"mcp_packs.{pack_name}.servers"):
            if not isinstance(server, dict):
                raise ConfigError(f"mcp_packs.{pack_name}.servers entries must be tables")
            name = require_transport_name(server.get("name"), f"mcp server in {pack_name}.name")
            if name in seen_servers:
                raise ConfigError(f"duplicate MCP server name across selected packs: {name}")
            seen_servers.add(name)
            normalized_name = transport_key(name)
            if normalized_name in seen_server_keys:
                raise ConfigError(
                    "MCP server names collide after relay normalization: "
                    f"{seen_server_keys[normalized_name]!r} and {name!r}"
                )
            seen_server_keys[normalized_name] = name
            server_type = server.get("type", "stdio")
            if server_type == "stdio":
                if server.get("auth") == "oauth":
                    raise ConfigError(f"OAuth MCP server {name!r} must use type = \"http\"")
                command = server.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ConfigError(f"stdio MCP server {name!r} requires command")
                if "\n" in command:
                    raise ConfigError(f"stdio MCP server {name!r} command cannot contain newlines")
                resolved.stdio_mcp.append({"name": name, "command": command.strip()})
            elif server_type == "http":
                url = server.get("url")
                if not isinstance(url, str) or not url:
                    raise ConfigError(f"http MCP server {name!r} requires url")
                parsed_url = urlsplit(url)
                if (
                    parsed_url.scheme not in {"http", "https"}
                    or not parsed_url.hostname
                    or parsed_url.username is not None
                    or parsed_url.password is not None
                    or parsed_url.fragment
                ):
                    raise ConfigError(
                        f"http MCP server {name!r} requires an http(s) URL without credentials or fragment"
                    )
                out = {"name": name, "type": "http", "url": url}
                server_auth = server.get("auth", "")
                if server_auth is None:
                    server_auth = ""
                if not isinstance(server_auth, str):
                    raise ConfigError(f"mcp server {name}.auth must be a string")
                if server_auth and server_auth not in {"oauth"}:
                    raise ConfigError(f"unsupported auth for http MCP server {name!r}: {server_auth}")
                bearer = server.get("bearer_token_env_var")
                if server_auth == "oauth" and bearer:
                    raise ConfigError(f"http MCP server {name!r} cannot combine OAuth and bearer_token_env_var")
                if bearer:
                    out["bearer_token_env_var"] = require_env_name(
                        bearer, f"mcp server {name}.bearer_token_env_var"
                    )
                    env.append(out["bearer_token_env_var"])
                if (bearer or server_auth == "oauth") and parsed_url.scheme != "https":
                    raise ConfigError(f"authenticated HTTP MCP server {name!r} must use https")
                if server_auth == "oauth":
                    out["auth"] = "oauth"
                    oauth_resource = optional_str(
                        server.get("oauth_resource"),
                        f"mcp server {name}.oauth_resource",
                    )
                    if oauth_resource:
                        out["oauth_resource"] = oauth_resource
                    client_id = optional_str(
                        server.get("oauth_client_id"),
                        f"mcp server {name}.oauth_client_id",
                    )
                    client_env = server.get("oauth_client_id_env_var")
                    if client_id and client_env:
                        raise ConfigError(
                            f"http MCP server {name!r} cannot combine "
                            "oauth_client_id and oauth_client_id_env_var"
                        )
                    if client_id:
                        out["oauth_client_id"] = client_id
                    if client_env:
                        out["oauth_client_id_env_var"] = require_env_name(
                            client_env, f"mcp server {name}.oauth_client_id_env_var"
                        )
                        env.append(out["oauth_client_id_env_var"])
                    scopes = as_str_list(
                        server.get("oauth_scopes"),
                        f"mcp server {name}.oauth_scopes",
                    )
                    if scopes:
                        out["oauth_scopes"] = scopes
                headers = server.get("headers")
                if headers is not None:
                    if tool == "codex":
                        raise ConfigError(
                            f"mcp server {name}.headers are not supported for Codex presets; "
                            "use bearer_token_env_var or OAuth"
                        )
                    out["headers"], header_env, sensitive_headers = validate_headers(
                        headers, f"mcp server {name}.headers"
                    )
                    env.extend(header_env)
                    if sensitive_headers and parsed_url.scheme != "https":
                        raise ConfigError(
                            f"MCP server {name!r} with sensitive headers must use https"
                        )
                collect_env(env, server.get("env"), f"mcp server {name}.env")
                resolved.remote_mcp.append(out)
            else:
                raise ConfigError(f"unsupported MCP server type for {name!r}: {server_type}")

    skill_pack_names = as_str_list(preset.get("skill_packs"), f"presets.{preset_name}.skill_packs")
    if skill_pack_names and tool != "codex":
        raise ConfigError("skill_packs are only supported for Codex presets")
    seen_skills: set[str] = set()
    for pack_name in skill_pack_names:
        require_name(pack_name, "skill pack name")
        pack = skill_packs.get(pack_name)
        if not isinstance(pack, dict):
            raise ConfigError(f"skill pack not found: {pack_name}")
        resolved.skill_pack_names.append(pack_name)
        source_raw = pack.get("source") or resolved.host_agents_dir or "~/.agents"
        if not isinstance(source_raw, str) or not source_raw:
            raise ConfigError(f"skill_packs.{pack_name}.source must be a string")
        if "\n" in source_raw:
            raise ConfigError(f"skill_packs.{pack_name}.source cannot contain newlines")
        source = Path(expand_path_string(source_raw))
        skills = as_str_list(pack.get("skills"), f"skill_packs.{pack_name}.skills")
        if not skills:
            raise ConfigError(f"skill_packs.{pack_name}.skills must list at least one skill")
        for raw_skill_name in skills:
            skill_name = require_skill_name(raw_skill_name, f"skill in pack {pack_name}")
            if skill_name in seen_skills:
                raise ConfigError(f"duplicate skill name across selected packs: {skill_name}")
            seen_skills.add(skill_name)
            skill_dir = source / "skills" / skill_name
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                raise ConfigError(f"selected skill {skill_name!r} is missing SKILL.md at {skill_md}")
            resolved.skill_mounts.append({"name": skill_name, "path": str(skill_dir)})

    seen_host_commands: set[str] = set()
    seen_host_command_keys: dict[str, str] = {}
    for item in as_list(preset.get("host_commands"), f"presets.{preset_name}.host_commands"):
        if isinstance(item, str):
            require_transport_name(item, "host command name")
            cmd_def = host_command_defs.get(item)
            if not isinstance(cmd_def, dict):
                raise ConfigError(f"host command not found: {item}")
            command = cmd_def.get("command")
            name = item
        elif isinstance(item, dict):
            reject_unknown_keys(item, {"name", "command"}, "inline host command")
            name = require_transport_name(item.get("name"), "inline host command name")
            command = item.get("command")
        else:
            raise ConfigError(f"presets.{preset_name}.host_commands entries must be strings or tables")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"host command {name!r} requires command")
        if "\n" in command:
            raise ConfigError(f"host command {name!r} command cannot contain newlines")
        if name in seen_host_commands:
            raise ConfigError(f"duplicate host command in preset {preset_name!r}: {name}")
        seen_host_commands.add(name)
        normalized_name = transport_key(name)
        if normalized_name in seen_host_command_keys:
            raise ConfigError(
                "host command names collide after relay normalization: "
                f"{seen_host_command_keys[normalized_name]!r} and {name!r}"
            )
        seen_host_command_keys[normalized_name] = name
        resolved.host_commands.append({"name": name, "command": command.strip()})

    for item in as_list(preset.get("extra_mounts"), f"presets.{preset_name}.extra_mounts"):
        if isinstance(item, str):
            if not item or "\n" in item:
                raise ConfigError(
                    f"presets.{preset_name}.extra_mounts entries must be non-empty paths without newlines"
                )
            resolved.extra_mounts.append(item)
        elif isinstance(item, dict):
            reject_unknown_keys(item, {"path", "mode"}, f"presets.{preset_name}.extra_mounts entry")
            path = item.get("path")
            if not isinstance(path, str) or not path or "\n" in path:
                raise ConfigError(f"presets.{preset_name}.extra_mounts entry requires path")
            mode = item.get("mode", "ro")
            if mode not in {"ro", "rw"}:
                raise ConfigError(f"extra mount {path!r} mode must be ro or rw")
            resolved.extra_mounts.append(f"{mode}={path}" if mode == "rw" else path)
        else:
            raise ConfigError(f"presets.{preset_name}.extra_mounts entries must be strings or tables")

    resolved.extra_env = dedupe(env)
    return resolved


def shell_assign(name: str, value: Any) -> str:
    if value is None:
        value = ""
    return f"{name}={shlex.quote(str(value))}"


def emit_shell(resolved: ResolvedConfig) -> None:
    mcp_servers = "\n".join(f"{s['name']}={s['command']}" for s in resolved.stdio_mcp)
    skill_mounts = "\n".join(f"{s['name']}={s['path']}" for s in resolved.skill_mounts)
    host_commands = "\n".join(f"{c['name']}={c['command']}" for c in resolved.host_commands)
    extra_mounts = "\n".join(resolved.extra_mounts)
    remote_json = json.dumps(resolved.remote_mcp, separators=(",", ":"))
    assignments = {
        "CAGE_PRESET": resolved.preset_name,
        "CAGE_TOOL_RESOLVED": resolved.tool,
        "CAGE_NET_MODE": resolved.net,
        "CAGE_REMOTE_MCP_SERVERS": remote_json if resolved.remote_mcp else "",
        "CLAUDE_AUTH": resolved.claude_auth,
        "AWS_PROFILE": resolved.aws_profile,
        "AWS_REGION": resolved.aws_region,
        "HOST_CODEX_DIR": resolved.host_codex_dir,
        "HOST_AGENTS_DIR": resolved.host_agents_dir,
        "CODEX_COPY_AUTH": resolved.codex_copy_auth,
        "GIT_USER_NAME": resolved.git_user_name,
        "GIT_USER_EMAIL": resolved.git_user_email,
        "SSH_KEY": resolved.ssh_key,
        "SSH_HOST": resolved.ssh_host,
        "GH_AUTH": resolved.gh_auth,
        "GH_ACCOUNT": resolved.gh_account,
        "EXTRA_ENV": " ".join(resolved.extra_env),
        "MCP_SERVERS": mcp_servers,
        "SKILL_MOUNTS": skill_mounts,
        "HOST_COMMANDS": host_commands,
        "EXTRA_MOUNTS": extra_mounts,
        "SESSION_SYNC": resolved.session_sync,
        "CAGE_YOLO": resolved.yolo,
        "CAGE_EXEC_TARGET": resolved.target,
    }
    for name, value in assignments.items():
        print(shell_assign(name, value))


def effective_exec_state(resolved: ResolvedConfig) -> dict[str, str]:
    """Single source of truth for effective target, yolo, and network mode.

    Used by explain, doctor, ui_summary, and the TUI to avoid divergent
    reconstruction of the same three-way state.
    """
    target = resolved.target
    yolo = resolved.yolo == "1"
    # Network: CLI/preset/defaults > yolo gate > open (same rule as launcher)
    net = resolved.net or ("gate" if yolo else "open")
    return {"target": target, "yolo": "1" if yolo else "0", "net": net}


def host_github_token_available(resolved: ResolvedConfig) -> bool:
    """Return whether the selected host GitHub authentication can resolve.

    Token contents are discarded and never included in doctor output.
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        gh_path = Path(gh).resolve(strict=True)
        repo_path = Path(resolved.repo_path).resolve(strict=False)
    except OSError:
        return False
    if gh_path == repo_path or gh_path.is_relative_to(repo_path):
        return False
    command = [str(gh_path), "auth", "token"]
    if resolved.gh_account:
        command.extend(["-u", resolved.gh_account])
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def format_server(server: dict[str, Any]) -> str:
    if server.get("type") == "http":
        if server.get("auth") == "oauth":
            return f"{server['name']} (http {server['url']} oauth)"
        auth = server.get("bearer_token_env_var")
        suffix = f" bearer_env={auth}" if auth else ""
        return f"{server['name']} (http {server['url']}{suffix})"
    return f"{server['name']} (stdio)"


def explain(resolved: ResolvedConfig, doctor: bool = False) -> int:
    print(f"Config: {resolved.config_path}")
    print(f"Repo:   {resolved.repo_path}")
    print(f"Preset: {resolved.preset_name} ({resolved.preset_source})")
    print(f"Tool:   {resolved.tool}")
    if resolved.target != "container":
        print(f"Target: {resolved.target} (no Docker isolation)")
    if resolved.net:
        print(f"Net:    {resolved.net}")
    if resolved.yolo:
        print(f"Yolo:   {'enabled' if resolved.yolo == '1' else 'disabled'}")
    if resolved.auth_name:
        print(f"Auth:   {resolved.auth_name}")
    if resolved.identity_name:
        print(f"Identity: {resolved.identity_name}")
    if resolved.mcp_pack_names:
        print(f"MCP packs: {', '.join(resolved.mcp_pack_names)}")
    if resolved.skill_pack_names:
        print(f"Skill packs: {', '.join(resolved.skill_pack_names)}")
    if resolved.stdio_mcp or resolved.remote_mcp:
        print("MCP servers:")
        for server in resolved.stdio_mcp:
            print(f"  - {server['name']} (stdio bridge)")
        for server in resolved.remote_mcp:
            print(f"  - {format_server(server)}")
    if resolved.skill_mounts:
        print("Skills:")
        for skill in resolved.skill_mounts:
            print(f"  - {skill['name']} ({skill['path']})")
    if resolved.host_commands:
        print("Host commands:")
        for command in resolved.host_commands:
            print(f"  - {command['name']} (executes on host)")
    if resolved.extra_env:
        print("Env forwarded:")
        for name in resolved.extra_env:
            status = "set" if os.environ.get(name) else "unset"
            print(f"  - {name}: {status}")
    if resolved.host_codex_dir:
        print(f"Codex dir: {resolved.host_codex_dir}")
    if resolved.host_agents_dir:
        print(f"Agents dir: {resolved.host_agents_dir}")
    if resolved.gh_auth == "1":
        acct = f" ({resolved.gh_account})" if resolved.gh_account else ""
        print(f"GitHub CLI auth: enabled{acct}")
    if resolved.extra_mounts:
        print("Extra mounts:")
        for mount in resolved.extra_mounts:
            print(f"  - {mount}")

    print("Capabilities:")
    if resolved.target == "host":
        print("  - execution: host-native (NO Docker isolation boundary)")
    print("  - repository: read/write, including .git")
    if resolved.tool == "claude":
        print(f"  - credentials: automated Claude {resolved.claude_auth or 'configured'} auth")
    elif resolved.codex_copy_auth == "0":
        print("  - credentials: host Codex auth.json copy disabled")
    else:
        print("  - credentials: automated host Codex state/auth reuse")
    if resolved.stdio_mcp or resolved.host_commands:
        print("  - host execution: enabled by selected bridge integrations")
    else:
        print("  - host execution: no selected bridge integrations")
    if resolved.remote_mcp:
        print("  - external connectors: enabled")
    eff = effective_exec_state(resolved)
    if resolved.target == "host":
        if eff["net"] == "open":
            print("  - network: unrestricted host networking (Cage enforces nothing)")
        else:
            print(f"  - network: {eff['net']} (INCOMPATIBLE — Cage cannot enforce this without a container)")
    elif eff["net"] == "gate":
        print("  - network: proxy approval helper (deliberate bypass remains possible)")
    elif eff["net"] == "off":
        print("  - network: Docker network disabled for the main tool container")
    else:
        print("  - network: open")
    if resolved.tool == "claude" and resolved.session_sync == "1":
        print("  - host state writeback: Claude session sync enabled")
    if resolved.ssh_key:
        print(f"  - SSH private key: mounted read-only ({resolved.ssh_key})")
    if resolved.yolo == "1":
        print("  - coding-tool permission prompts: disabled")

    if not doctor:
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    if resolved.target == "host":
        if resolved.tool != "codex":
            errors.append(f"host execution is only supported for Codex, not {resolved.tool!r}")
        if resolved.stdio_mcp:
            errors.append("stdio MCP packs require container execution (target = 'container')")
        if resolved.remote_mcp:
            errors.append("remote MCP server configuration requires container execution (target = 'container')")
        if resolved.skill_mounts:
            errors.append("skill packs require container execution (target = 'container')")
        if resolved.host_commands:
            errors.append("host command bridges require container execution (target = 'container')")
        if resolved.extra_mounts:
            errors.append("extra mounts require container execution (target = 'container')")
        eff = effective_exec_state(resolved)
        if eff["net"] in ("gate", "off"):
            errors.append(
                f"Cage cannot enforce network mode {eff['net']!r} without a container; "
                "host execution has no Cage network restriction. "
                "Set net = \"open\" explicitly or remove yolo to avoid the implicit gate default"
            )
        if resolved.host_agents_dir:
            default_agents = str(Path.home() / ".agents")
            if resolved.host_agents_dir != default_agents:
                errors.append(
                    f"custom host_agents_dir {resolved.host_agents_dir!r} is not supported in host mode; "
                    "the default ~/.agents is naturally available on the host"
                )
        if resolved.ssh_host:
            errors.append(
                f"ssh_host alias {resolved.ssh_host!r} is not supported in host mode; "
                "configure the alias in host ~/.ssh/config or use container execution"
            )
        if resolved.gh_auth == "1" and not host_github_token_available(resolved):
            errors.append(
                "GitHub authentication was requested but no token can be resolved "
                "from GH_TOKEN, GITHUB_TOKEN, or host gh authentication"
            )
        codex_bin = shutil.which("codex")
        if codex_bin is None:
            errors.append("codex command not found in PATH (required for host execution)")
    if resolved.target == "container" and shutil.which("docker") is None:
        errors.append("docker command not found")
    if resolved.tool == "claude" and resolved.claude_auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY is required for Claude api-key auth")
    if resolved.tool == "claude" and resolved.claude_auth == "bedrock":
        aws_creds = Path.home() / ".aws" / "credentials"
        if not aws_creds.is_file():
            errors.append("~/.aws/credentials must be a regular file for Claude Bedrock auth")
    if resolved.host_codex_dir and not Path(resolved.host_codex_dir).is_dir():
        warnings.append(f"Codex config directory is missing or not a directory: {resolved.host_codex_dir}")
    if resolved.host_agents_dir and not Path(resolved.host_agents_dir).is_dir():
        warnings.append(f"Agents directory is missing or not a directory: {resolved.host_agents_dir}")
    if resolved.ssh_key and not Path(expand_path_string(resolved.ssh_key)).is_file():
        errors.append(f"SSH key is missing or not a regular file: {resolved.ssh_key}")
    for env_name in resolved.extra_env:
        if not os.environ.get(env_name):
            warnings.append(f"env var is unset: {env_name}")
    for mount in resolved.extra_mounts:
        raw_path = mount.removeprefix("rw=")
        if not Path(expand_path_string(raw_path)).exists():
            warnings.append(f"extra mount does not exist and will be skipped: {raw_path}")
    for command in resolved.host_commands:
        try:
            argv = shlex.split(command["command"])
        except ValueError as exc:
            errors.append(f"host command {command['name']!r} has invalid quoting: {exc}")
            continue
        if argv and "/" not in argv[0] and shutil.which(argv[0]) is None:
            warnings.append(f"host command executable not found in PATH: {argv[0]}")
        if len(argv) > 1:
            warnings.append(
                f"host command {command['name']!r} embeds fixed arguments; Cage "
                "de-duplicates an identical caller suffix for compatibility, but an "
                "executable-only command is clearer when the client supplies arguments"
            )

    if warnings:
        print("Warnings:")
        for item in warnings:
            print(f"  - {item}")
    if errors:
        print("Errors:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("Doctor: ok" if not warnings else "Doctor: ok with warnings")
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(data, args.config, args.repo, args.preset or "", args.tool or "")
    emit_shell(resolved)
    return 0


def command_interactive_resolve(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    selections = interactive_select(
        data,
        args.repo,
        explicit_tool=args.tool or "",
        explicit_net=args.net or "",
        yolo_override=True if args.yolo else False if args.no_yolo else None,
    )
    resolved = resolve_interactive_selection(
        data,
        args.config,
        args.repo,
        selections,
        explicit_tool=args.tool or "",
    )
    emit_shell(resolved)
    return 0


def command_explain(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(data, args.config, args.repo, args.preset or "", args.tool or "")
    return explain(resolved, doctor=False)


def command_doctor(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_config(data, args.config, args.repo, args.preset or "", args.tool or "")
    return explain(resolved, doctor=True)


def codex_key_segment(name: str) -> str:
    if re.match(r"^[A-Za-z0-9_-]+$", name):
        return name
    return toml_quote(name)


def codex_mcp_overrides(server: dict[str, Any], resolve_client_env: bool = True) -> list[str]:
    name = codex_key_segment(str(server["name"]))
    prefix = f"mcp_servers.{name}"
    overrides = [f"{prefix}.url={toml_quote(str(server['url']))}"]
    if server.get("oauth_resource"):
        overrides.append(f"{prefix}.oauth_resource={toml_quote(str(server['oauth_resource']))}")
    client_id = server.get("oauth_client_id") or ""
    client_env = server.get("oauth_client_id_env_var") or ""
    if client_env and resolve_client_env:
        client_id = os.environ.get(str(client_env), "")
        if not client_id:
            raise ConfigError(f"env var is unset: {client_env}")
    if client_id:
        overrides.append(f"{prefix}.oauth.client_id={toml_quote(str(client_id))}")
    return overrides


def selected_oauth_mcp_server(resolved: ResolvedConfig, name: str) -> dict[str, Any]:
    for server in resolved.remote_mcp:
        if server.get("name") == name:
            if server.get("auth") != "oauth":
                raise ConfigError(f"MCP server {name!r} is not configured for OAuth")
            return server
    raise ConfigError(f"OAuth MCP server not selected by preset: {name}")


def command_mcp_auth(args: argparse.Namespace, action: str) -> int:
    data = load_config(args.config)
    resolved = resolve_config(data, args.config, args.repo, args.preset or "", "codex")
    server = selected_oauth_mcp_server(resolved, args.name)
    codex_home = resolved.host_codex_dir or expand_path_string("~/.codex")
    Path(codex_home).mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    cmd = ["codex", "-c", 'mcp_oauth_credentials_store="file"']
    for override in codex_mcp_overrides(server, resolve_client_env=(action == "login")):
        cmd.extend(["-c", override])
    cmd.extend(["mcp", action])
    if action == "login" and server.get("oauth_scopes"):
        cmd.extend(["--scopes", ",".join(str(scope) for scope in server["oauth_scopes"])])
    cmd.append(args.name)

    print(f"Codex dir: {codex_home}", flush=True)
    return subprocess.call(cmd, env=env)


def command_mcp_login(args: argparse.Namespace) -> int:
    return command_mcp_auth(args, "login")


def command_mcp_logout(args: argparse.Namespace) -> int:
    return command_mcp_auth(args, "logout")


def command_default_tool(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    preset_name = data.get("default_preset") or as_table(data, "defaults").get("default_preset")
    if not isinstance(preset_name, str) or not preset_name:
        print("claude")
        return 0
    preset = as_table(data, "presets").get(preset_name, {})
    if isinstance(preset, dict):
        tool = preset.get("tool")
        auth_name = preset.get("auth")
        if not tool and isinstance(auth_name, str):
            auth = as_table(data, "auth").get(auth_name, {})
            if isinstance(auth, dict):
                tool = auth.get("tool")
        if tool in {"claude", "codex"}:
            print(tool)
            return 0
    print("claude")
    return 0


def command_list(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    default = data.get("default_preset") or as_table(data, "defaults").get("default_preset") or ""
    presets = as_table(data, "presets")
    projects = as_table(data, "projects")
    print(f"Config: {args.config}")
    print("Presets:")
    if presets:
        for name in sorted(presets):
            marker = " (default)" if name == default else ""
            tool = presets[name].get("tool", "") if isinstance(presets[name], dict) else ""
            suffix = f" [{tool}]" if tool else ""
            print(f"  - {name}{suffix}{marker}")
    else:
        print("  (none)")
    if projects:
        print("Projects:")
        for path, preset in sorted(projects.items()):
            print(f"  - {path} -> {preset}")
    return 0


def command_show(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    presets = as_table(data, "presets")
    preset = presets.get(args.preset)
    if not isinstance(preset, dict):
        raise ConfigError(f"preset not found: {args.preset}")
    print(f"Preset: {args.preset}")
    print(json.dumps(preset, indent=2, sort_keys=True))
    return 0


SAMPLE_CONFIG = """# cage central configuration
# This starter is intentionally minimal and uses existing host Codex state.
# Add identities, MCP packs, skill packs, host commands, and extra mounts only
# after the base launch works. See README.md for advanced examples.
version = 1
default_preset = "codex-local"

[defaults]
net = "gate"
session_sync = true

[auth.codex-local]
tool = "codex"
host_codex_dir = "~/.codex"
host_agents_dir = "~/.agents"
copy_auth = true

[presets.codex-local]
tool = "codex"
auth = "codex-local"
net = "gate"

[projects]
# "/Users/me/code/project-a" = "codex-local"
"""


def command_init(args: argparse.Namespace) -> int:
    if args.config.exists() and not args.force:
        print(f"Config already exists: {args.config}")
        print("Use --force to overwrite, or 'cage config edit'.")
        return 0
    args.config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(args.config, SAMPLE_CONFIG)
    print(f"Wrote {args.config}")
    return 0


def command_edit(args: argparse.Namespace) -> int:
    if not args.config.exists():
        command_init(argparse.Namespace(config=args.config, force=False))
    editor = os.environ.get("EDITOR") or "vi"
    try:
        editor_args = shlex.split(editor)
    except ValueError as exc:
        raise ConfigError(f"invalid EDITOR value: {exc}") from exc
    if not editor_args:
        editor_args = ["vi"]
    return subprocess.call(editor_args + [str(args.config)])


def toml_quote(value: str) -> str:
    return json.dumps(value)


def replace_projects_section(text: str, projects: dict[str, str]) -> str:
    lines = text.splitlines()
    start = None
    end = len(lines)
    header = "[projects]"
    preserved_comments: list[str] = []
    for i, line in enumerate(lines):
        if PROJECTS_TABLE_RE.fullmatch(line):
            start = i
            header = line
            for j in range(i + 1, len(lines)):
                if TABLE_RE.fullmatch(lines[j]):
                    end = j
                    break
                if not lines[j].strip() or lines[j].lstrip().startswith("#"):
                    preserved_comments.append(lines[j])
            break
    section = [header]
    section.extend(preserved_comments)
    if preserved_comments and preserved_comments[-1].strip():
        section.append("")
    for path, preset in sorted(projects.items()):
        section.append(f"{toml_quote(path)} = {toml_quote(preset)}")
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(section)
    else:
        lines = lines[:start] + section + lines[end:]
    return "\n".join(lines) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        try:
            destination = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ConfigError(f"refusing to write through broken config symlink: {path}") from exc
        if not destination.is_file():
            raise ConfigError(f"config symlink target must be a regular file: {destination}")
    else:
        destination = path
    mode = destination.stat().st_mode & 0o777 if destination.exists() else 0o600
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(mode)
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def config_destination(path: Path) -> Path:
    if not path.is_symlink():
        if path.exists() and not path.is_file():
            raise ConfigError(f"config must be a regular file: {path}")
        return path
    try:
        destination = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ConfigError(f"refusing to use broken config symlink: {path}") from exc
    if not destination.is_file():
        raise ConfigError(f"config symlink target must be a regular file: {destination}")
    return destination


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else toml_quote(value)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return toml_quote(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{toml_key(str(key))} = {toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise ConfigError(f"cannot serialize unsupported TOML value: {type(value).__name__}")


def render_table(path: tuple[str, ...], value: dict[str, Any]) -> str:
    header = ".".join(toml_key(part) for part in path)
    lines = [f"[{header}]"]
    for key, item in value.items():
        lines.append(f"{toml_key(str(key))} = {toml_value(item)}")
    return "\n".join(lines) + "\n"


def parse_header_path(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    header = stripped.split("#", 1)[0].strip()
    try:
        parsed = tomllib.loads(header + "\n")
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    cursor: Any = parsed
    while True:
        if isinstance(cursor, dict) and len(cursor) == 1:
            key, cursor = next(iter(cursor.items()))
            path.append(str(key))
        elif isinstance(cursor, list) and len(cursor) == 1:
            cursor = cursor[0]
        else:
            break
    return tuple(path)


def table_spans(text: str) -> list[tuple[tuple[str, ...], int, int]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    headers: list[tuple[tuple[str, ...], int]] = []
    for line in lines:
        offsets.append(offset)
        path = parse_header_path(line)
        if path:
            headers.append((path, offset))
        offset += len(line)
    spans: list[tuple[tuple[str, ...], int, int]] = []
    for index, (path, start) in enumerate(headers):
        end = headers[index + 1][1] if index + 1 < len(headers) else len(text)
        spans.append((path, start, end))
    return spans


def replace_table(text: str, path: tuple[str, ...], value: dict[str, Any] | None) -> str:
    matching = [span for span in table_spans(text) if span[0][: len(path)] == path]
    rendered = render_table(path, value) if value is not None else ""
    if not matching:
        if value is None:
            return text
        separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + rendered
    start = min(span[1] for span in matching)
    end = max(span[2] for span in matching)
    suffix = text[end:]
    if rendered and suffix and not rendered.endswith("\n\n"):
        rendered += "\n"
    return text[:start] + rendered + suffix


def replace_top_level_value(text: str, key: str, value: Any | None) -> str:
    first_table = min((start for _, start, _ in table_spans(text)), default=len(text))
    prefix = text[:first_table]
    suffix = text[first_table:]
    pattern = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*(?:\n|$)")
    replacement = "" if value is None else f"{key} = {toml_value(value)}\n"
    if pattern.search(prefix):
        prefix = pattern.sub(replacement, prefix, count=1)
    elif value is not None:
        prefix = replacement + prefix
    return prefix + suffix


def referenced_by(data: dict[str, Any], collection: str, name: str) -> list[str]:
    refs: list[str] = []
    presets = as_table(data, "presets")
    scalar_key = {"auth": "auth", "identities": "identity"}.get(collection)
    list_key = {
        "mcp_packs": "mcp_packs",
        "skill_packs": "skill_packs",
        "host_commands": "host_commands",
    }.get(collection)
    if collection == "presets":
        if data.get("default_preset") == name or as_table(data, "defaults").get("default_preset") == name:
            refs.append("default preset")
        refs.extend(f"project {path}" for path, preset in as_table(data, "projects").items() if preset == name)
    for preset_name, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        if scalar_key and preset.get(scalar_key) == name:
            refs.append(f"preset {preset_name}")
        if list_key:
            for item in preset.get(list_key, []):
                if item == name:
                    refs.append(f"preset {preset_name}")
    return sorted(set(refs))


def update_references(data: dict[str, Any], collection: str, old: str, new: str) -> None:
    presets = as_table(data, "presets")
    scalar_key = {"auth": "auth", "identities": "identity"}.get(collection)
    list_key = {
        "mcp_packs": "mcp_packs",
        "skill_packs": "skill_packs",
        "host_commands": "host_commands",
    }.get(collection)
    if collection == "presets":
        if data.get("default_preset") == old:
            data["default_preset"] = new
        defaults = as_table(data, "defaults")
        if defaults.get("default_preset") == old:
            defaults["default_preset"] = new
        projects = as_table(data, "projects")
        for path, preset in list(projects.items()):
            if preset == old:
                projects[path] = new
    for preset in presets.values():
        if not isinstance(preset, dict):
            continue
        if scalar_key and preset.get(scalar_key) == old:
            preset[scalar_key] = new
        if list_key and isinstance(preset.get(list_key), list):
            preset[list_key] = [new if item == old else item for item in preset[list_key]]


def validate_references(data: dict[str, Any]) -> None:
    validate_schema(data)
    presets = as_table(data, "presets")
    collections = {
        "auth": as_table(data, "auth"),
        "identity": as_table(data, "identities"),
        "mcp_packs": as_table(data, "mcp_packs"),
        "skill_packs": as_table(data, "skill_packs"),
        "host_commands": as_table(data, "host_commands"),
    }
    for preset_name, preset in presets.items():
        if not isinstance(preset, dict):
            continue
        for key in ("auth", "identity"):
            selected = preset.get(key)
            if selected and selected not in collections[key]:
                raise ConfigError(f"presets.{preset_name}.{key} references missing {key}: {selected}")
        for key in ("mcp_packs", "skill_packs", "host_commands"):
            for selected in preset.get(key, []):
                if isinstance(selected, str) and selected not in collections[key]:
                    raise ConfigError(f"presets.{preset_name}.{key} references missing object: {selected}")
    for path, preset in as_table(data, "projects").items():
        if not isinstance(path, str) or not isinstance(preset, str):
            raise ConfigError("[projects] must map path strings to preset names")
        if preset not in presets:
            raise ConfigError(f"project {path!r} references missing preset: {preset}")
    default_name = data.get("default_preset") or as_table(data, "defaults").get("default_preset")
    if default_name and default_name not in presets:
        raise ConfigError(f"default_preset references missing preset: {default_name}")


def hidden_project_preset_name(repo: str) -> str:
    path = normalize_project_path(repo)
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(path).name).strip("-") or "project"
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"__cage_project_{base}_{digest}"


def apply_ui_operations(data: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ConfigError("each UI operation must be an object")
        action = operation.get("action")
        if action == "upsert":
            collection = operation.get("collection")
            name = operation.get("name")
            if collection not in EDITABLE_COLLECTIONS:
                raise ConfigError(f"unsupported collection: {collection}")
            require_name(name, f"{collection} name")
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ConfigError("upsert value must be an object")
            updated.setdefault(collection, {})[name] = copy.deepcopy(value)
        elif action == "rename":
            collection = operation.get("collection")
            old = operation.get("name")
            new = operation.get("new_name")
            if collection not in EDITABLE_COLLECTIONS:
                raise ConfigError(f"unsupported collection: {collection}")
            require_name(old, f"{collection} name")
            require_name(new, f"{collection} new name")
            table = as_table(updated, collection)
            if old not in table:
                raise ConfigError(f"{collection} object not found: {old}")
            if new in table:
                raise ConfigError(f"{collection} object already exists: {new}")
            value = table.pop(old)
            table[new] = value
            update_references(updated, collection, old, new)
        elif action == "delete":
            collection = operation.get("collection")
            name = operation.get("name")
            if collection not in EDITABLE_COLLECTIONS:
                raise ConfigError(f"unsupported collection: {collection}")
            require_name(name, f"{collection} name")
            refs = referenced_by(updated, collection, name)
            if refs:
                raise ConfigError(f"cannot delete {collection}.{name}; referenced by: {', '.join(refs)}")
            table = as_table(updated, collection)
            if name not in table:
                raise ConfigError(f"{collection} object not found: {name}")
            del table[name]
        elif action == "set_default":
            name = operation.get("name")
            require_name(name, "default preset")
            if name not in as_table(updated, "presets"):
                raise ConfigError(f"preset not found: {name}")
            updated["default_preset"] = name
        elif action == "update_defaults":
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ConfigError("update_defaults value must be an object")
            reject_unknown_keys(value, DEFAULT_KEYS, "defaults")
            updated["defaults"] = copy.deepcopy(value)
        elif action == "set_project":
            path = normalize_project_path(str(operation.get("path", "")))
            name = operation.get("name")
            require_name(name, "project preset")
            if name not in as_table(updated, "presets"):
                raise ConfigError(f"preset not found: {name}")
            updated.setdefault("projects", {})[path] = name
        elif action == "remove_project":
            path = normalize_project_path(str(operation.get("path", "")))
            as_table(updated, "projects").pop(path, None)
        elif action == "remember_project":
            path = normalize_project_path(str(operation.get("path", "")))
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ConfigError("remember_project value must be an object")
            name = hidden_project_preset_name(path)
            projects = updated.setdefault("projects", {})
            presets = updated.setdefault("presets", {})
            existing = projects.get(path)
            if isinstance(existing, str) and existing.startswith("__cage_project_"):
                name = existing
            elif name in presets:
                suffix = 2
                candidate = f"{name}_{suffix}"
                while candidate in presets:
                    suffix += 1
                    candidate = f"{name}_{suffix}"
                name = candidate
            presets[name] = copy.deepcopy(value)
            projects[path] = name
        else:
            raise ConfigError(f"unsupported UI operation: {action}")
    validate_references(updated)
    return updated


def affected_preset_names(
    before: dict[str, Any], after: dict[str, Any], operations: list[dict[str, Any]]
) -> list[str]:
    affected: set[str] = set()
    for operation in operations:
        action = operation.get("action")
        collection = operation.get("collection")
        name = operation.get("new_name") if action == "rename" else operation.get("name")
        if collection == "presets" and isinstance(name, str):
            affected.add(name)
        elif collection in EDITABLE_COLLECTIONS and isinstance(name, str):
            affected.update(
                preset_name
                for preset_name, preset in as_table(after, "presets").items()
                if isinstance(preset, dict) and (
                    (collection == "auth" and preset.get("auth") == name)
                    or (collection == "identities" and preset.get("identity") == name)
                    or (
                        collection in {"mcp_packs", "skill_packs", "host_commands"}
                        and name in preset.get(collection, [])
                    )
                )
            )
        if action == "remember_project":
            path = normalize_project_path(str(operation.get("path", "")))
            mapped = as_table(after, "projects").get(path)
            if isinstance(mapped, str):
                affected.add(mapped)
        elif action in {"set_default", "set_project"} and isinstance(operation.get("name"), str):
            affected.add(str(operation["name"]))
        elif action == "update_defaults":
            default_name = after.get("default_preset") or as_table(after, "defaults").get("default_preset")
            if isinstance(default_name, str):
                affected.add(default_name)
    return sorted(name for name in affected if name in as_table(after, "presets"))


def validate_affected_presets(
    before: dict[str, Any], after: dict[str, Any], operations: list[dict[str, Any]],
    config_path: Path, repo: str,
) -> None:
    for name in affected_preset_names(before, after, operations):
        resolve_config(after, config_path, repo, preset_name=name)


def render_config_changes(text: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    rendered = text
    for collection in sorted(EDITABLE_COLLECTIONS):
        before_table = as_table(before, collection)
        after_table = as_table(after, collection)
        for name in sorted(set(before_table) | set(after_table)):
            if before_table.get(name) != after_table.get(name):
                value = after_table.get(name)
                rendered = replace_table(
                    rendered,
                    (collection, name),
                    value if isinstance(value, dict) else None,
                )
    if before.get("defaults", {}) != after.get("defaults", {}):
        rendered = replace_table(rendered, ("defaults",), as_table(after, "defaults"))
    if before.get("projects", {}) != after.get("projects", {}):
        rendered = replace_projects_section(rendered, as_table(after, "projects"))
    if before.get("default_preset") != after.get("default_preset"):
        rendered = replace_top_level_value(rendered, "default_preset", after.get("default_preset"))
    if not rendered.endswith("\n"):
        rendered += "\n"
    try:
        reparsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to render invalid TOML: {exc}") from exc
    if reparsed != after:
        raise ConfigError("rendered TOML does not match the validated configuration")
    return rendered


def create_config_backup(config_path: Path, text: str) -> None:
    backup_dir = config_path.parent / "backups"
    if backup_dir.is_symlink():
        raise ConfigError(f"refusing symlinked config backup directory: {backup_dir}")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not backup_dir.is_dir() or backup_dir.stat().st_uid != os.getuid():
        raise ConfigError(f"config backup directory must be owned by the current user: {backup_dir}")
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"config-{timestamp}-{sha256_text(text)[:12]}.toml"
    atomic_write_text(backup, text)
    backup.chmod(0o600)
    backups = sorted(backup_dir.glob("config-*.toml"), key=lambda item: item.name, reverse=True)
    for old in backups[10:]:
        old.unlink()


@contextmanager
def config_write_lock(path: Path):
    destination = config_destination(path)
    lock_path = destination.parent / f".{destination.name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ConfigError(f"cannot open private config lock {lock_path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigError(f"config lock must be a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield destination
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_ui_request(path: Path) -> dict[str, Any]:
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise OSError("artifact must be a regular file no larger than 1 MiB")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identity = lambda value: (value.st_dev, value.st_ino)
        if identity(before) != identity(opened) or identity(opened) != identity(current):
            os.close(descriptor)
            raise OSError("artifact changed while it was being opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid UI request artifact: {exc}") from exc
    if not isinstance(request, dict):
        raise ConfigError("UI request artifact must contain an object")
    return request


def ui_summary(data: dict[str, Any], config_path: Path, repo: str) -> dict[str, Any]:
    effective: dict[str, Any]
    try:
        resolved = resolve_config(data, config_path, repo)
        eff = effective_exec_state(resolved)
        effective = {
            "preset": resolved.preset_name,
            "source": resolved.preset_source,
            "tool": resolved.tool,
            "target": eff["target"],
            "auth": resolved.auth_name,
            "identity": resolved.identity_name,
            "net": eff["net"],
            "yolo": eff["yolo"] == "1",
            "session_sync": resolved.session_sync != "0",
            "mcp_packs": resolved.mcp_pack_names,
            "skill_packs": resolved.skill_pack_names,
            "host_commands": [item["name"] for item in resolved.host_commands],
            "extra_mounts": resolved.extra_mounts,
            "required_env": resolved.extra_env,
        }
    except ConfigError as exc:
        effective = {"error": str(exc)}
    dependencies = {
        collection: {
            name: referenced_by(data, collection, name)
            for name in as_table(data, collection)
        }
        for collection in EDITABLE_COLLECTIONS
    }
    return {"effective": effective, "dependencies": dependencies}


def command_ui_export(args: argparse.Namespace) -> int:
    destination = config_destination(args.config)
    text = destination.read_text(encoding="utf-8")
    data = parse_config_text(text, destination)
    output = {
        "config_path": str(args.config),
        "destination": str(destination),
        "repo": normalize_project_path(args.repo),
        "sha256": sha256_text(text),
        "config": data,
        **ui_summary(data, args.config, args.repo),
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


def command_ui_preview(args: argparse.Namespace) -> int:
    request = load_ui_request(args.request)
    data = load_config(args.config)
    operations = request.get("operations", [])
    if not isinstance(operations, list):
        raise ConfigError("operations must be a list")
    updated = apply_ui_operations(data, operations)
    validate_affected_presets(data, updated, operations, args.config, args.repo)
    output = {"config": updated, **ui_summary(updated, args.config, args.repo)}
    print(json.dumps(output, separators=(",", ":")))
    return 0


def command_ui_commit(args: argparse.Namespace) -> int:
    request = load_ui_request(args.request)
    expected = request.get("expected_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ConfigError("UI commit requires the opening config SHA-256")
    operations = request.get("operations", [])
    if not isinstance(operations, list):
        raise ConfigError("operations must be a list")
    with config_write_lock(args.config) as destination:
        expected_destination = request.get("expected_destination")
        if expected_destination is not None and expected_destination != str(destination):
            raise ConfigError("config symlink target changed since the TUI opened; reload before saving")
        text = destination.read_text(encoding="utf-8")
        actual = sha256_text(text)
        if actual != expected:
            raise ConfigError("config changed since the TUI opened; reload before saving")
        data = parse_config_text(text, destination)
        updated = apply_ui_operations(data, operations)
        validate_affected_presets(data, updated, operations, args.config, args.repo)
        if updated == data:
            rendered = text
        else:
            rendered = render_config_changes(text, data, updated)
            create_config_backup(args.config, text)
            atomic_write_text(destination, rendered)
    output = {
        "sha256": sha256_text(rendered),
        "config_path": str(args.config),
        "destination": str(destination),
        "repo": normalize_project_path(args.repo),
        "config": updated,
        **ui_summary(updated, args.config, args.repo),
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


def command_ui_resolve(args: argparse.Namespace) -> int:
    request = load_ui_request(args.result)
    data = load_config(args.config)
    action = request.get("action")
    if action == "preset":
        name = request.get("preset_name")
        require_name(name, "TUI preset name")
        resolved = resolve_config(data, args.config, args.repo, name, args.tool or "")
    elif action == "launch_once":
        value = request.get("preset")
        if not isinstance(value, dict):
            raise ConfigError("TUI launch result is missing its preset")
        selections_data = copy.deepcopy(data)
        selections_data.setdefault("presets", {})["__cage_launch_once"] = value
        validate_references(selections_data)
        resolved = resolve_config(
            selections_data,
            args.config,
            args.repo,
            "__cage_launch_once",
            args.tool or "",
        )
        resolved.preset_source = "tui:launch-once"
    else:
        raise ConfigError("TUI did not return a launch action")
    emit_shell(resolved)
    return 0


def command_set_project(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    presets = as_table(data, "presets")
    if args.preset not in presets:
        raise ConfigError(f"preset not found: {args.preset}")
    projects = {str(k): str(v) for k, v in as_table(data, "projects").items()}
    project_path = normalize_project_path(args.path)
    projects[project_path] = args.preset
    text = args.config.read_text(encoding="utf-8")
    updated = replace_projects_section(text, projects)
    # Validate the complete result before replacing the source file.
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to write invalid TOML: {exc}") from exc
    atomic_write_text(args.config, updated)
    print(f"Set {project_path} -> {args.preset}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cage config")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to central cage config.toml",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("resolve", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--preset")
    p.add_argument("--tool", choices=["claude", "codex"])
    p.set_defaults(func=command_resolve)

    p = sub.add_parser("interactive-resolve", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--tool", choices=["claude", "codex"])
    p.add_argument("--net", choices=["open", "gate", "off"])
    yolo_group = p.add_mutually_exclusive_group()
    yolo_group.add_argument("--yolo", action="store_true")
    yolo_group.add_argument("--no-yolo", action="store_true")
    p.set_defaults(func=command_interactive_resolve)

    p = sub.add_parser("default-tool", help=argparse.SUPPRESS)
    p.set_defaults(func=command_default_tool)

    p = sub.add_parser("init", help="Create a starter central config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=command_init)

    p = sub.add_parser("edit", help="Open the central config in $EDITOR")
    p.set_defaults(func=command_edit)

    p = sub.add_parser("list", help="List presets and project mappings")
    p.set_defaults(func=command_list)

    p = sub.add_parser("show", help="Show one preset")
    p.add_argument("preset")
    p.set_defaults(func=command_show)

    p = sub.add_parser("explain", help="Explain the resolved launch config")
    p.add_argument("--preset")
    p.add_argument("--tool", choices=["claude", "codex"])
    p.add_argument("repo")
    p.set_defaults(func=command_explain)

    p = sub.add_parser("doctor", help="Validate the resolved launch config")
    p.add_argument("--preset")
    p.add_argument("--tool", choices=["claude", "codex"])
    p.add_argument("repo")
    p.set_defaults(func=command_doctor)

    p = sub.add_parser("set-project", help="Map a project path to a preset")
    p.add_argument("path")
    p.add_argument("preset")
    p.set_defaults(func=command_set_project)

    p = sub.add_parser("ui-export", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.set_defaults(func=command_ui_export)

    p = sub.add_parser("ui-preview", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--request", type=Path, required=True)
    p.set_defaults(func=command_ui_preview)

    p = sub.add_parser("ui-commit", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--request", type=Path, required=True)
    p.set_defaults(func=command_ui_commit)

    p = sub.add_parser("ui-resolve", help=argparse.SUPPRESS)
    p.add_argument("--repo", required=True)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--tool", choices=["claude", "codex"])
    p.set_defaults(func=command_ui_resolve)

    p = sub.add_parser("mcp", help="Manage OAuth MCP authentication")
    mcp_sub = p.add_subparsers(dest="mcp_command", required=True)

    p_login = mcp_sub.add_parser("login", help="Authenticate with an OAuth MCP server")
    p_login.add_argument("--preset")
    p_login.add_argument("name")
    p_login.add_argument("repo")
    p_login.set_defaults(func=command_mcp_login)

    p_logout = mcp_sub.add_parser("logout", help="Remove OAuth MCP authentication")
    p_logout.add_argument("--preset")
    p_logout.add_argument("name")
    p_logout.add_argument("repo")
    p_logout.set_defaults(func=command_mcp_logout)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
