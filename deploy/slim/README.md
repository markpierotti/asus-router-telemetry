# Slim home lab deployment examples

Sanitized references for the Ubuntu 24.04 deployment recorded on
[17 October 2026](../../docs/slim-deployment-20261017.md). The tree mirrors installed
paths for inspection. **Do not copy the whole tree over `/`.** Configure and test
one component at a time. These files have not been redeployed by this update.

## Source and substitution

[SOURCE_MANIFEST.json](SOURCE_MANIFEST.json) identifies local backup, Claude
commissioning artifacts and reconstructed snippets. These are recovered source
examples, not a byte-for-byte snapshot of the current host. Third-party community
dashboards 1860 and 7587 are deliberately not redistributed; import them separately
and select the provisioned `prometheus` datasource.

| Example | Meaning |
| --- | --- |
| `192.0.2.10` | Slim host |
| `192.0.2.20` | ASUS access point |
| `192.0.2.1` | Main gateway |
| `192.0.2.0/24` | LAN CIDR |
| `enp1s0` | Slim Ethernet interface |
| `/Users/YOUR_USER` | Mac user's home directory |

The documentation-range addresses are not working LAN defaults. Replace them
consistently in Prometheus, alert rules, dashboards, Grafana and Mac scripts. The
`rtax` account and `/home/rtax` paths are a deployment convention and can be changed
consistently. No actual host keys, collector config, private keys or client inventory
are supplied. Set and store web-administration credentials privately.

## Components

| Source | Installed purpose |
| --- | --- |
| `usr/local/bin/pihole-exporter` | Read-only Pi-hole SQLite metrics, loopback port 9617 |
| `usr/local/bin/rtax-exporter` | Read-only collector state and history metrics, loopback port 9618 |
| `etc/prometheus/` and `etc/default/` | Scrapes, probes and loopback listeners; 30-day / 2 GB retention |
| `etc/grafana/provisioning/` | Datasource, dashboard provider and 13 alert rules |
| `var/lib/grafana/dashboards/` | Custom Slim overview, Pi-hole and ASUS dashboards (deploy and rollback: [operations runbook](../../docs/slim-operations.md)) |
| `etc/systemd/system/` | Collector/exporter units, backup timer and ICMP capability setup |
| `usr/local/bin/slim-backup*` | Private archive creation and restricted SSH download protocol |
| `macos/` | Daily backup pull and launchd example |
| `etc/pihole/` and `etc/grafana/*.example` | Minimal configuration excerpts to merge manually |

## Setup sequence

1. Establish reliable wired networking, a DHCP reservation, tested SSH access and a
   recovery path. Install and configure Pi-hole, Prometheus, node/blackbox exporters,
   Grafana and Python 3.11+ using your distribution and vendor instructions. Preserve
   existing package configuration. This tree is not a package installer.
2. Create the dedicated unprivileged `rtax` account and application, data, pinned
   artifact and key directories. Deploy this repository's source at `/home/rtax/app`.
   Keep the private key at `/home/rtax/keys/id_ed25519` (`0600`), its directory
   `/home/rtax/keys` (`0700`), and both owned by `rtax`. Start with
   [collector/config.example.json](../../collector/config.example.json); populate a
   private `/home/rtax/data/config.json` with the actual target, trusted pins, artifact
   digest and `identity_file`. Set `interactive_auth` false. Do not commit that file.
3. Verify the router host identity independently. Install the collector's public key
   through trusted router administration and verify it saved in full. Run one-shot
   key collection as `rtax` before enabling the service. This service example
   requires collector **1.6.2** (`RestartPreventExitStatus=4 2`); do not install it
   with deployed 1.6.0, which uses exit 1 for hard stops. Never publish the private key
   or replace verification with an automatic key scan.
4. Install reviewed scripts under `/usr/local/bin` as root-owned executables and
   units under `/etc/systemd/system`. Allow `rtax` to write its private data directory;
   the collector unit makes the rest of the filesystem read-only. Install the
   Pi-hole exporter names example as `/etc/pihole-exporter/names` only after replacing
   its values. The Pi-hole exporter runs as `pihole` and needs database read access.
