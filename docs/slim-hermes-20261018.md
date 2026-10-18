# Slim: Hermes network operator — 2026-10-18

Hermes Agent (v0.22.2, stable channel) runs on Slim as a **read-only** network
operator. The owner talks to it through the same Telegram bot that Grafana uses
for alerts. Routine monitoring stays with Prometheus, Grafana and plain scripts;
the model is called only for chat, alert follow-ups and one morning digest.

## Install shape

- Dedicated `hermes` account: no sudo, locked password, member of `adm` (system
  journal and `/var/log/asus`), systemd linger enabled. Installed with the official
  `install.sh --channel stable --non-interactive --skip-browser --skip-computer-use`;
  `libatomic1` and `ripgrep` were preinstalled because the account cannot use apt.
- Gateway: a **system** service running as `hermes`
  (`hermes gateway install --system --run-as-user hermes`), sandboxed by
  `etc/systemd/system/hermes-gateway.service.d/hardening.conf`: NoNewPrivileges,
  read-only system (`ProtectSystem=strict`), other homes hidden (`ProtectHome=tmpfs`
  with only `/home/hermes` bound back), writes limited to its home and its runtime
  directory, private /tmp and /dev, no capabilities, kernel/cgroup/clock protection.
  `systemd-analyze security` rates it 4.0 (OK). A user-service sandbox was not used:
  Ubuntu 24.04 restricts the unprivileged user namespaces it would need. Linger stays
  on because restart-safe cron workers use `systemd-run --user --scope`, which forks
  from the sandboxed gateway and inherits its restrictions.
- Telegram in polling mode, `TELEGRAM_ALLOWED_USERS` = the owner's ID only.
- `deploy/slim/hermes/SOUL.md`: role, network facts, read-only rules, the bottom-up
  investigation order and the incident reply format.
- `deploy/slim/hermes/config.overlay.yaml`: merged over the installer's default
  `config.yaml`. DeepSeek Flash via OpenRouter, low reasoning, 15 turns; approvals
  manual and dangerous commands denied in cron/unattended runs; delegation, browser,
  cronjob, computer-use and image tools disabled. `CHAT_ID` is substituted on the host.
- `.env` (0600, built on the host from `/etc/slim-secrets`, never copied off it):
  OpenRouter key, bot token, allowed user, webhook enable/port/secret.

## Proactive jobs

| Job | Trigger | Model |
| --- | --- | --- |
| `asus-log-watch` (`scripts/asus-logwatch.py`) | Hermes cron, `* * * * *` (every wall-clock minute), `--no-agent` | none |
| `morning-digest` (`scripts/slim-snapshot.py` as context) | Hermes cron, 08:00 | one call |
| Alert follow-up (`scripts/grafana-alert.py` transform) | Grafana webhook | newly firing alerts, and alerts still firing at Grafana's 4-hour repeat |

- The log watcher follows `asus.log` by inode and offset (including the tail of the
  rotated file) and reports unknown Wi-Fi MACs once each, failed logins, logins from
  anywhere but Slim, and AP restarts. Its first run ignores the backlog's restarts
  and logins. Known MACs: `~hermes/network/known-devices.json`
  (`known-devices.example.json` shows the format).
- Grafana's Telegram contact point has a second receiver, `slim-hermes`: a webhook
  to `http://127.0.0.1:8644/webhooks/grafana` (loopback only) signed with
  HMAC-SHA256 in `X-Webhook-Signature`. Grafana's `hmacConfig` has no timestamp
  format matching Hermes' V2 scheme, so the legacy body-only signature is used; the
  listener is loopback-only, which limits replay to processes already on Slim. The
  secret is `/etc/slim-secrets/hermes_webhook_secret` (root:grafana 0640), exported by
  `grafana-with-slim-secrets` and copied into the Hermes `.env`.
- The transform script (file-locked, atomic writes, malformed payloads ignored) logs
  every alert for the digest and wakes the agent when an alert newly fires, or when
  it is still firing at Grafana's 4-hour repeat. That repeat is the retry path: a
  follow-up lost to a model or network outage is attempted again, and a long incident
  gets a fresh look. Resolutions cost nothing. Investigation is best-effort; the raw
  Grafana alert is the delivery guarantee.
- Correlation: the transform's output includes every alert that fired in the last
  10 minutes plus a constant `group` key, and the route coalesces on it (60 s quiet
  window, 180 s cap). Gateway, AP and internet alerts arriving together become one
  investigation that looks for a shared cause; the Telegram alerts themselves are
  still grouped per alert name.
- Derived histories (`grafana-alerts.jsonl`, `asus-events.jsonl`) are trimmed to the
  last 30 days and at most 5,000 records once they pass 1 MB. 1 MB is the trigger,
  not a ceiling: 5,000 records can be several MB. The snapshot reads only their last
  1 MB and the last 4 MB of the ASUS log.
- The watcher and the metrics exporter read whole lines and finish a rotated file
  before moving to the new one. The rotated file's last line is taken even without a
  newline and kept as its own record. A single line longer than the read limit (2 MB
  watcher, 4 MB exporter) is read in pieces; rsyslog does not produce such lines here.
