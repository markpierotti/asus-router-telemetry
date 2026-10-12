# Provenance and attribution

Project lead: Mark Pierotti.

Reporter v1.0.3 is the remediation revision of the imported bench reporter; its source digest is recorded in [validation](validation.md). Collector v1.3.0 is a portable adaptation: private paths, username, address and host-key digest are removed; operator configuration is explicit; artifact verification remains required for normal live use. The remediation was applied to BOTH the private bench copy and this portable copy, so they are functionally aligned apart from those deliberate configuration differences.

[Import manifest](import-manifest.json) records the original and adapted code-file hashes. Test fixtures derive from reporter output and contain no device hostname, address, login, host key or credential. Synthetic credentials and documentation addresses in tests are examples, not live access material.

Hermes with AI coding models assisted with investigation, implementation, tests and documentation. Codex assisted with independent review, behavioural counterexamples, numeric checks and repository preparation. AI-generated implementation required correction around compatibility claims, stdin forwarding, BusyBox dispatch, shell exit status, watchdog descriptors, cleanup ownership, terminal foreground handling and descendant termination.

Ghidra, REA, GNU binutils, QEMU, GCC, Docker, OpenSSH and OpenSSL were tools. Their implementations, vendor firmware, proprietary extracted binaries and private bench evidence are not included. No claim of unaided authorship, bit-for-bit rebuild reproducibility or custom-firmware bootability is made.
