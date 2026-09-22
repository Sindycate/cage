"""Monitor failures shared by policy and effectful adapters."""

from __future__ import annotations




class MonitorError(RuntimeError):
    """Raised for an invalid monitor state or an unavailable collector."""
