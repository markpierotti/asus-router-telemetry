#!/usr/bin/env python3
"""Regression tests for interactive-terminal handling and descendant cleanup.

These are behavioural, not canned: a real controlling PTY is created, synthetic
input is written into it, and the child must actually READ it; and a real
descendant that ignores SIGTERM is left behind by a leader and must still be
cleaned up on timeout.

Offline only — no network, no router.
"""

import os
import shutil
import signal
import subprocess
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

try:
    import pty                                                            # noqa: E402
except ImportError:                                                       # pragma: no cover
    pty = None


def wait_path(path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.05)
    return False


def wait_gone(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-pty.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ======================================================= controlling terminal
@unittest.skipUnless(pty is not None, "POSIX pty required")
class TestControllingPTYInput(TempCase):
    """The child must READ synthetic input supplied through a controlling PTY.

    Opening /dev/tty, or matching SID/PGID, is NOT evidence; only an actual read is.

    RUNNERS DIFFER, and these tests say which kind they are in. The handover needs
    one thing from the environment: a process that owns a controlling terminal must
    be allowed to open ``/dev/tty`` read-write. An ordinary terminal (and an
    unsandboxed agent shell) allows it. A Seatbelt-sandboxed runner - reproduced
    with ``codex sandbox -P :workspace`` - grants the PTY and the controlling
    terminal but answers that open with EPERM. There ssh itself could not prompt,
    the transport correctly reports no usable terminal, and the two positive tests
    below FAIL. That failure is deliberate and is not softened into a skip: the
    terminal-read proof was not obtained, and a result that says otherwise would be
    false. What is added is the reason, taken from the probe's own record of what
    the runner permitted (``_environment_note``), so that a restricted runner is
    not mistaken for a transport defect - and a real defect in a capable runner
    still fails with no such note.
    """

    FAKE_SSH = (
        '#!/bin/sh\n'
        'stty -echo < /dev/tty 2>/dev/null || true\n'      # like ssh hiding the password
        'if IFS= read -r line < /dev/tty; then\n'
        '  printf "READ:%s" "$line" > "$PTY_INPUT_FILE"\n'
        'else\n'
        '  printf "READFAIL" > "$PTY_INPUT_FILE"\n'
        'fi\n'
        'exit 0\n'
    )

    _precheck = {}

    def _environment_note(self):
        """A concrete diagnostic when THIS RUNNER cannot exercise the handover.

        Empty in a capable runner. It never turns a failure into a pass or a skip;
        it is only prepended to a failure message, and it is derived from what the
        probe recorded with plain ``os`` calls before it touched the transport.
        """
        verdict = self._precheck.get("devtty_rdwr")
        if verdict in (None, "ok"):
            return ""
        return ("ENVIRONMENT RESTRICTION - not a transport verdict. In this runner a "
                "process that has just acquired a controlling terminal still cannot "
                "open it for writing: open('/dev/tty', O_RDWR) -> %s; the read-only "
                "open -> %s. ssh needs that same read-write open to prompt, so the "
                "transport correctly finds no usable terminal and hands nothing "
                "over, and the terminal-read proof CANNOT be obtained here. This is "
                "what a Seatbelt-sandboxed runner does (reproduced with `codex "
                "sandbox -P :workspace`). Run this file outside the sandbox for the "
                "proof. Original assertion: "
                % (verdict, self._precheck.get("devtty_rdonly")))

    def _run_pty(self, env_extra, *, inject, wait=15.0):
        """Drive the probe under a real controlling PTY; return (marker, log).

        Also loads ``self._precheck``: the probe's record of what this runner
        allowed on the controlling terminal (see ``_environment_note``).
        """
        ready = os.path.join(self.tmp, "ready")
        marker = os.path.join(self.tmp, "marker")
        precheck = os.path.join(self.tmp, "precheck")
        for f in (ready, marker, precheck):
            if os.path.exists(f):
                os.unlink(f)
        self._precheck = {}
        env = dict(os.environ, PTY_READY=ready, PTY_MARKER=marker,
                   PTY_PRECHECK=precheck)
        env.update(env_extra)

        # The PTY carries the controlling-terminal input; the probe's own
        # stdout/stderr go to a file so nothing can block writing into an
        # undrained PTY.
        logf = os.path.join(self.tmp, "probe.log")
        master, slave = pty.openpty()
        with open(logf, "w") as log:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "pty_auth_probe.py")],
                stdin=slave, stdout=log, stderr=log, env=env)
        os.close(slave)
        # Drain the master like a real terminal emulator would; without a reader a
        # session leader can stall on exit.
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    if not os.read(master, 4096):
                        break
                except OSError:
                    break

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        result = None
        try:
            self.assertTrue(wait_path(ready, 8), "probe never acquired the PTY")
            # written and closed by the probe before it signalled readiness
            if os.path.exists(precheck):
                with open(precheck) as fh:
                    self._precheck = dict(
                        line.split("=", 1) for line in fh.read().splitlines()
                        if "=" in line)
            # SYNTHETIC INPUT through the controlling PTY
            if inject is not None:
                os.write(master, (inject + "\n").encode())
            if wait_path(marker, wait):
                with open(marker) as fh:
                    result = fh.read().strip()
        finally:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            stop.set()
            os.close(master)
            reader.join(timeout=2)
        with open(logf) as fh:
            log = fh.read()
        return result, log

    def _write_fake_ssh(self):
        fake = os.path.join(self.tmp, "fake_ssh.sh")
        with open(fake, "w") as fh:
            fh.write(self.FAKE_SSH)
        os.chmod(fake, 0o755)
        return fake

    def _run_probe(self, *, interactive, inject, timeout=4.0, wait=15.0):
        inputf = os.path.join(self.tmp, "input")
        if os.path.exists(inputf):
            os.unlink(inputf)
        fake = self._write_fake_ssh()
        result, self._log = self._run_pty(
            {"PTY_MODE": "run_bounded", "PTY_FAKE_SSH": fake,
             "PTY_INPUT_FILE": inputf,
             "PTY_INTERACTIVE": "1" if interactive else "0",
             "PTY_TIMEOUT": str(timeout)},
            inject=inject, wait=wait)
        inputval = None
        if os.path.exists(inputf):
            with open(inputf) as fh:
                inputval = fh.read()
        return result, inputval

    def test_child_reads_input_with_terminal_handover(self):
        result, inputval = self._run_probe(interactive=True, inject="synthetic-secret-42")
        self.assertEqual(inputval, "READ:synthetic-secret-42",
                         self._environment_note() +
                         "the child did not READ the synthetic input from the PTY; "
                         "probe said %r; log=%s" % (result, self._log))
        self.assertTrue(str(result).startswith("rc=0"),
                        self._environment_note() +
                        "probe said %r; log=%s" % (result, self._log))
        self.assertIn("restored=True", str(result),
                      "terminal foreground group and/or settings were not restored: %r"
                      % result)

    def test_without_handover_the_read_stalls_on_sigttin(self):
        # documents WHY the handover exists: a background group reading /dev/tty is
        # stopped by SIGTTIN and the input is never consumed.
        result, inputval = self._run_probe(interactive=False, inject="synthetic-secret-42",
                                           timeout=2.0, wait=20.0)
        self.assertIsNone(inputval, "input was read even without the handover")
        self.assertTrue(str(result).startswith("exc=RemoteTimeout"),
                        "probe said %r; log=%s" % (result, self._log))

    def test_master_startup_reads_the_secret_from_the_terminal(self):
        """SSHTransport.start() must drive the interactive password prompt correctly.

        The fake ssh reads a line from /dev/tty on the MASTER invocation, exactly
        where ssh's password prompt reads it.
        """
        art = harness.make_artifact(self.tmp)
        kh = harness.make_known_hosts(self.tmp)
        tty_in = os.path.join(self.tmp, "tty_in")
        wrapper = harness.make_wrapper(self.tmp, "ok", extra_env={
            "FAKE_SSH_READ_TTY": "1", "FAKE_SSH_TTY_OUT": tty_in})
        result, log = self._run_pty(
            {"PTY_MODE": "start",
             "PTY_SSH_BIN": wrapper,
             "PTY_ARTIFACT": art,
             "PTY_KNOWN_HOSTS": kh,
             "PTY_ARTIFACT_SHA": harness._sha(art)},
            inject="prompt-secret-77", wait=20.0)
        read = None
        if os.path.exists(tty_in):
            with open(tty_in) as fh:
                read = fh.read()
        self.assertEqual(read, "READ:prompt-secret-77",
                         self._environment_note() +
                         "start() did not read the secret from the terminal; "
                         "marker=%r log=%s" % (result, log))
        self.assertTrue(str(result).startswith("started:"),
                        self._environment_note() +
                        "start() failed: %r log=%s" % (result, log))

    def test_terminal_detection_agrees_with_what_the_runner_permits(self):
        """The transport's own verdict must match the runner, in EVERY runner.

        `has_controlling_terminal()` decides whether master establishment is
        interactive at all. Inside a session that owns a controlling terminal it
        must say True exactly when `/dev/tty` can be opened read-write - which the
        probe establishes separately, with plain `os` calls. This holds in a capable
        runner (both True: the handover is then proven by the tests above) and in a
        restricted one (both False: the transport declines to prompt, which is the
        correct behaviour there, and the tests above report the restriction).
        """
        result, log = self._run_pty({"PTY_MODE": "detect"}, inject=None, wait=10.0)
        self.assertIn("devtty_rdwr", self._precheck,
                      "the probe recorded no precheck; marker=%r log=%s" % (result, log))
        permitted = self._precheck["devtty_rdwr"] == "ok"
        self.assertEqual(result, "has_tty=%s" % permitted,
                         "the transport's terminal detection disagrees with the "
                         "runner: /dev/tty read-write open -> %s; log=%s"
                         % (self._precheck["devtty_rdwr"], log))


