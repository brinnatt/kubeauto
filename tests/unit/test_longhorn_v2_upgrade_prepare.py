"""V2 old-install transition owns empty storage and preserves raw/target identity."""
import copy
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
SPEC = importlib.util.spec_from_file_location('v2_upgrade_preparation_contract', ROOT/'tests/helpers/longhorn-v2-upgrade-prepare.py')
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class PreparationContracts(unittest.TestCase):
    def fixture(self, directory):
        labs = []
        for name, nodes in (('source', MODULE.SOURCE_NODES), ('target', ('192.168.47.149',))):
            path = Path(directory)/name; (path/'yml/longhorn').mkdir(parents=True)
            config = {'longhorn_version': '1.12.1', 'longhorn_data_engines': ['v2'], 'longhorn_requests': [],
                'longhorn_upgrade_id': '', 'longhorn_upgrade_system_backup': '', 'longhorn_backup_target': '',
                'longhorn_backup_secret': '', 'longhorn_install': 'yes'}
            (path/'config.yml').write_text(yaml.safe_dump(config))
            state = {'run_id': name, 'cluster_uid': name, 'namespace_uid': name+'-namespace',
                'node_uids': {node: node+'-uid' for node in nodes},
                'raw_disks': {node: {'path': '/dev/disk/by-id/'+node, 'identity': node+'-identity'} for node in nodes}}
            receipt = {'cluster_uid': name, 'namespace_uid': state['namespace_uid'], 'nodes': {
                node: {'disks': {'raw': {'disk_uuid': node+'-disk-uuid', 'disk_driver': 'aio',
                    'path': state['raw_disks'][node]['path'], 'hardware_identity': state['raw_disks'][node]['identity']}}}
                for node in nodes}}
            (path/'yml/longhorn/block-disks.json').write_text(json.dumps(receipt))
            lab = SimpleNamespace(BASE=ROOT, DIRECTORY=path, NODES=nodes, core_identity=Mock(), owner=Mock(return_value=state), execute=Mock(),
                cache_artifacts=Mock(), installation_health=Mock(), recover_installation=Mock(), restore_hosts=Mock(), prepare_credentials=Mock(),
                write_private=lambda path, row: path.write_text(json.dumps(row)))
            lab.get = Mock(side_effect=lambda kind, *args, **kwargs:
                {'spec': {'backupTargetURL': '', 'credentialSecret': ''}} if kind == 'backuptargets.longhorn.io' else {'items': []})
            labs.append(lab)
        source, target = labs
        backups = SimpleNamespace(BACKUP_RESOURCES=('backups.longhorn.io',))
        install = SimpleNamespace(volume_baseline=Mock(), capture_hosts=Mock(), quiesce_multipath=Mock())
        scenario = MODULE.Preparation(source, target, backups, install)
        scenario.baseline = SimpleNamespace(capture=Mock(return_value={'fixture': 'baseline'}),
            bind_previous=Mock(return_value={'path': 'prior', 'sha256': 'prior-sha'}),
            reference=lambda path: {'path': path.name, 'sha256': MODULE.digest(path)}, validate_transition=Mock())
        return source, target, scenario

    def methods(self):
        return patch.object(MODULE.impl, 'release', return_value={'app_version': '1.12.1', 'status': 'deployed'})

    def preflight(self, scenario):
        with self.methods(), patch.object(MODULE.impl, 'retained_identity', side_effect=lambda lab, _: {'cluster': lab.owner()['cluster_uid']}), redirect_stdout(io.StringIO()):
            scenario.preflight()

    def read_context(self):
        return patch.object(MODULE.impl, 'retained_identity', side_effect=lambda lab, _: {'cluster': lab.owner()['cluster_uid']})

    def result(self, source, target, scenario):
        row = json.loads(scenario.path.read_text()); row.update(stage='old-ready', source_namespace_uid=source.owner()['namespace_uid'])
        scenario.save(row)
        result = {'stage': 'complete', 'id': 'completed',
            'source_identity': {'cluster_uid': source.owner()['cluster_uid'], 'namespace_uid': source.owner()['namespace_uid']},
            'target_identity': {'cluster_uid': target.owner()['cluster_uid']},
            'legacy': {'refusal_verified': True, 'retired': True}}
        path = source.DIRECTORY/'longhorn-upgrade-result-completed.json'; path.write_text(json.dumps(result))
        return path

    def test_preflight_pins_full_source_configuration_raw_hardware_and_independent_target_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, scenario = self.fixture(directory); self.preflight(scenario)
            row = json.loads(scenario.path.read_text())
            self.assertEqual('preflight-complete', row['stage']); self.assertEqual(3, len(row['source_raw']))
            self.assertEqual((source.DIRECTORY/'config.yml').read_text(), row['original_config'])
            source.cache_artifacts.assert_called_once(); scenario.install.volume_baseline.assert_called_once_with(source)
            source.recover_installation.assert_not_called(); target.recover_installation.assert_not_called()

    def test_foreign_inventory_nonindependent_target_or_existing_business_is_refused_before_retirement(self):
        for defect in ('inventory', 'target', 'journal', 'backup-consumer'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory)
                if defect == 'inventory': source.NODES = ('192.168.47.145',)
                if defect == 'target': target.owner.return_value['cluster_uid'] = source.owner()['cluster_uid']
                if defect == 'journal': (source.DIRECTORY/'longhorn-backup-store.json').write_text('{}')
                if defect == 'backup-consumer': source.get.side_effect = lambda kind, *a, **kw: {'spec': {'backupTargetURL': 's3://foreign'}} if kind == 'backuptargets.longhorn.io' else {'items': []}
                with self.assertRaises(ValueError): self.preflight(scenario)
                source.recover_installation.assert_not_called()

    def test_raw_hardware_path_uuid_or_namespace_receipt_mismatch_stops_preflight(self):
        for defect in ('hardware', 'path', 'uuid', 'namespace', 'empty'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, _, scenario = self.fixture(directory); path = source.DIRECTORY/'yml/longhorn/block-disks.json'
                row = json.loads(path.read_text()); disk = row['nodes'][source.NODES[0]]['disks']['raw']
                if defect == 'hardware': disk['hardware_identity'] = 'foreign'
                if defect == 'path': disk['path'] = '/dev/foreign'
                if defect == 'uuid': disk['disk_uuid'] = ''
                if defect == 'namespace': row['namespace_uid'] = 'foreign'
                if defect == 'empty': row['nodes'][source.NODES[0]]['disks'] = {}
                path.write_text(json.dumps(row))
                with self.assertRaises(ValueError): self.preflight(scenario)
                source.recover_installation.assert_not_called()

    def test_prepare_reuses_native_empty_uninstall_host_and_credential_entries_without_disk_wipe(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, scenario = self.fixture(directory); self.preflight(scenario)
            calls = []
            source.recover_installation.side_effect = lambda pid: calls.append(('uninstall', pid))
            scenario.install.capture_hosts.side_effect = lambda lab: calls.append(('capture', lab))
            scenario.install.quiesce_multipath.side_effect = lambda lab: calls.append(('multipath', lab))
            source.prepare_credentials.side_effect = lambda: calls.append(('credentials',))
            with self.read_context(), patch.object(MODULE, 'healthy'), patch.object(MODULE.impl, 'configure_old', side_effect=lambda lab, pid: calls.append(('install-old', pid))), redirect_stdout(io.StringIO()):
                scenario.prepare(41)
            self.assertEqual(['uninstall', 'capture', 'multipath', 'credentials', 'install-old'], [row[0] for row in calls])
            self.assertEqual('old-ready', json.loads(scenario.path.read_text())['stage'])
            target.recover_installation.assert_not_called(); target.prepare_credentials.assert_not_called()

    def test_retirement_endpoint_recovery_is_between_native_uninstall_and_original_host_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, scenario = self.fixture(directory); self.preflight(scenario)
            row = self.retiring(source, scenario); calls = []
            original = source.restore_hosts
            original.side_effect = lambda: calls.append('host-restore')
            source.recover_installation.side_effect = lambda pid: (calls.append('native-uninstall'), source.restore_hosts())
            with self.read_context(), patch.object(MODULE, 'healthy'), patch.object(MODULE.impl, 'configure_old', side_effect=lambda lab, pid: (source.DIRECTORY/'yml/longhorn/block-disks.json').write_text(json.dumps(row['baseline_before']['native_block_receipt']))), \
                    patch.object(scenario, 'recover_retired_endpoint', side_effect=lambda row: calls.append('endpoint-recovery')):
                scenario.prepare(41)
            self.assertEqual(['native-uninstall', 'endpoint-recovery', 'host-restore'], calls)
            self.assertIs(original, source.restore_hosts)

    def test_failed_old_rollout_retains_installing_old_stage_and_no_automatic_config_or_data_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, scenario = self.fixture(directory); self.preflight(scenario)
            def failure(lab, pid):
                config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text()); config['longhorn_version'] = '1.12.0'
                (lab.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config)); raise RuntimeError('first rollout failure')
            with self.read_context(), patch.object(MODULE, 'healthy'), patch.object(MODULE.impl, 'configure_old', side_effect=failure), self.assertRaisesRegex(RuntimeError, 'first rollout'):
                scenario.prepare(41)
            self.assertEqual('installing-old', json.loads(scenario.path.read_text())['stage'])
            self.assertEqual('1.12.0', yaml.safe_load((source.DIRECTORY/'config.yml').read_text())['longhorn_version'])
            with self.read_context(), self.assertRaises(ValueError): scenario.finalize()

    def retiring(self, source, scenario):
        row = json.loads(scenario.path.read_text()); row['stage'] = 'retiring-empty-new'
        receipt = source.DIRECTORY/'yml/longhorn/block-disks.json'
        row['baseline_before'] = {'baseline_identity': {'namespace_uid': source.owner()['namespace_uid']},
                                 'native_block_receipt': json.loads(receipt.read_text())}
        receipt.rename(receipt.with_name('block-disks-uninstalled-'+source.owner()['namespace_uid']+'.json'))
        scenario.save(row)
        return row

    def test_retiring_stage_resumes_original_uninstall_before_capture_credentials_and_old_install(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, scenario = self.fixture(directory); self.preflight(scenario)
            original = self.retiring(source, scenario); calls = []
            source.recover_installation.side_effect = lambda pid: calls.append('uninstall')
            # Re-admission after original lvstore import restores the same disk identity.
            def configure(lab, pid):
                calls.append('install-old')
                (source.DIRECTORY/'yml/longhorn/block-disks.json').write_text(json.dumps(original['baseline_before']['native_block_receipt']))
            with self.read_context(), patch.object(MODULE, 'healthy'), patch.object(MODULE.impl, 'configure_old', side_effect=configure), redirect_stdout(io.StringIO()):
                scenario.resume_preflight(); scenario.prepare(41)
            self.assertEqual(['uninstall', 'install-old'], calls)
            self.assertEqual('old-ready', json.loads(scenario.path.read_text())['stage'])
            source.execute.assert_called(); target.recover_installation.assert_not_called()

    def test_retirement_resume_refuses_other_stage_foreign_target_config_namespace_receipt_or_business(self):
        for defect in ('stage', 'target', 'config', 'namespace', 'receipt', 'active-receipt', 'business'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory); self.preflight(scenario)
                row = self.retiring(source, scenario)
                if defect == 'stage': row['stage']='installing-old'; scenario.save(row)
                if defect == 'target': (target.DIRECTORY/'config.yml').write_text('foreign')
                if defect == 'config': (source.DIRECTORY/'config.yml').write_text('foreign')
                if defect == 'namespace': source.owner.return_value['namespace_uid']='foreign'
                if defect == 'receipt': next((source.DIRECTORY/'yml/longhorn').glob('block-disks-uninstalled-*')).write_text('{}')
                if defect == 'active-receipt': (source.DIRECTORY/'yml/longhorn/block-disks.json').write_text('{}')
                if defect == 'business': (source.DIRECTORY/MODULE.JOURNALS[0]).write_text('{}')
                with self.read_context(), patch.object(MODULE, 'healthy'), self.assertRaises(ValueError): scenario.prepare(41)
                source.recover_installation.assert_not_called(); source.prepare_credentials.assert_not_called()
                scenario.install.capture_hosts.assert_not_called()

    def test_healthy_empty_old_ready_is_reused_without_uninstall_or_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            source,target,scenario=self.fixture(directory);self.preflight(scenario)
            row=json.loads(scenario.path.read_text());row.update(stage='old-ready',source_namespace_uid=source.owner()['namespace_uid']);scenario.save(row)
            config=yaml.safe_load(row['original_config']);config['longhorn_version']='1.12.0'
            (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
            before=scenario.path.read_bytes()
            with self.read_context(),patch.object(MODULE,'healthy'),redirect_stdout(io.StringIO()):scenario.resume_preflight();scenario.prepare(41)
            self.assertEqual(before,scenario.path.read_bytes());source.recover_installation.assert_not_called()
            source.prepare_credentials.assert_not_called();scenario.install.capture_hosts.assert_not_called()

    def test_old_ready_business_or_changed_config_namespace_raw_identity_refuses_reuse(self):
        for defect in ('business','config','namespace','raw'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                source,target,scenario=self.fixture(directory);self.preflight(scenario)
                row=json.loads(scenario.path.read_text());row.update(stage='old-ready',source_namespace_uid=source.owner()['namespace_uid']);scenario.save(row)
                config=yaml.safe_load(row['original_config']);config['longhorn_version']='1.12.0'
                if defect=='config':config['longhorn_upgrade_id']='foreign'
                (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
                if defect=='business':(source.DIRECTORY/MODULE.JOURNALS[0]).write_text('{}')
                if defect=='namespace':source.owner.return_value['namespace_uid']='foreign'
                if defect=='raw':
                    path=source.DIRECTORY/'yml/longhorn/block-disks.json';raw=json.loads(path.read_text());raw['nodes'][source.NODES[0]]['disks']['raw']['disk_uuid']='foreign';path.write_text(json.dumps(raw))
                with self.read_context(),patch.object(MODULE,'healthy'),self.assertRaises(ValueError):scenario.prepare(41)
                source.recover_installation.assert_not_called();scenario.install.capture_hosts.assert_not_called()

    def test_changed_owner_target_or_config_cannot_uninstall(self):
        for defect in ('owner', 'target', 'config'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory); self.preflight(scenario)
                if defect == 'owner': source.owner.return_value['run_id'] = 'foreign'
                if defect == 'target': (target.DIRECTORY/'config.yml').write_text('foreign')
                if defect == 'config': (source.DIRECTORY/'config.yml').write_text('foreign')
                with self.read_context(), patch.object(MODULE, 'healthy'), self.assertRaises(ValueError): scenario.prepare(41)
                source.recover_installation.assert_not_called()

    def test_finalize_restores_only_owned_upgrade_inputs_after_complete_result_and_same_raw_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, scenario = self.fixture(directory); self.preflight(scenario); self.result(source, target, scenario)
            original = (source.DIRECTORY/'config.yml').read_text(); config = yaml.safe_load(original)
            config.update(longhorn_upgrade_id='owned-upgrade', longhorn_upgrade_system_backup='owned-system')
            (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
            with self.read_context(), self.methods(), redirect_stdout(io.StringIO()): scenario.finalize()
            self.assertFalse(scenario.path.exists())
            self.assertEqual(yaml.safe_load(original), yaml.safe_load((source.DIRECTORY/'config.yml').read_text()))
            self.assertTrue((source.DIRECTORY/'longhorn-v2-upgrade-preparation-complete-completed.json').is_file())
            source.recover_installation.assert_not_called(); target.recover_installation.assert_not_called()

    def test_no_completed_real_legacy_result_or_foreign_config_may_finalize(self):
        for defect in ('missing', 'legacy', 'config', 'raw'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, target, scenario = self.fixture(directory); self.preflight(scenario); path = self.result(source, target, scenario)
                if defect == 'missing': path.unlink()
                if defect == 'legacy':
                    row=json.loads(path.read_text()); row['legacy']['refusal_verified']=False; path.write_text(json.dumps(row))
                if defect == 'config':
                    config=yaml.safe_load((source.DIRECTORY/'config.yml').read_text()); config['longhorn_backup_secret']='foreign'
                    (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
                if defect == 'raw':
                    receipt=source.DIRECTORY/'yml/longhorn/block-disks.json'; row=json.loads(receipt.read_text())
                    row['nodes'][source.NODES[0]]['disks']['raw']['disk_uuid']='replaced';receipt.write_text(json.dumps(row))
                with self.read_context(), self.methods(), self.assertRaises(ValueError): scenario.finalize()
                self.assertTrue(scenario.path.exists())


if __name__ == '__main__': unittest.main()
