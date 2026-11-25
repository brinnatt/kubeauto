"""Qualify evidence admission without starting SSH or database fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class MigrationEvidenceContracts(unittest.TestCase):
    def test_native_execution_binding_accepts_only_unchanged_owning_inputs(self):
        runner = (ROOT/'tests/run_tools_regression.sh').read_text()
        code = runner.split("<<'PYMIGFINAL'\n", 1)[1].split('\nPYMIGFINAL', 1)[0]
        proof = json.loads((ROOT/'tests/evidence/tools-migration-native-20261125.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(name, value):
                p = root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(value)
                return hashlib.sha256(p.read_bytes()).hexdigest()
            for name in proof['inputs']:
                proof['inputs'][name] = write(name, 'fixture')
            write('tests/run_tools_regression.sh', runner)
            proof['launcher']['sha256'] = write(proof['launcher']['snapshot'], runner)
            matrix = yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())
            write('tests/tools-test-matrix.yaml', yaml.safe_dump(matrix))
            record = proof['runs']['native']
            record['sha256'] = write(record['log'], '\n'.join(record['required_markers']))
            record['archive_sha256'] = write(record['archive'], 'archive')
            write(record['remote_status'], '0 0\n')
            write('tests/evidence/tools-migration-native-20261125.json', json.dumps(proof))
            def admit():
                return subprocess.run([sys.executable, '-B', '-c', code, str(root)], capture_output=True, text=True)
            result = admit(); self.assertEqual(result.returncode, 0, result.stderr)
            write('tools/mysqltools/MyBackupCli.py', 'unrelated source; frozen help dependency unchanged')
            write('tests/run_tools_regression.sh', runner.replace('TOOLS_KUBE_BACKUP_EXIT', 'TOOLS_UNRELATED_NEW_EXIT'))
            self.assertEqual(admit().returncode, 0)
            write('dist/MigrationCli-rocky8', 'changed executable')
            self.assertIn('MIGRATION_FINAL_STALE_INPUT', admit().stderr)
            write('dist/MigrationCli-rocky8', 'fixture')
            write(record['remote_status'], '1 1\n')
            self.assertIn('MIGRATION_FINAL_DURABLE_REFUSED', admit().stderr)
            write(record['remote_status'], '0 0\n')
            write('tests/run_tools_regression.sh', runner.replace('stage="${MIGRATION_STAGE:-full}"', 'stage=wrong'))
            self.assertIn('MIGRATION_FINAL_EXECUTION_CHANGED', admit().stderr)

    def test_build_input_snapshot_requires_its_exact_original_checksum(self):
        runner = (ROOT/'tests/run_tools_regression.sh').read_text()
        code = runner.split("<<'PYMIGBUILD'\n", 1)[1].split('\nPYMIGBUILD', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'dist').mkdir(); (root/'tests/helpers').mkdir(parents=True)
            source = root/'tests/helpers/build-tools-rocky8.sh'; source.write_text('current builder')
            old = hashlib.sha256(b'old builder').hexdigest()
            snapshot = root/'logs/tools-build-inputs'/(old+'.sh'); snapshot.parent.mkdir(parents=True)
            snapshot.write_text('old builder')
            (root/'dist/MigrationCli-rocky8').write_text('binary')
            (root/'dist/mysql-migration-build.json').write_text(json.dumps({
                'inputs': {'tests/helpers/build-tools-rocky8.sh': old},
                'artifacts': {'MigrationCli': hashlib.sha256(b'binary').hexdigest()}}))
            def admit():
                return subprocess.run([sys.executable, '-B', '-c', code, str(root)], capture_output=True)
            self.assertEqual(admit().returncode, 0)
            snapshot.write_text('tampered snapshot')
            self.assertNotEqual(admit().returncode, 0)


if __name__ == '__main__':
    unittest.main()
