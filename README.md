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
docker compose build              # shared base, then both agent images
docker compose build base         # just the agent-neutral shared base
docker compose build claude       # Claude Code (requires the base)
docker compose build codex        # Codex CLI (requires the base)
```

## Usage

```bash
# Open the interactive launcher for the current directory
cage

# Run Claude Code against a repo (default)
cage ~/projects/myapp
cage claude ~/projects/myapp     # explicit

# Run Codex CLI against a repo
cage codex ~/projects/myapp

# Run a named central preset
cage --preset codex-company ~/projects/myapp

# Open the interactive launcher for a specific project
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

# Multiple sessions for the same repo (choose "Start a parallel instance")
cage ~/repo-a   # terminal 3; reuses repo-a's persistent state volume
```

Same-project parallel sessions use distinct container names and share the
project's persistent tool-state volume. The collision menu remains available
when a terminal provides an interactive stdin but restricts direct access to
`/dev/tty`, as some IDE and sandboxed terminals do.

### Central configuration

Create a single TOML config with reusable presets, auth blocks, identities, MCP packs, and project mappings:

```bash
cage config init
cage config edit
cage config list
cage config explain ~/projects/myapp
cage config doctor --preset codex-company ~/projects/myapp
```

`~/.config/cage/config.toml` is required for launches. Run bare `cage` from a
project to open the terminal UI. It shows the effective configuration rather
than requiring you to remember preset names, and supports three launch paths:
launch once, remember the selection for this project, or save it as a named
reusable configuration.

The UI also manages defaults, Docker storage guardrails, reusable configurations, auth profiles,
identities, MCP and skill packs, host commands, project mappings, mounts, and
Claude history sync. Codex OAuth MCP login and logout actions are also available
from the management screen, so their host browser flow does not require a
separate memorized command. Saved changes remain in this one canonical TOML file.
Edits are concurrency-checked and atomic; untouched tables and comments are
preserved, and the ten newest private backups are kept under
`~/.config/cage/backups/`. If the source changed after the UI opened, Cage asks
you to reload instead of attempting an implicit merge.

Menus support arrow keys, `j`/`k`, Home/End, Page Up/Down, Enter, and
Escape/`q`. Checkbox lists toggle with Space or Enter and keep the cursor on the
selected row. Text fields are prefilled and support Left/Right, Home/End,
Backspace/Delete, Unicode, and long values; erase all text (or enter `-`) to
clear an optional field, and press Escape to cancel immediately.
Confirmation screens keep scrollable risk details separate from the input
field and require typing `yes`.

Before saving or launching a high-authority configuration, the UI presents a
separate risk review for yolo mode, open networking, read-write mounts, host
commands, GitHub credentials, and authenticated integrations. It reports only
environment-variable names and whether they are set; secret values are never
written to `config.toml`.

After customization, **Remember for this exact project** is highlighted by
default but is never applied without selection and confirmation. **Launch
once** discards the customization after the session. **Save named reusable
configuration** creates or explicitly overwrites a named preset but leaves the
current project mapping unchanged.

`cage PATH` and `cage --preset NAME PATH` remain direct, non-interactive
launches. `cage --interactive [PATH]` opens the same UI, with the path defaulting
to the current directory. If curses is unavailable, Cage falls back to its
launch-only numbered prompt. `cage config edit`, `list`, `show`, `explain`,
`doctor`, and `set-project` remain available for scripting and recovery.

```toml
version = 1
default_preset = "codex-work"

[defaults]
net = "gate"

[storage]
warn_free_gib = 20
critical_free_gib = 5
min_build_free_gib = 20
keep_versions = 2
dangling_min_age_hours = 24

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
# Optional. Explicit --yolo or --no-yolo wins for a single launch.
yolo = false

[presets.codex-company-debug]
tool = "codex"
auth = "codex-company-proxy"
identity = "work"
# Optional: layer ~/.codex-company/provider-preview.config.toml.
codex_profile = "provider-preview"
mcp_packs = ["linear"]
skill_packs = ["agent-basics", "external-systems"]
net = "gate"

