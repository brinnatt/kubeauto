"""The first-hop launcher must refuse before any stateful callback."""
from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upgrade_launcher", ROOT / "upgrade/product/launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherContracts(unittest.TestCase):
    def test_pending_prerequisite_refuses_before_runner(self):
        calls = []
        matrix = ROOT / "upgrade/v1.34.13/test-matrix.yaml"
        with tempfile.TemporaryDirectory(dir=ROOT / "upgrade") as temp_dir:
            temp_dir = Path(temp_dir)
            temp_matrix = temp_dir / "test-matrix.yaml"
            text = matrix.read_text()
            text = text.replace(
                "- id: UPG-07\n  group: U2\n  desc: 逐节点 drain、运行时及调度恢复\n  status: pass",
                "- id: UPG-07\n  group: U2\n  desc: 逐节点 drain、运行时及调度恢复\n  status: pending",
                1,
            )
            temp_matrix.write_text(text)
            (temp_dir / "release.yaml").symlink_to(matrix.parent / "release.yaml")
            with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, "UPG-07"):
                launcher.run(path=temp_matrix,
                             required=["UPG-01", "UPG-02", "UPG-07"],
                             runner=lambda: calls.append("mutated"))
        self.assertEqual(calls, [])

    def test_static_admission_accepts_frozen_combination_without_running_runner(self):
        snapshot = launcher.admit_static(required=["UPG-01", "UPG-02"])
        self.assertEqual(snapshot["total"], len(snapshot["cases"]))
        self.assertEqual(snapshot["pass"], sum(case["status"] == "pass"
                                                for case in snapshot["cases"]))
        self.assertEqual(snapshot["pending_or_fail"], snapshot["total"] - snapshot["pass"])

    def test_disabled_execution_refuses_after_matrix_prerequisite_check(self):
        matrix = ROOT / "upgrade/v1.34.13/test-matrix.yaml"
        text = matrix.read_text()
        try:
            matrix.write_text(text.replace("execution_enabled: true", "execution_enabled: false", 1))
            # The execution switch is checked before the runner binding.
            with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused, "execution remains disabled"):
                launcher.run(required=["UPG-01", "UPG-02"], runner=lambda: 1)
        finally:
            matrix.write_text(text)


if __name__ == "__main__":
    unittest.main()
