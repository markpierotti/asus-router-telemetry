"""Offline regressions for the fourth follow-up (collector 1.5.4).

An independent review of `c2176ba` (collector 1.5.3) reproduced two gaps:

  R1  a legacy RELATIVE sample pointer was still tried against the current working
      directory BEFORE its owning store, so a same-path file under whatever
      directory the collector was started from overrode the state-selected sample.
      Both have the same transport and host, so the source guard could not tell
      them apart and the new sample was compared against another dataset
  R2  when an ESTABLISHED session lost its link and the removal of its directory
      failed, the attempt recorded `cleanup_ok: false` but the exact directory was
      never queued in `pending_cleanup.jsonl`. The next attempt reset the
      per-attempt cleanup error, and after a recovery the clean final close wrote no
      `teardown` record - so nothing durable named the directory for reconciliation

Nothing here contacts a router. The transport is the offline scenario transport
with scripted sessions, or - for the end-to-end link-loss rehearsal - the real
`SSHTransport` driven by the fake `ssh` process.

Tests whose docstring starts with REGRESSION fail on `c2176ba` and pass after the
correction. Tests whose docstring starts with GUARD pass on both: they pin what the
correction must NOT change (outcome classes, exit precedence, the single final
teardown record, absolute-path semantics, and that nothing is deleted or retried).

No test touches real evidence: every data directory is created under a fresh
temporary directory, and a "missing" sample is one this file itself removed there.
"""

import contextlib
import copy
import hashlib
import io
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
from rtax_telemetry.core import (                                         # noqa: E402
    OUTCOME_REPORTER_FAILURE, OUTCOME_SUCCESS, OUTCOME_UNREACHABLE, Collector,
    _RemoteFacts,
)
from rtax_telemetry.errors import CleanupError, TransportUnreachableError  # noqa: E402
from rtax_telemetry.mock import MockSession, ScenarioTransport            # noqa: E402
from rtax_telemetry.status import render_status                           # noqa: E402
from rtax_telemetry.store import Store                                    # noqa: E402
from test_followup2_regressions import _with_facts                        # noqa: E402

FUTURE_NAME = "29991231T235959.000000Z.json"


@contextlib.contextmanager
def chdir(path):
    """Temporarily change the working directory, restoring it unconditionally."""
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _quiet():
    return ScenarioTransport("success", log=lambda *a: None)


