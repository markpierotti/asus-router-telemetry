# Supervised Ethernet dropout and recovery

On 13 October 2026, collector **1.5.4** and reporter **1.0.4** passed a supervised
cable-loss/recovery trial on one ASUS RT-AX56U running stock firmware. The operator
unplugged only its isolated bench Ethernet cable, kept the router powered, restored
the link and supplied the password when a new SSH session was required.

The tested code revision was `633963190ad85b6eed12cae208e1629ba9565430`. The reporter
artifact was SHA-256 `17ade0d2e85ed2880cb6a38de018bcf3f3d76d3f9090eb29eda54c007fe66e36`.
The target ran firmware 3.0.0.4.386_45898 and Linux 4.1.52 armv7l.

## Results

| Stage | Recorded result |
| --- | --- |
| Baseline | Two successful one-shots; CPU state `first`, then `ok` |
| Foreground loop, 30-second interval | Four initial successes, six classified `unreachable` failures, then three recovery successes |
| Incident opening | Opened after three consecutive failures |
| Incident resolution | Resolved after two consecutive recovery successes |
| Human stop | Clean Ctrl-C message, exit **130** |
| Post-stop one-shot | Success, exit **0**, proving the data-directory lock was released |
| Total | **16 attempts: 10 successes, six expected outage failures; 10 saved samples** |
| CPU cross-check | All **nine** aggregate-counter comparisons independently reproduce stored percentages |
| Preservation | 41 historical evidence files, 17 original trial files and two preservation manifest pins unchanged at closeout |

```mermaid
flowchart LR
    B[Healthy collection] --> L[Bench cable removed]
    L --> F[Three failed collections]
    F --> O[Collection incident opens]
    O --> R[Link restored and operator authenticates]
    R --> S[Two successful collections]
    S --> C[Incident resolves]
```

An SSH failure established loss of the management path. It did not establish that
the router had powered off or that the home network had failed. The home network
was separate from the fault injection.

## Cleanup uncertainty was preserved through recovery

The pre-loss session could not confirm removal of its owned temporary directory.
The failed attempt recorded that uncertainty and queued exactly one reconciliation
entry. Recovery created a new owned directory; a later clean close did not erase
the earlier entry. This is the behaviour the 1.5.4 remediation was intended to restore.

After the loop, the operator inspected all five exact trial paths. Four were absent;
the stranded one remained. A reviewed script verified its reporter digest, removed
only that owned path and verified removal. A separate read-only SSH session then
verified absence. All three operator-reported commands exited zero.

The original pending entry is retained as history, with resolution in a separate
private evidence record. The current `status` command does not consume that record
and can still display the historical pending count. No automatic cleanup or ledger
reconciliation feature was added for this trial.

## Evidence and limits

[Curated numeric results](../evidence/dropout-summary.json) include versions, counts,
exit statuses and the independently recalculated CPU counter deltas. Raw telemetry,
login details, addresses, host keys, private paths and exact session times remain
outside this repository. The curated file supports inspection of the published
arithmetic; it does not let a reader independently authenticate the private captures.

This was one short, supervised fault sequence using private bench configuration.
The portable collector has deliberate configuration differences and was checked
offline, not separately executed on hardware. Password-based recovery required the
operator. Multi-hour operation, unattended authentication, other transport faults,
reboot, AP mode and USB/NAS operation remain unproven. CPU values are not a benchmark
of the collector's incremental overhead. Local SSH control-socket checks do not
independently prove process exit. The documented terminal-handover timing race remains.
