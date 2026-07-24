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

## Network behavior

`--net off` applies Docker's `--network none` to the main tool container.

`--net gate` is currently a proxy-based approval helper. It is not enforced
against code that ignores proxy environment variables or uses raw TCP, UDP, SSH,
or DNS. An enforced-egress architecture is tracked in
`docs/hardening/WORKFLOW.md`.

## Host integrations

Selected MCP servers and host commands may execute on the host and can access
resources outside the repository. They are explicit trust extensions, not part
of the container boundary. Treat packages and command definitions used by those
integrations as host software.

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
- pins the Codex executable to an absolute path and rejects it if located
  inside the repository (the only Cage-designated writable path accepted by
  host mode);
- rejects MCP packs, skill packs, host command bridges, extra mounts, and
  custom `host_agents_dir` (all require container execution).

Host execution is supported only for Codex. Claude host execution is rejected.

This implements host-native Codex CLI only. ChatGPT desktop integration and
SSH-connected container backends are future milestones, not part of the
current release.

## Writable repository state

The repository mount includes `.git`, ignored files, and untracked files. A tool
can modify Git hooks and configuration, corrupt repository metadata, or delete
data that `git checkout` cannot recover. Keep independent backups and inspect
repository metadata after running untrusted or unusually broad tasks.

## Release integrity and provenance

Tagged source releases include a SHA-256 checksum and SPDX SBOM. The release
workflow creates signed GitHub provenance and SBOM attestations for the source
archive. Published container images include BuildKit SBOM and max-level
provenance metadata and a signed GitHub provenance attestation. Workflow action
dependencies are pinned to immutable commits and tracked by Dependabot.

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
