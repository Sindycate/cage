"""Register, adopt and retire exact sources while preserving their identity."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from dataclasses import replace
from typing import Any

from . import constants as constants_api
from . import errors as errors_api
from . import host_sources as host_sources_api
from . import identity as identity_api
from . import locks as locks_api
from . import models as models_api
from . import state as state_api
from . import validation as validation_api
from . import volumes as volumes_api


def retire_registration(config_root: Path, identity: str, *, disabled: bool) -> models_api.VolumeRegistration:
    """Retire one logical project, never an arbitrary row of a shared device."""

    if not isinstance(identity, str):
        raise errors_api.MonitorError("monitor project identity is invalid")
    with locks_api._registry_write_lock(config_root):
        registrations = load_registry(config_root)
        for index, record in enumerate(registrations):
            if identity not in {record.logical_id, record.legacy_device_id}:
                continue
            updated = replace(record, status="disabled" if disabled else "retired", last_error="")
            registrations[index] = updated
            save_registry(config_root, registrations)
            return updated
    raise errors_api.MonitorError("monitor project was not found")


def disable_all_registrations(config_root: Path) -> list[models_api.VolumeRegistration]:
    with locks_api._registry_write_lock(config_root):
        registrations = load_registry(config_root)
        updated = [replace(item, status="disabled", last_error="") for item in registrations]
        save_registry(config_root, updated)
        return updated


def clear_legacy_device_id(config_root: Path, logical_id: str, legacy_device_id: str) -> None:
    validation_api.validate_logical_id(logical_id)
    validation_api.validate_device_id(legacy_device_id)
    with locks_api._registry_write_lock(config_root):
        registrations = load_registry(config_root)
        record = next((item for item in registrations if item.logical_id == logical_id), None)
        if record is None or record.legacy_device_id != legacy_device_id:
            raise errors_api.MonitorError("monitor legacy device changed")
        save_registry(
            config_root,
            [replace(item, legacy_device_id="") if item.logical_id == logical_id else item for item in registrations],
        )


def _registry_path(root: Path) -> Path:
    return root / constants_api.REGISTRY_FILE


def _load_legacy_registration(config_root: Path, value: object) -> models_api.VolumeRegistration:
    if not isinstance(value, dict):
        raise errors_api.MonitorError("monitor registry entry must be an object")
    required = {
        "logical_id", "device_id", "volume_name", "target", "repository",
        "display_name", "fingerprint", "status", "registered_at",
        "last_scan_at", "last_success_at", "last_error",
    }
    if set(value) != required:
        raise errors_api.MonitorError("monitor registry entry has an invalid shape")
    legacy_device_id = validation_api.validate_device_id(value.get("device_id"))
    upgraded = dict(value)
    upgraded["device_id"] = identity_api.host_device_id(config_root)
    upgraded["legacy_device_id"] = legacy_device_id
    return models_api.VolumeRegistration.from_dict(upgraded)


def load_registry(config_root: Path) -> list[models_api.VolumeRegistration]:
    root = state_api.monitor_root(config_root)
    state_api._ensure_private_directory(root)
    value = state_api._read_json(_registry_path(root), max_bytes=constants_api.MAX_REGISTRY_BYTES)
    if value is None:
        return []
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "registrations"}
        or value["version"] not in {1, constants_api.REGISTRY_VERSION}
    ):
        raise errors_api.MonitorError("monitor registry has an invalid shape")
    registrations = value["registrations"]
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise errors_api.MonitorError("monitor registry registrations are invalid")
    if value["version"] == 1:
        result = [_load_legacy_registration(config_root, item) for item in registrations]
    else:
        result = [models_api.VolumeRegistration.from_dict(item) for item in registrations]
    if len({item.logical_id for item in result}) != len(result):
        raise errors_api.MonitorError("monitor registry contains duplicate logical targets")
    expected_device = identity_api.host_device_id(config_root)
    if any(item.device_id != expected_device for item in result):
        raise errors_api.MonitorError("monitor registry contains an unexpected Cage device")
    legacy_ids = [item.legacy_device_id for item in result if item.legacy_device_id]
    if len(set(legacy_ids)) != len(legacy_ids):
        raise errors_api.MonitorError("monitor registry contains duplicate legacy devices")
    return result


def save_registry(config_root: Path, registrations: list[models_api.VolumeRegistration]) -> None:
    if not isinstance(registrations, list) or len(registrations) > 4096:
        raise errors_api.MonitorError("too many monitor registrations")
    serialized: list[dict[str, Any]] = []
    logical_ids: set[str] = set()
    legacy_ids: set[str] = set()
    expected_device = identity_api.host_device_id(config_root)
    for item in registrations:
        if not isinstance(item, models_api.VolumeRegistration):
            raise errors_api.MonitorError("monitor registry entries must be VolumeRegistration values")
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
        validated = models_api.VolumeRegistration.from_dict(value)
        if validated.logical_id in logical_ids:
            raise errors_api.MonitorError("monitor registry contains duplicate logical targets")
        if validated.device_id != expected_device:
            raise errors_api.MonitorError("monitor registry contains an unexpected Cage device")
        if validated.legacy_device_id and validated.legacy_device_id in legacy_ids:
            raise errors_api.MonitorError("monitor registry contains duplicate legacy devices")
        logical_ids.add(validated.logical_id)
        if validated.legacy_device_id:
            legacy_ids.add(validated.legacy_device_id)
        serialized.append(value)
    state_api._write_json(
        _registry_path(state_api.monitor_root(config_root)),
        {
            "version": constants_api.REGISTRY_VERSION,
            "registrations": serialized,
        },
    )


def _recovered_registration_for_launch(
    registrations: list[models_api.VolumeRegistration],
    *,
    volume_name: str,
    target: str,
    fingerprint: dict[str, str],
    display_name: str,
) -> models_api.VolumeRegistration | None:
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
) -> models_api.VolumeRegistration:
    if (
        not Path(repository).is_absolute()
        or len(repository) > 4096
        or any(character in repository for character in "\0\r\n")
    ):
        raise errors_api.MonitorError("monitor repository must be an absolute path")
    validation_api.validate_display_name(display_name)
    validation_api.validate_volume_name(volume_name)
    logical_id = identity_api.logical_target_id(repository, target, preset)
    device_id = identity_api.device_id_for(config_root, logical_id)
    current = validation_api.validate_fingerprint(fingerprint or volumes_api.volume_fingerprint(docker, volume_name))
    if current["name"] != volume_name:
        raise errors_api.MonitorError("Docker volume fingerprint name mismatch")
    label_identity = current.get("label_identity", "")
    with locks_api._registry_write_lock(config_root):
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
            raise errors_api.MonitorError("monitor volume label belongs to a different logical target; run cage monitor add explicitly")
        existing = next((item for item in registrations if item.logical_id == logical_id), None)
        if existing is not None:
            if existing.volume_name != volume_name:
                raise errors_api.MonitorError("monitor logical target maps to a different volume")
            if existing.fingerprint != current and not allow_replacement:
                updated = replace(existing, status="needs-adoption", last_error="volume fingerprint changed; run cage monitor add explicitly")
                save_registry(config_root, [updated if item.logical_id == logical_id else item for item in registrations])
                raise errors_api.MonitorError("monitor volume changed; explicit adoption is required")
            if existing.status in {"disabled", "retired"} and not allow_replacement:
                raise errors_api.MonitorError("monitor registration is retired; run cage monitor add explicitly")
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
            raise errors_api.MonitorError(
                "monitor volume is already registered to a different logical target"
            )
        record = models_api.VolumeRegistration(
            logical_id=logical_id,
            device_id=device_id,
            volume_name=volume_name,
            target=target,
            repository=repository,
            display_name=display_name,
            fingerprint=current,
            registered_at=state_api._now(),
        )
        save_registry(config_root, [*registrations, record])
        return record


def register_host_source(
    config_root: Path,
    source_home: Path | str,
    *,
    copy_auth: bool,
    allow_replacement: bool = False,
) -> models_api.VolumeRegistration:
    """Adopt one auth-root-bound private host session store.

    The existing source ``CODEX_HOME`` is never scanned.  Instead this creates
    a separate private home below Cage monitor state, seeds only allowlisted
    static configuration, and later points Cage host launches at that home.
    Direct ``codex`` commands continue using the original source untouched.
    """

    if type(copy_auth) is not bool:
        raise errors_api.MonitorError("host Codex source copy policy is invalid")
    source, logical_id, fingerprint = host_sources_api._host_source_identity(config_root, source_home)
    volume_name = fingerprint["name"]
    repository_marker = host_sources_api._host_source_repository(logical_id)
    with locks_api.HostSourceLease.acquire(config_root, logical_id):
        with locks_api._registry_write_lock(config_root):
            registrations = load_registry(config_root)
            existing = next(
                (item for item in registrations if item.logical_id == logical_id),
                None,
            )
            if existing is not None:
                if (
                    existing.target != "host"
                    or existing.repository != repository_marker
                    or existing.volume_name != volume_name
                    or existing.fingerprint != fingerprint
                ):
                    if not allow_replacement:
                        raise errors_api.MonitorError(
                            "host Codex source changed; explicit monitor adoption is required"
                        )
                    record = replace(
                        existing,
                        volume_name=volume_name,
                        target="host",
                        repository=repository_marker,
                        display_name="Cage: Managed Host Sessions",
                        fingerprint=fingerprint,
                        status="active",
                        last_error="",
                    )
                elif existing.status in {"disabled", "retired", "needs-adoption"}:
                    if not allow_replacement:
                        raise errors_api.MonitorError(
                            "host Codex source is inactive; run cage monitor add --auth explicitly"
                        )
                    record = replace(existing, status="active", last_error="")
                else:
                    record = existing
            else:
                record = models_api.VolumeRegistration(
                    logical_id=logical_id,
                    device_id=identity_api.device_id_for(config_root, logical_id),
                    volume_name=volume_name,
                    target="host",
                    repository=repository_marker,
                    display_name="Cage: Managed Host Sessions",
                    fingerprint=fingerprint,
                    registered_at=state_api._now(),
                )

        # Do not persist an active registration until its managed home has a
        # complete, private static snapshot.  A failed copy leaves direct host
        # execution untouched and cannot turn an existing shared home into a
        # collector source.
        home = host_sources_api._ensure_managed_host_home(config_root, record)
        host_sources_api._synchronize_host_static_source(config_root, record, source, home)
        host_sources_api._synchronize_host_file(source, home, "auth.json", enabled=copy_auth)
        host_sources_api._synchronize_host_file(source, home, ".credentials.json", enabled=False)

        with locks_api._registry_write_lock(config_root):
            registrations = load_registry(config_root)
            current = next(
                (item for item in registrations if item.logical_id == logical_id),
                None,
            )
            if current is not None and current != existing:
                raise errors_api.MonitorError("host Codex source registration changed during adoption")
            if current is None:
                save_registry(config_root, [*registrations, record])
            elif current != record:
                save_registry(
                    config_root,
                    [record if item.logical_id == logical_id else item for item in registrations],
                )
        return record


def registered_host_source(
    config_root: Path,
    source_home: Path | str,
) -> models_api.VolumeRegistration | None:
    """Find an exact active host source without creating monitor state.

    This lookup intentionally treats an absent, replaced, or unsafe source as
    unadopted.  Ordinary host mode then retains its historical direct behavior
    instead of ever falling back to scanning the shared source directory.
    """

    root = state_api.monitor_root(config_root)
    try:
        root_info = os.lstat(root)
        registry_info = os.lstat(root / constants_api.REGISTRY_FILE)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise errors_api.MonitorError("cannot inspect Token Monitor host source state") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_ISLNK(registry_info.st_mode)
        or not stat.S_ISREG(registry_info.st_mode)
    ):
        raise errors_api.MonitorError("unsafe Token Monitor host source state")
    try:
        _source, logical_id, fingerprint = host_sources_api._host_source_identity(config_root, source_home)
    except errors_api.MonitorError:
        return None
    for record in load_registry(config_root):
        if (
            record.logical_id == logical_id
            and record.target == "host"
            and record.repository == host_sources_api._host_source_repository(logical_id)
            and record.fingerprint == fingerprint
            and record.status == "active"
        ):
            return record
    return None


def disable_host_source(
    config_root: Path,
    source_home: Path | str,
) -> models_api.VolumeRegistration:
    """Stop routing one adopted auth source through its managed session home.

    This is intentionally a local, non-destructive opt-out.  It leaves the
    private managed sessions available for a later explicit re-adoption and
    does not delete a shared provider device on the hub.  A subsequent normal
    host launch therefore returns to the original ``CODEX_HOME`` immediately.
    """

    _source, logical_id, fingerprint = host_sources_api._host_source_identity(config_root, source_home)
    repository_marker = host_sources_api._host_source_repository(logical_id)
    with locks_api.HostSourceLease.acquire(config_root, logical_id):
        with locks_api._registry_write_lock(config_root):
            registrations = load_registry(config_root)
            record = next(
                (item for item in registrations if item.logical_id == logical_id),
                None,
            )
            if (
                record is None
                or record.target != "host"
                or record.repository != repository_marker
                or record.fingerprint != fingerprint
            ):
                raise errors_api.MonitorError("host Codex source is not an adopted monitor source")
            updated = replace(record, status="disabled", last_error="")
            save_registry(
                config_root,
                [updated if item.logical_id == logical_id else item for item in registrations],
            )
            return updated


def update_registration(config_root: Path, record: models_api.VolumeRegistration) -> None:
    validation_api.validate_logical_id(record.logical_id)
    validation_api.validate_device_id(record.device_id)
    validation_api.validate_volume_name(record.volume_name)
    validation_api.validate_display_name(record.display_name)
    validation_api.validate_fingerprint(record.fingerprint)
    with locks_api._registry_write_lock(config_root):
        registrations = load_registry(config_root)
        if not any(item.logical_id == record.logical_id for item in registrations):
            raise errors_api.MonitorError("monitor registration disappeared")
        save_registry(config_root, [record if item.logical_id == record.logical_id else item for item in registrations])


def _scan_error_for_records(
    config_root: Path,
    records: list[models_api.VolumeRegistration],
    error: str,
) -> str:
    """Return a status-safe scan failure without managed host paths.

    Docker and filesystem diagnostics can echo a bind source.  That source is
    intentionally private, and scan errors are retained in several status
    files as well as printed by the optional background worker.  Redact only
    errors that contain that private managed-source path so unrelated,
    actionable aggregate failures remain visible.
    """

    for record in records:
        if record.target != "host":
            continue
        source_root, _home, _snapshot = host_sources_api._host_source_paths(config_root, record)
        if str(source_root) in error:
            return "Token Monitor scan failed for managed host sessions"
    return " ".join(error.split())[:512]


def _record_scan_error(config_root: Path, record: models_api.VolumeRegistration, error: str) -> None:
    """Best-effort status bookkeeping that never masks the scan failure."""

    try:
        with locks_api._registry_write_lock(config_root):
            registrations = load_registry(config_root)
            current = next(
                (item for item in registrations if item.logical_id == record.logical_id),
                None,
            )
            if current is None:
                return
            updated = replace(
                current,
                last_scan_at=state_api._now(),
                last_error=_scan_error_for_records(config_root, [current], error),
            )
            save_registry(
                config_root,
                [updated if item.logical_id == record.logical_id else item for item in registrations],
            )
    except (errors_api.MonitorError, OSError):
        return


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
        raise errors_api.MonitorError(f"Docker monitor operation failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().replace("\n", " ")[:300]
        raise errors_api.MonitorError(f"Docker monitor operation failed: {detail or 'unknown error'}")
    registrations = {item.volume_name: item for item in load_registry(config_root)}
    discovered: list[dict[str, Any]] = []
    names = sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith(constants_api.CODEX_VOLUME_PREFIX)
        }
    )
    for volume_name in names:
        validation_api.validate_volume_name(volume_name)
        fingerprint = volumes_api.volume_fingerprint(docker, volume_name)
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
    validation_api.validate_volume_name(volume_name)
    return f"/__cage_recovered__/{volume_name}"


def register_recovered_volume(
    config_root: Path,
    docker: str,
    *,
    volume_name: str,
    display_name: str = "",
) -> models_api.VolumeRegistration:
    """Adopt an exact dormant volume without inventing a host path."""

    validation_api.validate_volume_name(volume_name)
    fingerprint = volumes_api.volume_fingerprint(docker, volume_name)
    if not display_name:
        suffix = volume_name.removeprefix(constants_api.CODEX_VOLUME_PREFIX)
        display_name = f"Cage: Recovered {suffix}"
    validation_api.validate_display_name(display_name)
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
