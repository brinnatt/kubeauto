"""V1 sequencing, no-downgrade setup and independent cross-engine recovery."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v1_upgrade_contract', ROOT/'tests/helpers/longhorn-v1-upgrade-lifecycle.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class V1UpgradeContracts(unittest.TestCase):
    def test_fixture_registers_v1_module_before_store_consumes_its_owner(self):
        source = SimpleNamespace(__name__='v1_upgrade_fixture_unit', load_helper=Mock(return_value='backups'))
        target = SimpleNamespace(); installer = SimpleNamespace(fixture=Mock(return_value=(None, target)))
        modules = {'longhorn-lab.py': source, 'longhorn-v2-dr-store.py': 'shared', 'longhorn-v2-dr-target-install.py': installer}
        with patch.object(MODULE.upgrade, 'load', side_effect=modules.__getitem__), patch.dict(MODULE.sys.modules):
            self.assertEqual((source, target, 'backups', 'shared'), MODULE.fixture())
            self.assertIs(source, MODULE.sys.modules[source.__name__])

    def fixture(self, directory):
        source_dir = Path(directory)/'source'; target_dir = Path(directory)/'target'
        source_dir.mkdir(); target_dir.mkdir()
        def lab(path, nodes, engine, cluster):
            (path/'config.yml').write_text(yaml.safe_dump({'longhorn_data_engines': [engine]}))
            return SimpleNamespace(DIRECTORY=path, NODES=nodes, get=Mock(return_value={'items': []}),
                owner=Mock(return_value={'cluster_uid': cluster}), installation_health=Mock(),
                execute=Mock(return_value=SimpleNamespace(stdout='[]')), supervise_core=Mock(),
                write_private=lambda path, data: path.write_text(json.dumps(data)))
        source = lab(source_dir, MODULE.SOURCE_NODES, 'v1', 'source')
        target = lab(target_dir, ('192.168.47.149',), 'v2', 'target')
        source.backup_store = Mock(return_value=SimpleNamespace())
        backups = SimpleNamespace(BACKUP_RESOURCES=('backups.longhorn.io',))
        shared = SimpleNamespace(Consumer=Mock(return_value=SimpleNamespace()))
        return source, target, MODULE.V1Upgrade(source, target, backups, shared, 11, 12)

    def test_old_setup_rejects_existing_release_or_crd_before_configuration(self):
        for defect in ('release', 'crd', 'engine', 'host'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, _, _ = self.fixture(directory)
                if defect == 'release': source.execute.return_value.stdout = '[{"chart":"longhorn-1.12.1"}]'
                if defect == 'crd': source.get.return_value = {'items': [{'metadata': {'name': 'volumes.longhorn.io'}}]}
                if defect == 'engine': (source.DIRECTORY/'config.yml').write_text('longhorn_data_engines: [v1, v2]')
                if defect == 'host': source.NODES = ('192.168.47.149',)
                expected = (source.DIRECTORY/'config.yml').read_bytes()
                with self.assertRaises(ValueError): MODULE.configure_old(source, 11)
                source.supervise_core.assert_not_called()
                self.assertEqual(expected, (source.DIRECTORY/'config.yml').read_bytes())

    def test_clean_old_setup_uses_supervised_product_and_actual_version(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, _ = self.fixture(directory)
            with patch.object(MODULE.upgrade, 'release', return_value={'app_version': '1.12.0'}), redirect_stdout(io.StringIO()):
                MODULE.configure_old(source, 11)
            source.supervise_core.assert_called_once_with(11, 'longhorn-setup')
            config = yaml.safe_load((source.DIRECTORY/'config.yml').read_text())
            self.assertEqual('1.12.0', config['longhorn_version']); self.assertEqual('', config['longhorn_upgrade_id'])

    def test_source_engine_default_matches_product_and_rejects_invalid_values(self):
        for value in ('omitted', ['v1'], ['v2'], ['v1', 'v2'], None, 'v1'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                source, _, scenario = self.fixture(directory)
                config = {} if value == 'omitted' else {'longhorn_data_engines': value}
                (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
                valid = value == 'omitted' or value == ['v1']
                def release(lab): return {'app_version': '1.12.0' if lab is source else '1.12.1', 'status': 'deployed'}
                with patch.object(MODULE.upgrade, 'release', side_effect=release), redirect_stdout(io.StringIO()):
                    if valid:
                        scenario.validate_baseline(); MODULE.configure_old(source, 11)
                        source.supervise_core.assert_called_once()
                    else:
                        with self.assertRaises(ValueError): scenario.validate_baseline()
                        with self.assertRaises(ValueError): MODULE.configure_old(source, 11)
                        source.supervise_core.assert_not_called()

    def test_baseline_requires_independent_pinned_empty_source_and_target(self):
        for defect in ('', 'same-cluster', 'source-engine', 'target-engine', 'old-target', 'foreign-volume', 'journal'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory)
                if defect == 'same-cluster': target.owner.return_value = {'cluster_uid': 'source'}
                if defect == 'source-engine': (source.DIRECTORY/'config.yml').write_text('longhorn_data_engines: [v2]')
                if defect == 'target-engine': (target.DIRECTORY/'config.yml').write_text('longhorn_data_engines: [v1]')
                if defect == 'foreign-volume': source.get.return_value = {'items': [{'metadata': {'name': 'foreign'}}]}
                if defect == 'journal': scenario.path.write_text('{}')
                def release(lab): return {'app_version': '1.12.0' if lab is source or defect == 'old-target' else '1.12.1', 'status': 'deployed'}
                with patch.object(MODULE.upgrade, 'release', side_effect=release):
                    if defect:
                        with self.assertRaises(ValueError): scenario.validate_baseline()
                    else: scenario.validate_baseline()
                source.supervise_core.assert_not_called(); target.supervise_core.assert_not_called()

    def test_business_orders_backups_prechecks_manager_then_native_engines_then_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, scenario = self.fixture(directory); order = []
            scenario.validate_baseline = Mock()
            scenario.store = SimpleNamespace(prepare=Mock(), qualify=Mock(), product_configuration=Mock(return_value={}))
            scenario.consumer = SimpleNamespace(initialize=Mock(), qualify=Mock())
            scenario.workloads = {name: SimpleNamespace(prepare=Mock(), journal=Mock(return_value={'volume': name}), data=Mock())
                                  for name in ('plain', 'encrypted')}
            source.get.return_value = {'spec': {'dataEngine': 'v1'}}
            source.WORKERS = source.NODES[-3:]
            source.load_helper = Mock(return_value=SimpleNamespace(Probe=Mock(return_value=SimpleNamespace(
                host_probe=Mock(return_value={'luks_uuid': 'same', 'raw_bytes': 33, 'mapped_bytes': 17})))))
            scenario.protect = Mock(side_effect=lambda name, phase, _: order.append('backup-'+name+'-'+phase) or name+'-'+phase)
            scenario.independent_restore = Mock(side_effect=lambda key: order.append('restore-'+key))
            def system(_): scenario.row['system'] = {'name': 'system'}; order.append('system-backup')
            scenario.system_backup = Mock(side_effect=system)
            scenario.product = Mock(side_effect=lambda changes, **kw: order.append('refusal' if kw else 'manager'))
            def native(_, workload, payload): order.append('native-engine')
            with patch.object(MODULE.upgrade, 'retained_identity', side_effect=lambda lab, _: {'cluster': 'source' if lab is source else 'target'}), \
                    patch.object(MODULE.upgrade, 'runtime_identity', return_value={'actual': True}), \
                    patch.object(MODULE.upgrade, 'release', return_value={'app_version': '1.12.1'}), \
                    patch.object(MODULE.upgrade, 'upgrade_v1_engine', side_effect=native), redirect_stdout(io.StringIO()) as output:
                scenario.business()
            self.assertLess(order.index('restore-encrypted-before'), order.index('system-backup'))
            self.assertLess(order.index('system-backup'), order.index('refusal'))
            self.assertLess(order.index('refusal'), order.index('manager'))
            self.assertLess(order.index('manager'), order.index('native-engine'))
            self.assertLess(max(i for i, event in enumerate(order) if event == 'native-engine'), order.index('backup-plain-after'))
            self.assertEqual('refusal', order[-1]); self.assertEqual(4, scenario.independent_restore.call_count)
            self.assertEqual('business-pass', scenario.row['stage'])
            self.assertNotIn('LONGHORN_UPGRADE_PASS', output.getvalue())
            self.assertNotIn('detach', order)

    def test_v1_encrypted_protection_reuses_proven_header_aware_receipt(self):
        for name in ('plain', 'encrypted'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source, _, scenario = self.fixture(directory)
                scenario.row = {'payloads': {name: 'confirmed'}, 'points': {}}
                journal = {'volume': 'pvc-owned'}
                scenario.workloads = {name: SimpleNamespace(journal=Mock(return_value=journal), data=Mock(return_value='digest'))}
                scenario.store = SimpleNamespace(backup_receipt=Mock(return_value={'incremental': False}))
                encryption = SimpleNamespace(backup_receipt=Mock(return_value={
                    'incremental': False, 'size': 2*1024**3, 'stored_size': 2*1024**3+16*1024**2, 'encrypted': True}))
                product = SimpleNamespace(check_request=Mock(), request_object=Mock())
                source.load_helper = Mock(side_effect=lambda path: encryption if path == 'longhorn-encryption.py' else product)
                requests = []
                source.run_requests = Mock(side_effect=lambda pid, rows, **kw: requests.extend(rows))
                def get(kind, request_name, namespace):
                    obj = {'metadata': {'uid': request_name+'-uid'}}
                    if kind == 'backups.longhorn.io':
                        obj['status'] = {'state': 'Completed', 'progress': 100,
                            'url': 's3://bucket@us-east-1/?volume=pvc-owned&backup='+requests[1]['name']}
                    return obj
                source.get.side_effect = get
                key = scenario.protect(name, 'before', {'longhorn_backup_target': 's3://bucket@us-east-1/'})
                self.assertEqual(name+'-before', key)
                point = scenario.row['points'][key]
                if name == 'encrypted':
                    encryption.backup_receipt.assert_called_once_with(source, scenario.store, journal, point['name'], 2*1024**3)
                    scenario.store.backup_receipt.assert_not_called()
                    self.assertEqual(2*1024**3+16*1024**2, point['receipt']['stored_size'])
                else:
                    encryption.backup_receipt.assert_not_called()
                    scenario.store.backup_receipt.assert_called_once_with('pvc-owned', point['name'], 2*1024**3)
                self.assertEqual('digest', point['sha256']); self.assertTrue(point['backup_uid'])

    def test_restore_checks_v2_encryption_and_independent_binding_before_data(self):
        for defect in ('', 'engine', 'encryption', 'source-identity', 'ancestry', 'luks-identity'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory)
                point = {'name': 'backup', 'volume': 'source-volume', 'url': 's3://point', 'payload': 'confirmed', 'sha256': 'old-digest'}
                device = {'luks_uuid': 'same', 'raw_bytes': 33, 'mapped_bytes': 17}
                scenario.row = {'points': {'encrypted-before': point}, 'old_encrypted_device': device}
                scenario.consumer = SimpleNamespace(receipt=Mock(return_value={'product_binding': {}}))
                scenario.encrypted_target_class = Mock(return_value='isolated-key-class')
                scenario.workloads = {'encrypted': SimpleNamespace(journal=Mock(return_value={'volume_uid': 'source-uid'}), data=Mock())}
                row = {'volume': 'restored', 'volume_uid': 'new-uid'}
                restored = SimpleNamespace(prepare=Mock(), journal=Mock(return_value=row), data=Mock(return_value='digest'),
                    detach=Mock(), mount=Mock(), cleanup=Mock())
                volume = {'spec': {'dataEngine': 'v2', 'encrypted': True}}
                if defect == 'engine': volume['spec']['dataEngine'] = 'v1'
                if defect == 'encryption': volume['spec']['encrypted'] = False
                if defect == 'source-identity': row['volume_uid'] = 'source-uid'
                if defect == 'ancestry': volume['status'] = {'cloneStatus': {'sourceVolume': 'old'}}
                target.get.return_value = volume; target.verify_restore_capacity = Mock()
                target.WORKERS = ('192.168.47.149',)
                target.load_helper = Mock(return_value=SimpleNamespace(Probe=Mock(return_value=SimpleNamespace(
                    host_probe=Mock(return_value=dict(device, luks_uuid='other') if defect == 'luks-identity' else device)))))
                target.wait = Mock(return_value={'metadata': {'uid': 'discovered'}})
                dr = SimpleNamespace(sync_target=Mock())
                with patch.object(MODULE.upgrade, 'load', return_value=dr), patch.object(MODULE.upgrade, 'Workload', return_value=restored), redirect_stdout(io.StringIO()):
                    if defect:
                        with self.assertRaises(ValueError): scenario.independent_restore('encrypted-before')
                        restored.data.assert_not_called(); restored.cleanup.assert_not_called()
                    else:
                        scenario.independent_restore('encrypted-before')
                        restored.prepare.assert_called_once_with('isolated-key-class', point)
                        self.assertEqual(3, restored.data.call_count)
                        scenario.workloads['encrypted'].data.assert_called_once_with('confirmed', read=True)
                        restored.detach.assert_called_once(); restored.mount.assert_called_once_with('recovery-remount', 0)
                        restored.cleanup.assert_called_once()
                        receipt = scenario.row['restores']['v1-encrypted-before']
                        self.assertTrue(receipt['encrypted']); self.assertEqual('v2', receipt['target_engine'])

    def test_incomplete_upgrade_cleanup_retains_original_journal_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, scenario = self.fixture(directory)
            row = {'stage': 'manager-upgraded-engine-pending'}; scenario.path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, 'incomplete upgrade'): scenario.cleanup()
            self.assertEqual(row, json.loads(scenario.path.read_text())); source.get.assert_not_called()

    def test_cleanup_streams_shared_progress_with_only_owning_v1_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, scenario = self.fixture(directory)
            def clean(_):
                print('nested cleanup progress', flush=True)
                print('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS source_version=1.12.1', flush=True)
            with patch.object(MODULE.upgrade.Upgrade, 'cleanup', clean), redirect_stdout(io.StringIO()) as output:
                scenario.cleanup()
            self.assertIn('nested cleanup progress', output.getvalue())
            self.assertIn('LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS', output.getvalue())
            self.assertNotIn('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS', output.getvalue())


if __name__ == '__main__': unittest.main()
