# ASUS Router Telemetry

[![Offline tests](https://github.com/markpierotti/asus-router-telemetry/actions/workflows/offline-tests.yml/badge.svg?branch=main)](https://github.com/markpierotti/asus-router-telemetry/actions/workflows/offline-tests.yml)

**Custom ARM telemetry on stock firmware, with supervised hardware tests for collection, link loss and recovery.**

A small C reporter runs temporarily on an ASUS RT-AX56U. A Python standard-library
collector on an external host verifies the transferred executable, validates its
JSON, derives CPU utilisation and records collection incidents. Routine collection
makes **zero model calls**.

The project combines firmware analysis, cross-compilation, process handling,
defensive parsing and physical-device validation. I directed the work with Hermes
and AI coding agents; [provenance](docs/provenance.md) explains the
human contribution, assisted implementation and independent verification.

## Part of a larger home lab

The collector runs on Slim alongside Pi-hole, Prometheus and Grafana. The broader
DNS, dashboards, alerting, backups and Hermes deployment are maintained in
[home-lab](https://github.com/markpierotti/home-lab), which consumes an immutable
collector revision. Reporter and collector development, router trials and the
[collector service contract](collector/examples/rtax-collector.service) stay here.

See the home-lab [operations runbook](https://github.com/markpierotti/home-lab/blob/main/docs/slim-operations.md)
and [roadmap](https://github.com/markpierotti/home-lab/blob/main/docs/home-lab-roadmap.md).
Historical deployment links in this repository redirect to that source.

## Measured results

Historical accepted bench pair: **collector 1.5.5 / reporter 1.0.5**, on stock
firmware 3.0.0.4.386_45898 and Linux 4.1.52 armv7l.

| Trial | Observed result |
| --- | --- |
| Supervised healthy path | 14 successful samples: two one-shots, an 11-sample loop and a post-stop one-shot |
| Steady loop collection | 0.179–0.201 seconds per attempt; sample spacing 59.9962–60.0050 seconds |
| Host-side Ethernet dropout | 18 attempts: 12 successful samples and six expected unreachable outcomes |
| Incident transitions | Opened after three consecutive failures; resolved after two recovery successes |
| Stop and restart | Ctrl-C exited 130; post-stop collection exited 0 |
| Cleanup after link loss | One stranded owned directory recorded, manually removed and checked absent through a separate SSH connection |

The steady timings describe an isolated, lightly loaded bench trial. They are not
measurements of the reporter's CPU overhead or a guarantee under network/NAS load.
Hardware acceptance applies to the named artifact and private bench adaptation;
this portable configuration was tested offline.

Read the [case study](docs/case-study.md), [healthy-path record](docs/healthy-acceptance-20261014.md)
and [dropout/recovery record](docs/dropout-recovery-20261014.md). The
[sanitized numeric summary](evidence/dropout-summary-20261014.json) exposes the
trial counts and conclusions without publishing target access details.

## How it works

```mermaid
flowchart LR
    R[Stock router: temporary C reporter] --> S[Bounded SSH collection]
    S --> V[JSON validation and CPU deltas]
    V --> H[Local history and status]
    V --> I[Collection incident state]
    S --> C[Ownership and cleanup evidence]
```

At session startup, the collector establishes one SSH ControlMaster and creates
an owned temporary directory. Before transferring the reporter, it runs a long-job
watchdog self-test on the target and requires a timer-fired marker, SIGKILL status
and elapsed-time evidence. It then uploads and hash-checks the reporter once for
that session. Each loop tick reuses the session and reporter; teardown removes the
owned directory and verifies control-master shutdown. Transport failures can end
the session, with a new provision required on reconnection.

The collector checks pinned host identity and artifact hashes and bounds execution
and received output. Cleanup uncertainty survives later successful
collection. An unreachable management address is reported as a collection failure;
it does not establish that the router itself is down.

Review-driven regression tests cover malformed and oversized data, numeric limits,
terminal password input, process interruption, lost SSH masters, retention and
cleanup ownership. [Design notes](docs/design.md) explain the contracts and limits;
the [validation record](docs/validation.md) preserves review and version history.

## Try offline

Requires a C compiler, Make and Python 3.11+ on macOS or another POSIX host.
From the repository root:

```sh
make test
./out/router_health --pretty
demo_dir="$(mktemp -d /tmp/router-telemetry-demo.XXXXXX)"
python3 collector/collector.py collect --once --transport mock --data-dir "$demo_dir"
python3 collector/collector.py status --transport mock --data-dir "$demo_dir"
```

Keep the printed mock results in that disposable directory for inspection. The test
suite and mock transport contact no router. Native macOS checks exercise parsing,
JSON and process handling; actual Linux `/proc` telemetry requires Linux. Run the
suite serially; the [collector instructions](collector/README.md) explain terminal
requirements and environment-dependent test limitations.

<details>
<summary>Example: excerpt from an offline mock sample</summary>

This excerpt was generated with the mock command above. It omits fields and is not
itself a complete schema-valid sample. On the first sample, CPU utilisation is
unknown because there are no previous counters; the aggregate is `null`.

```json
{
  "schema": "rtax_telemetry.sample/1",
  "collector_version": "1.6.2",
  "outcome": "success",
  "derived": {
    "cpu_utilisation": {
      "state": "first",
      "aggregate": null
    }
  }
}
```

</details>

## Build and live use

`sh build-arm.sh` requires Docker with linux/amd64 support. The base image is pinned
by digest, but apt packages are not pinned, so future bit-for-bit rebuilds are not
guaranteed. Build records and QEMU checks are distinct from hardware acceptance of
the historical executable.

For live use, follow the [collector configuration instructions](collector/README.md)
and supply your own target, trusted host keys and artifact hashes. No live target
is configured by default. Collection uploads and executes a temporary userspace
program; it does not flash firmware or install persistent software on the router.
Unattended setup separately installs persistent host services and authorizes a
router public key; those are explicit commissioning steps.

## Scope and publication

Collector **1.6.0** adds explicit key authentication. The accompanying Slim example
runs it under systemd, with external exporters and Grafana alert evaluation.
At the 17 October live check, the collector was still producing samples about four
hours after startup. This is an observation, not a controlled endurance trial.
Collector **1.6.2**, with the storage correction and a distinct loop hard-stop exit
code, was deployed on Slim with its matching service unit on 18 October 2026; see
the [deployment record](docs/slim-collector-162-20261018.md) and the
[review dispositions](docs/slim-review-triage-20261017.md).
The earlier v0.1.0 release and its v1.5.5 bench acceptance remain unchanged.

Telegram alert delivery (firing and resolved) and the external heartbeat were
tested live on 18 October 2026. Controlled reboot/power-loss recovery, a backup
restore rehearsal, a real internet or model-provider outage, USB NAS performance and
whole-network visibility remain unvalidated. ASUS AP mode is in use; segmentation,
Suricata and Project Limbo remain planned. The Hermes operator is read-only: there
is no automatic remediation or SOC claim.

Credentials, actual host keys, private configuration, raw history, vendor firmware
and extracted proprietary binaries are excluded. Local agent worktrees are ignored.
Earlier hardware records retain their original version boundaries.

**License:** [MIT](LICENSE), copyright 2026 Mark Pierotti. Third-party tools and
vendor firmware are not distributed. See [SECURITY.md](SECURITY.md) for security
boundaries and reporting guidance. [Release versioning](docs/releases.md) separates
project milestones from collector and reporter versions.
