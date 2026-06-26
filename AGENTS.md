# AGENTS.md

This file provides guidance to AI coding agents — Claude Code (claude.ai/code) and OpenAI Codex CLI — when working with code in this repository. It is the canonical source of project instructions; `CLAUDE.md` imports it so Claude Code reads the same content.

## What This Project Is

A Docker-based isolation wrapper for running AI coding assistants (Claude Code, OpenAI Codex CLI) in containers on macOS and Linux. Limits the blast radius so the tool can only write to the mounted repo, not the rest of the host filesystem.

## Build and Run

```bash
# Build/rebuild all images
docker compose build

# Build just one image
docker compose build claude
docker compose build codex

# Rebuild from scratch (e.g., to update tool versions)
docker compose build --no-cache

# Force rebuild to get latest tool version (pulls fresh from upstream)
cage --rebuild ~/path/to/repo

# Fast tool refresh — re-run just the installer layer on top of the existing image
# (seconds, no apt/Node/gh re-run), then re-tag the image cage runs
cage update          # update the default tool
cage update claude
cage update codex

# Run Claude Code against a repo (default)
cage ~/path/to/repo
cage claude ~/path/to/repo

# Run Codex CLI against a repo
cage codex ~/path/to/repo

# Pass args through to the tool
cage ~/path/to/repo --resume
cage ~/path/to/repo -p "do something"

# Central configuration — one TOML file for presets, auth, MCP packs, identities,
# host commands, extra mounts, and project mappings. It is required for launches.
cage config init
cage config edit
cage config list
cage config explain ~/path/to/repo
cage config doctor --preset codex-company ~/path/to/repo
cage --preset codex-company ~/path/to/repo
cage --interactive ~/path/to/repo
cage codex -i ~/path/to/repo

# Yolo mode — skip all permission prompts (safe because containerized)
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

# Remote (HTTP) MCP servers — e.g. Linear — are generated as native tool
# config inside the container, with token env vars forwarded by name.
#   [mcp_packs.linear]
#   env = ["LINEAR_API_KEY"]
#   servers = [
#     { name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" },
#   ]

# Host command bridge — expose host commands (e.g. token minters) inside the container
# In config.toml:
#   [host_commands.ztoken]
#   command = "ztoken token -n codex"
#   [presets.codex-company]
#   host_commands = ["ztoken"]
```

## Architecture

**`cage`** (host-side launcher, symlinked to `~/.local/bin/`):
- Accepts optional subcommand (`cage claude` or `cage codex`) to select tool and `--preset NAME` to select a central runnable configuration
- Supports `--interactive`/`-i` for one-shot ad-hoc launches. Interactive mode prompts from existing central config objects (tool, auth, identity, MCP packs, host commands, net mode, Claude session sync), resolves them as an in-memory `interactive` preset, and never writes back to `config.toml`
- Requires central config at `~/.config/cage/config.toml` for launches. It is parsed by `cage-config.py` (Python 3.11+ `tomllib`) and contains reusable `auth`, `identities`, `mcp_packs`, `host_commands`, `presets`, and `[projects]` mappings. Project mappings use longest-prefix matching
- Acquires Docker images via pull-before-build: tries `docker pull` from `CAGE_REGISTRY` (ghcr.io), falls back to local `docker build` if pull fails. `--rebuild` forces a local build with `--no-cache` (useful for getting the latest tool version)
- `cage update [claude|codex]` refreshes just the tool binary without a full rebuild: it ensures the base image exists (same pull-before-build logic), then builds a tiny overlay image (`docker build --no-cache -f -` reading an inline Dockerfile from stdin) that does `FROM <current image>` and re-runs only the tool installer (Claude: `curl … install.sh`; Codex: `npm install -g @openai/codex@latest`), re-tagging the result over `<tool>:${CAGE_VERSION}` and `:latest`. The image stays the single source of the tool version — this intentionally diverges the local image from the same-tagged registry image; `--rebuild` resets to a clean build. Tool defaults to the central default preset's tool, then `claude` when no config exists
- Takes a repo path, derives a unique container name + Docker volume via md5 hash of the full path
- Runs `docker run` with security hardening (cap_drop ALL, no-new-privileges) and tool-specific mounts:
  - Repo at the **same absolute path as on host** (read-write) — mirrored so Claude's project slug (derived from cwd) matches on both sides, enabling session-history sync. This is the only writable host path. A guard rejects paths that would collide with the container filesystem (`/etc`, `/var`, `/home/claude`, etc.)
  - **Claude (bedrock auth):** `~/.aws/credentials` read-only, `~/.claude` read-only at `/host-claude`
  - **Claude (api-key auth):** `ANTHROPIC_API_KEY` env var, `~/.claude` read-only at `/host-claude`
  - **Claude (ccstatusline):** if `~/.config/ccstatusline/` exists on the host, it is mounted read-only at `/host-ccstatusline` and copied into the volume so a customized ccstatusline status line propagates (ccstatusline stores its config there, separate from `settings.json`)
  - **Codex:** host Codex directory from the preset auth block (default `~/.codex` if omitted) read-only at `/host-codex` for auth, `OPENAI_API_KEY` env var if set. If the selected auth block names a host agents directory, it is mounted read-only at `/host-agents` and copied into the volume so globally-installed skills (`npx skills add … -g`) are visible inside the container
  - **GitHub CLI (both tools, opt-in via preset identity `gh_auth = true`):** `~/.config/gh` read-only at `/host-gh` (if exists), `GH_TOKEN`/`GITHUB_TOKEN` env var if set
  - Per-repo named Docker volume for persistent state
  - SSH key read-only for git push (if the preset identity configures `ssh_key`)
  - `~/.ssh/known_hosts` read-only (if exists)
