"""Private installation identity and stable opaque project/device identifiers."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import sys

from . import constants as constants_api
from . import errors as errors_api
from . import state as state_api
from . import validation as validation_api


@dataclass(frozen=True)
class InstallationIdentity:
    """One read of the private installation identity for an aggregate scan."""

    install_id: str
    platform: str

    @property
    def host_device_id(self) -> str:
        return f"cage-local-{self.install_id[:8]}"

    def provider_device_id(self, provider: str, allowed_provider_ids: frozenset[str] | set[str]) -> str:
        provider_id = validation_api._provider_id_from_allowed(provider, allowed_provider_ids)
        if provider_id is None:
            raise errors_api.MonitorError("invalid monitor provider identity")
        return f"cage-{provider_id}-{self.platform}-{self.install_id[:8]}"

    def project_id(self, logical_id: str) -> str:
        validation_api.validate_logical_id(logical_id)
        digest = hmac.new(
            bytes.fromhex(self.install_id), logical_id.encode("ascii"), hashlib.sha256,
        ).hexdigest()[:20]
        return f"cage-project-{digest}"


def read_identity(config_root: Path) -> InstallationIdentity:
    return InstallationIdentity(host_install_id(config_root), _platform_slug())


def host_install_id(config_root: Path) -> str:
    root = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(root)
    value = state_api._read_json(root / constants_api.IDENTITY_FILE, max_bytes=constants_api.MAX_CONNECTION_BYTES)
    if value is not None:
        if not isinstance(value, dict) or set(value) != {"version", "install_id"}:
            raise errors_api.MonitorError("monitor identity has an invalid shape")
        install_id = value.get("install_id")
        if isinstance(install_id, str) and len(install_id) == 32 and all(c in "0123456789abcdef" for c in install_id):
            return install_id
        raise errors_api.MonitorError("monitor identity is invalid")
    install_id = secrets.token_hex(16)
    state_api._write_json(root / constants_api.IDENTITY_FILE, {"version": constants_api.STATE_VERSION, "install_id": install_id})
    return install_id


def logical_target_id(repository: str, target: str, preset: str = "") -> str:
    if target not in {"container", "desktop"}:
        raise errors_api.MonitorError("monitor target must be container or desktop")
    material = f"{target}\0{repository}\0{preset if target == 'desktop' else ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def host_device_id(config_root: Path) -> str:
    """Return one readable, collision-resistant hub identity per Cage install."""

    return read_identity(config_root).host_device_id


def _platform_slug() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    value = re.sub(r"[^a-z0-9]+", "-", sys.platform.lower()).strip("-")
    return value[:16] or "host"


def _provider_device_id_for(
    config_root: Path,
    provider: str,
    allowed_provider_ids: frozenset[str] | set[str],
) -> str:
    if validation_api._provider_id_from_allowed(provider, allowed_provider_ids) is None:
        raise errors_api.MonitorError("invalid monitor provider identity")
    return read_identity(config_root).provider_device_id(provider, allowed_provider_ids)


def device_id_for(config_root: Path, logical_id: str = "") -> str:
    """Compatibility wrapper for callers that previously passed a target id."""

    if logical_id:
        validation_api.validate_logical_id(logical_id)
    return host_device_id(config_root)


def project_id_for(config_root: Path, logical_id: str) -> str:
    """Return a stable opaque project id without exposing a path-derived hash."""

    validation_api.validate_logical_id(logical_id)
    return read_identity(config_root).project_id(logical_id)
