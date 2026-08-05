# Cage Hardening Progress

This is the durable execution log for `WORKFLOW.md`. Keep entries concise and
evidence-based. Newest entries go first.

## 2026-08-05 — issue #6 acceptance hardening and live timing evidence

Checkpoint: remaining locally actionable issue #6 gaps implemented for the
0.27.2 release candidate; publication and issue closure remain separate gates

The first live candidate-promotion release (`v0.26.9`, commit `a5e6cbb`) proved
the intended authorization-time speedup but also exposed three unattended-run
defects: a child command inherited the publisher's pseudo-TTY and opened Cage's
interactive launcher, public GHCR reads observed transient `latest`/platform
state, and an anonymous Docker pull could stall without a deadline. The
publisher now gives every child closed stdin, bounds external commands, retries
only idempotent public reads with fixed visible backoff, and gives anonymous
pulls two attempts inside both per-attempt and whole-check deadlines.

Resume evidence is now durable rather than best-effort display state. Matching
schema-v1/v2 journals restore only cumulative phase durations, prior redacted
checks, and observed asset digests; Git refs, workflow conclusions, phases, and
candidate digests are still reconstructed from authoritative remote state.
Failed attempts add their duration in a `finally` path. Each check is persisted
as it completes, and schema-v2 success/error JSON includes bounded redacted
details, per-phase timing, full SHA-256 plus size for all three public assets,
workflow URLs, and image digests. Public verification now separately proves the
source provenance and SPDX v2.3 SBOM attestations.

Live timing evidence (UTC, from GitHub run/release metadata):

- issue #6 baseline: `v0.26.2` tag to public release was approximately 12m30s;
- `v0.26.9` exact-commit CI run 30712870965: 18:35:26–18:47:40, 12m14s,
  including the 9m58s cold multi-architecture candidate job;
- annotated tag creation to public release: 18:47:53–18:49:41, 1m48s;
- complete main-CI-start to public release: 18:35:26–18:49:41, 14m15s;
- the comparable tag path improved from about 750s to 108s: an 85.6% reduction
  (approximately 6.9x faster).

This is exact-SHA build-result reuse, not a cross-version warm BuildKit cache.
The expensive build moved into protected branch CI and the tag workflow became
promotion-only; total cold push-to-public time did not become shorter. That
trade-off preserves fresh dependency resolution, immutable candidate digests,
SBOM/provenance generation, exact-workflow attestations, and all existing
security/reproducibility gates.

Local regression evidence currently covers real pseudo-TTY isolation, timeout
diagnostics with partial output, transient and persistent registry faults,
`latest`/platform propagation, bounded anonymous-pull retry, v1 journal
compatibility, identity-mismatched journal rejection, cumulative failed-phase
timing, redacted JSON diagnostics, full asset digests, and both source
attestation types. No commit, push, tag, release, issue edit, or GHCR mutation
has occurred in this checkpoint.

Validation evidence:

- focused publisher/supply-chain suite: `127 passed`;
- complete Python 3.12 suite: `475 passed, 8 skipped`;
- complete Python 3.11 suite: `475 passed, 8 skipped` (isolated reuse of the
  installed pure-Python pytest stack with third-party plugin autoload disabled);
- opt-in real-Docker suite: `7 passed, 1 skipped`;
- a clean temporary release commit ran the real publisher under a controlling
  pseudo-TTY with `--dry-run --json`, completed all local gates in 61.5s,
  remained at `local_ready`, emitted the exact four planned actions, and opened
  no nested prompt or remote mutation;
- shell syntax, recursive compilation, Compose validation, two fixed-epoch
  byte-identical release archives, maintainer-script exclusion, and
  `git diff --check` passed.

## 2026-08-05 — Docker storage guardrails slice implemented locally

Checkpoint: bounded P1-C/P2-B storage slice implemented without claiming either
parent packet complete

Implemented in the isolated `codex/storage-guardrails` worktree:

- added strict top-level `[storage]` defaults (20 GiB warning/build, 5 GiB
  critical, two semantic versions per role, 24-hour dangling age) and copied the
  immutable policy into schema-v2 public launch-plan evidence;
