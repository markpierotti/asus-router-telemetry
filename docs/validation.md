# Validation record

Snapshot date: 2026-10-12. Results distinguish private bench hardware work from this portable repository's offline checks.

## Current identities

Reporter source v1.0.4 (third remediation revision; identical in the bench project and this repository):

```
09d4b4c35798fe4502d6cf23e4d83bab74d3d0de6a6a62f212bfb2a2ba706329
```

ARM artifact rebuilt from that source with the recorded workflow (static, soft-float EABI5, ARMv7-A,
min kernel 3.2.0), **not distributed** and **OFFLINE-VALIDATED ONLY** — no version of the v1.0.4
reporter has ever run on the unit:

```
17ade0d2e85ed2880cb6a38de018bcf3f3d76d3f9090eb29eda54c007fe66e36
```

The **hardware-accepted** artifact is v1.0.3 (`3069b2bd…2ce0eb`): it is the one that produced the 17
accepted samples recorded in the [collector hardware record](collector-hardware-test.md). It remains
preserved in `out/router_health_arm.v1.0.3` (and as `out/router_health` in the bench project until the
next hardware acceptance replaces it). The older hardware-tested v1.0.2 artifact is also preserved:

```
3069b2bd3290ab3bb1db2d49b60e7e7c6a8e5090e4735537980c65926d2ce0eb   (reporter v1.0.3 — HARDWARE-ACCEPTED)
b15f6117a31eb8ce3a70d013ff6d3b1abe5e2140297cc2df03c8690abe8b0590   (reporter v1.0.2 — hardware-tested)
```

> **Correction.** Earlier revisions of this page described `3069b2bd…` as "not hardware-validated".
> That was wrong and contradicted `collector-hardware-test.md`, which records 17 accepted samples with
> exactly that digest. `3069b2bd…` **is** the hardware-accepted artifact.
>
> **The portable `build-arm.sh` and the bench `build.sh` now reproduce the IDENTICAL ELF digest** for
> v1.0.4, so the two recipes agree bit for bit. apt packages remain unpinned, so a future rebuild on
> updated packages may differ; the recorded package versions are the audit trail.

Collector base version: **1.5.1** (offline-validated only). [Import manifest](import-manifest.json)
records the bench-file and portable-copy hashes; only deliberate configuration differences (bench
target, data dir, artifact/known_hosts defaults, documentation addresses) remain.

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

## Third remediation revision (2026-10-12) — OFFLINE-VALIDATED ONLY

An independent **static** review (Claude Code, `claude-opus-5-5`, max effort) was then validated by a
second agent, which independently reproduced five of its findings. Nine findings (F1–F9) were raised;
this revision addresses them, and the reporter source moved to **v1.0.4** so the artifact was rebuilt.
Full mapping, including what was **deferred** and why:
`~/rea-worker/reports/RT-AX56U_REMEDIATION3_20261012.md`.

| # | Finding | Resolution |
| --- | --- | --- |
| F1 | A multiplexed command had no control-master liveness gate, so a lost master could fall back to a fresh direct connection needing interactive auth (`transport.py`) | `ssh -O check` before every multiplexed command (and before teardown); `BatchMode=yes` + `ControlMaster=no` on those clients; a dedicated `control_master_lost` outcome; the loop refuses an interval at/above `ControlPersist` |
| F2 | Remote state under-recorded: a failed provision persisted `cleanup_ok: true`, a lost `mkdir` reply recorded nothing, a final-teardown failure could vanish from history (`transport.py`, `core.py`) | Ownership is a recorded tri-state (`none`/`unknown`/`owned`) set before `mkdir` is sent; the provisioning cleanup consequence is carried onto the exception and into the attempt; a `teardown` record is appended to `outcomes.jsonl`; unconfirmed cleanup is written to `pending_cleanup.jsonl` naming the exact path — with **no** automatic deletion |
| F3 | Device output could raise past the classifier: `IndexError` on a control-byte reply, `ValueError` on a >4300-digit marker; `OSError`/`TypeError` from the store, a missing `ssh`, a wrong-shaped `state.json` (`transport.py`, `core.py`) | Tokenisation by `split()` with an empty-token guard; the numeric marker is a bounded, lookahead-anchored match; `state.json` of the wrong shape is quarantined; `OSError` becomes `storage_error`; a final catch-all records `internal_error` and still logs the attempt |
| F4 | Unsanitised, effectively unbounded device text reached the terminal and local history (`transport.py`, `schema.py`) | One sanitiser (`errors.sanitize_detail`) bounds every `detail` to printable ASCII, one line, ~500 chars; schema diagnostics render values and key names through `reprlib`; a multiplexed `255` is classified into transport classes only, so a device cannot select `ssh_hostkey_mismatch`/`ssh_auth_failure` |
| F5 | Only SIGINT ran teardown; `-O exit` status ignored; SIGINT during handover read as a transport failure | SIGHUP and SIGTERM route to the orderly path in **both** modes; master shutdown is verified with a follow-up `-O check` and a last-resort signal only to the PID OpenSSH reported; SIGINT during handover raises `KeyboardInterrupt` |
| F6 | Reboot detection relied on uptime falling; the predecessor was chosen by filename order; reset detection could false-positive on idle/iowait reclassification (`cpu.py`, `core.py`) | Explicit boot identity (`host.boot_time` from `/proc/stat` `btime`) is the primary test; the predecessor comes from `state.last_valid_sample.path` and must match the source; the idle group is compared as a sum |
| F7 | A reporter document with `"cpu": null` / `"loadavg": null` was rejected, discarding the sample (`schema.py`) | Both may be null; an absent or wrongly-typed section is still a failure; a reporter-derived fixture now exercises the contract through `collector.py validate` |
| F8 | Reporter output-integrity gaps: unescaped BogoMIPS and `cpu_stat` keys, `sscanf` field shift on an overlong `/proc/mounts` field, `\000`/`>\377` octal escapes, silent caps, exit 0 on a failed stdout write, statvfs underflow on `f_bfree > f_blocks`, floored `used_percent` (`router_health.c`) | All escaped/detected/explicit: a hand tokenizer that SKIPS and COUNTS an overlong field, literal non-byte octal escapes, a `limits` block reporting every bound, non-zero exit on a failed write, clamped free figures, df-style ceiling rounding |
| F9 | SSH not isolated from ambient configuration (`transport.py`) | Hermetic options (`-F /dev/null`, `/dev/null` global known-hosts, agent/forwarding/proxy pinned off, `UpdateHostKeys=no`, `KnownHostsCommand=none`) plus `SSH_ASKPASS*` scrubbed from the child environment. The precondition was checked locally: **there is no `~/.ssh/config`**, so the change cannot break the working bench connection's algorithm negotiation |

