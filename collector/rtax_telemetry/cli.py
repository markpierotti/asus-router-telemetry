"""Command-line entry points.

    collector.py collect [--once|--loop] [options]   one sample, or a foreground loop
    collector.py status  [options]                    last attempt / sample / incident
    collector.py prune   [options]                    apply bounded retention now
    collector.py validate FILE                        validate a raw reporter document
    collector.py selftest                             built-in offline checks

Healthy collection makes NO model calls and NO network access beyond the pinned
ssh transport. Nothing is installed as a service, cron job or background job.
"""

import argparse
import fcntl
import json
import os
import sys
import time
import traceback

from . import COLLECTOR_VERSION, cpu, schema
from .core import (
    Collector,
    OUTCOME_SUCCESS,
    OUTCOME_TEXT,
    default_state,
)
from .errors import LockHeld, TelemetryError, TransportError
from .mock import ScenarioTransport, SCENARIOS
from .status import render_status, sample_age_seconds
from .store import Store
from .transport import (
    DEFAULT_ARTIFACT,
    DEFAULT_KNOWN_HOSTS,
    EXPECT_ARTIFACT_SHA,
    EXPECT_KNOWN_HOSTS_SHA,
    SSHTransport,
)

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_HOST = None
DEFAULT_USER = None

_lock_fd = None


def log(msg):
    sys.stderr.write("%s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------- config
def load_config(data_dir):
    path = os.path.join(os.path.expanduser(str(data_dir or DEFAULT_DATA_DIR)), "config.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            raise ValueError("config.json must be an object")
        return cfg
    except (OSError, ValueError) as e:
        log("WARNING: ignoring unreadable config.json (%s)" % e)
        return {}


def cfg_get(args, cfg, name, default):
    v = getattr(args, name, None)
    if v is not None:
        return v
    if name in cfg:
        return cfg[name]
    return default


# ----------------------------------------------------------------------- lock
def acquire_lock(store):
    global _lock_fd
    store.dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(store.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LockHeld("another collector run already owns %s" % store.dir)
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % os.getpid()).encode())
    _lock_fd = fd
    return fd


# ------------------------------------------------------------------ transport
def build_transport(args, cfg):
    if args.transport == "mock":
        return ScenarioTransport(args.mock_scenario, interval=cfg_get(args, cfg, "interval", 60.0),
                                 log=log), None
    if not cfg_get(args, cfg, "host", DEFAULT_HOST) or not cfg_get(args, cfg, "user", DEFAULT_USER):
        raise TransportError("configure host and user explicitly before live collection")
    tr = SSHTransport(
        host=cfg_get(args, cfg, "host", DEFAULT_HOST),
        user=cfg_get(args, cfg, "user", DEFAULT_USER),
        artifact=cfg_get(args, cfg, "artifact", DEFAULT_ARTIFACT),
        known_hosts=cfg_get(args, cfg, "known_hosts", DEFAULT_KNOWN_HOSTS),
        artifact_sha=cfg_get(args, cfg, "artifact_sha", EXPECT_ARTIFACT_SHA),
        known_hosts_sha=cfg_get(args, cfg, "known_hosts_sha", EXPECT_KNOWN_HOSTS_SHA),
        ssh_bin=cfg_get(args, cfg, "ssh", "ssh"),
        connect_timeout=cfg_get(args, cfg, "connect_timeout", 8),
        run_bound=cfg_get(args, cfg, "run_bound", 15),
        watchdog_selftest=cfg_get(args, cfg, "watchdog_selftest", True),
        watchdog_selftest_bound=cfg_get(args, cfg, "watchdog_selftest_bound", 3),
        interactive_auth=cfg_get(args, cfg, "interactive_auth", None),
        log=log,
    )
    return tr, "%s@%s" % (tr.user, tr.host)


def build_store(args, cfg):
    return Store(
        cfg_get(args, cfg, "data_dir", DEFAULT_DATA_DIR),
        retain_samples=cfg_get(args, cfg, "retain_samples", 1440),
        retain_outcomes=cfg_get(args, cfg, "retain_outcomes", 5000),
    )


def build_collector(args, cfg, transport, store, host_label):
    return Collector(
        transport, store,
        open_after=cfg_get(args, cfg, "open_after", 3),
        resolve_after=cfg_get(args, cfg, "resolve_after", 2),
        artifact_sha=getattr(transport, "artifact_sha", None),
        host_label=host_label,
        log=log,
    )


# ---------------------------------------------------------------------- commands
def cmd_collect(args):
    cfg = load_config(args.data_dir)
    store = build_store(args, cfg)
    store.ensure()

    interval = float(cfg_get(args, cfg, "interval", 60.0))
    transport, host_label = build_transport(args, cfg)

    try:
        acquire_lock(store)
    except LockHeld as e:
        log("FATAL: %s" % e)
        return 3

    collector = build_collector(args, cfg, transport, store, host_label)
    rc = 0
    try:
        if args.loop:
            rc = run_loop(collector, store, interval)
        else:
            attempt = collector.collect_once()
            report_attempt(attempt)
            rc = 0 if (attempt["outcome"] == OUTCOME_SUCCESS
                       and attempt.get("cleanup_ok", True)) else 1
    finally:
        collector.close()
    # A cleanup failure is surfaced separately and never masks an original failure.
    if collector.cleanup_error:
        log("cleanup: SEPARATE FAILURE: %s" % collector.cleanup_error)
        if rc == 0:
            rc = 5
    return rc


def report_attempt(attempt):
    text = OUTCOME_TEXT.get(attempt["outcome"], attempt["outcome"])
    log("attempt %s outcome=%s (%s) dur=%ss" % (
        attempt["at"], attempt["outcome"], text, attempt["duration_s"]))
    if attempt.get("detail"):
        log("  detail: %s" % attempt["detail"])
    if attempt.get("sample_path"):
        log("  sample: %s" % attempt["sample_path"])
    if attempt.get("cleanup_ok") is False:
        log("  cleanup: FAILED: %s" % attempt.get("cleanup_detail"))


def run_loop(collector, store, interval):
    import signal

    log("foreground loop starting: interval=%ss, data dir=%s" % (interval, store.dir))
    log("press Ctrl-C to stop cleanly")
    base = time.monotonic()
    i = 0

    def _term(_signum, _frame):
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, _term)
    try:
        while True:
            attempt = collector.collect_once()
            report_attempt(attempt)
            i += 1
            target = base + i * interval
            now = time.monotonic()
            if target <= now:
                # we overran the interval: realign and continue immediately
                base = now
                i = 0
                continue
            remaining = target - now
            # sleep in small steps so Ctrl-C is prompt
            while remaining > 0:
                time.sleep(min(0.5, remaining))
                remaining = target - time.monotonic()
    except KeyboardInterrupt:
        log("interrupted (Ctrl-C): stopping cleanly")
        return 130
    finally:
        signal.signal(signal.SIGTERM, old_term)
        collector.close()
    return 0


