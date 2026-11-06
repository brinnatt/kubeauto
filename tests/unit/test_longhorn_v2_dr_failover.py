from contextlib import contextmanager
import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

gate=module('dr_handoff_contract',ROOT/'tests/helpers/longhorn-v2-dr-failover.py')
parent=module('dr_handoff_native_contract',ROOT/'tests/helpers/longhorn-v2-disaster.py')


class FencingContracts(unittest.TestCase):
    def fixture(self):
        journal={'namespace':'longhorn-business-source','objects':{'Pod/writer':'writer','PersistentVolumeClaim/data':'claim'},
            'volume':'source','volume_uid':'source-volume','pv':'source-pv','pv_uid':'pv'}
        objects={'volumes.longhorn.io':{'metadata':{'name':'source','uid':'source-volume','resourceVersion':'4'},
            'spec':{'nodeID':'','disableFrontend':True,'dataEngine':'v2','frontend':'blockdev'},'status':{'state':'detached','currentNodeID':''}},
            'pv':{'metadata':{'name':'source-pv','uid':'pv'},'spec':{'claimRef':{'uid':'claim'},
                'csi':{'volumeHandle':'source'},'persistentVolumeReclaimPolicy':'Retain'}},
            'pod':{'metadata':{'name':'writer','uid':'writer'}},'pvc':{'metadata':{'name':'data','uid':'claim'}},
            'engines.longhorn.io':[{'metadata':{'name':'source-engine','uid':'engine'},
                'spec':{'volumeName':'source','desireState':'stopped'},'status':{'currentState':'stopped'}}],
            'enginefrontends.longhorn.io':[{'metadata':{'name':'source-frontend','uid':'frontend'},
                'spec':{'volumeName':'source','desireState':'stopped'},'status':{'currentState':'stopped','endpoint':''}}],
            'volumeattachments.longhorn.io':[]}
        def get(kind,name='',namespace=''):
            if kind in ('pod','pvc'):
                return copy.deepcopy(objects[kind]) if name else {'items':[copy.deepcopy(objects[kind])] if objects[kind] else []}
            if kind=='pv':return copy.deepcopy(objects[kind]) if name else {'items':[copy.deepcopy(objects[kind])] if objects[kind] else []}
            if kind=='volumes.longhorn.io':return copy.deepcopy(objects[kind])
            return {'items':copy.deepcopy(objects[kind])}
        calls=[]
        def remove(obj):
            kind=next(k for k in ('pod','pvc','pv') if objects[k] and objects[k]['metadata']['uid']==obj['metadata']['uid'])
            calls.append(kind);objects[kind]=None
        def kubectl(*args,**kwargs):
            operations=json.loads(kwargs['stdin']);self.assertEqual('source-volume',operations[0]['value'])
            objects['volumes.longhorn.io']['spec']['disableFrontend']=True;calls.append('disable-frontend')
        lab=SimpleNamespace(get=get,owner=lambda:{'cluster_uid':'source-cluster'},remove=Mock(side_effect=remove),
            check_business_graph=Mock(),wait=lambda label,timeout,predicate:predicate(),kubectl=Mock(side_effect=kubectl))
        return lab,journal,objects,calls

    def clear_consumers(self,objects):
        for kind in ('pod','pvc','pv'):objects[kind]=None

    def test_fence_retires_only_original_consumers_and_disables_frontend_without_force(self):
        lab,journal,objects,calls=self.fixture();objects['volumes.longhorn.io']['spec']['disableFrontend']=False
        proof=gate.fence(lab,journal,parent)
        self.assertEqual(['pod','pvc','pv','disable-frontend'],calls)
        self.assertEqual('source-volume',proof['volume_uid'])
        self.assertEqual(proof,gate.fenced(lab,journal,parent.original))
        for call in lab.kubectl.call_args_list:
            self.assertNotIn('--force',call.args);self.assertNotIn('delete',call.args)

    def test_workloads_claim_pv_tickets_frontend_or_running_engine_block_proof(self):
        for fault in ('pod','pvc','pv','tickets','frontend','engine','disabled','attached','node'):
            with self.subTest(fault=fault):
                lab,journal,objects,_=self.fixture()
                if fault not in ('pod','pvc','pv'):self.clear_consumers(objects)
                else:
                    for kind in ('pod','pvc','pv'):
                        if kind!=fault:objects[kind]=None
                if fault=='tickets':objects['volumeattachments.longhorn.io']=[{'spec':{'volume':'source','attachmentTickets':{'old-writer':{}}}}]
                elif fault=='frontend':objects['enginefrontends.longhorn.io'][0]['status']['endpoint']='/dev/longhorn/source'
                elif fault=='engine':objects['engines.longhorn.io'][0]['status']['currentState']='running'
                elif fault=='disabled':objects['volumes.longhorn.io']['spec']['disableFrontend']=False
                elif fault=='attached':objects['volumes.longhorn.io']['status']['state']='attached'
                elif fault=='node':objects['volumes.longhorn.io']['spec']['nodeID']='source-node'
                with self.assertRaises(ValueError):gate.fenced(lab,journal,parent.original)
                lab.remove.assert_not_called();lab.kubectl.assert_not_called()

    def test_replaced_original_writer_stops_before_any_mutation(self):
        lab,journal,objects,_=self.fixture();objects['pod']['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'writer changed'):gate.fence(lab,journal,parent)
        lab.remove.assert_not_called();lab.kubectl.assert_not_called()

    def test_pv_reclaim_and_claim_binding_drift_are_not_deleted(self):
        for field in ('persistentVolumeReclaimPolicy','claimRef','csi'):
            lab,journal,objects,_=self.fixture()
            objects['pv']['spec'][field]='Delete' if field=='persistentVolumeReclaimPolicy' else {}
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'PV binding'):gate.fence(lab,journal,parent)
            self.assertTrue(objects['pv']);lab.kubectl.assert_not_called()

    def test_activation_requires_current_proof_latest_restore_and_original_declaration(self):
        source,journal,objects,_=self.fixture();self.clear_consumers(objects)
        proof=gate.fenced(source,journal,parent.original)
        volume={'metadata':{'uid':'target-volume'},'spec':{'size':str(2*1024**3),'fromBackup':'s3://original'}}
        native=SimpleNamespace(original=parent.original,standby_restored=Mock(return_value=True),guarded_patch=Mock())
        target=SimpleNamespace(get=Mock(return_value=volume))
        def activate(p):gate.activate(source,journal,target,'standby','target-volume',{'name':'last'},'raw',p,native,'s3://original')
        for fault in ('missing','old-proof','not-restored','changed-origin','changed-size'):
            with self.subTest(fault=fault):
                native.guarded_patch.reset_mock();volume['spec'].update(fromBackup='s3://original',size=str(2*1024**3))
                native.standby_restored.return_value=fault!='not-restored'
                current=copy.deepcopy(proof)
                if fault=='old-proof':current['spec_sha256']='old'
                elif fault=='changed-origin':volume['spec']['fromBackup']='s3://foreign'
                elif fault=='changed-size':volume['spec']['size']='1'
                with self.assertRaises(ValueError):activate(None if fault=='missing' else current)
                native.guarded_patch.assert_not_called()
        volume['spec'].update(fromBackup='s3://original',size=str(2*1024**3));native.standby_restored.return_value=True
        activate(proof)
        self.assertEqual({'Standby':False,'frontend':'blockdev'},native.guarded_patch.call_args.args[-1])

    def test_activated_is_not_complete_until_both_restore_flags_clear_and_detached(self):
        obj={'metadata':{'uid':'volume'},'spec':{'dataEngine':'v2','Standby':False,'frontend':'blockdev'},
            'status':{'restoreInitiated':True,'isStandby':False,'restoreRequired':False,'state':'detached','currentNodeID':''}}
        lab=SimpleNamespace(get=lambda *args:obj)
        self.assertTrue(gate.activated(lab,'target','volume',parent))
        for key,value in (('restoreInitiated',False),('isStandby',True),('restoreRequired',True),('state','attached'),('currentNodeID','node')):
            previous=obj['status'][key];obj['status'][key]=value
            self.assertFalse(gate.activated(lab,'target','volume',parent));obj['status'][key]=previous

    def test_static_pv_uses_existing_volume_and_exact_claim_uid_with_retain(self):
        claim,pv=gate.static_manifests('recovery','data','pv','original-volume','original-claim',1)
        self.assertEqual('pv',claim['spec']['volumeName']);self.assertEqual('original-volume',pv['spec']['csi']['volumeHandle'])
        self.assertEqual('original-claim',pv['spec']['claimRef']['uid'])
        self.assertEqual('Retain',pv['spec']['persistentVolumeReclaimPolicy'])
        self.assertEqual('1',pv['spec']['csi']['volumeAttributes']['numberOfReplicas'])