- added portable host-filesystem/Docker-overlay capacity probes, full image and
  running/stopped-container inventory, terminal managed-label classification,
  per-role semantic retention, and exact candidates;
- added `cage storage status` and TTY plus exact-`CLEAN` confirmed cleanup;
  removals are non-forced, recheck image identity and container references, and
  exclude volumes, containers, referenced images, unrelated repositories,
  legacy unlabeled Cage images, and custom derived tags;
- enforced warning/cleanup/abort policy before ordinary container and public
  Desktop effects, plus the 20 GiB floor immediately before local builds and
  tool-update overlays; host-native execution remains Docker-independent;
- added terminal managed role/version and OCI version labels to all Dockerfiles
  and passed the version through launcher, Compose, CI smoke, candidate, update,
  and digest-promotion paths;
- added transactional TUI editing, installer/package assertions, README,
  changelog, migration, and canonical architecture guidance.

Evidence:

- focused storage/config/planning/TUI/CLI/installer/supply-chain suite: `173 passed`;
- full Python 3.12 suite: `454 passed, 8 skipped`;
- available real-Docker suite: `7 passed, 1 skipped`; the dedicated cleanup
  test removed the eligible old managed tag while preserving running/stopped
  container images, a custom derived image, and a sentinel-bearing named volume;
- the real command measured 6.7 GiB free through a Docker-overlay probe,
  protected ten container image IDs, classified 48 legacy Cage images as
  report-only, and found no product cleanup candidate;
- DDH `state_5.sqlite` read-only `PRAGMA quick_check` returned `ok` with no WAL;
  three exact unreferenced 6.14 MB dangling test images were removed across the
  repeated real-Docker gates, while the
  1.83 GB dangling image referenced by a created container, the DDH container,
  and `codex-state-DDH-4c3ad5ff` remained;
- created a private mode-0600 DDH volume archive (85,203,660 bytes;
  SHA-256 `23fcd4c9464e862952c51d2f6f3fd642f35295061b0d4441c3d30657c5e979fa`)
  before maintenance;
- stopped and restarted the existing Colima instance with `disk: 150`,
  increasing `/var/lib/docker` to 148 GiB with 58 GiB available; online trim
  released another 623.4 MiB of guest blocks and the macOS data volume reported
  127 GiB available;
- repeated the immutable read-only DDH SQLite check after restart and after the
  normal launch (`quick_check=ok`, 188,416-byte database, no WAL);
- completed the final normal TTY Cage smoke against DDH: it pulled
  `codex:0.26.9`, applied selected-only MCP suppression, started the container
  path, and Codex printed its help successfully;
- final `cage storage status` measured 55.9 GiB free, protected eight container
  image IDs, and reported 49 legacy unlabeled Cage images without cleanup
  candidates.

No commit, merge, push, tag, release, or packet-status promotion has occurred.

## 2026-07-31 — issue #6 review hardening (round 4)

Fixed the last P1 from the fourth review pass. The focused suite is now 55
publish-command tests + 42 supply-chain tests (97 total); the full-suite failure
set is still byte-identical to a pristine `dddc15d` baseline (zero regressions).

- P1: `curl --no-config` is not a valid curl option (curl rejects the `--no-`
  negation of the non-boolean `--config`), so every registry probe and anonymous
  download failed: `ghcr_status` always returned 000 (failing candidate creation
  and version promotion closed) and the public-release asset/installer downloads
  could not complete. Replaced all occurrences with first-position `-q`
  (a.k.a. `--disable`) in `.github/scripts/ghcr-status.sh` (token + manifest
  calls) and `scripts/publish_release.py` (`curl_download` and the public
  installer fetch).
- Added regression tests that exercise the REAL curl argument parser (a fake
  runner accepts any argv): one captures the actual curl commands built by
  `curl_download` and the public-installer fetch; one executes the real
  `ghcr_status` helper and probes each captured curl invocation. Both include a
  control asserting real curl rejects `--no-config`, and both were verified to
  fail when the bug is reintroduced.

