"""Run each real DR shell through the original durable wrapper, with local fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
CANDIDATE=Path(os.environ.get('PROTECTION_CONTEXT_CANDIDATE',str(ROOT/'tests/helpers')))
FIXTURE=r'''import json,os,pathlib,signal,sys,time
root=pathlib.Path(__file__).resolve().parents[2]
action=sys.argv[1]
assert os.environ['KUBECONFIG']==str(root/'clusters/longhorn-v2-dr-target/kubectl.kubeconfig')
if action=='source-boundary': print('LONGHORN_V2_DR_SOURCE_BOUNDARY unchanged')
if action=='target-baseline': print('LONGHORN_V2_DR_TARGET_STORE_BASELINE unchanged')
if action=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/('relay-'+pathlib.Path(__file__).stem)).touch()
 while True:time.sleep(.01)
if action=='qualify':
 deadline=time.monotonic()+2
 while len(list(root.glob('relay-*')))!=2:
  assert time.monotonic()<deadline
  time.sleep(.01)
 p=root/'shared-context';other=root/'foreign-update';other.write_bytes(b'new-concurrent-owner-context')
 os.replace(other,p);st=p.stat()
 (root/'expected-context').write_text(json.dumps([st.st_ino,st.st_mtime_ns,p.read_bytes().hex()]))
 if os.environ['TEST_BUSINESS_FAIL']=='yes':sys.exit(7)
 name=pathlib.Path(__file__).name
 if name=='longhorn-v2-dr-store.py':
  for line in ('LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact','LONGHORN_V2_DR_SOURCE_BACKUP_TRANSPORT_ABSENT shared_server=retained','LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false','LONGHORN_V2_DR_TARGET_SHARED_STORE_VERIFIED target_available=true independent_endpoint=true','LONGHORN_V2_DR_TARGET_STORE_CONSUMER_CLEAN_PASS shared_server=retained','LONGHORN_BACKUP_STORE_CLEAN_PASS'):print(line)
 elif name=='longhorn-v2-disaster.py':
  for line in ('LONGHORN_V2_DR_STANDBY_SYNC_VERIFIED points=2 same_volume=true frontend=false pv=false','LONGHORN_V2_DR_CACHE_RETIRED side=target external_backups=preserved','LONGHORN_V2_DR_CACHE_RETIRED side=source external_backups=preserved','LONGHORN_V2_DR_STANDBY_FIXTURES_CLEAN_PASS','LONGHORN_V2_DR_STANDBY_BUSINESS_PASS','LONGHORN_V2_DR_STANDBY_RESTORED point=1','LONGHORN_V2_DR_STANDBY_RESTORED point=2'):print(line)
 else:
  for line in ('LONGHORN_V2_DR_WORKFLOW_UNFENCED_ACTIVATION_REFUSED native_api_not_called=true','LONGHORN_V2_DR_CACHE_RETIRED side=target external_backups=preserved','LONGHORN_V2_DR_CACHE_RETIRED side=source external_backups=preserved','LONGHORN_V2_DR_FAILOVER_FIXTURES_CLEAN_PASS','LONGHORN_V2_DR_ACTIVATION_BUSINESS_PASS','LONGHORN_V2_DR_SOURCE_FENCED verified=true','LONGHORN_V2_DR_ACTIVATED_BUSINESS_VERIFIED verified=true'):print(line)
'''

class ProtectionContextContracts(unittest.TestCase):
    def test_real_dr_shells_preserve_foreign_context_on_success_and_failure(self):
        scenarios=(('longhorn-v2-dr-store.sh','v2-dr-store-qualification'),
                   ('longhorn-v2-disaster.sh','v2-dr-standby-qualification'),
                   ('longhorn-v2-dr-failover.sh','v2-dr-activation-qualification'))
        for name,mode in scenarios:
            for failed in (False,True):
                with self.subTest(shell=name,failed=failed),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);helpers=root/'tests/helpers';helpers.mkdir(parents=True)
                    (root/'.venv/bin').mkdir(parents=True)
                    (root/'.venv/bin/python').symlink_to(sys.executable)
                    (root/'kube-bin').mkdir();kubectl=root/'kube-bin/kubectl'
                    kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o700)
                    (root/'clusters/longhorn-v2-dr-target').mkdir(parents=True)
                    context=root/'shared-context';context.write_bytes(b'original-owner-context')
                    original_inode=context.stat().st_ino
                    script=(CANDIDATE/name).read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root))
                    script=script.replace('/var/tmp/kubeauto-longhorn-v2-',str(root/'runtime-'))
                    script=script.replace('/root/.kube/config',str(context))
                    (helpers/name).write_text(script)
                    (helpers/'run-durable-gate.sh').write_bytes((ROOT/'tests/helpers/run-durable-gate.sh').read_bytes())
                    for helper in ('longhorn-v2-core.py','longhorn-v2-install.py','longhorn-v2-dr-target.py',
                                   'longhorn-v2-dr-store.py','longhorn-v2-disaster.py','longhorn-v2-dr-failover.py'):
                        (helpers/helper).write_text(FIXTURE)
                    result=subprocess.run(['bash',str(helpers/'run-durable-gate.sh'),str(root/'owning'),
                        'LONGHORN_GATE_EXIT','bash',str(helpers/name),mode],
                        env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',TEST_BUSINESS_FAIL='yes' if failed else 'no'),
                        capture_output=True,text=True,timeout=7)
                    self.assertEqual(result.returncode,7 if failed else 0,result.stdout+result.stderr)
                    st=context.stat();self.assertNotEqual(st.st_ino,original_inode)
                    self.assertEqual([st.st_ino,st.st_mtime_ns,context.read_bytes().hex()],
                                     json.loads((root/'expected-context').read_text()))
                    for suffix in ('exit','finalized'):
                        self.assertEqual((root/('owning.'+suffix)).read_text().strip(),'7' if failed else '0')
                    self.assertFalse(any(root.glob('runtime-*/control-kubeconfig')))
                    self.assertEqual(mode.upper().replace('-','_')+'_PASS' in result.stdout,not failed)

if __name__=='__main__':unittest.main(verbosity=2)
