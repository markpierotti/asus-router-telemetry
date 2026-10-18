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

## Deploying or rolling back a dashboard

The dashboards in `deploy/slim/var/lib/grafana/dashboards/` are loaded by the
`Home lab` file provider. Edits made in the Grafana UI to a provisioned dashboard
do not persist, and importing one with the same UID through the UI conflicts with
it, so deploy by file. Run these on Slim, from the directory holding the new files.
Deploy the overview first and look at it in Grafana before deploying the other two.

Both procedures start with the same backup. Keep the `BACKUP` value: rollback needs it.

```sh
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/var/backups/grafana-dashboards-$STAMP
sudo install -d -m 0750 "$BACKUP"
sudo cp -a /var/lib/grafana/dashboards/. "$BACKUP"/
echo "$BACKUP"
```

### A. Overview only

`slim-overview.json` needs no address edits. The other two dashboards are not touched.

```sh
sudo cp slim-overview.json /var/lib/grafana/dashboards/slim-overview.json
sudo chown --reference=/var/lib/grafana/dashboards/pihole.json /var/lib/grafana/dashboards/slim-overview.json
sudo chmod --reference=/var/lib/grafana/dashboards/pihole.json /var/lib/grafana/dashboards/slim-overview.json
```

### B. All three (overview, Pi-hole, Router)

`router.json` in the repository shows the example address `192.0.2.20` for the access
point. Substitute the real one on Slim; never commit the real address.

```sh
AP_IP=192.0.2.20   # replace with the real access-point address
sudo cp slim-overview.json pihole.json /var/lib/grafana/dashboards/
sed "s/192\.0\.2\.20/$AP_IP/g" router.json | sudo tee /var/lib/grafana/dashboards/router.json >/dev/null
for f in slim-overview pihole router; do
  sudo chown --reference="$BACKUP/pihole.json" /var/lib/grafana/dashboards/$f.json
  sudo chmod --reference="$BACKUP/pihole.json" /var/lib/grafana/dashboards/$f.json
done
```

### Check

Grafana re-reads the folder at the provider's update interval (10 seconds unless
`updateIntervalSeconds` is set).

```sh
ls -l /var/lib/grafana/dashboards
curl --fail --silent http://127.0.0.1:3000/api/health
```

Sign in and open each dashboard; use a panel's Inspect > Query view to read any query
error, because a tile that says NO DATA is reporting missing data, not necessarily a
broken query.

### Rollback

Restore each file you deployed from the backup. A file that was not in the backup did
not exist before, so it is removed instead; in particular `slim-overview.json` is
deleted only when it was absent at backup time, and an earlier copy is put back when
there was one. Set `FILES` to what you deployed: `slim-overview` for procedure A, all
three for procedure B. Grafana removes a provisioned dashboard whose file is deleted
unless `disableDeletion` is set on the provider.

```sh
BACKUP=/var/backups/grafana-dashboards-YYYYMMDD-HHMMSS   # the value printed above
FILES="slim-overview"                                    # or: slim-overview pihole router
for f in $FILES; do
  if [ -e "$BACKUP/$f.json" ]; then
    sudo cp -a "$BACKUP/$f.json" /var/lib/grafana/dashboards/$f.json
  else
    sudo rm -f /var/lib/grafana/dashboards/$f.json
  fi
done
curl --fail --silent http://127.0.0.1:3000/api/health
```

Pi-hole and Router have always existed, so they are always restored, never removed.

What was and was not checked for the current dashboard set is recorded in
[slim-dashboards-20261019.md](slim-dashboards-20261019.md).

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
private key at `/home/rtax/keys/id_ed25519` are excluded, provided the key remains
outside the archived data and pinned directories. Do not upload archives: they can contain Wi-Fi credentials,
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
