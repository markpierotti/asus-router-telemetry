# Provenance and attribution

Project lead: Mark Pierotti.

The reporter source is imported unchanged from the working `rt-ax56u-health` project at the source digest recorded in [validation](validation.md). This standalone repository intentionally does not modify that live bench project.

Hermes with AI coding models assisted with investigation, code generation, test development and documentation. Codex assisted with review and preparation of this repository. AI output required correction, particularly around compatibility claims, shell exit status, timeout handling and cleanup ownership.

Ghidra, REA, GNU binutils, QEMU, GCC, Docker and OpenSSL were investigation/build/test tools. Their implementations are not included. Vendor firmware, extracted binaries, device credentials, host keys, serial numbers and full raw inventory are excluded.

The firmware and bench findings in these notes summarize recorded project work. The repository's build recipe is a portable derivative, not a promise to reproduce an earlier binary byte for byte. Hardware execution has not been claimed without saved results.
