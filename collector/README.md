# Foreground telemetry collector

Python standard-library collector adapted from the RT-AX56U bench implementation, version 1.5.1. Validates `router_health/2`, derives CPU utilisation from successive counters, retains timestamped samples and outcomes, and tracks collection incidents. Healthy collection makes zero model calls.

Requires macOS or a POSIX host with Python 3.11+ and OpenSSH for live use. It remains a foreground process; it creates no scheduled jobs, services or startup hooks.

## Try offline

From this directory:

```sh
python3 collector.py collect --once --transport mock --data-dir /tmp/router-telemetry-demo
python3 collector.py status --transport mock --data-dir /tmp/router-telemetry-demo
bash run_tests.sh
```

Use a separate data directory for mock scenarios and live measurements. Test fixture documents derive from router output and contain no hostname, address, login, host key or credential. Test passwords are synthetic strings.

## Configure your own device

Live collection is an explicit operation. No working router address, username, host keys or build artifact is distributed. The CLI refuses live collection without a configured host and username, and the transport requires a reviewed artifact digest. The example address is documentation-only.

Build the ARM reporter from the repository root with `sh build-arm.sh`, review its output and record its SHA-256. Establish the intended router's SSH host-key identity and retain the reviewed keys in a local `known_hosts` file. This project does not establish that trust automatically; `StrictHostKeyChecking=yes` then enforces the file. Record the file's SHA-256 too.

Copy `config.example.json` to a private `<data-dir>/config.json`. Set your host, SSH username, absolute artifact and known-hosts paths, and both recorded hashes. Never store passwords in configuration. A new ARM build may differ from the historical hardware-tested artifact; this configuration does not make it hardware-validated.

```sh
python3 collector.py collect --once --data-dir /absolute/path/to/private/data
python3 collector.py status --data-dir /absolute/path/to/private/data
```

After a successful one-shot acceptance test, a foreground loop is available:

```sh
python3 collector.py collect --loop --interval 60 --data-dir /absolute/path/to/private/data
```

Stop with Ctrl-C. The password goes directly to SSH via the terminal. Each session provisions a temporary directory under the router's `/tmp`, verifies the transferred binary, self-tests a watchdog and removes the owned directory at teardown. These are temporary writes and process execution, not a read-only remote operation. It makes no firmware, NVRAM, package or network-configuration changes. Continuous hardware operation and unattended authentication remain future validation work. The current transport explicitly selects password authentication and sets `PubkeyAuthentication=no`; a key in `authorized_keys` alone therefore does **not** enable unattended use — that needs an implementation change.

## Interpretation

CPU utilisation uses `100 * (delta_total - delta_idle) / delta_total`, where idle includes iowait. Guest counters are not added again. First samples, counter resets, detected reboots and zero counter deltas have explicit states rather than a fabricated zero percentage. Load average is not CPU utilisation. Local monotonic intervals persisted across a collector-host reboot are not directly comparable.

Device reboot detection prefers the **explicit boot identity**: the reporter reads `btime` from `/proc/stat` and emits `host.boot_time`, and two samples from different boots carry different values whatever the clocks did. Only when a sample does not carry one does it fall back to a documented heuristic (uptime moving backwards, then a wall-clock estimate of the boot instant shifting beyond a tolerance). The previous sample is located through recorded state (`state.last_valid_sample.path`), not by filename order, and must carry the same `source` (transport + host); a mismatch is reported and the new sample is treated as a first sample. A counter reset is judged per field except the idle group (`idle` + `iowait`), which is judged as a sum, because a tickless kernel can reclassify time between the two.

Three failed attempts open a collection incident; two successes resolve it. An unreachable SSH address does not establish a device outage. Incidents and history remain local. No external notifications, automatic remediation or model calls are implemented. A pinned host-key or artifact-gate failure is a **hard stop**: the loop stops rather than retrying an endpoint whose identity is not established.

