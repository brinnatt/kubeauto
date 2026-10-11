import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.helpers import ceph_case_evidence as evidence


ROOT = Path(__file__).resolve().parents[2]


class CephCaseEvidenceTests(unittest.TestCase):
    def test_python_dependency_closure_does_not_follow_unrelated_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.py"
            path.write_text("PIN = 1\ndef shared():\n    return PIN\ndef owner():\n    return shared()\ndef other():\n    return False\n")
            before = evidence.python_dependencies(path, ["owner"])
            path.write_text(path.read_text().replace("return False", "return True"))
            self.assertEqual(before, evidence.python_dependencies(path, ["owner"]))
            path.write_text(path.read_text().replace("PIN = 1", "PIN = 2"))
            self.assertNotEqual(before, evidence.python_dependencies(path, ["owner"]))

    def test_shell_dependency_closure_excludes_unrelated_module(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.sh"
            path.write_text("set -eu\nshared() {\n  echo safe\n}\nowner() {\n  shared\n}\nother() {\n  echo other\n}\n")
            before = evidence.bash_dependencies(path, ["owner"])
            self.assertEqual(set(before), {"globals", "owner", "shared"})
            path.write_text(path.read_text().replace("echo other", "echo revised"))
            self.assertEqual(before, evidence.bash_dependencies(path, ["owner"]))
            path.write_text(path.read_text().replace("echo safe", "echo unsafe"))
            self.assertNotEqual(before, evidence.bash_dependencies(path, ["owner"]))

    def test_every_real_case_has_an_owning_function(self):
        data = yaml.safe_load((ROOT / "tests/ceph-test-matrix.yaml").read_text())
        self.assertEqual(set(evidence.OWNERS), {case["id"] for case in data["cases"]})
        for owner in evidence.OWNERS.values():
            evidence.bash_dependencies(ROOT / "tests/helpers/ceph-regression.sh", [owner])

    def fixture(self, root, text, rc="0\n0\n"):
        (root / "tests").mkdir()
        (root / "logs").mkdir()
        matrix = root / "tests/ceph-test-matrix.yaml"
        content = (ROOT / "tests/ceph-test-matrix.yaml").read_text()
        content = re.sub(r"(?m)^    status: (?:pass|fail)$", "    status: pending", content)
        content = re.sub(r"(?m)^(    - \{id: CEPH-OS-[^\n]*?, status: )(?:pass|fail)(, evidence: )", r"\1pending\2", content)
        content = re.sub(r"(?m)^  pass: \d+$", "  pass: 0", content)
        content = re.sub(r"(?m)^  pending: \d+$", "  pending: 28", content)
        matrix.write_text(content)
        log = root / "logs/live.log"
        log.write_text(text)
        state = root / "logs/exit"
        state.write_text(rc)
        return matrix, log, state

    def test_readonly_module_publishes_progress_without_delivery_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_STATIC_CONTRACT_PASS\nCEPH_STATIC_EXIT rc=0\n")
            before = {"CEPH-01": {"pin": "same"}}
            with patch.object(evidence, "source_dependencies", return_value=before["CEPH-01"]):
                evidence.publish(root, log, state, before, "static-1", ["CEPH-01"], readonly=True)
            data = yaml.safe_load(matrix.read_text())
            self.assertEqual(data["cases"][0]["status"], "pass")
            self.assertEqual(data["coverage_summary"]["pass"], 1)
            self.assertEqual(data["coverage_summary"]["pending"], 27)
            self.assertEqual(data["meta"]["result"], "PENDING")
            record = json.loads((root / "logs/ceph-cases/static-1/CEPH-01.json").read_text())
            self.assertEqual(record["cleanup"], "read-only-no-fixtures")

    def test_dependency_audit_returns_only_the_changed_case_to_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_STATIC_CONTRACT_PASS\nCEPH_SUPPLY_CHAIN_PASS\nCEPH_STATIC_EXIT rc=0\n")
            before = {scope: {"owner": scope} for scope in ("CEPH-01", "CEPH-02")}
            with patch.object(evidence, "source_dependencies", side_effect=lambda _, scope: before[scope]):
                evidence.publish(root, log, state, before, "two-modules", list(before), readonly=True)
                self.assertEqual(evidence.audit_matrix(root), [])
            changed = {**before, "CEPH-01": {"owner": "new"}}
            with patch.object(evidence, "source_dependencies", side_effect=lambda _, scope: changed[scope]):
                self.assertEqual(evidence.audit_matrix(root), ["CEPH-01"])
            data = yaml.safe_load(matrix.read_text())
            self.assertEqual([row["status"] for row in data["cases"][:2]], ["pending", "pass"])
            self.assertEqual(data["coverage_summary"]["pass"], 1)
            self.assertEqual(data["coverage_summary"]["pending"], 27)
            self.assertTrue((root / "logs/ceph-cases/two-modules/CEPH-01.json").is_file())

    def test_baseline_module_cannot_satisfy_the_full_cleanup_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_SLOW_OSD_FIXED_PASS\nCEPH_MODULE_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            before = {"CEPH-13": {"product": "same"}}
            with patch.object(evidence, "source_dependencies", return_value=before["CEPH-13"]):
                with self.assertRaisesRegex(ValueError, "cleanup"):
                    evidence.publish(root, log, state, before, "wrong-boundary", ["CEPH-13"])
                evidence.publish(root, log, state, before, "baseline", ["CEPH-13"], baseline=True)
            self.assertEqual(yaml.safe_load(matrix.read_text())["meta"]["result"], "PENDING")

    def test_os_audit_invalidates_only_the_changed_profile_and_retains_its_original_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, _ = self.fixture(root, "clean completed OS evidence")
            scopes = ["CEPH-OS-ROCKY8", "CEPH-OS-UBUNTU22"]
            dependencies = {scope: {"owner": scope} for scope in scopes}
            for scope in scopes:
                path = root / "logs" / (scope + ".json")
                path.write_text(json.dumps(dict(id=scope, dependencies=dependencies[scope], log="logs/live.log",
                    log_sha256=evidence.digest(log.read_bytes()), durable_rc=0, failure_markers=0)))
                binding = {"path": str(path.relative_to(root)), "sha256": evidence.digest(path.read_bytes())}
                pattern = r"(?m)^(    - \{id: " + scope + r",.*?, status: )pending(, evidence: ).*(\})$"
                matrix.write_text(re.sub(pattern, lambda match: match[1] + "pass" + match[2] + json.dumps(binding) + match[3], matrix.read_text()))
            before = yaml.safe_load(matrix.read_text())["os_qualification"]["profiles"][0]["evidence"]
            with patch.object(evidence, "source_dependencies", side_effect=lambda _, scope:
                              {"owner": "changed"} if scope == scopes[0] else dependencies[scope]):
                self.assertEqual(evidence.audit_matrix(root), [scopes[0]])
            rows = {row["id"]: row for row in yaml.safe_load(matrix.read_text())["os_qualification"]["profiles"]}
            self.assertEqual(rows[scopes[0]]["status"], "pending")
            self.assertEqual(rows[scopes[0]]["evidence"], before)
            self.assertEqual(rows[scopes[1]]["status"], "pass")

    def test_failure_missing_marker_changed_source_and_unclean_state_cannot_publish(self):
        valid = "CEPH_SLOW_OSD_FIXED_PASS\nCEPH_CLEAN_VERIFY_PASS\nLAB_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n"
        for fault in ("failure", "marker", "source", "exit", "cleanup", "readonly"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                text = valid + ("CEPH_STAGE_FAIL id=CEPH-13\n" if fault == "failure" else "")
                if fault == "marker":
                    text = text.replace("CEPH_SLOW_OSD_FIXED_PASS", "WRONG_MARKER")
                if fault == "cleanup":
                    text = text.replace("LAB_CLEAN_VERIFY_PASS", "UNCLEAN")
                matrix, log, state = self.fixture(root, text, "1\n1\n" if fault == "exit" else "0\n0\n")
                original = matrix.read_text()
                before = {"CEPH-13": {"pin": "before"}}
                current = {"pin": "after" if fault == "source" else "before"}
                with patch.object(evidence, "source_dependencies", return_value=current):
                    with self.assertRaises(ValueError):
                        evidence.publish(root, log, state, before, "bad", ["CEPH-13"], readonly=fault == "readonly")
                self.assertEqual(matrix.read_text(), original)

    def test_progress_from_focused_run_cannot_claim_full_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_SLOW_OSD_FIXED_PASS\nCEPH_CLEAN_VERIFY_PASS\nLAB_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            original = matrix.read_text()
            with self.assertRaisesRegex(ValueError, "full-run"):
                evidence.publish(root, log, state, {}, "focused", ["CEPH-13"], full=True)
            self.assertEqual(matrix.read_text(), original)

    def test_full_prerequisite_requires_its_completed_owner_and_ignores_unrelated_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_SLOW_OSD_FIXED_PASS\nCEPH_MODULE_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            before = {"CEPH-13": {"owner": "same"}}
            with patch.object(evidence, "source_dependencies", return_value=before["CEPH-13"]):
                with self.assertRaisesRegex(ValueError, "required"):
                    evidence.require_modules(root, ["CEPH-13"])
                evidence.publish(root, log, state, before, "slow-owner", ["CEPH-13"], baseline=True)
                evidence.require_modules(root, ["CEPH-13"])
                # An unrelated row may still be pending; it does not invalidate CEPH-13.
                self.assertEqual(yaml.safe_load(matrix.read_text())["cases"][1]["status"], "pending")
            with patch.object(evidence, "source_dependencies", return_value={"owner": "changed"}):
                with self.assertRaisesRegex(ValueError, "required"):
                    evidence.require_modules(root, ["CEPH-13"])

    def test_full_publication_rechecks_os_qualification_before_writing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = ("CEPH_CASE_LEDGER_PASS cases=28\nCEPH_DELIVERY_PASS cases=28\n"
                    "CEPH_CLEAN_VERIFY_PASS\nLAB_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            matrix, log, state = self.fixture(root, text)
            original = matrix.read_text()
            with self.assertRaisesRegex(ValueError, "current OS qualification"):
                evidence.publish(root, log, state, {}, "full-without-os", list(evidence.OWNERS), full=True)
            self.assertEqual(matrix.read_text(), original)
            self.assertFalse((root / "logs/ceph-cases/full-without-os").exists())

    def test_completed_publication_only_marks_present_owning_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            matrix, log, state = self.fixture(root, "CEPH_SLOW_OSD_FIXED_PASS\nCEPH_MODULE_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            before = {scope: {"owner": scope} for scope in ("CEPH-13", "CEPH-14")}
            with patch.object(evidence, "source_dependencies", side_effect=lambda _, scope: before[scope]):
                evidence.publish(root, log, state, before, "completed-owner", list(before), baseline=True, completed=True)
            rows = {row["id"]: row for row in yaml.safe_load(matrix.read_text())["cases"]}
            self.assertEqual(rows["CEPH-13"]["status"], "pass")
            self.assertEqual(rows["CEPH-14"]["status"], "pending")

    def test_healthy_performance_cannot_publish_fault_sequence_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = ("CEPH_PERFORMANCE_BASELINE_PASS\nCEPH_POST_FAULT_INTEGRITY_PASS\n"
                    "CEPH_MODULE_CLEAN_VERIFY_PASS\nCEPH_GATE_EXIT rc=0\n")
            matrix, log, state = self.fixture(root, text)
            before = {scope: {"owner": scope} for scope in ("CEPH-24", "CEPH-25")}
            with patch.object(evidence, "source_dependencies", side_effect=lambda _, scope: before[scope]):
                evidence.publish(root, log, state, before, "healthy", list(before), baseline=True, completed=True)
            rows = {row["id"]: row for row in yaml.safe_load(matrix.read_text())["cases"]}
            self.assertEqual(rows["CEPH-25"]["status"], "pass")
            self.assertEqual(rows["CEPH-24"]["status"], "pending")

    def test_os_binding_preserves_original_and_requires_the_actual_launch_snapshot(self):
        scope = "CEPH-OS-ROCKY9"
        launch = "a" * 64
        dependencies = {"product": "same"}
        for fault in (None, "pending", "checksum", "source", "dependencies", "exit", "failure"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                matrix, _, _ = self.fixture(root, "")
                path = root / "logs/rocky9.json"
                payload = dict(id=scope, source_sha256=launch, durable_rc=0, failure_markers=0)
                if fault == "exit":
                    payload["durable_rc"] = 1
                if fault == "failure":
                    payload["failure_markers"] = 1
                raw = (json.dumps(payload) + "\n").encode()
                path.write_bytes(raw)
                data = yaml.safe_load(matrix.read_text())
                row = next(row for row in data["os_qualification"]["profiles"] if row["id"] == scope)
                row["status"] = "pending" if fault == "pending" else "pass"
                row["evidence"] = {"path": "logs/rocky9.json", "sha256": evidence.digest(raw)}
                if fault == "checksum":
                    row["evidence"]["sha256"] = "b" * 64
                # Keep the project's one-line OS row format.
                import re
                line = "    - {id: " + scope + ", os_id: rocky, status: " + row["status"] + ", evidence: " + json.dumps(row["evidence"]) + "}"
                matrix.write_text(re.sub(r"(?m)^    - \{id: " + scope + r",.*$", line, matrix.read_text()))
                original = matrix.read_text()
                actual = {"product": "changed"} if fault == "dependencies" else dependencies
                with patch.object(evidence, "source_dependencies", return_value=actual):
                    if fault:
                        with self.assertRaises(ValueError):
                            evidence.bind_os_dependencies(root, {scope: dependencies}, [scope],
                                                          "b" * 64 if fault == "source" else launch)
                        self.assertEqual(matrix.read_text(), original)
                        self.assertFalse((root / "logs/rocky9-dependencies.json").exists())
                    else:
                        evidence.bind_os_dependencies(root, {scope: dependencies}, [scope], launch)
                        bound = next(row for row in yaml.safe_load(matrix.read_text())["os_qualification"]["profiles"]
                                     if row["id"] == scope)
                        target = root / bound["evidence"]["path"]
                        self.assertEqual(json.loads(target.read_text())["dependencies"], dependencies)
                        self.assertEqual(bound["evidence"]["sha256"], evidence.digest(target.read_bytes()))
                self.assertEqual(path.read_bytes(), raw)

    def test_reviewed_os_reuse_preserves_original_and_rejects_unproven_changes(self):
        scope = "CEPH-OS-ROCKY8"
        supplemental_scope = "CEPH-OS-ROCKY9"
        markers = ["CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
                   "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_IDEMPOTENCE_PASS",
                   "CEPH_OS_RECOVERY_PASS", "CEPH_OS_PERFORMANCE_PASS", "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"]
        for fault in (None, "source", "review", "supplement", "exit", "marker", "dependency"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                matrix, _, _ = self.fixture(root, "")
                prepare = root / "roles/prepare/tasks/main.yml"
                prepare.parent.mkdir(parents=True)
                prepare.write_text("current serialized task")
                product = root / "product.py"
                product.write_text("unchanged source")
                checksum = evidence.digest(product.read_bytes())
                launch = evidence.digest((checksum + "  " + str(product) + "\n").encode())
                deps = {"product.py": checksum}
                review = dict(source_root=str(root), original_source_sha256=launch, current_source_sha256=launch,
                              original_launch_files={"product.py": checksum}, current_launch_files={"product.py": checksum},
                              prepare_sha256=evidence.digest(prepare.read_bytes()), dependencies={scope: deps})
                review_path = root / "logs/review.json"
                if fault == "review":
                    review["original_source_sha256"] = "f" * 64
                review_path.write_text(json.dumps(review))
                supplement = root / "logs/supplement.json"
                supplemental_log = root / "logs/supplement.log"
                supplemental_log.write_text("current clean OS business evidence")
                supplement.write_text(json.dumps(dict(id=supplemental_scope, durable_rc=0, failure_markers=0, source_sha256=launch,
                    dependencies=deps, log="logs/supplement.log", log_sha256=evidence.digest(supplemental_log.read_bytes()))))
                import re
                binding = {"path": "logs/supplement.json", "sha256": evidence.digest(supplement.read_bytes())}
                matrix.write_text(re.sub(r"(?m)^(    - \{id: " + supplemental_scope + r",.*?, status: )pending(, evidence: ).*(\})$",
                    lambda match: match[1] + "pass" + match[2] + json.dumps(binding) + match[3], matrix.read_text()))
                old = root / "logs/original"
                old.mkdir()
                row = next(row for row in yaml.safe_load(matrix.read_text())["os_qualification"]["profiles"] if row["id"] == scope)
                payload = dict(id=scope, os_id=row["os_id"], major=row["major"], host=row["host"],
                    client_key_type=row["client_key_type"], source_sha256=launch, durable_rc=0, failure_markers=0, markers=markers,
                    fixture_kind="real-host", secure_msgr2=True, legacy_aes_risk_accepted=True,
                    cephadm_sha256=yaml.safe_load(matrix.read_text())["versions"]["cephadm_sha256"],
                    performance={"operations": 16, "errors": 0, "p95_ms": 1, "p99_ms": 2},
                    product_command="kubecli setup fixture 08")
                original = old / (scope + ".json")
                raw = json.dumps(payload).encode()
                original.write_bytes(raw)
                log = "\n".join(marker + ("" if marker.endswith("CLEAN_VERIFY_PASS") else " id=" + scope) for marker in markers) + "\nCEPH_GATE_EXIT rc=0\n"
                (old / "live.log").write_text(log.replace("CEPH_OS_RECOVERY_PASS", "WRONG") if fault == "marker" else log)
                (old / "durable.exit").write_text("1\n1\n" if fault == "exit" else "0\n0\n")
                if fault == "source":
                    product.write_text("changed source")
                if fault == "supplement":
                    supplemental_log.write_text("tampered")
                before_matrix = matrix.read_text()
                actual = {"product.py": "changed"} if fault == "dependency" else deps
                with patch.object(evidence, "source_dependencies", return_value=actual):
                    if fault:
                        with self.assertRaises(ValueError):
                            evidence.restore_reviewed_os(root, review_path, [original], supplement)
                        self.assertEqual(matrix.read_text(), before_matrix)
                    else:
                        evidence.restore_reviewed_os(root, review_path, [original], supplement)
                        bound = next(row for row in yaml.safe_load(matrix.read_text())["os_qualification"]["profiles"] if row["id"] == scope)
                        record = json.loads((root / bound["evidence"]["path"]).read_text())
                        self.assertEqual(record["source_sha256"], launch)
                        self.assertIn("dependency_review", record)
                self.assertEqual(original.read_bytes(), raw)

    def test_historical_reconstruction_refuses_an_unmatched_launch_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "review.json"
            with self.assertRaisesRegex(ValueError, "match the original launch"):
                evidence.capture_hosts_review(ROOT, "f" * 64, target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
