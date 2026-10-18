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
| `asus-log-watch` (`scripts/asus-logwatch.py`) | Hermes cron, every minute, `--no-agent` | none |
| `morning-digest` (`scripts/slim-snapshot.py` as context) | Hermes cron, 08:00 | one call |
| Alert follow-up (`scripts/grafana-alert.py` transform) | Grafana webhook | only for newly firing alerts |

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
- Derived histories (`grafana-alerts.jsonl`, `asus-events.jsonl`) are trimmed to 30
  days once they pass 1 MB; the snapshot reads only their last 1 MB and the last 4 MB
  of the ASUS log. The watcher reads whole lines only and finishes a rotated file
  before moving to the new one.

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
stale documentation. All are fixed above and covered by fixtures
(`tests/test_hermes_scripts.py`, `tests/test_asus_syslog_metrics.py`). Owner decisions:
daily syslog rotation with 30 kept; digest at 08:00; deterministic ASUS syslog
metrics plus a silence alert (see the syslog note); the hardened system service. The
empty restart allowlist is enforced by Hermes' manual approvals and the account
having no sudo; no OS-level restart helper exists until a restart is approved.

Verified live: the sandboxed gateway reconnected to Telegram, could read the ASUS log,
journal, Prometheus and unit status, could not write outside its home and saw no
other home directories; two signed synthetic alerts sent 5 s apart were both
coalesced into one investigation.

Not exercised: a real gateway/internet outage and a live model-provider outage.

## Rollback

Root-only manual backup directories on Slim (`*-hermes`, `*-grafana-hermes`) hold
the previous Grafana notification file, the wrapper, `slim-backup` and the Hermes
config, and `*-hermes-harden` holds the former user unit. To drop only the sandbox,
remove the drop-in and `systemctl daemon-reload && systemctl restart hermes-gateway`.
To remove Hermes: restore the two Grafana files and restart Grafana, then
`sudo hermes gateway uninstall --system`, `loginctl disable-linger hermes`, and
delete the account.
