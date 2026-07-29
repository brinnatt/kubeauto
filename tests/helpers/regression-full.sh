#!/bin/bash
# Full enterprise regression on 192.168.47.138 — non-interactive, all matrix groups.
# sudo bash /tmp/regression-full.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-full-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli
PW=123456
pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }
run_system_access(){
  local output status
  echo ">>> $K system -a $*"
  set +e
  output=$("$K" system -a "$@" 2>&1)
  status=$?
  set -e
  printf '%s\n' "$output"
  [[ "$status" -eq 0 ]] || fail "system -a command failed"
  if grep -Eq '\[(CRASH|FAILED)\]' <<<"$output"; then
    fail "system -a reported a host failure"
  fi
  return 0
}

nodes_ready(){
  local c="$1" tries="${2:-30}"
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    if kubectl get nodes --no-headers 2>/dev/null | grep -q Ready; then
      echo "[WAIT] cluster=$c nodes=Ready attempt=$((i+1))/$tries"
      return 0
    fi
    i=$((i+1))
    echo "[WAIT] cluster=$c nodes=not-ready attempt=$i/$tries next_check=10s"
    sleep 10
  done
  kubectl get nodes -o wide 2>/dev/null || true
  return 1
}

# Wait until a named node appears (and optionally is Ready). Avoids race after add-node/add-master.
wait_node(){
  local c="$1" name="$2" tries="${3:-36}"
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    if kubectl get nodes --no-headers 2>/dev/null | awk '{print $1,$2}' | grep -E "^${name}[[:space:]]+Ready" >/dev/null; then
      echo "[WAIT] cluster=$c node=$name status=Ready attempt=$((i+1))/$tries"
      return 0
    fi
    i=$((i+1))
    echo "[WAIT] cluster=$c node=$name status=not-ready attempt=$i/$tries next_check=5s"
    sleep 5
  done
  kubectl get nodes -o wide 2>/dev/null || true
  return 1
}

echo "========== G0 preflight =========="
python3 -c "
from common.utils import run_command
from common.ansible_python import ansible_python_policy, format_policy_summary
r = run_command(['ansible','--version'], capture=True, check=False)
assert r.returncode==0, r.stderr or r.stdout
print(format_policy_summary(ansible_python_policy()))
"
run bash "$BASE/tests/run_unit_tests.sh"
# Ensure brinnatt mirrors exist for this control node (idempotent)
if [ -x "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" ]; then
  bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh"
fi
python3 -c "
from common.constants import KubeConstant
from service.cluster.registry import _talkedu_mirror
kc = KubeConstant()
for imgs in kc.component_images.values():
    for img in imgs:
        assert img.startswith('brinnatt/'), img
        assert _talkedu_mirror(img, kc.v_talkedu_registry)
print('brinnatt_contract_ok', len(kc.component_images), 'components')
"
pass G0

echo "========== G1 CLI smoke =========="
run $K version
run $K list
run $K completion bash >/dev/null
run $K completion zsh >/dev/null
pass G1

echo "========== G5 system SSH + docker =========="
# Lab nodes are rebuilt repeatedly.  Clear every prior Rocky host key before
# password-authenticated key distribution; Paramiko correctly refuses a changed
# key and otherwise cannot refresh the authorized_keys bootstrap.
for ip in 192.168.47.128 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do
  ssh-keygen -R "$ip" 2>/dev/null || true
done
run_system_access --user root --password "$PW" 192.168.47.131-137 </dev/null
run_system_access --user brinnatt --password "$PW" 192.168.47.128 </dev/null
# When this script runs under sudo, ansible uses /root/.ssh — ensure root pubkey is on targets.
if [[ "$(id -u)" -eq 0 ]] && [[ -f /root/.ssh/id_rsa.pub ]]; then
  PUB="$(cat /root/.ssh/id_rsa.pub)"
  for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$ip" \
      "mkdir -p /root/.ssh; chmod 700 /root/.ssh; grep -qxF '$PUB' /root/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys" || true
  done
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no brinnatt@192.168.47.128 \
    "mkdir -p ~/.ssh; chmod 700 ~/.ssh; grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" || true
fi
# ansible-core 2.17 requires a supported target interpreter.  Rocky 8's
# platform Python is intentionally not an Ansible module runtime, so guarantee
# the same Python 3.9 installation the prepare role selects for fresh hosts.
for ip in 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137; do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$ip" \
    'command -v python3.9 >/dev/null 2>&1 || dnf install -y python39; python3.9 --version' \
    || fail "Python 3.9 preflight failed on $ip"
