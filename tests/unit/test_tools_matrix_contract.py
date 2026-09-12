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
        self.assertIn("pass: 48", text)
        self.assertIn("pending: 7", text)

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

    def test_live_artifacts_require_ext_images_dual_push_and_digest_evidence(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        artifacts = data["meta"]["artifact_prerequisites"]
        self.assertEqual(artifacts["owner_repository"], "kubeauto-ext-images-dockerfile")
        self.assertEqual(artifacts["publication"], "GitHub Actions dual-push")
        self.assertEqual(artifacts["primary_registry"], "hub.talkedu.cn/kubeauto")
        self.assertIn("manifest digest or SHA256", artifacts["required_evidence"])
        self.assertEqual(
            artifacts["mysql_tools"]["migration"],
            [
                "hub.talkedu.cn/kubeauto/mysql:8.0.46@sha256:0b6938c55ad3ef982d41cfa3ee01a63074cd5c9f3907487badeda53f6feb14da",
                "hub.talkedu.cn/kubeauto/mysql-8.4:8.4.4@sha256:0a3e659b9fb960330299e2a1847414f6185c573a3fd2cf1320221066904ea77d",
                "hub.talkedu.cn/kubeauto/mysql-9.2:9.2.0@sha256:867954f8c74131e891c2d3501560abbb7548ab8f439245c2feb4648f99b0caeb",
            ],
        )
        prerequisites = "\n".join(data["meta"]["live_prerequisites"])
        self.assertIn("双推", prerequisites)
        self.assertIn("无动态镜像发现", prerequisites)

    def test_stale_summary_is_rejected(self):
        text = MATRIX.read_text(encoding="utf-8").replace("pending: 7", "pending: 6", 1)
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
        self.assertIn("--star-live", text)
        self.assertIn("TOOLS_STARCLI_EXIT", text)
        self.assertIn('"$STARCLI_BINARY" "$STAR_HOST:/tmp/StarCli"', text)
        self.assertIn("STARCLI_LIVE_STAGE pre-clean-verified", text)
        self.assertIn("systemctl kill --kill-who=all", text)
        self.assertIn('test "$(cat "${state}.exit")" = 0', text)
        star_branch = text[text.index("  --star-live)"):]
        self.assertNotIn('"cat \'${state}.exit\'"', star_branch)
        self.assertNotIn("run_enterprise_regression.sh", text)

    def test_starcli_live_fixture_keeps_product_entrypoint_and_negative_cleanup(self):
        fixture = (ROOT / "tests/helpers/starcli-live-regression.sh").read_text(encoding="utf-8")
        self.assertIn('STARCLI_ARCHIVE_SHA256', fixture)
        self.assertIn('--setup --root-password', fixture)
        self.assertIn('--storage-root-path "../outside"', fixture)
        self.assertIn('--root-password "$PASSWORD" --user root --group root >/tmp/starcli-invalid.out', fixture)
        self.assertIn("grep -Eqi", fixture)
        self.assertIn("不能同时", fixture)
        self.assertIn('STARCLI_LIVE_REGRESSION_PASS', fixture)
        self.assertIn('rm -f /tmp/starcli-invalid.out', fixture)

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

    def test_kafka_live_gate_persists_evidence_then_removes_all_remote_state(self):
        runner = RUNNER.read_text(encoding="utf-8")
        fixture = (ROOT / "tests/helpers/kafka-cli-multinode-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("KAFKA_FIXTURE_DIGEST=sha256:", runner)
        self.assertIn("KAFKA_FIXTURE_REPOSITORY=quay.io/strimzi/kafka", runner)
        self.assertIn('$KAFKA_FIXTURE_REPOSITORY@$KAFKA_FIXTURE_DIGEST', runner)
        self.assertNotIn('$KAFKA_FIXTURE_IMAGE@$KAFKA_FIXTURE_DIGEST', runner)
        self.assertIn('KAFKA_DURABLE_STATUS rc=$gate_rc finalized=$finalized_rc', runner)
        self.assertIn("cleanup_kafka_live() (", runner)
        self.assertIn("'${state}.pid' '${state}.exit' '${state}.finalized'", runner)
        self.assertIn("! test -e '$remote_log'", runner)
        self.assertIn("kafkacli-*.client.properties", runner)
        pre_clean = runner.index('echo "KAFKA_LIVE_STAGE pre-clean"')
        pre_clean_verified = runner.index('echo "KAFKA_LIVE_STAGE pre-clean-verified"')
        lease = runner.index('echo "KAFKA_LIVE_STAGE lease-acquire', pre_clean_verified)
        self.assertLess(pre_clean, pre_clean_verified)
        self.assertLess(pre_clean_verified, lease)
        cleanup = runner.index("cleanup_kafka_live", runner.index("grep -q '^KAFKA_CLI_MULTINODE"))
        clean_marker = runner.index("TOOLS_CLEAN_VERIFY_PASS scope=kafka", cleanup)
        self.assertLess(cleanup, clean_marker)
        self.assertIn('>"$MULTI_ROOT/broker-down.out"', fixture)
        self.assertNotIn(">/tmp/kafka-cli-multi-broker-down.out", fixture)

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
