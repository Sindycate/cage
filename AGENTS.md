# AGENTS.md

This file provides guidance to AI coding agents — Claude Code (claude.ai/code) and OpenAI Codex CLI — when working with code in this repository. It is the canonical source of project instructions; `CLAUDE.md` imports it so Claude Code reads the same content.

## What This Project Is

A Docker-based isolation wrapper for running AI coding assistants (Claude Code, OpenAI Codex CLI) in containers on macOS and Linux. It reduces accidental host filesystem damage by restricting direct writes to configured mounts, while selected state-sync and host-integration features deliberately cross that boundary. See `SECURITY.md` for the effective threat model.

## Active Hardening Work

The July 2026 security and architecture remediation is tracked in
`docs/hardening/WORKFLOW.md`. Any agent continuing that work must read the
workflow, progress log, and migration guide before editing. Those files are the
durable source of truth across context compaction and maintainer handoffs.

## Build and Run

```bash
# Build/rebuild all images (builds shared base first, then all four leaves)
docker compose build

# Build just the shared base
docker compose build base

# Build just one leaf image (requires base to exist)
docker compose build claude
docker compose build codex
docker compose build opencode
docker compose build monitor

# Rebuild from scratch (e.g., to update tool versions)
docker compose build --no-cache

# Force rebuild to get latest tool version (pulls fresh from upstream)
cage --rebuild ~/path/to/repo

# Fast tool refresh — re-run just the installer layer on top of the existing image
# (seconds, no apt/Node/gh re-run), then re-tag the image cage runs
cage update          # update the default tool
cage update claude
cage update codex
cage update opencode

# Inspect capacity/retention and run exact, confirmation-gated image cleanup
cage storage status
cage storage clean
# Preview/apply noninteractive maintenance of exact safe image candidates
cage storage maintain
cage storage maintain --apply

# Run Claude Code against a repo (default)
cage ~/path/to/repo
cage claude ~/path/to/repo

# Run Codex CLI against a repo
cage codex ~/path/to/repo

# Run OpenCode against a repo (container-only)
cage opencode ~/path/to/repo

# Pass args through to the tool
cage ~/path/to/repo --resume
cage ~/path/to/repo -p "do something"

# Central configuration — one TOML file for presets, auth, MCP packs, skill packs,
# identities, host commands, extra mounts, and project mappings. It is required for launches.
cage config init
cage config edit
cage config list
cage config explain ~/path/to/repo
cage config doctor --preset codex-company ~/path/to/repo
cage --preset codex-company ~/path/to/repo
cage --interactive ~/path/to/repo
cage codex -i ~/path/to/repo
# Bare cage opens the same TUI for the current directory. It can launch once,
# remember an internal project configuration, or save a reusable configuration.
cage

# Yolo mode — skip coding-tool permission prompts. This does not contain
# credential use, external connector effects, writable Git metadata, host
# bridges, or deliberate network bypass.
# Yolo defaults to --net gate (domain-gated networking)
cage -y ~/path/to/repo
cage codex -y ~/path/to/repo

# Explicit network gating without yolo
cage --net gate ~/path/to/repo

# No network at all
cage --net off ~/path/to/repo

# Extra named mounts — mount additional host dirs (e.g. a cloned dependency or
# docs tree) at the SAME absolute path inside the container, read-only by default.
# Per-invocation:
cage --mount-ro ~/code/shared-lib ~/path/to/repo
cage --mount-rw ~/scratch/output ~/path/to/repo
# Or in config.toml:
#   [presets.codex-company]
#   extra_mounts = [
#     "~/code/shared-lib",
#     { path = "~/scratch/output", mode = "rw" },
#   ]

# MCP bridge — forward host-side STDIO MCP servers into the container
# In config.toml:
#   [mcp_packs.local-tools]
#   servers = [
#     { name = "myserver", type = "stdio", command = "some-tool --mcp-proxy https://example.com/mcp" },
#   ]

# Codex skill packs — select reusable Agent Skills per preset, similar to MCP packs.
# Skills live in a canonical host registry (usually ~/.agents/skills), and cage
# mounts only the selected skill directories into the container. If no skill packs
# are selected, cage falls back to copying the whole host_agents_dir.
# In config.toml:
#   [skill_packs.external-systems]
#   source = "~/.agents"
#   skills = ["linear-ticket-flow", "dash0-dashboard-flow"]
#   [presets.codex-company]
#   skill_packs = ["external-systems"]

# Remote (HTTP) MCP servers — e.g. Linear — are generated as native tool
# config inside the container, with token env vars forwarded by name.
#   [mcp_packs.linear]
#   env = ["LINEAR_API_KEY"]
#   servers = [
#     { name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" },
#   ]
#
# OAuth HTTP MCP servers — e.g. Dash0 when API tokens are not available — are
# also generated from central config. Codex authenticates on the host once per
# Codex auth directory with `cage mcp login NAME PATH`; Claude authenticates
# inside the cage session with `/mcp`. cage forces Codex's MCP OAuth credential
# store to file mode for host login and container launch; this is separate from
# auth.json, so copy_auth=false still skips only the main Codex login cache.
#   [mcp_packs.dash0]
#   servers = [
#     { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", auth = "oauth", oauth_resource = "https://api.eu-central-1.aws.dash0.com", oauth_scopes = ["*"], oauth_client_id_env_var = "DASH0_OAUTH_CLIENT_ID" },
#   ]

# Host command bridge — expose host commands (e.g. token minters) inside the container
# In config.toml:
#   [host_commands.ztoken]
#   command = "ztoken"
#   [presets.codex-company]
#   host_commands = ["ztoken"]
#
# Profile-pinned host AWS CLI access (browser/SSO remains on the host):
#   [presets.codex-staging-readonly]
#   tool = "codex"
#   aws_profile = "aws-staging.ReadOnly"
#   aws_access = "host-cli"
```

## Architecture

**`cage`** (host-side bootstrap, symlinked to `~/.local/bin/`):
- Is a Bash 3.2-compatible bootstrap only: resolves its real installation
  directory, validates Python 3.12+, exports the internal Cage version, and
  `exec`s `python3 -I cage-main.py`. It contains no launch policy, Docker
  construction, heredocs, state reconciliation, or trap chains
- `cage-main.py` rejects symlink/non-regular `cage_core` package entries before
  adding only the resolved installation root to isolated Python's `sys.path`
- `cage_core` owns the typed `LaunchRequest` → `ResolvedConfig` → immutable
  `LaunchPlan` pipeline. The complete plan is validated before Docker/image,
  bridge, session/OAuth, or target side effects. Its versioned `resolve-json`
  contract reports normalized public evidence and environment names only,
  never secret values, commands, headers, OAuth data, or raw passthrough
  arguments
