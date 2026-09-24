"""Private one-way Codex container accounting export for PokeTokenBar."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time

from . import poketoken_records as records
from .models import LaunchPlan
from .monitoring import collector, volumes
from .monitoring.errors import MonitorError


INTERVAL_SECONDS = 300
COLLECT_TIMEOUT = 120
CAPABILITY = "poketoken-local-export"


class ExportError(Exception):
    pass


def export_path(config_root: Path) -> Path:
    return config_root / "poketoken" / "exports"


def _check_file(fd: int, limit: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit
    ):
        raise ExportError("unsafe private PokeTokenBar file")


@contextmanager
def _directory(parent: int, name: str):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExportError("PokeTokenBar directories must be owned by you and mode 0700")
        yield fd
    finally:
        os.close(fd)


def _read(fd: int, name: str, limit: int) -> bytes | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as handle:
        _check_file(handle.fileno(), limit)
        result = handle.read(limit + 1)
        if len(result) > limit:
            raise ExportError("PokeTokenBar state exceeds its byte limit")
        return result


def _write(fd: int, name: str, data: bytes) -> None:
    existing = _read(fd, name, records.MAX_OUTPUT)
    if existing == data:
        return  # Preserve mtimes so the downstream incremental cache stays useful.
    temporary = ".pending-" + secrets.token_hex(16)
    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(target, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


@contextmanager
def _store(config_root: Path):
    base = os.open(config_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(base)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ExportError("unsafe Cage configuration directory")
        with _directory(base, "poketoken") as root:
            lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
            try:
                _check_file(lock, 0)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ExportError("another PokeTokenBar export is busy; retry shortly")
                        time.sleep(0.1)
                yield root
            finally:
                os.close(lock)
    finally:
        os.close(base)


def _opaque(key: bytes, domain: str, value: str) -> str:
    return hmac.new(key, (domain + "\0" + value).encode(), hashlib.sha256).hexdigest()


def _key(root: int) -> bytes:
    value = _read(root, "identity", 32)
    if value is None:
        value = secrets.token_bytes(32)
        _write(root, "identity", value)
    if len(value) != 32:
        raise ExportError("invalid PokeTokenBar export identity")
    return value


def _collect(docker: str, plan: LaunchPlan, install_root: Path) -> bytes:
    mounts = []
    roots = []
    for subpath in ("sessions", "archived_sessions"):
        if collector._subpath_available(docker, plan.image, plan.volume_name, subpath):
            destination = "/scan/" + subpath
            mounts.extend((
                "--mount",
                f"type=volume,src={plan.volume_name},dst={destination},readonly,volume-subpath={subpath},volume-nocopy",
            ))
            roots.append(destination)
    if not roots:
        return b""
    name = "cage-poketoken-" + secrets.token_hex(12)
    command = [
        docker, "run", "--rm", "--name", name, "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "512m", "--cpus", "1", "--pids-limit", "64",
        "--user", f"{os.getuid()}:{os.getgid()}", "-i", *mounts, "--entrypoint", "python3",
        plan.image, "-I", "-", *roots,
    ]
    script = (install_root / "cage_core" / "poketoken_records.py").read_bytes()
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                command, input=script, stdout=output, stderr=subprocess.DEVNULL,
                timeout=COLLECT_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A terminated Docker client does not necessarily stop its container.
            subprocess.run(
                [docker, "rm", "-f", name], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
            raise ExportError("PokeTokenBar collection timed out") from exc
        if result.returncode:
            raise ExportError("PokeTokenBar collection failed; source may be invalid, unsafe, or oversized")
        if output.tell() > records.MAX_OUTPUT:
            raise ExportError("PokeTokenBar collection exceeds its output limit")
        output.seek(0)
        return output.read(records.MAX_OUTPUT + 1)


def _prepare(data: bytes, key: bytes) -> dict[str, bytes]:
    if len(data) > records.MAX_OUTPUT:
        raise ExportError("PokeTokenBar collection exceeds its output limit")
    files: dict[str, bytes] = {}
    for line in data.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != {"name", "records"}:
            raise ExportError("invalid PokeTokenBar collection envelope")
        name = records.identifier(value["name"])
        raw = value["records"]
        if not isinstance(raw, list) or not raw:
            raise ExportError("invalid PokeTokenBar accounting records")
        clean = []
        for record in raw:
            sanitized = records.sanitize_record(record)
            if sanitized is None:
                raise ExportError("unexpected PokeTokenBar accounting record")
            if sanitized["type"] == "session_meta":
                payload = sanitized["payload"]
                for field in ("id", "forked_from_id"):
                    if field in payload:
                        payload[field] = _opaque(key, "session", payload[field])
            clean.append(sanitized)
        if clean[0]["type"] != "session_meta":
            raise ExportError("PokeTokenBar rollout has no initial metadata")
        # The same basename survives moves between active and archived sessions.
        filename = "rollout-" + _opaque(key, "file", name) + ".jsonl"
        content = b"".join(records.encode(record) for record in clean)
        prior = files.get(filename)
        if prior is not None:
            if prior.startswith(content):
                continue
            if not content.startswith(prior):
                raise ExportError("conflicting copies of a Codex rollout")
        files[filename] = content
        if len(files) > records.MAX_FILES:
            raise ExportError("too many PokeTokenBar rollout files")
    return files


def _require_plan(plan: LaunchPlan) -> None:
    if plan.tool != "codex" or plan.target != "container" or CAPABILITY not in plan.capabilities:
        raise ExportError("PokeTokenBar export requires poketoken = true on a Codex container preset")


def sync(config_root: Path, docker: str, install_root: Path, plan: LaunchPlan) -> Path:
    _require_plan(plan)
    try:
        with _store(config_root) as root:
            key = _key(root)
            source = _opaque(key, "volume", plan.volume_name)
            status_name = "status-" + source + ".json"
            try:
                before = volumes.volume_fingerprint(docker, plan.volume_name)
                identity = records.encode(before)
                previous = _read(root, "volume-" + source, 4096)
                if previous is not None and previous != identity:
                    raise ExportError("Codex volume was replaced; refusing to mix export histories")
                files = _prepare(_collect(docker, plan, install_root), key)
                if volumes.volume_fingerprint(docker, plan.volume_name) != before:
                    raise ExportError("Codex volume changed during collection")
                with _directory(root, "exports") as exports, _directory(exports, source) as destination:
                    # Validate every retained destination before making any updates.
                    for name, content in files.items():
                        old = _read(destination, name, records.MAX_OUTPUT)
                        if old is not None and not content.startswith(old):
                            raise ExportError("Codex accounting history changed or shrank; retaining the last good export")
                    for name, content in files.items():
                        _write(destination, name, content)
                _write(root, "volume-" + source, identity)
                result = {"source": source, "updated_at": datetime.now(timezone.utc).isoformat(), "files": len(files), "error": None}
                _write(root, status_name, records.encode(result))
            except (ExportError, MonitorError, OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
                message = str(exc) if isinstance(exc, ExportError) else "PokeTokenBar export failed validation or source access"
                _write(root, status_name, records.encode({"source": source, "error": message}))
                raise ExportError(message) from exc
    except (OSError, ValueError) as exc:
        raise ExportError("cannot access private PokeTokenBar state") from exc
    return export_path(config_root)


def status(config_root: Path) -> dict:
    if not (config_root / "poketoken").exists():
        return {"path": str(export_path(config_root)), "sources": []}
    try:
        with _store(config_root) as root:
            sources = []
            for name in sorted(os.listdir(root)):
                if name.startswith("status-") and name.endswith(".json"):
                    value = _read(root, name, 4096)
                    if value is not None:
                        sources.append(json.loads(value))
            return {"path": str(export_path(config_root)), "sources": sources}
    except (OSError, ValueError) as exc:
        raise ExportError("cannot read private PokeTokenBar status") from exc


class ActiveExport:
    def __init__(self, scan):
        self._scan = scan
        self._stop = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, name="cage-poketoken", daemon=True)
        self._thread.start()

    def _attempt(self) -> None:
        try:
            self._scan()
            self._error = None
        except ExportError as exc:
            self._error = str(exc)
            if not sys.stderr.isatty():
                print(f"WARNING: {exc}; run cage poketoken status", file=sys.stderr)

    def _run(self) -> None:
        self._attempt()
        while not self._stop.wait(INTERVAL_SECONDS):
            self._attempt()

    def stop(self) -> int:
        self._stop.set()
        self._thread.join()
        self._attempt()
        if self._error:
            print(f"WARNING: {self._error}; run cage poketoken status", file=sys.stderr)
        return 0
