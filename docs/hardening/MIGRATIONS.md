# Hardening Migration Guide

This file records user-visible and configuration migrations introduced by the
hardening workflow. Entries move from **Unreleased** to a concrete version only
when that version is committed and tagged.

## Unreleased

No migrations recorded yet.

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
