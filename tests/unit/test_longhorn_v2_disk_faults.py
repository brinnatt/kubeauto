"""Native disk fencing, QoS product transactions and recovery-before-delete."""
import ast
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import inspect
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import sys
import stat
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_disk_faults', ROOT/'tests/helpers/longhorn-v2-disk-faults.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class DiskFaultContracts(unittest.TestCase):
    def fixture(self, base):
        declaration = {'name': 'raw', 'path': '/dev/disk/by-id/wwn-fixed', 'identity': 'fixed',
                       'disk_driver': 'aio', 'storage_reserved': 1024**3}
        (base/'config.yml').write_text(yaml.safe_dump({'longhorn_block_disks': {'192.168.47.147': [declaration]},
            'longhorn_requests': [], GATE.CONFIG: None, 'private_input': 'never-public'}))
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid', 'namespace': 'business', 'objects': {}}
        replicas = {str(i): {'uid': 'r'+str(i), 'node': 'node'+str(i)} for i in range(3)}
        runtime = {'volume_uid': 'volume-uid', 'replicas': replicas,
                   'instances': {'engines.longhorn.io': {'engine': {'uid': 'engine-uid'}}}}
        remote = {'parent': {'node': 'longhorn-node-147'}, 'replica': 'replica', 'replica_uid': 'r-uid'}
        objects = {
            'node': {'metadata': {'uid': 'node-uid'}},
            'nodes.longhorn.io': {'spec': {'disks': {'raw': {'path': declaration['path'], 'diskType': 'block', 'diskDriver': 'aio'}}},
                                 'status': {'diskStatus': {'raw': {'diskUUID': 'disk-uuid'}}}},
            'replicas.longhorn.io': {'metadata': {'uid': 'r-uid'}, 'spec': {'diskID': 'disk-uuid', 'failedAt': 'failure'}},
            'volumes.longhorn.io': {'metadata': {'uid': 'volume-uid', 'resourceVersion': '10'},
                                   'spec': {'dataEngine': 'v2', GATE.FIELD: 0}},
            'engines.longhorn.io': {'items': [{'metadata': {'uid': 'engine-uid'}, 'spec': {'volumeName': 'volume'},
                'status': {'replicaModeMap': {'replica': 'ERR'}, 'rebuildStatus': {'tcp://owned:20001': {
                    'isRebuilding': True, 'appliedRebuildingMBps': 2, 'progress': 10}}}}]},
            'settings.longhorn.io': {'metadata': {'uid': 'setting-uid'}, 'value': '{"v2":"0"}'},
        }
        raw = {'path': declaration['path'], 'identity': 'fixed', 'size_bytes': 100*1024**3, 'machine_id': 'machine'}
        lab = SimpleNamespace(DIRECTORY=base, WORKERS=['192.168.47.146', '192.168.47.147', '192.168.47.148'],
            owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster', 'raw_disks': {'192.168.47.147': raw}}),
            get=Mock(side_effect=lambda kind, *a, **k: copy.deepcopy(objects[kind])), remote=Mock(), kubectl=Mock(),
            write_private=Mock(), healthy_replicas=Mock(return_value=True), replica_topology=Mock(return_value=replicas),
            installation_health=Mock(), check_business_graph=Mock(), restore_request_input=Mock())
        lab.wait = Mock(side_effect=lambda label, timeout, check: check())
        manager = SimpleNamespace(runtime=Mock(return_value=runtime))
        common = SimpleNamespace(boundary=Mock(return_value=({}, remote)))
        native = SimpleNamespace(native_consumers=Mock())
        modules = {'longhorn-v2-manager-loss.py': manager, 'longhorn-v2-control-faults.py': common,
                   'longhorn-v2-runtime-faults.py': native}
        host = {'machine_id': 'machine', 'boot_id': 'boot', 'state': 'running', 'receipt_sha256': 'a'*64,
                'state_path': '/sys/devices/owned/state', 'disk': dict(raw, device_number='8:16')}
        with patch.object(GATE, 'load', side_effect=modules.__getitem__), patch.object(GATE, 'host_call', return_value=host):
            proof = GATE.boundary(lab, journal, 'single-disk')
        return lab, journal, proof, objects, modules

    def test_boundary_rejects_wrong_native_disk_raw_identity_or_foreign_consumer(self):
        for defect in ('path', 'identity', 'disk-type', 'driver', 'uuid', 'replica-uid', 'extra-disk', 'unready', 'foreign'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, proof, objects, modules = self.fixture(Path(d))
                disk = objects['nodes.longhorn.io']['spec']['disks']['raw']
                if defect == 'path': disk['path'] = '/dev/disk/by-id/other'
                if defect == 'identity': lab.owner.return_value['raw_disks']['192.168.47.147']['identity'] = 'other'
                if defect == 'disk-type': disk['diskType'] = 'filesystem'
                if defect == 'driver': disk['diskDriver'] = 'nvme'
                if defect == 'uuid': objects['nodes.longhorn.io']['status']['diskStatus']['raw']['diskUUID'] = 'other'
                if defect == 'replica-uid': objects['replicas.longhorn.io']['metadata']['uid'] = 'other'
                if defect == 'extra-disk': objects['nodes.longhorn.io']['spec']['disks']['foreign'] = disk.copy()
                if defect == 'unready': proof['host']['state'] = 'offline'
                if defect == 'foreign': modules['longhorn-v2-runtime-faults.py'].native_consumers.side_effect = ValueError('foreign')
                with patch.object(GATE, 'load', side_effect=modules.__getitem__), patch.object(GATE, 'host_call', return_value=proof['host']) as host:
                    with self.assertRaises(ValueError): GATE.boundary(lab, journal, 'single-disk')
                self.assertFalse(any(c.args[-1] != 'probe' for c in host.call_args_list))

    def test_offline_intent_is_durable_before_ambiguous_write_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _ = self.fixture(Path(d))
            order = []
            lab.write_private.side_effect = lambda *a: order.append('journal')
            def unknown(*a):
                self.assertTrue(journal[GATE.KEY]['offline_intent'])
                order.append('offline')
                raise TimeoutError('lost SSH response')
            with patch.object(GATE, 'boundary', return_value=proof), patch.object(GATE, 'host_call', side_effect=unknown):
                with self.assertRaises(TimeoutError): GATE.inject(lab, journal, 'single-disk', 9)
                with self.assertRaises(ValueError): GATE.inject(lab, journal, 'single-disk', 9)
            self.assertEqual(['journal', 'journal', 'offline'], order)

    def test_changed_second_boundary_prevents_any_state_write(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _ = self.fixture(Path(d))
            changed = dict(proof, node_uid='replacement')
            with patch.object(GATE, 'boundary', side_effect=[proof, changed]), patch.object(GATE, 'host_call') as call:
                with self.assertRaises(ValueError): GATE.inject(lab, journal, 'single-disk', 9)
            call.assert_not_called()
            self.assertNotIn(GATE.KEY, journal)

    def test_restore_binds_original_disk_machine_node_and_volume(self):
        for defect in ('run', 'cluster', 'volume', 'path', 'hardware', 'machine', 'node', 'current-volume'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, proof, objects, _ = self.fixture(Path(d))
                if defect == 'run': proof['run_id'] = 'other'
                if defect == 'cluster': proof['cluster_uid'] = 'other'
                if defect == 'volume': proof['volume_uid'] = 'other'
                if defect == 'path': proof['declaration']['path'] = '/dev/sda'
                if defect == 'hardware': proof['declaration']['identity'] = 'other'
                if defect == 'machine': proof['host']['machine_id'] = 'other'
                if defect == 'node': objects['node']['metadata']['uid'] = 'replacement'
                if defect == 'current-volume': objects['volumes.longhorn.io']['metadata']['uid'] = 'replacement'
                proof['offline_intent'] = True
                with patch.object(GATE, 'host_call') as call:
                    with self.assertRaises(ValueError): GATE.restore_disk(lab, journal, proof)
                call.assert_not_called()

    def test_ready_pod_or_missing_replica_mode_cannot_prove_disk_failure(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, objects, _ = self.fixture(Path(d))
            objects['replicas.longhorn.io']['spec']['failedAt'] = ''
            objects['engines.longhorn.io']['items'][0]['status']['replicaModeMap'] = {}
            self.assertFalse(GATE.degraded(lab, journal, proof))
            self.assertNotIn('degraded', proof)
            objects['engines.longhorn.io']['items'][0]['status']['replicaModeMap'] = {'replica': 'ERR'}
            self.assertTrue(GATE.degraded(lab, journal, proof))

    def test_global_qos_uses_real_product_input_and_preserves_other_engines(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                lab, journal, proof, objects, _ = self.fixture(base)
                proof['case'] = 'rebuild-qos'
                original = (base/'config.yml').read_text()
                proof['qos'] = {'setting_uid': 'setting-uid', 'setting_original': {'v2': '0'}, 'allowed_inputs': [original]}
                calls = []
                def setup(pid, requests):
                    calls.append((pid, requests))
                    config = yaml.safe_load((base/'config.yml').read_text())
                    self.assertEqual(3, config[GATE.CONFIG])
                    objects['settings.longhorn.io']['value'] = json.dumps({'v2': '3'} if not foreign else {'v2': '3', 'v1': '99'})
                lab.run_requests = Mock(side_effect=setup)
                if foreign:
                    with self.assertRaises(ValueError): GATE.apply_global(lab, journal, proof, 19, 3)
                else: GATE.apply_global(lab, journal, proof, 19, 3)
                self.assertEqual([(19, [])], calls)
                self.assertIn((base/'config.yml').read_text(), proof['qos']['allowed_inputs'])
                self.assertNotIn('never-public', json.dumps(GATE.public_proof(proof)))

    def test_foreign_config_or_setting_uid_blocks_product_write(self):
        for defect in ('config', 'setting'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                lab, journal, proof, objects, _ = self.fixture(base)
                original = (base/'config.yml').read_text()
                proof['qos'] = {'setting_uid': 'setting-uid', 'setting_original': {'v2': '0'}, 'allowed_inputs': [original]}
                if defect == 'config': (base/'config.yml').write_text('foreign: input\n')
                if defect == 'setting': objects['settings.longhorn.io']['metadata']['uid'] = 'replacement'
                lab.run_requests = Mock()
                if defect == 'setting':
                    with self.assertRaises(ValueError): GATE.setting(lab, proof)
                else:
                    with self.assertRaises(ValueError): GATE.apply_global(lab, journal, proof, 3, 1)
                lab.run_requests.assert_not_called()

    def test_volume_qos_patch_has_uid_and_resourceversion_preconditions(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, _, objects, _ = self.fixture(Path(d))
            def patched(*a): objects['volumes.longhorn.io']['spec'][GATE.FIELD] = 2
            lab.kubectl.side_effect = patched
            GATE.patch_override(lab, journal, 0, 2)
            patch_document = json.loads(lab.kubectl.call_args.args[-1])
            self.assertEqual([{'op': 'test', 'path': '/metadata/uid', 'value': 'volume-uid'},
                              {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '10'}], patch_document[:2])
            lab.kubectl.reset_mock()
            with self.assertRaises(ValueError): GATE.patch_override(lab, journal, 0, 3)
            lab.kubectl.assert_not_called()

    def test_active_qos_requires_actual_rebuilding_applied_value_and_original_engine(self):
        for defect in ('none', 'completed', 'old-field', 'wrong-value', 'error', 'engine'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, proof, objects, _ = self.fixture(Path(d))
                proof['qos'] = {'setting_uid': 'setting-uid', 'observations': []}
                proof['degraded'] = {'engine_uid': 'engine-uid'}
                engine = objects['engines.longhorn.io']['items'][0]
                entry = engine['status']['rebuildStatus']['tcp://owned:20001']
                if defect == 'completed': entry['isRebuilding'] = False
                if defect == 'old-field': entry['appliedRebuildingMbps'] = entry.pop('appliedRebuildingMBps')
                if defect == 'wrong-value': entry['appliedRebuildingMBps'] = 3
                if defect == 'error': entry['error'] = 'failed'
                if defect == 'engine': engine['metadata']['uid'] = 'replacement'
                with redirect_stdout(io.StringIO()):
                    if defect == 'none': self.assertTrue(GATE.active_qos(lab, journal, proof, 2))
                    else:
                        with self.assertRaises(ValueError): GATE.active_qos(lab, journal, proof, 2)
                self.assertEqual(1 if defect == 'none' else 0, len(proof['qos']['observations']))

    def test_qos_resolution_order_is_positive_zero_inheritance_and_live_global_update(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _ = self.fixture(Path(d))
            proof['qos'] = {'observations': []}
            calls = []
            with patch.object(GATE, 'active_qos', side_effect=lambda *a: calls.append(('observe', a[-1])) or True), \
                 patch.object(GATE, 'patch_override', side_effect=lambda *a: calls.append(('volume', *a[-2:]))), \
                 patch.object(GATE, 'apply_global', side_effect=lambda *a: calls.append(('product', a[-1]))):
                GATE.qualify_qos(lab, journal, proof, 4)
                GATE.qualify_qos(lab, journal, proof, 4)
            self.assertEqual([('observe', 2), ('volume', 2, 0), ('observe', 1), ('product', 3), ('observe', 3)], calls)
            self.assertTrue(proof['qos']['qualified'])

    def test_cleanup_restores_disk_then_settings_before_business_and_does_not_requalify(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _ = self.fixture(Path(d))
            journal[GATE.KEY] = proof
            (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
            calls = []
            def restore(*a): calls.append('disk')
            def recover(*a, **kw):
                self.assertFalse(kw['qualify'])
                calls.append('health')
                a[2]['recovered'] = True
                return True
            with patch.object(GATE, 'restore_disk', side_effect=restore), patch.object(GATE, 'recovered', side_effect=recover), \
                 patch.object(GATE, 'restore_qos', side_effect=lambda *a: calls.append('settings')), \
                 patch.object(GATE, 'host_call', return_value=proof['host']), redirect_stdout(io.StringIO()):
                GATE.cleanup(lab, lambda *a, **k: calls.append('business'), relay_pid=7)
            self.assertEqual(['disk', 'health', 'settings', 'business'], calls)

    def test_unproven_recovery_preserves_business_and_fault_journal(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _ = self.fixture(Path(d))
            journal[GATE.KEY] = proof
            record = Path(d)/GATE.RECORD
            record.write_text(json.dumps(journal))
            original = Mock()
            with patch.object(GATE, 'restore_disk'), patch.object(GATE, 'recovered', return_value=False), \
                 patch.object(GATE, 'restore_qos') as qos:
                with self.assertRaises(ValueError): GATE.cleanup(lab, original, relay_pid=7)
            original.assert_not_called()
            qos.assert_not_called()
            self.assertTrue(record.exists())

    def test_filler_is_exclusive_bounded_fsynced_and_independently_read_only(self):
        for read_only in (False, True):
            code = GATE.filler_program(read_only)
            ast.parse(code)
            self.assertIn('os.O_NOFOLLOW', code)
            self.assertIn(str(1024**3), code)
            self.assertNotIn('/dev/', code)
            self.assertIn('os.O_EXCL', code)
            self.assertIn('os.fsync(fd)', code)
            self.assertIn('st.st_size!=size', code)

    def test_detached_oom_cleanup_reattaches_only_the_original_owned_claim(self):
        for defect in ('none', 'writer-uid', 'controller', 'running', 'reason', 'foreign-pod', 'node', 'claim', 'replaced'):
            with self.subTest(defect=defect):
                journal = {'namespace': 'owned', 'objects': {'Pod/writer': 'old'}}
                proof = {'runtime': {'client_node': 'worker'}}
                writer = {'metadata': {'uid': 'old'}, 'status': {'phase': 'Failed', 'containerStatuses': [
                    {'state': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]}}
                if defect == 'writer-uid': writer['metadata']['uid'] = 'foreign'
                if defect == 'controller': writer['metadata']['ownerReferences'] = [{'uid': 'controller'}]
                if defect == 'running': writer['status']['phase'] = 'Running'
                if defect == 'reason': writer['status']['containerStatuses'][0]['state']['terminated']['reason'] = 'Error'
                pods = {'writer': writer}
                if defect == 'foreign-pod': pods['disk-fault-recovery'] = {'metadata': {'uid': 'foreign'}}
                def template(namespace, name, claim, node):
                    return {'metadata': {'name': name}, 'spec': {'nodeSelector': {'kubernetes.io/hostname': node},
                        'volumes': [{'persistentVolumeClaim': {'claimName': claim}}]}}
                def create(row, record, pod):
                    pod['metadata']['uid'] = 'recovery'
                    pod['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}
                    if defect == 'node': pod['spec']['nodeSelector']['kubernetes.io/hostname'] = 'foreign'
                    if defect == 'claim': pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName'] = 'foreign'
                    row['objects']['Pod/'+pod['metadata']['name']] = pod['metadata']['uid']
                    pods[pod['metadata']['name']] = pod
                    return pod
                def wait(stage, timeout, predicate):
                    if defect == 'replaced': pods['disk-fault-recovery']['metadata']['uid'] = 'replacement'
                    self.assertTrue(predicate())
                lab = SimpleNamespace(get=lambda kind, name, ns: pods.get(name), client_pod=Mock(side_effect=template),
                    create_business_object=Mock(side_effect=create), wait=wait)
                with patch.object(GATE, 'volume', return_value={'spec': {'nodeID': ''}, 'status': {'state': 'detached'}}), \
                     redirect_stdout(io.StringIO()):
                    if defect == 'none':
                        GATE.recover_oom_client(lab, journal, proof, Path('receipt'))
                        GATE.recover_oom_client(lab, journal, proof, Path('receipt'))
                        self.assertEqual(lab.create_business_object.call_count, 1)
                        self.assertNotIn('recovered', proof, 'reattaching does not prove replica or data recovery')
                    else:
                        with self.assertRaises(ValueError): GATE.recover_oom_client(lab, journal, proof, Path('receipt'))
                    if defect in ('writer-uid', 'controller', 'running', 'reason', 'foreign-pod'):
                        lab.create_business_object.assert_not_called()

    def test_filler_bounds_dirty_cache_and_reads_back_the_same_data(self):
        # A buffered write charges dirty pages even with tiny Python RSS.
        # Execute the actual program against a real file, enforcing a small
        # dirty-page budget; the former final-only fsync exceeds this budget.
        with tempfile.TemporaryDirectory() as directory, patch.object(GATE, 'FILL_BYTES', 64*1024**2):
            real_write, real_sync = os.pwrite, os.fsync
            dirty = 0
            peak = 0
            def write(fd, data, offset):
                nonlocal dirty, peak
                dirty += len(data)
                peak = max(peak, dirty)
                if dirty > 16*1024**2:
                    raise MemoryError('dirty cache exhausted the client budget')
                return real_write(fd, data, offset)
            def sync(fd):
                nonlocal dirty
                real_sync(fd)
                dirty = 0
            def discard(fd, offset, length, advice):
                self.assertEqual(dirty, 0, 'dirty data must be durable before discard')
                self.assertEqual(advice, os.POSIX_FADV_DONTNEED)
            def execute(read_only):
                output = io.StringIO()
                code = GATE.filler_program(read_only).replace("'/data", repr(directory)[:-1])
                with redirect_stdout(output):
                    exec(compile(code, '<actual-filler>', 'exec'), {})
                return json.loads(output.getvalue())
            with patch.object(os, 'pwrite', side_effect=write), patch.object(os, 'fsync', side_effect=sync), \
                 patch.object(os, 'posix_fadvise', side_effect=discard) as drop:
                written = execute(False)
                self.assertGreater(drop.call_count, 1)
                self.assertLessEqual(peak, 16*1024**2)
                self.assertEqual(written, execute(True))
                self.assertEqual(written['bytes'], 64*1024**2)

    def test_filler_stops_at_failed_flush_before_discard_or_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(GATE, 'FILL_BYTES', 8*1024**2):
            code = GATE.filler_program().replace("'/data", repr(directory)[:-1])
            output = io.StringIO()
            with patch.object(os, 'fsync', side_effect=OSError('flush failed')), \
                 patch.object(os, 'posix_fadvise') as drop, redirect_stdout(output):
                with self.assertRaisesRegex(OSError, 'flush failed'):
                    exec(compile(code, '<actual-filler>', 'exec'), {})
            drop.assert_not_called()
            self.assertEqual(output.getvalue(), '')

    def observations(self, qos=False):
        faults, control = GATE.load('longhorn-faults.py'), GATE.load('longhorn-control-faults.py')
        token = 'run:instance-manager'
        def confirmed(t):
            rows = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(t, i)).hexdigest()} for i in range(4)]
            return control.confirmed(rows, t, faults)
        ledger = confirmed(token)
        ledger.update(client='writer', fresh=confirmed(token+':recovered'))
        ledger['fresh']['path'] = '/data/control-recovered-ledger'
        host = {'state': 'running', 'machine_id': 'machine', 'disk': {'identity': 'fixed'}}
        replicas = {str(i): {'node': 'n'+str(i), 'uid': 'r'+str(i)} for i in range(3)}
        case = 'rebuild-qos' if qos else 'single-disk'
        proof = {'case': case, 'volume_uid': 'volume-uid', 'offline_intent': True, 'host': host,
            'offline': dict(host, state='offline'), 'disk_uuid': 'disk-uuid',
            'degraded': {'replica_mode': 'ERR', 'engine_uid': 'engine-uid'}, 'runtime': {'replicas': replicas}}
        restored = {'case': case, 'volume_uid': 'volume-uid', 'disk_uuid': 'disk-uuid', 'host': host, 'replicas': replicas}
        result = {'variant': 'instance-manager', 'client_rc': 0, 'recovery_s': 2, 'retries': [],
            'recovery_mode': 'native-component-recreation-original-client', 'confirmed': ledger['summary'], 'fresh_confirmed': ledger['fresh']['summary']}
        rows = [('LONGHORN_V2_DISK_FAULT_INJECTED', proof), ('LONGHORN_V2_DISK_FAULT_RECOVERED', restored),
                ('LONGHORN_CONTROL_FAULT_RESULT', result), ('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS', ledger),
                ('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED', {'original': ledger['summary'], 'recovered': ledger['fresh']['summary']})]
        markers = ['LONGHORN_CONTROL_FAILURE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
            'LONGHORN_V2_DISK_FAULT_RESTORED_BEFORE_CLEANUP', 'LONGHORN_V2_DISK_FAULT_BUSINESS_PASS case='+case,
            'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes='+str(3*262144)+' sha256='+hashlib.sha256(b'run'*262144).hexdigest()+' replica_nodes=n0,n1,n2']
        if qos:
            proof['filler'] = {'bytes': GATE.FILL_BYTES, 'sha256': 'a'*64}
            rows.append(('LONGHORN_V2_REBUILD_FILLER_REMOUNT_VERIFIED', copy.deepcopy(proof['filler'])))
            for limit, override, glob in ((2, 2, '1'), (1, 0, '1'), (3, 0, '3')):
                rows.append(('LONGHORN_V2_ACTIVE_REBUILD_QOS', {'isRebuilding': True, 'engine_uid': 'engine-uid',
                    'appliedRebuildingMBps': limit, 'volume_override': override, 'global': {'v2': glob}}))
            markers.append('LONGHORN_V2_REBUILD_QOS_RESTORED')
        return rows, markers, case

    def test_verifier_rejects_non_native_disk_health_corrupt_ack_and_incomplete_cleanup(self):
        for defect in ('none', 'mode', 'offline', 'identity', 'disk', 'replicas', 'ack', 'remount', 'fresh', 'cleanup', 'failure'):
            with self.subTest(defect=defect):
                rows, markers, case = self.observations()
                data = dict(rows)
                if defect == 'mode': data['LONGHORN_V2_DISK_FAULT_INJECTED']['degraded']['replica_mode'] = None
                if defect == 'offline': data['LONGHORN_V2_DISK_FAULT_INJECTED']['offline']['state'] = 'running'
                if defect == 'identity': data['LONGHORN_V2_DISK_FAULT_RECOVERED']['volume_uid'] = 'replacement'
                if defect == 'disk': data['LONGHORN_V2_DISK_FAULT_RECOVERED']['disk_uuid'] = 'replacement'
                if defect == 'replicas': data['LONGHORN_V2_DISK_FAULT_RECOVERED']['replicas'].pop('1')
                if defect == 'ack': data['LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS']['records'][0]['sha256'] = 'b'*64
                if defect == 'remount': data['LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED']['original'] = {}
                if defect == 'fresh': data['LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS']['fresh']['token'] = 'wrong'
                if defect == 'cleanup': markers.remove('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
                if defect == 'failure': markers.append('LONGHORN_FIRST_FAILURE phase=disk rc=1')
                log = ''.join(k+' '+json.dumps(v)+'\n' for k, v in rows)+'\n'.join(markers)+'\n'
                if defect == 'none': self.assertEqual(case, GATE.verify_log(log, case)['case'])
                else:
                    with self.assertRaises(ValueError): GATE.verify_log(log, case)

    def test_qos_verifier_rejects_inheritance_dynamic_or_filler_shortcuts(self):
        for defect in ('none', 'completed', 'applied', 'inheritance', 'dynamic', 'engine', 'filler', 'restore'):
            with self.subTest(defect=defect):
                rows, markers, case = self.observations(qos=True)
                observations = [v for k, v in rows if k == 'LONGHORN_V2_ACTIVE_REBUILD_QOS']
                if defect == 'completed': observations[0]['isRebuilding'] = False
                if defect == 'applied': observations[0]['appliedRebuildingMBps'] = 0
                if defect == 'inheritance': observations[1]['volume_override'] = 1
                if defect == 'dynamic': observations[2]['global']['v2'] = '1'
                if defect == 'engine': observations[2]['engine_uid'] = 'replacement'
                if defect == 'filler': dict(rows)['LONGHORN_V2_REBUILD_FILLER_REMOUNT_VERIFIED']['sha256'] = 'b'*64
                if defect == 'restore': markers.remove('LONGHORN_V2_REBUILD_QOS_RESTORED')
                log = ''.join(k+' '+json.dumps(v)+'\n' for k, v in rows)+'\n'.join(markers)+'\n'
                if defect == 'none': self.assertEqual(case, GATE.verify_log(log, case)['case'])
                else:
                    with self.assertRaises(ValueError): GATE.verify_log(log, case)

    def test_generated_host_code_only_exposes_bounded_probe_offline_restore(self):
        code = GATE.host_script()
        ast.parse(code)
        for unsafe in ('mkfs', 'dd ', '/device/delete', 'scsi_host', 'shell=True', 'pkill'):
            self.assertNotIn(unsafe, code)
        self.assertNotIn('/usr/local/kubeauto/roles/longhorn/files/block-check.py', code)
        self.assertIn('--no-act', code)
        embedded = next(node.value.args[0].args[0].value for node in ast.parse(code).body
                        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name) and node.value.func.id == 'exec')
        self.assertEqual((ROOT/'roles/longhorn/files/block-check.py').read_text(), embedded)
        self.assertIn('initial=False', code)
        self.assertIn("{'offline':'offline','restore':'running'}", code)
        self.assertIn("current!=expected", code)
        self.assertIn("if inspect()!=current", code)

    def host_execution(self, operation, defect=None):
        disk = {'path': '/dev/disk/by-id/wwn-fixed', 'identity': 'fixed', 'size_bytes': 100*1024**3,
                'device_number': '8:16', 'disk_driver': 'aio', 'wwn': 'fixed', 'serial': 'serial'}
        receipt = {'cluster_uid': 'cluster', 'node_uid': 'node-uid', 'disks': {'raw': disk}}
        if defect == 'receipt': receipt['disks']['foreign'] = disk.copy()
        if defect == 'cluster': receipt['cluster_uid'] = 'foreign'
        raw = json.dumps(receipt).encode()
        state = {'value': 'running', 'writes': [], 'inspections': 0}
        if defect == 'already-offline': state['value'] = 'offline'
        if defect == 'invalid-state': state['value'] = 'blocked'
        expected = {'machine_id': 'machine', 'boot_id': 'boot', 'receipt_sha256': hashlib.sha256(raw).hexdigest(),
                    'disk': disk, 'state_path': '/sys/devices/pci/scsi/state', 'state': 'running'}
        if defect == 'expected-boot': expected['boot_id'] = 'old-boot'
        request = {'operation': operation, 'cluster_uid': 'cluster', 'node_uid': 'node-uid', 'machine_id': 'machine',
                   'disk': dict(disk, name='raw', storage_reserved=1024**3), 'expected': expected}
        class FakePath:
            def __init__(self, path): self.path = str(path)
            def __truediv__(self, part): return FakePath(self.path+'/'+str(part))
            def __str__(self): return self.path
            def __eq__(self, other): return isinstance(other, FakePath) and self.path == other.path
            @property
            def name(self): return PurePosixPath(self.path).name
            def is_symlink(self): return defect == 'symlink'
            def resolve(self, strict=True):
                if self.path == disk['path']: return FakePath('/dev/sdb')
                if self.path == '/sys/class/block/sdb/device/state': return FakePath(expected['state_path'])
                return self
            def read_bytes(self):
                if self.path == '/etc/kubeauto/longhorn-block-disks.json': return raw
                raise AssertionError(self.path)
            def read_text(self):
                if self.path == '/etc/machine-id': return 'foreign' if defect == 'machine' else 'machine'
                if self.path == '/proc/sys/kernel/random/boot_id': return 'boot'
                if self.path == expected['state_path']: return state['value']
                if self.path == '/proc/self/mountinfo': return ''
                if self.path == '/proc/swaps': return 'Filename Type Size Used Priority\n'
                raise AssertionError(self.path)
            def stat(self): return SimpleNamespace(st_mode=stat.S_IFBLK|0o600, st_rdev=os.makedev(8,16))
            def exists(self): return False
            def glob(self, pattern): return []
            def iterdir(self): return iter(())
            def write_text(self, text):
                self_test.assertEqual(expected['state_path'], self.path)
                state['writes'].append(text)
                state['value'] = text.strip()
        def command(argv, **kw):
            self.assertEqual('lsblk', argv[0])
            state['inspections'] += 1
            observed = {'path':'/dev/sdb','type':'disk','size':disk['size_bytes'],'ro':0,
                        'wwn':'fixed','serial':'serial','maj:min':'8:16'}
            if defect == 'hardware': observed.update(wwn='foreign',serial='foreign')
            if defect == 'race' and state['inspections'] == 2: observed['maj:min'] = '8:32'
            return SimpleNamespace(returncode=0,stdout=json.dumps({'blockdevices':[observed]}))
        self_test = self
        # Generate on the control host, then execute with the actual product
        # inspector and only host filesystem/command fixtures. Do not mock the
        # module loader: doing so hid the missing worker repository.
        code = GATE.host_script()
        with patch('pathlib.Path', FakePath), patch('subprocess.run',side_effect=command), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), redirect_stdout(io.StringIO()) as output:
            try:
                exec(compile(code, '<bounded-host>', 'exec'), {})
            except ValueError:
                return state, None
        return state, json.loads(output.getvalue())

    def test_real_generated_host_refuses_each_identity_failure_without_a_sysfs_write(self):
        for defect in ('receipt', 'cluster', 'symlink', 'hardware', 'race', 'machine', 'expected-boot', 'already-offline', 'invalid-state'):
            with self.subTest(defect=defect):
                state, observed = self.host_execution('offline', defect)
                self.assertIsNone(observed)
                self.assertEqual([], state['writes'])

    def test_real_generated_host_offlines_once_and_restores_original_running_state(self):
        state, observed = self.host_execution('offline')
        self.assertEqual(['offline\n'], state['writes'])
        self.assertEqual('offline', observed['state'])
        state, observed = self.host_execution('restore', 'already-offline')
        self.assertEqual(['running\n'], state['writes'])
        self.assertEqual('running', observed['state'])
        state, observed = self.host_execution('restore')
        self.assertEqual([], state['writes'])
        self.assertEqual('running', observed['state'])

    def test_worker_without_repository_executes_real_checker_in_isolated_process(self):
        code = GATE.host_script()
        harness = '''import copy,hashlib,io,json,os,stat,sys,unittest
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout
GATE=SimpleNamespace(host_script=lambda: %r)
class IsolatedHost(unittest.TestCase):
''' % code
        harness += textwrap.indent(textwrap.dedent(inspect.getsource(self.host_execution)), '    ')
        harness += '''
test=IsolatedHost()
for operation,defect,expected in (('probe',None,[]),('offline',None,['offline\\n']),
    ('restore','already-offline',['running\\n']),('restore',None,[])):
    state,row=test.host_execution(operation,defect)
    test.assertEqual(expected,state['writes']);test.assertIsNotNone(row)
for defect in ('receipt','cluster','symlink','hardware','race','machine','expected-boot','already-offline','invalid-state'):
    state,row=test.host_execution('offline',defect)
    test.assertIsNone(row);test.assertEqual([],state['writes'])
print('LONGHORN_V2_DISK_HOST_ISOLATED_CONTRACT_PASS')
'''
        with tempfile.TemporaryDirectory() as empty_worker:
            result = subprocess.run([sys.executable,'-I','-c',harness],cwd=empty_worker,
                                    text=True,capture_output=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('LONGHORN_V2_DISK_HOST_ISOLATED_CONTRACT_PASS\n',result.stdout)


if __name__ == '__main__':
    unittest.main()
