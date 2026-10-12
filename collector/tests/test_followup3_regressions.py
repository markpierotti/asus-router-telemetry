"""Offline regressions for the third follow-up (collector 1.5.3).

Four defects were reproduced against collector `1.5.2` and fixed offline:

  1  a RELATIVE `--data-dir` persisted a RELATIVE sample path, so `_predecessor()`
     could not reopen the state-selected sample from another working directory and
     the next sample silently derived as CPU state `first`
  2  in `--loop` mode a failed FINAL cleanup was recorded twice - the loop's
     `finally` AND the CLI's `finally` both call `close()`, producing two
     `teardown` records and two `pending_cleanup` entries for one directory
  3  an ordinary (non-cancelled) FAILED PROVISION with a control master that could
     not be confirmed stopped logged the verdict but never lifted it into durable
     lifecycle evidence (no `teardown` record, no exit-status effect)
  4  a cancellation during the FIRST-EVER provisioning wrote the teardown evidence
     yet reported `EVIDENCE NOT PERSISTED` merely because no `last_attempt` existed
     to annotate

Nothing here contacts a router. The transport is either the real `SSHTransport`
driven by the fake `ssh` process, or - where a cancellation must land at an exact
point - the scripted stand-in used by the second-follow-up suite.

Tests whose docstring starts with REGRESSION fail on `46d7fa3` and pass after the
correction. Tests whose docstring starts with GUARD pin what the correction must
NOT change.
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import cli, transport                                 # noqa: E402
from rtax_telemetry.core import Collector                                 # noqa: E402
from rtax_telemetry.errors import CleanupError                            # noqa: E402
from rtax_telemetry.mock import ScenarioTransport                         # noqa: E402
from rtax_telemetry.store import Store                                    # noqa: E402
# The scripted `run_bounded` stand-in and the base case are reused unchanged.
from test_followup2_regressions import _Base, _ScriptedSsh                # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "live_sample.json")


@contextlib.contextmanager
def chdir(path):
    """Temporarily change the working directory, restoring it unconditionally."""
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


# ---------------------------------------------------------------------------
# 1 - relative sample paths
# ---------------------------------------------------------------------------
class TestRelativeSamplePath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr6rel.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _collector(self, data_dir, log=None):
        return Collector(ScenarioTransport("success", log=lambda *a: None),
                         Store(data_dir), host_label="bench", log=log)

    def test_a_relative_data_dir_records_an_absolute_sample_path(self):
        """REGRESSION: the state pointer must not carry a relative path.

        On `46d7fa3` a relative `--data-dir` stored the sample path exactly as
        given (``data/samples/<ts>.json``), so it was reopenable only from the
        working directory it was written in.
        """
        work = os.path.join(self.tmp, "a")
        os.makedirs(work)
        with chdir(work):
            c = self._collector("data")
            c.collect_once()
            state = Store("data").load_state()
        recorded = state["last_valid_sample"]["path"]
        self.assertTrue(os.path.isabs(recorded),
                        "a relative sample path was persisted: %r" % recorded)

    def test_predecessor_survives_a_working_directory_change(self):
        """REGRESSION: resuming from another CWD must keep the state predecessor.

        Run 1 uses a RELATIVE data dir from directory A; run 2 reaches the SAME
        data dir by its absolute path from directory B. On `46d7fa3` the open of the
        recorded relative string failed, `_predecessor()` returned None and the
        second sample was derived as CPU state `first` - silently, with no log line.
        """
        work = os.path.join(self.tmp, "a")
        work_b = os.path.join(self.tmp, "b")
        os.makedirs(work)
        os.makedirs(work_b)
        with chdir(work):
            self._collector("data").collect_once()
        absdir = os.path.join(work, "data")
        logs = []
        with chdir(work_b):
            c2 = Collector(ScenarioTransport("success", log=lambda *a: None),
                           Store(absdir), host_label="bench", log=logs.append)
            prev, meta = c2._predecessor()
            self.assertIsNotNone(prev, "the state predecessor was lost after a CWD change")
            attempt = c2.collect_once()
        self.assertNotIn("cpu_util_state=first", attempt["detail"],
                         "the predecessor was lost: " + attempt["detail"])
        self.assertFalse([l for l in logs if "could not be located" in l],
                         "a located predecessor was reported as unlocatable")

    def test_a_legacy_relative_record_is_resolved_cwd_independently(self):
        """REGRESSION: OLDER relative records must keep working.

        Write exactly what `1.5.2` wrote - a state pointer holding a path relative to
        the data dir's parent - and resolve it from an unrelated working directory.
        """
        base = os.path.join(self.tmp, "proj")
        os.makedirs(base)
        store = Store(os.path.join(base, "data"))
        store.ensure()
        sample = {"schema": "x", "collected_at": "2026-01-01T00:00:00Z",
                  "monotonic": 1.0, "source": {"transport": "mock", "host": "bench"},
                  "reporter": {"host": {}}}
        path = store.save_sample(sample)
        rel = os.path.join("data", "samples", os.path.basename(str(path)))
        # the state pointer EXACTLY as an older version recorded it: a relative string
        store.write_state({"schema": "rtax_telemetry.state/1", "consecutive_failures": 0,
                           "last_attempt": {"at": "2026-01-01T00:00:00Z", "outcome": "success"},
                           "last_valid_sample": {"at": sample["collected_at"], "path": rel,
                                                 "monotonic": 1.0}})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        with chdir(elsewhere):
            resolved = Store(os.path.join(base, "data")).resolve_sample_path(rel)
        self.assertIsNotNone(resolved, "a legacy relative record could not be resolved")
        self.assertTrue(os.path.isfile(resolved))
        self.assertEqual(os.path.basename(resolved), os.path.basename(str(path)))

    def test_the_state_predecessor_is_not_replaced_by_the_newest_file(self):
        """GUARD: the predecessor comes from state, never from filename order.

        A future-dated decoy sample is present; the state-selected sample must win.
        Written against an ABSOLUTE data dir so this holds on both 1.5.2 and 1.5.3.
        """
        base = os.path.join(self.tmp, "proj")
        os.makedirs(base)
        store = Store(os.path.join(base, "data"))
        store.ensure()
        real = {"schema": "x", "collected_at": "2026-01-01T00:00:00Z", "monotonic": 1.0,
                "source": {"transport": "mock", "host": "bench"}, "reporter": {"host": {}}}
        real_path = store.save_sample(real)
        (store.samples_dir / "29991231T235959.000000Z.json").write_text(
            json.dumps({"nonsense": True}))
        store.write_state({"schema": "rtax_telemetry.state/1", "consecutive_failures": 0,
                           "last_valid_sample": {"at": real["collected_at"],
                                                 "path": str(real_path), "monotonic": 1.0}})
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        with chdir(elsewhere):
            c = Collector(ScenarioTransport("success", log=lambda *a: None),
                          Store(os.path.join(base, "data")), host_label="bench",
                          log=lambda *a: None)
            prev, _meta = c._predecessor()
        self.assertIsNotNone(prev, "the state predecessor was lost")
        self.assertEqual(prev.get("collected_at"), real["collected_at"],
                         "the newest (decoy) file was substituted for the state sample")


# ---------------------------------------------------------------------------
# 2 - duplicate final-cleanup records
# ---------------------------------------------------------------------------
class _LoopThenCleanupFails(ScenarioTransport):
    """Succeeds once, then a Ctrl-C ends the loop; the final teardown FAILS."""

    def __init__(self, tag="dir"):
        super().__init__("success", log=lambda *a: None)
        self.samples = 0
        self.tag = tag

    def collect_sample(self, session, timeout=None):
        self.samples += 1
        if self.samples >= 2:
            raise KeyboardInterrupt
        return super().collect_sample(session)

    def stop(self, session):
        raise CleanupError("failed to remove router dir /tmp/rt.%s (rc=1)" % self.tag)


class TestDuplicateFinalCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr6dup.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _store(self, name="data"):
        store = Store(os.path.join(self.tmp, name))
        store.ensure()
        return store

    def test_loop_shutdown_and_cli_finalization_record_one_teardown(self):
        """REGRESSION: the loop's close() and the CLI's close() are one teardown.

        `run_loop`'s `finally` and `cmd_collect`'s `finally` both call `close()`; a
        failed final cleanup on `46d7fa3` was therefore recorded twice - two
        `teardown` records and two `pending_cleanup` entries for the SAME directory.
        """
        store = self._store()
        c = Collector(_LoopThenCleanupFails(), store, host_label="bench",
                      log=lambda *a: None)
        rc = cli.run_loop(c, store, 0.1)          # run_loop's finally -> close() #1
        self.assertEqual(rc, cli.EXIT_INTERRUPTED)
        c.close()                                  # what cmd_collect's finally does -> #2
        disk = Store(store.dir)
        td = [r for r in disk.outcomes() if r.get("record") == "teardown"]
        self.assertEqual(len(td), 1, "the same cleanup failure was recorded twice")
        self.assertEqual(len(disk.pending_cleanup()), 1,
                         "the same directory was queued for reconciliation twice")

    def test_a_genuine_separate_failure_is_not_deduplicated(self):
        """GUARD: the dedup is per-run and per-directory, not a global suppression.

        Two INDEPENDENT runs that fail on DIFFERENT directories must each contribute
        their own teardown evidence.
        """
        store = self._store()
        for tag in ("dir-A", "dir-B"):
            c = Collector(_LoopThenCleanupFails(tag), store, host_label="bench",
                          log=lambda *a: None)
            try:
                cli.run_loop(c, store, 0.1)
            finally:
                c.close()
        disk = Store(store.dir)
        details = [r.get("cleanup_detail") or ""
                   for r in disk.outcomes() if r.get("record") == "teardown"]
        self.assertTrue(any("dir-A" in d for d in details),
                        "the first genuine failure was suppressed")
        self.assertTrue(any("dir-B" in d for d in details),
                        "the second genuine failure was suppressed")


# ---------------------------------------------------------------------------
# 3 - an unconfirmed master verdict after an ORDINARY failed provision
# ---------------------------------------------------------------------------
class TestFailedProvisionMasterVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr6prov.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _never_gone(self):
        return os.path.join(self.tmp, "never-gone")

    def test_failed_provision_master_verdict_is_persisted(self):
        """REGRESSION: an ordinary failed provision must lift the master verdict.

        The artifact transfer fails (a PROVISIONING failure after mkdir) while the
        control master cannot be confirmed stopped. On `46d7fa3` the verdict was only
        logged: no `teardown` record was written for it, because the collector lifts
        the transport's verdict only when a LIVE session is torn down.
        """
        store = Store(os.path.join(self.tmp, "data"))
        store.ensure()
        tr = harness.make_transport(
            self.tmp, "master_wont_exit",
            extra_env={"FAKE_SSH_EXTRA": "transfer_bad_json",
                       "FAKE_SSH_MASTER_STATE": self._never_gone()})
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], "artifact_mismatch", attempt["detail"])
        self.assertTrue(attempt["cleanup_ok"], "the directory was cleanly removed")
        c.close()
        disk = Store(store.dir)
        td = [r for r in disk.outcomes() if r.get("record") == "teardown"]
        self.assertEqual(len(td), 1, "the master verdict was not lifted into evidence")
        self.assertEqual(td[0]["outcome"], "lifecycle_failure")
        self.assertEqual(td[0]["master_shutdown"], transport.MS_STILL_ANSWERING)
        self.assertTrue(td[0]["lifecycle_error"])
        self.assertIsNone(td[0]["cleanup_detail"],
                          "a master-only failure named a directory for deletion")
        self.assertEqual(disk.pending_cleanup(), [])

    def test_failed_provision_lifecycle_does_not_mask_the_collection_failure(self):
        """REGRESSION: exit-status precedence is preserved through the CLI.

        The collection fails (provisioning) AND the master cannot be confirmed
        stopped; the run must exit with the COLLECTION failure, not the cleanup code,
        while the lifecycle evidence is still durable.
        """
        data = os.path.join(self.tmp, "data-cli")
        harness.write_ssh_config(
            data, self.tmp, mode="master_wont_exit",
            extra_env={"FAKE_SSH_EXTRA": "transfer_bad_json",
                       "FAKE_SSH_MASTER_STATE": self._never_gone()})
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, cli.EXIT_COLLECTION_FAILURE,
                         "the lifecycle failure masked the collection failure:\n"
                         + r.stderr)
        self.assertIn("outcome=artifact_mismatch", r.stderr)
        self.assertIn("master shutdown", r.stderr)
        store = Store(data)
        td = [x for x in store.outcomes() if x.get("record") == "teardown"]
        self.assertEqual(len(td), 1, "the unconfirmed shutdown was not persisted")
        self.assertEqual(td[0]["master_shutdown"], transport.MS_STILL_ANSWERING)

    def test_a_confirmed_failed_provision_records_nothing_extra(self):
        """GUARD: a failed provision whose master stopped cleanly adds no lifecycle record.

        Only the provisioning failure is recorded; no `teardown` record is invented.
        """
        store = Store(os.path.join(self.tmp, "data-clean"))
        store.ensure()
        tr = harness.make_transport(
            self.tmp, "ok",
            extra_env={"FAKE_SSH_EXTRA": "transfer_bad_json",
                       "FAKE_SSH_MASTER_STATE": os.path.join(self.tmp, "gone-ok")})
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], "artifact_mismatch", attempt["detail"])
        c.close()
        disk = Store(store.dir)
        self.assertEqual([r for r in disk.outcomes() if r.get("record") == "teardown"],
                         [], "a cleanly stopped master produced a lifecycle record")
        self.assertIsNone(c.lifecycle_error)


# ---------------------------------------------------------------------------
# 4 - a first-run cancellation must not cry "EVIDENCE NOT PERSISTED"
# ---------------------------------------------------------------------------
class TestFirstRunCancellation(_Base):
    def test_cancelled_first_provision_reports_no_false_persistence_error(self):
        """REGRESSION: a first-ever cancellation records teardown honestly.

        Ctrl-C during the FIRST provisioning (after mkdir) with a failed removal. The
        durable `teardown` record and the pending-cleanup entry ARE written, but on
        `46d7fa3` the run ALSO logged `EVIDENCE NOT PERSISTED: no last attempt in
        state to annotate` - a storage failure that never happened.
        """
        logs = []
        store = self.store()
        ssh = _ScriptedSsh(after_mkdir=KeyboardInterrupt,
                           removal=_ScriptedSsh.REMOVAL_FAILED)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench", log=logs.append)
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        c.close()
        self.assertIsNone(c.persistence_error,
                          "a first-run cancellation reported a false persistence failure")
        self.assertFalse([l for l in logs if "EVIDENCE NOT PERSISTED" in l],
                         "a false EVIDENCE NOT PERSISTED line was logged: %r" % (logs,))
        # the honest evidence IS there
        disk = Store(store.dir)
        td = [r for r in disk.outcomes() if r.get("record") == "teardown"]
        self.assertEqual(len(td), 1, "the teardown record was not written")
        self.assertEqual(td[0]["outcome"], "cleanup_failure")
        self.assertEqual(len(disk.pending_cleanup()), 1,
                         "the uncertain directory was not queued")

    def test_a_genuine_teardown_write_failure_is_still_reported(self):
        """GUARD: a real write failure must remain visible."""
        store = self.store()
        # make the outcomes log unwritable by putting a DIRECTORY in its place
        blocked = os.path.join(str(store.dir), "outcomes.jsonl")
        os.mkdir(blocked)
        ssh = _ScriptedSsh(after_mkdir=KeyboardInterrupt,
                           removal=_ScriptedSsh.REMOVAL_FAILED)
        tr = self.scripted_transport(ssh)
        c = Collector(tr, store, host_label="bench", log=lambda *a: None)
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        c.close()
        self.assertIsNotNone(c.persistence_error,
                             "a genuine teardown-record write failure was hidden")
        self.assertIn("teardown record not persisted", c.persistence_error)


if __name__ == "__main__":
    unittest.main()
