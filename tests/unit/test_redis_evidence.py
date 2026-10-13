"""Only complete current module evidence can be recorded as an acceptance result."""
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_evidence', ROOT / 'tests/helpers/redis-evidence.py')
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class RedisEvidenceTests(unittest.TestCase):
    def test_ansible_crd_stdout_json_string_preserves_exact_success_contract(self):
        module = 'crd-upgrade'
        plain = 'REDIS_CRD_UPGRADE_PASS definitions=4 stored-versions=retained shared-consumers=absent changed=false'
        lines = ['REDIS_CRD_PRODUCT_UPGRADE_PASS entry=config+kubecli',
                 'REDIS_FOCUSED_SCOPED_CLEAN_PASS module=crd-upgrade',
                 'REDIS_FOCUSED_MODULE_PASS module=crd-upgrade', 'REDIS_GATE_EXIT rc=0']
        output = '\n'.join(['    "stdout_lines": [', '        ' + json.dumps(plain), '    ]', *lines])
        case, markers = evidence.verify_log(module, output, 'state=exited rc=0')
        self.assertEqual(case, 'REDIS-22')
        self.assertIn(plain, markers)
        for bad in ('prose ' + plain, plain.replace('UPGRADE_PASS', 'UPGRADE_PENDING'),
                    'REDIS_CRD_UPGRADE_PASSX definitions=4'):
            with self.assertRaises(ValueError):
                evidence.verify_log(module, output.replace(json.dumps(plain), json.dumps(bad)), 'state=exited rc=0')
        with self.assertRaises(ValueError):
            evidence.verify_log(module, output + '\n"REDIS_CRD_UPGRADE_FAIL"', 'state=exited rc=0')

    def test_completed_gate_uses_frozen_remote_inputs_despite_local_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = evidence.DEPENDENCIES + evidence.MODULE_DEPENDENCIES['stability']
            for relative in paths:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('executed baseline')
            manifest = evidence.capture_dependencies(root, 'stability')
            frozen = root / 'launch-source.json'
            frozen.write_text(json.dumps(manifest))
            (root / paths[0]).write_text('unexecuted local change')
            self.assertEqual(evidence.frozen_dependencies(frozen, 'stability'), manifest['dependencies'])
            self.assertNotEqual(evidence.digest(root / paths[0]), manifest['dependencies'][paths[0]])
            for change in ({'module': 'backup'}, {'dependencies': {}}, {'schema': 2}):
                frozen.write_text(json.dumps(manifest | change))
                with self.assertRaises(ValueError):
                    evidence.frozen_dependencies(frozen, 'stability')
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        capture = runner.index('--capture $redis_module')
        launch = runner.index('nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh', capture)
        self.assertLess(capture, launch)

    def test_matrix_update_preserves_unrelated_cases_and_never_claims_ga(self):
        matrix = ('meta:\n  result: PENDING\ncases:\n'
                  '  - {id: REDIS-07, status: pending, evidence: ""}\n'
                  '  - {id: REDIS-08, status: pass, evidence: "unchanged fixture"}\n')
        updated = evidence.matrix_with_evidence(matrix, 'REDIS-07', Path('tests/evidence/current.json'))
        self.assertIn('result: PENDING', updated)
        self.assertIn('status: pass, evidence: "tests/evidence/current.json;', updated)
        old = [line for line in matrix.splitlines() if 'id: REDIS-07,' not in line]
        new = [line for line in updated.splitlines() if 'id: REDIS-07,' not in line]
        self.assertEqual(old, new)
        with self.assertRaises(ValueError):
            evidence.matrix_with_evidence(matrix, 'REDIS-99', Path('missing.json'))

    def test_every_supported_module_requires_own_business_scoped_and_exit_markers(self):
        for module, (case, marker) in (evidence.CASES | evidence.PARTIAL).items():
            lines = [marker, 'REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + module,
                     'REDIS_FOCUSED_MODULE_PASS module=' + module, 'REDIS_GATE_EXIT rc=0']
            if module == 'stability':
                lines[0] = ('REDIS_STABILITY_PASS seconds=1800 samples=100 business-errors=0 '
                            'restarts=pod+operator+node data-and-pvc=retained')
            lines.extend(evidence.EXTRA_MARKERS.get(module, ()))
            log = '\n'.join(lines)
            state = 'state=exited pid=1 rc=0 bytes=1024'
            self.assertEqual(evidence.verify_log(module, log, state)[0], case)
            for removed in lines:
                with self.assertRaises(ValueError):
                    evidence.verify_log(module, '\n'.join(line for line in lines if line != removed), state)
            for failed in ('[FAIL] product', 'REDIS_FIRST_FAILURE module=' + module,
                           'REDIS_BACKUP_CLEANUP_FAIL', 'REDIS_ROTATION_ROLLBACK_FAIL'):
                with self.assertRaises(ValueError):
                    evidence.verify_log(module, log + '\n' + failed, state)
            for bad_state in ('state=running rc=0', 'state=exited rc=1', 'state=exited rc=missing'):
                with self.assertRaises(ValueError):
                    evidence.verify_log(module, log, bad_state)
            with self.assertRaises(ValueError):
                evidence.verify_log(module, log.replace('REDIS_GATE_EXIT rc=0', 'REDIS_GATE_EXIT rc=00'), state)

    def test_partial_maintenance_and_persistence_do_not_claim_complete_case(self):
        self.assertNotIn('tls-rotation', evidence.CASES)
        self.assertNotIn('password-rotation', evidence.CASES)
        self.assertNotIn('idempotence', evidence.CASES)
        self.assertNotIn('persistence', evidence.CASES)
        focused = (ROOT / 'tests/helpers/redis-focused-gate.py').read_text()
        for module in ('sentinel-data', 'sentinel-security', 'sentinel-failure', 'sentinel-quorum'):
            self.assertIn(evidence.CASES[module][1], focused)


if __name__ == '__main__':
    unittest.main()
