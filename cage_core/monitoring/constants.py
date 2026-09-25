"""Versioned state schemas, collector pin, privacy allowlists and resource limits."""

import re


STATE_VERSION = 1
REGISTRY_VERSION = 2
PRICING_VERSION = 1
SPLIT_STATUS_VERSION = 1
SCHEDULER_STATE_VERSION = 1
VOLUME_SNAPSHOT_VERSION = 2
UPLOAD_STATE_VERSION = 1
PROVIDER_LABELS_VERSION = 1
PROVIDER_LABEL_MIGRATION_VERSION = 1
COLLECTOR_IMAGE = "cage-token-monitor"
COLLECTOR_REGISTRY = "ghcr.io/sindycate/cage/token-monitor"
COLLECTOR_DOCKERFILE = "Dockerfile.monitor"
COLLECTOR_SOURCE_VERSION = "0.49.0"
COLLECTOR_SOURCE_COMMIT = "7c74e61fd8f9d592e647f14107738746a51e49ff"
COLLECTOR_SOURCE_SHA256 = "c2f72a31e372b495c0816af561ff789233e0cb2cae2e7e8098d686f9b7fd441e"
COLLECTOR_SOURCE_URL = (
    "https://github.com/Javis603/token-monitor/archive/refs/tags/"
    f"v{COLLECTOR_SOURCE_VERSION}.tar.gz"
)

MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 24 * 60 * 60
MAX_CONNECTION_BYTES = 16 * 1024
MAX_SECRET_BYTES = 8192
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
# The hub returns all devices plus aggregate periods, including after ingest.
# Its response is not a single-device upload and needs a separate bounded cap.
MAX_HUB_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
SCAN_TIMEOUT_SECONDS = 180
FULL_RECONCILIATION_INTERVAL_SECONDS = 60 * 60
COORDINATOR_LEASE_SECONDS = SCAN_TIMEOUT_SECONDS + 60
MAX_SNAPSHOT_BYTES = 2 * MAX_OUTPUT_BYTES
MAX_GENERATIONS = 4
COLLECTOR_MEMORY = "1g"
COLLECTOR_CPUS = "1.0"
COLLECTOR_PIDS = "128"
MAX_HOST_STATIC_BYTES = 4 * 1024 * 1024
MAX_HOST_STATIC_FILES = 256
MAX_HOST_STATIC_DEPTH = 16
MAX_HOST_CREDENTIAL_BYTES = 4 * 1024 * 1024
MAX_HOST_STATIC_MANIFEST_BYTES = 128 * 1024
MAX_PROVIDER_LABELS = 64

# Token Monitor's local hub receives the upstream agent's sync payload.  Keep
# this top-level wire contract explicit so a future upstream collector cannot
# silently add native-session, credential, or diagnostic fields to a Cage
# upload.  The omission fields are produced by the pinned sync serializer when
# it trims an oversized payload; the remaining fields are the v0.49.0 summary
# contract (including optional fields).
COLLECTOR_SUMMARY_FIELDS = frozenset(
    {
        "deviceId",
        "hostname",
        "platform",
        "osName",
        "osVersion",
        "updatedAt",
        "agentVersion",
        "agentRuntime",
        "projectsEnabled",
        "trackedClients",
        "clientStatus",
        "clientHealth",
        "wslStatus",
        "periodWindows",
        "historyAvailable",
        "today",
        "month",
        "allTime",
        "history",
        "limits",
        "allTimeProjectsOmitted",
        "allTimeProjectsIncomplete",
        "sessionDetailsOmitted",
        "periodProjectsOmitted",
        "syncUploadIntervalMs",
    }
)

CONNECTION_FILE = "connection.json"
IDENTITY_FILE = "identity.json"
REGISTRY_FILE = "registry.json"
LOCK_DIR = "locks"
DEVICE_DIR = "devices"
PROJECT_DIR = "projects"
RUN_DIR = "runs"
PRICING_FILE = "pricing.json"
PROVIDER_LABELS_FILE = "provider-labels.json"
PROVIDER_LABEL_MIGRATION_FILE = "provider-label-migration.json"
AGGREGATE_STATUS_FILE = "aggregate-status.json"
SPLIT_STATUS_FILE = "split-status.json"
SCHEDULER_STATE_FILE = "scheduler.json"
UPLOAD_STATE_FILE = "upload-state.json"
GENERATION_DIR = "generations"
VOLUME_SNAPSHOT_FILE = "volume-snapshot.json"
HOST_SOURCE_DIR = "host-sources"
HOST_SOURCE_HOME = "codex-home"
HOST_STATIC_SNAPSHOT_FILE = "static-snapshot.json"
VOLUME_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
DEVICE_ID_PATTERN = re.compile(r"^cage-[a-z0-9_-]{1,120}$")
LOGICAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
GENERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CODEX_VOLUME_PREFIX = "codex-state-"
HOST_SOURCE_PREFIX = "cage-host-source-"
UNATTRIBUTED_PROVIDER = "unattributed"
# Provider names originate in a local Codex session.  Built-in public stream
# labels are safe by default.  Other labels stay unattributed unless the owner
# approves that exact normalized label in private monitor state and completes
# its verified migration.  The approval is never part of central config or
# tracked source.
PUBLIC_PROVIDER_IDS = frozenset(
    {"openai-api", "openai-compatible", "zllm", UNATTRIBUTED_PROVIDER}
)
HOST_STATIC_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.config\.toml$")
HOST_STATIC_FIXED_FILES = frozenset(
    {"config.toml", "AGENTS.md", "AGENTS.override.md", "hooks.json"}
)
HOST_SOURCE_CREDENTIAL_FILES = frozenset({"auth.json", ".credentials.json"})


SESSION_NUMBER_FIELDS = (
    "totalTokens",
    "costUsd",
    "messageCount",
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "reasoningTokens",
)


SESSION_MAP_FIELDS = ("models", "modelCosts", "providers")


TOKEN_COMPONENT_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
