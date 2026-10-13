"""Platform prerequisite contracts before spending a stateful Redis run."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_platform_contract', ROOT / 'tests/helpers/redis-platform-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def nodes():
    return [{'metadata': {'name': f'redis-node-{i}', 'labels': {'kubernetes.io/hostname': f'domain-{i}'}},
             'spec': {'unschedulable': i >= 134}, 'status': {
                 'addresses': [{'type': 'InternalIP', 'address': f'192.168.47.{i}'}],
                 'conditions': [{'type': 'Ready', 'status': 'True'},
                                {'type': 'MemoryPressure', 'status': 'False'}]}} for i in range(131, 137)]


class PlatformTests(unittest.TestCase):
    def test_prerequisite_requires_exact_leased_nodes_and_three_schedulable_domains(self):
        valid = nodes()
        self.assertEqual(len(gate.node_contract(valid)), 6)
        for mutate in (lambda n: n.pop(), lambda n: n.append(deepcopy(n[0])),
                       lambda n: n[0]['status']['addresses'][0].update(address='192.168.47.129'),
                       lambda n: n[0]['spec'].update(unschedulable=True),
                       lambda n: n[0]['spec'].update(taints=[{'effect': 'NoSchedule'}]),
                       lambda n: n[0]['status']['conditions'][0].update(status='False'),
                       lambda n: n[0]['status']['conditions'][1].update(status='True'),
                       lambda n: n[0]['metadata']['labels'].update({'kubernetes.io/hostname':'domain-132'})):
            bad = deepcopy(valid)
            mutate(bad)
            with self.assertRaises(ValueError):
                gate.node_contract(bad)

    def test_probe_has_matching_namespace_claim_mount_and_fixed_published_image(self):
        pvc, pod = gate.fixture_objects('redis-gate', 'redis-platform-owned', 'owned')
        self.assertEqual(pvc['metadata'], pod['metadata'])
        self.assertEqual(pvc['spec']['storageClassName'], 'redis-longhorn')
        self.assertEqual(pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName'], pvc['metadata']['name'])
        self.assertEqual(pod['spec']['containers'][0]['image'],
                         'registry.talkschool.cn:5000/brinnatt/redis:8.10.2')
        for ns, name in [('foreign', 'redis-platform-owned'), ('redis-gate', 'customer-data')]:
            with self.assertRaises(ValueError):
                gate.fixture_objects(ns, name, 'owned')
