"""Scheduler admission and final-accounting shutdown regressions."""

from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from cage_core import monitor, poketoken
from cage_core.lifecycle import LifecycleCoordinator
from cage_core.models import ResolvedConfig
from cage_core.targets import container, host


def make_worker(kind, scan):
    with patch.object(threading.Thread, "start"):
        return monitor.ActiveMonitor(scan, 30) if kind == "monitor" else poketoken.ActiveExport(scan)


@pytest.mark.parametrize("kind", ["monitor", "poketoken"])
def test_stop_request_skips_unadmitted_startup_and_keeps_final_accounting(kind):
    scan = Mock()
    worker = make_worker(kind, scan)
    with patch.object(worker._thread, "join") as join:
        worker.request_stop()
        worker.request_stop()
        scan.assert_not_called()
        join.assert_not_called()
        worker._run()
        scan.assert_not_called()
        worker.stop()
    assert join.call_count == 1
    assert scan.call_count == 1
    if kind == "monitor":
        scan.assert_called_once_with(True)


@pytest.mark.parametrize("kind", ["monitor", "poketoken"])
def test_stop_winning_expired_tick_race_prevents_new_admission(kind):
    scan = Mock()
    worker = make_worker(kind, scan)

    class StopDuringWait(threading.Event):
        def wait(self, timeout=None):
            if self.is_set():
                return True
            # A timed-out wait may return False as stop wins the race.
            worker.request_stop()
            return False

    worker._stop = StopDuringWait()
    worker._run()
    assert scan.call_count == 1  # only the admitted startup scan
    with patch.object(worker._thread, "join"):
        worker.stop()
    assert scan.call_count == 2  # the mandatory final scan survives


@pytest.mark.parametrize("kind", ["monitor", "poketoken"])
def test_stop_request_preserves_admitted_work_without_waiting_for_it(kind):
    admitted = threading.Event()
    release = threading.Event()
    finalized = threading.Event()
    calls = []

    def scan(*args):
        calls.append(args)
        if len(calls) == 1:
            admitted.set()
            assert release.wait(2)
        else:
            finalized.set()

    worker = make_worker(kind, scan)
    closer = threading.Thread(target=worker.stop)
    try:
        worker._thread.start()
        assert admitted.wait(1)
        # This must return while the scheduled scan still owns its work.
        worker.request_stop()
        assert not release.is_set()
        closer.start()
        assert not finalized.wait(0.02)
        release.set()
        closer.join(2)
        assert not closer.is_alive()
        assert finalized.is_set()
        assert len(calls) == 2
    finally:
        release.set()
        worker.request_stop()
        worker._thread.join(2)
        if closer.ident is not None:
            closer.join(2)


