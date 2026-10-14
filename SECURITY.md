# Security policy

## Status and reporting

This is an experimental userspace telemetry project. Collector 1.5.5 and reporter
1.0.5 have supervised hardware evidence for the named ASUS bench configuration;
the portable configuration has offline test evidence. This is not a production
support commitment or a certification of the router firmware.

For a suspected vulnerability, use GitHub private vulnerability reporting through
the repository's Security tab. Reporting is enabled. Do not include
live passwords, private keys, full host-key files or unredacted device captures.
Include affected versions, the relevant code path, prerequisites, impact and a
minimal synthetic reproduction. No response-time guarantee is offered.

## System and scope

The single-shot C reporter reads Linux telemetry and emits JSON. The external
Python collector uses OpenSSH to upload and execute a temporary reporter, validate
its response and retain local samples, outcomes and collection-incident state.
No listener, unattended service or router firmware modification is included.

Relevant assets are operator credentials, trusted host keys, executable identity,
the collector host's files and terminal, device files, and retained evidence.
Review the reporter, collector, build scripts, test infrastructure and CI together
with their different execution and privilege boundaries.

## Threat model and trust boundaries

- The operator explicitly selects the target, local configuration, artifact,
  host-key records and data directory. Local roots and configuration are trusted;
  this project does not sandbox a hostile process concurrently changing them.
- Device responses, numeric fields, filesystem names and diagnostics are untrusted
  even after SSH authenticates the server. An authenticated or compromised target
  is a relevant source of malicious input, not a reason to dismiss a finding.
- Ambient SSH configuration must not change the intended transport. Strict host-key
  verification is distinct from the optional integrity pin on the local host-key file.
- Test doubles and public fixtures are not live credentials. Hosted CI must use no
  real target credentials and no privileged trigger that executes external PR code.

## Security invariants

- Live execution must retain host-identity and configured artifact-integrity gates.
  A failed identity or artifact check must not silently fall back or continue.
- Received output, parsing and execution must have explicit limits. Non-finite,
  malformed or unavailable data must not become valid healthy measurements.
- Password entry must go to SSH, without being stored or logged by the collector.
  Device-controlled diagnostics must be bounded and sanitized before display.
- Remote removal requires acknowledged ownership of the exact created directory.
  Unconfirmed removal must remain visible through recovery and teardown.
- A reported PID alone must never authorize signalling a foreign process. The
  collector may terminate its own subprocess groups under its documented bounds.
- Interruption must release the local lock and record relevant cleanup uncertainty.
  Failed evidence persistence must not be represented as a successful saved record.
- History has count and byte bounds; local files and directories use restrictive
  permissions. Source identities must remain distinct across devices and mock runs.

## Reportable findings and limitations

Report violations of these properties with realistic prerequisites and impact,
including authenticated-target attacks, unsafe cleanup, credential disclosure,
unbounded resource use, terminal injection, command injection or CI privilege abuse.
Pure correctness findings should still be reported, with security impact separated
from numerical or operational impact. A passing test or short hardware trial does
not waive a reachable issue.

Unattended/key authentication, multi-hour reliability, host/router reboot recovery,
AP/NAS roles and general whole-network visibility remain unvalidated. SSH master
liveness checks describe a control socket, not independent process identity or
router reachability. The pending-cleanup ledger records historical uncertainty;
manual reconciliation does not erase it. Storage candidates are heuristics and
do not prove physical USB identity. None of these limits excludes a new finding
in the implemented surfaces.

Vendor firmware and external tools are not distributed here. A vulnerability
solely in those upstream components belongs with its maintainer; a flaw in this
project's use of them remains relevant.
