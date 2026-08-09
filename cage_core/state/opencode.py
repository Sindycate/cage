"""Validated OpenCode provider and selected-MCP authentication sync."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .oauth import (
    MAX_CREDENTIAL_BYTES,
    MISSING_HASH,
    SyncError,
    _canonical_json,
    _credential_blob,
    _parse_object,
    _sha256,
)


EXPORT_HELPER = r'''
import hashlib, json, os, stat, sys
root, output, limit = sys.argv[1:]
limit = int(limit)
paths = {"auth": "auth.json", "mcp": "mcp-auth.json"}
manifest = {}
def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RuntimeError("short write while exporting OpenCode state")
        view = view[written:]
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
directory = os.open(root, flags)
try:
    for component in ("data", "opencode"):
        try:
            child = os.open(component, flags, dir_fd=directory)
        except FileNotFoundError:
            os.close(directory)
            directory = None
            break
        info = os.fstat(child)
        if not stat.S_ISDIR(info.st_mode):
            os.close(child)
            raise RuntimeError("unsafe OpenCode state directory")
        os.close(directory)
        directory = child
    for key, name in paths.items():
      if directory is None:
        manifest[key] = {"exists": False, "sha256": None}
        continue
      try:
          before = os.stat(name, dir_fd=directory, follow_symlinks=False)
      except FileNotFoundError:
          manifest[key] = {"exists": False, "sha256": None}
          continue
      if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
          raise RuntimeError("unsafe OpenCode state file: " + name)
      if before.st_size > limit:
          raise RuntimeError("oversized OpenCode state file: " + name)
      fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
      try:
          opened = os.fstat(fd)
          if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
              raise RuntimeError("OpenCode state changed while opening: " + name)
          chunks, total = [], 0
          while True:
              chunk = os.read(fd, min(65536, limit + 1 - total))
              if not chunk:
                  break
              chunks.append(chunk); total += len(chunk)
              if total > limit:
                  raise RuntimeError("oversized OpenCode state file: " + name)
          after = os.fstat(fd)
          current = os.stat(name, dir_fd=directory, follow_symlinks=False)
          if ((opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or
              (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino) or
              opened.st_size != after.st_size or opened.st_mtime_ns != after.st_mtime_ns):
              raise RuntimeError("OpenCode state changed while reading: " + name)
      finally:
          os.close(fd)
      data = b"".join(chunks)
      out = os.open(os.path.join(output, key + ".raw"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      try:
          write_all(out, data); os.fsync(out)
      finally:
          os.close(out)
      manifest[key] = {"exists": True, "sha256": hashlib.sha256(data).hexdigest()}
finally:
    if directory is not None:
        os.close(directory)
encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd = os.open(os.path.join(output, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    write_all(fd, encoded); os.fsync(fd)
finally:
    os.close(fd)
'''


APPLY_HELPER = r'''
import hashlib, os, secrets, stat, sys
root, source, auth_hash, mcp_hash, auth_present, mcp_present, uid, gid, limit = sys.argv[1:]
uid, gid, limit = int(uid), int(gid), int(limit)
paths = {"auth": "auth.json", "mcp": "mcp-auth.json"}
flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
directory = os.open(root, flags)
for component in ("data", "opencode"):
    try:
        child = os.open(component, flags, dir_fd=directory)
    except FileNotFoundError:
        os.mkdir(component, 0o700, dir_fd=directory)
        child = os.open(component, flags, dir_fd=directory)
    info = os.fstat(child)
    if not stat.S_ISDIR(info.st_mode):
        os.close(child)
        raise RuntimeError("unsafe OpenCode data directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        if info.st_uid != 0:
            os.close(child)
            raise RuntimeError("OpenCode data directory has unsafe permissions")
        os.fchmod(child, 0o700)
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.fchown(child, uid, gid)
    os.close(directory)
    directory = child
def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RuntimeError("short write while applying OpenCode state")
        view = view[written:]
def digest(name):
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return "-"
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError("unsafe OpenCode state destination: " + name)
    if before.st_size > limit:
        raise RuntimeError("oversized OpenCode state destination: " + name)
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
    try:
        value = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk: break
            value.update(chunk); total += len(chunk)
            if total > limit: raise RuntimeError("oversized OpenCode state destination: " + name)
        return value.hexdigest()
    finally:
        os.close(fd)
def staged(key):
    path = os.path.join(source, key + ".json")
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError("unsafe staged OpenCode state: " + key)
    if before.st_size > limit: raise RuntimeError("oversized staged OpenCode state: " + key)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk: break
            chunks.append(chunk); total += len(chunk)
            if total > limit: raise RuntimeError("oversized staged OpenCode state: " + key)
        return b"".join(chunks)
    finally:
        os.close(fd)
def replace(name, data):
    temporary = "." + name + ".cage-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
    try:
        os.fchmod(fd, 0o600); os.fchown(fd, uid, gid)
        write_all(fd, data); os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try: os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError: pass
        raise
    finally:
        os.close(fd)
def apply(key, expected, present):
    name = paths[key]
    if digest(name) != expected:
        raise RuntimeError("OpenCode state changed during reconciliation: " + name)
    if present == "1":
        replace(name, staged(key))
    else:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode): raise RuntimeError("OpenCode state destination became a directory")
        os.unlink(name, dir_fd=directory)
apply("auth", auth_hash, auth_present)
apply("mcp", mcp_hash, mcp_present)
os.fsync(directory); os.close(directory)
'''


def _checked_blob(path: Path, label: str) -> dict[str, Any] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if info.st_nlink != 1:
        raise SyncError(f"{label} must not be hard-linked")
    return _credential_blob(str(path), label)


def _checked_blob_at(
    directory: int, name: str, label: str
) -> dict[str, Any] | None:
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SyncError(f"{label} must be a regular file, not a symlink")
    if before.st_nlink != 1:
        raise SyncError(f"{label} must not be hard-linked")
    if before.st_uid != os.getuid():
        raise SyncError(f"{label} must be owned by the current user")
    if before.st_size > MAX_CREDENTIAL_BYTES:
        raise SyncError(f"{label} exceeds the {MAX_CREDENTIAL_BYTES}-byte limit")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
    except OSError as exc:
        raise SyncError(f"cannot safely open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise SyncError(f"{label} changed while opening")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor, min(65536, MAX_CREDENTIAL_BYTES + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CREDENTIAL_BYTES:
                raise SyncError(
                    f"{label} exceeds the {MAX_CREDENTIAL_BYTES}-byte limit"
                )
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise SyncError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    value = _parse_object(raw, label)
    canonical = _canonical_json(value)
    if len(canonical) > MAX_CREDENTIAL_BYTES:
        raise SyncError(f"{label} canonical form exceeds the byte limit")
    return {
        "value": value,
        "canonical": canonical,
        "hash": _sha256(canonical),
    }


def _validate_provider_store(value: dict[str, Any] | None, label: str) -> None:
    if value is None:
        return
    for provider, entry in value["value"].items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            raise SyncError(f"{label} contains an invalid provider entry")
        kind = entry.get("type")
        allowed: set[str]
        required: dict[str, type]
        if kind == "oauth":
            allowed = {"type", "refresh", "access", "expires", "accountId", "enterpriseUrl"}
            required = {"refresh": str, "access": str, "expires": int}
        elif kind == "api":
            allowed = {"type", "key", "metadata"}
            required = {"key": str}
        elif kind == "wellknown":
            raise SyncError(
                f"{label} contains a wellknown provider whose remote managed "
                "configuration cannot be frozen safely; use copy_auth = false "
                "or a separate OpenCode data directory"
            )
        else:
            raise SyncError(f"{label} contains an unsupported provider auth type")
        if set(entry) - allowed:
            raise SyncError(f"{label} contains unsupported provider auth fields")
        for field_name, field_type in required.items():
            field = entry.get(field_name)
            if not isinstance(field, field_type) or (
                field_type is int and isinstance(field, bool)
            ):
                raise SyncError(f"{label} contains an invalid provider auth field")
        if kind == "oauth" and entry["expires"] < 0:
            raise SyncError(f"{label} contains a negative OAuth expiry")
        for optional in ("accountId", "enterpriseUrl"):
            if optional in entry and not isinstance(entry[optional], str):
                raise SyncError(f"{label} contains an invalid OAuth field")
        metadata = entry.get("metadata")
        if metadata is not None and (
            not isinstance(metadata, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items())
        ):
            raise SyncError(f"{label} contains invalid API-key metadata")


def _validate_mcp_entry(entry: Any, expected_url: str, label: str) -> None:
    if not isinstance(entry, dict):
        raise SyncError(f"{label} contains an invalid MCP OAuth entry")
    if set(entry) - {"tokens", "clientInfo", "codeVerifier", "oauthState", "serverUrl"}:
        raise SyncError(f"{label} contains unsupported MCP OAuth fields")
    if entry.get("serverUrl") != expected_url:
        raise SyncError(f"{label} is not tied to the selected MCP server URL")
    for field_name in ("codeVerifier", "oauthState", "serverUrl"):
        if field_name in entry and not isinstance(entry[field_name], str):
            raise SyncError(f"{label} contains an invalid MCP OAuth field")
    tokens = entry.get("tokens")
    if tokens is not None:
        if not isinstance(tokens, dict) or set(tokens) - {
            "accessToken", "refreshToken", "expiresAt", "scope"
        } or not isinstance(tokens.get("accessToken"), str):
            raise SyncError(f"{label} contains invalid MCP OAuth tokens")
        for field_name in ("refreshToken", "scope"):
            if field_name in tokens and not isinstance(tokens[field_name], str):
                raise SyncError(f"{label} contains an invalid MCP OAuth token field")
        if "expiresAt" in tokens and (
            not isinstance(tokens["expiresAt"], (int, float))
            or isinstance(tokens["expiresAt"], bool)
        ):
            raise SyncError(f"{label} contains an invalid MCP OAuth expiry")
    client = entry.get("clientInfo")
    if client is not None:
        if not isinstance(client, dict) or set(client) - {
            "clientId", "clientSecret", "clientIdIssuedAt", "clientSecretExpiresAt"
        } or not isinstance(client.get("clientId"), str):
            raise SyncError(f"{label} contains invalid MCP OAuth client information")
        if "clientSecret" in client and not isinstance(client["clientSecret"], str):
            raise SyncError(f"{label} contains an invalid MCP OAuth client secret")
        for field_name in ("clientIdIssuedAt", "clientSecretExpiresAt"):
            if field_name in client and (
                not isinstance(client[field_name], (int, float))
                or isinstance(client[field_name], bool)
            ):
                raise SyncError(f"{label} contains invalid MCP OAuth client timing")


def _selected(
    value: dict[str, Any] | None,
    names: set[str],
    urls: dict[str, str],
    label: str,
) -> dict[str, Any]:
    source = value["value"] if value is not None else {}
    result = {name: source[name] for name in sorted(names) if name in source}
    for name, entry in result.items():
        expected_url = urls.get(name)
        if not expected_url:
            raise SyncError(f"{label} contains OAuth state for a non-OAuth MCP server")
        _validate_mcp_entry(entry, expected_url, f"{label} entry {name!r}")
    return result


@dataclass
class OpenCodeStateReconciler:
    volume_name: str
    image: str
    host_directory: Path
    config_directory: Path
    selected_mcp_names: set[str]
    copy_auth: bool
    selected_mcp_urls: dict[str, str] = field(default_factory=dict)
    docker: str = "docker"
    host_uid: int = -1
    host_gid: int = -1
    _base_auth: bytes | None = field(default=None, init=False, repr=False)
    _base_mcp: bytes = field(default=b"{}\n", init=False, repr=False)

    def _uid(self) -> int:
        return os.getuid() if self.host_uid < 0 else self.host_uid

    def _gid(self) -> int:
        return os.getgid() if self.host_gid < 0 else self.host_gid

    def _ensure_host_directory(self) -> None:
        self.host_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(self.host_directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SyncError("OpenCode host data directory must be a real directory")
        if info.st_uid != self._uid():
            raise SyncError("OpenCode host data directory must be owned by the current user")
        os.chmod(self.host_directory, 0o700)

    def _open_host_directory(self) -> int:
        self._ensure_host_directory()
        before = os.lstat(self.host_directory)
        descriptor = os.open(
            self.host_directory,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise SyncError("OpenCode host data directory changed while opening")
        return descriptor

    def _verify_host_directory(self, descriptor: int) -> None:
        opened = os.fstat(descriptor)
        current = os.lstat(self.host_directory)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise SyncError("OpenCode host data directory changed during reconciliation")

    def _lock(self) -> int:
        lock = self.config_directory / (
            ".opencode-sync-" + _sha256(str(self.host_directory.resolve()).encode()) + ".lock"
        )
        try:
            before = os.lstat(lock)
        except FileNotFoundError:
            before = None
        fd = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(fd)
        current = os.lstat(lock)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != self._uid()
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (
                before is not None
                and (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
            )
        ):
            os.close(fd)
            raise SyncError("unsafe OpenCode state reconciliation lock")
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise SyncError("timed out waiting for OpenCode state reconciliation")
                time.sleep(0.1)

    def _run_helper(self, arguments: list[str], phase: str) -> None:
        name = f"cage-opencode-sync-{os.getpid()}-{secrets.token_hex(5)}"
        try:
            result = subprocess.run(
                [self.docker, "run", "--rm", "--name", name, *arguments],
                stdout=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(
                [self.docker, "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            raise SyncError(f"OpenCode {phase} helper timed out") from exc
        if result.returncode != 0:
            raise SyncError(
                f"OpenCode {phase} helper failed with status {result.returncode}"
            )

    def _export(self, stage: Path) -> dict[str, Any]:
        self._run_helper(
            [
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "32",
                "--memory", "96m",
                "--user", f"{self._uid()}:{self._gid()}",
                "--mount", f"type=volume,src={self.volume_name},dst=/state,readonly",
                "--mount", f"type=bind,src={stage},dst=/out",
                "--entrypoint", "python3", self.image, "-I", "-c", EXPORT_HELPER,
                "/state", "/out", str(MAX_CREDENTIAL_BYTES),
            ],
            "export",
        )
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        if set(manifest) != {"auth", "mcp"}:
            raise SyncError("OpenCode export helper returned an invalid manifest")
        return manifest

    def _volume_blob(self, stage: Path, manifest: dict[str, Any], key: str) -> dict[str, Any] | None:
        if not manifest[key].get("exists"):
            return None
        value = _checked_blob(stage / f"{key}.raw", f"OpenCode volume {key} state")
        if key == "auth":
            _validate_provider_store(value, "OpenCode volume provider auth")
        return value

    def _apply(
        self,
        stage: Path,
        manifest: dict[str, Any],
        auth: bytes | None,
        mcp: bytes | None,
    ) -> None:
        if auth is not None:
            (stage / "auth.json").write_bytes(auth)
            os.chmod(stage / "auth.json", 0o600)
        if mcp is not None:
            (stage / "mcp.json").write_bytes(mcp)
            os.chmod(stage / "mcp.json", 0o600)
        self._run_helper(
            [
                "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
                "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
                "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "96m",
                "--mount", f"type=volume,src={self.volume_name},dst=/state",
                "--mount", f"type=bind,src={stage},dst=/in,readonly",
                "--entrypoint", "python3", self.image, "-I", "-c", APPLY_HELPER,
                "/state", "/in",
                manifest["auth"].get("sha256") or MISSING_HASH,
                manifest["mcp"].get("sha256") or MISSING_HASH,
                "1" if auth is not None else "0", "1" if mcp is not None else "0",
                str(self._uid()), str(self._gid()), str(MAX_CREDENTIAL_BYTES),
            ],
            "apply",
        )

    def _stage(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="opencode-state-", dir=self.config_directory))

    @staticmethod
    def _atomic_host_write(
        directory: int,
        name: str,
        value: bytes,
        expected: dict[str, Any] | None,
    ) -> None:
        current = _checked_blob_at(directory, name, f"OpenCode host state {name}")
        current_hash = current["hash"] if current is not None else None
        expected_hash = expected["hash"] if expected is not None else None
        if current_hash != expected_hash:
            raise SyncError(f"OpenCode host state changed during reconciliation: {name}")
        temporary = f".{name}.cage-{secrets.token_hex(16)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(fd)

    @staticmethod
    def _atomic_host_remove(
        directory: int, name: str, expected: dict[str, Any] | None
    ) -> None:
        current = _checked_blob_at(directory, name, f"OpenCode host state {name}")
        current_hash = current["hash"] if current is not None else None
        expected_hash = expected["hash"] if expected is not None else None
        if current_hash != expected_hash:
            raise SyncError(f"OpenCode host state changed during reconciliation: {name}")
        if current is not None:
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)

    def sync_in(self) -> None:
        fd = self._lock()
        stage = self._stage()
        host = -1
        try:
            manifest = self._export(stage)
            self._volume_blob(stage, manifest, "auth")
            self._volume_blob(stage, manifest, "mcp")
            needs_host = self.copy_auth or bool(self.selected_mcp_names)
            if needs_host:
                host = self._open_host_directory()
            host_auth = None
            if self.copy_auth:
                host_auth = _checked_blob_at(
                    host, "auth.json", "OpenCode host auth.json"
                )
            host_mcp = None
            if self.selected_mcp_names:
                host_mcp = _checked_blob_at(
                    host, "mcp-auth.json", "OpenCode host mcp-auth.json"
                )
            _validate_provider_store(host_auth, "OpenCode host auth.json")
            auth = host_auth["canonical"] if self.copy_auth and host_auth is not None else None
            selected = _selected(
                host_mcp,
                self.selected_mcp_names,
                self.selected_mcp_urls,
                "OpenCode host mcp-auth.json",
            )
            mcp = _canonical_json(selected)
            self._apply(stage, manifest, auth, mcp)
            self._base_auth = auth
            self._base_mcp = mcp
            if host >= 0:
                self._verify_host_directory(host)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if host >= 0:
                os.close(host)
            os.close(fd)

    def sync_out(self) -> None:
        fd = self._lock()
        stage = self._stage()
        host = -1
        try:
            manifest = self._export(stage)
            volume_auth = self._volume_blob(stage, manifest, "auth")
            volume_mcp = self._volume_blob(stage, manifest, "mcp")
            needs_host = self.copy_auth or bool(self.selected_mcp_names)
            if needs_host:
                host = self._open_host_directory()
            current_auth = None
            if self.copy_auth:
                current_auth = _checked_blob_at(
                    host, "auth.json", "OpenCode host auth.json"
                )
            current_mcp = None
            if self.selected_mcp_names:
                current_mcp = _checked_blob_at(
                    host, "mcp-auth.json", "OpenCode host mcp-auth.json"
                )
            if self.copy_auth:
                volume_bytes = volume_auth["canonical"] if volume_auth is not None else None
                current_bytes = current_auth["canonical"] if current_auth is not None else None
                if current_bytes != self._base_auth and volume_bytes != self._base_auth and current_bytes != volume_bytes:
                    raise SyncError("OpenCode provider auth changed on both host and volume")
                if volume_bytes != self._base_auth and volume_bytes != current_bytes:
                    if volume_bytes is None:
                        self._atomic_host_remove(
                            host, "auth.json", current_auth
                        )
                    else:
                        self._atomic_host_write(
                            host, "auth.json", volume_bytes, current_auth
                        )
            volume_selected = _selected(
                volume_mcp,
                self.selected_mcp_names,
                self.selected_mcp_urls,
                "OpenCode volume mcp-auth.json",
            )
            volume_selected_bytes = _canonical_json(volume_selected)
            if self.selected_mcp_names:
                current_selected = _selected(
                    current_mcp,
                    self.selected_mcp_names,
                    self.selected_mcp_urls,
                    "OpenCode host mcp-auth.json",
                )
                current_selected_bytes = _canonical_json(current_selected)
                if (
                    current_selected_bytes != self._base_mcp
                    and volume_selected_bytes != self._base_mcp
                    and current_selected_bytes != volume_selected_bytes
                ):
                    raise SyncError(
                        "OpenCode selected MCP OAuth state changed on both host and volume"
                    )
                if (
                    volume_selected_bytes != self._base_mcp
                    and volume_selected_bytes != current_selected_bytes
                ):
                    merged = dict(
                        current_mcp["value"] if current_mcp is not None else {}
                    )
                    for name in self.selected_mcp_names:
                        merged.pop(name, None)
                    merged.update(volume_selected)
                    self._atomic_host_write(
                        host, "mcp-auth.json", _canonical_json(merged), current_mcp
                    )
            if host >= 0:
                self._verify_host_directory(host)
            self._apply(
                stage,
                manifest,
                volume_auth["canonical"]
                if self.copy_auth and volume_auth is not None
                else None,
                volume_selected_bytes,
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            if host >= 0:
                os.close(host)
            os.close(fd)
