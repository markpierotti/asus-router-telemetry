"""Offline regressions for the follow-up remediation (R1-R3).

Each test here FAILS on the code as committed at `6562688` and passes after the
follow-up correction; that is the point of the file. Nothing here contacts a router:
the transport is either the fake `ssh` (already used across the suite) or a local
test double that raises a chosen exception from `start()`.

Findings covered:
  R1  provisioning cleanup metadata is preserved regardless of the exception CLASS
  R2  a cancellation during TEARDOWN keeps the session facts and records uncertainty
  R3  the master-shutdown verdict is real (stderr diagnostic, no PID signalling,
      unconfirmed shutdown propagated durably and into the exit status)
"""

import json
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
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import cli, transport                                # noqa: E402
from rtax_telemetry.mock import MockSession                              # noqa: E402
from rtax_telemetry.core import (                                        # noqa: E402
    OUTCOME_ARTIFACT, OUTCOME_INTERNAL, OUTCOME_MASTER_LOST, OUTCOME_OVERFLOW,
    OUTCOME_PROVISION, OUTCOME_STORAGE, OUTCOME_SUCCESS, Collector,
)
from rtax_telemetry.errors import (                                      # noqa: E402
    ArtifactMismatch, ControlMasterLost, OutputLimitExceeded, ProvisioningError,
    StorageError,
)
from rtax_telemetry.store import Store                                   # noqa: E402


class _FactCarrier(Exception):
    """Raised from `start()` WITH the provisioning facts the transport attaches."""


class _FailingTransport(object):
    """A local double whose `start()` raises a chosen exception.

    The facts are attached exactly the way `SSHTransport.start()` attaches them
    (a `provision_facts` dict plus the individual attributes), so this exercises the
    COLLECTOR's handling of the exception class, not the transport's.
    """

    name = "double"

    def __init__(self, exc, remote_dir="/tmp/rt.r1fixture", ownership="owned",
                 cleanup_ok=False, cleanup_detail="REMOVAL_FAILED (rc=1)",
                 lifecycle_error=None, stop_exc=None):  # noqa: C901
        self.exc = exc
        self.remote_dir = remote_dir
        if exc is not None:
            setattr(exc, "provision_facts", {
                "remote_dir": remote_dir,
                "remote_ownership": ownership,
                "cleanup_ok": cleanup_ok,
                "cleanup_detail": cleanup_detail,
            })
            setattr(exc, "remote_dir", remote_dir)
            setattr(exc, "remote_ownership", ownership)
            setattr(exc, "provision_cleanup_ok", cleanup_ok)
            setattr(exc, "provision_cleanup_detail", cleanup_detail)
        self.stopped = []
        self.lifecycle_error = lifecycle_error
        self.master_shutdown = None
        self.stop_exc = stop_exc

    def start(self):
        if self.exc is not None:
            raise self.exc
        raise AssertionError("this double is only for failing start()")

    def stop(self, session):
        self.stopped.append(session)
        if self.stop_exc is not None:
            raise self.stop_exc


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr4.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def store(self, name="data", **kw):
        return Store(os.path.join(self.tmp, name), **kw)

    def outcomes(self, store):
        path = store.outcomes_path
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def pending(self, store):
        return store.pending_cleanup()


