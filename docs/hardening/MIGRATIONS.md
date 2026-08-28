# Hardening Migration Guide

This file records user-visible and configuration migrations introduced by the
hardening workflow. Entries move from **Unreleased** to a concrete version only
when that version is committed and tagged.

## Unreleased

## 0.33.0 — 2026-08-28

### Scheduler-friendly safe image maintenance

Who is affected: users who periodically accumulate old managed Cage releases
or Cage test/CI images and want a host scheduler to perform the narrow cleanup
without reviewing Docker objects interactively.

Previous behavior: `cage storage clean` always required an interactive `CLEAN`
confirmation. Non-semantic test/CI images, including images with labels such as
`ci`, were not eligible for automatic classification unless they also matched
the managed semantic-version rules.

New behavior: `cage storage maintain` prints the same exact candidate preview
without changing Docker state. `cage storage maintain --apply` is the
noninteractive form suitable for a host scheduler. It can remove old
unreferenced managed image versions, old terminal Cage build leftovers, and
explicitly ephemeral images after `ephemeral_min_age_hours` (default 168
hours). Ephemeral images must carry `io.cage.lifecycle=ephemeral` in addition
to Cage-managed role identity, have terminal Cage label history, contain only
exact Cage-owned tags, and be unreferenced by every running or stopped
container.

Maintenance never removes volumes, containers, referenced images, unrelated or
legacy unlabeled images, or custom-tagged images. Existing unlabeled test/CI
images remain report-only until rebuilt with the lifecycle label. No existing
configuration or Docker state requires migration. The versioned
`cage.launch-plan` public contract increments from schema version 2 to 3 to
carry the new storage-policy field; consumers of that internal contract should
accept schema 3.

Migration and scheduling:

1. Run `cage storage maintain` once and inspect the preview.
2. If the default 168-hour retention is appropriate, schedule
   `cage storage maintain --apply` with the host scheduler. Do not schedule
   Docker prune or the interactive `cage storage clean` command.
3. To change the age, add `ephemeral_min_age_hours = N` under `[storage]` and
   run `cage config doctor PATH`.
4. Keep normal `*-state-*` volumes and third-party application images outside
   automatic deletion; they remain visible through the status/report output.

Rollback: stop scheduling `storage maintain --apply` and install the previous
release. No volumes, sessions, credentials, or repository files are changed by
this feature.

## 0.32.3 — 2026-08-28

### Preserve Token Monitor period boundaries after repricing

No user action is required. Cage now uses each archived session's own
Token Monitor `periodWindows` marker when restoring `today` and `month`
details. This keeps historical sessions out of the current period when an
upstream repricing refresh updates the archive entry's shared day or month.
Older archives without per-period markers continue to use their shared fields.

## 0.32.2 — 2026-08-28

### Preserve Codex session-directory ownership during monitor scans

No user action is required. Token Monitor collector mounts now disable Docker's
automatic volume copy-up for the exact read-only `sessions/` and
`archived_sessions/` subpaths. This prevents an empty subpath from being
repopulated with the collector image's root-owned directory and avoids the
Codex transcript permission error on an existing Cage volume.

## 0.32.1 — 2026-08-28

### Empty new-project monitor scan

No user action is required. A newly registered Codex Container or Desktop
volume can have no session files when its first monitor scan starts. Token
Monitor may omit the `sessions` object for that zero-token period. Cage now
accepts that empty representation and continues to reject missing session
details when a period reports any tokens. Existing monitor registrations and
archives are unchanged.

## 0.32.0 — 2026-08-27

### One Cage device with volume projects

Token Monitor registrations now use one stable hub device per Cage
installation. Its ID has the form `cage-local-<8 hex characters>`. Each Codex
Container or Desktop volume is a project under that device. Project IDs are
keyed hashes of Cage logical-target IDs. They do not expose repository paths.

Cage scans all active registered volumes before it performs one replacement
upload. It deduplicates identical session records. When one cumulative copy
contains every counter from another copy, Cage keeps the larger copy. A session
that exists in more than one volume is assigned to `Cage: Unattributed` because
Cage cannot prove its original repository. Incompatible copies abort the new
upload, so the hub keeps its last good snapshot.

Existing v0.31.x registry files are read without an in-place destructive
conversion. Cage retains each exact old device ID as migration state. Use:

```bash
cage monitor status
cage monitor migrate --yes
```

The migration uploads the new aggregate device first. It then reads
authenticated hub statistics and requires the new device to be present. Only
after verification does it delete the exact legacy IDs recorded in the private
registry. Each successful delete is committed separately. If the command
stops, run it again; already completed IDs are not deleted again. Do not use
`monitor forget` as a substitute for this migration.

Cost now remains part of the aggregate payload. `monitor status` reports total
estimated cost, priced-token coverage, and missing model IDs. Token Monitor's
built-in catalog remains authoritative. For private aliases that have no
catalog price, add an explicit USD-per-million rate:

```bash
cage monitor pricing set MODEL --input 1.25 --output 10 --cache-read 0.125
cage monitor pricing status
cage monitor sync
```

At least `--input` or `--output` is required. A zero rate means explicitly
free. Cage does not infer or invent missing rates. Pricing data is mode 0600
under `~/.config/cage/monitor/`; the generated Tokscale file enters only the
network-disabled collector state.

## 0.31.2 — 2026-08-27

### Token Monitor prompt compatibility

`cage monitor connect URL` now keeps the hub secret prompt usable in terminals
where `/dev/tty` is readable but not writable, and reports a safe
`--secret-stdin` fallback instead of exposing the terminal's raw write error.
There is no configuration or state migration. Existing connections and hub
secrets are unchanged.

## 0.31.1 — 2026-08-27

### Token Monitor audit corrections

The Token Monitor hub client now uses a valid no-redirect handler, requires the
authenticated `/api/stats` response to have the hub's device/period shape, and
does not copy attacker-controlled HTTP error bodies into local diagnostics.
Collector summaries are constrained to the pinned upstream sync wire fields
and reject native-session additions or obvious source paths before upload.
Plain HTTP hub URLs are restricted to literal private or loopback IP addresses;
DNS names, including `.local` names, require HTTPS.

`cage monitor forget` now marks the exact locally registered device disabled
before attempting the remote delete. An unknown device is rejected without a
hub request, and a failed remote delete leaves a disabled local tombstone. The
private archive is removed only after the exact hub delete succeeds.

Desktop target removal performs a best-effort final monitor scan while the
volume still exists and retires its local monitor registration after successful
volume removal. If Docker does not support `volume-subpath`, monitoring fails
closed with a diagnostic instead of falling back to an unscoped volume mount;
missing session directories are created as empty temporary scan directories.
The collector explicitly disables OpenCode ambient/local-limit probes and WSL
scanning. The host-side uploader remains outside the tool container's network
namespace by design, including for `--net off`; disconnect the monitor to pause
uploads.

No Codex state volume migration is required. Existing monitor registrations are
compatible; a failed `forget` may be retried after reconnecting the hub, and a
disabled registration can be explicitly adopted with `cage monitor add`.

