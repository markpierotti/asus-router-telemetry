"""Run the actual Mac pull script with synthetic SSH; no server access."""
import base64
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'macos/pull-slim-backups'
VALID = 'slim-20261017-215821.tar.gz'
OTHER = 'slim-20261017-220000.tar.gz'


class BackupPullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.dest = self.home / 'Backups/slim'
        self.dest.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.payload = gzip.compress(b'synthetic backup contents')
        ssh = self.bin / 'ssh'
        ssh.write_text('#!' + sys.executable + '\n' + '''import base64,json,os,sys
if sys.argv[-1] == 'list':
    print(os.environ['FAKE_BACKUP_LIST'])
elif sys.argv[-2] == 'get':
    with open(os.environ['FAKE_CALLS'], 'a') as f:
        f.write(json.dumps(sys.argv[-1]) + '\\n')
    if sys.argv[-1] == os.environ.get('FAKE_BAD_GZIP'):
        sys.stdout.buffer.write(b'not gzip')
    else:
        sys.stdout.buffer.write(base64.b64decode(os.environ['FAKE_PAYLOAD']))
else:
    raise SystemExit(2)
''')
        ssh.chmod(0o755)

    def run_pull(self, listing, **extra):
        env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                   FAKE_BACKUP_LIST=listing, FAKE_CALLS=str(self.calls),
                   FAKE_PAYLOAD=base64.b64encode(self.payload).decode(), **extra)
        result = subprocess.run(['/bin/sh', str(SCRIPT)], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def requested(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_invalid_raw_names_are_rejected_before_paths_or_downloads(self):
        # An unquoted for-loop would expand this malicious wildcard into VALID.
        (self.root / VALID).write_bytes(b'glob decoy')
        outside = self.root / 'sentinel.part'
        outside.write_bytes(b'preserve outside staging')
        inside = self.home / 'sentinel.part'
        inside.write_bytes(b'preserve inside staging')
        invalid = ['slim-*.tar.gz', '../sentinel', './../../sentinel', '/../../sentinel',
                   'slim-2026101-215821.tar.gz', 'slim-20261017-21582.tar.gz',
                   VALID + '.extra', 'prefix-' + VALID, ' ' + VALID, VALID + ' ',
                   VALID + '\r', '\t' + VALID, 'slim-２０２６１０１７-215821.tar.gz',
                   '%2e%2e/sentinel', '..\\sentinel', '-option', '',
                   'slim-20261017-215821XtarYgz']
        result = self.run_pull('\n'.join(invalid + [OTHER]))
        self.assertEqual(self.requested(), [OTHER])
        self.assertEqual(outside.read_bytes(), b'preserve outside staging')
        self.assertEqual(inside.read_bytes(), b'preserve inside staging')
        self.assertEqual((self.dest / OTHER).read_bytes(), self.payload)
        self.assertIn('rejected invalid backup name', result.stderr)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), [OTHER])

    def test_valid_downloads_work_and_existing_archives_are_skipped(self):
        (self.dest / VALID).write_bytes(b'existing archive')
        self.run_pull(VALID + '\n' + OTHER)
        self.assertEqual(self.requested(), [OTHER])
        self.assertEqual((self.dest / VALID).read_bytes(), b'existing archive')
        self.assertEqual(gzip.decompress((self.dest / OTHER).read_bytes()), b'synthetic backup contents')
        self.assertEqual((self.dest / OTHER).stat().st_mode & 0o777, 0o600)

    def test_bad_gzip_is_removed_and_retention_still_keeps_thirty(self):
        for i in range(31):
            path = self.dest / ('slim-20261016-%06d.tar.gz' % i)
            path.write_bytes(self.payload)
            os.utime(path, (1700000000 + i, 1700000000 + i))
        self.run_pull(VALID, FAKE_BAD_GZIP=VALID)
        self.assertEqual(self.requested(), [VALID])
        self.assertFalse((self.dest / VALID).exists())
        self.assertFalse((self.dest / ('.' + VALID + '.part')).exists())
        self.assertEqual(len(list(self.dest.glob('slim-*.tar.gz'))), 30)
        self.assertFalse((self.dest / 'slim-20261016-000000.tar.gz').exists())
