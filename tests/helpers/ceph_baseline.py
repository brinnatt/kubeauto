#!/usr/bin/env python3
"""Verify identities before a focused gate reuses its owned Ceph baseline."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

try:
    from ceph_case_evidence import source_dependencies
except ModuleNotFoundError:
    from tests.helpers.ceph_case_evidence import source_dependencies


STORAGE = ["192.168.122." + suffix for suffix in ("135", "40", "72", "212", "165", "238")]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]


def execute(argv: list[str]) -> str:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=120)
    if result.returncode:
        # Never print the output of an auth or Secret query.
        raise ValueError("baseline probe failed: " + " ".join(argv[:3]))
    return result.stdout.strip()


def remote(host: str, command: str) -> str:
    if host not in STORAGE:
        raise ValueError("foreign baseline host")
    return execute(SSH + ["root@" + host, command])


def verify_disks(state: Path) -> None:
    """Re-read stable identities, capacity and root ancestry without disk writes."""
    rows = [line.split("|") for line in (state / "disk-allowlist").read_text().splitlines()]
    if len(rows) != 18 or any(len(row) != 7 for row in rows):
        raise ValueError("baseline disk allowlist is incomplete")
    seen = set()
    for host, kernel, stable, serial, wwn, identity, size in rows:
        if (host not in STORAGE or not re.fullmatch(r"/dev/[A-Za-z0-9_.:+/-]+", kernel)
                or not re.fullmatch(r"/dev/disk/by-(?:id|path)/[A-Za-z0-9_.:+-]+", stable)
                or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", identity)
                or not size.isdigit() or int(size) <= 0 or (host, stable) in seen):
            raise ValueError("unowned baseline disk identity")
        seen.add((host, stable))
        command = ("set -eu; test -b '" + stable + "'; "
                   "printf 'resolved=%s\\n' \"$(readlink -f '" + stable + "')\"; "
                   "printf 'bytes=%s\\n' \"$(lsblk -bdno SIZE '" + stable + "')\"; "
                   "printf 'path=%s\\n' \"$(udevadm info --query=property --name='" + stable + "' | sed -n 's/^ID_PATH=//p')\"; "
                   "printf 'serial=%s\\n' \"$(lsblk -dno SERIAL '" + stable + "' | xargs)\"; "
                   "printf 'wwn=%s\\n' \"$(lsblk -dno WWN '" + stable + "' | xargs)\"; "
                   "root=$(findmnt -no SOURCE /); lsblk -srnpo PATH \"$root\" | sed 's/^/root=/'")
        values = remote(host, command).splitlines()
        probe = dict(line.split("=", 1) for line in values if not line.startswith("root="))
        roots = {line.removeprefix("root=") for line in values if line.startswith("root=")}
        matches = identity == "path:" + probe.get("path", "") if "/by-path/" in stable else identity in {
            probe.get("serial", ""), probe.get("wwn", "")}
        if (not matches or probe.get("resolved") != kernel or probe.get("bytes") != size
                or not roots or kernel in roots):
            raise ValueError("baseline disk changed or overlaps the system disk")
    if any(sum(row[0] == host for row in rows) != 3 for host in STORAGE):
        raise ValueError("baseline storage lease differs from the allowlist")


def identities(root: Path, state: Path) -> dict:
    if state.is_symlink() or (state / "owner").read_text().strip() != "kubeauto-ceph-regression":
        raise ValueError("unowned focused baseline")
    fsid = (state / "fsid").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", fsid):
        raise ValueError("invalid baseline FSID")
    if remote(STORAGE[0], "cat /etc/ceph/kubeauto-owned-fsid") != fsid:
        raise ValueError("baseline FSID ownership mismatch")
    verify_disks(state)
    kube = ["kubectl", "--kubeconfig=" + str(root / "clusters/ceph-gate/kubectl.kubeconfig")]
    nodes = json.loads(execute(kube + ["get", "nodes", "-o", "json"]))["items"]
    if len(nodes) != 6 or any(not any(condition["type"] == "Ready" and condition["status"] == "True"
                                    for condition in node["status"]["conditions"]) for node in nodes):
        raise ValueError("baseline compute nodes are not healthy")
    allowed_compute = {"192.168.47." + suffix for suffix in ("134", "135", "136", "131", "132", "137")}
    addresses = {address["address"] for node in nodes for address in node["status"]["addresses"]
                 if address["type"] == "InternalIP"}
    if addresses != allowed_compute:
        raise ValueError("baseline compute inventory differs from the leased pool")
    fixtures = json.loads(execute(kube + ["-n", "ceph-regression", "get", "pod,pvc", "-o", "json"]))["items"]
    expected = {("Pod", "storage-client"), ("Pod", "cephfs-reader"), ("Pod", "s3-client"),
                ("PersistentVolumeClaim", "rbd-data"), ("PersistentVolumeClaim", "cephfs-data")}
    if {(row["kind"], row["metadata"]["name"]) for row in fixtures} != expected:
        raise ValueError("baseline fixture set changed")
    for row in fixtures:
        if row["kind"] == "Pod":
            labels = row["metadata"].get("labels", {})
            owned = labels.get("kubeauto.io/owner") == "ceph-regression" or (
                row["metadata"]["name"] == "cephfs-reader" and labels.get("run") == "cephfs-reader")
            if (not owned or not row["status"].get("containerStatuses")
                    or not all(container["ready"] for container in row["status"]["containerStatuses"])):
                raise ValueError("baseline Pod ownership or readiness mismatch")
        elif row["status"]["phase"] != "Bound":
            raise ValueError("baseline PVC is not bound")
    csi = json.loads(execute(kube + ["-n", "ceph-csi", "get", "pods", "-o", "json"]))["items"]
    if not csi or any(not row["status"].get("containerStatuses") or not all(
            container["ready"] for container in row["status"]["containerStatuses"]) for row in csi):
        raise ValueError("baseline CSI is not healthy")
    ceph = "/usr/local/sbin/cephadm shell -- ceph "
    if remote(STORAGE[0], ceph + "fsid") != fsid:
        raise ValueError("running FSID differs from baseline")
    versions = json.loads(remote(STORAGE[0], ceph + "versions --format json"))
    if not versions.get("overall") or any(not version.startswith("ceph version 20.2.4 ")
                                         for version in versions["overall"]):
        raise ValueError("baseline daemon versions are not the delivery target")
    osds = json.loads(remote(STORAGE[0], ceph + "osd dump --format json"))
    flags = set(osds.get("flags", "").split(",")) - {""}
    faults = {"noout", "noup", "nodown", "noin", "nobackfill", "norecover", "norebalance",
              "noscrub", "nodeep-scrub", "pauserd", "pausewr"}
    if flags & faults or len(osds["osds"]) != 12 or any(not row["up"] or not row["in"] for row in osds["osds"]):
        raise ValueError("baseline OSD faults or map flags remain")
    qdiscs = remote(STORAGE[0], "tc qdisc show")
    if re.search(r"\bqdisc (?:netem|tbf|htb)\b", qdiscs):
        raise ValueError("baseline network fault remains")
    remote(STORAGE[0], "set -eu; count=0; for unit in $(systemctl list-units --all --plain --no-legend "
           "'ceph-" + fsid + "@osd.*' | awk '{print $1}'); do count=$((count+1)); "
           "for property in IOReadIOPSMax IOWriteIOPSMax IOReadBandwidthMax IOWriteBandwidthMax; do "
           "test -z \"$(systemctl show \"$unit\" --property=\"$property\" --value)\"; done; "
           "cg=$(systemctl show \"$unit\" --property=ControlGroup --value); test -n \"$cg\"; "
           "if test -f \"/sys/fs/cgroup$cg/io.max\"; then "
           "awk '{for(i=2;i<=NF;i++) if($i !~ /=max$/) exit 1}' \"/sys/fs/cgroup$cg/io.max\"; fi; "
           "done; test \"$count\" -gt 0")
    pools = json.loads(remote(STORAGE[0], ceph + "osd pool ls --format json"))
    auth = json.loads(remote(STORAGE[0], ceph + "auth ls --format json"))
    auth_hash = hashlib.sha256(json.dumps(sorted(auth["auth_dump"], key=lambda row: row["entity"]),
                                         sort_keys=True).encode()).hexdigest()
    secrets = json.loads(execute(kube + ["-n", "ceph-csi", "get", "secret", "csi-rbd-secret",
                                        "csi-cephfs-secret", "-o", "json"]))["items"]
    secret_hashes = sorted((row["metadata"]["name"], row["metadata"]["uid"], hashlib.sha256(
        json.dumps(row["data"], sort_keys=True).encode()).hexdigest()) for row in secrets)
    product = {name: checksum for name, checksum in source_dependencies(root, "CEPH-05").items()
               if not name.startswith("tests/")}
    return {"fsid": fsid, "dependencies": product,
            "nodes": sorted((row["metadata"]["name"], row["metadata"]["uid"]) for row in nodes),
            "fixtures": sorted((row["kind"], row["metadata"]["name"], row["metadata"]["uid"]) for row in fixtures),
            "pools": sorted(pools), "osd_flags": sorted(flags), "auth_sha256": auth_hash,
            "csi_credentials": secret_hashes,
            "disk_allowlist_sha256": hashlib.sha256((state / "disk-allowlist").read_bytes()).hexdigest(),
            "fault_tables_sha256": hashlib.sha256((state / "fault-tables").read_bytes()).hexdigest(),
            "rados_sha256": (state / "rados.sha256").read_text().strip(),
            "csi_marker_sha256": hashlib.sha256((state / "csi-marker").read_bytes()).hexdigest()}


def checkpoint(root: Path, state: Path, create: bool) -> None:
    path = state / "focused-baseline.json"
    actual = identities(root, state)
    # Normalize tuples before comparing the JSON checkpoint.
    actual = json.loads(json.dumps(actual))
    if create:
        if path.exists():
            raise ValueError("refusing to replace an existing baseline")
        part = path.with_suffix(".part")
        part.write_text(json.dumps(actual, sort_keys=True, indent=2) + "\n")
        part.chmod(0o600)
        part.replace(path)
    elif json.loads(path.read_text()) != actual:
        raise ValueError("baseline identities or product dependencies changed")
    print("CEPH_FOCUSED_BASELINE_VERIFY_PASS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "verify"])
    parser.add_argument("--root", type=Path, default=Path("/usr/local/kubeauto"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/kubeauto-ceph-test"))
    args = parser.parse_args()
    checkpoint(args.root, args.state, args.command == "create")


if __name__ == "__main__":
    main()
