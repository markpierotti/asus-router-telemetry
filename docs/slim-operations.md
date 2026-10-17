# Slim operations and recovery

Use this alongside the [dated deployment record](slim-deployment-20261017.md) and
[example configuration](../deploy/slim/README.md). All addresses below are examples.
Run host commands on Slim; use the Mac only for SSH and backup-pull checks.

## Routine health check

```sh
systemctl --failed --no-pager
systemctl is-active pihole-FTL prometheus prometheus-node-exporter \
  prometheus-blackbox-exporter grafana-server pihole-exporter rtax-collector rtax-exporter
dig @127.0.0.1 example.com
curl --fail --silent http://127.0.0.1:9090/-/ready
curl --fail --silent http://127.0.0.1:3000/api/health
curl --fail --silent http://127.0.0.1:9090/api/v1/targets
curl --fail --silent --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=probe_success'
curl --fail --silent http://127.0.0.1:9617/metrics
curl --fail --silent http://127.0.0.1:9618/metrics
journalctl -u rtax-collector --no-pager -n 30
systemctl list-timers slim-backup.timer --no-pager
df -h /
```

Inspect target health, last scrape errors, freshness of collector timestamps and
custom `*_exporter_up` values, not just HTTP success. In Grafana's alerting UI,
check all 13 rules and their evaluation errors separately from notification status.
The Pi-hole exporter emits client identities and queried domains: keep metrics,
dashboards and screenshots private unless explicitly sanitized.

## Collector hard stop or stale samples

Inspect `systemctl status rtax-collector` and its journal before restarting.
Compare the configured host-key file and reporter digest against separately trusted
records. Establish a changed router identity through a trusted channel; do not
disable verification to clear an alert. Check key permissions, private config,
clock, data-directory space and whether another collection holds the lock.

Use `collector.py status --data-dir /home/rtax/data` as the `rtax` user. Preserve
the pending-cleanup ledger. Follow the existing [cleanup procedure](dropout-recovery.md)
for exact recorded owned paths; do not delete arbitrary router temporary directories.
After resolving the cause, explicitly restart the service and confirm new samples.

## Backups and Mac pulls

```sh
systemctl status slim-backup.service slim-backup.timer --no-pager
journalctl -u slim-backup.service --no-pager -n 40
ls -lt /var/backups/slim
# Substitute an actual archive name:
gzip -t /var/backups/slim/slim-YYYYmmdd-HHMMSS.tar.gz
tar -tzf /var/backups/slim/slim-YYYYmmdd-HHMMSS.tar.gz
```

On the Mac, inspect `~/Backups/slim`, the pull log and launchd job's last exit code.
Compare archive hashes between hosts when checking a transfer. The pull script's
gzip test checks compression integrity, not a successful application restore.
The recovered pull script can exit successfully after an individual transfer fails;
inspect its log and expected archive presence as well as the launchd status.

The backup uses SQLite's backup API for Pi-hole query history and Grafana state.
Its `files.tar.gz` step uses `--ignore-failed-read` and tolerates tar errors. Review
the nested archive's member list against required recovery files; a reported success
can coexist with missing optional files. The current success metric establishes
archive creation, not full recovery readiness. Prometheus TSDB and the collector's
private key are excluded. Do not upload archives: they can contain Wi-Fi credentials,
Grafana secrets, DNS history and device inventory.

## Restore rehearsal

1. Keep a verified off-host archive and extract it to a private staging directory on
   an isolated recovery host. Review both archive member lists before extraction.
2. Reinstall the required Ubuntu, Pi-hole, Prometheus and Grafana versions or assess
   migrations explicitly. Save the new host's current files before restoring anything.
3. Restore selected configuration and consistent database snapshots with services
   stopped, preserving expected owners and permissions. Use the Pi-hole teleporter
   with a compatible installation. Review the nested `files.tar.gz` contents manually.
4. Rebuild the collector application from the recorded source revision. Restore its
   data and trusted pins only after checking them. Generate a new collector key,
   install its public half through trusted router administration and retest login.
5. Restore monitoring sources, dashboard provisioning and units; reload systemd,
   start services in dependency order and repeat the functional checks above.
6. Verify DNS resolution and blocking with synthetic queries, collector samples,
   scrape freshness, dashboard data, alert evaluation and a test notification.
   Record results before switching household DNS or declaring recovery complete.

Do not overwrite netplan, SSH access, sudoers or firewall configuration wholesale
on a headless host. Check access and a recovery path before each network change.
No restore rehearsal has yet been recorded.

## Rollback and outages

For a collector rollback, stop its unit, preserve its data, restore the retained
application version and use a matching configuration. Version 1.5.5 has no key-auth
option; remove `identity_file` from a private configuration copy and run it manually
with terminal password authentication. Do not point an unattended unit at that
password-only version. Reporter and host pins still need verification.

For a DNS outage, use your documented router DHCP/DNS recovery procedure and test
a client after renewal. Do not experiment with network renderers over the only
working SSH connection. For a complete Slim outage, inspect power and cabling;
external alert delivery requires the still-pending independent uptime check.