## 2026-07-31 — issue #6 review hardening (round 3)

Closed the two remaining fail-closed gaps from the third review pass (both P1).
The focused suite is now 54 publish-command tests + 41 supply-chain tests (95
total), and the full-suite failure set is still byte-identical to a pristine
`dddc15d` baseline (zero regressions).

- P1: candidate not-found detection no longer matches bare substrings (`404`,
  `not found`) in Docker's free-form error output, which a commit SHA containing
  "404" or a credential-helper/network message could spoof. A new shared helper
  `.github/scripts/ghcr-status.sh` queries the GHCR registry HTTP API and
  branches on the structured status code: 200 = present, 404 = authoritatively
  absent (the only result that authorizes creating a write-once tag), and
  anything else (401/403/timeout/5xx/000) = ambiguous and fails closed.
- P1: the same authoritative absence check now guards immutable version tags in
  both the release gate and the promotion step. Previously a failed
  `imagetools inspect` (e.g. a registry 503) was treated as "tag absent" and
  reached `imagetools create`, risking replacement of an existing immutable
  version tag. Now only an authoritative 404 creates; a matching digest is
  resumable success; a conflicting digest or any ambiguous failure fails closed.
- Covered by executing the real candidate resolve and promotion blocks against
  stubbed curl/docker/gh (bash functions) for genuine absence (404 -> create),
  401/403/timeout/5xx (-> fail closed, never reaching `imagetools create`),
  matching digest (-> resume), and conflicting digest (-> fail closed), plus
  false-positive regressions: a SHA containing "404" with a simulated 401, and a
  credential-helper error containing "not found".

## 2026-07-31 — issue #6 review hardening (round 2)

