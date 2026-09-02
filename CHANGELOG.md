# Changelog

All notable Cage changes are recorded here. Breaking or recovery-sensitive
details live in the linked migration guide.

## Unreleased

## 0.35.2 — 2026-09-02

- add `cage mcp login|logout --auth AUTH NAME` for Codex OAuth MCPs, so one
  explicitly named auth directory and its refresh-token lease can be renewed
  without resolving an arbitrary project preset; Cage accepts the direct form
  only when every Codex preset selecting that server under the auth agrees on
  its URL, resource, client, and scopes, otherwise it fails closed and retains
  the existing explicit-preset command;

## 0.35.1 — 2026-09-02

- serialize Cage Codex sessions that select an OAuth MCP per resolved host
  Codex directory, including host-native, container, and Desktop targets plus
  `cage mcp login/logout`, so a concurrent process cannot spend a stale
  in-memory refresh token after another session rotates it; a conflicting
  launch now fails clearly and separate auth directories retain independent
  concurrency;

## 0.35.0 — 2026-08-28

- replace per-launch all-volume monitor polling with a host-wide,
  cross-process coordinator: launches refresh only their current exact volume,
  reuse private fingerprint-bound snapshots for unchanged peers, and perform a
  bounded wall-clock full reconciliation once per hour; explicit `monitor sync`
  remains the forced full-reconciliation and repair path, and exit refreshes
  only the current volume;
- progressively promote an exact unchanged recovered volume's display label to
  the real project basename plus target without changing its logical/project
  identity, fingerprint, cached history, or totals; replacements and ambiguous
  claims remain explicit-adoption cases;
- add private provider-upload generations, last-good payload rollback, and
  resumable repair markers for the Token Monitor v0.49.0 per-device ingest API;
  partial failures never delete or zero an unrelated provider device;
- pin the collector to official Token Monitor v0.49.0 and Tokscale 4.14 with
  verified commit/archive digest, while retaining the network-disabled,
  exact-subpath read-only collector boundary;
- keep mixed-model sessions' token counts while leaving their cost unpriced
  unless per-model component or authoritative cost evidence is sufficient;
  Cage never allocates aggregate input/output/cache tokens across models.

## 0.34.4 — 2026-08-28

- automatically reuse an exact, unchanged recovered Codex volume during a
  normal launch, so the host collector starts and uploads without a manual
  `cage monitor sync`; conflicting labels, replacements, and competing
  registrations still fail closed;

## 0.34.3 — 2026-08-28

- classify missing Docker volume subpaths before shortening daemon diagnostics,
  so long recovered volume names do not turn an empty scan into a hard error;

## 0.34.2 — 2026-08-28

- make the collector's private scan tmpfs writable by its unprivileged UID/GID,
  so older volumes with both session directories absent are scanned as empty
  inputs without changing the read-only volume mounts;

## 0.34.1 — 2026-08-28

- handle Docker daemon diagnostics that report a missing `sessions/` or
  `archived_sessions/` directory through the volume `_data` path, so recovered
  older volumes scan as empty inputs without widening the read-only mount;

## 0.34.0 — 2026-08-28

- split the host-owned Token Monitor upload into readable provider devices such
  as `cage-openai-api-mac-…` and `cage-zllm-mac-…`, after cross-volume session
  deduplication; missing or multi-provider sessions stay in `unattributed`;
- add `cage monitor split --dry-run`, provider-aware status output, and
  provider-qualified private pricing without sending the hub secret or custom
  prices into the coding container;
- discover all `codex-state-*` volumes and explicitly adopt recovered volumes
  by exact name, without starting a coding container or inventing a repository
  path;
- make the migration from the old unsplit Cage device resumable and fail-safe:
  provider uploads and per-device hub totals must reconcile with the old hub
  device before exact legacy deletion;

## 0.33.0 — 2026-08-28

- add `cage storage maintain` as a preview-only command with an explicit
  `--apply` mode for noninteractive, scheduler-friendly cleanup of exact safe
  image candidates;
