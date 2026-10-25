"""Refusal must precede destructive hooks and preserve exact data identities."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_uninstall', ROOT/'tests/helpers/longhorn-uninstall.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
spec = importlib.util.spec_from_file_location('retention_backups', ROOT/'tests/helpers/longhorn-backups.py')
backups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backups)


class NativeMetadataContracts(unittest.TestCase):
    def lease(self, name='longhorn-manager-webhook-lock'):
        return {'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
                'metadata': {'name': name, 'namespace': 'longhorn-system', 'uid': 'native-lease'},
                'spec': {'holderIdentity': 'worker', 'leaseDurationSeconds': 20, 'leaseTransitions': 1,
                         'acquireTime': '2026-10-20T00:00:00Z', 'renewTime': '2026-10-20T00:00:00Z'}}

    def test_only_native_factory_shapes_and_valid_current_or_retired_node_names_are_bound(self):
        for defect in ('none', 'released', 'rotation', 'retired-holder', 'holder-type', 'api', 'kind', 'namespace', 'uid', 'owner', 'labels',
                       'annotations', 'deleting', 'holder', 'duration', 'transition', 'time', 'unknown-field'):
            with self.subTest(defect=defect):
                obj = self.lease(); meta = obj['metadata']; spec = obj['spec']
                if defect == 'released': spec.update(holderIdentity='', leaseDurationSeconds=1)
                if defect == 'rotation': meta['name']='longhorn-webhook-cert-rotation'; spec['leaseDurationSeconds']=60
                if defect == 'api': obj['apiVersion']='coordination.k8s.io/v1beta1'
                if defect == 'kind': obj['kind']='Secret'
                if defect == 'namespace': meta['namespace']='foreign'
                if defect == 'uid': del meta['uid']
                if defect == 'owner': meta['ownerReferences']=[{'uid': 'foreign'}]
                if defect == 'labels': meta['labels']={'owner': 'foreign'}
                if defect == 'annotations': meta['annotations']={'foreign': 'annotation'}
                if defect == 'deleting': meta['deletionTimestamp']='now'
                if defect == 'retired-holder': spec['holderIdentity']='longhorn-node-148'
                if defect == 'holder': spec['holderIdentity']='foreign/node'
                if defect == 'holder-type': spec['holderIdentity']=123
                if defect == 'duration': spec['leaseDurationSeconds']=60
                if defect == 'transition': spec['leaseTransitions']=-1
                if defect == 'time': spec['renewTime']=None
                if defect == 'unknown-field': spec['unrecognized']='value'
                lab = SimpleNamespace(get=Mock(side_effect=lambda kind, **kw:
                    {'items': [{'metadata': {'name': 'worker'}}]} if kind=='nodes' else {'items': [obj]}))
                if defect in ('none', 'released', 'rotation', 'retired-holder'):
                    self.assertEqual(helper.native_leases(lab), {'Lease/'+meta['name']: 'native-lease'})
                else:
                    with self.assertRaisesRegex(ValueError, 'unrecognized native webhook lease'):
                        helper.native_leases(lab)

    def test_unknown_lease_remains_outside_the_namespace_deletion_boundary(self):
        spec=importlib.util.spec_from_file_location('lease_graph_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        known=self.lease(); unknown=self.lease('foreign-coordinator'); unknown['metadata']['uid']='foreign-uid'
        objects=[known]
        def get(kind, name='', namespace=''):
            if kind=='nodes': return {'items':[{'metadata':{'name':'worker'}}]}
            if kind=='namespace': return {'metadata':{'uid':'original-namespace'}}
            if kind=='leases.coordination.k8s.io': return {'items':copy.deepcopy(objects)}
            raise AssertionError(kind)
        with patch.object(lab,'get',side_effect=get), patch.object(lab,'proven_business_events',return_value=[]), \
                patch.object(lab,'kubectl',return_value=SimpleNamespace(stdout='leases.coordination.k8s.io')):
            # Original recovery omitted the native lease UID from this graph.
            with self.assertRaisesRegex(ValueError,'foreign resource.*leases.coordination.k8s.io'):
                lab.check_business_graph({'namespace':'longhorn-system','namespace_uid':'original-namespace','objects':{}})
            journal={'namespace':'longhorn-system','namespace_uid':'original-namespace',
                     'objects':helper.native_leases(lab)}
            lab.check_business_graph(journal)
            objects.append(unknown)
            self.assertEqual(helper.native_leases(lab),journal['objects'])
            with self.assertRaisesRegex(ValueError,'foreign resource'):
                lab.check_business_graph(journal)

    def test_recovery_checks_retained_objects_after_native_removal_before_backend_cleanup(self):
        for defect in ('none', 'no-backend', 'native', 'readback', 'cleanup'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'store.json';calls=[]
                if defect!='no-backend': path.write_text('{}')
                def native(pid):
                    calls.append('native')
                    if defect=='native': raise ValueError('native failure')
                def readback(lab,*,phase):
                    calls.append('readback');self.assertEqual(phase,'uninstalled')
                    if defect=='readback': raise ValueError('changed retained backup')
                def cleanup(*,after_uninstall):
                    calls.append('cleanup');self.assertIs(after_uninstall,True)
                    if defect=='cleanup': raise ValueError('cleanup failure')
                lab=SimpleNamespace(recover_installation=native,backup_store=Mock(return_value=
                    SimpleNamespace(record=path,cleanup=cleanup)))
                with patch.object(helper,'verify_retained_backup',side_effect=readback),redirect_stdout(io.StringIO()) as output:
                    if defect in ('none','no-backend'):
                        helper.recover(lab,123)
                        self.assertEqual(calls,['native','readback','cleanup'] if defect=='none' else ['native'])
                    else:
                        with self.assertRaises(ValueError): helper.recover(lab,123)
                        self.assertNotIn('_PASS',output.getvalue())
                        self.assertEqual(calls,{'native':['native'],'readback':['native','readback'],
                                               'cleanup':['native','readback','cleanup']}[defect])


class RefusalContracts(unittest.TestCase):
    def test_destroy_refusal_runs_the_real_cli_command_and_requires_owning_nonzero_exit(self):
        spec = importlib.util.spec_from_file_location('uninstall_lab_contract', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        reason = 'Longhorn PVs remain; migrate/retain data and uninstall storage before destroy'
        for rc, message in ((1, reason), (0, reason), (1, 'API unavailable')):
            with self.subTest(rc=rc, message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'kubecli.py').write_text(
                    'import sys\nassert sys.argv[1:]==["destroy","longhorn-gate"]\n'
                    'print(' + repr('LONGHORN_PRODUCT_FAIL reason=' + message) + ')\nsys.exit(' + str(rc) + ')\n')
                identity = ([b'python', str(Path(lab.__file__).resolve()).encode(), b'relay-maintain'], '123')
                with patch.object(lab, 'BASE', root), patch.object(lab, 'DIRECTORY', root), \
                        patch.object(lab, 'owner'), patch.object(lab, 'process_identity', return_value=identity), \
                        redirect_stdout(io.StringIO()) as output:
                    if rc == 1 and message == reason:
                        lab.supervise_core(123, 'longhorn-destroy', expected_refusal=reason)
                        self.assertIn('LONGHORN_EXPECTED_PRODUCT_REFUSAL_VERIFIED', output.getvalue())
                    else:
                        with self.assertRaisesRegex(ValueError, 'owning expected refusal'):
                            lab.supervise_core(123, 'longhorn-destroy', expected_refusal=reason)
                        self.assertTrue(list(root.glob('unexpected-refusal-*.json')))

    def fixture(self, directory, phase):
        root = Path(directory)
        (root/'config.yml').write_text('original product input')
        journal = {'namespace': 'longhorn-business-owned', 'pv': 'pv', 'pv_uid': 'pv-uid',
                   'volume': 'volume', 'volume_uid': 'volume-uid',
                   'objects': {'PersistentVolumeClaim/data': 'claim-uid'}}
        objects = {
            ('pv', 'pv'): {'metadata': {'uid': 'pv-uid'},
                          'spec': {'persistentVolumeReclaimPolicy': 'Retain',
                                   'claimRef': {'uid': 'claim-uid'}, 'csi': {'volumeHandle': 'volume'}},
                          'status': {'phase': 'Bound' if phase == 'bound' else 'Released'}},
            ('volumes.longhorn.io', 'volume'): {'metadata': {'uid': 'volume-uid'},
                'spec': {'nodeID': 'worker' if phase == 'bound' else ''},
                'status': {'state': 'attached' if phase == 'bound' else 'detached'}},
            ('settings.longhorn.io', 'deleting-confirmation-flag'):
                {'metadata': {'uid': 'setting-uid'}, 'value': 'false'},
            ('job', 'longhorn-uninstall'): None}
        calls = []
        lab = SimpleNamespace(DIRECTORY=root,
            get=Mock(side_effect=lambda kind, name, *a: copy.deepcopy(objects[kind, name])),
            wait=Mock(side_effect=lambda label, timeout, check: check()),
            idempotence_snapshot=Mock(return_value={'helm': {'revision': '1'}, 'objects': {'original': 'uid'}}))
        lab.run_requests = Mock(side_effect=lambda *a, **k: calls.append(('uninstall', a, k)))
        lab.supervise_core = Mock(side_effect=lambda *a, **k: calls.append(('destroy', a, k)))
        return lab, journal, objects, calls

    def test_bound_and_released_refusals_use_product_entry_and_preserve_state(self):
        for phase in ('bound', 'released'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                lab, journal, objects, calls = self.fixture(directory, phase)
                original = copy.deepcopy(objects)
                with redirect_stdout(io.StringIO()) as output:
                    helper.verify_refusal(lab, journal, 123, phase)
                self.assertEqual([c[0] for c in calls], ['uninstall', 'destroy'])
                self.assertEqual(calls[0][1], (123, []))
                self.assertTrue(calls[0][2]['uninstall_id'].startswith('refusal-'))
                self.assertEqual(calls[0][2]['expected_refusal'],
                                 'Longhorn uninstall refused: retained or bound PVs remain')
                self.assertEqual(calls[1][1], (123, 'longhorn-destroy'))
                self.assertIn('Longhorn PVs remain;', calls[1][2]['expected_refusal'])
                self.assertEqual(objects, original)
                self.assertIn('LONGHORN_UNINSTALL_REFUSAL_' + phase.upper() + '_PASS', output.getvalue())
                self.assertEqual(lab.wait.call_count, int(phase == 'released'))

    def test_unknown_phase_or_changed_precondition_blocks_both_commands(self):
        for defect in ('phase', 'pv-uid', 'claim-ref', 'policy', 'volume-uid', 'flag', 'hook'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, objects, calls = self.fixture(directory, 'bound')
                if defect == 'pv-uid': objects['pv', 'pv']['metadata']['uid'] = 'replacement'
                if defect == 'claim-ref': objects['pv', 'pv']['spec']['claimRef']['uid'] = 'other'
                if defect == 'policy': objects['pv', 'pv']['spec']['persistentVolumeReclaimPolicy'] = 'Delete'
                if defect == 'volume-uid': objects['volumes.longhorn.io', 'volume']['metadata']['uid'] = 'other'
                if defect == 'flag': objects['settings.longhorn.io', 'deleting-confirmation-flag']['value'] = 'true'
                if defect == 'hook': objects['job', 'longhorn-uninstall'] = {'metadata': {'uid': 'hook'}}
                with self.assertRaises(ValueError):
                    helper.verify_refusal(lab, journal, 123, 'unknown' if defect == 'phase' else 'bound')
                self.assertEqual(calls, [])

    def test_failed_refusal_or_changed_state_stops_before_the_next_command(self):
        for defect in ('cli', 'pv', 'volume', 'flag', 'hook', 'config', 'snapshot'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, journal, objects, calls = self.fixture(directory, 'bound')
                def change(*args, **kwargs):
                    calls.append(('uninstall', args, kwargs))
                    if defect == 'cli': raise ValueError('expected refusal not proved')
                    if defect == 'pv': objects['pv', 'pv']['metadata']['uid'] = 'replacement'
                    if defect == 'volume': objects['volumes.longhorn.io', 'volume']['spec']['nodeID'] = 'other'
                    if defect == 'flag': objects['settings.longhorn.io', 'deleting-confirmation-flag']['value'] = 'true'
                    if defect == 'hook': objects['job', 'longhorn-uninstall'] = {'metadata': {'uid': 'hook'}}
                    if defect == 'config': (lab.DIRECTORY/'config.yml').write_text('changed')
                    if defect == 'snapshot': lab.idempotence_snapshot.return_value = {'helm': {'revision': '2'}}
                lab.run_requests.side_effect = change
                with redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError):
                    helper.verify_refusal(lab, journal, 123, 'bound')
                lab.supervise_core.assert_not_called()
                self.assertNotIn('_PASS', output.getvalue())

    def test_released_volume_must_detach_before_refusal_and_replacement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, objects, calls = self.fixture(directory, 'released')
            volume = objects['volumes.longhorn.io', 'volume']
            volume['spec']['nodeID'] = 'worker'; volume['status']['state'] = 'detaching'
            def wait(label, timeout, check):
                self.assertIsNone(check())
                self.assertEqual(calls, [])
                volume['metadata']['uid'] = 'replacement'
                return check()
            lab.wait.side_effect = wait
            with self.assertRaisesRegex(ValueError, 'identity changed while detaching'):
                helper.verify_refusal(lab, journal, 123, 'released')
            self.assertEqual(calls, [])


class RetentionContracts(unittest.TestCase):
    def test_native_error_is_retained_privately_and_only_safe_categories_reach_traceback_reader(self):
        spec=importlib.util.spec_from_file_location('failure_capture_lab',ROOT/'tests/helpers/longhorn-lab.py')
        lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        temporary=tempfile.NamedTemporaryFile
        for native in (None,'Error from server (Conflict): Precondition failed: DO-NOT-PRINT-CREDENTIAL'):
            with self.subTest(native=bool(native)),tempfile.TemporaryDirectory() as directory:
                def create(*args,**kwargs):
                    self.assertEqual(kwargs.pop('dir'),'/var/tmp')
                    return temporary(*args,dir=directory,**kwargs)
                with patch.object(lab.tempfile,'NamedTemporaryFile',side_effect=create),redirect_stderr(io.StringIO()) as output:
                    try:
                        error=ValueError('safe failure')
                        if native is not None:error.stderr=native;error.returncode=1
                        raise error
                    except ValueError as error:lab.record_failure(error)
                traces=list(Path(directory).glob('kubeauto-longhorn-failure-*'))
                commands=list(Path(directory).glob('kubeauto-longhorn-command-*'))
                self.assertEqual(len(traces),1);self.assertEqual(len(commands),int(native is not None))
                self.assertNotIn('DO-NOT-PRINT-CREDENTIAL',output.getvalue()+traces[0].read_text())
                for path in traces+commands:self.assertEqual(path.stat().st_mode&0o777,0o600)
                if native is not None:
                    self.assertEqual(json.loads(commands[0].read_text()),{'returncode':1,'stderr':native})
                    summary=json.loads(output.getvalue().split('LONGHORN_COMMAND_FAILURE_SUMMARY ',1)[1])
                    self.assertTrue(summary['conflict']);self.assertTrue(summary['precondition'])

    def test_business_cleanup_deletes_the_detached_revision_with_real_api_preconditions(self):
        spec = importlib.util.spec_from_file_location('detach_cleanup_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        spec = importlib.util.spec_from_file_location('detach_cleanup_product', ROOT/'roles/longhorn/files/longhorn.py')
        product = importlib.util.module_from_spec(spec); spec.loader.exec_module(product)
        for outcome in ('detached', 'already-detached', 'replacement', 'missing'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);namespace='longhorn-business-detach'
                record=root/'longhorn-install-business.json'
                journal={'namespace':namespace,'namespace_uid':'namespace-uid','pv':'pv','pv_uid':'pv-uid',
                         'volume':'volume','volume_uid':'volume-uid','reclaim_policy':'Retain',
                         'objects':{'PersistentVolumeClaim/data':'claim-uid'}}
                record.write_text(json.dumps(journal))
                objects={
                    ('namespace',namespace):{'kind':'Namespace','metadata':{'name':namespace,'uid':'namespace-uid'}},
                    ('pvc','data'):{'kind':'PersistentVolumeClaim','metadata':{'name':'data','uid':'claim-uid'}},
                    ('pv','pv'):{'kind':'PersistentVolume','metadata':{'name':'pv','uid':'pv-uid'},'spec':{
                        'claimRef':{'name':'data','namespace':namespace,'uid':'claim-uid'},
                        'storageClassName':'longhorn','persistentVolumeReclaimPolicy':'Retain',
                        'csi':{'driver':'driver.longhorn.io','volumeHandle':'volume'}}},
                    ('volumes.longhorn.io','volume'):{'apiVersion':'longhorn.io/v1beta2','kind':'Volume',
                        'metadata':{'name':'volume','namespace':'longhorn-system','uid':'volume-uid','resourceVersion':'old'},
                        'status':{'state':'detached' if outcome=='already-detached' else 'detaching'}}}
                deletion_options=[]
                def get(kind,name='',namespace=''):
                    return copy.deepcopy(objects.get((kind,name))) if name else {'items':copy.deepcopy([v for (k,_),v in objects.items() if k==kind])}
                def native(argv,stdin=None,**kwargs):
                    if 'get' in argv:return json.dumps({'resources':[{'kind':'Volume','name':'volumes'}]})
                    self.assertIn('delete',argv)
                    options=json.loads(stdin);deletion_options.append(options)
                    live=objects['volumes.longhorn.io','volume']['metadata']
                    self.assertEqual(options['preconditions'],{'uid':live['uid'],'resourceVersion':live['resourceVersion']},
                                     'guarded deletion used the pre-detach resourceVersion')
                    objects.pop(('volumes.longhorn.io','volume'));return '{}'
                def remove(obj):
                    if obj['kind']=='Volume': product.API('fixture-kubeconfig').delete(obj)
                    else:
                        key=next(k for k,v in objects.items() if v['metadata']['uid']==obj['metadata']['uid'])
                        objects.pop(key)
                def wait(label,timeout,check):
                    if label=='business-volume-detached':
                        current=objects['volumes.longhorn.io','volume']
                        current['status']['state']='detached';current['metadata']['resourceVersion']='new'
                        if outcome=='replacement':current['metadata']['uid']='foreign'
                        if outcome=='missing':objects.pop(('volumes.longhorn.io','volume'))
                    result=check();self.assertTrue(result,label);return result
                with patch.object(lab,'DIRECTORY',root),patch.object(lab,'owner',return_value={}), \
                        patch.object(lab,'get',side_effect=get),patch.object(lab,'remove',side_effect=remove), \
                        patch.object(lab,'wait',side_effect=wait),patch.object(lab,'check_business_graph'), \
                        patch.object(product,'run',side_effect=native),redirect_stdout(io.StringIO()):
                    if outcome in ('detached','already-detached'):
                        lab.cleanup_business(record)
                        self.assertFalse(record.exists());self.assertFalse(objects)
                        self.assertEqual(deletion_options[0]['preconditions']['resourceVersion'],
                                         'old' if outcome=='already-detached' else 'new')
                    else:
                        with self.assertRaisesRegex(ValueError,'identity changed'):lab.cleanup_business(record)
                        self.assertFalse(deletion_options);self.assertTrue(record.exists())

    def fixture(self, directory):
        root = Path(directory)
        (root/'config.yml').write_text(yaml.safe_dump({'longhorn_backup_target': '',
            'longhorn_backup_secret': '', 'longhorn_requests': [], 'longhorn_action': 'present'}))
        original = {'backupTargetURL': '', 'credentialSecret': '', 'pollInterval': '5m0s'}
        desired = dict(original, backupTargetURL='s3://owned@us-east-1/', credentialSecret='owned')
        target = {'metadata': {'uid': 'target-uid', 'resourceVersion': '21'},
                  'spec': dict(desired, syncRequestedAt='native'), 'status': {'available': True}}
        receipt = {'volume': 'volume', 'backup': 'full', 'size': 2*1024**3, 'incremental': False,
                   'newly_uploaded_bytes': 32, 'reuploaded_bytes': 0,
                   'objects': {'backup.cfg': {'size': 12, 'sha256': 'metadata'},
                               'blocks/block.blk': {'size': 32, 'sha256': 'data'}}}
        journal = {'volume': 'volume', 'volume_uid': 'volume-uid',
                   'backups': {'full': {'uid': 'backup-uid', 'object_receipt': receipt}},
                   'backup_volume': {'name': 'backup-volume', 'uid': 'backup-volume-uid'}}
        resources = {kind: [] for kind in backups.BACKUP_RESOURCES}
        resources['backups.longhorn.io'] = [{'metadata': {'name': 'full', 'uid': 'backup-uid'}}]
        resources['backupvolumes.longhorn.io'] = [{'metadata': journal['backup_volume'].copy()}]
        fixture = {'bucket': 'owned', 'product_binding': {'target_uid': 'target-uid',
                   'original_spec': original, 'fixture_spec': desired}}
        store = SimpleNamespace(directory=root, receipt=Mock(return_value=fixture),
            object_digest=Mock(return_value={'size': 23, 'sha256': 'volume-cfg'}),
            backup_receipt=Mock(side_effect=lambda *args: copy.deepcopy(receipt)))
        def get(kind, *args, **kwargs):
            if kind == 'volumes.longhorn.io': return None
            if kind == 'pv': return {'items': []}
            return copy.deepcopy(target) if kind == 'backuptargets.longhorn.io' else {'items': copy.deepcopy(resources[kind])}
        lab = SimpleNamespace(DIRECTORY=root, yaml=yaml, write_private=lambda p,v: p.write_text(json.dumps(v)),
            owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster'}),
            backup_store=Mock(return_value=store), load_helper=Mock(return_value=backups),
            get=Mock(side_effect=get), supervise_core=Mock())
        def command(*args, stdin):
            self.assertEqual(args, ('-n', 'longhorn-system', 'patch', 'backuptargets.longhorn.io',
                                   'default', '--type=json', '--patch-file=/dev/stdin'))
            operations = json.loads(stdin)
            self.assertEqual(operations[:3], [
                {'op': 'test', 'path': '/metadata/uid', 'value': 'target-uid'},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '21'},
                {'op': 'test', 'path': '/spec', 'value': target['spec']}])
            self.assertEqual(operations[3]['value'], dict(original, syncRequestedAt='native'))
            target['spec'] = operations[3]['value']; target['status']['available'] = False
            for kind in resources: resources[kind] = []
        lab.kubectl = Mock(side_effect=command)
        def wait(label, timeout, predicate):
            self.assertEqual((label, timeout), ('uninstall-backup-target-disconnected', 600))
            self.assertTrue(predicate())
        lab.wait = Mock(side_effect=wait)
        return lab, journal, store, target, resources

    def test_original_native_target_is_disconnected_and_every_original_object_read_after_source_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, store, target, resources = self.fixture(directory)
            with redirect_stdout(io.StringIO()) as output:
                helper.retain_backup(lab, journal)
                helper.verify_retained_backup(lab, phase='source-removed')
            lab.kubectl.assert_called_once(); lab.supervise_core.assert_not_called()
            self.assertEqual(store.backup_receipt.call_count, 3)
            self.assertEqual(store.object_digest.call_count, 4)
            self.assertIn('LONGHORN_UNINSTALL_BACKUP_TARGET_DISCONNECTED_PASS', output.getvalue())
            self.assertIn('phase=source-removed', output.getvalue())
            self.assertTrue(all(not items for items in resources.values()))

    def test_foreign_resources_or_unverified_backup_stop_before_target_mutation(self):
        for fault in ('duplicate', 'incremental', 'target', 'backup', 'volume', 'foreign', 'declaration', 'content'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, journal, store, target, resources = self.fixture(directory)
                if fault == 'duplicate': (Path(directory)/'retained-backup.json').write_text('{}')
                if fault == 'incremental': journal['backups']['full']['object_receipt']['incremental'] = True
                if fault == 'target': target['metadata']['uid'] = 'replacement'
                if fault == 'backup': resources['backups.longhorn.io'][0]['metadata']['uid'] = 'replacement'
                if fault == 'volume': resources['backupvolumes.longhorn.io'][0]['metadata']['uid'] = 'replacement'
                if fault == 'foreign': resources['systembackups.longhorn.io'].append({'metadata': {'uid': 'foreign'}})
                if fault == 'declaration': (Path(directory)/'config.yml').write_text('longhorn_backup_target: s3://foreign')
                if fault == 'content': store.backup_receipt.side_effect = lambda *args: {'changed': True}
                with redirect_stdout(io.StringIO()), self.assertRaises(ValueError): helper.retain_backup(lab, journal)
                lab.kubectl.assert_not_called(); lab.wait.assert_not_called()

    def test_disconnect_or_readback_failure_never_emits_success(self):
        for fault in ('patch', 'target-uid', 'resources', 'object'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, journal, store, target, resources = self.fixture(directory)
                command = lab.kubectl.side_effect
                def mutate(*args, **kwargs):
                    if fault == 'patch': raise ValueError('conflict')
                    command(*args, **kwargs)
                    if fault == 'target-uid': target['metadata']['uid'] = 'replacement'
                    if fault == 'resources': resources['backups.longhorn.io'].append({'metadata': {'uid': 'remaining'}})
                    if fault == 'object': store.object_digest.return_value = {'size': 0, 'sha256': 'changed'}
                lab.kubectl.side_effect = mutate
                with redirect_stdout(io.StringIO()) as output, self.assertRaises((ValueError, AssertionError)):
                    helper.retain_backup(lab, journal)
                self.assertNotIn('_PASS', output.getvalue())

    def test_retained_readback_refuses_replacement_run_bucket_and_receipt_symlink(self):
        for fault in ('run', 'bucket', 'symlink', 'phase', 'source', 'pv'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, journal, store, target, resources = self.fixture(directory)
                with redirect_stdout(io.StringIO()): helper.retain_backup(lab, journal)
                path = Path(directory)/'retained-backup.json'
                if fault == 'run': lab.owner.return_value['run_id'] = 'other'
                if fault == 'bucket': store.receipt.return_value['bucket'] = 'foreign'
                if fault == 'symlink':
                    saved = path.with_suffix('.saved'); path.rename(saved); path.symlink_to(saved)
                if fault in ('source', 'pv'):
                    original_get = lab.get.side_effect
                    def get(kind, *args, **kwargs):
                        if kind == 'volumes.longhorn.io' and fault == 'source': return {'metadata': {'uid': 'volume-uid'}}
                        if kind == 'pv' and fault == 'pv': return {'items': [{'spec': {'csi': {'volumeHandle': 'volume'}}}]}
                        return original_get(kind, *args, **kwargs)
                    lab.get.side_effect = get
                with self.assertRaises(ValueError):
                    helper.verify_retained_backup(lab, phase='other' if fault == 'phase' else 'source-removed')

    def test_backup_suite_restores_binding_before_postcleanup_read_and_always_owns_external_cleanup(self):
        spec = importlib.util.spec_from_file_location('retention_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        for failure in ('none', 'business', 'restore', 'read'):
            with self.subTest(failure=failure), patch.dict(sys.modules, {lab.__name__: lab}):
                calls = []; store = Mock(); store.product_configuration.return_value = {'fixture': True}
                for name in ('prepare', 'qualify', 'restore_product', 'cleanup'):
                    getattr(store,name).side_effect = lambda n=name: calls.append(n)
                if failure == 'restore': store.restore_product.side_effect = ValueError('restore')
                business = Mock(side_effect=ValueError('business') if failure == 'business' else lambda *a,**k: calls.append('business'))
                reader = Mock(); reader.verify_retained_backup.side_effect = ValueError('read') if failure == 'read' else lambda *a,**k: calls.append('read')
                with patch.object(lab, 'backup_store', return_value=store), patch.object(lab, 'business', business), \
                        patch.object(lab, 'load_helper', return_value=reader), redirect_stdout(io.StringIO()):
                    if failure == 'none':
                        lab.backup_suite(123, 'uninstall_backup')
                        self.assertEqual(calls, ['prepare','qualify','business','restore_product','read','cleanup'])
                        business.assert_called_once_with('uninstall_backup', 123, backup_configuration={'fixture': True})
                    else:
                        with self.assertRaises(ValueError): lab.backup_suite(123, 'uninstall_backup')
                        self.assertEqual(calls[-1], 'cleanup')
                    if failure in ('business','restore'): reader.verify_retained_backup.assert_not_called()

    def test_postuninstall_readback_requires_native_proof_and_never_reads_removed_custom_apis(self):
        for fault in ('none', 'receipt', 'namespace', 'crd'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                lab, journal, store, target, resources = self.fixture(directory)
                with redirect_stdout(io.StringIO()): helper.retain_backup(lab, journal)
                lab.owner.return_value['native_uninstalled'] = fault != 'receipt'
                lab.core_identity = Mock()
                def get(kind, *args, **kwargs):
                    if kind == 'namespace': return {'metadata': {'uid': 'remaining'}} if fault == 'namespace' else None
                    if kind == 'crds': return {'items': [{'metadata': {'name': 'volumes.longhorn.io'}}] if fault == 'crd' else []}
                    raise AssertionError('post-uninstall must not read custom APIs: ' + kind)
                lab.get.side_effect = get
                with redirect_stdout(io.StringIO()):
                    if fault == 'none':
                        helper.verify_retained_backup(lab, phase='uninstalled')
                        lab.core_identity.assert_called_once()
                    else:
                        with self.assertRaises(ValueError): helper.verify_retained_backup(lab, phase='uninstalled')
                        lab.core_identity.assert_not_called()

    def test_full_module_stops_at_failed_nonempty_guard_or_retention(self):
        for fault in ('none', 'refusal', 'retention'):
            calls = []
            def business(*args):
                calls.append(('refusal', args))
                if fault == 'refusal': raise ValueError('refusal')
            def backup(*args, **kwargs):
                calls.append(('retention', args, kwargs))
                if fault == 'retention': raise ValueError('retention')
            lab = SimpleNamespace(business=Mock(side_effect=business), backup_suite=Mock(side_effect=backup))
            with self.subTest(fault=fault), redirect_stdout(io.StringIO()) as output:
                if fault == 'none':
                    helper.uninstall(lab, 123)
                    self.assertEqual(calls, [('refusal', ('uninstall_refusal', 123)),
                        ('retention', (123, 'uninstall_backup'), {'uninstall': True})])
                    self.assertIn('LONGHORN_UNINSTALL_BUSINESS_PASS', output.getvalue())
                else:
                    with self.assertRaises(ValueError): helper.uninstall(lab, 123)
                    self.assertNotIn('_PASS', output.getvalue())
                    if fault == 'refusal': lab.backup_suite.assert_not_called()

    def test_native_removal_follows_binding_and_transport_cleanup_and_selects_exact_store_cleanup_mode(self):
        spec = importlib.util.spec_from_file_location('uninstall_order_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        for fault in ('none', 'transport', 'native', 'readback'):
            with self.subTest(fault=fault), patch.dict(sys.modules, {lab.__name__: lab}):
                calls = []; store = Mock(); store.product_configuration.return_value = {'fixture': True}
                for name in ('prepare', 'qualify', 'restore_product', 'cleanup_transport'):
                    getattr(store,name).side_effect = lambda n=name: calls.append(n)
                store.cleanup.side_effect = lambda **kwargs: calls.append(('cleanup', kwargs))
                if fault == 'transport': store.cleanup_transport.side_effect = ValueError('transport')
                reader = Mock()
                def read(*args, phase):
                    calls.append(phase)
                    if fault == 'readback' and phase == 'uninstalled': raise ValueError('readback')
                reader.verify_retained_backup.side_effect = read
                def native(*args):
                    calls.append('native')
                    if fault == 'native': raise ValueError('native')
                with patch.object(lab, 'backup_store', return_value=store), patch.object(lab, 'business'), \
                        patch.object(lab, 'recover_installation', side_effect=native), \
                        patch.object(lab, 'load_helper', return_value=reader), redirect_stdout(io.StringIO()) as output:
                    if fault == 'none':
                        lab.backup_suite(123, 'uninstall_backup', uninstall=True)
                        self.assertEqual(calls, ['prepare','qualify','restore_product','source-removed',
                            'cleanup_transport','native','uninstalled',('cleanup',{'after_uninstall':True})])
                        self.assertIn('LONGHORN_UNINSTALL_EXTERNAL_BACKUP_PRESERVED_PASS', output.getvalue())
                    else:
                        with self.assertRaises(ValueError): lab.backup_suite(123, 'uninstall_backup', uninstall=True)
                        self.assertNotIn('LONGHORN_UNINSTALL_EXTERNAL_BACKUP_PRESERVED_PASS', output.getvalue())
                        self.assertEqual(calls[-1], ('cleanup',{'after_uninstall':True} if fault == 'readback' else {}))
                        if fault == 'transport': self.assertNotIn('native', calls)
                        if fault == 'native': self.assertNotIn('uninstalled', calls)

    def test_external_fixture_cleanup_after_native_uninstall_refuses_remaining_consumers_and_wrong_ownership(self):
        for fault in ('none', 'binding', 'transport', 'state', 'namespace', 'crd', 'run', 'intent'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = {'run_id':'run','cluster_uid':'cluster','native_uninstalled':fault != 'state'}
                def get(kind, *args, **kwargs):
                    if kind == 'namespace': return {'metadata':{'uid':'remaining'}} if fault == 'namespace' else None
                    if kind == 'crds': return {'items':[{'metadata':{'name':'volumes.longhorn.io'}}] if fault == 'crd' else []}
                    raise AssertionError('missing custom API must not be queried')
                lab = SimpleNamespace(DIRECTORY=root, owner=Mock(return_value=state), get=Mock(side_effect=get),
                                      core_identity=Mock(), execute=Mock())
                store = backups.Store(lab); store.directory.mkdir(); info = store.directory.stat()
                row = {'run_id':'foreign' if fault == 'run' else 'run','cluster_uid':'cluster',
                       'name':'longhorn-backup-run','bucket':'longhorn-backup-run',
                       'directory_identity':[info.st_dev,info.st_ino]}
                if fault in ('binding','transport'): row['product_binding' if fault == 'binding' else 'transport']={'uid':'consumer'}
                store.record.write_text(json.dumps(row)); original=store.directory/'backup.cfg';original.write_text('confirmed')
                with patch.object(store, 'restore_product', side_effect=AssertionError('custom API is removed')), \
                        patch.object(store, 'inspect', return_value=None), redirect_stdout(io.StringIO()):
                    if fault == 'none':
                        store.cleanup(after_uninstall=True)
                        self.assertFalse(store.record.exists()); self.assertFalse(store.directory.exists())
                        lab.core_identity.assert_called_once()
                    else:
                        with self.assertRaises(ValueError): store.cleanup(after_uninstall='yes' if fault == 'intent' else True)
                        self.assertTrue(store.record.exists());self.assertEqual(original.read_text(),'confirmed')
                lab.execute.assert_not_called()
