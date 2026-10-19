"""Run the backup shell against synthetic commands and disposable paths only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'usr/local/bin/slim-backup'


class BackupProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dest = self.root / 'archives'
        self.prom = self.root / 'metrics'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        source = SCRIPT.read_text().replace('DEST=/var/backups/slim', 'DEST="' + str(self.dest) + '"').replace(
            'PROM=/var/lib/prometheus/node-exporter/slim_backup.prom', 'PROM="' + str(self.prom) + '"')
        self.script = self.root / 'script'
        self.script.write_text(source)
        for name in ('chown', 'cp', 'pihole-FTL', 'pihole', 'grafana-server', 'prometheus', 'dpkg'):
            self.stub(name, '#!/bin/sh\nexit 0\n')
        self.stub('stat', '#!/bin/sh\necho 22\n')
        self.stub('date', '#!/bin/sh\necho 20261019-120000\n')
        self.stub('python3', '#!/bin/sh\nexit 0\n')  # SQLite reads contain no fixture data.
        self.stub('tar', '#!' + shutil.which('python3') + '\n' + """
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[sys.argv.index('-czf') + 1])
with path.open('wb') as stream:
    stream.write(b'synthetic private data')
    if '.part.' in path.name:
        pathlib.Path(os.environ['RECEIPT']).write_text(json.dumps({
            'file': path.stat().st_mode & 0o777, 'directory': path.parent.stat().st_mode & 0o777}))
        if os.environ.get('FAIL_ARCHIVE') == '1':
            sys.exit(7)
""")

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def run_backup(self, fail=False):
        env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                   RECEIPT=str(self.root / 'receipt'), FAIL_ARCHIVE='1' if fail else '0')
        return subprocess.run(['bash', str(self.script)], env=env, capture_output=True, text=True,
                              preexec_fn=lambda: os.umask(0o022))

    def test_partial_is_private_even_with_permissive_existing_destination(self):
        self.dest.mkdir(mode=0o755)
        # Predictable legacy partial cannot be reused or have its referent changed.
        sentinel = self.root / 'sentinel'
        sentinel.write_text('untouched')
        (self.dest / 'slim-20261019-120000.tar.gz.part').symlink_to(sentinel)
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / 'receipt').read_text()), {'file': 0o600, 'directory': 0o750})
        archives = list(self.dest.glob('slim-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o640)
        self.assertEqual(sentinel.read_text(), 'untouched')
        self.assertEqual(list(self.dest.glob('.slim-*.part.*')), [])

    def test_failure_removes_partial_and_does_not_publish_or_update_metrics(self):
        result = self.run_backup(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertFalse(self.prom.exists())

    def test_symlink_destination_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.dest.symlink_to(outside, target_is_directory=True)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
