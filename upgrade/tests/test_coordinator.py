"""Contract checks for the outer first-hop handoff."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from upgrade.product import coordinator


class CoordinatorContracts(unittest.TestCase):
    def test_runtime_inputs_are_checksum_bound_to_both_adjacent_edges(self):
        values = coordinator._runtime_inputs()
        self.assertEqual(set(values['UPGRADE_CONTAINERD_BUNDLES']), {'2.2.10', '2.3.7'})
        self.assertEqual(
            values['UPGRADE_CONTAINERD_BUNDLES']['2.2.10']['sha256']['runc'],
            values['UPGRADE_CONTAINERD_BUNDLES']['2.3.7']['sha256']['runc'])
        self.assertTrue(Path(values['UPGRADE_CONTAINERD_CONFIG']['path']).is_file())

    def test_missing_lease_refuses_before_product_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / 'receipt.json'
            receipt.write_text('{}')
            receipt.chmod(0o600)
            with patch.object(coordinator, 'LEASE', root / 'missing.lease'), \
                 patch.object(coordinator, 'SOURCE_RECEIPT', receipt):
                with self.assertRaisesRegex(coordinator.CoordinatorRefused, 'private file is missing'):
                    coordinator._verify_source_lease()

    def test_source_receipt_and_recovery_digest_are_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease = root / 'source.lease'; lease.write_text('owner\n'); lease.chmod(0o600)
            snapshot = root / 'snapshot.db'; snapshot.write_bytes(b'known snapshot')
            receipt = root / 'receipt.json'
            receipt.write_text(json.dumps({
                'schema_version': 1, 'source': coordinator.SOURCE,
                'target': coordinator.TARGET, 'cluster': coordinator.CLUSTER,
                'mutation': 'none-after-setup',
                'cluster_identity': {'namespace': 'kube-system', 'uid': 'cluster-uid'},
                'recovery_point': {'path': str(snapshot),
                                   'sha256': __import__('hashlib').sha256(snapshot.read_bytes()).hexdigest()},
            }))
            receipt.chmod(0o600)
            with patch.object(coordinator, 'LEASE', lease), \
                 patch.object(coordinator, 'SOURCE_RECEIPT', receipt):
                value = coordinator._verify_source_lease()
            self.assertEqual(value['source'], 'v1.33.6')
            snapshot.write_bytes(b'changed')
            with patch.object(coordinator, 'LEASE', lease), \
                 patch.object(coordinator, 'SOURCE_RECEIPT', receipt):
                with self.assertRaisesRegex(coordinator.CoordinatorRefused, 'SHA256 changed'):
                    coordinator._verify_source_lease()

    def test_entrypoint_uses_coordinator_and_never_legacy_playbook(self):
        source = Path(coordinator.__file__).read_text()
        self.assertIn('manager._run_playbook', source)
        self.assertNotIn('93.upgrade.yml', source)


if __name__ == '__main__':
    unittest.main()
