# Making a consumer router observable without replacing its firmware

**I directed an AI-assisted investigation that turned a stock ASUS RT-AX56U into a
target for custom ARM telemetry, then tested what happened when its management
connection disappeared.** The result is a C reporter, a bounded Python SSH collector
and a hardware-backed record of collection, incident detection, recovery and cleanup.

**Bench milestone (2026-10-14):** collector 1.5.5 / reporter 1.0.5 passed separate healthy-path and Ethernet dropout/recovery acceptance. The latest dropout trial recorded 18 attempts, 12 successes and six expected failures, with threshold transitions, CPU deltas and manual cleanup verified. [Current evidence](dropout-recovery-20261014.md); earlier results below keep their original scope.

## Why this project matters to the home lab

I’m building a home networking and security lab to investigate how devices behave,
how failures become visible, and how segmentation contains unwanted traffic.
ASUS Router Telemetry is its first completed component. It establishes a documented
experimental telemetry milestone that the next stages can build on.

Device-health data and collection failures will be one input to the broader lab.
Network captures, detection rules, alert triage and response exercises need their
own implementation and acceptance evidence. The [roadmap](home-lab-roadmap.md)
records that planned work; a deployed SOC remains a future goal.

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
- The current supervised dropout trial: **18 attempts, 12 accepted samples, six expected unreachable failures**, incident opening after three failures and resolution after two recovery successes.
- Evidence that cleanup uncertainty survived recovery, followed by scoped manual removal and a separate absence check. Historical evidence remained unchanged.

The [current hardware record](dropout-recovery-20261014.md) states the exact versions
and limits. The [published numeric summary](../evidence/dropout-summary-20261014.json)
records trial counts and reconciliation without disclosing device access details.
The [earlier 16-attempt trial](dropout-recovery.md) and its
[numeric summary](../evidence/dropout-summary.json) remain historical evidence for
collector 1.5.4 / reporter 1.0.4.

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

## Linux deployment (2026-10-17)

I commissioned Slim as the wired Linux host. Collector 1.6.0 uses an explicit SSH
key under a dedicated account and runs every minute as a systemd service. Pi-hole,
Prometheus, custom exporters and Grafana provide DNS and device-health dashboards,
13 alert rules and private backups. An operator check about four hours after
collector startup showed a recent sample and all eight checked services active.
The [deployment record](slim-deployment-20261017.md) states the evidence boundaries;
[configuration examples](../deploy/slim/README.md) document the arrangement.

## Next validation

Test controlled host/router restarts, longer operation and recovery from a backup.
Connect and test Telegram delivery and an external uptime check. AP mode is in use;
USB storage performance, segmentation and packet visibility remain future work.
A deployed SOC and custom firmware are not claims of either milestone.

Project lead: Mark Pierotti. Implementation is AI-assisted; attribution and tool
boundaries are recorded in [provenance](provenance.md).