- recognize `io.cage.lifecycle=ephemeral` only when paired with Cage-managed
  image identity, a terminal label history, exact Cage-owned tags, no container
  reference, and the configured age; normal state volumes and unknown images
  remain outside automatic cleanup;
- add the 168-hour `ephemeral_min_age_hours` storage policy and mark CI smoke
  images as ephemeral so future test artifacts do not require case-by-case
  review;

## 0.32.3 — 2026-08-28

- use Token Monitor's per-period session window markers when restoring
  archived details, so repricing refreshes cannot move older sessions into the
  current day's totals;

## 0.32.2 — 2026-08-28

- prevent Docker from copying the collector image's root-owned empty
  directories into Codex volumes during read-only Token Monitor scans;

## 0.32.1 — 2026-08-28

- accept the empty Token Monitor summary that a newly registered Codex volume
  produces before its first session, while continuing to reject incomplete
  details for non-empty periods;
- prevent the expected first-run monitor race from displaying a warning over
  the Codex input interface;

## 0.32.0 — 2026-08-27

- replace opaque per-volume Token Monitor devices with one readable Cage
  installation device and one project per registered Codex volume;
- aggregate every active volume before one hub upload, deduplicate identical or
  monotonic session copies, attribute cross-volume history to `Unattributed`,
  and preserve the last good hub snapshot on incompatible copies;
- report estimated cost, pricing coverage, missing model prices, and duplicate
  counts; add private custom pricing commands for model aliases;
- add an explicit verified and resumable migration that deletes only exact
  legacy device IDs after the new aggregate device is visible on the hub;

## 0.31.2 — 2026-08-27

- make the interactive Token Monitor secret prompt work in terminals where
  `/dev/tty` cannot be written, retrying with a safe prompt stream and giving
  clear `--secret-stdin` guidance when no interactive prompt is available;

## 0.31.1 — 2026-08-27

- repair Token Monitor hub requests with a valid no-redirect handler, strict
  authenticated stats-shape checks, and redacted HTTP error diagnostics;
- enforce an explicit Token Monitor summary-field allowlist and reject source
  paths before any collector result can reach the hub;
- restrict plain HTTP monitor hubs to literal private/loopback addresses and
  make `monitor forget` operate only on a local registration, leaving a
  disabled tombstone if remote deletion fails;
- fail closed when Docker lacks `volume-subpath`, create empty missing scan
  directories safely, disable unrelated Token Monitor probes, and retire
  Desktop monitor registrations after target removal;
- carry the upstream Token Monitor MIT notice in the source release.

## 0.31.0 — 2026-08-27

- add an optional host-owned Token Monitor integration for accumulated Codex
  token totals across Cage Container and Desktop volumes; each logical Cage
  target is one stable hub device, parallel sessions sharing a volume are
  serialized, and host-native Codex, Claude, and OpenCode remain excluded;
- collect only `sessions/` and `archived_sessions/` through a short-lived,
  pinned Token Monitor v0.48.0 image with Docker `volume-subpath`, no network,
  read-only source mounts, bounded resources, and a host-side authenticated
  uploader;
- add private monitor connection/identity/registry state, CLI and TUI controls
  for connect, disconnect, status, sync, explicit volume adoption, and forget,
  with replacement-volume detection and hub device records preserved until
  explicit forget;
- publish and verify the fifth managed `token-monitor` image alongside the
  existing Cage images.

## 0.30.2 — 2026-08-26

- make profile-pinned AWS CLI settings first-class reusable-preset and TUI
  fields; legacy auth-level AWS settings remain a compatibility fallback, and
  Claude Bedrock auth retains its own AWS profile/region settings.

## 0.30.1 — 2026-08-26

- refresh the OpenCode image contract for the current upstream binary minifier,
  still requiring the fixed `1455` OAuth callback assignment and all existing
  isolation markers.

## 0.30.0 — 2026-08-26

- add profile-pinned host AWS CLI access for container agents, preserving the
  host AWS SSO/browser flow while keeping profile and configuration overrides
  fail-closed; see `docs/hardening/MIGRATIONS.md`.

