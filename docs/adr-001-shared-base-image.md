# ADR-001: Shared Base Image for Claude and Codex

Status: accepted
Date: 2026-07-28
Issue: [#3](https://github.com/Sindycate/cage/issues/3)

## Context

Before v0.26.2, Cage published two final images from independent Dockerfiles:

| Image | Dockerfile | Registry path |
|-------|-----------|---------------|
| `claude-code:<version>` | `Dockerfile` | `ghcr.io/sindycate/cage/claude-code` |
| `codex:<version>` | `Dockerfile.codex` | `ghcr.io/sindycate/cage/codex` |

Both images shared approximately 80% of their build instructions: the same
Ubuntu 24.04 base, system packages, Node.js LTS, GitHub CLI, OCI labels, and
the `mcp-relay`/`host-cmd-relay` bridge scripts. They differ in agent
installation, user/home layout, entrypoint, and Codex-only OpenSSH support.

Despite the overlap, only the Ubuntu rootfs layer was shared. The two
Dockerfiles' `RUN` instructions diverged at the first `apt-get` call (Codex
used `--no-install-recommends` and retry logic; Claude did not). Docker layer
identity is content-addressed through the parent chain, so later layers
containing the same packages still produced different digests.

## Baseline estimates from Dockerfile analysis

Sizes are estimated from known package archives and verified against Ubuntu
24.04 package indexes. Build timings were not measured at decision time; later
release observations are recorded below.

These estimates motivated the decision but are not a substitute for recorded
multi-architecture registry measurements and release evidence below.

### Layer breakdown (compressed, amd64 estimates)

| Layer | Claude | Codex | Shared? |
|-------|--------|-------|---------|
| `ubuntu:24.04` base | ~29 MiB | ~29 MiB | Same digest (both `FROM ubuntu:24.04`) |
| System packages (apt) | ~85 MiB | ~80 MiB | **No** — different flags/retry |
| Node.js LTS | ~28 MiB | ~28 MiB | **No** — different parent |
| GitHub CLI | ~13 MiB | ~13 MiB | **No** — different parent |
| Labels + COPY relays | ~1 MiB | ~1 MiB | **No** — different parent |
| User creation + agent install | ~110 MiB | ~55 MiB | No (different agents) |
| openssh-server | — | ~4 MiB | Codex-only |
| **Total (compressed)** | **~266 MiB** | **~210 MiB** | |

### Duplicated content

The Ubuntu rootfs layer (~29 MiB) is already shared via the registry because
both images use `FROM ubuntu:24.04`. The system packages, Node.js, and GitHub
CLI layers contain identical binaries but produce different digests due to
instruction divergence. This means:

- **Layer references**: ~122 MiB of near-identical compressed content per
  arch produced divergent digests due to instruction differences, preventing
    digest-level reuse across the two images (amd64 + arm64 ≈ 244 MiB of
    logically duplicated content that could not share layer references).
- **CI build time**: Both matrix jobs independently run `apt-get`, NodeSource
  setup, and GitHub CLI installation. On a cold cache this adds ~3–5 minutes
  per image (arm64 under QEMU is significantly slower).
- **Local builds**: Users who build both images locally pay the full apt/Node
  cost twice.

### Historical build-time estimates (not measurements)

| Phase | Claude | Codex |
|-------|--------|-------|
| apt-get system packages | ~90s | ~120s (retries add overhead) |
| Node.js LTS | ~30s | ~30s |
| GitHub CLI | ~20s | ~20s |
| Agent install | ~40s | ~25s |
| **Total (amd64)** | **~3 min** | **~3.5 min** |
| **Total (arm64/QEMU)** | **~12 min** | **~14 min** |

These estimates established a hypothesis that moving common work into one base
would reduce both aggregate builder work and pipeline wall time. They were not
measurements. The release evidence below confirms shorter leaf builds and lower
aggregate work, but does **not** confirm a cold-pipeline wall-time reduction.


## Published registry measurements (v0.26.2)

Measured 2026-07-28 against `ghcr.io/sindycate/cage/{base,claude-code,codex}:0.26.2`
using the OCI distribution API (anonymous pull token). All sizes are compressed.

### Image sizes

