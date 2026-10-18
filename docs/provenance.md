# Provenance and attribution

Project lead: Mark Pierotti.

The current source versions are reporter **1.0.5** and collector **1.6.2** (unreleased).
Slim remains on deployed collector **1.6.0**. Source-only storage and loop-exit
corrections, including the matching 1.6.2 service unit, remain pending deployment.
The owner's separately reported 21:58 dashboard/interim-unit corrections are
recorded in the deployment record.
Claude implemented the key-auth change and commissioned the Slim services on
2026-10-17; Codex ported that patch onto publication history and prepared sanitized
source examples, operations documentation and an operator-evidence record.
Claude completed a fresh [source review](reviews/claude-pr4-20261017.md) on
2026-10-17 after the authorized retry outside the sandbox. It reviewed `b8cf1c1`;
it did not compare the main diff, independently inspect hosted CI, run checks,
access devices or approve hardware/runtime behavior. The
[triage record](slim-review-triage-20261017.md) tracks the subsequent corrections.
The [deployment record](slim-deployment-20261017.md) distinguishes prior session
claims, operator-supplied status and remaining validation.

The historical reporter **1.0.5** / collector **1.5.5** milestone follows. The exact
ARM artifact and private bench adaptation passed [supervised healthy-path acceptance](healthy-acceptance-20261014.md)
on 2026-10-14: 14 successful samples, including a foreground loop and post-stop restart.
Portable configuration is offline-tested. The new pair also passed its own
[supervised Ethernet dropout/recovery trial](dropout-recovery-20261014.md): 18 attempts,
12 successes and six expected unreachable outcomes, with manual cleanup separately
verified. Older 1.0.4 / 1.5.4 evidence remains attached to its original versions.
See the [integration record](integration-20261014.md) for build and source identities.

The collector is a portable adaptation: private paths, username, address and host-key
digest are removed; operator configuration is explicit. Verification remains required
for normal live use. Through 1.0.5 / 1.5.5, functionally aligned production code was applied to both
the private bench and portable copies, with those deliberate configuration differences
preserved.

Hardware results belong to the exact bench versions/artifacts named in each record.
The historical 1.0.5 / 1.5.5 pair has supervised healthy-path and one host-side Ethernet dropout/recovery acceptance. Earlier trials belong to their recorded older versions. Those bench trials were not unattended deployments. The later 1.6.0 Slim commissioning has its own dated record; it does not replace or broaden the older acceptance.

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
