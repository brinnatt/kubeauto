#!/usr/bin/env python3
"""Deterministic parsers used by the Ceph host and live regression gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path


IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")


def qualify_os_matrix(matrix: Path, source_sha256: str | None = None) -> int:
    """Reject missing OS profiles and stale or incomplete host qualification."""
    import yaml

    data = yaml.safe_load(matrix.read_text(encoding="utf-8"))
    profiles = data.get("os_qualification", {}).get("profiles", [])
    required = {("rocky", major) for major in ("8", "9", "10")} | {
        ("ubuntu", major) for major in ("22", "24", "26")
    }
    actual = {(row["os_id"], row["major"]) for row in profiles}
    if actual != required or len(profiles) != len(required):
        raise ValueError("OS qualification requires every Rocky 8/9/10 and Ubuntu 22/24/26 profile")
    if len({row["id"] for row in profiles}) != len(profiles):
        raise ValueError("duplicate OS qualification ID")
    for row in profiles:
        if row.get("status") not in {"pending", "pass", "fail"}:
            raise ValueError("invalid OS qualification status: " + row["id"])
        if row.get("client_key_type") not in {"aes", "aes256k"}:
            raise ValueError("invalid OS client key type: " + row["id"])
    if source_sha256 is None:
        return len(profiles)
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("invalid current source fingerprint")
    pending = [row["id"] for row in profiles if row["status"] != "pass"]
    if pending:
        raise ValueError("real-host OS qualification is incomplete: " + ", ".join(pending))
    logs = (matrix.resolve().parents[1] / "logs").resolve()
    required_markers = {
        "CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
        "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_IDEMPOTENCE_PASS",
        "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS",
    }
    for row in profiles:
        binding = row["evidence"]
        path = (logs.parent / binding["path"]).resolve()
        if logs not in path.parents:
            raise ValueError("OS evidence must be inside ignored logs/: " + row["id"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise ValueError("OS evidence checksum mismatch: " + row["id"])
        evidence = json.loads(raw)
        if (evidence["id"], evidence["os_id"], evidence["major"]) != (row["id"], row["os_id"], row["major"]):
            raise ValueError("OS evidence identity mismatch: " + row["id"])
        if evidence["source_sha256"] != source_sha256 or evidence["cephadm_sha256"] != data["versions"]["cephadm_sha256"]:
            raise ValueError("OS evidence belongs to a different source/artifact: " + row["id"])
        if evidence["durable_rc"] != 0 or evidence["failure_markers"] != 0:
            raise ValueError("OS qualification did not exit cleanly: " + row["id"])
        if not required_markers <= set(evidence["markers"]):
            raise ValueError("OS qualification lacks business/cleanup markers: " + row["id"])
        if (evidence["fixture_kind"] != "real-host" or evidence["secure_msgr2"] is not True
                or evidence.get("client_key_type") != row["client_key_type"]):
            raise ValueError("OS qualification needs a real selected kernel-client path: " + row["id"])
        if row["client_key_type"] == "aes256k" and evidence.get("aes256k") is not True:
            raise ValueError("OS qualification lacks aes256k support: " + row["id"])
        if row["client_key_type"] == "aes" and evidence.get("legacy_aes_risk_accepted") is not True:
            raise ValueError("OS qualification lacks explicit legacy-aes acceptance: " + row["id"])
        for key in ("run_id", "host", "kernel", "python", "runtime", "product_command"):
            if not isinstance(evidence.get(key), str) or not evidence[key].strip():
                raise ValueError("OS evidence missing " + key + ": " + row["id"])
        if not evidence["product_command"].startswith("kubecli setup "):
            raise ValueError("OS evidence bypassed the product entry point: " + row["id"])
    return len(profiles)


def assert_lab_inventory(data: dict, compute: list[str], storage: list[str]) -> None:
    """Bind the product inventory to the runner's separate leased host pools."""
    if len(set(compute)) != 6 or len(set(storage)) != 6 or set(compute) & set(storage):
        raise ValueError("lab requires six disjoint compute and storage hosts")
    if set(data.get("_meta", {}).get("hostvars", {})) != set(compute + storage):
        raise ValueError("inventory contains missing or foreign lab hosts")
    for name, hosts in (("etcd", compute[:3]), ("kube_master", compute[:3]),
                        ("kube_node", compute[3:]), ("ceph", storage)):
        group = data.get(name, {})
        if group.get("children") or set(group.get("hosts", [])) != set(hosts):
            raise ValueError(f"inventory role differs from the leased pool: {name}")


