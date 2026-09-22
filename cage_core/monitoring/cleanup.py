"""Remove exact private monitor artifacts after explicit lifecycle actions."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat

from . import constants as constants_api
from . import errors as errors_api
from . import identity as identity_api
from . import state as state_api
from . import validation as validation_api


def remove_aggregate_status(config_root: Path) -> None:
    path = state_api.monitor_root(config_root) / constants_api.AGGREGATE_STATUS_FILE
    try:
        state_api._reject_unsafe_path(path, max_bytes=constants_api.MAX_CONNECTION_BYTES)
    except FileNotFoundError:
        pass
    else:
        try:
            path.unlink()
        except OSError as exc:
            raise errors_api.MonitorError(f"cannot remove monitor aggregate status: {exc}") from exc
    # A successful explicit host-device forget also retires the private
    # scheduler, prepared generations, and repair journal.  Leaving payload
    # generations behind would retain aggregate session data after the user
    # deliberately forgot the host device.
    state_api._remove_private_file(
        state_api.monitor_root(config_root) / constants_api.SCHEDULER_STATE_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    state_api._remove_private_file(
        state_api.monitor_root(config_root) / constants_api.UPLOAD_STATE_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    state_api._remove_owned_directory(
        state_api.monitor_root(config_root) / constants_api.GENERATION_DIR,
        description="monitor upload generation",
    )


def remove_device_state(config_root: Path, device_id: str) -> None:
    validation_api.validate_device_id(device_id)
    monitor_directory = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(monitor_directory)
    root = monitor_directory.resolve()
    devices = root / constants_api.DEVICE_DIR
    state_api._ensure_private_directory(devices)
    # Keep the final component unresolved so a malicious device entry cannot
    # redirect deletion through a symlink.  The strict device-id grammar makes
    # this path containment check structural rather than string-based.
    path = devices / device_id
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise errors_api.MonitorError("unsafe monitor device state directory")
    if info.st_uid != os.getuid():
        raise errors_api.MonitorError("monitor device state directory is not owned by the current user")
    shutil.rmtree(path)


def remove_project_state(config_root: Path, logical_id: str) -> None:
    validation_api.validate_logical_id(logical_id)
    projects = state_api.monitor_root(config_root) / constants_api.PROJECT_DIR
    state_api._ensure_private_directory(projects)
    state_api._remove_owned_directory(projects / identity_api.project_id_for(config_root, logical_id), description="monitor project state")
