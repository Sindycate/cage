# Cage Hardening Progress

This is the durable execution log for `WORKFLOW.md`. Keep entries concise and
evidence-based. Newest entries go first.

## 2026-07-24 — v0.25.0 host-native Codex CLI execution target

Added an explicit preset execution target with `container` as the
backward-compatible default and Codex-only `host` execution as an acknowledged
no-isolation option. `--host` and `--container` are launch-only overrides and
the TUI reviews the effective target, yolo, and network state without persisting
command overrides.

The host branch runs before Docker, volume, bridge, synchronization, or image
side effects. It uses the resolved host `CODEX_HOME`, pins the Codex executable
outside the repository, applies Git/SSH/GitHub identity process-locally, and
fails closed on unsupported network policies, MCP/skill packs, host-command
bridges, extra mounts, custom agent registries, SSH aliases, missing SSH keys,
and unresolved requested GitHub authentication. Documentation states that this
is host-native Codex CLI, not ChatGPT desktop or an SSH-connected container.

Local evidence after independent correction and review: the focused
host-execution suite passes (`57 passed`); the complete suite passes (`223
passed, 6 skipped`); all six opt-in real-Docker smoke tests pass; shell syntax
passes under the active Bash and macOS `/bin/bash`; Python compilation,
Compose validation, file modes, and `git diff --check` pass. Remote publication
and artifact verification remain required before the release is considered
complete.

## 2026-07-23 — v0.24.1 TUI correctness and navigation correction

Corrected the published v0.24.0 terminal UI without changing the central TOML
schema, private launch decision, Docker orchestration, or runtime-state
boundaries. Text input is now a visible prefilled editor with immediate Escape
cancellation and unambiguous clearing; typed confirmations use a dedicated
field below scrollable review details; menus keep their selected row visible;
and checkbox/editor focus remains stable.

Persistence choices now describe their exact effects and initially highlight
the explicit remember-this-project action. Named overwrites require review,
inherited Claude history sync can be restored, and command-line network/yolo
overrides are displayed as fixed overrides. Regression coverage exercises the
input/navigation primitives, launch-once non-mutation, exact-project yolo
persistence, both tools' yolo arguments, explicit `--no-yolo`, cancellation,
and existing byte-for-byte Codex/Claude state preservation.

The v0.24.0 release workflow completed successfully with Python 3.11/3.12,
macOS Bash 3.2 installer, real-Docker state, reproducible package,
multi-architecture image, provenance, and public-installer verification. Local
v0.24.1 evidence before publication: the complete suite passes on Python 3.11
and 3.12 (`166 passed, 6 skipped`), all six opt-in real-Docker smoke tests pass,
and shell/Python syntax plus diff checks pass. Release evidence will be recorded
in the release handoff.

## 2026-07-22 — v0.24.0 transactional curses configuration launcher

Implemented a standard-library curses control plane over the existing central
configuration backend. The launcher runs before Docker inspection, bridge
startup, session/OAuth synchronization, and volume operations, and returns a
private mode-0600 launch artifact that is revalidated by `cage-config.py`.

Configuration writes use typed operations, dependency-aware renames and
deletes, an opening SHA-256 concurrency check, a private sidecar lock,
parse/schema/reference validation, semantic render comparison, atomic
replacement, source-mode and symlink-target preservation, and ten private
rolling backups. Only edited objects are canonicalized; untouched tables and
comments remain byte-preserved. High-authority saves and launches receive a
dedicated risk review.

State boundary evidence includes a pseudo-terminal cancellation test proving
Docker is not invoked, isolated byte-for-byte Codex and Claude state manifests
across config saves, the existing fail-closed Codex import fixtures, and opt-in
real-Docker tests that run both entrypoints twice against the same persistent
state after a transactional UI save. Release publication evidence remains
complete. Final local evidence: the complete suite passes (`147 passed, 6
skipped`), all six opt-in real-Docker smoke tests pass, shell/Python syntax and
diff checks pass, and the reproducible archive test includes the TUI payload.

## 2026-07-20 — v0.23.8 fail-closed Codex runtime-state import invariant

