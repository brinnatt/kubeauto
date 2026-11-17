"""Run real composed playbooks with offline process/API/CRI operation adapters."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

import yaml

from upgrade.product.journal import Journal
from upgrade.product.kubernetes_preflight import Refused, observe
from upgrade.product.kubernetes_stage import CONTROL, KubernetesStage, StageFailed
from upgrade.tests.test_containerd_stage import recording_tasks
from upgrade.tests.test_control_plane import NO_CONNECTION


ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ROOT/'upgrade/product'
FAKE_KUBECTL = '''#!/usr/bin/env python3
import json, sys
from pathlib import Path
root = Path(__file__).parent
state = json.loads((root/'state.json').read_text())
args = sys.argv[1:]
with (root/'queries.jsonl').open('a') as out: out.write(json.dumps(args)+'\\n')
server = args[args.index('--server')+1]
host = server.removeprefix('https://').split(':')[0]
if '--client' in args:
    value = {'clientVersion': {'gitVersion': 'v1.34.13'}}
elif '--raw=/version' in args:
    value = {'gitVersion': state['apis'][host]}
elif '--raw=/readyz' in args:
    print('ok'); raise SystemExit(0)
elif 'namespace' in args:
    value = {'metadata': {'name': 'kube-system', 'uid': state.get('cluster_uid', 'cluster-uid')},
             'status': {'phase': 'Active'}}
elif 'nodes' in args:
    value = {'items': [{'metadata': {'name': n['name'], 'uid': n['uid']},
        'spec': {'unschedulable': n['cordoned']},
        'status': {'nodeInfo': {'kubeletVersion': n['kubelet']},
                   'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        for n in state['nodes'].values()]}
else:
    raise SystemExit(99)
print(json.dumps(value))
'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class KubernetesHandoffContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'actions').mkdir(); (self.root/'connections').mkdir()
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        (self.root/'actions/runtime_record.py').write_bytes(
            (ROOT/'upgrade/tests/fixtures/kubernetes_action.py').read_bytes())
        for path in PRODUCT.glob('*.yml'):
            data = yaml.safe_load(path.read_text())
            if data and 'hosts' in data[0]:
                for play in data:
                    play['tasks'] = recording_tasks(play['tasks'])
            else:
                data = recording_tasks(data)
            (self.root/path.name).write_text(yaml.safe_dump(data, sort_keys=False))
        self.binary = self.root/'kubectl'
        self.binary.write_text(FAKE_KUBECTL); self.binary.chmod(0o700)
        self.config = self.root/'kubeconfig'; self.config.write_text('offline TLS fixture\n')
        self.hosts = {'master-one': 'master-1', 'worker-one': 'worker-1'}
        (self.root/'hosts').write_text('[kube_master]\nmaster-one k8s_nodename=master-1\n'
            '[kube_node]\nworker-one k8s_nodename=worker-1\n'
            '[all:vars]\nansible_connection=upgrade_test_forbidden\n'
            'ansible_python_interpreter='+sys.executable+'\n')
        self.variables = dict(UPGRADE_SOURCE_VERSION='v1.33.6', K8S_VER='1.34.13',
            UPGRADE_PRECONDITIONS_VERIFIED=True, UPGRADE_RUN_ID='handoff-1', SECURE_PORT=6443,
            UPGRADE_API_BACKENDS=['https://master-one:6443'],
            base_dir='/fixture', cluster_dir='/fixture', bin_dir='/fixture/bin', ca_dir='/fixture/ssl',
            UPGRADE_DRAIN_TIMEOUT_SECONDS=300, CONTAINER_RUNTIME='docker', CGROUP_DRIVER='systemd',
            DOCKER_STORAGE_DIR='/fixture/docker-data',
            UPGRADE_CRI_DOCKERD={'path': '/fixture/source/cri-dockerd', 'sha256': sha('cri0410')},
            UPGRADE_DOCKER_PRESERVED_FILES={p: sha(p) for p in [
                '/etc/docker/daemon.json', '/etc/systemd/system/docker.service',
                '/etc/systemd/system/cri-dockerd.service']})
        self.source = dict(source='v1.33.6', target='v1.34.13', mutation='none',
            kubectl_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            cluster_identity={'namespace': 'kube-system', 'uid': 'cluster-uid'},
            api_backends=[{'server': 'https://master-one:6443', 'gitVersion': 'v1.33.6', 'readyz_rc': 0}],
            nodes=[{'name': name, 'uid': name+'-uid', 'Ready': True,
                    'originally_schedulable': True, 'kubeletVersion': 'v1.33.6'}
                   for name in self.hosts.values()])
        self.state = dict(apis={'master-one': 'v1.33.6'},
            controllers={'master-one': {s: 'v1.33.6' for s in ['kube-controller-manager', 'kube-scheduler']}},
            nodes={host: {'name': name, 'uid': name+'-uid', 'cordoned': False,
                          'kubelet': 'v1.33.6', 'kube-proxy': 'v1.33.6',
                          'adapter': '0.3.26', 'adapter_hash': sha('cri0326')}
                   for host, name in self.hosts.items()})
        self.save(self.state)
        self.launched = []; self.business_calls = 0; self.business_failure = None
        self.stage = self.make_stage()

    def save(self, value):
        (self.root/'state.json').write_text(json.dumps(value))

    def read(self):
        return json.loads((self.root/'state.json').read_text())

    def trace(self):
        path = self.root/'trace.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def make_stage(self):
        return KubernetesStage(self.source, self.hosts, self.variables, self.binary,
                               self.config, self.run_book)

    def run_book(self, path, *, extra_vars):
        self.launched.append(path.name)
        (self.root/'variables.json').write_text(json.dumps(extra_vars))
        result = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
            '-i', str(self.root/'hosts'), str(self.root/path.name),
            '-e', '@'+str(self.root/'variables.json')], cwd=ROOT, capture_output=True,
            text=True, timeout=30, env={**os.environ,
                'UPGRADE_KUBERNETES_TEST_ROOT': str(self.root),
                'ANSIBLE_ACTION_PLUGINS': str(self.root/'actions'),
                'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections'),
                'ANSIBLE_LOCAL_TEMP': str(self.root/'ansible-local')})
        self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', result.stdout+result.stderr)
        self.assertNotIn('unmodelled', result.stdout+result.stderr)
        self.assertNotIn('Unexpected templating', result.stdout+result.stderr)
        (self.root/(str(len(self.launched))+'-'+path.stem+'.log')).write_text(result.stdout+result.stderr)
        if result.returncode and not self.read().get('fail'):
            self.assertRegex(result.stdout+result.stderr, r'Assertion failed|"assertion":',
                             result.stdout+result.stderr)
        return SimpleNamespace(rc=result.returncode)

    def business(self):
        self.business_calls += 1
        if self.business_calls == self.business_failure:
            raise StageFailed(17)

    def journal(self, binding=None, steps=None):
        return Journal(self.root/'journal', binding or {**self.stage.binding, 'run_id': 'handoff-1'},
                       steps or self.stage.steps)

    def execute(self):
        with self.journal() as journal:
            self.stage.run(journal, self.business)

    def test_actual_control_plane_runtime_node_handoffs_and_durable_completion(self):
        self.source = observe(SimpleNamespace(source='v1.33.6', target='v1.34.13',
            kubectl=self.binary, kubectl_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            kubeconfig=self.config, api_server=self.variables['UPGRADE_API_BACKENDS'],
            node=list(self.hosts.values()), output=self.root/'source-observation.json'))
        self.stage = self.make_stage()
        self.execute()
        self.assertEqual(self.launched, ['kubernetes-observe.yml', 'control-plane.yml', 'node.yml', 'node.yml'])
        self.assertEqual(self.business_calls, 4)
        state = self.read()
        self.assertTrue(all(n['kubelet'] == n['kube-proxy'] == 'v1.34.13' and
                            not n['cordoned'] and n['adapter'] == '0.4.10' for n in state['nodes'].values()))
        journal = json.loads((self.root/'journal/journal.json').read_text())
        self.assertEqual(journal['completed'], self.stage.steps)
        self.assertEqual(journal['failures'], [])
        names = [t['name'] for t in self.trace()]
        self.assertLess(names.index('Verify the executable of each running controller process'),
                        names.index('Drain with eviction PDB and local-data protection'))
        for host in self.hosts:
            calls = [t for t in self.trace() if t['host'] == host]
            mutations = [t for t in calls if t['module'] == 'systemd_service']
            services = [t['args']['name'] for t in mutations]
            self.assertLess(services.index('cri-dockerd'), services.index('kubelet'))

    def test_adapter_first_failure_preserves_active_cordon_and_resume_only_owned_node(self):
        self.state['fail'] = 'Restart only the changed Docker CRI adapter:master-one'
        self.save(self.state)
        with self.assertRaises(StageFailed):
            self.execute()
        failed = self.read()
        self.assertTrue(failed['nodes']['master-one']['cordoned'])
        self.assertFalse(failed['nodes']['worker-one']['cordoned'])
        with self.journal() as journal:
            self.assertEqual(journal.data['completed'], [CONTROL])
            self.assertEqual(journal.data['active'], self.stage.steps[1])
            first = deepcopy(journal.data['failures'][0])
        self.assertEqual(self.launched, ['kubernetes-observe.yml', 'control-plane.yml', 'node.yml'])
        calls_before = len(self.trace())
        del failed['fail']; self.save(failed)
        self.execute()
        later = self.trace()[calls_before:]
        self.assertFalse([c for c in later if c['module'] == 'systemd_service' and
                          c['args']['name'] in ['kube-apiserver', 'kube-scheduler', 'kube-controller-manager']])
        with self.journal() as journal:
            self.assertEqual(journal.data['failures'][0], first)
            self.assertEqual(journal.data['completed'], self.stage.steps)

    def test_business_failure_after_control_plane_never_drains_and_resume_reobserves(self):
        self.business_failure = 2
        with self.assertRaises(StageFailed):
            self.execute()
        self.assertFalse([t for t in self.trace() if 'drain' in t['args'].get('argv', [])])
        with self.journal() as journal:
            self.assertEqual(journal.data['active'], CONTROL)
            self.assertEqual(journal.data['failures'][0]['rc'], 17)
        calls_before = len(self.trace()); self.business_failure = None
        self.execute()
        self.assertFalse([t for t in self.trace()[calls_before:] if t['module'] == 'copy' and
                          'kube-apiserver' in t['args'].get('src', '')])

    def test_partial_ha_api_failure_never_launches_nodes_or_restarts_healthy_api_on_resume(self):
        self.hosts = {'master-one': 'master-1', 'master-two': 'master-2', 'worker-one': 'worker-1'}
        with (self.root/'hosts').open('a') as out:
            out.write('\n[kube_master]\nmaster-two k8s_nodename=master-2\n')
        self.variables['UPGRADE_API_BACKENDS'].append('https://master-two:6443')
        self.source['api_backends'].append({'server': 'https://master-two:6443',
                                          'gitVersion': 'v1.33.6', 'readyz_rc': 0})
        self.source['nodes'].append({'name': 'master-2', 'uid': 'master-2-uid', 'Ready': True,
                                    'originally_schedulable': True, 'kubeletVersion': 'v1.33.6'})
        state = self.read()
        state['apis']['master-two'] = 'v1.33.6'
        state['controllers']['master-two'] = deepcopy(state['controllers']['master-one'])
        state['nodes']['master-two'] = {**deepcopy(state['nodes']['master-one']),
                                        'name': 'master-2', 'uid': 'master-2-uid'}
        state['fail'] = 'Restart only this API server:master-two'
        self.save(state); self.stage = self.make_stage()
        with self.assertRaises(StageFailed): self.execute()
        state = self.read()
        self.assertEqual(state['apis'], {'master-one': 'v1.34.13', 'master-two': 'v1.33.6'})
        self.assertFalse([t for t in self.trace() if 'drain' in t['args'].get('argv', [])])
        with self.journal() as journal:
            self.assertEqual(journal.data['active'], CONTROL)
            self.assertEqual(journal.data['completed'], [])
        before = len(self.trace()); del state['fail']; self.save(state)
        self.execute()
        self.assertFalse([t for t in self.trace()[before:] if t['module'] == 'systemd_service' and
                          t['host'] == 'master-one' and t['args']['name'] == 'kube-apiserver'])

    def test_completed_resume_rechecks_actual_proxy_and_never_replays_mutation(self):
        self.execute(); before = len(self.trace())
        self.execute()
        self.assertFalse([t for t in self.trace()[before:] if t['module'] in ['copy', 'systemd_service']])
        state = self.read(); state['nodes']['worker-one']['kube-proxy'] = 'v1.33.6'; self.save(state)
        with self.assertRaises(StageFailed):
            self.execute()

    def test_source_foreign_cluster_node_replacement_or_cordon_refuses_before_playbooks(self):
        for fault in ['cluster', 'uid', 'cordon', 'early-kubelet', 'early-api']:
            with self.subTest(fault=fault):
                state = deepcopy(self.state)
                if fault == 'cluster': state['cluster_uid'] = 'foreign'
                elif fault == 'uid': state['nodes']['worker-one']['uid'] = 'replacement'
                elif fault == 'cordon': state['nodes']['worker-one']['cordoned'] = True
                elif fault == 'early-kubelet': state['nodes']['worker-one']['kubelet'] = 'v1.34.13'
                else: state['apis']['master-one'] = 'v1.34.13'
                self.save(state)
                with self.assertRaises(Refused): self.execute()
                self.assertEqual(self.launched, [])

    def test_changed_inputs_order_and_missing_preconditions_refuse_without_execution(self):
        for key, value in [('UPGRADE_PRECONDITIONS_VERIFIED', False), ('K8S_VER', '1.35.1')]:
            original = self.variables[key]; self.variables[key] = value
            with self.assertRaises(Refused): self.make_stage()
            self.variables[key] = original
        with Journal(self.root/'wrong-order', {**self.stage.binding, 'run_id': 'handoff-1'},
                     list(reversed(self.stage.steps))) as journal:
            with self.assertRaises(Refused): self.stage.run(journal, self.business)
        self.assertEqual(self.launched, [])
        # A different preserved unit/config fingerprint cannot acquire the old journal.
        self.variables['CGROUP_DRIVER'] = 'cgroupfs'
        changed = self.make_stage()
        with self.journal() as journal:
            with self.assertRaises(Refused): changed.run(journal, self.business)
        self.assertEqual(self.launched, [])


if __name__ == '__main__':
    unittest.main()