# ========================================================= descendant cleanup
class TestDescendantReap(TempCase):
    """The owned GROUP must be terminated even when its leader has already exited."""

    def test_timeout_kills_descendant_left_by_an_exited_leader(self):
        childpid = os.path.join(self.tmp, "child.pid")
        leader = os.path.join(self.tmp, "leader.sh")
        with open(leader, "w") as fh:
            fh.write('#!/bin/sh\n'
                     "sh -c 'trap \"\" TERM; exec sleep 30' &\n"   # descendant ignores SIGTERM
                     'echo $! > "%s"\n'
                     'exit 0\n' % childpid)                        # leader exits immediately
        os.chmod(leader, 0o755)

        old = transport.REAP_GRACE
        transport.REAP_GRACE = 0.5
        try:
            t0 = time.monotonic()
            with self.assertRaises(transport.RemoteTimeout):
                transport.run_bounded(["sh", leader], 1)
            self.assertLess(time.monotonic() - t0, 8.0)
        finally:
            transport.REAP_GRACE = old

        self.assertTrue(os.path.exists(childpid), "descendant never recorded its pid")
        with open(childpid) as fh:
            pid = int(fh.read().strip())
        self.assertTrue(wait_gone(pid, 6.0),
                        "descendant (pid %d) survived timeout cleanup" % pid)

    def test_group_signalled_when_leader_already_reaped(self):
        # direct unit-level check of the fix on the helper itself
        childpid = os.path.join(self.tmp, "child.pid")
        leader = os.path.join(self.tmp, "leader.sh")
        with open(leader, "w") as fh:
            fh.write('#!/bin/sh\n'
                     "sh -c 'trap \"\" TERM; exec sleep 30' &\n"
                     'echo $! > "%s"\n' % childpid)
        os.chmod(leader, 0o755)
        p = subprocess.Popen(["sh", leader], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, process_group=0)
        try:
            self.assertTrue(wait_path(childpid, 6))
            for _ in range(40):                     # let the leader exit and be reaped
                p.poll()
                if p.returncode is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(p.poll(), "leader did not exit")
            transport._terminate_and_reap(p, grace=0.5)
            with open(childpid) as fh:
                pid = int(fh.read().strip())
            self.assertTrue(wait_gone(pid, 6.0),
                            "descendant survived even though the leader had exited")
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:                       # noqa: BLE001
                pass
            try:
                p.wait(timeout=5)
            except Exception:                       # noqa: BLE001
                pass


