#!/usr/bin/env python3
"""Regression tests for the third (Claude/Codex) review remediation — offline.

Each test drives the REAL mechanism where the defect lived: the real remote
command text, real child processes, real signals, real files and real locks
against the fake `ssh` stand-in. No network I/O and no router contact.

Findings covered (F-numbers are the review's):

  F3  parser containment      control-byte and empty known-answer replies, an
                              unbounded numeric marker, an unreadable artifact, a
                              missing ssh binary, a wrong-shaped state file and a
                              storage error are all CLASSIFIED and RECORDED
  F4  diagnostics             hostile 200 KiB ESC-laden stderr from a device,
                              huge schema key names/values and control bytes never
                              reach the terminal or the outcome log unbounded
  F7  reporter contract       a reporter document with `cpu: null` and
                              `loadavg: null` is ACCEPTED, not discarded
  F1  master liveness         a lost control master is detected before any
                              multiplexed command and never falls back to an
                              interactive-authentication attempt
  F2  ownership/cleanup       tri-state ownership, failed-provision cleanup
                              consequences, durable teardown and pending-cleanup
                              evidence, and storage-refusal reporting
  F5  lifecycle               SIGHUP/SIGTERM reach teardown in BOTH modes,
                              SIGINT during handover is an interruption, master
                              shutdown is VERIFIED
  F6  identity                explicit boot identity beats the uptime heuristic,
                              the predecessor comes from state (not filename
                              order) and must match the source, and an
                              idle/iowait reclassification is not a reset
  F8/F9 storage & ssh         file modes, torn-line repair, state quarantine, a
                              symlinked lock, hermetic ssh options, askpass scrub
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
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import cli, cpu, mock, schema, transport             # noqa: E402
from rtax_telemetry.core import (                                        # noqa: E402
    Collector, HARD_STOP_OUTCOMES, OUTCOME_MASTER_LOST, OUTCOME_INTERNAL,
    OUTCOME_PROVISION, OUTCOME_STORAGE, OUTCOME_TRANSPORT, is_hard_stop,
    update_state, default_state,
)
from rtax_telemetry.errors import (                                      # noqa: E402
    CleanupError, ControlMasterLost, MalformedData, ProvisioningError,
    SchemaInvalid, StorageError, TransportAuthError, TransportError,
    TransportHostKeyError, TransportUnreachableError, sanitize_detail,
)
from rtax_telemetry.mock import ScenarioTransport                        # noqa: E402
from rtax_telemetry.store import Store                                   # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "live_sample.json")


def load_fixture_bytes():
    with open(FIXTURE, "rb") as fh:
        return fh.read()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-rem.")
        transport.REAP_GRACE = 0.5

    def tearDown(self):
        transport.REAP_GRACE = 3.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, name="data", **kw):
        s = Store(os.path.join(self.tmp, name), **kw)
        s.ensure()
        return s

    def collector(self, scenario, store=None, **kw):
        store = store or self.store()
        tr = ScenarioTransport(scenario, log=lambda *a: None)
        return Collector(tr, store, log=lambda *a: None, **kw), store


# ================================================================= F3: parser
class TestParserContainment(TempCase):
    """A device reply the transport cannot tokenise must never escape as an
    IndexError / ValueError that loses the attempt."""

    def _attempt(self, mode, **kwargs):
        tr = harness.make_transport(self.tmp, mode, **kwargs)
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        return a, c, store

    def test_control_byte_known_answer_is_classified_and_recorded(self):
        a, c, store = self._attempt("kat_control_byte")
        self.assertEqual(a["outcome"], OUTCOME_PROVISION, a["detail"])
        self.assertIn("known-answer test FAILED", a["detail"])
        self.assertTrue(os.path.isfile(store.outcomes_path),
                        "the failing attempt was not recorded")
        self.assertEqual(len(store.outcomes()), 1)
        c.close()

    def test_empty_known_answer_reply_is_classified(self):
        a, c, _ = self._attempt("kat_empty")
        self.assertEqual(a["outcome"], OUTCOME_PROVISION)
        self.assertIn("<empty>", a["detail"])
        c.close()

    def test_whitespace_only_known_answer_token_is_classified(self):
        # the exact IndexError reproducer at the transport level: replies made only
        # of characters Python's str.split() treats as whitespace, INCLUDING the
        # 0x1C-0x1F control range that a BYTE test would miss
        for raw in (b"\x1c", b"", b"\x1c\x1d\x1f", b"   ", b"\n\t",
                    "\u00a0".encode("utf-8"), "\u2028".encode("utf-8")):
            self.assertEqual(transport._last_token(raw), "", repr(raw))
            self.assertEqual(transport._tokens(raw), [], repr(raw))
        # a real token is still returned
        self.assertEqual(transport._last_token(b"SHA256(x)= deadbeef\n"), "deadbeef")

    def test_huge_unbounded_numeric_marker_is_classified(self):
        # a 5000-digit status: previously `int()` raised ValueError past 4300 digits
        a, c, store = self._attempt("selftest_huge_status", watchdog_selftest_bound=1)
        self.assertEqual(a["outcome"], OUTCOME_PROVISION, a["detail"])
        self.assertIn("missing or ambiguous", a["detail"])
        self.assertNotEqual(a["outcome"], OUTCOME_INTERNAL,
                            "a device reply escaped the classifier: " + a["detail"])
        self.assertEqual(len(store.outcomes()), 1)
        c.close()

    def test_a_good_sample_is_still_accepted_after_a_bad_provision(self):
        # the SAME collector instance must keep working after a contained failure
        class Flaky(ScenarioTransport):
            def start(self):
                if getattr(self, "_fired", False):
                    return super().start()
                self._fired = True
                e = ProvisioningError("provisioning step 'KAT' failed (mock)")
                setattr(e, "remote_dir", "/tmp/rt.flaky")
                setattr(e, "remote_ownership", "none")
                setattr(e, "provision_cleanup_ok", True)
                setattr(e, "provision_cleanup_detail", None)
                raise e

        store = self.store()
        c = Collector(Flaky("success", log=lambda *a: None), store, log=lambda *a: None)
        self.assertEqual(c.collect_once()["outcome"], OUTCOME_PROVISION)
        self.assertEqual(c.collect_once()["outcome"], "success")
        self.assertEqual(store.sample_count(), 1)

    def test_missing_ssh_binary_is_contained_and_recorded(self):
        tr = harness.make_transport(self.tmp, "ok",
                                    # a path that cannot be executed
                                    artifact=None)
        tr.ssh_bin = os.path.join(self.tmp, "definitely-not-a-binary")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_INTERNAL, a["detail"])
        self.assertEqual(store.sample_count(), 0)
        self.assertEqual(len(store.outcomes()), 1)

    def test_missing_artifact_is_contained_and_recorded(self):
        tr = harness.make_transport(self.tmp, "ok")
        os.remove(tr.artifact)          # the preflight now fails before any ssh
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "artifact_mismatch", a["detail"])
        self.assertEqual(len(store.outcomes()), 1)
        self.assertEqual(store.sample_count(), 0)

    def test_wrong_shaped_state_is_quarantined_and_the_attempt_lands(self):
        store = self.store()
        # a state file that is a JSON LIST: previously reached update_state and
        # raised TypeError AFTER the sample had already been saved
        store.state_path.write_text("[1, 2, 3]\n")
        c = Collector(ScenarioTransport("success", log=lambda *a: None), store,
                      log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "success")
        self.assertEqual(len(store.outcomes()), 1)
        quarantined = [p for p in os.listdir(str(store.dir))
                       if p.startswith("state.json.corrupt.")]
        self.assertTrue(quarantined, "the damaged state file was not quarantined")
        self.assertEqual(json.loads(store.state_path.read_text())["consecutive_successes"], 1)

    def test_unparseable_state_is_quarantined(self):
        store = self.store()
        store.state_path.write_text("{ this is not json")
        self.assertIsNone(store.load_state())
        self.assertTrue([p for p in os.listdir(str(store.dir))
                         if p.startswith("state.json.corrupt.")])

    def _break_samples_dir(self, store):
        """Make the samples path unusable: a FILE where a directory must be."""
        shutil.rmtree(str(store.samples_dir))
        with open(str(store.samples_dir), "w") as fh:
            fh.write("not a directory\n")

    def test_store_io_error_becomes_a_classified_storage_error(self):
        store = self.store()
        self._break_samples_dir(store)
        c = Collector(ScenarioTransport("success", log=lambda *a: None), store,
                      log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_STORAGE, a["detail"])
        self.assertEqual(store.sample_count(), 0)

    def test_store_oserror_is_converted_at_the_storage_boundary(self):
        store = self.store()
        self._break_samples_dir(store)
        with self.assertRaises(StorageError):
            store.save_sample({"x": 1})


class TestSchemaContainment(unittest.TestCase):
    def test_huge_key_name_produces_a_bounded_detail(self):
        d = json.loads(load_fixture_bytes().decode())
        d["cpu_stat"]["x" * 200000] = {"user": "no"}
        with self.assertRaises(SchemaInvalid) as cm:
            schema.validate(d)
        self.assertLess(len(str(cm.exception)), 600)

    def test_huge_wrong_typed_value_produces_a_bounded_detail(self):
        d = json.loads(load_fixture_bytes().decode())
        d["host"]["sysname"] = ["S" * 500000]      # wrong type AND huge
        with self.assertRaises(SchemaInvalid) as cm:
            schema.validate(d)
        self.assertLess(len(str(cm.exception)), 600)
        d = json.loads(load_fixture_bytes().decode())
        d["memory_kb"]["MemTotal"] = "9" * 500000  # wrong type AND huge
        with self.assertRaises(SchemaInvalid) as cm:
            schema.validate(d)
        self.assertLess(len(str(cm.exception)), 600)

    def test_duplicate_json_keys_are_refused(self):
        text = '{"schema":"router_health/2","tool":"t","tool":"u"}'
        with self.assertRaises(MalformedData) as cm:
            schema.parse(text)
        self.assertIn("duplicate JSON key", str(cm.exception))

    def test_schema_mismatch_detail_is_bounded(self):
        d = json.loads(load_fixture_bytes().decode())
        d["schema"] = "x" * 200000
        with self.assertRaises(SchemaInvalid) as cm:
            schema.validate(d)
        self.assertLess(len(str(cm.exception)), 600)


# ============================================================ F4: diagnostics
class TestDiagnosticSanitisation(TempCase):
    def test_sanitize_detail_is_ascii_single_line_and_bounded(self):
        raw = b"\x1b[2J\x1b[31mred\x07\n\tline2 \xc3\xa9 \xe2\x80\xa8x"
        out = sanitize_detail(raw)
        self.assertTrue(all(0x20 <= ord(ch) <= 0x7E for ch in out),
                        "control or non-ASCII bytes survived sanitisation: %r" % out)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 500 + 32)

    def test_sanitize_detail_caps_a_very_long_message(self):
        out = sanitize_detail("A" * 100000)
        self.assertLess(len(out), 600)
        self.assertIn("more chars", out)

    def test_sanitize_detail_handles_non_ascii_whitespace(self):
        out = sanitize_detail("\u00a0\u2028")
        self.assertEqual(out, "\\u00a0\\u2028")

    def test_hostile_stderr_is_a_transport_error_with_bounded_clean_detail(self):
        tr = harness.make_transport(self.tmp, "hostile_stderr")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        # The device printed "Host key verification failed" and "Permission denied"
        # on its OWN stderr. It must NOT get to choose those outcome classes: a
        # multiplexed command's failure is a TRANSPORT failure, whatever it says.
        self.assertEqual(a["outcome"], OUTCOME_TRANSPORT, a["detail"])
        self.assertNotEqual(a["outcome"], "ssh_hostkey_mismatch")
        self.assertNotEqual(a["outcome"], "ssh_auth_failure")
        detail = a["detail"]
        self.assertTrue(all(0x20 <= ord(ch) <= 0x7E for ch in detail),
                        "control bytes reached the recorded detail")
        self.assertLess(len(detail), 700)
        # ... and the attempt is still on disk, and it did NOT fill the log
        rec = store.outcomes()[-1]
        self.assertLess(len(json.dumps(rec)), 2000)
        c.close()

    def test_hostile_stderr_does_not_evict_earlier_evidence(self):
        store = self.store(retain_outcomes=1000, retain_outcome_bytes=20000)
        tr = harness.make_transport(self.tmp, "hostile_stderr")
        c = Collector(tr, store, log=lambda *a: None)
        for _ in range(6):
            c.collect_once()
        rows = store.outcomes()
        self.assertEqual(len(rows), 6, "earlier outcome records were pruned away")
        c.close()

    def test_hostkey_and_auth_classes_only_come_from_master_establishment(self):
        # a real host-key mismatch: ssh reports it while ESTABLISHING the master
        tr = harness.make_transport(self.tmp, "hostkey_mismatch")
        with self.assertRaises(TransportHostKeyError):
            tr.start()
        # a real auth refusal: likewise
        tr = harness.make_transport(self.tmp, "authfail")
        with self.assertRaises(TransportAuthError):
            tr.start()
        # a multiplexed 255 must NOT be able to claim either class
        e = transport.classify_transport_failure(
            "Host key verification failed. Permission denied (password).")
        self.assertIsInstance(e, TransportError)
        self.assertNotIsInstance(e, TransportAuthError)
        self.assertNotIsInstance(e, TransportHostKeyError)
        # ... while a genuine network diagnostic still classifies as unreachable
        u = transport.classify_transport_failure(
            "ssh: connect to host 192.0.2.1 port 22: Connection timed out")
        self.assertIsInstance(u, TransportUnreachableError)

    def test_cli_failure_is_a_classified_exit_not_a_crash(self):
        data = os.path.join(self.tmp, "d")
        harness.write_ssh_config(data, self.tmp, "hostile_stderr")
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("UNEXPECTED ERROR", r.stderr)
        self.assertNotIn("\x1b", r.stderr, "a control sequence reached the terminal")
        self.assertLess(len(r.stderr), 12000, "the diagnostic output was unbounded")

    def test_detail_in_state_and_status_is_sanitised(self):
        tr = harness.make_transport(self.tmp, "hostile_stderr")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        c.collect_once()
        st = store.load_state()
        detail = st["last_attempt"]["detail"]
        self.assertTrue(all(0x20 <= ord(ch) <= 0x7E for ch in detail))
        from rtax_telemetry.status import render_status
        # the DEVICE-derived lines must be printable ASCII; the collector's own
        # title uses a literal em dash and is not device-controlled
        device_lines = [ln for ln in render_status(store, st).splitlines()
                        if "detail:" in ln or "remote dir:" in ln or "cleanup:" in ln]
        self.assertTrue(device_lines, "no device-derived line was rendered")
        for ln in device_lines:
            self.assertTrue(all(0x20 <= ord(ch) <= 0x7E for ch in ln), repr(ln))
        c.close()


# ============================================================== F7: contract
class TestReporterContract(TempCase):
    def test_null_cpu_and_loadavg_sections_are_accepted(self):
        c, store = self.collector("null_sections")
        a = c.collect_once()
        self.assertEqual(a["outcome"], "success", a["detail"])
        self.assertEqual(store.sample_count(), 1)
        doc = store.last_sample()["reporter"]
        self.assertIsNone(doc["cpu"])
        self.assertIsNone(doc["loadavg"])

    def test_success_detail_tolerates_null_sections(self):
        c, store = self.collector("null_sections")
        a = c.collect_once()
        self.assertIn("cpu_util_state=", a["detail"])
        self.assertNotIn("load1=", a["detail"])

    def test_absent_section_is_still_an_error(self):
        d = json.loads(load_fixture_bytes().decode())
        del d["cpu"]
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)
        d = json.loads(load_fixture_bytes().decode())
        del d["loadavg"]
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_null_is_not_accepted_where_an_object_with_fields_is_required(self):
        d = json.loads(load_fixture_bytes().decode())
        d["cpu"] = "four"
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)
        d = json.loads(load_fixture_bytes().decode())
        d["loadavg"] = []
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_every_reporter_fixture_validates_through_the_cli(self):
        """The reporter-to-collector contract, exercised on REAL reporter bytes."""
        fixtures = sorted(p for p in os.listdir(os.path.join(HERE, "fixtures"))
                          if p.endswith(".json"))
        self.assertIn("live_sample.json", fixtures)
        for name in fixtures:
            path = os.path.join(HERE, "fixtures", name)
            r = harness.run_cli(["validate", path])
            self.assertEqual(r.returncode, 0,
                             "collector.py validate rejected %s: %s" % (name, r.stdout + r.stderr))

    def test_null_sections_document_validates_through_the_cli(self):
        # the same contract, on a real reporter document generated with the
        # /proc/cpuinfo and /proc/loadavg files MISSING
        path = os.path.join(HERE, "fixtures", "null_sections.json")
        if not os.path.isfile(path):
            self.skipTest("null_sections.json fixture is not present")
        r = harness.run_cli(["validate", path])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ========================================================= F1: master liveness
class TestMasterLiveness(TempCase):
    def _lost_master_transport(self, **env):
        """A transport whose master goes away once the named file exists."""
        down = os.path.join(self.tmp, "master.down")
        extra = {"FAKE_SSH_MASTER_STATE": down, "FAKE_SSH_MASTER_PID": "4242"}
        extra.update(env)
        return harness.make_transport(self.tmp, "ok", extra_env=extra), down

    def test_lost_master_is_detected_before_any_command_runs(self):
        tr, down = self._lost_master_transport()
        session = tr.start()                       # provisioning completes
        self.assertEqual(tr._master_pid, 4242)
        with open(down, "w"):                      # the master goes away
            pass
        t0 = time.monotonic()
        with self.assertRaises(ControlMasterLost) as cm:
            tr.collect_sample(session)
        self.assertLess(time.monotonic() - t0, 6.0,
                        "a lost master must fail fast, not stall to a deadline")
        self.assertIn("control master has gone away", str(cm.exception))
        self.assertFalse(tr._master_up)
        tr.close()

    def test_lost_master_is_a_dedicated_recorded_outcome(self):
        tr, down = self._lost_master_transport()
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        # the FIRST attempt provisions and succeeds; the master then goes away
        first = c.collect_once()
        self.assertEqual(first["outcome"], "success", first["detail"])
        with open(down, "w"):
            pass
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_MASTER_LOST, a["detail"])
        # the session from the successful attempt still OWNS a remote directory,
        # and it can no longer be removed: that must be recorded, not assumed clean
        self.assertEqual(a["remote_ownership"], "owned")
        self.assertTrue(a["remote_dir"].startswith("/tmp/rt."))
        c.close()
        self.assertIsNotNone(c.cleanup_error)
        self.assertEqual(len(store.outcomes()), 3)          # 2 attempts + teardown
        self.assertEqual(store.outcomes()[-1]["record"], "teardown")
        self.assertEqual([r["remote_dir"] for r in store.pending_cleanup()],
                         [a["remote_dir"]])

    def test_multiplexed_clients_carry_batchmode_and_no_master(self):
        tr = harness.make_transport(self.tmp, "ok")
        seen = []
        orig = transport.run_bounded

        def spy(argv, timeout, stdin_bytes=None, capture=True, **kwargs):
            seen.append(list(argv))
            return orig(argv, timeout, stdin_bytes, capture, **kwargs)

        transport.run_bounded = spy
        try:
            session = tr.start()
            tr.collect_sample(session)
            tr.stop(session)
        finally:
            transport.run_bounded = orig
        # every invocation that is NOT the master itself must forbid a fresh
        # connection and forbid prompting
        multiplexed = [a for a in seen if a and a[-1] not in ("-N",) and "@" not in a[-1]]
        self.assertTrue(multiplexed, "no multiplexed command was observed")
        for argv in multiplexed:
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("ControlMaster=no", argv)

    def test_master_liveness_is_checked_before_each_command(self):
        tr = harness.make_transport(self.tmp, "ok")
        session = tr.start()
        checks = []
        orig = tr._master_check

        def spy():
            checks.append(time.monotonic())
            return orig()

        tr._master_check = spy
        try:
            tr.collect_sample(session)
        finally:
            tr._master_check = orig
        self.assertTrue(checks, "no liveness check ran before the command")
        tr.stop(session)

    def test_require_master_refuses_without_invoking_ssh(self):
        tr = harness.make_transport(self.tmp, "ok")
        session = tr.start()
        tr.stop(session)                       # master is now down and released
        called = []
        orig = transport.run_bounded
        transport.run_bounded = lambda *a, **k: called.append(a) or (0, b"", b"")
        try:
            with self.assertRaises(ControlMasterLost):
                tr._rsh("true", 5)
        finally:
            transport.run_bounded = orig
        self.assertEqual(called, [], "ssh was invoked despite no live master")


class TestIntervalValidation(unittest.TestCase):
    def test_rejects_non_finite_and_zero(self):
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(TransportError):
                cli.validate_interval(bad, 900)

    def test_rejects_interval_at_or_above_control_persist(self):
        with self.assertRaises(TransportError) as cm:
            cli.validate_interval(900, 900)
        self.assertIn("ControlPersist", str(cm.exception))
        with self.assertRaises(TransportError):
            cli.validate_interval(1200, 900)

    def test_accepts_a_sane_interval(self):
        self.assertEqual(cli.validate_interval(30, 900), 30)


# ================================================== F2: ownership and cleanup
class TestOwnershipEvidence(TempCase):
    def test_failed_provision_carries_ownership_and_cleanup_consequence(self):
        tr = harness.make_transport(self.tmp, "unreachable")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "unreachable")
        # provisioning failed before mkdir, so nothing was created ...
        self.assertEqual(a["remote_dir"], None)
        self.assertEqual(a["remote_ownership"], "none")
        self.assertTrue(a["cleanup_ok"])
        self.assertEqual(store.pending_cleanup(), [])

    def test_lost_mkdir_reply_is_unknown_and_never_deleted(self):
        tr = harness.make_transport(self.tmp, "timeout")   # not used; we patch below
        session = tr.start()
        # simulate a mkdir whose reply never arrived: ownership unknown
        session.ownership = "unknown"
        with self.assertRaises(CleanupError) as cm:
            tr._dispose(session)
        self.assertIn("never acknowledged", str(cm.exception))
        self.assertIn("NOT removing it automatically", str(cm.exception))
        tr.close()

    def test_cleanup_without_the_removed_marker_is_unconfirmed(self):
        tr = harness.make_transport(self.tmp, "cleanup_no_marker")
        session = tr.start()
        self.assertEqual(session.ownership, "owned")
        with self.assertRaises(CleanupError) as cm:
            tr.stop(session)
        self.assertIn("REMOVED marker", str(cm.exception))

    def test_provision_failure_mock_records_pending_cleanup(self):
        c, store = self.collector("provision_failure")
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_PROVISION)
        self.assertEqual(a["remote_dir"], "/tmp/rt.mockprov")
        self.assertEqual(a["remote_ownership"], "unknown")
        self.assertFalse(a["cleanup_ok"], "a provisioning failure still claimed cleanup_ok")
        self.assertIn("never", a["cleanup_detail"])
        pending = store.pending_cleanup()
        self.assertEqual(len(pending), 1, "no pending-cleanup evidence was written")
        self.assertEqual(pending[0]["remote_dir"], "/tmp/rt.mockprov")
        self.assertTrue(pending[0]["manual_reconciliation_required"])
        self.assertFalse(pending[0]["automatic_deletion"])

    def test_teardown_failure_is_appended_to_the_outcome_log(self):
        tr = harness.make_transport(self.tmp, "cleanup_fail")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "success")
        c.close()
        rows = store.outcomes()
        self.assertEqual(len(rows), 2, "no teardown record was appended")
        teardown = rows[-1]
        self.assertEqual(teardown["record"], "teardown")
        self.assertEqual(teardown["outcome"], "cleanup_failure")
        self.assertTrue(teardown["remote_dir"].startswith("/tmp/rt."))
        self.assertEqual(teardown["remote_ownership"], "owned")

    def test_teardown_evidence_survives_a_later_overwriting_attempt(self):
        tr = harness.make_transport(self.tmp, "cleanup_fail")
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        c.collect_once()
        c.close()
        # a later, HEALTHY attempt overwrites last_attempt ...
        c2 = Collector(ScenarioTransport("success", log=lambda *a: None), store,
                       log=lambda *a: None)
        c2.collect_once()
        st = store.load_state()
        self.assertTrue(st["last_attempt"]["cleanup_ok"], "the new attempt should be clean")
        # ... but the append-only history still names the unconfirmed directory
        rows = store.outcomes()
        self.assertTrue(any(r.get("record") == "teardown" and not r["cleanup_ok"]
                            for r in rows))
        self.assertEqual(len(store.pending_cleanup()), 1)

    def test_cleanup_failure_is_reflected_in_the_exit_status(self):
        data = os.path.join(self.tmp, "d")
        harness.write_ssh_config(data, self.tmp, "cleanup_fail")
        r = harness.run_cli(["collect", "--once", "--data-dir", data])
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("SEPARATE FAILURE", r.stderr)

    @staticmethod
    def _block_outcome_log(data_dir):
        """Make the outcomes log unwritable by putting a DIRECTORY in its place."""
        path = os.path.join(data_dir, "outcomes.jsonl")
        if os.path.isdir(path):
            return
        if os.path.exists(path):
            os.remove(path)
        os.mkdir(path)

    def test_evidence_write_failure_is_reported_not_assumed(self):
        store = self.store()
        self._block_outcome_log(str(store.dir))
        c = Collector(ScenarioTransport("success", log=lambda *a: None), store,
                      log=lambda *a: None)
        a = c.collect_once()
        # the sample itself was saved (samples/ is a separate directory) ...
        self.assertEqual(a["outcome"], "success", a["detail"])
        # ... but the record could NOT be written, and that is REPORTED
        self.assertIsNotNone(c.persistence_error,
                             "a lost record was not reported as a persistence failure")
        self.assertIn("outcome record not persisted", c.persistence_error)

    def test_cli_reports_a_lost_record_with_exit_6(self):
        data = os.path.join(self.tmp, "blocked")
        harness.write_ssh_config(data, self.tmp, "ok")
        self._block_outcome_log(data)
        r = harness.run_cli(["collect", "--once", "--transport", "mock",
                             "--data-dir", data])
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertIn("EVIDENCE NOT PERSISTED", r.stderr)


# ======================================================== F5: lifecycle
class TestSignalLifecycle(TempCase):
    def test_sighup_during_once_mode_is_orderly(self):
        """SIGHUP reaches teardown in --once mode, releases the lock, and the
        control-socket directory is gone."""
        data = os.path.join(self.tmp, "d")
        harness.write_ssh_config(data, self.tmp, "slow_reporter")
        before = {d for d in os.listdir(tempfile.gettempdir())
                  if d.startswith("rt-ax56u-cm.")}
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "collector.py"), "collect", "--once",
             "--data-dir", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(4.0)
        self.assertIsNone(proc.poll(), "the collector finished before the signal")
        proc.send_signal(signal.SIGHUP)
        out, err = proc.communicate(timeout=40)
        self.assertEqual(proc.returncode, 130, err)
        self.assertIn("interrupted", err)
        after = {d for d in os.listdir(tempfile.gettempdir())
                 if d.startswith("rt-ax56u-cm.")}
        self.assertEqual(after - before, set(), "control-socket dir leaked after SIGHUP")
        # the data-directory lock was released
        r = harness.run_cli(["status", "--transport", "mock", "--data-dir", data])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sigterm_during_loop_is_orderly(self):
        data = os.path.join(self.tmp, "d2")
        harness.write_ssh_config(data, self.tmp, "ok")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "collector.py"), "collect", "--loop",
             "--interval", "30", "--data-dir", data],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(6.0)
        self.assertIsNone(proc.poll(), "the loop exited before the signal")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=40)
        self.assertEqual(proc.returncode, 130, err)
        self.assertIn("interrupted", err)

    def test_signal_handler_routes_to_keyboard_interrupt(self):
        installed = cli.install_stop_handlers()
        try:
            self.assertTrue(installed, "no stop handlers were installed")
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
        finally:
            cli.restore_stop_handlers(installed)

    def test_sigint_during_master_handover_is_an_interruption(self):
        tr = harness.make_transport(self.tmp, "ok")
        orig = transport.run_bounded

        def fake(argv, timeout, **kwargs):
            return -signal.SIGINT, b"", b""

        transport.run_bounded = fake
        try:
            with self.assertRaises(KeyboardInterrupt):
                tr.start()
        finally:
            transport.run_bounded = orig
        self.assertFalse(tr._master_up)
        self.assertIsNone(tr._ctrl_dir)

    def test_master_shutdown_is_verified(self):
        log = os.path.join(self.tmp, "exitlog")
        down = os.path.join(self.tmp, "gone")
        tr = harness.make_transport(self.tmp, "ok",
                                    extra_env={"FAKE_SSH_EXIT_LOG": log,
                                               "FAKE_SSH_MASTER_STATE": down})
        tr.start()
        result = tr._exit_master()
        self.assertIs(result, True, "a completed -O exit was not verified as gone")
        self.assertTrue(os.path.isfile(log), "-O exit was never sent")
        tr.close()

    def test_master_shutdown_is_not_inferred_from_absence_of_error(self):
        # `-O exit` returns 0, but the master keeps answering: NOT confirmed
        down = os.path.join(self.tmp, "never-gone")
        tr = harness.make_transport(self.tmp, "master_wont_exit",
                                    extra_env={"FAKE_SSH_MASTER_STATE": down})
        tr.start()
        messages = []
        tr.log = messages.append
        result = tr._exit_master()
        self.assertIs(result, False,
                      "a master that still answers was reported as shut down")
        self.assertTrue(any("still answered" in m for m in messages), messages)
        tr.close()

    def test_master_wont_exit_kills_only_the_reported_pid(self):
        down = os.path.join(self.tmp, "never-gone")
        tr = harness.make_transport(self.tmp, "master_wont_exit",
                                    extra_env={"FAKE_SSH_MASTER_PID": "1",
                                               "FAKE_SSH_MASTER_STATE": down})
        tr.start()
        tr._master_pid = 1
        killed = []
        orig = os.kill

        def spy(pid, sig):
            killed.append((pid, sig))
            raise ProcessLookupError

        os.kill = spy
        try:
            tr._kill_master_pid()
        finally:
            os.kill = orig
        self.assertTrue(killed)
        self.assertEqual({pid for pid, _ in killed}, {1},
                         "something other than the reported master pid was signalled")
        tr.close()


# ============================================================ F6: identity
class TestBootIdentity(TempCase):
    def test_explicit_boot_identity_detects_a_reboot_uptime_would_miss(self):
        c, store = self.collector("boot_change")
        c.collect_once()
        util = c.collect_once()
        last = store.last_sample()
        self.assertEqual(last["derived"]["cpu_utilisation"]["state"], "reboot")

    def test_uptime_fallback_still_detects_a_reboot(self):
        c, store = self.collector("reboot_legacy")
        c.collect_once()
        c.collect_once()
        self.assertEqual(store.last_sample()["derived"]["cpu_utilisation"]["state"], "reboot")

    def test_same_boot_is_not_a_reboot(self):
        c, store = self.collector("success")
        c.collect_once()
        c.collect_once()
        self.assertEqual(store.last_sample()["derived"]["cpu_utilisation"]["state"], "ok")

    def test_detect_reboot_prefers_the_boot_identity(self):
        a = {"host": {"uptime_seconds": 100.0, "boot_time": 1000.0}}
        b = {"host": {"uptime_seconds": 900.0, "boot_time": 1000.0}}
        self.assertFalse(cpu.detect_reboot(a, b))       # uptime rose, same boot
        c = {"host": {"uptime_seconds": 900.0, "boot_time": 2000.0}}
        self.assertTrue(cpu.detect_reboot(a, c))        # uptime rose, NEW boot

    def test_detect_reboot_wall_clock_fallback(self):
        a = {"host": {"uptime_seconds": 10.0}}
        b = {"host": {"uptime_seconds": 100.0}}
        # boot instant estimate moved by 5000s between the two samples
        self.assertTrue(cpu.detect_reboot(a, b, prev_at=1_000_000.0, cur_at=1_005_090.0))
        self.assertFalse(cpu.detect_reboot(a, b, prev_at=1_000_000.0, cur_at=1_000_090.0))

    def test_predecessor_comes_from_state_not_filename_order(self):
        c, store = self.collector("success")
        c.collect_once()
        first = store.last_sample()
        # a FUTURE-dated file appears in the samples directory: filename order would
        # pick it, the state pointer must not
        future = store.samples_dir / "29991231T235959.000000Z.json"
        future.write_text(json.dumps({"nonsense": True}))
        c.collect_once()
        second = store.last_sample()
        self.assertEqual(second["derived"]["cpu_utilisation"]["state"], "ok",
                         "the wrong (future-dated) predecessor was chosen")

    def test_predecessor_with_a_different_source_is_a_first_sample(self):
        c, store = self.collector("success")
        c.collect_once()
        # rewrite the saved sample's source as if it came from another device
        path = store.last_sample_path()
        doc = json.loads(path.read_text())
        doc["source"] = {"transport": "ssh", "host": "someone@elsewhere"}
        path.write_text(json.dumps(doc))
        c.collect_once()
        util = store.last_sample()["derived"]["cpu_utilisation"]
        self.assertEqual(util["state"], "first")
        self.assertIsNone(util["aggregate"])

    def test_missing_predecessor_file_is_a_first_sample(self):
        c, store = self.collector("success")
        c.collect_once()
        os.remove(store.last_sample_path())
        c.collect_once()
        self.assertEqual(
            store.last_sample()["derived"]["cpu_utilisation"]["state"], "first")

    def test_idle_iowait_reclassification_is_not_a_reset(self):
        prev = {"user": 100, "nice": 0, "system": 50, "idle": 800,
                "iowait": 50, "irq": 0, "softirq": 0, "steal": 0}
        # the kernel moved 40 jiffies from idle to iowait: NOT a counter reset
        cur = {"user": 200, "nice": 0, "system": 100, "idle": 1760,
               "iowait": 90, "irq": 0, "softirq": 0, "steal": 0}
        out = cpu._pair(prev, cur)
        self.assertNotEqual(out["state"], "reset")

    def test_a_genuine_reset_is_still_detected(self):
        prev = {"user": 500, "nice": 0, "system": 500, "idle": 500,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur = {"user": 5, "nice": 0, "system": 5, "idle": 5,
               "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        self.assertEqual(cpu._pair(prev, cur)["state"], "reset")


# ================================================== hard stops and loop rules
class TestHardStop(TempCase):
    def test_hostkey_and_artifact_are_hard_stops(self):
        self.assertTrue(is_hard_stop("ssh_hostkey_mismatch"))
        self.assertTrue(is_hard_stop("artifact_mismatch"))
        for soft in ("timeout", "unreachable", "transport_error", "control_master_lost",
                     "provisioning_failure", "success"):
            self.assertFalse(is_hard_stop(soft))

    def test_loop_stops_after_a_host_key_mismatch(self):
        from rtax_telemetry.cli import run_loop

        class HostKeyThenSuccess(ScenarioTransport):
            def __init__(self):
                super().__init__("success", log=lambda *a: None)
                self.calls = 0

            def start(self):
                self.calls += 1
                if self.calls == 1:
                    raise TransportHostKeyError("pinned host key did not match (test)")
                return super().start()

        store = self.store()
        c = Collector(HostKeyThenSuccess(), store, log=lambda *a: None)
        rc = run_loop(c, store, 0.1)
        self.assertEqual(rc, 1, "the loop did not stop on a hard stop")
        self.assertEqual(len(store.outcomes()), 1,
                         "the loop kept going after a host-key mismatch")


# ================================================== F8/F9 storage and ssh
class TestStorageHardening(TempCase):
    def test_evidence_files_and_directories_have_restrictive_modes(self):
        store = self.store()
        store.save_sample({"a": 1})
        store.append_outcome({"outcome": "success"})
        store.write_state(default_state())
        self.assertEqual(os.stat(str(store.dir)).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(str(store.samples_dir)).st_mode & 0o777, 0o700)
        for p in [store.state_path, store.outcomes_path] + store.sample_paths():
            self.assertEqual(os.stat(str(p)).st_mode & 0o777, 0o600, str(p))

    def test_a_torn_trailing_line_is_repaired_before_appending(self):
        store = self.store()
        store.append_outcome({"i": 1})
        with open(store.outcomes_path, "a") as fh:
            fh.write('{"i": 2, "tra')          # torn write: no newline
        store.append_outcome({"i": 3})
        rows = store.outcomes()
        # the TORN line itself is unparseable and is skipped, but the record that
        # followed it must NOT have been swallowed by concatenation
        self.assertEqual([r["i"] for r in rows if "i" in r and r["i"] in (1, 3)], [1, 3])
        self.assertEqual(rows[-1]["i"], 3, "the record after the torn line was lost")
        with open(store.outcomes_path, "rb") as fh:
            self.assertTrue(fh.read().endswith(b"\n"))

    def test_a_symlinked_lock_is_refused(self):
        store = self.store()
        target = os.path.join(self.tmp, "victim")
        with open(target, "w") as fh:
            fh.write("do not truncate me\n")
        os.symlink(target, str(store.lock_path))
        with self.assertRaises(cli.LockHeld):
            cli.acquire_lock(store)
        with open(target) as fh:
            self.assertEqual(fh.read(), "do not truncate me\n",
                             "the symlink target was truncated through the lock")

    def test_the_lock_is_released_and_reacquirable(self):
        store = self.store()
        fd = cli.acquire_lock(store)
        cli.release_lock(fd)
        fd2 = cli.acquire_lock(store)
        cli.release_lock(fd2)


class TestSshIsolation(unittest.TestCase):
    def _opts(self):
        return transport.SSHTransport("192.0.2.1", "u", known_hosts="/tmp/kh")._ssh_opts()

    def test_options_are_hermetic(self):
        opts = self._opts()
        joined = " ".join(opts)
        self.assertIn("-F /dev/null", joined)
        self.assertIn("GlobalKnownHostsFile=/dev/null", joined)
        self.assertIn("ForwardAgent=no", joined)
        self.assertIn("ClearAllForwardings=yes", joined)
        self.assertIn("ProxyCommand=none", joined)
        self.assertIn("KnownHostsCommand=none", joined)
        self.assertIn("UpdateHostKeys=no", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("UserKnownHostsFile=/tmp/kh", joined)
        self.assertIn("PreferredAuthentications=password", joined)
        self.assertIn("PubkeyAuthentication=no", joined)

    def test_askpass_variables_are_scrubbed_from_the_child_environment(self):
        old = {k: os.environ.get(k) for k in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")}
        os.environ["SSH_ASKPASS"] = "/tmp/leak"
        os.environ["SSH_ASKPASS_REQUIRE"] = "force"
        try:
            env = transport._child_env()
            self.assertNotIn("SSH_ASKPASS", env)
            self.assertNotIn("SSH_ASKPASS_REQUIRE", env)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestClosedStderr(TempCase):
    def test_log_never_raises_on_a_broken_stderr(self):
        import io

        real = sys.stderr
        sys.stderr = io.StringIO()
        sys.stderr.close()
        try:
            cli.log("this must not raise")           # closed stream -> ValueError
        finally:
            sys.stderr = real
        self.assertTrue(cli.stderr_is_broken())

    def test_a_broken_pipe_is_not_reported_as_success(self):
        source = open(os.path.join(ROOT, "rtax_telemetry", "cli.py")).read()
        self.assertNotIn("except BrokenPipeError:\n        return 0", source,
                         "a closed output pipe is still mapped to exit 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
