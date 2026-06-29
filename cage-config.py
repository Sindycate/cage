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
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on host Python
    print("ERROR: cage central config requires Python 3.11+ with tomllib.", file=sys.stderr)
    sys.exit(2)


ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TABLE_RE = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")


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
    net: str = ""
    session_sync: str = ""
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
    host_commands: list[dict[str, str]] = field(default_factory=list)
    extra_mounts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class InteractiveSelections:
    tool: str
    auth_name: str = ""
    identity_name: str = ""
    mcp_pack_names: list[str] = field(default_factory=list)
    host_command_names: list[str] = field(default_factory=list)
    net: str = ""
    session_sync: bool | None = None


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
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("top-level config must be a TOML table")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")
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
    if not NAME_RE.match(value):
        raise ConfigError(f"{label} has invalid characters: {value!r}")
    return value


def require_env_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ENV_RE.match(value):
        raise ConfigError(f"{label} must be an environment variable name")
    return value


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
    if selections.host_command_names:
        preset["host_commands"] = selections.host_command_names
    if selections.net:
        preset["net"] = selections.net
    if selections.session_sync is not None:
        preset["session_sync"] = selections.session_sync
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
    yolo: bool = False,
) -> InteractiveSelections:
    seed_name, seed_preset = selected_seed_preset(data, repo)
    seed_tool = preset_tool(data, seed_preset)
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
        host_command_names=host_command_names,
        net=net,
        session_sync=session_sync,
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
            name = require_name(server.get("name"), f"mcp server in {pack_name}.name")
            if name in seen_servers:
                raise ConfigError(f"duplicate MCP server name across selected packs: {name}")
            seen_servers.add(name)
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
                    if not isinstance(headers, dict):
                        raise ConfigError(f"mcp server {name}.headers must be a table")
                    out["headers"] = {str(k): str(v) for k, v in headers.items()}
                collect_env(env, server.get("env"), f"mcp server {name}.env")
                resolved.remote_mcp.append(out)
            else:
                raise ConfigError(f"unsupported MCP server type for {name!r}: {server_type}")

    seen_host_commands: set[str] = set()
    for item in as_list(preset.get("host_commands"), f"presets.{preset_name}.host_commands"):
        if isinstance(item, str):
            require_name(item, "host command name")
            cmd_def = host_command_defs.get(item)
            if not isinstance(cmd_def, dict):
                raise ConfigError(f"host command not found: {item}")
            command = cmd_def.get("command")
            name = item
        elif isinstance(item, dict):
            name = require_name(item.get("name"), "inline host command name")
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
        resolved.host_commands.append({"name": name, "command": command.strip()})

    for item in as_list(preset.get("extra_mounts"), f"presets.{preset_name}.extra_mounts"):
        if isinstance(item, str):
            resolved.extra_mounts.append(item)
        elif isinstance(item, dict):
            path = item.get("path")
            if not isinstance(path, str) or not path:
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
        "HOST_COMMANDS": host_commands,
        "EXTRA_MOUNTS": extra_mounts,
        "SESSION_SYNC": resolved.session_sync,
    }
    for name, value in assignments.items():
        print(shell_assign(name, value))


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
    if resolved.net:
        print(f"Net:    {resolved.net}")
    if resolved.auth_name:
        print(f"Auth:   {resolved.auth_name}")
    if resolved.identity_name:
        print(f"Identity: {resolved.identity_name}")
    if resolved.mcp_pack_names:
        print(f"MCP packs: {', '.join(resolved.mcp_pack_names)}")
    if resolved.stdio_mcp or resolved.remote_mcp:
        print("MCP servers:")
        for server in resolved.stdio_mcp:
            print(f"  - {server['name']} (stdio bridge)")
        for server in resolved.remote_mcp:
            print(f"  - {format_server(server)}")
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

    if not doctor:
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    if resolved.tool == "claude" and resolved.claude_auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY is required for Claude api-key auth")
    if resolved.tool == "claude" and resolved.claude_auth == "bedrock":
        aws_creds = Path.home() / ".aws" / "credentials"
        if not aws_creds.exists():
            warnings.append("~/.aws/credentials not found for Claude Bedrock auth")
    if resolved.host_codex_dir and not Path(resolved.host_codex_dir).exists():
        warnings.append(f"Codex config directory does not exist: {resolved.host_codex_dir}")
    if resolved.host_agents_dir and not Path(resolved.host_agents_dir).exists():
        warnings.append(f"Agents directory does not exist: {resolved.host_agents_dir}")
    for env_name in resolved.extra_env:
        if not os.environ.get(env_name):
            warnings.append(f"env var is unset: {env_name}")

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
        yolo=args.yolo,
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
version = 1
default_preset = "codex-work"

[defaults]
net = "gate"
session_sync = true

[auth.codex-work]
tool = "codex"
host_codex_dir = "~/.codex-work"
host_agents_dir = "~/.agents-work"
copy_auth = true

[auth.codex-company-proxy]
tool = "codex"
host_codex_dir = "~/.codex-company"
host_agents_dir = "~/.agents-company"
copy_auth = false
env = ["COMPANY_OPENAI_API_KEY", "OPENAI_BASE_URL"]

[identities.work]
git_user_name = "Your Name"
git_user_email = "you@example.com"
gh_auth = true
gh_account = "work"

[mcp_packs.linear]
env = ["LINEAR_API_KEY"]
servers = [
  { name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" },
]

[mcp_packs.local-tools]
servers = [
  { name = "jira", type = "stdio", command = "npx -y @company/jira-mcp" },
]

[presets.codex-work]
tool = "codex"
auth = "codex-work"
identity = "work"
mcp_packs = ["linear", "local-tools"]
net = "gate"

[presets.codex-company-debug]
tool = "codex"
auth = "codex-company-proxy"
identity = "work"
mcp_packs = ["linear"]
net = "gate"

[projects]
# "/Users/me/code/project-a" = "codex-work"
"""


def command_init(args: argparse.Namespace) -> int:
    if args.config.exists() and not args.force:
        print(f"Config already exists: {args.config}")
        print("Use --force to overwrite, or 'cage config edit'.")
        return 0
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(SAMPLE_CONFIG, encoding="utf-8")
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
    for i, line in enumerate(lines):
        if line.strip() == "[projects]":
            start = i
            for j in range(i + 1, len(lines)):
                if TABLE_RE.match(lines[j]):
                    end = j
                    break
            break
    section = ["[projects]"]
    for path, preset in sorted(projects.items()):
        section.append(f"{toml_quote(path)} = {toml_quote(preset)}")
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(section)
    else:
        lines = lines[:start] + section + lines[end:]
    return "\n".join(lines) + "\n"


def command_set_project(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    presets = as_table(data, "presets")
    if args.preset not in presets:
        raise ConfigError(f"preset not found: {args.preset}")
    projects = {str(k): str(v) for k, v in as_table(data, "projects").items()}
    project_path = normalize_project_path(args.path)
    projects[project_path] = args.preset
    text = args.config.read_text(encoding="utf-8")
    args.config.write_text(replace_projects_section(text, projects), encoding="utf-8")
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
    p.add_argument("--yolo", action="store_true")
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