Post-recovery review confirmed that `v0.23.7` prevents the reported overwrite,
but its destination restriction lived only in the caller's static import list.
The copy helpers themselves would still remove an arbitrary destination if a
future caller passed one directly.

Defense in depth:

- enforce the exact supported file allowlist inside `copy_host_codex_entry`
  before destination resolution or removal;
- reject empty, special, or path-containing file names before the profile-file
  pattern is evaluated;
- permit only `rules/` inside `copy_host_codex_directory`, likewise before any
  destination mutation;
- reject unsupported names with a clear launch error, preserving the original
  volume entry;
- expand isolated and real-Docker coverage across sessions, archived sessions,
  history and session indexes, SQLite databases/WALs, logs, memories, goals,
  caches, and shell snapshots under conflicting shared-host state;
- retain CI and tagged-release execution of the real entrypoint fixture.

Local evidence: all managed-state tests pass (`11 passed`), the complete suite
passes (`125 passed, 5 skipped`), all five real-Docker smoke tests pass, and all
14 installer/supply-chain tests pass. Shell/Python syntax, workflow/dependabot
YAML, Compose, version, and diff checks also pass. Publication and public-
installer evidence remain required for the `v0.23.8` release.

The preceding `v0.23.7` CI and release workflows completed successfully, and
the public curl installer was independently verified to install `cage 0.23.7`
from its checksum-verified GitHub Release archive.

## 2026-07-20 — v0.23.6 remote validation failure and v0.23.7 correction

The `v0.23.6` tag triggered both CI and the release workflow, but neither
published a release. Their Linux Docker smoke job reproduced a capability and
ownership mismatch hidden by the local macOS bind-mount implementation:
`cp -a` assigned imported Codex `rules/` entries to the host runner UID, then
failed to restore their permissions because Cage deliberately omits
`CAP_FOWNER`.

Correction candidate:

- copy the allowlisted `rules/` tree recursively without preserving host
  ownership, then retain the existing remapped-user recursive chown;
- make the Docker regression stage `/host-codex` with a deliberately different
  numeric owner so the failure is deterministic across host platforms;
- use the next immutable release version, `v0.23.7`; do not move or reuse the
  failed `v0.23.6` tag.

Required evidence remains a passing complete local suite, passing Docker smoke
suite, successful remote CI/release jobs, and a verified public installer
archive reporting `cage 0.23.7`.

## 2026-07-20 — P1-A/P1-B Codex state and token-command regressions in verification

Reported regressions:

- Codex history disappeared from the repository-specific resume list after the
  0.23.4/0.23.5 upgrade;
- a custom provider using the host `ztoken` bridge began returning an upstream
  `400` response complaining that `realm` was missing.

Root causes:

- the 0.23.4 hardened host-state copy removed every same-named destination
  before import, so shared-host sessions, history, SQLite indexes, logs,
  memories, and caches could replace the per-repository volume's runtime state;
- the 0.23.0 bridge correctly began forwarding caller arguments, but the
  documented legacy token command already embedded `token -n codex`, so newer
  Codex auth configuration could supply the identical suffix a second time.

Correction:

- narrowed Codex host import to documented static configuration surfaces
  (`config.toml`, profile files, global AGENTS guidance, hooks, and rules);
  `auth.json` and `.credentials.json` retain their existing explicit policies,
  while all resumable/runtime state remains volume-owned;
- retained general host-command argument forwarding but de-duplicated only an
  exact caller suffix already present after the configured executable;
- changed the recommended token bridge to `command = "ztoken"` and added a
  `cage config doctor` warning for definitions with fixed arguments;
- documented that the correction prevents further replacement but cannot
  reconstruct files already removed by a prior launch. Affected volumes must be
  preserved for a separate read-only-first recovery attempt.

Evidence:

- focused managed-state, bridge, and configuration suites pass (`61 passed`);
- the complete suite passes (`124 passed, 5 skipped`);
- all five opt-in real-Docker smoke tests pass, including a new actual-entrypoint
  case with conflicting host/volume sessions, history, and SQLite state;
- Python and shell syntax, workflow/dependabot YAML parsing, Compose validation,
  and `git diff --check` pass;
- no personal Cage configuration or existing history volume was edited during
  the correction; runtime inspection/recovery remains separately approval-gated.

