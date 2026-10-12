"""rtax_telemetry — Mac-side telemetry collector for the ASUS RT-AX56U bench unit.

Foundational layer for a home SOC. Foreground only: it never installs a service,
a cron job, an agent or an LLM call. Healthy collection is pure code — no model
calls are made at any point.

Layout
------
errors      exception taxonomy used to *classify* collection outcomes, plus the
            single sanitiser (`sanitize_detail`) for untrusted device text
schema      validation of the reporter's JSON (schema + required field types)
cpu         CPU utilisation derived from successive raw /proc/stat counters
transport   bounded SSH transport (real), adapted from the reviewed launchers
mock        an offline transport that replays scenarios (no network)
store       timestamped sample + outcome storage with bounded retention
core        the collector: one attempt, outcome classification, incident state
status      the human-readable status command
cli         argument parsing / process entry points
"""

COLLECTOR_VERSION = "1.5.3"
SAMPLE_SCHEMA = "rtax_telemetry.sample/1"
STATE_SCHEMA = "rtax_telemetry.state/1"
