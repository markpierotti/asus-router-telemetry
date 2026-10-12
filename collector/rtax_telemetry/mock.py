"""Offline transport: replays scenarios without touching any network.

Used by the offline validation suite and by ``--transport mock`` demonstrations.
It emits documents shaped exactly like the real reporter (the base document is
the live RT-AX56U sample captured on 2026-10-12), and raises the same exception
types the real transport raises, so the collector's outcome classification and
CPU/incident logic are exercised identically.

Scenarios added for the review remediation:

  provision_failure   the session cannot be provisioned; the raised exception
                      carries the same remote_dir / ownership / cleanup facts the
                      real transport attaches
  master_lost         the multiplexed control master disappeared
  control_byte        the device answers its own known-answer text with a single
                      0x1C byte (the IndexError reproducer)
  null_sections       /proc/cpuinfo and /proc/loadavg were unreadable, so the
                      reporter emits "cpu":null and "loadavg":null
  boot_change         a reboot whose LATER sample shows a HIGHER uptime, so only the
                      explicit boot identity can detect it
  reboot_legacy       the same reboot expressed by falling uptime and no boot_time
                      (the documented heuristic fallback)
"""

import copy
import json
import os
import time

from .errors import (
    ControlMasterLost,
    ProvisioningError,
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
    "reset", "reboot", "zero_elapsed", "nested", "huge_number",
    "provision_failure", "master_lost", "control_byte", "null_sections",
    "boot_change", "reboot_legacy",
)

# The boot identity the mock reports. Fixed so "success" and the other
# same-boot scenarios agree, and changed only by the reboot scenarios.
_BOOT_TIME = 1_700_000_000.0
# Sentinel: "use the mock's fixed boot identity" (as opposed to None = omit it).
_KEEP_BOOT = object()


def base_doc():
    with open(os.path.join(FIXTURES, "live_sample.json")) as fh:
        return json.load(fh)


class MockSession:
    def __init__(self, remote_dir="/tmp/rt.mock", ownership="none"):
        self.remote_dir = remote_dir
        self.artifact_sha = "mock"
        self.provisioned = True
        self.ownership = ownership


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
        s = self.scenario
        if s == "auth":
            raise TransportAuthError("ssh authentication was refused (mock)")
        if s == "unreachable":
            raise TransportUnreachableError(
                "management address unreachable (mock): connection refused")
        if s == "hostkey":
            raise TransportHostKeyError("pinned host key did not match (mock)")
        if s == "provision_failure":
            e = ProvisioningError(
                "provisioning step 'exclusive mkdir' failed: remote command exceeded "
                "its bound (mock)")
            # Same shape as the real transport: one dict so the PRESENCE of the facts
            # is unambiguous, plus the historic individual attributes.
            setattr(e, "provision_facts", {
                "remote_dir": "/tmp/rt.mockprov",
                "remote_ownership": "unknown",
                "cleanup_ok": False,
                "cleanup_detail": (
                    "remote dir /tmp/rt.mockprov may exist but its creation was never "
                    "acknowledged (ownership=unknown) (mock)"),
            })
            setattr(e, "remote_dir", "/tmp/rt.mockprov")
            setattr(e, "remote_ownership", "unknown")
            setattr(e, "provision_cleanup_ok", False)
            setattr(e, "provision_cleanup_detail", (
                "remote dir /tmp/rt.mockprov may exist but its creation was never "
                "acknowledged (ownership=unknown) (mock)"))
            raise e
        if s == "master_lost":
            raise ControlMasterLost(
                "ssh control master has gone away (mock); refusing to fall back to a "
                "fresh connection")
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
        if s == "control_byte":
            # the exact IndexError reproducer: a single control byte, which the
            # WHITESPACE-based split of the decoded text renders as no token at all
            return b"\x1c"
        if s == "nested":
            # a small but deeply nested document: must be classified, not crash the
            # interpreter with RecursionError
            return b'{"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
        if s == "huge_number":
            # an integer too large to convert to a float: must be classified as
            # schema_invalid, never crash a numeric check (OverflowError)
            d = copy.deepcopy(self._base)
            d["host"]["uptime_seconds"] = 10 ** 400
            return json.dumps(d).encode()
        if s == "wrong_schema":
            d = copy.deepcopy(self._base)
            d["schema"] = "router_health/1"
            return json.dumps(d).encode()
        if s == "wrong_type":
            d = copy.deepcopy(self._base)
            d["memory_kb"]["MemTotal"] = "lots"
            return json.dumps(d).encode()
        if s == "null_sections":
            d = copy.deepcopy(self._base)
            d["cpu"] = None
            d["loadavg"] = None
            d["host"]["boot_time"] = _BOOT_TIME
            return json.dumps(d).encode()
        if s == "reset":
            # second call: counters DROP toward zero (a real counter reset; /proc
            # counters are unsigned, so they can never go negative) with no uptime
            # drop, so this is a reset, not a reboot
            return json.dumps(self._variant(step=100, uptime=1000.0 + i * self.interval,
                                            reset=(i >= 1))).encode()
        if s == "reboot":
            # a reboot expressed by the EXPLICIT boot identity; the counters keep
            # advancing, so only boot_time reveals it
            boot = _BOOT_TIME if i == 0 else _BOOT_TIME + 3600.0
            return json.dumps(self._variant(step=100, uptime=1000.0 + i * self.interval,
                                            boot_time=boot)).encode()
        if s == "reboot_legacy":
            # the same reboot without any boot identity: uptime drops
            uptime = 1000.0 + i * self.interval
            if i >= 1:
                uptime = 5.0
            return json.dumps(self._variant(step=100, uptime=uptime, boot_time=None)).encode()
        if s == "boot_change":
            # a reboot where the SECOND sample shows a HIGHER uptime and larger
            # counters throughout - the case a falling-uptime heuristic cannot see
            if i == 0:
                return json.dumps(self._variant(step=100, uptime=100.0,
                                                boot_time=_BOOT_TIME)).encode()
            return json.dumps(self._variant(step=1000, uptime=500.0,
                                            boot_time=_BOOT_TIME + 7200.0)).encode()
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
    def _variant(self, *, step, uptime, freeze=False, reset=False, boot_time=_KEEP_BOOT):
        d = copy.deepcopy(self._base)
        d["host"]["uptime_seconds"] = float(format(uptime, ".1f"))
        if boot_time is _KEEP_BOOT:
            d["host"]["boot_time"] = _BOOT_TIME
        elif boot_time is None:
            d["host"].pop("boot_time", None)
        else:
            d["host"]["boot_time"] = float(boot_time)
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