Required before returning P1-A/P1-B to complete:

- independently review the host-import allowlist, exact-suffix compatibility
  rule, tests, and recovery guidance;
- restore GitHub authentication, publish a new version/tag, and verify a normal
  custom-provider launch plus persistent history across two launches;
- record remote release and runtime evidence here. Until then, no hotfix release
  is claimed.

## 2026-07-20 — P2-C supply-chain hardening in verification

Implemented locally:

- replaced every remote GitHub Actions moving tag with a verified full commit
  pin and added weekly Dependabot updates for the pinned revisions;
- extracted source packaging into a deterministic Python builder with an
  explicit payload, normalized ownership/timestamps, stable ordering, and a
  timestamp-free gzip header;
- added an SPDX SBOM for the source archive plus signed GitHub provenance and
  SBOM attestations;
- enabled BuildKit SBOM and max-level provenance for both multi-architecture
  images and added a signed GitHub provenance attestation for each image digest;
- made the final release job re-check the downloaded archive checksum and SBOM
  before creating the GitHub Release;
- documented verification commands and the limit that provenance and SBOMs do
  not establish artifact safety.

Local evidence:

- supply-chain and installer suites pass (`14 passed`), including byte-identical
  archives from two independent builds and rejection of non-SHA action refs;
- the complete unit suite passes (`121 passed, 4 skipped`) and all four opt-in
  real-Docker smoke tests pass;
- Python and shell syntax, workflow YAML parsing, Compose validation, and
  `git diff --check` pass;
- each pinned revision was resolved from the official action repository's
  current major-version tag before editing.

Accepted container-build boundary:

- release images intentionally resolve current coding-tool and operating-system
  packages; making those builds bit-reproducible would conflict with the current
  tool-refresh product behavior unless a separate dependency-locking design is
  introduced;
- the supported immutable identity is the pushed image digest, tied to its
  source and workflow by provenance and described by its SBOM. Version tags are
  never intentionally reused under the release policy, while `latest` remains a
  moving convenience tag;
- consumers requiring immutable deployment identity must retain the verified
  digest rather than relying on a mutable registry tag alone.

Required before P2-C is complete:

- independently review the release diff and generated-artifact boundaries;
- restore GitHub CLI authentication before publication; the 2026-07-20 check
  still reports invalid tokens for both configured accounts, so no commit, push,
  version tag, or release was claimed;
- publish one new version/tag and verify the source provenance, source SBOM
  attestation, release SBOM asset, both image attestations, and BuildKit metadata
  from the remote workflow and registries;
- record the immutable release evidence here before changing the packet state to
  `complete`.

## 2026-07-18 — v0.23.5 unauthenticated installer portability

An isolated consumer-side verification after v0.23.4 publication exposed a
pre-existing macOS Bash 3.2 incompatibility in latest-release discovery: with
no GitHub token available, expanding the empty optional header array under
`set -u` aborted the documented curl-pipe install command.

Correction:

- replace the optional array expansion with an explicit authenticated/public
  request branch;
- retain `GH_TOKEN`, `GITHUB_TOKEN`, and `gh auth token` precedence;
- add a full staged-install regression with no token and a failing fake `gh`,
  exercising the version-discovery path instead of pinning `CAGE_VERSION`;
- gate normal CI and tagged releases on the installer safety suite under the
  macOS system `/bin/bash` in addition to the existing Linux matrix.

Evidence for the release candidate:

- the documented unauthenticated install path failed before the fix and then
  installed the real public v0.23.4 archive successfully in an isolated home;
- all ten installer safety tests pass under macOS `/bin/bash` 3.2.57;
- the complete suite passes (`117 passed, 4 skipped`), all four opt-in
  real-Docker smoke tests pass, and syntax, Compose, workflow YAML, version, and
  diff checks pass;
- independent installer and workflow review returned `SHIP` with no blockers.

## 2026-07-18 — v0.23.4 remapped-owner mode correction

After v0.23.3 fixed host-to-Docker staging, a normal Codex launch reached the
entrypoint and exposed a second ownership-ordering regression. The OAuth helper
correctly stored `.credentials.json` as the host UID/GID, and the entrypoint
correctly remapped/chowned state to the Codex user, but it then ran an
unsuppressed `chmod 600` as root. Cage deliberately drops `CAP_FOWNER`, so Linux
rejected the mode change after root ceased to own the inode.

