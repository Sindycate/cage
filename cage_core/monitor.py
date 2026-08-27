"""Optional host-owned Token Monitor integration.

The monitor is deliberately outside the launch-plan contract.  Cage keeps the
hub credential and all aggregation traffic on the host, while a short-lived
collector container receives only the two Codex session subdirectories from a
registered volume.
"""

from __future__ import annotations

import fcntl
import hashlib
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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import storage


STATE_VERSION = 1
COLLECTOR_IMAGE = "cage-token-monitor"
COLLECTOR_REGISTRY = "ghcr.io/sindycate/cage/token-monitor"
COLLECTOR_DOCKERFILE = "Dockerfile.monitor"
COLLECTOR_SOURCE_VERSION = "0.48.0"
COLLECTOR_SOURCE_COMMIT = "6121585f5d5e7fa98385f8a5ac7f8639660e4965"
COLLECTOR_SOURCE_SHA256 = (
    "019b9dede6daa9e34a306dac0e3a6f90ca25ca900a298bb5205cdbe8a25a3cda"
)
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
SCAN_TIMEOUT_SECONDS = 180
COLLECTOR_MEMORY = "1g"
COLLECTOR_CPUS = "1.0"
COLLECTOR_PIDS = "128"

# Token Monitor's local hub receives the upstream agent's sync payload.  Keep
# this top-level wire contract explicit so a future upstream collector cannot
# silently add native-session, credential, or diagnostic fields to a Cage
# upload.  The omission fields are produced by the pinned sync serializer when
# it trims an oversized payload; the remaining fields are the v0.48.0 summary
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
RUN_DIR = "runs"
VOLUME_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
DEVICE_ID_PATTERN = re.compile(r"^cage-[a-z0-9_-]{1,120}$")
LOGICAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


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
        )
        if any(not isinstance(value[name], str) for name in strings):
            raise MonitorError("monitor registry entry fields must be strings")
        validate_logical_id(value["logical_id"])
        validate_device_id(value["device_id"])
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
        )

    def public_dict(self) -> dict[str, Any]:
        """Return a redacted status shape; never expose the repository path."""

        return {
            "logical_id": self.logical_id,
            "device_id": self.device_id,
            "volume_name": self.volume_name,
            "target": self.target,
            "display_name": self.display_name,
            "status": self.status,
            "registered_at": self.registered_at,
            "last_scan_at": self.last_scan_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }


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


def retire_registration(config_root: Path, device_id: str, *, disabled: bool) -> VolumeRegistration:
    validate_device_id(device_id)
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
        for index, record in enumerate(registrations):
            if record.device_id != device_id:
                continue
            updated = replace(record, status="disabled" if disabled else "retired", last_error="")
            registrations[index] = updated
            save_registry(config_root, registrations)
            return updated
    raise MonitorError("monitor device was not found")


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


def device_id_for(config_root: Path, logical_id: str) -> str:
    validate_logical_id(logical_id)
    return f"cage-{host_install_id(config_root)[:12]}-{logical_id[:16]}"


def _registry_path(root: Path) -> Path:
    return root / REGISTRY_FILE


def load_registry(config_root: Path) -> list[VolumeRegistration]:
    root = monitor_root(config_root)
    _ensure_private_directory(root)
    value = _read_json(_registry_path(root), max_bytes=MAX_REGISTRY_BYTES)
    if value is None:
        return []
    if not isinstance(value, dict) or set(value) != {"version", "registrations"} or value["version"] != STATE_VERSION:
        raise MonitorError("monitor registry has an invalid shape")
    registrations = value["registrations"]
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise MonitorError("monitor registry registrations are invalid")
    result = [VolumeRegistration.from_dict(item) for item in registrations]
    if len({item.logical_id for item in result}) != len(result):
        raise MonitorError("monitor registry contains duplicate logical targets")
    if len({item.device_id for item in result}) != len(result):
        raise MonitorError("monitor registry contains duplicate devices")
    return result


