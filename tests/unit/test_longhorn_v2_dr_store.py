import base64
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result

gate=module('dr_store_contract',ROOT/'tests/helpers/longhorn-v2-dr-store.py')
backups=module('dr_store_binding',ROOT/'tests/helpers/longhorn-backups.py')


class ConsumerContracts(unittest.TestCase):
    def fixture(self,directory):
        root=Path(directory);source_dir=root/'source';source_dir.mkdir();(source_dir/'cas').mkdir()
        (source_dir/'server.env').write_text('MINIO_ROOT_USER=fixture-user\nMINIO_ROOT_PASSWORD=fixture-password\n')
        (source_dir/'cas/ca.crt').write_text('fixture-ca')
        source={'run_id':'source-run','name':'backup-fixture','bucket':'source-bucket','container_id':'container'}
        target_owner={'run_id':'target-run','cluster_uid':'target-cluster','namespace_uid':'target-namespace'}
        store=SimpleNamespace(directory=source_dir,endpoint='https://192.168.47.130:23621',
            lab=SimpleNamespace(owner=lambda:{'cluster_uid':'source-cluster'}),
            receipt=lambda:copy.deepcopy(source),inspect=Mock(return_value={'State':{'Running':True}}),cleanup=Mock())
        objects={'backuptargets.longhorn.io':{'metadata':{'uid':'target','resourceVersion':'2'},
            'spec':{'backupTargetURL':'','credentialSecret':'','pollInterval':'5m0s'},'status':{'available':False}},
            'namespace':{'metadata':{'uid':'target-namespace'}},'secret':None,
            'volumes.longhorn.io':{'items':[]}}
        resources=[];calls=[]
        def get(kind,name='',namespace=''):
            return {'items':copy.deepcopy(resources)} if kind in backups.BACKUP_RESOURCES else copy.deepcopy(objects.get(kind))
        def private(path,obj):path.write_text(json.dumps(obj))
        def kubectl(*args,**kwargs):
            calls.append(args)
            if args[0]=='create':
                obj=json.loads(kwargs['stdin']);obj['metadata'].update(uid='credential',resourceVersion='1')
                objects['secret']=obj;return SimpleNamespace(stdout=json.dumps(obj))
            self.assertIn('patch',args)
            patches=json.loads(kwargs['stdin']);self.assertEqual('target',patches[0]['value'])
            self.assertEqual(objects['backuptargets.longhorn.io']['metadata']['resourceVersion'],patches[1]['value'])
            objects['backuptargets.longhorn.io']['spec']=patches[2]['value']
            objects['backuptargets.longhorn.io']['status']['available']=False
            return SimpleNamespace(stdout='{}')
        def remove(obj):
            self.assertEqual(objects['secret']['metadata'],obj['metadata']);objects['secret']=None
        lab=SimpleNamespace(DIRECTORY=root,owner=lambda:copy.deepcopy(target_owner),get=get,write_private=private,
            kubectl=kubectl,remove=Mock(side_effect=remove),
            wait=lambda label,timeout,predicate:self.assertTrue(predicate()),restore_request_input=Mock())
        return gate.Consumer(lab,store,backups),lab,store,source,target_owner,objects,resources,calls

    def test_independent_identity_binds_both_clusters_and_exact_shared_server(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer,lab,store,source,owner,_,_,_=self.fixture(directory)
            consumer.initialize();row=consumer.receipt()
            self.assertEqual('source-cluster',row['source_cluster_uid']);self.assertEqual('target-cluster',row['cluster_uid'])
            for key in ('run_id','cluster_uid','namespace_uid','source_run_id','container_id','bucket','endpoint'):
                wrong=copy.deepcopy(row);wrong[key]='replacement';consumer.record.write_text(json.dumps(wrong))
                with self.subTest(key=key),self.assertRaises(ValueError):consumer.receipt()
            consumer.record.write_text(json.dumps(row));source['container_id']='replacement'
            with self.assertRaises(ValueError):consumer.cleanup()
            store.cleanup.assert_not_called();lab.remove.assert_not_called()

    def test_initialize_refuses_existing_journal_missing_server_and_same_owner(self):
        for fault in ('previous','missing','same-owner'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                consumer,lab,store,source,owner,_,_,_=self.fixture(directory)
                if fault=='previous':consumer.record.write_text('{}')
                elif fault=='missing':store.inspect.return_value=None
                else:owner['run_id']=source['run_id']
                with self.assertRaises(ValueError):consumer.initialize()
                lab.remove.assert_not_called();store.cleanup.assert_not_called()

    def test_real_binding_uses_target_credential_same_bucket_and_direct_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            consumer,lab,store,source,_,objects,_,calls=self.fixture(directory)
            consumer.initialize();config=consumer.product_configuration()
            self.assertEqual({'longhorn_backup_target':'s3://source-bucket@us-east-1/',
                'longhorn_backup_secret':'backup-fixture'},config)
            secret=objects['secret'];data={k:base64.b64decode(v).decode() for k,v in secret['data'].items()}
            self.assertEqual(store.endpoint,data['AWS_ENDPOINTS'])
            self.assertEqual('fixture-ca',data['AWS_CERT'])
            self.assertEqual('target-run',secret['metadata']['labels']['kubeauto.io/longhorn-backup-run'])
            objects['backuptargets.longhorn.io']['spec'].update(consumer.receipt()['product_binding']['fixture_spec'])
            objects['backuptargets.longhorn.io']['status']['available']=True
            consumer.cleanup()
            self.assertFalse(consumer.record.exists());self.assertIsNone(objects['secret'])
            store.cleanup.assert_not_called();self.assertEqual(2,len(calls))

    def test_live_consumers_replacement_or_lost_create_response_preserve_backend(self):
        for fault in ('consumer','volume','target','credential','lost-create'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                consumer,lab,store,source,_,objects,resources,calls=self.fixture(directory)
                consumer.initialize();consumer.product_configuration()
                if fault=='consumer':resources.append({'metadata':{'uid':'backup'}})
                elif fault=='volume':objects['volumes.longhorn.io']['items'].append({'metadata':{'uid':'restoring'}})
                elif fault=='target':objects['backuptargets.longhorn.io']['metadata']['uid']='replacement'
                elif fault=='credential':objects['secret']['metadata']['uid']='replacement'
                else:
                    row=consumer.receipt();row['product_binding'].pop('secret_uid');consumer.record.write_text(json.dumps(row))
                lab.backup_store=lambda:store
                with patch.object(gate,'Consumer',return_value=consumer),self.assertRaises(ValueError):
                    gate.cleanup(lab,lab,backups)
                self.assertTrue(consumer.record.exists());store.cleanup.assert_not_called();lab.remove.assert_not_called()

    def test_qualification_requires_actual_tls_result_and_product_target_status(self):
        for fault in ('none','tls-marker','target-unavailable','replacement'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                consumer,lab,store,source,_,objects,_,_=self.fixture(directory)
                consumer.initialize()
                expected='LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false'
                lab.remote=Mock(return_value=SimpleNamespace(stdout='' if fault=='tls-marker' else expected))
                lab.tls_fixture=lambda:{'ca.crt':'wrong-ca'}
                def product(pid,requests,backup_configuration):
                    self.assertEqual(42,pid);self.assertEqual([],requests)
                    self.assertEqual('s3://source-bucket@us-east-1/',backup_configuration['longhorn_backup_target'])
                    objects['backuptargets.longhorn.io']['spec'].update(consumer.receipt()['product_binding']['fixture_spec'])
                    objects['backuptargets.longhorn.io']['status'].update(available=fault!='target-unavailable',conditions=None)
                    if fault=='replacement':objects['secret']['metadata']['uid']='replacement'
                lab.run_requests=Mock(side_effect=product)
                if fault=='none':consumer.qualify(42);lab.run_requests.assert_called_once()
                else:
                    with self.assertRaises(ValueError):consumer.qualify(42)
                    if fault=='tls-marker':lab.run_requests.assert_not_called()


class DirectTLSContracts(unittest.TestCase):
    def test_real_https_accepts_original_ca_and_rejects_wrong_ca_and_connection_errors(self):
        fixture=backups.tls_fixture(endpoints=backups.V2_ENDPOINTS)
        wrong=backups.tls_fixture(endpoints=backups.V2_ENDPOINTS)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,value in fixture.items():(root/name).write_text(value)
            server=HTTPServer(('127.0.0.1',0),Handler)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'tls.crt',root/'tls.key')
            server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                row={'url':'https://127.0.0.1:'+str(server.server_port)+'/minio/health/live',
                     'ca':fixture['ca.crt'],'wrong_ca':wrong['ca.crt']}
                result=subprocess.run([sys.executable,'-c',gate.tls_probe_script()],input=json.dumps(row),
                    text=True,capture_output=True,timeout=15)
                self.assertEqual(0,result.returncode,result.stderr)
                self.assertIn('wrong_ca_refused=true',result.stdout)
                row['wrong_ca']=fixture['ca.crt']
                result=subprocess.run([sys.executable,'-c',gate.tls_probe_script()],input=json.dumps(row),
                    text=True,capture_output=True,timeout=15)
                self.assertNotEqual(0,result.returncode);self.assertNotIn('TLS_PASS',result.stdout)
            finally:server.shutdown();server.server_close();thread.join(timeout=3)
            row['wrong_ca']=wrong['ca.crt']
            result=subprocess.run([sys.executable,'-c',gate.tls_probe_script()],input=json.dumps(row),
                text=True,capture_output=True,timeout=15)
            self.assertNotEqual(0,result.returncode);self.assertNotIn('TLS_PASS',result.stdout)



class StoreShellContracts(unittest.TestCase):
    def test_fixed_entry_allowlists_live_route_and_static_checks_belong_to_shared_store(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-v2-dr-store-qualification) longhorn_launch v2-dr-store-qualification LONGHORN_V2_DR_STORE_QUALIFICATION_PASS',runner)
        self.assertEqual(2,runner.count('v2-dr-target-install-recovery|v2-dr-store-qualification|'))
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-dr-store.sh" "$MODE"',live)
        static=(ROOT/'tests/helpers/longhorn-static.sh').read_text()
        self.assertIn('tests/helpers/longhorn-v2-dr-store.py',static)
        self.assertIn('tests.unit.test_longhorn_v2_dr_store -v',static)

    def shell(self,directory,fault=''):
        root=Path(directory)
        for name in ('.venv/bin','tests/helpers','kube-bin','clusters/longhorn-v2-dr-target'):(root/name).mkdir(parents=True,exist_ok=True)
        context=root/'control-config';context.write_text('original-context')
        driver=root/'.venv/bin/python';driver.write_text('''#!/usr/bin/env python3
import os,signal,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];helper,mode=sys.argv[1:3];name=Path(helper).name
with (root/'calls').open('a') as f:f.write(name+' '+mode+'\\n')
fault=os.environ.get('DR_STORE_FAULT','')
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/(name+'.ready')).touch()
 while True:time.sleep(.01)
if mode=='source-boundary' or mode=='target-baseline':
 marker='LONGHORN_V2_DR_SOURCE_BOUNDARY' if mode=='source-boundary' else 'LONGHORN_V2_DR_TARGET_STORE_BASELINE'
 count=(root/'calls').read_text().count(name+' '+mode+'\\n')
 changed=count>1 and fault==mode+'-drift'
 print(marker+' sha256='+('changed' if changed else 'original'))
elif mode=='qualify':
 deadline=time.monotonic()+3
 while len(list(root.glob('*.ready')))<2 and time.monotonic()<deadline:time.sleep(.01)
 if len(list(root.glob('*.ready')))!=2:sys.exit(8)
 # A concurrent owner updates the shared context during business; this gate must preserve it.
 (root/'control-config').write_text('foreign-concurrent-context')
 if fault=='qualify':sys.exit(7)
 markers=[
 'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
 'LONGHORN_V2_DR_SOURCE_BACKUP_TRANSPORT_ABSENT shared_server=retained',
 'LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false',
 'LONGHORN_V2_DR_TARGET_SHARED_STORE_VERIFIED target_available=true independent_endpoint=true',
 'LONGHORN_V2_DR_TARGET_STORE_CONSUMER_CLEAN_PASS shared_server=retained',
 'LONGHORN_BACKUP_STORE_CLEAN_PASS']
 for marker in markers:
  if fault!='missing-marker' or 'SHARED_STORE_VERIFIED' not in marker:print(marker)
elif mode=='cleanup' and fault=='cleanup':sys.exit(9)
''');driver.chmod(0o755)
        k=root/'kube-bin/kubectl';k.write_text('#!/bin/sh\nexit 0\n');k.chmod(0o755)
        text=(ROOT/'tests/helpers/longhorn-v2-dr-store.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root)).replace('control_config=/root/.kube/config','control_config='+str(context))
        shell=root/'run.sh';shell.write_text(text)
        import os
        result=subprocess.run(['bash',str(shell),'v2-dr-store-qualification'],text=True,capture_output=True,timeout=15,
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',DR_STORE_FAULT=fault))
        return result,(root/'calls').read_text().splitlines(),context.read_text()

    def test_success_requires_both_baselines_all_business_markers_and_foreign_context_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            result,calls,context=self.shell(directory)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertIn('LONGHORN_V2_DR_STORE_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertEqual('foreign-concurrent-context',context)
            self.assertEqual(2,calls.count('longhorn-v2-dr-target.py source-boundary'))
            self.assertEqual(2,calls.count('longhorn-v2-dr-store.py target-baseline'))
            cleanup=calls.index('longhorn-v2-dr-store.py cleanup')
            self.assertLess(cleanup,calls.index('longhorn-v2-install.py relay-clean'))
            self.assertLess(cleanup,calls.index('longhorn-v2-dr-target.py relay-clean'))

    def test_failed_product_marker_cleanup_or_either_baseline_never_pass(self):
        for fault in ('qualify','missing-marker','cleanup','source-boundary-drift','target-baseline-drift'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,calls,context=self.shell(directory,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DR_STORE_QUALIFICATION_PASS',result.stdout)
                self.assertEqual('foreign-concurrent-context',context)
                self.assertIn('longhorn-v2-dr-store.py cleanup',calls)
                self.assertIn('longhorn-v2-install.py relay-clean',calls)
                self.assertIn('longhorn-v2-dr-target.py relay-clean',calls)

class TargetBaselineContracts(unittest.TestCase):
    def test_requires_current_inputs_original_core_and_disk_and_zero_foreign_consumers(self):
        import hashlib
        files=('roles/longhorn/files/longhorn.py','tests/helpers/longhorn-v2-dr-target.py',
               'tests/helpers/longhorn-v2-dr-target-install.py','tests/helpers/longhorn-lab.py')
        for fault in ('none','input','core','disk','journal','volume','backup','business','credential','pv','target'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);cluster=root/'clusters/target';(cluster/'yml/longhorn').mkdir(parents=True)
                receipt=cluster/'yml/longhorn/block-disks.json';receipt.write_text('{}')
                (cluster/'config.yml').write_text('fixture config')
                state={'run_id':'target','cluster_uid':'cluster','namespace_uid':'namespace','node_uids':{'node':'uid'},'raw_disks':{'node':'disk'}}
                inputs={}
                for name in files:
                    path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
                    inputs[name]=hashlib.sha256(path.read_bytes()).hexdigest()
                evidence={'status':'pass','rc':0,'failure_markers':0,'frozen_inputs':inputs,
                    'baseline_identity':copy.deepcopy(state),'native_block_receipt_sha256':hashlib.sha256(receipt.read_bytes()).hexdigest()}
                p=root/'tests/evidence/longhorn-v2-dr-target-install-qualification-20261020.json'
                p.parent.mkdir(parents=True);p.write_text(json.dumps(evidence))
                if fault=='input':(root/files[0]).write_text('changed')
                elif fault=='core':state['cluster_uid']='replacement'
                elif fault=='disk':receipt.write_text('changed')
                elif fault=='journal':(cluster/'longhorn-dr-backup-consumer.json').write_text('{}')
                def get(kind,name='',namespace=''):
                    if kind=='backuptargets.longhorn.io':return {'spec':{'backupTargetURL':'occupied' if fault=='target' else '', 'credentialSecret':''}}
                    if kind=='pv':return {'items':[{'spec':{'csi':{'driver':'driver.longhorn.io'}}}]} if fault=='pv' else {'items':[]}
                    if kind=='volumes.longhorn.io' and fault=='volume':return {'items':[{}]}
                    if kind in backups.BACKUP_RESOURCES and fault=='backup':return {'items':[{}]}
                    if kind=='namespaces' and fault=='business':return {'items':[{'metadata':{'name':'longhorn-business-foreign'}}]}
                    if kind=='secrets' and fault=='credential':return {'items':[{'metadata':{'labels':{'kubeauto.io/longhorn-backup-run':'foreign'}}}]}
                    return {'items':[]}
                lab=SimpleNamespace(DIRECTORY=cluster,owner=lambda:state,get=get);install=SimpleNamespace(health=Mock())
                with patch.object(gate,'BASE',root):
                    if fault=='none':gate.target_baseline(install,lab,backups);install.health.assert_called_once_with(lab)
                    else:
                        with self.assertRaises(ValueError):gate.target_baseline(install,lab,backups)


if __name__=='__main__':unittest.main()
