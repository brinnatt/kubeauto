"""Real Ansible inventory/template events hand off to the real source observer.

No action stubs: assert/debug run on the controller. Any attempted connection is
forbidden. Kubernetes API responses alone are the existing offline CLI fixture.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ansible_runner
import yaml

from upgrade.product.inventory import BOOK, collect, observe_source
from upgrade.product.kubernetes_preflight import Refused
from common.exceptions import ClusterManageError
from service.cluster.manager import ClusterManager
from upgrade.tests.test_control_plane import NO_CONNECTION
from upgrade.tests import test_kubernetes_preflight as source_fixture


ROOT = Path(__file__).resolve().parents[2]


class InventoryContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root/'runner').mkdir(mode=0o700)
        (self.root/'connections').mkdir()
        (self.root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
        self.hosts = '''[etcd]
192.168.47.134
[kube_master:children]
control
[control]
192.168.47.134 k8s_nodename=master-1
192.168.47.135 k8s_nodename=master-2 SECURE_PORT=7443
[kube_node]
192.168.47.134
192.168.47.131 k8s_nodename=worker-1
[all:vars]
ansible_connection=upgrade_test_forbidden
ansible_user=fixture
ansible_password=private-fixture-password
SECURE_PORT="6443"
CONTAINER_RUNTIME=containerd
CLUSTER_NETWORK=calico
'''
        # Use the actual project's node-name template, including its fallback.
        self.variables = yaml.safe_load((ROOT/'conf/config.yml').read_text())
        self.variables.update(K8S_VER='1.34.13')
        self.calls = []
        self.limit = None
        self.binary = self.root/'kubectl'
        self.binary.write_text(source_fixture.FAKE); self.binary.chmod(0o700)
        self.kubeconfig = self.root/'kubeconfig'; self.kubeconfig.write_text('offline TLS fixture')

    def runner(self, book=BOOK, *, extra_vars=None, controller_only=False):
        self.calls.append((book, deepcopy(extra_vars)))
        self.controller_only = controller_only
        (self.root/'hosts').write_text(self.hosts)
        result = ansible_runner.run(private_data_dir=str(self.root/'runner'),
            playbook=str(book), inventory=str(self.root/'hosts'), extravars=extra_vars or self.variables,
            quiet=True, cmdline=('--limit '+self.limit) if self.limit else '',
            envvars={'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections'),
                     'ANSIBLE_LOCAL_TEMP': str(self.root/'local'),
                     'PATH': str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
                     'ANSIBLE_PYTHON_INTERPRETER': sys.executable})
        with result.stdout as stream:
            self.output = stream.read()
        self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', self.output)
        self.assertNotIn('private-fixture-password', self.output)
        self.events = list(result.events)
        return SimpleNamespace(rc=result.rc, events=self.events)

    def observed(self):
        return collect(self.runner(extra_vars=self.variables))

    def test_real_group_children_precedence_and_aio_membership_are_complete(self):
        inventory = self.observed()
        self.assertEqual(inventory['api_backends'],
            ['https://192.168.47.134:6443', 'https://192.168.47.135:7443'])
        self.assertEqual(inventory['nodes'], {'192.168.47.134': 'master-1',
            '192.168.47.135': 'master-2', '192.168.47.131': 'worker-1'})
        self.assertEqual(len(inventory['hosts']), 3)
        self.assertEqual(inventory['runtime'], 'containerd')
        self.assertEqual(inventory['network'], 'calico')
        self.assertNotIn('private-fixture-password', json.dumps(inventory))

    def test_config_extra_vars_have_real_product_precedence(self):
        self.variables.update(SECURE_PORT=8443, CONTAINER_RUNTIME='docker',
                              K8S_NODENAME="{{ 'node-' ~ inventory_hostname | replace('.', '-') }}")
        inventory = self.observed()
        self.assertEqual(inventory['api_backends'],
            ['https://192.168.47.134:8443', 'https://192.168.47.135:8443'])
        self.assertEqual(inventory['runtime'], 'docker')
        self.assertEqual(inventory['nodes']['192.168.47.134'], 'node-192-168-47-134')

    def test_actual_product_fallback_node_names_are_used(self):
        self.hosts = self.hosts.replace('k8s_nodename=worker-1', "k8s_nodename=''")
        self.assertEqual(self.observed()['nodes']['192.168.47.131'], 'k8s-192-168-47-131')

    def test_limited_run_cannot_hide_a_master_or_the_topology(self):
        for limit in ['localhost,192.168.47.134', 'kube_master']:
            with self.subTest(limit=limit):
                self.limit = limit
                with self.assertRaisesRegex(Refused, 'limited|topology'):
                    self.observed()

    def test_effective_duplicate_node_names_and_missing_templates_refuse(self):
        self.hosts = self.hosts.replace('k8s_nodename=worker-1', 'k8s_nodename=master-1')
        with self.assertRaisesRegex(Refused, 'unique'):
            self.observed()
        self.variables['K8S_NODENAME'] = '{{ missing_required_name }}'
        with self.assertRaisesRegex(Refused, 'observation failed'):
            self.observed()

    def test_mixed_runtime_alias_and_invalid_api_port_require_review(self):
        for extra, error in [('CONTAINER_RUNTIME=docker', 'mixed effective'),
                             ('ansible_host=192.168.47.136', 'aliases'),
                             ('SECURE_PORT=70000', 'secure port')]:
            with self.subTest(extra=extra):
                original = self.hosts
                self.hosts = original.replace('k8s_nodename=master-2 SECURE_PORT=7443',
                    'k8s_nodename=master-2 '+extra)
                with self.assertRaisesRegex(Refused, error):
                    self.observed()
                self.hosts = original

    def test_missing_etcd_and_target_configuration_are_refused(self):
        self.hosts = self.hosts.replace('[etcd]\n192.168.47.134', '[etcd]')
        with self.assertRaisesRegex(Refused, 'observation failed'):
            self.observed()
        with self.assertRaisesRegex(Refused, 'exact first-hop'):
            observe_source(self.runner, {'K8S_VER': '1.35.10'}, kubectl=self.binary,
                kubectl_sha256='', kubeconfig=self.kubeconfig, output=self.root/'receipt.json')

    def test_runner_nonzero_missing_duplicate_or_foreign_events_never_accept(self):
        result = self.runner()
        with self.assertRaises(Refused):
            collect(SimpleNamespace(rc=7, events=result.events))
        observations = [e for e in result.events if e.get('event') == 'runner_on_ok' and
                        isinstance(e.get('event_data', {}).get('res', {}).get('msg'), dict)]
        for events in [observations[:-1], [*observations, observations[-1]]]:
            with self.assertRaises(Refused):
                collect(SimpleNamespace(rc=0, events=events))
        foreign = deepcopy(observations)
        foreign[-1]['event_data']['host'] = 'foreign'
        with self.assertRaisesRegex(Refused, 'foreign'):
            collect(SimpleNamespace(rc=0, events=foreign))

    def test_real_inventory_handoff_observes_every_api_and_exact_node_set(self):
        names = ['master-1', 'master-2', 'worker-1']
        namespace = {'metadata': {'name': 'kube-system', 'uid': 'cluster-uid'},
                     'status': {'phase': 'Active'}}
        responses = [{'stdout': json.dumps({'clientVersion': {'gitVersion': 'v1.34.13'}})}]
        for _ in range(2):
            responses.extend([{'stdout': json.dumps({'gitVersion': 'v1.33.6'})},
                              {'stdout': 'ok'}, {'stdout': json.dumps(namespace)}])
        responses.append({'stdout': json.dumps({'items':
            [source_fixture.SourcePreflightContracts.node(name) for name in names]})})
        (self.root/'responses.json').write_text(json.dumps(responses))
        receipt = observe_source(self.runner, self.variables, kubectl=self.binary,
            kubectl_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            kubeconfig=self.kubeconfig, output=self.root/'receipt.json')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.controller_only)
        self.assertEqual([n['name'] for n in receipt['nodes']], names)
        self.assertEqual([b['server'] for b in receipt['api_backends']],
                         receipt['effective_inventory']['api_backends'])
        self.assertEqual(receipt['mutation'], 'none')
        self.assertFalse(receipt['parent_acceptance'])
        calls = [json.loads(line) for line in (self.root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls), 8)
        self.assertTrue(all('--insecure-skip-tls-verify=false' in call for call in calls))
        self.assertFalse((self.root/'receipt.json').exists())  # Caller owns durable writing.
        self.assertEqual(receipt['inventory_observer_sha256'], hashlib.sha256(BOOK.read_bytes()).hexdigest())

    def test_actual_product_runner_preserves_only_observations_after_temp_cleanup(self):
        cluster = self.root/'lab'; cluster.mkdir()
        (cluster/'hosts').write_text(self.hosts)
        (cluster/'config.yml').write_text(yaml.safe_dump(self.variables))
        manager = ClusterManager.__new__(ClusterManager)
        manager.clusters_dir = self.root
        env = {'PATH': str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
               'ANSIBLE_CONNECTION_PLUGINS': str(self.root/'connections'),
               'ANSIBLE_LOCAL_TEMP': str(self.root/'local')}
        with (patch.object(manager, '_ansible_runner_envvars', return_value=env),
              patch('service.cluster.manager.get_host_ip', return_value='192.168.47.130'),
              patch('service.cluster.manager._prepare_inventory_with_python',
                    side_effect=AssertionError('read-only observation must not bootstrap a host')) as prepare,
              patch('service.cluster.manager.ansible_python_policy',
                    side_effect=AssertionError('target policy is irrelevant to controller-only actions'))):
            result = manager._run_playbook('lab', BOOK, controller_only=True)
        prepare.assert_not_called()
        self.assertEqual(result.rc, 0)
        self.assertFalse(Path(result.config.private_data_dir).exists())
        inventory = collect(result)  # Must work after the real runner directory is gone.
        self.assertEqual(len(inventory['api_backends']), 2)
        self.assertEqual(len(inventory['nodes']), 3)
        self.assertNotIn('private-fixture-password', json.dumps(result.upgrade_inventory_events))
        self.assertTrue(all(set(event) == {'event', 'event_data'}
                            for event in result.upgrade_inventory_events))

    def test_product_runner_cannot_use_controller_only_to_bypass_other_playbooks(self):
        manager = ClusterManager.__new__(ClusterManager)
        manager.clusters_dir = self.root
        with (patch('service.cluster.manager._prepare_inventory_with_python') as prepare,
              patch('service.cluster.manager.ansible_runner.run') as run):
            with self.assertRaisesRegex(ClusterManageError, 'restricted'):
                manager._run_playbook('lab', ROOT/'upgrade/product/node.yml',
                                      extra_vars={}, controller_only=True)
        prepare.assert_not_called()
        run.assert_not_called()
