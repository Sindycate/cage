"""Public Token Monitor API. Implementation lives in :mod:`cage_core.monitoring`."""

from .monitoring.accounting import (
    session_provider,
)
from .monitoring.aggregation import (
    aggregate_provider_summaries,
    aggregate_summaries,
)
from .monitoring.cleanup import (
    remove_aggregate_status,
    remove_device_state,
    remove_project_state,
)
from .monitoring.collector import (
    collector_image,
    collector_registry_image,
    ensure_collector_image,
)
from .monitoring.connection import (
    disable_connection,
    load_connection,
    save_connection,
)
from .monitoring.constants import (
    COLLECTOR_DOCKERFILE,
    COLLECTOR_IMAGE,
    COLLECTOR_REGISTRY,
    COLLECTOR_SOURCE_COMMIT,
    COLLECTOR_SOURCE_SHA256,
    COLLECTOR_SOURCE_URL,
    COLLECTOR_SOURCE_VERSION,
    FULL_RECONCILIATION_INTERVAL_SECONDS,
)
from .monitoring.errors import (
    MonitorError,
)
from .monitoring.host_sources import (
    finish_host_source,
    host_source_home,
    host_source_logical_id,
    prepare_host_source,
)
from .monitoring.hub import (
    delete_device,
    verify_connection,
)
from .monitoring.identity import (
    device_id_for,
    host_device_id,
    logical_target_id,
    project_id_for,
)
from .monitoring.lifecycle import (
    ActiveMonitor,
)
from .monitoring.locks import (
    HostSourceLease,
)
from .monitoring.migrations import (
    migrate_legacy_devices,
    migrate_provider_label,
)
from .monitoring.models import (
    HostSourceSession,
    MonitorConnection,
    VolumeRegistration,
)
from .monitoring.pricing import (
    load_pricing,
    remove_model_pricing,
    save_pricing,
    set_model_pricing,
)
from .monitoring.providers import (
    approve_provider_label,
    load_provider_label_migration,
    load_provider_labels,
    provider_device_id,
    provider_device_ids,
    provider_label_migration_pending,
    provider_label_status,
    save_provider_label_migration,
    save_provider_labels,
)
from .monitoring.publication import (
    load_upload_state,
    save_upload_state,
)
from .monitoring.registry import (
    clear_legacy_device_id,
    disable_all_registrations,
    disable_host_source,
    discover_codex_volumes,
    load_registry,
    recovered_repository,
    register_host_source,
    register_recovered_volume,
    register_volume,
    registered_host_source,
    retire_registration,
    save_registry,
    update_registration,
)
from .monitoring.scheduler import (
    load_scheduler_state,
    save_scheduler_state,
    try_coordinator_lease,
)
from .monitoring.service import (
    preview_provider_split,
    scan_all_registrations,
    scan_registration,
)
from .monitoring.snapshots import (
    load_aggregate_status,
    load_split_status,
    load_volume_snapshot,
)
from .monitoring.split_state import (
    provider_split_pending,
    save_split_status,
)
from .monitoring.state import (
    monitor_root,
)
from .monitoring.validation import (
    normalize_hub_url,
    provider_display_name,
    validate_device_id,
    validate_display_name,
    validate_interval,
    validate_logical_id,
    validate_volume_name,
)
from .monitoring.volumes import (
    ensure_codex_volume,
    ensure_codex_volume_labels,
    volume_fingerprint,
)

__all__ = [
    "remove_aggregate_status",
    "ActiveMonitor",
    "COLLECTOR_DOCKERFILE",
    "COLLECTOR_IMAGE",
    "COLLECTOR_REGISTRY",
    "COLLECTOR_SOURCE_COMMIT",
    "COLLECTOR_SOURCE_SHA256",
    "COLLECTOR_SOURCE_URL",
    "COLLECTOR_SOURCE_VERSION",
    "FULL_RECONCILIATION_INTERVAL_SECONDS",
    "HostSourceLease",
    "HostSourceSession",
    "MonitorConnection",
    "MonitorError",
    "VolumeRegistration",
    "collector_image",
    "collector_registry_image",
    "aggregate_summaries",
    "aggregate_provider_summaries",
    "approve_provider_label",
    "clear_legacy_device_id",
    "delete_device",
    "device_id_for",
    "disable_all_registrations",
    "disable_connection",
    "disable_host_source",
    "ensure_codex_volume",
    "ensure_codex_volume_labels",
    "ensure_collector_image",
    "load_connection",
    "load_aggregate_status",
    "load_scheduler_state",
    "load_upload_state",
    "load_volume_snapshot",
    "load_pricing",
    "load_provider_label_migration",
    "load_provider_labels",
    "load_registry",
    "load_split_status",
    "logical_target_id",
    "monitor_root",
    "host_device_id",
    "host_source_home",
    "host_source_logical_id",
    "provider_device_id",
    "provider_device_ids",
    "provider_display_name",
    "provider_label_migration_pending",
    "provider_label_status",
    "provider_split_pending",
    "preview_provider_split",
    "normalize_hub_url",
    "register_volume",
    "register_host_source",
    "registered_host_source",
    "register_recovered_volume",
    "discover_codex_volumes",
    "recovered_repository",
    "remove_device_state",
    "remove_model_pricing",
    "remove_project_state",
    "retire_registration",
    "save_connection",
    "save_pricing",
    "save_provider_label_migration",
    "save_provider_labels",
    "save_registry",
    "save_scheduler_state",
    "save_split_status",
    "save_upload_state",
    "scan_registration",
    "scan_all_registrations",
    "set_model_pricing",
    "session_provider",
    "try_coordinator_lease",
    "prepare_host_source",
    "finish_host_source",
    "migrate_legacy_devices",
    "migrate_provider_label",
    "project_id_for",
    "update_registration",
    "validate_interval",
    "validate_device_id",
    "validate_display_name",
    "validate_logical_id",
    "validate_volume_name",
    "verify_connection",
    "volume_fingerprint",
]