done
$K docker -e >/dev/null 2>&1 || true
pass G5-system

echo "========== G2 cluster (re)setup =========="
# test137 Rocky single + addons
$K new test137 2>/dev/null || true
cat > "$BASE/clusters/test137/hosts" <<'EOF'
[etcd]
192.168.47.137
[kube_master]
192.168.47.137 k8s_nodename='master-137'
[kube_node]
192.168.47.137
[harbor]
192.168.47.137 NEW_INSTALL=true
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.70.0.0/16"
CLUSTER_CIDR="172.23.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/test137"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test137/config.yml"
run $K setup test137 90 </dev/null
run $K setup test137 07 </dev/null
run $K setup test137 11 </dev/null
nodes_ready test137 || fail "test137 not Ready"

# debian128
$K new debian128 2>/dev/null || true
cat > "$BASE/clusters/debian128/hosts" <<'EOF'
[etcd]
192.168.47.128
[kube_master]
192.168.47.128 k8s_nodename='master-debian'
[kube_node]
192.168.47.128
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.71.0.0/16"
CLUSTER_CIDR="172.24.0.0/16"
NODE_PORT_RANGE="31000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/debian128"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=brinnatt
ansible_become=true
ansible_become_method=sudo
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/debian128/config.yml"
# Debian 128 is the OS/privilege compatibility node (about 4 GiB in this lab),
# not the dedicated 8C/32Gi allocation-contract host.  Kubernetes rejects a
# kube+system reservation that exceeds capacity; keep the delivery defaults
# unchanged and disable the optional reservation only for this undersized case.
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/debian128/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/debian128/config.yml"
run $K setup debian128 90 </dev/null
nodes_ready debian128 || fail "debian128 not Ready"

# test-ded-etcd
$K new test-ded-etcd 2>/dev/null || true
cat > "$BASE/clusters/test-ded-etcd/hosts" <<'EOF'
[etcd]
192.168.47.136
[kube_master]
192.168.47.134 k8s_nodename='master-134'
[kube_node]
192.168.47.131
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.73.0.0/16"
CLUSTER_CIDR="172.26.0.0/16"
NODE_PORT_RANGE="32000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/test-ded-etcd"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test-ded-etcd/config.yml"
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/test-ded-etcd/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/test-ded-etcd/config.yml"
run $K setup test-ded-etcd 90 </dev/null
nodes_ready test-ded-etcd || fail "test-ded-etcd not Ready"

# This topology shares 131/134/136 with test-ha, so exercise its complete
# lifecycle while its own etcd data and CA are still present.  Keeping only the
# control-plane directory after the nodes are rebuilt would create a stale
# cluster entry whose credentials can never match the replacement HA cluster.
echo "========== G3 dedicated-etcd lifecycle before topology reuse =========="
run $K checkout test-ded-etcd
run $K backup test-ded-etcd </dev/null
run $K stop test-ded-etcd </dev/null
run $K start test-ded-etcd </dev/null
run $K restore test-ded-etcd </dev/null
nodes_ready test-ded-etcd || fail "G3 test-ded-etcd not Ready after restore"
pass "G3 test-ded-etcd"
run $K destroy test-ded-etcd </dev/null

# test-ded-etcd and test-ha intentionally reuse 131-136.  A member's etcd
# data directory belongs to exactly one initial cluster; carrying it into the
# HA inventory causes an etcd cluster-ID mismatch and leaves new masters unable
# to register.  Preserve the completed dedicated-etcd evidence, then reset the
# shared lab nodes before creating the independent HA topology.
run bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only \
  192.168.47.131 192.168.47.132 192.168.47.133 \
  192.168.47.134 192.168.47.135 192.168.47.136

