# Validation record

Updated: 2026-10-13. Remediation sections retain their historical test boundaries; current hardware status is stated separately. Results distinguish private bench hardware work from this portable repository's offline checks.

## Current identities

Reporter source v1.0.4 (third remediation revision; identical in the bench project and this repository):

```
09d4b4c35798fe4502d6cf23e4d83bab74d3d0de6a6a62f212bfb2a2ba706329
```

ARM artifact rebuilt from that source with the recorded workflow (static, soft-float EABI5, ARMv7-A,
min kernel 3.2.0), **not distributed**. This exact v1.0.4 artifact is now hardware-accepted
with bench collector 1.5.4 for the supervised healthy-path and Ethernet dropout sequence:

```
17ade0d2e85ed2880cb6a38de018bcf3f3d76d3f9090eb29eda54c007fe66e36
```

The **historically hardware-accepted** v1.0.3 artifact (`3069b2bd…2ce0eb`) produced the 17
accepted samples recorded in the [collector hardware record](collector-hardware-test.md).
Earlier v1.0.3 and v1.0.2 artifacts remain private historical evidence, not files distributed
in this repository. The active bench artifact is the v1.0.4 digest above:

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

Collector base version: **1.5.4**. Its functionally aligned private bench copy passed
the supervised dropout/recovery sequence; this portable configuration was checked offline. [Import manifest](import-manifest.json) records the bench-file and portable-copy hashes;
only deliberate configuration differences (bench target, data dir, artifact/known_hosts defaults,
documentation addresses) remain.

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

## Third remediation revision (2026-10-12) — historical offline milestone

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

**Verification.** 252 offline collector tests pass (137 at the start of the third remediation;
**15** were added by the follow-up below and **23** by the second follow-up): master loss and shutdown verification, failed-provision
and teardown evidence, hostile 200 KiB diagnostics, the null-section contract, file modes, torn
lines, hard stops, signal-driven teardown in both modes, and the boot-identity rules. The reporter's edge suite grew to 25 assertions and passes on the native
build **and** on the ARM artifact under QEMU. `sh build-arm.sh` and the bench `build.sh` reproduce the
**identical** ARM digest `17ade0d2…66e36` (self-test 8/8 under QEMU). The 17 bench fixture assertions
and 5 comparator cases still pass against the new artifact.

**At this remediation milestone, no hardware test was run.** Its then-current collector
and reporter changes were offline-only. Subsequent healthy-path and dropout hardware
results are recorded below; those results do not retroactively hardware-test each
intermediate revision or every offline failure case.

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
The consequence — a master that will not exit is left running — is reported rather than
acted on. (`ControlPersist` is an *idle* timeout; it is not a deadline by which a stuck
or still-busy master is guaranteed to be gone, as an earlier wording here implied.)

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

## Second follow-up (offline)

A focused re-check of the follow-up commit (`5059031`, collector 1.5.1) confirmed its
improvements and reproduced two remaining gaps in the collector, plus one in the private
acceptance instructions. They were corrected offline in collector **1.5.2**. The reporter
source and the ARM artifact are unchanged, so nothing was rebuilt.

| Finding | Defect reproduced | Correction |
| --- | --- | --- |
| A | R1 was incomplete. The collector read the provisioning facts only in the classifier branches that kept their own exception. `TransportUnreachableError` — reachable after an acknowledged `mkdir` through the multiplexed-command classifier — recorded no path, ownership `none` and `cleanup_ok: true`, and queued nothing. So did the timeout, authentication, host-key, malformed/schema, quota and generic branches | The exception a failed `start()` raises is kept at the single point it enters the collector, before classification, and the facts are read from there. No branch can drop them, the outcome class is untouched, and a failure with no facts attached changes nothing |
| B | R2 was incomplete. `_exit_master` caught `BaseException` around `ssh -O exit`, so a cancellation during the master exit was swallowed: the run did not end 130, and with an absent socket it reported `shutdown-verified` | The cancellation is held for one bounded `-O check`, the verdict is recorded, the local resources are released, and it is re-raised. Two results name it (`…the -O exit command was interrupted`, `…the follow-up probe was interrupted`). Nothing is retried and no process is signalled |
| C | The private acceptance sequence named superseded versions, and its sample validator read `os.environ['d']` although the operator is told to set `d` as a plain shell variable (`KeyError: 'd'`) | Private document, corrected there: the directory is passed to Python as an argument, and the versions, commit and test count describe the final state |