class ContextContracts(unittest.TestCase):
    def fixture(self,directory):
        source=SimpleNamespace(DIRECTORY=Path(directory)/'source');target=SimpleNamespace(DIRECTORY=Path(directory)/'target')
        source.DIRECTORY.mkdir();target.DIRECTORY.mkdir()
        row={'id':'run','contexts':{'target':{'namespace':'longhorn-business-target','pv':'recovery-pv',
            'volume':'recovery-volume','volume_uid':'volume'}},'intents':{
            'ns':{'side':'target','kind':'Namespace','name':'longhorn-business-target','namespace':'','uid':'namespace'},
            'claim':{'side':'target','kind':'PersistentVolumeClaim','name':'data','namespace':'longhorn-business-target','uid':'claim'},
            'pv':{'side':'target','kind':'PersistentVolume','name':'recovery-pv','namespace':'','uid':'pv'}}}
        objects={'namespace':{'metadata':{'uid':'namespace'}},'PersistentVolumeClaim':{'metadata':{'name':'data','uid':'claim'}},
            'PersistentVolume':{'metadata':{'name':'recovery-pv','uid':'pv'},'spec':{
                'csi':{'volumeHandle':'recovery-volume'},'claimRef':{'uid':'claim'},'persistentVolumeReclaimPolicy':'Retain'}},
            'volumes.longhorn.io':{'metadata':{'name':'recovery-volume','uid':'volume'},'status':{'state':'detached'}}}
        calls=[]
        def get(kind,name='',namespace=''):
            if kind=='pv':return {'items':[objects['PersistentVolume']] if objects.get('PersistentVolume') else []}
            return copy.deepcopy(objects.get(kind))
        def remove(obj):
            kind=next(k for k,v in objects.items() if v and v['metadata']['uid']==obj['metadata']['uid'])
            calls.append(kind);objects[kind]=None
        target.get=get;target.remove=Mock(side_effect=remove);target.check_business_graph=Mock()
        target.namespace_defaults=lambda namespace:['default-sa','root-ca']
        target.wait=lambda label,timeout,predicate:predicate()
        target.write_private=lambda path,body:path.write_text(json.dumps(body))
        dr=SimpleNamespace(source=source,target=target,store=Mock(),consumer=Mock(),receipt=lambda:copy.deepcopy(row))
        scenario=gate.Failover(dr,parent,Mock())
        return scenario,row,objects,calls

    def test_known_context_creation_window_recovers_exact_intents_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,_,_=self.fixture(directory)
            journal=scenario.recover_context('target')
            self.assertEqual({'PersistentVolumeClaim/data':'claim','PersistentVolume/recovery-pv':'pv'},journal['objects'])
            self.assertEqual(['default-sa','root-ca'],journal['defaults'])
            self.assertEqual(journal,json.loads(scenario.context_path('target').read_text()))
            scenario.target.check_business_graph.assert_called_once_with(journal)

    def test_unknown_or_replaced_context_intent_keeps_journal_and_data(self):
        for fault in ('unknown','replaced','namespace'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                scenario,row,objects,_=self.fixture(directory)
                if fault=='unknown':row['intents']['claim']['uid']=None
                elif fault=='replaced':objects['PersistentVolumeClaim']['metadata']['uid']='replacement'
                else:objects['namespace']['metadata']['uid']='replacement'
                with self.assertRaises(ValueError):scenario.recover_context('target')
                scenario.target.remove.assert_not_called();scenario.store.cleanup.assert_not_called()

    def test_full_namespace_graph_refusal_prevents_context_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,_,_=self.fixture(directory)
            scenario.target.check_business_graph.side_effect=ValueError('foreign resource')
            with self.assertRaisesRegex(ValueError,'foreign'):scenario.recover_context('target')
            self.assertFalse(scenario.context_path('target').exists());scenario.target.remove.assert_not_called()

    def test_consumers_retire_before_original_native_volume_and_references_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,objects,calls=self.fixture(directory)
            scenario.retire_context('target')
            self.assertEqual(['PersistentVolumeClaim','PersistentVolume','volumes.longhorn.io'],calls)
            self.assertIsNone(objects['volumes.longhorn.io'])
            self.assertTrue(scenario.context_path('target').exists())

    def test_foreign_static_pv_binding_keeps_native_volume_and_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,objects,calls=self.fixture(directory)
            objects['PersistentVolume']['spec']['csi']['volumeHandle']='foreign'
            with self.assertRaisesRegex(ValueError,'PV binding'):scenario.retire_context('target')
            self.assertEqual(['PersistentVolumeClaim'],calls)
            self.assertTrue(objects['volumes.longhorn.io']);scenario.store.cleanup.assert_not_called()


class WorkflowContracts(unittest.TestCase):
    def fixture(self,directory):
        calls=[];source=SimpleNamespace(owner=lambda:{'run_id':'source'});target=SimpleNamespace(DIRECTORY=Path(directory))
        (target.DIRECTORY/'yml/longhorn').mkdir(parents=True)
        (target.DIRECTORY/'yml/longhorn/block-disks.json').write_text(json.dumps({'nodes':{'192.168.47.149':{'disks':{'kubeauto-block':{'disk_uuid':'raw'}}}}}))
        target.wait=lambda label,timeout,predicate:True
        store=SimpleNamespace(prepare=lambda:calls.append('server'),qualify=lambda:calls.append('source-transport'),product_configuration=lambda:{})
        consumer=SimpleNamespace(initialize=Mock(),qualify=lambda pid:calls.append('independent-target'))
        row={'id':'run'}
        dr=SimpleNamespace(source=source,target=target,store=store,consumer=consumer,initialize=Mock(),
            source_workload=lambda:{'volume_uid':'old-source'},payload=Mock(),
            backup_point=Mock(side_effect=[{'name':'first','url':'s3://first'},{'name':'last','url':'s3://last'}]),
            create_standby=lambda point:('standby','recovery-volume'),restored=Mock(),discover=Mock(),
            receipt=lambda:copy.deepcopy(row),save=lambda value:(row.clear(),row.update(value)))
        scenario=gate.Failover(dr,parent,Mock())
        scenario.cleanup=Mock(side_effect=lambda:calls.append('cleanup'))
        @contextmanager
        def scoped_cleanup(cleanup):
            try:yield
            finally:cleanup()
        source.scoped_cleanup=scoped_cleanup
        def mount(role,*args):calls.append('mount-'+role);return {'volume_uid':'recovery-volume' if role=='target' else 'new-main'}
        scenario.mount=Mock(side_effect=mount);scenario.data=Mock(return_value='confirmed-hash')
        scenario.target_backup=Mock(return_value={'name':'return','sha256':'confirmed-hash'})
        scenario.restore_return=Mock(return_value=('new-main','new-main-uid'))
        def fence(lab,*args):calls.append('fence-source' if lab is source else 'fence-recovery');return {'site':'source' if lab is source else 'recovery'}
        def fenced(lab,*args):return {'site':'source' if lab is source else 'recovery'}
        def activate(*args):
            if args[7] is None:raise ValueError('DR activation requires current source fencing proof')
            calls.append('activate')
        return scenario,calls,fence,fenced,activate

    def test_activation_qualification_stops_after_confirmed_recovery_write_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,calls,fence,fenced,activate=self.fixture(directory)
            with patch.object(gate,'fence',side_effect=fence),patch.object(gate,'fenced',side_effect=fenced),patch.object(gate,'activate',side_effect=activate):
                scenario.qualify(1,2,'activation')
            self.assertEqual(['server','independent-target','source-transport','fence-source','activate','mount-target','cleanup'],calls)
            scenario.target_backup.assert_not_called();scenario.restore_return.assert_not_called();scenario.cleanup.assert_called_once()

    def test_failback_requires_new_native_identity_and_reverse_fence_before_main_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,calls,fence,fenced,activate=self.fixture(directory)
            with patch.object(gate,'fence',side_effect=fence),patch.object(gate,'fenced',side_effect=fenced),patch.object(gate,'activate',side_effect=activate):
                scenario.qualify(1,2,'failback')
            self.assertLess(calls.index('fence-source'),calls.index('activate'))
            self.assertLess(calls.index('fence-recovery'),calls.index('mount-failback'))
            self.assertEqual('cleanup',calls[-1]);scenario.target_backup.assert_called_once()

    def test_old_source_or_recovery_identity_cannot_become_return_volume(self):
        for uid in ('old-source','recovery-volume'):
            with self.subTest(uid=uid),tempfile.TemporaryDirectory() as directory:
                scenario,calls,fence,fenced,activate=self.fixture(directory)
                scenario.restore_return.return_value=('old-volume',uid)
                with patch.object(gate,'fence',side_effect=fence),patch.object(gate,'fenced',side_effect=fenced),patch.object(gate,'activate',side_effect=activate):
                    with self.assertRaisesRegex(ValueError,'old native identity'):scenario.qualify(1,2,'failback')
                self.assertNotIn('mount-failback',calls);self.assertEqual('cleanup',calls[-1])


class CleanupContracts(unittest.TestCase):
    def fixture(self,directory):
        source_dir=Path(directory)/'source';target_dir=Path(directory)/'target';source_dir.mkdir();target_dir.mkdir()
        source=SimpleNamespace(DIRECTORY=source_dir,restore_request_input=Mock())
        target=SimpleNamespace(DIRECTORY=target_dir,restore_request_input=Mock())
        path=source_dir/'coordinator.json';path.write_text('{}')
        row={'id':'original-run','volumes':{'standby':{'uid':'original-volume','from_backup':'s3://original'}},
            'activation':{'volume':'standby','volume_uid':'original-volume'}}
        native={'metadata':{'name':'standby','uid':'original-volume','labels':{'kubeauto.io/longhorn-dr-run':'original-run'}},
            'spec':{'fromBackup':'s3://original','Standby':False,'frontend':'blockdev','dataEngine':'v2','numberOfReplicas':1}}
        objects={'native':native};calls=[]
        def get(kind,name='',namespace=''):
            return {'items':[]} if kind=='pv' else copy.deepcopy(objects['native'])
        def remove(obj):
            self.assertEqual('original-volume',obj['metadata']['uid']);objects['native']=None;calls.append('native')
        target.get=get;target.remove=Mock(side_effect=remove);target.wait=lambda label,timeout,predicate:predicate()
        holders=[]
        for folder in (source_dir,target_dir):
            record=folder/'store.json';record.write_text('{}')
            holders.append(SimpleNamespace(record=record,receipt=lambda:{'product_binding':{'owned':'binding'}}))
        dr=SimpleNamespace(source=source,target=target,store=holders[0],consumer=holders[1],path=path,
            receipt=lambda:copy.deepcopy(row),validate_intents=Mock(),
            retain_caches=Mock(side_effect=lambda lab,binding,side:calls.append(side+'-cache')),
            cleanup=Mock(side_effect=lambda:calls.append('parent-cleanup')))
        scenario=gate.Failover(dr,parent,Mock())
        return scenario,row,native,calls

    def test_activated_native_retires_before_cache_and_backend_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,_,calls=self.fixture(directory);scenario.cleanup()
            self.assertEqual(['native','target-cache','source-cache','parent-cleanup'],calls)

    def test_activation_without_original_intent_or_changed_identity_never_deletes(self):
        for fault in ('intent','uid','origin','engine','replicas','label'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                scenario,row,native,calls=self.fixture(directory)
                if fault=='intent':row.pop('activation')
                elif fault=='uid':native['metadata']['uid']='replacement'
                elif fault=='origin':native['spec']['fromBackup']='s3://foreign'
                elif fault=='engine':native['spec']['dataEngine']='v1'
                elif fault=='replicas':native['spec']['numberOfReplicas']=3
                else:native['metadata']['labels']['kubeauto.io/longhorn-dr-run']='replacement'
                with self.assertRaises(ValueError):scenario.cleanup()
                scenario.target.remove.assert_not_called();scenario.dr.retain_caches.assert_not_called()
                scenario.dr.cleanup.assert_not_called();self.assertTrue(scenario.dr.path.exists())

    def test_any_cache_recovery_failure_keeps_backend_and_ownership_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,_,_,calls=self.fixture(directory)
            scenario.dr.retain_caches.side_effect=ValueError('foreign cache')
            with self.assertRaisesRegex(ValueError,'foreign cache'):scenario.cleanup()
            self.assertEqual(['native'],calls);scenario.dr.cleanup.assert_not_called()
            self.assertTrue(scenario.store.record.exists());self.assertTrue(scenario.consumer.record.exists())



class ShellContracts(unittest.TestCase):
    def test_fixed_routes_static_preflight_and_failure_evidence_cover_both_clusters(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        for stage in ('activation','failback'):
            self.assertGreaterEqual(runner.count('v2-dr-'+stage+'-qualification'),4)
        self.assertIn('longhorn-v2-dr-failover.sh',(ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v2_dr_failover -v',(ROOT/'tests/helpers/longhorn-static.sh').read_text())
        shell=(ROOT/'tests/helpers/longhorn-v2-dr-failover.sh').read_text()
        self.assertIn('for cluster in longhorn-v2-gate longhorn-v2-dr-target',shell)
        self.assertIn('describe pods -A',shell);self.assertIn('app=longhorn-manager',shell)
        self.assertIn('volumeattachments.longhorn.io,backups.longhorn.io',shell)
        self.assertIn('longhorn-dr-failback-business.json',shell)
        self.assertNotIn('get secrets',shell)

    def shell(self,directory,stage,fault=''):
        root=Path(directory)
        for name in ('.venv/bin','kube-bin','clusters/longhorn-v2-dr-target','tests/helpers'):
            (root/name).mkdir(parents=True,exist_ok=True)
        context=root/'control-config';context.write_text('original-context')
        driver=root/'.venv/bin/python';driver.write_text("""#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];helper,mode=sys.argv[1:3];name=Path(helper).name
with (root/'calls').open('a') as f:f.write(name+' '+mode+'\\n')
fault=os.environ.get('DR_HANDOFF_FAULT','')
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/(name+'.ready')).touch()
 while True:time.sleep(.01)
if mode in ('source-boundary','target-baseline'):
 marker='LONGHORN_V2_DR_SOURCE_BOUNDARY' if mode=='source-boundary' else 'LONGHORN_V2_DR_TARGET_STORE_BASELINE'
 count=(root/'calls').read_text().count(name+' '+mode+'\\n')
 print(marker+' sha256='+('drift' if count>1 and fault==mode else 'original'))
elif mode=='qualify':
 deadline=time.monotonic()+3
 while len(list(root.glob('*.ready')))<2 and time.monotonic()<deadline:time.sleep(.01)
 if len(list(root.glob('*.ready')))!=2:sys.exit(8)
 stage=sys.argv[sys.argv.index('--stage')+1]
 # A concurrent owner updates the shared context during business; this gate must preserve it.
 (root/'control-config').write_text('foreign-concurrent-context')
 if fault=='product':sys.exit(7)
 markers=[
 'LONGHORN_V2_DR_WORKFLOW_UNFENCED_ACTIVATION_REFUSED native_api_not_called=true',
 'LONGHORN_V2_DR_CACHE_RETIRED side=target external_backups=preserved',
 'LONGHORN_V2_DR_CACHE_RETIRED side=source external_backups=preserved',
 'LONGHORN_V2_DR_FAILOVER_FIXTURES_CLEAN_PASS',
 'LONGHORN_V2_DR_'+stage.upper()+'_BUSINESS_PASS',
 'LONGHORN_V2_DR_SOURCE_FENCED {}',
 'LONGHORN_V2_DR_ACTIVATED_BUSINESS_VERIFIED {}']
 if stage=='failback':markers += ['LONGHORN_V2_DR_RETURN_BACKUP {}',
   'LONGHORN_V2_DR_RECOVERY_FENCED {}','LONGHORN_V2_DR_FAILBACK_BUSINESS_VERIFIED {}']
 for marker in markers:
  if not (fault=='missing-activation' and 'ACTIVATED_BUSINESS_VERIFIED' in marker) and not (fault=='missing-reverse' and 'RECOVERY_FENCED' in marker):print(marker)
elif mode=='cleanup' and fault=='cleanup':sys.exit(9)
""");driver.chmod(0o755)
        kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
        text=(ROOT/'tests/helpers/longhorn-v2-dr-failover.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(context))
        shell=root/'run.sh';shell.write_text(text)
        result=subprocess.run(['bash',str(shell),'v2-dr-'+stage+'-qualification'],text=True,capture_output=True,timeout=15,
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',DR_HANDOFF_FAULT=fault))
        return result,(root/'calls').read_text().splitlines(),context.read_text()

    def test_both_stages_require_business_result_scoped_cleanup_and_preserved_foreign_context(self):
        for stage in ('activation','failback'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.shell(directory,stage)
                self.assertEqual(0,result.returncode,result.stderr)
                self.assertIn('LONGHORN_V2_DR_'+stage.upper()+'_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
                self.assertEqual('foreign-concurrent-context',context)
                self.assertLess(calls.index('longhorn-v2-dr-failover.py cleanup'),calls.index('longhorn-v2-install.py relay-clean'))
                self.assertEqual(2,calls.count('longhorn-v2-dr-store.py target-baseline'))

    def test_product_missing_result_cleanup_and_each_baseline_failure_cannot_pass(self):
        for stage,fault in (('activation','product'),('activation','missing-activation'),('activation','cleanup'),
                ('activation','source-boundary'),('activation','target-baseline'),('failback','missing-reverse')):
            with self.subTest(stage=stage,fault=fault),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.shell(directory,stage,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DR_'+stage.upper()+'_QUALIFICATION_PASS',result.stdout)
                self.assertEqual('foreign-concurrent-context',context)
                self.assertIn('longhorn-v2-dr-failover.py cleanup',calls)
                self.assertIn('longhorn-v2-install.py relay-clean',calls)
                self.assertIn('longhorn-v2-dr-target.py relay-clean',calls)



class ActivationPrerequisiteContracts(unittest.TestCase):
    def test_failback_guard_precedes_every_transport_and_cleanup_operation(self):
        shell=(ROOT/'tests/helpers/longhorn-v2-dr-failover.sh').read_text()
        self.assertLess(shell.index("<<'PY_ACTIVATION'"),shell.index('source_before='))
        self.assertLess(shell.index("<<'PY_ACTIVATION'"),shell.index('trap store_exit EXIT'))
        self.assertIn('if [[ "$stage" == failback ]]; then',shell)

    def test_real_guard_rejects_missing_incomplete_changed_or_unclean_activation(self):
        import ast,hashlib,sys
        shell=(ROOT/'tests/helpers/longhorn-v2-dr-failover.sh').read_text()
        program=shell.split("<<'PY_ACTIVATION'\n",1)[1].split('\nPY_ACTIVATION',1)[0]
        names=next(ast.literal_eval(n.iter) for n in ast.parse(program).body if isinstance(n,ast.For))
        self.assertIn('tests/helpers/longhorn-v2-dr-failover.py',names)
        self.assertIn('roles/longhorn/files/longhorn.py',names)
        for fault in ('','missing','status','rc','failure','identity','business','unverified','inputs','dependency','cleanup','residue','missing-cluster','manager-pin','manager-unrelated','manager-unpublished'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);inputs={}
                for name in names:
                    path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('original '+name)
                    inputs[name]=hashlib.sha256(path.read_bytes()).hexdigest()
                # The real guard imports this resolver before checking its
                # activation evidence. Supply its actual code in the fixture.
                material=root/'tests/helpers/longhorn-full-material.py'
                material.write_bytes((ROOT/'tests/helpers/longhorn-full-material.py').read_bytes())
                evidence={'status':'pass','rc':0,'failure_markers':0,'baseline_identity':{'source':{'cluster_uid':'source'},'target':{'cluster_uid':'target'}},
                    'activated_business':{'source_fenced':True},'frozen_inputs':inputs,
                    'remote_input_verification':{'mismatches':0,'matching_files':len(inputs)},
                    'independent_readback':dict(backend_container_removed=True,server_directory_removed=True,
                        api_console_tunnel_not_listening=True,target_consumer_removed=True,
                        clusters={'source':dict(volumes=0,backup_resources=0,backup_fixture_credentials=0,relays=0),
                                  'target':dict(volumes=0,backup_resources=0,backup_fixture_credentials=0,relays=0)})}
                if fault.startswith('manager-'):
                    for name in ('common/constants.py', 'roles/longhorn/files/images.json',
                            'tests/helpers/longhorn-manager-adopt.py',
                            'tests/fixtures/longhorn-manager-pin-transition.json',
                            'tests/fixtures/longhorn-manager-pending-pin-transition.json',
                            'tests/evidence/longhorn-manager-eviction-publication-20261024.json',
                            'tests/evidence/longhorn-manager-pending-publication-20261102.json'):
                        target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
                        target.write_bytes((ROOT/name).read_bytes())
                    historical = ROOT/'tests/fixtures/longhorn-im-revision-2-constants-fd9ae3d9.py.txt'
                    (root/'common/constants.py').write_bytes(historical.read_bytes())
                    transition=json.loads((root/'tests/fixtures/longhorn-manager-pending-pin-transition.json').read_text())
                    inputs['common/constants.py']=transition['inputs']['common/constants.py']['old_sha256']
                    if fault=='manager-unrelated':
                        with (root/'common/constants.py').open('a') as stream:stream.write('\n# unrelated change\n')
                    if fault=='manager-unpublished':
                        path=root/'tests/evidence/longhorn-manager-pending-publication-20261102.json'
                        path.write_text('{}')
                folder=root/'tests/evidence';folder.mkdir(exist_ok=True)
                (folder/'longhorn-v2-dr-standby-qualification-20261021.json').write_text(json.dumps({'baseline_identity':evidence['baseline_identity']}))
                if fault=='status':evidence['status']='pending'
                elif fault=='rc':evidence['rc']=1
                elif fault=='failure':evidence['failure_markers']=1
                elif fault=='identity':evidence['baseline_identity']['target']['cluster_uid']='foreign'
                elif fault=='business':evidence['activated_business']['source_fenced']=False
                elif fault=='unverified':evidence['remote_input_verification']['mismatches']=1
                elif fault=='inputs':evidence['remote_input_verification']['matching_files']-=1
                elif fault=='dependency':(root/'tests/helpers/longhorn-v2-dr-failover.py').write_text('changed')
                elif fault=='cleanup':evidence['independent_readback']['backend_container_removed']=False
                elif fault=='residue':evidence['independent_readback']['clusters']['target']['volumes']=1
                elif fault=='missing-cluster':evidence['independent_readback']['clusters'].pop('target')
                if fault!='missing':(folder/'longhorn-v2-dr-activation-qualification-20261021.json').write_text(json.dumps(evidence))
                result=subprocess.run([sys.executable,'-c',program,str(root)],text=True,capture_output=True,timeout=5)
                if fault not in ('','manager-pin'):
                    self.assertNotEqual(0,result.returncode)
                    self.assertNotIn('LONGHORN_V2_DR_ACTIVATION_QUALIFICATION_CURRENT',result.stdout)
                    expected={'missing':'longhorn-v2-dr-activation-qualification-20261021.json',
                        'dependency':'DR activation dependency changed:',
                        'cleanup':'DR activation cleanup was not independently verified',
                        'residue':'DR activation left consumers or transport residue',
                        'missing-cluster':'DR activation lacks independent readback for both clusters',
                        'manager-unrelated':'DR activation dependency changed:',
                        'manager-unpublished':'Manager transition publication bytes changed'}
                    self.assertIn(expected.get(fault,'DR failback requires current completed activation qualification'),result.stderr)
                else:
                    self.assertEqual(0,result.returncode,result.stderr)
                    self.assertIn('LONGHORN_V2_DR_ACTIVATION_QUALIFICATION_CURRENT',result.stdout)


if __name__=='__main__':unittest.main()