def _walk_devices(devices: Iterable[dict]) -> Iterable[dict]:
    for device in devices:
        yield device
        yield from _walk_devices(device.get("children") or [])


def _parse_probe_preamble(raw: str) -> tuple[list[str], dict]:
    lines = raw.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("{")), None)
    if start is None:
        raise ValueError("lsblk JSON is missing")
    return lines[:start], json.loads("\n".join(lines[start:]))


def qualify_host_probe(
    raw: str,
    host: str,
    expected_hostname: str,
    expected_disks: int,
    allow_test_paths: bool = False,
) -> list[str]:
    preamble, data = _parse_probe_preamble(raw)
    meta = next((line.split("|", 1)[1].split("|") for line in preamble if line.startswith("HOST_META|")), None)
    if not meta or len(meta) != 5:
        raise ValueError(f"invalid host metadata host={host}")
    hostname, os_id, version_id, cpus, memory_kib = meta
    if hostname != expected_hostname:
        raise ValueError(f"hostname mismatch host={host} expected={expected_hostname} actual={hostname}")
    if os_id != "rocky" or not version_id.startswith("9."):
        raise ValueError(f"unsupported OS host={host} os={os_id} version={version_id}")
    if int(cpus) < 2 or int(memory_kib) < 4 * 1024 * 1024:
        raise ValueError(f"insufficient capacity host={host} cpu={cpus} memory_kib={memory_kib}")

    root_devices = {
        line.split("|", 1)[1]
        for line in preamble
        if line.startswith("ROOT_DEVICE|") and line.split("|", 1)[1]
    }
    by_id: dict[str, list[str]] = {}
    by_path: dict[str, list[tuple[str, str]]] = {}
    for line in preamble:
        if line.startswith("BYID|"):
            _, target, link = line.split("|", 2)
            by_id.setdefault(target, []).append(link)
        elif line.startswith("BYPATH|"):
            _, target, link, path_id = line.split("|", 3)
            by_path.setdefault(target, []).append((link, path_id))

    candidates: list[tuple[str, str, str, str, str, int]] = []
    for device in data.get("blockdevices", []):
        if device.get("type") != "disk":
            continue
        kernel_path = str(device.get("path") or "")
        if kernel_path in root_devices or str(device.get("name") or "") in root_devices:
            continue
        descendants = list(_walk_devices(device.get("children") or []))
        fields = [device, *descendants]
        if descendants:
            continue
        if any(any(value for value in (entry.get("mountpoints") or [])) for entry in fields):
            continue
        if any(entry.get("fstype") for entry in fields):
            continue
        if bool(device.get("ro")) or int(device.get("size") or 0) <= 0:
            continue
        serial = str(device.get("serial") or "").strip()
        wwn = str(device.get("wwn") or "").strip()
        links = [path for path in by_id.get(kernel_path, []) if "-part" not in path]
        links.sort(
            key=lambda path: (
                not path.rsplit("/", 1)[-1].startswith("wwn-"),
                not path.rsplit("/", 1)[-1].startswith("nvme-eui."),
                not path.rsplit("/", 1)[-1].startswith("scsi-"),
                path,
            )
        )
        identity = wwn or serial
        stable_path = links[0] if links else ""
        if not IDENTITY_RE.fullmatch(identity):
            test_paths = sorted(
                (link, path_id)
                for link, path_id in by_path.get(kernel_path, [])
                if re.fullmatch(r"/dev/disk/by-path/pci-[A-Za-z0-9_.:-]+", link)
                and re.fullmatch(r"pci-[A-Za-z0-9_.:-]+", path_id)
                and link.rsplit("/", 1)[-1] == path_id
            )
            if not allow_test_paths or len(test_paths) != 1:
                raise ValueError(f"candidate disk lacks a safe serial/WWN host={host} path={kernel_path}")
            stable_path, path_id = test_paths[0]
            identity = f"path:{path_id}"
        elif not stable_path:
            raise ValueError(f"candidate disk lacks stable by-id path host={host} path={kernel_path}")
        candidates.append((kernel_path, stable_path, serial, wwn, identity, int(device["size"])))

    if len(candidates) != expected_disks:
        raise ValueError(f"expected {expected_disks} unused data disks, found {len(candidates)} host={host}")
    candidates.sort(key=lambda row: row[1])
    return ["|".join((host, *map(str, row))) for row in candidates]