- Uses `md5 -q` (macOS) or `md5sum` (Linux) for hashing — auto-detected

**`entrypoint.sh`** (runs inside Claude Code container on every start):
- Runs as root; remaps the `claude` user's UID/GID to match the host user (`HOST_UID`/`HOST_GID` env vars) for correct file ownership in the mounted repo
- Fixes ownership on home dir and volume after UID remapping
- Symlinks `~/.claude.json` into the volume so onboarding state persists across `--rm` restarts
- Merges the `mcpServers` key from host `/host-claude-json` (read-only), central-config HTTP MCP definitions, and the stdio bridge into the volume's `~/.claude.json`, expanding `${VAR}` refs from the env (servers with unset, defaultless vars are skipped with a warning)
- Copies `settings.json` from host read-only mount into writable volume
- Symlinks `CLAUDE.md` and `agents/` from host if present
- Sets `git safe.directory` to handle UID mismatch between host and container
- Sets `user.name`/`user.email` from env vars resolved from the preset identity
- Writes `~/.ssh/config` with SSH host alias if the preset identity sets `ssh_host`
- Copies GitHub CLI config from `/host-gh` into writable `~/.config/gh/` (non-auth settings like git_protocol)
- Switches to the target user via `gosu` before exec'ing `claude`

**`entrypoint-codex.sh`** (runs inside Codex container on every start):
- Same root→user pattern as Claude entrypoint (UID/GID remapping via `gosu`)
- Copies config/state files from `/host-codex` (read-only mount of `~/.codex`) into writable volume
- Copies `/host-agents` (read-only mount of `~/.agents/`, the npm `skills` CLI registry) into writable volume if present, so globally-installed skills work inside the container
- Appends central-config MCP servers to the writable container `~/.codex/config.toml` only. Stdio servers use `mcp-relay`; HTTP servers use native Codex `mcp_servers` entries. Duplicate server names already present in host config fail clearly rather than silently overriding
- Skips `auth.json` when the selected auth block has `copy_auth = false` (for non-OpenAI providers like Azure OpenAI)
- Preserves workspace trust across restarts (saves and restores `[projects]` entries in `config.toml`)
- Sets `git safe.directory`, git identity, SSH config (same as Claude entrypoint)
- Copies GitHub CLI config from `/host-gh` (same as Claude entrypoint)
- Execs `codex` instead of `claude`