5. Merge Prometheus settings, substitute probe targets and run `promtool check config`
   on the resulting configuration. Keep all five monitoring endpoints on loopback:
   Prometheus 9090, node 9100, blackbox 9115, Pi-hole 9617 and router 9618. The blackbox
   unit drop-in grants only the capability required for its ICMP probe binary.
6. Install Grafana provisioning and the two custom dashboards. Merge the server
   excerpt, set a private admin password and restrict port 3000 to your trusted LAN.
   Pi-hole administration/DNS, SSH and optional xrdp need their own reviewed LAN rules.
   Do not expose metrics or administrative pages through WAN port forwarding.
7. Reload systemd and enable the collector/exporters after their prerequisites pass.
   Restart/reload monitoring components and inspect their journals, scrape targets,
   panels and rule evaluation. Alert rules contain no contact point or token;
   notification delivery requires separate private configuration.
8. For backups, create `/var/backups/slim` with access for the `slim` group. Ensure
   `/var/lib/prometheus/node-exporter` exists and is the node exporter's textfile
   directory. Validate one manual backup and archive contents before enabling
   `slim-backup.timer`. It uses 03:30 plus up to 15 minutes of random delay and keeps 14.
9. On the Mac, create a separate backup-pull key and install its public half on Slim
   with a forced command and source restriction, for example:

   ```text
   command="/usr/local/bin/slim-backup-serve",restrict,from="192.0.2.0/24" ssh-ed25519 YOUR_BACKUP_PUBLIC_KEY
   ```

   Independently establish Slim's SSH host identity in the Mac's known-hosts file.
   Put the reviewed pull script at `~/.local/bin/pull-slim-backups`, make it executable,
   substitute the host and username, and customize the example launchd plist's paths
   before loading it. The job runs at 09:00; the script keeps 30 local archives.
   Confirm `list` and a backup download succeed and unrelated forced commands fail.

## Alert behavior

| Rule | Trigger | Pending period |
| --- | --- | --- |
| Pi-hole DNS | DNS probe fails | 2 minutes |
| Gateway / ASUS | Respective ping probe fails | 3 minutes |
| Internet | All configured internet probe series report failure | 5 minutes |
| Host disk | Root disk above 85% | 10 minutes |
| Host memory | Available memory below 10% | 10 minutes |
| Backup age | Last archive success over 30 hours ago | 10 minutes |
| Blocklist age | Gravity update over 9 days ago | 1 hour |
| Collector stale | Last attempt over 5 minutes old | 2 minutes |
| Collection incident | Collector incident state open | Immediate |
| Router reboot | Boot-time identity changes in a 15-minute window | Immediate |
| Router memory | Available memory below 10% | 15 minutes |
| Scrape target | Target `up` is zero | 5 minutes |

The collector opens an incident after three failures and resolves after two
successes. Rules have explicit no-data handling; inspect their actual provisioning
before adapting them. The internet rule operates on present series and is not a
quorum guarantee when series disappear. A stale-attempt rule does not establish
sample freshness or cleanup completeness. Missing optional reporter memory fields
can leave a memory rule without data. These limits matter when interpreting alerts.

## Operational limits

The custom exporters reflect the commissioned implementation. They are small
stdlib services, not general-purpose exporters: Pi-hole uses its v6 SQLite schema;
router outcome counts examine a bounded 4 MiB tail, so a large log can undercount
24-hour totals. They read local trusted state and do not sandbox it. Pi-hole metrics
include client identities and DNS domains, and query figures lag database flushing.
Do not publish live metrics, exports or unsanitized dashboard screenshots.

Backups contain private application databases and possibly secrets. Never commit
archives. The backup config tar tolerates read failures, and the Mac pull script can
finish successfully after an individual transfer failure. Verify contents and logs;
neither exit status alone proves recoverability. The collector private key at `/home/rtax/keys/id_ed25519` and Prometheus history
are excluded. Key exclusion depends on its location outside the archived data and
pinned directories; moving it into either would include it in backups. See [operations and recovery](../../docs/slim-operations.md).

The screen-rotation helper is optional and specific to an inverted local monitor.
It skips headless and xrdp sessions. This tree does not configure network renderers,
household DNS cutover, BIOS recovery, remote desktop or Telegram credentials.
