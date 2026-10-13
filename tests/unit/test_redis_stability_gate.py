"""Disprove restart identity shortcuts before any stability fault is launched."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_stability_contract', ROOT / 'tests/helpers/redis-stability-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class StabilityTests(unittest.TestCase):
    def test_operator_uses_official_deployment_selector_instead_of_helm_release_pod_label(self):
        deployment = {'metadata': {'name': 'redis-operator',
                      'annotations': {'meta.helm.sh/release-name': 'redis-operator'}},
                      'spec': {'replicas': 2, 'selector': {'matchLabels': {'name': 'redis-operator'}},
                               'template': {'metadata': {'labels': {'name': 'redis-operator'}}}}}
        self.assertEqual(gate.operator_selector(deployment), 'name=redis-operator')
        for edit in [lambda d: d['spec']['selector']['matchLabels'].update(name='foreign'),
                     lambda d: d['spec'].update(replicas=1),
                     lambda d: d['metadata'].update(name='foreign'),
                     lambda d: d['spec']['selector'].update(matchExpressions=[{}])]:
            bad = deepcopy(deployment)
            edit(bad)
            with self.assertRaises(ValueError):
                gate.operator_selector(bad)
        instance = object.__new__(gate.Stability)
        instance.source = Mock()
        instance.source.config = {'redis_operator_namespace': 'owned-operator'}
        instance.source.k.return_value = '{"items": []}'
        instance.operator_labels = gate.operator_selector(deployment)
        instance.operator_pods()
        instance.source.k.assert_called_once_with('-n', 'owned-operator', 'get', 'pods', '-l',
                                                 'name=redis-operator', '-o', 'json')
        source = (ROOT / 'tests/helpers/redis-stability-gate.py').read_text()
        self.assertNotIn('app.kubernetes.io/instance=redis-operator', source)
        self.assertLess(source.index('self.operator_labels = operator_selector(deployment)'),
                        source.index('def sample('))

    def test_journal_update_refuses_foreign_identity_and_removes_failed_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'restart.json'
            original = {'run_id': 'owned', 'cluster_uid': 'cluster', 'cr_uid': 'cr', 'stage': 'seed'}
            gate.write_journal(path, original, create=True)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                gate.write_journal(path, dict(original, run_id='foreign'))
            self.assertEqual(before, path.read_bytes())
            with patch.object(gate.json, 'dump', side_effect=ValueError('serialization')):
                with self.assertRaises(ValueError):
                    gate.write_journal(path, dict(original, stage='pod'))
            self.assertEqual(before, path.read_bytes())
            self.assertFalse(path.with_suffix('.tmp').exists())
            gate.write_journal(path, dict(original, stage='pod'))
            self.assertEqual(json.loads(path.read_text())['stage'], 'pod')

    def test_worker_reboot_allowlist_refuses_control_foreign_or_unhealthy_nodes(self):
        node = {'metadata': {'name': 'redis-node-131', 'uid': 'owned'}, 'spec': {},
                'status': {'addresses': [{'type': 'InternalIP', 'address': '192.168.47.131'}],
                           'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        self.assertEqual(gate.worker_identity(node), ('192.168.47.131', 'owned'))
        for name, address in [('redis-node-134', '192.168.47.134'), ('redis-node-131', '192.168.47.129'),
                              ('redis-node-137', '192.168.47.137'), ('other-131', '192.168.47.131')]:
            bad = deepcopy(node)
            bad['metadata']['name'] = name
            bad['status']['addresses'][0]['address'] = address
            with self.assertRaises(ValueError):
                gate.worker_identity(bad)
        for field in ['cordoned', 'not-ready']:
            bad = deepcopy(node)
            if field == 'cordoned': bad['spec']['unschedulable'] = True
            else: bad['status']['conditions'][0]['status'] = 'False'
            with self.assertRaises(ValueError):
                gate.worker_identity(bad)

    def test_reboot_requires_successful_probe_and_a_different_valid_boot_uuid(self):
        before = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        after = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        self.assertTrue(gate.reboot_proven(before, SimpleNamespace(returncode=0, stdout=after + '\n')))
        for rc, reply in [(255, after), (0, before), (0, ''), (0, 'command failed'), (0, 'OTHER')]:
            self.assertFalse(gate.reboot_proven(before, SimpleNamespace(returncode=rc, stdout=reply)))

    def test_running_old_ready_pod_never_proves_a_process_restart(self):
        before = {'metadata': {'name': 'data-1', 'uid': 'old'},
                  'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        self.assertFalse(gate.replacement_ready(before, before))
        after = deepcopy(before)
        after['metadata']['uid'] = 'new'
        self.assertTrue(gate.replacement_ready(before, after))
        after['metadata']['deletionTimestamp'] = 'now'
        self.assertFalse(gate.replacement_ready(before, after))

    def test_retention_reads_precede_any_post_restart_write(self):
        instance = object.__new__(gate.Stability)
        instance.source, instance.cluster, instance.focused = Mock(), Mock(), Mock()
        instance.source.forwards = {}
        instance.source.value = b'acknowledged'
        instance.key, instance.dataset = 'owned', {'cluster-owned': b'cluster-value'}
        instance.claims = []
        instance.focused.resource_identities.return_value = []
        instance.sample = Mock()
        instance.source.client.return_value.command.return_value = b'lost'
        with self.assertRaises(ValueError):
            instance.verify_retained()
        instance.sample.assert_not_called()
        instance.source.client.return_value.close.assert_called_once()
        instance.source.client.return_value.command.return_value = b'acknowledged'
        instance.cluster.command.return_value = b'lost'
        with self.assertRaises(ValueError):
            instance.verify_retained()
        instance.sample.assert_not_called()

    def test_faults_only_cannot_claim_soak_and_complete_gate_enforces_30_minutes(self):
        source = (ROOT / 'tests/helpers/redis-stability-gate.py').read_text()
        self.assertIn('args.seconds < 1800', source)
        self.assertIn('REDIS_STABILITY_FAULTS_PASS acceptance=partial-no-soak', source)
        instance = object.__new__(gate.Stability)
        instance.journal, instance.state = Mock(), {}
        instance.sample, instance.pod_restart = Mock(), Mock()
        instance.operator_restart, instance.worker_restart = Mock(), Mock()
        with patch.object(gate, 'write_journal'), patch('builtins.print') as output:
            instance.execute(0)
        lines = [str(call.args[0]) for call in output.call_args_list]
        self.assertTrue(any('REDIS_STABILITY_FAULTS_PASS' in line for line in lines))
        self.assertFalse(any(line.startswith('REDIS_STABILITY_PASS ') for line in lines))

    def test_restart_dispatch_and_acceptance_require_all_fault_and_clean_markers(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        shell = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        self.assertIn('"$MODE" == --redis-stability-faults', runner)
        self.assertIn('"$MODE" == --redis-stability', runner)
        self.assertIn('script=redis-stability-gate.py', shell)
        spec = importlib.util.spec_from_file_location('stability_evidence_contract', ROOT / 'tests/helpers/redis-evidence.py')
        evidence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(evidence)
        self.assertNotIn('stability-faults', evidence.CASES)
        markers = ['REDIS_STABILITY_PASS seconds=1800 samples=100 business-errors=0 '
                   'restarts=pod+operator+node data-and-pvc=retained',
                   'REDIS_FOCUSED_SCOPED_CLEAN_PASS module=stability',
                   'REDIS_FOCUSED_MODULE_PASS module=stability', 'REDIS_GATE_EXIT rc=0',
                   *evidence.EXTRA_MARKERS['stability']]
        self.assertEqual(evidence.verify_log('stability', '\n'.join(markers), 'state=exited rc=0')[0], 'REDIS-21')
        for missing in evidence.EXTRA_MARKERS['stability']:
            with self.assertRaises(ValueError):
                evidence.verify_log('stability', '\n'.join(m for m in markers if m != missing), 'state=exited rc=0')
        for old, new in [('seconds=1800', 'seconds=1799'), ('samples=100', 'samples=0'),
                         ('business-errors=0', 'business-errors=1')]:
            with self.assertRaises(ValueError):
                evidence.verify_log('stability', '\n'.join(markers).replace(old, new), 'state=exited rc=0')


if __name__ == '__main__':
    unittest.main()
