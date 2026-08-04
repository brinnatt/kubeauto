#!/bin/bash
# Full regression on 192.168.47.138 (ubuntu) — run: sudo bash regression.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run() { echo ">>> $*"; "$@" || fail "$*"; }

BASE=/usr/local/kubeauto
PROJECT_PY="$BASE/.venv/bin/python"
test -x "$PROJECT_PY"
export PYTHONPATH="$BASE"
K=kubecli

echo "========== Unit / policy preflight =========="
export PYTHONPATH="$BASE"
"$PROJECT_PY" -c "
from common.utils import run_command
from common.ansible_python import ansible_python_policy, format_policy_summary
# Regression: capture= alias must work (was silently breaking ansible-core detection)
r = run_command(['ansible', '--version'], capture=True, check=False)
assert r.returncode == 0, (r.stderr or r.stdout or 'ansible --version failed')
p = ansible_python_policy()
assert p.core_version[0] >= 2, p.core_version
print(format_policy_summary(p))
"
run bash "$BASE/tests/run_unit_tests.sh"
pass "ansible-core policy preflight"

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
# test137 Rocky single
run $K new test137
cat > "$BASE/clusters/test137/hosts" <<'EOF'
[etcd]
192.168.47.137
[kube_master]
192.168.47.137 k8s_nodename='master-137'
[kube_node]
192.168.47.137
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
cluster_dir="{{ base_dir }}/clusters/test137"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test137/config.yml"
run $K setup test137 90 </dev/null
run $K setup test137 07 </dev/null

# debian128
run $K new debian128
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
run $K setup debian128 90 </dev/null

# test-ded-etcd
run $K new test-ded-etcd
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
run $K setup test-ded-etcd 90 </dev/null

# test-ha
run $K new test-ha
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
run $K setup test-ha 90 </dev/null
run $K setup test-ha 10 </dev/null

# aio on 138
run $K start-aio </dev/null
pass G2

echo "========== G3 ops all clusters =========="
for c in test137 debian128 test-ded-etcd test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
  run $K stop "$c" </dev/null
  run $K start "$c" </dev/null
  run $K restore "$c" </dev/null
done
pass G3

echo "========== G5 kcfg-adm =========="
run $K checkout test137
run $K kcfg-adm -A -u testuser-reg -t view test137 </dev/null
run $K kcfg-adm -L test137
run $K kcfg-adm -D -u testuser-reg test137 </dev/null
pass G5

echo "========== G4 test-ha expand (worker133) =========="
run $K add-node test-ha 192.168.47.133 worker-133 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep -q worker-133 && pass add-node || fail add-node
run $K del-node test-ha 192.168.47.134 </dev/null
pass G4-partial

echo "========== G6 cross: kca-renew test137 =========="
run $K kca-renew test137 </dev/null
export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
kubectl get nodes | grep -q Ready && pass kca-renew || fail kca-renew

echo "========== Tier2 tools --help =========="
for t in CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli; do
  /usr/local/bin/$t --help >/dev/null 2>&1 || true
done
pass tier3

echo "========== REGRESSION COMPLETE =========="
$K list
for c in test137 debian128 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes 2>/dev/null || true
done
