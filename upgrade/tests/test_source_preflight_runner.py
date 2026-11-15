"""Static contract for the fixed source admission runner."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'upgrade/lab/source-preflight.sh'


class SourcePreflightRunnerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_shell_and_owned_topology(self):
        result = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for host in ('192.168.47.131', '192.168.47.132', '192.168.47.133',
                     '192.168.47.134', '192.168.47.135', '192.168.47.136'):
            self.assertIn(host, self.text)
        self.assertNotIn('192.168.122.1', self.text)

    def test_refuses_foreign_state_and_emits_durable_source_markers(self):
        for token in ('[[ ! -e "$LEASE" ]]', '[[ ! -e "$BASE/clusters/$CLUSTER" ]]',
                      'UPGRADE_SOURCE_PREFLIGHT_FAIL', 'UPGRADE_SOURCE_PREFLIGHT_PASS',
                      "'kind': 'etcd-v3-snapshot'", "'mutation': 'none-after-setup'"):
            self.assertIn(token, self.text)
        self.assertIn('LOG=${STATE}.log', self.text)
        self.assertIn('exec > >(tee -a "$LOG") 2>&1', self.text)
        self.assertIn('kubecli backup "$CLUSTER"', self.text)
        self.assertIn('chmod 0600 "$LEASE" "$RECEIPT"', self.text)

    def test_failure_cleanup_is_limited_to_owned_cluster_and_state(self):
        self.assertIn('kubecli destroy "$CLUSTER"', self.text)
        self.assertIn('rm -rf "$BASE/clusters/$CLUSTER" "$STATE"', self.text)
        self.assertNotIn('kubecli destroy "k8s-dev"', self.text)


if __name__ == '__main__':
    unittest.main()
