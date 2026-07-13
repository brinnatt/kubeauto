#!/bin/bash
# Resume regression after test141 setup 90/07 — sudo bash regression-147-resume-g2.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-resume-g2-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli
PW=123456
pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

nodes_ready(){
  local c="$1" tries="${2:-30}"
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    if kubectl get nodes --no-headers 2>/dev/null | grep -q Ready; then
      return 0
    fi
    i=$((i+1))
    sleep 10
  done
  return 1
}

# Fix test141 harbor inventory
cat > "$BASE/clusters/test141/hosts" <<'EOF'
[etcd]
192.168.47.141
[kube_master]
192.168.47.141 k8s_nodename='master-141'
[kube_node]
192.168.47.141
[harbor]
192.168.47.141 NEW_INSTALL=true
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

echo "========== resume G2 harbor test141 =========="
run $K setup test141 11 </dev/null
nodes_ready test141 || fail test141

# debian150
$K new debian150 2>/dev/null || true
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
nodes_ready debian150 || fail debian150

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
run $K setup test-ded-etcd 90 </dev/null
nodes_ready test-ded-etcd || fail test-ded-etcd

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
run $K setup test-ha 90 </dev/null
run $K setup test-ha 10 </dev/null
nodes_ready test-ha || fail test-ha

if nodes_ready aio 2>/dev/null; then pass aio-live; else
  $K destroy aio </dev/null 2>/dev/null || rm -rf "$BASE/clusters/aio"
  run $K start-aio </dev/null
fi
nodes_ready aio || fail aio
pass G2

echo "========== G3 ops all clusters =========="
for c in test141 debian150 test-ded-etcd test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
  run $K stop "$c" </dev/null
  run $K start "$c" </dev/null
  run $K restore "$c" </dev/null
  nodes_ready "$c" || fail "G3 $c not Ready after restore"
  pass "G3 $c"
done

echo "========== G5 kcfg-adm =========="
run $K checkout test141
run $K kcfg-adm -A -u testuser-reg -t view test141 </dev/null
run $K kcfg-adm -L test141
run $K kcfg-adm -D -u testuser-reg test141 </dev/null
pass G5-kcfg

echo "========== G4 node expand/shrink =========="
run $K checkout test-ha
run $K add-node test-ha 192.168.47.130 worker-130 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep -q worker-130 || fail add-node
run $K del-node test-ha 192.168.47.130 </dev/null
pass G4-node

echo "========== G4 add-master + del-master (133 disposable) =========="
run $K add-master test-ha 192.168.47.133 master-133 </dev/null
kubectl get nodes | grep -q master-133 || fail add-master
run $K backup test-ha </dev/null
run $K del-master test-ha 192.168.47.133 </dev/null
pass G4-master

echo "========== G4 add-etcd + del-etcd (133) =========="
run $K add-etcd test-ha 192.168.47.133 etcd-133 </dev/null
grep -q '192.168.47.133' "$BASE/clusters/test-ha/hosts"
run $K del-etcd test-ha 192.168.47.133 </dev/null
pass G4-etcd

echo "========== G6 cross kca-renew + multi checkout =========="
run $K kca-renew test141 </dev/null
export KUBECONFIG="$BASE/clusters/test141/kubectl.kubeconfig"
nodes_ready test141 || fail kca-renew-test141
run $K kca-renew aio </dev/null
for c in test141 debian150 test-ded-etcd test-ha aio; do
  run $K checkout "$c"
  run $K backup "$c" </dev/null
done
pass G6

echo "========== G8 Tier2 CNI/proxy on 133 =========="
for spec in "test133-flannel flannel ipvs" "test133-cilium cilium ipvs" "test133-kr kube-router ipvs" "test133-ovn kube-ovn ipvs" "test133-iptables calico iptables"; do
  set -- $spec
  cname=$1; net=$2; proxy=$3
  $K destroy "$cname" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$cname"
  $K new "$cname" 2>/dev/null || true
  bash "$BASE/tests/helpers/mk-cluster-133.sh" "$cname" "$net" "$proxy"
  sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$cname/config.yml"
  run $K setup "$cname" 90 </dev/null
  export KUBECONFIG="$BASE/clusters/$cname/kubectl.kubeconfig"
  nodes_ready "$cname" || fail "$cname $net"
  $K destroy "$cname" </dev/null 2>/dev/null || true
  pass "Tier2 $cname ($net/$proxy)"
done

echo "========== Tier3 tools --help =========="
for t in CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli; do
  /usr/local/bin/$t --help >/dev/null 2>&1 || fail "$t --help"
done
pass Tier3

echo "========== FINAL verification =========="
$K list
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes
done
echo REGRESSION_FULL_COMPLETE
