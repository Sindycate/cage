"""Resolve selected central configuration into launch capabilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cage_core import bridge as bridge_policy
from cage_core.models import ContractError, ResolvedConfig

from . import codex
from .packs import resolve_mcp_packs, resolve_skill_packs
from .schema import (
    CODEX_PROFILE_RE,
    ConfigError,
    VALID_EXEC_TARGETS,
    VALID_TOOLS,
    as_list,
    as_table,
    bool_to_flag,
    collect_env,
    dedupe,
    reject_unknown_keys,
    require_name,
    require_transport_name,
    storage_policy_from_config,
    transport_key,
)
from .selection import (
    expand_path_string,
    normalize_project_path,
    select_project_preset,
)


def resolve_config(
    data: dict[str, Any],
    config_path: Path,
    repo: str,
    preset_name: str = "",
    explicit_tool: str = "",
    mcp_inventory: bool = False,
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
    if not isinstance(tool, str) or tool not in VALID_TOOLS:
        raise ConfigError(
            f"preset {preset_name!r} must resolve to tool 'claude', 'codex', or 'opencode'"
        )
    if explicit_tool and explicit_tool != tool:
        raise ConfigError(
            f"preset {preset_name!r} is for {tool}, but command requested {explicit_tool}; "
            "choose a matching preset"
        )
    if auth.get("tool") and auth.get("tool") != tool:
        raise ConfigError(f"auth {auth_name!r} is for {auth.get('tool')}, but preset uses {tool}")
    if tool != "opencode":
        for key in ("host_opencode_config_dir", "host_opencode_data_dir"):
            if key in auth:
                raise ConfigError(
                    f"auth.{auth_name}.{key} is only supported for OpenCode"
                )
    elif "codex_copy_auth" in auth:
        raise ConfigError(
            f"auth.{auth_name}.codex_copy_auth is Codex-only; use copy_auth for OpenCode"
        )
    if "opencode_plugins" in preset and tool != "opencode":
        raise ConfigError(
            f"presets.{preset_name}.opencode_plugins is only supported for OpenCode"
        )

    resolved = ResolvedConfig(
        config_path=config_path,
        repo_path=repo_path,
        preset_name=preset_name,
        preset_source=preset_source,
        tool=tool,
        auth_name=auth_name or "",
        storage_policy=storage_policy_from_config(data),
    )

    _resolve_launch_settings(resolved, preset, defaults)

    identity_name = preset.get("identity", "")
    if identity_name is not None and not isinstance(identity_name, str):
        raise ConfigError(f"presets.{preset_name}.identity must be a string")
    if identity_name and identity_name not in identities:
        raise ConfigError(f"identity not found: {identity_name}")
    identity = identities.get(identity_name, {}) if identity_name else {}
    if identity_name and not isinstance(identity, dict):
        raise ConfigError(f"identity {identity_name!r} must be a table")
    resolved.identity_name = identity_name or ""

    _resolve_aws_access(resolved, preset, auth, auth_name)

    env: list[str] = []
    collect_env(env, auth.get("env"), f"auth.{auth_name}.env")
    collect_env(env, preset.get("env"), f"presets.{preset_name}.env")
    collect_env(env, preset.get("extra_env"), f"presets.{preset_name}.extra_env")

    _resolve_tool_auth(resolved, preset, auth, auth_name)

    resolved.git_user_name = str(identity.get("git_user_name") or "")
    resolved.git_user_email = str(identity.get("git_user_email") or "")
    resolved.ssh_key = str(identity.get("ssh_key") or "")
    resolved.ssh_host = str(identity.get("ssh_host") or "")
    gh_auth = identity.get("gh_auth")
    resolved.gh_auth = bool_to_flag(gh_auth, f"identities.{identity_name}.gh_auth")
    resolved.gh_account = str(identity.get("gh_account") or "")

    resolve_mcp_packs(resolved, preset, mcp_packs, env)

    resolve_skill_packs(resolved, preset, skill_packs)

    _resolve_host_capabilities(resolved, preset, host_command_defs)

    # Host-target launches inventory the host Codex runtime here. Container and
    # Desktop launches inventory inside the launching runtime (entrypoint and
    # codex-remote.py), because only that runtime knows its own system, plugin,
    # and imported configuration layers; a host inventory can reference servers
    # that do not exist in the image, which would fail Codex config load.
    if mcp_inventory and resolved.tool == "codex" and resolved.target == "host":
        selected_mcp_names = {server["name"] for server in resolved.stdio_mcp}
        selected_mcp_names |= {server["name"] for server in resolved.remote_mcp}
        inventory, runtime_enabled, direct_transports = codex.codex_mcp_inventory_enabled(
            resolved
        )
        suppressed = sorted(inventory - selected_mcp_names)
        resolved.mcp_inventory_enabled = sorted(inventory)
        resolved.mcp_suppressed = suppressed
        resolved.mcp_disable_overrides = codex.mcp_disable_plan(
            suppressed,
            runtime_enabled,
            direct_transports,
        )

    resolved.extra_env = dedupe(env)
    if resolved.aws_access:
        ambient_aws_names = [
            name for name in resolved.extra_env if name.startswith("AWS_")
        ]
        if ambient_aws_names:
            raise ConfigError(
                "aws_access = \"host-cli\" cannot forward AWS_* environment "
                "names into the container: "
                + ", ".join(ambient_aws_names)
            )
    return resolved


def _resolve_launch_settings(
    resolved: ResolvedConfig, preset: dict[str, Any], defaults: dict[str, Any],
) -> None:
    preset_name, tool = resolved.preset_name, resolved.tool
    resolved.net = str(preset.get("net") or defaults.get("net") or "")
    if resolved.net and resolved.net not in {"open", "gate", "off"}:
        raise ConfigError(f"invalid net mode for preset {preset_name!r}: {resolved.net}")
    resolved.session_sync = bool_to_flag(
        preset.get("session_sync", defaults.get("session_sync")),
        f"presets.{preset_name}.session_sync",
    )
    if tool == "opencode":
        if "session_sync" in preset:
            raise ConfigError(
                f"presets.{preset_name}.session_sync is not supported for OpenCode; "
                "OpenCode state remains in its per-repository volume"
            )
        resolved.session_sync = "0"
    resolved.yolo = bool_to_flag(preset.get("yolo"), f"presets.{preset_name}.yolo")

    target = preset.get("target", "container")
    if not isinstance(target, str) or target not in VALID_EXEC_TARGETS:
        raise ConfigError(
            f"presets.{preset_name}.target must be one of: {', '.join(sorted(VALID_EXEC_TARGETS))}"
        )
    resolved.target = target
    for table, label in ((defaults, "defaults"), (preset, f"presets.{preset_name}")):
        if "poketoken" in table and type(table["poketoken"]) is not bool:
            raise ConfigError(f"{label}.poketoken must be true or false")
    resolved.poketoken_default = defaults.get("poketoken", False)
    resolved.poketoken_override = preset.get("poketoken")
    try:
        resolved.poketoken_for_target(target)
    except ContractError as exc:
        raise ConfigError(str(exc)) from exc
    if target in {"host", "desktop"} and tool != "codex":
        raise ConfigError(
            f"preset {preset_name!r}: {target} execution is only supported for Codex, not {tool!r}"
        )
    codex_profile = preset.get("codex_profile", "")
    if not isinstance(codex_profile, str):
        raise ConfigError(f"presets.{preset_name}.codex_profile must be a string")
    if codex_profile and not CODEX_PROFILE_RE.fullmatch(codex_profile):
        raise ConfigError(
            f"presets.{preset_name}.codex_profile must contain only letters, digits, hyphens, or underscores"
        )
    if codex_profile and tool != "codex":
        raise ConfigError("codex_profile is only supported for Codex presets")
    resolved.codex_profile = codex_profile


def _resolve_aws_access(
    resolved: ResolvedConfig, preset: dict[str, Any], auth: dict[str, Any],
    auth_name: str | None,
) -> None:
    preset_name, tool, target = resolved.preset_name, resolved.tool, resolved.target
    # AWS CLI access is a launch capability, so a preset is authoritative when
    # it specifies one. Keep auth-level values as a compatibility fallback for
    # older configurations and for Claude Bedrock auth profiles.
    aws_profile = preset.get("aws_profile") or auth.get("aws_profile") or ""
    aws_access = preset.get("aws_access") or auth.get("aws_access") or ""
    aws_profile_label = (
        f"presets.{preset_name}.aws_profile"
        if preset.get("aws_profile")
        else f"auth.{auth_name or preset_name}.aws_profile"
    )
    aws_access_label = (
        f"presets.{preset_name}.aws_access"
        if preset.get("aws_access")
        else f"auth.{auth_name or preset_name}.aws_access"
    )
    if not isinstance(aws_profile, str):
        raise ConfigError(
            f"{aws_profile_label} must be a string"
        )
    if not isinstance(aws_access, str):
        raise ConfigError(
            f"{aws_access_label} must be a string"
        )
    if aws_profile:
        try:
            bridge_policy.validate_aws_profile(
                aws_profile,
                aws_profile_label,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    if aws_access not in ("", bridge_policy.AWS_ACCESS_HOST_CLI):
        raise ConfigError(
            f"auth.{auth_name or preset_name}.aws_access must be "
            f"{bridge_policy.AWS_ACCESS_HOST_CLI!r}"
        )
    if aws_access and not aws_profile:
        raise ConfigError(
            "aws_access = \"host-cli\" requires an aws_profile"
        )
    if aws_access and target == "host":
        raise ConfigError(
            "aws_access = \"host-cli\" requires container execution; "
            "host-native Codex already runs on the host"
        )
    if aws_profile and tool != "claude" and not aws_access:
        raise ConfigError(
            "aws_profile on non-Claude presets requires "
            "aws_access = \"host-cli\""
        )
    resolved.aws_profile = aws_profile
    resolved.aws_access = aws_access
    resolved.aws_region = str(
        auth.get("aws_region") or preset.get("aws_region") or ""
    )
    if aws_access:
        resolved.warnings.append(
            f"AWS host CLI enabled for profile {aws_profile!r}; the selected "
            "host AWS CLI runs outside Cage network enforcement"
        )


def _resolve_tool_auth(
    resolved: ResolvedConfig, preset: dict[str, Any], auth: dict[str, Any],
    auth_name: str | None,
) -> None:
    preset_name, tool = resolved.preset_name, resolved.tool
    if tool == "claude":
        mode = auth.get("mode") or preset.get("claude_auth") or "bedrock"
        if mode not in {"bedrock", "api-key"}:
            raise ConfigError(f"unsupported Claude auth mode for {auth_name or preset_name!r}: {mode}")
        resolved.claude_auth = mode
    elif tool == "codex":
        if auth.get("host_codex_dir"):
            resolved.host_codex_dir = expand_path_string(str(auth["host_codex_dir"]))
        if auth.get("host_agents_dir"):
            resolved.host_agents_dir = expand_path_string(str(auth["host_agents_dir"]))
        copy_auth = auth.get("copy_auth", auth.get("codex_copy_auth"))
        resolved.codex_copy_auth = bool_to_flag(copy_auth, f"auth.{auth_name}.copy_auth")
    else:
        config_default = Path(
            os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        ) / "opencode"
        data_default = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        ) / "opencode"
        resolved.host_opencode_config_dir = expand_path_string(
            str(auth.get("host_opencode_config_dir") or config_default)
        )
        resolved.host_opencode_data_dir = expand_path_string(
            str(auth.get("host_opencode_data_dir") or data_default)
        )
        if auth.get("host_agents_dir"):
            resolved.host_agents_dir = expand_path_string(str(auth["host_agents_dir"]))
        resolved.opencode_copy_auth = (
            bool_to_flag(auth.get("copy_auth"), f"auth.{auth_name}.copy_auth")
            or "1"
        )
        resolved.opencode_plugins = bool_to_flag(
            preset.get("opencode_plugins", False),
            f"presets.{preset_name}.opencode_plugins",
        )
        if resolved.opencode_plugins == "1":
            resolved.warnings.append(
                "OpenCode plugins are enabled; global and project plugin code may execute "
                "with the selected container capabilities"
            )


def _resolve_host_capabilities(
    resolved: ResolvedConfig, preset: dict[str, Any],
    host_command_defs: dict[str, Any],
) -> None:
    preset_name = resolved.preset_name
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
        if (
            resolved.aws_access
            and transport_key(name) == transport_key(bridge_policy.AWS_COMMAND_NAME)
        ):
            raise ConfigError(
                "host command name 'aws' is reserved when "
                "aws_access = \"host-cli\" is enabled"
            )
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