def save_registry(config_root: Path, registrations: list[VolumeRegistration]) -> None:
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise MonitorError("too many monitor registrations")
    serialized: list[dict[str, Any]] = []
    logical_ids: set[str] = set()
    device_ids: set[str] = set()
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
        }
        validated = VolumeRegistration.from_dict(value)
        if validated.logical_id in logical_ids:
            raise MonitorError("monitor registry contains duplicate logical targets")
        if validated.device_id in device_ids:
            raise MonitorError("monitor registry contains duplicate devices")
        logical_ids.add(validated.logical_id)
        device_ids.add(validated.device_id)
        serialized.append(value)
    _write_json(
        _registry_path(monitor_root(config_root)),
        {
            "version": STATE_VERSION,
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
    if label_identity and label_identity != logical_id and not allow_replacement:
        raise MonitorError("monitor volume label belongs to a different logical target; run cage monitor add explicitly")
    with _registry_write_lock(config_root):
        registrations = load_registry(config_root)
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


def _device_state_path(config_root: Path, device_id: str) -> Path:
    validate_device_id(device_id)
    path = monitor_root(config_root) / DEVICE_DIR / device_id
    _ensure_private_directory(path)
    return path


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
        f"type=volume,src={volume_name},dst=/probe,readonly,volume-subpath={subpath}",
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
    message = result.stderr.strip().replace("\n", " ")[:300]
    lower = message.lower()
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
    if "volume-subpath" in lower and any(
        marker in lower for marker in ("does not exist", "no such file or directory")
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
    state_path = _device_state_path(config_root, record.device_id)
    output_descriptor, output_name = tempfile.mkstemp(prefix="collector-", suffix=".json", dir=run_root)
    os.close(output_descriptor)
    output_path = Path(output_name)
    output_path.unlink(missing_ok=True)
    output_path.touch(mode=0o600)
    os.chmod(output_path, 0o600)
    mounts = []
    for subpath, destination in (("sessions", "/scan/codex/sessions"), ("archived_sessions", "/scan/codex/archived_sessions")):
        if _subpath_available(docker, image, record.volume_name, subpath):
            mounts.extend(("--mount", f"type=volume,src={record.volume_name},dst={destination},readonly,volume-subpath={subpath}"))
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
        "/scan/codex:rw,noexec,nosuid,nodev,size=32m",
        *mounts,
        "-e",
        "CODEX_HOME=/scan/codex",
        "-e",
        "TOKEN_MONITOR_SHARED_DIR=/state",
        "-e",
        "TOKEN_MONITOR_CLIENTS=codex",
        "-e",
        "TOKEN_MONITOR_LIMITS_ENABLED=0",
        "-e",
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


def delete_device(connection: MonitorConnection, device_id: str) -> None:
    validate_device_id(device_id)
    from urllib.parse import quote

    _hub_request(connection, "DELETE", "/api/devices/" + quote(device_id, safe=""))


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
) -> tuple[VolumeRegistration, dict[str, Any]]:
    try:
        connection = load_connection(config_root)
        if connection is None or not connection.enabled:
            raise MonitorError("Token Monitor is not connected")
        current_fingerprint = volume_fingerprint(docker, record.volume_name)
        if current_fingerprint != record.fingerprint:
            updated = replace(record, status="needs-adoption", last_error="volume fingerprint changed; run cage monitor add explicitly", last_scan_at=_now())
            update_registration(config_root, updated)
            raise MonitorError("monitor volume fingerprint changed")
        image = ensure_collector_image(docker, install_root, version=version, storage_policy=storage_policy, allow_build=allow_build)
        with try_volume_lock(config_root, record.logical_id) as acquired:
            if not acquired:
                raise MonitorError("monitor scan already running for this volume")
            payload = _run_collector(docker, image, record, config_root, uid=os.getuid() if uid is None else uid, gid=os.getgid() if gid is None else gid)
            payload["hostname"] = record.display_name
            payload["platform"] = "cage"
            payload["osName"] = "Cage"
            upload_summary(connection, payload)
        updated = replace(record, status="active", last_scan_at=_now(), last_success_at=_now(), last_error="")
        update_registration(config_root, updated)
        return updated, payload
    except MonitorError as exc:
        if str(exc) not in {"Token Monitor is not connected", "monitor volume fingerprint changed"}:
            _record_scan_error(config_root, record, str(exc))
        raise


class ActiveMonitor:
    """Best-effort active-session scanner owned by a Cage launch."""

    def __init__(self, scan, interval_seconds: int):
        self._scan = scan
        self._interval = validate_interval(interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cage-token-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as exc:  # optional observability must not stop Cage
                print(f"WARNING: Token Monitor scan skipped: {exc}", file=sys.stderr)
            if self._stop.wait(self._interval):
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=SCAN_TIMEOUT_SECONDS + 10)
        try:
            self._scan()
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
    "MonitorConnection",
    "MonitorError",
    "VolumeRegistration",
    "collector_image",
    "collector_registry_image",
    "delete_device",
    "device_id_for",
    "disable_connection",
    "ensure_codex_volume",
    "ensure_codex_volume_labels",
    "ensure_collector_image",
    "load_connection",
    "load_registry",
    "logical_target_id",
    "monitor_root",
    "normalize_hub_url",
    "register_volume",
    "remove_device_state",
    "retire_registration",
    "save_connection",
    "save_registry",
    "scan_registration",
    "update_registration",
    "validate_interval",
    "validate_device_id",
    "validate_display_name",
    "validate_logical_id",
    "validate_volume_name",
    "verify_connection",
    "volume_fingerprint",
]
