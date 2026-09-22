"""Validate fingerprint-bound source caches and aggregate status."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime
from typing import Any

from . import constants as constants_api
from . import errors as errors_api
from . import identity as identity_api
from . import models as models_api
from . import state as state_api
from . import validation as validation_api


def _project_state_path(config_root: Path, record: models_api.VolumeRegistration) -> Path:
    """Return isolated collector state and adopt a legacy archive once."""

    project_id = identity_api.project_id_for(config_root, record.logical_id)
    projects = state_api.monitor_root(config_root) / constants_api.PROJECT_DIR
    state_api._ensure_private_directory(projects)
    path = projects / project_id
    if path.exists():
        state_api._ensure_private_directory(path)
        return path
    if record.legacy_device_id:
        legacy = state_api.monitor_root(config_root) / constants_api.DEVICE_DIR / record.legacy_device_id
        try:
            info = os.lstat(legacy)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise errors_api.MonitorError("unsafe legacy monitor device state directory")
            if info.st_uid != os.getuid():
                raise errors_api.MonitorError("legacy monitor device state is not owned by the current user")
            try:
                os.replace(legacy, path)
            except OSError as exc:
                raise errors_api.MonitorError(f"cannot adopt legacy monitor archive: {exc}") from exc
            state_api._ensure_private_directory(path)
            return path
    state_api._ensure_private_directory(path)
    return path


def _volume_snapshot_path(config_root: Path, record: models_api.VolumeRegistration) -> Path:
    return _project_state_path(config_root, record) / constants_api.VOLUME_SNAPSHOT_FILE


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
    record: models_api.VolumeRegistration,
) -> tuple[dict[str, Any] | None, bool]:
    value = state_api._read_json(
        _volume_snapshot_path(config_root, record),
        max_bytes=constants_api.MAX_SNAPSHOT_BYTES,
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
        raise errors_api.MonitorError("monitor volume snapshot has an invalid shape")
    if (
        value["version"] != constants_api.VOLUME_SNAPSHOT_VERSION
        or value["logical_id"] != record.logical_id
        or value["device_id"] != record.device_id
        or value["volume_name"] != record.volume_name
        or value["fingerprint"] != record.fingerprint
    ):
        raise errors_api.MonitorError("monitor volume snapshot identity does not match the registry")
    validation_api.validate_display_name(value["display_name"])
    captured_at = value["captured_at"]
    if _snapshot_timestamp(captured_at) is None:
        raise errors_api.MonitorError("monitor volume snapshot timestamp is invalid")
    summary_hash = value["summary_hash"]
    if not isinstance(summary_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", summary_hash):
        raise errors_api.MonitorError("monitor volume snapshot hash is invalid")
    payload = validation_api._validate_summary(value["payload"], record.device_id)
    if _summary_content_hash(payload) != summary_hash:
        raise errors_api.MonitorError("monitor volume snapshot hash does not match its payload")
    return payload, value["display_name"] != record.display_name


def load_volume_snapshot(
    config_root: Path,
    record: models_api.VolumeRegistration,
) -> dict[str, Any] | None:
    """Load one exact, sanitized last-known-good volume summary."""

    payload, _metadata_changed = _load_trusted_volume_snapshot(config_root, record)
    return payload


def _save_volume_snapshot(
    config_root: Path,
    record: models_api.VolumeRegistration,
    payload: dict[str, Any],
) -> None:
    payload = validation_api._validate_summary(payload, record.device_id)
    captured_at = state_api._now()
    state_api._write_json(
        _volume_snapshot_path(config_root, record),
        {
            "version": constants_api.VOLUME_SNAPSHOT_VERSION,
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
    record: models_api.VolumeRegistration,
    *,
    max_age_seconds: int,
) -> bool:
    try:
        value = state_api._read_json(
            _volume_snapshot_path(config_root, record),
            max_bytes=constants_api.MAX_SNAPSHOT_BYTES,
        )
    except errors_api.MonitorError:
        return False
    if not isinstance(value, dict):
        return False
    timestamp = _snapshot_timestamp(value.get("captured_at"))
    if timestamp is None:
        return False
    age = time.time() - timestamp
    return 0 <= age < max(constants_api.MIN_INTERVAL_SECONDS, max_age_seconds)


def load_aggregate_status(config_root: Path) -> dict[str, Any] | None:
    value = state_api._read_json(
        state_api.monitor_root(config_root) / constants_api.AGGREGATE_STATUS_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != constants_api.STATE_VERSION:
        raise errors_api.MonitorError("monitor aggregate status is invalid")
    return value


def load_split_status(config_root: Path) -> dict[str, Any] | None:
    value = state_api._read_json(
        state_api.monitor_root(config_root) / constants_api.SPLIT_STATUS_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != constants_api.SPLIT_STATUS_VERSION:
        raise errors_api.MonitorError("monitor provider split status is invalid")
    if type(value.get("complete")) is not bool:
        raise errors_api.MonitorError("monitor provider split status is invalid")
    device_ids = value.get("device_ids", [])
    if not isinstance(device_ids, list) or any(
        not isinstance(item, str) for item in device_ids
    ):
        raise errors_api.MonitorError("monitor provider split device list is invalid")
    for item in device_ids:
        validation_api.validate_device_id(item)
    legacy_device_id = value.get("legacy_device_id", "")
    if not isinstance(legacy_device_id, str) or (
        legacy_device_id and validation_api.validate_device_id(legacy_device_id) != legacy_device_id
    ):
        raise errors_api.MonitorError("monitor provider split legacy device is invalid")
    updated_at = value.get("updated_at", "")
    if not isinstance(updated_at, str) or len(updated_at) > 128:
        raise errors_api.MonitorError("monitor provider split timestamp is invalid")
    total_tokens = value.get("total_tokens", 0)
    if (
        type(total_tokens) not in (int, float)
        or not math.isfinite(total_tokens)
        or total_tokens < 0
    ):
        raise errors_api.MonitorError("monitor provider split total is invalid")
    return value
