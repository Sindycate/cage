"""Owner-approved provider labels and their activation lifecycle."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

from . import constants as constants_api
from . import errors as errors_api
from . import identity as identity_api
from . import state as state_api
from . import validation as validation_api


def _validate_provider_label_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > constants_api.MAX_PROVIDER_LABELS:
        raise errors_api.MonitorError("monitor provider-label state is invalid")
    labels: list[str] = []
    for raw_label in value:
        label = validation_api._provider_slug(raw_label)
        if label is None or label != raw_label or label in constants_api.PUBLIC_PROVIDER_IDS:
            raise errors_api.MonitorError("monitor provider-label state is invalid")
        labels.append(label)
    if len(set(labels)) != len(labels):
        raise errors_api.MonitorError("monitor provider-label state is invalid")
    return sorted(labels)


def _empty_provider_labels() -> dict[str, object]:
    return {"version": constants_api.PROVIDER_LABELS_VERSION, "approved": [], "active": []}


def _validate_provider_labels(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"version", "approved", "active"}:
        raise errors_api.MonitorError("monitor provider-label state is invalid")
    if value["version"] != constants_api.PROVIDER_LABELS_VERSION:
        raise errors_api.MonitorError("monitor provider-label state has an invalid version")
    approved = _validate_provider_label_list(value["approved"], field="approved")
    active = _validate_provider_label_list(value["active"], field="active")
    if not set(active).issubset(approved):
        raise errors_api.MonitorError("monitor provider-label state is invalid")
    return {
        "version": constants_api.PROVIDER_LABELS_VERSION,
        "approved": approved,
        "active": active,
    }


def load_provider_labels(config_root: Path) -> dict[str, object]:
    """Load owner-approved readable provider labels from private state."""

    value = state_api._read_json(
        state_api.monitor_root(config_root) / constants_api.PROVIDER_LABELS_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return _empty_provider_labels()
    return _validate_provider_labels(value)


def save_provider_labels(config_root: Path, value: dict[str, object]) -> None:
    state_api._write_json(
        state_api.monitor_root(config_root) / constants_api.PROVIDER_LABELS_FILE,
        _validate_provider_labels(value),
    )


def _approved_provider_ids(config_root: Path) -> frozenset[str]:
    labels = load_provider_labels(config_root)
    approved = labels["approved"]
    assert isinstance(approved, list)
    return frozenset(constants_api.PUBLIC_PROVIDER_IDS | set(approved))


def _active_provider_ids(config_root: Path) -> frozenset[str]:
    labels = load_provider_labels(config_root)
    active = labels["active"]
    assert isinstance(active, list)
    return frozenset(constants_api.PUBLIC_PROVIDER_IDS | set(active))


def _public_provider_id(
    config_root: Path,
    value: object,
    *,
    include_approved: bool = False,
) -> str | None:
    """Return an owner-approved provider name that may reach the hub.

    An arbitrary session label never becomes public merely because it is a
    syntactically safe slug.  Built-ins are active by default; a custom label
    becomes active only after explicit local approval and verified migration.
    """

    allowed = (
        _approved_provider_ids(config_root)
        if include_approved
        else _active_provider_ids(config_root)
    )
    return validation_api._provider_id_from_allowed(value, allowed)


def provider_device_id(
    config_root: Path,
    provider: str,
    *,
    include_approved: bool = False,
) -> str:
    """Return a readable device identity for an active provider stream.

    ``include_approved`` is reserved for the verified migration path.  It
    never makes a pending label available to normal scans.
    """

    allowed = (
        _approved_provider_ids(config_root)
        if include_approved
        else _active_provider_ids(config_root)
    )
    return identity_api._provider_device_id_for(config_root, provider, allowed)


def _validated_custom_provider_label(value: object) -> str:
    label = validation_api._provider_slug(value)
    if label is None or label in constants_api.PUBLIC_PROVIDER_IDS:
        raise errors_api.MonitorError("invalid custom monitor provider label")
    return label


def _validate_provider_label_migration(
    config_root: Path,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "label",
        "state",
        "baseline_unattributed_tokens",
        "label_tokens",
        "residual_unattributed_tokens",
        "created_at",
        "updated_at",
    }:
        raise errors_api.MonitorError("monitor provider-label migration state is invalid")
    if value["version"] != constants_api.PROVIDER_LABEL_MIGRATION_VERSION:
        raise errors_api.MonitorError("monitor provider-label migration has an invalid version")
    label = _validated_custom_provider_label(value["label"])
    approved = _approved_provider_ids(config_root)
    if label not in approved:
        raise errors_api.MonitorError("monitor provider-label migration is not approved")
    if value["state"] not in {"prepared", "repartitioned"}:
        raise errors_api.MonitorError("monitor provider-label migration state is invalid")
    totals: dict[str, float] = {}
    for name in (
        "baseline_unattributed_tokens",
        "label_tokens",
        "residual_unattributed_tokens",
    ):
        raw_total = value[name]
        if (
            type(raw_total) not in (int, float)
            or not math.isfinite(raw_total)
            or raw_total < 0
        ):
            raise errors_api.MonitorError("monitor provider-label migration state is invalid")
        totals[name] = float(raw_total)
    for name in ("created_at", "updated_at"):
        if not isinstance(value[name], str) or len(value[name]) > 128:
            raise errors_api.MonitorError("monitor provider-label migration state is invalid")
    return {
        "version": constants_api.PROVIDER_LABEL_MIGRATION_VERSION,
        "label": label,
        "state": value["state"],
        **totals,
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
    }


def load_provider_label_migration(config_root: Path) -> dict[str, object] | None:
    value = state_api._read_json(
        state_api.monitor_root(config_root) / constants_api.PROVIDER_LABEL_MIGRATION_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    return _validate_provider_label_migration(config_root, value)


def save_provider_label_migration(config_root: Path, value: dict[str, object]) -> None:
    state_api._write_json(
        state_api.monitor_root(config_root) / constants_api.PROVIDER_LABEL_MIGRATION_FILE,
        _validate_provider_label_migration(config_root, value),
    )


def remove_provider_label_migration(config_root: Path) -> None:
    state_api._remove_private_file(
        state_api.monitor_root(config_root) / constants_api.PROVIDER_LABEL_MIGRATION_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )


def approve_provider_label(config_root: Path, value: object) -> str:
    """Approve one custom label locally without changing hub state."""

    label = _validated_custom_provider_label(value)
    migration = load_provider_label_migration(config_root)
    if migration is not None and migration["label"] != label:
        raise errors_api.MonitorError("another monitor provider-label migration is pending")
    labels = load_provider_labels(config_root)
    approved = set(labels["approved"])
    active = set(labels["active"])
    pending = approved - active
    if pending and label not in pending:
        raise errors_api.MonitorError("another monitor provider-label migration is pending")
    if label not in approved:
        approved.add(label)
        save_provider_labels(
            config_root,
            {
                "version": constants_api.PROVIDER_LABELS_VERSION,
                "approved": sorted(approved),
                "active": sorted(active),
            },
        )
    return label


def _activate_provider_label(config_root: Path, label: str) -> None:
    label = _validated_custom_provider_label(label)
    labels = load_provider_labels(config_root)
    approved = set(labels["approved"])
    active = set(labels["active"])
    if label not in approved:
        raise errors_api.MonitorError("monitor provider label is not approved")
    if label in active:
        return
    active.add(label)
    save_provider_labels(
        config_root,
        {
            "version": constants_api.PROVIDER_LABELS_VERSION,
            "approved": sorted(approved),
            "active": sorted(active),
        },
    )


def provider_label_status(config_root: Path) -> dict[str, object]:
    """Return local-only approval and migration state for CLI status."""

    labels = load_provider_labels(config_root)
    approved = list(labels["approved"])
    active = list(labels["active"])
    return {
        "approved": approved,
        "active": active,
        "pending": sorted(set(approved) - set(active)),
        "migration": load_provider_label_migration(config_root),
    }


def provider_label_migration_pending(config_root: Path) -> bool:
    labels = load_provider_labels(config_root)
    if set(labels["approved"]) != set(labels["active"]):
        return True
    return load_provider_label_migration(config_root) is not None


def provider_device_ids(config_root: Path, providers: Iterator[str] | list[str] | set[str]) -> list[str]:
    return [provider_device_id(config_root, provider) for provider in sorted(set(providers))]
