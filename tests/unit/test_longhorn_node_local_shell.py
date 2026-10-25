"""Execute the actual same-subnet shell with bounded isolated CLI fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

CANDIDATE = Path(__file__).resolve().parents[2]/'tests/helpers'


class LocalNodeShellContracts(unittest.TestCase):
    def run_fixture(self, failure='', cleanup=False, resume=False, completed_prefix=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.venv/bin', 'tests/helpers', 'kube-bin'):
                (root/name).mkdir(parents=True)
            python = root/'.venv/bin/python'
            python.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
r=Path(__file__).resolve().parents[2];action=sys.argv[2]
d=r/'clusters/longhorn-node-gate';j=d/'longhorn-node-lifecycle.json'
with (r/'calls').open('a') as f:f.write(action+'\\n')
failure=os.environ.get('NODE_LOCAL_FAILURE','')
if action=='new':d.mkdir(parents=True);(d/'config.yml').write_text('new')
if action=='configure':assert (d/'config.yml').is_file()
if action=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (r/'relay-ready').touch()
 while True:time.sleep(.01)
if action=='relay-clean':(r/'relay-ready').unlink(missing_ok=True)
if action in ('clean','resume-business','retire-storage'):
 for _ in range(100):
  if (r/'relay-ready').exists():break
  time.sleep(.01)
 else:sys.exit(29)
if action=='core-setup':
 for _ in range(100):
  if (r/'relay-ready').exists():break
  time.sleep(.01)
 else:sys.exit(29)
 (d/'core-ready').touch()
if action=='prepare-storage':assert (d/'core-ready').is_file();(d/'storage-inputs').touch()
if action=='storage-setup':assert (d/'storage-inputs').is_file();(d/'storage-ready').touch()
if action=='admit':assert (d/'storage-ready').is_file();j.write_text('{}')
if failure==action:sys.exit(17)
if action in ('business','resume-business'):
 assert j.is_file()
 markers=('LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4 host=192.168.47.149',
  'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node',
  'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master',
  'LONGHORN_NODE_NATIVE_EVACUATION_RETIRED host=192.168.47.149',
  'LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS','LONGHORN_NODE_LIFECYCLE_PASS')
 if action=='resume-business' and os.environ.get('NODE_LOCAL_PREFIX')=='yes':
  print('LONGHORN_NODE_COMPLETED_ADD_PREFIX_VERIFIED log_sha256='+'a'*64)
  markers=markers[3:]
 for marker in markers:print(marker)
if action=='clean':assert j.is_file();print('LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS')
if action=='finalize':assert j.is_file();j.unlink()
if action=='retire-storage':assert not j.exists()
if action=='retire-core':assert not j.exists();import shutil;shutil.rmtree(d)
''')
            python.chmod(0o755)
            wipe = root/'tests/helpers/lab-wipe-nodes.sh'
            wipe.write_text('#!/bin/sh\necho LAB_CLEAN_VERIFY_PASS\n'); wipe.chmod(0o755)
            kubectl = root/'kube-bin/kubectl'
            kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
            for name in ('source.json', 'target.json'): (root/name).write_text('{}')
            script = (CANDIDATE/'longhorn-node-local.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(root))
            script = script.replace('control_config=/root/.kube/config', 'control_config='+str(root/'control-config'))
            path = root/'run.sh'; path.write_text(script)
            if cleanup or resume:
                owned = root/'clusters/longhorn-node-gate'; owned.mkdir(parents=True)
                (owned/'longhorn-lab-owner.json').write_text('{}')
                (owned/'longhorn-node-lifecycle.json').write_text('{}')
            result = subprocess.run(['bash', str(path), 'node-lifecycle-cleanup' if cleanup else 'node-lifecycle'], capture_output=True, text=True, timeout=15,
                env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', NODE_LOCAL_FAILURE=failure,
                    NODE_LOCAL_PREFIX='yes' if completed_prefix else 'no',
                    LONGHORN_V2_RELEASE_PROOF=str(root/'source.json'), LONGHORN_V2_TARGET_RELEASE_PROOF=str(root/'target.json')))
            work = Path(next(line.split('path=', 1)[1] for line in result.stdout.splitlines()
                if line.startswith('LONGHORN_NODE_LOCAL_DURABLE_WORK ')))
            try:
                status = (work/'status').read_text()
                self.assertIn('rc='+str(result.returncode)+' ', status)
                return result, (root/'calls').read_text().splitlines(), status
            finally: shutil.rmtree(work)

    def test_real_shell_orders_core_storage_business_and_both_retirement_boundaries(self):
        result, calls, status = self.run_fixture()
        self.assertEqual(0, result.returncode, result.stderr)
        for before, after in (('configure', 'core-setup'), ('core-setup', 'prepare-storage'),
                ('prepare-storage', 'storage-setup'), ('storage-setup', 'admit'), ('admit', 'business'),
                ('business', 'clean'), ('clean', 'finalize'), ('finalize', 'retire-storage'), ('retire-storage', 'retire-core')):
            self.assertLess(calls.index(before), calls.index(after))
        self.assertEqual(1, calls.count('core-setup'))
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_NODE_LIFECYCLE_GATE_PASS'))
        self.assertIn('cleanup_rc=0', status)

    def test_failed_prerequisite_stops_dependent_business_and_preserves_first_exit(self):
        for failure, forbidden in (('core-setup', 'prepare-storage'), ('prepare-storage', 'storage-setup'),
                ('storage-setup', 'admit'), ('admit', 'business'), ('business', 'no-later-business')):
            with self.subTest(failure=failure):
                result, calls, _ = self.run_fixture(failure)
                self.assertEqual(17, result.returncode, result.stderr)
                self.assertNotIn(forbidden, calls)
                self.assertNotIn('LONGHORN_NODE_LIFECYCLE_GATE_PASS', result.stdout)

    def test_unclean_receiving_node_blocks_parent_storage_and_core_retirement(self):
        result, calls, status = self.run_fixture('clean')
        self.assertEqual(17, result.returncode)
        self.assertNotIn('finalize', calls); self.assertNotIn('retire-storage', calls); self.assertNotIn('retire-core', calls)
        self.assertIn('cleanup_rc=17', status)

    def test_recovery_uses_owned_journal_and_never_recreates_the_cluster_or_business(self):
        result, calls, _ = self.run_fixture(cleanup=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('recover-partial-join', calls); self.assertIn('clean', calls)
        self.assertIn('retire-storage', calls); self.assertIn('retire-core', calls)
        for action in ('new', 'configure', 'core-setup', 'prepare-storage', 'storage-setup', 'admit', 'business'):
            self.assertNotIn(action, calls)
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_NODE_LIFECYCLE_CLEANUP_PASS'))

    def test_partial_add_continuation_reuses_owned_core_and_real_business_boundary(self):
        result, calls, status = self.run_fixture(resume=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('resume-business', calls)
        self.assertLess(calls.index('resume-business'), calls.index('clean'))
        for action in ('new', 'configure', 'core-setup', 'prepare-storage', 'storage-setup', 'admit', 'business'):
            self.assertNotIn(action, calls)
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_NODE_LIFECYCLE_GATE_PASS'))
        result, calls, _ = self.run_fixture('resume-business', resume=True)
        self.assertEqual(17, result.returncode)
        self.assertNotIn('LONGHORN_NODE_LIFECYCLE_GATE_PASS', result.stdout)

    def test_completed_add_continuation_accepts_verified_prefix_without_reprinting_business(self):
        result, calls, _ = self.run_fixture(resume=True, completed_prefix=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('LONGHORN_NODE_COMPLETED_ADD_PREFIX_VERIFIED', result.stdout)
        self.assertNotIn('LONGHORN_NODE_DATA_EXPANSION_VERIFIED', result.stdout)
        self.assertNotIn('new', calls); self.assertNotIn('admit', calls)
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_NODE_LIFECYCLE_GATE_PASS'))


if __name__ == '__main__': unittest.main()
