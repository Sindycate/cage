"""Optional host-owned Token Monitor integration.

The monitor is deliberately outside the launch-plan contract.  Cage keeps the
hub credential and all aggregation traffic on the host, while a short-lived
collector container receives only the two Codex session subdirectories from a
registered volume.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import storage


STATE_VERSION = 1
REGISTRY_VERSION = 2
PRICING_VERSION = 1
SPLIT_STATUS_VERSION = 1
SCHEDULER_STATE_VERSION = 1
VOLUME_SNAPSHOT_VERSION = 1
UPLOAD_STATE_VERSION = 1
COLLECTOR_IMAGE = "cage-token-monitor"
COLLECTOR_REGISTRY = "ghcr.io/sindycate/cage/token-monitor"
COLLECTOR_DOCKERFILE = "Dockerfile.monitor"
COLLECTOR_SOURCE_VERSION = "0.49.0"
COLLECTOR_SOURCE_COMMIT = "7c74e61fd8f9d592e647f14107738746a51e49ff"
COLLECTOR_SOURCE_SHA256 = "c2f72a31e372b495c0816af561ff789233e0cb2cae2e7e8098d686f9b7fd441e"
COLLECTOR_SOURCE_URL = (
    "https://github.com/Javis603/token-monitor/archive/refs/tags/"
    f"v{COLLECTOR_SOURCE_VERSION}.tar.gz"
)

MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 24 * 60 * 60
MAX_CONNECTION_BYTES = 16 * 1024
MAX_SECRET_BYTES = 8192
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
SCAN_TIMEOUT_SECONDS = 180
FULL_RECONCILIATION_INTERVAL_SECONDS = 60 * 60
COORDINATOR_LEASE_SECONDS = SCAN_TIMEOUT_SECONDS + 60
MAX_SNAPSHOT_BYTES = 2 * MAX_OUTPUT_BYTES
MAX_GENERATIONS = 4
COLLECTOR_MEMORY = "1g"
COLLECTOR_CPUS = "1.0"
COLLECTOR_PIDS = "128"

# Token Monitor's local hub receives the upstream agent's sync payload.  Keep
# this top-level wire contract explicit so a future upstream collector cannot
# silently add native-session, credential, or diagnostic fields to a Cage
# upload.  The omission fields are produced by the pinned sync serializer when
# it trims an oversized payload; the remaining fields are the v0.49.0 summary
# contract (including optional fields).
COLLECTOR_SUMMARY_FIELDS = frozenset(
    {
        "deviceId",
        "hostname",
        "platform",
        "osName",
        "osVersion",
        "updatedAt",
        "agentVersion",
        "agentRuntime",
        "projectsEnabled",
        "trackedClients",
        "clientStatus",
        "clientHealth",
        "wslStatus",
        "periodWindows",
        "historyAvailable",
        "today",
        "month",
        "allTime",
        "history",
        "limits",
        "allTimeProjectsOmitted",
        "allTimeProjectsIncomplete",
        "sessionDetailsOmitted",
        "periodProjectsOmitted",
        "syncUploadIntervalMs",
    }
)

CONNECTION_FILE = "connection.json"
IDENTITY_FILE = "identity.json"
REGISTRY_FILE = "registry.json"
LOCK_DIR = "locks"
DEVICE_DIR = "devices"
PROJECT_DIR = "projects"
RUN_DIR = "runs"
PRICING_FILE = "pricing.json"
AGGREGATE_STATUS_FILE = "aggregate-status.json"
SPLIT_STATUS_FILE = "split-status.json"
SCHEDULER_STATE_FILE = "scheduler.json"
UPLOAD_STATE_FILE = "upload-state.json"
GENERATION_DIR = "generations"
VOLUME_SNAPSHOT_FILE = "volume-snapshot.json"
VOLUME_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
DEVICE_ID_PATTERN = re.compile(r"^cage-[a-z0-9_-]{1,120}$")
LOGICAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
GENERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CODEX_VOLUME_PREFIX = "codex-state-"
UNATTRIBUTED_PROVIDER = "unattributed"


class MonitorError(RuntimeError):
    """Raised for an invalid monitor state or an unavailable collector."""


def validate_volume_name(value: object) -> str:
    if not isinstance(value, str) or not VOLUME_NAME_PATTERN.fullmatch(value):
        raise MonitorError("invalid monitor volume name")
    return value


def validate_device_id(value: object) -> str:
    if not isinstance(value, str) or not DEVICE_ID_PATTERN.fullmatch(value):
        raise MonitorError("invalid monitor device id")
    return value


def validate_logical_id(value: object) -> str:
    if not isinstance(value, str) or not LOGICAL_ID_PATTERN.fullmatch(value):
        raise MonitorError("invalid monitor logical target id")
    return value


def validate_display_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character in value for character in "\x00\r\n\t")
    ):
        raise MonitorError("invalid monitor display name")
    return value


def validate_fingerprint(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "driver",
        "scope",
        "created_at",
        "label_identity",
    } or any(
        not isinstance(item, str)
        or len(item) > 512
        or (key != "label_identity" and not item)
        for key, item in value.items()
    ):
        raise MonitorError("monitor volume fingerprint is invalid")
    return dict(value)


@dataclass(frozen=True)
class MonitorConnection:
    hub_url: str
    secret: str
    interval_seconds: int = 300
    enabled: bool = True


@dataclass(frozen=True)
class VolumeRegistration:
    logical_id: str
    device_id: str
    volume_name: str
    target: str
    repository: str
    display_name: str
    fingerprint: dict[str, str]
    status: str = "active"
    registered_at: str = ""
    last_scan_at: str = ""
    last_success_at: str = ""
    last_error: str = ""
    legacy_device_id: str = ""

    @classmethod
    def from_dict(cls, value: object) -> "VolumeRegistration":
        if not isinstance(value, dict):
            raise MonitorError("monitor registry entry must be an object")
        required = {
            "logical_id",
            "device_id",
            "volume_name",
            "target",
            "repository",
            "display_name",
            "fingerprint",
            "status",
            "registered_at",
            "last_scan_at",
            "last_success_at",
            "last_error",
            "legacy_device_id",
        }
        if set(value) != required:
            raise MonitorError("monitor registry entry has an invalid shape")
        strings = (
            "logical_id",
            "device_id",
            "volume_name",
            "target",
            "repository",
            "display_name",
            "status",
            "registered_at",
            "last_scan_at",
            "last_success_at",
            "last_error",
            "legacy_device_id",
        )
        if any(not isinstance(value[name], str) for name in strings):
            raise MonitorError("monitor registry entry fields must be strings")
        validate_logical_id(value["logical_id"])
        validate_device_id(value["device_id"])
        if value["legacy_device_id"]:
            validate_device_id(value["legacy_device_id"])
        validate_volume_name(value["volume_name"])
        if (
            not value["repository"].startswith("/")
            or len(value["repository"]) > 4096
            or any(character in value["repository"] for character in "\x00\r\n")
        ):
            raise MonitorError("monitor registry repository is invalid")
        validate_display_name(value["display_name"])
        fingerprint = validate_fingerprint(value["fingerprint"])
        if value["target"] not in {"container", "desktop"}:
            raise MonitorError("monitor target is invalid")
        if value["status"] not in {"active", "retired", "disabled", "needs-adoption"}:
            raise MonitorError("monitor registration status is invalid")
        return cls(
            logical_id=value["logical_id"],
            device_id=value["device_id"],
            volume_name=value["volume_name"],
            target=value["target"],
            repository=value["repository"],
            display_name=value["display_name"],
            fingerprint=fingerprint,
            status=value["status"],
            registered_at=value["registered_at"],
            last_scan_at=value["last_scan_at"],
            last_success_at=value["last_success_at"],
            last_error=value["last_error"],
            legacy_device_id=value["legacy_device_id"],
        )

    def public_dict(self) -> dict[str, Any]:
        """Return a redacted status shape; never expose the repository path."""

        return {
            "logical_id": self.logical_id,
            "device_id": self.device_id,
            "project_id": "",
            "volume_name": self.volume_name,
            "target": self.target,
            "display_name": self.display_name,
            "status": self.status,
            "registered_at": self.registered_at,
            "last_scan_at": self.last_scan_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "legacy_device_id": self.legacy_device_id,
        }

    def public_dict_for(self, config_root: Path) -> dict[str, Any]:
        value = self.public_dict()
        value["project_id"] = project_id_for(config_root, self.logical_id)
        return value


def monitor_root(config_root: Path) -> Path:
    return config_root.resolve() / "monitor"


def _reject_unsafe_path(path: Path, *, max_bytes: int) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise MonitorError(f"cannot inspect monitor state {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MonitorError(f"unsafe monitor state file: {path}")
    if info.st_nlink != 1:
        raise MonitorError(f"hard-linked monitor state file: {path}")
    if info.st_uid != os.getuid():
        raise MonitorError(f"monitor state file is not owned by the current user: {path}")
    if info.st_size > max_bytes:
        raise MonitorError(f"monitor state file is too large: {path}")
    if info.st_mode & 0o077:
        raise MonitorError(f"monitor state file permissions are too broad: {path}")
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
        raise MonitorError(f"invalid monitor state {path}: {exc}") from exc
    return value


def _secure_collector_file(path: Path, *, max_bytes: int) -> None:
    """Adopt a collector-created file after strict ownership and link checks."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise MonitorError(f"cannot inspect collector state {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MonitorError(f"unsafe collector state file: {path}")
    if info.st_nlink != 1:
        raise MonitorError(f"hard-linked collector state file: {path}")
    if info.st_uid != os.getuid():
        raise MonitorError(f"collector state file is not owned by the current user: {path}")
    if info.st_size > max_bytes:
        raise MonitorError(f"collector state file is too large: {path}")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise MonitorError(f"cannot secure collector state {path}: {exc}") from exc


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
                raise MonitorError(f"cannot create monitor directory: {path}")
            candidate = parent
            continue
        except OSError as exc:
            raise MonitorError(f"cannot inspect monitor directory {candidate}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MonitorError(f"unsafe monitor directory: {candidate}")
        if candidate != Path("/") and info.st_uid != os.getuid():
            raise MonitorError(f"monitor directory is not owned by the current user: {candidate}")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise MonitorError(f"cannot inspect monitor directory {directory}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MonitorError(f"unsafe monitor directory: {directory}")
        if info.st_uid != os.getuid():
            raise MonitorError(f"monitor directory is not owned by the current user: {directory}")
        os.chmod(directory, 0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MonitorError(f"cannot secure monitor directory {path}: {exc}") from exc


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
        raise MonitorError(f"cannot write monitor state {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_interval(value: object) -> int:
    if type(value) is not int:
        raise MonitorError("monitor interval must be an integer")
    if not MIN_INTERVAL_SECONDS <= value <= MAX_INTERVAL_SECONDS:
        raise MonitorError(
            f"monitor interval must be between {MIN_INTERVAL_SECONDS} and "
            f"{MAX_INTERVAL_SECONDS} seconds"
        )
    return value


def _allowed_http_host(hostname: str) -> bool:
    lower = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(lower)
    except ValueError:
        return False
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address in ipaddress.ip_network("100.64.0.0/10")
        or address in ipaddress.ip_network("fd00::/8")
    ) and not address.is_unspecified


def normalize_hub_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MonitorError("Token Monitor hub URL must be a non-empty URL")
    raw = value.strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise MonitorError("Token Monitor hub URL is invalid") from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise MonitorError("Token Monitor hub URL must use https or http")
    if parts.username is not None or parts.password is not None:
        raise MonitorError("Token Monitor hub URL cannot contain userinfo")
    if parts.query or parts.fragment:
        raise MonitorError("Token Monitor hub URL cannot contain a query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise MonitorError("Token Monitor hub URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise MonitorError("Token Monitor hub URL has an invalid port")
    if parts.scheme == "http" and not _allowed_http_host(parts.hostname):
        raise MonitorError("plain HTTP is allowed only for a private or loopback hub")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _connection_path(root: Path) -> Path:
    return root / CONNECTION_FILE


def load_connection(config_root: Path) -> MonitorConnection | None:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    value = _read_json(_connection_path(root), max_bytes=MAX_CONNECTION_BYTES)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"version", "hub_url", "secret", "interval_seconds", "enabled"}:
        raise MonitorError("monitor connection has an invalid shape")
    if value["version"] != STATE_VERSION or not isinstance(value["secret"], str) or not value["secret"]:
        raise MonitorError("monitor connection secret is invalid")
    if len(value["secret"].encode("utf-8")) > MAX_SECRET_BYTES or any(
        character in value["secret"] for character in "\x00\r\n"
    ):
        raise MonitorError("monitor connection secret is invalid")
    if not isinstance(value["enabled"], bool):
        raise MonitorError("monitor connection enabled flag is invalid")
    return MonitorConnection(
        hub_url=normalize_hub_url(value["hub_url"]),
        secret=value["secret"],
        interval_seconds=validate_interval(value["interval_seconds"]),
        enabled=value["enabled"],
    )


def save_connection(config_root: Path, connection: MonitorConnection) -> None:
    if (
        not isinstance(connection.secret, str)
        or not connection.secret
        or len(connection.secret.encode("utf-8")) > MAX_SECRET_BYTES
        or any(character in connection.secret for character in "\x00\r\n")
    ):
        raise MonitorError("monitor connection secret is invalid")
    _write_json(
        _connection_path(monitor_root(config_root)),
        {
            "version": STATE_VERSION,
            "hub_url": normalize_hub_url(connection.hub_url),
            "secret": connection.secret,
            "interval_seconds": validate_interval(connection.interval_seconds),
            "enabled": bool(connection.enabled),
        },
    )


def disable_connection(config_root: Path) -> None:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    path = _connection_path(root)
    try:
        _reject_unsafe_path(path, max_bytes=MAX_CONNECTION_BYTES)
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
        raise MonitorError(f"cannot remove monitor connection: {exc}") from exc


@contextmanager
def _registry_write_lock(config_root: Path) -> Iterator[None]:
    """Serialize registry read/modify/write transactions across launches."""

    directory = monitor_root(config_root) / LOCK_DIR
    _ensure_private_directory(directory)
    path = directory / "registry.lock"
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        raise MonitorError(f"cannot lock monitor registry: {exc}") from exc
    try:
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise MonitorError(f"cannot lock monitor registry: {exc}") from exc
        yield
    finally:
        os.close(descriptor)


def _scheduler_state_path(config_root: Path) -> Path:
    return monitor_root(config_root) / SCHEDULER_STATE_FILE


def _default_scheduler_state() -> dict[str, Any]:
    return {
        "version": SCHEDULER_STATE_VERSION,
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
        raise MonitorError("monitor scheduler state has an invalid shape")
    if value["version"] != SCHEDULER_STATE_VERSION:
        raise MonitorError("monitor scheduler state has an invalid version")
    next_full = value["next_full_reconciliation_at"]
    if type(next_full) not in (int, float) or not math.isfinite(next_full) or next_full < 0:
        raise MonitorError("monitor scheduler next reconciliation time is invalid")
    for key in ("last_full_reconciliation_at", "last_generation", "last_error", "updated_at"):
        if not isinstance(value[key], str) or len(value[key]) > 512:
            raise MonitorError("monitor scheduler state contains an invalid text field")
    if value["last_generation"] and not GENERATION_ID_PATTERN.fullmatch(value["last_generation"]):
        raise MonitorError("monitor scheduler generation is invalid")
    progress = value["full_reconciliation_in_progress"]
    if progress is not None:
        if not isinstance(progress, dict) or set(progress) != {
            "owner",
            "scheduled_at",
            "started_at",
            "expires_at",
        }:
            raise MonitorError("monitor scheduler progress is invalid")
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
            raise MonitorError("monitor scheduler progress is invalid")
    return dict(value)


def load_scheduler_state(config_root: Path) -> dict[str, Any]:
    value = _read_json(
        _scheduler_state_path(config_root),
        max_bytes=MAX_CONNECTION_BYTES,
    )
    if value is None:
        return _default_scheduler_state()
    return _validate_scheduler_state(value)


def save_scheduler_state(config_root: Path, value: dict[str, Any]) -> None:
    _write_json(_scheduler_state_path(config_root), _validate_scheduler_state(value))


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
        raise MonitorError(f"cannot remove monitor state {path.name}: {exc}") from exc


@contextmanager
def try_coordinator_lease(config_root: Path) -> Iterator[bool]:
    """Claim the one host-wide monitor coordinator for one bounded operation.

    The flock is the crash-recovery primitive: the kernel releases it when a
    process exits, including an unclean exit.  The separate lease record is
    only bounded private observability and is overwritten by the next owner,
    so a stale record can never block automatic recovery.
    """

    directory = monitor_root(config_root) / LOCK_DIR
    _ensure_private_directory(directory)
    lock_path = directory / "coordinator.lock"
    lease_path = directory / "coordinator-lease.json"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise MonitorError(f"cannot create monitor coordinator lock: {exc}") from exc
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
        _write_json(
            lease_path,
            {
                "version": SCHEDULER_STATE_VERSION,
                "owner": owner,
                "pid": os.getpid(),
                "started_at": now,
                "expires_at": now + COORDINATOR_LEASE_SECONDS,
            },
        )
        yield True
    finally:
        try:
            if acquired:
                try:
                    current = _read_json(lease_path, max_bytes=MAX_CONNECTION_BYTES)
                except MonitorError:
                    # The lease record is observability only; a torn or
                    # externally damaged record must not mask the scan or
                    # prevent the kernel lock from being released.
                    current = None
                if isinstance(current, dict) and current.get("owner") == owner:
                    _remove_private_file(lease_path, max_bytes=MAX_CONNECTION_BYTES)
        finally:
            os.close(descriptor)


def retire_registration(config_root: Path, identity: str, *, disabled: bool) -> VolumeRegistration:
    """Retire one logical project, never an arbitrary row of a shared device."""

    if not isinstance(identity, str):
        raise MonitorError("monitor project identity is invalid")
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        for index, record in enumerate(registrations):
            if identity not in {record.logical_id, record.legacy_device_id}:
                continue
            updated = replace(record, status="disabled" if disabled else "retired", last_error="")
            registrations[index] = updated
            save_registry(config_root, registrations)
            return updated
    raise MonitorError("monitor project was not found")


def disable_all_registrations(config_root: Path) -> list[VolumeRegistration]:
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        updated = [replace(item, status="disabled", last_error="") for item in registrations]
        save_registry(config_root, updated)
        return updated


def clear_legacy_device_id(config_root: Path, logical_id: str, legacy_device_id: str) -> None:
    validate_logical_id(logical_id)
    validate_device_id(legacy_device_id)
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        record = next((item for item in registrations if item.logical_id == logical_id), None)
        if record is None or record.legacy_device_id != legacy_device_id:
            raise MonitorError("monitor legacy device changed")
        save_registry(
            config_root,
            [replace(item, legacy_device_id="") if item.logical_id == logical_id else item for item in registrations],
        )


def remove_aggregate_status(config_root: Path) -> None:
    path = monitor_root(config_root) / AGGREGATE_STATUS_FILE
    try:
        _reject_unsafe_path(path, max_bytes=MAX_CONNECTION_BYTES)
    except FileNotFoundError:
        pass
    else:
        try:
            path.unlink()
        except OSError as exc:
            raise MonitorError(f"cannot remove monitor aggregate status: {exc}") from exc
    # A successful explicit host-device forget also retires the private
    # scheduler, prepared generations, and repair journal.  Leaving payload
    # generations behind would retain aggregate session data after the user
    # deliberately forgot the host device.
    _remove_private_file(
        monitor_root(config_root) / SCHEDULER_STATE_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )
    _remove_private_file(
        monitor_root(config_root) / UPLOAD_STATE_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )
    _remove_owned_directory(
        monitor_root(config_root) / GENERATION_DIR,
        description="monitor upload generation",
    )


def remove_device_state(config_root: Path, device_id: str) -> None:
    validate_device_id(device_id)
    monitor_directory = monitor_root(config_root)
    _ensure_private_directory(monitor_directory)
    root = monitor_directory.resolve()
    devices = root / DEVICE_DIR
    _ensure_private_directory(devices)
    # Keep the final component unresolved so a malicious device entry cannot
    # redirect deletion through a symlink.  The strict device-id grammar makes
    # this path containment check structural rather than string-based.
    path = devices / device_id
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MonitorError("unsafe monitor device state directory")
    if info.st_uid != os.getuid():
        raise MonitorError("monitor device state directory is not owned by the current user")
    shutil.rmtree(path)


def _remove_owned_directory(path: Path, *, description: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MonitorError(f"unsafe {description} directory")
    if info.st_uid != os.getuid():
        raise MonitorError(f"{description} directory is not owned by the current user")
    shutil.rmtree(path)


def remove_project_state(config_root: Path, logical_id: str) -> None:
    validate_logical_id(logical_id)
    projects = monitor_root(config_root) / PROJECT_DIR
    _ensure_private_directory(projects)
    _remove_owned_directory(projects / project_id_for(config_root, logical_id), description="monitor project state")


def host_install_id(config_root: Path) -> str:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    value = _read_json(root / IDENTITY_FILE, max_bytes=MAX_CONNECTION_BYTES)
    if value is not None:
        if not isinstance(value, dict) or set(value) != {"version", "install_id"}:
            raise MonitorError("monitor identity has an invalid shape")
        install_id = value.get("install_id")
        if isinstance(install_id, str) and len(install_id) == 32 and all(c in "0123456789abcdef" for c in install_id):
            return install_id
        raise MonitorError("monitor identity is invalid")
    install_id = secrets.token_hex(16)
    _write_json(root / IDENTITY_FILE, {"version": STATE_VERSION, "install_id": install_id})
    return install_id


def logical_target_id(repository: str, target: str, preset: str = "") -> str:
    if target not in {"container", "desktop"}:
        raise MonitorError("monitor target must be container or desktop")
    material = f"{target}\0{repository}\0{preset if target == 'desktop' else ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def host_device_id(config_root: Path) -> str:
    """Return one readable, collision-resistant hub identity per Cage install."""

    return f"cage-local-{host_install_id(config_root)[:8]}"


def _platform_slug() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    value = re.sub(r"[^a-z0-9]+", "-", sys.platform.lower()).strip("-")
    return value[:16] or "host"


def _provider_slug(value: object) -> str | None:
    """Return a safe stable provider label, or ``None`` for unsafe input."""

    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "openai": "openai-api",
        "openai-api": "openai-api",
        "openai-compatible": "openai-compatible",
        "zllm": "zllm",
    }
    normalized = aliases.get(normalized, normalized)
    if not PROVIDER_SLUG_PATTERN.fullmatch(normalized):
        return None
    return normalized


def provider_device_id(config_root: Path, provider: str) -> str:
    """Return a readable device identity for one provider stream."""

    provider_id = _provider_slug(provider)
    if provider_id is None:
        raise MonitorError("invalid monitor provider identity")
    return f"cage-{provider_id}-{_platform_slug()}-{host_install_id(config_root)[:8]}"


def provider_display_name(provider: str) -> str:
    provider_id = _provider_slug(provider) or UNATTRIBUTED_PROVIDER
    if provider_id == "openai-api":
        return "OpenAI API"
    if provider_id == UNATTRIBUTED_PROVIDER:
        return "Unattributed"
    return provider_id.upper() if provider_id == "zllm" else provider_id


def device_id_for(config_root: Path, logical_id: str = "") -> str:
    """Compatibility wrapper for callers that previously passed a target id."""

    if logical_id:
        validate_logical_id(logical_id)
    return host_device_id(config_root)


def project_id_for(config_root: Path, logical_id: str) -> str:
    """Return a stable opaque project id without exposing a path-derived hash."""

    validate_logical_id(logical_id)
    digest = hmac.new(
        bytes.fromhex(host_install_id(config_root)),
        logical_id.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:20]
    return f"cage-project-{digest}"


def _registry_path(root: Path) -> Path:
    return root / REGISTRY_FILE


def _load_legacy_registration(config_root: Path, value: object) -> VolumeRegistration:
    if not isinstance(value, dict):
        raise MonitorError("monitor registry entry must be an object")
    required = {
        "logical_id", "device_id", "volume_name", "target", "repository",
        "display_name", "fingerprint", "status", "registered_at",
        "last_scan_at", "last_success_at", "last_error",
    }
    if set(value) != required:
        raise MonitorError("monitor registry entry has an invalid shape")
    legacy_device_id = validate_device_id(value.get("device_id"))
    upgraded = dict(value)
    upgraded["device_id"] = host_device_id(config_root)
    upgraded["legacy_device_id"] = legacy_device_id
    return VolumeRegistration.from_dict(upgraded)


def load_registry(config_root: Path) -> list[VolumeRegistration]:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    value = _read_json(_registry_path(root), max_bytes=MAX_REGISTRY_BYTES)
    if value is None:
        return []
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "registrations"}
        or value["version"] not in {1, REGISTRY_VERSION}
    ):
        raise MonitorError("monitor registry has an invalid shape")
    registrations = value["registrations"]
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise MonitorError("monitor registry registrations are invalid")
    if value["version"] == 1:
        result = [_load_legacy_registration(config_root, item) for item in registrations]
    else:
        result = [VolumeRegistration.from_dict(item) for item in registrations]
    if len({item.logical_id for item in result}) != len(result):
        raise MonitorError("monitor registry contains duplicate logical targets")
    expected_device = host_device_id(config_root)
    if any(item.device_id != expected_device for item in result):
        raise MonitorError("monitor registry contains an unexpected Cage device")
    legacy_ids = [item.legacy_device_id for item in result if item.legacy_device_id]
    if len(set(legacy_ids)) != len(legacy_ids):
        raise MonitorError("monitor registry contains duplicate legacy devices")
    return result


def save_registry(config_root: Path, registrations: list[VolumeRegistration]) -> None:
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise MonitorError("too many monitor registrations")
    serialized: list[dict[str, Any]] = []
    logical_ids: set[str] = set()
    legacy_ids: set[str] = set()
    expected_device = host_device_id(config_root)
    for item in registrations:
        if not isinstance(item, VolumeRegistration):
            raise MonitorError("monitor registry entries must be VolumeRegistration values")
        value = {
            "logical_id": item.logical_id,
            "device_id": item.device_id,
            "volume_name": item.volume_name,
            "target": item.target,
            "repository": item.repository,
            "display_name": item.display_name,
            "fingerprint": item.fingerprint,
            "status": item.status,
            "registered_at": item.registered_at,
            "last_scan_at": item.last_scan_at,
            "last_success_at": item.last_success_at,
            "last_error": item.last_error,
            "legacy_device_id": item.legacy_device_id,
        }
        validated = VolumeRegistration.from_dict(value)
        if validated.logical_id in logical_ids:
            raise MonitorError("monitor registry contains duplicate logical targets")
        if validated.device_id != expected_device:
            raise MonitorError("monitor registry contains an unexpected Cage device")
        if validated.legacy_device_id and validated.legacy_device_id in legacy_ids:
            raise MonitorError("monitor registry contains duplicate legacy devices")
        logical_ids.add(validated.logical_id)
        if validated.legacy_device_id:
            legacy_ids.add(validated.legacy_device_id)
        serialized.append(value)
    _write_json(
        _registry_path(monitor_root(config_root)),
        {
            "version": REGISTRY_VERSION,
            "registrations": serialized,
        },
    )


def _docker_json(docker: str, arguments: list[str], *, timeout: float = 15.0) -> object:
    try:
        result = subprocess.run(
            [docker, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError(f"Docker monitor operation failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().replace("\n", " ")[:300]
        raise MonitorError(f"Docker monitor operation failed: {detail or 'unknown error'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MonitorError("Docker monitor operation returned invalid JSON") from exc


def volume_fingerprint(docker: str, volume_name: str) -> dict[str, str]:
    validate_volume_name(volume_name)
    value = _docker_json(docker, ["volume", "inspect", volume_name])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise MonitorError("Docker returned an invalid monitor volume")
    item = value[0]
    labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
    created = item.get("CreatedAt")
    driver = item.get("Driver")
    scope = item.get("Scope", "")
    name = item.get("Name")
    if not all(isinstance(part, str) and part for part in (name, driver, scope, created)):
        raise MonitorError("Docker volume lacks a stable fingerprint")
    label_identity = labels.get("io.cage.identity", "")
    if not isinstance(label_identity, str):
        label_identity = ""
    return validate_fingerprint({
        "name": name,
        "driver": driver,
        "scope": scope,
        "created_at": created,
        "label_identity": label_identity,
    })


def ensure_codex_volume(
    docker: str,
    volume_name: str,
    *,
    logical_id: str,
) -> dict[str, str]:
    """Create a labeled Codex volume, preserving existing volume contents."""

    ensure_codex_volume_labels(docker, volume_name, logical_id=logical_id)
    return volume_fingerprint(docker, volume_name)


def ensure_codex_volume_labels(
    docker: str,
    volume_name: str,
    *,
    logical_id: str,
) -> None:
    """Ensure a newly-created Codex volume carries Cage ownership labels."""

    validate_volume_name(volume_name)
    validate_logical_id(logical_id)

    try:
        result = subprocess.run(
            [
                docker,
                "volume",
                "create",
                "--label",
                "io.cage.managed=true",
                "--label",
                "io.cage.resource=codex-state",
                "--label",
                f"io.cage.identity={logical_id}",
                volume_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError(f"cannot create Codex state volume: {exc}") from exc
    if result.returncode != 0:
        raise MonitorError(f"cannot create Codex state volume: {result.stderr.strip()[:300]}")


def _recovered_registration_for_launch(
    registrations: list[VolumeRegistration],
    *,
    volume_name: str,
    target: str,
    fingerprint: dict[str, str],
    display_name: str,
) -> VolumeRegistration | None:
    """Return one safe discovery record that a normal launch can reuse.

    Discovery cannot know the host path for an old volume, so it stores a
    synthetic ``Recovered`` project.  A launch may reuse that record only when
    the exact volume and Docker fingerprint still match and no other active
    registration claims the volume.  This keeps automatic monitoring useful
    without silently moving a real project or adopting a replacement volume.
    """

    if target != "container":
        return None
    candidates = [
        item
        for item in registrations
        if item.volume_name == volume_name
        and item.status not in {"retired", "disabled"}
    ]
    if len(candidates) != 1:
        return None
    record = candidates[0]
    if (
        record.status != "active"
        or record.target != "container"
        or record.repository != recovered_repository(volume_name)
        or record.fingerprint != fingerprint
    ):
        return None
    label_identity = fingerprint.get("label_identity", "")
    if label_identity and label_identity != record.logical_id:
        return None
    # The repository and logical identity are deliberately not changed here.
    # Only a safe basename-plus-target label may become readable after the
    # corresponding project launches.  The caller persists this replacement
    # atomically while holding the registry transaction lock.
    return replace(record, display_name=display_name)


def register_volume(
    config_root: Path,
    docker: str,
    *,
    volume_name: str,
    repository: str,
    target: str,
    preset: str,
    display_name: str,
    fingerprint: dict[str, str] | None = None,
    allow_replacement: bool = False,
    reuse_recovered: bool = False,
) -> VolumeRegistration:
    if (
        not Path(repository).is_absolute()
        or len(repository) > 4096
        or any(character in repository for character in "\0\r\n")
    ):
        raise MonitorError("monitor repository must be an absolute path")
    validate_display_name(display_name)
    validate_volume_name(volume_name)
    logical_id = logical_target_id(repository, target, preset)
    device_id = device_id_for(config_root, logical_id)
    current = validate_fingerprint(fingerprint or volume_fingerprint(docker, volume_name))
    if current["name"] != volume_name:
        raise MonitorError("Docker volume fingerprint name mismatch")
    label_identity = current.get("label_identity", "")
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        if reuse_recovered:
            recovered = _recovered_registration_for_launch(
                registrations,
                volume_name=volume_name,
                target=target,
                fingerprint=current,
                display_name=display_name,
            )
            if recovered is not None:
                if recovered.display_name != next(
                    item.display_name
                    for item in registrations
                    if item.logical_id == recovered.logical_id
                ):
                    save_registry(
                        config_root,
                        [
                            recovered if item.logical_id == recovered.logical_id else item
                            for item in registrations
                        ],
                    )
                return recovered
        if label_identity and label_identity != logical_id and not allow_replacement:
            raise MonitorError("monitor volume label belongs to a different logical target; run cage monitor add explicitly")
        existing = next((item for item in registrations if item.logical_id == logical_id), None)
        if existing is not None:
            if existing.volume_name != volume_name:
                raise MonitorError("monitor logical target maps to a different volume")
            if existing.fingerprint != current and not allow_replacement:
                updated = replace(existing, status="needs-adoption", last_error="volume fingerprint changed; run cage monitor add explicitly")
                save_registry(config_root, [updated if item.logical_id == logical_id else item for item in registrations])
                raise MonitorError("monitor volume changed; explicit adoption is required")
            if existing.status in {"disabled", "retired"} and not allow_replacement:
                raise MonitorError("monitor registration is retired; run cage monitor add explicitly")
            record = replace(
                existing,
                fingerprint=current,
                status="active",
                last_error="",
                device_id=device_id,
            )
            save_registry(config_root, [record if item.logical_id == logical_id else item for item in registrations])
            return record
        if any(
            item.volume_name == volume_name
            and item.logical_id != logical_id
            and item.status not in {"retired", "disabled"}
            for item in registrations
        ):
            raise MonitorError(
                "monitor volume is already registered to a different logical target"
            )
        record = VolumeRegistration(
            logical_id=logical_id,
            device_id=device_id,
            volume_name=volume_name,
            target=target,
            repository=repository,
            display_name=display_name,
            fingerprint=current,
            registered_at=_now(),
        )
        save_registry(config_root, [*registrations, record])
        return record


def update_registration(config_root: Path, record: VolumeRegistration) -> None:
    validate_logical_id(record.logical_id)
    validate_device_id(record.device_id)
    validate_volume_name(record.volume_name)
    validate_display_name(record.display_name)
    validate_fingerprint(record.fingerprint)
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        if not any(item.logical_id == record.logical_id for item in registrations):
            raise MonitorError("monitor registration disappeared")
        save_registry(config_root, [record if item.logical_id == record.logical_id else item for item in registrations])


def _record_scan_error(config_root: Path, record: VolumeRegistration, error: str) -> None:
    """Best-effort status bookkeeping that never masks the scan failure."""

    try:
        with _registry_write_lock(config_root):
            registrations = load_registry(config_root)
            current = next(
                (item for item in registrations if item.logical_id == record.logical_id),
                None,
            )
            if current is None:
                return
            updated = replace(
                current,
                last_scan_at=_now(),
                last_error=" ".join(error.split())[:512],
            )
            save_registry(
                config_root,
                [updated if item.logical_id == record.logical_id else item for item in registrations],
            )
    except (MonitorError, OSError):
        return


def _lock_path(config_root: Path, logical_id: str) -> Path:
    validate_logical_id(logical_id)
    return monitor_root(config_root) / LOCK_DIR / f"{logical_id}.lock"


@contextmanager
def try_volume_lock(config_root: Path, logical_id: str) -> Iterator[bool]:
    path = _lock_path(config_root, logical_id)
    _ensure_private_directory(path.parent)
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
    timeout_seconds: float = SCAN_TIMEOUT_SECONDS,
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
            raise MonitorError("monitor volume scan is already running")
        time.sleep(min(0.05, remaining))


def _project_state_path(config_root: Path, record: VolumeRegistration) -> Path:
    """Return isolated collector state and adopt a legacy archive once."""

    project_id = project_id_for(config_root, record.logical_id)
    projects = monitor_root(config_root) / PROJECT_DIR
    _ensure_private_directory(projects)
    path = projects / project_id
    if path.exists():
        _ensure_private_directory(path)
        return path
    if record.legacy_device_id:
        legacy = monitor_root(config_root) / DEVICE_DIR / record.legacy_device_id
        try:
            info = os.lstat(legacy)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise MonitorError("unsafe legacy monitor device state directory")
            if info.st_uid != os.getuid():
                raise MonitorError("legacy monitor device state is not owned by the current user")
            try:
                os.replace(legacy, path)
            except OSError as exc:
                raise MonitorError(f"cannot adopt legacy monitor archive: {exc}") from exc
            _ensure_private_directory(path)
            return path
    _ensure_private_directory(path)
    return path


def _volume_snapshot_path(config_root: Path, record: VolumeRegistration) -> Path:
    return _project_state_path(config_root, record) / VOLUME_SNAPSHOT_FILE


def _summary_content_hash(payload: dict[str, Any]) -> str:
    """Hash stable usage content while ignoring collector observation times."""

    value = json.loads(json.dumps(payload, ensure_ascii=True))
    if isinstance(value, dict):
        value.pop("updatedAt", None)
        limits = value.get("limits")
        if isinstance(limits, dict):
            limits.pop("updatedAt", None)
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return timestamp if math.isfinite(timestamp) else None


def _load_trusted_volume_snapshot(
    config_root: Path,
    record: VolumeRegistration,
) -> tuple[dict[str, Any] | None, bool]:
    value = _read_json(
        _volume_snapshot_path(config_root, record),
        max_bytes=MAX_SNAPSHOT_BYTES,
    )
    if value is None:
        return None, False
    if not isinstance(value, dict) or set(value) != {
        "version",
        "logical_id",
        "device_id",
        "volume_name",
        "fingerprint",
        "display_name",
        "captured_at",
        "summary_hash",
        "payload",
    }:
        raise MonitorError("monitor volume snapshot has an invalid shape")
    if (
        value["version"] != VOLUME_SNAPSHOT_VERSION
        or value["logical_id"] != record.logical_id
        or value["device_id"] != record.device_id
        or value["volume_name"] != record.volume_name
        or value["fingerprint"] != record.fingerprint
    ):
        raise MonitorError("monitor volume snapshot identity does not match the registry")
    validate_display_name(value["display_name"])
    captured_at = value["captured_at"]
    if _snapshot_timestamp(captured_at) is None:
        raise MonitorError("monitor volume snapshot timestamp is invalid")
    summary_hash = value["summary_hash"]
    if not isinstance(summary_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", summary_hash):
        raise MonitorError("monitor volume snapshot hash is invalid")
    payload = _validate_summary(value["payload"], record.device_id)
    if _summary_content_hash(payload) != summary_hash:
        raise MonitorError("monitor volume snapshot hash does not match its payload")
    return payload, value["display_name"] != record.display_name


def load_volume_snapshot(
    config_root: Path,
    record: VolumeRegistration,
) -> dict[str, Any] | None:
    """Load one exact, sanitized last-known-good volume summary."""

    payload, _metadata_changed = _load_trusted_volume_snapshot(config_root, record)
    return payload


def _save_volume_snapshot(
    config_root: Path,
    record: VolumeRegistration,
    payload: dict[str, Any],
) -> None:
    payload = _validate_summary(payload, record.device_id)
    captured_at = _now()
    _write_json(
        _volume_snapshot_path(config_root, record),
        {
            "version": VOLUME_SNAPSHOT_VERSION,
            "logical_id": record.logical_id,
            "device_id": record.device_id,
            "volume_name": record.volume_name,
            "fingerprint": record.fingerprint,
            "display_name": record.display_name,
            "captured_at": captured_at,
            "summary_hash": _summary_content_hash(payload),
            "payload": payload,
        },
    )


def _snapshot_is_recent(
    config_root: Path,
    record: VolumeRegistration,
    *,
    max_age_seconds: int,
) -> bool:
    try:
        value = _read_json(
            _volume_snapshot_path(config_root, record),
            max_bytes=MAX_SNAPSHOT_BYTES,
        )
    except MonitorError:
        return False
    if not isinstance(value, dict):
        return False
    timestamp = _snapshot_timestamp(value.get("captured_at"))
    if timestamp is None:
        return False
    age = time.time() - timestamp
    return 0 <= age < max(MIN_INTERVAL_SECONDS, max_age_seconds)


def _validate_model_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 256
        or any(character in value for character in "\x00\r\n\t")
    ):
        raise MonitorError("invalid pricing model id")
    return value.strip()


def _validate_pricing_key(value: object) -> str:
    key = _validate_model_id(value)
    if ":" not in key:
        return key
    provider, model = key.split(":", 1)
    if _provider_slug(provider) is None or not model or ":" in model:
        raise MonitorError("invalid pricing provider/model key")
    return f"{_provider_slug(provider)}:{model}"


def _validate_unit_price(value: object, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise MonitorError("pricing rates must be finite non-negative numbers")
    return float(value)


def load_pricing(config_root: Path) -> dict[str, dict[str, float]]:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    value = _read_json(root / PRICING_FILE, max_bytes=MAX_CONNECTION_BYTES)
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"version", "models"} or value["version"] != PRICING_VERSION:
        raise MonitorError("monitor pricing has an invalid shape")
    models = value["models"]
    if not isinstance(models, dict) or len(models) > 1024:
        raise MonitorError("monitor pricing models are invalid")
    result: dict[str, dict[str, float]] = {}
    allowed = {"input_per_million", "output_per_million", "cache_read_per_million"}
    for raw_model, raw_rates in models.items():
        model = _validate_pricing_key(raw_model)
        if not isinstance(raw_rates, dict) or set(raw_rates).difference(allowed):
            raise MonitorError("monitor pricing entry has an invalid shape")
        if not ({"input_per_million", "output_per_million"} & set(raw_rates)):
            raise MonitorError("monitor pricing needs an input or output rate")
        result[model] = {
            key: _validate_unit_price(rate)  # type: ignore[dict-item]
            for key, rate in raw_rates.items()
        }
    return result


def save_pricing(config_root: Path, models: dict[str, dict[str, float]]) -> None:
    normalized: dict[str, dict[str, float]] = {}
    if not isinstance(models, dict) or len(models) > 1024:
        raise MonitorError("monitor pricing models are invalid")
    for model, rates in models.items():
        model_id = _validate_pricing_key(model)
        if not isinstance(rates, dict):
            raise MonitorError("monitor pricing entry has an invalid shape")
        allowed = {"input_per_million", "output_per_million", "cache_read_per_million"}
        if set(rates).difference(allowed) or not ({"input_per_million", "output_per_million"} & set(rates)):
            raise MonitorError("monitor pricing entry has an invalid shape")
        normalized[model_id] = {key: _validate_unit_price(value) for key, value in rates.items()}  # type: ignore[dict-item]
    _write_json(
        monitor_root(config_root) / PRICING_FILE,
        {"version": PRICING_VERSION, "models": normalized},
    )


def set_model_pricing(
    config_root: Path,
    model: str,
    *,
    input_per_million: float | None,
    output_per_million: float | None,
    cache_read_per_million: float | None,
) -> None:
    model_id = _validate_pricing_key(model)
    if input_per_million is None and output_per_million is None:
        raise MonitorError("pricing needs --input or --output")
    rates = {
        key: value
        for key, value in {
            "input_per_million": _validate_unit_price(input_per_million, optional=True),
            "output_per_million": _validate_unit_price(output_per_million, optional=True),
            "cache_read_per_million": _validate_unit_price(cache_read_per_million, optional=True),
        }.items()
        if value is not None
    }
    models = load_pricing(config_root)
    models[model_id] = rates  # type: ignore[assignment]
    save_pricing(config_root, models)


def remove_model_pricing(config_root: Path, model: str) -> bool:
    model_id = _validate_pricing_key(model)
    models = load_pricing(config_root)
    existed = model_id in models
    models.pop(model_id, None)
    save_pricing(config_root, models)
    return existed


def _write_tokscale_pricing(config_root: Path, state_path: Path) -> None:
    models = load_pricing(config_root)
    destination = state_path / "tokscale"
    _ensure_private_directory(destination)
    upstream: dict[str, dict[str, float]] = {}
    mapping = {
        "input_per_million": "input_cost_per_million_tokens",
        "output_per_million": "output_cost_per_million_tokens",
        "cache_read_per_million": "cache_read_input_token_cost_per_million_tokens",
    }
    for model, rates in models.items():
        # Tokscale accepts model-only keys.  Provider-qualified prices are
        # applied by Cage after collection, so they must not be handed to the
        # upstream collector as if they were model IDs.
        if ":" in model:
            continue
        upstream[model] = {mapping[key]: value for key, value in rates.items()}
    _write_json(destination / "custom-pricing.json", {"models": upstream})


def collector_image(version: str) -> str:
    return f"{COLLECTOR_IMAGE}:{version}"


def collector_registry_image(version: str) -> str:
    return f"{COLLECTOR_REGISTRY}:{version}"


def ensure_collector_image(
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
) -> str:
    image = collector_image(version)
    def run(arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [docker, *arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MonitorError(f"Docker monitor operation failed: {exc}") from exc

    inspect = run(["image", "inspect", image], timeout=30)
    if inspect.returncode == 0:
        return image
    registry = collector_registry_image(version)
    pull = run(["pull", registry], timeout=300)
    if pull.returncode == 0:
        tagged = run(["tag", registry, image], timeout=30)
        if tagged.returncode == 0:
            return image
    if not allow_build:
        raise MonitorError(f"collector image {image} is unavailable")
    try:
        storage.preflight(docker, storage_policy, preferred_image=image, requires_build=True)
    except Exception as exc:
        raise MonitorError(f"collector image build preflight failed: {exc}") from exc
    base = f"cage-base:{version}"
    base_inspect = run(["image", "inspect", base], timeout=30)
    if base_inspect.returncode != 0:
        result = run(
            [
                "build", "--build-arg", f"CAGE_VERSION={version}", "-t", base,
                "-f", str(install_root / "Dockerfile.base"), str(install_root),
            ],
            timeout=1800,
        )
        if result.returncode != 0:
            raise MonitorError("shared base image build failed")
    result = run(
        [
            "build", "--build-arg", f"CAGE_BASE={base}", "--build-arg",
            f"CAGE_VERSION={version}", "-t", image, "-f",
            str(install_root / COLLECTOR_DOCKERFILE), str(install_root),
        ],
        timeout=1800,
    )
    if result.returncode != 0:
        raise MonitorError("Token Monitor collector image build failed")
    return image


def _subpath_available(docker: str, image: str, volume_name: str, subpath: str) -> bool:
    command = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--mount",
        # Docker otherwise copies an empty destination directory back into an
        # empty volume subpath, even for a read-only mount.  That copy-up can
        # reset Cage's Codex session directory ownership to root:root.
        f"type=volume,src={volume_name},dst=/probe,readonly,volume-subpath={subpath},volume-nocopy",
        "--entrypoint",
        "/usr/bin/true",
        image,
        "true",
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError(f"cannot probe Codex volume subpath: {exc}") from exc
    if result.returncode == 0:
        return True
    raw_message = result.stderr.strip().replace("\n", " ")
    lower = raw_message.lower()
    message = raw_message[:300]
    if "volume-subpath" in lower and any(
        marker in lower
        for marker in (
            "not supported",
            "unsupported",
            "unknown flag",
            "unknown option",
            "invalid option",
            "invalid field",
        )
    ):
        raise MonitorError(
            "Docker does not support volume-subpath; refusing an unscoped Codex scan"
        )
    # Docker daemon versions differ here: some mention volume-subpath, while
    # others report only the host volume _data path from lstat. Both forms
    # mean that this exact optional directory is absent. Treat it as an empty
    # scan input; never fall back to an unscoped volume mount.
    if any(marker in lower for marker in ("does not exist", "no such file or directory")) and (
        "volume-subpath" in lower or "/_data/" in lower
    ):
        return False
    raise MonitorError(f"Codex volume subpath probe failed: {message or 'unknown error'}")


def _validate_summary(payload: object, expected_device_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MonitorError("collector output must be a JSON object")
    if set(payload).difference(COLLECTOR_SUMMARY_FIELDS):
        raise MonitorError("collector output has unexpected fields")
    if payload.get("deviceId") != expected_device_id:
        raise MonitorError("collector output device identity mismatch")
    if payload.get("trackedClients") != ["codex"]:
        raise MonitorError("collector output tracked-client policy mismatch")
    limits = payload.get("limits")
    if not isinstance(limits, dict) or set(limits) - {"updatedAt", "refreshMs", "providers"}:
        raise MonitorError("collector output has an invalid limits shape")
    # The upstream agent may stamp the limits probe time even when provider
    # limits are disabled.  The absence of provider records, rather than an
    # empty timestamp, is the privacy boundary we enforce here.
    if limits.get("providers") not in (None, []):
        raise MonitorError("collector output unexpectedly contains account limits")
    for period_name in ("today", "month", "allTime"):
        period = payload.get(period_name)
        if not isinstance(period, dict):
            raise MonitorError(f"collector output is missing {period_name}")
        total = period.get("totalTokens", 0)
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise MonitorError("collector output has an invalid token total")
        cost = period.get("costUsd", 0)
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise MonitorError("collector output has an invalid cost total")
        for field in (
            "clients", "clientCosts", "models", "modelCosts", "sessions", "projects"
        ):
            if field in period and not isinstance(period[field], dict):
                raise MonitorError(f"collector output has invalid {field}")
    # Token Monitor's sync payload intentionally strips native paths and
    # unbounded all-time sessions. Reject obvious path-bearing fields even if a
    # future upstream version accidentally reintroduces them.
    encoded = json.dumps(payload, ensure_ascii=True)
    if (
        "/home/" in encoded
        or "/Users/" in encoded
        or "\\Users\\" in encoded
        or "CODEX_HOME" in encoded
        or "/scan/" in encoded
    ):
        raise MonitorError("collector output contains a source path")
    return payload


def _archive_sessions_for_payload(state_path: Path, payload: dict[str, Any]) -> None:
    """Restore complete session detail that upstream removes from sync payloads."""

    archive_path = state_path / "session-usage-archive.json"
    _secure_collector_file(archive_path, max_bytes=MAX_ARCHIVE_BYTES)
    value = _read_json(archive_path, max_bytes=MAX_ARCHIVE_BYTES)
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"version", "sessions"}:
        raise MonitorError("Token Monitor session archive has an invalid shape")
    archive = value["sessions"]
    if not isinstance(archive, dict) or len(archive) > 200000:
        raise MonitorError("Token Monitor session archive is invalid")
    windows = payload.get("periodWindows") if isinstance(payload.get("periodWindows"), dict) else {}
    today_key = str((windows.get("today") or {}).get("key", "")) if isinstance(windows.get("today"), dict) else ""
    month_key = str((windows.get("month") or {}).get("key", "")) if isinstance(windows.get("month"), dict) else ""
    sessions: dict[str, dict[str, Any]] = {"today": {}, "month": {}, "allTime": {}}
    for key, entry in archive.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise MonitorError("Token Monitor session archive entry is invalid")
        periods = entry.get("periods")
        if not isinstance(periods, dict):
            raise MonitorError("Token Monitor session archive periods are invalid")
        entry_windows = entry.get("periodWindows")
        for period_name in ("today", "month", "allTime"):
            session = periods.get(period_name)
            if not isinstance(session, dict):
                continue
            # Repricing can refresh the shared entry day while retaining the
            # original window marker. Use that per-period marker first, then
            # fall back to the shared fields written by older archives.
            period_window = (
                entry_windows.get(period_name)
                if isinstance(entry_windows, dict)
                and isinstance(entry_windows.get(period_name), dict)
                else {}
            )
            if period_name == "today" and today_key:
                entry_day = period_window.get("day")
                if not isinstance(entry_day, str) or not entry_day:
                    entry_day = entry.get("day")
                if entry_day != today_key:
                    continue
            if period_name == "month" and month_key:
                entry_month = period_window.get("month")
                if not isinstance(entry_month, str) or not entry_month:
                    entry_month = entry.get("month")
                if entry_month != month_key:
                    continue
            cleaned = dict(session)
            cleaned.pop("projectId", None)
            cleaned.pop("projectLabel", None)
            cleaned["projectId"] = ""
            cleaned["projectLabel"] = ""
            sessions[period_name][key] = cleaned
    for period_name, values in sessions.items():
        period = payload.get(period_name)
        if isinstance(period, dict):
            period["sessions"] = values
    payload.pop("sessionDetailsOmitted", None)


def _run_collector(
    docker: str,
    image: str,
    record: VolumeRegistration,
    config_root: Path,
    *,
    uid: int,
    gid: int,
) -> dict[str, Any]:
    root = monitor_root(config_root)
    run_root = root / RUN_DIR
    _ensure_private_directory(run_root)
    state_path = _project_state_path(config_root, record)
    _write_tokscale_pricing(config_root, state_path)
    output_descriptor, output_name = tempfile.mkstemp(prefix="collector-", suffix=".json", dir=run_root)
    os.close(output_descriptor)
    output_path = Path(output_name)
    output_path.unlink(missing_ok=True)
    output_path.touch(mode=0o600)
    os.chmod(output_path, 0o600)
    mounts = []
    for subpath, destination in (("sessions", "/scan/codex/sessions"), ("archived_sessions", "/scan/codex/archived_sessions")):
        if _subpath_available(docker, image, record.volume_name, subpath):
            mounts.extend(("--mount", f"type=volume,src={record.volume_name},dst={destination},readonly,volume-subpath={subpath},volume-nocopy"))
    mounts.extend(("--mount", f"type=bind,src={state_path},dst=/state", "--mount", f"type=bind,src={output_path},dst=/out/summary.json"))
    command = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        COLLECTOR_MEMORY,
        "--cpus",
        COLLECTOR_CPUS,
        "--pids-limit",
        COLLECTOR_PIDS,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/run:rw,noexec,nosuid,nodev,size=8m",
        "--tmpfs",
        f"/scan/codex:rw,noexec,nosuid,nodev,size=32m,uid={uid},gid={gid},mode=700",
        *mounts,
        "-e",
        "CODEX_HOME=/scan/codex",
        "-e",
        "TOKEN_MONITOR_SHARED_DIR=/state",
        "-e",
        "TOKSCALE_CONFIG_DIR=/state/tokscale",
        "-e",
        "TOKEN_MONITOR_CLIENTS=codex",
        "-e",
        "TOKEN_MONITOR_LIMITS_ENABLED=0",
        "-e",
        # Cage assigns volume projects after cross-volume deduplication.  Keep
        # native project metadata off this boundary because it can contain paths.
        "TOKEN_MONITOR_PROJECTS_ENABLED=0",
        "-e",
        "TOKEN_MONITOR_HISTORY_ENABLED=1",
        "-e",
        "TOKEN_MONITOR_SESSION_USAGE_ARCHIVE_ENABLED=1",
        "-e",
        "TOKEN_MONITOR_OPENCODE_AMBIENT=0",
        "-e",
        "TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS=0",
        "-e",
        "TOKEN_MONITOR_WSL_SCAN=0",
        "-e",
        "TOKEN_MONITOR_WATCH=0",
        "-e",
        f"TOKEN_MONITOR_DEVICE_ID={record.device_id}",
        "-e",
        f"CAGE_MONITOR_DISPLAY_NAME={record.display_name}",
        "-e",
        "CAGE_MONITOR_OUTPUT=/out/summary.json",
        image,
    ]
    try:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=SCAN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise MonitorError("Token Monitor collector timed out") from exc
        except OSError as exc:
            raise MonitorError(f"Token Monitor collector could not start: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().replace("\n", " ")[:300]
            raise MonitorError(f"Token Monitor collector failed: {detail or 'unknown error'}")
        if output_path.stat().st_size > MAX_OUTPUT_BYTES:
            raise MonitorError("Token Monitor collector output is too large")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MonitorError(f"Token Monitor collector output is invalid: {exc}") from exc
        _archive_sessions_for_payload(state_path, payload)
        return _validate_summary(payload, record.device_id)
    finally:
        output_path.unlink(missing_ok=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise MonitorError("Token Monitor hub redirect refused")


def _hub_request(connection: MonitorConnection, method: str, path: str, body: bytes | None = None) -> object:
    url = connection.hub_url + path
    request = Request(url, data=body, method=method, headers={
        "accept": "application/json",
        "authorization": f"Bearer {connection.secret}",
        **({"content-type": "application/json"} if body is not None else {}),
    })
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_OUTPUT_BYTES + 1)
    except HTTPError as exc:
        # Do not persist or display an attacker-controlled response body.  A
        # hub can reflect credentials or other request material in an error,
        # and scan errors are retained in the local registry.
        exc.close()
        raise MonitorError(f"Token Monitor hub returned HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise MonitorError(f"Token Monitor hub request failed: {exc.reason if isinstance(exc, URLError) else exc}") from exc
    if len(raw) > MAX_OUTPUT_BYTES:
        raise MonitorError("Token Monitor hub response is too large")
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonitorError("Token Monitor hub returned invalid JSON") from exc


def verify_connection(connection: MonitorConnection) -> None:
    health = _hub_request(replace(connection, secret="unused"), "GET", "/api/health")
    if not isinstance(health, dict) or health.get("ok") is not True or health.get("role") not in {"hub", "worker"}:
        raise MonitorError("configured URL is not a Token Monitor hub")
    stats = _hub_request(connection, "GET", "/api/stats")
    if (
        not isinstance(stats, dict)
        or not isinstance(stats.get("devices"), list)
        or not isinstance(stats.get("periods"), dict)
    ):
        raise MonitorError("Token Monitor hub authentication check failed")


def upload_summary(connection: MonitorConnection, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_OUTPUT_BYTES:
        raise MonitorError("Token Monitor ingest payload is too large")
    _hub_request(connection, "POST", "/api/ingest", body)


def _generation_root(config_root: Path) -> Path:
    path = monitor_root(config_root) / GENERATION_DIR
    _ensure_private_directory(path)
    return path


def _validate_generation_id(value: object) -> str:
    if not isinstance(value, str) or not GENERATION_ID_PATTERN.fullmatch(value):
        raise MonitorError("monitor upload generation is invalid")
    return value


def _generation_manifest_path(config_root: Path, generation: str) -> Path:
    return _generation_root(config_root) / _validate_generation_id(generation) / "generation.json"


def _validate_upload_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "state",
        "generation",
        "previous_generation",
        "provider_ids",
        "attempted",
        "created_at",
        "last_error",
    }:
        raise MonitorError("monitor upload state has an invalid shape")
    if value["version"] != UPLOAD_STATE_VERSION:
        raise MonitorError("monitor upload state has an invalid version")
    if value["state"] not in {"pending", "repair_pending"}:
        raise MonitorError("monitor upload state has an invalid status")
    _validate_generation_id(value["generation"])
    previous = value["previous_generation"]
    if previous:
        _validate_generation_id(previous)
    provider_ids = value["provider_ids"]
    if not isinstance(provider_ids, dict) or len(provider_ids) > 1024:
        raise MonitorError("monitor upload provider identities are invalid")
    for raw_provider, device_id in provider_ids.items():
        provider = _provider_slug(raw_provider)
        if provider != raw_provider:
            raise MonitorError("monitor upload provider identity is invalid")
        validate_device_id(device_id)
    attempted = value["attempted"]
    if not isinstance(attempted, list) or len(attempted) > len(provider_ids) or any(
        not isinstance(item, str) for item in attempted
    ):
        raise MonitorError("monitor upload attempt list is invalid")
    if any(item not in provider_ids or item in attempted[:index] for index, item in enumerate(attempted)):
        raise MonitorError("monitor upload attempt list is invalid")
    for key in ("created_at", "last_error"):
        if not isinstance(value[key], str) or len(value[key]) > 512:
            raise MonitorError("monitor upload state text is invalid")
    return dict(value)


def load_upload_state(config_root: Path) -> dict[str, Any] | None:
    value = _read_json(
        monitor_root(config_root) / UPLOAD_STATE_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    return _validate_upload_state(value)


def save_upload_state(config_root: Path, value: dict[str, Any]) -> None:
    _write_json(
        monitor_root(config_root) / UPLOAD_STATE_FILE,
        _validate_upload_state(value),
    )


def remove_upload_state(config_root: Path) -> None:
    _remove_private_file(
        monitor_root(config_root) / UPLOAD_STATE_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )


def _generation_directory(config_root: Path, generation: str) -> Path:
    return _generation_root(config_root) / _validate_generation_id(generation)


def _validate_exact_provider_ids(
    config_root: Path,
    provider_ids: dict[str, str],
) -> None:
    if not isinstance(provider_ids, dict):
        raise MonitorError("monitor provider device map is invalid")
    for raw_provider, device_id in provider_ids.items():
        provider = _provider_slug(raw_provider)
        if provider != raw_provider:
            raise MonitorError("monitor provider identity is invalid")
        expected = provider_device_id(config_root, provider)
        if device_id != expected:
            raise MonitorError(
                "monitor provider device identity was invalid; hub snapshot was preserved"
            )


def _write_generation_payloads(
    config_root: Path,
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> str:
    generation = secrets.token_hex(16)
    directory = _generation_directory(config_root, generation)
    _ensure_private_directory(directory)
    provider_ids: dict[str, str] = {}
    for provider, (payload, _status) in sorted(payloads.items()):
        normalized = _provider_slug(provider)
        if normalized != provider:
            raise MonitorError("monitor provider identity is invalid")
        device_id = provider_device_id(config_root, provider)
        _validate_summary(payload, device_id)
        provider_ids[provider] = device_id
        _write_json(directory / f"{provider}.json", payload)
    _write_json(
        directory / "generation.json",
        {
            "version": UPLOAD_STATE_VERSION,
            "generation": generation,
            "providers": {
                provider: {"device_id": device_id}
                for provider, device_id in sorted(provider_ids.items())
            },
        },
    )
    return generation


def _load_generation_payloads(
    config_root: Path,
    generation: str,
) -> dict[str, dict[str, Any]]:
    generation = _validate_generation_id(generation)
    value = _read_json(
        _generation_manifest_path(config_root, generation),
        max_bytes=MAX_CONNECTION_BYTES,
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "generation", "providers"}
        or value["version"] != UPLOAD_STATE_VERSION
        or value["generation"] != generation
        or not isinstance(value["providers"], dict)
    ):
        raise MonitorError("monitor upload generation manifest is invalid")
    provider_entries = value["providers"]
    if len(provider_entries) > 1024:
        raise MonitorError("monitor upload generation has too many providers")
    provider_ids: dict[str, str] = {}
    for raw_provider, entry in provider_entries.items():
        provider = _provider_slug(raw_provider)
        if provider != raw_provider or not isinstance(entry, dict) or set(entry) != {"device_id"}:
            raise MonitorError("monitor upload generation provider is invalid")
        device_id = entry["device_id"]
        validate_device_id(device_id)
        provider_ids[provider] = device_id
    _validate_exact_provider_ids(config_root, provider_ids)
    result: dict[str, dict[str, Any]] = {}
    directory = _generation_directory(config_root, generation)
    for provider, device_id in sorted(provider_ids.items()):
        payload = _read_json(directory / f"{provider}.json", max_bytes=MAX_OUTPUT_BYTES)
        result[provider] = _validate_summary(payload, device_id)
    return result


def _previous_generation(
    config_root: Path,
    previous_status: dict[str, Any] | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not isinstance(previous_status, dict):
        return "", {}
    generation = previous_status.get("last_good_generation") or previous_status.get("generation") or ""
    if not generation:
        return "", {}
    generation = _validate_generation_id(generation)
    return generation, _load_generation_payloads(config_root, generation)


def _upload_state_for_generation(
    *,
    generation: str,
    previous_generation: str,
    provider_ids: dict[str, str],
    attempted: list[str],
    state: str,
    last_error: str = "",
) -> dict[str, Any]:
    return {
        "version": UPLOAD_STATE_VERSION,
        "state": state,
        "generation": generation,
        "previous_generation": previous_generation,
        "provider_ids": dict(sorted(provider_ids.items())),
        "attempted": sorted(set(attempted)),
        "created_at": _now(),
        "last_error": " ".join(last_error.split())[:512],
    }


def _repair_pending_upload(
    config_root: Path,
    connection: MonitorConnection,
) -> None:
    pending = load_upload_state(config_root)
    if pending is None:
        return
    provider_ids = pending["provider_ids"]
    _validate_exact_provider_ids(config_root, provider_ids)
    previous_generation = pending["previous_generation"]
    previous = (
        _load_generation_payloads(config_root, previous_generation)
        if previous_generation
        else {}
    )
    try:
        for provider in sorted(pending["attempted"]):
            # A new provider has no last-good payload.  Leave that exact device
            # untouched; the complete next generation below will repair it.
            payload = previous.get(provider)
            if payload is not None:
                upload_summary(connection, payload)
    except Exception as exc:
        pending["state"] = "repair_pending"
        pending["last_error"] = " ".join(str(exc).split())[:512]
        save_upload_state(config_root, pending)
        raise MonitorError(
            "provider upload repair failed; the last-good local snapshot was preserved"
        ) from exc
    remove_upload_state(config_root)


def _rollback_attempted_uploads(
    config_root: Path,
    connection: MonitorConnection,
    attempted: list[str],
    previous: dict[str, dict[str, Any]],
) -> bool:
    reversible = True
    for provider in sorted(set(attempted)):
        payload = previous.get(provider)
        if payload is None:
            # The provider may have been introduced in this generation.  Do
            # not delete or zero its device; retain a repair marker so the
            # next complete generation explicitly rewrites it.
            reversible = False
            continue
        upload_summary(connection, payload)
    return reversible


def _prune_generations(
    config_root: Path,
    *,
    current_generation: str,
    previous_generation: str,
) -> None:
    root = _generation_root(config_root)
    keep = {item for item in (current_generation, previous_generation) if item}
    candidates: list[str] = []
    for entry in root.iterdir():
        try:
            info = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        if info.st_uid != os.getuid() or not GENERATION_ID_PATTERN.fullmatch(entry.name):
            continue
        candidates.append(entry.name)
    for generation in sorted(candidates, reverse=True):
        if len(keep) >= MAX_GENERATIONS:
            break
        keep.add(generation)
    for generation in candidates:
        if generation not in keep:
            _remove_owned_directory(
                root / generation,
                description="monitor upload generation",
            )


def _publish_provider_payloads(
    config_root: Path,
    connection: MonitorConnection,
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    status: dict[str, Any],
    previous_status: dict[str, Any] | None,
) -> dict[str, Any]:
    """Publish one generation and repair any interrupted older generation.

    Token Monitor v0.49.0 has one-device ingest rather than a transaction.  A
    private prepared generation and exact-device rollback make the best
    available behavior deterministic while keeping the last-good aggregate
    status authoritative locally.
    """

    _repair_pending_upload(config_root, connection)
    previous_generation, previous = _previous_generation(config_root, previous_status)
    generation = _write_generation_payloads(config_root, payloads)
    provider_ids = {
        provider: provider_device_id(config_root, provider)
        for provider in sorted(payloads)
    }
    _validate_exact_provider_ids(config_root, provider_ids)
    attempted: list[str] = []
    save_upload_state(
        config_root,
        _upload_state_for_generation(
            generation=generation,
            previous_generation=previous_generation,
            provider_ids=provider_ids,
            attempted=attempted,
            state="pending",
        ),
    )
    try:
        for provider in sorted(payloads):
            attempted.append(provider)
            save_upload_state(
                config_root,
                _upload_state_for_generation(
                    generation=generation,
                    previous_generation=previous_generation,
                    provider_ids=provider_ids,
                    attempted=attempted,
                    state="pending",
                ),
            )
            upload_summary(connection, payloads[provider][0])
    except Exception as exc:
        failure = exc
        save_upload_state(
            config_root,
            _upload_state_for_generation(
                generation=generation,
                previous_generation=previous_generation,
                provider_ids=provider_ids,
                attempted=attempted,
                state="repair_pending",
                last_error=str(exc),
            ),
        )
        try:
            rollback_complete = _rollback_attempted_uploads(
                config_root, connection, attempted, previous
            )
        except Exception as rollback_error:
            pending = load_upload_state(config_root) or {}
            if pending:
                pending["state"] = "repair_pending"
                pending["last_error"] = (
                    "provider upload failed and exact-device rollback is pending: "
                    + " ".join(str(rollback_error).split())
                )[:512]
                save_upload_state(config_root, pending)
            raise MonitorError(
                "provider upload failed; exact-device repair is pending and the last-good snapshot was preserved"
            ) from failure
        if not rollback_complete:
            pending = load_upload_state(config_root)
            if pending is not None:
                pending["last_error"] = (
                    "provider upload failed; a new provider device needs a complete next generation"
                )[:512]
                save_upload_state(config_root, pending)
            raise MonitorError(
                "provider upload failed; complete-generation repair is pending and the last-good snapshot was preserved"
            ) from failure
        remove_upload_state(config_root)
        raise failure

    next_status = dict(status)
    next_status["generation"] = generation
    next_status["last_good_generation"] = generation
    next_status["upload_state"] = "complete"
    _write_json(monitor_root(config_root) / AGGREGATE_STATUS_FILE, next_status)
    remove_upload_state(config_root)
    _prune_generations(
        config_root,
        current_generation=generation,
        previous_generation=previous_generation,
    )
    return next_status


def delete_device(connection: MonitorConnection, device_id: str) -> None:
    validate_device_id(device_id)
    from urllib.parse import quote

    _hub_request(connection, "DELETE", "/api/devices/" + quote(device_id, safe=""))


SESSION_NUMBER_FIELDS = (
    "totalTokens",
    "costUsd",
    "messageCount",
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
)
SESSION_MAP_FIELDS = ("models", "modelCosts", "providers")


def _session_number(session: dict[str, Any], field: str) -> float:
    value = session.get(field, 0)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise MonitorError(f"collector session has invalid {field}")
    return float(value)


def _session_map(session: dict[str, Any], field: str) -> dict[str, float]:
    raw = session.get(field, {})
    if not isinstance(raw, dict):
        raise MonitorError(f"collector session has invalid {field}")
    result: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise MonitorError(f"collector session has invalid {field}")
        result[key] = float(value)
    return result


def session_provider(session: dict[str, Any]) -> str:
    """Return the only trustworthy provider for a session.

    Token Monitor records provider totals in a session-level map.  One key is
    safe to attribute.  Missing, unsafe, or multi-provider maps stay in the
    explicit unattributed stream so Cage never guesses or duplicates tokens.
    """

    providers = _session_map(session, "providers")
    normalized = {_provider_slug(key) for key in providers}
    if len(normalized) == 1 and None not in normalized:
        provider = next(iter(normalized))
        if provider:
            return provider
    return UNATTRIBUTED_PROVIDER


def provider_device_ids(config_root: Path, providers: Iterator[str] | list[str] | set[str]) -> list[str]:
    return [provider_device_id(config_root, provider) for provider in sorted(set(providers))]


def _session_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in SESSION_NUMBER_FIELDS:
        if _session_number(left, field) < _session_number(right, field):
            return False
    for field in SESSION_MAP_FIELDS:
        left_map = _session_map(left, field)
        right_map = _session_map(right, field)
        if any(left_map.get(key, 0) < value for key, value in right_map.items()):
            return False
    return True


def _select_session(candidates: list[tuple[VolumeRegistration, dict[str, Any]]]) -> dict[str, Any]:
    winner = candidates[0][1]
    for _, candidate in candidates[1:]:
        candidate_dominates = _session_dominates(candidate, winner)
        winner_dominates = _session_dominates(winner, candidate)
        if candidate_dominates and not winner_dominates:
            winner = candidate
        elif not candidate_dominates and not winner_dominates:
            session_id = str(candidate.get("sessionId") or "unknown")[:128]
            raise MonitorError(
                f"conflicting copies of Codex session {session_id}; hub snapshot was preserved"
            )
    return dict(winner)


def _empty_aggregate_period() -> dict[str, Any]:
    return {
        "capabilities": {"tokenComponents": True},
        "totalTokens": 0,
        "costUsd": 0.0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "outputTokens": 0,
        "unclassifiedTokens": 0,
        "timedTokens": 0,
        "timedOutputTokens": 0,
        "timedDurationMs": 0,
        "clients": {},
        "clientCosts": {},
        "clientCacheReads": {},
        "clientCacheWrites": {},
        "clientOutputs": {},
        "clientUnclassifiedTokens": {},
        "models": {},
        "modelCosts": {},
        "modelCacheReads": {},
        "modelCacheWrites": {},
        "modelOutputs": {},
        "modelUnclassifiedTokens": {},
        "clientModels": {},
        "clientModelCosts": {},
        "projects": {},
        "sessions": {},
    }


def _add_map(target: dict[str, float], values: dict[str, float]) -> None:
    for key, value in values.items():
        target[key] = target.get(key, 0) + value


def _project_for_candidates(
    config_root: Path,
    candidates: list[tuple[VolumeRegistration, dict[str, Any]]],
) -> tuple[str, str]:
    logical_ids = {record.logical_id for record, _ in candidates}
    if len(logical_ids) == 1:
        record = candidates[0][0]
        return project_id_for(config_root, record.logical_id), record.display_name
    return "cage-project-unattributed", "Cage: Unattributed"


def _custom_session_cost(
    session: dict[str, Any],
    pricing: dict[str, dict[str, float]],
) -> tuple[str, float, int] | None:
    """Price an exactly attributable single-model session from its components."""

    provider = session_provider(session)
    if provider == UNATTRIBUTED_PROVIDER:
        return None
    models = _session_map(session, "models")
    total = round(_session_number(session, "totalTokens"))
    if len(models) != 1:
        return None
    model, model_tokens = next(iter(models.items()))
    # Provider-qualified prices take precedence.  The old model-only form is
    # retained only for an unambiguous OpenAI session.  Applying a legacy rate
    # to a proxy provider could produce a plausible but wrong cost.
    rates = pricing.get(f"{provider}:{model}")
    if rates is None and provider == "openai-api":
        rates = pricing.get(model)
    if rates is None or round(model_tokens) != total:
        return None
    input_tokens = round(_session_number(session, "inputTokens"))
    output_tokens = round(_session_number(session, "outputTokens"))
    cache_read = round(_session_number(session, "cacheReadTokens"))
    cache_write = round(_session_number(session, "cacheWriteTokens"))
    cost = 0.0
    covered = 0
    input_rate = rates.get("input_per_million")
    output_rate = rates.get("output_per_million")
    cache_rate = rates.get("cache_read_per_million", input_rate)
    if input_rate is not None:
        cost += (input_tokens + cache_write) * input_rate / 1_000_000
        covered += input_tokens + cache_write
    if output_rate is not None:
        cost += output_tokens * output_rate / 1_000_000
        covered += output_tokens
    if cache_rate is not None:
        cost += cache_read * cache_rate / 1_000_000
        covered += cache_read
    return model, round(cost, 9), min(total, covered)


def _authoritative_model_costs(session: dict[str, Any]) -> bool:
    """Return whether model costs cover every model without allocation guesses."""

    models = _session_map(session, "models")
    model_costs = _session_map(session, "modelCosts")
    if not models or set(models) != set(model_costs):
        return False
    session_cost = _session_number(session, "costUsd")
    model_cost = sum(model_costs.values())
    tolerance = max(1e-9, abs(session_cost) * 1e-6)
    return abs(model_cost - session_cost) <= tolerance


def _period_from_sessions(
    config_root: Path,
    occurrences: dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]],
    *,
    winners: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    period = _empty_aggregate_period()
    pricing = load_pricing(config_root)
    duplicates = 0
    for key in sorted(occurrences):
        candidates = occurrences[key]
        duplicates += max(0, len(candidates) - 1)
        session = dict(winners[key]) if winners is not None and key in winners else _select_session(candidates)
        provider = session_provider(session)
        custom_cost = _custom_session_cost(session, pricing)
        if custom_cost is not None:
            custom_model, custom_value, _ = custom_cost
            session["costUsd"] = custom_value
            session["modelCosts"] = {custom_model: custom_value}
        elif provider != "openai-api":
            # Tokscale's model catalog has no trustworthy account context.  A
            # proxy may report the same model name while charging a different
            # rate, so never carry an unqualified upstream cost into a
            # non-OpenAI stream.
            session["costUsd"] = 0.0
            session["modelCosts"] = {}
        elif len(_session_map(session, "models")) > 1 and not _authoritative_model_costs(session):
            # An aggregate OpenAI cost cannot be allocated across multiple
            # models.  Keep the token counts, but do not present an estimated
            # price or fabricate component/model allocation.
            session["costUsd"] = 0.0
            session["modelCosts"] = {}
        client = str(session.get("client") or "")
        session_id = str(session.get("sessionId") or "")
        if client != "codex" or not session_id:
            raise MonitorError("collector session identity is invalid")
        project_id, project_label = _project_for_candidates(config_root, candidates)
        session["projectId"] = project_id
        session["projectLabel"] = project_label
        total = round(_session_number(session, "totalTokens"))
        cost = _session_number(session, "costUsd")
        cache_read = round(_session_number(session, "cacheReadTokens"))
        cache_write = round(_session_number(session, "cacheWriteTokens"))
        output = round(_session_number(session, "outputTokens"))
        unclassified = max(0, total - min(total, cache_read + cache_write + output))
        models = _session_map(session, "models")
        model_costs = _session_map(session, "modelCosts")
        period["totalTokens"] += total
        period["costUsd"] += cost
        period["cacheReadTokens"] += cache_read
        period["cacheWriteTokens"] += cache_write
        period["outputTokens"] += output
        period["unclassifiedTokens"] += unclassified
        period["clients"][client] = period["clients"].get(client, 0) + total
        period["clientCosts"][client] = period["clientCosts"].get(client, 0) + cost
        period["clientCacheReads"][client] = period["clientCacheReads"].get(client, 0) + cache_read
        period["clientCacheWrites"][client] = period["clientCacheWrites"].get(client, 0) + cache_write
        period["clientOutputs"][client] = period["clientOutputs"].get(client, 0) + output
        period["clientUnclassifiedTokens"][client] = period["clientUnclassifiedTokens"].get(client, 0) + unclassified
        _add_map(period["models"], models)
        _add_map(period["modelCosts"], model_costs)
        period["clientModels"].setdefault(client, {})
        period["clientModelCosts"].setdefault(client, {})
        _add_map(period["clientModels"][client], models)
        _add_map(period["clientModelCosts"][client], model_costs)
        for model, tokens in models.items():
            period["modelUnclassifiedTokens"][model] = period["modelUnclassifiedTokens"].get(model, 0) + tokens
        project = period["projects"].setdefault(
            project_id,
            {"label": project_label, "tokens": 0, "costUsd": 0.0, "clients": {}},
        )
        project["tokens"] += total
        project["costUsd"] += cost
        project["clients"][client] = project["clients"].get(client, 0) + total
        period["sessions"][key] = session
    period["costUsd"] = round(period["costUsd"], 9)
    for costs in (period["clientCosts"], period["modelCosts"]):
        for key, value in costs.items():
            costs[key] = round(value, 9)
    for costs in period["clientModelCosts"].values():
        for key, value in costs.items():
            costs[key] = round(value, 9)
    for project in period["projects"].values():
        project["costUsd"] = round(project["costUsd"], 9)
    return period, duplicates


def _collect_occurrences(
    summaries: list[tuple[VolumeRegistration, dict[str, Any]]],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]],
]:
    """Validate collector results and index all session copies by ID."""

    if not summaries:
        raise MonitorError("no active Token Monitor projects")
    occurrences: dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]] = {
        name: {} for name in ("today", "month", "allTime")
    }
    first_windows = summaries[0][1].get("periodWindows")
    for record, payload in summaries:
        if payload.get("periodWindows") != first_windows:
            raise MonitorError("collector period windows changed during the aggregate scan")
        if payload.get("sessionDetailsOmitted"):
            raise MonitorError("collector omitted session details; hub snapshot was preserved")
        for period_name in occurrences:
            period = payload.get(period_name)
            sessions = period.get("sessions") if isinstance(period, dict) else None
            # Token Monitor omits the sessions object for an empty period.  A
            # newly registered Cage volume commonly has no Codex session yet,
            # so accept that representation only when its explicit token total
            # is zero.  A non-empty period still requires complete details.
            if isinstance(period, dict) and "sessions" not in period:
                total = period.get("totalTokens")
                if (
                    type(total) in (int, float)
                    and math.isfinite(total)
                    and total == 0
                ):
                    sessions = {}
            if not isinstance(sessions, dict):
                raise MonitorError("collector did not provide complete session details")
            session_total = 0
            for key, session in sessions.items():
                if not isinstance(key, str) or not isinstance(session, dict):
                    raise MonitorError("collector session detail is invalid")
                expected_key = f"{session.get('client', '')}:{session.get('sessionId', '')}"
                if key != expected_key:
                    raise MonitorError("collector session key is invalid")
                session_total += round(_session_number(session, "totalTokens"))
                occurrences[period_name].setdefault(key, []).append((record, session))
            expected = round(float(period.get("totalTokens", 0)))
            if session_total != expected:
                raise MonitorError(
                    f"collector {period_name} sessions do not cover its token total; hub snapshot was preserved"
                )
    return summaries[0][1], occurrences


def _build_device_payload(
    config_root: Path,
    first: dict[str, Any],
    summaries_count: int,
    occurrences: dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]],
    device_id: str,
    *,
    provider: str = "",
    winners_by_period: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    periods: dict[str, dict[str, Any]] = {}
    duplicate_counts: dict[str, int] = {}
    for period_name, values in occurrences.items():
        winners = winners_by_period.get(period_name) if winners_by_period else None
        periods[period_name], duplicate_counts[period_name] = _period_from_sessions(
            config_root, values, winners=winners
        )
    now = _now()
    payload: dict[str, Any] = {
        "deviceId": device_id,
        "hostname": (
            f"Cage ({provider_display_name(provider)})" if provider else "Cage (local)"
        ),
        "platform": "cage",
        "osName": "Cage",
        "osVersion": "",
        "updatedAt": now,
        "agentVersion": str(first.get("agentVersion") or COLLECTOR_SOURCE_VERSION),
        "agentRuntime": "cage-host-collector",
        "projectsEnabled": True,
        "trackedClients": ["codex"],
        "historyAvailable": False,
        "history": None,
        "limits": {"updatedAt": now, "refreshMs": 0, "providers": []},
        "today": periods["today"],
        "month": periods["month"],
        "allTime": periods["allTime"],
    }
    if isinstance(first.get("periodWindows"), dict):
        payload["periodWindows"] = first["periodWindows"]
    pricing = load_pricing(config_root)
    all_time = periods["allTime"]
    priced_tokens = 0
    missing_models: set[str] = set()
    missing_prices: set[str] = set()
    for session in all_time["sessions"].values():
        models = _session_map(session, "models")
        model_costs = _session_map(session, "modelCosts")
        session_provider_id = session_provider(session)
        custom_cost = _custom_session_cost(session, pricing)
        if custom_cost is not None:
            custom_model, _, covered = custom_cost
            priced_tokens += covered
            if covered < round(_session_number(session, "totalTokens")):
                missing_models.add(custom_model)
                missing_prices.add(f"{session_provider_id}:{custom_model}")
            continue
        if not models:
            if _session_number(session, "costUsd") > 0:
                priced_tokens += round(_session_number(session, "totalTokens"))
            continue
        if _authoritative_model_costs(session):
            priced_tokens += round(_session_number(session, "totalTokens"))
            continue
        if len(models) > 1:
            for model in models:
                missing_models.add(model)
                missing_prices.add(f"{session_provider_id}:{model}")
            continue
        for model, raw_tokens in models.items():
            tokens = round(raw_tokens)
            if model_costs.get(model, 0) > 0:
                priced_tokens += tokens
            else:
                missing_models.add(model)
                missing_prices.add(f"{session_provider_id}:{model}")
    total_tokens = all_time["totalTokens"]
    priced_tokens = min(total_tokens, priced_tokens)
    status = {
        "version": STATE_VERSION,
        "device_id": payload["deviceId"],
        "updated_at": now,
        "project_count": summaries_count,
        "duplicate_sessions": duplicate_counts["allTime"],
        "total_tokens": total_tokens,
        "cost_usd": all_time["costUsd"],
        "priced_tokens": priced_tokens,
        "unpriced_tokens": total_tokens - priced_tokens,
        "price_coverage_percent": round((priced_tokens * 100 / total_tokens) if total_tokens else 100.0, 2),
        "missing_models": sorted(missing_models),
        "missing_prices": sorted(missing_prices),
    }
    if provider:
        status["provider"] = provider
        status["provider_label"] = provider_display_name(provider)
    # Match the upstream wire contract: all-time session detail is local-only.
    # The exact all-time project rollup remains in the upload.
    payload["allTime"].pop("sessions", None)
    budget = MAX_OUTPUT_BYTES - 16 * 1024
    omitted: dict[str, int] = {}
    for period_name in ("month", "today"):
        if len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) <= budget:
            break
        sessions = payload[period_name].pop("sessions", {})
        if sessions:
            omitted[period_name] = len(sessions)
    if omitted:
        payload["sessionDetailsOmitted"] = omitted
    return _validate_summary(payload, payload["deviceId"]), status


def _empty_provider_payload(
    config_root: Path,
    first: dict[str, Any],
    device_id: str,
    provider: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    empty_occurrences = {name: {} for name in ("today", "month", "allTime")}
    return _build_device_payload(
        config_root,
        first,
        0,
        empty_occurrences,
        device_id,
        provider=provider,
        winners_by_period={name: {} for name in empty_occurrences},
    )


def _provider_partitions(
    occurrences: dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]],
) -> tuple[
    dict[str, dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    """Deduplicate first, then assign each winning session to one stream."""

    partitions: dict[str, dict[str, dict[str, list[tuple[VolumeRegistration, dict[str, Any]]]]]] = {}
    winners: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for period_name, values in occurrences.items():
        for key, candidates in values.items():
            winner = _select_session(candidates)
            provider = session_provider(winner)
            partitions.setdefault(provider, {name: {} for name in occurrences})
            winners.setdefault(provider, {name: {} for name in occurrences})
            partitions[provider][period_name][key] = candidates
            winners[provider][period_name][key] = winner
    return partitions, winners


def aggregate_summaries(
    config_root: Path,
    summaries: list[tuple[VolumeRegistration, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the compatibility unsplit aggregate without uploading it."""

    first, occurrences = _collect_occurrences(summaries)
    return _build_device_payload(
        config_root,
        first,
        len(summaries),
        occurrences,
        host_device_id(config_root),
    )


def aggregate_provider_summaries(
    config_root: Path,
    summaries: list[tuple[VolumeRegistration, dict[str, Any]]],
) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Build one hub payload per provider after cross-volume deduplication."""

    first, occurrences = _collect_occurrences(summaries)
    partitions, winners = _provider_partitions(occurrences)
    result: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for provider in sorted(partitions):
        result[provider] = _build_device_payload(
            config_root,
            first,
            len(summaries),
            partitions[provider],
            provider_device_id(config_root, provider),
            provider=provider,
            winners_by_period=winners[provider],
        )
    total_tokens = sum(item[1]["total_tokens"] for item in result.values())
    total_cost = round(sum(item[1]["cost_usd"] for item in result.values()), 9)
    duplicate_sessions = sum(item[1]["duplicate_sessions"] for item in result.values())
    priced_tokens = sum(item[1]["priced_tokens"] for item in result.values())
    missing_models = sorted({model for _, status in result.values() for model in status.get("missing_models", [])})
    missing_prices = sorted({key for _, status in result.values() for key in status.get("missing_prices", [])})
    manifest = {
        "version": STATE_VERSION,
        "device_id": host_device_id(config_root),
        "device_ids": [status["device_id"] for _, status in result.values()],
        "providers": {provider: status for provider, (_, status) in result.items()},
        "project_count": len(summaries),
        "duplicate_sessions": duplicate_sessions,
        "total_tokens": total_tokens,
        "cost_usd": total_cost,
        "priced_tokens": min(total_tokens, priced_tokens),
        "unpriced_tokens": max(0, total_tokens - priced_tokens),
        "price_coverage_percent": round(
            (priced_tokens * 100 / total_tokens) if total_tokens else 100.0, 2
        ),
        "missing_models": missing_models,
        "missing_prices": missing_prices,
        "updated_at": _now(),
    }
    return result, manifest


@contextmanager
def try_aggregate_lock(config_root: Path) -> Iterator[bool]:
    directory = monitor_root(config_root) / LOCK_DIR
    _ensure_private_directory(directory)
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


def load_aggregate_status(config_root: Path) -> dict[str, Any] | None:
    value = _read_json(
        monitor_root(config_root) / AGGREGATE_STATUS_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise MonitorError("monitor aggregate status is invalid")
    return value


def load_split_status(config_root: Path) -> dict[str, Any] | None:
    value = _read_json(
        monitor_root(config_root) / SPLIT_STATUS_FILE,
        max_bytes=MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != SPLIT_STATUS_VERSION:
        raise MonitorError("monitor provider split status is invalid")
    if type(value.get("complete")) is not bool:
        raise MonitorError("monitor provider split status is invalid")
    device_ids = value.get("device_ids", [])
    if not isinstance(device_ids, list) or any(
        not isinstance(item, str) for item in device_ids
    ):
        raise MonitorError("monitor provider split device list is invalid")
    for item in device_ids:
        validate_device_id(item)
    legacy_device_id = value.get("legacy_device_id", "")
    if not isinstance(legacy_device_id, str) or (
        legacy_device_id and validate_device_id(legacy_device_id) != legacy_device_id
    ):
        raise MonitorError("monitor provider split legacy device is invalid")
    updated_at = value.get("updated_at", "")
    if not isinstance(updated_at, str) or len(updated_at) > 128:
        raise MonitorError("monitor provider split timestamp is invalid")
    total_tokens = value.get("total_tokens", 0)
    if (
        type(total_tokens) not in (int, float)
        or not math.isfinite(total_tokens)
        or total_tokens < 0
    ):
        raise MonitorError("monitor provider split total is invalid")
    return value


def save_split_status(config_root: Path, value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or type(value.get("complete")) is not bool:
        raise MonitorError("monitor provider split status is invalid")
    device_ids = value.get("device_ids", [])
    if not isinstance(device_ids, list) or any(
        not isinstance(item, str) for item in device_ids
    ):
        raise MonitorError("monitor provider split device list is invalid")
    for item in device_ids:
        validate_device_id(item)
    legacy_device_id = value.get("legacy_device_id", "")
    if not isinstance(legacy_device_id, str):
        raise MonitorError("monitor provider split legacy device is invalid")
    if legacy_device_id:
        validate_device_id(legacy_device_id)
    updated_at = value.get("updated_at", _now())
    if not isinstance(updated_at, str) or len(updated_at) > 128:
        raise MonitorError("monitor provider split timestamp is invalid")
    total_tokens = value.get("total_tokens", 0)
    if (
        type(total_tokens) not in (int, float)
        or not math.isfinite(total_tokens)
        or total_tokens < 0
    ):
        raise MonitorError("monitor provider split total is invalid")
    _write_json(
        monitor_root(config_root) / SPLIT_STATUS_FILE,
        {
            "version": SPLIT_STATUS_VERSION,
            "complete": value["complete"],
            "legacy_device_id": legacy_device_id,
            "device_ids": sorted(set(device_ids)),
            "updated_at": updated_at,
            "total_tokens": total_tokens,
        },
    )


def _hub_stats(connection: MonitorConnection) -> dict[str, Any]:
    stats = _hub_request(connection, "GET", "/api/stats")
    if not isinstance(stats, dict):
        raise MonitorError("Token Monitor hub returned invalid device statistics")
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise MonitorError("Token Monitor hub returned invalid device statistics")
    return stats


def _hub_device_ids_from_stats(stats: dict[str, Any]) -> set[str]:
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise MonitorError("Token Monitor hub returned invalid device statistics")
    result: set[str] = set()
    for item in devices:
        if not isinstance(item, dict):
            continue
        device_id = item.get("deviceId")
        if isinstance(device_id, str):
            try:
                result.add(validate_device_id(device_id))
            except MonitorError:
                continue
    return result


def _hub_device_ids(connection: MonitorConnection) -> set[str]:
    return _hub_device_ids_from_stats(_hub_stats(connection))


def _hub_device_total_from_stats(stats: dict[str, Any], device_id: str) -> float | None:
    """Return one device's all-time token total from authenticated hub stats."""

    validate_device_id(device_id)
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise MonitorError("Token Monitor hub returned invalid device statistics")
    for item in devices:
        if not isinstance(item, dict) or item.get("deviceId") != device_id:
            continue
        periods = item.get("periods")
        all_time = periods.get("allTime") if isinstance(periods, dict) else None
        total = all_time.get("totalTokens") if isinstance(all_time, dict) else None
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise MonitorError(
                f"Token Monitor hub has no valid all-time total for device {device_id}"
            )
        return float(total)
    return None


def provider_split_pending(config_root: Path, connection: MonitorConnection) -> bool:
    """Return whether the old unsplit device still exists on the hub."""

    state = load_split_status(config_root)
    if state is not None and state.get("complete") is True:
        return False
    return host_device_id(config_root) in _hub_device_ids(connection)


def _mark_split_complete(
    config_root: Path,
    manifest: dict[str, Any],
    *,
    legacy_device_id: str = "",
) -> None:
    save_split_status(
        config_root,
        {
            "complete": True,
            "legacy_device_id": legacy_device_id,
            "device_ids": list(manifest.get("device_ids", [])),
            "updated_at": manifest.get("updated_at", _now()),
            "total_tokens": manifest.get("total_tokens", 0),
        },
    )


def discover_codex_volumes(
    docker: str,
    config_root: Path,
) -> list[dict[str, Any]]:
    """List Cage Codex state volumes without changing Docker or monitor state."""

    try:
        result = subprocess.run(
            [docker, "volume", "ls", "--format", "{{.Name}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MonitorError(f"Docker monitor operation failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().replace("\n", " ")[:300]
        raise MonitorError(f"Docker monitor operation failed: {detail or 'unknown error'}")
    registrations = {item.volume_name: item for item in load_registry(config_root)}
    discovered: list[dict[str, Any]] = []
    names = sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith(CODEX_VOLUME_PREFIX)
        }
    )
    for volume_name in names:
        validate_volume_name(volume_name)
        fingerprint = volume_fingerprint(docker, volume_name)
        record = registrations.get(volume_name)
        discovered.append(
            {
                "volume_name": volume_name,
                "fingerprint": fingerprint,
                "registered": record is not None,
                "status": record.status if record is not None else "unregistered",
                "display_name": record.display_name if record is not None else "",
                "logical_id": record.logical_id if record is not None else "",
                "label_identity": fingerprint.get("label_identity", ""),
            }
        )
    return discovered


def recovered_repository(volume_name: str) -> str:
    validate_volume_name(volume_name)
    return f"/__cage_recovered__/{volume_name}"


def register_recovered_volume(
    config_root: Path,
    docker: str,
    *,
    volume_name: str,
    display_name: str = "",
) -> VolumeRegistration:
    """Adopt an exact dormant volume without inventing a host path."""

    validate_volume_name(volume_name)
    fingerprint = volume_fingerprint(docker, volume_name)
    if not display_name:
        suffix = volume_name.removeprefix(CODEX_VOLUME_PREFIX)
        display_name = f"Cage: Recovered {suffix}"
    validate_display_name(display_name)
    return register_volume(
        config_root,
        docker,
        volume_name=volume_name,
        repository=recovered_repository(volume_name),
        target="container",
        preset="recovered",
        display_name=display_name,
        fingerprint=fingerprint,
        allow_replacement=True,
    )


def _scan_is_recent(config_root: Path) -> bool:
    status = load_aggregate_status(config_root)
    if not status or not isinstance(status.get("updated_at"), str):
        return False
    try:
        timestamp = datetime.fromisoformat(status["updated_at"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return time.time() - timestamp < MIN_INTERVAL_SECONDS


def _mark_volume_fingerprint_conflict(
    config_root: Path,
    record: VolumeRegistration,
) -> None:
    updated = replace(
        record,
        status="needs-adoption",
        last_error="volume fingerprint changed; run cage monitor add explicitly",
        last_scan_at=_now(),
    )
    update_registration(config_root, updated)


def _checked_volume_fingerprint(
    config_root: Path,
    docker: str,
    record: VolumeRegistration,
) -> dict[str, str]:
    current = volume_fingerprint(docker, record.volume_name)
    if current != record.fingerprint:
        _mark_volume_fingerprint_conflict(config_root, record)
        raise MonitorError(f"monitor volume changed for {record.display_name}")
    return current


def _collect_current_registration(
    config_root: Path,
    docker: str,
    install_root: Path,
    record: VolumeRegistration,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    interval_seconds: int,
    force: bool,
) -> tuple[VolumeRegistration, dict[str, Any], bool, bool]:
    """Refresh exactly one volume and return payload/content/metadata changes."""

    with _wait_for_volume_lock(config_root, record.logical_id):
        _checked_volume_fingerprint(config_root, docker, record)
        previous, previous_metadata_changed = _load_trusted_volume_snapshot(
            config_root, record
        )
        if (
            not force
            and previous is not None
            and _snapshot_is_recent(
                config_root,
                record,
                max_age_seconds=interval_seconds,
            )
        ):
            if previous_metadata_changed:
                # Promotion changes only the private display metadata.  Keep
                # the cached usage content and bring its identity metadata
                # forward so every later launch does not republish forever.
                _save_volume_snapshot(config_root, record, previous)
            return record, previous, False, previous_metadata_changed
        image = ensure_collector_image(
            docker,
            install_root,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
        )
        payload = _run_collector(
            docker,
            image,
            record,
            config_root,
            uid=os.getuid() if uid is None else uid,
            gid=os.getgid() if gid is None else gid,
        )
        content_changed = previous is None or _summary_content_hash(previous) != _summary_content_hash(payload)
        _save_volume_snapshot(config_root, record, payload)
        return record, payload, content_changed, previous_metadata_changed


def _summaries_from_cached_or_collected(
    config_root: Path,
    docker: str,
    install_root: Path,
    active: list[VolumeRegistration],
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    overrides: dict[str, dict[str, Any]] | None = None,
    full: bool = False,
) -> list[tuple[VolumeRegistration, dict[str, Any]]]:
    """Use trusted per-volume state and collect only missing/full inputs."""

    overrides = overrides or {}
    cached: dict[str, dict[str, Any]] = {}
    missing: list[VolumeRegistration] = []
    for record in active:
        _checked_volume_fingerprint(config_root, docker, record)
        override = overrides.get(record.logical_id)
        if override is not None and not full:
            cached[record.logical_id] = _validate_summary(override, record.device_id)
            continue
        if not full:
            payload, _metadata_changed = _load_trusted_volume_snapshot(config_root, record)
            if payload is not None:
                cached[record.logical_id] = payload
                continue
        missing.append(record)
    if missing:
        collected = _collect_registered_summaries(
            config_root,
            docker,
            install_root,
            missing,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
            uid=uid,
            gid=gid,
            overrides=overrides if full else None,
        )
        cached.update({record.logical_id: payload for record, payload in collected})
    if len(cached) != len(active):
        raise MonitorError("monitor aggregate has no trusted snapshot for every active volume")
    return [(record, cached[record.logical_id]) for record in active]


def _collect_registered_summaries(
    config_root: Path,
    docker: str,
    install_root: Path,
    active: list[VolumeRegistration],
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[VolumeRegistration, dict[str, Any]]]:
    overrides = overrides or {}
    to_collect = [record for record in active if record.logical_id not in overrides]
    image = None
    if to_collect:
        image = ensure_collector_image(
            docker,
            install_root,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
        )
    result: list[tuple[VolumeRegistration, dict[str, Any]]] = []
    for record in active:
        with _wait_for_volume_lock(config_root, record.logical_id):
            _checked_volume_fingerprint(config_root, docker, record)
            payload = overrides.get(record.logical_id)
            if payload is None:
                assert image is not None
                payload = _run_collector(
                    docker,
                    image,
                    record,
                    config_root,
                    uid=os.getuid() if uid is None else uid,
                    gid=os.getgid() if gid is None else gid,
                )
            payload = _validate_summary(payload, record.device_id)
            _save_volume_snapshot(config_root, record, payload)
            result.append((record, payload))
    return result


def _add_previous_provider_payloads(
    config_root: Path,
    summaries: list[tuple[VolumeRegistration, dict[str, Any]]],
    split_payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    status: dict[str, Any],
    previous_status: dict[str, Any] | None,
) -> None:
    previous_providers = (
        previous_status.get("providers")
        if isinstance(previous_status, dict)
        and isinstance(previous_status.get("providers"), dict)
        else {}
    )
    current_providers = set(split_payloads)
    for raw_provider, previous_provider_status in previous_providers.items():
        provider = _provider_slug(raw_provider)
        if provider is None or provider in current_providers:
            continue
        if not isinstance(previous_provider_status, dict):
            continue
        previous_device = previous_provider_status.get("device_id")
        expected_device = provider_device_id(config_root, provider)
        if previous_device != expected_device:
            raise MonitorError(
                "previous provider device identity was invalid; hub snapshot was preserved"
            )
        empty_payload, empty_status = _empty_provider_payload(
            config_root,
            summaries[0][1],
            expected_device,
            provider,
        )
        split_payloads[provider] = (empty_payload, empty_status)
        status.setdefault("providers", {})[provider] = empty_status
        status.setdefault("device_ids", []).append(expected_device)
    status["device_ids"] = sorted(set(status.get("device_ids", [])))


def _mark_scan_success(
    config_root: Path,
    active: list[VolumeRegistration],
    success_at: str,
) -> list[VolumeRegistration]:
    active_ids = {item.logical_id for item in active}
    with _registry_write_lock(config_root):
        current = load_registry(config_root)
        updated_all = [
            replace(
                item,
                status="active",
                last_scan_at=success_at,
                last_success_at=success_at,
                last_error="",
            )
            if item.logical_id in active_ids
            else item
            for item in current
        ]
        save_registry(config_root, updated_all)
    return [item for item in updated_all if item.logical_id in active_ids]


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
            else math.floor(now / FULL_RECONCILIATION_INTERVAL_SECONDS)
            * FULL_RECONCILIATION_INTERVAL_SECONDS
        )
    state["full_reconciliation_in_progress"] = {
        "owner": secrets.token_hex(16),
        "scheduled_at": float(scheduled_at),
        "started_at": now,
        "expires_at": now + COORDINATOR_LEASE_SECONDS,
    }
    state["updated_at"] = _now()
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
    next_due = scheduled_at + FULL_RECONCILIATION_INTERVAL_SECONDS
    while next_due <= now:
        next_due += FULL_RECONCILIATION_INTERVAL_SECONDS
    state["next_full_reconciliation_at"] = next_due
    state["last_full_reconciliation_at"] = _now()
    state["last_generation"] = _validate_generation_id(generation)
    state["last_error"] = ""
    state["updated_at"] = _now()
    state["full_reconciliation_in_progress"] = None
    save_scheduler_state(config_root, state)


def _fail_full_reconciliation(
    config_root: Path,
    state: dict[str, Any],
    error: str,
) -> None:
    state["last_error"] = " ".join(error.split())[:512]
    state["updated_at"] = _now()
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
    next_due = float(scheduled_at) + FULL_RECONCILIATION_INTERVAL_SECONDS
    now = time.time()
    while next_due <= now:
        next_due += FULL_RECONCILIATION_INTERVAL_SECONDS
    state["next_full_reconciliation_at"] = next_due
    state["full_reconciliation_in_progress"] = None
    save_scheduler_state(config_root, state)


def preview_provider_split(
    config_root: Path,
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, Any]:
    """Collect and calculate a provider split without hub uploads."""

    registrations = load_registry(config_root)
    active = [item for item in registrations if item.status == "active"]
    if not active:
        raise MonitorError("no active Token Monitor projects")
    with try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            raise MonitorError("monitor aggregate scan already running")
        with try_aggregate_lock(config_root) as acquired:
            if not acquired:
                raise MonitorError("monitor aggregate scan already running")
            summaries = _collect_registered_summaries(
                config_root,
                docker,
                install_root,
                active,
                version=version,
                storage_policy=storage_policy,
                allow_build=allow_build,
                uid=uid,
                gid=gid,
            )
            _payloads, manifest = aggregate_provider_summaries(config_root, summaries)
            return manifest


def scan_all_registrations(
    config_root: Path,
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None = None,
    gid: int | None = None,
    force: bool = False,
    migration: bool = False,
) -> tuple[list[VolumeRegistration], dict[str, Any]]:
    """Force one serialized full reconciliation of every active volume."""

    connection = load_connection(config_root)
    if connection is None or not connection.enabled:
        raise MonitorError("Token Monitor is not connected")
    with try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            raise MonitorError("monitor aggregate scan already running")
        with try_aggregate_lock(config_root) as acquired:
            if not acquired:
                raise MonitorError("monitor aggregate scan already running")
            registrations = load_registry(config_root)
            active = [item for item in registrations if item.status == "active"]
            if not active:
                raise MonitorError("no active Token Monitor projects")
            if not migration and provider_split_pending(config_root, connection):
                raise MonitorError(
                    "provider split migration is pending; run cage monitor migrate --yes"
                )
            if not force and _scan_is_recent(config_root):
                return active, {}
            scheduler = load_scheduler_state(config_root)
            scheduled_at = _begin_full_reconciliation(
                config_root,
                scheduler,
                time.time(),
                forced=force,
            )
            try:
                previous_status = load_aggregate_status(config_root)
                summaries = _collect_registered_summaries(
                    config_root,
                    docker,
                    install_root,
                    active,
                    version=version,
                    storage_policy=storage_policy,
                    allow_build=allow_build,
                    uid=uid,
                    gid=gid,
                )
                split_payloads, status = aggregate_provider_summaries(config_root, summaries)
                _add_previous_provider_payloads(
                    config_root,
                    summaries,
                    split_payloads,
                    status,
                    previous_status,
                )
                status["split_complete"] = False if migration else True
                status = _publish_provider_payloads(
                    config_root,
                    connection,
                    split_payloads,
                    status,
                    previous_status,
                )
                if not migration:
                    _mark_split_complete(config_root, status)
                success_at = _now()
                updated_all = _mark_scan_success(config_root, active, success_at)
                generation = status.get("generation")
                if not isinstance(generation, str):
                    raise MonitorError("monitor upload omitted its generation")
                _finish_full_reconciliation(
                    config_root,
                    scheduler,
                    scheduled_at=scheduled_at,
                    generation=generation,
                    now=time.time(),
                )
                return updated_all, status
            except Exception as exc:
                try:
                    _fail_full_reconciliation(config_root, scheduler, str(exc))
                except MonitorError:
                    pass
                for record in active:
                    _record_scan_error(config_root, record, str(exc))
                if isinstance(exc, MonitorError):
                    raise
                raise MonitorError("Token Monitor full reconciliation failed") from exc


def scan_registration(
    config_root: Path,
    docker: str,
    install_root: Path,
    record: VolumeRegistration,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None = None,
    gid: int | None = None,
    force: bool = False,
    final: bool = False,
) -> tuple[VolumeRegistration, dict[str, Any]]:
    """Refresh one current volume, then merge it with trusted cached volumes.

    A final lifecycle refresh is deliberately current-volume-only: it may
    publish already-trusted peer snapshots, but it never starts the bounded
    host-wide safety reconciliation or collects a missing peer.
    """

    connection = load_connection(config_root)
    if connection is None or not connection.enabled:
        raise MonitorError("Token Monitor is not connected")
    registrations = load_registry(config_root)
    current = next(
        (item for item in registrations if item.logical_id == record.logical_id),
        None,
    )
    if current is None or current.status != "active":
        raise MonitorError("monitor project is not active")
    try:
        refreshed, current_payload, content_changed, metadata_changed = _collect_current_registration(
            config_root,
            docker,
            install_root,
            current,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
            uid=uid,
            gid=gid,
            interval_seconds=connection.interval_seconds,
            force=force,
        )
    except Exception as exc:
        _record_scan_error(config_root, current, str(exc))
        if isinstance(exc, MonitorError):
            raise
        raise MonitorError("Token Monitor current-volume refresh failed") from exc

    with try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            return refreshed, load_aggregate_status(config_root) or {}
        with try_aggregate_lock(config_root) as acquired:
            if not acquired:
                return refreshed, load_aggregate_status(config_root) or {}
            registrations = load_registry(config_root)
            active = [item for item in registrations if item.status == "active"]
            if not active or not any(item.logical_id == refreshed.logical_id for item in active):
                raise MonitorError("monitor project is not active")
            if provider_split_pending(config_root, connection):
                raise MonitorError(
                    "provider split migration is pending; run cage monitor migrate --yes"
                )
            previous_status = load_aggregate_status(config_root)
            cache_complete = True
            for item in active:
                cached, _metadata = _load_trusted_volume_snapshot(config_root, item)
                if cached is None:
                    cache_complete = False
                    break
            if final and not cache_complete:
                # The current volume is safely refreshed above.  Do not turn
                # process shutdown into an all-volume scan merely because a
                # peer has no local snapshot yet; a future coordinator owner
                # can reconcile that peer normally.
                return refreshed, previous_status or {}
            scheduler = load_scheduler_state(config_root)
            now = time.time()
            full_due = (
                not final
                and not force
                and _full_reconciliation_due(scheduler, now)
            )
            scheduled_at: float | None = None
            if full_due:
                scheduled_at = _begin_full_reconciliation(config_root, scheduler, now)
            try:
                overrides = {refreshed.logical_id: current_payload}
                if full_due:
                    summaries = _collect_registered_summaries(
                        config_root,
                        docker,
                        install_root,
                        active,
                        version=version,
                        storage_policy=storage_policy,
                        allow_build=allow_build,
                        uid=uid,
                        gid=gid,
                        overrides=overrides,
                    )
                else:
                    summaries = _summaries_from_cached_or_collected(
                        config_root,
                        docker,
                        install_root,
                        active,
                        version=version,
                        storage_policy=storage_policy,
                        allow_build=allow_build,
                        uid=uid,
                        gid=gid,
                        overrides=overrides,
                    )
                should_publish = bool(
                    force
                    or full_due
                    or content_changed
                    or metadata_changed
                    or not cache_complete
                    or previous_status is None
                    or load_upload_state(config_root) is not None
                )
                if not should_publish:
                    return refreshed, previous_status or {}
                split_payloads, status = aggregate_provider_summaries(config_root, summaries)
                _add_previous_provider_payloads(
                    config_root,
                    summaries,
                    split_payloads,
                    status,
                    previous_status,
                )
                status["split_complete"] = True
                status = _publish_provider_payloads(
                    config_root,
                    connection,
                    split_payloads,
                    status,
                    previous_status,
                )
                _mark_split_complete(config_root, status)
                updated_all = _mark_scan_success(config_root, active, _now())
                result = next(
                    item for item in updated_all if item.logical_id == refreshed.logical_id
                )
                if full_due:
                    generation = status.get("generation")
                    if not isinstance(generation, str) or scheduled_at is None:
                        raise MonitorError("monitor upload omitted its generation")
                    _finish_full_reconciliation(
                        config_root,
                        scheduler,
                        scheduled_at=scheduled_at,
                        generation=generation,
                        now=time.time(),
                    )
                return result, status
            except Exception as exc:
                if full_due:
                    try:
                        _fail_full_reconciliation(config_root, scheduler, str(exc))
                    except MonitorError:
                        pass
                for item in active:
                    _record_scan_error(config_root, item, str(exc))
                if isinstance(exc, MonitorError):
                    raise
                raise MonitorError("Token Monitor aggregate update failed") from exc


def migrate_legacy_devices(
    config_root: Path,
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
) -> int:
    connection = load_connection(config_root)
    if connection is None or not connection.enabled:
        raise MonitorError("Token Monitor is not connected")
    pending = [item for item in load_registry(config_root) if item.legacy_device_id]
    legacy_device = host_device_id(config_root)
    split_state = load_split_status(config_root)
    hub_stats = _hub_stats(connection)
    hub_devices = _hub_device_ids_from_stats(hub_stats)
    split_pending = (
        not (split_state and split_state.get("complete") is True)
        and legacy_device in hub_devices
    )
    old_total = (
        _hub_device_total_from_stats(hub_stats, legacy_device)
        if split_pending
        else None
    )
    if not pending and not split_pending:
        return 0
    _updated, manifest = scan_all_registrations(
        config_root,
        docker,
        install_root,
        version=version,
        storage_policy=storage_policy,
        allow_build=True,
        force=True,
        migration=True,
    )
    visible_stats = _hub_stats(connection)
    visible = _hub_device_ids_from_stats(visible_stats)
    expected_provider_devices = set(manifest.get("device_ids", []))
    if not expected_provider_devices.issubset(visible):
        raise MonitorError(
            "new provider Cage devices were not visible on the hub; legacy devices were preserved"
        )
    for provider, provider_status in manifest.get("providers", {}).items():
        if not isinstance(provider_status, dict):
            raise MonitorError(
                "provider status was invalid; legacy devices were preserved"
            )
        provider_device = provider_status.get("device_id")
        provider_total = provider_status.get("total_tokens")
        if (
            not isinstance(provider_device, str)
            or type(provider_total) not in (int, float)
            or not math.isfinite(provider_total)
            or provider_total < 0
        ):
            raise MonitorError(
                "provider token total was invalid; legacy devices were preserved"
            )
        hub_total = _hub_device_total_from_stats(visible_stats, provider_device)
        if hub_total is None or round(hub_total) != round(float(provider_total)):
            raise MonitorError(
                f"provider device {provider_device} did not reconcile; legacy devices were preserved"
            )
    if split_pending:
        new_total = manifest.get("total_tokens")
        if old_total is None or type(new_total) not in (int, float):
            raise MonitorError(
                "cannot verify the old aggregate token total; legacy device was preserved"
            )
        if round(float(old_total)) != round(float(new_total)):
            raise MonitorError(
                "provider split token total does not match the old aggregate; legacy device was preserved"
            )
        delete_device(connection, legacy_device)
        _mark_split_complete(
            config_root,
            manifest,
            legacy_device_id=legacy_device,
        )
        status = load_aggregate_status(config_root)
        if isinstance(status, dict):
            status["split_complete"] = True
            _write_json(monitor_root(config_root) / AGGREGATE_STATUS_FILE, status)
    deleted = 0
    for pending_record in pending:
        legacy_id = pending_record.legacy_device_id
        delete_device(connection, legacy_id)
        with _registry_write_lock(config_root):
            registrations = load_registry(config_root)
            current = next(
                (item for item in registrations if item.logical_id == pending_record.logical_id),
                None,
            )
            if current is None or current.legacy_device_id != legacy_id:
                raise MonitorError("monitor registry changed during migration")
            save_registry(
                config_root,
                [replace(item, legacy_device_id="") if item.logical_id == current.logical_id else item for item in registrations],
            )
        remove_device_state(config_root, legacy_id)
        deleted += 1
    if split_pending:
        deleted += 1
    return deleted


class ActiveMonitor:
    """Best-effort current-volume scanner with a wall-clock cadence."""

    def __init__(self, scan, interval_seconds: int, final_scan=None):
        self._scan = scan
        self._final_scan = final_scan or scan
        self._interval = validate_interval(interval_seconds)
        self._stop = threading.Event()
        self._final_scan_done = False
        self._thread = threading.Thread(target=self._run, name="cage-token-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Compute the first wall-clock boundary before collection starts so a
        # slow collector cannot shift every later tick by one full interval.
        next_due = (math.floor(time.time() / self._interval) + 1) * self._interval
        try:
            self._scan(False)
        except Exception as exc:  # optional observability must not stop Cage
            print(f"WARNING: Token Monitor scan skipped: {exc}", file=sys.stderr)
        while not self._stop.is_set():
            wait_seconds = max(0.0, next_due - time.time())
            if self._stop.wait(wait_seconds):
                return
            try:
                self._scan(False)
            except Exception as exc:  # optional observability must not stop Cage
                print(f"WARNING: Token Monitor scan skipped: {exc}", file=sys.stderr)
            now = time.time()
            missed = max(1, math.floor((now - next_due) / self._interval) + 1)
            next_due += missed * self._interval

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=SCAN_TIMEOUT_SECONDS + 10)
        if self._final_scan_done:
            return
        self._final_scan_done = True
        try:
            # The lifecycle callback uses scan_registration(final=True): it
            # refreshes only this launch's exact volume, can merge cached peer
            # snapshots, and never calls the all-volume reconciliation path.
            final_scan = getattr(self, "_final_scan", self._scan)
            final_scan(True)
        except Exception as exc:
            print(f"WARNING: final Token Monitor scan skipped: {exc}", file=sys.stderr)


__all__ = [
    "ActiveMonitor",
    "COLLECTOR_DOCKERFILE",
    "COLLECTOR_IMAGE",
    "COLLECTOR_REGISTRY",
    "COLLECTOR_SOURCE_COMMIT",
    "COLLECTOR_SOURCE_SHA256",
    "COLLECTOR_SOURCE_URL",
    "COLLECTOR_SOURCE_VERSION",
    "FULL_RECONCILIATION_INTERVAL_SECONDS",
    "MonitorConnection",
    "MonitorError",
    "VolumeRegistration",
    "collector_image",
    "collector_registry_image",
    "aggregate_summaries",
    "aggregate_provider_summaries",
    "clear_legacy_device_id",
    "delete_device",
    "device_id_for",
    "disable_all_registrations",
    "disable_connection",
    "ensure_codex_volume",
    "ensure_codex_volume_labels",
    "ensure_collector_image",
    "load_connection",
    "load_aggregate_status",
    "load_scheduler_state",
    "load_upload_state",
    "load_volume_snapshot",
    "load_pricing",
    "load_registry",
    "load_split_status",
    "logical_target_id",
    "monitor_root",
    "host_device_id",
    "provider_device_id",
    "provider_device_ids",
    "provider_display_name",
    "provider_split_pending",
    "preview_provider_split",
    "normalize_hub_url",
    "register_volume",
    "register_recovered_volume",
    "discover_codex_volumes",
    "recovered_repository",
    "remove_device_state",
    "remove_model_pricing",
    "remove_project_state",
    "retire_registration",
    "save_connection",
    "save_pricing",
    "save_registry",
    "save_scheduler_state",
    "save_split_status",
    "save_upload_state",
    "scan_registration",
    "scan_all_registrations",
    "set_model_pricing",
    "session_provider",
    "try_coordinator_lease",
    "migrate_legacy_devices",
    "project_id_for",
    "update_registration",
    "validate_interval",
    "validate_device_id",
    "validate_display_name",
    "validate_logical_id",
    "validate_volume_name",
    "verify_connection",
    "volume_fingerprint",
]