Retention is bounded in two dimensions: `retain_samples` (default 1440) and `retain_outcomes` (default 5000) count records, and `retain_sample_bytes` / `retain_outcome_bytes` cap the bytes kept on disk. A single sample larger than `max_sample_bytes` is refused with a classified `storage_error`, never silently truncated. `status`, `prune`, `validate FILE` and `selftest` are available. `prune` takes the same data-directory lock as collection, so it cannot race a live collector (exit 3 on conflict).

Exit codes: **0** success, **1** collection failure or a hard stop, **2** an invalid option (a non-finite/too-small `--interval`, or one at/above `ControlPersist`), **3** overlap lock, **5** cleanup could not be confirmed after an otherwise successful collection, **6** the evidence could not be written, **130** interrupted loop. A cleanup failure and a persistence failure are each reported separately and never mask an original collection failure.

## Containment and outcome classes

Device output is drained incrementally under explicit byte ceilings (`max_stdout_bytes`, default 1 MiB; `max_stderr_bytes`, default 256 KiB) enforced *while receiving*, before anything is fully buffered, decoded or parsed; a stream that exceeds its ceiling terminates and reaps the child group and is classified `output_overflow`. The parser also refuses a document larger than 1 MiB, one nested beyond 64 levels (a string- and escape-aware scan) and one with a duplicated JSON key, and converts `RecursionError` into a normal `malformed_data` outcome, so a bad response records a failed attempt and the loop continues.

Untrusted device text is rendered through a single sanitiser (`errors.sanitize_detail`) before it reaches a terminal or is persisted: the result is printable ASCII, one line, and capped near 500 characters. Control bytes appear as `\xNN` escapes, so a hostile endpoint can neither inject terminal control sequences nor make one oversized failure evict earlier evidence from the bounded outcome log. A multiplexed command's `255` carries genuine network diagnostics but is classified into **transport** classes only — a device cannot choose `ssh_hostkey_mismatch` or `ssh_auth_failure` by printing those strings on its own stderr; those classes are decided at master establishment.

Provisioning gates are hard stops: the required-tool probe must exit 0 and return exactly one well-formed marker showing no missing tools, the watchdog self-test must show the watchdog's own SIGKILL status with positive evidence its timer fired and an elapsed time within tolerance of the bound, the control execution must print its marker, and the transfer's router-side SHA-256 must match. The known-answer test and the tool probe run before any remote state exists; the watchdog self-test, the artifact transfer and the control execution run **after** the exclusive directory has been created, so a failure there is followed by an ownership-scoped cleanup attempt.

Full outcome taxonomy: `success` · `malformed_data` · `schema_invalid` · `timeout` · `reporter_failure` · `provisioning_failure` · `control_master_lost` · `ssh_auth_failure` · `ssh_hostkey_mismatch` · `unreachable` · `artifact_mismatch` · `transport_error` · `output_overflow` · `storage_error` · `internal_error`.

`timeout` means a *reporter run* ended — either the remote watchdog's SIGKILL (`137`) or a local bound. A status of `129`/`130`/`143` (the remote script's own trap, SIGINT, SIGTERM) is a `reporter_failure` with a note that it is **not** proven to be the watchdog timer. A deadline expiry during *provisioning* is a `provisioning_failure` naming its stage, never a reporter timeout. `internal_error` is the final catch-all: a device response or a local fault the taxonomy did not name (a missing `ssh` binary, an unreadable artifact, a serialisation fault) is still recorded and classified rather than crashing.

Numeric input is contained too. Numbers are checked against the range each reporter field can actually take (64-bit counters, uptime below 1e12 s, load below 1e9), and the predicates are safe for arbitrary-size JSON integers — a value such as `1e400`-scale integer is rejected as `schema_invalid`, never allowed to raise. A numeric literal whose exponent overflows to a non-finite double (`1e999` → `inf`), even in an unknown/extra field, is rejected at parse as `malformed_data`. Stored samples, outcomes, state and pending-cleanup records are serialised as strict JSON (`allow_nan=False`), so a non-standard `Infinity`/`NaN` token can never be written. A rejected response is a classified failed attempt, and the same collector still accepts the next good response.

