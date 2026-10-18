"""MongoDB matrix rejects omitted coverage and premature acceptance, offline."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongodb_matrix', ROOT / 'tests/helpers/validate_mongodb_matrix.py')
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.data = yaml.safe_load((ROOT / 'tests/mongodb-test-matrix.yaml').read_text())
        self.data['meta']['result'] = 'pending'
        for case in self.data['cases']:
            case.update(status='pending', evidence='')

    def check_data(self, data=None, require_pass=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'matrix.yaml'
            path.write_text(yaml.safe_dump(self.data if data is None else data))
            return matrix.validate(path, require_pass)

    def test_pending_is_valid_but_cannot_sign_off(self):
        self.assertEqual(self.check_data(), [])
        self.assertTrue(self.check_data(require_pass=True))

    def test_omitted_recovery_scenario_blocks_matrix(self):
        self.data['cases'] = [c for c in self.data['cases'] if c['group'] != 'disaster-recovery']
        self.assertTrue(any('missing coverage' in e for e in self.check_data()))

    def test_duplicate_module_or_marker_is_rejected(self):
        self.data['cases'].append(copy.deepcopy(self.data['cases'][0]))
        self.assertTrue(any('duplicate' in e for e in self.check_data()))

    def test_pass_requires_evidence_and_complete_coverage(self):
        self.data['cases'][0]['status'] = 'pass'
        self.assertTrue(any('requires current evidence' in e for e in self.check_data()))
        self.data['meta']['result'] = 'PASS'
        self.assertTrue(any('all cases' in e for e in self.check_data()))

    def test_non_runner_disproof_and_wrong_marker_are_rejected(self):
        self.data['cases'][0].update(disproof='kubectl get pods', success_marker='POD_READY')
        errors = self.check_data()
        self.assertTrue(any('fixed runner' in e for e in errors))
        self.assertTrue(any('module identity' in e for e in errors))

    def test_malformed_types_fail_cleanly(self):
        self.data['cases'][0]['group'] = []
        self.assertTrue(self.check_data())
        self.assertTrue(self.check_data({'meta': [], 'cases': [None]}))

    def test_documentation_gate_binds_manual_review_to_current_text(self):
        for defect in (None, 'text-changed', 'extra-document', 'open-defect', 'reader-path', 'figure', 'static', 'evidence'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); directory = root/'docs/middleware/mongodb'
                directory.mkdir(parents=True); (root/'tests/evidence').mkdir(parents=True)
                documents = {}
                for name in ('operations-manual.md', 'technical-whitepaper.md', 'development-manual.md'):
                    p = directory/name; p.write_text('reviewed text')
                    documents[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
                accepted = root/'tests/evidence/operation.json'
                accepted.write_text(json.dumps({'durable_rc':0, 'failure_markers':1 if defect=='evidence' else 0,
                                                'case_ids':['MONGODB-16']}))
                review = {'content_review':'pass', 'open_defects':[],
                          'reader_paths':{'novice':'pass', 'experienced':'pass'}, 'documents':documents,
                          'figures':[{'rendered':True, 'visually_checked':True}],
                          'static_check_rc':0, 'operation_evidence':[{'path':'tests/evidence/operation.json',
                              'sha256':hashlib.sha256(accepted.read_bytes()).hexdigest(), 'case_id':'MONGODB-16'}]}
                if defect=='text-changed':(directory/'operations-manual.md').write_text('changed')
                if defect=='extra-document':(directory/'README.md').write_text('extra')
                if defect=='open-defect':review['open_defects']=['unresolved']
                if defect=='reader-path':review['reader_paths']['novice']='pending'
                if defect=='figure':review['figures'][0]['visually_checked']=False
                if defect=='static':review['static_check_rc']=1
                (root/'tests/evidence/mongodb-documentation.json').write_text(json.dumps(review))
                errors = matrix.validate_documentation(root)
                self.assertTrue(errors) if defect else self.assertEqual(errors, [])
                if not defect:
                    (root/'.venv').symlink_to(ROOT/'.venv', target_is_directory=True)
                    (root/'tests/helpers').mkdir()
                    (root/'tests/helpers/validate_mongodb_matrix.py').symlink_to(ROOT/'tests/helpers/validate_mongodb_matrix.py')
                    (root/'tests/mongodb-test-matrix.yaml').write_text(yaml.safe_dump(self.data))
                    result = subprocess.run(['bash', '-euc',
                        'source tests/helpers/mongodb-runner.sh; ROOT="$1"; mongodb_dispatch --mongodb-documentation',
                        'documentation-contract', str(root)], cwd=ROOT, capture_output=True, text=True, check=True)
                    self.assertIn('MONGODB_DOCUMENTATION_PASS', result.stdout.splitlines())
