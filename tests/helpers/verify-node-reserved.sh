#!/usr/bin/env bash
# Verify kube/system reserved enablement against Node Allocatable (official formula).
# Docs: https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/
# Usage: verify-node-reserved.sh <kubeconfig> [node-name]
set -euo pipefail

KUBECONFIG_PATH="${1:?kubeconfig required}"
NODE_NAME="${2:-}"
export KUBECONFIG="$KUBECONFIG_PATH"

if [[ -z "$NODE_NAME" ]]; then
  NODE_NAME="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
fi

echo "== reserved verify node=$NODE_NAME =="

if systemctl is-active kubelet >/dev/null 2>&1; then
  systemctl is-active kubelet
  systemctl is-active podruntime.slice || true
  if [[ -f /var/lib/kubelet/config.yaml ]]; then
    grep -E '^(kubeReserved|systemReserved|kubeReservedCgroup|systemReservedCgroup|enforceNodeAllocatable|cgroupDriver)' -A6 \
      /var/lib/kubelet/config.yaml || true
  fi
  # systemd: /podruntime must resolve to podruntime.slice (not .slice.slice) — k8s#78629
  if grep -q 'kubeReservedCgroup: /podruntime$' /var/lib/kubelet/config.yaml 2>/dev/null; then
    test -d /sys/fs/cgroup/podruntime.slice || test -d /sys/fs/cgroup/systemd/podruntime.slice
    if [[ -d /sys/fs/cgroup/podruntime.slice.slice ]] || [[ -d /sys/fs/cgroup/systemd/podruntime.slice.slice ]]; then
      echo "FAIL: double-slice podruntime.slice.slice exists (systemd naming bug)"
      exit 1
    fi
  fi
fi

json="$(kubectl get node "$NODE_NAME" -o json)"
python3 - "$json" <<'PY'
import json, sys

node = json.loads(sys.argv[1])
status = node["status"]
cap = status["capacity"]
alloc = status["allocatable"]

def parse_cpu(s: str) -> int:
    s = str(s)
    if s.endswith("m"):
        return int(s[:-1])
    return int(float(s) * 1000)

def parse_mem(s: str) -> int:
    s = str(s)
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "K": 1000, "M": 1000**2, "G": 1000**3}
    for u, m in units.items():
        if s.endswith(u):
            return int(s[: -len(u)]) * m
    return int(s)

cap_cpu, alloc_cpu = parse_cpu(cap["cpu"]), parse_cpu(alloc["cpu"])
cap_mem, alloc_mem = parse_mem(cap["memory"]), parse_mem(alloc["memory"])
cpu_delta = cap_cpu - alloc_cpu
mem_delta = cap_mem - alloc_mem

print(f"capacity.cpu={cap['cpu']} allocatable.cpu={alloc['cpu']} delta_m={cpu_delta}")
print(f"capacity.memory={cap['memory']} allocatable.memory={alloc['memory']} delta_bytes={mem_delta}")

# Contract baseline (≥16C/32Gi nodes): kube 1000m+1536Mi + system 1000m+2560Mi = 2 CPU + 4Gi
if cpu_delta < 2000:
    print(f"FAIL: cpu reserved delta {cpu_delta}m < 2000m (expect kube+system reserved)")
    sys.exit(1)

# memory: 1536Mi + 2560Mi reserved + evictionHard memory.available 300Mi
min_mem = (1536 + 2560 + 300) * 1024**2
if mem_delta < min_mem * 0.9:
    print(f"FAIL: memory reserved delta {mem_delta} < ~{min_mem} (kube+system+eviction)")
    sys.exit(1)

conds = {c["type"]: c["status"] for c in status.get("conditions", [])}
if conds.get("Ready") != "True":
    print("FAIL: node not Ready")
    sys.exit(1)

print("RESERVED_ALLOCATABLE_PASS")
PY
