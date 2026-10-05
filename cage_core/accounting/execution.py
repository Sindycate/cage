"""Attempt ownership, also honored by synchronous collectors after an upgrade."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import grants, runtime, store


@dataclass(frozen=True)
class Work:
    config_root: Path
    job_id: str
    guard: Callable
    engine: str


_work: ContextVar[Work | None] = ContextVar("accounting_work", default=None)


def active() -> bool:
    return _work.get() is not None


@contextmanager
def scope(work: Work):
    token = _work.set(work)
    try:
        yield
    finally:
        _work.reset(token)


@contextmanager
def commit(config_root: Path):
    work = _work.get()
    if work is None:
        yield
    else:
        if config_root.resolve() != work.config_root.resolve():
            raise store.AccountingError("accounting execution root changed")
        with work.guard():
            yield


def _inspect(docker: str, name: str) -> dict | None:
    result = subprocess.run([docker, "container", "inspect", name], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    if result.returncode:
        # Distinguish absence from an unavailable daemon. Never assume a failed
        # inspection proves that a collector stopped writing shared state.
        if "No such container" in result.stderr or "No such object" in result.stderr:
            return None
        raise store.AccountingError("cannot reconcile an accounting collector")
    try:
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError
        return values[0]
    except ValueError as exc:
        raise store.AccountingError("invalid accounting collector inspection") from exc


def _finish(docker: str, attempt: dict) -> bool:
    observed = _inspect(docker, attempt["name"])
    if observed is None:
        return False
    labels = observed.get("Config", {}).get("Labels") or {}
    host = observed.get("HostConfig", {})
    # The unguessable attempt label, exact name, immutable image and complete
    # requested mounts bind cleanup to our child, including crash-before-cid.
    if (observed.get("Name") != "/" + attempt["name"]
            or labels.get("io.cage.accounting.attempt") != attempt["nonce"]
            or labels.get("io.cage.accounting.job") != attempt["job"]
            or observed.get("Image") != attempt["image"]
            or host.get("NetworkMode") != "none" or not host.get("ReadonlyRootfs")
            or grants.digest(_normalized_mounts(host.get("Mounts", []))) != attempt["mount_digest"]
            or (attempt.get("container_id") and observed.get("Id") != attempt["container_id"])):
        raise store.AccountingError("accounting collector identity changed; cleanup refused")
    identity = observed.get("Id", "")
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise store.AccountingError("invalid accounting collector ID")
    subprocess.run([docker, "rm", "-f", identity], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
    if _inspect(docker, attempt["name"]) is not None:
        raise store.AccountingError("accounting collector cleanup is still pending")
    return True


def _mounts(command: list[str]) -> list[dict]:
    """Docker's inspect HostConfig.Mounts representation of our bounded mounts."""
    result = []
    for i, item in enumerate(command):
        if item != "--mount":
            continue
        values = dict(part.split("=", 1) if "=" in part else (part, True) for part in command[i + 1].split(","))
        mount = {"Type": values["type"], "Source": values["src"], "Target": values["dst"]}
        if values.get("readonly"):
            mount["ReadOnly"] = True
        if values["type"] == "volume":
            options = {}
            if values.get("volume-nocopy"):
                options["NoCopy"] = True
            if "volume-subpath" in values:
                options["Subpath"] = values["volume-subpath"]
            if options:
                mount["VolumeOptions"] = options
        result.append(mount)
    return _normalized_mounts(result)


def _normalized_mounts(mounts: list[dict]) -> list[dict]:
    # Docker adds default fields to its request representation. Compare all
    # identity/permission fields, independently of ordering and omitted false.
    return sorted([
        {"type": m.get("Type"), "source": m.get("Source"), "target": m.get("Target"),
         "readonly": bool(m.get("ReadOnly", False)),
         "nocopy": bool((m.get("VolumeOptions") or {}).get("NoCopy", False)),
         "subpath": (m.get("VolumeOptions") or {}).get("Subpath", "")}
        for m in mounts
    ], key=lambda m: str(m["target"]))