# ---------------------------------------------------------------------------
# R1 - a legacy relative pointer belongs to its owning store, not to the CWD
# ---------------------------------------------------------------------------
class TestLegacyRelativeLookup(unittest.TestCase):
    """The state pointer is rewritten to the RELATIVE form `1.5.2` persisted.

    The owned sample is a real one (collected through the collector with the
    offline transport) whose `monotonic` is 1000.0. A decoy is a copy of it, same
    source, with `monotonic` 500.0 and a `decoy` marker. A second collection at
    `monotonic` 1060.0 therefore derives `interval_seconds` 60.0 from the owned
    sample and 560.0 from the decoy - so the test can see which one was COMPARED
    AGAINST, not only which one was returned.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr7rel.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- fixtures ----------------------------------------------------------
    def _owned(self, root="a", data="data", legacy=True):
        """One accepted sample under <tmp>/<root>/<data>.

        With ``legacy`` the state pointer is rewritten exactly as `1.5.2` wrote it
        for a relative `--data-dir`: ``<data>/samples/<name>``.
        Returns ``(workdir, store, name, recorded_pointer, sample_doc)``.
        """
        work = os.path.join(self.tmp, root)
        os.makedirs(work)
        store = Store(os.path.join(work, data))
        attempt = Collector(_quiet(), store, host_label="bench",
                            monotonic=lambda: 1000.0).collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_SUCCESS, attempt["detail"])
        state = store.load_state()
        absolute = state["last_valid_sample"]["path"]
        self.assertTrue(os.path.isabs(absolute))
        name = os.path.basename(absolute)
        with open(absolute) as fh:
            doc = json.load(fh)
        recorded = absolute
        if legacy:
            recorded = os.path.join(data, "samples", name)
            state["last_valid_sample"]["path"] = recorded
            store.write_state(state)
        return work, store, name, recorded, doc

    @staticmethod
    def _decoy(path, doc):
        """A same-source sample from 'another dataset', written at ``path``."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fake = copy.deepcopy(doc)
        fake["decoy"] = True
        fake["monotonic"] = 500.0
        with open(path, "w") as fh:
            json.dump(fake, fh)
        return path

    def _elsewhere(self, name="b"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        return path

    def _second_collector(self, store, logs=None):
        tr = _quiet()
        tr.n = 1                    # counters advance past the first sample's
        return Collector(tr, Store(store.dir), host_label="bench",
                         monotonic=lambda: 1060.0,
                         log=(logs.append if logs is not None else None))

    @staticmethod
    def _derived(attempt):
        with open(attempt["sample_path"]) as fh:
            return json.load(fh)["derived"]["cpu_utilisation"]

    # -- the reproduced defect ---------------------------------------------
    def test_a_cwd_decoy_cannot_override_the_state_sample_for_the_collector(self):
        """REGRESSION: `_predecessor()` must not take a same-path file from the CWD.

        The reviewer's reproduction: directory A's store holds the state-selected
        sample, directory B holds a decoy at the SAME relative path, both from the
        same transport and host. Reading A's store from B returned the decoy on
        `c2176ba`, and the next sample was compared against it.
        """
        _work, store, _name, recorded, doc = self._owned()
        other = self._elsewhere()
        self._decoy(os.path.join(other, recorded), doc)
        with chdir(other):
            c = self._second_collector(store)
            prev, meta = c._predecessor()
            self.assertIsNotNone(prev, "the state predecessor was not located")
            self.assertNotIn("decoy", prev,
                             "the CWD decoy was returned as the predecessor")
            self.assertEqual(prev["monotonic"], 1000.0)
            self.assertEqual(meta["path"], recorded, "the recorded pointer was altered")
            attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_SUCCESS, attempt["detail"])
        derived = self._derived(attempt)
        self.assertEqual(derived["state"], "ok")
        self.assertEqual(derived["interval_seconds"], 60.0,
                         "the new sample was COMPARED AGAINST the decoy (560.0 would "
                         "be the decoy's interval)")

    def test_a_cwd_decoy_cannot_override_the_state_sample_for_the_store(self):
        """REGRESSION: the store's own state-selected lookup, same collision.

        `resolve_sample_path()` tried the relative string against the CWD first, so
        `Store.last_sample()` (what `status` reads) returned the decoy as well.
        """
        _work, store, name, recorded, doc = self._owned()
        other = self._elsewhere()
        decoy = self._decoy(os.path.join(other, recorded), doc)
        owned = os.path.join(str(store.samples_dir), name)
        with chdir(other):
            fresh = Store(store.dir)
            resolved = fresh.resolve_sample_path(recorded)
            last = fresh.last_sample()
            located = fresh._state_sample_path()
        self.assertIsNotNone(resolved)
        self.assertTrue(os.path.samefile(resolved, owned),
                        "resolved %r, not the owning store's %r" % (resolved, owned))
        self.assertFalse(os.path.samefile(resolved, decoy))
        self.assertTrue(os.path.samefile(located, owned))
        self.assertEqual(os.path.basename(resolved), name,
                         "the state-selected file name was not preserved")
        self.assertNotIn("decoy", last, "last_sample() returned the CWD decoy")
        self.assertEqual(last["monotonic"], 1000.0)

    def test_a_missing_owned_sample_is_not_replaced_by_a_cwd_decoy(self):
        """REGRESSION: an owned sample that is GONE must stay gone.

        With the owned file missing, the only file at the recorded relative path is
        the decoy under the CWD. On `c2176ba` that decoy became the predecessor; the
        honest answer is "not located", a logged first sample and null rates.
        """
        _work, store, name, recorded, doc = self._owned()
        os.remove(os.path.join(str(store.samples_dir), name))
        other = self._elsewhere()
        self._decoy(os.path.join(other, recorded), doc)
        logs = []
        with chdir(other):
            fresh = Store(store.dir)
            self.assertIsNone(fresh.resolve_sample_path(recorded),
                              "a missing owned sample resolved to something else")
            self.assertIsNone(fresh.last_sample(),
                              "last_sample() returned a sample although the "
                              "state-selected one is missing")
            c = self._second_collector(store, logs)
            prev, meta = c._predecessor()
            self.assertIsNone(prev, "a decoy stood in for the missing predecessor")
            self.assertEqual(meta["path"], recorded)
            attempt = c.collect_once()
        self.assertTrue([l for l in logs if "could not be located" in l],
                        "an unlocatable predecessor was not reported: %r" % (logs,))
        derived = self._derived(attempt)
        self.assertEqual(derived["state"], "first")
        self.assertIsNone(derived["interval_seconds"])
        self.assertIsNone(derived["aggregate"])

    def test_a_missing_owned_sample_is_not_replaced_by_the_newest_file_for_the_store(self):
        """REGRESSION: the store must not fall back to filename order past a pointer.

        State names a sample that is missing; a FUTURE-dated same-source file sits
        in the store's own samples directory. On `c2176ba` `last_sample()` fell back
        to "the lexicographically last file" and returned it as the last accepted
        sample. A state pointer is authoritative: missing means None.
        """
        _work, store, name, recorded, doc = self._owned()
        os.remove(os.path.join(str(store.samples_dir), name))
        self._decoy(os.path.join(str(store.samples_dir), FUTURE_NAME), doc)
        with chdir(self._elsewhere()):
            fresh = Store(store.dir)
            self.assertIsNone(fresh._state_sample_path())
            self.assertIsNone(fresh.resolve_sample_path(recorded))
            self.assertIsNone(fresh.last_sample(),
                              "a future-dated file was returned in place of the "
                              "missing state-selected sample")

    def test_a_missing_owned_sample_is_not_replaced_by_the_newest_file_for_the_collector(self):
        """GUARD: `_predecessor()` never chooses by filename order.

        Same fixture as above. The collector's lookup had no filename fallback on
        `c2176ba` either; this pins that it does not gain one.
        """
        _work, store, name, recorded, doc = self._owned()
        os.remove(os.path.join(str(store.samples_dir), name))
        self._decoy(os.path.join(str(store.samples_dir), FUTURE_NAME), doc)
        logs = []
        with chdir(self._elsewhere()):
            c = self._second_collector(store, logs)
            prev, _meta = c._predecessor()
            attempt = c.collect_once()
        self.assertIsNone(prev, "the newest file was substituted for the predecessor")
        self.assertEqual(self._derived(attempt)["state"], "first")
        self.assertTrue([l for l in logs if "could not be located" in l])

    def test_a_future_dated_file_does_not_displace_a_legacy_pointer(self):
        """GUARD: with the owned sample present, a future-dated file changes nothing."""
        _work, store, _name, recorded, doc = self._owned()
        self._decoy(os.path.join(str(store.samples_dir), FUTURE_NAME), doc)
        with chdir(self._elsewhere()):
            fresh = Store(store.dir)
            self.assertNotIn("decoy", fresh.last_sample())
            c = self._second_collector(store)
            prev, _meta = c._predecessor()
            attempt = c.collect_once()
        self.assertNotIn("decoy", prev)
        self.assertEqual(prev["monotonic"], 1000.0)
        self.assertEqual(self._derived(attempt)["interval_seconds"], 60.0)

    def test_the_writing_directory_still_resolves_to_the_owning_store(self):
        """GUARD: from the directory the relative path was written in, nothing changes.

        There the CWD-relative string and the owning store name the SAME file - the
        layout of the real `1.5.2` acceptance directory - so both revisions agree.
        """
        work, store, name, recorded, _doc = self._owned()
        owned = os.path.join(str(store.samples_dir), name)
        with chdir(work):
            self.assertTrue(os.path.samefile(recorded, owned))
            fresh = Store(store.dir)
            self.assertTrue(os.path.samefile(fresh.resolve_sample_path(recorded), owned))
            prev, _meta = self._second_collector(store)._predecessor()
        self.assertEqual(prev["monotonic"], 1000.0)

    # -- absolute pointers: the supported behaviour, stated explicitly -------
    def test_an_absolute_record_is_honoured_verbatim_from_any_directory(self):
        """GUARD: an absolute pointer names one file, whatever the CWD holds.

        Everything written since the data directory became absolute carries an
        absolute pointer. It is used exactly as recorded even when the CWD holds a
        same-named file at the old relative location and the store holds a
        future-dated one.
        """
        _work, store, name, recorded, doc = self._owned(legacy=False)
        self.assertTrue(os.path.isabs(recorded))
        other = self._elsewhere()
        self._decoy(os.path.join(other, "data", "samples", name), doc)
        self._decoy(os.path.join(str(store.samples_dir), FUTURE_NAME), doc)
        with chdir(other):
            fresh = Store(store.dir)
            self.assertEqual(fresh.resolve_sample_path(recorded),
                             os.path.abspath(recorded))
            prev, _meta = self._second_collector(store)._predecessor()
            last = fresh.last_sample()
        self.assertNotIn("decoy", prev)
        self.assertNotIn("decoy", last)

    def test_an_absolute_record_that_is_gone_is_reported_not_re_anchored(self):
        """GUARD: an absolute pointer is never re-anchored by its file name.

        The pointer names a file under a directory that no longer exists, while the
        store's own samples directory holds a file of the SAME name. Verbatim means
        verbatim: the answer is "not located", reported - not a guess that the
        same-named file is the same sample.
        """
        _work, store, name, _recorded, _doc = self._owned(legacy=False)
        gone = os.path.join(self.tmp, "moved-away", "data", "samples", name)
        state = store.load_state()
        state["last_valid_sample"]["path"] = gone
        store.write_state(state)
        self.assertTrue(os.path.isfile(os.path.join(str(store.samples_dir), name)))
        logs = []
        with chdir(self._elsewhere()):
            self.assertIsNone(Store(store.dir).resolve_sample_path(gone))
            prev, meta = self._second_collector(store, logs)._predecessor()
        self.assertIsNone(prev, "an absolute pointer was re-anchored by file name")
        self.assertEqual(meta["path"], gone)
        self.assertTrue([l for l in logs if "could not be located" in l])

    # -- what stays as it was ------------------------------------------------
    def test_without_a_state_pointer_the_status_fallback_is_unchanged(self):
        """GUARD: filename order is still the fallback when state names NO sample.

        With no `state.json` there is no selection to preserve; `last_sample()`
        (the `status` display) keeps returning the newest file by name. Only a
        pointer that EXISTS is authoritative.
        """
        store = Store(os.path.join(self.tmp, "nostate"))
        store.save_sample({"marker": "older"}, when=1_700_000_000.0)
        store.save_sample({"marker": "newer"}, when=1_700_000_100.0)
        self.assertIsNone(store.load_state())
        self.assertEqual(store.last_sample(), {"marker": "newer"})

    def test_a_lookup_never_rewrites_state_or_samples(self):
        """GUARD: resolving a legacy pointer reads; it rewrites nothing.

        Historical evidence carries relative pointers. Locating a sample through
        one must leave `state.json` and every sample byte-identical.
        """
        _work, store, name, recorded, doc = self._owned()
        other = self._elsewhere()
        self._decoy(os.path.join(other, recorded), doc)
        watched = [str(store.state_path), os.path.join(str(store.samples_dir), name)]
        before = [_sha(p) for p in watched]
        with chdir(other):
            fresh = Store(store.dir)
            fresh.resolve_sample_path(recorded)
            fresh.last_sample()
            self._second_collector(store)._predecessor()
        self.assertEqual([_sha(p) for p in watched], before,
                         "a lookup rewrote the state pointer or the sample")
        self.assertEqual(store.load_state()["last_valid_sample"]["path"], recorded)

    def test_status_does_not_say_no_sample_when_the_named_one_is_missing(self):
        """REGRESSION: `status` must not print "(no valid sample yet)" over a pointer.

        State names a sample that is missing. The old wording was reached only when
        no file at all could stand in; with the pointer authoritative it would be
        printed for a data directory that DID accept a sample.
        """
        _work, store, name, _recorded, _doc = self._owned()
        os.remove(os.path.join(str(store.samples_dir), name))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["status", "--transport", "mock", "--data-dir", str(store.dir)])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("the sample named by state could not be read", text)
        self.assertNotIn("(no valid sample yet)", text)


