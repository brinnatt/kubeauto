"""Exercise the real last-replica wrapper's durable exits without a lab."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class FailureShell(unittest.TestCase):
    def run_fixture(self,fault='',cleanup=False,journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-gate'):(root/name).mkdir(parents=True)
            record=root/'clusters/longhorn-gate/longhorn-install-business.json'
            if journal:
                record.write_text('{}')
                (record.parent/'longhorn-last-replica-intent.json').write_text('{}')
            control=root/'control-config';control.write_text('original')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('LAST_REPLICA_FAULT','')
journal=root/'clusters/longhorn-gate/longhorn-install-business.json'
with (root/'calls').open('a') as stream:stream.write(action+'\\n')
if action=='verify-transport' and fault=='preflight':sys.exit(27)
if action=='relay-ensure':(root/'control-config').write_text('changed')
if action=='relay-maintain':
 if fault=='dead-relay':sys.exit(29)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));(root/'ready').touch()
 while True:time.sleep(.01)
if action in ('business','clean'):
 for _ in range(100):
  if fault=='dead-relay' or (root/'ready').exists():break
  time.sleep(.01)
 else:sys.exit(37)
if action=='business':
 journal.write_text('{}')
 if fault in ('business','business-and-cleanup'):sys.exit(17)
 print('LONGHORN_LAST_REPLICA_RECOVERY {}')
 print('LONGHORN_LAST_REPLICA_SCOPED_CLEAN_PASS original_scheduling=true')
 if fault!='missing-marker':print('LONGHORN_LAST_REPLICA_PASS')
 journal.unlink(missing_ok=True)
if action=='clean':
 if fault in ('cleanup','business-and-cleanup'):sys.exit(19)
 if fault!='missing-clean-marker':print('LONGHORN_LAST_REPLICA_RECOVERY_SCOPED_CLEAN_PASS original_worker=true')
 journal.unlink(missing_ok=True)
if action=='relay-clean' and fault=='relay-clean':sys.exit(21)
if action in ('verify-core','health') and (root/'ready').exists():
 assert (root/'control-config').read_text()=='original'
 if action=='health' and fault=='late-health':sys.exit(23)
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            script=root/'run.sh';script.write_text((ROOT/'tests/helpers/longhorn-last-replica.sh').read_text()
                .replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(control)))
            result=subprocess.run(['bash',str(script),'last-replica-cleanup' if cleanup else 'last-replica'],text=True,
                capture_output=True,timeout=10,env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LAST_REPLICA_FAULT=fault))
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines()
                if line.startswith('LONGHORN_LAST_REPLICA_DURABLE_WORK ')))
            try:
                self.assertIn('rc='+str(result.returncode)+' ',(work/'status').read_text())
                first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                return result,(root/'calls').read_text().splitlines(),first,control.read_text()
            finally:shutil.rmtree(work)

    def test_success_supervises_relay_and_idempotent_cleanup_before_final_health(self):
        result,calls,first,control=self.run_fixture()
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first);self.assertEqual('original',control)
        self.assertEqual(1,calls.count('business'));self.assertLess(calls.index('relay-clean'),calls.index('health'))
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_LAST_REPLICA_GATE_PASS'))

    def test_preflight_failure_cannot_start_relay_or_fault(self):
        result,calls,first,_=self.run_fixture('preflight')
        self.assertEqual(27,result.returncode);self.assertTrue(first);self.assertEqual(['verify-transport'],calls)

    def test_business_first_failure_survives_failed_recovery(self):
        for fault in ('business','business-and-cleanup'):
            with self.subTest(fault=fault):
                result,calls,first,control=self.run_fixture(fault)
                self.assertEqual(17,result.returncode);self.assertIn('rc=17 ',first);self.assertEqual('original',control)
                self.assertIn('clean',calls);self.assertNotIn('LONGHORN_LAST_REPLICA_GATE_PASS',result.stdout)

    def test_missing_marker_dead_relay_or_late_failure_cannot_pass(self):
        for fault in ('missing-marker','dead-relay','relay-clean','late-health'):
            with self.subTest(fault=fault):
                result,_,first,control=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
                self.assertNotIn('LONGHORN_LAST_REPLICA_GATE_PASS',result.stdout)

    def test_cleanup_requires_owned_journal_and_does_not_repeat_business(self):
        result,calls,_,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('relay-ensure',calls)
        result,calls,first,_=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertIn('clean',calls);self.assertNotIn('business',calls)
        self.assertIn('LONGHORN_LAST_REPLICA_CLEANUP_PASS',result.stdout)

    def test_cleanup_failure_or_absent_scoped_marker_is_durable(self):
        for fault in ('cleanup','missing-clean-marker'):
            with self.subTest(fault=fault):
                result,_,first,_=self.run_fixture(fault,cleanup=True,journal=True)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('LONGHORN_LAST_REPLICA_CLEANUP_PASS',result.stdout)


if __name__=='__main__':unittest.main()
