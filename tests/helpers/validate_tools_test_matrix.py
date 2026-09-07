#!/usr/bin/env python3
"""Validate the standalone tools delivery matrix before any live mutation."""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
ALLOWED_STATUSES = {"pass", "pending", "fail", "skip", "na"}
REQUIRED_CASE_FIELDS = {"id", "tool", "category", "scenario", "command", "expected", "status"}
REQUIRED_FUNCTION_FIELDS = {"id", "operation", "case_ids"}
FORBIDDEN_LOCAL_ROOTS = {"common", "controller", "model", "service", "tools"}
EXPECTED_TOOLS = {
    "NetCheckCli": ROOT / "tools/NetCheckCli.py",
    "OvpnUserCli": ROOT / "tools/OvpnUserCli.py",
    "CalicoPolicyCli": ROOT / "tools/k8stools/CalicoPolicyCli.py",
    "KubeBackupCli": ROOT / "tools/k8stools/KubeBackupCli.py",
    "KubePublishCli": ROOT / "tools/k8stools/KubePublishCli.py",
    "KafkaCli": ROOT / "tools/kafka/KafkaCli.py",
    "MigrationCli": ROOT / "tools/mysqltools/MigrationCli.py",
    "MyBackupCli": ROOT / "tools/mysqltools/MyBackupCli.py",
    "StarCli": ROOT / "tools/starrocks/StarCli.py",
}
ALLOWED_122_ADDRESSES = {
    "192.168.122.2", "192.168.122.217", "192.168.122.243",
    "192.168.122.246", "192.168.122.193", "192.168.122.210",
    "192.168.122.216",
}