# ---------------------------------------------------------------------------
# R2 - a live session's unconfirmed directory is queued when it is learned
# ---------------------------------------------------------------------------
def _dir(n):
    return "/tmp/rt.%08x" % n


class _Bench(ScenarioTransport):
    """Numbered sessions; chosen collections lose the link; chosen directories stay.

    ``fail_calls``  1-based `collect_sample()` call numbers that raise `unreachable`
    ``stuck``       remote directories whose removal cannot be confirmed
    ``stop_at``     the `collect_sample()` call that raises KeyboardInterrupt
                    (how a loop under test is ended)
    ``master``      a ``(master_shutdown, lifecycle_error)`` pair the transport
                    reports once a session has been stopped (master-only verdict)
    """

    def __init__(self, fail_calls=(), stuck=(), stop_at=None, master=None):
        super().__init__("success", log=lambda *a: None)
        self.fail_calls = set(fail_calls)
        self.stuck = set(stuck)
        self.stop_at = stop_at
        self.master = master
        self.starts = 0
        self.calls = 0
        self.stops = []

    def start(self):
        self.starts += 1
        return MockSession(_dir(self.starts), "owned")

    def collect_sample(self, session, timeout=None):
        self.calls += 1
        if self.stop_at is not None and self.calls >= self.stop_at:
            raise KeyboardInterrupt
        if self.calls in self.fail_calls:
            raise TransportUnreachableError(
                "management address unreachable: mux_client_request_session: read "
                "from master failed: Broken pipe")
        return super().collect_sample(session)

    def stop(self, session):
        self.stops.append(session.remote_dir)
        if self.master is not None:
            self.master_shutdown, self.lifecycle_error = self.master
        if session.remote_dir in self.stuck:
            raise CleanupError(
                "failed to remove router dir %s: management address unreachable: "
                "mux_client_request_session: read from master failed: Broken pipe"
                % session.remote_dir)


