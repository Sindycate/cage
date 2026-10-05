"""Revisioned jobs with independent collection and delivery receipts."""

from __future__ import annotations

import math
import os
import secrets
import time
from pathlib import Path

from . import grants, store


MAX_JOBS = 4096
BACKOFF = (5, 30, 120, 600, 1800)
FIELDS = {"version", "id", "source", "grants", "requested", "claimed", "final", "collected", "delivered", "attempt", "phase", "attempts", "next_due", "created", "updated", "last_success", "error", "snapshot", "receipt"}
PHASES = {"pending", "collecting", "collected", "delivered", "retry_wait", "blocked", "cancelled"}


def _validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS or value["version"] != 1:
        raise store.AccountingError("invalid accounting job schema")
    if not isinstance(value["phase"], str) or value["phase"] not in PHASES or not isinstance(value["id"], str) or len(value["id"]) != 64:
        raise store.AccountingError("invalid accounting job identity")
    for key in ("requested", "claimed", "final", "collected", "delivered", "attempts"):
        if type(value[key]) is not int or not 0 <= value[key] <= 2**53:
            raise store.AccountingError("invalid accounting job revision")
    if not (value["delivered"] <= value["collected"] <= value["claimed"] <= value["requested"] and value["final"] <= value["requested"]):
        raise store.AccountingError("inconsistent accounting job revisions")
    for key in ("next_due", "created", "updated", "last_success"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise store.AccountingError("invalid accounting job clock")
    for key in ("attempt", "error", "snapshot", "receipt"):
        if not isinstance(value[key], str) or len(value[key]) > 512:
            raise store.AccountingError("invalid accounting job result")
    if (not isinstance(value["grants"], dict) or not 1 <= len(value["grants"]) <= 3
            or any(not isinstance(k, str) or len(k) > 288 or not isinstance(v, str) or len(v) != 32 for k, v in value["grants"].items())):
        raise store.AccountingError("invalid accounting job permissions")
    # Source validation happens at the backend boundary, before any effect.
    if not isinstance(value["source"], dict) or len(str(value["source"])) > 16384:
        raise store.AccountingError("invalid accounting source")
    if value["id"] != grants.digest({"source": value["source"], "grants": value["grants"]}):
        raise store.AccountingError("accounting job identity mismatch")
    return value


def _load(fd: int, name: str) -> dict | None:
    value = store.read(fd, name, limit=32768)
    return _validate(value) if value is not None else None


def enqueue(config_root: Path, source: dict, permissions: dict[str, str], *, final: bool = False, resume: bool = False) -> dict:
    grants.require(config_root, permissions)
    identity = grants.digest({"source": source, "grants": permissions})
    now = time.time()
    with store.root(config_root) as fd, store.lock(fd, "queue.lock"), store.directory(fd, "jobs") as jobs:
        job = _load(jobs, identity + ".json")
        if job is None:
            if len(os.listdir(jobs)) >= MAX_JOBS:
                raise store.AccountingError("accounting queue is full; pending jobs were preserved")
            job = dict(version=1, id=identity, source=source, grants=permissions,
                       requested=0, claimed=0, final=0, collected=0, delivered=0,
                       attempt="", phase="pending", attempts=0, next_due=0,
                       created=now, updated=now, last_success=0, error="", snapshot="", receipt="")
        if job["phase"] == "cancelled":
            raise store.AccountingError("accounting job was cancelled")
        job["requested"] += 1
        if final:
            job["final"] = job["requested"]
        if job["phase"] == "delivered" or resume:
            job.update(phase="pending", attempts=0, next_due=0, error="")
        job["updated"] = now
        store.write(jobs, identity + ".json", _validate(job))
        return dict(job)


def jobs(config_root: Path) -> list[dict]:
    try:
        with store.root(config_root, create=False) as fd, store.directory(fd, "jobs", create=False) as directory:
            names = sorted(os.listdir(directory))
            if len(names) > MAX_JOBS:
                raise store.AccountingError("accounting queue exceeds its limit")
            result = []
            for name in names:
                if name.startswith(".pending-"):
                    continue
                if not name.endswith(".json") or len(name) != 69:
                    raise store.AccountingError("unexpected accounting queue entry")
                try:
                    value = _load(directory, name)
                    if value is not None:
                        result.append(value)
                except (store.AccountingError, OSError):
                    # A poison job must be visible without blocking other sources.
                    result.append({"id": name[:-5], "phase": "blocked", "error": "invalid or unsafe accounting job", "source": {}})
            return result
    except FileNotFoundError:
        return []


def claim(config_root: Path, identity: str) -> dict | None:
    with store.root(config_root) as fd, store.lock(fd, "queue.lock"), store.directory(fd, "jobs") as directory:
        job = _load(directory, identity + ".json")
        if job is None or job["phase"] in {"blocked", "cancelled"} or job["requested"] <= job["delivered"] or job["next_due"] > time.time():
            return None
        # A singleton worker owns calls to claim. Previous process attempts are
        # recovered before backend access; there is no timestamp-based stealing.
        reuse = job["collected"] == job["requested"] and bool(job["snapshot"])
        job.update(claimed=job["requested"], attempt=secrets.token_hex(16),
                   phase="collected" if reuse else "collecting", updated=time.time())
        store.write(directory, identity + ".json", _validate(job))
        return dict(job)


def update(config_root: Path, claimed: dict, *, snapshot: str | None = None, receipt: str | None = None, error: str | None = None, cancelled: bool = False, blocked: bool = False) -> None:
    with store.root(config_root) as fd, store.lock(fd, "queue.lock"), store.directory(fd, "jobs") as directory:
        job = _load(directory, claimed["id"] + ".json")
        if job is None or job["attempt"] != claimed["attempt"] or job["phase"] == "cancelled":
            return
        seq = claimed["claimed"]
        if snapshot is not None:
            job.update(collected=seq, snapshot=snapshot, phase="collected")
        if receipt is not None:
            job.update(collected=seq, delivered=seq, receipt=receipt, attempts=0,
                       error="", next_due=0, last_success=time.time(),
                       phase="delivered" if job["requested"] == seq else "pending")
        if error is not None:
            count = job["attempts"]
            job.update(error=error[:512], attempts=count + 1,
                       phase="retry_wait" if count < len(BACKOFF) else "blocked",
                       next_due=time.time() + BACKOFF[min(count, len(BACKOFF)-1)] * (0.9 + secrets.randbelow(21) / 100))
        if cancelled:
            job.update(phase="cancelled", error="permission or source changed")
        elif blocked:
            job.update(phase="blocked", next_due=0)
        job["updated"] = time.time()
        store.write(directory, job["id"] + ".json", _validate(job))


def status(config_root: Path, backend: str) -> list[dict]:
    return [{key: j.get(key) for key in ("id", "phase", "requested", "collected", "delivered", "created", "updated", "last_success", "next_due", "error")}
            for j in jobs(config_root) if j["source"].get("backend", backend) == backend]


def resume(config_root: Path, backend: str) -> None:
    """Explicit user retry, without giving old work a new permission epoch."""
    for job in jobs(config_root):
        if job["source"].get("backend") != backend or job["phase"] in {"delivered", "cancelled"}:
            continue
        try:
            enqueue(config_root, job["source"], job["grants"], resume=True)
        except grants.Revoked:
            cancel(config_root, job["id"])


def cancel(config_root: Path, identity: str) -> None:
    with store.root(config_root) as fd, store.lock(fd, "queue.lock"), store.directory(fd, "jobs") as directory:
        job = _load(directory, identity + ".json")
        if job is not None and job["phase"] != "delivered":
            job.update(phase="cancelled", error="permission or source changed", updated=time.time())
            store.write(directory, identity + ".json", job)
