#!/usr/bin/env python3
"""Webhook transform for Grafana alert notifications (route "grafana").

Logs every alert (firing and resolved) for the morning digest, then wakes the
agent only for alerts that newly started firing. Repeat notifications for an
alert already investigated, and resolved-only notifications, print nothing so
the webhook is ignored (no model call).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

STATE = Path.home() / "network" / "state"
LOG = STATE / "grafana-alerts.jsonl"
SEEN = STATE / "grafana-investigated.json"


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        seen = json.loads(SEEN.read_text())
    except (OSError, ValueError):
        seen = {}

    fresh = []
    with LOG.open("a") as f:
        for a in payload.get("alerts", [])[:20]:
            labels, ann = a.get("labels", {}), a.get("annotations", {})
            rec = {"ts": now, "status": a.get("status"), "alertname": labels.get("alertname"),
                   "severity": labels.get("severity"), "startsAt": a.get("startsAt"),
                   "summary": (ann.get("summary") or "")[:300]}
            f.write(json.dumps(rec) + "\n")
            key = f"{a.get('fingerprint')}|{a.get('startsAt')}"
            if a.get("status") == "firing" and key not in seen:
                seen[key] = now
                fresh.append(rec | {"value": (a.get("valueString") or "")[:300]})

    # keep the dedupe table small
    seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-200:])
    SEEN.write_text(json.dumps(seen))
    if not fresh:
        return
    text = "\n".join(f"- {r['alertname']} (severity {r['severity']}), firing since {r['startsAt']}\n"
                     f"  summary: {r['summary']}\n  values: {r['value']}" for r in fresh)
    print(json.dumps({"alerts_text": text, "count": len(fresh)}))


if __name__ == "__main__":
    main()
