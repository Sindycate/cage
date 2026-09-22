"""Typed monitor connection, registration and host-session records."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any

from . import errors as errors_api
from . import identity as identity_api
from . import validation as validation_api


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
            raise errors_api.MonitorError("monitor registry entry must be an object")
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
            raise errors_api.MonitorError("monitor registry entry has an invalid shape")
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
            raise errors_api.MonitorError("monitor registry entry fields must be strings")
        validation_api.validate_logical_id(value["logical_id"])
        validation_api.validate_device_id(value["device_id"])
        if value["legacy_device_id"]:
            validation_api.validate_device_id(value["legacy_device_id"])
        validation_api.validate_volume_name(value["volume_name"])
        if (
            not value["repository"].startswith("/")
            or len(value["repository"]) > 4096
            or any(character in value["repository"] for character in "\x00\r\n")
        ):
            raise errors_api.MonitorError("monitor registry repository is invalid")
        validation_api.validate_display_name(value["display_name"])
        fingerprint = validation_api.validate_fingerprint(value["fingerprint"])
        if value["target"] not in {"container", "desktop", "host"}:
            raise errors_api.MonitorError("monitor target is invalid")
        if value["status"] not in {"active", "retired", "disabled", "needs-adoption"}:
            raise errors_api.MonitorError("monitor registration status is invalid")
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
        value["project_id"] = identity_api.project_id_for(config_root, self.logical_id)
        return value


@dataclass(frozen=True)
class HostSourceSession:
    """One prepared, Cage-managed native-host Codex source.

    ``source_home`` is retained only in private process state.  It is never
    placed in a collector environment, status payload, registry public view,
    or hub upload.  The baseline hashes and source identity let the host target
    decline stale auth or OAuth write-back instead of overwriting an
    independently refreshed or replaced source.
    """

    record: VolumeRegistration
    source_home: Path
    codex_home: Path
    source_identity: tuple[int, int]
    auth_baseline: str
    sync_auth: bool
    credential_baseline: str
    sync_oauth_credentials: bool
