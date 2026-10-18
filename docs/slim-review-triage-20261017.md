# PR #4: Claude review of 17 October — fixes verified 18 October 2026

Input: the [Claude source review of b8cf1c1](reviews/claude-pr4-20261017.md),
originally saved as `SLIM_PR4_CLAUDE_REVIEW_20261017.md`. These dispositions concern
the repository candidate, collector **1.6.2**, which remains pending deployment.
No live host was accessed. The owner's reported **21:58 AEST** live corrections
are recorded separately in the [deployment record](slim-deployment-20261017.md).

| Finding | Disposition | Reason and change |
| --- | --- | --- |
| 1 — restart policy | Fixed in source; pending deployment | Loop host-key/artifact hard stops return unused code 4. Ordinary failures remain 1, bad config 2 and lock acquisition errors 3. The matching 1.6.2 unit blocks only 4 and 2, so 1 and 3 can restart. Tests cover both identity outcomes, no retry within the loop, generic errors, one-shot compatibility and the unit contract. |
| 2 — DNS latency | Fixed in repository | Panel 8/A uses `probe_dns_duration_seconds{job="dns_pihole",phase="request"}` and legend `Pi-hole answer`, matching the owner's live correction. No independent live measurement is claimed. |
| 3 — stale review/CI claims | Fixed | Publication checks record the four successful CI jobs at b8cf1c1. Provenance and the review record link the completed 17 October source review with its limits. This does not certify later source changes or hardware/runtime behavior. |
| 4 — untrusted backup filenames | Fixed | A line-preserving loop validates the entire filename in the C locale before any existence check, path construction, SSH get or write. Tests use fake SSH to reject traversal, wildcard, whitespace, malformed widths, suffixes and encoded forms, and preserve normal downloads, skips, gzip cleanup and retention. |
| 5 — placeholders and gateway labels | Fixed | Collector target is `192.0.2.20`; alert labels say Main gateway. This is consistency/privacy minimization, not a credential disclosure finding. |
| 6 — durability wording | Fixed in documentation | README limits file/parent-directory sync claims to atomic replacements; append-log creation, quarantine and pruning do not gain new directory syncing. No durability implementation change is made here. |
| 7 — hashes / exporter assumption | Hashes fixed; live verification deferred | Manifest hashes are refreshed and a test checks every published deployment file. The example explicitly sets the node-exporter textfile directory; its live configuration and backup metric are not checked because Slim access is excluded. |
| 8 — hardening / key location | Documentation fixed; hardening deferred | Private key is explicitly `/home/rtax/keys/id_ed25519`, outside the backed-up data/pinned trees. Exporter account separation and Pi-hole `ProtectSystem=` remain follow-ups, as requested. Neither service-hardening change is applied. |
| 9 — commissioning test count | Fixed | Claims say 386 tests run. The retained commissioning summary does not establish zero skips; historical hosted counts remain separately identified. |

## Filename boundary and scope

Remote `list` output was used in local path checks and staging writes before
validation. A plain `../name` produces a staging path containing a literal `...`
directory, so the report's shortest traversal example alone overstates that route.
However, `./../../name` and `/../../name` can escape through the staging path;
redirection opens the local file before the remote `get` result is known. The
finding is valid. The fix also prevents word splitting or glob expansion from
turning an invalid raw record into a permitted filename. No remote pathname or
encoding is decoded or normalized into the whitelist.

The backup protocol still trusts the selected host to return archive contents and
uses gzip integrity checking rather than content authentication. Existing partial
transfer failure reporting is unchanged and remains documented. This fix addresses
filename validation, not every trust or backup-completeness concern.

## Pending deployment and follow-ups

Deploy collector 1.6.2 and its `RestartPreventExitStatus=4 2` unit together. The
owner reports live Slim at 1.6.0 with interim `RestartPreventExitStatus=1 2`; pairing
the new unit with that older collector would permit identity-stop retries.
The changed Mac pull script and explicit node-exporter argument are repository
candidates, not claims that those live installations were updated.

Separate exporter identity/key access and additional Pi-hole exporter sandboxing
remain deferred. Controlled restarts, actual systemd restart decisions, PromQL
evaluation, restore readiness and external notification delivery remain unverified
by these source changes.

## Verification

Full `make test` exited 0 outside the sandbox: **392 collector tests run, 391 passed,
one expected skip; eight deployment tests passed**. Reporter self-tests, smoke checks
and 74 assertions passed. The original filename issue reproduced using only temporary
files; the patched regression preserved sentinels and valid download/retention
behavior. The [publication checks](slim-publication-checks.md) record command results,
historical CI boundaries and remaining runtime limitations.
