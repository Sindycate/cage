"""Adopt isolated host sources and reconcile only authorized static inputs."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from . import constants as constants_api
from . import errors as errors_api
from . import identity as identity_api
from . import models as models_api
from . import state as state_api
from . import validation as validation_api


def _checked_host_source_directory(value: Path | str) -> tuple[Path, os.stat_result]:
    """Resolve one user-owned host Codex directory without following its leaf.

    A monitored host source is deliberately stricter than the ordinary native
    target.  Cage copies only selected configuration into a private state
    directory, so accepting a symlink, group-writable root, or raced leaf here
    would make that private boundary ambiguous.
    """

    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path(os.path.abspath(raw))
    try:
        raw_info = os.lstat(raw)
        if stat.S_ISLNK(raw_info.st_mode):
            raise errors_api.MonitorError("host Codex source must not be a symlink")
        resolved = raw.resolve(strict=True)
        before = os.lstat(resolved)
    except FileNotFoundError as exc:
        raise errors_api.MonitorError("host Codex source directory does not exist") from exc
    except OSError as exc:
        raise errors_api.MonitorError("cannot inspect host Codex source directory") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise errors_api.MonitorError("host Codex source must be a real directory")
    if before.st_uid != os.getuid():
        raise errors_api.MonitorError("host Codex source must be owned by the current user")
    if stat.S_IMODE(before.st_mode) & 0o022:
        raise errors_api.MonitorError("host Codex source must not be group or world writable")
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise errors_api.MonitorError("cannot safely open host Codex source directory") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(resolved)
    finally:
        os.close(descriptor)
    if (
        (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise errors_api.MonitorError("host Codex source changed while it was opened")
    return resolved, opened


def _host_source_identity_from_checked(
    config_root: Path,
    source: Path,
    info: os.stat_result,
) -> tuple[str, dict[str, str]]:
    """Build the opaque source identity from one already-checked directory."""

    material = (
        str(source).encode("utf-8")
        + b"\0"
        + str(info.st_dev).encode("ascii")
        + b":"
        + str(info.st_ino).encode("ascii")
    )
    logical_id = hmac.new(
        bytes.fromhex(identity_api.host_install_id(config_root)),
        b"host-source\0" + material,
        hashlib.sha256,
    ).hexdigest()[:32]
    name = f"{constants_api.HOST_SOURCE_PREFIX}{logical_id}"
    fingerprint = validation_api.validate_fingerprint(
        {
            # The legacy field names are retained for registry compatibility.
            # Their values are opaque and never identify a local pathname.
            "name": name,
            "driver": "host",
            "scope": "local",
            "created_at": logical_id,
            "label_identity": logical_id,
        }
    )
    return logical_id, fingerprint


def _host_source_identity(
    config_root: Path,
    source_home: Path | str,
) -> tuple[Path, str, dict[str, str]]:
    """Return a private, inode-bound identity for one host auth source."""

    source, info = _checked_host_source_directory(source_home)
    logical_id, fingerprint = _host_source_identity_from_checked(
        config_root, source, info
    )
    return source, logical_id, fingerprint


def host_source_logical_id(config_root: Path, source_home: Path | str) -> str:
    """Return the opaque logical ID for an adopted native-host auth source."""

    _source, logical_id, _fingerprint = _host_source_identity(config_root, source_home)
    return logical_id


def _host_source_repository(logical_id: str) -> str:
    """Return the registry marker for a host source without retaining its path."""

    validation_api.validate_logical_id(logical_id)
    return f"/__cage_managed_host_source__/{logical_id}"


def _host_source_paths(
    config_root: Path,
    record: models_api.VolumeRegistration,
) -> tuple[Path, Path, Path]:
    if record.target != "host":
        raise errors_api.MonitorError("monitor registration is not a host source")
    validation_api.validate_logical_id(record.logical_id)
    root = state_api.monitor_root(config_root) / constants_api.HOST_SOURCE_DIR / identity_api.project_id_for(
        config_root, record.logical_id
    )
    home = root / constants_api.HOST_SOURCE_HOME
    return root, home, root / constants_api.HOST_STATIC_SNAPSHOT_FILE


def host_source_home(config_root: Path, record: models_api.VolumeRegistration) -> Path:
    """Return the private managed CODEX_HOME for one adopted host source."""

    _root, home, _manifest = _host_source_paths(config_root, record)
    return home


def _ensure_managed_host_home(config_root: Path, record: models_api.VolumeRegistration) -> Path:
    root, home, _manifest = _host_source_paths(config_root, record)
    state_api._ensure_private_directory(root)
    state_api._ensure_private_directory(home)
    for name in ("sessions", "archived_sessions"):
        state_api._ensure_private_directory(home / name)
    return home


def _read_host_regular(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    missing_ok: bool = True,
) -> bytes | None:
    """Read a source file through one no-follow descriptor and recheck it."""

    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise errors_api.MonitorError(f"{label} is missing")
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise errors_api.MonitorError(f"{label} must be a regular non-symlink file")
    if before.st_uid != os.getuid() or before.st_nlink != 1:
        raise errors_api.MonitorError(f"{label} must be a private user-owned file")
    if before.st_size > maximum_bytes:
        raise errors_api.MonitorError(f"{label} exceeds its size limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot safely open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
        ):
            raise errors_api.MonitorError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise errors_api.MonitorError(f"{label} exceeds its size limit")
        after = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise errors_api.MonitorError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_host_regular_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes | None:
    """Read one allowed source credential through its verified directory FD.

    This is deliberately separate from ``_read_host_regular``: post-session
    compare-and-swap needs to read the source name relative to the same opened
    directory that will receive ``os.replace``.  That prevents a renamed
    source root from redirecting the write-back to a replacement directory.
    """

    if name not in constants_api.HOST_SOURCE_CREDENTIAL_FILES:
        raise errors_api.MonitorError("unsafe host Codex credential name")
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise errors_api.MonitorError(f"{label} must be a regular non-symlink file")
    if before.st_uid != os.getuid() or before.st_nlink != 1:
        raise errors_api.MonitorError(f"{label} must be a private user-owned file")
    if before.st_size > maximum_bytes:
        raise errors_api.MonitorError(f"{label} exceeds its size limit")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise errors_api.MonitorError(f"cannot safely open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
        ):
            raise errors_api.MonitorError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise errors_api.MonitorError(f"{label} exceeds its size limit")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise errors_api.MonitorError(f"{label} changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_private_bytes(path: Path, value: bytes) -> None:
    """Atomically write a single private regular file below Cage state."""

    state_api._ensure_private_directory(path.parent)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise errors_api.MonitorError("cannot inspect managed host state") from exc
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
        or existing.st_uid != os.getuid()
    ):
        raise errors_api.MonitorError("unsafe managed host state file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        state_api._reject_unsafe_path(path, max_bytes=max(len(value), 1) + 1)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise errors_api.MonitorError("cannot write managed host state") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_private_regular(path: Path, *, maximum_bytes: int) -> None:
    try:
        state_api._reject_unsafe_path(path, max_bytes=maximum_bytes)
    except FileNotFoundError:
        return
    try:
        path.unlink()
    except OSError as exc:
        raise errors_api.MonitorError("cannot remove managed host state") from exc


def _static_file_name(name: str) -> bool:
    return name in constants_api.HOST_STATIC_FIXED_FILES or bool(
        constants_api.HOST_STATIC_PROFILE_PATTERN.fullmatch(name)
    )


def _read_host_rules(source: Path) -> dict[str, bytes]:
    """Copy a bounded, symlink-free rules tree without exposing its names."""

    root = source / "rules"
    try:
        root_info = os.lstat(root)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise errors_api.MonitorError("cannot inspect host Codex rules") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise errors_api.MonitorError("host Codex rules must be a real directory")
    if root_info.st_uid != os.getuid():
        raise errors_api.MonitorError("host Codex rules must be owned by the current user")

    files: dict[str, bytes] = {}
    total = 0

    def visit(directory: Path, relative: Path, depth: int) -> None:
        nonlocal total
        if depth > constants_api.MAX_HOST_STATIC_DEPTH:
            raise errors_api.MonitorError("host Codex rules are nested too deeply")
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise errors_api.MonitorError("host Codex rules changed while being read") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise errors_api.MonitorError("host Codex rules contain an unsafe directory")
        if info.st_uid != os.getuid():
            raise errors_api.MonitorError("host Codex rules contain a foreign-owned directory")
        try:
            names = sorted(entry.name for entry in os.scandir(directory))
        except OSError as exc:
            raise errors_api.MonitorError("cannot read host Codex rules") from exc
        for name in names:
            if not name or name in {".", ".."} or any(c in name for c in "\0\r\n"):
                raise errors_api.MonitorError("host Codex rules contain an unsafe name")
            child = directory / name
            child_relative = relative / name
            try:
                child_info = os.lstat(child)
            except OSError as exc:
                raise errors_api.MonitorError("host Codex rules changed while being read") from exc
            if stat.S_ISDIR(child_info.st_mode):
                if stat.S_ISLNK(child_info.st_mode):
                    raise errors_api.MonitorError("host Codex rules contain a symlink")
                visit(child, child_relative, depth + 1)
                continue
            if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISREG(child_info.st_mode):
                raise errors_api.MonitorError("host Codex rules contain an unsafe file")
            if len(files) >= constants_api.MAX_HOST_STATIC_FILES:
                raise errors_api.MonitorError("host Codex rules contain too many files")
            remaining = constants_api.MAX_HOST_STATIC_BYTES - total
            if remaining <= 0:
                raise errors_api.MonitorError("host Codex static configuration is too large")
            data = _read_host_regular(
                child,
                label="host Codex rule",
                maximum_bytes=remaining,
                missing_ok=False,
            )
            assert data is not None
            total += len(data)
            files[str(child_relative)] = data

    visit(root, Path(), 0)
    return files


def _read_host_static_source(source: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        names = sorted(entry.name for entry in os.scandir(source))
    except OSError as exc:
        raise errors_api.MonitorError("cannot read host Codex source directory") from exc
    for name in names:
        if not _static_file_name(name):
            continue
        if len(files) >= constants_api.MAX_HOST_STATIC_FILES:
            raise errors_api.MonitorError("host Codex static configuration contains too many files")
        data = _read_host_regular(
            source / name,
            label="host Codex static configuration",
            maximum_bytes=constants_api.MAX_HOST_STATIC_BYTES - total,
        )
        if data is None:
            continue
        total += len(data)
        if total > constants_api.MAX_HOST_STATIC_BYTES:
            raise errors_api.MonitorError("host Codex static configuration is too large")
        files[name] = data
    rules = _read_host_rules(source)
    if total + sum(len(item) for item in rules.values()) > constants_api.MAX_HOST_STATIC_BYTES:
        raise errors_api.MonitorError("host Codex static configuration is too large")
    return files, rules


def _static_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[name]).digest())
    return digest.hexdigest()


def _load_host_static_snapshot(path: Path, logical_id: str) -> tuple[dict[str, str], str] | None:
    value = state_api._read_json(path, max_bytes=constants_api.MAX_HOST_STATIC_MANIFEST_BYTES)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "version", "logical_id", "files", "rules_digest"
    }:
        raise errors_api.MonitorError("host Codex static snapshot has an invalid shape")
    if value["version"] != 1 or value["logical_id"] != logical_id:
        raise errors_api.MonitorError("host Codex static snapshot identity is invalid")
    raw_files = value["files"]
    if not isinstance(raw_files, dict) or any(
        not isinstance(name, str)
        or not _static_file_name(name)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in raw_files.items()
    ):
        raise errors_api.MonitorError("host Codex static snapshot files are invalid")
    rules_digest = value["rules_digest"]
    if not isinstance(rules_digest, str) or (
        rules_digest and not re.fullmatch(r"[0-9a-f]{64}", rules_digest)
    ):
        raise errors_api.MonitorError("host Codex static snapshot rules are invalid")
    return dict(raw_files), rules_digest


def _replace_managed_rules(home: Path, files: dict[str, bytes]) -> None:
    destination = home / "rules"
    try:
        existing = os.lstat(destination)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise errors_api.MonitorError("cannot inspect managed Codex rules") from exc
    if existing is not None:
        if (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISDIR(existing.st_mode)
            or existing.st_uid != os.getuid()
        ):
            raise errors_api.MonitorError("unsafe managed Codex rules directory")
        state_api._remove_owned_directory(destination, description="managed Codex rules")
    if not files:
        return
    stage = Path(tempfile.mkdtemp(prefix=".rules.", dir=home))
    try:
        state_api._ensure_private_directory(stage)
        staged_rules = stage / "rules"
        state_api._ensure_private_directory(staged_rules)
        for relative, data in files.items():
            parts = Path(relative).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise errors_api.MonitorError("host Codex rules contain an unsafe destination")
            target = staged_rules.joinpath(*parts)
            state_api._ensure_private_directory(target.parent)
            _write_private_bytes(target, data)
        os.replace(staged_rules, destination)
        stage.rmdir()
    except OSError as exc:
        raise errors_api.MonitorError("cannot install managed Codex rules") from exc
    finally:
        try:
            if stage.exists():
                state_api._remove_owned_directory(stage, description="managed Codex rules staging")
        except errors_api.MonitorError:
            pass


def _synchronize_host_static_source(
    config_root: Path,
    record: models_api.VolumeRegistration,
    source: Path,
    home: Path,
) -> None:
    files, rules = _read_host_static_source(source)
    _root, _home, manifest_path = _host_source_paths(config_root, record)
    previous = _load_host_static_snapshot(manifest_path, record.logical_id)
    old_files, _old_rules = previous if previous is not None else ({}, "")
    current_files = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    for name in sorted(set(old_files).difference(current_files)):
        _remove_private_regular(home / name, maximum_bytes=constants_api.MAX_HOST_STATIC_BYTES)
    # The managed home is a session boundary, not a second user configuration
    # authority.  Copy the approved source configuration on every launch so
    # the pre-launch MCP inventory and the configuration Codex receives cannot
    # diverge after a prior process changed its private home.
    for name, data in files.items():
        _write_private_bytes(home / name, data)
    rules_digest = _static_digest(rules) if rules else ""
    _replace_managed_rules(home, rules)
    state_api._write_json(
        manifest_path,
        {
            "version": 1,
            "logical_id": record.logical_id,
            "files": current_files,
            "rules_digest": rules_digest,
        },
    )


def _synchronize_host_file(
    source: Path,
    home: Path,
    name: str,
    *,
    enabled: bool,
) -> tuple[str, bool]:
    destination = home / name
    if not enabled:
        _remove_private_regular(destination, maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES)
        return "", False
    data = _read_host_regular(
        source / name,
        label="host Codex credential",
        maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES,
    )
    if data is None:
        _remove_private_regular(destination, maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES)
        return "", True
    _write_private_bytes(destination, data)
    return hashlib.sha256(data).hexdigest(), True


def _write_source_private_bytes(
    source: Path,
    name: str,
    value: bytes,
    *,
    expected_hash: str,
    expected_source_identity: tuple[int, int],
) -> None:
    """Write one changed credential back only after a source-wins recheck.

    The caller has already compared the source to its session baseline.  Read
    it again through the destination directory descriptor immediately before
    replacement: a direct host Codex process or a second tool can otherwise
    change the source in the narrow interval between that first comparison and
    ``os.replace``.  A directory identity check also prevents a source-root
    replacement from receiving a stale managed credential.
    """

    if name not in constants_api.HOST_SOURCE_CREDENTIAL_FILES:
        raise errors_api.MonitorError("unsafe host Codex credential name")
    if (
        not isinstance(value, bytes)
        or not isinstance(expected_hash, str)
        or (expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash))
        or not isinstance(expected_source_identity, tuple)
        or len(expected_source_identity) != 2
        or any(type(item) is not int or item < 0 for item in expected_source_identity)
    ):
        raise errors_api.MonitorError("host Codex credential write-back state is invalid")
    verified, before = _checked_host_source_directory(source)
    if (before.st_dev, before.st_ino) != expected_source_identity:
        raise errors_api.MonitorError("host Codex source changed; source was preserved")
    try:
        directory_fd = os.open(
            verified,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise errors_api.MonitorError("cannot safely open host Codex credential directory") from exc
    temporary = f".{name.lstrip('.')}.cage-{secrets.token_hex(16)}"
    descriptor = -1
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != expected_source_identity:
            raise errors_api.MonitorError("host Codex source changed before credential write-back")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        # Keep this recheck immediately adjacent to the replacement.  If the
        # path now names another directory, the descriptor still identifies
        # the original one and we leave the new source untouched.
        current_source = os.lstat(verified)
        if (
            stat.S_ISLNK(current_source.st_mode)
            or not stat.S_ISDIR(current_source.st_mode)
            or (current_source.st_dev, current_source.st_ino)
            != expected_source_identity
        ):
            raise errors_api.MonitorError("host Codex source changed; source was preserved")
        current = _read_host_regular_at(
            directory_fd,
            name,
            label="host Codex credential",
            maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES,
        )
        current_hash = hashlib.sha256(current).hexdigest() if current is not None else ""
        if not hmac.compare_digest(current_hash, expected_hash):
            raise errors_api.MonitorError(
                "host Codex credentials changed outside this Cage session; source was preserved"
            )
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise errors_api.MonitorError("cannot write back host Codex credentials") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def prepare_host_source(
    config_root: Path,
    record: models_api.VolumeRegistration,
    source_home: Path | str,
    *,
    copy_auth: bool,
    copy_oauth_credentials: bool,
) -> models_api.HostSourceSession:
    """Refresh allowed input and return the isolated home for one Cage launch."""

    if type(copy_auth) is not bool or type(copy_oauth_credentials) is not bool:
        raise errors_api.MonitorError("host Codex source copy policy is invalid")
    source, source_info = _checked_host_source_directory(source_home)
    logical_id, fingerprint = _host_source_identity_from_checked(
        config_root, source, source_info
    )
    if (
        record.target != "host"
        or record.logical_id != logical_id
        or record.repository != _host_source_repository(logical_id)
        or record.fingerprint != fingerprint
    ):
        raise errors_api.MonitorError("host Codex source changed; explicit monitor adoption is required")
    home = _ensure_managed_host_home(config_root, record)
    _synchronize_host_static_source(config_root, record, source, home)
    auth_baseline, sync_auth = _synchronize_host_file(
        source,
        home,
        "auth.json",
        enabled=copy_auth,
    )
    baseline, enabled = _synchronize_host_file(
        source,
        home,
        ".credentials.json",
        enabled=copy_oauth_credentials,
    )
    checked_source, checked_info = _checked_host_source_directory(source)
    source_identity = (source_info.st_dev, source_info.st_ino)
    if (
        checked_source != source
        or (checked_info.st_dev, checked_info.st_ino) != source_identity
    ):
        raise errors_api.MonitorError("host Codex source changed during session preparation")
    return models_api.HostSourceSession(
        record=record,
        source_home=source,
        codex_home=home,
        source_identity=source_identity,
        auth_baseline=auth_baseline,
        sync_auth=sync_auth,
        credential_baseline=baseline,
        sync_oauth_credentials=enabled,
    )


def _finish_host_credential(
    session: models_api.HostSourceSession,
    *,
    name: str,
    baseline: str,
    enabled: bool,
    label: str,
) -> None:
    """Preserve one changed credential unless the independent source won."""

    if not enabled:
        return
    source = _read_host_regular(
        session.source_home / name,
        label=f"host Codex {label}",
        maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES,
    )
    managed = _read_host_regular(
        session.codex_home / name,
        label=f"managed Codex {label}",
        maximum_bytes=constants_api.MAX_HOST_CREDENTIAL_BYTES,
    )
    source_hash = hashlib.sha256(source).hexdigest() if source is not None else ""
    managed_hash = hashlib.sha256(managed).hexdigest() if managed is not None else ""
    if not hmac.compare_digest(source_hash, baseline):
        raise errors_api.MonitorError(
            f"host Codex {label} changed outside this Cage session; source was preserved"
        )
    if hmac.compare_digest(managed_hash, baseline):
        return
    if managed is None:
        raise errors_api.MonitorError(f"managed Codex {label} disappeared; source was preserved")
    _write_source_private_bytes(
        session.source_home,
        name,
        managed,
        expected_hash=baseline,
        expected_source_identity=session.source_identity,
    )


def finish_host_source(session: models_api.HostSourceSession) -> None:
    """Conditionally preserve changed host auth and selected OAuth credentials.

    A source changed outside this Cage session wins.  We never use a managed
    copy to overwrite it, and a disappeared managed credential never deletes
    the source credential automatically.  Handle the two independent stores
    separately so a conflict in one cannot discard a valid rotation in the
    other.
    """

    if not isinstance(session, models_api.HostSourceSession):
        return
    failures: list[str] = []
    for name, baseline, enabled, label in (
        ("auth.json", session.auth_baseline, session.sync_auth, "auth state"),
        (
            ".credentials.json",
            session.credential_baseline,
            session.sync_oauth_credentials,
            "OAuth credentials",
        ),
    ):
        try:
            _finish_host_credential(
                session,
                name=name,
                baseline=baseline,
                enabled=enabled,
                label=label,
            )
        except errors_api.MonitorError as exc:
            failures.append(str(exc))
    if failures:
        raise errors_api.MonitorError("; ".join(failures))
