"""Pure shape and privacy validation for monitor names, URLs and summaries."""

from __future__ import annotations

import ipaddress
import json
import math
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import constants as constants_api
from . import errors as errors_api


def validate_volume_name(value: object) -> str:
    if not isinstance(value, str) or not constants_api.VOLUME_NAME_PATTERN.fullmatch(value):
        raise errors_api.MonitorError("invalid monitor volume name")
    return value


def validate_device_id(value: object) -> str:
    if not isinstance(value, str) or not constants_api.DEVICE_ID_PATTERN.fullmatch(value):
        raise errors_api.MonitorError("invalid monitor device id")
    return value


def validate_logical_id(value: object) -> str:
    if not isinstance(value, str) or not constants_api.LOGICAL_ID_PATTERN.fullmatch(value):
        raise errors_api.MonitorError("invalid monitor logical target id")
    return value


def validate_display_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character in value for character in "\x00\r\n\t")
    ):
        raise errors_api.MonitorError("invalid monitor display name")
    return value


def validate_fingerprint(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "name",
        "driver",
        "scope",
        "created_at",
        "label_identity",
    } or any(
        not isinstance(item, str)
        or len(item) > 512
        or (key != "label_identity" and not item)
        for key, item in value.items()
    ):
        raise errors_api.MonitorError("monitor volume fingerprint is invalid")
    return dict(value)


def validate_interval(value: object) -> int:
    if type(value) is not int:
        raise errors_api.MonitorError("monitor interval must be an integer")
    if not constants_api.MIN_INTERVAL_SECONDS <= value <= constants_api.MAX_INTERVAL_SECONDS:
        raise errors_api.MonitorError(
            f"monitor interval must be between {constants_api.MIN_INTERVAL_SECONDS} and "
            f"{constants_api.MAX_INTERVAL_SECONDS} seconds"
        )
    return value


def _allowed_http_host(hostname: str) -> bool:
    lower = hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(lower)
    except ValueError:
        return False
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address in ipaddress.ip_network("100.64.0.0/10")
        or address in ipaddress.ip_network("fd00::/8")
    ) and not address.is_unspecified


def normalize_hub_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise errors_api.MonitorError("Token Monitor hub URL must be a non-empty URL")
    raw = value.strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise errors_api.MonitorError("Token Monitor hub URL is invalid") from exc
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise errors_api.MonitorError("Token Monitor hub URL must use https or http")
    if parts.username is not None or parts.password is not None:
        raise errors_api.MonitorError("Token Monitor hub URL cannot contain userinfo")
    if parts.query or parts.fragment:
        raise errors_api.MonitorError("Token Monitor hub URL cannot contain a query or fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise errors_api.MonitorError("Token Monitor hub URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise errors_api.MonitorError("Token Monitor hub URL has an invalid port")
    if parts.scheme == "http" and not _allowed_http_host(parts.hostname):
        raise errors_api.MonitorError("plain HTTP is allowed only for a private or loopback hub")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _provider_slug(value: object) -> str | None:
    """Return a safe stable provider label, or ``None`` for unsafe input."""

    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "openai": "openai-api",
        "openai-api": "openai-api",
        "openai-compatible": "openai-compatible",
        "zllm": "zllm",
    }
    normalized = aliases.get(normalized, normalized)
    if not constants_api.PROVIDER_SLUG_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _provider_id_from_allowed(
    value: object,
    allowed_provider_ids: frozenset[str] | set[str],
) -> str | None:
    provider = _provider_slug(value)
    return provider if provider is not None and provider in allowed_provider_ids else None


def provider_display_name(
    provider: str,
    *,
    allowed_provider_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Render a provider label only when it belongs to an allowed stream."""

    allowed = constants_api.PUBLIC_PROVIDER_IDS if allowed_provider_ids is None else allowed_provider_ids
    provider_id = _provider_id_from_allowed(provider, allowed) or constants_api.UNATTRIBUTED_PROVIDER
    if provider_id == "openai-api":
        return "OpenAI API"
    if provider_id == constants_api.UNATTRIBUTED_PROVIDER:
        return "Unattributed"
    return provider_id.upper() if provider_id == "zllm" else provider_id


def _validate_summary(payload: object, expected_device_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise errors_api.MonitorError("collector output must be a JSON object")
    if set(payload).difference(constants_api.COLLECTOR_SUMMARY_FIELDS):
        raise errors_api.MonitorError("collector output has unexpected fields")
    if payload.get("deviceId") != expected_device_id:
        raise errors_api.MonitorError("collector output device identity mismatch")
    if payload.get("trackedClients") != ["codex"]:
        raise errors_api.MonitorError("collector output tracked-client policy mismatch")
    limits = payload.get("limits")
    if not isinstance(limits, dict) or set(limits) - {"updatedAt", "refreshMs", "providers"}:
        raise errors_api.MonitorError("collector output has an invalid limits shape")
    # The upstream agent may stamp the limits probe time even when provider
    # limits are disabled.  The absence of provider records, rather than an
    # empty timestamp, is the privacy boundary we enforce here.
    if limits.get("providers") not in (None, []):
        raise errors_api.MonitorError("collector output unexpectedly contains account limits")
    for period_name in ("today", "month", "allTime"):
        period = payload.get(period_name)
        if not isinstance(period, dict):
            raise errors_api.MonitorError(f"collector output is missing {period_name}")
        total = period.get("totalTokens", 0)
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise errors_api.MonitorError("collector output has an invalid token total")
        cost = period.get("costUsd", 0)
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            raise errors_api.MonitorError("collector output has an invalid cost total")
        for field in (
            "clients", "clientCosts", "models", "modelCosts", "sessions", "projects"
        ):
            if field in period and not isinstance(period[field], dict):
                raise errors_api.MonitorError(f"collector output has invalid {field}")
    # Token Monitor's sync payload intentionally strips native paths and
    # unbounded all-time sessions. Reject obvious path-bearing fields even if a
    # future upstream version accidentally reintroduces them.
    encoded = json.dumps(payload, ensure_ascii=True)
    if (
        "/home/" in encoded
        or "/Users/" in encoded
        or "\\Users\\" in encoded
        or "CODEX_HOME" in encoded
        or "/scan/" in encoded
    ):
        raise errors_api.MonitorError("collector output contains a source path")
    return payload