# ---------------------------------------------------------------------------
# R1 - provisioning cleanup metadata survives every exception CLASS
# ---------------------------------------------------------------------------
class TestR1ProvisioningMetadata(_Base):

    CASES = [
        (ControlMasterLost("the master went away mid-provision"), OUTCOME_MASTER_LOST),
        (ArtifactMismatch("sha256 of the pushed artifact does not match"), OUTCOME_ARTIFACT),
        (ProvisioningError("the remote tool probe failed"), OUTCOME_PROVISION),
        (OutputLimitExceeded("stdout budget exceeded"), OUTCOME_OVERFLOW),
        (StorageError("evidence filesystem refused the write"), OUTCOME_STORAGE),
        (_FactCarrier("something the taxonomy does not name"), OUTCOME_INTERNAL),
    ]

    def test_cleanup_facts_survive_every_exception_class(self):
        """R1: the failure class must not decide whether the cleanup facts survive.

        Before the fix only `ProvisioningError` carried them, so a
        `ControlMasterLost`/`ArtifactMismatch`/... raised after `mkdir` recorded
        `cleanup_ok: true` and lost the path of a directory that may still exist.
        """
        for exc, expected_outcome in self.CASES:
            with self.subTest(exc=type(exc).__name__):
                work = os.path.join(self.tmp, type(exc).__name__)
                os.makedirs(work)
                store = Store(work)
                store.ensure()
                tr = _FailingTransport(exc)
                c = Collector(tr, store, host_label="bench", log=lambda *a: None)
                attempt = c.collect_once()

                self.assertEqual(attempt["outcome"], expected_outcome,
                                 "the original outcome classification was changed")
                self.assertEqual(attempt["remote_dir"], "/tmp/rt.r1fixture",
                                 "the remote directory was lost")
                self.assertEqual(attempt["remote_ownership"], "owned",
                                 "the recorded ownership was lost")
                self.assertFalse(attempt["cleanup_ok"],
                                 "an unconfirmed removal was recorded as cleanup_ok")
                self.assertIn("REMOVAL_FAILED", attempt["cleanup_detail"] or "")
                # ...and the path is durably queued for manual reconciliation
                pend = self.pending(store)
                self.assertEqual(len(pend), 1, "no pending-cleanup evidence was written")
                self.assertEqual(pend[0]["remote_dir"], "/tmp/rt.r1fixture")
                self.assertTrue(pend[0]["manual_reconciliation_required"])
                self.assertFalse(pend[0]["automatic_deletion"])

    def test_successful_provision_cleanup_is_not_flipped_to_false(self):
        """The fix must not blanket-fail every provisioning error.

        A provisioning failure that DID reconcile its own directory still reports
        `cleanup_ok: true` and creates no pending-cleanup entry.
        """
        store = self.store()
        store.ensure()
        exc = ArtifactMismatch("sha mismatch, directory removed cleanly")
        tr = _FailingTransport(exc, cleanup_ok=True, cleanup_detail=None,
                               remote_dir=None)
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_ARTIFACT)
        self.assertTrue(attempt["cleanup_ok"],
                        "a cleanly removed provisioning failure was marked unclean")
        self.assertEqual(self.pending(store), [])

    def test_artifact_mismatch_after_mkdir_keeps_the_failed_cleanup(self):
        """End-to-end through the real fake ssh: a failure AFTER mkdir.

        The provision creates the directory, pushes the artifact, and then the sha
        comparison fails; the removal that follows fails too. The attempt must name
        the path and admit that removal is unconfirmed.
        """
        store = self.store()
        store.ensure()
        # The LOCAL pre-check must pass (otherwise the mismatch happens before the
        # directory exists, which is a different case); the mismatch comes from the
        # pushed artifact's echoed digest, i.e. AFTER mkdir. The removal then fails.
        tr = harness.make_transport(self.tmp, "cleanup_fail",
                                    extra_env={"FAKE_SSH_EXTRA": "transfer_bad_json"})
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_ARTIFACT, attempt["detail"])
        self.assertTrue(attempt["remote_dir"], "the mkdir'd path was lost")
        self.assertFalse(attempt["cleanup_ok"],
                         "a failed removal after mkdir was recorded as clean")
        pend = self.pending(store)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["remote_dir"], attempt["remote_dir"])

    def test_a_later_failure_does_not_clobber_the_live_session_ownership(self):
        """An exception raised AFTER a successful provision carries no facts.

        The collector must keep the ownership the live session reported, not fall
        back to "none" simply because the exception has nothing attached.
        """
        store = self.store()
        store.ensure()

        class _LostAfterProvision(harness.__dict__["SSHTransport"]):
            def collect_sample(self, session):
                raise ControlMasterLost("master lost after provisioning")

        tr = harness.make_transport(self.tmp, "ok")
        tr.__class__ = _LostAfterProvision
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_MASTER_LOST)
        self.assertEqual(attempt["remote_ownership"], "owned",
                         "the live session ownership was overwritten by a default")


