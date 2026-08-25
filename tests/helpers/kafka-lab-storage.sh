#!/usr/bin/env bash
# Disposable distributed block CSI used only by the Kafka live delivery gate.
set -Eeuo pipefail

MODE="${1:-prepare}"
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
NAMESPACE="${KAFKA_LAB_STORAGE_NAMESPACE:-kafka-lab-storage}"
RELEASE="kafka-lab-longhorn"
STORAGE_CLASS="${KAFKA_STORAGE_CLASS:-kafka-longhorn}"
REGISTRY_HOST="registry.talkschool.cn:5000"
SOURCE_PREFIX="${KAFKA_LAB_IMAGE_SOURCE_PREFIX:-hub.talkedu.cn/kubeauto}"
VERIFY_PREFIX="${KAFKA_LAB_IMAGE_VERIFY_PREFIX:-${KAFKA_IMAGE_VERIFY_PREFIX:-}}"
CHART="$BASE/tests/fixtures/longhorn-1.10.1.tgz"
CHART_SHA256="a871d397e19cf3243949abd41fd294869f5c2c490014f29e71866a2433ec7fb9"
OWNED_STATE="/var/tmp/kubeauto-kafka-storage-owned"
TAG_OWNERSHIP="/var/tmp/kubeauto-kafka-storage-tags-owned"
ISCSI_INSTALLED="/var/tmp/kubeauto-kafka-storage-iscsi-installed"
ISCSI_STARTED="/var/tmp/kubeauto-kafka-storage-iscsi-started"
VALUES="/tmp/kubeauto-kafka-longhorn-values.yaml"
DATA_PATH="/var/lib/kubeauto-kafka-longhorn"
OFFICIAL_REGISTRY_AVAILABLE=unknown

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

node_ips() {
  kubectl get nodes -o wide --no-headers | awk '{print $6}'
}

namespace_owned() {
  [[ "$(kubectl get namespace "$NAMESPACE" \
    -o jsonpath='{.metadata.labels.kubeauto\.io/component}' 2>/dev/null || true)" == kafka-test-storage ]]
}

platform_digest() {
  local image="$1" descriptor
  descriptor="$(skopeo inspect --raw "docker://${image}" \
    | jq -r '[.manifests[]? | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest][0] // empty')"
  if [[ -n "$descriptor" ]]; then
    printf '%s\n' "$descriptor"
  else
    skopeo inspect "docker://${image}" | jq -r '.Digest'
  fi
}

source_available() {
  timeout --signal=TERM --kill-after=5s 60s skopeo inspect --raw "docker://$1" >/dev/null 2>&1
}

materialize_image() {
  local repository="$1" tag="$2" mirror_name="$3"
  local source official source_digest official_digest target_digest verifier
  official="docker.io/${repository}:${tag}"
  if [[ "$SOURCE_PREFIX" == hub.talkedu.cn/kubeauto ]]; then
    source="${SOURCE_PREFIX}/${mirror_name}:${tag}"
    verifier="${VERIFY_PREFIX}/brinnatt/${mirror_name}:${tag}"
  elif [[ "$SOURCE_PREFIX" == docker.io ]]; then
    source="$official"
    verifier="$official"
  else
    source="${SOURCE_PREFIX}/${repository}:${tag}"
    verifier="$source"
  fi
  if ! source_available "$source"; then
    source="$official"
    verifier="$official"
    echo "[WARN] TalkEdu Longhorn image unavailable; using official upstream image=${official}"
  fi
  source_digest="$(platform_digest "$source")"
  [[ "$source_digest" == sha256:* ]] || fail "Longhorn source manifest digest missing: ${repository}:${tag}"
  if [[ "$source" == "$official" ]]; then
    official_digest="$source_digest"
  elif [[ "$SOURCE_PREFIX" == hub.talkedu.cn/kubeauto && -n "$VERIFY_PREFIX" ]]; then
    source_available "$verifier" || fail "Longhorn Docker Hub dual-push verifier unavailable: ${verifier}"
    official_digest="$(platform_digest "$verifier")"
    [[ "$source_digest" == "$official_digest" ]] \
      || fail "Longhorn TalkEdu/Docker Hub digest mismatch: ${repository}:${tag}"
  elif [[ "$SOURCE_PREFIX" == hub.talkedu.cn/kubeauto ]]; then
    verifier="release-ci-dual-push-contract"
    official_digest="$source_digest"
    echo "KAFKA_STORAGE_DUAL_PUSH_ONLINE_VERIFY_SKIPPED image=${repository}:${tag} reason=china_delivery_uses_verified_talkedu_artifact"
  else
    if [[ "$OFFICIAL_REGISTRY_AVAILABLE" == unknown ]]; then
      if source_available "$official"; then
        OFFICIAL_REGISTRY_AVAILABLE=true
      else
        OFFICIAL_REGISTRY_AVAILABLE=false
        echo "[WARN] Docker Hub manifest endpoint unavailable; using Chart-pinned tag and end-to-end digest verification"
      fi
    fi
    if [[ "$OFFICIAL_REGISTRY_AVAILABLE" == true ]]; then
      official_digest="$(platform_digest "$official")"
      [[ "$source_digest" == "$official_digest" ]] \
        || fail "Longhorn source/official digest mismatch: ${repository}:${tag}"
      verifier="$official"
    else
      official_digest="$source_digest"
    fi
  fi
  timeout --signal=TERM --kill-after=10s 300s \
    skopeo copy --override-os linux --override-arch amd64 --retry-times 3 \
    "docker://${source}" \
    "docker://127.0.0.1:5000/kafka-lab/${repository}:${tag}" \
    --dest-tls-verify=false >/dev/null
  target_digest="$(skopeo inspect --override-os linux --override-arch amd64 --tls-verify=false \
    "docker://127.0.0.1:5000/kafka-lab/${repository}:${tag}" | jq -r '.Digest')"
  [[ "$target_digest" == "$official_digest" ]] \
    || fail "Longhorn local Registry digest mismatch: ${repository}:${tag}"
  printf '%s:%s\n' "$repository" "$tag" >>"$TAG_OWNERSHIP"
  echo "KAFKA_STORAGE_IMAGE_MATERIALIZED image=${repository}:${tag} digest=${target_digest} source=${source} verifier=${verifier}"
}

