#!/usr/bin/env bash
# Remove only resources and node configuration owned by the MySQL/PXC gate.
set -Eeuo pipefail

MODE="${1:-cleanup}"
MYSQL_NAMESPACE="${MYSQL_NAMESPACE:-mysql}"
MYSQL_OPERATOR_NAMESPACE="${MYSQL_OPERATOR_NAMESPACE:-mysql-operator}"
REGISTRY_NAME="kubeauto-mysql-registry"
REGISTRY_DATA="/var/lib/kubeauto-mysql-registry"
REGISTRY_HOST="registry.talkschool.cn:5000"
MARKER="kubeauto-mysql-gate"
STAGE_NODE="${MYSQL_IMAGE_STAGE_NODE:-}"
SOURCE_PREFIX="${MYSQL_IMAGE_SOURCE_PREFIX:-docker.io}"
TAG_OWNERSHIP="/var/tmp/kubeauto-mysql-registry-tags-owned"
PXC_IMAGE_TAGS=(
  percona-xtradb-cluster-operator:1.20.0
  percona-xtradb-cluster:8.4.8-8.1
  percona-xtrabackup:8.4.0-5.1
  percona-haproxy:2.8.18-1
  percona-fluentbit:5.0.6-1
  mysql-gate-minio:RELEASE.2025-04-08T15-41-24Z
  mysql-gate-mc:RELEASE.2025-04-08T15-39-49Z
  mysql-gate-sysbench:1.1
)

node_ips() {
  kubectl get nodes -o wide --no-headers | awk '{print $6}'
}

cleanup_node() {
  local ip="$1"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" bash -s -- "$REGISTRY_HOST" "$MARKER" <<'NODE_CLEAN'
set -Eeuo pipefail
registry_host="$1"
marker="$2"
sed -i "/[[:space:]]# ${marker}$/d" /etc/hosts
config_dir="/etc/containerd/certs.d/${registry_host}"
if [[ -f "${config_dir}/.kubeauto-mysql-gate" ]]; then
  rm -rf -- "${config_dir}"
fi
for image in \
  percona-xtradb-cluster-operator:1.20.0 \
  percona-xtradb-cluster:8.4.8-8.1 \
  percona-xtrabackup:8.4.0-5.1 \
  percona-haproxy:2.8.18-1 \
  percona-fluentbit:5.0.6-1 \
  mysql-gate-minio:RELEASE.2025-04-08T15-41-24Z \
  mysql-gate-mc:RELEASE.2025-04-08T15-39-49Z \
  mysql-gate-sysbench:1.1; do
  ctr -n k8s.io images rm "${registry_host}/brinnatt/${image}" >/dev/null 2>&1 || true
done
NODE_CLEAN
}

cleanup_stage_sources() {
  [[ -n "$STAGE_NODE" ]] || return 0
  [[ "$STAGE_NODE" =~ ^[a-zA-Z0-9.-]+$ ]] || {
    echo "Invalid MYSQL_IMAGE_STAGE_NODE" >&2
    return 1
  }
  [[ "$SOURCE_PREFIX" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]+)?$ ]] || {
    echo "Invalid MYSQL_IMAGE_SOURCE_PREFIX" >&2
    return 1
  }
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    "root@${STAGE_NODE}" bash -s -- "$SOURCE_PREFIX" <<'NODE_STAGE_CLEAN'
set -Eeuo pipefail
source_prefix="$1"
ownership_file=/var/tmp/kubeauto-mysql-stage-owned
active_baseline_file=/var/tmp/kubeauto-mysql-stage-active-baseline
active_source_file=/var/tmp/kubeauto-mysql-stage-active-source
ingest_root=/var/lib/containerd/io.containerd.content.v1.content/ingest

