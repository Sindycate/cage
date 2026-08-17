# ADR-002: Tiny Bash Bootstrap with a Typed Python Host Core

Status: accepted
Date: 2026-07-29

Amended: 2026-08-17. Cage now requires Python 3.12 or newer and validates the
host control plane in one Python 3.12 CI lane. This intentionally drops the
Python 3.11 compatibility promise for the sole-user deployment while retaining
all Docker, Desktop, OpenCode, installer, and release gates. No exact patch
version is pinned; all other architecture and dependency decisions below are
unchanged.

## Context

The host-side `cage` launcher grew to 2,691 lines of Bash. It parsed the public
CLI, resolved configuration through shell assignments, built Docker arguments,
started bridges, reconciled persistent state, installed traps, and dispatched
three execution targets. Several of those responsibilities embedded Python
heredocs, and Codex passthrough/MCP suppression policy had separate
implementations for host, container, and Desktop execution.

That structure made behavior-preserving changes difficult to review. Shell
quoting and cleanup order formed implicit interfaces, launch effects could be
interleaved with resolution, and there was no complete immutable description of
a launch before resources were created.

## Decision

Keep `cage` as a Bash 3.2-compatible bootstrap and move host orchestration into
a Python 3.12 standard-library-only package:

- `cage` resolves its real installation directory, validates Python 3.12+, sets
  the internal version, and executes `cage-main.py` with `python3 -I`;
- `cage-main.py` rejects symlink or non-regular package entries, then adds only
  its resolved installation root to `sys.path`;
- `LaunchRequest`, `ResolvedConfig`, and immutable `LaunchPlan` are the typed
  boundaries between parsing, resolution, and execution;
- a complete `LaunchPlan` is validated before image inspection, Docker volume
  changes, bridge startup, OAuth synchronization, or target launch;
- host, ordinary container, and Desktop targets consume the same plan and the
  same pure Codex passthrough/MCP suppression policy;
- runtime inventory and process execution live outside the policy/model layer;
- lifecycle resources register immediately and clean up in reverse order;
- compatibility frontends remain for configuration, the TUI, Desktop
  management, bridges, remote Codex, and container entrypoints.

The internal `resolve-json` contract is versioned and exact. Its public form
contains normalized repository, preset, execution, image, state, mount,
environment-name, and capability information. It never contains environment
values, credentials, bridge/proxy authentication data, OAuth state, MCP command
lines or headers, or raw passthrough arguments; only the passthrough count is
reported.

The source installer, release builder, CI syntax checks, and Codex image package
the core as a directory. Installation fails closed on package symlinks and
non-regular entries.

## Consequences

The public CLI, config format, TUI flow, target behavior, state locations, and
documented isolation model do not change. Python 3.12 remains a host
requirement. The Bash bootstrap is intentionally not a fallback launcher: if
the installed package fails integrity or import validation, Cage refuses to
run.

Tests should assert contracts and observable execution behavior rather than
copying implementation source or comparing embedded snippets. New launcher
features must extend planning first and keep secret resolution at the
process-creation boundary.

## Alternatives rejected

- Splitting the Bash script into sourced shell libraries would reduce file size
  but retain implicit global state, quoting-based interfaces, and trap
  composition.
- A large compatibility rewrite with a new CLI or config format would simplify
  internals at the cost of unrelated user migration and behavior risk.
- Adding a third-party Python framework would expand the installer and supply
  chain without providing a necessary capability.
