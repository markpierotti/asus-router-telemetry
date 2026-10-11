# Foreground telemetry collector

Python standard-library collector adapted from the RT-AX56U bench implementation, version 1.2.0. Validates `router_health/2`, derives CPU utilisation from successive counters, retains timestamped samples and outcomes, and tracks collection incidents. Healthy collection makes zero model calls.

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

Retention defaults to 1440 sample files and 5000 outcomes. `status`, `prune`, `validate FILE` and `selftest` are available. Exit codes: 0 success, 1 collection failure, 3 overlap lock, 5 cleanup failure after successful collection, 130 interrupted loop. Cleanup failure is reported separately while preserving an original collection failure.

## Validation boundary

The bench collector completed two real SSH collections and produced independently cross-checked CPU utilisation. The portable copy replaces private defaults with operator configuration; its offline tests run separately. See [hardware results](../docs/collector-hardware-test.md) and [validation](../docs/validation.md). Tests cover controlling-PTY input and terminal restoration, descendant cleanup, local execution of the remote watchdog strings, fake-SSH transport, incidents, retention and schema checks. They are not an exhaustive audit or proof of long-term reliability.
