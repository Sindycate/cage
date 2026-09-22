"""Accounting regressions across collector evidence, provider rates and periods."""
from contextlib import ExitStack
import copy
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from cage_core import monitor
from cage_core.monitoring import (
    accounting as accounting_api,
    collector as collector_api,
    constants as constants_api,
    hub as hub_api,
    state as state_api,
)


ROOT = Path(__file__).resolve().parents[1]


def session():
    return {
        "client": "codex", "sessionId": "example", "models": {"model-a": 100, "model-b": 200},
        "providers": {"zllm": 300}, "totalTokens": 300, "inputTokens": 170,
        "outputTokens": 50, "cacheReadTokens": 80, "cacheWriteTokens": 0,
        "modelCosts": {}, "costUsd": 0,
        "modelTokenUsage": {
            "model-a": {"totalTokens": 100, "inputTokens": 30, "outputTokens": 10,
                        "cacheReadTokens": 40, "cacheWriteTokens": 20, "cacheWriteVerified": True},
            "model-b": {"totalTokens": 200, "inputTokens": 120, "outputTokens": 40,
                        "cacheReadTokens": 40, "cacheWriteTokens": 0, "cacheWriteVerified": True},
        },
    }


def rates():
    return {
        "zllm:model-a": {"input_per_million": 2, "output_per_million": 10,
                         "cache_read_per_million": .2, "cache_write_per_million": 2.5},
        "zllm:model-b": {"input_per_million": 4, "output_per_million": 20, "cache_read_per_million": .4},
    }


def price(s, r):
    return accounting_api._price_session(s, r, allowed_provider_ids=constants_api.PUBLIC_PROVIDER_IDS)


def test_switching_models_prices_each_component_without_changing_tokens():
    s = session()
    costs, covered, reasons = price(s, rates())
    assert costs == {"model-a": .000218, "model-b": .001296}
    assert covered == 300
    assert not any(reasons.values())
    assert s["inputTokens"] == 170  # raw dedup counters remain immutable


def test_partial_rates_keep_other_models_and_identify_actual_reason():
    r = rates(); del r["zllm:model-b"]
    costs, covered, reasons = price(session(), r)
    assert costs == {"model-a": .000218}
    assert covered == 100
    assert reasons["missing_rates"] == {"zllm:model-b"}
    assert not reasons["missing_components"]


def test_cache_writes_require_explicit_rate_and_are_not_billed_as_input():
    r = rates(); del r["zllm:model-a"]["cache_write_per_million"]
    costs, covered, reasons = price(session(), r)
    assert costs["model-a"] == .000168
    assert covered == 280
    assert reasons["missing_rates"] == {"zllm:model-a"}


def test_legacy_mixed_session_reports_missing_components_not_missing_tariffs():
    s = session(); del s["modelTokenUsage"]
    costs, covered, reasons = price(s, rates())
    assert costs == {} and covered == 0
    assert reasons["missing_components"] == {"zllm:model-a", "zllm:model-b"}
    assert not reasons["missing_rates"]


def test_unverified_writes_cannot_look_fully_priced():
    s = session(); s["modelTokenUsage"]["model-a"]["cacheWriteVerified"] = False
    costs, covered, reasons = price(s, rates())
    assert covered == 200 and set(costs) == {"model-b"}
    assert reasons["unverified_cache_writes"] == {"zllm:model-a"}


@pytest.mark.parametrize("mutation", [
    lambda s: s["modelTokenUsage"]["model-a"].update(inputTokens=-1),
    lambda s: s["modelTokenUsage"]["model-a"].update(inputTokens=31),
    lambda s: s["modelTokenUsage"]["model-a"].update(cacheWriteVerified="yes"),
    lambda s: s["modelTokenUsage"].pop("model-b"),
    lambda s: s.update(outputTokens=51),
])
def test_inconsistent_model_evidence_is_rejected(mutation):
    s = session(); mutation(s)
    with pytest.raises(monitor.MonitorError):
        price(s, rates())


def test_zero_upstream_cost_is_not_proof_of_free_usage():
    s = session(); s["providers"] = {"openai": 300}; s["modelCosts"] = {"model-a": 0, "model-b": 0}
    _, covered, reasons = price(s, {})
    assert covered == 0 and len(reasons["missing_rates"]) == 2


def test_explicit_free_rates_are_priced():
    r = {k: dict.fromkeys(v, 0) for k, v in rates().items()}
    costs, covered, _ = price(session(), r)
    assert sum(costs.values()) == 0 and covered == 300


def test_equal_copies_prefer_components_but_conflicts_stop_upload():
    old = session(); del old["modelTokenUsage"]
    rich = session()
    assert "modelTokenUsage" in accounting_api._select_session([(None, old), (None, rich)])
    conflict = copy.deepcopy(rich)
    conflict["modelTokenUsage"]["model-a"].update(inputTokens=40, cacheWriteTokens=10)
    with pytest.raises(monitor.MonitorError, match="conflicting model"):
        accounting_api._select_session([(None, rich), (None, conflict)])


