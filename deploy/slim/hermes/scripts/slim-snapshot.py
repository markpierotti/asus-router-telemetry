#!/usr/bin/env python3
"""Read-only health snapshot of Slim and the home network, as compact text.

Used as the morning digest's pre-run script (its output becomes the agent's
context) and by the agent on demand. Every value comes from local read-only
sources: Prometheus on loopback, systemd, and Hermes' own state files.
"""
import json
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROM = "http://127.0.0.1:9090/api/v1/query"
NET = Path.home() / "network"
NOW = time.time()
SETTINGS = {"slim_ip": None}


def q(expr):
    url = PROM + "?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r)["data"]["result"]
    except Exception as e:  # report, don't crash the digest
        return [{"metric": {"error": str(e)[:80]}, "value": [0, "nan"]}]


def val(expr, default=None):
    r = q(expr)
    try:
        return float(r[0]["value"][1])
    except (IndexError, KeyError, ValueError):
        return default


def ago(ts):
    if not ts:
        return "never"
    s = NOW - ts
    return f"{s/3600:.1f}h ago" if s >= 3600 else f"{s/60:.0f}m ago"


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def tail_lines(path, max_bytes):
    """The last max_bytes of a file as lines (the first, possibly partial, line dropped)."""
    with path.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines


def recent_jsonl(path, hours=24):
    out, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        for line in tail_lines(path, 1_000_000):
            try:
                d = json.loads(line)
                if datetime.fromisoformat(d["ts"]) >= cutoff:
                    out.append(d)
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    return out


def main():
    print(f"SNAPSHOT {datetime.now().astimezone():%a %d %b %Y %H:%M %Z}")

    print("\n[Probes: up now / uptime over 24h]")
    for r in q('probe_success'):
        m = r["metric"]
        up = r["value"][1] == "1"
        avg = val(f'avg_over_time(probe_success{{job="{m.get("job")}",instance="{m.get("instance")}"}}[24h])')
        pct = f"{avg*100:.2f}%" if avg is not None else "?"
        print(f"- {m.get('job')} {m.get('instance')}: {'UP' if up else 'DOWN'}, 24h {pct}")

    print("\n[Slim host]")
    load = val('node_load15')
    mem = val('node_memory_MemAvailable_bytes')
    disk = val('node_filesystem_avail_bytes{mountpoint="/"}')
    temp = val('max(node_hwmon_temp_celsius)')
    boot = val('node_boot_time_seconds')
    print(f"- load15 {load:.2f}" if load is not None else "- load15 ?")
    print(f"- memory available {mem/2**30:.1f} GiB" if mem else "- memory ?")
    print(f"- root disk free {disk/2**30:.1f} GiB" if disk else "- disk ?")
    print(f"- max temperature {temp:.0f}°C" if temp else "- temperature ?")
    print(f"- booted {ago(boot)}")
    failed = sh("systemctl", "--failed", "--no-legend", "--plain")
    print("- failed units: " + (", ".join(l.split()[0] for l in failed.splitlines()) or "none"))
    upd = [l for l in sh("apt", "list", "--upgradable").splitlines() if "/" in l]
    sec = sum("-security" in l for l in upd)
    print(f"- package updates waiting: {len(upd)} ({sec} security)")
    if Path("/var/run/reboot-required").exists():
        print("- REBOOT REQUIRED after updates")

    print("\n[DNS: Pi-hole -> Unbound]")
    qs, bl = val('pihole_queries_24h'), val('pihole_blocked_24h')
    if qs is not None:
        print(f"- 24h queries {qs:.0f}, blocked {bl:.0f} ({(bl or 0)/max(qs,1)*100:.1f}%)")
    up_avg = val('avg(pihole_upstream_reply_seconds_avg_24h)')
    if up_avg is not None:
        print(f"- upstream avg reply {up_avg*1000:.0f} ms")
    top = sorted(q('pihole_top_blocked_domain_24h'), key=lambda r: -float(r["value"][1]))[:3]
    if top:
        print("- top blocked: " + ", ".join(f"{r['metric'].get('domain')} ({r['value'][1]})" for r in top))
    gravity = val('pihole_gravity_updated_timestamp_seconds')
    print(f"- blocklists updated {ago(gravity)}")

    print("\n[DNS clients in 24h]")
    try:
        cfg = json.loads((NET / "known-devices.json").read_text())
        devices = cfg.get("devices") if isinstance(cfg.get("devices"), list) else []
        known = {str(d["ip"]): str(d.get("name", "?")) for d in devices if isinstance(d, dict) and d.get("ip")}
        SETTINGS["slim_ip"] = cfg.get("slim_ip") if isinstance(cfg.get("slim_ip"), str) else None
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
        known = {}
        print(f"- (known-devices.json unreadable: {type(e).__name__}; every client shows UNKNOWN)")
    for r in sorted(q('pihole_client_queries_24h'), key=lambda r: -float(r["value"][1])):
        ip = r["metric"].get("client")
        tag = known.get(ip) or ("Slim itself" if ip == "127.0.0.1" else "UNKNOWN")
        print(f"- {ip} {tag}: {float(r['value'][1]):.0f} queries")

    print("\n[ASUS AP (rtax collector)]")
    last = val('rtax_last_sample_timestamp_seconds')
    print(f"- last telemetry sample {ago(last)}")
    for name, expr, fmt in [("uptime", 'rtax_router_uptime_seconds', lambda v: f"{v/86400:.1f} days"),
                            ("cpu", 'rtax_router_cpu_util_percent', lambda v: f"{v:.0f}%"),
                            ("consecutive collector failures", 'rtax_consecutive_failures', lambda v: f"{v:.0f}"),
                            ("incident open", 'rtax_incident_open', lambda v: "yes" if v else "no")]:
        v = val(f"max({expr})")
        if v is not None:
            print(f"- {name}: {fmt(v)}")

    print("\n[Backups]")
    b = val('slim_backup_last_success_timestamp_seconds')
    size = val('slim_backup_last_size_bytes')
    print(f"- last nightly backup {ago(b)}" + (f", {size/2**20:.0f} MiB" if size else ""))

    print("\n[Grafana alerts in 24h]")
    alerts = recent_jsonl(NET / "state" / "grafana-alerts.jsonl")
    if not alerts:
        print("- none")
    for a in alerts[-15:]:
        print(f"- {a['ts'][11:16]} {a.get('status')} {a.get('alertname')} ({a.get('severity')})")

    print("\n[ASUS log events in 24h]")
    ev = recent_jsonl(NET / "state" / "asus-events.jsonl")
    if not ev:
        print("- none")
    for e in ev[-15:]:
        print(f"- {e['ts'][11:16]} {e['type']} {e.get('mac') or e.get('at') or e.get('line', '')[:120]}")
    try:
        lines = tail_lines(Path("/var/log/asus/asus.log"), 4_000_000)
        cutoff = (datetime.now().astimezone() - timedelta(hours=24)).isoformat()
        day = [l for l in lines if l[:19] >= cutoff[:19]]
        noisy = sum("dropbear" in l and bool(SETTINGS["slim_ip"]) and SETTINGS["slim_ip"] in l for l in day)
        print(f"- {len(day)} log lines in 24h ({noisy} are Slim's own SSH probes)")
    except OSError as e:
        print(f"- ASUS log unreadable: {e}")


if __name__ == "__main__":
    main()