## 0.29.0 — 2026-08-17

- require Python 3.12 or newer for the host control plane and maintainer
  publisher, replace the Python 3.11/3.12 CI matrix with one Python 3.12 lane,
  and retain the complete Docker, Desktop SSH, OpenCode, installer, packaging,
  and release-validation gates without pinning an exact Python patch version.

## 0.28.3 — 2026-08-17

- designate the canonical publisher as the single ordinary-release
  orchestrator across fresh contexts: avoid mandatory duplicate dry runs,
  routine external workflow polling, manual release phases, and redundant
  post-publication verification; resume its journal and use its milestone output
  plus final schema-v2 evidence instead.

## 0.28.2 — 2026-08-17

- make the maintainer handoff contract explicit: local validation produces only
  a prepared release candidate, while completion requires the exact remote-main
  commit, successful CI, immutable tag and GitHub Release, independent public
  verification, and a fresh unauthenticated `curl` install before product-owner
  acceptance testing begins.

## 0.28.1 — 2026-08-13

- make container UID/GID remapping collision-safe and fail closed for Claude,
  Codex, and OpenCode, including Ubuntu 24.04 images whose pre-existing
  `ubuntu:1000:1000` account previously blocked the common Linux host mapping;
- route each OpenCode OAuth callback listener to its matching loopback port and
  publish the required ports even when policy-approved global flags precede an
  `auth login`, `providers login`, or selected `mcp auth` command;
- add a post-image-publication release-workflow gate that verifies all four managed
  GHCR version and `latest` tags from a fresh empty Docker credential directory,
  checks exact promoted digests plus `amd64`/`arm64` manifests, and performs
  literal anonymous pulls; document the one-time per-package visibility and
  source-association action for maintainers;
- update eight pinned GitHub workflow actions to their validated current major
  releases while retaining hosted-runner and artifact-flow compatibility;
- coalesce each leaf image's recursive home-permission normalization into the
  same layer that installs Claude Code, Codex, or OpenCode, removing the
  metadata-only copy of the complete tool tree while preserving the existing
  owner/mode distribution and runtime UID/GID remapping behavior;
- apply the same single-layer construction to `cage update` overlays and keep
  root-owned entrypoints outside the world-writable installed-tool trees;
- avoid `COPY --chmod` or other BuildKit-only syntax, retaining legacy Docker
  builder, multi-architecture, and source-install compatibility.

## 0.28.0 — 2026-08-09

- add OpenCode as a container-only third assistant, with a dedicated managed
  image, persistent per-repository state, controlled updates, TUI/configuration
  support, Cage-labelled `--auto`, and fail-closed passthrough policy;
- freeze bounded host and project OpenCode configuration before launch, remove
  inherited MCP definitions, reconcile only Cage-selected local/remote MCPs,
  keep plugins disabled by default with `--pure`, and verify the final MCP
  inventory before execution;
- hand proxy, provider, GitHub, bridge, identity, and selected environment
  values to OpenCode through a private launch file rather than Docker
  `Config.Env`;
- synchronize the selected provider store and selected MCP OAuth entries with
  private locking and compare-and-swap conflict checks while keeping sessions,
  history, indexes, and caches volume-local;
- give only the OpenCode container an ephemeral executable `/tmp` tmpfs required
  by its native TUI renderer, retaining `nosuid`/`nodev` and leaving Claude and
  Codex tmpfs behavior unchanged;
- extend storage, Compose, deterministic archives, CI candidates, attestations,
  release promotion, and anonymous verification to the fourth managed image;
  candidate manifests now require schema 2 and exact entries for `base`,
  `claude-code`, `codex`, and `opencode`.

## 0.27.4 — 2026-08-09

- restore current ChatGPT/Codex Desktop SSH-host startup by accepting the
  launcher's exact `features.code_mode_host=true` app-server override while
  continuing to reject every other feature, plugin, MCP, project, or unknown
  passthrough configuration root; host and ordinary container launches remain
  unchanged and fail closed for the same override.

## 0.27.3 — 2026-08-09

