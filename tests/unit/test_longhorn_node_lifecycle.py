"""The receiving-host gate must fail before destructive work on identity drift."""
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('node_lifecycle_contract', ROOT/'tests/helpers/longhorn-node-lifecycle.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)
RUN = '11111111-1111-1111-1111-111111111111'


class NodeLifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.log = self.directory/'release.log'; self.log.write_text(MODULE.RELEASE_MARKER+' host='+MODULE.HOST+'\n')
        self.proof = {'status':'pass', 'signed_scope':'v2-source-host-release', 'host':MODULE.HOST,
            'durable':{'exit':0, 'finalized':0}, 'cleanup':{'host_clean_verified':True,
                'raw_disk_clean_verified':True, 'page_pool_restored':True},
            'markers':[MODULE.RELEASE_MARKER], 'owner_run_id':RUN, 'source_cluster_uid':RUN,
            'released_at':'2026-10-21T12:00:00+08:00',
            'original_raw_disk':{'machine_id':'a'*32, 'path':'/dev/disk/by-id/scsi-wwid',
                                 'identity':'wwid', 'size_bytes':107374182400},
            'raw_logs':[{'path':str(self.log), 'sha256':hashlib.sha256(self.log.read_bytes()).hexdigest()}]}
        self.input = self.directory/'proof.json'
        self.candidate = {'host':MODULE.HOST, 'inventory':{'machine_id':'a'*32,'hugepages_2mi':'0'},
            'assessment':{'blockers':[], 'candidates':[{'name':'sdb','size_bytes':107374182400,
                'identity':'wwid','by_id':['/dev/disk/by-id/scsi-wwid']}]}}

    def validate(self):
        self.input.write_text(json.dumps(self.proof)); return MODULE.release_proof(self.input)

    def test_release_requires_hashed_durable_real_marker(self):
        self.assertEqual(self.proof, self.validate())
        self.log.write_text('no marker\n')
        with self.assertRaisesRegex(ValueError,'identity changed'): MODULE.release_proof(self.input)
        self.proof['raw_logs'][0]['sha256'] = hashlib.sha256(self.log.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'marker absent'): self.validate()

    def test_nonterminal_cleanup_or_foreign_host_cannot_release(self):
        original = copy.deepcopy(self.proof)
        for defect in ('status','host','exit','finalized','cleanup','markers','scope'):
            self.proof = copy.deepcopy(original)
            if defect in ('status','host'): self.proof[defect] = 'other'
            elif defect in ('exit','finalized'): self.proof['durable'][defect] = 1
            elif defect == 'cleanup': self.proof['cleanup']['page_pool_restored'] = False
            elif defect == 'markers': self.proof['markers'] = []
            else: self.proof['signed_scope'] = 'matrix-pass'
            with self.subTest(defect=defect), self.assertRaises(ValueError): self.validate()

    def test_release_marker_must_name_exact_receiving_host(self):
        for line in (MODULE.RELEASE_MARKER, MODULE.RELEASE_MARKER+' host=192.168.47.149',
                     MODULE.RELEASE_MARKER+' host='+MODULE.HOST+'0'):
            self.log.write_text(line+'\n')
            self.proof['raw_logs'][0]['sha256'] = hashlib.sha256(self.log.read_bytes()).hexdigest()
            with self.subTest(line=line), self.assertRaisesRegex(ValueError,'marker absent'): self.validate()

    def test_release_rejects_unstable_device_wrong_size_or_naive_time(self):
        original = copy.deepcopy(self.proof)
        for key,value in (('path','/dev/sdb'),('identity',''),('machine_id','unknown'),('size_bytes',1024)):
            self.proof = copy.deepcopy(original); self.proof['original_raw_disk'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.validate()
        self.proof = original; self.proof['released_at'] = '2026-10-21T12:00:00'
        with self.assertRaisesRegex(ValueError,'timezone'): self.validate()

    def test_symlink_or_missing_raw_log_blocks_release(self):
        self.validate(); target = self.directory/'alias.log'; target.symlink_to(self.log)
        self.proof['raw_logs'][0]['path'] = str(target)
        with self.assertRaises(ValueError): self.validate()
        self.input.unlink(); self.input.symlink_to(self.log)
        with self.assertRaisesRegex(ValueError,'regular'): MODULE.release_proof(self.input)

    def test_released_disk_must_match_fresh_host_identity_and_clean_assessment(self):
        disk = MODULE.disk_plan(self.proof,self.candidate,RUN)
        self.assertEqual('/dev/disk/by-id/scsi-wwid',disk['device'])
        self.assertEqual('/data/kubeauto-longhorn',disk['path'])
        self.assertEqual(str(uuid.uuid5(uuid.UUID(RUN),'node25:'+MODULE.HOST)),disk['uuid'])
        for defect in ('host','blocker','machine','identity','alias','size','pages'):
            candidate = copy.deepcopy(self.candidate)
            if defect=='host': candidate['host']='192.168.47.149'
            elif defect=='blocker':candidate['assessment']['blockers']=['existing-host-state']
            elif defect=='machine':candidate['inventory']['machine_id']='b'*32
            elif defect=='pages':candidate['inventory']['hugepages_2mi']='1024'
            else:
                row=candidate['assessment']['candidates'][0]
                row[{'identity':'identity','alias':'by_id','size':'size_bytes'}[defect]]={'identity':'foreign','alias':[], 'size':1}[defect]
            with self.subTest(defect=defect), self.assertRaises(ValueError): MODULE.disk_plan(self.proof,candidate,RUN)

    def test_inventory_extension_is_exactly_one_product_worker_line(self):
        original = '[etcd]\na\n[kube_master]\na\n[kube_node]\nb\nc\n[all:vars]\nx=1\n'
        expected = original.replace('[all:vars]',MODULE.HOST+" k8s_nodename='"+MODULE.NAME+"'\n[all:vars]")
        self.assertEqual(expected,MODULE.extended_inventory(original))
        with self.assertRaises(ValueError): MODULE.extended_inventory(expected)
        with self.assertRaises(ValueError): MODULE.extended_inventory('[kube_master]\n')

    def scenario(self):
        state = {'run_id':RUN, 'nodes':['192.168.122.'+str(n) for n in (135,40,72,212,165,238)]}
        lab = SimpleNamespace(DIRECTORY=self.directory, BASE=ROOT, owner=Mock(return_value=state),
            get=Mock(), kubectl=Mock(), remote=Mock(), remove=Mock(), core_identity=Mock(), installation_health=Mock(),
            write_private=lambda p,d:p.write_text(json.dumps(d)), execute=Mock(),
            wait=Mock(side_effect=lambda label,timeout,predicate:predicate()))
        scenario = MODULE.Lifecycle(lab,123)
        row = {'run_id':RUN,'host':MODULE.HOST,'name':MODULE.NAME,'native_uid':'native-uid','node_uid':'node-uid',
            'phase':'joined','payload':'payload','inventory':'original\n','disk':{'machine_id':'a'*32},
            'native_disks':{'default':{'path':'/data/kubeauto-longhorn'}}}
        scenario.save(row); (self.directory/'hosts').write_text(row['inventory'])
        ssl = self.directory/'ssl'; ssl.mkdir(exist_ok=True)
        for name in ('ca.pem',MODULE.NAME+'-kubelet.pem',MODULE.NAME+'-kubelet-key.pem'):
            (ssl/name).write_text('owned-test-certificate-'+name)
        scenario.workload = SimpleNamespace(identities=Mock(return_value={'binding':'same'}), data=Mock(),
            path=self.directory/MODULE.BUSINESS, cleanup=Mock())
        return scenario,lab,row

    def test_foreign_native_or_machine_identity_blocks_mutation(self):
        scenario,lab,_ = self.scenario()
        lab.get.return_value={'metadata':{'uid':'replacement'}}
        with self.assertRaisesRegex(ValueError,'identity'):scenario.native_patch(allowScheduling=False)
        lab.kubectl.assert_not_called()
        lab.remote.return_value.stdout='b'*32
        with self.assertRaisesRegex(ValueError,'machine identity'):scenario.host_identity()

    def test_guard_prerequisite_blocks_destructive_cli_before_launch(self):
        scenario,lab,_=self.scenario()
        lab.get.return_value={'metadata':{'uid':'native-uid'},'spec':{'allowScheduling':False,'disks':{}}}
        with patch.object(MODULE,'supervise') as launch:
            with self.assertRaisesRegex(ValueError,'prerequisite'):scenario.refusal('del-master')
            launch.assert_not_called()

    def test_guard_refusal_preserves_inventory_bindings_native_and_application(self):
        scenario,lab,_=self.scenario()
        lab.get.return_value={'metadata':{'uid':'native-uid'},'spec':{'allowScheduling':True,'disks':{}}}
        with patch.object(MODULE,'supervise') as launch:
            scenario.refusal('del-master')
            launch.assert_called_once_with(lab,123,'del-master',expected_refusal=MODULE.REFUSAL)
        self.assertEqual(2,scenario.workload.data.call_count)
        self.assertEqual(2,lab.core_identity.call_count)

    def test_post_refusal_binding_drift_is_not_success(self):
        scenario,lab,_=self.scenario()
        lab.get.return_value={'metadata':{'uid':'native-uid'},'spec':{'allowScheduling':True,'disks':{}}}
        scenario.workload.identities.side_effect=[{'binding':'old'},{'binding':'replacement'}]
        with patch.object(MODULE,'supervise'),self.assertRaisesRegex(ValueError,'protected identities'):
            scenario.refusal('del-node')

    def test_unconfirmed_node_creation_is_retained_instead_of_adopted(self):
        scenario,lab,row=self.scenario();row['node_uid']=None;scenario.save(row)
        scenario.host_identity=Mock();lab.get.return_value={'metadata':{'uid':'unconfirmed'}}
        with self.assertRaisesRegex(ValueError,'unconfirmed'):scenario.retire()
        lab.remove.assert_not_called()

    def partial_join(self):
        scenario, lab, row = self.scenario()
        row.update(phase='adding', config='longhorn_storage_nodes: []\n',
            inventory='[kube_master]\ncontrol\n[kube_node]\noriginal\n[all:vars]\n',
            multipath={'original': {'boot_id': 'boot-id'}})
        row['disk']['phase'] = 'mounted'; scenario.save(row)
        row.pop('node_uid'); row.pop('native_uid'); scenario.save(row)
        (self.directory/'hosts').write_text(row['inventory'])
        (self.directory/'config.yml').write_text(row['config'])
        node = {'metadata': {'name': MODULE.NAME, 'uid': 'partial-node'},
            'status': {'nodeInfo': {'machineID': 'a'*32, 'bootID': 'boot-id'},
                'addresses': [{'type': 'InternalIP', 'address': MODULE.HOST}]}}
        state = lab.owner(); state.update(cluster_uid='cluster', node_uids={'original': 'original-uid'})
        original = {'metadata': {'name': 'original', 'uid': 'original-uid'}}
        def get(kind, name='', namespace=''):
            return {'node': node, 'nodes.longhorn.io': None,
                'nodes': {'items': [original, node]}, 'namespace': {'metadata': {'uid': 'cluster'}}}[kind]
        lab.get.side_effect = get
        expected = MODULE.worker_certificates(lab, MODULE.NAME)
        lab.remote.side_effect = [SimpleNamespace(stdout='a'*32), SimpleNamespace(stdout=json.dumps(expected))]
        lab.execute.return_value = SimpleNamespace(returncode=0, stdout='LONGHORN_REMOVAL_GUARD_PASS\n')
        return scenario, lab, row, node, original

    def test_partial_join_recovery_binds_host_certificates_and_empty_product_guard(self):
        scenario, lab, row, node, _ = self.partial_join()
        with redirect_stdout(io.StringIO()): scenario.recover_partial_join()
        accepted = scenario.row()
        self.assertEqual(node['metadata']['uid'], accepted['node_uid'])
        self.assertEqual('partial-joined', accepted['phase'])
        self.assertEqual(MODULE.extended_inventory(row['inventory']), (self.directory/'hosts').read_text())
        self.assertEqual(row['config'], (self.directory/'config.yml').read_text())
        lab.remove.assert_not_called(); lab.kubectl.assert_not_called()
        self.assertIn('guard', lab.execute.call_args.args[0])

    def resumable_join(self):
        scenario, lab, row, node, original = self.partial_join()
        row.update(release_proof=self.validate(), release_sha256=hashlib.sha256(self.input.read_bytes()).hexdigest())
        row['disk'].update(path='/data/kubeauto-longhorn', uuid='filesystem-uuid')
        scenario.save(row)
        (self.directory/'config.yml').write_text('longhorn_storage_nodes: ['+MODULE.HOST+']\n')
        native = {'metadata': {'uid': 'native-created-by-add'},
            'spec': {'disks': {'data': {'path': row['disk']['path'], 'diskType': 'filesystem'}}},
            'status': {'diskStatus': {'data': {'diskUUID': 'native-disk-uuid'}}}}
        lab.remote.side_effect = [*lab.remote.side_effect,
            SimpleNamespace(stdout=json.dumps({'disk_uuid': 'native-disk-uuid', 'config_sha256': 'cfg-sha'}))]
        get = lab.get.side_effect
        lab.get.side_effect = lambda kind, *args: native if kind == 'nodes.longhorn.io' else get(kind, *args)
        lab.replica_topology = Mock(return_value={str(i): {'node': 'original-'+str(i)} for i in range(3)})
        scenario.workload.journal = Mock(return_value={'volume_uid': 'original-volume'})
        scenario.complete_add = Mock()
        return scenario, lab, row, node, original, native

    def test_partial_add_retries_product_only_after_original_host_data_and_native_identity(self):
        scenario, lab, row, node, _, native = self.resumable_join()
        with patch.object(MODULE, 'supervise') as launch, redirect_stdout(io.StringIO()):
            scenario.resume_business(self.input)
        launch.assert_called_once_with(lab, 123, 'add-node')
        scenario.workload.data.assert_called_once_with(row['payload'], read=True)
        scenario.complete_add.assert_called_once_with(lab.replica_topology.return_value,
            (node['metadata']['uid'], native['metadata']['uid']))
        self.assertNotIn('node_uid', scenario.row())
        self.assertIn('add_retry_intent', scenario.row())
        self.assertEqual(row['inventory'], (self.directory/'hosts').read_text())
        lab.remove.assert_not_called(); lab.kubectl.assert_not_called()

    def test_filesystem_and_native_disk_ids_are_independent_and_both_required(self):
        request = {'path': '/data/kubeauto-longhorn', 'device': '/dev/disk/by-id/wwn-original',
                   'uuid': 'filesystem-uuid'}
        path = self.directory/'data/kubeauto-longhorn'; path.mkdir(parents=True)
        config = path/'longhorn-disk.cfg'; config.write_text('{"diskUUID":"native-disk-uuid"}')
        mount = {'filesystems': [{'target': str(path), 'uuid': request['uuid'], 'fstype': 'ext4'}]}
        def probe(args, **kwargs):
            self.assertEqual(30, kwargs['timeout'])
            if args[0] == 'findmnt': return SimpleNamespace(stdout=json.dumps(mount))
            self.assertEqual(['blkid', '-s', 'UUID', '-o', 'value', request['device']], args)
            return SimpleNamespace(stdout='filesystem-uuid\n')
        with patch('subprocess.run', side_effect=probe):
            identity = MODULE.filesystem_disk_identity(request, root=self.directory)
            self.assertEqual('native-disk-uuid', identity['disk_uuid'])
            self.assertNotEqual(request['uuid'], identity['disk_uuid'])
            self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), identity['config_sha256'])
            mount['filesystems'][0]['uuid'] = 'foreign'
            with self.assertRaisesRegex(ValueError, 'mount identity changed'):
                MODULE.filesystem_disk_identity(request, root=self.directory)
            mount['filesystems'][0]['uuid'] = request['uuid']
            with patch('subprocess.run', side_effect=[SimpleNamespace(stdout=json.dumps(mount)),
                    SimpleNamespace(stdout='foreign')]):
                with self.assertRaisesRegex(ValueError, 'stable device'):
                    MODULE.filesystem_disk_identity(request, root=self.directory)
            config.rename(path/'foreign'); config.symlink_to(path/'foreign')
            with self.assertRaisesRegex(ValueError, 'regular owned file'):
                MODULE.filesystem_disk_identity(request, root=self.directory)

    def test_retirement_disables_disk_and_observes_zero_scheduled_storage_before_delete(self):
        scenario, lab, row = self.scenario()
        node = self.bootstrap_node(row)
        native = {'metadata': {'uid': row['native_uid']},
                  'spec': {'disks': dict(row['native_disks']), 'allowScheduling': True},
                  'status': {'diskStatus': {'default': {'diskUUID': 'native-disk', 'storageScheduled': 0}}}}
        def get(kind, *args, **kwargs):
            if kind == 'node': return node or None
            if kind == 'nodes.longhorn.io': return native
            return {'items': []}
        lab.get.side_effect = get
        scenario.host_identity = Mock(); scenario.config = Mock(); scenario.finish_retirement = Mock()
        transitions = []
        def change(**updates):
            if updates.get('disks') == {}:
                self.assertFalse(native['spec']['disks']['default']['allowScheduling'])
                self.assertEqual(0, native['status']['diskStatus']['default']['storageScheduled'])
                self.assertEqual('false', node['metadata']['labels']['node.longhorn.io/create-default-disk'])
            native['spec'].update(updates); transitions.append(copy.deepcopy(updates))
        scenario.native_patch = change
        lab.kubectl.side_effect = lambda *args, **kwargs: node['metadata']['labels'].update(
            {'node.longhorn.io/create-default-disk': json.loads(kwargs['stdin'])[-1]['value']})
        def launch(*args):
            if args[-1] == 'del-node': node.clear()
        with patch.object(MODULE, 'supervise', side_effect=launch): scenario.retire()
        self.assertEqual(False, transitions[-2]['disks']['default']['allowScheduling'])
        self.assertEqual({}, transitions[-1]['disks'])
        self.assertIn('node25-disk-disabled-empty', [call.args[0] for call in lab.wait.call_args_list])
        scenario.finish_retirement.assert_called_once()

    def bootstrap_node(self, row):
        return {'metadata': {'uid': row['node_uid'], 'resourceVersion': 'rv',
            'labels': {'kubeauto.io/component': 'longhorn', 'node.longhorn.io/create-default-disk': 'config'},
            'annotations': {'node.longhorn.io/default-disks-config': json.dumps([
                {'name': name, 'path': disk['path'], 'allowScheduling': True,
                 'storageReserved': disk.get('storageReserved', 0)} for name, disk in row['native_disks'].items()])}}}

    def test_default_disk_disable_requires_exact_owned_node_and_bootstrap_annotation(self):
        for defect in ('uid', 'owner', 'label', 'annotation'):
            with self.subTest(defect=defect):
                scenario, lab, row = self.scenario(); node = self.bootstrap_node(row)
                if defect == 'uid': node['metadata']['uid'] = 'foreign'
                elif defect == 'owner': node['metadata']['labels']['kubeauto.io/component'] = 'foreign'
                elif defect == 'label': node['metadata']['labels']['node.longhorn.io/create-default-disk'] = 'true'
                else: node['metadata']['annotations']['node.longhorn.io/default-disks-config'] = '[]'
                lab.get.return_value = node
                with self.assertRaises(ValueError): scenario.disable_default_disk()
                lab.kubectl.assert_not_called()

    def retirement_continuation(self):
        scenario, lab, row, node, _, native = self.resumable_join()
        row.update(phase='joined', node_uid=node['metadata']['uid'], native_uid=native['metadata']['uid'],
            add_retry_intent={'node_uid': node['metadata']['uid'], 'native_uid': native['metadata']['uid'],
                'disk_identity': {'disk_uuid': 'native-disk-uuid', 'config_sha256': 'cfg-sha'},
                'certificates': MODULE.worker_certificates(lab, MODULE.NAME)})
        scenario.save(row); (self.directory/'hosts').write_text(MODULE.extended_inventory(row['inventory']))
        lab.BASE = self.directory
        product = self.directory/'roles/longhorn/files/longhorn.py'; product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes((ROOT/'roles/longhorn/files/longhorn.py').read_bytes())
        log = self.directory/'business.log'; log.write_text('\n'.join([
            'LONGHORN_NODE_DATA_EXPANSION_VERIFIED replicas=4 host='+MODULE.HOST,
            'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node',
            'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master'])+'\n')
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        proof = {'status': 'completed-prefix-only', 'journal_sha256': sha(scenario.path),
            'product_sha256': sha(product), 'business_log': str(log), 'business_log_sha256': sha(log),
            'remote_receipts': [{'path': str(log), 'sha256': sha(log)}],
            'functions': {name: hashlib.sha256(inspect.getsource(
                MODULE.supervise if name == 'supervise' else getattr(MODULE.Lifecycle, name)).encode()).hexdigest()
                for name in ('complete_add', 'refusal', 'supervise')}}
        proof['certificates'] = MODULE.worker_certificates(lab, MODULE.NAME)
        # add-node regenerated leaf/key after the pre-add retry intent; CA and
        # both currently issued files remain bound to the original cluster.
        row['add_retry_intent']['certificates']['kubelet-key.pem'] = 'before-add-key'
        scenario.save(row); proof['journal_sha256'] = sha(scenario.path)
        path = self.directory/'tests/evidence/longhorn-node-retirement-prefix-20261024.json'
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(proof))
        scenario.retire = Mock(); scenario.clean = Mock()
        scenario.workload.detach = Mock(); scenario.workload.mount = Mock()
        return scenario, lab, path, proof, log

    def test_completed_add_prefix_reuses_real_guards_and_only_continues_retirement(self):
        scenario, lab, _, _, _ = self.retirement_continuation()
        with redirect_stdout(io.StringIO()) as output:
            scenario.resume_business(self.input)
        lab.execute.assert_not_called(); scenario.complete_add.assert_not_called()
        scenario.retire.assert_called_once(); scenario.clean.assert_called_once()
        self.assertIn('LONGHORN_NODE_COMPLETED_ADD_PREFIX_VERIFIED', output.getvalue())
        self.assertNotIn('LONGHORN_NODE_DATA_EXPANSION_VERIFIED', output.getvalue())
        self.assertEqual(2, scenario.workload.data.call_count)

    def test_completed_prefix_accepts_the_original_config_after_retirement_started(self):
        scenario, _, _, _, _ = self.retirement_continuation()
        (self.directory/'config.yml').write_text(scenario.row()['config'])
        with redirect_stdout(io.StringIO()): scenario.resume_business(self.input)
        scenario.retire.assert_called_once()

    def test_completed_prefix_drift_refuses_before_retirement_or_business_mutation(self):
        for defect in ('journal', 'product', 'function', 'receipt', 'marker', 'certificates', 'data', 'replicas'):
            with self.subTest(defect=defect):
                scenario, lab, path, proof, log = self.retirement_continuation()
                if defect == 'journal': proof['journal_sha256'] = 'foreign'
                elif defect == 'product': proof['product_sha256'] = 'foreign'
                elif defect == 'function': proof['functions']['refusal'] = 'foreign'
                elif defect == 'receipt': log.write_text('changed')
                elif defect == 'marker':
                    log.write_text('missing'); proof['business_log_sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
                    proof['remote_receipts'][0]['sha256'] = proof['business_log_sha256']
                elif defect == 'certificates': lab.remote.side_effect = [SimpleNamespace(stdout='a'*32), SimpleNamespace(stdout='{}')]
                elif defect == 'data': scenario.workload.data.side_effect = ValueError('changed data')
                else: lab.replica_topology.return_value = None
                path.write_text(json.dumps(proof))
                with self.assertRaises(ValueError): scenario.resume_business(self.input)
                scenario.retire.assert_not_called(); scenario.clean.assert_not_called()

    def test_partial_add_identity_or_data_drift_blocks_product_retry(self):
        for defect in ('proof', 'phase', 'boot', 'machine', 'certificate', 'original', 'native-path',
                       'disk-uuid', 'native-deleting', 'retry-intent', 'inventory', 'config', 'data', 'replicas'):
            with self.subTest(defect=defect):
                scenario, lab, row, node, original, native = self.resumable_join()
                if defect == 'proof': self.input.write_text('{}')
                elif defect == 'phase': row['phase'] = 'joined'; scenario.save(row)
                elif defect == 'boot': node['status']['nodeInfo']['bootID'] = 'other'
                elif defect == 'machine': node['status']['nodeInfo']['machineID'] = 'other'
                elif defect == 'certificate': lab.remote.side_effect = [SimpleNamespace(stdout='a'*32), SimpleNamespace(stdout='{}')]
                elif defect == 'original': original['metadata']['uid'] = 'other'
                elif defect == 'native-path': native['spec']['disks']['data']['path'] = '/foreign'
                elif defect == 'disk-uuid': native['status']['diskStatus']['data']['diskUUID'] = 'other'
                elif defect == 'native-deleting': native['metadata']['deletionTimestamp'] = 'now'
                elif defect == 'retry-intent': row['add_retry_intent'] = {}; scenario.save(row)
                elif defect == 'inventory': (self.directory/'hosts').write_text('foreign')
                elif defect == 'config': (self.directory/'config.yml').write_text(row['config'])
                elif defect == 'data': scenario.workload.data.side_effect = ValueError('changed data')
                else: lab.replica_topology.return_value = None
                with patch.object(MODULE, 'supervise') as launch:
                    with self.assertRaises(ValueError): scenario.resume_business(self.input)
                    launch.assert_not_called()
                scenario.complete_add.assert_not_called()

    def test_partial_join_drift_refuses_before_binding_or_inventory_mutation(self):
        for defect in ('machine', 'boot', 'address', 'certificate', 'native', 'original', 'guard', 'inventory', 'config'):
            with self.subTest(defect=defect):
                scenario, lab, row, node, original = self.partial_join()
                if defect == 'machine': node['status']['nodeInfo']['machineID'] = 'foreign'
                elif defect == 'boot': node['status']['nodeInfo']['bootID'] = 'foreign'
                elif defect == 'address': node['status']['addresses'][0]['address'] = 'other'
                elif defect == 'certificate': lab.remote.side_effect = [SimpleNamespace(stdout='a'*32), SimpleNamespace(stdout='{}')]
                elif defect == 'native':
                    original_get = lab.get.side_effect
                    lab.get.side_effect = lambda kind, *args: {'metadata': {'uid': 'foreign'}} if kind == 'nodes.longhorn.io' else original_get(kind, *args)
                elif defect == 'original': original['metadata']['uid'] = 'replacement'
                elif defect == 'guard': lab.execute.return_value.returncode = 1
                elif defect == 'inventory': (self.directory/'hosts').write_text('foreign')
                elif defect == 'config': (self.directory/'config.yml').write_text('foreign: true\n')
                before = (self.directory/'hosts').read_text()
                with self.assertRaises(ValueError): scenario.recover_partial_join()
                self.assertEqual(row, scenario.row())
                self.assertEqual(before, (self.directory/'hosts').read_text())
                lab.remove.assert_not_called(); lab.kubectl.assert_not_called()

    def test_replacement_kubernetes_node_blocks_retirement(self):
        scenario,lab,_=self.scenario();scenario.host_identity=Mock()
        lab.get.return_value={'metadata':{'uid':'replacement'}}
        with self.assertRaisesRegex(ValueError,'replacement'):scenario.retire()
        lab.kubectl.assert_not_called();lab.remove.assert_not_called()

    def test_missing_node_resumes_only_product_committed_retirement(self):
        scenario,lab,row=self.scenario();scenario.host_identity=Mock();scenario.finish_retirement=Mock();lab.get.return_value=None
        scenario.retire();scenario.finish_retirement.assert_called_once()
        (self.directory/'hosts').write_text('foreign')
        with self.assertRaisesRegex(ValueError,'product retirement'):scenario.retire()

    def test_disk_declarations_cannot_be_removed_if_foreign_disk_appears(self):
        scenario,lab,row=self.scenario();scenario.host_identity=Mock();scenario.native_patch=Mock()
        lab.get.return_value={'metadata':{'uid':'node-uid'}}
        scenario.native=Mock(return_value={'spec':{'disks':{'foreign':{'path':'/foreign'}}},'status':{'diskStatus':{}}})
        with self.assertRaisesRegex(ValueError,'foreign receiving disk'):scenario.retire()
        scenario.native_patch.assert_called_once_with(allowScheduling=False,evictionRequested=True)

    def test_native_empty_uses_full_product_guard_marker_and_rc(self):
        scenario,lab,_=self.scenario();scenario.native=Mock()
        for rc,output,expected in ((0,'LONGHORN_REMOVAL_GUARD_PASS\n',True),(0,'other\n',False),(1,'LONGHORN_REMOVAL_GUARD_PASS\n',False)):
            lab.execute.return_value=SimpleNamespace(returncode=rc,stdout=output)
            self.assertEqual(expected,scenario.empty())
        args=lab.execute.call_args.args[0]
        self.assertIn('guard',args);self.assertEqual(MODULE.HOST,args[-1])

    def test_host_script_syntax_and_no_force_or_lazy_unmount(self):
        compile(MODULE.host_script(),'node25-host','exec')
        self.assertIn("run('umount',str(p))",MODULE.host_script())
        self.assertNotIn("'umount','-l'",MODULE.host_script());self.assertNotIn("'umount','-f'",MODULE.host_script())

    def test_private_adapter_preserves_six_node_owner_and_accepts_only_exact_extension(self):
        lab=MODULE.fixture(); original=MODULE.load('longhorn-lab.py')
        self.assertEqual(6,len(original.NODES));self.assertEqual(7,len(lab.NODES))
        lab.DIRECTORY=self.directory;lab.STATE=self.directory/'owner.json'
        state={'cluster':lab.CLUSTER,'nodes':list(original.NODES),'clean_start_verified':True,'run_id':RUN,
               'cluster_uid':'cluster-uid','node_uids':{'longhorn-node-'+ip.rsplit('.',1)[1]:'uid-'+ip for ip in original.NODES}}
        lab.STATE.write_text(json.dumps(state));(self.directory/'hosts').write_text(original.inventory())
        self.assertEqual(state,lab.owner());self.assertEqual(3,len(lab.WORKERS))
        (self.directory/'hosts').write_text(MODULE.extended_inventory(original.inventory()))
        self.assertEqual(state,lab.owner());self.assertEqual(4,len(lab.WORKERS))
        self.assertEqual(6,len(lab.owner()['nodes']))
        (self.directory/'hosts').write_text(MODULE.extended_inventory(original.inventory())+'# foreign\n')
        with self.assertRaisesRegex(ValueError,'foreign'):lab.owner()

    def test_original_node_uid_drift_is_never_accepted_by_private_adapter(self):
        lab=MODULE.fixture();original=MODULE.load('longhorn-lab.py')
        lab.DIRECTORY=self.directory;lab.STATE=self.directory/'owner.json'
        state={'cluster':lab.CLUSTER,'nodes':list(original.NODES),'clean_start_verified':True,'run_id':RUN,
               'cluster_uid':'cluster-uid','node_uids':{'longhorn-node-'+ip.rsplit('.',1)[1]:'uid-'+ip for ip in original.NODES}}
        lab.STATE.write_text(json.dumps(state));(self.directory/'hosts').write_text(original.inventory())
        nodes=[{'metadata':{'name':'longhorn-node-'+ip.rsplit('.',1)[1],'uid':'uid-'+ip},
            'status':{'addresses':[{'type':'InternalIP','address':ip}],'conditions':[{'type':'Ready','status':'True'}]}}
            for ip in original.NODES]
        lab.get=lambda kind,*a,**kw:{'items':nodes} if kind=='nodes' else {'metadata':{'uid':'cluster-uid'}}
        with redirect_stdout(io.StringIO()):lab.core_identity()
        nodes[0]['metadata']['uid']='replacement'
        with self.assertRaisesRegex(ValueError,'identity'):lab.core_identity()
        self.assertEqual(state,json.loads(lab.STATE.read_text()))


    def test_admitted_multipath_stop_precedes_business_format_and_embedded_add_node(self):
        scenario,lab,row=self.scenario();row.update(phase='admitted',multipath={'original':{'boot_id':'boot'},'phase':'planned'})
        scenario.save(row);scenario.host_identity=Mock();events=[]
        def stop(*args,**kwargs):
            self.assertEqual('stop-intent',scenario.row()['multipath']['phase'])
            self.assertEqual('stop',args[2]);self.assertEqual({'boot_id':'boot'},kwargs['expected']);events.append('stop')
            return {'boot_id':'boot','stopped':True}
        scenario.workload.prepare=Mock(side_effect=lambda: (_ for _ in ()).throw(RuntimeError('business-boundary')))
        with patch.object(MODULE,'load',return_value=SimpleNamespace(multipath=stop)):
            with self.assertRaisesRegex(RuntimeError,'business-boundary'):scenario.business()
        self.assertEqual(['stop'],events);self.assertEqual('stopped',scenario.row()['multipath']['phase'])
        lab.remote.assert_not_called()

    def test_multipath_uncertain_stop_retains_intent_and_blocks_business_and_format(self):
        scenario,lab,row=self.scenario();row.update(phase='admitted',multipath={'original':{},'phase':'planned'})
        scenario.save(row);scenario.host_identity=Mock()
        with patch.object(MODULE,'load',return_value=SimpleNamespace(multipath=Mock(side_effect=ValueError('identity changed')))):
            with self.assertRaisesRegex(ValueError,'identity changed'):scenario.business()
        self.assertEqual('stop-intent',scenario.row()['multipath']['phase']);scenario.workload.prepare=Mock()
        lab.remote.assert_not_called()

    def test_cleanup_restores_multipath_and_owned_hostname_after_native_and_disk_retirement(self):
        scenario,lab,row=self.scenario();events=[]
        row.update(config='original: value\n',host_before={},host_expected={},hostname='original-host',
                   release_proof=self.proof,multipath={'phase':'stopped','original':{'boot_id':'boot'},'current':{'stopped':True}})
        row['disk'].update(device='/dev/disk/by-id/scsi-wwid',uuid='filesystem',path='/data/kubeauto-longhorn')
        scenario.save(row);lab.DATA=row['disk']['path'];scenario.host_identity=Mock()
        scenario.retire=Mock(side_effect=lambda:events.append('native-retired'))
        lab.verify_device=Mock();lab.disk_parent_script=lambda:'parent';lab.disk_cleanup_script=lambda:'disk-clean'
        lab.host_state_script=lambda:'host-restore'
        def remote(host,argv,**kwargs):
            if argv[0]=='lsblk':return SimpleNamespace(stdout=json.dumps({'blockdevices':[{'uuid':'filesystem','mountpoints':[]}]}))
            if argv[0]=='blkid':return SimpleNamespace(returncode=0,stdout='filesystem\n')
            if argv[0]=='wipefs':events.append('disk-signature-removed')
            if argv[:2]==['python3','-c'] and 'hostnamectl' in argv[2]:
                self.assertEqual(['a'*32,MODULE.NAME,'original-host'],argv[3:]);compile(argv[2],'hostname-restore','exec')
                self.assertIn('socket.gethostname() in (sys.argv[2],sys.argv[3])',argv[2]);events.append('hostname-restored')
            return SimpleNamespace(returncode=0,stdout='')
        lab.remote.side_effect=remote
        def restore(*args,**kwargs):
            self.assertEqual('restore',args[2]);self.assertIn('native-retired',events);self.assertIn('disk-signature-removed',events)
            self.assertEqual({'stopped':True},kwargs['expected']);events.append('multipath-restored');return kwargs['original']
        actual_load=MODULE.load
        def load(name):
            if name=='longhorn-v2-install.py':return SimpleNamespace(multipath=restore)
            if name=='longhorn-v2-pool.py':return SimpleNamespace(collect=lambda host:self.candidate)
            return actual_load(name)
        lab.get.return_value = None
        with patch.object(MODULE,'load',side_effect=load):scenario.clean()
        self.assertEqual(['native-retired','disk-signature-removed','multipath-restored','hostname-restored'],events)
        self.assertEqual('clean',scenario.row()['phase']);self.assertEqual('restored',scenario.row()['multipath']['phase'])


class WorkerCertificateContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name,value in (('etc/machine-id','machine'),('proc/sys/kernel/random/boot_id','boot')):
            p=self.root/name; p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
        self.ssl=self.root/'etc/kubernetes/ssl';self.ssl.mkdir(parents=True)
        self.expected={}
        for name in ('ca.pem','kubelet.pem','kubelet-key.pem'):
            blob=('fixture-'+name).encode();(self.ssl/name).write_bytes(blob)
            self.expected[name]=hashlib.sha256(blob).hexdigest()
        self.request={'machine_id':'machine','boot_id':'boot','expected':self.expected}

    def release(self):
        with patch('subprocess.run',return_value=SimpleNamespace(stdout='inactive\n')):
            MODULE.release_worker_certificates(self.request,self.root)

    def test_exact_product_worker_files_are_removed_and_absent_retry_is_safe(self):
        self.release();self.assertFalse(self.ssl.parent.exists());self.release()

    def test_all_files_are_validated_before_any_removal(self):
        (self.ssl/'kubelet-key.pem').write_text('replacement')
        with self.assertRaisesRegex(ValueError,'foreign'):self.release()
        self.assertEqual(set(self.expected),{p.name for p in self.ssl.iterdir()})

    def test_foreign_file_and_shared_role_configuration_are_preserved(self):
        for path in (self.ssl/'etcd-key.pem',self.ssl.parent/'admin.kubeconfig'):
            path.write_text('foreign')
            with self.subTest(path=path.name),self.assertRaisesRegex(ValueError,'foreign'):self.release()
            self.assertTrue(all((self.ssl/name).exists() for name in self.expected));path.unlink()

    def test_symlink_certificate_and_directory_are_never_followed(self):
        owned=self.ssl/'ca.pem';blob=owned.read_bytes();owned.unlink()
        target=self.root/'foreign';target.write_bytes(blob);owned.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'foreign'):self.release()
        self.assertTrue(target.exists());owned.unlink();owned.write_bytes(blob)
        original=self.ssl.with_name('original');self.ssl.rename(original);self.ssl.symlink_to(original)
        with self.assertRaisesRegex(ValueError,'symlink'):self.release()
        self.assertTrue((original/'ca.pem').exists())

    def test_machine_boot_and_active_consumers_block_before_deleting(self):
        for key in ('machine_id','boot_id'):
            old=self.request[key];self.request[key]='other'
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'identity'):self.release()
            self.request[key]=old
        with patch('subprocess.run',return_value=SimpleNamespace(stdout='active\n')):
            with self.assertRaisesRegex(ValueError,'consumer'):
                MODULE.release_worker_certificates(self.request,self.root)
        self.assertEqual(3,len(list(self.ssl.iterdir())))

    def test_remote_wrapper_refuses_live_node_or_committed_inventory_before_mutation(self):
        (self.root/'hosts').write_text('original\n')
        lab=SimpleNamespace(DIRECTORY=self.root,get=Mock(return_value={'metadata':{'uid':'live'}}),remote=Mock())
        with self.assertRaisesRegex(ValueError,'completed product'):MODULE.retire_worker_certificates(
            lab,MODULE.HOST,MODULE.NAME,'machine','boot',self.expected)
        lab.get.return_value=None;(self.root/'hosts').write_text(MODULE.HOST+' worker\n')
        with self.assertRaisesRegex(ValueError,'completed product'):MODULE.retire_worker_certificates(
            lab,MODULE.HOST,MODULE.NAME,'machine','boot',self.expected)
        lab.remote.assert_not_called()


if __name__=='__main__':unittest.main()
