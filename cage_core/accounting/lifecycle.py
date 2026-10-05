"""Foreground producers: bounded metadata only, including the final handoff."""

from __future__ import annotations

from pathlib import Path
import sys
import threading

from . import grants, queue, store, worker


class HandoffError(RuntimeError):
    pass


class Producer:
    def __init__(self, root: Path, docker: str, pinned_runtime: Path, source: dict, permissions: dict, interval: int):
        self.root, self.docker, self.runtime = root, docker, pinned_runtime
        self.source, self.permissions = source, permissions
        self.interval = interval
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._finished = False
        self._thread = threading.Thread(target=self._run, name="cage-accounting-producer", daemon=True)
        self._thread.start()

    def _submit(self, *, final=False):
        try:
            self._retry_busy(lambda: queue.enqueue(self.root, self.source, self.permissions, final=final))
        except grants.Revoked:
            raise
        except Exception as exc:
            raise HandoffError(f"durable request was not confirmed ({type(exc).__name__})") from exc
        try:
            self._retry_busy(lambda: worker.wake(self.root, self.docker, self.runtime))
        except Exception as exc:
            raise HandoffError(f"request saved; worker wake needs retry ({type(exc).__name__})") from exc

    @staticmethod
    def _retry_busy(operation):
        # Busy is raised before obtaining the short transaction lock, so retry
        # cannot duplicate a successful revision or launch an extra process.
        try:
            return operation()
        except store.Busy:
            return operation()

    def _run(self):
        while True:
            with self._lock:
                if self._stopping.is_set():
                    return
                try:
                    self._submit()
                except grants.Revoked:
                    self._stopping.set()
                    return
                except Exception as exc:
                    if not sys.stderr.isatty():
                        self._warning(exc)
            if self._stopping.wait(self.interval):
                return

    def request_stop(self):
        self._stopping.set()

    def _warning(self, error):
        command = "monitor jobs" if self.source["backend"] == "monitor" else "poketoken status"
        detail = str(error) if isinstance(error, HandoffError) else type(error).__name__
        print(f"WARNING: accounting handoff needs attention: {detail}; run cage {command}", file=sys.stderr)

    def stop(self) -> int:
        self.request_stop()
        # This lock can cover only a small fsync/queue transaction and spawn,
        # never collection, an HTTP operation, or a backend's long-lived lock.
        with self._lock:
            if self._finished:
                return 0
            self._finished = True
            try:
                self._submit(final=True)
            except grants.Revoked:
                pass
            except Exception as exc:
                self._warning(exc)
        return 0  # Optional accounting cannot mask the Codex exit status.
