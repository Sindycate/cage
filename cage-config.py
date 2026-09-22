#!/usr/bin/env python3
"""Compatibility frontend for Cage central configuration."""

from __future__ import annotations

import sys
from pathlib import Path


INSTALL_ROOT = Path(__file__).resolve().parent
if str(INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(INSTALL_ROOT))

# Re-exported names preserve the import surface used by existing integrations
# while all implementation ownership lives in the core package.
from cage_core.config import *  # noqa: F401,F403,E402


if __name__ == "__main__":
    raise SystemExit(main())
