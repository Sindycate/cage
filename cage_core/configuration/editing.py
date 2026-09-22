"""Configuration edit operations and reference integrity."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from .resolution import resolve_config
from .schema import (
    ConfigError,
    DEFAULT_KEYS,
    EDITABLE_COLLECTIONS,
    STORAGE_KEYS,
    as_table,
    reject_unknown_keys,
    require_name,
    storage_policy_from_config,
    validate_schema,
)
from .selection import normalize_project_path


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
        elif action == "update_storage":
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ConfigError("update_storage value must be an object")
            reject_unknown_keys(value, STORAGE_KEYS, "storage")
            candidate = copy.deepcopy(updated)
            candidate["storage"] = copy.deepcopy(value)
            storage_policy_from_config(candidate)
            updated["storage"] = copy.deepcopy(value)
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
