#!/usr/bin/env python3
"""Real-host Ceph qualification fixtures and evidence; invoked by the fixed runner."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path

import yaml


SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]
DISKS = ["192.168.122." + suffix for suffix in ("135", "40", "72", "212", "165", "238")]
TARGETS = {"legacy": ["192.168.47.134", "192.168.47.138"],
           "modern": ["192.168.47.145", "192.168.47.146", "192.168.47.147"],
           "rocky9": ["192.168.122.238"]}
REQUIRED_MARKERS = {"CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
                    "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_IDEMPOTENCE_PASS",
                    "CEPH_OS_RECOVERY_PASS", "CEPH_OS_PERFORMANCE_PASS"}


def split_cgroup_compatible(metadata: str) -> bool:
    """Podman 4.9's GetOwnCgroupDisallowRoot prefers even a hybrid 0:: entry."""
    lines = metadata.splitlines()
    if not lines or lines[0] not in {"cgroup2fs", "tmpfs", "no-podman"}:
        raise ValueError("unknown host cgroup filesystem")
    return lines[0] in {"cgroup2fs", "no-podman"} or "0::/" not in lines[1:]


def runtime_preflight(profile: str, recover: bool = False) -> None:
    """Recover only a clean, leased qualification host's transient kernel state."""
    command = ("if command -v podman >/dev/null; then "
               "stat -fc %T /sys/fs/cgroup; cat /proc/self/cgroup; else echo no-podman; fi")
    for host in TARGETS[profile]:
        if split_cgroup_compatible(remote(host, command)):
            continue
        if not recover:
            raise ValueError("Podman split cgroups reject hybrid 0::/ on " + host
                             + "; run --ceph-lab-runtime-bootstrap after scoped cleanup")
        # A reboot must never interrupt a live or foreign storage/workload path.
        remote(host, "set -eu; test ! -e /etc/kubernetes; "
               "test ! -e /etc/ceph/kubeauto-owned-fsid; "
               "for unit in kubelet containerd; do "
               "state=$(systemctl is-active \"$unit\" || true); "
               "test \"$state\" = inactive -o \"$state\" = unknown; done; "
               "test -z \"$(podman ps -aq)\"; "
               "if command -v docker >/dev/null && systemctl is-active --quiet docker; then "
               "test -z \"$(docker ps -aq)\"; fi; "
               "if test -d /var/lib/ceph; then "
               "test -z \"$(find /var/lib/ceph -mindepth 1 -maxdepth 1 -type d)\"; fi")
        before = remote(host, "cat /proc/sys/kernel/random/boot_id")
        print("CEPH_LAB_RUNTIME_REBOOT_BEGIN host=" + host, flush=True)
        result = subprocess.run(SSH + ["root@" + host, "systemctl reboot"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode not in {0, 255}:
            raise RuntimeError("qualification host reboot was rejected: " + host)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                after = remote(host, "cat /proc/sys/kernel/random/boot_id")
                if after != before and split_cgroup_compatible(remote(host, command)):
                    print("CEPH_LAB_RUNTIME_REBOOT_PASS host=" + host, flush=True)
                    break
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            print("CEPH_LAB_RUNTIME_WAIT_HEARTBEAT host=" + host, flush=True)
            time.sleep(3)
        else:
            raise RuntimeError("qualification cgroup state did not recover after reboot: " + host)
    print("CEPH_QUALIFICATION_RUNTIME_PASS profile=" + profile, flush=True)


def run(argv: list[str], input_text: str | None = None, timeout: int = 900,
        stream_output: bool = False) -> str:
    output = None if stream_output else subprocess.PIPE
    result = subprocess.run(argv, input=input_text, text=True, stdout=output,
                            stderr=output, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed rc={result.returncode}: {argv[:5]}\n{(result.stderr or '')[-6000:]}")
    return (result.stdout or "").strip()


def remote(host: str, command: str) -> str:
    return run(SSH + ["root@" + host, command], input_text="", timeout=120)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare_hostnames(path: Path, profile: str) -> None:
    """Record all initial names; change only the supplied localhost fixture."""
    if path.exists():
        raise ValueError("hostname fixture already exists; scoped cleanup must finish first")
    compute = (["192.168.47." + suffix for suffix in ("134", "135", "136", "131", "132", "137", "138")]
               if profile == "legacy" else TARGETS[profile] if profile == "modern" else DISKS)
    rows = {}
    for host in dict.fromkeys(DISKS + compute):
        original = remote(host, "hostname -s")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", original):
            raise ValueError("invalid host identity: " + host)
        selected = "ceph-qual-145" if host == "192.168.47.145" and original == "localhost" else original
        if selected == "localhost":
            raise ValueError("unqualified localhost identity: " + host)
        rows[host] = {"original": original, "selected": selected}
    if len({row["selected"] for row in rows.values()}) != len(rows):
        raise ValueError("duplicate hostnames in qualification fixture")
    for host in compute:
        manifests("ceph-os-preflight", rows[host]["selected"].lower())
    atomic_json(path, rows)
    for host, row in rows.items():
        if row["original"] != row["selected"]:
            remote(host, "hostnamectl set-hostname " + row["selected"])


def restore_hostnames(path: Path) -> None:
    if not path.exists():
        return
    rows = json.loads(path.read_text())
    allowed = set(DISKS + ["192.168.47." + suffix for suffix in (
        "131", "132", "134", "135", "136", "137", "138", "145", "146", "147")])
    for host, row in rows.items():
        if host not in allowed or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", value)
                                          for value in row.values()):
            raise ValueError("foreign hostname fixture")
        if row["original"] != row["selected"]:
            current = remote(host, "hostname -s")
            if current not in {row["original"], row["selected"]}:
                raise ValueError("hostname changed outside fixture: " + host)
            remote(host, "hostnamectl set-hostname " + row["original"])
    path.unlink()


def rewrite_inventory(path: Path, hostnames: Path) -> None:
    rows = json.loads(hostnames.read_text())
    content = path.read_text()
    for host, row in rows.items():
        pattern = r"(?m)^" + re.escape(host) + r"\s+k8s_nodename='[^']+'$"
        content = re.sub(pattern, host + " k8s_nodename='" + row["selected"].lower() + "'", content)
    path.write_text(content)


def configure(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    # Qualification deliberately co-locates only MGR / non-RGW OSD and CSI.
    # Turn off unconstrained monitoring placement and preserve actual names.
    data.update(ceph_monitoring_install="no", ceph_dashboard_install="no", ENABLE_SETTING_HOSTNAME=False,
                ceph_public_network="192.168.122.0/24,192.168.47.0/24")
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def manifests(namespace: str, node: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", node):
        raise ValueError("invalid Kubernetes node identity")
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {
        "name": namespace, "labels": {"kubeauto.io/owner": "ceph-regression"}}}]
    for kind, storage_class, access in (("rbd", "ceph-rbd", "ReadWriteOnce"),
                                        ("cephfs", "cephfs", "ReadWriteMany")):
        items.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {
            "name": kind + "-data", "namespace": namespace}, "spec": {
            "accessModes": [access], "storageClassName": storage_class,
            "resources": {"requests": {"storage": "2Gi"}}}})
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "storage-client", "namespace": namespace,
           "labels": {"kubeauto.io/owner": "ceph-regression"}}, "spec": {
           "nodeName": node, "containers": [{"name": "client", "image": "registry.talkschool.cn:5000/brinnatt/busybox:1.37",
           "command": ["sh", "-c", "sleep 86400"], "volumeMounts": [
               {"name": kind, "mountPath": "/" + kind} for kind in ("rbd", "cephfs")]}],
           "volumes": [{"name": kind, "persistentVolumeClaim": {"claimName": kind + "-data"}}
                       for kind in ("rbd", "cephfs")]}}
    items.append(pod)
    return {"apiVersion": "v1", "kind": "List", "items": items}


