"""Network refusal attribution, exact identities and interruption-safe cleanup."""
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('network_probe', ROOT/'tests/helpers/longhorn-network.py')
network = importlib.util.module_from_spec(spec); spec.loader.exec_module(network)


class NetworkContracts(unittest.TestCase):
    def fixture(self, directory):
        policies = {'items': [{'metadata': {'name': 'longhorn-manager', 'uid': 'policy-uid'},
                              'spec': {'policyTypes': ['Ingress']}}]}
        pods = {}
        state = {'run_id': 'run', 'namespace_uid': 'namespace-uid'}
        def get(kind, name='', namespace=''):
            if kind == 'namespace': return {'metadata': {'uid': 'namespace-uid'}}
            if kind == 'networkpolicies': return copy.deepcopy(policies)
            if kind == 'pod': return copy.deepcopy(pods.get(name))
            raise AssertionError(kind)
        def write(path, row):
            path.write_text(json.dumps(row)); path.chmod(0o600)
        def remove(pod):
            self.assertEqual(pods[pod['metadata']['name']]['metadata']['uid'], pod['metadata']['uid'])
            del pods[pod['metadata']['name']]
        lab = SimpleNamespace(DIRECTORY=Path(directory), owner=Mock(return_value=state),
            get=Mock(side_effect=get), write_private=Mock(side_effect=write), remove=Mock(side_effect=remove),
            wait=Mock(side_effect=lambda label, timeout, check: self.assertTrue(check())))
        probe = network.Probe(lab)
        row = {'run_id': state['run_id'], 'namespace_uid': state['namespace_uid'],
               'policies': network.policy_identity(lab), 'pods': {}}
        write(probe.record, row)
        return probe, lab, pods, policies

    def test_socket_timeout_is_distinct_from_refused_and_broken_network(self):
        cases = [(TimeoutError(), {'state': 'timeout'}),
                 (ConnectionRefusedError(), {'state': 'error', 'type': 'ConnectionRefusedError'}),
                 (OSError(113, 'No route to host'), {'state': 'error', 'type': 'OSError'})]
        for error, expected in cases:
            with self.subTest(error=type(error)), patch.object(network.socket, 'create_connection', side_effect=error):
                self.assertEqual(network.connection('10.0.0.2', 9500), expected)
        with patch.object(network.socket, 'create_connection'):
            self.assertEqual(network.connection('10.0.0.2', 9500), {'state': 'connected'})

    def test_http_positive_control_and_metrics_require_valid_response(self):
        for path, payload, status, expected in (
                ('/control', b'kubeauto-network-control', 200, 'connected'),
                ('/control', b'foreign', 200, 'invalid-payload'),
                ('/metrics', b'# HELP longhorn_volume_capacity_bytes', 200, 'connected'),
                ('/metrics', b'go_goroutines 1', 200, 'invalid-payload'),
                ('/v1', b'{"type":"apiVersion"}', 200, 'connected'),
                ('/v1', b'[]', 200, 'invalid-payload'),
                ('/v1', b'not-json', 200, 'error'),
                ('/v1', b'{}', 403, 'invalid-http')):
            client = Mock(); client.getresponse.return_value = SimpleNamespace(status=status, read=Mock(return_value=payload))
            with self.subTest(path=path, payload=payload, status=status), \
                    patch.object(network.http.client, 'HTTPConnection', return_value=client) as factory:
                self.assertEqual(network.connection('10.0.0.2', 9500, path)['state'], expected)
                factory.assert_called_once_with('10.0.0.2', 9500, timeout=3)
                client.close.assert_called_once()

    def test_invalid_probe_destinations_fail_before_connection(self):
        for address, port, path in (('foreign.example', 9500, ''), ('10.0.0.2', True, ''),
                                    ('10.0.0.2', 0, ''), ('10.0.0.2', 9500, '/v1/volumes?delete')):
            with patch.object(network.socket, 'create_connection') as connect:
                with self.assertRaises(ValueError): network.connection(address, port, path)
                connect.assert_not_called()

    def test_expect_refuses_every_failure_except_the_exact_requested_state(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, *_ = self.fixture(directory)
            for state in ('error', 'invalid-http', 'invalid-payload', 'connected'):
                probe.request = Mock(return_value={'state': state})
                with self.assertRaises(ValueError): probe.expect('ns', 'client', '10.0.0.2', 9500, state='timeout')
            probe.request = Mock(return_value={'state': 'timeout'})
            probe.expect('ns', 'client', '10.0.0.2', 9500, state='timeout')

    def test_policy_propagation_keeps_controls_and_never_retries_a_different_failure(self):
        for defect in ('none', 'broken-network'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, *_ = self.fixture(directory)
                probe.check = Mock(); probe.expect = Mock()
                probe.request = Mock(side_effect=[{'state': 'connected'},
                    {'state': 'timeout'} if defect == 'none' else {'state': 'error', 'type': 'ConnectionRefusedError'}])
                def wait(label, timeout, check):
                    self.assertEqual(timeout, 60)
                    self.assertFalse(check())
                    self.assertTrue(check())
                lab.wait = Mock(side_effect=wait)
                args = ({'name': 'denied', 'namespace': 'foreign'}, 'allowed', '10.0.0.3',
                        {'name': 'manager', 'address': '10.0.0.2', 'port': 9500})
                if defect == 'none': probe.await_denial(*args)
                else:
                    with self.assertRaises(ValueError): probe.await_denial(*args)
                self.assertEqual(probe.check.call_count, 2)
                self.assertEqual(probe.expect.call_count, 4)

    def test_create_intent_recovers_a_lost_response_and_refuses_name_only_adoption(self):
        for interrupted, foreign_nonce in ((False, False), (True, False), (True, True)):
            with self.subTest(interrupted=interrupted, foreign_nonce=foreign_nonce), tempfile.TemporaryDirectory() as directory:
                probe, lab, pods, _ = self.fixture(directory)
                obj = {'metadata': {'name': 'network-owned', 'namespace': network.NAMESPACE, 'labels': {}},
                       'spec': {'containers': [{'image': 'pinned', 'command': ['python3']} ]}}
                def create(*_args, **kwargs):
                    original = json.loads(kwargs['stdin'])
                    receipt = json.loads(probe.record.read_text())['pods']['network-owned']
                    self.assertIsNone(receipt['uid'])
                    self.assertEqual(receipt['nonce'], original['metadata']['annotations']['kubeauto.io/network-probe'])
                    original['metadata']['uid'] = 'original-pod-uid'
                    if foreign_nonce: original['metadata']['annotations']['kubeauto.io/network-probe'] = 'foreign'
                    pods['network-owned'] = original
                    if interrupted: raise RuntimeError('response was lost')
                    return subprocess.CompletedProcess([], 0, json.dumps(original), '')
                lab.kubectl = Mock(side_effect=create)
                if interrupted:
                    with self.assertRaises(RuntimeError): probe.create_system(copy.deepcopy(obj))
                else:
                    self.assertEqual(probe.create_system(copy.deepcopy(obj))['metadata']['uid'], 'original-pod-uid')
                with redirect_stdout(io.StringIO()):
                    if foreign_nonce:
                        with self.assertRaises(ValueError): probe.cleanup()
                        lab.remove.assert_not_called()
                        self.assertTrue(probe.record.exists())
                    else:
                        probe.cleanup()
                        lab.remove.assert_called_once()
                        self.assertFalse(probe.record.exists())

    def test_cleanup_validates_all_pods_and_policy_before_any_delete(self):
        for defect in ('uid', 'labels', 'owner', 'policy-uid', 'policy-spec', 'namespace', 'run'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, pods, policies = self.fixture(directory)
                row = json.loads(probe.record.read_text())
                for name in ('first', 'second'):
                    pod = {'metadata': {'name': name, 'uid': name+'-uid', 'labels': {},
                                       'annotations': {'kubeauto.io/network-probe': name}},
                           'spec': {'containers': [{'image': 'pinned', 'command': ['python3']}]}}
                    pods[name] = pod
                    row['pods'][name] = {'uid': name+'-uid', 'labels': {}, 'nonce': name,
                                         'image': 'pinned', 'command': ['python3']}
                if defect == 'uid': pods['second']['metadata']['uid'] = 'foreign'
                if defect == 'labels': pods['second']['metadata']['labels'] = {'foreign': 'true'}
                if defect == 'owner': pods['second']['metadata']['ownerReferences'] = [{'uid': 'foreign'}]
                if defect == 'policy-uid': policies['items'][0]['metadata']['uid'] = 'foreign'
                if defect == 'policy-spec': policies['items'][0]['spec'] = {'ingress': [{}]}
                if defect == 'namespace': row['namespace_uid'] = 'foreign'
                if defect == 'run': row['run_id'] = 'foreign'
                probe.record.write_text(json.dumps(row))
                with self.assertRaises(ValueError): probe.cleanup()
                lab.remove.assert_not_called()
                self.assertTrue(probe.record.exists())

    def test_probe_pods_have_no_storage_or_api_token_and_do_not_join_services(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, lab, *_ = self.fixture(directory)
            lab.WORKERS = ('192.168.122.212', '192.168.122.165')
            lab.client_pod = Mock(side_effect=lambda ns, name, claim, node: {
                'metadata': {'name': name, 'namespace': ns}, 'spec': {'volumes': [{}],
                    'nodeSelector': {'kubernetes.io/hostname': node},
                    'containers': [{'volumeMounts': [{}], 'image': 'published', 'command': ['python3']}]}})
            for control in (False, True):
                obj = probe.pod('ns', 'probe', {'longhorn.io/job-task': 'probe'}, control=control)
                self.assertNotIn('volumes', obj['spec'])
                self.assertNotIn('volumeMounts', obj['spec']['containers'][0])
                self.assertFalse(obj['spec']['automountServiceAccountToken'])
                self.assertNotIn('app', obj['metadata']['labels'])
                if control: compile(obj['spec']['containers'][0]['command'][-1], 'control-server', 'exec')

    def test_api_server_checks_are_dry_run_for_every_owned_control(self):
        for defect in ('none', 'response', 'persisted'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, *_ = self.fixture(directory)
                lab.NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72')
                lab.get = Mock(side_effect=[None, {'metadata': {'uid': 'unexpected'}} if defect == 'persisted' else None])
                def dry_run(*args, **kwargs):
                    self.assertIn('--dry-run=server', args)
                    self.assertEqual(args[1:3], ('create', '--dry-run=server'))
                    obj = json.loads(kwargs['stdin'])
                    self.assertEqual(obj['spec']['volume'], 'pvc-owned')
                    if defect == 'response': obj['spec']['volume'] = 'foreign'
                    return subprocess.CompletedProcess([], 0, json.dumps(obj), '')
                lab.kubectl = Mock(side_effect=dry_run)
                with redirect_stdout(io.StringIO()):
                    if defect == 'none': probe.verify_ha({'volume': 'pvc-owned'})
                    else:
                        with self.assertRaises(ValueError): probe.verify_ha({'volume': 'pvc-owned'})
                if defect == 'none':
                    self.assertEqual([call.args[0] for call in lab.kubectl.call_args_list],
                                     ['--server=https://'+node+':6443' for node in lab.NODES])

    def test_targets_require_one_ready_manager_and_instance_manager_on_each_storage_worker(self):
        for defect in ('none', 'missing', 'extra', 'duplicate-node', 'not-ready', 'deleting', 'foreign-node'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, *_ = self.fixture(directory)
                lab.WORKERS = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
                pods = []
                for role, labels in (('manager', {'app': 'longhorn-manager'}),
                                     ('im', {'longhorn.io/component': 'instance-manager'})):
                    for index, address in enumerate(lab.WORKERS):
                        pods.append({'metadata': {'name': role+'-'+str(index), 'uid': role+'-uid-'+str(index),
                                                  'labels': labels},
                                     'spec': {'nodeName': 'longhorn-node-'+address.rsplit('.', 1)[1]},
                                     'status': {'podIP': '10.0.0.'+str(len(pods)+1),
                                                'conditions': [{'type': 'Ready', 'status': 'True'}]}})
                if defect == 'missing': pods.pop()
                if defect == 'extra': pods.append(copy.deepcopy(pods[-1]))
                if defect == 'duplicate-node': pods[-1]['spec']['nodeName'] = pods[-2]['spec']['nodeName']
                if defect == 'foreign-node': pods[-1]['spec']['nodeName'] = 'unowned-worker'
                if defect == 'not-ready': pods[-1]['status']['conditions'][0]['status'] = 'False'
                if defect == 'deleting': pods[-1]['metadata']['deletionTimestamp'] = '2026-10-19T00:00:00Z'
                lab.get = Mock(return_value={'items': pods})
                if defect == 'none':
                    targets = probe.targets()
                    self.assertEqual({key: len(value) for key, value in targets.items()},
                                     {'manager': 3, 'instance-manager': 3})
                    self.assertEqual({row['uid'] for group in targets.values() for row in group},
                                     {pod['metadata']['uid'] for pod in pods})
                    self.assertEqual({row['port'] for row in targets['manager']}, {9500})
                    self.assertEqual({row['port'] for row in targets['instance-manager']}, {8500})
                else:
                    with self.assertRaises(ValueError): probe.targets()

    def test_full_probe_requires_paired_controls_ha_and_cleanup_on_first_failure(self):
        from contextlib import contextmanager
        for defect in ('none', 'positive-control'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, _pods, policies = self.fixture(directory)
                probe.record.unlink()
                for name in ('instance-manager', 'longhorn-webhook'):
                    policies['items'].append({'metadata': {'name': name, 'uid': name+'-uid'}, 'spec': {}})
                @contextmanager
                def cleanup_scope(cleanup):
                    try: yield
                    finally: cleanup()
                lab.scoped_cleanup = cleanup_scope
                original_get = lab.get.side_effect
                def get(kind, name='', namespace=''):
                    if kind == 'pod': return {'metadata': {'name': name, 'namespace': namespace},
                        'status': {'podIP': '10.0.0.3', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                    return original_get(kind, name, namespace)
                lab.get.side_effect = get
                probe.pod = Mock(side_effect=lambda ns, name, labels=None, **kwargs:
                                 {'metadata': {'name': name, 'namespace': ns, 'labels': labels or {}}})
                probe.create_system = Mock(side_effect=lambda obj: obj)
                probe.cleanup = Mock(); probe.check = Mock(); probe.verify_ha = Mock()
                probe.await_denial = Mock()
                probe.targets = Mock(return_value={'manager': [{'name': 'manager', 'uid': 'm-uid', 'address': '10.0.0.2', 'port': 9500}],
                    'instance-manager': [{'name': 'im', 'uid': 'im-uid', 'address': '10.0.0.4', 'port': 8500}]})
                probe.request = Mock(return_value={'state': 'connected'})
                probe.expect = Mock(side_effect=ValueError('positive prerequisite') if defect != 'none' else None)
                with redirect_stdout(io.StringIO()) as output:
                    if defect == 'none': probe.verify({'namespace': 'foreign', 'volume': 'pvc-owned'}, lambda obj: obj)
                    else:
                        with self.assertRaises(ValueError): probe.verify({'namespace': 'foreign', 'volume': 'pvc-owned'}, lambda obj: obj)
                probe.cleanup.assert_called_once()
                if defect == 'none':
                    probe.verify_ha.assert_called_once_with({'namespace': 'foreign', 'volume': 'pvc-owned'})
                    observations = [json.loads(line.split(' ', 1)[1]) for line in output.getvalue().splitlines()
                                    if line.startswith('LONGHORN_NETWORK_POLICY_OBSERVATION ')]
                    self.assertEqual(observations[0]['round_timeout_observations'], 12)
                    denials = [call for call in probe.expect.call_args_list if call.kwargs.get('state') == 'timeout']
                    self.assertEqual({call.args[0] for call in denials}, {'foreign', network.NAMESPACE})
                    for before, after in zip(denials, denials[1:]):
                        self.assertNotEqual(before.args[1], after.args[1])
                else:
                    probe.verify_ha.assert_not_called()
                    probe.await_denial.assert_not_called()
                    self.assertNotIn('LONGHORN_NETWORK_POLICY_OBSERVATION', output.getvalue())


if __name__ == '__main__':
    unittest.main()
