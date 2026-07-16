# Cage Hardening Progress

This is the durable execution log for `WORKFLOW.md`. Keep entries concise and
evidence-based. Newest entries go first.

## 2026-07-16 — v0.23.2 final-release correction

The v0.23.1 CI, package, Codex image, and Claude image jobs succeeded. The final
release job downloaded the artifact but failed immediately in `gh release
create`; the job intentionally had no checkout and the command did not supply a
repository, leaving `gh` without Git context for repository discovery.

Correction:

- pass `--repo "$GITHUB_REPOSITORY"` to the checkout-free release command;
- remove the brittle Python-version condition that GitHub skipped and enforce
  the opt-in real-Docker suite in both Python 3.11 and 3.12 jobs;
- bump the next immutable complete-release attempt to `0.23.2` while preserving
  the already published versioned v0.23.1 container images.

## 2026-07-16 — v0.23.1 release-workflow correction

The v0.23.0 source commit and tag reached GitHub, but both CI and Release failed
inside `actions/setup-python@v5` before any project test. The authenticated job
view showed that pip caching searched for `requirements.txt`/`pyproject.toml`
instead of the repository's tracked `requirements-dev.txt`.

Correction:

- set `cache-dependency-path: requirements-dev.txt` in both CI and Release;
- bumped the next immutable checkpoint to `0.23.1`; v0.23.0 is not described as
  a completed release because no archive or container image was published;
- require the same full local gate, new commit/tag, and remote workflow/artifact
  verification before declaring v0.23.1 released.

## 2026-07-16 — v0.23.0 local release candidate verified

Checkpoint: boundary, state, network, bridge, configuration, installer, and
release-workflow hardening integrated

Evidence:

- the complete Python 3.12 suite passed (`113 passed, 2 skipped`); the skips are
  the explicitly opt-in Docker suite;
- the real-Docker integration suite passed separately (`2 passed`), covering
  authenticated container-to-host Netgate traffic and the nested read-only
  repository `.mcp.json` overlay;
- shell syntax, Python compilation, Compose validation, workflow YAML parsing,
  and `git diff --check` passed;
- a repository-wide high-signal credential-pattern scan found no candidate
  secrets;
- an independent adversarial diff review found and drove closure of host
  `PYTHONPATH` import injection, ambient/repository `PATH` executable selection,
  model-owned persistent symlink writes, and inaccurate generated trust text;
- Python 3.11 remains an enforced CI/release matrix target because it is not
  installed on the local workstation.

Release state:

- version `0.23.0` is assigned and the independent blocker re-review plus complete
  local release gate pass; publication is still pending commit, push, tag,
  workflow, and remote-artifact verification;
- resource/mount/concurrency controls, trust-mode implementation, session-sync
  hardening, immutable supply-chain identity, and architectural extraction remain
  subsequent packets rather than claims of this release.

## 2026-07-16 — P1-B host bridge packet verified

Checkpoint: selected host execution is authenticated, bounded, and observable

Implemented:

- generated independent 256-bit per-launch authentication tokens for MCP and
  host-command bridge protocols and authenticated before process spawn;
- replaced `shell=True` with startup-time `shlex` parsing and `shell=False`;
- ran commands from a trusted host-home cwd with a minimal base environment plus
  only explicitly selected forwarded variables;
- sanitized host `PATH`, excluded the repository and every normalized config/CLI
  read-write mount, and pinned the resolved executable at bridge startup;
- kept MCP JSON-RPC bytes unchanged after its bounded handshake and drained
  server stderr into Cage's private bridge log with a 1 MiB visible cap;
- replaced the host-command byte stream with bounded frames carrying argv,
  stdin/EOF, stdout, stderr, structured errors, and final exit status;
- added process, input, output, frame, handshake, and lifetime limits;
- tracked process groups and active connections so cleanup terminates descendants;
- bound authenticated listeners on all interfaces for native Linux host-gateway
  compatibility, with an internal loopback override used by tests.

Evidence:

- live local bridge suite passed outside the socket-restricted sandbox
  (`13 passed`), covering unauthorized clients, raw MCP bytes, argv injection,
  environment minimization, stdin behavior, stdout/stderr/status, output limits,
  timeouts, descendant cleanup, launcher token injection, PATH sanitization, and
  config/CLI read-write mount denial;
- bridge/config focused suite reported `60 passed` before final integration;
- Python and shell syntax checks passed for the packet.

Residual limitations:

- unauthenticated LAN clients can consume a bounded five-second handshake slot
  but cannot spawn a command; source-interface filtering or a Unix/vsock
  transport remains follow-up work;
