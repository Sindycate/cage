"""Cross-process ownership for registry, source and aggregation mutations."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from . import constants as constants_api
from . import errors as errors_api
from . import state as state_api
from . import validation as validation_api


@contextmanager
def _registry_write_lock(config_root: Path) -> Iterator[None]:
    """Serialize registry read/modify/write transactions across launches."""

    directory = state_api.monitor_root(config_root) / constants_api.LOCK_DIR
    state_api._ensure_private_directory(directory)
    path = directory / "registry.lock"
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot lock monitor registry: {exc}") from exc
    try:
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise errors_api.MonitorError(f"cannot lock monitor registry: {exc}") from exc
        yield
    finally:
        os.close(descriptor)


def _lock_path(config_root: Path, logical_id: str) -> Path:
    validation_api.validate_logical_id(logical_id)
    return state_api.monitor_root(config_root) / constants_api.LOCK_DIR / f"{logical_id}.lock"


@contextmanager
def try_volume_lock(config_root: Path, logical_id: str) -> Iterator[bool]:
    path = _lock_path(config_root, logical_id)
    state_api._ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


@contextmanager
def _wait_for_volume_lock(
    config_root: Path,
    logical_id: str,
    *,
    timeout_seconds: float = constants_api.SCAN_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Wait briefly for a peer launch to finish the same volume refresh.

    The non-blocking primitive remains public for callers that need to skip a
    busy volume.  Collection itself waits so two simultaneous sessions for a
    shared state volume converge on one trusted snapshot instead of racing or
    making the second session fail merely because the first collector is slow.
    """

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        with try_volume_lock(config_root, logical_id) as acquired:
            if acquired:
                yield
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise errors_api.MonitorError("monitor volume scan is already running")
        time.sleep(min(0.05, remaining))


@dataclass
class HostSourceLease:
    """Serialize live Cage host sessions that share one managed CODEX_HOME.

    The session store is auth-scoped rather than repository-scoped.  Holding a
    small private lease for the complete process prevents a second host launch
    from replacing its static/auth snapshot while Codex is using it.  Separate
    source directories retain independent concurrency.
    """

    descriptor: int | None

    @classmethod
    def acquire(cls, config_root: Path, logical_id: str) -> "HostSourceLease":
        validation_api.validate_logical_id(logical_id)
        directory = state_api.monitor_root(config_root) / constants_api.LOCK_DIR
        state_api._ensure_private_directory(directory)
        path = directory / f"host-source-{logical_id}.lock"
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise errors_api.MonitorError("cannot open host Codex session lease") from exc
        try:
            opened = os.fstat(descriptor)
            current = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise errors_api.MonitorError("host Codex session lease is unsafe")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise errors_api.MonitorError(
                    "another Cage host session is already using this monitored auth source"
                ) from exc
            lease = cls(descriptor)
            descriptor = -1
            return lease
        except OSError as exc:
            raise errors_api.MonitorError("cannot acquire host Codex session lease") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def close(self) -> int:
        if self.descriptor is None:
            return 0
        descriptor = self.descriptor
        self.descriptor = None
        try:
            os.close(descriptor)
        except OSError as exc:
            raise errors_api.MonitorError("cannot release host Codex session lease") from exc
        return 0

    def __enter__(self) -> "HostSourceLease":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


@contextmanager
def try_aggregate_lock(config_root: Path) -> Iterator[bool]:
    directory = state_api.monitor_root(config_root) / constants_api.LOCK_DIR
    state_api._ensure_private_directory(directory)
    path = directory / "aggregate.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)
