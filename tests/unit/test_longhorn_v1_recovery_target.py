"""Single-site V1 fixture preserves the already owned independent raw/V2 disk."""
import base64
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import yaml

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('recovery_target_contract',ROOT/'tests/helpers/longhorn-v1-recovery-target.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)


class RecoveryTarget(unittest.TestCase):
    def setUp(self):
        self.fixture()

    def fixture(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)
        (self.path/'yml/longhorn').mkdir(parents=True);(self.path/'yml/longhorn/block-disks.json').write_text('original-raw')
        self.config={'longhorn_data_engines':['v2'],'longhorn_storage_nodes':[], 'longhorn_requests':[],
            'longhorn_block_disks':{MODULE.HOST:[{'path':'/dev/disk/by-id/verified'}]},
            'longhorn_storage_classes':[{'name':'longhorn','data_engine':'v2','replicas':1}]}
        self.original=yaml.safe_dump(self.config).encode();(self.path/'config.yml').write_bytes(self.original)
        self.identity={'run_id':'run','cluster_uid':'core','namespace_uid':'namespace','node_uids':{MODULE.NAME:'node'},
            'credentials':{'key':'original'},'crd_uids':{'volumes':'crd'},'disks':{'original':'retained'}}
        self.state={**self.identity,'raw_disks':{MODULE.HOST:{'machine_id':'machine'}}}
        self.raw={'diskType':'block','path':'/dev/disk/by-id/verified'}
        self.disk={'diskType':'filesystem','path':'/data/kubeauto-longhorn-coexist46','allowScheduling':True}
        self.node={'metadata':{'name':MODULE.NAME,'uid':'native-node','resourceVersion':'5'},
            'spec':{'disks':{'raw':copy.deepcopy(self.raw),'kubeauto-data':copy.deepcopy(self.disk)}},
            'status':{'diskStatus':{'raw':{'diskUUID':'original-raw'},'kubeauto-data':{'diskUUID':'new-filesystem'}}}}
        self.storage={'metadata':{'uid':'class','labels':{'kubeauto.io/component':'longhorn'}},
            'parameters':{'dataEngine':'v1','numberOfReplicas':'1'}}
        self.events=[];self.volumes=[]
        def get(kind,name='',namespace=''):
            if kind=='nodes.longhorn.io':return self.node if name else {'items':[self.node]}
            if kind=='storageclass':return self.storage
            if kind=='settings.longhorn.io':return {'value':'true'}
            if kind=='instancemanagers.longhorn.io':return {'items':[{'metadata':{'name':engine,'uid':engine},'spec':{'dataEngine':engine,'nodeID':MODULE.NAME},'status':{'currentState':'running'}} for engine in ('v1','v2')]}
            if kind=='volumes.longhorn.io':return {'items':self.volumes}
            return {'items':[]}
        def native_patch(*args,**kwargs):
            for operation in json.loads(kwargs['stdin']):
                keys=operation['path'].strip('/').split('/');parent=self.node
                for key in keys[:-1]:parent=parent[key]
                if operation['op']=='test':self.assertEqual(parent[keys[-1]],operation['value'])
                elif operation['op']=='replace':
                    self.assertEqual(operation['path'],'/spec/disks/kubeauto-data/allowScheduling')
                    self.events.append('native-disable');parent[keys[-1]]=operation['value']
                elif operation['op']=='remove':
                    if parent[keys[-1]]['allowScheduling']:
                        raise ValueError('Please disable the disk and remove all replicas and backing images first')
                    self.events.append('native-remove');parent.pop(keys[-1]);self.node['status']['diskStatus'].pop('kubeauto-data')
                else:raise AssertionError(operation)
            self.node['metadata']['resourceVersion']=str(int(self.node['metadata']['resourceVersion'])+1)
        def remove(obj):self.events.append('class-remove');self.storage=None
        self.lab=SimpleNamespace(DIRECTORY=self.path,NODES=(MODULE.HOST,),owner=Mock(return_value=self.state),
            core_identity=Mock(),installation_health=Mock(),get=Mock(side_effect=get),kubectl=Mock(side_effect=native_patch),remove=Mock(side_effect=remove),
            write_private=lambda p,r:p.write_text(json.dumps(r)),supervise_core=Mock(side_effect=lambda *a:self.events.append('product')),
            remote=Mock(side_effect=lambda node,argv,**k:self.events.append('daemon-reload' if argv==['systemctl','daemon-reload'] else 'host-release')),wait=Mock(side_effect=lambda label,timeout,check:check()),
            load_helper=Mock(return_value=SimpleNamespace(require_owned=Mock())))
        self.install=SimpleNamespace(health=Mock())
        self.target=MODULE.Target(self.lab,self.install)
        self.target.upgrade=SimpleNamespace(retained_identity=Mock(return_value=self.identity))
        self.target.host=Mock(side_effect=lambda row,action:self.events.append('host-'+action) or {'disk_uuid':'new-filesystem'})
        self.row={'identity':copy.deepcopy(self.identity),'raw_disks':copy.deepcopy(self.state['raw_disks']),
            'raw_receipt_sha256':hashlib.sha256(b'original-raw').hexdigest(),
            'host':{'machine_id':'machine'},'original_b64':base64.b64encode(self.original).decode(),
            'original_sha256':hashlib.sha256(self.original).hexdigest(),'native_disks':{MODULE.NAME:{'uid':'native-node','disks':{'raw':copy.deepcopy(self.raw)}}},
            'v1_disk':{'node_uid':'native-node','spec':copy.deepcopy(self.disk),'uuid':'new-filesystem'},'class_uid':'class','stage':'ready'}
        content=yaml.safe_dump(MODULE.desired(self.original)).encode();(self.path/'config.yml').write_bytes(content)
        self.row['applied_sha256']=hashlib.sha256(content).hexdigest();self.target.save(self.row)

    def test_host_function_is_exact_frozen_shared_implementation(self):
        self.assertEqual(MODULE.HOST_FUNCTION_SHA256,hashlib.sha256(inspect.getsource(self.target.coexist.host_filesystem).encode()).hexdigest())

    def test_desired_is_explicit_single_node_v1_beside_original_raw_v2(self):
        config=MODULE.desired(self.original)
        self.assertEqual(['v1','v2'],config['longhorn_data_engines']);self.assertTrue(config['longhorn_allow_engine_coexistence'])
        self.assertEqual([MODULE.HOST],config['longhorn_storage_nodes'])
        self.assertEqual(self.config['longhorn_block_disks'],config['longhorn_block_disks'])
        self.assertEqual('v1',config['longhorn_storage_classes'][-1]['data_engine']);self.assertEqual(1,config['longhorn_storage_classes'][-1]['replicas'])
        self.assertEqual(self.config['longhorn_storage_classes'][0],config['longhorn_storage_classes'][0])

    def test_active_request_or_existing_v1_configuration_is_not_adopted(self):
        for defect in ('request','engine','host','class'):
            config=copy.deepcopy(self.config)
            if defect=='request':config['longhorn_requests']=[{'kind':'system-restore'}]
            elif defect=='engine':config['longhorn_data_engines']=['v1','v2']
            elif defect=='host':config['longhorn_block_disks']={}
            else:config['longhorn_storage_classes'].append({'name':MODULE.STORAGE})
            with self.subTest(defect=defect),self.assertRaises(ValueError):MODULE.desired(yaml.safe_dump(config))

    def test_raw_receipt_or_core_drift_prevents_any_recovery_mutation(self):
        (self.path/'yml/longhorn/block-disks.json').write_text('foreign')
        with self.assertRaisesRegex(ValueError,'raw receipt'):self.target.clean(12)
        self.assertFalse(self.events);self.lab.kubectl.assert_not_called();self.lab.supervise_core.assert_not_called()

    def test_capture_requires_exact_native_and_filesystem_uuid_and_original_raw_spec(self):
        self.target.capture(self.row)
        self.node['spec']['disks']['raw']['path']='/dev/foreign'
        with self.assertRaisesRegex(ValueError,'original raw declaration'):self.target.capture(self.row)
        self.lab.kubectl.assert_not_called()

    def test_partial_product_disk_can_be_retired_without_assuming_class_was_created(self):
        self.row.pop('v1_disk');self.row.pop('class_uid');self.target.save(self.row);self.storage=None
        self.target.clean(12)
        self.assertIn('native-remove',self.events);self.assertIn('host-remove',self.events)
        self.assertFalse(self.target.path.exists())

    def test_ready_accepts_native_manager_recreation_but_preserves_credentials_and_raw(self):
        result=self.target.ready();self.assertEqual({'v1','v2'},{r['engine'] for r in result['managers'].values()})
        self.identity['credentials']['key']='foreign'
        with self.assertRaisesRegex(ValueError,'shared identity'):self.target.ready()

    def test_product_input_drift_never_overwrites_external_change(self):
        (self.path/'config.yml').write_text('foreign')
        with self.assertRaisesRegex(ValueError,'input changed'):self.target.apply(self.row,self.original,12)
        self.assertEqual('foreign',(self.path/'config.yml').read_text());self.lab.supervise_core.assert_not_called()

    def test_data_reference_blocks_first_cleanup_mutation(self):
        self.volumes.append({'metadata':{'uid':'foreign-volume'}})
        with self.assertRaisesRegex(ValueError,'native data consumers'):self.target.clean(12)
        self.assertFalse(self.events);self.lab.kubectl.assert_not_called();self.target.host.assert_not_called()

    def test_replaced_filesystem_uuid_cannot_be_deleted_or_released(self):
        self.node['status']['diskStatus']['kubeauto-data']['diskUUID']='foreign'
        with self.assertRaisesRegex(ValueError,'disk changed'):self.target.clean(12)
        self.assertFalse(self.events);self.lab.kubectl.assert_not_called();self.lab.remote.assert_not_called()

    def test_cleanup_retires_native_before_product_guard_host_and_preserves_raw(self):
        self.target.clean(12)
        self.assertLess(self.events.index('native-disable'),self.events.index('native-remove'))
        self.assertLess(self.events.index('native-remove'),self.events.index('product'))
        self.assertLess(self.events.index('product'),self.events.index('host-release'))
        self.assertLess(self.events.index('host-release'),self.events.index('host-remove'))
        self.assertLess(self.events.index('host-remove'),self.events.index('daemon-reload'))
        self.assertEqual(self.original,(self.path/'config.yml').read_bytes());self.assertFalse(self.target.path.exists())
        self.assertEqual({'raw':self.raw},self.node['spec']['disks']);self.install.health.assert_called_once_with(self.lab)

    def test_reload_failure_retains_recovery_journal_and_cannot_report_cleanup(self):
        def failed(node, argv, **kwargs):
            if argv == ['systemctl', 'daemon-reload']:
                raise OSError('reload failed')
        self.lab.remote.side_effect = failed
        with self.assertRaisesRegex(OSError, 'reload failed'): self.target.clean(12)
        self.assertTrue(self.target.path.exists()); self.install.health.assert_not_called()

    def test_cleanup_resumes_after_disable_without_changing_original_disk_receipt(self):
        self.node['spec']['disks']['kubeauto-data']['allowScheduling']=False
        self.target.clean(12)
        self.assertNotIn('native-disable',self.events);self.assertIn('native-remove',self.events)
        self.assertTrue(self.row['v1_disk']['spec']['allowScheduling'])

    def test_disable_failure_preserves_host_and_journal_for_exact_reentry(self):
        original=self.lab.kubectl.side_effect
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            if self.events[-1]=='native-disable':raise OSError('interruption after disable')
        self.lab.kubectl.side_effect=interrupted
        with self.assertRaises(OSError):self.target.clean(12)
        self.assertTrue(self.target.path.exists());self.assertEqual(['native-disable'],self.events)
        self.lab.remote.assert_not_called();self.lab.supervise_core.assert_not_called()
        self.lab.kubectl.side_effect=original;self.target.clean(12)
        self.assertFalse(self.target.path.exists());self.assertEqual(1,self.events.count('native-disable'))

    def test_changed_uuid_or_data_after_disable_stops_before_disk_or_host_removal(self):
        for defect in ('uuid','scheduled','replica','node','path'):
            with self.subTest(defect=defect):
                self.fixture()
                original=self.lab.kubectl.side_effect
                def changed(*args,**kwargs):
                    original(*args,**kwargs)
                    if self.events[-1]!='native-disable':return
                    status=self.node['status']['diskStatus']['kubeauto-data']
                    if defect=='uuid':status['diskUUID']='foreign'
                    elif defect=='scheduled':status['scheduledBackingImage']={'foreign':1}
                    elif defect=='replica':self.volumes.append({'spec':{'diskID':'new-filesystem'}})
                    elif defect=='node':self.node['metadata']['uid']='foreign'
                    else:self.node['spec']['disks']['kubeauto-data']['path']='/foreign'
                self.lab.kubectl.side_effect=changed
                if defect=='replica':
                    get=self.lab.get.side_effect
                    self.lab.get.side_effect=lambda kind,*a,**kw:{'items':self.volumes} if kind=='replicas.longhorn.io' else get(kind,*a,**kw)
                with self.assertRaises(ValueError):self.target.clean(12)
                self.assertEqual(['native-disable'],self.events);self.assertTrue(self.target.path.exists())
                self.lab.remote.assert_not_called();self.lab.supervise_core.assert_not_called()


if __name__=='__main__':unittest.main()
