"""Native V2 runtime fault identity, bounded mutation and recovery contracts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_runtime_faults', ROOT/'tests/helpers/longhorn-v2-runtime-faults.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
COMMON = GATE.load('longhorn-v2-control-faults.py')


class RuntimeFaultContracts(unittest.TestCase):
    def fixture(self, base):
        runtime = {'volume_uid': 'volume-uid', 'client_node': 'longhorn-node-146',
            'managers': {f'im{i}': {'uid': f'parent-{i}', 'pod_uid': f'pod-{i}',
                'spec': {'nodeID': f'longhorn-node-{146+i}', 'image': 'fixed-image'}} for i in range(3)},
            'instances': {'engines.longhorn.io': {'engine': {'uid': 'engine-uid', 'manager': 'im0'}},
                          'enginefrontends.longhorn.io': {'frontend': {'uid': 'ef-uid', 'manager': 'im0'}},
                          'replicas.longhorn.io': {f'replica{i}': {'uid': f'r-{i}', 'manager': f'im{i}'} for i in range(3)}}}
        common = SimpleNamespace(boundary=Mock(return_value=({}, {'parent': {'node': 'longhorn-node-147'},
            'replica': 'replica', 'replica_uid': 'replica-uid'})), owner=COMMON.owner, pod_ready=COMMON.pod_ready)
        manager = SimpleNamespace(runtime=Mock(return_value=runtime))
        boot = SimpleNamespace(arm=Mock(), complete=Mock(return_value=True), clean=Mock(),
            check_inactive_kubelet=Mock(), recover_kubelet=Mock())
        resources = SimpleNamespace(probe=Mock(return_value={'kubelet_active': 'active'}))
        objects = {
            'volumes.longhorn.io': {'metadata': {'uid': 'volume-uid'}, 'spec': {'dataEngine': 'v2'},
                                   'status': {'remountRequestedAt': ''}},
            'engines.longhorn.io': {'metadata': {'uid': 'engine-uid'}, 'status': {
                'currentReplicaAddressMap': {'replica': '172.31.1.2:20001'}, 'replicaModeMap': {'replica': 'ERR'}, 'ip': '172.31.1.1'}},
            'replicas.longhorn.io': {'metadata': {'uid': 'replica-uid'}, 'status': {'port': 20001, 'instanceManagerName': 'im1', 'ip': '172.31.1.2'}},
            'instancemanagers.longhorn.io': {'metadata': {'uid': 'parent-1'},
                'spec': {'nodeID': 'longhorn-node-147', 'image': 'fixed-image'}, 'status': {'currentState': 'running'}},
            'pod': {'metadata': {'uid': 'pod-1', 'ownerReferences': [
                {'kind': 'InstanceManager', 'uid': 'parent-1', 'controller': True}]},
                'spec': {'nodeName': 'longhorn-node-147', 'containers': [{'image': 'fixed-image'}]},
                'status': {'podIP': '172.31.1.2', 'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        }
        relay = Mock()
        def load(name):
            if name == 'longhorn-v2-manager-loss.py': return manager
            if name == 'longhorn-v2-control-faults.py': return common
            if name == 'longhorn-v2-boot-fixture.py': return boot
            if name == 'longhorn-v2-resources.py': return resources
            if name == 'longhorn-v2-reboot-relay.py': return relay
            raise ValueError(name)
        topology = {str(i): {'node': str(i), 'uid': 'replica-'+str(i)} for i in range(3)}
        lab = SimpleNamespace(BASE=base, DIRECTORY=base, WORKERS=['192.168.47.'+str(i) for i in range(146,149)],
            owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster', 'raw_disks': {
                '192.168.47.'+str(i): {'machine_id': 'machine-'+str(i)} for i in range(146,149)}}),
            get=Mock(), remote=Mock(), kubectl=Mock(return_value=SimpleNamespace(
                stdout='sl local_address rem_address st\n0: 01011FAC:8000 02011FAC:4E21 01\n')),
            write_private=Mock(), check_business_graph=Mock(), installation_health=Mock(),
            healthy_replicas=Mock(return_value=True), replica_topology=Mock(return_value=topology))
        managers = {}
        for name, parent in runtime['managers'].items():
            row = {'metadata': {'uid': parent['uid']}, 'spec': parent['spec'], 'status': {'currentState': 'running'}}
            for kind, key in (('engines.longhorn.io', 'instanceEngines'),
                              ('enginefrontends.longhorn.io', 'instanceEngineFrontends'),
                              ('replicas.longhorn.io', 'instanceReplicas')):
                row['status'][key] = {instance: {'spec': {'name': instance}, 'status': {'state': 'running'}}
                    for instance, native in runtime['instances'][kind].items() if native['manager'] == name}
            managers[name] = row
        def get(kind, *args, **kwargs):
            if kind == 'pod' and args and args[0] != 'im1':
                row = copy.deepcopy(objects[kind]); index = int(args[0][-1])
                row['metadata']['uid'] = 'pod-'+str(index)
                row['metadata']['ownerReferences'][0]['uid'] = 'parent-'+str(index)
                row['spec']['nodeName'] = 'longhorn-node-'+str(146+index)
                row['status']['podIP'] = '172.31.1.'+str(1+index)
                return row
            if kind == 'instancemanagers.longhorn.io' and args and args[0] != 'im1':
                return copy.deepcopy(managers[args[0]])
            if kind == 'instancemanagers.longhorn.io':
                obj = copy.deepcopy(objects[kind])
                obj['status'].update({key: value for key, value in managers['im1']['status'].items() if key != 'currentState'})
                return obj
            return copy.deepcopy(objects[kind])
        lab.get.side_effect = get
        lab.wait = Mock(side_effect=lambda label, timeout, fn: fn())
        observed = {'machine_id': 'machine-147', 'boot_id': 'old-boot', 'process': {'pid': 7, 'start': '3'}}
        return lab, {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'owned',
                     'objects': {'Pod/writer': 'writer-uid'}}, runtime, common, objects, load, observed

    def test_boundaries_choose_original_attached_or_remote_native_consumer(self):
        for variant in GATE.VARIANTS:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as d:
                lab, journal, runtime, common, objects, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    proof = GATE.boundary(lab, journal, variant)
                self.assertEqual('longhorn-node-146' if variant == 'attached-spdk' else 'longhorn-node-147', proof['node'])
                if variant == 'replica-network':
                    self.assertEqual(('172.31.1.1', '172.31.1.2', 20001),
                                     (proof['source'], proof['destination'], proof['port']))
                    self.assertEqual('192.168.47.146', proof['address'])
                    self.assertFalse(proof['host_network'])
                self.assertEqual(runtime, proof['runtime'])

    def test_transport_rejects_unbounded_foreign_ambiguous_or_changed_endpoint(self):
        for endpoint in ('', '192.168.47.149:20001', '192.168.47.147:22', 'udp://192.168.47.147:20001',
                         'tcp://name:20001', 'tcp://user@192.168.47.147:20001',
                         'tcp://192.168.47.147:20001/path', 'tcp://192.168.47.147:20001?x=1'):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, objects, loader, host = self.fixture(Path(d))
                objects['engines.longhorn.io']['status']['currentReplicaAddressMap']['replica'] = endpoint
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'replica-network')
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, objects, loader, host = self.fixture(Path(d))
            objects['replicas.longhorn.io']['status']['port'] = 20002
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'replica-network')

    def test_transport_requires_original_ready_pod_namespace_and_native_address(self):
        for defect in ('uid', 'owner', 'node', 'ready', 'native-ip'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, objects, loader, host = self.fixture(Path(d))
                pod = objects['pod']
                if defect == 'uid': pod['metadata']['uid'] = 'replacement'
                if defect == 'owner': pod['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'node': pod['spec']['nodeName'] = 'longhorn-node-148'
                if defect == 'ready': pod['status']['conditions'][0]['status'] = 'False'
                if defect == 'native-ip': objects['replicas.longhorn.io']['status']['storageIP'] = '172.31.1.99'
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe',
                        side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'replica-network')
                lab.kubectl.assert_not_called()
                lab.remote.assert_not_called()

    def test_transport_socket_requires_exact_established_data_endpoint_in_im_namespace(self):
        header = 'sl local_address rem_address st\n'
        valid = '0: 01011FAC:8000 02011FAC:4E21 01\n'
        self.assertTrue(GATE.established_socket(header+valid, '172.31.1.1', '172.31.1.2', 20001))
        for row in (valid.replace('4E21', '4E20'), valid.replace('4E21', '4E22'),
                    valid.replace(' 01\n', ' 02\n'), valid.replace('01011FAC', '03011FAC'),
                    valid.replace('02011FAC', '04011FAC'), ''):
            with self.subTest(row=row):
                self.assertFalse(GATE.established_socket(header+row, '172.31.1.1', '172.31.1.2', 20001))
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            lab.kubectl.return_value.stdout = header+valid.replace('4E21', '4E22')
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe',
                    side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                with self.assertRaisesRegex(ValueError, 'actual established'): GATE.boundary(lab, journal, 'replica-network')
            lab.kubectl.assert_called_once_with('-n', GATE.NS, 'exec', 'im0', '-c', 'instance-manager',
                                                '--', 'cat', '/proc/net/tcp', timeout=30)

    def test_kill_persists_original_intent_before_mutation_and_never_replays_uncertain_write(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            calls = []
            lab.write_private.side_effect = lambda *a: calls.append('journal')
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                proof = GATE.boundary(lab, journal, 'remote-spdk')
            def fail(*args):
                calls.append('signal')
                raise TimeoutError('unknown SSH response')
            with patch.object(GATE, 'boundary', return_value=proof), patch.object(GATE, 'host_call', side_effect=fail):
                with self.assertRaises(TimeoutError): GATE.inject_case(lab, journal, 'remote-spdk')
                with self.assertRaises(ValueError): GATE.inject_case(lab, journal, 'remote-spdk')
            self.assertEqual(['journal', 'signal'], calls)
            self.assertEqual(proof, journal[GATE.KEY])

    def test_changed_boundary_or_missing_original_target_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            with patch.object(GATE, 'boundary', side_effect=[{'a': 1}, {'a': 2}]), patch.object(GATE, 'host_call') as call:
                with self.assertRaises(ValueError): GATE.inject_case(lab, journal, 'remote-spdk')
            call.assert_not_called()
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=dict(host, process=None)):
                with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'remote-spdk')

    def test_transport_unknown_partial_write_is_recovered_before_business_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                proof = GATE.boundary(lab, journal, 'replica-network')
            responses = [SimpleNamespace(stdout=json.dumps({'present': [False, False]})), TimeoutError('partial add')]
            with patch.object(GATE, 'boundary', return_value=proof), patch.object(GATE, 'host_call', side_effect=responses):
                with self.assertRaises(TimeoutError): GATE.inject_case(lab, journal, 'replica-network')
            self.assertTrue(journal[GATE.KEY]['rules_absent_before'])
            record = Path(d)/GATE.RECORD
            record.write_text(json.dumps(journal))
            calls = []
            original = Mock(side_effect=lambda *a, **k: calls.append('business'))
            def restore(*a):
                calls.append('restore')
                proof['transport_restored'] = True
            with patch.object(GATE, 'restore_transport', side_effect=restore), patch.object(GATE, 'recovered', return_value=False):
                # A wait returning without proof must never authorize deletion.
                with self.assertRaises(ValueError): GATE.cleanup(lab, original)
            original.assert_not_called()
            self.assertEqual(['restore'], calls)
            self.assertTrue(record.exists())

    def test_missing_transport_absence_proof_never_adopts_or_deletes_a_rule(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                proof = GATE.boundary(lab, journal, 'replica-network')
            journal[GATE.KEY] = proof
            (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
            original = Mock()
            with patch.object(GATE, 'host_call') as call:
                with self.assertRaises(ValueError): GATE.cleanup(lab, original)
            call.assert_not_called(); original.assert_not_called()

    def test_recovery_requires_new_target_original_parent_host_and_three_replicas(self):
        for defect in ('none', 'old-process', 'missing-process', 'reboot', 'machine', 'parent', 'unhealthy'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, objects, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    proof = GATE.boundary(lab, journal, 'remote-spdk')
                current = dict(host, process={'pid': 8, 'start': '4'})
                if defect == 'old-process': current = host
                if defect == 'missing-process': current['process'] = None
                if defect == 'reboot': current['boot_id'] = 'different'
                if defect == 'machine': current['machine_id'] = 'foreign'
                if defect == 'parent': objects['instancemanagers.longhorn.io']['metadata']['uid'] = 'foreign'
                if defect == 'unhealthy': lab.healthy_replicas.return_value = False
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=current):
                    if defect in ('reboot', 'machine', 'parent'):
                        with self.assertRaises(ValueError): GATE.component_restored(lab, journal, proof)
                    else:
                        self.assertEqual(defect == 'none', bool(GATE.component_restored(lab, journal, proof)))

    def test_worker_reboot_requires_actual_changed_boot_id_not_ssh_success(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, _, _, loader, host = self.fixture(Path(d))
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                proof = GATE.boundary(lab, journal, 'worker-reboot')
                self.assertFalse(GATE.component_restored(lab, journal, proof))
            with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=dict(host, boot_id='new')):
                self.assertTrue(GATE.component_restored(lab, journal, proof))

    def test_reboot_arms_after_durable_intent_before_mutation_and_never_replays(self):
        for arm_fails in (False, True):
            with self.subTest(arm_fails=arm_fails), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, _, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=host):
                    proof = GATE.boundary(lab, journal, 'worker-reboot')
                calls = []
                lab.write_private.side_effect = lambda *a: calls.append('intent')
                boot = loader('longhorn-v2-boot-fixture.py')
                def arm(*args):
                    self.assertEqual(proof, journal[GATE.KEY])
                    calls.append('arm')
                    if arm_fails: raise ValueError('fixture admission failed')
                boot.arm.side_effect = arm
                loader('longhorn-v2-reboot-relay.py').pause.side_effect = lambda *args: calls.append('pause')
                def reboot(*args, **kwargs):
                    calls.append('reboot')
                    raise TimeoutError('uncertain reboot response')
                with patch.object(GATE, 'boundary', return_value=proof), patch.object(GATE, 'load', side_effect=loader), \
                        patch.object(GATE, 'host_call', side_effect=reboot) as mutate:
                    with self.assertRaises(ValueError if arm_fails else TimeoutError):
                        GATE.inject_case(lab, journal, 'worker-reboot', relay_pid=23)
                    with self.assertRaises(ValueError): GATE.inject_case(lab, journal, 'worker-reboot', relay_pid=23)
                    self.assertEqual(0 if arm_fails else 1, mutate.call_count)
                self.assertEqual(['intent', 'arm']+([] if arm_fails else ['pause', 'reboot']), calls)

    def test_new_boot_is_observed_before_fixture_then_native_recovery(self):
        for stage in ('outage', 'old-boot', 'kubelet-starting', 'fixture-pending', 'ready', 'fixture-failed', 'completed-inactive'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, _, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=host):
                    proof = GATE.boundary(lab, journal, 'worker-reboot')
                calls = []
                boot, resources = loader('longhorn-v2-boot-fixture.py'), loader('longhorn-v2-resources.py')
                current = None if stage == 'outage' else dict(host, boot_id='old-boot' if stage == 'old-boot' else 'new-boot')
                def observe(*a, **kw): calls.append('host'); return current
                def pages(*a): calls.append('resources'); return {'kubelet_active': 'inactive' if stage in ('kubelet-starting','completed-inactive') else 'active'}
                def complete(*a):
                    calls.append('fixture')
                    if stage == 'fixture-failed': raise ValueError('receipt changed')
                    return stage != 'fixture-pending'
                resources.probe.side_effect = pages; boot.complete.side_effect = complete
                loader('longhorn-v2-reboot-relay.py').resume.side_effect=lambda *args:calls.append('relay-resume')
                if stage == 'completed-inactive':
                    boot.check_inactive_kubelet.side_effect=ValueError('inactive after completed boot fixture')
                original_get = lab.get.side_effect
                lab.get.side_effect = lambda *a, **kw: (calls.append('native') or original_get(*a, **kw))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=observe):
                    if stage in ('fixture-failed','completed-inactive'):
                        with self.assertRaises(ValueError): GATE.component_restored(lab, journal, proof)
                    else:
                        self.assertEqual(stage == 'ready', bool(GATE.component_restored(lab, journal, proof)))
                if stage == 'ready': self.assertEqual(['host', 'resources', 'fixture', 'relay-resume', 'native', 'native'], calls)
                else: self.assertNotIn('native', calls)

    def test_reboot_fixture_cleanup_precedes_business_delete_and_failure_blocks_it(self):
        for fixture_fails in (False, True):
            with self.subTest(fixture_fails=fixture_fails), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, _, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', return_value=host):
                    proof = GATE.boundary(lab, journal, 'worker-reboot')
                proof['recovered'] = {'boot': 'new'}
                journal[GATE.KEY] = proof
                (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
                calls = []
                lab.installation_health.side_effect = lambda: calls.append('health')
                def clean(*a):
                    calls.append('fixture-clean')
                    if fixture_fails: raise ValueError('foreign boot artifact')
                loader('longhorn-v2-boot-fixture.py').clean.side_effect = clean
                loader('longhorn-v2-boot-fixture.py').recover_kubelet.side_effect=lambda *a:calls.append('kubelet-recover')
                original = Mock(side_effect=lambda *a, **kw: calls.append('business-delete'))
                with patch.object(GATE, 'load', side_effect=loader):
                    if fixture_fails:
                        with self.assertRaises(ValueError): GATE.cleanup(lab, original)
                        original.assert_not_called()
                    else: GATE.cleanup(lab, original)
                self.assertEqual(['kubelet-recover', 'health', 'fixture-clean']+([] if fixture_fails else ['business-delete']), calls)

    def test_log_verifier_checks_real_fsync_ledgers_and_specific_native_recovery(self):
        faults = GATE.load('longhorn-faults.py')
        control = GATE.load('longhorn-control-faults.py')
        import hashlib
        for variant in GATE.VARIANTS:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as d:
                lab, journal, runtime, _, _, loader, host = self.fixture(Path(d))
                runtime['replicas'] = {str(i): {'node': 'node-'+str(i), 'uid': 'r-'+str(i)} for i in range(3)}
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe', side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    proof = GATE.boundary(lab, journal, variant)
                native = 'engine' if variant == 'attached-spdk' else 'instance-manager'
                token = 'run:'+native
                def ledger(value):
                    rows = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(value, i)).hexdigest()}
                            for i in range(4)]
                    return control.confirmed(rows, value, faults)
                original, fresh = ledger(token), ledger(token+':recovered')
                fresh['path'] = '/data/control-recovered-ledger'
                original.update(client='recovered-writer' if native == 'engine' else 'writer', fresh=fresh)
                result = {'variant': native, 'recovery_mode': 'documented-bare-pod-recreation' if native == 'engine'
                    else 'native-component-recreation-original-client', 'recovery_s': 11.25,
                    'client_rc': 137 if native == 'engine' else 0, 'retries': [],
                    'confirmed': original['summary'], 'fresh_confirmed': fresh['summary']}
                current = copy.deepcopy(proof['original_host'])
                if variant.endswith('spdk'): current['process']['pid'] = 8
                if variant == 'worker-reboot': current['boot_id'] = 'new-boot'
                if variant == 'replica-network': proof.update(rules_absent_before=True, observed_replica_mode='ERR')
                recovery = {'case': variant, 'volume_uid': journal['volume_uid'], 'host': current, 'pod_uid': 'recovered',
                            'replicas': runtime['replicas'], 'transport_restored': variant == 'replica-network'}
                if variant == 'worker-reboot': proof['reboot_relay'] = {'pid': 91, 'phase': 'paused'}
                rows = [
                    'LONGHORN_V2_RUNTIME_FAULT_INJECTED '+json.dumps(proof),
                    'LONGHORN_V2_RUNTIME_FAULT_RECOVERED '+json.dumps(recovery),
                    'LONGHORN_CONTROL_FAULT_RESULT '+json.dumps(result),
                    'LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS '+json.dumps(original),
                    'LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED '+json.dumps({
                        'original': original['summary'], 'recovered': fresh['summary']}),
                    'LONGHORN_CONTROL_FAILURE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                    'LONGHORN_V2_RUNTIME_FAULT_RESTORED_BEFORE_CLEANUP',
                    'LONGHORN_V2_RUNTIME_FAULT_BUSINESS_PASS case='+variant,
                    'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes='+str(3*262144)+' sha256='+
                    hashlib.sha256(b'run'*262144).hexdigest()+' replica_nodes=node-0,node-1,node-2']
                log = '\n'.join(rows)
                if variant == 'worker-reboot':
                    log += '\nLONGHORN_V2_REBOOT_FIXTURE_ARMED node='+proof['address']+' id=owned-id'
                    log += '\nLONGHORN_V2_REBOOT_FIXTURE_RESTORED node='+proof['address']+' id=owned-id'
                    log += '\nLONGHORN_V2_REBOOT_FIXTURE_SCOPED_CLEAN_PASS'
                    log += '\nLONGHORN_V2_REBOOT_RELAY_PAUSED pid=91'
                    log += '\nLONGHORN_V2_REBOOT_RELAY_RESUMED pid=91 elapsed=11'
                self.assertEqual(result['confirmed'], GATE.verify_log(log, variant)['confirmed'])
                if variant == 'remote-spdk':
                    retry = {'type': 'retry', 'sequence': 4, 'errno': 'EROFS'}
                    resumed = copy.deepcopy(result)
                    resumed.update(recovery_mode='documented-read-only-bare-pod-recreation', client_rc=137,
                        retries=[retry], read_only_recovery={'reason': 'unexpected-detach-read-only',
                            'volume_uid': journal['volume_uid'], 'original_pod_uid': proof['client_uid'],
                            'replacement_pod_uid': 'replacement-uid', 'before_remount': proof['remount_requested_at'],
                            'after_remount': '2026-10-21T10:35:15Z', 'mount_read_only': True, 'last_retry': retry})
                    ledger_recreated = dict(original, client='recovered-writer')
                    recreated_log = log.replace(rows[2], 'LONGHORN_CONTROL_FAULT_RESULT '+json.dumps(resumed)).replace(
                        rows[3], 'LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS '+json.dumps(ledger_recreated))
                    self.assertEqual(result['confirmed'], GATE.verify_log(recreated_log, variant)['confirmed'])
                    for observed_readonly in (True, False):
                        observed = copy.deepcopy(resumed); observed['read_only_recovery']['mount_read_only'] = observed_readonly
                        self.assertEqual(result['confirmed'], GATE.verify_log(
                            recreated_log.replace(json.dumps(resumed), json.dumps(observed)), variant)['confirmed'])
                    for key, value in (('after_remount', ''), ('mount_read_only', None),
                                       ('original_pod_uid', 'foreign'), ('replacement_pod_uid', proof['client_uid']),
                                       ('volume_uid', 'foreign'), ('last_retry', {'type': 'retry', 'errno': 'EIO'})):
                        rejected = copy.deepcopy(resumed); rejected['read_only_recovery'][key] = value
                        with self.subTest(recovery_defect=key), self.assertRaises(ValueError):
                            GATE.verify_log(recreated_log.replace(json.dumps(resumed), json.dumps(rejected)), variant)
                bad = [log+'\nLONGHORN_V2_RUNTIME_FAULT_FAIL', log+'\n'+rows[0],
                       log.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', ''),
                       log.replace(original['records'][0]['sha256'], '0'*64),
                       log.replace('replica_nodes=node-0,node-1,node-2', 'replica_nodes=node-0,node-1,foreign')]
                changed = copy.deepcopy(recovery)
                changed['host'] = proof['original_host']
                if variant.endswith('spdk') or variant == 'worker-reboot':
                    bad.append(log.replace(rows[1], 'LONGHORN_V2_RUNTIME_FAULT_RECOVERED '+json.dumps(changed)))
                if variant == 'replica-network':
                    changed['transport_restored'] = False
                    bad.append(log.replace(rows[1], 'LONGHORN_V2_RUNTIME_FAULT_RECOVERED '+json.dumps(changed)))
                if variant == 'worker-reboot':
                    bad.extend((log.replace('LONGHORN_V2_REBOOT_RELAY_PAUSED pid=91', ''),
                                log.replace('REBOOT_RELAY_RESUMED pid=91', 'REBOOT_RELAY_RESUMED pid=92'),
                                log.replace('pid=91 elapsed=11', 'pid=91 elapsed=1201'),
                                log+'\nLONGHORN_V2_REBOOT_RELAY_RESUMED pid=91 elapsed=11',
                                log.replace('LONGHORN_V2_REBOOT_FIXTURE_SCOPED_CLEAN_PASS', ''),
                                log.replace('RESTORED node='+proof['address'], 'RESTORED node=foreign')))
                for rejected in bad:
                    with self.assertRaises(ValueError): GATE.verify_log(rejected, variant)

    def test_readonly_recovery_requires_real_ero_fs_native_remount_and_original_bare_client(self):
        for defect in ('none', 'rw-mount', 'volume', 'engine', 'remount', 'pod', 'owner', 'deleting', 'node', 'claim', 'mount'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, runtime, _, objects, _, _ = self.fixture(Path(directory))
                objects['volumes.longhorn.io']['status']['remountRequestedAt'] = '2026-10-21T10:35:15Z'
                objects['pod'] = {'metadata': {'uid': 'writer-uid'}, 'spec': {
                    'nodeName': runtime['client_node'], 'volumes': [{'persistentVolumeClaim': {'claimName': 'data'}}]}}
                proof = {'client_uid': 'writer-uid', 'runtime': runtime, 'remount_requested_at': ''}
                retry = {'type': 'retry', 'sequence': 4, 'errno': 'EROFS'}
                if defect == 'volume': objects['volumes.longhorn.io']['metadata']['uid'] = 'foreign'
                if defect == 'engine': objects['volumes.longhorn.io']['spec']['dataEngine'] = 'v1'
                if defect == 'remount': objects['volumes.longhorn.io']['status']['remountRequestedAt'] = ''
                if defect == 'pod': objects['pod']['metadata']['uid'] = 'foreign'
                if defect == 'owner': objects['pod']['metadata']['ownerReferences'] = [{'kind': 'Deployment'}]
                if defect == 'deleting': objects['pod']['metadata']['deletionTimestamp'] = 'now'
                if defect == 'node': objects['pod']['spec']['nodeName'] = 'foreign'
                if defect == 'claim': objects['pod']['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = 'foreign'
                lab.get.side_effect = lambda kind, *a, **kw: copy.deepcopy(objects[kind])
                lab.kubectl.return_value.stdout = json.dumps({'read_only': None if defect == 'mount' else defect != 'rw-mount'})
                if defect in ('none', 'rw-mount'):
                    result = GATE.read_only_recovery(lab, journal, proof, [retry])
                    self.assertEqual(result['volume_uid'], journal['volume_uid'])
                    self.assertEqual(result['original_pod_uid'], journal['objects']['Pod/writer'])
                    self.assertEqual(result['mount_read_only'], defect != 'rw-mount')
                    self.assertIn('os.statvfs("/data")', lab.kubectl.call_args.args[-1])
                else:
                    with self.assertRaises(ValueError): GATE.read_only_recovery(lab, journal, proof, [retry])
                    if defect != 'mount': lab.kubectl.assert_not_called()

    def test_nonreadonly_writer_keeps_original_client_recovery_without_mutation(self):
        for rows in ([], [{'type': 'ack'}], [{'type': 'retry', 'errno': 'EIO'}],
                     [{'type': 'retry', 'errno': 'EROFS'}, {'type': 'ack'}]):
            lab = Mock()
            self.assertIsNone(GATE.read_only_recovery(lab, {}, {}, rows))
            lab.check_business_graph.assert_not_called(); lab.get.assert_not_called(); lab.kubectl.assert_not_called()

    def test_expected_worker_ssh_outage_waits_but_host_script_failure_is_not_masked(self):
        with tempfile.TemporaryDirectory() as d:
            lab, *_ = self.fixture(Path(d))
            with patch.object(GATE, 'host_call', return_value=SimpleNamespace(returncode=255, stdout='')):
                self.assertIsNone(GATE.probe(lab, '192.168.47.147', unavailable=True))
            with patch.object(GATE, 'host_call', side_effect=TimeoutError('expected reboot')):
                self.assertIsNone(GATE.probe(lab, '192.168.47.147', unavailable=True))
                with self.assertRaises(TimeoutError): GATE.probe(lab, '192.168.47.147')
            with patch.object(GATE, 'host_call', return_value=SimpleNamespace(returncode=1, stdout='')):
                with self.assertRaises(ValueError): GATE.probe(lab, '192.168.47.147', unavailable=True)

    def test_all_cached_native_consumers_are_accounted_before_shared_fault(self):
        for defect in ('backing-image', 'extra-engine', 'unready', 'wrong-name'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, runtime, _, _, loader, host = self.fixture(Path(d))
                original = lab.get.side_effect
                def get(kind, *args, **kwargs):
                    row = original(kind, *args, **kwargs)
                    if kind == 'instancemanagers.longhorn.io' and args[0] == 'im0':
                        if defect == 'backing-image': row['status']['backingImages'] = {'foreign': {}}
                        if defect == 'extra-engine': row['status']['instanceEngines']['foreign'] = {}
                        if defect == 'unready': row['status']['instanceEngines']['engine']['status']['state'] = 'stopped'
                        if defect == 'wrong-name': row['status']['instanceEngines']['engine']['spec']['name'] = 'foreign'
                    return row
                lab.get.side_effect = get
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'host_call') as call:
                    with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'attached-spdk')
                call.assert_not_called()

    def test_foreign_recovery_intent_never_removes_transport_or_deletes_business(self):
        for defect in ('run', 'cluster', 'volume', 'machine', 'comment', 'port', 'destination',
                       'source-pod', 'destination-pod', 'source-manager', 'network-mode'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, _, _, _, loader, host = self.fixture(Path(d))
                with patch.object(GATE, 'load', side_effect=loader), patch.object(GATE, 'probe',
                        side_effect=lambda lab, ip, *a, **k: dict(host, machine_id='machine-'+ip.rsplit('.', 1)[1])):
                    proof = GATE.boundary(lab, journal, 'replica-network')
                proof['rules_absent_before'] = True
                if defect == 'run': proof['run_id'] = 'foreign'
                if defect == 'cluster': proof['cluster_uid'] = 'foreign'
                if defect == 'volume': proof['runtime']['volume_uid'] = 'foreign'
                if defect == 'machine': proof['original_host']['machine_id'] = 'foreign'
                if defect == 'comment': proof['comment'] = 'kubeauto-longhorn-v2-'+'0'*24
                if defect == 'port': proof['port'] = 22
                if defect == 'destination': proof['destination'] = '192.168.47.149'
                if defect == 'source-pod': proof['endpoints']['source']['pod_uid'] = 'replacement'
                if defect == 'destination-pod': proof['endpoints']['destination']['pod_uid'] = 'replacement'
                if defect == 'source-manager': proof['endpoints']['source']['manager'] = 'im2'
                if defect == 'network-mode': proof['host_network'] = True
                journal[GATE.KEY] = proof
                (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
                original = Mock()
                with patch.object(GATE, 'host_call') as call:
                    with self.assertRaises(ValueError): GATE.cleanup(lab, original)
                call.assert_not_called(); original.assert_not_called()

    def test_actual_transport_rules_use_forward_for_pods_and_exact_idempotent_recovery(self):
        import subprocess
        import sys
        for host_network in (False, True):
            with self.subTest(host_network=host_network), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base/'machine-id').write_text('machine')
                (base/'boot-id').write_text('boot')
                source = GATE.host_script().replace("Path('/etc/machine-id')", 'Path('+repr(str(base/'machine-id'))+')')
                source = source.replace("Path('/proc/sys/kernel/random/boot_id')", 'Path('+repr(str(base/'boot-id'))+')')
                request = {'action': 'rules', 'expected': {'machine_id': 'machine', 'boot_id': 'boot'},
                    'comment': 'kubeauto-longhorn-v2-'+'a'*24, 'source': '172.31.1.1',
                    'destination': '172.31.1.2', 'port': 20001, 'host_network': host_network}
                rules = set(); commands = []
                def iptables(command, **kwargs):
                    commands.append(command)
                    self.assertEqual(['iptables', '-w', '5', '-t', 'filter'], command[:5])
                    self.assertEqual(15, kwargs['timeout'])
                    op = command[5]; tail = command[6:]
                    if op == '-I':
                        self.assertEqual('1', tail[1])
                        tail = [tail[0]]+tail[2:]
                    key = tuple(tail)
                    if op == '-C': return SimpleNamespace(returncode=0 if key in rules else 1)
                    self.assertTrue(kwargs['check'])
                    if op == '-I': rules.add(key)
                    elif op == '-D': rules.remove(key)
                    else: self.fail('unexpected broad mutation '+op)
                    return SimpleNamespace(returncode=0)
                def run(operation, **changes):
                    row = dict(request, operation=operation, **changes); out = io.StringIO()
                    with patch.object(sys, 'stdin', io.StringIO(json.dumps(row))), redirect_stdout(out), \
                            patch.object(subprocess, 'run', side_effect=iptables):
                        exec(compile(source, '<isolated-transport>', 'exec'), {})
                    return json.loads(out.getvalue())
                expected = ['OUTPUT', 'INPUT'] if host_network else ['FORWARD', 'FORWARD']
                self.assertEqual([False, False], run('probe')['present'])
                self.assertEqual(expected, run('add')['chains'])
                self.assertEqual(2, len(rules))
                for rule in rules:
                    self.assertIn(rule[0], expected)
                    self.assertEqual('tcp', rule[rule.index('-p')+1])
                    self.assertIn('172.31.1.1/32', rule); self.assertIn('172.31.1.2/32', rule)
                    self.assertIn('20001', rule); self.assertIn(request['comment'], rule)
                before = set(rules)
                with self.assertRaisesRegex(ValueError, 'collision'): run('add')
                self.assertEqual(before, rules)
                # An interrupted add may leave one rule; restoration removes exactly that rule.
                rules.pop()
                self.assertEqual([False, False], run('remove')['present'])
                deletes = len([c for c in commands if c[5] == '-D'])
                self.assertEqual(1, deletes)
                self.assertEqual([False, False], run('remove')['present'])
                self.assertEqual(deletes, len([c for c in commands if c[5] == '-D']))
                before = len(commands)
                with self.assertRaisesRegex(ValueError, 'network mode'): run('add', host_network=None)
                self.assertEqual(before, len(commands))

    def test_actual_host_script_signals_only_original_pidfd_and_rejects_post_open_pid_reuse(self):
        import os
        import signal
        import sys
        for race in (False, True):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                proc = base/'proc'
                task = proc/'7'
                task.mkdir(parents=True)
                token = '12345678-1234-1234-1234-123456789abc'
                (task/'cgroup').write_text('0::/kubepods/pod'+token+'/container')
                binary = base/'spdk_tgt'
                binary.touch()
                (task/'exe').symlink_to(binary)
                (task/'stat').write_text('7 (spdk_tgt) '+' '.join(['S']+['0']*18+['123']))
                (task/'cmdline').write_bytes(b'spdk_tgt\x00--rpc-socket\x00/var/tmp/spdk.sock\x00')
                (base/'machine-id').write_text('machine')
                (base/'boot-id').write_text('boot')
                source = GATE.host_script().replace("Path('/proc')", 'Path('+repr(str(proc))+')')
                source = source.replace("Path('/etc/machine-id')", 'Path('+repr(str(base/'machine-id'))+')')
                source = source.replace("Path('/proc/sys/kernel/random/boot_id')", 'Path('+repr(str(base/'boot-id'))+')')
                def run(request):
                    out = io.StringIO()
                    with patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), redirect_stdout(out):
                        exec(compile(source, '<isolated-host>', 'exec'), {})
                    return json.loads(out.getvalue())
                original = run({'action': 'probe', 'pod_uid': token})
                self.assertEqual('123', original['process']['start'])
                def open_pid(pid):
                    self.assertEqual(7, pid)
                    if race:
                        (task/'stat').write_text('7 (spdk_tgt) '+' '.join(['S']+['0']*18+['456']))
                    return 77
                with patch.object(os, 'pidfd_open', side_effect=open_pid), patch.object(os, 'close') as close, \
                        patch.object(signal, 'pidfd_send_signal') as send:
                    request = {'action': 'kill', 'pod_uid': token, 'expected': original}
                    if race:
                        with self.assertRaises(ValueError): run(request)
                        send.assert_not_called()
                    else:
                        self.assertEqual('SIGKILL', run(request)['signal'])
                        send.assert_called_once_with(77, signal.SIGKILL)
                    close.assert_called_once_with(77)

    def test_host_mutations_are_pidfd_scoped_and_rules_are_exact_not_chain_flushes(self):
        source = GATE.host_script()
        compile(source, '<host-runtime-fault>', 'exec')
        for expression in ('os.pidfd_open', 'signal.pidfd_send_signal', "probe()!=before", "'/32'", "'--dport'", "'--sport'", "'-C'", "'-D'", "'-m','comment'"):
            self.assertIn(expression, source)
        for forbidden in ('pkill', "'--dports'", "'-F'", "'-X'", 'shell=True', 'wipefs', 'dd '):
            self.assertNotIn(forbidden, source)
        with tempfile.TemporaryDirectory() as d:
            lab, *_ = self.fixture(Path(d))
            with self.assertRaises(ValueError): GATE.host_call(lab, '192.168.47.129', {'action': 'host'})
            lab.remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
