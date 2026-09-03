#!/usr/bin/env python3
"""Validate delivery-matrix detail statuses against its coverage summary."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


STATUS_FIELDS = {
    "pass": "pass",
    "pending": "pending",
    "partial": "partial",
    "open": "open",
    "fail": "fail",
    "skip": "skip",
    "na": "na",
}


def _items(value: Any) -> list[dict[str, Any]]:
    """Recursively collect test items, including nested tier groups."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "id" in value and "status" in value:
            found.append(value)
        else:
            for child in value.values():
                found.extend(_items(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_items(child))
    return found


def _all_matrix_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    sources = (
        data.get("tier0_foundation"),
        data.get("tier1_commands"),
        data.get("tier1_cross_scenarios"),
        data.get("tier1_jumper"),
        data.get("tier2_matrix"),
        data.get("prometheus_delivery"),
        data.get("tier3_tools"),
    )
    items: list[dict[str, Any]] = []
    for source in sources:
        items.extend(_items(source))
    return items


def _assert_equal(errors: list[str], label: str, actual: int, expected: int) -> None:
    if actual != expected:
        errors.append(f"{label}: declared={actual}, calculated={expected}")


def validate_matrix(path: Path, require_pass: bool = False) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ["matrix root must be a mapping"]
    summary = data.get("coverage_summary")
    if not isinstance(summary, dict):
        return ["coverage_summary must be a mapping"]

    errors: list[str] = []
    tier_sources = {
        "tier0": (data.get("tier0_foundation"),),
        "tier1": (data.get("tier1_commands"),),
        "tier2": (data.get("tier2_matrix"), data.get("prometheus_delivery")),
        "tier3": (data.get("tier3_tools"),),
    }
    supplemental_sources = {
        "tier1_cross": data.get("tier1_cross_scenarios"),
        "tier1_jumper": data.get("tier1_jumper"),
    }
    all_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tier, sources in tier_sources.items():
        tier_items: list[dict[str, Any]] = []
        for source in sources:
            tier_items.extend(_items(source))
        all_items.extend(tier_items)
        counts = Counter(str(item.get("status", "")).lower() for item in tier_items)
        unknown = sorted(set(counts) - set(STATUS_FIELDS))
        if unknown:
            errors.append(f"{tier}: unknown statuses={unknown}")
        _assert_equal(errors, f"coverage_summary.{tier}_total", int(summary.get(f"{tier}_total", -1)), len(tier_items))
        for status, field_status in STATUS_FIELDS.items():
            field = f"{tier}_{field_status}"
            if field in summary:
                _assert_equal(errors, f"coverage_summary.{field}", int(summary[field]), counts.get(status, 0))

    for name, source in supplemental_sources.items():
        supplemental_items = _items(source)
        all_items.extend(supplemental_items)
        counts = Counter(str(item.get("status", "")).lower() for item in supplemental_items)
        unknown = sorted(set(counts) - set(STATUS_FIELDS))
        if unknown:
            errors.append(f"{name}: unknown statuses={unknown}")
        for status, field_status in STATUS_FIELDS.items():
            field = f"{name}_{field_status}"
            if field in summary:
                _assert_equal(errors, f"coverage_summary.{field}", int(summary[field]), counts.get(status, 0))

    for item in all_items:
        item_id = str(item["id"])
        if item_id in seen_ids:
            errors.append(f"duplicate test id: {item_id}")
        seen_ids.add(item_id)

    # A final delivery declaration must describe exactly the same item set.
    overall = str(summary.get("overall_assessment", ""))
    declaration = re.search(r"(\d+)\s*/\s*(\d+)", overall)
    if declaration:
        declared_pass, declared_total = map(int, declaration.groups())
        calculated_pass = sum(
            str(item.get("status", "")).lower() == "pass" for item in all_items
        )
        _assert_equal(errors, "overall_assessment.pass", declared_pass, calculated_pass)
        _assert_equal(errors, "overall_assessment.total", declared_total, len(all_items))
    elif overall and require_pass:
        errors.append("overall_assessment has no numeric pass/total declaration")

    if require_pass and any(str(item.get("status", "")).lower() != "pass" for item in all_items):
        errors.append("matrix contains non-pass item(s); delivery PASS is prohibited")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--require-pass"):
        print(f"usage: {argv[0]} MATRIX.yaml [--require-pass]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        errors = validate_matrix(path, require_pass=len(argv) == 3)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"MATRIX_VALIDATION_FAIL path={path}: {exc}", file=sys.stderr)
        return 1
    if errors:
        print(f"MATRIX_VALIDATION_FAIL path={path}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    summary = data["coverage_summary"]
    print(
        "MATRIX_VALIDATION_PASS "
        f"path={path} total={len(_all_matrix_items(data))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
