"""Durability, process ownership and admission races using private fixtures."""

from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from cage_core.accounting import execution, grants, lifecycle, queue, runtime, store, worker
from cage_core.lifecycle import LifecycleCoordinator

ROOT = Path(__file__).resolve().parents[1]


def admitted(root, *, backend="poke", tag="example"):
    permissions = {backend: grants.ensure(root, backend)}
    return {"backend": backend, "tag": tag}, permissions


def test_final_arriving_during_an_old_attempt_cannot_be_acknowledged(tmp_path):
    source, permissions = admitted(tmp_path)
    initial = queue.enqueue(tmp_path, source, permissions)
    first = queue.claim(tmp_path, initial["id"])
    queue.update(tmp_path, first, snapshot="snapshot-one")
    final = queue.enqueue(tmp_path, source, permissions, final=True)
    queue.update(tmp_path, first, receipt="generation-one")
    pending = queue.jobs(tmp_path)[0]
    assert pending["delivered"] == 1
    assert pending["requested"] == pending["final"] == 2
    assert pending["phase"] == "pending"
    second = queue.claim(tmp_path, final["id"])
    assert second["phase"] == "collecting"  # old snapshot cannot cover N+1
    queue.update(tmp_path, first, receipt="late-stale-callback")
    assert queue.jobs(tmp_path)[0]["receipt"] == "generation-one"
    queue.update(tmp_path, second, snapshot="snapshot-two", receipt="generation-two")
    assert queue.jobs(tmp_path)[0]["delivered"] == 2


def test_collected_is_not_delivered_and_backoff_survives_ticks(tmp_path, monkeypatch):
    source, permissions = admitted(tmp_path)
    job = queue.enqueue(tmp_path, source, permissions, final=True)
    claimed = queue.claim(tmp_path, job["id"])
    queue.update(tmp_path, claimed, snapshot="complete-local-snapshot")
    queue.update(tmp_path, claimed, error="coordinator busy")
    before = queue.jobs(tmp_path)[0]
    queue.enqueue(tmp_path, source, permissions)
    after = queue.jobs(tmp_path)[0]
    assert after["collected"] == 1 and after["delivered"] == 0
    assert (before["attempts"], before["next_due"]) == (after["attempts"], after["next_due"])
    assert queue.claim(tmp_path, job["id"]) is None
    monkeypatch.setattr(queue, "BACKOFF", ())
    # Restore a bounded sequence for the terminal transition without waiting.
    monkeypatch.setattr(queue, "BACKOFF", (0,))
    queue.update(tmp_path, claimed, error="still unavailable")
    assert queue.jobs(tmp_path)[0]["phase"] == "blocked"
    queue.enqueue(tmp_path, source, permissions)
    assert queue.jobs(tmp_path)[0]["phase"] == "blocked"
    queue.resume(tmp_path, "poke")
    assert queue.jobs(tmp_path)[0]["phase"] == "pending"


def test_collection_receipt_can_be_reused_after_delivery_failure(tmp_path, monkeypatch):
    source, permissions = admitted(tmp_path)
    job = queue.enqueue(tmp_path, source, permissions, final=True)
    claim = queue.claim(tmp_path, job["id"])
    queue.update(tmp_path, claim, snapshot="trusted-hash", error="network unavailable")
    monkeypatch.setattr(queue.time, "time", lambda: time.time_ns() / 1e9 + 60)
    retried = queue.claim(tmp_path, job["id"])
    assert retried["phase"] == "collected" and retried["snapshot"] == "trusted-hash"


def test_revocation_prevents_retry_and_same_source_readmission_is_new_job(tmp_path):
    source, old = admitted(tmp_path)
    job = queue.enqueue(tmp_path, source, old, final=True)
    with grants.effects(tmp_path):
        grants.revoke(tmp_path, ["poke"])
    with pytest.raises(grants.Revoked):
        queue.enqueue(tmp_path, source, old, final=True)
    queue.resume(tmp_path, "poke")
    assert queue.jobs(tmp_path)[0]["phase"] == "cancelled"
    fresh = queue.enqueue(tmp_path, source, {"poke": grants.ensure(tmp_path, "poke")})
    assert fresh["id"] != job["id"]


def test_failed_atomic_commit_keeps_previous_revision(tmp_path, monkeypatch):
    source, permissions = admitted(tmp_path)
    first = queue.enqueue(tmp_path, source, permissions)
    with patch.object(store.os, "rename", side_effect=OSError("fixture disk failure")):
        with pytest.raises(OSError):
            queue.enqueue(tmp_path, source, permissions, final=True)
    assert queue.jobs(tmp_path) == [first]
    assert not list((tmp_path / "accounting" / "jobs").glob(".pending-*"))


