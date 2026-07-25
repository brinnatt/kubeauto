#!/bin/bash
# Focused docker runtime gate on 137 after cri-dockerd product fix.
# Usage (on 138 as root): bash /usr/local/kubeauto/tests/helpers/delivery-docker-gate.sh
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:$PATH"
K=kubecli
C=deliver-docker
# 133 is disposable for docker runtime + reserved gate (avoids colliding with test137 on 137).
NODE="${DOCKER_GATE_NODE:-192.168.47.133}"
LOG=/tmp/kubeauto-delivery-docker-$(date +%Y%m%d%H%M%S).log
if [ -w /var/log ] 2>/dev/null; then
  LOG=/var/log/kubeauto-delivery-docker-$(date +%Y%m%d%H%M%S).log
fi
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }

nodes_ready(){
  local kc="$1" tries="${2:-48}"
  export KUBECONFIG="$BASE/clusters/$kc/kubectl.kubeconfig"
  for i in $(seq 1 "$tries"); do
    notready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2!="Ready"{print}' | wc -l)
    total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    if [ "$total" -ge 1 ] && [ "$notready" -eq 0 ]; then
      kubectl get nodes -o wide
      return 0
    fi
    sleep 10
  done
  kubectl get nodes -o wide || true
  return 1
}

# Contract reserved floor is 4Gi; lab VMs under ~6Gi cannot run live (kubelet rejects).
MEM_MI="$(sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE \
  "awk '/MemTotal/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 0)"
echo "docker_gate_node=$NODE mem_mi=$MEM_MI"
if [[ "$MEM_MI" -lt 6144 ]]; then
  echo "DOCKER_SKIP_LAB_UNDERSIZED: ${MEM_MI}Mi < 6Gi (contract ≥32Gi; reserved 4Gi)"
  grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/conf/config.yml"
  grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/conf/config.yml"
  grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/conf/config.yml"
  grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/conf/config.yml"
  echo DOCKER_GATE_SKIP_UNDERSIZED
  exit 0
fi

echo "========== destroy leftover clusters using $NODE =========="
for c in deliver-docker deliver-upgrade; do
  $K destroy "$c" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$c"
done

echo "========== hard reset $NODE =========="
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE bash -s <<'RST'
set -e
systemctl stop kubelet cri-dockerd docker containerd 2>/dev/null || true
pkill -9 kubelet 2>/dev/null || true
pkill -9 cri-dockerd 2>/dev/null || true
mount | awk '/kubelet|kubernetes|docker|containerd/ {print $3}' | sort -r | while read m; do umount -l "$m" 2>/dev/null || true; done
rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni/bin /var/lib/cni \
  /run/flannel /etc/systemd/system/kubelet.service /etc/systemd/system/kubelet.service.d \
  /etc/systemd/system/cri-dockerd.service /var/run/cri-dockerd.sock \
  /var/lib/docker /etc/docker /var/lib/containerd /etc/containerd
systemctl daemon-reload || true
systemctl reset-failed 2>/dev/null || true
systemctl disable kubelet docker containerd cri-dockerd 2>/dev/null || true
sed -i '/registry.talkschool.cn/d' /etc/hosts
echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts
echo RESET_OK
RST

echo "========== create docker cluster on $NODE =========="
$K new "$C" </dev/null || true
mkdir -p "$BASE/clusters/$C"
cat > "$BASE/clusters/$C/hosts" <<EOF
[etcd]
$NODE

[kube_master]
$NODE k8s_nodename='master-docker'

[kube_node]
$NODE

[harbor]
[ex_lb]
[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="docker"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.71.0.0/16"
CLUSTER_CIDR="172.24.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/${C}"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
if [ ! -f "$BASE/clusters/$C/config.yml" ]; then
  cp "$BASE/conf/config.yml" "$BASE/clusters/$C/config.yml"
fi
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$C/config.yml"

$K download -X </dev/null || true

echo "========== setup 90 =========="
if ! $K setup "$C" 90 </dev/null; then
  echo "--- node services ---"
  sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE \
    'systemctl status docker cri-dockerd kubelet --no-pager -l | head -120; ls -la /var/run/cri-dockerd.sock /run/containerd/containerd.sock 2>&1; journalctl -u kubelet -n 40 --no-pager'
  fail "setup 90"
fi

export KUBECONFIG="$BASE/clusters/$C/kubectl.kubeconfig"
if ! nodes_ready "$C" 48; then
  fail "nodes not Ready"
fi

rt=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)
echo "runtime=$rt"
echo "$rt" | grep -qi docker || fail "expected docker runtime, got $rt"

ok=0
for i in $(seq 1 36); do
  # kubectl --no-headers columns: NS NAME READY STATUS RESTARTS AGE
  bad=$(kubectl get pods -A --no-headers 2>/dev/null | awk '$4!="Running" && $4!="Completed"{print}' | wc -l)
  if [ "$bad" -eq 0 ]; then ok=1; break; fi
  sleep 10
done
[ "$ok" -eq 1 ] || { kubectl get pods -A -o wide; fail "system pods not Running"; }

docker0=$(kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.podIP} {.spec.hostNetwork}{"\n"}{end}' \
  | awk '$3!="true" && $2 ~ /^172\.17\./ {print}')
if [ -n "$docker0" ]; then
  echo "$docker0"
  fail "non-hostNetwork pods on docker0 — cri-dockerd CNI misconfigured"
fi

sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE \
  'systemctl is-active docker cri-dockerd kubelet; /usr/local/bin/cri-dockerd --version; grep -E "container-runtime-endpoint|pod-infra|network-plugin" /etc/systemd/system/kubelet.service /etc/systemd/system/cri-dockerd.service'

pass "docker runtime Ready ($rt) + system pods + CNI"

echo "========== reserved enablement (contract) =========="
grep -q 'KUBE_RESERVED_ENABLED: "yes"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED not yes"
grep -q 'SYS_RESERVED_ENABLED: "yes"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED not yes"
grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED_CPU not 1000m"
grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED_MEMORY not 1536Mi"
grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_CPU not 1000m"
grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_MEMORY not 2560Mi"
grep -q 'SYS_RESERVED_ENFORCE: "no"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_ENFORCE not no"
bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG" || fail "reserved allocatable"
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE \
  'systemctl show kubelet docker cri-dockerd -p Slice --value; test ! -d /sys/fs/cgroup/systemd/podruntime.slice.slice -a ! -d /sys/fs/cgroup/podruntime.slice.slice && echo NO_DOUBLE_SLICE'
pass "docker reserved RESERVED_ALLOCATABLE_PASS"

echo "DOCKER_GATE_PASS"
echo "LOG=$LOG"
