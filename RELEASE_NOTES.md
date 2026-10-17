# Unreleased — collector 1.6.1 storage correction

- Sync the actual parent directory after an atomic write. Sample files live under
  `samples/`; syncing only the top-level data directory did not flush their renamed
  directory entries. State and pruned outcomes still sync the top-level directory.
- Added focused regression tests. Directory fsync remains best effort on systems
  that reject it; this is not a guarantee against every crash or storage failure.
- Gemini supplied the observation; Codex checked it against source and implemented
  this correction. It has **not** been deployed to Slim, which still runs 1.6.0.

# Unreleased — Slim deployment and collector 1.6.0

- Optional public-key authentication for unattended collection: `identity_file` in
  `config.json` or `--identity-file`. Exactly one pinned key is offered under
  `BatchMode` (no password, agent or default keys, no terminal handover); a
  missing, relative, non-regular or group/world-accessible key file, or one combined
  with `interactive_auth: true`, exits 2 before any attempt. Password mode is
  unchanged and remains the default.
- Key mode was deployed on Slim on 2026-10-17. The earlier Claude session reported
  386 collector tests passing on Slim; later operator output showed the service
  running and producing samples. This does not extend the 1.5.5 bench acceptance.
- Added sanitized monitoring/exporter/dashboard/alert/service/backup examples and
  setup, operations and recovery documentation. Telegram delivery, controlled
  reboot/endurance testing and restore validation remain pending.
- See the [dated deployment record](docs/slim-deployment-20261017.md) for evidence
  sources and limits. No new project release or tag is created by this branch.


# v0.1.0 — Initial experimental release

Component versions: collector **1.5.5**, reporter **1.0.5**, JSON schema
`router_health/2`. [Project release v0.1.0](https://github.com/markpierotti/asus-router-telemetry/releases/tag/v0.1.0)
is published as an experimental milestone. Production source identities and the
accepted ARM digest are recorded in the acceptance documents.

## Included

- C telemetry reporter and an external Python standard-library collector.
- Explicit SSH identity/artifact gates, a tested execution watchdog, bounded device
  output, schema validation, CPU deltas, local retention and incident transitions.
- Behavioral regression tests and an offline mock workflow.
- Sanitized evidence and a case study describing review-driven corrections,
  human decisions and AI-assisted implementation.

## Hardware evidence

Two separate supervised bench trials on the exact accepted artifact and private
bench configuration: 14 healthy-path successes, then an 18-attempt Ethernet
dropout/recovery trial with 12 successes and six expected unreachable outcomes.
The collection incident opened after failure three and resolved after recovery
success two. A stranded owned directory was recorded, manually reconciled and
checked absent through a fresh SSH connection.

These are management-link failure observations, not proof of router outage
detection, unattended recovery or continuous production reliability. The portable
configuration is offline-tested and is not separately hardware-accepted.

## Limits and first use

Run the README's offline tests and mock example first. Live collection requires
your own reviewed configuration and executes a temporary program on the target.
No compiled ARM artifact, vendor firmware, credentials or raw private captures are
included. Future ARM builds may differ because apt packages are not pinned.

Multi-hour runs, unattended authentication, power-loss/reboot handling, AP and USB
NAS roles remain future work. No service, automatic remediation or full-network
SOC is deployed by this release. The offline matrix passed on ubuntu-24.04 and
macos-15 with Python 3.11 and 3.13: 368 collector tests passed per job, one
expected no-controlling-terminal skip, and all reporter checks passed. Real
controlling-PTY tests passed. The publication history changes commit email
metadata; every original commit tree, message, author name and timestamp was
preserved and compared with the privately archived history.

The repository is public and licensed under MIT. GitHub private vulnerability
reporting is enabled; SECURITY.md describes the reporting guidance and security
boundaries. The v0.1.0 tag remains fixed at its accepted source snapshot,
`5a68321cb1bbd6d712d3fe1f16516a9c2a835d59`. Documentation updates record
publication without moving that tag or changing its code.
