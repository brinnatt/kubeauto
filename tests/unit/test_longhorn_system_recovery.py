"""Independent source/target restore semantics, not the old single-site probe."""
from contextlib import contextmanager, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('system_recovery_contract',ROOT/'tests/helpers/longhorn-system-recovery.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)
URL='s3://own/backupstore/system-backups/v1.12.1/system-own'


class SystemRecoveryContracts(unittest.TestCase):
    def test_cross_cluster_manifest_drops_source_uid_status_and_server_metadata(self):
        source={'apiVersion':'v1','kind':'PersistentVolume','metadata':{'uid':'source-uid','name':'volume',
            'resourceVersion':'7','annotations':{'foreign':'source'},'ownerReferences':[{'uid':'source'}]},
            'spec':{'capacity':{'storage':'2Gi'}},'status':{'phase':'Bound'}}
        actual=MODULE.manifest(source)
        self.assertEqual({'name':'volume'},actual['metadata']);self.assertNotIn('status',actual)
        actual['spec']['capacity']['storage']='4Gi';self.assertEqual('2Gi',source['spec']['capacity']['storage'])

    def test_bound_store_configuration_is_reused_without_creating_another_secret(self):
        holder=SimpleNamespace(receipt=Mock(return_value={'product_binding':{'fixture_spec':{'backupTargetURL':'s3://own'},'secret_name':'own-secret'}}),product_configuration=Mock())
        self.assertEqual({'longhorn_backup_target':'s3://own','longhorn_backup_secret':'own-secret'},MODULE.configuration(holder))
        holder.product_configuration.assert_not_called()

    def graph(self):
        before={kind:{'apiVersion':'v1','kind':kind,'metadata':{'name':kind.lower(),'uid':'target-'+kind,
            'namespace':'longhorn-system' if kind=='Volume' else 'business' if kind=='PersistentVolumeClaim' else ''},
            'spec':{'owned':'target'}} for kind in ('Volume','PersistentVolume','PersistentVolumeClaim')}
        current=copy.deepcopy(before)
        for obj in current.values():obj['metadata']['annotations']={
            'longhorn.io/last-skipped-system-restored':URL,'longhorn.io/last-skipped-system-restored-at':'2026-10-21T10:01:00Z'}
        request={'metadata':{'creationTimestamp':'2026-10-21T10:00:00Z'}}
        return before,current,request

    def test_existing_target_preserves_own_three_uids_spec_and_current_skip_provenance(self):
        before,current,request=self.graph()
        self.assertEqual({k:v['metadata']['uid'] for k,v in before.items()},MODULE.validate_skipped(before,current,URL,request))

    def test_existing_skip_rejects_source_adoption_spec_change_stale_or_wrong_bundle(self):
        before,current,request=self.graph();original=copy.deepcopy(current)
        for defect in ('uid','spec','bundle','time','missing'):
            current=copy.deepcopy(original)
            if defect=='uid':current['Volume']['metadata']['uid']='source-volume'
            elif defect=='spec':current['PersistentVolumeClaim']['spec']['owned']='source'
            elif defect=='bundle':current['Volume']['metadata']['annotations']['longhorn.io/last-skipped-system-restored']='foreign'
            elif defect=='time':current['Volume']['metadata']['annotations']['longhorn.io/last-skipped-system-restored-at']='2026-10-20T10:01:00Z'
            else:current['PersistentVolumeClaim']=None
            with self.subTest(defect=defect),self.assertRaises(ValueError):MODULE.validate_skipped(before,current,URL,request)

    def test_source_class_uses_product_one_replica_without_rewriting_original_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);original=yaml.safe_dump({'longhorn_data_engines':['v1'],
                'longhorn_storage_classes':[{'name':'longhorn','replicas':3,'data_engine':'v1'}]})
            (path/'config.yml').write_text(original)
            storage={'metadata':{'uid':'class-uid'},'parameters':{'numberOfReplicas':'1','dataEngine':'v1'}}
            source=SimpleNamespace(DIRECTORY=path,get=Mock(side_effect=[None,storage]),run_requests=Mock(),
                load_helper=Mock(return_value=SimpleNamespace(require_owned=Mock())))
            recovery=MODULE.Recovery.__new__(MODULE.Recovery);recovery.source=source;recovery.source_pid=11
            recovery.row={};recovery.save=Mock();recovery.target_module=SimpleNamespace(STORAGE='longhorn-recovery-v1')
            recovery.source_class()
            declaration=source.run_requests.call_args.kwargs['storage_classes']
            self.assertEqual(3,declaration[0]['replicas']);self.assertEqual(1,declaration[1]['replicas'])
            self.assertEqual('v1',declaration[1]['data_engine']);self.assertEqual('class-uid',recovery.row['source_class_uid'])
            self.assertEqual(original,(path/'config.yml').read_text())

    def test_attached_refusal_uses_product_and_requires_no_created_request_and_same_data(self):
        for invalid in (False,True):
            workload=SimpleNamespace(identities=Mock(return_value={'own':'uid'}),data=Mock())
            recovery=MODULE.Recovery.__new__(MODULE.Recovery);recovery.target_pid=12
            recovery.target=SimpleNamespace(run_requests=Mock(),get=Mock(return_value={'items':[{'metadata':{'uid':'wrong'}}] if invalid else []}))
            recovery.consumer=SimpleNamespace(receipt=Mock(return_value={'product_binding':{'fixture_spec':{'backupTargetURL':'s3://own'},'secret_name':'own'}}))
            recovery.row={'bundle_expected':{'name':'system-own'},'existing_payload':'target-owned'}
            recovery.receivers={'existing':workload};recovery.save=Mock()
            with redirect_stdout(io.StringIO()):
                if invalid:
                    with self.assertRaisesRegex(ValueError,'created a request'):recovery.refuse_attached()
                    workload.data.assert_not_called()
                else:
                    recovery.refuse_attached();workload.data.assert_called_once_with('target-owned',read=True)
            self.assertEqual('system restore requires all volumes detached',recovery.target.run_requests.call_args.kwargs['expected_refusal'])

    def test_prebusiness_host_failure_cleanup_is_exact_and_refuses_later_or_foreign_state(self):
        for defect in ('none','later-intent','product-intent','config','identity','class','backup'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);recovery=MODULE.Recovery.__new__(MODULE.Recovery)
                labs={}
                for side in ('source','target'):
                    path=root/side;path.mkdir();(path/'config.yml').write_bytes(side.encode())
                    labs[side]=SimpleNamespace(DIRECTORY=path,installation_health=Mock(),get=Mock(return_value=None),
                        write_private=lambda p,row:p.write_text(json.dumps(row)))
                recovery.source,recovery.target=labs['source'],labs['target'];recovery.path=root/'journal.json';recovery.path.write_text('{}')
                recovery.row={'id':'own','stage':'preparing','source_identity':'source','target_identity':'target',
                    'config_sha256':{side:__import__('hashlib').sha256(side.encode()).hexdigest() for side in labs}}
                recovery.read=Mock();recovery.backups=Mock();recovery.target_pid=12
                recovery.target_module=SimpleNamespace(STORAGE='longhorn-recovery-v1')
                target={'stage':'host-intent'}
                recovery.target_fixture=SimpleNamespace(read=Mock(return_value=target),clean=Mock())
                recovery.upgrade=SimpleNamespace(retained_identity=Mock(side_effect=lambda lab,_:lab.DIRECTORY.name))
                gate=SimpleNamespace(healthy=Mock(),empty=Mock())
                if defect=='later-intent':recovery.row['source_class']={'name':'intent'}
                elif defect=='product-intent':target['applied_sha256']='unknown'
                elif defect=='config':(labs['source'].DIRECTORY/'config.yml').write_text('foreign')
                elif defect=='identity':recovery.row['target_identity']='foreign'
                elif defect=='class':labs['source'].get.return_value={'metadata':{'uid':'foreign'}}
                elif defect=='backup':gate.empty.side_effect=ValueError('external backup consumer')
                with patch.object(MODULE,'load',return_value=gate),redirect_stdout(io.StringIO()) as output:
                    if defect=='none':recovery.clean()
                    else:
                        with self.assertRaises(ValueError):recovery.clean()
                if defect=='none':
                    recovery.target_fixture.clean.assert_called_once_with(12)
                    self.assertFalse(recovery.path.exists());self.assertIn('business=not-started',output.getvalue())
                    self.assertNotIn('LONGHORN_SYSTEM_RESTORE_PASS',output.getvalue())
                    self.assertTrue((root/'longhorn-system-recovery-prebusiness-cleaned-own.json').exists())
                else:
                    recovery.target_fixture.clean.assert_not_called();self.assertTrue(recovery.path.exists())

    def test_unverified_recovery_never_deletes_source_or_target_material(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery=MODULE.Recovery.__new__(MODULE.Recovery);recovery.path=Path(directory)/'journal.json';recovery.path.write_text('{}')
            recovery.row={'stage':'restoring'};recovery.read=Mock();recovery.target=SimpleNamespace(remove=Mock())
            recovery.bundle=SimpleNamespace(cleanup=Mock());recovery.store=SimpleNamespace(cleanup=Mock())
            with self.assertRaisesRegex(ValueError,'retains all'):recovery.clean()
            recovery.target.remove.assert_not_called();recovery.bundle.cleanup.assert_not_called();recovery.store.cleanup.assert_not_called()

    def test_final_material_readback_must_match_original_bundle_and_backup_blocks(self):
        recovery=MODULE.Recovery.__new__(MODULE.Recovery)
        recovery.row={'original':{'Volume':{'metadata':{'name':'volume'}}},'backup_request':{'name':'backup'},
            'bundle_result':{'sha256':'bundle'},'backup_receipt':{'sha256':'blocks'}}
        for defect in ('','bundle','blocks'):
            recovery.probe=SimpleNamespace(read_bundle=Mock(return_value={'sha256':'changed' if defect=='bundle' else 'bundle'}))
            recovery.store=SimpleNamespace(backup_receipt=Mock(return_value={'sha256':'changed' if defect=='blocks' else 'blocks'}))
            with self.subTest(defect=defect):
                if defect:
                    with self.assertRaisesRegex(ValueError,'material changed'):recovery.verify_materials()
                else:
                    recovery.verify_materials();recovery.store.backup_receipt.assert_called_once_with('volume','backup',2*1024**3)

    def test_cache_retirement_binds_source_target_uids_and_original_external_receipt(self):
        recovery=MODULE.Recovery.__new__(MODULE.Recovery)
        recovery.row={'backup_request':{'name':'backup'},'original':{'Volume':{'metadata':{'name':'volume'}}},
            'backup':{'metadata':{'uid':'source-backup'}},'target_backup_uid':'target-backup','backup_receipt':{'bytes':'verified'},'backup_url':'s3://own'}
        handler=Mock();recovery.upgrade=SimpleNamespace(Upgrade=SimpleNamespace(retire_caches=handler))
        recovery.retire_caches('target','consumer','target')
        self.assertEqual('source-backup',recovery.row['points']['missing']['backup_uid'])
        self.assertEqual('target-backup',recovery.row['points']['missing']['target_backup_uid'])
        self.assertEqual({'bytes':'verified'},recovery.row['points']['missing']['receipt'])
        handler.assert_called_once_with(recovery,'target','consumer','target')

    def test_receiver_namespace_is_source_named_with_new_uid_and_creation_journal_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'business.json';workload=SimpleNamespace(path=path,save=Mock())
            lab=SimpleNamespace(get=Mock(return_value=None),kubectl=Mock(return_value=SimpleNamespace(stdout=json.dumps({'metadata':{'uid':'target-new'}}))),
                wait=Mock(return_value=['target-default']),namespace_defaults=Mock())
            recovery=MODULE.Recovery.__new__(MODULE.Recovery);recovery.target=lab;recovery.target_module=SimpleNamespace(STORAGE='longhorn-recovery-v1')
            row=recovery.namespace(workload,{'namespace':'longhorn-business-source','volume':'source-handle','pv':'source-pv'})
            self.assertEqual('target-new',row['namespace_uid']);self.assertEqual('longhorn-business-source',row['namespace'])
            self.assertEqual('source-handle',row['volume']);self.assertEqual('source-pv',row['pv'])
            self.assertEqual({},row['objects']);self.assertEqual(3,workload.save.call_count)


if __name__=='__main__':unittest.main()

class PrebusinessProductIntentContracts(unittest.TestCase):
 def test_only_exact_prebusiness_product_intent_without_native_additions_can_roll_back(self):
  import base64,hashlib
  for defect in ('none','input','original','desired','native','captured','class','source-intent','backup'):
   with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
    root=Path(directory);r=MODULE.Recovery.__new__(MODULE.Recovery);labs={}
    originals={'source':b'source','target':b'target'};desired={'dual':'engine'};applied=yaml.safe_dump(desired,sort_keys=False).encode()
    for side in originals:
     path=root/side;path.mkdir();(path/'config.yml').write_bytes(originals[side])
     labs[side]=SimpleNamespace(DIRECTORY=path,installation_health=Mock(),get=Mock(return_value=None),write_private=lambda p,row:p.write_text(json.dumps(row)))
    (labs['target'].DIRECTORY/'config.yml').write_bytes(applied)
    r.source,r.target=labs['source'],labs['target'];r.path=root/'recovery.json';r.path.write_text('{}');r.target_pid=12;r.backups=Mock()
    r.row={'id':'own','stage':'preparing','source_identity':'source','target_identity':'target','config_sha256':{s:hashlib.sha256(v).hexdigest() for s,v in originals.items()}}
    disks={'raw':'exact'}
    target={'stage':'product-intent','original_b64':base64.b64encode(originals['target']).decode(),
     'original_sha256':r.row['config_sha256']['target'],'applied_sha256':hashlib.sha256(applied).hexdigest(),'native_disks':disks}
    r.read=Mock();r.target_module=SimpleNamespace(STORAGE='recovery-class',desired=Mock(return_value=desired))
    clean=Mock(side_effect=lambda pid:(labs['target'].DIRECTORY/'config.yml').write_bytes(originals['target']))
    r.target_fixture=SimpleNamespace(read=Mock(return_value=target),clean=clean,coexist=SimpleNamespace(native_disks=Mock(return_value=disks)))
    r.upgrade=SimpleNamespace(retained_identity=Mock(side_effect=lambda lab,_:lab.DIRECTORY.name));gate=SimpleNamespace(healthy=Mock(),empty=Mock())
    if defect=='input':(labs['target'].DIRECTORY/'config.yml').write_text('foreign')
    elif defect=='original':target['original_sha256']='wrong'
    elif defect=='desired':target['applied_sha256']='wrong'
    elif defect=='native':r.target_fixture.coexist.native_disks.return_value={'filesystem':'unknown'}
    elif defect=='captured':target['v1_disk']={}
    elif defect=='class':labs['target'].get.return_value={'metadata':{'uid':'foreign'}}
    elif defect=='source-intent':r.row['source_class']={}
    elif defect=='backup':gate.empty.side_effect=ValueError('backup consumer')
    with patch.object(MODULE,'load',return_value=gate),redirect_stdout(io.StringIO()):
     if defect=='none':r.clean()
     else:
      with self.assertRaises(ValueError):r.clean()
    if defect=='none':
     clean.assert_called_once_with(12);self.assertFalse(r.path.exists())
     self.assertEqual(gate.healthy.call_count,1)
    else:clean.assert_not_called();self.assertTrue(r.path.exists())

class PreparedCheckpointContracts(unittest.TestCase):
    def test_fixture_routes_installed_refusal_without_relaxing_initial_installer(self):
        initial=Mock(side_effect=lambda *args,**kwargs: (_ for _ in ()).throw(ValueError('initial refuses')) if kwargs.get('expected_refusal') else None)
        target=SimpleNamespace(supervise_core=initial)
        with patch.object(MODULE,'load',return_value=SimpleNamespace(fixture=lambda:('source',target,'backups','shared'))),patch.object(MODULE,'supervise_target_refusal') as own:
            result=MODULE.fixture();self.assertIs(result[1],target)
            target.supervise_core(12,'longhorn-setup')
            target.supervise_core(12,'longhorn-setup',expected_refusal='expected')
            initial.assert_called_once_with(12,'longhorn-setup')
            own.assert_called_once_with(target,12,'longhorn-setup','expected')

    def test_installed_target_refusal_uses_real_kubecli_and_exact_reason(self):
        reason='system restore requires all volumes detached'
        for rc,message,valid in ((7,'LONGHORN_PRODUCT_FAIL reason='+reason,True),
                (7,'fatal: [target]: FAILED! => '+json.dumps({'stdout':'LONGHORN_PRODUCT_FAIL reason='+reason}),True),
                (0,'LONGHORN_PRODUCT_FAIL reason='+reason,False),(7,'another failure',False)):
            with self.subTest(rc=rc,message=message),tempfile.TemporaryDirectory() as directory:
                lab=SimpleNamespace(BASE=ROOT,DIRECTORY=Path(directory),CLUSTER='longhorn-v2-dr-target',
                    NODES=('192.168.47.149',),core_identity=Mock(),stop_product=Mock(),write_private=Mock())
                identity=([b'python',str(ROOT/'tests/helpers/longhorn-v2-dr-target.py').encode(),b'relay-maintain'],'start')
                lab.process_identity=Mock(return_value=identity)
                @contextmanager
                def scoped(clean):
                    try:yield
                    finally:clean()
                lab.scoped_cleanup=scoped
                def popen(command,**kwargs):
                    self.assertEqual(command[-3:],['setup','longhorn-v2-dr-target','09'])
                    kwargs['stdout'].write(message+'\n');kwargs['stdout'].flush()
                    return SimpleNamespace(poll=lambda:rc,returncode=rc)
                with patch.object(MODULE.subprocess,'Popen',side_effect=popen) as launch:
                    if valid:MODULE.supervise_target_refusal(lab,12,'longhorn-setup',reason)
                    else:
                        with self.assertRaisesRegex(ValueError,'exact expected refusal'):
                            MODULE.supervise_target_refusal(lab,12,'longhorn-setup',reason)
                launch.assert_called_once();lab.stop_product.assert_called_once()

    def test_prepared_checkpoint_reuses_only_original_materials_and_stops_before_any_operation(self):
        import hashlib
        for defect in ('','stage','restore-intent','input','bundle-uid','foreign-volume','existing-data','missing-journal'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);row={'id':'own','stage':'preparing','source_class':{'name':'own-class'},
                    'source_class_uid':'class','target_bundle_uid':'bundle','target_backup_uid':'backup',
                    'existing_payload':'target','existing_digest':'digest','backup_receipt':{},'bundle_result':{},
                    'source_latest':{'missing':'source-missing','existing':'source-existing'},
                    'bundle_expected':{'name':'bundle-name'},'backup_request':{'name':'backup-name'},'config_sha256':{}}
                r=MODULE.Recovery.__new__(MODULE.Recovery);r.row=row;r.read=Mock();r.verify_materials=Mock()
                r.refuse_attached=Mock();r.restore_target=Mock()
                labs={}
                for side in ('source','target'):
                    d=root/side;d.mkdir();data=side.encode();(d/'config.yml').write_bytes(data)
                    row['config_sha256'][side]=hashlib.sha256(data).hexdigest()
                    def get(kind,name='',namespace='',side=side):
                        if kind=='systemrestores.longhorn.io':return {'items':[]}
                        if kind=='storageclass':return {'metadata':{'uid':'class'}}
                        if kind in ('systembackups.longhorn.io','backups.longhorn.io'):
                            return {'metadata':{'uid':('changed' if defect=='bundle-uid' else 'bundle') if kind.startswith('system') else 'backup'},'status':{'state':'Ready' if kind.startswith('system') else 'Completed'}}
                        if kind=='volumes.longhorn.io':
                            names=['source-missing','source-existing'] if side=='source' else ['target-existing']
                            if defect=='foreign-volume':names.append('foreign')
                            return {'items':[{'metadata':{'name':v}} for v in names]}
                        raise AssertionError(kind)
                    labs[side]=SimpleNamespace(DIRECTORY=d,installation_health=Mock(),get=get)
                r.source=labs['source'];r.target=labs['target']
                r.target_fixture=SimpleNamespace(read=Mock(return_value={'applied_sha256':row['config_sha256']['target'],
                    'original_sha256':row['config_sha256']['target']}),ready=Mock())
                r.workloads={k:SimpleNamespace(identities=Mock(),data=Mock(),journal=Mock(return_value={'volume':'source-'+k})) for k in ('missing','existing')}
                path=root/'missing.json'
                r.receivers={'existing':SimpleNamespace(identities=Mock(),data=Mock(return_value='bad' if defect=='existing-data' else 'digest'),journal=Mock(return_value={'volume':'target-existing'})),
                    'missing':SimpleNamespace(path=path)}
                if defect=='stage':row['stage']='restoring'
                elif defect=='restore-intent':row['restore_request']={}
                elif defect=='input':(labs['source'].DIRECTORY/'config.yml').write_text('changed')
                elif defect=='missing-journal':path.write_text('{}')
                if defect:
                    with self.assertRaises(ValueError):r.resume_prepared()
                    r.refuse_attached.assert_not_called();r.restore_target.assert_not_called()
                else:
                    r.resume_prepared();r.verify_materials.assert_called_once()
                    r.refuse_attached.assert_called_once();r.restore_target.assert_called_once()


if __name__ == '__main__':
    unittest.main()
