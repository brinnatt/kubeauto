#!/bin/bash
# Standalone Kubernetes patch-upgrade gate on the large-memory Docker host 137.
# The v1.33.5 binaries come from the project's dual-pushed k8s-bin image and
# are pinned to the official dl.k8s.io checksums, so the delivery test does not
# depend on international download endpoints or a manually prepared fixture.
set -euo pipefail

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
K=kubecli
C=deliver-upgrade
NODE="${UPGRADE_GATE_NODE:-192.168.47.137}"
OLD_VERSION=v1.33.5
NEW_VERSION=v1.33.6
OLD_PRIVATE_IMAGE="hub.talkedu.cn/kubeauto/kubeauto-k8s-bin:$OLD_VERSION"
OLD_FALLBACK_IMAGE="brinnatt/kubeauto-k8s-bin:$OLD_VERSION"
OLD_STAGE=$(mktemp -d /tmp/kubeauto-k8s-1335.XXXXXX)
OLD_BIN="$OLD_STAGE/k8s"
OLD_CONTAINER=
LOG=/tmp/kubeauto-delivery-upgrade-$(date +%Y%m%d%H%M%S).log
if [ -w /var/log ] 2>/dev/null; then
  LOG=/var/log/kubeauto-delivery-upgrade-$(date +%Y%m%d%H%M%S).log
fi
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

cleanup_fixture(){
  if [ -n "$OLD_CONTAINER" ]; then
    docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -rf "$OLD_STAGE"
}
trap cleanup_fixture EXIT

nodes_ready(){
  export KUBECONFIG="$BASE/clusters/$C/kubectl.kubeconfig"
  local tries="${1:-48}" attempt total notready
  for attempt in $(seq 1 "$tries"); do
    total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    notready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2!="Ready"{print}' | wc -l)
    if [ "$total" -eq 1 ] && [ "$notready" -eq 0 ]; then
      kubectl get nodes -o wide
      return 0
    fi
    echo "[WAIT] nodes status=not-ready total=$total notready=$notready attempt=$attempt/$tries next_check=10s"
    if [ "$attempt" -lt "$tries" ]; then
      sleep 10
    fi
  done
  kubectl get nodes -o wide || true
  return 1
}

healthy_pods(){
  export KUBECONFIG="$BASE/clusters/$C/kubectl.kubeconfig"
  local tries="${1:-48}" attempt bad
  for attempt in $(seq 1 "$tries"); do
    if ! kubectl get pods -A --no-headers > /tmp/kubeauto-upgrade-all-pods; then
      echo "[WAIT] pods status=api-unavailable bad=unknown attempt=$attempt/$tries next_check=10s"
      if [ "$attempt" -lt "$tries" ]; then
        sleep 10
      fi
      continue
    fi
    awk '$4!="Running" && $4!="Completed"{print}' \
      /tmp/kubeauto-upgrade-all-pods > /tmp/kubeauto-upgrade-bad-pods
    bad=$(wc -l < /tmp/kubeauto-upgrade-bad-pods)
    if [ "$bad" -eq 0 ]; then
      kubectl get pods -A -o wide
      return 0
    fi
    echo "[WAIT] pods status=not-ready bad=$bad attempt=$attempt/$tries next_check=10s"
    cat /tmp/kubeauto-upgrade-bad-pods
    if [ "$attempt" -lt "$tries" ]; then
      sleep 10
    fi
  done
  baseline_diagnostics
  return 1
}

baseline_diagnostics(){
  export KUBECONFIG="$BASE/clusters/$C/kubectl.kubeconfig"
  echo "========== baseline automatic diagnostics =========="
  kubectl get nodes -o wide || true
  kubectl get pods -A -o wide || true
  kubectl get events -A --sort-by=.lastTimestamp | tail -n 160 || true
  kubectl describe pod -n kube-system -l k8s-app=calico-node || true
  kubectl logs -n kube-system -l k8s-app=calico-node --all-containers --tail=160 || true
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" '
    systemctl --no-pager --full status docker cri-dockerd kubelet kube-proxy || true
    crictl ps -a || true
    crictl images || true
    journalctl -u docker -u cri-dockerd -u kubelet -n 240 --no-pager || true
  ' || true
}

