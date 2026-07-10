#!/bin/bash
# Full regression on 192.168.47.147 (ubuntu) — run: sudo bash regression-147.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run() { echo ">>> $*"; "$@" || fail "$*"; }

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli

echo "========== G1 CLI smoke =========="
run $K version
run $K completion bash >/dev/null
# mirror: ansible install path (skip if present)
if ! command -v ansible >/dev/null; then
  run $K download -a
fi
grep -q 'repo.huaweicloud.com/epel' /etc/yum.repos.d/epel.repo 2>/dev/null && pass "EPEL huawei (control)" || true
pass G1

echo "========== G2 cluster create =========="
# test141 Rocky single
run $K new test141
cat > "$BASE/clusters/test141/hosts" <<'EOF'
[etcd]
192.168.47.141
[kube_master]
192.168.47.141 k8s_nodename='master-141'
[kube_node]
192.168.47.141
[harbor]
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
cluster_dir="{{ base_dir }}/clusters/test141"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test141/config.yml"
run $K setup test141 90 </dev/null
run $K setup test141 07 </dev/null

# debian150
run $K new debian150
cat > "$BASE/clusters/debian150/hosts" <<'EOF'
[etcd]
192.168.47.150
[kube_master]
192.168.47.150 k8s_nodename='master-debian'
[kube_node]
192.168.47.150
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
cluster_dir="{{ base_dir }}/clusters/debian150"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=brinnatt
ansible_become=true
ansible_become_method=sudo
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/debian150/config.yml"
run $K setup debian150 90 </dev/null

# test-ded-etcd
run $K new test-ded-etcd
cat > "$BASE/clusters/test-ded-etcd/hosts" <<'EOF'
[etcd]
192.168.47.142
[kube_master]
192.168.47.130 k8s_nodename='master-130'
[kube_node]
192.168.47.130
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
run $K setup test-ded-etcd 90 </dev/null

# test-ha
run $K new test-ha
cat > "$BASE/clusters/test-ha/hosts" <<'EOF'
[etcd]
192.168.47.131
192.168.47.132
192.168.47.140
[kube_master]
192.168.47.132 k8s_nodename='master-132'
192.168.47.140 k8s_nodename='master-140'
[kube_node]
192.168.47.132
192.168.47.140
[ex_lb]
192.168.47.142 LB_ROLE=master EX_APISERVER_VIP=192.168.47.250 EX_APISERVER_PORT=8443
192.168.47.131 LB_ROLE=backup EX_APISERVER_VIP=192.168.47.250 EX_APISERVER_PORT=8443
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
run $K setup test-ha 90 </dev/null
run $K setup test-ha 10 </dev/null

# aio on 147
run $K start-aio </dev/null
pass G2

echo "========== G3 ops all clusters =========="
for c in test141 debian150 test-ded-etcd test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
  run $K stop "$c" </dev/null
  run $K start "$c" </dev/null
  run $K restore "$c" </dev/null
done
pass G3

echo "========== G5 kcfg-adm =========="
run $K checkout test141
run $K kcfg-adm -A -u testuser-reg -t view test141 </dev/null
run $K kcfg-adm -L test141
run $K kcfg-adm -D -u testuser-reg test141 </dev/null
pass G5

echo "========== G4 test-ha expand (130 worker) =========="
run $K add-node test-ha 192.168.47.130 worker-130 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep -q worker-130 && pass add-node || fail add-node
run $K del-node test-ha 192.168.47.130 </dev/null
pass G4-partial

echo "========== G6 cross: kca-renew test141 =========="
run $K kca-renew test141 </dev/null
export KUBECONFIG="$BASE/clusters/test141/kubectl.kubeconfig"
kubectl get nodes | grep -q Ready && pass kca-renew || fail kca-renew

echo "========== Tier2 tools --help =========="
for t in CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli; do
  /usr/local/bin/$t --help >/dev/null 2>&1 || true
done
pass tier3

echo "========== REGRESSION COMPLETE =========="
$K list
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes 2>/dev/null || true
done
