"""Execute the real strict-mode upgrade wrapper without a cluster or mutation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]


class UpgradeShell(unittest.TestCase):
    def run_fixture(self,fault='',cleanup=False,journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-gate'):(root/name).mkdir(parents=True)
            record=root/'clusters/longhorn-gate/longhorn-v1-upgrade-lifecycle.json'
            if journal:record.write_text('{}')
            control=root/'control-config';control.write_text('original')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('UPGRADE_FAULT','')
journal=root/'clusters/longhorn-gate/longhorn-v1-upgrade-lifecycle.json'
with (root/'calls').open('a') as stream:stream.write(action+'\\n')
if action=='relay-maintain':
 if fault=='dead-relay':sys.exit(29)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/('relay-ready-'+str(os.getpid()))).write_text('handler-installed')
 while True:time.sleep(.01)
if action=='preflight':
 if fault=='preflight':sys.exit(27)
 print('LONGHORN_V1_UPGRADE_TRANSITION_PREFLIGHT_PASS')
if action=='prepare':
 (root/'control-config').write_text('changed')
 if fault=='prepare':sys.exit(31)
 print('LONGHORN_V1_UPGRADE_TRANSITION_OLD_READY')
if action=='business':
 journal.write_text('{}')
 if fault in ('business','business-and-cleanup'):sys.exit(17)
 if fault.startswith('manager-'):
  if fault!='manager-missing-proof':print('LONGHORN_V1_UPGRADE_BEFORE_RESTORES_REVALIDATED count=2 original_points=true original_manager_rollout=true')
  for point in ('plain-after','plain-after' if fault=='manager-duplicate' else 'encrypted-after'):
   print('LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED {"point": "'+point+'"}')
 else:
  for i in range(3 if fault=='restore-count' else 4):print('LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED point='+str(i))
 print('LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS source_version=1.12.1')
 print('LONGHORN_UPGRADE_PASS')
 if fault!='missing-marker':print('LONGHORN_UPGRADE_RECOVERY_PASS')
 journal.unlink()
if action=='clean':
 assert journal.is_file()
 if fault in ('cleanup','business-and-cleanup'):sys.exit(19)
 deadline=time.monotonic()+2
 while len(list(root.glob('relay-ready-*')))<2:
  assert time.monotonic()<deadline,'fixture relay handlers never became ready'
  time.sleep(.005)
 print('LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS source_version=1.12.1');journal.unlink()
if action=='relay-clean' and fault=='relay-clean':sys.exit(21)
if action=='finalize':
 assert (root/'control-config').read_text()=='original'
 if fault=='finalize':sys.exit(23)
 print('LONGHORN_V1_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS original_core=true')
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            source=(ROOT/'tests/helpers/longhorn-v1-upgrade-lifecycle.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(control))
            script=root/'run.sh';script.write_text(source)
            result=subprocess.run(['bash',str(script),'v1-upgrade-cleanup' if cleanup else 'v1-upgrade-lifecycle'],text=True,capture_output=True,timeout=10,
                env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',UPGRADE_FAULT=fault))
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines() if line.startswith('LONGHORN_V1_UPGRADE_DURABLE_WORK ')))
            try:
                status=(work/'status').read_text();self.assertIn('rc='+str(result.returncode)+' ',status)
                first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                return result,(root/'calls').read_text().splitlines(),first,control.read_text()
            finally:shutil.rmtree(work)

    def test_complete_chain_runs_once_and_restores_control_before_finalization(self):
        result,calls,first,control=self.run_fixture()
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first);self.assertEqual('original',control)
        self.assertLess(calls.index('preflight'),calls.index('relay-ensure'))
        self.assertLess(calls.index('prepare'),calls.index('business'))
        self.assertLess(calls.index('relay-clean'),calls.index('finalize'))
        self.assertEqual(1,calls.count('business'));self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_V1_UPGRADE_GATE_PASS'))

    def test_preflight_failure_cannot_start_or_remove_relays_or_uninstall(self):
        result,calls,first,control=self.run_fixture('preflight')
        self.assertEqual(27,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
        for action in ('relay-ensure','relay-clean','prepare','business','finalize'):self.assertNotIn(action,calls)

    def test_manager_resume_requires_revalidated_before_scope_and_both_distinct_after_restores(self):
        for fault in ('manager-resume','manager-missing-proof','manager-duplicate'):
            with self.subTest(fault=fault):
                result,calls,first,_=self.run_fixture(fault)
                if fault=='manager-resume':
                    self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
                    self.assertIn('LONGHORN_V1_UPGRADE_GATE_PASS',result.stdout)
                else:
                    self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                    self.assertNotIn('finalize',calls);self.assertNotIn('LONGHORN_V1_UPGRADE_GATE_PASS',result.stdout)

    def test_failed_prepare_retains_stage_without_business_cleanup_or_false_pass(self):
        result,calls,first,control=self.run_fixture('prepare')
        self.assertEqual(31,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
        for action in ('business','clean','finalize'):self.assertNotIn(action,calls)

    def test_business_first_failure_survives_cleanup_failure(self):
        for fault in ('business','business-and-cleanup'):
            with self.subTest(fault=fault):
                result,calls,first,_=self.run_fixture(fault)
                self.assertEqual(17,result.returncode);self.assertIn('rc=17 ',first);self.assertIn('clean',calls)
                self.assertNotIn('finalize',calls);self.assertNotIn('LONGHORN_V1_UPGRADE_GATE_PASS',result.stdout)

    def test_markers_restore_count_and_late_cleanup_or_relay_failure_cannot_pass(self):
        for fault in ('restore-count','missing-marker','dead-relay','relay-clean','finalize'):
            with self.subTest(fault=fault):
                result,_,first,_=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('LONGHORN_V1_UPGRADE_GATE_PASS',result.stdout)

    def test_cleanup_needs_owned_journal_and_never_repeats_transition_or_business(self):
        result,calls,_,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('relay-ensure',calls)
        result,calls,first,_=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        for action in ('preflight','prepare','business'):self.assertNotIn(action,calls)
        self.assertIn('clean',calls);self.assertIn('finalize',calls)
        self.assertIn('LONGHORN_V1_UPGRADE_CLEANUP_PASS',result.stdout)

    def test_cleanup_fixture_waits_for_both_relay_handlers_instead_of_process_start_timing(self):
        write_text=Path.write_text
        def delayed_handler(path, content, *args, **kwargs):
            if path.name=='python' and 'signal.signal(signal.SIGTERM' in content:
                content=content.replace(' signal.signal(signal.SIGTERM', ' time.sleep(.15)\n signal.signal(signal.SIGTERM')
            return write_text(path,content,*args,**kwargs)
        with patch.object(Path,'write_text',new=delayed_handler):
            result,calls,first,_=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertFalse(first);self.assertEqual(2,calls.count('relay-maintain'))


if __name__=='__main__':unittest.main()
