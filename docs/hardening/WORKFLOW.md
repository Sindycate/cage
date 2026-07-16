# Cage Hardening Workflow

Status: active
Started: 2026-07-16
Baseline: `v0.22.5` (`292efb0`)
Working branch: `codex/security-hardening`

## Objective

Remediate the security, correctness, maintainability, and product-trust findings
from the July 2026 technical review while keeping Cage practical for daily use.
The work is complete when every accepted finding is either fixed and covered by
tests, explicitly accepted as a documented limitation, or moved to a bounded
follow-up with an owner and validation plan.

This file is the source of truth for long-running work. It must remain useful
after context compaction, a new agent session, or a maintainer handoff.

## Product-owner constraints

- Automated credential setup is a core usability requirement. Do not make
  manual token management the default remediation.
- Safer credential brokers and short-lived credentials should be additive and
  automated where possible.
- Breaking changes are allowed when migration instructions are complete.
- Intermediate versions may be published. Every pushed commit must have its own
  version and tag, as required by `AGENTS.md`.
- Security claims must describe effective behavior, not intended behavior.

## Trust model decision

Cage will distinguish three operating profiles instead of presenting every
launch as the same security boundary:

1. **Strict**: intended for untrusted repositories. No raw host command bridges,
   no direct credential directories, enforced egress, bounded resources, and an
   ephemeral or review-before-export workspace.
2. **Developer**: optimized for low-friction daily work with automated
   credentials and direct repository access. Credential confidentiality and
   writable Git metadata are explicit accepted risks.
3. **Host-integrated**: enables host MCP/command bridges and external side
   effects. The launch plan must identify those capabilities prominently.

The existing behavior maps most closely to Developer or Host-integrated mode.
Mode implementation is a later milestone; current fixes must not make that
separation harder.

## Scope

Included:

- confirmed host write and code-execution primitives;
- generated auth/config lifecycle and preset isolation;
- Netgate exposure, SSRF, resource bounds, and honest product semantics;
- host MCP and command bridge isolation and protocol correctness;
- resource, mount, concurrency, and persistent-state controls;
- configuration validation, starter experience, installer safety, and doctor;
- build/release reproducibility, CI gates, documentation, and migrations;
- adversarial regression tests for every confirmed security defect.

Excluded unless needed by an included fix:

- forcing manual credentials as the normal workflow;
- claiming protection from Docker or host-kernel compromise;
- unrelated feature development;
- a full language rewrite before the immediate escape paths are closed.

## Workflow packets

| ID | Packet | State | Required evidence |
|---|---|---|---|
| P0-A | Remove `.mcp.json` host mutation/path injection and isolate host Python imports | complete | adversarial path, import-shadowing, and symlink tests |
| P0-B | Remove stale generated auth/MCP state without adding credential toil | complete | preset-switch and repeat-launch tests |
| P0-C | Harden Netgate listener, resolution, prompts, and resource usage | complete | SSRF, body-limit, prompt, and public-destination tests |
| P1-A | Harden OAuth synchronization and durable-state ownership | complete | malformed, symlink, mtime, account-switch, and race tests |
| P1-B | Harden MCP/host-command bridges and repair relay protocol | complete | auth, argv, status, timeout, cleanup, and collision tests |
| P1-C | Add resource limits, mount validation, locking, and crash recovery | pending | real-Docker integration tests |
| P2-A | Repair config schema/editor, trust handling, and starter config | complete | strict-schema and round-trip tests |
| P2-B | Add trust modes, capability manifest, dry-run, and state tooling | pending | CLI acceptance tests and migration guide |
| P2-C | Harden installer, builds, release workflow, and supply chain | in progress | CI release gate, installer safety, SBOM/provenance checks |
| P3 | Consolidate architecture and remove duplicated orchestration | pending | behavior parity and cross-platform matrix |

Packet states are `pending`, `in progress`, `verification`, `complete`, or
`deferred`. A packet is not complete merely because code was edited.

## Integration rules

- Preserve pre-existing user changes and keep packet file ownership disjoint
  where parallel work is used.
- Every security fix gets a regression test that fails on the baseline behavior.
- An implementation packet is reviewed against its diff and evidence before
  integration.
- No finding is silently dropped. Rejected or deferred findings must include the
  reason and residual risk in `PROGRESS.md`.
- Compatibility changes must update `MIGRATIONS.md` in the same release.
- A release checkpoint requires the full unit suite, shell/Python syntax checks,
  Compose validation, diff review, and any available focused integration tests.
- External publication is recorded only after commit, push, tag, and workflow
  status are verified.

## Release checkpoints

The exact versions may change after integration review.

- **Checkpoint 1 — boundary safety:** P0-A, the safe portion of P0-B, accurate
  warnings, and regression tests. Published source checkpoint: `v0.23.0`;
  release-workflow correction: `v0.23.1`.
- **Checkpoint 2 — state and network:** remaining P0-B/P0-C plus OAuth and
  concurrency protections.
- **Checkpoint 3 — controlled capabilities:** bridges, limits, mounts, trust
  modes, and launch-plan UX.
- **Checkpoint 4 — maintainability:** configuration, installer, supply chain,
  release gates, and architectural consolidation.

## Approval and publication state

- Local edits, tests, commits, pushes, tags, and intermediate releases are
  authorized by the product owner for this workflow.
- GitHub CLI authentication was invalid at workflow start. Local work may
  continue, but GitHub publication must not be reported as successful until
  authentication and remote state are verified.
- Destructive cleanup, history rewriting, force-pushes, secret changes, and
  production-environment changes are not authorized.

## Done condition

The workflow is done when:

1. all accepted packets are complete or explicitly deferred with residual risk;
2. migrations describe every breaking behavior change;
3. automated tests cover confirmed escape paths and critical state transitions;
4. documentation matches effective trust boundaries;
5. release artifacts and source revisions are auditable and reproducible enough
   for the documented support level;
6. the final residual-risk register is reviewed with the product owner.

## Compaction and resume instructions

After compaction or handoff, read these files before taking action:

1. `AGENTS.md`
2. `docs/hardening/WORKFLOW.md`
3. `docs/hardening/PROGRESS.md`
4. `docs/hardening/MIGRATIONS.md`
5. `git status -sb` and the current branch diff

Resume the first packet whose state is `in progress`, `verification`, or
`pending`. Do not repeat completed proof-of-concept work. Update `PROGRESS.md`
at every release checkpoint, material decision, repeated failure, or pause.
