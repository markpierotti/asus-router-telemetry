# Validation record

Snapshot date: 2026-10-12. This document distinguishes prior recorded project evidence from tests that a clone can rerun. It is not a hardware execution certificate.

## Recorded prior evidence

| Item | Result | Limit |
| --- | --- | --- |
| Known-answer fixture | Expected outputs 7, 10, 22, 37, -1 matched recovered logic | Small, unoptimized, non-stripped ELF only |
| Independent disassembly | GNU objdump corroborated fixture instructions | Does not independently review all reasoning |
| Reporter self-test | Native Linux and ARM/QEMU passes recorded | Emulation is not the physical router |
| Mount escaping | Capacity recovered for a real tmpfs path containing a space | Offline Linux test |
| Live inventory | RT-AX56U HW1.0; firmware 3.0.0.4.386_45898-gfa90458; Linux 4.1.52 armv7l; four ARMv7 cores; VFP/NEON features | Reporter not yet executed in this snapshot |
| Temporary storage | `/tmp` explicitly mounted tmpfs, rw, without noexec | Execution still needs a real test |

The analysed firmware image was 386_52090. Its ELF ABI observations must not be silently attributed to the physical unit running 386_45898. Live tool discovery could not recover ELF headers because the required dump utilities were unavailable. GLIBC_2.26 strings do not alone identify the installed libc release.

## Artifact identity

The unmodified reporter source imported into this repository has SHA-256:

```
4a862423e6f66cb0f640161e2b2a26629bd4dbeedc4287b1528ecb018555fcbb
```

The earlier recorded ARM artifact (not distributed here) has SHA-256:

```
9cf10eb5266cebf14e527354cad80049dedee8a5c624cbceef1add76b8b26273
```

A new build is not required to have the same artifact hash: apt package versions remain unpinned. Record each build's toolchain and hash.

## Repeatable checks

- `make test`: compiler warnings, five embedded self-test groups, and host JSON smoke checks.
- `sh build-arm.sh`: native Linux checks, ARM ELF inspection, static-link check, ARM self-test under QEMU and ARM JSON parsing.

A genuine on-device milestone should record the exact artifact hash, transfer integrity, real exit status, output validation and cleanup outcome. Persistent deployment, USB disk behavior, AP integration and continuous monitoring remain unverified.

## Standalone repository preparation checks

Executed on 2026-10-12:

- macOS native build with `cc -O2 -Wall -Wextra -std=c11`: passed without emitted compiler warnings; five self-test groups passed; compact and pretty JSON passed the host smoke checks. Linux telemetry is not asserted on macOS.
- Linux amd64 build in the cached, digest-pinned GCC container with networking disabled: five self-test groups and JSON smoke checks passed, including positive MemTotal and nonempty filesystem collection.
- `build-arm.sh` shell syntax and the Python smoke check compilation passed. The new ARM build recipe has not been run end to end in this repository; the ARM emulation results above are prior project evidence.

No router connection or deployment occurred during repository preparation.
