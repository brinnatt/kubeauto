"""Actual SPDK resource assertions and stopped-kubelet CPU fixture contracts."""
import base64
import copy
from contextlib import contextmanager
import importlib.util
import json
import mmap
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import yaml

from tests.unit.test_longhorn_v2 import v2_configuration
from tests.unit.test_longhorn_delivery import product

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('runtime_resources', ROOT/'tests/helpers/longhorn-v2-runtime-resources.py')
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)


def config():
    return dict(longhorn_v2_cpu_mask='0x1',longhorn_v2_cpu_cores=0,longhorn_v2_memory_mib=2048,
        longhorn_v2_hugepages=True,longhorn_v2_interrupt_mode=False,
        longhorn_v2_cpu_reservation_percentage=25,longhorn_v2_iobuf_large_pool_size=1024,
        longhorn_v2_iobuf_small_pool_size=8192)


def measured(desired):
    args=['spdk_tgt','--mem-size','2048','-m','0xc' if desired['longhorn_v2_cpu_cores'] else '0x1']
    if not desired['longhorn_v2_hugepages']:args.append('--no-huge')
    options={'large_pool_count':desired['longhorn_v2_iobuf_large_pool_size'],
             'small_pool_count':desired['longhorn_v2_iobuf_small_pool_size'],
             'large_bufsize':132*1024,'small_bufsize':8*1024}
    command={'method':'iobuf_set_options','params':options}
    tuned=options['large_pool_count']>1024 or options['small_pool_count']>8192
    if tuned:args+=['--json','/tmp/spdk-startup.json']
    return dict(argv=args,memory_limit=128*1024**2 if desired['longhorn_v2_hugepages'] else None,
        nohuge={'files':[{'device':'00:01','inode':73,'size_bytes':2048*1024**2}],
            'mappings':[{'device':'00:01','inode':73,'offset':0,'size_bytes':2048*1024**2,'perms':'rw-s'}]},
        rss_bytes=2048*1024**2,hugetlb_bytes=2048*1024**2 if desired['longhorn_v2_hugepages'] else 0,
        memory_events={'oom':0,'oom_kill':0},cpuset='2-3' if desired['longhorn_v2_cpu_cores'] else '0-7',
        iobuf_runtime=[command],startup_json={'subsystems':[{'subsystem':'iobuf','config':[command]}]} if tuned else None)


