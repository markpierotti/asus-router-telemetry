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
- Gateway: Hermes' own systemd **user** service (`hermes gateway install`), Telegram
  in polling mode, `TELEGRAM_ALLOWED_USERS` = the owner's ID only.
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
| `morning-digest` (`scripts/slim-snapshot.py` as context) | Hermes cron, 07:30 | one call |
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
- The transform script logs every alert for the digest and wakes the agent only once
  per (fingerprint, start time); repeats and resolutions cost nothing.

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

## Rollback

Root-only manual backup directories on Slim (`*-hermes`, `*-grafana-hermes`) hold
the previous Grafana notification file, the wrapper, `slim-backup` and the Hermes
config. To remove Hermes: restore those two Grafana files and restart Grafana, then
`sudo -u hermes hermes gateway stop` and `uninstall`, `loginctl disable-linger
hermes`, and delete the account.