- preserve same-project parallel launches in IDE and sandboxed terminals that
  provide an interactive stdin but deny direct `/dev/tty` access; the container
  collision menu now falls back to that PTY and no longer recommends forcibly
  deleting a running container when no interactive input exists.

## 0.27.2 — 2026-08-05

- make the maintainer release command TTY-safe: only its explicit publication
  confirmation may read the terminal, while every spawned Git, GitHub, Docker,
  curl, test, and installer process receives closed stdin, no controlling TTY,
  and a bounded timeout whose cleanup terminates descendants;
- make public verification tolerate bounded GHCR/GitHub propagation failures,
  cap and retry anonymous image pulls, persist cumulative phase timings and
  redacted check diagnostics across resumes, and include full release-asset
  digests plus failure details in schema-v2 JSON output;
- verify the source SPDX SBOM attestation separately from source provenance and
  record the first live candidate-promotion timing evidence and trade-offs for
  issue #6.

## 0.27.1 — 2026-08-05

- make the host-native storage-bypass coverage independent of a runner's
  pre-existing Cage configuration directory.

## 0.27.0 — 2026-08-05

- add a validated top-level `[storage]` policy and immutable launch-plan
  evidence with 20 GiB warning/build defaults, a 5 GiB critical default, two
  retained semantic versions per managed image role, and a 24-hour dangling
  image age;
- add portable Docker capacity and image-usage inventory, `cage storage status`,
  and explicit `CLEAN`-confirmed cleanup whose exact candidates exclude every
  volume, container, referenced image, unrelated image, legacy unlabeled Cage
  image, and custom derived tag;
- enforce storage warning, cleanup, and abort decisions before container and
  Desktop launch effects, fail closed for critical noninteractive launches and
  builds, and leave host-native execution unchanged;
- label local, Compose, update-overlay, candidate, and promoted release images
  with managed Cage role/version identity, and expose transactional storage
  policy editing in the TUI.

## 0.26.9 — 2026-08-01

- make the maintainer release verifier portable across Docker installations by
  reading public GHCR index digests and platforms from the Registry API instead
  of requiring the optional Docker Buildx plugin;
- exercise the public installer with a genuinely absent destination directory,
  matching a first-time install while retaining the installer's refusal to
  replace an unrecognized directory, and preserve both stdout and stderr in
  bounded failure diagnostics.

## 0.26.8 — 2026-07-31

- verify annotated release tags from the remote direct and peeled refs instead
  of the checkout-local tag ref, which GitHub Actions can materialize as a
  lightweight ref for a tag-triggered workflow.

## 0.26.7 — 2026-07-31

- add maintainer-only, deterministic, resumable release automation
  (`python3 scripts/publish_release.py`, with `--dry-run` and `--json`):
  preflight validation, one explicit confirmation, automatic phase resume,
  exact-SHA workflow selection, immutable annotated tagging, and independent
  public-release verification; private state and logs live under the
  per-worktree Git dir behind an exclusive `fcntl.flock` lock;
- publish immutable `candidate-<full-commit-sha>` images (base, claude-code,
  codex) from `ci.yml` on a successful `main` push after every existing gate
  passes, with BuildKit SBOM, `provenance: mode=max`, signed GitHub
  provenance attestations, and a `release-candidate-<SHA>` manifest artifact;
  candidate tags are public, write-once, serialized per SHA, and never
  referenced by Cage's pull logic;
- refactor `release.yml` into four stages — exact-commit gate, source package,
  image promotion, and GitHub Release — so the tag workflow verifies the exact
  CI run and candidate attestations and promotes exact candidate digests to the
  version and `latest` tags instead of rebuilding; the duplicated
  Python/macOS/Docker/history-scan jobs are replaced by the verified CI run
  while the archive-content secret scan is retained;
- fix the release automation's curl usage: `curl --no-config` is not a valid
  option and made every GHCR registry probe and anonymous release-asset download
  fail; use first-position `-q` (`--disable`) in the shared `ghcr-status.sh`
  helper and the publish command's anonymous downloads, with regression tests
  that drive the real curl argument parser;
