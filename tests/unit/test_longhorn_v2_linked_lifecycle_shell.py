"""Execute the fixed LONGHORN-49 supplement with isolated process fixtures."""
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LinkedLifecycleShell(unittest.TestCase):
    def test_fixed_public_dispatch_reaches_owned_gate_and_static_contracts(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        allowed = re.search(r'case "\$module" in ([^)]+)\)', runner).group(1).split('|')
        leased = re.search(r'case "\$mode" in ([^)]+)\) ;; \*\) exit 2', runner).group(1).split('|')
        for suffix in ('qualification', 'cleanup'):
            mode = 'v2-linked-lifecycle-'+suffix
            self.assertIn(mode, allowed); self.assertIn(mode, leased)
            result = subprocess.run(['bash', '-c', 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode], capture_output=True, text=True, check=True)
            self.assertEqual([mode, 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'], result.stdout.splitlines())
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-linked-lifecycle.sh" "$MODE"', live)
        self.assertIn('py_compile tests/helpers/longhorn-v2-linked-lifecycle.py', static)
        self.assertIn('tests.unit.test_longhorn_v2_linked_lifecycle_shell -v', static)

    def run_fixture(self, fault='', cleanup=False, journal=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('.venv/bin', 'tests/helpers', 'kube-bin', 'clusters/longhorn-v2-gate'):
                (root/name).mkdir(parents=True)
            if journal:
                (root/'clusters/longhorn-v2-gate/longhorn-v2-linked-lifecycle.json').write_text('{}')
            driver = root/'.venv/bin/python'
            driver.write_text('''#!/usr/bin/env python3
import os, signal, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[2]
mode = sys.argv[2]; fault = os.environ.get('LIFECYCLE_FAULT', '')
journal = root/'clusters/longhorn-v2-gate/longhorn-v2-linked-lifecycle.json'
with (root/'calls').open('a') as stream: stream.write(mode+'\\n')
if mode == 'relay-maintain':
 if fault == 'dead-relay': sys.exit(29)
 signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
 while True: time.sleep(0.01)
if mode == 'baseline':
 with (root/'baseline-calls').open('a') as stream: stream.write('x')
 changed = fault == 'baseline-change' and len((root/'baseline-calls').read_text()) > 1
 print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256='+('changed' if changed else 'original')+' backup_objects=0 fixture_objects=0')
if mode == 'business':
 journal.write_text('{}')
 if fault == 'business-rc': sys.exit(17)
 print('LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED {}')
 print('LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED {}')
 if fault == 'missing-marker': sys.exit(0)
 print('LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS')
 print('LONGHORN_V2_LINKED_LIFECYCLE_PASS')
 journal.unlink()
if mode == 'clean':
 assert '--relay-pid' in sys.argv and int(sys.argv[-1]) > 0
 if fault == 'cleanup-rc': sys.exit(19)
 journal.unlink(missing_ok=True)
 if fault != 'cleanup-missing-marker': print('LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS')
''')
            driver.chmod(0o755)
            kubectl = root/'kube-bin/kubectl'
            kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
            script = (ROOT/'tests/helpers/longhorn-v2-linked-lifecycle.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(root))
            path = root/'run.sh'; path.write_text(script)
            mode = 'v2-linked-lifecycle-cleanup' if cleanup else 'v2-linked-lifecycle-qualification'
            result = subprocess.run(['bash', str(path), mode], text=True, capture_output=True, timeout=10,
                env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', LIFECYCLE_FAULT=fault))
            work = Path(next(line.split('path=', 1)[1] for line in result.stdout.splitlines()
                             if line.startswith('LONGHORN_DURABLE_WORK ')))
            try:
                calls = (root/'calls').read_text().splitlines()
                status = (work/'status').read_text()
                first = (work/'first-failure.status').read_text() if (work/'first-failure.status').exists() else ''
                self.assertIn('rc='+str(result.returncode)+' ', status)
                return result, calls, first
            finally:
                shutil.rmtree(work)

    def test_success_retains_durable_status_and_verifies_original_baseline(self):
        result, calls, first = self.run_fixture()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, calls.count('baseline')); self.assertEqual(2, calls.count('health'))
        self.assertNotIn('clean', calls); self.assertFalse(first)
        self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_V2_LINKED_LIFECYCLE_QUALIFICATION_PASS'))

    def test_business_failure_preserves_first_rc_and_recovers_owned_journal(self):
        result, calls, first = self.run_fixture('business-rc')
        self.assertEqual(17, result.returncode)
        self.assertIn('clean', calls); self.assertIn('rc=17 ', first)
        self.assertNotIn('QUALIFICATION_PASS', result.stdout)

    def test_missing_business_marker_blocks_pass_and_recovers(self):
        result, calls, _ = self.run_fixture('missing-marker')
        self.assertNotEqual(0, result.returncode); self.assertIn('clean', calls)
        self.assertNotIn('QUALIFICATION_PASS', result.stdout)

    def test_cleanup_mode_needs_actual_journal_and_helper_clean_marker(self):
        result, calls, _ = self.run_fixture(cleanup=True)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('clean', calls); self.assertNotIn('CLEANUP_PASS', result.stdout)
        for fault in ('', 'cleanup-rc', 'cleanup-missing-marker'):
            with self.subTest(fault=fault):
                result, calls, first = self.run_fixture(fault, cleanup=True, journal=True)
                self.assertIn('clean', calls); self.assertNotIn('business', calls)
                self.assertEqual(fault == '', result.returncode == 0, result.stderr)
                self.assertEqual(fault == '', 'LONGHORN_V2_LINKED_LIFECYCLE_CLEANUP_PASS' in result.stdout)
                if fault: self.assertIn('rc='+str(result.returncode)+' ', first)

    def test_baseline_drift_or_dead_relay_blocks_success(self):
        for fault in ('baseline-change', 'dead-relay'):
            with self.subTest(fault=fault):
                result, calls, _ = self.run_fixture(fault)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('relay-clean', calls); self.assertIn('verify-core', calls)
                self.assertNotIn('QUALIFICATION_PASS', result.stdout)


if __name__ == '__main__': unittest.main()
