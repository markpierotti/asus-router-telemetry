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
  result applies to the pre-correction snapshot; final hosted CI remains a gate.
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

Claude's original implementation artifacts were recovered locally. A fresh CLI
review was attempted, but Claude reported `Not logged in`; no fresh approval is
claimed. The [Claude review brief](claude-review-brief.md) is ready for that review.

The final branch requires hosted CI and review before merge. The prior v0.1.0
hosted matrix belongs to that older release. No new release tag or live deployment
is made by this preparation work.