Correction:

- retain the narrower main-container capability set and normalize each
  sensitive inode through a pinned, no-follow descriptor;
- assign the opened inode to the mapped Codex user, then fork, drop to that
  owner, and apply mode `0600` to the descriptor rather than the path;
- reject symlinked, hard-linked, non-regular, or detected concurrently replaced
  sensitive files without redirecting the mode change to another mount.

Evidence for the release candidate:

- reproduced root `chmod` failure in a disposable container with Cage's exact
  CHOWN/DAC_OVERRIDE/SETGID/SETUID capability set and no `CAP_FOWNER`;
- added a real-Docker entrypoint regression that failed on the old ordering and
  now verifies credential owner/mode state, plus a negative symlink test that
  confirms an owner-mapped target outside the state directory is unchanged;
- ran the patched entrypoint successfully inside the real local v0.23.3 Codex
  image with a dummy owner-mapped credential and the macOS UID/GID shape;
- the complete suite passes (`116 passed, 4 skipped`), all four opt-in
  real-Docker smoke tests pass, and shell/Python syntax, Compose, workflow YAML,
  version, and diff checks pass;
- independent security re-review returned `SHIP` with no blocking findings.

## 2026-07-18 — v0.23.3 macOS/Colima bind-path correction

A normal post-upgrade Codex launch exposed a v0.23.x regression: the OAuth
reconciler created its private helper stage under macOS `/var/folders`, while
the active Colima Docker VM shared the user home but not that system temporary
tree. Docker therefore rejected the bind before Codex started. Canonicalizing
the path to `/private/var` was insufficient because that tree was also outside
the VM's shares.

Correction:

- stage OAuth helper exchange files under the already validated, canonical Cage
  config directory instead of the operating-system temporary directory;
- move the private project `.mcp.json` overlay to the same Docker-shareable
  directory, closing the sibling latent failure;
- reject a config/staging directory nested below the repository or a read-write
  extra mount so the container cannot mutate a read-only overlay through a
  writable alias;
- preserve mode-0700 temporary directories, mode-0600 files, normal/error
  cleanup, no writable host credential mount, and the read-only project overlay.

Evidence for the release candidate:

- the exact `/var/folders/.../cage-oauth-*` Docker error reproduced against the
  local Colima daemon, while an equivalent bind below `/Users` succeeded;
- the new regression test fails on v0.23.2 placement and passes after the fix;
- the focused OAuth and host-boundary suites pass, including cleanup after a
  failed reconciliation and project-overlay source cleanup;
- the complete suite passes (`116 passed, 2 skipped`), both opt-in real-Docker
  smoke tests pass, and shell/Python syntax, Compose, version, and diff checks
  pass;
- independent re-review found no remaining release blocker. The external
  release workflow remains required before publication is considered verified.

## 2026-07-16 — v0.23.2 final-release correction

The v0.23.1 CI, package, Codex image, and Claude image jobs succeeded. The final
release job downloaded the artifact but failed immediately in `gh release
create`; the job intentionally had no checkout and the command did not supply a
repository, leaving `gh` without Git context for repository discovery.

Correction:

- pass `--repo "$GITHUB_REPOSITORY"` to the checkout-free release command;
- remove the brittle Python-version condition that GitHub skipped and enforce
  the opt-in real-Docker suite in both Python 3.11 and 3.12 jobs;
- bump the next immutable complete-release attempt to `0.23.2` while preserving
  the already published versioned v0.23.1 container images.

## 2026-07-16 — v0.23.1 release-workflow correction

The v0.23.0 source commit and tag reached GitHub, but both CI and Release failed
inside `actions/setup-python@v5` before any project test. The authenticated job
view showed that pip caching searched for `requirements.txt`/`pyproject.toml`
instead of the repository's tracked `requirements-dev.txt`.

Correction:

- set `cache-dependency-path: requirements-dev.txt` in both CI and Release;
- bumped the next immutable checkpoint to `0.23.1`; v0.23.0 is not described as
  a completed release because no archive or container image was published;
