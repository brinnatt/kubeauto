"""The target branch must not route a Kubernetes minor upgrade to playbook 93."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upgrade_entrypoint", ROOT / "upgrade/product/entrypoint.py")
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class EntryPointContracts(unittest.TestCase):
    def test_harbor_bridge_is_selected_on_managed_host_not_controller(self):
        manager=Mock();manager.clusters_dir=Path('/fixture/clusters')
        manager._yaml_to_dict.return_value={'HARBOR_PATH':'/var/data'}
        with patch.object(entrypoint,'_admit',side_effect=lambda runner,required:runner()):
            entrypoint.run_harbor_operation(manager,'customer',operation='upgrade',
                backup_directory='/recovery/original',bridge_package='/input/bridge.tgz',
                target_package='/input/target.tgz')
        self.assertEqual(manager._run_playbook.call_args.kwargs['extra_vars']['UPGRADE_HARBOR_PACKAGES'],
                         {'bridge':'/input/bridge.tgz','v2.16.0':'/input/target.tgz'})

    def test_harbor_static_refusal_precedes_all_remote_or_setup_callbacks(self):
        manager=Mock()
        with patch.object(entrypoint,'_admit',side_effect=ValueError('pending prerequisites')):
            with self.assertRaisesRegex(ValueError,'pending prerequisites'):
                entrypoint.run_harbor_operation(manager,'customer',operation='backup',
                    backup_directory='/recovery/harbor',ca_file='/etc/harbor/ca.pem')
        manager._validate_for_setup.assert_not_called()
        manager._run_playbook.assert_not_called()

    def test_harbor_recovery_uses_original_config_entry_without_kubernetes_admission(self):
        manager=Mock();manager.clusters_dir=Path('/fixture/clusters')
        manager._yaml_to_dict.return_value={'HARBOR_PATH':'/data/harbor','unrelated':'preserved'}
        with patch.object(entrypoint,'_admit',side_effect=AssertionError('must not block recovery')):
            entrypoint.run_harbor_operation(manager,'customer',operation='restore',
                backup_directory='/recovery/original')
        manager._validate_for_setup.assert_called_once_with('customer')
        args=manager._run_playbook.call_args
        self.assertEqual(args.args,('customer','upgrade/product/harbor.yml'))
        self.assertEqual(args.kwargs['extra_vars']['UPGRADE_HARBOR_OPERATION'],'restore')
        self.assertEqual(args.kwargs['extra_vars']['unrelated'],'preserved')
        self.assertNotIn('93',str(args))

    def test_harbor_cli_rejects_ambiguous_kubernetes_or_path_arguments(self):
        from controller.cluster.cli import KubeautoCLI
        for values in [dict(harbor='backup',prepare=True,continuity='ledger.json'),
                       dict(harbor=None,harbor_backup_dir='/recovery/harbor')]:
            with patch('controller.cluster.cli.ClusterManager') as manager:
                with self.assertRaises(ValueError):
                    KubeautoCLI._handle_upgrade(object(),SimpleNamespace(cluster='customer',**values))
                manager.assert_not_called()
        with patch('controller.cluster.cli.ClusterManager') as manager, \
             patch('upgrade.product.entrypoint.run_harbor_operation') as operation:
            KubeautoCLI._handle_upgrade(object(),SimpleNamespace(cluster='customer',harbor='restore',
                harbor_backup_dir='/recovery/original'))
            operation.assert_called_once_with(manager.return_value,'customer',operation='restore',
                backup_directory='/recovery/original',ca_file=None,bridge_package=None,target_package=None,
                transfer_directory=None)

    def test_harbor_import_requires_an_explicit_transfer_and_cleanup_remains_independent(self):
        manager=Mock();manager.clusters_dir=Path('/fixture/clusters')
        manager._yaml_to_dict.return_value={'HARBOR_PATH':'/var/data'}
        with self.assertRaisesRegex(ValueError,'transfer-dir'):
            entrypoint.run_harbor_operation(manager,'target',operation='import',
                backup_directory='/recovery/target',target_package='/input/216.tgz')
        manager._run_playbook.assert_not_called()
        with patch.object(entrypoint,'_admit',side_effect=AssertionError('must not block cleanup')):
            entrypoint.run_harbor_operation(manager,'target',operation='abort-import',
                backup_directory='/recovery/target')
        self.assertEqual(manager._run_playbook.call_args.kwargs['extra_vars']['UPGRADE_HARBOR_OPERATION'],
                         'abort-import')

    def test_pending_prerequisites_refuse_before_stage_callback(self):
        data = yaml.safe_load(entrypoint.DEFAULT_MATRIX.read_text())
        for case in data['cases']:
            case.update(status='pending', evidence=None)
        # Keep the real version directory's combination inputs, but isolate
        # mutable acceptance state from the delivered matrix and lab leases.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                dir=entrypoint.DEFAULT_MATRIX.parent) as matrix:
            yaml.safe_dump(data, matrix)
            matrix.flush()
            with patch.object(entrypoint, 'DEFAULT_MATRIX', Path(matrix.name)), \
                 patch('upgrade.product.launcher.validate.combination_errors', return_value=[]), \
                 patch.object(entrypoint, 'run_next_hop') as coordinator:
                with self.assertRaisesRegex(ValueError, 'required matrix prerequisites are not pass: UPG-01'):
                    entrypoint.run_minor_upgrade(object(), 'dev')
                coordinator.assert_not_called()

    def test_target_entrypoint_binds_no_legacy_93_playbook(self):
        calls = []

        def fake_run(path, *, runner, required):
            calls.append((path, list(required)))
            return "admitted"

        with patch.object(entrypoint, "admit_and_run", side_effect=fake_run):
            result = entrypoint.run_minor_upgrade(object(), "dev")
        self.assertEqual(result, "admitted")
        self.assertEqual(calls[0][0], entrypoint.DEFAULT_MATRIX)
        self.assertEqual(calls[0][1], ["UPG-01", "UPG-02", "UPG-03", "UPG-04", "UPG-05"])


if __name__ == "__main__":
    unittest.main()
