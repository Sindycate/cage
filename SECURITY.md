# Security Model

Cage reduces the filesystem blast radius of AI coding tools by placing the tool
inside a Docker container and making only explicitly selected host paths
writable. It is primarily a defense against accidental or over-broad file
operations. It is not currently a complete sandbox for hostile repositories,
dependencies, models, or connectors.

## Current trust boundary

The current release is appropriate when:

- the repository and its normal development dependencies are reasonably
  trusted;
- the main risk is accidental deletion or modification elsewhere on the host;
- the user accepts that credentials made available to the tool can be read and
  used by processes inside the container;
- enabled MCP servers and host commands are trusted to run with their documented
  host authority.

The current release is not sufficient by itself when:

- repository code may be actively malicious;
- credential confidentiality from repository processes is required;
- network policy must resist deliberate bypass;
- external connector side effects must remain approval-gated independently of
  the coding tool;
- protection from a Docker daemon, runtime, or host-kernel compromise is needed.

## Credentials and usability

Cage deliberately supports automated credential reuse because repeatedly
provisioning tokens makes normal development impractical. Read-only mounts stop
the container from changing a mounted credential file, but they do not stop it
from reading, using, copying, or exfiltrating the credential.

The hardening roadmap therefore preserves the convenient workflow while adding
automated safer options:

- narrowly mounted files instead of complete configuration directories;
- separate generated configuration and durable session state;
- explicit cleanup when switching presets or identities;
- short-lived or brokered credentials where providers support them;
- a future strict mode that does not expose reusable host credentials;
- launch-time capability summaries so the risk is visible before execution.

Users should prefer least-privilege provider accounts and repository-scoped or
short-lived credentials when available. Do not place secrets in repository
configuration.

## Host launcher control plane

The host launcher validates one immutable launch plan before image inspection,
Docker volume changes, bridge startup, OAuth/session synchronization, or target
execution. Its versioned public JSON contract contains environment-variable
names and selected capability names, but never their values, MCP commands or
header values, OAuth state, proxy/bridge credentials, GitHub tokens, or raw
passthrough prompts.

`cage` executes the Python core in isolated mode. The bootstrap resolves the
installation directory rather than trusting the current directory,
`PYTHONPATH`, or user-site packages, and rejects symlink or non-regular
`cage_core` entries before import. Source and release installers apply the same
package-entry checks.

The Codex passthrough and selected-only MCP suppression rules are implemented
once as pure policy. Host, container entrypoint, and Desktop remote execution
delegate to it; target-specific adapters own filesystem inspection and process
execution. Desktop remote execution enables only one target-specific
passthrough exception: the current app's exact `features.code_mode_host=true`
override before `app-server`. Other feature values and roots, and
the same override on host or ordinary container paths, still fail closed.
OpenCode passthrough and selected-only MCP rules are likewise implemented as
pure policy. Secret values are resolved only after plan validation and at the
process-creation boundary; OpenCode MCP headers are materialized only in the
container's tmpfs-backed `/run` state, while proxy, provider, GitHub, bridge,
identity, and selected environment values cross a private launch-file handoff,
not Docker metadata.

## Network behavior

`--net off` applies Docker's `--network none` to the main tool container.

`--net gate` is currently a proxy-based approval helper. It is not enforced
against code that ignores proxy environment variables or uses raw TCP, UDP, SSH,
or DNS. An enforced-egress architecture is tracked in
`docs/hardening/WORKFLOW.md`.

The supported OpenCode image is contract-tested in real Docker for both
provider and remote-MCP HTTP traffic: the runtime must present Cage's fresh
authenticated proxy credential. `--net off` is also exercised by a real
OpenCode process. An upstream runtime that breaks these image/runtime contracts
fails CI; these checks do not expand Netgate beyond its proxy-based boundary.

## Host integrations

Selected MCP servers and host commands may execute on the host and can access
resources outside the repository. They are explicit trust extensions, not part
of the container boundary. Treat packages and command definitions used by those
integrations as host software.

The built-in profile-pinned AWS CLI capability is one such trust extension.
`aws_access = "host-cli"` creates a reserved `aws` relay that invokes the
host-installed AWS CLI with the configured `aws_profile`. The host AWS config,
SSO cache, browser, keychain integration, and credential providers remain on
the host; Cage does not mount them or forward ambient AWS credential variables
into the AWS child process. The relay blocks profile/configuration/debug
overrides, `configure`, and `sso logout`, but it is not an AWS action allowlist:
the selected profile's IAM policy remains the authority. Host AWS traffic
bypasses Netgate, and the capability is rejected with `--net off` or host-native
Codex execution. The host process still has the host user's filesystem
authority for local paths supplied to AWS CLI operations; this is not a second
filesystem sandbox.

Cage resolves each selected bridge executable once at launch, removes the
repository and explicit read-write mounts from its child `PATH`, and refuses an
executable located under those writable roots. A deliberately configured trusted
wrapper can still act on repository paths, so review the full command definition.

External systems also have a separate side-effect boundary: containing local
filesystem writes does not undo a pushed Git commit, changed ticket, sent
message, or modified cloud resource.

