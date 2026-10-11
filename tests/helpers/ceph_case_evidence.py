#!/usr/bin/env python3
"""Publish completed Ceph module evidence without claiming full delivery."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml


OWNERS = {
    "CEPH-01": "verify_matrix_contract", "CEPH-02": "verify_supply_chain",
    "CEPH-03": "run_host_probe", "CEPH-04": "validate_allowlist_shape",
    "CEPH-05": "prepare_product_cluster", "CEPH-06": "verify_idempotence",
    "CEPH-07": "verify_rados_data", "CEPH-08": "deploy_business_fixtures",
    "CEPH-09": "deploy_business_fixtures", "CEPH-10": "deploy_rgw_fixture",
    "CEPH-11": "verify_security", "CEPH-12": "run_slow_vs_down",
    "CEPH-13": "run_fixed_slow_osd", "CEPH-14": "run_intermittent_slow_osd",
    "CEPH-15": "run_slow_vs_down", "CEPH-16": "run_osd_throttle",
    "CEPH-17": "run_slow_recovery", "CEPH-18": "run_scrub_contention",
    "CEPH-19": "run_bluefs_slow", "CEPH-20": "run_slow_differential",
    "CEPH-21": "run_hot_pg_differential", "CEPH-22": "run_network_differential",
    "CEPH-23": "run_slow_remediation", "CEPH-24": "run_post_fault_integrity",
    "CEPH-25": "ensure_performance_baseline", "CEPH-26": "run_upgrade",
    "CEPH-27": "verify_observability", "CEPH-28": "cleanup_business_fixtures",
}
FAILURE = re.compile(r"CEPH_(?:STAGE|OS_PROBE)_FAIL|\[FAIL\]|Traceback|^fatal:", re.M)
CONTRACT_OWNERS = {
    "CEPH-11": ["assert_s3_auth_denied"], "CEPH-26": ["assert_upgrade_complete"],
    "CEPH-27": ["assert_observability_health", "rotating_keys_pending", "assert_upgrade_complete"],
    "CEPH-16": ["assert_cgroup_iops_limit", "cgroup_io_snapshot", "throttle_phase_evidence", "assert_throttle_effect"],
    "CEPH-17": ["recovery_target", "assert_pg_activity"], "CEPH-18": ["assert_pg_activity"],
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bash_dependencies(path: Path, roots: list[str]) -> dict[str, str]:
    """Follow named shell functions conservatively, including shared globals."""
    source = path.read_text()
    matches = list(re.finditer(r"(?ms)^([a-zA-Z_]\w*)\(\) \{\n.*?^\}\n", source))
    functions = {match[1]: match[0] for match in matches}
    if not functions or any(name not in functions for name in roots):
        raise ValueError("unknown shell dependency in " + str(path))
    pending, selected = list(roots), {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        body = functions[name]
        selected[name] = digest(body.encode())
        pending.extend(set(re.findall(r"\b[A-Za-z_]\w*\b", body)) & functions.keys() - selected.keys())
    selected["globals"] = digest(source[:matches[0].start()].encode())
    return selected


def python_dependencies(path: Path, roots: list[str]) -> dict[str, str]:
    tree = ast.parse(path.read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if any(name not in functions for name in roots):
        raise ValueError("unknown Python dependency")
    pending, selected = list(roots), {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        node = functions[name]
        selected[name] = digest(ast.dump(node, include_attributes=False).encode())
        names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        pending.extend(names & functions.keys() - selected.keys())
    globals_ = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    selected["globals"] = digest("\n".join(ast.dump(node, include_attributes=False) for node in globals_).encode())
    return selected


def source_dependencies(root: Path, scope: str) -> dict[str, str]:
    """Keep status/doc/unit changes out; bind product and owning gate code."""
    if scope not in OWNERS and not scope.startswith("CEPH-OS-"):
        raise ValueError("unknown Ceph evidence scope")
    files = {"common/constants.py", "conf/config.yml"}
    if scope == "CEPH-01":
        files.update({"tests/unit/test_ceph_delivery.py", "tests/unit/test_ceph_case_evidence.py",
                      "tests/unit/test_ceph_baseline.py", "tests/unit/test_storage_documentation.py",
                      "tests/unit/test_six_repo_version_sync.py", "tests/run_enterprise_regression.sh",
                      "tests/helpers/ceph_case_evidence.py", "tests/helpers/ceph_baseline.py",
                      "tests/helpers/ceph_evidence_runner.sh", "tests/helpers/ceph_focused_baseline.sh",
                      "../kubeauto-ext-images-dockerfile/catalog.json",
                      "../kubeauto-ext-images-dockerfile/scripts/validate_catalog.py"})
    if scope != "CEPH-02":
        for directory in ("roles/ceph", "roles/prepare", "roles/docker", "roles/containerd"):
            files.update(str(path.relative_to(root)) for path in (root / directory).rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
        files.update({"service/cluster/manager.py", "service/cluster/downloader.py",
                      "common/ansible_python.py", "playbooks/08.ceph.yml", "playbooks/90.setup.yml",
                      "tests/helpers/ceph-cleanup.sh", "tests/helpers/ceph_runtime_cleanup.py",
                      "tests/helpers/ceph-host-probe.sh", "tests/helpers/kubernetes-production-smoke.sh"})
    if scope.startswith("CEPH-OS-"):
        files.update({"tests/helpers/ceph_os_qualification.py", "tests/helpers/ceph_ssh_fixture.py",
                      "tests/helpers/ceph-qualification-topology.sh"})
        owners = ["prepare_product_cluster", "wait_clean", "verify_rados_data",
                  "deploy_business_fixtures", "deploy_rgw_fixture", "cleanup_business_fixtures"]
    else:
        owners = [OWNERS[scope]]
        if scope not in {"CEPH-01", "CEPH-02"}:
            files.update({"tests/helpers/ceph_baseline.py", "tests/helpers/ceph_focused_baseline.sh"})
            owners.extend(["wait_clean", "verify_post_fault_hashes"])
        if scope in {f"CEPH-{number:02}" for number in range(13, 24)}:
            owners.append("prepare_slow_osd_context")
    if scope in {"CEPH-10", "CEPH-11", "CEPH-24"}:
        files.add("tests/helpers/ceph_s3_put.py")
    result = {name: digest((root / name).read_bytes()) for name in sorted(files)}
    if scope != "CEPH-02":
        contract_roots = ["assert_ceph_versions", "assert_clean_pgs", "qualify_host_probe",
                          "assert_lab_inventory", "render_csi_preflight"] + CONTRACT_OWNERS.get(scope, [])
        if scope in {f"CEPH-{number:02}" for number in range(13, 24)}:
            contract_roots.extend(["delayed_linear_table", "osd_ids_for_device", "osd_lv_for_device"])
        if scope.startswith("CEPH-OS-"):
            contract_roots.append("assert_qualification_inventory")
        for name, checksum in python_dependencies(root / "tests/helpers/ceph_contract.py", contract_roots).items():
            result["tests/helpers/ceph_contract.py#" + name] = checksum
        tree = ast.parse((root / "tests/helpers/ceph_contract.py").read_text())
        dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        result["tests/helpers/ceph_contract.py#dispatch"] = digest(ast.dump(dispatch, include_attributes=False).encode())
    for name, checksum in bash_dependencies(root / "tests/helpers/ceph-regression.sh", owners).items():
        result["tests/helpers/ceph-regression.sh#" + name] = checksum
    return result


def snapshot(root: Path, scopes: list[str]) -> dict:
    return {scope: source_dependencies(root, scope) for scope in scopes}


def capture_hosts_review(root: Path, original_sha256: str, target: Path) -> None:
    """Recover a known launch fingerprint; never guess historical source hashes."""
    runner_path = root / "tests/run_enterprise_regression.sh"
    current = runner_path.read_text()
    previous, count = re.subn(
        r'(?ms)^if \[\[ "\$MODE" == "--ceph-prepare-hosts-only" \]\]; then\n.*?^fi\n\n', "", current)
    if count != 1:
        raise ValueError("cannot isolate the new hosts contract entry point")
    additions = {"roles/prepare/tasks/main.yml", "tests/helpers/prepare_hosts_contract.py",
                 "tests/unit/test_prepare_hosts_contract.py"}
    for name in sorted(additions):
        previous, count = re.subn(r'(?m)^.*"\$ROOT/' + re.escape(name) + r'".*\n', "", previous)
        if count != 1:
            raise ValueError("cannot isolate the hosts fingerprint addition: " + name)
    function = "ceph_gate_fingerprint() {" + previous.split("ceph_gate_fingerprint() {", 1)[1].split(
        "\nrequire_ceph_focused_evidence()", 1)[0]
    pipeline = " | sort | xargs sha256sum | sha256sum | awk '{print $1}'"
    if function.count(pipeline) != 1:
        raise ValueError("unknown historical fingerprint algorithm")
    # The recorded algorithm includes absolute filenames in sha256sum output.
    paths = subprocess.check_output(["bash", "-c", 'ROOT="$1"\n' + function.replace(pipeline, " | sort") +
                                     "\nceph_gate_fingerprint --without-matrix", "review", str(root)], text=True).splitlines()
    manifest = {}
    for name in paths:
        path = Path(name)
        relative = str(path.relative_to(root))
        manifest[relative] = digest(previous.encode() if path == runner_path else path.read_bytes())
    reconstructed = digest("".join(checksum + "  " + str(root / name) + "\n"
                                   for name, checksum in sorted(manifest.items())).encode())
    if reconstructed != original_sha256:
        raise ValueError("reconstructed source does not match the original launch")
    prepare = "roles/prepare/tasks/main.yml"
    original = subprocess.check_output(["git", "show", "HEAD:" + prepare], cwd=root, text=True)
    actual = yaml.safe_load((root / prepare).read_text())
    owned = [task for group in actual for task in group.get("block", [])
             if task.get("name", "").startswith("the master[0] resolves all hosts")]
    if len(owned) != 1 or owned[0].pop("throttle", None) != 1 or actual != yaml.safe_load(original):
        raise ValueError("prepare change exceeds serialization of the same delegated hosts task")
    scopes = [row["id"] for row in yaml.safe_load((root / "tests/ceph-test-matrix.yaml").read_text())[
        "os_qualification"]["profiles"]]
    current_function = "ceph_gate_fingerprint() {" + current.split("ceph_gate_fingerprint() {", 1)[1].split(
        "\nrequire_ceph_focused_evidence()", 1)[0]
    current_sha256 = subprocess.check_output(["bash", "-c", 'ROOT="$1"\n' + current_function +
                                            "\nceph_gate_fingerprint --without-matrix", "review", str(root)], text=True).strip()
    review = dict(kind="reconstructed-launch-plus-reviewed-hosts-serialization", original_source_sha256=original_sha256,
                  current_source_sha256=current_sha256,
                  source_root=str(root), original_launch_files=manifest,
                  current_launch_files={name: digest((root / name).read_bytes()) for name in manifest},
                  prepare_sha256=digest((root / prepare).read_bytes()),
                  prepare_reference="https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_delegation.html#delegation-and-parallel-execution",
                  review_boundary="Original launch did not fingerprint prepare/main.yml. The repository delta is exactly throttle: 1; current focused and real-host evidence supplement that shared task. This is a dependency review, not a new execution of the historical OS profile.",
                  dependencies=snapshot(root, scopes))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("refusing to overwrite an existing source review")
    target.write_text(json.dumps(review, sort_keys=True, indent=2) + "\n")
    print("CEPH_HISTORICAL_SOURCE_RECONSTRUCTION_PASS source=" + reconstructed)


def restore_reviewed_os(root: Path, review_path: Path, originals: list[Path], supplement: Path) -> None:
    """Keep original successful OS measurements with explicit current delta proof."""
    raw_review = review_path.read_bytes()
    review = json.loads(raw_review)
    manifest = review["original_launch_files"]
    reconstructed = digest("".join(checksum + "  " + str(root / name) + "\n"
                                   for name, checksum in sorted(manifest.items())).encode())
    if review["source_root"] != str(root) or reconstructed != review["original_source_sha256"]:
        raise ValueError("historical source reconstruction mismatch")
    if ({name: digest((root / name).read_bytes()) for name in manifest} != review["current_launch_files"]
            or digest((root / "roles/prepare/tasks/main.yml").read_bytes()) != review["prepare_sha256"]):
        raise ValueError("reviewed source changed before restoration")
    proof = json.loads(supplement.read_bytes())
    if (proof["id"] != "CEPH-OS-ROCKY9" or proof["durable_rc"] != 0 or proof["failure_markers"] != 0
            or proof["source_sha256"] != review["current_source_sha256"]
            or proof["dependencies"] != source_dependencies(root, proof["id"])):
        raise ValueError("missing current real-host supplementary evidence")
    validate_record(root, proof)
    matrix = root / "tests/ceph-test-matrix.yaml"
    content = matrix.read_text()
    rows = {row["id"]: row for row in yaml.safe_load(content)["os_qualification"]["profiles"]}
    # Verify the supplement's published checksum and its complete durable log.
    binding = rows[proof["id"]]["evidence"]
    if rows[proof["id"]]["status"] != "pass" or digest(supplement.read_bytes()) != binding["sha256"]:
        raise ValueError("supplement is not a published passing record")
    required = {"CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
                "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_IDEMPOTENCE_PASS",
                "CEPH_OS_RECOVERY_PASS", "CEPH_OS_PERFORMANCE_PASS", "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"}
    records = []
    for original in originals:
        payload = json.loads(original.read_bytes())
        scope = payload["id"]
        row = rows[scope]
        log = original.parent / "live.log"
        text = log.read_text()
        if ((original.parent / "durable.exit").read_text().splitlines() != ["0", "0"] or FAILURE.search(text)
                or payload["source_sha256"] != review["original_source_sha256"]
                or payload["durable_rc"] != 0 or payload["failure_markers"] != 0
                or (payload["os_id"], payload["major"], payload["host"], payload["client_key_type"]) !=
                   (row["os_id"], row["major"], row["host"], row["client_key_type"])
                or not required <= set(payload["markers"])):
            raise ValueError("incomplete or unrelated historical OS evidence")
        performance = payload.get("performance", {})
        if (payload.get("fixture_kind") != "real-host" or payload.get("secure_msgr2") is not True
                or payload.get("cephadm_sha256") != yaml.safe_load(content)["versions"]["cephadm_sha256"]
                or performance.get("operations", 0) < 16 or performance.get("errors") != 0
                or not 0 < performance.get("p95_ms", 0) <= performance.get("p99_ms", 0)
                or (row["client_key_type"] == "aes256k" and payload.get("aes256k") is not True)
                or (row["client_key_type"] == "aes" and payload.get("legacy_aes_risk_accepted") is not True)
                or not payload.get("product_command", "").startswith("kubecli setup ")
                or not re.search(r"(?m)^CEPH_GATE_EXIT rc=0$", text)):
            raise ValueError("historical OS evidence lacks actual accepted measurements")
        for marker in required:
            suffix = "" if marker in {"CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"} else " id=" + scope
            if marker + suffix not in text:
                raise ValueError("historical OS log lacks its owning marker")
        dependencies = source_dependencies(root, scope)
        if dependencies != review["dependencies"][scope]:
            raise ValueError("OS dependencies changed after review")
        payload.update(dependencies=dependencies, log=str(log.relative_to(root)), log_sha256=digest(log.read_bytes()),
                       dependency_review={"path": str(review_path.relative_to(root)), "sha256": digest(raw_review),
                                          "original_record": str(original.relative_to(root)),
                                          "original_sha256": digest(original.read_bytes()),
                                          "supplement": str(supplement.relative_to(root)),
                                          "supplement_sha256": digest(supplement.read_bytes())})
        target = original.with_name(original.stem + "-reviewed-dependencies.json")
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        if target.exists() and target.read_bytes() != raw:
            raise ValueError("refusing to overwrite historical reviewed evidence")
        binding = {"path": str(target.relative_to(root)), "sha256": digest(raw)}
        pattern = r"(?m)^(\s+- \{id: " + re.escape(scope) + r",[^\n]*?, status: )(?:pending|fail|pass)(, evidence: ).*(\})$"
        content, count = re.subn(pattern, lambda match: match[1] + "pass" + match[2] + json.dumps(binding) + match[3], content)
        if count != 1:
            raise ValueError("cannot restore the exact OS row")
        records.append((target, raw))
    for target, raw in records:
        part = target.with_suffix(".part")
        part.write_bytes(raw)
        part.replace(target)
    part = matrix.with_suffix(".part")
    part.write_text(content)
    part.replace(matrix)
    print("CEPH_OS_REVIEWED_EVIDENCE_PASS profiles=" + str(len(records)))


def validate_record(root: Path, record: dict) -> None:
    if record["dependencies"] != source_dependencies(root, record["id"]):
        raise ValueError("changed module dependencies: " + record["id"])
    log = root / record["log"]
    if (root / "logs").resolve() not in log.resolve().parents:
        raise ValueError("evidence log must be inside ignored logs/")
    if digest(log.read_bytes()) != record["log_sha256"]:
        raise ValueError("evidence log checksum mismatch")
    if record["durable_rc"] != 0 or record["failure_markers"] != 0:
        raise ValueError("module did not complete successfully")


def audit_matrix(root: Path) -> list[str]:
    """Invalidate exactly the completed cases whose bound dependencies changed."""
    matrix = root / "tests/ceph-test-matrix.yaml"
    content = matrix.read_text()
    invalid = []
    for case in yaml.safe_load(content)["cases"]:
        if case["status"] != "pass":
            continue
        binding = re.search(r"record=([^;]+); sha256=([0-9a-f]{64})", case.get("evidence", ""))
        if not binding:
            raise ValueError("completed module lacks bound evidence: " + case["id"])
        path = (root / binding[1]).resolve()
        if (root / "logs").resolve() not in path.parents:
            raise ValueError("module record must be inside ignored logs/")
        raw = path.read_bytes()
        if digest(raw) != binding[2]:
            raise ValueError("module record checksum mismatch")
        record = json.loads(raw)
        if record["id"] != case["id"]:
            raise ValueError("module evidence identity mismatch")
        if record["dependencies"] == source_dependencies(root, case["id"]):
            validate_record(root, record)
            continue
        invalid.append(case["id"])
        pattern = r"(?ms)(^  - id: " + case["id"] + r"\n)(.*?)(?=^  - id:|^coverage_summary:)"
        def invalidate(match: re.Match) -> str:
            body = match[2].replace("    status: pass\n", "    status: pending\n", 1)
            body = re.sub(r"(?m)^    evidence:.*$", "    evidence: " + json.dumps(
                "Pending: owning dependency changed; previous bound evidence retained at " + binding[1]), body)
            return match[1] + body
        content, count = re.subn(pattern, invalidate, content)
        if count != 1:
            raise ValueError("cannot invalidate exact module row")
    for profile in yaml.safe_load(content)["os_qualification"]["profiles"]:
        if profile["status"] != "pass":
            continue
        binding = profile["evidence"]
        path = (root / binding["path"]).resolve()
        if (root / "logs").resolve() not in path.parents or digest(path.read_bytes()) != binding["sha256"]:
            raise ValueError("OS record location or checksum mismatch")
        record = json.loads(path.read_bytes())
        if record["id"] != profile["id"] or "dependencies" not in record:
            raise ValueError("OS record needs dependency impact analysis: " + profile["id"])
        if record["dependencies"] == source_dependencies(root, profile["id"]):
            validate_record(root, record)
            continue
        invalid.append(profile["id"])
        pattern = r"(?m)^(\s+- \{id: " + re.escape(profile["id"]) + r",[^\n]*?, status: )pass(, evidence: .*)$"
        content, count = re.subn(pattern, lambda match: match[1] + "pending" + match[2], content)
        if count != 1:
            raise ValueError("cannot invalidate exact OS row")
    if invalid:
        data = yaml.safe_load(content)
        passed = sum(case["status"] == "pass" for case in data["cases"])
        pending = sum(case["status"] == "pending" for case in data["cases"])
        content = re.sub(r"(?m)^  pass: \d+$", f"  pass: {passed}", content)
        content = re.sub(r"(?m)^  pending: \d+$", f"  pending: {pending}", content)
        content = re.sub(r"(?m)^  result: \w+$", "  result: PENDING", content)
        content = re.sub(r'(?m)^  overall_assessment:.*$', f'  overall_assessment: "{passed}/28 PENDING"', content)
        part = matrix.with_suffix(".part")
        part.write_text(content)
        part.replace(matrix)
    print("CEPH_MODULE_DEPENDENCY_AUDIT_PASS invalidated=" + (",".join(invalid) or "none"))
    return invalid


def bind_os_dependencies(root: Path, before: dict, scopes: list[str], original_sha256: str) -> None:
    """Attach the pre-measurement dependency snapshot to finalized OS evidence."""
    matrix = root / "tests/ceph-test-matrix.yaml"
    content = matrix.read_text()
    rows = {row["id"]: row for row in yaml.safe_load(content)["os_qualification"]["profiles"]}
    replacements = []
    for scope in scopes:
        row = rows[scope]
        if row["status"] != "pass":
            raise ValueError("OS qualification is not finalized")
        path = (root / row["evidence"]["path"]).resolve()
        if (root / "logs").resolve() not in path.parents:
            raise ValueError("OS evidence must be inside ignored logs/")
        raw = path.read_bytes()
        if digest(raw) != row["evidence"]["sha256"]:
            raise ValueError("OS evidence checksum mismatch")
        payload = json.loads(raw)
        if (payload["source_sha256"] != original_sha256 or payload["id"] != scope
                or payload["durable_rc"] != 0 or payload["failure_markers"] != 0):
            raise ValueError("OS evidence does not belong to the completed launch")
        if before[scope] != source_dependencies(root, scope):
            raise ValueError("OS execution dependencies changed")
        payload["dependencies"] = before[scope]
        log = path.parent / "live.log"
        payload["log"] = str(log.relative_to(root))
        payload["log_sha256"] = digest(log.read_bytes())
        target = path if path.stem.endswith("-dependencies") else path.with_name(path.stem + "-dependencies.json")
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        if target.exists() and target.read_bytes() != raw:
            raise ValueError("refusing to overwrite a different OS evidence record")
        replacements.append((target, raw))
        binding = {"path": str(target.relative_to(root)), "sha256": digest(raw)}
        pattern = r"(?m)^(\s+- \{id: " + re.escape(scope) + r",[^\n]*?, status: pass, evidence: ).*(\})$"
        content, count = re.subn(pattern, lambda match: match[1] + json.dumps(binding) + match[2], content)
        if count != 1:
            raise ValueError("cannot bind OS dependency evidence")
    for path, raw in replacements:
        part = path.with_suffix(".part")
        part.write_bytes(raw)
        part.replace(path)
    part = matrix.with_suffix(".part")
    part.write_text(content)
    part.replace(matrix)
    print("CEPH_OS_DEPENDENCY_BIND_PASS profiles=" + str(len(scopes)))


def require_modules(root: Path, scopes: list[str]) -> None:
    audit_matrix(root)
    cases = {case["id"]: case for case in yaml.safe_load((root / "tests/ceph-test-matrix.yaml").read_text())["cases"]}
    if not scopes or any(scope not in cases or cases[scope]["status"] != "pass" for scope in scopes):
        raise ValueError("current completed module evidence is required: " + ", ".join(scopes))
    print("CEPH_FOCUSED_PREREQUISITE_PASS cases=" + ",".join(scopes))


def publish(root: Path, log: Path, state: Path, before: dict, run_id: str,
            scopes: list[str], readonly: bool = False, full: bool = False, baseline: bool = False,
            completed: bool = False) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid evidence run ID")
    text = log.read_text()
    if state.read_text().splitlines() != ["0", "0"] or FAILURE.search(text):
        raise ValueError("invalid durable exit or failure markers")
    terminal = re.search(r"(?m)^CEPH_(?:GATE|STATIC)_EXIT rc=0$", text)
    if not terminal:
        raise ValueError("missing durable terminal marker")
    clean_markers = ("CEPH_MODULE_CLEAN_VERIFY_PASS",) if baseline else ("CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS")
    if not readonly and not all(re.search(r"(?m)^" + marker + r"$", text) for marker in clean_markers):
        raise ValueError("missing scoped cleanup evidence")
    if full and baseline:
        raise ValueError("full delivery cannot retain a baseline")
    if readonly and any(scope not in {"CEPH-01", "CEPH-02"} for scope in scopes):
        raise ValueError("stateful evidence cannot use the read-only cleanup boundary")
    if full and (set(scopes) != set(OWNERS) or not all(marker in text for marker in (
            "CEPH_CASE_LEDGER_PASS cases=28", "CEPH_DELIVERY_PASS cases=28"))):
        raise ValueError("missing full-run ledger and terminal marker")
    matrix = root / "tests/ceph-test-matrix.yaml"
    content = matrix.read_text()
    data = yaml.safe_load(content)
    cases = {case["id"]: case for case in data["cases"]}
    if full:
        # Recheck the independent OS prerequisites at publication, after the
        # long live run; launch-time validation alone cannot authorize sign-off.
        try:
            from ceph_contract import qualify_os_matrix
        except ModuleNotFoundError:
            from tests.helpers.ceph_contract import qualify_os_matrix
        qualify_os_matrix(matrix)
        for profile in data["os_qualification"]["profiles"]:
            if profile["status"] != "pass":
                raise ValueError("full delivery requires current OS qualification: " + profile["id"])
            path = (root / profile["evidence"]["path"]).resolve()
            if ((root / "logs").resolve() not in path.parents
                    or digest(path.read_bytes()) != profile["evidence"]["sha256"]):
                raise ValueError("OS qualification evidence checksum or location mismatch")
            record = json.loads(path.read_bytes())
            if record["id"] != profile["id"]:
                raise ValueError("OS qualification evidence identity mismatch")
            validate_record(root, record)
    if not scopes or not set(scopes) <= cases.keys():
        raise ValueError("invalid module selection")
    markers = {scope for scope, case in cases.items() if re.search(
        r"(?m)^" + re.escape(case["expected_marker"]) + r"(?:\s|$)", text)}
    # This cross-scenario item certifies read-back after the complete fault
    # sequence. Healthy workload read-back alone cannot satisfy it.
    if not {f"CEPH-{number:02}" for number in range(12, 24)} <= markers:
        markers.discard("CEPH-24")
    if completed:
        if full:
            raise ValueError("full delivery cannot filter unfinished cases")
        scopes = [scope for scope in scopes if scope in markers]
        if not scopes:
            raise ValueError("no selected module completed")
    directory = root / "logs/ceph-cases" / run_id
    records = []
    for scope in scopes:
        case = cases[scope]
        marker = case["expected_marker"]
        if scope not in markers:
            raise ValueError("missing owning product marker: " + scope)
        dependencies = source_dependencies(root, scope)
        if before.get(scope) != dependencies:
            raise ValueError("module source changed during execution: " + scope)
        record = dict(id=scope, run_id=run_id, dependencies=dependencies,
                      log=str((directory / "live.log").relative_to(root)), log_sha256=digest(text.encode()),
                      durable_rc=0, failure_markers=0, expected_marker=marker, terminal_marker=terminal[0],
                      cleanup="read-only-no-fixtures" if readonly else clean_markers[-1])
        records.append(record)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "live.log").write_text(text)
    for record in records:
        target = directory / (record["id"] + ".json")
        target.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
        boundary = "cleanup=read-only-no-fixtures" if readonly else clean_markers[-1]
        evidence = (f"{record['expected_marker']}; run_id={run_id}; {terminal[0]}; durable_rc=0; failure_markers=0; "
                    f"{boundary}; record={target.relative_to(root)}; sha256={digest(target.read_bytes())}")
        pattern = r"(?ms)(^  - id: " + record["id"] + r"\n)(.*?)(?=^  - id:|^coverage_summary:)"
        def replace(match: re.Match) -> str:
            body = re.sub(r"(?m)^    status: (?:pending|pass|fail)$", "    status: pass", match[2])
            body = re.sub(r"(?m)^    evidence:.*\n", "", body)
            return match[1] + body.rstrip() + "\n    evidence: " + json.dumps(evidence) + "\n\n"
        content, count = re.subn(pattern, replace, content)
        if count != 1:
            raise ValueError("cannot bind module matrix row")
    current = yaml.safe_load(content)
    for status in ("pass", "pending", "fail"):
        count = sum(case["status"] == status for case in current["cases"])
        content = re.sub(r"(?m)^  " + status + r": \d+$", f"  {status}: {count}", content)
    passed = sum(case["status"] == "pass" for case in current["cases"])
    if full and passed != 28:
        raise ValueError("full delivery requires every case")
    result = "PASS" if full else "PENDING"
    content = re.sub(r"(?m)^  result: \w+$", "  result: " + result, content)
    content = re.sub(r'(?m)^  overall_assessment:.*$', f'  overall_assessment: "{passed}/28 {result}"', content)
    part = matrix.with_suffix(".part")
    part.write_text(content)
    part.replace(matrix)
    print(f"CEPH_MODULE_EVIDENCE_PASS cases={len(scopes)} matrix_pass={passed}/28 delivery={result}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "publish", "bind-os", "audit", "require"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--readonly", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--completed", action="store_true")
    parser.add_argument("--source-sha256")
    args = parser.parse_args()
    required = {
        "audit": [], "require": ["cases"], "snapshot": ["snapshot", "cases"],
        "bind-os": ["snapshot", "cases", "source_sha256"],
        "publish": ["snapshot", "cases", "log", "state", "run_id"],
    }
    missing = ["--" + name.replace("_", "-") for name in required[args.command] if not getattr(args, name)]
    if missing:
        parser.error("missing arguments for " + args.command + ": " + ", ".join(missing))
    if args.command == "audit":
        audit_matrix(args.root)
    elif args.command == "require":
        require_modules(args.root, args.cases)
    elif args.command == "snapshot":
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(snapshot(args.root, args.cases), sort_keys=True, indent=2) + "\n")
    elif args.command == "bind-os":
        bind_os_dependencies(args.root, json.loads(args.snapshot.read_text()), args.cases, args.source_sha256)
    else:
        publish(args.root, args.log, args.state, json.loads(args.snapshot.read_text()), args.run_id,
                args.cases, args.readonly, args.full, args.baseline, args.completed)


if __name__ == "__main__":
    main()