- Core target adapters implement host, ordinary container, and Desktop
  execution; state adapters own OAuth/session reconciliation; the lifecycle
  coordinator owns reverse-order cleanup. Pure policy/model modules do not
  execute processes or mutate the filesystem
- `cage_core.storage` owns portable Docker capacity probing, image/container
  inventory, managed role/version classification, semantic retention, explicit
  ephemeral-image lifecycle classification, and exact race-rechecked cleanup
  candidates. Launch and update paths apply its immutable global policy before
  container/Desktop effects or builds; host-native execution bypasses it.
  Interactive cleanup remains confirmation-gated; noninteractive maintenance
  may remove only exact managed candidates and explicitly labelled, aged
  ephemeral images. Cleanup never prunes or deletes volumes, containers,
  referenced images, unrelated images, legacy unlabeled Cage images, or custom
  derived tags
- `cage_core.monitor` is an optional host-owned Token Monitor integration. It
  stores its hub credential, host identity, logical-target registry, locks,
  per-project collector state, custom prices, and aggregate status under private
  `~/.config/cage/monitor/` files; it is outside
  the launch-plan contract. Only Codex Container/Desktop volumes can be
  registered. A short-lived pinned collector mounts the exact `sessions/` and
  `archived_sessions/` volume subpaths read-only with no network, while the
  host performs authenticated hub requests. One Cage installation publishes
  one readable hub device per provider stream, and each logical Codex volume is
  a project under the stream that owns its sessions. Every upload scans all
  active volumes, deduplicates identical or monotonic session copies before
  provider partitioning, assigns missing or multi-provider copies to
  `Unattributed`, and fails closed on incompatible copies. Discovery is limited
  to Cage-named `codex-state-*` volumes and recovered volumes require explicit
  `cage monitor add --volume` adoption. Replacement volumes require explicit
  `cage monitor add` adoption. Legacy unsplit and per-volume hub devices require
  verified, resumable `monitor migrate`; private custom pricing never enters
  the tool container or hub credential path. Split state and aggregate status
  remain private under the monitor directory
- `cage-config.py`, `cage-tui.py`, `cage-desktop.py`, both bridge scripts,
  `codex-remote.py`, and the container entrypoints remain compatibility
  frontends. Codex host/container/Desktop paths delegate passthrough and MCP
  suppression decisions to one shared policy implementation. The Desktop
  remote path alone accepts the current app's exact
  `features.code_mode_host=true` override before `app-server`;
  other feature values/roots and host/container uses still fail closed
- Accepts optional subcommand (`cage claude`, `cage codex`, or `cage opencode`) to select a tool and `--preset NAME` to select a central runnable configuration
- Bare `cage` and `--interactive`/`-i` open `cage-tui.py` before any Docker,
  bridge, sync, or volume operation. The curses UI can launch once, remember a
  hidden project-owned preset, save reusable presets, and manage all central
  config objects. On macOS it also lists all registered Desktop targets
  independently of the current project mapping and provides their lifecycle
  actions through a bounded structured helper interface. It returns a private
  launch artifact that is revalidated by `cage-config.py`; cancellation is a
  state no-op. If curses is unavailable, Cage falls back to the legacy
  launch-only numbered prompt
- Requires central config at `~/.config/cage/config.toml` for launches. It is
  parsed by `cage_core.config` (`cage-config.py` remains the compatibility
  frontend; Python 3.12+ standard library) and contains reusable `auth`,
  `identities`, `mcp_packs`, `skill_packs`, `host_commands`, `presets`, and
  `[projects]` mappings. Project mappings use longest-prefix matching
- The optional top-level `[storage]` policy defaults to a 20 GiB warning/build
  floor, 5 GiB critical floor, two retained semantic versions per managed image
  role, a 24-hour dangling-build minimum age, and a 168-hour minimum age for
  explicitly ephemeral images. The TUI edits it through the same
  concurrency-checked atomic transaction path as other configuration
- Codex presets may select a native `$CODEX_HOME/<name>.config.toml` layer with
  `codex_profile = "<name>"`; Cage validates the file and forwards
  `--profile <name>` to either execution target
- TUI config mutations are typed, dependency-aware, concurrency-checked, and
  atomic. They preserve untouched TOML blocks, source permissions, and symlink
  targets; keep ten private backups under the config directory; and never load
  backup files as config fragments
- Acquires Docker images via pull-before-build: tries `docker pull` from `CAGE_REGISTRY` (ghcr.io), falls back to local `docker build` if pull fails. Local builds automatically ensure the shared base image (`cage-base:<version>`) exists first. `--rebuild` forces a local build with `--no-cache` for both the base and the selected leaf image (useful for getting the latest tool version)
- `cage update [claude|codex|opencode]` refreshes just the tool binary without a full rebuild: it ensures the base image exists (same pull-before-build logic), then builds a tiny overlay image (`docker build --no-cache -f -` reading an inline Dockerfile from stdin) that does `FROM <current image>` and re-runs only the tool installer (Claude: `curl … install.sh`; Codex: `npm install -g @openai/codex@latest`; OpenCode: an unprivileged `npm install -g --allow-scripts=opencode-ai opencode-ai@latest` plus its image-level contract checks), re-tagging the result over `<tool>:${CAGE_VERSION}` and `:latest`. The image stays the single source of the tool version — this intentionally diverges the local image from the same-tagged registry image; `--rebuild` resets to a clean build. Tool defaults to the central default preset's tool, then `claude` when no config exists
- `cage monitor connect|disconnect|status|sync|add|migrate|pricing|forget` manages the
  optional host-side collector. Monitor connection state is not copied into
  containers, and monitor failures are warnings during ordinary launches so
  the accounting aid cannot make a coding session fail open or fail closed
- Takes a repo path, derives a unique container name + Docker volume via md5 hash of the full path
- Ordinary same-repository launches keep one shared persistent state volume but
  use suffixed container names for parallel sessions. The collision menu reads
  from `/dev/tty` when available and otherwise from an interactive stdin, so
  restricted IDE/sandbox terminals remain usable; truly noninteractive
  collisions fail closed
- Presets default to `target = "container"`. Claude and OpenCode are container-only; Codex may opt into
  `target = "host"` or `target = "desktop"` (also available as launch-only
  `--host`/`--desktop` overrides).
  The host branch runs before Docker/image/volume/bridge/state-sync work,
  provides no Docker or Cage network boundary, and rejects `gate`/`off`.
  Git/SSH/GitHub identity remains process-scoped. Selected HTTP/stdio MCP packs
  become process-local Codex overrides, with stdio executables pinned outside
  the writable repository. Selected skill packs become a process-local filter
  only for the default `~/.agents/skills` registry. Host commands, extra
  mounts, custom agent registries, and SSH aliases still fail closed