class _R2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtaxr7cln.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def store(self, name="data"):
        store = Store(os.path.join(self.tmp, name))
        store.ensure()
        return store

    @staticmethod
    def attempts(store):
        return [r for r in Store(store.dir).outcomes() if r.get("record") != "teardown"]

    @staticmethod
    def teardowns(store):
        return [r for r in Store(store.dir).outcomes() if r.get("record") == "teardown"]

    @staticmethod
    def pending(store):
        return Store(store.dir).pending_cleanup()

    def pending_dirs(self, store):
        return [p["remote_dir"] for p in self.pending(store)]

    @staticmethod
    def block(path):
        """Make an append-only log unwritable by putting a DIRECTORY in its place."""
        os.mkdir(str(path))


class TestLiveSessionCleanupQueue(_R2Base):

    def test_link_loss_then_recovery_then_clean_close_queues_the_lost_directory(self):
        """REGRESSION: the reviewer's sequence, on the real Collector and Store.

        success in /tmp/rt.00000001 -> the link drops: the collection fails AND the
        removal of that directory fails -> a new session collects -> clean close.
        On `c2176ba` the failed attempt kept `cleanup_ok: false`, but there was no
        `pending_cleanup.jsonl` at all and `status` showed nothing to reconcile.
        """
        store = self.store()
        logs = []
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),)), store,
                      host_label="bench", log=logs.append)
        first = c.collect_once()
        second = c.collect_once()

        # IMMEDIATELY: before any later attempt can reset the per-attempt fields
        self.assertEqual(self.pending_dirs(store), [_dir(1)],
                         "the lost directory was not queued when it became uncertain")

        third = c.collect_once()
        c.close()
        c.close()                                   # the CLI's second finalization

        # outcomes and the attempt's own evidence are unchanged
        self.assertEqual([a["outcome"] for a in (first, second, third)],
                         [OUTCOME_SUCCESS, OUTCOME_UNREACHABLE, OUTCOME_SUCCESS])
        self.assertIs(second["cleanup_ok"], False)
        self.assertEqual(second["remote_dir"], _dir(1))
        self.assertEqual(second["remote_ownership"], "owned")
        self.assertIn("failed to remove router dir %s" % _dir(1), second["cleanup_detail"])
        self.assertEqual(third["remote_dir"], _dir(2))
        self.assertIs(third["cleanup_ok"], True)
        self.assertEqual([a["outcome"] for a in self.attempts(store)],
                         [OUTCOME_SUCCESS, OUTCOME_UNREACHABLE, OUTCOME_SUCCESS])

        # exactly one durable entry, with the exact path and the ownership basis
        pend = self.pending(store)
        self.assertEqual(len(pend), 1, "queued %d times: %r" % (len(pend), pend))
        self.assertEqual(pend[0]["remote_dir"], _dir(1))
        self.assertEqual(pend[0]["remote_ownership"], "owned")
        self.assertEqual(pend[0]["reason"], second["cleanup_detail"])
        self.assertIs(pend[0]["manual_reconciliation_required"], True)
        self.assertIs(pend[0]["automatic_deletion"], False)

        # the final close WAS clean, so it writes no teardown record and reports no
        # failure of its own - that part is unchanged, and is why the entry above
        # has to exist
        self.assertEqual(self.teardowns(store), [])
        self.assertIsNone(c.cleanup_error)
        self.assertIsNone(c.lifecycle_error)
        self.assertIsNone(c.persistence_error)
        self.assertIs(Store(store.dir).load_state()["last_attempt"]["cleanup_ok"], True)

        # what the operator sees afterwards
        status = render_status(Store(store.dir), Store(store.dir).load_state(),
                               transport_name="mock")
        self.assertIn("unconfirmed cleanup : 1 entry", status)
        self.assertIn(_dir(1), status)

    def test_each_lost_directory_is_queued_once_and_in_order(self):
        """REGRESSION: two DISTINCT losses in one run are both kept.

        Sessions 1 and 2 each lose their link and leave their own directory; session
        3 recovers and the run closes cleanly. Both paths must be queued, each once.
        """
        store = self.store()
        c = Collector(_Bench(fail_calls=(2, 4), stuck=(_dir(1), _dir(2))), store,
                      host_label="bench")
        got = [c.collect_once() for _ in range(5)]
        c.close()
        self.assertEqual([a["outcome"] for a in got],
                         [OUTCOME_SUCCESS, OUTCOME_UNREACHABLE, OUTCOME_SUCCESS,
                          OUTCOME_UNREACHABLE, OUTCOME_SUCCESS])
        self.assertEqual([a["remote_dir"] for a in got],
                         [_dir(1), _dir(1), _dir(2), _dir(2), _dir(3)])
        self.assertEqual(self.pending_dirs(store), [_dir(1), _dir(2)])
        self.assertEqual(self.teardowns(store), [])

    def test_the_same_path_lost_twice_is_two_events(self):
        """REGRESSION: the queue is keyed on the EVENT, not on the path string.

        The offline transport reuses one directory name for every session. Two
        separate sessions that each fail to remove it are two uncertainties, and a
        de-duplication by path would have hidden the second.
        """
        store = self.store()

        class _SamePath(_Bench):
            def start(self):
                self.starts += 1
                return MockSession("/tmp/rt.5a3e9a7b", "owned")

        c = Collector(_SamePath(fail_calls=(2, 4), stuck=("/tmp/rt.5a3e9a7b",)), store,
                      host_label="bench")
        for _ in range(4):
            c.collect_once()
        c.close()
        self.assertEqual(self.pending_dirs(store), ["/tmp/rt.5a3e9a7b"] * 2)
        self.assertEqual(len(self.teardowns(store)), 1,
                         "the last loss was not also the final-close record")

    def test_the_final_close_does_not_queue_a_directory_twice(self):
        """GUARD: duplicate finalization. One loss, no recovery, close() twice.

        The loop's `finally` and the CLI's `finally` both call `close()`. On
        `c2176ba` the single entry was written by the first close; now it is written
        by the attempt, and neither close may add another.
        """
        store = self.store()
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),)), store, host_label="bench")
        c.collect_once()
        c.collect_once()
        c.close()
        c.close()
        self.assertEqual(self.pending_dirs(store), [_dir(1)])
        td = self.teardowns(store)
        self.assertEqual(len(td), 1, "the final-close record was duplicated or lost")
        self.assertEqual(td[0]["outcome"], "cleanup_failure")
        self.assertEqual(td[0]["remote_dir"], _dir(1))
        self.assertIsNotNone(c.cleanup_error, "the SEPARATE FAILURE verdict was lost")

    def test_a_close_failure_after_an_earlier_loss_keeps_both(self):
        """REGRESSION: an earlier loss and a DIFFERENT failure at the final close.

        Session 1's directory is lost mid-run; session 2 recovers; at the end
        session 2's own removal fails. On `c2176ba` only the second was queued.
        """
        store = self.store()
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1), _dir(2))), store,
                      host_label="bench")
        for _ in range(3):
            c.collect_once()
        self.assertEqual(self.pending_dirs(store), [_dir(1)])
        c.close()
        c.close()
        self.assertEqual(self.pending_dirs(store), [_dir(1), _dir(2)])
        td = self.teardowns(store)
        self.assertEqual(len(td), 1)
        self.assertEqual(td[0]["remote_dir"], _dir(2),
                         "the final-close record must describe the FINAL teardown")
        self.assertEqual(td[0]["outcome"], "cleanup_failure")

    def test_master_only_uncertainty_queues_no_directory(self):
        """GUARD: an unconfirmed MASTER is not a directory to reconcile.

        The link drops, the directory IS removed, but the control master cannot be
        confirmed stopped. Nothing may be queued; the master's verdict goes to the
        final `teardown` record, exactly as before.
        """
        store = self.store()
        verdict = (transport.MS_STILL_ANSWERING,
                   "ssh control master shutdown is NOT confirmed (%s)"
                   % transport.MS_STILL_ANSWERING)
        c = Collector(_Bench(fail_calls=(2,), master=verdict), store, host_label="bench")
        got = [c.collect_once() for _ in range(3)]
        c.close()
        self.assertEqual(got[1]["outcome"], OUTCOME_UNREACHABLE)
        self.assertIs(got[1]["cleanup_ok"], True)
        self.assertEqual(self.pending(store), [],
                         "a master-only uncertainty named a directory for removal")
        td = self.teardowns(store)
        self.assertEqual(len(td), 1)
        self.assertEqual(td[0]["outcome"], "lifecycle_failure")
        self.assertEqual(td[0]["master_shutdown"], transport.MS_STILL_ANSWERING)
        self.assertIsNone(td[0]["cleanup_detail"])
        self.assertIsNone(c.cleanup_error)

    def test_a_lost_directory_with_an_unconfirmed_master_queues_only_the_directory(self):
        """REGRESSION: both at once - one queue entry, for the directory alone."""
        store = self.store()
        verdict = (transport.MS_PROBE_TIMEOUT,
                   "ssh control master shutdown is NOT confirmed (%s)"
                   % transport.MS_PROBE_TIMEOUT)
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),), master=verdict), store,
                      host_label="bench")
        for _ in range(3):
            c.collect_once()
        c.close()
        self.assertEqual(self.pending_dirs(store), [_dir(1)])
        td = self.teardowns(store)
        self.assertEqual(len(td), 1)
        # the directory's own failure belongs to its attempt; what is still
        # unconfirmed at the final close is the master
        self.assertEqual(td[0]["outcome"], "lifecycle_failure")
        self.assertIsNone(td[0]["cleanup_detail"])

    def test_a_cancelled_provision_is_queued_without_waiting_for_close(self):
        """REGRESSION: a cancellation's facts are queued where they are learned.

        Ctrl-C ends a provision after the mkdir was acknowledged and the removal
        failed. A cancelled attempt has no attempt record, and on `c2176ba` the path
        reached `pending_cleanup.jsonl` only if `close()` ran next.
        """
        store = self.store()

        class _CancelledStart(ScenarioTransport):
            def start(self):
                raise _with_facts(KeyboardInterrupt())

        c = Collector(_CancelledStart("success", log=lambda *a: None), store,
                      host_label="bench")
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        self.assertEqual(self.pending_dirs(store), ["/tmp/rt.a11fac75"],
                         "the cancelled provision's directory waited for close()")
        c.close()
        self.assertEqual(self.pending_dirs(store), ["/tmp/rt.a11fac75"],
                         "close() queued the same directory again")
        self.assertEqual(len(self.teardowns(store)), 1)

    def test_an_unqueued_directory_is_flushed_before_the_per_attempt_reset(self):
        """REGRESSION: the reset at the start of an attempt cannot lose a path.

        The invariant behind the fix, checked directly: whatever left an
        unconfirmed directory un-queued, the next attempt queues it BEFORE it resets
        `cleanup_error`.
        """
        store = self.store()
        c = Collector(_Bench(), store, host_label="bench")
        c._last_session = _RemoteFacts("/tmp/rt.0badc0de", "unknown")
        c.cleanup_error = "removal of the remote directory is UNCONFIRMED (test)"
        c._cleanup_queued = False
        attempt = c.collect_once()
        self.assertEqual(attempt["outcome"], OUTCOME_SUCCESS)
        pend = self.pending(store)
        self.assertEqual([p["remote_dir"] for p in pend], ["/tmp/rt.0badc0de"])
        self.assertEqual(pend[0]["remote_ownership"], "unknown")
        self.assertIsNone(c.cleanup_error, "the per-attempt reset did not happen")


