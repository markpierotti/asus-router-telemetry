# Slim dashboards: overview and restyle, 19 October 2026

Branch `add-slim-overview-dashboard`. Adds the **Slim overview** dashboard and
restyles the existing Pi-hole and Router dashboards. Dashboard UIDs, titles, panel
ids and layout are unchanged. No alert rule, notification route, collector, DNS or
network configuration changed.

## What changed

- **Slim overview** (`slim-overview`): host CPU, memory, root filesystem and uptime;
  Pi-hole, internet and router-collector status; charts for CPU by mode, memory,
  network, DNS rate, latency and disk I/O. Queries name the real jobs (`node`,
  `pihole`, `ping`, `http`, `dns_pihole`, `router`) and use no template variables.
- **Router collector tile** reports four states: OK, FAILING (last attempt failed or
  an incident is open), STALE (no attempt for over five minutes, the same threshold
  as the "Router collector stopped" alert) and UNAVAILABLE (exporter down or no
  data). Missing data falls through to UNAVAILABLE, never to OK.
- **Status and headline tiles use instant queries** and are gated on the scrape
  job's own `up` and the exporter's `*_exporter_up`, so a dead exporter blanks the
  tile (NO DATA) within a scrape interval instead of leaving its last value on screen.
  Router tiles derived from the newest sample also require that sample to be under
  five minutes old, because the exporter keeps serving the last valid sample after
  the collector stops.
- **CPU** tile and "CPU by mode" chart use the same basis, busy time divided by all
  CPU time across cores, so the stacked chart total equals the tile and stays within
  0-100. Averaging idle alone did not match the sum of the other modes on this host
  (about one percentage point apart).
- **Root filesystem** uses the `df` formula, used / (used + available), instead of
  1 - available / size, which counts root-reserved blocks as used (about 2 points
  higher on this host). The existing "slim disk over 85%" alert still uses the older
  formula; it was deliberately not changed here.
- Time series leave gaps for missing data rather than bridging them. Pi-hole and
  Router panels share the overview's style; the Router panels fed by the last
  sample show gaps when that sample is stale.
- All three dashboards gain a "Home lab" dashboard-links dropdown.

## Verified

Through a signed-in Grafana (version 13.2.4) and its query API, read-only:

- 22 overview queries and 33 Pi-hole and Router queries ran without error and
  returned live series.
- The Router collector expression was exercised with forced variants of its
  conditions, not only the live OK case.
- The CPU tile and the stacked CPU chart total returned the same value from the
  same data (4.621 against 4.621 at the time of the check).
- The `df`-style and size-based root filesystem formulas differ by the
  root-reserved blocks (54.59% against 56.93% at the time of the check).
- `make test-deployment` includes new static checks (`test_dashboards.py`) that fail
  on the previous dashboard versions.

## Not verified

- **Deployment to Slim and visual rendering.** The session could not write to Slim
  or open the new dashboards in Grafana, so layout, legends, colours and narrow-width
  behavior have not been seen. Run the deploy steps in
  [slim-operations.md](slim-operations.md) and inspect each dashboard before relying on it.
- The root filesystem value was not compared with `df -h /` on the host. `df`
  rounds Use% up to a whole number.
- Panel-level query errors in the browser UI (the Inspect view), as opposed to
  API results.
- Dashboard JSON was not imported into Grafana; schema acceptance is untested.

## Privacy

The repository files contain queries and styling only. Pi-hole panels display client
names and queried domains when rendered; keep rendered dashboards and screenshots
private unless they are sanitized.