**B has a consequence for A.** Once a cancellation can leave the transport's own failure
handling it must carry the provisioning facts with it, and something must read them: a
cancelled attempt is not classified, so no attempt record does. The collector therefore
lifts them into the `teardown` record and the pending-cleanup log. The same path closes an
older hole — a plain Ctrl-C during provisioning, after `mkdir`, whose removal then failed
used to leave the directory unnamed. And "teardown was interrupted" is recorded by stage:
an interrupt during the master exit that follows a *confirmed* removal queues no directory,
and a removal that had already failed keeps its own reason rather than being called
interrupted. Finally, an attempt that had *already been classified* when its own teardown
was cancelled is still recorded: the cancellation is held for that one local write and
then re-raised, where it used to take the attempt's outcome with it.

**Verification of the regressions themselves.**
`collector/tests/test_followup2_regressions.py` adds 23 tests: 17 regressions and 6 guards
(what the correction must *not* change — the outcome class, a live session's ownership, the
uninterrupted shutdown vocabulary). Against an isolated `git archive` snapshot of `5059031`
with the new tests and test doubles in place, **all 17 regressions fail** (15 failures, 2
errors) and all 6 guards pass; with the corrections all 23 pass. The working tree was never
stashed or reverted to obtain that. The transport is the real `SSHTransport` throughout:
driven by the fake `ssh` process for the link-loss case (the device acknowledges `mkdir`,
then every later command, the removal included, exits 255 with a network diagnostic) and
for three CLI cases that deliver a real `SIGINT` to a real process, and by a scripted
stand-in for the one network-facing call where a cancellation has to land at an exact
point.

**Not established.** How the bench's Python and OpenSSH deliver a signal inside a real
`ssh -O exit`, and what a real master does with a half-sent exit request, are not
reproduced here: this host has no live control master. The follow-up check after an
interrupted exit is one `-O check` with a 10 s bound, and its non-zero exit is read as
"gone" exactly as it is for an uninterrupted shutdown.

**Adjacent behaviour observed and deliberately left alone** (outside this correction, and
unchanged by it): in `--loop` mode a teardown failure is written twice, because both the
loop and the command close the collector — one event, two `teardown` records and two
pending-cleanup entries; and when a provision *fails* (is not cancelled) while the master
also cannot be confirmed stopped, the master's verdict is logged but not persisted, because
it is only lifted into the collector when an established session is torn down.

## Third follow-up (offline)

Collector **1.5.3** corrected four defects reproduced against 1.5.2, including the two noted
just above: a relative `--data-dir` persisted a relative sample pointer; a failed final
cleanup was recorded twice in `--loop` mode; an ordinary failed provision did not persist an
unconfirmed master verdict; and a cancelled first-ever provision reported a false
`EVIDENCE NOT PERSISTED`. `collector/tests/test_followup3_regressions.py` holds 11 tests
(7 regressions, 4 guards). It did not refresh the import manifest; the fourth follow-up did.

## Fourth follow-up (offline)

An independent review of the 1.5.3 commit (`c2176ba`) reproduced two gaps with network-free
probes and reported a test discrepancy. All three were worked offline in collector **1.5.4**.
The reporter source and the ARM artifact are unchanged, so nothing was rebuilt.

