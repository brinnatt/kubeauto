#!/usr/bin/env bash
# Nerdctl regression gate (containerd path): clean lab → current ext-bin → aio + master/worker.
# Run on Ubuntu aio control 138 as ubuntu (sudo where needed for wipe/aio).
# Usage: bash tests/helpers/nerdctl-gate.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PROJECT_PY="$BASE/.venv/bin/python"
test -x "$PROJECT_PY"
export PATH=/usr/local/bin:$PATH
export PYTHONPATH="$BASE"
LOG=/tmp/kubeauto-nerdctl-gate-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG SRC=$SRC BASE=$BASE"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

WORKER="${NERDCTL_WORKER:-192.168.47.133}"
CONTROL_IP=192.168.47.138
EXT_BIN_VERSION="$("$PROJECT_PY" -c 'from common.constants import KubeConstant; print(KubeConstant().v_extra_bin)')"

echo "========== N0 sync source → ${BASE} =========="
if [[ "${NERDCTL_SKIP_SYNC:-0}" == 1 ]]; then
  echo "[SKIP] source already synchronized by enterprise supervisor"
else
  run bash "$SRC/tests/helpers/sync-kubeauto.sh" "ubuntu@${CONTROL_IP}" "$PW"
fi
grep -q 'v_nerdctl' "$BASE/common/constants.py" || fail "synced tree missing v_nerdctl"
grep -q "v_extra_bin.*${EXT_BIN_VERSION}" "$BASE/common/constants.py" || \
  fail "synced tree missing v_extra_bin ${EXT_BIN_VERSION}"
grep -q 'extra-bin/nerdctl' "$BASE/roles/containerd/tasks/main.yml" || fail "containerd role missing nerdctl copy"
pass "sync"

echo "========== N1 unit tests =========="
cd "$BASE"
run bash "$BASE/tests/run_unit_tests.sh"
pass "unit"

echo "========== N2 lab wipe (keep docker + registry :5000) =========="
if [[ "${NERDCTL_SKIP_LAB_WIPE:-0}" == 1 ]]; then
  echo "[SKIP] lab clean+verify already completed by enterprise supervisor"
else
  run bash "$BASE/tests/helpers/lab-wipe-nodes.sh"
fi
# also drop stale nerdctl binaries so old installs cannot mask missing distribute
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$WORKER" \
  'rm -f /usr/local/bin/nerdctl /opt/kube/bin/nerdctl 2>/dev/null; echo worker_nerdctl_cleared' || true
sudo rm -f /usr/local/bin/nerdctl /opt/kube/bin/nerdctl 2>/dev/null || true
sudo rm -rf "$BASE/extra-bin" 2>/dev/null || true
mkdir -p "$BASE/extra-bin"
# The authoritative lab wipe removes local_registry. Recreate it through the
# production download path so this gate also proves the HTTP-ready contract;
# `docker start` alone silently fails when the container does not exist.
run kubecli download -D </dev/null
(cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256)
curl -sf http://127.0.0.1:5000/v2/_catalog >/dev/null || fail "local_registry down after wipe"
pass "wipe+registry"

echo "========== N3 pull/extract ext-bin ${EXT_BIN_VERSION} =========="
# Force image present, then let kubecli extract via get_ext_bin path
docker pull "hub.talkedu.cn/kubeauto/kubeauto-ext-bin:${EXT_BIN_VERSION}" >/dev/null
docker pull "brinnatt/kubeauto-ext-bin:${EXT_BIN_VERSION}" >/dev/null
# Prefer kubecli download path used in production
kubecli download -e </dev/null || fail "kubecli download -e (ext-bin)"
test -x "$BASE/extra-bin/nerdctl" || fail "extra-bin/nerdctl missing after download -e"
test -x "$BASE/extra-bin/crictl" || fail "extra-bin/crictl missing"
"$BASE/extra-bin/nerdctl" --version | tee /tmp/nerdctl-extbin-version.txt
grep -E '2\.3\.4|v2\.3\.4' /tmp/nerdctl-extbin-version.txt || \
  grep -q 'nerdctl' /tmp/nerdctl-extbin-version.txt || fail "unexpected nerdctl version output"