def _entry_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _entry_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _entry_strings(item)


def osd_ids_for_device(data: dict, device: str) -> list[str]:
    aliases = {device, device.rsplit("/", 1)[-1]}
    if device.startswith("/dev/mapper/"):
        aliases.add(f"/dev/{device.rsplit('/', 1)[-1]}")
    matches: list[str] = []
    for osd_id, entries in data.items():
        if not str(osd_id).isdigit():
            continue
        values = set(_entry_strings(entries))
        tokens = {token for value in values for token in re.split(r"[\s,]+", value) if token}
        if aliases & tokens or any(token.endswith(f"/{device.rsplit('/', 1)[-1]}") for token in tokens):
            matches.append(str(osd_id))
    return sorted(set(matches), key=int)


def osd_lv_for_device(data: dict, osd_id: str, lv_type: str, fsid: str, device: str) -> str:
    entries = data.get(osd_id, [])
    matches = []
    for entry in entries:
        if entry.get("type") != lv_type:
            continue
        tags = entry.get("tags", {})
        if tags.get("ceph.cluster_fsid") != fsid:
            raise ValueError("OSD LV does not belong to the runner FSID")
        if device not in entry.get("devices", []):
            raise ValueError("OSD LV backing is outside its recorded disk identity")
        path = entry.get("lv_path", "")
        if not re.fullmatch(r"/dev/ceph-[0-9a-f-]+/osd-(block|db)-[0-9a-f-]+", path):
            raise ValueError(f"invalid Ceph-owned LV path: {path}")
        matches.append(path)
    if len(matches) != 1:
        raise ValueError(f"expected one {lv_type} LV for osd.{osd_id}, found {len(matches)}")
    return matches[0]


def delayed_linear_table(original: str, delay_ms: int) -> str:
    if delay_ms <= 0:
        raise ValueError("device delay must be positive")
    lines = []
    for line in original.splitlines():
        fields = line.split()
        if (len(fields) != 5 or fields[2] != "linear"
                or not all(value.isdigit() for value in (fields[0], fields[1], fields[4]))
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[3])
                or int(fields[1]) <= 0):
            raise ValueError("fault injection requires a valid native linear table")
        start, length, _, device, offset = fields
        lines.append(f"{start} {length} delay {device} {offset} {delay_ms} {device} {offset} {delay_ms}")
    if not lines:
        raise ValueError("native OSD linear table is empty")
    return "\n".join(lines)


def assert_ceph_versions(data: dict, expected: str) -> None:
    overall = data.get("overall")
    if not isinstance(overall, dict) or not overall:
        raise ValueError("ceph versions JSON has no overall daemon summary")
    seen = 0
    for role, versions in data.items():
        if not isinstance(versions, dict):
            raise ValueError(f"invalid version summary role={role}")
        for version, count in versions.items():
            if int(count) <= 0:
                raise ValueError(f"invalid daemon count role={role} version={version}")
            if not version.startswith(f"ceph version {expected} "):
                raise ValueError(f"unexpected daemon version role={role} version={version}")
            seen += int(count)
    if seen <= 0:
        raise ValueError("ceph versions JSON contains no daemons")


