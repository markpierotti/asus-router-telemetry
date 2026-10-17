# Claude review brief: Slim publication update

The owner requested Codex and Claude collaborate on preparing the deployed home
lab for GitHub. Claude's original session supplied the implementation sources;
a fresh CLI review was attempted during this update but returned `Not logged in`.
This document requests a fresh review; it is not a claim that Claude approved it.

Review the current branch against `main`, especially:

- Faithfulness of the recovered deployment examples to the 17 October commissioning
  session, including 13 rules, dedicated accounts and local-only metrics listeners.
- Transfer of private-development patch `34b883a` into public history without
  importing private-development history or replacing the publication's prior edits.
- Accurate distinction between v1.5.5 bench acceptance and v1.6.0 deployment evidence.
- No claim that process status establishes successful DNS, all probes, notification
  delivery, complete backups, restore readiness or controlled reboot acceptance.
- No private keys, tokens, live host pins, client inventory, raw history, backups,
  firmware or third-party community dashboard redistribution.
- Consistency of substituted addresses, exporter metric names, Prometheus jobs,
  dashboard datasource identifiers and provisioning paths.
- The backup tar's best-effort behavior and the Mac pull script's per-file failure
  handling are disclosed; these have not been corrected on the live host.
- The supplied [Gemini review](gemini-review-notes.md) identified a valid sample
  parent-directory sync issue. Collector 1.6.1 fixes it in publication source only;
  deployed Slim remains 1.6.0. Check the regression and version distinctions.

The operator supplied service output at 18:34 AEST: eight services active/enabled,
latest collector sample 18:33, backup archive 14:19, next timer 03:36 next day.
Follow-up reran wait-online successfully and showed zero failed units. Ethernet
carrier arrived after the original boot timeout. No network change was made.
Telegram and an external uptime check remain pending.

Return file-specific findings. Do not access live devices, alter networking, read
credentials or publish changes as part of this review.
