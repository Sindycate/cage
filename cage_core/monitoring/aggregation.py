"""Read one private input snapshot, then delegate to pure accounting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import accounting as accounting_api
from . import constants as constants_api
from . import identity as identity_api
from . import models as models_api
from . import pricing as pricing_api
from . import providers as providers_api
from . import state as state_api


def _accounting_inputs(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    identity: identity_api.InstallationIdentity,
) -> accounting_api.AccountingInputs:
    return accounting_api.AccountingInputs(
        pricing=pricing_api.load_pricing(config_root),
        project_ids={record.logical_id: identity.project_id(record.logical_id) for record, _ in summaries},
        timestamp=state_api._now(),
    )


def aggregate_summaries(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the compatibility unsplit aggregate without uploading it."""

    first, occurrences = accounting_api._collect_occurrences(summaries)
    allowed_provider_ids = providers_api._active_provider_ids(config_root)
    identity = identity_api.read_identity(config_root)
    inputs = _accounting_inputs(config_root, summaries, identity)
    return accounting_api._build_device_payload(
        inputs,
        first,
        len(summaries),
        occurrences,
        identity.host_device_id,
        allowed_provider_ids=allowed_provider_ids,
    )


def aggregate_provider_summaries(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    *,
    include_approved: bool = False,
) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Build one hub payload per provider after cross-volume deduplication."""

    allowed_provider_ids = (
        providers_api._approved_provider_ids(config_root)
        if include_approved
        else providers_api._active_provider_ids(config_root)
    )
    return _aggregate_provider_summaries_for_allowed(
        config_root, summaries, allowed_provider_ids
    )


def _aggregate_provider_summaries_for_allowed(
    config_root: Path,
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
    allowed_provider_ids: frozenset[str] | set[str],
) -> tuple[dict[str, tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Build provider payloads for one already-authorized label set.

    The label-recovery path needs an explicit baseline in which the pending
    label remains unattributed, even after a crash has activated it locally.
    Normal callers use :func:`aggregate_provider_summaries` instead.
    """

    first, occurrences = accounting_api._collect_occurrences(summaries)
    allowed_provider_ids = frozenset(allowed_provider_ids)
    identity = identity_api.read_identity(config_root)
    inputs = _accounting_inputs(config_root, summaries, identity)
    partitions, winners = accounting_api._provider_partitions(
        occurrences, allowed_provider_ids=allowed_provider_ids
    )
    result: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for provider in sorted(partitions):
        result[provider] = accounting_api._build_device_payload(
            inputs,
            first,
            len(summaries),
            partitions[provider],
            identity.provider_device_id(provider, allowed_provider_ids),
            provider=provider,
            allowed_provider_ids=allowed_provider_ids,
            winners_by_period=winners[provider],
        )
    total_tokens = sum(item[1]["total_tokens"] for item in result.values())
    total_cost = round(sum(item[1]["cost_usd"] for item in result.values()), 9)
    duplicate_sessions = sum(item[1]["duplicate_sessions"] for item in result.values())
    priced_tokens = sum(item[1]["priced_tokens"] for item in result.values())
    missing_models = sorted({model for _, status in result.values() for model in status.get("missing_models", [])})
    missing_prices = sorted({key for _, status in result.values() for key in status.get("missing_prices", [])})
    manifest = {
        "version": constants_api.STATE_VERSION,
        "device_id": identity.host_device_id,
        "device_ids": [status["device_id"] for _, status in result.values()],
        "providers": {provider: status for provider, (_, status) in result.items()},
        "project_count": len(summaries),
        "duplicate_sessions": duplicate_sessions,
        "total_tokens": total_tokens,
        "cost_usd": total_cost,
        "priced_tokens": min(total_tokens, priced_tokens),
        "unpriced_tokens": max(0, total_tokens - priced_tokens),
        "price_coverage_percent": round(
            (priced_tokens * 100 / total_tokens) if total_tokens else 100.0, 2
        ),
        "missing_models": missing_models,
        "missing_prices": missing_prices,
        "updated_at": inputs.timestamp,
    }
    manifest["period_pricing"] = {}
    for name in ("today", "month", "allTime"):
        rows = [status["period_pricing"][name] for _, status in result.values()]
        total = sum(row["total_tokens"] for row in rows)
        priced = sum(row["priced_tokens"] for row in rows)
        details = {
            "total_tokens": total, "priced_tokens": priced,
            "unpriced_tokens": total - priced,
            "cost_usd": round(sum(row["cost_usd"] for row in rows), 9),
            "cost_complete": priced == total,
            "price_coverage_percent": round(priced * 100 / total, 2) if total else 100.0,
        }
        for reason in ("missing_rates", "missing_components", "unverified_cache_writes", "unattributed_models"):
            details[reason] = sorted({key for row in rows for key in row.get(reason, [])})
        manifest["period_pricing"][name] = details
    manifest.update({k: v for k, v in manifest["period_pricing"]["allTime"].items() if k not in manifest})
    return result, manifest


def _empty_provider_payload(
    config_root: Path,
    first: dict[str, Any],
    device_id: str,
    provider: str,
    *,
    allowed_provider_ids: frozenset[str] | set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    # Retired streams contain no sessions, so no pricing or project IDs are needed.
    inputs = accounting_api.AccountingInputs({}, {}, state_api._now())
    return accounting_api._empty_provider_payload(
        inputs, first, device_id, provider, allowed_provider_ids=allowed_provider_ids,
    )
