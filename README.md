# ASUS Router Telemetry

A small C program that collects Linux router health as JSON, developed during an investigation of an ASUS RT-AX56U running stock firmware.

**Question:** can useful custom monitoring run on a consumer router without replacing its firmware?

The project combines firmware analysis, ARM cross-compilation and evidence-based validation. The reporter reads system information, emits one document and exits. It opens no network listener and does not change router configuration.

## Status

| Milestone | Evidence / limitation |
| --- | --- |
| Analysis toolchain | REA/Ghidra validated against a known-answer fixture; instructions corroborated with GNU objdump |
| Firmware examination | Downloaded 386_52090 firmware extracted; ARM userland investigated |
| Reporter | C source cross-built as a static ARM EABI5 executable; offline self-tests and ARM emulation recorded |
| Physical device | Authenticated inventory confirms firmware 386_45898, Linux 4.1.52 and ARMv7 hardware |
| On-device reporter execution | **Passed on the physical router**: hash-verified transfer, exit 0, valid JSON and reported cleanup |
| Persistent deployment / AP / NAS integration | Future work |

See the [hardware test](docs/hardware-test.md) and [validation](docs/validation.md) for the boundary between recorded evidence and demonstrated behavior. This is custom userspace software, not a custom firmware image.

## Try locally

Requires a C compiler, Make and Python 3. Linux provides the intended `/proc` inputs. macOS can run parser self-tests and JSON smoke checks, but it cannot validate Linux telemetry collection.

```sh
make test
./out/router_health --pretty
```

The test compiles the reporter, runs its embedded self-tests, parses compact and pretty output as JSON, and checks host identity. On Linux it also requires memory and filesystem information.

## Build and test ARM

Requires Docker with linux/amd64 support (including emulation on Apple Silicon). The disposable container installs cross-compilation and test tools; it does not contact the router.

```sh
sh build-arm.sh
```

The base image is pinned by digest. Debian package versions are recorded but package resolution is not pinned: **bit-for-bit reproducibility is not guaranteed**. The build fails if native or ARM self-tests, JSON validation, or the static-link check fail.

## Output

- Host architecture, kernel and uptime
- CPU information, raw CPU counters and load averages
- Memory counters
- Filesystem capacity and candidate USB/storage mount information

Schema identifier: `router_health/1`. `--pretty` formats output; `--selftest` runs embedded checks. CPU counters need two samples to derive utilization. Storage detection is a path/device heuristic, not proof that a physical USB drive is attached. Missing information should be interpreted as unavailable, not automatically healthy.

## Engineering decisions

- **Validate the toolchain first:** use known behavior before trusting decompilation.
- **Inspect the real unit:** downloaded firmware and installed firmware differ.
- **Keep collection lightweight:** single-shot execution, bounded buffers, no daemon or listener.
- **Exercise realistic inputs:** escaped spaces in mount paths caused a capacity-reporting bug.
- **Separate collection from analysis:** the reporter emits structured data; a future external monitor can interpret it.

[Design notes](docs/design.md) describe the implementation and its limits. [Provenance](docs/provenance.md) explains authorship and source selection.

## Scope of this repository

Includes the reporter, portable local tests, ARM build recipe and curated project notes. Bench-specific SSH automation, host keys, credentials, raw inventory, vendor firmware and extracted proprietary binaries are excluded. No command here transfers software to a router or enables services.

## AI-assisted development

Mark Pierotti directed the investigation using Hermes and AI coding agents for research, implementation and documentation. Human work included defining the goal, coordinating physical setup, authenticating to the device, challenging conclusions and deciding which evidence justified the next step. AI-generated implementation is identified explicitly rather than presented as unaided authorship.

## Licensing

An open-source license has not yet been selected. This snapshot grants no additional license; vendor firmware and third-party tool code are not included.