class RuntimeContracts(unittest.TestCase):
    def components_fixture(self,desired):
        nodes=('192.168.47.146','192.168.47.147','192.168.47.148')
        objects={('namespace',R.NS,R.NS):{'metadata':{'uid':'installation'}}}
        objects[('namespace',R.NS,'')]={'metadata':{'uid':'installation'}}
        for key,name in R.FIELDS.items():
            value=str(desired[key]).lower() if isinstance(desired[key],bool) else str(desired[key])
            objects['settings.longhorn.io',name,R.NS]={'metadata':{'uid':name},'value':json.dumps({'v2':value})}
        managers=[]
        for index,node in enumerate(nodes):
            name='im-'+str(index);node_name='longhorn-node-'+node.rsplit('.',1)[1]
            im={'metadata':{'name':name,'uid':name},'spec':{'nodeID':node_name,'dataEngine':'v2','type':'aio','image':'published:pin'},
                'status':{'currentState':'running','dataEngineStatus':{'v2':{'cpuCoreNumber':desired['longhorn_v2_cpu_cores']}}}}
            managers.append(im);objects['instancemanagers.longhorn.io',name,R.NS]=im
            args=['--spdk-core-number',str(desired['longhorn_v2_cpu_cores']),'--spdk-memory-size','2048',
                  '--spdk-cpumask','' if desired['longhorn_v2_cpu_cores'] else desired['longhorn_v2_cpu_mask']]
            if not desired['longhorn_v2_hugepages']:args+=['--spdk-no-hugepage']
            if desired['longhorn_v2_iobuf_large_pool_size']>1024:args+=['--spdk-iobuf-large-pool-size',str(desired['longhorn_v2_iobuf_large_pool_size'])]
            if desired['longhorn_v2_iobuf_small_pool_size']>8192:args+=['--spdk-iobuf-small-pool-size',str(desired['longhorn_v2_iobuf_small_pool_size'])]
            objects['pod',name,R.NS]={'metadata':{'uid':'pod-'+name,'ownerReferences':[{'kind':'InstanceManager','uid':name,'controller':True}]},
                'spec':{'nodeName':node_name,'containers':[{'name':'instance-manager','image':'published:pin','args':args,
                    'resources':{'requests':{'cpu':'2','memory':'128Mi'},'limits':{'cpu':'2','memory':'128Mi'}}}]},
                'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],'qosClass':'Guaranteed','containerStatuses':[{'restartCount':0}]}}
            objects['nodes.longhorn.io',node_name,R.NS]={'status':{'cpuPolicy':'static'}}
        objects['instancemanagers.longhorn.io','',R.NS]={'items':managers}
        lab=SimpleNamespace(WORKERS=nodes,owner=lambda:{'namespace_uid':'installation'},
            get=lambda kind,name='',namespace='':copy.deepcopy(objects.get((kind,name,namespace))),
            load_helper=lambda name:SimpleNamespace(product=lambda:product))
        return lab,objects

    def cpu_proof_fixture(self):
        desired=config();lab,objects=self.components_fixture(desired)
        state=R.components(lab,desired);state['runtime']={}
        for i,name in enumerate(state['pods']):
            state['runtime'][name]=dict(measured(desired),pid=100+i,start_time=str(123+i))
        proof={}
        for name,request,reason in (
                ('exclusive-without-static',R.profile(desired,'exclusive'),R.CPU_POLICY_REFUSAL),
                ('under-reserved-mask',dict(desired,longhorn_v2_cpu_reservation_percentage=1),R.CPU_RESERVATION_REFUSAL)):
            proof[name]={'desired':request,'reason':reason,'before':copy.deepcopy(state),'after':copy.deepcopy(state),
                'policies':{'longhorn-node-'+str(i):'none' for i in (146,147,148)},
                'policies_after':{'longhorn-node-'+str(i):'none' for i in (146,147,148)},
                'disk_before':{'receipt_sha256':'d'*64},'disk_after':{'receipt_sha256':'d'*64},
                'config_before':'a'*64,'config_after':'a'*64,'receipt_absent':True}
        return desired,state,proof

    def test_cpu_refusal_uses_original_product_input_and_restores_after_each_exact_guard(self):
        desired,state,_=self.cpu_proof_fixture()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);original=yaml.safe_dump(desired);(root/'config.yml').write_text(original)
            calls=[]
            def write(path,value):path.write_text(json.dumps(value))
            def restore():
                path=root/'longhorn-request-input.json';row=json.loads(path.read_text())
                (root/'config.yml').write_text(row['original']);path.unlink()
            @contextmanager
            def cleanup(action):
                try:yield
                finally:action()
            def core(pid,step,expected_refusal):
                submitted=yaml.safe_load((root/'config.yml').read_text());calls.append(submitted)
                self.assertTrue((root/'longhorn-request-input.json').exists())
                self.assertEqual((pid,step),(7,'longhorn-setup'))
                try:
                    if submitted['longhorn_v2_cpu_cores']:
                        cfg=v2_configuration();cfg.update(submitted)
                        nodes={a:{'metadata':{'name':'longhorn-node-'+a.rsplit('.',1)[1]}} for a in cfg['longhorn_v2_nodes']}
                        api=SimpleNamespace(get=lambda kind,*a,**k: {'items':[]} if kind=='volumes.longhorn.io' else
                            {'items':[{'metadata':{'name':'longhorn-node-146'},'status':{'cpuPolicy':'none'}}]} if kind=='nodes.longhorn.io' else True)
                        product.check_engine_changes(cfg,api,nodes)
                    else:product.check_v2_resources(submitted,{'status':{'allocatable':{'cpu':'8','memory':'8Gi','hugepages-2Mi':'2Gi'}}})
                except ValueError as error:self.assertEqual(str(error),expected_refusal)
                else:self.fail('actual owning product guard did not refuse')
            def observed(*args):
                self.assertEqual((root/'config.yml').read_text(),original)
                self.assertFalse((root/'longhorn-request-input.json').exists())
                return copy.deepcopy(state)
            lab=SimpleNamespace(DIRECTORY=root,WORKERS=('192.168.47.146','192.168.47.147','192.168.47.148'),
                get=lambda *a,**k:{'status':{'cpuPolicy':'none'}},write_private=write,
                restore_request_input=restore,scoped_cleanup=cleanup,supervise_core=core)
            with patch.object(R,'load_record',return_value={'original_config':original,'run_id':'run'}),\
                 patch.object(R,'runtime',side_effect=observed),\
                 patch.object(R,'baseline_identity',return_value={'receipt_sha256':'d'*64}),\
                 patch('builtins.print'):
                proof=R.cpu_refusals(lab,7,desired,{},copy.deepcopy(state))
            self.assertEqual(len(calls),2);self.assertEqual(set(proof),{'exclusive-without-static','under-reserved-mask'})
            self.assertEqual(calls[0]['longhorn_v2_cpu_cores'],2)
            self.assertEqual(calls[1]['longhorn_v2_cpu_reservation_percentage'],1)
            self.assertEqual((root/'config.yml').read_text(),original)
            self.assertFalse((root/'longhorn-request-input.json').exists())

    def test_cpu_refusal_rejects_mutation_missing_process_or_unproven_refusal(self):
        for defect in ('missing','reason','config','receipt','disk','pod','process','argv','policy'):
            with self.subTest(defect=defect):
                desired,_,proof=self.cpu_proof_fixture();row=proof['exclusive-without-static']
                if defect=='missing':proof.pop('under-reserved-mask')
                elif defect=='reason':row['reason']='generic refusal'
                elif defect=='config':row['config_after']='b'*64
                elif defect=='receipt':row['receipt_absent']=False
                elif defect=='disk':row['disk_after']={'receipt_sha256':'x'}
                elif defect=='pod':row['after']['pods']['im-0']['pod_uid']='new'
                elif defect=='process':row['after']['runtime']['im-0']['pid']+=1
                elif defect=='argv':row['after']['runtime']['im-0']['argv']+=['--no-huge']
                else:row['policies']['longhorn-node-146']='static'
                with self.assertRaises(ValueError):R.assert_cpu_refusals(proof,desired)

    def test_native_components_require_live_guaranteed_whole_cpu_and_core_count_precedence(self):
        desired=R.profile(config(),'exclusive');lab,objects=self.components_fixture(desired)
        self.assertEqual(len(R.components(lab,desired)['pods']),3)
        for failure in ('owner','image','policy','fractional','memory','core-count','mask','isolation'):
            with self.subTest(failure=failure):
                lab,objects=self.components_fixture(desired)
                pod=objects['pod','im-0',R.NS];container=pod['spec']['containers'][0]
                if failure=='owner':pod['metadata']['ownerReferences'][0]['uid']='foreign'
                elif failure=='image':container['image']='unreviewed:tag'
                elif failure=='policy':objects['nodes.longhorn.io','longhorn-node-146',R.NS]['status']['cpuPolicy']='none'
                elif failure=='fractional':container['resources']['requests']['cpu']='2500m'
                elif failure=='memory':container['resources']['limits']['memory']='256Mi'
                elif failure=='core-count':objects['instancemanagers.longhorn.io','im-0',R.NS]['status']['dataEngineStatus']['v2']['cpuCoreNumber']=1
                elif failure=='mask':container['args'][-1]='0x1'
                else:container['args']+=['--enable-rps']
                if failure in ('owner','image','isolation'):
                    with self.assertRaises(ValueError):R.components(lab,desired)
                else:self.assertIsNone(R.components(lab,desired))

    def test_supported_profiles_measure_mode_mask_live_pools_and_default_reset(self):
        for name in ('original','anonymous-iobuf','exclusive'):
            desired=R.profile(config(),name)
            R.assert_runtime(measured(desired),desired,{'restarts':[0]})
        tuned=R.profile(config(),'anonymous-iobuf')
        self.assertEqual(R.pool_bytes(tuned),392*1024**2)
        self.assertFalse(tuned['longhorn_v2_hugepages'])
        self.assertEqual(R.profile(config(),'original'),config())
        self.assertEqual(R.cpus('0,2-3,65'),{0,2,3,65})

    def test_wrong_runtime_memory_cpu_live_pool_startup_or_restarts_fail(self):
        for failure in ('128Mi-anonymous','reservation','hugetlb','oom','mask','cpuset','livepool','json','stale-json','restart'):
            with self.subTest(failure=failure):
                desired=R.profile(config(),'anonymous-iobuf' if failure in ('128Mi-anonymous','reservation','json') else 'exclusive' if failure in ('mask','cpuset') else 'original')
                row=measured(desired);pod={'restarts':[0]}
                if failure=='128Mi-anonymous':row['memory_limit']=128*1024**2
                elif failure=='reservation':row['nohuge']['files'][0]['size_bytes']=128*1024**2
                elif failure=='hugetlb':row['hugetlb_bytes']=0
                elif failure=='oom':row['memory_events']['oom_kill']=1
                elif failure=='mask':row['argv'][row['argv'].index('-m')+1]='0x1'
                elif failure=='cpuset':row['cpuset']='0-1'
                elif failure=='livepool':row['iobuf_runtime'][0]['params']['large_pool_count']=1
                elif failure=='json':row['startup_json']['subsystems'][0]['config']=[]
                elif failure=='stale-json':row['startup_json']={'subsystems':[]}
                elif failure=='restart':pod['restarts']=[1]
                with self.assertRaises(ValueError):R.assert_runtime(row,desired,pod)

    def test_linux_memfd_reservation_can_be_valid_without_resident_preallocation(self):
        desired = R.profile(config(), 'anonymous-iobuf')
        size = desired['longhorn_v2_memory_mib']*1024**2
        descriptor = os.memfd_create('nohuge', os.MFD_CLOEXEC)
        try:
            os.ftruncate(descriptor, size)
            with mmap.mmap(descriptor, size, flags=mmap.MAP_SHARED) as allocation:
                script = R.runtime_script()
                probe = script[script.index('# DPDK'):script.index('cgroup=')]
                scope = {'p': Path('/proc/self'), 'os': os, 'row': {}}
                exec(compile(probe, 'native-nohuge-reservation', 'exec'), scope)
                row = measured(desired)
                row.update(rss_bytes=300*1024**2, nohuge=scope['row']['nohuge'])
                R.assert_runtime(row, desired, {'restarts': [0]})
                self.assertEqual(allocation.size(), size)
        finally:
            os.close(descriptor)

    def test_anonymous_reservation_rejects_missing_truncated_foreign_and_gapped_mappings(self):
        desired = R.profile(config(), 'anonymous-iobuf')
        for defect in ('absent', 'file-size', 'extent', 'gap', 'overlap', 'foreign', 'permissions'):
            with self.subTest(defect=defect):
                row = measured(desired)
                proof = row['nohuge']
                if defect == 'absent': proof['files'] = []
                elif defect == 'file-size': proof['files'][0]['size_bytes'] //= 2
                elif defect == 'extent': proof['mappings'][0]['size_bytes'] //= 2
                elif defect == 'gap': proof['mappings'][0]['offset'] = 4096
                elif defect == 'overlap': proof['mappings'] *= 2
                elif defect == 'foreign': proof['mappings'][0]['inode'] += 1
                elif defect == 'permissions': proof['mappings'][0]['perms'] = 'rw-p'
                with self.assertRaises(ValueError): R.assert_runtime(row, desired, {'restarts': [0]})

    def test_generated_readonly_probe_compiles_and_uses_actual_wrapper_host_cgroup(self):
        script=R.runtime_script();compile(script,'runtime-probe','exec')
        self.assertIn("Path('/host/sys/fs/cgroup')",script)
        self.assertIn("'framework_get_config'",script)
        self.assertIn("'name':'iobuf'",script)
        self.assertNotIn('write_text',script)
        self.assertNotIn('iobuf_set_options',script)

    def test_host_budget_uses_measured_memory_without_resizing_original_pool(self):
        observed={'MemAvailable':4*1024**2,'MemTotal':8*1024**2,'HugePages_Total':1024,'Hugepagesize':2048}
        def remote(node,args):
            compile(args[2],'memory-probe','exec')
            self.assertNotIn('write',args[2])
            return SimpleNamespace(stdout=json.dumps(observed))
        lab=SimpleNamespace(NODES=('owned',),remote=remote)
        self.assertIn('owned',R.host_budget(lab,R.profile(config(),'anonymous-iobuf')))
        observed['MemAvailable']=1024**2
        with self.assertRaises(ValueError):R.host_budget(lab,R.profile(config(),'anonymous-iobuf'))
        observed['MemAvailable']=4*1024**2;observed['HugePages_Total']=512
        with self.assertRaises(ValueError):R.host_budget(lab,config())

    def test_runtime_shell_restores_before_stopping_its_exact_relay_and_persists_exit(self):
        shell=(ROOT/'tests/helpers/longhorn-v2-runtime-resources.sh').read_text()
        self.assertLess(shell.index('"$DRIVER" cleanup'),shell.index('kill -TERM "$relay_pid"'))
        self.assertIn('"$rc" > "$work/exit"',shell)
        self.assertIn('LONGHORN_V2_RUNTIME_RESOURCES_PASS',shell)
        self.assertNotIn('LONGHORN_V2_PLATFORM_PASS',shell)

    def cpu_fixture(self,directory):
        root=Path(directory)
        for name,data in {'etc/machine-id':'owned-machine','proc/sys/kernel/random/boot_id':'owned-boot',
            'var/lib/kubelet/config.yaml':'cpuManagerPolicy: none\nkubeReserved:\n  cpu: 100m\n',
            'var/lib/kubelet/cpu_manager_state':json.dumps({'policyName':'none','defaultCpuSet':'0-7','entries':{}})}.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
        request={'run_id':'67125100-c9da-4acd-ae31-f081ae18f9e7'}
        events=[]
        def kubelet(args,**kwargs):
            events.append(args[1]);checkpoint=root/'var/lib/kubelet/cpu_manager_state'
            if args[1]=='start' and not checkpoint.exists():
                # The test double represents kubelet, which owns generation.
                desired=yaml.safe_load((root/'var/lib/kubelet/config.yaml').read_text())
                checkpoint.write_text(json.dumps({'policyName':desired['cpuManagerPolicy'],'defaultCpuSet':'0-7','entries':{}}))
            return SimpleNamespace(returncode=0,stdout='active\n')
        return root,request,kubelet,events

    def cpu_request(self,root,request):
        original=R.cpu_action(request,'capture',root)
        desired=yaml.safe_load(base64.b64decode(original['config']['data']))
        desired.update(cpuManagerPolicy='static',reservedSystemCPUs='0')
        return dict(request,original=original,desired=base64.b64encode(json.dumps(desired).encode()).decode())

    def test_cpu_fixture_renames_real_checkpoint_kubelet_generates_and_restores_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root,request,kubelet,events=self.cpu_fixture(directory)
            with patch('subprocess.run',side_effect=kubelet):
                request=self.cpu_request(root,request)
                original=(root/'var/lib/kubelet/cpu_manager_state').read_bytes()
                R.cpu_action(request,'install',root)
                backup=next((root/'var/lib/kubelet').glob('.kubeauto-*.cpu-state'))
                self.assertEqual(backup.read_bytes(),original)
                self.assertEqual(R.cpu_action(request,'observe',root)['policy'],'static')
                R.cpu_action(request,'restore',root)
                self.assertEqual((root/'var/lib/kubelet/cpu_manager_state').read_bytes(),original)
                self.assertEqual((root/'var/lib/kubelet/config.yaml').read_bytes(),base64.b64decode(request['original']['config']['data']))
                self.assertFalse(backup.exists())
                self.assertEqual(events,['is-active','stop','start','stop','start'])

    def test_cpu_capture_or_install_rejects_foreign_state_before_stop(self):
        for failure in ('checkpoint-entries','boot','config','marker'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                root,request,kubelet,events=self.cpu_fixture(directory)
                with patch('subprocess.run',side_effect=kubelet):
                    request=self.cpu_request(root,request)
                    if failure=='checkpoint-entries':
                        (root/'var/lib/kubelet/cpu_manager_state').write_text(json.dumps({'policyName':'static','entries':{'foreign':{'app':'1-2'}}}))
                    elif failure=='boot':(root/'proc/sys/kernel/random/boot_id').write_text('changed')
                    elif failure=='config':(root/'var/lib/kubelet/config.yaml').write_text('foreign')
                    else:
                        marker=root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
                        marker.parent.mkdir(parents=True);marker.write_text('0x1')
                    with self.assertRaises(ValueError):R.cpu_action(request,'capture' if failure=='marker' else 'install',root)
                    self.assertNotIn('stop',events)

    def test_lost_start_response_preserves_original_checkpoint_for_exact_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root,request,kubelet,events=self.cpu_fixture(directory)
            with patch('subprocess.run',side_effect=kubelet):request=self.cpu_request(root,request)
            def interrupted(args,**kwargs):
                result=kubelet(args,**kwargs)
                if args[1]=='start':raise OSError('lost start response')
                return result
            with patch('subprocess.run',side_effect=interrupted):
                with self.assertRaises(OSError):R.cpu_action(request,'install',root)
            with patch('subprocess.run',side_effect=kubelet):R.cpu_action(request,'restore',root)
            self.assertEqual((root/'var/lib/kubelet/config.yaml').read_bytes(),base64.b64decode(request['original']['config']['data']))

    def guarded_product(self):
        path=ROOT/'roles/longhorn/files/longhorn.py';source=path.read_text()
        if R.COMBINATION_REFUSAL not in source:
            anchor="    large = integer(c.get('longhorn_v2_iobuf_large_pool_size', 1024), 1024, 2**63 - 1)\n"
            self.assertEqual(source.count(anchor),1)
            source=source.replace(anchor,"    if cores and not flag(c.get('longhorn_v2_hugepages', True)):\n        raise ValueError("+repr(R.COMBINATION_REFUSAL)+")\n"+anchor)
        module=SimpleNamespace(__file__=str(path))
        exec(compile(source,str(path),'exec'),module.__dict__)
        return module

    def test_reviewed_guard_keeps_independent_profiles_and_rejects_combination_before_api_or_values(self):
        module=self.guarded_product()
        for name in ('original','anonymous-iobuf','exclusive'):
            row=v2_configuration();row.update(R.profile(config(),name));module.validate(row)
        row=v2_configuration();row.update(R.profile(config(),'combined'))
        for action in ('validate','preflight','reconcile'):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'input.json';path.write_text(json.dumps(row))
                with patch.object(module,'API') as api,patch.object(module,'run') as run,patch.object(sys,'argv',
                        ['longhorn.py',action,'--config',str(path),'--kubeconfig','/owned/config']):
                    with self.assertRaisesRegex(ValueError,R.COMBINATION_REFUSAL):module.main()
                    api.assert_not_called();run.assert_not_called()
                    self.assertFalse(path.with_name('values.json').exists())

    def test_product_profile_enters_supervised_kubecli_and_restores_config_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);original=yaml.safe_dump(config());(path/'config.yml').write_text(original)
            state={'run_id':'owned','cluster_uid':'cluster','namespace_uid':'namespace'}
            (path/R.RECORD).write_text(json.dumps(dict(state,original_config=original)))
            def restore():
                receipt=json.loads((path/'longhorn-request-input.json').read_text())
                self.assertEqual(receipt['original'],original)
                (path/'config.yml').write_text(original);(path/'longhorn-request-input.json').unlink()
            @contextmanager
            def scoped(clean):
                try:yield
                finally:clean()
            lab=SimpleNamespace(DIRECTORY=path,NODES=(),owner=lambda:state,scoped_cleanup=scoped,restore_request_input=restore,
                write_private=lambda p,r:p.write_text(json.dumps(r)),supervise_core=MagicMock(side_effect=RuntimeError('product refused')))
            with self.assertRaises(RuntimeError):R.product(lab,101,R.profile(config(),'anonymous-iobuf'))
            lab.supervise_core.assert_called_once_with(101,'longhorn-setup',expected_refusal=None)
            self.assertEqual((path/'config.yml').read_text(),original)

    def test_cleanup_rejects_a_foreign_business_namespace_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            state={'run_id':'owned','cluster_uid':'cluster','namespace_uid':'installation'}
            path=Path(directory)
            row=dict(state,business={'namespace':'owned','namespace_uid':'original','volume_uid':'volume'})
            (path/R.RECORD).write_text(json.dumps(row))
            lab=SimpleNamespace(DIRECTORY=path,NODES=(),owner=lambda:state,load_helper=MagicMock())
            with self.assertRaisesRegex(ValueError,'another module'):
                R.only_business(lab,dict(namespace='foreign',namespace_uid='other',volume_uid='volume'))
            lab.load_helper.assert_not_called()

    def test_namespace_lost_create_reply_recovers_only_matching_operation_token(self):
        for foreign in (False,True):
            with self.subTest(foreign=foreign),tempfile.TemporaryDirectory() as directory:
                path=Path(directory);state={'run_id':'owned','cluster_uid':'cluster','namespace_uid':'installation'}
                (path/R.RECORD).write_text(json.dumps(state));objects={}
                def create(*args,**kwargs):
                    obj=json.loads(kwargs['stdin']);obj['metadata']['uid']='created-namespace'
                    objects[obj['metadata']['name']]=obj
                    self.assertIn('namespace_intent',json.loads((path/R.RECORD).read_text()))
                    raise OSError('lost namespace-create reply')
                lab=SimpleNamespace(DIRECTORY=path,NODES=(),owner=lambda:state,
                    get=lambda kind,name:objects.get(name),kubectl=create,
                    write_private=lambda p,r:p.write_text(json.dumps(r)))
                with self.assertRaises(OSError):R.business_namespace(lab,create=True)
                if foreign:next(iter(objects.values()))['metadata']['annotations']['kubeauto.io/runtime-resource-operation']='foreign'
                if foreign:
                    with self.assertRaisesRegex(ValueError,'foreign object'):R.business_namespace(lab)
                    self.assertFalse((path/R.BUSINESS).exists())
                else:
                    journal=R.business_namespace(lab)
                    self.assertEqual(journal['namespace_uid'],'created-namespace')
                    self.assertEqual(json.loads((path/R.RECORD).read_text())['business']['namespace_uid'],'created-namespace')


