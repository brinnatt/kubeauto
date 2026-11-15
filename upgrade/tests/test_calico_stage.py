"""Actual subprocess handoffs for both fixed etcdv3 Calico migration edges."""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from upgrade.product.calico_stage import (CONTROLLER, NODE, SECRET, CalicoStage,
    CalicoRefused, CalicoCommandFailed, delta, manifests, matches, new_container_defaults)


FIXTURE = Path(__file__).parent/'fixtures/calico_kubectl.py'
VARIABLES = {'ETCD_ENDPOINTS': 'https://etcd.test:2379', 'CALICO_NETWORKING_BACKEND': 'bird',
    'CALICO_ENABLE_OVERLAY': 'Always', 'CLUSTER_CIDR': '10.244.0.0/16',
    'ca_dir': '/etc/kubernetes/ssl', 'IP_AUTODETECTION_METHOD': 'interface=ens.*',
    'NODE_PORT_RANGE': '30000-32767'}


def source_objects(version, variables):
    objects = manifests(variables, version)
    objects[SECRET] = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
        'metadata': {'name': 'calico-etcd-secrets', 'namespace': 'kube-system'},
        'data': {'etcd-ca': 'private-ca', 'etcd-cert': 'private-cert', 'etcd-key': 'private-key'}}
    for ref, obj in objects.items():
        obj['metadata'].update(uid=ref+'-uid', resourceVersion='1', generation=1)
        if obj['kind'] not in ['DaemonSet', 'Deployment']:
            continue
        pod = obj['spec']['template']['spec']
        for container in pod.get('containers', [])+pod.get('initContainers', []):
            container.setdefault('imagePullPolicy', 'IfNotPresent')
            container.setdefault('terminationMessagePath', '/dev/termination-log')
            container.setdefault('terminationMessagePolicy', 'File')
            container.setdefault('resources', {})
        if obj['kind'] == 'DaemonSet':
            obj['status'] = dict(observedGeneration=1, desiredNumberScheduled=2,
                currentNumberScheduled=2, updatedNumberScheduled=2, numberReady=2,
                numberAvailable=2, numberMisscheduled=0)
        else:
            obj['status'] = dict(observedGeneration=1, replicas=1, updatedReplicas=1,
                                 readyReplicas=1, availableReplicas=1)
    return objects


class CalicoStageContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binary = self.root/'kubectl'
        self.binary.write_text('#!'+sys.executable+'\n'+FIXTURE.read_text())
        self.binary.chmod(0o700)
        (self.root/'kubeconfig').write_text('offline fixture')
        self.source = {'source': 'v1.33.6', 'target': 'v1.34.13', 'mutation': 'none',
            'cluster_identity': {'namespace': 'kube-system', 'uid': 'cluster-uid'},
            'api_backends': [{'server': 'https://master-one:6443'},
                             {'server': 'https://master-two:6443'}],
            'nodes': [{'name': 'node1', 'uid': 'uid1'}, {'name': 'node2', 'uid': 'uid2'}]}
        self.events = []
        self.configure(('v3.28.4', 'v3.31.7'))

    def configure(self, edge):
        self.stage = CalicoStage(self.binary, self.root/'kubeconfig', self.source, VARIABLES,
                                 edge, run_id='contract-run', timeout=30, progress=self.events.append)
        self.reviewed = source_objects(edge[0], VARIABLES)
        self.state = {'objects': deepcopy(self.reviewed), 'platform': self.stage.platform,
            'nodes': [{'metadata': {'name': n['name'], 'uid': n['uid']}, 'status': {
                'nodeInfo': {'kubeletVersion': self.stage.platform},
                'conditions': [{'type': 'Ready', 'status': 'True'}]}} for n in self.source['nodes']]}
        self.save()

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state))

    def calls(self):
        path = self.root/'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def mutations(self):
        return [c for c in self.calls() if c[0] == 'patch' and '--dry-run=server' not in c]

    def creations(self):
        return [c for c in self.calls() if c[0] == 'create' and '--dry-run=server' not in c]

    def execute(self, **callbacks):
        gates = {k: (lambda k=k: self.events.append(k))
                 for k in ['verify_hosts', 'verify_datastore', 'verify_traffic']}
        gates['install_client'] = lambda version: self.events.append('client '+version)
        gates.update(callbacks)
        return self.stage.run(self.reviewed, **gates)

    def test_real_cli_edges_preserve_identity_defaults_secret_and_exact_order(self):
        for edge in [('v3.28.4', 'v3.31.7'), ('v3.31.7', 'v3.32.4')]:
            with self.subTest(edge=edge):
                self.configure(edge)
                (self.root/'calls.jsonl').unlink(missing_ok=True)
                result = self.execute()
                expected = ['clusterrole/calico-kube-controllers']
                if edge[0] == 'v3.28.4':
                    expected.append('configmap/calico-config')
                expected += [NODE, CONTROLLER]
                self.assertEqual([c[1] for c in self.mutations()], expected)
                calls = self.calls()
                self.assertTrue(all(calls.index(c) < calls.index(self.mutations()[0])
                                    for c in calls if '--dry-run=server' in c))
                self.assertFalse(result['parent_acceptance'])
                current = json.loads((self.root/'state.json').read_text())['objects']
                for ref in self.reviewed:
                    self.assertEqual(current[ref]['metadata']['uid'], self.reviewed[ref]['metadata']['uid'])
                self.assertEqual(current[SECRET], self.reviewed[SECRET])
                self.assertIn('client '+edge[1], self.events)
                for ref, obj in self.stage.after.items():
                    self.assertTrue(matches(current[ref], obj), ref)
                self.assertEqual(len(self.creations()), len(self.stage.new))
                self.assertTrue(all(calls.index(c) < calls.index(self.mutations()[0])
                                    for c in self.creations()))
                before = len(self.mutations())
                self.execute()  # Completed matching edge observes; it does not repatch.
                self.assertEqual(len(self.mutations()), before)
                self.assertEqual(len(self.creations()), len(self.stage.new))

    def test_all_original_backends_overlays_and_defaults_survive_reviewed_delta(self):
        for backend, overlay in [('bird', 'Always'), ('bird', 'Never'), ('vxlan', 'CrossSubnet')]:
            variables = {**VARIABLES, 'CALICO_NETWORKING_BACKEND': backend,
                         'CALICO_ENABLE_OVERLAY': overlay}
            for before, after in [('v3.28.4', 'v3.31.7'), ('v3.31.7', 'v3.32.4')]:
                original = source_objects(before, variables)
                source, target = manifests(variables, before), manifests(variables, after)
                for ref, obj in source.items():
                    actual = delta(original[ref], obj, target[ref])
                    new_container_defaults(original[ref], actual)
                    self.assertEqual(actual['metadata'], original[ref]['metadata'])
                    if ref == NODE:
                        pod = actual['spec']['template']['spec']
                        for c in pod['containers']+pod['initContainers']:
                            self.assertEqual(c['terminationMessagePolicy'], 'File')
                        self.assertEqual(actual['spec']['selector'], original[ref]['spec']['selector'])
                    if ref == 'configmap/calico-config':
                        self.assertEqual(actual['data']['etcd_endpoints'], original[ref]['data']['etcd_endpoints'])

    def test_source_customization_unknown_edge_or_missing_secret_refuses_before_commands(self):
        for fault in ['image', 'extra-env', 'secret']:
            with self.subTest(fault=fault):
                saved = deepcopy(self.reviewed)
                if fault == 'secret':
                    del self.reviewed[SECRET]
                else:
                    agent = self.reviewed[NODE]['spec']['template']['spec']['containers'][0]
                    if fault == 'image': agent['image'] = 'unknown:latest'
                    else: agent['env'].append({'name': 'CUSTOM_FEATURE', 'value': 'true'})
                with self.assertRaises(CalicoRefused): self.execute()
                self.assertEqual(self.calls(), [])
                self.reviewed = saved
        with self.assertRaises(CalicoRefused):
            CalicoStage(self.binary, self.root/'kubeconfig', self.source, VARIABLES,
                        ('v3.28.4', 'v3.32.4'), run_id='contract-run')

    def test_foreign_backend_node_or_cordon_refuses_before_mutation(self):
        for fault in ['backend', 'uid', 'cordon', 'cluster']:
            saved = deepcopy(self.state)
            if fault == 'backend': self.state['apis'] = {'https://master-two:6443': 'v1.34.13'}
            elif fault == 'uid': self.state['nodes'][0]['metadata']['uid'] = 'foreign'
            elif fault == 'cordon': self.state['nodes'][0]['spec'] = {'unschedulable': True}
            else: self.state['cluster_uid'] = 'foreign'
            self.save()
            with self.subTest(fault=fault), self.assertRaises(CalicoRefused): self.execute()
            self.assertEqual(self.mutations(), [])
            self.state = saved

    def test_server_dryrun_admission_mutation_blocks_every_write(self):
        self.state['admission_mutation'] = NODE
        self.save()
        with self.assertRaisesRegex(CalicoRefused, 'dry-run'): self.execute()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.creations(), [])

    def test_foreign_new_tier_rbac_is_never_adopted_or_deleted(self):
        for fault in ['missing-owner', 'changed-subject']:
            self.configure(('v3.28.4', 'v3.31.7'))
            ref = self.stage.new[1]
            foreign = deepcopy(self.stage.after[ref])
            foreign['metadata'].update(uid='foreign', resourceVersion='1')
            if fault == 'missing-owner': foreign['metadata'].pop('labels')
            else: foreign['subjects'][0]['name'] = 'unreviewed-user'
            self.state['objects'][ref] = foreign
            self.save()
            with self.subTest(fault=fault), self.assertRaisesRegex(CalicoRefused, 'adoption refused'):
                self.execute()
            self.assertEqual(self.mutations(), [])
            self.assertEqual(self.creations(), [])
            self.assertFalse(any(c[0] == 'delete' for c in self.calls()))

    def test_conflict_stops_then_exact_saved_prefix_resumes_without_repatch(self):
        self.state['conflict'] = NODE
        self.save()
        with self.assertRaises(CalicoCommandFailed) as failure: self.execute()
        self.assertEqual(failure.exception.rc, 42)
        self.assertEqual([c[1] for c in self.mutations()],
                         ['clusterrole/calico-kube-controllers', 'configmap/calico-config', NODE])
        self.assertNotIn('client v3.31.7', self.events)
        self.state = json.loads((self.root/'state.json').read_text())
        del self.state['conflict']
        self.save()
        self.execute()
        self.assertEqual([c[1] for c in self.mutations()][3:], [NODE, CONTROLLER])
        self.assertEqual(len(self.creations()), 2)

    def test_nonprefix_progress_or_unreviewed_target_mutation_is_not_a_resume(self):
        target = delta(self.reviewed[CONTROLLER], self.stage.before[CONTROLLER],
                       self.stage.after[CONTROLLER])
        self.state['objects'][CONTROLLER] = target
        self.save()
        with self.assertRaisesRegex(CalicoRefused, 'prefix'): self.execute()
        self.assertEqual(self.mutations(), [])
        self.configure(('v3.28.4', 'v3.31.7'))
        target['spec']['template']['spec']['unreviewed'] = True
        self.state['objects'][CONTROLLER] = target
        self.save()
        with self.assertRaisesRegex(CalicoRefused, 'saved transition'): self.execute()
        self.assertEqual(self.mutations(), [])

    def test_tls_secret_drift_stops_dependent_rollout_and_retains_first_boundary(self):
        self.state['drift_after'] = 'configmap/calico-config'
        self.save()
        with self.assertRaisesRegex(CalicoRefused, 'Secret or ownership drift'): self.execute()
        self.assertEqual([c[1] for c in self.mutations()],
                         ['clusterrole/calico-kube-controllers', 'configmap/calico-config'])
        self.assertFalse(any(c[0] in ['delete', 'apply'] for c in self.calls()))

    def test_host_datastore_traffic_and_client_failures_are_propagated(self):
        for gate in ['verify_hosts', 'verify_datastore', 'verify_traffic']:
            def refused(): raise CalicoRefused('real prerequisite refused')
            with self.subTest(gate=gate), self.assertRaisesRegex(CalicoRefused, 'prerequisite'):
                self.execute(**{gate: refused})
            self.assertEqual(self.mutations(), [])
        def client_refused(version): raise CalicoRefused('client identity refused')
        with self.assertRaisesRegex(CalicoRefused, 'client identity'):
            self.execute(install_client=client_refused)
        self.assertEqual(len(self.mutations()), 4)

    def test_rollout_timeout_is_bounded_and_stops_before_controllers(self):
        self.state['stalled'] = NODE
        self.save()
        with patch('upgrade.product.calico_stage.time.monotonic', side_effect=itertools.count(0, 31)), \
                patch('upgrade.product.calico_stage.time.sleep'), \
                self.assertRaisesRegex(CalicoRefused, 'bounded window'):
            self.execute()
        self.assertEqual([c[1] for c in self.mutations()][-1], NODE)
        self.assertNotIn('client v3.31.7', self.events)


if __name__ == '__main__':
    unittest.main()
