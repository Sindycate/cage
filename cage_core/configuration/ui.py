"""Configuration authoring transactions and TUI launch results."""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

from cage_core.models import ResolvedConfig

from .diagnostics import emit_resolved_json
from .editing import (
    apply_ui_operations,
    referenced_by,
    validate_affected_presets,
    validate_references,
)
from .rendering import render_config_changes
from .resolution import resolve_config
from .schema import (
    ConfigError,
    EDITABLE_COLLECTIONS,
    as_table,
    parse_config_text,
    require_name,
    storage_policy_from_config,
)
from .selection import (
    effective_exec_state,
    normalize_project_path,
)
from .storage import (
    atomic_write_text,
    config_destination,
    config_write_lock,
    create_config_backup,
    load_config,
    load_ui_request,
    sha256_text,
)


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
            "codex_profile": resolved.codex_profile,
            "auth": resolved.auth_name,
            "aws_profile": resolved.aws_profile,
            "aws_access": resolved.aws_access,
            "identity": resolved.identity_name,
            "net": eff["net"],
            "yolo": eff["yolo"] == "1",
            "session_sync": resolved.session_sync != "0",
            "poketoken": resolved.poketoken,
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
    return {
        "effective": effective,
        "dependencies": dependencies,
        "storage": storage_policy_from_config(data).public_dict(),
    }


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


def resolve_ui_result(
    data: dict[str, Any],
    config_path: Path,
    repo: str,
    result_path: Path,
    explicit_tool: str = "",
    *,
    mcp_inventory: bool = True,
) -> ResolvedConfig:
    request = load_ui_request(result_path)
    action = request.get("action")
    if action == "preset":
        name = request.get("preset_name")
        require_name(name, "TUI preset name")
        resolved = resolve_config(
            data, config_path, repo, name, explicit_tool,
            mcp_inventory=mcp_inventory,
        )
    elif action == "launch_once":
        value = request.get("preset")
        if not isinstance(value, dict):
            raise ConfigError("TUI launch result is missing its preset")
        selections_data = copy.deepcopy(data)
        selections_data.setdefault("presets", {})["__cage_launch_once"] = value
        validate_references(selections_data)
        resolved = resolve_config(
            selections_data,
            config_path,
            repo,
            "__cage_launch_once",
            explicit_tool,
            mcp_inventory=mcp_inventory,
        )
        resolved.preset_source = "tui:launch-once"
    else:
        raise ConfigError("TUI did not return a launch action")
    return resolved


def command_ui_resolve_json(args: argparse.Namespace) -> int:
    data = load_config(args.config)
    resolved = resolve_ui_result(
        data,
        args.config,
        args.repo,
        args.result,
        args.tool or "",
    )
    emit_resolved_json(resolved)
    return 0
