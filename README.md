# ASUS Router Telemetry

**Custom ARM telemetry on stock firmware — with hardware-tested outage detection and recovery.**

A C reporter and Python collector developed through firmware analysis, cross-compilation, adversarial testing and validation on a physical ASUS RT-AX56U. The reporter emits Linux system measurements as JSON; the external collector verifies execution, saves history and calculates CPU utilisation. Healthy collection makes zero model calls.

The latest supervised trial recorded **16 attempts: 10 successful samples and six expected link-loss failures**. The collector opened and resolved an incident at the configured thresholds, preserved uncertain cleanup through recovery, and stopped cleanly. Read the [project case study](docs/case-study.md) or the [dropout/recovery evidence](docs/dropout-recovery.md).

## Demonstrated on hardware

| Milestone | Recorded result |
| --- | --- |
| Reverse-engineering toolchain | REA/Ghidra checked against a known-answer fixture; GNU objdump corroborated instructions |
| Firmware and live inventory | Downloaded 386_52090 examined; physical unit runs 386_45898, Linux 4.1.52 armv7l |
| Custom reporter | Static ARM executable runs temporarily on stock firmware |
| Parser comparison | 26 checks passed, zero failed; 10 separate-sample comparisons remained inconclusive |
| SSH collector | One-shots plus a 14-sample supervised foreground loop accepted (17 samples), with artifact verification, watchdog proof and cleanup recorded |
| CPU utilisation | Independently recalculated per sample pair (1.71%, 1.16%); all 13 loop deltas reproduced |
| Independent review remediation | Two low-severity security findings (both needing an authenticated router) and six correctness findings fixed, each with a behavioural regression test |
| Follow-up review remediation | Numeric-input containment (huge/overflowing JSON numbers classified, not crashed), strict JSON storage, and non-blocking time-bounded artifact transfer fixed, each with a behavioural regression test |
| Third review remediation (offline) | Nine further findings (F1–F9) addressed: parser containment and bounded/sanitised diagnostics, the reporter-to-collector null contract, control-master liveness gating with no interactive fallback, recorded ownership/cleanup uncertainty, signal-driven teardown, explicit boot identity, and reporter output/format fixes — 73 new regressions |
| Follow-up review remediation (offline) | Four reproduced defects (R1–R4): provisioning cleanup facts preserved for every error class, cancellation-safe teardown with an independently-released lock, an honest master-shutdown verdict with **no PID signalling**, and a corrected acceptance preservation check — 15 new regressions that fail on the previous revision |
| Supervised Ethernet dropout/recovery, collector 1.5.4 / reporter 1.0.4 | 16 attempts; incident opened after three failures, resolved after two successes; exit 130 on Ctrl-C, post-stop exit 0; stranded directory reconciled separately |
| Multi-hour operation / unattended auth / AP / USB NAS / other failure modes | Not yet validated |

The [archived Hermes prompt](docs/next-phase-prompt.md) framed an earlier foreground trial. The [collector hardware record](docs/collector-hardware-test.md) separates historical results from the latest 1.5.4 milestone.

See [reporter hardware tests](docs/hardware-test.md), [collector hardware tests](docs/collector-hardware-test.md) and [validation](docs/validation.md). This is custom userspace software, not a custom firmware image or a deployed SOC.

## Try offline

Requires a C compiler, Make and Python 3.11+ on macOS or another POSIX host:

```sh
make test
./out/router_health --pretty
python3 collector/collector.py collect --once --transport mock --data-dir /tmp/router-telemetry-demo
python3 collector/collector.py status --transport mock --data-dir /tmp/router-telemetry-demo
```

Tests contact no router. Native macOS checks exercise parsers, JSON and process handling; Linux `/proc` telemetry requires Linux. The collector suite includes real controlling-PTY input, watchdog execution, interruption, descendant cleanup, failure classification and incident recovery.

## Build ARM

```sh
sh build-arm.sh
```

Requires Docker with linux/amd64 support. The base image is pinned by digest, but apt package resolution is not pinned; bit-for-bit reproducibility is not guaranteed. The recipe records tool versions and artifact hashes, checks static linkage, and runs ARM self-tests under QEMU. A rebuild is distinct from the historical hardware-tested artifact.

## Components

- `src/router_health.c`: reporter source v1.0.5, schema `router_health/2` (offline-tested only: **not hardware-accepted, and no ARM artifact has been built from it**; the last hardware-accepted reporter is v1.0.4); host, uptime, **boot identity** (`host.boot_time`), CPU counters, load, memory, filesystem capacity, storage candidates and an explicit `limits` block. Output is always valid UTF-8/JSON, including under CPU-counter truncation (represented as `cpu_stat._truncated`); a metric that cannot be read is `null`, never 0; a failed stdout write exits non-zero. **Reporter v1.0.4 was validated on the bench router with the exact artifact recorded in the hardware results; v1.0.5 has not run on hardware.**
- `collector/`: foreground Python collector v1.5.5 (offline-tested only: **not hardware-accepted**; the last hardware-accepted collector is v1.5.4), bounded SSH transport with byte ceilings, non-blocking time-bounded transfer, hardened provisioning gates, a control-master liveness gate, sanitised diagnostics, an ownership-aware cleanup model with durable pending-cleanup evidence, local history, derived CPU measurements and collection incidents. **Portable configuration checked offline. The private bench copy is still v1.5.4, which passed supervised hardware acceptance; the v1.5.5 changes have not been applied to it.**
- `tests/`: embedded-test runner, JSON smoke check, synthetic `/proc` fixtures and reporter edge tests.
- `docs/`: design, validation, hardware milestones and AI-assisted provenance.

Storage detection remains a path/device heuristic, not proof of USB identity. Missing telemetry is not automatically healthy. [Design notes](docs/design.md) explain the boundaries.

## Live use

Follow the [collector instructions](collector/README.md) to supply your own target, reviewed host keys and artifact hashes. No live target is configured by default. Live collection uploads and executes a temporary program over SSH and removes its owned directory on teardown; it does not install persistent software or alter firmware or network settings.

## Privacy and provenance

Device credentials, actual host keys, private configuration, raw collection history, vendor firmware and extracted proprietary binaries are excluded. Sanitized fixture documents and curated numeric validation results are included. This repository contains a portable adaptation of the private bench collector, not its private defaults. See [provenance](docs/provenance.md).

Mark Pierotti directed the investigation using Hermes and AI coding agents for research, implementation and review. Human work included defining goals, physical setup, authentication, challenging conclusions and deciding which evidence justified each next step. AI-generated implementation is identified explicitly rather than presented as unaided authorship.

## Licensing

An open-source license has not yet been selected. This snapshot grants no additional license; vendor firmware and third-party tool code are not included.
