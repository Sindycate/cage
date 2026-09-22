"""Validate and compose selected MCP and skill packs.

The caller owns the resolved configuration and environment-name accumulator.
This layer does not execute MCP commands or start authentication operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cage_core.models import ResolvedConfig

from .schema import (
    ConfigError,
    as_list,
    as_str_list,
    collect_env,
    optional_str,
    require_env_name,
    require_name,
    require_skill_name,
    require_transport_name,
    transport_key,
    validate_headers,
)
from .selection import expand_path_string


def resolve_mcp_packs(
    resolved: ResolvedConfig, preset: dict[str, Any], mcp_packs: dict[str, Any],
    env: list[str],
) -> None:
    """Validate and compose selected MCP definitions in selection order."""
    preset_name, tool = resolved.preset_name, resolved.tool
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
                        if tool == "opencode":
                            raise ConfigError(
                                f"MCP server {name!r} uses oauth_resource, which OpenCode "
                                "cannot represent faithfully"
                            )
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


def resolve_skill_packs(
    resolved: ResolvedConfig, preset: dict[str, Any], skill_packs: dict[str, Any],
) -> None:
    """Validate selected skill names and their exact source directories."""
    preset_name, tool = resolved.preset_name, resolved.tool
    skill_pack_names = as_str_list(preset.get("skill_packs"), f"presets.{preset_name}.skill_packs")
    if skill_pack_names and tool not in {"codex", "opencode"}:
        raise ConfigError("skill_packs are only supported for Codex and OpenCode presets")
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
