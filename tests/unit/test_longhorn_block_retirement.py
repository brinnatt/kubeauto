"""Exact AIO retirement; native CR absence alone cannot sign disk release."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import yaml
from unittest.mock import patch

from tests.unit.test_longhorn_delivery import product, FakeAPI, native, module, ROOT
from tests.unit.test_longhorn_v2 import v2_configuration

retire=module('block-retire.py')
host=module('block-check.py')


def runtime_fixture():
    return {'process':{'pid':8,'start_time':'123'},'bdevs':[
        {'name':'target','product_name':'AIO disk','uuid':'bdev-target','driver_specific':{'aio':{
            'filename':'/host/dev/disk/by-id/wwn-target','resolved_filename':'/host/dev/sdb'}}},
        {'name':'peer','product_name':'AIO disk','uuid':'bdev-peer','driver_specific':{'aio':{
            'filename':'/host/dev/disk/by-id/wwn-peer','resolved_filename':'/host/dev/sdc'}}}],
        'lvstores':[{'uuid':'disk-target','name':'target','base_bdev':'target'},
            {'uuid':'disk-peer','name':'peer','base_bdev':'peer'}],
        'controllers':[{'name':'remote-engine','ctrlrs':[{'state':'enabled','cntlid':3,
            'trid':{'traddr':'10.0.0.1','trtype':'TCP'},'host':{'nqn':'nqn-test'}}]}],
        'manager':{'name':'im','im_uid':'im-uid','pod_uid':'pod-uid','image':'pinned'}}


def retired_fixture():
    row=runtime_fixture();row['bdevs']=row['bdevs'][1:];row['lvstores']=row['lvstores'][1:];return row


class RpcWireContract(unittest.TestCase):
    def snapshot(self, fail=False):
        requests=[]
        class Connection:
            def __init__(self,*args): pass
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def settimeout(self,value): pass
            def connect(self,path): self.path=path
            def sendall(self,data): self.request=json.loads(data);requests.append(self.request)
            def recv(self,size):
                request=self.request
                if 'params' in request or fail:
                    return json.dumps({'id':request['id'],'error':{'code':-22,'message':'Invalid argument'}}).encode()
                result={'bdev_get_bdevs':[], 'bdev_lvol_get_lvstores':[{'uuid':'owned-store'}],
                        'bdev_nvme_get_controllers':[]}[request['method']]
                return json.dumps({'id':request['id'],'result':result}).encode()
        with tempfile.TemporaryDirectory() as directory:
            process=Path(directory)/'123';process.mkdir()
            (process/'cmdline').write_bytes(b'/usr/bin/spdk_tgt\0')
            (process/'stat').write_text('123 (spdk_tgt) '+' '.join(['S']+['0']*18+['99']))
            output=io.StringIO()
            with patch.object(Path,'iterdir',return_value=iter([process])),patch('socket.socket',Connection),redirect_stdout(output):
                retire.rpc_snapshot()
        return requests,json.loads(output.getvalue())

    def test_parameterless_spdk_queries_omit_params_like_pinned_official_client(self):
        requests,result=self.snapshot()
        self.assertEqual(['bdev_get_bdevs','bdev_lvol_get_lvstores','bdev_nvme_get_controllers'],[r['method'] for r in requests])
        self.assertTrue(all(set(r)=={'jsonrpc','id','method'} for r in requests))
        self.assertEqual([{'uuid':'owned-store'}],result['lvstores'])
        self.assertEqual({'pid':123,'start_time':'99'},result['process'])

    def test_native_query_error_still_refuses_retirement(self):
        with self.assertRaisesRegex(ValueError,'SPDK state query failed'):self.snapshot(fail=True)


class RuntimeAbsence(unittest.TestCase):
    def setUp(self):
        self.target={'name':'target','disk_uuid':'disk-target','resolved_path':'/host/dev/sdb'}
        self.before=runtime_fixture()

    def test_cr_removed_but_aio_or_lvstore_remaining_is_not_retirement(self):
        self.assertFalse(retire.assert_runtime(self.before,[self.target],removed=True,before=self.before))
        after=retired_fixture();after['lvstores'].append(self.before['lvstores'][0])
        self.assertFalse(retire.assert_runtime(after,[self.target],removed=True,before=self.before))
        after=retired_fixture();after['bdevs'][0]['driver_specific']['aio']['resolved_filename']='/host/dev/sdb'
        self.assertFalse(retire.assert_runtime(after,[self.target],removed=True,before=self.before))

    def test_exact_uuid_and_device_path_required_before_delete(self):
        self.assertTrue(retire.assert_runtime(self.before,[self.target],removed=False))
        changed=copy.deepcopy(self.before);changed['lvstores'][0]['uuid']='replacement'
        with self.assertRaisesRegex(ValueError,'UUID'):retire.assert_runtime(changed,[self.target],removed=False)
        changed=copy.deepcopy(self.before);changed['bdevs'][0]['driver_specific']['aio']['resolved_filename']='/host/dev/sdc'
        with self.assertRaisesRegex(ValueError,'device path'):retire.assert_runtime(changed,[self.target],removed=False)

    def test_lvol_dependency_prevents_any_release(self):
        changed=retired_fixture();changed['bdevs'].append({'name':'untracked-lvol','driver_specific':{'lvol':{'lvol_store_uuid':'disk-target'}}})
        with self.assertRaisesRegex(ValueError,'lvol dependencies'):retire.assert_runtime(changed,[self.target],removed=True)

    def test_peer_disk_and_nested_nvme_controller_identity_preserved(self):
        after=retired_fixture();self.assertTrue(retire.assert_runtime(after,[self.target],removed=True,before=self.before))
        for variant in ('peer','controller','process'):
            changed=copy.deepcopy(after)
            if variant=='peer':changed['lvstores'][0]['uuid']='replacement'
            elif variant=='controller':changed['controllers'][0]['ctrlrs'][0]['trid']['traddr']='10.0.0.2'
            else:changed['process']['pid']=9
            with self.subTest(variant=variant),self.assertRaisesRegex(ValueError,'another disk/controller'):
                retire.assert_runtime(changed,[self.target],removed=True,before=self.before)


class HostReceipt(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.marker=self.root/'disks.json'
        self.old={'cluster_uid':'cluster','node_uid':'node','platform':{'cpu_mask':'0x1'},'disks':{}}
        declarations=[]
        for index,name in enumerate(('target','peer')):
            declaration={'name':name,'path':'/dev/disk/by-id/wwn-'+name,'disk_driver':'aio','identity':'wwn-'+name,'storage_reserved':0}
            declarations.append(declaration)
            self.old['disks'][name]={key:value for key,value in declaration.items() if key in ('path','disk_driver','identity')}
            self.old['disks'][name].update(wwn='wwn-'+name,serial=name,size_bytes=107374182400,device_number='8:'+str(index+16))
        self.original=json.dumps(self.old).encode();self.marker.write_bytes(self.original)
        self.c={'id':'retire-1','node':'192.168.47.146','cluster_uid':'cluster','node_uid':'node','minimum_gib':20,
            'disks':declarations,'targets':[{'node':'192.168.47.146','name':'target','disk_uuid':'disk-target'}]}
        self.inspect=patch.object(host,'inspect_disk',side_effect=lambda disk,*a,**k:copy.deepcopy(self.old['disks'][disk['name']]))
        self.inspect.start();self.addCleanup(self.inspect.stop)
        self.proc=patch.object(Path,'iterdir',return_value=iter(()));self.proc.start();self.addCleanup(self.proc.stop)

    def prove(self):
        checked=host.retire_receipt(self.c,self.marker)
        self.c['proof']={'id':self.c['id'],'cluster_uid':'cluster','node_uid':'node','targets':self.c['targets'],
            'before_sha256':checked['marker_sha256'],'native_absent':True}
        return checked

    def test_releases_only_target_and_replays_with_original_peer_policy(self):
        self.prove();result=host.retire_receipt(self.c,self.marker,apply=True)
        after=json.loads(self.marker.read_text());self.assertEqual(after['disks'],{'peer':self.old['disks']['peer']})
        self.assertEqual(after['platform'],self.old['platform']);self.assertTrue(result['data_preserved'])
        self.assertEqual(host.retire_receipt(self.c,self.marker,apply=True),result)

    def test_missing_native_absence_or_wrong_before_hash_preserves_host_receipt(self):
        self.prove()
        for key,value in (('native_absent',False),('before_sha256','foreign')):
            original=copy.deepcopy(self.c['proof']);self.c['proof'][key]=value
            with self.assertRaisesRegex(ValueError,'exact control proof'):host.retire_receipt(self.c,self.marker,apply=True)
            self.assertEqual(self.marker.read_bytes(),self.original);self.c['proof']=original

    def test_foreign_host_peer_receipt_and_replacement_identity_are_rejected(self):
        self.prove();self.marker.write_text('{"foreign":true}')
        with self.assertRaisesRegex(ValueError,'cluster/node UID'):host.retire_receipt(self.c,self.marker,apply=True)
        self.assertEqual(json.loads(self.marker.read_text()),{'foreign':True})
        self.marker.write_bytes(self.original);self.old['disks']['target']['identity']='replacement'
        with self.assertRaisesRegex(ValueError,'stable raw identity'):host.retire_receipt(self.c,self.marker,apply=True)

    def test_interrupted_host_release_resumes_exact_archive_after_receipt_write(self):
        self.prove();result=host.retire_receipt(self.c,self.marker,apply=True)
        archive=self.root/'longhorn-block-retirement-retire-1.json'
        state=json.loads(archive.read_text());state['phase']='release-intent';archive.write_text(json.dumps(state))
        self.assertEqual(host.retire_receipt(self.c,self.marker,apply=True),result)
        self.assertEqual(json.loads(archive.read_text())['phase'],'complete')

    def test_last_disk_release_removes_receipt_and_retains_exact_completed_archive(self):
        self.old['disks'].pop('peer');self.c['disks']=self.c['disks'][:1];self.marker.write_text(json.dumps(self.old));self.prove()
        result=host.retire_receipt(self.c,self.marker,apply=True)
        self.assertFalse(self.marker.exists());self.assertIsNone(result['after_sha256'])
        archive=json.loads((self.root/'longhorn-block-retirement-retire-1.json').read_text())
        self.assertEqual(archive['phase'],'complete');self.assertEqual(host.retire_receipt(self.c,self.marker,apply=True),result)


class ControlRelease(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.values=self.root/'values.json';self.native={'cluster_uid':'cluster','namespace_uid':'namespace','nodes':{
            '192.168.47.146':{'name':'node','kubernetes_node_uid':'node-uid','longhorn_node_uid':'native-node',
                'disks':{'target':{'disk_uuid':'disk-target'},'peer':{'disk_uuid':'disk-peer'}}}}}
        self.receipt=self.root/'block-disks.json';self.receipt.write_text(json.dumps(self.native))
        self.row={'id':'retire-1','phase':'native-removed','native_before':self.native,'hosts':{'192.168.47.146':{
            'node_uid':'node-uid','targets':[{'name':'target','disk_uuid':'disk-target'}],
            'host_before':{'marker_sha256':'before-hash','targets':[{'node':'192.168.47.146','name':'target','disk_uuid':'disk-target'}]}}},
            'cluster_uid':'cluster','namespace_uid':'namespace'}
        self.journal=self.root/'operation.json';self.config={'longhorn_block_retirement_release_receipts':{}}
        self.planner=patch.object(retire,'plan',return_value=(self.journal,self.row));self.planner.start();self.addCleanup(self.planner.stop)

    def test_no_host_proof_keeps_all_control_ownership(self):
        with self.assertRaisesRegex(ValueError,'every host'):retire.finalize(self.config,None,self.values,None)
        self.assertEqual(json.loads(self.receipt.read_text()),self.native)

    def test_precise_control_release_follows_every_host_proof_and_preserves_peer(self):
        address='192.168.47.146';hostrow=self.row['hosts'][address]
        self.config['longhorn_block_retirement_release_receipts'][address]={'id':'retire-1','cluster_uid':'cluster','node_uid':'node-uid',
            'before_sha256':'before-hash','targets':hostrow['host_before']['targets'],'data_preserved':True}
        retire.finalize(self.config,None,self.values,None)
        after=json.loads(self.receipt.read_text());self.assertEqual(after['nodes'][address]['disks'],{'peer':{'disk_uuid':'disk-peer'}})
        self.assertEqual(self.row['phase'],'complete');retire.finalize(self.config,None,self.values,None)


class RetirementPlaybook(unittest.TestCase):
    def test_all_host_checks_precede_native_and_every_release_precedes_finalize(self):
        plays=yaml.safe_load((ROOT/'playbooks/09.longhorn.yml').read_text())
        checks=next(i for i,p in enumerate(plays) if any(t.get('register')=='longhorn_block_retirement_check' for t in p.get('tasks',[])))
        native_reconcile=next(i for i,p in enumerate(plays) if any(r.get('longhorn_phase')=='reconcile' for r in p.get('roles',[])))
        releases=next(i for i,p in enumerate(plays) if any(t.get('register')=='longhorn_block_retirement_release' for t in p.get('tasks',[])))
        finalize=next(i for i,p in enumerate(plays) if any(t.get('block') and any(c.get('register')=='longhorn_block_retirement_final' for c in t['block']) for t in p.get('tasks',[])))
        self.assertLess(checks,native_reconcile);self.assertLess(native_reconcile,releases);self.assertLess(releases,finalize)
        for index in (checks,releases):
            self.assertTrue(plays[index]['any_errors_fatal']);self.assertFalse(plays[index]['gather_facts'])
            task=plays[index]['tasks'][0]
            self.assertIn("longhorn_action | default('present') == 'retire-block-disks'",task['when'])
            self.assertIn("inventory_hostname in (longhorn_retire_block_disks",task['when'][-1])
        self.assertIn('native SPDK absence',plays[releases]['name'])


class NativeDeletion(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.values=self.root/'values.json';self.journal=self.root/'plan.json'
        target={'node':'192.168.47.146','name':'target','disk_uuid':'disk-target',
            'path':'/dev/disk/by-id/wwn-target','identity':'wwn-target','resolved_path':'/host/dev/sdb'}
        disk={'path':target['path'],'diskType':'block','diskDriver':'aio','allowScheduling':False,'evictionRequested':True}
        peer=dict(disk,path='/dev/disk/by-id/wwn-peer')
        self.node={'metadata':{'uid':'native-node','resourceVersion':'1'},'spec':{'disks':{'target':disk,'peer':peer}},
            'status':{'diskStatus':{'target':{'diskUUID':'disk-target'},'peer':{'diskUUID':'disk-peer'}}}}
        self.host={'node':'node','native_uid':'native-node','targets':[target],'spec_before':copy.deepcopy(self.node['spec']['disks']),
            'runtime_before':runtime_fixture()}
        self.row={'phase':'preflight','hosts':{'192.168.47.146':self.host}}
        self.writes=[]
        def get(kind,name='',namespace=''):
            if kind=='nodes.longhorn.io':return copy.deepcopy(self.node)
            return None if name else {'items':[]}
        def update(kind,name,value,namespace):
            self.writes.append(value)
            for key,item in value['spec']['disks'].items():
                self.assertIsNone(item);self.node['spec']['disks'].pop(key);self.node['status']['diskStatus'].pop(key)
        self.api=SimpleNamespace(get=get,patch=update)
        self.product=SimpleNamespace(CommandError=product.CommandError,block_disk_drained=lambda *a:True,
            wait=lambda label,timeout,check: self.assertTrue(check()))
        self.config={'longhorn_ready_timeout':60}

    def test_native_status_disappearance_without_spdk_absence_does_not_release_receipt(self):
        def wait(label,timeout,check):
            self.assertFalse(check());raise TimeoutError('native DiskDelete incomplete')
        self.product.wait=wait
        with patch.object(retire,'preflight',return_value=(self.journal,self.row)), \
                patch.object(retire,'dependencies'),patch.object(retire,'runtime',return_value=runtime_fixture()):
            with self.assertRaisesRegex(TimeoutError,'DiskDelete incomplete'):
                retire.reconcile(self.config,self.api,self.values,self.product)
        self.assertNotIn('target',self.node['status']['diskStatus']);self.assertFalse(self.host.get('removed'))
        self.assertEqual(self.row['phase'],'preflight');self.assertEqual(len(self.writes),1)

    def test_exact_native_patch_preserves_peer_and_never_releases_control_receipt(self):
        with patch.object(retire,'preflight',return_value=(self.journal,self.row)), \
                patch.object(retire,'dependencies'),patch.object(retire,'runtime',return_value=retired_fixture()):
            retire.reconcile(self.config,self.api,self.values,self.product)
        self.assertEqual(self.writes[0]['spec']['disks'],{'target':None})
        self.assertEqual(self.node['spec']['disks'],{'peer':self.host['spec_before']['peer']})
        self.assertTrue(self.host['removed']);self.assertEqual(self.row['phase'],'native-removed')
        self.assertFalse(self.values.with_name('block-disks.json').exists())

    def test_native_conflict_retries_fresh_resource_version_without_overwriting_peer(self):
        original=self.api.patch;attempts=[]
        def update(*args):
            attempts.append(args[2]['metadata']['resourceVersion'])
            if len(attempts)==1:
                self.node['metadata']['resourceVersion']='2'
                raise product.CommandError('kubectl',1,'Error from server (Conflict): changed')
            return original(*args)
        self.api.patch=update
        with patch.object(retire,'preflight',return_value=(self.journal,self.row)), \
                patch.object(retire,'dependencies'),patch.object(retire,'runtime',return_value=retired_fixture()):
            retire.reconcile(self.config,self.api,self.values,self.product)
        self.assertEqual(attempts,['1','2']);self.assertEqual(len(self.writes),1)
        self.assertEqual(self.node['spec']['disks'],{'peer':self.host['spec_before']['peer']})

    def test_replay_after_native_removal_only_observes_and_never_readds_disk(self):
        self.node['spec']['disks'].pop('target');self.node['status']['diskStatus'].pop('target')
        with patch.object(retire,'preflight',return_value=(self.journal,self.row)), \
                patch.object(retire,'dependencies'),patch.object(retire,'runtime',return_value=retired_fixture()):
            retire.reconcile(self.config,self.api,self.values,self.product)
        self.assertFalse(self.writes);self.assertEqual(self.row['phase'],'native-removed')

    def test_all_hosts_preflight_before_any_native_mutation(self):
        second=copy.deepcopy(self.host);second['node']='other-node';self.row['hosts']['192.168.47.147']=second
        self.host.pop('runtime_before');second.pop('runtime_before')
        def runtime(api,node,*args):
            if node=='other-node':raise ValueError('second host changed')
            return runtime_fixture()
        with patch.object(retire,'plan',return_value=(self.journal,self.row)),patch.object(retire,'host_inputs'), \
                patch.object(retire,'runtime',side_effect=runtime):
            with self.assertRaisesRegex(ValueError,'second host changed'):
                retire.preflight(self.config,self.api,self.values,self.product,record=True)
        self.assertFalse(self.writes);self.assertFalse(self.journal.exists())


class RetirementPreflight(unittest.TestCase):
    def test_retirement_replay_uses_owned_installation_and_not_present_identity_path(self):
        api=FakeAPI();api.put('namespace',native('Namespace','longhorn-system'))
        releases=[{'name':'longhorn','namespace':'longhorn-system','chart':'longhorn-1.12.1','app_version':'v1.12.1'}]
        with patch.object(product,'run',return_value=json.dumps(releases)), \
                patch.object(product,'check_installation_identity'),patch.object(product,'node_map',return_value={}), \
                patch.object(product,'check_block_identity',side_effect=AssertionError('present path cannot accept retired disks')), \
                patch.object(product,'block_retirement',return_value=retire),patch.object(retire,'preflight') as preflight:
            result=product.preflight({'longhorn_action':'retire-block-disks'},api,'helm','values.json')
        self.assertEqual(result,({},releases[0]));preflight.assert_called_once()

    def test_optional_null_backing_image_maps_are_not_disk_consumers(self):
        api=FakeAPI();api.put('crd',{'metadata':{'name':'backingimages.longhorn.io'}})
        api.listing('backingimages.longhorn.io',[{'spec':{'disks':None,'diskFileSpecMap':None},
            'status':{'diskFileStatusMap':None}}],retire.NS)
        retire.dependencies(api,[{'disk_uuid':'disk-target'}],product)

    def test_pending_backing_image_spec_copy_or_v2_first_copy_blocks_native_delete(self):
        for spec,status in (({'diskFileSpecMap':{'disk-target':{}}},{}),({}, {'v2FirstCopyDisk':'disk-target'})):
            api=FakeAPI();api.put('crd',{'metadata':{'name':'backingimages.longhorn.io'}})
            api.listing('backingimages.longhorn.io',[{'spec':spec,'status':status}],retire.NS)
            with self.subTest(spec=spec,status=status),self.assertRaisesRegex(ValueError,'backingimages'):
                retire.dependencies(api,[{'disk_uuid':'disk-target'}],product)


class ProductConfiguration(unittest.TestCase):
    def test_explicit_action_requires_exact_unique_approval_and_prior_eviction(self):
        config=v2_configuration();address=config['longhorn_v2_nodes'][0];disk=config['longhorn_block_disks'][address][0]
        config.update(longhorn_action='retire-block-disks',longhorn_block_retirement_id='change-1',
            longhorn_retire_block_disks=[{'node':address,'name':disk['name'],'disk_uuid':'native-uuid'}])
        with self.assertRaisesRegex(ValueError,'scheduling disabled'):product.validate(config)
        disk.update(allow_scheduling=False,eviction_requested=True);product.validate(config)
        config['longhorn_retire_block_disks']*=2
        with self.assertRaisesRegex(ValueError,'duplicate'):product.validate(config)


if __name__=='__main__':unittest.main()