assert_remote_version(){
  local expected="$1" binary
  for binary in kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy kubectl; do
    if [ "$binary" = kubectl ]; then
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" \
        "/usr/local/bin/kubectl version --client=true -o yaml" | grep -F "gitVersion: $expected" \
        || fail "$binary is not $expected on $NODE"
    else
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" \
        "/usr/local/bin/$binary --version" | grep -F "$expected" \
        || fail "$binary is not $expected on $NODE"
    fi
  done
}

echo "========== official v1.33.5 artifact contract =========="
# Official checksum endpoints:
# https://dl.k8s.io/v1.33.5/bin/linux/amd64/<binary>.sha256
OLD_IMAGE=
for candidate in "$OLD_PRIVATE_IMAGE" "$OLD_FALLBACK_IMAGE"; do
  echo ">>> docker pull $candidate"
  if docker pull "$candidate"; then
    OLD_IMAGE="$candidate"
    break
  fi
  echo "[WAIT] Kubernetes $OLD_VERSION artifact unavailable from $candidate; trying fallback"
done
[ -n "$OLD_IMAGE" ] || fail "cannot pull Kubernetes $OLD_VERSION artifact from private registry or Docker Hub"
mkdir -p "$OLD_BIN"
OLD_CONTAINER=$(docker create "$OLD_IMAGE")
run docker cp "$OLD_CONTAINER:/k8s/." "$OLD_BIN/"
run docker rm "$OLD_CONTAINER"
OLD_CONTAINER=
cat > "$OLD_STAGE/official.sha256" <<EOF
394a66ee7c22d2dfc52b09e01eb4ace2ed5109dc3d8f09677af190ced83332ee  $OLD_BIN/kube-apiserver
2772be36e1d9b9a7d423cd6dc53410b7ea8cb59b53e809d00180a0ff6e109b17  $OLD_BIN/kube-controller-manager
f9dcbcf0a5f2cb9c959d5ad660c4a21d5220d788c46c7aca6a306f7f0b1d5831  $OLD_BIN/kube-scheduler
8f6106b970259486c5af5cbee404d4f23406d96d99dfb92a6965b299c2a4db0e  $OLD_BIN/kubelet
4681433b0dd216eb591eee440934cf79a68f9c5185f32c62905fa8fecf0c4d95  $OLD_BIN/kube-proxy
6a12d6c39e4a611a3687ee24d8c733961bb4bae1ae975f5204400c0a6930c6fc  $OLD_BIN/kubectl
EOF
run sha256sum -c "$OLD_STAGE/official.sha256"
"$OLD_BIN/kube-apiserver" --version | grep -F "$OLD_VERSION"
"$OLD_BIN/kubelet" --version | grep -F "$OLD_VERSION"
pass "official Kubernetes $OLD_VERSION artifacts from $OLD_IMAGE"

echo "========== create clean $NEW_VERSION Docker cluster =========="
"$BASE/kube-bin/kube-apiserver" --version | grep -F "$NEW_VERSION" \
  || fail "controller kube-bin is not $NEW_VERSION"
# sync-kubeauto exposes kube-bin under /usr/local/bin on the control host.
# Do not copy it onto itself: GNU cp correctly rejects identical source and
# destination paths.  The upgrade playbook's delegated version probe uses
# these control-host binaries, so verify every required component explicitly.
for binary in kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy kubectl; do
  if [ "$binary" = kubectl ]; then
    /usr/local/bin/kubectl version --client=true -o yaml | grep -F "gitVersion: $NEW_VERSION" \
      || fail "controller $binary is not $NEW_VERSION"
  else
    "/usr/local/bin/$binary" --version | grep -F "$NEW_VERSION" \
      || fail "controller $binary is not $NEW_VERSION"
  fi
done
if [ -d "$BASE/clusters/$C" ]; then
  run $K destroy "$C" </dev/null
fi
run $K new "$C" </dev/null
cat > "$BASE/clusters/$C/hosts" <<EOF
[etcd]
$NODE

[kube_master]
$NODE k8s_nodename='master-upgrade'

