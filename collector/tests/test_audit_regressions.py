"""Offline regressions for the independent audit of `8778fb3` (collector 1.5.4).

The audit reproduced three ways the collector deletes evidence while reporting
success:

  C1  retention prunes samples by their wall-clock FILENAME. When the retained set
      is at its limit and the clock steps back further than the set spans, the
      sample just written sorts first and is the one deleted. The attempt still
      reports `success`, and the state pointer names a file that no longer exists
  C2  a `config.json` that cannot be parsed is ignored with a warning, so `prune`
      runs with the DEFAULT retention limits and deletes what the configured
      limits would have kept
  C3  a retention count of zero or below is accepted during collection: the run
      exits 0 having deleted the sample (or the outcome log) it has just written

and three smaller hardening and classification gaps:

  C5  `validate FILE` prints the document's `schema`, `tool` and `version` strings
      raw, so escape bytes in a hostile file reach the terminal and a lone
      surrogate ends the command with a traceback
  C6  with `known_hosts_sha` absent, empty or null the pinned-file hash gate is
      skipped without a word, although a missing `artifact_sha` is refused
  C7  a stalled control-master establishment is recorded as `timeout`, the class
      reserved for a reporter run that ended, though the reporter never started

Nothing here contacts a router. Every transport is the offline scenario transport
(directly or through `--transport mock`), or the real `SSHTransport` driven by the
fake `ssh` or a local stand-in script.

Tests whose docstring starts with REGRESSION fail on `8778fb3` and must pass after
the correction. Tests whose docstring starts with GUARD pass on both: they pin what
the correction must NOT change (ordinary oldest-first retention, a valid
configuration being honoured, and the explicit `prune --retain-samples 0` purge
that `test_fixes.py` relies on).

The regressions state the invariant, not the fix. C2 and C3 accept either a
refusal (non-zero exit) or a run that keeps the evidence; C6 accepts either a
refusal or a diagnostic that names the unchecked file.

No test touches real evidence: every data directory is created under a fresh
temporary directory.
"""

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
from rtax_telemetry import transport                                      # noqa: E402
from rtax_telemetry.core import (                                         # noqa: E402
    OUTCOME_HOSTKEY, OUTCOME_SUCCESS, OUTCOME_TIMEOUT, Collector,
)
from rtax_telemetry.mock import ScenarioTransport                         # noqa: E402
from rtax_telemetry.store import Store                                    # noqa: E402

T0 = 1_800_000_000.0
DAY = 86400


def quiet(*_a):
    pass


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtax-audit.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")

    def samples(self):
        d = os.path.join(self.data, "samples")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def outcome_lines(self):
        path = os.path.join(self.data, "outcomes.jsonl")
        if not os.path.isfile(path):
            return 0
        with open(path) as fh:
            return sum(1 for ln in fh if ln.strip())


# ================================================== C1: retention and the clock
class TestRetentionSurvivesBackwardClockStep(TempCase):
    def collector(self, retain=5):
        self.clock = [T0]
        self.mono = [1000.0]
        self.store = Store(self.data, retain_samples=retain, log=quiet)
        return Collector(ScenarioTransport("success", log=quiet), self.store, log=quiet,
                         clock=lambda: self.clock[0], monotonic=lambda: self.mono[0])

    def tick(self, seconds=60):
        self.clock[0] += seconds
        self.mono[0] += seconds

    def fill(self, c, n=5):
        for _ in range(n):
            a = c.collect_once()
            self.assertEqual(a["outcome"], OUTCOME_SUCCESS, a["detail"])
            self.tick()

    def test_new_sample_survives_a_backward_step_at_the_limit(self):
        """REGRESSION: a successful attempt after the clock steps back keeps its sample."""
        c = self.collector()
        self.fill(c)
        self.clock[0] -= DAY
        for i in range(3):
            a = c.collect_once()
            self.assertEqual(a["outcome"], OUTCOME_SUCCESS, a["detail"])
            self.assertTrue(os.path.isfile(a["sample_path"]),
                            "attempt %d reported success but its sample %s was pruned"
                            % (i + 1, os.path.basename(a["sample_path"])))
            self.tick()

    def test_state_pointer_resolves_after_a_backward_step(self):
        """REGRESSION: state never names a sample that retention has just deleted."""
        c = self.collector()
        self.fill(c)
        self.clock[0] -= DAY
        c.collect_once()
        meta = self.store.load_state()["last_valid_sample"]
        self.assertIsNotNone(self.store.resolve_sample_path(meta["path"]),
                             "state.last_valid_sample names a file that is gone: %s"
                             % meta["path"])

    def test_store_keeps_a_sample_saved_with_an_earlier_timestamp(self):
        """REGRESSION: save_sample(when=earlier) at the limit leaves the new file in place."""
        store = Store(self.data, retain_samples=3, log=quiet)
        for i in range(3):
            store.save_sample({"n": i}, when=T0 + 60 * i)
        path = store.save_sample({"n": "after the step"}, when=T0 - DAY)
        self.assertTrue(os.path.isfile(path),
                        "the sample save_sample() just returned was pruned by the same call")

    def test_the_count_limit_still_holds_after_a_backward_step(self):
        """GUARD: a backward step does not let the retained set grow past its limit."""
        c = self.collector()
        self.fill(c)
        self.clock[0] -= DAY
        for _ in range(3):
            c.collect_once()
            self.tick()
        self.assertLessEqual(self.store.sample_count(), 5)

    def test_ordinary_retention_drops_the_oldest_first(self):
        """GUARD: with a forward clock, the oldest sample goes and the newest stays."""
        c = self.collector()
        self.fill(c)
        before = self.samples()
        a = c.collect_once()
        after = self.samples()
        self.assertEqual(len(after), 5)
        self.assertNotIn(before[0], after, "the oldest sample was not the one removed")
        self.assertEqual(after[:4], before[1:])
        self.assertTrue(os.path.isfile(a["sample_path"]))


