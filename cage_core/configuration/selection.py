"""Preset selection and shared path/default interpretation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cage_core.models import ResolvedConfig

from .schema import (
    ConfigError,
    VALID_TOOLS,
    as_table,
)


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
    if tool in VALID_TOOLS:
        return str(tool)
    auth_name = preset.get("auth")
    if isinstance(auth_name, str):
        auth = as_table(data, "auth").get(auth_name, {})
        if isinstance(auth, dict) and auth.get("tool") in VALID_TOOLS:
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
