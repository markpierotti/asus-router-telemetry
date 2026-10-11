#!/usr/bin/env python3
"""Transport hardening regression tests — offline.

These go beyond canned fake-SSH responses: they exercise the process model
(controlling terminal, interruption, reaping) and run the REAL remote command
text under a local POSIX shell to measure timing and confirm the watchdog
actually bounds and actually exits promptly.

No network I/O, no router contact.
"""

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import transport                                      # noqa: E402
from rtax_telemetry.core import Collector                                 # noqa: E402
from rtax_telemetry.errors import (                                       # noqa: E402
    CleanupError, TransportError, TransportUnreachableError,
)
from rtax_telemetry.store import Store                                    # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-hard.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, name="data"):
        s = Store(os.path.join(self.tmp, name))
        s.ensure()
        return s


# ============================================================ process model
class TestProcessGroupAndTTY(unittest.TestCase):
    """Fix 1: the child must keep this SESSION (so /dev/tty resolves for the ssh
    password prompt) while getting its OWN process group (so it can be killed)."""

    PROBE = (
        "import os\n"
        "print('sid=%d' % os.getsid(0))\n"
        "print('pgid=%d' % os.getpgid(0))\n"
        "print('pid=%d' % os.getpid())\n"
        "try:\n"
        "    os.close(os.open('/dev/tty', os.O_RDONLY)); print('tty=open')\n"
        "except OSError as e:\n"
        "    print('tty=errno%d' % e.errno)\n"
    )

    def test_child_keeps_session_and_gets_own_process_group(self):
        rc, out, err = transport.run_bounded([sys.executable, "-c", self.PROBE], 10)
        self.assertEqual(rc, 0, err)
        vals = dict(line.split("=", 1) for line in out.decode().split() if "=" in line)
        self.assertEqual(int(vals["sid"]), os.getsid(0),
                         "child must stay in this session so /dev/tty still resolves")
        self.assertEqual(int(vals["pgid"]), int(vals["pid"]),
                         "child must be its own process-group leader")
        self.assertNotEqual(int(vals["pgid"]), os.getpgid(0))

    def test_dev_tty_reachable_when_parent_has_one(self):
        try:
            os.close(os.open("/dev/tty", os.O_RDONLY))
        except OSError:
            self.skipTest("no controlling terminal in this environment")
        rc, out, _ = transport.run_bounded([sys.executable, "-c", self.PROBE], 10)
        self.assertIn(b"tty=open", out,
                      "the child must retain the controlling terminal")

    def test_start_new_session_would_detach(self):
        # documents WHY process_group is used instead of start_new_session
        import subprocess
        p = subprocess.run([sys.executable, "-c", self.PROBE],
                           capture_output=True, text=True, start_new_session=True)
        vals = dict(line.split("=", 1) for line in p.stdout.split() if "=" in line)
        self.assertEqual(int(vals["sid"]), int(vals["pid"]),
                         "setsid makes the child its own session leader (ctty lost)")


