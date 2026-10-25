"""All-replica native refusal, restore-tested RPO and fault recovery ownership."""
import copy
from contextlib import redirect_stdout
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_all_replicas', ROOT/'tests/helpers/longhorn-v2-all-replicas.py')
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class AllReplicaContracts(unittest.TestCase):
    def test_direct_read_program_reaches_file_io_without_mutating_warm_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'confirmed'
            payload = bytes(range(256))*32
            path.write_bytes(payload)
            self.assertEqual(payload, path.read_bytes())  # Deliberately warm cache.
            original_open = os.open
            def open_original(name, flags):
                self.assertEqual('/data/confirmed', name)
                self.assertEqual(os.O_DIRECT | os.O_NOATIME | os.O_NOFOLLOW,
                                 flags & (os.O_DIRECT | os.O_NOATIME | os.O_NOFOLLOW))
                self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                return original_open(path, flags)
            output = io.StringIO()
            with patch('os.open', side_effect=open_original), redirect_stdout(output):
                exec(GATE.source_read_program(), {})
            self.assertEqual({'attempts': [{'errno': 0, 'read_bytes': 4096}]*3}, json.loads(output.getvalue()))
            self.assertEqual(payload, path.read_bytes())

    def test_direct_read_program_preserves_actual_errno_and_closes_its_descriptor(self):
        for number in (errno.EIO, errno.EINVAL):
            lengths = []
            def fail_read(fd, buffers, offset):
                lengths.append(len(buffers[0]))
                raise OSError(number, 'read failed')
            with self.subTest(errno=number), patch('os.open', return_value=91), \
                    patch('os.preadv', side_effect=fail_read) as read, \
                    patch('os.close') as close, redirect_stdout(io.StringIO()) as output:
                exec(GATE.source_read_program(), {})
                self.assertEqual({'attempts': [{'errno': number, 'read_bytes': 0}]*3}, json.loads(output.getvalue()))
                self.assertEqual(0, read.call_args.args[2])
                self.assertEqual([4096]*3, lengths)
                close.assert_called_once_with(91)

    def test_direct_read_requires_original_writer_and_actual_storage_error(self):
        for defect in ('none', 'mixed', 'replaced', 'controller', 'not-running', 'healthy-cache', 'alignment', 'exec', 'timeout'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, proof, _, _, _ = self.fixture(Path(directory))
                journal.update(namespace='business', objects={'Pod/writer': 'original-writer'})
                pod = {'metadata': {'uid': 'original-writer'}, 'status': {'phase': 'Running'}}
                if defect == 'replaced': pod['metadata']['uid'] = 'foreign'
                if defect == 'controller': pod['metadata']['ownerReferences'] = [{'uid': 'foreign'}]
                if defect == 'not-running': pod['status']['phase'] = 'Failed'
                lab.get.return_value = pod; lab.get.side_effect = None
                result = {'errno': errno.EIO, 'read_bytes': 0}
                if defect == 'healthy-cache': result = {'errno': 0, 'read_bytes': 4096}
                if defect == 'alignment': result['errno'] = errno.EINVAL
                lab.kubectl = Mock(return_value=SimpleNamespace(returncode=1 if defect == 'exec' else 0,
                                                               stdout=json.dumps({'attempts': [result]*3})))
                if defect == 'mixed':
                    lab.kubectl.return_value.stdout = json.dumps({'attempts': [
                        {'errno': errno.EOPNOTSUPP, 'read_bytes': 0}, result, result]})
                if defect == 'timeout': lab.kubectl.side_effect = TimeoutError('bounded read')
                with redirect_stdout(io.StringIO()):
                    if defect in ('none', 'mixed'):
                        GATE.probe_source_io(lab, journal, proof)
                        self.assertEqual(errno.EIO, proof['source_io']['attempts'][-1]['errno'])
                        self.assertTrue(proof['source_io']['read_only'])
                        self.assertEqual(120, lab.kubectl.call_args.kwargs['timeout'])
                    else:
                        with self.assertRaises((ValueError, TimeoutError)):
                            GATE.probe_source_io(lab, journal, proof)
                        self.assertNotIn('source_io', proof)
                if defect in ('none', 'mixed', 'healthy-cache', 'alignment', 'exec'):
                    self.assertEqual({'returncode': lab.kubectl.return_value.returncode,
                                      'stdout': lab.kubectl.return_value.stdout}, proof['source_io_observed'])
                    self.assertTrue(any(call.args[1].get(GATE.KEY, {}).get('source_io_observed')
                                        for call in lab.write_private.call_args_list))
                else:
                    self.assertNotIn('source_io_observed', proof)
                lab.cleanup_business.assert_not_called()
                if defect in ('replaced', 'controller', 'not-running'): lab.kubectl.assert_not_called()

    def test_actual_mixed_errno_requires_two_later_storage_errors_and_native_refusal(self):
        rows, markers = self.observation()
        data = dict(rows)
        data['LONGHORN_V2_ALL_REPLICAS_SOURCE_IO']['attempts'][0]['errno'] = errno.EOPNOTSUPP
        log = ''.join(k+' '+json.dumps(v)+'\n'+('LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3\n' if k=='LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED' else '') for k,v in rows)+'\n'.join(markers)+'\n'
        self.assertEqual('restored', GATE.verify_log(log)['restored_volume_uid'])
        for errors in ((95,95,95), (5,95,5), (95,5,95), (95,0,5), (22,5,5), (95,5)):
            with self.subTest(errors=errors):
                observed = {'attempts': [{'errno': n, 'read_bytes': 0} for n in errors]}
                self.assertFalse(GATE.source_io_failed(observed))
        data['LONGHORN_V2_ALL_REPLICAS_REFUSED']['robustness'] = 'healthy'
        log = ''.join(k+' '+json.dumps(v)+'\n'+('LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3\n' if k=='LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED' else '') for k,v in rows)+'\n'.join(markers)+'\n'
        with self.assertRaises(ValueError): GATE.verify_log(log)

    def test_injection_reaches_source_io_only_after_all_disks_offline_and_before_native_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, proof, _, _, disk = self.fixture(Path(directory))
            order = []
            def host(current, item, operation):
                order.append(operation)
                return dict(item['host'], state='offline')
            disk.host_call.side_effect = host
            original_load = GATE.load
            def load(name): return disk if name == 'longhorn-v2-disk-faults.py' else original_load(name)
            def probe(current, owned, fault):
                self.assertTrue(all(item.get('offline') for item in fault['disks'].values()))
                order.append('direct-read')
            def wait(label, timeout, predicate):
                order.append('native-refusal')
                return predicate()
            lab.wait.side_effect = wait
            with patch.object(GATE, 'load', side_effect=load), \
                    patch.object(GATE, 'all_disks', return_value=proof['disks']), \
                    patch.object(GATE, 'probe_source_io', side_effect=probe), redirect_stdout(io.StringIO()):
                GATE.inject(lab, journal, self.backup_point(journal))
            self.assertEqual(['offline']*3+['direct-read', 'native-refusal'], order)

    def test_all_disk_collection_resolves_every_native_replica_to_exact_stable_leased_disk(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), tempfile.TemporaryDirectory() as d:
                base = Path(d)
                addresses = tuple('192.168.47.'+str(i) for i in range(146,149))
                raw = {a: {'path': '/dev/disk/by-id/wwn-'+a[-3:], 'identity': 'id'+a[-3:],
                           'machine_id': 'm'+a[-3:], 'size_bytes': 100*1024**3} for a in addresses}
                declarations = {a: [{'name': 'raw', 'path': r['path'], 'identity': r['identity'],
                                    'disk_driver': 'aio', 'storage_reserved': 1024**3}] for a,r in raw.items()}
                (base/'config.yml').write_text(yaml.safe_dump({'longhorn_block_disks': declarations}))
                replicas = [{'metadata': {'name': 'r'+a[-3:], 'uid': 'u'+a[-3:]},
                             'spec': {'nodeID': 'longhorn-node-'+a[-3:], 'diskID': 'disk'+a[-3:]}} for a in addresses]
                runtime = {'instances': {'replicas.longhorn.io': {r['metadata']['name']: {'uid': r['metadata']['uid']} for r in replicas}}}
                original = {'runtime': runtime, 'volume_uid': 'source-uid'}
                def get(kind, *args, **kwargs):
                    if kind == 'replicas.longhorn.io': return {'items': replicas}
                    if kind == 'node': return {'metadata': {'uid': 'node'+args[0][-3:]}}
                    if kind == 'nodes.longhorn.io':
                        suffix = args[0][-3:]
                        return {'spec': {'disks': {'raw': {'diskType': 'block', 'diskDriver': 'aio',
                            'path': '/dev/disk/by-id/wwn-'+suffix}}}, 'status': {'diskStatus': {'raw': {'diskUUID': 'disk'+suffix}}}}
                    raise AssertionError(kind)
                lab = SimpleNamespace(DIRECTORY=base, WORKERS=addresses, owner=lambda: {'raw_disks': raw}, get=get)
                if foreign: replicas[1]['spec']['diskID'] = 'foreign'
                disk = SimpleNamespace(boundary=Mock(return_value=original),
                    host_call=lambda lab,item,op: {'state': 'running', 'machine_id': raw[item['address']]['machine_id'],
                                                   'disk': {'size_bytes': raw[item['address']]['size_bytes']}})
                native = SimpleNamespace(address=lambda lab,node: '192.168.47.'+node[-3:])
                with patch.object(GATE, 'load', side_effect=lambda name: disk if name == 'longhorn-v2-disk-faults.py' else native):
                    if foreign:
                        with self.assertRaises(ValueError): GATE.all_disks(lab, {'volume_uid': 'source-uid'})
                    else:
                        result = GATE.all_disks(lab, {'volume_uid': 'source-uid'})
                        self.assertEqual(set(addresses), set(result))
                        self.assertEqual({'u146','u147','u148'}, {p['replica_uid'] for p in result.values()})

    def fixture(self, base):
        journal = {'volume': 'volume', 'volume_uid': 'source-uid'}
        runtime = {'instances': {'engines.longhorn.io': {'engine': {'uid': 'engine-uid'}}}}
        disks = {'192.168.47.'+str(i): {'runtime': runtime, 'replica': 'r'+str(i), 'replica_uid': 'ru'+str(i),
            'host': {'state': 'running', 'machine_id': 'm'+str(i)}, 'disk_uuid': 'disk'+str(i)} for i in range(146,149)}
        policy = {'metadata': {'uid': 'policy-uid'}, 'value': 'false'}
        replicas = [{'metadata': {'name': 'r'+str(i), 'uid': 'ru'+str(i)},
            'spec': {'volumeName': 'volume', 'failedAt': 'failed', 'healthyAt': 'healthy'}} for i in range(146,149)]
        engine = {'metadata': {'uid': 'engine-uid'}, 'spec': {'volumeName': 'volume', 'salvageRequested': False},
                  'status': {'currentState': 'error', 'salvageExecuted': False}}
        volume = {'metadata': {'uid': 'source-uid'}, 'status': {'robustness': 'faulted'}}
        objects = {'settings.longhorn.io': policy, 'replicas.longhorn.io': {'items': replicas},
                   'engines.longhorn.io': {'items': [engine]}, 'backups.longhorn.io': {'metadata': {'uid': 'backup-uid'}}}
        lab = SimpleNamespace(DIRECTORY=base, WORKERS=tuple(disks), get=Mock(side_effect=lambda kind, *a, **k: copy.deepcopy(objects[kind])),
            owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster'}), write_private=Mock(),
            check_business_graph=Mock(), installation_health=Mock(), cleanup_business=Mock())
        lab.wait = Mock(side_effect=lambda label, timeout, fn: fn())
        disk = SimpleNamespace(volume=Mock(return_value=volume), validate=Mock(), host_call=Mock())
        proof = {'run_id': 'run', 'cluster_uid': 'cluster', 'volume_uid': 'source-uid',
                 'disks': disks, 'policy': {'uid': 'policy-uid', 'value': 'false'}}
        return lab, journal, proof, objects, volume, disk

    def checkpoint(self):
        faults, control = GATE.load('longhorn-faults.py'), GATE.load('longhorn-control-faults.py')
        token = 'run:all-replicas'
        records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()} for i in range(4)]
        return dict(control.confirmed(records, token, faults), path='/data/fault-ledger', retries=[])

    def backup_point(self, journal):
        receipt = {'volume': journal['volume'], 'backup': 'backup', 'objects': {'config': {'sha256': 'a'*64}}}
        journal['backups'] = {'backup': {'object_receipt': receipt}}
        return {'source_volume_uid': journal['volume_uid'], 'backup': 'backup', 'backup_uid': 'backup-uid',
            'object_receipt_sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
            'checkpoint': self.checkpoint(), 'preflight_restore_verified': True, 'preflight_volume_uid': 'preflight-uid'}

    def test_refusal_requires_original_faulted_volume_three_original_replicas_and_no_salvage(self):
        for defect in ('none', 'healthy', 'never-healthy', 'replica-uid', 'missing-replica', 'engine-uid', 'request', 'executed', 'policy', 'policy-uid'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, proof, objects, volume, disk = self.fixture(Path(d))
                if defect == 'healthy': volume['status']['robustness'] = 'healthy'
                if defect == 'never-healthy': objects['replicas.longhorn.io']['items'][0]['spec']['healthyAt'] = ''
                if defect == 'replica-uid': objects['replicas.longhorn.io']['items'][0]['metadata']['uid'] = 'foreign'
                if defect == 'missing-replica': objects['replicas.longhorn.io']['items'].pop()
                if defect == 'engine-uid': objects['engines.longhorn.io']['items'][0]['metadata']['uid'] = 'replacement'
                if defect == 'request': objects['engines.longhorn.io']['items'][0]['spec']['salvageRequested'] = True
                if defect == 'executed': objects['engines.longhorn.io']['items'][0]['status']['salvageExecuted'] = True
                if defect == 'policy': objects['settings.longhorn.io']['value'] = 'true'
                if defect == 'policy-uid': objects['settings.longhorn.io']['metadata']['uid'] = 'replacement'
                with patch.object(GATE, 'load', return_value=disk):
                    if defect == 'none': self.assertEqual('faulted', GATE.faulted(lab, journal, proof)['robustness'])
                    elif defect in ('healthy', 'never-healthy'): self.assertFalse(GATE.faulted(lab, journal, proof))
                    else:
                        with self.assertRaises(ValueError): GATE.faulted(lab, journal, proof)
                lab.cleanup_business.assert_not_called()

    def test_foreign_restore_test_backup_objects_or_corrupt_ack_block_all_disk_mutations(self):
        for defect in ('no-preflight', 'source', 'preflight-source', 'backup-uid', 'receipt', 'checkpoint'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                lab, journal, proof, objects, _, disk = self.fixture(Path(d))
                point = self.backup_point(journal)
                if defect == 'no-preflight': point['preflight_restore_verified'] = False
                if defect == 'source': point['source_volume_uid'] = 'foreign'
                if defect == 'preflight-source': point['preflight_volume_uid'] = journal['volume_uid']
                if defect == 'backup-uid': objects['backups.longhorn.io']['metadata']['uid'] = 'replacement'
                if defect == 'receipt': point['object_receipt_sha256'] = 'b'*64
                if defect == 'checkpoint': point['checkpoint']['records'][0]['sha256'] = 'b'*64
                with patch.object(GATE, 'all_disks') as boundary:
                    with self.assertRaises(ValueError): GATE.inject(lab, journal, point)
                boundary.assert_not_called()
                disk.host_call.assert_not_called()
                self.assertNotIn(GATE.KEY, journal)

    def test_engine_outage_refusal_does_not_invent_failed_replica_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, proof, objects, volume, disk = self.fixture(Path(directory))
            for replica in objects['replicas.longhorn.io']['items']:
                replica['spec']['failedAt'] = ''
            with patch.object(GATE, 'load', return_value=disk):
                result = GATE.faulted(lab, journal, proof)
            self.assertEqual('faulted', result['robustness'])
            self.assertTrue(all(r['failedAt'] == '' for r in result['replicas'].values()))
            objects['engines.longhorn.io']['items'][0]['status']['currentState'] = 'running'
            with patch.object(GATE, 'load', return_value=disk):
                self.assertFalse(GATE.faulted(lab, journal, proof))

    def test_source_fence_requires_absent_original_consumer_native_detach_and_no_salvage(self):
        for defect in ('none','pod','volume-attached','node','engine-node','engine-running','desire-running',
                       'engine-uid','requested-salvage','executed-salvage','policy'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, proof, objects, volume, disk = self.fixture(Path(directory))
                journal.update(namespace='business', objects={'Pod/writer':'writer-uid'})
                proof['refusal'] = {'engine_uid':'engine-uid'}
                volume['status'].update(state='detached', currentNodeID='', currentEngineNodeID='')
                engine = objects['engines.longhorn.io']['items'][0]
                engine['status']['currentState'] = 'stopped';engine['spec']['desireState'] = 'stopped'
                if defect == 'volume-attached': volume['status']['state'] = 'attached'
                if defect == 'node': volume['status']['currentNodeID'] = 'node'
                if defect == 'engine-node': volume['status']['currentEngineNodeID'] = 'node'
                if defect == 'engine-running': engine['status']['currentState'] = 'running'
                if defect == 'desire-running': engine['spec']['desireState'] = 'running'
                if defect == 'engine-uid': engine['metadata']['uid'] = 'foreign'
                if defect == 'requested-salvage': engine['spec']['salvageRequested'] = True
                if defect == 'executed-salvage': engine['status']['salvageExecuted'] = True
                if defect == 'policy': objects['settings.longhorn.io']['value'] = 'true'
                lab.get.side_effect = lambda kind,*a,**kw: ({'metadata':{'uid':'writer-uid'}} if defect=='pod' else None) if kind=='pod' else copy.deepcopy(objects[kind])
                with patch.object(GATE,'load',return_value=disk):
                    if defect in ('engine-uid','requested-salvage','executed-salvage','policy'):
                        with self.assertRaises(ValueError): GATE.source_fenced(lab,journal,proof)
                    elif defect == 'none':
                        result = GATE.source_fenced(lab,journal,proof)
                        self.assertTrue(result['writer_absent'])
                        self.assertEqual('writer-uid',result['writer_uid'])
                        self.assertEqual('detached',result['volume_state'])
                    else: self.assertFalse(GATE.source_fenced(lab,journal,proof))
                lab.cleanup_business.assert_not_called()

    def test_fencing_allows_native_faulted_to_unknown_transition_without_salvage(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, proof, objects, volume, disk = self.fixture(Path(directory))
            journal.update(namespace='business', objects={'Pod/writer': 'writer-uid'})
            pod = {'metadata': {'uid': 'writer-uid'}}
            proof['refusal'] = {'engine_uid': 'engine-uid'}
            volume['status'].update(robustness='unknown', state='detached', currentNodeID='', currentEngineNodeID='')
            engine = objects['engines.longhorn.io']['items'][0]
            engine['spec']['desireState'] = 'stopped'
            engine['status']['currentState'] = 'stopped'
            consumer = [pod]
            lab.get.side_effect = lambda kind,*a,**kw: consumer[0] if kind=='pod' else copy.deepcopy(objects[kind])
            lab.remove = Mock(side_effect=lambda obj: consumer.__setitem__(0, None))
            with patch.object(GATE, 'load', return_value=disk), patch.object(GATE, 'faulted') as refusal, redirect_stdout(io.StringIO()):
                result = GATE.fence_source(lab, journal, proof, pod)
            refusal.assert_not_called()
            lab.remove.assert_called_once_with(pod)
            self.assertEqual(result, proof['source_fenced'])
            self.assertEqual('detached', result['volume_state'])
            self.assertTrue(result['writer_absent'])
            self.assertFalse(result['salvageRequested'])
            self.assertEqual(['all-replica-original-writer-fenced','all-replica-source-native-detached'],
                             [call.args[0] for call in lab.wait.call_args_list])

    def test_scenario_handoff_fences_original_source_before_restoring_hardware_or_new_consumer(self):
        import ast, inspect, textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(GATE.scenario)))
        calls = [(node.lineno, getattr(node.func,'id',getattr(node.func,'attr','')),node)
                 for node in ast.walk(tree) if isinstance(node,ast.Call)]
        def first(name): return min(line for line,function,node in calls if function==name)
        self.assertLess(first('inject'),first('fence_source'))
        self.assertLess(first('fence_source'),first('restore_disks'))
        restores = [line for line,function,node in calls if function=='restore']
        self.assertEqual(2,len(restores))
        self.assertLess(first('restore_disks'),max(restores))
        self.assertLess(max(restores),max(line for line,function,node in calls if function=='source_fenced'))

    def test_fault_journals_each_original_disk_before_unknown_offline_and_does_not_replay(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _, disk = self.fixture(Path(d))
            point = self.backup_point(journal)
            calls = []
            original_load = GATE.load
            def load(name): return disk if name == 'longhorn-v2-disk-faults.py' else original_load(name)
            def unknown(current, item, operation):
                self.assertTrue(item['offline_intent'])
                self.assertIn(GATE.KEY, journal)
                calls.append(operation)
                raise TimeoutError('lost response')
            disk.host_call.side_effect = unknown
            with patch.object(GATE, 'load', side_effect=load), patch.object(GATE, 'all_disks', return_value=proof['disks']):
                with self.assertRaises(TimeoutError): GATE.inject(lab, journal, point)
                with self.assertRaises(ValueError): GATE.inject(lab, journal, point)
            self.assertEqual(['offline'], calls)
            self.assertTrue(next(iter(journal[GATE.KEY]['disks'].values()))['offline_intent'])

    def test_restore_all_original_disks_without_creating_single_disk_child_journal(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _, disk = self.fixture(Path(d))
            for item in proof['disks'].values(): item['offline_intent'] = True
            disk.host_call.side_effect = lambda current, item, operation: item['host']
            with patch.object(GATE, 'load', return_value=disk), redirect_stdout(io.StringIO()):
                GATE.restore_disks(lab, journal, proof)
            self.assertTrue(proof['disks_restored'])
            self.assertEqual(3, disk.validate.call_count)
            self.assertEqual(['restore']*3, [c.args[-1] for c in disk.host_call.call_args_list])
            self.assertNotIn('v2_disk_fault', journal)
            self.assertIs(proof, journal[GATE.KEY])

    def test_unknown_disk_restoration_keeps_source_and_backups(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _, disk = self.fixture(Path(d))
            for item in proof['disks'].values(): item['offline_intent'] = True
            journal[GATE.KEY] = proof
            record = Path(d)/GATE.RECORD
            record.write_text(json.dumps(journal))
            disk.host_call.return_value = {'state': 'running', 'machine_id': 'foreign'}
            with patch.object(GATE, 'load', return_value=disk):
                with self.assertRaises(ValueError): GATE.cleanup(lab, relay_pid=3)
            lab.cleanup_business.assert_not_called()
            self.assertTrue(record.exists())

    def test_cleanup_restores_fault_before_shared_health_and_native_business_delete(self):
        with tempfile.TemporaryDirectory() as d:
            lab, journal, proof, _, _, _ = self.fixture(Path(d))
            journal[GATE.KEY] = proof
            (Path(d)/GATE.RECORD).write_text(json.dumps(journal))
            order = []
            lab.installation_health.side_effect = lambda: order.append('health')
            lab.cleanup_business.side_effect = lambda *a, **k: order.append('business-delete')
            with patch.object(GATE, 'restore_disks', side_effect=lambda *a: order.append('disks')), redirect_stdout(io.StringIO()):
                GATE.cleanup(lab, relay_pid=9)
            self.assertEqual(['disks', 'health', 'business-delete'], order)

    def test_retiring_preflight_uses_csi_delete_and_never_wipes_or_direct_deletes_volume(self):
        lab = SimpleNamespace(get=Mock(return_value=None), remove=Mock(), wait=Mock(side_effect=lambda a,b,f:f()))
        journal = {'namespace': 'business', 'objects': {'Pod/preflight': 'pod', 'PersistentVolumeClaim/preflight': 'claim'},
            'claims': {'preflight': {'pv': 'pv', 'volume': 'vol'}}}
        with redirect_stdout(io.StringIO()): GATE.retire_preflight(lab, journal, 'preflight')
        lab.remove.assert_not_called()
        self.assertTrue(any(c.args == ('volumes.longhorn.io', 'vol', GATE.NS) for c in lab.get.call_args_list))

    def observation(self):
        checkpoint = self.checkpoint()
        faults, control = GATE.load('longhorn-faults.py'), GATE.load('longhorn-control-faults.py')
        token = checkpoint['token']+':recovered'
        records = [{'type': 'ack', 'sequence': i, 'sha256': hashlib.sha256(faults.ledger_block(token, i)).hexdigest()} for i in range(4)]
        fresh = dict(control.confirmed(records, token, faults), path='/data/all-replicas-recovered-ledger', retries=[])
        replicas = {'r'+str(i): {'uid': 'ru'+str(i), 'failedAt': 'failed', 'healthyAt': 'healthy'} for i in range(146,149)}
        refused = {'volume_uid': 'source', 'engine_uid': 'engine', 'engine_state': 'error', 'robustness': 'faulted',
                   'autoSalvage': False, 'salvageRequested': False, 'salvageExecuted': False, 'replicas': replicas}
        offline = {}
        for i in range(146,149):
            host = {'state': 'running', 'machine_id': 'm'+str(i), 'disk': {'identity': 'id'+str(i)}}
            offline['192.168.47.'+str(i)] = {'original': host, 'offline': dict(host, state='offline'),
                'replica_uid': 'ru'+str(i), 'disk_uuid': 'disk'+str(i)}
        fenced = {'volume_uid': 'source', 'engine_uid': refused['engine_uid'], 'writer_uid': 'writer', 'writer_absent': True,
            'volume_state': 'detached', 'engine_state': 'stopped', 'engine_desire': 'stopped',
            'current_node': '', 'current_engine_node': '', 'salvageRequested': False, 'salvageExecuted': False}
        outcome = {'source_volume_uid': 'source', 'restored_volume_uid': 'restored', 'backup': 'backup', 'backup_uid': 'backup-uid',
            'preflight_volume_uid': 'preflight', 'preflight_restore_verified': True, 'source_fenced': fenced,
            'cross_node': True, 'restore_node': 'node147', 'remount_node': 'node148', 'original': checkpoint, 'fresh': fresh,
            'payload_bytes': len(b'run')*262144, 'payload_sha256': hashlib.sha256(b'run'*262144).hexdigest(),
            'refusal': copy.deepcopy(refused)}
        backup = {'source_volume_uid': 'source', 'backup': 'backup', 'backup_uid': 'backup-uid', 'preflight_volume_uid': 'preflight',
                  'checkpoint': checkpoint['summary']}
        source_io = {'pod_uid': 'writer', 'volume_uid': 'source', 'path': '/data/confirmed', 'offset': 0,
                     'length': 4096, 'direct': True, 'read_only': True,
                     'attempts': [{'errno': errno.EIO, 'read_bytes': 0} for _ in range(3)]}
        rows = [('LONGHORN_V2_ALL_REPLICAS_BACKUP_RESTORE_TESTED', backup),
                ('LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE', offline),
                ('LONGHORN_V2_ALL_REPLICAS_SOURCE_IO', source_io), ('LONGHORN_V2_ALL_REPLICAS_REFUSED', refused),
                ('LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED', fenced), ('LONGHORN_V2_ALL_REPLICAS_RECOVERY', outcome)]
        markers = ['LONGHORN_V2_ALL_REPLICAS_PREFLIGHT_RESTORE_RETIRED',
            'LONGHORN_V2_ALL_REPLICAS_FAULT_RESTORED_BEFORE_CLEANUP', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
            'LONGHORN_BACKUP_STORE_CLEAN_PASS', 'LONGHORN_V2_ALL_REPLICAS_BUSINESS_PASS',
]
        return rows, markers

    def test_verifier_binds_real_native_refusal_three_disks_prior_backup_and_new_volume_readback(self):
        defects = ('none', 'healthy', 'salvage', 'old-source', 'same-node', 'old-backup', 'preflight-source',
                   'preflight-order', 'disk-count', 'not-offline', 'changed-disk', 'no-restore', 'changed-refusal',
                   'bad-fence', 'fence-order', 'no-io', 'cached-io', 'unsupported-io', 'foreign-io', 'io-order', 'ack', 'fresh', 'payload', 'cleanup', 'failure')
        for defect in defects:
            with self.subTest(defect=defect):
                rows, markers = self.observation()
                data = dict(rows)
                refusal = data['LONGHORN_V2_ALL_REPLICAS_REFUSED']
                outcome = data['LONGHORN_V2_ALL_REPLICAS_RECOVERY']
                offline = data['LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE']
                source_io = data['LONGHORN_V2_ALL_REPLICAS_SOURCE_IO']
                if defect == 'healthy': refusal['robustness'] = 'healthy'
                if defect == 'salvage': refusal['salvageRequested'] = True
                if defect == 'old-source': outcome['restored_volume_uid'] = outcome['source_volume_uid']
                if defect == 'same-node': outcome['restore_node'] = outcome['remount_node']
                if defect == 'old-backup': outcome['backup_uid'] = 'foreign'
                if defect == 'preflight-source': outcome['preflight_volume_uid'] = 'source'
                if defect == 'preflight-order': rows[0], rows[1] = rows[1], rows[0]
                if defect == 'disk-count': offline.pop('192.168.47.146')
                if defect == 'not-offline': offline['192.168.47.146']['offline']['state'] = 'running'
                if defect == 'changed-disk': offline['192.168.47.146']['offline']['machine_id'] = 'foreign'
                if defect == 'no-restore': outcome['preflight_restore_verified'] = False
                if defect == 'changed-refusal': outcome['refusal']['replicas']['r146']['uid'] = 'replacement'
                if defect == 'bad-fence': data['LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED']['writer_absent'] = False
                if defect == 'fence-order':
                    i = next(i for i,(k,v) in enumerate(rows) if k=='LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED')
                    rows[3],rows[i] = rows[i],rows[3]
                if defect == 'no-io': rows = [(k,v) for k,v in rows if k != 'LONGHORN_V2_ALL_REPLICAS_SOURCE_IO']
                if defect == 'cached-io': source_io['attempts'][0].update(errno=0, read_bytes=4096)
                if defect == 'unsupported-io': source_io['attempts'][0]['errno'] = errno.EINVAL
                if defect == 'foreign-io': source_io['volume_uid'] = 'foreign'
                if defect == 'io-order': rows[1], rows[2] = rows[2], rows[1]
                if defect == 'ack': outcome['original']['records'][0]['sha256'] = 'b'*64
                if defect == 'fresh': outcome['fresh']['token'] = 'foreign'
                if defect == 'payload': outcome['payload_sha256'] = 'b'*64
                if defect == 'cleanup': markers.remove('LONGHORN_BACKUP_STORE_CLEAN_PASS')
                if defect == 'failure': markers.append('LONGHORN_FIRST_FAILURE phase=restore rc=1')
                log = ''.join(k+' '+json.dumps(v)+'\n'+('LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3\n' if k=='LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED' else '') for k, v in rows)+'\n'.join(markers)+'\n'
                if defect == 'none': self.assertEqual('restored', GATE.verify_log(log)['restored_volume_uid'])
                else:
                    with self.assertRaises(ValueError): GATE.verify_log(log)

    def test_business_keeps_backup_store_until_scenario_fault_and_business_recovery_finish(self):
        order = []
        class Scope:
            def __init__(self, callback): self.callback = callback
            def __enter__(self): pass
            def __exit__(self, *a): self.callback()
        store = SimpleNamespace(prepare=lambda: order.append('prepare'), qualify=lambda: order.append('qualify'),
            product_configuration=lambda: {'configuration': 'private'}, cleanup=lambda: order.append('store-cleanup'))
        lab = SimpleNamespace(backup_store=lambda: store, scoped_cleanup=Scope)
        with patch.object(GATE, 'scenario', side_effect=lambda *a: order.append('scenario')):
            GATE.business(lab, 4)
        self.assertEqual(['prepare', 'qualify', 'scenario', 'store-cleanup'], order)


if __name__ == '__main__':
    unittest.main()