- close the remaining fail-closed gaps: candidate and immutable-version-tag
  existence is now decided by an authoritative GHCR registry status code (new
  shared `.github/scripts/ghcr-status.sh`: 200 present, 404 absent, anything
  else fails closed) instead of parsing `imagetools inspect` error text or exit
  codes, so a commit SHA containing "404", a credential-helper/network "not
  found", or a registry 401/403/timeout/5xx can never be mistaken for an absent
  tag and authorize overwriting an immutable candidate or version tag;
- further harden after a second review: commit reconstruction restores
  canonical, umask-independent permission bits (executables stay executable);
  the candidate resolve step fails closed on ambiguous registry errors
  (401/403, timeout, 5xx) so only an authoritative not-found authorizes
  candidate creation; and the idempotent release rerun validates release
  metadata and compares each existing asset's size and SHA-256 against the
  generated artifact instead of trusting filenames alone;
- harden that automation and those workflows after review: candidate
  publication is now truly write-once (an existing candidate is verified by
  platform and `ci.yml` attestation for the exact SHA and reused, or fails
  closed, with builds conditionally skipped so a rerun never overwrites an
  immutable candidate); image attestation verification uses the required
  `oci://` reference form; public verification performs an anonymous
  `docker pull` (native-platform layers) and runs the publicly fetched installer
  with curl configuration disabled and all credentials stripped; GitHub Release
  creation is idempotent; the reproducibility check rebuilds from the recorded
  commit via read-only `git archive`; and malformed public artifacts become
  structured failed checks (with dependent checks skipped) instead of
  tracebacks;
- this is maintainer tooling only: it is not added to the `cage` CLI and is
  excluded from the release archive. There is no user configuration migration.

## 0.26.6 — 2026-07-30

- replace ADR-001's unverified clean/warm build-time estimates with step-level
  observations from the successful v0.26.1, v0.26.2, and v0.26.3 release
  workflows;
- record that shared-base leaf builds were 63–70% shorter and aggregate image
  build work fell by 14–30%, while the serial base prerequisite left observed
  cold release-pipeline wall time 5–34% longer;
- explicitly record that no cross-run warm-cache timing exists for the current
  release workflow because it uses fresh hosted runners without a persistent
  BuildKit cache;
- complete ADR-001's evidence criteria and synchronize the repository record
  with the completed closure of issue #3.

## 0.26.5 — 2026-07-30

- refactor the 2,691-line host launcher into a Bash 3.2-compatible bootstrap
  and a Python 3.11 standard-library core with typed request, resolved-config,
  and immutable launch-plan boundaries;
- add a versioned, strictly validated, secret-redacted `resolve-json` contract;
- validate the complete launch plan before any Docker, bridge, state-sync, or
  target side effects and centralize reverse-order lifecycle cleanup;
- centralize Codex passthrough and MCP suppression policy across host,
  container, and Desktop execution while keeping runtime inventory outside the
  pure policy layer;
- package and integrity-check `cage_core` in source installs, release archives,
  CI syntax gates, and the Codex image. Public CLI and configuration behavior
  remain unchanged.

## 0.26.4 — 2026-07-28

- **Breaking:** `mcp_packs` is now the authoritative allowlist for every Cage
  session. Only MCP servers selected by the resolved preset may start; inherited
  servers from user, profile, project, system, and plugin configuration layers
  are disabled and disclosed. An absent or empty `mcp_packs` selection means
  zero active MCPs. See `docs/hardening/MIGRATIONS.md`.
- build the MCP inventory in the launching runtime — host binary for
  `target=host`, the container `codex mcp list --json` for container launches
  (entrypoint, after configuration import), and a per-connection inventory for
  Desktop — supplemented by direct profile/project layer parsing; loaded
  servers receive `enabled=false`, while direct-only untrusted definitions get
  a same-kind inert transport plus `enabled=false` so launch remains valid
  before and after repository trust;
