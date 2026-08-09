"""Python host launcher CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import config, opencode_policy, storage
from .models import LaunchRequest
from .planning import PlanError, PreparedLaunch, build_launch_plan
from .targets.host import HostTargetError, run_host_target


USAGE = """Usage: cage [claude|codex|opencode] [-y|--yolo] [-i|--interactive] [--host|--container|--desktop] [--no-open] [--rebuild] [--preset NAME] [--net open|gate|off] [--mount-ro PATH] [--mount-rw PATH] [/path/to/repo] [tool-args...]

Tools:
  claude    Run Claude Code (default)
  codex     Run OpenAI Codex CLI
  opencode  Run OpenCode

Commands:
  setup                     Create starter ~/.config/cage/config.toml
  config                    Manage central TOML configuration
  config init               Create starter config.toml
  config edit               Edit config.toml in $EDITOR
  config explain PATH       Explain the resolved launch configuration
  config doctor PATH        Validate the resolved launch configuration
  mcp login NAME PATH       Authenticate with a selected OAuth MCP server
  mcp logout NAME PATH      Remove selected OAuth MCP authentication
  desktop setup             Register Cage's managed SSH Include
  desktop start ...         Start or reuse a persistent Desktop target
  desktop restart ...       Restart a Desktop target
  desktop status ...        Show a Desktop target's state
  desktop stop ...          Stop a Desktop target and preserve its state
  desktop logs ...          Print a Desktop target's supervisor log
  desktop list              List registered Desktop targets
  desktop remove ...        Delete a Desktop target after confirmation
  netgate                    Manage domain allow/deny lists for --net gate
  netgate list [PATH]        Show rules (defaults + global + project)
  netgate allow DOMAIN       Allow a domain (project or --global)
  netgate deny DOMAIN        Deny a domain (project)
  netgate remove DOMAIN      Remove a decision (re-enables prompting)
  netgate reset [PATH]       Delete all project decisions
  update [claude|codex|opencode] Refresh the tool binary only (fast; no full rebuild)
  storage status             Show Docker capacity and exact cleanup candidates
  storage clean              Preview and confirm narrow Cage image cleanup

Options:
  --preset NAME     Use a central config preset (one-shot override)
  -i, --interactive Open the configuration and launch TUI (path defaults to cwd)
  -y, --yolo         Skip all permission prompts
  --no-yolo          Override a saved yolo setting for this launch
  --net open         Full network access (default without --yolo)
  --net gate         Domain-gated network via proxy (default with --yolo)
  --net off          No network access at all
  --mount-ro PATH    Mount a host dir read-only at the same path (repeatable)
  --mount-rw PATH    Mount a host dir read-write at the same path (repeatable)
  --host             Run Codex natively on the host (no Docker isolation)
  --container        Force container execution (override a saved host target)
  --desktop          Run Codex for ChatGPT Desktop in a persistent Cage container
  --no-open          Do not open ChatGPT after a Desktop target becomes ready
  --rebuild          Force local rebuild (get latest tool version)
  --version, -V      Show version and exit
  --help, -h         Show this help and exit

