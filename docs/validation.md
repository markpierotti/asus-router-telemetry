# Validation record

Snapshot date: 2026-10-12. Results distinguish private bench hardware work from this portable repository's offline checks.

## Current identities

Reporter source v1.0.2 (imported unchanged):

```
8e4ce0e2034886f6e8f1f97cfde76323f5b0276e7b377194134d1df726e4f016
```

Hardware-tested ARM artifact (not distributed):

```
b15f6117a31eb8ce3a70d013ff6d3b1abe5e2140297cc2df03c8690abe8b0590
```

Collector base version: 1.2.0. [Import manifest](import-manifest.json) records original bench-file hashes and portable-copy hashes. Paths, examples and default configuration are adapted; the bench project is untouched. Apt packages remain unpinned, so rebuilt artifact hashes may differ.

## Prior investigation

REA/Ghidra recovered a known-answer fixture whose outputs were 7, 10, 22, 37, -1; GNU objdump corroborated instructions. This covered a small, unoptimized, non-stripped ELF. The downloaded ASUS image was 386_52090; the physical router runs 386_45898. Image ABI observations cannot silently establish the live unit's ABI. Inventory identifies Linux 4.1.52 armv7l, four ARMv7 cores and approximately 498 MiB memory. GLIBC symbol strings alone do not identify an installed libc release.

## Hardware results

The [reporter hardware record](hardware-test.md) preserves the first schema-1 pass and the subsequent schema-2 comparison: 26 passed, zero failed, ten separate-sample comparisons inconclusive. The storage placeholder regression is resolved for the no-disk capture.

The [collector hardware record](collector-hardware-test.md) records two real collections, accepted samples, cleanup state and independently recalculated 1.62% CPU utilisation. The private bench collector suite ran 85 tests: 84 passed, one skipped. Real controlling-PTY input tests passed when terminal access was permitted; the restricted execution sandbox denied terminal access, so those checks were rerun with local PTY access.

## Repeatable repository checks

- `make test-reporter`: native compile, five embedded groups, compact/pretty JSON and synthetic `/proc` fixtures. macOS cannot validate real Linux `/proc` collection; fixture statvfs capacity comes from the executing host.
- `make test-collector`: offline schema, CPU derivation, incidents, retention, fake SSH, controlling-PTY input, watchdog and process cleanup tests. No router connection.
- `make test`: both targets.
- `sh build-arm.sh`: native Linux and ARM/QEMU checks plus static-link inspection, with toolchain hashes recorded. This updated standalone ARM recipe has not been rerun end to end; historical ARM build/emulation evidence belongs to the bench artifact.

## Portable-copy preparation

Executed for the portable repository update:

- `make test` passed on macOS: five embedded reporter groups, compact/pretty JSON, 29 fixture assertions, and 85 collector tests (84 passed, one skipped). The controlling-PTY input tests passed with local terminal access.
- Native Linux reporter compilation, embedded groups, JSON and the same fixture assertions passed in the cached digest-pinned GCC container with networking disabled and the repository mounted read-only.
- Packaged mock CLI, foreground-loop shutdown (exit 130 and lock release) and scenario demo passed.
- Unconfigured live collection refused before invoking SSH. Shell syntax and import-manifest hashes were checked; no private target defaults or raw collection history were packaged.

No hardware test was performed as part of repository preparation. Continuous hardware operation, persistence, unattended authentication, USB/NAS and AP integration remain future work.
