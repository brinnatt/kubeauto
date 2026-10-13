"""Full orchestration must stop at the first failed or incomplete durable stage."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT=Path(__file__).resolve().parents[2]


class FullGateTests(unittest.TestCase):
    def test_focused_full_lease_verification_runs_only_clean_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root); calls = root / 'calls'
            (root / 'tests/helpers/redis-lab-clean-gate.sh').write_text('''
set -eu
test "$1" = --verify
test "$REDIS_PARENT_LEASE_HELD" = yes
[[ /proc/self/fd/9 -ef "$REDIS_LAB_LEASE_FILE" ]]
echo verify >> "$REDIS_FULL_TEST_CALLS"
echo LAB_CLEAN_VERIFY_PASS
echo REDIS_LAB_CLEAN_VERIFY_PASS
''')
            result = subprocess.run(['bash', str(ROOT / 'tests/helpers/redis-full-gate.sh'), '--clean-verify'],
                env={**os.environ, 'KUBEAUTO_BASE':tmp, 'REDIS_FULL_TEST_CALLS':str(calls),
                     'REDIS_LAB_LEASE_FILE':tmp + '/lease', 'REDIS_FULL_EVIDENCE_ROOT':tmp},
                text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls.read_text().splitlines(), ['verify'])
            self.assertIn('REDIS_FULL_LEASE_VERIFY_PASS', result.stdout)
            self.assertNotIn('REDIS_DELIVERY_BRANCH_PASS', result.stdout)
            self.assertNotIn('REDIS_FULL_GATE_PASS', result.stdout)

    def fixture(self, root):
        helpers=root/'tests/helpers';helpers.mkdir(parents=True)
        evidence=root/'tests/evidence';evidence.mkdir()
        python=root/'.venv/bin/python';python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');python.chmod(0o755)
        matrix=yaml.safe_load((ROOT/'tests/redis-test-matrix.yaml').read_text())
        for c in matrix['cases']:
            if c['id']!='REDIS-24': c.update(status='pass',evidence='isolated contract fixture')
        (root/'tests/redis-test-matrix.yaml').write_text(yaml.safe_dump(matrix))
        for name in ('redis-images-20261012.json',
                     'redis-storage-images-20261012.json','redis-observability-images-20261013.json'):
            shutil.copyfile(ROOT/'tests/evidence'/name,evidence/name)
        # Pure contract input, never registry publication evidence.
        protected=json.loads((ROOT/'tests/evidence/redis-protected-operator-images-20261013.json').read_text())
        row=protected['images'][0]
        row.update(key='brinnatt/redis-operator:0.27.0-kubeauto.2',
                   dockerhub_ref='brinnatt/redis-operator:0.27.0-kubeauto.2',
                   talkedu_ref='hub.talkedu.cn/kubeauto/redis-operator:0.27.0-kubeauto.2')
        (evidence/'redis-topology-operator-images-20261013.json').write_text(json.dumps(protected))
        cluster=json.loads(json.dumps(protected))
        row=cluster['images'][0]
        row.update(key='brinnatt/redis:8.10.2-kubeauto.1',
                   dockerhub_ref='brinnatt/redis:8.10.2-kubeauto.1',
                   talkedu_ref='hub.talkedu.cn/kubeauto/redis:8.10.2-kubeauto.1')
        (evidence/'redis-state-refresh-images-20261013.json').write_text(json.dumps(cluster))
        for name in ('redis-artifacts.py','run-durable-gate.sh'):
            shutil.copyfile(ROOT/'tests/helpers'/name,helpers/name)
        (helpers/'redis-evidence.py').write_text('print("{}")\n')
        (helpers/'redis-lab-clean-gate.sh').write_text(
            'echo clean >> "$REDIS_FULL_TEST_CALLS"\n'
            'echo LAB_CLEAN_VERIFY_PASS\necho REDIS_LAB_CLEAN_VERIFY_PASS\necho REDIS_LAB_CLEANUP_PASS\n')
        (helpers/'redis-install-gate.sh').write_text('echo install >> "$REDIS_FULL_TEST_CALLS"\necho REDIS_INSTALL_MODULE_PASS\n')
        (helpers/'redis-focused-gate.sh').write_text('''
echo "$1" >> "$REDIS_FULL_TEST_CALLS"
if test "$1" = "$REDIS_FULL_TEST_FAIL"; then
 case "$REDIS_FULL_TEST_KIND" in exit) exit 7;; marker) exit 0;;
 failure-marker) echo REDIS_FIRST_FAILURE; echo "REDIS_FOCUSED_MODULE_PASS module=$1"; exit 0;; esac
fi
echo "REDIS_FOCUSED_SCOPED_CLEAN_PASS module=$1"
echo "REDIS_FOCUSED_MODULE_PASS module=$1"
''')
        return matrix

    def test_pending_module_blocks_full_run_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);matrix=self.fixture(root)
            matrix['cases'][4].update(status='pending',evidence='')
            (root/'tests/redis-test-matrix.yaml').write_text(yaml.safe_dump(matrix))
            calls=root/'calls'
            result=subprocess.run(['bash',str(ROOT/'tests/helpers/redis-full-gate.sh'),'--full'],
                env={**os.environ,'KUBEAUTO_BASE':tmp,'REDIS_FULL_TEST_CALLS':str(calls)},
                text=True,capture_output=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('focused-qualification-pending cases=REDIS-05',result.stderr)
            self.assertFalse(calls.exists())

    def test_complete_orchestration_records_every_stage_and_one_terminal_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root);calls=root/'calls'
            result=subprocess.run(['bash',str(ROOT/'tests/helpers/redis-full-gate.sh'),'--full'],
                env={**os.environ,'KUBEAUTO_BASE':tmp,'REDIS_FULL_TEST_CALLS':str(calls),
                     'REDIS_FULL_TEST_FAIL':'none','REDIS_FULL_TEST_KIND':'exit',
                     'REDIS_LAB_LEASE_FILE':tmp+'/lease','REDIS_FULL_EVIDENCE_ROOT':tmp},
                text=True,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(result.stdout.splitlines().count('REDIS_FULL_GATE_PASS'),1)
            self.assertEqual(result.stdout.splitlines().count('REDIS_DELIVERY_BRANCH_PASS'),1)
            directories=list(root.glob('kubeauto-redis-full.*'))
            self.assertEqual(len(directories),1)
            names={p.name for p in directories[0].iterdir()}
            self.assertEqual(len(names),32)
            self.assertIn('final-idempotence',names)
            self.assertEqual(calls.read_text().splitlines()[-3:],['stability','clean','clean'])
            for path in directories[0].iterdir():
                self.assertEqual((path/'durable.exit').read_text().strip(),'0')
                self.assertEqual((path/'durable.finalized').read_text().strip(),'0')
                self.assertTrue((path/'launch-source.json').is_file())

    def test_first_exit_missing_marker_or_failure_marker_stops_later_scenarios(self):
        for kind in ('exit','marker','failure-marker'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.fixture(root);calls=root/'calls'
                result=subprocess.run(['bash',str(ROOT/'tests/helpers/redis-full-gate.sh'),'--full'],
                    env={**os.environ,'KUBEAUTO_BASE':tmp,'REDIS_FULL_TEST_CALLS':str(calls),
                         'REDIS_FULL_TEST_FAIL':'sentinel-data','REDIS_FULL_TEST_KIND':kind,
                         'REDIS_LAB_LEASE_FILE':tmp+'/lease','REDIS_FULL_EVIDENCE_ROOT':tmp},
                    text=True,capture_output=True,timeout=20)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(calls.read_text().splitlines(),
                                 ['clean','clean','install','platform','sentinel-data','clean'])
                self.assertNotIn('REDIS_DELIVERY_BRANCH_PASS',result.stdout)
                self.assertNotIn('REDIS_FULL_GATE_PASS',result.stdout)
                directories=list(root.glob('kubeauto-redis-full.*'))
                self.assertEqual(len(directories),1)
                state=directories[0]/'sentinel-data/durable.exit'
                self.assertEqual(state.read_text().strip(),'7' if kind=='exit' else '0')
                self.assertTrue((directories[0]/'sentinel-data/launch-source.json').is_file())


if __name__=='__main__': unittest.main()