Examples:
  cage                         Open the TUI for the current directory
  cage setup
  cage config edit
  cage mcp login dash0 ~/projects/myapp
  cage ~/projects/myapp
  cage --preset codex-company ~/projects/myapp
  cage --preset codex-company --desktop ~/projects/myapp
  cage desktop status --preset codex-company ~/projects/myapp
  cage --interactive ~/projects/myapp
  cage codex ~/projects/myapp
  cage opencode ~/projects/myapp
  cage codex -y ~/projects/myapp
  cage -y ~/projects/myapp
  cage --net gate ~/projects/myapp
  cage --mount-ro ~/code/shared-lib ~/projects/myapp
  cage ~/projects/myapp --resume
  cage ~/projects/myapp -p 'explain this codebase'"""


class CliError(ValueError):
    pass


def _need_value(arguments: list[str], index: int, option: str) -> str:
    if index + 1 >= len(arguments) or not arguments[index + 1]:
        noun = "mode" if option == "--net" else "path" if option.startswith("--mount-") else "value"
        raise CliError(f"Missing {noun} after {option}")
    return arguments[index + 1]


def parse_launch_request(arguments: list[str]) -> LaunchRequest:
    args = list(arguments)
    explicit_tool = ""
    if args and args[0] in {"claude", "codex", "opencode"}:
        explicit_tool = args.pop(0)
    preset = ""
    interactive = False
    yolo: bool | None = None
    network = ""
    target = ""
    rebuild = False
    no_open = False
    mounts: list[tuple[str, str]] = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        argument = args[index]
        if argument == "--":
            index += 1
            break
        if argument in {"--help", "-h"}:
            raise CliError("__help__")
        if argument in {"-y", "--yolo"}:
            yolo = True
        elif argument == "--no-yolo":
            yolo = False
        elif argument in {"-i", "--interactive"}:
            interactive = True
        elif argument == "--net":
            network = _need_value(args, index, "--net")
            index += 1
        elif argument.startswith("--net="):
            network = argument.partition("=")[2]
        elif argument == "--preset":
            preset = _need_value(args, index, "--preset")
            index += 1
        elif argument.startswith("--preset="):
            preset = argument.partition("=")[2]
        elif argument == "--rebuild":
            rebuild = True
        elif argument in {"--host", "--container", "--desktop"}:
            selected = argument[2:]
            if target:
                raise CliError(
                    "ERROR: --host, --container, and --desktop cannot be "
                    "combined (they are mutually exclusive)"
                )
            target = selected
        elif argument == "--no-open":
            no_open = True
        elif argument in {"--mount-ro", "--mount-rw"}:
            value = _need_value(args, index, argument)
            mounts.append((argument.removeprefix("--mount-"), value))
            index += 1
        elif argument.startswith("--mount-ro="):
            mounts.append(("ro", argument.partition("=")[2]))
        elif argument.startswith("--mount-rw="):
            mounts.append(("rw", argument.partition("=")[2]))
        else:
            raise CliError(f"Unknown option: {argument}")
        index += 1
    if interactive and preset:
        raise CliError("ERROR: --interactive cannot be combined with --preset")
    if preset and not config.NAME_RE.fullmatch(preset):
        raise CliError(
            f"Invalid preset name: {preset!r} (use only letters, digits, dots, "
            "hyphens, underscores)"
        )
    if network and network not in {"open", "gate", "off"}:
        raise CliError(
            f"Invalid --net mode: {network} (use open, gate, or off)"
        )
    remaining = args[index:]
    if interactive and not remaining:
        remaining = ["."]
    if not remaining:
        interactive = True
        remaining = ["."]
    return LaunchRequest(
        repo_operand=remaining[0],
        tool_arguments=tuple(remaining[1:]),
        explicit_tool=explicit_tool,
        preset=preset,
        interactive=interactive,
        yolo=yolo,
        network=network,
        target=target,
        rebuild=rebuild,
        no_open=no_open,
        extra_mounts=tuple(mounts),
    )


def _run_tui(
    request: LaunchRequest,
    *,
    install_root: Path,
    config_path: Path,
    repository: Path,
) -> LaunchRequest:
    if not config_path.is_file():
        raise CliError(f"central config not found at {config_path}")
    try:
        tty_descriptor = os.open("/dev/tty", os.O_RDONLY)
    except OSError as exc:
        raise CliError("interactive mode requires a TTY") from exc
    try:
        if not os.isatty(tty_descriptor):
            raise CliError("interactive mode requires a TTY")
    finally:
        os.close(tty_descriptor)
    descriptor, result_name = tempfile.mkstemp(
        prefix=".tui-result.", dir=config_path.parent
    )
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    result_path = Path(result_name)
    arguments = [
        sys.executable,
        "-I",
        str(install_root / "cage-tui.py"),
        "--backend",
        str(install_root / "cage-config.py"),
        "--config",
        str(config_path),
        "--repo",
        str(repository),
        "--result",
        str(result_path),
    ]
    if request.network:
        arguments.extend(("--net-override", request.network))
    if request.explicit_tool:
        arguments.extend(("--tool-override", request.explicit_tool))
    if request.yolo is not None:
        arguments.extend(
            ("--yolo-override", "on" if request.yolo else "off")
        )
    if request.target:
        arguments.extend(("--target-override", request.target))
    status = subprocess.run(arguments, check=False).returncode
    if status == 70:
        print(
            "WARNING: curses UI unavailable; using the launch-only prompt.",
            file=sys.stderr,
        )
        result_path.unlink(missing_ok=True)
        return replace(request, legacy_interactive=True)
    if status != 0:
        result_path.unlink(missing_ok=True)
        raise SystemExit(status)
    return replace(request, tui_result=str(result_path))


def _resolve(
    request: LaunchRequest,
    *,
    config_path: Path,
    repository: Path,
) -> config.ResolvedConfig:
    if not config_path.is_file():
        raise CliError(
            f"central config not found at {config_path}\n"
            "Run: cage config init && cage config edit"
        )
    data = config.load_config(config_path)
    if request.tui_result:
        return config.resolve_ui_result(
            data,
            config_path,
            str(repository),
            Path(request.tui_result),
            request.explicit_tool,
            mcp_inventory=True,
        )
    if request.legacy_interactive:
        selections = config.interactive_select(
            data,
            str(repository),
            explicit_tool=request.explicit_tool,
            explicit_net=request.network,
            yolo_override=request.yolo,
        )
        return config.resolve_interactive_selection(
            data,
            config_path,
            str(repository),
            selections,
            explicit_tool=request.explicit_tool,
            mcp_inventory=True,
        )
    return config.resolve_config(
        data,
        config_path,
        str(repository),
        request.preset,
        request.explicit_tool,
        mcp_inventory=True,
    )


def _exec_python(script: Path, arguments: list[str], environment: dict[str, str]) -> None:
    os.execve(
        sys.executable,
        [sys.executable, "-I", str(script), *arguments],
        environment,
    )


def _dispatch_management(
    arguments: list[str],
    *,
    install_root: Path,
    config_root: Path,
    cage_version: str,
) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    rest = arguments[1:]
    environment = os.environ.copy()
    if command in {"setup", "--setup"}:
        _exec_python(
            install_root / "cage-config.py",
            ["--config", str(config_root / "config.toml"), "init", *rest],
            environment,
        )
    if command in {"config", "--config"}:
        _exec_python(
            install_root / "cage-config.py",
            ["--config", str(config_root / "config.toml"), *rest],
            environment,
        )
    if command in {"mcp", "--mcp"}:
        _exec_python(
            install_root / "cage-config.py",
            [
                "--config",
                str(config_root / "config.toml"),
                "mcp",
                *rest,
            ],
            environment,
        )
    if command in {"desktop", "--desktop-command"}:
        _exec_python(
            install_root / "cage-desktop.py",
            [
                "--config-dir",
                str(config_root),
                "--launcher",
                str(install_root / "cage"),
                *rest,
            ],
            environment,
        )
    if command in {"netgate", "--netgate"}:
        environment["SCRIPT_DIR"] = str(install_root)
        os.execve(
            "/bin/bash",
            ["/bin/bash", str(install_root / "cage-netgate.sh"), *rest],
            environment,
        )
    if command in {"update", "--update"}:
        raise SystemExit(
            _run_update(
                rest,
                install_root=install_root,
                config_root=config_root,
                cage_version=cage_version,
            )
        )
    if command == "storage":
        raise SystemExit(_run_storage(rest, config_root=config_root))
    return False


def _run_storage(arguments: list[str], *, config_root: Path) -> int:
    if len(arguments) != 1 or arguments[0] not in {"status", "clean"}:
        print("Usage: cage storage status|clean", file=sys.stderr)
        return 1
    try:
        config_path = config_root / "config.toml"
        policy = (
            config.storage_policy_from_config(config.load_config(config_path))
            if config_path.is_file()
            else storage.StoragePolicy()
        )
        return storage.run_storage_command(arguments[0], policy)
    except (config.ConfigError, storage.StorageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _default_tool(config_path: Path) -> str:
    if not config_path.is_file():
        return "claude"
    try:
        data = config.load_config(config_path)
        preset_name = data.get("default_preset") or config.as_table(
            data, "defaults"
        ).get("default_preset")
        preset = config.as_table(data, "presets").get(preset_name, {})
        if isinstance(preset, dict):
            tool = preset.get("tool")
            auth_name = preset.get("auth")
            if not tool and isinstance(auth_name, str):
                auth = config.as_table(data, "auth").get(auth_name, {})
                if isinstance(auth, dict):
                    tool = auth.get("tool")
            if tool in {"claude", "codex", "opencode"}:
                return str(tool)
    except (config.ConfigError, OSError):
        pass
    return "claude"


def _run_update(
    arguments: list[str],
    *,
    install_root: Path,
    config_root: Path,
    cage_version: str,
) -> int:
    tool = arguments[0] if arguments else _default_tool(
        config_root / "config.toml"
    )
    if len(arguments) > 1:
        print("update accepts at most one tool name", file=sys.stderr)
        return 1
    if tool == "claude":
        image = f"claude-code:{cage_version}"
        registry = f"ghcr.io/sindycate/cage/claude-code:{cage_version}"
        dockerfile = "Dockerfile"
        overlay = (
            f"FROM {image}\n"
            "USER claude\n"
            "RUN curl -fsSL https://claude.ai/install.sh | bash\n"
            "USER root\n"
            "RUN chmod -R a+rwX /home/claude\n"
            f"LABEL io.cage.managed=\"true\" io.cage.role=\"claude\" io.cage.version=\"{cage_version}\"\n"
        )
    elif tool == "codex":
        image = f"codex:{cage_version}"
        registry = f"ghcr.io/sindycate/cage/codex:{cage_version}"
        dockerfile = "Dockerfile.codex"
        overlay = (
            f"FROM {image}\n"
            "ENV NPM_CONFIG_PREFIX=/home/codex/.npm-global\n"
            "USER codex\n"
            "RUN npm install -g @openai/codex@latest\n"
            "USER root\n"
            "RUN chown -R codex:codex /home/codex/.npm-global && "
            "chmod -R a+rwX /home/codex\n"
            f"LABEL io.cage.managed=\"true\" io.cage.role=\"codex\" io.cage.version=\"{cage_version}\"\n"
        )
    elif tool == "opencode":
        image = f"opencode:{cage_version}"
        registry = f"ghcr.io/sindycate/cage/opencode:{cage_version}"
        dockerfile = "Dockerfile.opencode"
        overlay = (
            f"FROM {image}\n"
            "ENV NPM_CONFIG_PREFIX=/home/opencode/.npm-global\n"
            "USER opencode\n"
            "RUN npm install -g --allow-scripts=opencode-ai opencode-ai@latest && "
            "opencode --help 2>&1 | grep -q -- '--pure' && "
            "OPENCODE_BINARY=/home/opencode/.npm-global/lib/node_modules/opencode-ai/bin/opencode.exe && "
            "test -x \"$OPENCODE_BINARY\" && "
            "grep -a -F -q 'OPENCODE_DISABLE_PROJECT_CONFIG' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q 'OPENCODE_DISABLE_EXTERNAL_SKILLS' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q '127.0.0.1:19876' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q 'O$=1455' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q '/auth/callback' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q '/.well-known/opencode' \"$OPENCODE_BINARY\"\n"
            "USER root\n"
            "RUN chown -R opencode:opencode /home/opencode/.npm-global && "
            "chmod -R a+rwX /home/opencode\n"
            f"LABEL io.cage.managed=\"true\" io.cage.role=\"opencode\" io.cage.version=\"{cage_version}\"\n"
        )
    else:
        print(
            f"Unknown tool: {tool} (use 'claude', 'codex', or 'opencode')",
            file=sys.stderr,
        )
        return 1
    docker = shutil.which("docker")
    if docker is None:
        print("docker command not found in PATH", file=sys.stderr)
        return 1
    try:
        config_path = config_root / "config.toml"
        policy = (
            config.storage_policy_from_config(config.load_config(config_path))
            if config_path.is_file()
            else storage.StoragePolicy()
        )
        storage.preflight(
            docker,
            policy,
            preferred_image=image,
            requires_build=True,
        )
    except (config.ConfigError, storage.StorageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    inspect = subprocess.run(
        [docker, "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspect.returncode != 0:
        print(f"Base image {image} not found locally.")
        pull = subprocess.run(
            [docker, "pull", registry],
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if pull.returncode == 0:
            if subprocess.run(
                [docker, "tag", registry, image], check=False
            ).returncode != 0:
                return 1
            print(f"Pulled {registry}")
        else:
            print("Pull failed or unavailable. Building locally...")
            base = f"cage-base:{cage_version}"
            if subprocess.run(
                [
                    docker,
                    "image",
                    "inspect",
                    base,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                print(f"Building shared base image {base}...")
                if subprocess.run(
                    [
                        docker,
                        "build",
                        "--build-arg",
                        f"CAGE_VERSION={cage_version}",
                        "-t",
                        base,
                        "-f",
                        str(install_root / "Dockerfile.base"),
                        str(install_root),
                    ],
                    check=False,
                ).returncode != 0:
                    return 1
            if subprocess.run(
                [
                    docker,
                    "build",
                    "--build-arg",
                    f"CAGE_BASE={base}",
                    "--build-arg",
                    f"CAGE_VERSION={cage_version}",
                    "-t",
                    image,
                    "-f",
                    str(install_root / dockerfile),
                    str(install_root),
                ],
                check=False,
            ).returncode != 0:
                return 1
    print(f"Updating {tool} in {image} (re-running installer only)...")
    result = subprocess.run(
        [
            docker,
            "build",
            "--no-cache",
            "-t",
            image,
            "-t",
            image.partition(":")[0] + ":latest",
            "-f",
            "-",
            str(install_root),
        ],
        input=overlay,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return result.returncode
    print(f"Done. {image} now has the latest {tool}.")
    return 0


def _delegate_public_desktop(
    prepared: PreparedLaunch,
    *,
    install_root: Path,
    config_root: Path,
) -> int:
    if sys.platform != "darwin":
        raise CliError(
            "desktop execution is currently supported only on macOS"
        )
    arguments = [
        "--config-dir",
        str(config_root),
        "--launcher",
        str(install_root / "cage"),
        "start",
        "--preset",
        prepared.plan.preset_name,
        "--net",
        prepared.plan.network,
        "--yolo" if prepared.plan.yolo else "--no-yolo",
    ]
    if prepared.plan.rebuild:
        arguments.append("--rebuild")
    if prepared.plan.no_open:
        arguments.append("--no-open")
    arguments.append(prepared.plan.repository)
    docker = storage.docker_command()
    storage.preflight(
        docker,
        prepared.plan.storage_policy,
        preferred_image=prepared.plan.image,
        requires_build=prepared.plan.rebuild,
    )
    environment = os.environ.copy()
    environment["CAGE_STORAGE_PREFLIGHT_DONE"] = "1"
    _exec_python(
        install_root / "cage-desktop.py",
        arguments,
        environment,
    )
    return 127


def main(
    argv: list[str] | None = None,
    *,
    cage_version: str | None = None,
    install_root: Path | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    version = cage_version or os.environ.get("CAGE_INTERNAL_VERSION", "")
    root = (install_root or Path(__file__).resolve().parents[1]).resolve()
    config_root = Path(
        os.environ.get(
            "CAGE_CONFIG_DIR",
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "cage",
        )
    ).expanduser()
    if arguments and arguments[0] in {"--version", "-V"}:
        print(f"cage {version}")
        return 0
    if arguments and arguments[0] in {"--help", "-h"}:
        print(USAGE)
        return 0
    resolve_only = bool(arguments and arguments[0] == "resolve-json")
    if resolve_only:
        arguments = arguments[1:]
    if _dispatch_management(
        arguments,
        install_root=root,
        config_root=config_root,
        cage_version=version,
    ):
        return 0
    try:
        request = parse_launch_request(arguments)
    except CliError as exc:
        if str(exc) == "__help__":
            print(USAGE)
            return 0
        print(str(exc), file=sys.stderr)
        return 1
    try:
        repository = Path(request.repo_operand).resolve(strict=True)
    except OSError as exc:
        print(f"ERROR: cannot resolve repository: {exc}", file=sys.stderr)
        return 1
    try:
        if request.interactive:
            request = _run_tui(
                request,
                install_root=root,
                config_path=config_root / "config.toml",
                repository=repository,
            )
        resolved = _resolve(
            request,
            config_path=config_root / "config.toml",
            repository=repository,
        )
        if resolved.tool == "codex":
            config.reject_unsafe_codex_passthrough_args(
                list(request.tool_arguments)
            )
        elif resolved.tool == "opencode":
            try:
                opencode_policy.reject_unsafe_passthrough_args(
                    list(request.tool_arguments),
                    selected_mcp_names={
                        str(server["name"])
                        for server in (*resolved.stdio_mcp, *resolved.remote_mcp)
                    },
                )
            except opencode_policy.PolicyError as exc:
                raise config.ConfigError(str(exc)) from exc
        prepared = build_launch_plan(
            request,
            resolved,
            cage_version=version,
            config_root=config_root,
            install_root=root,
        )
    except (CliError, config.ConfigError, PlanError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if request.tui_result:
            Path(request.tui_result).unlink(missing_ok=True)
    if resolve_only:
        print(
            json.dumps(
                prepared.plan.public_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    try:
        if prepared.plan.target == "host":
            return run_host_target(prepared)
        if (
            prepared.plan.target == "desktop"
            and os.environ.get("CAGE_DESKTOP_INTERNAL") != "1"
        ):
            return _delegate_public_desktop(
                prepared,
                install_root=root,
                config_root=config_root,
            )
        from .targets.container import run_container_target

        return run_container_target(
            prepared,
            install_root=root,
            config_root=config_root,
        )
    except (
        CliError,
        HostTargetError,
        config.ConfigError,
        storage.StorageError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
