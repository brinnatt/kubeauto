#!/bin/bash
# Focused Docker runtime/artifact gate on the large-memory reserved host 137.
# Usage (on 138 as root): bash /usr/local/kubeauto/tests/helpers/delivery-docker-gate.sh
set -euo pipefail
BASE=/usr/local/kubeauto
PROJECT_PY="$BASE/.venv/bin/python"
test -x "$PROJECT_PY"
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:$PATH"
K=kubecli
C=deliver-docker
NODE="${DOCKER_GATE_NODE:-192.168.47.137}"
LOG=/tmp/kubeauto-delivery-docker-$(date +%Y%m%d%H%M%S).log
if [ -w /var/log ] 2>/dev/null; then
  LOG=/var/log/kubeauto-delivery-docker-$(date +%Y%m%d%H%M%S).log
fi
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

pass(){ echo "[PASS] $*"; }
fail(){ echo "[FAIL] $*"; exit 1; }
run(){ echo ">>> $*"; "$@" || fail "$*"; }

nodes_ready(){
  local kc="$1" tries="${2:-48}"
  export KUBECONFIG="$BASE/clusters/$kc/kubectl.kubeconfig"
  for i in $(seq 1 "$tries"); do
    notready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2!="Ready"{print}' | wc -l)
    total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
    if [ "$total" -ge 1 ] && [ "$notready" -eq 0 ]; then
      kubectl get nodes -o wide
      return 0
    fi
    sleep 10
  done
  kubectl get nodes -o wide || true
  return 1
}

# Contract reserved floor is 4Gi; lab VMs under ~6Gi cannot run live (kubelet rejects).
MEM_MI="$(ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$NODE \
  "awk '/MemTotal/{print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 0)"
echo "docker_gate_node=$NODE mem_mi=$MEM_MI"
if [[ "$MEM_MI" -lt 6144 ]]; then
  echo "DOCKER_SKIP_LAB_UNDERSIZED: ${MEM_MI}Mi < 6Gi (contract ≥32Gi; reserved 4Gi)"
  grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/conf/config.yml"
  grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/conf/config.yml"
  grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/conf/config.yml"
  grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/conf/config.yml"
  echo DOCKER_GATE_SKIP_UNDERSIZED
  exit 0
fi

echo "========== artifact recovery + dual-source delivery =========="
run bash "$BASE/tests/run_unit_tests.sh"
EXT_BIN_VERSION="$("$PROJECT_PY" -c 'from common.constants import KubeConstant; print(KubeConstant().v_extra_bin)')"
EXT_IMAGE="brinnatt/kubeauto-ext-bin:${EXT_BIN_VERSION}"
PRIVATE_IMAGE="hub.talkedu.cn/kubeauto/kubeauto-ext-bin:${EXT_BIN_VERSION}"
CACHE_IMAGE="kubeauto-delivery-cache:${EXT_BIN_VERSION}"
ARTIFACT_BACKUP="$(mktemp -d /tmp/kubeauto-docker-artifacts.XXXXXX)"
restore_artifacts_on_exit() {
  rc=$?
  trap - EXIT
  docker image rm "$CACHE_IMAGE" >/dev/null 2>&1 || true
  if ! (cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256 >/dev/null 2>&1); then
    cp -a "$ARTIFACT_BACKUP"/. "$BASE/docker-bin"/
  fi
  rm -rf "$ARTIFACT_BACKUP"
  exit "$rc"
}
trap restore_artifacts_on_exit EXIT

run kubecli download -D </dev/null
(cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256) || \
  fail "initial Docker runtime artifact manifest"
cp -a "$BASE/docker-bin/docker-compose" "$BASE/docker-bin/docker-buildx" \
  "$BASE/docker-bin/cri-dockerd" "$BASE/docker-bin/docker-runtime-artifacts.sha256" \
  "$ARTIFACT_BACKUP"/

# Force a real restage with neither delivery tag cached. RegistryManager must
# pull TalkEdu first and atomically replace the corrupted local artifact.
docker image rm "$EXT_IMAGE" "$PRIVATE_IMAGE" >/dev/null 2>&1 || true
printf '\ncorrupted-by-delivery-gate\n' >> "$BASE/docker-bin/docker-buildx"
if (cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256 >/dev/null 2>&1); then
  fail "corrupted buildx unexpectedly passed manifest"
fi
run kubecli download -D </dev/null
(cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256) || \
  fail "TalkEdu restage manifest"