| Image | amd64 | arm64 | Layers |
|-------|-------|-------|--------|
| `base:0.26.2` | 150.9 MiB | 148.5 MiB | 7 |
| `claude-code:0.26.2` | 316.5 MiB | 313.2 MiB | 14 (7 base + 7 leaf) |
| `codex:0.26.2` | 545.4 MiB | 520.8 MiB | 15 (7 base + 8 leaf) |

Each image also carries two `unknown/unknown` attestation manifests
(SBOM + provenance) of ~8 MiB each.

### Layer sharing verification

All 7 base layers are **digest-identical** across all three images on both
architectures. Confirmed by comparing `layer.digest` values from the OCI
manifests:

| Layer | Content | amd64 size | arm64 size | Shared |
|-------|---------|-----------|-----------|--------|
| 0 | `ubuntu:24.04` rootfs | 28.4 MiB | 27.6 MiB | ✓ all three |
| 1 | System packages (apt) | 50.3 MiB | 50.1 MiB | ✓ all three |
| 2 | GitHub CLI | 14.1 MiB | 12.8 MiB | ✓ all three |
| 3 | Node.js LTS | 58.1 MiB | 58.1 MiB | ✓ all three |
| 4–6 | Labels, COPY relays, chmod | ~3 KB | ~3 KB | ✓ all three |

**Layer reference identity**: all three image manifests reference the same
seven base-layer digests (150.9 MiB amd64 / 148.5 MiB arm64). Before the
shared base (v0.26.1), the two leaf images already shared the Ubuntu rootfs
layer (~29 MiB per arch) because both used `FROM ubuntu:24.04`, but the
apt/Node.js/GitHub CLI layers diverged due to different instruction flags,
limiting deduplication to that single base layer. The shared base extends
identity to all seven infrastructure layers.

Note: matching digests prove the manifests reference identical content. They
do not prove that GHCR physically stores those blobs once across the three
separate repository paths (`base`, `claude-code`, `codex`). Cross-repository
blob mounting is optional in the [OCI Distribution
Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md),
and backend storage implementation is not specified. The practical benefit is
that clients pulling multiple images transfer shared layers only once.

### Observed release build timings

The following measurements come from successful GitHub Actions release runs:

