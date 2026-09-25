import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from cage_core import monitor
from cage_core.monitoring import accounting, collector, constants, hub, snapshots, state, thread_providers
from monitor_test_support import MonitorTestCase


def create_database(root, provider="openai-eu"):
    connection = sqlite3.connect(root / thread_providers.DATABASE_NAME)
    connection.execute("CREATE TABLE threads (model_provider TEXT, rollout_path TEXT)")
    connection.execute("INSERT INTO threads VALUES (?, ?)", (provider, "/private/history/demo.jsonl"))
    connection.commit()
    return connection


def test_database_reader_uses_wal_without_changing_source(tmp_path):
    connection = create_database(tmp_path, "zllm")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("UPDATE threads SET model_provider='openai-eu'")
    connection.commit()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if not p.name.endswith("-shm")}
    assert thread_providers.read_thread_providers(tmp_path) == {"demo": "openai-eu"}
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if not p.name.endswith("-shm")}
    connection.close()


def test_database_absent_is_not_created(tmp_path):
    assert thread_providers.read_thread_providers(tmp_path) == {}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "corrupt", "schema", "oversized", "invalid-row"])
def test_database_reader_rejects_unsafe_or_invalid_metadata(tmp_path, kind):
    path = tmp_path / thread_providers.DATABASE_NAME
    if kind == "corrupt":
        path.write_bytes(b"not SQLite")
    elif kind == "schema":
        sqlite3.connect(path).close()
    elif kind == "oversized":
        with path.open("wb") as handle:
            handle.truncate(thread_providers.MAX_DATABASE_BYTES + 1)
    else:
        connection = create_database(tmp_path, "bad\nprovider" if kind == "invalid-row" else "zllm")
        connection.close()
        if kind == "symlink":
            path.rename(tmp_path / "real.sqlite")
            path.symlink_to(tmp_path / "real.sqlite")
        elif kind == "hardlink":
            os.link(path, tmp_path / "alias.sqlite")
    with pytest.raises((ValueError, sqlite3.Error)):
        thread_providers.read_thread_providers(tmp_path)


