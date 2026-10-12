# Foreground telemetry collector

Python standard-library collector adapted from the RT-AX56U bench implementation, version 1.4.0. Validates `router_health/2`, derives CPU utilisation from successive counters, retains timestamped samples and outcomes, and tracks collection incidents. Healthy collection makes zero model calls.

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

Stop with Ctrl-C. The password goes directly to SSH via the terminal. Each session provisions a temporary directory under the router's `/tmp`, verifies the transferred binary, self-tests a watchdog and removes the owned directory at teardown. These are temporary writes and process execution, not a read-only remote operation. It makes no firmware, NVRAM, package or network-configuration changes. Continuous hardware operation and unattended authentication remain future validation work. The current transport explicitly selects password authentication; key authentication requires a separate implementation change.

## Interpretation

CPU utilisation uses `100 * (delta_total - delta_idle) / delta_total`, where idle includes iowait. Guest counters are not added again. First samples, counter resets, detected reboots and zero counter deltas have explicit states rather than a fabricated zero percentage. Load average is not CPU utilisation. Local monotonic intervals persisted across a collector-host reboot are not directly comparable; device reboot detection uses falling router uptime and is heuristic.

Three failed attempts open a collection incident; two successes resolve it. An unreachable SSH address does not establish a device outage. Incidents and history remain local. No external notifications, automatic remediation or model calls are implemented.

Retention is bounded in two dimensions: `retain_samples` (default 1440) and `retain_outcomes` (default 5000) count records, and `retain_sample_bytes` / `retain_outcome_bytes` cap the bytes kept on disk. A single sample larger than `max_sample_bytes` is refused with a classified `storage_error`, never silently truncated. `status`, `prune`, `validate FILE` and `selftest` are available. `prune` takes the same data-directory lock as collection, so it cannot race a live collector (exit 3 on conflict). Exit codes: 0 success, 1 collection failure, 3 overlap lock, 5 cleanup failure after successful collection, 130 interrupted loop. Cleanup failure is reported separately while preserving an original collection failure.

## Containment and outcome classes

Device output is drained incrementally under explicit byte ceilings (`max_stdout_bytes`, default 1 MiB; `max_stderr_bytes`, default 256 KiB) enforced *while receiving*, before anything is fully buffered, decoded or parsed; a stream that exceeds its ceiling terminates and reaps the child group and is classified `output_overflow`. The parser also refuses a document larger than 1 MiB and one nested beyond 64 levels (a string- and escape-aware scan), and converts `RecursionError` into a normal `malformed_data` outcome, so a bad response records a failed attempt and the loop continues.

Provisioning gates are hard stops: the required-tool probe must exit 0 and return exactly one well-formed marker showing no missing tools, and the watchdog self-test must show the watchdog's own SIGKILL status, positive evidence its timer fired, and an elapsed time within tolerance of the bound — a crash such as SIGSEGV (139), a bare status 137, or a printed success line is not accepted. All of these stop provisioning before any remote directory is created or anything is executed.

Full outcome taxonomy: `success` · `malformed_data` · `schema_invalid` · `timeout` · `reporter_failure` · `ssh_auth_failure` · `ssh_hostkey_mismatch` · `unreachable` · `artifact_mismatch` · `transport_error` · `output_overflow` · `storage_error`.

Numeric input is contained too. Numbers are checked against the range each reporter field can actually take (64-bit counters, uptime below 1e12 s, load below 1e9), and the predicates are safe for arbitrary-size JSON integers — a value such as `1e400`-scale integer is rejected as `schema_invalid`, never allowed to raise. A numeric literal whose exponent overflows to a non-finite double (`1e999` → `inf`), even in an unknown/extra field, is rejected at parse as `malformed_data`. Stored samples, outcomes and state are serialised as strict JSON (`allow_nan=False`), so a non-standard `Infinity`/`NaN` token can never be written. A rejected response is a classified failed attempt, and the same collector still accepts the next good response.

The transferred artifact is written to the child over a non-blocking pipe: because selector readiness does not guarantee a whole chunk fits, a slow or partial reader cannot make the write overrun the deadline. Partial writes, backpressure, broken pipes and empty input are all handled while stdout/stderr are drained and the deadline is checked.

## Validation boundary

The bench collector completed two real SSH collections and produced independently cross-checked CPU utilisation. The portable copy replaces private defaults with operator configuration; its offline tests run separately. See [hardware results](../docs/collector-hardware-test.md) and [validation](../docs/validation.md). Tests cover controlling-PTY input and terminal restoration, descendant cleanup, local execution of the remote watchdog strings, fake-SSH transport, incidents, retention and schema checks. They are not an exhaustive audit or proof of long-term reliability.