# test-ha + ex_lb
$K new test-ha 2>/dev/null || true
cat > "$BASE/clusters/test-ha/hosts" <<'EOF'
[etcd]
192.168.47.134
192.168.47.135
192.168.47.136
[kube_master]
192.168.47.134 k8s_nodename='master-134'
192.168.47.135 k8s_nodename='master-135'
192.168.47.136 k8s_nodename='master-136'
[kube_node]
192.168.47.131
192.168.47.132
[ex_lb]
192.168.47.136 LB_ROLE=master EX_APISERVER_VIP=192.168.47.250 EX_APISERVER_PORT=8443
192.168.47.133 LB_ROLE=backup EX_APISERVER_VIP=192.168.47.250 EX_APISERVER_PORT=8443
[harbor]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.72.0.0/16"
CLUSTER_CIDR="172.25.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/test-ha"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test-ha/config.yml"
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/test-ha/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/test-ha/config.yml"
run $K setup test-ha 90 </dev/null
run $K setup test-ha 10 </dev/null
nodes_ready test-ha || fail "test-ha not Ready"

# aio (138 localhost)
# This is an existence/reuse probe, not the post-install readiness gate.  A
# clean regression has no aio cluster, so one quick check is sufficient before
# start-aio; the full 30-attempt gate remains immediately below.
if nodes_ready aio 1 2>/dev/null; then
  pass "aio already live"
else
  $K destroy aio </dev/null 2>/dev/null || rm -rf "$BASE/clusters/aio"
  run $K start-aio </dev/null
fi
nodes_ready aio || fail "aio not Ready"
pass G2

echo "========== G11 Node Allocatable reserved (contract) =========="
# Docs: https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/
# Must cover cgroup v2 (aio/debian) and cgroup v1 hybrid (Rocky test137).
for c in aio test137; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  grep -E '^KUBE_RESERVED_|^SYS_RESERVED_' "$BASE/clusters/$c/config.yml" || true
  grep -q 'KUBE_RESERVED_ENABLED: "yes"' "$BASE/clusters/$c/config.yml" || fail "$c KUBE_RESERVED not yes"
  grep -q 'SYS_RESERVED_ENABLED: "yes"' "$BASE/clusters/$c/config.yml" || fail "$c SYS_RESERVED not yes"
  grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/clusters/$c/config.yml" || fail "$c KUBE_RESERVED_CPU not 1000m"
  grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/clusters/$c/config.yml" || fail "$c KUBE_RESERVED_MEMORY not 1536Mi"
  grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/clusters/$c/config.yml" || fail "$c SYS_RESERVED_CPU not 1000m"
  grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/clusters/$c/config.yml" || fail "$c SYS_RESERVED_MEMORY not 2560Mi"
  grep -q 'SYS_RESERVED_ENFORCE: "no"' "$BASE/clusters/$c/config.yml" || fail "$c SYS_RESERVED_ENFORCE not no"
  bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG" || fail "reserved verify $c"
  pass "G11-reserved $c"
done
# Local aio: runtime must sit under podruntime.slice (kubeReserved enforcement)
systemctl show kubelet -p Slice --value | tee /tmp/aio-kubelet-slice.txt
systemctl show containerd -p Slice --value | tee /tmp/aio-containerd-slice.txt
grep -q podruntime.slice /tmp/aio-kubelet-slice.txt || fail "kubelet not in podruntime.slice"
grep -q podruntime.slice /tmp/aio-containerd-slice.txt || fail "containerd not in podruntime.slice"
pass "G11-aio-slice-placement"

echo "========== G3 ops all clusters =========="
for c in test137 debian128 test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
  run $K stop "$c" </dev/null
  run $K start "$c" </dev/null
  run $K restore "$c" </dev/null
  nodes_ready "$c" || fail "G3 $c not Ready after restore"
  pass "G3 $c"
done

echo "========== G5 kcfg-adm =========="
run $K checkout test137
run $K kcfg-adm -A -u testuser-reg -t view test137 </dev/null
run $K kcfg-adm -L test137
run $K kcfg-adm -D -u testuser-reg test137 </dev/null
pass G5-kcfg

echo "========== G4 node expand/shrink =========="
run $K checkout test-ha
# 133 is the active ex-lb backup and must never be cleaned as a disposable
# Kubernetes node.  Free the ordinary worker 132 through the supported shrink
# path, then reuse that role-neutral host for every G4 lifecycle operation.
run $K del-node test-ha 192.168.47.132 </dev/null
run $K add-node test-ha 192.168.47.132 worker-132 </dev/null
wait_node test-ha worker-132 || fail add-node
run $K del-node test-ha 192.168.47.132 </dev/null
pass G4-node

