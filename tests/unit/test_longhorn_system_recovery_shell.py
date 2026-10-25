"""Run the real two-site strict-mode wrapper without any live mutation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class RecoveryShell(unittest.TestCase):
    def run_fixture(self,fault='',cleanup=False,journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-gate'):(root/name).mkdir(parents=True)
            record=root/'clusters/longhorn-gate/longhorn-system-recovery.json'
            if journal:record.write_text('{}')
            control=root/'control-config';control.write_text('original')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('SYSTEM_RECOVERY_FAULT','')
site='target' if 'dr-target' in sys.argv[1] else 'source'
journal=root/'clusters/longhorn-gate/longhorn-system-recovery.json'
with (root/'calls').open('a') as stream:stream.write(site+':'+action+'\\n')
if action=='verify-transport' and fault=='preflight':sys.exit(27)
if action=='relay-ensure':
 (root/'control-config').write_text('changed')
 if fault=='target-relay-ensure' and site=='target':sys.exit(31)
if action=='relay-maintain':
 if fault=='dead-relay' and site=='target':sys.exit(29)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/(site+'-ready')).touch()
 while True:time.sleep(.01)
if action=='business':
 for _ in range(100):
  if fault=='dead-relay' or all((root/(name+'-ready')).exists() for name in ('source','target')):break
  time.sleep(.01)
 else:sys.exit(37)
 journal.write_text('{}')
 if fault in ('business','business-and-cleanup'):sys.exit(17)
 print('LONGHORN_SYSTEM_RESTORE_ATTACHED_REFUSAL_PASS target_uid_unchanged=true')
 print('LONGHORN_SYSTEM_RESTORE_INDEPENDENT_OBSERVED {}')
 print('LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS source=retained')
 if fault!='missing-marker':print('LONGHORN_SYSTEM_RESTORE_PASS')
 journal.unlink()
if action=='clean':
 assert journal.is_file()
 for _ in range(100):
  if all((root/(name+'-ready')).exists() for name in ('source','target')):break
  time.sleep(.01)
 else:sys.exit(37)
 if fault in ('cleanup','business-and-cleanup'):sys.exit(19)
 if fault!='missing-clean-marker':print('LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS source=retained')
 journal.unlink()
if action=='relay-clean' and fault=='relay-clean':sys.exit(21)
if action in ('verify-core','core-identity','health'):
 assert (root/'control-config').read_text()=='original'
 if action=='health' and fault=='late-health':sys.exit(23)
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            script=root/'run.sh'
            script.write_text((ROOT/'tests/helpers/longhorn-system-recovery.sh').read_text()
                .replace('BASE=/usr/local/kubeauto','BASE='+str(root))
                .replace('control_config=/root/.kube/config','control_config='+str(control)))
            result=subprocess.run(['bash',str(script),'system-restore-cleanup' if cleanup else 'system-restore'],
                text=True,capture_output=True,timeout=10,
                env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',SYSTEM_RECOVERY_FAULT=fault))
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines()
                if line.startswith('LONGHORN_SYSTEM_RECOVERY_DURABLE_WORK ')))
            try:
                status=(work/'status').read_text();self.assertIn('rc='+str(result.returncode)+' ',status)
                first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                return result,(root/'calls').read_text().splitlines(),first,control.read_text()
            finally:shutil.rmtree(work)

    def test_success_has_two_supervised_relays_and_restores_control_before_health(self):
        result,calls,first,control=self.run_fixture()
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first);self.assertEqual('original',control)
        self.assertEqual(1,calls.count('source:business'))
        for site in ('source','target'):
            self.assertIn(site+':relay-maintain',calls);self.assertIn(site+':relay-clean',calls)
        self.assertLess(calls.index('target:relay-clean'),calls.index('source:verify-core'))
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_SYSTEM_RESTORE_GATE_PASS'))

    def test_preflight_failure_never_starts_relays_or_business(self):
        result,calls,first,control=self.run_fixture('preflight')
        self.assertEqual(27,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
        self.assertEqual(['source:verify-transport'],calls)

    def test_partial_transport_failure_cleans_owned_transports_without_business(self):
        result,calls,first,control=self.run_fixture('target-relay-ensure')
        self.assertEqual(31,result.returncode);self.assertIn('rc=31 ',first);self.assertEqual('original',control)
        self.assertNotIn('source:business',calls);self.assertNotIn('source:clean',calls)
        self.assertIn('source:relay-clean',calls);self.assertIn('target:relay-clean',calls)

    def test_first_business_failure_survives_cleanup_failure(self):
        for fault in ('business','business-and-cleanup'):
            with self.subTest(fault=fault):
                result,calls,first,control=self.run_fixture(fault)
                self.assertEqual(17,result.returncode);self.assertIn('rc=17 ',first);self.assertEqual('original',control)
                self.assertIn('source:clean',calls);self.assertNotIn('LONGHORN_SYSTEM_RESTORE_GATE_PASS',result.stdout)

    def test_marker_dead_relay_and_late_failures_cannot_report_pass(self):
        for fault in ('missing-marker','dead-relay','relay-clean','late-health'):
            with self.subTest(fault=fault):
                result,_,first,control=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
                self.assertNotIn('LONGHORN_SYSTEM_RESTORE_GATE_PASS',result.stdout)

    def test_cleanup_requires_owned_journal_and_never_repeats_business(self):
        result,calls,_,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('source:relay-ensure',calls)
        result,calls,first,_=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertIn('source:clean',calls);self.assertNotIn('source:business',calls)
        self.assertIn('LONGHORN_SYSTEM_RESTORE_CLEANUP_PASS',result.stdout)

    def test_cleanup_exit_or_marker_failure_is_durable_and_never_passes(self):
        for fault in ('cleanup','missing-clean-marker'):
            with self.subTest(fault=fault):
                result,calls,first,_=self.run_fixture(fault,cleanup=True,journal=True)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first);self.assertIn('source:clean',calls)
                self.assertNotIn('LONGHORN_SYSTEM_RESTORE_CLEANUP_PASS',result.stdout)


if __name__=='__main__':unittest.main()
