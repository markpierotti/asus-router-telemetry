"""Regressions for the gaps the review of the audit corrections reproduced.

The first round of corrections (R1-R3, C1-C3, C5-C7) was challenged offline at
`59774d1`. Five gaps were reproduced there; these tests pin them:

  C3  a byte budget of at least 1 but smaller than one record still deleted the
      record just written, with exit 0
  C3  `prune` applied config values that `collect` refuses (false, 0.5, -1, "3"),
      and a null ended either command in a traceback
  C2  a dangling symlink or a directory named config.json, an unknown key and a
      repeated key were all treated as "no configuration"
  C6  `known_hosts_sha` of 0, [] or {} opted out of the file-digest gate exactly as
      `false` does
  C1  standalone `prune` after a backward clock step deleted the sample state names

Tests whose docstring starts with REGRESSION fail on `59774d1` and must pass after
the correction. Tests whose docstring starts with GUARD pass on both: they pin what
the correction must NOT change (the explicit command-line purge, valid
configuration, the `false` opt-out, ordinary retention).

Nothing here contacts a router: the mock transport, the fake `ssh`, or the Store
directly. Every data directory is under a fresh temporary directory.
"""

import json
import os
import sys
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
from test_audit_regressions import DAY, T0, TempCase, quiet               # noqa: E402

DOC = {"k": "v" * 200}


class CliCase(TempCase):
    def write_config(self, cfg):
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            if isinstance(cfg, str):
                fh.write(cfg)
            else:
                json.dump(cfg, fh)

    def collect(self, *extra):
        return harness.run_cli(["collect", "--once", "--transport", "mock",
                                "--data-dir", self.data] + list(extra))

    def prune(self, *extra):
        return harness.run_cli(["prune", "--data-dir", self.data] + list(extra))

    def seed(self, n=8):
        st = Store(self.data, log=quiet)
        for i in range(n):
            st.save_sample(DOC, when=T0 + i * 60)
            st.append_outcome({"i": i})
        return st


# ============================================ C3: byte budgets during collection
class TestByteBudgetKeepsTheRecordJustWritten(CliCase):
    def test_a_small_sample_byte_budget_does_not_delete_the_new_sample(self):
        """REGRESSION: `retain_sample_bytes: 1024` cannot exit 0 with no sample kept."""
        self.write_config({"retain_sample_bytes": 1024})
        r = self.collect()
        self.assertTrue(r.returncode != 0 or len(self.samples()) == 1,
                        "exit 0 with %d samples on disk" % len(self.samples()))

    def test_a_small_outcome_byte_budget_does_not_empty_the_log(self):
        """REGRESSION: `retain_outcome_bytes: 100` cannot exit 0 with an empty log."""
        self.write_config({"retain_outcome_bytes": 100})
        r = self.collect()
        self.assertTrue(r.returncode != 0 or self.outcome_lines() >= 1,
                        "exit 0 with the outcome log emptied")

    def test_the_store_keeps_a_sample_larger_than_its_byte_budget(self):
        """REGRESSION: save_sample never removes the file it has just written."""
        st = Store(self.data, retain_sample_bytes=10, log=quiet)
        path = st.save_sample(DOC, when=T0)
        self.assertTrue(path.exists(), "save_sample returned a path it had deleted")

    def test_the_store_keeps_an_outcome_larger_than_its_byte_budget(self):
        """REGRESSION: append_outcome never removes the line it has just written."""
        st = Store(self.data, retain_outcome_bytes=10, log=quiet)
        st.append_outcome({"outcome": "success", "detail": "x" * 50})
        self.assertEqual(self.outcome_lines(), 1)

    def test_a_byte_budget_still_bounds_the_older_samples(self):
        """GUARD: a budget that fits three samples keeps at most three, newest included."""
        size = len(json.dumps(DOC, separators=(",", ":")))
        st = Store(self.data, retain_sample_bytes=3 * size, log=quiet)
        last = None
        for i in range(6):
            last = st.save_sample(DOC, when=T0 + i * 60)
        self.assertLessEqual(len(self.samples()), 3)
        self.assertTrue(last.exists())

    def test_an_outcome_byte_budget_still_bounds_the_older_lines(self):
        """GUARD: a budget that fits a few lines trims the oldest and keeps the newest."""
        st = Store(self.data, retain_outcome_bytes=60, log=quiet)
        for i in range(10):
            st.append_outcome({"i": i})
        with open(os.path.join(self.data, "outcomes.jsonl")) as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertLess(len(lines), 10)
        self.assertEqual(lines[-1], {"i": 9})

    def test_an_explicit_byte_purge_still_purges(self):
        """GUARD: `prune --retain-sample-bytes 0` on the command line removes every sample."""
        self.seed()
        r = self.prune("--retain-sample-bytes", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 0)