class ProviderEvidenceTests(MonitorTestCase):
    def mixed_session(self):
        session = self._session("demo", total=330, input_tokens=300, output_tokens=30, provider="zllm")
        session["messageCount"] = 2
        session["providerTokenUsage"] = {
            provider: {"messageCount": 1, "reasoningTokens": 0, "models": {
                "gpt-test": {"totalTokens": factor * 110, "inputTokens": factor * 10,
                             "outputTokens": factor * 10, "cacheReadTokens": 0,
                             "cacheWriteTokens": factor * 90, "cacheWriteVerified": True},
            }}
            for provider, factor in (("zllm", 1), ("openai", 2))
        }
        return session

    def test_exact_provider_splits_survive_old_ambiguity_and_replica_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = self._registered_monitor_projects(root, "one", "two")
            left = self._summary(first.device_id, {"codex:demo": self.mixed_session()})
            right = copy.deepcopy(left)
            right["deviceId"] = second.device_id
            for period in ("today", "month", "allTime"):
                right[period]["sessions"]["codex:demo"].pop("providerTokenUsage", None)
                right[period]["sessions"]["codex:demo"]["providers"] = {"unattributed": 330}
            state._write_json(root / "provider-observations.json", {
                "version": 1, "logical_id": first.logical_id, "fingerprint": first.fingerprint,
                "sessions": {"demo": ["openai-api", "unattributed", "zllm"]},
            })
            state._write_json(root / "provider-evidence.json", {"version": 1, "observations": [{}, {}]})
            collector._apply_provider_evidence(root, left, first)
            original = copy.deepcopy(left)
            monitor.save_pricing(root, {
                f"{provider}:gpt-test": {"input_per_million": rate, "output_per_million": rate,
                                       "cache_read_per_million": rate, "cache_write_per_million": rate}
                for provider, rate in (("zllm", 1), ("openai-api", 2))
            })
            streams, status = monitor.aggregate_provider_summaries(root, [(first, left), (second, right)])
            self.assertEqual(left, original)
            self.assertEqual(set(streams), {"zllm", "openai-api"})
            self.assertEqual(status["duplicate_sessions"], 1)
            for period in ("today", "month", "allTime"):
                self.assertEqual(streams["zllm"][0][period]["totalTokens"], 110)
                self.assertEqual(streams["openai-api"][0][period]["totalTokens"], 220)
                self.assertEqual(streams["zllm"][0][period]["cacheWriteTokens"], 90)
                self.assertAlmostEqual(streams["openai-api"][0][period]["costUsd"], .000440)
            for payload, _ in streams.values():
                wire = json.dumps(hub._outbound_payload(root, payload))
                self.assertNotIn("providerTokenUsage", wire)
                self.assertNotIn("modelTokenUsage", wire)
                self.assertNotIn('"sessionId": "demo"', wire)

    def test_unapproved_provider_slices_merge_without_losing_known_usage(self):
        session = self.mixed_session()
        first = session["providerTokenUsage"].pop("zllm")
        second = session["providerTokenUsage"].pop("openai")
        session["providerTokenUsage"] = {"private-one": first, "private-two": second}
        original = copy.deepcopy(session)
        slices = accounting._provider_slices(session, allowed_provider_ids={"openai-api", "zllm"})
        self.assertEqual(set(slices), {"unattributed"})
        self.assertEqual(slices["unattributed"]["totalTokens"], 330)
        self.assertEqual(slices["unattributed"]["modelTokenUsage"]["gpt-test"]["cacheWriteTokens"], 270)
        self.assertEqual(session, original)

    def test_provider_split_rejects_component_or_message_mismatches(self):
        for field in ("totalTokens", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
            with self.subTest(field=field):
                session = self.mixed_session()
                session["providerTokenUsage"]["zllm"]["models"]["gpt-test"][field] += 1
                with self.assertRaises(monitor.MonitorError):
                    accounting._provider_token_usage(session)
        session = self.mixed_session()
        session["providerTokenUsage"]["zllm"]["messageCount"] += 1
        with self.assertRaisesRegex(monitor.MonitorError, "do not reconcile"):
            accounting._provider_token_usage(session)

    def test_whole_model_price_is_preserved_but_not_divided_between_providers(self):
        session = self.mixed_session()
        session["modelCosts"] = {"gpt-test": 1.0}
        slices = accounting._provider_token_usage(session)
        self.assertEqual(slices["zllm"]["modelCosts"], {})
        self.assertEqual(slices["openai"]["modelCosts"], {})
        session["providerTokenUsage"] = {"openai": {
            "messageCount": 2, "reasoningTokens": 0,
            "models": {"gpt-test": {
                "totalTokens": 330, "inputTokens": 300, "outputTokens": 30,
                "cacheReadTokens": 0, "cacheWriteTokens": 0, "cacheWriteVerified": True,
            }},
        }}
        self.assertEqual(accounting._provider_token_usage(session)["openai"]["modelCosts"], {"gpt-test": 1.0})

    def test_private_provider_snapshots_upgrade_without_breaking_legacy_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._registered_monitor_projects(root, "demo")[0]
            payload = self._summary(record.device_id, {"codex:demo": self.mixed_session()})
            snapshots._save_volume_snapshot(root, record, payload)
            path = snapshots._volume_snapshot_path(root, record)
            saved = state._read_json(path, max_bytes=constants.MAX_SNAPSHOT_BYTES)
            self.assertEqual(saved["version"], 2)
            self.assertEqual(snapshots.load_volume_snapshot(root, record), payload)
            with patch.object(constants, "VOLUME_SNAPSHOT_VERSION", 1):
                with self.assertRaisesRegex(monitor.MonitorError, "identity does not match"):
                    snapshots.load_volume_snapshot(root, record)
            saved["version"] = 1
            state._write_json(path, saved)
            self.assertEqual(snapshots.load_volume_snapshot(root, record), payload)

    def test_conflicting_exact_provider_evidence_stops_publication(self):
        left = self.mixed_session()
        right = copy.deepcopy(left)
        right["providerTokenUsage"]["zllm"], right["providerTokenUsage"]["openai"] = (
            right["providerTokenUsage"]["openai"], right["providerTokenUsage"]["zllm"]
        )
        with tempfile.TemporaryDirectory() as directory:
            records = self._registered_monitor_projects(Path(directory), "one", "two")
            with self.assertRaisesRegex(monitor.MonitorError, "conflicting provider token evidence"):
                accounting._select_session([(records[0], left), (records[1], right)])

    def test_mismatch_is_unattributed_and_persists_after_switch_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._registered_monitor_projects(root, "demo")[0]
            original = self._summary(record.device_id, {
                "codex:demo": self._session("demo", total=110, input_tokens=100, output_tokens=10, provider="zllm"),
            })
            state._write_json(root / "provider-evidence.json", {
                "version": 1, "observations": [{"demo": "openai-eu"}, {"demo": "openai-eu"}],
            })
            payload = copy.deepcopy(original)
            collector._apply_provider_evidence(root, payload, record)
            for period in ("today", "month", "allTime"):
                expected = copy.deepcopy(original[period])
                expected["sessions"]["codex:demo"]["providers"] = {"unattributed": 110}
                self.assertEqual(payload[period], expected)
            state._write_json(root / "provider-evidence.json", {
                "version": 1, "observations": [{"demo": "zllm"}, {"demo": "zllm"}],
            })
            resumed = copy.deepcopy(original)
            collector._apply_provider_evidence(root, resumed, record)
            self.assertEqual(resumed, payload)
            streams, status = monitor.aggregate_provider_summaries(root, [(record, resumed)])
            self.assertEqual(set(streams), {"unattributed"})
            self.assertEqual(streams["unattributed"][0]["allTime"]["totalTokens"], 110)
            self.assertFalse(status["period_pricing"]["allTime"]["cost_complete"])
            wire = json.dumps(streams)
            self.assertNotIn("openai-eu", wire)
            self.assertNotIn("/private/", wire)

    def test_unchanged_custom_provider_and_absent_database_preserve_accounting(self):
        for observed in ({}, {"demo": "openai-eu"}):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = self._registered_monitor_projects(root, "demo")[0]
                payload = self._summary(record.device_id, {
                    "codex:demo": self._session("demo", total=110, input_tokens=100, output_tokens=10, provider="openai-eu"),
                })
                original = copy.deepcopy(payload)
                state._write_json(root / "provider-evidence.json", {
                    "version": 1, "observations": [observed, observed],
                })
                collector._apply_provider_evidence(root, payload, record)
                self.assertEqual(payload, original)

    def test_provider_change_during_scan_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._registered_monitor_projects(root, "demo")[0]
            payload = self._summary(record.device_id, {
                "codex:demo": self._session("demo", total=110, input_tokens=100, output_tokens=10, provider="zllm"),
            })
            state._write_json(root / "provider-evidence.json", {
                "version": 1, "observations": [{"demo": "openai"}, {"demo": "zllm"}],
            })
            collector._apply_provider_evidence(root, payload, record)
            self.assertEqual(payload["today"]["sessions"]["codex:demo"]["providers"], {"unattributed": 110})

    def test_missing_malformed_and_wrong_identity_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._registered_monitor_projects(root, "demo")[0]
            payload = self._summary(record.device_id, {})
            with self.assertRaisesRegex(monitor.MonitorError, "evidence is missing"):
                collector._apply_provider_evidence(root, payload, record)
            state._write_json(root / "provider-evidence.json", {"version": 1, "observations": ["/private/path"]})
            with self.assertRaisesRegex(monitor.MonitorError, "evidence is invalid"):
                collector._apply_provider_evidence(root, payload, record)
            state._write_json(root / "provider-observations.json", {
                "version": 1, "logical_id": record.logical_id, "fingerprint": {}, "sessions": {},
            })
            with self.assertRaisesRegex(monitor.MonitorError, "invalid identity"):
                collector._apply_provider_evidence(root, payload, record)

    def test_conflicting_replica_providers_conserve_tokens_across_all_periods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = self._registered_monitor_projects(root, "one", "two")
            session = self._session("demo", total=110, input_tokens=100, output_tokens=10, provider="zllm")
            left = self._summary(first.device_id, {"codex:demo": copy.deepcopy(session)})
            right = self._summary(second.device_id, {"codex:demo": copy.deepcopy(session)})
            right["allTime"] = copy.deepcopy(right["allTime"])
            right["allTime"]["sessions"]["codex:demo"]["providers"] = {"unattributed": 110}
            right["today"] = {"totalTokens": 0, "costUsd": 0, "sessions": {}}
            streams, _ = monitor.aggregate_provider_summaries(root, [(first, left), (second, right)])
            self.assertEqual(set(streams), {"unattributed"})
            for period in ("today", "month", "allTime"):
                self.assertEqual(streams["unattributed"][0][period]["totalTokens"], 110)

    def test_container_metadata_mounts_are_exact_readonly_no_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._registered_monitor_projects(root, "demo")[0]
            with patch.object(collector, "_subpath_available", return_value=True) as probe:
                mounts = collector._provider_metadata_mounts("docker", "image", root, record)
            self.assertEqual(probe.call_count, 3)
            self.assertEqual(len(mounts), 6)
            for mount in mounts[1::2]:
                self.assertIn("readonly,volume-subpath=state_5.sqlite", mount)
                self.assertIn("volume-nocopy", mount)
                self.assertNotIn("auth", mount)