## Host-native execution (target = "host")

Cage supports running Codex directly on the host without a Docker container.
This is deliberately for maintenance tasks that need access outside the
repository. Host execution:

- provides **no Docker isolation boundary** — Codex runs with full host-user
  file access;
- provides **no Cage network restriction** — `--net gate` and `--net off`
  are rejected because Cage cannot enforce them without a container;
- uses the resolved `host_codex_dir` as `CODEX_HOME`;
- applies Git identity via process-scoped `GIT_CONFIG_COUNT`/`KEY`/`VALUE`
  environment variables (no host config mutation);
- applies SSH keys via a process-scoped `GIT_SSH_COMMAND` (no `~/.ssh/config`
  mutation); `ssh_host` aliases are rejected because they require writing
  `~/.ssh/config`;
- resolves GitHub tokens process-scoped via `gh auth token`;
- applies a selected native Codex profile with `--profile` after validating the
  corresponding file under the resolved `CODEX_HOME`;
- passes selected remote MCP definitions as process-local Codex configuration
  overrides without rewriting host Codex configuration;
- resolves each selected stdio MCP command to a pinned absolute executable and
  rejects executables under the writable repository. The MCP process still runs
  with full host-user authority and receives the selected environment names;
- filters selected skill packs through process-local Codex configuration only
  when their source is the default `~/.agents/skills` registry. Cage does not
  rewrite or copy the host registry in host mode;
- pins the Codex executable to an absolute path and rejects it if located
  inside the repository (the only Cage-designated writable path accepted by
  host mode);
- rejects duplicate selected MCP names already present in base, selected
  profile, or project Codex configuration rather than silently overriding
  them;
- rejects host command bridges, extra mounts, custom `host_agents_dir` and
  non-default skill-pack sources (all still require container execution).

Host execution is supported only for Codex. Claude and OpenCode host execution
are rejected.

## OpenCode container execution

OpenCode is container-only in this release. Cage snapshots host and repository
configuration through bounded no-follow reads, rejects symlinked, hard-linked,
special, oversized, or concurrently replaced inputs, then resolves and
sanitizes the snapshot with the exact OpenCode binary installed in the image.
The final process has live project-config loading disabled and must report an
effective MCP inventory matching the selected Cage pack exactly. Configuration-
defined external instruction and skill paths/URLs fail closed. Root project
instructions and discovered project skills come from the snapshot; nested
instruction files remain ordinary readable repository content, because Cage
does not hide or rewrite the repository mount.

OpenCode's TUI extracts a native renderer library at startup. Its container
therefore receives a dedicated ephemeral `/tmp` tmpfs with `exec` enabled,
while retaining `nosuid` and `nodev`; Claude and Codex keep Cage's existing
non-executable default tmpfs. The executable temporary state disappears with
the container and is not stored in the per-repository volume.

External OpenCode plugins are disabled with `--pure` by default. A preset with
`opencode_plugins = true` explicitly permits snapshot-copied global/project
plugin code and is therefore a larger code-execution trust boundary. Plugin
network and external side effects are not constrained by the native selected-
MCP allowlist.

OpenCode proxy credentials, provider environment values, GitHub tokens, bridge
handshakes, and selected identity/environment values are loaded from a private
mode-0600 launch file only after the container starts. They are available to
the final tool process as needed but do not appear in Docker `Config.Env`.

Provider `auth.json` is synchronized exactly only when `copy_auth` is enabled.
MCP OAuth state is imported and merged back only for selected server names.
Both use bounded duplicate-key-rejecting JSON parsing, regular-file and
hardlink checks, private modes, per-identity locks, and conflict detection.
OpenCode sessions, history, indexes, logs, cache, and other runtime state stay
inside the per-repository volume. Only provider auth and selected MCP OAuth
entries can be written back to the chosen host OpenCode data directory.

Provider and MCP browser authentication temporarily publishes only the known
loopback callback ports on host `127.0.0.1`, relayed to the container's loopback
listener. Ordinary OpenCode launches publish no ports. `serve`, `web`,
`attach`, and `pr` are not managed targets.

## ChatGPT Desktop execution (target = "desktop")

The macOS-only Desktop target keeps ChatGPT's UI on the host while its Codex
app-server and repository tools run in a persistent Cage container through the
official SSH-host workflow.

- The transport has no TCP listener or published app-server port. OpenSSH uses
  a concrete managed alias whose `ProxyCommand` runs the absolute installed
  Cage helper; that helper validates the target label and invokes `sshd -i`
  through `docker exec`.
- Each canonical repository plus preset receives a separate Codex volume,
  client key, persistent container host key, known-hosts file, alias, log, and
  private Unix control socket.
- Password, keyboard-interactive and root login, forwarding, tunnels, public
  listeners, user environment files, and user rc are disabled. The container
  host key is pinned and unexpected client or host key replacement fails
  closed.
- Desktop containers add `SYS_CHROOT` solely for OpenSSH's per-connection
  privilege-separation sandbox. Other Cage targets retain the existing
  capability set, and `CAP_FOWNER` remains disabled.
