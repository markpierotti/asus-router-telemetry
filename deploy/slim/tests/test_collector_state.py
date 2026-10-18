"""Behavioral tests for the Router collector state expression used by both dashboards.

The expression is evaluated by the small evaluator in promql_mini.py against synthetic
samples. States: 0 OK, 1 FAILING, 2 STALE, 3 UNAVAILABLE. The evaluator's results were
also cross-checked against a real Prometheus engine for these scenarios (see
docs/slim-dashboards-20261019.md); this suite keeps the logic from regressing offline.
"""
import json
from pathlib import Path
import unittest

from promql_mini import Evaluator

DASH = Path(__file__).resolve().parents[1] / 'var' / 'lib' / 'grafana' / 'dashboards'
NOW = 1_800_000_000
OK, FAILING, STALE, UNAVAILABLE = 0, 1, 2, 3
NAMES = {OK: 'OK', FAILING: 'FAILING', STALE: 'STALE', UNAVAILABLE: 'UNAVAILABLE'}

METRICS = ('up', 'rtax_exporter_up', 'rtax_last_attempt_timestamp_seconds',
           'rtax_last_attempt_success', 'rtax_incident_open')


def expressions():
    found = {}
    for name in ('router.json', 'slim-overview.json'):
        for panel in json.loads((DASH / name).read_text())['panels']:
            if panel['title'] in ('Collector', 'Router collector'):
                found[name] = panel['targets'][0]['expr']
    return found


def samples(**overrides):
    """Healthy baseline; a value of None removes that metric entirely."""
    base = {
        'up': 1,
        'rtax_exporter_up': 1,
        'rtax_last_attempt_timestamp_seconds': NOW - 30,
        'rtax_last_attempt_success': 1,
        'rtax_incident_open': 0,
    }
    base.update(overrides)
    out = {}
    for metric, value in base.items():
        if value is None:
            continue
        labels = {'job': 'router', 'instance': 'collector'} if metric == 'up' else {'instance': 'collector'}
        out[metric] = [(labels, value)]
    # An unrelated job's `up` must never influence the router state.
    out.setdefault('up', []).append(({'job': 'node', 'instance': 'x'}, 1))
    return out


class CollectorStateTests(unittest.TestCase):
    def state(self, expr, **overrides):
        result = Evaluator(samples(**overrides), NOW).evaluate(expr)
        self.assertEqual(len(result), 1, f'expected exactly one series, got {result}')
        value = next(iter(result.values()))
        return int(value)

    def check(self, expected, **overrides):
        for name, expr in expressions().items():
            with self.subTest(dashboard=name, overrides=overrides):
                got = self.state(expr, **overrides)
                self.assertEqual(NAMES[got], NAMES[expected])

    def test_both_dashboards_use_the_same_expression(self):
        exprs = expressions()
        self.assertEqual(set(exprs), {'router.json', 'slim-overview.json'})
        self.assertEqual(len(set(exprs.values())), 1)

    def test_healthy_is_ok(self):
        self.check(OK)

    def test_each_required_metric_missing_is_never_ok(self):
        for metric in METRICS:
            # Only the router job's own `up` counts; a missing router `up` stays missing.
            overrides = {metric: None}
            for name, expr in expressions().items():
                with self.subTest(dashboard=name, missing=metric):
                    got = self.state(expr, **overrides)
                    self.assertNotEqual(got, OK, f'{metric} missing fell through to OK')

    def test_each_required_metric_missing_gives_unavailable(self):
        for metric in METRICS:
            self.check(UNAVAILABLE, **{metric: None})

    def test_everything_missing_is_unavailable(self):
        self.check(UNAVAILABLE, **{m: None for m in METRICS})

    def test_scrape_target_down(self):
        self.check(UNAVAILABLE, up=0)

    def test_exporter_down(self):
        self.check(UNAVAILABLE, rtax_exporter_up=0)

    def test_stale_attempt(self):
        self.check(STALE, rtax_last_attempt_timestamp_seconds=NOW - 301)
        self.check(STALE, rtax_last_attempt_timestamp_seconds=NOW - 3600)

    def test_stale_boundary_is_five_minutes(self):
        self.check(OK, rtax_last_attempt_timestamp_seconds=NOW - 300)

    def test_failed_attempt(self):
        self.check(FAILING, rtax_last_attempt_success=0)

    def test_open_incident(self):
        self.check(FAILING, rtax_incident_open=1)

    def test_recovery_returns_to_ok(self):
        self.check(FAILING, rtax_last_attempt_success=0, rtax_incident_open=1)
        self.check(OK, rtax_last_attempt_success=1, rtax_incident_open=0)

    def test_precedence_unavailable_over_stale_and_failing(self):
        stale_failing = dict(rtax_last_attempt_timestamp_seconds=NOW - 900,
                             rtax_last_attempt_success=0, rtax_incident_open=1)
        self.check(UNAVAILABLE, up=0, **stale_failing)
        self.check(UNAVAILABLE, rtax_exporter_up=0, **stale_failing)
        self.check(UNAVAILABLE, up=None, **stale_failing)
        self.check(UNAVAILABLE, rtax_exporter_up=None, **stale_failing)

    def test_precedence_stale_over_failing(self):
        self.check(STALE, rtax_last_attempt_timestamp_seconds=NOW - 900,
                   rtax_last_attempt_success=0, rtax_incident_open=1)

    def test_precedence_failing_over_ok(self):
        self.check(FAILING, rtax_last_attempt_success=0, rtax_incident_open=0)
        self.check(FAILING, rtax_last_attempt_success=1, rtax_incident_open=1)

    def test_missing_freshness_with_failure_reports_failing_not_ok(self):
        self.check(FAILING, rtax_last_attempt_timestamp_seconds=None, rtax_last_attempt_success=0)
        self.check(FAILING, rtax_last_attempt_timestamp_seconds=None, rtax_incident_open=1)

    def test_missing_success_or_incident_with_a_failure_signal_reports_failing(self):
        self.check(FAILING, rtax_last_attempt_success=None, rtax_incident_open=1)
        self.check(FAILING, rtax_incident_open=None, rtax_last_attempt_success=0)

    def test_missing_input_without_failure_signal_is_unavailable_not_failing(self):
        self.check(UNAVAILABLE, rtax_last_attempt_timestamp_seconds=None)
        self.check(UNAVAILABLE, rtax_last_attempt_success=None)
        self.check(UNAVAILABLE, rtax_incident_open=None)


if __name__ == '__main__':
    unittest.main()