# ============================== C3: prune and configuration values of the wrong type
class TestPruneRefusesMistypedConfig(CliCase):
    def assert_prune_refuses(self, value):
        self.seed()
        self.write_config({"retain_samples": value})
        r = self.prune()
        self.assertEqual((len(self.samples()), self.outcome_lines()), (8, 8),
                         "prune deleted evidence for retain_samples=%r (exit %d)"
                         % (value, r.returncode))
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_false_in_config_is_not_a_purge(self):
        """REGRESSION: `retain_samples: false` does not make prune delete everything."""
        self.assert_prune_refuses(False)

    def test_a_fraction_in_config_is_not_a_purge(self):
        """REGRESSION: `retain_samples: 0.5` does not make prune delete everything."""
        self.assert_prune_refuses(0.5)

    def test_a_negative_in_config_is_not_a_purge(self):
        """REGRESSION: `retain_samples: -1` does not make prune delete everything."""
        self.assert_prune_refuses(-1)

    def test_zero_in_config_is_not_a_purge(self):
        """REGRESSION: a purge has to be asked for on the command line, not found in config."""
        self.assert_prune_refuses(0)

    def test_true_in_config_is_not_a_limit_of_one(self):
        """REGRESSION: `retain_samples: true` is not read as 1."""
        self.assert_prune_refuses(True)

    def test_a_string_in_config_is_not_a_number(self):
        """REGRESSION: `retain_samples: "3"` is refused by prune as it is by collect."""
        self.assert_prune_refuses("3")

    def test_null_in_config_is_reported_by_prune_not_a_traceback(self):
        """REGRESSION: `retain_samples: null` exits 2 with a message."""
        self.seed()
        self.write_config({"retain_samples": None})
        r = self.prune()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_null_in_config_is_reported_by_collect_not_a_traceback(self):
        """REGRESSION: `retain_samples: null` exits 2 with a message."""
        self.write_config({"retain_samples": None})
        r = self.collect()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_negative_flag_is_refused(self):
        """REGRESSION: `prune --retain-samples -5` has no meaning and deletes nothing."""
        self.seed()
        r = self.prune("--retain-samples", "-5")
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 8)

    def test_a_config_byte_budget_below_one_sample_is_not_a_purge(self):
        """REGRESSION: `retain_sample_bytes: 1` in config, refused by collect, is refused by prune."""
        self.seed()
        self.write_config({"retain_sample_bytes": 1})
        r = self.prune()
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 8)

    def test_a_whole_number_in_config_is_applied(self):
        """GUARD: `retain_samples: 3` makes prune keep three."""
        self.seed()
        self.write_config({"retain_samples": 3})
        r = self.prune()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 3)

    def test_a_flag_overrides_the_config(self):
        """GUARD: the command line wins over config.json."""
        self.seed()
        self.write_config({"retain_samples": 5000})
        r = self.prune("--retain-samples", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 2)

    def test_a_zero_flag_overrides_a_valid_config(self):
        """GUARD: the explicit purge works with a configuration present."""
        self.seed()
        self.write_config({"retain_samples": 5000})
        r = self.prune("--retain-samples", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 0)


# ================================== C2: configuration that is present but not loaded
class TestConfigThatDidNotLoadIsNotAbsent(CliCase):
    def test_a_dangling_symlink_is_not_an_absent_config(self):
        """REGRESSION: config.json pointing at a missing file is refused, not defaulted."""
        self.seed()
        os.symlink(os.path.join(self.tmp, "gone.json"), os.path.join(self.data, "config.json"))
        self.assertNotEqual(self.prune().returncode, 0)
        self.assertNotEqual(self.collect().returncode, 0)
        self.assertEqual(len(self.samples()), 8)

    def test_a_directory_is_not_an_absent_config(self):
        """REGRESSION: a directory named config.json is refused, not defaulted."""
        self.seed()
        os.mkdir(os.path.join(self.data, "config.json"))
        self.assertNotEqual(self.prune().returncode, 0)
        self.assertNotEqual(self.collect().returncode, 0)
        self.assertEqual(len(self.samples()), 8)

    def test_an_unknown_key_is_named_not_ignored(self):
        """REGRESSION: `retain_sample` (a typo) is refused and named in the message."""
        self.seed()
        self.write_config({"retain_sample": 5000})
        for r in (self.prune(), self.collect()):
            self.assertNotEqual(r.returncode, 0, r.stderr)
            self.assertIn("retain_sample", r.stderr)
        self.assertEqual(len(self.samples()), 8)

    def test_a_repeated_key_is_refused(self):
        """REGRESSION: two `retain_samples` members do not silently mean the second."""
        self.seed()
        self.write_config('{"retain_samples": 5000, "retain_samples": 2}')
        self.assertNotEqual(self.prune().returncode, 0)
        self.assertEqual(len(self.samples()), 8)

    def test_collect_refuses_a_malformed_config(self):
        """GUARD: `collect` exits 2 on a config.json it cannot parse, and writes nothing."""
        self.write_config('{"retain_samples": 3000,}')
        r = self.collect()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual((len(self.samples()), self.outcome_lines()), (0, 0))

    def test_status_still_runs_with_an_unknown_key(self):
        """GUARD: a read-only command warns about the configuration and carries on."""
        self.seed()
        self.write_config({"retain_sample": 5000})
        r = harness.run_cli(["status", "--transport", "mock", "--data-dir", self.data])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_documented_key_is_accepted(self):
        """GUARD: a configuration using the full set of keys is not refused as unknown."""
        self.write_config({
            "host": "192.0.2.1", "user": "u", "artifact": "/nonexistent", "ssh": "ssh",
            "known_hosts": "/nonexistent", "artifact_sha": "0" * 64,
            "known_hosts_sha": "0" * 64, "connect_timeout": 5, "run_bound": 5,
            "control_persist": 600, "watchdog_selftest": True, "watchdog_selftest_bound": 2,
            "interactive_auth": False, "max_stdout_bytes": 1 << 20, "max_stderr_bytes": 1 << 16,
            "retain_samples": 10, "retain_outcomes": 10, "retain_sample_bytes": 1 << 22,
            "retain_outcome_bytes": 1 << 20, "max_sample_bytes": 1 << 21,
            "open_after": 3, "resolve_after": 2, "interval": 60})
        r = self.collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.samples()), 1)