- reject caller profile (`-p`/`--profile`), working-directory
  (`-C`/`--cd`), and feature (`--enable`/`--disable`) overrides across host,
  container, and Desktop paths; restrict `-c`/`--config` to an explicit
  runtime-only root allowlist so no later argument can change MCP/plugin
  discovery after inventory; reject `--remote` app-server handoff to an
  uninventoried runtime and `--ignore-user-config` removal of an inventoried
  transport layer (`--` still preserves following positional payload);
- keep Desktop selected-MCP authorization metadata root-owned and
  non-replaceable by the remote Codex user;
- stop merging host `~/.claude.json` MCP definitions for Claude; reconcile the
  volume `mcpServers` to the selected set only and always mount a private
  read-only `.mcp.json` overlay that suppresses repository MCP definitions;
- disclose `MCP policy: selected packs only` and selected servers in
  `config explain`, `config doctor`, and the TUI; host resolution and
  container/Desktop launch output disclose terminal-escaped suppressed names;
- fail closed when a trustworthy MCP inventory cannot be obtained.

## 0.26.3 — 2026-07-28

- record actual multi-architecture registry image sizes for the shared base
  image design (ADR-001) from published v0.26.2 images on ghcr.io;
- confirm all seven base-layer digests are identical across the base,
  claude-code, and codex image manifests on both amd64 and arm64;
- correct ADR-001 units (MiB), prior-deduplication baseline (Ubuntu rootfs
  was already shared in v0.26.1), and storage-deduplication claims (manifest
  reference identity, not proven GHCR physical deduplication);
- identify chmod layers (213.1 MiB combined amd64) as a future optimization
  target with explicit upper-bound caveat;
- clean-build and warm-cache timings remain unmeasured — that acceptance
  criterion stays open.

## 0.26.2 — 2026-07-28

- add an always-discoverable macOS **Manage Desktop targets** screen to the
  main TUI, independent of the current folder's resolved preset;
- show live registered-target status and provide start/recover, restart, recent
  logs, stop-with-history, refresh, setup, and confirmed destructive removal
  without requiring lifecycle commands to be remembered;
- use a versioned, bounded, non-secret JSON target-list interface between the
  lifecycle helper and TUI instead of parsing human-readable command output;
- prevent Mac sleep and long scheduler pauses from being mistaken for a dead
  Desktop supervisor while retaining active-time fail-closed heartbeat expiry;
- build the Claude and Codex images from one agent-neutral shared base while
  keeping users, entrypoints, agent binaries, and OpenSSH in their respective
  leaf images;
- publish the multi-architecture base with the same SBOM/provenance controls as
  the leaf images, and include `Dockerfile.base` in source installs and release
  archives so local pull-fallback and rebuild flows remain self-contained.

## 0.26.1 — 2026-07-28

- remove maintainer-specific validation paths, aliases, runtime identifiers,
  provider names, and approval details from current public content;
- replace provider-derived documentation and test fixtures with neutral
  examples without changing runtime behavior;
- add checksum-pinned Gitleaks full-history gates to CI and releases, plus an
  extracted-source-archive scan before publication;
- add narrow, reviewed false-positive exceptions, public-evidence hygiene
  regressions, and common local credential-file ignore guards;
- retain older commits and release assets unchanged, with their low-sensitivity
  metadata recorded as accepted residual risk rather than claiming erasure.

## 0.26.0 — 2026-07-27

- add a macOS-only Codex `desktop` target backed by ChatGPT Desktop's SSH-host
  workflow, with a persistent repository/preset-specific Cage container,
  volume, client identity, and pinned container host key;
- add `cage desktop setup|start|restart|status|stop|logs|list|remove`, automatic
  ChatGPT launch, `--no-open`, and mutually exclusive `--desktop`,
  `--container`, and `--host` overrides;
- keep SSH listener-free through an installed-launcher `ProxyCommand`, start
  `sshd` once per connection, and disable passwords, forwarding, tunnels, root
  login, user environment files, and public listeners;
- reuse Cage's mounts, Netgate, MCP and host-command bridges, skill selection,
  identities, OAuth reconciliation, and volume-owned Codex history;
