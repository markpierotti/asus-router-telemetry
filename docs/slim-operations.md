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
check all 16 rules and their evaluation errors separately from notification status.
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
it, so deploy by file. Run these on Slim, from a checkout of this repository that
holds the new files. Deploy the overview first and look at it in Grafana before
deploying the other two.

Backups are root-owned and mode 0750, so an ordinary operator cannot even test
whether a file exists in one: an unprivileged `[ -e ... ]` answers "absent" for a file
that is there. Backup and rollback therefore go through
`deploy/slim/usr/local/bin/slim-dashboards`, which escalates with `sudo` first and does
every check and copy as root. Never test the backup directory yourself without `sudo`.

```sh
D=deploy/slim/var/lib/grafana/dashboards
SD="sh deploy/slim/usr/local/bin/slim-dashboards"
LIVE=/var/lib/grafana/dashboards

# Run after backup, while the existing Pi-hole file provides trusted permissions.
publish_dashboard() {
  sudo python3 - "$1" "$LIVE/$2.json" "$LIVE/pihole.json" "${3:-}" <<'PY'
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

source, target, reference = map(Path, sys.argv[1:4])
data = source.read_bytes()
if sys.argv[4]:
    address = str(ipaddress.IPv4Address(sys.argv[4]))
    data = data.replace(b'192.0.2.20', address.encode('ascii'))
json.loads(data)  # Refuse invalid JSON before touching the live directory.
metadata = reference.stat()
fd, temporary = tempfile.mkstemp(prefix='.dashboard-', dir=target.parent)
try:
    with os.fdopen(fd, 'wb') as stream:
        os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
        os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    Path(temporary).unlink(missing_ok=True)
PY
}
```

### Backup (both procedures)

The script refuses to back up unless `pihole.json` and `router.json` are present, and
verifies the copy. Keep the printed path: rollback needs it.

```sh
BACKUP=$($SD backup)
echo "$BACKUP"
sudo ls -l "$BACKUP"
```

### A. Overview only

`slim-overview.json` needs no address edits. The other two dashboards are not touched.

```sh
publish_dashboard "$D/slim-overview.json" slim-overview
```

### B. All three (overview, Pi-hole, Router)

`router.json` in the repository shows the example address `192.0.2.20` for the access
point. Substitute the real one on Slim; never commit the real address.

```sh
AP_IP=192.0.2.20   # replace with the real access-point address
publish_dashboard "$D/slim-overview.json" slim-overview
publish_dashboard "$D/pihole.json" pihole
publish_dashboard "$D/router.json" router "$AP_IP"
```

### Check

Grafana re-reads the folder at the provider's update interval (10 seconds unless
`updateIntervalSeconds` is set).

```sh
ls -l $LIVE
curl --fail --silent http://127.0.0.1:3000/api/health
```

Sign in and open each dashboard; use a panel's Inspect > Query view to read any query
error, because a tile that says NO DATA is reporting missing data, not necessarily a
broken query.

### Rollback

Name the dashboards you deployed: `slim-overview` for procedure A, all three for
procedure B. Use the `BACKUP` path printed above, or `sudo ls -d /var/backups/grafana-dashboards-*`
to find it.

```sh
$SD rollback "$BACKUP" slim-overview                    # after procedure A
$SD rollback "$BACKUP" slim-overview pihole router      # after procedure B
```

The script does everything as root and changes nothing unless the checks pass. It
aborts, leaving the live directory as it was, if the backup is missing, not a
directory, unreadable, or not a valid backup (it must hold readable `pihole.json` and
`router.json`, which existed before this change), or if a name is unknown. It restores
each named dashboard that is in the backup. It deletes `slim-overview.json` only after
confirming, as root, that a valid backup does not contain it, so the overview is removed
only if it did not exist before and an earlier copy is put back if there was one.
Restores stage and fsync every replacement before publishing any of them, preserve
existing live ownership/mode (or use Pi-hole as the reference for a missing file),
and replace each file by rename within the live filesystem. Grafana therefore sees
complete files, never an in-place partial write. Temporary files do not end in
`.json`. A staging failure leaves all live files unchanged. Publication across
multiple dashboards is sequential: a later rename, deletion or directory-fsync
failure can leave earlier files changed. Inspect the error and live files before
retrying; this procedure is not a multi-file transaction.
Pi-hole and Router always existed, so they are restored, and a backup missing one of
them aborts rather than deleting it. Grafana removes a provisioned dashboard whose file
is deleted unless `disableDeletion` is set on the provider.

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
the healthchecks.io heartbeat reports the outage externally within about 15 minutes (5-minute pings, 10-minute grace).

