#!/bin/bash
# Remainder after partial enterprise regression: fix gaps → addon smoke → cilium reverify → matrix.
# sudo bash /tmp/regression-147-remainder.sh
set -euo pipefail
LOG=/var/log/kubeauto-regression-remainder-$(date +%Y%m%d-%H%M).log
exec > >(tee -a "$LOG") 2>&1

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
K=kubecli
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
    i=$((i+1)); sleep 10
  done
  return 1
}

echo "========== R0 unit + image contract =========="
run bash "$BASE/tests/run_unit_tests.sh"
pass R0

echo "========== R1 bootstrap full + download addons =========="
# Only images needed for cilium reverify + addon smoke (skip nacos/rocketmq/hubble).
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
# Targeted addon pulls (faster than MODE=full with optional apps)
python3 - <<'PY' || true
import subprocess
pairs = [
  ("kubernetesui/dashboard-api:1.14.0", "brinnatt/dashboard-api:1.14.0"),
  ("kubernetesui/dashboard-auth:1.4.0", "brinnatt/dashboard-auth:1.4.0"),
  ("kubernetesui/dashboard-web:1.7.0", "brinnatt/dashboard-web:1.7.0"),
  ("kubernetesui/dashboard-metrics-scraper:1.2.2", "brinnatt/dashboard-metrics-scraper:1.2.2"),
  ("kong:3.9", "brinnatt/kong:3.9"),
  ("grafana/grafana:12.0.2", "brinnatt/grafana:12.0.2"),
  ("quay.io/kiwigrid/k8s-sidecar:1.30.5", "brinnatt/k8s-sidecar:1.30.5"),
  ("quay.io/prometheus/prometheus:v3.4.2", "brinnatt/prometheus:v3.4.2"),
  ("quay.io/prometheus/alertmanager:v0.28.1", "brinnatt/alertmanager:v0.28.1"),
  ("quay.io/prometheus/node-exporter:v1.9.1", "brinnatt/node-exporter:v1.9.1"),
  ("quay.io/prometheus-operator/prometheus-operator:v0.83.0", "brinnatt/prometheus-operator:v0.83.0"),
  ("quay.io/prometheus-operator/prometheus-config-reloader:v0.83.0", "brinnatt/prometheus-config-reloader:v0.83.0"),
  ("registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.16.0", "brinnatt/kube-state-metrics:v2.16.0"),
  ("registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.6.0", "brinnatt/kube-webhook-certgen:v1.6.0"),
  ("rancher/local-path-provisioner:v0.0.31", "brinnatt/local-path-provisioner:v0.0.31"),
  ("openebs/provisioner-localpv:4.3.0", "brinnatt/provisioner-localpv:4.3.0"),
  ("openebs/linux-utils:4.2.0", "brinnatt/linux-utils:4.2.0"),
  ("bitnami/kubectl:1.33.6", "brinnatt/openebs-kubectl:1.33.6"),
  ("quay.io/minio/operator:v7.1.1", "brinnatt/minio-operator:v7.1.1"),
  ("quay.io/minio/operator-sidecar:v7.0.1", "brinnatt/minio-operator-sidecar:v7.0.1"),
  ("quay.io/minio/minio:RELEASE.2025-04-08T15-41-24Z", "brinnatt/minio:RELEASE.2025-04-08T15-41-24Z"),
]
for up, tg in pairs:
    r = subprocess.run(["docker", "image", "inspect", tg], capture_output=True)
    if r.returncode == 0:
        print(f"[skip] {tg}"); continue
    print(f"[pull] {tg}")
    if subprocess.run(["timeout", "180", "docker", "pull", up]).returncode == 0:
        subprocess.run(["docker", "tag", up, tg], check=False)
    else:
        print(f"[WARN] {tg}")
PY
for comp in cilium dashboard prometheus local-path-provisioner openebs minio; do
  $K download -E "$comp" </dev/null || true
done
pass R1