def credential_snapshot(document: dict) -> list[tuple[str, str, str]]:
    """Compare CSI credentials; Helm release-history Secrets change on upgrades."""
    names = {"csi-rbd-secret", "csi-cephfs-secret"}
    rows = [row for row in document["items"] if row["metadata"]["name"] in names]
    if len(rows) != 2 or {row["metadata"]["name"] for row in rows} != names:
        raise ValueError("both CSI credential Secrets are required")
    return sorted((row["metadata"]["name"], row["metadata"]["uid"],
                   hashlib.sha256(json.dumps(row["data"], sort_keys=True).encode()).hexdigest()) for row in rows)


def qualify(args: argparse.Namespace) -> None:
    root, state = args.base, args.state
    rows = yaml.safe_load((root / "tests/ceph-test-matrix.yaml").read_text())["os_qualification"]["profiles"]
    targets = TARGETS[args.profile]
    selected = [row for row in rows if row["host"] in targets]
    if len(selected) != len(targets):
        raise ValueError("matrix differs from the fixed OS qualification fixture")
    key_type = "aes" if args.profile == "legacy" else "aes256k"
    if any(row["client_key_type"] != key_type for row in selected):
        raise ValueError("OS key policy differs from selected product configuration")
    kube = ["kubectl", "--kubeconfig=" + str(root / "clusters/ceph-gate/kubectl.kubeconfig")]
    cli = [str(root / ".venv/bin/python"), str(root / "kubecli.py")]
    bootstrap = DISKS[0]
    daemons = json.loads(remote(bootstrap, "/usr/local/sbin/cephadm shell -- ceph orch ps --refresh --format json"))
    hostnames = json.loads((state / "qualification-hostnames.json").read_text())
    drafts = state / "os-results" / args.run_id
    qualified = []
    for row in selected:
        host, profile = row["host"], row["id"]
        hostname = hostnames[host]["selected"]
        # kubelet canonicalizes its host/override to lowercase; Ceph does not.
        node = hostname.lower()
        print(f"CEPH_OS_STAGE_BEGIN id={profile} action=real-daemon-and-kernel-CSI", flush=True)
        meta = remote(host, '. /etc/os-release; printf "%s|%s|%s|%s" "$ID" "$VERSION_ID" "$(uname -r)" "$(python3 --version)"').split("|")
        if (meta[0], meta[1].split(".")[0]) != (row["os_id"], row["major"]):
            raise ValueError("actual OS differs from matrix: " + host)
        features = json.loads(run(SSH + ["root@" + host, "python3 - " + key_type],
                                 (root / "roles/ceph/files/kernel-client-check.py").read_text(), timeout=120))
        version = remote(host, "/usr/local/sbin/cephadm version")
        if not version.startswith("cephadm version 20.2.4 "):
            raise ValueError("unexpected cephadm version: " + host)
        checksum = remote(host, "sha256sum /usr/local/libexec/kubeauto/cephadm").split()[0]
        if checksum != "5b78c8d5772ef7c5c8619dac6ee0b36716b829338ea7a11c9f2b896626ab354f":
            raise ValueError("cephadm checksum differs: " + host)
        print(f"CEPH_HOST_VERSION_PASS id={profile}", flush=True)
        remote(host, "/usr/local/sbin/cephadm check-host --expect-hostname " + hostname)
        print(f"CEPH_CHECK_HOST_PASS id={profile}", flush=True)
        if not any(d["hostname"] == hostname and d["daemon_type"] == "mgr" and d["status_desc"] == "running" for d in daemons):
            raise ValueError("upstream remote MGR did not converge: " + host)
        print(f"CEPH_ORCH_REMOTE_PASS id={profile}", flush=True)
        namespace = "ceph-os-" + profile.removeprefix("CEPH-OS-").lower()
        fixture = manifests(namespace, node)
        run(kube + ["apply", "-f", "-"], json.dumps(fixture))
        run(kube + ["-n", namespace, "wait", "--for=condition=Ready", "pod/storage-client", "--timeout=15m"], timeout=920)
        actual_node = json.loads(run(kube + ["-n", namespace, "get", "pod/storage-client", "-o", "json"]))["spec"]["nodeName"]
        if actual_node != node:
            raise ValueError("CSI workload did not use selected OS")
        exec_cmd = kube + ["-n", namespace, "exec", "storage-client", "--", "sh", "-ceu"]
        expected = hashlib.sha256(profile.encode()).hexdigest()
        run(exec_cmd + [f"printf '%s' '{profile}' | tee /rbd/marker /cephfs/marker >/dev/null; sync"])
        def read_hashes() -> None:
            values = run(exec_cmd + ["sha256sum /rbd/marker /cephfs/marker"]).splitlines()
            if len(values) != 2 or any(line.split()[0] != expected for line in values):
                raise ValueError("OS CSI data hash mismatch: " + profile)
        read_hashes()
        # krbd defaults to prefer-crc (Ceph v20.2.4 src/krbd.cc). Require
        # the product's explicit secure request, including MON connections.
        # Never print config_info: it may include authentication material.
        secure_mounts = remote(host, "findmnt -rn -t ceph -o OPTIONS")
        if "ms_mode=secure" not in secure_mounts:
            raise ValueError("CephFS mount lacks secure msgr2: " + host)
        remote(host, "set -eu; count=0; for path in /sys/bus/rbd/devices/*/config_info; do "
                     "test -f \"$path\" || continue; grep -q 'ms_mode=secure' \"$path\"; count=$((count+1)); "
                     "done; test \"$count\" -gt 0")
        samples = []
        for attempt in range(16):
            start = time.monotonic_ns()
            run(exec_cmd + ["dd if=/dev/zero of=/rbd/performance bs=1M count=16 conv=fsync >/dev/null 2>&1; "
                            "dd if=/dev/zero of=/cephfs/performance bs=1M count=16 conv=fsync >/dev/null 2>&1; sync"])
            samples.append((time.monotonic_ns() - start) / 1_000_000)
            print(f"CEPH_OS_WAIT_HEARTBEAT id={profile} action=fsync-sample iteration={attempt + 1}/16", flush=True)
        print(f"CEPH_OS_PERFORMANCE_PASS id={profile} fsync_samples=16 errors=0 p99_ms={max(samples):.2f}", flush=True)
        # Delete and re-create the Pod with the same PVCs: exercise RBD detach,
        # re-attach, secure CephFS remount and persistent data recovery.
        run(kube + ["-n", namespace, "delete", "pod/storage-client", "--wait=true", "--timeout=5m"])
        run(kube + ["apply", "-f", "-"], json.dumps(fixture["items"][-1]))
        run(kube + ["-n", namespace, "wait", "--for=condition=Ready", "pod/storage-client", "--timeout=15m"], timeout=920)
        read_hashes()
        print(f"CEPH_CSI_RBD_PASS id={profile}", flush=True)
        print(f"CEPH_CSI_CEPHFS_PASS id={profile}", flush=True)
        print(f"CEPH_OS_RECOVERY_PASS id={profile}", flush=True)
        payload = dict(id=profile, os_id=meta[0], major=meta[1].split(".")[0], host=host,
                       kernel=meta[2], python=meta[3], runtime=remote(host, "command -v podman >/dev/null && echo podman || echo docker"),
                       fixture_kind="real-host", run_id=args.run_id, source_sha256=args.fingerprint,
                       cephadm_sha256=checksum, secure_msgr2=features["secure_msgr2"], aes256k=features["aes256k"],
                       client_key_type=key_type, legacy_aes_risk_accepted=key_type == "aes",
                       product_command="kubecli setup ceph-gate 08; kubecli setup ceph-gate 90; kubecli setup ceph-gate 08",
                       performance={"operations": 16, "bytes_per_operation": 32 * 1024 * 1024, "errors": 0,
                                    "p95_ms": sorted(samples)[math.ceil(len(samples) * .95) - 1], "p99_ms": max(samples)},
                       markers=["CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
                                "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_OS_RECOVERY_PASS",
                                "CEPH_OS_PERFORMANCE_PASS"])
        atomic_json(drafts / (profile + ".json"), payload)
        qualified.append((profile, namespace, expected, payload))
    def identities() -> dict:
        result = {}
        for _, namespace, _, _ in qualified:
            objects = json.loads(run(kube + ["-n", namespace, "get", "pod,pvc", "-o", "json"]))
            result[namespace] = sorted((x["kind"], x["metadata"]["name"], x["metadata"]["uid"]) for x in objects["items"])
        secrets = json.loads(run(kube + ["-n", "ceph-csi", "get", "secret", "-o", "json"]))
        result["csi_secrets"] = credential_snapshot(secrets)
        return result
    before = identities()
    # Ansible failures are printed on stdout. Keep this product invocation in
    # the durable foreground log before cleanup removes its temporary events.
    run(cli + ["setup", "ceph-gate", "08", "-e", "ceph_allow_test_mappers=true"],
        input_text="", timeout=2400, stream_output=True)
    if before != identities():
        raise ValueError("second product execution replaced PVC, Pod or CSI credential")
    after_daemons = json.loads(remote(bootstrap, "/usr/local/sbin/cephadm shell -- ceph orch ps --format json"))
    stable = lambda rows: sorted((row["daemon_name"], row["hostname"], row["service_name"]) for row in rows)
    if stable(daemons) != stable(after_daemons):
        raise ValueError("second product execution changed daemon identity or placement")
    for profile, namespace, expected, payload in qualified:
        hashes = run(kube + ["-n", namespace, "exec", "storage-client", "--", "sha256sum", "/rbd/marker", "/cephfs/marker"])
        if any(line.split()[0] != expected for line in hashes.splitlines()) or len(hashes.splitlines()) != 2:
            raise ValueError("second product execution lost business data: " + profile)
        payload["markers"].append("CEPH_IDEMPOTENCE_PASS")
        atomic_json(drafts / (profile + ".json"), payload)
        print(f"CEPH_IDEMPOTENCE_PASS id={profile}", flush=True)
        run(kube + ["delete", "namespace", namespace, "--wait=true", "--timeout=10m"])
    # These remain drafts until the supervisor verifies durable exit and cleanup.
    print(f"CEPH_OS_DATA_PATHS_PASS profile={args.profile} hosts={len(selected)}", flush=True)


