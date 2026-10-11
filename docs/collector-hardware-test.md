# Collector acceptance on the physical router

Date: 2026-10-12, Australia/Brisbane. Two one-shot collections were run interactively by Mark Pierotti using the private bench collector v1.2.0 and reporter v1.0.2 (`router_health/2`).

Reporter artifact SHA-256: `b15f6117a31eb8ce3a70d013ff6d3b1abe5e2140297cc2df03c8690abe8b0590`.

## Observed results

Both transcripts show successful password authentication, an established SSH master, a watchdog self-test killing its long job after three seconds with status 137, verified artifact transfer, and `outcome=success`. The first sample correctly reported CPU state `first`; the second reported `ok` and utilisation **1.62%**. Two accepted sample files and two outcomes were retained; no collection incident was open.

The saved second-attempt state records `cleanup_ok=true` and no cleanup detail. This is recorded transport evidence, not a separate post-run SSH inspection. Raw samples, timestamps tied to private session paths, network identifiers and login material remain outside this repository.

## Independent numeric check

An independent read of the two saved aggregate CPU counter sets produced:

| Counter | Delta |
| --- | ---: |
| user | 144 |
| nice | 0 |
| system | 806 |
| idle | 57637 |
| iowait / irq / softirq / steal | 0 each |
| total | 58587 |

`100 × (58587 − 57637) ÷ 58587 = 1.62%` after rounding, matching the collector. Router uptime advanced approximately 146.4 seconds. This validates that pair; it does not independently validate every collector field or every failure path on hardware.

## Limits

The tested implementation used private bench defaults. This repository adapts paths and requires explicit operator configuration, so it is a separate portable copy checked offline. Continuous looping, reconnection after real network failure, hardware interruption/cleanup failure, unattended key authentication, reboot persistence, USB/NAS and AP operation remain unvalidated. The current transport explicitly selects password authentication; adding an SSH key alone would not enable unattended use without code/config support.
