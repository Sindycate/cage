#!/usr/bin/env python3
"""Compatibility frontend for Cage central configuration."""

from __future__ import annotations

import sys
import types
from pathlib import Path


INSTALL_ROOT = Path(__file__).resolve().parent
if str(INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(INSTALL_ROOT))

# Re-exported names preserve the import surface used by existing integrations
# while all implementation ownership lives in the core package.
from cage_core import config as _config  # noqa: E402
from cage_core.config import *  # noqa: F401,F403,E402


class _CompatibilityModule(types.ModuleType):
    """Forward patched public constants to the implementation module."""

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(_config, name):
            setattr(_config, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CompatibilityModule


if __name__ == "__main__":
    raise SystemExit(main())
