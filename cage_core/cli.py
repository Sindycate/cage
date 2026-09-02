"""Python host launcher CLI."""

from __future__ import annotations

import json
import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import config, monitor, opencode_policy, storage
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
  mcp login --auth AUTH NAME  Authenticate a Codex OAuth MCP for one auth directory
  mcp logout --auth AUTH NAME Remove Codex OAuth MCP authentication for one auth directory
  mcp login NAME PATH         Authenticate with a selected OAuth MCP server
  mcp logout NAME PATH        Remove selected OAuth MCP authentication
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
  storage maintain           Preview automatic safe image maintenance
  storage maintain --apply   Apply exact managed/ephemeral image maintenance
  monitor connect URL         Connect the optional host-owned Token Monitor hub
  monitor disconnect          Remove the local hub credential and pause uploads
  monitor status [--json]     Show Cage devices, projects, cost, and migration state
  monitor sync [PATH]         Scan and split all registered Codex volumes
  monitor split --dry-run     Preview provider totals without hub changes
  monitor discover [--json]   List all existing Cage Codex state volumes
  monitor add PATH            Explicitly adopt/register a Codex volume
  monitor add --volume NAME   Adopt an exact dormant or recovered volume
  monitor migrate --yes       Replace legacy hub devices safely
  monitor pricing ...         Manage private custom model prices
  monitor forget DEVICE_ID    Delete a Cage-owned hub device

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
  cage mcp login --auth codex-work dash0
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
    if command == "monitor":
        raise SystemExit(
            _run_monitor(
                rest,
                config_root=config_root,
                install_root=install_root,
                cage_version=cage_version,
            )
        )
    return False


