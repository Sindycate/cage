"""Coordinate collection, accounting and publication with persisted failures."""

from __future__ import annotations

import os
from pathlib import Path
import time
from dataclasses import replace
from datetime import datetime
from typing import Any

from . import accounting as accounting_api
from . import aggregation as aggregation_api
from . import collector as collector_api
from . import connection as connection_api
from . import constants as constants_api
from . import errors as errors_api
from . import host_sources as host_sources_api
from . import identity as identity_api
from . import locks as locks_api
from . import models as models_api
from . import providers as providers_api
from . import publication as publication_api
from . import registry as registry_api
from . import scheduler as scheduler_api
from . import snapshots as snapshots_api
from . import split_state as split_state_api
from . import state as state_api
from . import validation as validation_api
from . import volumes as volumes_api


def _scan_is_recent(config_root: Path) -> bool:
    status = snapshots_api.load_aggregate_status(config_root)
    if not status or not isinstance(status.get("updated_at"), str):
        return False
    try:
        timestamp = datetime.fromisoformat(status["updated_at"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return time.time() - timestamp < constants_api.MIN_INTERVAL_SECONDS


def _mark_volume_fingerprint_conflict(
    config_root: Path,
    record: models_api.VolumeRegistration,
) -> None:
    updated = replace(
        record,
        status="needs-adoption",
        last_error="volume fingerprint changed; run cage monitor add explicitly",
        last_scan_at=state_api._now(),
    )
    registry_api.update_registration(config_root, updated)


def _checked_volume_fingerprint(
    config_root: Path,
    docker: str,
    record: models_api.VolumeRegistration,
) -> dict[str, str]:
    if record.target == "host":
        # A host registration's fingerprint identifies the source auth root
        # for automatic launch matching.  Collection reads the immutable
        # Cage-managed session home instead, so an intentional source-root
        # replacement cannot make a later full reconciliation inspect the new
        # shared home or discard already-isolated Cage session history.
        host_sources_api._ensure_managed_host_home(config_root, record)
        return record.fingerprint
    current = volumes_api.volume_fingerprint(docker, record.volume_name)
    if current != record.fingerprint:
        _mark_volume_fingerprint_conflict(config_root, record)
        raise errors_api.MonitorError(f"monitor volume changed for {record.display_name}")
    return current


def _collect_current_registration(
    config_root: Path,
    docker: str,
    install_root: Path,
    record: models_api.VolumeRegistration,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    interval_seconds: int,
    force: bool,
) -> tuple[models_api.VolumeRegistration, dict[str, Any], bool, bool]:
    """Refresh exactly one volume and return payload/content/metadata changes."""

    with locks_api._wait_for_volume_lock(config_root, record.logical_id):
        _checked_volume_fingerprint(config_root, docker, record)
        previous, previous_metadata_changed = snapshots_api._load_trusted_volume_snapshot(
            config_root, record
        )
        if (
            not force
            and previous is not None
            and snapshots_api._snapshot_is_recent(
                config_root,
                record,
                max_age_seconds=interval_seconds,
            )
        ):
            if previous_metadata_changed:
                # Promotion changes only the private display metadata.  Keep
                # the cached usage content and bring its identity metadata
                # forward so every later launch does not republish forever.
                snapshots_api._save_volume_snapshot(config_root, record, previous)
            return record, previous, False, previous_metadata_changed
        image = collector_api.ensure_collector_image(
            docker,
            install_root,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
        )
        payload = collector_api._run_collector(
            docker,
            image,
            record,
            config_root,
            uid=os.getuid() if uid is None else uid,
            gid=os.getgid() if gid is None else gid,
        )
        content_changed = previous is None or snapshots_api._summary_content_hash(previous) != snapshots_api._summary_content_hash(payload)
        snapshots_api._save_volume_snapshot(config_root, record, payload)
        return record, payload, content_changed, previous_metadata_changed


def _summaries_from_cached_or_collected(
    config_root: Path,
    docker: str,
    install_root: Path,
    active: list[models_api.VolumeRegistration],
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    overrides: dict[str, dict[str, Any]] | None = None,
    full: bool = False,
    reference_payload: dict[str, Any] | None = None,
) -> tuple[list[tuple[models_api.VolumeRegistration, dict[str, Any]]], bool]:
    """Use compatible trusted state and return whether stale windows refreshed."""

    overrides = overrides or {}
    cached: dict[str, dict[str, Any]] = {}
    missing: list[models_api.VolumeRegistration] = []
    period_windows_refreshed = False
    for record in active:
        _checked_volume_fingerprint(config_root, docker, record)
        override = overrides.get(record.logical_id)
        if override is not None and not full:
            payload = validation_api._validate_summary(override, record.device_id)
            if (
                reference_payload is None
                or accounting_api._same_period_windows(payload, reference_payload)
            ):
                cached[record.logical_id] = payload
                continue
            period_windows_refreshed = True
            missing.append(record)
            continue
        if not full:
            payload, _metadata_changed = snapshots_api._load_trusted_volume_snapshot(config_root, record)
            if payload is not None:
                if (
                    reference_payload is None
                    or accounting_api._same_period_windows(payload, reference_payload)
                ):
                    cached[record.logical_id] = payload
                    continue
                period_windows_refreshed = True
        missing.append(record)
    collected: list[tuple[models_api.VolumeRegistration, dict[str, Any]]] = []
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
        raise errors_api.MonitorError("monitor aggregate has no trusted snapshot for every active volume")
    summaries = [(record, cached[record.logical_id]) for record in active]
    newest_collected = next(
        (
            payload
            for record, payload in reversed(collected)
            if record.logical_id not in overrides
        ),
        None,
    )
    summaries, corrected = _repair_period_window_mismatch(
        config_root,
        docker,
        install_root,
        summaries,
        version=version,
        storage_policy=storage_policy,
        allow_build=allow_build,
        uid=uid,
        gid=gid,
        reference_payload=(
            newest_collected
            if newest_collected is not None
            else reference_payload
        ),
    )
    return summaries, period_windows_refreshed or corrected


def _collect_registered_summaries(
    config_root: Path,
    docker: str,
    install_root: Path,
    active: list[models_api.VolumeRegistration],
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[models_api.VolumeRegistration, dict[str, Any]]]:
    overrides = overrides or {}
    to_collect = [record for record in active if record.logical_id not in overrides]
    image = None
    if to_collect:
        image = collector_api.ensure_collector_image(
            docker,
            install_root,
            version=version,
            storage_policy=storage_policy,
            allow_build=allow_build,
        )
    result: list[tuple[models_api.VolumeRegistration, dict[str, Any]]] = []
    for record in active:
        with locks_api._wait_for_volume_lock(config_root, record.logical_id):
            _checked_volume_fingerprint(config_root, docker, record)
            payload = overrides.get(record.logical_id)
            if payload is None:
                assert image is not None
                payload = collector_api._run_collector(
                    docker,
                    image,
                    record,
                    config_root,
                    uid=os.getuid() if uid is None else uid,
                    gid=os.getgid() if gid is None else gid,
                )
            payload = validation_api._validate_summary(payload, record.device_id)
            snapshots_api._save_volume_snapshot(config_root, record, payload)
            result.append((record, payload))
    return result


def _repair_period_window_mismatch(
    config_root: Path,
    docker: str,
    install_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None,
    gid: int | None,
    reference_payload: dict[str, Any] | None = None,
) -> tuple[list[tuple[models_api.VolumeRegistration, dict[str, Any]]], bool]:
    """Retry only stale period observations once before an aggregate upload.

    A collector can begin just before the UTC reporting boundary and finish
    after it.  Its observations then legitimately use different day/month
    markers, but combining them would make a false aggregate.  The last
    collection is the newest observation, so reread only sources with a
    different marker.  A second mismatch is not guessed at: the existing
    aggregate guard remains fail-closed and the hub keeps its last good payload.
    """

    if not summaries:
        return summaries, False
    reference = (
        reference_payload
        if reference_payload is not None
        else summaries[-1][1]
    )
    stale = [
        record
        for record, payload in summaries
        if not accounting_api._same_period_windows(payload, reference)
    ]
    if not stale:
        return summaries, False
    refreshed = _collect_registered_summaries(
        config_root,
        docker,
        install_root,
        stale,
        version=version,
        storage_policy=storage_policy,
        allow_build=allow_build,
        uid=uid,
        gid=gid,
    )
    refreshed_by_id = {record.logical_id: payload for record, payload in refreshed}
    repaired = [
        (record, refreshed_by_id.get(record.logical_id, payload))
        for record, payload in summaries
    ]
    if any(
        not accounting_api._same_period_windows(payload, reference)
        for _record, payload in repaired
    ):
        raise errors_api.MonitorError("collector period windows changed during the aggregate scan")
    return repaired, True


def _add_previous_provider_payloads(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    split_payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    status: dict[str, Any],
    previous_status: dict[str, Any] | None,
    *,
    include_approved: bool = False,
    allowed_provider_ids: frozenset[str] | set[str] | None = None,
) -> None:
    previous_providers = (
        previous_status.get("providers")
        if isinstance(previous_status, dict)
        and isinstance(previous_status.get("providers"), dict)
        else {}
    )
    if allowed_provider_ids is None:
        allowed_provider_ids = (
            providers_api._approved_provider_ids(config_root)
            if include_approved
            else providers_api._active_provider_ids(config_root)
        )
    else:
        allowed_provider_ids = frozenset(allowed_provider_ids)
    current_providers = set(split_payloads)
    for raw_provider, previous_provider_status in previous_providers.items():
        provider = validation_api._provider_id_from_allowed(raw_provider, allowed_provider_ids)
        if provider is None or provider in current_providers:
            continue
        if not isinstance(previous_provider_status, dict):
            continue
        previous_device = previous_provider_status.get("device_id")
        expected_device = identity_api._provider_device_id_for(
            config_root, provider, allowed_provider_ids
        )
        if previous_device != expected_device:
            raise errors_api.MonitorError(
                "previous provider device identity was invalid; hub snapshot was preserved"
            )
        empty_payload, empty_status = aggregation_api._empty_provider_payload(
            config_root,
            summaries[0][1],
            expected_device,
            provider,
            allowed_provider_ids=allowed_provider_ids,
        )
        split_payloads[provider] = (empty_payload, empty_status)
        status.setdefault("providers", {})[provider] = empty_status
        status.setdefault("device_ids", []).append(expected_device)
    status["device_ids"] = sorted(set(status.get("device_ids", [])))


def _mark_scan_success(
    config_root: Path,
    active: list[models_api.VolumeRegistration],
    success_at: str,
) -> list[models_api.VolumeRegistration]:
    active_ids = {item.logical_id for item in active}
    with locks_api._registry_write_lock(config_root):
        current = registry_api.load_registry(config_root)
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
        registry_api.save_registry(config_root, updated_all)
    return [item for item in updated_all if item.logical_id in active_ids]


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

    registrations = registry_api.load_registry(config_root)
    active = [item for item in registrations if item.status == "active"]
    if not active:
        raise errors_api.MonitorError("no active Token Monitor projects")
    with scheduler_api.try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            raise errors_api.MonitorError("monitor aggregate scan already running")
        with locks_api.try_aggregate_lock(config_root) as acquired:
            if not acquired:
                raise errors_api.MonitorError("monitor aggregate scan already running")
            try:
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
                summaries, _period_windows_refreshed = _repair_period_window_mismatch(
                    config_root,
                    docker,
                    install_root,
                    summaries,
                    version=version,
                    storage_policy=storage_policy,
                    allow_build=allow_build,
                    uid=uid,
                    gid=gid,
                )
                _payloads, manifest = aggregation_api.aggregate_provider_summaries(config_root, summaries)
                return manifest
            except Exception as exc:
                safe_error = registry_api._scan_error_for_records(config_root, active, str(exc))
                if safe_error != str(exc):
                    raise errors_api.MonitorError(safe_error) from exc
                raise


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
) -> tuple[list[models_api.VolumeRegistration], dict[str, Any]]:
    """Force one serialized full reconciliation of every active volume."""

    connection = connection_api.load_connection(config_root)
    if connection is None or not connection.enabled:
        raise errors_api.MonitorError("Token Monitor is not connected")
    with scheduler_api.try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            raise errors_api.MonitorError("monitor aggregate scan already running")
        with locks_api.try_aggregate_lock(config_root) as acquired:
            if not acquired:
                raise errors_api.MonitorError("monitor aggregate scan already running")
            registrations = registry_api.load_registry(config_root)
            active = [item for item in registrations if item.status == "active"]
            if not active:
                raise errors_api.MonitorError("no active Token Monitor projects")
            if not migration and providers_api.provider_label_migration_pending(config_root):
                raise errors_api.MonitorError(
                    "provider label migration is pending; run cage monitor provider status"
                )
            if not migration and split_state_api.provider_split_pending(config_root, connection):
                raise errors_api.MonitorError(
                    "provider split migration is pending; run cage monitor migrate --yes"
                )
            if not force and _scan_is_recent(config_root):
                return active, {}
            scheduler = scheduler_api.load_scheduler_state(config_root)
            scheduled_at = scheduler_api._begin_full_reconciliation(
                config_root,
                scheduler,
                time.time(),
                forced=force,
            )
            try:
                previous_status = snapshots_api.load_aggregate_status(config_root)
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
                summaries, _period_windows_refreshed = _repair_period_window_mismatch(
                    config_root,
                    docker,
                    install_root,
                    summaries,
                    version=version,
                    storage_policy=storage_policy,
                    allow_build=allow_build,
                    uid=uid,
                    gid=gid,
                )
                split_payloads, status = aggregation_api.aggregate_provider_summaries(config_root, summaries)
                _add_previous_provider_payloads(
                    config_root,
                    summaries,
                    split_payloads,
                    status,
                    previous_status,
                )
                status["split_complete"] = False if migration else True
                status = publication_api._publish_provider_payloads(
                    config_root,
                    connection,
                    split_payloads,
                    status,
                    previous_status,
                )
                if not migration:
                    split_state_api._mark_split_complete(config_root, status)
                success_at = state_api._now()
                updated_all = _mark_scan_success(config_root, active, success_at)
                generation = status.get("generation")
                if not isinstance(generation, str):
                    raise errors_api.MonitorError("monitor upload omitted its generation")
                scheduler_api._finish_full_reconciliation(
                    config_root,
                    scheduler,
                    scheduled_at=scheduled_at,
                    generation=generation,
                    now=time.time(),
                )
                return updated_all, status
            except Exception as exc:
                safe_error = registry_api._scan_error_for_records(config_root, active, str(exc))
                try:
                    scheduler_api._fail_full_reconciliation(config_root, scheduler, safe_error)
                except errors_api.MonitorError:
                    pass
                for record in active:
                    registry_api._record_scan_error(config_root, record, safe_error)
                if isinstance(exc, errors_api.MonitorError):
                    if safe_error != str(exc):
                        raise errors_api.MonitorError(safe_error) from exc
                    raise
                raise errors_api.MonitorError("Token Monitor full reconciliation failed") from exc


def scan_registration(
    config_root: Path,
    docker: str,
    install_root: Path,
    record: models_api.VolumeRegistration,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
    uid: int | None = None,
    gid: int | None = None,
    force: bool = False,
    final: bool = False,
) -> tuple[models_api.VolumeRegistration, dict[str, Any]]:
    """Refresh one current volume, then merge it with trusted cached volumes.

    A final lifecycle refresh is deliberately current-volume-only: it may
    publish already-trusted peer snapshots and reread a stale reporting-period
    peer, but it never starts the bounded host-wide safety reconciliation or
    collects a peer with no snapshot.
    """

    connection = connection_api.load_connection(config_root)
    if connection is None or not connection.enabled:
        raise errors_api.MonitorError("Token Monitor is not connected")
    registrations = registry_api.load_registry(config_root)
    current = next(
        (item for item in registrations if item.logical_id == record.logical_id),
        None,
    )
    if current is None or current.status != "active":
        raise errors_api.MonitorError("monitor project is not active")
    try:
        if providers_api.provider_label_migration_pending(config_root):
            raise errors_api.MonitorError(
                "provider label migration is pending; run cage monitor provider status"
            )
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
        safe_error = registry_api._scan_error_for_records(config_root, [current], str(exc))
        registry_api._record_scan_error(config_root, current, safe_error)
        if isinstance(exc, errors_api.MonitorError):
            if safe_error != str(exc):
                raise errors_api.MonitorError(safe_error) from exc
            raise
        raise errors_api.MonitorError("Token Monitor current-volume refresh failed") from exc

    with scheduler_api.try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            return refreshed, snapshots_api.load_aggregate_status(config_root) or {}
        with locks_api.try_aggregate_lock(config_root) as acquired:
            if not acquired:
                return refreshed, snapshots_api.load_aggregate_status(config_root) or {}
            active = [refreshed]
            full_due = False
            try:
                registrations = registry_api.load_registry(config_root)
                active = [item for item in registrations if item.status == "active"]
                if not active or not any(item.logical_id == refreshed.logical_id for item in active):
                    raise errors_api.MonitorError("monitor project is not active")
                if split_state_api.provider_split_pending(config_root, connection):
                    raise errors_api.MonitorError(
                        "provider split migration is pending; run cage monitor migrate --yes"
                    )
                previous_status = snapshots_api.load_aggregate_status(config_root)
                cache_complete = True
                for item in active:
                    cached, _metadata = snapshots_api._load_trusted_volume_snapshot(config_root, item)
                    if cached is None:
                        cache_complete = False
                        break
                if final and not cache_complete:
                    # The current volume is safely refreshed above.  Do not turn
                    # process shutdown into an all-volume scan merely because a
                    # peer has no local snapshot yet; a future coordinator owner
                    # can reconcile that peer normally.
                    return refreshed, previous_status or {}
                scheduler = scheduler_api.load_scheduler_state(config_root)
                now = time.time()
                full_due = (
                    not final
                    and not force
                    and scheduler_api._full_reconciliation_due(scheduler, now)
                )
                scheduled_at: float | None = None
                if full_due:
                    scheduled_at = scheduler_api._begin_full_reconciliation(config_root, scheduler, now)
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
                    newest_collected = next(
                        (
                            payload
                            for item, payload in reversed(summaries)
                            if item.logical_id != refreshed.logical_id
                        ),
                        current_payload,
                    )
                    summaries, period_windows_refreshed = _repair_period_window_mismatch(
                        config_root,
                        docker,
                        install_root,
                        summaries,
                        version=version,
                        storage_policy=storage_policy,
                        allow_build=allow_build,
                        uid=uid,
                        gid=gid,
                        reference_payload=newest_collected,
                    )
                else:
                    summaries, period_windows_refreshed = _summaries_from_cached_or_collected(
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
                        reference_payload=current_payload,
                    )
                should_publish = bool(
                    force
                    or full_due
                    or content_changed
                    or metadata_changed
                    or not cache_complete
                    or period_windows_refreshed
                    or previous_status is None
                    or publication_api.load_upload_state(config_root) is not None
                )
                if not should_publish:
                    return refreshed, previous_status or {}
                split_payloads, status = aggregation_api.aggregate_provider_summaries(config_root, summaries)
                _add_previous_provider_payloads(
                    config_root,
                    summaries,
                    split_payloads,
                    status,
                    previous_status,
                )
                status["split_complete"] = True
                status = publication_api._publish_provider_payloads(
                    config_root,
                    connection,
                    split_payloads,
                    status,
                    previous_status,
                )
                split_state_api._mark_split_complete(config_root, status)
                updated_all = _mark_scan_success(config_root, active, state_api._now())
                result = next(
                    item for item in updated_all if item.logical_id == refreshed.logical_id
                )
                if full_due:
                    generation = status.get("generation")
                    if not isinstance(generation, str) or scheduled_at is None:
                        raise errors_api.MonitorError("monitor upload omitted its generation")
                    scheduler_api._finish_full_reconciliation(
                        config_root,
                        scheduler,
                        scheduled_at=scheduled_at,
                        generation=generation,
                        now=time.time(),
                    )
                return result, status
            except Exception as exc:
                safe_error = registry_api._scan_error_for_records(config_root, active, str(exc))
                if full_due:
                    try:
                        scheduler_api._fail_full_reconciliation(config_root, scheduler, safe_error)
                    except errors_api.MonitorError:
                        pass
                for item in active:
                    registry_api._record_scan_error(config_root, item, safe_error)
                if isinstance(exc, errors_api.MonitorError):
                    if safe_error != str(exc):
                        raise errors_api.MonitorError(safe_error) from exc
                    raise
                raise errors_api.MonitorError("Token Monitor aggregate update failed") from exc