def finalize(args: argparse.Namespace) -> None:
    """Publish only current, complete real-host drafts from a finished clean run."""
    ssh = SSH + (["-J", args.jumper] if args.jumper else []) + [args.control]
    log = run(ssh + ["cat /tmp/kubeauto-ceph-live.log"], input_text="", timeout=120)
    rc = run(ssh + ["cat /tmp/kubeauto-ceph-gate.exit /tmp/kubeauto-ceph-gate.finalized"], input_text="")
    if rc.splitlines() != ["0", "0"] or re.search(r"CEPH_(?:STAGE|OS_PROBE)_FAIL|\[FAIL\]|Traceback", log):
        raise ValueError("qualification durable exit or failure markers are invalid")
    for marker in ("CEPH_OS_QUALIFICATION_BRANCH_PASS profile=" + args.profile,
                   "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"):
        if marker not in log:
            raise ValueError("qualification terminal evidence is missing: " + marker)
    matrix = args.base / "tests/ceph-test-matrix.yaml"
    content = matrix.read_text()
    rows = yaml.safe_load(content)["os_qualification"]["profiles"]
    directory = args.base / "logs/ceph-os-qualification" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "live.log").write_text(log)
    (directory / "durable.exit").write_text(rc)
    for row in rows:
        if row["host"] not in TARGETS[args.profile]:
            continue
        draft_path = "/var/lib/kubeauto-ceph-test/os-results/" + args.run_id + "/" + row["id"] + ".json"
        payload = json.loads(run(ssh + ["cat " + draft_path], input_text=""))
        if (payload["source_sha256"] != args.fingerprint or payload["id"] != row["id"]
                or payload["host"] != row["host"] or payload["run_id"] != args.run_id):
            raise ValueError("qualification draft source/host/run mismatch")
        performance = payload.get("performance", {})
        if ((payload.get("os_id"), payload.get("major")) != (row["os_id"], row["major"])
                or payload.get("cephadm_sha256") != yaml.safe_load(content)["versions"]["cephadm_sha256"]):
            raise ValueError("qualification draft OS/artifact mismatch")
        for key in ("kernel", "python", "runtime", "product_command"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise ValueError("qualification draft missing " + key)
        if not payload["product_command"].startswith("kubecli setup "):
            raise ValueError("qualification draft bypassed the product entry point")
        if (not REQUIRED_MARKERS <= set(payload["markers"]) or payload.get("fixture_kind") != "real-host"
                or payload.get("secure_msgr2") is not True or payload.get("client_key_type") != row["client_key_type"]
                or (row["client_key_type"] == "aes256k" and payload.get("aes256k") is not True)
                or (row["client_key_type"] == "aes" and payload.get("legacy_aes_risk_accepted") is not True)
                or performance.get("operations", 0) < 16 or performance.get("errors") != 0
                or not 0 < performance.get("p95_ms", 0) <= performance.get("p99_ms", 0)):
            raise ValueError("qualification draft lacks complete kernel, business or performance evidence")
        for marker in payload["markers"]:
            if marker + " id=" + row["id"] not in log:
                raise ValueError("qualification draft claims missing marker: " + marker)
        payload.update(durable_rc=0, failure_markers=0)
        payload["markers"] += ["CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"]
        path = directory / (row["id"] + ".json")
        atomic_json(path, payload)
        binding = {"path": str(path.relative_to(args.base)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        pattern = r"(?m)^(\s+- \{id: " + re.escape(row["id"]) + r",[^\n]*?, status: )(?:pending|fail|pass)(, evidence: ).*(\})$"
        content, count = re.subn(pattern, lambda match: match[1] + "pass" + match[2] + json.dumps(binding) + match[3], content)
        if count != 1:
            raise ValueError("cannot bind the exact qualification matrix row")
    # All profiles in this round are validated before the atomic matrix update.
    temporary = matrix.with_suffix(".part")
    temporary.write_text(content)
    temporary.replace(matrix)
    print("CEPH_OS_EVIDENCE_PUBLISHED profile=" + args.profile)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runtime = sub.add_parser("runtime-preflight")
    runtime.add_argument("--profile", choices=TARGETS, required=True)
    runtime.add_argument("--recover", action="store_true")
    for name in ("prepare-hostnames", "inventory", "restore-hostnames", "config"):
        child = sub.add_parser(name)
        child.add_argument("--path", type=Path, required=True)
        if name == "prepare-hostnames":
            child.add_argument("--profile", choices=TARGETS, required=True)
        elif name == "inventory":
            child.add_argument("--hostnames", type=Path, required=True)
    child = sub.add_parser("qualify")
    child.add_argument("--profile", choices=TARGETS, required=True)
    child.add_argument("--base", type=Path, required=True)
    child.add_argument("--state", type=Path, required=True)
    child.add_argument("--run-id", required=True)
    child.add_argument("--fingerprint", required=True)
    final = sub.add_parser("finalize")
    final.add_argument("--profile", choices=TARGETS, required=True)
    final.add_argument("--base", type=Path, required=True)
    final.add_argument("--run-id", required=True)
    final.add_argument("--fingerprint", required=True)
    final.add_argument("--control", required=True)
    final.add_argument("--jumper", default="")
    args = parser.parse_args()
    if args.command == "runtime-preflight":
        if args.recover:
            with open("/var/lock/kubeauto-ceph-priority.lock", "a") as storage, \
                    open("/var/lock/kubeauto-ceph-compute.lock", "a") as compute:
                fcntl.flock(storage, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(compute, fcntl.LOCK_EX | fcntl.LOCK_NB)
                runtime_preflight(args.profile, recover=True)
        else:
            runtime_preflight(args.profile)
    elif args.command == "prepare-hostnames":
        prepare_hostnames(args.path, args.profile)
    elif args.command == "restore-hostnames":
        restore_hostnames(args.path)
    elif args.command == "inventory":
        rewrite_inventory(args.path, args.hostnames)
    elif args.command == "config":
        configure(args.path)
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) or not re.fullmatch(r"[a-f0-9]{64}", args.fingerprint):
            raise ValueError("invalid qualification run identity")
        if args.command == "finalize":
            finalize(args)
        else:
            qualify(args)


if __name__ == "__main__":
    main()