class DesktopContracts(unittest.TestCase):
 def fixture(self,root,*,active='active',foreign_user=False,unit='gdm.service',missing=False):
  for name,value in [('etc/machine-id','owned'),('proc/sys/kernel/random/boot_id','boot'),('usr/lib/systemd/system/gdm.service','unit-v1')]:
   p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
  state={'active':active,'mutations':[]}
  def run(args,**kwargs):
   if args[:2]==['systemctl','show']:
    result='Id='+unit+'\nLoadState='+('not-found' if missing else 'loaded')+'\nActiveState='+state['active']+'\nFragmentPath=/usr/lib/systemd/system/gdm.service\n'
   elif args[:2]==['loginctl','list-sessions']:
    if args[2:]!=['--no-legend','--no-pager']:return SimpleNamespace(returncode=1,stdout='',stderr='unrecognized option')
    result='gdm 60578 greeter seat0 tty1\nssh 0 root - -\n'
   elif args[:2]==['loginctl','show-session']:
    result='Class='+('user' if foreign_user or args[2]=='ssh' else 'greeter')+'\nType='+('tty' if args[2]=='ssh' else 'wayland')+'\n'
   elif args[:2] in (['systemctl','stop'],['systemctl','start']):
    state['mutations'].append(args);state['active']='active' if args[1]=='start' else 'inactive';result=''
   else:raise AssertionError(args)
   return SimpleNamespace(returncode=0,stdout=result)
  return state,patch('subprocess.run',side_effect=run)
 def test_exact_login_screen_stop_restore_and_inactive_or_absent_noop(self):
  for active,missing in [('active',False),('inactive',False),('inactive',True)]:
   with self.subTest(active=active,missing=missing),tempfile.TemporaryDirectory() as directory:
    root=Path(directory);state,mock=self.fixture(root,active=active,missing=missing)
    with mock:
     request={'original':R.desktop_action({},'capture',root)}
     self.assertEqual('inactive',R.desktop_action(request,'stop',root)['active'])
     self.assertEqual(active,R.desktop_action(request,'restore',root)['active'])
    self.assertEqual(2 if active=='active' else 0,len(state['mutations']))
 def test_foreign_user_unit_boot_or_fragment_refuses_before_mutation(self):
  for defect in ('user','unit','boot','fragment'):
   with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
    root=Path(directory);state,mock=self.fixture(root,foreign_user=defect=='user',unit='foreign.service' if defect=='unit' else 'gdm.service')
    with mock:
     if defect in ('user','unit'):
      with self.assertRaises(ValueError):R.desktop_action({},'capture',root)
     else:
      request={'original':R.desktop_action({},'capture',root)}
      (root/('proc/sys/kernel/random/boot_id' if defect=='boot' else 'usr/lib/systemd/system/gdm.service')).write_text('foreign')
      with self.assertRaises(ValueError):R.desktop_action(request,'stop',root)
    self.assertEqual([],state['mutations'])
 def test_lost_stop_response_is_recoverable_and_keeps_original_active_state(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);state,mock=self.fixture(root)
   with mock:
    request={'original':R.desktop_action({},'capture',root)}
    state['active']='inactive'
    self.assertEqual('active',R.desktop_action(request,'restore',root)['active'])
   self.assertEqual([['systemctl','start','gdm.service']],state['mutations'])
 def test_actual_preparation_journals_before_stop_and_cleanup_recovers_only_owned_hosts(self):
  for foreign in (False,True):
   with self.subTest(foreign=foreign),tempfile.TemporaryDirectory() as directory:
    root=Path(directory);nodes=('one','two');owner={'run_id':'run','cluster_uid':'cluster','namespace_uid':'namespace','raw_disks':{n:{'machine_id':n} for n in nodes}}
    path=root/R.RECORD;path.write_text(json.dumps({k:owner[k] for k in ('run_id','cluster_uid','namespace_uid')}))
    lab=SimpleNamespace(DIRECTORY=root,NODES=nodes,owner=lambda:owner,write_private=lambda p,r:p.write_text(json.dumps(r)))
    calls=[]
    def remote(lab,request,action):
     node=request['node'];calls.append((node,action))
     if action=='capture':return {'machine_id':'foreign' if foreign and node=='two' else node,'boot_id':'boot','unit':'gdm.service','active':'active','fragment_sha256':'sha'}
     row=json.loads(path.read_text());self.assertEqual('stop-intent' if action=='stop' else 'stopped',row['desktop_hosts'][node]['phase'])
     return {'active':'inactive' if action=='stop' else 'active'}
    with patch.object(R,'desktop_remote',side_effect=remote):
     if foreign:
      with self.assertRaises(ValueError):R.prepare_desktops(lab)
      R.restore_desktops(lab)
      self.assertEqual([('one','capture'),('two','capture')],calls)
     else:
      R.prepare_desktops(lab);R.restore_desktops(lab)
      self.assertEqual([('one','capture'),('two','capture'),('one','stop'),('two','stop'),('two','restore'),('one','restore')],calls)
      self.assertTrue(all(h['phase']=='restored' for h in json.loads(path.read_text())['desktop_hosts'].values()))
 def test_business_and_cleanup_own_the_desktop_lifecycle_without_weakening_budget(self):
  import inspect
  source=inspect.getsource(R.business);cleanup=inspect.getsource(R.cleanup)
  self.assertLess(source.index('with lab.scoped_cleanup'),source.index('prepare_desktops(lab)'))
  self.assertLess(source.index('prepare_desktops(lab)'),source.index('prepare_cpus(lab)'))
  self.assertLess(cleanup.index('restore_cpus(lab)'),cleanup.index('restore_desktops(lab)'))
  self.assertLess(cleanup.index('restore_desktops(lab)'),cleanup.index('path.unlink()'))
  self.assertIn("required = desired['longhorn_v2_memory_mib']*1024 + 1024**2",inspect.getsource(R.host_budget))