# ============================================= C2: unreadable config and prune
class TestUnreadableConfigDoesNotPruneWithDefaults(TempCase):
    SAMPLES = 1500      # over the default limit of 1440, under the configured 3000
    OUTCOMES = 5200     # over the default limit of 5000, under the configured 9000

    def populate(self, config_text):
        Store(self.data, retain_samples=3000, retain_outcomes=9000, log=quiet).ensure()
        for i in range(self.SAMPLES):
            name = "20200101T%06d.000000Z.json" % i
            with open(os.path.join(self.data, "samples", name), "w") as fh:
                fh.write("{}")
        with open(os.path.join(self.data, "outcomes.jsonl"), "w") as fh:
            for i in range(self.OUTCOMES):
                fh.write('{"i":%d}\n' % i)
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            fh.write(config_text)

    def test_prune_with_a_malformed_config_deletes_nothing(self):
        """REGRESSION: a config.json with a trailing comma is not replaced by defaults."""
        self.populate('{"retain_samples": 3000, "retain_outcomes": 9000,}')
        r = harness.run_cli(["prune", "--data-dir", self.data])
        self.assertEqual((len(self.samples()), self.outcome_lines()),
                         (self.SAMPLES, self.OUTCOMES),
                         "prune deleted evidence using default limits (exit %d): %s"
                         % (r.returncode, r.stderr.strip()[-200:]))

    def test_prune_with_a_malformed_config_does_not_report_success(self):
        """REGRESSION: prune exits non-zero when it cannot read its configuration."""
        self.populate('{"retain_samples": 3000, "retain_outcomes": 9000,}')
        r = harness.run_cli(["prune", "--data-dir", self.data])
        self.assertNotEqual(r.returncode, 0, r.stderr)

    def test_prune_with_a_non_object_config_deletes_nothing(self):
        """REGRESSION: a config.json that is valid JSON but not an object is not ignored."""
        self.populate('[3000, 9000]')
        r = harness.run_cli(["prune", "--data-dir", self.data])
        self.assertEqual((len(self.samples()), self.outcome_lines()),
                         (self.SAMPLES, self.OUTCOMES),
                         "prune deleted evidence using default limits (exit %d)" % r.returncode)

    def test_prune_honours_a_valid_config(self):
        """GUARD: with a readable config the configured limits apply and nothing is cut."""
        self.populate(json.dumps({"retain_samples": 3000, "retain_outcomes": 9000}))
        r = harness.run_cli(["prune", "--data-dir", self.data])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((len(self.samples()), self.outcome_lines()),
                         (self.SAMPLES, self.OUTCOMES))

    def test_prune_without_a_config_uses_the_defaults(self):
        """GUARD: an ABSENT config.json still means the documented defaults."""
        self.populate("{}")
        os.unlink(os.path.join(self.data, "config.json"))
        r = harness.run_cli(["prune", "--data-dir", self.data])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((len(self.samples()), self.outcome_lines()), (1440, 5000))


