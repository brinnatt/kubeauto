"""Local contracts for the fixed six-stage ownership lifecycle."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'tests/helpers/longhorn-ownership-aggregate.sh'
STAGES = (
    'ownership-csi-refusal-qualification',
    'ownership-preinstall-refusal-qualification',
    'install',
    'ownership-class-refusal-qualification',
    'ownership-controller-refusal-qualification',
    'ownership-request-refusal-qualification',
)


class OwnershipAggregateContracts(unittest.TestCase):
    def test_public_dispatch_transport_lease_and_static_registration(self):
        runner = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT / 'tests/helpers/longhorn-static.sh').read_text()
        self.assertIn('--longhorn-ownership) longhorn_launch ownership LONGHORN_OWNERSHIP_PASS', runner)
        for name in ('module', 'mode'):
            # Admission is set membership; ordering unrelated modes is immaterial.
            admitted = re.search(r'case "\$'+name+r'" in ([^)]+)\)', runner)
            self.assertIsNotNone(admitted)
            self.assertIn('ownership', admitted.group(1).split('|'))
        self.assertIn('"$module" == ownership', runner)
        self.assertIn('ownership)\n    exec bash "$BASE/tests/helpers/longhorn-ownership-aggregate.sh" "$MODE"', live)
        self.assertIn('tests.unit.test_longhorn_ownership_aggregate', static)

    def verify(self, directory, stage):
        return subprocess.run(['bash', '-c',
            'source "$1"; PY="$2"; verify_stage "$3" "$4"',
            'contract', str(HELPER), sys.executable, str(directory), stage],
            capture_output=True, text=True)

    def fixture(self, directory, stage):
        prefix = Path(directory) / stage
        marker = 'LONGHORN_' + stage.upper().replace('-', '_')
        for suffix, content in (
                ('.exit', '0\n'), ('.finalized', '0\n'),
                ('.log', '\n'.join((marker + '_SCOPED_CLEAN_PASS', marker + '_PASS',
                                   marker + '_EXIT rc=0')) + '\n')):
            path = prefix.with_suffix(suffix)
            path.write_text(content)
            path.chmod(0o600)
        return prefix

    def test_each_existing_stage_requires_its_own_durable_success(self):
        for stage in STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                self.fixture(directory, stage)
                result = self.verify(directory, stage)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(),
                    'LONGHORN_OWNERSHIP_STAGE_VERIFIED stage=' + stage + ' durable_rc=0 finalized=0')

    def test_nonzero_missing_fence_and_bad_markers_cannot_be_promoted(self):
        for defect in ('exit', 'finalized', 'missing-fence', 'missing-marker', 'duplicate', 'failure'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                stage = STAGES[0]
                prefix = self.fixture(directory, stage)
                log = prefix.with_suffix('.log')
                if defect in ('exit', 'finalized'):
                    prefix.with_suffix('.' + defect).write_text('7\n')
                elif defect == 'missing-fence':
                    prefix.with_suffix('.finalized').unlink()
                elif defect == 'missing-marker':
                    log.write_text('\n'.join(log.read_text().splitlines()[1:]) + '\n')
                elif defect == 'duplicate':
                    log.write_text(log.read_text() + log.read_text().splitlines()[0] + '\n')
                else:
                    log.write_text(log.read_text() + 'LONGHORN_FIRST_FAILURE phase=fixture rc=1\n')
                self.assertNotEqual(self.verify(directory, stage).returncode, 0)

    def test_unknown_stage_symlink_and_public_evidence_are_rejected(self):
        for defect in ('unknown', 'symlink-file', 'symlink-directory', 'public'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                stage = STAGES[0]
                prefix = self.fixture(directory, stage)
                target_directory = Path(directory)
                if defect == 'unknown':
                    stage = 'ownership-interruption-qualification'
                elif defect == 'symlink-file':
                    path = prefix.with_suffix('.exit')
                    path.unlink()
                    path.symlink_to(prefix.with_suffix('.finalized'))
                elif defect == 'symlink-directory':
                    target_directory = Path(directory) / 'link'
                    target_directory.symlink_to(directory, target_is_directory=True)
                else:
                    prefix.with_suffix('.log').chmod(0o644)
                self.assertNotEqual(self.verify(target_directory, stage).returncode, 0)

    def test_sequence_reuses_existing_owned_gates_without_an_extra_uninstall(self):
        text = HELPER.read_text()
        # Read the actual shell loop and check the lifecycle ordering exactly.
        loop = re.search(r'for phase in (.*?); do', text, re.S).group(1)
        self.assertEqual(loop.replace('\\\n', ' ').split(), list(STAGES))
        self.assertIn('run-durable-gate.sh" "$work/$phase" "${marker}_EXIT"', text)
        self.assertIn('longhorn-live.sh" "$phase"', text)
        self.assertLess(text.index('verify_stage "$work" "$phase"'),
                        text.index('echo LONGHORN_OWNERSHIP_PASS'))
        self.assertIn('LONGHORN_PARENT_LEASE_HELD', text)
        self.assertIn('flock -n "$fd"', text)
        self.assertNotIn('kubectl ', text)
        self.assertNotIn('helm ', text)
        self.assertNotIn('ssh ', text)

    def test_failed_child_stops_the_sequence_before_the_next_stage(self):
        # Exercise strict shell pipeline failure without touching any lab path.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / 'helper.sh'
            text = HELPER.read_text()
            main = text[text.index('  for phase in '):text.index('  echo LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS')]
            script = ('set -Eeuo pipefail\nBASE="$1"; work="$1"\n'
                      'verify_stage() { echo unexpected-verification; }\n' + main)
            runner = root / 'tests/helpers/run-durable-gate.sh'
            runner.parent.mkdir(parents=True)
            runner.write_text('#!/usr/bin/env bash\necho fixture-first-failure\nexit 7\n')
            helper.write_text(script)
            result = subprocess.run(['bash', str(helper), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stdout.count('LONGHORN_OWNERSHIP_STAGE_START'), 1)
            self.assertNotIn('unexpected-verification', result.stdout)


if __name__ == '__main__':
    unittest.main()
