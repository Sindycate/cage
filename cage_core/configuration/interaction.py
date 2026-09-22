"""Terminal selection prompts and temporary preset composition."""

from __future__ import annotations

import copy
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cage_core.models import ResolvedConfig

from .resolution import resolve_config
from .schema import (
    ConfigError,
    VALID_TOOLS,
    as_table,
)
from .selection import (
    default_net,
    default_session_sync,
    matching_auth_names,
    preset_tool,
    selected_seed_preset,
    str_list_or_empty,
    valid_named_tables,
)


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
    if selections.tool not in VALID_TOOLS:
        raise ConfigError(
            "interactive selection must choose tool 'claude', 'codex', or 'opencode'"
        )
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
    mcp_inventory: bool = False,
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
        mcp_inventory=mcp_inventory,
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
                [("codex", "codex"), ("claude", "claude"), ("opencode", "opencode")],
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
        if tool in {"codex", "opencode"}:
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
