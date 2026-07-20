# cage

Run AI coding assistants ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [OpenAI Codex CLI](https://github.com/openai/codex)) in Docker containers to reduce the host filesystem blast radius of accidental or over-broad tool actions.

Born after a sub-agent deleted ~200GB of files on a MacBook. Never again.

## What it does

- Runs Claude Code or Codex CLI inside a Docker container with an isolated home
- Mounts the target repo read-write and makes extra host mounts explicit
- Reuses host credentials and supported configuration automatically for a low-friction workflow
- Per-repo persistent state via Docker volumes (sessions, onboarding survive restarts)

Cage's current security boundary is designed primarily for accidental filesystem
damage, not hostile repository code. Read-only credentials can still be read and
used, `.git` is writable, host integrations extend authority outside the
container, and proxy-based network gating can be deliberately bypassed. Read the
[security model](SECURITY.md) before using Cage with untrusted code or powerful
credentials.

## Requirements

- macOS or Linux (Ubuntu, etc.)
- Docker + Docker Compose (macOS: [Colima](https://github.com/abiosoft/colima) or Docker Desktop)
- Python 3.11+ (for central config parsing; also used by network gating)
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

# Run a named central preset
cage --preset codex-company ~/projects/myapp

# Pick an ad-hoc one-shot preset from configured auth/identity/MCP blocks
cage --interactive ~/projects/myapp
cage codex -i ~/projects/myapp

# Inspect what will run before launching
cage config explain ~/projects/myapp
cage config doctor --preset codex-company ~/projects/myapp

# Yolo mode — skip the coding tool's permission prompts
# Automatically enables proxy-based domain approval, which is advisory rather
# than enforced against code that ignores the proxy
cage -y ~/projects/myapp
cage codex -y ~/projects/myapp

# Yolo mode with full network access (no domain gating)
cage -y --net open ~/projects/myapp

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

### Central configuration

Create a single TOML config with reusable presets, auth blocks, identities, MCP packs, and project mappings:

```bash
cage config init
cage config edit
cage config list
cage config explain ~/projects/myapp
cage config doctor --preset codex-company ~/projects/myapp
```

`~/.config/cage/config.toml` is required for launches. `cage config explain` shows exactly which preset, auth block, MCP packs, skill packs, env vars, mounts, and identity will be used.

Use `cage --interactive ~/projects/myapp` for a one-shot ad-hoc launch. It prompts for the tool, auth block, identity, MCP packs, Codex skill packs, host commands, network mode, and Claude session sync from objects already defined in `config.toml`; it does not save or edit the config.

```toml
version = 1
default_preset = "codex-work"

[defaults]
net = "gate"

[auth.codex-work]
tool = "codex"
host_codex_dir = "~/.codex-work"
host_agents_dir = "~/.agents"
copy_auth = true

[auth.codex-company-proxy]
tool = "codex"
host_codex_dir = "~/.codex-company"
host_agents_dir = "~/.agents"
copy_auth = false
env = ["COMPANY_OPENAI_API_KEY", "OPENAI_BASE_URL"]

[identities.work]
git_user_name = "Your Name"
git_user_email = "you@example.com"
gh_auth = true
gh_account = "work"

[mcp_packs.linear]
env = ["LINEAR_API_KEY"]
servers = [
  { name = "linear", type = "http", url = "https://mcp.linear.app/mcp", bearer_token_env_var = "LINEAR_API_KEY" },
]

[mcp_packs.dash0]
servers = [
  { name = "dash0", type = "http", url = "https://api.eu-central-1.aws.dash0.com/mcp", auth = "oauth", oauth_resource = "https://api.eu-central-1.aws.dash0.com", oauth_scopes = ["*"], oauth_client_id_env_var = "DASH0_OAUTH_CLIENT_ID" },
]

[mcp_packs.local-tools]
servers = [
  { name = "jira", type = "stdio", command = "npx -y @company/jira-mcp" },
]

[skill_packs.agent-basics]
source = "~/.agents"
skills = ["agents-best-practices"]

[skill_packs.external-systems]
source = "~/.agents"
skills = ["linear-ticket-flow", "dash0-dashboard-flow"]

[presets.codex-work]
tool = "codex"
auth = "codex-work"
identity = "work"
mcp_packs = ["linear", "dash0", "local-tools"]
skill_packs = ["agent-basics", "external-systems"]
net = "gate"

[presets.codex-company-debug]
tool = "codex"
auth = "codex-company-proxy"
identity = "work"
mcp_packs = ["linear"]
skill_packs = ["agent-basics", "external-systems"]
net = "gate"

[projects]
"/Users/me/projects/myapp" = "codex-work"
```

Use project defaults or override per run:

```bash
cage ~/projects/myapp
cage --preset codex-company-debug ~/projects/myapp
cage --interactive ~/projects/myapp
```

For Codex OAuth MCP servers such as Dash0, authenticate on the host once per
Codex auth directory:

```bash
cage mcp login dash0 ~/projects/myapp
cage mcp logout dash0 ~/projects/myapp
```

The browser callback runs on the host, so no container port publishing is
needed. The central TOML remains the source of the MCP server definition. cage
forces Codex's MCP OAuth credential store to file mode for these logins and
for container launches; this is separate from `auth.json`, so auth blocks with
`copy_auth = false` still skip the main Codex login cache.

For Codex, cage synchronizes `.credentials.json` between the resolved host
Codex directory and the per-repo Docker volume before launch and after exit.
This keeps providers that rotate MCP OAuth refresh tokens, such as Dash0, from
leaving stale token copies in either place.

Codex runtime state remains owned by that per-repository volume. Cage imports
supported static global configuration (`config.toml`, profile config files,
global AGENTS guidance, hooks, and rules) and governed credentials, but does not
import or replace shared-host sessions, history, SQLite indexes, logs, memories,
or caches. If history is unexpectedly absent, preserve the volume and
investigate it; do not reset or delete the volume as a first recovery step.
The import helpers enforce that allowlist before removing any destination and
abort the launch on every unsupported file or directory name. CI and tagged
releases exercise the real Codex entrypoint with conflicting host and volume
runtime state so this boundary cannot regress silently.

Host command definitions should name the executable when Codex supplies its own
arguments. For example, a custom provider whose Codex auth configuration runs
`ztoken token -n codex` should use:

```toml
[host_commands.ztoken]
command = "ztoken"

[presets.codex-company]
host_commands = ["ztoken"]
```

Cage still de-duplicates an exact caller suffix already embedded in a legacy
host-command definition, but `cage config doctor` warns about that compatibility
path so the definition can be simplified.

For Claude OAuth MCP servers, select the same central MCP pack from a Claude
preset and authenticate inside the cage session with Claude's `/mcp` command.
No container port publishing is required for this first version; if the browser
callback cannot reach the container, use Claude's fallback flow to paste the
callback URL. Claude generation uses the server URL plus optional client ID;
shared Codex fields such as `oauth_resource` and `oauth_scopes` may stay in the
central pack.

### Authentication

Authentication is selected by the preset's `auth` reference. Secrets stay in environment variables or existing tool auth directories; `config.toml` stores only paths and env var names.

Claude Bedrock:

```toml
[auth.claude-bedrock]
tool = "claude"
mode = "bedrock"
aws_profile = "your-profile"
aws_region = "us-east-1"
```

Claude API key:

```toml
[auth.claude-api]
tool = "claude"
mode = "api-key"
env = ["ANTHROPIC_API_KEY"]
```

Codex using a separate host config directory:

```toml
[auth.codex-work]
tool = "codex"
host_codex_dir = "~/.codex-work"
host_agents_dir = "~/.agents"
copy_auth = true
```

Codex with a custom OpenAI-compatible proxy:

```toml
[auth.codex-proxy]
tool = "codex"
host_codex_dir = "~/.codex-company"
copy_auth = false
env = ["COMPANY_OPENAI_API_KEY", "OPENAI_BASE_URL"]
```

## How it works

`cage` is a host launcher around `docker run`. It constructs mounts, generated
tool configuration, state synchronization, optional host integrations, and
network mode from the selected central preset. Mounts vary by tool:

**Claude Code** (`cage claude ~/repo`):

| Mount | Path in container | Access |
|-------|-------------------|--------|
| Your repo | same absolute path as on host | **read-write** |
| `~/.aws/credentials` *(bedrock only)* | `/home/claude/.aws/credentials` | read-only |
| `~/.claude` | `/host-claude` | read-only |
| Docker volume (per-repo) | `/home/claude/.claude` | read-write |
| SSH key (from preset identity) | `/home/claude/.ssh/id` | read-only |
| `~/.ssh/known_hosts` | `/home/claude/.ssh/known_hosts` | read-only |

**Codex CLI** (`cage codex ~/repo`):

| Mount | Path in container | Access |
|-------|-------------------|--------|
| Your repo | same absolute path as on host | **read-write** |
| Codex host directory from preset auth | `/host-codex` | read-only |
| Selected Codex skills from `skill_packs` | `/host-agent-skills/<name>` | read-only |
| Legacy Codex agents directory *(only when no `skill_packs` are selected)* | `/host-agents` | read-only |
| Docker volume (per-repo) | `/home/codex/.codex` | read-write |
| SSH key (from preset identity) | `/home/codex/.ssh/id` | read-only |
| `~/.ssh/known_hosts` | `/home/codex/.ssh/known_hosts` | read-only |

Unlisted host paths are not directly mounted. Selected host commands/MCP bridges
can still access the host with the configured command's authority, credentials
can be used from inside the container, and enabled session/OAuth synchronization
writes selected state back outside the repository. See [SECURITY.md](SECURITY.md).

On each start, the entrypoint copies host settings into the container's writable volume. For Claude Code, this includes `settings.json`, `CLAUDE.md`, and `agents/`. For Codex, auth/config files from `~/.codex/` are copied in; selected skill-pack skills are copied into `$HOME/.agents/skills`, or the whole host agents directory is copied when no `skill_packs` are selected. Codex MCP OAuth credentials in `.credentials.json` are synchronized by the host launcher before and after the run so refresh-token rotation persists outside the container volume.

## Git commit & push

To enable git commit and push inside the container, define an identity and attach it to a preset:

```toml
[identities.work]
git_user_name = "Your Name"
git_user_email = "you@example.com"
ssh_key = "~/.ssh/id_ed25519"
ssh_host = "github-alias=github.com" # optional
gh_auth = true
gh_account = "work"

[presets.codex-work]
tool = "codex"
auth = "codex-work"
identity = "work"
```

**Limitations:**
- Passphrase-protected SSH keys prompt on each use because ssh-agent is not forwarded
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

To force-rebuild the versioned image Cage actually launches:

```bash
cage --rebuild ~/path/to/repo
cage codex --rebuild ~/path/to/repo
```

### Verify release provenance

Tagged releases publish the source archive, its SHA-256 checksum, and an SPDX
SBOM. GitHub also records signed provenance and SBOM attestations for the source
archive. The two GHCR images carry BuildKit SBOM and max-level provenance
metadata plus a signed GitHub provenance attestation.

```bash
VERSION="$(cage --version | awk '{print $NF}')"
gh release download "v${VERSION}" --repo Sindycate/cage \
  --pattern "cage-${VERSION}.tar.gz" \
  --pattern "cage-${VERSION}.tar.gz.sha256" \
  --pattern "cage-${VERSION}.spdx.json"
if command -v shasum >/dev/null 2>&1; then
  shasum -a 256 -c "cage-${VERSION}.tar.gz.sha256"
else
  sha256sum --check "cage-${VERSION}.tar.gz.sha256"
fi
gh attestation verify "cage-${VERSION}.tar.gz" --repo Sindycate/cage
gh attestation verify "cage-${VERSION}.tar.gz" --repo Sindycate/cage \
  --predicate-type https://spdx.dev/Document/v2.3
gh attestation verify \
  "oci://ghcr.io/sindycate/cage/codex:${VERSION}" \
  --repo Sindycate/cage
```

Repeat the image command with `claude-code` to verify that image. Attestations
link an artifact to its source and build workflow; an SBOM inventories detected
components. Neither is a guarantee that the artifact is vulnerability-free.

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

With `--net gate`, proxy-aware HTTP/HTTPS clients receive a host-side proxy that
prompts before allowing new domains. This is an approval and visibility helper,
not enforced egress isolation: code can ignore the proxy environment variables.

**How it works:**
1. A Python proxy starts on the host on a random port with a fresh per-launch credential
2. The container gets authenticated `HTTP_PROXY`/`HTTPS_PROXY` URLs pointing to it; no manual credential setup is required
3. When Claude Code (or any tool) tries to reach a new domain, a macOS dialog pops up
4. You choose: **Allow (project)**, **Allow (always)**, or **Deny**
5. The connection is held open during the prompt — no failed first request

**Pre-allowed domains:** AWS infrastructure (`*.amazonaws.com`, `*.amazontrust.com`, `*.cloudfront.net`) and OpenAI API (`*.openai.com`, `*.oaiusercontent.com`, `*.oaistatic.com`) are allowed without a prompt. Some are broad shared-hosting domains; use `--net off` when deliberate exfiltration resistance is required.

**Allowlist storage:**
- Global (all projects): `~/.claude/netgate/global.json`
- Per-project: `~/.claude/netgate/project-{hash}.json`
- Manually edit these files to add/remove domains

**Yolo + gating:** `cage -y` defaults to `--net gate`. Override with `cage -y --net open` if you want full network access.

## Limitations

- Network gating dialogs use native macOS popups (`osascript`); on Linux, prompts appear in the terminal
- Network gating only covers HTTP/HTTPS via proxy env vars — raw TCP, SSH, and DNS bypass the proxy
- The per-launch proxy credential prevents unrelated local/LAN clients from using Netgate, but any process in the selected container can read and use that credential
- The repository, including `.git`, ignored files, and untracked files, is fully writable; `git checkout .` is not a complete recovery mechanism
- Read-only credential mounts prevent modification, not reading, use, copying, or exfiltration
- MCP/host-command bridges and external connector actions extend the blast radius beyond local files
