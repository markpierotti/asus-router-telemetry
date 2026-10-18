#!/usr/bin/env python3
"""Webhook transform for Grafana alert notifications (route "grafana").

Logs every alert (firing and resolved) for the morning digest, then wakes the
agent only when an alert newly fires, or is still firing when Grafana repeats
it after REINVESTIGATE_AFTER (so a follow-up that failed, e.g. during a model
outage, is retried on the next repeat). Otherwise prints nothing and the
webhook is ignored (no model call).

The output carries every alert that fired in the last CORRELATE_WINDOW, and a
constant "group" key the route coalesces on, so related alerts arriving within
a minute of each other become one investigation.
"""
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

STATE = Path.home() / "network" / "state"
LOG = STATE / "grafana-alerts.jsonl"
SEEN = STATE / "grafana-investigated.json"
LOCK = STATE / ".grafana-alert.lock"
MAX_ALERTS = 20
REINVESTIGATE_AFTER = timedelta(hours=3, minutes=30)   # Grafana repeats every 4h
CORRELATE_WINDOW = timedelta(minutes=10)
LOG_MAX_BYTES, LOG_KEEP_DAYS = 1_000_000, 30


def atomic_write(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def append_bounded(records, now):
    with LOG.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    lines = LOG.read_text().splitlines()
    if LOG.stat().st_size > LOG_MAX_BYTES:
        cutoff = now - timedelta(days=LOG_KEEP_DAYS)
        keep = []
        for l in lines:
            try:
                if parse_ts(json.loads(l)["ts"]) >= cutoff:
                    keep.append(l)
            except (ValueError, KeyError, TypeError):
                continue
        lines = keep[-5000:]
        atomic_write(LOG, "".join(l + "\n" for l in lines))
    return lines


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
        return
    now = datetime.now().astimezone()

    with LOCK.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)     # concurrent webhook deliveries
        try:
            seen = json.loads(SEEN.read_text())
            if not isinstance(seen, dict):
                seen = {}
        except (OSError, ValueError):
            seen = {}

        alerts = [a for a in payload["alerts"] if isinstance(a, dict)]
        records, wake = [], False
        for a in alerts[:MAX_ALERTS]:
            labels = a.get("labels") if isinstance(a.get("labels"), dict) else {}
            ann = a.get("annotations") if isinstance(a.get("annotations"), dict) else {}
            rec = {"ts": now.isoformat(timespec="seconds"), "status": a.get("status"),
                   "alertname": labels.get("alertname"), "severity": labels.get("severity"),
                   "startsAt": a.get("startsAt"), "summary": str(ann.get("summary") or "")[:300],
                   "value": str(a.get("valueString") or "")[:300]}
            records.append(rec)
            key = f"{a.get('fingerprint')}|{a.get('startsAt')}"
            last = parse_ts(seen.get(key))
            if rec["status"] == "firing" and (last is None or now - last >= REINVESTIGATE_AFTER):
                seen[key] = rec["ts"]
                wake = True
        if len(alerts) > MAX_ALERTS:
            records.append({"ts": now.isoformat(timespec="seconds"), "status": "note",
                            "alertname": f"{len(alerts) - MAX_ALERTS} more alerts not logged",
                            "severity": None, "startsAt": None, "summary": "", "value": ""})
        lines = append_bounded(records, now)

        horizon = now - timedelta(hours=24)
        seen = {k: v for k, v in seen.items() if (parse_ts(v) or horizon) > horizon}
        atomic_write(SEEN, json.dumps(seen))

    if not wake:
        return
    # Everything that fired recently, so a coalesced run sees the whole incident.
    recent, names = [], set()
    for l in reversed(lines):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        t = parse_ts(r.get("ts"))
        if t is None or now - t > CORRELATE_WINDOW:
            break
        if r.get("status") == "firing" and r.get("alertname") not in names:
            names.add(r.get("alertname"))
            recent.append(r)
    recent.reverse()
    text = "\n".join(f"- {r['alertname']} (severity {r['severity']}), firing since {r['startsAt']}\n"
                     f"  summary: {r['summary']}\n  values: {r['value']}" for r in recent)
    print(json.dumps({"group": "slim", "alerts_text": text, "count": len(recent)}))


if __name__ == "__main__":
    main()
