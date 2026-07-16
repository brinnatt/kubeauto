#!/bin/bash
# Full enterprise regression on 192.168.47.147 — non-interactive, all matrix groups.
# sudo bash /tmp/regression-147-full.sh
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

# Wait until a named node appears (and optionally is Ready). Avoids race after add-node/add-master.
wait_node(){
  local c="$1" name="$2" tries="${3:-36}"
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  local i=0
  while [ "$i" -lt "$tries" ]; do
    if kubectl get nodes --no-headers 2>/dev/null | awk '{print $1,$2}' | grep -E "^${name}[[:space:]]+Ready" >/dev/null; then
      return 0
    fi
    i=$((i+1))
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
# 133 may have stale host key after rebuild
ssh-keygen -R 192.168.47.133 2>/dev/null || true
run $K system -a --user root --password "$PW" 192.168.47.130-133 192.168.47.140-142 </dev/null
run $K system -a --user brinnatt --password "$PW" 192.168.47.150 </dev/null
$K docker -l >/dev/null 2>&1 || true
pass G5-system

echo "========== G2 cluster (re)setup =========="
# test141 Rocky single + addons
$K new test141 2>/dev/null || true
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
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test141/config.yml"
run $K setup test141 90 </dev/null
run $K setup test141 07 </dev/null
run $K setup test141 11 </dev/null
nodes_ready test141 || fail "test141 not Ready"

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
nodes_ready debian150 || fail "debian150 not Ready"

# test-ded-etcd
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
nodes_ready test-ded-etcd || fail "test-ded-etcd not Ready"

# test-ha + ex_lb
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
nodes_ready test-ha || fail "test-ha not Ready"

# aio (147 localhost)
if nodes_ready aio 2>/dev/null; then
  pass "aio already live"
else
  $K destroy aio </dev/null 2>/dev/null || rm -rf "$BASE/clusters/aio"
  run $K start-aio </dev/null
fi
nodes_ready aio || fail "aio not Ready"
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
wait_node test-ha worker-130 || fail add-node
run $K del-node test-ha 192.168.47.130 </dev/null
pass G4-node

echo "========== G4 add-master + del-master (133 disposable) =========="
run $K add-master test-ha 192.168.47.133 master-133 </dev/null
wait_node test-ha master-133 || fail add-master
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
  local i=0
  while [ "$i" -lt "$tries" ]; do
    if kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | grep -qvE 'Running|0/'; then
      :
    fi
    bad=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | grep -v Running | wc -l || echo 1)
    total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | wc -l || echo 0)
    if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then
      return 0
    fi
    i=$((i+1)); sleep 10
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
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes
done
echo REGRESSION_FULL_COMPLETE
