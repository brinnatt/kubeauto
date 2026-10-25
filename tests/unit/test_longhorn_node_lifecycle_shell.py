"""Real strict-mode shell execution with isolated interpreter/relay fixtures."""
import os
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class NodeLifecycleShell(unittest.TestCase):
    def test_current_launcher_is_bound_by_retirement_evidence_transfer(self):
        # Source sync always validates this finite map before launching a gate.
        # Check its real input too; isolated stdin fixtures cannot catch a stale pin.
        impact = json.loads((ROOT / 'tests/evidence/longhorn-linked-legacy-dependency-impact-20261024.json').read_text())
        name = 'tests/helpers/longhorn-runner.sh'
        self.assertEqual(impact['changes'][name]['sha256'],
                         hashlib.sha256((ROOT / name).read_bytes()).hexdigest())

    def test_launcher_transfers_release_proof_as_a_separate_stdin_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers'):(root/name).mkdir(parents=True)
            for name in ('.venv/bin/python','tests/helpers/sync-kubeauto.sh'):
                path=root/name;path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o755)
            script='''set -Eeuo pipefail
source "$1"
ROOT="$2"
longhorn_local_gate() { :; }
ssh_longhorn() {
 if [[ "$1" == *LONGHORN_LEASE_READY* ]]; then
  printf 'LONGHORN_LEASE_READY\\n'
  IFS= read -r mode
  IFS= read -r proof
  printf '%s\\n%s\\n' "$mode" "$proof" > "$ROOT/received"
  if [[ "$mode" == node-lifecycle ]]; then
    IFS= read -r target
    printf '%s\\n' "$target" >> "$ROOT/received"
  fi
  printf 'LONGHORN_DURABLE_LAUNCHED\\n'
 fi
}
monitor_remote_job() { :; }
remote_job_summary() { :; }
longhorn_launch "$CONTRACT_MODE" CONTRACT_PASS
'''
            for mode,variable,scope,address in (
                ('node-lifecycle','LONGHORN_V2_RELEASE_PROOF','source','148'),
                ('node-lifecycle','LONGHORN_V2_TARGET_RELEASE_PROOF','target','149'),
                ('v2-disk-lifecycle','LONGHORN_V2_TARGET_RELEASE_PROOF','target','149')):
                valid='/var/tmp/kubeauto-longhorn-v2-retirement.Ab1234/release-proofs/longhorn-v2-'+scope+'-released-192.168.47.'+address+'.json'
                for proof in (valid,valid+'\nother',valid.replace('.'+address,'.150'),'$(touch '+str(root/'injected')+')',''):
                    with self.subTest(mode=mode,proof=proof):
                        received=root/'received';received.unlink(missing_ok=True)
                        prefix='/var/tmp/kubeauto-longhorn-v2-retirement.Ab1234/release-proofs/'
                        source=prefix+'longhorn-v2-source-released-192.168.47.148.json'
                        target=prefix+'longhorn-v2-target-released-192.168.47.149.json'
                        env=dict(os.environ,CONTRACT_MODE=mode,LONGHORN_V2_RELEASE_PROOF=source,LONGHORN_V2_TARGET_RELEASE_PROOF=target)
                        env[variable]=proof
                        result=subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh'),str(root)],
                            env=env,capture_output=True,text=True,timeout=5)
                        self.assertEqual(proof==valid,result.returncode==0,result.stderr)
                        self.assertEqual(proof==valid,received.exists())
                        if proof==valid:self.assertEqual([mode,source,target] if mode=='node-lifecycle' else [mode,target],received.read_text().splitlines())
                        self.assertFalse((root/'injected').exists())

    def run_fixture(self,fault='',cleanup=False,journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-gate'):(root/name).mkdir(parents=True)
            if journal:(root/'clusters/longhorn-gate/longhorn-node-lifecycle.json').write_text('{}')
            proof=root/'released.json';proof.write_text('{}')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('NODE_FAULT','')
journal=root/'clusters/longhorn-gate/longhorn-node-lifecycle.json'
with (root/'calls').open('a') as stream:stream.write(action+'\\n')
if action=='relay-maintain':
 if fault=='dead-relay':sys.exit(29)
 time.sleep(.05)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/'relay-ready').touch()
 while True:time.sleep(.01)
if action=='admit':
 assert '--released-v2-proof' in sys.argv and Path(sys.argv[-1]).is_file()
 if fault=='rejected-proof':sys.exit(27)
 journal.write_text('{}')
