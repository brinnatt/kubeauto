"""Static contract for the first-hop final runner."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "upgrade/lab/final-regression.sh"


class FinalRegressionContractTest(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text()

    def test_admission_precedes_any_disposable_cluster_mutation(self):
        admission = self.text.index("FINAL_PREREQUISITE_CHECK")
        destroy = self.text.index('for cluster in "$SOURCE" "$TARGET"')
        source_setup = self.text.index('source-preflight.sh')
        self.assertLess(admission, destroy)
        self.assertLess(admission, source_setup)

    def test_scope_is_fixed_to_the_two_disposable_clusters(self):
        self.assertIn('SOURCE=upgrade-v1336-source', self.text)
        self.assertIn('TARGET=upgrade-v13413-clean', self.text)
        self.assertNotIn('192.168.122.1\n', self.text)
        self.assertIn('UPGRADE_FINAL_COMPLETE_PLAN_PASS', self.text)

    def test_success_requires_cleanup_marker_and_durable_zero(self):
        self.assertIn('LAB_CLEAN_VERIFY_PASS', self.text)
        self.assertIn('UPGRADE_FINAL_SCOPE_PASS', self.text)
        self.assertIn("printf '0\\n' > \"$FINALIZED_FILE\"", self.text)
        self.assertIn('! -e "$BASE/clusters/$SOURCE"', self.text)
        self.assertIn('! -e "$BASE/clusters/$TARGET"', self.text)

    def test_source_admission_uses_v1336_pack_before_setup_and_restores_target(self):
        source_pack = self.text.index('k8s_bin_v1.33.6.tar')
        source_setup = self.text.index('source-preflight.sh')
        restore = self.text.index('mv "$TARGET_BIN" "$BASE/kube-bin"', source_setup)
        self.assertLess(source_pack, source_setup)
        self.assertLess(source_setup, restore)
        self.assertIn('K8S_VER: "1\\.34\\.13"', self.text)

    def test_source_upgrade_resumes_node_stage_before_final_version_assertion(self):
        first = self.text.index('kubecli.py" upgrade "$SOURCE"')
        second = self.text.index('kubecli.py" upgrade "$SOURCE"', first + 1)
        final_assertion = self.text.index('FINAL_SOURCE_UPGRADE_PASS')
        self.assertLess(first, second)
        self.assertLess(second, final_assertion)

    def test_metrics_repair_is_scoped_and_keeps_local_data_protection(self):
        self.assertIn('repair_metrics_server_emptydir', self.text)
        self.assertIn('UPGRADE_FINAL_METRICS_REPAIR_PASS', self.text)
        self.assertIn('k8s-app=metrics-server', self.text)
        self.assertIn('expected exactly one metrics-server pod', self.text)
        self.assertNotIn('--delete-emptydir-data', self.text)
        self.assertIn('cannot delete Pods with local storage', self.text)
        self.assertIn('MAX_METRICS_REPAIRS=6', self.text)
        self.assertIn('metrics_repairs=$((metrics_repairs + 1))', self.text)
        self.assertIn('while :; do', self.text)


if __name__ == '__main__':
    unittest.main()
