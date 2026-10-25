"""Native full-copy source retirement, replacement refusal and business results."""
import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import subprocess
import tempfile
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('clone_lifecycle_contract', ROOT/'tests/helpers/longhorn-v2-clone-lifecycle.py')
GATE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GATE)


class CloneLifecycleContracts(unittest.TestCase):
    def fixture(self):
        journal = {'namespace': 'business', 'objects': {'Pod/writer': 'pod-uid', 'PersistentVolumeClaim/data': 'pvc-uid'},
                   'pv': 'pv', 'pv_uid': 'pv-uid', 'volume': 'source', 'volume_uid': 'source-uid'}
        objects = {('pod','writer'): {'metadata': {'uid':'pod-uid'}},
                   ('pvc','data'): {'metadata': {'uid':'pvc-uid'}},
                   ('pv','pv'): {'metadata': {'uid':'pv-uid'}, 'spec': {'claimRef': {'uid':'pvc-uid'},
                       'persistentVolumeReclaimPolicy':'Retain', 'csi': {'volumeHandle':'source'}}},
                   ('volumes.longhorn.io','source'): {'metadata': {'uid':'source-uid'}, 'status': {'state':'detached'}}}
        removed = []
        def get(kind, name='', namespace=''):
            if name: return copy.deepcopy(objects.get((kind,name)))
            if kind == 'pv': return {'items': [copy.deepcopy(o) for (k,n),o in objects.items() if k=='pv']}
            return {'items': []}
        def remove(obj):
            removed.append(obj['metadata']['uid'])
            key = next(k for k,o in objects.items() if o['metadata']['uid']==obj['metadata']['uid'])
            del objects[key]
        def wait(label, timeout, check):
            result = check()
            if not result: raise TimeoutError(label)
            return result
        lab = SimpleNamespace(get=Mock(side_effect=get), remove=Mock(side_effect=remove),
            wait=Mock(side_effect=wait), check_business_graph=Mock())
        return lab,journal,objects,removed

    def test_native_retirement_orders_source_graph_and_waits_for_detached_and_replicas(self):
        lab,journal,objects,removed = self.fixture()
        GATE.retire_source(lab,journal)
        self.assertEqual(['pod-uid','pvc-uid','pv-uid','source-uid'],removed)
        self.assertEqual({},objects)
        stages = [c.args[0] for c in lab.wait.call_args_list]
        self.assertLess(stages.index('full-copy-source-detached'),stages.index('full-copy-retire-volumes.longhorn.io-source'))
        self.assertEqual(stages[-1],'full-copy-source-replicas-retired')

    def test_every_replaced_missing_or_wrong_binding_source_stops_before_any_delete(self):
        for defect in ('pod','pvc','pv','volume','missing','claim-ref','handle','policy'):
            with self.subTest(defect=defect):
                lab,journal,objects,removed = self.fixture()
                if defect in ('pod','pvc','pv','volume'):
                    key = {'pod':('pod','writer'),'pvc':('pvc','data'),'pv':('pv','pv'),
                           'volume':('volumes.longhorn.io','source')}[defect]
                    objects[key]['metadata']['uid']='foreign'
                elif defect=='missing': del objects[('pv','pv')]
                elif defect=='claim-ref': objects[('pv','pv')]['spec']['claimRef']['uid']='foreign'
                elif defect=='handle': objects[('pv','pv')]['spec']['csi']['volumeHandle']='foreign'
                else: objects[('pv','pv')]['spec']['persistentVolumeReclaimPolicy']='Delete'
                with self.assertRaises(ValueError): GATE.retire_source(lab,journal)
                self.assertEqual([],removed)

    def test_attached_source_foreign_referring_pv_or_remaining_replicas_never_pass(self):
        for defect in ('attached','another-pv','replicas'):
            with self.subTest(defect=defect):
                lab,journal,objects,removed = self.fixture()
                if defect=='attached': objects[('volumes.longhorn.io','source')]['status']['state']='attached'
                if defect=='another-pv': objects[('pv','foreign')]={'metadata':{'uid':'foreign'},'spec':{'csi':{'volumeHandle':'source'}}}
                if defect=='replicas':
                    original=lab.get.side_effect
                    lab.get.side_effect=lambda kind,*a,**kw: {'items':[{'spec':{'volumeName':'source'}}]} if kind=='replicas.longhorn.io' else original(kind,*a,**kw)
                with self.assertRaises((ValueError,TimeoutError)): GATE.retire_source(lab,journal)
                self.assertNotIn('foreign',removed)
                if defect!='replicas': self.assertNotIn('source-uid',removed)

    def result(self):
        row={'source_uid':'source','clone_uid':'clone','clone_pvc_uid':'pvc','clone_pv_uid':'pv',
             'source_volume_absent':True,'source_pv_absent':True,'cross_node_readback':True,
             'before_sha256':'a'*64,'after_sha256':'b'*64,
             'replicas':{str(i):{'node':'n'+str(i),'uid':'r'+str(i)} for i in range(3)}}
        def log():
            return 'LONGHORN_V2_FULL_COPY_SOURCE_RETIRED_RESULT '+json.dumps(row)+'\nLONGHORN_V2_FULL_COPY_SOURCE_RETIRED_BUSINESS_PASS\nLONGHORN_BUSINESS_FIXTURES_CLEAN_PASS\n'
        return row,log

    def test_result_rejects_missing_identity_data_placement_cleanup_or_failure(self):
        row,log=self.result(); self.assertEqual(row,GATE.verify_log(log()))
        for defect in ('same-uid','missing-uid','volume','pv','remount','hash','same-hash','nodes','replicas','cleanup','failure','duplicate'):
            with self.subTest(defect=defect):
                row,log=self.result()
                if defect=='same-uid': row['clone_uid']='source'
                if defect=='missing-uid': row['clone_pvc_uid']=''
                if defect in ('volume','pv'): row['source_'+defect+'_absent']=False
                if defect=='remount': row['cross_node_readback']=False
                if defect=='hash': row['after_sha256']='bad'
                if defect=='same-hash': row['after_sha256']=row['before_sha256']
                if defect=='nodes': row['replicas']['1']['node']='n0'
                if defect=='replicas': row['replicas']['1']['uid']='r0'
                text=log()
                if defect=='cleanup': text=text.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','')
                if defect=='failure': text+='LONGHORN_FIRST_FAILURE phase=retire rc=1\n'
                if defect=='duplicate': text+=text.splitlines()[0]+'\n'
                with self.assertRaises(ValueError): GATE.verify_log(text)

    def test_data_read_and_write_require_original_client_and_exact_digest(self):
        import hashlib
        digest=hashlib.sha256(b'value'*262144).hexdigest()
        for defect in ('none','uid','missing','bytes'):
            with self.subTest(defect=defect):
                lab=SimpleNamespace(get=Mock(return_value=None if defect=='missing' else {'metadata':{'uid':'other' if defect=='uid' else 'uid'}}),
                    kubectl=Mock(return_value=SimpleNamespace(stdout='bad' if defect=='bytes' else digest)), payload_program=Mock(return_value='code'))
                journal={'namespace':'n','objects':{'Pod/client':'uid'}}
                if defect=='none': self.assertEqual(digest,GATE.io(lab,journal,'client','value',read=True))
                else:
                    with self.assertRaises(ValueError): GATE.io(lab,journal,'client','value',read=True)
                    if defect!='bytes': lab.kubectl.assert_not_called()

    def test_clone_state_rejects_replaced_bindings_incomplete_copy_or_linked_dependencies(self):
        for defect in ('none','pvc','pv','volume','binding','handle','claim','v1','mode','state','size','linked','unhealthy'):
            with self.subTest(defect=defect):
                row={'pv':'pv','pv_uid':'p','volume':'clone','volume_uid':'v'}
                journal={'namespace':'n','objects':{'PersistentVolumeClaim/clone':'c'},'claims':{'clone':row}}
                objects={'pvc':{'metadata':{'uid':'c'},'spec':{'volumeName':'pv'}},
                    'pv':{'metadata':{'uid':'p'},'spec':{'csi':{'volumeHandle':'clone'},'claimRef':{'uid':'c'}}},
                    'volumes.longhorn.io':{'metadata':{'uid':'v'},'spec':{'size':str(2*1024**3),'dataEngine':'v2','cloneMode':'full-copy'},
                        'status':{'cloneStatus':{'state':'completed'}}}}
                if defect in ('pvc','pv','volume'): objects['volumes.longhorn.io' if defect=='volume' else defect]['metadata']['uid']='other'
                if defect=='binding': objects['pvc']['spec']['volumeName']='other'
                if defect=='handle': objects['pv']['spec']['csi']['volumeHandle']='other'
                if defect=='claim': objects['pv']['spec']['claimRef']['uid']='other'
                if defect=='v1': objects['volumes.longhorn.io']['spec']['dataEngine']='v1'
                if defect=='mode': objects['volumes.longhorn.io']['spec']['cloneMode']='linked-clone'
                if defect=='state': objects['volumes.longhorn.io']['status']['cloneStatus']['state']='copy-completed-awaiting-healthy'
                if defect=='size': objects['volumes.longhorn.io']['spec']['size']=str(4*1024**3)
                replicas=[{'spec':{'volumeName':'clone','linkedCloneSrcReplicaName':'source' if defect=='linked' else ''}}]*3
                lab=SimpleNamespace(get=Mock(side_effect=lambda kind,*a,**kw: {'items':replicas} if kind=='replicas.longhorn.io' else objects[kind]),
                    replica_topology=Mock(return_value={'healthy':'topology'}),healthy_replicas=Mock(return_value=None if defect=='unhealthy' else ['a','b','c']))
                if defect=='none': self.assertEqual({'healthy':'topology'},GATE.clone_state(lab,journal))
                elif defect=='unhealthy': self.assertIsNone(GATE.clone_state(lab,journal)); lab.replica_topology.assert_not_called()
                else:
                    with self.assertRaises(ValueError): GATE.clone_state(lab,journal)

    def test_fixed_shell_reaches_real_lifecycle_verifier_and_preserves_cleanup_on_failure(self):
        source=(ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none','business','bad-result','cleanup','baseline','core'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                base=Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(os.sys.executable)
                (base/'kube-bin').mkdir(); command=base/'kube-bin/kubectl'; command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o755)
                row,log=self.result()
                if failure=='bad-result': row['source_volume_absent']=False
                (base/'observation.log').write_text(log())
                helper='''import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if (mode=='business-clean' and failure=='cleanup') or (mode=='volume-baseline' and failure=='baseline') or (mode=='verify-core' and failure=='core'):sys.exit(17)
'''
                for name in ('core','install'): (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                (base/'tests/helpers/longhorn-v2-clones.py').write_text('''import os,runpy,sys
from pathlib import Path
mode=sys.argv[1]
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='lifecycle-verify':runpy.run_path(os.environ['CLONE_SOURCE'],run_name='__main__')
elif mode=='lifecycle-business':
 if os.environ['FAIL_MODE']=='business':sys.exit(17)
 print(Path('observation.log').read_text())
else:sys.exit(18)
''')
                script=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-volumes.',str(base/'work.'))
                result=subprocess.run(['bash','-c',script,'contract','v2-full-copy-lifecycle-qualification'],text=True,capture_output=True,timeout=15,
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',FAIL_MODE=failure,CLONE_SOURCE=str(ROOT/'tests/helpers/longhorn-v2-clones.py')))
                if failure=='none':
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertIn('LONGHORN_V2_FULL_COPY_LIFECYCLE_QUALIFICATION_SCOPED_CLEAN_PASS',result.stdout)
                    self.assertIn('lifecycle-verify',(base/'calls.txt').read_text())
                else:
                    self.assertNotEqual(0,result.returncode)
                    self.assertNotIn('LONGHORN_V2_FULL_COPY_LIFECYCLE_QUALIFICATION_PASS',result.stdout)
                if failure!='baseline': self.assertIn('business-clean',(base/'calls.txt').read_text())

    def test_dispatch_keeps_lifecycle_in_the_leased_product_volume_gate(self):
        mode='v2-full-copy-lifecycle-qualification'
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertIn('--longhorn-'+mode+') longhorn_launch '+mode+' LONGHORN_V2_FULL_COPY_LIFECYCLE_QUALIFICATION_PASS',runner)
        self.assertIn(mode+'|v2-full-copy-clone-qualification',(ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v2_clone_lifecycle',(ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__=='__main__': unittest.main()