| Finding | Defect reproduced | Correction |
| --- | --- | --- |
| R1 | The 1.5.3 lookup still tried a legacy **relative** sample pointer against the current working directory *before* its owning store, and the collector kept a second verbatim-path fallback. A file at the same relative path under the directory the collector was started from — same transport, same host, so the source guard could not tell — was taken as the predecessor and compared against. `Store.last_sample()` additionally fell back to the newest file by name when the state-selected sample was missing | One resolver, in the store, and the working directory is never consulted: an absolute pointer is used verbatim; a relative one resolves to the same file name inside that store's own `samples/`. A missing sample is reported and never replaced — not by a file elsewhere and not by filename order. Nothing is rewritten |
| R2 | When an **established** session lost its link, the collection failed and so did the removal of its directory. The attempt recorded `cleanup_ok: false`, but only *provisioning* failures queued a directory. The next attempt reset the per-attempt cleanup error, and after a recovery the clean final close wrote no `teardown` record, so `pending_cleanup.jsonl` did not exist and `status` showed nothing to reconcile | The directory is queued, with its exact path and ownership, by the attempt that learns of it; once per event, however many code paths notice it. The `teardown` record stays a final-close record. A master-only uncertainty still queues nothing. A queue write that fails is reported with the directory's name and is not displaced by a later failure; one cut short by a cancellation is retried at the close |
| PTY | Two controlling-terminal tests failed for the reviewer (`exc=RemoteTimeout;restored=True`) on two interpreters while passing elsewhere on the same machine | Not a transport defect and not a harness race — see below. The assertions are unchanged; a failure now states the environment restriction that caused it |

**The terminal-test discrepancy is an environment restriction.** Under the reviewer's Seatbelt
sandbox a process that has acquired a controlling terminal may still not open it for writing:
`open("/dev/tty", O_RDWR)` returns `EPERM`, while the read-only open, `TIOCSCTTY` and
`tcsetpgrp` on an already-open descriptor succeed. The transport performs exactly that
read-write open, both to decide that a terminal is usable and to hand its foreground to the
child, as ssh's own prompt does. Reproduced on the unmodified 1.5.3 tree with
`codex sandbox -P :workspace`: the same two failures with the same two signatures, on Python
3.14.7 and on 3.13.1; outside the sandbox both interpreters pass all tests in that file. The
harness's synchronisation was examined separately and no race was found: 40 of 40 unmodified
runs passed, and the read succeeds whether the input is queued a second before any reader
exists or injected a second after the reader blocks. The two tests are **not** skipped in a
restricted runner — there the proof was not obtained — and their failure message begins
`ENVIRONMENT RESTRICTION` with the errno the runner returned; a third test checks in any
runner that the transport's own terminal detection agrees with what the runner permits.

**A latent ordering window was found and deliberately not changed.** The transport spawns the
ssh child and *then* hands it the terminal's foreground. A child that touched the terminal
inside that gap would be stopped and never continued. Delaying the handover by 0.5 s under
fault injection reproduces a timeout every time; with no injected delay the gap measured
0.15–0.26 ms against roughly 6 ms before the test's stand-in first touches the terminal, and
a real ssh must complete a TCP connection and a key exchange before it prompts. It was never
observed without the injected delay, it is not what the reviewer saw, and closing it would
change the password-prompt path that was exercised on hardware. It is recorded for review
rather than patched.

**Verification of the regressions themselves.** `collector/tests/test_followup4_regressions.py`
adds 28 tests: 18 regressions and 10 guards. Against an isolated `git archive` snapshot of
`c2176ba` with only the new test file and the test stand-in's link-loss knob placed in it,
**all 18 regressions fail** (by assertion, no errors) and all 10 guards pass; with the
corrections all 28 pass. The working tree was never stashed or reverted to obtain that. One
of them rehearses the link-loss sequence on the real `SSHTransport` against the fake `ssh`:
a healthy attempt, the link pulled under the established session, three failures and an
incident, recovery, resolution and a clean close — with the lost directory queued exactly
once and its removal never retried.

**Historical offline-review limit.** The fake `ssh` reports a lost link as an immediately
broken channel. At that review, real lost-link timing/classification were unmeasured.
The later 1.5.4 cable-loss trial observed one first-failure duration of 22.67 seconds
and an `unreachable` classification; it does not determine behaviour under all faults. An absolute sample pointer is honoured verbatim, including
one that names a file outside its data directory; it is not re-anchored. A cancellation that
lands inside a local evidence write can still cost that attempt's own record, in which case
the directory is queued at the final close.

## Prior investigation

REA/Ghidra recovered a known-answer fixture whose outputs were 7, 10, 22, 37, -1; GNU objdump corroborated instructions. This covered a small, unoptimized, non-stripped ELF. The downloaded ASUS image was 386_52090; the physical router runs 386_45898. Image ABI observations cannot silently establish the live unit's ABI. Inventory identifies Linux 4.1.52 armv7l, four ARMv7 cores and approximately 498 MiB memory. GLIBC symbol strings alone do not identify an installed libc release.

