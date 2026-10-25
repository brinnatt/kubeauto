"""Contracts for LONGHORN-49's independent graph, recovery and refusal gates."""
import copy
from contextlib import contextmanager, nullcontext, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('linked_lifecycle_contract',
    ROOT/'tests/helpers/longhorn-v2-linked-lifecycle.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class InterruptionContracts(unittest.TestCase):
    def test_real_stop_signals_unwind_product_and_business_scopes(self):
        spec = importlib.util.spec_from_file_location('linked_signal_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                events = []
                original = signal.signal(signum, MODULE.interrupted)
                try:
                    with self.assertRaises(SystemExit) as stopped:
                        with lab.scoped_cleanup(lambda: events.append('business-recovery')):
                            with lab.scoped_cleanup(lambda: events.append('product-stop')):
                                os.kill(os.getpid(), signum)
                    self.assertEqual(128 + signum, stopped.exception.code)
                    self.assertEqual(['product-stop', 'business-recovery'], events)
                finally:
                    signal.signal(signum, original)

    def test_signal_handlers_are_installed_before_loading_live_dependencies(self):
        import ast
        main = next(node for node in ast.parse(Path(MODULE.__file__).read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'main')
        self.assertEqual(['signal.signal(signal.SIGINT, interrupted)',
                          'signal.signal(signal.SIGTERM, interrupted)'],
                         [ast.unparse(node) for node in main.body[:2]])


class AncestryContracts(unittest.TestCase):
    def fixture(self):
        source = {'metadata': {'name': 'source', 'uid': 'source-uid'}, 'spec': {'dataEngine': 'v2'}}
        clone = {'metadata': {'name': 'clone', 'uid': 'clone-uid', 'labels': {MODULE.linked.LABEL: 'entry'}},
            'spec': {'dataEngine': 'v2', 'cloneMode': 'linked-clone'},
            'status': {'cloneStatus': {'sourceVolume': 'source', 'state': 'completed', 'snapshot': 'entry'}}}
        replicas = []
        for prefix, volume in (('s', 'source'), ('c', 'clone')):
            for index in range(2):
                spec = {'volumeName': volume, 'nodeID': 'node'+str(index), 'diskID': 'disk'+str(index),
                        'healthyAt': 'now', 'failedAt': ''}
                if prefix == 'c': spec['linkedCloneSrcReplicaName'] = 's'+str(index)
                replicas.append({'metadata': {'name': prefix+str(index), 'uid': prefix+str(index)+'-uid'},
                    'spec': spec, 'status': {'currentState': 'running'}})
        return source, clone, replicas

    def test_two_fault_domains_have_unique_healthy_same_disk_parents(self):
        result = MODULE.ancestry(*self.fixture())
        self.assertEqual(2, len(result)); self.assertEqual('s0-uid', result['c0']['parent_uid'])

    def test_broken_ancestry_and_unhealthy_parent_cannot_sign_rebuild(self):
        for defect in ('incomplete', 'label', 'disk', 'failed-parent', 'parent-stopped', 'duplicate-parent',
                       'parent-deleting', 'missing-child', 'extra-child', 'wrong-engine'):
            with self.subTest(defect=defect):
                source, child, replicas = self.fixture()
                if defect == 'incomplete': child['status']['cloneStatus']['state'] = 'initiated'
                if defect == 'label': child['metadata']['labels'].clear()
                if defect == 'disk': replicas[2]['spec']['diskID'] = 'foreign'
                if defect == 'failed-parent': replicas[0]['spec']['failedAt'] = 'now'
                if defect == 'parent-stopped': replicas[0]['status']['currentState'] = 'stopped'
                if defect == 'duplicate-parent': replicas[3]['spec']['linkedCloneSrcReplicaName'] = 's0'
                if defect == 'parent-deleting': replicas[0]['metadata']['deletionTimestamp'] = 'now'
                if defect == 'missing-child': replicas.pop()
                if defect == 'extra-child': replicas.append(copy.deepcopy(replicas[-1]))
                if defect == 'wrong-engine': source['spec']['dataEngine'] = 'v1'
                with self.assertRaises(ValueError): MODULE.ancestry(source, child, replicas)

    def test_drained_flag_requires_actual_replacement_on_third_node_for_each_volume(self):
        before = {name: {str(i): {'uid': name+str(i), 'node': 'node'+str(i)} for i in range(2)}
                  for name in ('source', 'clone', 'nested')}
        after = copy.deepcopy(before)
        for name in after: after[name]['0'] = {'uid': name+'2', 'node': 'node2'}
        MODULE.migrated_graph(before, after, 'node0')
        for defect in ('unchanged-nested', 'same-uid', 'same-node', 'both-replaced'):
            broken = copy.deepcopy(after)
            if defect == 'unchanged-nested': broken['nested'] = before['nested']
            if defect == 'same-uid': broken['clone']['0']['uid'] = before['clone']['0']['uid']
            if defect == 'same-node': broken['clone']['0']['node'] = 'node1'
            if defect == 'both-replaced': broken['clone']['1']['uid'] = 'also-replaced'
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                MODULE.migrated_graph(before, broken, 'node0')

    def test_backup_restore_must_have_no_linked_reference_or_source_uid(self):
        volume = {'metadata': {'uid': 'new'}, 'spec': {'dataEngine': 'v2', 'cloneMode': 'full-copy'}}
        MODULE.independent_restore(volume, {'old'})
        for defect in ('uid', 'mode', 'ancestry', 'label', 'engine'):
            obj = copy.deepcopy(volume)
            if defect == 'uid': obj['metadata']['uid'] = 'old'
            if defect == 'mode': obj['spec']['cloneMode'] = 'linked-clone'
            if defect == 'ancestry': obj['status'] = {'cloneStatus': {'sourceVolume': 'old'}}
            if defect == 'label': obj['metadata']['labels'] = {MODULE.linked.LABEL: 'entry'}
            if defect == 'engine': obj['spec']['dataEngine'] = 'v1'
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                MODULE.independent_restore(obj, {'old'})


class ProductConfigurationContracts(unittest.TestCase):
    def fixture(self, directory):
        lab = SimpleNamespace(DIRECTORY=Path(directory), yaml=yaml, owner=lambda: {'run_id': 'run'},
            supervise_core=Mock(), write_private=lambda path, data: path.write_text(json.dumps(data)))
        path = lab.DIRECTORY/'config.yml'
        path.write_text(yaml.safe_dump({'longhorn_requests': [], 'longhorn_storage_classes': [], 'longhorn_block_disks': {}}))
        def restore():
            receipt = lab.DIRECTORY/'longhorn-request-input.json'
            if receipt.exists():
                path.write_text(json.loads(receipt.read_text())['original']); receipt.unlink()
        @contextmanager
        def cleanup(action):
            try: yield
            finally: action()
        lab.restore_request_input = restore; lab.scoped_cleanup = cleanup
        return lab, MODULE.Probe(lab, 91)

    def test_config_runs_supervised_product_and_restores_input_on_success_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lab, probe = self.fixture(directory); path = lab.DIRECTORY/'config.yml'
                original = path.read_bytes(); observations = []
                def run(pid, step):
                    observations.append((pid, step, yaml.safe_load(path.read_text())))
                    if failure: raise RuntimeError('product failed')
                lab.supervise_core.side_effect = run
                if failure:
                    with self.assertRaises(RuntimeError): probe.config(longhorn_block_disks={'host': []})
                else: probe.config(longhorn_block_disks={'host': []})
                self.assertEqual((91, 'longhorn-setup'), observations[0][:2])
                self.assertEqual({'host': []}, observations[0][2]['longhorn_block_disks'])
                self.assertEqual(original, path.read_bytes())
                self.assertFalse((lab.DIRECTORY/'longhorn-request-input.json').exists())

    def test_config_rejects_foreign_input_receipt_or_unrelated_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            with self.assertRaises(ValueError): probe.config(longhorn_version='1.12.0')
            (lab.DIRECTORY/'longhorn-request-input.json').write_text('{}')
            with self.assertRaises(ValueError): probe.config(longhorn_block_disks={})
            lab.supervise_core.assert_not_called()

    def test_disk_restore_cancels_eviction_before_enabling_scheduling(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            original = {'host': [{'name': 'disk', 'allow_scheduling': True, 'eviction_requested': False}]}
            probe.journal = {'original_disks': copy.deepcopy(original), 'maintenance': {'host': 'host'}}
            probe.config = Mock(); probe.save = Mock(); probe.restore_disks()
            calls = probe.config.call_args_list
            self.assertEqual(2, len(calls))
            self.assertEqual(False, calls[0].kwargs['longhorn_block_disks']['host'][0]['allow_scheduling'])
            self.assertEqual(False, calls[0].kwargs['longhorn_block_disks']['host'][0]['eviction_requested'])
            self.assertEqual(original, calls[1].kwargs['longhorn_block_disks'])
            self.assertNotIn('maintenance', probe.journal)

    def test_failed_disk_restore_keeps_recovery_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, probe = self.fixture(directory)
            probe.journal = {'original_disks': {'host': [{'name': 'disk'}]}, 'maintenance': {'host': 'host'}}
            probe.config = Mock(side_effect=RuntimeError('failed')); probe.save = Mock()
            with self.assertRaises(RuntimeError): probe.restore_disks()
            self.assertIn('maintenance', probe.journal); probe.save.assert_not_called()


class LegacyContracts(unittest.TestCase):
    def fixture(self):
        volume = {'metadata': {'name': 'legacy', 'uid': 'old', 'resourceVersion': '3',
                    'labels': {MODULE.LEGACY_LABEL: 'true'}},
                  'spec': {'dataEngine': 'v2', 'cloneMode': 'linked-clone', 'size': str(2*1024**3),
                           'numberOfReplicas': 3, 'migrationNodeID': ''}}
        source = {'metadata': {'uid': 'source-uid'}}
        origin = {'created_version': '1.12.0', 'upgraded_version': '1.12.1', 'volume_name': 'legacy',
                  'volume_uid': 'old', 'source_volume': 'source', 'source_volume_uid': 'source-uid'}
        lab = SimpleNamespace(NODES=['192.168.47.146', '192.168.47.147', '192.168.47.148'],
            get=Mock(side_effect=lambda kind, name, namespace: copy.deepcopy(volume if name == 'legacy' else source)),
            kubectl=Mock())
        reasons = ['cannot change replica count for legacy linked-clone volumes',
                   'cannot expand legacy linked-clone volumes', 'cannot migrate legacy linked-clone volumes',
                   'cannot add recurring jobs to legacy linked-clone volumes']
        lab.kubectl.side_effect = [SimpleNamespace(returncode=1, stdout='', stderr=reason) for reason in reasons]
        return lab, origin, volume

    def test_real_upgrade_legacy_four_dry_run_refusals_without_label_forgery(self):
        lab, origin, volume = self.fixture()
        with redirect_stdout(io.StringIO()): MODULE.legacy_refusals(lab, origin)
        self.assertEqual(4, lab.kubectl.call_count)
        for call in lab.kubectl.call_args_list:
            self.assertIn('--dry-run=server', call.args)
            patch = json.loads(call.kwargs['stdin'])
            self.assertEqual('/metadata/uid', patch[0]['path'])
            self.assertEqual('/metadata/resourceVersion', patch[1]['path'])
            if patch[2]['path'] == '/metadata/labels':
                self.assertEqual('true', patch[2]['value'][MODULE.LEGACY_LABEL])
                self.assertEqual(2, len(patch[2]['value']))

    def test_legacy_rejects_wrong_origin_identity_marker_or_successful_admission(self):
        for defect in ('version', 'uid', 'source', 'marker', 'accepted', 'wrong-refusal'):
            with self.subTest(defect=defect):
                lab, origin, volume = self.fixture()
                if defect == 'version': origin['created_version'] = '1.12.1'
                if defect == 'uid': origin['volume_uid'] = 'foreign'
                if defect == 'source': origin['source_volume_uid'] = 'foreign'
                if defect == 'marker': volume['metadata']['labels'].clear()
                if defect == 'accepted': lab.kubectl.side_effect = None; lab.kubectl.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
                if defect == 'wrong-refusal': lab.kubectl.side_effect = None; lab.kubectl.return_value = SimpleNamespace(returncode=1, stdout='', stderr='transport failure')
                with self.assertRaises(ValueError): MODULE.legacy_refusals(lab, origin)
                if defect in ('version', 'uid', 'source', 'marker'): lab.kubectl.assert_not_called()


class LifecycleExitContracts(unittest.TestCase):
    def test_clean_receipt_survives_store_cleanup_failure_and_is_retired_only_at_final_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SimpleNamespace(cleanup=Mock(side_effect=RuntimeError('transport remains')))
            def get(kind, name='', namespace=''):
                return {'items': []} if not name else None
            lab = SimpleNamespace(DIRECTORY=Path(directory), get=get, backup_store=lambda: store,
                restore_request_input=Mock(), owner=lambda: {'run_id': 'run'})
            probe = MODULE.Probe(lab, 91)
            probe.cleanup_backup_objects = Mock()
            row = {'run_id': 'run', 'namespace': 'longhorn-linked-test', 'objects': {}, 'claims': {},
                   'class_config': {'name': 'child'}, 'source_class': {'name': 'source'}}
            probe.record.write_text(json.dumps(row))
            with self.assertRaises(RuntimeError): probe.cleanup()
            self.assertTrue(probe.record.exists())
            store.cleanup.side_effect = None
            with redirect_stdout(io.StringIO()): probe.cleanup()
            self.assertFalse(probe.record.exists())

    def test_business_keeps_three_replica_baseline_and_retires_graph_before_restore_remount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); events = []
            config = {'longhorn_storage_classes': [{'name': 'longhorn', 'replicas': 3,
                'fs_type': 'ext4', 'default': False, 'encrypted': False, 'encryption_secret': '',
                'recurring_jobs': [], 'data_engine': 'v2', 'clone_mode': 'full-copy', 'reclaim_policy': 'Retain'}],
                'longhorn_block_disks': {'192.168.47.146': [{'name': 'disk'}]}}
            (root/'config.yml').write_text(yaml.safe_dump(config))
            store = SimpleNamespace(prepare=Mock(), qualify=Mock(), product_configuration=Mock(return_value={}))
            def get(kind, name='', namespace=''):
                if not name: return {'items': []}
                if kind == 'nodes.longhorn.io':
                    return {'spec': {'disks': {'disk': {'allowScheduling': True, 'evictionRequested': False}}}}
                return {'metadata': {'uid': name+'-uid'}}
            lab = SimpleNamespace(DIRECTORY=root, yaml=yaml, get=get, owner=lambda: {'run_id': 'run'},
                backup_store=lambda: store, write_private=Mock(), scoped_cleanup=lambda action: nullcontext(),
                namespace_defaults=lambda ns: ['default'],
                wait=lambda name, seconds, fn: fn(), kubectl=Mock(return_value=SimpleNamespace(stdout='{"metadata":{"uid":"ns"}}')))
            probe = MODULE.Probe(lab, 91); declarations = []
            probe.config = Mock(side_effect=lambda **kwargs: declarations.append(kwargs))
            probe.claim = Mock(); probe.io = Mock(return_value='digest'); probe.evacuate = Mock()
            probe.backup = Mock()
            def restore(name):
                probe.journal.setdefault('backups', {})[name] = {}
                probe.journal.setdefault('restores', {})[name] = {'payload': name}
                events.append(('restore', name))
            probe.restore = Mock(side_effect=restore)
            probe.retire = Mock(side_effect=lambda name: events.append(('retire', name)))
            probe.delete_pod = Mock()
            probe.client = Mock(side_effect=lambda name, index, suffix: events.append(('remount', name)) or name+suffix)
            with redirect_stdout(io.StringIO()): probe.business(91)
            classes = declarations[0]['longhorn_storage_classes']
            self.assertEqual([3, 2, 2], [row['replicas'] for row in classes])
            self.assertEqual(['full-copy', 'full-copy', 'linked-clone'], [row['clone_mode'] for row in classes])
            self.assertNotIn('number_of_replicas', classes[1])
            self.assertEqual([('restore', 'clone'), ('restore', 'nested'), ('retire', 'nested'),
                ('retire', 'clone'), ('retire', 'source'), ('remount', 'restored-clone'),
                ('remount', 'restored-nested')], events)
            self.assertEqual(2, probe.backup.call_count)


class RestoreSnapshotRaceContracts(unittest.TestCase):
    class ReachedPVC(Exception): pass

    def fixture(self, directory, defect=None):
        objects = {}; created = []; state = {'settled': False}
        def create(journal, record, obj):
            obj = copy.deepcopy(obj); kind = obj['kind']; name = obj['metadata']['name']
            if kind == 'PersistentVolumeClaim': raise self.ReachedPVC()
            obj['metadata']['uid'] = kind+'-uid'; created.append(kind)
            objects[kind] = obj; journal['objects'][kind+'/'+name] = obj['metadata']['uid']
            return obj
        def get(kind, name='', namespace=''):
            key = ('VolumeSnapshotContent' if kind.startswith('volumesnapshotcontents.') else 'VolumeSnapshot')
            obj = copy.deepcopy(objects[key])
            if key == 'VolumeSnapshotContent':
                if defect == 'content-replaced': obj['metadata']['uid'] = 'foreign'
                if defect == 'wrong-binding': obj['spec']['volumeSnapshotRef']['uid'] = 'foreign'
            else:
                obj['status'] = ({'readyToUse': True, 'boundVolumeSnapshotContentName': name}
                    if state['settled'] else {'error': {'message': ('backend restore denied' if defect == 'terminal' else 'VolumeSnapshotContent is missing')}})
            return obj
        def wait(stage, timeout, predicate):
            self.assertTrue(stage.startswith('linked-backup-snapshot-ready-'))
            self.assertFalse(predicate(), 'transient cache miss must wait without accepting readiness')
            state['settled'] = True
            self.assertTrue(predicate(), 'same bound Content and settled snapshot must permit PVC creation')
        lab = SimpleNamespace(DIRECTORY=Path(directory), get=get, wait=wait,
            create_business_object=create, write_private=lambda p,v:p.write_text(json.dumps(v)))
        probe = MODULE.Probe(lab, 91)
        probe.journal = {'namespace':'longhorn-linked-race','objects':{},
            'backups':{'clone':{'volume':'source-volume','backup':'backup-one','receipt':{'size':2147483648}}}}
        return probe, created

    def test_real_restore_waits_through_exact_content_cache_miss_before_creating_pvc(self):
        with tempfile.TemporaryDirectory() as directory:
            probe, created = self.fixture(directory)
            with self.assertRaises(self.ReachedPVC): probe.restore('clone')
            self.assertEqual(['VolumeSnapshot','VolumeSnapshotContent'],created)

    def test_real_restore_refuses_terminal_errors_and_replaced_or_misbound_content(self):
        for defect, reason in (('terminal','returned an error'),('content-replaced','replaced'),('wrong-binding','binding')):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe,_ = self.fixture(directory,defect)
                with self.assertRaisesRegex(ValueError,reason):probe.restore('clone')


class FailedRestoreCleanupOrderContracts(unittest.TestCase):
    def test_real_cleanup_retires_native_graph_before_detached_snapshot_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            objects={};events=[];namespace='longhorn-linked-cleanup'
            def obj(kind,name,spec=None,status=None):
                value={'kind':kind,'metadata':{'name':name,'uid':kind+'-'+name},'spec':spec or {},'status':status or {}}
                objects[(kind,name)]=value;return value
            ns=obj('namespace',namespace)
            row={'run_id':'run','namespace':namespace,'namespace_uid':ns['metadata']['uid'],'objects':{},'claims':{},
                 'class_name':'child','class_config':{'name':'child'},'source_class':{'name':'source-class'}}
            for name in ('source','clone','nested'):
                cls='source-class' if name=='source' else 'child'
                pvc=obj('pvc',name,{'storageClassName':cls});row['objects']['PersistentVolumeClaim/'+name]=pvc['metadata']['uid']
                pv=obj('pv','pv-'+name,{'claimRef':{'namespace':namespace,'name':name,'uid':pvc['metadata']['uid']},
                    'storageClassName':cls,'persistentVolumeReclaimPolicy':'Retain',
                    'csi':{'driver':'driver.longhorn.io','volumeHandle':'volume-'+name}})
                volume=obj('volumes.longhorn.io','volume-'+name,{'dataEngine':'v2'},{'state':'detached'})
                row['claims'][name]={'pv':pv['metadata']['name'],'pv_uid':pv['metadata']['uid'],
                    'volume':volume['metadata']['name'],'volume_uid':volume['metadata']['uid']}
            snapshot=obj('snapshots.longhorn.io','backup-snapshot',{'volume':'volume-clone','createSnapshot':True})
            row['backups']={'clone':{'volume':'volume-clone','backup':'backup','snapshot':'backup-snapshot',
                'snapshot_uid':snapshot['metadata']['uid'],'request':{'kind':'snapshot','name':'backup-snapshot','volume':'volume-clone'},
                'backup_request':{'kind':'backup','name':'backup','snapshot':'backup-snapshot','mode':'full'}}}
            def get(kind,name='',namespace=''):
                if name:return copy.deepcopy(objects.get((kind,name)))
                return {'items':[copy.deepcopy(v) for (k,n),v in objects.items() if k==kind]}
            def remove(value):
                kind,name=value['kind'],value['metadata']['name']
                if kind=='snapshots.longhorn.io':raise RuntimeError('detached snapshot deletion is delayed')
                if kind=='volumes.longhorn.io':
                    child={'volume-source':'volume-clone','volume-clone':'volume-nested'}.get(name)
                    self.assertNotIn(('volumes.longhorn.io',child),objects,'native parent still has linked child')
                    events.append(name)
                    for key in list(objects):
                        if key[0]=='snapshots.longhorn.io' and objects[key]['spec']['volume']==name:del objects[key]
                del objects[(kind,name)]
            def wait(stage,timeout,predicate):
                value=predicate()
                if not value:raise AssertionError('unmet actual cleanup predicate '+stage)
                return value
            product=SimpleNamespace(check_request=Mock(),request_object=lambda x:x)
            lab=SimpleNamespace(DIRECTORY=Path(directory), get=get, remove=remove,wait=wait,
                write_private=lambda p,v:p.write_text(json.dumps(v)),restore_request_input=Mock(),
                owner=lambda:{'run_id':'run'}, check_business_graph=Mock(),
                backup_store=lambda:SimpleNamespace(cleanup=Mock()),load_helper=lambda _:product)
            probe=MODULE.Probe(lab,91);probe.record.write_text(json.dumps(row))
            with redirect_stdout(io.StringIO()):probe.cleanup()
            self.assertEqual(['volume-nested','volume-clone','volume-source'],events)
            self.assertFalse(probe.record.exists())
            self.assertFalse(objects)


if __name__ == '__main__': unittest.main()
