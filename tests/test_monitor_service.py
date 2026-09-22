import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from cage_core import cli, monitor
from cage_core.monitoring import (
    collector as collector_api,
    constants as constants_api,
    hub as hub_api,
    lifecycle as lifecycle_api,
    scheduler as scheduler_api,
    service as service_api,
    snapshots as snapshots_api,
    state as state_api,
    volumes as volumes_api,
)
from monitor_test_support import FINGERPRINT, MonitorTestCase


class MonitorServiceTests(MonitorTestCase):
    def test_pending_provider_label_blocks_normal_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            monitor.approve_provider_label(root, "approved-provider")
            with patch.object(service_api, "_collect_registered_summaries") as collect:
                with self.assertRaisesRegex(
                    monitor.MonitorError, "provider label migration is pending"
                ):
                    monitor.scan_all_registrations(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.36.2",
                        storage_policy=object(),
                        allow_build=False,
                        force=True,
                    )
            collect.assert_not_called()

    def test_scan_clears_a_provider_stream_that_has_no_remaining_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            record = monitor.VolumeRegistration(
                "a" * 32,
                device_id,
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            old_provider_id = monitor.provider_device_id(root, "zllm")
            monitor.save_split_status(
                root,
                {"complete": True, "device_ids": [old_provider_id]},
            )
            state_api._write_json(
                monitor.monitor_root(root) / constants_api.AGGREGATE_STATUS_FILE,
                {
                    "version": constants_api.STATE_VERSION,
                    "device_id": device_id,
                    "device_ids": [old_provider_id],
                    "providers": {
                        "zllm": {
                            "device_id": old_provider_id,
                            "provider": "zllm",
                            "provider_label": "ZLLM",
                            "total_tokens": 100,
                            "cost_usd": 0,
                        }
                    },
                    "updated_at": "2026-08-27T00:00:00Z",
                    "project_count": 1,
                    "duplicate_sessions": 0,
                    "total_tokens": 100,
                    "cost_usd": 0,
                    "priced_tokens": 0,
                    "unpriced_tokens": 100,
                    "price_coverage_percent": 0,
                    "missing_models": ["gpt-test"],
                    "missing_prices": ["zllm:gpt-test"],
                },
            )

            def collect(_docker, _image, _record, _root, **_kwargs):
                return self._summary(device_id, {})

            with patch.object(
                volumes_api, "volume_fingerprint", return_value=FINGERPRINT
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ), patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                _updated, manifest = monitor.scan_all_registrations(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.34.0",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                )

            upload.assert_called_once()
            cleared = upload.call_args.args[1]
            self.assertEqual(cleared["deviceId"], old_provider_id)
            self.assertEqual(cleared["allTime"]["totalTokens"], 0)
            self.assertEqual(manifest["device_ids"], [old_provider_id])
            self.assertEqual(manifest["providers"]["zllm"]["total_tokens"], 0)

    def test_normal_scan_stops_when_legacy_unsplit_device_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            record = monitor.VolumeRegistration(
                "a" * 32,
                monitor.host_device_id(root),
                "codex-state-a",
                "container",
                "/work/a",
                "Cage: a (Container)",
                FINGERPRINT,
            )
            monitor.save_registry(root, [record])
            stats = {
                "devices": [{"deviceId": monitor.host_device_id(root)}],
                "periods": {},
            }
            with patch.object(hub_api, "_hub_request", return_value=stats):
                with self.assertRaisesRegex(monitor.MonitorError, "split migration is pending"):
                    monitor.scan_all_registrations(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.34.0",
                        storage_policy=object(),
                        allow_build=False,
                        force=True,
                    )

    def test_scan_all_collects_each_project_and_uploads_provider_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    value * 32,
                    device_id,
                    f"codex-state-{value}",
                    "container",
                    f"/work/{value}",
                    f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for value in ("a", "b")
            ]
            monitor.save_registry(root, records)

            def collect(_docker, _image, record, _root, **_kwargs):
                session = self._session(
                    record.logical_id,
                    total=100,
                    input_tokens=80,
                    output_tokens=20,
                )
                return self._summary(
                    device_id,
                    {f"codex:{record.logical_id}": session},
                )

            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: next(
                    item.fingerprint for item in records if item.volume_name == name
                ),
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api,
                "_hub_request",
                return_value={"devices": [], "periods": {}},
            ), patch.object(hub_api, "upload_summary") as upload:
                updated, manifest = monitor.scan_all_registrations(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.32.0",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                )
            self.assertEqual(len(updated), 2)
            self.assertEqual(collector.call_count, 2)
            upload.assert_called_once()
            uploaded = upload.call_args.args[1]
            self.assertEqual(
                uploaded["deviceId"], monitor.provider_device_id(root, "openai-api")
            )
            self.assertEqual(uploaded["allTime"]["totalTokens"], 200)
            self.assertEqual(manifest["device_ids"], [uploaded["deviceId"]])
            self.assertEqual(manifest["providers"]["openai-api"]["total_tokens"], 200)

    def test_incremental_scan_refreshes_current_and_reuses_cached_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    f"{index:032x}",
                    device_id,
                    f"codex-state-{value}",
                    "container",
                    f"/work/{value}",
                    f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for index, value in enumerate(("current", "peer"))
            ]
            monitor.save_registry(root, records)
            current, peer = records
            peer_payload = self._summary(
                peer.device_id,
                {
                    "codex:peer": self._session(
                        "peer", total=11, input_tokens=8, output_tokens=3
                    )
                },
            )
            snapshots_api._save_volume_snapshot(root, peer, peer_payload)
            scheduler = scheduler_api._default_scheduler_state()
            scheduler["next_full_reconciliation_at"] = time.time() + 3600
            monitor.save_scheduler_state(root, scheduler)
            current_payload = self._summary(
                current.device_id,
                {
                    "codex:current": self._session(
                        "current", total=7, input_tokens=5, output_tokens=2
                    )
                },
            )

            def collect(_docker, _image, record, _root, **_kwargs):
                self.assertEqual(record.logical_id, current.logical_id)
                return current_payload

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload, patch.object(
                service_api,
                "scan_all_registrations",
                side_effect=AssertionError("incremental scan called full reconciliation"),
            ):
                updated, status = monitor.scan_registration(
                    root,
                    "docker",
                    Path("/work/cage"),
                    current,
                    version="0.35.0",
                    storage_policy=object(),
                    allow_build=False,
                )

            self.assertEqual(updated.logical_id, current.logical_id)
            self.assertEqual(collector.call_count, 1)
            upload.assert_called_once()
            self.assertEqual(status["total_tokens"], 18)
            self.assertEqual(
                monitor.load_volume_snapshot(root, peer)["allTime"]["totalTokens"],
                11,
            )

    def test_incremental_scan_refreshes_cached_peer_after_utc_rollover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            records = self._registered_monitor_projects(root, "current", "peer")
            current, peer = records
            current_window = self._period_windows("2026-09-03")
            snapshots_api._save_volume_snapshot(
                root,
                peer,
                self._period_summary(peer, "peer", 11, "2026-09-02"),
            )
            scheduler = scheduler_api._default_scheduler_state()
            scheduler["next_full_reconciliation_at"] = time.time() + 3600
            monitor.save_scheduler_state(root, scheduler)
            current_payload = self._period_summary(
                current, "current", 7, "2026-09-03"
            )
            peer_payload = self._period_summary(peer, "peer", 11, "2026-09-03")
            calls = []

            def collect(_docker, _image, record, _root, **_kwargs):
                calls.append(record.logical_id)
                return (
                    current_payload
                    if record.logical_id == current.logical_id
                    else peer_payload
                )

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                _updated, status = monitor.scan_registration(
                    root,
                    "docker",
                    Path("/work/cage"),
                    current,
                    version="0.36.3",
                    storage_policy=object(),
                    allow_build=False,
                )

            self.assertEqual(calls, [current.logical_id, peer.logical_id])
            self.assertEqual(collector.call_count, 2)
            upload.assert_called_once()
            self.assertEqual(status["total_tokens"], 18)
            self.assertEqual(
                monitor.load_volume_snapshot(root, peer)["periodWindows"],
                current_window,
            )

    def test_full_scan_retries_only_pre_rollover_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            records = self._registered_monitor_projects(root, "a", "b")
            current_window = self._period_windows("2026-09-03")
            old_a = self._period_summary(records[0], "a", 100, "2026-09-02")
            new_b = self._period_summary(records[1], "b", 100, "2026-09-03")
            new_a = self._period_summary(records[0], "a", 100, "2026-09-03")
            responses = [old_a, new_b, new_a]
            calls = []

            def collect(_docker, _image, record, _root, **_kwargs):
                calls.append(record.logical_id)
                return responses.pop(0)

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                _updated, status = monitor.scan_all_registrations(
                    root,
                    "docker",
                    Path("/work/cage"),
                    version="0.36.3",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                )

            self.assertEqual(
                calls,
                [records[0].logical_id, records[1].logical_id, records[0].logical_id],
            )
            self.assertEqual(collector.call_count, 3)
            upload.assert_called_once()
            self.assertEqual(status["total_tokens"], 200)
            for record in records:
                self.assertEqual(
                    monitor.load_volume_snapshot(root, record)["periodWindows"],
                    current_window,
                )

    def test_due_reconciliation_retries_current_summary_after_utc_rollover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            records = self._registered_monitor_projects(root, "current", "peer")
            current, peer = records
            old_current = self._period_summary(
                current, "current", 7, "2026-09-02"
            )
            new_peer = self._period_summary(peer, "peer", 11, "2026-09-03")
            new_current = self._period_summary(
                current, "current", 7, "2026-09-03"
            )
            responses = [old_current, new_peer, new_current]
            calls = []

            scheduler = scheduler_api._default_scheduler_state()
            scheduler["next_full_reconciliation_at"] = 0.0
            monitor.save_scheduler_state(root, scheduler)

            def collect(_docker, _image, record, _root, **_kwargs):
                calls.append(record.logical_id)
                return responses.pop(0)

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                _updated, status = monitor.scan_registration(
                    root,
                    "docker",
                    Path("/work/cage"),
                    current,
                    version="0.36.3",
                    storage_policy=object(),
                    allow_build=False,
                )

            self.assertEqual(
                calls,
                [current.logical_id, peer.logical_id, current.logical_id],
            )
            self.assertEqual(collector.call_count, 3)
            upload.assert_called_once()
            self.assertEqual(status["total_tokens"], 18)

    def test_full_scan_preserves_hub_snapshot_when_retry_still_crosses_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            records = self._registered_monitor_projects(root, "a", "b")
            responses = [
                self._period_summary(records[0], "a", 100, "2026-09-02"),
                self._period_summary(records[1], "b", 100, "2026-09-03"),
                self._period_summary(records[0], "a", 100, "2026-09-02"),
            ]
            calls = []

            def collect(_docker, _image, record, _root, **_kwargs):
                calls.append(record.logical_id)
                return responses.pop(0)

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                with self.assertRaisesRegex(
                    monitor.MonitorError,
                    "collector period windows changed during the aggregate scan",
                ):
                    monitor.scan_all_registrations(
                        root,
                        "docker",
                        Path("/work/cage"),
                        version="0.36.3",
                        storage_policy=object(),
                        allow_build=False,
                        force=True,
                    )

            self.assertEqual(
                calls,
                [records[0].logical_id, records[1].logical_id, records[0].logical_id],
            )
            self.assertEqual(collector.call_count, 3)
            upload.assert_not_called()

    def test_incremental_scan_rejects_replaced_cached_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    f"{index:032x}",
                    device_id,
                    f"codex-state-{value}",
                    "container",
                    f"/work/{value}",
                    f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for index, value in enumerate(("current", "peer"))
            ]
            monitor.save_registry(root, records)
            monitor.save_split_status(root, {"complete": True, "device_ids": []})
            for item in records:
                snapshots_api._save_volume_snapshot(root, item, self._summary(item.device_id, {}))
            scheduler = scheduler_api._default_scheduler_state()
            scheduler["next_full_reconciliation_at"] = time.time() + 3600
            monitor.save_scheduler_state(root, scheduler)
            changed = dict(records[1].fingerprint, created_at="2026-08-28T00:00:00Z")
            fingerprints = {
                records[0].volume_name: records[0].fingerprint,
                records[1].volume_name: changed,
            }
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ):
                with self.assertRaisesRegex(monitor.MonitorError, "volume changed"):
                    monitor.scan_registration(
                        root,
                        "docker",
                        Path("/work/cage"),
                        records[0],
                        version="0.35.0",
                        storage_policy=object(),
                        allow_build=False,
                    )
            self.assertEqual(monitor.load_registry(root)[1].status, "needs-adoption")

    def test_corrupt_peer_snapshot_is_persisted_without_interrupting_interactive_prompt(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            current, peer = self._registered_monitor_projects(root, "current", "peer")
            monitor.save_split_status(root, {"complete": True, "device_ids": []})
            for record in (current, peer):
                snapshots_api._save_volume_snapshot(root, record, self._summary(record.device_id, {}))
            # A syntactically valid but incomplete cached peer fails during
            # coordinator preflight, after the current snapshot is refreshed.
            state_api._write_json(snapshots_api._volume_snapshot_path(root, peer), {"version": 1})

            def scan(force):
                return monitor.scan_registration(
                    root, "docker", Path("/work/cage"), current,
                    version="0.37.2", storage_policy=object(),
                    allow_build=False, force=force,
                )

            terminal = Terminal()
            with patch.object(
                volumes_api, "volume_fingerprint", return_value=current.fingerprint
            ), patch.object(
                lifecycle_api.threading.Thread, "start"
            ), patch.object(
                hub_api, "upload_summary"
            ) as upload, contextlib.redirect_stderr(terminal):
                worker = monitor.ActiveMonitor(scan, 30)
                worker._stop.set()
                worker._run()

            self.assertEqual(terminal.getvalue(), "")
            upload.assert_not_called()
            records = monitor.load_registry(root)
            for record in records:
                self.assertIn("snapshot has an invalid shape", record.last_error)
                self.assertTrue(record.last_scan_at)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ):
                self.assertEqual(cli._monitor_status(root, as_json=True), 0)
            status = json.loads(output.getvalue())
            self.assertTrue(all(
                "snapshot has an invalid shape" in project["last_error"]
                for project in status["projects"]
            ))

    def test_ten_staggered_launches_share_one_host_wide_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    f"{index:032x}",
                    device_id,
                    f"codex-state-{index}",
                    "container",
                    f"/work/{index}",
                    f"Cage: {index} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{index}"),
                )
                for index in range(10)
            ]
            monitor.save_registry(root, records)
            monitor.save_split_status(root, {"complete": True, "device_ids": []})
            for item in records:
                snapshots_api._save_volume_snapshot(root, item, self._summary(item.device_id, {}))

            fingerprints = {item.volume_name: item.fingerprint for item in records}
            barrier = threading.Barrier(len(records))
            results = []
            errors = []
            result_lock = threading.Lock()

            def launch(item):
                try:
                    barrier.wait(timeout=5)
                    result = monitor.scan_registration(
                        root,
                        "docker",
                        Path("/work/cage"),
                        item,
                        version="0.35.0",
                        storage_policy=object(),
                        allow_build=False,
                    )
                    with result_lock:
                        results.append(result)
                except BaseException as exc:
                    with result_lock:
                        errors.append(exc)

            def collect(_docker, _image, item, _root, **_kwargs):
                session = self._session(
                    item.logical_id, total=1, input_tokens=1, output_tokens=0
                )
                return self._summary(item.device_id, {f"codex:{item.logical_id}": session})

            threads = [threading.Thread(target=launch, args=(item,)) for item in records]
            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", side_effect=collect
            ) as collector, patch.object(
                hub_api, "_hub_request", return_value={"devices": [], "periods": {}}
            ), patch.object(hub_api, "upload_summary") as upload:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), len(records))
            # The first launch owns the due full reconciliation. Its current
            # snapshot is reused as the one fresh input, so nine peers need a
            # collector; no launch rescans all ten volumes independently.
            self.assertLessEqual(collector.call_count, len(records))
            upload.assert_called_once()
            self.assertTrue(monitor.load_scheduler_state(root)["last_generation"])

    def test_coordinator_lease_is_taken_over_after_owner_crash(self):
        if not hasattr(os, "fork"):
            self.skipTest("process fork is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid = os.fork()
            if pid == 0:
                try:
                    with monitor.try_coordinator_lease(root) as acquired:
                        os._exit(0 if acquired else 2)
                except BaseException:
                    os._exit(3)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertTrue(
                (root / "monitor" / "locks" / "coordinator-lease.json").exists()
            )
            with monitor.try_coordinator_lease(root) as acquired:
                self.assertTrue(acquired)
            self.assertFalse(
                (root / "monitor" / "locks" / "coordinator-lease.json").exists()
            )

    def test_active_monitor_uses_wall_clock_boundaries_and_current_only_exit(self):
        calls = []
        clock = [100.0]

        class FakeStop:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, seconds):
                self.waits.append(seconds)
                if len(self.waits) == 1:
                    # The first scan took five seconds. The next wait should
                    # catch the already-passed boundary, not add one interval.
                    clock[0] = 150.0
                    return False
                return True

        stop = FakeStop()

        def scan(force):
            calls.append(force)
            if len(calls) == 1:
                clock[0] = 125.0

        worker = object.__new__(monitor.ActiveMonitor)
        worker._scan = scan
        worker._interval = 30
        worker._stop = stop
        with patch.object(lifecycle_api.time, "time", side_effect=lambda: clock[0]):
            worker._run()

        self.assertEqual(calls, [False, False])
        self.assertEqual(stop.waits[0], 0.0)
        self.assertEqual(stop.waits[1], 30.0)

    def test_interactive_background_failure_stays_off_the_prompt(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        error = monitor.MonitorError("provider upload repair failed")
        terminal = Terminal()
        def scan(_force):
            raise error
        with contextlib.redirect_stderr(terminal):
            with patch.object(lifecycle_api.threading.Thread, "start"):
                worker = monitor.ActiveMonitor(scan, 30)
            worker._stop.set()
            worker._run()  # startup scan fails while Codex owns the terminal
            self.assertEqual(terminal.getvalue(), "")
            with patch.object(worker._thread, "join"):
                worker.stop()
                worker.stop()
        self.assertEqual(terminal.getvalue().count("WARNING:"), 1)
        self.assertIn("final Token Monitor scan skipped", terminal.getvalue())
        self.assertIn("cage monitor status", terminal.getvalue())

    def test_redirected_background_failures_are_still_logged(self):
        output = io.StringIO()
        def scan(_force):
            raise monitor.MonitorError("hub unavailable")
        with contextlib.redirect_stderr(output):
            with patch.object(lifecycle_api.threading.Thread, "start"):
                worker = monitor.ActiveMonitor(scan, 30)
            worker._stop.set()
            worker._run()
        self.assertIn("WARNING: Token Monitor scan skipped: hub unavailable", output.getvalue())

    def test_failed_full_reconciliation_waits_for_next_wall_clock_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = scheduler_api._default_scheduler_state()
            state["next_full_reconciliation_at"] = 100.0
            state["full_reconciliation_in_progress"] = {
                "owner": "owner",
                "scheduled_at": 100.0,
                "started_at": 100.0,
                "expires_at": 200.0,
            }
            monitor.save_scheduler_state(root, state)
            with patch.object(lifecycle_api.time, "time", return_value=101.0):
                scheduler_api._fail_full_reconciliation(root, state, "hub unavailable")

            updated = monitor.load_scheduler_state(root)
            self.assertEqual(
                updated["next_full_reconciliation_at"],
                100.0 + monitor.FULL_RECONCILIATION_INTERVAL_SECONDS,
            )
            self.assertIsNone(updated["full_reconciliation_in_progress"])

    def test_final_refresh_does_not_take_due_full_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            monitor.save_connection(
                root, monitor.MonitorConnection("https://hub.example", "secret")
            )
            device_id = monitor.host_device_id(root)
            records = [
                monitor.VolumeRegistration(
                    f"{index:032x}",
                    device_id,
                    f"codex-state-{value}",
                    "container",
                    f"/work/{value}",
                    f"Cage: {value} (Container)",
                    dict(FINGERPRINT, name=f"codex-state-{value}"),
                )
                for index, value in enumerate(("current", "peer"))
            ]
            monitor.save_registry(root, records)
            monitor.save_split_status(root, {"complete": True, "device_ids": []})
            for item in records:
                snapshots_api._save_volume_snapshot(root, item, self._summary(item.device_id, {}))
            scheduler = scheduler_api._default_scheduler_state()
            scheduler["next_full_reconciliation_at"] = 1.0
            monitor.save_scheduler_state(root, scheduler)
            current, peer = records
            current_payload = self._summary(
                current.device_id,
                {
                    "codex:current": self._session(
                        "current", total=3, input_tokens=2, output_tokens=1
                    )
                },
            )
            fingerprints = {item.volume_name: item.fingerprint for item in records}

            with patch.object(
                volumes_api,
                "volume_fingerprint",
                side_effect=lambda _docker, name: fingerprints[name],
            ), patch.object(
                collector_api, "ensure_collector_image", return_value="collector"
            ), patch.object(
                collector_api, "_run_collector", return_value=current_payload
            ) as collector, patch.object(
                service_api, "_collect_registered_summaries",
                side_effect=AssertionError("final refresh performed a full scan"),
            ) as full, patch.object(
                hub_api, "upload_summary"
            ) as upload:
                updated, status = monitor.scan_registration(
                    root,
                    "docker",
                    Path("/work/cage"),
                    current,
                    version="0.35.0",
                    storage_policy=object(),
                    allow_build=False,
                    force=True,
                    final=True,
                )

            self.assertEqual(updated.logical_id, current.logical_id)
            self.assertEqual(status["total_tokens"], 3)
            collector.assert_called_once()
            full.assert_not_called()
            upload.assert_called_once()
            self.assertEqual(
                monitor.load_volume_snapshot(root, peer)["allTime"]["totalTokens"],
                0,
            )
