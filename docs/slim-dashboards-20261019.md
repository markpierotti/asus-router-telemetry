# Slim dashboards: overview and restyle, 19 October 2026

## Deployment acceptance — 19 October 2026

The dashboards were deployed from `9491997` to Slim and visually checked in
authenticated Grafana 13.2.4 at desktop width and a 400-pixel viewport. The earlier
source-only deployment limitations below describe the original implementation
session and are superseded by this acceptance record.

- All 45 dashboard/deployment tests passed as root on Slim, with no skips.
- All 54 dashboard targets ran against live Prometheus and returned series without
  query errors: 21 overview, 15 Pi-hole and 18 Router targets. These counts reflect
  the deployed JSON, rather than the earlier session's reported totals.
- Overview, Pi-hole and Router panels rendered; navigation, legends and the narrow
  layout were inspected. The screenshots contain private Pi-hole details and are
  retained privately.
- Existing community dashboards and alerting, Prometheus and notification
  configuration were unchanged by dashboard deployment. The later syslog storage
  security fix is a separate PR #5 change.
- The ASUS ping target was substituted only in the live Router JSON. The public
  source keeps its documentation address.
- A root-owned, mode-0750 timestamped backup and the root-run `slim-dashboards`
  helper provide rollback. Dashboard changes are already live; merging records
  their reviewed source.


Branch `add-slim-overview-dashboard`. Adds the **Slim overview** dashboard and
restyles the existing Pi-hole and Router dashboards. Dashboard UIDs, titles, panel
ids and layout are unchanged. No alert rule, notification route, collector, DNS or
network configuration changed.

## What changed

- **Slim overview** (`slim-overview`): host CPU, memory, root filesystem and uptime;
  Pi-hole, internet and router-collector status; charts for CPU by mode, memory,
  network, DNS rate, latency and disk I/O. Queries name the real jobs (`node`,
  `pihole`, `ping`, `http`, `dns_pihole`, `router`) and use no template variables.
- **Router collector tile** reports four states: OK, FAILING, STALE and UNAVAILABLE.
  OK requires every input to be present and good: the router scrape `up` and
  `rtax_exporter_up` both exactly 1, the last attempt no more than five minutes old
  (the "Router collector stopped" alert threshold), `rtax_last_attempt_success` 1 and
  `rtax_incident_open` 0. A missing input can therefore never produce OK.
  Precedence is UNAVAILABLE (scrape or exporter down or absent), then STALE (attempt
  older than five minutes), then FAILING (failed attempt or open incident), then OK.
  The tile is not "UNAVAILABLE whenever anything is missing": a missing freshness
  metric together with a failed attempt or an open incident reports FAILING, because
  a failure signal is still present. A missing success, freshness or incident metric
  with no failure signal falls through to UNAVAILABLE. An earlier revision tested only
  for zero values, so a missing health, freshness or incident metric could fall
  through to OK.
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
- The Router collector expression was evaluated for all 324 combinations of
  scrape `up` (missing, 0, 1), exporter health (missing, 0, 1), attempt age (missing,
  30, 300, 301 seconds), success (missing, 0, 1) and incident (missing, 0, 1), by
  substituting synthetic series into the real expression and running it on the live
  Prometheus engine. The states matched the offline evaluator in every case
  (2 OK, 15 FAILING, 9 STALE, 298 UNAVAILABLE).
- The CPU tile and the stacked CPU chart total returned the same value from the
  same data (4.621 against 4.621 at the time of the check).
- The `df`-style and size-based root filesystem formulas differ by the
  root-reserved blocks (54.59% against 56.93% at the time of the check).
- `make test-deployment` includes static checks (`test_dashboards.py`) and behavioral
  state tests (`test_collector_state.py`, using the subset evaluator in
  `promql_mini.py`) covering each metric missing on its own, exporter and scrape
  failure, stale and failed attempts, open incidents, recovery and state precedence.
  The state tests fail on the previous expression, which let missing metrics read OK.

## Rollback helper

`usr/local/bin/slim-dashboards` runs backup and rollback entirely as root, because the
backup directory is root-owned 0750 and an unprivileged existence check cannot tell
"absent" from "permission denied". Its tests run the real script as an unprivileged
user against a root-owned backup with a sudo stand-in that does not elevate, and require
an abort with the live dashboards untouched; they also cover the root-side behaviour
(restore, delete only when absent from a valid backup, abort on a missing, empty or
invalid backup or an unknown name). With a naive version of the script those tests
fail. Not covered: a real `sudo` password prompt or sudoers policy, and any run on Slim.

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