class TestEvidenceWriteFailures(_R2Base):

    def test_a_failed_queue_write_is_reported_and_stays_reported(self):
        """REGRESSION: the queue entry could not be written - say so, and keep saying.

        `pending_cleanup.jsonl` is unwritable. The attempt record still carries the
        directory; the run must report `EVIDENCE NOT PERSISTED` naming that
        directory, and a later successful attempt must not clear the report.
        On `c2176ba` nothing tried to write the entry, so nothing was reported.
        """
        store = self.store()
        self.block(store.pending_cleanup_path)
        logs = []
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),)), store,
                      host_label="bench", log=logs.append)
        c.collect_once()
        second = c.collect_once()
        self.assertIsNotNone(c.persistence_error,
                             "an unwritten queue entry was not reported")
        self.assertIn("pending-cleanup evidence", c.persistence_error)
        self.assertIn(_dir(1), c.persistence_error)
        third = c.collect_once()
        c.close()
        self.assertEqual(third["outcome"], OUTCOME_SUCCESS)
        self.assertIsNotNone(c.persistence_error, "a later success cleared the report")
        self.assertIn(_dir(1), c.persistence_error)
        self.assertTrue([l for l in logs if "EVIDENCE NOT PERSISTED" in l])
        # nothing is claimed that was not written ...
        self.assertEqual(self.pending(store), [])
        # ... and the attempt's own record (a different file) still names the path
        recorded = self.attempts(store)[1]
        self.assertIs(recorded["cleanup_ok"], False)
        self.assertEqual(recorded["remote_dir"], _dir(1))
        self.assertEqual(recorded["cleanup_detail"], second["cleanup_detail"])

    def test_a_failed_queue_write_is_not_displaced_by_a_failed_attempt_write(self):
        """REGRESSION: two evidence writes fail in one attempt; both are reported.

        The queue entry AND the attempt record are unwritable. The report must name
        both: the earlier revision REPLACED the message, so the more specific one
        (which directory was not queued) would have been lost.
        """
        store = self.store()
        self.block(store.pending_cleanup_path)
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),)), store, host_label="bench")
        c.collect_once()                            # healthy: the log exists
        os.replace(str(store.outcomes_path), str(store.outcomes_path) + ".kept")
        self.block(store.outcomes_path)             # ... and is now unwritable too
        c.collect_once()                            # the queue write fails, THEN the record
        self.assertIsNotNone(c.persistence_error)
        self.assertIn("pending-cleanup evidence for %s" % _dir(1), c.persistence_error)
        self.assertIn("outcome record not persisted", c.persistence_error)

    def test_a_queue_write_cut_short_by_a_cancellation_is_retried_at_close(self):
        """REGRESSION: Ctrl-C lands INSIDE the queue write; the path is not lost.

        The first append is interrupted before anything is written. The
        cancellation propagates - it is never swallowed - and the final close
        queues the directory after all. A write that merely FAILED is reported and
        not retried (the tests above); only an interrupted one is.
        """
        store = self.store()
        real_append = store.append_pending_cleanup
        calls = []

        def interrupted_once(rec):
            calls.append(rec["remote_dir"])
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_append(rec)

        store.append_pending_cleanup = interrupted_once
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),)), store, host_label="bench")
        c.collect_once()
        with self.assertRaises(KeyboardInterrupt):
            c.collect_once()
        self.assertEqual(self.pending(store), [])
        c.close()                                   # what the CLI does next
        self.assertEqual(calls, [_dir(1), _dir(1)])
        self.assertEqual(self.pending_dirs(store), [_dir(1)],
                         "an interrupted queue write was not retried at the final close")
        self.assertEqual(len(self.teardowns(store)), 1)
        self.assertIsNone(c.persistence_error)

    def test_cli_exit_status_reports_an_unwritten_queue_entry(self):
        """REGRESSION: a real write failure through the CLI names the lost directory.

        `--once`, the offline `provision_failure` scenario (a directory whose
        creation was never acknowledged) and an unwritable queue. The collection
        failure keeps exit 1 - a persistence failure never masks it; that part
        already held. What fails on `c2176ba` is the message: it said the queue
        entry was not persisted without saying WHICH directory was not queued, and
        with the queue unwritable that line is the only place the path is tied to
        "not queued".
        """
        data = os.path.join(self.tmp, "data-cli")
        os.makedirs(data)
        os.mkdir(os.path.join(data, "pending_cleanup.jsonl"))
        r = harness.run_cli(["collect", "--once", "--transport", "mock",
                             "--mock-scenario", "provision_failure", "--data-dir", data])
        self.assertEqual(r.returncode, cli.EXIT_COLLECTION_FAILURE, r.stderr)
        self.assertIn("outcome=provisioning_failure", r.stderr)
        self.assertIn("EVIDENCE NOT PERSISTED", r.stderr)
        self.assertIn("pending-cleanup evidence for /tmp/rt.mockprov", r.stderr)


