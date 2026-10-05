# Background accounting for Codex CLI exit

Introduced in Cage 0.38.12. After updating, new Codex CLI container processes
use this accounting lifecycle. Already-open processes retain their loaded code.

## Ownership and completion

The foreground owns only `accounting.lifecycle.Producer`. At session start it
captures the authorized backend/source, Docker context and engine, permission
epochs, and a verified private copy of Cage's Python package. Each producer
submits an initial request and periodic requests. Cleanup first quiesces every
producer, then atomically writes and fsyncs its final revision. It does not join
a collector, hold an HTTP lock or wait for delivery. Optional accounting errors
warn without replacing the primary CLI exit status.

An on-demand Python process owns all collection and delivery for one Cage
configuration directory. A kernel lifetime lock provides single ownership;
the short wake handshake prevents a request from being lost during idle exit.
No PID is used as proof of ownership. A running worker is reused without
spawning another Python process. It exits when all jobs are delivered, cancelled
or blocked. Retry waits are bounded, use jitter, and check for new ready work at
least once per second between backend operations. Four final jobs are followed
by a ready ordinary job when one exists, preventing starvation.

Jobs coalesce by exact source descriptor and permission epochs. `requested`,
`claimed`, `collected`, `delivered` and `final` are independent revisions.
Receipts acknowledge only the claimed revision. If request N+1 arrives while N
is running, completing N leaves N+1 pending. A collected Token Monitor snapshot
can be reused for a delivery retry when its current hash still matches; a new
final revision requires fresh collection. Poke retries recollect and preserve
the existing history-prefix validation before committing export files.

`monitoring.service.ScanOutcome` distinguishes successful publication from a
busy coordinator or aggregate lock. A durable caller can collect a missing peer
snapshot off the foreground path. It never treats an old status dictionary or
a skipped coordinator attempt as proof that the new request was delivered.
Provider partitioning, deduplication, pricing, diff-aware publication,
last-good generations and attempted-provider repair remain in their existing
modules. A successful unchanged comparison can acknowledge the actual retained
last-good generation without sending duplicate POSTs.

## Authority and recovery

Queue files never contain credentials, arbitrary commands or a full LaunchPlan.
The backend validates fixed fields before Docker or HTTP effects. Token Monitor
requires the captured source and connection epochs. Poke requires its capability
admission, captured volume fingerprint and export epochs. Peers contributing to
a monitor aggregate are revalidated before commits. Registry completion cannot
reactivate a disabled or replaced source.

Connection changes, disconnect, source disable/re-adoption and forget rotate
epochs under an effect fence. Same-value reconnect/adoption cannot revive old
work. Publication and its repair journal bind both connection and source
authority. A revoked journal requires an explicit full `monitor sync`; the
supersession marker survives a crash before its replacement journal is written,
so an old baseline cannot be used for rollback onto the new connection.

The collector path retains the existing per-source/export locks. Before cache
reuse, recovery reconciles owned attempts against their original Docker engine.
A journal precedes `docker create`; the returned ID is persisted before
`docker start --attach`. Cleanup checks exact name, nonce, job, image, mounts,
network mode, read-only root and known ID. An uncertain create retains its
journal until the stopped container is observed; start was never issued, so
it cannot be an unowned cache writer. Only exact owned IDs are removed. Existing
user sessions, unrelated containers and volumes are never cleanup candidates.

Worker subprocesses have detached standard streams, no inherited terminal and
a restricted environment. Docker context/host/config/TLS paths are explicit;
engine mismatch blocks collection. The copied Python runtime and Poke helper
stay available after installer replacement. Source paths and credentials are
not emitted by the local queue-status view. Host and Desktop retain their
existing synchronous lifecycle and host credential/source-lease ordering.

## Operating contract

| Need | Command or behavior |
| --- | --- |
| Local monitor progress, no Docker/HTTP request | `cage monitor jobs [--json]` |
| Monitor totals and repair diagnostics | `cage monitor status [--json]` |
| Retry retained authorized monitor work | `cage monitor jobs --retry` |
| Complete a fresh full monitor reconciliation | `cage monitor sync` |
| Export status and pending revisions | `cage poketoken status [--json]` |
| Retry retained authorized exports | `cage poketoken retry` |
| Revoke admitted exports, preserve completed files | `cage poketoken cancel-pending` |
| Explicit synchronous project export | `cage poketoken sync PATH` |

