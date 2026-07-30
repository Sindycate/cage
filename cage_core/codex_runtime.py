#!/usr/bin/env python3
"""Runtime adapter for the pure selected-only Codex policy."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Isolated mode intentionally excludes the script directory. Add only this
# resolved installation root, never cwd, PYTHONPATH, or the user site.
_INSTALL_ROOT = Path(__file__).resolve().parents[1]
if str(_INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(_INSTALL_ROOT))

from cage_core import codex_policy


MAX_INVENTORY_BYTES = 4 * 1024 * 1024
INVENTORY_TIMEOUT = 60.0


def toml_transports(
    path: Path, *, maximum_bytes: int = MAX_INVENTORY_BYTES
) -> dict[str, str] | None:
    if not path.exists():
        return {}
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(maximum_bytes + 1)
    except OSError:
        return None
    return codex_policy.parse_toml_transports(
        raw, maximum_bytes=maximum_bytes
    )


def inventory_enabled(
    *,
    codex_binary: str,
    codex_home: Path,
    repository: Path,
    profile: str,
    environment: dict[str, str],
    timeout: float = INVENTORY_TIMEOUT,
    maximum_bytes: int = MAX_INVENTORY_BYTES,
    temporary_home_when_missing: bool = False,
) -> tuple[set[str], set[str], dict[str, str]]:
    inventory_home = codex_home
    temporary_home: tempfile.TemporaryDirectory[str] | None = None
    if temporary_home_when_missing and not codex_home.is_dir():
        temporary_home = tempfile.TemporaryDirectory(
            prefix="cage-mcp-inventory-"
        )
        inventory_home = Path(temporary_home.name)

    command = [codex_binary]
    if profile:
        command.extend(("--profile", profile))
    command.extend(("mcp", "list", "--json"))
    child_environment = dict(environment)
    child_environment["CODEX_HOME"] = str(inventory_home)
    try:
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                env=child_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise codex_policy.PolicyError(
                f"cannot inventory MCP servers: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise codex_policy.PolicyError(
                "cannot build a trustworthy MCP inventory: codex mcp list "
                f"exited {completed.returncode}"
            )
        runtime_enabled = codex_policy.parse_inventory_output(
            completed.stdout or b"", maximum_bytes=maximum_bytes
        )
    finally:
        if temporary_home is not None:
            temporary_home.cleanup()

    direct_transports: dict[str, str] = {}
    if profile:
        profile_transports = toml_transports(
            codex_home / f"{profile}.config.toml",
            maximum_bytes=maximum_bytes,
        )
        if profile_transports is None:
            raise codex_policy.PolicyError(
                "cannot build a trustworthy MCP inventory: unreadable "
                "profile layer"
            )
        codex_policy.merge_transports(
            direct_transports, profile_transports
        )
    project_transports = toml_transports(
        repository / ".codex" / "config.toml",
        maximum_bytes=maximum_bytes,
    )
    if project_transports is None:
        raise codex_policy.PolicyError(
            "cannot build a trustworthy MCP inventory: unreadable project layer"
        )
    codex_policy.merge_transports(direct_transports, project_transports)
    enabled = runtime_enabled | set(direct_transports)
    return enabled, runtime_enabled, direct_transports


def runtime_overrides(
    *,
    selected: set[str],
    codex_binary: str,
    codex_home: Path,
    repository: Path,
    profile: str,
    environment: dict[str, str],
) -> tuple[list[str], list[str]]:
    enabled, runtime_enabled, direct_transports = inventory_enabled(
        codex_binary=codex_binary,
        codex_home=codex_home,
        repository=repository,
        profile=profile,
        environment=environment,
    )
    return codex_policy.resolve_suppression(
        selected=selected,
        enabled=enabled,
        runtime_enabled=runtime_enabled,
        direct_transports=direct_transports,
    )


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-argv")
    validate.add_argument("argv", nargs=argparse.REMAINDER)
    runtime = subparsers.add_parser("runtime-overrides")
    runtime.add_argument("--codex-bin", required=True)
    runtime.add_argument("--codex-home", type=Path, required=True)
    runtime.add_argument("--repo", type=Path, required=True)
    runtime.add_argument("--profile", default="")
    runtime.add_argument("--selected-stdio-json", default="")
    runtime.add_argument("--selected-remote-json", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-argv":
            passthrough = args.argv
            if passthrough and passthrough[0] == "--":
                passthrough = passthrough[1:]
            codex_policy.reject_unsafe_passthrough_args(passthrough)
            return 0
        selected = codex_policy.selected_names(
            args.selected_stdio_json,
            args.selected_remote_json,
        )
        overrides, suppressed = runtime_overrides(
            selected=selected,
            codex_binary=args.codex_bin,
            codex_home=args.codex_home,
            repository=args.repo,
            profile=args.profile,
            environment=os.environ.copy(),
        )
        print("cage: MCP policy: selected packs only", file=sys.stderr)
        if suppressed:
            rendered = " ".join(
                json.dumps(name, ensure_ascii=True) for name in suppressed
            )
            print(
                "cage: inherited MCPs suppressed for this launch: "
                + rendered,
                file=sys.stderr,
            )
        for override in overrides:
            print(override)
        return 0
    except codex_policy.PolicyError as exc:
        print(f"cage: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
