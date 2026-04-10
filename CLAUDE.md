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
```

## Architecture

**`cage`** (host-side launcher, symlinked to `~/.local/bin/`):
- Accepts optional subcommand (`cage claude` or `cage codex`) to select tool; defaults to `claude` (overridable via `CAGE_DEFAULT` in `cage.conf`)
- Takes a repo path, derives a unique container name + Docker volume via md5 hash of the full path
- Loads config from `~/.config/cage/cage.conf` (global) then `<repo>/.cage.conf` (per-project override)
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
- Symlinks `~/.claude.json` into the volume so onboarding state persists across `--rm` restarts
- Copies `settings.json` from host read-only mount into writable volume
- Symlinks `CLAUDE.md` and `agents/` from host if present
- Sets `git safe.directory` to handle UID mismatch between host and container
- Sets `user.name`/`user.email` from env vars (passed from `cage.conf`)
- Writes `~/.ssh/config` with SSH host alias if `SSH_HOST` is set
- Copies GitHub CLI config from `/host-gh` into writable `~/.config/gh/` (non-auth settings like git_protocol)

**`entrypoint-codex.sh`** (runs inside Codex container on every start):
- Copies config/state files from `/host-codex` (read-only mount of `~/.codex`) into writable volume
- Skips `auth.json` when `CODEX_COPY_AUTH=0` (for non-OpenAI providers like Azure OpenAI)
- Preserves workspace trust across restarts (saves and restores `[projects]` entries in `config.toml`)
- Sets `git safe.directory`, git identity, SSH config (same as Claude entrypoint)
- Copies GitHub CLI config from `/host-gh` (same as Claude entrypoint)
- Execs `codex` instead of `claude`

**`Dockerfile`**: Ubuntu 24.04, installs GitHub CLI and Claude Code via official installer, runs as non-root `claude` user. `jq` is required by the statusLine command in the host's `settings.json`.

**`Dockerfile.codex`**: Ubuntu 24.04 + GitHub CLI + Node.js LTS, installs Codex CLI via `npm install -g @openai/codex`, runs as non-root `codex` user.

**`docker-compose.yml`**: Build-only helper — tags images as `claude-code:latest` and `codex:latest`. Not used for running containers (that's `cage`'s job).

**`netgate-proxy.py`** (host-side, runs when `--net gate` is active):
- Python3 forward proxy that gates outbound HTTP/HTTPS by domain
- Handles HTTPS via CONNECT method (sees hostname without TLS decryption)
- Holds unknown domains' connections open while prompting the user (macOS `osascript` dialog, or terminal prompt on Linux)
- Saves user decisions to allowlist files in `~/.claude/netgate/`
- Pre-allows AWS and OpenAI domains via `netgate/defaults.json`
- Concurrent requests to the same unknown domain show only one dialog (deduplication via threading.Event)

**`netgate/defaults.json`**: Pre-allowed domain patterns (AWS infrastructure, GitHub, OpenAI API). Loaded on every proxy start.

**`Makefile`**: Install/uninstall targets. `make install` copies files to `~/.local/share/cage/` and symlinks to `~/.local/bin/cage`.

**`install.sh`**: Curl-pipe-bash installer. Downloads the latest GitHub Release tarball, verifies checksum, extracts to `~/.local/share/cage/`, and symlinks the binary. Also supports `--uninstall`.

**`.github/workflows/release.yml`**: Creates a GitHub Release with tarball and SHA-256 checksum when a `v*` tag is pushed. Verifies that the tag matches `CAGE_VERSION` in the cage script.

## Versioning & Release Flow

- Version is defined in `CAGE_VERSION` at the top of the `cage` script (e.g., `CAGE_VERSION="0.1.0"`)
- `cage --version` prints the current version
- Git tags use `v` prefix: `v0.1.0`, `v0.2.0`, etc.
- Docker images are tagged with the version (`claude-code:0.1.0`) plus `:latest`
- Upgrading cage triggers automatic Docker image rebuilds (the new versioned tag doesn't exist yet)
- Releases are automated via GitHub Actions on tag push
- **Release flow:** bump `CAGE_VERSION` → commit → push → `git tag v{version}` → `git push origin v{version}`. Never skip tagging — releases only trigger on `v*` tag push
- **Every pushed commit gets its own version.** Never push multiple commits under the same version — if a follow-up fix is needed, bump again

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
- When `--net gate` is active, cage does NOT use `exec docker run` (needs shell alive for proxy cleanup)
- **Codex security:** Codex containers use `apparmor=unconfined` and `seccomp=unconfined` so bubblewrap can create user namespaces for tool sandboxing. `no-new-privileges` is also omitted for Codex. `--cap-drop ALL` still applies. Claude containers retain full lockdown (`no-new-privileges`, default seccomp + AppArmor)