if action=='recover-partial-join':
 assert journal.is_file()
 if fault=='recovery-refusal':sys.exit(31)
if action=='business':
 assert journal.is_file()
 if fault in ('business-rc','business-and-cleanup-rc'):sys.exit(17)
 for marker in ('LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4',
  'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node','LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master',
  'LONGHORN_NODE_NATIVE_EVACUATION_RETIRED host=192.168.47.148',
  'LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS','LONGHORN_NODE_LIFECYCLE_PASS'):
  if fault=='missing-marker' and 'del-master' in marker:continue
  print(marker)
if action=='clean':
 assert journal.is_file() and '--relay-pid' in sys.argv
 for _ in range(200):
  if fault=='dead-relay' or (root/'relay-ready').exists():break
  time.sleep(.005)
 else:sys.exit(37)
 if fault in ('cleanup-rc','business-and-cleanup-rc'):sys.exit(19)
 if fault!='cleanup-missing-marker':print('LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS')
if action=='finalize':
 assert journal.is_file()
 if fault=='finalize-rc':sys.exit(23)
 journal.unlink()
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            source=(ROOT/'tests/helpers/longhorn-node-lifecycle.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root))
            script=root/'run.sh';script.write_text(source)
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LONGHORN_V2_RELEASE_PROOF=str(proof),NODE_FAULT=fault)
            if fault=='missing-proof':env.pop('LONGHORN_V2_RELEASE_PROOF')
            result=subprocess.run(['bash',str(script),'node-lifecycle-cleanup' if cleanup else 'node-lifecycle'],
                text=True,capture_output=True,timeout=10,env=env)
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines() if line.startswith('LONGHORN_NODE_DURABLE_WORK ')))
            try:
                status=(work/'status').read_text();self.assertIn('rc='+str(result.returncode)+' ',status)
                first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                return result,(root/'calls').read_text().splitlines(),first
            finally:shutil.rmtree(work)

    def test_normal_path_requires_all_markers_cleanup_and_finalization(self):
        result,calls,first=self.run_fixture()
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertLess(calls.index('admit'),calls.index('relay-ensure'))
        self.assertLess(calls.index('clean'),calls.index('relay-clean'))
        self.assertLess(calls.index('relay-clean'),calls.index('finalize'))
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_NODE_LIFECYCLE_GATE_PASS'))

    def test_missing_or_rejected_release_proof_prevents_remote_mutation(self):
        for fault in ('missing-proof','rejected-proof'):
            with self.subTest(fault=fault):
                result,calls,first=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('relay-ensure',calls);self.assertNotIn('business',calls)

    def test_business_failure_preserves_first_rc_even_when_cleanup_fails(self):
        for fault in ('business-rc','business-and-cleanup-rc'):
            result,calls,first=self.run_fixture(fault)
            self.assertEqual(17,result.returncode,result.stderr);self.assertIn('rc=17 ',first)
            self.assertIn('clean',calls);self.assertNotIn('finalize',calls)
            self.assertNotIn('LONGHORN_NODE_LIFECYCLE_GATE_PASS',result.stdout)

    def test_missing_guard_proof_or_failed_cleanup_finalization_and_relay_cannot_pass(self):
        for fault in ('missing-marker','cleanup-rc','cleanup-missing-marker','dead-relay','finalize-rc'):
            with self.subTest(fault=fault):
                result,calls,first=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('LONGHORN_NODE_LIFECYCLE_GATE_PASS',result.stdout)

    def test_cleanup_requires_real_journal_and_never_repeats_admission_or_business(self):
        result,calls,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('relay-ensure',calls)
        result,calls,first=self.run_fixture(cleanup=True,journal=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertNotIn('admit',calls);self.assertNotIn('business',calls)
        self.assertLess(calls.index('recover-partial-join'),calls.index('relay-ensure'))
        self.assertIn('clean',calls);self.assertIn('finalize',calls)
        self.assertIn('LONGHORN_NODE_LIFECYCLE_CLEANUP_PASS',result.stdout)

    def test_partial_recovery_refusal_stops_before_transport_or_cleanup(self):
        result,calls,first=self.run_fixture('recovery-refusal',cleanup=True,journal=True)
        self.assertEqual(31,result.returncode,result.stderr);self.assertTrue(first)
        self.assertNotIn('relay-ensure',calls);self.assertNotIn('clean',calls)
        self.assertNotIn('finalize',calls)


if __name__=='__main__':unittest.main()
