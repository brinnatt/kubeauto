"""Batch orchestration preserves both owning qualifications and durable fences."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT/'tests/helpers/longhorn-v2-interrupt-manager.sh'
BRANCHES = ['v2-interrupt-qualification', 'v2-manager-loss-qualification']


class BatchContracts(unittest.TestCase):
    def run_batch(self, directory, fault='', stage=BRANCHES[0], *, lease=True, locks=True):
        root = Path(directory)
        helpers = root/'tests/helpers'
        helpers.mkdir(parents=True)
        durable = (ROOT/'tests/helpers/run-durable-gate.sh').read_text()
        if fault in ('missing-exit', 'missing-finalized', 'bad-finalized'):
            # Exact durable-fence defects: a successful child cannot substitute
            # for the wrapper's two independent terminal status files.
            durable += '''
if [[ "$*" == *"$BATCH_FAULT_STAGE"* ]]; then
 case "$BATCH_FAULT" in
  missing-exit) rm -f "${STATE_PREFIX}.exit" ;;
  missing-finalized) rm -f "${STATE_PREFIX}.finalized" ;;
  bad-finalized) printf '17\\n' > "${STATE_PREFIX}.finalized" ;;
 esac
fi
'''
            durable = durable.replace('exit "$rc"', ': "$rc"')
        (helpers/'run-durable-gate.sh').write_text(durable)
        (helpers/'longhorn-v2-volumes.sh').write_text('''#!/usr/bin/env bash
set -eu
branch="$1"
printf '%s\\n' "$branch" >> "$BATCH_ROOT/stages"
marker="LONGHORN_${branch^^}";marker="${marker//-/_}"
if [[ "$branch" == "$BATCH_FAULT_STAGE" ]]; then
 case "$BATCH_FAULT" in
  rc) exit 17 ;;
  missing-clean) echo "${marker}_PASS";exit 0 ;;
  missing-pass) echo "${marker}_SCOPED_CLEAN_PASS";exit 0 ;;
  duplicate-pass) echo "${marker}_PASS" ;;
  false-success) echo LONGHORN_FIRST_FAILURE ;;
  error) echo 'ERROR: invalid prerequisite' ;;
 esac
fi
echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
''')
        script = SCRIPT.read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+str(root))
        script = script.replace('/var/lock/', str(root)+'/locks/')
        script = script.replace('/var/tmp/kubeauto-longhorn-v2-interrupt-manager.', str(root)+'/batch.')
        (root/'locks').mkdir()
        path = root/'run.sh'
        path.write_text(script)
        if locks:
            launch = '''exec 9>"$BATCH_ROOT/locks/kubeauto-ceph-priority.lock"
exec 7>"$BATCH_ROOT/locks/kubeauto-ceph-compute.lock"
exec 8>"$BATCH_ROOT/locks/kubeauto-redis-core-pool.lock"
flock -n 9; flock -n 7; flock -n 8
exec bash "$BATCH_ROOT/run.sh" v2-interrupt-manager-qualification'''
        else:
            launch = 'exec bash "$BATCH_ROOT/run.sh" v2-interrupt-manager-qualification'
        result = subprocess.run(['bash', '-ec', launch], text=True, capture_output=True, timeout=10,
            env=dict(os.environ, BATCH_ROOT=str(root), BATCH_FAULT=fault, BATCH_FAULT_STAGE=stage,
                     LONGHORN_PARENT_LEASE_HELD='yes' if lease else 'no'))
        stages = (root/'stages').read_text().splitlines() if (root/'stages').exists() else []
        return result, stages

    def test_both_existing_product_paths_run_once_with_individual_durable_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            result, stages = self.run_batch(directory)
            self.assertEqual(0, result.returncode, result.stdout+result.stderr)
            self.assertEqual(BRANCHES, stages)
            self.assertEqual(2, result.stdout.count('LONGHORN_V2_INTERRUPT_MANAGER_STAGE_PASS '))
            self.assertIn('LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_SCOPED_CLEAN_PASS', result.stdout)
            self.assertIn('LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS', result.stdout)
            work = next(Path(directory).glob('batch.*'))
            self.assertEqual(0o700, work.stat().st_mode & 0o777)
            for branch in BRANCHES:
                for suffix in ('exit', 'finalized'):
                    self.assertEqual('0\n', (work/(branch+'.'+suffix)).read_text())

    def test_each_stage_stops_at_first_failed_prerequisite_and_retains_durable_state(self):
        for stage in BRANCHES:
            for fault in ('rc', 'missing-clean', 'missing-pass', 'duplicate-pass', 'false-success', 'error',
                          'missing-exit', 'missing-finalized', 'bad-finalized'):
                with self.subTest(stage=stage, fault=fault), tempfile.TemporaryDirectory() as directory:
                    result, stages = self.run_batch(directory, fault, stage)
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(BRANCHES[:BRANCHES.index(stage)+1], stages)
                    self.assertNotIn('\nLONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS\n', result.stdout)
                    self.assertIn('phase='+stage, result.stderr)
                    work = next(Path(directory).glob('batch.*'))
                    self.assertTrue((work/'first-failure.status').is_file())
                    if fault == 'rc':
                        self.assertEqual(17, result.returncode)
                        self.assertEqual('17\n', (work/(stage+'.finalized')).read_text())

    def test_environment_claim_without_actual_inherited_lease_cannot_run_children(self):
        for lease, locks in ((False, True), (True, False)):
            with self.subTest(lease=lease, locks=locks), tempfile.TemporaryDirectory() as directory:
                result, stages = self.run_batch(directory, lease=lease, locks=locks)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], stages)

    def test_shell_syntax(self):
        result = subprocess.run(['bash', '-n', str(SCRIPT)], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_authoritative_dispatch_and_remote_route_keep_both_narrow_checks(self):
        script = '''source "$1"
longhorn_launch() { printf '%s\\n' "$@"; }
longhorn_dispatch --longhorn-v2-interrupt-manager-qualification
'''
        result = subprocess.run(['bash', '-ec', script, 'contract',
                                 str(ROOT/'tests/helpers/longhorn-runner.sh')], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(['v2-interrupt-manager-qualification',
                          'LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS'], result.stdout.splitlines())
        runner = (ROOT/'tests/run_enterprise_regression.sh').read_text()
        self.assertIn('if [[ "$MODE" == --longhorn-* ]]; then', runner)
        self.assertIn('longhorn_dispatch "$MODE"', runner)
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('v2-interrupt-manager-qualification)', live)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-interrupt-manager.sh" "$MODE"', live)
        self.assertIn('tests.unit.test_longhorn_v2_interrupt_manager',
                      (ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__ == '__main__':
    unittest.main()
