# First execution on the physical router

Date: 2026-10-12 (Australia/Brisbane).

**Result: the custom C reporter executed on an ASUS RT-AX56U running stock firmware and produced valid JSON.** No firmware replacement was required.

## Identity

- Physical unit: RT-AX56U HW1.0.
- Installed firmware previously inventoried: 3.0.0.4.386_45898-gfa90458.
- Reporter-observed running kernel/architecture: Linux 4.1.52, armv7l.
- Reporter output: four CPU cores and 510456 KiB total memory.

## Recorded checks

| Check | Result | Evidence boundary |
| --- | --- | --- |
| OpenSSL SHA-256 known-answer test | Passed | User-supplied terminal transcript |
| Host and transferred artifact hashes | Matched recorded digest | Transcript; saved scope identifies expected artifact |
| Watchdog self-test | Long job killed, status 137 | Transcript; not proof of every interruption path |
| Control executable from temporary storage | Exit 0, control-exec-ok | Saved control output and transcript |
| Reporter | Exit 0 | Saved stderr: router_health_exit=0 |
| Output | Valid JSON; schema router_health/1; kernel 4.1.52; arch armv7l | Saved JSON re-parsed during repository update |
| Host artifact after run | Same hash | Transcript |
| Temporary directory removal | Reported verified; final status 0 | Transcript; no separate post-run SSH check performed |

Reporter artifact SHA-256:

```
9cf10eb5266cebf14e527354cad80049dedee8a5c624cbceef1add76b8b26273
```

Bench launcher SHA-256:

```
6a842c98a467e13bdfdc27ef4b6fb14225d00b065fd8b0d8d5cb324336c02c1e
```

Local evidence bundle identifier: `RT-AX56U_TEST_EVIDENCE_20261012T081944`. Raw bench evidence and device access material are retained outside this repository.

## A useful finding, not a hidden success claim

The output lists `/tmp/mnt` twice in `usb_storage`. Both entries are `tmpfs` placeholders; no USB disk was connected. The reporter's storage heuristic currently includes these candidates and does not prove physical USB presence. Filtering this case and testing with a real disk are future improvements.

The earlier failed control test returned `bb: applet not found`: copying BusyBox under the basename `bb` caused its command dispatcher to reject that name. Preserving the basename `busybox` fixed the control test. The first failed attempt did not execute the custom reporter and cleaned up its directory according to its transcript.

## What this establishes

A hash-identified custom executable can run as a temporary, single-shot userspace program on this unit's installed stock firmware and return parseable telemetry.

It does not establish persistence, monitoring reliability, performance overhead, USB NAS behavior, AP-mode behavior, accurate interpretation of every counter, custom-firmware bootability or security hardening of the device.