stage_pull_pids() {
  local source_ref="$1" pid command_line
  while IFS= read -r pid; do
    [[ -r "/proc/${pid}/cmdline" ]] || continue
    command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
    command_line="${command_line% }"
    if [[ "$command_line" == "ctr -n k8s.io images pull --platform linux/amd64 ${source_ref}" ]]; then
      printf '%s\n' "$pid"
    fi
  done < <(pgrep -f '^ctr -n k8s.io images pull --platform linux/amd64 ' || true)
}

active_stage_ingests() {
  ctr -n k8s.io content active | awk 'NR > 1 && NF {print $1}'
}

abort_stage_ingest() {
  local active_ref="$1" ref_file ingest_dir
  local -a matches=()
  while IFS= read -r ref_file; do
    stored_ref="$(cat "$ref_file")"
    if [[ "$stored_ref" == "$active_ref" || "$stored_ref" == */"$active_ref" ]]; then
      matches+=("$ref_file")
    fi
  done < <(find "$ingest_root" -mindepth 2 -maxdepth 2 -type f -name ref -print 2>/dev/null)
  (( ${#matches[@]} == 1 )) || {
    echo "Expected one ingest directory for ref ${active_ref}, found ${#matches[@]}" >&2
    return 1
  }
  ingest_dir="$(dirname "${matches[0]}")"
  [[ "$ingest_dir" == "$ingest_root/"* ]] || return 1
  # containerd 2.1 local Store.Abort removes this ref-hashed ingest root.
  rm -rf -- "$ingest_dir"
}

for image in \
  percona/percona-xtradb-cluster-operator:1.20.0 \
  percona/percona-xtradb-cluster:8.4.8-8.1 \
  percona/percona-xtrabackup:8.4.0-5.1 \
  percona/haproxy:2.8.18-1 \
  percona/fluentbit:5.0.6-1 \
  minio/minio:RELEASE.2025-04-08T15-41-24Z \
  minio/mc:RELEASE.2025-04-08T15-39-49Z \
  perconalab/sysbench:1.1; do
  source_ref="${source_prefix}/${image}"
  mapfile -t pull_pids < <(stage_pull_pids "$source_ref")
  if (( ${#pull_pids[@]} > 0 )); then
    kill -TERM "${pull_pids[@]}" 2>/dev/null || true
    for attempt in $(seq 1 20); do
      mapfile -t pull_pids < <(stage_pull_pids "$source_ref")
      (( ${#pull_pids[@]} == 0 )) && break
      sleep 1
    done
    (( ${#pull_pids[@]} == 0 )) || kill -KILL "${pull_pids[@]}" 2>/dev/null || true
  fi
  ctr -n k8s.io images rm "$source_ref" >/dev/null 2>&1 || true
done

if [[ -f "$active_source_file" ]]; then
  active_source="$(cat "$active_source_file")"
  [[ "$active_source" == "${source_prefix}/"* ]] || {
    echo "Refusing to abort an ingest owned by another runtime source" >&2
    exit 1
  }
  while IFS= read -r active_ref; do
    [[ -n "$active_ref" ]] || continue
    if [[ ! -f "$active_baseline_file" ]] || ! grep -Fxq "$active_ref" "$active_baseline_file"; then
      abort_stage_ingest "$active_ref"
    fi
  done < <(active_stage_ingests)
fi
rm -f -- "$ownership_file"
rm -f -- "$active_baseline_file" "$active_source_file"
NODE_STAGE_CLEAN
}

verify_clean() {
  local dirty=0 ip
  for namespace in "$MYSQL_NAMESPACE" "$MYSQL_OPERATOR_NAMESPACE"; do
    if kubectl get namespace "$namespace" >/dev/null 2>&1; then
      echo "namespace_residue=${namespace}" >&2
      dirty=1
    fi
  done
  if kubectl get crd -o name | grep -q 'pxc.percona.com'; then
    echo "pxc_crd_residue=true" >&2
    dirty=1
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx "$REGISTRY_NAME"; then
    echo "registry_container_residue=${REGISTRY_NAME}" >&2
    dirty=1
  fi
  [[ ! -e "$REGISTRY_DATA" ]] || {
    echo "registry_data_residue=${REGISTRY_DATA}" >&2
    dirty=1
  }
  [[ ! -e "$TAG_OWNERSHIP" ]] || {
    echo "registry_tag_ownership_residue=${TAG_OWNERSHIP}" >&2
    dirty=1
  }
  if curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
    for image in "${PXC_IMAGE_TAGS[@]}"; do
      repository="${image%:*}"
      tag="${image##*:}"
      if curl -fsS --max-time 3 "http://127.0.0.1:5000/v2/brinnatt/${repository}/tags/list" 2>/dev/null | grep -Fq "\"${tag}\""; then
        echo "registry_tag_residue=${image}" >&2
        dirty=1
      fi
    done
  fi
  for ip in $(node_ips); do
    if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" \
      "! grep -q '[[:space:]]# ${MARKER}$' /etc/hosts && ! test -e '/etc/containerd/certs.d/${REGISTRY_HOST}/.kubeauto-mysql-gate'"; then
      echo "node_registry_residue=${ip}" >&2
      dirty=1
    fi
  done
  if [[ -n "$STAGE_NODE" ]] && ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    "root@${STAGE_NODE}" test -e /var/tmp/kubeauto-mysql-stage-owned; then
    echo "node_stage_ownership_residue=${STAGE_NODE}" >&2
    dirty=1
  fi
  if [[ -n "$STAGE_NODE" ]]; then
    if ! [[ "$SOURCE_PREFIX" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]+)?$ ]]; then
      echo "invalid_node_stage_source_prefix=true" >&2
      dirty=1
    elif ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
      "root@${STAGE_NODE}" bash -s -- "$SOURCE_PREFIX" <<'NODE_STAGE_VERIFY'
set -Eeuo pipefail
source_prefix="$1"
test ! -e /var/tmp/kubeauto-mysql-stage-active-baseline
test ! -e /var/tmp/kubeauto-mysql-stage-active-source
for image in \
  percona/percona-xtradb-cluster-operator:1.20.0 \
  percona/percona-xtradb-cluster:8.4.8-8.1 \
  percona/percona-xtrabackup:8.4.0-5.1 \
  percona/haproxy:2.8.18-1 \
  percona/fluentbit:5.0.6-1 \
  minio/minio:RELEASE.2025-04-08T15-41-24Z \
  minio/mc:RELEASE.2025-04-08T15-39-49Z \
  perconalab/sysbench:1.1; do
  source_ref="${source_prefix}/${image}"
  ! ctr -n k8s.io images inspect "$source_ref" >/dev/null 2>&1
  while IFS= read -r pid; do
    [[ -r "/proc/${pid}/cmdline" ]] || continue
    command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
    command_line="${command_line% }"
    [[ "$command_line" != "ctr -n k8s.io images pull --platform linux/amd64 ${source_ref}" ]]
  done < <(pgrep -f '^ctr -n k8s.io images pull --platform linux/amd64 ' || true)
done
NODE_STAGE_VERIFY
    then
      echo "node_stage_source_residue=${STAGE_NODE}" >&2
      dirty=1
    fi
  fi
  [[ "$dirty" -eq 0 ]] || return 1
  echo MYSQL_CLEAN_VERIFY_PASS
}

pxc_custom_resources() {
  printf '%s\n' \
    perconaxtradbclusterbackup \
    perconaxtradbclusterrestore \
    perconaxtradbcluster
}

wait_pxc_custom_resources_deleted() {
  local attempts="${1:-120}" attempt resource remaining
  for attempt in $(seq 1 "$attempts"); do
    remaining=0
    while IFS= read -r resource; do
      kubectl -n "$MYSQL_NAMESPACE" get "$resource" -o name 2>/dev/null | grep -q . && remaining=1
    done < <(pxc_custom_resources)
    [[ "$remaining" -eq 0 ]] && return 0
    if (( attempt == 1 || attempt % 12 == 0 )); then
      echo "[WAIT] PXC custom-resource cleanup attempt=${attempt}/${attempts}"
      while IFS= read -r resource; do
        kubectl -n "$MYSQL_NAMESPACE" get "$resource" \
          -o custom-columns='KIND:.kind,NAME:.metadata.name,DELETING:.metadata.deletionTimestamp,FINALIZERS:.metadata.finalizers' \
          --no-headers 2>/dev/null || true
      done < <(pxc_custom_resources)
    fi
    (( attempt < attempts )) && sleep 5
  done
  return 1
}

recover_orphaned_pxc_finalizers() {
  local resource name
  while IFS= read -r resource; do
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      echo "[RECOVER] removing orphaned finalizers from deleting ${resource}/${name}"
      kubectl -n "$MYSQL_NAMESPACE" patch "$resource" "$name" --type=merge \
        -p '{"metadata":{"finalizers":[]}}' >/dev/null
    done < <(kubectl -n "$MYSQL_NAMESPACE" get "$resource" -o json 2>/dev/null \
      | jq -r '.items[]? | select(.metadata.deletionTimestamp != null) | .metadata.name' || true)
  done < <(pxc_custom_resources)
}

recover_orphaned_pxc_webhooks() {
  local kind config non_pxc_webhooks
  for kind in validatingwebhookconfiguration mutatingwebhookconfiguration; do
    while IFS= read -r config; do
      [[ -n "$config" ]] || continue
      non_pxc_webhooks="$(kubectl get "$kind" "$config" -o json \
        | jq '[.webhooks[]? | select((.name | endswith(".pxc.percona.com")) | not)] | length')"
      [[ "$non_pxc_webhooks" -eq 0 ]] || {
        echo "Refusing to remove mixed webhook configuration $kind/$config" >&2
        return 1
      }
      echo "[RECOVER] removing orphaned PXC webhook configuration $kind/$config"
      kubectl delete "$kind" "$config" --wait=true --timeout=1m >/dev/null
    done < <(kubectl get "$kind" -o json 2>/dev/null | jq -r --arg namespace "$MYSQL_OPERATOR_NAMESPACE" '
      .items[]?
      | select(any(.webhooks[]?; .clientConfig.service.namespace == $namespace))
      | .metadata.name
    ' || true)
  done
}

if [[ "$MODE" == "--verify" ]]; then
  verify_clean
  exit 0
fi
[[ "$MODE" == "cleanup" ]] || {
  echo "Usage: $0 [cleanup|--verify]" >&2
  exit 2
}

kubectl -n "$MYSQL_NAMESPACE" delete \
  perconaxtradbcluster,perconaxtradbclusterbackup,perconaxtradbclusterrestore \
  --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
recover_pxc_crs=0
if kubectl -n "$MYSQL_OPERATOR_NAMESPACE" get deployment pxc-operator >/dev/null 2>&1; then
  operator_replicas="$(kubectl -n "$MYSQL_OPERATOR_NAMESPACE" get deployment pxc-operator \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ "$operator_replicas" == 0 ]]; then
    echo "[RECOVER] PXC Operator scaled to zero; recovering deleting test resources"
    recover_pxc_crs=1
  elif ! wait_pxc_custom_resources_deleted 120; then
    echo "[WARN] PXC custom resources did not terminate while the Operator was present" >&2
    recover_pxc_crs=1
  fi
elif ! wait_pxc_custom_resources_deleted 1; then
  echo "[RECOVER] PXC Operator is absent; recovering finalizers from deleting test resources"
  recover_pxc_crs=1
fi
if [[ "$recover_pxc_crs" -eq 1 ]]; then
  operator_replicas="$(kubectl -n "$MYSQL_OPERATOR_NAMESPACE" get deployment pxc-operator \
    -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [[ -z "$operator_replicas" || "$operator_replicas" == 0 ]]; then
    recover_orphaned_pxc_webhooks
  fi
  recover_orphaned_pxc_finalizers
  wait_pxc_custom_resources_deleted 24 || {
    echo "PXC custom resources remain after scoped finalizer recovery" >&2
    exit 1
  }
fi
helm -n "$MYSQL_OPERATOR_NAMESPACE" uninstall pxc-operator --wait --timeout 5m >/dev/null 2>&1 || true
# The live gate may deliberately orphan a StatefulSet while injecting member
# loss.  Remove only workload controllers and Pods in the dedicated test
# namespace before requesting namespace deletion, so they cannot recreate
# objects while namespace finalizers drain.
kubectl -n "$MYSQL_NAMESPACE" delete statefulset --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
kubectl -n "$MYSQL_NAMESPACE" delete deployment --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
kubectl -n "$MYSQL_NAMESPACE" delete pod --all --ignore-not-found --grace-period=0 --force --wait=false >/dev/null 2>&1 || true
kubectl delete namespace "$MYSQL_NAMESPACE" "$MYSQL_OPERATOR_NAMESPACE" \
  --ignore-not-found --wait=false >/dev/null 2>&1 || true

for attempt in $(seq 1 180); do
  remaining=0
  for namespace in "$MYSQL_NAMESPACE" "$MYSQL_OPERATOR_NAMESPACE"; do
    kubectl get namespace "$namespace" >/dev/null 2>&1 && remaining=1
  done
  [[ "$remaining" -eq 0 ]] && break
  if (( attempt % 12 == 0 )); then
    echo "[WAIT] mysql namespace cleanup attempt=${attempt}/180"
  fi
  sleep 5
done

mapfile -t pxc_crds < <(kubectl get crd -o name | grep 'pxc.percona.com' || true)
if (( ${#pxc_crds[@]} > 0 )); then
  kubectl delete --wait=true --timeout=3m "${pxc_crds[@]}" >/dev/null
fi
for ip in $(node_ips); do
  cleanup_node "$ip"
done
cleanup_stage_sources

if [[ -f "$TAG_OWNERSHIP" ]] && docker ps --format '{{.Names}}' | grep -qx local_registry; then
  docker exec local_registry rm -rf -- \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/percona-xtradb-cluster-operator \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/percona-xtradb-cluster \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/percona-xtrabackup \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/percona-haproxy \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/percona-fluentbit \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/mysql-gate-minio \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/mysql-gate-mc \
    /var/lib/registry/docker/registry/v2/repositories/brinnatt/mysql-gate-sysbench
  docker restart local_registry >/dev/null
  for attempt in $(seq 1 30); do
    curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null && break
    sleep 1
  done
fi
for image in "${PXC_IMAGE_TAGS[@]}"; do
  docker image rm "127.0.0.1:5000/brinnatt/${image}" >/dev/null 2>&1 || true
done
rm -f -- "$TAG_OWNERSHIP"

if docker ps -a --format '{{.Names}}' | grep -qx "$REGISTRY_NAME"; then
  owner="$(docker inspect -f '{{index .Config.Labels "kubeauto.mysql-gate"}}' "$REGISTRY_NAME" 2>/dev/null || true)"
  [[ "$owner" == "true" ]] || {
    echo "Refusing to remove unowned container ${REGISTRY_NAME}" >&2
    exit 1
  }
  docker rm -f "$REGISTRY_NAME" >/dev/null
fi
rm -rf -- "$REGISTRY_DATA"
rm -rf -- /usr/local/kubeauto/clusters/mysql-gate
rm -rf -- /tmp/kubeauto-mysql-minio-tls
rm -f -- /tmp/kubeauto-mysql-playbook.yml /tmp/kubeauto-mysql-auth \
  /tmp/kubeauto-sysbench-node-loss.log

verify_clean
