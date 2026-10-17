# Slim publication checks — 2026-10-17

Base: public repository `main` at `b5d72cceaefcca5d2fafc2e4046838fc83299746`.
Branch: `docs/slim-deployment-20261017`.

## Local verification

- Ported the 1.6.0 key-auth patch from private-development commit `34b883a`; private
  development commit history was not imported into the publication checkout.
- Ran `make test` after porting that patch. Reporter self-tests, JSON smoke tests,
  fixtures, edge tests and reporter regression checks passed. Collector: **386 run,
  383 passed, two failed, one skipped**, in 238.795 seconds.
- Both failures were the existing real controlling-terminal input tests. Their
  diagnostics established that this Seatbelt runner denies `open('/dev/tty', O_RDWR)`
  with `EPERM`. The no-controlling-terminal skip was expected. The suite is **not**
  represented as passing locally; no tests were disabled or converted to skips.
- Subsequently corrected sample parent-directory sync in the unreleased 1.6.1
  source. Both new `test_storage_parent_sync` tests passed, checking directory sync
  after sample rename and after state/outcome replacement. The earlier full-suite
  result applies to the pre-correction snapshot. Hosted CI subsequently passed at
  `b8cf1c1`, as recorded below; it is not evidence for later 1.6.2 edits.
- Four synthetic exporter tests passed: known metrics and database/state contents,
  reads without data modification, and missing-data failure metrics without file
  creation. They run through the new `make test-deployment` target in hosted CI.
- Generated the README JSON excerpt with an offline mock collection from the
  1.6.1 source; no router was contacted.
- Parsed the deployment JSON, YAML, Python and launchd plist, checked shell syntax,
  verified local Markdown link targets and ran `git diff --check`.
- Checked 13 alert rules, two custom 13-panel dashboards, documentation addresses
  and exclusion of known private paths, keys, tokens and household access details.

These are offline checks of the publication package. They do not prove a successful
deployment, PromQL evaluation in a new installation, systemd sandbox behavior,
notification delivery, restart recovery or backup restoration.

## Review and remaining gates

The owner supplied a Gemini evaluation. [Disposition notes](gemini-review-notes.md)
record accepted documentation changes, the confirmed storage correction and deferred
items. General praise is not treated as audit evidence.

Hosted [CI run 39022969457](https://github.com/markpierotti/asus-router-telemetry/actions/runs/39022969457)
passed at **`b8cf1c141258164f4eeafbea7297a0f6dca0cead`**. All four jobs passed:
Ubuntu 24.04 and macOS 15, each with Python 3.11 and 3.13. Each ran **388 collector
tests: 387 passed and one expected no-controlling-terminal skip**, four passing
exporter tests and the passing reporter checks. Both controlling-PTY input tests
passed on hosted runners.

Claude completed a fresh [source review](reviews/claude-pr4-20261017.md) on
**2026-10-17** after the escalated retry. It reviewed the source at `b8cf1c1`, with
no diff comparison against `main`, independent CI inspection, executed checks,
device access or confirmation of the earlier commissioning claims. It does not
approve hardware or runtime behavior. The [scope record](claude-review-brief.md)
and [finding dispositions](slim-review-triage-20261017.md) preserve these limits.

The later 1.6.2 candidate has separate local verification; prior CI does not certify
its new exit-code, pull-script or configuration changes. No new hosted run is
claimed for a local-only commit. No new release tag or live deployment is made.

## Review-fix candidate: local verification, 18 October 2026

Ran the full `make test` outside the sandbox to permit the existing controlling-PTY
tests. Exit status was **0**. Collector: **392 run, 391 passed, one expected
no-controlling-terminal skip**, in 213.309 seconds. Both controlling-PTY input
tests passed. Deployment: **8 run, 8 passed**, in 0.882 seconds, including the
backup filename regressions and complete manifest hash coverage. Reporter: eight
self-test groups, JSON smoke checks and **74 assertions** passed, with zero fixture,
edge or reporter audit regression failures. Native Linux telemetry integration was
not exercised by this macOS run. Python 3.14 emitted ResourceWarnings for pre-existing
SQLite fixture connections; these did not fail the suite.

Focused tests also reproduced the old backup-script staging-path issue in disposable
directories. The patched script preserved outside sentinels while supporting valid
downloads, existing-file skips, invalid-gzip cleanup and retention. A fresh read-only
patch review found no surviving filename bypass or compatibility regression. Shell
syntax, edited document links, JSON/YAML/Python syntax and whitespace checks passed.

The working-tree audit covered exactly 20 modified and five new review-fix files;
no household addresses, MACs, private keys, access tokens or unrelated files were
found. This local result applies to collector 1.6.2 and its repository examples.
The historical hosted CI record above remains tied to b8cf1c1; a new branch push
requires its own hosted run. Slim was not accessed or changed.