verify_storage_class() {
  [[ "$(kubectl get storageclass "$STORAGE_CLASS" -o jsonpath='{.provisioner}' 2>/dev/null || true)" == driver.longhorn.io ]]
  [[ "$(kubectl get storageclass "$STORAGE_CLASS" -o jsonpath='{.allowVolumeExpansion}' 2>/dev/null || true)" == true ]]
  [[ "$(kubectl get storageclass "$STORAGE_CLASS" -o jsonpath='{.volumeBindingMode}' 2>/dev/null || true)" == WaitForFirstConsumer ]]
}

verify_clean() {
  local residue=0
  if [[ "$(kubectl get storageclass "$STORAGE_CLASS" \
    -o jsonpath='{.metadata.labels.kubeauto\.io/component}' 2>/dev/null || true)" == kafka-test-storage ]]; then
    echo "KAFKA_STORAGE_CLEAN_RESIDUE storageclass=${STORAGE_CLASS}" >&2
    residue=1
  fi
  if namespace_owned; then
    echo "KAFKA_STORAGE_CLEAN_RESIDUE namespace=${NAMESPACE}" >&2
    residue=1
  fi
  if [[ -e "$OWNED_STATE" || -e "$TAG_OWNERSHIP" || -e "$ISCSI_INSTALLED" || -e "$ISCSI_STARTED" ]]; then
    echo "KAFKA_STORAGE_CLEAN_RESIDUE ownership-state" >&2
    residue=1
  fi
  [[ "$residue" -eq 0 ]] || fail "Kafka lab storage cleanup verification failed"
  echo KAFKA_STORAGE_CLEAN_VERIFY_PASS
}