if __name__=='__main__':unittest.main()

class RefusalDerivedInputContracts(unittest.TestCase):
 def test_cleanup_reconciles_original_input_even_before_runtime_profile_intent(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);original=yaml.safe_dump(config());(root/'config.yml').write_text(original)
   row={'original_config':original,'profile_intent':False,'native':{},'baseline_identity':{'disk':'original'}}
   (root/R.RECORD).write_text('{}')
   events=[]
   lab=SimpleNamespace(DIRECTORY=root,restore_request_input=lambda:events.append('request-restored'),
      installation_health=lambda:events.append('health'),wait=lambda label,timeout,check:check())
   with patch.object(R,'load_record',return_value=row),patch.object(R,'product',side_effect=lambda *a:events.append('product')),\
        patch.object(R,'runtime',side_effect=lambda *a:events.append('runtime')),patch.object(R,'restore_cpus'),\
        patch.object(R,'restore_desktops'),patch.object(R,'baseline_identity',return_value=row['baseline_identity']),patch('builtins.print'):
    R.cleanup(lab,7)
   self.assertEqual(events,['request-restored','product','runtime','health'])


class CleanupConvergence(unittest.TestCase):
    def run_cleanup(self, states):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = yaml.safe_dump(config())
            (root/'config.yml').write_text(original)
            (root/R.RECORD).write_text('{}')
            row = {'original_config': original, 'native': {}, 'baseline_identity': {'disk': 'original'}}
            events = []
            health = MagicMock(side_effect=states)
            def wait(label, timeout, check):
                self.assertEqual((label, timeout), ('runtime-restored-installation-health', 600))
                events.append('wait')
                for _ in range(3):
                    if check(): return True
                raise TimeoutError(label)
            lab = SimpleNamespace(DIRECTORY=root, restore_request_input=lambda: events.append('input'),
                                  installation_health=health, wait=wait)
            with patch.object(R, 'load_record', return_value=row), \
                 patch.object(R, 'product', side_effect=lambda *a: events.append('product')), \
                 patch.object(R, 'runtime', side_effect=lambda *a: events.append('runtime')), \
                 patch.object(R, 'restore_cpus', side_effect=lambda *a: events.append('cpus')), \
                 patch.object(R, 'restore_desktops', side_effect=lambda *a: events.append('desktops')), \
                 patch.object(R, 'baseline_identity', side_effect=lambda *a: events.append('identity') or row['baseline_identity']), \
                 patch('builtins.print') as output:
                try:
                    R.cleanup(lab, 7)
                finally:
                    self.assertEqual(events, ['input', 'product', 'runtime', 'cpus', 'desktops', 'identity', 'wait'])
                    if health.call_count != len(states): self.fail('health retry count changed')
                    self.assertFalse(any('SCOPED_CLEAN_PASS' in str(c) for c in output.call_args_list)
                                     if isinstance(states[-1], Exception) else False)
            self.assertTrue(any('SCOPED_CLEAN_PASS' in str(c) for c in output.call_args_list))

    def test_cleanup_waits_for_control_recovery_after_restoring_cpus_and_desktops(self):
        self.run_cleanup([ValueError('Longhorn control or disk health failed'), None])

    def test_persistent_unhealthy_installation_times_out_without_clean_marker(self):
        with self.assertRaises(TimeoutError):
            self.run_cleanup([ValueError('Longhorn control or disk health failed') for _ in range(3)])

    def test_identity_credentials_and_other_failures_are_not_retried(self):
        for error in (ValueError('Longhorn fixture namespace or credentials changed'),
                      ValueError('Longhorn client inventory differs from the owned baseline'),
                      RuntimeError('API transport failed')):
            with self.subTest(error=str(error)), self.assertRaises(type(error)):
                self.run_cleanup([error])


