# ADR-003: Explicit monitor and configuration component boundaries

Status: accepted
Date: 2026-09-22

## Context

Token Monitor grew into a 6,472-line module containing accounting, credential
reconciliation, Docker collection, persistence, HTTP, upload recovery,
migrations and scheduling. Configuration grew to 3,410 lines, including a
485-line resolver. Shared module globals obscured ownership and made tests rely
on patching unrelated operations through compatibility frontends.

One concrete failure crossed those boundaries: background scans suppressed
interactive warnings assuming errors were persisted, but peer-cache validation
ran outside the aggregation error handler. Uploads could stop without a cause
in status. Aggregation also reloaded pricing while building different periods
and providers, allowing a concurrent price edit to produce an inconsistent
result.

## Decision

Keep `cage_core.monitor` and `cage_core.config` as explicit compatibility
facades. Their implementation packages have acyclic dependencies and never
import their facade. Re-exports preserve callable entry points; no dynamic
attribute forwarding, source execution or mutable shared namespace is used.

### Monitoring

| Components | Ownership |
| --- | --- |
| `constants`, `errors`, `validation`, `models` | Versioned contracts, bounds, validation and typed records |
| `state`, `identity`, `connection`, `pricing`, `providers` | Private files, installation identity, connection and approved policy |
| `registry`, `volumes`, `host_sources`, `cleanup` | Exact source adoption, Docker identity, host static/auth reconciliation and removal |
| `locks`, `snapshots`, `scheduler`, `split_state` | Cross-process ownership, trusted caches and durable coordination |
| `collector`, `thread_providers` | Bounded, network-disabled collection, scoped read-only thread metadata and local usage evidence |
| `accounting`, `aggregation` | Pure accounting and its one-snapshot input adapter |
| `hub`, `publication` | Outbound privacy/HTTP and durable generation/rollback transactions |
| `service`, `migrations`, `lifecycle` | Scan coordination, resumable migrations and optional background execution |

`accounting.AccountingInputs` copies prices and project IDs into immutable
mappings and receives a timestamp explicitly. `aggregation` reads prices and
installation identity once, captures the approved provider set, and passes
explicit inputs through every provider and reporting period. Accounting does
not read files, execute processes, access the network or obtain the current
time. Inactive streams can produce empty payloads without reading prices.

Cross-component calls go through the owning module. Upload generation writes,
attempt recording, exact-device rollback and last-good status publication stay
together in `publication`; extracting them does not change their ordering.
Host-source reconciliation retains its existing copy policy and race checks.

`service` records aggregation precheck failures as well as collection and
publication failures. `lifecycle` can therefore keep the interactive terminal
quiet while status retains the actionable error.

### Configuration

`configuration.schema` owns shapes and primitive validation; `selection` owns
project/preset selection; `resolution` coordinates named launch, auth and
capability helpers; `packs` resolves MCP and skill selections. `codex` owns
Codex inventory and process overrides. `storage`, `rendering` and `editing`
own file transactions and TOML updates. `diagnostics`, `ui`, `interaction`,
`oauth` and `cli` own their respective presentation and effectful adapters.

Selected capabilities are still fully validated before inventory or runtime
effects. The isolated frontends resolve the installation root rather than the
working directory. OAuth helpers continue to use the actual installation root
after moving one package level deeper.

## Compatibility and verification

- Public CLI, configuration, private state and hub wire formats remain stable.
- Every required component is checked before installer replacement; recursive
  archive and Docker copies retain the packages, and bootstrap path validation
  still rejects symlinks and special files.
- Tests are organized by responsibility and patch the module performing each
  effect. Monitor unit fixtures reject unexpected subprocess and HTTP calls,
  so stale patches fail before reaching Docker or a hub.
- Accounting tests block file, process, network and clock access, verify
  immutable inputs and unchanged source data, and check every reporting period.
- Dependency tests protect acyclic imports and the pure accounting boundary.
  Configuration regressions protect validation order and selected MCP/skill
  composition; source/archive installer tests exercise isolated imports.

Future changes belong to the component owning the behavior. Add an abstraction
when it clarifies a real boundary; module length alone is not a reason to move
code into a generic utilities module.