Also addressed from the review's lower-severity list: `--interval` validation, hard stops that
actually stop the loop, provisioning-stage classification (`provisioning_failure`) distinct from a
reporter failure, signal statuses no longer read as "killed by the watchdog", rc-0-without-marker
refused for cleanup and control execution, a closed stderr no longer mapped to exit 0, `0600`/`0700`
evidence modes, `fsync` on evidence writes, torn-line repair, duplicate-JSON-key refusal, and an
`O_NOFOLLOW` lock. The stale legacy shell launchers are **deprecated by documentation, not edited**
(their bytes are historical evidence): `LEGACY_LAUNCHERS.md` in the bench reporter project records
the exact weaknesses.

**Verification.** 229 offline collector tests pass (137 at the start of the third remediation,
**15 of them** added by the follow-up below): master loss and shutdown verification, failed-provision
and teardown evidence, hostile 200 KiB diagnostics, the null-section contract, file modes, torn
lines, hard stops, signal-driven teardown in both modes, and the boot-identity rules. The reporter's edge suite grew to 25 assertions and passes on the native
build **and** on the ARM artifact under QEMU. `sh build-arm.sh` and the bench `build.sh` reproduce the
**identical** ARM digest `17ade0d2…66e36` (self-test 8/8 under QEMU). The 17 bench fixture assertions
and 5 comparator cases still pass against the new artifact.

**This revision ran no hardware test.** Neither collector 1.5.1 nor reporter 1.0.4 has been executed
on the unit; reconnection after a real transport failure, hardware interruption/cleanup paths,
unattended authentication, long runs, reboot persistence, USB/NAS and AP operation all remain
unvalidated.

## Follow-up remediation (offline)

A second independent static review reproduced four remaining defects at the
third-remediation commit. They were corrected offline; the full detail is in
`RT-AX56U_REMEDIATION3_20261012.md` (follow-up section).

| Finding | Defect reproduced | Correction |
| --- | --- | --- |
| R1 | Provisioning cleanup facts were folded in only for `ProvisioningError`; a `ControlMasterLost` or `ArtifactMismatch` ending the provisioning phase recorded `cleanup_ok: true` and lost the remote path | The transport attaches one `provision_facts` dict to whichever exception ends provisioning, and the collector folds it in for **every** class while preserving the outcome classification |
| R2 | A cancellation during **teardown** was not recorded: `_drop_session` caught only `Exception`, and `release_lock()` ran after `close()`, so a raise there leaked the lock | Cancellations during teardown are recorded as UNCERTAIN removal and re-raised; the teardown record is written from a `finally` and the lock is released from its own independent `finally`; stop handlers stay installed through teardown |
| R3 | Real macOS OpenSSH writes the `-O check` success diagnostic to **stderr**; the fake wrote stdout, so a stdout-only PID parse looked correct. Fixing the parse alone would have activated a fallback that signalled a cached PID with no identity check | The diagnostic is read from either stream; the PID-killing fallback was **removed** and the shutdown verdict became a distinct result (confirmed / still answering / probe incomplete / no master), with an unconfirmed shutdown persisted and reflected in the exit status |
| R4 | The acceptance preservation step selected 25 files while expecting 28 (the three `.lock` files were omitted), validated no root independently, and used a truncated JSON display as the check | The evidence set is defined explicitly, each root is validated on its own, the comparison is like-for-like against the recorded manifest, and the display was replaced by explicit assertions |