def assert_clean_pgs(data: dict) -> None:
    # v20.2.4 pg stat wraps its counts; an unready mgr may report stale states.
    if not isinstance(data, dict):
        raise ValueError("invalid PG summary")
    if data.get("pg_ready") is not True:
        raise ValueError("PG map is not ready")
    summary = data.get("pg_summary")
    if not isinstance(summary, dict):
        raise ValueError("PG summary is missing or invalid")
    total = summary.get("num_pgs")
    states = summary.get("num_pg_by_state")
    if type(total) is not int or total <= 0 or not isinstance(states, list) or not states:
        raise ValueError("PG summary is missing or empty")
    counted = 0
    for state in states:
        count = state.get("num") if isinstance(state, dict) else None
        if type(count) is not int or count <= 0:
            raise ValueError("invalid PG state count")
        if state.get("name") != "active+clean":
            raise ValueError(f"PGs have not fully recovered: {state.get('name')}")
        counted += count
    if counted != total:
        raise ValueError("PG state counts do not match total")


def pg_rows(document: dict) -> list[dict]:
    rows = document.get("pg_stats") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("PG stats are missing or empty")
    return rows


def recovery_target(document: dict, slow_osd: int) -> int:
    for row in pg_rows(document):
        acting = row.get("acting", [])
        if (row.get("state") == "active+clean" and slow_osd in acting
                and row.get("stat_sum", {}).get("num_bytes", 0) > 0):
            for osd in acting:
                if type(osd) is int and osd >= 0 and osd != slow_osd:
                    return osd
    raise ValueError("no populated clean PG has another replica alongside the slow OSD")


def assert_pg_activity(document: dict, activity: str, osd_id: int | None = None) -> None:
    if activity not in {"recovery", "deep-scrub"}:
        raise ValueError("unknown PG activity")
    for row in pg_rows(document):
        states = set(row.get("state", "").split("+"))
        if osd_id is not None and osd_id not in row.get("acting", []):
            continue
        if activity == "recovery" and states & {"recovering", "backfilling"}:
            return
        if activity == "deep-scrub" and {"scrubbing", "deep"} <= states:
            return
    raise ValueError(f"no active {activity} in the selected PGs")


def cgroup_device_fields(raw: str, device: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9]+:[0-9]+", device):
        raise ValueError("invalid cgroup block device number")
    rows = [line.split() for line in raw.splitlines() if line.split()[:1] == [device]]
    if len(rows) > 1:
        raise ValueError("duplicate cgroup block device row")
    return dict(field.split("=", 1) for field in rows[0][1:]) if rows else {}


def cgroup_io_snapshot(raw: str, device: str) -> dict:
    fields = cgroup_device_fields(raw, device)
    counters = {key: int(fields[key]) for key in ("rbytes", "wbytes", "rios", "wios")}
    if any(value < 0 for value in counters.values()):
        raise ValueError("negative cgroup I/O counter")
    return {"device": device, "time": time.monotonic(), **counters}


def assert_cgroup_iops_limit(raw: str, device: str, expected: str) -> None:
    fields = cgroup_device_fields(raw, device)
    if expected != "max" and (not expected.isdigit() or int(expected) <= 0):
        raise ValueError("invalid IOPS ceiling")
    if any(fields.get(key, "max") != expected for key in ("riops", "wiops")):
        raise ValueError(f"kernel IOPS limit does not match {expected} on {device}")


def throttle_phase_evidence(benchmark: dict, before: dict, after: dict) -> dict:
    bench = benchmark["bench"]
    elapsed = float(after["time"]) - float(before["time"])
    writes = int(after["wios"]) - int(before["wios"])
    latency = float(bench["average_latency"])
    run_time = float(bench["total_time_run"])
    if (before["device"] != after["device"] or not math.isfinite(elapsed)
            or elapsed < 15 or writes <= 0 or not math.isfinite(latency) or latency <= 0
            or not math.isfinite(run_time) or run_time < 15 or int(bench["total_writes_made"]) <= 0):
        raise ValueError("benchmark did not produce a sustained, device-bound I/O sample")
    return {"device": before["device"], "elapsed": elapsed, "write_ios": writes,
            "write_iops": writes / elapsed, "average_latency": latency,
            "rados_writes": int(bench["total_writes_made"])}


