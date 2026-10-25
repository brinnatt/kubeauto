"""Execute the real strict-mode upgrade wrapper without a cluster or mutation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class UpgradeShell(unittest.TestCase):
    def run_fixture(self,fault='',cleanup=False,journal=False,retiring=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-v2-gate'):(root/name).mkdir(parents=True)
            record=root/'clusters/longhorn-v2-gate/longhorn-upgrade-lifecycle.json'
            preparation=root/'clusters/longhorn-v2-gate/longhorn-v2-upgrade-preparation.json'
            if journal:
                record.write_text('{}');preparation.write_text('{}')
            if retiring: preparation.write_text('{"stage":"retiring-empty-new"}')
            control=root/'control-config';control.write_text('original')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('UPGRADE_FAULT','')
journal=root/'clusters/longhorn-v2-gate/longhorn-upgrade-lifecycle.json'
with (root/'calls').open('a') as stream:stream.write(action+'\\n')
if action=='relay-maintain':
 if fault=='dead-relay':sys.exit(29)
 if fault=='slow-relay':time.sleep(.2)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/('relay-ready-'+str(os.getpid()))).touch()
 while True:time.sleep(.01)
if action=='preflight':
 (root/'clusters/longhorn-v2-gate/longhorn-v2-upgrade-preparation.json').write_text('{}')
 if fault=='preflight':sys.exit(27)
 print('LONGHORN_V2_UPGRADE_TRANSITION_PREFLIGHT_PASS')
if action=='resume-preflight':
 if fault=='resume-preflight':sys.exit(28)
 print('LONGHORN_V2_UPGRADE_RETIREMENT_RESUME_PREFLIGHT_PASS')
 if fault=='post-upgrade':print('LONGHORN_V2_UPGRADE_POST_ROLLOUT_RESUME_READY')
# Fast cleanup may reach the fixture before Python relay startup completes.
# Wait for both registered signal handlers rather than timing the OS scheduler.
if action=='recovery-check' and fault=='abort-before-upgrade':
 print('LONGHORN_V2_UPGRADE_ABORT_PREFLIGHT_PASS source_version=1.12.0 rollout_attempted=false')
if action in ('prepare','clean','abort-clean','resume') and fault!='dead-relay':
 deadline=time.monotonic()+3
 while len(list(root.glob('relay-ready-*')))!=2:
  if time.monotonic()>=deadline:sys.exit(30)
  time.sleep(.01)
if action=='prepare':
 (root/'control-config').write_text('changed')
 if fault=='prepare':sys.exit(31)
 print('LONGHORN_V2_UPGRADE_TRANSITION_OLD_READY')
if action in ('business','resume'):
 journal.write_text('{}')
 if fault in ('term','term-and-cleanup'):
  os.kill(os.getppid(),signal.SIGTERM);sys.exit(143)
 if fault in ('business','business-and-cleanup'):sys.exit(17)
 for i in range(2 if action=='resume' else 3 if fault=='restore-count' else 4):print('LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED point='+str(i))
 print('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS source_version=1.12.1')
 print('LONGHORN_V2_UPGRADE_PASS')
 print('LONGHORN_V2_UPGRADE_LEGACY_VERIFIED real_upgrade=true')
 if fault!='missing-marker':print('LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true')
 journal.unlink()
if action=='abort-clean':
 assert journal.is_file()
 print('LONGHORN_V2_UPGRADE_ABORT_SCOPED_CLEAN_PASS source_version=1.12.0 rollout_attempted=false');journal.unlink()
if action=='clean':
 assert journal.is_file()
 if fault in ('cleanup','business-and-cleanup','term-and-cleanup'):sys.exit(19)
 print('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS source_version=1.12.1');journal.unlink()
if action=='relay-clean' and fault=='relay-clean':sys.exit(21)
if action=='finalize':
 assert (root/'control-config').read_text()=='original'
 if fault=='finalize':sys.exit(23)
 print('LONGHORN_V2_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS original_core=true')
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            source=(ROOT/'tests/helpers/longhorn-v2-upgrade-lifecycle.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(control))
            script=root/'run.sh';script.write_text(source)
            result=subprocess.run(['bash',str(script),'v2-upgrade-cleanup' if cleanup else 'v2-upgrade-lifecycle'],text=True,capture_output=True,timeout=10,
                env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',UPGRADE_FAULT=fault))
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines() if line.startswith('LONGHORN_V2_UPGRADE_DURABLE_WORK ')))
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
        self.assertEqual(1,calls.count('business'));self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_V2_UPGRADE_GATE_PASS'))

    def test_preflight_failure_cannot_start_or_remove_relays_or_uninstall(self):
        result,calls,first,control=self.run_fixture('preflight')
        self.assertEqual(27,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
        for action in ('relay-ensure','relay-clean','prepare','business','finalize'):self.assertNotIn(action,calls)

    def test_existing_retirement_is_validated_before_transport_then_uses_original_prepare(self):
        result,calls,first,_=self.run_fixture(retiring=True)
        self.assertEqual(0,result.returncode,result.stderr); self.assertFalse(first)
        self.assertNotIn('preflight',calls)
        self.assertLess(calls.index('resume-preflight'),calls.index('relay-ensure'))
        self.assertLess(calls.index('prepare'),calls.index('business'))
        result,calls,first,_=self.run_fixture('resume-preflight',retiring=True)
        self.assertEqual(28,result.returncode);self.assertTrue(first)
        for action in ('relay-ensure','prepare','business','finalize'):self.assertNotIn(action,calls)

    def test_failed_prepare_retains_stage_without_business_cleanup_or_false_pass(self):
        result,calls,first,control=self.run_fixture('prepare')
        self.assertEqual(31,result.returncode);self.assertTrue(first);self.assertEqual('original',control)
        for action in ('business','clean','finalize'):self.assertNotIn(action,calls)

    def test_business_first_failure_survives_cleanup_failure(self):
        for fault in ('business','business-and-cleanup'):
            with self.subTest(fault=fault):
                result,calls,first,_=self.run_fixture(fault)
                self.assertEqual(17,result.returncode);self.assertIn('rc=17 ',first);self.assertIn('clean',calls)
                self.assertNotIn('finalize',calls);self.assertNotIn('LONGHORN_V2_UPGRADE_GATE_PASS',result.stdout)

    def test_markers_restore_count_and_late_cleanup_or_relay_failure_cannot_pass(self):
        for fault in ('restore-count','missing-marker','dead-relay','relay-clean','finalize'):
            with self.subTest(fault=fault):
                result,_,first,_=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('LONGHORN_V2_UPGRADE_GATE_PASS',result.stdout)

    def test_sigterm_preserves_143_and_owned_cleanup_and_exact_control_context(self):
        for fault in ('term','term-and-cleanup'):
            with self.subTest(fault=fault):
                result,calls,first,control=self.run_fixture(fault)
                self.assertEqual(143,result.returncode,result.stderr);self.assertIn('rc=143 ',first)
                self.assertEqual('original',control);self.assertIn('clean',calls)
                self.assertNotIn('finalize',calls);self.assertNotIn('LONGHORN_V2_UPGRADE_GATE_PASS',result.stdout)

    def test_explicit_before_upgrade_abort_skips_finalize_and_never_claims_business_pass(self):
        result,calls,first,control=self.run_fixture('abort-before-upgrade',cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first);self.assertEqual('original',control)
        self.assertLess(calls.index('recovery-check'),calls.index('relay-ensure'))
        self.assertIn('abort-clean',calls)
        for action in ('clean','prepare','business','finalize'):self.assertNotIn(action,calls)
        self.assertIn('LONGHORN_V2_UPGRADE_ABORT_SCOPED_CLEAN_PASS ',result.stdout)
        self.assertNotIn('LONGHORN_V2_UPGRADE_GATE_PASS',result.stdout)

    def test_fast_cleanup_waits_for_both_slow_fixture_relay_signal_handlers(self):
        result,calls,first,control=self.run_fixture('slow-relay',cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertFalse(first)
        self.assertEqual('original',control)
        self.assertEqual(2,calls.count('relay-maintain'))
        self.assertIn('clean',calls)
        self.assertIn('LONGHORN_V2_UPGRADE_CLEANUP_PASS',result.stdout)

    def test_cleanup_needs_owned_journal_and_never_repeats_transition_or_business(self):
        result,calls,_,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('relay-ensure',calls)
        result,calls,first,_=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        for action in ('preflight','prepare','business'):self.assertNotIn(action,calls)
        self.assertIn('recovery-check',calls);self.assertIn('clean',calls);self.assertIn('finalize',calls)
        self.assertIn('LONGHORN_V2_UPGRADE_CLEANUP_PASS',result.stdout)



    def test_post_upgrade_handoff_skips_empty_retirement_and_requires_real_two_restore_tail(self):
        result,calls,first,_=self.run_fixture('post-upgrade',retiring=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertNotIn('prepare',calls);self.assertNotIn('business',calls)
        self.assertLess(calls.index('resume-preflight'),calls.index('relay-ensure'))
        self.assertLess(calls.index('relay-ensure'),calls.index('resume'))
        self.assertLess(calls.index('resume'),calls.index('finalize'))
        self.assertEqual(2,result.stdout.count('LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '))


if __name__=='__main__':unittest.main()
