#!/usr/bin/env python3
"""Lifecycle manager for ChatGPT Desktop SSH-backed Cage targets.

The public process performs small, explicit mutations under Cage's private
configuration directory and ~/.ssh.  A detached supervisor owns the ordinary
Cage launcher process, which in turn owns Docker, Netgate, bridges, and OAuth
cleanup.  The SSH ProxyCommand never opens a listening socket: it starts one
OpenSSH inetd-mode connection inside the already-running container.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_DESKTOP_TARGETS = 256
MAX_PRESET_CHARS = 128
MAX_REPO_CHARS = 4096
TARGET_ID_RE = re.compile(r"^[0-9a-f]{16}$")
PRESET_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ALIAS_COMPONENT_RE = re.compile(r"[^A-Za-z0-9-]+")
SETUP_VERSION = 1
STATE_VERSION = 1
LIST_FORMAT_VERSION = 1


class DesktopError(RuntimeError):
    pass


def die(message: str) -> None:
    raise DesktopError(message)


def read_bounded(path: Path, *, missing_ok: bool = False) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return b""
        raise DesktopError(f"missing file: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise DesktopError(f"expected a regular file: {path}")
    if before.st_size > MAX_FILE_BYTES:
        raise DesktopError(f"file is too large: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identity = lambda value: (value.st_dev, value.st_ino)
        if identity(before) != identity(opened) or identity(opened) != identity(current):
            raise DesktopError(f"file changed while it was opened: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise DesktopError(f"file is too large: {path}")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > MAX_FILE_BYTES:
        raise DesktopError(f"file is too large: {path}")
    return data


def secure_directory(path: Path, mode: int = 0o700) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise DesktopError(f"refusing symlinked private directory: {current}")
            if not stat.S_ISDIR(info.st_mode):
                raise DesktopError(f"private path is not a directory: {current}")
        else:
            current.mkdir(mode=mode)
        if current == path:
            current.chmod(mode)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def update_in_place(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Update an already-created bind source without changing its inode."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise DesktopError(f"expected a regular file: {path}")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        identity = lambda value: (value.st_dev, value.st_ino)
        if identity(before) != identity(opened) or identity(opened) != identity(current):
            raise DesktopError(f"file changed while it was opened: {path}")
        os.fchmod(descriptor, mode)
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write(
        path,
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode,
    )


def read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    if missing_ok and not path.exists():
        return None
    try:
        value = json.loads(read_bounded(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopError(f"invalid JSON state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DesktopError(f"invalid JSON state object: {path}")
    return value


def canonical_repo(raw: str) -> Path:
    if (
        len(raw) > MAX_REPO_CHARS
        or any(character in raw for character in "\0\r\n")
    ):
        raise DesktopError("repository path is invalid or too long")
    path = Path(raw).expanduser().resolve(strict=True)
    if (
        len(str(path)) > MAX_REPO_CHARS
        or any(character in str(path) for character in "\0\r\n")
    ):
        raise DesktopError("repository path is invalid or too long")
    if not path.is_dir():
        raise DesktopError(f"repository is not a directory: {path}")
    return path


def validate_preset(name: str) -> str:
    if len(name) > MAX_PRESET_CHARS or not PRESET_RE.fullmatch(name):
        raise DesktopError(f"invalid preset name: {name!r}")
    return name


def target_id(repo: Path, preset: str) -> str:
    material = f"{repo}\0{preset}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def alias_component(value: str, limit: int) -> str:
    value = ALIAS_COMPONENT_RE.sub("-", value).strip("-").lower()
    value = value or "target"
    return value[:limit].rstrip("-")


def target_alias(repo: Path, preset: str, identifier: str) -> str:
    return (
        f"cage-{alias_component(preset, 24)}-"
        f"{alias_component(repo.name, 20)}-{identifier[:8]}"
    )


def config_root(args: argparse.Namespace) -> Path:
    return Path(args.config_dir).expanduser().resolve()


def desktop_root(args: argparse.Namespace) -> Path:
    return config_root(args) / "desktop"


def targets_root(args: argparse.Namespace) -> Path:
    return desktop_root(args) / "targets"


def target_root(args: argparse.Namespace, identifier: str) -> Path:
    if not TARGET_ID_RE.fullmatch(identifier):
        raise DesktopError("invalid desktop target id")
    return targets_root(args) / identifier


def setup_path(args: argparse.Namespace) -> Path:
    return desktop_root(args) / "setup.json"


def ssh_include_path(args: argparse.Namespace) -> Path:
    return desktop_root(args) / "ssh_config"


def metadata_path(root: Path) -> Path:
    return root / "metadata.json"


def runtime_path(root: Path) -> Path:
    return root / "runtime.json"


def ready_path(root: Path) -> Path:
    return root / "ready.json"


def socket_path(root: Path) -> Path:
    return root / "control.sock"


def heartbeat_path(root: Path) -> Path:
    return root / "heartbeat"


def log_path(root: Path) -> Path:
    return root / "supervisor.log"


def operation_lock_path(args: argparse.Namespace, identifier: str) -> Path:
    if not TARGET_ID_RE.fullmatch(identifier):
        raise DesktopError("invalid desktop target id")
    return desktop_root(args) / "locks" / f"{identifier}.lock"


def current_version(launcher: Path) -> str:
    result = subprocess.run(
        [str(launcher), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise DesktopError(
            f"cannot execute Cage launcher {launcher}: {result.stderr.strip()}"
        )
    match = re.fullmatch(r"cage ([0-9A-Za-z.+-]+)\s*", result.stdout)
    if not match:
        raise DesktopError(f"unexpected Cage version output from {launcher}")
    return match.group(1)


def installed_launcher(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, str]:
    running = Path(args.launcher).resolve(strict=True)
    running_version = current_version(running)
    candidate_text = shutil.which("cage")
    if not candidate_text:
        raise DesktopError(
            "desktop setup requires an installed Cage launcher in PATH; "
            "run the source installer first"
        )
    candidate = Path(candidate_text).absolute()
    resolved = candidate.resolve(strict=True)
    if current_version(candidate) != running_version:
        raise DesktopError(
            "the installed Cage launcher is not this source version; "
            "install the current source before desktop setup"
        )
    marker = resolved.parent / ".cage-install"
    if (
        not marker.is_file()
        or marker.is_symlink()
        or read_bounded(marker).decode("utf-8").strip() != running_version
    ):
        raise DesktopError(
            "desktop setup requires Cage to be installed outside the source tree; "
            "run the source installer first"
        )
    helper = resolved.parent / "cage-desktop.py"
    if not helper.is_file() or helper.is_symlink():
        raise DesktopError(f"installed desktop helper is missing or unsafe: {helper}")
    docker_text = shutil.which("docker")
    if not docker_text:
        raise DesktopError("docker is required for Cage desktop targets")
    docker = Path(docker_text).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    return candidate, resolved, helper, docker, running_version


@contextlib.contextmanager
def private_lock(path: Path):
    secure_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ssh_config_target(path: Path) -> tuple[Path, os.stat_result | None]:
    try:
        lexical = os.lstat(path)
    except FileNotFoundError:
        return path, None
    if stat.S_ISLNK(lexical.st_mode):
        destination = path.resolve(strict=True)
        target_info = os.lstat(destination)
        if not stat.S_ISREG(target_info.st_mode):
            raise DesktopError(f"SSH config symlink target is not regular: {destination}")
        return destination, lexical
    if not stat.S_ISREG(lexical.st_mode):
        raise DesktopError(f"SSH config is not a regular file: {path}")
    return path, None


def backup_ssh_config(root: Path, data: bytes) -> None:
    backup_root = root / "backups"
    secure_directory(backup_root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = backup_root / f"ssh-config-{stamp}-{time.time_ns()}.bak"
    atomic_write(destination, data, 0o600)
    backups = sorted(backup_root.glob("ssh-config-*.bak"), reverse=True)
    for old in backups[10:]:
        old.unlink()


def ensure_ssh_include(args: argparse.Namespace) -> None:
    home = Path.home().resolve()
    ssh_dir = home / ".ssh"
    if ssh_dir.exists() and ssh_dir.is_symlink():
        raise DesktopError(f"refusing symlinked SSH directory: {ssh_dir}")
    secure_directory(ssh_dir)
    config = ssh_dir / "config"
    include = ssh_include_path(args)
    include_line = f"Include {include}\n"
    lock = ssh_dir / ".cage-desktop-config.lock"
    with private_lock(lock):
        destination, symlink_before = ssh_config_target(config)
        original = read_bounded(destination, missing_ok=True)
        text = original.decode("utf-8")
        exact = re.compile(
            rf"(?m)^\s*Include\s+{re.escape(str(include))}\s*$"
        )
        if exact.search(text):
            return
        backup_ssh_config(desktop_root(args), original)
        updated = include_line.encode() + original
        mode = 0o600
        if destination.exists():
            mode = stat.S_IMODE(os.lstat(destination).st_mode)
        atomic_write(destination, updated, mode)
        if symlink_before is not None:
            after = os.lstat(config)
            if (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
            ) != (
                symlink_before.st_dev,
                symlink_before.st_ino,
                symlink_before.st_mtime_ns,
            ):
                raise DesktopError("SSH config symlink changed during setup")


def command_setup(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        raise DesktopError("Cage desktop targets are currently supported only on macOS")
    root = desktop_root(args)
    secure_directory(root)
    launcher, resolved, helper, docker, version = installed_launcher(args)
    setup = {
        "version": SETUP_VERSION,
        "cage_version": version,
        "launcher": str(launcher),
        "launcher_resolved": str(resolved),
        "helper": str(helper),
        "python": str(Path(sys.executable).resolve(strict=True)),
        "docker": str(docker),
        "ssh_include": str(ssh_include_path(args)),
    }
    include = ssh_include_path(args)
    if include.exists() or include.is_symlink():
        info = os.lstat(include)
        if not stat.S_ISREG(info.st_mode):
            raise DesktopError(f"desktop SSH include is unsafe: {include}")
    else:
        atomic_write(include, b"", 0o600)
    ensure_ssh_include(args)
    write_json(setup_path(args), setup)
    print(f"Cage desktop SSH setup is ready: {ssh_include_path(args)}")
    return 0


def load_setup(args: argparse.Namespace, *, ensure: bool = False) -> dict[str, Any]:
    if ensure and not setup_path(args).exists():
        command_setup(args)
    setup = read_json(setup_path(args))
    assert setup is not None
    if setup.get("version") != SETUP_VERSION:
        raise DesktopError("desktop setup is from an unsupported Cage version; rerun setup")
    required = ("launcher", "launcher_resolved", "helper", "python", "docker")
    for key in required:
        value = setup.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise DesktopError(f"desktop setup has an invalid {key}")
    for key in ("launcher_resolved", "helper", "python", "docker"):
        path = Path(str(setup[key]))
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise DesktopError(f"desktop setup {key} is no longer a safe regular file")
    resolved_launcher = Path(str(setup["launcher_resolved"]))
    helper = Path(str(setup["helper"]))
    if helper.parent != resolved_launcher.parent:
        raise DesktopError("desktop helper is no longer beside the installed launcher")
    installed_version = current_version(Path(setup["launcher"]))
    marker = resolved_launcher.parent / ".cage-install"
    if (
        not marker.is_file()
        or marker.is_symlink()
        or read_bounded(marker).decode("utf-8").strip() != installed_version
    ):
        raise DesktopError("desktop installation marker is missing or stale; rerun setup")
    if installed_version != current_version(Path(args.launcher)):
        raise DesktopError("desktop setup is stale; install this Cage version and rerun setup")
    return setup


def metadata_entries(args: argparse.Namespace) -> list[dict[str, Any]]:
    root = targets_root(args)
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        child_info = os.lstat(child)
        if not stat.S_ISDIR(child_info.st_mode) or not TARGET_ID_RE.fullmatch(child.name):
            continue
        value = read_json(metadata_path(child), missing_ok=True)
        if value:
            if value.get("target_id") != child.name:
                raise DesktopError(
                    f"desktop target metadata does not match its directory: {child}"
                )
            entries.append(value)
            if len(entries) > MAX_DESKTOP_TARGETS:
                raise DesktopError(
                    f"too many registered desktop targets (maximum {MAX_DESKTOP_TARGETS})"
                )
    return entries


def base_ssh_alias_conflict(alias: str) -> bool:
    """Find a concrete alias in the user's config or recursively included files."""
    ssh_root = Path.home().resolve() / ".ssh"
    pending = [ssh_root / "config"]
    visited: set[Path] = set()
    alias_folded = alias.casefold()
    while pending:
        candidate = pending.pop()
        try:
            destination, _ = ssh_config_target(candidate)
        except FileNotFoundError:
            continue
        if not destination.exists():
            continue
        resolved = destination.resolve(strict=True)
        if resolved in visited:
            continue
        visited.add(resolved)
        if len(visited) > 256:
            raise DesktopError("SSH configuration includes too many files")
        text = read_bounded(resolved).decode("utf-8")
        for raw_line in text.splitlines():
            try:
                fields = shlex.split(raw_line, comments=True, posix=True)
            except ValueError as exc:
                raise DesktopError(
                    f"cannot parse SSH configuration line in {resolved}: {exc}"
                ) from exc
            if not fields:
                continue
            keyword = fields[0].casefold()
            if keyword == "host":
                if any(
                    not name.startswith("!") and name.casefold() == alias_folded
                    for name in fields[1:]
                ):
                    return True
            elif keyword == "include":
                for pattern in fields[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = ssh_root / expanded
                    for included in glob.glob(str(expanded), recursive=False):
                        pending.append(Path(included))
    return False


def quote_ssh_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise DesktopError("unsafe newline in generated SSH configuration")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_ssh_include(args: argparse.Namespace, setup: dict[str, Any]) -> bytes:
    blocks: list[str] = [
        "# Managed by Cage. Use `cage desktop remove` instead of editing this file.\n"
    ]
    python = setup["python"]
    helper = setup["helper"]
    for item in sorted(metadata_entries(args), key=lambda value: str(value["alias"])):
        alias = str(item["alias"])
        identifier = str(item["target_id"])
        key = str(item["private_key"])
        known_hosts = str(item["known_hosts"])
        command = " ".join(
            quote_ssh_value(value)
            for value in (
                python,
                "-I",
                helper,
                "--config-dir",
                str(config_root(args)),
                "--launcher",
                setup["launcher_resolved"],
                "proxy",
                identifier,
            )
        )
        blocks.append(
            "\n".join(
                (
                    f"Host {alias}",
                    f"  HostName {alias}",
                    "  User codex",
                    f"  IdentityFile {quote_ssh_value(key)}",
                    "  IdentitiesOnly yes",
                    "  BatchMode yes",
                    "  StrictHostKeyChecking yes",
                    f"  UserKnownHostsFile {quote_ssh_value(known_hosts)}",
                    f"  HostKeyAlias {alias}",
                    f"  ProxyCommand {command}",
                    "",
                )
            )
        )
    return "\n".join(blocks).encode("utf-8")


def rebuild_ssh_include(args: argparse.Namespace, setup: dict[str, Any]) -> None:
    path = ssh_include_path(args)
    with private_lock(desktop_root(args) / ".ssh-include.lock"):
        atomic_write(path, render_ssh_include(args, setup), 0o600)


def generate_client_key(root: Path) -> tuple[Path, Path]:
    private = root / "id_ed25519"
    public = root / "id_ed25519.pub"
    if private.exists() or public.exists():
        if not private.is_file() or private.is_symlink():
            raise DesktopError(f"unsafe desktop private key: {private}")
        if not public.is_file() or public.is_symlink():
            raise DesktopError(f"unsafe desktop public key: {public}")
        private.chmod(0o600)
        public.chmod(0o600)
        return private, public
    result = subprocess.run(
        [
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "cage-desktop",
            "-f",
            str(private),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DesktopError(f"ssh-keygen failed: {result.stderr.strip()}")
    private.chmod(0o600)
    public.chmod(0o600)
    return private, public


def validate_client_key(
    root: Path, metadata: dict[str, Any], private: Path, public: Path
) -> None:
    if private != root / "id_ed25519" or public != root / "id_ed25519.pub":
        raise DesktopError("desktop SSH key paths do not match the target directory")
    for path in (private, public):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise DesktopError(f"unsafe desktop SSH key: {path}")
    if stat.S_IMODE(os.lstat(private).st_mode) & 0o077:
        raise DesktopError(f"desktop private key permissions are too broad: {private}")
    derived = subprocess.run(
        ["/usr/bin/ssh-keygen", "-y", "-f", str(private)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if derived.returncode != 0:
        raise DesktopError(f"desktop private key is invalid: {derived.stderr.strip()}")
    public_fields = read_bounded(public).decode("utf-8").strip().split()
    derived_fields = derived.stdout.strip().split()
    if len(public_fields) < 2 or derived_fields[:2] != public_fields[:2]:
        raise DesktopError("desktop SSH public and private keys do not match")
    expected = metadata.get("client_public_key_sha256")
    actual = hashlib.sha256(read_bounded(public)).hexdigest()
    if expected is not None and expected != actual:
        raise DesktopError(
            "desktop client key changed unexpectedly; remove the target explicitly "
            "before generating a replacement"
        )


def resolved_preset_assignments(
    setup: dict[str, Any], args: argparse.Namespace, repo: Path, preset: str
) -> dict[str, str]:
    """Resolve a preset through the versioned JSON contract.

    The compatibility-shaped return value keeps fingerprint callers small
    while eliminating parsing of executable shell assignments.
    """

    config_helper = Path(str(setup["helper"])).with_name("cage-config.py")
    if not config_helper.is_file() or config_helper.is_symlink():
        raise DesktopError(
            f"installed Cage configuration helper is missing or unsafe: {config_helper}"
        )
    result = subprocess.run(
        [
            str(setup["python"]),
            "-I",
            str(config_helper),
            "--config",
            str(config_root(args) / "config.toml"),
            "resolve-json",
            "--repo",
            str(repo),
            "--preset",
            preset,
            "--tool",
            "codex",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise DesktopError(
            f"cannot resolve desktop preset fingerprint: {result.stderr.strip()}"
        )
    try:
        contract = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DesktopError("invalid JSON from installed Cage config helper") from exc
    if not isinstance(contract, dict):
        raise DesktopError("invalid output from installed Cage config helper")
    if set(contract) != {
        "schema",
        "schema_version",
        "cage_version",
        "resolved_config",
    }:
        raise DesktopError("unexpected fields in installed Cage config contract")
    if (
        contract.get("schema") != "cage.resolved-config"
        or contract.get("schema_version") != 1
    ):
        raise DesktopError("unsupported installed Cage config contract")
    resolved = contract.get("resolved_config")
    if not isinstance(resolved, dict):
        raise DesktopError("installed Cage config contract is missing resolved_config")
    auth = resolved.get("auth")
    mcp = resolved.get("mcp")
    skills = resolved.get("skills")
    if not isinstance(auth, dict) or not isinstance(mcp, dict) or not isinstance(skills, dict):
        raise DesktopError("invalid installed Cage config contract payload")
    payload = {
        "profile": resolved.get("codex_profile", ""),
        "stdio": mcp.get("stdio", []),
        "remote": mcp.get("remote", []),
        "skills": skills.get("mounts", []),
        "env_names": resolved.get("environment_names", []),
        "disable_mcp": mcp.get("suppressed", []),
        "disable_mcp_overrides": mcp.get("disable_overrides", []),
    }
    return {
        "CAGE_NET_MODE": str(resolved.get("network", "")),
        "CAGE_YOLO": "1" if resolved.get("yolo") is True else "",
        "CAGE_CODEX_PROFILE": str(resolved.get("codex_profile", "")),
        "HOST_CODEX_DIR": str(auth.get("host_codex_dir", "")),
        "HOST_AGENTS_DIR": str(auth.get("host_agents_dir", "")),
        "CAGE_HOST_CODEX_PAYLOAD": json.dumps(
            payload, ensure_ascii=True, separators=(",", ":")
        ),
    }


def update_tree_metadata(digest: Any, root: Path) -> None:
    """Fingerprint copied skill state without reading arbitrary skill payloads."""
    if not root.exists():
        digest.update(b"missing\0")
        return
    entries = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        entries += 1
        if entries > 50_000:
            raise DesktopError(f"desktop fingerprint source has too many entries: {root}")
        info = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(
            f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}".encode("ascii")
        )
        digest.update(b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
            digest.update(b"\0")


def config_fingerprint(
    args: argparse.Namespace,
    setup: dict[str, Any],
    repo: Path,
    preset: str,
) -> str:
    assignments = resolved_preset_assignments(setup, args, repo, preset)
    effective_net = getattr(args, "net", "") or assignments.get(
        "CAGE_NET_MODE", "open"
    )
    requested_yolo = getattr(args, "yolo", None)
    effective_yolo = (
        requested_yolo
        if requested_yolo is not None
        else assignments.get("CAGE_YOLO", "") == "1"
    )
    digest = hashlib.sha256()
    digest.update(str(repo).encode())
    digest.update(b"\0")
    digest.update(preset.encode())
    digest.update(b"\0")
    digest.update(
        json.dumps(
            {
                "net": effective_net,
                "yolo": effective_yolo,
                "profile": assignments.get("CAGE_CODEX_PROFILE", ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    config = config_root(args) / "config.toml"
    digest.update(read_bounded(config))
    digest.update(current_version(Path(args.launcher)).encode())

    host_codex_text = assignments.get("HOST_CODEX_DIR", "")
    if host_codex_text:
        host_codex = Path(host_codex_text).expanduser()
        static_files = [
            host_codex / "config.toml",
            host_codex / "AGENTS.md",
            host_codex / "AGENTS.override.md",
            host_codex / "hooks.json",
            *sorted(host_codex.glob("*.config.toml")),
        ]
        seen: set[Path] = set()
        for path in static_files:
            if path in seen or not path.exists():
                continue
            seen.add(path)
            digest.update(str(path.name).encode())
            digest.update(b"\0")
            digest.update(read_bounded(path))
        update_tree_metadata(digest, host_codex / "rules")

    payload_text = assignments.get("CAGE_HOST_CODEX_PAYLOAD", "")
    if payload_text:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise DesktopError("invalid resolved host Codex fingerprint payload") from exc
        if not isinstance(payload, dict):
            raise DesktopError("invalid resolved host Codex fingerprint payload")
        skills = payload.get("skills", [])
        if not isinstance(skills, list):
            raise DesktopError("invalid resolved skill fingerprint payload")
        for item in skills:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise DesktopError("invalid resolved skill fingerprint entry")
            digest.update(str(item.get("name", "")).encode())
            digest.update(b"\0")
            update_tree_metadata(digest, Path(item["path"]).expanduser())
        if not skills:
            host_agents = assignments.get("HOST_AGENTS_DIR", "")
            if host_agents:
                update_tree_metadata(digest, Path(host_agents).expanduser())
        env_names = payload.get("env_names", [])
        if not isinstance(env_names, list) or not all(
            isinstance(name, str) for name in env_names
        ):
            raise DesktopError("invalid resolved environment fingerprint payload")
        for name in sorted(set(env_names) | {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"}):
            digest.update(name.encode())
            digest.update(b"=")
            value = os.environ.get(name)
            digest.update(b"unset" if value is None else hashlib.sha256(value.encode()).digest())
            digest.update(b"\0")
    return digest.hexdigest()


def runtime_status(root: Path) -> dict[str, Any]:
    value = read_json(runtime_path(root), missing_ok=True)
    if not value:
        return {"status": "stopped"}
    return value


def live_runtime_status(root: Path) -> dict[str, Any]:
    """Return current state, marking an unreachable running target stale."""
    runtime = runtime_status(root)
    status = runtime.get("status")
    if status in {"starting", "ready", "stopping"}:
        try:
            runtime = control_request(root, "status")
        except DesktopError:
            transition_key = "started_at" if status == "starting" else "stopping_at"
            transition_at = runtime.get(transition_key)
            transition_age = (
                time.time() - transition_at
                if isinstance(transition_at, int) and not isinstance(transition_at, bool)
                else None
            )
            if (
                status != "ready"
                and transition_age is not None
                and 0 <= transition_age < 30
            ):
                return runtime
            runtime = merge_runtime(
                root,
                {"status": "stale", "container_id": None},
            )
    return runtime


def public_target_summary(
    args: argparse.Namespace, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Build the bounded, non-secret target shape consumed by the TUI."""
    identifier = metadata.get("target_id")
    preset = metadata.get("preset")
    repo = metadata.get("repo")
    alias = metadata.get("alias")
    volume = metadata.get("volume_name")
    if not isinstance(identifier, str) or not TARGET_ID_RE.fullmatch(identifier):
        raise DesktopError("desktop target metadata has an invalid target id")
    if (
        not isinstance(preset, str)
        or len(preset) > MAX_PRESET_CHARS
        or not PRESET_RE.fullmatch(preset)
    ):
        raise DesktopError(f"desktop target {identifier} has an invalid preset")
    if (
        not isinstance(repo, str)
        or len(repo) > MAX_REPO_CHARS
        or not Path(repo).is_absolute()
        or any(character in repo for character in "\0\r\n")
    ):
        raise DesktopError(f"desktop target {identifier} has an invalid repository")
    if target_id(Path(repo), preset) != identifier:
        raise DesktopError(f"desktop target {identifier} has an invalid identity")
    expected_alias = target_alias(Path(repo), preset, identifier)
    if alias != expected_alias:
        raise DesktopError(f"desktop target {identifier} has an invalid SSH alias")
    expected_volume = f"cage-codex-desktop-{identifier}"
    if volume != expected_volume:
        raise DesktopError(f"desktop target {identifier} has an invalid volume")
    runtime = live_runtime_status(target_root(args, identifier))
    status = runtime.get("status", "stopped")
    if status not in {"starting", "ready", "stopping", "stopped", "stale", "failed"}:
        status = "unknown"
    summary: dict[str, Any] = {
        "target_id": identifier,
        "alias": alias,
        "preset": preset,
        "repo": repo,
        "status": status,
        "volume_name": volume,
    }
    for key in ("started_at", "ready_at", "stopped_at", "exit_code"):
        value = runtime.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            summary[key] = value
    container_id = runtime.get("container_id")
    if isinstance(container_id, str) and re.fullmatch(r"[0-9a-f]{12,64}", container_id):
        summary["container_id"] = container_id
    return summary


def control_request(root: Path, command: str, timeout: float = 5.0) -> dict[str, Any]:
    path = socket_path(root)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall((command + "\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 1024 * 1024:
                raise DesktopError("desktop supervisor response is too large")
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise DesktopError(f"desktop supervisor is unavailable: {exc}") from exc
    finally:
        client.close()
    try:
        value = json.loads(b"".join(chunks).decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopError("desktop supervisor returned invalid state") from exc
    if not isinstance(value, dict):
        raise DesktopError("desktop supervisor returned invalid state")
    return value


def open_chatgpt() -> None:
    app = Path("/Applications/ChatGPT.app")
    legacy = Path("/Applications/Codex.app")
    selected = app if app.is_dir() else legacy
    if not selected.is_dir():
        raise DesktopError("ChatGPT desktop app is not installed under /Applications")
    result = subprocess.run(
        ["/usr/bin/open", str(selected)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DesktopError(f"could not open ChatGPT: {result.stderr.strip()}")


def ensure_target_metadata(
    args: argparse.Namespace, repo: Path, preset: str, setup: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    identifier = target_id(repo, preset)
    root = target_root(args, identifier)
    secure_directory(root)
    alias = target_alias(repo, preset, identifier)
    existing = read_json(metadata_path(root), missing_ok=True)
    if existing:
        if existing.get("repo") != str(repo) or existing.get("preset") != preset:
            raise DesktopError("desktop target hash collision")
        private = Path(str(existing.get("private_key", "")))
        public = Path(str(existing.get("public_key", "")))
        validate_client_key(root, existing, private, public)
        return root, existing
    if base_ssh_alias_conflict(alias):
        raise DesktopError(f"SSH alias already exists outside Cage: {alias}")
    private, public = generate_client_key(root)
    known_hosts = root / "known_hosts"
    atomic_write(known_hosts, b"", 0o600)
    metadata = {
        "version": STATE_VERSION,
        "target_id": identifier,
        "alias": alias,
        "repo": str(repo),
        "preset": preset,
        "private_key": str(private),
        "public_key": str(public),
        "client_public_key_sha256": hashlib.sha256(read_bounded(public)).hexdigest(),
        "known_hosts": str(known_hosts),
        "container_name": f"cage-desktop-{identifier}",
        "volume_name": f"cage-codex-desktop-{identifier}",
    }
    write_json(metadata_path(root), metadata)
    validate_client_key(root, metadata, private, public)
    rebuild_ssh_include(args, setup)
    return root, metadata


def build_internal_command(
    setup: dict[str, Any],
    repo: Path,
    preset: str,
    args: argparse.Namespace,
) -> list[str]:
    command = [setup["launcher_resolved"], "--preset", preset, "--desktop"]
    if getattr(args, "net", ""):
        command += ["--net", args.net]
    if getattr(args, "yolo", None) is True:
        command.append("--yolo")
    elif getattr(args, "yolo", None) is False:
        command.append("--no-yolo")
    if getattr(args, "rebuild", False):
        command.append("--rebuild")
    command.append(str(repo))
    return command


def wait_until_ready(root: Path, timeout: float = 600.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runtime = runtime_status(root)
        status = runtime.get("status")
        if status == "ready":
            return runtime
        if status in {"failed", "stopped"} and runtime.get("exit_code") is not None:
            tail = ""
            with contextlib.suppress(Exception):
                tail = "\n".join(
                    log_path(root).read_text(encoding="utf-8").splitlines()[-30:]
                )
            raise DesktopError(
                f"desktop target failed to start (exit {runtime.get('exit_code')})"
                + (f":\n{tail}" if tail else "")
            )
        time.sleep(0.2)
    raise DesktopError("timed out waiting for the desktop target to become ready")


def update_known_hosts(
    args: argparse.Namespace,
    setup: dict[str, Any],
    metadata: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    container_id = runtime.get("container_id")
    if not isinstance(container_id, str) or not container_id:
        raise DesktopError("desktop runtime did not report a container id")
    result = subprocess.run(
        [
            setup["docker"],
            "exec",
            "--user",
            "root",
            container_id,
            "cat",
            "/home/codex/.codex/.cage-desktop/ssh_host_ed25519_key.pub",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise DesktopError(f"could not read desktop host key: {result.stderr.strip()}")
    fields = result.stdout.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise DesktopError("desktop container returned an invalid SSH host key")
    line = f"{metadata['alias']} {fields[0]} {fields[1]}\n"
    known_hosts = Path(metadata["known_hosts"])
    existing = read_bounded(known_hosts, missing_ok=True).decode().strip()
    if existing and existing != line.strip():
        raise DesktopError(
            "desktop SSH host key changed unexpectedly; remove the target explicitly "
            "before trusting a replacement"
        )
    atomic_write(known_hosts, line.encode(), 0o600)
    rebuild_ssh_include(args, setup)


def command_start(args: argparse.Namespace, *, restart: bool = False) -> int:
    root_hint = target_root(
        args,
        target_id(canonical_repo(args.repo), validate_preset(args.preset)),
    )
    with private_lock(operation_lock_path(args, root_hint.name)):
        return command_start_locked(args, restart=restart)


def recover_stale_target(
    setup: dict[str, Any], root: Path, metadata: dict[str, Any]
) -> None:
    """Remove only a container whose immutable Cage target label still matches."""
    docker = setup["docker"]
    name = str(metadata["container_name"])
    identifier = str(metadata["target_id"])
    inspect = subprocess.run(
        [
            docker,
            "inspect",
            "--format",
            '{{index .Config.Labels "com.sindycate.cage.desktop-target"}}',
            name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if inspect.returncode == 0:
        if inspect.stdout.strip() != identifier:
            raise DesktopError(
                f"container name {name!r} is occupied by a non-matching container"
            )
        removed = subprocess.run(
            [docker, "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        if removed.returncode != 0:
            raise DesktopError(
                f"could not remove stale desktop container: {removed.stderr.strip()}"
            )
    merge_runtime(
        root,
        {
            "status": "stopped",
            "container_id": None,
            "exit_code": None,
            "recovered_at": int(time.time()),
        },
    )
    for transient in (ready_path(root), socket_path(root)):
        with contextlib.suppress(FileNotFoundError):
            transient.unlink()


def command_start_locked(args: argparse.Namespace, *, restart: bool = False) -> int:
    if sys.platform != "darwin":
        raise DesktopError("Cage desktop targets are currently supported only on macOS")
    preset = validate_preset(args.preset)
    repo = canonical_repo(args.repo)
    setup = load_setup(args, ensure=True)
    for key in ("launcher_resolved", "helper", "python", "docker"):
        executable = Path(str(setup[key])).resolve(strict=True)
        if executable == repo or repo in executable.parents:
            raise DesktopError(
                f"desktop {key} is inside the writable repository: {executable}"
            )
    fingerprint = config_fingerprint(args, setup, repo, preset)
    # Resolve and fingerprint the saved preset before registering an alias,
    # key, or target metadata. Invalid/missing/Claude presets remain a complete
    # state no-op.
    root, metadata = ensure_target_metadata(args, repo, preset, setup)
    runtime = runtime_status(root)
    if runtime.get("status") in {"starting", "ready"}:
        supervisor_live = True
        try:
            live = control_request(root, "status")
            supervisor_live = live.get("status") in {"starting", "ready", "stopping"}
        except DesktopError:
            supervisor_live = False
        if not supervisor_live:
            recover_stale_target(setup, root, metadata)
            runtime = runtime_status(root)
        elif restart:
            command_stop_for_root(root)
        elif runtime.get("fingerprint") != fingerprint:
            raise DesktopError(
                "desktop target is running with different configuration; "
                "use `cage desktop restart`"
            )
        else:
            print(f"Desktop target is already ready: {metadata['alias']}")
            print(f"Repository: {repo}")
            if not args.no_open:
                open_chatgpt()
            return 0
    else:
        # Metadata can outlive a crash.  A label check makes stale recovery
        # safe without requiring the user to delete the persistent volume.
        recover_stale_target(setup, root, metadata)

    for transient in (ready_path(root), socket_path(root)):
        with contextlib.suppress(FileNotFoundError):
            transient.unlink()
    atomic_write(heartbeat_path(root), b"starting\n", 0o600)
    command = build_internal_command(setup, repo, preset, args)
    environment = os.environ.copy()
    environment.update(
        {
            "CAGE_DESKTOP_INTERNAL": "1",
            "CAGE_DESKTOP_TARGET_ID": metadata["target_id"],
            "CAGE_DESKTOP_STATE_DIR": str(root),
            "CAGE_DESKTOP_PUBLIC_KEY": metadata["public_key"],
            "CAGE_DESKTOP_HEARTBEAT": str(heartbeat_path(root)),
            "CAGE_DESKTOP_CONTAINER_NAME": metadata["container_name"],
            "CAGE_DESKTOP_VOLUME_NAME": metadata["volume_name"],
            "CAGE_DESKTOP_FINGERPRINT": fingerprint,
        }
    )
    supervise = [
        setup["python"],
        "-I",
        setup["helper"],
        "--config-dir",
        str(config_root(args)),
        "--launcher",
        setup["launcher_resolved"],
        "supervise",
        metadata["target_id"],
        "--",
        *command,
    ]
    log_descriptor = os.open(
        log_path(root),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        write_json(
            runtime_path(root),
            {
                "version": STATE_VERSION,
                "status": "starting",
                "supervisor_pid": None,
                "fingerprint": fingerprint,
                "started_at": int(time.time()),
            },
        )
        process = subprocess.Popen(
            supervise,
            stdin=subprocess.DEVNULL,
            stdout=log_descriptor,
            stderr=log_descriptor,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log_descriptor)
    runtime = wait_until_ready(root, timeout=1800.0 if args.rebuild else 600.0)
    try:
        update_known_hosts(args, setup, metadata, runtime)
    except Exception:
        with contextlib.suppress(DesktopError):
            command_stop_for_root(root)
        raise
    print(f"Desktop target ready: {metadata['alias']}")
    print(f"Repository: {repo}")
    print("In ChatGPT: Settings → Connections → select the host above.")
    if not args.no_open:
        open_chatgpt()
    return 0


def command_stop_for_root(root: Path) -> None:
    runtime = runtime_status(root)
    if runtime.get("status") not in {"starting", "ready"}:
        return
    try:
        response = control_request(root, "stop", timeout=10)
    except DesktopError:
        # A dead supervisor cannot perform cleanup.  The next start performs
        # label-checked stale recovery; stop still records the truthful state.
        merge_runtime(
            root,
            {
                "status": "stale",
                "container_id": None,
                "stopped_at": int(time.time()),
            },
        )
        return
    if response.get("ok") is not True:
        raise DesktopError(str(response.get("error") or "desktop stop failed"))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = runtime_status(root)
        if state.get("status") not in {"starting", "ready", "stopping"}:
            return
        time.sleep(0.2)
    raise DesktopError("timed out waiting for desktop target to stop")


def locate_target(
    args: argparse.Namespace, *, require: bool = True
) -> tuple[Path, dict[str, Any]] | None:
    secure_directory(targets_root(args))
    if getattr(args, "target_id", ""):
        root = target_root(args, args.target_id)
        metadata = read_json(metadata_path(root), missing_ok=not require)
        if metadata is None:
            return None
        return root, metadata
    preset = validate_preset(args.preset)
    repo = canonical_repo(args.repo)
    root = target_root(args, target_id(repo, preset))
    metadata = read_json(metadata_path(root), missing_ok=not require)
    if metadata is None:
        return None
    return root, metadata


def command_status(args: argparse.Namespace) -> int:
    located = locate_target(args)
    assert located is not None
    root, metadata = located
    runtime = live_runtime_status(root)
    print(f"Host:       {metadata['alias']}")
    print(f"Repository: {metadata['repo']}")
    print(f"Preset:     {metadata['preset']}")
    print(f"Status:     {runtime.get('status', 'stopped')}")
    if runtime.get("container_id"):
        print(f"Container:  {runtime['container_id'][:12]}")
    print(f"Volume:     {metadata['volume_name']}")
    return 0 if runtime.get("status") == "ready" else 1


def command_list(args: argparse.Namespace) -> int:
    entries = metadata_entries(args)
    summaries = [public_target_summary(args, item) for item in entries]
    if args.json:
        print(json.dumps(
            {"version": LIST_FORMAT_VERSION, "targets": summaries},
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0
    if not summaries:
        print("No Cage desktop targets.")
        return 0
    for item in summaries:
        print(
            f"{item['alias']}\t{item['status']}\t{item['preset']}\t{item['repo']}"
        )
    return 0


def command_stop(args: argparse.Namespace) -> int:
    located = locate_target(args)
    assert located is not None
    root, metadata = located
    with private_lock(operation_lock_path(args, root.name)):
        setup = load_setup(args)
        command_stop_for_root(root)
        if runtime_status(root).get("status") == "stale":
            recover_stale_target(setup, root, metadata)
        print(f"Desktop target stopped: {metadata['alias']}")
    return 0


def command_logs(args: argparse.Namespace) -> int:
    located = locate_target(args)
    assert located is not None
    root, _ = located
    data = read_bounded(log_path(root), missing_ok=True)
    sys.stdout.buffer.write(data)
    return 0


def confirm_remove(metadata: dict[str, Any], assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise DesktopError("desktop remove requires a TTY or --yes")
    alias = str(metadata["alias"])
    print(
        f"This deletes desktop history, SSH keys, and volume "
        f"{metadata['volume_name']}."
    )
    answer = input(f"Type {alias} to remove: ")
    if answer != alias:
        raise DesktopError("desktop target removal cancelled")


def command_remove(args: argparse.Namespace) -> int:
    located = locate_target(args)
    assert located is not None
    root, metadata = located
    with private_lock(operation_lock_path(args, root.name)):
        setup = load_setup(args)
        confirm_remove(metadata, args.yes)
        command_stop_for_root(root)
        # Always perform the immutable-label check before removing a container
        # by its deterministic name. A stopped target's name may have been
        # reused outside Cage since the last launch; never delete that
        # unrelated container during destructive target removal.
        recover_stale_target(setup, root, metadata)
        docker = setup["docker"]
        result = subprocess.run(
            [docker, "volume", "rm", metadata["volume_name"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0 and "No such volume" not in result.stderr:
            raise DesktopError(f"could not remove desktop volume: {result.stderr.strip()}")
        tombstone = root.with_name(f".remove-{root.name}-{time.time_ns()}")
        root.rename(tombstone)
        shutil.rmtree(tombstone)
        rebuild_ssh_include(args, setup)
        print(f"Desktop target removed: {metadata['alias']}")
    return 0


def command_proxy(args: argparse.Namespace) -> int:
    setup = load_setup(args)
    root = target_root(args, args.target_id)
    runtime = runtime_status(root)
    if runtime.get("status") != "ready":
        raise DesktopError("Cage desktop target is not running")
    container_id = runtime.get("container_id")
    if not isinstance(container_id, str) or not container_id:
        raise DesktopError("Cage desktop target has no container id")
    docker = setup["docker"]
    inspect = subprocess.run(
        [
            docker,
            "inspect",
            "--format",
            '{{.State.Running}}|{{index .Config.Labels "com.sindycate.cage.desktop-target"}}',
            container_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if inspect.returncode != 0 or inspect.stdout.strip() != f"true|{args.target_id}":
        raise DesktopError("Cage desktop container identity check failed")
    os.execv(
        docker,
        [
            docker,
            "exec",
            "-i",
            "--user",
            "root",
            container_id,
            "/usr/sbin/sshd",
            "-i",
            "-e",
            "-f",
            "/run/cage/sshd_config",
        ],
    )
    return 1


def merge_runtime(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    value = read_json(runtime_path(root), missing_ok=True) or {
        "version": STATE_VERSION
    }
    value.update(updates)
    write_json(runtime_path(root), value)
    return value


def command_mark_ready(args: argparse.Namespace) -> int:
    root = target_root(args, args.target_id)
    value = merge_runtime(
        root,
        {
            "status": "ready",
            "container_id": args.container_id,
            "container_name": args.container_name,
            "volume_name": args.volume_name,
            "fingerprint": args.fingerprint,
            "ready_at": int(time.time()),
        },
    )
    write_json(ready_path(root), value)
    return 0


def command_supervise(args: argparse.Namespace) -> int:
    root = target_root(args, args.target_id)
    secure_directory(root)
    if not args.command:
        raise DesktopError("internal supervisor command is missing")
    lock_path = root / "supervisor.lock"
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_descriptor, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DesktopError("a desktop supervisor is already active") from exc
        control = socket_path(root)
        with contextlib.suppress(FileNotFoundError):
            control.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(control))
        control.chmod(0o600)
        server.listen(4)
        server.settimeout(0.25)
        child = subprocess.Popen(args.command, start_new_session=True)
        merge_runtime(
            root,
            {
                "status": "starting",
                "supervisor_pid": os.getpid(),
                "child_pid": child.pid,
                "exit_code": None,
            },
        )
        stop_requested = False
        last_heartbeat = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_heartbeat >= 2:
                    update_in_place(
                        heartbeat_path(root),
                        f"{time.time_ns()}\n".encode(),
                        0o600,
                    )
                    last_heartbeat = now
                if child.poll() is not None:
                    break
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    request = connection.recv(128).decode("utf-8", "replace").strip()
                    if request == "stop":
                        stop_requested = True
                        merge_runtime(
                            root,
                            {
                                "status": "stopping",
                                "stopping_at": int(time.time()),
                            },
                        )
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGTERM)
                        response = {"ok": True}
                    elif request == "status":
                        response = runtime_status(root)
                    else:
                        response = {"ok": False, "error": "unsupported command"}
                    connection.sendall(json.dumps(response).encode())
            exit_code = child.wait()
        finally:
            server.close()
            with contextlib.suppress(FileNotFoundError):
                control.unlink()
    finally:
        os.close(lock_descriptor)
    merge_runtime(
        root,
        {
            "status": "stopped" if stop_requested or exit_code == 0 else "failed",
            "exit_code": exit_code,
            "stopped_at": int(time.time()),
            "container_id": None,
        },
    )
    return exit_code


def add_target_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", required=True)
    parser.add_argument("repo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--launcher", required=True)
    sub = parser.add_subparsers(dest="command_name", required=True)

    sub.add_parser("setup")

    for name in ("start", "restart"):
        item = sub.add_parser(name)
        add_target_selector(item)
        item.add_argument("--net", choices=("open", "gate", "off"), default="")
        yolo = item.add_mutually_exclusive_group()
        yolo.add_argument("--yolo", dest="yolo", action="store_true")
        yolo.add_argument("--no-yolo", dest="yolo", action="store_false")
        item.set_defaults(yolo=None)
        item.add_argument("--rebuild", action="store_true")
        item.add_argument("--no-open", action="store_true")

    for name in ("status", "stop", "logs"):
        item = sub.add_parser(name)
        add_target_selector(item)

    list_targets = sub.add_parser("list")
    list_targets.add_argument(
        "--json",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    remove = sub.add_parser("remove")
    add_target_selector(remove)
    remove.add_argument("--yes", action="store_true")

    proxy = sub.add_parser("proxy")
    proxy.add_argument("target_id")

    supervise = sub.add_parser("supervise")
    supervise.add_argument("target_id")
    supervise.add_argument("separator", nargs="?")
    supervise.add_argument("command", nargs=argparse.REMAINDER)

    ready = sub.add_parser("mark-ready")
    ready.add_argument("target_id")
    ready.add_argument("--container-id", required=True)
    ready.add_argument("--container-name", required=True)
    ready.add_argument("--volume-name", required=True)
    ready.add_argument("--fingerprint", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command_name == "setup":
        return command_setup(args)
    if args.command_name == "start":
        return command_start(args)
    if args.command_name == "restart":
        return command_start(args, restart=True)
    if args.command_name == "status":
        return command_status(args)
    if args.command_name == "list":
        return command_list(args)
    if args.command_name == "stop":
        return command_stop(args)
    if args.command_name == "logs":
        return command_logs(args)
    if args.command_name == "remove":
        return command_remove(args)
    if args.command_name == "proxy":
        return command_proxy(args)
    if args.command_name == "supervise":
        if args.separator == "--":
            pass
        elif args.separator:
            args.command.insert(0, args.separator)
        return command_supervise(args)
    if args.command_name == "mark-ready":
        return command_mark_ready(args)
    raise DesktopError("unsupported desktop command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DesktopError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
