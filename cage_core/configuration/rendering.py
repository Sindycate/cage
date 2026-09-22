"""Comment-preserving TOML rendering and validated edit output."""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from .schema import (
    ConfigError,
    EDITABLE_COLLECTIONS,
    as_table,
)


TABLE_RE = re.compile(r"^\s*\[\[?[^\]\r\n]+\]\]?\s*(?:#.*)?$")


PROJECTS_TABLE_RE = re.compile(r"^\s*\[projects\]\s*(?:#.*)?$")


def toml_quote(value: str) -> str:
    return json.dumps(value)


def replace_projects_section(text: str, projects: dict[str, str]) -> str:
    lines = text.splitlines()
    start = None
    end = len(lines)
    header = "[projects]"
    preserved_comments: list[str] = []
    for i, line in enumerate(lines):
        if PROJECTS_TABLE_RE.fullmatch(line):
            start = i
            header = line
            for j in range(i + 1, len(lines)):
                if TABLE_RE.fullmatch(lines[j]):
                    end = j
                    break
                if not lines[j].strip() or lines[j].lstrip().startswith("#"):
                    preserved_comments.append(lines[j])
            break
    section = [header]
    section.extend(preserved_comments)
    if preserved_comments and preserved_comments[-1].strip():
        section.append("")
    for path, preset in sorted(projects.items()):
        section.append(f"{toml_quote(path)} = {toml_quote(preset)}")
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(section)
    else:
        lines = lines[:start] + section + lines[end:]
    return "\n".join(lines) + "\n"


def toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else toml_quote(value)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return toml_quote(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{toml_key(str(key))} = {toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise ConfigError(f"cannot serialize unsupported TOML value: {type(value).__name__}")


def render_table(path: tuple[str, ...], value: dict[str, Any]) -> str:
    header = ".".join(toml_key(part) for part in path)
    lines = [f"[{header}]"]
    for key, item in value.items():
        lines.append(f"{toml_key(str(key))} = {toml_value(item)}")
    return "\n".join(lines) + "\n"


def parse_header_path(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    header = stripped.split("#", 1)[0].strip()
    try:
        parsed = tomllib.loads(header + "\n")
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    cursor: Any = parsed
    while True:
        if isinstance(cursor, dict) and len(cursor) == 1:
            key, cursor = next(iter(cursor.items()))
            path.append(str(key))
        elif isinstance(cursor, list) and len(cursor) == 1:
            cursor = cursor[0]
        else:
            break
    return tuple(path)


def table_spans(text: str) -> list[tuple[tuple[str, ...], int, int]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    headers: list[tuple[tuple[str, ...], int]] = []
    for line in lines:
        offsets.append(offset)
        path = parse_header_path(line)
        if path:
            headers.append((path, offset))
        offset += len(line)
    spans: list[tuple[tuple[str, ...], int, int]] = []
    for index, (path, start) in enumerate(headers):
        end = headers[index + 1][1] if index + 1 < len(headers) else len(text)
        spans.append((path, start, end))
    return spans


def replace_table(text: str, path: tuple[str, ...], value: dict[str, Any] | None) -> str:
    matching = [span for span in table_spans(text) if span[0][: len(path)] == path]
    rendered = render_table(path, value) if value is not None else ""
    if not matching:
        if value is None:
            return text
        separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + rendered
    start = min(span[1] for span in matching)
    end = max(span[2] for span in matching)
    suffix = text[end:]
    if rendered and suffix and not rendered.endswith("\n\n"):
        rendered += "\n"
    return text[:start] + rendered + suffix


def replace_top_level_value(text: str, key: str, value: Any | None) -> str:
    first_table = min((start for _, start, _ in table_spans(text)), default=len(text))
    prefix = text[:first_table]
    suffix = text[first_table:]
    pattern = re.compile(rf"(?m)^\s*{re.escape(key)}\s*=.*(?:\n|$)")
    replacement = "" if value is None else f"{key} = {toml_value(value)}\n"
    if pattern.search(prefix):
        prefix = pattern.sub(replacement, prefix, count=1)
    elif value is not None:
        prefix = replacement + prefix
    return prefix + suffix


def render_config_changes(text: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    rendered = text
    for collection in sorted(EDITABLE_COLLECTIONS):
        before_table = as_table(before, collection)
        after_table = as_table(after, collection)
        for name in sorted(set(before_table) | set(after_table)):
            if before_table.get(name) != after_table.get(name):
                value = after_table.get(name)
                rendered = replace_table(
                    rendered,
                    (collection, name),
                    value if isinstance(value, dict) else None,
                )
    if before.get("defaults", {}) != after.get("defaults", {}):
        rendered = replace_table(rendered, ("defaults",), as_table(after, "defaults"))
    if before.get("storage", {}) != after.get("storage", {}):
        rendered = replace_table(rendered, ("storage",), as_table(after, "storage"))
    if before.get("projects", {}) != after.get("projects", {}):
        rendered = replace_projects_section(rendered, as_table(after, "projects"))
    if before.get("default_preset") != after.get("default_preset"):
        rendered = replace_top_level_value(rendered, "default_preset", after.get("default_preset"))
    if not rendered.endswith("\n"):
        rendered += "\n"
    try:
        reparsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"refusing to render invalid TOML: {exc}") from exc
    if reparsed != after:
        raise ConfigError("rendered TOML does not match the validated configuration")
    return rendered