class ProfileLedgers(unittest.TestCase):
    def test_profiles_keep_separate_acknowledged_bytes_and_refuse_reuse(self):
        import subprocess
        faults = R.load('longhorn-faults.py')
        control = R.load('longhorn-control-faults.py')
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory)
            def kubectl(*args, **kwargs):
                code = args[args.index('-c')+1].replace('/data/', str(mount)+'/')
                result = subprocess.run([sys.executable, '-c', code],
                    input=kwargs.get('stdin'), text=True, capture_output=True, check=True)
                return result
            lifecycle = SimpleNamespace(io=lambda *args, **kwargs: None)
            modules = {'longhorn-faults.py': faults, 'longhorn-control-faults.py': control,
                       'longhorn-v2-clone-lifecycle.py': lifecycle}
            lab = SimpleNamespace(kubectl=kubectl, load_helper=modules.__getitem__)
            journal = {'namespace': 'owned-runtime'}
            original_program = faults.ledger_program
            def short_program(token, duration):
                return original_program(token, duration=0.1).replace('stop=ledger_stop_requested', 'interval=0,stop=ledger_stop_requested')
            snapshots = {}
            with patch.object(faults, 'ledger_program', side_effect=short_program):
                for name in ('runtime-anonymous-iobuf', 'runtime-exclusive', 'runtime-original'):
                    ledger = R.observe_io(lab, journal, name, 'run-id')
                    self.assertEqual(control.confirmed(ledger['records'], ledger['token'], faults), ledger)
                    self.assertGreater(ledger['summary']['confirmed'], 0)
                    current = {p.name: p.read_bytes() for p in mount.iterdir()}
                    self.assertEqual({k: current[k] for k in snapshots}, snapshots)
                    self.assertEqual(len(current), len(snapshots)+1)
                    snapshots = current
                with self.assertRaises(subprocess.CalledProcessError):
                    R.observe_io(lab, journal, 'runtime-exclusive', 'run-id')
            self.assertEqual({p.name: p.read_bytes() for p in mount.iterdir()}, snapshots)

