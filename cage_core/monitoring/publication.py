"""Prepared upload generations, exact-device rollback and crash recovery."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from . import constants as constants_api
from . import errors as errors_api
from . import hub as hub_api
from . import identity as identity_api
from . import models as models_api
from . import providers as providers_api
from . import state as state_api
from . import validation as validation_api


def _generation_root(config_root: Path) -> Path:
    path = state_api.monitor_root(config_root) / constants_api.GENERATION_DIR
    state_api._ensure_private_directory(path)
    return path


def _validate_generation_id(value: object) -> str:
    if not isinstance(value, str) or not constants_api.GENERATION_ID_PATTERN.fullmatch(value):
        raise errors_api.MonitorError("monitor upload generation is invalid")
    return value


def _generation_manifest_path(config_root: Path, generation: str) -> Path:
    return _generation_root(config_root) / _validate_generation_id(generation) / "generation.json"


def _validate_upload_state(config_root: Path, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "state",
        "generation",
        "previous_generation",
        "provider_ids",
        "attempted",
        "created_at",
        "last_error",
    }:
        raise errors_api.MonitorError("monitor upload state has an invalid shape")
    if value["version"] != constants_api.UPLOAD_STATE_VERSION:
        raise errors_api.MonitorError("monitor upload state has an invalid version")
    if value["state"] not in {"pending", "repair_pending"}:
        raise errors_api.MonitorError("monitor upload state has an invalid status")
    _validate_generation_id(value["generation"])
    previous = value["previous_generation"]
    if previous:
        _validate_generation_id(previous)
    provider_ids = value["provider_ids"]
    if not isinstance(provider_ids, dict) or len(provider_ids) > 1024:
        raise errors_api.MonitorError("monitor upload provider identities are invalid")
    for raw_provider, device_id in provider_ids.items():
        provider = providers_api._public_provider_id(config_root, raw_provider)
        if provider != raw_provider:
            raise errors_api.MonitorError("monitor upload provider identity is invalid")
        validation_api.validate_device_id(device_id)
    attempted = value["attempted"]
    if not isinstance(attempted, list) or len(attempted) > len(provider_ids) or any(
        not isinstance(item, str) for item in attempted
    ):
        raise errors_api.MonitorError("monitor upload attempt list is invalid")
    if any(item not in provider_ids or item in attempted[:index] for index, item in enumerate(attempted)):
        raise errors_api.MonitorError("monitor upload attempt list is invalid")
    for key in ("created_at", "last_error"):
        if not isinstance(value[key], str) or len(value[key]) > 512:
            raise errors_api.MonitorError("monitor upload state text is invalid")
    return dict(value)


def load_upload_state(config_root: Path) -> dict[str, Any] | None:
    value = state_api._read_json(
        state_api.monitor_root(config_root) / constants_api.UPLOAD_STATE_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if value is None:
        return None
    return _validate_upload_state(config_root, value)


def save_upload_state(config_root: Path, value: dict[str, Any]) -> None:
    state_api._write_json(
        state_api.monitor_root(config_root) / constants_api.UPLOAD_STATE_FILE,
        _validate_upload_state(config_root, value),
    )


def remove_upload_state(config_root: Path) -> None:
    state_api._remove_private_file(
        state_api.monitor_root(config_root) / constants_api.UPLOAD_STATE_FILE,
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )


def _generation_directory(config_root: Path, generation: str) -> Path:
    return _generation_root(config_root) / _validate_generation_id(generation)


def _validate_exact_provider_ids(
    config_root: Path,
    provider_ids: dict[str, str],
) -> None:
    if not isinstance(provider_ids, dict):
        raise errors_api.MonitorError("monitor provider device map is invalid")
    for raw_provider, device_id in provider_ids.items():
        provider = providers_api._public_provider_id(config_root, raw_provider)
        if provider != raw_provider:
            raise errors_api.MonitorError("monitor provider identity is invalid")
        expected = providers_api.provider_device_id(config_root, provider)
        if device_id != expected:
            raise errors_api.MonitorError(
                "monitor provider device identity was invalid; hub snapshot was preserved"
            )


def _is_legacy_private_provider_entry(
    config_root: Path,
    raw_provider: object,
    entry: object,
) -> bool:
    """Return whether one unapproved old provider entry has its exact old ID.

    Before 0.36.0, a syntactically safe local provider label could become a
    provider device ID without the owner's current explicit approval. A prior
    generation can still be needed as a rollback baseline, so recognize only
    the exact legacy shape and deterministic device ID, then omit it unless
    the owner restores that label through the verified migration path.
    """

    provider = validation_api._provider_slug(raw_provider)
    if (
        provider != raw_provider
        or provider is None
        or providers_api._public_provider_id(config_root, provider) is not None
        or not isinstance(entry, dict)
        or set(entry) != {"device_id"}
    ):
        return False
    device_id = entry["device_id"]
    if not isinstance(device_id, str):
        return False
    expected = (
        f"cage-{provider}-{identity_api._platform_slug()}-{identity_api.host_install_id(config_root)[:8]}"
    )
    return hmac.compare_digest(device_id, expected)


def _write_generation_payloads(
    config_root: Path,
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> str:
    generation = secrets.token_hex(16)
    directory = _generation_directory(config_root, generation)
    state_api._ensure_private_directory(directory)
    provider_ids: dict[str, str] = {}
    for provider, (payload, _status) in sorted(payloads.items()):
        normalized = providers_api._public_provider_id(config_root, provider)
        if normalized != provider:
            raise errors_api.MonitorError("monitor provider identity is invalid")
        device_id = providers_api.provider_device_id(config_root, provider)
        validation_api._validate_summary(payload, device_id)
        provider_ids[provider] = device_id
        state_api._write_json(directory / f"{provider}.json", payload)
    state_api._write_json(
        directory / "generation.json",
        {
            "version": constants_api.UPLOAD_STATE_VERSION,
            "generation": generation,
            "providers": {
                provider: {"device_id": device_id}
                for provider, device_id in sorted(provider_ids.items())
            },
        },
    )
    return generation


def _load_generation_payloads(
    config_root: Path,
    generation: str,
    *,
    allow_legacy_private: bool = False,
) -> dict[str, dict[str, Any]]:
    generation = _validate_generation_id(generation)
    value = state_api._read_json(
        _generation_manifest_path(config_root, generation),
        max_bytes=constants_api.MAX_CONNECTION_BYTES,
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "generation", "providers"}
        or value["version"] != constants_api.UPLOAD_STATE_VERSION
        or value["generation"] != generation
        or not isinstance(value["providers"], dict)
    ):
        raise errors_api.MonitorError("monitor upload generation manifest is invalid")
    provider_entries = value["providers"]
    if len(provider_entries) > 1024:
        raise errors_api.MonitorError("monitor upload generation has too many providers")
    provider_ids: dict[str, str] = {}
    for raw_provider, entry in provider_entries.items():
        provider = providers_api._public_provider_id(config_root, raw_provider)
        if provider == raw_provider and isinstance(entry, dict) and set(entry) == {"device_id"}:
            device_id = entry["device_id"]
            validation_api.validate_device_id(device_id)
            provider_ids[provider] = device_id
            continue
        if allow_legacy_private and _is_legacy_private_provider_entry(
            config_root, raw_provider, entry
        ):
            # The old payload can contain the private label.  It is retained
            # only as a local generation file and must not be parsed or sent.
            continue
        raise errors_api.MonitorError("monitor upload generation provider is invalid")
    _validate_exact_provider_ids(config_root, provider_ids)
    result: dict[str, dict[str, Any]] = {}
    directory = _generation_directory(config_root, generation)
    for provider, device_id in sorted(provider_ids.items()):
        payload = state_api._read_json(directory / f"{provider}.json", max_bytes=constants_api.MAX_OUTPUT_BYTES)
        result[provider] = validation_api._validate_summary(payload, device_id)
    return result


def _previous_generation(
    config_root: Path,
    previous_status: dict[str, Any] | None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    if not isinstance(previous_status, dict):
        return "", {}
    generation = previous_status.get("last_good_generation") or previous_status.get("generation") or ""
    if not generation:
        return "", {}
    generation = _validate_generation_id(generation)
    return generation, _load_generation_payloads(
        config_root, generation, allow_legacy_private=True
    )


def _upload_state_for_generation(
    *,
    generation: str,
    previous_generation: str,
    provider_ids: dict[str, str],
    attempted: list[str],
    state: str,
    last_error: str = "",
) -> dict[str, Any]:
    return {
        "version": constants_api.UPLOAD_STATE_VERSION,
        "state": state,
        "generation": generation,
        "previous_generation": previous_generation,
        "provider_ids": dict(sorted(provider_ids.items())),
        "attempted": sorted(set(attempted)),
        "created_at": state_api._now(),
        "last_error": " ".join(last_error.split())[:512],
    }


def _repair_pending_upload(
    config_root: Path,
    connection: models_api.MonitorConnection,
) -> None:
    pending = load_upload_state(config_root)
    if pending is None:
        return
    provider_ids = pending["provider_ids"]
    _validate_exact_provider_ids(config_root, provider_ids)
    previous_generation = pending["previous_generation"]
    previous = (
        _load_generation_payloads(
            config_root, previous_generation, allow_legacy_private=True
        )
        if previous_generation
        else {}
    )
    try:
        for provider in sorted(pending["attempted"]):
            # A new provider has no last-good payload.  Leave that exact device
            # untouched; the complete next generation below will repair it.
            payload = previous.get(provider)
            if payload is not None:
                hub_api.upload_summary(connection, payload, config_root=config_root)
    except Exception as exc:
        pending["state"] = "repair_pending"
        pending["last_error"] = " ".join(str(exc).split())[:512]
        save_upload_state(config_root, pending)
        raise errors_api.MonitorError(
            "provider upload repair failed; the last-good local snapshot was preserved"
        ) from exc
    remove_upload_state(config_root)


def _rollback_attempted_uploads(
    config_root: Path,
    connection: models_api.MonitorConnection,
    attempted: list[str],
    previous: dict[str, dict[str, Any]],
) -> bool:
    reversible = True
    for provider in sorted(set(attempted)):
        payload = previous.get(provider)
        if payload is None:
            # The provider may have been introduced in this generation.  Do
            # not delete or zero its device; retain a repair marker so the
            # next complete generation explicitly rewrites it.
            reversible = False
            continue
        hub_api.upload_summary(connection, payload, config_root=config_root)
    return reversible


def _prune_generations(
    config_root: Path,
    *,
    current_generation: str,
    previous_generation: str,
) -> None:
    root = _generation_root(config_root)
    keep = {item for item in (current_generation, previous_generation) if item}
    candidates: list[str] = []
    for entry in root.iterdir():
        try:
            info = os.lstat(entry)
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            continue
        if info.st_uid != os.getuid() or not constants_api.GENERATION_ID_PATTERN.fullmatch(entry.name):
            continue
        candidates.append(entry.name)
    for generation in sorted(candidates, reverse=True):
        if len(keep) >= constants_api.MAX_GENERATIONS:
            break
        keep.add(generation)
    for generation in candidates:
        if generation not in keep:
            state_api._remove_owned_directory(
                root / generation,
                description="monitor upload generation",
            )


def _publish_provider_payloads(
    config_root: Path,
    connection: models_api.MonitorConnection,
    payloads: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    status: dict[str, Any],
    previous_status: dict[str, Any] | None,
    *,
    skip_upload_for: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Publish one generation and repair any interrupted older generation.

    Token Monitor v0.49.0 has one-device ingest rather than a transaction.  A
    private prepared generation and exact-device rollback make the best
    available behavior deterministic while keeping the last-good aggregate
    status authoritative locally.
    """

    skipped = set(skip_upload_for)
    if not skipped.issubset(payloads):
        raise errors_api.MonitorError("monitor provider upload skip set is invalid")
    _repair_pending_upload(config_root, connection)
    previous_generation, previous = _previous_generation(config_root, previous_status)
    generation = _write_generation_payloads(config_root, payloads)
    provider_ids = {
        provider: providers_api.provider_device_id(config_root, provider)
        for provider in sorted(payloads)
    }
    _validate_exact_provider_ids(config_root, provider_ids)
    attempted: list[str] = []
    save_upload_state(
        config_root,
        _upload_state_for_generation(
            generation=generation,
            previous_generation=previous_generation,
            provider_ids=provider_ids,
            attempted=attempted,
            state="pending",
        ),
    )
    try:
        for provider in sorted(payloads):
            if provider in skipped:
                continue
            attempted.append(provider)
            save_upload_state(
                config_root,
                _upload_state_for_generation(
                    generation=generation,
                    previous_generation=previous_generation,
                    provider_ids=provider_ids,
                    attempted=attempted,
                    state="pending",
                ),
            )
            hub_api.upload_summary(
                connection,
                payloads[provider][0],
                config_root=config_root,
            )
    except Exception as exc:
        failure = exc
        save_upload_state(
            config_root,
            _upload_state_for_generation(
                generation=generation,
                previous_generation=previous_generation,
                provider_ids=provider_ids,
                attempted=attempted,
                state="repair_pending",
                last_error=str(exc),
            ),
        )
        try:
            rollback_complete = _rollback_attempted_uploads(
                config_root, connection, attempted, previous
            )
        except Exception as rollback_error:
            pending = load_upload_state(config_root) or {}
            if pending:
                pending["state"] = "repair_pending"
                pending["last_error"] = (
                    "provider upload failed and exact-device rollback is pending: "
                    + " ".join(str(rollback_error).split())
                )[:512]
                save_upload_state(config_root, pending)
            raise errors_api.MonitorError(
                "provider upload failed; exact-device repair is pending and the last-good snapshot was preserved"
            ) from failure
        if not rollback_complete:
            pending = load_upload_state(config_root)
            if pending is not None:
                pending["last_error"] = (
                    "provider upload failed; a new provider device needs a complete next generation"
                )[:512]
                save_upload_state(config_root, pending)
            raise errors_api.MonitorError(
                "provider upload failed; complete-generation repair is pending and the last-good snapshot was preserved"
            ) from failure
        remove_upload_state(config_root)
        raise failure

    next_status = dict(status)
    next_status["generation"] = generation
    next_status["last_good_generation"] = generation
    next_status["upload_state"] = "complete"
    state_api._write_json(state_api.monitor_root(config_root) / constants_api.AGGREGATE_STATUS_FILE, next_status)
    remove_upload_state(config_root)
    _prune_generations(
        config_root,
        current_generation=generation,
        previous_generation=previous_generation,
    )
    return next_status
