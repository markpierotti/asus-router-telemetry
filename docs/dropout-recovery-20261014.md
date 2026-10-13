# Supervised Ethernet dropout/recovery — 2026-10-14

Collector **1.5.5**, reporter **1.0.5**, schema `router_health/2`.
ARM SHA-256: `a1cb7d1f1787c2893bacd7fa1233727da01cca8944fc8d47064708c8c8d6d0c3`.
Production source revision: `036d3383a04002e81ca2710d076ad04f42c89b72`.
The private bench adaptation differs only in documented operator defaults; portable
configuration remains offline-tested. This follows the separate
[14-sample healthy-path acceptance](healthy-acceptance-20261014.md).

## Trial

One host-side Ethernet cable disconnect/reconnect on the isolated bench; router power
and configuration unchanged. **18 attempts: 12 successful samples and six expected
unreachable outcomes.** The incident opened after failure 3 and resolved on recovery
success 2. Recovery established a new pinned SSH master and verified the same ARM
artifact before execution. The loop stopped with exit 130; a fresh post-stop
collection exited 0 and the local lock was independently free.

All 11 aggregate and four-per-core counter comparisons independently match. Router
uptime advanced with unchanged boot identity. A recovery CPU value of 8.42% covers
about 1.4 seconds immediately after provisioning; attempt starts were 30 seconds
apart. Counter-based utilisation is valid for that shorter sampling interval.
All 82 pre-trial data files, including prior acceptance evidence, remained unchanged.

## Cleanup and reconciliation

The loss prevented cleanup of one owned session directory. The attempt recorded
cleanup uncertainty and one pending-cleanup entry; recovery did not silently delete
or erase it. Read-only inspection of the four exact trial paths found three absent
and the lost-session path present with the expected reporter and output files.
The reporter digest and expected contents were checked before exact-path removal.
A separate fresh pinned SSH connection then verified absence. All three operator
inspection/removal/absence exits were zero.

The original pending ledger is retained byte-for-byte, with a separate manual
resolution record. Status may still display its historical pending count; that
count is not a live inventory of remaining directories. No automatic reconciliation
feature was added. One mistyped password on the first read-only inspection was
preserved separately; it did not alter the collector trial's outcomes.

## Evidence and boundaries

The operator performed live SSH actions. Verification independently inspected saved
samples, outcomes, incident transitions, scripts/logs, hashes and local lock state.
A distinct SSH absence check corroborates cleanup; it is not a second model's review.
Raw captures, target configuration and local paths remain private. The
[curated numeric summary](../evidence/dropout-summary-20261014.json) contains no credentials
or target identifiers. Historical [1.5.4 / 1.0.4 results](dropout-recovery.md) remain intact.

This proves one **supervised host-side link-loss/recovery trial**, not power-loss or
reboot recovery, address/DHCP changes, long-running operation, unattended auth,
USB/NAS/AP operation or production deployment. Local control-socket checks do not
independently prove process exit. Nothing is scheduled or persistently deployed.
