# Validation record

Snapshot date: 2026-10-12. Results distinguish private bench hardware work from this portable repository's offline checks.

## Current identities

Reporter source v1.0.3 (remediation revision; imported into both the bench project and this repository):

```
73ff16a943c2dbd5a6be12cfbe57b72d1d03425d9a19544241832dd817fff795
```

ARM artifact rebuilt from that source with the recorded workflow (static, soft-float EABI5, ARMv7-A, min kernel 3.2.0), **not distributed** and **not hardware-validated**:

```
3069b2bd3290ab3bb1db2d49b60e7e7c6a8e5090e4735537980c65926d2ce0eb
```

The previous hardware-tested artifact is preserved for provenance only:

```
b15f6117a31eb8ce3a70d013ff6d3b1abe5e2140297cc2df03c8690abe8b0590   (reporter v1.0.2)
```

Collector base version: 1.4.0. [Import manifest](import-manifest.json) records the bench-file and portable-copy hashes; only deliberate configuration differences (bench target, data dir, artifact/known_hosts defaults, documentation addresses) remain. Apt packages remain unpinned, so rebuilt artifact hashes may differ.

## Remediation revision (2026-10-12)

An independent review (`report.md`, `artifacts/code-review.md`) reported two low-severity security findings (both requiring control of the authenticated router, not unauthenticated SSH) and six correctness findings. All were fixed in both copies, each with a regression test that drives the real mechanism:

| # | Defect (source location) | Fix |
| --- | --- | --- |
| S1 | Unbounded SSH stdout/stderr and full-document persistence (`transport.py` run_bounded / `store.py`) | Incremental draining with byte ceilings, per-write quota, byte-budget retention; `output_overflow` outcome |
| S2 | `RecursionError` from deeply nested JSON escaped the classifier (`schema.py`, `cli.py`) | Nesting budget + `RecursionError`→`MalformedData`; recorded as `malformed_data` |
| C1 | Required-tool gate failed open (`transport.py` `_provision`) | Require rc 0 and exactly one well-formed `TOOLS_OK`/`TOOLS_MISSING:` marker |
| C2 | Watchdog proof accepted any status ≥128 (`transport.py` `_watchdog_selftest`) | Require SIGKILL status, a timer-fired marker, and elapsed time within tolerance |
| C3 | `prune` bypassed the collection lock (`cli.py`, `store.py`) | `prune` takes the same lock; scoped release in `finally`; unique temp filenames |
| C4 | CPU truncation emitted malformed JSON (`router_health.c`) | Rejected append restores the buffer terminator; truncation emitted as `_truncated:true` |
| C5 | Invalid filesystem bytes produced non-UTF-8 output (`router_health.c`) | UTF-8-validating escape with a documented U+FFFD replacement policy |
| C6 | Schema accepted NaN/Infinity, booleans-as-numbers and out-of-range values (`schema.py`) | Reject non-standard constants; require finite, in-range numbers; reject booleans |

Verification: the reporter's edge cases (C4/C5) were reproduced and fixed at the C level and pass on both the native and the ARM build under QEMU; the collector findings (S1/S2/C1/C2/C3/C6) each have a behavioural test in `collector/tests/test_fixes.py`. The nested-JSON failure was reported on Python 3.11.16; only Python 3.13.1 was available here, so that interpreter-specific path is covered by the nesting-budget test rather than by reproducing 3.11's exact recursion limit.

## Second remediation revision (2026-10-12)

A follow-up independent review (Codex and Claude Code) raised three numeric-input defects and one transport timing defect. Reporter source is UNCHANGED (still v1.0.3, hash above), so no ARM artifact was rebuilt. All four were fixed in both copies, each with a regression test driving the real mechanism:

| # | Defect (source location) | Fix |
| --- | --- | --- |
| N1 | A huge integer for `host.uptime_seconds` / `loadavg.*` (`10**400`) made `_is_finite_num` call `math.isfinite` on an int and raise `OverflowError`, escaping the classifier (`schema.py`) | Only floats are passed to `math.isfinite`; every numeric field is range-bounded to what the reporter can emit (64-bit counters, uptime < 1e12 s, load < 1e9); oversized values are `schema_invalid`, not a crash |
| N2 | A huge CPU counter made `cpu.py` raise `OverflowError` at `100.0 * (dt - di) / dt` (`cpu.py`) | The ratio `(dt - di) / dt` is computed before scaling by 100, which is safe for arbitrary-size ints; `detect_reboot` avoids coercing a big int through a float operand |
| N3 | A numeric literal whose exponent overflows to `inf` (`1e999`) bypassed `parse_constant` in an extra field, and the sample was stored with the non-standard token `Infinity` (`schema.py`, `store.py`) | A `parse_float` hook rejects non-finite float literals at parse (`malformed_data`); samples/outcomes/state are serialised with `allow_nan=False` |
| T1 | `transport._drain_bounded` wrote up to 64 KiB to a blocking stdin pipe after selector readiness, so a slow/partial reader could overrun the deadline (`transport.py`) | The write end is set non-blocking; partial writes, `EAGAIN` (keep registered, keep draining, keep checking the deadline), broken pipes and empty input are all handled |

