"""Offline regressions for the evidence-loss defects in the independent audit.

An independent audit of `8778fb3` (collector 1.5.4) reproduced three ways the
collector deletes evidence while reporting success:

  C1  retention prunes samples by their wall-clock FILENAME. When the retained set
      is at its limit and the clock steps back further than the set spans, the
      sample just written sorts first and is the one deleted. The attempt still
      reports `success`, and the state pointer names a file that no longer exists
  C2  a `config.json` that cannot be parsed is ignored with a warning, so `prune`
      runs with the DEFAULT retention limits and deletes what the configured
      limits would have kept
  C3  a retention count of zero or below is accepted during collection: the run
      exits 0 having deleted the sample (or the outcome log) it has just written

Nothing here contacts a router. Every transport is the offline scenario transport,
directly or through `--transport mock`.

Tests whose docstring starts with REGRESSION fail on `8778fb3` and must pass after
the correction. Tests whose docstring starts with GUARD pass on both: they pin what
the correction must NOT change (ordinary oldest-first retention, a valid
configuration being honoured, and the explicit `prune --retain-samples 0` purge
that `test_fixes.py` relies on).

The regressions state the invariant, not the fix. C2 and C3 accept either a
refusal (non-zero exit) or a run that keeps the evidence.

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
from rtax_telemetry.core import OUTCOME_SUCCESS, Collector                # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
