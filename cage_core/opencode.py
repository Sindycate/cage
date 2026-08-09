"""OpenCode launch snapshots and private selected-MCP handoff."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any


MAX_SNAPSHOT_FILES = 2_000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RUNTIME_ENV_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class OpenCodeError(RuntimeError):
    """Raised when OpenCode inputs cannot be frozen safely."""


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _open_directory(path: Path, label: str, *, missing_ok: bool) -> int | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise OpenCodeError(f"OpenCode {label} is missing: {path}")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise OpenCodeError(f"OpenCode {label} must be a real directory: {path}")
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise OpenCodeError(f"cannot safely open OpenCode {label} {path}: {exc}") from exc
    opened = os.fstat(descriptor)
    if _identity(before) != _identity(opened):
        os.close(descriptor)
        raise OpenCodeError(f"OpenCode {label} changed while opening: {path}")
    return descriptor


def _open_child_directory(
    parent: int, name: str, label: str, *, missing_ok: bool
) -> int | None:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise OpenCodeError(f"OpenCode {label} is missing")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise OpenCodeError(f"OpenCode {label} must be a real directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        raise OpenCodeError(f"cannot safely open OpenCode {label}: {exc}") from exc
    opened = os.fstat(descriptor)
    if _identity(before) != _identity(opened):
        os.close(descriptor)
        raise OpenCodeError(f"OpenCode {label} changed while opening")
    return descriptor


def _copy_regular_at(
    parent: int, name: str, destination: Path, label: str
) -> int:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise OpenCodeError(f"cannot inspect OpenCode input {label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OpenCodeError(f"OpenCode input must be a regular file: {label}")
    if before.st_nlink != 1:
        raise OpenCodeError(f"OpenCode input must not be hard-linked: {label}")
    if before.st_size > MAX_FILE_BYTES:
        raise OpenCodeError(
            f"OpenCode input exceeds the {MAX_FILE_BYTES}-byte limit: {label}"
        )
    try:
        descriptor = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent
        )
    except OSError as exc:
        raise OpenCodeError(f"cannot safely open OpenCode input {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise OpenCodeError(f"OpenCode input changed while opening: {label}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise OpenCodeError(
                    f"OpenCode input exceeds the {MAX_FILE_BYTES}-byte limit: {label}"
                )
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            _identity(opened) != _identity(after)
            or _identity(after) != _identity(current)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise OpenCodeError(f"OpenCode input changed while reading: {label}")
    finally:
        os.close(descriptor)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(output, "wb", closefd=False) as handle:
            handle.write(b"".join(chunks))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(output)
    return total


def _copy_tree_fd(
    source: int,
    source_label: str,
    destination: Path,
    budget: dict[str, int],
    *,
    ignored_names: frozenset[str] = frozenset(),
) -> None:
    before = os.fstat(source)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        names = sorted(os.listdir(source))
    except OSError as exc:
        raise OpenCodeError(f"cannot list OpenCode directory {source_label}: {exc}") from exc
    for name in names:
        if name in ignored_names:
            continue
        if name in {".", ".."} or "/" in name:
            raise OpenCodeError(f"invalid OpenCode directory entry in {source_label}")
        label = f"{source_label}/{name}"
        try:
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
        except OSError as exc:
            raise OpenCodeError(f"cannot inspect OpenCode input {label}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OpenCodeError(f"OpenCode snapshot refuses symlink: {label}")
        if stat.S_ISDIR(info.st_mode):
            child = _open_child_directory(source, name, label, missing_ok=False)
            assert child is not None
            try:
                _copy_tree_fd(child, label, destination / name, budget)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OpenCodeError(f"OpenCode snapshot refuses special file: {label}")
        budget["files"] += 1
        if budget["files"] > MAX_SNAPSHOT_FILES:
            raise OpenCodeError(
                f"OpenCode snapshot exceeds the {MAX_SNAPSHOT_FILES}-file limit"
            )
        budget["bytes"] += _copy_regular_at(source, name, destination / name, label)
        if budget["bytes"] > MAX_SNAPSHOT_BYTES:
            raise OpenCodeError(
                f"OpenCode snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte limit"
            )
    after = os.fstat(source)
    if _fingerprint(before) != _fingerprint(after):
        raise OpenCodeError(f"OpenCode directory changed while freezing: {source_label}")


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def create_launch_snapshot(
    *,
    destination: Path,
    host_config_directory: Path,
    repository: Path,
    stdio_mcp: list[dict[str, Any]],
    remote_mcp: list[dict[str, Any]],
    skill_mounts: list[dict[str, str]] | None = None,
    host_agents_directory: Path | None = None,
    plugins_enabled: bool = False,
) -> None:
    """Freeze bounded config inputs without following host/repository links."""

    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    budget = {"files": 0, "bytes": 0}
    ignored_extension_names = (
        frozenset()
        if plugins_enabled
        else frozenset(
            {
                "bun.lock",
                "bun.lockb",
                "node_modules",
                "package-lock.json",
                "package.json",
                "plugins",
                "pnpm-lock.yaml",
                "yarn.lock",
            }
        )
    )
    try:
        host = _open_directory(
            host_config_directory, "configuration directory", missing_ok=True
        )
        if host is not None:
            try:
                _copy_tree_fd(
                    host,
                    str(host_config_directory),
                    destination / "global",
                    budget,
                    ignored_names=ignored_extension_names,
                )
            finally:
                os.close(host)

        repo = _open_directory(repository, "repository", missing_ok=False)
        assert repo is not None
        try:
            project = _open_child_directory(
                repo, ".opencode", f"{repository}/.opencode", missing_ok=True
            )
            if project is not None:
                try:
                    _copy_tree_fd(
                        project,
                        f"{repository}/.opencode",
                        destination / "project-dir",
                        budget,
                        ignored_names=ignored_extension_names,
                    )
                finally:
                    os.close(project)
            for external_name, snapshot_name in (
                (".agents", "project-agents-skills"),
                (".claude", "project-claude-skills"),
            ):
                external = _open_child_directory(
                    repo,
                    external_name,
                    f"{repository}/{external_name}",
                    missing_ok=True,
                )
                if external is None:
                    continue
                try:
                    skills = _open_child_directory(
                        external,
                        "skills",
                        f"{repository}/{external_name}/skills",
                        missing_ok=True,
                    )
                    if skills is not None:
                        try:
                            _copy_tree_fd(
                                skills,
                                f"{repository}/{external_name}/skills",
                                destination / snapshot_name,
                                budget,
                            )
                        finally:
                            os.close(skills)
                finally:
                    os.close(external)
            project_instruction = ""
            for name in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md"):
                try:
                    os.stat(name, dir_fd=repo, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                budget["files"] += 1
                budget["bytes"] += _copy_regular_at(
                    repo,
                    name,
                    destination / "project-instruction" / name,
                    f"{repository}/{name}",
                )
                if budget["files"] > MAX_SNAPSHOT_FILES or budget["bytes"] > MAX_SNAPSHOT_BYTES:
                    raise OpenCodeError("OpenCode snapshot exceeds its bounded input limits")
                project_instruction = name
                break
            project_config = ""
            for name in ("opencode.jsonc", "opencode.json"):
                try:
                    os.stat(name, dir_fd=repo, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                budget["files"] += 1
                if budget["files"] > MAX_SNAPSHOT_FILES:
                    raise OpenCodeError(
                        f"OpenCode snapshot exceeds the {MAX_SNAPSHOT_FILES}-file limit"
                    )
                budget["bytes"] += _copy_regular_at(
                    repo, name, destination / name, f"{repository}/{name}"
                )
                if budget["bytes"] > MAX_SNAPSHOT_BYTES:
                    raise OpenCodeError(
                        f"OpenCode snapshot exceeds the {MAX_SNAPSHOT_BYTES}-byte limit"
                    )
                project_config = name
                break
        finally:
            os.close(repo)

        selected_skill_names: list[str] = []
        for item in skill_mounts or []:
            name = str(item["name"])
            source = Path(item["path"])
            descriptor = _open_directory(
                source, f"selected skill {name!r}", missing_ok=False
            )
            assert descriptor is not None
            try:
                _copy_tree_fd(
                    descriptor,
                    str(source),
                    destination / "selected-skills" / name,
                    budget,
                )
            finally:
                os.close(descriptor)
            selected_skill_names.append(name)

        if not selected_skill_names and host_agents_directory is not None:
            agents = _open_directory(
                host_agents_directory, "fallback agent registry", missing_ok=True
            )
            if agents is not None:
                try:
                    skills = _open_child_directory(
                        agents,
                        "skills",
                        f"{host_agents_directory}/skills",
                        missing_ok=True,
                    )
                    if skills is not None:
                        try:
                            _copy_tree_fd(
                                skills,
                                f"{host_agents_directory}/skills",
                                destination / "fallback-skills",
                                budget,
                            )
                        finally:
                            os.close(skills)
                finally:
                    os.close(agents)

        _write_manifest(
            destination / "manifest.json",
            {
                "schema": 1,
                "project_config": project_config,
                "project_instruction": project_instruction,
                "selected_skill_names": selected_skill_names,
                "selected_mcp": {
                    "stdio": [dict(item) for item in stdio_mcp],
                    "remote": [dict(item) for item in remote_mcp],
                },
            },
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def remove_snapshot(path: Path) -> int:
    """Remove one launcher-owned snapshot directory."""

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    return 0


def write_runtime_environment(path: Path, environment: dict[str, str]) -> None:
    """Write the private OpenCode-only environment handoff into its snapshot."""

    for name, value in environment.items():
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise OpenCodeError(f"invalid private OpenCode environment name: {name!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise OpenCodeError(
                f"invalid private OpenCode environment value for {name}"
            )
    encoded = (
        json.dumps(
            environment,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_RUNTIME_ENV_BYTES:
        raise OpenCodeError(
            f"private OpenCode environment exceeds {MAX_RUNTIME_ENV_BYTES} bytes"
        )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
