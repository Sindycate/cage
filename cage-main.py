#!/usr/bin/env python3
"""Isolated Python entrypoint for the Cage host launcher."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def trusted_install_root() -> Path:
    root = Path(__file__).resolve().parent
    package = root / "cage_core"
    try:
        package_info = os.lstat(package)
    except OSError as exc:
        raise SystemExit(f"ERROR: Cage core package is missing: {exc}") from exc
    if stat.S_ISLNK(package_info.st_mode) or not stat.S_ISDIR(package_info.st_mode):
        raise SystemExit("ERROR: Cage core package path is unsafe")
    for directory, names, files in os.walk(package, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            try:
                info = os.lstat(path)
            except OSError as exc:
                raise SystemExit(
                    f"ERROR: Cage core package entry cannot be inspected: {path}: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise SystemExit(
                    f"ERROR: Cage core package entry is unsafe: {path}"
                )
    return root


if sys.version_info < (3, 11):
    raise SystemExit("ERROR: Cage requires Python 3.11 or newer.")

INSTALL_ROOT = trusted_install_root()
sys.path.insert(0, str(INSTALL_ROOT))

from cage_core.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(
        main(
            cage_version=os.environ.get("CAGE_INTERNAL_VERSION", ""),
            install_root=INSTALL_ROOT,
        )
    )
