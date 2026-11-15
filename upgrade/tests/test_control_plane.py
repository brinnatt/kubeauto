"""Execute real Ansible refusal paths without a lab or a usable connection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
BOOK = ROOT / 'upgrade/product/control-plane.yml'
ANSIBLE = str(Path(sys.executable).parent / 'ansible-playbook')
BACKENDS = ['https://master-one:6443', 'https://master-two:6443']
FAKE = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path
root = Path(__file__).parents[1]
log = root / 'calls.jsonl'
calls = log.read_text().splitlines() if log.exists() else []
with log.open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')
response = json.loads((root / 'responses.json').read_text())[len(calls)]
print(response.get('stdout', ''))
raise SystemExit(response.get('rc', 0))
'''
NO_CONNECTION = '''from ansible.plugins.connection import ConnectionBase
from ansible.errors import AnsibleConnectionFailure

class Connection(ConnectionBase):
    transport = 'upgrade_test_forbidden'
    def _connect(self):
        raise AnsibleConnectionFailure('TEST_REMOTE_CONNECTION_REFUSED')
    def exec_command(self, *args, **kwargs):
        return self._connect()
    def put_file(self, *args, **kwargs):
        return self._connect()
    def fetch_file(self, *args, **kwargs):
        return self._connect()
    def close(self):
        pass
'''


class ControlPlaneRefusals(unittest.TestCase):
    book = BOOK

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'kube-bin').mkdir()
        (self.root / 'connections').mkdir()
        (self.root / 'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        binary = self.root / 'kube-bin/kubectl'
        binary.write_text(FAKE); binary.chmod(0o700)
        # Even a broken refusal cannot open SSH or mutate the development host.
        # The only real connection is localhost for GET-command fixtures.
        (self.root / 'hosts').write_text(
            '[kube_master]\nmaster-one ansible_connection=upgrade_test_forbidden\n'
            'master-two ansible_connection=upgrade_test_forbidden\n'
            '[all:vars]\nansible_python_interpreter=' + sys.executable + '\n')
        self.variables = {'UPGRADE_SOURCE_VERSION': 'v1.33.6', 'K8S_VER': 'v1.34.13',
            'UPGRADE_PRECONDITIONS_VERIFIED': True, 'UPGRADE_RUN_ID': 'fixture-1',
            'SECURE_PORT': 6443,
            'UPGRADE_API_BACKENDS': BACKENDS, 'base_dir': str(self.root),
            'cluster_dir': str(self.root), 'bin_dir': str(self.root / 'never-written')}

    def run_book(self, responses=(), start=None):
        (self.root / 'vars.json').write_text(json.dumps(self.variables))
        (self.root / 'responses.json').write_text(json.dumps(responses))
        command = [ANSIBLE, '-i', str(self.root / 'hosts'), str(self.book),
                   '-e', '@' + str(self.root / 'vars.json')]
        if start:
            command.extend(['--start-at-task', start])
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
            timeout=30, env={**os.environ, 'ANSIBLE_NOCOLOR': '1',
                'ANSIBLE_LOCAL_TEMP': str(self.root / 'ansible-local'),
                'ANSIBLE_CONNECTION_PLUGINS': str(self.root / 'connections'),
                'ANSIBLE_REMOTE_TEMP': str(self.root / 'ansible-remote')})
        self.assertFalse((self.root / 'never-written').exists())
        self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', result.stdout + result.stderr,
                         'refusal attempted a forbidden remote operation')
        self.assertNotIn('connection plugin', result.stdout + result.stderr,
                         'failure crossed its owning play into a mutation play')
        return result

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_unadmitted_transition_fails_before_any_command_or_remote_connection(self):
        self.variables['UPGRADE_PRECONDITIONS_VERIFIED'] = False
        result = self.run_book()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Minor-upgrade admission is incomplete', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertNotIn('Replace only', result.stdout)

    def test_foreign_version_refuses_before_any_api_replacement(self):
        responses = [{'stdout': json.dumps({'gitVersion': v})}
                     for v in ['v1.33.6', 'v1.35.10']]
        result = self.run_book(responses)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Refuse versions outside this exact transition', result.stdout)
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIn('Replace only', result.stdout)

    def test_same_count_foreign_backend_refuses_before_any_command(self):
        self.variables['UPGRADE_API_BACKENDS'] = [BACKENDS[0], 'https://foreign:6443']
        result = self.run_book()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_unknown_running_controller_refuses_before_first_api_replacement(self):
        self.variables['upgrade_source_controllers'] = {'results': [
            {'item': 'kube-controller-manager', 'stdout': 'Kubernetes v1.35.10'}]}
        result = self.run_book(start='Refuse unknown or prematurely advanced controllers before mutation')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertNotIn('Replace only', result.stdout)

    def test_target_controller_with_remaining_source_api_refuses_before_mutation(self):
        self.variables['upgrade_source_controllers'] = {'results': [
            {'item': 'kube-scheduler', 'stdout': 'Kubernetes v1.34.13'}]}
        self.variables['upgrade_api_versions'] = {'results': [
            {'stdout': json.dumps({'gitVersion': version})}
            for version in ['v1.33.6', 'v1.34.13']]}
        result = self.run_book(start='Refuse unknown or prematurely advanced controllers before mutation')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('evaluated_to', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertNotIn('Replace only', result.stdout)

    def test_all_api_barrier_rejects_a_remaining_source_before_controllers(self):
        responses = [{'stdout': json.dumps({'gitVersion': v})}
                     for v in ['v1.34.13', 'v1.33.6']]
        result = self.run_book(responses,
            start='Verify every reachable backend has the exact target version')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 2)
        self.assertNotIn('Replace only controller', result.stdout)

    def test_barrier_readiness_failure_stops_before_controllers(self):
        responses = [{'stdout': json.dumps({'gitVersion': 'v1.34.13'})}] * 2
        responses += [{'stdout': 'ok'}, {'rc': 1}]
        result = self.run_book(responses,
            start='Verify every reachable backend has the exact target version')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 4)
        self.assertNotIn('Replace only controller', result.stdout)

    def test_mutation_scope_is_serial_binaries_only_and_api_precedes_controllers(self):
        plays = yaml.safe_load(BOOK.read_text())
        mutations = [(i, p) for i, p in enumerate(plays)
                     if any('ansible.builtin.copy' in t for t in p['tasks'])]
        self.assertEqual([i for i, _ in mutations], [2, 4])
        admission = plays[1]
        self.assertEqual(admission['hosts'], 'kube_master')
        self.assertTrue(admission['any_errors_fatal'])
        self.assertTrue(all('ansible.builtin.copy' not in t and
                            'ansible.builtin.systemd_service' not in t
                            for t in admission['tasks']))
        self.assertIn('selectattr', str(admission['tasks'][-1]))
        for _, play in mutations:
            self.assertEqual(play['serial'], 1)
            self.assertTrue(play['any_errors_fatal'])
        body = BOOK.read_text()
        for forbidden in ('include_role:', 'import_role:', 'kubelet', 'kube-proxy',
                          'ignore_errors:', 'failed_when: false', 'exclude=etcd',
                          'insecure-skip-tls-verify=true'):
            self.assertNotIn(forbidden, body)
        self.assertEqual([t['ansible.builtin.copy']['src'] for t in plays[2]['tasks']
                          if 'ansible.builtin.copy' in t],
                         ['{{ base_dir }}/kube-bin/kube-apiserver'])
        copies = [t for t in plays[4]['tasks'] if 'ansible.builtin.copy' in t]
        self.assertEqual(copies[0]['loop'], ['kube-controller-manager', 'kube-scheduler'])
        for _, play in mutations:
            for task in play['tasks']:
                if any(key in task for key in ('ansible.builtin.copy', 'ansible.builtin.systemd_service')):
                    self.assertIn('when', task, 'resume must retain already healthy target processes')


if __name__ == '__main__':
    unittest.main()
