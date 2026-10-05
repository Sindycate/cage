"""Narrow durable inputs for the two existing accounting implementations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import re

from . import execution, grants, queue, runtime, store
from ..models import StoragePolicy
from ..monitoring import connection, registry, service, validation, volumes
from ..monitoring.errors import MonitorError
from .. import poketoken


def validate(source: dict, permissions: dict) -> None:
    common = {"backend", "volume", "fingerprint", "transport", "version"}
    backend = source.get("backend")
    extra = {"logical_id", "identity", "storage_policy"} if backend == "monitor" else {"image"}
    if backend not in {"monitor", "poke"} or set(source) != common | extra:
        raise store.AccountingError("invalid accounting source schema")
    validation.validate_volume_name(source["volume"])
    fingerprint = validation.validate_fingerprint(source["fingerprint"])
    if fingerprint["name"] != source["volume"]:
        raise store.AccountingError("accounting volume identity mismatch")
    runtime.validate_transport(source["transport"])
    if not isinstance(source["version"], str) or not re.fullmatch(r"(?:[0-9]+\.[0-9]+\.[0-9]+|dev)", source["version"]):
        raise store.AccountingError("invalid accounting runtime version")
    if backend == "monitor":
        validation.validate_logical_id(source["logical_id"])
        if not isinstance(source["identity"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["identity"]):
            raise store.AccountingError("invalid accounting source identity")
        if not isinstance(source["storage_policy"], dict) or set(source["storage_policy"]) != set(StoragePolicy().public_dict()):
            raise store.AccountingError("invalid accounting storage policy")
        StoragePolicy(**source["storage_policy"])
        expected = {"connection", "monitor:" + source["logical_id"]}
    else:
        if not isinstance(source["image"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", source["image"]):
            raise store.AccountingError("invalid accounting export image")
        expected = {"poke", "poke:" + source["volume"]}
    if set(permissions) != expected:
        raise store.AccountingError("accounting source permission mismatch")


def _identity(record) -> str:
    return grants.digest(grants.source_identity(asdict(record)))


def monitor_source(root: Path, record, transport: dict, version: str, policy: StoragePolicy) -> tuple[dict, dict]:
    # Admission never waits behind a network publication. This optimistic read
    # is revalidated under the effect fence before any result can be committed.
    current = next((r for r in registry.load_registry(root) if r.logical_id == record.logical_id), None)
    conn = connection.load_connection(root)
    if current is None or current.status != "active" or current.target != "container" or _identity(current) != _identity(record):
        raise grants.Revoked("Token Monitor source is no longer active")
    if conn is None or not conn.enabled:
        raise grants.Revoked("Token Monitor is disconnected")
    permissions = {key: grants.ensure(root, key) for key in ("connection", "monitor:" + record.logical_id)}
    source = dict(backend="monitor", volume=record.volume_name, fingerprint=record.fingerprint,
                  transport=transport, version=version, logical_id=record.logical_id,
                  identity=_identity(record), storage_policy=policy.public_dict())
    validate(source, permissions)
    return source, permissions


def poke_source(root: Path, docker: str, plan, transport: dict) -> tuple[dict, dict]:
    poketoken._require_plan(plan)
    source = dict(backend="poke", volume=plan.volume_name,
                  fingerprint=volumes.volume_fingerprint(docker, plan.volume_name),
                  image=runtime.image_id(docker, plan.image), transport=transport, version=plan.cage_version)
    permissions = {key: grants.ensure(root, key) for key in ("poke", "poke:" + plan.volume_name)}
    validate(source, permissions)
    return source, permissions


def execute(root: Path, docker: str, install_root: Path, job: dict) -> str:
    source, permissions = job["source"], job["grants"]
    try:
        validate(source, permissions)
    except (MonitorError, ValueError, TypeError, KeyError) as exc:
        raise store.Invalid("invalid accounting source; inspect the private queue") from exc
    grants.require(root, permissions)
    # Capture every aggregate peer's authorization. Changes invalidate the
    # aggregate instead of allowing an old repair to resurrect a removed peer.
    records = registry.load_registry(root) if source["backend"] == "monitor" else []
    active = {r.logical_id: _identity(r) for r in records if r.status == "active"}
    peer_epochs = {"monitor:" + key: grants.ensure(root, "monitor:" + key) for key in active}

    @contextmanager
    def guard():
        with grants.effects(root):
            grants.require(root, permissions)
            if source["backend"] == "monitor":
                try:
                    grants.require(root, peer_epochs)
                except grants.Revoked as exc:
                    raise store.AccountingError("accounting peer authorization changed") from exc
                observed = {r.logical_id: _identity(r) for r in registry.load_registry(root) if r.status == "active"}
                if observed != active or observed.get(source["logical_id"]) != source["identity"]:
                    raise store.AccountingError("accounting registry changed during collection")
            yield

    with runtime.endpoint(docker, source["transport"]), execution.scope(
        execution.Work(root, job["id"], guard, source["transport"]["engine"])
    ):
        with guard():
            pass
        if volumes.volume_fingerprint(docker, source["volume"]) != source["fingerprint"]:
            raise grants.Revoked("queued accounting volume was replaced")
        collected = lambda value: queue.update(root, job, snapshot=value)
        if source["backend"] == "poke":
            poketoken.sync_source(
                root, docker, install_root,
                poketoken.ExportSource(source["volume"], source["image"], source["fingerprint"]),
                on_collected=collected,
            )
            receipt = "export:" + job["attempt"]
        else:
            record = next(r for r in records if r.logical_id == source["logical_id"])
            result = service.scan_registration_outcome(
                root, docker, install_root, record, version=source["version"],
                storage_policy=StoragePolicy(**source["storage_policy"]), allow_build=False,
                final=job["final"] > job["delivered"],
                work=service.ScanWork(collected, job["snapshot"] if job["collected"] == job["claimed"] else ""),
            )
            if not result.delivered:
                raise store.AccountingError("accounting delivery pending: " + result.reason)
            generation = result.status.get("last_good_generation")
            if not isinstance(generation, str) or not generation:
                raise store.AccountingError("accounting delivery has no publication receipt")
            receipt = "generation:" + generation
        with guard():
            # Completion and effects use the same revocation fence. Only this
            # claimed revision is acknowledged; a newer final remains pending.
            queue.update(root, job, receipt=receipt)
        return receipt