## 0.31.0 — 2026-08-27

### Optional host-owned Token Monitor aggregation

This release adds an opt-in way to aggregate accumulated Codex token totals
from Cage's persistent Docker volumes into a Token Monitor hub. It is not a
new launch target or a replacement for Cage's isolation boundary.

Supported sources are ordinary Codex `target = "container"` launches and
Codex `target = "desktop"` targets. Host-native Codex, Claude, and OpenCode
state are never scanned. The collector runs briefly on the host's Docker
daemon using the pinned Token Monitor v0.48.0 image. It has no network, runs as
the host numeric UID/GID with dropped capabilities and resource limits, and
mounts only the exact `sessions` and `archived_sessions` subdirectories of the
selected Codex volume read-only. The host performs the authenticated hub
request; the hub secret is never passed to the collector container.
The bundled collector source is pinned to Token Monitor v0.48.0 (commit
`6121585f5d5e7fa98385f8a5ac7f8639660e4965`, archive SHA-256
`019b9dede6daa9e34a306dac0e3a6f90ca25ca900a298bb5205cdbe8a25a3cda`).

Connect a hub from a terminal (the secret is prompted on `/dev/tty` and is
never accepted as a command-line argument):

```bash
cage monitor connect https://token-monitor.example
# or for a noninteractive secret handoff:
printf '%s\n' "$TOKEN_MONITOR_HUB_SECRET" | \
  cage monitor connect https://token-monitor.example --secret-stdin
```

Plain HTTP is accepted only for literal loopback/private IP addresses. The connection URL,
secret, and interval are stored in the private
`~/.config/cage/monitor/connection.json`; identity, registration, lock, and
per-device archive state are stored beside it with mode-0600 files and
mode-0700 directories. Use `cage monitor status` to inspect the redacted local
registry and hub reachability.

Once connected, Cage automatically registers a Codex volume when that target
is launched and performs an immediate, periodic (five-minute by default), and
final scan. A registration is keyed by the host installation identity plus the
logical target (repository and, for Desktop, preset), not by a volatile volume
fingerprint. Therefore parallel Cage sessions for one repository share one
device and one serialized scan. If a volume is recreated or replaced, Cage
marks the registration `needs-adoption` and refuses to upload it until the
replacement is explicitly adopted:

```bash
cage monitor add ~/projects/myapp --preset codex-company --container
cage monitor sync
```

`add` is also the way to register a dormant target that is not currently
running. `cage monitor disconnect` removes the local hub credential and pauses
uploads without deleting registrations or the existing hub device record.
`cage monitor forget DEVICE_ID --yes` first marks the exact local registration
disabled, then deletes that exact device from the configured hub; its private
collector archive is removed only after the remote delete succeeds. Explicit replacement adoption reuses the stable device identity and
upserts the replacement volume's current summary; use the hub's own export or
retention controls if the old volume must remain a separate historical device.

Migration and verification:

1. Confirm that the selected Token Monitor hub is reachable and protected by
   its own secret; prefer HTTPS for non-loopback hubs.
2. Run `cage monitor connect URL`, then `cage monitor status` and inspect that
   only the intended Codex Container/Desktop devices are registered.
3. Launch a monitored Codex target or use explicit `monitor add`, then run
   `cage monitor sync` to force a bounded one-shot update.
4. If a state volume was intentionally replaced, use `monitor add` to adopt it;
   do not delete or edit the private registry by hand.

Rollback: disconnect the monitor and install the previous Cage release. The
Codex state volumes remain untouched. The optional `cage-token-monitor` image
may remain locally; it is a managed image subject to the normal Cage storage
retention policy.

## 0.30.2 — 2026-08-26

### AWS CLI settings belong to reusable presets

New TUI configurations set `aws_access = "host-cli"` and `aws_profile` on the
reusable preset. Existing configurations with these values on an auth block
continue to work as a compatibility fallback, but preset values take
precedence. Claude Bedrock's own `aws_profile` and `aws_region` remain valid
auth-profile settings.

## 0.30.1 — 2026-08-26

### OpenCode image contract refresh

No user configuration migration. The OpenCode image build and `cage update
opencode` overlay now recognize the current upstream binary's minified
identifier for the fixed `1455` OAuth callback without weakening the required
callback, isolation, or `--pure` contracts.

## 0.30.0 — 2026-08-26

### Profile-pinned host AWS CLI access

Container presets can opt into a host-side AWS CLI relay with
`aws_access = "host-cli"` and a non-empty `aws_profile` directly on the
selected preset. Cage creates a reserved `aws` shim in the container and
invokes the host-installed AWS CLI with the selected profile. Older
configurations with these values on the selected auth block continue to work
as a compatibility fallback, but preset values take precedence.
The host AWS configuration, SSO cache, keychain integration, and browser flow
remain on the host; no AWS credential directory or ambient AWS credential
environment variables are mounted for this relay. Use separate presets when
switching between profiles such as `aws-prod.ReadOnly`,
`aws-staging.ReadOnly`, and `aws-staging.Manual`.

This is an additive host-integrated capability, not a read-only AWS sandbox:
the selected profile's IAM permissions remain authoritative, and the host CLI
bypasses Netgate. `--net off` and host-native Codex targets are incompatible.
The relay fixes the profile and rejects profile/configuration/debug overrides,
`aws configure`, and `aws sso logout`; `aws sso login` remains available and
uses the host browser flow. Existing Claude Bedrock `aws_profile` behavior is
unchanged unless `aws_access` is also selected.

When this capability is enabled, `AWS_*` names are not allowed in the selected
preset/auth `env` lists; pass non-secret CLI settings as AWS CLI arguments or
keep them in the host profile instead.

If a non-Claude auth block or preset already contains an `aws_profile` value,
add `aws_access = "host-cli"` to opt into the new behavior, or remove the
unused value; non-Claude `aws_profile` without an access mode now fails closed
instead of being silently ignored.

## 0.29.0 — 2026-08-17

### Python 3.12 host requirement

Who is affected: users or custom automation that launch or install Cage with
Python 3.11. Container images and their persistent volumes are not migrated.

Previous behavior: the host launcher, installer, and maintainer publisher
accepted Python 3.11 or newer, and normal Linux CI repeated the test and
real-Docker suites under both Python 3.11 and 3.12.

New behavior: Cage requires Python 3.12 or newer. CI has one Python 3.12 test
lane and still runs the complete unit suite, real-Docker entrypoint suite,
shared-base/Codex/OpenCode image builds, OpenCode container contracts, Desktop
SSH smoke, syntax checks, and Compose validation. The macOS Bash 3.2 installer
job and release package job remain pinned to the Python 3.12 minor series. Cage
does not require one exact Python patch release.

Migration and verification:

1. Install a maintained Python 3.12 or newer interpreter and ensure `python3`
   resolves to it in the environment that invokes Cage.
2. Run `python3 --version`, then `cage --version`; both must succeed and the
   Python version must be at least 3.12.
3. Custom CI or packaging should remove Python 3.11 jobs and retain the Python
   3.12 unit, Docker, Desktop, OpenCode, installer, archive, and supply-chain
   gates represented by the shipped workflows.