def _run_storage(arguments: list[str], *, config_root: Path) -> int:
    if not arguments or arguments[0] not in {"status", "clean", "maintain"}:
        print(
            "Usage: cage storage status|clean\n"
            "       cage storage maintain [--apply|--dry-run]",
            file=sys.stderr,
        )
        return 1
    action = arguments[0]
    apply = False
    if action in {"status", "clean"} and len(arguments) != 1:
        print(
            "Usage: cage storage status|clean\n"
            "       cage storage maintain [--apply|--dry-run]",
            file=sys.stderr,
        )
        return 1
    if action == "maintain":
        if len(arguments) == 2 and arguments[1] == "--apply":
            apply = True
        elif len(arguments) == 2 and arguments[1] in {"--dry-run", "--preview"}:
            pass
        elif len(arguments) != 1:
            print(
                "Usage: cage storage status|clean\n"
                "       cage storage maintain [--apply|--dry-run]",
                file=sys.stderr,
            )
            return 1
    try:
        config_path = config_root / "config.toml"
        policy = (
            config.storage_policy_from_config(config.load_config(config_path))
            if config_path.is_file()
            else storage.StoragePolicy()
        )
        return storage.run_storage_command(action, policy, apply=apply)
    except (config.ConfigError, storage.StorageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _monitor_secret_from_terminal(*, stdin_stream=None) -> str:
    """Read a hub secret without putting it in argv or ordinary output."""

    stream = stdin_stream or sys.stdin
    prompt = "Token Monitor hub secret: "
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        tty = None
    if tty is not None:
        try:
            try:
                return getpass.getpass(prompt, stream=tty).strip()
            except (OSError, ValueError):
                # Some IDE and sandbox terminals expose /dev/tty for reads but
                # reject writes.  Retry with a writable prompt stream while
                # letting getpass control echo on the terminal itself.
                pass
        finally:
            tty.close()
    try:
        stdin_is_tty = stream.isatty()
    except (AttributeError, OSError, ValueError):
        stdin_is_tty = False
    if not stdin_is_tty:
        raise CliError("monitor connect needs --secret-stdin when no TTY is available")
    try:
        return getpass.getpass(prompt, stream=sys.stderr).strip()
    except (OSError, ValueError) as exc:
        raise CliError(
            "interactive secret prompt is unavailable; use --secret-stdin"
        ) from exc


def _monitor_read_secret_stdin() -> str:
    if sys.stdin.isatty():
        raise CliError("--secret-stdin requires a noninteractive stdin")
    value = sys.stdin.readline(1024 * 1024).strip()
    if not value:
        raise CliError("Token Monitor hub secret cannot be empty")
    return value


def _monitor_target_and_preset(arguments: list[str]) -> tuple[str, str, str, bool]:
    """Parse the bounded project selector shared by monitor add/sync."""

    preset = ""
    target = ""
    path = ""
    json_output = False
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in {"--container", "--desktop"}:
            selected = item[2:]
            if target:
                raise CliError("monitor target options are mutually exclusive")
            target = selected
        elif item == "--preset":
            if index + 1 >= len(arguments) or not arguments[index + 1]:
                raise CliError("Missing value after --preset")
            preset = arguments[index + 1]
            index += 1
        elif item.startswith("--preset="):
            preset = item.partition("=")[2]
        elif item == "--json":
            json_output = True
        elif item.startswith("-"):
            raise CliError(f"Unknown monitor option: {item}")
        elif path:
            raise CliError("monitor accepts only one project path")
        else:
            path = item
        index += 1
    if preset and not config.NAME_RE.fullmatch(preset):
        raise CliError(f"Invalid preset name: {preset!r}")
    return path, preset, target, json_output


def _monitor_volume_arguments(arguments: list[str]) -> tuple[str, str, bool]:
    volume_name = ""
    display_name = ""
    json_output = False
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--volume":
            if index + 1 >= len(arguments) or not arguments[index + 1]:
                raise CliError("Missing value after --volume")
            volume_name = arguments[index + 1]
            index += 1
        elif item.startswith("--volume="):
            volume_name = item.partition("=")[2]
        elif item == "--display-name":
            if index + 1 >= len(arguments) or not arguments[index + 1]:
                raise CliError("Missing value after --display-name")
            display_name = arguments[index + 1]
            index += 1
        elif item.startswith("--display-name="):
            display_name = item.partition("=")[2]
        elif item == "--json":
            json_output = True
        else:
            raise CliError(f"Unknown monitor volume option: {item}")
        index += 1
    if not volume_name:
        raise CliError("monitor add --volume requires a volume name")
    return volume_name, display_name, json_output


def _resolve_monitor_registration(
    path: str,
    preset: str,
    target: str,
    *,
    config_root: Path,
    install_root: Path,
    cage_version: str,
) -> tuple[PreparedLaunch, str]:
    if not path:
        raise CliError("monitor project path is required")
    try:
        repository = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CliError(f"cannot resolve monitor repository: {exc}") from exc
    if not repository.is_dir():
        raise CliError(f"monitor repository is not a directory: {repository}")
    config_path = config_root / "config.toml"
    if not config_path.is_file():
        raise CliError(f"central config not found at {config_path}")
    data = config.load_config(config_path)
    resolved = config.resolve_config(
        data,
        config_path,
        str(repository),
        preset_name=preset,
        explicit_tool="codex",
        mcp_inventory=False,
    )
    request = LaunchRequest(
        repo_operand=str(repository),
        explicit_tool="codex",
        preset=preset,
        target=target,
    )
    prepared = build_launch_plan(
        request,
        resolved,
        cage_version=cage_version,
        config_root=config_root,
        install_root=install_root,
    )
    if prepared.plan.tool != "codex" or prepared.plan.target not in {"container", "desktop"}:
        raise CliError("Token Monitor supports only Codex container or Desktop targets")
    return prepared, str(repository)


def _monitor_status(config_root: Path, *, as_json: bool = False) -> int:
    connection = monitor.load_connection(config_root)
    registrations = monitor.load_registry(config_root)
    rows = [item.public_dict_for(config_root) for item in registrations]
    aggregate = monitor.load_aggregate_status(config_root)
    scheduler = monitor.load_scheduler_state(config_root)
    pending_upload = monitor.load_upload_state(config_root)
    hub: dict[str, object] | None = None
    hub_device_ids: set[str] = set()
    if connection is not None and connection.enabled:
        try:
            raw = monitor._hub_request(connection, "GET", "/api/stats")
            if isinstance(raw, dict):
                raw_devices = raw.get("devices")
                if isinstance(raw_devices, list):
                    hub_device_ids = {
                        item.get("deviceId")
                        for item in raw_devices
                        if isinstance(item, dict) and isinstance(item.get("deviceId"), str)
                    }
                hub = {
                    "device_count": len(raw.get("devices", []))
                    if isinstance(raw.get("devices"), list)
                    else None,
                    "updated_at": raw.get("updatedAt", ""),
                    "periods": raw.get("periods", {}),
                }
        except monitor.MonitorError as exc:
            hub = {"error": str(exc)}
    split_complete = bool(
        isinstance(aggregate, dict) and aggregate.get("split_complete") is True
    )
    split_pending = (
        not split_complete
        and monitor.host_device_id(config_root) in hub_device_ids
    )
    device_ids = (
        aggregate.get("device_ids")
        if isinstance(aggregate, dict) and isinstance(aggregate.get("device_ids"), list)
        else [monitor.host_device_id(config_root)]
    )
    providers = (
        aggregate.get("providers")
        if isinstance(aggregate, dict) and isinstance(aggregate.get("providers"), dict)
        else {}
    )
    payload = {
        "connected": bool(connection is not None and connection.enabled),
        "hub_url": connection.hub_url if connection is not None else "",
        "interval_seconds": connection.interval_seconds if connection is not None else None,
        "device_id": monitor.host_device_id(config_root),
        "device_ids": device_ids,
        "providers": providers,
        "split_migration_pending": split_pending,
        "projects": rows,
        "migration_pending": sum(bool(item.legacy_device_id) for item in registrations),
        "aggregate": aggregate,
        "hub": hub,
        "scheduler": {
            "next_full_reconciliation_at": scheduler["next_full_reconciliation_at"],
            "last_full_reconciliation_at": scheduler["last_full_reconciliation_at"],
            "last_generation": scheduler["last_generation"],
            "last_error": scheduler["last_error"],
            "full_reconciliation_in_progress": scheduler["full_reconciliation_in_progress"]
            is not None,
            "full_reconciliation_interval_seconds": monitor.FULL_RECONCILIATION_INTERVAL_SECONDS,
        },
        "upload_repair": (
            {
                "state": pending_upload["state"],
                "generation": pending_upload["generation"],
                "attempted": pending_upload["attempted"],
                "last_error": pending_upload["last_error"],
            }
            if pending_upload is not None
            else None
        ),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    if connection is None or not connection.enabled:
        print("Token Monitor: disconnected")
    else:
        print(
            f"Token Monitor: connected to {connection.hub_url} "
            f"(current volume every {connection.interval_seconds}s; "
            f"host-wide full reconciliation every "
            f"{monitor.FULL_RECONCILIATION_INTERVAL_SECONDS}s)"
        )
        if hub and "error" in hub:
            print(f"Hub: unavailable ({hub['error']})")
        elif hub:
            print(f"Hub devices: {hub.get('device_count', '?')}")
    if len(device_ids) == 1:
        print(f"Cage device: {device_ids[0]}")
    else:
        print(f"Cage devices: {len(device_ids)}")
        for device_id in device_ids:
            print(f"  {device_id}")
    if isinstance(providers, dict) and providers:
        print(f"Provider streams: {len(providers)}")
        for provider, provider_status in sorted(providers.items()):
            if not isinstance(provider_status, dict):
                continue
            print(
                f"  {provider_status.get('provider_label', provider)}: "
                f"${provider_status.get('cost_usd', 0):.6f} for "
                f"{provider_status.get('total_tokens', 0):,} tokens"
            )
    if not rows:
        print("Registered Cage projects: none")
    else:
        print(f"Registered Cage projects: {len(rows)}")
        for row in rows:
            print(
                f"  {row['project_id']}  {row['status']}  {row['display_name']}"
                f"  last={row['last_success_at'] or 'never'}"
            )
    if aggregate:
        print(
            f"Estimated cost: ${aggregate.get('cost_usd', 0):.6f} for "
            f"{aggregate.get('total_tokens', 0):,} tokens"
        )
        print(
            f"Price coverage: {aggregate.get('price_coverage_percent', 0):.2f}% "
            f"({aggregate.get('unpriced_tokens', 0):,} unpriced tokens)"
        )
        missing = aggregate.get("missing_prices") or aggregate.get("missing_models")
        if isinstance(missing, list) and missing:
            print("Missing prices: " + ", ".join(str(item) for item in missing))
        print(f"Deduplicated session copies: {aggregate.get('duplicate_sessions', 0)}")
    pending = sum(bool(item.legacy_device_id) for item in registrations)
    if pending:
        print(
            f"Legacy migration: {pending} old hub device(s) remain; "
            "run cage monitor migrate --yes"
        )
    if split_pending:
        print("Provider split migration: pending; run cage monitor migrate --yes")
    if pending_upload is not None:
        print(
            "Provider upload repair: pending for "
            f"{len(pending_upload['attempted'])} attempted stream(s); "
            "the next successful sync will repair the prepared generation"
        )
    if scheduler["last_error"]:
        print(f"Automatic full reconciliation: retry pending ({scheduler['last_error']})")
    return 0


def _run_monitor_pricing(arguments: list[str], *, config_root: Path) -> int:
    if not arguments:
        raise CliError("Usage: cage monitor pricing status|set|remove ...")
    action, rest = arguments[0], arguments[1:]
    if action == "status":
        if rest not in ([], ["--json"]):
            raise CliError("monitor pricing status accepts only --json")
        models = monitor.load_pricing(config_root)
        if rest == ["--json"]:
            print(json.dumps({"models": models}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        elif not models:
            print("Custom Token Monitor prices: none")
        else:
            print(f"Custom Token Monitor prices: {len(models)}")
            for model, rates in sorted(models.items()):
                rendered = ", ".join(f"{key}={value:g}" for key, value in sorted(rates.items()))
                print(f"  {model}  {rendered}")
        return 0
    if action == "remove":
        if len(rest) != 1:
            raise CliError("monitor pricing remove requires one model ID")
        if not monitor.remove_model_pricing(config_root, rest[0]):
            raise CliError("custom price was not found")
        print(f"Removed custom price for {rest[0]}")
        return 0
    if action != "set" or not rest:
        raise CliError(
            "Usage: cage monitor pricing set [PROVIDER:]MODEL "
            "--input N --output N [--cache-read N]"
        )
    model_id = rest.pop(0)
    values: dict[str, float | None] = {"input": None, "output": None, "cache_read": None}
    index = 0
    while index < len(rest):
        option = rest[index]
        names = {"--input": "input", "--output": "output", "--cache-read": "cache_read"}
        if option not in names or index + 1 >= len(rest):
            raise CliError(f"invalid monitor pricing option: {option}")
        try:
            values[names[option]] = float(rest[index + 1])
        except ValueError as exc:
            raise CliError(f"invalid price for {option}") from exc
        index += 2
    monitor.set_model_pricing(
        config_root,
        model_id,
        input_per_million=values["input"],
        output_per_million=values["output"],
        cache_read_per_million=values["cache_read"],
    )
    print(f"Saved custom price for {model_id}; run cage monitor sync to recalculate cost.")
    return 0


def _run_monitor(
    arguments: list[str],
    *,
    config_root: Path,
    install_root: Path,
    cage_version: str,
) -> int:
    if not arguments:
        print(
            "Usage: cage monitor connect|disconnect|status|sync|discover|add|migrate|pricing|forget ...",
            file=sys.stderr,
        )
        return 1
    action = arguments[0]
    rest = arguments[1:]
    try:
        if action == "pricing":
            return _run_monitor_pricing(rest, config_root=config_root)
        if action == "connect":
            if not rest:
                raise CliError("monitor connect requires a hub URL")
            url = rest.pop(0)
            interval = 300
            secret_stdin = False
            index = 0
            while index < len(rest):
                item = rest[index]
                if item == "--secret-stdin":
                    secret_stdin = True
                elif item == "--interval-seconds":
                    if index + 1 >= len(rest):
                        raise CliError("Missing value after --interval-seconds")
                    interval = int(rest[index + 1])
                    index += 1
                elif item.startswith("--interval-seconds="):
                    interval = int(item.partition("=")[2])
                else:
                    raise CliError(f"Unknown monitor connect option: {item}")
                index += 1
            secret = _monitor_read_secret_stdin() if secret_stdin else _monitor_secret_from_terminal()
            connection = monitor.MonitorConnection(
                hub_url=monitor.normalize_hub_url(url),
                secret=secret,
                interval_seconds=monitor.validate_interval(interval),
            )
            monitor.verify_connection(connection)
            monitor.save_connection(config_root, connection)
            print(f"Token Monitor connected to {connection.hub_url}")
            return 0
        if action == "disconnect":
            if rest:
                raise CliError("monitor disconnect accepts no options")
            monitor.disable_connection(config_root)
            print("Token Monitor disconnected; the Cage device and project totals were preserved.")
            return 0
        if action == "status":
            if rest not in ([], ["--json"]):
                raise CliError("monitor status accepts only --json")
            return _monitor_status(config_root, as_json=rest == ["--json"])
        if action == "discover":
            if rest not in ([], ["--json"]):
                raise CliError("monitor discover accepts only --json")
            discovered = monitor.discover_codex_volumes(
                storage.docker_command(), config_root
            )
            if rest == ["--json"]:
                print(json.dumps({"volumes": discovered}, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            elif not discovered:
                print("Cage Codex state volumes: none")
            else:
                print(f"Cage Codex state volumes: {len(discovered)}")
                for item in discovered:
                    state = "registered" if item["registered"] else "unregistered"
                    label = item.get("display_name") or "Recovered candidate"
                    print(f"  {item['volume_name']}  {state}  {label}")
            return 0
        if action == "migrate":
            if rest != ["--yes"]:
                raise CliError("monitor migrate requires --yes")
            docker = storage.docker_command()
            config_path = config_root / "config.toml"
            policy = (
                config.storage_policy_from_config(config.load_config(config_path))
                if config_path.is_file()
                else storage.StoragePolicy()
            )
            count = monitor.migrate_legacy_devices(
                config_root,
                docker,
                install_root,
                version=cage_version,
                storage_policy=policy,
            )
            print(f"Token Monitor migration complete; removed {count} legacy hub device(s).")
            return 0
        if action not in {"sync", "add", "split"}:
            if action == "forget":
                if not rest:
                    raise CliError("monitor forget requires a device ID")
                device_id = rest.pop(0)
                assume_yes = False
                for item in rest:
                    if item == "--yes":
                        assume_yes = True
                    else:
                        raise CliError(f"Unknown monitor forget option: {item}")
                connection = monitor.load_connection(config_root)
                if connection is None or not connection.enabled:
                    raise CliError("Token Monitor is disconnected")
                registrations = monitor.load_registry(config_root)
                host_id = monitor.host_device_id(config_root)
                split_state = monitor.load_split_status(config_root)
                legacy_record = next(
                    (item for item in registrations if item.legacy_device_id == device_id),
                    None,
                )
                is_host_device = device_id == host_id and bool(registrations)
                is_provider_device = bool(
                    split_state
                    and device_id in split_state.get("device_ids", [])
                )
                if not is_host_device and legacy_record is None and not is_provider_device:
                    raise CliError("monitor device was not found")
                if not assume_yes:
                    if not sys.stdin.isatty():
                        raise CliError("monitor forget requires --yes in a noninteractive shell")
                    print(f"Type FORGET to delete {device_id} from the Token Monitor hub: ", end="", flush=True)
                    if sys.stdin.readline().strip() != "FORGET":
                        raise CliError("forget aborted")
                if is_host_device:
                    monitor.disable_all_registrations(config_root)
                elif legacy_record is not None:
                    monitor.retire_registration(config_root, legacy_record.logical_id, disabled=True)
                try:
                    monitor.delete_device(connection, device_id)
                except monitor.MonitorError as exc:
                    raise CliError(
                        "Token Monitor hub deletion failed; the local registration "
                        f"remains disabled: {exc}"
                    ) from exc
                if is_host_device:
                    for record in registrations:
                        monitor.remove_project_state(config_root, record.logical_id)
                    monitor.remove_aggregate_status(config_root)
                else:
                    if is_provider_device:
                        monitor.remove_device_state(config_root, device_id)
                        assert split_state is not None
                        remaining = [
                            item for item in split_state.get("device_ids", [])
                            if item != device_id
                        ]
                        monitor.save_split_status(
                            config_root,
                            {
                                **split_state,
                                "device_ids": remaining,
                                "updated_at": monitor._now(),
                            },
                        )
                    else:
                        assert legacy_record is not None
                        monitor.remove_device_state(config_root, device_id)
                        monitor.clear_legacy_device_id(
                            config_root, legacy_record.logical_id, device_id
                        )
                if is_provider_device:
                    print(
                        f"Forgot Token Monitor provider device {device_id}; "
                        "a later sync may recreate it if matching sessions remain."
                    )
                elif is_host_device:
                    print(
                        f"Forgot Token Monitor device {device_id}; "
                        "disabled projects require explicit add."
                    )
                else:
                    print(f"Forgot legacy Token Monitor device {device_id}.")
                return 0
            raise CliError(f"unknown monitor action: {action}")

        docker = storage.docker_command()
        config_path = config_root / "config.toml"
        policy = (
            config.storage_policy_from_config(config.load_config(config_path))
            if config_path.is_file()
            else storage.StoragePolicy()
        )
        if action == "split":
            if any(item not in {"--dry-run", "--json"} for item in rest) or "--dry-run" not in rest:
                raise CliError("monitor split requires --dry-run; optional output flag is --json")
            manifest = monitor.preview_provider_split(
                config_root,
                docker,
                install_root,
                version=cage_version,
                storage_policy=policy,
                allow_build=True,
            )
            if "--json" in rest:
                print(json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            else:
                print(
                    f"Dry-run provider split: {manifest.get('total_tokens', 0):,} tokens, "
                    f"${manifest.get('cost_usd', 0):.6f}"
                )
                for provider, provider_status in sorted(manifest.get("providers", {}).items()):
                    print(
                        f"  {provider_status.get('provider_label', provider)}: "
                        f"{provider_status.get('total_tokens', 0):,} tokens, "
                        f"${provider_status.get('cost_usd', 0):.6f}, "
                        f"device={provider_status.get('device_id', '?')}"
                    )
                if manifest.get("missing_prices"):
                    print("Missing prices: " + ", ".join(manifest["missing_prices"]))
            return 0
        if action == "add" and any(
            item == "--volume" or item.startswith("--volume=") for item in rest
        ):
            volume_name, display_name, json_output = _monitor_volume_arguments(rest)
            connection = monitor.load_connection(config_root)
            if connection is None or not connection.enabled:
                raise CliError("registration requires an active Token Monitor connection")
            record = monitor.register_recovered_volume(
                config_root,
                docker,
                volume_name=volume_name,
                display_name=display_name,
            )
            updated, _ = monitor.scan_registration(
                config_root,
                docker,
                install_root,
                record,
                version=cage_version,
                storage_policy=policy,
                allow_build=True,
                force=True,
            )
            if json_output:
                print(json.dumps(updated.public_dict_for(config_root), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            else:
                print(
                    f"Registered recovered volume {record.volume_name} as "
                    f"{monitor.project_id_for(config_root, record.logical_id)}"
                )
                print(f"Synchronized provider streams at {updated.last_success_at}")
            return 0

        path, preset, target, _ = _monitor_target_and_preset(rest)
        if action == "add" and not path:
            raise CliError("monitor add requires a project path")
        if path:
            prepared, repository = _resolve_monitor_registration(
                path,
                preset,
                target,
                config_root=config_root,
                install_root=install_root,
                cage_version=cage_version,
            )
            logical_id = monitor.logical_target_id(
                repository, prepared.plan.target, prepared.plan.preset_name
            )
            fingerprint = monitor.ensure_codex_volume(
                docker,
                prepared.plan.volume_name,
                logical_id=logical_id,
            )
            display_kind = "Desktop" if prepared.plan.target == "desktop" else "Container"
            record = monitor.register_volume(
                config_root,
                docker,
                volume_name=prepared.plan.volume_name,
                repository=repository,
                target=prepared.plan.target,
                preset=prepared.plan.preset_name,
                display_name=f"Cage: {Path(repository).name} ({display_kind})",
                fingerprint=fingerprint,
                allow_replacement=action == "add",
            )
            print(
                f"Registered {monitor.project_id_for(config_root, record.logical_id)} "
                f"for {record.display_name}"
            )
            connection = monitor.load_connection(config_root)
            if connection is None or not connection.enabled:
                raise CliError("registration saved, but Token Monitor is disconnected")
            updated, _ = monitor.scan_registration(
                config_root,
                docker,
                install_root,
                record,
                version=cage_version,
                storage_policy=policy,
                allow_build=True,
                force=True,
            )
            aggregate = monitor.load_aggregate_status(config_root) or {}
            device_count = len(aggregate.get("device_ids", [])) if isinstance(aggregate.get("device_ids"), list) else 1
            print(f"Synchronized {device_count} provider device(s) at {updated.last_success_at}")
            return 0

        if action == "add":
            raise CliError("monitor add requires a project path")
        connection = monitor.load_connection(config_root)
        if connection is None or not connection.enabled:
            raise CliError("Token Monitor is disconnected")
        registrations = monitor.load_registry(config_root)
        active = [item for item in registrations if item.status == "active"]
        if not active:
            print("No active Token Monitor projects.")
            return 0
        updated, _ = monitor.scan_all_registrations(
            config_root,
            docker,
            install_root,
            version=cage_version,
            storage_policy=policy,
            allow_build=True,
            force=True,
        )
        aggregate = monitor.load_aggregate_status(config_root) or {}
        device_count = len(aggregate.get("device_ids", [])) if isinstance(aggregate.get("device_ids"), list) else 1
        print(
            f"Synchronized {device_count} provider device(s) with "
            f"{len(updated)} project(s) at {updated[0].last_success_at}"
        )
        return 0
    except (CliError, config.ConfigError, PlanError, monitor.MonitorError, storage.StorageError, ValueError, OSError) as exc:
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
            "RUN curl -fsSL https://claude.ai/install.sh | bash && "
            "chmod -R a+rwX /home/claude/.local\n"
            "USER root\n"
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
            "RUN npm install -g @openai/codex@latest && "
            "chmod -R a+rwX /home/codex/.npm-global\n"
            "USER root\n"
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
            "grep -a -E -q '[[:alpha:]_$][[:alnum:]_$]*=1455([,;]|$)' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q '/auth/callback' \"$OPENCODE_BINARY\" && "
            "grep -a -F -q '/.well-known/opencode' \"$OPENCODE_BINARY\" && "
            "chmod -R a+rwX /home/opencode/.npm-global\n"
            "USER root\n"
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
