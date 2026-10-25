"""Pinned upgrade boundary, immutable identities and isolated restore contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('upgrade_lifecycle_contract', ROOT/'tests/helpers/longhorn-upgrade-lifecycle.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class BundleContracts(unittest.TestCase):
    def fixture(self, defect=''):
        expected = {'name': 'system-before', 'namespace_uid': 'namespace', 'target': 's3://bucket@region/',
            'manager_image': 'registry/longhorn-manager:v1.12.0',
            'resource_uids': [['Volume', 'longhorn-system', 'volume', 'volume-uid']]}
        metadata = {'longhornversion': 'v1.12.0', 'longhorngitcommit': MODULE.OLD_MANAGER_COMMIT,
                    'longhornnamespaceuuid': 'namespace', 'managerimage': expected['manager_image']}
        volume = {'kind': 'VolumeList', 'apiVersion': 'longhorn.io/v1beta2', 'items': [
            {'metadata': {'namespace': 'longhorn-system', 'name': 'volume', 'uid': 'volume-uid'}}]}
        if defect == 'namespace': metadata['longhornnamespaceuuid'] = 'other'
        if defect == 'version': metadata['longhornversion'] = 'v1.12.1'
        if defect == 'uid': volume['items'][0]['metadata']['uid'] = 'replacement'
        if defect == 'secret': volume['kind'] = 'SecretList'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('system-before/metadata.yaml', yaml.safe_dump(metadata))
            archive.writestr('system-before/yamls/volumes.yaml', yaml.safe_dump(volume))
            if defect == 'traversal': archive.writestr('system-before/../outside.yaml', '{}')
        data = stream.getvalue()
        config = {'Name': 'system-before', 'LonghornVersion': 'v1.12.0', 'LonghornGitCommit': MODULE.OLD_MANAGER_COMMIT,
            'ManagerImage': expected['manager_image'], 'BackupTargetURL': expected['target'],
            'Checksum': hashlib.sha256(data).hexdigest()}
        if defect == 'checksum': config['Checksum'] = '0'*64
        if defect == 'commit': config['LonghornGitCommit'] = 'unreviewed'
        return json.dumps(config).encode(), data, expected

    def test_read_actual_source_version_bundle_and_resource_uid_without_extraction(self):
        result = MODULE.validate_system_bundle(*self.fixture())
        self.assertEqual('1.12.0', result['version']); self.assertEqual(1, result['resource_uids_verified'])

    def test_new_version_wrong_namespace_uid_checksum_commit_or_unsafe_bundle_is_rejected(self):
        for defect in ('version', 'namespace', 'uid', 'checksum', 'commit', 'secret', 'traversal'):
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                MODULE.validate_system_bundle(*self.fixture(defect))


class ProductBoundaryContracts(unittest.TestCase):
    def fixture(self, directory):
        source = SimpleNamespace(DIRECTORY=Path(directory), supervise_core=Mock(),
            write_private=lambda path, data: path.write_text(json.dumps(data)))
        (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump({'longhorn_version': '1.12.0'}))
        (source.DIRECTORY/'yml/longhorn').mkdir(parents=True)
        (source.DIRECTORY/'yml/longhorn/input.json').write_text('{"longhorn_version":"1.12.0"}\n')
        target = SimpleNamespace()
        shared = SimpleNamespace(Consumer=Mock(return_value=SimpleNamespace()))
        source.backup_store = Mock(return_value=SimpleNamespace())
        scenario = MODULE.Upgrade(source, target, SimpleNamespace(), shared, 21, 22)
        scenario.row = {'id': 'run', 'stage': 'source-1.12.0'}
        return source, scenario

    def test_expected_precheck_refusal_restores_config_and_proves_unchanged_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            source, scenario = self.fixture(directory); path = source.DIRECTORY/'config.yml'; before = path.read_bytes()
            with patch.object(MODULE, 'control_identity', return_value={'uid': 'same'}):
                scenario.product({'longhorn_version': '1.12.1'}, refusal='must detach')
            self.assertEqual(before, path.read_bytes())
            source.supervise_core.assert_called_once_with(21, 'longhorn-setup', expected_refusal='must detach')
            self.assertEqual(1, len(scenario.row['refusals']))

    def test_refusal_restores_rendered_input_bytes_even_when_product_overwrote_them(self):
        with tempfile.TemporaryDirectory() as directory:
            source,scenario=self.fixture(directory);rendered=source.DIRECTORY/'yml/longhorn/input.json';before=rendered.read_bytes()
            source.supervise_core.side_effect=lambda *a,**kw:rendered.write_text('{"longhorn_version":"rejected"}')
            with patch.object(MODULE,'control_identity',return_value={'uid':'same'}):scenario.product({'longhorn_version':'1.12.1'},refusal='must detach')
            self.assertEqual(before,rendered.read_bytes())

    def test_refusal_missing_or_symlinked_render_stops_before_product_or_config_change(self):
        for symlink in (False,True):
            with self.subTest(symlink=symlink),tempfile.TemporaryDirectory() as directory:
                source,scenario=self.fixture(directory);path=source.DIRECTORY/'yml/longhorn/input.json';before=(source.DIRECTORY/'config.yml').read_bytes();path.unlink()
                if symlink:path.symlink_to('/dev/null')
                with self.assertRaises(ValueError):scenario.product({'longhorn_version':'1.12.1'},refusal='must detach')
                source.supervise_core.assert_not_called();self.assertEqual(before,(source.DIRECTORY/'config.yml').read_bytes())

    def test_render_repair_uses_product_and_checks_unchanged_control_before_health(self):
        with tempfile.TemporaryDirectory() as directory:
            source,scenario=self.fixture(directory);path=source.DIRECTORY/'yml/longhorn/input.json'
            (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump({'longhorn_version':'1.12.1','longhorn_requests':[]}))
            scenario.row={'stage':'cleanup-started','refusals':[{'expected_refusal':'unsupported Longhorn upgrade or direct downgrade'}]}
            source.supervise_core.side_effect=lambda *a,**kw:path.write_text('{"longhorn_version":"1.12.1"}')
            with patch.object(MODULE,'release',return_value={'app_version':'1.12.1'}),patch.object(MODULE,'control_identity',return_value={'uid':'same'}),redirect_stdout(io.StringIO()):scenario.restore_refused_render()
            source.supervise_core.assert_called_once_with(21,'longhorn-setup')
            source.supervise_core.reset_mock();scenario.restore_refused_render();source.supervise_core.assert_not_called()

    def test_unknown_render_mismatch_or_product_control_change_never_becomes_healthy_baseline(self):
        for defect in ('unknown','request','refusal','release','control'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                source,scenario=self.fixture(directory);path=source.DIRECTORY/'yml/longhorn/input.json'
                config={'longhorn_version':'1.12.1','longhorn_requests':[]}
                scenario.row={'stage':'cleanup-started','refusals':[{'expected_refusal':'unsupported Longhorn upgrade or direct downgrade'}]}
                if defect=='unknown':path.write_text('{"longhorn_version":"1.11.0"}')
                elif defect=='request':config['longhorn_requests']=[{'kind':'backup'}]
                elif defect=='refusal':scenario.row['refusals']=[]
                (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
                source.supervise_core.side_effect=lambda *a,**kw:path.write_text('{"longhorn_version":"1.12.1"}')
                with patch.object(MODULE,'release',return_value={'app_version':'1.12.0' if defect=='release' else '1.12.1'}),patch.object(MODULE,'control_identity',side_effect=[{'uid':'before'},{'uid':'changed'}]),self.assertRaises(ValueError):scenario.restore_refused_render()
                if defect!='control':source.supervise_core.assert_not_called()

    def test_precheck_detects_any_runtime_or_crd_mutation_before_accepting_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, scenario = self.fixture(directory)
            with patch.object(MODULE, 'control_identity', side_effect=[{'uid': 'old'}, {'uid': 'new'}]):
                with self.assertRaises(ValueError): scenario.product({'longhorn_version': '1.12.1'}, refusal='refused')
            self.assertNotIn('refusals', scenario.row)
            self.assertEqual('1.12.0', yaml.safe_load((source.DIRECTORY/'config.yml').read_text())['longhorn_version'])

    def test_failed_real_rollout_keeps_new_desired_version_and_first_attempt_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            source, scenario = self.fixture(directory); source.supervise_core.side_effect = RuntimeError('rollout failed')
            with patch.object(MODULE, 'control_identity', return_value={'uid': 'old'}), self.assertRaises(RuntimeError):
                scenario.product({'longhorn_version': '1.12.1'})
            self.assertEqual('1.12.1', yaml.safe_load((source.DIRECTORY/'config.yml').read_text())['longhorn_version'])
            self.assertIsNone(scenario.row['attempt']['expected_refusal'])
            self.assertNotIn('successful_configuration', scenario.row)

    def test_old_install_refuses_target_existing_release_or_existing_crds_before_config_mutation(self):
        for defect in ('target', 'release', 'crd'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, _ = self.fixture(directory); path = source.DIRECTORY/'config.yml'; before = path.read_bytes()
                source.NODES = ('192.168.47.149',) if defect == 'target' else ('192.168.47.146', '192.168.47.147', '192.168.47.148')
                source.execute = Mock(return_value=SimpleNamespace(stdout='[{"chart":"longhorn-1.12.1"}]' if defect == 'release' else '[]'))
                source.get = Mock(return_value={'items': [{'metadata': {'name': 'volumes.longhorn.io'}}] if defect == 'crd' else []})
                with self.assertRaises(ValueError): MODULE.configure_old(source, 21)
                self.assertEqual(before, path.read_bytes()); source.supervise_core.assert_not_called()

    def test_incomplete_upgrade_cannot_delete_source_backups_or_emit_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            source, scenario = self.fixture(directory)
            scenario.path.write_text(json.dumps({'stage': 'upgrade-starting'}))
            with self.assertRaises(ValueError): scenario.cleanup()
            self.assertTrue(scenario.path.exists())
            source.backup_store.return_value.__dict__.get('cleanup', Mock()).assert_not_called()

    def test_unrelated_product_config_is_outside_upgrade_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            source, scenario = self.fixture(directory)
            with self.assertRaises(ValueError): scenario.product({'longhorn_block_disks': {}})
            source.supervise_core.assert_not_called()


class WorkloadContracts(unittest.TestCase):
    def test_v1_engine_uses_official_action_after_manager_upgrade_and_v2_never_calls_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'config.yml').write_text(yaml.safe_dump({'longhorn_image_prefix': 'registry/longhorn'}))
            old = {'metadata': {'uid': 'volume-uid'}, 'spec': {'dataEngine': 'v1', 'frontend': 'blockdev',
                'image': 'registry/longhorn/longhorn-engine:v1.12.0'}, 'status': {'robustness': 'healthy'}}
            new = copy.deepcopy(old); new['spec']['image'] = 'registry/longhorn/longhorn-engine:v1.12.1'
            new['status']['currentImage'] = new['spec']['image']
            workload = SimpleNamespace(journal=lambda: {'volume': 'data', 'volume_uid': 'volume-uid'},
                data=Mock(return_value='digest'), identities=Mock(return_value={'uid': 'unchanged'}))
            lab = SimpleNamespace(DIRECTORY=root, get=Mock(side_effect=[old, new]), kubectl=Mock(),
                replica_topology=Mock(return_value={'r': {}}), wait=lambda name, timeout, check: check())
            with patch.object(MODULE, 'release', return_value={'app_version': '1.12.1'}), redirect_stdout(io.StringIO()):
                MODULE.upgrade_v1_engine(lab, workload, 'confirmed')
            self.assertEqual('/api/v1/namespaces/longhorn-system/services/http:longhorn-frontend:80/proxy/v1/volumes/data?action=engineUpgrade', lab.kubectl.call_args.args[2])
            self.assertEqual('create', lab.kubectl.call_args.args[0])
            self.assertEqual({'image': new['spec']['image']}, json.loads(lab.kubectl.call_args.kwargs['stdin']))
            old['spec']['dataEngine'] = 'v2'; lab.get.side_effect = None; lab.get.return_value = old; lab.kubectl.reset_mock()
            with patch.object(MODULE, 'release', return_value={'app_version': '1.12.1'}), self.assertRaises(ValueError):
                MODULE.upgrade_v1_engine(lab, workload, 'confirmed')
            lab.kubectl.assert_not_called()

    def test_target_restore_waits_for_declared_one_replica_instead_of_source_three(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); lab = SimpleNamespace(DIRECTORY=root, WORKERS=('192.168.47.149',), replica_topology=Mock(return_value={'r': {}}))
            lab.write_private = lambda path, data: path.write_text(json.dumps(data))
            workload = MODULE.Workload(lab, 'restore')
            lab.kubectl = Mock(return_value=SimpleNamespace(stdout='{"metadata":{"uid":"namespace-uid"}}'))
            lab.namespace_defaults = Mock(return_value=['default'])
            def create(journal, path, obj):
                obj = copy.deepcopy(obj); obj['metadata']['uid'] = obj['kind']+'-uid'
                if obj['kind'] == 'Pod':
                    self.assertEqual(['python3', '-c', 'import signal; signal.pause()'], obj['spec']['containers'][0]['command'])
                journal['objects'][obj['kind']+'/'+obj['metadata']['name']] = obj['metadata']['uid']; lab.write_private(path, journal)
                return obj
            lab.create_business_object = create
            lab.client_pod = lambda ns, name, pvc, node: {'kind': 'Pod', 'metadata': {'name': name}, 'spec': {'containers': [{}]}}
            def get(kind, name='', namespace=''):
                if kind == 'namespace': return None
                if kind == 'storageclass': return {'parameters': {'numberOfReplicas': '1'}}
                if kind == 'pvc': return {'status': {'phase': 'Bound'}}
                if kind == 'pod': return {'metadata': {'uid': 'Pod-uid'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                raise AssertionError(kind)
            lab.get = get
            def bind(journal, path):
                journal.update(volume='new-volume', volume_uid='new-uid', pv='new-pv', pv_uid='new-pv-uid'); lab.write_private(path, journal)
            lab.bind_business_volume = bind
            def wait(name, timeout, check):
                result = check(); self.assertTrue(result, name); return result
            lab.wait = wait
            workload.prepare()
            self.assertEqual(1, lab.replica_topology.call_args.args[1])

    def test_actual_data_mismatch_stops_before_any_upgrade_success(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = SimpleNamespace(DIRECTORY=Path(directory), kubectl=Mock(return_value=SimpleNamespace(stdout='wrong')),
                payload_program=Mock(return_value='read'))
            workload = MODULE.Workload(lab, 'plain')
            workload.path.write_text(json.dumps({'namespace': 'business', 'client': 'writer'}))
            with self.assertRaises(ValueError): workload.data('confirmed', read=True)

    def test_real_legacy_retirement_is_idempotent_without_reopening_deleted_parent_journal(self):
        source = SimpleNamespace(DIRECTORY=Path('/unused'), backup_store=Mock())
        scenario = MODULE.Upgrade(source, SimpleNamespace(), None, SimpleNamespace(Consumer=Mock()), 1, 2)
        scenario.row = {'legacy': {'retired': True}}
        scenario.workloads['plain'].journal = Mock(side_effect=AssertionError('must not open deleted journal'))
        scenario.retire_legacy(); scenario.workloads['plain'].journal.assert_not_called()


class LegacyUpgradeContracts(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root/'config.yml').write_text(yaml.safe_dump({'longhorn_storage_classes': [{'name': 'longhorn', 'replicas': 3}]}))
        lab = SimpleNamespace(DIRECTORY=root, WORKERS=('192.168.47.146',), run_requests=Mock(), remove=Mock(), backup_store=Mock(),
            write_private=lambda path, data: path.write_text(json.dumps(data)))
        scenario = MODULE.Upgrade(lab, SimpleNamespace(), None, SimpleNamespace(Consumer=Mock()), 1, 2)
        scenario.row = {'payloads': {'plain': 'original'}}
        parent = SimpleNamespace(journal=lambda: {'namespace': 'business', 'volume': 'parent', 'volume_uid': 'parent-uid'})
        scenario.workloads = {'plain': parent}
        rows = {
            'pvc': {'metadata': {'uid': 'claim-uid', 'resourceVersion': '7'}, 'spec': {'storageClassName': '', 'volumeName': 'pv',
                'resources': {'requests': {'storage': '2Gi'}},
                'dataSource': {'kind': 'PersistentVolumeClaim', 'name': 'data'}}, 'status': {'phase': 'Bound'}},
            'pv': {'metadata': {'name': 'pv', 'uid': 'pv-uid'}, 'spec': {'storageClassName': '',
                'claimRef': {'uid': 'claim-uid'}, 'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'child'}}},
            'volumes.longhorn.io': {'metadata': {'name': 'child', 'uid': 'child-uid'},
                'spec': {'cloneMode': 'linked-clone', 'dataEngine': 'v2', 'numberOfReplicas': 1, 'size': '2147483648'},
                'status': {'state': 'detached', 'cloneStatus': {'sourceVolume': 'parent', 'state': 'completed'}}},
            'pod': {'metadata': {'uid': 'pod-uid'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}}
        def get(kind, name='', namespace=''):
            if kind == 'storageclass':
                return {'metadata': {'uid': 'sc-uid'}}
            if kind == 'pod' and lab.remove.called: return None
            return copy.deepcopy(rows[kind])
        lab.get = Mock(side_effect=get)
        lab.clone_claim = lambda namespace, name, size: {'metadata': {'name': name}, 'spec': {
            'dataSource': {'kind': 'PersistentVolumeClaim', 'name': 'data'}}}
        def create(obj):
            if obj['metadata']['name'] == 'legacy':
                rows['pvc']['spec']['storageClassName'] = obj['spec']['storageClassName']
                rows['pv']['spec']['storageClassName'] = obj['spec']['storageClassName']
                return {'metadata': {'uid': 'claim-uid'}}
            return rows['pod']
        parent.create = Mock(side_effect=create)
        lab.client_pod = lambda namespace, name, claim, node: {'metadata': {'name': name}, 'spec': {'containers': [{}]}}
        lab.replica_topology = Mock(return_value={'replica': {'uid': 'r-uid', 'node': 'longhorn-node-146'}})
        lab.payload_program = Mock(return_value='read-only')
        def kubectl(*args, **kwargs):
            if 'patch' in args:
                update = json.loads(kwargs['stdin'])
                rows['pvc']['spec']['resources']['requests']['storage'] = update[-1]['value']
            return SimpleNamespace(stdout=hashlib.sha256(b'original'*262144).hexdigest())
        lab.kubectl = Mock(side_effect=kubectl)
        def wait(name, timeout, check):
            result = check()
            if not result: raise TimeoutError(name)
            return result
        lab.wait = wait
        return lab, scenario, rows

    def prepare(self, lab, scenario):
        with patch.object(MODULE, 'release', return_value={'app_version': '1.12.0'}):
            scenario.prepare_legacy()

    def test_real_old_clone_is_one_replica_and_retained_without_label_injection_or_product_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, scenario, rows = self.fixture(directory)
            self.prepare(lab, scenario)
            declaration = lab.run_requests.call_args.kwargs['storage_classes'][-1]
            self.assertEqual((1, 'linked-clone'), (declaration['replicas'], declaration['clone_mode']))
            self.assertEqual('1.12.0', scenario.row['legacy']['created_version'])
            self.assertEqual(('parent', 'parent-uid'), (scenario.row['legacy']['source_volume'], scenario.row['legacy']['source_volume_uid']))
            self.assertNotIn('retired', scenario.row['legacy'])
            lab.replica_topology.assert_called_once_with({'volume': 'child', 'volume_uid': 'child-uid'}, 1)
            self.assertEqual(1, lab.remove.call_count)
            self.assertEqual('pod-uid', lab.remove.call_args.args[0]['metadata']['uid'])
            self.assertEqual(1, lab.kubectl.call_count)
            self.assertIn('exec', lab.kubectl.call_args.args)
            self.assertNotIn('longhorn.io/legacy-linked-clone', json.dumps(rows))

    def test_new_manager_cannot_manufacture_old_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, scenario, _ = self.fixture(directory)
            with patch.object(MODULE, 'release', return_value={'app_version': '1.12.1'}), self.assertRaises(ValueError):
                scenario.prepare_legacy()
            lab.run_requests.assert_not_called(); scenario.workloads['plain'].create.assert_not_called()

    def test_old_clone_rejects_claim_parent_pv_driver_count_native_parent_and_failed_replica_before_upgrade(self):
        for defect in ('claim-uid', 'claim-source', 'claim-group', 'pv-claim', 'pv-driver', 'count', 'parent', 'legacy', 'replica'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, scenario, rows = self.fixture(directory)
                if defect == 'claim-uid': rows['pvc']['metadata']['uid'] = 'foreign'
                if defect == 'claim-source': rows['pvc']['spec']['dataSource']['name'] = 'foreign'
                if defect == 'claim-group': rows['pvc']['spec']['dataSource']['apiGroup'] = 'foreign.io'
                if defect == 'pv-claim': rows['pv']['spec']['claimRef']['uid'] = 'foreign'
                if defect == 'pv-driver': rows['pv']['spec']['csi']['driver'] = 'foreign.io'
                if defect == 'count': rows['volumes.longhorn.io']['spec']['numberOfReplicas'] = 3
                if defect == 'parent': rows['volumes.longhorn.io']['status']['cloneStatus']['sourceVolume'] = 'foreign'
                if defect == 'legacy': rows['volumes.longhorn.io']['metadata']['labels'] = {'longhorn.io/legacy-linked-clone': 'true'}
                if defect == 'replica': lab.replica_topology.return_value = None
                with self.assertRaises((ValueError, TimeoutError)):
                    self.prepare(lab, scenario)
                self.assertNotIn('refusal_verified', scenario.row.get('legacy', {}))

    def upgraded(self, lab, scenario, rows):
        self.prepare(lab, scenario)
        rows['volumes.longhorn.io']['metadata']['labels'] = {
            'longhorn.io/legacy-linked-clone': 'true', 'longhorn.io/clone-source-volume': 'parent'}

    def test_native_upgrade_marker_original_csi_bindings_and_origin_feed_existing_four_refusal_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, scenario, rows = self.fixture(directory); self.upgraded(lab, scenario, rows)
            linked = SimpleNamespace(legacy_refusals=Mock()); lab.kubectl.reset_mock(); lab.run_requests.reset_mock()
            with patch.object(MODULE, 'release', return_value={'app_version': '1.12.1'}), patch.object(MODULE, 'load', return_value=linked), redirect_stdout(io.StringIO()):
                scenario.verify_legacy_upgrade()
            linked.legacy_refusals.assert_called_once_with(lab, {'created_version': '1.12.0', 'upgraded_version': '1.12.1',
                'volume_name': 'child', 'volume_uid': 'child-uid', 'source_volume': 'parent', 'source_volume_uid': 'parent-uid'})
            self.assertTrue(scenario.row['legacy']['refusal_verified'])
            lab.kubectl.assert_called_once(); lab.run_requests.assert_not_called()
            call = lab.kubectl.call_args
            self.assertEqual(('-n', 'business', 'patch', 'pvc', 'legacy', '--type=json', '--patch-file=/dev/stdin'), call.args)
            self.assertEqual([{'op':'test','path':'/metadata/uid','value':'claim-uid'},
                {'op':'test','path':'/metadata/resourceVersion','value':'7'},
                {'op':'replace','path':'/spec/resources/requests/storage','value':'3221225472'}], json.loads(call.kwargs['stdin']))
            self.assertEqual('2147483648', rows['volumes.longhorn.io']['spec']['size'])
            self.assertEqual('3221225472', rows['pvc']['spec']['resources']['requests']['storage'])

    def test_expansion_refusal_requires_persisted_owned_pvc_request_and_preserves_native_volume(self):
        for defect in ('foreign-capacity', 'unpersisted', 'changed-expansion'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, scenario, rows = self.fixture(directory); self.upgraded(lab, scenario, rows)
                if defect == 'foreign-capacity': rows['pvc']['spec']['resources']['requests']['storage'] = '4Gi'
                if defect == 'unpersisted': lab.kubectl.side_effect = None
                if defect == 'changed-expansion': scenario.row['legacy']['expansion_request'] = {'claim_uid':'foreign'}
                linked = SimpleNamespace(legacy_refusals=Mock())
                with patch.object(MODULE, 'release', return_value={'app_version':'1.12.1'}), patch.object(MODULE, 'load', return_value=linked), self.assertRaises(ValueError):
                    scenario.verify_legacy_upgrade()
                linked.legacy_refusals.assert_not_called()
                self.assertEqual('2147483648', rows['volumes.longhorn.io']['spec']['size'])

    def test_upgraded_clone_replacement_binding_marker_parent_label_and_migration_cannot_reach_refusal_gate(self):
        for defect in ('volume-uid', 'claim-uid', 'pv-uid', 'pv-driver', 'claim-parent', 'handle', 'marker', 'parent-label', 'migration'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, scenario, rows = self.fixture(directory); self.upgraded(lab, scenario, rows)
                if defect == 'volume-uid': rows['volumes.longhorn.io']['metadata']['uid'] = 'foreign'
                if defect == 'claim-uid': rows['pvc']['metadata']['uid'] = 'foreign'
                if defect == 'pv-uid': rows['pv']['metadata']['uid'] = 'foreign'
                if defect == 'pv-driver': rows['pv']['spec']['csi']['driver'] = 'foreign.io'
                if defect == 'claim-parent': rows['pvc']['spec']['dataSource']['name'] = 'foreign'
                if defect == 'handle': rows['pv']['spec']['csi']['volumeHandle'] = 'foreign'
                if defect == 'marker': rows['volumes.longhorn.io']['metadata']['labels'].pop('longhorn.io/legacy-linked-clone')
                if defect == 'parent-label': rows['volumes.longhorn.io']['metadata']['labels']['longhorn.io/clone-source-volume'] = 'foreign'
                if defect == 'migration': rows['volumes.longhorn.io']['spec']['migrationNodeID'] = 'foreign'
                linked = SimpleNamespace(legacy_refusals=Mock())
                with patch.object(MODULE, 'release', return_value={'app_version': '1.12.1'}), patch.object(MODULE, 'load', return_value=linked), self.assertRaises((ValueError, TimeoutError)):
                    scenario.verify_legacy_upgrade()
                linked.legacy_refusals.assert_not_called()

    def test_all_owned_volumes_including_retained_legacy_must_exist_with_same_uid_and_stop(self):
        for defect in ('', 'missing', 'replaced', 'foreign', 'attached', 'migration', 'engine', 'replica'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                lab, scenario, rows = self.fixture(directory); self.prepare(lab, scenario)
                plain = {'metadata': {'name': 'parent', 'uid': 'parent-uid'}, 'spec': {}, 'status': {'state': 'detached'}}
                volumes = [plain, rows['volumes.longhorn.io']]
                scenario.workloads['plain'].journal = lambda: {'volume': 'parent', 'volume_uid': 'parent-uid'}
                if defect == 'missing': volumes.pop()
                if defect == 'replaced': volumes[-1]['metadata']['uid'] = 'foreign'
                if defect == 'foreign': volumes.append({'metadata': {'name': 'foreign', 'uid': 'foreign'}, 'spec': {}, 'status': {}})
                if defect == 'attached': volumes[-1]['status']['state'] = 'attached'
                if defect == 'migration': volumes[-1]['spec']['migrationNodeID'] = 'foreign'
                def get(kind, name='', namespace=''):
                    if kind == 'volumes.longhorn.io': return {'items': volumes}
                    running = (defect == 'engine' and kind == 'engines.longhorn.io' or defect == 'replica' and kind == 'replicas.longhorn.io')
                    return {'items': [{'spec': {'volumeName': 'child'}, 'status': {'currentState': 'running' if running else 'stopped'}}]}
                lab.get.side_effect = get
                if defect:
                    with self.assertRaises((ValueError, TimeoutError)): scenario.wait_detached()
                else: scenario.wait_detached()


if __name__ == '__main__': unittest.main()


class ManagerTopologyContracts(unittest.TestCase):
    def test_actual_catalog_pin_is_used_without_changing_the_upstream_api_version(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = self.fixture(directory)
            (Path(directory)/'images.json').write_text(json.dumps({'1.12.0': {'longhorn-manager': 'v1.12.0-kubeauto.test'}}))
            pods = lab.get('pods')['items']
            for pod in pods: pod['spec']['containers'][0]['image'] = 'registry/longhorn-manager:v1.12.0-kubeauto.test'
            result = MODULE.runtime_identity(lab, '1.12.0')
            self.assertEqual(3, len(result['managers']))
            self.assertTrue(all(p['image'].endswith(':v1.12.0-kubeauto.test') for p in result['managers']))

    def fixture(self, directory, *, v1=True, defect=''):
        path=Path(directory); addresses=['192.168.122.'+str(n) for n in (135,40,72,212,165,238)] if v1 else ['192.168.47.'+str(n) for n in (146,147,148)]
        clients=addresses[-3:];names={a:'node-'+a.rsplit('.',1)[1] for a in addresses}
        config={'longhorn_image_prefix':'registry','longhorn_version':'1.12.0'}
        (path/'images.json').write_text(json.dumps({'1.12.0': {'longhorn-manager': 'v1.12.0'}}))
        (path/'config.yml').write_text(yaml.safe_dump(config));chart=path/'longhorn-1.12.0.tgz';chart.write_bytes(b'chart')
        schema={'kind':'CustomResourceDefinition','metadata':{'name':'volumes.longhorn.io'},'spec':{'scope':'Namespaced'}}
        nodes=[{'metadata':{'name':names[a]},'status':{'addresses':[{'type':'InternalIP','address':a}]}} for a in addresses]
        pods=[{'metadata':{'uid':a},'spec':{'nodeName':names[a],'containers':[{'name':'longhorn-manager','image':'registry/longhorn-manager:v1.12.0'}]},'status':{'containerStatuses':[{'name':'longhorn-manager','imageID':'sha256:'+a,'ready':True,'state':{'running':{'startedAt':'2026-10-23T00:00:00Z'}}}]}} for a in clients]
        if defect=='missing':pods.pop()
        elif defect=='duplicate':pods[-1]['spec']['nodeName']=pods[0]['spec']['nodeName']
        elif defect=='foreign':pods[-1]['spec']['nodeName']='foreign'
        elif defect=='unmapped-client':nodes.pop()
        elif defect=='foreign-client':clients=['192.0.2.1']
        elif defect=='unready':pods[-1]['status']['containerStatuses'][0]['ready']=False
        elif defect=='wrong-version':pods[-1]['spec']['containers'][0]['image']='registry/longhorn-manager:v1.12.1'
        elif defect=='image-id':pods[-1]['status']['containerStatuses'][0]['imageID']=''
        rendered = dict(config, longhorn_client_nodes=clients, longhorn_inventory_nodes=addresses,
                        longhorn_namespace='longhorn-system')
        if defect == 'generated-prefix': rendered['longhorn_image_prefix'] = 'foreign'
        elif defect == 'generated-version': rendered['longhorn_version'] = '1.12.1'
        elif defect == 'generated-namespace': rendered['longhorn_namespace'] = 'foreign'
        elif defect == 'generated-inventory': rendered['longhorn_inventory_nodes'] = addresses[:-1]
        if defect == 'raw-derived-override': config['longhorn_client_nodes'] = ['192.0.2.1']; (path/'config.yml').write_text(yaml.safe_dump(config))
        output = path/'yml/longhorn/input.json'; output.parent.mkdir(parents=True)
        if defect != 'generated-missing': output.write_text(json.dumps(rendered))
        if defect == 'generated-symlink':
            output.rename(output.with_suffix('.saved')); output.symlink_to(output.with_suffix('.saved'))
        def get(kind,*args,**kwargs):
            if kind=='pods':return {'items':pods}
            if kind=='nodes':return {'items':nodes}
            if kind=='crd':return {'metadata':{'uid':'crd'},'spec':schema['spec']}
            raise AssertionError(kind)
        product=SimpleNamespace(FILES=path,CHART_HASHES={'1.12.0':hashlib.sha256(b'chart').hexdigest()},contains=lambda a,b:a==b)
        lab=SimpleNamespace(DIRECTORY=path,NODES=addresses,WORKERS=addresses[-3:],get=Mock(side_effect=get),load_helper=Mock(return_value=product),execute=Mock(return_value=SimpleNamespace(stdout=yaml.safe_dump(schema))))
        return lab
    def test_real_shared_runtime_reader_accepts_six_node_v1_three_client_topology_and_v2(self):
        for v1 in (True,False):
            with self.subTest(v1=v1),tempfile.TemporaryDirectory() as directory:
                result=MODULE.runtime_identity(self.fixture(directory,v1=v1),'1.12.0')
                self.assertEqual(3,len(result['managers']));self.assertEqual('crd',result['crds']['volumes.longhorn.io']['uid'])
    def test_missing_duplicate_foreign_unmapped_unready_or_wrong_runtime_is_refused(self):
        for defect in ('missing','duplicate','foreign','unmapped-client','foreign-client','unready','wrong-version','image-id','generated-prefix','generated-version','generated-namespace','generated-inventory','generated-missing','generated-symlink'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):MODULE.runtime_identity(self.fixture(directory,defect=defect),'1.12.0')

    def test_raw_config_cannot_override_ansible_inventory_derived_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(3,len(MODULE.runtime_identity(self.fixture(directory,defect='raw-derived-override'),'1.12.0')['managers']))


V1_SPEC = importlib.util.spec_from_file_location('v1_protection_contract', ROOT/'tests/helpers/longhorn-v1-upgrade-lifecycle.py')
V1 = importlib.util.module_from_spec(V1_SPEC); V1_SPEC.loader.exec_module(V1)


class UpgradeCacheReceiptContracts(unittest.TestCase):
    def test_cache_retirement_reuses_actual_v1_header_aware_receipt_and_preserves_plain_v2_path(self):
        with tempfile.TemporaryDirectory() as directory:
            lab=SimpleNamespace(DIRECTORY=Path(directory),load_helper=Mock())
            encryption_spec=importlib.util.spec_from_file_location('cache_encryption',ROOT/'tests/helpers/longhorn-encryption.py')
            encryption=importlib.util.module_from_spec(encryption_spec);encryption_spec.loader.exec_module(encryption)
            lab.load_helper.side_effect=lambda name:encryption if name=='longhorn-encryption.py' else SimpleNamespace(volume_prefix=lambda volume:volume+'/')
            raw={'volume':'encrypted','backup':'enc-before','size':2*1024**3+encryption.HEADER,'objects':{'block':'original'}}
            encrypted=dict(raw,size=2*1024**3,stored_size=raw['size'],encrypted=True)
            plain={'volume':'plain','backup':'plain-before','size':2*1024**3,'objects':{'block':'plain'}}
            store=SimpleNamespace(backup_receipt=Mock(side_effect=lambda volume,name,size:dict(raw) if volume=='encrypted' and size==raw['size'] else plain if volume=='plain' and size==plain['size'] else (_ for _ in ()).throw(ValueError('capacity differs'))),
                receipt=lambda:{'bucket':'bucket'},mc=Mock(return_value=json.dumps({'Name':'encrypted','Size':raw['size'],'Labels':{'volume-encrypted':'enabled'}})))
            scenario=object.__new__(MODULE.Upgrade);scenario.source=lab;scenario.store=store;scenario.backups=SimpleNamespace();scenario.save=Mock()
            scenario.row={'points':{'one':{'name':'enc-before','volume':'encrypted','backup_uid':'enc-uid','receipt':encrypted},
                'two':{'name':'plain-before','volume':'plain','backup_uid':'plain-uid','receipt':plain}}}
            holder=SimpleNamespace(record=Path(directory)/'holder.json',receipt=lambda:{'product_binding':{'owned':True}});holder.record.write_text('{}')
            observations=[]
            def retain(adapter,actual_lab,binding,side):
                for point in adapter.receipt()['points'].values():
                    result=adapter.store.backup_receipt(point['source_volume'],point['name'],2*1024**3)
                    self.assertEqual(point['receipt'],result);observations.append(result)
                adapter.save(adapter.receipt())
            dr=SimpleNamespace(Standby=SimpleNamespace(retain_caches=retain))
            with patch.object(MODULE,'load',return_value=dr):scenario.retire_caches(lab,holder,'target')
            self.assertEqual([encrypted,plain],observations)
            self.assertEqual([(('encrypted','enc-before',raw['size']),), (('plain','plain-before',plain['size']),)],[(c.args,) for c in store.backup_receipt.call_args_list])
            store.mc.return_value=json.dumps({'Name':'encrypted','Size':raw['size'],'Labels':{'volume-encrypted':'disabled'}})
            with patch.object(MODULE,'load',return_value=dr),self.assertRaises(ValueError):scenario.retire_caches(lab,holder,'target')


class V1ProtectionContinuationContracts(unittest.TestCase):
    def fixture(self, directory):
        base = Path(directory); source_dir = base/'source'; target_dir = base/'target'
        source_dir.mkdir(); target_dir.mkdir()
        source = SimpleNamespace(DIRECTORY=source_dir, NODES=V1.SOURCE_NODES, WORKERS=V1.SOURCE_NODES[-3:])
        target = SimpleNamespace(DIRECTORY=target_dir, NODES=('192.168.47.149',))
        row = {'id':'a'*32, 'source_identity':{'cluster_uid':'source'}, 'target_identity':{'cluster_uid':'target'},
            'stage':'source-1.12.0', 'old_runtime':{'version':'1.12.0','managers':['old']},
            'old_encrypted_device':{'uuid':'luks'}, 'payloads':{}, 'points':{},
            'restores':{'v1-plain-before':{'remount':True}}}
        scenario = object.__new__(V1.V1Upgrade)
        scenario.source=source; scenario.target=target; scenario.source_pid=21; scenario.target_pid=22
        scenario.path=source_dir/'longhorn-v1-upgrade-lifecycle.json'; scenario.workloads={}
        source.write_private=lambda p,r:p.write_text(json.dumps(r))
        source.check_business_graph=Mock();source.replica_topology=Mock(return_value={'three':'healthy'})
        probe=SimpleNamespace(host_probe=Mock(return_value={'uuid':'luks'}))
        source.load_helper=Mock(return_value=SimpleNamespace(Probe=Mock(return_value=probe)))
        volumes={}
        for name in ('plain','encrypted'):
            saved={'volume':name,'volume_uid':'uid-'+name,'namespace':'business-'+name,'client':'writer','objects':{'Pod/writer':'original-pod'}}
            scenario.workloads[name]=SimpleNamespace(journal=Mock(return_value=saved),identities=Mock(),data=Mock(return_value='digest'),prepare=Mock())
            row['payloads'][name]=row['id']+'-'+name
            snapshot={'kind':'snapshot','name':'snapshot-'+name,'volume':name}
            request={'kind':'backup','name':'backup-'+name,'snapshot':snapshot['name'],'mode':'full'}
            row['points'][name+'-before']={'volume':name,'payload':row['payloads'][name],
                'snapshot_request':snapshot,'request':request,'name':request['name'],'backup_uid':'b-'+name,'snapshot_uid':'s-'+name}
            volumes[name]={'metadata':{'uid':'uid-'+name},'spec':{'dataEngine':'v1','image':'registry/longhorn-engine:v1.12.0'},
                'status':{'currentImage':'registry/longhorn-engine:v1.12.0','state':'attached','robustness':'healthy'}}
        scenario.backups=SimpleNamespace(target_configuration=lambda s:s)
        source.installation_health=Mock();target.installation_health=Mock()
        for lab,version,engine,key in ((source,'1.12.0','v1','source'),(target,'1.12.1','v2','target')):
            (lab.DIRECTORY/'config.yml').write_text(yaml.safe_dump({'longhorn_version':version,'longhorn_data_engines':[engine],
                'longhorn_requests':[],'longhorn_image_prefix':'registry'}))
            lab.identity=row[key+'_identity']; lab.version=version
            data={'key':'encoded'}; secret={'metadata':{'uid':'secret-'+key},'data':data}
            binding={'secret_name':'backup-'+key,'target_uid':'target-'+key,'namespace_uid':'ns-'+key,
                'secret_uid':secret['metadata']['uid'],'secret_digest':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),
                'fixture_spec':{'backupTargetURL':'s3://bucket@region/'}}
            def get(kind,name=None,namespace=None,*,_lab=lab,_key=key,_binding=binding,_secret=secret):
                if kind=='namespace':return {'metadata':{'uid':'ns-'+_key}}
                if kind=='secret':return _secret
                if kind=='backuptargets.longhorn.io':return {'metadata':{'uid':'target-'+_key},'spec':_binding['fixture_spec'],'status':{'available':True}}
                if kind=='volumes.longhorn.io':return volumes[name] if name else {'items':list(volumes.values()) if _lab is source else []}
                if kind=='snapshots.longhorn.io':return {'metadata':{'uid':'s-'+name.removeprefix('snapshot-')}}
                if kind=='backups.longhorn.io':return {'metadata':{'uid':'b-'+name.removeprefix('backup-')}}
                raise AssertionError(kind)
            lab.get=Mock(side_effect=get)
            holder=SimpleNamespace(lab=lab,receipt=Mock(return_value={'product_binding':binding}),qualify=Mock(),
                inspect=Mock(return_value={'State':{'Running':True}}),live=Mock(return_value=True),prepare=Mock(),
                product_configuration=Mock(return_value={'existing':'configuration'}),initialize=Mock())
            if lab is source:scenario.store=holder
            else:scenario.consumer=holder
        scenario.store.directory=source_dir/'store';scenario.store.directory.mkdir()
        (scenario.store.directory/'transport.log').write_text('original tunnel failure')
        transport={'socket':str(scenario.store.directory/'transport.sock'),'pod_name':'original-transport'}
        scenario.store.transport_receipt=Mock(return_value=transport)
        scenario.store.transport_listener=Mock(return_value=None)
        scenario.store.cleanup_transport=Mock()
        original=source.get.side_effect
        source.get.side_effect=lambda kind,*a,**kw: {'metadata':{'uid':'original-pod'}} if kind=='pod' else original(kind,*a,**kw)
        scenario.path.write_text(json.dumps(row))
        source.run_requests=Mock(side_effect=RuntimeError('after-owning-request'))
        return scenario,row,volumes,probe

    def guards(self, scenario, row):
        return (patch.object(V1.upgrade,'release',side_effect=lambda l:{'app_version':l.version,'status':'deployed'}),
                patch.object(V1.upgrade,'retained_identity',side_effect=lambda l,w:l.identity),
                patch.object(V1.upgrade,'runtime_identity',return_value=row['old_runtime']))

    def test_real_resume_guard_and_business_handoff_preserve_sources_and_reach_original_upgrade_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,volumes,probe=self.fixture(directory);trace=[]
            s.protect=Mock(side_effect=lambda name,label,c:trace.append(('protect',name,label)) or name)
            s.independent_restore=Mock(side_effect=lambda name:trace.append(('restore',name)))
            s.upgrade_and_recover=Mock(side_effect=lambda *a:trace.append(('upgrade',)))
            def cleanup():
                trace.append(('cleanup-transport',))
            def qualify():
                self.assertFalse((s.store.directory/'transport.log').exists())
                self.assertEqual('original tunnel failure',(s.store.directory/('transport-before-v1-resume-'+row['id']+'.log')).read_text())
                trace.append(('qualify-transport',))
            s.store.cleanup_transport.side_effect=cleanup;s.store.qualify.side_effect=qualify
            s.source.run_requests.side_effect=lambda *a,**kw:trace.append(('product-target',))
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],redirect_stdout(io.StringIO()):s.business()
            self.assertEqual([('cleanup-transport',),('qualify-transport',),('product-target',),('protect','plain','before'),('restore','plain'),('protect','encrypted','before'),('restore','encrypted'),('upgrade',)],trace)
            s.store.prepare.assert_not_called();s.store.product_configuration.assert_not_called();s.consumer.initialize.assert_not_called()
            s.store.qualify.assert_called_once();s.consumer.qualify.assert_not_called()
            for w in s.workloads.values():w.prepare.assert_not_called();w.data.assert_called_once()
            self.assertEqual(row,json.loads(s.path.read_text()))

    def test_transport_reentry_refuses_live_replacement_missing_pod_or_unsafe_logs(self):
        for defect in ('live-socket','live-listener','missing-binding','missing-pod','missing-log','archive','symlink-log'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,_,_=self.fixture(directory);s.row=row
                binding=s.store.transport_receipt.return_value
                if defect=='live-socket':Path(binding['socket']).touch()
                elif defect=='live-listener':s.store.transport_listener.return_value={'pid':99}
                elif defect=='missing-binding':s.store.transport_receipt.return_value=None
                elif defect=='missing-pod':s.source.get.side_effect=lambda *a,**kw:None
                elif defect=='missing-log':(s.store.directory/'transport.log').unlink()
                elif defect=='archive':(s.store.directory/('transport-before-v1-resume-'+row['id']+'.log')).touch()
                elif defect=='symlink-log':
                    (s.store.directory/'transport.log').unlink();(s.store.directory/'transport.log').symlink_to('/dev/null')
                with self.assertRaises(ValueError):s.lost_protection_transport()
                s.store.cleanup_transport.assert_not_called();s.store.qualify.assert_not_called()

    def test_real_guard_refuses_mutation_of_every_reused_protection_boundary(self):
        for defect in ('stage','system','id','payload','point','source','target','runtime','version','engine','image',
                       'replicas','foreign-volume','target-volume','unfinished-restore','key','store','container','luks'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,volumes,probe=self.fixture(directory);runtime=copy.deepcopy(row['old_runtime'])
                if defect=='stage':row['stage']='upgrade-starting'
                elif defect=='system':row['system']={}
                elif defect=='id':row['id']='../foreign'
                elif defect=='payload':row['payloads']['plain']='changed'
                elif defect=='point':row['points']['plain-before']['volume']='foreign'
                elif defect in ('source','target'):getattr(s,defect).identity={'cluster_uid':'foreign'}
                elif defect=='runtime':runtime['managers']=['replacement']
                elif defect=='version':s.source.version='1.12.1'
                elif defect in ('engine','image'):volumes['plain']['spec']['dataEngine' if defect=='engine' else 'image']='changed'
                elif defect=='replicas':s.source.replica_topology.return_value={}
                elif defect=='foreign-volume':volumes['foreign']={'metadata':{'uid':'foreign'}}
                elif defect=='target-volume':
                    original=s.target.get.side_effect;s.target.get.side_effect=lambda kind,*a,**kw:{'items':[{'metadata':{'uid':'foreign'}}]} if kind=='volumes.longhorn.io' else original(kind,*a,**kw)
                elif defect=='unfinished-restore':(s.target.DIRECTORY/'longhorn-upgrade-business-v1-plain-before.json').write_text('{}')
                elif defect=='key':s.store.receipt.return_value['product_binding']['secret_digest']='changed'
                elif defect=='store':s.store.live.return_value=False
                elif defect=='container':s.store.inspect.return_value=None
                elif defect=='luks':probe.host_probe.return_value={'uuid':'changed'}
                s.path.write_text(json.dumps(row));before=s.path.read_bytes();guards=self.guards(s,row)
                with guards[0],guards[1],patch.object(V1.upgrade,'runtime_identity',return_value=runtime):
                    with self.assertRaises(ValueError):s.resume_preflight()
                self.assertEqual(before,s.path.read_bytes());s.source.run_requests.assert_not_called();s.store.qualify.assert_not_called()

    def test_real_protection_reuses_exact_original_requests_and_rejects_replacement_before_product(self):
        for defect in ('none','request','uid'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,volumes,probe=self.fixture(directory);s.row=row;point=row['points']['plain-before']
                if defect=='request':point['request']['mode']='incremental'
                if defect=='uid':point['backup_uid']='replacement'
                with patch.object(V1.secrets,'token_hex',side_effect=AssertionError('must not create a new protection name')):
                    if defect=='none':
                        with self.assertRaisesRegex(RuntimeError,'after-owning-request'):s.protect('plain','before',{})
                        self.assertEqual([point['snapshot_request'],point['request']],s.source.run_requests.call_args.args[1])
                    else:
                        with self.assertRaises(ValueError):s.protect('plain','before',{})
                        s.source.run_requests.assert_not_called()

    def test_original_fresh_business_still_prepares_both_sources_and_uses_one_new_store_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,volumes,probe=self.fixture(directory);s.path.unlink();s.validate_baseline=Mock()
            s.protect=Mock(return_value='point');s.independent_restore=Mock();s.upgrade_and_recover=Mock()
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],redirect_stdout(io.StringIO()):s.business()
            s.validate_baseline.assert_called_once();s.store.prepare.assert_called_once();s.store.product_configuration.assert_called_once()
            s.consumer.initialize.assert_called_once()
            self.assertEqual(2,s.independent_restore.call_count);s.upgrade_and_recover.assert_called_once()
            for w in s.workloads.values():w.prepare.assert_called_once()


class RetainedCRDUpgradeContracts(unittest.TestCase):
    def test_pinned_charts_add_only_the_two_reviewed_crds(self):
        import tarfile
        import re
        names={}
        for version,sha in (('1.12.0','869bb20701b154473606f1e8967b27f34f2448a2dfe6eb8970f1cae6957384f5'),
                            ('1.12.1','c8cf4b35a9d872cd5f7e44fd26d8e6ac7c2abaee42f4e2f2a0b0ebbc6e3a6116')):
            path=ROOT/'roles/longhorn/files'/('longhorn-'+version+'.tgz')
            self.assertEqual(sha,hashlib.sha256(path.read_bytes()).hexdigest())
            with tarfile.open(path) as archive:
                raw=archive.extractfile('longhorn/templates/crds.yaml').read().decode()
            names[version]=set(re.findall(r'^  name: ([a-z]+\.longhorn\.io)$',raw,re.M))
        self.assertIn('volumes.longhorn.io',names['1.12.0'])
        self.assertEqual({'shards.longhorn.io','shardgroups.longhorn.io'},names['1.12.1']-names['1.12.0'])
        self.assertFalse(names['1.12.0']-names['1.12.1'])

    def test_only_chart_verified_additions_pass_and_every_original_identity_stays_exact(self):
        original={'cluster_uid':'original','credentials':{'key':'digest'},'crd_uids':{'volumes.longhorn.io':'volume-crd'}}
        current=copy.deepcopy(original);current['crd_uids'].update({'shards.longhorn.io':'shards','shardgroups.longhorn.io':'groups'})
        runtime={'version':'1.12.1','crds':{k:{'uid':v} for k,v in current['crd_uids'].items()}}
        MODULE.verify_retained(current,original,runtime)
        for defect in ('replace-old','replace-new','foreign','missing-new','schema-not-verified','wrong-version','key','cluster'):
            with self.subTest(defect=defect):
                actual=copy.deepcopy(current);verified=copy.deepcopy(runtime)
                if defect=='replace-old':actual['crd_uids']['volumes.longhorn.io']='replacement'
                elif defect=='replace-new':actual['crd_uids']['shards.longhorn.io']='replacement'
                elif defect=='foreign':actual['crd_uids']['foreign.longhorn.io']='foreign'
                elif defect=='missing-new':del actual['crd_uids']['shards.longhorn.io']
                elif defect=='schema-not-verified':del verified['crds']['shards.longhorn.io']
                elif defect=='wrong-version':verified['version']='1.12.0'
                elif defect=='key':actual['credentials']['key']='changed'
                else:actual['cluster_uid']='changed'
                with self.assertRaises(ValueError):MODULE.verify_retained(actual,original,verified)


class V1ManagerContinuationContracts(unittest.TestCase):
    def fixture(self,directory):
        s,row,volumes,probe=V1ProtectionContinuationContracts.fixture(self,directory)
        spec=importlib.util.spec_from_file_location('manager_resume_product',ROOT/'roles/longhorn/files/longhorn.py')
        product=importlib.util.module_from_spec(spec);spec.loader.exec_module(product)
        source=s.source;target=s.target;source.version='1.12.1'
        row.update(stage='upgrade-starting',new_runtime={'version':'1.12.1','managers':['new']},
            retained_before=source.identity,attempt={'expected_refusal':None},
            refusals=[{'expected_refusal':'Longhorn upgrade requires change ID and verified system backup'},
                      {'expected_refusal':'Longhorn upgrade system backup is not Ready for the source version'}])
        config=yaml.safe_load((source.DIRECTORY/'config.yml').read_text());config.update(longhorn_version='1.12.1',
            longhorn_upgrade_id='upgrade-'+row['id'],longhorn_upgrade_system_backup='system-before')
        (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config));row['successful_configuration']=config
        raw=json.dumps({'Checksum':'a'*64})
        row['system']={'name':'system-before','uid':'system-uid','manager_image':'old-manager',
            'request':{'kind':'system-backup','name':'system-before','volume_backup_policy':'disabled'},
            'receipt':{'config_sha256':hashlib.sha256(raw.encode()).hexdigest(),'zip_sha256':'a'*64,'version':'1.12.0','resource_uids_verified':6}}
        source_get=source.get.side_effect;target_get=target.get.side_effect
        original_key=source_get('secret','longhorn-volume-key');key_data=original_key['data']
        sc={'name':'recovery-key','replicas':1,'fs_type':'ext4','data_engine':'v2','reclaim_policy':'Retain',
            'encrypted':True,'encryption_secret':'recovery-key','default':False,'recurring_jobs':[]}
        row['target_key']={'name':'recovery-key','uid':'target-key','source_uid':original_key['metadata']['uid'],
            'data_sha256':hashlib.sha256(json.dumps(key_data,sort_keys=True).encode()).hexdigest(),'class':sc,'class_uid':'class-uid'}
        objects={}
        for name,w in s.workloads.items():
            point=row['points'][name+'-before'];point.update(sha256='b'*64,url='s3://bucket/'+name)
            for field in ('snapshot','backup'):
                request=point['snapshot_request' if field=='snapshot' else 'request'];obj=product.request_object(request)
                obj['metadata']['uid']=point[field+'_uid']
                if field=='backup':obj['status']={'state':'Completed','progress':100,'url':point['url']}
                objects[request['name']]=obj
            row['restores']['v1-'+name+'-before']={'remount':True,'confirmed_sha256':point['sha256'],
                'independent_write_sha256':'c'*64,'volume_uid':'independent-'+name}
            if name=='encrypted':row['restores']['v1-'+name+'-before']['encrypted_device']=row['old_encrypted_device']
        system=product.request_object(row['system']['request']);system['metadata']['uid']='system-uid'
        system['status']={'state':'Ready','version':'v1.12.0','managerImage':'old-manager'}
        def get_source(kind,name=None,*args,**kwargs):
            if kind=='systembackups.longhorn.io':return system
            if kind in ('snapshots.longhorn.io','backups.longhorn.io'):return objects.get(name)
            return source_get(kind,name,*args,**kwargs)
        class_object=product.storage_class(sc);class_object['metadata']['uid']='class-uid'
        key_object={'metadata':{'uid':'target-key'},'data':key_data}
        def get_target(kind,name=None,*args,**kwargs):
            if kind=='storageclass':return class_object
            if kind=='secret' and name=='recovery-key':return key_object
            return target_get(kind,name,*args,**kwargs)
        source.get.side_effect=get_source;target.get.side_effect=get_target
        encryption=source.load_helper.return_value
        source.load_helper.side_effect=lambda name:product if name.endswith('/longhorn.py') else encryption
        binding=s.store.transport_receipt.return_value;Path(binding['socket']).touch()
        binding['listener']={'pid':123};s.store.transport_listener.return_value=binding['listener']
        s.store.receipt.return_value['bucket']='owned';s.store.mc=Mock(return_value=raw)
        s.path.write_text(json.dumps(row));s.row=row
        return s,row,objects,system,key_object,class_object

    def guards(self,s,row):
        return (patch.object(V1.upgrade,'release',side_effect=lambda l:{'app_version':l.version,'status':'deployed'}),
                patch.object(V1.upgrade,'retained_identity',side_effect=lambda l,w:l.identity),
                patch.object(V1.upgrade,'runtime_identity',return_value=row['new_runtime']))

    def test_real_guard_then_business_consumes_before_proof_and_skips_completed_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,*_=self.fixture(directory);s.finish_manager_upgrade=Mock();s.protect=Mock();s.product=Mock();s.system_backup=Mock()
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],redirect_stdout(io.StringIO()) as output:s.business()
            s.finish_manager_upgrade.assert_called_once();self.assertTrue(s.finish_manager_upgrade.call_args.kwargs['verified'])
            s.protect.assert_not_called();s.product.assert_not_called();s.system_backup.assert_not_called()
            s.store.prepare.assert_not_called();s.store.qualify.assert_not_called();s.store.cleanup_transport.assert_not_called()
            s.source.run_requests.assert_not_called();s.consumer.initialize.assert_not_called()
            self.assertIn('BEFORE_RESTORES_REVALIDATED count=2',output.getvalue())
            self.assertEqual(row,json.loads(s.path.read_text()))

    def test_unaccepted_engine_action_reentry_reuses_real_manager_boundary_without_replaying_product(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,*_=self.fixture(directory)
            row['stage']='manager-upgraded-engine-pending';s.path.write_text(json.dumps(row))
            s.finish_manager_upgrade=Mock();s.product=Mock();s.protect=Mock()
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],redirect_stdout(io.StringIO()):s.business()
            s.finish_manager_upgrade.assert_called_once()
            self.assertTrue(s.finish_manager_upgrade.call_args.kwargs['verified'])
            s.product.assert_not_called();s.protect.assert_not_called();s.store.qualify.assert_not_called()
            self.assertEqual(row,json.loads(s.path.read_text()))

    def test_pending_action_reentry_refuses_partial_engine_change_or_record_before_retry(self):
        for defect in ('record','desired-image','current-image','state'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,*_=self.fixture(directory);row['stage']='manager-upgraded-engine-pending'
                if defect=='record':row['engines_upgraded']=['plain']
                else:
                    original=s.source.get.side_effect
                    def get(kind,name=None,*a,**kw):
                        obj=copy.deepcopy(original(kind,name,*a,**kw))
                        if kind=='volumes.longhorn.io' and name=='plain':
                            if defect=='desired-image':obj['spec']['image']='new'
                            elif defect=='current-image':obj.setdefault('status',{})['currentImage']='new'
                            else:obj.setdefault('status',{})['state']='attaching'
                        return obj
                    s.source.get.side_effect=get
                s.path.write_text(json.dumps(row));before=s.path.read_bytes();guards=self.guards(s,row)
                with guards[0],guards[1],guards[2],self.assertRaises(ValueError):s.resume_preflight()
                self.assertEqual(before,s.path.read_bytes());s.source.run_requests.assert_not_called()

    def expired_fixture(self, directory):
        s,row,*_=self.fixture(directory);row['stage']='manager-upgraded-engine-pending'
        original=s.source.get.side_effect;pods={};replicas=[]
        def client(namespace,name,claim,node):
            return {'kind':'Pod','metadata':{'name':name,'namespace':namespace},'spec':{
                'restartPolicy':'Never','nodeSelector':{'kubernetes.io/hostname':node},
                'containers':[{'name':'client','image':'pinned','command':['python3','-c','import time; time.sleep(7200)'],
                    'volumeMounts':[{'name':'data','mountPath':'/data'}]}],
                'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':claim}}]}}
        s.source.client_pod=client
        for name,w in s.workloads.items():
            saved=w.journal();pod=client(saved['namespace'],'writer','data','longhorn-node-212')
            pod['metadata']['uid']='original-pod';pod['status']={'phase':'Succeeded','containerStatuses':[
                {'name':'client','restartCount':0,'state':{'terminated':{'exitCode':0,'reason':'Completed'}}}]}
            pod['spec']['volumes'].append({'name':'kube-api-access-defaulted','projected':{'sources':[{'serviceAccountToken':{'path':'token'}}]}})
            pod['spec']['containers'][0]['volumeMounts'].append({'name':'kube-api-access-defaulted','mountPath':'/var/run/secrets/kubernetes.io/serviceaccount','readOnly':True})
            pods[saved['namespace']]=pod
            volume=original('volumes.longhorn.io',saved['volume']);volume['spec'].update(nodeID='',numberOfReplicas=3)
            volume['status'].update(state='detached',robustness='unknown')
            for i,node in enumerate(('212','165','238')):
                replicas.append({'metadata':{'uid':name+node},'spec':{'volumeName':saved['volume'],
                    'nodeID':'longhorn-node-'+node,'healthyAt':'original','failedAt':'','image':volume['spec']['image']},
                    'status':{'currentState':'stopped'}})
            def mount(client_name,index,*,w=w,saved=saved,volume=volume):
                self.assertEqual('writer-resumed',client_name);self.assertEqual(0,index)
                # Original completed Pod UID remains in the cleanup graph.
                saved['client']=client_name;saved['objects']['Pod/'+client_name]='new-client'
                pod=client(saved['namespace'],client_name,'data','longhorn-node-212')
                pod['metadata']['uid']='new-client';pod['status']={'phase':'Running'};pods[saved['namespace']]=pod
                volume['status'].update(state='attached',robustness='healthy')
            w.mount=Mock(side_effect=mount)
        def get(kind,name=None,namespace=None,**kw):
            if kind=='pod' and namespace in pods:return pods[namespace]
            if kind=='replicas.longhorn.io':return {'items':replicas}
            return original(kind,name,namespace,**kw)
        s.source.get.side_effect=get;s.path.write_text(json.dumps(row))
        s.source.wait=lambda n,t,check:self.assertTrue(check())
        return s,row,pods,replicas

    def test_expired_clients_reattach_same_volumes_after_all_readonly_guards_then_recheck_data_and_luks(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,pods,replicas=self.expired_fixture(directory);s.finish_manager_upgrade=Mock()
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],redirect_stdout(io.StringIO()) as output:s.business()
            for w in s.workloads.values():
                w.mount.assert_called_once_with('writer-resumed',0)
                self.assertEqual(2,w.data.call_count)
                self.assertEqual('original-pod',w.journal()['objects']['Pod/writer'])
            self.assertIn('EXPIRED_CLIENTS_REATTACHED original_volumes=true original_data=true',output.getvalue())
            s.finish_manager_upgrade.assert_called_once()
            s.store.prepare.assert_not_called();s.source.run_requests.assert_not_called()

    def test_expired_client_readonly_preflight_never_reads_unmounted_data_or_renews_a_client(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,*_=self.expired_fixture(directory);before=s.path.read_bytes();guards=self.guards(s,row)
            with guards[0],guards[1],guards[2]:s.resume_preflight(allow_expired_clients=True)
            self.assertEqual(['plain','encrypted'],s.expired_clients);self.assertEqual(before,s.path.read_bytes())
            for w in s.workloads.values():w.data.assert_not_called();w.mount.assert_not_called()
            guards=self.guards(s,row)
            with guards[0],guards[1],guards[2],self.assertRaises(ValueError):s.resume_preflight()

    def test_failed_changed_or_unhealthy_expired_client_refuses_before_any_mount(self):
        for defect in ('uid','exit','restart','command','claim','replica-error','replica-image','replica-node','volume-image','binding','runtime'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,pods,replicas=self.expired_fixture(directory);pod=next(iter(pods.values()))
                if defect=='uid':pod['metadata']['uid']='foreign'
                elif defect=='exit':pod['status']['containerStatuses'][0]['state']['terminated']['exitCode']=1
                elif defect=='restart':pod['status']['containerStatuses'][0]['restartCount']=1
                elif defect=='command':pod['spec']['containers'][0]['command']=['true']
                elif defect=='claim':pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName']='foreign'
                elif defect=='replica-error':replicas[0]['spec']['failedAt']='failed'
                elif defect=='replica-image':replicas[0]['spec']['image']='new'
                elif defect=='replica-node':replicas[0]['spec']['nodeID']='foreign'
                elif defect=='volume-image':s.source.get('volumes.longhorn.io','plain')['status']['currentImage']='new'
                elif defect=='binding':s.workloads['plain'].identities.side_effect=ValueError('changed')
                else:row['new_runtime']['managers']=['foreign'];s.path.write_text(json.dumps(row))
                guards=self.guards(s,row)
                with guards[0],guards[1],patch.object(V1.upgrade,'runtime_identity',return_value={'version':'1.12.1','managers':['new']}),self.assertRaises(ValueError):s.business()
                for w in s.workloads.values():w.mount.assert_not_called()

    def test_manager_guard_refuses_each_changed_protection_and_handoff_prerequisite_before_mutation(self):
        for defect in ('refusal','attempt','configuration','source','runtime','old-engine','source-data','backup-uid','backup-error',
                       'snapshot-request','system-uid','system-version','bundle','restore-hash','restore-write','restore-source',
                       'encrypted-header','target-key','class','transport','target-volume'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                s,row,objects,system,key,sc=self.fixture(directory)
                if defect=='refusal':row['refusals'].pop()
                elif defect=='attempt':row['attempt']['expected_refusal']='not-real-rollout'
                elif defect=='configuration':row['successful_configuration']['longhorn_upgrade_id']='foreign'
                elif defect=='source':s.source.identity={'cluster_uid':'changed'}
                elif defect=='runtime':row['new_runtime']['managers']=['unverified']
                elif defect=='old-engine':s.source.get.side_effect=lambda *a,**kw:None
                elif defect=='source-data':s.workloads['plain'].data.side_effect=ValueError('bytes changed')
                elif defect=='backup-uid':objects['backup-plain']['metadata']['uid']='replacement'
                elif defect=='backup-error':objects['backup-plain']['status']['error']='failed'
                elif defect=='snapshot-request':objects['snapshot-plain']['spec']['volume']='foreign'
                elif defect=='system-uid':system['metadata']['uid']='replacement'
                elif defect=='system-version':system['status']['version']='v1.12.1'
                elif defect=='bundle':s.store.mc.return_value='{}'
                elif defect=='restore-hash':row['restores']['v1-plain-before']['confirmed_sha256']='changed'
                elif defect=='restore-write':row['restores']['v1-plain-before']['independent_write_sha256']=''
                elif defect=='restore-source':row['restores']['v1-plain-before']['volume_uid']='uid-plain'
                elif defect=='encrypted-header':row['restores']['v1-encrypted-before']['encrypted_device']={'uuid':'foreign'}
                elif defect=='target-key':key['metadata']['uid']='replacement'
                elif defect=='class':sc['parameters']['dataEngine']='v1'
                elif defect=='transport':s.store.transport_listener.return_value=None
                elif defect=='target-volume':
                    original=s.target.get.side_effect;s.target.get.side_effect=lambda kind,*a,**kw:{'items':[{'metadata':{'uid':'foreign'}}]} if kind=='volumes.longhorn.io' else original(kind,*a,**kw)
                s.path.write_text(json.dumps(row));guards=self.guards(s,row)
                # Runtime must come from actual observed state, not a modified journal.
                actual={'version':'1.12.1','managers':['new']}
                with guards[0],guards[1],patch.object(V1.upgrade,'runtime_identity',return_value=actual):
                    with self.assertRaises(ValueError):s.resume_preflight()
                s.source.run_requests.assert_not_called();s.store.qualify.assert_not_called();s.store.cleanup_transport.assert_not_called()

    def test_real_remaining_tail_keeps_original_order_and_finishes_both_after_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            s,row,*_=self.fixture(directory);trace=[];s.row=row
            s.product=Mock(side_effect=lambda *a,**kw:trace.append(('downgrade-refusal',kw['refusal'])))
            s.protect=Mock(side_effect=lambda name,label,config:trace.append(('backup',name,label)) or name+'-'+label)
            s.independent_restore=Mock(side_effect=lambda key:trace.append(('restore',key)))
            encryption=SimpleNamespace(host_probe=Mock(return_value=row['old_encrypted_device']))
            guards=self.guards(s,row)
            with guards[0],guards[1],patch.object(V1.upgrade,'upgrade_v1_engine',side_effect=lambda lab,w,payload:trace.append(('engine',w.journal()['volume']))),redirect_stdout(io.StringIO()):
                s.finish_manager_upgrade({},encryption,s.workloads['encrypted'].journal(),verified=True)
            self.assertEqual([('engine','plain'),('engine','encrypted'),('backup','plain','after'),('restore','plain-after'),
                ('backup','encrypted','after'),('restore','encrypted-after'),('downgrade-refusal','unsupported Longhorn upgrade or direct downgrade')],trace)
            self.assertEqual('business-pass',json.loads(s.path.read_text())['stage'])
            s.store.prepare.assert_not_called();s.store.qualify.assert_not_called()
