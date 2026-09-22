"""Selected-server OAuth operations under the host credential lease."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

from cage_core.models import ResolvedConfig
from cage_core.state import SyncError

from .codex import codex_mcp_overrides
from .resolution import resolve_config
from .schema import (
    ConfigError,
    as_table,
    bool_to_flag,
    require_name,
    require_transport_name,
)
from .selection import expand_path_string
from .storage import load_config


_INSTALL_ROOT = Path(__file__).resolve().parents[2]


def selected_oauth_mcp_server(resolved: ResolvedConfig, name: str) -> dict[str, Any]:
    for server in resolved.remote_mcp:
        if server.get("name") == name:
            if server.get("auth") != "oauth":
                raise ConfigError(f"MCP server {name!r} is not configured for OAuth")
            return server
    raise ConfigError(f"OAuth MCP server not selected by preset: {name}")


def _codex_auth_directory_for_mcp_auth(
    data: dict[str, Any], auth_name: str
) -> tuple[dict[str, Any], str]:
    """Return the explicit Codex auth block and its credential directory.

    The auth-scoped MCP command deliberately requires an explicit Codex auth
    type.  Preset resolution can infer a tool from surrounding launch policy,
    but this path has no repository or launch policy to make that inference
    trustworthy.
    """

    require_name(auth_name, "auth name")
    auth = as_table(data, "auth").get(auth_name)
    if not isinstance(auth, dict):
        raise ConfigError(f"auth not found: {auth_name}")
    if auth.get("tool") != "codex":
        raise ConfigError(
            f"auth {auth_name!r} must set tool = \"codex\" for auth-scoped operations"
        )
    raw_directory = auth.get("host_codex_dir")
    if raw_directory is not None and not isinstance(raw_directory, str):
        raise ConfigError(f"auth.{auth_name}.host_codex_dir must be a string")
    return auth, expand_path_string(raw_directory or "~/.codex")


def codex_auth_source_for_monitor(
    data: dict[str, Any], auth_name: str
) -> tuple[str, bool]:
    """Resolve the explicit Codex auth root for host-session monitoring.

    Monitoring is keyed to the real source directory rather than the auth
    alias, so two auth blocks that deliberately share ``host_codex_dir`` are
    adopted once.  The normal ``copy_auth`` policy remains authoritative for
    the private managed home.
    """

    auth, codex_home = _codex_auth_directory_for_mcp_auth(data, auth_name)
    copy_auth = bool_to_flag(
        auth.get("copy_auth", auth.get("codex_copy_auth")),
        f"auth.{auth_name}.copy_auth",
    )
    return codex_home, copy_auth != "0"


def _auth_scoped_codex_mcp_resolution(
    data: dict[str, Any],
    config_path: Path,
    auth_name: str,
    auth: dict[str, Any],
    preset: dict[str, Any],
) -> ResolvedConfig:
    """Resolve only the MCP selection needed to identify an OAuth endpoint.

    Login is credential-scoped, rather than a launch.  Build a minimal
    synthetic Codex preset so this path retains the normal MCP validation and
    normalization without inheriting unrelated target, identity, skill, mount,
    or host-command policy from a launch preset.
    """

    scoped_auth: dict[str, Any] = {"tool": "codex"}
    if auth.get("host_codex_dir"):
        scoped_auth["host_codex_dir"] = auth["host_codex_dir"]
    scoped_preset: dict[str, Any] = {
        "tool": "codex",
        "auth": auth_name,
    }
    if "mcp_packs" in preset:
        scoped_preset["mcp_packs"] = preset["mcp_packs"]
    scoped_data = {
        "version": 1,
        "auth": {auth_name: scoped_auth},
        "mcp_packs": as_table(data, "mcp_packs"),
        "presets": {"__cage_mcp_auth": scoped_preset},
    }
    return resolve_config(
        scoped_data,
        config_path,
        str(config_path.parent),
        "__cage_mcp_auth",
        "codex",
    )


def _oauth_server_signature(server: dict[str, Any]) -> tuple[object, ...]:
    """Return the OAuth properties that must agree across auth consumers."""

    return (
        server.get("url"),
        server.get("oauth_resource"),
        server.get("oauth_client_id"),
        server.get("oauth_client_id_env_var"),
        tuple(server.get("oauth_scopes", [])),
    )


def selected_codex_oauth_mcp_server_for_auth(
    data: dict[str, Any], config_path: Path, auth_name: str, name: str
) -> tuple[str, dict[str, Any]]:
    """Find one unambiguous OAuth MCP endpoint for a Codex auth block.

    An auth block owns the credential directory, while a preset normally owns
    the selected MCP endpoint.  The direct ``--auth`` route is safe only when
    every Codex preset that selects the requested server under that auth has
    an identical definition. Different endpoint details must stay explicit
    through the existing preset-and-repository command.
    """

    require_transport_name(name, "MCP server name")
    auth, codex_home = _codex_auth_directory_for_mcp_auth(data, auth_name)
    candidates: list[tuple[str, dict[str, Any]]] = []
    presets = as_table(data, "presets")
    for preset_name in sorted(presets):
        preset = presets[preset_name]
        if not isinstance(preset, dict) or preset.get("auth") != auth_name:
            continue
        if (preset.get("tool") or auth.get("tool")) != "codex":
            continue
        try:
            resolved = _auth_scoped_codex_mcp_resolution(
                data, config_path, auth_name, auth, preset
            )
        except ConfigError as exc:
            raise ConfigError(
                f"cannot resolve OAuth MCP server {name!r} for auth {auth_name!r} "
                f"from preset {preset_name!r}: {exc}"
            ) from exc
        matches = [
            server
            for server in [*resolved.stdio_mcp, *resolved.remote_mcp]
            if server.get("name") == name
        ]
        if not matches:
            continue
        server = matches[0]
        if server.get("auth") != "oauth":
            raise ConfigError(
                f"MCP server {name!r} selected by Codex preset {preset_name!r} "
                f"for auth {auth_name!r} is not configured for OAuth"
            )
        candidates.append((preset_name, server))

    if not candidates:
        raise ConfigError(
            f"OAuth MCP server {name!r} is not selected by any Codex preset "
            f"using auth {auth_name!r}"
        )

    signatures = {_oauth_server_signature(server) for _, server in candidates}
    if len(signatures) != 1:
        presets_text = ", ".join(repr(preset_name) for preset_name, _ in candidates)
        raise ConfigError(
            f"OAuth MCP server {name!r} is ambiguous for Codex auth {auth_name!r} "
            f"across presets: {presets_text}; use --preset NAME with a repository path"
        )
    return codex_home, candidates[0][1]


def _run_codex_mcp_auth(
    action: str,
    server: dict[str, Any],
    codex_home: str,
    *,
    auth_name: str = "",
) -> int:
    """Run one host-side Codex OAuth action under the directory lease."""

    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    cmd = ["codex", "-c", 'mcp_oauth_credentials_store="file"']
    for override in codex_mcp_overrides(server, resolve_client_env=(action == "login")):
        cmd.extend(["-c", override])
    cmd.extend(["mcp", action])
    if action == "login" and server.get("oauth_scopes"):
        cmd.extend(["--scopes", ",".join(str(scope) for scope in server["oauth_scopes"])])
    cmd.append(str(server["name"]))

    try:
        from cage_core.oauth_broker import connect
        with connect(Path(codex_home), Path(__file__).resolve().parents[2]):
            if auth_name:
                print(f"Codex auth: {auth_name}", flush=True)
            print(f"Codex dir: {codex_home}", flush=True)
            return subprocess.call(cmd, env=env)
    except SyncError as exc:
        raise ConfigError(
            f"cannot start Codex MCP OAuth {action}: {exc}"
        ) from exc


def command_mcp_auth(args: argparse.Namespace, action: str) -> int:
    raw_auth_name = getattr(args, "auth", None)
    if raw_auth_name is not None and not isinstance(raw_auth_name, str):
        raise ConfigError("--auth must name a Codex auth block")
    auth_name = raw_auth_name or ""
    preset_name = getattr(args, "preset", None) or ""
    repo = getattr(args, "repo", None)
    if auth_name and preset_name:
        raise ConfigError("--auth and --preset cannot be combined for MCP OAuth authentication")
    if auth_name and repo:
        raise ConfigError("--auth selects the Codex auth directory directly; do not pass a repository path")
    if not auth_name and not repo:
        raise ConfigError("MCP OAuth authentication requires a repository path unless --auth is supplied")

    data = load_config(args.config)
    if auth_name:
        codex_home, server = selected_codex_oauth_mcp_server_for_auth(
            data, args.config, auth_name, args.name
        )
        return _run_codex_mcp_auth(
            action, server, codex_home, auth_name=auth_name
        )

    resolved = resolve_config(data, args.config, repo, preset_name, "")
    server = selected_oauth_mcp_server(resolved, args.name)
    if resolved.tool == "opencode":
        cage_bin = _INSTALL_ROOT / "cage"
        command = [
            str(cage_bin),
            "opencode",
            "--preset",
            resolved.preset_name,
            repo,
            "mcp",
            "auth" if action == "login" else "logout",
            args.name,
        ]
        return subprocess.call(command)
    if resolved.tool != "codex":
        raise ConfigError(
            f"MCP OAuth login/logout is not supported for {resolved.tool!r}"
        )
    codex_home = resolved.host_codex_dir or expand_path_string("~/.codex")
    return _run_codex_mcp_auth(action, server, codex_home)


def command_mcp_login(args: argparse.Namespace) -> int:
    return command_mcp_auth(args, "login")


def command_mcp_logout(args: argparse.Namespace) -> int:
    return command_mcp_auth(args, "logout")