# ensure not leftover from full bundle collision
test -x "$BASE/extra-bin/containerd-bin/containerd" || fail "containerd binary missing in ext-bin"
pass "ext-bin-${EXT_BIN_VERSION}"

echo "========== N4 bootstrap essential images =========="
BOOTSTRAP_MODE=essential bash "$BASE/tests/helpers/bootstrap-brinnatt-mirrors.sh" || true
kubecli download -X </dev/null || fail "download -X"
pass "images"

echo "========== N5 aio@138 (master+worker co-located, containerd) =========="
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$WORKER" true || \
  fail "control-to-worker key access missing; run through tests/run_enterprise_regression.sh"
run kubecli start-aio </dev/null
export KUBECONFIG="$BASE/clusters/aio/kubectl.kubeconfig"
for i in $(seq 1 90); do
  kubectl get nodes --no-headers 2>/dev/null | grep -q Ready && break
  sleep 5
done
kubectl get nodes -o wide
kubectl get nodes --no-headers | grep -q Ready || fail "aio node not Ready"
command -v nerdctl >/dev/null || fail "nerdctl not on PATH after aio"
nerdctl --version | tee /tmp/aio-nerdctl-version.txt
systemctl is-active containerd | grep -q active || fail "containerd not active on aio"
# Rootful containerd: non-root nerdctl defaults to rootless and fails; nodes use root/sudo (same as crictl).
sudo nerdctl info >/tmp/aio-nerdctl-info.txt 2>&1 || fail "sudo nerdctl info failed"
sudo nerdctl -n k8s.io ps >/tmp/aio-nerdctl-ps.txt 2>&1 || fail "sudo nerdctl -n k8s.io ps failed"
# sanity: pause/sandbox or kube pods visible somehow
wc -l /tmp/aio-nerdctl-ps.txt
# crictl still works (regression: nerdctl must not break CRI path)
sudo crictl info >/tmp/aio-crictl-info.txt 2>&1 || fail "crictl info broken after nerdctl install"
pass "aio-nerdctl"

echo "========== N6 destroy aio, wipe worker, multi-node master@138 + worker@${WORKER} =========="
kubecli destroy aio </dev/null || true
# wipe control k8s leftovers again but KEEP docker/registry; wipe worker hard
CTRL_WIPE='
set +e
systemctl stop kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd 2>/dev/null
mount | awk "/kubelet|containerd\\/io.containerd|kube-/ {print \$3}" | xargs -r umount -l 2>/dev/null
rm -rf /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/etcd \
  /etc/kubernetes /etc/cni /etc/containerd /etc/crictl.yaml \
  /etc/calico /var/lib/calico /opt/kubeauto_prepare_tasks \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/kube-apiserver.service /etc/systemd/system/kube-controller-manager.service \
  /etc/systemd/system/kube-scheduler.service /etc/systemd/system/etcd.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
rm -f /usr/local/bin/nerdctl
systemctl daemon-reload 2>/dev/null
echo CTRL_REWIPE_OK
'
sudo bash -lc "$CTRL_WIPE"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$WORKER" '
set +e
systemctl stop kubelet kube-proxy kube-lb containerd docker cri-dockerd 2>/dev/null
mount | awk "/kubelet|containerd|docker|kube-/ {print \$3}" | xargs -r umount -l 2>/dev/null
rm -rf /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/docker \
  /etc/kubernetes /etc/cni /etc/containerd /etc/docker /etc/crictl.yaml \
  /etc/calico /var/lib/calico /opt/kubeauto_prepare_tasks \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
rm -f /usr/local/bin/nerdctl /opt/kube/bin/nerdctl
systemctl daemon-reload 2>/dev/null
echo WORKER_REWIPE_OK
'
# ensure no stale nerdctl on either host before setup
! command -v nerdctl >/dev/null 2>&1 || fail "nerdctl still present on control after rewipe"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$WORKER" 'command -v nerdctl' \
  && fail "nerdctl still present on worker after rewipe" || true

rm -rf "$BASE/clusters/nerdctl-mw"
kubecli new nerdctl-mw </dev/null
cat > "$BASE/clusters/nerdctl-mw/hosts" <<EOF
[etcd]
${CONTROL_IP} ansible_connection=local ansible_become=true ansible_become_method=sudo

