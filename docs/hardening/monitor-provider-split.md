# Token Monitor provider/account split

Status: implemented in Cage 0.34.0

## Goal

Make Token Monitor totals readable and useful across providers, accounts,
machines, and all known Cage Codex state volumes. The hub must not receive the
same session twice, and Cage must not guess a provider or a price.

## Device model

Each Cage installation publishes one stable hub device for each observed
provider stream. Device IDs use a readable, secret-free form:

```text
cage-<provider-or-account>-<platform>-<installation-id>
```

For the current Mac, the expected streams are:

```text
cage-zllm-mac-63ef6569
cage-openai-api-mac-63ef6569
```

An `unattributed` stream is created only when a session has no trustworthy
provider attribution, including a session that reports more than one provider.
It is not created merely because one repository contains sessions from more
than one provider. The old unsplit
`cage-local-<id>` device remains during migration and is removed only after the
new streams reconcile with its totals.

The pinned collector exposes provider names, not credential values. Different
credentials that report the same provider therefore remain in one provider
stream; Cage does not guess an account from a preset or a volume. Secrets,
credential values, repository paths, and raw auth configuration never enter an
ID or display label.

## Attribution rules

1. Scan and deduplicate session copies before partitioning them.
2. Use the provider information recorded in each session as the historical
   accounting source of truth.
3. Put a session with no provider in `unattributed`.
4. Put a session with more than one provider in `unattributed`; never split its
   counters by guesswork and never count it twice.
5. A repository may appear under more than one provider device. This is correct
   when its session history contains both providers.
6. A provider device contains only its attributed sessions. Do not upload the
   unsplit aggregate at the same time as its partitions. If a provider leaves
   the retained history, upload a zero summary so the hub does not keep stale
   totals.

## Discovery and coverage

Discovery is limited to Cage-owned Codex state volumes (`codex-state-*`) and
the exact volume paths registered by Cage. It must include volumes for normal,
hidden, scratch, worktree, and older projects when evidence shows that they
contain Cage Codex state. A missing repository mapping does not discard usage:
the volume is shown as `Recovered` or `Unattributed` and can be adopted by its
exact volume name.

Discovery is read-only. It must report mapped, registered, unregistered, and
unmapped volumes before any adoption or upload. Adoption requires the exact
volume identity and records a stable display name without inventing a host
path. Scanning a stopped volume is allowed; starting a coding container is not
required.

## Pricing

Price lookup is keyed by provider and model, for example
`openai-api:gpt-5.4` and `zllm:gpt-5.4`. Official OpenAI rates apply only to the
OpenAI stream. ZLLM rates are separate values and must be supplied from ZLLM's
price schedule. A missing rate remains unpriced and is reported. A legacy
model-only price may be used only when the provider is unambiguous; otherwise
it must not create a cost estimate.

For sessions containing multiple models, calculate cost per model when the
session contains model-level token counters. If that detail is absent, report
the tokens but leave the affected cost unpriced. Never apply one model's rate
to another model.

## Safe migration

1. Produce a dry-run report with provider, project, session, token, and
   cost totals, plus unknown/multi-provider counts.
2. Compare the partitioned token total with the current unsplit device. Keep a
   last-good local snapshot if the comparison cannot be completed.
3. Upload provider/account devices while retaining the old device.
4. Read back authenticated per-device hub statistics and verify that each
   provider total and the partition sum match the old unsplit device. Cost may
   change only when provider-specific prices improve the estimate; token counts
   must not change.
5. Delete the old device only after verification. Deletion is exact, resumable,
   and never uses a broad cleanup operation.
6. If any step fails, keep the old device and retry; do not publish partial
   replacement state as the only copy.

## User-visible checks

`cage monitor status` must show the provider/account devices, readable project
names, aggregate totals, price coverage, and any unattributed or unreconciled
records. A dry-run must not contact the hub or mutate Docker volumes. A normal
sync may contact the configured hub but must keep the collector network-free
and the hub secret host-side.
