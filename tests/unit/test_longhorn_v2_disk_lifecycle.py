"""Receiving-host identity, precise raw retirement and durable runner contracts."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('disk45_contract', ROOT/'tests/helpers/longhorn-v2-disk-lifecycle.py')
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)
RUN = str(uuid.UUID(int=1))


class DiskContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.log = self.directory/'release.log'; self.log.write_text(M.RELEASE_MARKER+' host='+M.HOST+'\n')
        self.proof = {'status':'pass', 'signed_scope':'v2-target-host-release', 'host':M.HOST,
            'durable':{'exit':0,'finalized':0}, 'cleanup':{'host_clean_verified':True,
                'raw_disk_clean_verified':True,'page_pool_restored':True}, 'markers':[M.RELEASE_MARKER],
            'owner_run_id':RUN, 'target_cluster_uid':RUN, 'released_at':'2026-10-21T12:00:00+08:00',
            'original_raw_disk':{'machine_id':'a'*32,'identity':'wwid','path':'/dev/disk/by-id/wwn-disk',
                                 'size_bytes':100*1024**3},
            'raw_logs':[{'path':str(self.log),'sha256':hashlib.sha256(self.log.read_bytes()).hexdigest()}]}
        self.proof_path = self.directory/'proof.json'
        self.collected = {'host':M.HOST, 'inventory':{'machine_id':'a'*32,'hugepages_2mi':'0'},
            'assessment':{'blockers':[],'candidates':[{'identity':'wwid','size_bytes':100*1024**3,
                                                     'by_id':['/dev/disk/by-id/wwn-disk']}]}}

    def validate(self):
        self.proof_path.write_text(json.dumps(self.proof)); return M.release_proof(self.proof_path)

    def test_target_release_needs_durable_success_exact_scope_and_hashed_marker(self):
        self.assertEqual(self.proof,self.validate())
        original = copy.deepcopy(self.proof)
        for field,value in (('status','pending'),('signed_scope','v2-source-host-release'),('host',M.SOURCE[0]),
                            ('durable',{'exit':0,'finalized':1}),('markers',[])):
            self.proof = copy.deepcopy(original); self.proof[field] = value
            with self.subTest(field=field),self.assertRaises(ValueError): self.validate()

    def test_release_log_cannot_be_missing_symlink_changed_or_failure(self):
        self.validate(); self.log.write_text('changed')
        with self.assertRaisesRegex(ValueError,'identity changed'): M.release_proof(self.proof_path)
        self.log.write_text(M.RELEASE_MARKER+' host='+M.HOST+'\nLONGHORN_FIRST_FAILURE rc=1\n')
        self.proof['raw_logs'][0]['sha256'] = hashlib.sha256(self.log.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'failed raw log'): self.validate()
        alias = self.directory/'alias'; alias.symlink_to(self.log); self.proof['raw_logs'][0]['path'] = str(alias)
        with self.assertRaisesRegex(ValueError,'identity changed'): self.validate()

    def test_marker_must_name_exact_receiving_host(self):
        for line in (M.RELEASE_MARKER,M.RELEASE_MARKER+' host='+M.SOURCE[0],M.RELEASE_MARKER+' host='+M.HOST+'0'):
            self.log.write_text(line+'\n'); self.proof['raw_logs'][0]['sha256'] = hashlib.sha256(self.log.read_bytes()).hexdigest()
            with self.subTest(line=line),self.assertRaisesRegex(ValueError,'marker absent'): self.validate()

    def test_release_identity_requires_stable_whole_disk_and_timezone(self):
        original = copy.deepcopy(self.proof)
        for key,value in (('path','/dev/sdb'),('identity',''),('machine_id','unknown'),('size_bytes',1024)):
            self.proof = copy.deepcopy(original); self.proof['original_raw_disk'][key] = value
            with self.subTest(key=key),self.assertRaises(ValueError): self.validate()
        self.proof = original; self.proof['released_at'] = '2026-10-21T12:00:00'
        with self.assertRaisesRegex(ValueError,'timezone'): self.validate()

    def test_receiving_admission_rechecks_real_disk_and_empty_pool(self):
        self.assertEqual(self.proof['original_raw_disk'],M.disk_identity(self.proof,self.collected))
        original = copy.deepcopy(self.collected)
        for defect in ('host','blocker','count','machine','pages','identity','size','alias'):
            actual = copy.deepcopy(original)
            if defect == 'host': actual['host'] = M.SOURCE[0]
            elif defect == 'blocker': actual['assessment']['blockers'] = ['in-use']
            elif defect == 'count': actual['assessment']['candidates'] *= 2
            elif defect == 'machine': actual['inventory']['machine_id'] = 'b'*32
            elif defect == 'pages': actual['inventory']['hugepages_2mi'] = '1024'
            else:
                item = actual['assessment']['candidates'][0]
                item[{'identity':'identity','size':'size_bytes','alias':'by_id'}[defect]] = {'identity':'other','size':1,'alias':[]}[defect]
            with self.subTest(defect=defect),self.assertRaises(ValueError): M.disk_identity(self.proof,actual)

    def test_inventory_adds_only_one_worker_and_preserves_control_plane(self):
        original = '[etcd]\na\n[kube_master]\na\n[kube_node]\na\nb\nc\n[all:vars]\nx=1\n'
        expanded = M.extended_inventory(original)
        self.assertEqual(original.replace('[all:vars]',M.HOST+" k8s_nodename='"+M.NAME+"'\n[all:vars]"),expanded)
        for invalid in (expanded,'[etcd]\na\n',original.replace('[all:vars]','[kube_node]\n[all:vars]')):
            with self.assertRaises(ValueError): M.extended_inventory(invalid)

    def test_post_upgrade_consumer_uses_original_three_node_fixture_and_single_validator(self):
        source = SimpleNamespace(NODES=M.SOURCE)
        install = SimpleNamespace(fixture=Mock(return_value=(None,source)))
        validator = SimpleNamespace(accepted=Mock(return_value={'scope':'accepted-after-52'}))
        def load(name):
            return {'longhorn-v2-install.py':install,'longhorn-v2-upgrade-baseline.py':validator}[name]
        with patch.object(M,'load',side_effect=load):self.assertEqual({'scope':'accepted-after-52'},M.accepted_source())
        validator.accepted.assert_called_once_with(source)
        validator.accepted.side_effect = ValueError('52 transition has no durable acceptance')
        with patch.object(M,'load',side_effect=load),self.assertRaisesRegex(ValueError,'no durable acceptance'):M.accepted_source()

    def scenario(self):
        lab = SimpleNamespace(DIRECTORY=self.directory,STATE=self.directory/'owner',BASE=ROOT)
        lab.wait = lambda label,timeout,fn:fn()
        lab.STATE.write_text(json.dumps({'run_id':RUN,'cluster_uid':RUN,'nodes':M.SOURCE}))
        lab.write_private = lambda p,v:p.write_text(json.dumps(v))
        row = {'run_id':RUN,'cluster_uid':RUN,'host':M.HOST,'allowed_inputs':['original'],
            'raw_disks':{M.HOST:self.proof['original_raw_disk']},'native_uids':{M.HOST:'native'},
            'disk_uuids':{M.HOST:'disk-uuid'},'retirements':{}}
        (self.directory/M.RECORD).write_text(json.dumps(row)); (self.directory/'config.yml').write_text('original')
        return M.Lifecycle(lab,42),lab,row

    def test_config_write_is_journaled_before_product_and_rejects_foreign_edits(self):
        scenario,lab,row = self.scenario(); events = []
        writer = SimpleNamespace(atomic_config=lambda l,c:events.append(('write',c)))
        with patch.object(M,'load',return_value=writer),patch.object(M,'supervise',side_effect=lambda *a:events.append(('product',a))):
            scenario.config({'longhorn_action':'present'})
        self.assertIn(yaml.safe_dump({'longhorn_action':'present'},sort_keys=False),scenario.row()['allowed_inputs'])
        self.assertEqual(['write','product'],[e[0] for e in events])
        (self.directory/'config.yml').write_text('foreign')
        with patch.object(M,'supervise') as product,self.assertRaisesRegex(ValueError,'foreign'): scenario.config({})
        product.assert_not_called()

    def join_scenario(self, partial=False):
        scenario, lab, row = self.scenario()
        inventory = '[kube_node]\n192.168.47.146\n\n[kube_master]\n192.168.47.146\n'
        row.update(phase='joining' if partial else 'admitted', inventory=inventory,
                   multipath={'phase':'planned', 'original':{'boot_id':'original-boot'},
                              'current':{'boot_id':'original-boot'}})
        row['native_uids'].pop(M.HOST)
        scenario.save(row); (self.directory/'hosts').write_text(inventory)
        node = {'metadata':{'uid':'receiving'}, 'status':{'nodeInfo':{
            'machineID':'a'*32, 'bootID':'original-boot'},
            'addresses':[{'type':'InternalIP','address':M.HOST}]}}
        objects = {'node':node if partial else None, 'native':None}
        def get(kind, name=None, namespace=None):
            if kind == 'node': return objects['node']
            if kind == 'nodes.longhorn.io': return objects['native']
            return {'items':[]}
        lab.get = Mock(side_effect=get); lab.core_identity = Mock()
        scenario.recover_join_network = Mock()
        events = []
        def multipath(*args, **kwargs):
            self.assertEqual('stop',args[2]); events.append('stop')
            return {'boot_id':'original-boot','stopped':True}
        install = SimpleNamespace(multipath=Mock(side_effect=multipath))
        def product(*args):
            self.assertEqual('stopped',scenario.row()['multipath']['phase'])
            self.assertEqual('joining',scenario.row()['phase'])
            if partial: self.assertEqual('receiving',scenario.row()['node_uid'])
            events.append('add-node'); objects['node'] = node
            objects['native'] = {'metadata':{'uid':'native'},'spec':{'disks':{}}}
            (self.directory/'hosts').write_text(M.extended_inventory(inventory))
        return scenario, lab, row, node, objects, install, product, events

    def test_multipath_is_stopped_before_product_add_node_embedded_longhorn(self):
        scenario,lab,row,node,objects,install,product,events = self.join_scenario()
        with patch.object(M,'load',return_value=install),patch.object(M,'supervise',side_effect=product): scenario.join()
        self.assertEqual(['stop','add-node'],events)
        self.assertEqual('joined',scenario.row()['phase']); self.assertEqual('receiving',scenario.row()['node_uid'])
        lab.core_identity.assert_called_once()

    def test_partial_join_binds_machine_boot_ip_and_uid_before_product_retry(self):
        scenario,lab,row,node,objects,install,product,events = self.join_scenario(partial=True)
        with patch.object(M,'load',return_value=install),patch.object(M,'supervise',side_effect=product): scenario.join()
        self.assertEqual(['stop','add-node'],events); self.assertEqual('joined',scenario.row()['phase'])
        scenario.recover_join_network.assert_called_once_with()

    def network_scenario(self):
        scenario, lab, row, node, objects, install, product, events = self.join_scenario(partial=True)
        scenario.recover_join_network = M.Lifecycle.recover_join_network.__get__(scenario)
        row['node_uid'] = 'receiving'; scenario.save(row)
        def pod(name, uid, host=M.NAME, app=None, owner='longhorn-csi-plugin'):
            return {'metadata':{'name':name,'uid':uid,'namespace':M.NS,'resourceVersion':'7',
                'labels':{'app':app or owner}, 'ownerReferences':[{'kind':'DaemonSet','name':owner,
                'uid':owner+'-uid','controller':True}]},'spec':{'nodeName':host},
                'status':{'podIP':'172.31.0.3','conditions':[{'type':'Ready','status':'True'}]}}
        old = pod('csi-old','old'); new = pod('csi-new','new')
        source = [pod(app+address,address+app,'longhorn-node-'+address.rsplit('.',1)[1],app)
                  for address in M.SOURCE for app in ('longhorn-manager','longhorn-csi-plugin')]
        calico = pod('calico','calico'); calico['metadata']['labels'] = {'k8s-app':'calico-node'}
        api = {'pods':source+[old], 'calico':[calico], 'old':old, 'ds':{'metadata':{'uid':'longhorn-csi-plugin-uid'}}}
        def get(kind, name=None, namespace=None):
            if kind == 'pods': return {'items':api['calico'] if namespace=='kube-system' else api['pods']}
            if kind == 'pod': return api['old']
            if kind == 'daemonset': return api['ds']
            raise AssertionError(kind)
        lab.get = Mock(side_effect=get)
        allocation = {'name':'csi-old','uid':'old','namespace':M.NS,'sandbox':'sandbox','ip':'172.31.0.3'}
        probe = {'machine_id':'a'*32,'boot_id':'original-boot','cni_sha256':'f'*64,'allocations':[allocation]}
        after = dict(probe,allocations=[])
        lab.remote = Mock(side_effect=[SimpleNamespace(stdout=json.dumps(probe)),SimpleNamespace(stdout=json.dumps(after))])
        def remove(obj):
            self.assertEqual('delete-intent',scenario.row()['join_network_repairs']['old']['phase'])
            self.assertEqual(old,obj); api['old'] = None; api['pods'] = source+[new]
        lab.remove = Mock(side_effect=remove)
        return scenario,lab,api,probe,after

    def test_partial_join_network_binds_cri_allocation_then_guarded_delete_and_ready_replacement(self):
        scenario,lab,api,probe,after = self.network_scenario()
        scenario.recover_join_network()
        lab.remove.assert_called_once()
        self.assertEqual('new',scenario.row()['join_network_repairs']['old']['replacement_uid'])
        self.assertEqual('ready',scenario.row()['join_network_repairs']['old']['phase'])
        compile(M.JOIN_NETWORK_PROBE,'join-network-probe','exec')

    def test_network_recovery_refuses_foreign_host_source_calico_pod_and_controller(self):
        for defect in ('machine','boot','source','calico','namespace','node','owner','uid','ip','hostnetwork','ds'):
            scenario,lab,api,probe,after = self.network_scenario()
            old = api['old']
            if defect == 'machine': probe['machine_id'] = 'b'*32
            elif defect == 'boot': probe['boot_id'] = 'new-boot'
            elif defect == 'source': api['pods'][0]['status']['conditions'] = []
            elif defect == 'calico': api['calico'][0]['status']['conditions'] = []
            elif defect == 'namespace': probe['allocations'][0]['namespace'] = 'foreign'
            elif defect == 'node': old['spec']['nodeName'] = 'other'
            elif defect == 'owner': old['metadata']['ownerReferences'][0]['name'] = 'foreign'
            elif defect == 'uid': probe['allocations'][0]['uid'] = 'foreign'
            elif defect == 'ip': old['status']['podIP'] = 'different'
            elif defect == 'hostnetwork': old['spec']['hostNetwork'] = True
            elif defect == 'ds': api['ds']['metadata']['uid'] = 'replacement'
            lab.remote.side_effect = [SimpleNamespace(stdout=json.dumps(probe))]
            with self.subTest(defect=defect),self.assertRaises(ValueError): scenario.recover_join_network()
            lab.remove.assert_not_called()

    def test_network_recovery_cannot_claim_ready_when_old_bridge_consumers_remain_or_cni_changes(self):
        for defect in ('allocation','cni'):
            scenario,lab,api,probe,after = self.network_scenario()
            if defect == 'allocation': after['allocations'] = probe['allocations']
            else: after['cni_sha256'] = 'changed'
            lab.remote.side_effect = [SimpleNamespace(stdout=json.dumps(probe)),SimpleNamespace(stdout=json.dumps(after))]
            with self.subTest(defect=defect),self.assertRaisesRegex(ValueError,'consumers or CNI drift'):
                scenario.recover_join_network()

    def test_failed_fixture_bridge_release_requires_same_machine_boot_link_and_no_ports(self):
        for name,value in (('etc/machine-id','machine'),('proc/sys/kernel/random/boot_id','boot'),
                           ('sys/class/net/mynet0/address','mac'),('sys/class/net/mynet0/ifindex','15')):
            path = self.directory/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(value)
        bridge = self.directory/'sys/class/net/mynet0'
        (bridge/'bridge').mkdir(); (bridge/'brif').mkdir()
        expected = {'machine_id':'machine','boot_id':'boot','bridge':{'address':'mac','ifindex':'15'}}
        with patch('subprocess.run') as command:
            for defect in ('machine','boot','mac','ifindex','ports'):
                changed = copy.deepcopy(expected)
                if defect == 'machine': changed['machine_id'] = 'other'
                elif defect == 'boot': changed['boot_id'] = 'other'
                elif defect == 'mac': changed['bridge']['address'] = 'other'
                elif defect == 'ifindex': changed['bridge']['ifindex'] = '99'
                else: (bridge/'brif/foreign').touch()
                with self.subTest(defect=defect),self.assertRaises(ValueError):
                    M.release_join_bridge(changed, self.directory)
                command.assert_not_called()
                if defect == 'ports': (bridge/'brif/foreign').unlink()
            M.release_join_bridge(expected, self.directory)
            command.assert_called_once_with(['ip','link','delete','dev','mynet0','type','bridge'],check=True,timeout=30)

    def test_partial_join_refuses_replaced_machine_boot_ip_or_recorded_uid_before_mutation(self):
        for field in ('machineID','bootID','address','uid'):
            scenario,lab,row,node,objects,install,product,events = self.join_scenario(partial=True)
            if field in ('machineID','bootID'): node['status']['nodeInfo'][field] = 'replacement'
            elif field == 'address': node['status']['addresses'][0]['address'] = M.SOURCE[0]
            else: row['node_uid'] = 'different'; scenario.save(row)
            with self.subTest(field=field),patch.object(M,'load') as loader,patch.object(M,'supervise') as command:
                with self.assertRaisesRegex(ValueError,'unconfirmed/replaced'): scenario.join()
                loader.assert_not_called(); command.assert_not_called()

    def test_partial_join_refuses_native_disks_and_consumers_before_stop(self):
        for kind in ('disk','instancemanagers.longhorn.io','replicas.longhorn.io','engines.longhorn.io','enginefrontends.longhorn.io'):
            scenario,lab,row,node,objects,install,product,events = self.join_scenario(partial=True)
            original = lab.get.side_effect
            if kind == 'disk': objects['native'] = {'metadata':{'uid':'native'},'spec':{'disks':{'foreign':{}}}}
            else:
                lab.get.side_effect = lambda k,*a,**kw: {'items':[{'spec':{'nodeID':M.NAME}}]} if k==kind else original(k,*a,**kw)
            with self.subTest(kind=kind),patch.object(M,'load') as loader,patch.object(M,'supervise') as command:
                with self.assertRaisesRegex(ValueError,'foreign disks|native consumers'): scenario.join()
                loader.assert_not_called(); command.assert_not_called()

    def test_lost_join_response_reuses_committed_inventory_without_second_add(self):
        scenario,lab,row,node,objects,install,product,events = self.join_scenario(partial=True)
        (self.directory/'hosts').write_text(M.extended_inventory(row['inventory']))
        with patch.object(M,'load',return_value=install),patch.object(M,'supervise') as command: scenario.join()
        command.assert_not_called(); self.assertEqual('joined',scenario.row()['phase'])

    def test_cleanup_completes_real_partial_join_before_its_owned_object_cleanup(self):
        scenario,lab,row,node,objects,install,product,events = self.join_scenario(partial=True)
        scenario.current = lambda: {}
        def cleanup():
            self.assertEqual('joined',scenario.row()['phase'])
            self.assertEqual('receiving',scenario.row()['node_uid'])
            self.assertEqual('native',scenario.row()['native_uids'][M.HOST])
            events.append('business-cleanup')
            raise RuntimeError('stop at verified cleanup handoff')
        scenario.workload.cleanup = cleanup
        with patch.object(M,'load',return_value=install),patch.object(M,'supervise',side_effect=product):
            with self.assertRaisesRegex(RuntimeError,'verified cleanup handoff'): scenario.cleanup()
        self.assertEqual(['stop','add-node','business-cleanup'],events)

    def test_new_join_refuses_preexisting_node_and_ambiguous_multipath_write(self):
        scenario,lab,row,node,objects,install,product,events = self.join_scenario()
        objects['node'] = node
        with patch.object(M,'supervise') as command,self.assertRaisesRegex(ValueError,'unconfirmed/replaced'): scenario.join()
        command.assert_not_called()
        objects['node'] = None; row['multipath']['phase'] = 'stop-intent'; scenario.save(row)
        with patch.object(M,'load',return_value=install),patch.object(M,'supervise') as command:
            with self.assertRaisesRegex(ValueError,'ambiguous'): scenario.join()
            install.multipath.assert_not_called(); command.assert_not_called()

    def test_native_disk_checks_original_node_uid_uuid_type_driver_and_path(self):
        scenario,lab,row = self.scenario()
        native = {'metadata':{'uid':'native'},'spec':{'disks':{M.DISK:{'diskType':'block','diskDriver':'aio',
            'path':row['raw_disks'][M.HOST]['path']}}},'status':{'diskStatus':{M.DISK:{'diskUUID':'disk-uuid'}}}}
        lab.get = Mock(return_value=native); self.assertEqual(native,scenario.disk(M.HOST))
        for defect in ('node','uuid','type','driver','path'):
            changed = copy.deepcopy(native)
            if defect == 'node': changed['metadata']['uid'] = 'other'
            elif defect == 'uuid': changed['status']['diskStatus'][M.DISK]['diskUUID'] = 'other'
            else: changed['spec']['disks'][M.DISK][{'type':'diskType','driver':'diskDriver','path':'path'}[defect]] = 'other'
            lab.get.return_value = changed
            with self.subTest(defect=defect),self.assertRaises(ValueError): scenario.disk(M.HOST)
        lab.get.return_value = native
        with self.assertRaisesRegex(ValueError,'remains'): scenario.disk(M.HOST,absent=True)
        native['spec']['disks'] = {}; native['status']['diskStatus'] = {}
        scenario.disk(M.HOST,absent=True)

    def test_retirement_replay_uses_exact_operation_and_does_not_require_missing_native_disk(self):
        scenario,lab,row = self.scenario(); operation = 'retire-owned'; row['retirements'][M.HOST] = operation
        scenario.save(row)
        config = {'longhorn_block_disks':{M.HOST:[{'name':M.DISK}]}}
        scenario.current = lambda:config; scenario.config = Mock(); scenario.disk = Mock()
        target = self.directory/'yml/longhorn'; target.mkdir(parents=True)
        (target/('block-retirement-'+operation+'.json')).write_text(json.dumps({'phase':'complete','cluster_uid':RUN}))
        (target/'block-disks.json').write_text(json.dumps({'nodes':{}}))
        lab.remote = Mock(return_value=SimpleNamespace(returncode=0))
        scenario.retire(M.HOST)
        supplied = scenario.config.call_args.args[0]
        self.assertEqual(operation,supplied['longhorn_block_retirement_id'])
        self.assertEqual([{'node':M.HOST,'name':M.DISK,'disk_uuid':'disk-uuid'}],supplied['longhorn_retire_block_disks'])
        scenario.disk.assert_called_once_with(M.HOST,absent=True)
        self.assertEqual([M.HOST],scenario.row()['retired'])

    def test_incomplete_native_retirement_never_signs_or_accepts_host_receipt(self):
        scenario,lab,row = self.scenario(); row['retirements'][M.HOST] = 'retire-owned'; scenario.save(row)
        scenario.current = lambda:{}; scenario.config = Mock(); scenario.disk = Mock()
        directory = self.directory/'yml/longhorn'; directory.mkdir(parents=True)
        archive = directory/'block-retirement-retire-owned.json'
        archive.write_text(json.dumps({'phase':'native-removed','cluster_uid':RUN}))
        lab.remote = Mock()
        with self.assertRaisesRegex(ValueError,'precise native/host receipts'): scenario.retire(M.HOST)
        lab.remote.assert_not_called(); self.assertNotIn('retired',scenario.row())

    def page_retry_scenario(self):
        scenario,lab,row = self.scenario()
        current = {'machine_id':row['raw_disks'][M.HOST]['machine_id'], 'boot_id':'boot',
                   'total':748, 'free':748, 'reserved':0, 'kubelet_active':'active'}
        row.update(phase='joined', node_uid='original-node')
        row['pages'] = {'original':dict(current,total=0,free=0), 'current':current, 'phase':'allocated'}
        scenario.save(row); scenario.native = Mock(return_value={'spec':{'disks':{}}})
        lab.get = Mock(side_effect=lambda kind,*args,**kwargs: {'items':[]} if kind=='instancemanagers.longhorn.io' else
            {'metadata':{'uid':'original-node','labels':{'node.longhorn.io/disable-v2-data-engine':'true'}}})
        return scenario,lab,current

    def test_short_receiving_allocation_compacts_once_before_guarded_retry(self):
        scenario,lab,current = self.page_retry_scenario()
        def compact(host,argv,**kwargs):
            self.assertEqual(M.HOST,host); self.assertEqual('intent',scenario.row()['pages']['compaction'])
            self.assertEqual('allocated',scenario.row()['pages']['phase'])
            self.assertEqual(current,json.loads(kwargs['stdin'])); self.assertEqual(120,kwargs['timeout'])
        lab.remote = Mock(side_effect=compact)
        def probe(lab,host,**kwargs):
            self.assertEqual(M.HOST,host); self.assertEqual(current,kwargs['expected']); self.assertEqual(1024,kwargs['target'])
            self.assertEqual('write-intent',scenario.row()['pages']['phase'])
            self.assertEqual('complete',scenario.row()['pages']['compaction'])
            return dict(current,total=1024,free=1024)
        with patch.object(M,'load',return_value=SimpleNamespace(probe=probe)):scenario.retry_receiving_pages()
        self.assertEqual(1024,scenario.row()['pages']['current']['total']);lab.remote.assert_called_once()
        with self.assertRaisesRegex(ValueError,'not a fresh owned'):scenario.retry_receiving_pages()
        lab.remote.assert_called_once()

    def test_receiving_retry_stops_before_compaction_on_identity_or_consumer_drift(self):
        for drift in ('phase','boot','machine','repeated','disk','node','label','im'):
            with self.subTest(drift=drift):
                scenario,lab,current = self.page_retry_scenario();row=scenario.row();lab.remote=Mock()
                if drift=='phase':row['phase']='enabled'
                if drift=='boot':row['pages']['original']['boot_id']='different'
                if drift=='machine':row['pages']['current']['machine_id']='different'
                if drift=='repeated':row['pages']['compaction']='intent'
                if drift=='disk':scenario.native.return_value['spec']['disks']={'foreign':{}}
                if drift in ('node','label','im'):
                    lab.get.side_effect=lambda kind,*args,**kwargs: ({'items':[{'spec':{'nodeID':M.NAME,'dataEngine':'v2'}}]} if drift=='im' else {'items':[]}) if kind=='instancemanagers.longhorn.io' else {'metadata':{'uid':'replaced' if drift=='node' else 'original-node','labels':{'node.longhorn.io/disable-v2-data-engine':'false' if drift=='label' else 'true'}}}
                scenario.save(row)
                with self.assertRaises(ValueError):scenario.retry_receiving_pages()
                lab.remote.assert_not_called()

    def test_receiving_retry_keeps_partial_result_or_ambiguous_write_for_cleanup(self):
        scenario,lab,current=self.page_retry_scenario();lab.remote=Mock()
        with patch.object(M,'load',return_value=SimpleNamespace(probe=Mock(return_value=current))):scenario.retry_receiving_pages()
        self.assertEqual(748,scenario.row()['pages']['current']['total']);self.assertEqual('allocated',scenario.row()['pages']['phase'])
        scenario,lab,current=self.page_retry_scenario();lab.remote=Mock()
        with patch.object(M,'load',return_value=SimpleNamespace(probe=Mock(side_effect=TimeoutError))),self.assertRaises(TimeoutError):scenario.retry_receiving_pages()
        self.assertEqual('write-intent',scenario.row()['pages']['phase'])

    def test_compaction_checks_actual_pool_headroom_and_identity_before_single_write(self):
        host=self.directory/'host'; pages=host/'sys/kernel/mm/hugepages/hugepages-2048kB'
        files={'etc/machine-id':'machine', 'proc/sys/kernel/random/boot_id':'boot',
               'proc/meminfo':'MemAvailable: 6000000 kB\nHugePages_Rsvd: 0\n',
               'sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages':'748',
               'sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages':'748',
               'proc/sys/vm/compact_memory':'untouched'}
        for name,data in files.items():
            path=host/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
        expected={'machine_id':'machine','boot_id':'boot','total':748,'free':748,'reserved':0,'kubelet_active':'active'}
        compact=host/'proc/sys/vm/compact_memory'
        with patch('subprocess.check_output',return_value='active\n'):
            for name,data in [('etc/machine-id','different'),('proc/sys/kernel/random/boot_id','different'),
                              ('sys/kernel/mm/hugepages/hugepages-2048kB/free_hugepages','747'),
                              ('proc/meminfo','MemAvailable: 1 kB\nHugePages_Rsvd: 0\n'),
                              ('proc/meminfo','MemAvailable: 6000000 kB\nHugePages_Rsvd: 1\n')]:
                (host/name).write_text(data)
                with self.subTest(name=name,data=data),self.assertRaises(ValueError):M.compact_receiving_pages(expected,host)
                self.assertEqual('untouched',compact.read_text());(host/name).write_text(files[name])
            M.compact_receiving_pages(expected,host)
        self.assertEqual('1\n',compact.read_text());self.assertEqual('748',(pages/'nr_hugepages').read_text())

    def test_hugepage_restore_reuses_fixture_and_records_intent_before_write(self):
        scenario,lab,row = self.scenario(); current = {'machine_id':'machine','boot_id':'boot','total':1024,'free':1024,'reserved':0}
        row['pages'] = {'original':dict(current,total=0,free=0),'current':current,'phase':'allocated'}; scenario.save(row)
        phases = []
        def probe(lab,host,**kwargs):
            self.assertEqual(M.HOST,host)
            if kwargs:
                phases.append(scenario.row()['pages']['phase']); self.assertEqual(0,kwargs['target'])
                self.assertEqual(current,kwargs['expected']); return dict(current,total=0,free=0)
            return current
        with patch.object(M,'load',return_value=SimpleNamespace(probe=probe)): scenario.restore_pages()
        self.assertEqual(['restore-intent'],phases); self.assertEqual('restored',scenario.row()['pages']['phase'])

    def test_page_restore_waits_for_native_process_release_before_resize(self):
        scenario,lab,row = self.scenario(); current = {'machine_id':'machine','boot_id':'boot','total':1024,'free':1024,'reserved':0}
        row['pages'] = {'current':current,'phase':'allocated'}; scenario.save(row)
        probe = Mock(side_effect=[dict(current,free=1000),current,dict(current,total=0,free=0)])
        def wait(label,timeout,fn):
            self.assertEqual(180,timeout);self.assertIsNone(fn());return fn()
        lab.wait=wait
        with patch.object(M,'load',return_value=SimpleNamespace(probe=probe)):scenario.restore_pages()
        self.assertEqual(3,probe.call_count);self.assertEqual(0,probe.call_args.kwargs['target'])

    def test_ambiguous_or_replaced_page_pool_is_not_overwritten(self):
        scenario,lab,row = self.scenario(); current = {'machine_id':'machine','boot_id':'boot','total':1024,'free':1024,'reserved':0}
        row['pages'] = {'current':current,'phase':'write-intent'}; scenario.save(row)
        probe = Mock(return_value=current)
        with patch.object(M,'load',return_value=SimpleNamespace(probe=probe)),self.assertRaisesRegex(ValueError,'ambiguous'): scenario.restore_pages()
        self.assertEqual(0,probe.call_count)
        row['pages']['phase'] = 'allocated'; scenario.save(row); probe.return_value = dict(current,boot_id='replacement')
        with patch.object(M,'load',return_value=SimpleNamespace(probe=probe)),self.assertRaisesRegex(ValueError,'outside lifecycle'): scenario.restore_pages()

    def test_cleanup_does_not_reimport_already_retired_fourth_disk_and_restores_pages_before_del_node(self):
        scenario,lab,row = self.scenario(); events = []
        row.update(phase='enabled',node_uid='receiving-node',retired=[M.HOST],inventory='original-inventory',config='original: input\n',
            canaries={},host_expected={},host_before={},hostname='original-host',release=self.proof,
            multipath={'phase':'stopped','current':{},'original':{'boot_id':'boot'}})
        scenario.save(row);(self.directory/'hosts').write_text('extended-inventory')
        present = {'node':True,'native':True}
        native = {'metadata':{'uid':'native'},'spec':{'allowScheduling':False,'disks':{}}}
        def get(kind,name=None,namespace=None):
            if kind=='node':return {'metadata':{'uid':'receiving-node'}} if present['node'] else None
            if kind=='nodes.longhorn.io':return native if present['native'] else None
            if kind=='instancemanagers.longhorn.io':return {'items':[]}
            raise AssertionError(kind)
        lab.get=get;lab.remote=Mock(return_value=SimpleNamespace(stdout='{}',returncode=0))
        lab.execute=Mock();lab.core_identity=Mock();lab.installation_health=Mock();lab.host_state_script=lambda:'host'
        def wait(label,timeout,fn):self.assertTrue(fn());return True
        lab.wait=wait;lab.remove=lambda obj:present.update(native=False)
        scenario.workload.cleanup=Mock(side_effect=lambda:events.append('business-cleanup'))
        config={'longhorn_block_disks':{a:[{'name':M.DISK}] for a in M.SOURCE+(M.HOST,)},'longhorn_v2_nodes':list(M.SOURCE)+( [M.HOST])}
        scenario.current=lambda:copy.deepcopy(config)
        def apply(value,**kwargs):
            self.assertNotIn(M.HOST,value['longhorn_block_disks']);self.assertNotIn(M.HOST,value['longhorn_v2_nodes'])
            events.append('remove-fourth-declaration')
        scenario.config=Mock(side_effect=apply);scenario.native=lambda address:native
        scenario.maintenance=Mock(side_effect=AssertionError('retired fourth disk must not be implicitly recreated'))
        scenario.restore_pages=lambda:events.append('pages-restored')
        scenario.retire=Mock(side_effect=AssertionError('completed retirement must not be repeated'))
        install=SimpleNamespace(host_files=lambda lab:{},multipath=lambda *a,**k:{})
        config_writer=SimpleNamespace(atomic_config=lambda *a:None)
        pool=SimpleNamespace(collect=lambda host:self.collected)
        native_module=SimpleNamespace(patch=lambda *a:events.append('node-disabled'))
        certificates = M.load('longhorn-node-lifecycle.py')
        ssl = self.directory/'ssl'; ssl.mkdir()
        for name in ('ca.pem',M.NAME+'-kubelet.pem',M.NAME+'-kubelet-key.pem'):
            (ssl/name).write_text('owned-test-certificate-'+name)
        def load(name):
            return {'longhorn-v2-cleanup.py':SimpleNamespace(),'longhorn-v2-install.py':install,
                'longhorn-disk-lifecycle.py':native_module,'longhorn-v2-pool.py':pool,
                'longhorn-upgrade-lifecycle.py':config_writer,'longhorn-node-lifecycle.py':certificates}[name]
        def product(lab,pid,step,*args):
            events.append(step)
            if step=='del-node':present['node']=False;(self.directory/'hosts').write_text('original-inventory')
        with patch.object(M,'load',side_effect=load),patch.object(M,'supervise',side_effect=product):scenario.cleanup()
        self.assertLess(events.index('pages-restored'),events.index('del-node'))
        self.assertEqual('clean',scenario.row()['phase']);scenario.maintenance.assert_not_called();scenario.retire.assert_not_called()

    def test_product_supervisor_refuses_foreign_process_before_launch(self):
        lab = SimpleNamespace(owner=Mock(),process_identity=Mock(return_value=([b'other',b'relay-maintain'],'42')))
        with patch.object(M.subprocess,'Popen') as process,self.assertRaisesRegex(ValueError,'exact live Registry'): M.supervise(lab,42,'add-node')
        process.assert_not_called()

    def test_fixture_adapter_preserves_original_owner_and_accepts_only_original_or_exact_extension(self):
        # Use the real private fixture composition with only its base path
        # redirected. No SSH, Kubernetes process or global module is touched.
        install = M.load('longhorn-v2-install.py'); install.BASE = ROOT
        core = install.load('longhorn-v2-core.py'); core.BASE = ROOT
        profile = core.load('longhorn-v2-lab.py'); profile.BASE = ROOT
        profile.DIRECTORY = self.directory
        original = profile.inventory()
        base = profile.fixture(); base.DIRECTORY = self.directory; base.STATE = self.directory/'owner'
        state = {'cluster':base.CLUSTER,'nodes':list(M.SOURCE),'run_id':RUN,'cluster_uid':RUN,
                 'node_uids':{},'clean_start_verified':True}
        base.STATE.write_text(json.dumps(state)); (self.directory/'hosts').write_text(original)
        with patch.object(M,'load',side_effect=lambda name: SimpleNamespace(fixture=lambda:(None,base)) if name=='longhorn-v2-install.py' else profile): lab=M.fixture()
        self.assertEqual(state,lab.owner()); self.assertEqual(M.SOURCE,lab.WORKERS)
        (self.directory/'hosts').write_text(M.extended_inventory(original)); self.assertEqual(state,lab.owner())
        self.assertEqual(M.SOURCE+(M.HOST,),lab.WORKERS)
        (self.directory/'hosts').write_text(original+'foreign\n')
        with self.assertRaises(ValueError): lab.owner()


class ShellContracts(unittest.TestCase):
    def run_shell(self,mode='v2-disk-lifecycle',fail='',terminate=False):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for name in ('.venv/bin','kube-bin','tests/helpers','control','clusters/longhorn-v2-gate'):(base/name).mkdir(parents=True)
            context=base/'control/config';context.write_text('original-context')
            script=(ROOT/'tests/helpers/longhorn-v2-disk-lifecycle.sh').read_text().replace('/usr/local/kubeauto',str(base))
            script=script.replace('/var/tmp/kubeauto-longhorn-v2-disk-lifecycle.',str(base/'run.')).replace('/root/.kube/config',str(context))
            (base/'runner.sh').write_text(script)
            (base/'proof').write_text('{}')
            if mode.endswith('-cleanup'):(base/'clusters/longhorn-v2-gate'/M.RECORD).write_text('{}')
            fake=base/'.venv/bin/python';fake.write_text('''#!/usr/bin/env bash
set -eu
name="${1##*/}"; action="${2:-}"
echo "$name $action" >> "$D45_TRACE"
if [[ "$action" == relay-maintain ]]; then
 exec "$D45_PY" -c 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,lambda *a:exit(0)); Path(os.environ["D45_RELAY_READY"]).touch(); time.sleep(60)'
fi
if [[ "$action" == clean ]]; then
 "$D45_PY" -c 'import os,time; from pathlib import Path; p=Path(os.environ["D45_RELAY_READY"]); start=time.monotonic()
while not p.exists():
 assert time.monotonic()-start<3
 time.sleep(.01)'
fi
if [[ "$name:$action" == "$D45_FAIL" ]]; then exit 17; fi
if [[ "$action" == business ]]; then
 echo changed-context > "$D45_CONTEXT"
 if [[ "$D45_TERMINATE" == yes ]]; then touch "$D45_READY"; exec "$D45_PY" -c 'import time;time.sleep(2)'; fi
 echo 'LONGHORN_V2_DISK_CLIENT_EXPANSION_PASS host=192.168.47.149 selective_disable=true'
 echo 'LONGHORN_V2_DISK_NATIVE_RETIREMENT_PASS host=192.168.47.148 disk_uuid=original'
 echo 'LONGHORN_V2_DISK_NATIVE_RETIREMENT_PASS host=192.168.47.149 disk_uuid=added'
 echo 'LONGHORN_V2_DISK_REPLACEMENT_REIMPORT_PASS source=192.168.47.148 replica_domain=192.168.47.149'
 echo LONGHORN_V2_DISK_LIFECYCLE_BUSINESS_PASS
fi
if [[ "$action" == clean ]]; then echo LONGHORN_V2_DISK_LIFECYCLE_SCOPED_CLEAN_PASS; fi
''');fake.chmod(0o755)
            kubectl=base/'kube-bin/kubectl';kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LONGHORN_V2_TARGET_RELEASE_PROOF=str(base/'proof'),
                D45_TRACE=str(base/'trace'),D45_FAIL=fail,D45_PY=sys.executable,D45_RELAY_READY=str(base/'relay'),
                D45_READY=str(base/'ready'),D45_CONTEXT=str(context),D45_TERMINATE='yes' if terminate else 'no')
            process=subprocess.Popen(['bash',str(base/'runner.sh'),mode],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            if terminate:
                deadline=time.monotonic()+5
                while not (base/'ready').exists() and process.poll() is None:
                    if time.monotonic()>deadline:process.kill();self.fail('business did not start')
                    time.sleep(.01)
                process.send_signal(signal.SIGTERM)
            stdout,stderr=process.communicate(timeout=10)
            runs=list(base.glob('run.*'));durable={p.name:p.read_text().strip() for p in runs[0].iterdir() if p.name in ('exit','finalized')} if runs else {}
            trace=(base/'trace').read_text() if (base/'trace').exists() else ''
            return process.returncode,stdout,stderr,trace,durable,context.read_text()

    def test_full_success_requires_business_cleanup_durable_zero_and_context_restore(self):
        rc,out,err,trace,durable,context=self.run_shell()
        self.assertEqual(0,rc,out+err);self.assertEqual({'exit':'0','finalized':'0'},durable)
        self.assertLess(trace.index(' clean'),trace.index(' relay-clean'));self.assertIn(' finalize',trace)
        self.assertEqual(1,out.splitlines().count('LONGHORN_V2_DISK_LIFECYCLE_PASS'));self.assertEqual('original-context',context)

    def test_business_failure_preserves_first_exit_and_recovers_without_success(self):
        rc,out,err,trace,durable,context=self.run_shell(fail='longhorn-v2-disk-lifecycle.py:business')
        self.assertEqual(17,rc,out+err);self.assertEqual('17',durable['exit']);self.assertIn(' clean',trace)
        self.assertNotIn('LONGHORN_V2_DISK_LIFECYCLE_PASS',out);self.assertIn('LONGHORN_FIRST_FAILURE',err)
        self.assertEqual('original-context',context)

    def test_cleanup_failure_retains_journal_without_terminal_or_finalize(self):
        rc,out,err,trace,durable,context=self.run_shell(fail='longhorn-v2-disk-lifecycle.py:clean')
        self.assertEqual(17,rc,out+err);self.assertEqual('17',durable['finalized']);self.assertNotIn(' finalize',trace)
        self.assertIn('LONGHORN_CLEANUP_FAIL',err);self.assertNotIn('LONGHORN_V2_DISK_LIFECYCLE_PASS',out)
        self.assertEqual('original-context',context)

    def test_cleanup_only_has_own_terminal_and_no_admission_or_business(self):
        rc,out,err,trace,durable,context=self.run_shell('v2-disk-lifecycle-cleanup')
        self.assertEqual(0,rc,out+err);self.assertEqual({'exit':'0','finalized':'0'},durable)
        self.assertNotIn(' admit',trace);self.assertNotIn(' business',trace)
        self.assertIn('LONGHORN_V2_DISK_LIFECYCLE_CLEANUP_PASS',out);self.assertNotIn('LONGHORN_V2_DISK_LIFECYCLE_PASS',out)

    def test_sigterm_preserves_143_and_restores_owned_scope(self):
        rc,out,err,trace,durable,context=self.run_shell(terminate=True)
        self.assertEqual(143,rc,out+err);self.assertEqual({'exit':'143','finalized':'0'},durable)
        self.assertIn(' clean',trace);self.assertEqual('original-context',context)
        self.assertNotIn('LONGHORN_V2_DISK_LIFECYCLE_PASS',out)

    def test_unknown_mode_fails_before_lab(self):
        rc,out,err,trace,durable,context=self.run_shell('unknown')
        self.assertEqual(2,rc);self.assertFalse(trace);self.assertFalse(durable)


if __name__ == '__main__': unittest.main()
