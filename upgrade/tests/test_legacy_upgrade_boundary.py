"""Execute the real legacy first play; all host observations are offline adapters."""
from copy import deepcopy
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
ADAPTER = '''from ansible.plugins.action import ActionBase
import json, os
from pathlib import Path
class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        root=Path(os.environ['UPGRADE_LEGACY_FIXTURE'])
        state=json.loads((root/'state.json').read_text())
        host=task_vars['inventory_hostname']
        command=self._task.args.get('command', self._task.args.get('_raw_params'))
        if command == '/fixture/bin/kube-apiserver --version':
            assert self._task.delegate_to == 'localhost'
            value=state['target']
        else:
            assert 'systemctl is-active --quiet kube-apiserver' in command
            assert '"/proc/$upgrade_pid/exe" --version' in command
            assert self._task.delegate_to is None
            value=state['source'][host]
        with (root/'trace.jsonl').open('a') as out:out.write(json.dumps({'host':host,'command':command})+'\\n')
        return {'changed':False,'rc':0,'stdout':value,'stderr':''}
'''


class LegacyUpgradeBoundaryContracts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root/'actions').mkdir(); (self.root/'connections').mkdir()
        (self.root/'actions/legacy_read.py').write_text(ADAPTER)
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        (self.root/'hosts').write_text('[kube_master]\nmaster-one\nmaster-two\n[all:vars]\n'
            'ansible_connection=upgrade_test_forbidden\nansible_python_interpreter='+sys.executable+'\n')
        self.plays = yaml.safe_load((ROOT/'playbooks/93.upgrade.yml').read_text())
        first = deepcopy(self.plays[0])
        self.assertFalse(first['gather_facts'])
        self.assertTrue(first['any_errors_fatal'])
        for task in first['tasks']:
            if 'shell' in task:
                task['legacy_read'] = {'command': task.pop('shell')}
                task.pop('args', None)
        (self.root/'book.yml').write_text(yaml.safe_dump([first]))
        self.state = {'source': {'master-one': 'Kubernetes v1.33.5', 'master-two': 'Kubernetes v1.33.5'},
                      'target': 'Kubernetes v1.33.6'}

    def execute(self, config='1.33.6', *, full=False):
        (self.root/'state.json').write_text(json.dumps(self.state))
        (self.root/'trace.jsonl').unlink(missing_ok=True)
        if full:
            # Override only shell observations; use the actual complete product
            # playbook and real roles. Any attempted remote mutation must fail.
            (self.root/'actions/shell.py').write_text(ADAPTER)
        result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'), '-i',
            str(self.root/'hosts'), str(ROOT/'playbooks/93.upgrade.yml' if full else self.root/'book.yml'), '-e',
            json.dumps({'bin_dir': '/fixture/bin', 'K8S_VER': config})],
            capture_output=True, text=True, timeout=20, cwd=ROOT, env={**os.environ,
                'UPGRADE_LEGACY_FIXTURE': str(self.root), 'ANSIBLE_LOCAL_TEMP': str(self.root/'local'),
                'ANSIBLE_ROLES_PATH': str(ROOT/'roles'),
                'ANSIBLE_ACTION_PLUGINS': str(self.root/'actions'),
                'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections')})
        self.output = result.stdout+result.stderr
        for forbidden in ['TEST_REMOTE_CONNECTION_REFUSED', 'Unexpected templating', 'unmodelled']:
            self.assertNotIn(forbidden, self.output)
        return result

    def test_original_patch_fixture_and_v_prefixed_config_remain_admitted_before_unchanged_roles(self):
        for config in ['1.33.6', 'v1.33.6']:
            result = self.execute(config)
            self.assertEqual(result.returncode, 0, self.output)
            trace = [json.loads(s) for s in (self.root/'trace.jsonl').read_text().splitlines()]
            self.assertEqual(len(trace), 3)  # Actual process on each master; target once locally.
        self.assertEqual(self.plays[1]['roles'], ['kube-master', 'kube-node'])
        self.assertEqual(self.plays[1]['serial'], 1)

    def test_first_minor_and_skipped_minor_refuse_before_any_install_role(self):
        for target in ['1.34.13', '1.35.10']:
            self.state['target'] = 'Kubernetes v'+target
            self.assertNotEqual(self.execute(target).returncode, 0, self.output)
            self.assertIn('Legacy patch upgrade refuses minor changes and downgrades', self.output)

    def test_other_master_cannot_hide_a_minor_change_or_patch_downgrade(self):
        for version in ['Kubernetes v1.32.13', 'Kubernetes v1.34.1', 'Kubernetes v1.33.7']:
            self.state['source']['master-two'] = version
            self.assertNotEqual(self.execute().returncode, 0, self.output)
            self.assertIn('Legacy patch upgrade refuses minor changes and downgrades', self.output)

    def test_malformed_prerelease_or_config_mismatch_refuses(self):
        for value in ['Kubernetes v1.33.6-rc.1', 'Kubernetes v1.33.6+vendor', 'Kubernetes v1.033.6',
                      'Kubernetes v1.33.6\nextra']:
            self.state['target'] = value
            self.assertNotEqual(self.execute().returncode, 0, self.output)
            self.assertIn('Legacy patch upgrade requires exact running and configured binary identities', self.output)
        self.state['target'] = 'Kubernetes v1.33.6'
        self.assertNotEqual(self.execute('1.33.13').returncode, 0, self.output)

    def test_same_version_still_refuses_without_entering_upgrade_roles(self):
        self.state['source'] = {name: 'Kubernetes v1.33.6' for name in self.state['source']}
        self.assertNotEqual(self.execute().returncode, 0, self.output)
        self.assertIn('running version is the same as the update version', self.output)

    def test_actual_complete_product_playbook_stops_before_roles_on_minor_or_hidden_master_downgrade(self):
        self.state['target'] = 'Kubernetes v1.34.13'
        result = self.execute('1.34.13', full=True)
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn('Legacy patch upgrade refuses minor changes and downgrades', self.output)
        self.assertNotIn('kube-master :', self.output)
        self.assertNotIn('kube-node :', self.output)
        self.state['target'] = 'Kubernetes v1.33.6'
        self.state['source']['master-two'] = 'Kubernetes v1.33.7'
        self.assertNotEqual(self.execute(full=True).returncode, 0, self.output)
        self.assertIn('Legacy patch upgrade refuses minor changes and downgrades', self.output)
        self.assertNotIn('kube-master :', self.output)


if __name__ == '__main__':
    unittest.main()