# ---------------------------------------------------------------------------
# R2 - cancellation during TEARDOWN
# ---------------------------------------------------------------------------
class TestR2CancellationDuringTeardown(_Base):

    def _collector_ready(self, name="data"):
        store = self.store(name)
        store.ensure()
        return store

    def test_cancellation_during_cleanup_records_uncertainty_and_reraises(self):
        """R2: interrupts do not arrive only during collection or authentication.

        A cancellation while the remote directory is being removed must (a) record
        the uncertainty durably and (b) still propagate the cancellation.
        """
        store = self._collector_ready()
        tr = _FailingTransport(None, stop_exc=KeyboardInterrupt())
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        session = MockSession(remote_dir="/tmp/rt.r2interrupted", ownership="owned")
        c._session = session

        with self.assertRaises(KeyboardInterrupt):
            c.close()

        self.assertIsNotNone(c.cleanup_error, "the interruption was not recorded")
        self.assertIn("INTERRUPTED", c.cleanup_error or "")
        self.assertTrue(c.cleanup_interrupted)

        records = self.outcomes(store)
        teardown = [r for r in records if r.get("record") == "teardown"]
        self.assertEqual(len(teardown), 1, "no durable teardown record was written")
        self.assertFalse(teardown[0]["cleanup_ok"])
        self.assertTrue(teardown[0]["cleanup_interrupted"])
        pend = self.pending(store)
        self.assertEqual(len(pend), 1, "the uncertain directory was not queued")
        self.assertEqual(pend[0]["remote_dir"], session.remote_dir)

    def test_teardown_failure_does_not_swallow_the_signal_to_success(self):
        """The CLI must not report success after an interrupted teardown."""
        data = os.path.join(self.tmp, "data")
        harness.write_ssh_config(data, self.tmp, mode="slow_cleanup")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "collector.py"),
             "collect", "--once", "--data-dir", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Wait for the sample to land: at that point the only remote work left is the
        # removal, which this mode deliberately makes slow, so the SIGINT below is
        # delivered DURING teardown rather than during collection or authentication.
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if os.listdir(os.path.join(data, "samples")):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        self.assertIsNone(proc.poll(),
                          "the collector exited before teardown could be interrupted")
        time.sleep(1.5)                      # let the slow removal start
        proc.send_signal(signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("the collector did not stop after SIGINT during teardown")
        self.assertEqual(proc.returncode, cli.EXIT_INTERRUPTED,
                         "an interrupted teardown did not keep the interrupted status:\n"
                         + err)
        store = Store(data)
        records = self.outcomes(store)
        teardown = [r for r in records if r.get("record") == "teardown"]
        self.assertEqual(len(teardown), 1,
                         "interrupting the removal wrote no durable teardown record")
        self.assertFalse(teardown[0]["cleanup_ok"])
        self.assertTrue(teardown[0]["cleanup_interrupted"])
        pending = self.pending(store)
        self.assertEqual(len(pending), 1,
                         "the directory whose removal was interrupted was not queued")

    def test_lock_is_released_after_an_interrupted_teardown(self):
        """The lock release is in its own `finally`, so it survives a raise."""
        data = os.path.join(self.tmp, "data-lock")
        harness.write_ssh_config(data, self.tmp, mode="slow_cleanup")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "collector.py"),
             "collect", "--once", "--data-dir", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(4.0)
        proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail("the collector did not stop")
        # a following run must be able to take the lock
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertNotEqual(r.returncode, cli.EXIT_LOCK_HELD,
                            "the data-dir lock leaked after an interrupted teardown:\n"
                            + r.stderr)

    def test_signal_handlers_stay_installed_through_teardown(self):
        """Handlers are restored AFTER the teardown, not before it."""
        with open(os.path.join(ROOT, "rtax_telemetry", "cli.py")) as fh:
            src = fh.read()
        teardown = src.index("collector.close()")
        restore = src.index("restore_stop_handlers(installed)", teardown)
        release = src.index("release_lock()", teardown)
        self.assertLess(teardown, release,
                        "release_lock() runs before the teardown")
        self.assertLess(release, restore,
                        "the stop handlers are restored before the lock is released")
        self.assertGreater(restore, release)