- [v0.26.1](https://github.com/Sindycate/cage/actions/runs/30346339300),
  the last independent-Dockerfile release;
- [v0.26.2](https://github.com/Sindycate/cage/actions/runs/30358209182), the
  first shared-base release; and
- [v0.26.3](https://github.com/Sindycate/cage/actions/runs/30363803333), a
  second shared-base release with unchanged Dockerfiles and copied-file inputs.

Each image time is the interval from the start of its
`docker/build-push-action` step to the start of the following provenance
attestation step. Aggregate work is the sum of those image times. Pipeline span
is the interval from the first image-build step to the last image-build step;
the independent leaves run in parallel, while the shared leaves wait for the
base job.

| Release | Design | Base | Claude | Codex | Aggregate work | Pipeline span |
|---------|--------|------|--------|-------|----------------|---------------|
| v0.26.1 | independent leaves | — | 5:15 | 7:16 | 12:31 | 7:16 |
| v0.26.2 | shared base | 6:33 | 1:33 | 2:40 | 10:46 (−14%) | 9:46 (+34%) |
| v0.26.3 | shared base | 4:25 | 1:36 | 2:42 | 8:43 (−30%) | 7:39 (+5%) |

Compared with v0.26.1, the shared-base leaf steps were 63–70% shorter and
aggregate builder work was 14–30% lower. The new serial base prerequisite
offset those gains in both observed cold release pipelines, however, so the
original estimate of a 40% cold wall-clock reduction was not supported.

These are observed release timings, not a controlled performance benchmark:
the jobs ran on separate ephemeral `ubuntu-24.04` hosted runners and include
network and registry variance. The workflow configures neither `cache-from`
nor `cache-to`, and the inspected build logs contained no cached build steps.
It therefore has no cross-run warm-cache timing to measure. The fast shared
leaf jobs demonstrate the operational base-reuse case—build the base once,
then build both agent-specific leaves—but must not be labeled a fully warm
rebuild.

### Leaf-only layers

**Claude leaf** (7 layers, ~165.6 MiB amd64):
- User creation, entrypoint COPY, sudoers: ~12 KB
- Claude Code installer (`curl | bash`): 83.0 MiB
- `chmod -R a+rwX /home/claude`: 82.6 MiB

**Codex leaf** (8 layers, ~394.5 MiB amd64):
- `openssh-server` install: 3.0 MiB
- User creation, npm prefix setup: ~11 KB
- `npm install -g @openai/codex`: 261.0 MiB
- `codex-remote.py` COPY, entrypoint COPY: ~2 KB
- `chmod -R a+rwX /home/codex`: 130.5 MiB

### Observations

1. **Estimates vs. reality**: the Codex image is 2.6× larger than estimated
   (545.4 MiB vs. ~210 MiB). The npm global install layer (261.0 MiB) and the
   `chmod -R` layer (130.5 MiB) are the main contributors. The chmod layer
   duplicates file metadata for the entire npm prefix. Claude's chmod layer
   (82.6 MiB) shows the same pattern at smaller scale.

2. **chmod layers are a future optimization target**: both leaf images carry
   a full-filesystem-copy chmod layer (82.6 + 130.5 = 213.1 MiB amd64).
   These sizes establish an upper bound on potential savings, not a
   guarantee: `COPY --chmod` alone does not repair permissions of files
   created by npm or the Claude installer. A prototype combining
   installation and permission adjustment into a single layer would be
   needed to realize the savings. This is out of scope for the base-image
   decision but worth a follow-up.

3. **Build timings**: successful release logs provide comparable image-step,
   aggregate-work, and pipeline-span observations. They confirm substantially
   shorter leaf builds and lower aggregate work, but no cold pipeline
   wall-clock improvement in the two shared-base samples. A true cross-run
   warm-cache measurement is unavailable because the release workflow does
   not persist BuildKit cache. This residual performance uncertainty is
   accepted: it does not affect the verified security boundary, compatibility,
   image reuse, or release correctness. If persistent CI caching is added, its
   design must include a new cold/warm benchmark and must not reuse these
   release observations as warm-cache evidence.

## Options evaluated

### Option 1: Keep two independent Dockerfiles (status quo)

**Advantages:**
- Simplest to understand; no new build dependencies.
- Each image can evolve independently without coordination.
- No risk of accidental coupling between agent runtimes.

**Disadvantages:**
- ~122 MiB of near-identical apt/Node/GitHub CLI layers duplicated per
  arch (only the ~29 MiB Ubuntu rootfs was already shared in v0.26.1).
- CI builds both images from scratch on every release; no cache sharing.
- Security patches to shared packages require updating two Dockerfiles.
- Drift risk: the Dockerfiles have already diverged in apt flags and retry
  logic, making it unclear which differences are intentional.
- `cage update` overlay builds already assume a specific base; divergence
  makes the overlay Dockerfiles fragile.

**Security:** No change. Each image contains only its own agent and
credentials surface.

### Option 2: Shared pinned base image with thin leaf images (recommended)

Introduce a `Dockerfile.base` that produces `cage-base:<version>`, containing:
- Ubuntu 24.04
- Shared system packages (unified flags: `--no-install-recommends` + retry)
- Node.js LTS
- GitHub CLI
- `mcp-relay` and `host-cmd-relay`
- OCI labels

Then `Dockerfile` and `Dockerfile.codex` become thin leaves:

```dockerfile
# Dockerfile (Claude leaf)
FROM cage-base:${CAGE_VERSION}
RUN useradd -m -s /bin/bash claude && ...
COPY entrypoint.sh /home/claude/entrypoint.sh
USER claude
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
RUN chmod -R a+rwX /home/claude
ENTRYPOINT ["/home/claude/entrypoint.sh"]
```

```dockerfile
# Dockerfile.codex (Codex leaf)
FROM cage-base:${CAGE_VERSION}
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && ...
RUN useradd -m -s /bin/bash codex && ...
COPY entrypoint-codex.sh /home/codex/entrypoint.sh
COPY codex-remote.py /usr/local/bin/codex
ENV NPM_CONFIG_PREFIX=/home/codex/.npm-global
RUN npm install -g @openai/codex && ...
ENTRYPOINT ["/home/codex/entrypoint.sh"]
```

**Advantages:**
- Makes ~122 MiB/arch of previously divergent infrastructure content
  digest-identical across the leaf manifests.
- CI builds the base once; leaf images build in parallel in seconds.
- Security patches to shared packages happen in one place.
- `cage update` overlays remain unchanged (they build `FROM` the leaf image).
- Pull-before-build behavior unchanged for end users (they pull leaf images).
- The base image can be published to the registry for cache reuse, or kept
  as a local-only intermediate.

**Disadvantages:**
- Adds one more image to the release matrix (base + 2 leaves = 3 builds).
- Requires versioning discipline: base and leaves must share `CAGE_VERSION`.
- Slightly more complex `docker-compose.yml` and CI workflow.
- If the base image is published, it needs its own SBOM/provenance.

**Security:**
- The base image contains no agent binaries, no credentials, and no
  entrypoints. It is a inert system-packages layer.
- `openssh-server` remains Codex-only (installed in the leaf).
- Claude's image does not gain any Codex-specific surface, and vice versa.
- The base image does not need to be pullable by end users; it can be a
  CI-only intermediate that is never published, or published as
  `ghcr.io/sindycate/cage/base` for transparency.

**Backward compatibility:**
- `claude-code:<version>` and `codex:<version>` image names, tags, and
  registry paths are unchanged.
- `docker pull ghcr.io/sindycate/cage/claude-code:0.26.0` still works.
- `cage update claude` / `cage update codex` overlay builds are unaffected
  (they build `FROM claude-code:<version>` / `FROM codex:<version>`).
- Local `docker compose build` still produces both images.

### Option 3: Single combined image with runtime agent selection

One image containing both Claude Code and Codex CLI, with the entrypoint
selecting the agent based on an environment variable or argument.

**Advantages:**
- Single image to build, publish, pull, and update.
- Maximum layer sharing.
- Simplest release matrix (one image).

**Disadvantages:**
- **Security**: Every Claude session gains `openssh-server`, `codex-remote.py`,
  and the Codex npm prefix. Every Codex session gains the Claude Code binary
  and its installer artifacts. This violates the principle of minimal surface.
- **Size**: Every pull downloads both agents (~370 MiB compressed) even when
  only one is needed. Users who only use Codex pay for Claude Code and vice
  versa.
- **Update cadence**: Claude Code and Codex CLI release on different schedules.
  A combined image forces synchronized releases or leaves one agent stale.
  `cage update claude` would need to rebuild the entire combined image.
- **State model**: The two agents have fundamentally different home directories,
  volume layouts, entrypoint logic (318 vs 756 lines), and credential handling.
  Combining them in one image creates a complex conditional entrypoint that is
  harder to audit and test.
- **Desktop SSH**: The ChatGPT Desktop target requires `sshd`, `SYS_CHROOT`,
  and a specific supervisor/heartbeat model. Putting this in a shared image
  means Claude containers carry dead SSH infrastructure.
- **Backward compatibility**: Breaks the `claude-code:<version>` and
  `codex:<version>` pull contracts unless alias tags are maintained, adding
  confusion about what each tag actually contains.

**Security:** Strictly worse than Options 1 and 2. The attack surface of each
runtime expands to include the other agent's binaries and dependencies.

## Recommendation

**Option 2: Shared pinned base image with thin leaf images.**

This option captures the maintenance and efficiency benefits of consolidation
without the security, size, and compatibility costs of a combined image. It is
the standard multi-stage pattern used by most projects with shared
infrastructure (e.g., `python:3.12-slim` as a base for multiple service
images).

## Local implementation validation

The accepted implementation was validated on native arm64 macOS:

- `docker compose build` completed for `cage-base:latest`,
  `claude-code:latest`, and `codex:latest`;
- Docker reported the exact same first six root-filesystem layer digests for
  the base and both leaf images;
- disposable-container inspection confirmed that the base contains neither
  agent user, agent binary, entrypoint, nor OpenSSH, while each leaf contains
  only its expected agent surface and Codex alone contains OpenSSH;
- all seven opt-in real-Docker smoke tests passed against the Codex leaf,
  including the Desktop SSH/app-server boundary;
- the complete Python 3.11 and 3.12 suites each passed with 287 tests and seven
  intentional skips, and the release archive was byte-identical across two
  fixed-epoch builds with `Dockerfile.base` present.

The release workflow still supplies the required amd64/arm64 build,
SBOM/provenance, and registry gates. Those publication results must be
confirmed before the corresponding acceptance item is closed.

### Implementation plan

1. **Create `Dockerfile.base`** with unified system packages, Node.js, GitHub
   CLI, relay scripts, and labels. Use `--no-install-recommends` and retry
   logic (adopting the Codex Dockerfile's more robust approach).

2. **Refactor `Dockerfile` and `Dockerfile.codex`** to `FROM cage-base:${CAGE_VERSION}`.
   Each leaf adds only its agent-specific user, packages, installation, and
   entrypoint.

3. **Update `docker-compose.yml`** to build the base first:
   ```yaml
   services:
     base:
       build:
         context: .
         dockerfile: Dockerfile.base
       image: cage-base:latest
     claude:
       build: .
       image: claude-code:latest
       depends_on: [base]
     codex:
       build:
         context: .
         dockerfile: Dockerfile.codex
       image: codex:latest
       depends_on: [base]
   ```

4. **Update the release workflow** to build the base image first (single-arch
   is fine for the base if leaves are multi-arch with `FROM` referencing a
   local tag; alternatively, publish the base as multi-arch too). The matrix
   then builds leaves in parallel.

5. **Update `cage` script** local-build fallback to build the base first when
   neither leaf image exists locally.

6. **Add tests**: verify that the base image contains no agent binaries, no
   entrypoints, and no `openssh-server`. Verify that leaf images contain
   exactly their expected agent.

7. **Update documentation**: AGENTS.md architecture section, SECURITY.md
   (note that the base is inert), and this ADR.

### What does NOT change

- Image names and tags: `claude-code:<version>`, `codex:<version>`
- Registry paths: `ghcr.io/sindycate/cage/claude-code`, `ghcr.io/sindycate/cage/codex`
- `cage update` overlay mechanism
- Entrypoint logic and state models
- Desktop SSH support (Codex-only)
- Pull-before-build behavior for end users
- SBOM/provenance attestations (each published leaf gets its own)
- `--rebuild` behavior

### Open questions

1. **Should the base image be published to ghcr.io?** Publishing provides
   transparency and allows users to inspect shared layers. Not publishing
   keeps the registry surface smaller. Recommendation: publish it as
   `ghcr.io/sindycate/cage/base:<version>` with SBOM, but do not document it
   as a user-facing artifact.

2. **Should the base be multi-arch?** If leaves are built with BuildKit
   multi-platform, the `FROM` reference resolves per-platform automatically
   when the base is also multi-arch. If the base is local-only, each platform
   build produces its own base layer. Recommendation: build and publish the
   base as multi-arch to enable CI cache sharing.

3. **Version coupling:** The base and leaves share `CAGE_VERSION`. A base-only
   change (e.g., security patch to a system package) requires a version bump
   and full release. This is acceptable given the existing "every pushed commit
   gets its own version" policy.

## Acceptance criteria status

- [x] Capture multi-architecture registry image sizes, layer overlap, and
      applicable build/cache evidence. *(Registry sizes and exact layer
      identity were measured from published v0.26.2 images. Step-level timings
      from v0.26.1–v0.26.3 quantify observed fresh-run release builds and base
      reuse. The current workflow has no persistent cache, so a cross-run
      warm-cache timing is explicitly accepted as not applicable rather than
      inferred from workflow duration.)*
- [x] Document why agent-specific users, entrypoints, state rules, and update
      paths can or cannot share a base safely.
- [x] Compare all three options above, including security and operational
      trade-offs.
- [x] Preserve existing `claude-code:<version>` and `codex:<version>` pull
      contracts unless a separately documented migration is approved.
- [x] Preserve independent tool updates and Codex Desktop SSH support.
- [x] Validate the chosen design with amd64/arm64 builds, reproducibility,
      SBOM/provenance, installer, and real-Docker tests. *(v0.26.2 release
      workflow succeeded: amd64/arm64 base and leaf builds, Docker smoke
      tests, installer tests, SBOMs, and provenance jobs all passed.
      Published archive verified to contain all three Dockerfiles.)*
- [x] Add the final decision to the architecture/security documentation.
