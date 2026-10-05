"""Existing accounting semantics behind a durable caller's honest receipts."""

from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
import tempfile
from unittest.mock import patch

from cage_core import cli, monitor, poketoken
from cage_core.accounting import backends, execution, grants, queue, runtime, store, worker
from cage_core.models import StoragePolicy
from cage_core.monitoring import collector, connection, hub, publication, scheduler, service, snapshots
from monitor_test_support import FINGERPRINT, MonitorTestCase

TRANSPORT = {"environment": {"DOCKER_CONTEXT": "fixture"}, "engine": "fixture-engine-1234"}


class AccountingBackendTests(MonitorTestCase):
    def fixture(self, root):
        monitor.save_connection(root, monitor.MonitorConnection("https://hub.example", "synthetic"))
        records = self._registered_monitor_projects(root, "current", "peer")
        monitor.save_split_status(root, {"complete": True, "device_ids": []})
        source, permissions = backends.monitor_source(root, records[0], TRANSPORT, "0.38.11", StoragePolicy())
        queued = queue.enqueue(root, source, permissions, final=True)
        return records, queue.claim(root, queued["id"])

    def collected(self, record):
        return self._period_summary(record, record.logical_id, 5, "2026-09-03")

    def transport_and_collect(self, records):
        self.enterContext(patch.object(runtime, "endpoint", return_value=nullcontext()))
        fingerprints = {r.volume_name: r.fingerprint for r in records}
        from cage_core.monitoring import volumes
        self.enterContext(patch.object(volumes, "volume_fingerprint", side_effect=lambda d, n: fingerprints[n]))
        self.enterContext(patch.object(volumes, "volume_fingerprints", side_effect=lambda d, ns: {n: fingerprints[n] for n in ns}))
        self.enterContext(patch.object(collector, "ensure_collector_image", return_value="fixture"))
        return self.enterContext(patch.object(collector, "_run_collector", side_effect=lambda d, i, r, *a, **kw: self.collected(r)))

    def test_busy_coordinator_keeps_collection_receipt_without_delivery(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            collected = self.transport_and_collect(records)
            with patch.object(scheduler, "try_coordinator_lease", return_value=nullcontext(False)):
                with self.assertRaisesRegex(store.AccountingError, "coordinator_busy"):
                    backends.execute(root, "/unused", root, job)
            current = queue.jobs(root)[0]
            self.assertEqual(current["collected"], 1)
            self.assertEqual(current["delivered"], 0)
            self.assertTrue(current["snapshot"])
            collected.assert_called_once()

    def test_final_job_fills_missing_peer_and_acknowledges_complete_generation(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            collected = self.transport_and_collect(records)
            with patch.object(hub, "upload_summary") as upload:
                receipt = backends.execute(root, "/unused", root, job)
            self.assertEqual(collected.call_count, 2)
            upload.assert_called_once()
            state = queue.jobs(root)[0]
            self.assertEqual(state["phase"], "delivered")
            self.assertEqual(state["collected"], state["delivered"])
            self.assertEqual(receipt, "generation:" + snapshots.load_aggregate_status(root)["last_good_generation"])

    def test_peer_failure_never_uses_old_status_as_a_delivery_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            collected = self.transport_and_collect(records)
            collected.side_effect = lambda d, i, r, *a, **kw: self.collected(r) if r == records[0] else (_ for _ in ()).throw(monitor.MonitorError("fixture peer unavailable"))
            with patch.object(hub, "upload_summary") as upload:
                with self.assertRaises(monitor.MonitorError):
                    backends.execute(root, "/unused", root, job)
            upload.assert_not_called()
            self.assertEqual(queue.jobs(root)[0]["collected"], 1)
            self.assertEqual(queue.jobs(root)[0]["delivered"], 0)

    def test_disable_during_collection_cannot_write_snapshot_or_reactivate_source(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            collected = self.transport_and_collect(records)

            def finish(*a, **kw):
                monitor.retire_registration(root, records[0].logical_id, disabled=True)
                return self.collected(records[0])

            collected.side_effect = finish
            with patch.object(hub, "upload_summary") as upload:
                with self.assertRaises(monitor.MonitorError):
                    backends.execute(root, "/unused", root, job)
            upload.assert_not_called()
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")
            self.assertIsNone(monitor.load_volume_snapshot(root, records[0]))
            with self.assertRaises(monitor.MonitorError):
                service._mark_scan_success(root, records, "2026-09-03T00:00:00Z")
            self.assertEqual(monitor.load_registry(root)[0].status, "disabled")

    def test_reconnect_with_identical_credentials_cannot_revive_old_work(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            old = connection.load_connection(root)
            connection.disable_connection(root)
            connection.save_connection(root, old)
            with patch.object(runtime, "endpoint") as endpoint:
                with self.assertRaises(grants.Revoked):
                    backends.execute(root, "/unused", root, job)
            endpoint.assert_not_called()
            self.assertNotEqual(old.epoch, connection.load_connection(root).epoch)

    def test_source_disable_and_readopt_with_same_fingerprint_is_not_same_permission(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            monitor.retire_registration(root, records[0].logical_id, disabled=True)
            monitor.save_registry(root, records)
            with self.assertRaises(grants.Revoked):
                grants.require(root, job["grants"])

    def test_queue_cannot_supply_an_executable_or_drop_connection_permission(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            for changed in (
                {**job, "source": {**job["source"], "command": ["/bin/sh"]}},
                {**job, "grants": {"monitor:" + records[0].logical_id: job["grants"]["monitor:" + records[0].logical_id]}},
            ):
                with patch.object(runtime, "endpoint") as endpoint:
                    with self.assertRaises(store.Invalid):
                        backends.execute(root, "/unused", root, changed)
                endpoint.assert_not_called()

    def test_first_poke_export_rejects_replacement_before_collecting(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = dict(backend="poke", volume=FINGERPRINT["name"], fingerprint=FINGERPRINT,
                          image="sha256:" + "a" * 64, transport=TRANSPORT, version="0.38.11")
            permissions = {k: grants.ensure(root, k) for k in ("poke", "poke:" + source["volume"])}
            queued = queue.enqueue(root, source, permissions)
            job = queue.claim(root, queued["id"])
            with patch.object(runtime, "endpoint", return_value=nullcontext()), patch.object(
                backends.volumes, "volume_fingerprint", return_value={**FINGERPRINT, "created_at": "replacement"}
            ), patch.object(poketoken, "sync_source") as export:
                with self.assertRaises(grants.Revoked):
                    backends.execute(root, "/unused", root, job)
            export.assert_not_called()

    def test_retry_reuses_verified_snapshot_but_a_new_revision_collects_again(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            records, job = self.fixture(root)
            collected = self.transport_and_collect(records)
            with patch.object(scheduler, "try_coordinator_lease", return_value=nullcontext(False)):
                with self.assertRaises(store.AccountingError):
                    backends.execute(root, "/unused", root, job)
            second = queue.claim(root, job["id"])
            with patch.object(scheduler, "try_coordinator_lease", return_value=nullcontext(False)):
                with self.assertRaises(store.AccountingError):
                    backends.execute(root, "/unused", root, second)
            self.assertEqual(collected.call_count, 1)
            queue.enqueue(root, job["source"], job["grants"], final=True)
            third = queue.claim(root, job["id"])
            with patch.object(scheduler, "try_coordinator_lease", return_value=nullcontext(False)):
                with self.assertRaises(store.AccountingError):
                    backends.execute(root, "/unused", root, third)
            self.assertEqual(collected.call_count, 2)
