"""Configuration files, private writes, backups, and UI artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import (
    ConfigError,
    parse_config_text,
)


def default_config_path() -> Path:
    config_dir = os.environ.get("CAGE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "cage" / "config.toml"
    return Path.home() / ".config" / "cage" / "config.toml"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    return parse_config_text(text, path)


def atomic_write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        try:
            destination = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ConfigError(f"refusing to write through broken config symlink: {path}") from exc
        if not destination.is_file():
            raise ConfigError(f"config symlink target must be a regular file: {destination}")
    else:
        destination = path
    mode = destination.stat().st_mode & 0o777 if destination.exists() else 0o600
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(mode)
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def config_destination(path: Path) -> Path:
    if not path.is_symlink():
        if path.exists() and not path.is_file():
            raise ConfigError(f"config must be a regular file: {path}")
        return path
    try:
        destination = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ConfigError(f"refusing to use broken config symlink: {path}") from exc
    if not destination.is_file():
        raise ConfigError(f"config symlink target must be a regular file: {destination}")
    return destination


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_config_backup(config_path: Path, text: str) -> None:
    backup_dir = config_path.parent / "backups"
    if backup_dir.is_symlink():
        raise ConfigError(f"refusing symlinked config backup directory: {backup_dir}")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not backup_dir.is_dir() or backup_dir.stat().st_uid != os.getuid():
        raise ConfigError(f"config backup directory must be owned by the current user: {backup_dir}")
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"config-{timestamp}-{sha256_text(text)[:12]}.toml"
    atomic_write_text(backup, text)
    backup.chmod(0o600)
    backups = sorted(backup_dir.glob("config-*.toml"), key=lambda item: item.name, reverse=True)
    for old in backups[10:]:
        old.unlink()


@contextmanager
def config_write_lock(path: Path):
    destination = config_destination(path)
    lock_path = destination.parent / f".{destination.name}.lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ConfigError(f"cannot open private config lock {lock_path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConfigError(f"config lock must be a regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield destination
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_ui_request(path: Path) -> dict[str, Any]:
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
            raise OSError("artifact must be a regular file no larger than 1 MiB")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identity = lambda value: (value.st_dev, value.st_ino)
        if identity(before) != identity(opened) or identity(opened) != identity(current):
            os.close(descriptor)
            raise OSError("artifact changed while it was being opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid UI request artifact: {exc}") from exc
    if not isinstance(request, dict):
        raise ConfigError("UI request artifact must contain an object")
    return request