docker image inspect "$PRIVATE_IMAGE" >/dev/null || fail "TalkEdu image was not pulled first"
pass "TalkEdu-first corrupted-artifact recovery"

# Exercise the production fallback path with an intentionally unreachable
# private endpoint, then restage another corrupt artifact from Docker Hub.
# Keep a temporary tag so Docker's official content-addressable store can reuse
# the already verified layers.  The fallback still contacts Docker Hub and
# resolves its manifest, but the China delivery gate does not depend on a full
# repeated transfer of the same large layers from an intentionally secondary
# overseas source.
docker image tag "$PRIVATE_IMAGE" "$CACHE_IMAGE"
CACHE_IMAGE_ID="$(docker image inspect "$CACHE_IMAGE" --format '{{.Id}}')"
docker image rm "$EXT_IMAGE" "$PRIVATE_IMAGE" >/dev/null 2>&1 || true
printf '\ncorrupted-by-fallback-gate\n' >> "$BASE/docker-bin/docker-compose"
"$PROJECT_PY" - <<'PY'
from common.constants import KubeConstant
from service.cluster.docker import DockerManager
from service.cluster.registry import RegistryManager

image = KubeConstant().docker_runtime_artifact_image
registry = RegistryManager()
registry.kube_constant.v_talkedu_registry = "127.0.0.1:9/kubeauto"
registry._ensure_image_local(image)
DockerManager().ensure_docker_runtime_artifacts()
print("DOCKERHUB_FALLBACK_OK")
PY
docker image inspect "$EXT_IMAGE" >/dev/null || fail "Docker Hub fallback image missing"
[[ "$(docker image inspect "$EXT_IMAGE" --format '{{.Id}}')" == "$CACHE_IMAGE_ID" ]] || \
  fail "dual-pushed Docker runtime artifact image IDs differ"
docker image inspect "$EXT_IMAGE" --format '{{range .RepoDigests}}{{println .}}{{end}}' | \
  grep -q 'brinnatt/kubeauto-ext-bin@sha256:' || \
  fail "Docker Hub fallback digest missing"
(cd "$BASE/docker-bin" && sha256sum -c docker-runtime-artifacts.sha256) || \
  fail "Docker Hub fallback restage manifest"
docker compose version
docker buildx version
"$BASE/docker-bin/cri-dockerd" --version
pass "DockerHub-fallback corrupted-artifact recovery"
docker image rm "$CACHE_IMAGE" >/dev/null 2>&1 || true

echo "========== destroy leftover clusters using $NODE =========="
for c in deliver-docker deliver-upgrade; do
  $K destroy "$c" </dev/null 2>/dev/null || rm -rf "$BASE/clusters/$c"
done

echo "========== hard reset $NODE =========="
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$NODE bash -s <<'RST'
set -e
systemctl stop kubelet cri-dockerd docker containerd 2>/dev/null || true
pkill -9 kubelet 2>/dev/null || true
pkill -9 cri-dockerd 2>/dev/null || true
mount | awk '/kubelet|kubernetes|docker|containerd/ {print $3}' | sort -r | while read m; do umount -l "$m" 2>/dev/null || true; done
rm -rf /etc/kubernetes /var/lib/kubelet /var/lib/etcd /etc/cni /opt/cni/bin /var/lib/cni \
  /run/flannel /etc/systemd/system/kubelet.service /etc/systemd/system/kubelet.service.d \
  /etc/systemd/system/cri-dockerd.service /var/run/cri-dockerd.sock \
  /var/lib/docker /etc/docker /var/lib/containerd /etc/containerd
systemctl daemon-reload || true
systemctl reset-failed 2>/dev/null || true
systemctl disable kubelet docker containerd cri-dockerd 2>/dev/null || true
sed -i '/registry.talkschool.cn/d' /etc/hosts
echo '192.168.47.138    registry.talkschool.cn' >> /etc/hosts
echo RESET_OK
RST

echo "========== create docker cluster on $NODE =========="
$K new "$C" </dev/null || true
mkdir -p "$BASE/clusters/$C"
cat > "$BASE/clusters/$C/hosts" <<EOF
[etcd]
$NODE

[kube_master]
$NODE k8s_nodename='master-docker'

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
SERVICE_CIDR="10.71.0.0/16"
CLUSTER_CIDR="172.24.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/${C}"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
if [ ! -f "$BASE/clusters/$C/config.yml" ]; then
  cp "$BASE/conf/config.yml" "$BASE/clusters/$C/config.yml"
