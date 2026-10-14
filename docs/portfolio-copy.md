# Portfolio and CV copy

This copy describes collector 1.5.5 / reporter 1.0.5 and the supervised hardware
results recorded on 2026-10-14. Link to the published repository when a destination
and license have been selected; no public URL is assumed here.

## Website card

### Making a stock router observable

Custom ARM telemetry, a Python SSH collector and hardware-tested outage detection
on an ASUS RT-AX56U. I directed an AI-assisted engineering workflow from firmware
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
unattended operation, AP/NAS roles and long-term reliability have not been established.

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
