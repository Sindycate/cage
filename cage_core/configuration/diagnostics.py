"""Redacted configuration inspection and local readiness checks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cage_core.models import ResolvedConfig

from .codex import (
    host_codex_arg_lines,
    host_codex_payload_for,
    validate_codex_layers,
)
from .schema import ConfigError
from .selection import (
    effective_exec_state,
    expand_path_string,
)


def emit_resolved_json(resolved: ResolvedConfig) -> None:
    """Emit the versioned, non-secret configuration resolver contract."""

    payload = {
        "schema": "cage.resolved-config",
        "schema_version": 1,
        "cage_version": os.environ.get("CAGE_INTERNAL_VERSION", ""),
        "resolved_config": resolved.public_dict(),
    }
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def host_github_token_available(resolved: ResolvedConfig) -> bool:
    """Return whether the selected host GitHub authentication can resolve.

    Token contents are discarded and never included in doctor output.
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return True
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        gh_path = Path(gh).resolve(strict=True)
        repo_path = Path(resolved.repo_path).resolve(strict=False)
    except OSError:
        return False
    if gh_path == repo_path or gh_path.is_relative_to(repo_path):
        return False
    command = [str(gh_path), "auth", "token"]
    if resolved.gh_account:
        command.extend(["-u", resolved.gh_account])
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


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
    if resolved.codex_profile:
        print(f"Codex profile: {resolved.codex_profile}")
    if resolved.target == "host":
        print(f"Target: {resolved.target} (no Docker isolation)")
    elif resolved.target == "desktop":
        print("Target: desktop (persistent Cage container through SSH)")
    if resolved.net:
        print(f"Net:    {resolved.net}")
    storage = resolved.storage_policy
    print(
        "Storage: "
        f"warn={storage.warn_free_gib}GiB "
        f"critical={storage.critical_free_gib}GiB "
        f"build={storage.min_build_free_gib}GiB "
        f"keep={storage.keep_versions} "
        f"dangling-age={storage.dangling_min_age_hours}h "
        f"ephemeral-age={storage.ephemeral_min_age_hours}h"
        + (" (Docker targets only)" if resolved.target == "host" else "")
    )
    if resolved.yolo:
        print(f"Yolo:   {'enabled' if resolved.yolo == '1' else 'disabled'}")
    if resolved.auth_name:
        print(f"Auth:   {resolved.auth_name}")
    if resolved.aws_profile:
        print(f"AWS profile: {resolved.aws_profile}")
    if resolved.aws_access:
        print(
            "AWS host CLI: enabled (profile-pinned; browser/SSO auth stays on host)"
        )
    if resolved.identity_name:
        print(f"Identity: {resolved.identity_name}")
    if resolved.mcp_pack_names:
        print(f"MCP packs: {', '.join(resolved.mcp_pack_names)}")
    if resolved.skill_pack_names:
        print(f"Skill packs: {', '.join(resolved.skill_pack_names)}")
    print("MCP policy: selected packs only")
    if resolved.stdio_mcp or resolved.remote_mcp:
        print("Active MCP servers:")
        for server in resolved.stdio_mcp:
            print(f"  - {server['name']} (stdio bridge)")
        for server in resolved.remote_mcp:
            print(f"  - {format_server(server)}")
    else:
        print("Active MCP servers: none")
    if resolved.mcp_suppressed:
        print("Inherited MCPs suppressed for this launch:")
        for name in resolved.mcp_suppressed:
            print(f"  - {json.dumps(name, ensure_ascii=True)}")
    elif resolved.tool == "codex" and resolved.target in {"container", "desktop"}:
        print(
            f"Inherited MCP suppression: enforced at launch by the {resolved.target} "
            "runtime (not enumerable from the host without launching)"
        )
    if resolved.skill_mounts:
        print("Skills:")
        for skill in resolved.skill_mounts:
            print(f"  - {skill['name']} ({skill['path']})")
    if resolved.host_commands:
        print("Host commands:")
        for command in resolved.host_commands:
            print(f"  - {command['name']} (executes on host)")
    if resolved.extra_env:
        print("Env forwarded:")
        for name in resolved.extra_env:
            status = "set" if os.environ.get(name) else "unset"
            print(f"  - {name}: {status}")
    if resolved.host_codex_dir:
        print(f"Codex dir: {resolved.host_codex_dir}")
    if resolved.host_opencode_config_dir:
        print(f"OpenCode config dir: {resolved.host_opencode_config_dir}")
    if resolved.host_opencode_data_dir:
        print(f"OpenCode data dir: {resolved.host_opencode_data_dir}")
    if resolved.host_agents_dir:
        print(f"Agents dir: {resolved.host_agents_dir}")
    if resolved.gh_auth == "1":
        acct = f" ({resolved.gh_account})" if resolved.gh_account else ""
        print(f"GitHub CLI auth: enabled{acct}")
    if resolved.extra_mounts:
        print("Extra mounts:")
        for mount in resolved.extra_mounts:
            print(f"  - {mount}")

    print("Capabilities:")
    if resolved.target == "host":
        print("  - execution: host-native (NO Docker isolation boundary)")
    elif resolved.target == "desktop":
        print("  - execution: persistent Cage container reached through managed SSH")
    print("  - repository: read/write, including .git")
    if resolved.tool == "claude":
        print(f"  - credentials: automated Claude {resolved.claude_auth or 'configured'} auth")
    elif resolved.target == "host":
        print(
            "  - credentials: resolved host CODEX_HOME used directly unless its auth "
            "source was explicitly adopted by Token Monitor"
        )
    elif resolved.tool == "opencode":
        if resolved.opencode_copy_auth == "0":
            print("  - credentials: host OpenCode provider auth copy disabled")
        else:
            print("  - credentials: exact host OpenCode provider auth reuse")
        print("  - configuration: frozen, sanitized launch snapshot")
        print(
            "  - plugins: "
            + ("enabled (expanded trust boundary)" if resolved.opencode_plugins == "1" else "disabled (--pure)")
        )
    elif resolved.codex_copy_auth == "0":
        print("  - credentials: host Codex auth.json copy disabled")
    else:
        print("  - credentials: automated host Codex state/auth reuse")
    if resolved.aws_access:
        print(
            "  - AWS: profile-pinned host CLI relay (host-integrated; bypasses Netgate)"
        )
    if resolved.target == "host" and resolved.stdio_mcp:
        print("  - host execution: selected stdio MCP servers run directly on the host")
    elif resolved.stdio_mcp or resolved.host_commands or resolved.aws_access:
        print("  - host execution: enabled by selected bridge integrations")
    else:
        print("  - host execution: no selected bridge integrations")
    if resolved.remote_mcp:
        print("  - external connectors: enabled")
    eff = effective_exec_state(resolved)
    if resolved.target == "host":
        if eff["net"] == "open":
            print("  - network: unrestricted host networking (Cage enforces nothing)")
        else:
            print(f"  - network: {eff['net']} (INCOMPATIBLE — Cage cannot enforce this without a container)")
    elif eff["net"] == "gate":
        print("  - network: proxy approval helper (deliberate bypass remains possible)")
    elif eff["net"] == "off":
        print("  - network: Docker network disabled for the main tool container")
    else:
        print("  - network: open")
    if resolved.tool == "claude" and resolved.session_sync == "1":
        print("  - host state writeback: Claude session sync enabled")
    if resolved.poketoken:
        print("  - local usage export: PokeTokenBar accounting only (no conversations or credentials)")
    if resolved.ssh_key:
        print(f"  - SSH private key: mounted read-only ({resolved.ssh_key})")
    if resolved.yolo == "1":
        print("  - coding-tool permission prompts: disabled")

    if not doctor:
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    if resolved.target == "host":
        if resolved.tool != "codex":
            errors.append(f"host execution is only supported for Codex, not {resolved.tool!r}")
        if resolved.host_commands:
            errors.append("host command bridges require container execution (target = 'container')")
        if resolved.extra_mounts:
            errors.append("extra mounts require container execution (target = 'container')")
        eff = effective_exec_state(resolved)
        if eff["net"] in ("gate", "off"):
            errors.append(
                f"Cage cannot enforce network mode {eff['net']!r} without a container; "
                "host execution has no Cage network restriction. "
                "Set net = \"open\" explicitly or remove yolo to avoid the implicit gate default"
            )
        if resolved.host_agents_dir:
            default_agents = str(Path.home() / ".agents")
            if resolved.host_agents_dir != default_agents:
                errors.append(
                    f"custom host_agents_dir {resolved.host_agents_dir!r} is not supported in host mode; "
                    "the default ~/.agents is naturally available on the host"
                )
        if resolved.ssh_host:
            errors.append(
                f"ssh_host alias {resolved.ssh_host!r} is not supported in host mode; "
                "configure the alias in host ~/.ssh/config or use container execution"
            )
        if resolved.gh_auth == "1" and not host_github_token_available(resolved):
            errors.append(
                "GitHub authentication was requested but no token can be resolved "
                "from GH_TOKEN, GITHUB_TOKEN, or host gh authentication"
            )
        codex_bin = shutil.which("codex")
        if codex_bin is None:
            errors.append("codex command not found in PATH (required for host execution)")
        try:
            host_codex_arg_lines(
                host_codex_payload_for(resolved),
                Path(resolved.repo_path),
                Path(resolved.host_codex_dir or (Path.home() / ".codex")),
            )
        except ConfigError as exc:
            errors.append(str(exc))
    if resolved.target in {"container", "desktop"} and shutil.which("docker") is None:
        errors.append("docker command not found")
    if resolved.target == "desktop":
        if resolved.tool != "codex":
            errors.append(
                f"desktop execution is only supported for Codex, not {resolved.tool!r}"
            )
        if sys.platform != "darwin":
            errors.append("desktop execution is currently supported only on macOS")
        if not Path("/Applications/ChatGPT.app").is_dir() and not Path(
            "/Applications/Codex.app"
        ).is_dir():
            warnings.append("ChatGPT desktop app was not found under /Applications")
        configured_root = os.environ.get("CAGE_CONFIG_DIR")
        if configured_root:
            setup = Path(configured_root).expanduser() / "desktop" / "setup.json"
        else:
            setup = Path(
                os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            ) / "cage" / "desktop" / "setup.json"
        if not setup.is_file():
            warnings.append("desktop SSH setup is missing; run `cage desktop setup`")
    if resolved.aws_access:
        if resolved.target == "host":
            errors.append(
                'aws_access = "host-cli" requires container execution; '
                "host-native Codex already runs on the host"
            )
        if effective_exec_state(resolved)["net"] == "off":
            errors.append(
                'aws_access = "host-cli" cannot be combined with --net off; '
                "the host AWS CLI must make outbound connections"
            )
        if shutil.which("aws") is None:
            errors.append(
                "AWS host CLI access was selected, but the host 'aws' command "
                "was not found in PATH"
            )
        else:
            warnings.append(
                "AWS host CLI runs outside the container and bypasses Netgate; "
                "IAM permissions for the selected profile remain authoritative"
            )
    if resolved.tool == "codex" and resolved.target in {"container", "desktop"}:
        try:
            validate_codex_layers(
                host_codex_payload_for(resolved),
                Path(resolved.repo_path),
                Path(resolved.host_codex_dir or (Path.home() / ".codex")),
            )
        except ConfigError as exc:
            errors.append(str(exc))
    if resolved.tool == "opencode":
        if resolved.target != "container":
            errors.append("OpenCode currently supports only target = 'container'")
        if resolved.host_opencode_config_dir and not Path(
            resolved.host_opencode_config_dir
        ).is_dir():
            warnings.append(
                "OpenCode config directory is missing; an empty configuration snapshot "
                f"will be used: {resolved.host_opencode_config_dir}"
            )
        if resolved.host_opencode_data_dir and not Path(
            resolved.host_opencode_data_dir
        ).is_dir():
            warnings.append(
                "OpenCode data directory is missing; provider and MCP auth start empty: "
                f"{resolved.host_opencode_data_dir}"
            )
    if resolved.tool == "claude" and resolved.claude_auth == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY is required for Claude api-key auth")
    if resolved.tool == "claude" and resolved.claude_auth == "bedrock":
        aws_creds = Path.home() / ".aws" / "credentials"
        if not aws_creds.is_file():
            errors.append("~/.aws/credentials must be a regular file for Claude Bedrock auth")
    if resolved.host_codex_dir and not Path(resolved.host_codex_dir).is_dir():
        warnings.append(f"Codex config directory is missing or not a directory: {resolved.host_codex_dir}")
    if resolved.host_agents_dir and not Path(resolved.host_agents_dir).is_dir():
        warnings.append(f"Agents directory is missing or not a directory: {resolved.host_agents_dir}")
    if resolved.ssh_key and not Path(expand_path_string(resolved.ssh_key)).is_file():
        errors.append(f"SSH key is missing or not a regular file: {resolved.ssh_key}")
    for env_name in resolved.extra_env:
        if not os.environ.get(env_name):
            warnings.append(f"env var is unset: {env_name}")
    for mount in resolved.extra_mounts:
        raw_path = mount.removeprefix("rw=")
        if not Path(expand_path_string(raw_path)).exists():
            warnings.append(f"extra mount does not exist and will be skipped: {raw_path}")
    for command in resolved.host_commands:
        try:
            argv = shlex.split(command["command"])
        except ValueError as exc:
            errors.append(f"host command {command['name']!r} has invalid quoting: {exc}")
            continue
        if argv and "/" not in argv[0] and shutil.which(argv[0]) is None:
            warnings.append(f"host command executable not found in PATH: {argv[0]}")
        if len(argv) > 1:
            warnings.append(
                f"host command {command['name']!r} embeds fixed arguments; Cage "
                "de-duplicates an identical caller suffix for compatibility, but an "
                "executable-only command is clearer when the client supplies arguments"
            )

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
