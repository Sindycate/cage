"""Pure session reconciliation, provider partitioning and cost calculation.

All pricing, project identities and time come from one immutable input snapshot.
This layer performs no file, process, clock or network operations.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

from . import constants as constants_api
from . import errors as errors_api
from . import validation as validation_api

if TYPE_CHECKING:
    from . import models as models_api


@dataclass(frozen=True)
class AccountingInputs:
    """A stable view shared by every provider and reporting period in a scan."""

    pricing: Mapping[str, Mapping[str, float]]
    project_ids: Mapping[str, str]
    timestamp: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pricing", MappingProxyType({
            model: MappingProxyType(dict(rates))
            for model, rates in self.pricing.items()
        }))
        object.__setattr__(self, "project_ids", MappingProxyType(dict(self.project_ids)))


def _session_number(session: dict[str, Any], field: str) -> float:
    value = session.get(field, 0)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise errors_api.MonitorError(f"collector session has invalid {field}")
    return float(value)


def _session_map(session: dict[str, Any], field: str) -> dict[str, float]:
    raw = session.get(field, {})
    if not isinstance(raw, dict):
        raise errors_api.MonitorError(f"collector session has invalid {field}")
    result: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise errors_api.MonitorError(f"collector session has invalid {field}")
        result[key] = float(value)
    return result


def session_provider(
    session: dict[str, Any],
    *,
    allowed_provider_ids: frozenset[str] | set[str] | None = None,
) -> str:
    """Return the only trustworthy provider for a session.

    Token Monitor records provider totals in a session-level map.  One key is
    safe to attribute only when it is a built-in or explicitly approved stream
    label.  Missing, unapproved, unsafe, or multi-provider maps stay in the
    explicit unattributed stream so Cage never guesses, duplicates tokens, or
    publishes a local provider label.
    """

    providers = _session_map(session, "providers")
    allowed = constants_api.PUBLIC_PROVIDER_IDS if allowed_provider_ids is None else allowed_provider_ids
    normalized = {validation_api._provider_id_from_allowed(key, allowed) for key in providers}
    if len(normalized) == 1 and None not in normalized:
        provider = next(iter(normalized))
        assert provider is not None
        return provider
    return constants_api.UNATTRIBUTED_PROVIDER


def _session_dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in constants_api.SESSION_NUMBER_FIELDS:
        if _session_number(left, field) < _session_number(right, field):
            return False
    for field in constants_api.SESSION_MAP_FIELDS:
        if field == "providers":
            continue
        left_map = _session_map(left, field)
        right_map = _session_map(right, field)
        if any(left_map.get(key, 0) < value for key, value in right_map.items()):
            return False
    return True


def _select_session(candidates: list[tuple[models_api.VolumeRegistration, dict[str, Any]]]) -> dict[str, Any]:
    winner = candidates[0][1]
    for _, candidate in candidates[1:]:
        candidate_dominates = _session_dominates(candidate, winner)
        winner_dominates = _session_dominates(winner, candidate)
        if candidate_dominates and not winner_dominates:
            winner = candidate
        elif candidate_dominates and winner_dominates:
            old = winner.get("modelTokenUsage")
            new = candidate.get("modelTokenUsage")
            if old is not None and new is not None and old != new:
                old_parts = _model_token_usage(winner)
                new_parts = _model_token_usage(candidate)
                merged = dict(old)
                for model in old_parts:
                    left, right = old_parts[model], new_parts[model]
                    same_base = (
                        left["inputTokens"] + left["cacheWriteTokens"] == right["inputTokens"] + right["cacheWriteTokens"]
                        and left["outputTokens"] == right["outputTokens"]
                        and left["cacheReadTokens"] == right["cacheReadTokens"]
                    )
                    if not same_base or (left["cacheWriteVerified"] and right["cacheWriteVerified"] and left != right):
                        raise errors_api.MonitorError("conflicting model token evidence; hub snapshot was preserved")
                    if right["cacheWriteVerified"]:
                        merged[model] = new[model]
                winner = {**winner, "modelTokenUsage": merged}
            elif old is None and new is not None:
                winner = candidate
        elif not candidate_dominates and not winner_dominates:
            raise errors_api.MonitorError(
                "conflicting copies of one Codex session; hub snapshot was preserved"
            )
    result = dict(winner)
    evidence = [
        candidate["providerTokenUsage"] for _, candidate in candidates
        if "providerTokenUsage" in candidate
        and _session_dominates(candidate, winner) and _session_dominates(winner, candidate)
    ]
    if evidence:
        if any(item != evidence[0] for item in evidence):
            raise errors_api.MonitorError("conflicting provider token evidence; hub snapshot was preserved")
        result["providerTokenUsage"] = evidence[0]
    providers = {
        validation_api._provider_slug(name)
        for _, candidate in candidates
        for name in (_session_map(candidate, "providers") or {constants_api.UNATTRIBUTED_PROVIDER: 0})
    }
    if len(providers) != 1 or None in providers or constants_api.UNATTRIBUTED_PROVIDER in providers:
        result["providers"] = {
            constants_api.UNATTRIBUTED_PROVIDER: _session_number(result, "totalTokens")
        }
    if evidence:
        result["providers"] = {
            provider: part["totalTokens"] for provider, part in _provider_token_usage(result).items()
        }
    return result


def _empty_aggregate_period() -> dict[str, Any]:
    return {
        "capabilities": {"tokenComponents": True},
        "totalTokens": 0,
        "costUsd": 0.0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "outputTokens": 0,
        "unclassifiedTokens": 0,
        "timedTokens": 0,
        "timedOutputTokens": 0,
        "timedDurationMs": 0,
        "clients": {},
        "clientCosts": {},
        "clientCacheReads": {},
        "clientCacheWrites": {},
        "clientOutputs": {},
        "clientUnclassifiedTokens": {},
        "models": {},
        "modelCosts": {},
        "modelCacheReads": {},
        "modelCacheWrites": {},
        "modelOutputs": {},
        "modelUnclassifiedTokens": {},
        "clientModels": {},
        "clientModelCosts": {},
        "projects": {},
        "sessions": {},
    }


def _add_map(target: dict[str, float], values: dict[str, float]) -> None:
    for key, value in values.items():
        target[key] = target.get(key, 0) + value


def _project_for_candidates(
    inputs: AccountingInputs,
    candidates: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
) -> tuple[str, str]:
    logical_ids = {record.logical_id for record, _ in candidates}
    if len(logical_ids) == 1:
        record = candidates[0][0]
        return inputs.project_ids[record.logical_id], record.display_name
    return "cage-project-unattributed", "Cage: Unattributed"


def _model_token_usage(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate disjoint per-model buckets against the unchanged source totals."""
    models = _session_map(session, "models")
    raw = session.get("modelTokenUsage")
    if raw is None:
        if len(models) != 1 or sum(models.values()) != _session_number(session, "totalTokens"):
            return {}
        if sum(_session_number(session, k) for k in constants_api.TOKEN_COMPONENT_FIELDS) != sum(models.values()):
            return {}
        return {next(iter(models)): {
            **{k: _session_number(session, k) for k in constants_api.TOKEN_COMPONENT_FIELDS},
            "cacheWriteVerified": True,
        }}
    if not isinstance(raw, dict) or set(raw) != set(models):
        raise errors_api.MonitorError("collector model token evidence does not cover its models")
    result = {}
    for model, parts in raw.items():
        if not isinstance(parts, dict) or set(parts) != set(constants_api.TOKEN_COMPONENT_FIELDS) | {"totalTokens", "cacheWriteVerified"}:
            raise errors_api.MonitorError("collector model token evidence has an invalid shape")
        if type(parts["cacheWriteVerified"]) is not bool:
            raise errors_api.MonitorError("collector model cache-write evidence is invalid")
        values = {k: _session_number(parts, k) for k in constants_api.TOKEN_COMPONENT_FIELDS}
        if any(not v.is_integer() for v in values.values()):
            raise errors_api.MonitorError("collector model token evidence is not integral")
        if sum(values.values()) != models[model] or _session_number(parts, "totalTokens") != models[model]:
            raise errors_api.MonitorError("collector model token components do not reconcile")
        result[model] = {**values, "cacheWriteVerified": parts["cacheWriteVerified"]}
    if sum(models.values()) != _session_number(session, "totalTokens"):
        raise errors_api.MonitorError("collector model tokens do not reconcile")
    for keys in (("inputTokens", "cacheWriteTokens"), ("outputTokens",), ("cacheReadTokens",)):
        if sum(parts[k] for parts in result.values() for k in keys) != sum(_session_number(session, k) for k in keys):
            raise errors_api.MonitorError("collector model token evidence disagrees with session components")
    return result