class TestOutcomeAndExitPrecedence(_R2Base):

    def test_the_loop_keeps_its_exit_status_and_queues_the_directory(self):
        """REGRESSION: `run_loop` - loss, recovery, Ctrl-C, both finalizations.

        The exit status is the interruption's (130), not a cleanup code: the final
        close is clean. The lost directory is queued, once.
        """
        store = self.store()
        c = Collector(_Bench(fail_calls=(2,), stuck=(_dir(1),), stop_at=4), store,
                      host_label="bench")
        rc = cli.run_loop(c, store, 0.05)           # run_loop's finally -> close() #1
        c.close()                                   # cmd_collect's finally -> close() #2
        self.assertEqual(rc, cli.EXIT_INTERRUPTED)
        self.assertEqual([a["outcome"] for a in self.attempts(store)],
                         [OUTCOME_SUCCESS, OUTCOME_UNREACHABLE, OUTCOME_SUCCESS])
        self.assertEqual(self.pending_dirs(store), [_dir(1)])
        self.assertEqual(self.teardowns(store), [])
        self.assertIsNone(c.cleanup_error)
        self.assertIsNone(c.persistence_error)

    def test_cli_once_reports_the_collection_failure_and_queues_once(self):
        """GUARD: `--once`, real CLI and real transport - precedence and one entry.

        The reporter fails AND the removal fails. The run exits 1 (the collection
        failure, which the cleanup failure must not mask), prints the SEPARATE
        FAILURE line, writes one teardown record and queues the directory ONCE -
        the attempt and the final close are the same event.
        """
        data = os.path.join(self.tmp, "data-cli")
        harness.write_ssh_config(
            data, self.tmp, mode="cleanup_fail",
            extra_env={"FAKE_SSH_EXTRA": "reporter_fail",
                       "FAKE_SSH_MASTER_STATE": os.path.join(self.tmp, "master.gone")})
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, cli.EXIT_COLLECTION_FAILURE, r.stderr)
        self.assertIn("outcome=%s" % OUTCOME_REPORTER_FAILURE, r.stderr)
        self.assertIn("cleanup: SEPARATE FAILURE", r.stderr)
        store = Store(data)
        attempts = self.attempts(store)
        self.assertEqual(len(attempts), 1)
        self.assertIs(attempts[0]["cleanup_ok"], False)
        self.assertEqual(self.pending_dirs(store), [attempts[0]["remote_dir"]],
                         "one failed removal must be queued exactly once")
        self.assertEqual(len(self.teardowns(store)), 1)


