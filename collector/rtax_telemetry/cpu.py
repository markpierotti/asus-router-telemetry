"""CPU utilisation from successive raw /proc/stat counters.

FORMULA (documented once, here — requirement 7)
-----------------------------------------------
The reporter (`router_health`) exposes eight cumulative per-CPU jiffie counters,
straight from /proc/stat:

    user  nice  system  idle  iowait  irq  softirq  steal

Two deliberate decisions, both required for correctness:

1. guest / guest_nice are NOT summed. On Linux the kernel already includes
   `guest` time inside `user` and `guest_nice` inside `nice`. Adding a separate
   guest term would count guest time TWICE. The reporter does not emit guest
   fields, so the eight values above are the complete, non-overlapping
   accounting, and the formula below cannot double-count guest time.

2. Utilisation comes from the DIFFERENCE of two successive samples. A single
   cumulative reading says nothing on its own; two readings bracket an interval.

For consecutive samples with counters prev, cur:

    total(c)     = user + nice + system + idle + iowait + irq + softirq + steal
    idle(c)      = idle + iowait            (iowait is idle CPU time waiting on I/O)
    delta_total  = total(cur) - total(prev)
    delta_idle   = idle(cur)  - idle(prev)
    util_percent = 100 * (delta_total - delta_idle) / delta_total

Degenerate cases are reported EXPLICITLY, never silently as 0:

    "first"        no previous sample                 -> util null
    "reboot"       host uptime went backwards         -> util null
    "reset"        a counter went backwards           -> util null
    "zero_elapsed" delta_total <= 0                    -> util null
    "unavailable"  cpu_stat missing / no comparable key
    "ok"           a real percentage in [0, 100]

`interval_seconds` is the LOCAL MONOTONIC delta between the two samples; it is
recorded for provenance and is never used to derive the percentage (the counters
already carry the elapsed time).
"""

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
IDLE_FIELDS = ("idle", "iowait")


def total_jiffies(cpu):
    return sum(int(cpu.get(f, 0)) for f in CPU_FIELDS)


def idle_jiffies(cpu):
    return sum(int(cpu.get(f, 0)) for f in IDLE_FIELDS)


def _pair(prev, cur):
    if prev is None or cur is None:
        return {"state": "unavailable", "util_percent": None}
    if any(int(cur.get(f, 0)) < int(prev.get(f, 0)) for f in CPU_FIELDS):
        return {"state": "reset", "util_percent": None,
                "delta_total": None, "delta_idle": None, "delta_iowait": None}
    dt = total_jiffies(cur) - total_jiffies(prev)
    di = idle_jiffies(cur) - idle_jiffies(prev)
    diow = int(cur.get("iowait", 0)) - int(prev.get("iowait", 0))
    if dt <= 0:
        return {"state": "zero_elapsed", "util_percent": None,
                "delta_total": dt, "delta_idle": di, "delta_iowait": diow}
    util = 100.0 * (dt - di) / dt
    util = max(0.0, min(100.0, util))      # clamp against a partial reset
    return {"state": "ok", "util_percent": round(util, 2),
            "delta_total": dt, "delta_idle": di, "delta_iowait": diow}


def detect_reboot(prev_doc, cur_doc):
    """True when the host uptime moved backwards between two samples."""
    pu = (prev_doc or {}).get("host", {}).get("uptime_seconds")
    cu = (cur_doc or {}).get("host", {}).get("uptime_seconds")
    if pu is None or cu is None:
        return False
    return cu < pu - 1.0


def compare(prev_stat, cur_stat, *, reboot=False, interval_seconds=None):
    """Derive utilisation for a pair of cpu_stat blocks.

    prev_stat / cur_stat are the reporter's ``cpu_stat`` objects (or None for the
    first sample). Returns a JSON-serialisable ``derived`` block.
    """
    out = {
        "source": "successive raw cpu_stat counters",
        "formula": ("util_percent = 100*(delta_total-delta_idle)/delta_total; "
                    "idle = idle+iowait; guest/guest_nice excluded because Linux "
                    "already accounts them inside user/nice"),
        "interval_seconds": interval_seconds,
        "state": "first" if prev_stat is None else None,
        "aggregate": None,
        "per_cpu": {},
    }

    if prev_stat is None:
        return out
    if cur_stat is None:
        out["state"] = "unavailable"
        return out
    if reboot:
        out["state"] = "reboot"
        return out

    states = []
    for key in sorted(cur_stat):
        pc = _pair(prev_stat.get(key), cur_stat.get(key))
        if key == "cpu":
            out["aggregate"] = pc
        else:
            out["per_cpu"][key] = pc
        states.append(pc["state"])

    if any(s == "reset" for s in states):
        out["state"] = "reset"
    elif all(s == "zero_elapsed" for s in states):
        out["state"] = "zero_elapsed"
    elif any(s == "ok" for s in states):
        out["state"] = "ok"
    else:
        out["state"] = "unavailable"
    return out