Addressed the second review pass (2 P1, 1 P2). The focused suite is now 54
publish-command tests + 30 supply-chain tests (84 total), and the full-suite
failure set remains byte-identical to a pristine `dddc15d` baseline (zero
regressions; the sandbox's noexec/reserved-path failures are environmental).

- P1: `safe_extract_tar` now restores canonical permission bits (directories
  0755, executables 0755, other files 0644, special bits stripped). `git
  archive` writes tar modes as `(0666|0777) & ~umask` rather than the tracked
  index mode, so preserving `member.mode` verbatim would make commit
  reconstruction depend on the maintainer's umask; canonicalizing from the
  executable bit is umask-independent and matches the release workflow's
  checkout. Covered by a focused mode test and a real-git, real-packager
  byte-for-byte reconstruction test (the canned fake packager cannot catch
  this).
- P1: the candidate resolve step now fails closed on ambiguous registry errors.
  It captures the inspect result and only an authoritative not-found
  (`not found` / `manifest unknown` / `404`) authorizes candidate creation;
  authentication (401/403), timeout, network, and registry 5xx failures stop
  the job so a CI rerun can never overwrite an immutable candidate tag whose
  existence could not be confirmed. Covered by executing the real resolve script
  against stubbed `docker`/`gh` (bash functions) for not-found, 401, 403,
  timeout, 5xx, verified-reuse, and unverifiable-attestation scenarios.
- P2: the idempotent GitHub Release rerun path now validates release metadata
  (non-draft, non-prerelease, matching tag) and downloads each existing asset to
  compare its size and SHA-256 against the freshly generated artifact; empty,
  truncated, or different files under the right names are rejected rather than
  accepted as recovered.

## 2026-07-31 — issue #6 review hardening

Addressed the issue #6 implementation review (1 P0, 3 P1, 3 P2). Every fix is
covered by new adversarial tests; the focused suite is now 52 publish-command
tests + 23 supply-chain tests (75 total), and a pristine `dddc15d` baseline run
from the same location produced a byte-identical pre-existing failure set
(zero regressions).

- P0: image attestation verification now passes the required `oci://` image
  reference to `gh attestation verify` in both `release.yml` and
  `scripts/publish_release.py` (a bare `ghcr.io/...` argument is interpreted as
  a local file path and would block every release).
- P1: candidate publication is truly write-once — a new resolve step verifies an
  existing candidate (amd64/arm64 platforms plus a `ci.yml` provenance
  attestation for the exact source SHA and `refs/heads/main`) and reuses its
  digest, or fails closed; the base/leaf build and attest steps are
  conditionally skipped for reused images, so a CI rerun can never overwrite an
  immutable candidate with freshly resolved mutable dependencies.
- P1: the public-installer check fetches `install.sh` anonymously from the
  published tag (not the local checkout) with `curl -q` (curlrc disabled) and runs it with
  all GitHub credential variables and `gh` configuration stripped, so it cannot
  fall back to a maintainer token.
- P1: verification converts any uncontrolled exception (malformed JSON, tar
  errors, I/O) into a structured, redacted failed check and skips checks whose
  prerequisite did not pass, instead of emitting a traceback; the unsafe
  unfiltered `extractall` fallback was replaced by explicit per-member safe
  extraction.
- P2: anonymous GHCR verification performs a real `docker pull` (native-platform
  layer downloads) under a fresh credential dir, not merely a manifest inspect.
- P2: GitHub Release creation is idempotent — a rerun verifies an existing
  release carries exactly the expected assets and resumes, failing closed on a
  conflicting asset set.
- P2: the reproducibility check rebuilds the archive from the recorded commit
  materialized via read-only `git archive`, not the live checkout, so a changing
  worktree during the long workflow wait cannot invalidate the reconstruction.

## 2026-07-31 — issue #6 deterministic, resumable release automation

Implemented the maintainer-only release command `python3
scripts/publish_release.py` (`--dry-run`, `--json`; Python 3.11 standard
library only). It validates the prepared release commit, asks for one explicit
confirmation (`release v<VERSION> from <12-char-SHA>`), pushes `main` if
needed, waits for the exact commit's CI run, pushes an immutable annotated tag,
waits for the release workflow, and independently verifies the public release.
Phases (`local_ready` → `main_pushed` → `ci_passed` → `tag_pushed` →
`release_workflow_passed` → `public_verified`) resume automatically; remote
state is authoritative and the per-worktree Git-dir state file is only a hint,
guarded by an exclusive `fcntl.flock` lock with `0700`/`0600` modes and atomic
updates. Subprocesses run without a shell; logs are bounded and secret-redacted.

CI now publishes immutable `candidate-<full-SHA>` images (base, claude-code,
codex) on a successful `main` push after every existing gate passes, with
BuildKit SBOM, `provenance: mode=max`, signed GitHub provenance attestations,
and a `release-candidate-<SHA>` manifest artifact; candidate tags are public,
write-once, serialized per SHA, and never referenced by Cage's pull logic.
`release.yml` was refactored into four stages (exact-commit gate, source
package, image promotion, GitHub Release): the gate verifies the exact CI run
and candidate digests/platforms/attestations and protects manual tag pushes;
promotion moves exact candidate digests to the version and `latest` tags
without rebuilding; the release is created last. The duplicated
Python/macOS/Docker/history-scan jobs were replaced by the verified CI run; the
archive-content secret scan was retained. No cross-version BuildKit cache was
introduced.

Validation evidence (sandbox; Docker not installed here):

- `tests/test_publish_release.py`: 44 tests passed (preflight/validation,
  dirty tree / wrong branch / divergence / multiple unpublished commits,
  annotated/lightweight/mismatched tags, confirmation mismatch, dry-run issues
  no mutating commands, exact-SHA workflow selection rejecting branch-latest
  evidence, all six resume phases, ambiguous-push recovery, CI failure
  preventing tags, post-tag failure never moving/deleting tags, candidate and
  version digest conflicts, exclusive locking and atomic private journal,
  bounded/redacted logs, deterministic secret-free JSON, and an end-to-end
  command-ordering test against a temporary bare Git remote with fake gh/docker
  /curl that reaches `public_verified`);
- `tests/test_release_supply_chain.py`: 21 tests passed, extended to assert
  candidate gating/full-SHA tags/exact base digest/SBOM+provenance+
  attestations, gate exact-CI and candidate-attestation verification, promotion
  rather than rebuild, version-before-latest ordering, release created last,
  SHA-pinned actions, and that the archive excludes maintainer-only `scripts/`;
- `python3 -m compileall` and `bash -n` passed across the listed targets;
- a pristine checkout of the same commit run from the same location produced an
  identical pre-existing failure set, confirming no regressions (the sandbox's
  Docker-dependent and reserved-path/noexec failures are environmental and are
  covered by CI).

This is maintainer tooling only: it is not added to the `cage` CLI and is
excluded from the release archive. There is no user configuration migration.
No push, tag, release, issue edit, or GHCR mutation was performed; the work is
delivered as an uncommitted working tree on branch
`codex/issue-6-release-automation` for review. The first authorized real
release using this mechanism is the end-to-end acceptance test and must record
phase timings against the v0.26.2 baseline before claiming any speedup.

## 2026-07-30 — v0.26.6 ADR-001 release timing evidence

Replaced unverified ADR-001 build-time estimates with step-level evidence from
the successful v0.26.1, v0.26.2, and v0.26.3 release workflows. Shared-base
Claude and Codex leaf steps were 63–70% shorter than the independent builds,
and aggregate image-build work fell by 14–30%. The serial base prerequisite
left observed cold pipeline wall time 5–34% longer, so the earlier estimate of
a 40% cold wall-clock reduction was withdrawn.

The release workflow uses fresh hosted runners and configures no persistent
BuildKit cache; inspected logs contained no cached build steps. Cross-run
warm-cache timing is therefore recorded as not applicable to the shipped
workflow, not inferred from overall workflow duration. Residual risk is limited
to unquantified performance if persistent caching is introduced later; such a
change requires its own cold/warm benchmark.

Issue #3 was updated with this evidence and closed as completed on 2026-07-28.
This release synchronizes the repository ADR, changelog, and durable progress
record with that decision; it does not change image architecture or runtime
behavior.

## 2026-07-29 — P3 host launcher modularization complete

Replaced the 2,691-line Bash host launcher with a 28-line Bash 3.2-compatible
bootstrap and a Python 3.11 standard-library core. The bootstrap resolves its
real installation directory, validates Python, enters isolated mode, and
rejects symlink/non-regular package entries before import.

The core now has typed `LaunchRequest`, `ResolvedConfig`, immutable
`RuntimeConfig`, and `LaunchPlan` boundaries. Resolution and redacted,
versioned JSON serialization occur before target side effects. Runtime command,
MCP, skill, identity, and state inputs are frozen into the plan; environment
values and other secrets are resolved only at process creation and are never
serialized. Host, ordinary container, and Desktop adapters consume the same
plan. OAuth and Claude session reconciliation are dedicated state adapters, and
a lifecycle coordinator owns immediate registration, reverse cleanup, bounded
TERM/KILL, readiness, and primary-status precedence.

Codex passthrough and MCP suppression decisions now have one pure policy
implementation. A separate runtime adapter owns file inspection and Codex
inventory execution. `entrypoint-codex.sh` and `codex-remote.py` delegate to
the packaged helper instead of carrying independent policy copies. Both host
bridge frontends share environment allowlisting, command parsing, executable
pinning, authentication, process-group tracking, and shutdown infrastructure.

Compatibility frontends remain for configuration, TUI, Desktop management,
bridges, container entrypoints, and remote Codex. The legacy shell-assignment
emitter and every launcher `eval` consumer were removed. Source/release
installation, the reproducible archive, CI syntax gates, and the Codex image
now package and validate `cage-main.py` plus `cage_core`.

Validation evidence:

- Python 3.12: `333 passed, 7 skipped` outside the separately privileged bridge
  suite; all 14 authenticated bridge tests passed with real loopback sockets;
- Python 3.11.14: 339 non-bridge tests passed with 7 optional skips, and all 14
  bridge tests passed;
- all seven real-Docker smokes passed against a current local Codex image,
  including optional Desktop SSH/secret-handoff coverage;
- all 28 installer/release focused tests passed, including macOS system Bash
  3.2 source install, generated-release install parity, package symlink
  rejection, deterministic archive bytes, and checksum validation;
- recursive Python compilation, shell syntax, `docker compose config`,
  public-evidence/secret-pattern scans, and `git diff --check` passed;
- `cage` is 28 lines and contains no Docker construction, heredocs, trap chains,
  state logic, or launch policy.

P3 is complete and versioned as v0.26.5. The tag-triggered release workflow is
the publication gate.

## 2026-07-28 — v0.26.4 authoritative MCP pack selection

Made `mcp_packs` the authoritative allowlist for every Cage session. Cage now
inventories the inherited MCP servers in the launching runtime (`mcp list
--json`, supplemented by direct profile/project TOML parsing because `codex mcp
list` does not enumerate those layers) and disables every inherited server the
preset did not select with highest-precedence overrides. Loaded servers receive
`enabled=false`; direct-only profile/project definitions receive a same-kind
inert transport plus `enabled=false`, avoiding Codex's transport-less
`invalid transport` failure before repository trust and remaining authoritative
if trust is granted in the same process. The inventory runs in the runtime that eventually launches: the host
binary for `target=host`, the container `codex` in `entrypoint-codex.sh` after
configuration import for container launches, and `codex-remote.py` on every
Desktop app-server connection (so a live project MCP added after the supervisor
started is still suppressed). Inventorying in the runtime is required for
correctness: disabling a server that exists only on the host but not in the
image would fail Codex config load with `invalid transport`. Caller profile,
working-directory, and feature overrides are rejected across host, container,
and Desktop paths, and `-c`/`--config` uses an explicit runtime-only root
allowlist so no caller argument can add a post-inventory MCP/plugin layer.
Remote app-server handoff is rejected because that runtime was not inventoried;
`--ignore-user-config` cannot remove an inventoried transport layer; the `--`
delimiter still preserves following positional payload. Desktop selected-MCP
metadata is root-owned
outside the remote user's writable runtime directory. For Claude, the entrypoint
no longer merges host `~/.claude.json` MCP definitions, reconciles the volume
`mcpServers` to the selected set only, and the launcher always mounts a private
read-only `.mcp.json` overlay (selected bridged servers only) that suppresses
repository MCP definitions. `config explain`, `config doctor`, the TUI review,
and launch output disclose `MCP policy: selected packs only`; suppressed names
are terminal-escaped before display. Inventory failure fails closed.

Evidence: the authoritative MCP, entrypoint, host-boundary, host/Desktop,
configuration, and TUI suites pass (`250 passed`). Coverage includes the
reported `node_repl` reproduction, real-Codex untrusted-to-trusted project
behavior, transport-complete direct-layer suppression, every supported
`-c`/`--config` argument shape (including quoted-key escapes), fail-closed
profile/cwd/feature/remote/user-config argument guards with `--` delimiter
handling, per-connection Desktop inventory, root-owned non-replaceable Desktop
policy state, net-off Claude overlays, malformed inventory/layers, and selected-name conflicts. The
real-Docker suite passes all six ordinary smokes; its optional Desktop smoke
also passes against a disposable image containing the patched entrypoint and
remote wrapper. The complete suite reports `330 passed, 7 skipped, 1 failed`;
the remaining release-archive assertion is the pre-existing local checkout
mode mismatch (`cage` is mode `0700` on disk while Git records `0755`), not a
content or index-mode change in this worktree.

## 2026-07-28 — v0.26.3 ADR-001 registry measurements recorded

Published v0.26.2 registry image sizes measured via OCI distribution API:
base 150.9 MiB (amd64), claude-code 316.5 MiB, codex 545.4 MiB. All seven
base-layer digests confirmed identical across all three images on both
architectures. Corrected earlier claims: units are MiB not MB; v0.26.1
already shared the Ubuntu rootfs layer (~29 MiB); matching digests prove
manifest reference identity, not GHCR physical blob deduplication. Clean-build
and warm-cache timings remain unmeasured; that acceptance criterion is still
open. chmod layers (213.1 MiB combined) noted as upper-bound optimization
target requiring a prototype.

## 2026-07-28 — v0.26.2 release candidate shared base-image integration

Implemented ADR-001 with one agent-neutral `Dockerfile.base`, thin Claude and
Codex leaf Dockerfiles, version-coupled local fallback builds, and a
multi-architecture release base that receives BuildKit SBOM/provenance metadata
plus signed GitHub provenance. Agent binaries, users, entrypoints, and
Codex-only OpenSSH remain outside the base; existing leaf registry paths and
update behavior remain unchanged.

Integration review added the base Dockerfile to source installs and
reproducible release archives, restored portable source modes, fixed the direct
unittest entry point so shared-base tests cannot be skipped, and made normal CI
build the base before its Codex smoke leaf.

## 2026-07-28 — v0.26.2 release candidate Desktop lifecycle TUI

Added a top-level macOS Desktop target manager to the ordinary Cage TUI. It
discovers registered targets through a versioned, bounded, non-secret JSON
interface rather than the current folder's resolved preset. The selected
target's stored repository and preset drive start/recover, restart, logs,
stop, and exact-alias-confirmed removal, preventing a mismatched project
mapping from creating or operating on a different persistent volume.

Changed the remote watchdog from wall-clock heartbeat age to active polling
progress. A Mac sleep or scheduler gap starts a fresh grace window after wake;
an unchanged or missing heartbeat still exits fail-closed after 45 active
seconds. The installed TUI discovered the pre-existing Desktop target from an
ordinary container configuration without restarting or replacing its volume.
After reconciliation onto the public v0.26.1 privacy-hardening commit, Python
3.11 and 3.12 each pass the complete suite (`287 passed, 7 skipped`), all seven
real-Docker smokes pass against the rebuilt Codex leaf, and disposable image
checks preserve agent/OpenSSH separation. Fixed-epoch v0.26.2 archives are
byte-identical, contain `Dockerfile.base`, and pass the extracted-archive
Gitleaks scan; the combined worktree also passes the neutral-public-evidence
and Gitleaks gates.

## 2026-07-28 — v0.26.1 public-repository privacy hardening

Removed maintainer-specific validation metadata from current tracked content
while preserving the underlying security evidence. Documentation, comments,
and non-functional fixtures now use provider-neutral examples. Historical
commits and the v0.26.0 archive intentionally remain unchanged; they contain
low-sensitivity maintainer metadata but no confirmed public credential.

Added a checksum-pinned Gitleaks full-history gate to normal CI and tagged
releases. Packaging now waits for that gate and scans the extracted source
archive before SBOM generation, attestation, or upload. The policy extends the
default rules with one exact-line exception for a credential-state helper and
one fingerprint-specific exception for historical private-key header strings.

The ignored local diagnostic log was confirmed absent from public Git objects
and moved to a private location outside the repository. Its audit record
contains fingerprints and classifications only, never credential values.
No credential or provider account was changed as part of this release.

## 2026-07-27 — v0.26.0 ChatGPT Desktop SSH target

Implemented a macOS-only persistent `desktop` execution target using ChatGPT
Desktop's documented SSH-host workflow. A detached per-target supervisor owns
the ordinary Cage launcher and therefore Netgate, MCP/host-command bridges,
OAuth reconciliation, Docker cleanup, a private control socket, and the
container heartbeat. Repository plus preset deterministically selects a
dedicated Codex volume, SSH client key, persistent container host key, alias,
and known-hosts file.

The generated OpenSSH block points only at the installed Cage helper through
`ProxyCommand`; there is no TCP listener. Each connection runs `sshd -i` in the
labeled container with passwords, root login, forwarding, tunnels, user
environment files, and user rc disabled. The remote Codex launcher reads only
selected provider and bridge variables from a private ephemeral `/run` file
and prepends the selected native profile and yolo setting to `codex
app-server`.

Local release-candidate evidence:

- Python 3.11 and 3.12 each pass the complete suite (`258 passed, 7 skipped`);
  all seven opt-in real-Docker tests pass, including SSH/app-server profile,
  provider, yolo, MCP, host-command, UID, state-preservation, and mount-safety
  coverage;
- the installed source build registers one idempotent top-level SSH Include,
  resolves the generated alias through the absolute installed helper, keeps
  OpenSSH listener-free, publishes no ports, and limits the Desktop-only
  additional capability to `SYS_CHROOT`;
- real SSH opens the canonical test repository, reads Git state, performs and
  removes a repository write sentinel, keeps unrelated host paths unavailable,
  and preserves the volume plus pinned host key across restart;
- a non-default provider target completes real requests with its selected model
  through Netgate, and the same target remains reachable in `off`, `open`, and
  restored `gate` network modes;
- provider/proxy/bridge values are absent from Docker `Config.Env`, PID 1,
  Cage metadata/log/SSH files, and the persistent volume; the short-lived host
  handoff is removed after readiness and the allowlisted tmpfs file is
  mode `0600`;
- shell/Python syntax, Compose configuration, staged installer tests, archive
  contents, deterministic package bytes/checksums, installed-source byte
  equality, and `git diff --check` pass.

Installed ChatGPT validation passed with the maintainer:

- ChatGPT discovered the generated `ProxyCommand` alias and added the canonical
  test repository as a remote project;
- the remote task reported the expected working directory and Git state,
  created and removed a repository write sentinel, and left no unrelated
  changes;
- the persisted session records the expected model, profile, and approval
  configuration without retaining provider credentials; its initial bubblewrap
  loopback setup failed explicitly and automatic review retried the command
  with escalation successfully;
- killing the verified detached supervisor caused the container to remove
  itself after heartbeat expiry with no remaining Netgate or target process.
  Cage detected stale metadata and recovered the same alias, pinned host key,
  volume, and session history;
- explicit `stop` disconnected SSH, removed the target container and processes,
  and preserved the alias, client key, host-key pin, volume, and history. A
  final start restored the target to `ready`.

## 2026-07-27 — v0.25.1 Codex profiles and host integration reuse

Added native Codex profile selection to Cage presets. Both execution targets
validate `$CODEX_HOME/<name>.config.toml`; container mode forwards
`--profile`, while host mode composes it with process-local MCP and skill
overrides.

Host-native Codex can now reuse selected HTTP/stdio MCP packs and default
`~/.agents` skill packs without modifying host Codex or skill-registry files.
Stdio MCP executables are parsed without a shell, pinned to absolute paths, and
rejected under the writable repository. Selected MCP names fail closed when a
base, profile, or project Codex layer already defines them. Custom agent
registries, host command bridges, extra mounts, SSH aliases, and Cage network
restrictions remain rejected in host mode.

The TUI exposes the named profile and reviews the direct host authority of
stdio MCP servers and skills. Documentation distinguishes the supported
host-native CLI path from ChatGPT desktop: the app can open a workspace and
shares base Codex configuration, but Codex does not document a desktop
named-profile launch selector. A container-backed desktop UI remains a separate
remote app-server/SSH design.

Local release evidence: the profile/host/config/TUI suite passes (`148
passed`); strict-config validation succeeds against installed Codex CLI
`0.144.6` for HTTP, stdio, OAuth, and skill overrides; the complete suite,
including live loopback bridge coverage, passes (`238 passed, 6 skipped`); and
all six opt-in real-Docker smoke tests pass against the existing Colima
instance. Python/shell syntax, Compose validation, file modes, packaging, and
`git diff --check` pass. The GitHub branch and tagged-release gates must still
pass before publication is claimed.

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

Evidence after independent correction and review: the focused
host-execution suite passes (`57 passed`); the complete suite passes (`223
passed, 6 skipped`); all six opt-in real-Docker smoke tests pass; shell syntax
passes under the active Bash and macOS `/bin/bash`; Python compilation,
Compose validation, file modes, and `git diff --check` pass. Commit `cb1b23e`
was published as tag and GitHub release `v0.25.0`; release assets and checksum
were verified.

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