**`Dockerfile`**: Ubuntu 24.04, installs Python 3, Node.js LTS, GitHub CLI, bubblewrap, sudo, gosu, and Claude Code via official installer. Entrypoint runs as root (switches to host UID via gosu). `jq` is required by the statusLine command in the host's `settings.json`.

**`Dockerfile.codex`**: Ubuntu 24.04 + Python 3 + GitHub CLI + Node.js LTS, installs Codex CLI via `npm install -g @openai/codex`. Same root→gosu pattern as Claude.

**`docker-compose.yml`**: Build-only helper — tags images as `claude-code:latest` and `codex:latest`. Not used for running containers (that's `cage`'s job).

**`netgate-proxy.py`** (host-side, runs when `--net gate` is active):
- Python3 forward proxy that gates outbound HTTP/HTTPS by domain
- Handles HTTPS via CONNECT method (sees hostname without TLS decryption)
- Holds unknown domains' connections open while prompting the user (macOS `osascript` dialog, or terminal prompt on Linux)
- Saves user decisions to allowlist files in `~/.claude/netgate/`
- Pre-allows AWS and OpenAI domains via `netgate/defaults.json`
- Concurrent requests to the same unknown domain show only one dialog (deduplication via threading.Event)

**`netgate/defaults.json`**: Pre-allowed domain patterns (AWS infrastructure, Dash0, GitHub, Linear, OpenAI API). Loaded on every proxy start.

**`mcp-bridge.py`** (host-side, runs when selected `mcp_packs` include stdio servers):
- Python3 TCP relay that bridges host-side MCP commands into the container
- For each configured server, listens on a random TCP port on 127.0.0.1
- On incoming connection (from container via `host.docker.internal`), spawns the configured command and relays bidirectionally between TCP and subprocess stdio
- Auth tokens are resolved on the host at connection time — handles token expiry naturally
- Startup protocol: prints `SERVER:name=PORT:N` per server, then `READY` (same pattern as netgate-proxy.py)

**`mcp-relay`** (runs inside container, installed at `/usr/local/bin/mcp-relay`):
- Tiny Python script that connects container stdio to the host MCP bridge via TCP
- Usage: `mcp-relay <server-name>` — reads `MCP_BRIDGE_HOST` and `MCP_BRIDGE_PORT_<NAME>` env vars
- Configured as the MCP server command in Claude Code's `~/.claude.json` (the file Claude reads `mcpServers` from) by the entrypoint
- If the repo has `.mcp.json` with matching server names, cage patches it before launch and restores the original on exit

