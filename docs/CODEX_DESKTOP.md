# Codex CLI profiles and ChatGPT desktop

Status: July 27, 2026.

## Supported now

Cage presets can select a native Codex profile with:

```toml
[presets.codex-provider]
tool = "codex"
auth = "codex-work"
codex_profile = "provider-preview"
```

The selected auth block determines `CODEX_HOME`; Cage then validates
`$CODEX_HOME/provider-preview.config.toml` and passes
`--profile provider-preview` to Codex. This works for both container and
host-native CLI targets.

Native Codex profile files can select a model, `model_provider`, reasoning
level, approval policy, and `approvals_reviewer`. Cage forwards the profile
selection but does not implement provider protocols or promise that every
provider supports every Codex feature. In particular, automatic review uses
additional model calls and should be tested against the selected provider.

In host mode, selected MCP packs are passed as process-local Codex
configuration. Stdio server executables are pinned outside the writable
repository and then run with host-user authority. Selected skill packs from
the default `~/.agents/skills` registry become a process-local filter. Cage
does not rewrite `config.toml` or the skill registry.

## Desktop via a Cage container

ChatGPT Desktop documents connecting to an SSH host and running remote Codex
app-server there. Cage's `desktop` target uses that supported route:

```bash
# Install this Cage source version first, then register one managed SSH Include.
./install.sh --from-source
cage desktop setup

# Start or reuse the target and open ChatGPT.
cage --preset codex-provider --desktop ~/path/to/repo

# Inspect or stop it without deleting history.
cage desktop status --preset codex-provider ~/path/to/repo
cage desktop logs --preset codex-provider ~/path/to/repo
cage desktop stop --preset codex-provider ~/path/to/repo

# Destructive: typed confirmation deletes the alias, keys, volume, and history.
cage desktop remove --preset codex-provider ~/path/to/repo
```

The first connection still requires opening **Settings → Connections** in
ChatGPT, selecting the displayed `cage-...` host, and choosing the displayed
repository path. Later Cage launches reuse the same host and volume. Use
`--no-open` when ChatGPT is already open.

Each canonical repository path plus saved preset name gets its own:

- persistent Codex Docker volume and volume-owned history;
- detached supervisor, private Unix control socket, heartbeat, and private log;
- Ed25519 client key, container host key, concrete SSH alias, and pinned
  known-hosts file.

The SSH block points at the absolute installed Cage helper. `ProxyCommand`
executes one `sshd -i` connection through `docker exec`; Cage does not publish
app-server or bind a TCP listener. Passwords, root login, agent/TCP/X11
forwarding, tunnels, user environment files, and user rc are disabled.

The remote `codex` wrapper reads selected provider, authenticated proxy, and
bridge values from a private tmpfs-backed `/run` file, then prepends the
preset's native `--profile` and yolo setting to ChatGPT's `codex app-server`
command. Cage removes those values from Docker `Config.Env`, unlinks the
short-lived host handoff after readiness, and scrubs the watchdog environment.
It does not persist those secret values in SSH configuration, Docker or target
metadata, logs, or the Desktop volume.

The desktop app itself remains a host process. Its remote repository tools and
app-server run inside Cage; its application UI and local application data do
not. Selected read-write mounts, credentials, MCP servers, host commands, and
external connector actions retain the authority documented in `SECURITY.md`.

## Official references

- [Codex configuration profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles)
- [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
- [Amazon Bedrock provider](https://learn.chatgpt.com/docs/amazon-bedrock)
- [Automatic approval review](https://learn.chatgpt.com/docs/sandboxing/auto-review)
- [`codex app` and app-server commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-app)
- [Connect ChatGPT to an SSH host](https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host)
