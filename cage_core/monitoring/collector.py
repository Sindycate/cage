"""Execute the bounded, network-disabled collector and recover its evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any
from .. import storage

from . import accounting as accounting_api
from . import constants as constants_api
from . import errors as errors_api
from . import host_sources as host_sources_api
from . import models as models_api
from . import pricing as pricing_api
from . import snapshots as snapshots_api
from . import state as state_api
from . import validation as validation_api


def collector_image(version: str) -> str:
    return f"{constants_api.COLLECTOR_IMAGE}:{version}"


def collector_registry_image(version: str) -> str:
    return f"{constants_api.COLLECTOR_REGISTRY}:{version}"


def ensure_collector_image(
    docker: str,
    install_root: Path,
    *,
    version: str,
    storage_policy: object,
    allow_build: bool,
) -> str:
    image = collector_image(version)
    def run(arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [docker, *arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise errors_api.MonitorError(f"Docker monitor operation failed: {exc}") from exc

    inspect = run(["image", "inspect", image], timeout=30)
    if inspect.returncode == 0:
        return image
    registry = collector_registry_image(version)
    pull = run(["pull", registry], timeout=300)
    if pull.returncode == 0:
        tagged = run(["tag", registry, image], timeout=30)
        if tagged.returncode == 0:
            return image
    if not allow_build:
        raise errors_api.MonitorError(f"collector image {image} is unavailable")
    try:
        storage.preflight(docker, storage_policy, preferred_image=image, requires_build=True)
    except Exception as exc:
        raise errors_api.MonitorError(f"collector image build preflight failed: {exc}") from exc
    base = f"cage-base:{version}"
    base_inspect = run(["image", "inspect", base], timeout=30)
    if base_inspect.returncode != 0:
        result = run(
            [
                "build", "--build-arg", f"CAGE_VERSION={version}", "-t", base,
                "-f", str(install_root / "Dockerfile.base"), str(install_root),
            ],
            timeout=1800,
        )
        if result.returncode != 0:
            raise errors_api.MonitorError("shared base image build failed")
    result = run(
        [
            "build", "--build-arg", f"CAGE_BASE={base}", "--build-arg",
            f"CAGE_VERSION={version}", "-t", image, "-f",
            str(install_root / constants_api.COLLECTOR_DOCKERFILE), str(install_root),
        ],
        timeout=1800,
    )
    if result.returncode != 0:
        raise errors_api.MonitorError("Token Monitor collector image build failed")
    return image


def _subpath_available(docker: str, image: str, volume_name: str, subpath: str) -> bool:
    command = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--mount",
        # Docker otherwise copies an empty destination directory back into an
        # empty volume subpath, even for a read-only mount.  That copy-up can
        # reset Cage's Codex session directory ownership to root:root.
        f"type=volume,src={volume_name},dst=/probe,readonly,volume-subpath={subpath},volume-nocopy",
        "--entrypoint",
        "/usr/bin/true",
        image,
        "true",
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise errors_api.MonitorError(f"cannot probe Codex volume subpath: {exc}") from exc
    if result.returncode == 0:
        return True
    raw_message = result.stderr.strip().replace("\n", " ")
    lower = raw_message.lower()
    message = raw_message[:300]
    if "volume-subpath" in lower and any(
        marker in lower
        for marker in (
            "not supported",
            "unsupported",
            "unknown flag",
            "unknown option",
            "invalid option",
            "invalid field",
        )
    ):
        raise errors_api.MonitorError(
            "Docker does not support volume-subpath; refusing an unscoped Codex scan"
        )
    # Docker daemon versions differ here: some mention volume-subpath, while
    # others report only the host volume _data path from lstat. Both forms
    # mean that this exact optional directory is absent. Treat it as an empty
    # scan input; never fall back to an unscoped volume mount.
    if any(marker in lower for marker in ("does not exist", "no such file or directory")) and (
        "volume-subpath" in lower or "/_data/" in lower
    ):
        return False
    raise errors_api.MonitorError(f"Codex volume subpath probe failed: {message or 'unknown error'}")


def _host_session_mounts(
    config_root: Path,
    record: models_api.VolumeRegistration,
) -> list[str]:
    """Return exact read-only bind mounts for a managed host source.

    The collector never receives the managed ``CODEX_HOME`` root: only its
    two session directories are bind-mounted.  Credentials, static config,
    history, logs, and every other runtime entry remain unavailable inside the
    network-disabled collector.
    """

    home = host_sources_api._ensure_managed_host_home(config_root, record)
    mounts: list[str] = []
    for subpath, destination in (
        ("sessions", "/scan/codex/sessions"),
        ("archived_sessions", "/scan/codex/archived_sessions"),
    ):
        source = home / subpath
        try:
            info = os.lstat(source)
        except OSError as exc:
            raise errors_api.MonitorError("cannot inspect managed host session directory") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise errors_api.MonitorError("unsafe managed host session directory")
        mounts.extend(
            (
                "--mount",
                f"type=bind,src={source},dst={destination},readonly",
            )
        )
    return mounts


def _archive_sessions_for_payload(state_path: Path, payload: dict[str, Any]) -> None:
    """Restore complete session detail that upstream removes from sync payloads."""

    archive_path = state_path / "session-usage-archive.json"
    state_api._secure_collector_file(archive_path, max_bytes=constants_api.MAX_ARCHIVE_BYTES)
    value = state_api._read_json(archive_path, max_bytes=constants_api.MAX_ARCHIVE_BYTES)
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"version", "sessions"}:
        raise errors_api.MonitorError("Token Monitor session archive has an invalid shape")
    archive = value["sessions"]
    if not isinstance(archive, dict) or len(archive) > 200000:
        raise errors_api.MonitorError("Token Monitor session archive is invalid")
    windows = payload.get("periodWindows") if isinstance(payload.get("periodWindows"), dict) else {}
    today_key = str((windows.get("today") or {}).get("key", "")) if isinstance(windows.get("today"), dict) else ""
    month_key = str((windows.get("month") or {}).get("key", "")) if isinstance(windows.get("month"), dict) else ""
    sessions: dict[str, dict[str, Any]] = {"today": {}, "month": {}, "allTime": {}}
    for key, entry in archive.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise errors_api.MonitorError("Token Monitor session archive entry is invalid")
        periods = entry.get("periods")
        if not isinstance(periods, dict):
            raise errors_api.MonitorError("Token Monitor session archive periods are invalid")
        entry_windows = entry.get("periodWindows")
        for period_name in ("today", "month", "allTime"):
            session = periods.get(period_name)
            if not isinstance(session, dict):
                continue
            # Repricing can refresh the shared entry day while retaining the
            # original window marker. Use that per-period marker first, then
            # fall back to the shared fields written by older archives.
            period_window = (
                entry_windows.get(period_name)
                if isinstance(entry_windows, dict)
                and isinstance(entry_windows.get(period_name), dict)
                else {}
            )
            if period_name == "today" and today_key:
                entry_day = period_window.get("day")
                if not isinstance(entry_day, str) or not entry_day:
                    entry_day = entry.get("day")
                if entry_day != today_key:
                    continue
            if period_name == "month" and month_key:
                entry_month = period_window.get("month")
                if not isinstance(entry_month, str) or not entry_month:
                    entry_month = entry.get("month")
                if entry_month != month_key:
                    continue
            cleaned = dict(session)
            cleaned.pop("projectId", None)
            cleaned.pop("projectLabel", None)
            cleaned["projectId"] = ""
            cleaned["projectLabel"] = ""
            sessions[period_name][key] = cleaned
    for period_name, values in sessions.items():
        period = payload.get(period_name)
        if isinstance(period, dict):
            period["sessions"] = values
    payload.pop("sessionDetailsOmitted", None)


def _restore_model_token_usage(state_path: Path, payload: dict[str, Any]) -> None:
    sidecar = state_path / "model-token-usage.json"
    if not sidecar.exists():
        return  # compatible with existing collector images and retained archives
    state_api._secure_collector_file(sidecar, max_bytes=constants_api.MAX_ARCHIVE_BYTES)
    value = state_api._read_json(sidecar, max_bytes=constants_api.MAX_ARCHIVE_BYTES)
    if not isinstance(value, dict) or set(value) != {"version", "observations"} or value["version"] != 1:
        raise errors_api.MonitorError("collector model token sidecar is invalid")
    observations = value["observations"]
    if not isinstance(observations, list) or len(observations) > 16 or any(not isinstance(o, dict) for o in observations):
        raise errors_api.MonitorError("collector model token observations are invalid")
    for name in ("today", "month", "allTime"):
        for key, session in payload[name].get("sessions", {}).items():
            matches = []
            for observation in observations:
                candidate = observation.get(key)
                if not isinstance(candidate, dict):
                    continue
                if all(candidate.get(k) == session.get(k) for k in (
                    "totalTokens", *constants_api.TOKEN_COMPONENT_FIELDS, "models", "providers"
                )):
                    evidence = candidate.get("modelTokenUsage")
                    if not isinstance(evidence, dict):
                        raise errors_api.MonitorError("collector model token evidence is missing")
                    accounting_api._model_token_usage({**session, "modelTokenUsage": evidence})
                    matches.append(evidence)
            if matches and all(m == matches[0] for m in matches):
                session["modelTokenUsage"] = matches[0]


def _run_collector(
    docker: str,
    image: str,
    record: models_api.VolumeRegistration,
    config_root: Path,
    *,
    uid: int,
    gid: int,
) -> dict[str, Any]:
    root = state_api.monitor_root(config_root)
    run_root = root / constants_api.RUN_DIR
    state_api._ensure_private_directory(run_root)
    state_path = snapshots_api._project_state_path(config_root, record)
    pricing_api._write_tokscale_pricing(config_root, state_path)
    output_descriptor, output_name = tempfile.mkstemp(prefix="collector-", suffix=".json", dir=run_root)
    os.close(output_descriptor)
    output_path = Path(output_name)
    output_path.unlink(missing_ok=True)
    output_path.touch(mode=0o600)
    os.chmod(output_path, 0o600)
    mounts = []
    if record.target == "host":
        mounts.extend(_host_session_mounts(config_root, record))
    else:
        for subpath, destination in (("sessions", "/scan/codex/sessions"), ("archived_sessions", "/scan/codex/archived_sessions")):
            if _subpath_available(docker, image, record.volume_name, subpath):
                mounts.extend(("--mount", f"type=volume,src={record.volume_name},dst={destination},readonly,volume-subpath={subpath},volume-nocopy"))
    mounts.extend(("--mount", f"type=bind,src={state_path},dst=/state", "--mount", f"type=bind,src={output_path},dst=/out/summary.json"))
    command = [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        constants_api.COLLECTOR_MEMORY,
        "--cpus",
        constants_api.COLLECTOR_CPUS,
        "--pids-limit",
        constants_api.COLLECTOR_PIDS,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/run:rw,noexec,nosuid,nodev,size=8m",
        "--tmpfs",
        f"/scan/codex:rw,noexec,nosuid,nodev,size=32m,uid={uid},gid={gid},mode=700",
        *mounts,
        "-e",
        "CODEX_HOME=/scan/codex",
        "-e",
        "TOKEN_MONITOR_SHARED_DIR=/state",
        "-e",
        "TOKSCALE_CONFIG_DIR=/state/tokscale",
        "-e",
        "TOKEN_MONITOR_CLIENTS=codex",
        "-e",
        "TOKEN_MONITOR_LIMITS_ENABLED=0",
        "-e",
        # Cage assigns volume projects after cross-volume deduplication.  Keep
        # native project metadata off this boundary because it can contain paths.
        "TOKEN_MONITOR_PROJECTS_ENABLED=0",
        "-e",
        "TOKEN_MONITOR_HISTORY_ENABLED=1",
        "-e",
        "TOKEN_MONITOR_SESSION_USAGE_ARCHIVE_ENABLED=1",
        "-e",
        "TOKEN_MONITOR_OPENCODE_AMBIENT=0",
        "-e",
        "TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS=0",
        "-e",
        "TOKEN_MONITOR_WSL_SCAN=0",
        "-e",
        "TOKEN_MONITOR_WATCH=0",
        "-e",
        f"TOKEN_MONITOR_DEVICE_ID={record.device_id}",
        "-e",
        f"CAGE_MONITOR_DISPLAY_NAME={record.display_name}",
        "-e",
        "CAGE_MONITOR_OUTPUT=/out/summary.json",
        image,
    ]
    try:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=constants_api.SCAN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise errors_api.MonitorError("Token Monitor collector timed out") from exc
        except OSError as exc:
            raise errors_api.MonitorError(f"Token Monitor collector could not start: {exc}") from exc
        if result.returncode != 0:
            if record.target == "host":
                # Docker can echo a bind source in its diagnostic.  The
                # managed source lives below a user-specific configuration
                # directory, and this error later appears in monitor status.
                # Keep the status surface path-free.
                raise errors_api.MonitorError(
                    "Token Monitor collector failed for managed host sessions"
                )
            detail = result.stderr.strip().replace("\n", " ")[:300]
            raise errors_api.MonitorError(f"Token Monitor collector failed: {detail or 'unknown error'}")
        if output_path.stat().st_size > constants_api.MAX_OUTPUT_BYTES:
            raise errors_api.MonitorError("Token Monitor collector output is too large")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise errors_api.MonitorError(f"Token Monitor collector output is invalid: {exc}") from exc
        _archive_sessions_for_payload(state_path, payload)
        _restore_model_token_usage(state_path, payload)
        return validation_api._validate_summary(payload, record.device_id)
    finally:
        output_path.unlink(missing_ok=True)
