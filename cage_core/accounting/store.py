"""Small private transactions. No history, Docker or HTTP under these locks."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from ..monitoring.errors import MonitorError


class AccountingError(MonitorError):
    pass


class Busy(AccountingError):
    pass


class Invalid(AccountingError):
    """Malformed durable inputs cannot improve with automatic retries."""


MAX_BYTES = 2 * 1024 * 1024
NAME = re.compile(r"[a-zA-Z0-9_.-]{1,160}\Z")


def checked_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in {".", ".."}:
        raise AccountingError("invalid accounting state name")
    return name


def check_file(fd: int, limit: int = MAX_BYTES) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
        raise AccountingError("unsafe or oversized accounting state")


@contextmanager
def directory(parent: int, name: str, *, create: bool = True):
    checked_name(name)
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AccountingError("unsafe accounting directory")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def root(config_root: Path, *, create: bool = True):
    fd = os.open(config_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise AccountingError("unsafe Cage configuration directory")
        with directory(fd, "accounting", create=create) as owned:
            yield owned
    finally:
        os.close(fd)


def read(fd: int, name: str, *, limit: int = MAX_BYTES):
    checked_name(name)
    try:
        opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(opened, "rb") as handle:
        check_file(handle.fileno(), limit)
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise AccountingError("oversized accounting state")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AccountingError("invalid accounting state JSON") from exc


def write(fd: int, name: str, value) -> None:
    checked_name(name)
    # Refuse unsafe existing destinations as well as unsafe new files.
    read(fd, name)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_BYTES:
        raise AccountingError("accounting state exceeds its byte limit")
    temporary = ".pending-" + secrets.token_hex(16)
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(out, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def remove(fd: int, name: str) -> None:
    if read(fd, name) is not None:
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)


@contextmanager
def lock(fd: int, name: str, *, timeout: float = 0.25):
    opened = os.open(checked_name(name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    try:
        check_file(opened, 0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(opened, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("accounting state is busy; retry shortly")
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(opened)
