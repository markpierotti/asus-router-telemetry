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
from datetime import datetime
from pathlib import Path

LOG = Path(os.environ.get("ASUS_LOG", "/var/log/asus/asus.log"))
NET = Path.home() / "network"
STATE = NET / "state" / "asus-logwatch.json"
EVENTS = NET / "state" / "asus-events.jsonl"
KNOWN = NET / "known-devices.json"
SLIM_IP = "192.0.2.10"
MAX_BYTES = 2_000_000  # never read more than this per run

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


def read_new(state):
    """Return new text since the saved offset, following rotation to asus.log.1."""
    try:
        st = LOG.stat()
    except FileNotFoundError:
        return ""
    chunks = []
    ino, off = state.get("inode"), state.get("offset", 0)
    if ino is not None and ino != st.st_ino:
        old = LOG.with_name(LOG.name + ".1")
        try:
            if old.stat().st_ino == ino:
                with old.open("rb") as f:
                    f.seek(off)
                    chunks.append(f.read(MAX_BYTES))
        except OSError:
            pass
        off = 0
    elif st.st_size < off:
        off = 0
    with LOG.open("rb") as f:
        f.seek(off)
        chunks.append(f.read(MAX_BYTES))
        state["offset"] = f.tell()
    state["inode"] = st.st_ino
    return b"".join(chunks).decode("utf-8", "replace")


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
    known = {d["mac"].lower() for d in load_json(KNOWN, {}).get("devices", []) if d.get("mac")}
    seen = set(state.get("seen_unknown", []))
    text = read_new(state)

    new_devices, failed, logins, restarts = {}, [], [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        for mac in MAC_RE.findall(line):
            mac = mac.lower()
            if mac not in known and mac not in seen and mac not in new_devices:
                new_devices[mac] = hhmm(line)
        if FAILED_RE.search(line):
            failed.append(line)
        elif OK_LOGIN_RE.search(line):
            ips = IP_RE.findall(line)
            if not ips or any(ip != SLIM_IP for ip in ips):
                logins.append(line)
        if RESTART_RE.search(line):
            restarts.append(hhmm(line))

    if first_run:
        failed, logins, restarts = [], [], []
    msg = []
    if new_devices:
        msg.append("📡 New device on the ASUS Wi-Fi" + ("s" if len(new_devices) > 1 else ""))
        for mac, t in new_devices.items():
            kind = "private/random address" if is_random(mac) else "fixed address"
            msg.append(f"• {t}  {mac} ({kind})")
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
    with EVENTS.open("a") as ev:
        for mac in new_devices:
            ev.write(json.dumps({"ts": now, "type": "new_device", "mac": mac}) + "\n")
        for l in failed:
            ev.write(json.dumps({"ts": now, "type": "failed_login", "line": l[:300]}) + "\n")
        for l in logins:
            ev.write(json.dumps({"ts": now, "type": "login", "line": l[:300]}) + "\n")
        for t in restarts:
            ev.write(json.dumps({"ts": now, "type": "restart", "at": t}) + "\n")

    state["seen_unknown"] = sorted(seen | set(new_devices))
    save_json(STATE, state)
    if msg:
        print("\n".join(msg))


if __name__ == "__main__":
    sys.exit(main())
