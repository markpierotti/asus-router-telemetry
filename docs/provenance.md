# Provenance and attribution

Project lead: Mark Pierotti.

The current source versions are reporter **1.0.4** and collector **1.5.4**. The
collector is a portable adaptation: private paths, username, address and host-key
digest are removed; operator configuration is explicit. Verification remains required
for normal live use. Functionally aligned code was applied to both the private bench
and portable copies, with those deliberate configuration differences preserved.

Hardware results belong to the exact bench versions/artifacts named in each record.
The latest is a supervised Ethernet dropout/recovery trial, not an unattended deployment.
The portable copy was checked offline; it was not separately run against the device.

[Import manifest](import-manifest.json) records the original and adapted code-file hashes. Test fixtures derive from reporter output and contain no device hostname, address, login, host key or credential. Synthetic credentials and documentation addresses in tests are examples, not live access material.

Hermes with AI coding models assisted with investigation, implementation, tests and documentation. Codex assisted with independent review, behavioural counterexamples, numeric checks and repository preparation. AI-generated implementation required correction around compatibility claims, stdin forwarding, BusyBox dispatch, shell exit status, watchdog descriptors, cleanup ownership, terminal foreground handling and descendant termination.

Ghidra, REA, GNU binutils, QEMU, GCC, Docker, OpenSSH and OpenSSL were tools. Their implementations, vendor firmware, proprietary extracted binaries and private bench evidence are not included. No claim of unaided authorship, bit-for-bit rebuild reproducibility or custom-firmware bootability is made.


## Curated dropout milestone (2026-10-13)

The new case study and numeric summary derive from private operator-executed bench
runs. Codex independently inspected saved local records and recomputed nine CPU
counter ratios, then checked preservation hashes. Neither raw captures nor login
material were imported. The public summary omits addresses, usernames, host keys,
private paths, exact session timestamps and raw device output. Its arithmetic is
inspectable, but it is not independent authentication of the private source captures.

Documentation closeout changed no product code, artifact or original private
evidence. Original evidence manifests and the append-only cleanup ledger remain
unchanged; manual resolution was recorded separately.