def _cases(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "id" in value and "status" in value:
            found.append(value)
        else:
            for child in value.values():
                found.extend(_cases(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_cases(child))
    return found


def _import_boundary_errors() -> list[str]:
    errors: list[str] = []
    spec = ROOT / "tools-onefile.spec"
    try:
        spec_text = spec.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"tools-onefile.spec cannot be read: {exc}")
    else:
        if "pathex=[str(script_path.parent)]" not in spec_text:
            errors.append("tools-onefile.spec must isolate PyInstaller pathex per script")
    tool_files = sorted((ROOT / "tools").rglob("*.py"))
    expected_paths = set(EXPECTED_TOOLS.values())
    if set(tool_files) != expected_paths:
        errors.append(
            "tool inventory mismatch: "
            f"expected={sorted(str(p.relative_to(ROOT)) for p in expected_paths)} "
            f"actual={sorted(str(p.relative_to(ROOT)) for p in tool_files)}"
        )
    for path in tool_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            errors.append(f"{path.relative_to(ROOT)} cannot parse: {exc}")
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                root = name.lstrip(".").split(".", 1)[0]
                if root in FORBIDDEN_LOCAL_ROOTS or name.lstrip(".") in {"kubecli", "runtime_hook"}:
                    errors.append(f"{path.relative_to(ROOT)} imports forbidden project module {name}")
    return errors


def _lab_address_errors() -> list[str]:
    """Reject unapproved 192.168.122.* hosts, including the forbidden .1."""
    errors: list[str] = []
    address_pattern = re.compile(r"192\.168\.122\.\d{1,3}")
    # AGENTS.md intentionally names the forbidden .1 address; scan executable
    # test assets only so the policy itself can document the prohibition.
    roots = (ROOT / "tests",)
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix in {".pyc", ".log"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for address in sorted(set(address_pattern.findall(text))):
                if address not in ALLOWED_122_ADDRESSES:
                    errors.append(f"{path.relative_to(ROOT)} uses unapproved lab host {address}")
    return errors


def validate_matrix(path: Path, require_pass: bool = False) -> list[str]:
    errors: list[str] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"cannot parse matrix: {exc}"]
    if not isinstance(data, dict):
        return ["matrix root must be a mapping"]
    meta = data.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta must be a mapping")
    elif meta.get("runner") != "tests/run_tools_regression.sh":
        errors.append("meta.runner must be tests/run_tools_regression.sh")
    cases = _cases(data.get("test_cases"))
    if not cases:
        errors.append("test_cases must contain at least one case")
    seen: set[str] = set()
    for case in cases:
        missing = REQUIRED_CASE_FIELDS - set(case)
        if missing:
            errors.append(f"{case.get('id', '<unknown>')} missing fields: {sorted(missing)}")
        case_id = str(case.get("id", ""))
        if case_id in seen:
            errors.append(f"duplicate test id: {case_id}")
        seen.add(case_id)
        status = str(case.get("status", "")).lower()
        if status not in ALLOWED_STATUSES:
            errors.append(f"{case_id}: invalid status {status!r}")
        if case.get("tool") not in EXPECTED_TOOLS and case.get("tool") != "cross-tool":
            errors.append(f"{case_id}: unknown tool {case.get('tool')!r}")
        for key in ("command", "expected", "scenario"):
            if not str(case.get(key, "")).strip():
                errors.append(f"{case_id}: {key} must not be empty")

    tools_in_cases = {str(c.get("tool")) for c in cases if c.get("tool") != "cross-tool"}
    missing_tools = sorted(set(EXPECTED_TOOLS) - tools_in_cases)
    if missing_tools:
        errors.append(f"matrix has no test case for tool(s): {missing_tools}")
    required_categories = {"build", "cli", "functional", "security", "recovery"}
    for tool in sorted(set(EXPECTED_TOOLS) & tools_in_cases):
        categories = {str(c.get("category")) for c in cases if c.get("tool") == tool}
        missing_categories = sorted(required_categories - categories)
        if missing_categories:
            errors.append(f"{tool}: missing required categories {missing_categories}")
    inventory = data.get("functional_inventory")
    if not isinstance(inventory, dict):
        errors.append("functional_inventory must be a mapping of tool to public capabilities")
    else:
        case_by_id = {str(c.get("id")): c for c in cases}
        inventory_ids: set[str] = set()
        for tool, capabilities in inventory.items():
            if tool not in EXPECTED_TOOLS:
                errors.append(f"functional_inventory has unknown tool {tool!r}")
                continue
            if not isinstance(capabilities, list) or not capabilities:
                errors.append(f"functional_inventory.{tool} must contain capabilities")
                continue
            for capability in capabilities:
                if not isinstance(capability, dict):
                    errors.append(f"functional_inventory.{tool} contains a non-mapping capability")
                    continue
                missing = REQUIRED_FUNCTION_FIELDS - set(capability)
                cap_id = str(capability.get("id", "<unknown>"))
                if missing:
                    errors.append(f"{cap_id} missing fields: {sorted(missing)}")
                    continue
                if cap_id in inventory_ids:
                    errors.append(f"duplicate functional capability id: {cap_id}")
                inventory_ids.add(cap_id)
                case_ids = capability.get("case_ids")
                if not isinstance(case_ids, list) or not case_ids:
                    errors.append(f"{cap_id}: case_ids must contain at least one executable case")
                    continue
                referenced = []
                for case_id in case_ids:
                    case = case_by_id.get(str(case_id))
                    if case is None:
                        errors.append(f"{cap_id}: unknown case id {case_id!r}")
                    else:
                        referenced.append(case)
                if referenced and not any(c.get("category") in {"cli", "functional", "security", "recovery"} for c in referenced):
                    errors.append(f"{cap_id}: capability must be covered by cli/functional/security/recovery case")
        missing_inventory_tools = sorted(set(EXPECTED_TOOLS) - set(inventory))
        if missing_inventory_tools:
            errors.append(f"functional_inventory has no entries for tool(s): {missing_inventory_tools}")
    summary = data.get("coverage_summary")
    if not isinstance(summary, dict):
        errors.append("coverage_summary must be a mapping")
    else:
        counts = Counter(str(c.get("status", "")).lower() for c in cases)
        for key, value in (("total", len(cases)), ("pass", counts["pass"]),
                           ("pending", counts["pending"]), ("fail", counts["fail"]),
                           ("skip", counts["skip"]), ("na", counts["na"])):
            if summary.get(key) != value:
                errors.append(f"coverage_summary.{key}: declared={summary.get(key)!r}, calculated={value}")
        overall = str(summary.get("overall_assessment", ""))
        match = re.search(r"(\d+)\s*/\s*(\d+)", overall)
        if match and tuple(map(int, match.groups())) != (counts["pass"], len(cases)):
            errors.append("coverage_summary.overall_assessment pass/total is stale")
        if require_pass and any(c.get("status") != "pass" for c in cases):
            errors.append("matrix contains non-pass item(s); tools delivery PASS is prohibited")
    errors.extend(_import_boundary_errors())
    errors.extend(_lab_address_errors())
    return errors


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--require-pass"):
        print(f"usage: {argv[0]} MATRIX.yaml [--require-pass]", file=sys.stderr)
        return 2
    errors = validate_matrix(Path(argv[1]), require_pass=len(argv) == 3)
    if errors:
        print(f"TOOLS_MATRIX_VALIDATION_FAIL path={argv[1]}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    data = yaml.safe_load(Path(argv[1]).read_text(encoding="utf-8"))
    print(f"TOOLS_MATRIX_VALIDATION_PASS path={argv[1]} total={len(_cases(data['test_cases']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