@pytest.mark.parametrize("mode", ["symlink", "hardlink", "public", "fifo", "oversized", "malformed"])
def test_unsafe_job_does_not_block_healthy_sources(tmp_path, mode):
    source, permissions = admitted(tmp_path)
    first = queue.enqueue(tmp_path, source, permissions)
    path = tmp_path / "accounting" / "jobs" / (first["id"] + ".json")
    data = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(data)
    outside.chmod(0o600)
    if mode == "symlink":
        path.symlink_to(outside)
    elif mode == "hardlink":
        os.link(outside, path)
    elif mode == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(data if mode == "public" else b"x" * 33000 if mode == "oversized" else b'{"phase":[]}')
        path.chmod(0o644 if mode == "public" else 0o600)
    other = queue.enqueue(tmp_path, {**source, "tag": "other"}, permissions)
    jobs = {j["id"]: j for j in queue.jobs(tmp_path)}
    assert jobs[first["id"]]["phase"] == "blocked"
    assert jobs[other["id"]]["phase"] == "pending"
    assert outside.read_bytes() == data


def test_final_handoff_never_waits_for_the_backend_and_preserves_exit(tmp_path):
    source, permissions = admitted(tmp_path)
    initial = threading.Event()
    with patch.object(worker, "wake", side_effect=lambda *a: initial.set()):
        producer = lifecycle.Producer(tmp_path, "/unused/docker", ROOT, source, permissions, 300)
        coordinator = LifecycleCoordinator()
        coordinator.register("producer", producer.stop, quiesce=producer.request_stop)
        assert initial.wait(2)
        # A backend lock can be held for minutes without touching this path.
        with store.root(tmp_path) as fd, store.lock(fd, "worker.lock"), store.lock(fd, "effects.lock"):
            assert coordinator.cleanup(37) == 37
        producer._thread.join(1)
        job = queue.jobs(tmp_path)[0]
        assert job["final"] == job["requested"] == 2 and job["delivered"] == 0
        assert producer.stop() == 0 and queue.jobs(tmp_path)[0] == job


def test_final_commit_failure_warns_without_masking_primary_status(tmp_path, capsys):
    source, permissions = admitted(tmp_path)
    with patch.object(threading.Thread, "start"):
        producer = lifecycle.Producer(tmp_path, "/unused/docker", ROOT, source, permissions, 300)
    with patch.object(queue, "enqueue", side_effect=OSError("private failure")):
        assert producer.stop() == 0
    assert "handoff needs attention" in capsys.readouterr().err


def test_busy_admission_retries_once_without_double_increment(tmp_path):
    source, permissions = admitted(tmp_path)
    real_enqueue = queue.enqueue
    count = 0

    def enqueue(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise store.Busy("fixture contention")
        return real_enqueue(*args, **kwargs)

    with patch.object(threading.Thread, "start"):
        producer = lifecycle.Producer(tmp_path, "/unused", ROOT, source, permissions, 300)
    with patch.object(queue, "enqueue", side_effect=enqueue), patch.object(worker, "wake"):
        assert producer.stop() == 0
    assert count == 2
    assert queue.jobs(tmp_path)[0]["final"] == queue.jobs(tmp_path)[0]["requested"] == 1


def test_wake_failure_preserves_final_and_reports_saved_state(tmp_path, capsys):
    source, permissions = admitted(tmp_path)
    with patch.object(threading.Thread, "start"):
        producer = lifecycle.Producer(tmp_path, "/unused", ROOT, source, permissions, 300)
    with patch.object(worker, "wake", side_effect=store.Busy("fixture contention")) as wake:
        assert producer.stop() == 0
    assert wake.call_count == 2
    assert "request saved; worker wake needs retry (Busy)" in capsys.readouterr().err
    assert queue.jobs(tmp_path)[0]["final"] == queue.jobs(tmp_path)[0]["requested"] == 1


def test_worker_is_singleton_and_consumes_new_final_after_old_receipt(tmp_path):
    source, permissions = admitted(tmp_path)
    first = queue.enqueue(tmp_path, source, permissions)
    started, release = threading.Event(), threading.Event()
    revisions = []

    def execute(root, _docker, _install, job):
        revisions.append(job["claimed"])
        if len(revisions) == 1:
            started.set()
            assert release.wait(3)
        queue.update(root, job, receipt="done")

    owner = threading.Thread(target=lambda: worker.drain(tmp_path, "/unused", ROOT, execute=execute))
    owner.start()
    try:
        assert started.wait(2)
        assert worker.status(tmp_path)["running"]
        assert worker.drain(tmp_path, "/unused", ROOT, execute=execute) == 0
        queue.enqueue(tmp_path, source, permissions, final=True)
    finally:
        release.set()
        owner.join(4)
    assert not owner.is_alive()
    assert revisions == [1, 2]
    assert queue.jobs(tmp_path)[0]["phase"] == "delivered"
    assert not worker.status(tmp_path)["running"]


def test_owner_crash_releases_kernel_lease_and_revision_is_reclaimed(tmp_path):
    source, permissions = admitted(tmp_path)
    queue.enqueue(tmp_path, source, permissions, final=True)
    code = "from pathlib import Path; import os,sys; from cage_core.accounting.worker import drain; drain(Path(sys.argv[1]), '/unused', Path.cwd(), execute=lambda *a: os._exit(19))"
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT, timeout=10)
    assert result.returncode == 19
    assert queue.jobs(tmp_path)[0]["phase"] == "collecting"
    assert not worker.status(tmp_path)["running"]
    assert worker.drain(tmp_path, "/unused", ROOT, execute=lambda r, d, p, j: queue.update(r, j, receipt="recovered")) == 0
    assert queue.jobs(tmp_path)[0]["receipt"] == "recovered"


