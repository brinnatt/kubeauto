#!/usr/bin/env bash
# Resume G8b addon smoke + final gates after G8 CNI matrix already PASS.
set -euo pipefail
BASE=/usr/local/kubeauto
export PATH=/usr/local/bin:$PATH PYTHONPATH=$BASE
cd "$BASE"
LOG=/tmp/kubeauto-resume-g8b-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

echo "========== G8b addon image smoke on aio =========="
run kubecli checkout aio
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
# Ensure root helm/kubectl path used by ansible also sees the cluster
install -m 0400 "$KUBECONFIG" /root/.kube/config 2>/dev/null || {
  mkdir -p /root/.kube
  cp "$KUBECONFIG" /root/.kube/config
  chmod 400 /root/.kube/config
}
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
for ns in monitor openebs kube-system minio-operator; do
  for i in $(seq 1 48); do
    bad=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | grep -v Running | wc -l || echo 1)
    total=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -vE 'Completed|Succeeded' | wc -l || echo 0)
    if [ "$total" -gt 0 ] && [ "$bad" -eq 0 ]; then
      echo "ns=$ns ready"
      break
    fi
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
for c in test141 debian150 test-ded-etcd test-ha aio; do
  if [ -f "$BASE/clusters/$c/kubectl.kubeconfig" ]; then
    export KUBECONFIG="$BASE/clusters/$c/kubectl.kubeconfig"
    echo "--- $c ---"
    kubectl get nodes 2>/dev/null || true
  fi
done
echo RESUME_G8B_COMPLETE
echo "LOG=$LOG"