fi
K8S_VERSION="$("$PROJECT_PY" -c 'from common.constants import KubeConstant; print(KubeConstant().v_k8s_bin.lstrip("v"))')"
sed -i "s/__k8s_ver__/${K8S_VERSION}/g" "$BASE/clusters/$C/config.yml"

$K download -X </dev/null || true

echo "========== setup 90 =========="
if ! $K setup "$C" 90 </dev/null; then
  echo "--- node services ---"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$NODE \
    'systemctl status docker cri-dockerd kubelet --no-pager -l | head -120; ls -la /var/run/cri-dockerd.sock /run/containerd/containerd.sock 2>&1; journalctl -u kubelet -n 40 --no-pager'
  fail "setup 90"
fi

export KUBECONFIG="$BASE/clusters/$C/kubectl.kubeconfig"
if ! nodes_ready "$C" 48; then
  fail "nodes not Ready"
fi

rt=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)
echo "runtime=$rt"
echo "$rt" | grep -qi docker || fail "expected docker runtime, got $rt"

ok=0
for i in $(seq 1 36); do
  if ! kubectl get pods -A --no-headers > /tmp/kubeauto-docker-all-pods; then
    echo "[WAIT] pods status=api-unavailable bad=unknown attempt=$i/36 next_check=10s"
    sleep 10
    continue
  fi
  # kubectl --no-headers columns: NS NAME READY STATUS RESTARTS AGE
  bad=$(awk '$4!="Running" && $4!="Completed"{print}' /tmp/kubeauto-docker-all-pods | wc -l)
  if [ "$bad" -eq 0 ]; then ok=1; break; fi
  echo "[WAIT] pods status=not-ready bad=$bad attempt=$i/36 next_check=10s"
  sleep 10
done
[ "$ok" -eq 1 ] || { kubectl get pods -A -o wide; fail "system pods not Running"; }

docker0=$(kubectl get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.status.podIP} {.spec.hostNetwork}{"\n"}{end}' \
  | awk '$3!="true" && $2 ~ /^172\.17\./ {print}')
if [ -n "$docker0" ]; then
  echo "$docker0"
  fail "non-hostNetwork pods on docker0 — cri-dockerd CNI misconfigured"
fi

ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$NODE \
  'systemctl is-active docker cri-dockerd kubelet; /usr/local/bin/cri-dockerd --version; grep -E "container-runtime-endpoint|pod-infra|network-plugin" /etc/systemd/system/kubelet.service /etc/systemd/system/cri-dockerd.service'

SMOKE_IMAGE=$(
  "$PROJECT_PY" -c '
from common.constants import KubeConstant

c = KubeConstant()
print(f"{c.v_talkedu_registry}/json-mock:{c.v_json_mock}")
'
)
PRODUCTION_SMOKE_IMAGE="$SMOKE_IMAGE" \
PRODUCTION_SMOKE_SERVER_NODE=master-docker \
PRODUCTION_SMOKE_CLIENT_NODE=master-docker \
  bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"
pass "docker runtime ($rt) + application DNS/ClusterIP HTTP read-write"

echo "========== reserved enablement (contract) =========="
grep -q 'KUBE_RESERVED_ENABLED: "yes"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED not yes"
grep -q 'SYS_RESERVED_ENABLED: "yes"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED not yes"
grep -q 'KUBE_RESERVED_CPU: "1000m"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED_CPU not 1000m"
grep -q 'KUBE_RESERVED_MEMORY: "1536Mi"' "$BASE/clusters/$C/config.yml" || fail "KUBE_RESERVED_MEMORY not 1536Mi"
grep -q 'SYS_RESERVED_CPU: "1000m"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_CPU not 1000m"
grep -q 'SYS_RESERVED_MEMORY: "2560Mi"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_MEMORY not 2560Mi"
grep -q 'SYS_RESERVED_ENFORCE: "no"' "$BASE/clusters/$C/config.yml" || fail "SYS_RESERVED_ENFORCE not no"
bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG" || fail "reserved allocatable"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@$NODE \
  'systemctl show kubelet docker cri-dockerd -p Slice --value; test ! -d /sys/fs/cgroup/systemd/podruntime.slice.slice -a ! -d /sys/fs/cgroup/podruntime.slice.slice && echo NO_DOUBLE_SLICE'
pass "docker reserved RESERVED_ALLOCATABLE_PASS"

echo "DOCKER_GATE_PASS"
echo "LOG=$LOG"
