"""Release admission must remain closed without complete, current acceptance."""
import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from upgrade.product import entrypoint, release_policy
from upgrade.product.launcher import UpgradeAdmissionRefused
from upgrade.tests import test_launcher as launcher_fixture
from upgrade import validate

class ReleasePolicyContracts(unittest.TestCase):
    def complete_release_fixture(self):
        fixture = launcher_fixture.LauncherContracts(methodName='runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.data['meta'].update(source=release_policy.SOURCE, target=release_policy.TARGET)
        release = fixture.matrix.parent/'release.yaml'
        import yaml
        combination = yaml.safe_load(release.read_text())
        combination.update(source_kubernetes=release_policy.SOURCE, target_kubernetes=release_policy.TARGET)
        release.write_text(yaml.safe_dump(combination))
        evidence = fixture.root/'upgrade/private-fixture'; evidence.mkdir()
        (evidence/'run.log').write_text('FIXTURE_PASS\nLAB_CLEAN_VERIFY_PASS\n')
        for name in ['exit', 'finalized']:(evidence/name).write_text('0\n')
        def reference(path):
            return {'path':str(path.relative_to(fixture.root)),
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        official = fixture.root/'upgrade/official.txt'
        for case in fixture.data['cases']:
            case.update(status='pass', subcase_expansion_complete=True, subcases=['isolated-boundary'],
                        evidence='upgrade/private-fixture/'+case['id']+'.json', success_marker='FIXTURE_PASS')
            value = {'schema_version':1, 'accepted':True, 'source':release_policy.SOURCE,
                     'target':release_policy.TARGET, 'case_ids':[case['id']],
                     'method':'live' if case['id'] in validate.LIVE_CASES else 'static',
                     'claim':'Isolated release packaging fixture; no project acceptance',
                     'covered_subcases':['isolated-boundary'],
                     'inputs':{'upgrade/official.txt':reference(official)['sha256']},
                     'references':[reference(official)], 'checks':[{'command':'fixture','rc':0}],
                     'run_id':'isolated-fixture', 'durable_rc':0, 'finalized_rc':0, 'failure_markers':0,
                     'success_marker':'FIXTURE_PASS', 'cleanup_marker':'LAB_CLEAN_VERIFY_PASS',
                     'business_assertions':['isolated-fixture'], 'log':reference(evidence/'run.log'),
                     'durable_exit':reference(evidence/'exit'), 'durable_finalized':reference(evidence/'finalized')}
            (fixture.root/case['evidence']).write_text(json.dumps(value))
        fixture.save()
        product = fixture.root/'upgrade/product'; product.mkdir()
        (product/'entrypoint.py').write_text('accepted = True\n')
        (product/'node.yml').write_text('[]\n')
        (product/'containerd-config-v3.toml').write_text('version=3\n')
        target = fixture.root/('upgrade/'+release_policy.TARGET); target.mkdir()
        (target/'artifact-pins.json').write_text('{}\n')
        for name in ['kubecli.py','runtime_hook.py','build.py','kubecli-onefile.spec','requirements']:
            (fixture.root/name).write_text('accepted fixture\n')
        return fixture, evidence, target/'release-policy.json'

    def test_clean_checkout_build_uses_exported_policy_without_private_evidence(self):
        fixture, evidence, policy = self.complete_release_fixture()
        release_policy.export(fixture.root, fixture.matrix, policy)
        shutil.rmtree(evidence)
        # Reproduces the former spec failure through actual matrix admission.
        with self.assertRaises(UpgradeAdmissionRefused):
            release_policy.export(fixture.root, fixture.matrix, fixture.root/'must-not-exist.json')
        self.assertEqual(release_policy.admit_build(fixture.root, fixture.matrix, policy)['accepted_ids'],
                         release_policy.ACCEPTANCE_IDS)
        (fixture.root/'upgrade/product/entrypoint.py').write_text('unaccepted = True\n')
        with self.assertRaisesRegex(UpgradeAdmissionRefused, 'build inputs'):
            release_policy.admit_build(fixture.root, fixture.matrix, policy)

    def test_build_policy_refuses_changed_matrix_or_missing_policy(self):
        fixture, evidence, policy = self.complete_release_fixture()
        release_policy.export(fixture.root, fixture.matrix, policy)
        fixture.matrix.write_text(fixture.matrix.read_text()+'\n# changed after acceptance\n')
        with self.assertRaisesRegex(UpgradeAdmissionRefused, 'matrix'):
            release_policy.admit_build(fixture.root, fixture.matrix, policy)
        policy.unlink()
        with self.assertRaises(UpgradeAdmissionRefused):
            release_policy.admit_build(fixture.root, fixture.matrix, policy)

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
            version = root/'upgrade/v1.36.6'; version.mkdir()
            (version/'artifact-pins.json').write_text('{"fixture":true}\n')
            policy = {'schema_version':1,'source':'v1.35.10','target':'v1.36.6',
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