def test_both_container_schedules_stop_before_a_blocked_final_export(tmp_path, monkeypatch):
    """A slow first cleanup cannot leave the other timer free to tick."""
    lifecycle = LifecycleCoordinator()
    runtime = SimpleNamespace(
        plan=SimpleNamespace(cage_version="dev", storage_policy=object(), capabilities=(poketoken.CAPABILITY,)),
        monitor_record=SimpleNamespace(), monitor_worker=None,
        config_root=tmp_path, install_root=tmp_path, docker="unused", lifecycle=lifecycle,
    )
    initial_monitor = threading.Event()
    initial_export = threading.Event()
    final_export = threading.Event()
    release_export = threading.Event()
    counts = {"monitor_ticks": 0, "export_ticks": 0, "monitor_final": 0, "export_final": 0}
    holder = {}
    real_export = poketoken.ActiveExport

    def construct_export(scan):
        holder["export"] = real_export(scan)
        return holder["export"]

    def monitor_scan(*args, **kwargs):
        if kwargs["final"]:
            counts["monitor_final"] += 1
        else:
            counts["monitor_ticks"] += 1
            initial_monitor.set()

    def export_scan(*args):
        # Scheduled work admitted before stop may begin after the request.
        # Its thread, rather than the stop bit, distinguishes final export.
        if threading.current_thread() is not holder["export"]._thread:
            final_export.set()
            assert release_export.wait(2)
            counts["export_final"] += 1
        else:
            counts["export_ticks"] += 1
            initial_export.set()

    monkeypatch.setattr(poketoken, "INTERVAL_SECONDS", 0.01)
    result = []
    closer = threading.Thread(target=lambda: result.append(lifecycle.cleanup()))
    with (
        patch.object(monitor, "load_connection", return_value=monitor.MonitorConnection("https://hub.example", "synthetic")),
        patch.object(monitor, "scan_registration", side_effect=monitor_scan),
        patch.object(poketoken, "sync", side_effect=export_scan),
        patch.object(poketoken, "ActiveExport", side_effect=construct_export),
    ):
        with patch.object(threading.Thread, "start"):
            container._start_codex_monitor(runtime)
            container._start_poketoken_export(runtime)
        token = runtime.monitor_worker
        export = holder["export"]
        token._interval = 0.01
        try:
            token._thread.start()
            export._thread.start()
            assert initial_monitor.wait(1)
            assert initial_export.wait(1)
            closer.start()
            assert final_export.wait(1)
            assert token._stop.is_set() and export._stop.is_set()
            token._thread.join(1)
            export._thread.join(1)
            assert not token._thread.is_alive() and not export._thread.is_alive()
            before = counts.copy()
            token._scheduled_scan()
            export._scheduled_attempt()
            assert counts == before
            release_export.set()
            closer.join(2)
            assert not closer.is_alive()
            assert result == [0]
            assert counts["monitor_final"] == counts["export_final"] == 1
            lifecycle.cleanup()
            assert counts["monitor_final"] == counts["export_final"] == 1
        finally:
            release_export.set()
            token.request_stop()
            export.request_stop()
            token._thread.join(2)
            export._thread.join(2)
            if closer.ident is not None:
                closer.join(2)


def test_host_quiesces_and_finalizes_before_auth_oauth_and_source_lease_cleanup(tmp_path):
    observed = []
    resolved = ResolvedConfig(
        config_path=tmp_path / "config.toml", repo_path=str(tmp_path),
        preset_name="main", preset_source="test", tool="codex", target="host", net="open",
        host_codex_dir=str(tmp_path / "source"),
    )
    prepared = SimpleNamespace(
        plan=SimpleNamespace(repository=str(tmp_path), runtime_config=resolved, yolo=False, network="open"),
        request=SimpleNamespace(tool_arguments=()),
    )
    record = SimpleNamespace(logical_id="a" * 32)
    session = SimpleNamespace(record=record, codex_home=tmp_path / "managed")
    worker = SimpleNamespace(
        request_stop=lambda: observed.append("stop-admission"),
        stop=lambda: observed.append("final-accounting"),
    )
    lease = SimpleNamespace(close=lambda: observed.append("source-lease"))
    broker = SimpleNamespace(result={"token": "synthetic"}, close=lambda: observed.append("oauth"))
    with (
        patch.object(host.monitor, "registered_host_source", return_value=record),
        patch.object(host.monitor.HostSourceLease, "acquire", return_value=lease),
        patch.object(host.monitor, "prepare_host_source", return_value=session),
        patch.object(host.monitor, "finish_host_source", side_effect=lambda _: observed.append("credentials")),
        patch.object(host, "agent_process_environment", return_value={}),
        patch.object(host.config, "host_codex_payload_for", return_value={}),
        patch.object(host.config, "host_codex_arg_lines", return_value=[]),
        patch.object(host, "_process_git_identity"),
        patch.object(host, "_process_ssh_identity"),
        patch.object(host, "_process_github_auth"),
        patch.object(host, "_pin_codex", return_value=Path("/unused/codex")),
        patch.object(host.oauth_broker, "selected_servers", return_value=[object()]),
        patch.object(host.oauth_broker, "connect", return_value=broker),
        patch.object(host.oauth_broker, "routed_servers", return_value=[]),
        patch.object(host, "_start_host_monitor", return_value=worker),
        patch.object(host, "_run_supervised_host_codex", side_effect=lambda *args: observed.append("child-exit") or 17),
    ):
        assert host.run_host_target(prepared, config_root=tmp_path, install_root=tmp_path) == 17
    assert observed == ["child-exit", "stop-admission", "final-accounting", "credentials", "oauth", "source-lease"]