def assert_throttle_effect(healthy: dict, limited: dict, recovered: dict, limit: int) -> None:
    phases = (healthy, limited, recovered)
    values = [float(phase[key]) for phase in phases for key in ("write_iops", "average_latency")]
    if limit <= 0 or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("invalid throttle comparison sample")
    if healthy["write_iops"] <= limit * 2:
        raise ValueError("healthy workload did not exceed the IOPS ceiling")
    # Linux io.max allows short bursts; a sustained sample has bounded slack.
    if limited["write_iops"] > limit * 1.5 or limited["average_latency"] <= healthy["average_latency"] * 2:
        raise ValueError("IOPS ceiling did not measurably throttle the workload")
    if (recovered["write_iops"] <= limited["write_iops"] * 2
            or recovered["average_latency"] >= limited["average_latency"] / 2):
        raise ValueError("I/O did not recover after clearing the ceiling")


def assert_csi_resource_ownership(releases: dict[str, list[dict]], namespace: str) -> None:
    cluster_scoped = {
        "ClusterRole", "ClusterRoleBinding", "CSIDriver", "StorageClass",
        "VolumeSnapshotClass", "VolumeGroupSnapshotClass",
    }
    owners = {}
    for release, resources in releases.items():
        configmaps = {row["metadata"]["name"] for row in resources if row["kind"] == "ConfigMap"}
        for row in resources:
            kind, metadata = row["kind"], row["metadata"]
            scope = "" if kind in cluster_scoped else metadata.get("namespace", namespace)
            if kind not in cluster_scoped and scope != namespace:
                raise ValueError(f"{release} renders a resource in the wrong namespace: {scope}")
            key = kind, scope, metadata["name"]
            if key in owners:
                raise ValueError(f"resource ownership collision {key}: {owners[key]} and {release}")
            owners[key] = release
            if kind in {"Deployment", "DaemonSet"}:
                for volume in row["spec"]["template"]["spec"].get("volumes", []):
                    configmap = volume.get("configMap", {}).get("name")
                    if configmap and configmap not in configmaps:
                        raise ValueError(f"{release} references an unowned ConfigMap {configmap}")


def parse_task_templates(base: Path) -> int:
    import yaml
    from jinja2 import Environment, TemplateSyntaxError

    environment = Environment(autoescape=False)
    files = sorted((base / "roles/ceph/tasks").glob("*.yml"))
    if not files:
        raise ValueError("Ceph task files are missing")
    count = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        # Older distro libyaml rejects colons in unquoted flow scalars.
        flow_depth = 0
        for token in yaml.scan(source):
            if isinstance(token, (yaml.tokens.FlowSequenceStartToken, yaml.tokens.FlowMappingStartToken)):
                flow_depth += 1
            elif isinstance(token, (yaml.tokens.FlowSequenceEndToken, yaml.tokens.FlowMappingEndToken)):
                flow_depth -= 1
            elif isinstance(token, yaml.tokens.ScalarToken) and flow_depth and token.plain and ":" in token.value:
                raise ValueError(f"{path.name}: unquoted colon in YAML flow scalar at line {token.start_mark.line + 1}")
        tasks = yaml.safe_load(source)
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"invalid Ceph task list: {path.name}")
        for task in tasks:
            command = task.get("ansible.builtin.command", {})
            if isinstance(command, dict) and isinstance(command.get("argv"), list):
                if not all(isinstance(argument, str) for argument in command["argv"]):
                    raise ValueError(f"{path.name}: command argv must contain only strings: {task.get('name')}")
        for value in _entry_strings(tasks):
            try:
                environment.parse(value)
            except TemplateSyntaxError as error:
                raise ValueError(f"{path.name}: {error}") from error
            count += 1
    return count


