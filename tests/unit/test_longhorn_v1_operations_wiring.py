"""Exercise public routing and recovery shells without an SSH or cluster client."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULES = ('maintenance', 'capacity', 'integrity', 'disk-lifecycle')


class OperationsWiring(unittest.TestCase):
    def test_public_modes_have_exact_dispatch_lease_and_remote_owner(self):
        runner = ROOT/'tests/helpers/longhorn-runner.sh'
        text = runner.read_text()
        local_modes = re.search(r'case "\$module" in (.*?)esac', text, re.S).group(1)
        lease_modes = re.search(r'case "\$mode" in ([^)]+)\)', text).group(1).split('|')
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        for name in MODULES:
            for suffix in ('', '-cleanup'):
                mode = name+suffix
                with self.subTest(mode=mode):
                    result = subprocess.run(['bash', '-c',
                        'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                        'contract', str(runner), '--longhorn-'+mode],
                        capture_output=True, text=True, check=True)
                    marker = 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'
                    if mode == 'disk-lifecycle': marker = 'LONGHORN_DISK_LIFECYCLE_GATE_PASS'
                    self.assertEqual([mode, marker], result.stdout.splitlines())
                    self.assertIn(mode, local_modes)
                    self.assertIn(mode, lease_modes)
                    self.assertIn(name+'|'+name+'-cleanup)', live)
                    self.assertIn('longhorn-'+name+'.sh" "$MODE"', live)

    def fixture(self, name, *, cleanup=False, fault='', journal=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in ('.venv/bin', 'tests/helpers', 'kube-bin', 'clusters/longhorn-gate'):
                (root/part).mkdir(parents=True)
            if journal: (root/'clusters/longhorn-gate'/('longhorn-'+name+'.json')).write_text('{}')
            python = root/'.venv/bin/python'
            python.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];action=sys.argv[2];fault=os.environ.get('OP_FAULT','')
with (root/'calls').open('a') as stream:stream.write(action+'\\n')
if action=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/'relay.ready').touch()
 if fault=='dead-relay':sys.exit(22)
 while True:time.sleep(.01)
if action=='clean':
 assert int(sys.argv[-1])>0
 for _ in range(200):
  if (root/'relay.ready').exists():break
  time.sleep(.005)
 if fault=='clean':sys.exit(19)
if action=='business' and fault=='business':sys.exit(17)
if action=='finish' and fault=='finish':sys.exit(18)
''')
            python.chmod(0o755)
            kubectl = root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            script = root/'run.sh'
            script.write_text((ROOT/'tests/helpers'/('longhorn-'+name+'.sh')).read_text().replace(
                'BASE=/usr/local/kubeauto', 'BASE='+str(root)))
            result = subprocess.run(['bash', str(script), name+('-cleanup' if cleanup else '')],
                env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', OP_FAULT=fault),
                capture_output=True, text=True, timeout=10)
            work = Path(next(l.split('path=', 1)[1] for l in result.stdout.splitlines()
                             if '_DURABLE_DIRECTORY path=' in l))
            try:
                self.assertEqual(str(result.returncode), (work/'exit').read_text().strip())
                first = (work/'first-failure.status').exists()
                return result, (root/'calls').read_text().splitlines(), first
            finally:
                shutil.rmtree(work)

    def test_recovery_never_runs_business_or_claims_the_normal_pass(self):
        for name in MODULES[:3]:
            with self.subTest(name=name):
                result, calls, first = self.fixture(name, cleanup=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(first)
                self.assertIn('clean', calls);self.assertNotIn('business', calls);self.assertNotIn('finish', calls)
                self.assertLess(calls.index('clean'), calls.index('relay-clean'))
                self.assertIn('LONGHORN_'+name.upper()+'_CLEANUP_PASS', result.stdout.splitlines())
                self.assertNotIn('LONGHORN_'+name.upper()+'_PASS', result.stdout.splitlines())

    def test_missing_journal_or_failed_restoration_cannot_pass(self):
        for name in MODULES[:3]:
            for fault, journal in (('', False), ('clean', True), ('dead-relay', True)):
                with self.subTest(name=name, fault=fault, journal=journal):
                    result, calls, first = self.fixture(name, cleanup=True, fault=fault, journal=journal)
                    self.assertNotEqual(0, result.returncode)
                    self.assertNotIn('LONGHORN_'+name.upper()+'_CLEANUP_PASS', result.stdout.splitlines())
                    self.assertNotIn('business', calls)

    def test_normal_failure_recovers_before_transport_and_preserves_first_rc(self):
        for name in MODULES[:3]:
            for fault, rc in (('business', 17), ('finish', 18)):
                with self.subTest(name=name, fault=fault):
                    result, calls, first = self.fixture(name, fault=fault)
                    self.assertEqual(rc, result.returncode)
                    self.assertTrue(first)
                    self.assertLess(calls.index('clean'), calls.index('relay-clean'))
                    self.assertNotIn('LONGHORN_'+name.upper()+'_PASS', result.stdout.splitlines())


if __name__ == '__main__':
    unittest.main()
