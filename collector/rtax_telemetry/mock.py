"""Offline transport: replays scenarios without touching any network.

Used by the offline validation suite and by ``--transport mock`` demonstrations.
It emits documents shaped exactly like the real reporter (the base document is
the live RT-AX56U sample captured on 2026-10-12), and raises the same exception
types the real transport raises, so the collector's outcome classification and
CPU/incident logic are exercised identically.
"""

import copy
import json
import os
import time

from .errors import (
    RemoteCommandError,
    RemoteTimeout,
    TransportAuthError,
    TransportHostKeyError,
    TransportUnreachableError,
)

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures")

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")

SCENARIOS = (
    "success", "malformed", "wrong_schema", "wrong_type", "timeout",
    "auth", "unreachable", "hostkey", "reporter_failure",
    "reset", "reboot", "zero_elapsed", "nested",
)


def base_doc():
    with open(os.path.join(FIXTURES, "live_sample.json")) as fh:
        return json.load(fh)


class MockSession:
    def __init__(self):
        self.remote_dir = "/tmp/rt.mock"
        self.artifact_sha = "mock"
        self.provisioned = True


class ScenarioTransport:
    """Deterministic offline transport driven by a named scenario."""

    name = "mock"

    def __init__(self, scenario="success", *, interval=60.0, clock=time.time,
                 log=None, fixture=None):
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario %r (known: %s)" % (scenario, ", ".join(SCENARIOS)))
        self.scenario = scenario
        self.interval = interval
        self.clock = clock
        self.log = log or (lambda *a: None)
        self.n = 0
        self._started = 0
        self._base = fixture or base_doc()
        self.started = False
        self.stopped = False

    # ---- session lifecycle ------------------------------------------------
    def start(self):
        if self.scenario == "auth":
            raise TransportAuthError("ssh authentication was refused (mock)")
        if self.scenario == "unreachable":
            raise TransportUnreachableError(
                "management address unreachable (mock): connection refused")
        if self.scenario == "hostkey":
            raise TransportHostKeyError("pinned host key did not match (mock)")
        self.started = True
        return MockSession()

    def collect_sample(self, session, timeout=None):
        i = self.n
        self.n += 1
        s = self.scenario
        if s == "timeout":
            raise RemoteTimeout("reporter run was killed by the watchdog (rc=137) (mock)")
        if s == "reporter_failure":
            raise RemoteCommandError(1, "reporter exited 1: mock failure")
        if s == "malformed":
            return b'{"schema":"router_health/2", oops not json'
        if s == "nested":
            # a small but deeply nested document: must be classified, not crash the
            # interpreter with RecursionError
            return b'{"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
        if s == "wrong_schema":
            d = copy.deepcopy(self._base)
            d["schema"] = "router_health/1"
            return json.dumps(d).encode()
        if s == "wrong_type":
            d = copy.deepcopy(self._base)
            d["memory_kb"]["MemTotal"] = "lots"
            return json.dumps(d).encode()
        if s == "reset":
            # second call: counters DROP toward zero (a real counter reset; /proc
            # counters are unsigned, so they can never go negative) with no uptime
            # drop, so this is a reset, not a reboot
            return json.dumps(self._variant(step=100, uptime=1000.0 + i * self.interval,
                                            reset=(i >= 1))).encode()
        if s == "reboot":
            # uptime drops between samples
            uptime = 1000.0 + i * self.interval
            if i >= 1:
                uptime = 5.0
            return json.dumps(self._variant(step=100, uptime=uptime)).encode()
        if s == "zero_elapsed":
            # identical counters on both samples -> zero elapsed counter time
            return json.dumps(self._variant(step=100,
                                            uptime=1000.0 + i * self.interval)).encode()
        # success: counters and uptime advance every call
        return json.dumps(self._variant(step=100 * (i + 1),
                                        uptime=1000.0 + i * self.interval)).encode()

    def stop(self, session):
        self.stopped = True

    # ---- document synthesis ----------------------------------------------
    def _variant(self, *, step, uptime, freeze=False, reset=False):
        d = copy.deepcopy(self._base)
        d["host"]["uptime_seconds"] = float(format(uptime, ".1f"))
        n = d["cpu"]["count"]
        agg = {"user": 0, "nice": 0, "system": 0, "idle": 0,
               "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        stat = {}
        for idx in range(n):
            key = "cpu%d" % idx
            prev = self._base["cpu_stat"].get(key, agg)
            if reset:
                cur = {f: 5 for f in CPU_FIELDS}       # restarted near zero
            else:
                cur = {f: int(prev.get(f, 0)) + step for f in CPU_FIELDS}
            stat[key] = cur
            for f in CPU_FIELDS:
                agg[f] += cur[f]
        if freeze:
            stat = {k: dict(v) for k, v in self._base["cpu_stat"].items() if k != "cpu"}
            agg = {f: 0 for f in CPU_FIELDS}
            for v in stat.values():
                for f in CPU_FIELDS:
                    agg[f] += v[f]
        stat["cpu"] = agg
        d["cpu_stat"] = stat
        return d
