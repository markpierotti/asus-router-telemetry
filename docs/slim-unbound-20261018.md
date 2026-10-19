# Slim: Unbound recursive DNS — 2026-10-18

Pi-hole's only upstream is now Unbound on loopback (`127.0.0.1#5335`), replacing
1.1.1.1 and 9.9.9.9. Slim resolves names itself from the root servers and validates
DNSSEC, so no single public resolver sees the household's lookups.

## Configuration

- `deploy/slim/etc/unbound/unbound.conf.d/pi-hole.conf`: loopback only, port 5335,
  IPv4 only (no IPv6 upstream), hardened glue/DNSSEC, prefetch, qname minimisation,
  64 MB message / 128 MB rrset caches, private-address rebinding protection. The
  DNSSEC trust anchor comes from the Ubuntu package's `root-auto-trust-anchor-file.conf`.
- `unbound-resolvconf.service` is disabled so Unbound never becomes Slim's own resolver.
- Monitoring: blackbox module `dns_unbound`, Prometheus job `dns_unbound`
  (`127.0.0.1:5335`), and alert `unbound-down` (critical, 2 minutes, routed to
  Telegram; the message carries the one-line fallback command).
- `slim-backup` includes `/etc/unbound`.

## Verification (2026-10-18 12:28 AEST)

Before Pi-hole was switched, Unbound alone resolved `google.com`, returned SERVFAIL
for `dnssec-failed.org` and NOERROR for `sigok.verteiltesysteme.net`. After the
switch, Pi-hole resolved `google.com`, `ubuntu.com` and `wikipedia.org`, passed the
DNSSEC failure through, and still answered `0.0.0.0` for a blocklisted domain. From a
LAN client: names resolved, `dnssec-failed.org` returned SERVFAIL, and a repeat
lookup took 4 ms from cache (82 ms uncached). The `dns_unbound` probe reported up.
Unbound used about 9 MB of memory at start.

## Rollback

The install script saved the previous files and a `rollback.sh` in a root-only
manual backup directory on Slim. It restores Pi-hole's previous upstreams, the
Prometheus/blackbox/alert/backup files, and stops Unbound. The quickest single-step
fallback is `sudo pihole-FTL --config dns.upstreams '["1.1.1.1","9.9.9.9"]'`.
