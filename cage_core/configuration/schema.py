"""Central configuration schema, value validation, and errors."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from cage_core import bridge as bridge_policy
from cage_core.models import ContractError, StoragePolicy


ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")


TRANSPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


CODEX_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


HEADER_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


VALID_TOOLS = {"claude", "codex", "opencode"}


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
    "storage",
}


DEFAULT_KEYS = {"default_preset", "net", "session_sync"}


STORAGE_KEYS = {
    "warn_free_gib",
    "critical_free_gib",
    "min_build_free_gib",
    "keep_versions",
    "dangling_min_age_hours",
    "ephemeral_min_age_hours",
}


AUTH_KEYS = {
    "tool",
    "env",
    "mode",
    "aws_profile",
    "aws_access",
    "aws_region",
    "host_codex_dir",
    "host_opencode_config_dir",
    "host_opencode_data_dir",
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
    "codex_profile",
    "target",
    "net",
    "session_sync",
    "env",
    "extra_env",
    "claude_auth",
    "aws_profile",
    "aws_access",
    "aws_region",
    "mcp_packs",
    "skill_packs",
    "host_commands",
    "extra_mounts",
    "yolo",
    "opencode_plugins",
    "poketoken",
}


VALID_EXEC_TARGETS = {"container", "desktop", "host"}


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


def storage_policy_from_config(data: dict[str, Any]) -> StoragePolicy:
    """Resolve the optional top-level policy using conservative defaults."""

    table = as_table(data, "storage")
    reject_unknown_keys(table, STORAGE_KEYS, "storage")
    values = StoragePolicy().public_dict()
    for name, value in table.items():
        if type(value) is not int:
            raise ConfigError(f"storage.{name} must be an integer")
        values[name] = value
    try:
        return StoragePolicy(**values)
    except ContractError as exc:
        raise ConfigError(f"invalid [storage] policy: {exc}") from exc


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
    storage_policy_from_config(data)
    validate_named_table(data, "auth", AUTH_KEYS)
    validate_named_table(data, "identities", IDENTITY_KEYS)
    mcp_packs = validate_named_table(data, "mcp_packs", MCP_PACK_KEYS)
    validate_named_table(data, "skill_packs", SKILL_PACK_KEYS)
    host_commands = validate_named_table(data, "host_commands", HOST_COMMAND_KEYS)
    presets = validate_named_table(data, "presets", PRESET_KEYS)
    as_table(data, "projects")

    for table_name, table in (("auth", as_table(data, "auth")), ("presets", presets)):
        for name, value in table.items():
            label = f"{table_name}.{name}"
            for key in ("aws_profile", "aws_access"):
                if key in value and not isinstance(value[key], str):
                    raise ConfigError(f"{label}.{key} must be a string")
            profile = value.get("aws_profile", "")
            if profile:
                try:
                    bridge_policy.validate_aws_profile(
                        profile, f"{label}.aws_profile"
                    )
                except ValueError as exc:
                    raise ConfigError(str(exc)) from exc
            access = value.get("aws_access", "")
            if access not in ("", bridge_policy.AWS_ACCESS_HOST_CLI):
                raise ConfigError(
                    f"{label}.aws_access must be {bridge_policy.AWS_ACCESS_HOST_CLI!r}"
                )

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
        codex_profile = preset.get("codex_profile", "")
        if not isinstance(codex_profile, str):
            raise ConfigError(f"presets.{preset_name}.codex_profile must be a string")
        if codex_profile and not CODEX_PROFILE_RE.fullmatch(codex_profile):
            raise ConfigError(
                f"presets.{preset_name}.codex_profile must contain only letters, digits, hyphens, or underscores"
            )
        if codex_profile and preset.get("tool") in {"claude", "opencode"}:
            raise ConfigError(
                f"presets.{preset_name}.codex_profile is only supported for Codex presets"
            )
        if "opencode_plugins" in preset and not isinstance(
            preset["opencode_plugins"], bool
        ):
            raise ConfigError(
                f"presets.{preset_name}.opencode_plugins must be true or false"
            )
        if "poketoken" in preset and type(preset["poketoken"]) is not bool:
            raise ConfigError(f"presets.{preset_name}.poketoken must be true or false")


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


def collect_env(target: list[str], value: Any, label: str) -> None:
    for env_name in as_str_list(value, label):
        target.append(require_env_name(env_name, label))


def optional_str(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string")
    return value
