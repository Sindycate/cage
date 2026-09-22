"""Coordinator leases and persisted full-reconciliation scheduling."""

from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path
import secrets
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import constants as constants_api
from . import errors as errors_api
from . import publication as publication_api
from . import state as state_api


def _scheduler_state_path(config_root: Path) -> Path:
    return state_api.monitor_root(config_root) / constants_api.SCHEDULER_STATE_FILE


def _default_scheduler_state() -> dict[str, Any]:
    return {
        "version": constants_api.SCHEDULER_STATE_VERSION,
        "next_full_reconciliation_at": 0.0,
        "last_full_reconciliation_at": "",
        "last_generation": "",
        "last_error": "",
        "updated_at": "",
        "full_reconciliation_in_progress": None,
    }


def _validate_scheduler_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "next_full_reconciliation_at",
        "last_full_reconciliation_at",
        "last_generation",
        "last_error",
        "updated_at",
        "full_reconciliation_in_progress",
    }:
        raise errors_api.MonitorError("monitor scheduler state has an invalid shape")
    if value["version"] != constants_api.SCHEDULER_STATE_VERSION:
        raise errors_api.MonitorError("monitor scheduler state has an invalid version")
    next_full = value["next_full_reconciliation_at"]
    if type(next_full) not in (int, float) or not math.isfinite(next_full) or next_full < 0:
        raise errors_api.MonitorError("monitor scheduler next reconciliation time is invalid")
    for key in ("last_full_reconciliation_at", "last_generation", "last_error", "updated_at"):
        if not isinstance(value[key], str) or len(value[key]) > 512:
            raise errors_api.MonitorError("monitor scheduler state contains an invalid text field")
    if value["last_generation"] and not constants_api.GENERATION_ID_PATTERN.fullmatch(value["last_generation"]):
        raise errors_api.MonitorError("monitor scheduler generation is invalid")
    progress = value["full_reconciliation_in_progress"]
    if progress is not None:
        if not isinstance(progress, dict) or set(progress) != {
            "owner",
            "scheduled_at",
            "started_at",
            "expires_at",
        }:
            raise errors_api.MonitorError("monitor scheduler progress is invalid")
        if (
            not isinstance(progress["owner"], str)
            or not progress["owner"]
            or len(progress["owner"]) > 64
            or any(
                type(progress[key]) not in (int, float)
                or not math.isfinite(progress[key])
                or progress[key] < 0
                for key in ("scheduled_at", "started_at", "expires_at")
            )
        ):
            raise errors_api.MonitorError("monitor scheduler progress is invalid")
    return dict(value)


def load_scheduler_state(config_root: Path) -> dict[str, Any]:
    value = state_api._read_json(
        _scheduler_state_path(config_root),
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return _default_scheduler_state()
    return _validate_scheduler_state(value)


def save_scheduler_state(config_root: Path, value: dict[str, Any]) -> None:
    state_api._write_json(_scheduler_state_path(config_root), _validate_scheduler_state(value))


@contextmanager
def try_coordinator_lease(config_root: Path) -> Iterator[bool]:
    """Claim the one host-wide monitor coordinator for one bounded operation.

    The flock is the crash-recovery primitive: the kernel releases it when a
    process exits, including an unclean exit.  The separate lease record is
    only bounded private observability and is overwritten by the next owner,
    so a stale record can never block automatic recovery.
    """

    directory = state_api.monitor_root(config_root) / constants_api.LOCK_DIR
    state_api._ensure_private_directory(directory)
    lock_path = directory / "coordinator.lock"
    lease_path = directory / "coordinator-lease.json"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot create monitor coordinator lock: {exc}") from exc
    owner = secrets.token_hex(16)
    acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        acquired = True
        now = time.time()
        state_api._write_json(
            lease_path,
            {
                "version": constants_api.SCHEDULER_STATE_VERSION,
                "owner": owner,
                "pid": os.getpid(),
                "started_at": now,
                "expires_at": now + constants_api.COORDINATOR_LEASE_SECONDS,
            },
        )
        yield True
    finally:
        try:
            if acquired:
                try:
                    current = state_api._read_json(lease_path, max_bytes=constants_api.MAX_CONNECTION_BYTES)
                except errors_api.MonitorError:
                    # The lease record is observability only; a torn or
                    # externally damaged record must not mask the scan or
                    # prevent the kernel lock from being released.
                    current = None
                if isinstance(current, dict) and current.get("owner") == owner:
                    state_api._remove_private_file(lease_path, max_bytes=constants_api.MAX_CONNECTION_BYTES)
        finally:
            os.close(descriptor)


def _full_reconciliation_due(state: dict[str, Any], now: float) -> bool:
    if state.get("full_reconciliation_in_progress") is not None:
        return True
    next_due = state.get("next_full_reconciliation_at", 0.0)
    return type(next_due) in (int, float) and now >= float(next_due)


def _begin_full_reconciliation(
    config_root: Path,
    state: dict[str, Any],
    now: float,
    *,
    forced: bool = False,
) -> float:
    progress = state.get("full_reconciliation_in_progress")
    scheduled_at = progress.get("scheduled_at") if isinstance(progress, dict) else 0.0
    if not forced and not scheduled_at:
        scheduled_at = state.get("next_full_reconciliation_at", 0.0)
    if type(scheduled_at) not in (int, float) or scheduled_at <= 0:
        scheduled_at = (
            now
            if forced
            else math.floor(now / constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS)
            * constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS
        )
    state["full_reconciliation_in_progress"] = {
        "owner": secrets.token_hex(16),
        "scheduled_at": float(scheduled_at),
        "started_at": now,
        "expires_at": now + constants_api.COORDINATOR_LEASE_SECONDS,
    }
    state["updated_at"] = state_api._now()
    save_scheduler_state(config_root, state)
    return float(scheduled_at)


def _finish_full_reconciliation(
    config_root: Path,
    state: dict[str, Any],
    *,
    scheduled_at: float,
    generation: str,
    now: float,
) -> None:
    next_due = scheduled_at + constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS
    while next_due <= now:
        next_due += constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS
    state["next_full_reconciliation_at"] = next_due
    state["last_full_reconciliation_at"] = state_api._now()
    state["last_generation"] = publication_api._validate_generation_id(generation)
    state["last_error"] = ""
    state["updated_at"] = state_api._now()
    state["full_reconciliation_in_progress"] = None
    save_scheduler_state(config_root, state)


def _fail_full_reconciliation(
    config_root: Path,
    state: dict[str, Any],
    error: str,
) -> None:
    state["last_error"] = " ".join(error.split())[:512]
    state["updated_at"] = state_api._now()
    # A process crash leaves the in-progress marker behind, so the next owner
    # takes over immediately.  A completed-but-failed attempt, however, must
    # advance to the next wall-clock slot or ten active launches could perform
    # the same full scan in succession while the hub is unavailable.
    progress = state.get("full_reconciliation_in_progress")
    scheduled_at = (
        progress.get("scheduled_at")
        if isinstance(progress, dict)
        else time.time()
    )
    if type(scheduled_at) not in (int, float) or scheduled_at < 0:
        scheduled_at = time.time()
    next_due = float(scheduled_at) + constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS
    now = time.time()
    while next_due <= now:
        next_due += constants_api.FULL_RECONCILIATION_INTERVAL_SECONDS
    state["next_full_reconciliation_at"] = next_due
    state["full_reconciliation_in_progress"] = None
    save_scheduler_state(config_root, state)
