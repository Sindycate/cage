"""Validate and persist the private host-owned hub connection."""

from __future__ import annotations

import os
from pathlib import Path

from . import constants as constants_api
from . import errors as errors_api
from . import models as models_api
from . import state as state_api
from . import validation as validation_api


def _connection_path(root: Path) -> Path:
    return root / constants_api.CONNECTION_FILE


def load_connection(config_root: Path) -> models_api.MonitorConnection | None:
    root = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(root)
    value = state_api._read_json(_connection_path(root), max_bytes=constants_api.MAX_CONNECTION_BYTES)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"version", "hub_url", "secret", "interval_seconds", "enabled"}:
        raise errors_api.MonitorError("monitor connection has an invalid shape")
    if value["version"] != constants_api.STATE_VERSION or not isinstance(value["secret"], str) or not value["secret"]:
        raise errors_api.MonitorError("monitor connection secret is invalid")
    if len(value["secret"].encode("utf-8")) > constants_api.MAX_SECRET_BYTES or any(
        character in value["secret"] for character in "\x00\r\n"
    ):
        raise errors_api.MonitorError("monitor connection secret is invalid")
    if not isinstance(value["enabled"], bool):
        raise errors_api.MonitorError("monitor connection enabled flag is invalid")
    return models_api.MonitorConnection(
        hub_url=validation_api.normalize_hub_url(value["hub_url"]),
        secret=value["secret"],
        interval_seconds=validation_api.validate_interval(value["interval_seconds"]),
        enabled=value["enabled"],
    )


def save_connection(config_root: Path, connection: models_api.MonitorConnection) -> None:
    if (
        not isinstance(connection.secret, str)
        or not connection.secret
        or len(connection.secret.encode("utf-8")) > constants_api.MAX_SECRET_BYTES
        or any(character in connection.secret for character in "\x00\r\n")
    ):
        raise errors_api.MonitorError("monitor connection secret is invalid")
    state_api._write_json(
        _connection_path(state_api.monitor_root(config_root)),
        {
            "version": constants_api.STATE_VERSION,
            "hub_url": validation_api.normalize_hub_url(connection.hub_url),
            "secret": connection.secret,
            "interval_seconds": validation_api.validate_interval(connection.interval_seconds),
            "enabled": bool(connection.enabled),
        },
    )


def disable_connection(config_root: Path) -> None:
    root = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(root)
    path = _connection_path(root)
    try:
        state_api._reject_unsafe_path(path, max_bytes=constants_api.MAX_CONNECTION_BYTES)
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
        raise errors_api.MonitorError(f"cannot remove monitor connection: {exc}") from exc
