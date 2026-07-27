# Codex CLI profiles and ChatGPT desktop

Status: July 24, 2026.

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

## Desktop boundary

Codex provides the stable `codex app PATH` command to open a workspace in the
ChatGPT desktop app. The desktop app, CLI, and IDE share Codex configuration,
and the desktop supports MCP, skills, custom providers, and automatic review.

Named configuration profile selection is documented through the CLI
`--profile` option. There is no documented desktop launch option that binds a
new app instance to a named profile or a separate process-local `CODEX_HOME`.
An already-running macOS app also cannot be assumed to inherit an environment
from a launcher process. Cage therefore does not advertise multiple isolated
desktop identities.

The desktop app is a host process. Opening a Cage-managed repository in it does
not put the desktop app, its terminal, or its tools inside the Cage Docker
boundary.

## Future container-backed desktop route

A real desktop-UI/container-execution target should use a supported Codex
remote connection rather than environment-variable tricks:

1. Start a Codex app-server/remote-control endpoint inside a Cage container.
2. Authenticate the transport and expose it only through a narrowly scoped
   local or SSH connection.
3. Connect the desktop UI to that registered remote host.
4. Preserve Cage's per-project volume, mount, identity, MCP, skill, network,
   and cleanup invariants around the server lifecycle.

That design has a materially larger trust and lifecycle surface than the
current host CLI toggle. It remains a separate milestone rather than being
silently approximated by `codex app`.

## Official references

- [Codex configuration profiles](https://learn.chatgpt.com/docs/config-file/config-advanced#profiles)
- [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
- [Amazon Bedrock provider](https://learn.chatgpt.com/docs/amazon-bedrock)
- [Automatic approval review](https://learn.chatgpt.com/docs/sandboxing/auto-review)
- [`codex app` and app-server commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-app)