echo "========== R2 repair test-ded-etcd if Unauthorized =========="
if [ -d "$BASE/clusters/test-ded-etcd" ]; then
  export KUBECONFIG="$BASE/clusters/test-ded-etcd/kubectl.kubeconfig"
  if ! kubectl get nodes >/dev/null 2>&1; then
    echo "test-ded-etcd Unauthorized — recreate"
    $K destroy test-ded-etcd </dev/null 2>/dev/null || rm -rf "$BASE/clusters/test-ded-etcd"
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
  fi
  nodes_ready test-ded-etcd || fail "test-ded-etcd not Ready"
  pass "R2 test-ded-etcd"
else
  echo "[SKIP] test-ded-etcd absent"
fi

echo "========== R3 cilium live reverify =========="
cname=test133-cilium
$K destroy "$cname" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$cname"
$K new "$cname" 2>/dev/null || true
bash "$BASE/tests/helpers/mk-cluster-133.sh" "$cname" cilium ipvs
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/$cname/config.yml"
run $K setup "$cname" 90 </dev/null
export KUBECONFIG="$BASE/clusters/$cname/kubectl.kubeconfig"
nodes_ready "$cname" || fail "cilium not Ready"
img=$(kubectl -n kube-system get deploy -l io.cilium/app=operator -o jsonpath='{.items[0].spec.template.spec.containers[0].image}')
echo "cilium_operator_image=$img"
echo "$img" | grep -q 'brinnatt/cilium-operator-generic:' || fail "bad cilium operator image"
echo "$img" | grep -q 'generic-generic' && fail "double-generic"
run $K destroy "$cname" </dev/null
pass R3-cilium

echo "========== R4 addon smoke on aio (co-located with local registry) =========="
# Prefer aio: REGISTRY_HOST_IP matches the download host. Remote nodes may still
# point registry.talkschool.cn at a stale IP (e.g. 136) from an older bootstrap.
run $K checkout aio
nodes_ready aio || fail "aio not Ready"
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
# Force reinstall path for main.yml "not in pod_info" guards
helm -n monitor uninstall prometheus 2>/dev/null || true
helm -n openebs uninstall openebs 2>/dev/null || true
kubectl delete ns monitor openebs minio minio-operator --wait=false 2>/dev/null || true
kubectl -n kube-system delete deploy local-path-provisioner --wait=false 2>/dev/null || true
kubectl -n kube-system delete deploy,sts -l 'app.kubernetes.io/part-of=kubernetes-dashboard' --wait=false 2>/dev/null || true
sleep 5
run $K setup aio 07 </dev/null
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
sleep 20
kubectl get pods -A | grep -E 'dashboard|prometheus|openebs|local-path|minio|ingress|ImagePull|NAME' || true
if kubectl get pods -A --no-headers 2>/dev/null | grep -E 'ImagePullBackOff|ErrImagePull'; then
  kubectl get pods -A | grep -E 'ImagePullBackOff|ErrImagePull'
  fail "addon ImagePullBackOff"
fi
for ns in monitor openebs kube-system minio-operator minio ingress-nginx; do
  echo "--- pods $ns ---"
  kubectl get pods -n "$ns" 2>/dev/null || true
done
prom_img=$(kubectl -n monitor get deploy prometheus-kube-prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "prom_operator_image=$prom_img"
echo "$prom_img" | grep -q 'brinnatt/prometheus-operator' || fail "prometheus-operator not brinnatt"
op_img=$(kubectl -n openebs get deploy openebs-localpv-provisioner -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "openebs_provisioner_image=$op_img"
echo "$op_img" | grep -q 'brinnatt/provisioner-localpv' || fail "openebs provisioner not brinnatt"
d_img=$(kubectl -n kube-system get deploy kubernetes-dashboard-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
echo "dashboard_image=$d_img"
echo "$d_img" | grep -q 'brinnatt/' || fail "dashboard not brinnatt"
pass R4-addons

echo "========== R5 FINAL =========="
$K list
for c in test141 debian150 test-ded-etcd test-ha aio; do
  export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
  echo "--- $c ---"
  kubectl get nodes --no-headers 2>/dev/null | awk '{print $1,$2}' || fail "$c nodes"
done
echo REGRESSION_REMAINDER_COMPLETE
pass ALL