## Hardware results

The [reporter hardware record](hardware-test.md) preserves the first schema-1 pass and the subsequent schema-2 comparison: 26 passed, zero failed, ten separate-sample comparisons inconclusive. The storage placeholder regression is resolved for the no-disk capture.

The [collector hardware record](collector-hardware-test.md) separates each accepted
revision. Collector 1.2.0 / reporter 1.0.2 produced two samples with independently
recalculated 1.62% utilisation. Collector 1.4.0 / reporter 1.0.3 produced 17 accepted
samples across two one-shots, a 14-sample foreground loop and a post-stop one-shot.
Collector 1.5.2 / reporter 1.0.4 later completed healthy-path acceptance.

The latest [dropout/recovery record](dropout-recovery.md), collector **1.5.4** / reporter
**1.0.4**, contains 16 attempts, 10 successes and six expected unreachable outcomes.
Incident thresholds, nine CPU counter comparisons, exit-130 shutdown, exit-0 post-stop
collection and manual reconciliation of one stranded owned directory were verified.
The [curated summary](../evidence/dropout-summary.json) publishes numeric results only.
Raw evidence remains private. Unattended authentication, multi-hour operation, other
failure conditions, reboot persistence, USB/NAS and AP operation remain unvalidated.

## Repeatable repository checks

- `make test-reporter`: native compile, **eight** embedded groups, compact/pretty JSON, synthetic `/proc` fixtures, and 25 reporter edge assertions (CPU truncation stays valid JSON; invalid-byte mount names stay valid UTF-8; an overlong `/proc/mounts` field is skipped and counted; a hostile BogoMIPS is escaped; `\000`/`\777` stay literal; `boot_time` is present/null correctly; a failed stdout write exits non-zero). macOS cannot validate real Linux `/proc` collection; fixture statvfs capacity comes from the executing host.
- `make test-collector`: offline schema, CPU derivation, incidents, retention, fake SSH, controlling-PTY input, watchdog and process cleanup, plus all six regression suites (`test_fixes.py`, `test_remediation.py` and `test_followup_regressions.py` through `test_followup4_regressions.py`). No router connection. Run it serially, and from a runner that may open its controlling terminal for writing (see the fourth follow-up above).
- `make test`: both targets.
- `sh build-arm.sh`: native Linux and ARM/QEMU checks plus static-link inspection, with toolchain hashes recorded.

## Portable-copy preparation

Executed for the portable repository update:

- `make test` passed on macOS: eight embedded reporter groups, compact/pretty JSON, the synthetic `/proc` fixture assertions, 25 reporter edge assertions, and **252 collector tests (251 passed, one skipped)**. The controlling-PTY input tests passed with local terminal access.
- The third remediation revision was verified end to end on this host: the ARM artifact built with `sh build-arm.sh` passed its embedded groups (8/8) and the reporter edge suite (25/25) under QEMU, and reported `boot_time` and the `limits` block.
- The bench `build.sh` independently reproduced the **same** ARM digest.
- Packaged mock CLI, foreground-loop shutdown (exit 130 and lock release) and scenario demo passed.
- Unconfigured live collection refused before invoking SSH. Shell syntax and import-manifest hashes were checked; no private target defaults or raw collection history were packaged.
- For collector 1.5.4 the collector suite was run once, serially, in each tree: **292 tests (291 passed, one skipped)** on Python 3.14.7 for this copy and on Python 3.13.1 for the bench copy. The skipped test needs the *test process itself* to have a controlling terminal, which the runner used did not provide; the controlling-PTY input tests create their own and passed.

No device requests were made during this documentation closeout. The latest private
bench hardware milestone is collector 1.5.4 / reporter 1.0.4. Collector 1.5.3 remains
an offline intermediate revision; the portable configuration is not separately
hardware-accepted. No code changed for this closeout, so completed offline suites
were not rerun. New documentation links, curated JSON, privacy boundaries and
unchanged source/private evidence were checked.