# ---------------------------------------------------------------------------
# R3 - ssh master lifecycle
# ---------------------------------------------------------------------------
class TestR3MasterLifecycle(_Base):

    def test_pid_is_read_from_the_stderr_diagnostic(self):
        """R3: real OpenSSH reports `Master running (pid=N)` on STDERR.

        The fake wrote it on stdout, which is why a stdout-only parse looked correct.
        The diagnostic is now read from either stream, so this passes with the
        faithful (stderr) fake AND remains correct if a build uses stdout.
        """
        tr = harness.make_transport(self.tmp, "ok",
                                    extra_env={"FAKE_SSH_MASTER_PID": "4242"})
        tr.start()
        alive, pid, failed = tr._master_check()
        self.assertTrue(alive, "the master was not seen as alive")
        self.assertFalse(failed, "the probe was reported as unable to complete")
        self.assertEqual(pid, 4242,
                         "the PID was not read from the stderr diagnostic")
        tr.close()

    def test_the_fake_writes_the_diagnostic_to_stderr(self):
        """Test fidelity: the double must not be easier to pass than real ssh."""
        with open(os.path.join(HERE, "fake_ssh.sh")) as fh:
            src = fh.read()
        self.assertIn("Master running (pid=%s)\\r\\n' \"${FAKE_SSH_MASTER_PID:-4242}\" 1>&2",
                      src.replace("\\r\\n", "\\r\\n"),
                      "the fake writes the -O check diagnostic somewhere else")

    def test_probe_that_cannot_complete_is_not_gone(self):
        """A failed probe is UNKNOWN, never "confirmed gone"."""
        tr = harness.make_transport(self.tmp, "ok")
        tr._ctrl_path = "/tmp/rtax-nothing-here.sock"
        calls = {"n": 0}

        def _boom(*a, **kw):
            calls["n"] += 1
            raise transport.RemoteTimeout("the probe did not return")

        real = transport.run_bounded
        transport.run_bounded = _boom
        try:
            tr._master_up = True
            result = tr._exit_master()
        finally:
            transport.run_bounded = real
        self.assertEqual(result, transport.MS_PROBE_TIMEOUT,
                         "an unrunnable probe was reported as a confirmed exit")
        self.assertTrue(transport.shutdown_is_unconfirmed(result))

    def test_absent_socket_and_no_master_is_not_a_failure(self):
        """Nothing to stop is not an unconfirmed shutdown."""
        tr = harness.make_transport(self.tmp, "ok")
        tr._ctrl_path = None
        self.assertEqual(tr._exit_master(), transport.MS_NOT_ATTEMPTED)
        tr.close()
        self.assertIsNone(tr.lifecycle_error,
                          "a run with no master reported a lifecycle failure")

    def test_unconfirmed_shutdown_is_durable_and_exits_nonzero(self):
        """An authenticated master that may still run must not look like success."""
        data = os.path.join(self.tmp, "data-lifecycle")
        never = os.path.join(self.tmp, "never-gone")
        harness.write_ssh_config(data, self.tmp, mode="master_wont_exit")
        # point the fake at a state file that `-O exit` will not remove
        wrapper = os.path.join(self.tmp, "fake-ssh")
        with open(wrapper) as fh:
            body = fh.read()
        with open(wrapper, "w") as fh:
            fh.write(body.replace("export FAKE_SSH_MODE=",
                                  "export FAKE_SSH_MASTER_STATE=%s\n"
                                  "export FAKE_SSH_MODE=" % never))
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, cli.EXIT_CLEANUP_FAILURE,
                         "an unconfirmed master shutdown exited as success:\n" + r.stderr)
        self.assertIn("master shutdown", r.stderr)
        store = Store(data)
        teardown = [x for x in self.outcomes(store) if x.get("record") == "teardown"]
        self.assertEqual(len(teardown), 1, "the unconfirmed shutdown was not persisted")
        self.assertEqual(teardown[0]["outcome"], "lifecycle_failure")
        self.assertEqual(teardown[0]["master_shutdown"],
                         transport.MS_STILL_ANSWERING)
        self.assertTrue(teardown[0]["lifecycle_error"])
        self.assertIsNone(teardown[0]["cleanup_detail"],
                          "a master-only failure named a directory for deletion")
        self.assertEqual(self.pending(store), [])

    def test_unconfirmed_shutdown_does_not_mask_the_collection_failure(self):
        """Both facts are reported; the original failure keeps the exit status."""
        data = os.path.join(self.tmp, "data-mask")
        never = os.path.join(self.tmp, "never-gone-mask")
        harness.write_ssh_config(data, self.tmp, mode="master_wont_exit")
        cfg_path = os.path.join(data, "config.json")
        with open(cfg_path) as fh:
            cfg = json.load(fh)
        # make the COLLECTION fail too: the reporter exits non-zero
        wrapper = os.path.join(self.tmp, "fake-ssh-failing-reporter")
        with open(wrapper, "w") as fh:
            fh.write("#!/bin/bash\n"
                     "export FAKE_SSH_MASTER_STATE=%s\n"
                     "export FAKE_SSH_MODE=master_wont_exit\n"
                     "export FAKE_SSH_EXTRA=reporter_fail\n"
                     "export FAKE_SSH_DOC=%s\n"
                     "exec %s \"$@\"\n"
                     % (never, harness.FIXTURE, os.path.join(HERE, "fake_ssh.sh")))
        os.chmod(wrapper, 0o755)
        cfg["ssh"] = wrapper
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh)
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, cli.EXIT_COLLECTION_FAILURE,
                         "the lifecycle failure masked the collection failure:\n"
                         + r.stderr)
        self.assertIn("outcome=reporter_failure", r.stderr)
        self.assertIn("master shutdown", r.stderr)
        store = Store(data)
        teardown = [x for x in self.outcomes(store) if x.get("record") == "teardown"]
        self.assertEqual(len(teardown), 1)
        self.assertEqual(teardown[0]["master_shutdown"], transport.MS_STILL_ANSWERING)

    def test_no_process_is_signalled_for_an_unconfirmed_shutdown(self):
        """The refused fallback stays refused."""
        tr = harness.make_transport(self.tmp, "master_wont_exit",
                                    extra_env={"FAKE_SSH_MASTER_STATE":
                                               os.path.join(self.tmp, "nope"),
                                               "FAKE_SSH_MASTER_PID": str(os.getpid())})
        tr.start()
        sent = []
        real_kill = os.kill
        os.kill = lambda pid, sig: sent.append((pid, sig))
        try:
            tr.close()
        finally:
            os.kill = real_kill
        self.assertEqual(sent, [], "a process was signalled: %r" % (sent,))
        self.assertTrue(transport.shutdown_is_unconfirmed(tr.master_shutdown))
        self.assertFalse(hasattr(tr, "_kill_master_pid"),
                         "the PID-killing fallback is still present")


if __name__ == "__main__":
    unittest.main()
