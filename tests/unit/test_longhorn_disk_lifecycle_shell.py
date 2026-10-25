"""Execute the V1 disk runner with isolated interpreter/process fixtures."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class DiskLifecycleShell(unittest.TestCase):
    def run_fixture(self,fault='',cleanup=False,journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('.venv/bin','tests/helpers','tests/fixtures','kube-bin','clusters/longhorn-gate'):(root/name).mkdir(parents=True)
            if journal:(root/'clusters/longhorn-gate/longhorn-disk-lifecycle.json').write_text('{}')
            shutil.copyfile(ROOT/'tests/fixtures/longhorn-v1-qualified-disks.txt',root/'tests/fixtures/longhorn-v1-qualified-disks.txt')
            if fault=='changed-allowlist':(root/'tests/fixtures/longhorn-v1-qualified-disks.txt').write_text('changed')
            driver=root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];mode=sys.argv[2];fault=os.environ.get('DISK_FAULT','')
journal=root/'clusters/longhorn-gate/longhorn-disk-lifecycle.json'
with (root/'calls').open('a') as stream:stream.write(mode+'\\n')
if mode=='relay-maintain':
 if fault=='dead-relay':sys.exit(29)
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(0.01)
if mode=='health' and fault=='health-after':
 with (root/'health-calls').open('a') as stream:stream.write('x')
 if len((root/'health-calls').read_text())>1:sys.exit(31)
if mode=='business':
 assert '--allowlist' in sys.argv and Path(sys.argv[-1]).read_bytes()==(root/'tests/fixtures/longhorn-v1-qualified-disks.txt').read_bytes()
 journal.write_text('{}')
 if fault=='business-rc':sys.exit(17)
 for marker in ('LONGHORN_DISK_DUPLICATE_FILESYSTEM_REFUSAL {}','LONGHORN_DISK_NATIVE_EVACUATION_VERIFIED {}','LONGHORN_DISK_REPLACEMENT_VERIFIED {}','LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS','LONGHORN_DISK_LIFECYCLE_PASS'):
  if fault=='missing-marker' and 'EVACUATION' in marker:continue
  print(marker)
 if fault!='missing-marker':journal.unlink()
if mode=='clean':
 assert '--relay-pid' in sys.argv and int(sys.argv[-1])>0
 if fault=='cleanup-rc':sys.exit(19)
 journal.unlink(missing_ok=True)
 if fault!='cleanup-missing-marker':print('LONGHORN_DISK_LIFECYCLE_SCOPED_CLEAN_PASS')
''')
            driver.chmod(0o755)
            kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            source=(ROOT/'tests/helpers/longhorn-disk-lifecycle.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root))
            path=root/'run.sh';path.write_text(source)
            result=subprocess.run(['bash',str(path),'disk-lifecycle-cleanup' if cleanup else 'disk-lifecycle'],
                text=True,capture_output=True,timeout=10,env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',DISK_FAULT=fault))
            work=Path(next(line.split('path=',1)[1] for line in result.stdout.splitlines() if line.startswith('LONGHORN_DISK_DURABLE_WORK ')))
            try:
                status=(work/'status').read_text();self.assertIn('rc='+str(result.returncode)+' ',status)
                first=(work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                calls=(root/'calls').read_text().splitlines()
                return result,calls,first
            finally:shutil.rmtree(work)

    def test_success_requires_all_product_markers_core_and_health_checks(self):
        result,calls,first=self.run_fixture()
        self.assertEqual(0,result.returncode,result.stderr);self.assertFalse(first)
        self.assertEqual(2,calls.count('verify-core'));self.assertEqual(2,calls.count('health'));self.assertNotIn('clean',calls)
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_DISK_LIFECYCLE_GATE_PASS'))

    def test_product_failure_preserves_first_rc_and_recovers_before_relay_cleanup(self):
        result,calls,first=self.run_fixture('business-rc')
        self.assertEqual(17,result.returncode);self.assertIn('rc=17 ',first)
        self.assertLess(calls.index('clean'),calls.index('relay-clean'))
        self.assertNotIn('LONGHORN_DISK_LIFECYCLE_GATE_PASS',result.stdout)

    def test_missing_native_proof_blocks_success(self):
        result,calls,first=self.run_fixture('missing-marker')
        self.assertNotEqual(0,result.returncode);self.assertTrue(first);self.assertIn('clean',calls)
        self.assertNotIn('LONGHORN_DISK_LIFECYCLE_GATE_PASS',result.stdout)

    def test_cleanup_requires_actual_journal_and_verified_scoped_exit(self):
        result,calls,_=self.run_fixture(cleanup=True)
        self.assertNotEqual(0,result.returncode);self.assertNotIn('clean',calls)
        for fault in ('','cleanup-rc','cleanup-missing-marker'):
            with self.subTest(fault=fault):
                result,calls,first=self.run_fixture(fault,cleanup=True,journal=True)
                self.assertNotIn('business',calls);self.assertIn('clean',calls)
                self.assertEqual(fault=='',result.returncode==0,result.stderr)
                self.assertEqual(fault=='','LONGHORN_DISK_LIFECYCLE_CLEANUP_PASS' in result.stdout)
                if fault=='':
                    self.assertEqual(1,calls.count('health'));self.assertLess(calls.index('clean'),calls.index('health'))
                if fault:self.assertTrue(first)

    def test_dead_relay_health_drift_or_unverified_allowlist_blocks_gate(self):
        for fault in ('dead-relay','health-after','changed-allowlist'):
            with self.subTest(fault=fault):
                result,calls,first=self.run_fixture(fault)
                self.assertNotEqual(0,result.returncode);self.assertTrue(first)
                self.assertNotIn('LONGHORN_DISK_LIFECYCLE_GATE_PASS',result.stdout)
                if fault=='changed-allowlist':self.assertNotIn('business',calls);self.assertNotIn('relay-ensure',calls)

    def test_allowlist_matches_original_accepted_18_disk_qualification(self):
        path=ROOT/'tests/fixtures/longhorn-v1-qualified-disks.txt'
        rows=[line.split('|') for line in path.read_text().splitlines()]
        allowed={'192.168.122.135','192.168.122.40','192.168.122.72','192.168.122.212','192.168.122.165','192.168.122.238'}
        self.assertEqual(18,len(rows));self.assertEqual(allowed,{r[0] for r in rows})
        for host in allowed:
            disks=[r for r in rows if r[0]==host];self.assertEqual(3,len(disks))
            self.assertEqual(3,len({r[2] for r in disks}))
            self.assertTrue(all(len(r)==7 and r[2].startswith('/dev/disk/by-path/') and
                                r[5].startswith('path:pci-') and int(r[6])==107374182400 for r in disks))
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual('fd50cf8ccb3b1f1e0b1efa0a1b7fc576cf84f897af6d5f59460cb87f412ee7c9',digest)
        self.assertIn(digest,(ROOT/'tests/helpers/longhorn-disk-lifecycle.sh').read_text())


if __name__=='__main__':unittest.main()
