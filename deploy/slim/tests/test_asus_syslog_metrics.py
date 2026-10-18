"""ASUS syslog metrics: counters, rotation and the textfile output."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'usr/local/bin/asus-syslog-metrics'
loader = importlib.machinery.SourceFileLoader('asus_syslog_metrics', str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


class AsusSyslogMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / 'asus.log'
        self.prom = root / 'asus_syslog.prom'
        self.patches = [patch.object(m, 'LOG', self.log), patch.object(m, 'STATE', root / 'state.json'),
                        patch.object(m, 'PROM', self.prom), patch.object(m, 'SLIM_IP', '192.0.2.10')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, *msgs, path=None):
        with (path or self.log).open('a') as f:
            for msg in msgs:
                f.write(f'2026-10-18T14:00:00+10:00 {msg}\n')

    def metrics(self):
        m.main()
        return {l.split()[0]: float(l.split()[1]) for l in self.prom.read_text().splitlines() if not l.startswith('#')}

    def test_counts_accumulate_across_runs_and_rotation(self):
        self.write('kernel: klogd started: BusyBox',
                   "dropbear[1]: Bad password attempt for 'admin' from 192.0.2.99:1",
                   "dropbear[2]: Pubkey auth succeeded for 'admin' with key from 192.0.2.10:2",
                   "dropbear[3]: Password auth succeeded for 'admin' from 192.0.2.99:3",
                   'wl0: wlc_ucode_download: firmware error, watchdog reset')
        first = self.metrics()
        self.assertEqual(first['asus_syslog_lines_total'], 5)
        self.assertEqual(first['asus_syslog_restart_total'], 1)
        self.assertEqual(first['asus_syslog_auth_failure_total'], 1)
        self.assertEqual(first['asus_syslog_login_other_total'], 1)
        self.assertEqual(first['asus_syslog_wifi_driver_error_total'], 1)
        self.write("dropbear[4]: Bad password attempt for 'admin' from 192.0.2.99:4")
        os.rename(self.log, self.log.with_name('asus.log.1'))
        self.write('kernel: klogd started: BusyBox')
        second = self.metrics()
        self.assertEqual(second['asus_syslog_lines_total'], 7)
        self.assertEqual(second['asus_syslog_auth_failure_total'], 2)
        self.assertEqual(second['asus_syslog_restart_total'], 2)
        self.assertGreater(second['asus_syslog_last_line_timestamp_seconds'], 0)

    def test_missing_log_still_writes_metrics(self):
        out = self.metrics()
        self.assertEqual(out['asus_syslog_lines_total'], 0)


if __name__ == '__main__':
    unittest.main()
