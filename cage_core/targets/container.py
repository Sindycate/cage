"""Ordinary container target and shared container construction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .. import bridge as bridge_policy, config, opencode_policy, storage
from ..opencode import (
    OpenCodeError,
    create_launch_snapshot,
    remove_snapshot,
    write_runtime_environment,
)
from ..lifecycle import (
    LifecycleCoordinator,
    Resource,
    terminate_process,
    wait_for_line,
)
from ..planning import CAGE_REGISTRY, PreparedLaunch
from ..state import (
    ClaudeSessionSync,
    OAuthReconciler,
    OpenCodeStateReconciler,
    SyncError,
)


class ContainerTargetError(RuntimeError):
    pass


class SignalExit(Exception):
    def __init__(self, status: int):
        self.status = status


@dataclass
class BridgeResult:
    process: subprocess.Popen[object]
    docker_arguments: list[str]
    names: list[str]
    label: str


@dataclass
class ContainerRuntime:
    prepared: PreparedLaunch
    install_root: Path
    config_root: Path
    docker: str
    lifecycle: LifecycleCoordinator = field(default_factory=LifecycleCoordinator)
    dependency_processes: list[tuple[str, subprocess.Popen[object]]] = field(
        default_factory=list
    )
    started_mcp_names: list[str] = field(default_factory=list)
    parallel_label: str = ""
    proxy_label: str = ""
    mcp_label: str = ""
    host_command_label: str = ""
    container_name_override: str = ""
    build_preflight_done: bool = False
    opencode_snapshot: Path | None = None
    opencode_environment: dict[str, str] = field(default_factory=dict)

    @property
    def plan(self):
        return self.prepared.plan

    @property
    def resolved(self):
        return self.plan.runtime_config

    @property
    def writable_roots(self) -> list[str]:
        return [
            mount.path
            for mount in self.plan.mounts
            if mount.mode == "rw"
        ]

    @property
    def container_name(self) -> str:
        return self.container_name_override or self.plan.container_name

    def run(self, arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.docker, *arguments], text=True, **kwargs)

    def output(self, arguments: list[str], **kwargs) -> str:
        result = self.run(
            arguments,
            stdout=subprocess.PIPE,
            check=False,
            **kwargs,
        )
        return result.stdout

    def register_temp(self, path: Path, name: str) -> Resource:
        return self.lifecycle.register(
            name,
            lambda: _unlink(path),
        )

    def start_managed_process(
        self,
        *,
        name: str,
        arguments: list[str],
        environment: dict[str, str],
        output_path: Path,
        log_path: Path,
        ready_timeout: float,
        ready_interval: float,
    ) -> subprocess.Popen[object]:
        output = output_path.open("wb")
        log = log_path.open("wb")
        try:
            process = subprocess.Popen(
                arguments,
                env=environment,
                stdout=output,
                stderr=log,
                start_new_session=True,
            )
        finally:
            output.close()
            log.close()
        self.lifecycle.register(
            name,
            lambda: _stop_process(process),
        )
        self.dependency_processes.append((name, process))
        if not wait_for_line(
            output_path,
            "READY",
            process,
            timeout_seconds=ready_timeout,
            interval_seconds=ready_interval,
        ):
            details = ""
            try:
                details = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                pass
            raise ContainerTargetError(
                f"{name} failed to start"
                + (f"\n{details.rstrip()}" if details else "")
            )
        return process


def _unlink(path: Path) -> int:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return 0


def _stop_process(process: subprocess.Popen[object]) -> int:
    terminate_process(process, grace_seconds=2.0, process_group=True)
    return 0


def _temporary_path(
    runtime: ContainerRuntime,
    *,
    prefix: str,
    directory: Path | None = None,
) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=prefix,
        dir=directory,
    )
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    path = Path(name)
    runtime.register_temp(path, f"temporary file {path.name}")
    return path


def _validate_before_effects(runtime: ContainerRuntime) -> None:
    plan = runtime.plan
    resolved = runtime.resolved
    if resolved.aws_access:
        if resolved.aws_access != bridge_policy.AWS_ACCESS_HOST_CLI:
            raise ContainerTargetError(
                f"unsupported AWS access mode: {resolved.aws_access}"
            )
        if not resolved.aws_profile:
            raise ContainerTargetError(
                "AWS host CLI access requires a selected AWS profile"
            )
        if plan.network == "off":
            raise ContainerTargetError(
                'aws_access = "host-cli" cannot be combined with --net off'
            )
        if shutil.which(bridge_policy.AWS_COMMAND_NAME) is None:
            raise ContainerTargetError(
                "AWS host CLI access was selected, but the host 'aws' command "
                "was not found in PATH"
            )
    if plan.tool == "claude":
        mode = resolved.claude_auth or "bedrock"
        if mode == "api-key" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ContainerTargetError(
                "CLAUDE_AUTH=api-key but ANTHROPIC_API_KEY is not set"
            )
        if mode not in {"bedrock", "api-key"}:
            raise ContainerTargetError(
                f"Invalid CLAUDE_AUTH: {mode} (use 'bedrock' or 'api-key')"
            )
    elif plan.tool == "codex":
        codex_home = Path(
            resolved.host_codex_dir or (Path.home() / ".codex")
        ).expanduser()
        config.validate_codex_layers(
            config.host_codex_payload_for(resolved),
            Path(plan.repository),
            codex_home,
        )
        for item in resolved.skill_mounts:
            skill_path = Path(item["path"]).expanduser()
            if not skill_path.is_dir():
                raise ContainerTargetError(
                    f"selected skill {item['name']!r} directory does not "
                    f"exist: {skill_path}"
                )
            if not (skill_path / "SKILL.md").is_file():
                raise ContainerTargetError(
                    f"selected skill {item['name']!r} is missing SKILL.md: "
                    f"{skill_path}"
                )
    else:
        if plan.target != "container":
            raise ContainerTargetError(
                "OpenCode currently supports only container execution"
            )
        for item in resolved.skill_mounts:
            skill_path = Path(item["path"]).expanduser()
            if not skill_path.is_dir() or not (skill_path / "SKILL.md").is_file():
                raise ContainerTargetError(
                    f"selected skill {item['name']!r} is missing or invalid: {skill_path}"
                )
    if plan.target == "desktop":
        if os.environ.get("CAGE_DESKTOP_INTERNAL") != "1":
            raise ContainerTargetError("invalid internal desktop launch state")
        repository = Path(plan.repository)
        for name in ("CAGE_DESKTOP_PUBLIC_KEY", "CAGE_DESKTOP_HEARTBEAT"):
            raw = os.environ.get(name, "")
            path = Path(raw)
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise ContainerTargetError(
                    "missing or unsafe internal desktop state file"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ContainerTargetError(
                    "missing or unsafe internal desktop state file"
                )
            resolved_path = path.resolve()
            if (
                resolved_path == repository
                or resolved_path.is_relative_to(repository)
            ):
                raise ContainerTargetError(
                    "desktop private state cannot be inside the writable repository"
                )


@contextmanager
def _collision_prompt(
    name: str,
) -> Iterator[tuple[TextIO, TextIO]]:
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")
    except OSError as exc:
        try:
            stdin_is_tty = sys.stdin.isatty()
        except (AttributeError, OSError, ValueError):
            stdin_is_tty = False
        if not stdin_is_tty:
            raise ContainerTargetError(
                f"container {name!r} already exists, but Cage has no "
                "interactive input for collision handling.\n"
                "Re-run from an interactive terminal to start a parallel "
                "instance or attach."
            ) from exc
        yield sys.stdin, sys.stderr
        return
    with tty:
        yield tty, tty


def _handle_collision(runtime: ContainerRuntime) -> None:
    name = runtime.container_name
    existing = runtime.output(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        stderr=subprocess.DEVNULL,
    ).strip()
    running = runtime.output(
        ["ps", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        stderr=subprocess.DEVNULL,
    ).strip()
    if not existing:
        return
    with _collision_prompt(name) as (input_stream, output_stream):
        next_name = ""
        parallel_index = 0
        for index in range(2, 10):
            candidate = f"{name}-{index}"
            occupied = runtime.output(
                [
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^{candidate}$",
                    "--format",
                    "{{.ID}}",
                ],
                stderr=subprocess.DEVNULL,
            ).strip()
            if not occupied:
                next_name = candidate
                parallel_index = index
                break
        actions: list[tuple[str, str]] = []
        if next_name:
            actions.append(
                (
                    "parallel",
                    f"Start a parallel instance ({next_name}, shares state)",
                )
            )
        if running:
            actions.append(("exec", "Open a shell in the existing container"))
        else:
            actions.append(
                ("rm", "Remove the stopped container and start fresh")
            )
        actions.append(("abort", "Abort"))
        output_stream.write(
            "\nA cage container already exists for this repo:\n"
            f"  Name:   {name}\n"
            f"  Status: {'running' if running else 'stopped'}\n\n"
            "What would you like to do?\n"
        )
        for index, (_, label) in enumerate(actions, 1):
            output_stream.write(f"  {index}) {label}\n")
        output_stream.write(f"\nChoice [{len(actions)}]: ")
        output_stream.flush()
        choice = input_stream.readline().strip() or str(len(actions))
    if not choice.isdigit() or not 1 <= int(choice) <= len(actions):
        raise ContainerTargetError("Invalid choice; aborting.")
    action = actions[int(choice) - 1][0]
    if action == "parallel":
        runtime.container_name_override = next_name
        runtime.parallel_label = f" (parallel #{parallel_index})"
    elif action == "exec":
        print(
            f"Attaching to existing container {name} (exec). Exit the shell "
            "to detach; the original cage session keeps running.",
            file=sys.stderr,
        )
        has_bash = runtime.run(
            [
                "exec",
                running,
                "sh",
                "-c",
                "command -v bash >/dev/null 2>&1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        os.execvp(
            runtime.docker,
            [
                runtime.docker,
                "exec",
                "-it",
                running,
                "bash" if has_bash else "sh",
            ],
        )
    elif action == "rm":
        result = runtime.run(
            ["rm", existing],
            stdout=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise ContainerTargetError(
                f"cannot remove stopped container {name!r}"
            )
    else:
        raise ContainerTargetError("Aborted.")


def _ensure_base_image(runtime: ContainerRuntime) -> None:
    base_image = f"cage-base:{runtime.plan.cage_version}"
    inspect = runtime.run(
        ["image", "inspect", base_image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspect.returncode == 0:
        return
    _ensure_build_capacity(runtime)
    print(f"Building shared base image {base_image}...")
    result = runtime.run(
        [
            "build",
            "--build-arg",
            f"CAGE_VERSION={runtime.plan.cage_version}",
            "-t",
            base_image,
            "-f",
            str(runtime.install_root / "Dockerfile.base"),
            str(runtime.install_root),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ContainerTargetError("shared base image build failed")


def _acquire_image(runtime: ContainerRuntime) -> None:
    plan = runtime.plan
    base_image = f"cage-base:{plan.cage_version}"
    latest = plan.image.partition(":")[0] + ":latest"
    if plan.rebuild:
        _ensure_build_capacity(runtime)
        print(f"Rebuilding {plan.image} from local Dockerfile...")
        result = runtime.run(
            [
                "build",
                "--no-cache",
                "--build-arg",
                f"CAGE_VERSION={plan.cage_version}",
                "-t",
                base_image,
                "-f",
                str(runtime.install_root / "Dockerfile.base"),
                str(runtime.install_root),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ContainerTargetError("shared base image rebuild failed")
        result = runtime.run(
            [
                "build",
                "--no-cache",
                "--build-arg",
                f"CAGE_BASE={base_image}",
                "--build-arg",
                f"CAGE_VERSION={plan.cage_version}",
                "-t",
                plan.image,
                "-t",
                latest,
                "-f",
                str(runtime.install_root / plan.dockerfile),
                str(runtime.install_root),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ContainerTargetError(f"{plan.tool} image rebuild failed")
        return
    inspect = runtime.run(
        ["image", "inspect", plan.image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspect.returncode == 0:
        return
    print(f"Image {plan.image} not found locally.")
    pull = runtime.run(
        ["pull", plan.registry_image],
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if pull.returncode == 0:
        for tag in (plan.image, latest):
            result = runtime.run(
                ["tag", plan.registry_image, tag],
                check=False,
            )
            if result.returncode != 0:
                raise ContainerTargetError("cannot tag pulled Cage image")
        print(f"Pulled {plan.registry_image}")
        return
    print("Pull failed or unavailable. Building locally...")
    _ensure_base_image(runtime)
    result = runtime.run(
        [
            "build",
            "--build-arg",
            f"CAGE_BASE={base_image}",
            "--build-arg",
            f"CAGE_VERSION={plan.cage_version}",
            "-t",
            plan.image,
            "-t",
            latest,
            "-f",
            str(runtime.install_root / plan.dockerfile),
            str(runtime.install_root),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ContainerTargetError(f"{plan.tool} image build failed")


def _ensure_build_capacity(runtime: ContainerRuntime) -> None:
    if runtime.build_preflight_done:
        return
    storage.preflight(
        runtime.docker,
        runtime.plan.storage_policy,
        preferred_image=runtime.plan.image,
        requires_build=True,
    )
    runtime.build_preflight_done = True


def _append_environment_argument(
    runtime: ContainerRuntime,
    arguments: list[str],
    name: str,
    value: str,
) -> None:
    if runtime.plan.tool == "opencode":
        previous = runtime.opencode_environment.get(name)
        if previous is not None and previous != value:
            raise ContainerTargetError(
                f"conflicting private OpenCode environment value for {name}"
            )
        runtime.opencode_environment[name] = value
        return
    arguments.extend(("-e", f"{name}={value}"))


def _start_netgate(runtime: ContainerRuntime) -> list[str]:
    if runtime.plan.network == "off":
        return ["--network", "none"]
    if runtime.plan.network != "gate":
        return []
    output_path = _temporary_path(runtime, prefix="cage-netgate-output-")
    log_path = _temporary_path(runtime, prefix="cage-netgate-log-")
    token = secrets.token_hex(32)
    environment = os.environ.copy()
    environment["CAGE_NETGATE_AUTH_TOKEN"] = token
    process = runtime.start_managed_process(
        name="netgate proxy",
        arguments=[
            sys.executable,
            "-I",
            str(runtime.install_root / "netgate-proxy.py"),
            "--project-hash",
            hashlib.md5(
                runtime.plan.repository.encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()[:8],
            "--container-name",
            runtime.container_name,
        ],
        environment=environment,
        output_path=output_path,
        log_path=log_path,
        ready_timeout=15.0,
        ready_interval=0.1,
    )
    port = ""
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("PORT="):
            port = line.partition("=")[2]
            break
    if not port.isdigit():
        raise ContainerTargetError("netgate proxy returned an invalid port")
    runtime.proxy_label = f" [NET:GATED :{port}]"
    proxy_url = (
        f"http://cage:{token}@host.docker.internal:{port}"
    )
    arguments = [
        "--add-host",
        "host.docker.internal:host-gateway",
    ]
    for name, value in (
        ("HTTP_PROXY", proxy_url),
        ("HTTPS_PROXY", proxy_url),
        ("http_proxy", proxy_url),
        ("https_proxy", proxy_url),
        ("NO_PROXY", "localhost,127.0.0.1"),
        ("no_proxy", "localhost,127.0.0.1"),
    ):
        _append_environment_argument(runtime, arguments, name, value)
    return arguments


def _parse_bridge_ports(
    output: Path, prefix: str
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    pattern = re.compile(
        rf"^{re.escape(prefix)}:([A-Za-z0-9_-]+)=PORT:([0-9]+)$"
    )
    for line in output.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(line)
        if match:
            entries.append((match.group(1), match.group(2)))
    return entries


def _start_bridge(
    runtime: ContainerRuntime,
    *,
    kind: str,
    definitions: list[dict[str, str]],
    already_has_host_gateway: bool,
    aws_profile: str = "",
) -> BridgeResult | None:
    selected_definitions = list(definitions)
    if aws_profile:
        if kind != "host-command":
            raise ContainerTargetError(
                "AWS host CLI access requires the host-command bridge"
            )
        if any(
            str(item.get("name", "")).upper()
            == bridge_policy.AWS_COMMAND_NAME.upper()
            for item in selected_definitions
        ):
            raise ContainerTargetError(
                "host command name 'aws' is reserved for AWS host CLI access"
            )
        selected_definitions.append(
            {"name": bridge_policy.AWS_COMMAND_NAME, "command": "aws"}
        )
    if not selected_definitions:
        return None
    if runtime.plan.network == "off":
        noun = "stdio MCP servers" if kind == "mcp" else "host commands"
        print(
            f"WARNING: {noun} ignored - network is disabled (--net off)",
            file=sys.stderr,
        )
        return None
    bridge_args: list[str] = []
    for root in runtime.writable_roots:
        bridge_args.extend(("--deny-executable-root", root))
    for name in runtime.resolved.extra_env:
        if runtime.resolved.aws_access and name.startswith("AWS_"):
            continue
        bridge_args.extend(("--pass-env", name))
    option = "--server" if kind == "mcp" else "--command"
    value_key = "command"
    for definition in selected_definitions:
        bridge_args.extend(
            (option, definition["name"], definition[value_key])
        )
    if aws_profile:
        bridge_args.extend(("--aws-profile", aws_profile))
    output_path = _temporary_path(
        runtime, prefix=f"cage-{kind}-bridge-output-"
    )
    log_path = _temporary_path(
        runtime, prefix=f"cage-{kind}-bridge-log-"
    )
    token = secrets.token_hex(32)
    environment = os.environ.copy()
    environment["CAGE_BRIDGE_AUTH_TOKEN"] = token
    frontend = "mcp-bridge.py" if kind == "mcp" else "host-cmd-bridge.py"
    display = "MCP bridge" if kind == "mcp" else "host command bridge"
    process = runtime.start_managed_process(
        name=display,
        arguments=[
            sys.executable,
            "-I",
            str(runtime.install_root / frontend),
            *bridge_args,
        ],
        environment=environment,
        output_path=output_path,
        log_path=log_path,
        ready_timeout=3.0,
        ready_interval=0.1,
    )
    prefix = "SERVER" if kind == "mcp" else "COMMAND"
    ports = _parse_bridge_ports(output_path, prefix)
    if len(ports) != len(selected_definitions):
        raise ContainerTargetError(f"{display} returned an invalid port map")
    docker_arguments: list[str] = []
    for name, port in ports:
        normalized = name.upper().replace("-", "_")
        _append_environment_argument(
            runtime,
            docker_arguments,
            (
                f"MCP_BRIDGE_PORT_{normalized}"
                if kind == "mcp"
                else f"HOST_CMD_BRIDGE_PORT_{normalized}"
            ),
            port,
        )
    names = [name for name, _ in ports]
    if kind == "mcp":
        mapping = {
            name: int(port)
            for name, port in ports
        }
        _append_environment_argument(
            runtime, docker_arguments, "MCP_BRIDGE_HOST", "host.docker.internal"
        )
        _append_environment_argument(
            runtime, docker_arguments, "MCP_BRIDGE_TOKEN", token
        )
        _append_environment_argument(
            runtime,
            docker_arguments,
            "CAGE_MCP_SERVERS",
            json.dumps(mapping, separators=(",", ":")),
        )
        label = " [MCP]"
        runtime.started_mcp_names.extend(names)
        runtime.mcp_label = label
    else:
        _append_environment_argument(
            runtime,
            docker_arguments,
            "HOST_CMD_BRIDGE_HOST",
            "host.docker.internal",
        )
        _append_environment_argument(
            runtime, docker_arguments, "HOST_CMD_BRIDGE_TOKEN", token
        )
        _append_environment_argument(
            runtime,
            docker_arguments,
            "CAGE_HOST_COMMANDS",
            " ".join(names),
        )
        label = " [HOST-CMD + AWS]" if aws_profile else " [HOST-CMD]"
        runtime.host_command_label = label
    if not already_has_host_gateway:
        docker_arguments.extend(
            ("--add-host", "host.docker.internal:host-gateway")
        )
    return BridgeResult(
        process=process,
        docker_arguments=docker_arguments,
        names=names,
        label=label,
    )


def _resolve_github_token(resolved) -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    gh = shutil.which("gh")
    if gh is None:
        return ""
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
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""


def _base_docker_arguments(runtime: ContainerRuntime) -> list[str]:
    plan = runtime.plan
    resolved = runtime.resolved
    arguments = [
        "--name",
        runtime.container_name,
        "--hostname",
        runtime.container_name,
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--tmpfs",
        (
            "/tmp:rw,nosuid,nodev,exec,mode=1777"
            if plan.tool == "opencode"
            else "/tmp"
        ),
        "--tmpfs",
        "/run",
        "-v",
        f"{plan.repository}:{plan.repository}",
        "-e",
        f"WORKSPACE_DIR={plan.repository}",
        "-e",
        f"HOST_UID={os.getuid()}",
        "-e",
        f"HOST_GID={os.getgid()}",
    ]
    if plan.target != "desktop":
        arguments = ["--rm", "-it", *arguments]
    if os.environ.get("TERM"):
        arguments.extend(("-e", f"TERM={os.environ['TERM']}"))
    if os.environ.get("COLORTERM"):
        arguments.extend(
            ("-e", f"COLORTERM={os.environ['COLORTERM']}")
        )
    arguments.extend(
        (
            "--security-opt",
            "apparmor=unconfined",
            "--security-opt",
            "seccomp=unconfined",
        )
    )
    if plan.tool == "claude":
        home = Path.home()
        host_claude = home / ".claude"
        arguments.extend(
            (
                "-v",
                f"{plan.volume_name}:{plan.container_home}/.claude",
                "-v",
                f"{host_claude}:/host-claude:ro",
            )
        )
        if (home / ".claude.json").is_file():
            arguments.extend(
                ("-v", f"{home / '.claude.json'}:/host-claude-json:ro")
            )
        ccstatusline = home / ".config" / "ccstatusline"
        if ccstatusline.is_dir():
            arguments.extend(
                ("-v", f"{ccstatusline}:/host-ccstatusline:ro")
            )
        auth = resolved.claude_auth or "bedrock"
        if auth == "bedrock":
            arguments.extend(
                (
                    "-e",
                    "CLAUDE_CODE_USE_BEDROCK=1",
                    "-e",
                    f"AWS_PROFILE={resolved.aws_profile}",
                    "-e",
                    f"AWS_REGION={resolved.aws_region}",
                    "-v",
                    f"{home / '.aws' / 'credentials'}:"
                    f"{plan.container_home}/.aws/credentials:ro",
                )
            )
        else:
            arguments.extend(
                ("-e", f"ANTHROPIC_API_KEY={os.environ['ANTHROPIC_API_KEY']}")
            )
    elif plan.tool == "codex":
        arguments.extend(
            ("-v", f"{plan.volume_name}:{plan.container_home}/.codex")
        )
        codex_home = Path(
            resolved.host_codex_dir or (Path.home() / ".codex")
        ).expanduser()
        if codex_home.is_dir():
            arguments.extend(("-v", f"{codex_home}:/host-codex:ro"))
        if resolved.skill_mounts:
            skill_names: list[str] = []
            for item in resolved.skill_mounts:
                arguments.extend(
                    (
                        "-v",
                        f"{Path(item['path']).expanduser()}:"
                        f"/host-agent-skills/{item['name']}:ro",
                    )
                )
                skill_names.append(item["name"])
            arguments.extend(
                ("-e", "CAGE_SKILL_NAMES=" + " ".join(skill_names))
            )
        else:
            agents = Path(
                resolved.host_agents_dir or (Path.home() / ".agents")
            ).expanduser()
            if agents.is_dir():
                arguments.extend(("-v", f"{agents}:/host-agents:ro"))
        if os.environ.get("OPENAI_API_KEY"):
            arguments.extend(
                ("-e", f"OPENAI_API_KEY={os.environ['OPENAI_API_KEY']}")
            )
        if resolved.codex_copy_auth == "0":
            arguments.extend(("-e", "CODEX_COPY_AUTH=0"))
        arguments.extend(
            ("-e", f"CAGE_CODEX_PROFILE={resolved.codex_profile}")
        )
        if plan.target == "desktop":
            arguments.extend(
                (
                    "--cap-add",
                    "SYS_CHROOT",
                    "--label",
                    "com.sindycate.cage.desktop-target="
                    + os.environ["CAGE_DESKTOP_TARGET_ID"],
                    "-e",
                    "CAGE_DESKTOP_REMOTE=1",
                    "-e",
                    "CAGE_DESKTOP_ENV_NAMES="
                    + " ".join(
                        name
                        for name in resolved.extra_env
                        if not resolved.aws_access or not name.startswith("AWS_")
                    ),
                    "-v",
                    os.environ["CAGE_DESKTOP_PUBLIC_KEY"]
                    + ":/cage-desktop/client.pub:ro",
                    "-v",
                    os.environ["CAGE_DESKTOP_HEARTBEAT"]
                    + ":/cage-desktop/heartbeat:ro",
                )
            )
    else:
        arguments.extend(
            (
                "-v",
                f"{plan.volume_name}:{plan.container_home}/.cage-state",
                "-e",
                "OPENCODE_DISABLE_AUTOUPDATE=1",
                "-e",
                "CAGE_OPENCODE_PLUGINS="
                + ("1" if resolved.opencode_plugins == "1" else "0"),
            )
        )
        if runtime.opencode_snapshot is None:
            raise ContainerTargetError("OpenCode launch snapshot was not prepared")
        arguments.extend(
            (
                "-v",
                f"{runtime.opencode_snapshot}:/cage-opencode-snapshot:ro",
            )
        )
        callback_ports = opencode_policy.callback_ports(
            list(runtime.prepared.request.tool_arguments)
        )
        if callback_ports:
            arguments.extend(
                ("-e", "CAGE_OPENCODE_CALLBACK_PORTS=" + " ".join(callback_ports))
            )
            for port in callback_ports:
                arguments.extend(("-p", f"127.0.0.1:{port}:{port}"))
    if resolved.git_user_name:
        _append_environment_argument(
            runtime, arguments, "GIT_USER_NAME", resolved.git_user_name
        )
    if resolved.git_user_email:
        _append_environment_argument(
            runtime, arguments, "GIT_USER_EMAIL", resolved.git_user_email
        )
    if resolved.ssh_key:
        ssh_key = Path(resolved.ssh_key).expanduser()
        if ssh_key.is_file():
            arguments.extend(
                ("-v", f"{ssh_key}:{plan.container_home}/.ssh/id:ro")
            )
            _append_environment_argument(
                runtime,
                arguments,
                "GIT_SSH_COMMAND",
                "ssh -i "
                f"{plan.container_home}/.ssh/id -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new",
            )
            known_hosts = Path.home() / ".ssh" / "known_hosts"
            if known_hosts.is_file():
                arguments.extend(
                    (
                        "-v",
                        f"{known_hosts}:"
                        f"{plan.container_home}/.ssh/known_hosts:ro",
                    )
                )
    if resolved.ssh_host:
        _append_environment_argument(
            runtime, arguments, "SSH_HOST", resolved.ssh_host
        )
    for name in resolved.extra_env:
        if resolved.aws_access and name.startswith("AWS_"):
            continue
        value = os.environ.get(name)
        if value:
            _append_environment_argument(runtime, arguments, name, value)
    if resolved.remote_mcp and plan.tool != "opencode":
        arguments.extend(
            (
                "-e",
                "CAGE_REMOTE_MCP_SERVERS="
                + json.dumps(
                    resolved.remote_mcp,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            )
        )
    if resolved.gh_auth == "1":
        host_gh = Path(
            os.environ.get(
                "XDG_CONFIG_HOME", Path.home() / ".config"
            )
        ) / "gh"
        if host_gh.is_dir():
            arguments.extend(("-v", f"{host_gh}:/host-gh:ro"))
        token = _resolve_github_token(resolved)
        if token:
            _append_environment_argument(runtime, arguments, "GH_TOKEN", token)
    return arguments


def _create_opencode_snapshot(runtime: ContainerRuntime) -> None:
    destination = runtime.config_root / (
        ".opencode-launch-" + str(os.getpid()) + "-" + secrets.token_hex(8)
    )
    create_launch_snapshot(
        destination=destination,
        host_config_directory=Path(
            runtime.resolved.host_opencode_config_dir
            or Path.home() / ".config" / "opencode"
        ).expanduser(),
        repository=Path(runtime.plan.repository),
        stdio_mcp=(
            [] if runtime.plan.network == "off" else runtime.resolved.stdio_mcp
        ),
        remote_mcp=runtime.resolved.remote_mcp,
        skill_mounts=runtime.resolved.skill_mounts,
        host_agents_directory=(
            None
            if runtime.resolved.skill_mounts
            else Path(
                runtime.resolved.host_agents_dir or Path.home() / ".agents"
            ).expanduser()
        ),
        plugins_enabled=runtime.resolved.opencode_plugins == "1",
    )
    runtime.opencode_snapshot = destination
    runtime.lifecycle.register(
        "OpenCode configuration snapshot",
        lambda: remove_snapshot(destination),
    )


def _append_extra_mounts(
    runtime: ContainerRuntime, arguments: list[str]
) -> None:
    for mount in runtime.plan.mounts:
        if mount.source == "repo":
            continue
        suffix = "" if mount.mode == "rw" else ":ro"
        arguments.extend(("-v", f"{mount.path}:{mount.path}{suffix}"))


def _create_claude_mcp_overlay(runtime: ContainerRuntime) -> Path:
    path = _temporary_path(
        runtime,
        prefix=".cage-mcp.",
        directory=runtime.config_root,
    )
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ContainerTargetError(
            "generated MCP destination is not a regular file"
        )
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identities = {
            (before.st_dev, before.st_ino),
            (opened.st_dev, opened.st_ino),
            (current.st_dev, current.st_ino),
        }
        if len(identities) != 1:
            raise ContainerTargetError(
                "generated MCP destination changed while it was being opened"
            )
        os.ftruncate(descriptor, 0)
        value = {
            "mcpServers": {
                name: {
                    "type": "stdio",
                    "command": "mcp-relay",
                    "args": [name],
                }
                for name in runtime.started_mcp_names
            }
        }
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(value, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return path


def _run_ordinary(
    runtime: ContainerRuntime,
    docker_arguments: list[str],
    tool_arguments: list[str],
) -> int:
    command = [
        runtime.docker,
        "run",
        *docker_arguments,
        runtime.plan.image,
        *tool_arguments,
        *runtime.prepared.request.tool_arguments,
    ]
    if not runtime.lifecycle.requires_supervision:
        os.execvp(runtime.docker, command)
        return 127
    result = subprocess.run(command, check=False)
    return result.returncode


def _install_signal_handlers():
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def handle(signum, _frame):
        raise SignalExit(128 + signum)

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    return previous


def _restore_signal_handlers(previous) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run_container_target(
    prepared: PreparedLaunch,
    *,
    install_root: Path,
    config_root: Path,
) -> int:
    docker = shutil.which("docker")
    if docker is None:
        raise ContainerTargetError("docker command not found in PATH")
    runtime = ContainerRuntime(
        prepared=prepared,
        install_root=install_root,
        config_root=config_root.resolve(),
        docker=docker,
    )
    previous_handlers = _install_signal_handlers()
    primary_status = 0
    try:
        _validate_before_effects(runtime)
        if os.environ.get("CAGE_STORAGE_PREFLIGHT_DONE") != "1":
            storage.preflight(
                docker,
                prepared.plan.storage_policy,
                preferred_image=prepared.plan.image,
                requires_build=prepared.plan.rebuild,
            )
        runtime.build_preflight_done = prepared.plan.rebuild
        for warning in prepared.plan.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        if prepared.plan.tool == "opencode":
            _create_opencode_snapshot(runtime)
        _handle_collision(runtime)
        _acquire_image(runtime)
        if prepared.plan.tool == "opencode":
            volume = runtime.run(
                ["volume", "create", prepared.plan.volume_name],
                stdout=subprocess.DEVNULL,
                check=False,
            )
            if volume.returncode != 0:
                raise ContainerTargetError("cannot create OpenCode state volume")
            opencode_state = OpenCodeStateReconciler(
                volume_name=prepared.plan.volume_name,
                image=prepared.plan.image,
                host_directory=Path(
                    runtime.resolved.host_opencode_data_dir
                    or Path.home() / ".local" / "share" / "opencode"
                ).expanduser(),
                config_directory=config_root.resolve(),
                selected_mcp_names={
                    str(server["name"])
                    for server in runtime.resolved.remote_mcp
                    if server.get("auth") == "oauth"
                },
                copy_auth=runtime.resolved.opencode_copy_auth != "0",
                selected_mcp_urls={
                    str(server["name"]): str(server["url"])
                    for server in runtime.resolved.remote_mcp
                    if server.get("auth") == "oauth"
                },
                docker=docker,
            )
            opencode_state.sync_in()
            runtime.lifecycle.register(
                "OpenCode authentication reconciliation",
                lambda: _cleanup_opencode_state(opencode_state),
            )
        proxy_arguments = _start_netgate(runtime)
        mcp = _start_bridge(
            runtime,
            kind="mcp",
            definitions=runtime.resolved.stdio_mcp,
            already_has_host_gateway=prepared.plan.network == "gate",
        )
        host_command = _start_bridge(
            runtime,
            kind="host-command",
            definitions=runtime.resolved.host_commands,
            already_has_host_gateway=(
                prepared.plan.network == "gate" or mcp is not None
            ),
            aws_profile=(
                runtime.resolved.aws_profile
                if runtime.resolved.aws_access == bridge_policy.AWS_ACCESS_HOST_CLI
                else ""
            ),
        )
        if runtime.resolved.remote_mcp and not runtime.mcp_label:
            runtime.mcp_label = " [MCP]"

        session_sync: ClaudeSessionSync | None = None
        if prepared.plan.tool == "claude":
            session_sync = ClaudeSessionSync(
                docker=docker,
                volume_name=prepared.plan.volume_name,
                repository=prepared.plan.repository,
                host_claude_dir=Path.home() / ".claude",
                enabled=(
                    runtime.resolved.session_sync != "0"
                ),
            )
            session_sync.sync_in()
            if session_sync.enabled:
                runtime.lifecycle.register(
                    "Claude session sync",
                    session_sync.sync_out,
                )

        oauth: OAuthReconciler | None = None
        if prepared.plan.tool == "codex":
            codex_home = Path(
                runtime.resolved.host_codex_dir
                or (Path.home() / ".codex")
            ).expanduser()
            if codex_home.is_dir():
                volume = runtime.run(
                    ["volume", "create", prepared.plan.volume_name],
                    stdout=subprocess.DEVNULL,
                    check=False,
                )
                if volume.returncode != 0:
                    raise ContainerTargetError(
                        "cannot create Codex state volume"
                    )
                oauth = OAuthReconciler(
                    volume_name=prepared.plan.volume_name,
                    image=prepared.plan.image,
                    host_directory=codex_home,
                    config_directory=config_root.resolve(),
                    docker=docker,
                )
                oauth.reconcile()
                runtime.lifecycle.register(
                    "Codex OAuth reconciliation",
                    lambda: _cleanup_oauth(oauth),
                )
        yolo_label = " [YOLO]" if prepared.plan.yolo else ""
        network_label = runtime.proxy_label
        if prepared.plan.network == "off":
            network_label = " [NET:OFF]"
        display = {
            "claude": "Claude Code",
            "codex": "Codex CLI",
            "opencode": "OpenCode",
        }[prepared.plan.tool]
        print(
            f"=== {display} Container{yolo_label}{network_label}"
            f"{runtime.mcp_label}{runtime.host_command_label} ==="
        )
        print(f"  Repo:      {prepared.plan.repository}")
        if prepared.plan.preset_name:
            print(f"  Preset:    {prepared.plan.preset_name}")
        print(
            f"  Container: {runtime.container_name}"
            f"{runtime.parallel_label}"
        )
        print(f"  Volume:    {prepared.plan.volume_name}")
        print("  MCP:       selected packs only")
        print("==============================")
        sys.stdout.flush()

        docker_arguments = _base_docker_arguments(runtime)
        docker_arguments.extend(proxy_arguments)
        if mcp is not None:
            docker_arguments.extend(mcp.docker_arguments)
        if host_command is not None:
            docker_arguments.extend(host_command.docker_arguments)
        if prepared.plan.tool == "opencode":
            assert runtime.opencode_snapshot is not None
            write_runtime_environment(
                runtime.opencode_snapshot / "runtime-env.json",
                runtime.opencode_environment,
            )
        _append_extra_mounts(runtime, docker_arguments)
        if prepared.plan.tool == "claude" and prepared.plan.target != "desktop":
            overlay = _create_claude_mcp_overlay(runtime)
            docker_arguments.extend(
                (
                    "-v",
                    f"{overlay}:{prepared.plan.repository}/.mcp.json:ro",
                )
            )
        tool_arguments: list[str] = []
        if prepared.plan.yolo:
            if prepared.plan.tool == "claude":
                tool_arguments.append("--dangerously-skip-permissions")
            elif prepared.plan.tool == "codex":
                tool_arguments.append("--yolo")
                docker_arguments.extend(("-e", "CAGE_YOLO=1"))
            else:
                tool_arguments.append("--auto")
        if prepared.plan.target == "desktop":
            from .desktop import run_desktop

            primary_status = run_desktop(runtime, docker_arguments)
        else:
            primary_status = _run_ordinary(
                runtime, docker_arguments, tool_arguments
            )
    except SignalExit as exc:
        primary_status = exc.status
    except SyncError as exc:
        print(
            f"ERROR: authentication state sync failed: {exc}",
            file=sys.stderr,
        )
        primary_status = 1
    except (
        ContainerTargetError,
        OpenCodeError,
        config.ConfigError,
        storage.StorageError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        primary_status = 1
    finally:
        primary_status = runtime.lifecycle.cleanup(primary_status)
        _restore_signal_handlers(previous_handlers)
    return primary_status


def _cleanup_oauth(oauth: OAuthReconciler) -> int:
    try:
        oauth.reconcile()
        return 0
    except (SyncError, OSError, ValueError) as exc:
        print(
            f"ERROR: Codex OAuth credential sync failed: {exc}",
            file=sys.stderr,
        )
        return 1


def _cleanup_opencode_state(state: OpenCodeStateReconciler) -> int:
    try:
        state.sync_out()
        return 0
    except (SyncError, OSError, ValueError) as exc:
        print(
            f"ERROR: OpenCode authentication sync failed: {exc}",
            file=sys.stderr,
        )
        return 1