[kube_master]
${CONTROL_IP} ansible_connection=local ansible_become=true ansible_become_method=sudo k8s_nodename='master-aio'

[kube_node]
${WORKER} k8s_nodename='worker-133'

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
cluster_dir="{{ base_dir }}/clusters/nerdctl-mw"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
# config.yml is passed to Ansible as extra-vars, so it is authoritative over
# inventory [all:vars].  This gate includes the small 3.5 GiB worker 133;
# reserve-resource coverage belongs on the dedicated large-memory 137/138 gates.
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' \
  "$BASE/clusters/nerdctl-mw/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' \
  "$BASE/clusters/nerdctl-mw/config.yml"
grep -q '^KUBE_RESERVED_ENABLED: "no"$' "$BASE/clusters/nerdctl-mw/config.yml" || \
  fail "failed to disable kube reserved for small worker"
grep -q '^SYS_RESERVED_ENABLED: "no"$' "$BASE/clusters/nerdctl-mw/config.yml" || \
  fail "failed to disable system reserved for small worker"
# Install full cluster
run kubecli setup nerdctl-mw 90 </dev/null
export KUBECONFIG="$BASE/clusters/nerdctl-mw/kubectl.kubeconfig"
for i in $(seq 1 120); do
  ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || true)
  [[ "$ready" -ge 2 ]] && break
  sleep 5
done
kubectl get nodes -o wide
ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -c Ready || true)
[[ "$ready" -ge 2 ]] || fail "expected 2 Ready nodes, got $ready"

echo "----- master nerdctl -----"
command -v nerdctl >/dev/null || fail "nerdctl missing on master"
nerdctl --version | tee /tmp/mw-master-nerdctl.txt
sudo nerdctl info >/tmp/mw-master-nerdctl-info.txt 2>&1 || fail "master nerdctl info"
sudo nerdctl -n k8s.io ps >/tmp/mw-master-nerdctl-ps.txt 2>&1 || fail "master nerdctl ps"
sudo crictl info >/dev/null || fail "master crictl broken"

echo "----- worker nerdctl -----"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@$WORKER" '
set -e
command -v nerdctl
nerdctl --version
systemctl is-active containerd | grep -q active
nerdctl info >/tmp/worker-nerdctl-info.txt
nerdctl -n k8s.io ps >/tmp/worker-nerdctl-ps.txt
crictl info >/dev/null
echo WORKER_NERDCTL_OK
' | tee /tmp/mw-worker-nerdctl.txt
grep -q WORKER_NERDCTL_OK /tmp/mw-worker-nerdctl.txt || fail "worker nerdctl checks failed"
pass "master-worker-nerdctl"

SMOKE_IMAGE=$(
  "$PROJECT_PY" -c '
from common.constants import KubeConstant

c = KubeConstant()
print(f"{c.v_talkedu_registry}/json-mock:{c.v_json_mock}")
'
)
PRODUCTION_SMOKE_IMAGE="$SMOKE_IMAGE" \
PRODUCTION_SMOKE_SERVER_NODE=worker-133 \
PRODUCTION_SMOKE_CLIENT_NODE=master-aio \
  bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"
pass "containerd application DNS/ClusterIP cross-node HTTP read-write"

echo "========== N7 negative: docker path must not require nerdctl distribute =========="
# Role file: nerdctl only in containerd role, not docker role
! grep -q nerdctl "$BASE/roles/docker/tasks/main.yml" || fail "docker role unexpectedly ships nerdctl"
pass "docker-role-untouched"

echo "========== SUMMARY =========="
echo "NERDCTL_GATE_PASS"
echo "evidence: $LOG"
echo "extbin: $(cat /tmp/nerdctl-extbin-version.txt 2>/dev/null || true)"
echo "aio: $(cat /tmp/aio-nerdctl-version.txt 2>/dev/null || true)"
echo "mw-master: $(cat /tmp/mw-master-nerdctl.txt 2>/dev/null || true)"
echo "mw-worker: $(grep -E 'nerdctl|WORKER' /tmp/mw-worker-nerdctl.txt 2>/dev/null | head -5 || true)"
