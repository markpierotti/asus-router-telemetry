# Making a consumer router observable without replacing its firmware

**I directed an AI-assisted investigation that turned a stock ASUS RT-AX56U into a
target for custom ARM telemetry, then tested what happened when its management
connection disappeared.** The result is a C reporter, a bounded Python SSH collector
and a hardware-backed record of collection, incident detection, recovery and cleanup.

## The engineering problem

A successful decompilation or one valid JSON response was not enough. The software
had to run on the router's actual architecture, contain malformed output, keep
passwords out of logs, preserve evidence when SSH failed, and distinguish missing
telemetry from a device outage. Cleanup had to follow recorded ownership rather
than assume that a path or reported process ID was safe to act on.

## What I delivered

- A known-answer REA/Ghidra fixture, corroborated with a second disassembler, before relying on the toolchain for firmware work.
- A statically linked C reporter validated on stock ARMv7 firmware, without flashing a custom image.
- A Python standard-library collector with pinned identity/artifact checks, bounded execution and output, local retention, CPU deltas and deterministic incident thresholds.
- Behavioural regression tests for defects found through independent code/security review, including real controlling-terminal input and descendant-process cleanup.
- A supervised dropout trial: **16 attempts, 10 accepted samples, six expected unreachable failures**, incident opening after three failures and resolution after two recovery successes.
- Evidence that cleanup uncertainty survived recovery, followed by scoped manual removal and a separate absence check. Historical evidence remained unchanged.

The [hardware record](dropout-recovery.md) states the exact versions and limits. The
published [numeric summary](../evidence/dropout-summary.json) lets readers check the
CPU arithmetic without disclosing device access details.

## How I used AI agents

I set the scope, chose the sequence of milestones, performed physical setup and
authentication, and decided which evidence justified the next step. Hermes and coding
agents helped research, implement, test and review the work. Findings were checked
against source and reproduced before fixes were accepted; tests and model confidence
were not treated as substitutes for hardware results.

The operational collector makes **zero model calls**. AI supports investigation and
review; ordinary sampling, classification and incident state remain local code.

## Decisions that mattered

Temporary userspace execution made a useful result possible while preserving stock
firmware. Keeping the collector on an external host reduced the router component to
a single-shot program. Explicit unknown states prevented missing telemetry from
being presented as healthy. An append-only cleanup ledger preserved a failure that
a later successful connection could otherwise hide.

The work also exposed limits: terminal foreground handling is subtle, a local SSH
master check is not a network probe, and a short supervised trial does not establish
unattended reliability. Those limits are part of the deliverable.

## Next validation

Commission a small external host, add deliberate unattended-authentication support,
and test longer runs before considering a service deployment. Validate AP and storage
roles separately. A deployed SOC, full-network traffic visibility, custom firmware
and NAS performance are future work, not claims of this milestone.

Project lead: Mark Pierotti. Implementation is AI-assisted; attribution and tool
boundaries are recorded in [provenance](provenance.md).