Rollback: install the previous Cage release to restore Python 3.11 support.
Configuration, credentials, Docker volumes, images, and project state are
unchanged by this migration.

## 0.28.3 — 2026-08-17

### Maintainer publisher orchestration efficiency

No user configuration migration. `scripts/publish_release.py` is now explicitly
the single ordinary-release controller across fresh contexts and handoffs.
Maintainers run real mode once for a clean prepared commit; `--dry-run` is
optional for ambiguous state or mutation review rather than a mandatory second
preflight. A healthy publisher owns push, exact-SHA CI waiting, immutable tag,
release-workflow waiting, retries, and independent public verification. Do not
duplicate those phases with manual mutations, routine `gh` polling, registry or
installer probes, or another verification checklist. Resume its private journal
after interruption and use targeted read-only diagnostics only for a reported
failure.

## 0.28.2 — 2026-08-17

### Maintainer release handoff

No user configuration migration. Local edits, commits, tests, worktrees, and
images are now explicitly classified as a prepared release candidate rather
than a completed handoff. Agents own pre-release validation and publication.
Product-owner acceptance starts from a fresh unauthenticated `curl`
installation only after the exact commit is on remote `main`, required CI
succeeds, the immutable tag and GitHub Release exist, and the canonical
publisher reaches `public_verified`.

## 0.28.1 — 2026-08-13

### Collision-safe container identity remapping

No configuration migration. Claude, Codex, and OpenCode containers still run
as the non-root host UID/GID supplied by Cage. If an image account already owns
either requested numeric ID, the shared remapper now moves that account to the
tool account's previous free ID before assigning the requested host identity.
Invalid, incomplete, root, or unverifiable mappings fail before the assistant
starts instead of silently retaining an unwritable identity.

### OpenCode OAuth callback routing correction

No configuration migration. OpenCode provider login now forwards callback port
`1455` to the matching loopback listener instead of the MCP callback port
`19876`. Cage also recognizes policy-approved global options before provider or
selected MCP auth subcommands when deciding which fixed localhost-only callback
ports to publish.

### Public GHCR consumer gate

No user configuration migration. After image promotion and before GitHub
Release creation, the workflow now runs a `public-images` job for `base`,
`claude-code`, `codex`, and `opencode`. For both the immutable version and
`latest` tag, it requires the promoted digest, `linux/amd64` plus
`linux/arm64`, and a literal pull under a fresh empty Docker credential
directory with ambient credential variables removed.

Maintainers must make each newly created GHCR package public once through its
GitHub package settings and confirm its source-repository association. A public
source repository does not set GHCR package visibility. If the consumer gate
fails because a package is private, correct that package setting and rerun the
failed job; never move an immutable version tag or create a replacement release
for a visibility-only correction.

## 0.28.0 — 2026-08-09

### OpenCode container integration

Who is affected: users who add an OpenCode auth block or preset, and maintainers
of custom image, CI, registry, or release automation. Existing Claude and Codex
presets keep their current behavior.

