"""Exercise actual runtime-stage conditions through recording action adapters."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml
from upgrade.tests.test_control_plane import NO_CONNECTION

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ROOT/'upgrade/product'
BINARIES = ['containerd', 'containerd-shim-runc-v2', 'ctr', 'runc']


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def recording_tasks(tasks):
    """Replace only external operations; preserve real expressions and includes."""
    tasks = copy.deepcopy(tasks)
    for task in tasks:
        for key in list(task):
            if key.startswith('ansible.builtin.') and key.rsplit('.', 1)[1] in {
                    'command', 'shell', 'stat', 'copy', 'systemd_service', 'file'}:
                values = task.pop(key)
                if isinstance(values, str):
                    values = {'_raw_params': values}
                task['runtime_record'] = {'module': key.rsplit('.', 1)[1], 'values': values}
        for nested in ['block', 'rescue', 'always']:
            if nested in task:
                task[nested] = recording_tasks(task[nested])
    return tasks


class ContainerdStageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'actions').mkdir(); (self.root/'connections').mkdir()
        (self.root/'actions/runtime_record.py').write_bytes((ROOT/'upgrade/tests/fixtures/runtime_action.py').read_bytes())
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        for name in ['containerd.yml', 'containerd-edge.yml', 'containerd-admission.yml',
                     'docker.yml', 'docker-admission.yml']:
            tasks = yaml.safe_load((PRODUCT/name).read_text())
            (self.root/name).write_text(yaml.safe_dump(recording_tasks(tasks), sort_keys=False))
        self.variables = dict(UPGRADE_PRECONDITIONS_VERIFIED=True, CONTAINER_RUNTIME='containerd',
            UPGRADE_NODE_HOST='localhost', UPGRADE_NODE_NAME='node-one', UPGRADE_NODE_UID='uid-one',
            UPGRADE_RUN_ID='contract-1', UPGRADE_API_BACKENDS=['https://master-one:6443'],
            base_dir='/fixture', cluster_dir='/fixture', bin_dir='/fixture/bin',
            UPGRADE_CONTAINERD_CONFIG={'path': '/fixture/config', 'sha256': digest('target-config')},
            UPGRADE_CONTAINERD_SOURCE_CONFIG_SHA256=digest('source-config'),
            UPGRADE_CONTAINERD_UNIT_SHA256=digest('source-unit'),
            UPGRADE_CONTAINERD_BUNDLES={v: {'directory': '/fixture/'+v,
                'sha256': {b: digest('runc1.5.2' if b == 'runc' else v+b) for b in BINARIES}}
                for v in ['2.2.10', '2.3.7']})
        self.state = dict(version='2.1.4', config_hash=digest('source-config'),
                          unit_hash=digest('source-unit'), disk_hashes={b: digest('source'+b) for b in BINARIES})

    def run_stage(self, name='containerd.yml', start=None, node=False):
        (self.root/'trace.jsonl').unlink(missing_ok=True)
        self.variables['ansible_connection'] = 'upgrade_test_forbidden'
        (self.root/'variables.json').write_text(json.dumps(self.variables))
        (self.root/'state.json').write_text(json.dumps(self.state))
        tasks = [{'ansible.builtin.import_tasks': name}]
        if node:
            plays = yaml.safe_load((PRODUCT/'node.yml').read_text())
            for play in plays:
                play['tasks'] = recording_tasks(play['tasks'])
        else:
            plays = [{'hosts': 'localhost', 'gather_facts': False,
                      'any_errors_fatal': True, 'tasks': tasks}]
        (self.root/'book.yml').write_text(yaml.safe_dump(plays))
        cmd = [str(Path(sys.executable).parent/'ansible-playbook'), '-i', 'localhost,',
               str(self.root/'book.yml'), '-e', '@'+str(self.root/'variables.json')]
        if start:
            cmd.extend(['--start-at-task', start])
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30,
            env={**os.environ, 'UPGRADE_RUNTIME_TEST_ROOT': str(self.root),
                 'ANSIBLE_ACTION_PLUGINS': str(self.root/'actions'),
                 'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections'),
                 'ANSIBLE_LOCAL_TEMP': str(self.root/'local')})
        self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', result.stdout+result.stderr)
        self.assertNotIn('unmodelled', result.stdout+result.stderr)
        self.assertNotIn('Unexpected templating', result.stdout+result.stderr)
        self.assertNotIn('Unexpected failure during module execution', result.stdout+result.stderr)
        self.trace = [json.loads(line) for line in (self.root/'trace.jsonl').read_text().splitlines()] if (self.root/'trace.jsonl').exists() else []
        self.final = json.loads((self.root/'state.json').read_text())
        return result

    def test_two_real_adjacent_edges_complete_in_order_with_own_cri_and_sandbox_checks(self):
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('UPGRADE_CONTAINERD_STAGE_PASS', result.stdout)
        restarts = [c for c in self.trace if c['module'] == 'systemd_service']
        self.assertEqual([c['target'] for c in restarts], ['2.2.10', '2.3.7'])
        for target in ['2.2.10', '2.3.7']:
            calls = [c for c in self.trace if c['target'] == target]
            names = [c['name'] for c in calls]
            self.assertLess(names.index('Decode the staged configuration with the actual target binary before activation'),
                            names.index('Restart only the containerd daemon preserving existing shims'))
            self.assertLess(names.index('Wait for both CRI health conditions after this edge'),
                            names.index('Read back ready sandboxes retained by this edge'))
        self.assertEqual(self.final['version'], '2.3.7')
        self.assertEqual(self.final['disk_hashes'], self.variables['UPGRADE_CONTAINERD_BUNDLES']['2.3.7']['sha256'])

    def test_owned_bridge_resume_skips_bridge_and_completes_target(self):
        self.state['version'] = '2.2.10'; self.variables['UPGRADE_NODE_RESUME'] = True
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual([c['target'] for c in self.trace if c['module'] == 'systemd_service'], ['2.3.7'])

    def test_completed_target_resume_rechecks_final_files_without_downgrading(self):
        self.state.update(version='2.3.7', config_hash=digest('target-config'),
                          disk_hashes=self.variables['UPGRADE_CONTAINERD_BUNDLES']['2.3.7']['sha256'])
        self.variables['UPGRADE_NODE_RESUME'] = True
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse([c for c in self.trace if c['module'] in ['copy', 'systemd_service']])
        self.assertEqual(len([c for c in self.trace if c['name'].startswith('Reobserve final on-disk')]), 4)

    def test_target_resume_with_disk_drift_is_not_accepted(self):
        self.state.update(version='2.3.7', config_hash=digest('target-config'))
        self.variables['UPGRADE_NODE_RESUME'] = True
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse final on-disk drift before kubelet replacement]', result.stdout)
        self.assertNotIn('UPGRADE_CONTAINERD_STAGE_PASS', result.stdout)
        self.assertFalse([c for c in self.trace if c['module'] in ['copy', 'systemd_service']])

    def test_first_edge_failure_never_launches_second_or_cleans_failure_evidence(self):
        self.state['fail'] = 'Restart only the containerd daemon preserving existing shims:2.2.10'
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('fixture first failure', result.stdout)
        self.assertFalse([c for c in self.trace if c['target'] == '2.3.7'])
        self.assertFalse([c for c in self.trace if c['module'] == 'file'])
        self.assertNotIn('UPGRADE_CONTAINERD_STAGE_PASS', result.stdout)

    def test_lost_retained_sandbox_stops_at_the_first_edge(self):
        self.state['lose_sandbox'] = True
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse lost ready sandboxes before advancing the runtime boundary]', result.stdout)
        self.assertFalse([c for c in self.trace if c['target'] == '2.3.7'])
        self.assertNotIn('UPGRADE_CONTAINERD_STAGE_PASS', result.stdout)

    def test_changed_node_or_foreign_cordon_boundary_refuses_before_remote_operation(self):
        self.state['uid'] = 'replacement'
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse runtime changes without the same cordoned node and owner]', result.stdout)
        self.assertEqual(len(self.trace), 1)
        self.assertEqual(self.trace[0]['module'], 'command')

    def test_invalid_bundle_mapping_refuses_before_commands(self):
        self.variables['UPGRADE_CONTAINERD_BUNDLES'].pop('2.2.10')
        result = self.run_stage('containerd-admission.yml')
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertEqual(self.trace, [])

    def test_unreviewed_source_config_refuses_before_runtime_identity_and_mutation(self):
        self.state['config_hash'] = digest('drift')
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse unreviewed configuration drift before changing the runtime]', result.stdout)
        self.assertFalse([c for c in self.trace if c['module'] in ['copy', 'systemd_service']])
        self.assertFalse([c for c in self.trace if 'crictl' in c['args'].get('argv', [''])[0]])

    def test_actual_node_handoff_stops_after_runtime_failure_without_kubelet_copy_or_uncordon(self):
        self.variables['UPGRADE_DRAIN_TIMEOUT_SECONDS'] = 300
        self.state['fail'] = 'Restart only the containerd daemon preserving existing shims:2.2.10'
        result = self.run_stage(start='Drain with eviction PDB and local-data protection', node=True)
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('fixture first failure', result.stdout)
        self.assertTrue(self.final['cordoned'])
        self.assertIn('drain', self.trace[0]['args']['argv'])
        self.assertFalse([c for c in self.trace if 'uncordon' in c['args'].get('argv', [])])
        self.assertNotIn('TASK [Replace only the drained node executables]', result.stdout)
        self.assertNotIn('UPGRADE_NODE_STAGE_PASS', result.stdout)

    def test_unknown_or_unowned_partial_runtime_refuses_before_any_copy(self):
        for version in ['2.0.0', '2.2.10', '2.3.7', '2.4.0']:
            with self.subTest(version=version):
                self.state['version'] = version
                result = self.run_stage()
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('TASK [Refuse unknown runtime sources or an unowned partial migration]', result.stdout)
                self.assertFalse([c for c in self.trace if c['module'] in ['copy', 'systemd_service']])

    def test_noncordoned_node_or_unsafe_unit_refuses_before_runtime_mutation(self):
        for key, value, refusal in [('cordoned', False, 'Refuse runtime changes'),
                                     ('kill_mode', 'control-group', 'Refuse a unit'),
                                     ('unit_hash', digest('foreign-unit'), 'Refuse a unit')]:
            with self.subTest(key=key):
                old = self.state.copy(); self.state[key] = value
                result = self.run_stage()
                self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                self.assertIn('TASK ['+refusal, result.stdout)
                self.assertFalse([c for c in self.trace if c['module'] in ['copy', 'systemd_service']])
                self.state = old

    def test_staged_config_drift_never_activates_or_restarts(self):
        self.state['staged_config_hash'] = digest('changed-after-controller-admission')
        result = self.run_stage()
        self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
        self.assertIn('TASK [Refuse staged configuration drift before activation]', result.stdout)
        self.assertFalse([c for c in self.trace if c['module'] == 'systemd_service'])
        self.assertEqual(self.final['config_hash'], digest('source-config'))

    def test_real_node_call_order_admits_then_drains_then_advances_runtime_before_kubelet(self):
        plays = yaml.safe_load((PRODUCT/'node.yml').read_text())
        admission = [t for t in plays[0]['tasks'] if t.get('ansible.builtin.import_tasks') == 'containerd-admission.yml']
        self.assertEqual(len(admission), 1)
        names = [t['name'] for t in plays[1]['tasks']]
        self.assertLess(names.index('Drain with eviction PDB and local-data protection'),
                        names.index('Advance the drained node runtime before replacing kubelet'))
        self.assertLess(names.index('Advance the drained node runtime before replacing kubelet'),
                        names.index('Replace only the drained node executables'))
        for filename in ['containerd.yml', 'containerd-edge.yml']:
            body = (PRODUCT/filename).read_text()
            for forbidden in ['ignore_errors', 'rescue:', 'always:', 'state: stopped', 'rm -rf', 'uncordon']:
                self.assertNotIn(forbidden, body)

    def test_runtime_dynamic_includes_parse_with_the_ansible_loader(self):
        """Dynamic includes must pass Ansible's YAML parser before a live run."""
        playbook = self.root/'runtime-parse.yml'
        playbook.write_text(yaml.safe_dump([{
            'hosts': 'localhost', 'gather_facts': False,
            'tasks': [
                {'ansible.builtin.import_tasks': str(PRODUCT/'containerd.yml')},
                {'ansible.builtin.import_tasks': str(PRODUCT/'containerd-edge.yml')},
            ],
        }], sort_keys=False))
        result = subprocess.run([
            str(Path(sys.executable).parent/'ansible-playbook'), '--syntax-check',
            '-i', 'localhost,', str(playbook),
        ], cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