def cmd_status(args):
    cfg = load_config(args.data_dir)
    store = build_store(args, cfg)
    state = store.load_state() or default_state(
        cfg_get(args, cfg, "open_after", 3), cfg_get(args, cfg, "resolve_after", 2))
    host = None
    if args.transport != "mock":
        host = "%s@%s" % (cfg_get(args, cfg, "user", DEFAULT_USER),
                          cfg_get(args, cfg, "host", DEFAULT_HOST))
    print(render_status(store, state, host=host, transport_name=args.transport))
    age = sample_age_seconds(store, time.time())
    if age is None:
        print("  sample age          : (no valid sample yet)")
    return 0


def cmd_prune(args):
    cfg = load_config(args.data_dir)
    store = build_store(args, cfg)
    n = store.prune()
    log("pruned %d record(s); samples now %d, outcomes now %d" % (
        n, store.sample_count(), len(store.outcomes())))
    return 0


def cmd_validate(args):
    path = args.file
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        log("cannot read %s: %s" % (path, e))
        return 2
    try:
        doc = schema.parse(raw)
        schema.validate(doc)
    except TelemetryError as e:
        log("INVALID: %s" % e)
        return 1
    print("VALID router_health document: schema=%s tool=%s version=%s" % (
        doc.get("schema"), doc.get("tool"), doc.get("version")))
    return 0


