# Slim: ASUS access-point syslog — 2026-10-18

The ASUS access point now sends its system log to Slim over UDP 514. Slim keeps it
in `/var/log/asus/asus.log`, separate from Slim's own logs, so router events
(Wi-Fi associations, DHCP/DNS helpers, logins, reboots) survive the AP's small
in-memory log and its restarts.

## Configuration

- `deploy/slim/etc/rsyslog.d/10-asus-remote.conf`: rsyslog `imudp` bound to Slim's
  LAN address only, `$AllowedSender` limited to the ASUS address, and a dedicated
  ruleset that writes only lines from the ASUS and then stops, so remote lines never
  reach `/var/log/syslog`. Lines are stamped with Slim's arrival time because the
  AP's clock may be unset before NTP sync.
- ufw: `514/udp ALLOW from <ASUS address>` (comment `ASUS-syslog`). No other host
  may send.
- `deploy/slim/etc/logrotate.d/asus-remote`: weekly, 8 rotations, early rotation at
  50 MB, compressed.
- `slim-backup` includes `/etc/rsyslog.d` and `/etc/logrotate.d/asus-remote`. The
  log itself is not backed up.
- rsyslog was not previously installed on Slim (journald only). Installing it also
  enables Ubuntu's default local files under `/var/log`.
- ASUS side: System Log → General Log → Remote Log Server = Slim's address, port 514.

## Verification (2026-10-18 14:35 AEST)

`rsyslogd -N1` validated the configuration; rsyslog listened on Slim's LAN address,
UDP 514. A test line sent from Slim itself was not written to `asus.log`. After the
AP setting was applied, `asus.log` received the AP's `klogd started` line, followed
by `dropbear` connect/exit pairs from Slim's blackbox SSH probe — expected on every
probe interval, and a convenient liveness signal for the log stream.

## Rollback

The install script saved the previous ufw rules and `slim-backup`, plus a
`rollback.sh`, in a root-only manual backup directory on Slim. It removes the
firewall rule and both config files, restores `slim-backup`, and stops rsyslog.
Clear the Remote Log Server field on the AP as well.
