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
import math
import os
import signal
import sys
import time
import traceback

from . import COLLECTOR_VERSION, cpu, schema
from .core import (
    Collector,
    OUTCOME_SUCCESS,
    OUTCOME_TEXT,
    default_state,
    is_hard_stop,
)
from .errors import LockHeld, TelemetryError, TransportError
from .mock import ScenarioTransport, SCENARIOS
from .status import render_status, sample_age_seconds
from .store import (
    DEFAULT_MAX_SAMPLE_BYTES,
    DEFAULT_RETAIN_OUTCOME_BYTES,
    DEFAULT_RETAIN_SAMPLE_BYTES,
    Store,
)
from .transport import (
    DEFAULT_ARTIFACT,
    DEFAULT_KNOWN_HOSTS,
    DEFAULT_MAX_STDERR,
    DEFAULT_MAX_STDOUT,
    DEFAULT_CONTROL_PERSIST,
    EXPECT_ARTIFACT_SHA,
    EXPECT_KNOWN_HOSTS_SHA,
    SSHTransport,
)

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_HOST = None
DEFAULT_USER = None

# The loop must not spin: an interval below this would hammer sshd with attempts.
MIN_INTERVAL = 1.0

# Exit codes (documented in the README).
EXIT_OK = 0
EXIT_COLLECTION_FAILURE = 1
EXIT_BAD_OPTION = 2
EXIT_LOCK_HELD = 3
EXIT_CLEANUP_FAILURE = 5
EXIT_EVIDENCE_NOT_PERSISTED = 6
EXIT_INTERRUPTED = 130

_lock_fd = None
_stderr_broken = False


def log(msg):
    """Write one diagnostic line to stderr.

    A closed or broken stderr must NEVER abort teardown bookkeeping: the write
    failure is recorded and swallowed HERE, so no caller -- least of all the
    cleanup path -- can be turned into an exception by an unreadable terminal.
    """
    global _stderr_broken
    try:
        sys.stderr.write("%s\n" % msg)
        sys.stderr.flush()
    except (OSError, ValueError):
        _stderr_broken = True


def stderr_is_broken():
    return _stderr_broken


def validate_interval(interval, control_persist):
    """Reject an interval the loop cannot honour.

    ``--interval 0`` (or a NaN) previously produced a hot loop against sshd, and an
    interval at or above ``ControlPersist`` lets the control master idle-expire
    between attempts -- which is one of the ways the master is lost.
    """
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) \
            or not math.isfinite(interval):
        raise TransportError("--interval must be a finite number of seconds (got %r)"
                             % (interval,))
    if interval < MIN_INTERVAL:
        raise TransportError("--interval must be at least %gs (got %r) - a smaller "
                             "value would hammer the target" % (MIN_INTERVAL, interval))
    if control_persist and interval >= control_persist:
        raise TransportError(
            "--interval (%gs) must be below ControlPersist (%gs): otherwise the ssh "
            "control master idle-expires between attempts and every other attempt "
            "loses its master" % (interval, control_persist))
    return interval


def install_stop_handlers():
    """Route SIGHUP and SIGTERM to the orderly path in BOTH modes.

    Without this, closing the terminal tab (SIGHUP) or ``kill`` (SIGTERM in
    ``--once`` mode) skipped teardown entirely: the remote directory stayed, the
    authenticated master lingered until its idle timeout, the local control-socket
    directory stayed and NOTHING was recorded. SIGINT already raises
    KeyboardInterrupt by default. Returns a list for ``restore_stop_handlers``.
    """
    def _raise(_signum, _frame):
        raise KeyboardInterrupt

    installed = []
    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:                                # pragma: no cover - non-POSIX
            continue
        try:
            installed.append((sig, signal.signal(sig, _raise)))
        except (ValueError, OSError, RuntimeError):    # not the main thread
            pass
    return installed


def restore_stop_handlers(installed):
    for sig, old in installed:
        try:
            signal.signal(sig, old)
        except (ValueError, OSError, RuntimeError):
            pass


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
    store.ensure()
    flags = os.O_CREAT | os.O_RDWR
    # O_NOFOLLOW: the lock path must not be a SYMLINK. Following one would let
    # anything that can write the data directory redirect the open (and the
    # ftruncate below) at an arbitrary file; a symlink here is refused instead.
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(store.lock_path), flags, 0o600)
    except OSError as e:
        raise LockHeld("cannot open the lock file %s: %s" % (store.lock_path, e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LockHeld("another collector run already owns %s" % store.dir)
    try:
        os.chmod(str(store.lock_path), 0o600)
    except OSError:
        pass
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % os.getpid()).encode())
    _lock_fd = fd
    return fd


