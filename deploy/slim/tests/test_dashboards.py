"""Static checks for the provisioned Grafana dashboards (no Grafana or Prometheus needed)."""
import json
from pathlib import Path
import re
import unittest


DASH = Path(__file__).resolve().parents[1] / 'var' / 'lib' / 'grafana' / 'dashboards'
EXPECTED = {
    'pihole.json': ('pihole-slim', 'Pi-hole'),
    'router.json': ('router-asus', 'Router (ASUS RT-AX56U)'),
    'slim-overview.json': ('slim-overview', 'Slim overview'),
}
# The Router collector state expression must be identical wherever it appears.
STATES = ('OK', 'FAILING', 'STALE', 'UNAVAILABLE')
PRIVATE_ADDR = re.compile(r'\b(?:192\.168\.|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.)')


def load(name):
    return json.loads((DASH / name).read_text())


def panels(dash):
    for panel in dash['panels']:
        yield panel
        yield from panel.get('panels', [])


class DashboardTests(unittest.TestCase):
    def test_expected_files_and_identities_are_stable(self):
        self.assertEqual({p.name for p in DASH.glob('*.json')}, set(EXPECTED))
        for name, (uid, title) in EXPECTED.items():
            with self.subTest(name=name):
                dash = load(name)
                self.assertEqual((dash['uid'], dash['title']), (uid, title))
                self.assertIn('home-lab', dash['tags'])
                self.assertEqual(dash['links'][0]['type'], 'dashboards')

    def test_panels_are_well_formed(self):
        for name in EXPECTED:
            dash = load(name)
            ids = [p['id'] for p in panels(dash)]
            with self.subTest(name=name):
                self.assertEqual(len(ids), len(set(ids)), 'duplicate panel ids')
                for panel in panels(dash):
                    grid = panel['gridPos']
                    self.assertLessEqual(grid['x'] + grid['w'], 24, panel['title'])
                    for target in panel['targets']:
                        self.assertTrue(target['expr'].strip(), panel['title'])
                        self.assertEqual(target.get('datasource', panel.get('datasource'))['uid'], 'prometheus')
                        self.assertEqual(target['expr'].count('('), target['expr'].count(')'), panel['title'])
                        self.assertEqual(target['expr'].count('{'), target['expr'].count('}'), panel['title'])

    def test_status_tiles_cannot_carry_old_values_forward(self):
        for name in EXPECTED:
            for panel in panels(load(name)):
                if panel['type'] != 'stat':
                    continue
                with self.subTest(dashboard=name, panel=panel['title']):
                    defaults = panel['fieldConfig']['defaults']
                    self.assertEqual(defaults.get('noValue'), 'NO DATA')
                    self.assertTrue(any(m.get('type') == 'special' for m in defaults['mappings']))
                    self.assertEqual(panel['options']['graphMode'], 'none')
                    for target in panel['targets']:
                        self.assertTrue(target.get('instant'), 'status tiles must use instant queries')

    def test_time_series_show_gaps_instead_of_bridging_outages(self):
        for name in EXPECTED:
            for panel in panels(load(name)):
                if panel['type'] == 'timeseries':
                    with self.subTest(dashboard=name, panel=panel['title']):
                        self.assertIs(panel['fieldConfig']['defaults']['custom']['spanNulls'], False)

    def test_router_collector_reports_four_distinct_states(self):
        tiles = []
        for name in ('router.json', 'slim-overview.json'):
            tiles += [p for p in panels(load(name)) if p['title'] in ('Collector', 'Router collector')]
        self.assertEqual(len(tiles), 2)
        self.assertEqual(tiles[0]['targets'][0]['expr'], tiles[1]['targets'][0]['expr'])
        expr = tiles[0]['targets'][0]['expr']
        for needle in ('up{job="router"}', 'rtax_exporter_up', 'rtax_last_attempt_timestamp_seconds',
                       'rtax_last_attempt_success', 'rtax_incident_open'):
            self.assertIn(needle, expr)
        self.assertTrue(expr.rstrip().endswith('or vector(3)'), 'absent data must report UNAVAILABLE')
        for tile in tiles:
            mapping = tile['fieldConfig']['defaults']['mappings'][0]['options']
            self.assertEqual([mapping[str(i)]['text'] for i in range(4)], list(STATES))

    def test_cpu_tile_and_chart_use_the_same_basis(self):
        dash = load('slim-overview.json')
        by_title = {p['title']: p for p in dash['panels']}
        tile = by_title['CPU']['targets'][0]['expr']
        chart = by_title['CPU by mode']['targets'][0]['expr']
        for expr in (tile, chart):
            self.assertIn('sum(rate(node_cpu_seconds_total{job="node"}[2m]))', expr)
        self.assertNotIn('avg(', tile)

    def test_no_private_household_addresses_are_published(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                self.assertIsNone(PRIVATE_ADDR.search((DASH / name).read_text()))


if __name__ == '__main__':
    unittest.main()