def render_csi_preflight(base: Path, helm: str) -> None:
    import yaml
    from jinja2 import Environment, StrictUndefined

    sys.path.insert(0, str(base))
    from service.cluster.manager import ClusterManager

    config = (base / "conf/config.yml").read_text(encoding="utf-8")
    for placeholder, version in ClusterManager()._get_config_placeholders().items():
        config = config.replace(placeholder, version)
    variables = yaml.safe_load(config)
    variables.update({
        "ceph_cluster_fsid": {"stdout": "01234567-89ab-cdef-0123-456789abcdef"},
        "ceph_mon_dump": {"stdout": json.dumps({"mons": [
            {"public_addrs": {"addrvec": [{"type": "v2", "addr": "10.1.0.1:3300/0"}]}}
        ]})},
    })
    env = Environment(autoescape=False, undefined=StrictUndefined)
    env.filters["from_json"] = json.loads
    env.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(pattern, replacement, value)
    releases = {}
    role = base / "roles/ceph"
    variables.update({"base_dir": str(base), "role_path": str(role),
                      "cluster_dir": str(base / "clusters/ceph-preflight")})
    tasks = yaml.safe_load((role / "tasks/csi.yml").read_text(encoding="utf-8"))
    helm_task = next(task for task in tasks if task.get("register") == "ceph_csi_helm")
    for item in helm_task["loop"]:
        argv = [env.from_string(argument).render(**variables, item=item)
                for argument in helm_task["ansible.builtin.command"]["argv"]]
        expected_values = str(Path(variables["cluster_dir"]) / item["values"])
        if argv[argv.index("--values") + 1] != expected_values:
            raise ValueError(f"Helm values path is not the declared file: {item['release']}")
    print(f"CEPH_CSI_HELM_ARGV_PASS releases={len(helm_task['loop'])}")
    namespace = variables["ceph_csi_namespace"]
    version = variables["ceph_csi_ver"].lstrip("v")
    with tempfile.TemporaryDirectory(prefix="kubeauto-ceph-csi-render-") as directory:
        for component in ("rbd", "cephfs"):
            release = f"ceph-csi-{component}"
            template = (role / f"templates/csi-{component}-values.yml.j2").read_text(encoding="utf-8")
            values = Path(directory) / f"{release}.yml"
            values.write_text(env.from_string(template).render(**variables), encoding="utf-8")
            result = subprocess.run([
                helm, "template", release, str(role / f"files/{release}-{version}.tgz"),
                "--namespace", namespace, "--values", str(values),
            ], check=True, text=True, capture_output=True, timeout=60)
            releases[release] = [row for row in yaml.safe_load_all(result.stdout) if row]
    assert_csi_resource_ownership(releases, namespace)
    print(f"CEPH_CSI_RENDER_PASS releases={len(releases)} resources={sum(map(len, releases.values()))}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("lab-inventory")
    inventory_parser.add_argument("--compute", nargs=6, required=True)
    inventory_parser.add_argument("--storage", nargs=6, required=True)
    host_parser = subparsers.add_parser("host-probe")
    host_parser.add_argument("--host", required=True)
    host_parser.add_argument("--hostname", required=True)
    host_parser.add_argument("--expected-disks", type=int, required=True)
    host_parser.add_argument("--allow-test-paths", action="store_true")
    osd_parser = subparsers.add_parser("osd-ids")
    osd_parser.add_argument("--device", required=True)
    osd_parser.add_argument("--single", action="store_true")
    lv_parser = subparsers.add_parser("osd-lv")
    lv_parser.add_argument("--osd-id", required=True)
    lv_parser.add_argument("--type", choices=("block", "db"), required=True)
    lv_parser.add_argument("--fsid", required=True)
    lv_parser.add_argument("--device", required=True)
    table_parser = subparsers.add_parser("delay-table")
    table_parser.add_argument("--delay-ms", type=int, required=True)
    version_parser = subparsers.add_parser("versions")
    version_parser.add_argument("--expected", required=True)
    csi_parser = subparsers.add_parser("csi-render")
    csi_parser.add_argument("--base", type=Path, required=True)
    csi_parser.add_argument("--helm", required=True)
    task_parser = subparsers.add_parser("task-templates")
    task_parser.add_argument("--base", type=Path, required=True)
    os_parser = subparsers.add_parser("os-qualification")
    os_parser.add_argument("--matrix", type=Path, required=True)
    os_parser.add_argument("--source-sha256")
    subparsers.add_parser("pg-clean")
    target_parser = subparsers.add_parser("recovery-target")
    target_parser.add_argument("--osd-id", type=int, required=True)
    activity_parser = subparsers.add_parser("pg-activity")
    activity_parser.add_argument("--activity", choices=("recovery", "deep-scrub"), required=True)
    activity_parser.add_argument("--osd-id", type=int)
    for command in ("io-snapshot", "io-limit"):
        io_parser = subparsers.add_parser(command)
        io_parser.add_argument("--device", required=True)
        if command == "io-limit":
            io_parser.add_argument("--expected", required=True)
    phase_parser = subparsers.add_parser("throttle-phase")
    phase_parser.add_argument("--before", required=True)
    phase_parser.add_argument("--after", required=True)
    effect_parser = subparsers.add_parser("throttle-effect")
    effect_parser.add_argument("--healthy", required=True)
    effect_parser.add_argument("--limited", required=True)
    effect_parser.add_argument("--recovered", required=True)
    effect_parser.add_argument("--limit", type=int, required=True)
    args = parser.parse_args()

    try:
        if args.command == "os-qualification":
            count = qualify_os_matrix(args.matrix, args.source_sha256)
            marker = "CEPH_OS_QUALIFICATION_PASS" if args.source_sha256 else "CEPH_OS_MATRIX_CONTRACT_PASS"
            print(f"{marker} profiles={count}")
            return 0
        if args.command == "task-templates":
            count = parse_task_templates(args.base)
            print(f"CEPH_TASK_TEMPLATE_SYNTAX_PASS strings={count}")
            return 0
        if args.command == "csi-render":
            render_csi_preflight(args.base, args.helm)
            return 0
        raw = sys.stdin.read()
        if args.command == "lab-inventory":
            assert_lab_inventory(json.loads(raw), args.compute, args.storage)
            print("CEPH_LAB_INVENTORY_PASS storage=6 compute=6")
        elif args.command == "host-probe":
            for line in qualify_host_probe(
                raw, args.host, args.hostname, args.expected_disks, args.allow_test_paths
            ):
                print(line)
        elif args.command == "osd-ids":
            matches = osd_ids_for_device(json.loads(raw), args.device)
            if args.single and len(matches) != 1:
                raise ValueError(f"expected one OSD for {args.device}, found {matches}")
            if not matches:
                raise ValueError(f"no OSD found for {args.device}")
            print("\n".join(matches))
        elif args.command == "osd-lv":
            print(osd_lv_for_device(json.loads(raw), args.osd_id, args.type, args.fsid, args.device))
        elif args.command == "delay-table":
            print(delayed_linear_table(raw, args.delay_ms))
        elif args.command == "versions":
            assert_ceph_versions(json.loads(raw), args.expected)
            print(f"CEPH_VERSION_JSON_PASS version={args.expected}")
        elif args.command == "recovery-target":
            print(recovery_target(json.loads(raw), args.osd_id))
        elif args.command == "pg-activity":
            assert_pg_activity(json.loads(raw), args.activity, args.osd_id)
            print(f"CEPH_PG_ACTIVITY_PASS activity={args.activity}")
        elif args.command == "io-snapshot":
            print(json.dumps(cgroup_io_snapshot(raw, args.device)))
        elif args.command == "io-limit":
            assert_cgroup_iops_limit(raw, args.device, args.expected)
            print(f"CEPH_IOPS_LIMIT_PASS device={args.device} expected={args.expected}")
        elif args.command == "throttle-phase":
            print(json.dumps(throttle_phase_evidence(json.loads(raw), json.loads(args.before), json.loads(args.after))))
        elif args.command == "throttle-effect":
            phases = {key: json.loads(getattr(args, key)) for key in ("healthy", "limited", "recovered")}
            assert_throttle_effect(**phases, limit=args.limit)
            print(f"CEPH_IOPS_EFFECT_PASS limit={args.limit} phases={json.dumps(phases)}")
        else:
            assert_clean_pgs(json.loads(raw))
            print("CEPH_PG_CLEAN_JSON_PASS")
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"CEPH_CONTRACT_FAIL reason={error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