# ============================================ C6: what counts as opting out
class TestKnownHostsDigestOptOutIsExactlyFalse(TempCase):
    def configure(self, value):
        gone = os.path.join(self.tmp, "master.gone")
        cfg = harness.write_ssh_config(self.data, self.tmp, mode="ok",
                                       extra_env={"FAKE_SSH_MASTER_STATE": gone})
        good = cfg["known_hosts_sha"]
        cfg["known_hosts_sha"] = value(good) if callable(value) else value
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            json.dump(cfg, fh)

    def collect(self):
        return harness.run_cli(["collect", "--once", "--data-dir", self.data])

    def assert_refused_before_ssh(self, value):
        self.configure(value)
        r = self.collect()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.outcome_lines(), 0, "an attempt was made")

    def test_zero_is_not_an_opt_out(self):
        """REGRESSION: `known_hosts_sha: 0` does not switch the digest gate off."""
        self.assert_refused_before_ssh(0)

    def test_an_empty_list_is_not_an_opt_out(self):
        """REGRESSION: `known_hosts_sha: []` does not switch the digest gate off."""
        self.assert_refused_before_ssh([])

    def test_an_empty_object_is_not_an_opt_out(self):
        """REGRESSION: `known_hosts_sha: {}` does not switch the digest gate off."""
        self.assert_refused_before_ssh({})

    def test_a_value_that_is_not_a_digest_is_a_configuration_error(self):
        """REGRESSION: a string that cannot be a SHA-256 exits 2 before any attempt."""
        self.assert_refused_before_ssh("not-a-digest")

    def test_an_upper_case_digest_is_a_configuration_error(self):
        """REGRESSION: the digest is compared as lower-case hex; upper case is named, not mismatched."""
        self.assert_refused_before_ssh(lambda good: good.upper())

    def test_false_opts_out_and_says_so(self):
        """GUARD: `known_hosts_sha: false` collects and logs that the file is not hash-checked."""
        self.configure(False)
        r = self.collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NOT hash-checked", r.stderr)

    def test_the_correct_digest_collects_without_the_warning(self):
        """GUARD: the configured digest is accepted and no opt-out warning appears."""
        self.configure(lambda good: good)
        r = self.collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("NOT hash-checked", r.stderr)


# ==================================== C1: standalone prune and the state pointer
class TestStandalonePruneKeepsTheSampleStateNames(TempCase):
    def collected(self, step_back):
        clock, mono = [T0], [1000.0]
        store = Store(self.data, retain_samples=100, log=quiet)
        c = Collector(ScenarioTransport("success", log=quiet), store, log=quiet,
                      clock=lambda: clock[0], monotonic=lambda: mono[0])
        for _ in range(5):
            self.assertEqual(c.collect_once()["outcome"], OUTCOME_SUCCESS)
            clock[0] += 60
            mono[0] += 60
        if step_back:
            clock[0] -= DAY
        a = c.collect_once()
        self.assertEqual(a["outcome"], OUTCOME_SUCCESS, a["detail"])
        return a["sample_path"]

    def test_prune_after_a_backward_step_keeps_the_last_accepted_sample(self):
        """REGRESSION: prune to one keeps the sample state names, not the latest name."""
        newest = self.collected(step_back=True)
        Store(self.data, retain_samples=1, log=quiet).prune()
        self.assertTrue(os.path.exists(newest),
                        "prune deleted the last accepted sample; kept %r" % self.samples())
        self.assertEqual(len(self.samples()), 1)

    def test_prune_with_a_forward_clock_keeps_the_latest(self):
        """GUARD: without a clock step, prune to two keeps the two latest names."""
        newest = self.collected(step_back=False)
        before = self.samples()
        Store(self.data, retain_samples=2, log=quiet).prune()
        self.assertEqual(self.samples(), before[-2:])
        self.assertTrue(os.path.exists(newest))

    def test_prune_to_zero_removes_the_state_named_sample_too(self):
        """GUARD: an explicit purge is not blocked by the state pointer."""
        self.collected(step_back=True)
        Store(self.data, retain_samples=0, log=quiet).prune()
        self.assertEqual(self.samples(), [])


if __name__ == "__main__":
    unittest.main()
