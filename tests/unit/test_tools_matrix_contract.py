"""Contracts for the independent tools matrix and runner boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tests.helpers.validate_tools_test_matrix import validate_matrix


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "tools-test-matrix.yaml"
RUNNER = ROOT / "tests" / "run_tools_regression.sh"


class ToolsMatrixContractTests(unittest.TestCase):
    def test_tools_matrix_is_consistent_and_review_pending(self):
        self.assertEqual(validate_matrix(MATRIX), [])
        text = MATRIX.read_text(encoding="utf-8")
        self.assertIn("status: review", text)
        self.assertIn("pass: 39", text)
        self.assertIn("pending: 16", text)

    def test_tools_matrix_require_pass_rejects_review_baseline(self):
        errors = validate_matrix(MATRIX, require_pass=True)
        self.assertTrue(any("non-pass" in error for error in errors))

    def test_public_function_inventory_is_fully_mapped_to_cases(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        inventory = data["functional_inventory"]
        self.assertEqual(set(inventory), {
            "NetCheckCli", "OvpnUserCli", "CalicoPolicyCli", "KubeBackupCli",
            "KubePublishCli", "KafkaCli", "MigrationCli", "MyBackupCli", "StarCli",
        })
        case_ids = {case["id"] for case in data["test_cases"]}
        capabilities = [cap for entries in inventory.values() for cap in entries]
        self.assertGreaterEqual(len(capabilities), 80)
        self.assertTrue(all(cap["case_ids"] and set(cap["case_ids"]) <= case_ids for cap in capabilities))

    def test_stale_summary_is_rejected(self):
        text = MATRIX.read_text(encoding="utf-8").replace("pending: 16", "pending: 15", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tools.yaml"
            path.write_text(text, encoding="utf-8")
            errors = validate_matrix(path)
        self.assertTrue(any("coverage_summary.pending" in error for error in errors))

    def test_runner_is_a_separate_review_gated_entrypoint(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("validate_tools_test_matrix.py", text)
        self.assertIn("TOOLS_MATRIX_APPROVED", text)
        self.assertIn("--build-only", text)
        self.assertIn("TOOLS_BUILD_EXIT", text)
        self.assertIn("flock -n 9", text)
        self.assertIn("run-durable-gate.sh", text)
        self.assertIn("TOOLS_CALICO_EXIT", text)
        self.assertIn("--kube-backup-live", text)
        self.assertIn("TOOLS_KUBE_BACKUP_EXIT", text)
        self.assertIn('env PYTHON="$PY" bash', text)
        self.assertIn("KUBE_PUBLISH_TOOL=/tmp/KubePublishCli.py", text)
        self.assertNotIn("run_enterprise_regression.sh", text)

    def test_kube_backup_live_fixture_is_scoped_and_exercises_the_cli(self):
        fixture = (ROOT / "tests/helpers/kube-backup-live-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("root@192.168.122.243", fixture)
        forbidden_host = "192.168.122." + "1"
        self.assertNotIn(forbidden_host, fixture)
        self.assertIn("KUBE_BACKUP_LIVE_REGRESSION_PASS", fixture)
        self.assertIn("KUBE_BACKUP_CLEAN_VERIFY_PASS", fixture)
        self.assertIn("--include-crds", fixture)
        self.assertIn("--namespace-mapping", fixture)
        self.assertIn("--merge-patch-kind", fixture)
        self.assertIn("'configmap kb-config'", fixture)

    def test_rocky8_build_repairs_python_expat_abi_before_pip(self):
        build = (ROOT / "tests" / "helpers" / "build-tools-rocky8.sh").read_text(
            encoding="utf-8"
        )
        install = build.index("dnf install -y expat expat-devel")
        upgrade = build.index("dnf upgrade -y expat expat-devel")
        probe = build.index('python3.12 -c "import pyexpat"')
        self.assertLess(install, upgrade)
        self.assertLess(upgrade, probe)

    def test_calico_interface_input_is_constrained_before_manifest_generation(self):
        source = (ROOT / "tools/k8stools/CalicoPolicyCli.py").read_text(encoding="utf-8")
        self.assertIn("仅允许真实网卡名", source)
        self.assertIn("含非法网卡名", source)

    def test_calico_cases_distinguish_the_live_gate_from_kdd_architecture(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in data["test_cases"]}
        calico = [case for case in cases.values() if case["tool"] == "CalicoPolicyCli"]
        self.assertTrue(calico)
        self.assertEqual(cases["TL-CAL-07"]["status"], "pass")
        self.assertEqual(cases["TL-CAL-08"]["status"], "na")

    def test_calico_live_fixture_uses_supported_delete_arguments(self):
        fixture = (ROOT / "tests/helpers/calico-live-regression.sh").read_text(encoding="utf-8")
        self.assertNotIn('--delete-hostendpoints --hep-prefix', fixture)
        self.assertIn('CALICO_LIVE_REGRESSION_PASS', fixture)
        self.assertIn('registry.talkschool.cn:5000/brinnatt/busybox:1.37', fixture)
        self.assertNotIn('image: busybox:', fixture)
        self.assertIn('--dry-run=client', fixture)
        self.assertIn('--dry-run=server', fixture)


if __name__ == "__main__":
    unittest.main()
