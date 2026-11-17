"""Exercise the real launcher against isolated, checksum-bound acceptance files.

Historical GA evidence is verified in its original checkout. A changing target
branch must not rewrite that evidence merely to make a unit test pass.
"""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import yaml

from upgrade.product import launcher

ROOT = Path(__file__).resolve().parents[2]


class LauncherContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root/'upgrade/v1.35.10'
        self.directory.mkdir(parents=True)
        self.matrix = self.directory/'test-matrix.yaml'
        self.data = yaml.safe_load((ROOT/'upgrade/v1.35.10/test-matrix.yaml').read_text())
        self.data['meta'].update(execution_enabled=True, runner='fixture-product-entry')
        source = self.root/'upgrade/official.txt'
        source.write_text('isolated official statement fixture\n')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.write('upgrade/catalog.yaml', {'components': [{'id': 'product'}]})
        self.write('upgrade/official-sources.yaml', {'sources': [{
            'id': 'FIXTURE', 'facts': ['fixture support statement'],
            'obtained_content': {'local_copy': {'path': 'upgrade/official.txt',
                'sha256': digest, 'bytes': source.stat().st_size}}}]})
        self.write('upgrade/v1.35.10/release.yaml', {
            'source_kubernetes': 'v1.34.13', 'target_kubernetes': 'v1.35.10',
            'review': {'release_frozen': True}, 'component_decisions': [{
                'component_id': 'product', 'decision': 'keep', 'rationale': 'isolated fixture',
                'target_pins': {'product': '1.0.0'}, 'source_ids': ['FIXTURE'],
                'transition_edges': [{'edge_id': 'platform', 'source': 'source fixture',
                    'target': 'target fixture', 'product_entry': 'fixture product CLI',
                    'irreversible_point': 'no mutation', 'recovery': 'no mutation',
                    'cleanup': 'temporary directory', 'preconditions': ['healthy fixture'],
                    'steps': ['admit'], 'success_conditions': ['admitted'],
                    'official_source_ids': ['FIXTURE'], 'depends_on': ['UPG-01']}]}]})
        for case in self.data['cases']:
            case.update(status='pending', evidence=None)
            if case['id'] in ['UPG-01', 'UPG-02']:
                case.update(status='pass', subcases=['isolated-boundary'],
                            evidence='upgrade/v1.35.10/'+case['id']+'.json')
                receipt = {'schema_version': 1, 'accepted': True, 'source': 'v1.34.13',
                    'target': 'v1.35.10', 'case_ids': [case['id']], 'method': 'static',
                    'claim': 'Isolated launcher contract fixture; no project acceptance',
                    'covered_subcases': ['isolated-boundary'],
                    'inputs': {'upgrade/official.txt': digest},
                    'references': [{'path': 'upgrade/official.txt', 'sha256': digest}],
                    'checks': [{'command': 'fixture check', 'rc': 0}]}
                (self.root/case['evidence']).write_text(json.dumps(receipt))
        self.save()
        override = patch.object(launcher, 'ROOT', self.root)
        override.start()
        self.addCleanup(override.stop)

    def write(self, path, value):
        (self.root/path).write_text(yaml.safe_dump(value))

    def save(self):
        self.matrix.write_text(yaml.safe_dump(self.data))

    def test_pending_prerequisite_refuses_before_runner(self):
        calls = []
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, 'UPG-07'):
            launcher.run(self.matrix, required=['UPG-01','UPG-02','UPG-07'],
                         runner=lambda: calls.append('mutated'))
        self.assertEqual(calls, [])

    def test_static_admission_accepts_frozen_combination_without_running_runner(self):
        snapshot = launcher.admit_static(self.matrix, required=['UPG-01','UPG-02'])
        self.assertEqual((snapshot['pass'], snapshot['total']), (2, 15))

    def test_disabled_execution_refuses_after_matrix_prerequisite_check(self):
        self.data['meta']['execution_enabled'] = False
        self.save()
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, 'execution remains disabled'):
            launcher.run(self.matrix, required=['UPG-01','UPG-02'], runner=lambda: self.fail('mutation'))

    def test_stale_evidence_refuses_before_runner(self):
        (self.root/'upgrade/official.txt').write_text('changed fixture')
        with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, 'missing or stale'):
            launcher.run(self.matrix, required=['UPG-01'], runner=lambda: self.fail('mutation'))

    def test_admitted_current_inputs_invoke_the_real_callback_once(self):
        calls = []
        result = launcher.run(self.matrix, required=['UPG-01','UPG-02'],
                              runner=lambda: calls.append('product') or 'complete')
        self.assertEqual((result, calls), ('complete', ['product']))
