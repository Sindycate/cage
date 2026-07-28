#!/usr/bin/env python3
"""Remote Codex launcher and Cage supervisor heartbeat watchdog."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sys
import time


REAL_CODEX = "/home/codex/.npm-global/bin/codex"
ENV_PATH = Path("/run/cage-user/remote-env.json")
LAUNCH_PATH = Path("/run/cage-user/remote-launch.json")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEARTBEAT_TIMEOUT = 45.0
HEARTBEAT_POLL_INTERVAL = 2.0
SCHEDULER_GAP_GRACE = 10.0


def load_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain an object")
    return value


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

    arguments = [REAL_CODEX]
    if profile:
        arguments += ["--profile", profile]
    if yolo:
        arguments.append("--yolo")
    arguments += sys.argv[1:]
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
