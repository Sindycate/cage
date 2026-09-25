"""Read only thread/provider identifiers from the scoped Codex state database."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import time


MAX_ROWS = 200000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
DATABASE_NAME = "state_5.sqlite"


def read_thread_providers(root: Path) -> dict[str, str]:
    database = root / DATABASE_NAME
    try:
        database.lstat()
    except FileNotFoundError:
        return {}
    for suffix in ("", "-wal", "-shm"):
        path = root / (DATABASE_NAME + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_DATABASE_BYTES
        ):
            raise ValueError("unsafe Codex provider metadata file")
    deadline = time.monotonic() + 5
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        rows = connection.execute(
            "SELECT model_provider, rollout_path FROM threads LIMIT ?",
            (MAX_ROWS + 1,),
        )
        providers: dict[str, str] = {}
        for count, (provider, rollout_path) in enumerate(rows, 1):
            if count > MAX_ROWS:
                raise ValueError("too many Codex thread provider records")
            if (
                not isinstance(provider, str) or not provider.strip()
                or len(provider) > 256
                or not isinstance(rollout_path, str) or len(rollout_path) > 4096
                or any(ord(c) < 32 for c in provider + rollout_path)
            ):
                raise ValueError("invalid Codex thread provider record")
            filename = PurePosixPath(rollout_path.replace("\\", "/")).name
            if filename.endswith(".jsonl.zst"):
                session_id = filename[:-10]
            elif filename.endswith(".jsonl"):
                session_id = filename[:-6]
            else:
                raise ValueError("invalid Codex rollout filename")
            if not session_id or len(session_id) > 300:
                raise ValueError("invalid Codex rollout identifier")
            if session_id in providers and providers[session_id] != provider:
                raise ValueError("conflicting Codex thread provider records")
            providers[session_id] = provider
        return providers
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        print(json.dumps(read_thread_providers(Path("/scan/codex-state"))))
    except (OSError, ValueError, sqlite3.Error):
        print("cage: cannot read scoped Codex thread/provider metadata", file=sys.stderr)
        raise SystemExit(1)
