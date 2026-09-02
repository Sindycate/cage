"""Native-host Codex execution target."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config
from ..planning import PreparedLaunch
from ..state import OAuthSessionLease, SyncError


class HostTargetError(RuntimeError):
    pass


def _pin_codex(repository: Path) -> Path:
    located = shutil.which("codex")
    if located is None:
        raise HostTargetError("codex command not found in PATH")
    try:
        resolved = Path(located).resolve(strict=True)
    except OSError as exc:
        raise HostTargetError(f"cannot resolve codex executable: {exc}") from exc
    if resolved == repository or resolved.is_relative_to(repository):
        raise HostTargetError(
            f"refusing codex executable from a Cage-writable path: {resolved}"
        )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise HostTargetError(
            f"codex is not a runnable regular file: {resolved}"
        )
    return resolved


def _process_git_identity(environment: dict[str, str], prepared: PreparedLaunch) -> None:
    resolved = prepared.plan.runtime_config
    raw_count = environment.get("GIT_CONFIG_COUNT", "0")
    if not raw_count.isdigit():
        raise HostTargetError(
            "inherited GIT_CONFIG_COUNT must be a non-negative integer"
        )
    index = int(raw_count, 10)
    if resolved.git_user_name:
        environment[f"GIT_CONFIG_KEY_{index}"] = "user.name"
        environment[f"GIT_CONFIG_VALUE_{index}"] = resolved.git_user_name
        index += 1
    if resolved.git_user_email:
        environment[f"GIT_CONFIG_KEY_{index}"] = "user.email"
        environment[f"GIT_CONFIG_VALUE_{index}"] = resolved.git_user_email
        index += 1
    if resolved.git_user_name or resolved.git_user_email:
        environment["GIT_CONFIG_COUNT"] = str(index)


def _process_ssh_identity(environment: dict[str, str], prepared: PreparedLaunch) -> None:
    selected = prepared.plan.runtime_config.ssh_key
    if selected:
        key_path = Path(selected).expanduser()
        if not key_path.is_file():
            raise HostTargetError(
                f"selected SSH key does not exist: {key_path}"
            )
        environment["CAGE_SELECTED_SSH_KEY"] = str(key_path)
        environment["GIT_SSH_COMMAND"] = (
            'ssh -i "$CAGE_SELECTED_SSH_KEY" -o IdentitiesOnly=yes '
            "-o StrictHostKeyChecking=accept-new"
        )
    if prepared.plan.runtime_config.ssh_host:
        raise HostTargetError(
            f"ssh_host alias {prepared.plan.runtime_config.ssh_host!r} is not supported "
            "in host mode"
        )


def _process_github_auth(
    environment: dict[str, str], prepared: PreparedLaunch
) -> None:
    resolved = prepared.plan.runtime_config
    if resolved.gh_auth != "1":
        return
    token = environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN")
    if not token:
        gh = shutil.which("gh")
        if gh is not None:
            command = [gh, "auth", "token"]
            if resolved.gh_account:
                command.extend(("-u", resolved.gh_account))
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                token = result.stdout.rstrip("\r\n")
    if not token:
        raise HostTargetError(
            "GitHub authentication was requested (gh_auth = true) but no token "
            "could be resolved"
        )
    environment["GH_TOKEN"] = token


def run_host_target(prepared: PreparedLaunch) -> int:
    """Apply process-scoped host settings and replace Cage with pinned Codex."""

    plan = prepared.plan
    resolved = plan.runtime_config
    repository = Path(plan.repository)
    codex_home = Path(
        resolved.host_codex_dir or (Path.home() / ".codex")
    ).expanduser()
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    for name in resolved.extra_env:
        value = os.environ.get(name)
        if value:
            environment[name] = value
    payload = config.host_codex_payload_for(resolved)
    tool_arguments = config.host_codex_arg_lines(
        payload,
        repository,
        codex_home,
    )
    _process_git_identity(environment, prepared)
    _process_ssh_identity(environment, prepared)
    _process_github_auth(environment, prepared)
    executable = _pin_codex(repository)
    if plan.yolo:
        tool_arguments.append("--yolo")
    tool_arguments.extend(prepared.request.tool_arguments)

    print("cage: host-native Codex (no Docker isolation)", file=sys.stderr)
    print(f"  Executable: {executable}", file=sys.stderr)
    print(f"  CODEX_HOME: {codex_home}", file=sys.stderr)
    print(f"  Repository: {repository}", file=sys.stderr)
    print(
        f"  Network:    {plan.network} "
        "(Cage provides no network restriction)",
        file=sys.stderr,
    )
    if resolved.codex_profile:
        print(f"  Profile:    {resolved.codex_profile}", file=sys.stderr)
    if resolved.stdio_mcp or resolved.remote_mcp:
        print(
            "  MCP:        selected packs applied for this process",
            file=sys.stderr,
        )
    if resolved.skill_mounts:
        print(
            "  Skills:     selected pack filter applied for this process",
            file=sys.stderr,
        )
    print("  MCP policy: selected packs only", file=sys.stderr)
    if resolved.mcp_suppressed:
        display = " ".join(
            json.dumps(name, ensure_ascii=True)
            for name in resolved.mcp_suppressed
        )
        print(f"  Inherited MCPs suppressed: {display}", file=sys.stderr)
    if plan.yolo:
        print("  Yolo:       enabled", file=sys.stderr)
    if resolved.git_user_name:
        print(
            f"  Git identity: {resolved.git_user_name} "
            f"<{resolved.git_user_email}>",
            file=sys.stderr,
        )
    print("", file=sys.stderr)

    lease: OAuthSessionLease | None = None
    try:
        if any(server.get("auth") == "oauth" for server in resolved.remote_mcp):
            lease = OAuthSessionLease.acquire(codex_home, create=True)
            print("  OAuth:      exclusive CODEX_HOME lease", file=sys.stderr)
            lease.preserve_across_exec()
        os.chdir(repository)
        os.execve(
            executable,
            [str(executable), *tool_arguments],
            environment,
        )
        return 127
    except SyncError as exc:
        raise HostTargetError(f"cannot start Codex OAuth session: {exc}") from exc
    finally:
        if lease is not None:
            try:
                lease.close()
            except SyncError:
                pass
