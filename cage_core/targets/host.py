"""Native-host Codex execution target."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config, monitor
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


class _HostSignalExit(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def _return_code(value: int) -> int:
    return 128 + -value if value < 0 else value


def _run_supervised_host_codex(
    executable: Path,
    arguments: list[str],
    environment: dict[str, str],
    repository: Path,
) -> int:
    """Run Codex without losing its terminal or skipping monitor cleanup."""

    os.chdir(repository)
    try:
        child = subprocess.Popen([str(executable), *arguments], env=environment)
    except OSError as exc:
        raise HostTargetError(f"cannot start Codex: {exc}") from exc
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def forward(signum, _frame):
        if child.poll() is None:
            try:
                child.send_signal(signum)
            except OSError:
                pass
        raise _HostSignalExit(signum)

    try:
        signal.signal(signal.SIGINT, forward)
        signal.signal(signal.SIGTERM, forward)
        try:
            return _return_code(child.wait())
        except _HostSignalExit as exc:
            try:
                return _return_code(child.wait(timeout=10))
            except subprocess.TimeoutExpired:
                try:
                    child.terminate()
                except OSError:
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        child.kill()
                    except OSError:
                        pass
                    child.wait()
                return 128 + exc.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _start_host_monitor(
    *,
    config_root: Path,
    install_root: Path,
    prepared: PreparedLaunch,
    record: monitor.VolumeRegistration,
) -> monitor.ActiveMonitor | None:
    """Start optional scans without making a host Codex launch depend on Docker."""

    try:
        connection = monitor.load_connection(config_root)
    except monitor.MonitorError as exc:
        print(f"WARNING: Token Monitor state is invalid: {exc}", file=sys.stderr)
        return None
    if connection is None or not connection.enabled:
        return None
    docker = shutil.which("docker")
    if docker is None:
        print("WARNING: Token Monitor scan skipped: docker command not found", file=sys.stderr)
        return None

    def scan(force: bool, *, final: bool = False) -> None:
        monitor.scan_registration(
            config_root,
            docker,
            install_root,
            record,
            version=prepared.plan.cage_version,
            storage_policy=prepared.plan.storage_policy,
            allow_build=False,
            force=force,
            final=final,
        )

    def final_scan(force: bool) -> None:
        scan(force, final=True)

    return monitor.ActiveMonitor(
        scan,
        connection.interval_seconds,
        final_scan=final_scan,
    )


def run_host_target(
    prepared: PreparedLaunch,
    *,
    config_root: Path | None = None,
    install_root: Path | None = None,
) -> int:
    """Run pinned host Codex, optionally through an adopted Cage-only store.

    Unadopted host launches retain the historical ``execve`` path exactly:
    they neither require Docker nor create monitor state.  An explicit
    auth-source adoption changes only matching Cage launches to a private
    managed home, which gives the collector a reliable Cage-only session
    boundary.
    """

    plan = prepared.plan
    resolved = plan.runtime_config
    repository = Path(plan.repository)
    source_home = Path(
        resolved.host_codex_dir or (Path.home() / ".codex")
    ).expanduser()
    managed_session: monitor.HostSourceSession | None = None
    source_lease: monitor.HostSourceLease | None = None
    oauth_lease: OAuthSessionLease | None = None
    worker: monitor.ActiveMonitor | None = None

    if config_root is not None:
        try:
            record = monitor.registered_host_source(config_root, source_home)
        except monitor.MonitorError as exc:
            # Monitoring is optional.  A damaged monitor registry cannot turn
            # a normal host-native coding session into a failed launch.
            print(f"WARNING: Token Monitor host source skipped: {exc}", file=sys.stderr)
            record = None
        if record is not None:
            try:
                source_lease = monitor.HostSourceLease.acquire(
                    config_root, record.logical_id
                )
                # `monitor disable --auth` uses this same lease.  Re-read the
                # registration after acquiring it so an opt-out that wins the
                # race restores direct CODEX_HOME routing before Codex starts.
                record = monitor.registered_host_source(config_root, source_home)
                if record is None:
                    source_lease.close()
                    source_lease = None
                else:
                    has_oauth = any(
                        server.get("auth") == "oauth"
                        for server in resolved.remote_mcp
                    )
                    if has_oauth:
                        oauth_lease = OAuthSessionLease.acquire(
                            source_home, create=True
                        )
                    managed_session = monitor.prepare_host_source(
                        config_root,
                        record,
                        source_home,
                        copy_auth=resolved.codex_copy_auth != "0",
                        copy_oauth_credentials=has_oauth,
                    )
            except (monitor.MonitorError, SyncError) as exc:
                if oauth_lease is not None:
                    try:
                        oauth_lease.close()
                    except SyncError:
                        pass
                    oauth_lease = None
                if source_lease is not None:
                    try:
                        source_lease.close()
                    except monitor.MonitorError:
                        pass
                    source_lease = None
                raise HostTargetError(
                    f"cannot prepare monitored host Codex source: {exc}"
                ) from exc

    codex_home = managed_session.codex_home if managed_session else source_home
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
    if managed_session is None:
        print(f"  CODEX_HOME: {codex_home}", file=sys.stderr)
    else:
        print("  CODEX_HOME: Cage-managed host session store", file=sys.stderr)
        print("  Monitor:    adopted auth source (Cage sessions only)", file=sys.stderr)
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
    if oauth_lease is not None:
        print("  OAuth:      exclusive source CODEX_HOME lease", file=sys.stderr)
    print("", file=sys.stderr)

    try:
        if managed_session is None:
            if any(server.get("auth") == "oauth" for server in resolved.remote_mcp):
                oauth_lease = OAuthSessionLease.acquire(codex_home, create=True)
                print("  OAuth:      exclusive CODEX_HOME lease", file=sys.stderr)
                oauth_lease.preserve_across_exec()
            os.chdir(repository)
            os.execve(
                executable,
                [str(executable), *tool_arguments],
                environment,
            )
            return 127

        assert config_root is not None
        worker = _start_host_monitor(
            config_root=config_root,
            install_root=install_root or Path.cwd(),
            prepared=prepared,
            record=managed_session.record,
        )
        return _run_supervised_host_codex(
            executable,
            tool_arguments,
            environment,
            repository,
        )
    except SyncError as exc:
        raise HostTargetError(f"cannot start Codex OAuth session: {exc}") from exc
    finally:
        if worker is not None:
            worker.stop()
        if managed_session is not None:
            try:
                monitor.finish_host_source(managed_session)
            except monitor.MonitorError as exc:
                # The source wins on any compare-and-swap conflict.  The
                # completed coding session remains usable, while the warning
                # tells the user that changed managed credentials were
                # deliberately not written over an independent source update.
                print(f"WARNING: Codex credential sync skipped: {exc}", file=sys.stderr)
        if oauth_lease is not None:
            try:
                oauth_lease.close()
            except SyncError:
                pass
        if source_lease is not None:
            try:
                source_lease.close()
            except monitor.MonitorError:
                pass
