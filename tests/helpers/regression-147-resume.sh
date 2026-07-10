#!/bin/bash
# Resume regression from debian150 — sudo bash regression-147-resume.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-resume.log
exec > >(tee -a "$LOG") 2>&1
BASE=/usr/local/kubeauto
K=kubecli
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; exit 1; }
run() { echo ">>> $*"; "$@" || fail "$*"; }

echo "========== resume: debian150 =========="
run $K setup debian150 90 </dev/null
run $K setup debian150 07 </dev/null

echo "========== test-ded-etcd =========="
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

echo "========== test-ha =========="
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

echo "========== aio =========="
run $K start-aio </dev/null

echo "========== G3 all clusters =========="
for c in test141 debian150 test-ded-etcd test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
  run $K stop "$c" </dev/null
  run $K start "$c" </dev/null
  run $K restore "$c" </dev/null
done

echo "========== G5 kcfg-adm =========="
run $K checkout test141
run $K kcfg-adm -A -u testuser-reg -t view test141 </dev/null
run $K kcfg-adm -L test141
run $K kcfg-adm -D -u testuser-reg test141 </dev/null

echo "========== G4 add/del node =========="
run $K add-node test-ha 192.168.47.130 worker-130 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep -q worker-130 && pass add-node
run $K del-node test-ha 192.168.47.130 </dev/null

echo "========== G6 kca-renew =========="
run $K kca-renew test141 </dev/null
export KUBECONFIG="$BASE/clusters/test141/kubectl.kubeconfig"
kubectl get nodes | grep -q Ready && pass kca-renew

echo "========== G6 checkout multi-cluster =========="
for c in test141 debian150 test-ded-etcd test-ha aio; do run $K checkout "$c"; run $K backup "$c" </dev/null; done
pass X-G6-06

echo "========== FINAL =========="
$K list
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"; kubectl get nodes 2>/dev/null || true
done
echo REGRESSION_COMPLETE
