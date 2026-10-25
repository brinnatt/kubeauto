"""Failure module supervision, fail-stop order and exact cleanup owner routing."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class FailureRunnerContracts(unittest.TestCase):
    def test_public_modes_bind_fixed_owner_lease_dispatch_and_contracts(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        allowed = re.search(r'case "\$module" in ([^)]+)\)', runner).group(1).split('|')
        leased = re.search(r'case "\$mode" in ([^)]+)\) ;; \*\) exit 2', runner).group(1).split('|')
        modes = ['v2-failure', 'v2-failure-remaining', 'v2-failure-cleanup'] + ['v2-failure-'+x for x in
            ('replica-im', 'manager', 'attached-spdk', 'remote-spdk', 'replica-network', 'worker-reboot', 'single-disk', 'rebuild-qos', 'all-replicas')]
        for mode in modes:
            self.assertIn(mode, allowed); self.assertIn(mode, leased)
            result = subprocess.run(['bash', '-c', 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode], capture_output=True, text=True, check=True)
            self.assertEqual([mode, 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'], result.stdout.splitlines())
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-failure.sh" "$MODE"', live)
        self.assertIn('tests.unit.test_longhorn_v2_failure_runner', static)
        self.assertIn('tests.unit.test_longhorn_v2_boot_fixture -v', static)

    def run_gate(self, mode, failure=None, journal=None, relay_delay=0):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'tests/helpers').mkdir(parents=True)
            (base/'clusters/longhorn-v2-gate').mkdir(parents=True)
            (base/'.venv/bin').mkdir(parents=True)
            (base/'kube-bin').mkdir()
            trace = base/'trace'
            script = (ROOT/'tests/helpers/longhorn-v2-failure.sh').read_text().replace('/usr/local/kubeauto', str(base))
            script = script.replace('/var/tmp/kubeauto-longhorn-v2-failure.', str(base/'run.'))
            # The fake relay needs a ready barrier: focused/cleanup fixtures can
            # otherwise send TERM before Python installs its test signal handler.
            script = script.replace('relay_pid=$!', '''relay_pid=$!
for fixture_wait in {1..200}; do
  [[ ! -f "$FAILURE_RELAY_READY" ]] || break
  kill -0 "$relay_pid" 2>/dev/null || exit 91
  sleep 0.01
done
[[ -f "$FAILURE_RELAY_READY" ]] || exit 92''')
            runner = base/'runner.sh'
            runner.write_text(script)
            shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh', base/'tests/helpers/run-durable-gate.sh')
            if journal is not None:
                (base/'clusters/longhorn-v2-gate/longhorn-install-business.json').write_text(json.dumps(journal))
            fake = base/'.venv/bin/python'
            fake.write_text('''#!/usr/bin/env bash
set -eu
if [[ "$1" == - ]]; then exec "$FAILURE_REAL_PY" "$@"; fi
helper="${1##*/}"; step="${2:-}"
echo "$helper $step ${*:3}" >> "$FAILURE_TRACE"
if [[ "$helper" == longhorn-v2-install.py && "$step" == relay-maintain ]]; then
  exec "$FAILURE_REAL_PY" -c 'import os,signal,time; from pathlib import Path; time.sleep(float(os.environ["FAILURE_RELAY_DELAY"])); signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["FAILURE_RELAY_READY"]).touch(); time.sleep(120)'
fi
if [[ "$step" == "${FAILURE_INJECT_STEP:-never}" ]]; then exit 17; fi
if [[ "$step" == business ]]; then echo FIXTURE_BUSINESS_PASS; fi
exit 0
''')
            fake.chmod(0o755)
            kubectl = base/'kube-bin/kubectl'
            kubectl.write_text('#!/usr/bin/env bash\nexit 0\n')
            kubectl.chmod(0o755)
            environment = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAILURE_TRACE=str(trace),
                               FAILURE_REAL_PY=sys.executable, FAILURE_INJECT_STEP=failure or 'never',
                               FAILURE_RELAY_READY=str(base/'relay-ready'), FAILURE_RELAY_DELAY=str(relay_delay))
            output = subprocess.run(['bash', str(runner), mode], env=environment, capture_output=True, text=True, timeout=15)
            lines = trace.read_text().splitlines() if trace.exists() else []
            states = {p.name: p.read_text() for p in base.glob('run.*/*.exit')}
            return output, lines, states

    def test_all_nine_cases_have_durable_business_exit_verifier_and_baseline_before_next_case(self):
        output, trace, states = self.run_gate('v2-failure')
        self.assertEqual(0, output.returncode, output.stdout+output.stderr)
        self.assertEqual(9, output.stdout.count('LONGHORN_V2_FAILURE_STAGE_PASS case='))
        self.assertIn('LONGHORN_V2_FAILURE_SCOPED_CLEAN_PASS', output.stdout)
        self.assertIn('LONGHORN_V2_FAILURE_PASS', output.stdout)
        self.assertEqual(9, len(states))
        self.assertTrue(all(value == '0\n' for value in states.values()))
        business = [line for line in trace if ' business ' in line]
        self.assertEqual(9, len(business))
        self.assertIn('longhorn-v2-control-faults.py', business[0])
        self.assertIn('longhorn-v2-manager-loss.py', business[1])
        self.assertTrue(business[-1].startswith('longhorn-v2-all-replicas.py'))
        for index, line in enumerate(trace):
            if ' business ' not in line: continue
            self.assertIn(' verify ', trace[index+1])
            self.assertTrue(trace[index+2].startswith('longhorn-v2-install.py volume-baseline'))

    def test_focused_qos_does_not_repeat_accepted_runtime_or_backup_cases(self):
        output, trace, states = self.run_gate('v2-failure-rebuild-qos')
        self.assertEqual(0, output.returncode, output.stdout+output.stderr)
        business = [line for line in trace if ' business ' in line]
        self.assertEqual(1, len(business))
        self.assertIn('longhorn-v2-disk-faults.py business', business[0])
        self.assertIn('--case rebuild-qos', business[0])
        self.assertEqual({'rebuild-qos.exit': '0\n'}, states)

    def test_remaining_batch_proves_repaired_remote_first_without_repeating_accepted_scopes(self):
        for failure in (None, 'business'):
            with self.subTest(failure=failure):
                output, trace, states = self.run_gate('v2-failure-remaining', failure=failure)
                self.assertEqual(output.returncode, 17 if failure else 0, output.stdout+output.stderr)
                expected = ['remote-spdk'] if failure else ['remote-spdk', 'replica-network', 'worker-reboot',
                    'single-disk', 'rebuild-qos', 'all-replicas']
                self.assertEqual(set(states), {case+'.exit' for case in expected})
                self.assertEqual(re.findall(r'^LONGHORN_V2_FAILURE_STAGE_START case=(.*)$', output.stdout, re.M), expected)
                business = [line for line in trace if ' business ' in line]
                self.assertIn('--case remote-spdk', business[0])
                self.assertFalse(any('longhorn-v2-manager-loss.py business' in line or
                                     'longhorn-v2-control-faults.py business' in line or
                                     '--case attached-spdk' in line for line in business))
                self.assertEqual('LONGHORN_V2_FAILURE_REMAINING_PASS' in output.stdout, failure is None)

    def test_delayed_fixture_relay_is_ready_before_focused_and_cleanup_shutdown(self):
        for mode in ('v2-failure-rebuild-qos', 'v2-failure-cleanup'):
            with self.subTest(mode=mode):
                output, _, _ = self.run_gate(mode, relay_delay=0.1)
                self.assertEqual(0, output.returncode, output.stdout+output.stderr)
                self.assertIn('SCOPED_CLEAN_PASS', output.stdout)
                self.assertNotIn('LONGHORN_FIRST_FAILURE', output.stderr)

    def test_failed_business_stops_dependents_and_retains_real_failure_before_cleanup(self):
        output, trace, states = self.run_gate('v2-failure', failure='business')
        self.assertEqual(17, output.returncode, output.stdout+output.stderr)
        self.assertEqual({'replica-im.exit': '17\n'}, states)
        self.assertEqual(1, len([s for s in trace if ' business ' in s]))
        self.assertTrue(any(s.startswith('longhorn-v2-control-faults.py cleanup') for s in trace))
        self.assertNotIn('LONGHORN_V2_FAILURE_PASS', output.stdout)
        self.assertIn('LONGHORN_FIRST_FAILURE phase=replica-im rc=17', output.stderr)

    def test_failed_verifier_is_not_mistaken_for_durable_business_success(self):
        output, trace, states = self.run_gate('v2-failure-single-disk', failure='verify')
        self.assertEqual(17, output.returncode, output.stdout+output.stderr)
        self.assertEqual({'single-disk.exit': '0\n'}, states)
        self.assertNotIn('LONGHORN_V2_FAILURE_SINGLE_DISK_PASS', output.stdout)
        self.assertTrue(any(s.startswith('longhorn-v2-disk-faults.py cleanup') for s in trace))

    def test_cleanup_routes_only_to_actual_journal_owner_before_any_business_deletion(self):
        owners = {'v2_all_replicas_fault': 'longhorn-v2-all-replicas.py', 'v2_disk_fault': 'longhorn-v2-disk-faults.py',
            'v2_runtime_fault': 'longhorn-v2-runtime-faults.py', 'v2_manager_loss': 'longhorn-v2-manager-loss.py',
            'v2_replica_im_loss': 'longhorn-v2-control-faults.py'}
        for key, helper in owners.items():
            with self.subTest(key=key):
                output, trace, _ = self.run_gate('v2-failure-cleanup', journal={key: {}})
                self.assertEqual(0, output.returncode, output.stdout+output.stderr)
                cleanup = [line for line in trace if ' cleanup ' in line]
                self.assertEqual(1, len(cleanup))
                self.assertTrue(cleanup[0].startswith(helper+' cleanup'))

    def test_ambiguous_or_unknown_cleanup_owner_preserves_journal_and_cannot_fall_through(self):
        for journal in ({'unrecognized_fault': {}}, {'v2_disk_fault': {}, 'v2_runtime_fault': {}}):
            with self.subTest(journal=journal):
                output, trace, _ = self.run_gate('v2-failure-cleanup', journal=journal)
                self.assertNotEqual(0, output.returncode)
                self.assertFalse(any(' cleanup ' in s or ' business-clean ' in s for s in trace))
                self.assertNotIn('LONGHORN_V2_FAILURE_CLEANUP_PASS', output.stdout)


if __name__ == '__main__':
    unittest.main()
