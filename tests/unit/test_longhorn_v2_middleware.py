"""LONGHORN-51 contracts: shared native SQL behavior, V2 ownership and recovery."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml

ROOT=Path(__file__).resolve().parents[2]
def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
GATE=load(ROOT/'tests/helpers/longhorn-v2-middleware.py','v2_middleware_contract')
V1=load(ROOT/'tests/unit/test_longhorn_v1_middleware.py','v1_middleware_fixture')


def translated(value):
    text=json.dumps(value)
    for old,new in (('longhorn-node-212','longhorn-node-146'),('longhorn-node-165','longhorn-node-147'),('longhorn-node-238','longhorn-node-148')):text=text.replace(old,new)
    return json.loads(text)


def product_config():
    c=yaml.safe_load((ROOT/'roles/longhorn/defaults/main.yml').read_text())
    c.update(longhorn_install=True,longhorn_grpc_tls_required=False,longhorn_data_engines=['v2'],longhorn_storage_nodes=[],
        longhorn_inventory_nodes=list(GATE.NODES),longhorn_client_nodes=list(GATE.NODES),longhorn_v2_nodes=list(GATE.NODES),
        longhorn_block_disks={a:[{'name':'raw','path':'/dev/disk/by-id/wwn-'+a,'disk_driver':'aio','identity':'wwn-'+a,'storage_reserved':1024**3}] for a in GATE.NODES})
    for sc in c['longhorn_storage_classes']:sc['data_engine']='v2'
    return c


def business_log():
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(V1,'GATE',GATE.SHARED):row,lab,monitor,sql,client,trace=V1.database_fixture(directory)
        native=translated(row['bindings']);row['bindings']=native;row['storage_class_uid']='db-class'
        config=product_config();original=yaml.safe_dump(config);(lab.DIRECTORY/'config.yml').write_text(original);row['allowed_inputs']=[original]
        native_get=lab.get;lab.get=lambda kind,*a,**kw:None if kind=='storageclass' else native_get(kind,*a,**kw)
        lab.supervise_core=Mock()
        row['monitoring_identity']['claims']={str(i):{'uid':'m-'+str(i),'pv':'mpv-'+str(i),'pv_uid':'mpvuid-'+str(i)} for i in range(6)}
        monitor.receipt=lambda l:copy.deepcopy(row['monitoring_identity'])
        raw={a:{'identity':'hardware-'+a,'path':'/dev/disk/by-id/'+a} for a in GATE.NODES}
        identity={'data_engine':'v2','core':{'run_id':'unit-run','cluster_uid':'cluster','namespace_uid':'longhorn',
            'node_uids':{'longhorn-node-'+a.rsplit('.',1)[1]:'node-'+a for a in GATE.NODES},'raw_disks':raw},
            'native_block_receipt_sha256':'b'*64,'disks':{'longhorn-node-'+a.rsplit('.',1)[1]:{'disk_uuid':'disk-'+a,'hardware_identity':r['identity'],'path':r['path']} for a,r in raw.items()},
            'database_uid':row['pxc_uid'],'database_namespace_uid':row['database_namespace_uid'],'operator_namespace_uid':row['operator_namespace_uid'],
            'statefulset_uid':'sts','storage_class_uid':'db-class','database_bindings':{n:{k:b[k] for k in ('claim_uid','pv','pv_uid','volume','volume_uid')} for n,b in native.items()},
            'monitoring_namespace_uid':'monitor-ns','monitoring_class_uid':'monitor-sc','monitoring_bindings':{n:dict(b,volume='monitor-'+n,volume_uid='monitoruid-'+n,data_engine='v2') for n,b in row['monitoring_identity']['claims'].items()}}
        out=io.StringIO()
        with patch.object(GATE.SHARED,'receipt',return_value=row),patch.object(GATE.SHARED,'monitor',return_value=monitor), \
                patch.object(GATE.SHARED,'pxc',return_value={'status':{'state':'ready'}}),patch.object(GATE.SHARED,'bindings',return_value=native), \
                patch.object(GATE.SHARED,'sql',side_effect=sql),patch.object(GATE.SHARED,'client',side_effect=client),patch.object(GATE,'identity',return_value=identity),contextlib.redirect_stdout(out):
            GATE.business(lab,123)
        return out.getvalue(),trace


def disk_fixture(directory):
    base=Path(directory);(base/'yml/longhorn').mkdir(parents=True)
    owner={'run_id':'run','cluster_uid':'cluster','namespace_uid':'ns','node_uids':{},'raw_disks':{}};receipt={'cluster_uid':'cluster','namespace_uid':'ns','nodes':{}};nodes={}
    for address in GATE.NODES:
        name='longhorn-node-'+address.rsplit('.',1)[1];raw={'identity':'wwn-'+address,'path':'/dev/disk/by-id/'+address}
        owner['raw_disks'][address]=raw;owner['node_uids'][name]='k8s-'+name
        receipt['nodes'][address]={'name':name,'longhorn_node_uid':'lh-'+name,'kubernetes_node_uid':'k8s-'+name,
            'disks':{'owned':{'disk_uuid':'disk-'+name,'path':raw['path'],'hardware_identity':raw['identity'],'disk_driver':'aio'}}}
        nodes[name]={'metadata':{'uid':'lh-'+name},'spec':{'allowScheduling':True,'disks':{'owned':{'path':raw['path'],'diskType':'block','diskDriver':'aio','allowScheduling':True,'storageReserved':0}}},
            'status':{'diskStatus':{'owned':{'diskUUID':'disk-'+name,'diskType':'block','diskDriver':'aio','storageMaximum':100*1024**3,'storageAvailable':90*1024**3,'storageScheduled':0}}}}
    (base/'yml/longhorn/block-disks.json').write_text(json.dumps(receipt))
    return SimpleNamespace(DIRECTORY=base,owner=lambda:owner,get=lambda kind,name,*args:nodes[name]),owner,receipt,nodes


class V2MiddlewareContracts(unittest.TestCase):
    def test_v1_default_is_unchanged_and_v2_product_template_uses_explicit_raw_engine_class(self):
        self.assertEqual('v1',V1.GATE.DATA_ENGINE);self.assertEqual('v2',GATE.SHARED.DATA_ENGINE)
        source=yaml.safe_load((ROOT/'conf/config.yml').read_text());source.update(mysql_install='no',longhorn_data_engines=['v2'],prom_install='yes',longhorn_monitoring_enabled=True)
        config=GATE.SHARED.desired(yaml.safe_dump(source),'db','operator');sc=config['longhorn_storage_classes'][-1]
        self.assertEqual((GATE.CLASS,'v2',3,'Delete'),(sc['name'],sc['data_engine'],sc['replicas'],sc['reclaim_policy']))
        self.assertEqual(('2Gi','4Gi','1'),(config['mysql_pxc_memory_request'],config['mysql_pxc_memory_limit'],config['mysql_pxc_cpu_request']))
        m=GATE.monitoring();env=m.jinja2.Environment(undefined=m.jinja2.StrictUndefined);env.filters.update(bool=bool,quote=json.dumps)
        obj=yaml.safe_load(env.from_string((ROOT/'roles/cluster-addon/templates/percona-pxc/cluster.yaml.j2').read_text()).render(**config))
        self.assertEqual(GATE.CLASS,obj['spec']['pxc']['volumeSpec']['persistentVolumeClaim']['storageClassName'])
        self.assertTrue(obj['spec']['pxc']['image'].endswith(':8.4.8-8.1'));self.assertEqual('1.20.0',str(obj['spec']['crVersion']))
        source['longhorn_data_engines']=['v1']
        with self.assertRaises(ValueError):GATE.SHARED.desired(yaml.safe_dump(source),'db','operator')

    def test_raw_receipt_original_disk_driver_identity_uuid_and_node_are_required(self):
        for mutation in ('none','identity','uuid','driver','filesystem','node','foreign'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                lab,state,receipt,nodes=disk_fixture(directory);node=next(iter(nodes.values()))
                if mutation=='identity':state['raw_disks'][GATE.NODES[0]]['identity']='foreign'
                if mutation=='uuid':node['status']['diskStatus']['owned']['diskUUID']='foreign'
                if mutation=='driver':node['spec']['disks']['owned']['diskDriver']='nvme'
                if mutation=='filesystem':node['spec']['disks']['owned']['diskType']='filesystem'
                if mutation=='node':node['metadata']['uid']='foreign'
                if mutation=='foreign':state['cluster_uid']='foreign'
                if mutation=='none':self.assertEqual(3,len(GATE.disk_rows(lab)))
                else:
                    with self.assertRaises(ValueError):GATE.disk_rows(lab)

    def test_actual_admission_keeps_existing_pods_and_allocatable_cpu_memory_and_runtime_stop_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            source=yaml.safe_load((ROOT/'conf/config.yml').read_text());source.update(mysql_install='no',longhorn_data_engines=['v2'],prom_install='yes',longhorn_monitoring_enabled=True)
            lab,state,receipt,nodes=disk_fixture(directory);(lab.DIRECTORY/'config.yml').write_text(yaml.safe_dump(source));native_get=lab.get
            existing=[{'spec':{'nodeName':'longhorn-node-146','containers':[{'resources':{'requests':{'cpu':'1','memory':'2Gi'}}}]},'status':{'phase':'Running'}}]
            kube=[{'metadata':{'name':'longhorn-node-'+a.rsplit('.',1)[1],'labels':{}},'spec':{},'status':{'allocatable':{'cpu':'8','memory':'6Gi'}}} for a in GATE.NODES]
            lab.get=lambda kind,*args,**kw:{'items':kube} if kind=='nodes' else {'items':existing} if kind=='pods' else {'value':'25'} if kind=='settings.longhorn.io' else native_get(kind,*args)
            m=GATE.monitoring();m.verify=Mock();m.host_memory=Mock(return_value={a:{'MemAvailable':4*1024**3} for a in GATE.NODES})
            real=m.placement;m.placement=Mock(side_effect=real)
            with patch.object(GATE,'monitoring',return_value=m):
                GATE.admission(lab,1);self.assertIs(existing,m.placement.call_args.args[1])
                m.host_memory.return_value[GATE.NODES[0]]['MemAvailable']=1024**3
                with self.assertRaisesRegex(ValueError,'available memory'):GATE.admission(lab,1)
                m.host_memory.return_value[GATE.NODES[0]]['MemAvailable']=4*1024**3
                kube[0]['status']['allocatable']['cpu']='1'
                with self.assertRaisesRegex(ValueError,'does not fit'):GATE.admission(lab,1)
                kube[0]['status']['allocatable']['cpu']='8';next(iter(nodes.values()))['status']['diskStatus']['owned']['storageAvailable']=26*1024**3
                with self.assertRaisesRegex(ValueError,'raw-disk stop budget'):GATE.admission(lab,1)

    def test_shared_sql_dump_independent_restore_three_local_members_and_v2_identity_execute(self):
        text,trace=business_log();result=GATE.verify_log(text)
        self.assertEqual('v2',result['data_engine']);self.assertEqual(6,len(result['monitoring_bindings']))
        self.assertEqual(1024,result['native_logical_recovery']['source']['rows']);self.assertTrue(result['native_logical_recovery']['isolation_verified'])
        self.assertEqual(3,len(result['member_persistence']['members']));self.assertTrue(any(isinstance(r,tuple) and r[1][0]=='mysqldump' for r in trace))

    def test_marker_only_wrong_engine_foreign_identity_missing_monitor_and_sql_tampering_fail(self):
        text,_=business_log()
        for mutation in ('marker','engine','changed','monitor','raw','controller','isolation','member','refusal','failure'):
            rows=text.splitlines()
            if mutation=='marker':rows=['LONGHORN_V2_MIDDLEWARE_BUSINESS_PASS']
            for i,line in enumerate(rows):
                if line.startswith('LONGHORN_V2_MIDDLEWARE_IDENTITY '):
                    p=json.loads(line.split(' ',1)[1]);b=p['before']
                    if mutation=='engine':b['data_engine']='v1'
                    if mutation=='changed':p['after']['database_namespace_uid']='foreign'
                    if mutation=='monitor':b['monitoring_bindings'].pop('0')
                    if mutation=='raw':next(iter(b['disks'].values()))['hardware_identity']='foreign'
                    if mutation=='controller':b['statefulset_uid']='foreign'
                    if mutation in ('engine','monitor','raw','controller'):p['after']=copy.deepcopy(b)
                    rows[i]='LONGHORN_V2_MIDDLEWARE_IDENTITY '+json.dumps(p)
                if mutation=='isolation':rows[i]=rows[i].replace('"isolation_verified": true','"isolation_verified": false')
                if mutation=='member':rows[i]=rows[i].replace('"replacement_uid": "new-pod"','"replacement_uid": "old-pod"')
            if mutation=='refusal':rows=[l for l in rows if not l.startswith('LONGHORN_V2_MIDDLEWARE_REFUSALS ')]
            if mutation=='failure':rows.append('LONGHORN_FIRST_FAILURE phase=unit')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):GATE.verify_log('\n'.join(rows))

    def test_refusal_inputs_pass_real_static_validate_and_reach_exact_native_guards(self):
        product=load(ROOT/'roles/longhorn/files/longhorn.py','middleware51_product')
        nodes={a:{'metadata':{'name':'longhorn-node-'+a.rsplit('.',1)[1]}} for a in GATE.NODES}
        def get(kind,*args,**kwargs):
            if kind=='crd':return {'metadata':{'name':'volumes.longhorn.io'}}
            if kind=='volumes.longhorn.io':return {'items':[{'spec':{'dataEngine':'v2'}}]}
            if kind=='instancemanagers.longhorn.io':return {'items':[{'spec':{'dataEngine':'v2','nodeID':'longhorn-node-148'},'status':{'instanceReplicas':{'replica':'running'}}}]}
            return {'items':[]}
        api=SimpleNamespace(get=get)
        for label,config,reason in GATE.refusal_inputs(product_config()):
            with self.subTest(label=label):
                product.validate(config)
                with self.assertRaisesRegex(ValueError,'^'+reason+'$'):product.check_engine_changes(config,api,nodes)

    def test_refusal_failure_restores_original_product_input_before_propagating(self):
        with tempfile.TemporaryDirectory() as directory:
            lab=SimpleNamespace(DIRECTORY=Path(directory),get=lambda *args:None,supervise_core=Mock(side_effect=[ValueError('precise-refusal-missing'),None]))
            original=yaml.safe_dump(product_config());(lab.DIRECTORY/'config.yml').write_text(original)
            row={'schema':'source','restored_schema':'restored'}
            def apply(lab,row,text):(lab.DIRECTORY/'config.yml').write_text(text)
            with patch.object(GATE,'identity',return_value={'original':'uid'}),patch.object(GATE.SHARED,'apply',side_effect=apply),patch.object(GATE.SHARED,'sql',return_value='\n'.join(str(i) for i in range(1025))):
                with self.assertRaisesRegex(ValueError,'precise-refusal-missing'):GATE.refusals(lab,1,row)
            self.assertEqual(original,(lab.DIRECTORY/'config.yml').read_text())
            self.assertEqual([{'expected_refusal':'cannot disable an engine while its volumes remain'},{}],
                [c.kwargs for c in lab.supervise_core.call_args_list])

    def test_refusals_restore_real_rendered_input_before_health_and_sql_readback(self):
        product=load(ROOT/'roles/longhorn/files/longhorn.py','middleware51_restore_product')
        nodes={a:{'metadata':{'name':'longhorn-node-'+a.rsplit('.',1)[1]}} for a in GATE.NODES}
        def get(kind,*args,**kwargs):
            if kind=='settings.longhorn.io':
                settings=product.chart_values(product_config())['defaultSettings']
                return {'value':json.dumps(next(v for k,v in settings.items() if k.lower()==args[0].replace('-','')))}
            if kind=='crd':return {'metadata':{'name':'volumes.longhorn.io'}}
            if kind=='volumes.longhorn.io':return {'items':[{'spec':{'dataEngine':'v2'}}]}
            if kind=='instancemanagers.longhorn.io':return {'items':[{'spec':{'dataEngine':'v2','nodeID':'longhorn-node-148'},'status':{'instanceReplicas':{'replica':'running'}}}]}
            return {'items':[]}
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);original=yaml.safe_dump(product_config());(base/'config.yml').write_text(original)
            derived=base/'input.json';derived.write_text(json.dumps(product_config()));trace=[]
            row={'allowed_inputs':[original],'schema':'source','restored_schema':'restored'}
            lab=SimpleNamespace(DIRECTORY=base,get=lambda *args:None)
            def supervise(pid,step,**kwargs):
                self.assertEqual('longhorn-setup',step)
                config=yaml.safe_load((base/'config.yml').read_text());product.validate(config)
                derived.write_text(json.dumps(config));trace.append(kwargs.get('expected_refusal','restore'))
                if 'expected_refusal' in kwargs:
                    with self.assertRaisesRegex(ValueError,'^'+kwargs['expected_refusal']+'$'):
                        product.check_engine_changes(config,SimpleNamespace(get=get),nodes)
                else:product.check_engine_changes(config,SimpleNamespace(get=get),nodes)
            lab.supervise_core=supervise
            def health(*args):
                self.assertEqual(product_config(),json.loads(derived.read_text()))
                return {'original':'identity'}
            def apply(lab,row,text):(base/'config.yml').write_text(text)
            with patch.object(GATE,'identity',side_effect=health),patch.object(GATE.SHARED,'apply',side_effect=apply),patch.object(GATE.SHARED,'sql',return_value='\n'.join(str(i) for i in range(1025))):
                GATE.refusals(lab,1,row)
            self.assertEqual(['cannot disable an engine while its volumes remain','restore',
                'cannot disable a V2 node with active native instances','restore'],trace)

    def test_cleanup_restores_derived_input_before_shared_cleanup_and_preserves_refusal_boundaries(self):
        for failure in ('none','foreign','symlink','external','restore','absent'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);original=yaml.safe_dump(product_config());current='rejected input'
                (base/'config.yml').write_text(current)
                owner={'run_id':'run','cluster_uid':'cluster','namespace_uid':'namespace'}
                row=dict(owner,original_input=original,allowed_inputs=[current,original])
                if failure=='foreign':row['run_id']='foreign'
                if failure=='external':(base/'config.yml').write_text('external')
                receipt=base/GATE.RECORD
                if failure!='absent':receipt.write_text(json.dumps(row))
                if failure=='symlink':receipt.rename(base/'target');receipt.symlink_to(base/'target')
                derived=base/'input.json';derived.write_text('{}');trace=[]
                def supervise(*args):
                    trace.append('restore')
                    if failure=='restore':raise ValueError('restore failed')
                    derived.write_text(json.dumps(yaml.safe_load((base/'config.yml').read_text())))
                lab=SimpleNamespace(DIRECTORY=base,owner=lambda:owner,supervise_core=supervise,
                    write_private=lambda p,r:p.write_text(json.dumps(r)))
                def shared_cleanup(lab,pid):
                    trace.append('cleanup')
                    if failure!='absent':self.assertEqual(product_config(),json.loads(derived.read_text()))
                with patch.object(GATE.SHARED,'cleanup',side_effect=shared_cleanup):
                    if failure in ('none','absent'):GATE.cleanup(lab,1)
                    else:
                        with self.assertRaises(ValueError):GATE.cleanup(lab,1)
                self.assertEqual(['restore','cleanup'] if failure=='none' else ['cleanup'] if failure=='absent' else ['restore'] if failure=='restore' else [],trace)
                if failure!='absent':self.assertTrue(receipt.exists())

    def test_storage_class_replacement_or_v1_cannot_be_accepted(self):
        sc={'metadata':{'uid':'original'},'provisioner':'driver.longhorn.io','parameters':{'dataEngine':'v2','numberOfReplicas':'3'},'reclaimPolicy':'Delete'}
        lab=SimpleNamespace(get=lambda *args:sc);GATE.class_identity(lab,GATE.CLASS,'original',3)
        for field,value in (('dataEngine','v1'),('numberOfReplicas','1')):
            with patch.dict(sc['parameters'],{field:value}):
                with self.assertRaises(ValueError):GATE.class_identity(lab,GATE.CLASS,'original',3)
        with self.assertRaises(ValueError):GATE.class_identity(lab,GATE.CLASS,'replacement',3)

    def test_actual_monitoring_six_original_claims_pvs_and_v2_volumes_cannot_be_rebound(self):
        receipt={'namespace':'monitor','storage_class_uid':'msc','claims':{str(i):{'uid':'mc'+str(i),'pv':'mpv'+str(i),'pv_uid':'mpvu'+str(i)} for i in range(6)}}
        objects={}
        for name,s in receipt['claims'].items():
            objects['pvc',name]={'metadata':{'uid':s['uid']},'spec':{'storageClassName':GATE.monitoring().CLASS,'volumeName':s['pv']},'status':{'phase':'Bound'}}
            objects['pv',s['pv']]={'metadata':{'uid':s['pv_uid']},'spec':{'storageClassName':GATE.monitoring().CLASS,'claimRef':{'uid':s['uid']},'persistentVolumeReclaimPolicy':'Delete','csi':{'driver':'driver.longhorn.io','volumeHandle':'vol'+name}}}
            objects['volumes.longhorn.io','vol'+name]={'metadata':{'uid':'vu'+name},'spec':{'dataEngine':'v2','numberOfReplicas':1},'status':{'robustness':'healthy'}}
        lab=SimpleNamespace(get=lambda kind,name,*args:objects[kind,name])
        with patch.object(GATE.SHARED,'monitor_identity',return_value=receipt),patch.object(GATE,'class_identity',return_value='msc'):
            self.assertEqual(6,len(GATE.monitoring_bindings(lab,{})))
            for kind,name,section,key,value in (('volumes.longhorn.io','vol0','spec','dataEngine','v1'),('pvc','0','metadata','uid','foreign'),('pv','mpv0','metadata','uid','foreign'),('pv','mpv0','spec','persistentVolumeReclaimPolicy','Retain')):
                with self.subTest(kind=kind,key=key),patch.dict(objects[kind,name][section],{key:value}):
                    with self.assertRaises(ValueError):GATE.monitoring_bindings(lab,{})

    def test_identity_reads_original_core_namespace_controller_class_and_storage_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,state,receipt,nodes=disk_fixture(directory);lab.core_identity=Mock();lab.installation_health=Mock();original_get=lab.get
            sts={'metadata':{'uid':'sts','ownerReferences':[{'kind':'PerconaXtraDBCluster','uid':'db','controller':True}]}}
            lab.get=lambda kind,*args:sts if kind=='statefulset' else original_get(kind,*args)
            row={'pxc_uid':'db','database_namespace':'dbns','database_namespace_uid':'dbnsuid','operator_namespace_uid':'opnsuid','storage_class_uid':'sc','monitoring_identity':{'monitor_namespace_uid':'mns','storage_class_uid':'msc'}}
            bindings=translated(V1.native_bindings())
            with patch.object(GATE.SHARED,'namespace_identity',return_value={'metadata':{'uid':'ns'}}),patch.object(GATE.SHARED,'pxc',return_value={'metadata':{'uid':'db'}}),patch.object(GATE.SHARED,'bindings',return_value=bindings),patch.object(GATE,'class_identity',return_value='sc'),patch.object(GATE,'monitoring_bindings',return_value={'monitor':'original'}):
                proof=GATE.identity(lab,row);self.assertEqual('sts',proof['statefulset_uid']);self.assertEqual(state['raw_disks'],proof['core']['raw_disks']);self.assertEqual(3,len(proof['database_bindings']))
                sts['metadata']['ownerReferences'][0]['uid']='foreign'
                with self.assertRaisesRegex(ValueError,'controller ownership'):GATE.identity(lab,row)
                sts['metadata']['ownerReferences'][0]['uid']='db'
                with patch.object(GATE.SHARED,'namespace_identity',return_value=None):
                    with self.assertRaisesRegex(ValueError,'namespace disappeared'):GATE.identity(lab,row)

    def run_shell(self,mode='v2-middleware',fail='',terminate=False,foreign=False):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for d in ('.venv/bin','kube-bin','tests/helpers','control'):(base/d).mkdir(parents=True)
            context=base/'control/config';context.write_bytes(b'original-context\n')
            script=(ROOT/'tests/helpers/longhorn-v2-middleware.sh').read_text().replace('/usr/local/kubeauto',str(base)).replace('/var/tmp/kubeauto-longhorn-v2-middleware.',str(base/'run.'))
            (base/'runner.sh').write_text(script);shutil.copyfile(ROOT/'tests/helpers/run-durable-gate.sh',base/'tests/helpers/run-durable-gate.sh')
            fake=base/'.venv/bin/python';fake.write_text('''#!/usr/bin/env bash
set -eu
name="${1##*/}"; action="${2:-}"
echo "$name $action" >> "$MID_TRACE"
if [[ "$action" == relay-maintain ]]; then
 exec "$MID_PY" -c 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["MID_RELAY_READY"]).touch();time.sleep(60)'
fi
if [[ "$action" == cleanup ]]; then
 "$MID_PY" -c 'import os,time;from pathlib import Path;p=Path(os.environ["MID_RELAY_READY"]);start=time.monotonic()
while not p.exists():
 assert time.monotonic()-start<3
 time.sleep(.01)'
fi
if [[ "$name:$action" == "$MID_FAIL" ]];then exit 17;fi
if [[ "$action" == business && "$MID_FOREIGN" == yes ]];then echo foreign-context > "$MID_CONTEXT";fi
if [[ "$action" == business && "$MID_TERMINATE" == yes ]];then
 touch "$MID_READY";exec "$MID_PY" -c 'import time;time.sleep(2)'
fi
if [[ "$name:$action" == longhorn-v2-monitoring.py:setup ]];then echo LONGHORN_V2_MONITORING_SETUP_PASS;fi
if [[ "$name:$action" == longhorn-v2-middleware.py:setup ]];then echo LONGHORN_V2_MIDDLEWARE_SETUP_PASS;fi
if [[ "$name:$action" == longhorn-v2-middleware.py:business ]];then echo LONGHORN_V2_MIDDLEWARE_BUSINESS_PASS;fi
if [[ "$name:$action" == longhorn-v2-middleware.py:cleanup ]];then echo LONGHORN_V2_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS;fi
''');fake.chmod(0o755)
            kubectl=base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',MID_TRACE=str(base/'trace'),MID_FAIL=fail,MID_PY=sys.executable,MID_RELAY_READY=str(base/'relay'),MID_READY=str(base/'ready'),MID_CONTEXT=str(context),MID_TERMINATE='yes' if terminate else 'no',MID_FOREIGN='yes' if foreign else 'no')
            p=subprocess.Popen(['bash',str(base/'runner.sh'),mode],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            if terminate:
                deadline=time.monotonic()+5
                while not (base/'ready').exists() and p.poll() is None:
                    if time.monotonic()>deadline:p.kill();self.fail('business did not start')
                    time.sleep(.01)
                p.send_signal(signal.SIGTERM)
            out,err=p.communicate(timeout=10);work=list(base.glob('run.*'))
            durable={f.name:f.read_text().strip() for f in work[0].iterdir() if f.name in ('exit','finalized')} if work else {}
            return p.returncode,out,err,(base/'trace').read_text() if (base/'trace').exists() else '',durable,context.read_bytes()

    def test_shell_success_scoped_cleanup_durable_zero_and_no_shared_context_write(self):
        rc,out,err,trace,durable,context=self.run_shell();self.assertEqual(0,rc,out+err);self.assertEqual({'exit':'0','finalized':'0'},durable)
        self.assertEqual(b'original-context\n',context);self.assertIn('longhorn-v2-middleware.py verify',trace)
        self.assertLess(trace.index('longhorn-v2-middleware.py cleanup'),trace.index('longhorn-v2-monitoring.py cleanup'))
        self.assertEqual(1,out.splitlines().count('LONGHORN_V2_MIDDLEWARE_PASS'))
        self.assertNotIn('/root/.kube/config',(ROOT/'tests/helpers/longhorn-v2-middleware.sh').read_text())
        self.assertEqual(b'foreign-context\n',self.run_shell(foreign=True)[-1])

    def test_shell_failure_stops_dependents_and_preserves_original_failure(self):
        for fail in ('longhorn-v2-monitoring.py:setup','longhorn-v2-middleware.py:setup','longhorn-v2-middleware.py:business','longhorn-v2-middleware.py:verify'):
            rc,out,err,trace,durable,context=self.run_shell(fail=fail)
            with self.subTest(fail=fail):
                self.assertEqual(17,rc,out+err);self.assertEqual('17',durable['exit']);self.assertIn('LONGHORN_FIRST_FAILURE',err)
                if fail.endswith(':setup'):self.assertNotIn('longhorn-v2-middleware.py business',trace)
                self.assertNotIn('LONGHORN_V2_MIDDLEWARE_PASS',out)

    def test_shell_database_cleanup_failure_retains_monitoring_and_no_success(self):
        rc,out,err,trace,durable,context=self.run_shell(fail='longhorn-v2-middleware.py:cleanup')
        self.assertEqual(17,rc);self.assertEqual('17',durable['finalized']);self.assertNotIn('longhorn-v2-monitoring.py cleanup',trace);self.assertNotIn('LONGHORN_V2_MIDDLEWARE_PASS',out)

    def test_cleanup_only_reaches_owned_recovery_without_empty_volume_precondition(self):
        rc,out,err,trace,durable,context=self.run_shell('v2-middleware-cleanup');self.assertEqual(0,rc,out+err)
        self.assertNotIn('longhorn-v2-middleware.py setup',trace);self.assertNotIn('longhorn-v2-middleware.py business',trace)
        self.assertLess(trace.index('longhorn-v2-middleware.py cleanup'),trace.index('longhorn-v2-install.py volume-baseline'))
        self.assertIn('LONGHORN_V2_MIDDLEWARE_CLEANUP_PASS',out);self.assertNotIn('LONGHORN_V2_MIDDLEWARE_PASS',out)

    def test_sigterm_has_durable_143_and_dependency_ordered_cleanup(self):
        rc,out,err,trace,durable,context=self.run_shell(terminate=True);self.assertEqual(143,rc,out+err);self.assertEqual({'exit':'143','finalized':'0'},durable)
        self.assertIn('longhorn-v2-middleware.py cleanup',trace);self.assertIn('longhorn-v2-monitoring.py cleanup',trace);self.assertNotIn('LONGHORN_V2_MIDDLEWARE_PASS',out)

    def test_unknown_mode_cannot_reach_lab(self):
        rc,out,err,trace,durable,context=self.run_shell('unknown');self.assertEqual(2,rc);self.assertFalse(trace);self.assertFalse(durable)

    def test_public_runner_routes_both_modes_with_owning_markers(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text();live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        for mode,marker in (('v2-middleware','LONGHORN_V2_MIDDLEWARE_PASS'),('v2-middleware-cleanup','LONGHORN_V2_MIDDLEWARE_CLEANUP_PASS')):
            self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' '+marker,runner)
            self.assertGreaterEqual(runner.count('|'+mode+'|'),2)
        self.assertIn('longhorn-v2-middleware.sh',live)


if __name__=='__main__':unittest.main()