- OpenCode presets add `host_opencode_config_dir` and
  `host_opencode_data_dir` auth roots plus `opencode_plugins` (false by
  default). Cage creates a bounded, symlink-free private snapshot of applicable
  host and repository configuration, project instructions and skills, and
  selected host skills. The image-installed OpenCode binary resolves that
  snapshot once with project/external discovery disabled; Cage removes inherited
  MCPs, writes only selected transports, and verifies the final MCP and disk-skill
  inventories before execution. Plugins are suppressed with `--pure` unless the
  preset explicitly opts in. Provider `auth.json` is synchronized exactly when
  `copy_auth=true`; `mcp-auth.json` is filtered and merged only for selected
  OAuth MCP names. Sessions, history, cache, indexes, and static config are never
  synchronized to the host. OpenCode `target=host`, `target=desktop`, and
  `session_sync` fail closed
- The macOS-only Desktop branch delegates to `cage-desktop.py`. A detached
  repository/preset-specific supervisor owns the ordinary container launcher,
  Netgate and selected bridges, OAuth reconciliation, a private Unix control
  socket, heartbeat, and cleanup. Cage manages one top-level SSH Include and
  concrete aliases whose installed-helper `ProxyCommand` runs one `sshd -i`
  connection through `docker exec`; there is no TCP listener. Each target has
  a separate Codex volume, client key, persistent container host key, and
  pinned known-hosts file. `stop` preserves state; confirmed `remove` deletes
  the alias, keys, metadata, and volume. Provider/proxy/bridge secrets bypass
  Docker `Config.Env` through a short-lived private handoff, live only in
  tmpfs-backed `/run` for remote app-server processes, and are scrubbed from
  the persistent watchdog. The watchdog counts missed heartbeats in active
  polling time so host sleep does not cause immediate expiry after wake, while
  genuine supervisor loss still stops fail-closed. Desktop alone adds
  `SYS_CHROOT` for OpenSSH privilege separation; `CAP_FOWNER` remains absent
- Runs `docker run` with `cap_drop ALL` followed by the capabilities currently needed for UID/GID remapping; AppArmor and seccomp are unconfined for bubblewrap compatibility, and `no-new-privileges` is not currently set. Treat the container as accidental-damage isolation rather than a hostile-code boundary.
  - Repo at the **same absolute path as on host** (read-write) — mirrored so Claude's project slug (derived from cwd) matches on both sides, enabling session-history sync. This is the main direct writable host mount. Explicit read-write extra mounts and selected host-side state synchronization can also write outside it. A guard rejects paths that would collide with the container filesystem (`/etc`, `/var`, `/home/claude`, etc.)
  - **Claude (bedrock auth):** `~/.aws/credentials` read-only, `~/.claude` read-only at `/host-claude`
  - **Claude (api-key auth):** `ANTHROPIC_API_KEY` env var, `~/.claude` read-only at `/host-claude`
  - **Claude (ccstatusline):** if `~/.config/ccstatusline/` exists on the host, it is mounted read-only at `/host-ccstatusline` and copied into the volume so a customized ccstatusline status line propagates (ccstatusline stores its config there, separate from `settings.json`)
  - **Codex:** host Codex directory from the preset auth block (default `~/.codex` if omitted) read-only at `/host-codex` for auth and supported global configuration, `OPENAI_API_KEY` env var if set. The entrypoint allowlists user `config.toml`, profile `*.config.toml` files, global `AGENTS.md`/`AGENTS.override.md`, `hooks.json`, and `rules/`, plus explicitly governed credentials; per-repository sessions, history, SQLite indexes, logs, memories, and caches remain volume-local and must never be replaced from the shared host directory. MCP OAuth `.credentials.json` is synchronized by the host launcher between the host Codex dir and the per-repo Docker volume before launch and after exit, so rotating refresh tokens do not diverge. If the preset selects `skill_packs`, each selected skill directory is mounted read-only at `/host-agent-skills/<name>` and copied into `$HOME/.agents/skills` inside the container. If no `skill_packs` are selected, the selected auth block's host agents directory is mounted read-only at `/host-agents` and copied wholesale so globally-installed skills (`npx skills add … -g`) are visible inside the container
  - **OpenCode:** bounded private host/repository configuration snapshot read-only at `/cage-opencode-snapshot`; per-repository state volume at `/home/opencode/.cage-state`; selected skills or the fallback registry copied into that private snapshot before Docker starts; provider `auth.json` and selected `mcp-auth.json` entries reconciled by the host launcher around the run. The final effective configuration is generated under tmpfs-backed `/run`; credentials, selected environment values, and resolved headers never enter Docker `Config.Env`
  - **AWS host CLI (opt-in):** when a preset selects `aws_access = "host-cli"` and a non-empty `aws_profile`, Cage adds a reserved `/usr/local/bin/aws` relay backed by the authenticated host-command bridge. Legacy auth-level AWS values remain a compatibility fallback. The host AWS CLI uses host `~/.aws`/SSO/browser state; those paths and ambient AWS credential variables are not mounted into the container. The capability bypasses Netgate and is rejected with `--net off` or host-native execution
  - **GitHub CLI (all container tools, opt-in via preset identity `gh_auth = true`):** `~/.config/gh` read-only at `/host-gh` (if exists), `GH_TOKEN`/`GITHUB_TOKEN` env var if set
  - Per-repo named Docker volume for persistent state
  - SSH key read-only for git push (if the preset identity configures `ssh_key`)
  - `~/.ssh/known_hosts` read-only (if exists)
- Uses `md5 -q` (macOS) or `md5sum` (Linux) for hashing — auto-detected

**`entrypoint.sh`** (runs inside Claude Code container on every start):
- Runs as root; remaps the `claude` user's UID/GID to match the non-root host user (`HOST_UID`/`HOST_GID` env vars) for correct file ownership in the mounted repo. If an image account already owns either requested ID, the remapper swaps that account onto the target user's old ID; invalid, root, incomplete, or unverifiable mappings fail before tool execution
- Fixes ownership on home dir and volume after UID remapping
- Symlinks `~/.claude.json` into the volume so onboarding state persists across `--rm` restarts
- Reconciles the volume's `~/.claude.json` `mcpServers` to exactly the selected set (central-config HTTP MCP definitions and the stdio bridge), expanding `${VAR}` refs from the env (servers with unset, defaultless vars are skipped with a warning). Host `~/.claude.json` MCP definitions are no longer merged, and stale or manually added volume entries are removed on every launch
- Copies `settings.json` from host read-only mount into writable volume
- Atomically generates `CLAUDE.md` with accurate Cage trust context and appends
  the host file if present; replaces the `agents/` destination before linking it
- Sets `git safe.directory` to handle UID mismatch between host and container
- Sets `user.name`/`user.email` from env vars resolved from the preset identity
- Writes `~/.ssh/config` with SSH host alias if the preset identity sets `ssh_host`
- Copies GitHub CLI config from `/host-gh` into writable `~/.config/gh/` (non-auth settings like git_protocol)
- Switches to the target user via `gosu` before exec'ing `claude`

