"""Revocation generations shared by admission, source mutations and effects.

Lock order: registry transaction (when needed), effects, grants. Publication
never takes a registry transaction while holding effects. Receipt commits may
take effects then the short queue lock; no queue operation takes an effect,
registry or collector lock while holding the queue lock.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import store

_held: ContextVar[tuple[str, ...]] = ContextVar("accounting_effects", default=())


class Revoked(store.AccountingError):
    """An old admission cannot be retried with new authority."""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _load(fd: int) -> dict:
    value = store.read(fd, "grants.json")
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) != {"version", "epochs"} or value["version"] != 1:
        raise store.AccountingError("invalid accounting permissions")
    epochs = value["epochs"]
    if not isinstance(epochs, dict) or len(epochs) > 16384:
        raise store.AccountingError("accounting permission limit exceeded")
    for key, epoch in epochs.items():
        if (not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key)
                or not isinstance(epoch, str) or len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch)):
            raise store.AccountingError("invalid accounting permission generation")
    return epochs


def current(config_root: Path, key: str) -> str:
    try:
        with store.root(config_root, create=False) as fd:
            return _load(fd).get(digest(key), "")
    except FileNotFoundError:
        return ""


def ensure(config_root: Path, key: str) -> str:
    with store.root(config_root) as fd, store.lock(fd, "grants.lock"):
        epochs = _load(fd)
        name = digest(key)
        if name not in epochs:
            if len(epochs) >= 16384:
                raise store.AccountingError("accounting permission limit exceeded")
            epochs[name] = secrets.token_hex(16)
            store.write(fd, "grants.json", {"version": 1, "epochs": epochs})
        return epochs[name]


def revoke(config_root: Path, keys: list[str]) -> None:
    """Caller holds effects when revocation accompanies an external mutation."""
    with store.root(config_root) as fd, store.lock(fd, "grants.lock", timeout=5):
        epochs = _load(fd)
        for key in keys:
            epochs[digest(key)] = secrets.token_hex(16)
        if len(epochs) > 16384:
            raise store.AccountingError("accounting permission limit exceeded")
        store.write(fd, "grants.json", {"version": 1, "epochs": epochs})


@contextmanager
def effects(config_root: Path):
    key = str(config_root.resolve())
    if key in _held.get():
        yield
    else:
        with store.root(config_root) as fd, store.lock(fd, "effects.lock", timeout=5):
            token = _held.set((*_held.get(), key))
            try:
                yield
            finally:
                _held.reset(token)


def require(config_root: Path, expected: dict[str, str]) -> None:
    for key, epoch in expected.items():
        if not epoch or current(config_root, key) != epoch:
            raise Revoked("accounting permission was revoked; a new authorized request is required")


def source_identity(record: dict) -> dict:
    return {key: record.get(key) for key in ("logical_id", "target", "volume_name", "repository", "fingerprint", "status")}


@contextmanager
def registry_change(config_root: Path, previous: list[dict], following: list[dict]):
    old = {r["logical_id"]: source_identity(r) for r in previous}
    new = {r["logical_id"]: source_identity(r) for r in following}
    changed = ["monitor:" + key for key in old.keys() | new.keys() if old.get(key) != new.get(key)]
    if not changed:
        yield
        return
    with effects(config_root):
        # Invalidate before the mutation. A failed write never revives old work.
        revoke(config_root, changed)
        yield