- A `slim_ip` that is missing, empty or not an IP address pauses login alerts with a
  one-time notice.

Provisioning gotcha: rewrite `slim-notifications.yaml` with
`json.dump(..., ensure_ascii=False)`. ASCII-escaped emoji (`\ud83d…`) in the message
template make Grafana's YAML parser fail and alert provisioning stop.

## Verification (2026-10-18 14:54 AEST)

- Script fixtures: `tests/test_hermes_scripts.py`. On Slim the snapshot and the log
  watcher ran cleanly as `hermes`, and a second watcher run was silent.
- Gateway: Telegram polling healthy; webhook listening on `127.0.0.1:8644` only.
- A synthetic Grafana rule fired: the raw Telegram alert arrived, Hermes accepted the
  signed webhook, investigated in 11 s (2 model calls) and delivered a correct
  follow-up ("synthetic test; snapshot clean; do nothing"). The resolution arrived
  and the rule removed itself.

## Review fixes (PR #5, 2026-10-18 15:35–15:50 AEST)

Codex and Gemini Argon reviews found unbounded derived histories, split lines at the
read limit, abandoned rotated tails, dedupe committed before triage, unlocked state
writes, an unguarded known-devices read, missing correlation, no service sandbox and
stale documentation. The fixes are described above. Fixtures in
`tests/test_hermes_scripts.py` and `tests/test_asus_syslog_metrics.py` cover the
log readers, payload handling, gating and correlation. Concurrent webhook deliveries
(the file lock) and the live systemd sandbox are not exercised by fixtures; the
sandbox was checked by hand on Slim. Owner decisions:
daily syslog rotation with 30 kept; digest at 08:00; deterministic ASUS syslog
metrics plus a silence alert (see the syslog note); the hardened system service. The
empty restart allowlist is enforced by Hermes' manual approvals and the account
having no sudo; no OS-level restart helper exists until a restart is approved.

Verified live: the sandboxed gateway reconnected to Telegram, could read the ASUS log,
journal, Prometheus and unit status, could not write outside its home and saw no
other home directories; two signed synthetic alerts sent 5 s apart were both
coalesced into one investigation.

Not exercised: a real gateway/internet outage and a live model-provider outage.

## Codex re-review fixes (commit `1d2eb7f`, deployed 2026-10-18 20:49 AEST)

Codex re-reviewed `844c7be..ab451e1` and raised three issues, all addressed by
`1d2eb7f`:

1. **Rotated-tail record merge (fixed).** An unterminated last line of the rotated
   file was joined to the first line of the new file, so a failed-login tail hid a
   following foreign login and the exporter counted one line too few. Both readers
   now end that tail with a newline before appending the new file's lines.
2. **Empty `slim_ip` silently disabled login alerts (fixed).** Empty,
   whitespace-only and non-IP values are now treated as missing: login alerts pause
   with the one-time notice.
3. **Documentation (corrected).** `SECURITY.md` heartbeat wording, the follow-up
   trigger in the jobs table, the 1 MB trim threshold, oversized single lines and
   the stated fixture coverage.

Tests at `1d2eb7f`:

- Deployment suite, run locally before commit: 37 tests, OK. The new rotated-tail
  and `slim_ip` fixtures were also run against the `ab451e1` scripts and failed
  there, as expected.
- Full `make test`, run locally: exited 0 with `OK`. The saved output kept only its
  last lines, so local per-suite counts were not recorded.
