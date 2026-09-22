# Cage contributor instructions

Canonical repository guidance for coding agents. `CLAUDE.md` imports this file.
Cage runs Claude Code, Codex CLI and OpenCode in Docker on macOS and Linux,
with additional host and Desktop targets for Codex. It limits accidental host
filesystem damage; selected mounts, credentials, bridges and state sync cross
that boundary.

## Working on a change

Use the user's requested outcome as the scope. Inspect the affected code and
its tests, implement the change, validate it and fix failures it causes through
the completion gate below. Make routine implementation decisions and continue
without asking for approval at each reversible local step. Preserve unrelated
user changes; use an isolated worktree when practical.

Load documentation for the part you are changing, using the map below. A small
edit does not require reading the entire reference or historical progress log.
When resuming work, inspect the current branch/diff and the relevant checkpoint;
read older entries only to resolve a concrete dependency or uncertainty.

## Non-negotiable contracts

- Keep `cage` a Bash 3.2-compatible bootstrap and host Python compatible with
  Python 3.12+. Launch policy belongs in `cage_core`; compatibility frontends
  delegate to it. Validate the complete immutable launch plan before image,
  bridge, synchronization or target effects; keep pure policy code side-effect free.
- Central `config.toml` is required and stores environment variable names and
  paths, never secret values. Preserve validation and redaction in public
  diagnostics. Do not reintroduce legacy Cage configuration discovery.
- The resolved preset is the authoritative MCP allowlist: only selected
  servers may start, including across trust transitions and Desktop reconnects.
  Preserve fail-closed inventory and passthrough checks across all targets.
- Protect runtime-owned session/history/database/memory/cache state and its
  explicit synchronization contracts. Host Codex import allows static configuration and
  separately governed credentials; never overwrite project state from the
  shared host directory. Preserve OAuth leases and reconciliation rules.
- Host read-only mounts remain read-only. Host bridges, extra writable mounts,
  auth/session writeback and monitor adoption are explicit capabilities;
  preserve their selection, isolation and lifecycle boundaries.
- Storage cleanup uses exact managed candidates and race rechecks. Never
  broaden it to prune volumes, containers, referenced images or unrelated data.
- Security claims must match effective behavior. Netgate is an HTTP/HTTPS
  proxy, and host execution/bridges can bypass it. Read the affected section of
  [SECURITY.md](SECURITY.md) when changing trust boundaries; do not describe
  Cage as containment for hostile code.
- Treat tracked files, commits and release logs/assets as public. Use generic
  examples; keep credentials, private configuration and unredacted diagnostics
  outside the repository.

## Task-specific references

| When working on | Start here |
| --- | --- |
| CLI usage, configuration or auth | [README](README.md#usage); [command examples](docs/DEVELOPER_REFERENCE.md#build-and-run) |
| Launch policy, targets or state ownership | [Python launcher ADR](docs/adr-002-python-host-launcher.md); [architecture](docs/DEVELOPER_REFERENCE.md#architecture) |
| MCP, OAuth, skill packs or passthrough | [authoritative MCP selection](README.md#authoritative-mcp-selection); [detailed contracts](docs/DEVELOPER_REFERENCE.md#detailed-contracts); [HTTP MCP flows](docs/DEVELOPER_REFERENCE.md#remote-http-mcp-servers) |
| Desktop lifecycle or SSH | [Desktop guide](docs/CODEX_DESKTOP.md) |
| Images or base/leaf changes | [shared-base ADR](docs/adr-001-shared-base-image.md); `Dockerfile*`, `docker-compose.yml` |
| Netgate or host bridges | [security model](SECURITY.md); [architecture](docs/DEVELOPER_REFERENCE.md#architecture); [Netgate rules](docs/DEVELOPER_REFERENCE.md#netgate-management) |
| Storage or Token Monitor | `cage_core/storage.py`, `cage_core/monitoring/`; [component boundaries](docs/adr-003-monitor-config-boundaries.md); [monitor provider split](docs/hardening/monitor-provider-split.md); related tests |
| July hardening packets | [workflow](docs/hardening/WORKFLOW.md), relevant [progress](docs/hardening/PROGRESS.md) and [migration](docs/hardening/MIGRATIONS.md) entries before editing |
| Releases, CI or installer | [maintainer release process](README.md#maintainer-release-process), `scripts/publish_release.py`, `.github/workflows/` |

## Validation and local authority

For iteration, run checks that can detect the change's failure modes, then
broaden when shared contracts or failures justify it. A documentation-only
edit needs link/content/diff review, not a new behavior test. Security fixes
need a regression that fails on the old behavior.

Use Python 3.12+ and `requirements-dev.txt`. Run focused Python checks with
`python3 -m pytest -q tests/test_<area>.py`, or the suite with
`python3 -m pytest -q`. The default suite uses temporary fixtures and mocks;
real Docker smoke tests are opt-in with `CAGE_RUN_DOCKER_SMOKE=1` (see
`tests/test_docker_smoke.py`). Local fixture tests and disposable test resources
are authorized: run, repair and rerun affected checks without a separate
approval. This does not authorize destructive cleanup of user state,
credential changes, history rewriting, force-pushes or production changes.

Release checkpoints retain the full suite, Python/shell syntax, Compose,
secret/public-content and required integration gates. The canonical publisher
runs its local gates and owns CI/public validation; avoid duplicating a healthy
publisher's checks or polling. After a failure, diagnose that condition and
rerun the affected validation or resume the same publisher journal.

## Completion and publication

Every tracked Cage change, including docs and tests, is release-bound unless
the user explicitly requests local-only work or no publication. A review-only
request does not require changes or a release. Explicit user scope overrides
these workflow defaults; “test first” alone is not a no-publish instruction.

Keep one coherent pushed commit per version. Bump `CAGE_VERSION` in `cage`,
update `CHANGELOG.md`, record the checkpoint in `docs/hardening/PROGRESS.md`,
and update `docs/hardening/MIGRATIONS.md` for user-visible or breaking changes.
Integrate the validated commit on clean `main`, then use the
[canonical publisher](README.md#maintainer-release-process) with Python 3.12+.
Standing owner authorization covers ordinary in-scope publication; supply the
publisher's exact `release v<VERSION> from <12-character-SHA>` confirmation
without a redundant approval request. Manual publication is emergency recovery
only, as documented in the release process.

Complete release-bound work at `public_verified`: exact remote commit, passing
CI, immutable tag, GitHub Release, assets, images and public installer verified
by that publisher. Report version, commit and public evidence after checking
`git status --short`. Do not ask the owner to test a local candidate as the
normal handoff. For explicit local-only work, report the validated diff as
unpublished `prepared` work. If an external blocker prevents completion,
report the failed phase and the action needed to resume; do not claim success.