- require the same full local gate, new commit/tag, and remote workflow/artifact
  verification before declaring v0.23.1 released.

## 2026-07-16 — v0.23.0 local release candidate verified

Checkpoint: boundary, state, network, bridge, configuration, installer, and
release-workflow hardening integrated

Evidence:

- the complete Python 3.12 suite passed (`113 passed, 2 skipped`); the skips are
  the explicitly opt-in Docker suite;
- the real-Docker integration suite passed separately (`2 passed`), covering
  authenticated container-to-host Netgate traffic and the nested read-only
  repository `.mcp.json` overlay;
- shell syntax, Python compilation, Compose validation, workflow YAML parsing,
  and `git diff --check` passed;
- a repository-wide high-signal credential-pattern scan found no candidate
  secrets;
- an independent adversarial diff review found and drove closure of host
  `PYTHONPATH` import injection, ambient/repository `PATH` executable selection,
  model-owned persistent symlink writes, and inaccurate generated trust text;
- Python 3.11 remains an enforced CI/release matrix target because it is not
  installed on the local workstation.

Release state:

- version `0.23.0` is assigned and the independent blocker re-review plus complete
  local release gate pass; publication is still pending commit, push, tag,
  workflow, and remote-artifact verification;
- resource/mount/concurrency controls, trust-mode implementation, session-sync
  hardening, immutable supply-chain identity, and architectural extraction remain
  subsequent packets rather than claims of this release.

## 2026-07-16 — P1-B host bridge packet verified

Checkpoint: selected host execution is authenticated, bounded, and observable

Implemented:

- generated independent 256-bit per-launch authentication tokens for MCP and
  host-command bridge protocols and authenticated before process spawn;
- replaced `shell=True` with startup-time `shlex` parsing and `shell=False`;
- ran commands from a trusted host-home cwd with a minimal base environment plus
  only explicitly selected forwarded variables;
- sanitized host `PATH`, excluded the repository and every normalized config/CLI
  read-write mount, and pinned the resolved executable at bridge startup;
- kept MCP JSON-RPC bytes unchanged after its bounded handshake and drained
  server stderr into Cage's private bridge log with a 1 MiB visible cap;
- replaced the host-command byte stream with bounded frames carrying argv,
  stdin/EOF, stdout, stderr, structured errors, and final exit status;
- added process, input, output, frame, handshake, and lifetime limits;
- tracked process groups and active connections so cleanup terminates descendants;
- bound authenticated listeners on all interfaces for native Linux host-gateway
  compatibility, with an internal loopback override used by tests.

Evidence:

- live local bridge suite passed outside the socket-restricted sandbox
  (`13 passed`), covering unauthorized clients, raw MCP bytes, argv injection,
  environment minimization, stdin behavior, stdout/stderr/status, output limits,
  timeouts, descendant cleanup, launcher token injection, PATH sanitization, and
  config/CLI read-write mount denial;
- bridge/config focused suite reported `60 passed` before final integration;
- Python and shell syntax checks passed for the packet.

Residual limitations:

- unauthenticated LAN clients can consume a bounded five-second handshake slot
  but cannot spawn a command; source-interface filtering or a Unix/vsock
  transport remains follow-up work;
- any process inside the selected Cage container can read the bridge token and
  invoke that explicitly enabled host capability. Host-integrated mode must make
  this authority prominent.

## 2026-07-16 — P1-A OAuth reconciliation packet integrated

Checkpoint: automatic OAuth rotation preserved with narrow, validated host writes

Implemented:

- removed every writable helper mount of the host Codex directory;
- validated host and volume credentials as regular, non-symlink, bounded UTF-8
  JSON objects and canonicalized them before comparison;
- replaced mtime selection with content hashes, per-identity revision/base state,
  explicit two-sided conflict detection, and per-volume/per-identity locks;
- bound volume sync state to the canonical selected host Codex directory so an
  account-directory switch resets from the new host source;
- used random exclusive mode-0600 temporaries, repeated compare-and-swap checks,
  atomic host replacement, and content CAS for volume application;
- ran helpers with no network, bounded memory/PIDs/time, dropped capabilities,
  and no host credential mount;
