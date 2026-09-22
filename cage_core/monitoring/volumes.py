"""Bounded Docker volume inspection and Cage ownership labels."""

from __future__ import annotations

import json
import subprocess

from . import errors as errors_api
from . import validation as validation_api


def _docker_json(docker: str, arguments: list[str], *, timeout: float = 15.0) -> object:
    try:
        result = subprocess.run(
            [docker, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise errors_api.MonitorError(f"Docker monitor operation failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().replace("\n", " ")[:300]
        raise errors_api.MonitorError(f"Docker monitor operation failed: {detail or 'unknown error'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise errors_api.MonitorError("Docker monitor operation returned invalid JSON") from exc


def volume_fingerprint(docker: str, volume_name: str) -> dict[str, str]:
    validation_api.validate_volume_name(volume_name)
    value = _docker_json(docker, ["volume", "inspect", volume_name])
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise errors_api.MonitorError("Docker returned an invalid monitor volume")
    item = value[0]
    labels = item.get("Labels") if isinstance(item.get("Labels"), dict) else {}
    created = item.get("CreatedAt")
    driver = item.get("Driver")
    scope = item.get("Scope", "")
    name = item.get("Name")
    if not all(isinstance(part, str) and part for part in (name, driver, scope, created)):
        raise errors_api.MonitorError("Docker volume lacks a stable fingerprint")
    label_identity = labels.get("io.cage.identity", "")
    if not isinstance(label_identity, str):
        label_identity = ""
    return validation_api.validate_fingerprint({
        "name": name,
        "driver": driver,
        "scope": scope,
        "created_at": created,
        "label_identity": label_identity,
    })


def ensure_codex_volume(
    docker: str,
    volume_name: str,
    *,
    logical_id: str,
) -> dict[str, str]:
    """Create a labeled Codex volume, preserving existing volume contents."""

    ensure_codex_volume_labels(docker, volume_name, logical_id=logical_id)
    return volume_fingerprint(docker, volume_name)


def ensure_codex_volume_labels(
    docker: str,
    volume_name: str,
    *,
    logical_id: str,
) -> None:
    """Ensure a newly-created Codex volume carries Cage ownership labels."""

    validation_api.validate_volume_name(volume_name)
    validation_api.validate_logical_id(logical_id)

    try:
        result = subprocess.run(
            [
                docker,
                "volume",
                "create",
                "--label",
                "io.cage.managed=true",
                "--label",
                "io.cage.resource=codex-state",
                "--label",
                f"io.cage.identity={logical_id}",
                volume_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise errors_api.MonitorError(f"cannot create Codex state volume: {exc}") from exc
    if result.returncode != 0:
        raise errors_api.MonitorError(f"cannot create Codex state volume: {result.stderr.strip()[:300]}")