class TestRealTransportLinkLossRehearsal(_R2Base):
    """The dropout trial's state machine, offline: real transport, fake `ssh`."""

    def test_link_loss_incident_recovery_and_clean_close(self):
        """REGRESSION: an established session loses its link, then recovers.

        The real `SSHTransport`, `Collector` and `Store` against the fake `ssh`:

          1  healthy attempt provisions a directory
          2  the link is pulled. `ssh -O check` still succeeds - it asks the LOCAL
             master, not the device - so the loss surfaces in the multiplexed
             reporter run, and the removal that follows fails the same way
          3,4  no master can be established while the link is down
          5,6  the link returns: a new master, a new directory, two successes

        On `c2176ba` attempt 2 kept `cleanup_ok: false` but nothing queued its
        directory, and after the recovery the clean close left no record of it.
        """
        store = self.store()
        link_down = os.path.join(self.tmp, "link.down")
        master_gone = os.path.join(self.tmp, "master.gone")
        cmd_log = os.path.join(self.tmp, "device-commands.log")
        logs = []
        tr = harness.make_transport(
            self.tmp, "ok", log=logs.append,
            extra_env={"FAKE_SSH_LINK_DOWN": link_down,
                       "FAKE_SSH_MASTER_STATE": master_gone,
                       "FAKE_SSH_CMD_LOG": cmd_log})
        c = Collector(tr, store, host_label="bench", log=logs.append)

        first = c.collect_once()
        self.assertEqual(first["outcome"], OUTCOME_SUCCESS, first["detail"])
        lost = first["remote_dir"]
        self.assertEqual(first["remote_ownership"], "owned")

        with open(link_down, "w"):                  # the bench link is pulled
            pass
        checks = []
        real_check = tr._master_check
        tr._master_check = lambda: checks.append(real_check()) or checks[-1]
        second = c.collect_once()
        tr._master_check = real_check
        self.assertEqual(second["outcome"], OUTCOME_UNREACHABLE, second["detail"])
        self.assertTrue(checks and checks[0][0] is True,
                        "the local master check was expected to PASS on a dead link; "
                        "the loss must surface in the command that follows it")
        self.assertEqual(second["remote_dir"], lost)
        self.assertEqual(second["remote_ownership"], "owned")
        self.assertIs(second["cleanup_ok"], False)
        self.assertIn("failed to remove router dir %s" % lost, second["cleanup_detail"])
        # the old LOCAL master was asked to exit and the follow-up check found it gone
        self.assertEqual(tr.master_shutdown, transport.MS_GONE)
        self.assertIsNone(c.lifecycle_error)
        self.assertEqual(self.pending_dirs(store), [lost],
                         "the lost directory was not queued when it became uncertain")

        os.unlink(master_gone)                      # a new master is a new process
        third = c.collect_once()
        fourth = c.collect_once()
        for attempt in (third, fourth):
            self.assertEqual(attempt["outcome"], OUTCOME_UNREACHABLE, attempt["detail"])
            self.assertIsNone(attempt["remote_dir"])
            self.assertEqual(attempt["remote_ownership"], "none")
            self.assertIs(attempt["cleanup_ok"], True)
        state = Store(store.dir).load_state()
        self.assertEqual(state["consecutive_failures"], 3)
        self.assertEqual(state["incident"]["opened_at"], fourth["at"],
                         "the incident must open on the THIRD consecutive failure")

        os.unlink(link_down)                        # the link returns
        fifth = c.collect_once()
        self.assertEqual(fifth["outcome"], OUTCOME_SUCCESS, fifth["detail"])
        self.assertNotEqual(fifth["remote_dir"], lost, "a new session reused the path")
        self.assertIsNotNone(Store(store.dir).load_state()["incident"],
                             "the incident must stay open after ONE success")
        sixth = c.collect_once()
        self.assertEqual(sixth["outcome"], OUTCOME_SUCCESS, sixth["detail"])
        c.close()
        c.close()

        state = Store(store.dir).load_state()
        self.assertIsNone(state["incident"])
        self.assertEqual(state["last_resolved_incident"]["resolved_at"], sixth["at"])
        self.assertEqual([a["outcome"] for a in self.attempts(store)],
                         [OUTCOME_SUCCESS] + [OUTCOME_UNREACHABLE] * 3
                         + [OUTCOME_SUCCESS] * 2)

        # the final close was clean: verified master exit, no teardown record
        self.assertEqual(tr.master_shutdown, transport.MS_GONE)
        self.assertEqual(self.teardowns(store), [])
        self.assertIsNone(c.cleanup_error)
        self.assertIsNone(c.persistence_error)

        # ... and the lost directory is still on record, exactly once
        pend = self.pending(store)
        self.assertEqual([p["remote_dir"] for p in pend], [lost])
        self.assertEqual(pend[0]["remote_ownership"], "owned")
        self.assertIs(pend[0]["automatic_deletion"], False)

        # NOTHING retried the removal: the device was asked to remove the lost
        # directory once (while the link was down) and never again
        with open(cmd_log) as fh:
            sent = fh.read().splitlines()
        self.assertEqual(sum(1 for s in sent if 'rm -rf "%s"' % lost in s), 1,
                         "the uncertain directory's removal was retried automatically")
        self.assertEqual(sum(1 for s in sent if 'rm -rf "%s"' % fifth["remote_dir"] in s),
                         1)


if __name__ == "__main__":
    unittest.main()
