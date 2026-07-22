#!/usr/bin/env python3
"""Build Cage's source release archive reproducibly."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_FILES = (
    "cage",
    "cage-config.py",
    "cage-tui.py",
    "cage-netgate.sh",
    "netgate-proxy.py",
    "mcp-bridge.py",
    "mcp-relay",
    "host-cmd-bridge.py",
    "host-cmd-relay",
    "docker-compose.yml",
    "Dockerfile",
    "Dockerfile.codex",
    "entrypoint.sh",
    "entrypoint-codex.sh",
    "install.sh",
    "Makefile",
    "README.md",
    "SECURITY.md",
    "CHANGELOG.md",
)
PAYLOAD_DIRS = ("docs", "netgate")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?$")


def source_date_epoch() -> int:
    raw_value = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit("SOURCE_DATE_EPOCH must be a non-negative integer") from exc
    if value < 0:
        raise SystemExit("SOURCE_DATE_EPOCH must be a non-negative integer")
    return value


def payload_paths() -> list[Path]:
    paths = [Path(name) for name in PAYLOAD_FILES]
    for directory_name in PAYLOAD_DIRS:
        directory = ROOT / directory_name
        if not directory.is_dir() or directory.is_symlink():
            raise SystemExit(f"release payload directory is missing or unsafe: {directory_name}")
        paths.append(Path(directory_name))
        paths.extend(
            path.relative_to(ROOT)
            for path in directory.rglob("*")
            if "__pycache__" not in path.parts
        )

    unique_paths = sorted(set(paths), key=lambda item: item.as_posix())
    for relative_path in unique_paths:
        source = ROOT / relative_path
        if source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise SystemExit(f"release payload path is missing or unsafe: {relative_path}")
    return unique_paths


def normalized_info(
    archive: tarfile.TarFile,
    source: Path,
    archive_name: str,
    epoch: int,
) -> tarfile.TarInfo:
    info = archive.gettarinfo(str(source), arcname=archive_name)
    if not (info.isfile() or info.isdir()):
        raise SystemExit(f"release payload contains unsupported file type: {source}")
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    info.pax_headers = {}
    return info


def write_archive(destination: Path, version: str, epoch: int) -> None:
    prefix = f"cage-{version}"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw_stream:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_stream,
                compresslevel=9,
                mtime=epoch,
            ) as gzip_stream:
                with tarfile.open(
                    fileobj=gzip_stream,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    root_info = tarfile.TarInfo(prefix)
                    root_info.type = tarfile.DIRTYPE
                    root_info.mode = 0o755
                    root_info.uid = 0
                    root_info.gid = 0
                    root_info.uname = "root"
                    root_info.gname = "root"
                    root_info.mtime = epoch
                    archive.addfile(root_info)

                    for relative_path in payload_paths():
                        source = ROOT / relative_path
                        archive_name = f"{prefix}/{relative_path.as_posix()}"
                        info = normalized_info(archive, source, archive_name, epoch)
                        if info.isfile():
                            with source.open("rb") as source_stream:
                                archive.addfile(info, source_stream)
                        else:
                            archive.addfile(info)
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_checksum(archive_path: Path) -> Path:
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = archive_path.with_name(f"{archive_path.name}.sha256")
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    checksum_path.chmod(0o644)
    return checksum_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("output_dir", nargs="?", default=".")
    args = parser.parse_args()

    if not VERSION_RE.fullmatch(args.version):
        raise SystemExit(f"invalid Cage release version: {args.version!r}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"cage-{args.version}.tar.gz"
    write_archive(archive_path, args.version, source_date_epoch())
    checksum_path = write_checksum(archive_path)
    print(archive_path)
    print(checksum_path)


if __name__ == "__main__":
    main()