class TestInterruption(unittest.TestCase):
    """Fix 2: KeyboardInterrupt must terminate AND reap the owned child group."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-int.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keyboard_interrupt_kills_and_reaps_child(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        helper = os.path.join(self.tmp, "child.sh")
        with open(helper, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'echo $$ > "%s"\n'
                     'exec sleep 30\n' % pidfile)
        os.chmod(helper, 0o755)

        timer = threading.Timer(0.6, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            with self.assertRaises(KeyboardInterrupt):
                transport.run_bounded(["sh", helper], 30)
        finally:
            timer.cancel()

        self.assertTrue(os.path.exists(pidfile), "helper never started")
        with open(pidfile) as fh:
            pid = int(fh.read().strip())

        deadline = time.monotonic() + 3
        gone = False
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.05)
        self.assertTrue(gone, "child process leaked after KeyboardInterrupt")

    def test_timeout_kills_and_reaps_child(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        helper = os.path.join(self.tmp, "child.sh")
        with open(helper, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'echo $$ > "%s"\n'
                     'exec sleep 30\n' % pidfile)
        os.chmod(helper, 0o755)
        t0 = time.monotonic()
        with self.assertRaises(transport.RemoteTimeout):
            transport.run_bounded(["sh", helper], 1)
        self.assertLess(time.monotonic() - t0, 5)
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
        deadline = time.monotonic() + 3
        gone = False
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                gone = True
                break
            time.sleep(0.05)
        self.assertTrue(gone, "child process leaked after a timeout")


# ======================================================= real remote command
class TestRemoteWatchdogLocally(TempCase):
    """Fix 4 (and 5): run the REAL remote command text under a local shell.

    A near-instant reporter must return promptly (the watchdog must not hold the
    channel), and a slow reporter must be killed AT the bound.
    """

    FAST = '#!/bin/sh\necho \'{"schema":"x"}\'\n'
    SLOW = '#!/bin/sh\nexec sleep 30\n'   # exec: $p IS the sleep, so killing it kills all

    def test_fast_reporter_returns_promptly(self):
        rd = harness.remote_dir(self.tmp, self.FAST)
        script = transport.run_cmd(rd, 10)
        t0 = time.monotonic()
        p = harness.run_local_script(script, self.tmp, timeout=20)
        dt = time.monotonic() - t0
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('{"schema":"x"}', p.stdout)
        self.assertLess(dt, 3.0,
                        "a fast reporter must NOT wait for the watchdog (%0.1fs)" % dt)
        self.assertFalse(os.path.exists(os.path.join(rd, "wd.pid")),
                         "the watchdog pid file must be cleaned up")

    def test_slow_reporter_is_killed_at_the_bound(self):
        rd = harness.remote_dir(self.tmp, self.SLOW)
        script = transport.run_cmd(rd, 2)
        t0 = time.monotonic()
        p = harness.run_local_script(script, self.tmp, timeout=20)
        dt = time.monotonic() - t0
        self.assertEqual(p.returncode, 137, "expected SIGKILL status")
        self.assertIn("router_health_exit=137", p.stderr)
        self.assertGreaterEqual(dt, 1.5)
        self.assertLess(dt, 8.0)
        self.assertFalse(os.path.exists(os.path.join(rd, "wd.pid")))

    def test_watchdog_selftest_bounds_a_long_job_locally(self):
        rd = harness.remote_dir(self.tmp)
        script = transport.selftest_cmd(rd, 2)
        t0 = time.monotonic()
        p = harness.run_local_script(script, self.tmp, timeout=20)
        dt = time.monotonic() - t0
        self.assertIn("selftest_exit=137", p.stdout)
        self.assertGreaterEqual(dt, 1.5)
        self.assertLess(dt, 8.0)

    def test_no_leftover_processes_after_fast_reporter(self):
        # the detached watchdog must not linger holding anything: the harness
        # (which uses pipes) returns promptly only if nothing holds the channel.
        rd = harness.remote_dir(self.tmp, self.FAST)
        script = transport.run_cmd(rd, 8)
        p = harness.run_local_script(script, self.tmp, timeout=12)
        self.assertEqual(p.returncode, 0)


# ============================================================ watchdog gate
class TestWatchdogSelftestGate(TempCase):
    """Fix 5: the transport must PROVE the bound before trusting it."""

    def test_selftest_passes_in_normal_provisioning(self):
        tr = harness.make_transport(self.tmp, "ok")
        session = tr.start()
        self.assertTrue(session.provisioned)
        tr.stop(session)

    def test_unbounded_selftest_is_a_hard_stop(self):
        tr = harness.make_transport(self.tmp, "selftest_fail")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("watchdog self-test FAILED", str(cm.exception))
        self.assertFalse(tr._master_up)
        self.assertIsNone(tr._ctrl_dir)


# ================================================================= cleanup
class TestCleanupFailure(TempCase):
    """Fix 3: cleanup failure is surfaced, and never masks the original failure."""

    def test_stop_raises_cleanup_error(self):
        tr = harness.make_transport(self.tmp, "cleanup_fail")
        session = tr.start()
        tr.collect_sample(session)          # collection itself succeeds
        with self.assertRaises(CleanupError):
            tr.stop(session)
        self.assertFalse(tr._master_up)     # teardown still completed

    def test_collector_close_records_cleanup_error(self):
        tr = harness.make_transport(self.tmp, "cleanup_fail")
        c = Collector(tr, self.store(), log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "success")
        self.assertTrue(a["cleanup_ok"])
        c.close()
        self.assertIsNotNone(c.cleanup_error)

    def test_collection_failure_preserved_when_cleanup_also_fails(self):
        tr = harness.make_transport(self.tmp, "timeout_cleanup_fail")
        c = Collector(tr, self.store(), log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "timeout")     # original failure kept
        self.assertFalse(a["cleanup_ok"])
        self.assertTrue(a["cleanup_detail"])

    def test_cli_exit_5_on_cleanup_failure(self):
        data = os.path.join(self.tmp, "d")
        harness.write_ssh_config(data, self.tmp, "cleanup_fail")
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("SEPARATE FAILURE", r.stderr)
        self.assertIn("outcome=success", r.stderr)     # the sample WAS collected

    def test_cli_preserves_original_failure_over_cleanup(self):
        data = os.path.join(self.tmp, "d2")
        harness.write_ssh_config(data, self.tmp, "timeout_cleanup_fail")
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, 1, r.stderr)    # collection failure wins
        self.assertIn("outcome=timeout", r.stderr)
        self.assertIn("SEPARATE FAILURE", r.stderr)

    def test_status_reports_cleanup_failure(self):
        tr = harness.make_transport(self.tmp, "cleanup_fail")
        s = self.store()
        c = Collector(tr, s, log=lambda *a: None)
        c.collect_once()
        c.close()
        st = s.load_state()
        self.assertFalse(st["last_attempt"]["cleanup_ok"])
        from rtax_telemetry.status import render_status
        self.assertIn("cleanup: FAILED", render_status(s, st))


# ===================================================== master resource leak
class TestMasterResourceRelease(TempCase):
    """Resources are released when the master never comes up, or is interrupted."""

    def test_failed_master_releases_control_dir(self):
        before = set(d for d in os.listdir(tempfile.gettempdir())
                     if d.startswith("rt-ax56u-cm."))
        tr = harness.make_transport(self.tmp, "unreachable")
        with self.assertRaises(TransportUnreachableError):
            tr.start()
        self.assertIsNone(tr._ctrl_dir)
        self.assertFalse(tr._master_up)
        after = set(d for d in os.listdir(tempfile.gettempdir())
                    if d.startswith("rt-ax56u-cm."))
        self.assertEqual(after - before, set(), "control-socket dir leaked")

    def test_interrupted_master_releases_control_dir(self):
        tr = harness.make_transport(self.tmp, "ok")

        def boom(*a, **k):
            raise KeyboardInterrupt

        orig = transport.run_bounded
        transport.run_bounded = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                tr.start()
        finally:
            transport.run_bounded = orig
        self.assertIsNone(tr._ctrl_dir)
        self.assertFalse(tr._master_up)

    def test_interrupted_provision_releases_control_dir(self):
        tr = harness.make_transport(self.tmp, "ok")
        calls = {"n": 0}
        orig = transport.run_bounded

        def counting(argv, timeout, stdin_bytes=None, capture=True, **kwargs):
            # let the master start, then interrupt the first remote command
            if calls["n"] == 0:
                calls["n"] += 1
                return orig(argv, timeout, stdin_bytes, capture)
            raise KeyboardInterrupt

        transport.run_bounded = counting
        try:
            with self.assertRaises(KeyboardInterrupt):
                tr.start()
        finally:
            transport.run_bounded = orig
        self.assertIsNone(tr._ctrl_dir)
        self.assertFalse(tr._master_up)


if __name__ == "__main__":
    unittest.main(verbosity=2)