def _validate_attempt(value: object) -> dict:
    required = {"version", "source", "name", "nonce", "job", "image", "mount_digest", "engine"}
    if (not isinstance(value, dict) or set(value) - {"container_id"} != required
            or value.get("version") != 1):
        raise store.AccountingError("invalid accounting collector journal")
    for key, length in (("nonce", 32), ("job", 64), ("mount_digest", 64), ("container_id", 64)):
        text = value.get(key, "0" * length)
        if not isinstance(text, str) or len(text) != length or any(c not in "0123456789abcdef" for c in text):
            raise store.AccountingError("invalid accounting collector identity")
    if (value["name"] != "cage-accounting-" + value["nonce"]
            or not isinstance(value["source"], str) or len(value["source"]) > 288
            or not isinstance(value["engine"], str) or not 8 <= len(value["engine"]) <= 128
            or not isinstance(value["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"])):
        raise store.AccountingError("invalid accounting collector identity")
    return value


def recover(config_root: Path, docker: str | None, source: str) -> None:
    """Caller holds its backend/source lock, before touching collector cache."""
    try:
        with store.root(config_root, create=False) as fd, store.directory(fd, "attempts", create=False) as attempts:
            names = os.listdir(attempts)
            if len(names) > 8192:
                raise store.AccountingError("too many unresolved accounting collectors")
            for name in names:
                if not name.endswith(".json") or name.startswith(".pending-"):
                    continue
                value = _validate_attempt(store.read(attempts, name, limit=32768))
                if value.get("source") == source:
                    if docker is None:
                        from ..storage import docker_command
                        docker = docker_command()
                    if runtime._output(docker, ["info", "--format", "{{.ID}}"] ) != value["engine"]:
                        raise store.AccountingError("collector recovery requires its original Docker engine")
                    removed = _finish(docker, value)
                    if removed or value.get("container_id"):
                        store.remove(attempts, name)
                    # An ambiguous create may still arrive late. Preserve its
                    # name/nonce until observed; no start was ever issued, so
                    # an absent or late stopped child cannot mutate the cache.
    except FileNotFoundError:
        return


@contextmanager
def collector(config_root: Path, docker: str, source: str, command: list[str], image: str):
    """Create/journal before start, then attach without changing scan semantics."""
    work = _work.get()
    if work is None:
        yield command
        return
    immutable = runtime.image_id(docker, image)
    nonce = secrets.token_hex(16)
    name = "cage-accounting-" + nonce
    original = list(command)
    # Replace a pre-existing Poke name and pin the actual execution image.
    if "--name" in original:
        pos = original.index("--name")
        del original[pos:pos + 2]
    pos = original.index(image)
    original[pos] = immutable
    # docker create makes the real ID observable before start; the journal
    # already owns name+nonce if create itself is interrupted.
    create = [docker, "create", "--name", name, "--label", "io.cage.accounting.attempt=" + nonce,
              "--label", "io.cage.accounting.job=" + work.job_id, *original[2:]]
    # --rm is valid with create. A stopped child may disappear before inspect.
    attempt = dict(version=1, source=source, name=name, nonce=nonce, job=work.job_id,
                   image=immutable, mount_digest=grants.digest(_mounts(create)), engine=work.engine)
    with store.root(config_root) as fd, store.directory(fd, "attempts") as attempts:
        store.write(attempts, nonce + ".json", attempt)
    created = False
    try:
        made = subprocess.run(create, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=30, check=True)
        identity = made.stdout.strip()
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise store.AccountingError("Docker did not return an accounting collector ID")
        attempt["container_id"] = identity
        created = True
        with store.root(config_root) as fd, store.directory(fd, "attempts") as attempts:
            store.write(attempts, nonce + ".json", attempt)
        yield [docker, "start", "--attach", *( ["--interactive"] if "-i" in original else []), identity]
    finally:
        # A failed cleanup retains the journal and blocks reuse of its cache.
        if created:
            _finish(docker, attempt)
            with store.root(config_root) as fd, store.directory(fd, "attempts") as attempts:
                store.remove(attempts, nonce + ".json")
        # An uncertain create can finish late. Keep its journal for the next
        # recovery; because start was never issued it cannot write the cache.
