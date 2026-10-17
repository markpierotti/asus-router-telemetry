# Portfolio and CV copy

The bench copy below describes collector 1.5.5 / reporter 1.0.5 and the supervised
hardware results recorded on 2026-10-14. The deployment addendum describes 1.6.0
commissioning on 2026-10-17. The MIT-licensed project is public at
[markpierotti/asus-router-telemetry](https://github.com/markpierotti/asus-router-telemetry),
with an initial experimental [v0.1.0 release](https://github.com/markpierotti/asus-router-telemetry/releases/tag/v0.1.0).

## Website card

### Making a stock router observable

Custom ARM telemetry and a Python SSH collector, with supervised hardware tests
for management-link failure detection and recovery on an ASUS RT-AX56U. I directed an AI-assisted engineering workflow from firmware
analysis through review, regression testing and supervised recovery trials.

**Stack:** C · Python · Linux · ARM · OpenSSH · Ghidra · Docker/QEMU

**Results:** 14 successful healthy-path samples; an 18-attempt cable-loss trial;
incident detection and recovery at verified thresholds; independently recalculated
CPU metrics; explicit cleanup reconciliation.

Suggested links: **Read the case study** · **Explore the code** · **Inspect the evidence**

## Website case-study introduction

I wanted a consumer router to report useful system measurements while retaining its
stock firmware. I directed the development of a statically linked ARM C reporter
and an external Python collector, using Hermes and AI coding agents for research,
implementation and review. The engineering challenge extended beyond generating
valid JSON: authentication had to work in a real terminal, device output needed
strict bounds, and failed cleanup had to remain visible after the connection recovered.

On the physical RT-AX56U, the accepted revision completed a 14-sample healthy-path
trial. A separate Ethernet disconnect/reconnect trial recorded 12 successes and six
expected unreachable outcomes across 18 attempts. The incident opened after three
failures and resolved after two recovery successes. One stranded temporary directory
was reconciled manually and verified absent through a fresh SSH connection.

Routine sampling makes no model calls. I performed the physical setup and
authentication, defined acceptance criteria and challenged unsupported conclusions.
Code and security review findings were reproduced and addressed through behavioral
regression tests. The case study distinguishes these results from future work:
the historical trial did not establish unattended operation, AP/NAS roles or long-term reliability. Later Slim deployment observations are documented separately below.

## Broader home-lab context

I’m building a home networking and security lab to investigate device behavior,
collection failures and traffic containment. ASUS Router Telemetry is the first
completed component, supported by supervised hardware trials and automated
regression tests. Slim now hosts collection, DNS and monitoring. Project Limbo’s
simulated IoT exercises and detection/investigation workflows remain planned. A deployed SOC
remains future work. See the [roadmap](home-lab-roadmap.md).

## CV bullets

- Directed an AI-assisted embedded Linux project: firmware analysis, static ARM C
  telemetry and a Python standard-library SSH collector validated on stock ASUS
  hardware; defined scope, operated the bench and assessed acceptance evidence.
- Verified healthy collection and cable-loss recovery through 32 attempts across
  two supervised trials; checked CPU arithmetic, incident transitions, clean
  interruption and ownership-scoped cleanup, with review findings addressed through
  behavioral regression tests.

## Interview explanation

“The most useful lesson was that a successful response does not prove a reliable
system. Disconnecting the cable exposed cleanup uncertainty, so we preserved that
fact through recovery and reconciled it separately. AI accelerated implementation,
but I used source review, reproducible counterexamples and physical tests to decide
what the project could honestly claim.”

## Evidence for the copy

- [Current healthy-path acceptance](healthy-acceptance-20261014.md)
- [Current dropout and recovery acceptance](dropout-recovery-20261014.md)
- [Sanitized numeric summary](../evidence/dropout-summary-20261014.json)
- [Design decisions](design.md)
- [Human and AI attribution](provenance.md)

The two trial counts total 32 attempts, of which 26 succeeded and six were expected
link-loss failures. They are separate supervised exercises, not continuous production
uptime. Steady CPU percentages describe overall router activity, not instrumentation
overhead. Do not present the work as custom firmware, a deployed SOC, a complete
security audit or proof that all failure paths are handled.

## Deployment addendum (2026-10-17)

I deployed the collector on a wired Ubuntu thin client with explicit SSH key
verification, a dedicated service account and one-minute collection. Prometheus,
custom exporters and Grafana connect router and DNS health to dashboards and 13
alert rules. Private backup jobs create local archives and pull copies to my Mac.
An operator check showed all eight checked services running and recent collection
about four hours after startup. Telegram delivery, controlled reboot/endurance
validation and a restore rehearsal remain pending.

**Additional stack:** Ubuntu · systemd · Pi-hole · Prometheus · Grafana · SQLite.
See the [deployment record](slim-deployment-20261017.md) before quoting these results.