A reporter section that is **unavailable** is preserved, not discarded: the reporter emits `"cpu": null` and `"loadavg": null` when the corresponding `/proc` file cannot be read, and the collector accepts both, so one unreadable file does not throw away the whole sample. An absent section, or a section of the wrong type, is still a `schema_invalid` failure. A damaged `state.json` is quarantined beside the data directory instead of being silently reset.

The transferred artifact is written to the child over a non-blocking pipe: because selector readiness does not guarantee a whole chunk fits, a slow or partial reader cannot make the write overrun the deadline. Partial writes, backpressure, broken pipes and empty input are all handled while stdout/stderr are drained and the deadline is checked.

## Remote state, cleanup and evidence

Ownership of the remote directory is a tri-state recorded in every attempt (`remote_ownership`): `none` (provably not created), `unknown` (the `mkdir` was sent but never acknowledged, so a directory *may* exist) and `owned` (the `mkdir` was acknowledged). A directory is removed only when ownership is `owned`, and the removal must print its own positive marker — rc 0 alone is not accepted. `cleanup_ok` is `false` whenever removal is not confirmed, including when a *provisioning* failure could not reconcile its own directory.

A cleanup that cannot be confirmed is recorded durably in three places: the attempt record, a `teardown` record appended to `outcomes.jsonl`, and an entry in `pending_cleanup.jsonl` naming the exact path, how ownership was established and why. `status` shows the count of unconfirmed entries. **Nothing** deletes an uncertain directory, and no process is killed broadly — the only last-resort signal goes to the PID OpenSSH itself reported for the control master. If any of those writes fails, the run reports `EVIDENCE NOT PERSISTED` and exits non-zero rather than claiming a record was made.

## Transport lifecycle

Every multiplexed command is gated on `ssh -O check`; a lost control master becomes `control_master_lost` with no fallback to a fresh connection (`BatchMode=yes`, `ControlMaster=no`), so it can neither prompt for a password nor stall on a terminal read. Interactive authentication is handed the terminal **only** during master establishment. Ctrl-C during that handover is treated as an interruption, not a transport failure. Master shutdown is verified with a follow-up `-O check`, never inferred from the absence of an error. `SIGHUP` and `SIGTERM` reach the same orderly teardown as Ctrl-C in `--once` and `--loop` modes alike, and the data-directory lock is released in a `finally`.

The ssh option set is hermetic: `-F /dev/null` ignores `~/.ssh/config`, the system-wide known-hosts file is disabled, and agent forwarding, connection forwarding, `ProxyCommand`, `KnownHostsCommand` and `UpdateHostKeys` are pinned off, so ambient configuration cannot alter the connection. `SSH_ASKPASS*` is removed from the child environment, so a password can only come from the operator's terminal.

Evidence files are created `0600` and their directories `0700` regardless of the operator's umask; writes are `fsync`ed (file and directory); a torn trailing line in the append-only logs is terminated before the next record is appended; and the lock file is opened `O_NOFOLLOW`, so a symlink placed at the lock path is refused rather than followed and truncated.

## Validation boundary

The bench collector completed 17 accepted hardware samples (**collector v1.4.0 / reporter v1.0.3**, artifact `3069b2bd…2ce0eb`) with independently cross-checked CPU utilisation. Version 1.5.1 and reporter v1.0.4 are **offline-validated only**: no version of either has been run on the unit. See [hardware results](../docs/collector-hardware-test.md) and [validation](../docs/validation.md). Tests cover controlling-PTY input and terminal restoration, descendant cleanup, local execution of the remote watchdog strings, master loss and shutdown verification, failed-provision and teardown evidence, hostile diagnostics, the reporter-to-collector null contract, file modes and torn-line repair, fake-SSH transport, incidents, retention and schema checks. They are not an exhaustive audit or proof of long-term reliability.