**`entrypoint-codex.sh`** (runs inside Codex container on every start):
- Same root→user pattern as Claude entrypoint (UID/GID remapping via `gosu`)
- Imports only supported static global configuration (`config.toml`, profile config files, global AGENTS guidance, hooks, and rules), `auth.json` (when enabled), and reconciled `.credentials.json` from `/host-codex`; runtime-owned sessions/history/SQLite/log/memory/cache state remains in the per-repository volume. Both import helpers independently reject every non-allowlisted destination before any removal, and real-Docker CI covers conflicting host/volume runtime entries
- Copies selected `/host-agent-skills/<name>` directories into `$HOME/.agents/skills` when `skill_packs` are selected; otherwise copies `/host-agents` (read-only mount of `~/.agents/`, the npm `skills` CLI registry) into writable home if present, so globally-installed skills work inside the container
- Appends central-config MCP servers to the writable container `~/.codex/config.toml` only. Stdio servers use `mcp-relay`; HTTP servers use native Codex `mcp_servers` entries. Duplicate server names already present in host config fail clearly rather than silently overriding
- Inventories inherited MCPs in the launching runtime. Direct-only untrusted
  profile/project entries receive a same-kind inert transport plus
  `enabled=false`, avoiding Codex's transport-less override failure while
  keeping them disabled across an in-process trust transition. Caller
  `-c`/`--config` assignments under `mcp_servers` are rejected
- Skips `auth.json` when the selected auth block has `copy_auth = false` (for non-OpenAI providers like Azure OpenAI)
- Preserves workspace trust across restarts (saves and restores `[projects]` entries in `config.toml`)
- Sets `git safe.directory`, git identity, SSH config (same as Claude entrypoint)
- Copies GitHub CLI config from `/host-gh` (same as Claude entrypoint)
- Creates the volume-local `sessions/` and `archived_sessions/` directories with
  the target user's ownership so the optional host collector can mount them by
  Docker `volume-subpath` without broadening the volume boundary
- Execs `codex` instead of `claude`

**`entrypoint-opencode.sh`** (runs inside the OpenCode container on every start):
- Uses the same root-to-user UID/GID remapping, Git/SSH/GitHub identity, extra
  mounts, host-command shims, and selected bridge environment as the other
  container entrypoints
- Keeps XDG data, state, and cache in the per-repository state volume, while the
  final XDG configuration is private run state under tmpfs-backed `/run`
- Uses an ephemeral `exec,nosuid,nodev` `/tmp` tmpfs because OpenCode's TUI
  extracts and maps its native renderer there; the other tool containers keep
  the existing non-executable default tmpfs
- Resolves only Cage's bounded host/repository snapshot, rejects unfreezable
  instructions or external skill paths, strips inherited MCPs, generates the
  selected local/remote MCP definitions with private header expansion, and
  validates exact effective MCP transports before execution
- Copies frozen project skills plus selected host skill packs into the final
  OpenCode discovery directory and verifies that no disk skill escaped that
  boundary. When no pack is selected, it uses the existing fallback host agents
  registry behavior
- Runs with `--pure` unless `opencode_plugins=true`, disables in-process updates,
  and maps Cage yolo to `--auto`. Raw `--auto`, project/working-directory
  overrides, unselected MCP auth, MCP mutation, and live server modes are
  rejected before Docker effects

**`Dockerfile`**: Thin leaf image (`FROM cage-base`). Creates the `claude` user, installs Claude Code via official installer, and copies `entrypoint.sh`. Entrypoint runs as root (switches to host UID via gosu). `jq` is required by the statusLine command in the host's `settings.json`.

**`Dockerfile.codex`**: Thin leaf image (`FROM cage-base`). Adds `openssh-server` (Codex-only, for Desktop SSH), creates the `codex` user, installs Codex CLI via `npm install -g @openai/codex`, and copies `entrypoint-codex.sh` and `codex-remote.py`. Same root→gosu pattern as Claude.

**`Dockerfile.opencode`**: Thin leaf image (`FROM cage-base`). Creates the
`opencode` user, installs `opencode-ai@latest` without root-owned lifecycle
scripts, checks the required `--pure`, project/external-skill suppression, and
fixed OAuth callback contracts at image build time, disables OpenCode's
in-process updater, and copies `entrypoint-opencode.sh`.

**`Dockerfile.monitor`**: Pinned, network-disabled Token Monitor collector
image. It verifies the upstream v0.48.0 source archive by SHA-256, installs
only production dependencies and the required architecture-specific token
counter, and runs `token-monitor-collector.js`. The wrapper accepts the
upstream summary only over loopback and writes one bounded JSON result to the
host-provided output bind; it never receives the real hub secret.

**`Dockerfile.base`**: Shared base image (`cage-base:<version>`) containing Ubuntu 24.04, system packages (bash, bubblewrap, ca-certificates, curl, git, gosu, jq, less, procps, python3, pip, venv, ripgrep, sudo), Node.js LTS, GitHub CLI, the `mcp-relay`/`host-cmd-relay` bridge scripts, and the shared fail-closed user-remapping helper. Contains no agent binaries, no entrypoints, no Cage tool accounts, and no `openssh-server`. Published to `ghcr.io/sindycate/cage/base` for CI cache sharing and transparency. See `docs/adr-001-shared-base-image.md` for the architecture decision.

All five Dockerfiles end with OCI version plus `io.cage.managed`,
`io.cage.role`, and `io.cage.version` labels. Local launcher, Compose, update
overlay, CI candidate, and release-promotion paths preserve that identity so
storage cleanup never infers ownership from repository names alone.

**`docker-compose.yml`**: Build-only helper — builds the shared base first, then tags leaf images as `claude-code:latest`, `codex:latest`, `opencode:latest`, and `cage-token-monitor:latest`. Not used for running containers (that's `cage`'s job).

**`netgate-proxy.py`** (host-side, runs when `--net gate` is active):
- Python3 forward proxy that gates outbound HTTP/HTTPS by domain
- Requires a fresh 256-bit per-launch HTTP Basic proxy credential before DNS,
  prompts, or upstream connections; Cage injects the authenticated proxy URL
  automatically
- Handles HTTPS via CONNECT method (sees hostname without TLS decryption)
- Resolves once, rejects mixed/non-public results, and connects to the validated
  numeric endpoint; CONNECT is restricted to 443/8443
- Holds unknown domains' connections open while prompting the user (macOS `osascript` dialog, or terminal prompt on Linux)
- Saves user decisions to allowlist files in `~/.claude/netgate/`
- Pre-allows AWS and OpenAI domains via `netgate/defaults.json`
- Concurrent requests to the same unknown domain show only one dialog (deduplication via threading.Event)

