# Changelog

All notable Cage changes are recorded here. Breaking or recovery-sensitive
details live in the linked migration guide.

## Unreleased

No changes yet.

## 0.23.0 — 2026-07-16

### Security and correctness

- stopped rewriting repository `.mcp.json` files and replaced that behavior with
  a validated private read-only overlay;
- reconciled generated Claude/Codex auth and MCP state so preset switches remove
  stale authority without requiring manual credential provisioning;
- hardened Codex OAuth rotation with validated, identity-bound, conflict-aware
  synchronization and no writable host credential mount in helper containers;
- authenticated and bounded Netgate, MCP, and host-command transports with fresh
  per-launch credentials;
- added Netgate DNS-rebinding/SSRF defenses, request and prompt limits, fixed
  CONNECT ports, and portable authenticated Docker host-gateway access;
- changed host bridge execution to explicit argv, `shell=False`, a sanitized
  host `PATH`, startup-pinned executables outside every Cage-writable mount,
  minimal environment, framed status where applicable, and process-group cleanup;
- isolated every host Python control-plane launch from repository `PYTHONPATH`
  and made root entrypoint writes treat persistent model-owned symlinks as unsafe.

### Configuration, installation, and supportability

- added strict central-config schema and transport-name validation, safer custom
  header rules, capability-oriented explain/doctor output, and a minimal starter
  preset;
- made source and release installation staged, ownership-checked,
  checksum-verified, atomic, and rollback-capable;
- added Python 3.11/3.12 CI, real-Docker smoke tests, release ordering gates,
  focused adversarial regression suites, a security model, and durable hardening
  records;
- corrected product language: Cage reduces accidental filesystem blast radius,
  while readable credentials, writable Git metadata, proxy bypass, and enabled
  host integrations remain explicit Developer/Host-integrated risks.

### Breaking changes

Bridge command parsing/protocol, strict configuration validation, generated-state
cleanup, Netgate restrictions, OAuth reconciliation rules, and installer
ownership checks can affect existing setups. Follow the
[0.23.0 migration guide](docs/hardening/MIGRATIONS.md#0230--2026-07-16).
