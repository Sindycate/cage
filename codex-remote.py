#!/usr/bin/env python3
"""Remote Codex launcher and Cage supervisor heartbeat watchdog."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import tomllib


REAL_CODEX = "/home/codex/.npm-global/bin/codex"
CODEX_HOME = "/home/codex/.codex"
ENV_PATH = Path("/run/cage-user/remote-env.json")
LAUNCH_PATH = Path("/run/cage/remote-launch.json")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_INVENTORY_BYTES = 4 * 1024 * 1024
INVENTORY_TIMEOUT = 60.0
INERT_STDIO_COMMAND = "/usr/bin/false"
INERT_HTTP_URL = "https://invalid.invalid/mcp"
SAFE_PASSTHROUGH_CONFIG_ROOTS = {
    "approval_policy",
    "model",
    "model_auto_compact_token_limit",
    "model_context_window",
    "model_provider",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "personality",
    "sandbox_mode",
    "sandbox_permissions",
    "sandbox_workspace_write",
    "shell_environment_policy",
    "web_search",
}
HEARTBEAT_TIMEOUT = 45.0
HEARTBEAT_POLL_INTERVAL = 2.0
SCHEDULER_GAP_GRACE = 10.0


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain an object")
    return value


def key_segment(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name)


def disable_override(name: str) -> str:
    if not name or "\n" in name or "\r" in name:
        raise RuntimeError(f"unsafe MCP server name: {name!r}")
    return f"mcp_servers.{key_segment(name)}.enabled=false"


def toml_transports(path: Path) -> dict[str, str] | None:
    """Enabled MCP names and command/url transport kinds, or None."""
    if not path.exists():
        return {}
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INVENTORY_BYTES + 1)
        if len(raw) > MAX_INVENTORY_BYTES:
            return None
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return None
    transports: dict[str, str] = {}
    for name, conf in servers.items():
        if not isinstance(conf, dict):
            return None
        has_command = isinstance(conf.get("command"), str) and bool(conf["command"])
        has_url = isinstance(conf.get("url"), str) and bool(conf["url"])
        if has_command == has_url:
            return None
        if conf.get("enabled") is not False:
            transports[str(name)] = "stdio" if has_command else "http"
    return transports


def merge_transports(target: dict[str, str], incoming: dict[str, str]) -> None:
    for name, transport in incoming.items():
        previous = target.get(name)
        if previous is not None and previous != transport:
            raise RuntimeError(
                f"conflicting direct transport types for MCP server {name!r}"
            )
        target[name] = transport


def inventory_enabled(
    profile: str,
    work_dir: str,
    environment: dict,
) -> tuple[set[str], set[str], dict[str, str]]:
    """Inventory the live desktop runtime using the container Codex binary.

    Runs on every connection so a later project MCP definition (the repository
    is a live writable mount) is discovered and suppressed. Only names and the
    enabled flag are read. Fails closed on any untrustworthy result.
    """
    command = [REAL_CODEX]
    if profile:
        command += ["--profile", profile]
    command += ["mcp", "list", "--json"]
    env = dict(environment)
    env["CODEX_HOME"] = CODEX_HOME
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=INVENTORY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot inventory MCP servers: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "cannot build a trustworthy MCP inventory: codex mcp list exited "
            f"{completed.returncode}"
        )
    try:
        output = completed.stdout or b""
        if len(output) > MAX_INVENTORY_BYTES:
            raise RuntimeError(
                "cannot build a trustworthy MCP inventory: output too large"
            )
        entries = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot build a trustworthy MCP inventory: {exc}") from exc
    if not isinstance(entries, list):
        raise RuntimeError("cannot build a trustworthy MCP inventory: expected a JSON list")
    runtime_enabled: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(
                "cannot build a trustworthy MCP inventory: every entry "
                "must be a JSON object"
            )
        name = entry.get("name")
        enabled = entry.get("enabled")
        if not isinstance(name, str) or not name or not isinstance(enabled, bool):
            raise RuntimeError(
                "cannot build a trustworthy MCP inventory: every entry must "
                "contain a non-empty string name and boolean enabled flag"
            )
        if enabled:
            runtime_enabled.add(name)
    direct_transports: dict[str, str] = {}
    if profile:
        profile_transports = toml_transports(
            Path(CODEX_HOME) / f"{profile}.config.toml"
        )
        if profile_transports is None:
            raise RuntimeError(
                "cannot build a trustworthy MCP inventory: unreadable profile layer"
            )
        merge_transports(direct_transports, profile_transports)
    project_transports = toml_transports(
        Path(work_dir) / ".codex" / "config.toml"
    )
    if project_transports is None:
        raise RuntimeError(
            "cannot build a trustworthy MCP inventory: unreadable project layer"
        )
    merge_transports(direct_transports, project_transports)
    enabled = runtime_enabled | set(direct_transports)
    return enabled, runtime_enabled, direct_transports


def config_override_root(expression: str) -> str | None:
    quote = ""
    escaped = False
    assignment = -1
    for index, character in enumerate(expression):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = ""
            continue
        if quote == "'":
            if character == "'":
                quote = ""
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "=":
            assignment = index
            break
    if assignment < 0:
        return None
    key = expression[:assignment].strip()
    try:
        parsed = tomllib.loads(f"{key}=0")
    except tomllib.TOMLDecodeError:
        return None
    if len(parsed) != 1:
        return None
    return next(iter(parsed))


def reject_unsafe_codex_passthrough_args(argv: list[str]) -> None:
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            break
        if (
            argument in {"-p", "--profile"}
            or argument.startswith("--profile=")
            or argument.startswith("-p=")
            or (argument.startswith("-p") and len(argument) > 2)
        ):
            raise RuntimeError(
                "Codex launch arguments may not override the profile; select "
                "codex_profile in the Cage preset"
            )
        if (
            argument in {"-C", "--cd"}
            or argument.startswith("--cd=")
            or argument.startswith("-C=")
            or (argument.startswith("-C") and len(argument) > 2)
        ):
            raise RuntimeError(
                "Codex launch arguments may not override the working directory; "
                "the Cage repository path is authoritative"
            )
        if (
            argument in {"--enable", "--disable"}
            or argument.startswith("--enable=")
            or argument.startswith("--disable=")
        ):
            raise RuntimeError(
                "Codex launch arguments may not change feature flags because "
                "features can change MCP/plugin discovery"
            )
        if (
            argument in {"--remote", "--remote-auth-token-env"}
            or argument.startswith("--remote=")
            or argument.startswith("--remote-auth-token-env=")
        ):
            raise RuntimeError(
                "Codex launch arguments may not select a remote app-server "
                "runtime because Cage did not inventory that runtime"
            )
        if argument == "--ignore-user-config":
            raise RuntimeError(
                "Codex launch arguments may not ignore user configuration after "
                "Cage inventories that layer"
            )
        expression: str | None = None
        if argument in {"-c", "--config"}:
            if index + 1 < len(argv):
                expression = argv[index + 1]
                index += 1
        elif argument.startswith("--config="):
            expression = argument[len("--config=") :]
        elif argument.startswith("-c="):
            expression = argument[3:]
        elif argument.startswith("-c") and len(argument) > 2:
            expression = argument[2:]
        if expression is not None:
            root = config_override_root(expression)
            if root == "mcp_servers":
                raise RuntimeError(
                    "Codex launch arguments may not override mcp_servers; define "
                    "the server in a central Cage mcp_pack and select that pack"
                )
            if root not in SAFE_PASSTHROUGH_CONFIG_ROOTS:
                raise RuntimeError(
                    f"Codex config override root {root!r} is not safe after MCP "
                    "inventory; move it to the selected Cage-owned Codex "
                    "configuration layer"
                )
        index += 1


def suppression_overrides(
    suppressed: list[str],
    runtime_enabled: set[str],
    direct_transports: dict[str, str],
) -> list[str]:
    overrides: list[str] = []
    for name in suppressed:
        if not name or "\n" in name or "\r" in name:
            raise RuntimeError(f"unsafe MCP server name: {name!r}")
        key = f"mcp_servers.{key_segment(name)}"
        if name not in runtime_enabled:
            transport = direct_transports.get(name)
            if transport == "stdio":
                overrides.append(f"{key}.command={json.dumps(INERT_STDIO_COMMAND)}")
            elif transport == "http":
                overrides.append(f"{key}.url={json.dumps(INERT_HTTP_URL)}")
            else:
                raise RuntimeError(
                    f"no loaded or direct transport for MCP server {name!r}"
                )
        overrides.append(f"{key}.enabled=false")
    return overrides


def launch() -> None:
    values = load_object(ENV_PATH)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"HOME", "LANG", "LOGNAME", "PATH", "SHELL", "TERM", "USER"}
        or name.startswith("LC_")
    }
    for name, value in values.items():
        if not isinstance(name, str) or not ENV_RE.fullmatch(name):
            raise RuntimeError("invalid remote environment name")
        if not isinstance(value, str):
            raise RuntimeError(f"invalid remote environment value for {name}")
        environment[name] = value

    launch_config = load_object(LAUNCH_PATH)
    profile = launch_config.get("profile", "")
    yolo = launch_config.get("yolo", False)
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", profile):
        raise RuntimeError("invalid remote Codex profile")
    if not isinstance(yolo, bool):
        raise RuntimeError("invalid remote yolo setting")
    selected_mcp = launch_config.get("selected_mcp", [])
    if not isinstance(selected_mcp, list) or any(
        not isinstance(name, str) or not name for name in selected_mcp
    ):
        raise RuntimeError("invalid remote selected MCP names")
    work_dir = launch_config.get("work_dir", "")
    if not isinstance(work_dir, str) or not work_dir:
        raise RuntimeError("invalid remote work directory")

    passthrough = sys.argv[1:]
    reject_unsafe_codex_passthrough_args(passthrough)

    # Authoritative MCP selection: re-inventory the live runtime on every
    # connection and disable every inherited server the preset did not select.
    enabled, runtime_enabled, direct_transports = inventory_enabled(
        profile,
        work_dir,
        environment,
    )
    duplicates = sorted(set(selected_mcp) & set(direct_transports))
    if duplicates:
        raise RuntimeError(
            "selected MCP server(s) already exist in a profile/project layer: "
            + " ".join(json.dumps(name, ensure_ascii=True) for name in duplicates)
        )
    suppressed = sorted(enabled - set(selected_mcp))
    sys.stderr.write("cage: MCP policy: selected packs only\n")
    if suppressed:
        sys.stderr.write(
            "cage: inherited MCPs suppressed for this connection: %s\n"
            % " ".join(json.dumps(name, ensure_ascii=True) for name in suppressed)
        )

    arguments = [REAL_CODEX]
    if profile:
        arguments += ["--profile", profile]
    if yolo:
        arguments.append("--yolo")
    for override in suppression_overrides(
        suppressed,
        runtime_enabled,
        direct_transports,
    ):
        arguments += ["-c", override]
    arguments += passthrough
    os.execve(REAL_CODEX, arguments, environment)


def evaluate_heartbeat(
    marker: int | None,
    previous_marker: object,
    now: float,
    last_progress: float,
    last_check: float,
) -> tuple[object, float, bool]:
    """Track active-time heartbeat loss without treating host sleep as failure."""
    if now - last_check > SCHEDULER_GAP_GRACE or marker != previous_marker:
        last_progress = now
    return marker, last_progress, now - last_progress > HEARTBEAT_TIMEOUT


def wait_for_supervisor(heartbeat: Path) -> None:
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous_marker: object = object()
    last_progress = time.monotonic()
    last_check = last_progress
    while not stopping:
        now = time.monotonic()
        try:
            marker = heartbeat.stat().st_mtime_ns
        except FileNotFoundError:
            marker = None
        previous_marker, last_progress, expired = evaluate_heartbeat(
            marker,
            previous_marker,
            now,
            last_progress,
            last_check,
        )
        last_check = now
        if expired:
            print("cage: desktop supervisor heartbeat expired", file=sys.stderr)
            raise SystemExit(70)
        time.sleep(HEARTBEAT_POLL_INTERVAL)


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--cage-wait":
        wait_for_supervisor(Path(sys.argv[2]))
        return
    launch()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"cage: remote Codex launcher failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
