#!/usr/bin/env bash
# Clean-lab gate for contract reserved floor: 2 CPU + 4Gi (kube 1000m/1536Mi + system 1000m/2560Mi).
# Contract node floor is ≥16C/32Gi. Lab peers are often 2C/≈3Gi — those MUST skip live setup
# (kubelet correctly refuses reservation > capacity). aio@138 (8C/15Gi) is the live proof node.
set -euo pipefail
BASE="$(cd "$(dirname "$0")/../.." && pwd)"
export PATH=/usr/local/bin:$PATH PYTHONPATH="$BASE"
cd "$BASE"
LOG=/tmp/kubeauto-reserved-4g-gate-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG BASE=$BASE"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
skip(){ echo "[SKIP] $*"; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

node_mem_mi(){
  local user="$1" host="$2"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$user@$host" \
    "awk '/MemTotal/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 0
}

echo "========== U0 unit (2C+4Gi contract) =========="
run bash "$BASE/tests/run_unit_tests.sh"
pass "unit"

echo "========== U1 lab wipe (keep 138 docker/registry) =========="
run bash "$BASE/tests/helpers/lab-wipe-nodes.sh"
docker start local_registry >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:5000/v2/_catalog >/dev/null && break
  sleep 2
done
curl -sf http://127.0.0.1:5000/v2/_catalog >/dev/null || fail "local_registry down after wipe"
pass "wipe+registry"

echo "========== U2 bootstrap essential images =========="
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
kubecli download -X </dev/null || fail "download -X"
pass "images"

echo "========== U3 aio cgroup2 reserved 4Gi (live) =========="
PUB="$(cat /home/ubuntu/.ssh/id_rsa.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || true)"
if [[ -n "${PUB:-}" ]]; then
  for ip in 192.168.47.133 192.168.47.137; do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$ip" \
      "mkdir -p /root/.ssh; chmod 700 /root/.ssh; grep -qxF '$PUB' /root/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys" || true
  done
fi
run kubecli start-aio </dev/null
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
for i in $(seq 1 72); do
  kubectl get nodes --no-headers 2>/dev/null | grep -q Ready && break
  sleep 5
done
kubectl get nodes -o wide
CFG="$BASE/clusters/aio/config.yml"
grep -q 'KUBE_RESERVED_CPU: "1000m"' "$CFG" || fail "aio config CPU not 1000m"
grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$CFG" || fail "aio config mem not 1536Mi"
grep -q 'SYS_RESERVED_CPU: "1000m"' "$CFG" || fail "aio config sys CPU not 1000m"
grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$CFG" || fail "aio config sys mem not 2560Mi"
grep -q 'SYS_RESERVED_ENFORCE: "no"' "$CFG" || fail "aio ENFORCE not no"
run bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG"
python3 - <<'PY'
import json, subprocess, os
raw = subprocess.check_output(["kubectl", "get", "node", "-o", "json"], env=os.environ)
n = json.loads(raw)["items"][0]
cap, alloc = n["status"]["capacity"], n["status"]["allocatable"]

def cpu(s):
    s = str(s)
    return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)

def mem(s):
    s = str(s)
    for u, m in (("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3)):
        if s.endswith(u):
            return int(s[: -len(u)]) * m
    return int(s)

cd = cpu(cap["cpu"]) - cpu(alloc["cpu"])
md = mem(cap["memory"]) - mem(alloc["memory"])
print(f"aio_delta_cpu_m={cd} aio_delta_mem={md}")
assert cd >= 2000, cd
assert md >= int(4096 * 1024**2 * 0.9), md
print("AIO_FLOOR_DELTA_OK")
PY
grep -A8 '^kubeReserved:' /var/lib/kubelet/config.yaml | grep -q '1536Mi'
grep -A8 '^systemReserved:' /var/lib/kubelet/config.yaml | grep -q '2560Mi'
grep -A5 enforceNodeAllocatable /var/lib/kubelet/config.yaml | grep -q kube-reserved
! grep -A5 enforceNodeAllocatable /var/lib/kubelet/config.yaml | grep -q 'system-reserved' || fail "aio still hard-enforces system-reserved"
systemctl show kubelet -p Slice --value | grep -q podruntime.slice
systemctl show containerd -p Slice --value | grep -q podruntime.slice
test ! -d /sys/fs/cgroup/podruntime.slice.slice
echo "system.slice memory.max=$(cat /sys/fs/cgroup/system.slice/memory.max 2>/dev/null || echo n/a)"
pass "aio-4g-reserved"

echo "========== U4 Rocky141 config pins + capacity gate =========="
MEM_MI="$(node_mem_mi root 192.168.47.137)"
echo "rocky141_mem_mi=$MEM_MI"
rm -rf "$BASE/clusters/test137"
kubecli new test137 </dev/null
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test137/config.yml"
grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/clusters/test137/config.yml" || fail "rocky mem pin"
grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/clusters/test137/config.yml" || fail "rocky sys mem pin"
grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/clusters/test137/config.yml" || fail "rocky cpu pin"
grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/clusters/test137/config.yml" || fail "rocky sys cpu pin"
grep -q 'SYS_RESERVED_ENFORCE: "no"' "$BASE/clusters/test137/config.yml" || fail "rocky enforce pin"
if [[ "$MEM_MI" -lt 6144 ]]; then
  skip "rocky live setup: node ${MEM_MI}Mi < 6Gi (contract ≥32Gi; kubelet rejects reservation>capacity — expected)"
else
  run bash "$BASE/tests/helpers/rocky-reserved-gate.sh"
fi
pass "rocky-4g-config-or-live"

echo "========== U5 docker@133 config pins + capacity gate =========="
MEM133="$(node_mem_mi root 192.168.47.133)"
echo "docker133_mem_mi=$MEM133"
if [[ "$MEM133" -lt 6144 ]]; then
  skip "docker live setup: node ${MEM133}Mi < 6Gi (contract ≥32Gi)"
  grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/conf/config.yml"
  grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/conf/config.yml"
else
  run bash "$BASE/tests/helpers/delivery-docker-gate.sh"
fi
pass "docker-4g-config-or-live"

echo "========== FINAL =========="
echo RESERVED_4G_GATE_PASS
echo "LOG=$LOG"
