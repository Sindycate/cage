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

for _policy_root in (
    Path("/usr/local/lib/cage"),
    Path(__file__).resolve().parent,
):
    if (_policy_root / "cage_core" / "codex_policy.py").is_file():
        sys.path.insert(0, str(_policy_root))
        break

from cage_core import codex_policy, codex_runtime


REAL_CODEX = "/home/codex/.npm-global/bin/codex"
CODEX_HOME = "/home/codex/.codex"
ENV_PATH = Path("/run/cage-user/remote-env.json")
LAUNCH_PATH = Path("/run/cage/remote-launch.json")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_INVENTORY_BYTES = 4 * 1024 * 1024
INVENTORY_TIMEOUT = 60.0
HEARTBEAT_TIMEOUT = 45.0
HEARTBEAT_POLL_INTERVAL = 2.0
SCHEDULER_GAP_GRACE = 10.0


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain an object")
    return value


def key_segment(name: str) -> str:
    return codex_policy.key_segment(name)


def disable_override(name: str) -> str:
    if not name or "\n" in name or "\r" in name:
        raise RuntimeError(f"unsafe MCP server name: {name!r}")
    return f"mcp_servers.{key_segment(name)}.enabled=false"


def toml_transports(path: Path) -> dict[str, str] | None:
    """Enabled MCP names and command/url transport kinds, or None."""
    return codex_runtime.toml_transports(
        path, maximum_bytes=MAX_INVENTORY_BYTES
    )


def merge_transports(target: dict[str, str], incoming: dict[str, str]) -> None:
    try:
        codex_policy.merge_transports(target, incoming)
    except codex_policy.PolicyError as exc:
        raise RuntimeError(str(exc)) from exc


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
    try:
        return codex_runtime.inventory_enabled(
            codex_binary=REAL_CODEX,
            codex_home=Path(CODEX_HOME),
            repository=Path(work_dir),
            profile=profile,
            environment=environment,
            timeout=INVENTORY_TIMEOUT,
            maximum_bytes=MAX_INVENTORY_BYTES,
        )
    except codex_policy.PolicyError as exc:
        raise RuntimeError(str(exc)) from exc


def config_override_root(expression: str) -> str | None:
    return codex_policy.config_override_root(expression)


def reject_unsafe_codex_passthrough_args(argv: list[str]) -> None:
    try:
        codex_policy.reject_unsafe_passthrough_args(
            argv, allow_desktop_code_mode_host=True
        )
    except codex_policy.PolicyError as exc:
        raise RuntimeError(str(exc)) from exc


def suppression_overrides(
    suppressed: list[str],
    runtime_enabled: set[str],
    direct_transports: dict[str, str],
) -> list[str]:
    try:
        return codex_policy.suppression_overrides(
            suppressed, runtime_enabled, direct_transports
        )
    except codex_policy.PolicyError as exc:
        raise RuntimeError(str(exc)) from exc


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
