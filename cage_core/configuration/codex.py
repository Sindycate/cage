"""Codex inventory, layer validation, and process-local overrides."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tomllib
from pathlib import Path
from typing import Any

from cage_core import codex_policy, codex_runtime
from cage_core.models import ResolvedConfig

from .rendering import toml_quote
from .schema import (
    CODEX_PROFILE_RE,
    ConfigError,
    ENV_RE,
    SKILL_NAME_RE,
    TRANSPORT_NAME_RE,
)


MAX_CODEX_CONFIG_BYTES = 4 * 1024 * 1024


MAX_CODEX_INVENTORY_BYTES = 4 * 1024 * 1024


CODEX_INVENTORY_TIMEOUT = 30.0


SAFE_CODEX_PASSTHROUGH_CONFIG_ROOTS = (
    codex_policy.SAFE_PASSTHROUGH_CONFIG_ROOTS
)


def mcp_server_transports_in_toml(path: Path) -> dict[str, str] | None:
    """Return enabled MCP names and transport kinds from a Codex TOML layer.

    Returns None when the layer exists but cannot be read or parsed, so callers
    can distinguish "no enumerable servers" from "unreadable layer". Only names
    plus the command/url discriminator are retained; transport values and
    environment values are never returned or logged.
    """
    return codex_runtime.toml_transports(
        path, maximum_bytes=MAX_CODEX_INVENTORY_BYTES
    )


def merge_mcp_transport_maps(
    target: dict[str, str],
    incoming: dict[str, str],
) -> None:
    try:
        codex_policy.merge_transports(target, incoming)
    except codex_policy.PolicyError as exc:
        raise ConfigError(str(exc)) from exc


def mcp_disable_plan(
    suppressed: list[str],
    runtime_enabled: set[str],
    direct_transports: dict[str, str],
) -> list[str]:
    """Build transport-complete, highest-precedence suppression overrides.

    `codex mcp list` omits an untrusted project layer. An enabled-only override
    for such a name creates a transport-less table and makes Codex abort with
    `invalid transport`. Seed a same-kind inert transport for definitions seen
    only by direct parsing; it remains disabled before trust and safely shadows
    the real transport if the user grants trust in the same Codex process.
    """
    try:
        return codex_policy.suppression_overrides(
            suppressed, runtime_enabled, direct_transports
        )
    except codex_policy.PolicyError as exc:
        raise ConfigError(str(exc)) from exc


def codex_mcp_inventory_enabled(
    resolved: ResolvedConfig,
) -> tuple[set[str], set[str], dict[str, str]]:
    """Enumerate inherited MCP servers the host Codex runtime would enable.

    This is the host-target inventory (and the disclosure source for host-side
    inspection commands). Container and Desktop launches inventory inside the
    launching runtime instead, because only that runtime knows its own system,
    plugin, and imported configuration layers.

    The primary source is the active codex binary's `mcp list --json`, run with
    the resolved CODEX_HOME, profile, and project directory. The selected
    profile layer and the project layer are then merged in, because
    `codex mcp list` does not enumerate them. Only server names and the enabled
    flag are read; transport configuration and environment values are never
    logged. Fails closed when a trustworthy inventory cannot be obtained.
    """
    codex_home = Path(resolved.host_codex_dir or "~/.codex").expanduser()
    repo_path = Path(resolved.repo_path).expanduser()
    codex_bin = shutil.which("codex")
    if codex_bin is None:
        raise ConfigError(
            "cannot build the launch-time MCP inventory: the codex executable "
            "was not found in PATH"
        )
    # Reject a repository-controlled codex binary (parity with host-mode
    # executable pinning) before executing anything.
    resolved_bin = Path(codex_bin).resolve(strict=False)
    repo_resolved = repo_path.resolve(strict=False)
    if resolved_bin == repo_resolved or resolved_bin.is_relative_to(repo_resolved):
        raise ConfigError(
            f"refusing codex executable from a Cage-writable path: {resolved_bin}"
        )

    try:
        return codex_runtime.inventory_enabled(
            codex_binary=codex_bin,
            codex_home=codex_home,
            repository=repo_path,
            profile=resolved.codex_profile,
            environment=dict(os.environ),
            timeout=CODEX_INVENTORY_TIMEOUT,
            maximum_bytes=MAX_CODEX_INVENTORY_BYTES,
            temporary_home_when_missing=True,
        )
    except codex_policy.PolicyError as exc:
        raise ConfigError(str(exc)) from exc


def mcp_disable_override(name: str) -> str:
    """Build the highest-precedence Codex override that disables one MCP server."""
    if not name or "\n" in name or "\r" in name:
        raise ConfigError(f"suppressed MCP server name is unsafe: {name!r}")
    return f"mcp_servers.{codex_key_segment(name)}.enabled=false"


def config_override_root(expression: str) -> str | None:
    return codex_policy.config_override_root(expression)


def reject_unsafe_codex_passthrough_args(argv: list[str]) -> None:
    """Reject caller arguments that invalidate the MCP inventory boundary."""
    try:
        codex_policy.reject_unsafe_passthrough_args(argv)
    except codex_policy.PolicyError as exc:
        raise ConfigError(str(exc)) from exc


def host_codex_payload_for(resolved: ResolvedConfig) -> dict[str, Any]:
    return {
        "profile": resolved.codex_profile,
        "stdio": list(resolved.stdio_mcp),
        "remote": list(resolved.remote_mcp),
        "skills": list(resolved.skill_mounts),
        "env_names": list(resolved.extra_env),
        "disable_mcp": list(resolved.mcp_suppressed),
        "disable_mcp_overrides": list(resolved.mcp_disable_overrides),
    }


def toml_string(value: Any) -> str:
    """Encode a scalar string for a Codex `-c key=value` override."""
    return json.dumps(str(value), ensure_ascii=False)


def toml_string_array(values: list[str]) -> str:
    return "[" + ",".join(toml_string(value) for value in values) + "]"


def selected_mcp_names_in_file(path: Path, selected_names: set[str]) -> set[str]:
    if not path.exists():
        return set()
    if not path.is_file():
        raise ConfigError(f"Codex config layer is not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CODEX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CODEX_CONFIG_BYTES:
            raise ConfigError(
                f"Codex config layer exceeds {MAX_CODEX_CONFIG_BYTES} bytes: {path}"
            )
        parsed = tomllib.loads(raw.decode("utf-8"))
    except ConfigError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read Codex config layer {path}: {exc}") from exc
    servers = parsed.get("mcp_servers", {})
    if servers is None:
        return set()
    if not isinstance(servers, dict):
        raise ConfigError(f"Codex config layer {path} has a non-table mcp_servers value")
    return selected_names.intersection(str(name) for name in servers)


def validate_codex_layers(payload: dict[str, Any], repo: Path, codex_home: Path) -> None:
    if not isinstance(payload, dict):
        raise ConfigError("invalid Codex configuration payload")
    profile = payload.get("profile", "")
    if not isinstance(profile, str) or (profile and not CODEX_PROFILE_RE.fullmatch(profile)):
        raise ConfigError("invalid Codex profile in launch payload")
    if profile:
        profile_path = codex_home / f"{profile}.config.toml"
        if not profile_path.is_file():
            raise ConfigError(f"selected Codex profile is missing: {profile_path}")

    stdio = payload.get("stdio", [])
    remote = payload.get("remote", [])
    if not isinstance(stdio, list) or not isinstance(remote, list):
        raise ConfigError("invalid Codex launch payload: MCP servers must be lists")
    selected_mcp_names: set[str] = set()
    for server in [*stdio, *remote]:
        if not isinstance(server, dict):
            raise ConfigError("invalid MCP server in Codex launch payload")
        name = server.get("name")
        if not isinstance(name, str) or not TRANSPORT_NAME_RE.fullmatch(name):
            raise ConfigError("invalid MCP server name in Codex launch payload")
        if name in selected_mcp_names:
            raise ConfigError(f"duplicate MCP server in Codex launch payload: {name}")
        selected_mcp_names.add(name)

    disable_mcp = payload.get("disable_mcp", [])
    if not isinstance(disable_mcp, list):
        raise ConfigError("invalid Codex launch payload: disable_mcp must be a list")
    for suppressed_name in disable_mcp:
        if (
            not isinstance(suppressed_name, str)
            or not suppressed_name
            or "\n" in suppressed_name
            or "\r" in suppressed_name
        ):
            raise ConfigError("invalid suppressed MCP name in Codex launch payload")
    disable_overrides = payload.get("disable_mcp_overrides", [])
    if not isinstance(disable_overrides, list) or any(
        not isinstance(value, str) or "\n" in value or "\r" in value
        for value in disable_overrides
    ):
        raise ConfigError(
            "invalid Codex launch payload: disable_mcp_overrides must be safe strings"
        )
    allowed_overrides: set[str] = set()
    required_enabled: set[str] = set()
    for suppressed_name in disable_mcp:
        key = f"mcp_servers.{codex_key_segment(suppressed_name)}"
        enabled_override = f"{key}.enabled=false"
        required_enabled.add(enabled_override)
        allowed_overrides.update(
            {
                enabled_override,
                f"{key}.command={toml_string(codex_policy.INERT_STDIO_COMMAND)}",
                f"{key}.url={toml_string(codex_policy.INERT_HTTP_URL)}",
            }
        )
    if len(disable_overrides) != len(set(disable_overrides)):
        raise ConfigError("duplicate MCP suppression override in Codex launch payload")
    if not required_enabled.issubset(disable_overrides) or any(
        value not in allowed_overrides for value in disable_overrides
    ):
        raise ConfigError("invalid MCP suppression override in Codex launch payload")

    if not selected_mcp_names:
        return
    layers: list[Path] = [codex_home / "config.toml"]
    if profile:
        layers.append(codex_home / f"{profile}.config.toml")
    project_config = repo / ".codex" / "config.toml"
    if project_config.exists():
        layers.append(project_config)
    for layer in layers:
        duplicates = selected_mcp_names_in_file(layer, selected_mcp_names)
        if duplicates:
            names = ", ".join(sorted(duplicates))
            raise ConfigError(
                f"selected MCP server(s) already exist in Codex config layer {layer}: {names}; "
                "remove the duplicate definitions or deselect the Cage MCP pack"
            )


def pin_host_executable(command: str, repo_path: Path, label: str) -> tuple[str, list[str]]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ConfigError(f"{label} has invalid quoting: {exc}") from exc
    if not argv:
        raise ConfigError(f"{label} has an empty command")
    located = shutil.which(argv[0])
    if located is None:
        raise ConfigError(f"{label} executable is not available in PATH: {argv[0]}")
    try:
        executable = Path(located).resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"cannot resolve {label} executable {located!r}: {exc}") from exc
    repo = repo_path.resolve(strict=False)
    if executable == repo or executable.is_relative_to(repo):
        raise ConfigError(
            f"refusing {label} executable from the writable repository: {executable}"
        )
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ConfigError(f"{label} executable is not a runnable regular file: {executable}")
    return str(executable), argv[1:]


def host_codex_arg_lines(payload: dict[str, Any], repo: Path, codex_home: Path) -> list[str]:
    """Build process-local Codex CLI overrides for a host-native launch.

    The returned list alternates normal CLI flags and values. It never edits
    CODEX_HOME, repository config, or the global agents registry.
    """
    if not isinstance(payload, dict):
        raise ConfigError("invalid host Codex configuration payload")
    profile = payload.get("profile", "")
    if not isinstance(profile, str) or (profile and not CODEX_PROFILE_RE.fullmatch(profile)):
        raise ConfigError("invalid Codex profile in host launch payload")
    stdio = payload.get("stdio", [])
    remote = payload.get("remote", [])
    skills = payload.get("skills", [])
    env_names = payload.get("env_names", [])
    for label, value in (
        ("stdio MCP servers", stdio),
        ("remote MCP servers", remote),
        ("skills", skills),
        ("environment names", env_names),
    ):
        if not isinstance(value, list):
            raise ConfigError(f"invalid host Codex payload: {label} must be a list")
    if any(not isinstance(name, str) or not ENV_RE.fullmatch(name) for name in env_names):
        raise ConfigError("invalid environment name in host Codex payload")

    validate_codex_layers(payload, repo, codex_home)

    args: list[str] = []
    if profile:
        args.extend(["--profile", profile])
    try:
        provider_override = codex_runtime.configured_model_provider_override(
            codex_home, profile
        )
    except codex_policy.PolicyError as exc:
        raise ConfigError(str(exc)) from exc
    if provider_override is not None:
        args.extend(["-c", provider_override])
    for server in stdio:
        name = str(server["name"])
        command = server.get("command")
        if not isinstance(command, str):
            raise ConfigError(f"stdio MCP server {name!r} is missing its command")
        executable, command_args = pin_host_executable(
            command,
            repo,
            f"stdio MCP server {name!r}",
        )
        prefix = f"mcp_servers.{name}"
        args.extend(["-c", f"{prefix}.command={toml_string(executable)}"])
        args.extend(["-c", f"{prefix}.args={toml_string_array(command_args)}"])
        if env_names:
            args.extend(["-c", f"{prefix}.env_vars={toml_string_array(env_names)}"])

    any_oauth = False
    for server in remote:
        name = str(server["name"])
        url = server.get("url")
        if not isinstance(url, str) or not url:
            raise ConfigError(f"HTTP MCP server {name!r} is missing its URL")
        prefix = f"mcp_servers.{name}"
        args.extend(["-c", f"{prefix}.url={toml_string(url)}"])
        auth = server.get("auth")
        if auth == "oauth":
            any_oauth = True
            if server.get("oauth_resource"):
                args.extend([
                    "-c",
                    f"{prefix}.oauth_resource={toml_string(server['oauth_resource'])}",
                ])
            scopes = server.get("oauth_scopes") or []
            if scopes:
                if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
                    raise ConfigError(f"OAuth MCP server {name!r} has invalid scopes")
                args.extend(["-c", f"{prefix}.scopes={toml_string_array(scopes)}"])
            client_id = server.get("oauth_client_id")
            client_env = server.get("oauth_client_id_env_var")
            if client_env:
                client_id = os.environ.get(str(client_env))
                if not client_id:
                    raise ConfigError(
                        f"OAuth MCP server {name!r} requires env var to be set: {client_env}"
                    )
            if client_id:
                args.extend([
                    "-c",
                    f"{prefix}.oauth.client_id={toml_string(client_id)}",
                ])
        elif server.get("bearer_token_env_var"):
            args.extend([
                "-c",
                f"{prefix}.bearer_token_env_var="
                f"{toml_string(server['bearer_token_env_var'])}",
            ])
    if any_oauth:
        args.extend(["-c", f"mcp_oauth_credentials_store={toml_string('file')}"])

    if skills:
        skill_root = Path.home() / ".agents" / "skills"
        selected: dict[str, Path] = {}
        for skill in skills:
            if not isinstance(skill, dict):
                raise ConfigError("invalid skill in host Codex payload")
            name = skill.get("name")
            raw_path = skill.get("path")
            if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
                raise ConfigError("invalid skill name in host Codex payload")
            if not isinstance(raw_path, str):
                raise ConfigError(f"selected skill {name!r} is missing its path")
            path = Path(raw_path).expanduser()
            if path.parent != skill_root or path.name != name:
                raise ConfigError(
                    f"selected skill {name!r} is outside the default host registry {skill_root}; "
                    "host-native skill packs currently require source = \"~/.agents\""
                )
            if not (path / "SKILL.md").is_file():
                raise ConfigError(f"selected skill {name!r} is missing SKILL.md at {path}")
            selected[name] = path

        inventory: dict[str, Path] = {}
        if skill_root.is_dir():
            for candidate in sorted(skill_root.iterdir(), key=lambda item: item.name):
                if SKILL_NAME_RE.fullmatch(candidate.name) and (candidate / "SKILL.md").is_file():
                    inventory[candidate.name] = candidate
        missing = set(selected).difference(inventory)
        if missing:
            raise ConfigError(
                "selected host skill(s) are not discoverable in the default registry: "
                + ", ".join(sorted(missing))
            )
        entries = []
        for name, path in inventory.items():
            entries.append(
                "{path=%s,enabled=%s}"
                % (toml_string(path / "SKILL.md"), "true" if name in selected else "false")
            )
        args.extend(["-c", "skills.config=[" + ",".join(entries) + "]"])

    disable_overrides = payload.get("disable_mcp_overrides", [])
    # Highest precedence: apply the validated transport seeds and disable
    # overrides after Cage's selected server definitions.
    for override in disable_overrides:
        args.extend(["-c", override])

    if any("\n" in arg or "\r" in arg for arg in args):
        raise ConfigError("host Codex arguments contain an unsafe line break")
    return args


def codex_key_segment(name: str) -> str:
    return codex_policy.key_segment(name)


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
