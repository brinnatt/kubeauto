#!/usr/bin/env bash
# Scoped cleanup for the independent Strimzi/Kafka delivery branch.
set -Eeuo pipefail

MODE="${1:-clean}"
KAFKA_NAMESPACE="${KAFKA_NAMESPACE:-kafka}"
KAFKA_OPERATOR_NAMESPACE="${KAFKA_OPERATOR_NAMESPACE:-kafka-operator}"
KAFKA_DRAIN_CLEANER_NAMESPACE="${KAFKA_DRAIN_CLEANER_NAMESPACE:-kafka-drain-cleaner}"
KAFKA_CLUSTER="${KAFKA_CLUSTER:-kafka-prod}"
REGISTRY_HOST="registry.talkschool.cn:5000"
REGISTRY_NAME="kubeauto-kafka-registry"
REGISTRY_DATA="/var/lib/kubeauto-kafka-registry"
MARKER="kubeauto-kafka-gate"
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
TAG_OWNERSHIP="/var/tmp/kubeauto-kafka-tags-owned"

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }

node_ips() {
  kubectl get nodes -o wide --no-headers 2>/dev/null | awk '{print $6}'
}

namespace_is_owned() {
  local namespace="$1"
  [[ "$(kubectl get namespace "$namespace" -o jsonpath='{.metadata.labels.kubeauto\.io/component}' 2>/dev/null || true)" == kafka ]]
}

api_resource_exists() {
  local resource="$1" candidate found=1
  # Do not pipe api-resources into grep -q while pipefail is enabled: grep
  # may close early and make kubectl exit with SIGPIPE, falsely reporting a
  # missing resource during cleanup.
  while IFS= read -r candidate; do
    [[ "$candidate" == "$resource" ]] && found=0
  done < <(kubectl api-resources -o name 2>/dev/null || true)
  return "$found"
}

wait_namespace_deleted() {
  local namespace="$1" attempt
  for attempt in $(seq 1 120); do
    kubectl get namespace "$namespace" >/dev/null 2>&1 || return 0
    if (( attempt % 12 == 0 )); then
      echo "KAFKA_CLEAN_WAIT namespace=${namespace} attempt=${attempt}/120"
      kubectl get namespace "$namespace" -o jsonpath='{.status.phase}{" finalizers="}{.spec.finalizers}{"\n"}' || true
    fi
    sleep 5
  done
  return 1
}