- make the detached supervisor own bridge lifetime and a private Unix control
  socket, stop fail-closed after supervisor or required-bridge loss, and recover
  stale labeled containers without deleting their volumes;
- keep provider, proxy, and bridge secrets out of Docker metadata and the
  Desktop volume through a short-lived private handoff and tmpfs-only remote
  environment, while scrubbing the persistent watchdog process;
- manage one transactional SSH Include while preserving the user's config
  comments, permissions, and symlink target; require explicit confirmation
  before deleting a Desktop target's keys, history, metadata, and volume.

## 0.24.1 — 2026-07-23

- replace blocking TUI text prompts with visible, editable fields that support
  immediate Escape cancellation, cursor editing, long values, and clearing;
- separate typed confirmations from scrollable risk and preflight details;
- preserve menu and checkbox focus, keep selected rows visible, and add
  conventional navigation keys for long screens;
- make optional fields and inherited Claude history sync consistently
  clearable, while showing command-line network and yolo overrides accurately;
- clarify launch persistence, default customization to the explicit
  remember-this-project choice, and require confirmation before overwriting a
  named reusable configuration;
- add regression coverage for UI input behavior, project-specific yolo
  persistence, one-shot launches, both tools' yolo arguments, and
  `--no-yolo` precedence.

## 0.24.0 — 2026-07-22

- add a standard-library curses launcher and configuration UI: bare `cage`
  opens it for the current project, while `cage PATH` remains a direct launch;
- let users launch once, remember a hidden project-owned configuration, or save
  a named reusable configuration without editing TOML manually;
- manage all central configuration object types with generated summaries,
  dependency-aware rename/delete behavior, preflight warnings, and dedicated
  reviews for high-authority settings;
- add transactional, concurrency-checked, comment-preserving TOML mutations,
  atomic replacement, source-mode preservation, and ten private rolling
  backups;
- support persisted preset `yolo`, with explicit `--yolo` and `--no-yolo`
  taking launch-time precedence;
- keep the TUI before every Docker, bridge, sync, and volume operation, and add
  cancellation plus Codex/Claude state-preservation regression gates;
- include the new TUI in source installs and reproducible release archives.

## 0.23.8 — 2026-07-20

- enforce the Codex host-import allowlist inside the file and directory copy
  helpers before they resolve or remove any volume destination;
- require imported profile configuration names to be safe single basenames,
  rejecting path traversal before destination construction;
- fail closed if a future caller attempts to import runtime-owned sessions,
  archived sessions, history, SQLite state, logs, memories, goals, caches, shell
  snapshots, or any other unsupported name;
- expand unit and real-Docker release gates to verify the complete resumable
  state set remains byte-for-byte unchanged under conflicting host state.

## 0.23.7 — 2026-07-20

- copy Codex `rules/` configuration without preserving the host UID, avoiding
  an entrypoint failure when Cage intentionally lacks `CAP_FOWNER`;
- strengthen the Docker entrypoint regression with a deterministic host/source
  UID mismatch;
- supersede the failed, unpublished `v0.23.6` release attempt while retaining
  all of its Codex history, host-token, and supply-chain corrections.

## 0.23.6 — 2026-07-20

- preserve Codex sessions, history, SQLite indexes, logs, memories, and caches
  as per-repository volume state instead of replacing them from the selected
  shared host Codex directory on every launch;
- repair legacy host token-command definitions by de-duplicating an exact caller
  argument suffix, recommend executable-only definitions such as `command =
  "ztoken"`, and surface fixed-argument use through `cage config doctor`;
- add regression coverage for volume-owned Codex history and host-command
  argument compatibility;
- pin every GitHub Actions dependency to an immutable commit and enable weekly
  Dependabot refreshes for those pins;
- build the source release archive deterministically, publish an SPDX SBOM, and
  sign both provenance and SBOM attestations through GitHub;
- attach SBOM and max-level provenance metadata to both multi-architecture
  container images, plus a signed GitHub provenance attestation;
