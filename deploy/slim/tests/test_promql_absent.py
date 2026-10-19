"""absent() preserves equality selector labels, unlike aggregate expressions."""
import unittest
from promql_mini import Evaluator


class AbsentTests(unittest.TestCase):
    def test_missing_selector_retains_equality_labels(self):
        self.assertEqual(Evaluator({}, 1).evaluate('absent(up{job="router"}) or vector(3)'),
                         {(("job", "router"),): 1.0, (): 3.0})

    def test_regex_and_negative_matchers_do_not_become_labels(self):
        self.assertEqual(Evaluator({}, 1).evaluate(
            'absent(up{job="router",instance=~".*",other!="x"})'),
            {(("job", "router"),): 1.0})

    def test_duplicate_label_matchers_are_not_inferred(self):
        self.assertEqual(Evaluator({}, 1).evaluate('absent(up{job="router",job=~".*"})'), {(): 1.0})

    def test_aggregate_absence_has_no_inferred_labels(self):
        self.assertEqual(Evaluator({}, 1).evaluate('absent(min(up{job="router"}))'), {(): 1.0})

    def test_present_selector_returns_empty_and_escaped_labels_decode(self):
        self.assertEqual(Evaluator({'up': [({'job': 'router'}, 1)]}, 1).evaluate('absent(up{job="router"})'), {})
        self.assertEqual(Evaluator({}, 1).evaluate(r'absent(up{job="a\\b"})'), {(("job", r'a\b'),): 1.0})

    def test_invalid_duplicate_metric_name_is_rejected(self):
        with self.assertRaises(ValueError):
            Evaluator({}, 1).evaluate('absent(up{__name__="up"})')
