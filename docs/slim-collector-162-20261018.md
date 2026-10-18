# Slim: collector 1.6.2 deployment — 2026-10-18

Collector **1.6.2** and its paired unit replaced 1.6.0 and the interim
`RestartPreventExitStatus=1 2` at 16:10 AEST.

## Procedure

1. The application tree (repository top level without `deploy/`, plus the unit file
   one collector test reads) was staged as `/home/rtax/app-1.6.2` and the full
   collector suite ran on Slim as `rtax` before anything changed: 392 run, one
   expected skip. A first attempt stopped here without changes because the staged
   tree lacked that unit file.
2. The collector was stopped (exit 130, orderly), `app` moved to `app-1.6.0`, the
   staged tree moved to `app`, and the repository unit installed
   (`RestartPreventExitStatus=4 2`). A scripted rollback to 1.6.0 was armed for a
   failed start or no fresh sample within three minutes.
3. The first sample after the swap reported collector version 1.6.2; Prometheus
   sample age was 75 s a minute later and no incident was open.

`/home/rtax/app-1.6.0` and a root-only copy of the previous unit are retained for
rollback.

## Stop-path cleanup

The 1.6.0 stop during the swap could not remove its router work dir: systemd's
default `KillMode=control-group` signalled the ssh control master together with the
collector, so the cleanup had no connection and recorded a pending-cleanup entry
(by design it never deletes on a fresh connection). The unit now sets
`KillMode=mixed`. Verified: a stop issued mid-collection exited 130, logged
`ssh control master exited (shutdown verified)` and added no pending-cleanup entry.

Two older entries (`/tmp/rt.0369c207` from an earlier dropout and `/tmp/rt.94abffc1`
from the swap) still need the documented manual reconciliation; the router's `/tmp`
is RAM-backed, so a router reboot also clears them.