**`host-cmd-bridge.py`** (host-side, runs when selected presets include `host_commands`):
- Same pattern as `mcp-bridge.py`: per-command TCP listener on `127.0.0.1`, spawns the host command on each incoming connection, relays stdio bidirectionally
- Unlike MCP bridge, subprocess stderr is forwarded to the bridge's own stderr so command errors surface in the cage terminal (MCP bridge sends to DEVNULL to protect JSON-RPC framing)
- Startup protocol: prints `COMMAND:name=PORT:N` per command, then `READY`
- Use case: token-refresh commands that need host keychain/auth context (e.g. `[host_commands.ztoken] command = "ztoken token -n codex"` so Codex's `auth.command = "ztoken"` config keeps working inside the container across indefinite sessions)

**`host-cmd-relay`** (runs inside container, installed at `/usr/local/bin/host-cmd-relay`):
- Container-side stdio-to-TCP relay — reads `HOST_CMD_BRIDGE_HOST` and `HOST_CMD_BRIDGE_PORT_<NAME>`
- Per-command shims are written to `/usr/local/bin/<name>` by the entrypoint (two-line wrappers that `exec host-cmd-relay <name> "$@"`), so tools inside the container find the command by name in `PATH`

**`Makefile`**: Install/uninstall targets. `make install` copies files to `~/.local/share/cage/` and symlinks to `~/.local/bin/cage`.

**`install.sh`**: Curl-pipe-bash installer. Downloads the latest GitHub Release tarball, verifies checksum, extracts to `~/.local/share/cage/`, and symlinks the binary. Also supports `--uninstall`.

**`.github/workflows/release.yml`**: Creates a GitHub Release with tarball and SHA-256 checksum when a `v*` tag is pushed. Also builds and pushes multi-arch (amd64/arm64) Docker images to `ghcr.io/sindycate/cage/` via `docker/build-push-action`. Verifies that the tag matches `CAGE_VERSION` in the cage script.

## Versioning & Release Flow

- Version is defined in `CAGE_VERSION` at the top of the `cage` script (e.g., `CAGE_VERSION="0.1.0"`)
- `cage --version` prints the current version
- Git tags use `v` prefix: `v0.1.0`, `v0.2.0`, etc.
- Docker images are tagged with the version (`claude-code:0.1.0`) plus `:latest`, and published to `ghcr.io/sindycate/cage/` as multi-arch (amd64/arm64)
- On first run, cage pulls the pre-built image from ghcr.io; falls back to local build if pull fails
- `--rebuild` forces a local `docker build --no-cache` to get the latest tool version
- Releases are automated via GitHub Actions on tag push
- **Release flow:** bump `CAGE_VERSION` → commit → push → `git tag v{version}` → `git push origin v{version}`. Never skip tagging — releases only trigger on `v*` tag push
- **Every pushed commit gets its own version.** Never push multiple commits under the same version — if a follow-up fix is needed, bump again

## Netgate Management

`cage netgate` manages domain allow/deny lists used by `--net gate` mode.

**Storage:** `~/.claude/netgate/` directory (shared with `netgate-proxy.py`, NOT under `CAGE_CONFIG_DIR`). Three file tiers: `{SCRIPT_DIR}/netgate/defaults.json` (shipped, read-only), `global.json` (user always-allow), `project-{hash}.json` (per-project allow + deny).

**`cage-netgate.sh`** (sourced for `cage netgate` subcommand): list rules, allow/deny domains, remove decisions, reset files. Uses `python3 -c` for JSON manipulation (no jq dependency). Hash computation mirrors the main cage script (`md5 -q` on macOS, `md5sum` on Linux, first 8 chars).

## Remote HTTP MCP servers (Linear, Dash0)

The stdio bridge is for **local stdio** MCP servers selected through `mcp_packs`. `mcp_packs` can also define **remote streamable-HTTP** servers like Linear (`https://mcp.linear.app/mcp`) or Dash0 (`https://api.<region>.aws.dash0.com/mcp`). cage forwards the named token env vars and generates tool-native MCP config inside the writable container state only: Claude gets `mcpServers` entries in `~/.claude.json`; Codex gets `[mcp_servers.<name>]` entries in `~/.codex/config.toml`.

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

## Key Constraints

- Central `config.toml` stores env var names and paths, not secret values. `cage config explain`/`doctor` must redact secrets and report env vars only as set/unset
- Central presets are complete runnable configurations. `--preset NAME` overrides project/default preset selection; explicit `cage claude`/`cage codex` must match the resolved preset tool or fail clearly
- Interactive mode is a one-shot composition layer over central config blocks. It is mutually exclusive with `--preset`, requires a TTY, and must not save selections unless a separate config-authoring feature is explicitly added
- Central `mcp_packs` are composed per preset. Duplicate MCP server names across selected packs are invalid. Stdio MCP servers still run on the host through the MCP bridge; HTTP MCP servers are generated as tool-native container config
- `config.toml` is mandatory for launches. Do not reintroduce `cage.conf`, profiles, folder mappings, or repo `.cage.conf`
- Host `~/.claude` is mounted **read-only** — entrypoint must copy/symlink, never write back
- `~/.claude.json` lives at `$HOME/.claude.json` (outside `$HOME/.claude/`), so the entrypoint symlinks it into the volume. The host file is also mounted read-only at `/host-claude-json`; the entrypoint copies **only** its `mcpServers` key into the volume copy (with `${VAR}` expansion), so user-scope MCP servers (e.g. Linear) work in-container while onboarding/account/history stay isolated. This is also where stdio-bridge servers (`MCP_SERVERS`) land — Claude reads `mcpServers` from here, not `settings.json`
- **Session history sync** (Claude, default on): cage mirrors the entire `~/.claude/projects/-<repo-slug>/` subtree between host and per-repo Docker volume on entry/exit — session JSONLs, `memory/` (persistent memory), per-session `subagents/` and `tool-results/`. All host-side writes happen from the host cage script running as the host user; the container's read-only `/host-claude` mount is unchanged. Merge rules: `*.jsonl` uses size-based "larger wins" (append-only invariant); all other files use mtime-based "newer wins". First-run migration copies the pre-existing `-workspace-<name>/` subtree into the new slug with `cwd` rewritten in every JSONL (including `*/subagents/*.jsonl`), leaving that old session-history dir intact as a fallback. Disable with `session_sync = false` in central config defaults or preset
- Claude auth is configured in central `auth` blocks: `mode = "bedrock"` mounts `~/.aws/credentials`; `mode = "api-key"` passes `ANTHROPIC_API_KEY`
- Codex auth uses `host_codex_dir` in the selected auth block, or `~/.codex` when omitted. Set `copy_auth = false` to skip copying `auth.json` for non-OpenAI providers like Azure OpenAI
- Per-preset npm skills directory: set `host_agents_dir` in the selected Codex auth block to mount an alternate skills registry instead of `~/.agents/`. Mount is conditional on the host directory existing
- GitHub CLI auth is off by default. Set `gh_auth = true` in the selected identity. When enabled: cage auto-extracts the token via `gh auth token` on the host (works with keychain-based auth), or passes `GH_TOKEN`/`GITHUB_TOKEN` env var if set. `~/.config/gh/` is mounted read-only for non-auth settings. Set `gh_account` in the identity for account selection
- Hashing uses `md5 -q` on macOS and `md5sum` on Linux (auto-detected in the cage script)
- Network gating (`--net gate`) only covers HTTP/HTTPS traffic routed via proxy env vars. Raw TCP/SSH/DNS bypass the proxy (including `git push` over SSH)
- Git push requires the selected identity to set `ssh_key` pointing to a private key. Passphrase-protected keys work but will prompt each time (ssh-agent is not available in the container)
- Allowlists: global at `~/.claude/netgate/global.json`, per-project at `~/.claude/netgate/project-{hash}.json`
- When `--net gate`, MCP bridge, host command bridge, or session sync is active, cage does NOT use `exec docker run` (needs shell alive for cleanup)
- MCP bridge runs stdio MCP commands from selected `mcp_packs` on the host and relays stdio MCP protocol into the container via TCP on `host.docker.internal`. Incompatible with `--net off`. When `--net gate` is also active, MCP bridge traffic bypasses the netgate proxy (direct TCP, not HTTP)
- Host command bridge uses selected `host_commands`: each `name=host command` entry gets a TCP listener on the host and a `/usr/local/bin/<name>` shim in the container. Commands run with full host user privileges — treat as opt-in only, like MCP bridge. Incompatible with `--net off`; bypasses netgate when `--net gate` is active
- Extra named mounts from preset `extra_mounts`, or `--mount-ro`/`--mount-rw` flags, bind-mount additional host directories at their **same absolute host path** inside the container (mirroring the repo mount), read-only by default. Paths are validated against the same reserved-path guard as the repo (`_is_reserved_mount_path`, a shared function); tildes are expanded and relative paths resolve against cage's launch cwd. Non-existent paths and paths overlapping the repo are **warn-and-skipped**; reserved container paths **hard-fail**. Extra mounts do **not** affect the container/volume name hash (derived from `REPO_PATH` only). Adding a mount requires relaunching cage (Docker fixes bind mounts at `docker run` time). No entrypoint involvement — these are plain bind mounts used in place, not copied into the volume
- **Container security:** Both Claude and Codex containers use `apparmor=unconfined` and `seccomp=unconfined` so bubblewrap can create user namespaces for subprocess isolation/sandboxing. `--cap-drop ALL` still applies. Entrypoints run as root for UID remapping then switch to the target user via `gosu`. Users have passwordless `sudo` for installing packages (Playwright, etc.) — the container itself is the security boundary
