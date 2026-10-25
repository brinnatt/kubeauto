"""V2 Pod fault authority, controller recreation and exact acknowledged ledgers."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_control_contract', ROOT/'tests/helpers/longhorn-v2-control-faults.py')
GATE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GATE)


class V2ControlContracts(unittest.TestCase):
    def fixture(self, base):
        inventory=base/'roles/longhorn/files/images.json';inventory.parent.mkdir(parents=True)
        inventory.write_text(json.dumps({'1.12.1':{'longhorn-instance-manager':'v1.12.1'}}))
        image='registry.test/longhorn-instance-manager:v1.12.1'
        volume={'metadata':{'name':'volume','uid':'v'},'spec':{'dataEngine':'v2','encrypted':False,'accessMode':'rwo'},
                'status':{'state':'attached','currentNodeID':'longhorn-node-146'}}
        replicas=[{'metadata':{'name':'r'+str(i),'uid':'r'+str(i),'ownerReferences':[{'kind':'Volume','uid':'v'}]},
            'spec':{'volumeName':'volume','nodeID':'longhorn-node-'+str(146+i)},
            'status':{'instanceManagerName':'im'+str(i)}} for i in range(3)]
        manager={'metadata':{'name':'im1','uid':'im-uid'},'spec':{'dataEngine':'v2','type':'aio','nodeID':'longhorn-node-147','image':image},
                 'status':{'currentState':'running','instanceReplicas':{'r1':{'spec':{'name':'r1'},'status':{'state':'running','type':'replica'}}}}}
        pod={'metadata':{'name':'im1','uid':'old-pod','ownerReferences':[{'kind':'InstanceManager','uid':'im-uid','controller':True}]},
            'spec':{'nodeName':'longhorn-node-147','containers':[{'name':'instance-manager','image':image,'args':['--spdk-enabled']}],
                    'volumes':[{'secret':{'secretName':'longhorn-grpc-tls'}}]},
            'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}
        state={'namespace':{'metadata':{'uid':'ns'}},'volumes.longhorn.io':{'items':[volume]},
               'replicas.longhorn.io':{'items':replicas},'engines.longhorn.io':{'items':[]},
               'enginefrontends.longhorn.io':{'items':[]},'instancemanagers.longhorn.io':manager,'pod':pod}
        lab=SimpleNamespace(BASE=base,DIRECTORY=base,write_private=Mock(),WORKERS=['192.168.47.146','192.168.47.147','192.168.47.148'],
            owner=Mock(return_value={'namespace_uid':'ns'}), get=Mock(side_effect=lambda kind,*a,**kw:copy.deepcopy(state[kind])),
            healthy_replicas=Mock(return_value=['a','b','c']),replica_topology=Mock(return_value={'three':'replicas'}),remove=Mock())
        return lab,{'volume':'volume','volume_uid':'v'},state

    def test_native_v2_reference_without_controller_flag_is_admitted_but_pod_requires_it(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,j,state=self.fixture(Path(directory))
            pod,proof=GATE.boundary(lab,j)
            self.assertEqual('old-pod',pod['metadata']['uid']);self.assertNotEqual(proof['parent']['node'],proof['original_client_node'])
            self.assertEqual(state['instancemanagers.longhorn.io']['spec'],proof['manager_spec'])
            state['pod']['metadata']['ownerReferences'][0].pop('controller')
            with self.assertRaises(ValueError):GATE.boundary(lab,j)

    def test_foreign_volume_wrong_engine_image_tls_or_native_consumer_never_deletes(self):
        defects=('namespace','volume-uid','foreign-volume','v1','encrypted','rwx','detached','unhealthy','topology',
                 'replica-owner','im-engine','im-type','im-node','im-state','pod-owner','pod-node','pod-ready',
                 'image','pod-image','spdk','tls','foreign-replica','extra-native','native-state','native-type','native-name','assigned-cr')
        for defect in defects:
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                lab,j,state=self.fixture(Path(directory));v=state['volumes.longhorn.io']['items'][0]
                im=state['instancemanagers.longhorn.io'];pod=state['pod'];native=im['status']['instanceReplicas']['r1']
                if defect=='namespace':state['namespace']['metadata']['uid']='foreign'
                if defect=='volume-uid':v['metadata']['uid']='foreign'
                if defect=='foreign-volume':state['volumes.longhorn.io']['items'].append(copy.deepcopy(v))
                if defect=='v1':v['spec']['dataEngine']='v1'
                if defect=='encrypted':v['spec']['encrypted']=True
                if defect=='rwx':v['spec']['accessMode']='rwx'
                if defect=='detached':v['status']['state']='detached'
                if defect=='unhealthy':lab.healthy_replicas.return_value=None
                if defect=='topology':lab.replica_topology.return_value=None
                if defect=='replica-owner':state['replicas.longhorn.io']['items'][1]['metadata']['ownerReferences'][0]['uid']='other'
                if defect=='im-engine':im['spec']['dataEngine']='v1'
                if defect=='im-type':im['spec']['type']='replica'
                if defect=='im-node':im['spec']['nodeID']='other'
                if defect=='im-state':im['status']['currentState']='error'
                if defect=='pod-owner':pod['metadata']['ownerReferences'][0]['uid']='other'
                if defect=='pod-node':pod['spec']['nodeName']='other'
                if defect=='pod-ready':pod['status']['conditions'][0]['status']='False'
                if defect=='image':im['spec']['image']='registry.test/longhorn-instance-manager:unreviewed'
                if defect=='pod-image':pod['spec']['containers'][0]['image']='other'
                if defect=='spdk':pod['spec']['containers'][0]['args']=[]
                if defect=='tls':pod['spec']['volumes']=[]
                if defect=='foreign-replica':im['status']['instanceReplicas']['foreign']=copy.deepcopy(native)
                if defect=='extra-native':im['status']['instanceEngines']={'foreign':{}}
                if defect=='native-state':native['status']['state']='starting'
                if defect=='native-type':native['status']['type']='engine'
                if defect=='native-name':native['spec']['name']='other'
                if defect=='assigned-cr':state['engines.longhorn.io']['items']=[{'metadata':{'uid':'other'},'status':{'instanceManagerName':'im1'}}]
                with self.assertRaises(ValueError):GATE.inject(lab,j,'instance-manager')
                lab.remove.assert_not_called()

    def test_double_read_rejects_replacement_before_uid_guarded_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,j,_=self.fixture(Path(directory));pod,proof=GATE.boundary(lab,j)
            changed=copy.deepcopy(proof);changed['pod_uid']='replacement'
            with patch.object(GATE,'boundary',side_effect=[(pod,proof),(pod,changed)]):
                with self.assertRaises(ValueError):GATE.inject(lab,j,'instance-manager')
            lab.remove.assert_not_called()
            with redirect_stdout(io.StringIO()) as output:GATE.inject(lab,j,'instance-manager')
            self.assertEqual('old-pod',lab.remove.call_args.args[0]['metadata']['uid'])
            self.assertEqual(proof,j['v2_replica_im_loss'])
            lab.write_private.assert_called_once()
            self.assertIn('LONGHORN_V2_REPLICA_IM_LOSS_INJECTED',output.getvalue())

    def test_recovery_requires_new_ready_pod_original_parent_configuration_and_full_raw_health(self):
        for defect in ('none','old-pod','unready','unhealthy','parent','spec','owner','image','node'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                lab,j,state=self.fixture(Path(directory));_,proof=GATE.boundary(lab,j);state['pod']['metadata']['uid']='new-pod'
                if defect=='old-pod':state['pod']['metadata']['uid']='old-pod'
                if defect=='unready':state['pod']['status']['conditions'][0]['status']='False'
                if defect=='unhealthy':lab.healthy_replicas.return_value=None
                if defect=='parent':state['instancemanagers.longhorn.io']['metadata']['uid']='other'
                if defect=='spec':state['instancemanagers.longhorn.io']['spec']['type']='other'
                if defect=='owner':state['pod']['metadata']['ownerReferences'][0]['uid']='other'
                if defect=='image':state['pod']['spec']['containers'][0]['image']='other'
                if defect=='node':state['pod']['spec']['nodeName']='other'
                if defect=='none':self.assertTrue(GATE.recovered(lab,j,proof))
                elif defect in ('old-pod','unready','unhealthy'):self.assertFalse(GATE.recovered(lab,j,proof))
                else:
                    with self.assertRaises(ValueError):GATE.recovered(lab,j,proof)

    def observation(self):
        faults=GATE._load('longhorn-faults.py');control=GATE._load('longhorn-control-faults.py')
        token='test-run:instance-manager'
        def confirmed(value):
            rows=[{'type':'ack','sequence':i,'sha256':hashlib.sha256(faults.ledger_block(value,i)).hexdigest()} for i in range(4)]
            return control.confirmed(rows,value,faults)
        ledger=confirmed(token);ledger.update(client='writer',fresh=confirmed(token+':recovered'))
        ledger['fresh']['path']='/data/control-recovered-ledger'
        proof={'fault':'remote-v2-spdk-instance-manager-pod-loss','volume_uid':'v','replica_uid':'r','pod_uid':'p',
               'parent':{'node':'other','uid':'im'},'original_client_node':'client','image':'pinned-image'}
        recovery={'volume_uid':'v','parent_uid':'im','pod_uid':'new','image':'pinned-image',
                  'replicas':{str(i):{'node':'n'+str(i),'uid':'r'+str(i)} for i in range(3)}}
        result={'variant':'instance-manager','client_rc':0,'recovery_mode':'native-component-recreation-original-client',
                'recovery_s':1.0,'retries':[],'confirmed':ledger['summary'],'fresh_confirmed':ledger['fresh']['summary']}
        remount={'original':ledger['summary'],'recovered':ledger['fresh']['summary']}
        rows={'LONGHORN_V2_REPLICA_IM_LOSS_INJECTED':proof,'LONGHORN_V2_REPLICA_IM_RECOVERED':recovery,'LONGHORN_CONTROL_FAULT_RESULT':result,
              'LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS':ledger,'LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED':remount}
        payload='test-run';suffix=('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes='+str(len(payload)*262144)+' sha256='+
            hashlib.sha256(payload.encode()*262144).hexdigest()+' replica_nodes=a,b,c\nLONGHORN_V2_REPLICA_IM_LOSS_BUSINESS_PASS\n'
            'LONGHORN_CONTROL_FAILURE_BUSINESS_PASS\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\n'
            'LONGHORN_V2_REPLICA_IM_FAULT_RESTORED_BEFORE_CLEANUP\n')
        return rows,suffix

    def test_verifier_recomputes_every_ack_and_rejects_wrong_source_remount_or_cleanup(self):
        for defect in ('none','identity','client','duration','token','fresh-path','ack','count','summary','remount','payload',
                       'old-pod','foreign-parent','recovery-uid','recovery-image','recovery-placement','cleanup','failure'):
            with self.subTest(defect=defect):
                rows,suffix=self.observation();result=rows['LONGHORN_CONTROL_FAULT_RESULT'];ledger=rows['LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS']
                if defect=='identity':rows['LONGHORN_V2_REPLICA_IM_LOSS_INJECTED']['volume_uid']=''
                if defect=='client':result['client_rc']=1
                if defect=='duration':result['recovery_s']=1300
                if defect=='token':ledger['fresh']['token']='other'
                if defect=='fresh-path':ledger['fresh']['path']='/data/unknown'
                if defect=='ack':ledger['records'][0]['sha256']='f'*64
                if defect=='count':ledger['records'].pop()
                if defect=='summary':result['confirmed']=dict(result['confirmed'],confirmed=99)
                if defect=='remount':rows['LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED']['original']={}
                if defect=='payload':suffix=suffix.replace('replica_nodes=a,b,c','replica_nodes=a,a,c')
                if defect=='old-pod':rows['LONGHORN_V2_REPLICA_IM_RECOVERED']['pod_uid']='p'
                if defect=='foreign-parent':rows['LONGHORN_V2_REPLICA_IM_RECOVERED']['parent_uid']='other'
                if defect=='recovery-uid':rows['LONGHORN_V2_REPLICA_IM_RECOVERED']['volume_uid']='other'
                if defect=='recovery-image':rows['LONGHORN_V2_REPLICA_IM_RECOVERED']['image']='other'
                if defect=='recovery-placement':rows['LONGHORN_V2_REPLICA_IM_RECOVERED']['replicas']['1']['node']='n0'
                if defect=='cleanup':suffix=suffix.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','')
                if defect=='failure':suffix+='LONGHORN_FIRST_FAILURE phase=recover rc=1\n'
                log=''.join(k+' '+json.dumps(v)+'\n' for k,v in rows.items())+suffix
                if defect=='none':self.assertEqual(result,GATE.verify_log(log))
                else:
                    with self.assertRaises(ValueError):GATE.verify_log(log)

    def test_business_adapter_reuses_ledger_and_always_restores_original_loader(self):
        for failure in (False,True):
            with self.subTest(failure=failure):
                control=SimpleNamespace(inject=Mock(),recovered=Mock())
                original=Mock(return_value=control);original_cleanup=Mock()
                lab=SimpleNamespace(load_helper=original,run_requests=Mock(),cleanup_business=original_cleanup)
                def business(*a,**kw):
                    self.assertIs(lab.load_helper('longhorn-control-faults.py'),control)
                    self.assertIs(control.inject,GATE.inject);self.assertIs(control.recovered,GATE.recovered)
                    if failure:raise ValueError('business failed')
                lab.business=Mock(side_effect=business)
                with redirect_stdout(io.StringIO()) as output:
                    if failure:
                        with self.assertRaises(ValueError):GATE.business(lab,12)
                        self.assertNotIn('LONGHORN_V2_REPLICA_IM_LOSS_BUSINESS_PASS',output.getvalue())
                    else:GATE.business(lab,12)
                self.assertIs(original,lab.load_helper)
                self.assertIs(original_cleanup,lab.cleanup_business)

    def test_cleanup_recovers_before_delete_and_retains_journal_on_uncertain_recovery(self):
        for defect in ('none','pending','timeout','parent','pod','symlink'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);lab,j,state=self.fixture(base);_,proof=GATE.boundary(lab,j)
                j.update(v2_replica_im_loss=proof,v2_replica_im_recovered=defect!='pending')
                record=base/'longhorn-install-business.json';record.write_text(json.dumps(j))
                state['pod']['metadata']['uid']='new'
                if defect=='parent':state['instancemanagers.longhorn.io']['metadata']['uid']='other'
                if defect=='pod':state['pod']['metadata']['uid']='old-pod'
                if defect=='symlink':record.unlink();record.symlink_to(base/'missing')
                original=Mock();lab.check_business_graph=Mock();lab.installation_health=Mock()
                def wait(label,timeout,check):
                    original.assert_not_called()
                    if defect=='timeout' or not check():raise TimeoutError(label)
                lab.wait=Mock(side_effect=wait)
                if defect=='timeout':
                    j['v2_replica_im_recovered']=False;record.write_text(json.dumps(j))
                with redirect_stdout(io.StringIO()):
                    if defect in ('none','pending'):
                        GATE.cleanup(lab,original,record,relay_pid=9)
                        original.assert_called_once_with(record,relay_pid=9)
                        lab.installation_health.assert_called_once()
                    else:
                        with self.assertRaises((ValueError,TimeoutError)):GATE.cleanup(lab,original,record)
                        original.assert_not_called();self.assertTrue(record.exists() or record.is_symlink())

    def test_fixed_shell_calls_real_verifier_and_guarded_cleanup_before_baseline(self):
        source=(ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        cases=[('qualification',f) for f in ('none','business','bad-result','cleanup','baseline','core')]
        cases += [('cleanup',f) for f in ('none','cleanup','baseline','core')]
        for mode,failure in cases:
            with self.subTest(mode=mode,failure=failure),tempfile.TemporaryDirectory() as directory:
                base=Path(directory);(base/'.venv/bin').mkdir(parents=True);(base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(os.sys.executable)
                (base/'kube-bin').mkdir();command=base/'kube-bin/kubectl';command.write_text('#!/bin/sh\nexit 0\n');command.chmod(0o755)
                rows,suffix=self.observation()
                if failure=='bad-result':rows['LONGHORN_CONTROL_FAULT_RESULT']['client_rc']=1
                (base/'observation.log').write_text(''.join(k+' '+json.dumps(v)+'\n' for k,v in rows.items())+suffix)
                helper='''import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if (mode=='volume-baseline' and failure=='baseline') or (mode=='verify-core' and failure=='core'):sys.exit(17)
'''
                for name in ('core','install'):(base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                (base/'tests/helpers/longhorn-v2-control-faults.py').write_text('''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1]
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='verify':
 # The real CLI intentionally retains failure diagnostics. This local test
 # owns its expected-failure diagnostics inside the temporary fixture too.
 import tempfile
 make_private=tempfile.NamedTemporaryFile
 def local_private(*a,**kw):
  kw['dir']=str(Path.cwd())
  return make_private(*a,**kw)
 tempfile.NamedTemporaryFile=local_private
 runpy.run_path(os.environ['CONTROL_SOURCE'],run_name='__main__')
elif mode=='business':
 if os.environ['FAIL_MODE']=='business':sys.exit(17)
 print(Path('observation.log').read_text())
elif mode=='cleanup':
 if os.environ['FAIL_MODE']=='cleanup':sys.exit(17)
else:sys.exit(18)
''')
                script=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-volumes.',str(base/'work.'))
                result=subprocess.run(['bash','-c',script,'contract','v2-replica-im-loss-'+mode],text=True,capture_output=True,timeout=15,
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',FAIL_MODE=failure,CONTROL_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-control-faults.py')))
                calls=(base/'calls.txt').read_text().splitlines()
                if failure=='none':
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertIn('LONGHORN_V2_REPLICA_IM_LOSS_'+mode.upper()+'_SCOPED_CLEAN_PASS',result.stdout)
                    self.assertLess(calls.index('cleanup'),len(calls)-1-calls[::-1].index('volume-baseline'))
                    if mode=='qualification':self.assertIn('verify',calls)
                    else:
                        self.assertNotIn('verify',calls);self.assertNotIn('business',calls)
                        self.assertNotIn('LONGHORN_V2_REPLICA_IM_LOSS_QUALIFICATION_PASS',result.stdout)
                else:
                    self.assertNotEqual(0,result.returncode)
                    self.assertNotIn('LONGHORN_V2_REPLICA_IM_LOSS_'+mode.upper()+'_PASS',result.stdout)
                if failure!='baseline' or mode=='cleanup':self.assertIn('cleanup',calls)
                self.assertNotIn('business-clean',calls)
                diagnostics=list(base.glob('longhorn-v2-control-fault-*.txt'))
                self.assertEqual(1 if failure=='bad-result' else 0,len(diagnostics))
                if diagnostics:
                    self.assertEqual(0o600,diagnostics[0].stat().st_mode & 0o777)

    def test_dispatch_keeps_fault_inside_the_leased_product_gate(self):
        mode='v2-replica-im-loss-qualification'
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' LONGHORN_V2_REPLICA_IM_LOSS_QUALIFICATION_PASS',runner)
        self.assertIn(mode+'|v2-full-copy-lifecycle-qualification',(ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('--longhorn-v2-replica-im-loss-cleanup) longhorn_launch v2-replica-im-loss-cleanup LONGHORN_V2_REPLICA_IM_LOSS_CLEANUP_PASS',runner)
        self.assertIn('tests.unit.test_longhorn_v2_control_faults',(ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__=='__main__':unittest.main()
