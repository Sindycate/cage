"""Bounded authenticated hub transport and the outbound privacy boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from pathlib import Path
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import constants as constants_api
from . import errors as errors_api
from . import identity as identity_api
from . import models as models_api
from . import validation as validation_api


def _outbound_session_id(config_root: Path, client: str, session_id: str) -> str:
    """Return a stable, hub-safe pseudonym for one local session identity.

    Session IDs are useful to the hub only as stable keys for a later
    replacement upload.  They do not need to remain raw UUIDs (or expose
    whatever future Codex versions choose as an ID).  The full per-install
    identity is private mode-0600 state; only a short public prefix appears in
    Cage device IDs, so this HMAC cannot be reversed by a hub or a repository.
    """

    if client != "codex" or not isinstance(session_id, str) or not session_id:
        raise errors_api.MonitorError("collector session identity is invalid")
    digest = hmac.new(
        bytes.fromhex(identity_api.host_install_id(config_root)),
        b"session\0" + client.encode("utf-8") + b"\0" + session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # 128 bits keeps collision risk negligible while retaining a compact,
    # ordinary identifier accepted by the upstream session map.
    return "cage-session-" + digest[:32]


def _outbound_payload(config_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a local aggregate and remove raw session IDs before upload.

    Local snapshots and prepared generations intentionally retain raw IDs so
    deduplication can compare real session copies across Cage sources.  This
    final boundary is the only route to ``/api/ingest`` and substitutes stable
    HMAC pseudonyms in both the session object and its map key.
    """

    try:
        copied = json.loads(
            json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise errors_api.MonitorError("Token Monitor upload payload is invalid") from exc
    if not isinstance(copied, dict):
        raise errors_api.MonitorError("Token Monitor upload payload is invalid")
    device_id = copied.get("deviceId")
    if not isinstance(device_id, str):
        raise errors_api.MonitorError("Token Monitor upload payload is invalid")
    validation_api._validate_summary(copied, device_id)
    for period_name in ("today", "month", "allTime"):
        period = copied.get(period_name)
        if not isinstance(period, dict):
            continue
        sessions = period.get("sessions")
        if sessions is None:
            continue
        if not isinstance(sessions, dict):
            raise errors_api.MonitorError("Token Monitor upload session map is invalid")
        pseudonymous: dict[str, dict[str, Any]] = {}
        for key, session in sessions.items():
            if not isinstance(key, str) or not isinstance(session, dict):
                raise errors_api.MonitorError("Token Monitor upload session map is invalid")
            client = session.get("client")
            session_id = session.get("sessionId")
            if not isinstance(client, str) or not isinstance(session_id, str):
                raise errors_api.MonitorError("collector session identity is invalid")
            if key != f"{client}:{session_id}":
                raise errors_api.MonitorError("collector session key is invalid")
            pseudonym = _outbound_session_id(config_root, client, session_id)
            pseudonymous_key = f"{client}:{pseudonym}"
            if pseudonymous_key in pseudonymous:
                raise errors_api.MonitorError("collector session pseudonym collision")
            session.pop("modelTokenUsage", None)
            session["sessionId"] = pseudonym
            pseudonymous[pseudonymous_key] = session
        period["sessions"] = pseudonymous
    return validation_api._validate_summary(copied, device_id)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise errors_api.MonitorError("Token Monitor hub redirect refused")


def _hub_request(connection: models_api.MonitorConnection, method: str, path: str, body: bytes | None = None) -> object:
    url = connection.hub_url + path
    request = Request(url, data=body, method=method, headers={
        "accept": "application/json",
        "authorization": f"Bearer {connection.secret}",
        **({"content-type": "application/json"} if body is not None else {}),
    })
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(constants_api.MAX_HUB_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        # Do not persist or display an attacker-controlled response body.  A
        # hub can reflect credentials or other request material in an error,
        # and scan errors are retained in the local registry.
        exc.close()
        raise errors_api.MonitorError(f"Token Monitor hub returned HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise errors_api.MonitorError(f"Token Monitor hub request failed: {exc.reason if isinstance(exc, URLError) else exc}") from exc
    if len(raw) > constants_api.MAX_HUB_RESPONSE_BYTES:
        raise errors_api.MonitorError("Token Monitor hub response is too large")
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors_api.MonitorError("Token Monitor hub returned invalid JSON") from exc


def verify_connection(connection: models_api.MonitorConnection) -> None:
    health = _hub_request(replace(connection, secret="unused"), "GET", "/api/health")
    if not isinstance(health, dict) or health.get("ok") is not True or health.get("role") not in {"hub", "worker"}:
        raise errors_api.MonitorError("configured URL is not a Token Monitor hub")
    stats = _hub_request(connection, "GET", "/api/stats")
    if (
        not isinstance(stats, dict)
        or not isinstance(stats.get("devices"), list)
        or not isinstance(stats.get("periods"), dict)
    ):
        raise errors_api.MonitorError("Token Monitor hub authentication check failed")


def upload_summary(
    connection: models_api.MonitorConnection,
    payload: dict[str, Any],
    *,
    config_root: Path,
) -> None:
    """Send one aggregate only after pseudonymizing session identities."""

    outbound = _outbound_payload(config_root, payload)
    body = json.dumps(outbound, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > constants_api.MAX_OUTPUT_BYTES:
        raise errors_api.MonitorError("Token Monitor ingest payload is too large")
    _hub_request(connection, "POST", "/api/ingest", body)


def delete_device(connection: models_api.MonitorConnection, device_id: str) -> None:
    validation_api.validate_device_id(device_id)
    from urllib.parse import quote

    _hub_request(connection, "DELETE", "/api/devices/" + quote(device_id, safe=""))


def _hub_stats(connection: models_api.MonitorConnection) -> dict[str, Any]:
    stats = _hub_request(connection, "GET", "/api/stats")
    if not isinstance(stats, dict):
        raise errors_api.MonitorError("Token Monitor hub returned invalid device statistics")
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise errors_api.MonitorError("Token Monitor hub returned invalid device statistics")
    return stats


def _hub_device_ids_from_stats(stats: dict[str, Any]) -> set[str]:
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise errors_api.MonitorError("Token Monitor hub returned invalid device statistics")
    result: set[str] = set()
    for item in devices:
        if not isinstance(item, dict):
            continue
        device_id = item.get("deviceId")
        if isinstance(device_id, str):
            try:
                result.add(validation_api.validate_device_id(device_id))
            except errors_api.MonitorError:
                continue
    return result


def _hub_device_ids(connection: models_api.MonitorConnection) -> set[str]:
    return _hub_device_ids_from_stats(_hub_stats(connection))


def _hub_device_total_from_stats(stats: dict[str, Any], device_id: str) -> float | None:
    """Return one device's all-time token total from authenticated hub stats."""

    validation_api.validate_device_id(device_id)
    devices = stats.get("devices")
    if not isinstance(devices, list):
        raise errors_api.MonitorError("Token Monitor hub returned invalid device statistics")
    for item in devices:
        if not isinstance(item, dict) or item.get("deviceId") != device_id:
            continue
        periods = item.get("periods")
        all_time = periods.get("allTime") if isinstance(periods, dict) else None
        total = all_time.get("totalTokens") if isinstance(all_time, dict) else None
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise errors_api.MonitorError(
                f"Token Monitor hub has no valid all-time total for device {device_id}"
            )
        return float(total)
    return None
