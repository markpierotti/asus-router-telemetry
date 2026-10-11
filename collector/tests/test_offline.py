#!/usr/bin/env python3
"""Offline validation suite for rtax_telemetry.

Everything here runs against SAVED SAMPLE FIXTURES and a MOCKED transport. No
network call is made and the router is never contacted.

    python3 -m unittest discover -s tests -v

Covers: success, malformed JSON, wrong schema, wrong field type, timeout,
authentication failure, unreachable, host-key mismatch, reporter failure,
counter reset, reboot, zero elapsed counter time, incident open/recovery,
retention, the CPU formula, the schema validator, the status command, the
overlap lock, and the real transport driven by a fake ssh (hash gate included).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from rtax_telemetry import cpu, schema                                  # noqa: E402
from rtax_telemetry.cli import acquire_lock                             # noqa: E402
from rtax_telemetry.core import Collector, OUTCOME_SUCCESS, default_state, update_state  # noqa: E402
from rtax_telemetry.errors import (                                     # noqa: E402
    ArtifactMismatch, LockHeld, MalformedData, SchemaInvalid,
)
from rtax_telemetry.mock import ScenarioTransport                       # noqa: E402
from rtax_telemetry.status import render_status, sample_age_seconds     # noqa: E402
from rtax_telemetry.store import Store                                  # noqa: E402
from rtax_telemetry.transport import SSHTransport                       # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "live_sample.json")


def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-test.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, **kw):
        s = Store(self.tmp, **kw)
        s.ensure()
        return s

    def make_collector(self, scenario, store=None, **kw):
        store = store or self.make_store()
        tr = ScenarioTransport(scenario, log=lambda *a: None)
        return Collector(tr, store, log=lambda *a: None, **kw), store, tr


# --------------------------------------------------------------------- CPU
class TestCpuFormula(unittest.TestCase):
    def test_first_sample_is_null(self):
        out = cpu.compare(None, load_fixture()["cpu_stat"])
        self.assertEqual(out["state"], "first")
        self.assertIsNone(out["aggregate"])

    def test_known_answer(self):
        prev = {"user": 100, "nice": 0, "system": 50, "idle": 850,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur = {"user": 200, "nice": 0, "system": 100, "idle": 1700,
               "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        # total delta 1000, idle delta 850 -> 15.0 %
        p = cpu._pair(prev, cur)
        self.assertEqual(p["state"], "ok")
        self.assertAlmostEqual(p["util_percent"], 15.0, places=6)

    def test_guest_not_double_counted(self):
        prev = {"user": 100, "nice": 0, "system": 0, "idle": 900,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur = {"user": 200, "nice": 0, "system": 0, "idle": 1800,
               "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur_g = dict(cur, guest=500, guest_nice=500, cached_guest=500)
        self.assertEqual(cpu._pair(prev, cur)["util_percent"],
                         cpu._pair(prev, cur_g)["util_percent"])

    def test_iowait_counts_as_idle(self):
        prev = {"user": 0, "nice": 0, "system": 0, "idle": 0,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur = {"user": 0, "nice": 0, "system": 0, "idle": 0,
               "iowait": 100, "irq": 0, "softirq": 0, "steal": 0}
        self.assertEqual(cpu._pair(prev, cur)["util_percent"], 0.0)

    def test_counter_reset(self):
        prev = {"user": 500, "nice": 0, "system": 0, "idle": 500,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        cur = {"user": 10, "nice": 0, "system": 0, "idle": 10,
               "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        self.assertEqual(cpu._pair(prev, cur)["state"], "reset")
        self.assertIsNone(cpu._pair(prev, cur)["util_percent"])

    def test_zero_elapsed(self):
        same = {"user": 5, "nice": 0, "system": 0, "idle": 5,
                "iowait": 0, "irq": 0, "softirq": 0, "steal": 0}
        self.assertEqual(cpu._pair(same, dict(same))["state"], "zero_elapsed")

    def test_reboot_detection(self):
        a = {"host": {"uptime_seconds": 1000.0}}
        b = {"host": {"uptime_seconds": 5.0}}
        self.assertTrue(cpu.detect_reboot(a, b))
        self.assertFalse(cpu.detect_reboot(b, a))

    def test_missing_uptime_is_not_a_reboot(self):
        self.assertFalse(cpu.detect_reboot({"host": {"uptime_seconds": None}},
                                           {"host": {"uptime_seconds": 5.0}}))


# ------------------------------------------------------------------ schema
class TestSchema(unittest.TestCase):
    def test_real_fixture_is_valid(self):
        schema.parse_and_validate(open(FIXTURE, "rb").read())

    def test_malformed(self):
        with self.assertRaises(MalformedData):
            schema.parse_and_validate(b"{ not json")

    def test_non_object(self):
        with self.assertRaises(MalformedData):
            schema.parse_and_validate(b"[1,2,3]")

    def test_wrong_schema(self):
        d = load_fixture()
        d["schema"] = "router_health/1"
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_wrong_field_type(self):
        d = load_fixture()
        d["memory_kb"]["MemTotal"] = "lots"
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_missing_required_field(self):
        d = load_fixture()
        del d["storage"]["candidates_count"]
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_bad_cpu_stat(self):
        d = load_fixture()
        d["cpu_stat"]["cpu00"] = {"user": "x"}
        with self.assertRaises(SchemaInvalid):
            schema.validate(d)

    def test_null_uptime_accepted(self):
        d = load_fixture()
        d["host"]["uptime_seconds"] = None
        schema.validate(d)


# -------------------------------------------------------------- outcomes
class TestOutcomes(TempDirCase):
    def test_success(self):
        c, store, tr = self.make_collector("success")
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_SUCCESS)
        self.assertTrue(a["sample_path"] and os.path.isfile(a["sample_path"]))
        self.assertEqual(store.sample_count(), 1)

    def test_malformed(self):
        c, _, _ = self.make_collector("malformed")
        self.assertEqual(c.collect_once()["outcome"], "malformed_data")

    def test_wrong_schema(self):
        c, _, _ = self.make_collector("wrong_schema")
        self.assertEqual(c.collect_once()["outcome"], "schema_invalid")

    def test_wrong_type(self):
        c, _, _ = self.make_collector("wrong_type")
        self.assertEqual(c.collect_once()["outcome"], "schema_invalid")

    def test_timeout(self):
        c, _, _ = self.make_collector("timeout")
        self.assertEqual(c.collect_once()["outcome"], "timeout")

    def test_auth_failure(self):
        c, _, _ = self.make_collector("auth")
        self.assertEqual(c.collect_once()["outcome"], "ssh_auth_failure")

    def test_unreachable(self):
        c, _, _ = self.make_collector("unreachable")
        a = c.collect_once()
        self.assertEqual(a["outcome"], "unreachable")
        self.assertIn("does not prove the router is down", a["detail"])

    def test_hostkey(self):
        c, _, _ = self.make_collector("hostkey")
        self.assertEqual(c.collect_once()["outcome"], "ssh_hostkey_mismatch")

    def test_reporter_failure(self):
        c, _, _ = self.make_collector("reporter_failure")
        self.assertEqual(c.collect_once()["outcome"], "reporter_failure")

    def test_failure_produces_no_sample_file(self):
        c, store, _ = self.make_collector("timeout")
        c.collect_once()
        self.assertEqual(store.sample_count(), 0)


# --------------------------------------------------------- derived across 2
class TestDerivedAcrossSamples(TempDirCase):
    def _two(self, scenario, **kw):
        c, store, _ = self.make_collector(scenario, **kw)
        c.collect_once()
        c.collect_once()
        last = store.last_sample()
        return last["derived"]["cpu_utilisation"]

    def test_success_second_sample_has_utilisation(self):
        util = self._two("success")
        self.assertEqual(util["state"], "ok")
        self.assertTrue(0 <= util["aggregate"]["util_percent"] <= 100)
        self.assertIsNotNone(util["interval_seconds"])

    def test_counter_reset(self):
        self.assertEqual(self._two("reset")["state"], "reset")

    def test_reboot(self):
        self.assertEqual(self._two("reboot")["state"], "reboot")

    def test_zero_elapsed(self):
        self.assertEqual(self._two("zero_elapsed")["state"], "zero_elapsed")


# ------------------------------------------------------------- incidents
class TestIncidents(TempDirCase):
    def test_opens_after_three_failures(self):
        c, store, tr = self.make_collector("timeout")
        c.collect_once()
        self.assertIsNone(store.load_state()["incident"])
        c.collect_once()
        self.assertIsNone(store.load_state()["incident"])
        c.collect_once()
        st = store.load_state()
        self.assertIsNotNone(st["incident"])
        self.assertEqual(st["consecutive_failures"], 3)
        self.assertEqual(st["incident"]["reason_outcome"], "timeout")

    def test_two_failures_do_not_open(self):
        c, store, _ = self.make_collector("timeout")
        for _ in range(2):
            c.collect_once()
        self.assertIsNone(store.load_state()["incident"])

    def test_resolves_after_two_successes(self):
        c, store, tr = self.make_collector("timeout")
        for _ in range(3):
            c.collect_once()
        self.assertIsNotNone(store.load_state()["incident"])
        tr.scenario = "success"
        c.collect_once()
        self.assertIsNotNone(store.load_state()["incident"], "one success must not resolve")
        c.collect_once()
        st = store.load_state()
        self.assertIsNone(st["incident"])
        self.assertIsNotNone(st["last_resolved_incident"])

    def test_custom_thresholds(self):
        c, store, _ = self.make_collector("timeout", open_after=2, resolve_after=1)
        c.collect_once()
        self.assertIsNone(store.load_state()["incident"])
        c.collect_once()
        self.assertIsNotNone(store.load_state()["incident"])

    def test_success_clears_failure_streak(self):
        c, store, tr = self.make_collector("timeout")
        c.collect_once(); c.collect_once()
        tr.scenario = "success"
        c.collect_once()
        st = store.load_state()
        self.assertEqual(st["consecutive_failures"], 0)
        self.assertEqual(st["consecutive_successes"], 1)


# ------------------------------------------------------------- retention
class TestRetention(TempDirCase):
    def test_sample_retention(self):
        s = self.make_store(retain_samples=3)
        for _ in range(5):
            s.save_sample({"n": 1})
        self.assertEqual(s.sample_count(), 3)

    def test_outcome_retention(self):
        s = self.make_store(retain_outcomes=2)
        for i in range(5):
            s.append_outcome({"outcome": "success", "i": i})
        rows = s.outcomes()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["i"], 4)

    def test_prune_command_counts(self):
        s = self.make_store(retain_samples=1, retain_outcomes=1)
        for i in range(4):
            s.save_sample({"i": i})
            s.append_outcome({"i": i})
        # retention already applied; prune is a no-op
        self.assertEqual(s.prune(), 0)
        self.assertEqual(s.sample_count(), 1)


# -------------------------------------------------------------- status
class TestStatus(TempDirCase):
    def test_status_fields(self):
        c, store, _ = self.make_collector("success")
        c.collect_once()
        out = render_status(store, store.load_state(), host="routeradmin@192.0.2.1")
        self.assertIn("last attempt", out)
        self.assertIn("last valid sample", out)
        self.assertIn("collection incident : none", out)
        self.assertIn("consecutive_failures=0", out)

    def test_status_reports_open_incident(self):
        c, store, _ = self.make_collector("unreachable")
        for _ in range(3):
            c.collect_once()
        out = render_status(store, store.load_state())
        self.assertIn("collection incident : OPEN", out)
        self.assertIn("unreachable", out)

    def test_sample_age(self):
        c, store, _ = self.make_collector("success")
        c.collect_once()
        import time
        self.assertLessEqual(sample_age_seconds(store, time.time()), 2)


# ----------------------------------------------------------------- lock
class TestLock(TempDirCase):
    def test_second_lock_refused_and_released(self):
        store = self.make_store()
        fd = acquire_lock(store)
        with self.assertRaises(LockHeld):
            acquire_lock(store)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        # after release, a fresh lock succeeds
        fd2 = acquire_lock(store)
        os.close(fd2)

    def test_cli_lock_conflict_exit_code(self):
        data = os.path.join(self.tmp, "d")
        store = Store(data)
        store.ensure()
        held = acquire_lock(store)
        try:
            r = subprocess.run(
                [sys.executable, os.path.join(ROOT, "collector.py"), "collect", "--once",
                 "--transport", "mock", "--data-dir", data],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 3)
            self.assertIn("another collector run", r.stderr)
        finally:
            import fcntl
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)


# --------------------------------------------------------- transport gate
class TestTransportPreflight(TempDirCase):
    def test_artifact_hash_mismatch_blocks_before_network(self):
        artifact = os.path.join(self.tmp, "artifact")
        with open(artifact, "wb") as fh:
            fh.write(b"not the real binary")
        kh = os.path.join(self.tmp, "known_hosts")
        with open(kh, "w") as fh:
            fh.write("example ssh-rsa AAAA\n")
        tr = SSHTransport("192.0.2.1", "routeradmin", artifact=artifact,
                          known_hosts=kh, artifact_sha="00" * 32,
                          known_hosts_sha=None)
        with self.assertRaises(ArtifactMismatch):
            tr.preflight()

    def test_known_hosts_hash_mismatch_blocks(self):
        artifact = os.path.join(self.tmp, "artifact")
        with open(artifact, "wb") as fh:
            fh.write(b"x")
        kh = os.path.join(self.tmp, "known_hosts")
        with open(kh, "w") as fh:
            fh.write("example ssh-rsa AAAA\n")
        tr = SSHTransport("192.0.2.1", "routeradmin", artifact=artifact,
                          known_hosts=kh, artifact_sha=None,
                          known_hosts_sha="00" * 32)
        with self.assertRaises(ArtifactMismatch):
            tr.preflight()


# ----------------------------------------------- real transport, fake ssh
class TestTransportWithFakeSSH(TempDirCase):
    def _make(self, mode="ok", artifact_sha=None, verify_artifact=True):
        artifact = os.path.join(self.tmp, "artifact")
        with open(artifact, "wb") as fh:
            fh.write(b"FAKE-ARM-ARTIFACT-BYTES" * 100)
        kh = os.path.join(self.tmp, "known_hosts")
        with open(kh, "w") as fh:
            fh.write("192.0.2.1 ssh-ed25519 AAAAC3Nza\n")
        env = dict(os.environ,
                   FAKE_SSH_MODE=mode,
                   FAKE_SSH_DOC=FIXTURE)
        # a tiny wrapper so we can inject the env into the ssh child
        wrapper = os.path.join(self.tmp, "fake-ssh")
        with open(wrapper, "w") as fh:
            fh.write('#!/bin/bash\nexport FAKE_SSH_MODE=%s\nexport FAKE_SSH_DOC=%s\n'
                     'exec "%s" "$@"\n'
                     % (mode, FIXTURE, os.path.join(HERE, "fake_ssh.sh")))
        os.chmod(wrapper, 0o755)
        with open(artifact, "rb") as fh:
            artifact_bytes = fh.read()
        import hashlib
        sha = hashlib.sha256(artifact_bytes).hexdigest()
        tr = SSHTransport("192.0.2.1", "routeradmin", artifact=artifact,
                          known_hosts=kh,
                          artifact_sha=artifact_sha if artifact_sha is not None else sha,
                          known_hosts_sha=None, ssh_bin=wrapper,
                          connect_timeout=5, run_bound=5,
                          interactive_auth=False, log=lambda *a: None)
        return tr

    def test_full_provision_collect_teardown(self):
        tr = self._make("ok")
        session = tr.start()
        self.assertTrue(session.provisioned)
        raw = tr.collect_sample(session)
        doc = schema.parse_and_validate(raw)
        self.assertEqual(doc["schema"], "router_health/2")
        tr.stop(session)

    def test_router_side_hash_gate_blocks(self):
        tr = self._make("ok", artifact_sha="de" * 32, verify_artifact=False)
        with self.assertRaises(ArtifactMismatch):
            tr.start()

    def test_reporter_timeout_maps_to_remote_timeout(self):
        from rtax_telemetry.errors import RemoteTimeout
        tr = self._make("timeout")
        session = tr.start()
        try:
            with self.assertRaises(RemoteTimeout):
                tr.collect_sample(session)
        finally:
            tr.close()

    def test_reporter_failure_maps_to_command_error(self):
        from rtax_telemetry.errors import RemoteCommandError
        tr = self._make("reporter_fail")
        session = tr.start()
        try:
            with self.assertRaises(RemoteCommandError):
                tr.collect_sample(session)
        finally:
            tr.close()

    def test_unreachable_maps_to_unreachable(self):
        from rtax_telemetry.errors import TransportUnreachableError
        tr = self._make("unreachable")
        with self.assertRaises(TransportUnreachableError):
            tr.start()

    def test_auth_failure_maps_to_auth(self):
        from rtax_telemetry.errors import TransportAuthError
        tr = self._make("authfail")
        with self.assertRaises(TransportAuthError):
            tr.start()

    def test_end_to_end_through_collector(self):
        tr = self._make("ok")
        c = Collector(tr, self.make_store(), log=lambda *a: None)
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_SUCCESS)
        c.close()


# ------------------------------------------------------------------ CLI e2e
class TestCli(TempDirCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "collector.py"), *args],
                              capture_output=True, text=True)

    def test_collect_once_success(self):
        r = self._run("collect", "--once", "--transport", "mock", "--data-dir", self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Store(self.tmp).sample_count(), 1)

    def test_collect_once_failure_exit_code(self):
        r = self._run("collect", "--once", "--transport", "mock",
                      "--mock-scenario", "malformed", "--data-dir", self.tmp)
        self.assertEqual(r.returncode, 1)

    def test_status_command(self):
        self._run("collect", "--once", "--transport", "mock", "--data-dir", self.tmp)
        r = self._run("status", "--transport", "mock", "--data-dir", self.tmp)
        self.assertEqual(r.returncode, 0)
        self.assertIn("collection incident", r.stdout)

    def test_validate_command(self):
        r = self._run("validate", FIXTURE)
        self.assertEqual(r.returncode, 0, r.stderr)
        bad = os.path.join(self.tmp, "bad.json")
        open(bad, "w").write("{ nope")
        self.assertEqual(self._run("validate", bad).returncode, 1)

    def test_selftest_command(self):
        self.assertEqual(self._run("selftest").returncode, 0)


# ----------------------------------------------------------- loop control
class TestLoop(unittest.TestCase):
    def test_loop_stops_on_keyboard_interrupt(self):
        import threading
        store = Store(tempfile.mkdtemp(prefix="rtax-loop."))
        store.ensure()
        tr = ScenarioTransport("success", log=lambda *a: None)

        class Boom(Collector):
            def collect_once(self):
                raise KeyboardInterrupt

        c = Boom(tr, store, log=lambda *a: None)
        from rtax_telemetry.cli import run_loop
        rc = run_loop(c, store, 1.0)
        self.assertEqual(rc, 130)
        shutil.rmtree(str(store.dir), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