# ===================================================== remote signal cleanup
class TestRemoteSignalTraps(TempCase):
    """The remote run/self-test scripts must clean up their children on a signal."""

    def test_command_strings_carry_signal_cleanup_traps(self):
        for name, text in (("run", transport.run_cmd("/tmp/rt.x", 5)),
                           ("selftest", transport.selftest_cmd("/tmp/rt.x", 5))):
            self.assertIn("trap '", text, name)
            self.assertIn("HUP INT TERM", text, name)
            self.assertIn("exit 129", text, name)

    def test_killing_the_remote_shell_kills_the_reporter(self):
        rd = harness.remote_dir(self.tmp)
        reporter = os.path.join(rd, "router_health")
        reporter_pid = os.path.join(rd, "reporter.pid")
        with open(reporter, "w") as fh:
            fh.write('#!/bin/sh\necho $$ > "%s"\nexec sleep 30\n' % reporter_pid)
        os.chmod(reporter, 0o755)

        script = os.path.join(self.tmp, "run.sh")
        with open(script, "w") as fh:
            fh.write(transport.run_cmd(rd, 30))
        os.chmod(script, 0o755)

        p = subprocess.Popen(["sh", script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, process_group=0)
        try:
            self.assertTrue(wait_path(reporter_pid, 8), "reporter never started")
            with open(reporter_pid) as fh:
                pid = int(fh.read().strip())
            # simulate the channel being lost: signal ONLY the remote shell
            os.kill(p.pid, signal.SIGTERM)
            rc = p.wait(timeout=10)
            self.assertIn(rc, (129, 143), "the trap should exit 129")
            self.assertTrue(wait_gone(pid, 6.0),
                            "reporter (pid %d) survived the remote signal trap" % pid)
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:                       # noqa: BLE001
                pass
            try:
                p.wait(timeout=5)
            except Exception:                       # noqa: BLE001
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
