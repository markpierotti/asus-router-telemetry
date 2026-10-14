# Initial portfolio release candidate

Component versions: collector **1.5.5**, reporter **1.0.5**, JSON schema
`router_health/2`. Project release tag remains to be selected. Production source
identities and the accepted ARM digest are recorded in the acceptance documents.

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
SOC is deployed by this release. Hosted CI acceptance is pending its first run.

This is a private GitHub release candidate under markpierotti/asus-router-telemetry,
licensed under MIT. Public visibility and a release tag remain pending owner approval.
GitHub private vulnerability reporting must be enabled before a public release;
SECURITY.md describes the reporting guidance and security boundaries.