- Provider values, authenticated proxy URLs, and bridge credentials are
  removed from Docker `Config.Env`, transferred through a short-lived private
  host bind, and written only to a user-readable tmpfs-backed `/run` file.
  The handoff is unlinked after readiness; PID 1 is launched with a scrubbed
  environment. SSH configuration, Docker metadata, Cage target metadata,
  supervisor logs, and the Desktop volume contain no such secret values.
- The supervisor owns the ordinary Cage child process, Netgate, selected
  bridges, OAuth reconciliation, heartbeat, and cleanup. Loss of the
  supervisor or a required bridge stops the target; stale labeled containers
  are recoverable without deleting the volume.
- `stop` preserves keys, alias, volume, and history. `remove` requires explicit
  confirmation before deleting them.

This preserves Cage's existing container threat model; it does not turn the
host ChatGPT application itself into a containerized process. The repository,
explicit read-write mounts, selected credentials, MCP/host-command bridges,
and external connector actions retain their documented authority.

## Writable repository state

The repository mount includes `.git`, ignored files, and untracked files. A tool
can modify Git hooks and configuration, corrupt repository metadata, or delete
data that `git checkout` cannot recover. Keep independent backups and inspect
repository metadata after running untrusted or unusually broad tasks.

## Release integrity and provenance

Tagged source releases include a SHA-256 checksum and SPDX SBOM. The release
workflow creates signed GitHub provenance and SBOM attestations for the source
archive. Published container images, including the agent-neutral shared base,
include BuildKit SBOM and max-level provenance metadata and a signed GitHub
provenance attestation. The base contains shared operating-system tooling and
bridge relays only; agent users, entrypoints, agent binaries, and Codex-only
OpenSSH remain in the Claude, Codex, and OpenCode leaf images. Workflow action dependencies are pinned
to immutable commits and tracked by Dependabot.

These records make the source revision, build workflow, and detected components
auditable. They do not establish that the source, dependencies, container image,
or resulting runtime behavior is safe. Consumers must verify attestations and
apply their own vulnerability and trust policy.

Container builds intentionally resolve current coding-tool and operating-system
packages so `cage update` and rebuild workflows can refresh them. The published
SBOM and provenance identify what a release build actually contained, but the
container images are not claimed to be bit-for-bit reproducible from mutable
upstream package repositories. Consumers that require immutable deployment
identity should retain and use the verified image digest rather than relying on
a registry tag alone.

### SHA candidates, exact-digest promotion, and attestation identities

A successful `ci.yml` run on `main` publishes immutable
`candidate-<full-commit-sha>` images for `base`, `claude-code`, `codex`, and
`opencode`.
Candidate tags are **public and write-once**: anyone can inspect them, a
conflicting or unverifiable candidate fails closed and is never overwritten,
and they are not stable releases — Cage's image-pull logic never references
them. They share content-addressed blobs with the eventual version tags and
serve as audit and resume evidence. On a CI rerun for the same SHA, an existing
candidate is verified (amd64/arm64 platforms and a `ci.yml` provenance
attestation tied to the exact source SHA and `refs/heads/main`) and reused; its
build and attest steps are skipped. A candidate that exists but cannot be
verified fails closed rather than being rebuilt, so freshly resolved mutable
dependencies can never replace an immutable candidate.

The tag-triggered release workflow **promotes** the exact verified candidate
digests to the version and `latest` tags with `docker buildx imagetools
create`; it never rebuilds images, never uses QEMU, and never resolves mutable
package sources. Version tags are immutable (an existing version tag with a
different digest fails closed), and `latest` moves only after all four version
tags and their attestations succeed. Promotion is idempotent so a workflow
rerun can finish after a partial registry interruption.

Attestation identities are pinned. Candidate images carry a signed GitHub
provenance attestation from the pinned `ci.yml` signer with source ref
`refs/heads/main` and source digest equal to the commit SHA. The release
workflow re-attests each promoted digest from the pinned `release.yml` signer.
The release workflow's exact-commit gate verifies the candidate digests,
amd64/arm64 platforms, and CI attestations (expected repository, exact source
digest, `refs/heads/main`, pinned `ci.yml` signer workflow) before any release
work, using the `oci://` image reference form that `gh attestation verify`
requires, and it enforces this for **manual tag pushes too** — the maintainer
command `scripts/publish_release.py` is convenience, not a security boundary.
There is intentionally no cross-version BuildKit cache, so a warm cache cannot
silently reuse stale dependencies.

## Reporting a vulnerability

Please use the repository's GitHub private vulnerability-reporting or security
advisory flow. Do not publish exploit details in a public issue before a fix is
available. Include the Cage version, host platform, Docker runtime, relevant
preset shape with secrets removed, reproduction steps, and observed impact.

## Hardening status

The active remediation plan, validation evidence, and migration notes live in:

- `docs/hardening/WORKFLOW.md`
- `docs/hardening/PROGRESS.md`
- `docs/hardening/MIGRATIONS.md`
