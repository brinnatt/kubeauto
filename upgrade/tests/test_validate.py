"""Deterministic refusal contracts; no SSH, registries or lab mutation."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('upgrade_validate', ROOT / 'upgrade/validate.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class UpgradeEvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root / 'upgrade/v1.34.13'
        self.release.mkdir(parents=True)
        self.matrix_path = self.release / 'test-matrix.yaml'
        (self.release / 'release.yaml').write_text('review:\n  release_frozen: false\n')
        self.data = gate.read_mapping(ROOT / 'upgrade/v1.34.13/test-matrix.yaml')
        for c in self.data['cases']:
            c.update(status='pending', evidence='', subcases=[], subcase_expansion_complete=False)
        self.input = self.root / 'product.conf'
        self.input.write_text('unchanged production input\n')
        self.reference = self.root / 'upgrade/old-record.json'
        self.reference.write_text('{"historical_claim": "original scope"}\n')
        self.receipt = self.release / 'baseline.json'

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def accepted_baseline(self):
        case = self.data['cases'][0]
        case.update(status='pass', evidence='upgrade/v1.34.13/baseline.json',
                    subcases=['exact-source-identity'], subcase_expansion_complete=True)
        receipt = {'schema_version': 1, 'accepted': True, 'source': 'v1.33.6',
                   'target': 'v1.34.13', 'case_ids': ['UPG-01'], 'method': 'analysis',
                   'claim': 'Exact source identity for the scoped lab migration',
                   'covered_subcases': ['exact-source-identity'],
                   'inputs': {'product.conf': self.digest(self.input)},
                   'references': [{'path': 'upgrade/old-record.json',
                                   'sha256': self.digest(self.reference)}],
                   'applicability': {'conditions': 'source identities match',
                                     'differences': 'target remains untested',
                                     'reason': 'source-only assertion; no target acceptance'}}
        self.receipt.write_text(json.dumps(receipt))
        return receipt

    def check(self):
        self.matrix_path.write_text(yaml.safe_dump(self.data))
        return gate.validate(self.root, self.matrix_path)[1]

    def test_pending_design_can_be_counted_without_live_execution(self):
        self.assertEqual(self.check(), [])
        counts = gate.snapshot(self.data)
        self.assertEqual((counts['pass'], counts['total'], counts['completed_groups']), (0, 15, 0))

    def test_accepted_source_claim_does_not_sign_target_integration(self):
        self.accepted_baseline()
        self.assertEqual(self.check(), [])
        self.assertEqual(gate.snapshot(self.data)['pass'], 1)
        self.assertEqual(self.data['cases'][1]['status'], 'pending')

    def test_changed_input_only_rejects_dependent_acceptance(self):
        self.accepted_baseline()
        self.input.write_text('changed runtime input\n')
        errors = self.check()
        self.assertTrue(any('UPG-01: missing or stale input' in e for e in errors), errors)
        self.assertEqual(self.data['cases'][0]['status'], 'pass')  # read-only, never reset evidence

    def test_copied_record_cannot_be_silently_changed(self):
        self.accepted_baseline()
        self.reference.write_text('{}')
        self.assertTrue(any('missing or stale' in e for e in self.check()))

    def test_other_target_and_missing_claim_are_rejected(self):
        receipt = self.accepted_baseline()
        for patch in ({'target': 'v1.35.10'}, {'case_ids': ['UPG-02']}, {'claim': ''},
                      {'covered_subcases': []}, {'accepted': False}, {'applicability': {}}):
            with self.subTest(patch=patch):
                self.receipt.write_text(json.dumps(receipt | patch))
                self.assertTrue(self.check())

    def test_final_coverage_cannot_drop_an_item(self):
        self.data['cases'].pop(2)
        self.data['cases'][-1]['depends_on'].remove('UPG-03')
        self.assertTrue(any('fifteen' in e for e in self.check()))

    def test_analysis_or_reuse_cannot_sign_new_migration_claims(self):
        receipt = self.accepted_baseline()
        for cid in sorted(gate.LIVE_CASES):
            case = next(c for c in self.data['cases'] if c['id'] == cid)
            case.update(subcases=['exact-source-identity'], subcase_expansion_complete=True)
            for method in ('analysis', 'reuse', 'static'):
                with self.subTest(cid=cid, method=method):
                    self.receipt.write_text(json.dumps(receipt | {'case_ids': [cid], 'method': method}))
                    case['evidence'] = 'upgrade/v1.34.13/baseline.json'
                    with self.assertRaisesRegex(gate.Invalid, 'requires live'):
                        gate.evidence_valid(self.root, self.data['meta'], case)

    def test_duplicate_ids_and_dependency_cycles_are_rejected(self):
        original = deepcopy(self.data)
        self.data['cases'][1]['id'] = 'UPG-01'
        with self.assertRaises(gate.Invalid):
            self.check()
        self.data = original
        self.data['cases'][0]['depends_on'] = ['UPG-02']
        self.assertTrue(any('cycle' in e for e in self.check()))

    def test_yaml_duplicate_keys_and_external_paths_are_rejected(self):
        self.matrix_path.write_text('meta: {}\nmeta: {}\n')
        with self.assertRaises(gate.Invalid):
            gate.read_mapping(self.matrix_path)
        for path in ('../outside.json', '/tmp/evidence.json', 'upgrade/../product.conf'):
            with self.subTest(path=path), self.assertRaises(gate.Invalid):
                gate.owned_path(self.root, path)

    def test_minor_skip_is_rejected(self):
        self.data['meta']['target'] = 'v1.35.10'
        with self.assertRaises(gate.Invalid):
            self.check()

    def test_live_marker_alone_cannot_sign_an_execution(self):
        receipt = self.accepted_baseline()
        receipt.update(method='live', run_id='fake-marker-only', durable_rc=0,
                       finalized_rc=0, failure_markers=0, success_marker='PASS',
                       cleanup_marker='CLEAN', business_assertions=['write/read'])
        self.data['cases'][0]['success_marker'] = 'PASS'
        self.receipt.write_text(json.dumps(receipt))
        self.assertTrue(any('original log' in e for e in self.check()))

    def test_final_prerequisite_cli_refuses_before_any_network_or_lab_action(self):
        self.check()
        result = subprocess.run([__import__('sys').executable, str(ROOT / 'upgrade/validate.py'),
                                 str(self.matrix_path), '--final-prerequisites'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UPG-01: prerequisite is pending', result.stdout)
        self.assertIn('release combination is not frozen', result.stdout)
        self.assertNotIn('UPGRADE_MATRIX_VALIDATION_PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
