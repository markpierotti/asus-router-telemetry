# ASUS Router Telemetry

**Custom ARM telemetry on a consumer router, without replacing its firmware.**

A C reporter and Python collector developed through firmware analysis, cross-compilation, adversarial testing and validation on a physical ASUS RT-AX56U. The reporter emits Linux system measurements as JSON; the external collector verifies execution, saves history and calculates CPU utilisation. Healthy collection makes zero model calls.

## Demonstrated on hardware

| Milestone | Recorded result |
| --- | --- |
| Reverse-engineering toolchain | REA/Ghidra checked against a known-answer fixture; GNU objdump corroborated instructions |
| Firmware and live inventory | Downloaded 386_52090 examined; physical unit runs 386_45898, Linux 4.1.52 armv7l |
| Custom reporter | Static ARM executable runs temporarily on stock firmware |
| Parser comparison | 26 checks passed, zero failed; 10 separate-sample comparisons remained inconclusive |
| SSH collector | Two real collections accepted, with artifact verification and cleanup recorded |
| CPU utilisation | 1.62%, independently recalculated from the two saved aggregate counter sets |
| Continuous operation / unattended auth / AP / USB NAS | Not yet validated |

The [next-phase Hermes prompt](docs/next-phase-prompt.md) prepares a short foreground monitoring trial.

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

- `src/router_health.c`: reporter v1.0.2, schema `router_health/2`; host, uptime, CPU counters, load, memory, filesystem capacity and storage candidates.
- `collector/`: foreground Python collector v1.2.0, bounded SSH transport, local history, derived CPU measurements and collection incidents.
- `tests/`: embedded-test runner, JSON smoke check and synthetic `/proc` fixtures.
- `docs/`: design, validation, hardware milestones and AI-assisted provenance.

Storage detection remains a path/device heuristic, not proof of USB identity. Missing telemetry is not automatically healthy. [Design notes](docs/design.md) explain the boundaries.

## Live use

Follow the [collector instructions](collector/README.md) to supply your own target, reviewed host keys and artifact hashes. No live target is configured by default. Live collection uploads and executes a temporary program over SSH and removes its owned directory on teardown; it does not install persistent software or alter firmware or network settings.

## Privacy and provenance

Device credentials, actual host keys, private configuration, raw collection history, vendor firmware and extracted proprietary binaries are excluded. Sanitized fixture documents and curated numeric validation results are included. This repository contains a portable adaptation of the private bench collector, not its private defaults. See [provenance](docs/provenance.md).

Mark Pierotti directed the investigation using Hermes and AI coding agents for research, implementation and review. Human work included defining goals, physical setup, authentication, challenging conclusions and deciding which evidence justified each next step. AI-generated implementation is identified explicitly rather than presented as unaided authorship.

## Licensing

An open-source license has not yet been selected. This snapshot grants no additional license; vendor firmware and third-party tool code are not included.
