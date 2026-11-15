"""Run actual etcd playbook assertions and serial handoffs, offline only."""
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
HOSTS = ['member-one', 'member-two', 'member-three']


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def record(tasks):
    tasks = copy.deepcopy(tasks)
    for task in tasks:
        for key in list(task):
            if key in ['ansible.builtin.'+m for m in ['command', 'shell', 'stat', 'copy', 'systemd_service']]:
                values = task.pop(key)
                if isinstance(values, str):
                    values = {'_raw_params': values}
                task['etcd_record'] = {'module': key.rsplit('.', 1)[1], 'values': values}
    return tasks


class EtcdStageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'actions').mkdir(); (self.root/'connections').mkdir()
        (self.root/'actions/etcd_record.py').write_bytes((ROOT/'upgrade/tests/fixtures/etcd_action.py').read_bytes())
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        plays = yaml.safe_load((ROOT/'upgrade/product/etcd.yml').read_text())
        for play in plays:
            play['tasks'] = record(play['tasks'])
        (self.root/'book.yml').write_text(yaml.safe_dump(plays))
        (self.root/'etcd-observe.yml').write_text(yaml.safe_dump(record(
            yaml.safe_load((ROOT/'upgrade/product/etcd-observe.yml').read_text()))))
        (self.root/'hosts').write_text('[etcd]\n'+'\n'.join(HOSTS)+'\n[all:vars]\n'
            'ansible_connection=upgrade_test_forbidden\nansible_python_interpreter='+sys.executable+'\n')
        members = [{'ID': i+1, 'name': 'etcd-'+host, 'peerURLs': ['https://'+host+':2380'],
                    'clientURLs': ['https://'+host+':2379'], 'isLearner': False} for i, host in enumerate(HOSTS)]
        self.variables = dict(UPGRADE_PRECONDITIONS_VERIFIED=True, UPGRADE_ETCD_RECOVERY_VERIFIED=True,
            UPGRADE_RUN_ID='contract-1', UPGRADE_ETCD_CLUSTER_ID=101, UPGRADE_ETCD_SOURCE_REVISION=100,
            UPGRADE_ETCD_MEMBERS=copy.deepcopy(members),
            UPGRADE_ETCD_ENDPOINTS=['https://'+h+':2379' for h in HOSTS],
            UPGRADE_ETCDCTL='/fixture/etcdctl', UPGRADE_ETCDCTL_SHA256=digest('etcdctl'),
            UPGRADE_ETCD_TARGETS={h: {'path': '/fixture/'+h+'/etcd', 'sha256': digest(h+'target')} for h in HOSTS},
            UPGRADE_ETCD_UNIT_SHA256={h: digest(h+'unit') for h in HOSTS},
            cluster_dir='/fixture', bin_dir='/fixture/bin')
        self.state = {'members': members, 'version': {h: '3.6.4' for h in HOSTS},
                      'disk_hash': {h: digest(h+'source') for h in HOSTS},
                      'unit_hash': copy.deepcopy(self.variables['UPGRADE_ETCD_UNIT_SHA256']), 'restarted': []}

    def run_stage(self):
        (self.root/'trace.jsonl').unlink(missing_ok=True)
        (self.root/'vars.json').write_text(json.dumps(self.variables))
        (self.root/'state.json').write_text(json.dumps(self.state))
        result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'), '-i',
            str(self.root/'hosts'), str(self.root/'book.yml'), '-e', '@'+str(self.root/'vars.json')],
            cwd=ROOT, capture_output=True, text=True, timeout=40, env={**os.environ,
                'UPGRADE_ETCD_TEST_ROOT': str(self.root), 'ANSIBLE_LOCAL_TEMP': str(self.root/'local'),
                'ANSIBLE_ACTION_PLUGINS': str(self.root/'actions'),
                'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections')})
        output = result.stdout+result.stderr
        for forbidden in ['TEST_REMOTE_CONNECTION_REFUSED', 'unmodelled', 'Unexpected templating',
                          'Unexpected failure during module execution']:
            self.assertNotIn(forbidden, output)
        self.trace = [json.loads(line) for line in (self.root/'trace.jsonl').read_text().splitlines()] if (self.root/'trace.jsonl').exists() else []
        self.final = json.loads((self.root/'state.json').read_text())
        return result

    def refuse(self, marker, *, after_restart=False):
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn(marker, result.stdout+result.stderr)
        if not after_restart:
            self.assertFalse(any(t['module'] in ['copy', 'systemd_service'] for t in self.trace))

    def test_three_members_are_replaced_serially_with_complete_cluster_checks_between(self):
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('UPGRADE_ETCD_STAGE_PASS', result.stdout)
        self.assertEqual(self.final['restarted'], HOSTS)
        self.assertEqual(set(self.final['version'].values()), {'3.6.16'})
        mutations = [t for t in self.trace if t['module'] in ['copy', 'systemd_service']]
        self.assertEqual([(t['host'], t['module']) for t in mutations],
                         [(h, m) for h in HOSTS for m in ['copy', 'systemd_service']])
        for i, host in enumerate(HOSTS):
            start = next(n for n, t in enumerate(self.trace) if t['host'] == host and t['module'] == 'systemd_service')
            end = next((n for n in range(start+1, len(self.trace)) if self.trace[n]['module'] == 'copy'), len(self.trace))
            commands = [t['args'].get('argv', [])[-3:] for t in self.trace[start+1:end] if t['module'] == 'command']
            self.assertIn(['endpoint', 'health', '--write-out=json'], commands)
            self.assertIn(['endpoint', 'status', '--write-out=json'], commands)
        self.assertEqual(self.final['members'], self.state['members'])
        self.assertEqual(self.final['unit_hash'], self.state['unit_hash'])

    def test_unverified_recovery_refuses_before_even_an_observation(self):
        self.variables['UPGRADE_ETCD_RECOVERY_VERIFIED'] = False
        self.refuse('Etcd admission is incomplete')
        self.assertEqual(self.trace, [])

    def test_wrong_artifact_refuses_before_endpoint_or_remote_operations(self):
        self.state['artifact_hash'] = 'f'*64
        self.refuse('Refuse stale or missing per-host etcd artifacts')
        self.assertTrue(all(t['module'] == 'stat' for t in self.trace))

    def test_equal_count_foreign_endpoint_refuses_before_observation(self):
        self.variables['UPGRADE_ETCD_ENDPOINTS'][-1] = 'https://foreign:2379'
        self.refuse('Etcd admission is incomplete')
        self.assertEqual(self.trace, [])

    def test_unreviewed_service_dropin_is_not_hidden_by_base_unit_digest(self):
        self.state['dropins'] = '/etc/systemd/system/etcd.service.d/foreign.conf'
        self.refuse('Refuse unreviewed etcd service drop-ins before any member replacement')

    def test_foreign_cluster_revision_regression_and_alarm_refuse_mutation(self):
        for key, value in [('cluster_id', 102), ('revision', 99), ('errors', ['NOSPACE']), ('leader', 99)]:
            with self.subTest(key=key):
                self.state[key] = value
                self.refuse('Refuse foreign cluster identities revisions versions or alarms')
                self.state.pop(key)

    def test_renamed_member_and_learner_are_rejected(self):
        for field, value in [('name', 'foreign'), ('isLearner', True)]:
            with self.subTest(field=field):
                original = self.state['members'][0][field]
                self.state['members'][0][field] = value
                self.refuse('Match every member to its reviewed persistent identity')
                self.state['members'][0][field] = original

    def test_all_unit_and_running_process_identities_are_checked_before_first_copy(self):
        self.state['unit_hash'][HOSTS[-1]] = 'f'*64
        self.refuse('Refuse an altered etcd service definition')
        self.state['unit_hash'] = copy.deepcopy(self.variables['UPGRADE_ETCD_UNIT_SHA256'])
        self.state['process_version'] = '3.7.3'
        self.refuse('Match each process to the admitted live server version')

    def test_failed_first_member_blocks_every_following_member_without_rollback(self):
        self.state['fail_after_restart'] = True
        self.refuse('fixture first endpoint failure', after_restart=True)
        self.assertEqual(self.final['restarted'], [HOSTS[0]])
        self.assertFalse(any(t['module'] == 'copy' and t['host'] != HOSTS[0] for t in self.trace))
        self.assertEqual(self.final['version'][HOSTS[0]], '3.6.16')

    def test_target_version_and_unit_drift_after_restart_block_the_next_member(self):
        for field, value, marker in [
                ('restart_version', '3.6.4', 'Require this restarted member to serve the exact target'),
                ('unit_drift', True, 'Reject service configuration drift during this member transition')]:
            with self.subTest(field=field):
                self.state[field] = value
                self.refuse(marker, after_restart=True)
                self.assertEqual(self.final['restarted'], [HOSTS[0]])
                self.state.pop(field)

    def test_final_stage_reobserves_every_completed_member_before_success(self):
        self.state['regress_completed'] = True
        self.refuse('Reject a previously completed member that regressed during the serial stage', after_restart=True)
        self.assertEqual(self.final['restarted'], HOSTS)

    def test_owned_target_resume_rechecks_all_identities_without_restarting(self):
        self.variables['UPGRADE_ETCD_RESUME'] = True
        self.state['version'] = {h: '3.6.16' for h in HOSTS}
        self.state['disk_hash'] = {h: self.variables['UPGRADE_ETCD_TARGETS'][h]['sha256'] for h in HOSTS}
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(self.final['restarted'], [])
        self.assertTrue(any(t['module'] == 'command' for t in self.trace))
        self.variables.pop('UPGRADE_ETCD_RESUME')
        self.refuse('Refuse foreign cluster identities revisions versions or alarms')


if __name__ == '__main__':
    unittest.main()
