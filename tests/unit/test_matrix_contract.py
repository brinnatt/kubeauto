"""Tests for delivery-matrix arithmetic and status consistency."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tests.helpers.validate_test_matrix import validate_matrix


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "enterprise-test-matrix.yaml"


class MatrixContractTests(unittest.TestCase):
    def test_enterprise_matrix_is_internally_consistent(self):
        self.assertEqual(validate_matrix(MATRIX), [])

    def test_pending_enterprise_matrix_does_not_claim_current_pass(self):
        errors = validate_matrix(MATRIX, require_pass=True)
        if any("non-pass" in error for error in errors):
            data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
            self.assertNotRegex(data["meta"]["regression_result"], r"^\s*PASS\b")

    def test_delivery_mode_rejects_pending_matrix(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        data["prometheus_delivery"][0]["status"] = "pending"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            errors = validate_matrix(path, require_pass=True)
        self.assertTrue(any("non-pass" in error for error in errors))

    def test_stale_tier2_summary_is_rejected(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        summary = data["coverage_summary"]
        summary["tier2_pass"] -= 1
        summary["tier2_pending"] += 1
        summary["tier2_open"] = 7
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
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