## Fixed ASUS log storage

Remote ASUS logs must live on the separate 256 MiB ext4 image mounted at
`/var/log/asus`. Daily logrotate's `maxsize` is only checked when logrotate runs;
it cannot enforce a disk budget between runs. The preallocated image includes
current logs, rotated logs and filesystem metadata in one budget. When it fills,
UDP logs can be lost until space is freed, while the remaining root disk stays
available. The original allowlist and file permissions still apply. Internal
rsyslog errors are rate limited and stderr has a per-process cap; see the
[rsyslog global options](https://docs.rsyslog.com/doc/rainerscript/global.html).

Migration needs a short syslog outage. Take a root-only configuration/state backup
first. Stop rsyslog **and its activating syslog.socket**, the syslog metrics
timer/service and Hermes gateway while
copying so neither incremental reader advances its offset during migration.
Create the image with `sudo slim-syslog-storage create`: it refuses existing,
sparse, wrong-sized or insecure images and requires a 1 GiB free-space reserve.
Formatting disables discard and lazy metadata initialization to preserve that
reservation. Do not run fstrim against this loop filesystem: discarding free image
blocks removes the reservation and the next guard check will refuse it.
Never automatically recreate a missing or damaged image during service startup.

Move the old `/var/log/asus` directory to a timestamped root-only backup location.
Create an **empty root-owned mode 000** directory at `/var/log/asus`; this is the
underlying directory, not the root directory inside the image. Install the mount
unit, rsyslog drop-in and helper from `deploy/slim`, reload systemd, and start
`var-log-asus.mount`. Inside the mounted filesystem, set its root directory to
`syslog:adm` mode 0750 and copy the old directory's contents with ownership and
mode preserved. Do not leave live logs hidden underneath the mount.

Copying changes inodes. Before restarting either reader, map its saved `inode`
to the copied file with the same basename (`asus.log` or `asus.log.1`), preserving
its `offset`, counters, timestamps and history. The metrics state is
`/var/lib/asus-syslog-metrics/state.json`; the Hermes state is
`/home/hermes/network/state/asus-logwatch.json` (confirm the service user's
actual home first). Abort if a saved inode cannot be mapped: finish investigating
rather than resetting counters or replaying old security events.

Install the guarded logrotate file and the updated ASUS rsyslog snippet, replacing
example addresses locally. Validate the full configuration with `sudo rsyslogd
-N1`. Run `sudo slim-syslog-storage check` before starting rsyslog and the readers.
The service Requires/After/BindsTo dependency starts the mount first and stops
rsyslog if it disappears. Its ExecStartPre also rejects a wrong backing image,
filesystem, mount options or size. Logrotate uses the same check before rotating.
Provision the `ASUS log storage over 80%` alert; missing filesystem data also
alerts. Confirm it against the live node-exporter metrics after migration.

Check `findmnt /var/log/asus`, `sudo slim-syslog-storage check`, new log timestamps,
metrics freshness, the watcher offsets, local system logging and service health.
Do not flood the live listener. An isolated Linux root integration test is included:

```sh
sudo unshare --mount --net --propagation private \
  python3 deploy/slim/tests/syslog_storage_linux.py
```

It uses its own mount/network namespaces, a 32 MiB disposable image and loopback
listener. It checks exhaustion, bounded diagnostic output, recovery after freeing
space, rotation, persistence across remount, refusal to overwrite an existing
image and refusal when the mount is absent.

For rollback, stop the same writers/readers, back up their current states and
copy the mounted log contents to a new root-only staging directory. Remap saved
inodes to the copies just as during migration. Remove the rsyslog storage drop-in,
unmount the image, and replace the empty underlying directory with the copied
logs (syslog:adm 0750). Restore the old rsyslog/logrotate configuration and alert
provisioning, reload systemd, then restart the services. Preserve the image and
backups until checked; rollback removes the hard disk budget and reopens the
original resource-exhaustion risk. Never restore pre-migration reader offsets
against post-migration logs.
