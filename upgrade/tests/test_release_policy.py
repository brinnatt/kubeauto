"""Release admission must remain closed without complete, current acceptance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upgrade.product import entrypoint, release_policy
from upgrade.product.launcher import UpgradeAdmissionRefused
from upgrade.tests import test_launcher as launcher_fixture

class ReleasePolicyContracts(unittest.TestCase):
    def test_incomplete_fixture_cannot_create_customer_policy(self):
        # The project matrix can legitimately reach GA. Refusal must use an
        # isolated pending matrix, never depend on the delivery still pending.
        fixture = launcher_fixture.LauncherContracts(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        output = fixture.root/'policy.json'
        with self.assertRaisesRegex(UpgradeAdmissionRefused, 'UPG-03'):
            release_policy.export(fixture.root, fixture.matrix, output)
        self.assertFalse(output.exists())

    def test_packaged_admission_requires_actual_data_closure_and_exact_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root/'upgrade/product'; product.mkdir(parents=True)
            (product/'node.yml').write_text('fixture tasks\n')
            (product/'containerd-config-v3.toml').write_text('version = 3\n')
            version = root/'upgrade/v1.35.10'; version.mkdir()
            (version/'artifact-pins.json').write_text('{"fixture":true}\n')
            policy = {'schema_version':1,'source':'v1.34.13','target':'v1.35.10',
                'accepted_ids':release_policy.ACCEPTANCE_IDS,
                'runtime_data':release_policy.runtime_data(root)}
            path = root/'policy.json'; path.write_text(json.dumps(policy))
            self.assertEqual(release_policy.admit_packaged(root,path),policy)
            (product/'node.yml').write_text('changed fixture\n')
            with self.assertRaisesRegex(UpgradeAdmissionRefused,'differs'):
                release_policy.admit_packaged(root,path)
            (product/'node.yml').unlink()
            with self.assertRaises(UpgradeAdmissionRefused):release_policy.admit_packaged(root,path)
            path.unlink()
            with self.assertRaisesRegex(UpgradeAdmissionRefused,'missing'):
                release_policy.admit_packaged(root,path)

    def test_frozen_customer_entry_refuses_missing_policy_before_runner(self):
        with patch.object(entrypoint.sys,'frozen',True,create=True), \
             patch.object(entrypoint,'admit_packaged',side_effect=UpgradeAdmissionRefused('missing policy')), \
             patch.object(entrypoint,'run_next_hop') as runner:
            with self.assertRaisesRegex(UpgradeAdmissionRefused,'missing policy'):
                entrypoint.run_minor_upgrade(object(),'customer')
            runner.assert_not_called()

    def test_source_entry_never_falls_back_when_matrix_is_missing(self):
        with patch.object(entrypoint,'DEFAULT_MATRIX',Path('/nonexistent-upgrade-matrix')), \
             patch.object(entrypoint,'run_next_hop') as runner:
            with self.assertRaisesRegex(RuntimeError,'admission matrix'):
                entrypoint.run_minor_upgrade(object(),'customer')
            runner.assert_not_called()

    def test_actual_manager_does_not_route_missing_admission_to_legacy_playbook(self):
        from service.cluster.manager import ClusterManager
        manager=ClusterManager.__new__(ClusterManager)
        with patch.object(manager,'_validate_for_setup'),patch.object(manager,'_show_component_versions'), \
             patch('service.cluster.manager.confirm_action',return_value=True), \
             patch.object(entrypoint,'DEFAULT_MATRIX',Path('/nonexistent-upgrade-matrix')), \
             patch.object(manager,'_run_playbook') as legacy:
            with self.assertRaisesRegex(RuntimeError,'admission matrix'):
                manager.cluster_command('customer','upgrade')
            legacy.assert_not_called()