New behavior: `tool = "opencode"` selects a container-only third assistant.
OpenCode auth blocks may set `host_opencode_config_dir` (defaulting to the
host's XDG OpenCode config directory), `host_opencode_data_dir` (defaulting to
the host's XDG OpenCode data directory), `copy_auth`, and the existing
`host_agents_dir`. Presets may select existing `skill_packs`, `mcp_packs`,
identities, host commands, and extra mounts. They also add
`opencode_plugins = false`; the default launches with `--pure`, while `true`
explicitly trusts global and project plugin code. OpenCode rejects `target =
"host"`, `target = "desktop"`, and `session_sync`.

Cage freezes bounded, symlink-free copies of applicable JSON/JSONC settings,
root project instructions, project-local skills, and selected host skills. It
resolves the snapshot with the exact image binary, removes inherited MCPs,
adds only selected local/remote MCP definitions, disables live project config
and external skill discovery, and aborts unless the final MCP transports and
disk skills match the frozen selection. Repository files are not rewritten.
Proxy, provider, GitHub, bridge, identity, and selected environment values use
a private mode-0600 launch-file handoff rather than Docker `Config.Env`.
Configuration-defined external instruction and skill paths/URLs fail closed;
nested instruction files remain ordinary readable repository content and are
not claimed to be hidden by the container boundary.

Provider `auth.json` is synchronized as one selected store when `copy_auth =
true`; `mcp-auth.json` is imported and merged only for selected OAuth server
names and matching configured URLs. Both use bounded duplicate-key JSON
validation, private permissions, no-follow/hardlink checks, locking, and
compare-and-swap conflict detection. Static configuration, sessions, history,
indexes, state, and cache remain volume-local. With `copy_auth = false`, stale
provider credentials are removed from that volume and no provider writeback
occurs.

`cage -y` maps to OpenCode `--auto`; raw `--auto` is rejected so Cage retains
the yolo label and gated-network default. Live server modes, project/working
directory overrides, `mcp add`, and unselected MCP auth operations fail before
Docker effects. Selected OpenCode OAuth commands run in the container through
fixed, localhost-only callback relays. Image-level contracts pin those callback
ports and the required project/skill suppression flags. Real-Docker CI proves
that provider and remote-MCP HTTP traffic presents Cage's authenticated proxy
credential and that `--net off` starts without network access.

Migration and verification:

1. Create a distinct OpenCode auth block for each provider-account store that
   must remain isolated; set both host directories explicitly when relying on a
   non-default XDG layout.
2. Create an OpenCode preset and select only the MCP and skill packs needed for
   that repository. Leave `opencode_plugins = false` unless extension code is
   intentionally trusted.
3. Run `cage config doctor --preset NAME PATH` and inspect `cage config explain
   --preset NAME PATH`; secret values and raw passthrough arguments must not
   appear in the public plan.
4. Launch with `cage opencode --preset NAME PATH`, then verify `run`, provider
   auth, selected MCPs, Git/SSH/GitHub identity, mounts, and host commands as
   applicable. Use `cage mcp login NAME --preset PRESET PATH` only for a selected
   OAuth MCP server.
5. Custom builders and release tooling must treat `Dockerfile.opencode`, image
   role/repository `opencode`, and candidate-manifest schema 2 as mandatory
   alongside `base`, `claude-code`, and `codex`.

Rollback: install 0.27.2 and stop using OpenCode presets; the older strict
schema will reject them. Do not delete `opencode-state-*` volumes during
rollback. They are intentionally preserved and can be reused after reinstalling
0.28.0. Host static configuration is never modified by OpenCode launch, and
only the explicitly selected provider/MCP auth stores may have received normal
credential rotation writeback.

## 0.27.2 — 2026-08-05

### Maintainer release reliability and evidence

No user configuration migration. The maintainer-only publisher now closes stdin
and removes the controlling TTY for all child processes, applies bounded external-command and anonymous-pull
deadlines, retries safe public reads, persists cumulative resume timing and
redacted check details, reports full release-asset digests in schema-v2 JSON,
and verifies the source SPDX SBOM attestation separately from provenance.
Schema-v1 private journals remain readable. The script is still excluded from
the release archive and does not change the Cage CLI, central configuration,
installed payload, image names, or runtime state.

## 0.27.0 — 2026-08-05

### Docker storage guardrails and narrow image cleanup

Who is affected: users launching container or Desktop targets, especially on a
Docker or Colima data disk with less than 20 GiB free, and maintainers of custom
central configuration or image-build automation. Host-native targets are not
affected.

Previous behavior: Cage did not measure Docker backing-store capacity, classify
its images, retain a bounded version history, or offer a product-owned cleanup
preview. A full Docker disk could surface later as unrelated pull/build errors
or Codex SQLite `disk I/O error` failures. Cleanup required manual Docker
commands with no Cage-specific ownership boundary.

New behavior: the optional top-level `[storage]` table has these defaults:

```toml
[storage]
warn_free_gib = 20
critical_free_gib = 5
min_build_free_gib = 20
keep_versions = 2
dangling_min_age_hours = 24
```

Container and Desktop launches probe Docker capacity before launch effects.
Interactive low-space launches offer cleanup/proceed/abort; critical launches
and builds require cleanup or abort. Noninteractive launches may proceed only
between the warning and critical floors; critical launches and builds fail
closed. An unavailable portable probe is disclosed and does not fabricate a
block. `cage storage status` shows the policy, capacity, retention, protected
container image IDs, and exact candidates. `cage storage clean` requires an
interactive TTY and the exact word `CLEAN`, rechecks references before each
ordinary `docker image rm`, and never uses force or prune.

Managed local, Compose, candidate, and promoted release images now carry
`io.cage.managed`, `io.cage.role`, `io.cage.version`, and OCI version labels.
Only matching labels plus an exact Cage repository tag authorize version-tag
cleanup. Old dangling images also require a terminal Cage identity label and
the configured minimum age. Volumes, containers, images referenced by running
or stopped containers, unrelated repositories, legacy unlabeled Cage images,
and custom derived tags are never candidates. Existing unlabeled Cage images
remain usable and report-only until replaced by a newly labeled build or pull.

Migration and verification:

1. No configuration edit is required; omit `[storage]` to use the defaults.
2. Run `cage config doctor PATH`, then `cage storage status` and inspect the
   measured source, retained versions, and exact candidate list.
3. If cleanup is wanted, run `cage storage clean`, verify the preview, and type
   `CLEAN`. Do not substitute `docker system prune` or volume-prune commands.
4. Launch one container or Desktop preset and verify the preflight behavior.
   Run a host-native preset separately to confirm it does not require Docker.
5. Custom builders should pass `CAGE_VERSION` to all three Dockerfiles; the
   shipped launcher, Compose file, CI candidate build, and release promotion
   already preserve the managed role/version labels.

Rollback: install the prior Cage version and remove `[storage]` if its strict
schema does not recognize the table. OCI labels on already built images are
harmless metadata. No state or volume migration occurs, and rollback must not
delete a per-repository Cage volume.

## 0.26.7 — 2026-07-31

### Deterministic release automation (issue #6)

No user configuration migration. `scripts/publish_release.py` and the
`ci.yml`/`release.yml` candidate-and-promotion changes are maintainer-only
release tooling. There is no change to the `cage` CLI, central `config.toml`
schema, state locations, image names, or install/update behavior. The published
image names (`base`, `claude-code`, `codex`) and the `version`/`latest` tag
semantics are unchanged; release images are now promoted from immutable
`candidate-<full-SHA>` digests instead of being rebuilt by the tag workflow.
`scripts/publish_release.py` is excluded from the release archive.

## 0.26.5 — 2026-07-30

### Host launcher modularization

No public CLI, configuration, state-location, or isolation behavior changes.
The installed payload now includes `cage-main.py` and the `cage_core` package
and still requires Python 3.11 or newer. Custom packaging or mirrors must retain
that directory as regular files; symlink and special-file package entries are
rejected. Normal source and release installer users need no configuration
change.

## 0.26.4 — 2026-07-28

### Authoritative MCP pack selection

Who is affected: every Codex and Claude launch that relies on MCP servers
defined outside the central Cage config — user `~/.codex/config.toml`, named
Codex profiles, repository `.codex/config.toml` or `.mcp.json`, system or
plugin-generated configuration, or host `~/.claude.json`.

Previous behavior: Cage added the selected `mcp_packs` on top of whatever MCP
servers the tool already inherited from those layers. Inherited servers stayed
enabled, so an unselected server (for example a built-in `node_repl`) could
still start, fail initialization, and appear in startup-failure output.

New behavior: `mcp_packs` is the authoritative allowlist. Only servers selected
by the resolved preset may start; an absent or empty `mcp_packs` selection means
zero active MCPs. At launch Cage inventories the inherited servers in the
launching runtime (`mcp list --json`, supplemented by direct profile/project
layer parsing) and disables every unselected inherited server with
highest-precedence overrides. Loaded servers receive `enabled=false`.
Direct-only profile/project definitions that Codex has not loaded yet (notably
an untrusted repository) receive a same-kind inert transport plus
`enabled=false`, so Codex sees a complete disabled definition before trust and
the server stays inert if trust is granted in the same process. `target=host`
inventories the host binary; container launches inventory inside the image
(entrypoint, after configuration import); Desktop re-inventories inside the
persistent container on every app-server connection. Caller `-c`/`--config`
assignments are restricted to an explicit allowlist of runtime-only roots.
Caller profile (`-p`/`--profile`), working-directory (`-C`/`--cd`), and
feature (`--enable`/`--disable`) overrides are rejected because they can
introduce configuration or plugin layers after inventory. Dedicated model and
sandbox options, normal prompts, and positional/subcommand payload after `--`
remain available. `--remote` and `--remote-auth-token-env` are also rejected
because the destination app-server runtime was not inventoried;
`--ignore-user-config` is rejected because removing the already-inventoried
layer could leave a transport-less suppression override. Desktop authorization
metadata is root-owned rather than
writable by the remote Codex user. For Claude, host `~/.claude.json` MCP definitions are no
longer merged, the volume `mcpServers` is reconciled to the selected set only,
and a private read-only `.mcp.json` overlay always suppresses repository MCP
definitions. Unselected servers may remain visible as disabled in
`codex mcp list`; they never start. Cage fails closed when a trustworthy
inventory cannot be obtained, and there is no legacy inheritance escape hatch.

Migration: move every MCP server you want into a central `[mcp_packs.*]` block
and select it explicitly in the preset. This includes servers previously
inherited from `~/.codex/config.toml`, named profiles, repository config, or
host `~/.claude.json`. Replace any launch script that supplies
`-c mcp_servers.*=...` or `--config mcp_servers.*=...` with an equivalent
central MCP pack selection. Move caller profile selection into the preset's
`codex_profile`, use the Cage repository path instead of `-C`/`--cd`, and move
feature/plugin/project or unknown-root `-c` settings into a Cage-owned Codex
configuration layer. Do not use `--remote` through Cage; select a Cage-managed
execution target instead. Container and Desktop launches use the Codex binary
inside their image and do not require a host Codex executable; `target=host`
continues to require one. `cage config explain`, `cage config doctor`, and the
TUI list selected servers; host resolution and container/Desktop launch output
disclose the terminal-escaped suppressed set.

Rollback: installing 0.26.3 restores additive MCP behavior. Host Codex/Claude
configuration, repository configuration, plugin files, and OAuth credentials
are never modified by either version; the suppression is applied per-launch
through process-local overrides and container state only.

## 0.26.2 — 2026-07-28

### Shared base image for local and published builds

Who is affected: users who build Cage images locally or inspect release image
provenance.

New behavior: Claude and Codex leaf images share an agent-neutral
`cage-base:<version>` containing common operating-system packages, Node.js,
GitHub CLI, and the bridge relays. Agent users, binaries, entrypoints, and
Codex-only OpenSSH remain separated in the existing `claude-code` and `codex`
leaf images. Release workflows publish the multi-architecture base with SBOM
and provenance metadata before building either leaf.

Migration: none. Existing leaf image names, pull-first behavior, volumes,
presets, and `cage update` flows are unchanged. Source installers and release
archives now include `Dockerfile.base`; an automatic local fallback build
creates the versioned base before the selected leaf.

Rollback: installing 0.26.1 restores independent leaf Dockerfiles. Existing
container volumes and Cage configuration remain compatible.

### Desktop lifecycle management in the TUI

Who is affected: macOS users with registered ChatGPT Desktop targets,
especially when the current repository mapping does not itself use
`target = "desktop"`.

Previous behavior: lifecycle controls appeared only on the launch screen when
the currently resolved configuration was already a Desktop target. A target
created with another named preset could therefore be invisible from bare
`cage`, leaving users to remember its exact lifecycle commands. The remote
watchdog also compared heartbeat age with wall-clock time, so Mac sleep could
be interpreted as supervisor loss immediately after wake.

New behavior: bare `cage` has a top-level **Manage Desktop targets** screen on
macOS. It discovers every registered target independently of the current
project mapping, shows live status, and offers start/recover, restart, recent
logs, stop, setup, refresh, and confirmed removal. Actions use the selected
target's stored preset and repository so they address the existing identity
and volume. Removal still requires typing the exact alias. Sleep and long
scheduler gaps now reset the watchdog's active-time grace window; genuine
supervisor loss remains fail-closed after 45 active seconds.

Migration: none. Install the corrected version, run `cage`, and choose
**Manage Desktop targets**. Existing aliases, keys, volumes, ChatGPT
connections, and history are reused without conversion.

Verification: the screen lists a previously registered target even when the
current project configuration uses `container`; a failed or stopped target
offers **Start / recover and open ChatGPT**; stop preserves its volume; and
removal cannot proceed without the exact alias.

Rollback: install 0.26.1 and use `cage desktop ...` lifecycle commands
directly. Existing Desktop state remains compatible, but TUI discovery and the
sleep-safe watchdog are lost.

## 0.26.0 — 2026-07-27

### ChatGPT Desktop through an SSH-backed Cage container

Who is affected: macOS users who want ChatGPT Desktop to run repository tools
inside Cage while retaining preset-specific providers, skills, MCP servers,
identity, network policy, and persistent Codex history.

New behavior:

- Codex presets may set `target = "desktop"`; `--desktop`, `--container`, and
  `--host` are mutually exclusive launch overrides.
- Run `cage desktop setup` once. Cage installs one managed `Include` in
  `~/.ssh/config`; it generates and owns concrete target blocks automatically.
- A normal launch starts or reuses the repository/preset target and opens
  ChatGPT. Use `--no-open` to suppress that final step.
- `cage desktop stop --preset NAME PATH` disconnects ChatGPT but preserves the
  alias, keys, Codex volume, and history. `remove` deletes them only after
  explicit confirmation.
- Desktop configurations must be saved or project-owned. TUI launch-once
  configurations are rejected because a persistent target must be
  reconstructable.

The desktop app remains a host process. Repository commands and Codex
app-server run in the Cage container reached through the official SSH-host
workflow. The transport has no listening port: the generated SSH alias invokes
the installed Cage helper through `ProxyCommand`, which starts one inetd-mode
`sshd` connection inside the labeled container.

Rollback: stop and remove each target, remove `target = "desktop"` from
presets, then remove the single Cage `Include` line from `~/.ssh/config` if no
Desktop targets will be used. Existing container and host targets are
unchanged.

## 0.25.1 — 2026-07-27

### Codex profiles and host-native MCP/skill reuse

Who is affected: Codex users who keep provider/model variants in native Codex
profile files or who want a Cage preset's MCP and skill selections while
running with `target = "host"`.

New behavior:

- Codex presets may set `codex_profile = "name"`. Cage validates
  `$CODEX_HOME/name.config.toml` and supplies `--profile name` for both
  container and host launches.
- Host mode applies selected remote and stdio MCP servers through process-local
  Codex CLI overrides. It does not rewrite `config.toml`. Stdio executables are
  pinned before changing into the repository and are rejected if they resolve
  under that writable repository.
- Host mode applies selected skill packs as a process-local Codex skill filter
  when they use the default `~/.agents` registry. Custom agent registries and
  skill-pack sources still fail closed.
- Selected MCP names that already exist in base, selected-profile, or project
  Codex configuration fail clearly instead of being ambiguously merged.
- Host command bridges, extra mounts, `ssh_host`, non-open Cage network modes,
  and custom agent registries remain unsupported in host mode.

No migration is required. Existing presets without `codex_profile` retain their
current base configuration. Presets without MCP/skill packs are unchanged.

The ChatGPT desktop app can be opened for a workspace by native Codex tooling,
but named configuration profile selection is currently documented for the CLI,
not as a desktop launch selector. Cage does not emulate desktop identities by
changing `CODEX_HOME`.

Rollback: remove `codex_profile` and selected MCP/skill packs from host presets,
or switch the preset back to `target = "container"`.

## 0.25.0 — 2026-07-24

### Host-native Codex CLI execution target

Who is affected: users who want to run Codex directly on the host for
maintenance tasks that need access outside the repository.

New behavior: presets accept `target = "container"` (default) or
`target = "host"`. The CLI flags `--host` and `--container` override the
saved target for one launch only. Host execution:

- runs Codex with `CODEX_HOME` set to the resolved `host_codex_dir`;
- provides no Docker isolation and no Cage network restriction;
- rejects `--net gate`/`--net off` (yolo's implicit gate default is also
  rejected — use `--net open` explicitly to acknowledge unrestricted
  networking);
- applies Git identity, SSH keys, and GitHub tokens process-scoped (no
  host config mutation);
- rejects MCP packs, skill packs, host commands, extra mounts, custom
  `host_agents_dir`, and `ssh_host` aliases;
- pins the Codex executable and rejects it if inside the repository.

No configuration migration is required. Existing configs without `target`
default to container execution. `--host` and `--container` are launch-only
overrides and do not alter saved presets or project mappings.

This implements host-native Codex CLI only. ChatGPT desktop integration
and SSH-connected container backends are future milestones.

Rollback: remove `target` keys from presets; the default remains container.

## 0.24.1 — 2026-07-23

### Corrected terminal configuration controls

No configuration migration is required. Text fields now preload their current
value, support cursor editing, clear when their contents are erased, and cancel
immediately on Escape. `-` remains accepted as a clear shortcut. Confirmation
input is displayed separately from scrollable risk details, and checkbox/menu
focus no longer jumps to the first row after an edit.

The customization persistence choices are unchanged, but their presentation is
explicit: remember-this-project is initially highlighted, launch-once does not
write TOML, and a named save does not alter the project mapping. All saves still
require an explicit selection and confirmation. Existing named configurations
cannot be overwritten silently.

Saved preset `yolo` behavior and CLI precedence are unchanged. Remembering a
configuration with yolo enabled stores `yolo = true` in the internal exact-
project preset; `--no-yolo` still disables it for one invocation.

Rollback: install `0.24.0`. The canonical TOML is unchanged and remains
compatible, but the original TUI input and navigation defects return.

## 0.24.0 — 2026-07-22

### Interactive configuration launcher

Bare `cage` now opens a curses launcher for the current directory. Existing
`cage PATH` and `cage --preset NAME PATH` commands remain direct launches, and
`cage --interactive [PATH]` opens the same UI. No config migration is required:
the UI edits the existing canonical `~/.config/cage/config.toml` and does not
introduce fragments or merge precedence.

"Remember for this project" creates an internal preset whose name starts with
`__cage_project_` and maps the canonical project path to it. These internal
presets are hidden from ordinary reusable-configuration lists but remain plain
TOML for recovery. Named saves remain normal `[presets.NAME]` entries.

Preset `yolo = true|false` is now supported. Explicit `--yolo` or `--no-yolo`
wins over the saved value. Existing network precedence is unchanged.

Every UI save validates and locks the source, rejects concurrent changes,
preserves the source mode and symlink target, atomically replaces the file, and
keeps up to ten mode-0600 backups in `~/.config/cage/backups/`. Backups are not
loaded during normal resolution. Config UI actions do not inspect, migrate,
reset, or remove Docker volumes; Codex runtime state and Claude session-sync
rules are unchanged.

Rollback: install `0.23.8`. The canonical TOML remains readable after removing
the optional `yolo` keys. Internal project preset names are ordinary valid
preset names and may be kept or replaced with named presets.

## 0.23.8 — 2026-07-20

No configuration migration. This release adds defense in depth to the `0.23.7`
state-preservation behavior: both host-import helpers reject every unsupported
destination before removing anything from the project volume. Unit and
real-Docker release gates cover sessions, archived sessions, history, SQLite
state and WALs, logs, memories, goals, caches, and shell snapshots.

Rollback: `0.23.7` retains the corrected static allowlist, but lacks the helper-
level fail-closed invariant and comprehensive regression fixture added here.

## 0.23.7 — 2026-07-20

No additional configuration migration. This version supersedes the failed,
unpublished `v0.23.6` release attempt and includes the `0.23.6` migrations
below. Its entrypoint additionally copies Codex `rules/` without preserving the
host UID so the import works with Cage's intentionally reduced capabilities.

## 0.23.6 — 2026-07-20

### Codex volume history and host token commands

Who is affected: Codex users who launched a per-repository Cage volume after
0.23.4 while the selected host Codex directory also contained runtime state,
and users whose host command embeds the same arguments supplied by Codex (for
example, both layers specifying `ztoken token -n codex`).

Previous behavior: the Codex entrypoint removed every same-named volume entry
before importing the host Codex directory. Shared-host `sessions`,
`history.jsonl`, SQLite indexes, logs, memories, and caches could therefore
replace the repository volume's own resumable state. Separately, the repaired
host-command protocol appended caller arguments to fixed configured arguments;
an unchanged legacy token definition could receive the same suffix twice and
leave the provider request unauthenticated.

New behavior: host import is allowlisted to supported static global
configuration (`config.toml`, profile `*.config.toml` files, global AGENTS
guidance, hooks, and rules); `auth.json` and MCP OAuth `.credentials.json`
continue through their existing explicit policies. All runtime-owned Codex state
remains untouched in the per-repository volume. Host commands continue
forwarding arguments, but an exact caller suffix already present after the
configured executable is de-duplicated for compatibility. `cage config doctor`
warns when a host command embeds fixed arguments.

Migration:

1. Preserve every affected Docker volume. Do not reset or delete it merely
   because history is missing from the Codex list.
2. Install the corrected release and launch the repository normally. This stops
   further replacement but cannot reconstruct files already removed by an older
   entrypoint.
3. Prefer an executable-only token bridge definition such as
   `[host_commands.ztoken] command = "ztoken"` when the Codex provider auth table
   already supplies `args = ["token", "-n", "codex"]`.
4. Run `cage config doctor --preset NAME /path/to/repo` and resolve the
   fixed-argument warning. The exact-suffix compatibility behavior permits the
   legacy definition during migration.
5. Treat recovery of already replaced history as a separate read-only-first
   operation against the exact affected volume or an existing backup; do not
   merge or rewrite session stores without preserving the originals.

Rollback: restoring 0.23.4 or 0.23.5 reintroduces destructive host-state
replacement. If rollback is unavoidable, back up the affected volume first and
avoid launching Codex until the entrypoint import is patched.

### Verifiable release artifacts

Who is affected: release consumers who mirror, audit, or verify Cage source
archives or container images. Normal installation and launch configuration are
unchanged.

Previous behavior: releases published a source tarball and SHA-256 checksum,
and pushed two multi-architecture images. Workflow action dependencies used
moving major-version tags. Releases did not publish an SBOM or signed provenance
records.

New behavior: workflow actions are pinned to immutable commits and refreshed by
Dependabot. The source archive is reproducible for a fixed source revision and
build epoch, ships with an SPDX SBOM, and receives signed GitHub provenance and
SBOM attestations. Both container images carry BuildKit SBOM and max-level
provenance metadata plus signed GitHub provenance attestations.

Migration: none. Existing installers continue to verify the archive checksum.
Consumers that require provenance can additionally run `gh attestation verify`
using the commands in `README.md`.

Verification: download the archive, checksum, and SBOM from the GitHub Release;
verify the checksum and both source attestations; then verify the versioned
`codex` and `claude-code` GHCR images by OCI reference.

Rollback: older releases remain installable with their checksums but do not gain
attestations retroactively. Restoring the old workflow removes these records
from future releases and should be treated as a supply-chain regression.

## 0.23.5 — 2026-07-18

Who is affected: users running the public curl-pipe installer without
`GH_TOKEN`, `GITHUB_TOKEN`, or a usable `gh auth token`, particularly with the
system Bash 3.2 shipped by macOS.

Previous behavior: latest-release lookup expanded an empty Bash array while
`set -u` was active, aborting with `GH_AUTH_HEADER[@]: unbound variable` before
the release could be downloaded.

New behavior: authenticated lookup remains available when a token exists;
otherwise the installer makes the same public GitHub API request without an
optional header argument.

Migration: rerun the normal installer command. No config, credentials, Docker
volumes, or project state need to be changed.

## 0.23.4 — 2026-07-18

Who is affected: Codex users on v0.23.0 through v0.23.3 whose persistent volume
contains `.credentials.json`, `auth.json`, or a copied `config.toml`. OAuth
users normally encounter the failure first on `.credentials.json`.

Previous behavior: the entrypoint assigned persistent files to the remapped
Codex user, then attempted to set mode `0600` as container root. Cage
intentionally omits Linux `CAP_FOWNER`; root was therefore not the file owner
and `chmod` aborted startup with `Operation not permitted`.

New behavior: Cage keeps `CAP_FOWNER` disabled and performs private mode
normalization as the mapped Codex owner on an already-open, no-follow file
descriptor. Sensitive Codex files are explicitly assigned to that owner and
must be regular, single-link files. Symlinks, non-regular files, hard links, and
detected path replacements fail closed without redirecting `chmod` elsewhere.

Migration:

1. Install Cage 0.23.4 or later with the normal installer.
2. Re-run the original `cage` command. Do not delete or recreate credentials.
3. If Cage rejects unsafe sensitive state, inspect the named volume before
   replacing anything; convert only the reported symlink, hard link, or
   non-file into the intended regular private file, or reset that disposable
   volume.

Verification: startup proceeds past the sensitive-mode step. Existing
`.credentials.json`, `auth.json`, and `config.toml` files are mode `0600` and
owned by the mapped Codex user.

Rollback: no persistent data migration occurs. Restoring v0.23.3 also restores
the startup defect for affected volumes, so prefer upgrading; if a rollback is
unavoidable, preserve the volume and credentials for a later fixed launch.

## 0.23.3 — 2026-07-18

Who is affected: Codex users on macOS Docker Desktop or Colima configurations
that do not share `/var/folders` with the Docker VM. The same issue could affect
repositories whose project `.mcp.json` needed Cage's private stdio-MCP overlay.

Previous behavior: Cage created Docker-bind-mounted staging files under the
host operating system's temporary directory. The path existed for Cage's host
process but not inside some Docker VMs, so Docker rejected the launch with
`bind source path does not exist`.

New behavior: Cage creates those short-lived, private staging files under the
canonical Cage config directory, normally `~/.config/cage`, which is inside the
Docker-shared user home on standard macOS setups. Existing file modes, content
validation, and success/error cleanup remain enforced. Cage rejects a config
directory inside the repository or another read-write Cage mount because that
would expose the same staging inode through a writable container path.

Migration:

1. Install Cage 0.23.3 or later with the normal installer.
2. Re-run the same `cage` command; no credential or preset change is required.
3. If `XDG_CONFIG_HOME` points outside every directory shared with the Docker
   daemon, either share that path with Docker or move Cage's config back under
   the user home.
4. If the config directory is inside the repository or a configured read-write
   extra mount, move it to a private path outside all Cage-writable mounts.

Verification: the launch proceeds past OAuth reconciliation without a missing
`cage-oauth-sync-*` bind-source error. Presets using a bridged project
`.mcp.json` should likewise proceed without a missing `.cage-mcp.*` source.

Rollback: restore 0.23.2 and set `TMPDIR` to a private directory under the
Docker-shared user home for each launch. No persistent data migration is needed.

## 0.23.2 — 2026-07-16

No user-side migration. This patch supplies explicit repository context to the
checkout-free GitHub Release job and enforces real-Docker smoke tests in every
Python matrix job. It is the first complete GitHub Release containing the v0.23
hardening changes.

## 0.23.1 — 2026-07-16

No user-side migration. This patch corrects the GitHub Actions pip-cache
dependency path; it supersedes the failed, unpublished v0.23.0 workflow run and
published both versioned container images. Its final GitHub Release job failed
before creating the release object.

## 0.23.0 — 2026-07-16

These changes were prepared in the v0.23.0 source/tag and first fully published
in v0.23.2. Review every section that matches a capability used by your presets
before upgrading.

### Project `.mcp.json` overlay

When a selected host-side stdio MCP server has the same name as an entry in the
repository's `.mcp.json`, Cage now generates a private patched copy and mounts it
over that file read-only inside the container. The host repository file is never
temporarily rewritten.

Who is affected: repositories whose `.mcp.json` is a symlink, non-regular file,
invalid JSON, or has a non-object `mcpServers` value. Those configurations now
fail closed instead of being ignored or temporarily modified.

Migration:

1. Replace a symlinked `.mcp.json` with a regular repository file.
2. Validate that the root and `mcpServers` are JSON objects.
3. Relaunch Cage and confirm the stdio server is available.

Rollback: deselect the stdio MCP pack or restore the previous Cage version. Cage
does not modify the repository file, so there is no data rollback step.

### Generated auth and MCP state reconciliation

Who is affected:

- Codex presets that set `copy_auth = false` but accidentally relied on an
  `auth.json` retained from an earlier preset;
- persistent volumes containing pre-upgrade, unmarked generated MCP entries;
- scripts or users relying on group-readable generated auth/config files.

New behavior:

- `copy_auth = false` and a missing current host auth source remove the volume's
  stale `auth.json`;
- Claude removes Cage-owned connectors and expanded tokens when they are no
  longer active, while preserving a user-defined server shadowed by the same
  managed name;
- Codex Cage-generated MCP servers live in a marked block that is replaced or
  removed atomically on every launch;
- generated sensitive files use mode `0600` and their directories use `0700`.
- root entrypoint reconciliation refuses unsafe symlinked JSON state, removes a
  symlink/non-file Codex config before use, replaces copied destinations before
  writing, and atomically replaces generated instruction files.

Migration:

1. Ensure every `copy_auth = false` preset provides its intended API-key or
   provider authentication through the selected environment configuration.
2. On the first upgrade, if a stale Claude MCP entry survives, remove it once
   from the per-repository volume's `.claude/.claude.json`, or reset that Cage
   volume if its sessions are disposable.
3. If Codex reports a duplicate MCP server from an old unmarked generated table,
   remove that old table once from the volume config, or reset the volume.
4. Update any external script that reads these private files as another user or
   group; normal execution as the mapped Cage user is unchanged.
5. Launch the privileged preset, then a restricted/no-MCP preset, and use
   `cage config explain` to verify the intended current capabilities.

Rollback: restore the previous Cage version and, only if required, restore a
backed-up per-repository volume. Do not copy stale `auth.json` between identities
as a workaround.

### Netgate destination and request restrictions

Who is affected: private/VPN/split-horizon MCP or package endpoints, nonstandard
HTTPS CONNECT ports, chunked uploads, uploads larger than 64 MiB, tunnels idle
for more than two minutes, and scripts that manually override Cage's proxy URLs.

New behavior:

- DNS answers containing any loopback, private, link-local, multicast, reserved,
  unspecified, or site-local address are denied;
- CONNECT permits ports 443 and 8443 only;
- request bodies require unambiguous Content-Length and are capped at 64 MiB;
- idle tunnels close after 120 seconds;
- every launch uses a fresh proxy credential, injected automatically into the
  container's proxy URLs, so unrelated local, bridge, or LAN clients cannot use
  the host listener;
- prompt values no longer enter AppleScript source.

Migration:

1. Use `--net open` only when a trusted workflow genuinely needs a private or
   nonstandard endpoint, and treat that as an explicit reduction in isolation.
2. Prefer HTTPS services on 443/8443 and fixed-length uploads under 64 MiB.
3. Do not replace the generated authenticated proxy URL with a manually assembled
   host/port URL; doing so produces an HTTP 407 response. There is no credential
   provisioning step for normal Cage launches.
4. Re-run `cage netgate list PATH`; domain decisions remain, but an allowed name
   is still denied if its current address is unsafe.

Rollback: use the previous Cage version. Do not permanently allow a broad public
domain as a workaround for a private-address rejection.

### Codex MCP OAuth reconciliation

Who is affected: users carrying a newer `.credentials.json` only in an old
per-repository volume, users with malformed/non-object credentials, credentials
larger than 4 MiB, symlinked credential files, or concurrent independent changes
to both host and volume copies.

New behavior:

- a legacy volume without Cage's identity marker initializes from the currently
  selected host Codex directory; an unbound volume-only credential is not copied
  into that host account;
- account-directory switches reset the volume from the selected host source;
- one-sided refresh-token rotation is selected by canonical content hash rather
  than timestamp;
- two-sided changes fail explicitly and overwrite neither copy;
- credentials must be mode-0600 regular UTF-8 JSON objects no larger than 4 MiB;
- helper containers never mount the host Codex directory writable.

Migration:

1. Before upgrading, if the only current OAuth credential exists inside a Cage
   volume, complete a normal launch/exit on the previous version so it reaches
   the intended host Codex directory, or back it up securely.
2. Ensure the selected host `.credentials.json` is a regular JSON object and not
   a symlink.
3. After upgrading, launch once and verify MCP authentication.
4. If Cage reports a two-sided conflict, preserve both files, identify the
   provider-current token, place that one in the selected host Codex directory,
   and reset the per-repository sync state/volume only after backing up sessions.

Rollback: restore the previous Cage version and the securely backed-up credential
copy. Avoid using mtimes to choose a rotating refresh token manually.

### Host MCP and command bridge protocol

Who is affected: host MCP/command definitions that rely on implicit shell
operators, globbing, variable expansion, leading `NAME=value` assignments,
ambient host environment variables, or the old host-command behavior that lost
arguments/stderr/status. Definitions whose executable resolves inside the
repository or another read-write Cage mount are also affected.

New behavior:

- bridge clients authenticate with a fresh per-launch token before a host
  process is created;
- command strings are parsed as argv and run with `shell=False` from the host
  home directory;
- only a small base environment plus variables declared by selected config
  `env`/`extra_env` fields reach the host process;
- Cage removes relative, empty, and Cage-writable directories from the child
  `PATH`, resolves the configured executable once at bridge startup, rejects an
  executable under the repository or any config/CLI read-write extra mount, and
  pins the resolved absolute path for the launch;
- host-command shims forward caller arguments, stdin, stdout, stderr, and exit
  status through a bounded framed protocol;
- processes and descendants are terminated on timeout, limit, disconnect, or
  launcher cleanup.

Migration:

1. Replace pipelines, redirects, globbing, and compound commands with a reviewed
   executable wrapper script.
2. If shell interpretation is genuinely required, opt in visibly with a command
   such as `sh -lc '...'`; this restores shell risk for that definition.
3. Move leading environment assignments into the relevant preset/MCP `env` list.
4. Move an executable stored in the repository or another read-write mount to a
   trusted host installation directory. A reviewed wrapper outside Cage-writable
   mounts may explicitly reference repository content when that authority is
   intentional.
5. Declare every non-base environment variable the host command needs.
6. Verify argument handling and nonzero exit behavior from inside Cage before
   depending on the command for authentication refresh.

Rollback: restore the prior Cage version and its matching relay/bridge scripts
together. Old and new bridge protocols are intentionally incompatible and must
not be mixed.

### Stricter configuration validation

Who is affected: configurations containing misspelled/unknown keys, authenticated
plain-HTTP MCP URLs, dots in any configured MCP/host-command definition name, or
selected names that differ only by case or `-` versus `_`.

New behavior:

- unknown keys fail with their exact table location instead of being ignored;
- MCP and host-command names must use letters, digits, `_`, or `-`;
- relay-normalized name collisions fail before launch;
- bearer-token and OAuth MCP endpoints require HTTPS;
- sensitive custom header values must reference environment variables and may
  not contain literal secret fallbacks;
- custom MCP `headers` fail clearly for Codex presets instead of being silently
  ignored; use `bearer_token_env_var` or OAuth there;
- names with a final newline are rejected.

Migration:

1. Run `cage config doctor --preset NAME PATH` for each used preset.
2. Correct any reported typo; in particular, replace `copy_aut` with
   `copy_auth`.
3. Rename dotted MCP/host commands and any case or dash/underscore collision.
4. Update authenticated MCP URLs to `https://`.
5. Replace literal sensitive headers with references such as
   `Authorization = "Bearer ${SERVICE_TOKEN}"`; Cage automatically forwards the
   referenced variable for Claude. Replace Codex custom headers with its bearer
   or OAuth fields.
6. Run `cage config explain --preset NAME PATH` and confirm the resolved
   capability set.

Rollback: restore the previous Cage version. Do not work around the validator by
placing credentials directly in repository configuration.

### Minimal starter configuration

`cage config init` now creates one `codex-local` preset using `~/.codex`. It no
longer enables fictional company MCP packages, GitHub auth, or locally missing
skills.

Existing config files are not rewritten. New Claude users should initialize,
then add a Claude auth block and preset using the documented examples.

### Managed installer directory

The installer now marks owned directories with `.cage-install` and refuses to
delete or replace an unrecognized custom path. The historical default
`~/.local/share/cage` is recognized automatically for one-step upgrades.
`make install` now routes through the same staged, rollback-capable installer via
`--from-source` instead of copying in place.

For a legitimate older custom installation:

```bash
printf '%s\n' "$(cage --version | awk '{print $NF}')" > "$CAGE_INSTALL_DIR/.cage-install"
./install.sh
```

First verify that `CAGE_INSTALL_DIR` points only to the Cage installation. As a
safer alternative, move the old directory aside, install into an empty target,
verify `cage --version`, and then remove the backup manually.

Rollback: move the saved installation directory back into place and restore the
previous `cage` symlink.

### Release validation

Tag-triggered artifact and image publication now waits for Python 3.11/3.12
tests, syntax checks, Compose validation, and successful publication of both
container images. The GitHub release is created last. This does not require a
user-side migration.

Still planned:

- explicit strict, developer, and host-integrated modes;
- resource-limit defaults and mount validation.

For every breaking change, the release entry must include:

1. who is affected;
2. the previous and new behavior;
3. exact commands or configuration edits required;
4. how to verify the migration;
5. rollback or recovery instructions.