- gate release-workflow changes with tests for immutable action references,
  required supply-chain metadata, and reproducible archive contents.

## 0.23.5 — 2026-07-18

- fixed unauthenticated public installs on macOS Bash 3.2, where expanding an
  empty optional GitHub-auth header array under `set -u` aborted version lookup;
- preserve optional `GH_TOKEN`, `GITHUB_TOKEN`, and `gh auth token` support while
  issuing the public release request without an auth argument when none exists;
- gate CI and releases on the installer safety suite running under macOS's
  system `/bin/bash`, so Bash 3.2 compatibility remains continuously checked.

## 0.23.4 — 2026-07-18

- fixed Codex startup after OAuth synchronization when private state is owned
  by the remapped container user and root intentionally lacks `CAP_FOWNER`;
- normalize sensitive-file modes through a no-follow descriptor as the mapped
  owner instead of widening the main container's Linux capabilities;
- reject symlinked, hard-linked, non-regular, or concurrently replaced
  sensitive Codex state without following the path to another mount.

## 0.23.3 — 2026-07-18

- fixed Codex launches on macOS Docker/Colima contexts that do not share the
  host `/var/folders` temporary directory with their VM;
- moved Docker-bind-mounted OAuth and project `.mcp.json` staging into Cage's
  canonical private config directory, while retaining mode-restricted files,
  automatic cleanup, and the read-only project overlay;
- reject a Cage config directory located inside the repository or another
  read-write Cage mount, preventing writable aliases to private staging files.

## 0.23.2 — 2026-07-16

- supplied the repository explicitly to `gh release create` in the checkout-free
  final release job;
- made real-Docker integration smoke tests run in both Python matrix jobs after
  GitHub unexpectedly skipped the previous conditional step. v0.23.1 published
  both versioned container images but did not create its GitHub Release object.

## 0.23.1 — 2026-07-16

- fixed CI and release setup by pointing `setup-python`'s pip cache at the
  repository's actual `requirements-dev.txt` dependency file. Version 0.23.0
  remains an unreleased tag because its workflows stopped before running tests
  or publishing artifacts.

## 0.23.0 — 2026-07-16

### Security and correctness

- stopped rewriting repository `.mcp.json` files and replaced that behavior with
  a validated private read-only overlay;
- reconciled generated Claude/Codex auth and MCP state so preset switches remove
  stale authority without requiring manual credential provisioning;
- hardened Codex OAuth rotation with validated, identity-bound, conflict-aware
  synchronization and no writable host credential mount in helper containers;
- authenticated and bounded Netgate, MCP, and host-command transports with fresh
  per-launch credentials;
- added Netgate DNS-rebinding/SSRF defenses, request and prompt limits, fixed
  CONNECT ports, and portable authenticated Docker host-gateway access;
- changed host bridge execution to explicit argv, `shell=False`, a sanitized
  host `PATH`, startup-pinned executables outside every Cage-writable mount,
  minimal environment, framed status where applicable, and process-group cleanup;
- isolated every host Python control-plane launch from repository `PYTHONPATH`
  and made root entrypoint writes treat persistent model-owned symlinks as unsafe.

### Configuration, installation, and supportability

- added strict central-config schema and transport-name validation, safer custom
  header rules, capability-oriented explain/doctor output, and a minimal starter
  preset;
- made source and release installation staged, ownership-checked,
  checksum-verified, atomic, and rollback-capable;
- added Python 3.11/3.12 CI, real-Docker smoke tests, release ordering gates,
  focused adversarial regression suites, a security model, and durable hardening
  records;
- corrected product language: Cage reduces accidental filesystem blast radius,
  while readable credentials, writable Git metadata, proxy bypass, and enabled
  host integrations remain explicit Developer/Host-integrated risks.

### Breaking changes

Bridge command parsing/protocol, strict configuration validation, generated-state
cleanup, Netgate restrictions, OAuth reconciliation rules, and installer
ownership checks can affect existing setups. Follow the
[0.23.0 migration guide](docs/hardening/MIGRATIONS.md#0230--2026-07-16).
