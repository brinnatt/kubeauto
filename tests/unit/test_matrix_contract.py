"""Tests for delivery-matrix arithmetic and status consistency."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers.validate_test_matrix import validate_matrix


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "enterprise-test-matrix.yaml"


class MatrixContractTests(unittest.TestCase):
    def test_enterprise_matrix_is_internally_consistent(self):
        self.assertEqual(validate_matrix(MATRIX), [])

    def test_delivery_mode_rejects_pending_matrix(self):
        text = MATRIX.read_text(encoding="utf-8")
        pending = text.replace(
            "id: PROM-01, desc: \"kube-prometheus-stack 88.0.0 全新七节点 HA 安装\", status: pass",
            "id: PROM-01, desc: \"kube-prometheus-stack 88.0.0 全新七节点 HA 安装\", status: pending",
            1,
        ).replace("tier2_pass: 74", "tier2_pass: 73", 1).replace(
            "tier2_pending: 0", "tier2_pending: 1", 1
        ).replace("当前 127/127 项 PASS", "当前 126/127 项 PASS", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.yaml"
            path.write_text(pending, encoding="utf-8")
            errors = validate_matrix(path, require_pass=True)
        self.assertTrue(any("non-pass" in error for error in errors))

    def test_stale_tier2_summary_is_rejected(self):
        text = MATRIX.read_text(encoding="utf-8")
        broken = text.replace("tier2_pass: 74", "tier2_pass: 73").replace(
            "tier2_pending: 0", "tier2_pending: 1"
        ).replace("tier2_open: 0", "tier2_open: 7")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.yaml"
            path.write_text(broken, encoding="utf-8")
            errors = validate_matrix(path)
        self.assertTrue(any("tier2_pass" in error for error in errors))
        self.assertTrue(any("tier2_pending" in error for error in errors))
        self.assertTrue(any("tier2_open" in error for error in errors))

    def test_duplicate_test_id_is_rejected(self):
        text = MATRIX.read_text(encoding="utf-8")
        duplicate = text.replace("id: PROM-02", "id: PROM-01", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.yaml"
            path.write_text(duplicate, encoding="utf-8")
            errors = validate_matrix(path)
        self.assertTrue(any("duplicate test id: PROM-01" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