[projects]
"/Users/me/projects/myapp" = "codex-work"
```

### Authoritative MCP selection

`mcp_packs` is the authoritative allowlist for every Cage session. **Only the
MCP servers selected by the resolved preset may start.** An absent or empty
`mcp_packs` selection means zero active MCPs.

At launch, Cage inventories the MCP servers the tool would otherwise inherit
(from user, profile, project, system, and plugin configuration layers) and
disables every server the preset did not select with highest-precedence
overrides. Loaded servers receive `mcp_servers.<name>.enabled=false`. A direct
profile/project definition that Codex has not loaded yet (notably in a newly
cloned, untrusted repository) receives a same-kind inert transport plus
`enabled=false`; this prevents Codex's transport-less-table error and keeps the
server disabled if trust is granted during that same process. The inventory
always runs in the **launching runtime**:

- `target = "host"` inventories the host Codex binary (`mcp list --json`).
- Container launches inventory inside the image (the entrypoint runs the
  container's `codex mcp list --json` after importing configuration).
- Desktop re-inventories inside the persistent container on **every** app-server
  connection, so a project MCP added to the live repository mount after the
  target started is still suppressed.

Unselected servers may still appear as disabled in `codex mcp list`, but they
never start, fail initialization, or become model-accessible.

Codex launch arguments cannot replace the inventoried profile (`-p` /
`--profile`) or repository (`-C` / `--cd`), and cannot change feature flags
with `--enable` / `--disable`, because each can introduce configuration or
plugin layers after the inventory is built. Caller `-c` / `--config`
assignments use a small allowlist of runtime-only roots such as model, sandbox,
approval, personality, shell-environment, and web-search settings; MCP,
plugin, feature, project, and unknown roots fail closed. Normal positional
prompts and dedicated options such as `--model` and `--sandbox` remain
available. `--remote` and `--remote-auth-token-env` are rejected because they
would hand execution to an app-server runtime Cage did not inventory, and
`--ignore-user-config` is rejected because it would remove a layer after Cage
classified its MCP transports. The conventional `--` delimiter ends this
policy scan, so later tokens are preserved as positional/subcommand payload.

For Claude, Cage no longer merges host `~/.claude.json` MCP definitions. The
volume `mcpServers` is reconciled to exactly the selected set, and a private
read-only `.mcp.json` overlay (built from the bridges that actually started, so
`--net off` leaves it empty) always suppresses repository MCP definitions.

`cage config explain`, `cage config doctor`, and the TUI review state
`MCP policy: selected packs only` and list the selected active servers. For
`target = "host"` they also list the suppressed inherited servers; for container
and Desktop targets the authoritative suppressed set is disclosed at launch by
the runtime (the host cannot enumerate the image's layers without launching).
Cage fails closed when a trustworthy MCP inventory cannot be obtained.

**Migration:** move any user-, profile-, or repository-defined MCP you want into
a central `[mcp_packs.*]` block and select it explicitly in the preset. See
`docs/hardening/MIGRATIONS.md`.

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

### Named Codex profiles and providers

A Codex preset can select one of Codex's native named configuration profiles:

```toml
[presets.codex-provider]
tool = "codex"
auth = "codex-work"
codex_profile = "provider-preview"
net = "gate"
```

The corresponding file is
`$CODEX_HOME/provider-preview.config.toml` (for the example auth block above,
`~/.codex-work/provider-preview.config.toml`). Cage validates that the file exists
and launches Codex with `--profile provider-preview` in either container or host
mode. The base `$CODEX_HOME/config.toml` is loaded first and the named profile
is layered over it. Profile names may contain letters, digits, hyphens, and
underscores. The TUI lists valid `*.config.toml` profile files from the Codex
home selected by the preset's auth block.

The profile file is native Codex configuration, so it can select a
`model_provider`, model, reasoning level, approval policy, or
`approvals_reviewer = "auto_review"`. Cage does not implement or reinterpret a
provider protocol: provider compatibility, authentication, and auto-review
behavior remain Codex/provider capabilities. Keep secrets out of Cage TOML;
select environment variable names in the auth/MCP blocks and export the values
in the launching shell. In host mode, a provider's native Codex
`model_providers.<name>.auth.command` already runs on the host and can use
host-installed credential helpers directly; Cage `host_commands` are
container-side bridges and remain unsupported there. See Codex's official
[profile documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles),
[provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers),
and [auto-review documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review).

## Host-native execution (no Docker)

For maintenance tasks that need access outside the repository, Cage can run
Codex directly on the host without a Docker container:

```bash
# One-run override (does not alter saved presets)
cage --host ~/path/to/repo
cage codex --host -y --net open ~/path/to/repo

