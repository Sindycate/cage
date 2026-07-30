"""Deterministic launch-resource ownership and reverse-order cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


Cleanup = Callable[[], int | None]


@dataclass(frozen=True)
class Resource:
    name: str
    cleanup: Cleanup


class LifecycleCoordinator:
    """Own every created resource until cleanup or an intentional exec."""

    def __init__(self) -> None:
        self._resources: list[Resource] = []
        self._lock = threading.Lock()
        self._closed = False

    def register(self, name: str, cleanup: Cleanup) -> Resource:
        resource = Resource(name=name, cleanup=cleanup)
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot register a resource after cleanup")
            self._resources.append(resource)
        return resource

    def release(self, resource: Resource) -> None:
        with self._lock:
            if resource in self._resources:
                self._resources.remove(resource)

    def cleanup(self, primary_status: int = 0) -> int:
        """Clean up in reverse order and preserve primary failure precedence."""

        with self._lock:
            if self._closed:
                return primary_status
            self._closed = True
            resources = list(reversed(self._resources))
            self._resources.clear()
        cleanup_status = 0
        for resource in resources:
            try:
                status = resource.cleanup()
            except Exception:
                status = 1
            if cleanup_status == 0 and status:
                cleanup_status = int(status)
        return primary_status if primary_status != 0 else cleanup_status

    @property
    def requires_supervision(self) -> bool:
        with self._lock:
            return bool(self._resources)


def terminate_process(
    process: subprocess.Popen[object],
    *,
    grace_seconds: float = 2.0,
    process_group: bool = True,
) -> None:
    """TERM, wait a bounded grace period, then KILL."""

    if process.poll() is not None:
        return
    try:
        if process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def wait_for_line(
    path: Path,
    expected: str,
    process: subprocess.Popen[object],
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> bool:
    """Wait until a readiness line appears or the process exits."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if any(
                line.rstrip("\r\n") == expected
                for line in path.read_text(encoding="utf-8").splitlines()
            ):
                return True
        except (FileNotFoundError, OSError, UnicodeError):
            pass
        if process.poll() is not None:
            return False
        time.sleep(interval_seconds)
    return False
