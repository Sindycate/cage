"""Fail-closed Codex MCP OAuth credential reconciliation.

This is the former launcher-embedded reconciler. Keeping it as a dedicated
adapter preserves its lock, identity, hash, mode, and compare-and-swap
invariants while making lifecycle ownership explicit.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_CREDENTIAL_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 4096
STATE_NAME = ".cage-oauth-sync-state.json"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MISSING_HASH = "-"


class SyncError(Exception):
    pass


def _reject_constant(value: str) -> None:
    raise SyncError("non-finite JSON value is not allowed")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SyncError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SyncError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"{label} must contain a JSON object")
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 100_000:
            raise SyncError(f"{label} contains too many JSON values")
        if depth > 64:
            raise SyncError(f"{label} is nested too deeply")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _fingerprint_of(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_regular(
    path: str,
    limit: int,
    label: str,
    *,
    normalize_mode: bool = False,
    missing_ok: bool = True,
) -> dict[str, Any] | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SyncError(f"{label} is missing")
    if stat.S_ISLNK(before.st_mode):
        raise SyncError(f"{label} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise SyncError(f"{label} is not a regular file")
    if before.st_size > limit:
        raise SyncError(f"{label} exceeds the {limit}-byte limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SyncError(f"cannot safely open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            _identity_of(before) != _identity_of(opened)
            or _identity_of(opened) != _identity_of(current)
        ):
            raise SyncError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise SyncError(f"{label} exceeds the {limit}-byte limit")
        if normalize_mode:
            os.fchmod(descriptor, 0o600)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            _identity_of(opened) != _identity_of(after)
            or _identity_of(after) != _identity_of(current)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise SyncError(f"{label} changed while it was read")
        raw = b"".join(chunks)
        return {
            "raw": raw,
            "raw_hash": _sha256(raw),
            "mode": stat.S_IMODE(after.st_mode),
            "fingerprint": _fingerprint_of(after),
        }
    finally:
        os.close(descriptor)


def _credential_blob(
    path: str,
    label: str,
    *,
    normalize_mode: bool = False,
    missing_ok: bool = True,
) -> dict[str, Any] | None:
    blob = _read_regular(
        path,
        MAX_CREDENTIAL_BYTES,
        label,
        normalize_mode=normalize_mode,
        missing_ok=missing_ok,
    )
    if blob is None:
        return None
    value = _parse_object(blob["raw"], label)
    blob["value"] = value
    blob["canonical"] = _canonical_json(value)
    if len(blob["canonical"]) > MAX_CREDENTIAL_BYTES:
        raise SyncError(
            f"{label} canonical form exceeds the {MAX_CREDENTIAL_BYTES}-byte limit"
        )
    blob["hash"] = _sha256(blob["canonical"])
    return blob


def _state_blob(
    path: str, label: str, *, missing_ok: bool = True
) -> dict[str, Any] | None:
    blob = _read_regular(path, MAX_STATE_BYTES, label, missing_ok=missing_ok)
    if blob is None:
        return None
    value = _parse_object(blob["raw"], label)
    if set(value) != {"version", "identity", "base_sha256", "revision"}:
        raise SyncError(f"{label} has an unsupported schema")
    if value["version"] != 1:
        raise SyncError(f"{label} has an unsupported version")
    if not isinstance(value["identity"], str) or not HASH_RE.fullmatch(
        value["identity"]
    ):
        raise SyncError(f"{label} has an invalid identity")
    base = value["base_sha256"]
    if base is not None and (
        not isinstance(base, str) or not HASH_RE.fullmatch(base)
    ):
        raise SyncError(f"{label} has an invalid base hash")
    revision = value["revision"]
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise SyncError(f"{label} has an invalid revision")
    blob["value"] = value
    blob["canonical"] = _canonical_json(value)
    blob["hash"] = _sha256(blob["canonical"])
    return blob


EXPORT_HELPER = r'''
import hashlib
import json
import os
import stat
import sys

volume_root, output_root, credential_limit, state_limit = sys.argv[1:]
limits = {
    ".credentials.json": int(credential_limit),
    ".cage-oauth-sync-state.json": int(state_limit),
}
outputs = {
    ".credentials.json": "credentials.raw",
    ".cage-oauth-sync-state.json": "state.raw",
}

def ident(info):
    return (info.st_dev, info.st_ino)

def export_one(name):
    source = os.path.join(volume_root, name)
    try:
        before = os.lstat(source)
    except FileNotFoundError:
        return {"exists": False, "raw_sha256": None, "mode": None}
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("unsafe non-regular OAuth volume state: " + name)
    if before.st_size > limits[name]:
        raise RuntimeError("oversized OAuth volume state: " + name)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(source)
        if ident(before) != ident(opened) or ident(opened) != ident(current):
            raise RuntimeError("OAuth volume state changed while opening: " + name)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limits[name] + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limits[name]:
                raise RuntimeError("oversized OAuth volume state: " + name)
        after = os.fstat(descriptor)
        current = os.lstat(source)
        if (
            ident(opened) != ident(after)
            or ident(after) != ident(current)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("OAuth volume state changed while reading: " + name)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    destination = os.path.join(output_root, outputs[name])
    out = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(out, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(out)
    return {
        "exists": True,
        "raw_sha256": hashlib.sha256(data).hexdigest(),
        "mode": stat.S_IMODE(after.st_mode),
    }

manifest = {
    "credentials": export_one(".credentials.json"),
    "state": export_one(".cage-oauth-sync-state.json"),
}
data = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
descriptor = os.open(
    os.path.join(output_root, "manifest.json"),
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    with os.fdopen(descriptor, "wb", closefd=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
finally:
    os.close(descriptor)
'''


APPLY_HELPER = r'''
import hashlib
import os
import secrets
import stat
import sys

(
    volume_root,
    input_root,
    expected_credential_hash,
    expected_state_hash,
    credential_present,
    owner_uid,
    owner_gid,
    credential_limit,
    state_limit,
) = sys.argv[1:]
owner_uid = int(owner_uid)
owner_gid = int(owner_gid)
credential_limit = int(credential_limit)
state_limit = int(state_limit)
directory = os.open(volume_root, os.O_RDONLY | os.O_DIRECTORY)

def current_hash(name, limit):
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return "-"
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("unsafe non-regular OAuth volume state: " + name)
    if before.st_size > limit:
        raise RuntimeError("oversized OAuth volume state: " + name)
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory,
    )
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("OAuth volume state changed while opening: " + name)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > limit:
                raise RuntimeError("oversized OAuth volume state: " + name)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError("OAuth volume state changed while reading: " + name)
        return digest.hexdigest()
    finally:
        os.close(descriptor)

def read_input(name, limit):
    path = os.path.join(input_root, name)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("unsafe staged OAuth sync input: " + name)
    if info.st_size > limit:
        raise RuntimeError("oversized staged OAuth sync input: " + name)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("staged OAuth sync input changed: " + name)
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise RuntimeError("oversized staged OAuth sync input: " + name)
        return b"".join(chunks)
    finally:
        os.close(descriptor)

def atomic_write(name, data):
    temporary = "." + name.lstrip(".") + ".cage-" + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)

state_data = read_input("state.json", state_limit)
credential_data = (
    read_input("credentials.json", credential_limit)
    if credential_present == "1"
    else None
)
if current_hash(".credentials.json", credential_limit) != expected_credential_hash:
    raise RuntimeError("OAuth credential conflict: volume changed during reconciliation")
if current_hash(".cage-oauth-sync-state.json", state_limit) != expected_state_hash:
    raise RuntimeError("OAuth sync-state conflict: volume changed during reconciliation")
if credential_data is not None:
    atomic_write(".credentials.json", credential_data)
else:
    try:
        info = os.stat(".credentials.json", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(info.st_mode):
            raise RuntimeError("OAuth credential destination became a directory")
        os.unlink(".credentials.json", dir_fd=directory)
atomic_write(".cage-oauth-sync-state.json", state_data)
os.fsync(directory)
os.close(directory)
'''


@dataclass(frozen=True)
class OAuthReconciler:
    volume_name: str
    image: str
    host_directory: Path
    config_directory: Path
    docker: str = "docker"
    host_uid: int = -1
    host_gid: int = -1

    def _uid(self) -> int:
        return os.getuid() if self.host_uid < 0 else self.host_uid

    def _gid(self) -> int:
        return os.getgid() if self.host_gid < 0 else self.host_gid

    def _checked_directory(
        self, path: str, label: str, *, create: bool = False
    ) -> str:
        if create:
            os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise SyncError(f"cannot access {label}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SyncError(f"{label} must be a real directory")
        if info.st_uid != self._uid():
            raise SyncError(f"{label} must be owned by the current user")
        os.chmod(path, 0o700)
        return path

    @staticmethod
    def _acquire_lock(path: str, timeout: float = 15.0) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise SyncError(f"cannot open OAuth sync lock: {exc}") from exc
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise SyncError(
                        "timed out waiting for another OAuth reconciliation"
                    )
                time.sleep(0.1)

    def _run_helper(self, arguments: list[str], phase: str) -> None:
        helper_name = f"cage-oauth-{os.getpid()}-{secrets.token_hex(5)}"
        command = [
            self.docker,
            "run",
            "--rm",
            "--name",
            helper_name,
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [self.docker, "rm", "-f", helper_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            raise SyncError(f"OAuth {phase} helper timed out") from exc
        except OSError as exc:
            raise SyncError(f"cannot run OAuth {phase} helper: {exc}") from exc
        if result.returncode != 0:
            raise SyncError(
                f"OAuth {phase} helper failed with status {result.returncode}"
            )

    def _export_volume(self, stage: str) -> None:
        self._run_helper(
            [
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=16m",
                "--user",
                f"{self._uid()}:{self._gid()}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "32",
                "--memory",
                "96m",
                "--mount",
                f"type=volume,src={self.volume_name},dst=/vol,readonly",
                "--mount",
                f"type=bind,src={stage},dst=/out",
                "--entrypoint",
                "python3",
                self.image,
                "-I",
                "-c",
                EXPORT_HELPER,
                "/vol",
                "/out",
                str(MAX_CREDENTIAL_BYTES),
                str(MAX_STATE_BYTES),
            ],
            "export",
        )

    def _apply_volume(
        self,
        stage: str,
        manifest: dict[str, Any],
        credential: dict[str, Any] | None,
    ) -> None:
        self._run_helper(
            [
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=16m",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--cap-add",
                "DAC_OVERRIDE",
                "--cap-add",
                "FOWNER",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "32",
                "--memory",
                "96m",
                "--mount",
                f"type=volume,src={self.volume_name},dst=/vol",
                "--mount",
                f"type=bind,src={stage},dst=/sync-in,readonly",
                "--entrypoint",
                "python3",
                self.image,
                "-I",
                "-c",
                APPLY_HELPER,
                "/vol",
                "/sync-in",
                manifest["credentials"]["raw_sha256"] or MISSING_HASH,
                manifest["state"]["raw_sha256"] or MISSING_HASH,
                "1" if credential is not None else "0",
                str(self._uid()),
                str(self._gid()),
                str(MAX_CREDENTIAL_BYTES),
                str(MAX_STATE_BYTES),
            ],
            "apply",
        )

    @staticmethod
    def _read_manifest(stage: str) -> dict[str, Any]:
        blob = _read_regular(
            os.path.join(stage, "manifest.json"),
            MAX_STATE_BYTES,
            "OAuth export manifest",
            missing_ok=False,
        )
        assert blob is not None
        manifest = _parse_object(blob["raw"], "OAuth export manifest")
        if set(manifest) != {"credentials", "state"}:
            raise SyncError("OAuth export manifest has an unsupported schema")
        for key in ("credentials", "state"):
            entry = manifest[key]
            if not isinstance(entry, dict) or set(entry) != {
                "exists",
                "raw_sha256",
                "mode",
            }:
                raise SyncError("OAuth export manifest has an unsupported entry")
            if not isinstance(entry["exists"], bool):
                raise SyncError(
                    "OAuth export manifest has an invalid existence flag"
                )
            if entry["exists"]:
                if not isinstance(entry["raw_sha256"], str) or not HASH_RE.fullmatch(
                    entry["raw_sha256"]
                ):
                    raise SyncError("OAuth export manifest has an invalid hash")
                if not isinstance(entry["mode"], int):
                    raise SyncError("OAuth export manifest has an invalid mode")
            elif entry["raw_sha256"] is not None or entry["mode"] is not None:
                raise SyncError(
                    "OAuth export manifest has inconsistent missing-file data"
                )
        return manifest

    @staticmethod
    def _verify_export(
        stage: str, manifest: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        credential = _credential_blob(
            os.path.join(stage, "credentials.raw"),
            "volume OAuth credentials",
            missing_ok=not manifest["credentials"]["exists"],
        )
        state = _state_blob(
            os.path.join(stage, "state.raw"),
            "volume OAuth sync state",
            missing_ok=not manifest["state"]["exists"],
        )
        for key, blob in (("credentials", credential), ("state", state)):
            if manifest[key]["exists"] != (blob is not None):
                raise SyncError("OAuth export helper returned inconsistent files")
            if blob is not None and manifest[key]["raw_sha256"] != blob["raw_hash"]:
                raise SyncError("OAuth export helper returned inconsistent content")
            if blob is not None:
                blob["source_mode"] = manifest[key]["mode"]
        return credential, state

    @staticmethod
    def _ensure_host_unchanged(
        path: str, observed: dict[str, Any] | None
    ) -> None:
        current = _credential_blob(
            path,
            "host OAuth credentials",
            missing_ok=observed is None,
        )
        if (current is None) != (observed is None):
            raise SyncError("host OAuth credentials changed during reconciliation")
        if current is not None and observed is not None and (
            current["raw_hash"] != observed["raw_hash"]
            or current["fingerprint"] != observed["fingerprint"]
        ):
            raise SyncError("host OAuth credentials changed during reconciliation")

    def _atomic_host_write(
        self, path: str, data: bytes, observed: dict[str, Any] | None
    ) -> None:
        self._ensure_host_unchanged(path, observed)
        directory = os.path.dirname(path)
        descriptor: int | None = None
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".credentials.json.cage-", dir=directory
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._ensure_host_unchanged(path, observed)
            os.replace(temporary, path)
            temporary = None
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _write_stage(path: str, data: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)

    def reconcile(self) -> None:
        host_directory = os.path.realpath(os.path.abspath(self.host_directory))
        self._checked_directory(host_directory, "host Codex directory")
        host_path = os.path.join(host_directory, ".credentials.json")
        identity = _sha256(
            b"cage-codex-oauth-v1\0" + os.fsencode(host_directory)
        )
        config_directory = os.path.realpath(os.path.abspath(self.config_directory))
        self._checked_directory(config_directory, "Cage config directory")
        lock_directory = os.path.join(config_directory, "oauth-locks")
        self._checked_directory(
            lock_directory, "OAuth lock directory", create=True
        )
        lock_names = sorted(
            {
                f"identity-{identity}.lock",
                "volume-"
                + _sha256(self.volume_name.encode("utf-8"))
                + ".lock",
            }
        )
        with contextlib.ExitStack() as stack:
            for name in lock_names:
                descriptor = self._acquire_lock(
                    os.path.join(lock_directory, name)
                )
                stack.callback(os.close, descriptor)
            host = _credential_blob(
                host_path,
                "host OAuth credentials",
                normalize_mode=True,
                missing_ok=True,
            )
            with tempfile.TemporaryDirectory(
                prefix=".cage-oauth-sync-",
                dir=config_directory,
            ) as stage:
                os.chmod(stage, 0o700)
                self._export_volume(stage)
                manifest = self._read_manifest(stage)
                volume, state = self._verify_export(stage, manifest)
                same_identity = (
                    state is not None and state["value"]["identity"] == identity
                )
                if not same_identity:
                    if state is not None:
                        print(
                            "cage: Codex OAuth state identity changed; resetting "
                            "it from the selected host Codex directory",
                            file=os.sys.stderr,
                        )
                    desired = host
                    previous_revision = 0
                else:
                    assert state is not None
                    previous_revision = state["value"]["revision"]
                    base = state["value"]["base_sha256"]
                    host_hash = host["hash"] if host is not None else None
                    volume_hash = (
                        volume["hash"] if volume is not None else None
                    )
                    if host is None:
                        desired = None
                    elif volume is None:
                        desired = host
                    elif host_hash == volume_hash:
                        desired = host
                    else:
                        host_changed = host_hash != base
                        volume_changed = volume_hash != base
                        if host_changed and volume_changed:
                            raise SyncError(
                                "OAuth credential conflict: host and volume both "
                                "changed; neither copy was overwritten"
                            )
                        if volume_changed:
                            desired = volume
                            self._atomic_host_write(
                                host_path, desired["canonical"], host
                            )
                            host = _credential_blob(
                                host_path,
                                "host OAuth credentials",
                                normalize_mode=True,
                                missing_ok=False,
                            )
                        else:
                            desired = host
                desired_hash = (
                    desired["hash"] if desired is not None else None
                )
                volume_hash = (
                    volume["hash"] if volume is not None else None
                )
                state_core_matches = (
                    same_identity
                    and state is not None
                    and state["value"]["base_sha256"] == desired_hash
                )
                credential_matches = (
                    (volume is None) == (desired is None)
                    and (volume is None or volume_hash == desired_hash)
                )
                credential_mode_ok = (
                    volume is None or volume["source_mode"] == 0o600
                )
                state_mode_ok = (
                    state is not None and state["source_mode"] == 0o600
                )
                apply_needed = not (
                    state_core_matches
                    and credential_matches
                    and credential_mode_ok
                    and state_mode_ok
                )
                if apply_needed:
                    revision = (
                        previous_revision + 1 if same_identity else 1
                    )
                    desired_state = {
                        "version": 1,
                        "identity": identity,
                        "base_sha256": desired_hash,
                        "revision": revision,
                    }
                    self._write_stage(
                        os.path.join(stage, "state.json"),
                        _canonical_json(desired_state),
                    )
                    if desired is not None:
                        self._write_stage(
                            os.path.join(stage, "credentials.json"),
                            desired["canonical"],
                        )
                    self._apply_volume(stage, manifest, desired)
