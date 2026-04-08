# cage

Run AI coding assistants ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex)) in Docker containers so they can only touch the repo you point them at — nothing else on your machine.

Born after a sub-agent deleted ~200GB of files on a MacBook. Never again.

## What it does

- Runs Claude Code or Codex CLI inside a hardened Docker container (all capabilities dropped, no privilege escalation)
- Only the target repo is mounted read-write — the sole blast radius
- Auth credentials and tool settings are mounted read-only
- Per-repo persistent state via Docker volumes (sessions, onboarding survive restarts)
- Reuses your host settings automatically

## Requirements

- macOS or Linux (Ubuntu, etc.)
- Docker + Docker Compose (macOS: [Colima](https://github.com/abiosoft/colima) or Docker Desktop)
- Python 3 (for network gating)
- **Claude Code:** `ANTHROPIC_API_KEY` env var, or AWS Bedrock credentials in `~/.aws/credentials`
- **Codex CLI:** Codex auth on host (`~/.codex/`) or `OPENAI_API_KEY` env var

Start Colima with enough memory (macOS, Claude Code needs 4GB+):

```bash
colima start --cpu 4 --memory 8 --disk 100
```

## Install

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Sindycate/cage/main/install.sh | bash
```

This downloads the latest release, installs to `~/.local/share/cage/`, and symlinks `cage` to `~/.local/bin/`.

### From source

```bash
git clone git@github.com:Sindycate/cage.git ~/cage
cd ~/cage
make install     # installs to ~/.local/bin/cage
```

### Manual

```bash
git clone git@github.com:Sindycate/cage.git ~/cage
cd ~/cage
chmod +x cage
ln -sf ~/cage/cage ~/.local/bin/cage
```

Docker images are built automatically on first run. To pre-build:

```bash
docker compose build              # both images
docker compose build claude       # just Claude Code
docker compose build codex        # just Codex CLI
```

## Usage

```bash
# Run Claude Code against a repo (default)
cage ~/projects/myapp
cage claude ~/projects/myapp     # explicit

# Run Codex CLI against a repo
cage codex ~/projects/myapp

# Yolo mode — skip all permission prompts (safe because containerized)
# Automatically enables domain-gated networking
cage -y ~/projects/myapp
cage codex -y ~/projects/myapp

# Explicit network gating (prompts for each new domain)
cage --net gate ~/projects/myapp

# No network at all
cage --net off ~/projects/myapp

# Pass any tool args through
cage ~/projects/myapp --resume
cage ~/projects/myapp -p "fix the failing tests"

# Multiple repos in parallel (separate terminals)
cage ~/repo-a   # terminal 1
cage ~/repo-b   # terminal 2
```

### Default tool

By default, `cage ~/repo` runs Claude Code. Override with `CAGE_DEFAULT` in your config:

```bash
# ~/.config/cage/cage.conf
CAGE_DEFAULT=codex
```

### Authentication

**Claude Code** supports two auth modes, set via `CLAUDE_AUTH` in `cage.conf`:

```bash
# ~/.config/cage/cage.conf

# Option 1: API key (simple — set ANTHROPIC_API_KEY in your shell env)
CLAUDE_AUTH=api-key

# Option 2: AWS Bedrock (default)
CLAUDE_AUTH=bedrock
AWS_PROFILE=your-profile
AWS_REGION=us-east-1
```

**Codex CLI** authenticates via `~/.codex/` (sign in on host first with `codex`), or `OPENAI_API_KEY` env var.

## How it works

`cage` is a small bash script that runs `docker run` with hardened security. Mounts vary by tool:

**Claude Code** (`cage claude ~/repo`):

| Mount | Path in container | Access |
|-------|-------------------|--------|
| Your repo | `/workspace` | **read-write** |
| `~/.aws/credentials` *(bedrock only)* | `/home/claude/.aws/credentials` | read-only |
| `~/.claude` | `/host-claude` | read-only |
| Docker volume (per-repo) | `/home/claude/.claude` | read-write |
| SSH key (from `cage.conf`) | `/home/claude/.ssh/id` | read-only |
| `~/.ssh/known_hosts` | `/home/claude/.ssh/known_hosts` | read-only |

**Codex CLI** (`cage codex ~/repo`):

| Mount | Path in container | Access |
|-------|-------------------|--------|
| Your repo | `/workspace` | **read-write** |
| `~/.codex` | `/host-codex` | read-only |
| Docker volume (per-repo) | `/home/codex/.codex` | read-write |
| SSH key (from `cage.conf`) | `/home/codex/.ssh/id` | read-only |
| `~/.ssh/known_hosts` | `/home/codex/.ssh/known_hosts` | read-only |

Everything else — your home directory, OS config, other repos — is not accessible to the container.

On each start, the entrypoint copies host settings into the container's writable volume. For Claude Code, this includes `settings.json`, `CLAUDE.md`, and `agents/`. For Codex, auth/config files from `~/.codex/` are copied in.

## Git commit & push

To enable git commit and push inside the container, create a config file:

```bash
# ~/.config/cage/cage.conf (global defaults)
GIT_USER_NAME="Your Name"
GIT_USER_EMAIL="you@example.com"
SSH_KEY="~/.ssh/id_ed25519"
SSH_HOST="github-alias=github.com"   # optional: resolve SSH host aliases
```

Per-project overrides: place a `.cage.conf` in the repo root (same format). Values override the global config.

```bash
# ~/my-repo/.cage.conf (per-project)
GIT_USER_NAME="DifferentName"
GIT_USER_EMAIL="other@example.com"
SSH_KEY="~/.ssh/other_key"
```

**Limitations:**
- SSH keys must be unencrypted (no passphrase) — ssh-agent is not available in the container
- Git push over SSH bypasses `--net gate` (raw TCP, not HTTP)
- With `--net off`, push is blocked entirely (no network)

## Updating

Check your current version:

```bash
cage --version
```

### Installed via one-liner

Re-run the install script — it downloads the latest release:

```bash
curl -fsSL https://raw.githubusercontent.com/Sindycate/cage/main/install.sh | bash
```

### Installed from source

```bash
cd ~/cage
git pull
make install
```

Docker images are rebuilt automatically on the next `cage` run after a version bump (the new versioned tag triggers a build).

To force-rebuild Docker images (e.g., to pick up new tool versions):

```bash
docker compose build --no-cache           # rebuild both images
docker compose build --no-cache claude     # just Claude Code
docker compose build --no-cache codex      # just Codex CLI
```

### Uninstall

```bash
# If installed via one-liner:
curl -fsSL https://raw.githubusercontent.com/Sindycate/cage/main/install.sh | bash -s -- --uninstall

# If installed via make:
cd ~/cage && make uninstall
```

## Managing state

```bash
# List active containers
docker ps --filter "name=claude-"
docker ps --filter "name=codex-"

# List per-repo state volumes
docker volume ls --filter "name=claude-state-"
docker volume ls --filter "name=codex-state-"

# Reset state for a repo
docker volume rm claude-state-<name>
docker volume rm codex-state-<name>
```

## Network gating

With `--net gate`, all outbound HTTP/HTTPS from the container routes through a host-side proxy that prompts you before allowing access to new domains.

**How it works:**
1. A Python proxy starts on the host and binds to a random port
2. The container gets `HTTP_PROXY`/`HTTPS_PROXY` env vars pointing to it
3. When Claude Code (or any tool) tries to reach a new domain, a macOS dialog pops up
4. You choose: **Allow (project)**, **Allow (always)**, or **Deny**
5. The connection is held open during the prompt — no failed first request

**Pre-allowed domains:** AWS infrastructure (`*.amazonaws.com`, `*.amazontrust.com`, `*.cloudfront.net`) and OpenAI API (`*.openai.com`, `*.oaiusercontent.com`, `*.oaistatic.com`) are always allowed.

**Allowlist storage:**
- Global (all projects): `~/.claude/netgate/global.json`
- Per-project: `~/.claude/netgate/project-{hash}.json`
- Manually edit these files to add/remove domains

**Yolo + gating:** `cage -y` defaults to `--net gate`. Override with `cage -y --net open` if you want full network access.

## Limitations

- Network gating dialogs use native macOS popups (`osascript`); on Linux, prompts appear in the terminal
- Network gating only covers HTTP/HTTPS via proxy env vars — raw TCP, SSH, and DNS bypass the proxy
- The mounted repo is still fully writable — but it's git-tracked, so worst case you `git checkout .`
