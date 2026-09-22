"""Persist and validate completion of the provider-split transition."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from . import constants as constants_api
from . import errors as errors_api
from . import hub as hub_api
from . import identity as identity_api
from . import models as models_api
from . import snapshots as snapshots_api
from . import state as state_api
from . import validation as validation_api


def save_split_status(config_root: Path, value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or type(value.get("complete")) is not bool:
        raise errors_api.MonitorError("monitor provider split status is invalid")
    device_ids = value.get("device_ids", [])
    if not isinstance(device_ids, list) or any(
        not isinstance(item, str) for item in device_ids
    ):
        raise errors_api.MonitorError("monitor provider split device list is invalid")
    for item in device_ids:
        validation_api.validate_device_id(item)
    legacy_device_id = value.get("legacy_device_id", "")
    if not isinstance(legacy_device_id, str):
        raise errors_api.MonitorError("monitor provider split legacy device is invalid")
    if legacy_device_id:
        validation_api.validate_device_id(legacy_device_id)
    updated_at = value.get("updated_at", state_api._now())
    if not isinstance(updated_at, str) or len(updated_at) > 128:
        raise errors_api.MonitorError("monitor provider split timestamp is invalid")
    total_tokens = value.get("total_tokens", 0)
    if (
        type(total_tokens) not in (int, float)
        or not math.isfinite(total_tokens)
        or total_tokens < 0
    ):
        raise errors_api.MonitorError("monitor provider split total is invalid")
    state_api._write_json(
        state_api.monitor_root(config_root) / constants_api.SPLIT_STATUS_FILE,
        {
            "version": constants_api.SPLIT_STATUS_VERSION,
            "complete": value["complete"],
            "legacy_device_id": legacy_device_id,
            "device_ids": sorted(set(device_ids)),
            "updated_at": updated_at,
            "total_tokens": total_tokens,
        },
    )


def provider_split_pending(config_root: Path, connection: models_api.MonitorConnection) -> bool:
    """Return whether the old unsplit device still exists on the hub."""

    state = snapshots_api.load_split_status(config_root)
    if state is not None and state.get("complete") is True:
        return False
    return identity_api.host_device_id(config_root) in hub_api._hub_device_ids(connection)


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
            "updated_at": manifest.get("updated_at", state_api._now()),
            "total_tokens": manifest.get("total_tokens", 0),
        },
    )