cleanup() {
  local ip attempt
  if [[ ! -e "$OWNED_STATE" ]] && ! namespace_owned; then
    rm -f -- "$TAG_OWNERSHIP" "$ISCSI_INSTALLED" "$ISCSI_STARTED" "$VALUES"
    verify_clean
    return
  fi
  namespace_owned || fail "refusing to clean unowned storage namespace: ${NAMESPACE}"

  echo "KAFKA_STORAGE_CLEAN_STAGE_BEGIN action=delete-storageclass"
  if kubectl get storageclass "$STORAGE_CLASS" >/dev/null 2>&1; then
    [[ "$(kubectl get storageclass "$STORAGE_CLASS" \
      -o jsonpath='{.metadata.labels.kubeauto\.io/component}')" == kafka-test-storage ]] \
      || fail "refusing to delete unowned StorageClass: ${STORAGE_CLASS}"
    kubectl delete storageclass "$STORAGE_CLASS" --wait=true >/dev/null
  fi

  echo "KAFKA_STORAGE_CLEAN_STAGE_BEGIN action=uninstall-longhorn"
  kubectl -n "$NAMESPACE" patch settings.longhorn.io deleting-confirmation-flag \
    --type=merge -p '{"value":"true"}' >/dev/null 2>&1 || true
  helm -n "$NAMESPACE" uninstall "$RELEASE" --wait --timeout 15m >/dev/null 2>&1 || true
  kubectl delete namespace "$NAMESPACE" --wait=false >/dev/null 2>&1 || true
  for attempt in $(seq 1 180); do
    kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || break
    if (( attempt % 12 == 0 )); then
      echo "KAFKA_STORAGE_CLEAN_WAIT namespace=${NAMESPACE} attempt=${attempt}/180"
    fi
    sleep 5
  done
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 \
    && fail "Longhorn test namespace deletion timed out"
  mapfile -t longhorn_crds < <(kubectl get crd -l app.kubernetes.io/name=longhorn -o name 2>/dev/null || true)
  if (( ${#longhorn_crds[@]} > 0 )); then
    kubectl delete --wait=true --timeout=5m "${longhorn_crds[@]}" >/dev/null
  fi

  echo "KAFKA_STORAGE_CLEAN_STAGE_BEGIN action=remove-node-state"
  for ip in $(node_ips); do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" bash -s -- \
      "$REGISTRY_HOST" "$DATA_PATH" <<'NODE_CLEAN'
set -Eeuo pipefail
registry_host="$1"
data_path="$2"
while IFS= read -r image; do
  [[ -n "$image" ]] && ctr -n k8s.io images rm "$image" >/dev/null 2>&1 || true
done < <(ctr -n k8s.io images ls -q 2>/dev/null | grep -E "^${registry_host}/kafka-lab/longhornio/" || true)
rm -rf -- "$data_path"
NODE_CLEAN
  done

  if [[ -f "$TAG_OWNERSHIP" ]] && docker ps --format '{{.Names}}' | grep -qx local_registry; then
    docker exec local_registry rm -rf -- \
      /var/lib/registry/docker/registry/v2/repositories/kafka-lab
    docker restart local_registry >/dev/null
    for attempt in $(seq 1 30); do
      curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null && break
      sleep 1
    done
  fi

  if [[ -f "$ISCSI_STARTED" ]]; then
    while IFS= read -r ip; do
      [[ -n "$ip" ]] || continue
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
        'systemctl disable --now iscsid >/dev/null 2>&1 || true'
    done <"$ISCSI_STARTED"
  fi
  if [[ -f "$ISCSI_INSTALLED" ]]; then
    while IFS= read -r ip; do
      [[ -n "$ip" ]] || continue
      ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
        'dnf remove -y iscsi-initiator-utils >/dev/null'
    done <"$ISCSI_INSTALLED"
  fi
  rm -f -- "$OWNED_STATE" "$TAG_OWNERSHIP" "$ISCSI_INSTALLED" "$ISCSI_STARTED" "$VALUES"
  verify_clean
}

if [[ "$MODE" == --verify ]]; then
  verify_clean
  exit 0
fi
if [[ "$MODE" == cleanup ]]; then
  cleanup
  exit 0
fi
[[ "$MODE" == prepare ]] || fail "usage: $0 [prepare|cleanup|--verify]"
[[ "$SOURCE_PREFIX" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*$ ]] \
  || fail "invalid KAFKA_LAB_IMAGE_SOURCE_PREFIX"
if [[ -n "$VERIFY_PREFIX" ]]; then
  [[ "$VERIFY_PREFIX" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*$ ]] \
    || fail "invalid KAFKA_LAB_IMAGE_VERIFY_PREFIX"
fi

if kubectl get storageclass "$STORAGE_CLASS" >/dev/null 2>&1; then
  verify_storage_class || fail "existing StorageClass does not meet Kafka block expansion contract: ${STORAGE_CLASS}"
  echo "KAFKA_STORAGE_REUSED storageclass=${STORAGE_CLASS}"
  exit 0
fi
! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 \
  || namespace_owned \
  || fail "refusing to use unowned storage namespace: ${NAMESPACE}"
curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null \
  || fail "local Registry must be ready before Kafka lab storage preparation"
command -v skopeo >/dev/null || fail "skopeo is required for verified Longhorn image staging"
test "$(sha256sum "$CHART" | awk '{print $1}')" = "$CHART_SHA256" \
  || fail "Longhorn Chart checksum mismatch"

touch "$OWNED_STATE"
: >"$TAG_OWNERSHIP"
: >"$ISCSI_INSTALLED"
: >"$ISCSI_STARTED"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "$NAMESPACE" app.kubernetes.io/managed-by=kubeauto \
  kubeauto.io/component=kafka-test-storage --overwrite >/dev/null

echo "KAFKA_STORAGE_STAGE_BEGIN action=install-node-prerequisites"
for ip in $(node_ips); do
  if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
    'command -v iscsiadm >/dev/null'; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
      'dnf install -y iscsi-initiator-utils >/dev/null'
    printf '%s\n' "$ip" >>"$ISCSI_INSTALLED"
  fi
  if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
    'systemctl is-active --quiet iscsid'; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
      'modprobe iscsi_tcp && systemctl enable --now iscsid >/dev/null'
    printf '%s\n' "$ip" >>"$ISCSI_STARTED"
  fi
done

echo "KAFKA_STORAGE_STAGE_BEGIN action=materialize-official-images"
materialize_image longhornio/longhorn-engine v1.10.1 longhorn-engine
materialize_image longhornio/longhorn-manager v1.10.1 longhorn-manager
materialize_image longhornio/longhorn-ui v1.10.1 longhorn-ui
materialize_image longhornio/longhorn-instance-manager v1.10.1 longhorn-instance-manager
materialize_image longhornio/longhorn-share-manager v1.10.1 longhorn-share-manager
materialize_image longhornio/backing-image-manager v1.10.1 longhorn-backing-image-manager
materialize_image longhornio/support-bundle-kit v0.0.71 longhorn-support-bundle-kit
materialize_image longhornio/csi-attacher v4.10.0-20251030 longhorn-csi-attacher
materialize_image longhornio/csi-provisioner v5.3.0-20251030 longhorn-csi-provisioner
materialize_image longhornio/csi-node-driver-registrar v2.15.0-20251030 longhorn-csi-node-driver-registrar
materialize_image longhornio/csi-resizer v1.14.0-20251030 longhorn-csi-resizer
materialize_image longhornio/csi-snapshotter v8.4.0-20251030 longhorn-csi-snapshotter
materialize_image longhornio/livenessprobe v2.17.0-20251030 longhorn-livenessprobe

cat >"$VALUES" <<EOF
global:
  imageRegistry: ${REGISTRY_HOST}
image:
  longhorn:
    engine: {repository: kafka-lab/longhornio/longhorn-engine}
    manager: {repository: kafka-lab/longhornio/longhorn-manager}
    ui: {repository: kafka-lab/longhornio/longhorn-ui}
    instanceManager: {repository: kafka-lab/longhornio/longhorn-instance-manager}
    shareManager: {repository: kafka-lab/longhornio/longhorn-share-manager}
    backingImageManager: {repository: kafka-lab/longhornio/backing-image-manager}
    supportBundleKit: {repository: kafka-lab/longhornio/support-bundle-kit}
  csi:
    attacher: {repository: kafka-lab/longhornio/csi-attacher}
    provisioner: {repository: kafka-lab/longhornio/csi-provisioner}
    nodeDriverRegistrar: {repository: kafka-lab/longhornio/csi-node-driver-registrar}
    resizer: {repository: kafka-lab/longhornio/csi-resizer}
    snapshotter: {repository: kafka-lab/longhornio/csi-snapshotter}
    livenessProbe: {repository: kafka-lab/longhornio/livenessprobe}
persistence:
  defaultClass: false
defaultSettings:
  defaultDataPath: ${DATA_PATH}
  defaultReplicaCount: 2
  storageMinimalAvailablePercentage: 10
  storageReservedPercentageForDefaultDisk: 10
  upgradeChecker: false
EOF

echo "KAFKA_STORAGE_STAGE_BEGIN action=install-longhorn"
helm upgrade --install "$RELEASE" "$CHART" --namespace "$NAMESPACE" \
  --values "$VALUES" --wait --timeout 20m --history-max 2
kubectl -n "$NAMESPACE" rollout status daemonset/longhorn-manager --timeout=10m
kubectl -n "$NAMESPACE" rollout status deployment/longhorn-driver-deployer --timeout=10m

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${STORAGE_CLASS}
  labels:
    app.kubernetes.io/managed-by: kubeauto
    kubeauto.io/component: kafka-test-storage
provisioner: driver.longhorn.io
allowVolumeExpansion: true
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
parameters:
  numberOfReplicas: "2"
  staleReplicaTimeout: "30"
  dataLocality: "disabled"
  fsType: "ext4"
EOF
verify_storage_class || fail "Kafka lab StorageClass contract failed"
ready_nodes=0
for attempt in $(seq 1 60); do
  ready_nodes="$(kubectl -n "$NAMESPACE" get nodes.longhorn.io -o json \
    | jq '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length')"
  if [[ "$ready_nodes" -ge 6 ]]; then
    break
  fi
  if (( attempt % 6 == 0 )); then
    echo "KAFKA_STORAGE_WAIT_HEARTBEAT attempt=${attempt}/60 ready_nodes=${ready_nodes}"
    kubectl -n "$NAMESPACE" get nodes.longhorn.io -o wide || true
  fi
  sleep 10
done
[[ "$ready_nodes" -ge 6 ]] || fail "Longhorn Ready nodes below six after 10m: ${ready_nodes}"
pass "KAFKA_STORAGE_LAB_PASS version=1.10.1 storageclass=${STORAGE_CLASS} nodes=${ready_nodes}"
