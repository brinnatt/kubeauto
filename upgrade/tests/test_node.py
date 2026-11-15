"""Real node-stage refusal paths; no lab connection or service mutation."""
import copy
import json
from pathlib import Path
import unittest

import yaml

from upgrade.tests import test_control_plane as fixture


BOOK = Path(__file__).resolve().parents[1] / 'product/node.yml'


class NodeRefusals(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ControlPlaneRefusals(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.book = BOOK
        (self.fixture.root / 'hosts').write_text(
            '[kube_master]\n'
            'master-one k8s_nodename=master-1 ansible_connection=upgrade_test_forbidden\n'
            'master-two k8s_nodename=master-2 ansible_connection=upgrade_test_forbidden\n'
            '[kube_node]\nworker-one k8s_nodename=worker-1 ansible_connection=upgrade_test_forbidden\n')
        self.fixture.variables.update(UPGRADE_NODE_HOST='master-one',
            UPGRADE_NODE_NAME='master-1', UPGRADE_NODE_UID='node-uid-1',
            UPGRADE_ORIGINALLY_SCHEDULABLE=True, UPGRADE_DRAIN_TIMEOUT_SECONDS=300)
        self.node = {'metadata': {'name': 'master-1', 'uid': 'node-uid-1'}, 'spec': {},
                     'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                                'nodeInfo': {'kubeletVersion': 'v1.33.6'}}}
        self.responses = [{'stdout': json.dumps({'gitVersion': 'v1.34.13'})}] * 2
        self.responses += [{'stdout': 'ok'}] * 2

    def run_book(self, responses=(), start=None):
        return self.fixture.run_book(responses, start)

    def test_no_admission_no_commands_or_mutation(self):
        self.fixture.variables['UPGRADE_PRECONDITIONS_VERIFIED'] = False
        result = self.run_book()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.fixture.calls(), [])

    def test_remaining_source_api_blocks_drain_and_remote_operations(self):
        responses = [{'stdout': json.dumps({'gitVersion': v})}
                     for v in ['v1.34.13', 'v1.33.6']]
        result = self.run_book(responses)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.calls()), 2)
        self.assertNotIn('Drain with eviction', result.stdout)

    def test_changed_uid_not_ready_and_foreign_cordon_refuse_before_eviction(self):
        for kind in ['uid', 'ready', 'cordon', 'target-without-resume']:
            with self.subTest(kind=kind):
                node = copy.deepcopy(self.node)
                if kind == 'uid':
                    node['metadata']['uid'] = 'replacement-node'
                elif kind == 'ready':
                    node['status']['conditions'][0]['status'] = 'False'
                elif kind == 'cordon':
                    node['spec']['unschedulable'] = True
                else:
                    node['status']['nodeInfo']['kubeletVersion'] = 'v1.34.13'
                self.fixture.variables['upgrade_node_observation'] = {'stdout': json.dumps(node)}
                result = self.run_book(start='Reject replaced unhealthy or foreign-cordoned nodes')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.fixture.calls(), [])
                self.assertNotIn('Read eviction API', result.stdout)

    def test_missing_or_wrong_eviction_support_refuses_drain_delete_fallback(self):
        for resources in [[], [{'name': 'pods/eviction', 'kind': 'Eviction', 'group': 'policy',
                               'version': 'v1beta1'}]]:
            self.fixture.variables['upgrade_eviction_discovery'] = {
                'stdout': json.dumps({'resources': resources})}
            result = self.run_book(start='Require policy v1 eviction support before maintenance')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(self.fixture.calls(), [])
            self.assertNotIn('Drain with eviction', result.stdout)

    def test_drain_failure_leaves_node_boundary_and_never_replaces_or_uncordons(self):
        result = self.run_book([{'rc': 1, 'stdout': 'eviction refused'}],
            start='Drain with eviction PDB and local-data protection')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        calls = self.fixture.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('drain', calls[0])
        self.assertIn('--timeout=300s', calls[0])
        self.assertNotIn('Replace only the drained', result.stdout)
        self.assertNotIn('Return the originally', result.stdout)

    def test_unknown_running_node_version_refuses_before_drain(self):
        self.fixture.variables['upgrade_prior_node_processes'] = {'results': [
            {'item': 'kubelet', 'stdout': 'Kubernetes v1.35.10'}]}
        result = self.run_book(start='Reject unexpected running node versions before drain')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.fixture.calls(), [])
        self.assertNotIn('Drain with eviction', result.stdout)

    def test_bounded_serial_order_preserves_configuration_and_pdb_protections(self):
        plays = yaml.safe_load(BOOK.read_text())
        self.assertTrue(all(p['any_errors_fatal'] for p in plays))
        self.assertEqual(plays[1]['serial'], 1)
        tasks = plays[1]['tasks']
        names = [task['name'] for task in tasks]
        self.assertLess(names.index('Drain with eviction PDB and local-data protection'),
                        names.index('Replace only the drained node executables'))
        self.assertGreater(names.index('Return the originally schedulable owned node to service'),
                           names.index('Wait for the same node Ready with the exact target kubelet'))
        for task in tasks:
            self.assertFalse(set(task) & {'ignore_errors', 'rescue', 'always'})
        body = BOOK.read_text()
        for forbidden in ['--force', '--delete-emptydir-data', '--disable-eviction',
                          '--grace-period=0', 'include_role:', 'import_role:',
                          'insecure-skip-tls-verify=true']:
            self.assertNotIn(forbidden, body)
        copies = [task for task in tasks if 'ansible.builtin.copy' in task]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]['loop'], ['kubelet', 'kube-proxy'])
        self.assertIn('when', copies[0])

    def test_original_scheduling_state_is_a_real_boolean_and_may_be_false(self):
        plays = yaml.safe_load(BOOK.read_text())
        admission = plays[0]['tasks'][0]['ansible.builtin.assert']['that']
        self.assertIn('UPGRADE_ORIGINALLY_SCHEDULABLE is boolean', admission)
        self.assertNotIn('UPGRADE_ORIGINALLY_SCHEDULABLE | default(false) | bool', admission)
        scheduling = plays[0]['tasks'][5]['ansible.builtin.assert']['that']
        self.assertTrue(any('not (UPGRADE_ORIGINALLY_SCHEDULABLE | bool)' in item
                            for item in scheduling))


if __name__ == '__main__':
    unittest.main()
