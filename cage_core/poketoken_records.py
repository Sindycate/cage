"""Allowlisted Codex accounting records; also runs in a network-free collector."""

from __future__ import annotations

from datetime import datetime
import json
import os
import re
import stat
import sys


MAX_LINE = 16 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_SCAN = 2 * 1024 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
MAX_FILES = 20000
MAX_COUNTER = 10**12
COUNTERS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)


class RecordError(ValueError):
    pass


def timestamp(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise RecordError("invalid accounting timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError("invalid accounting timestamp") from exc
    if parsed.tzinfo is None:
        raise RecordError("accounting timestamp needs a timezone")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RecordError("invalid accounting identifier")
    return value


def usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise RecordError("invalid token counters")
    result = {}
    for key in COUNTERS:
        count = value.get(key, 0)
        if type(count) is not int or not 0 <= count <= MAX_COUNTER:
            raise RecordError("invalid token counter")
        result[key] = count
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise RecordError("cached tokens exceed input tokens")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise RecordError("reasoning tokens exceed output tokens")
    return result


def model_name(payload: dict) -> str | None:
    value = payload.get("model")
    if value is None and isinstance(payload.get("turn_context"), dict):
        value = payload["turn_context"].get("model")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}", value):
        raise RecordError("invalid accounting model name")
    return value


def sanitize_record(value: object) -> dict | None:
    """Rebuild records from primitives, never copy arbitrary nested payloads."""
    if not isinstance(value, dict):
        raise RecordError("invalid rollout record")
    kind = value.get("type")
    payload = value.get("payload")
    if kind not in {"session_meta", "turn_context", "event_msg"}:
        return None
    if not isinstance(payload, dict):
        raise RecordError("invalid rollout payload")
    model = model_name(payload)
    if kind == "session_meta":
        clean = {"id": identifier(payload.get("id") or payload.get("session_id"))}
        parent = payload.get("forked_from_id") or payload.get("parent_thread_id")
        if parent is not None:
            clean["forked_from_id"] = identifier(parent)
        source = payload.get("source")
        if payload.get("thread_source") == "subagent" or (
            isinstance(source, dict) and source.get("subagent") is not None
        ):
            clean["thread_source"] = "subagent"
    elif kind == "turn_context":
        if model is None:
            return None
        clean = {"model": model}
    else:
        if payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        # Codex also emits rate-limit-only events without any token observation.
        if info is None:
            return None
        if not isinstance(info, dict):
            raise RecordError("invalid token observation")
        if info.get("last_token_usage") is None:
            raise RecordError("token observation has no last usage")
        clean_info = {"last_token_usage": usage(info["last_token_usage"])}
        if info.get("total_token_usage") is not None:
            clean_info["total_token_usage"] = usage(info["total_token_usage"])
        clean = {"type": "token_count", "info": clean_info}
    if model is not None:
        clean["model"] = model
    return {"type": kind, "timestamp": timestamp(value.get("timestamp")), "payload": clean}


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")


def read_rollout(fd: int, budget: list[int]) -> list[dict]:
    records = []
    output_size = 0
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise RecordError("unsafe or oversized rollout file")
        # Snapshot the length: concurrent appends belong to the next refresh.
        remaining = info.st_size
        budget[0] += remaining
        if budget[0] > MAX_SCAN:
            raise RecordError("source scan exceeds its byte limit")
        while remaining:
            line = handle.readline(min(remaining, MAX_LINE + 1))
            if not line:
                raise RecordError("rollout changed during collection")
            remaining -= len(line)
            if len(line) > MAX_LINE:
                raise RecordError("rollout line exceeds its byte limit")
            if not line.endswith(b"\n"):
                if remaining:
                    raise RecordError("rollout line exceeds its byte limit")
                break  # An in-flight final line is never published partially.
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise RecordError("invalid rollout JSON") from exc
            record = sanitize_record(value)
            if record is not None:
                output_size += len(encode(record))
                if output_size > MAX_OUTPUT:
                    raise RecordError("accounting output exceeds its byte limit")
                records.append(record)
    if records and records[0]["type"] != "session_meta":
        raise RecordError("rollout has no initial session metadata")
    return records


def collect(roots: list[str]) -> None:
    """Only stdout carries allowlisted data; no source path appears in errors."""
    budget = [0]
    output_size = 0
    files = 0
    entries = 0

    def fail_walk(_error: OSError) -> None:
        raise RecordError("cannot traverse session directory")

    for root in roots:
        for _path, dirs, names, directory_fd in os.fwalk(root, follow_symlinks=False, onerror=fail_walk):
            entries += len(dirs) + len(names)
            if entries > MAX_FILES * 10:
                raise RecordError("session tree exceeds its entry limit")
            for name in dirs:
                if not stat.S_ISDIR(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                    raise RecordError("unsafe session directory")
            for name in sorted(names):
                if not name.endswith(".jsonl"):
                    continue
                files += 1
                if files > MAX_FILES:
                    raise RecordError("source scan exceeds its file limit")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                records = read_rollout(fd, budget)
                if not records:
                    continue
                data = encode({"name": name, "records": records})
                output_size += len(data)
                if output_size > MAX_OUTPUT:
                    raise RecordError("accounting output exceeds its byte limit")
                sys.stdout.buffer.write(data)


if __name__ == "__main__":
    try:
        collect(sys.argv[1:])
    except (RecordError, OSError, RecursionError):
        print("Codex accounting collection failed: invalid, unsafe, or oversized source", file=sys.stderr)
        sys.exit(1)