def test_sidecar_joins_exact_window_evidence_only(tmp_path):
    s = session(); evidence = copy.deepcopy(s); s.pop("modelTokenUsage")
    payload = {name: {"sessions": {"codex:example": copy.deepcopy(s)}} for name in ("today", "month", "allTime")}
    payload["today"]["sessions"]["codex:example"]["totalTokens"] = 99
    state_api._write_json(tmp_path / "model-token-usage.json", {"version": 1, "observations": [{"codex:example": evidence}]})
    collector_api._restore_model_token_usage(tmp_path, payload)
    assert "modelTokenUsage" not in payload["today"]["sessions"]["codex:example"]
    assert payload["allTime"]["sessions"]["codex:example"]["modelTokenUsage"] == evidence["modelTokenUsage"]


def test_full_aggregation_preserves_model_charts_and_period_coverage(tmp_path):
    monitor.save_pricing(tmp_path, rates())
    record = monitor.VolumeRegistration("a" * 32, monitor.host_device_id(tmp_path), "codex-state-test", "container", "/work/test", "Cage: test", {})
    s = session()
    summary = {"deviceId": record.device_id, "trackedClients": ["codex"], "limits": {"providers": []},
               **{name: {"totalTokens": 300, "sessions": {"codex:example": s}} for name in ("today", "month", "allTime")}}
    streams, status = monitor.aggregate_provider_summaries(tmp_path, [(record, summary)])
    payload, _ = streams["zllm"]
    assert payload["today"]["costUsd"] == .001514
    assert payload["today"]["cacheWriteTokens"] == 20
    assert payload["today"]["modelCacheWrites"] == {"model-a": 20, "model-b": 0}
    assert status["period_pricing"]["today"]["cost_complete"] is True
    # Supplemental private schema never enters the hub upload.
    wire = hub_api._outbound_payload(tmp_path, payload)
    assert all("modelTokenUsage" not in row for row in wire["today"]["sessions"].values())


def test_node_accounting_adapter():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    result = subprocess.run([node, "--test", str(ROOT / "tests" / "monitor-accounting.test.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_equal_copies_can_upgrade_unverified_writes():
    unknown = session()
    unknown["modelTokenUsage"]["model-a"].update(inputTokens=50, cacheWriteTokens=0, cacheWriteVerified=False)
    rich = session()
    selected = accounting_api._select_session([(None, unknown), (None, rich)])
    assert selected["modelTokenUsage"] == rich["modelTokenUsage"]
    assert accounting_api._select_session([(None, rich), (None, unknown)])["modelTokenUsage"] == rich["modelTokenUsage"]


def test_old_catalog_cost_cannot_cover_new_cache_write_charges():
    s = session(); s["providers"] = {"openai": 300}
    s["modelCosts"] = {"model-a": .0002, "model-b": .0013}
    costs, covered, reasons = price(s, {})
    assert costs == {"model-b": .0013} and covered == 200
    assert reasons["missing_rates"] == {"openai-api:model-a"}


def test_full_accounting_is_repeatable_without_io_and_uses_one_input_snapshot():
    record = monitor.VolumeRegistration(
        "a" * 32, "cage-device-test", "codex-state-test", "container",
        "/work/test", "Cage: test", {},
    )
    summary = {
        "deviceId": record.device_id, "trackedClients": ["codex"],
        "limits": {"providers": []},
        **{name: {"totalTokens": 300, "sessions": {"codex:example": session()}}
           for name in ("today", "month", "allTime")},
    }
    original_summary = copy.deepcopy(summary)
    mutable_rates = rates()
    mutable_ids = {record.logical_id: "cage-project-test"}
    timestamp = "2026-09-22T12:34:56Z"
    inputs = accounting_api.AccountingInputs(mutable_rates, mutable_ids, timestamp)
    # Changing the caller's dictionaries during a scan cannot mix tariffs or
    # project identities between providers and reporting periods.
    mutable_rates["zllm:model-a"]["input_per_million"] = 999
    mutable_ids[record.logical_id] = "cage-project-changed"

    with ExitStack() as blocked:
        for boundary in (
            "builtins.open", "io.open", "os.open", "subprocess.run",
            "urllib.request.OpenerDirector.open", "time.time", "time.monotonic",
        ):
            blocked.enter_context(patch(
                boundary, side_effect=AssertionError(f"accounting attempted {boundary}")
            ))
        first, occurrences = accounting_api._collect_occurrences([
            (record, summary), (record, copy.deepcopy(summary)),
        ])
        results = [accounting_api._build_device_payload(
            inputs, first, 2, occurrences, record.device_id, provider="zllm",
            allowed_provider_ids=constants_api.PUBLIC_PROVIDER_IDS,
        ) for _ in range(2)]

    assert results[0] == results[1]
    assert summary == original_summary
    payload, status = results[0]
    assert payload["updatedAt"] == status["updated_at"] == timestamp
    assert status["duplicate_sessions"] == 1
    for period in ("today", "month", "allTime"):
        assert payload[period]["totalTokens"] == 300
        assert payload[period]["costUsd"] == .001514
        assert status["period_pricing"][period]["cost_complete"] is True
    assert payload["today"]["sessions"]["codex:example"]["projectId"] == "cage-project-test"
    with pytest.raises(TypeError):
        inputs.pricing["zllm:model-a"]["input_per_million"] = 999
    with pytest.raises(TypeError):
        inputs.project_ids[record.logical_id] = "cage-project-changed"