**R3 is a rule, not a preference.** No process is signalled from a PID reported by
`ssh`: a PID carries no executable, owner or start-identity evidence and can be reused.
The consequence — a master that will not exit lingers until its `ControlPersist` idle
timeout — is reported rather than acted on.

**Verification of the regressions themselves.** `collector/tests/test_followup_regressions.py`
adds 15 tests. With the package reverted to its committed state and the new tests left
in place, **all 15 fail** (10 failures, 5 errors); with the corrections in place all 15
pass. That is the evidence that they test the defects and not themselves. Two further
checks were confirmed: the acceptance preservation script passes on an untouched copy,
stops on a one-byte change to a historical sample, and stops when a historical root is
absent.

**Not established.** The `ssh` behaviours behind R3 are OpenSSH source semantics plus the
review's own reproduction against `/usr/bin/ssh`; this host has no live control master to
inspect, so the stderr channel was verified against the faithful double and the source,
not against a running master. See the report's remaining-uncertainties section.

## Prior investigation

REA/Ghidra recovered a known-answer fixture whose outputs were 7, 10, 22, 37, -1; GNU objdump corroborated instructions. This covered a small, unoptimized, non-stripped ELF. The downloaded ASUS image was 386_52090; the physical router runs 386_45898. Image ABI observations cannot silently establish the live unit's ABI. Inventory identifies Linux 4.1.52 armv7l, four ARMv7 cores and approximately 498 MiB memory. GLIBC symbol strings alone do not identify an installed libc release.

## Hardware results

The [reporter hardware record](hardware-test.md) preserves the first schema-1 pass and the subsequent schema-2 comparison: 26 passed, zero failed, ten separate-sample comparisons inconclusive. The storage placeholder regression is resolved for the no-disk capture.

The [collector hardware record](collector-hardware-test.md) records two milestones on the same unit. The earlier one (collector v1.2.0 / reporter v1.0.2) is two real collections with independently recalculated 1.62% CPU utilisation. The current one (collector v1.4.0 / reporter v1.0.3) is 17 accepted samples across four milestones — two one-shots (1.71%), a **14-sample supervised foreground loop** (`--loop --interval 60`, 14/14 success, a single password prompt, watchdog and artifact gates run once, clean Ctrl-C, no incident) and a post-stop collection (1.16%, exit 0) that proves the foreground lock was released. Every CPU delta was recomputed independently from the saved aggregate counters and matched. Raw samples and private identifiers remain outside this repository. Continuous operation beyond ~13 minutes, reconnection after a real transport failure, hardware interruption/cleanup-failure paths, unattended key authentication, reboot persistence, USB/NAS and AP operation remain unvalidated.

## Repeatable repository checks

- `make test-reporter`: native compile, **eight** embedded groups, compact/pretty JSON, synthetic `/proc` fixtures, and 25 reporter edge assertions (CPU truncation stays valid JSON; invalid-byte mount names stay valid UTF-8; an overlong `/proc/mounts` field is skipped and counted; a hostile BogoMIPS is escaped; `\000`/`\777` stay literal; `boot_time` is present/null correctly; a failed stdout write exits non-zero). macOS cannot validate real Linux `/proc` collection; fixture statvfs capacity comes from the executing host.
- `make test-collector`: offline schema, CPU derivation, incidents, retention, fake SSH, controlling-PTY input, watchdog and process cleanup, plus all three regression suites (`test_fixes.py`, `test_remediation.py`, `test_followup_regressions.py`). No router connection.
- `make test`: both targets.
- `sh build-arm.sh`: native Linux and ARM/QEMU checks plus static-link inspection, with toolchain hashes recorded.

## Portable-copy preparation

Executed for the portable repository update:

- `make test` passed on macOS: eight embedded reporter groups, compact/pretty JSON, the synthetic `/proc` fixture assertions, 25 reporter edge assertions, and **229 collector tests (228 passed, one skipped)**. The controlling-PTY input tests passed with local terminal access.
- The third remediation revision was verified end to end on this host: the ARM artifact built with `sh build-arm.sh` passed its embedded groups (8/8) and the reporter edge suite (25/25) under QEMU, and reported `boot_time` and the `limits` block.
- The bench `build.sh` independently reproduced the **same** ARM digest.
- Packaged mock CLI, foreground-loop shutdown (exit 130 and lock release) and scenario demo passed.
- Unconfigured live collection refused before invoking SSH. Shell syntax and import-manifest hashes were checked; no private target defaults or raw collection history were packaged.

No hardware test was performed as part of repository preparation. Continuous hardware operation, persistence, unattended authentication, USB/NAS and AP integration remain future work. Collector **1.5.1** and reporter **1.0.4** are **OFFLINE-VALIDATED ONLY**; the hardware-accepted pair remains collector 1.4.0 / reporter 1.0.3 with artifact `3069b2bd…2ce0eb`.