# Saved in a preset
# [presets.host-maintenance]
# tool = "codex"
# target = "host"
# net = "open"

# Force container for a saved host preset
cage --container ~/path/to/repo
```

Host execution is **Codex-only** and provides:

- **No Docker isolation** — Codex runs with full host-user file access.
- **No Cage network restriction** — `--net gate`/`off` are rejected. Yolo's
  implicit gate default is also rejected; use `--net open` explicitly.
- Process-scoped Git identity (`GIT_CONFIG_COUNT`/`KEY`/`VALUE`), SSH key
  (`GIT_SSH_COMMAND`), and GitHub token (`GH_TOKEN`) — no host config mutation.
- Native Codex profile selection with `codex_profile`.
- Selected remote MCP servers as process-local Codex `-c` overrides.
- Selected stdio MCP servers as pinned host executables. They run directly with
  host-user authority and are rejected when the executable resolves inside the
  writable repository.
- Selected skill packs as a process-local Codex skill filter when they come
  from the documented default `~/.agents/skills` registry. Other discovered
  user skills are disabled for that launch; no registry or Codex config file is
  rewritten.
- Pinned Codex executable — rejected if inside the repository.

Unsupported in host mode (fail closed): host command bridges, extra mounts,
custom `host_agents_dir`/skill-pack sources, and `ssh_host` aliases.

## ChatGPT Desktop via Cage

On macOS, a saved Codex preset can back ChatGPT Desktop through the official
SSH-host workflow:

```bash
./install.sh --from-source
cage desktop setup
cage --preset codex-company --desktop ~/path/to/repo
```

After a target has been registered, open the normal TUI with `cage` and choose
**Manage Desktop targets**. The screen lists all registered targets regardless
of the configuration selected for the current folder, shows live status, and
offers start/recover, restart, recent logs, stop, and confirmed removal. Each
action uses the selected target's own saved preset and repository, so it
reconnects the same persistent volume and history.

The equivalent lifecycle commands remain available for scripts:

```bash
cage desktop status --preset codex-company ~/path/to/repo
cage desktop restart --preset codex-company ~/path/to/repo
cage desktop stop --preset codex-company ~/path/to/repo
cage desktop logs --preset codex-company ~/path/to/repo
cage desktop list
cage desktop remove --preset codex-company ~/path/to/repo
```

The first launch displays a generated `cage-...` SSH host. In ChatGPT, open
**Settings → Connections**, select that host, and choose the displayed
repository path. Cage opens ChatGPT automatically after readiness; pass
`--no-open` to suppress it.

`target = "desktop"` is Codex-only and requires a saved or project-owned
preset. Repository plus preset gets a dedicated persistent Codex volume and
SSH identity, so providers cannot overwrite one another's configuration or
history. `stop` preserves that state; `remove` requires confirmation and
deletes it.

Mac sleep does not count as missed supervisor time. After wake, the watchdog
grants a fresh active-time heartbeat window; if the supervisor is genuinely
gone, the container still shuts down fail-closed after that window.

No SSH or app-server port is published. The generated host uses an absolute
installed Cage helper as `ProxyCommand`, which runs one inetd-mode SSH
connection inside the labeled container. Netgate, skills, HTTP/stdio MCP,
host commands, mounts, Git/GitHub/SSH identity, and OAuth reconciliation reuse
the ordinary container launch path. Provider, proxy, and bridge secrets are
removed from Docker `Config.Env`, handed through a short-lived private bind,
and retained only in the container's tmpfs-backed `/run` state. See
[docs/CODEX_DESKTOP.md](docs/CODEX_DESKTOP.md) for setup and boundaries.

## How it works

`cage` is a small Bash 3.2-compatible bootstrap around a Python 3.11
standard-library host core. The core parses a request, resolves central
configuration, and validates an immutable launch plan before inspecting images,
changing Docker state, starting bridges, or synchronizing persistent state.
Host, container, and Desktop execution consume that same plan and shared
security policy. See
[ADR-002](docs/adr-002-python-host-launcher.md) for the dependency boundaries
and redacted internal contract.

The host core constructs mounts, generated tool configuration, state
synchronization, optional host integrations, and network mode from the selected
central preset. Mounts vary by tool:

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

## Maintainer release process

Releases are published with one maintainer-only command:

```bash
python3 scripts/publish_release.py            # run / resume a release
python3 scripts/publish_release.py --dry-run  # read-only discovery + planned mutations
python3 scripts/publish_release.py --json     # one JSON result object on stdout
```

`scripts/publish_release.py` is maintainer tooling. It is not part of the
end-user `cage` CLI and is excluded from the release archive. It uses only the
Python 3.11 standard library and requires `git`, `gh`, `docker`, `curl`, and
`/bin/bash`. The script owns the only terminal read (the explicit confirmation):
all child commands receive closed stdin in a new session with no controlling
TTY, so a pseudo-TTY cannot open a nested Cage, credential, or editor prompt.
External commands and public checks have fixed timeouts that terminate their
detached process groups; safe public reads retry with bounded, visible backoff,
and interrupted or failed attempts retain redacted diagnostics and cumulative
phase timings in the private resume journal.

What it does, in order:

1. Validates the prepared release commit (clean tree, on `main`, `origin`
   resolves to `Sindycate/cage`, local `main` ahead by at most one commit,
   `CAGE_VERSION`/`CHANGELOG.md`/tag agree, existing tags/releases/digests
   absent or matching, plus local test/compile/shell/compose/archive gates).
2. Detects the current release phase and resumes automatically. Phases are
   `local_ready`, `main_pushed`, `ci_passed`, `tag_pushed`,
   `release_workflow_passed`, and `public_verified`. Remote state (Git refs,
   workflow results, GHCR digests, the GitHub Release) is authoritative; the
   private state file under the per-worktree Git dir is only a resume hint.
3. Shows the repository, full commit SHA, version, tag, resumed phase, and the
   exact remaining remote mutations, then requires the maintainer to type
   exactly:

   ```text
   release v<VERSION> from <12-character-SHA>
   ```

   `--dry-run` displays the same plan and changes nothing. If only public
   verification remains, no confirmation is required.
4. Pushes `main` (explicit refspec from the recorded SHA), waits for the exact
   commit's `ci.yml` push run, creates and pushes the immutable annotated tag
   `v<VERSION>`, waits for the tag-triggered release workflow, and then
   independently verifies the public release.

### Candidate images

To reduce release time safely, a successful `ci.yml` run on `main` publishes
immutable `candidate-<full-commit-sha>` images for `base`, `claude-code`, and
`codex`, each with BuildKit SBOM, max-level provenance, and a signed GitHub
provenance attestation, plus a small `release-candidate-<SHA>` manifest
artifact. Candidate tags are public and write-once, but they are not stable
releases and are never referenced by Cage's image-pull logic. The
tag-triggered release workflow verifies the exact candidate digests and
attestations and *promotes* them to the version and `latest` tags — it never
rebuilds images or resolves mutable package sources. There is intentionally no
cross-version BuildKit cache: Cage resolves current packages on each release.

The first live run of this design was `v0.26.9` (commit
`a5e6cbb196d1a0a09f67a3aaea23a3250b07649d`). The measurements distinguish
the cold candidate build from the authorization-time tag path:

| Measurement | `v0.26.2` baseline | `v0.26.9` candidate flow |
|---|---:|---:|
| Cold multi-arch build/publish stage | about 12m30s tag path | 9m58s candidate job (12m14s complete CI) |
| Exact-SHA reuse authorization path | none; tag path rebuilt | 1m48s tag to public |
| Cross-run warm BuildKit cache | not configured (N/A) | not configured (N/A) |
| Branch CI start to public release | preceding CI not recorded | 14m15s |

The comparable tag-to-public path fell from about 750 seconds to 108 seconds
(about 86%, or 6.9x faster). This is not a warm BuildKit-cache result: the
multi-architecture images are still built cold once in exact-commit branch CI,
then the release workflow promotes those immutable, attested digests. The
trade-off is that authorization-time publication is short and deterministic,
while the expensive build moves earlier rather than disappearing. Evidence:
[`v0.26.9` CI](https://github.com/Sindycate/cage/actions/runs/30712870965),
[`v0.26.9` release](https://github.com/Sindycate/cage/actions/runs/30713346455),
and [issue #6](https://github.com/Sindycate/cage/issues/6).

### Public verification

After the release workflow succeeds, the command independently verifies that
the GitHub Release is public (non-draft, non-prerelease) with exactly the
archive, checksum, and SPDX assets; that the assets download anonymously and
the checksum passes; that the SPDX parses and identifies the archive; that
rebuilding the archive from the recorded commit is byte-identical; that source
provenance, source SPDX SBOM, and image attestations verify against the exact
SHA and release workflow; that
the version and `latest` image tags resolve to the candidate digests with
`amd64`+`arm64` platforms; that a fresh anonymous Docker credential dir can
pull the images (exercising native-platform layer downloads); and that the
public installer — fetched anonymously from the published tag with curl
configuration disabled and all credentials stripped, not from the local
checkout — installs into temporary directories and reports the right version.
It records full SHA-256 digests and sizes for all downloaded release assets in
the human and JSON summaries. It never touches the maintainer's real Cage
installation, configuration, Docker volumes, SSH config, or Codex state.

Manual `git push` / `git tag` / `gh release create` remain available only as
emergency recovery; the release workflow's exact-commit gate protects manual
tag pushes too.

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

### Docker storage guardrails

Cage measures free space in Docker's backing filesystem before container and
Desktop launches. Host-native Codex execution does not use Docker and bypasses
this policy. The optional top-level `[storage]` table shown above defaults to a
20 GiB warning and build floor, a 5 GiB critical floor, two retained semantic
versions per Cage image role, and a 24-hour minimum age for dangling builds.

Interactive launches below the warning floor can preview cleanup, proceed, or
abort. Critical launches and image builds must recover enough space or abort;
noninteractive critical launches and builds fail closed. When the Docker
backend cannot expose a portable capacity measurement, Cage reports that
limitation rather than inventing a free-space value.

```bash
cage storage status
cage storage clean
```

`status` lists capacity, retained versions, protected container image IDs, and
exact cleanup candidates. `clean` repeats that preview and requires typing
`CLEAN`. It removes only explicitly labeled Cage image tags outside retention
and old terminal Cage build leftovers. It never uses Docker prune, never uses
forced removal, and never deletes volumes, containers, images referenced by a
running or stopped container, unrelated images, legacy unlabeled Cage images,
or custom derived image tags. Every candidate is rechecked immediately before
removal so a newly created container makes deletion fail safe.

### Verify release provenance

Tagged releases publish the source archive, its SHA-256 checksum, and an SPDX
SBOM. GitHub also records signed provenance and SBOM attestations for the source
archive. The two GHCR images carry BuildKit SBOM and max-level provenance
metadata plus a signed GitHub provenance attestation. Their agent-neutral
shared base is published under `ghcr.io/sindycate/cage/base` with the same
metadata and signed provenance; the existing `claude-code` and `codex` image
names remain unchanged.

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