- propagated post-run sync errors without skipping other cleanup.

Evidence:

- adversarial OAuth suite passed (`7 passed`) for future/equal mtimes, mode
  repair, host/volume symlinks, malformed/oversized JSON, two-sided conflicts,
  identity switches, CAS races, and mount boundaries;
- obsolete launcher fake that did not execute the helper protocol was removed;
  its security assertion is superseded by the end-to-end adversarial harness.

Accepted Developer-mode residual risk:

- a malicious process already running as the Codex user can author a different
  valid credential JSON object that looks like legitimate refresh-token
  rotation. Distinguishing process provenance requires Strict-mode brokered
  credentials, not file validation.
- simultaneous live Codex processes can still race before reconciliation; CAS
  detects sync races but does not serialize provider writes during the run.

## 2026-07-16 — independent pre-release verification pass

Checkpoint: completed packets and release scaffolding challenged independently

Verifier-confirmed corrections:

- fixed CI omission of the ignored dependency lockfile and classified Python
  relay scripts under Python rather than shell syntax checks;
- changed release workflow permissions to job-level least privilege, disabled
  checkout credential persistence, validated both Python 3.11/3.12, packaged
  before image publication, and created the GitHub release only after both images
  succeed;
- fixed installer rollback/ownership/symlink bypasses, routed `make install`
  through the same staged implementation, and expanded behavioral tests from
  three to nine cases;
- made strict schema validation cover unused inline preset entries and newline
  serialization hazards;
- preserved dotfiles-managed config symlinks during atomic `set-project` writes;
- corrected Codex capability output so it does not claim Claude session
  writeback;
- added behavioral repository import-shadow coverage to the project MCP overlay
  launch test;
- corrected remaining README and canonical `AGENTS.md` trust-boundary claims.

Evidence at this checkpoint:

- full Python 3.12 suite passed (`75 passed`) before the latest bridge/OAuth
  packets began editing shared files;
- installer suite passed (`9 passed` after the shared source-install path was added);
- host-boundary suite passed (`4 passed`);
- config, host-boundary, and installer focused suites passed;
- shell syntax, Python compilation, `git diff --check`, Compose validation, YAML
  parsing, and release tarball-content simulation passed.

Evidence still required:

- Python 3.11 execution is delegated to CI because that runtime is unavailable
  locally;
- real Docker nested-bind and Netgate bridge smoke tests remain unavailable in
  the restricted local environment.

## 2026-07-16 — P0-C Netgate packet verified

Checkpoint: proxy exposure, SSRF, prompt injection, and resource usage bounded

Implemented:

- required an automatically injected fresh 256-bit per-launch proxy credential
  before DNS resolution, prompting, or upstream connection, allowing portable
  Docker host-gateway access without exposing a usable LAN proxy;
- resolved destinations once, rejected any non-public or mixed public/private
  answer, and connected to the validated numeric endpoint;
- restricted CONNECT to 443/8443;
- bounded request bodies, worker count, concurrent prompts, connection timeouts,
  and tunnel idle duration;
- streamed accepted request bodies and rejected ambiguous/chunked framing;
- removed AppleScript source interpolation and sanitized/bounded visible prompt
  values;
- stripped hop-by-hop/proxy credentials and rebuilt the upstream Host header.

Independent evidence:

- `pytest -q tests/test_netgate_proxy.py` passed (`17 passed`);
- the opt-in real-Docker integration suite passed (`2 passed`) against the
  local Docker daemon;
- `python -m py_compile netgate-proxy.py` passed.

Residual limitations:

- proxy environment variables remain deliberately bypassable by raw networking;
- any process inside the selected container can read and use its launch's proxy
  credential; this does not broaden its authority beyond that container's
  documented gated-network capability.

## 2026-07-16 — P0-B automated auth/state packet verified

Checkpoint: generated authorization state reconciled without manual-token setup

Implemented:

- kept automated host credential reuse as the default Developer-mode workflow;
- explicitly removed persistent Codex `auth.json` when copying is disabled or
  the current host source is absent;
- replaced append-only Codex MCP generation with an atomic, removable, marked
  block that is idempotent across launches;
