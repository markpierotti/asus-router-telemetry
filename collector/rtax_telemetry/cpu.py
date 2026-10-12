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
    "reboot"       host rebooted (see detect_reboot)  -> util null
    "reset"        a counter went backwards           -> util null
    "zero_elapsed" delta_total <= 0                    -> util null
    "unavailable"  cpu_stat missing / no comparable key
    "ok"           a real percentage in [0, 100]

A counter "reset" is judged per field EXCEPT the idle group (`idle` + `iowait`),
which is judged as a sum: a tickless SMP kernel can reclassify time between those
two without losing it, so `idle` dropping on its own is not a reset. A genuine
reset moves the other fields too.

Reboot detection prefers the EXPLICIT boot identity (`host.boot_time`, from
/proc/stat's `btime`) over a wall-clock heuristic; see `detect_reboot`.

`interval_seconds` is the LOCAL MONOTONIC delta between the two samples; it is
recorded for provenance and is never used to derive the percentage (the counters
already carry the elapsed time).

A truncated cpu_stat block carries an explicit `"_truncated": true` marker (the
reporter could not serialise every per-CPU entry within its own bound). The marker
is reported in the derived block and is NEVER treated as a counter, so the
aggregate — which the kernel always lists first and the reporter therefore keeps —
is still compared honestly and no counter is fabricated.
"""

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
IDLE_FIELDS = ("idle", "iowait")

# Per-field reset detection EXCLUDES the idle group, which is judged as a SUM.
# A tickless SMP kernel can move time between `idle` and `iowait` between two
# readings (the same CPU time, reclassified), so `idle` alone dropping is not a
# counter reset. A genuine reset drops `user`/`system`/... as well, which keeps the
# per-field test effective. (Source-derived HARDENING of an unreproduced hypothesis:
# recorded iowait was 0 on the bench unit, so this is latent there.)
_RESET_FIELDS = tuple(f for f in CPU_FIELDS if f not in IDLE_FIELDS)

# How far the wall-clock estimate of the boot instant may drift between two samples
# before it is treated as a reboot, when no explicit boot identity is available.
_BOOT_SHIFT_TOL = 30.0


def total_jiffies(cpu):
    return sum(int(cpu.get(f, 0)) for f in CPU_FIELDS)


def idle_jiffies(cpu):
    return sum(int(cpu.get(f, 0)) for f in IDLE_FIELDS)


def _is_reset(prev, cur):
    """True when a counter went backwards (a reset), judged per the note above."""
    if any(int(cur.get(f, 0)) < int(prev.get(f, 0)) for f in _RESET_FIELDS):
        return True
    return idle_jiffies(cur) < idle_jiffies(prev)


def _pair(prev, cur):
    if prev is None or cur is None:
        return {"state": "unavailable", "util_percent": None}
    if _is_reset(prev, cur):
        return {"state": "reset", "util_percent": None,
                "delta_total": None, "delta_idle": None, "delta_iowait": None}
    dt = total_jiffies(cur) - total_jiffies(prev)
    di = idle_jiffies(cur) - idle_jiffies(prev)
    diow = int(cur.get("iowait", 0)) - int(prev.get("iowait", 0))
    if dt <= 0:
        return {"state": "zero_elapsed", "util_percent": None,
                "delta_total": dt, "delta_idle": di, "delta_iowait": diow}
    # Ratio FIRST, then scale: `100.0 * (dt - di)` would convert the (possibly
    # very large) integer delta to a C double and raise OverflowError for a
    # value beyond float range. `(dt - di) / dt` is a ratio in [0, 1] that true
    # division evaluates safely even for arbitrary-precision ints.
    util = 100.0 * ((dt - di) / dt)
    util = max(0.0, min(100.0, util))      # clamp against a partial reset
    return {"state": "ok", "util_percent": round(util, 2),
            "delta_total": dt, "delta_idle": di, "delta_iowait": diow}


def _boot_time(doc):
    """The explicit boot identity from a reporter document, if it has one."""
    v = (doc or {}).get("host", {}).get("boot_time")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def detect_reboot(prev_doc, cur_doc, *, prev_at=None, cur_at=None):
    """True when the host rebooted between two samples.

    PREFERRED test — the EXPLICIT boot identity. The reporter reads ``btime`` from
    /proc/stat and emits it as ``host.boot_time`` (epoch seconds). Two samples taken
    in the same boot carry the same value whatever the clocks did, and two samples
    from different boots carry different values, so this needs no heuristic. It is
    the primary test whenever BOTH samples expose it.

    FALLBACKS when ``boot_time`` is unavailable (a reporter built before it existed,
    or an unreadable /proc/stat):

      1. uptime moving backwards; and
      2. the wall-clock estimate of the boot instant, ``collected_at - uptime``,
         shifting between the two samples by more than ``_BOOT_SHIFT_TOL``.

    Both fallbacks are HEURISTICS and are documented as such: a wall-clock-only
    signal inherits clock-step and measurement-latency ambiguity.

    Every comparison is written so it stays safe for arbitrary-size integers: a
    subtraction involving a float operand would coerce a huge int to a C double and
    raise OverflowError. Integer-vs-float COMPARISON never coerces, so the float case
    is phrased as ``cu + 1.0 < pu``.
    """
    pb, cb = _boot_time(prev_doc), _boot_time(cur_doc)
    if pb is not None and cb is not None:
        return cb != pb

    pu = (prev_doc or {}).get("host", {}).get("uptime_seconds")
    cu = (cur_doc or {}).get("host", {}).get("uptime_seconds")
    if pu is None or cu is None:
        return False
    if cu < pu:
        if isinstance(pu, float) or isinstance(cu, float):
            if cu + 1.0 < pu:
                return True
        elif (pu - cu) > 1.0:
            return True
    if prev_at is None or cur_at is None:
        return False
    try:
        shift = (cur_at - cu) - (prev_at - pu)
        return abs(shift) > _BOOT_SHIFT_TOL
    except (OverflowError, TypeError):
        return False


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
        "truncated": bool(isinstance(cur_stat, dict) and cur_stat.get("_truncated") is True),
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
    # "_truncated" is a marker, not a CPU entry: never compared as a counter.
    for key in sorted(k for k in cur_stat if k != "_truncated"):
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