Automatic retries wait approximately 5, 30, 120, 600 and 1800 seconds; a sixth
failed attempt becomes `blocked`. Periodic submissions do not reset backoff or
unblock errors. Explicit retry preserves captured epochs and cancels revoked
work. New eligible launches wake pending work; after reboot no job runs until
such a wake. No launchd/systemd service or login automation is installed.

The v1 store bounds job files to 32 KiB, 4096 jobs, 16384 permission tombstones,
8192 outstanding attempt entries, and 64 code snapshots of at most 16 MiB each.
It retains terminal records and code snapshots rather than deleting resources
that an open producer may still reference. Reaching a bound fails visibly and
requires maintenance with all relevant producers/workers stopped. Do not prune
this state or Docker resources while work is active.

Older already-open processes keep their loaded behavior and do not know the
new revocation fence or attempt journal. Shared collector locks remain
compatible, but new guarantees do not retroactively protect old processes.
Before rollback, finish/revoke pending work using this version and preserve
private state; old versions cannot safely repair new upload journals. Failed
or partial remote effects remain governed by existing generation repair;
the queue promises retryable, at-least-once work, not transactional hub ingest.

## Measurements and validation

macOS 27.0 arm64, Python 3.12.3, local Colima Docker. The reproducible
`tests/benchmark_accounting_exit.py` uses fixed synthetic 1 MiB and 64 MiB JSONL
histories, an existing `cage-token-monitor:0.38.10` image for both baseline and
candidate, a private HTTP fixture on loopback, disposable volumes and fresh
private accounting roots. It never reads real sessions or contacts a real hub.

| Fixture | Synchronous first collection + export | Two repeated synchronous runs | Exit handoff p95, worker initially absent | Exit handoff p95, worker running |
| --- | ---: | ---: | ---: | ---: |
| 1 MiB | 3.790 s | 3.784 / 3.750 s | 25.979 ms | 17.383 ms |
| 64 MiB | 4.090 s | 3.927 / 3.906 s | 24.401 ms | 17.330 ms |

There are 20 handoff samples per cell. Medians were 22.123/13.529 ms (1 MiB)
and 20.686/13.113 ms (64 MiB), absent/running worker respectively. Maximum was
35.621 ms. The synchronous baseline has three samples per fixture and is not
reported as a p95. Every final revision was eventually delivered, the hub
aggregate retained exactly 150 synthetic tokens, and one sanitized export file
was verified for each fixture.

"Cold" here means no worker owns the lifetime lock before creating producers.
It does not mean cold OS/Docker/filesystem caches. The handoff measurement covers
both producers' final cleanup and returns the primary status 23. It excludes
Codex's own TUI shutdown and other Cage teardown, so it is not a whole-CLI exit
guarantee. Background collection still takes seconds; those seconds have moved
outside the foreground lifecycle.

A subsequent successful three-sample check measured median completion of the
background delivery at 3.923 seconds for the 1 MiB fixture. An additional
300-exit stress run on the final path advanced every final revision without
warnings and drained the complete queue; handoff p95 was 19.294 ms, maximum
23.251 ms. A prior diagnostic emitted two generic warnings whose cause was not
captured. Admission now retries short lock contention once, distinguishes
save/wake failures, and the benchmark fails on any such exception. See the
checkpoint for that observation; the local result is not a production soak.

Code pinning cost 29–30 ms initially and about 11 ms when cached; capturing the
Docker binding cost 121.5 ms in this run. These are startup components, not a
measurement of full CLI launch. No existing session was closed for this work.

Run the focused regressions with `python3 -m pytest -q tests/test_accounting_*.py`.
Real Docker recovery is opt-in:

```bash
CAGE_RUN_DOCKER_SMOKE=1 CAGE_ACCOUNTING_TEST_IMAGE=cage-token-monitor:0.38.10 \
  python3 -m pytest -q tests/test_accounting_docker.py
python3 tests/benchmark_accounting_exit.py --version 0.38.10 --samples 20
```

The Docker tests verify create/start, immutable identity checks and a killed
test owner whose collector keeps writing until exact recovery stops it. Unit
tests cover revision races, process crash and lease release, idle-wake races,
backoff, poison files, revoked/ABA authority, pending repair supersession,
missing peers, unchanged delivery, retained exports, runtime replacement,
and host/Desktop lifecycle compatibility. The full suite and release-format
checks are recorded in [the local checkpoint](hardening/PROGRESS.md).
