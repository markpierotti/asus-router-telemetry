# Design and engineering tradeoffs

```mermaid
flowchart LR
    P[Linux /proc and statvfs] --> R[Single-shot C reporter]
    R --> S[Bounded SSH transport]
    S --> V[Schema validation]
    V --> D[Successive CPU deltas]
    D --> H[Local history and status]
    V --> I[Collection incidents]
```

## Lightweight router component

The C reporter opens no listener and exits after one JSON document. A static ARMv7 build avoids needing the router's exact dynamic loader, while still depending on its kernel and instruction support. This is userspace software, not replacement firmware. Filesystem capacity uses statvfs; `/proc` parsing supports fixture input.

Mount paths decode octal escapes such as `\040` before capacity lookup. Schema 2 excludes virtual placeholder mounts and duplicate records; unavailable memory and inode values remain null. Candidate storage uses path/device heuristics and does not establish physical USB identity.

## External collection

The collector makes no model calls. It verifies local inputs, uses strict SSH host-key checking and a shared session, creates an exclusive owned `/tmp` directory, self-tests its remote watchdog, verifies transferred bytes, and runs the reporter with a bound. Directory cleanup failures are surfaced separately. Interactive SSH needs both a controlling terminal and foreground process-group ownership; restoring terminal modes and cleaning descendants were explicit regression cases.

The ssh option set is hermetic: ambient configuration is ignored (`-F /dev/null`), the system-wide known-hosts file is disabled, and agent/forwarding/proxy settings are pinned off, so the connection cannot be altered by a `~/.ssh/config` stanza. Every multiplexed command first asks the control master — through `ssh -O check` — whether it is still alive; a lost master becomes an explicit `control_master_lost` failure instead of a silent fallback to a fresh direct connection that would need interactive authentication. Master shutdown is **verified** with a follow-up `-O check`, never inferred from the absence of an error. `SIGHUP` and `SIGTERM` reach the same orderly teardown as Ctrl-C in both modes.

Remote state is recorded rather than assumed. Ownership of the remote directory is a tri-state — `none`, `unknown`, `owned` — and appears in every attempt record; `unknown` means the `mkdir` was sent but never acknowledged, so a directory *may* exist. A directory is removed only when ownership was acknowledged, and the removal must print its own marker. A cleanup that cannot be confirmed adds a `teardown` record to the append-only outcome log and an entry to `pending_cleanup.jsonl`, which names the exact path for a later read-only reconciliation. Nothing deletes an uncertain directory automatically, and no process is killed broadly: the only last-resort signal goes to the PID OpenSSH itself reported.

Device output is drained under explicit byte ceilings while it is received, and the parser enforces a size and nesting budget, so an authenticated-but-hostile endpoint cannot exhaust host memory or stop the loop with a decoder failure. Untrusted device text is bounded and stripped of control bytes by a single sanitiser before it can reach a terminal or an outcome record, so it can neither spoof terminal lines nor fill the outcome log. Provisioning gates are hard stops: the required-tool probe and the watchdog self-test must each present unambiguous positive evidence. The gates that can stop before any remote state exists are the known-answer test and the tool probe; the watchdog self-test, the transfer and the control execution run **after** the exclusive directory has been created, and their failures trigger the ownership-scoped cleanup described above.

Classification is contained. A device reply the collector cannot tokenise, an unbounded numeric marker, a missing `ssh` binary, an unreadable artifact or a damaged `state.json` all become recorded, classified outcomes; a damaged `state.json` is quarantined rather than silently reset. A failure to write evidence is reported through `persistence_error` and a non-zero exit, never assumed to have succeeded.

Local history is bounded by record count and by byte budget, files are created `0600` and directories `0700`, writes are `fsync`ed, a torn trailing line is terminated before the next append, and `prune` takes the same lock as collection.

Raw CPU counters require two readings. Idle includes iowait; guest time is not added twice. First readings, resets and detected reboots have explicit states; a counter reset is judged per field except the idle group, which is judged as a sum, because a tickless kernel can reclassify time between `idle` and `iowait`. Reboot detection prefers the **explicit boot identity** — `host.boot_time`, read from `/proc/stat`'s `btime` — and falls back to a documented heuristic (uptime moving backwards, then a wall-clock estimate of the boot instant) only when a sample does not carry one. The previous sample is located through recorded state, not by filename order, and must carry the same source.

Persisted monotonic values should not be compared across a collector-host reboot or across hosts. Samples from different devices or mock/live sources must use separate data directories.

## Operational boundaries

Three consecutive collection failures open a local incident; two successes resolve it. SSH failure does not establish a router outage. A pinned host-key or artifact-gate failure is a **hard stop**: the loop ends instead of retrying an endpoint whose identity is not established. Retention limits record counts. The loop is foreground only, with no scheduled service, notifications or automatic remediation, and it refuses an interval that is non-finite, below one second or at/above `ControlPersist`. Password authentication is the current transport setting; public-key authentication is explicitly disabled, so unattended use needs an implementation change and not merely a key in `authorized_keys`.

Two successful hardware samples establish the measured path, not long-term reliability, safe failure handling under every real network condition, AP/NAS operation or a security-hardened network. Heavy analysis remains a separate external-host concern.
