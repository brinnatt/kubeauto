from contextlib import nullcontext
import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('v2_disaster_contract',ROOT/'tests/helpers/longhorn-v2-disaster.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
bspec=importlib.util.spec_from_file_location('v2_disaster_backup',ROOT/'tests/helpers/longhorn-backups.py')
backups=importlib.util.module_from_spec(bspec);bspec.loader.exec_module(backups)


class StandbyContracts(unittest.TestCase):
    def restore_fixture(self):
        volume={'metadata':{'name':'standby','uid':'volume'},'spec':{
            'dataEngine':'v2','numberOfReplicas':1,'Standby':True,'frontend':'','fromBackup':'s3://fixture'},
            'status':{'isStandby':True,'state':'attached','robustness':'healthy','lastBackup':'point',
                'restoreRequired':True}}
        engine={'metadata':{'uid':'engine'},'spec':{'volumeName':'standby','dataEngine':'v2',
            'requestedBackupRestore':'point'},'status':{'currentState':'running','lastRestoredBackup':'point',
                'restoreStatus':{},'endpoint':'internal-export-is-not-a-business-frontend'}}
        replica={'metadata':{'uid':'replica'},'spec':{'volumeName':'standby','nodeID':'longhorn-node-149',
            'diskID':'raw-uuid','dataEngine':'v2','healthyAt':'now','failedAt':''},'status':{'currentState':'running'}}
        objects={'volumes.longhorn.io':volume,'engines.longhorn.io':{'items':[engine]},
            'replicas.longhorn.io':{'items':[replica]},'enginefrontends.longhorn.io':{'items':[]},'pv':{'items':[]}}
        lab=SimpleNamespace(get=lambda kind,*args,**kwargs:copy.deepcopy(objects[kind]))
        return lab,objects,volume,engine,replica

    def restored(self,lab):return gate.standby_restored(lab,'standby','volume','point','raw-uuid')

    def test_actual_engine_restore_required_discovery_alone_does_not_pass(self):
        lab,_,_,engine,_=self.restore_fixture()
        engine['status']['lastRestoredBackup']='previous'
        self.assertIsNone(self.restored(lab))
        engine['status']['lastRestoredBackup']='point';engine['status']['restoreStatus']={'replica':{'isRestoring':True}}
        self.assertIsNone(self.restored(lab))
        engine['status']['restoreStatus']={'replica':{'error':'broken chain'}}
        with self.assertRaisesRegex(ValueError,'restore error'):self.restored(lab)

    def test_v2_internal_export_is_allowed_but_engine_frontend_is_refused(self):
        lab,objects,_,_,_=self.restore_fixture()
        self.assertEqual('point',self.restored(lab)['last_restored_backup'])
        for spec,status in (({'frontend':'blockdev','disableFrontend':False},{}),({}, {'endpoint':'/dev/longhorn/standby'})):
            objects['enginefrontends.longhorn.io']['items']=[{'spec':dict(volumeName='standby',**spec),'status':status}]
            with self.subTest(spec=spec,status=status),self.assertRaisesRegex(ValueError,'writable frontend'):self.restored(lab)

    def test_unassigned_replica_waits_and_wrong_node_disk_or_engine_is_refused(self):
        lab,_,_,_,replica=self.restore_fixture()
        for field in ('nodeID','diskID'):
            value=replica['spec'][field];replica['spec'][field]=''
            self.assertIsNone(self.restored(lab));replica['spec'][field]=value
        for field in ('nodeID','diskID','dataEngine'):
            value=replica['spec'][field];replica['spec'][field]='foreign'
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'raw disk'):self.restored(lab)
            replica['spec'][field]=value

    def test_replaced_volume_changed_declaration_and_pv_are_refused(self):
        lab,objects,volume,_,_=self.restore_fixture()
        for key in ('dataEngine','Standby','frontend','numberOfReplicas'):
            value=volume['spec'][key];volume['spec'][key]='changed'
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'declaration'):self.restored(lab)
            volume['spec'][key]=value
        volume['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'identity'):self.restored(lab)
        volume['metadata']['uid']='volume';objects['pv']['items']=[{'spec':{'csi':{'volumeHandle':'standby'}}}]
        with self.assertRaisesRegex(ValueError,'PV consumer'):self.restored(lab)

    def test_manifest_pins_v2_and_native_uppercase_standby_before_admission(self):
        lab=SimpleNamespace(get=lambda *args:{'value':'pinned-engine'})
        obj=gate.standby_manifest(lab,'standby','s3://exact')
        self.assertEqual({'Standby','dataEngine','frontend','fromBackup'} & set(obj['spec']),
            {'Standby','dataEngine','frontend','fromBackup'})
        self.assertIs(obj['spec']['Standby'],True);self.assertEqual('v2',obj['spec']['dataEngine'])
        self.assertEqual('',obj['spec']['frontend']);self.assertNotIn('standby',obj['spec'])

    def test_discovery_requires_exact_source_target_and_backup_url(self):
        obj={'metadata':{'labels':{'backup-target':'default','backup-volume':'source'}},
            'status':{'state':'Completed','url':'s3://exact'}}
        lab=SimpleNamespace(get=lambda *args:obj)
        self.assertIs(obj,gate.discovered_backup(lab,'point','source','s3://exact'))
        for field,value in (('state','InProgress'),('url','s3://wrong'),('error','denied')):
            old=copy.deepcopy(obj);obj['status'][field]=value
            if field=='state':self.assertIsNone(gate.discovered_backup(lab,'point','source','s3://exact'))
            else:
                with self.assertRaises(ValueError):gate.discovered_backup(lab,'point','source','s3://exact')
            obj=old

    def test_patch_retains_native_uid_resource_version_and_full_spec_guards(self):
        lab=SimpleNamespace(kubectl=Mock())
        gate.guarded_patch(lab,'volumes.longhorn.io',{'metadata':{'uid':'original','resourceVersion':'7','name':'standby'},
            'spec':{'Standby':True}},{'Standby':False})
        operations=json.loads(lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual(['test','test','test','add'],[o['op'] for o in operations])
        self.assertEqual(['original','7',{'Standby':True},False],[o['value'] for o in operations])


class JournalContracts(unittest.TestCase):
    def fixture(self,directory):
        labs=[]
        for name in ('source','target'):
            folder=Path(directory)/name;folder.mkdir()
            identity={'run_id':name,'cluster_uid':name+'-cluster','namespace_uid':name+'-namespace','node_uids':{name:'node'}}
            lab=SimpleNamespace(DIRECTORY=folder,owner=lambda identity=identity:identity,
                write_private=lambda path,row:path.write_text(json.dumps(row)),get=Mock(return_value=None),
                cleanup_business=Mock(),restore_request_input=Mock(),remove=Mock(),
                namespace_defaults=Mock(return_value=['serviceaccount','rootca']),check_business_graph=Mock(),
                wait=lambda label,timeout,predicate:predicate())
            labs.append(lab)
        source,target=labs
        store=SimpleNamespace(record=source.DIRECTORY/'store.json',cleanup=Mock(),receipt=Mock())
        consumer=SimpleNamespace(record=target.DIRECTORY/'consumer.json',cleanup=Mock(),receipt=Mock())
        source.backup_store=lambda:store
        shared=SimpleNamespace(Consumer=lambda *args:consumer)
        scenario=gate.Standby(source,target,backups,shared);scenario.initialize()
        return scenario,source,target,store,consumer

    def test_source_transport_is_created_once_after_independent_target_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,_,store,consumer=self.fixture(directory)
            calls=[];active=[];log=Path(directory)/'transport.log'
            def prepare_transport():
                calls.append('transport')
                with log.open('x') as stream:stream.write('original SSH log')
                active.append(True)
            def qualify():
                calls.append('source-qualified');prepare_transport()
            def cleanup_transport():
                calls.append('transport-cleaned');active.clear()
            def probe(pid):
                self.assertFalse(active);calls.append('independent-target')
            store.prepare=Mock(side_effect=lambda:calls.append('server'))
            store.qualify=qualify;store.prepare_transport=prepare_transport;store.cleanup_transport=cleanup_transport
            store.transport_listener=lambda:True if active else None;store.receipt.return_value={}
            consumer.initialize=Mock();consumer.qualify=probe
            source.scoped_cleanup=lambda cleanup:nullcontext()
            scenario.initialize=Mock();scenario.source_workload=Mock(side_effect=StopIteration('prerequisite boundary reached'))
            with self.assertRaisesRegex(StopIteration,'prerequisite boundary'):scenario.qualify(11,22)
            self.assertEqual(['server','independent-target','source-qualified','transport'],calls)
            self.assertEqual('original SSH log',log.read_text())

    def test_namespace_create_crash_window_is_recovered_by_original_uid_and_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,_,_,_=self.fixture(directory)
            row=scenario.receipt();row['intents']['namespace']={'side':'source','kind':'Namespace',
                'name':'longhorn-business-dr-fixture','namespace':'','uid':'original'};scenario.save(row)
            source.get.return_value={'metadata':{'uid':'original'}}
            scenario.recover_business_journal()
            journal=json.loads(scenario.business_path.read_text())
            self.assertEqual('original',journal['namespace_uid']);self.assertEqual({},journal['objects'])
            source.check_business_graph.assert_called_once_with(journal)

    def test_unknown_create_outcome_or_replacement_preserves_journal_and_backend(self):
        for uid in (None,'original'):
            with self.subTest(uid=uid),tempfile.TemporaryDirectory() as directory:
                scenario,source,_,store,consumer=self.fixture(directory)
                row=scenario.receipt();row['intents']['namespace']={'side':'source','kind':'Namespace',
                    'name':'longhorn-business-dr-fixture','namespace':'','uid':uid};scenario.save(row)
                source.get.return_value={'metadata':{'uid':'replacement'}}
                with self.assertRaisesRegex(ValueError,'unknown|replaced'):scenario.cleanup()
                self.assertTrue(scenario.path.exists());store.cleanup.assert_not_called();consumer.cleanup.assert_not_called()

    def test_foreign_namespace_graph_preserves_ownership_material(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,_,store,_=self.fixture(directory)
            row=scenario.receipt();row['intents']['namespace']={'side':'source','kind':'Namespace',
                'name':'longhorn-business-dr-fixture','namespace':'','uid':'original'};scenario.save(row)
            source.get.return_value={'metadata':{'uid':'original'}}
            source.check_business_graph.side_effect=ValueError('foreign resource')
            with self.assertRaisesRegex(ValueError,'foreign'):scenario.cleanup()
            self.assertTrue(scenario.path.exists());store.cleanup.assert_not_called();source.remove.assert_not_called()

    def test_unknown_backup_outcome_blocks_cache_retirement_and_backend_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,_,store,_=self.fixture(directory)
            store.record.write_text('{}');store.receipt.return_value={'product_binding':{'target_uid':'target',
                'original_spec':{},'fixture_spec':{}}}
            target={'metadata':{'name':'default','uid':'target','resourceVersion':'1'},'spec':{}}
            backup={'metadata':{'name':'unconfirmed','uid':'backup','labels':{'backup-target':'default','backup-volume':'source'}}}
            source.get.side_effect=lambda kind,*args,**kwargs:target if kind=='backuptargets.longhorn.io' else {'items':[backup]}
            with self.assertRaisesRegex(ValueError,'foreign backup'):scenario.cleanup()
            self.assertTrue(scenario.path.exists());store.cleanup.assert_not_called();source.cleanup_business.assert_not_called()

    def test_native_cache_retirement_preserves_verified_remote_bytes_and_rejects_replacement(self):
        for fault in ('none','replaced','external-bytes'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                scenario,source,_,store,_=self.fixture(directory)
                target={'metadata':{'name':'default','uid':'target','resourceVersion':'4'},
                    'spec':{'backupTargetURL':'s3://fixture','credentialSecret':'fixture'},'status':{'available':True}}
                backup={'metadata':{'name':'point','uid':'original-backup','labels':{
                    'backup-target':'default','backup-volume':'original-source'}}}
                objects={'backups.longhorn.io':[backup]}
                row=scenario.receipt();row['points']['point']={'name':'point','source_volume':'original-source',
                    'source_backup_uid':'original-backup','receipt':{'objects':[{'sha256':'original-bytes'}]}}
                scenario.save(row)
                binding={'target_uid':'target','original_spec':{'backupTargetURL':'','credentialSecret':''},
                    'fixture_spec':copy.deepcopy(target['spec'])}
                source.get.side_effect=lambda kind,*args,**kwargs:copy.deepcopy(target) if kind=='backuptargets.longhorn.io' else {'items':copy.deepcopy(objects.get(kind,[]))}
                def kubectl(*args,**kwargs):
                    patch=json.loads(kwargs['stdin'])
                    self.assertEqual('4',patch[1]['value'])
                    target['spec']=patch[-1]['value'];target['status']['available']=False
                    if fault=='replaced':objects['backups.longhorn.io'][0]['metadata']['uid']='replacement'
                    else:objects.clear()
                source.kubectl=Mock(side_effect=kubectl)
                store.backup_receipt=Mock(return_value={'objects':[{'sha256':'changed' if fault=='external-bytes' else 'original-bytes'}]})
                if fault=='none':
                    scenario.retain_caches(source,binding,'source')
                    store.backup_receipt.assert_called_once_with('original-source','point',2*1024**3)
                else:
                    with self.assertRaisesRegex(ValueError,'replaced|external backup data'):
                        scenario.retain_caches(source,binding,'source')
                store.cleanup.assert_not_called();source.remove.assert_not_called()

    def test_cleanup_retires_target_before_source_and_checks_external_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,target,store,consumer=self.fixture(directory)
            store.record.write_text('{}');consumer.record.write_text('{}')
            store.receipt.return_value={'product_binding':{'side':'source'}}
            consumer.receipt.return_value={'product_binding':{'side':'target'}}
            calls=[]
            scenario.retain_caches=lambda lab,binding,side:calls.append(side)
            source.cleanup_business.side_effect=lambda:calls.append('business')
            consumer.cleanup.side_effect=lambda:calls.append('consumer')
            store.cleanup.side_effect=lambda:calls.append('backend')
            scenario.cleanup()
            self.assertEqual(['target','source','business','consumer','backend'],calls)
            self.assertFalse(scenario.path.exists())

    def test_receipt_refuses_another_cluster_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            scenario,source,_,store,_=self.fixture(directory)
            source.owner()['cluster_uid']='replacement'
            with self.assertRaisesRegex(ValueError,'another installation'):scenario.cleanup()
            store.cleanup.assert_not_called();self.assertTrue(scenario.path.exists())


class DispatchContracts(unittest.TestCase):
    def test_native_standby_product_workflow_and_scoped_completion_are_runner_owned(self):
        shell=(ROOT/'tests/helpers/longhorn-v2-disaster.sh').read_text()
        self.assertIn('"$DR" qualify --source-relay-pid "$source_pid" --target-relay-pid "$target_pid"',shell)
        self.assertIn('LONGHORN_V2_DR_STANDBY_QUALIFICATION_SCOPED_CLEAN_PASS',shell)
        self.assertIn('"$DR" cleanup',shell)
        self.assertNotIn('"$DR" target-baseline',shell)
        self.assertIn('"$BASE/tests/helpers/longhorn-v2-dr-store.py" target-baseline',shell)
        self.assertIn('same_volume=true frontend=false pv=false',shell)
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertGreaterEqual(runner.count('v2-dr-standby-qualification'),4)
        self.assertIn('longhorn-v2-disaster.sh',(ROOT/'tests/helpers/longhorn-live.sh').read_text())


class ShellContracts(unittest.TestCase):
    def shell(self,directory,fault=''):
        root=Path(directory)
        for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-v2-dr-target'):
            (root/name).mkdir(parents=True,exist_ok=True)
        context=root/'control-config';context.write_text('original-context')
        driver=root/'.venv/bin/python';driver.write_text("""#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];helper,mode=sys.argv[1:3];name=Path(helper).name
with (root/'calls').open('a') as f:f.write(name+' '+mode+'\\n')
fault=os.environ.get('DR_STANDBY_FAULT','')
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
 # A concurrent owner updates the shared context during business; this gate must preserve it.
 (root/'control-config').write_text('foreign-concurrent-context')
 if fault=='product':sys.exit(7)
 for marker in (
 'LONGHORN_V2_DR_STANDBY_SYNC_VERIFIED points=2 same_volume=true frontend=false pv=false',
 'LONGHORN_V2_DR_CACHE_RETIRED side=target external_backups=preserved',
 'LONGHORN_V2_DR_CACHE_RETIRED side=source external_backups=preserved',
 'LONGHORN_V2_DR_STANDBY_FIXTURES_CLEAN_PASS',
 'LONGHORN_V2_DR_STANDBY_BUSINESS_PASS'):
  if fault!='missing-marker' or 'SYNC_VERIFIED' not in marker:print(marker)
 for point in range(1 if fault=='one-point' else 2):print('LONGHORN_V2_DR_STANDBY_RESTORED '+str(point))
elif mode=='cleanup' and fault=='cleanup':sys.exit(9)
""");driver.chmod(0o755)
        kubectl=root/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
        text=(ROOT/'tests/helpers/longhorn-v2-disaster.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(context))
        shell=root/'run.sh';shell.write_text(text)
        result=subprocess.run(['bash',str(shell),'v2-dr-standby-qualification'],text=True,capture_output=True,timeout=15,
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',DR_STANDBY_FAULT=fault))
        return result,(root/'calls').read_text().splitlines(),context.read_text()

    def test_completion_requires_two_points_both_baselines_and_preserved_foreign_context(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls,context=self.shell(directory)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertIn('LONGHORN_V2_DR_STANDBY_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertEqual('foreign-concurrent-context',context)
            self.assertEqual(2,calls.count('longhorn-v2-dr-store.py target-baseline'))
            self.assertLess(calls.index('longhorn-v2-disaster.py cleanup'),calls.index('longhorn-v2-install.py relay-clean'))

    def test_incomplete_business_cleanup_and_baseline_drift_never_emit_pass(self):
        for fault in ('product','missing-marker','one-point','cleanup','source-boundary','target-baseline'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.shell(directory,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DR_STANDBY_QUALIFICATION_PASS',result.stdout)
                self.assertEqual('foreign-concurrent-context',context)
                self.assertIn('longhorn-v2-disaster.py cleanup',calls)
                self.assertIn('longhorn-v2-dr-target.py relay-clean',calls)


if __name__=='__main__':unittest.main()