force_remove_owned_custom_resources() {
  local resource name
  local -a resources=("$@")
  if (( ${#resources[@]} == 0 )); then
    resources=(
      kafkarebalances.kafka.strimzi.io
      kafkatopics.kafka.strimzi.io
      kafkausers.kafka.strimzi.io
      kafkas.kafka.strimzi.io
      kafkanodepools.kafka.strimzi.io
    )
  fi
  for resource in "${resources[@]}"; do
    if api_resource_exists "$resource"; then
      while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        kubectl -n "$KAFKA_NAMESPACE" patch "$name" --type=merge \
          -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
        kubectl -n "$KAFKA_NAMESPACE" delete "$name" --wait=false \
          --ignore-not-found >/dev/null 2>&1 || true
      done < <(kubectl -n "$KAFKA_NAMESPACE" get "$resource" -o name 2>/dev/null || true)
    fi
  done
}

force_finalize_owned_namespace() {
  local namespace="$1"
  namespace_is_owned "$namespace" || return 0
  kubectl get namespace "$namespace" -o json \
    | jq '.spec.finalizers = []' \
    | kubectl replace --raw "/api/v1/namespaces/${namespace}/finalize" -f - \
    >/dev/null 2>&1 || true
}

verify_clean() {
  local namespace ip residue=0
  local -a strimzi_cluster_residue=() strimzi_crd_residue=()
  for namespace in "$KAFKA_NAMESPACE" "$KAFKA_OPERATOR_NAMESPACE" "$KAFKA_DRAIN_CLEANER_NAMESPACE"; do
    if kubectl get namespace "$namespace" >/dev/null 2>&1; then
      echo "KAFKA_CLEAN_RESIDUE namespace=${namespace}" >&2
      residue=1
    fi
  done
  for release in \
    "$KAFKA_OPERATOR_NAMESPACE strimzi-kafka-operator" \
    "$KAFKA_DRAIN_CLEANER_NAMESPACE strimzi-drain-cleaner"; do
    read -r namespace name <<<"$release"
    if helm -n "$namespace" status "$name" >/dev/null 2>&1; then
      echo "KAFKA_CLEAN_RESIDUE helm=${namespace}/${name}" >&2
      residue=1
    fi
  done
  if kubectl get validatingwebhookconfiguration strimzi-drain-cleaner >/dev/null 2>&1; then
    echo "KAFKA_CLEAN_RESIDUE webhook=strimzi-drain-cleaner" >&2
    residue=1
  fi
  mapfile -t strimzi_cluster_residue < <(
    kubectl get clusterrole,clusterrolebinding -o name 2>/dev/null \
      | grep -E '(^|/)strimzi-' || true
  )
  if (( ${#strimzi_cluster_residue[@]} > 0 )); then
    printf 'KAFKA_CLEAN_RESIDUE cluster-resource=%s\n' \
      "${strimzi_cluster_residue[@]}" >&2
    residue=1
  fi
  mapfile -t strimzi_crd_residue < <(
    kubectl get crd -o name 2>/dev/null \
      | grep -E '(kafka\.strimzi\.io|core\.strimzi\.io)$' || true
  )
  if (( ${#strimzi_crd_residue[@]} > 0 )); then
    printf 'KAFKA_CLEAN_RESIDUE crd=%s\n' "${strimzi_crd_residue[@]}" >&2
    residue=1
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$REGISTRY_NAME"; then
    echo "KAFKA_CLEAN_RESIDUE container=${REGISTRY_NAME}" >&2
    residue=1
  fi
  [[ ! -e "$REGISTRY_DATA" ]] || {
    echo "KAFKA_CLEAN_RESIDUE path=${REGISTRY_DATA}" >&2
    residue=1
  }
  for ip in $(node_ips); do
    if ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
      "grep -Fq ' # ${MARKER}' /etc/hosts 2>/dev/null || test -e /etc/containerd/certs.d/${REGISTRY_HOST}/.kubeauto-kafka-gate || ctr -n k8s.io images ls -q 2>/dev/null | grep -Eq '^${REGISTRY_HOST}/brinnatt/strimzi-(operator|kafka|drain-cleaner):'"; then
      echo "KAFKA_CLEAN_RESIDUE node=${ip}" >&2
      residue=1
    fi
  done
  if curl -fsS --max-time 3 http://127.0.0.1:5000/v2/_catalog 2>/dev/null \
    | grep -Eq '"brinnatt/strimzi-(operator|kafka|drain-cleaner)"'; then
    echo "KAFKA_CLEAN_RESIDUE registry-repository" >&2
    residue=1
  fi
  if [[ -x "$BASE/tests/helpers/kafka-lab-storage.sh" ]] \
    && ! bash "$BASE/tests/helpers/kafka-lab-storage.sh" --verify; then
    residue=1
  fi
  [[ "$residue" -eq 0 ]] || fail "Kafka scoped cleanup verification failed"
  echo KAFKA_CLEAN_VERIFY_PASS
}

if [[ "$MODE" == "--verify" ]]; then
  verify_clean
  exit 0
fi
[[ "$MODE" == clean ]] || fail "usage: $0 [--verify]"

echo "KAFKA_CLEAN_STAGE_BEGIN action=restore-transient-state"
if [[ -s /var/tmp/kubeauto-kafka-cordoned-nodes ]]; then
  while IFS= read -r node; do
    [[ -n "$node" ]] && kubectl uncordon "$node" >/dev/null 2>&1 || true
  done </var/tmp/kubeauto-kafka-cordoned-nodes
fi
if kubectl -n "$KAFKA_OPERATOR_NAMESPACE" get deployment strimzi-cluster-operator >/dev/null 2>&1; then
  kubectl -n "$KAFKA_OPERATOR_NAMESPACE" scale deployment strimzi-cluster-operator --replicas=2 >/dev/null 2>&1 || true
  kubectl -n "$KAFKA_OPERATOR_NAMESPACE" rollout status deployment/strimzi-cluster-operator --timeout=3m >/dev/null 2>&1 || true
fi

echo "KAFKA_CLEAN_STAGE_BEGIN action=delete-kafka-resources"
if namespace_is_owned "$KAFKA_NAMESPACE"; then
  for resource in \
    kafkarebalances.kafka.strimzi.io \
    kafkatopics.kafka.strimzi.io \
    kafkausers.kafka.strimzi.io; do
    if api_resource_exists "$resource"; then
      kubectl -n "$KAFKA_NAMESPACE" delete "$resource" --all \
        --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
  done
  echo "KAFKA_CLEAN_STAGE_BEGIN action=wait-dependent-custom-resources"
  for attempt in $(seq 1 60); do
    dependent_count=0
    for resource in kafkarebalances.kafka.strimzi.io kafkatopics.kafka.strimzi.io \
      kafkausers.kafka.strimzi.io; do
      if api_resource_exists "$resource"; then
        current_count="$(kubectl -n "$KAFKA_NAMESPACE" get "$resource" \
          --ignore-not-found -o name 2>/dev/null | wc -l)"
        dependent_count=$((dependent_count + current_count))
      fi
    done
    [[ "$dependent_count" -eq 0 ]] && break
    if (( attempt % 12 == 0 )); then
      echo "KAFKA_CLEAN_WAIT dependent_custom_resources=${dependent_count} attempt=${attempt}/60"
      kubectl -n "$KAFKA_NAMESPACE" get kafkarebalance,kafkatopic,kafkauser 2>/dev/null || true
    fi
    sleep 5
  done
  if [[ "$dependent_count" -ne 0 ]]; then
    echo "KAFKA_CLEAN_STAGE_BEGIN action=force-remove-dependent-finalizers"
    force_remove_owned_custom_resources \
      kafkarebalances.kafka.strimzi.io \
      kafkatopics.kafka.strimzi.io \
      kafkausers.kafka.strimzi.io
    sleep 2
  fi
  if api_resource_exists kafkanodepools.kafka.strimzi.io; then
    kubectl -n "$KAFKA_NAMESPACE" delete kafkanodepools.kafka.strimzi.io --all \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  if api_resource_exists kafkas.kafka.strimzi.io; then
    kubectl -n "$KAFKA_NAMESPACE" delete kafkas.kafka.strimzi.io "$KAFKA_CLUSTER" \
      --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  for attempt in $(seq 1 120); do
    count=0
    for resource in kafkarebalances.kafka.strimzi.io kafkatopics.kafka.strimzi.io \
      kafkausers.kafka.strimzi.io kafkas.kafka.strimzi.io kafkanodepools.kafka.strimzi.io; do
      if api_resource_exists "$resource"; then
        current_count="$(kubectl -n "$KAFKA_NAMESPACE" get "$resource" \
          --ignore-not-found -o name 2>/dev/null | wc -l)"
        count=$((count + current_count))
      fi
    done
    [[ "$count" -eq 0 ]] && break
    if (( attempt % 12 == 0 )); then
      echo "KAFKA_CLEAN_WAIT custom_resources=${count} attempt=${attempt}/120"
      kubectl -n "$KAFKA_NAMESPACE" get pod,pvc 2>/dev/null || true
    fi
    sleep 5
  done
  if [[ "$count" -ne 0 ]]; then
    echo "KAFKA_CLEAN_STAGE_BEGIN action=force-remove-owned-finalizers"
    force_remove_owned_custom_resources
    sleep 2
    count=0
    for resource in kafkarebalances.kafka.strimzi.io kafkatopics.kafka.strimzi.io \
      kafkausers.kafka.strimzi.io kafkas.kafka.strimzi.io kafkanodepools.kafka.strimzi.io; do
      if api_resource_exists "$resource"; then
        current_count="$(kubectl -n "$KAFKA_NAMESPACE" get "$resource" \
          --ignore-not-found -o name 2>/dev/null | wc -l)"
        count=$((count + current_count))
      fi
    done
  fi
  [[ "$count" -eq 0 ]] || fail "Kafka custom resource deletion timed out: ${count}"
fi

echo "KAFKA_CLEAN_STAGE_BEGIN action=uninstall-helm"
helm -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" uninstall strimzi-drain-cleaner --wait >/dev/null 2>&1 || true
helm -n "$KAFKA_OPERATOR_NAMESPACE" uninstall strimzi-kafka-operator --wait >/dev/null 2>&1 || true
kubectl delete validatingwebhookconfiguration strimzi-drain-cleaner --ignore-not-found >/dev/null 2>&1 || true

echo "KAFKA_CLEAN_STAGE_BEGIN action=delete-owned-namespaces"
for namespace in "$KAFKA_NAMESPACE" "$KAFKA_OPERATOR_NAMESPACE" "$KAFKA_DRAIN_CLEANER_NAMESPACE"; do
  if namespace_is_owned "$namespace"; then
    kubectl delete namespace "$namespace" --wait=false >/dev/null 2>&1 || true
  elif kubectl get namespace "$namespace" >/dev/null 2>&1; then
    fail "refusing to delete unowned namespace: ${namespace}"
  fi
done
for namespace in "$KAFKA_NAMESPACE" "$KAFKA_OPERATOR_NAMESPACE" "$KAFKA_DRAIN_CLEANER_NAMESPACE"; do
  if ! wait_namespace_deleted "$namespace"; then
    if [[ "$namespace" == "$KAFKA_NAMESPACE" ]] && namespace_is_owned "$namespace"; then
      echo "KAFKA_CLEAN_STAGE_BEGIN action=force-remove-owned-finalizers"
      force_remove_owned_custom_resources
      force_finalize_owned_namespace "$namespace"
      kubectl delete namespace "$namespace" --wait=false >/dev/null 2>&1 || true
      wait_namespace_deleted "$namespace" || fail "namespace deletion timed out: ${namespace}"
    else
      fail "namespace deletion timed out: ${namespace}"
    fi
  fi
done

echo "KAFKA_CLEAN_STAGE_BEGIN action=delete-owned-cluster-resources"
mapfile -t owned_cluster_resources < <(
  kubectl get clusterrole,clusterrolebinding \
    -l "kubeauto.io/component=kafka,strimzi.io/cluster=${KAFKA_CLUSTER}" \
    -o name 2>/dev/null || true
)
if (( ${#owned_cluster_resources[@]} > 0 )); then
  kubectl delete --wait=true "${owned_cluster_resources[@]}" >/dev/null
fi

if [[ -e /var/tmp/kubeauto-kafka-crds-owned ]]; then
  echo "KAFKA_CLEAN_STAGE_BEGIN action=delete-owned-crds"
  mapfile -t owned_crds < <(
    kubectl get crd -o name 2>/dev/null \
      | grep -E '(kafka\.strimzi\.io|core\.strimzi\.io)$' || true
  )
  if (( ${#owned_crds[@]} > 0 )); then
    kubectl delete --wait=true "${owned_crds[@]}" >/dev/null
  fi
fi

echo "KAFKA_CLEAN_STAGE_BEGIN action=remove-node-registry-state"
for ip in $(node_ips); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" bash -s -- \
    "$REGISTRY_HOST" "$MARKER" <<'NODE_CLEAN'
set -Eeuo pipefail
registry_host="$1"
marker="$2"
sed -i "/[[:space:]]# ${marker}$/d" /etc/hosts
config_dir="/etc/containerd/certs.d/${registry_host}"
if [[ -e "$config_dir/.kubeauto-kafka-gate" ]]; then
  rm -rf -- "$config_dir"
fi
while IFS= read -r image; do
  [[ -n "$image" ]] && ctr -n k8s.io images rm "$image" >/dev/null 2>&1 || true
done < <(ctr -n k8s.io images ls -q 2>/dev/null | grep -E "^${registry_host}/brinnatt/strimzi-(operator|kafka|drain-cleaner):" || true)
NODE_CLEAN
done

echo "KAFKA_CLEAN_STAGE_BEGIN action=remove-lab-storage"
if [[ -x "$BASE/tests/helpers/kafka-lab-storage.sh" ]]; then
  bash "$BASE/tests/helpers/kafka-lab-storage.sh" cleanup
fi

echo "KAFKA_CLEAN_STAGE_BEGIN action=remove-scoped-registry"
if [[ -f "$TAG_OWNERSHIP" ]] && docker ps --format '{{.Names}}' | grep -qx local_registry; then
  docker exec local_registry rm -rf -- \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/strimzi-operator \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/strimzi-kafka \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/strimzi-drain-cleaner
  docker restart local_registry >/dev/null
  for attempt in $(seq 1 30); do
    curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null && break
    sleep 1
  done
fi
docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
rm -rf -- "$REGISTRY_DATA"
if [[ -e /var/tmp/kubeauto-kafka-registry-image-owned ]]; then
  registry_image="$(head -n 1 /var/tmp/kubeauto-kafka-registry-image-owned)"
  [[ -n "$registry_image" ]] && docker image rm "$registry_image" >/dev/null 2>&1 || true
fi
rm -f -- /var/tmp/kubeauto-kafka-crds-owned \
  /var/tmp/kubeauto-kafka-registry-image-owned \
  "$TAG_OWNERSHIP" \
  /var/tmp/kubeauto-kafka-cordoned-nodes \
  /tmp/kubeauto-kafka-playbook.yml
rm -rf -- "$BASE/clusters/kafka-gate"

pass "Kafka scoped resources removed"
verify_clean
