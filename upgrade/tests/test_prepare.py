"""Source-boundary preparation: refusal, exclusive ownership and scoped cleanup."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from upgrade.product import coordinator, prepare
from upgrade.product.kubernetes_preflight import Refused
from upgrade.tests import test_continuity as fixture


class PrepareContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ContinuityContracts(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.save()
        self.root = self.fixture.root
        self.cluster = 'customer-source'
        cluster = self.root/'clusters'/self.cluster
        cluster.mkdir(parents=True)
        (cluster/'config.yml').write_text('K8S_VER: 1.35.10\nCONTAINER_RUNTIME: containerd\n')
        # kubectl fixture binds the canonical config path, independent of this
        # manager's directory. A symlink is adequate for this TLS-free fixture.
        (cluster/'kubectl.kubeconfig').symlink_to(self.root/'config')
        self.manager = SimpleNamespace(clusters_dir=self.root/'clusters', kube_bin_dir=self.root,
            _validate_for_setup=Mock(), _yaml_to_dict=lambda path: __import__('yaml').safe_load(path.read_text()),
            _run_playbook=Mock(return_value=SimpleNamespace(rc=0)))
        self.request = {k: v for k, v in self.fixture.contract.items()
                        if k in {'namespace','service','pvc','client_selector','ledger_sha256'}}
        self.request_path = self.root/'request.json'
        self.request_path.write_text(json.dumps(self.request))
        self.state = self.root/('kubeauto-upgrade-'+self.cluster)
        self.observed = {'cluster_identity': {'namespace': 'kube-system','uid':'cluster-uid'},
                         'nodes':[{'name':'node','uid':'node-uid'}],
                         'api_backends':[{'server':'https://master-one:6443'}]}

    def run_prepare(self, *, recovery=None):
        def recover(manager, cluster, variables, observed, state):
            if recovery:
                return recovery(state)
            snapshot=state/'snapshot.db';snapshot.write_bytes(b'offline test snapshot')
            snapshot.chmod(0o600)
            return {'path':str(snapshot),'sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest()}
        # Only the source/API inventory and snapshot backend are isolated here;
        # actual kubectl subprocesses prove the Service/PVC contract. Separate
        # etcd tests and live gates own real backup and restoration evidence.
        original = prepare.continuity_contract
        def continuity(binary, config, server, request):
            return original(binary, self.root/'config', server, request)
        actual_continuity = prepare.Continuity
        with patch.object(coordinator, '_target_observer', return_value=self.observed), \
             patch.object(prepare, 'recovery_point', side_effect=recover), \
             patch.object(prepare, 'continuity_contract', side_effect=continuity), \
             patch.object(prepare, 'Continuity', side_effect=lambda b,c,s,v:actual_continuity(b,self.root/'config',s,v)):
            return prepare.prepare_next_hop(self.manager,self.cluster,self.request_path,state_root=self.root)

    def test_preparation_binds_read_only_ledger_private_source_and_snapshot(self):
        receipt=self.run_prepare()
        self.assertEqual(receipt['continuity'],self.fixture.contract)
        self.assertEqual(receipt['source_observation_sha256'],
                         coordinator._sha256(self.state/'source-observation.json'))
        self.assertEqual(self.state.stat().st_mode & 0o777,0o700)
        for path in self.state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        lease=Path(str(self.state)+'.lease')
        coordinator._verify_source_lease(source='v1.35.10',target='v1.36.6',cluster=self.cluster,
            lease=lease,receipt_path=Path(str(self.state)+'.receipt.json'))
        before={p.name:p.read_bytes() for p in self.state.iterdir()}
        with self.assertRaisesRegex(Refused,'already exists'):self.run_prepare()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.state.iterdir()})

    def test_failed_backup_removes_only_this_attempts_owned_local_state(self):
        foreign=self.root/'foreign';foreign.write_text('keep')
        def fail(state):
            (state/'partial-snapshot').write_text('partial')
            raise Refused('fixture backup failure')
        with self.assertRaisesRegex(Refused,'backup failure'):self.run_prepare(recovery=fail)
        self.assertFalse(self.state.exists())
        self.assertFalse(Path(str(self.state)+'.lease').exists())
        self.assertFalse(Path(str(self.state)+'.receipt.json').exists())
        self.assertEqual(foreign.read_text(),'keep')

    def test_foreign_boundary_and_invalid_source_refuse_before_backup(self):
        self.state.mkdir(mode=0o700)
        (self.state/'foreign-journal').write_text('keep')
        with self.assertRaisesRegex(Refused,'already exists'):self.run_prepare()
        self.manager._run_playbook.assert_not_called()
        self.assertEqual((self.state/'foreign-journal').read_text(),'keep')
        (self.state/'foreign-journal').unlink();self.state.rmdir()
        (self.manager.clusters_dir/self.cluster/'config.yml').write_text('K8S_VER: 1.33.6\n')
        with self.assertRaisesRegex(Refused,'exact source'):self.run_prepare()
        self.assertFalse(self.state.exists())
        self.manager._run_playbook.assert_not_called()

    def test_existing_upgrade_owner_blocks_before_coordinator_or_remote_call(self):
        self.run_prepare()
        lease=Path(str(self.state)+'.lease')
        with lease.open('r+') as stream,patch.object(coordinator,'_run_hop') as run:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Keep the real outer flock implementation, changing only /var/tmp
            # placement to this deterministic local fixture.
            actual=coordinator.Path
            def path(value):return self.root if value=='/var/tmp' else actual(value)
            with patch.object(coordinator,'Path',side_effect=path):
                with self.assertRaisesRegex(coordinator.CoordinatorRefused,'another preparation'):
                    coordinator.run_next_hop(self.manager,self.cluster)
            run.assert_not_called()

    def test_cli_validates_options_before_manager_and_routes_prepare_separately(self):
        from controller.cluster.cli import KubeautoCLI
        with patch('controller.cluster.cli.ClusterManager') as manager:
            for args in [argparse.Namespace(cluster='x',prepare=True,continuity=None),
                         argparse.Namespace(cluster='x',prepare=False,continuity='contract.json')]:
                with self.assertRaisesRegex(ValueError,'supplied together'):
                    KubeautoCLI._handle_upgrade(object(),args)
            manager.assert_not_called()
            with patch('upgrade.product.entrypoint.prepare_minor_upgrade') as call:
                KubeautoCLI._handle_upgrade(object(),argparse.Namespace(cluster='x',prepare=True,continuity='contract.json'))
                call.assert_called_once_with(manager.return_value,'x','contract.json')
                manager.return_value.cluster_command.assert_not_called()