def cmd_selftest(args):
    failures = []
    # 1. the CPU formula, on a hand-computed pair
    prev = {"user": 100, "nice": 0, "system": 50, "idle": 850,
            "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
    cur = {"user": 200, "nice": 0, "system": 100, "idle": 1700,
           "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
    # total delta = 1000, idle delta = 850 -> util = 100*150/1000 = 15.0
    got = cpu._pair(prev, cur)
    if not (got["state"] == "ok" and abs(got["util_percent"] - 15.0) < 1e-9):
        failures.append("cpu formula: %r" % got)
    # 2. guest is not double counted: adding a guest field changes nothing
    cur2 = dict(cur, guest=999, guest_nice=999)
    got2 = cpu._pair(prev, cur2)
    if got2["util_percent"] != got["util_percent"]:
        failures.append("cpu formula double-counts guest: %r vs %r" % (got, got2))
    # 3. a counter reset yields null, not a fake number
    if cpu._pair(prev, dict(cur, idle=1))["state"] != "reset":
        failures.append("reset not detected")
    # 4. the schemas accept the real captured sample
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures",
                               "live_sample.json"), "rb") as fh:
            schema.parse_and_validate(fh.read())
    except Exception as e:                              # noqa: BLE001
        failures.append("fixture validation: %s" % e)
    if failures:
        for f in failures:
            log("SELFTEST FAIL: %s" % f)
        return 1
    print("selftest OK (cpu formula, guest handling, reset detection, schema)")
    return 0


# -------------------------------------------------------------------- argparse
def add_common(p):
    p.add_argument("--data-dir", default=None,
                   help="data directory (default: %s)" % DEFAULT_DATA_DIR)
    p.add_argument("--retain-samples", type=int, default=None)
    p.add_argument("--retain-outcomes", type=int, default=None)
    p.add_argument("--open-after", type=int, default=None)
    p.add_argument("--resolve-after", type=int, default=None)


def add_transport(p):
    p.add_argument("--transport", choices=("ssh", "mock"), default="ssh")
    p.add_argument("--mock-scenario", choices=SCENARIOS, default="success")
    p.add_argument("--host", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--ssh", default=None, help="ssh binary to use (default: ssh)")
    p.add_argument("--artifact", default=None)
    p.add_argument("--known-hosts", dest="known_hosts", default=None)
    p.add_argument("--connect-timeout", type=int, default=None)
    p.add_argument("--run-bound", type=int, default=None, help="seconds allowed for the reporter")
    p.add_argument("--watchdog-selftest", dest="watchdog_selftest", action="store_true",
                   default=None, help="prove the remote watchdog bounds before use (default on)")
    p.add_argument("--no-watchdog-selftest", dest="watchdog_selftest", action="store_false",
                   default=None, help="skip the remote watchdog self-test")
    p.add_argument("--watchdog-selftest-bound", type=int, default=None,
                   help="seconds for the watchdog self-test (default 3)")
    p.add_argument("--interactive-auth", dest="interactive_auth", action="store_true",
                   default=None, help="force interactive terminal authentication")
    p.add_argument("--no-interactive-auth", dest="interactive_auth", action="store_false",
                   default=None, help="never take over the terminal (key auth only)")


def build_parser():
    ap = argparse.ArgumentParser(prog="collector.py",
                                 description="Mac-side telemetry collector for the ASUS RT-AX56U")
    ap.add_argument("--version", action="version",
                    version="rtax_telemetry %s" % COLLECTOR_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="collect one sample, or run a foreground loop")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="collect a single sample (default)")
    g.add_argument("--loop", action="store_true", help="foreground collection loop")
    c.add_argument("--interval", type=float, default=None, help="loop interval seconds (default 60)")
    add_common(c)
    add_transport(c)
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("status", help="show last attempt / sample / incident")
    add_common(s)
    s.add_argument("--transport", choices=("ssh", "mock"), default="ssh")
    s.set_defaults(func=cmd_status)

    p = sub.add_parser("prune", help="apply bounded retention now")
    add_common(p)
    p.set_defaults(func=cmd_prune)

    v = sub.add_parser("validate", help="validate a raw router_health document")
    v.add_argument("file")
    v.set_defaults(func=cmd_validate)

    t = sub.add_parser("selftest", help="built-in offline checks")
    t.set_defaults(func=cmd_selftest)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except TelemetryError as e:
        log("FATAL: %s" % e)
        return 1
    except BrokenPipeError:
        return 0
    except Exception:                                   # noqa: BLE001
        log("UNEXPECTED ERROR:\n" + traceback.format_exc())
        return 70
