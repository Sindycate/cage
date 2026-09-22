"""Bounded private file access, atomic writes and ownership checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from datetime import datetime, timezone

from . import errors as errors_api


def monitor_root(config_root: Path) -> Path:
    return config_root.resolve() / "monitor"


def _reject_unsafe_path(path: Path, *, max_bytes: int) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot inspect monitor state {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise errors_api.MonitorError(f"unsafe monitor state file: {path}")
    if info.st_nlink != 1:
        raise errors_api.MonitorError(f"hard-linked monitor state file: {path}")
    if info.st_uid != os.getuid():
        raise errors_api.MonitorError(f"monitor state file is not owned by the current user: {path}")
    if info.st_size > max_bytes:
        raise errors_api.MonitorError(f"monitor state file is too large: {path}")
    if info.st_mode & 0o077:
        raise errors_api.MonitorError(f"monitor state file permissions are too broad: {path}")
    return info


def _read_json(path: Path, *, max_bytes: int) -> object | None:
    try:
        _reject_unsafe_path(path, max_bytes=max_bytes)
    except FileNotFoundError:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise errors_api.MonitorError(f"invalid monitor state {path}: {exc}") from exc
    return value


def _secure_collector_file(path: Path, *, max_bytes: int) -> None:
    """Adopt a collector-created file after strict ownership and link checks."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot inspect collector state {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise errors_api.MonitorError(f"unsafe collector state file: {path}")
    if info.st_nlink != 1:
        raise errors_api.MonitorError(f"hard-linked collector state file: {path}")
    if info.st_uid != os.getuid():
        raise errors_api.MonitorError(f"collector state file is not owned by the current user: {path}")
    if info.st_size > max_bytes:
        raise errors_api.MonitorError(f"collector state file is too large: {path}")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot secure collector state {path}: {exc}") from exc


def _ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while True:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            missing.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise errors_api.MonitorError(f"cannot create monitor directory: {path}")
            candidate = parent
            continue
        except OSError as exc:
            raise errors_api.MonitorError(f"cannot inspect monitor directory {candidate}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise errors_api.MonitorError(f"unsafe monitor directory: {candidate}")
        if candidate != Path("/") and info.st_uid != os.getuid():
            raise errors_api.MonitorError(f"monitor directory is not owned by the current user: {candidate}")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise errors_api.MonitorError(f"cannot inspect monitor directory {directory}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise errors_api.MonitorError(f"unsafe monitor directory: {directory}")
        if info.st_uid != os.getuid():
            raise errors_api.MonitorError(f"monitor directory is not owned by the current user: {directory}")
        os.chmod(directory, 0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot secure monitor directory {path}: {exc}") from exc


def _write_json(path: Path, value: object) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot write monitor state {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_private_file(path: Path, *, max_bytes: int) -> None:
    try:
        _reject_unsafe_path(path, max_bytes=max_bytes)
    except FileNotFoundError:
        return
    try:
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot remove monitor state {path.name}: {exc}") from exc


def _remove_owned_directory(path: Path, *, description: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise errors_api.MonitorError(f"unsafe {description} directory")
    if info.st_uid != os.getuid():
        raise errors_api.MonitorError(f"{description} directory is not owned by the current user")
    shutil.rmtree(path)
