"""Exercise recovered exporters against synthetic state; no host services required."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / 'usr/local/bin' / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ExporterTests(unittest.TestCase):
    def test_pihole_reads_synthetic_v6_schema_without_changing_databases(self):
        mod = load('pihole-exporter')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mod.FTL_DB = str(root / 'ftl.db')
            mod.GRAVITY_DB = str(root / 'gravity.db')
            mod.NAMES_FILE = str(root / 'names')
            (root / 'names').write_text('192.0.2.30 synthetic-client\n')
            with sqlite3.connect(mod.FTL_DB) as db:
                db.execute('CREATE TABLE query_storage (id INTEGER, timestamp INTEGER, status INTEGER, client TEXT, domain TEXT, forward TEXT, reply_time REAL)')
                db.execute('CREATE VIEW queries AS SELECT * FROM query_storage')
                db.executemany('INSERT INTO query_storage VALUES (?,?,?,?,?,?,?)', [
                    (1, int(time.time()), 1, '192.0.2.30', 'blocked.example', None, 0),
                    (2, int(time.time()), 2, '192.0.2.30', 'allowed.example', '192.0.2.53', 0.05),
                ])
            with sqlite3.connect(mod.GRAVITY_DB) as db:
                db.executescript('CREATE TABLE gravity (domain TEXT); INSERT INTO gravity VALUES ("blocked.example"); CREATE TABLE adlist (enabled INTEGER); INSERT INTO adlist VALUES (1); CREATE TABLE info (property TEXT, value INTEGER); INSERT INTO info VALUES ("updated", 1700000000);')
            before = {p: p.read_bytes() for p in (root / 'ftl.db', root / 'gravity.db')}
            text = mod.collect()
            self.assertIn('pihole_exporter_up 1', text)
            self.assertIn('pihole_queries_24h 2', text)
            self.assertIn('pihole_blocked_24h 1', text)
            self.assertIn('name="synthetic-client"', text)
            self.assertIn('domain="blocked.example"', text)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_pihole_missing_databases_report_failure_without_creating_them(self):
        mod = load('pihole-exporter')
        with tempfile.TemporaryDirectory() as tmp:
            mod.FTL_DB = str(Path(tmp) / 'missing-ftl.db')
            mod.GRAVITY_DB = str(Path(tmp) / 'missing-gravity.db')
            self.assertIn('pihole_exporter_up 0', mod.collect())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_router_metrics_follow_state_pointer_and_count_recent_outcomes(self):
        mod = load('rtax-exporter')
        with tempfile.TemporaryDirectory() as tmp:
            mod.DATA = tmp
            root = Path(tmp)
            at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            sample = root / 'accepted.json'
            sample.write_text(json.dumps({
                'collected_at': at, 'collector_version': '1.6.0',
                'reporter': {'version': '1.0.5', 'host': {'uptime_seconds': 600, 'boot_time': 1700000000},
                             'memory_kb': {'MemAvailable': 100, 'MemTotal': 200}},
                'derived': {'cpu_utilisation': {'aggregate': {'state': 'ok', 'util_percent': 2.5}}},
            }))
            (root / 'state.json').write_text(json.dumps({
                'last_attempt': {'at': at, 'outcome': 'success', 'duration_s': 0.2, 'cleanup_ok': True},
                'last_valid_sample': {'path': str(sample)}, 'consecutive_failures': 0, 'incident': None,
            }))
            (root / 'outcomes.jsonl').write_text(json.dumps({'at': at, 'outcome': 'success'}) + '\n')
            before = {p: p.read_bytes() for p in root.iterdir()}
            text = mod.collect()
            self.assertIn('rtax_exporter_up 1', text)
            self.assertIn('rtax_last_attempt_success 1', text)
            self.assertIn('rtax_attempts_24h{outcome="success"} 1', text)
            self.assertIn('rtax_router_cpu_util_percent{cpu="all"} 2.5', text)
            self.assertIn('rtax_router_memory_bytes{type="MemAvailable"} 102400', text)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

    def test_router_missing_state_reports_failure_without_creating_data(self):
        mod = load('rtax-exporter')
        with tempfile.TemporaryDirectory() as tmp:
            mod.DATA = tmp
            self.assertIn('rtax_exporter_up 0', mod.collect())
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
