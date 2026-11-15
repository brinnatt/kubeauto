"""A freeze flag cannot authorize an incomplete or stale upgrade combination."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('upgrade_validate', ROOT / 'upgrade/validate.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class CombinationPreflightContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'upgrade/v1.34.13'
        self.directory.mkdir(parents=True)
        self.meta = {'source': 'v1.33.6', 'target': 'v1.34.13'}
        self.official_path = self.root / 'upgrade/official.txt'
        self.official_path.write_text('fixed official platform support statement\n')
        self.write('upgrade/catalog.yaml', {'components': [{'id': 'controller'}]})
        self.sources = {'sources': [{
            'id': 'OFFICIAL', 'facts': ['Supported source and target platforms'],
            'obtained_content': {'local_copy': {
                'path': 'upgrade/official.txt', 'bytes': self.official_path.stat().st_size,
                'sha256': hashlib.sha256(self.official_path.read_bytes()).hexdigest()}}}]}
        self.write('upgrade/official-sources.yaml', self.sources)
        self.component = {
            'component_id': 'controller', 'decision': 'keep',
            'rationale': 'Exact supported version unchanged',
            'target_pins': {'operator': '3.5.0'}, 'source_ids': ['OFFICIAL'],
            'transition_edges': [{
                'edge_id': 'platform', 'source': '3.5.0 on 1.33.6', 'target': '3.5.0 on 1.34.13',
                'bridge': None, 'depends_on': ['UPG-06', 'UPG-07'],
                'official_source_ids': ['OFFICIAL'], 'product_entry': 'config.yml + kubecli upgrade',
                'preconditions': ['Healthy source and identity'], 'steps': ['Preserve the controller'],
                'success_conditions': ['Data and reconciliation verified'],
                'irreversible_point': 'No engine format change',
                'recovery': 'Platform recovery under UPG-12', 'cleanup': 'Owned probes only'}]}
        self.release = {
            'source_kubernetes': self.meta['source'], 'target_kubernetes': self.meta['target'],
            'review': {'release_frozen': True}, 'component_decisions': [self.component]}

    def write(self, name, value):
        (self.root / name).write_text(yaml.safe_dump(value))

    def check(self):
        self.write('upgrade/v1.34.13/release.yaml', self.release)
        return gate.combination_errors(self.root, self.directory, self.meta)

    def test_complete_unchanged_component_retains_its_platform_edge(self):
        before = self.official_path.read_bytes()
        self.assertEqual(self.check(), [])
        self.assertEqual(self.official_path.read_bytes(), before)
        self.assertTrue(self.release['review']['release_frozen'])

    def test_real_cli_refuses_frozen_boolean_with_investigate_decision(self):
        self.component['decision'] = 'investigate'
        self.check()
        matrix = gate.read_mapping(ROOT / 'upgrade/v1.34.13/test-matrix.yaml')
        for case in matrix['cases']:
            case.update(status='pending', evidence='')
        self.write('upgrade/v1.34.13/test-matrix.yaml', matrix)
        output = io.StringIO()
        with patch.object(gate, 'ROOT', self.root), redirect_stdout(output):
            rc = gate.main([str(self.directory / 'test-matrix.yaml'), '--combination-preflight'])
        self.assertEqual(rc, 1)
        self.assertIn('controller: unresolved component decision', output.getvalue())
        self.assertNotIn('UPGRADE_MATRIX_VALIDATION_PASS', output.getvalue())

    def test_catalog_omission_and_duplicate_cannot_hide_a_supported_component(self):
        for components in ([], [deepcopy(self.component), deepcopy(self.component)]):
            with self.subTest(components=len(components)):
                self.release['component_decisions'] = components
                self.assertTrue(any('exact catalog' in e for e in self.check()))

    def test_blank_or_floating_target_is_not_an_implicit_source_inheritance(self):
        for pins in (None, {}, {'operator': ''}, {'image': 'operator:latest'},
                     {'operator': 'stable'}, {'operator': 3.5}, {'operator': 'v1.34.0-rc.1'}):
            with self.subTest(pins=pins):
                self.component['target_pins'] = pins
                self.assertTrue(any('immutable target' in e for e in self.check()))

    def test_stale_or_unread_official_source_refuses_freeze(self):
        self.official_path.write_text('changed upstream page\n')
        self.assertTrue(any('missing or stale' in e for e in self.check()))
        self.component['source_ids'] = ['UNREAD']
        self.assertTrue(any('official source not obtained' in e for e in self.check()))

    def test_nested_image_and_chart_pins_are_checked_to_every_leaf(self):
        self.component['target_pins'] = {
            'operator': '3.5.0', 'images': {'driver': 'v3.17.2'},
            'vendored_charts_sha256': {'roles/storage/chart.tgz': 'a' * 64}}
        self.assertEqual(self.check(), [])
        for pin in ['latest', 'driver:stable', 'v3.18.0-rc.1', {}, [], None, 3.17]:
            with self.subTest(pin=pin):
                self.component['target_pins']['images']['driver'] = pin
                self.assertTrue(any('immutable target' in e for e in self.check()))

    def test_multi_page_observation_checks_every_copy_including_mixed_records(self):
        source = self.sources['sources'][0]
        first = source.pop('obtained_content')
        second_path = self.root / 'upgrade/second.txt'
        second_path.write_text('fixed official operational restriction\n')
        second = {'local_copy': {'path': 'upgrade/second.txt',
            'bytes': second_path.stat().st_size,
            'sha256': hashlib.sha256(second_path.read_bytes()).hexdigest()}}
        source['obtained_contents'] = [first, second]
        self.write('upgrade/official-sources.yaml', self.sources)
        self.assertEqual(self.check(), [])
        second_path.write_text('stale\n')
        self.assertTrue(any('missing or stale' in e for e in self.check()))
        source['obtained_content'] = first
        self.write('upgrade/official-sources.yaml', self.sources)
        self.assertTrue(any('missing or stale' in e for e in self.check()))
        for invalid in [[], {}, None, [first, None], [first, {'local_copy': None}]]:
            source['obtained_contents'] = invalid
            self.write('upgrade/official-sources.yaml', self.sources)
            with self.subTest(invalid=invalid):
                self.assertTrue(self.check())

    def test_keep_still_requires_migration_recovery_and_bridge_cannot_be_implicit(self):
        original = deepcopy(self.component['transition_edges'])
        self.component['transition_edges'] = []
        self.assertTrue(any('platform transition' in e for e in self.check()))
        self.component['transition_edges'] = original
        self.component['transition_edges'][0]['recovery'] = None
        self.assertTrue(any('recovery required' in e for e in self.check()))
        self.component['decision'] = 'bridge'
        self.assertTrue(any('explicit bridge' in e for e in self.check()))

    def test_other_release_and_unbound_migration_sources_are_refused(self):
        self.release['target_kubernetes'] = 'v1.35.10'
        self.component['transition_edges'][0]['official_source_ids'] = ['UNREVIEWED']
        errors = self.check()
        self.assertIn('release endpoints differ from matrix', errors)
        self.assertTrue(any('outside component references' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