- any process inside the selected Cage container can read the bridge token and
  invoke that explicitly enabled host capability. Host-integrated mode must make
  this authority prominent.

## 2026-07-16 — P1-A OAuth reconciliation packet integrated

Checkpoint: automatic OAuth rotation preserved with narrow, validated host writes

Implemented:

- removed every writable helper mount of the host Codex directory;
- validated host and volume credentials as regular, non-symlink, bounded UTF-8
  JSON objects and canonicalized them before comparison;
- replaced mtime selection with content hashes, per-identity revision/base state,
  explicit two-sided conflict detection, and per-volume/per-identity locks;
- bound volume sync state to the canonical selected host Codex directory so an
  account-directory switch resets from the new host source;
- used random exclusive mode-0600 temporaries, repeated compare-and-swap checks,
  atomic host replacement, and content CAS for volume application;
- ran helpers with no network, bounded memory/PIDs/time, dropped capabilities,
  and no host credential mount;
- propagated post-run sync errors without skipping other cleanup.

Evidence:

- adversarial OAuth suite passed (`7 passed`) for future/equal mtimes, mode
  repair, host/volume symlinks, malformed/oversized JSON, two-sided conflicts,
  identity switches, CAS races, and mount boundaries;
- obsolete launcher fake that did not execute the helper protocol was removed;
  its security assertion is superseded by the end-to-end adversarial harness.

Accepted Developer-mode residual risk:

- a malicious process already running as the Codex user can author a different
  valid credential JSON object that looks like legitimate refresh-token
  rotation. Distinguishing process provenance requires Strict-mode brokered
  credentials, not file validation.
- simultaneous live Codex processes can still race before reconciliation; CAS
  detects sync races but does not serialize provider writes during the run.

## 2026-07-16 — independent pre-release verification pass

Checkpoint: completed packets and release scaffolding challenged independently

Verifier-confirmed corrections:

- fixed CI omission of the ignored dependency lockfile and classified Python
  relay scripts under Python rather than shell syntax checks;
- changed release workflow permissions to job-level least privilege, disabled
  checkout credential persistence, validated both Python 3.11/3.12, packaged
  before image publication, and created the GitHub release only after both images
  succeed;
- fixed installer rollback/ownership/symlink bypasses, routed `make install`
  through the same staged implementation, and expanded behavioral tests from
  three to nine cases;
- made strict schema validation cover unused inline preset entries and newline
  serialization hazards;
- preserved dotfiles-managed config symlinks during atomic `set-project` writes;
- corrected Codex capability output so it does not claim Claude session
  writeback;
- added behavioral repository import-shadow coverage to the project MCP overlay
  launch test;
- corrected remaining README and canonical `AGENTS.md` trust-boundary claims.

Evidence at this checkpoint:

- full Python 3.12 suite passed (`75 passed`) before the latest bridge/OAuth
  packets began editing shared files;
- installer suite passed (`9 passed` after the shared source-install path was added);
- host-boundary suite passed (`4 passed`);
- config, host-boundary, and installer focused suites passed;
- shell syntax, Python compilation, `git diff --check`, Compose validation, YAML
  parsing, and release tarball-content simulation passed.

Evidence still required:

- Python 3.11 execution is delegated to CI because that runtime is unavailable
  locally;
- real Docker nested-bind and Netgate bridge smoke tests remain unavailable in
  the restricted local environment.

## 2026-07-16 — P0-C Netgate packet verified

Checkpoint: proxy exposure, SSRF, prompt injection, and resource usage bounded

Implemented:

- required an automatically injected fresh 256-bit per-launch proxy credential
  before DNS resolution, prompting, or upstream connection, allowing portable
  Docker host-gateway access without exposing a usable LAN proxy;
- resolved destinations once, rejected any non-public or mixed public/private
  answer, and connected to the validated numeric endpoint;
- restricted CONNECT to 443/8443;
- bounded request bodies, worker count, concurrent prompts, connection timeouts,
  and tunnel idle duration;
- streamed accepted request bodies and rejected ambiguous/chunked framing;
- removed AppleScript source interpolation and sanitized/bounded visible prompt
  values;
- stripped hop-by-hop/proxy credentials and rebuilt the upstream Host header.

Independent evidence:

- `pytest -q tests/test_netgate_proxy.py` passed (`17 passed`);
- the opt-in real-Docker integration suite passed (`2 passed`) against the
  local Docker daemon;
- `python -m py_compile netgate-proxy.py` passed.

Residual limitations:

- proxy environment variables remain deliberately bypassable by raw networking;
- any process inside the selected container can read and use its launch's proxy
  credential; this does not broaden its authority beyond that container's
  documented gated-network capability.