**`netgate/defaults.json`**: Pre-allowed domain patterns (AWS infrastructure, Dash0, GitHub, Linear, OpenAI API). Loaded on every proxy start.

**`mcp-bridge.py`** (host-side, runs when selected `mcp_packs` include stdio servers):
- Python3 TCP relay that bridges host-side MCP commands into the container
- For each configured server, listens on a random TCP port reachable through
  `host.docker.internal` and requires a fresh 256-bit per-launch handshake before
  spawning anything
- Parses configured commands as argv with `shell=False`, runs them from the host
  home directory, and exposes only a minimal environment plus explicitly selected
  variables
- Removes repository/read-write-mount entries from host `PATH`, rejects
  configured executables under those mounts, and pins the resolved executable at
  bridge startup
- Relays MCP bytes unchanged and drains bounded subprocess stderr into Cage's
  private bridge log
- Startup protocol: prints `SERVER:name=PORT:N` per server, then `READY` (same pattern as netgate-proxy.py)

**`mcp-relay`** (runs inside container, installed at `/usr/local/bin/mcp-relay`):
- Tiny Python script that connects container stdio to the host MCP bridge via TCP
- Usage: `mcp-relay <server-name>` — reads the bridge host, port, and per-launch
  authentication token from its environment
- Configured as the MCP server command in Claude Code's `~/.claude.json` (the file Claude reads `mcpServers` from) by the entrypoint
- If the repo has `.mcp.json` with matching server names, Cage generates a
  private patched overlay and mounts it read-only; the repository file is never
  rewritten

**`host-cmd-bridge.py`** (host-side, runs when selected presets include `host_commands`):
- Uses the same authenticated, bounded, `shell=False`, minimal-environment host
  process boundary as the MCP bridge
- Uses a framed protocol that preserves caller arguments, stdin, stdout, stderr,
  structured errors, and final exit status; limits and disconnects terminate the
  subprocess process group