Verification: 137 tests pass on Python 3.11.16 and 3.13.1 (was 116); the partial-reader reproducer now returns within the deadline (0.2 s bound, ~0.2 s elapsed, previously ~2.0 s). The stored JSON, outcome classification and bad-input-then-good-input recovery are asserted. Both copies pass the full suite; `smoke_loop.sh` and `demo_offline.sh` still exit cleanly.

## Prior investigation

REA/Ghidra recovered a known-answer fixture whose outputs were 7, 10, 22, 37, -1; GNU objdump corroborated instructions. This covered a small, unoptimized, non-stripped ELF. The downloaded ASUS image was 386_52090; the physical router runs 386_45898. Image ABI observations cannot silently establish the live unit's ABI. Inventory identifies Linux 4.1.52 armv7l, four ARMv7 cores and approximately 498 MiB memory. GLIBC symbol strings alone do not identify an installed libc release.

## Hardware results

The [reporter hardware record](hardware-test.md) preserves the first schema-1 pass and the subsequent schema-2 comparison: 26 passed, zero failed, ten separate-sample comparisons inconclusive. The storage placeholder regression is resolved for the no-disk capture.

The [collector hardware record](collector-hardware-test.md) records two milestones on the same unit. The earlier one (collector v1.2.0 / reporter v1.0.2) is two real collections with independently recalculated 1.62% CPU utilisation. The current one (collector v1.4.0 / reporter v1.0.3) is 17 accepted samples across four milestones — two one-shots (1.71%), a **14-sample supervised foreground loop** (`--loop --interval 60`, 14/14 success, a single password prompt, watchdog and artifact gates run once, clean Ctrl-C, no incident) and a post-stop collection (1.16%, exit 0) that proves the foreground lock was released. Every CPU delta was recomputed independently from the saved aggregate counters and matched. Raw samples and private identifiers remain outside this repository. Continuous operation beyond ~13 minutes, reconnection after a real transport failure, hardware interruption/cleanup-failure paths, unattended key authentication, reboot persistence, USB/NAS and AP operation remain unvalidated.

## Repeatable repository checks

- `make test-reporter`: native compile, five embedded groups, compact/pretty JSON, synthetic `/proc` fixtures, and the reporter edge tests (CPU truncation stays valid JSON; invalid-byte mount names stay valid UTF-8). macOS cannot validate real Linux `/proc` collection; fixture statvfs capacity comes from the executing host.
- `make test-collector`: offline schema, CPU derivation, incidents, retention, fake SSH, controlling-PTY input, watchdog and process cleanup tests, plus the remediation regression suite. No router connection.
- `make test`: both targets.
- `sh build-arm.sh`: native Linux and ARM/QEMU checks plus static-link inspection, with toolchain hashes recorded. The rebuilt standalone ARM recipe was rerun for the remediation revision and its output is recorded above; historical ARM build/emulation evidence belongs to the bench artifact.

## Portable-copy preparation

Executed for the portable repository update:

- `make test` passed on macOS: five embedded reporter groups, compact/pretty JSON, 29 fixture assertions, the reporter edge tests, and 137 collector tests (136 passed, one skipped). The controlling-PTY input tests passed with local terminal access.
- The remediation revision was verified end to end on this host: the ARM artifact built with `sh build-arm.sh` passed its embedded groups, the fixture assertions and the reporter edge tests under QEMU.
- Packaged mock CLI, foreground-loop shutdown (exit 130 and lock release) and scenario demo passed.
- Unconfigured live collection refused before invoking SSH. Shell syntax and import-manifest hashes were checked; no private target defaults or raw collection history were packaged.

No hardware test was performed as part of repository preparation. Continuous hardware operation, persistence, unattended authentication, USB/NAS and AP integration remain future work. The rebuilt ARM artifact is **not** hardware-validated.
