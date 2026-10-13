# Reviewed integration — 2026-10-14

Collector **1.5.5** and reporter **1.0.5** are integrated from reviewed source
commit `036d3383a04002e81ca2710d076ad04f42c89b72`. The private bench collector is
synchronized with only the documented private default blocks and example
configuration differing. The import manifest is regenerated from the files.

**Not hardware-accepted.** The last hardware-accepted pair remains collector
1.5.4 / reporter 1.0.4, artifact
`17ade0d2e85ed2880cb6a38de018bcf3f3d76d3f9090eb29eda54c007fe66e36`.
The old code, artifact and historical evidence are preserved. No router request,
auth change, firmware change or persistent deployment occurred during integration.

## New build

- Source SHA-256: `18c2fdfa7466ceb6cdf18b4d07cffd83f0b56d1b8a583118fc9ffd17c6d65907`.
- ARM SHA-256: `a1cb7d1f1787c2893bacd7fa1233727da01cca8944fc8d47064708c8c8d6d0c3`.
- Pinned image: `gcc@sha256:f6ddfc063e12d8141189d5b5fd1afc64aedc6eb50a6b8876263f9a944fa9f8ca`.
- Cross compiler: Debian arm-linux-gnueabi GCC 12.2.0-14; gcc-arm-linux-gnueabi
  package 4:12.2.0-3; libc6-dev-armel-cross 2.36-8cross1;
  qemu-user-static 1:7.2+dfsg-7+deb12u18+b3.
- ELF 32-bit little-endian ARM, EABI5 soft-float, static, Linux minimum 3.2.0.
- Built once with the existing `build-arm.sh`; package versions recorded, not pinned.

The exact ARM executable passes its 8-group self-test, JSON check and all reporter
fixture, edge and audit-regression checks under QEMU. The integrated portable
offline suites also pass. Private bench verification is recorded separately in
the integration report. These results do not establish device interoperability.

The GCC build emits a format-truncation warning for `cpustat_has` (native and ARM).
Its only caller first accepts keys of at most 13 characters through
`cpustat_key_ok`; the 24-byte pattern buffer therefore fits the reachable key,
quotes, colon and terminator. No warning suppression or source change was made.
The earlier analyzer stream-state observations remain deferred.

## Acceptance boundary

Use a fresh local data directory for supervised hardware acceptance: two one-shots,
independent CPU-delta checks, a foreground loop with Ctrl-C, and a post-stop
one-shot. Do not infer a successful new hardware run from preserved old samples.
The dropout-recovery milestone belongs to the earlier accepted versions until
retested. No unattended service or SSH key is introduced here.

## Integration test-fixture correction

The first private collector suite exposed an inherited default in the stalled-master
fixture: it created synthetic host keys without setting their digest, so the private
build correctly refused them before reaching the intended master timeout. The fixture
now explicitly hashes its own fake file in both trees (no production code or real
host-key policy change). The three affected tests pass in both trees; the first failed
private-suite log is retained in the private integration evidence. The private full
suite is rerun after this correction. The production source/build identity is unchanged.

Test-only commit: `1e19e71a3b46f3055219af561334f5e079c86589`.
