#!/usr/bin/env python3
"""Validate the independent Redis delivery matrix before a live gate."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

STATUSES = {"pending", "pass", "fail"}


def validate(path: Path, require_pass: bool = False) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["matrix root must be a mapping"]
    meta = data.get("meta")
    cases = data.get("cases")
    if not isinstance(meta, dict):
        errors.append("meta must be a mapping")
        meta = {}
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty list")
        return errors
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            errors.append("every case must be a mapping")
            continue
        for field in ("id", "group", "desc", "status", "evidence"):
            if not case.get(field) and field != "evidence":
                errors.append(f"case missing {field}: {case!r}")
        case_id = str(case.get("id", ""))
        if case_id in seen:
            errors.append(f"duplicate case id: {case_id}")
        seen.add(case_id)
        status = str(case.get("status", "")).lower()
        if status not in STATUSES:
            errors.append(f"{case_id}: invalid status={status}")
        if status == "pending" and case.get("evidence"):
            errors.append(f"{case_id}: pending case cannot carry evidence")
        if status == "pass" and not case.get("evidence"):
            errors.append(f"{case_id}: pass case requires evidence")
    result = str((meta or {}).get("result", "")).upper()
    if result not in {"PENDING", "PASS", "FAIL"}:
        errors.append(f"meta.result invalid: {result}")
    if result == "PASS" and any(str(c.get("status", "")).lower() != "pass" for c in cases if isinstance(c, dict)):
        errors.append("PASS matrix requires every case to be pass")
    if require_pass:
        if result != "PASS":
            errors.append("--require-pass requires meta.result=PASS")
        if any(not isinstance(c, dict) or str(c.get("status", "")).lower() != "pass" for c in cases):
            errors.append("--require-pass requires every case to be pass")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--require-pass"):
        print(f"usage: {argv[0]} MATRIX.yaml [--require-pass]", file=sys.stderr)
        return 2
    try:
        errors = validate(Path(argv[1]), require_pass=len(argv) == 3)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        print(f"REDIS_MATRIX_VALIDATION_FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        print(f"REDIS_MATRIX_VALIDATION_FAIL path={argv[1]}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"REDIS_MATRIX_VALIDATION_PASS path={argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