# ======================================== C3: retention below one in collection
class TestCollectionRejectsRetentionBelowOne(TempCase):
    def collect(self, *extra):
        return harness.run_cli(["collect", "--once", "--transport", "mock",
                                "--data-dir", self.data] + list(extra))

    def test_retain_samples_zero_does_not_delete_the_new_sample(self):
        """REGRESSION: `collect --retain-samples 0` cannot exit 0 with no sample kept."""
        r = self.collect("--retain-samples", "0")
        self.assertTrue(r.returncode != 0 or len(self.samples()) == 1,
                        "exit 0 with %d samples on disk" % len(self.samples()))

    def test_negative_retain_samples_does_not_delete_the_new_sample(self):
        """REGRESSION: `collect --retain-samples -3` cannot exit 0 with no sample kept."""
        r = self.collect("--retain-samples", "-3")
        self.assertTrue(r.returncode != 0 or len(self.samples()) == 1,
                        "exit 0 with %d samples on disk" % len(self.samples()))

    def test_retain_outcomes_zero_does_not_empty_the_log(self):
        """REGRESSION: `collect --retain-outcomes 0` cannot exit 0 with an empty log."""
        for _ in range(3):
            self.assertEqual(self.collect().returncode, 0)
        self.assertEqual(self.outcome_lines(), 3)
        r = self.collect("--retain-outcomes", "0")
        self.assertTrue(r.returncode != 0 or self.outcome_lines() >= 1,
                        "exit 0 with the outcome log emptied")

    def test_retention_from_config_below_one_is_not_applied(self):
        """REGRESSION: `retain_samples: 0` in config.json is treated like the flag."""
        os.makedirs(self.data)
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            json.dump({"retain_samples": 0}, fh)
        r = self.collect()
        self.assertTrue(r.returncode != 0 or len(self.samples()) == 1,
                        "exit 0 with %d samples on disk" % len(self.samples()))

    def test_retain_samples_one_keeps_exactly_the_newest(self):
        """GUARD: the smallest valid limit still works."""
        for _ in range(2):
            r = self.collect("--retain-samples", "1")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 1)

    def test_explicit_prune_to_zero_still_purges(self):
        """GUARD: `prune --retain-samples 0` remains the explicit purge."""
        self.assertEqual(self.collect().returncode, 0)
        r = harness.run_cli(["prune", "--data-dir", self.data, "--retain-samples", "0"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 0)


# =========================================== C5: validate prints device strings
class TestValidateSanitisesDeviceStrings(TempCase):
    def document(self, **fields):
        with open(harness.FIXTURE) as fh:
            doc = json.load(fh)
        doc.update(fields)
        path = os.path.join(self.tmp, "document.json")
        with open(path, "w") as fh:
            json.dump(doc, fh)
        return path

    def validate(self, path):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(ROOT, "collector.py"),
                               "validate", path], capture_output=True, timeout=60)

    def test_escape_bytes_in_tool_and_version_do_not_reach_the_terminal(self):
        """REGRESSION: control bytes in `tool`/`version` are not printed raw."""
        r = self.validate(self.document(tool="router_health\x1b]0;TITLE\x07\x1b[2J",
                                        version="1.0.4\x1b[31mRED"))
        out = r.stdout + r.stderr
        self.assertFalse(any(b < 0x20 and b != 0x0A for b in out) or 0x7F in out,
                         "raw control bytes on the terminal: %r" % out[:160])

    def test_a_lone_surrogate_does_not_end_validate_with_a_traceback(self):
        """REGRESSION: a lone surrogate in `tool` is reported, not a crash (exit 70)."""
        r = self.validate(self.document(tool="x\ud800y"))
        self.assertNotEqual(r.returncode, 70, r.stderr[-300:])
        self.assertNotIn(b"Traceback", r.stderr)

    def test_an_ordinary_document_is_still_reported_valid(self):
        """GUARD: the fixture validates and its tool and version are shown as-is."""
        r = self.validate(self.document())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"VALID router_health document", r.stdout)
        self.assertIn(b"tool=router_health", r.stdout)

    def test_an_invalid_document_is_still_rejected(self):
        """GUARD: a schema failure still exits 1."""
        r = self.validate(self.document(schema="something/else"))
        self.assertEqual(r.returncode, 1, r.stdout)


