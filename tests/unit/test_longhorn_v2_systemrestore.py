import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('restore_contract', ROOT/'tests/helpers/longhorn-v2-systemrestore.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


def fixture():
    backup_url = 's3://fixture@us-east-1/?backup=backup&volume=source'
    bundle_url = gate.system_url('s3://fixture@us-east-1/', 'system-probe')
    original, current = {}, {}
    for kind, name, ns in (('Volume', 'source', 'longhorn-system'),
                            ('PersistentVolume', 'pv', ''), ('PersistentVolumeClaim', 'data', 'business')):
        original[kind] = {'kind':kind, 'metadata':{'name':name, 'namespace':ns, 'uid':'old-'+kind},
                          'spec':{'storageClassName':'longhorn'}}
        current[kind] = {'kind':kind, 'metadata':{'name':name, 'namespace':ns, 'uid':'new-'+kind,
            'creationTimestamp':'2026-10-20T10:01:00Z', 'annotations':{
                gate.RESTORED:bundle_url, gate.RESTORED_AT:'2026-10-20T10:00:30Z'}},
            'spec':{'storageClassName':'longhorn'}}
    original['PersistentVolume']['spec']['capacity'] = {'storage':'2Gi'}
    current['PersistentVolume']['spec']['capacity'] = {'storage':'2Gi'}
    original['PersistentVolumeClaim']['spec']['resources'] = {'requests':{'storage':'2Gi'}}
    current['PersistentVolumeClaim']['spec']['resources'] = {'requests':{'storage':'2Gi'}}
    original['PersistentVolumeClaim']['status'] = {'phase':'Bound', 'capacity':{'storage':'2Gi'}}
    current['Volume']['spec'].update(dataEngine='v2', size=str(2*1024**3), numberOfReplicas=3, fromBackup=backup_url)
    current['Volume']['metadata']['annotations'][gate.RESTORED_BACKUP] = backup_url
    current['Volume']['status'] = {'restoreRequired':False, 'conditions':None}
    current['PersistentVolume']['spec'].update(persistentVolumeReclaimPolicy='Retain',
        csi={'driver':'driver.longhorn.io', 'volumeHandle':'source'},
        claimRef={'uid':'new-PersistentVolumeClaim', 'name':'data', 'namespace':'business'})
    current['PersistentVolumeClaim']['spec']['volumeName'] = 'pv'
    current['PersistentVolumeClaim']['status'] = {'phase':'Bound', 'capacity':{'storage':'2Gi'}}
    request = {'metadata':{'name':'restore', 'uid':'restore-uid', 'creationTimestamp':'2026-10-20T10:00:00Z'},
               'status':{'state':'Completed'}}
    return original, current, request, backup_url, bundle_url


class ReconstructionContracts(unittest.TestCase):
    def test_explicit_v1_single_node_target_and_v2_defaults_keep_exact_engine_and_replica_contract(self):
        for engine,count in (('v1',1),('v1',3),('v2',3)):
            original,current,request,backup,bundle=fixture()
            current['Volume']['spec'].update(dataEngine=engine,numberOfReplicas=count)
            expected={kind:obj['metadata']['uid'] for kind,obj in current.items()}
            self.assertEqual(expected,gate.validate_reconstruction(original,current,request,backup,bundle,data_engine=engine,replicas=count))
            self.assertEqual(current,gate.reconstruction_ready(original,current,request,backup,bundle,data_engine=engine,replicas=count))
            with self.assertRaises(ValueError):gate.validate_reconstruction(original,current,request,backup,bundle,data_engine='v2' if engine=='v1' else 'v1',replicas=count)
            with self.assertRaises(ValueError):gate.validate_reconstruction(original,current,request,backup,bundle,data_engine=engine,replicas=count+1)
            if (engine,count)==('v2',3):self.assertEqual(expected,gate.validate_reconstruction(original,current,request,backup,bundle))
            else:
                with self.assertRaises(ValueError):gate.validate_reconstruction(original,current,request,backup,bundle)
        for engine,count in (('other',3),('v1',0),('v1',True)):
            with self.assertRaisesRegex(ValueError,'unsupported'):
                gate.validate_reconstruction(*fixture(),data_engine=engine,replicas=count)

    def test_completed_rollout_waits_for_absence_pending_binding_and_restore_convergence(self):
        original,current,request,backup,bundle=fixture()
        self.assertEqual(gate.reconstruction_ready(original,current,request,backup,bundle),current)
        for kind in current:
            pending=copy.deepcopy(current);pending[kind]=None
            self.assertIsNone(gate.reconstruction_ready(original,pending,request,backup,bundle))
        pending=copy.deepcopy(current);pending['PersistentVolumeClaim']['status']['phase']='Pending'
        self.assertIsNone(gate.reconstruction_ready(original,pending,request,backup,bundle))
        pending=copy.deepcopy(current);pending['PersistentVolume']['spec']['claimRef']=None
        self.assertIsNone(gate.reconstruction_ready(original,pending,request,backup,bundle))
        pending=copy.deepcopy(current);pending['Volume']['status']['restoreRequired']=True
        self.assertIsNone(gate.reconstruction_ready(original,pending,request,backup,bundle))
        wrong=copy.deepcopy(current);wrong['PersistentVolume']['spec']['claimRef']['uid']='foreign'
        with self.assertRaises(ValueError):gate.reconstruction_ready(original,wrong,request,backup,bundle)

    def test_new_uids_original_namespace_exact_binding_and_nullable_restore_conditions(self):
        original, current, request, backup, bundle = fixture()
        self.assertEqual(gate.validate_reconstruction(original, current, request, backup, bundle),
                         {k:o['metadata']['uid'] for k,o in current.items()})
        current['Volume']['status']['conditions'] = [{'type':'Restore', 'status':'False'}]
        gate.validate_reconstruction(original, current, request, backup, bundle)

    def test_each_old_uid_same_name_or_wrong_creation_boundary_is_rejected(self):
        original, current, request, backup, bundle = fixture()
        for kind in current:
            for field, value in (('uid',original[kind]['metadata']['uid']), ('name','foreign'),
                                  ('namespace','foreign'), ('creationTimestamp','2026-10-20T09:59:59Z')):
                changed = copy.deepcopy(current); changed[kind]['metadata'][field] = value
                with self.subTest(kind=kind, field=field), self.assertRaises(ValueError):
                    gate.validate_reconstruction(original, changed, request, backup, bundle)

    def test_rollout_provenance_and_independent_latest_backup_cannot_replace_packaged_point(self):
        original, current, request, backup, bundle = fixture()
        for kind in current:
            for field,value in ((gate.RESTORED,'foreign-url'), (gate.RESTORED_AT,''),
                                (gate.RESTORED_AT,'2026-10-20T10:00:35Z')):
                changed=copy.deepcopy(current); changed[kind]['metadata']['annotations'][field]=value
                with self.subTest(kind=kind,field=field),self.assertRaises(ValueError):
                    gate.validate_reconstruction(original,changed,request,backup,bundle)
        changed=copy.deepcopy(current); changed['Volume']['spec']['fromBackup']=backup+'-later'
        with self.assertRaises(ValueError):gate.validate_reconstruction(original,changed,request,backup,bundle)

    def test_every_material_binding_restore_state_and_engine_constraint_is_checked(self):
        original, current, request, backup, bundle=fixture()
        cases=[('Volume','spec','dataEngine','v1'), ('Volume','spec','size','1073741824'),
               ('Volume','spec','numberOfReplicas',1), ('Volume','status','restoreRequired',True),
               ('Volume','status','conditions',[{'type':'Restore','status':'True'}]),
               ('PersistentVolume','spec','persistentVolumeReclaimPolicy','Delete'),
               ('PersistentVolume','spec','claimRef',{'uid':'old-PersistentVolumeClaim'}),
               ('PersistentVolume','spec','csi',{'driver':'foreign','volumeHandle':'source'}),
               ('PersistentVolumeClaim','spec','volumeName','foreign'),
               ('PersistentVolumeClaim','spec','storageClassName','foreign'),
               ('PersistentVolume','spec','capacity',{'storage':'1Gi'}),
               ('PersistentVolumeClaim','status','capacity',{'storage':'1Gi'}),
               ('PersistentVolumeClaim','spec','resources',{'requests':{'storage':'1Gi'}}),
               ('PersistentVolumeClaim','status','phase','Pending')]
        for kind,section,field,value in cases:
            changed=copy.deepcopy(current); changed[kind][section][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                gate.validate_reconstruction(original,changed,request,backup,bundle)
        request['status']['state']='InProgress'
        with self.assertRaises(ValueError):gate.validate_reconstruction(original,current,request,backup,bundle)

    def test_system_url_matches_pinned_path_and_preserves_target_context(self):
        self.assertEqual(gate.system_url('s3://bucket@region/customer/', 'backup'),
                         's3://bucket@region/customer/backupstore/system-backups/v1.12.1/backup')
        with self.assertRaises(ValueError):gate.timestamp('2026-10-20T10:00:00')

    def test_official_cleaned_annotation_is_exact_not_a_loose_url_comparison(self):
        original,current,request,backup,bundle = fixture()
        self.assertFalse(bundle.endswith('/'))
        gate.validate_reconstruction(original,current,request,backup,bundle)
        for kind in current:
            changed = copy.deepcopy(current)
            changed[kind]['metadata']['annotations'][gate.RESTORED] = bundle+'/'
            with self.assertRaises(ValueError):
                gate.validate_reconstruction(original,changed,request,backup,bundle)


class RolloutContracts(unittest.TestCase):
    def fixture(self):
        request=fixture()[2]
        job={'metadata':{'name':'longhorn-system-rollout-restore','uid':'job-uid','ownerReferences':[
            {'kind':'SystemRestore','name':'restore','uid':'restore-uid','controller':True}]},
             'status':{'succeeded':1,'conditions':[{'type':'Complete','status':'True'}]}}
        pod={'metadata':{'uid':'pod-uid','ownerReferences':[
            {'kind':'Job','name':job['metadata']['name'],'uid':'job-uid','controller':True}]},
             'status':{'phase':'Succeeded','containerStatuses':[{'state':{'terminated':{'exitCode':0}}}]}}
        return request,job,[pod]

    def test_success_is_bound_to_both_uid_edges_and_completed_pod(self):
        request,job,pods=self.fixture()
        self.assertEqual(gate.validate_rollout(request,job,pods),{'job_uid':'job-uid','pod_uids':['pod-uid']})
        for edge in ('job','pod'):
            for field,value in (('uid','foreign'),('name','foreign'),('kind','foreign'),('controller',False)):
                changed_job=copy.deepcopy(job); changed_pods=copy.deepcopy(pods)
                owner=(changed_job if edge=='job' else changed_pods[0])['metadata']['ownerReferences'][0]
                owner[field]=value
                with self.subTest(edge=edge,field=field),self.assertRaises(ValueError):
                    gate.validate_rollout(request,changed_job,changed_pods)

    def test_missing_or_failed_workloads_do_not_pass_on_request_completed(self):
        request,job,pods=self.fixture()
        for changed_job,changed_pods in ((None,pods),(job,[]),
                (dict(job,status={'succeeded':0}),pods),
                (job,[dict(pods[0],status={'phase':'Running'})]),
                (job,[dict(pods[0],status={'phase':'Succeeded','containerStatuses':[]})]),
                (job,[dict(pods[0],status={'phase':'Succeeded','containerStatuses':[{'state':{'terminated':{'exitCode':1}}}]})])):
            with self.subTest(job=changed_job is None),self.assertRaises(ValueError):
                gate.validate_rollout(request,changed_job,changed_pods)


class OwnershipAndCleanupContracts(unittest.TestCase):
    def test_protect_binds_published_manager_commit_through_shared_bundle_helper(self):
        spec = importlib.util.spec_from_file_location('backup_identity_contract', ROOT/'tests/helpers/longhorn-v2-systembackup.py')
        system = importlib.util.module_from_spec(spec); spec.loader.exec_module(system)
        for engine in ('v1', 'v2'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as folder:
                probe = self.probe(Path(folder)); lab = probe.lab
                probe.system = system; probe.data_engine = engine
                probe.save = Mock(); probe.data = Mock(side_effect=('protected-digest', 'later-digest'))
                probe.read_bundle = Mock(return_value={'verified': True})
                lab.owner = lambda: {'run_id': 'run'}
                lab.write_private = Mock(); lab.wait = lambda label, timeout, check: check()
                product = SimpleNamespace(check_request=Mock(), request_object=lambda request: request)
                lab.load_helper = lambda name: product
                objects = {}
                def requests(pid, rows, **kwargs):
                    for request in rows:
                        kind = {'snapshot': 'snapshots.longhorn.io', 'backup': 'backups.longhorn.io',
                                'system-backup': 'systembackups.longhorn.io'}[request['kind']]
                        objects[kind] = {'metadata': {'name': request['name'], 'uid': kind},
                            'status': {'state': 'Ready' if request['kind']=='system-backup' else 'Completed',
                                       'url': 's3://fixture@local/?backup=point&volume=source',
                                       'managerImage': 'registry.talkschool.cn:5000/brinnatt/longhorn-manager:v1.12.1-kubeauto.1'}}
                lab.run_requests = Mock(side_effect=requests)
                def get(kind, name=None, namespace=None):
                    if kind in objects: return objects[kind]
                    if kind == 'volumes.longhorn.io':
                        return {'status': {'lastBackup': objects['backups.longhorn.io']['metadata']['name']}}
                    if kind == 'backupvolumes.longhorn.io':
                        return {'items': [{'spec': {'volumeName': 'source', 'backupTargetName': 'default'}}]}
                    if kind == 'storageclass': return {'metadata': {'name': 'longhorn', 'uid': 'class'}}
                    return {'metadata': {'name': name, 'namespace': namespace, 'uid': kind},
                            'spec': {'persistentVolumeReclaimPolicy': 'Retain'}}
                lab.get = get; probe.current = lambda *args: get(args[0])
                probe.store.backup_receipt = Mock(return_value={'incremental': False})
                probe.bundle.identity = lambda: {'namespace_uid': 'source-namespace'}
                probe.bundle.record = Path(folder)/'bundle.json'
                row = {'namespace_uid': 'source-namespace'}
                journal = {'volume': 'source', 'volume_uid': 'volume', 'pv': 'pv',
                           'namespace': 'business', 'storage_class': 'longhorn'}
                configuration = {'longhorn_backup_target': 's3://fixture@local/'}
                probe.protect(row, journal, 12, configuration)
                self.assertEqual(row['bundle_expected']['manager_commit'],
                                 '8320238242e8c804325d9588ec1db10feb9f36b8-kubeauto.9275735a33fe')
                self.assertEqual(row['bundle_expected']['manager_image'],
                                 objects['systembackups.longhorn.io']['status']['managerImage'])
                self.assertEqual(lab.run_requests.call_count, 2)
                self.assertEqual(lab.run_requests.call_args.args[0], 12)
                self.assertEqual(lab.run_requests.call_args.kwargs['backup_configuration'], configuration)
                probe.read_bundle.assert_called_once_with(row)

    def probe(self, directory):
        system=SimpleNamespace(Probe=lambda lab,**kwargs:SimpleNamespace(identity=lambda:{'cluster_uid':'cluster'}))
        lab=SimpleNamespace(DIRECTORY=directory,load_helper=lambda name:system,backup_store=lambda:Mock())
        return gate.Probe(lab)

    def test_historical_uids_authorize_events_only_not_live_objects_or_other_events(self):
        with tempfile.TemporaryDirectory() as folder:
            probe=self.probe(Path(folder)); lab=probe.lab
            journal={'namespace':'business','namespace_uid':'namespace-uid','objects':{'PersistentVolumeClaim/data':'new-claim'},'defaults':[]}
            row={'original':{'PersistentVolumeClaim':{'metadata':{'uid':'old-claim'}}}}
            event={'kind':'Event','metadata':{'uid':'event'},'involvedObject':{'uid':'old-claim'}}
            lab.kubectl=lambda *args:SimpleNamespace(stdout='events pvc')
            def objects(kind, name='', namespace=''):
                if kind=='namespace':return {'metadata':{'uid':'namespace-uid'}}
                if kind=='events':return {'items':[event]}
                return {'items':[]}
            lab.get=objects; probe.graph(journal,row)
            event['involvedObject']['uid']='foreign'
            with self.assertRaises(ValueError):probe.graph(journal,row)
            event['involvedObject']['uid']='old-claim'; event['kind']='PersistentVolumeClaim'
            with self.assertRaises(ValueError):probe.graph(journal,row)
            lab.kubectl=lambda *args:SimpleNamespace(stdout='')
            with self.assertRaisesRegex(ValueError,'empty'):probe.graph(journal,row)

    def test_uncertain_or_unverified_restore_preserves_all_materials(self):
        with tempfile.TemporaryDirectory() as folder:
            probe=self.probe(Path(folder)); probe.journal=Mock()
            probe.record.write_text('{}')
            for phase in ('source','deleting-source','source-deleted','restoring','reconstructed'):
                probe.journal.return_value={'phase':phase}
                with self.subTest(phase=phase),self.assertRaisesRegex(ValueError,'preserve'):
                    probe.cleanup()
                probe.store.cleanup.assert_not_called()
                self.assertTrue(probe.record.exists())

    def test_replaced_identity_and_missing_ledger_block_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            probe=self.probe(Path(folder)); probe.lab.get=Mock(return_value={'metadata':{'uid':'replacement'}})
            with self.assertRaisesRegex(ValueError,'replaced'):probe.current('Volume','source','longhorn-system','original')
            probe.business_record.write_text('{}')
            with self.assertRaisesRegex(ValueError,'lost'):probe.cleanup()
            probe.store.cleanup.assert_not_called()

    def test_delete_uses_fresh_resource_version_and_original_uid(self):
        with tempfile.TemporaryDirectory() as folder:
            probe=self.probe(Path(folder)); obj={'kind':'Pod','metadata':{'name':'client','namespace':'business','uid':'uid','resourceVersion':'old'}}
            fresh=copy.deepcopy(obj); fresh['metadata']['resourceVersion']='new'
            probe.lab.get=Mock(side_effect=[fresh,None]); probe.lab.remove=Mock()
            probe.lab.wait=lambda name,timeout,check:check()
            probe.remove(obj); probe.lab.remove.assert_called_once_with(fresh)

    def test_shared_comparison_keeps_uid_specs_labels_and_secret_digest(self):
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'key','uid':'uid','labels':{'owner':'original'},'annotations':{}},
             'data':{'key':'confidential'}}
        before=gate.resource_signature(obj); self.assertNotIn('confidential',json.dumps(before))
        changed=copy.deepcopy(obj);changed['metadata']['annotations'][gate.RESTORED]='url'
        self.assertEqual(before,gate.resource_signature(changed))
        for mutation in ('uid','label','data'):
            changed=copy.deepcopy(obj)
            if mutation=='uid':changed['metadata']['uid']='other'
            elif mutation=='label':changed['metadata']['labels']['owner']='other'
            else:changed['data']['key']='other'
            self.assertNotEqual(before,gate.resource_signature(changed))

    def test_recovery_binds_all_archived_ledgers_and_new_native_uids_before_replay(self):
        evidence=json.loads((ROOT/'tests/evidence/longhorn-v2-systemrestore-url-first-failure-20261020.json').read_text())
        with tempfile.TemporaryDirectory() as folder:
            probe=self.probe(Path(folder)); probe.bundle.record=Path(folder)/'longhorn-systembackup-qualification.json'
            ledgers=copy.deepcopy(evidence['journals'])
            probe.journal=lambda:ledgers[probe.record.name]
            probe.bundle.journal=lambda:ledgers[probe.bundle.record.name]
            probe.business_record.write_text(json.dumps(ledgers[probe.business_record.name]))
            probe.shared=lambda:evidence['journals'][probe.record.name]['shared']
            def get(kind,name='',namespace=''):
                if kind=='systemrestores.longhorn.io':
                    return copy.deepcopy(evidence['request']) if name else {'items':[copy.deepcopy(evidence['request'])]}
                return copy.deepcopy(evidence['native'][kind])
            probe.lab.get=get
            old_base=gate.BASE; gate.BASE=ROOT
            try:
                probe.recovery_state()
                for name in ledgers:
                    old=copy.deepcopy(ledgers[name]);ledgers[name]['foreign']=True
                    if name==probe.business_record.name:
                        probe.business_record.write_text(json.dumps(ledgers[name]))
                    with self.subTest(journal=name),self.assertRaises(ValueError):probe.recovery_state()
                    ledgers[name]=old
                    probe.business_record.write_text(json.dumps(ledgers[probe.business_record.name]))
                for kind in evidence['native']:
                    def replaced(k,name='',namespace=''):
                        obj=get(k,name,namespace)
                        if k==kind:obj['metadata']['uid']='replacement'
                        return obj
                    probe.lab.get=replaced
                    with self.subTest(kind=kind),self.assertRaises(ValueError):probe.recovery_state()
            finally:
                gate.BASE=old_base

    def test_fixed_recovery_replays_original_product_request_then_checks_data_without_new_source(self):
        import inspect
        recovery=inspect.getsource(gate.Probe.recover)
        self.assertLess(recovery.index('self.recovery_state()'),recovery.index('self.lab.run_requests('))
        self.assertLess(recovery.index('self.read_bundle('),recovery.index('self.lab.run_requests('))
        self.assertIn("[row['restore_request']]",recovery)
        self.assertIn('self.verify_restored(row, journal, current)',recovery)
        for forbidden in ('self.create_source(', 'self.delete_source(', 'self.store.product_configuration('):
            self.assertNotIn(forbidden,recovery)

    def test_fixed_runner_orders_static_lease_product_reconstruction_and_cleanup(self):
        shell=(ROOT/'tests/helpers/longhorn-v2-systemrestore.sh').read_text()
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertEqual(runner.count('v2-systemrestore-qualification|'),2)
        self.assertIn('--longhorn-v2-systemrestore-qualification)',runner)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-systemrestore.sh" "$MODE"',live)
        for marker in ('${marker}_SCOPED_CLEAN_PASS','${marker}_PASS'):
            self.assertIn(marker,shell)
        self.assertLess(shell.index('"$RESTORE" cleanup'),shell.index('"${marker}_PASS"'))
        self.assertIn('trap restore_exit EXIT',shell)
        self.assertIn('recovery_material=retained',shell)
        self.assertIn('"$RESTORE" verify --log',shell)
        self.assertNotIn('helm install',shell)


class LogContracts(unittest.TestCase):
    def test_missing_repeated_or_same_uid_data_scope_cannot_pass(self):
        original,current,request,backup,bundle=fixture()
        row={'old_uids':{k:o['metadata']['uid'] for k,o in original.items()},
             'new_uids':{k:o['metadata']['uid'] for k,o in current.items()},
             'backup_digest':'a'*64,'post_backup_digest':'b'*64,'recovered_digest':'a'*64,
             'recovered_bytes':9437184,'replica_nodes':['n1','n2','n3'],'external_objects':3,
             'restore_uid':'restore','rollout':{'job_uid':'job','pod_uids':['pod']},
             'source_objects_absent_before_restore':3,'shared_declarations_unchanged':True,'independent_dr_proven':False}
        line='LONGHORN_V2_SYSTEMRESTORE_OBSERVED '+json.dumps(row)
        gate.verify_log(line)
        for bad in ('',line+'\n'+line):
            with self.assertRaises(ValueError):gate.verify_log(bad)
        for key,value in (('recovered_digest','b'*64),('independent_dr_proven',True),
                          ('replica_nodes',['n1','n1','n2']),('source_objects_absent_before_restore',0)):
            changed=copy.deepcopy(row);changed[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                gate.verify_log('LONGHORN_V2_SYSTEMRESTORE_OBSERVED '+json.dumps(changed))

class HelmStorageContracts(unittest.TestCase):
    def release(self, revision=6, status='deployed'):
        release={'name':'longhorn','namespace':gate.NAMESPACE,'version':revision,
                 'chart':{'metadata':{'name':'longhorn','version':'1.12.1'}},
                 'config':{'defaultSettings':{'v2DataEngine':True}},'manifest':'kind: Deployment',
                 'hooks':[{'name':'pre-upgrade','manifest':'kind: Job','last_run':{'phase':'Succeeded'}}],
                 'info':{'status':status,'last_deployed':'old-time','notes':'installed'}}
        return release

    def secret(self, release):
        import base64,gzip
        labels={'owner':'helm','name':'longhorn','version':str(release['version']),'status':release['info']['status']}
        data=base64.b64encode(base64.b64encode(gzip.compress(json.dumps(release).encode()))).decode()
        return {'kind':'Secret','metadata':{'uid':'helm-'+labels['version'],'namespace':gate.NAMESPACE,
                'name':'sh.helm.release.v1.longhorn.v'+labels['version'],'labels':labels},
                'type':'helm.sh/release.v1','data':{'release':data}}

    def test_reassertion_revision_status_time_and_hook_execution_preserve_profile(self):
        before=self.release();after=copy.deepcopy(before)
        before['info']['status']='superseded'
        after['version']=7;after['info']['last_deployed']='new-time';after['hooks'][0]['last_run']['phase']='Unknown'
        self.assertEqual(gate.helm_release_profile(self.secret(before))[1],gate.helm_release_profile(self.secret(after))[1])
        self.assertEqual(gate.helm_release_profile(self.secret(after))[0],'deployed')

    def test_chart_values_manifest_and_hook_declarations_remain_sensitive(self):
        before=self.release();expected=gate.helm_release_profile(self.secret(before))[1]
        for field in ('chart','config','manifest','hooks'):
            changed=copy.deepcopy(before)
            if field=='chart':changed[field]['metadata']['version']='1.12.0'
            elif field=='config':changed[field]['defaultSettings']['v2DataEngine']=False
            elif field=='manifest':changed[field]='foreign manifest'
            else:changed[field][0]['manifest']='foreign hook'
            with self.subTest(field=field):self.assertNotEqual(expected,gate.helm_release_profile(self.secret(changed))[1])

    def test_unowned_or_malformed_storage_cannot_hide_as_helm(self):
        for field in ('uid','namespace','name','owner','release_name','version','status','type','payload','pending','decoded_name'):
            obj=self.secret(self.release())
            if field in ('uid','namespace','name'):obj['metadata'][field]='' if field=='uid' else 'foreign'
            elif field=='owner':obj['metadata']['labels']['owner']='foreign'
            elif field=='release_name':obj['metadata']['labels']['name']='foreign'
            elif field in ('version','status'):obj['metadata']['labels'][field]='foreign'
            elif field=='type':obj['type']='Opaque'
            elif field=='payload':obj['data']['release']='invalid'
            elif field=='pending':obj=self.secret(self.release(status='pending-upgrade'))
            else:
                release=self.release();release['name']='foreign';obj=self.secret(release)
            with self.subTest(field=field),self.assertRaises((ValueError,KeyError,OSError)):
                gate.helm_release_profile(obj)

    def test_shared_keeps_real_key_uid_and_digest_and_requires_one_deployed_release(self):
        probe=gate.Probe.__new__(gate.Probe)
        key={'kind':'Secret','metadata':{'name':'real-key','uid':'key-uid','namespace':gate.NAMESPACE},'data':{'key':'secret'}}
        records=[self.secret(self.release()),key]
        probe.lab=SimpleNamespace(get=lambda kind,namespace='':{'items':records if kind=='secrets' else []},shared_snapshot_identity=lambda:{'identity':'snapshot'})
        before=probe.shared()
        records[0]=self.secret(self.release(7))
        self.assertEqual(before,probe.shared())
        for field in ('uid','data'):
            changed=copy.deepcopy(key)
            if field=='uid':changed['metadata']['uid']='foreign'
            else:changed['data']['key']='foreign'
            records[1]=changed
            self.assertNotEqual(before,probe.shared())
        records[1]=key
        for records_value in ([key],[self.secret(self.release()),self.secret(self.release(7)),key]):
            records[:]=records_value
            with self.assertRaisesRegex(ValueError,'one deployed'):probe.shared()

    def test_archived_recovery_checks_journals_shared_native_uids_namespace_and_consumers(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'tests/evidence').mkdir(parents=True)
            original,current,request,backup,bundle=fixture();request['spec']={'source':'bundle'}
            row={'phase':'reconstructed','restore':request,'original':original,'reconstructed':current,'backup_url':backup,'bundle_url':bundle}
            journal={'namespace':'business','namespace_uid':'namespace-uid'}
            probe=gate.Probe.__new__(gate.Probe);probe.data_engine='v2';probe.replicas=3
            probe.record=root/'restore.json';probe.record.write_text(json.dumps(row))
            probe.business_record=root/'business.json';probe.business_record.write_text(json.dumps(journal))
            probe.bundle=SimpleNamespace(record=root/'bundle.json');probe.bundle.record.write_text('{}')
            shared={'objects':{'key':'exact'},'helm_profile_sha256':'a'*64}
            probe.shared=lambda:copy.deepcopy(shared);probe.journal=lambda:copy.deepcopy(row);probe.graph=Mock()
            state={'namespace_uid':'namespace-uid','consumer':False,'native':copy.deepcopy(current)}
            def get(kind,name='',namespace=''):
                if kind=='namespace':return {'metadata':{'uid':state['namespace_uid']}}
                if kind=='systemrestores.longhorn.io':return copy.deepcopy(request) if name else {'items':[request]+([{'metadata':{'uid':'foreign'}}] if state['consumer'] else [])}
                return copy.deepcopy(state['native'][kind])
            probe.current=lambda kind,name,namespace,uid:get(kind,name,namespace)
            probe.lab=SimpleNamespace(get=get)
            evidence={'status':'failed','classification':'test-gate','rc':1,'defect':'helm-release-bookkeeping',
                      'journals_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (probe.record,probe.business_record,probe.bundle.record)},
                      'shared_sha256':hashlib.sha256(json.dumps(shared,sort_keys=True).encode()).hexdigest(),'helm_profile_sha256':'a'*64}
            path=root/'tests/evidence/longhorn-v2-systemrestore-helm-first-failure-20261025.json';path.write_text(json.dumps(evidence))
            old_base=gate.BASE;gate.BASE=root
            try:
                probe.helm_recovery_state();probe.graph.assert_called_once()
                for p in (probe.record,probe.business_record,probe.bundle.record):
                    raw=p.read_bytes();p.write_bytes(raw+b' ')
                    with self.subTest(journal=p.name),self.assertRaisesRegex(ValueError,'archived journals'):probe.helm_recovery_state()
                    p.write_bytes(raw)
                shared['objects']['key']='foreign'
                with self.assertRaisesRegex(ValueError,'shared declarations'):probe.helm_recovery_state()
                shared['objects']['key']='exact'
                state['namespace_uid']='foreign'
                with self.assertRaisesRegex(ValueError,'shared declarations'):probe.helm_recovery_state()
                state['namespace_uid']='namespace-uid';state['consumer']=True
                with self.assertRaisesRegex(ValueError,'another request'):probe.helm_recovery_state()
                state['consumer']=False
                for kind in current:
                    state['native'][kind]['metadata']['uid']='foreign'
                    with self.subTest(kind=kind),self.assertRaises(ValueError):probe.helm_recovery_state()
                    state['native']=copy.deepcopy(current)
            finally:gate.BASE=old_base
