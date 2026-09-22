"""Resumable legacy-device and provider-label migrations."""

from __future__ import annotations

import math
from pathlib import Path
from dataclasses import replace
from typing import Any

from . import accounting as accounting_api
from . import aggregation as aggregation_api
from . import cleanup as cleanup_api
from . import connection as connection_api
from . import constants as constants_api
from . import errors as errors_api
from . import hub as hub_api
from . import identity as identity_api
from . import locks as locks_api
from . import models as models_api
from . import providers as providers_api
from . import publication as publication_api
from . import registry as registry_api
from . import scheduler as scheduler_api
from . import service as service_api
from . import snapshots as snapshots_api
from . import split_state as split_state_api
from . import state as state_api


def migrate_legacy_devices(
    config_root: Path,
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
) -> int:
    connection = connection_api.load_connection(config_root)
    if connection is None or not connection.enabled:
        raise errors_api.MonitorError("Token Monitor is not connected")
    pending = [item for item in registry_api.load_registry(config_root) if item.legacy_device_id]
    legacy_device = identity_api.host_device_id(config_root)
    split_state = snapshots_api.load_split_status(config_root)
    hub_stats = hub_api._hub_stats(connection)
    hub_devices = hub_api._hub_device_ids_from_stats(hub_stats)
    split_pending = (
        not (split_state and split_state.get("complete") is True)
        and legacy_device in hub_devices
    )
    old_total = (
        hub_api._hub_device_total_from_stats(hub_stats, legacy_device)
        if split_pending
        else None
    )
    if not pending and not split_pending:
        return 0
    _updated, manifest = service_api.scan_all_registrations(
        config_root,
        docker,
        install_root,
        version=version,
        storage_policy=storage_policy,
        allow_build=True,
        force=True,
        migration=True,
    )
    visible_stats = hub_api._hub_stats(connection)
    visible = hub_api._hub_device_ids_from_stats(visible_stats)
    expected_provider_devices = set(manifest.get("device_ids", []))
    if not expected_provider_devices.issubset(visible):
        raise errors_api.MonitorError(
            "new provider Cage devices were not visible on the hub; legacy devices were preserved"
        )
    for provider, provider_status in manifest.get("providers", {}).items():
        if not isinstance(provider_status, dict):
            raise errors_api.MonitorError(
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
            raise errors_api.MonitorError(
                "provider token total was invalid; legacy devices were preserved"
            )
        hub_total = hub_api._hub_device_total_from_stats(visible_stats, provider_device)
        if hub_total is None or round(hub_total) != round(float(provider_total)):
            raise errors_api.MonitorError(
                f"provider device {provider_device} did not reconcile; legacy devices were preserved"
            )
    if split_pending:
        new_total = manifest.get("total_tokens")
        if old_total is None or type(new_total) not in (int, float):
            raise errors_api.MonitorError(
                "cannot verify the old aggregate token total; legacy device was preserved"
            )
        if round(float(old_total)) != round(float(new_total)):
            raise errors_api.MonitorError(
                "provider split token total does not match the old aggregate; legacy device was preserved"
            )
        hub_api.delete_device(connection, legacy_device)
        split_state_api._mark_split_complete(
            config_root,
            manifest,
            legacy_device_id=legacy_device,
        )
        status = snapshots_api.load_aggregate_status(config_root)
        if isinstance(status, dict):
            status["split_complete"] = True
            state_api._write_json(state_api.monitor_root(config_root) / constants_api.AGGREGATE_STATUS_FILE, status)
    deleted = 0
    for pending_record in pending:
        legacy_id = pending_record.legacy_device_id
        hub_api.delete_device(connection, legacy_id)
        with locks_api._registry_write_lock(config_root):
            registrations = registry_api.load_registry(config_root)
            current = next(
                (item for item in registrations if item.logical_id == pending_record.logical_id),
                None,
            )
            if current is None or current.legacy_device_id != legacy_id:
                raise errors_api.MonitorError("monitor registry changed during migration")
            registry_api.save_registry(
                config_root,
                [replace(item, legacy_device_id="") if item.logical_id == current.logical_id else item for item in registrations],
            )
        cleanup_api.remove_device_state(config_root, legacy_id)
        deleted += 1
    if split_pending:
        deleted += 1
    return deleted


def _provider_stream_total(
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    provider: str,
) -> float:
    value = payloads.get(provider)
    if value is None:
        return 0.0
    status = value[1]
    total = status.get("total_tokens")
    if (
        type(total) not in (int, float)
        or not math.isfinite(total)
        or total < 0
    ):
        raise errors_api.MonitorError("monitor provider migration has an invalid stream total")
    return float(total)


def _same_token_total(left: float, right: float) -> bool:
    """Compare collector and hub token counters at their integer boundary."""

    return round(left) == round(right)


def _ensure_provider_stream(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    status: dict[str, Any],
    provider: str,
    *,
    allowed_provider_ids: frozenset[str] | set[str],
) -> None:
    """Ensure an intentional zero stream has a complete replacement payload."""

    if provider in payloads:
        return
    if not summaries:
        raise errors_api.MonitorError("no active Token Monitor projects")
    allowed_provider_ids = frozenset(allowed_provider_ids)
    device_id = identity_api._provider_device_id_for(
        config_root, provider, allowed_provider_ids
    )
    payload, stream_status = aggregation_api._empty_provider_payload(
        config_root,
        summaries[0][1],
        device_id,
        provider,
        allowed_provider_ids=allowed_provider_ids,
    )
    payloads[provider] = (payload, stream_status)
    providers = status.get("providers")
    if not isinstance(providers, dict):
        raise errors_api.MonitorError("monitor provider migration status is invalid")
    providers[provider] = stream_status
    device_ids = status.get("device_ids")
    if not isinstance(device_ids, list):
        raise errors_api.MonitorError("monitor provider migration status is invalid")
    status["device_ids"] = sorted(set(device_ids) | {device_id})


def _provider_label_migration_record(
    label: str,
    *,
    state: str,
    baseline_unattributed_tokens: float,
    label_tokens: float,
    residual_unattributed_tokens: float,
    created_at: str | None = None,
) -> dict[str, object]:
    now = state_api._now()
    return {
        "version": constants_api.PROVIDER_LABEL_MIGRATION_VERSION,
        "label": label,
        "state": state,
        "baseline_unattributed_tokens": baseline_unattributed_tokens,
        "label_tokens": label_tokens,
        "residual_unattributed_tokens": residual_unattributed_tokens,
        "created_at": created_at or now,
        "updated_at": now,
    }


def _provider_label_migration_matches(
    marker: dict[str, object],
    label: str,
    *,
    baseline_unattributed_tokens: float,
    label_tokens: float,
    residual_unattributed_tokens: float,
) -> bool:
    if marker.get("label") != label:
        return False
    for name, total in (
        ("baseline_unattributed_tokens", baseline_unattributed_tokens),
        ("label_tokens", label_tokens),
        ("residual_unattributed_tokens", residual_unattributed_tokens),
    ):
        saved = marker.get(name)
        if type(saved) not in (int, float) or not _same_token_total(float(saved), total):
            return False
    return True


def _verify_provider_label_hub_totals(
    stats: dict[str, Any],
    *,
    label_device_id: str,
    label_tokens: float,
    unattributed_device_id: str,
    unattributed_tokens: float,
) -> None:
    named_total = hub_api._hub_device_total_from_stats(stats, label_device_id)
    if named_total is None or not _same_token_total(named_total, label_tokens):
        raise errors_api.MonitorError(
            "named provider stream did not match the fresh local total; hub state was preserved"
        )
    unattributed_total = hub_api._hub_device_total_from_stats(stats, unattributed_device_id)
    if unattributed_total is None or not _same_token_total(
        unattributed_total, unattributed_tokens
    ):
        raise errors_api.MonitorError(
            "Unattributed provider stream did not match the fresh local total; hub state was preserved"
        )


def _verify_provider_payload_totals(
    stats: dict[str, Any],
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    for provider, (_payload, stream_status) in payloads.items():
        device_id = stream_status.get("device_id")
        total = stream_status.get("total_tokens")
        if (
            not isinstance(device_id, str)
            or type(total) not in (int, float)
            or not math.isfinite(total)
            or total < 0
        ):
            raise errors_api.MonitorError("monitor provider migration status is invalid")
        hub_total = hub_api._hub_device_total_from_stats(stats, device_id)
        if hub_total is None or not _same_token_total(hub_total, float(total)):
            raise errors_api.MonitorError(
                "provider migration did not verify every hub stream; recovery remains pending"
            )


def migrate_provider_label(
    config_root: Path,
    docker: str,
    install_root: Path,
    label: str,
    *,
    version: str,
    storage_policy: object,
) -> dict[str, Any]:
    """Activate one explicitly approved custom stream through a verified move.

    The named hub device is never deleted or recreated.  A fresh scan produces
    a baseline (the label still in ``Unattributed``) and a proposed split.  We
    first prove the existing named and unattributed totals, replace only the
    unattributed payload, and then commit a complete local generation while
    deliberately leaving those two already-verified hub devices untouched.
    """

    label = providers_api._validated_custom_provider_label(label)
    connection = connection_api.load_connection(config_root)
    if connection is None or not connection.enabled:
        raise errors_api.MonitorError("Token Monitor is not connected")
    with scheduler_api.try_coordinator_lease(config_root) as coordinator:
        if not coordinator:
            raise errors_api.MonitorError("monitor aggregate scan already running")
        with locks_api.try_aggregate_lock(config_root) as acquired:
            if not acquired:
                raise errors_api.MonitorError("monitor aggregate scan already running")
            labels = providers_api.load_provider_labels(config_root)
            approved = set(labels["approved"])
            active = set(labels["active"])
            marker = providers_api.load_provider_label_migration(config_root)
            if label not in approved:
                raise errors_api.MonitorError(
                    "provider label is not approved; run cage monitor provider allow LABEL first"
                )
            if approved - active - {label}:
                raise errors_api.MonitorError("another monitor provider-label migration is pending")
            if marker is not None and marker.get("label") != label:
                raise errors_api.MonitorError("another monitor provider-label migration is pending")
            if label in active and marker is None:
                raise errors_api.MonitorError("monitor provider label is already active")
            if publication_api.load_upload_state(config_root) is not None:
                # A crash during the final local-generation commit can leave
                # an ordinary upload marker after the named and unattributed
                # devices have already been verified.  Repair that marker
                # here, under the same exclusive locks, so this explicit
                # migration remains resumable rather than deadlocking normal
                # scans behind its own pending-label guard.
                publication_api._repair_pending_upload(config_root, connection)
            registrations = registry_api.load_registry(config_root)
            active_records = [item for item in registrations if item.status == "active"]
            if not active_records:
                raise errors_api.MonitorError("no active Token Monitor projects")
            previous_status = snapshots_api.load_aggregate_status(config_root)
            # Validate the old rollback source before changing the hub's
            # unattributed stream.  A legacy private generation is accepted
            # only under its exact deterministic binding.
            publication_api._previous_generation(config_root, previous_status)
            summaries = service_api._collect_registered_summaries(
                config_root,
                docker,
                install_root,
                active_records,
                version=version,
                storage_policy=storage_policy,
                allow_build=True,
                uid=None,
                gid=None,
            )
            summaries, _period_windows_refreshed = service_api._repair_period_window_mismatch(
                config_root,
                docker,
                install_root,
                summaries,
                version=version,
                storage_policy=storage_policy,
                allow_build=True,
                uid=None,
                gid=None,
            )
            # Keep the baseline deliberately on the pre-migration partition.
            # If a prior attempt reached local activation before it stopped,
            # the active set already contains ``label``; exclude it here so
            # the persisted baseline remains stable and verifiable on retry.
            baseline_allowed_provider_ids = frozenset(
                providers_api._active_provider_ids(config_root) - {label}
            )
            proposed_allowed_provider_ids = providers_api._approved_provider_ids(config_root)
            baseline_payloads, baseline_status = aggregation_api._aggregate_provider_summaries_for_allowed(
                config_root, summaries, baseline_allowed_provider_ids
            )
            service_api._add_previous_provider_payloads(
                config_root,
                summaries,
                baseline_payloads,
                baseline_status,
                previous_status,
                allowed_provider_ids=baseline_allowed_provider_ids,
            )
            proposed_payloads, proposed_status = aggregation_api._aggregate_provider_summaries_for_allowed(
                config_root, summaries, proposed_allowed_provider_ids
            )
            service_api._add_previous_provider_payloads(
                config_root,
                summaries,
                proposed_payloads,
                proposed_status,
                previous_status,
                allowed_provider_ids=proposed_allowed_provider_ids,
            )
            # The existing named and unattributed devices are both expected
            # to exist.  Explicit zero payloads give the move a complete,
            # recoverable target when one side currently has no sessions.
            _ensure_provider_stream(
                config_root,
                summaries,
                baseline_payloads,
                baseline_status,
                constants_api.UNATTRIBUTED_PROVIDER,
                allowed_provider_ids=baseline_allowed_provider_ids,
            )
            _ensure_provider_stream(
                config_root,
                summaries,
                proposed_payloads,
                proposed_status,
                label,
                allowed_provider_ids=proposed_allowed_provider_ids,
            )
            _ensure_provider_stream(
                config_root,
                summaries,
                proposed_payloads,
                proposed_status,
                constants_api.UNATTRIBUTED_PROVIDER,
                allowed_provider_ids=proposed_allowed_provider_ids,
            )
            baseline_total = float(baseline_status.get("total_tokens", 0))
            proposed_total = float(proposed_status.get("total_tokens", 0))
            if not _same_token_total(baseline_total, proposed_total):
                raise errors_api.MonitorError(
                    "provider migration changed the deduplicated total; hub state was preserved"
                )
            for provider in set(baseline_payloads) | set(proposed_payloads):
                if provider in {label, constants_api.UNATTRIBUTED_PROVIDER}:
                    continue
                if not _same_token_total(
                    _provider_stream_total(baseline_payloads, provider),
                    _provider_stream_total(proposed_payloads, provider),
                ):
                    raise errors_api.MonitorError(
                        "provider migration changed an unrelated stream; hub state was preserved"
                    )
            baseline_unattributed = _provider_stream_total(
                baseline_payloads, constants_api.UNATTRIBUTED_PROVIDER
            )
            label_total = _provider_stream_total(proposed_payloads, label)
            residual_unattributed = _provider_stream_total(
                proposed_payloads, constants_api.UNATTRIBUTED_PROVIDER
            )
            if marker is not None and not _provider_label_migration_matches(
                marker,
                label,
                baseline_unattributed_tokens=baseline_unattributed,
                label_tokens=label_total,
                residual_unattributed_tokens=residual_unattributed,
            ):
                raise errors_api.MonitorError(
                    "provider-label migration inputs changed; wait for sessions to settle and retry"
                )
            if marker is None:
                marker = _provider_label_migration_record(
                    label,
                    state="prepared",
                    baseline_unattributed_tokens=baseline_unattributed,
                    label_tokens=label_total,
                    residual_unattributed_tokens=residual_unattributed,
                )
                providers_api.save_provider_label_migration(config_root, marker)
            label_device_id = providers_api.provider_device_id(
                config_root, label, include_approved=True
            )
            unattributed_device_id = providers_api.provider_device_id(config_root, constants_api.UNATTRIBUTED_PROVIDER)
            hub_stats = hub_api._hub_stats(connection)
            split_state = snapshots_api.load_split_status(config_root)
            if (
                not (split_state and split_state.get("complete") is True)
                and identity_api.host_device_id(config_root) in hub_api._hub_device_ids_from_stats(hub_stats)
            ):
                raise errors_api.MonitorError(
                    "provider split migration is pending; complete it before migrating a provider label"
                )
            named_total = hub_api._hub_device_total_from_stats(hub_stats, label_device_id)
            if named_total is None or not _same_token_total(named_total, label_total):
                raise errors_api.MonitorError(
                    "named provider stream did not match the fresh local total; hub state was preserved"
                )
            hub_unattributed = hub_api._hub_device_total_from_stats(
                hub_stats, unattributed_device_id
            )
            if hub_unattributed is None:
                raise errors_api.MonitorError(
                    "Unattributed provider stream was not visible on the hub; hub state was preserved"
                )
            marker_state = marker.get("state")
            if _same_token_total(hub_unattributed, residual_unattributed):
                if marker_state != "repartitioned":
                    marker = _provider_label_migration_record(
                        label,
                        state="repartitioned",
                        baseline_unattributed_tokens=baseline_unattributed,
                        label_tokens=label_total,
                        residual_unattributed_tokens=residual_unattributed,
                        created_at=str(marker["created_at"]),
                    )
                    providers_api.save_provider_label_migration(config_root, marker)
            elif _same_token_total(hub_unattributed, baseline_unattributed):
                # A prior attempt may have stopped before recording the phase
                # transition, or a later generation may have rolled back an
                # unrelated stream.  Re-enter the explicitly prepared phase.
                marker = _provider_label_migration_record(
                    label,
                    state="prepared",
                    baseline_unattributed_tokens=baseline_unattributed,
                    label_tokens=label_total,
                    residual_unattributed_tokens=residual_unattributed,
                    created_at=str(marker["created_at"]),
                )
                providers_api.save_provider_label_migration(config_root, marker)
                hub_api.upload_summary(
                    connection,
                    proposed_payloads[constants_api.UNATTRIBUTED_PROVIDER][0],
                    config_root=config_root,
                )
                repartitioned_stats = hub_api._hub_stats(connection)
                _verify_provider_label_hub_totals(
                    repartitioned_stats,
                    label_device_id=label_device_id,
                    label_tokens=label_total,
                    unattributed_device_id=unattributed_device_id,
                    unattributed_tokens=residual_unattributed,
                )
                marker = _provider_label_migration_record(
                    label,
                    state="repartitioned",
                    baseline_unattributed_tokens=baseline_unattributed,
                    label_tokens=label_total,
                    residual_unattributed_tokens=residual_unattributed,
                    created_at=str(marker["created_at"]),
                )
                providers_api.save_provider_label_migration(config_root, marker)
            else:
                raise errors_api.MonitorError(
                    "Unattributed provider stream did not match either verified migration total; hub state was preserved"
                )
            providers_api._activate_provider_label(config_root, label)
            proposed_status["split_complete"] = True
            published = publication_api._publish_provider_payloads(
                config_root,
                connection,
                proposed_payloads,
                proposed_status,
                previous_status,
                # These two streams have just been verified at the hub.  Do
                # not touch the named device again while the complete local
                # generation is committed for later ordinary rollbacks.
                skip_upload_for={label, constants_api.UNATTRIBUTED_PROVIDER},
            )
            _verify_provider_payload_totals(hub_api._hub_stats(connection), proposed_payloads)
            split_state_api._mark_split_complete(config_root, published)
            providers_api.remove_provider_label_migration(config_root)
            service_api._mark_scan_success(config_root, active_records, state_api._now())
            return published
