"""Exercise the V2 drain wrapper without a cluster or shared lab state."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DrainShellContracts(unittest.TestCase):
    def test_public_dispatch_and_both_lease_allowlists_reach_the_owned_gate(self):
        runner = ROOT/'tests/helpers/longhorn-runner.sh'
        source = runner.read_text()
        for suffix in ('qualification', 'cleanup'):
            mode = 'v2-maintenance-drain-'+suffix
            for variable in ('module', 'mode'):
                allowed = re.search(r'case "\$'+variable+r'" in ([^)]+)\)', source).group(1).split('|')
                self.assertIn(mode, allowed)
            result = subprocess.run(['bash','-c',
                'source "$1"; longhorn_launch() { printf "%s %s" "$1" "$2"; }; longhorn_dispatch "$2"',
                'contract',str(runner),'--longhorn-'+mode],capture_output=True,text=True,check=True)
            self.assertEqual(mode+' LONGHORN_'+mode.upper().replace('-','_')+'_PASS',result.stdout)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-maintenance-drain.sh" "$MODE"',
                      (ROOT/'tests/helpers/longhorn-live.sh').read_text())

    def run_fixture(self, fault='', cleanup=False, journal=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ('.venv/bin', 'tests/helpers', 'kube-bin', 'clusters/longhorn-v2-gate'):
                (root/directory).mkdir(parents=True)
            if journal:
                (root/'clusters/longhorn-v2-gate/longhorn-v2-maintenance-drain.json').write_text('{}')
            python = root/'.venv/bin/python'
            python.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2]
mode=sys.argv[2];fault=os.environ.get('DRAIN_FAULT','')
if mode=='relay-maintain':signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
with (root/'calls').open('a') as stream:stream.write(mode+'\\n')
if mode=='relay-maintain':
 (root/'relay-ready').write_text('ready')
 while True:time.sleep(.01)
if mode in ('business','clean'):
 deadline=time.monotonic()+2
 while not (root/'relay-ready').exists():
  assert time.monotonic()<deadline
  time.sleep(.005)
if mode=='business':
 if fault=='term':os.kill(os.getpid(),signal.SIGTERM)
 if fault=='business':sys.exit(17)
 if fault!='marker':print('LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS')
if mode=='finish':print('LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS')
if mode=='clean' and fault=='clean':sys.exit(19)
''')
            python.chmod(0o755)
            kubectl = root/'kube-bin/kubectl'
            kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
            script = root/'run.sh'
            script.write_text((ROOT/'tests/helpers/longhorn-v2-maintenance-drain.sh').read_text().replace(
                'BASE=/usr/local/kubeauto', 'BASE='+str(root)))
            mode = 'v2-maintenance-drain-'+('cleanup' if cleanup else 'qualification')
            result = subprocess.run(['bash', str(script), mode], capture_output=True, text=True, timeout=10,
                env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', DRAIN_FAULT=fault))
            work = Path(next(line.split('path=', 1)[1] for line in result.stdout.splitlines()
                             if line.startswith('LONGHORN_MAINTENANCE_DURABLE_DIRECTORY ')))
            try:
                self.assertEqual(str(result.returncode), (work/'exit').read_text().strip())
                return result, (root/'calls').read_text().splitlines()
            finally:
                shutil.rmtree(work)

    def test_success_requires_both_business_and_cleanup_markers(self):
        result, calls = self.run_fixture()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('finish', calls)
        self.assertNotIn('clean', calls)
        self.assertIn('LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS', result.stdout)

    def test_failure_and_real_sigterm_recover_without_pass(self):
        for fault, expected in (('business',17), ('term',143), ('marker',1)):
            with self.subTest(fault=fault):
                result, calls = self.run_fixture(fault)
                self.assertEqual(expected, result.returncode, result.stderr)
                self.assertIn('clean', calls)
                self.assertIn('relay-clean', calls)
                self.assertNotIn('LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS', result.stdout)

    def test_cleanup_needs_owned_journal_and_keeps_its_own_marker(self):
        result, calls = self.run_fixture(cleanup=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('clean', calls); self.assertNotIn('business', calls)
        self.assertIn('LONGHORN_V2_MAINTENANCE_DRAIN_CLEANUP_PASS', result.stdout)
        for fault, journal in (('clean',True), ('',False)):
            with self.subTest(fault=fault,journal=journal):
                result, calls = self.run_fixture(fault, cleanup=True, journal=journal)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('LONGHORN_V2_MAINTENANCE_DRAIN_CLEANUP_PASS', result.stdout)


if __name__ == '__main__': unittest.main()
