#!/usr/bin/env python3
"""Regression tests for the code/security remediation — offline.

Each test reproduces a specific defect that was found in review and verifies the
fix, driving the REAL mechanism where the defect lived (the real remote command
text under a local shell, real child processes, real files, real locks):

  * provisioning gates        — failed / missing / malformed tool-probe results,
                                and a watchdog proof that must reject an unrelated
                                crash or a status-137 without timer evidence;
  * output containment        — independent stdout/stderr overflow with bounded
                                allocation and no leaked descendants, classified
                                as a collection failure;
  * parser containment        — deeply nested input classified as malformed, then
                                a good sample accepted; non-finite / boolean /
                                out-of-range numeric telemetry rejected;
  * storage locking           — explicit prune refuses while collection holds the
                                lock;
  * storage budgets           — byte budgets and the per-write quota.

No network I/O, no router contact.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import harness                                                            # noqa: E402
from rtax_telemetry import cpu, schema, transport                        # noqa: E402
from rtax_telemetry.cli import acquire_lock, release_lock                 # noqa: E402
from rtax_telemetry.core import Collector                                 # noqa: E402
from rtax_telemetry.errors import (                                       # noqa: E402
    MalformedData, OutputLimitExceeded, QuotaExceeded, SchemaInvalid, TransportError,
)
from rtax_telemetry.store import Store                                    # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "live_sample.json")
def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


def load_fixture_bytes():
    with open(FIXTURE, "rb") as fh:
        return fh.read()

DEEP = b'{"extra":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'


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
        self.tmp = tempfile.mkdtemp(prefix="rtax-fix.")
        transport.REAP_GRACE = 0.5

    def tearDown(self):
        transport.REAP_GRACE = 3.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def store(self, name="data", **kw):
        s = Store(os.path.join(self.tmp, name), **kw)
        s.ensure()
        return s


class ScriptedTransport:
    """Minimal transport that replays a fixed list of raw responses."""

    name = "scripted"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.n = 0
        self.started = 0

    def start(self):
        self.started += 1
        return object()

    def collect_sample(self, session, timeout=None):
        p = self.payloads[min(self.n, len(self.payloads) - 1)]
        self.n += 1
        return p

    def stop(self, session):
        pass


# ============================================================ provisioning gates
class TestProvisioningGates(TempCase):
    def test_tools_ok_proceeds(self):
        tr = harness.make_transport(self.tmp, "ok")
        session = tr.start()
        self.assertTrue(session.provisioned)
        tr.stop(session)

    def test_tool_probe_nonzero_is_hard_stop(self):
        tr = harness.make_transport(self.tmp, "tools_fail")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("required-tool probe failed", str(cm.exception))
        self.assertFalse(tr._master_up)
        self.assertIsNone(tr._ctrl_dir)

    def test_tool_probe_missing_tool_is_hard_stop(self):
        tr = harness.make_transport(self.tmp, "tools_missing")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("MISSING", str(cm.exception))

    def test_tool_probe_malformed_marker_is_hard_stop(self):
        tr = harness.make_transport(self.tmp, "tools_malformed")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("unrecognised", str(cm.exception))

    def test_gate_stops_before_mkdir_transfer_or_execution(self):
        tr = harness.make_transport(self.tmp, "tools_missing")
        seen = []
        real = tr._rsh

        def spy(cmd, timeout, stdin_bytes=None):
            seen.append(cmd)
            return real(cmd, timeout, stdin_bytes)

        tr._rsh = spy
        with self.assertRaises(TransportError):
            tr.start()
        joined = "\n".join(seen)
        self.assertNotIn("mkdir -m 700", joined, "remote dir created despite a failed tool gate")
        self.assertNotIn("cat > ", joined, "artifact transferred despite a failed tool gate")
        self.assertNotIn("busybox", joined, "control exec ran despite a failed tool gate")


class TestWatchdogProof(TempCase):
    def test_genuine_timer_termination_is_accepted(self):
        tr = harness.make_transport(self.tmp, "ok")
        session = tr.start()
        self.assertTrue(session.provisioned)
        tr.stop(session)

    def test_unbounded_job_is_rejected(self):
        tr = harness.make_transport(self.tmp, "selftest_fail")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("watchdog self-test FAILED", str(cm.exception))

    def test_unrelated_crash_status_139_is_rejected(self):
        # a bare "status >= 128" would accept SIGSEGV; the fix requires the
        # watchdog's own SIGKILL status
        tr = harness.make_transport(self.tmp, "selftest_crash")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("unexpected termination status", str(cm.exception))

    def test_status_137_without_timer_evidence_is_rejected(self):
        # a printed 137 (or a success message) must NOT be taken as proof the timer
        # fired: the marker file is required too
        tr = harness.make_transport(self.tmp, "selftest_nofired")
        with self.assertRaises(TransportError) as cm:
            tr.start()
        self.assertIn("no evidence the watchdog timer fired", str(cm.exception))

    def test_real_selftest_reports_the_timer_fired(self):
        rd = harness.remote_dir(self.tmp)
        script = transport.selftest_cmd(rd, 2)
        t0 = time.monotonic()
        p = harness.run_local_script(script, self.tmp, timeout=20)
        dt = time.monotonic() - t0
        self.assertIn("selftest_exit=137", p.stdout)
        self.assertIn("selftest_wd_fired=1", p.stdout)
        self.assertTrue(os.path.exists(os.path.join(rd, "wd.fired")),
                        "the watchdog did not leave its timer-fired marker")
        self.assertGreaterEqual(dt, 1.5)
        self.assertLess(dt, 8.0)


# ============================================================== output limits
class TestOutputLimits(TempCase):
    def _flood(self, *, stream, size, descendant=False):
        pidfile = os.path.join(self.tmp, "desc.pid")
        helper = os.path.join(self.tmp, "flood.sh")
        body = ["#!/bin/sh"]
        if descendant:
            # a descendant that IGNORES SIGTERM, to prove the GROUP is killed
            body.append("sh -c 'trap \"\" TERM; exec sleep 30' &")
            body.append('echo $! > "%s"' % pidfile)
        body.append('python3 -c "import sys; sys.%s.buffer.write(b\'x\'*%d)"'
                    % ("stderr" if stream == "stderr" else "stdout", size))
        with open(helper, "w") as fh:
            fh.write("\n".join(body) + "\n")
        os.chmod(helper, 0o755)
        return helper, pidfile

    def test_stdout_overflow_is_bounded_and_raises(self):
        helper, _ = self._flood(stream="stdout", size=8 * 1024 * 1024)
        t0 = time.monotonic()
        with self.assertRaises(OutputLimitExceeded) as cm:
            transport.run_bounded(["sh", helper], 20, max_stdout=256 * 1024, max_stderr=4096)
        self.assertLess(time.monotonic() - t0, 6.0,
                        "overflow was not detected early (unbounded buffering)")
        self.assertIn("stdout", str(cm.exception))

    def test_stderr_overflow_raises_independently(self):
        helper, _ = self._flood(stream="stderr", size=4 * 1024 * 1024)
        with self.assertRaises(OutputLimitExceeded) as cm:
            transport.run_bounded(["sh", helper], 20, max_stdout=1024 * 1024, max_stderr=4096)
        self.assertIn("stderr", str(cm.exception))

    def test_overflow_reaps_the_whole_child_group(self):
        helper, pidfile = self._flood(stream="stdout", size=8 * 1024 * 1024, descendant=True)
        with self.assertRaises(OutputLimitExceeded):
            transport.run_bounded(["sh", helper], 20, max_stdout=256 * 1024, max_stderr=4096)
        self.assertTrue(os.path.exists(pidfile), "descendant never recorded its pid")
        with open(pidfile) as fh:
            pid = int(fh.read().strip())
        self.assertTrue(wait_gone(pid, 6.0),
                        "descendant survived the output-limit cleanup")

    def test_output_under_the_limit_is_returned_intact(self):
        rc, out, err = transport.run_bounded(
            ["sh", "-c", "printf 'hello'; printf 'oops' 1>&2"], 5,
            max_stdout=1024, max_stderr=1024)
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"hello")
        self.assertEqual(err, b"oops")

    def test_overflow_becomes_a_classified_collection_failure(self):
        class FloodTransport:
            name = "flood"

            def start(self):
                return object()

            def collect_sample(self, session, timeout=None):
                raise OutputLimitExceeded("device stdout exceeded the 1024-byte limit")

            def stop(self, session):
                pass

        c = Collector(FloodTransport(), self.store(), log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "output_overflow")
        self.assertEqual(self.store().sample_count(), 0)


# ============================================================== parser limits
class TestParserContainment(unittest.TestCase):
    def test_deeply_nested_document_is_malformed(self):
        with self.assertRaises(MalformedData):
            schema.parse(DEEP)

    def test_braces_inside_strings_do_not_count(self):
        doc = '{"note":"' + "[" * 500 + '"}'
        self.assertEqual(schema.parse(doc)["note"], "[" * 500)

    def test_oversized_document_is_rejected_before_parsing(self):
        big = b'{"pad":"' + b"x" * 5000 + b'"}'
        with self.assertRaises(MalformedData):
            schema.parse(big, max_bytes=1000)

    def test_nan_and_infinity_are_rejected(self):
        good = load_fixture()
        for const in ("NaN", "Infinity", "-Infinity"):
            text = json.dumps(good).replace("2.15", const, 1)
            with self.assertRaises(MalformedData):
                schema.parse(text)

    def test_boolean_as_a_number_is_rejected(self):
        d = load_fixture()
        d["loadavg"]["load1"] = True
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_negative_and_out_of_range_values_are_rejected(self):
        for mutate in (
            lambda d: d["host"].__setitem__("uptime_seconds", -1.0),
            lambda d: d["memory_kb"].__setitem__("MemTotal", -5),
            lambda d: d["cpu_stat"]["cpu"].__setitem__("user", -1),
            lambda d: d["filesystems"][0].__setitem__("used_percent", 101),
        ):
            d = load_fixture()
            mutate(d)
            with self.assertRaises(SchemaInvalid):
                schema.validate(d)

    def test_truncated_cpu_stat_marker_is_accepted_and_represented(self):
        d = load_fixture()
        agg = d["cpu_stat"]["cpu"]
        d["cpu_stat"] = {"_truncated": True, "cpu": agg}
        schema.validate(d)
        out = cpu.compare(None, d["cpu_stat"])
        self.assertTrue(out["truncated"])

    def test_truncated_marker_must_be_true(self):
        d = load_fixture()
        d["cpu_stat"]["_truncated"] = 1
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)


class TestDeepInputThenGoodSample(TempCase):
    def test_malformed_then_good_sample_is_accepted(self):
        good = load_fixture_bytes()
        tr = ScriptedTransport([DEEP, good])
        store = self.store()
        c = Collector(tr, store, log=lambda *a: None)
        a1 = c.collect_once()
        self.assertEqual(a1["outcome"], "malformed_data")
        a2 = c.collect_once()
        self.assertEqual(a2["outcome"], "success")
        self.assertEqual(store.sample_count(), 1)

    def test_cli_once_on_nested_input_is_a_classified_failure_not_a_crash(self):
        data = os.path.join(self.tmp, "d")
        r = harness.run_cli(["collect", "--once", "--transport", "mock",
                             "--mock-scenario", "nested", "--data-dir", data])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("outcome=malformed_data", r.stderr)
        self.assertNotIn("UNEXPECTED ERROR", r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(data, "outcomes.jsonl")),
                        "the failing attempt was not recorded")


# =============================================================== prune locking
class TestPruneLocking(TempCase):
    def test_prune_refuses_while_collection_holds_the_lock(self):
        store = self.store(retain_samples=5)
        store.save_sample({"proof": "owned lock"}, when=1000)
        held = acquire_lock(store)
        try:
            r = harness.run_cli(["prune", "--data-dir", str(store.dir), "--retain-samples", "0"])
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("another collector run", r.stderr)
            self.assertEqual(store.sample_count(), 1,
                             "prune deleted a sample while the collection lock was held")
        finally:
            release_lock(held)

    def test_prune_succeeds_after_the_lock_is_released(self):
        store = self.store(retain_samples=5)
        store.save_sample({"proof": "released lock"}, when=1000)
        held = acquire_lock(store)
        release_lock(held)
        r = harness.run_cli(["prune", "--data-dir", str(store.dir), "--retain-samples", "0"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(store.sample_count(), 0)


# ============================================================== storage budget
class TestStorageBudgets(TempCase):
    def test_sample_over_the_write_budget_raises_quota(self):
        store = self.store(max_sample_bytes=50)
        with self.assertRaises(QuotaExceeded):
            store.save_sample({"pad": "x" * 200})

    def test_sample_byte_budget_prunes_the_oldest(self):
        store = self.store(retain_samples=1000, retain_sample_bytes=600)
        for i in range(20):
            store.save_sample({"i": i, "pad": "x" * 90})
        total = sum(p.stat().st_size for p in store.sample_paths())
        self.assertLessEqual(total, 600)
        self.assertLess(store.sample_count(), 20)

    def test_outcome_byte_budget_prunes_the_oldest(self):
        store = self.store(retain_outcomes=1000, retain_outcome_bytes=400)
        for i in range(50):
            store.append_outcome({"i": i, "pad": "x" * 40})
        rows = store.outcomes()
        self.assertLess(len(rows), 50)
        self.assertEqual(rows[-1]["i"], 49)

    def test_quota_failure_is_a_classified_collection_failure(self):
        good = load_fixture_bytes()
        tr = ScriptedTransport([good])
        store = self.store(max_sample_bytes=10)
        c = Collector(tr, store, log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], "storage_error")
        self.assertEqual(store.sample_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