def _price_session(
    session: dict[str, Any],
    pricing: Mapping[str, Mapping[str, float]],
    *,
    allowed_provider_ids: frozenset[str] | set[str],
) -> tuple[dict[str, float], int, dict[str, set[str]]]:
    provider = session_provider(session, allowed_provider_ids=allowed_provider_ids)
    models = _session_map(session, "models")
    components = _model_token_usage(session)
    costs: dict[str, float] = {}
    covered = 0
    reasons: dict[str, set[str]] = {
        "missing_rates": set(), "missing_components": set(),
        "unverified_cache_writes": set(), "unattributed_models": set(),
    }
    upstream_costs = _session_map(session, "modelCosts")
    if not models and _session_number(session, "totalTokens"):
        reasons["missing_components"].add(f"{provider}:unknown")
    for model, tokens in models.items():
        key = f"{provider}:{model}"
        if provider == constants_api.UNATTRIBUTED_PROVIDER:
            reasons["unattributed_models"].add(key)
            continue
        rates = pricing.get(key)
        if rates is None and provider == "openai-api":
            rates = pricing.get(model)
        parts = components.get(model)
        if parts is not None and not parts["cacheWriteVerified"]:
            reasons["unverified_cache_writes"].add(key)
            continue
        if rates is None:
            # Only OpenAI may use the collector's model-only catalog. Positive
            # model costs are evidence; an explicit zero is not a free tariff.
            if (provider == "openai-api" and upstream_costs.get(model, 0) > 0
                    and not (parts and parts["cacheWriteTokens"])):
                costs[model] = upstream_costs[model]
                covered += round(tokens)
            else:
                reasons["missing_rates"].add(key)
            continue
        if parts is None:
            reasons["missing_components"].add(key)
            continue
        model_cost = 0.0
        model_covered = 0
        for field, rate_name in (
            ("inputTokens", "input_per_million"),
            ("outputTokens", "output_per_million"),
            ("cacheReadTokens", "cache_read_per_million"),
            ("cacheWriteTokens", "cache_write_per_million"),
        ):
            count = round(parts[field])
            rate = rates.get(rate_name)
            # Preserve the established cache-read fallback. Cache writes need
            # their own explicit rate: they may carry a different surcharge.
            if rate is None and field == "cacheReadTokens":
                rate = rates.get("input_per_million")
            if count and rate is None:
                reasons["missing_rates"].add(key)
            elif rate is not None:
                model_cost += count * rate / 1_000_000
                model_covered += count
        if model_covered:
            costs[model] = round(model_cost, 9)
            covered += model_covered
    return costs, covered, reasons