def test_wake_during_idle_release_cannot_be_lost(tmp_path, monkeypatch):
    source, permissions = admitted(tmp_path)
    in_exit, release = threading.Event(), threading.Event()
    actual = worker._pending
    calls = 0

    def pending(root):
        nonlocal calls
        result = actual(root)
        if threading.current_thread().name == "old-owner":
            calls += 1
            if calls == 2:
                in_exit.set()
                assert release.wait(3)
        return result

    monkeypatch.setattr(worker, "_pending", pending)
    execute = lambda r, d, p, j: queue.update(r, j, receipt="delivered-after-exit-race")
    old = threading.Thread(name="old-owner", target=lambda: worker.drain(tmp_path, "/unused", ROOT, execute=execute))
    new = threading.Thread(target=lambda: worker.drain(tmp_path, "/unused", ROOT, execute=execute))
    old.start()
    try:
        assert in_exit.wait(2)
        queue.enqueue(tmp_path, source, permissions, final=True)
        new.start()
    finally:
        release.set()
        old.join(4)
        if new.ident:
            new.join(4)
    assert not old.is_alive() and not new.is_alive()
    assert queue.jobs(tmp_path)[0]["phase"] == "delivered"


def test_final_priority_has_a_fairness_bound(tmp_path):
    source, permissions = admitted(tmp_path)
    ordinary = queue.enqueue(tmp_path, source, permissions)
    final = queue.enqueue(tmp_path, {**source, "tag": "second"}, permissions, final=True)
    assert worker.select([ordinary, final], 0)["id"] == final["id"]
    assert worker.select([ordinary, final], 4)["id"] == ordinary["id"]


def test_runtime_snapshot_survives_install_replacement(tmp_path):
    install = tmp_path / "install"
    (install / "cage_core").mkdir(parents=True)
    (install / "cage-main.py").write_text("# version one\n")
    (install / "cage_core" / "__init__.py").write_text("# helper one\n")
    pinned = runtime.snapshot(tmp_path, install)
    (install / "cage_core" / "__init__.py").write_text("# helper two\n")
    newer = runtime.snapshot(tmp_path, install)
    assert pinned != newer
    assert (pinned / "cage_core" / "__init__.py").read_text() == "# helper one\n"
    assert runtime.snapshot(tmp_path, install) == newer
    (newer / "cage_core" / "__init__.py").write_text("# tampered\n")
    with pytest.raises(store.AccountingError, match="changed"):
        runtime.snapshot(tmp_path, install)


def test_wake_detaches_terminal_and_does_not_wait(tmp_path):
    with patch.object(worker.subprocess, "Popen") as popen:
        worker.wake(tmp_path, "/trusted/docker", tmp_path / "trusted-runtime")
    args, kwargs = popen.call_args
    assert args[0][1:3] == ["-I", str(tmp_path / "trusted-runtime" / "cage-main.py")]
    assert kwargs["close_fds"] and kwargs["start_new_session"] and kwargs["cwd"] == "/"
    assert all(kwargs[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    popen.return_value.wait.assert_not_called()
