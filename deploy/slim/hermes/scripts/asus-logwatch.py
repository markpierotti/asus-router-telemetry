#!/usr/bin/env python3
"""ASUS syslog watcher (Hermes no-agent cron job, every minute).

Reads lines appended to /var/log/asus/asus.log since the last run and prints a
short Telegram message only for notable events: an unknown Wi-Fi device (first
time it is seen), failed logins, logins from anywhere but Slim, and the AP
restarting. Prints nothing when there is nothing to report (silent tick).
No model calls. Log text is treated as data only.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path(os.environ.get("ASUS_LOG", "/var/log/asus/asus.log"))
NET = Path.home() / "network"
STATE = NET / "state" / "asus-logwatch.json"
EVENTS = NET / "state" / "asus-events.jsonl"
KNOWN = NET / "known-devices.json"
MAX_BYTES = 2_000_000          # read at most this much per file per run
EVENTS_MAX_BYTES = 1_000_000   # derived history is trimmed past this size...
EVENTS_KEEP_DAYS = 30          # ...to this many days
SEEN_MAX = 2000                # remembered unknown MACs (private addresses rotate)

MAC_RE = re.compile(r"\b(?:STA|Assoc|ReAssoc|Auth|Deauth|Disassoc)\b[^0-9a-fA-F]{0,12}"
                    r"((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})")
FAILED_RE = re.compile(r"(?i)bad password|login fail|failed password|auth(?:entication)? fail"
                       r"|invalid user|wrong password|unauthori[sz]ed")
OK_LOGIN_RE = re.compile(r"(?i)(?:password|pubkey) auth succeeded|login success|logged in")
IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
RESTART_RE = re.compile(r"klogd started|kernel: Linux version|syslogd started")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def append_bounded(path, records):
    """Append JSONL records; once the file passes EVENTS_MAX_BYTES keep only recent ones."""
    if not records:
        return
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    if path.stat().st_size <= EVENTS_MAX_BYTES:
        return
    cutoff = datetime.now().astimezone() - timedelta(days=EVENTS_KEEP_DAYS)
    keep = []
    for l in path.read_text().splitlines():
        try:
            if datetime.fromisoformat(json.loads(l)["ts"]) >= cutoff:
                keep.append(l)
        except (ValueError, KeyError, TypeError):
            continue
    keep = keep[-5000:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(l + "\n" for l in keep))
    os.replace(tmp, path)


def read_chunk(path, off):
    """Read whole lines from off, at most MAX_BYTES. Returns (bytes, new_offset, at_eof)."""
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(off)
        data = f.read(MAX_BYTES)
    if not data.endswith(b"\n"):
        nl = data.rfind(b"\n")
        if nl >= 0:
            data = data[:nl + 1]          # leave the partial line for the next run
        elif len(data) < MAX_BYTES:
            data = b""                    # a single unfinished line: wait for it
    new_off = off + len(data)
    return data, new_off, new_off >= size


def read_new(state):
    """New text since the saved position. A rotated file is finished before moving on."""
    try:
        st = LOG.stat()
    except FileNotFoundError:
        return ""
    ino, off = state.get("inode"), state.get("offset", 0)
    old_text = b""
    if ino is not None and ino != st.st_ino:
        old = LOG.with_name(LOG.name + ".1")
        try:
            ost = old.stat()
        except OSError:
            ost = None
        if ost is not None and ost.st_ino == ino and off < ost.st_size:
            old_text, new_off, done = read_chunk(old, off)
            if not done:                  # more of the old file remains: stay on it
                state["offset"] = new_off
                return old_text.decode("utf-8", "replace")
        off = 0
    elif st.st_size < off:                # truncated in place
        off = 0
    data, new_off, _ = read_chunk(LOG, off)
    state["inode"], state["offset"] = st.st_ino, new_off
    return (old_text + data).decode("utf-8", "replace")


def is_random(mac):
    return int(mac.split(":")[0], 16) & 0x02 == 0x02


def hhmm(line):
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0]).strftime("%-I:%M%p").lower()
    except ValueError:
        return "?"


def main():
    (NET / "state").mkdir(parents=True, exist_ok=True)
    state = load_json(STATE, {})
    first_run = not state  # the backlog's own restart/login lines are old news
    cfg = load_json(KNOWN, {})
    slim_ip = cfg.get("slim_ip")
    known = {d["mac"].lower() for d in cfg.get("devices", []) if isinstance(d, dict) and d.get("mac")}
    seen = state.get("seen_unknown", [])
    seen_set = set(seen)
    text = read_new(state)

    new_devices, failed, logins, restarts = {}, [], [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        for mac in MAC_RE.findall(line):
            mac = mac.lower()
            if mac not in known and mac not in seen_set and mac not in new_devices:
                new_devices[mac] = hhmm(line)
        if FAILED_RE.search(line):
            failed.append(line)
        elif OK_LOGIN_RE.search(line):
            ips = IP_RE.findall(line)
            if not ips or any(ip != slim_ip for ip in ips):
                logins.append(line)
        if RESTART_RE.search(line):
            restarts.append(hhmm(line))

    if first_run:
        failed, logins, restarts = [], [], []
    msg = []
    if new_devices:
        msg.append("📡 New device on the ASUS Wi-Fi")
        for mac, t in list(new_devices.items())[:10]:
            kind = "private/random address" if is_random(mac) else "fixed address"
            msg.append(f"• {t}  {mac} ({kind})")
        if len(new_devices) > 10:
            msg.append(f"• …and {len(new_devices) - 10} more")
        msg.append("Not in your known-devices list. Tell me what it is and I'll note it.")
    if failed:
        msg.append(f"🔐 {len(failed)} failed login attempt(s) on the ASUS")
        msg.extend("• " + l.split(" ", 1)[-1][:160] for l in failed[-3:])
    if logins:
        msg.append("🔑 Login to the ASUS from somewhere other than Slim")
        msg.extend("• " + l.split(" ", 1)[-1][:160] for l in logins[-3:])
    if restarts:
        msg.append(f"🔄 ASUS restarted (or restarted its logging) at {restarts[-1]}")

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    append_bounded(EVENTS,
                   [{"ts": now, "type": "new_device", "mac": m} for m in new_devices]
                   + [{"ts": now, "type": "failed_login", "line": l[:300]} for l in failed[-50:]]
                   + [{"ts": now, "type": "login", "line": l[:300]} for l in logins[-50:]]
                   + [{"ts": now, "type": "restart", "at": t} for t in restarts[-5:]])

    state["seen_unknown"] = (seen + list(new_devices))[-SEEN_MAX:]
    save_json(STATE, state)
    if msg:
        print("\n".join(msg))


if __name__ == "__main__":
    sys.exit(main())
