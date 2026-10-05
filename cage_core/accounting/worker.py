"""On-demand process owner. No terminal, service installation or PID killing."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

from . import grants, queue, store


def wake(root: Path, docker: str, pinned_runtime: Path) -> None:
    with store.root(root) as fd, store.lock(fd, "wake.lock"):
        try:
            with store.lock(fd, "worker.lock", timeout=0):
                pass
        except store.Busy:
            # The idle owner also takes wake.lock before its last queue read.
            # Therefore this short-circuit cannot lose a committed request.
            return
        _spawn(root, docker, pinned_runtime)


def _spawn(root: Path, docker: str, pinned_runtime: Path) -> None:
    # The executable comes from the trusted launcher snapshot, never a job.
    environment = {key: value for key, value in os.environ.items() if key in {
        "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }}
    subprocess.Popen(
        [sys.executable, "-I", str(pinned_runtime / "cage-main.py"), "_accounting-drain", str(root), docker],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, cwd="/", env=environment,
    )


def _pending(root: Path) -> list[dict]:
    pending = []
    for job in queue.jobs(root):
        if job["phase"] in {"delivered", "cancelled", "blocked"}:
            continue
        try:
            grants.require(root, job["grants"])
        except grants.Revoked:
            queue.cancel(root, job["id"])
            continue
        pending.append(job)
    return pending


def select(jobs: list[dict], final_streak: int) -> dict | None:
    ready = [j for j in jobs if j["next_due"] <= time.time()]
    if not ready:
        return None
    ordinary = [j for j in ready if j["final"] <= j["delivered"]]
    if final_streak >= 4 and ordinary:
        return min(ordinary, key=lambda j: j["updated"])
    return min(ready, key=lambda j: (j["final"] <= j["delivered"], j["updated"]))


def drain(root: Path, docker: str, install_root: Path, *, execute=None) -> int:
    if execute is None:
        from .backends import execute
    lease = None
    root = root.resolve()
    with store.root(root) as fd:
        # Every wake candidate and idle exit use this short handshake. A job
        # committed during idle exit either keeps this owner alive or elects
        # the candidate after the lifetime lease is released.
        with store.lock(fd, "wake.lock", timeout=5):
            candidate = store.lock(fd, "worker.lock", timeout=0)
            try:
                candidate.__enter__()
            except store.Busy:
                return 0
            lease = candidate
        try:
            streak = 0
            store.write(fd, "worker.json", {"pid": os.getpid(), "started": time.time(), "state": "running", "error": ""})
            while True:
                pending = _pending(root)
                if not pending:
                    with store.lock(fd, "wake.lock", timeout=5):
                        if _pending(root):
                            continue
                        store.write(fd, "worker.json", {"pid": os.getpid(), "finished": time.time(), "state": "idle", "error": ""})
                        lease.__exit__(None, None, None)
                        lease = None
                        return 0
                selected = select(pending, streak)
                if selected is None:
                    # New final work must be noticed during another source's
                    # backoff. This bounded worker exits once work is terminal.
                    time.sleep(min(1, max(0.01, min(j["next_due"] for j in pending) - time.time())))
                    continue
                job = queue.claim(root, selected["id"])
                if job is None:
                    continue
                streak = streak + 1 if job["final"] > job["delivered"] else 0
                try:
                    execute(root, docker, install_root, job)
                except grants.Revoked:
                    queue.update(root, job, cancelled=True)
                except store.Invalid as exc:
                    queue.update(root, job, error=str(exc), blocked=True)
                except Exception as exc:
                    # Backend error strings can include private paths or hub
                    # response data. Status deliberately contains fixed text.
                    try:
                        grants.require(root, job["grants"])
                    except grants.Revoked:
                        queue.update(root, job, cancelled=True)
                    else:
                        message = str(exc) if isinstance(exc, store.AccountingError) else "collection or delivery failed; retry with the matching sync command"
                        queue.update(root, job, error=message)
        except Exception:
            store.write(fd, "worker.json", {"pid": os.getpid(), "finished": time.time(), "state": "failed", "error": "worker stopped; retained jobs need a new wake"})
            return 1
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)


def status(root: Path) -> dict:
    try:
        with store.root(root, create=False) as fd:
            value = store.read(fd, "worker.json") or {"state": "not_started"}
            try:
                with store.lock(fd, "worker.lock", timeout=0):
                    running = False
            except store.Busy:
                running = True
            return {**value, "running": running}
    except FileNotFoundError:
        return {"state": "not_started", "running": False}