- Startup protocol: prints `COMMAND:name=PORT:N` per command, then `READY`
- Use case: token-refresh commands that need host keychain/auth context (e.g. `[host_commands.ztoken] command = "ztoken"` so Codex's auth command and args are forwarded exactly once inside the container). For compatibility with pre-0.23 definitions, an exact caller suffix already embedded in the configured command is de-duplicated; `cage config doctor` warns so the definition can be simplified.

**`host-cmd-relay`** (runs inside container, installed at `/usr/local/bin/host-cmd-relay`):
- Container-side framed stdio-to-TCP relay — reads the host, port, and per-launch
  authentication token from its environment
- Per-command shims are written to `/usr/local/bin/<name>` by the entrypoint (two-line wrappers that `exec host-cmd-relay <name> "$@"`), so tools inside the container find the command by name in `PATH`

**`Makefile`**: Install/uninstall targets. Both route through `install.sh`; source
installs use the same staged, ownership-checked, rollback-capable path as release
installs.

**`install.sh`**: Curl-pipe-bash installer. Downloads the latest GitHub Release tarball, verifies checksum, extracts to `~/.local/share/cage/`, and symlinks the binary. Also supports `--uninstall`.

**`scripts/build-release.py`**: Builds the source release tarball and checksum
with a fixed file allowlist, normalized ownership and timestamps, deterministic
ordering, and a timestamp-free gzip header. `SOURCE_DATE_EPOCH` is set from the
tagged source commit by the release workflow.

**`scripts/publish_release.py`**: Maintainer-only, deterministic, resumable
release automation (`python3 scripts/publish_release.py`, with `--dry-run` and
`--json`). Standard library only. It validates the prepared release commit,
asks for one explicit confirmation, pushes `main` if needed, waits for the
exact commit's CI run, pushes an immutable annotated tag, waits for
publication, and independently verifies the public release. State and a private
log live under the per-worktree Git dir (`git rev-parse --git-path
cage-release`), guarded by an `fcntl.flock` exclusive lock; remote state is
authoritative and the state file is only a resume hint. The explicit release
confirmation is the only terminal read: every child receives closed stdin, no
controlling TTY, and bounded execution time. Idempotent public reads use fixed retry/backoff,
anonymous Docker pulls have per-attempt and whole-check deadlines, and matching
private journals retain cumulative phase timing plus redacted verification
evidence across resumes. Schema-v2 JSON success and failure results include
workflow URLs, full public-asset digests/sizes, image digests, per-check details,
and timings. Public verification proves source provenance and the source SPDX
SBOM attestation separately. It is not part of the `cage` CLI and is excluded
from the release archive.

**`.github/workflows/ci.yml`**: In addition to the secret-scan, macOS Bash 3.2
installer, and Python 3.12 test/Docker/Desktop gates, a `candidate` job
runs only on a `push` to `main` after every gate passes. It builds the shared
base for `linux/amd64`+`linux/arm64`, publishes it as
`ghcr.io/sindycate/cage/base:candidate-<full-SHA>`, builds all four leaves from
that exact base digest, publishes `claude-code`/`codex`/`opencode`/`token-monitor`
candidate tags, retains BuildKit SBOM and `provenance: mode=max`, signs GitHub
provenance attestations for all five digests, and uploads a schema-v3
`release-candidate-<SHA>` manifest
artifact. Candidate tags are public, write-once, serialized per SHA
(`cancel-in-progress: false`), and never referenced by Cage's pull logic. On a
rerun for the same SHA, an existing candidate is verified (amd64/arm64 platforms
and its `ci.yml` provenance attestation for the exact source SHA) and reused —
its build/attest steps are skipped — while an unverifiable candidate fails
closed rather than being rebuilt. No cross-version BuildKit cache is used.

**`.github/workflows/release.yml`**: Five logical stages, all actions pinned to
immutable commit SHAs (maintained by Dependabot), serialized per tag with
cancellation disabled. (1) **Exact-commit gate**: requires an annotated
`v<VERSION>` tag whose commit matches `CAGE_VERSION` and `GITHUB_SHA`, finds a
successful `ci.yml` push run for exactly that SHA on `main`, downloads its
candidate manifest, and verifies the five candidate digests, their
amd64/arm64 platforms, and their CI attestations (expected repo, exact source
digest, `refs/heads/main`, pinned `ci.yml` signer); fails closed if a version
tag already exists with a different digest. This gate protects manual tag
pushes too. (2) **Source package**: reproducible tarball, archive-content
secret scan, checksum, SPDX SBOM, and source provenance/SBOM attestations. (3)
**Image promotion**: creates immutable version tags from the exact candidate
digests (`docker buildx imagetools create`, never rebuild/QEMU), re-attests
each digest from the release workflow, and only then moves `latest`; idempotent
for resume. (4) **Public consumer gate**: from a fresh empty Docker credential
directory, verifies all five version and `latest` digests, their amd64/arm64
platforms, and literal anonymous pulls. (5) **GitHub Release**: reverifies the
downloaded assets and creates the release last. The
exact successful CI run replaces the duplicated Python/macOS/Docker/history-scan
jobs; the archive-content scan stays because it validates the generated
deliverable.

## Versioning & Release Flow

- Version is defined in `CAGE_VERSION` at the top of the `cage` script (e.g., `CAGE_VERSION="0.1.0"`)
- `cage --version` prints the current version
- Git tags use `v` prefix: `v0.1.0`, `v0.2.0`, etc.
- Docker images are tagged with the version (`claude-code:0.1.0`, `codex:0.1.0`, `opencode:0.1.0`, and `token-monitor:0.1.0`) plus `:latest`, and published to `ghcr.io/sindycate/cage/` as multi-arch (amd64/arm64)
- On first run, cage pulls the pre-built image from ghcr.io; falls back to local build if pull fails
- `--rebuild` forces a local `docker build --no-cache` to get the latest tool version
- Releases are automated via GitHub Actions on tag push
- **Canonical release flow (maintainer-only):** bump `CAGE_VERSION`, update
  `CHANGELOG.md`, commit on `main`, then run `python3 scripts/publish_release.py`.
  It validates the commit, asks for one confirmation
  (`release v<VERSION> from <12-char-SHA>`), pushes `main` if needed, waits for
  the exact CI run (which publishes immutable `candidate-<SHA>` images), pushes
  the immutable annotated tag, waits for the release workflow (which promotes
  the exact candidate digests), and independently verifies the public release.
  It resumes automatically from the current phase; `--dry-run` shows the plan
  without mutating anything and `--json` emits one result object. Do not add it
  to the `cage` CLI or the release archive.
- **Publisher orchestration discipline (including fresh contexts):** treat
  `scripts/publish_release.py` as the single release orchestrator. Once a clean,
  versioned commit is ready, run the real publisher once and leave it attached.
  `--dry-run` is diagnostic, not a mandatory duplicate gate; use it only when
  the release state or planned mutations are genuinely unclear. While the
  publisher is healthy, do not duplicate its work with manual pushes, tags,
  GitHub Release creation, routine `gh run view`/`watch` polling, registry
  probes, installer checks, or a second public-verification pass. Use its own
  milestone output and final schema-v2 JSON result. If the client requires a
  heartbeat, report the last publisher milestone tersely without making another
  external query. After interruption or failure, resume the same journaled
  command; use targeted read-only diagnostics only for the failure it reports,
  and never replace the canonical flow with manual mutations except under the
  documented emergency-recovery rule. This minimizes wall time, tool calls, and
  token use without weakening any release gate.
- **Release completion and product-owner handoff:** local edits, commits, tests,
  worktrees, and Docker images are only a *prepared release candidate*. Do not
  report release-bound work as done, ask the product owner to test it, or hand
  it off as complete until the exact commit is on remote `main`, its required CI
  run succeeds, the immutable annotated version tag and GitHub Release exist,
  the canonical publisher reaches `public_verified`, and a fresh unauthenticated
  `curl` installation of the public release reports the expected `cage
  --version`. Agents own pre-release local and CI validation. The product owner
  tests the installed public release after this gate; never ask them to check
  out or locally test repository `main`, an agent worktree, a local commit, or a
  locally built image unless they explicitly request pre-release testing.
- The active hardening workflow's standing publication authorization is
  sufficient for its in-scope releases. Do not pause for a redundant push/tag/
  release approval; the canonical publisher's exact confirmation is the
  mutation guard. If authentication, CI, publication, or public verification
  cannot complete, report the release as pending or blocked, never done.
- **Every tracked Cage change is release-bound by default:** implementation,
  configuration, documentation, and test changes must proceed from prepared
  validation through the canonical publisher to `public_verified`. Treat
  “test first” or “I’ll test it” as public-release validation, not permission
  to stop at a local checkout. Stop before publication only when the user
  explicitly says “local-only”, “do not publish yet”, or equivalent; label
  that result as unpublished `prepared` work. Check `git status --short` before
  handoff so a dirty or prepared state cannot be presented as complete.
- **Emergency recovery only:** manual `git push` → `git tag v{version}` →
  `git push origin v{version}` → `gh release create`. The release workflow's
  exact-commit gate still protects manual tag pushes. Never skip tagging —
  releases only trigger on `v*` tag push.
- **Every pushed commit gets its own version.** Never push multiple commits under the same version — if a follow-up fix is needed, bump again

## Netgate Management

`cage netgate` manages domain allow/deny lists used by `--net gate` mode.

**Storage:** `~/.claude/netgate/` directory (shared with `netgate-proxy.py`, NOT under `CAGE_CONFIG_DIR`). Three file tiers: `{SCRIPT_DIR}/netgate/defaults.json` (shipped, read-only), `global.json` (user always-allow), `project-{hash}.json` (per-project allow + deny).

**`cage-netgate.sh`** (sourced for `cage netgate` subcommand): list rules, allow/deny domains, remove decisions, reset files. Uses `python3 -c` for JSON manipulation (no jq dependency). Hash computation mirrors the main cage script (`md5 -q` on macOS, `md5sum` on Linux, first 8 chars).

## Remote HTTP MCP servers (Linear, Dash0)

The stdio bridge is for **local stdio** MCP servers selected through `mcp_packs`. `mcp_packs` can also define **remote streamable-HTTP** servers like Linear (`https://mcp.linear.app/mcp`) or Dash0 (`https://api.<region>.aws.dash0.com/mcp`). cage forwards the named token env vars and generates tool-native MCP config inside private runtime/container state: Claude gets `mcpServers` entries in `~/.claude.json`; Codex gets `[mcp_servers.<name>]` entries in `~/.codex/config.toml`; OpenCode gets a sanitized frozen `mcp` object under tmpfs-backed `/run`.

**How it works:**
- The token is forwarded by naming the env var in `mcp_packs.<name>.env` and/or `bearer_token_env_var`, then exporting it in your host shell. The secret is never stored in `config.toml`.
- `*.linear.app` and `*.dash0.com` are pre-allowed in `netgate/defaults.json`, so `--net gate` works without an interactive prompt. (Unlike the stdio bridge, remote MCP makes real HTTPS calls from inside the container, so the domain must be allowlisted. Still incompatible with `--net off`.)

**Linear**:
```toml
[mcp_packs.linear]
env = ["LINEAR_API_KEY"]
servers = [
  { name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" },
]
```

**Dash0** follows the identical pattern; only the URL, token var, and header differ. Dash0's MCP endpoint is **region-specific and per-org** — copy yours from the Dash0 app under **Endpoints → MCP** (e.g. `https://api.eu-central-1.aws.dash0.com/mcp`), and create a token under **Auth Tokens** with All-permissions on your datasets.
```toml
[mcp_packs.dash0]
env = ["DASH0_AUTH_TOKEN"]
servers = [
  { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", bearer_token_env_var = "DASH0_AUTH_TOKEN" },
]
```

If Dash0 requires OAuth instead of an API token, use the OAuth shape:
```toml
[mcp_packs.dash0]
servers = [
  { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", auth = "oauth", oauth_resource = "https://api.eu-central-1.aws.dash0.com", oauth_scopes = ["*"], oauth_client_id_env_var = "DASH0_OAUTH_CLIENT_ID" },
]
```
For Codex, run `cage mcp login dash0 ~/path/to/repo` (or add `--preset NAME`) on
the host. The login command resolves the same preset as launch, runs Codex's
OAuth flow with the resolved host Codex directory as `CODEX_HOME`, and leaves
normal cage launches to generate the MCP config from central TOML. cage sets
Codex's documented `mcp_oauth_credentials_store` to `file` for both operations;
this is separate from `auth.json`, so Codex auth blocks with `copy_auth = false`
still skip only the main Codex login cache.
The host launcher synchronizes Codex MCP OAuth `.credentials.json` between the
host Codex directory and the per-repo Docker volume before launch and after
exit, so providers with rotating refresh tokens do not leave stale copies.

For OpenCode, the same command resolves an OpenCode preset, restricts the
operation to a selected server name, and runs `opencode mcp auth/logout` inside
the prepared container. Cage publishes only the image-contract loopback OAuth
ports on host `127.0.0.1` for the duration of the auth operation. The selected
server entry in `mcp-auth.json` is synchronized around the run; unrelated host
entries remain untouched. OpenCode presets reject `oauth_resource` because the
current OpenCode schema cannot preserve it faithfully.

For Claude, select the same OAuth MCP pack from a Claude preset and authenticate
inside the cage session with `/mcp`. No container port publishing is required
for the first version; if the browser callback cannot reach the container, use
Claude's documented fallback flow to paste the callback URL. Claude generation
uses the URL and optional client ID; shared Codex fields such as
`oauth_resource` and `oauth_scopes` remain accepted in central packs.

## Key Constraints

- Central `config.toml` stores env var names and paths, not secret values. `cage config explain`/`doctor` must redact secrets and report env vars only as set/unset
- Central presets are complete runnable configurations. `--preset NAME` overrides project/default preset selection; explicit `cage claude`/`cage codex`/`cage opencode` must match the resolved preset tool or fail clearly
- Desktop targets are Codex-only, macOS-only, and require a saved or
  project-owned preset. Launch-once TUI configurations fail closed because a
  persistent target must be reconstructable
- `codex_profile` is Codex-only, uses letters/digits/hyphens/underscores, and
  names a separate native file under the resolved `CODEX_HOME`. Cage never
  copies a profile into a different host identity or rewrites it
- Interactive mode is mutually exclusive with `--preset`, requires a TTY, and
  uses the config-authoring TUI. Direct `cage PATH` launches remain unchanged
- Central `mcp_packs` are the **authoritative allowlist** for every Cage session. Only MCP servers selected by the resolved preset may start; an absent or empty `mcp_packs` selection means zero active MCPs. Duplicate MCP server names across selected packs are invalid. For container and desktop targets, stdio MCP servers run on the host through the authenticated MCP bridge and HTTP MCP servers become tool-native container config. For host targets, both become process-local Codex config overrides; stdio commands execute directly as pinned host processes. A selected name already defined in a base/profile/project Codex layer fails closed
- At launch, Cage inventories the inherited MCP servers in the **launching runtime** and disables every server the preset did not select with highest-precedence overrides, supplemented by direct profile/project layer parsing because `codex mcp list` does not enumerate every layer. Loaded servers receive `mcp_servers.<name>.enabled=false`. A direct-only definition that is not loaded yet (especially an untrusted project) receives a same-kind inert transport plus `enabled=false`; this avoids Codex's `invalid transport` failure and remains authoritative if trust is granted in the same process. `target = "host"` inventories the host Codex binary; container launches inventory inside the image (entrypoint, after configuration import); Desktop re-inventories inside the persistent container on every app-server connection so live project changes are still suppressed. Across all three Codex paths, caller arguments may not replace the inventoried profile (`-p`/`--profile`) or repository (`-C`/`--cd`), use `--enable`/`--disable`, select another app-server with `--remote`, ignore the inventoried user layer with `--ignore-user-config`, or set an unallowlisted `-c`/`--config` root; normal prompts and dedicated model/sandbox options remain available, and `--` ends the policy scan for positional/subcommand payload. Desktop selected-MCP authorization metadata is root-owned and non-replaceable by the remote Codex user. Unselected definitions may remain visible as disabled in `codex mcp list`; they never start. For Claude, the volume `mcpServers` is reconciled to the selected set only (host `~/.claude.json` MCP definitions are no longer merged) and a private read-only `.mcp.json` overlay — built from the bridges that actually started, empty under `--net off` — always suppresses repository MCP definitions. `config explain`, `config doctor`, and the TUI review state the policy and list selected servers; for host targets they also list suppressed servers, while container/Desktop disclose the authoritative suppressed set at launch. Cage fails closed when a trustworthy inventory cannot be obtained; there is no legacy inheritance escape hatch
- Central `skill_packs` are composed per Codex or OpenCode preset. Each pack names a source agents registry (usually `~/.agents`) and a list of skill folder names under `source/skills/`. Duplicate skill names across selected packs are invalid, and selected skills must have `SKILL.md`. For container and Desktop targets, Cage mounts and copies only selected skills; when none are selected it falls back to copying `host_agents_dir`. OpenCode additionally freezes project-local skills and verifies its final disk-skill inventory. For host targets, selected packs require the default `~/.agents` source and are applied as a process-local Codex `skills.config` filter without host file mutation
- OAuth HTTP MCP servers are supported for Codex, Claude, and OpenCode presets. `cage mcp login NAME PATH` and `cage mcp logout NAME PATH` dispatch by resolved tool: Codex remains a host-mediated wrapper so browser callbacks happen on the host, while OpenCode runs the selected-only operation inside the container and publishes only its image-contract callback ports on `127.0.0.1`. Claude OAuth login happens inside the cage session through `/mcp`. Cage forces `mcp_oauth_credentials_store = "file"` for Codex and synchronizes `.credentials.json`; OpenCode synchronizes only selected entries in `mcp-auth.json`. Central TOML remains the source of server definitions; do not permanently duplicate OAuth MCP entries in host tool configs unless intentionally debugging.
- `config.toml` is mandatory for launches. Do not reintroduce `cage.conf`,
  legacy Cage profile files, folder mappings, or repo `.cage.conf`; native
  Codex profile files selected by `codex_profile` are a separate supported
  Codex mechanism
- Host `~/.claude` is mounted **read-only** — entrypoint must copy/symlink, never write back
- `~/.claude.json` lives at `$HOME/.claude.json` (outside `$HOME/.claude/`), so the entrypoint symlinks it into the volume. The host file is still mounted read-only at `/host-claude-json` for reference, but its `mcpServers` are **no longer merged**: the entrypoint reconciles the volume `mcpServers` to exactly the selected set (central-config HTTP servers with `${VAR}` expansion, plus stdio-bridge servers from `MCP_SERVERS`). Claude reads `mcpServers` from here, not `settings.json`. Repository-scope MCP definitions are suppressed by a private read-only `.mcp.json` overlay mounted by the launcher
- **Session history sync** (Claude, default on): cage mirrors the entire `~/.claude/projects/-<repo-slug>/` subtree between host and per-repo Docker volume on entry/exit — session JSONLs, `memory/` (persistent memory), per-session `subagents/` and `tool-results/`. All host-side writes happen from the host cage script running as the host user; the container's read-only `/host-claude` mount is unchanged. Merge rules: `*.jsonl` uses size-based "larger wins" (append-only invariant); all other files use mtime-based "newer wins". First-run migration copies the pre-existing `-workspace-<name>/` subtree into the new slug with `cwd` rewritten in every JSONL (including `*/subagents/*.jsonl`), leaving that old session-history dir intact as a fallback. Disable with `session_sync = false` in central config defaults or preset
- Claude auth is configured in central `auth` blocks: `mode = "bedrock"` mounts `~/.aws/credentials`; `mode = "api-key"` passes `ANTHROPIC_API_KEY`
- Profile-pinned AWS CLI access is configured with `aws_access = "host-cli"` and a non-empty `aws_profile` on the selected preset. Legacy auth-level values are accepted only as a compatibility fallback, and preset values take precedence. The generated `aws` shim fixes `--profile` on the host command, rejects profile/configuration/debug overrides plus `configure` and `sso logout`, and leaves IAM policy as the actual permission boundary. `aws sso login` uses the host browser flow. This bridge bypasses Netgate and cannot be combined with `--net off`; do not also define generic `host_commands.aws`
- Codex auth uses `host_codex_dir` in the selected auth block, or `~/.codex` when omitted. Set `copy_auth = false` to skip copying `auth.json` for non-OpenAI providers like Azure OpenAI
- Codex runtime state is per-repository volume data. Host import is allowlisted to user/profile TOML configuration, global AGENTS guidance, hooks/rules, plus the separately governed `auth.json` and `.credentials.json`; never copy or replace shared-host `sessions`, `archived_sessions`, `history.jsonl`, SQLite state, logs, memories, goals, caches, or shell snapshots over a project volume. Keep the fail-closed name checks inside both import helpers and the complete real-Docker preservation fixture when changing this boundary
- Codex skills: set `host_agents_dir` in the selected Codex auth block to define the canonical/fallback agents registry, usually `~/.agents/`. Prefer preset-level `skill_packs` for choosing which skills are available in a cage session. Mounts are conditional on the host paths existing; selected `skill_packs` hard-fail if a selected skill is missing `SKILL.md`
- OpenCode auth uses `host_opencode_config_dir` and
  `host_opencode_data_dir`, defaulting to the host XDG config/data roots. Set
  `copy_auth=false` to remove stale provider auth from that project volume and
  disable provider-store writeback. Static configuration and runtime-owned
  sessions/history/cache/indexes are never synchronized to the host
- GitHub CLI auth is off by default. Set `gh_auth = true` in the selected identity. When enabled: cage auto-extracts the token via `gh auth token` on the host (works with keychain-based auth), or passes `GH_TOKEN`/`GITHUB_TOKEN` env var if set. `~/.config/gh/` is mounted read-only for non-auth settings. Set `gh_account` in the identity for account selection
- Hashing uses `md5 -q` on macOS and `md5sum` on Linux (auto-detected in the cage script)
- Network gating (`--net gate`) only covers HTTP/HTTPS traffic routed via proxy env vars. Raw TCP/SSH/DNS bypass the proxy (including `git push` over SSH)
- Netgate uses a fresh per-launch proxy credential so unrelated local, bridge,
  or LAN clients cannot use the listener; processes inside the selected container
  receive that credential automatically
- Git push requires the selected identity to set `ssh_key` pointing to a private key. Passphrase-protected keys work but will prompt each time (ssh-agent is not available in the container)
- Allowlists: global at `~/.claude/netgate/global.json`, per-project at `~/.claude/netgate/project-{hash}.json`
- When `--net gate`, MCP bridge, host command bridge, or session sync is active, cage does NOT use `exec docker run` (needs shell alive for cleanup)
- MCP bridge runs stdio MCP commands from selected `mcp_packs` on the host and relays stdio MCP protocol into the container via TCP on `host.docker.internal`. Incompatible with `--net off`. When `--net gate` is also active, MCP bridge traffic bypasses the netgate proxy (direct TCP, not HTTP)
- Host command bridge uses selected `host_commands`: each `name=host command` entry gets a TCP listener on the host and a `/usr/local/bin/<name>` shim in the container. Caller arguments are appended; if they exactly equal fixed arguments already present after the configured executable, Cage de-duplicates that suffix solely for pre-0.23 compatibility. Prefer executable-only definitions when the client supplies arguments. Commands run with full host user privileges — treat as opt-in only, like MCP bridge. Incompatible with `--net off`; bypasses netgate when `--net gate` is active
- The built-in AWS host CLI uses the same authenticated bridge but is profile-pinned and reserved as the `aws` shim; it is not a generic host command and is disclosed as a host-integrated capability. The host AWS CLI runs outside Netgate, so `ReadOnly`/`Manual` profile names are descriptive only and IAM policy remains authoritative
- Extra named mounts from preset `extra_mounts`, or `--mount-ro`/`--mount-rw` flags, bind-mount additional host directories at their **same absolute host path** inside the container (mirroring the repo mount), read-only by default. Paths are validated against the same reserved-path guard as the repo (`_is_reserved_mount_path`, a shared function); tildes are expanded and relative paths resolve against cage's launch cwd. Non-existent paths and paths overlapping the repo are **warn-and-skipped**; reserved container paths **hard-fail**. Extra mounts do **not** affect the container/volume name hash (derived from `REPO_PATH` only). Adding a mount requires relaunching cage (Docker fixes bind mounts at `docker run` time). No entrypoint involvement — these are plain bind mounts used in place, not copied into the volume
- **Container security:** Claude, Codex, and OpenCode containers use `apparmor=unconfined` and `seccomp=unconfined` so coding tools can create user namespaces for subprocess isolation/sandboxing. `--cap-drop ALL` still applies. Entrypoints run as root for UID remapping then switch to the target user via `gosu`. Users have passwordless `sudo` for installing packages (Playwright, etc.) — the container itself is the security boundary