[kube_node]
$NODE

[harbor]
[ex_lb]
[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="docker"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.74.0.0/16"
CLUSTER_CIDR="172.27.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/$C"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
# kubecli new resolves every component placeholder from KubeConstant.  This
# generated file is authoritative; replacing it with conf/config.yml would
# reintroduce unresolved CNI/addon placeholders and invalidate the test.
grep -Fq "K8S_VER: \"${NEW_VERSION#v}\"" "$BASE/clusters/$C/config.yml" \
  || fail "generated config is not $NEW_VERSION"
unresolved_versions=$(grep -oE '__[A-Za-z0-9_]+__' "$BASE/clusters/$C/config.yml" \
  | grep -vE '^__(dbuser|yourpassword)__$' || true)
if [ -n "$unresolved_versions" ]; then
  printf '%s\n' "$unresolved_versions"
  fail "generated config contains unresolved component-version placeholders"
fi
# Reuse the proven Docker delivery path: populate the local registry before a
# clean node first creates sandbox/CNI pods.  This removes any dependency on a
# foreign registry during the bootstrap critical path.
run $K download -X </dev/null
if ! $K setup "$C" 90 </dev/null; then
  baseline_diagnostics
  fail "kubecli setup $C 90"
fi
nodes_ready || fail "$NEW_VERSION baseline node not Ready"
healthy_pods || fail "$NEW_VERSION baseline pods"
assert_remote_version "$NEW_VERSION"
pass "$NEW_VERSION baseline"

echo "========== establish supported $OLD_VERSION source state =========="
# Official upgrade order is API server, controller/scheduler, then
# kubelet/proxy.  Establishing the old patch source state follows the same
# dependency order before kubecli performs the forward upgrade.
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" \
  'systemctl stop kubelet kube-proxy kube-controller-manager kube-scheduler kube-apiserver'
scp -o BatchMode=yes -o StrictHostKeyChecking=no \
  "$OLD_BIN/kube-apiserver" "$OLD_BIN/kube-controller-manager" \
  "$OLD_BIN/kube-scheduler" "$OLD_BIN/kubelet" "$OLD_BIN/kube-proxy" \
  "$OLD_BIN/kubectl" root@"$NODE":/usr/local/bin/
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" '
  set -e
  systemctl start kube-apiserver
  for i in $(seq 1 20); do systemctl is-active --quiet kube-apiserver && break; sleep 2; done
  systemctl is-active --quiet kube-apiserver
  systemctl start kube-controller-manager kube-scheduler
  systemctl is-active --quiet kube-controller-manager
  systemctl is-active --quiet kube-scheduler
  systemctl start kubelet kube-proxy
  systemctl is-active --quiet kubelet
  systemctl is-active --quiet kube-proxy
'
assert_remote_version "$OLD_VERSION"
nodes_ready || fail "$OLD_VERSION source node not Ready"
healthy_pods || fail "$OLD_VERSION source pods"
before=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')
echo "BEFORE=$before"
[ "$before" = "$OLD_VERSION" ] || fail "source kubelet reports $before"
pass "$OLD_VERSION source state"

echo "========== kubecli patch upgrade =========="
# Kubernetes official version-skew policy permits patch upgrades within the
# same minor.  This project's 93.upgrade.yml upgrades masters serially and
# waits for the API server before controller/scheduler and node components.
run $K upgrade "$C" </dev/null
nodes_ready || fail "$NEW_VERSION upgraded node not Ready"
healthy_pods || fail "$NEW_VERSION upgraded pods"
assert_remote_version "$NEW_VERSION"
after=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')
runtime=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}')
echo "AFTER=$after runtime=$runtime"
[ "$after" = "$NEW_VERSION" ] || fail "upgraded kubelet reports $after"
echo "$runtime" | grep -q '^docker://' || fail "runtime changed during upgrade: $runtime"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@"$NODE" \
  'systemctl is-active kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy docker cri-dockerd'
pass "$OLD_VERSION -> $NEW_VERSION Docker cluster upgrade"
echo UPGRADE_GATE_PASS
echo "LOG=$LOG"
