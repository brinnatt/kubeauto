#!/usr/bin/env bash
# Resume enterprise regression from G8 (CNI matrix) after image registry was incomplete.
# Precondition: G0–G6 already PASS; local_registry up; root SSH to peers works.
set -euo pipefail
BASE=/usr/local/kubeauto
export PATH=/usr/local/bin:$PATH PYTHONPATH=$BASE
cd "$BASE"
LOG=/tmp/kubeauto-resume-g8-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

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
    i=$((i+1)); sleep 5
  done
  return 1
}

echo "========== preflight: registry + CNI images + root SSH =========="
docker start local_registry >/dev/null 2>&1 || true
curl -sf http://127.0.0.1:5000/v2/_catalog >/dev/null || fail "local registry down"
# Ensure control-root can SSH (ansible under sudo)
if [[ "$(id -u)" -eq 0 ]] && [[ -f /root/.ssh/id_rsa.pub ]]; then
  PUB="$(cat /root/.ssh/id_rsa.pub)"
  for ip in 192.168.47.134 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.135 192.168.47.137 192.168.47.136; do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$ip" \
      "mkdir -p /root/.ssh; chmod 700 /root/.ssh; grep -qxF '$PUB' /root/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys" || true
  done
fi
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
for comp in flannel cilium kube-router kube-ovn; do
  kubecli download -E "$comp" </dev/null || fail "upload $comp"
done
# Addon images for G8b
BOOTSTRAP_MODE=full bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
for comp in dashboard prometheus local-path-provisioner openebs minio ingress-nginx; do
  kubecli download -E "$comp" </dev/null || true
done
pass "registry primed"

echo "========== G8 Tier2 CNI/proxy on 133 =========="
PW=123456
for spec in "test133-flannel flannel ipvs" "test133-cilium cilium ipvs" "test133-kr kube-router ipvs" "test133-ovn kube-ovn ipvs" "test133-iptables calico iptables"; do
  set -- $spec
  cname=$1; net=$2; proxy=$3
  kubecli destroy "$cname" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$cname"
  # hard wipe 133 between CNI swaps
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.133 \
    'set +e; systemctl stop kubelet containerd; rm -rf /var/lib/kubelet /etc/kubernetes /etc/cni /var/lib/containerd /etc/containerd /etc/systemd/system/podruntime.slice /opt/kubeauto_prepare_tasks /etc/calico /run/flannel /etc/cilium; systemctl daemon-reload; sed -i "/registry.talkschool.cn/d" /etc/hosts; echo "192.168.47.138    registry.talkschool.cn" >> /etc/hosts; echo wipe133'
  kubecli new "$cname" </dev/null || true
  bash "$BASE/tests/helpers/mk-cluster-133.sh" "$cname" "$net" "$proxy"
  sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$cname/config.yml"
  # contract reserved defaults must stay yes
  grep -q 'KUBE_RESERVED_ENABLED: "yes"' "$BASE/clusters/$cname/config.yml" || fail "$cname reserved off"
  run kubecli setup "$cname" 90 </dev/null
  export KUBECONFIG="$BASE/clusters/$cname/kubectl.kubeconfig"
  nodes_ready "$cname" || fail "$cname $net not Ready"
  bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG" || fail "$cname reserved"
  if [ "$net" = "cilium" ]; then
    img=$(kubectl -n kube-system get deploy -l io.cilium/app=operator -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null || true)
    echo "cilium_operator_image=$img"
    echo "$img" | grep -q 'cilium-operator-generic:' || fail "cilium operator image missing"
    echo "$img" | grep -q 'generic-generic' && fail "cilium double-generic"
  fi
  run kubecli destroy "$cname" </dev/null
  pass "Tier2 $cname ($net/$proxy)"
done

echo "========== G8b addon image smoke on aio =========="
run kubecli checkout aio
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
mkdir -p /root/.kube
cp "$KUBECONFIG" /root/.kube/config
chmod 400 /root/.kube/config
CFG="$BASE/clusters/aio/config.yml"
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
run kubecli setup aio 07 </dev/null
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
if kubectl get pods -A --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull'; then
  kubectl get pods -A | grep -E 'ImagePullBackOff|ErrImagePull' || true
  fail "addon ImagePullBackOff"
fi
# soft waits
for ns in monitor openebs; do
  for i in $(seq 1 36); do
    bad=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | grep -v Running | wc -l || echo 1)
    total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | wc -l || echo 0)
    if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then break; fi
    sleep 10
  done
done
prom_img=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "prom_operator_image=$prom_img"
echo "$prom_img" | grep -q 'brinnatt/prometheus-operator' || fail "prometheus-operator not brinnatt"
op_img=$(kubectl -n openebs get deploy openebs-localpv-provisioner -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "openebs_provisioner_image=$op_img"
echo "$op_img" | grep -q 'brinnatt/provisioner-localpv' || fail "openebs provisioner not brinnatt"
bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG" || fail "aio reserved after addons"
pass "G8b-addons"

echo "========== six-repo unit (ecosystem consistency) =========="
run bash "$BASE/tests/run_unit_tests.sh"
pass "six-repo+unit"

echo "========== FINAL =========="
kubecli list
for c in test137 debian128 test-ded-etcd test-ha aio; do
  if [ -f "$BASE/clusters/$c/kubectl.kubeconfig" ]; then
    export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
    echo "--- $c ---"
    kubectl get nodes 2>/dev/null || true
  fi
done
echo RESUME_G8_COMPLETE
echo "LOG=$LOG"
