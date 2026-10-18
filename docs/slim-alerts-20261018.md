# Slim Telegram alerts and external heartbeat — 2026-10-18

This milestone adds direct Grafana Telegram delivery and an external Healthchecks
heartbeat. It leaves the live collector at 1.6.0 with its interim restart policy.
The repository's 1.6.2 collector/unit deployment remains separate.

## Configuration

Grafana's root notification policy sends all 13 existing rules to `Telegram`.
Grouping uses `grafana_folder` and `alertname`, with a 30-second initial wait,
five-minute group interval and four-hour repeat interval. Resolved messages are
explicitly enabled. The existing rules and their thresholds are unchanged.

Alert provisioning supports environment interpolation, not the INI `$__file{}`
provider. `grafana-with-slim-secrets` reads the token and recipient from files,
exports them only to the Grafana process and executes the packaged command.
The provisioning source contains variable names rather than credentials.
Grafana users with process inspection privileges can access its environment;
this is not isolation from root or the Grafana account.

Root-owned secret files (mode 0640):

- `/etc/slim-secrets/telegram_bot_token` — group grafana, new Slim bot.
- `/etc/slim-secrets/telegram_chat_id` — group grafana, private recipient.
- `/etc/slim-secrets/healthchecks_url` — group root, external check.
- `/etc/slim-secrets/openrouter_api_key` — group root, reserved for later Hermes setup.

The heartbeat timer runs every five minutes. Its oneshot service checks a real
local Pi-hole DNS answer, Prometheus readiness and Grafana database health, then
sends success only if all three pass; otherwise it sends `/fail`. Curl has
connection/execution bounds and retries. The URL travels over stdin configuration,
not process arguments or journal output. Failure to deliver exits nonzero.
No model calls or Hermes process participates in either alert path.

The external check is configured by the operator for a five-minute period and
ten-minute grace, with Healthchecks' own Telegram integration. Missing heartbeats
can mean host, network, or heartbeat failure; they do not identify the cause.
A successful request proves endpoint acceptance, not human receipt of Telegram.

## Backups and rollback

`slim-backup` now includes `/etc/slim-secrets`, the Grafana drop-in, heartbeat
units and existing `/usr/local/bin` script tree. Backups and Mac copies therefore
contain credentials and must stay private. The collector private key remains
excluded. The existing best-effort archive limitation is unchanged.

Before installation, changed files and a consistent Grafana SQLite snapshot were
saved in a root-only manual backup directory. Its `rollback.sh` disables the
heartbeat, stops Grafana, restores the saved configuration/database, removes new
files and restarts Grafana. A database rollback restores the setup-time state;
subsequent Grafana edits would be lost. Run it only after reviewing that tradeoff.
The rollback path and private host details are kept in the operator's local record.

## Verification

- Grafana health returned database `ok` after provisioning; Telegram became the
  root receiver, resolved delivery was enabled and all 13 original rules remained.
- Grafana's authenticated contact-point test reported notification success.
- A separate synthetic rule was observed in `Firing`, then `Normal`. It used a
  time-limited Prometheus expression, without stopping any real service. Grafana's
  Telegram notification counter reached three requests/notifications during the
  test window; the temporary rule and provisioning file were then removed.
- Healthchecks accepted the normal heartbeat, a deliberately failed in-memory DNS
  probe (`/fail`, local exit 1), and a normal recovery run (local exit 0).
- The five-minute timer is enabled. The local probes all pass.
- The new backup was inspected by archive member names: all new configurations,
  scripts, units and secret files were present; the collector key was absent.
- Deployed notification, heartbeat and backup source hashes match the repository.
- The live collector still reports 1.6.0 and `RestartPreventExitStatus=1 2`.
- Full-suite results and operator confirmation of Telegram receipt are recorded
  separately below.
No router, gateway, SSH, firewall, or household DNS configuration changes are part
of this milestone. The gateway model was confirmed by the operator as Archer AX53.

## Sources

[Grafana alerting file provisioning](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/)
documents Telegram settings and environment interpolation.
[Healthchecks Telegram integration](https://healthchecks.io/integrations/telegram/)
documents the independent hosted notification path.