def release_lock(fd=None):
    """Release the data-directory lock. Scoped release for ``finally`` paths."""
    global _lock_fd
    fd = _lock_fd if fd is None else fd
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    if fd is _lock_fd:
        _lock_fd = None


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
        control_persist=cfg_get(args, cfg, "control_persist", DEFAULT_CONTROL_PERSIST),
        watchdog_selftest=cfg_get(args, cfg, "watchdog_selftest", True),
        watchdog_selftest_bound=cfg_get(args, cfg, "watchdog_selftest_bound", 3),
        interactive_auth=cfg_get(args, cfg, "interactive_auth", None),
        max_stdout_bytes=cfg_get(args, cfg, "max_stdout_bytes", DEFAULT_MAX_STDOUT),
        max_stderr_bytes=cfg_get(args, cfg, "max_stderr_bytes", DEFAULT_MAX_STDERR),
        log=log,
    )
    return tr, "%s@%s" % (tr.user, tr.host)


def build_store(args, cfg):
    return Store(
        cfg_get(args, cfg, "data_dir", DEFAULT_DATA_DIR),
        retain_samples=cfg_get(args, cfg, "retain_samples", 1440),
        retain_outcomes=cfg_get(args, cfg, "retain_outcomes", 5000),
        retain_sample_bytes=cfg_get(args, cfg, "retain_sample_bytes",
                                    DEFAULT_RETAIN_SAMPLE_BYTES),
        retain_outcome_bytes=cfg_get(args, cfg, "retain_outcome_bytes",
                                     DEFAULT_RETAIN_OUTCOME_BYTES),
        max_sample_bytes=cfg_get(args, cfg, "max_sample_bytes", DEFAULT_MAX_SAMPLE_BYTES),
        log=log,
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
    try:
        store.ensure()
    except TelemetryError as e:
        log("FATAL: %s" % e)
        return EXIT_EVIDENCE_NOT_PERSISTED

    interval = float(cfg_get(args, cfg, "interval", 60.0))
    want_loop = bool(getattr(args, "loop", False))
    if want_loop:
        try:
            interval = validate_interval(
                interval, float(cfg_get(args, cfg, "control_persist", DEFAULT_CONTROL_PERSIST)))
        except TelemetryError as e:
            log("FATAL: %s" % e)
            return EXIT_BAD_OPTION

    transport, host_label = build_transport(args, cfg)

    try:
        acquire_lock(store)
    except LockHeld as e:
        log("FATAL: %s" % e)
        return EXIT_LOCK_HELD
    except TelemetryError as e:
        log("FATAL: %s" % e)
        return EXIT_LOCK_HELD

    collector = build_collector(args, cfg, transport, store, host_label)
    # SIGHUP/SIGTERM must reach the SAME orderly path as Ctrl-C in BOTH modes.
    installed = install_stop_handlers()
    rc = EXIT_OK
    try:
        if want_loop:
            rc = run_loop(collector, store, interval)
        else:
            attempt = collector.collect_once()
            report_attempt(attempt)
            rc = EXIT_OK if (attempt["outcome"] == OUTCOME_SUCCESS
                             and attempt.get("cleanup_ok", True)) else EXIT_COLLECTION_FAILURE
    except KeyboardInterrupt:
        log("interrupted: stopping cleanly")
        rc = EXIT_INTERRUPTED
    finally:
        # ---- teardown -------------------------------------------------------
        # The stop handlers stay INSTALLED through the teardown, so a second signal
        # during cleanup lands in the same orderly path instead of killing the
        # process mid-teardown. `collector.close()` records whatever the teardown
        # could not confirm (durably) BEFORE it re-raises, the nested finally
        # guarantees the lock is released regardless, and a cancellation during
        # teardown is neither swallowed nor turned into success.
        try:
            collector.close()
        except KeyboardInterrupt:
            log("teardown was interrupted; cleanup is recorded as UNCERTAIN")
            if rc == EXIT_OK:
                rc = EXIT_INTERRUPTED
        except BaseException as e:                      # noqa: BLE001
            log("teardown raised %s; cleanup is recorded as UNCERTAIN"
                % type(e).__name__)
            if rc == EXIT_OK:
                rc = EXIT_CLEANUP_FAILURE
        finally:
            # scoped release. This MUST be its own finally rather than a statement
            # later in a shared block: that is what leaked the data-dir lock when
            # teardown raised.
            release_lock()
            restore_stop_handlers(installed)
    # A cleanup failure is surfaced separately and never masks an original failure.
    if collector.cleanup_error:
        log("cleanup: SEPARATE FAILURE: %s" % collector.cleanup_error)
        if rc == EXIT_OK:
            rc = EXIT_CLEANUP_FAILURE
    # An UNCONFIRMED control-master shutdown is also surfaced, and also does not mask
    # the original collection failure: the run reports its own outcome AND the fact
    # that an authenticated master may still be running.
    if collector.lifecycle_error:
        log("master shutdown: SEPARATE FAILURE: %s" % collector.lifecycle_error)
        if rc == EXIT_OK:
            rc = EXIT_CLEANUP_FAILURE
    # ... and so is a failure to WRITE the evidence. Never report success when the
    # record could not be stored.
    if collector.persistence_error:
        log("EVIDENCE NOT PERSISTED: %s" % collector.persistence_error)
        if rc == EXIT_OK:
            rc = EXIT_EVIDENCE_NOT_PERSISTED
    return rc


def report_attempt(attempt):
    text = OUTCOME_TEXT.get(attempt["outcome"], attempt["outcome"])
    log("attempt %s outcome=%s (%s) dur=%ss" % (
        attempt["at"], attempt["outcome"], text, attempt["duration_s"]))
    if attempt.get("detail"):
        log("  detail: %s" % attempt["detail"])
    if attempt.get("sample_path"):
        log("  sample: %s" % attempt["sample_path"])
    if attempt.get("remote_dir"):
        log("  remote dir: %s (ownership=%s)" % (
            attempt["remote_dir"], attempt.get("remote_ownership")))
    if attempt.get("cleanup_ok") is False:
        log("  cleanup: NOT CONFIRMED: %s" % attempt.get("cleanup_detail"))


def run_loop(collector, store, interval):
    """Foreground collection loop.

    SIGHUP/SIGTERM are routed to the orderly path by ``install_stop_handlers`` in
    ``cmd_collect`` (both modes), so this function does not install its own.
    """
    log("foreground loop starting: interval=%ss, data dir=%s" % (interval, store.dir))
    log("press Ctrl-C to stop cleanly")
    base = time.monotonic()
    i = 0
    try:
        while True:
            attempt = collector.collect_once()
            report_attempt(attempt)
            if is_hard_stop(attempt["outcome"]):
                # A pinned-identity or artifact-gate failure is not transient:
                # retrying against an endpoint whose identity is not established
                # cannot fix it, so the loop STOPS instead of accumulating evidence
                # of a mismatch it will never recover from by itself.
                log("HARD STOP: %s is not retryable - stopping the loop without further "
                    "attempts" % attempt["outcome"])
                return EXIT_COLLECTION_FAILURE
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
        return EXIT_INTERRUPTED
    finally:
        collector.close()
    return EXIT_OK


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
    # prune REWRITES the outcomes log and unlinks samples: it must take the SAME
    # data-directory lock as collection, so it cannot race a live collector.
    try:
        acquire_lock(store)
    except LockHeld as e:
        log("FATAL: %s" % e)
        return 3
    try:
        n = store.prune()
    finally:
        release_lock()
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
    p.add_argument("--retain-sample-bytes", type=int, default=None,
                   help="byte budget for retained samples (default %d)" % DEFAULT_RETAIN_SAMPLE_BYTES)
    p.add_argument("--retain-outcome-bytes", type=int, default=None,
                   help="byte budget for the outcomes log (default %d)" % DEFAULT_RETAIN_OUTCOME_BYTES)
    p.add_argument("--max-sample-bytes", type=int, default=None,
                   help="per-write sample budget (default %d)" % DEFAULT_MAX_SAMPLE_BYTES)
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
    p.add_argument("--control-persist", dest="control_persist", type=float, default=None,
                   help="seconds an idle control master is kept (default %d); a loop "
                        "interval at or above this is refused" % DEFAULT_CONTROL_PERSIST)
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
                   default=None, help="never take over the terminal for a password prompt. "
                                      "This transport DISABLES public-key authentication "
                                      "(PubkeyAuthentication=no), so with no interactive "
                                      "prompt a NEW master cannot be established at all")
    p.add_argument("--max-stdout-bytes", type=int, default=None,
                   help="device stdout byte ceiling (default %d)" % DEFAULT_MAX_STDOUT)
    p.add_argument("--max-stderr-bytes", type=int, default=None,
                   help="device stderr byte ceiling (default %d)" % DEFAULT_MAX_STDERR)


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
        return EXIT_COLLECTION_FAILURE
    except BrokenPipeError:
        # A closed output pipe must NOT be reported as success: the diagnostics and
        # the exit status are the operator's only evidence that a run happened.
        log("FATAL: output pipe was closed before the run finished")
        return EXIT_COLLECTION_FAILURE
    except Exception:                                   # noqa: BLE001
        log("UNEXPECTED ERROR:\n" + traceback.format_exc())
        return 70
