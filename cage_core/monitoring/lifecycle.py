"""Run optional scans on schedule without interrupting the agent terminal."""

from __future__ import annotations

import math
import sys
import threading
import time

from . import constants as constants_api
from . import validation as validation_api


class ActiveMonitor:
    """Best-effort current-volume scanner with a wall-clock cadence."""

    def __init__(self, scan, interval_seconds: int, final_scan=None):
        self._scan = scan
        self._final_scan = final_scan or scan
        self._interval = validation_api.validate_interval(interval_seconds)
        self._interactive = sys.stderr.isatty()
        self._stop = threading.Event()
        self._final_scan_done = False
        self._thread = threading.Thread(target=self._run, name="cage-token-monitor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Compute the first wall-clock boundary before collection starts so a
        # slow collector cannot shift every later tick by one full interval.
        next_due = (math.floor(time.time() / self._interval) + 1) * self._interval
        self._background_scan()
        while not self._stop.is_set():
            wait_seconds = max(0.0, next_due - time.time())
            if self._stop.wait(wait_seconds):
                return
            self._background_scan()
            now = time.time()
            missed = max(1, math.floor((now - next_due) / self._interval) + 1)
            next_due += missed * self._interval

    def _background_scan(self) -> None:
        try:
            self._scan(False)
        except Exception as exc:  # optional observability must not stop Cage
            # scan_registration persists failures for `cage monitor status`.
            # An interactive child owns the terminal until stop(); writing
            # here corrupts its prompt. Redirected logs still get warnings.
            if not self._interactive:
                print(f"WARNING: Token Monitor scan skipped: {exc}", file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=constants_api.SCAN_TIMEOUT_SECONDS + 10)
        if self._final_scan_done:
            return
        self._final_scan_done = True
        try:
            # The lifecycle callback uses scan_registration(final=True): it
            # refreshes only this launch's exact volume, can merge cached peer
            # snapshots, and never calls the all-volume reconciliation path.
            final_scan = getattr(self, "_final_scan", self._scan)
            final_scan(True)
        except Exception as exc:
            print(
                f"WARNING: final Token Monitor scan skipped: {exc}; "
                "run cage monitor status for details",
                file=sys.stderr,
            )
