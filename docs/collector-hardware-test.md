# Collector acceptance on the physical router

Date: 2026-10-12, Australia/Brisbane. All collections were run interactively by Mark Pierotti on one physical RT-AX56U (stock firmware) using the private bench collector against the reporter artifact recorded below. Raw samples, session timestamps, network identifiers, login material and host keys remain outside this repository.

> **Status note (added after the third remediation revision).** Everything on this page describes
> **collector v1.4.0 / reporter v1.0.3, artifact `3069b2bd…2ce0eb`** — the pair that actually ran on
> the unit. That remains the only hardware-accepted pair. A newer revision exists
> (**collector 1.5.1 / reporter 1.0.4, artifact `17ade0d2…66e36`**) which changed the transport
> lifecycle, cleanup evidence, diagnostics handling and reporter output. It is **OFFLINE-VALIDATED
> ONLY**: it has never been executed on the unit, so none of the results below describe it. A revised
> supervised acceptance sequence is required before any of its claims are used.

Two revisions have now been accepted on the unit. The **current** milestone (collector v1.4.0 / reporter v1.0.3) is described first, followed by the earlier milestone (collector v1.2.0 / reporter v1.0.2) as historical evidence for that revision.

---

## Current revision: collector v1.4.0 / reporter v1.0.3

Reporter artifact SHA-256:

```
3069b2bd3290ab3bb1db2d49b60e7e7c6a8e5090e4735537980c65926d2ce0eb
```

**17 samples in total were accepted**: two one-shot collections, a 14-sample supervised foreground loop, and one post-stop collection. Every attempt returned `outcome=success`, `cleanup_ok=true`, and no collection incident was opened.

| # | Milestone | Result |
| --- | --- | --- |
| 1 | One-shot `--once` | `success`, cleanup_ok, CPU state `first` |
| 2 | One-shot `--once` | `success`, CPU utilisation **1.71%** |
| 3 | Foreground loop `--loop --interval 60`, 14 samples | **14/14 `success`**, one password prompt, watchdog and artifact gates run once, clean Ctrl-C, no incident |
| 4 | Post-stop one-shot (lock-release proof) | **exit 0**, `success`, CPU utilisation **1.16%** |

Each new SSH session showed the full gate sequence: password accepted → SSH master established → temporary directory created → watchdog self-test killed its long job after three seconds with **status 137 and positive timer evidence** → router-side artifact SHA-256 matched the recorded digest before execution → `outcome=success` → silent cleanup. No gate failure, no hash mismatch and no separate cleanup failure occurred; nothing was deployed or left running.

### Independent CPU checks

Utilisation was recomputed independently from the saved aggregate `cpu_stat` counter sets, not from the collector's own arithmetic:

| Interval | delta_total | delta_idle | Re-derived | Collector |
| --- | ---: | ---: | ---: | ---: |
| one-shot #1 → #2 | 53671 | 52753 | 1.7104% → **1.71%** | 1.71% |
| loop, 13 consecutive pairs (#2→#14) | — | — | matched on all 13 | 0.90–1.07% |
| loop #14 → post-stop #15 | 178240 | 176170 | 1.1614% → **1.16%** | 1.16% |

Router uptime advanced monotonically across the loop. It also demonstrated two operational properties: the loop reused one SSH master (a single password prompt for all 14 samples), and the foreground lock was released on Ctrl-C — proven by the successful post-stop one-shot, since a still-held lock would have refused it.

### Recorded measurement notes

- **Measurement window vs attempt-start spacing.** The first loop pair spans **48.1 seconds** of router uptime, while the stored `interval_seconds` of **60.005** describes the local-monotonic spacing between attempt starts. The first attempt carries about 12 seconds of provisioning before the reporter runs. Later pairs within the reused session are about 60 seconds on both measures. Provisioning a new session for the post-stop collection produces another difference: **445.6 seconds** of router uptime versus **430.970 seconds** between attempt starts. The stored interval therefore describes collection scheduling, not exact measurement spacing. CPU utilisation uses the aggregate counter ratio and does not depend on that stored interval.
- **Small aggregate/per-CPU differences.** The aggregate `/proc/stat` `cpu` line differs from the sum of the per-CPU lines by 0–5 jiffies in the inspected samples. Differences of the same magnitude occur in the earlier v1.0.2 samples. Their cause was not independently established; the utilisation checks compare successive aggregate lines and do not require equality with the sum of the per-CPU lines.

---

## Earlier revision: collector v1.2.0 / reporter v1.0.2 (historical)

Two one-shot collections were run interactively by Mark Pierotti using the private bench collector v1.2.0 and reporter v1.0.2 (`router_health/2`).

Reporter artifact SHA-256: `b15f6117a31eb8ce3a70d013ff6d3b1abe5e2140297cc2df03c8690abe8b0590`.

### Observed results

Both transcripts show successful password authentication, an established SSH master, a watchdog self-test killing its long job after three seconds with status 137, verified artifact transfer, and `outcome=success`. The first sample correctly reported CPU state `first`; the second reported `ok` and utilisation **1.62%**. Two accepted sample files and two outcomes were retained; no collection incident was open.

The saved second-attempt state records `cleanup_ok=true` and no cleanup detail. This is recorded transport evidence, not a separate post-run SSH inspection. Raw samples, timestamps tied to private session paths, network identifiers and login material remain outside this repository.

### Independent numeric check

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

---

## Limits and remaining unproven conditions

The tested implementation used private bench defaults. This repository adapts paths and requires explicit operator configuration, so it is a separate portable copy checked offline. The following remain unvalidated:

- Continuous operation for periods longer than about 13 minutes (the supervised loop ran ~13 minutes); no multi-hour or overnight run.
- Reconnection after a real network or transport failure on hardware.
- Hardware interruption and cleanup-failure paths (exercised only in the offline suite).
- Unattended (key-based) authentication. The current transport explicitly selects password authentication; adding an SSH key alone would not enable unattended use without code/config support.
- Reboot persistence, USB/NAS storage and AP-mode operation.
- Monitoring overhead was not benchmarked; the utilisation checks validate counter ratios, not the monitor's incremental CPU cost.
