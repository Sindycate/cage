# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

# Run Claude Code against a repo (default)
cage ~/path/to/repo
cage claude ~/path/to/repo

# Run Codex CLI against a repo
cage codex ~/path/to/repo

# Pass args through to the tool
cage ~/path/to/repo --resume
cage ~/path/to/repo -p "do something"

# Yolo mode — skip all permission prompts (safe because containerized)
# Yolo defaults to --net gate (domain-gated networking)
cage -y ~/path/to/repo
cage codex -y ~/path/to/repo

# Explicit network gating without yolo
cage --net gate ~/path/to/repo

# No network at all
cage --net off ~/path/to/repo

# MCP bridge — forward host-side MCP servers into the container
# In cage.conf or .cage.conf:
#   MCP_SERVERS="myserver=some-tool --mcp-proxy https://example.com/mcp"
```

## Architecture

**`cage`** (host-side launcher, symlinked to `~/.local/bin/`):
- Accepts optional subcommand (`cage claude` or `cage codex`) to select tool; defaults to `claude` (overridable via `CAGE_DEFAULT` in `cage.conf`)
- Acquires Docker images via pull-before-build: tries `docker pull` from `CAGE_REGISTRY` (ghcr.io), falls back to local `docker build` if pull fails. `--rebuild` forces a local build with `--no-cache` (useful for getting the latest tool version)
- Takes a repo path, derives a unique container name + Docker volume via md5 hash of the full path
- Loads config in layers: `~/.config/cage/cage.conf` (global) → `profiles/<name>.conf` (named profile) → `<repo>/.cage.conf` (per-project override)
- Runs `docker run` with security hardening (cap_drop ALL, no-new-privileges) and tool-specific mounts:
  - Repo at `/workspace/{reponame}` (read-write) — the only writable host path; unique path gives each project its own identity in Claude Code
  - **Claude (bedrock auth):** `~/.aws/credentials` read-only, `~/.claude` read-only at `/host-claude`
  - **Claude (api-key auth):** `ANTHROPIC_API_KEY` env var, `~/.claude` read-only at `/host-claude`
  - **Codex:** `~/.codex` read-only at `/host-codex` for auth, `OPENAI_API_KEY` env var if set
  - **GitHub CLI (both tools, opt-in via `GH_AUTH=1`):** `~/.config/gh` read-only at `/host-gh` (if exists), `GH_TOKEN`/`GITHUB_TOKEN` env var if set
  - Per-repo named Docker volume for persistent state
  - SSH key read-only for git push (if `SSH_KEY` configured)
  - `~/.ssh/known_hosts` read-only (if exists)
- Uses `md5 -q` (macOS) or `md5sum` (Linux) for hashing — auto-detected

**`entrypoint.sh`** (runs inside Claude Code container on every start):
- Runs as root; remaps the `claude` user's UID/GID to match the host user (`HOST_UID`/`HOST_GID` env vars) for correct file ownership in the mounted repo
- Fixes ownership on home dir and volume after UID remapping
- Symlinks `~/.claude.json` into the volume so onboarding state persists across `--rm` restarts
- Copies `settings.json` from host read-only mount into writable volume
- Symlinks `CLAUDE.md` and `agents/` from host if present
- Sets `git safe.directory` to handle UID mismatch between host and container
- Sets `user.name`/`user.email` from env vars (passed from `cage.conf`)
- Writes `~/.ssh/config` with SSH host alias if `SSH_HOST` is set
- Copies GitHub CLI config from `/host-gh` into writable `~/.config/gh/` (non-auth settings like git_protocol)
- Switches to the target user via `gosu` before exec'ing `claude`

**`entrypoint-codex.sh`** (runs inside Codex container on every start):
- Same root→user pattern as Claude entrypoint (UID/GID remapping via `gosu`)
- Copies config/state files from `/host-codex` (read-only mount of `~/.codex`) into writable volume
- Skips `auth.json` when `CODEX_COPY_AUTH=0` (for non-OpenAI providers like Azure OpenAI)
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

**`netgate/defaults.json`**: Pre-allowed domain patterns (AWS infrastructure, GitHub, OpenAI API). Loaded on every proxy start.

**`mcp-bridge.py`** (host-side, runs when `MCP_SERVERS` is configured):
- Python3 TCP relay that bridges host-side MCP commands into the container
- For each configured server, listens on a random TCP port on 127.0.0.1
- On incoming connection (from container via `host.docker.internal`), spawns the configured command and relays bidirectionally between TCP and subprocess stdio
- Auth tokens are resolved on the host at connection time — handles token expiry naturally
- Startup protocol: prints `SERVER:name=PORT:N` per server, then `READY` (same pattern as netgate-proxy.py)

**`mcp-relay`** (runs inside container, installed at `/usr/local/bin/mcp-relay`):
- Tiny Python script that connects container stdio to the host MCP bridge via TCP
- Usage: `mcp-relay <server-name>` — reads `MCP_BRIDGE_HOST` and `MCP_BRIDGE_PORT_<NAME>` env vars
- Configured as the MCP server command in Claude Code's `settings.json` by the entrypoint

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

## Profiles

Named profiles allow switching between configurations (e.g., work vs personal) without editing files per-repo.

**Storage:** `~/.config/cage/profiles/<name>.conf` — same format as `cage.conf`. Created via `cage setup --profile <name>`.

**Config loading order:** `cage.conf` (global defaults) → `profiles/<name>.conf` (profile overrides) → `.cage.conf` (per-project override, always wins). Users with no profiles get the original two-layer loading unchanged.

**Profile resolution priority:**
1. `--profile <name>` CLI flag (one-shot, not persisted)
2. `CAGE_PROFILE=<name>` in repo's `.cage.conf` (peeked via grep before full source)
3. Lookup in `~/.config/cage/folder-profiles` (persistent folder→profile mapping)
4. Interactive prompt (if 2+ profiles exist, TTY, and no mapping yet — choice is saved)

**`folder-profiles`** file: flat `<absolute-path>=<profile-name>` lines. `_none_` means "no profile, don't prompt again".

**`cage-profiles.sh`** (sourced for `cage profiles` subcommand): list profiles + mappings, show a profile, set/reset folder mappings.

**Profile name validation:** `[a-zA-Z0-9_-]` only.

## Netgate Management

`cage netgate` manages domain allow/deny lists used by `--net gate` mode.

**Storage:** `~/.claude/netgate/` directory (shared with `netgate-proxy.py`, NOT under `CAGE_CONFIG_DIR`). Three file tiers: `{SCRIPT_DIR}/netgate/defaults.json` (shipped, read-only), `global.json` (user always-allow), `project-{hash}.json` (per-project allow + deny).

**`cage-netgate.sh`** (sourced for `cage netgate` subcommand): list rules, allow/deny domains, remove decisions, reset files. Uses `python3 -c` for JSON manipulation (no jq dependency). Hash computation mirrors the main cage script (`md5 -q` on macOS, `md5sum` on Linux, first 8 chars).

## Key Constraints

- Host `~/.claude` is mounted **read-only** — entrypoint must copy/symlink, never write back
- `~/.claude.json` lives at `$HOME/.claude.json` (outside `$HOME/.claude/`), so the entrypoint symlinks it into the volume
- Claude auth is configured via `CLAUDE_AUTH` in `cage.conf`: `bedrock` (mounts `~/.aws/credentials`) or `api-key` (passes `ANTHROPIC_API_KEY` env var)
- Codex auth uses `~/.codex/` directory (sign in on host first) or `OPENAI_API_KEY` env var. Set `CODEX_COPY_AUTH=0` in `cage.conf` to skip copying `auth.json` (for non-OpenAI providers like Azure OpenAI)
- GitHub CLI auth is **off by default**. Set `GH_AUTH=1` in `cage.conf` to enable. When enabled: cage auto-extracts the token via `gh auth token` on the host (works with keychain-based auth), or passes `GH_TOKEN`/`GITHUB_TOKEN` env var if set. `~/.config/gh/` is mounted read-only for non-auth settings. Set `GH_ACCOUNT` in `.cage.conf` for per-project account selection
- Hashing uses `md5 -q` on macOS and `md5sum` on Linux (auto-detected in the cage script)
- Network gating (`--net gate`) only covers HTTP/HTTPS traffic routed via proxy env vars. Raw TCP/SSH/DNS bypass the proxy (including `git push` over SSH)
- Git push requires `cage.conf` with `SSH_KEY` pointing to a private key. Passphrase-protected keys work but will prompt each time (ssh-agent is not available in the container)
- Allowlists: global at `~/.claude/netgate/global.json`, per-project at `~/.claude/netgate/project-{hash}.json`
- When `--net gate` or MCP bridge is active, cage does NOT use `exec docker run` (needs shell alive for cleanup)
- MCP bridge (`MCP_SERVERS` in cage.conf) runs host commands and relays stdio MCP protocol into the container via TCP on `host.docker.internal`. Incompatible with `--net off`. When `--net gate` is also active, MCP bridge traffic bypasses the netgate proxy (direct TCP, not HTTP)
- **Container security:** Both Claude and Codex containers use `apparmor=unconfined` and `seccomp=unconfined` so bubblewrap can create user namespaces for subprocess isolation/sandboxing. `--cap-drop ALL` still applies. Entrypoints run as root for UID remapping then switch to the target user via `gosu`. Users have passwordless `sudo` for installing packages (Playwright, etc.) — the container itself is the security boundary
