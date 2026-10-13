# Supervised healthy-path acceptance — 2026-10-14

Collector **1.5.5**, reporter **1.0.5**, schema `router_health/2`.
Source production revision: `036d3383a04002e81ca2710d076ad04f42c89b72`.
Reporter source SHA-256: `18c2fdfa7466ceb6cdf18b4d07cffd83f0b56d1b8a583118fc9ffd17c6d65907`.
ARM artifact SHA-256: `a1cb7d1f1787c2893bacd7fa1233727da01cca8944fc8d47064708c8c8d6d0c3`.

The exact ARM artifact and private bench adaptation ran on an ASUS RT-AX56U HW 1.0,
firmware 3.0.0.4.386_45898, Linux 4.1.52 armv7l. Portable source has documented
configuration differences; portable configuration was tested offline.

| Stage | Observed result |
|---|---|
| First one-shot | Success, CPU state first, exit 0 |
| Second one-shot | Success, independently reproduced CPU 1.97%, exit 0 |
| Foreground loop, interval 60 s | 11/11 successes, clean Ctrl-C, exit 130 |
| Post-stop one-shot | Success, independently reproduced CPU 1.56%, exit 0 |

14 samples and 14 outcomes were accepted. All 13 aggregate counter comparisons and
all four per-core comparisons independently matched. Boot identity stayed unchanged
and router uptime advanced. Every outcome records confirmed cleanup; no collection
incident or pending-cleanup entry remained. The local data-directory lock could be
acquired after stopping, and the post-stop collection proves end-to-end restart.

Loop spacing was 59.9962–60.0050 s. Steady attempts took 0.179–0.201 s; the first
included authentication/provisioning. Steady aggregate CPU was 0.68–1.10%.
Those figures describe this supervised bench trial, not sustained loaded operation.

The operator ran the SSH commands; verification independently inspected saved
samples, outcomes, state, hashes and the local lock. Remote cleanup and master
shutdown are transport-recorded conclusions, not separate remote directory/process
inspection. Raw evidence and target configuration remain private. All 64 pre-existing
bench data files were byte-identical after the trial.

## Limits

This record establishes **supervised healthy-path acceptance only**. A later, separate
[Ethernet dropout/recovery trial](dropout-recovery-20261014.md) passed on the same versions
and includes manual cleanup. Longer runs, unattended authentication, reboot persistence
and USB/NAS/AP operation remain unproven. Older trials retain their own version identities.
Nothing is installed persistently or scheduled.

An unset router wall clock means boot_time is a boot identity, not an accurate civil
timestamp. Attempt-start timestamps include provisioning; CPU percentages derive
from raw counter deltas and do not divide by that attempt-start interval.
