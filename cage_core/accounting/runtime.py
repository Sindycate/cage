"""Trusted executable snapshots and explicit Docker endpoint binding."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager

from . import store


DOCKER_ENV = ("DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def _output(docker: str, arguments: list[str]) -> str:
    try:
        result = subprocess.run([docker, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise store.AccountingError("accounting Docker endpoint is unavailable") from exc
    if len(result.stdout) > 8192:
        raise store.AccountingError("invalid accounting Docker response")
    return result.stdout.strip()


def transport(docker: str) -> dict:
    environment = {key: os.environ[key] for key in DOCKER_ENV if os.environ.get(key)}
    if not environment.get("DOCKER_HOST") and not environment.get("DOCKER_CONTEXT"):
        environment["DOCKER_CONTEXT"] = _output(docker, ["context", "show"])
    result = {"environment": environment, "engine": _output(docker, ["info", "--format", "{{.ID}}"])}
    validate_transport(result)
    return result


def validate_transport(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {"environment", "engine"}:
        raise store.AccountingError("invalid accounting Docker binding")
    engine = value["engine"]
    if not isinstance(engine, str) or not re.fullmatch(r"[a-zA-Z0-9:-]{8,128}", engine):
        raise store.AccountingError("invalid accounting Docker engine identity")
    environment = value["environment"]
    if not isinstance(environment, dict) or set(environment) - set(DOCKER_ENV):
        raise store.AccountingError("invalid accounting Docker environment")
    for name, text in environment.items():
        if not isinstance(text, str) or not 1 <= len(text) <= 4096 or any(c in text for c in "\0\r\n"):
            raise store.AccountingError("invalid accounting Docker environment value")
        if name in {"DOCKER_CONFIG", "DOCKER_CERT_PATH"} and not Path(text).is_absolute():
            raise store.AccountingError("Docker configuration paths must be absolute")
        if name == "DOCKER_HOST":
            # Credentials never enter durable work. SSH user names and socket
            # paths are allowed; passwords, query strings and fragments are not.
            from urllib.parse import urlsplit
            parsed = urlsplit(text)
            if parsed.scheme not in {"unix", "tcp", "ssh", "npipe"} or parsed.password or parsed.query or parsed.fragment:
                raise store.AccountingError("Docker endpoint cannot be safely persisted")
    if not environment.get("DOCKER_CONTEXT") and not environment.get("DOCKER_HOST"):
        raise store.AccountingError("accounting Docker endpoint is not explicit")


@contextmanager
def endpoint(docker: str, value: dict):
    validate_transport(value)
    saved = {key: os.environ.get(key) for key in DOCKER_ENV}
    try:
        for key in DOCKER_ENV:
            os.environ.pop(key, None)
        os.environ.update(value["environment"])
        if _output(docker, ["info", "--format", "{{.ID}}"] ) != value["engine"]:
            raise store.AccountingError("accounting Docker engine changed")
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def image_id(docker: str, image: str) -> str:
    result = _output(docker, ["image", "inspect", "--format", "{{.Id}}", image])
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", result):
        raise store.AccountingError("accounting image lacks an immutable identity")
    return result


def snapshot(config_root: Path, install_root: Path) -> Path:
    """Copy only trusted code, then exec it afresh. Never import mixed versions."""
    install_root = install_root.resolve()
    before = install_root.stat()
    paths = [install_root / "cage-main.py", *sorted((install_root / "cage_core").rglob("*.py"))]
    content: dict[str, bytes] = {}
    signatures = {}
    for path in paths:
        relative = path.relative_to(install_root).as_posix()
        if any(part.startswith(".") or part == "__pycache__" for part in Path(relative).parts):
            continue
        for parent in path.parents:
            if parent == install_root:
                break
            if parent.is_symlink():
                raise store.AccountingError("unsafe accounting runtime package")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 2 * 1024 * 1024:
                raise store.AccountingError("unsafe accounting runtime file")
            content[relative] = handle.read(2 * 1024 * 1024 + 1)
            signatures[path] = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    after = install_root.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or any(
        (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size) != expected
        for path, expected in signatures.items() for s in [path.stat(follow_symlinks=False)]
    ):
        raise store.AccountingError("Cage installation changed while preparing accounting")
    if not content or sum(map(len, content.values())) > 16 * 1024 * 1024:
        raise store.AccountingError("accounting runtime exceeds its size limit")
    manifest = {path: hashlib.sha256(data).hexdigest() for path, data in content.items()}
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    with store.root(config_root) as fd, store.directory(fd, "runtimes") as runtimes:
        with store.lock(fd, "runtime.lock", timeout=5):
            destination = config_root / "accounting" / "runtimes" / identity
            if not destination.exists():
                if len(os.listdir(runtimes)) >= 64:
                    raise store.AccountingError("accounting runtime limit reached; retained work was preserved")
                temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=destination.parent))
                try:
                    for name, data in content.items():
                        target = temporary / name
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with target.open("xb") as out:
                            os.fchmod(out.fileno(), 0o600)
                            out.write(data)
                            out.flush()
                            os.fsync(out.fileno())
                    for directory, _names, _files in os.walk(temporary, topdown=False):
                        opened = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            os.fsync(opened)
                        finally:
                            os.close(opened)
                    os.rename(temporary, destination)
                    os.fsync(runtimes)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            # A cached snapshot is executable authority, not a trusted job field.
            with store.directory(runtimes, identity, create=False):
                for name, expected in manifest.items():
                    path = destination / name
                    for parent in path.parents:
                        if parent == destination:
                            break
                        if parent.is_symlink():
                            raise store.AccountingError("unsafe cached accounting runtime")
                    opened = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(opened, "rb") as inp:
                        store.check_file(inp.fileno())
                        if hashlib.sha256(inp.read(store.MAX_BYTES + 1)).hexdigest() != expected:
                            raise store.AccountingError("cached accounting runtime changed")
            return destination