echo "========== G4 add-master + del-master (132 disposable) =========="
run $K add-master test-ha 192.168.47.132 master-132 </dev/null
wait_node test-ha master-132 || fail add-master
run $K backup test-ha </dev/null
run $K del-master test-ha 192.168.47.132 </dev/null
pass G4-master

echo "========== G4 add-etcd + del-etcd (132) =========="
run $K add-etcd test-ha 192.168.47.132 etcd-132 </dev/null
grep -q '192.168.47.132' "$BASE/clusters/test-ha/hosts"
run $K del-etcd test-ha 192.168.47.132 </dev/null
pass G4-etcd

echo "========== G6 cross kca-renew + multi checkout =========="
run $K kca-renew test137 </dev/null
export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
nodes_ready test137 || fail kca-renew-test137
run $K kca-renew aio </dev/null
for c in test137 debian128 test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
done
pass G6

echo "========== G8 Tier2 CNI/proxy on 133 =========="
# Prime local registry so CNI matrix never ImagePullBackOff mid-run (six-repo tags).
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
for comp in flannel cilium kube-router kube-ovn; do
  kubecli download -E "$comp" </dev/null || fail "upload $comp before G8"
done
for spec in "test133-flannel flannel ipvs" "test133-cilium cilium ipvs" "test133-kr kube-router ipvs" "test133-ovn kube-ovn ipvs" "test133-iptables calico iptables"; do
  set -- $spec
  cname=$1; net=$2; proxy=$3
  $K destroy "$cname" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$cname"
  # wipe 133 between CNI swaps to avoid leftover CNI/cgroup pollution
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.133 \
    'set +e; systemctl stop kubelet containerd; rm -rf /var/lib/kubelet /etc/kubernetes /etc/cni /var/lib/containerd /etc/containerd /etc/systemd/system/podruntime.slice /opt/kubeauto_prepare_tasks /etc/calico /run/flannel /etc/cilium; systemctl daemon-reload; grep -q registry.talkschool.cn /etc/hosts || echo "192.168.47.138    registry.talkschool.cn" >> /etc/hosts' || true
  $K new "$cname" 2>/dev/null || true
  bash "$BASE/tests/helpers/mk-cluster-133.sh" "$cname" "$net" "$proxy"
  sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$cname/config.yml"
  sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/$cname/config.yml"
  sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/$cname/config.yml"
  run $K setup "$cname" 90 </dev/null
  export KUBECONFIG="$BASE/clusters/$cname/kubectl.kubeconfig"
  nodes_ready "$cname" || fail "$cname $net"
  if [ "$net" = "cilium" ]; then
    img=$(kubectl -n kube-system get deploy -l io.cilium/app=operator -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null || true)
    echo "cilium_operator_image=$img"
    echo "$img" | grep -q 'cilium-operator-generic:' || fail "cilium operator image missing"
    echo "$img" | grep -q 'generic-generic' && fail "cilium double-generic regression"
  fi
  run $K destroy "$cname" </dev/null
  pass "Tier2 $cname ($net/$proxy)"
done

echo "========== G8b addon image smoke on aio =========="
# aio shares the control-node local registry (avoids stale registry.talkschool.cn → wrong IP on remote nodes).
run $K checkout aio
CFG="$BASE/clusters/aio/config.yml"
# Enable image-path-sensitive optional addons (skip nacos: needs external MySQL).
grep -q '^openebs_lvm_enabled:' "$CFG" || echo 'openebs_lvm_enabled: "no"' >> "$CFG"
sed -i 's/^dashboard_install:.*/dashboard_install: "yes"/' "$CFG"
sed -i 's/^local_path_provisioner_install:.*/local_path_provisioner_install: "yes"/' "$CFG"
sed -i 's/^openebs_install:.*/openebs_install: "yes"/' "$CFG"
sed -i 's/^openebs_lvm_enabled:.*/openebs_lvm_enabled: "no"/' "$CFG"
sed -i 's/^prom_install:.*/prom_install: "yes"/' "$CFG"
sed -i 's/^minio_install:.*/minio_install: "yes"/' "$CFG"
sed -i 's/^minio_pool_servers:.*/minio_pool_servers: 1/' "$CFG"
sed -i 's|^minio_storage_class:.*|minio_storage_class: "local-path"|' "$CFG"
sed -i 's/^ingress_nginx_install:.*/ingress_nginx_install: "yes"/' "$CFG"
sed -i 's/^rocketmq_install:.*/rocketmq_install: "no"/' "$CFG"
sed -i 's/^nacos_install:.*/nacos_install: "no"/' "$CFG"
# Ensure addon images present
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
for comp in dashboard prometheus local-path-provisioner openebs minio ingress-nginx; do
  $K download -E "$comp" </dev/null || true