- CI [run 39120348870](https://github.com/markpierotti/asus-router-telemetry/actions/runs/39120348870)
  (`make test`): all four jobs passed (macOS 15 and Ubuntu 24.04, Python 3.11 and
  3.13). Each job ran 393 collector tests, OK (1 skipped), and 37 deployment tests,
  OK.

Deployment (only these two files changed; no service restarted, no other
configuration touched). Backup:
`/var/backups/slim-manual/20261018-204915-codex-rereview/` (root:root 755).

| File | Owner, mode | Backed-up SHA-256 (`ab451e1`) | Installed SHA-256 (`1d2eb7f`) |
| --- | --- | --- | --- |
| `/home/hermes/.hermes/scripts/asus-logwatch.py` | hermes:hermes 0700 | `c1092d8bd4eae5cbc261cd977dcd24adb534f9dd778a37ac46bbb9921de27f65` | `3c0e7163d56137794dda08301321bac1ee2dd1ed01555f0949f2b8c30c67b46a` |
| `/usr/local/bin/asus-syslog-metrics` | root:root 0755 | `0fcbc52dc2a77fa60707e9452c598e6625d52bc3e5e6b9c7dd52ef0113119222` | `5630347df6920459cd28ef9ae2c5cb39778b91846b61ce05f2a02074ea886674` |

Ownership and modes are unchanged from the backed-up copies, and the installed
hashes match the repository at `1d2eb7f`. Files installed 20:49:15 AEST.

Live observations, each script running under its normal identity:

- `asus-syslog-metrics.service` (started once by hand, then the timer): finished
  successfully at 20:49:21, 20:50:28, 20:51:28 and 20:52:28. The textfile was
  rewritten (root:root 0644), and Prometheus (job `node`, instance `slim`) returned
  `asus_syslog_metrics_run_timestamp_seconds` from the 20:50:28 run.
- `asus-log-watch` (Hermes scheduler inside the sandboxed gateway): runs at 20:50:41
  and 20:52:41 completed. Its state file was updated at 20:50:44 and follows the
  current log inode. No new events were derived, the no-`slim_ip` notice is not set,
  and the gateway journal showed no errors after the install.
- Observed cadence: although the job was configured `every 1m`, its recorded runs
  (20:38:41 to 20:52:41) were 2 minutes apart. Corrected at 21:09; see the next
  section.

These observations cover one short window. They are not outage, endurance or
reboot acceptance and establish no general reliability claim; the limits under
"Not exercised" above still apply.

## Watcher cadence fix (2026-10-18 21:09 AEST)

Cause (read from the installed Hermes source; the separate diagnosis has the file
references): an `every 1m` interval is due one minute after the previous run
*completes*, and Hermes' built-in scheduler checks for due jobs only every 60 s.
Each run finished about 3 s after a tick, so the next tick came just before the job
was due and it ran on the tick after that, every 120 s. A cron expression is due at
the next wall-clock minute instead.

Change: the existing job `asus-log-watch` (`088668929e5b`) was edited with the
supported CLI, without restarting the gateway:

```sh
sudo -n -u hermes -H /home/hermes/.local/bin/hermes cron edit 088668929e5b --schedule '* * * * *'
```

Before: `{"kind": "interval", "minutes": 1, "display": "every 1m"}`. After:
`{"kind": "cron", "expr": "* * * * *", "display": "* * * * *"}`. A field-by-field
comparison with the backup showed only `schedule` and `schedule_display` changed
(plus the runtime `next_run_at`). The script (`asus-logwatch.py`), `no_agent: true`,
`enabled` and the delivery settings are unchanged. The other job (`morning-digest`)
is unchanged, and there is still exactly one watcher job.

Backup (private; root:root 0700 directory, file kept at hermes:hermes 0600):
`/var/backups/slim-manual/20261018-210910-watcher-cadence/jobs.json`.

Observed from Hermes' execution records, normal scheduled runs only (no manual
runs):

| Scheduled | Started | Finished | Duration | Gap since previous start | Status |
| --- | --- | --- | --- | --- | --- |
| 21:10:00 | 21:10:44.470 | 21:10:44.923 | 0.454 s | 120.040 s (last interval-era gap) | completed |
| 21:11:00 | 21:11:44.446 | 21:11:44.905 | 0.460 s | 59.976 s | completed |
| 21:12:00 | 21:12:44.505 | 21:12:44.932 | 0.427 s | 60.059 s | completed |
| 21:13:00 | 21:13:44.490 | 21:13:44.924 | 0.434 s | 59.985 s | completed |
| 21:14:00 | 21:14:44.464 | 21:14:44.929 | 0.465 s | 59.974 s | completed |
| 21:15:00 | 21:15:44.474 | 21:15:44.918 | 0.444 s | 60.010 s | completed |
| 21:16:00 | 21:16:44.462 | 21:16:44.914 | 0.452 s | 59.987 s | completed |

- After the change, six consecutive gaps were 59.974–60.059 s. Before it, nine gaps
  from 20:52 to 21:08 were 119.921–120.098 s.
- No run overlapped the previous one and none failed or recorded an error. Every
  delivery outcome was `suppressed`, the normal result when the watcher has nothing
  to report. The gateway journal showed no errors after the edit.
- Runs start about 44 s after the scheduled minute. That is the phase of Hermes'
  60 s tick, not a delay in the watcher; the cron expression does not promise
  starts on the exact minute.

These are observations over one short, lightly loaded window, not a timing
guarantee or outage/endurance acceptance.

## Rollback

Root-only manual backup directories on Slim (`*-hermes`, `*-grafana-hermes`) hold
the previous Grafana notification file, the wrapper, `slim-backup` and the Hermes
config, and `*-hermes-harden` holds the former user unit. To drop only the sandbox,
remove the drop-in and `systemctl daemon-reload && systemctl restart hermes-gateway`.
To undo `1d2eb7f` on Slim, copy the two files back from
`*-codex-rereview` with `cp -a`, which keeps their ownership and modes.
To undo the cadence fix, use the same supported edit:
`sudo -n -u hermes -H /home/hermes/.local/bin/hermes cron edit 088668929e5b --schedule 'every 1m'`.
Do not copy the `*-watcher-cadence` backup over the live `jobs.json`, because that
would overwrite newer scheduler state; the backup is evidence and a last resort.
To remove Hermes: restore the two Grafana files and restart Grafana, then
`sudo hermes gateway uninstall --system`, `loginctl disable-linger hermes`, and
delete the account.
