#!/bin/bash
# Phase3: test-ded-etcd, test-ha, aio, G3-G6 — sudo bash regression-phase3.sh
set -euo pipefail
exec > /var/log/kubeauto-regression-phase3.log 2>&1
BASE=/usr/local/kubeauto
K=kubecli
pass(){ echo "[PASS] $*"; }

echo "=== test-ded-etcd ==="
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
$K setup test-ded-etcd 90 </dev/null
pass test-ded-etcd

echo "=== test-ha ==="
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
$K setup test-ha 90 </dev/null
$K setup test-ha 10 </dev/null
pass test-ha

echo "=== aio ==="
$K start-aio </dev/null
pass aio

echo "=== G3 ops ==="
for c in test137 debian128 test-ded-etcd test-ha aio; do
  $K checkout "$c"
  $K backup "$c" </dev/null
  $K stop "$c" </dev/null
  $K start "$c" </dev/null
  $K restore "$c" </dev/null
  pass "G3 $c"
done

echo "=== G5 kcfg-adm ==="
$K checkout test137
$K kcfg-adm -A -u testuser-reg -t view test137 </dev/null
$K kcfg-adm -L test137
$K kcfg-adm -D -u testuser-reg test137 </dev/null
pass G5

echo "=== G4 add/del node ==="
$K add-node test-ha 192.168.47.133 worker-133 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep worker-133
$K del-node test-ha 192.168.47.134 </dev/null
pass G4

echo "=== G6 kca-renew ==="
$K kca-renew test137 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep Ready
pass G6-kca-renew

echo "=== G6 multi checkout ==="
for c in test137 debian128 test-ded-etcd test-ha aio; do $K checkout "$c"; $K backup "$c" </dev/null; done
pass X-G6-06

$K list
for c in test137 debian128 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"; kubectl get nodes
done
echo REGRESSION_COMPLETE
