"""Contracts for the three-fault, real-duration V1 confirmed-write owner."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v1_stability', ROOT/'tests/helpers/longhorn-v1-stability.py')
GATE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GATE)


def fixture_log():
    token = 'owned:v1-stability'; faults = GATE.load('longhorn-faults.py')
    records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()}
               for i in range(16)]
    proof = GATE.load('longhorn-control-faults.py').confirmed(records, token, faults)
    rows = [{'type': 'started', 'token': token, 'pid': 1, 'start_tick': '2'}]
    rows += [dict(r, observed_elapsed_s=i*1799/15) for i, r in enumerate(records)]
    rows.append(dict(proof['summary'], type='complete', observed_elapsed_s=1800., errors={}, retries=0, max_latency_s=.01))
    checked = GATE.load('longhorn-v2-observability.py').confirmed_load(rows, token)
    topology = {n: {'uid': n, 'node': n} for n in ('a', 'b', 'c')}
    observations = [
        {'kind': 'controller', 'intent': True, 'recovered': True, 'before_acks': 4, 'after_acks': 8,
         'pod_uid': 'old-manager', 'observations': {'pod_uid': 'new-manager', 'replicas': topology}},
        {'kind': 'worker-reboot', 'intent': True, 'recovered': True, 'before_acks': 8, 'after_acks': 12,
         'original_host': {'machine_id': 'machine', 'boot_id': 'old'},
         'observations': {'host': {'machine_id': 'machine', 'boot_id': 'new'}, 'replicas': topology}},
        {'kind': 'replica-process', 'intent': True, 'recovered': True, 'before_acks': 12, 'after_acks': 16,
         'native': {'uuid': 'native', 'name': 'c'}, 'before': topology,
         'observations': {'original_native_uuid_deleted': True,
                          'replicas': dict({n: v for n, v in topology.items() if n != 'c'}, d={'uid': 'd', 'node': 'c'})}}]
    return [('LONGHORN_V1_STABILITY_ACKNOWLEDGEMENTS', {'token': token, 'rows': rows}),
            ('LONGHORN_V1_STABILITY_RESULT', {k: checked[k] for k in ('summary', 'timeline', 'errors', 'retries', 'max_latency_s')}),
            ('LONGHORN_V1_STABILITY_REMOUNT', checked['summary'])] + [
            ('LONGHORN_V1_STORAGE_FAULT_RECOVERED', row) for row in observations]


def render(rows):
    return '\n'.join([k+' '+json.dumps(v) for k, v in rows]+[
        'LONGHORN_V1_STABILITY_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
        'LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP'])


class StabilityContracts(unittest.TestCase):
    def test_full_raw_ledger_three_faults_and_remount_pass(self):
        GATE.verify_log(render(fixture_log()), 'stability')

    def test_missing_fault_duration_reused_uid_or_bad_hash_never_pass(self):
        for mutation in ('duration', 'hash', 'fault', 'boot', 'manager', 'survivor', 'overlap', 'after', 'failure', 'terminal'):
            rows = copy.deepcopy(fixture_log())
            if mutation == 'duration': rows[0][1]['rows'][-1]['observed_elapsed_s'] = 1799
            if mutation == 'hash': rows[0][1]['rows'][1]['sha256'] = '0'*64
            if mutation == 'fault': rows.pop(4)
            if mutation == 'boot': rows[4][1]['observations']['host']['boot_id'] = 'old'
            if mutation == 'manager': rows[3][1]['observations']['pod_uid'] = 'old-manager'
            if mutation == 'survivor': rows[5][1]['observations']['replicas']['a'] = {'uid': 'other', 'node': 'a'}
            if mutation == 'overlap': rows[4][1]['before_acks'] = 4
            if mutation == 'after': rows[5][1]['after_acks'] = 17
            log = render(rows)
            if mutation == 'failure': log += '\nLONGHORN_FIRST_FAILURE phase=unit'
            if mutation == 'terminal': log = log.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', '')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): GATE.verify_log(log, 'stability')

    def test_node_ready_and_exact_scheduling_policy_are_distinct_from_new_identity(self):
        saved = {'uid': 'node', 'unschedulable': False, 'taints': []}
        node = {'metadata': {'uid': 'node'}, 'spec': {}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        lab = SimpleNamespace(get=lambda *a: node)
        self.assertTrue(GATE.node_restored(lab, 'node', saved))
        node['spec']['taints'] = [{'key': 'node.kubernetes.io/not-ready', 'effect': 'NoExecute'}]
        self.assertFalse(GATE.node_restored(lab, 'node', saved))
        node['metadata']['uid'] = 'replacement'
        with self.assertRaises(ValueError): GATE.node_restored(lab, 'node', saved)

    def test_source_requires_exact_one_volume_before_shared_fault(self):
        lab = SimpleNamespace(get=lambda *a, **kw: {'items': [
            {'metadata': {'name': 'source', 'uid': 'source'}}, {'metadata': {'name': 'foreign', 'uid': 'foreign'}}]},
            check_business_graph=Mock())
        with patch.object(GATE, 'load', return_value=SimpleNamespace(source=lambda *a: {'engine': 'owned'})), \
                self.assertRaises(ValueError): GATE.source(lab, {'volume': 'source', 'volume_uid': 'source'})
        lab.check_business_graph.assert_not_called()

    def test_controller_replacement_may_have_a_new_daemonset_generated_name(self):
        old = {'metadata': {'name': 'manager-old', 'uid': 'old'}, 'spec': {'nodeName': 'node'}}
        new = {'metadata': {'name': 'manager-new', 'uid': 'new'}, 'spec': {'nodeName': 'node'}}
        parent = {'kind': 'DaemonSet', 'uid': 'ds', 'node': 'node'}
        control = SimpleNamespace(fault_pod=lambda *a: (old, parent), recovered=lambda *a: True,
            controlled=lambda *a: True, ready=lambda *a: True)
        order = []
        lab = SimpleNamespace(get=lambda kind, *a, **kw: {'items': [new]} if kind == 'pods' else old,
            remove=lambda p: order.append('remove'), wait=lambda name, timeout, check: check(), replica_topology=lambda *a: {'topology': True})
        probe = GATE.Probe(lab, 1); probe.journal = {'namespace': 'owned'}
        def begin(kind, row): order.append('intent'); return dict(row, kind=kind)
        probe.begin = begin; probe.finish = Mock()
        with patch.object(GATE, 'source', return_value={'node': 'node'}), patch.object(GATE, 'load', return_value=control):
            probe.manager()
        self.assertEqual(['intent', 'remove'], order)
        self.assertEqual('new', probe.finish.call_args.args[1]['pod_uid'])

    def test_unavailable_host_only_accepts_bounded_ssh_outage(self):
        lab = SimpleNamespace(WORKERS=['192.168.122.212'])
        runtime = SimpleNamespace(host_call=Mock(return_value=SimpleNamespace(returncode=255, stdout='')))
        with patch.object(GATE, 'load', return_value=runtime):
            self.assertIsNone(GATE.host(lab, '192.168.122.212', unavailable=True))
            runtime.host_call.return_value = SimpleNamespace(returncode=1, stdout='')
            with self.assertRaises(ValueError): GATE.host(lab, '192.168.122.212', unavailable=True)
            with self.assertRaises(ValueError): GATE.host(lab, '192.168.122.1', unavailable=True)

    def test_cleanup_fences_remote_writer_after_latest_confirmed_prefix_readback(self):
        token = 'run:v1-stability'; faults = GATE.load('longhorn-faults.py')
        records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()} for i in range(4)]
        summary = GATE.load('longhorn-control-faults.py').confirmed(records, token, faults)['summary']
        with tempfile.TemporaryDirectory(prefix='kubeauto-longhorn-v1-faults-', dir='/var/tmp') as directory:
            path = Path(directory)/'ledger.log'; path.write_text('\n'.join(json.dumps(dict(r, observed_elapsed_s=i)) for i, r in enumerate(records)))
            order = []
            lab = SimpleNamespace(owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster'},
                replica_topology=lambda *a: {'healthy': True}, installation_health=lambda: order.append('health'),
                get=lambda *a: {'metadata': {'uid': 'pod'}},
                wait=lambda name, timeout, check: order.append(name) if 'fenced' in name else check(),
                kubectl=lambda *a, **kw: (order.append('readback') or SimpleNamespace(stdout=json.dumps(summary))),
                remove=lambda p: order.append('fence'))
            probe = GATE.Probe(lab, 1)
            probe.journal = {'namespace': 'owned', 'volume_uid': 'volume', 'objects': {'Pod/writer': 'pod'}, GATE.KEY: {
                'run_id': 'run', 'cluster_uid': 'cluster', 'volume_uid': 'volume', 'faults': [],
                'token': token, 'log_directory': directory}}
            probe.restore()
            self.assertLess(order.index('readback'), order.index('fence'))

    def test_outer_cleanup_restores_before_deletion_and_refuses_another_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/GATE.RECORD; path.write_text(json.dumps({GATE.KEY: {}}))
            order = []
            lab = SimpleNamespace(DIRECTORY=Path(directory), check_business_graph=lambda j: order.append('graph'),
                cleanup_business=lambda **kw: order.append('delete'))
            with patch.object(GATE, 'Probe', return_value=SimpleNamespace(restore=lambda: order.append('restore'))):
                GATE.cleanup(lab, 1)
            self.assertEqual(['graph', 'restore', 'delete'], order)
            path.write_text('{}')
            with self.assertRaises(ValueError): GATE.cleanup(lab, 1)

    def test_fixed_wrapper_requires_durable_exit_raw_log_verifier_and_scoped_cleanup(self):
        path = ROOT/'tests/helpers/longhorn-v1-stability.sh'
        subprocess.run(['bash', '-n', str(path)], check=True)
        source = path.read_text()
        self.assertEqual(1, source.count('"$LAB" relay-maintain &'))
        self.assertIn('"$work/business.finalized")" == 0', source)
        self.assertIn('"$DRIVER" verify --log', source)
        self.assertIn('LONGHORN_STABILITY_PASS', source)
        self.assertLess(source.index('"$DRIVER" cleanup'), source.index('"$LAB" relay-clean'))


if __name__ == '__main__': unittest.main()
