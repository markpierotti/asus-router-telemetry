"""Execute the runbook's actual publisher against disposable dashboard files."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

RUNBOOK = Path(__file__).resolve().parents[3] / 'docs/slim-operations.md'


class PublishRunbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.target = self.root / 'source.json', self.root / 'router.json'
        self.reference = self.root / 'pihole.json'
        self.source.write_text('{"target":"192.0.2.20"}\n')
        self.target.write_text('{"old":true}\n')
        self.reference.write_text('{}\n')
        self.reference.chmod(0o640)
        self.code = re.search(r"publish_dashboard\(\).*?<<'PY'\n(.*?)\nPY", RUNBOOK.read_text(), re.S).group(1)

    def publish(self, address=''):
        return subprocess.run([sys.executable, '-', str(self.source), str(self.target),
                               str(self.reference), address], input=self.code,
                              capture_output=True, text=True)

    def test_atomic_publish_substitutes_address_and_preserves_reference_permissions(self):
        with self.target.open() as old_reader:
            result = self.publish('192.0.2.99')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.load(old_reader), {'old': True})
        self.assertEqual(json.loads(self.target.read_text()), {'target': '192.0.2.99'})
        st, reference = self.target.stat(), self.reference.stat()
        self.assertEqual((st.st_uid, st.st_gid, st.st_mode & 0o777),
                         (reference.st_uid, reference.st_gid, 0o640))
        self.assertEqual(list(self.root.glob('.dashboard-*')), [])

    def test_invalid_json_leaves_live_file_untouched(self):
        self.source.write_text('{unfinished')
        self.assertNotEqual(self.publish().returncode, 0)
        self.assertEqual(json.loads(self.target.read_text()), {'old': True})
        self.assertEqual(list(self.root.glob('.dashboard-*')), [])

    def test_invalid_address_leaves_live_file_untouched(self):
        self.assertNotEqual(self.publish('192.0.2.99/extra').returncode, 0)
        self.assertEqual(json.loads(self.target.read_text()), {'old': True})


if __name__ == '__main__':
    unittest.main()
