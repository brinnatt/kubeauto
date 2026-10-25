"""Positive worker fencing, force-delete authority and rollback contracts."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('node_failure_contract',ROOT/'tests/helpers/longhorn-node-failure.py')
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)


class GuestFenceContracts(unittest.TestCase):
    def fixture(self,root,foreign=False,live=False):
        for path in ('etc','proc/sys/kernel/random','run'):(root/path).mkdir(parents=True,exist_ok=True)
        (root/'etc/machine-id').write_text('machine');(root/'proc/sys/kernel/random/boot_id').write_text('boot')
        proc=root/'proc/123';proc.mkdir();(proc/'cgroup').write_text('0::/kubepods/pod-owned')
        (proc/'stat').write_text('123 (worker) S '+'0 '*25)
        request={'token':'a'*32,'host':{'machine_id':'machine','boot_id':'boot'},'pod_uids':['pod-owned']}
        units={name:{'ActiveState':'active','UnitFileState':'enabled'} for name in ('kubelet.service','containerd.service')}
        state={'running':True,'sandbox':True};calls=[]
        def run(args,**kwargs):
            calls.append(args)
            output=''
            if args[:2]==['systemctl','show']:
                output='\n'.join(k+'='+v for k,v in units[args[2]].items())
            elif args[:2]==['systemctl','stop']:units[args[2]]['ActiveState']='inactive'
            elif args[:2]==['systemctl','start']:units[args[2]]['ActiveState']='active'
            elif args[0].endswith('crictl'):
                action=args[3]
                if action=='ps':output=json.dumps({'containers':[{'id':'container','labels':{'io.kubernetes.pod.uid':'foreign' if foreign else 'pod-owned'}}] if state['running'] else []})
                elif action=='pods':output=json.dumps({'items':[{'id':'sandbox','metadata':{'uid':'pod-owned'}}] if state['sandbox'] else []})
                elif action=='stop':state['running']=False
                elif action=='stopp':
                    state['sandbox']=False
                    if not live:(proc/'cgroup').write_text('0::/system.slice/old-exited')
                else:raise AssertionError(args)
            return subprocess.CompletedProcess(args,0,output,'')
        return request,units,calls,run

    def test_fence_positive_process_proof_and_restore_preserve_original_service_enablement(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request,units,calls,run=self.fixture(root)
            with patch('subprocess.run',side_effect=run):
                request['original']=M.guest_fence(request,'inspect',root)
                proof=M.guest_fence(request,'fence',root)
                self.assertTrue(proof['fenced']);self.assertEqual([],proof['kubepods_pids'])
                self.assertEqual(proof,M.guest_fence(request,'probe',root))
                result=M.guest_fence(request,'restore',root)
                self.assertTrue(result['restored']);self.assertEqual(request['original']['units'],result['units'])
                self.assertTrue(M.guest_fence(request,'restore',root)['restored'])
            stop_kubelet=calls.index(['systemctl','stop','kubelet.service'])
            stop_runtime=calls.index(['systemctl','stop','containerd.service'])
            stop_container=next(i for i,args in enumerate(calls) if len(args)>3 and args[3]=='stop')
            self.assertLess(stop_kubelet,stop_container);self.assertLess(stop_container,stop_runtime)
            self.assertFalse(list((root/'run').glob('kubeauto-longhorn-node20-*.json')))
            self.assertFalse(list((root/'run/systemd/system').glob('*/*.conf')))

    def test_foreign_pod_is_refused_before_any_service_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request,_,calls,run=self.fixture(root,foreign=True)
            with patch('subprocess.run',side_effect=run),self.assertRaisesRegex(ValueError,'unowned running Pod'):
                M.guest_fence(request,'inspect',root)
            self.assertFalse(any(args[:2]==['systemctl','stop'] for args in calls))
            self.assertEqual([],list((root/'run').iterdir()))

    def test_live_kubepods_process_blocks_fence_even_with_both_services_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request,_,_,run=self.fixture(root,live=True)
            with patch('subprocess.run',side_effect=run):
                request['original']=M.guest_fence(request,'inspect',root)
                with self.assertRaisesRegex(ValueError,'not proven'):M.guest_fence(request,'fence',root)
            self.assertEqual(1,len(list((root/'run').glob('kubeauto-longhorn-node20-*.json'))))

    def test_replaced_guard_cannot_be_adopted_or_removed_during_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request,_,calls,run=self.fixture(root)
            with patch('subprocess.run',side_effect=run):
                request['original']=M.guest_fence(request,'inspect',root);M.guest_fence(request,'fence',root)
                guard=next((root/'run/systemd/system').glob('*/*.conf'));guard.write_text('foreign')
                calls.clear()
                for action in ('probe','restore'):
                    with self.subTest(action=action),self.assertRaisesRegex(ValueError,'guard replaced'):M.guest_fence(request,action,root)
                self.assertEqual('foreign',guard.read_text());self.assertFalse(any(args[:2]==['systemctl','start'] for args in calls))

    def test_changed_boot_never_authorizes_replay_or_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request,_,calls,run=self.fixture(root)
            (root/'proc/sys/kernel/random/boot_id').write_text('another-boot')
            with patch('subprocess.run',side_effect=run),self.assertRaisesRegex(ValueError,'boot identity changed'):
                M.guest_fence(request,'restore',root)
            self.assertEqual([],calls)


class NodeFailureContracts(unittest.TestCase):
    def fixture(self):
        business={'namespace':'business'}
        node={'metadata':{'uid':'node-uid'},'status':{'conditions':[{'type':'Ready','status':'Unknown'}]}}
        pod={'metadata':{'uid':'pod-uid','name':'original','resourceVersion':'7'},'spec':{'nodeName':'worker'}}
        probe=M.Failure.__new__(M.Failure)
        probe.row={'failed_node':'worker','nodes':{'worker':{'uid':'node-uid'}},
            'original_pod':{'name':'original','uid':'pod-uid'},'fence':{'host':{'machine_id':'machine','boot_id':'boot'}}}
        proof={'fenced':True,'host':probe.row['fence']['host'],'kubepods_pids':[]}
        probe.call=Mock(return_value=proof);probe.save=Mock();probe.original_binding=Mock()
        probe.maintenance=SimpleNamespace(generated=Mock())
        probe.lab=SimpleNamespace(get=Mock(side_effect=lambda kind,*a,**kw:node if kind=='node' else pod),kubectl=Mock(return_value=subprocess.CompletedProcess([],0,'','')),wait=Mock())
        return probe,business,node,pod

    def test_force_delete_requires_positive_fence_and_exact_pod_uid_rv(self):
        probe,business,_,_=self.fixture();probe.force_old_pod(business)
        options=json.loads(probe.lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual({'uid':'pod-uid','resourceVersion':'7'},options['preconditions'])
        self.assertEqual(0,options['gracePeriodSeconds']);probe.call.assert_called_once_with('probe')
        self.assertIn('force_delete_intent',probe.row);self.assertEqual(2,probe.save.call_count)
        self.assertEqual('/api/v1/namespaces/business/pods/original',probe.lab.kubectl.call_args.args[2])

    def test_ssh_loss_notready_or_missing_process_proof_cannot_authorize_force_delete(self):
        for defect in ('missing','live-process','wrong-host','ready-node','replaced-pod','replaced-node'):
            probe,business,node,pod=self.fixture()
            if defect=='missing':probe.call.return_value={'fenced':False}
            elif defect=='live-process':probe.call.return_value['kubepods_pids']=[17]
            elif defect=='wrong-host':probe.call.return_value['host']={'machine_id':'other','boot_id':'boot'}
            elif defect=='ready-node':node['status']['conditions'][0]['status']='True'
            elif defect=='replaced-pod':pod['metadata']['uid']='foreign'
            else:node['metadata']['uid']='foreign'
            with self.subTest(defect=defect),self.assertRaises(ValueError):probe.force_old_pod(business)
            probe.lab.kubectl.assert_not_called();probe.save.assert_not_called()

    def conflict(self,version='7',record='8',name='original'):
        error=('Error from server (Conflict): Operation cannot be fulfilled on Pod "'+name+
            '": the ResourceVersion in the precondition ('+version+') does not match the ResourceVersion in record ('+
            record+'). The object might have been modified\n')
        return subprocess.CompletedProcess([],1,'',error)

    def test_exact_rv_conflict_rechecks_all_authority_and_retries_fresh_uid_rv(self):
        probe,business,_,pod=self.fixture();first=self.conflict();options=[]
        def delete(*args,**kwargs):
            options.append(json.loads(kwargs['stdin']))
            if len(options)==1:
                pod['metadata']['resourceVersion']='8'
                if kwargs.get('check',True):raise ValueError('fixture command failed: kubectl rc=1')
                return first
            return subprocess.CompletedProcess([],0,'','')
        probe.lab.kubectl.side_effect=delete;probe.force_old_pod(business)
        self.assertEqual([{'uid':'pod-uid','resourceVersion':v} for v in ('7','8')],
                         [o['preconditions'] for o in options])
        self.assertTrue(all(c.kwargs.get('check') is False for c in probe.lab.kubectl.call_args_list))
        self.assertEqual(2,probe.call.call_count);self.assertEqual(2,probe.maintenance.generated.call_count)
        self.assertEqual(2,probe.original_binding.call_count)
        self.assertEqual(first.stderr,probe.row['force_delete_attempts'][0]['stderr'])
        self.assertEqual([1,0],[r['rc'] for r in probe.row['force_delete_attempts']])

    def test_delete_reads_latest_pod_after_controller_and_volume_binding_validation(self):
        probe,business,node,pod=self.fixture()
        probe.lab.get.side_effect=lambda kind,*a,**kw:copy.deepcopy(node if kind=='node' else pod)
        probe.maintenance.generated.side_effect=lambda *a:pod['metadata'].update(resourceVersion='8')
        probe.force_old_pod(business)
        options=json.loads(probe.lab.kubectl.call_args.kwargs['stdin'])
        self.assertEqual({'uid':'pod-uid','resourceVersion':'8'},options['preconditions'])
        self.assertEqual(1,probe.lab.kubectl.call_count)

    def test_rv_conflict_never_retries_changed_fence_worker_workload_binding_or_absent_pod(self):
        for defect in ('fence','host','live-process','worker-uid','worker-ready','pod-uid','pod-node',
                       'absent-pod','binding','controller'):
            with self.subTest(defect=defect):
                probe,business,node,pod=self.fixture();missing=[False]
                probe.lab.get.side_effect=lambda kind,*a,**kw:node if kind=='node' else None if missing[0] else pod
                def delete(*args,**kwargs):
                    pod['metadata']['resourceVersion']='8'
                    if defect=='fence':probe.call.return_value['fenced']=False
                    elif defect=='host':probe.call.return_value['host']={'machine_id':'other','boot_id':'boot'}
                    elif defect=='live-process':probe.call.return_value['kubepods_pids']=[17]
                    elif defect=='worker-uid':node['metadata']['uid']='foreign'
                    elif defect=='worker-ready':node['status']['conditions'][0]['status']='True'
                    elif defect=='pod-uid':pod['metadata']['uid']='foreign'
                    elif defect=='pod-node':pod['spec']['nodeName']='foreign'
                    elif defect=='absent-pod':missing[0]=True
                    elif defect=='binding':probe.original_binding.side_effect=ValueError('binding changed')
                    else:probe.maintenance.generated.side_effect=ValueError('controller changed')
                    return self.conflict()
                probe.lab.kubectl.side_effect=delete
                with self.assertRaises(ValueError):probe.force_old_pod(business)
                self.assertEqual(1,probe.lab.kubectl.call_count)

    def test_only_exact_rv_conflict_retries_and_no_fresh_version_or_exhaustion_stops(self):
        errors=['Error from server (Forbidden): no', 'Error from server (NotFound): pods "original" not found',
                'Error from server (Conflict): UID precondition failed',self.conflict(name='another').stderr,
                self.conflict(version='wrong').stderr,self.conflict(record='7').stderr]
        for error in errors:
            with self.subTest(error=error):
                probe,business,_,_=self.fixture()
                probe.lab.kubectl.return_value=subprocess.CompletedProcess([],1,'',error)
                with self.assertRaises(ValueError):probe.force_old_pod(business)
                self.assertEqual(1,probe.lab.kubectl.call_count)
        probe,business,_,_=self.fixture();probe.lab.kubectl.return_value=self.conflict()
        with self.assertRaisesRegex(ValueError,'fresh resourceVersion'):probe.force_old_pod(business)
        self.assertEqual(1,probe.lab.kubectl.call_count)
        probe,business,_,pod=self.fixture()
        def delete(*args,**kwargs):
            version=pod['metadata']['resourceVersion'];pod['metadata']['resourceVersion']=str(int(version)+1)
            return self.conflict(version,pod['metadata']['resourceVersion'])
        probe.lab.kubectl.side_effect=delete
        with self.assertRaisesRegex(ValueError,'retry limit'):probe.force_old_pod(business)
        self.assertEqual(3,probe.lab.kubectl.call_count)
        self.assertEqual(3,len(probe.row['force_delete_attempts']))

    def test_notfound_before_delete_is_refused_and_after_success_means_retired_without_replacement(self):
        for state in ('absent-before','absent-after','foreign-after'):
            with self.subTest(state=state):
                probe,business,node,pod=self.fixture();deleted=[False]
                def get(kind,*args,**kwargs):
                    if kind=='node':return node
                    if state=='absent-before' or deleted[0] and state=='absent-after':return None
                    if deleted[0] and state=='foreign-after':
                        replaced=copy.deepcopy(pod);replaced['metadata']['uid']='foreign';return replaced
                    return pod
                probe.lab.get.side_effect=get
                def delete(*args,**kwargs):
                    deleted[0]=True;return subprocess.CompletedProcess([],0,'','')
                probe.lab.kubectl.side_effect=delete
                probe.lab.wait.side_effect=lambda label,timeout,predicate:self.assertTrue(predicate())
                if state=='absent-after':probe.force_old_pod(business)
                else:
                    with self.assertRaises(ValueError):probe.force_old_pod(business)
                self.assertEqual(0 if state=='absent-before' else 1,probe.lab.kubectl.call_count)

    def test_original_binding_never_adopts_same_name_replacement(self):
        business={'volume':'volume','volume_uid':'volume-uid','pv':'pv','pv_uid':'pv-uid',
            'namespace':'business','objects':{'PersistentVolumeClaim/data':'claim-uid'}}
        probe=M.Failure.__new__(M.Failure);values={'volumes.longhorn.io':'volume-uid','pv':'pv-uid','pvc':'claim-uid'}
        probe.lab=SimpleNamespace(get=lambda kind,*a:{'metadata':{'uid':values[kind]}})
        self.assertEqual(values,probe.original_binding(business))
        values['pvc']='foreign'
        with self.assertRaisesRegex(ValueError,'binding changed'):probe.original_binding(business)

    def test_cleanup_restores_fence_before_healthy_business_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            probe=M.Failure.__new__(M.Failure);probe.path=Path(directory)/M.RECORD;probe.path.write_text('{}')
            probe.row={'nodes':{'worker':{'uid':'node-uid','taints':[],'unschedulable':False}},'touched':['worker'],'failed_node':'worker','token':'a'*32}
            probe.read=Mock();calls=[];probe.restore_host=lambda:calls.append('restore-fence')
            probe.common=SimpleNamespace(node_restored=lambda *a:calls.append('ready') or True)
            probe.lab=SimpleNamespace(DIRECTORY=Path(directory),get=lambda *a:{'metadata':{'uid':'node-uid'},'spec':{'unschedulable':False},
                'status':{'conditions':[{'type':'Ready','status':'True'}]}},
                wait=lambda label,timeout,fn:fn())
            probe.maintenance=SimpleNamespace(scheduling=lambda *a:calls.append('restore-scheduling'),
                clean=lambda *a,**kw:calls.append('delete-business'))
            probe.pid=123;probe.clean()
            self.assertEqual(['restore-fence','ready','restore-scheduling','ready','delete-business'],calls)


    def test_cleanup_restores_only_owned_cordon_then_waits_for_exact_original_taints(self):
        for defect in ('none','timeAdded','empty-value-timeAdded','foreign-taint','value-on-cordon-taint','node-uid',
                       'unknown-field','duplicate','wrong-effect','invalid-timeAdded','wrong-key'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                probe=M.Failure.__new__(M.Failure);probe.path=Path(directory)/M.RECORD;probe.path.write_text('{}')
                saved={'uid':'node-uid','taints':[],'unschedulable':False}
                probe.row={'nodes':{'worker':saved},'touched':['worker'],'failed_node':'worker','token':'a'*32}
                cordon={'key':'node.kubernetes.io/unschedulable','effect':'NoSchedule'}
                node={'metadata':{'uid':'node-uid','resourceVersion':'9'},
                    'spec':{'unschedulable':True,'taints':[cordon]},
                    'status':{'conditions':[{'type':'Ready','status':'True'}]}}
                if defect in ('timeAdded','empty-value-timeAdded'):
                    cordon['timeAdded']='2026-10-21T21:52:05Z'
                    if defect=='empty-value-timeAdded':cordon['value']=''
                elif defect=='unknown-field':cordon['foreign']=True
                elif defect=='duplicate':node['spec']['taints'].append(dict(cordon))
                elif defect=='wrong-effect':cordon['effect']='NoExecute'
                elif defect=='invalid-timeAdded':cordon['timeAdded']='2026-99-21T21:52:05Z'
                elif defect=='wrong-key':cordon['key']='foreign'
                elif defect=='foreign-taint':node['spec']['taints'].append({'key':'foreign','effect':'NoSchedule'})
                elif defect=='value-on-cordon-taint':node['spec']['taints'][0]=dict(cordon,value='foreign')
                elif defect=='node-uid':node['metadata']['uid']='foreign'
                probe.read=Mock();calls=[];probe.restore_host=lambda:calls.append('restore-fence')
                probe.common=M.load('longhorn-v1-stability.py');maintenance=M.load('longhorn-maintenance.py')
                def kubectl(*args,**kwargs):
                    patch=json.loads(kwargs['stdin']);self.assertEqual('node-uid',patch[0]['value'])
                    self.assertEqual('9',patch[1]['value']);self.assertFalse(patch[2]['value'])
                    node['spec']['unschedulable']=False;calls.append('restore-scheduling')
                def wait(label,timeout,predicate):
                    result=predicate()
                    if label=='node20-original-scheduling-restored':
                        self.assertFalse(result) # controller has not yet removed our taint
                        node['spec']['taints']=[];result=predicate()
                    if not result:raise TimeoutError(label)
                    calls.append(label)
                probe.lab=SimpleNamespace(DIRECTORY=Path(directory),get=lambda *a:copy.deepcopy(node),
                    kubectl=Mock(side_effect=kubectl),wait=wait)
                probe.maintenance=SimpleNamespace(scheduling=maintenance.scheduling,
                    clean=lambda *a,**kw:calls.append('delete-business'));probe.pid=123
                if defect in ('none','timeAdded','empty-value-timeAdded'):
                    probe.clean();self.assertEqual(saved['unschedulable'],node['spec']['unschedulable'])
                    self.assertEqual(saved['taints'],node['spec']['taints'])
                    self.assertLess(calls.index('restore-fence'),calls.index('restore-scheduling'))
                    self.assertLess(calls.index('node20-original-scheduling-restored'),calls.index('delete-business'))
                else:
                    with self.assertRaises((ValueError,TimeoutError)):probe.clean()
                    probe.lab.kubectl.assert_not_called();self.assertNotIn('delete-business',calls)

    def test_reboot_ready_and_cleanup_share_cordon_recovery_and_exact_final_policy(self):
        for label in ('node20-original-worker-ready-after-reboot','node20-ready-before-scheduling'):
            with self.subTest(label=label):
                probe=M.Failure.__new__(M.Failure)
                saved={'uid':'node-uid','taints':[],'unschedulable':False}
                probe.row={'failed_node':'worker','touched':['worker']}
                cordon={'key':'node.kubernetes.io/unschedulable','effect':'NoSchedule',
                    'timeAdded':'2026-10-21T21:52:05Z'}
                node={'metadata':{'uid':'node-uid'},'spec':{'unschedulable':True,'taints':[cordon]},
                    'status':{'conditions':[{'type':'Ready','status':'True'}]}}
                probe.common=M.load('longhorn-v1-stability.py');calls=[]
                def schedule(*args):
                    self.assertEqual(('worker',False),args[2:])
                    node['spec']['unschedulable']=False;calls.append('uncordon')
                def wait(name,timeout,predicate):
                    if name=='node20-original-scheduling-restored':
                        self.assertFalse(predicate())
                        node['spec']['taints']=[]
                    self.assertTrue(predicate());calls.append(name)
                probe.lab=SimpleNamespace(get=lambda *args:copy.deepcopy(node),wait=wait)
                probe.maintenance=SimpleNamespace(scheduling=schedule)
                probe.restore_scheduling('worker',saved,label)
                self.assertEqual([label,'uncordon','node20-original-scheduling-restored'],calls)
                self.assertEqual([],saved['taints'])

    def test_reboot_transient_notready_taints_wait_without_mutation_then_ready_policy_is_strict(self):
        for foreign in (False,True):
            with self.subTest(foreign=foreign):
                probe=M.Failure.__new__(M.Failure)
                saved={'uid':'node-uid','taints':[],'unschedulable':False}
                probe.row={'failed_node':'worker','touched':['worker']}
                cordon={'key':'node.kubernetes.io/unschedulable','effect':'NoSchedule',
                    'timeAdded':'2026-10-21T21:52:05Z'}
                node={'metadata':{'uid':'node-uid'},'spec':{'unschedulable':True,
                    'taints':[cordon,{'key':'node.kubernetes.io/not-ready','effect':'NoSchedule'},
                        {'key':'node.kubernetes.io/unreachable','effect':'NoExecute'}]},
                    'status':{'conditions':[{'type':'Ready','status':'Unknown'}]}}
                probe.common=M.load('longhorn-v1-stability.py')
                def schedule(*args):node['spec'].update(unschedulable=False)
                probe.maintenance=SimpleNamespace(scheduling=Mock(side_effect=schedule))
                def wait(label,timeout,predicate):
                    if label=='node20-original-worker-ready-after-reboot':
                        self.assertEqual(600,timeout);self.assertFalse(predicate())
                        probe.maintenance.scheduling.assert_not_called()
                        node['status']['conditions'][0]['status']='True'
                        self.assertFalse(predicate()) # Ready arrived before taint-controller cleanup
                        probe.maintenance.scheduling.assert_not_called()
                        node['spec']['taints']=[cordon]+([{'key':'foreign','effect':'NoSchedule'}] if foreign else [])
                    else:
                        self.assertFalse(predicate());node['spec']['taints']=[]
                    self.assertTrue(predicate())
                probe.lab=SimpleNamespace(get=lambda *args:copy.deepcopy(node),wait=wait)
                if foreign:
                    with self.assertRaisesRegex(ValueError,'changed worker taints'):
                        probe.restore_scheduling('worker',saved,'node20-original-worker-ready-after-reboot')
                    probe.maintenance.scheduling.assert_not_called()
                else:
                    probe.restore_scheduling('worker',saved,'node20-original-worker-ready-after-reboot')
                    probe.maintenance.scheduling.assert_called_once()
                    self.assertEqual(saved['taints'],node['spec']['taints'])


if __name__=='__main__':unittest.main()
