#!/bin/bash
# Resume from G3 test-ha onward — sudo bash regression-resume-g3.sh
set -euo pipefail
exec > /var/log/kubeauto-regression-resume-g3.log 2>&1
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli
pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

nodes_ready(){
  local c="$1" tries="${2:-36}"
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

echo "========== verify G3 test-ha =========="
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes
nodes_ready test-ha || fail "test-ha not Ready"
pass G3-test-ha

echo "========== G5 kcfg-adm =========="
run $K checkout test137
run $K kcfg-adm -A -u testuser-reg -t view test137 </dev/null
run $K kcfg-adm -L test137
run $K kcfg-adm -D -u testuser-reg test137 </dev/null
pass G5-kcfg

echo "========== G4 node expand/shrink =========="
run $K checkout test-ha
run $K add-node test-ha 192.168.47.133 worker-133 </dev/null
export KUBECONFIG="$BASE/clusters/test-ha/kubectl.kubeconfig"
kubectl get nodes | grep -q worker-133 || fail add-node
run $K del-node test-ha 192.168.47.134 </dev/null
pass G4-node

echo "========== G4 add-master + del-master (133) =========="
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
run $K kca-renew test137 </dev/null
export KUBECONFIG="$BASE/clusters/test137/kubectl.kubeconfig"
nodes_ready test137 || fail kca-renew-test137
run $K kca-renew aio </dev/null
for c in test137 debian128 test-ded-etcd test-ha aio; do
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
  nodes_ready "$cname" 48 || fail "$cname $net"
  $K destroy "$cname" </dev/null 2>/dev/null || true
  pass "Tier2 $cname ($net/$proxy)"
done

echo "========== Tier3 tools --help =========="
TOOL_DIR="$BASE/dist"
for t in CalicoPolicyCli NetCheckCli KafkaCli MyBackupCli MigrationCli StarCli KubeBackupCli KubePublishCli OvpnUserCli; do
  bin="$TOOL_DIR/$t"
  [ -x "$bin" ] || bin="/usr/local/bin/$t"
  "$bin" --help >/dev/null 2>&1 || fail "$t --help"
done
pass Tier3

echo "========== FINAL verification =========="
$K list
for c in test137 debian128 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes
done
echo REGRESSION_FULL_COMPLETE