done
run $K setup aio 07 </dev/null
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
# Wait / assert key pods using mirrored images
wait_ns_ready() {
  local ns="$1" tries="${2:-36}"
  local i=0 pod_counts total bad
  while [ "$i" -lt "$tries" ]; do
    if ! pod_counts=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | awk '
      $3 != "Completed" && $3 != "Succeeded" {
        total++
        split($2, ready, "/")
        if ($3 != "Running" || ready[1] != ready[2]) bad++
      }
      END { print total + 0, bad + 0 }
    '); then
      pod_counts="0 1"
    fi
    read -r total bad <<< "$pod_counts"
    if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then
      echo "[WAIT] namespace=$ns status=Ready total=$total attempt=$((i+1))/$tries"
      return 0
    fi
    i=$((i+1))
    echo "[WAIT] namespace=$ns status=not-ready total=$total bad=$bad attempt=$i/$tries next_check=10s"
    sleep 10
  done
  kubectl get pods -n "$ns" -o wide || true
  return 1
}
kubectl get pods -A | grep -E 'dashboard|prometheus|openebs|local-path|minio|ImagePull' || true
# Assert no ImagePullBackOff on addon namespaces
if kubectl get pods -A --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull'; then
  kubectl get pods -A | grep -E 'ImagePullBackOff|ErrImagePull' || true
  fail "addon ImagePullBackOff"
fi
# Soft waits
wait_ns_ready kube-system 24 || true
wait_ns_ready monitor 36 || fail "prometheus pods not Ready"
wait_ns_ready openebs 24 || fail "openebs pods not Ready"
# Prove deploy images are brinnatt/*
kubectl -n monitor get prometheus -o yaml 2>/dev/null | grep -E 'image:|repository:' | head -20 || true
prom_img=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "prom_operator_image=$prom_img"
echo "$prom_img" | grep -q 'brinnatt/prometheus-operator' || fail "prometheus-operator not brinnatt"
op_img=$(kubectl -n openebs get deploy openebs-localpv-provisioner -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "openebs_provisioner_image=$op_img"
echo "$op_img" | grep -q 'brinnatt/provisioner-localpv' || fail "openebs provisioner not brinnatt"
dash_img=$(kubectl -n kube-system get deploy kubernetes-dashboard-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || \
  kubectl -n kube-system get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.containers[*]}{.image}{" "}{end}{"\n"}{end}' 2>/dev/null | grep -iE 'dashboard|kong' | head -1 || true)
echo "dashboard_related_image=$dash_img"
echo "$dash_img" | grep -q 'brinnatt/' || fail "dashboard image not brinnatt"
ing_img=$(kubectl -n ingress-nginx get deploy ingress-nginx-controller -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "ingress_image=$ing_img"
if [ -n "$ing_img" ]; then
  echo "$ing_img" | grep -q 'brinnatt/ingress-nginx-controller' || fail "ingress-nginx not brinnatt"
fi
pass "G8b-addons"

echo "========== Tier3 tools --help =========="
TOOL_DIR="$BASE/dist"
tier3_ok=0
tier3_skip=0
for t in CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli; do
  bin="$TOOL_DIR/$t"
  [ -x "$bin" ] || bin="/usr/local/bin/$t"
  if [ ! -x "$bin" ]; then
    echo "[SKIP] $t binary not present on control node"
    tier3_skip=$((tier3_skip+1))
    continue
  fi
  "$bin" --help >/dev/null 2>&1 || fail "$t --help"
  tier3_ok=$((tier3_ok+1))
done
if [ "$tier3_ok" -eq 0 ] && [ "$tier3_skip" -gt 0 ]; then
  echo "[SKIP] Tier3 — no tool binaries installed (not part of image-migration scope)"
else
  pass "Tier3 ($tier3_ok ok, $tier3_skip skipped)"
fi

echo "========== FINAL verification =========="
$K list
for c in test137 debian128 test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes
done
echo "--- test-ded-etcd ---"
echo "validated and destroyed before shared nodes were rebuilt as test-ha"
echo REGRESSION_FULL_COMPLETE
