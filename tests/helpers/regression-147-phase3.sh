#!/bin/bash
# Phase3: test-ded-etcd, test-ha, aio, G3-G6 — sudo bash regression-147-phase3.sh
set -euo pipefail
exec > /var/log/kubeauto-regression-phase3.log 2>&1
BASE=/usr/local/kubeauto
K=kubecli
pass(){ echo "[PASS] $*"; }

echo "=== test-ded-etcd ==="
$K new test-ded-etcd 2>/dev/null || true
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
$K setup test-ded-etcd 90 </dev/null
pass test-ded-etcd

echo "=== test-ha ==="
$K new test-ha 2>/dev/null || true
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
$K setup test-ha 90 </dev/null
$K setup test-ha 10 </dev/null
pass test-ha

echo "=== aio ==="
$K start-aio </dev/null
pass aio

echo "=== G3 ops ==="
for c in test141 debian150 test-ded-etcd test-ha aio; do
  $K checkout "$c"
  $K backup "$c" </dev/null
  $K stop "$c" </dev/null
  $K start "$c" </dev/null
  $K restore "$c" </dev/null
  pass "G3 $c"
done

echo "=== G5 kcfg-adm ==="
$K checkout test141
$K kcfg-adm -A -u testuser-reg -t view test141 </dev/null
$K kcfg-adm -L test141
$K kcfg-adm -D -u testuser-reg test141 </dev/null
pass G5

echo "=== G4 add/del node ==="
$K add-node test-ha 192.168.47.130 worker-130 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep worker-130
$K del-node test-ha 192.168.47.130 </dev/null
pass G4

echo "=== G6 kca-renew ==="
$K kca-renew test141 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep Ready
pass G6-kca-renew

echo "=== G6 multi checkout ==="
for c in test141 debian150 test-ded-etcd test-ha aio; do $K checkout "$c"; $K backup "$c" </dev/null; done
pass X-G6-06

$K list
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"; kubectl get nodes
done
echo REGRESSION_COMPLETE