- tracked Claude Cage-owned MCP entries in a private manifest, removed stale
  connectors/tokens on the next launch, and preserved/restored user entries
  shadowed by a managed server of the same name;
- changed sensitive generated files/directories to `0600`/`0700`;
- ran embedded entrypoint Python in isolated import mode;
- made malformed persistent preference/config state fail closed.

Independent evidence:

- focused entrypoint tests passed (`9 passed`), including repeat launch, preset
  removal, rotating/unset token, shadowed user server, stale auth, file mode, and
  isolated-import cases;
- `bash -n entrypoint.sh entrypoint-codex.sh` passed.

Accepted Developer-mode residual risk:

- active Claude connector tokens are materialized in the private per-repository
  volume for compatibility and remain at rest between launches. They are
  refreshed on launch and removed on the next launch when inactive. Strict mode
  will require a broker/no-reusable-secret design instead.

## 2026-07-16 — P0-A host-boundary packet verified

Checkpoint: confirmed escape paths removed

Implemented:

- stopped rewriting/backing up/restoring host `.mcp.json`;
- generated a private, mode-0600 project MCP overlay and nested-mounted it
  read-only into the tool container;
- rejected symlinked, non-regular, invalid, or concurrently replaced project MCP
  configuration;
- passed every path through `argv` and used Python isolated mode;
- isolated every host launcher/config/Netgate/bridge Python process from
  repository-controlled `PYTHONPATH` modules.

Independent evidence:

- `pytest -q tests/test_host_boundaries.py` passed (`4 passed`);
- `bash -n cage cage-netgate.sh` passed;
- no legacy backup, direct path interpolation, or non-isolated inline-Python
  pattern remains in the changed launcher/Netgate paths.

Residual verification:

- run a real Docker smoke test for the nested file bind on macOS and Linux before
  publishing the checkpoint; current regression tests use protocol-compatible
  fake bridge and Docker processes.

## 2026-07-16 — early safety and release scaffolding

Checkpoint: pre-integration supporting work

Completed locally:

- replaced inaccurate top-level isolation and yolo claims with an explicit
  current security model;
- documented automated credential reuse as an intentional usability feature and
  confidentiality tradeoff rather than removing it;
- added Python 3.11/3.12 CI and made release artifact/image jobs depend on the
  validation job;
- restricted Docker build context with an allowlist-style `.dockerignore`;
- hardened installer path validation, ownership recognition, checksum fallback,
  staged replacement, and rollback behavior;
- added three installer safety tests.

Evidence:

- `bash -n install.sh` passed;
- `pytest -q tests/test_install_safety.py` passed (`3 passed`);
- `docker compose config` passed.

Not yet integrated or released:

- packet diffs and the complete test suite still require review;
- release action dependencies remain tag-pinned rather than SHA-pinned;
- license selection remains a product-owner decision and is not assumed here.

## 2026-07-16 — workflow initialized

Checkpoint: baseline and packet decomposition
Source revision: `v0.22.5` (`292efb0`)
Branch: `codex/security-hardening`

Completed:

- recorded the product-owner requirement to preserve automated credential UX;
- established strict, developer, and host-integrated trust-model direction;
- split immediate remediation into host-boundary, auth-state, and Netgate
  packets with non-overlapping file ownership;
- confirmed the baseline worktree had only a pre-existing untracked
  `__pycache__/` directory;
- confirmed GitHub CLI authentication is currently invalid.

Prior review evidence retained:

- 37 tests passed under Python 3.12 on the baseline;
- shell and Python syntax checks passed;
- `docker compose config` passed;
- harmless tests confirmed host Python path injection and symlink-following
  restore behavior;
- no live Docker build or live bridge/network test was performed during review.

In progress:

- P0-A host-boundary fixes;
- P0-B generated auth/config lifecycle fixes;
- P0-C Netgate hardening.

Next integration gate:

- inspect each packet diff;
- run focused regression tests;
- independently attempt safe adversarial cases;
- update migrations and effective-security documentation;
- run the full suite before deciding the first version bump.

Known publication blocker:

- `gh auth status` reports invalid tokens for both configured accounts. Do not
  claim a push, pull request, tag, or release until separately verified.