def _provider_token_usage(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Accept recorded provider buckets only when every usage component reconciles."""
    raw = session.get("providerTokenUsage")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw or len(raw) > constants_api.MAX_PROVIDER_LABELS:
        raise errors_api.MonitorError("collector provider token evidence is invalid")
    source_models = _session_map(session, "models")
    source_costs = _session_map(session, "modelCosts")
    result = {}
    for provider, bucket in raw.items():
        if (
            not isinstance(provider, str) or not provider or len(provider) > 256
            or not isinstance(bucket, dict)
            or set(bucket) != {"messageCount", "reasoningTokens", "models"}
            or not isinstance(bucket["models"], dict) or not bucket["models"]
            or any(not isinstance(parts, dict) for parts in bucket["models"].values())
        ):
            raise errors_api.MonitorError("collector provider token bucket is invalid")
        models = {model: _session_number(parts, "totalTokens") for model, parts in bucket["models"].items()}
        costs = {model: source_costs[model] for model in models
                 if model in source_costs and models[model] == source_models.get(model)}
        part = {
            **session, "models": models, "modelCosts": costs, "costUsd": sum(costs.values()),
            "totalTokens": sum(models.values()), "providers": {provider: sum(models.values())},
            "messageCount": _session_number(bucket, "messageCount"),
            "reasoningTokens": _session_number(bucket, "reasoningTokens"),
            "modelTokenUsage": bucket["models"],
            **{field: sum(_session_number(m, field) for m in bucket["models"].values())
               for field in constants_api.TOKEN_COMPONENT_FIELDS},
        }
        part.pop("providerTokenUsage", None)
        if any(not part[f].is_integer() for f in ("messageCount", "reasoningTokens")):
            raise errors_api.MonitorError("collector provider token counts are not integral")
        _model_token_usage(part)
        result[provider] = part
    for fields in (
        ("totalTokens",), ("inputTokens", "cacheWriteTokens"), ("outputTokens",),
        ("cacheReadTokens",), ("messageCount",), ("reasoningTokens",),
    ):
        if sum(_session_number(p, f) for p in result.values() for f in fields) != sum(
            _session_number(session, f) for f in fields
        ):
            raise errors_api.MonitorError("collector provider token components do not reconcile")
    combined: dict[str, dict[str, float]] = {}
    for part in result.values():
        for model, values in _model_token_usage(part).items():
            total = combined.setdefault(model, {field: 0 for field in constants_api.TOKEN_COMPONENT_FIELDS})
            for field in total:
                total[field] += values[field]
    expected = _model_token_usage(session)
    if set(combined) != set(expected):
        raise errors_api.MonitorError("collector provider evidence does not cover its models")
    for model in expected:
        for fields in (("inputTokens", "cacheWriteTokens"), ("outputTokens",), ("cacheReadTokens",)):
            if sum(combined[model][f] for f in fields) != sum(expected[model][f] for f in fields):
                raise errors_api.MonitorError("collector provider model components do not reconcile")
    return result


def _provider_slices(
    session: dict[str, Any], *, allowed_provider_ids: frozenset[str] | set[str],
) -> dict[str, dict[str, Any]]:
    slices: dict[str, dict[str, Any]] = {}
    for part in _provider_token_usage(session).values():
        provider = session_provider(part, allowed_provider_ids=allowed_provider_ids)
        if provider not in slices:
            slices[provider] = part
            continue
        target = slices[provider]
        for field in constants_api.SESSION_NUMBER_FIELDS:
            target[field] += part[field]
        for model, value in part["models"].items():
            target["models"][model] = target["models"].get(model, 0) + value
        for model, value in part["modelCosts"].items():
            target["modelCosts"][model] = target["modelCosts"].get(model, 0) + value
        evidence = dict(target["modelTokenUsage"])
        for model, values in part["modelTokenUsage"].items():
            if model not in evidence:
                evidence[model] = dict(values)
            else:
                left = evidence[model]
                evidence[model] = {
                    **{f: left[f] + values[f] for f in (*constants_api.TOKEN_COMPONENT_FIELDS, "totalTokens")},
                    "cacheWriteVerified": left["cacheWriteVerified"] and values["cacheWriteVerified"],
                }
        target["modelTokenUsage"] = evidence
    for provider, part in slices.items():
        part["providers"] = {provider: part["totalTokens"]}
    return slices


def _period_from_sessions(
    inputs: AccountingInputs,
    occurrences: dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]],
    *,
    allowed_provider_ids: frozenset[str] | set[str],
    winners: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    period = _empty_aggregate_period()
    pricing = inputs.pricing
    duplicates = 0
    for key in sorted(occurrences):
        candidates = occurrences[key]
        duplicates += max(0, len(candidates) - 1)
        session = dict(winners[key]) if winners is not None and key in winners else _select_session(candidates)
        session.pop("providerTokenUsage", None)
        provider = session_provider(session, allowed_provider_ids=allowed_provider_ids)
        model_costs, _, _ = _price_session(
            session, pricing, allowed_provider_ids=allowed_provider_ids
        )
        session["modelCosts"] = model_costs
        session["costUsd"] = round(sum(model_costs.values()), 9)
        components = _model_token_usage(session)
        if components and all(parts["cacheWriteVerified"] for parts in components.values()):
            # Reclassify writes without changing any token total. Raw snapshots
            # retain the upstream buckets for old/new copy reconciliation.
            for field in constants_api.TOKEN_COMPONENT_FIELDS:
                session[field] = sum(parts[field] for parts in components.values())
        client = str(session.get("client") or "")
        session_id = str(session.get("sessionId") or "")
        if client != "codex" or not session_id:
            raise errors_api.MonitorError("collector session identity is invalid")
        project_id, project_label = _project_for_candidates(inputs, candidates)
        session["projectId"] = project_id
        session["projectLabel"] = project_label
        total = round(_session_number(session, "totalTokens"))
        cost = _session_number(session, "costUsd")
        cache_read = round(_session_number(session, "cacheReadTokens"))
        cache_write = round(_session_number(session, "cacheWriteTokens"))
        output = round(_session_number(session, "outputTokens"))
        unclassified = max(0, total - min(total, cache_read + cache_write + output))
        models = _session_map(session, "models")
        model_costs = _session_map(session, "modelCosts")
        # Keep the raw provider map only in private collector snapshots, where
        # it is needed to compare session copies.  The aggregate can expose
        # only the stream to which this already-selected session was assigned.
        session["providers"] = {provider: total}
        period["totalTokens"] += total
        period["costUsd"] += cost
        period["cacheReadTokens"] += cache_read
        period["cacheWriteTokens"] += cache_write
        period["outputTokens"] += output
        period["unclassifiedTokens"] += unclassified
        period["clients"][client] = period["clients"].get(client, 0) + total
        period["clientCosts"][client] = period["clientCosts"].get(client, 0) + cost
        period["clientCacheReads"][client] = period["clientCacheReads"].get(client, 0) + cache_read
        period["clientCacheWrites"][client] = period["clientCacheWrites"].get(client, 0) + cache_write
        period["clientOutputs"][client] = period["clientOutputs"].get(client, 0) + output
        period["clientUnclassifiedTokens"][client] = period["clientUnclassifiedTokens"].get(client, 0) + unclassified
        _add_map(period["models"], models)
        _add_map(period["modelCosts"], model_costs)
        period["clientModels"].setdefault(client, {})
        period["clientModelCosts"].setdefault(client, {})
        _add_map(period["clientModels"][client], models)
        _add_map(period["clientModelCosts"][client], model_costs)
        for model, tokens in models.items():
            parts = components.get(model, {})
            known = parts.get("cacheWriteVerified", False)
            reads = parts.get("cacheReadTokens", 0) if known else 0
            writes = parts.get("cacheWriteTokens", 0) if known else 0
            outputs = parts.get("outputTokens", 0) if known else 0
            for field, count in (
                ("modelCacheReads", reads), ("modelCacheWrites", writes),
                ("modelOutputs", outputs),
                ("modelUnclassifiedTokens", tokens - reads - writes - outputs),
            ):
                period[field][model] = period[field].get(model, 0) + count
        project = period["projects"].setdefault(
            project_id,
            {"label": project_label, "tokens": 0, "costUsd": 0.0, "clients": {}},
        )
        project["tokens"] += total
        project["costUsd"] += cost
        project["clients"][client] = project["clients"].get(client, 0) + total
        period["sessions"][key] = session
    period["costUsd"] = round(period["costUsd"], 9)
    for costs in (period["clientCosts"], period["modelCosts"]):
        for key, value in costs.items():
            costs[key] = round(value, 9)
    for costs in period["clientModelCosts"].values():
        for key, value in costs.items():
            costs[key] = round(value, 9)
    for project in period["projects"].values():
        project["costUsd"] = round(project["costUsd"], 9)
    return period, duplicates


def _collect_occurrences(
    summaries: list[tuple[models_api.VolumeRegistration, dict[str, Any]]],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]],
]:
    """Validate collector results and index all session copies by ID."""

    if not summaries:
        raise errors_api.MonitorError("no active Token Monitor projects")
    occurrences: dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]] = {
        name: {} for name in ("today", "month", "allTime")
    }
    first = summaries[0][1]
    for record, payload in summaries:
        if not _same_period_windows(payload, first):
            raise errors_api.MonitorError("collector period windows changed during the aggregate scan")
        if payload.get("sessionDetailsOmitted"):
            raise errors_api.MonitorError("collector omitted session details; hub snapshot was preserved")
        for period_name in occurrences:
            period = payload.get(period_name)
            sessions = period.get("sessions") if isinstance(period, dict) else None
            # Token Monitor omits the sessions object for an empty period.  A
            # newly registered Cage volume commonly has no Codex session yet,
            # so accept that representation only when its explicit token total
            # is zero.  A non-empty period still requires complete details.
            if isinstance(period, dict) and "sessions" not in period:
                total = period.get("totalTokens")
                if (
                    type(total) in (int, float)
                    and math.isfinite(total)
                    and total == 0
                ):
                    sessions = {}
            if not isinstance(sessions, dict):
                raise errors_api.MonitorError("collector did not provide complete session details")
            session_total = 0
            for key, session in sessions.items():
                if not isinstance(key, str) or not isinstance(session, dict):
                    raise errors_api.MonitorError("collector session detail is invalid")
                expected_key = f"{session.get('client', '')}:{session.get('sessionId', '')}"
                if key != expected_key:
                    raise errors_api.MonitorError("collector session key is invalid")
                session_total += round(_session_number(session, "totalTokens"))
                occurrences[period_name].setdefault(key, []).append((record, session))
            expected = round(float(period.get("totalTokens", 0)))
            if session_total != expected:
                raise errors_api.MonitorError(
                    f"collector {period_name} sessions do not cover its token total; hub snapshot was preserved"
                )
    return summaries[0][1], occurrences


def _same_period_windows(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    """Return whether two collector observations cover the same periods.

    Older collector snapshots do not have a ``periodWindows`` marker.  Keeping
    ``None == None`` compatible lets those older snapshots retain their
    existing behavior, while a marked current observation cannot be combined
    with an unmarked or differently marked cached observation.
    """

    return left.get("periodWindows") == right.get("periodWindows")


def _build_device_payload(
    inputs: AccountingInputs,
    first: dict[str, Any],
    summaries_count: int,
    occurrences: dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]],
    device_id: str,
    *,
    provider: str = "",
    allowed_provider_ids: frozenset[str] | set[str],
    winners_by_period: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    periods: dict[str, dict[str, Any]] = {}
    duplicate_counts: dict[str, int] = {}
    for period_name, values in occurrences.items():
        winners = winners_by_period.get(period_name) if winners_by_period else None
        periods[period_name], duplicate_counts[period_name] = _period_from_sessions(
            inputs,
            values,
            allowed_provider_ids=allowed_provider_ids,
            winners=winners,
        )
    now = inputs.timestamp
    payload: dict[str, Any] = {
        "deviceId": device_id,
        "hostname": (
            f"Cage ({validation_api.provider_display_name(provider, allowed_provider_ids=allowed_provider_ids)})"
            if provider
            else "Cage (local)"
        ),
        "platform": "cage",
        "osName": "Cage",
        "osVersion": "",
        "updatedAt": now,
        "agentVersion": str(first.get("agentVersion") or constants_api.COLLECTOR_SOURCE_VERSION),
        "agentRuntime": "cage-host-collector",
        "projectsEnabled": True,
        "trackedClients": ["codex"],
        "historyAvailable": False,
        "history": None,
        "limits": {"updatedAt": now, "refreshMs": 0, "providers": []},
        "today": periods["today"],
        "month": periods["month"],
        "allTime": periods["allTime"],
    }
    if isinstance(first.get("periodWindows"), dict):
        payload["periodWindows"] = first["periodWindows"]
    pricing = inputs.pricing
    period_pricing = {}
    for name, period in periods.items():
        priced_tokens = 0
        reasons: dict[str, set[str]] = {}
        for session in period["sessions"].values():
            _, covered, session_reasons = _price_session(
                session, pricing, allowed_provider_ids=allowed_provider_ids
            )
            priced_tokens += covered
            for reason, keys in session_reasons.items():
                reasons.setdefault(reason, set()).update(keys)
        total = period["totalTokens"]
        missing = sorted(set().union(*reasons.values())) if reasons else []
        period_pricing[name] = {
            "cost_usd": period["costUsd"], "total_tokens": total,
            "priced_tokens": min(total, priced_tokens),
            "unpriced_tokens": max(0, total - priced_tokens),
            "price_coverage_percent": round(priced_tokens * 100 / total, 2) if total else 100.0,
            "cost_complete": priced_tokens == total,
            "missing_models": sorted({key.split(":", 1)[1] for key in missing}),
            "missing_prices": missing,  # compatible union; reason fields disambiguate it
            **{reason: sorted(keys) for reason, keys in reasons.items()},
        }
    status = {
        "version": constants_api.STATE_VERSION,
        "device_id": payload["deviceId"],
        "updated_at": now,
        "project_count": summaries_count,
        "duplicate_sessions": duplicate_counts["allTime"],
        **period_pricing["allTime"],
        "period_pricing": period_pricing,
    }
    if provider:
        status["provider"] = provider
        status["provider_label"] = validation_api.provider_display_name(
            provider, allowed_provider_ids=allowed_provider_ids
        )
    # Match the upstream wire contract: all-time session detail is local-only.
    # The exact all-time project rollup remains in the upload.
    payload["allTime"].pop("sessions", None)
    budget = constants_api.MAX_OUTPUT_BYTES - 16 * 1024
    omitted: dict[str, int] = {}
    for period_name in ("month", "today"):
        if len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) <= budget:
            break
        sessions = payload[period_name].pop("sessions", {})
        if sessions:
            omitted[period_name] = len(sessions)
    if omitted:
        payload["sessionDetailsOmitted"] = omitted
    return validation_api._validate_summary(payload, payload["deviceId"]), status


def _empty_provider_payload(
    inputs: AccountingInputs,
    first: dict[str, Any],
    device_id: str,
    provider: str,
    *,
    allowed_provider_ids: frozenset[str] | set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    empty_occurrences = {name: {} for name in ("today", "month", "allTime")}
    return _build_device_payload(
        inputs,
        first,
        0,
        empty_occurrences,
        device_id,
        provider=provider,
        allowed_provider_ids=allowed_provider_ids,
        winners_by_period={name: {} for name in empty_occurrences},
    )


def _provider_partitions(
    occurrences: dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]],
    *,
    allowed_provider_ids: frozenset[str] | set[str],
) -> tuple[
    dict[str, dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]]],
    dict[str, dict[str, dict[str, dict[str, Any]]]],
]:
    """Deduplicate first, then assign each winning session to one stream."""

    partitions: dict[str, dict[str, dict[str, list[tuple[models_api.VolumeRegistration, dict[str, Any]]]]]] = {}
    winners: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    providers_by_session: dict[str, set[str]] = {}
    for values in occurrences.values():
        for key, candidates in values.items():
            providers_by_session.setdefault(key, set()).update(
                session_provider(candidate, allowed_provider_ids=allowed_provider_ids)
                for _, candidate in candidates
            )
    for period_name, values in occurrences.items():
        for key, candidates in values.items():
            winner = _select_session(candidates)
            slices = _provider_slices(winner, allowed_provider_ids=allowed_provider_ids)
            if slices:
                for provider, part in slices.items():
                    partitions.setdefault(provider, {name: {} for name in occurrences})
                    winners.setdefault(provider, {name: {} for name in occurrences})
                    partitions[provider][period_name][key] = candidates
                    winners[provider][period_name][key] = part
                continue
            providers = providers_by_session[key]
            if len(providers) != 1 or constants_api.UNATTRIBUTED_PROVIDER in providers:
                winner["providers"] = {
                    constants_api.UNATTRIBUTED_PROVIDER: _session_number(winner, "totalTokens")
                }
            provider = session_provider(
                winner, allowed_provider_ids=allowed_provider_ids
            )
            partitions.setdefault(provider, {name: {} for name in occurrences})
            winners.setdefault(provider, {name: {} for name in occurrences})
            partitions[provider][period_name][key] = candidates
            winners[provider][period_name][key] = winner
    return partitions, winners