## 2026-07-16 — P0-B automated auth/state packet verified

Checkpoint: generated authorization state reconciled without manual-token setup

Implemented:

- kept automated host credential reuse as the default Developer-mode workflow;
- explicitly removed persistent Codex `auth.json` when copying is disabled or
  the current host source is absent;
- replaced append-only Codex MCP generation with an atomic, removable, marked
  block that is idempotent across launches;
- tracked Claude Cage-owned MCP entries in a private manifest, removed stale
  connectors/tokens on the next launch, and preserved/restored user entries
  shadowed by a managed server of the same name;
- changed sensitive generated files/directories to `0600`/`0700`;
- ran embedded entrypoint Python in isolated import mode;
- made malformed persistent preference/config state fail closed.

Independent evidence:

- focused entrypoint tests passed (`9 passed`), including repeat launch, preset
  removal, rotating/unset token, shadowed user server, stale auth, file mode, and
  isolated-import cases;
- `bash -n entrypoint.sh entrypoint-codex.sh` passed.

Accepted Developer-mode residual risk:

- active Claude connector tokens are materialized in the private per-repository
  volume for compatibility and remain at rest between launches. They are
  refreshed on launch and removed on the next launch when inactive. Strict mode
  will require a broker/no-reusable-secret design instead.

## 2026-07-16 — P0-A host-boundary packet verified

Checkpoint: confirmed escape paths removed

Implemented:

- stopped rewriting/backing up/restoring host `.mcp.json`;
- generated a private, mode-0600 project MCP overlay and nested-mounted it
  read-only into the tool container;
- rejected symlinked, non-regular, invalid, or concurrently replaced project MCP
  configuration;
- passed every path through `argv` and used Python isolated mode;
- isolated every host launcher/config/Netgate/bridge Python process from
  repository-controlled `PYTHONPATH` modules.

Independent evidence:

- `pytest -q tests/test_host_boundaries.py` passed (`4 passed`);
- `bash -n cage cage-netgate.sh` passed;
- no legacy backup, direct path interpolation, or non-isolated inline-Python
  pattern remains in the changed launcher/Netgate paths.

Residual verification:

- run a real Docker smoke test for the nested file bind on macOS and Linux before
  publishing the checkpoint; current regression tests use protocol-compatible
  fake bridge and Docker processes.

## 2026-07-16 — early safety and release scaffolding

Checkpoint: pre-integration supporting work

Completed locally:

- replaced inaccurate top-level isolation and yolo claims with an explicit
  current security model;
- documented automated credential reuse as an intentional usability feature and
  confidentiality tradeoff rather than removing it;
- added Python 3.11/3.12 CI and made release artifact/image jobs depend on the
  validation job;
- restricted Docker build context with an allowlist-style `.dockerignore`;
- hardened installer path validation, ownership recognition, checksum fallback,
  staged replacement, and rollback behavior;
- added three installer safety tests.

Evidence:

- `bash -n install.sh` passed;
- `pytest -q tests/test_install_safety.py` passed (`3 passed`);
- `docker compose config` passed.

Not yet integrated or released:

- packet diffs and the complete test suite still require review;
- release action dependencies remain tag-pinned rather than SHA-pinned;
- license selection remains a product-owner decision and is not assumed here.

## 2026-07-16 — workflow initialized

Checkpoint: baseline and packet decomposition
Source revision: `v0.22.5` (`292efb0`)
Branch: `codex/security-hardening`

Completed:

- recorded the product-owner requirement to preserve automated credential UX;
- established strict, developer, and host-integrated trust-model direction;
- split immediate remediation into host-boundary, auth-state, and Netgate
  packets with non-overlapping file ownership;
- confirmed the baseline worktree had only a pre-existing untracked
  `__pycache__/` directory;
- confirmed GitHub CLI authentication is currently invalid.

Prior review evidence retained:

- 37 tests passed under Python 3.12 on the baseline;
- shell and Python syntax checks passed;
- `docker compose config` passed;
- harmless tests confirmed host Python path injection and symlink-following
  restore behavior;
- no live Docker build or live bridge/network test was performed during review.

In progress:

- P0-A host-boundary fixes;
- P0-B generated auth/config lifecycle fixes;
- P0-C Netgate hardening.

Next integration gate:

- inspect each packet diff;
- run focused regression tests;
- independently attempt safe adversarial cases;
- update migrations and effective-security documentation;
- run the full suite before deciding the first version bump.

Known publication blocker:

- `gh auth status` reports invalid tokens for both configured accounts. Do not
  claim a push, pull request, tag, or release until separately verified.