# ============================================== C6: the known_hosts hash gate
class TestKnownHostsHashGateIsNotSilentlySkipped(TempCase):
    ADDED = "192.0.2.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIadded-after-review\n"

    def write_config(self):
        # The fake master goes away once `-O exit` reaches it, as a real one does.
        gone = os.path.join(self.tmp, "master.gone")
        return harness.write_ssh_config(self.data, self.tmp, mode="ok",
                                        extra_env={"FAKE_SSH_MASTER_STATE": gone})

    def configure(self, **overrides):
        cfg = self.write_config()
        with open(cfg["known_hosts"], "a") as fh:
            fh.write(self.ADDED)                 # the pinned file no longer matches
        for key, value in overrides.items():
            if value is KeyError:
                cfg.pop(key)
            else:
                cfg[key] = value
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            json.dump(cfg, fh)

    def collect(self):
        return harness.run_cli(["collect", "--once", "--data-dir", self.data])

    def assert_not_silent(self, r):
        text = r.stderr.lower()
        said = "known_hosts" in text and ("hash" in text or "sha" in text)
        self.assertTrue(r.returncode != 0 or said,
                        "exit 0 with an altered known_hosts file and no diagnostic:\n%s"
                        % r.stderr[-400:])

    def test_absent_hash_is_not_silently_accepted(self):
        """REGRESSION: no `known_hosts_sha` key does not skip the gate without a word."""
        self.configure(known_hosts_sha=KeyError)
        self.assert_not_silent(self.collect())

    def test_empty_hash_is_not_silently_accepted(self):
        """REGRESSION: `known_hosts_sha: ""` does not skip the gate without a word."""
        self.configure(known_hosts_sha="")
        self.assert_not_silent(self.collect())

    def test_null_hash_is_not_silently_accepted(self):
        """REGRESSION: `known_hosts_sha: null` does not skip the gate without a word."""
        self.configure(known_hosts_sha=None)
        self.assert_not_silent(self.collect())

    def test_a_configured_hash_still_refuses_an_altered_file(self):
        """GUARD: with the hash configured, an altered file is an artifact mismatch."""
        self.configure()
        r = self.collect()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("outcome=artifact_mismatch", r.stderr)

    def test_a_configured_hash_accepts_the_reviewed_file(self):
        """GUARD: with the hash configured and the file intact, collection succeeds."""
        self.write_config()
        r = self.collect()
        self.assertEqual(r.returncode, 0, r.stderr)


# ====================================== C7: a stalled master is not a `timeout`
class TestStalledMasterEstablishmentIsNotAReporterTimeout(TempCase):
    def stalled_attempt(self):
        stall = os.path.join(self.tmp, "stall-ssh")
        with open(stall, "w") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        os.chmod(stall, 0o755)
        artifact = harness.make_artifact(self.tmp)
        old = transport.REAP_GRACE
        transport.REAP_GRACE = 0.5
        self.addCleanup(setattr, transport, "REAP_GRACE", old)
        tr = transport.SSHTransport(
            "192.0.2.1", "routeradmin", artifact=artifact,
            known_hosts=harness.make_known_hosts(self.tmp),
            artifact_sha=harness._sha(artifact), ssh_bin=stall,
            connect_timeout=-18,                 # local bound: -18 + 20 = 2 s
            interactive_auth=False, log=quiet)
        c = Collector(tr, Store(self.data, log=quiet), log=quiet)
        try:
            return c.collect_once()
        finally:
            c.close()

    def test_a_stalled_master_is_not_classified_timeout(self):
        """REGRESSION: `timeout` is not used when the reporter never started."""
        a = self.stalled_attempt()
        self.assertNotEqual(a["outcome"], OUTCOME_TIMEOUT, a["detail"])

    def test_the_detail_names_master_establishment(self):
        """REGRESSION: the record says which stage stalled."""
        a = self.stalled_attempt()
        self.assertIn("master", a["detail"].lower())

    def test_a_hostkey_mismatch_at_establishment_keeps_its_class(self):
        """GUARD: establishment failures that have their own class keep it."""
        tr = harness.make_transport(self.tmp, "hostkey_mismatch")
        c = Collector(tr, Store(self.data, log=quiet), log=quiet)
        try:
            a = c.collect_once()
        finally:
            c.close()
        self.assertEqual(a["outcome"], OUTCOME_HOSTKEY, a["detail"])


if __name__ == "__main__":
    unittest.main()
