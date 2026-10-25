"""Precise admission, identity, teardown and observable-operation contracts."""
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
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('disk_lifecycle_contract', ROOT/'tests/helpers/longhorn-disk-lifecycle.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class DiskLifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.run_id = '11111111-1111-1111-1111-111111111111'
        self.lab = SimpleNamespace(DIRECTORY=Path(self.directory.name), WORKERS=('192.168.122.212', '192.168.122.165', '192.168.122.238'),
            DATA='/data/kubeauto-longhorn', owner=Mock(return_value={'run_id': self.run_id,
                'disks': {'192.168.122.212': {'device': '/dev/disk/by-path/pci-main'}}}),
            write_private=lambda p,d:p.write_text(json.dumps(d)), get=Mock(), kubectl=Mock(), remote=Mock(),
            remove=Mock(), wait=Mock(side_effect=lambda label, timeout, predicate: predicate()),
            installation_health=Mock(), run_requests=Mock(), disk_plan=Mock(), verify_device=Mock())
        self.workload = SimpleNamespace(cleanup=Mock(), journal=Mock(return_value={'volume':'source', 'volume_uid':'source-uid'}),
            data=Mock(), prepare=Mock(), detach=Mock(), mount=Mock())
        self.scenario = MODULE.Lifecycle(self.lab, self.workload, 123)
        self.main = {'path': self.lab.DATA, 'diskType':'filesystem', 'diskDriver':'', 'storageReserved':1024,
                     'tags':[], 'allowScheduling':True, 'evictionRequested':False}
        self.disks = []
        for i in range(2):
            self.disks.append({'node': self.lab.WORKERS[0], 'name':'kubeauto-extra26-'+str(i),
                'path':MODULE.EXTRA, 'device':'/dev/disk/by-path/pci-extra'+str(i), 'kernel':'/dev/vd'+str(i),
                'identity':'path:pci-extra'+str(i), 'size':100*1024**3,
                'uuid':str(uuid.uuid5(uuid.UUID(self.run_id), 'disk26:'+self.lab.WORKERS[0]+':'+str(i))),
                'phase':'planned', 'fstab_line':'owned', 'disk_uuid':'native-extra'+str(i)})
        self.scenario.row = {'id':'abc', 'run_id':self.run_id, 'node':'longhorn-node-212', 'node_uid':'node-uid',
            'main':'main', 'original_disks':{'main':copy.deepcopy(self.main)}, 'disks':self.disks,
            'payload':'payload'}
        self.node = {'apiVersion':'longhorn.io/v1beta2', 'kind':'Node',
            'metadata':{'name':'longhorn-node-212','uid':'node-uid','resourceVersion':'7'},
            'spec':{'disks':{'main':copy.deepcopy(self.main)}}, 'status':{'diskStatus':{}}}
        def get(kind, name='', namespace=''):
            if kind=='nodes.longhorn.io':return copy.deepcopy(self.node)
            return {'items':[]} if not name else None
        self.lab.get.side_effect=get

    def test_plan_selects_two_unused_stable_disks_from_complete_qualification(self):
        raw='\n'.join('192.168.122.212|/dev/vd'+str(i)+'|/dev/disk/by-path/pci-'+suffix+'|||path:pci-'+suffix+'|107374182400'
                      for i,suffix in enumerate(('main','extra0','extra1')))
        result=MODULE.plan(self.lab,raw)
        self.lab.disk_plan.assert_called_once_with(raw,self.run_id)
        self.assertEqual(['/dev/disk/by-path/pci-extra0','/dev/disk/by-path/pci-extra1'],[r['device'] for r in result])
        self.assertEqual(2,len({r['uuid'] for r in result}));self.assertTrue(all(r['path']==MODULE.EXTRA for r in result))
        for defect in ('alias','unstable','small'):
            changed=raw
            if defect=='alias':changed=raw.replace('pci-extra1','pci-extra0')
            if defect=='unstable':changed=raw.replace('/dev/disk/by-path/pci-extra1','/dev/vdd')
            if defect=='small':changed=raw.replace('107374182400','1024')
            with self.subTest(defect=defect),self.assertRaises(ValueError):MODULE.plan(self.lab,changed)

    def test_full_qualification_failure_blocks_selection(self):
        self.lab.disk_plan.side_effect=ValueError('missing 18 disks')
        with self.assertRaisesRegex(ValueError,'18 disks'):MODULE.plan(self.lab,'incomplete')
        self.lab.remote.assert_not_called()

    def test_native_patch_binds_uid_resourceversion_and_complete_original_spec(self):
        desired=copy.deepcopy(self.node['spec']);desired['disks']['extra']={'path':MODULE.EXTRA}
        MODULE.patch(self.lab,self.node,desired)
        args,kwargs=self.lab.kubectl.call_args;body=json.loads(kwargs['stdin'])
        self.assertEqual('node.longhorn.io',args[3]);self.assertNotIn('--dry-run=server',args)
        self.assertEqual(['/metadata/uid','/metadata/resourceVersion','/spec','/spec'],[p['path'] for p in body])
        self.assertEqual(self.node['spec'],body[2]['value']);self.assertEqual(desired,body[3]['value'])
        self.assertTrue(kwargs['check'])

    def test_same_name_node_replacement_blocks_native_mutation(self):
        self.node['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'identity changed'):self.scenario.flags('main',False,True)
        self.lab.kubectl.assert_not_called()

    def test_main_restoration_cancels_eviction_before_enabling_scheduling(self):
        self.node['spec']['disks']['main'].update(allowScheduling=False,evictionRequested=True)
        calls=[]
        def flags(name,allow,evict):
            calls.append((name,allow,evict));self.node['spec']['disks'][name].update(allowScheduling=allow,evictionRequested=evict)
        self.scenario.flags=flags
        self.scenario.restore_main()
        self.assertEqual([('main',False,False),('main',True,False)],calls)

    def test_main_identity_change_blocks_restoration(self):
        self.node['spec']['disks']['main']['path']='/foreign'
        with self.assertRaises(ValueError):self.scenario.restore_main()
        self.lab.kubectl.assert_not_called()

    def test_extra_status_requires_exact_declaration_and_stable_native_uuid(self):
        disk=self.disks[0];self.node['spec']['disks'][disk['name']]=self.scenario.declaration(disk)
        status={'diskUUID':disk['disk_uuid'],'conditions':[{'type':'Ready','status':'True'}]}
        self.node['status']['diskStatus'][disk['name']]=status
        self.assertEqual(status,self.scenario.status(disk))
        self.node['status']['diskStatus'][disk['name']]['diskUUID']='replacement'
        with self.assertRaisesRegex(ValueError,'UUID changed'):self.scenario.status(disk)

    def test_duplicate_requires_actual_native_reason_and_no_scheduled_bytes(self):
        disk=self.disks[0];self.scenario.disks=Mock();self.scenario.remove_duplicate=Mock()
        name='kubeauto-extra26-duplicate'
        declaration=self.scenario.declaration(disk);declaration.update(path=MODULE.EXTRA+'/duplicate',allowScheduling=False)
        self.node['spec']['disks'][name]=declaration
        ready={'type':'Ready','status':'False','reason':'DiskFilesystemChanged','message':'disk has same file system ID 7 as other disks'}
        self.node['status']['diskStatus'][name]={'conditions':[ready],'storageScheduled':0}
        with redirect_stdout(io.StringIO()):self.scenario.duplicate(disk)
        self.assertEqual(ready,self.scenario.row['duplicate']['refusal']);self.scenario.remove_duplicate.assert_called_once()
        for defect in ('reason','scheduled','timeout-message'):
            status={'conditions':[copy.deepcopy(ready)],'storageScheduled':0}
            if defect=='reason':status['conditions'][0]['reason']='DiskNotFound'
            if defect=='scheduled':status['storageScheduled']=1
            if defect=='timeout-message':status['conditions'][0]['message']='timed out'
            self.node['status']['diskStatus'][name]=status
            def timeout(label,seconds,predicate):
                self.assertIsNone(predicate());raise TimeoutError(label)
            self.lab.wait.side_effect=timeout
            with redirect_stdout(io.StringIO()),self.assertRaises(TimeoutError):self.scenario.duplicate(disk)

    def test_empty_rejects_error_deleting_and_unassigned_replica_references(self):
        disk=self.disks[0];disk.update(allow=False,evict=True)
        self.node['spec']['disks'][disk['name']]=self.scenario.declaration(disk)
        self.assertTrue(self.scenario.empty(disk))
        original_get=self.lab.get.side_effect
        for spec in ({'diskID':disk['disk_uuid']},{'nodeID':self.scenario.row['node'],'diskPath':MODULE.EXTRA}):
            self.lab.get.side_effect=lambda kind,*a,**kw: {'items':[{'spec':spec,'metadata':{'deletionTimestamp':'now'},'status':{'currentState':'error'}}]} if kind=='replicas.longhorn.io' else original_get(kind,*a,**kw)
            self.assertFalse(self.scenario.empty(disk))
        del disk['disk_uuid']
        self.assertFalse(self.scenario.empty(disk))

    def test_empty_rejects_backing_image_spec_status_and_runtime_references(self):
        disk=self.disks[0];disk.update(allow=False,evict=True)
        self.node['spec']['disks'][disk['name']]=self.scenario.declaration(disk)
        original_get=self.lab.get.side_effect
        for kind,obj in (('backingimages.longhorn.io',{'spec':{'diskFileSpecMap':{disk['disk_uuid']:{}}}}),
                ('backingimages.longhorn.io',{'status':{'diskFileStatusMap':{disk['disk_uuid']:{'state':'failed'}}}}),
                ('backingimagemanagers.longhorn.io',{'spec':{'diskUUID':disk['disk_uuid']}}),
                ('backingimagedatasources.longhorn.io',{'spec':{'diskUUID':disk['disk_uuid']}})):
            with self.subTest(kind=kind,obj=obj):
                self.lab.get.side_effect=lambda k,*a,**kw: {'items':[obj]} if k==kind else original_get(k,*a,**kw)
                self.assertFalse(self.scenario.empty(disk))

    def test_backing_image_replacement_and_unconfirmed_creation_block_cleanup(self):
        self.scenario.row['backing']={'name':'bi','uid':None}
        with self.assertRaisesRegex(ValueError,'unconfirmed'):self.scenario.remove_image()
        self.lab.remove.assert_not_called()
        self.scenario.row['backing']['uid']='original'
        self.lab.get.side_effect=lambda kind,*a,**kw: {'metadata':{'uid':'replaced'}} if kind=='backingimages.longhorn.io' else {'items':[]}
        with self.assertRaisesRegex(ValueError,'identity'):self.scenario.remove_image()
        self.lab.remove.assert_not_called()

    def test_historical_export_source_is_accepted_only_after_handoff_and_pod_removal(self):
        self.scenario.row['backing']={'name':'bi','uid':'bi-uid'}
        obj={'metadata':{'name':'bi','uid':'ds-uid','ownerReferences':[{'kind':'BackingImage','uid':'bi-uid'}]},
             'spec':{'fileTransferred':True,'sourceType':'export-from-volume','parameters':{'volume-name':'source'}}}
        self.assertTrue(self.scenario.transferred_source(obj))
        for defect in ('owner','pending','source','process'):
            changed=copy.deepcopy(obj);original_get=self.lab.get.side_effect
            if defect=='owner':changed['metadata']['ownerReferences'][0]['uid']='foreign'
            if defect=='pending':changed['spec']['fileTransferred']=False
            if defect=='source':changed['spec']['parameters']['volume-name']='foreign'
            if defect=='process':self.lab.get.side_effect=lambda k,*a,**kw: {'items':[{'metadata':{'ownerReferences':[{'uid':'ds-uid'}]}}]} if k=='pods' else original_get(k,*a,**kw)
            with self.subTest(defect=defect):self.assertFalse(self.scenario.transferred_source(changed))
            self.lab.get.side_effect=original_get

    def test_foreign_backing_image_volume_consumer_blocks_delete(self):
        self.scenario.row['backing']={'name':'bi','uid':'original'}
        self.lab.get.side_effect=lambda kind,*a,**kw: {'metadata':{'uid':'original'}} if kind=='backingimages.longhorn.io' else {'items':[{'spec':{'backingImage':'bi'}}]}
        with self.assertRaisesRegex(ValueError,'foreign'):self.scenario.remove_image()
        self.lab.remove.assert_not_called()

    def test_cleanup_validates_both_disk_journals_before_any_mutation(self):
        self.disks[1]['uuid']='foreign'
        for name in ('restore_main','remove_duplicate','remove_image','retire_disk'):
            setattr(self.scenario,name,Mock())
        with self.assertRaisesRegex(ValueError,'foreign'):self.scenario.clean()
        self.scenario.restore_main.assert_not_called();self.workload.cleanup.assert_not_called()
        self.lab.remote.assert_not_called()

    def test_cleanup_requires_real_non_symlink_owned_journal(self):
        self.scenario.row=None
        with self.assertRaisesRegex(ValueError,'missing real'):self.scenario.clean()
        self.scenario.path.symlink_to('missing')
        with self.assertRaisesRegex(ValueError,'missing real'):self.scenario.clean()
        self.lab.kubectl.assert_not_called()

    def test_blank_device_refuses_signatures_children_or_pci_identity_changes(self):
        row=self.disks[0]
        probe={'path':row['kernel'],'type':'disk','size':row['size'],'mountpoints':[]}
        for defect in ('signature','pci','mount'):
            candidate=copy.deepcopy(probe)
            if defect=='mount':candidate['mountpoints']=['/foreign']
            def remote(node,args,**kw):
                if args[0]=='lsblk':return SimpleNamespace(stdout=json.dumps({'blockdevices':[candidate]}))
                if args[0]=='udevadm':return SimpleNamespace(stdout='ID_PATH='+('foreign' if defect=='pci' else row['identity'][5:]))
                return SimpleNamespace(stdout=json.dumps({'signatures':[{'type':'gpt'}] if defect=='signature' else []}))
            self.lab.remote.side_effect=remote
            with self.subTest(defect=defect),self.assertRaises(ValueError):MODULE.check_device(self.lab,row,blank=True)

    def test_host_fixture_compiles_and_never_force_unmounts_or_restarts_kubelet(self):
        source=MODULE.host_script();compile(source,'disk26-host','exec')
        self.assertIn("run('umount',str(p))",source);self.assertNotIn("'umount','-",source)
        self.assertIn('RequiresMountsFor=',source);self.assertIn('ExecStartPre=',source)
        self.assertNotIn('restart',source);self.assertNotIn('rmtree',source)
        self.assertNotIn('longhorn-disk.json',source)

    def test_retirement_refuses_same_name_foreign_path_before_host_mutation(self):
        disk=self.disks[0];declaration=self.scenario.declaration(disk);declaration['path']='/foreign'
        self.node['spec']['disks'][disk['name']]=declaration
        with self.assertRaisesRegex(ValueError,'replaced'):self.scenario.retire_disk(disk)
        self.lab.remote.assert_not_called();self.lab.kubectl.assert_not_called()

    def test_business_requires_true_replica_uid_movement_before_native_backing_eviction(self):
        order=[];allowlist=self.lab.DIRECTORY/'allowlist';allowlist.write_text('qualified')
        @contextmanager
        def scope(cleanup):
            try:yield
            finally:cleanup()
        self.lab.scoped_cleanup=scope
        before={'old':{'uid':'r1','node':'longhorn-node-212','disk':'main-native'},
                'peer1':{'uid':'r2','node':'longhorn-node-165','disk':'peer1'},
                'peer2':{'uid':'r3','node':'longhorn-node-238','disk':'peer2'}}
        migrated={k:v for k,v in before.items() if k!='old'}
        migrated['new']={'uid':'r4','node':'longhorn-node-212','disk':'native-extra0'}
        returned={k:v for k,v in migrated.items() if k!='new'}
        returned['back']={'uid':'r5','node':'longhorn-node-212','disk':'main-native'}
        self.scenario.ready_placement=Mock(side_effect=[before,migrated,returned])
        self.workload.prepare.side_effect=lambda:order.append('source')
        self.workload.data.side_effect=lambda *a,**kw:order.append('read' if kw.get('read') else 'write')
        self.scenario.prepare_disk=Mock(side_effect=lambda d:order.append('add-'+d['name']))
        self.scenario.duplicate=Mock(side_effect=lambda d:order.append('duplicate-refusal'))
        self.scenario.flags=Mock(side_effect=lambda name,*a:order.append('evict-'+name))
        self.scenario.restore_main=Mock(side_effect=lambda:order.append('restore-main'))
        def backing(disk):
            order.append('backing-ready');self.scenario.row['backing']={'ready':{'checksum':'f'*128,'size':2097152}}
        self.scenario.backing=backing
        self.scenario.image=Mock(return_value={'status':{'checksum':'f'*128,'size':2097152,
                                                       'diskFileStatusMap':{'other':{'state':'ready'}}}})
        self.scenario.empty=Mock(return_value=True)
        self.scenario.remove_image=Mock(side_effect=lambda:order.append('image-retired'))
        self.scenario.retire_disk=Mock(side_effect=lambda d:order.append('retire-'+d['name']))
        self.workload.detach.side_effect=lambda:order.append('detach')
        self.workload.mount.side_effect=lambda *a:order.append('cross-node-remount')
        self.scenario.clean=Mock(side_effect=lambda:order.append('scoped-clean'))
        with patch.object(MODULE,'plan',return_value=self.disks),patch.object(MODULE,'check_device'),redirect_stdout(io.StringIO()) as output:
            self.scenario.business(allowlist)
        self.assertLess(order.index('source'),order.index('add-kubeauto-extra26-0'))
        self.assertLess(order.index('duplicate-refusal'),order.index('evict-main'))
        self.assertLess(order.index('restore-main'),order.index('backing-ready'))
        self.assertLess(order.index('image-retired'),order.index('retire-kubeauto-extra26-0'))
        self.assertLess(order.index('retire-kubeauto-extra26-0'),order.index('add-kubeauto-extra26-1'))
        self.assertEqual('scoped-clean',order[-1]);self.assertIn('LONGHORN_DISK_LIFECYCLE_PASS',output.getvalue())
        self.assertEqual(migrated,self.scenario.row['on_extra'])
        self.scenario.empty.assert_called_once_with(self.disks[0],transferred_source=True)


if __name__=='__main__':unittest.main()
