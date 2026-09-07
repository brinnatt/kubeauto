#!/usr/bin/env bash
set -Eeuo pipefail
KC="kubectl ${KUBECONFIG:+--kubeconfig=$KUBECONFIG}"
NS="${LOGGING_NAMESPACE:-logging}"
SMOKE_NS="logging-smoke"
VERIFY_ONLY=0
CLEAN_CLUSTER_SCOPED=0
[[ "$NS" == logging ]] && CLEAN_CLUSTER_SCOPED=1
RUNTIME_REGISTRY_NAME="kubeauto-logging-registry"
RUNTIME_REGISTRY_DATA="/var/lib/kubeauto-logging-registry"
[[ "${1:-}" == "--verify" ]] && VERIFY_ONLY=1
if ! $KC get namespace "$NS" >/dev/null 2>&1; then
  :
else
  if (( VERIFY_ONLY == 0 )); then
    # The Loki test fixture owns an unlabelled MinIO deployment/service. Remove
    # these names explicitly so namespace deletion cannot hang on fixture
    # residue after a pre-install failure.
    $KC -n "$NS" delete deployment/minio service/minio service/minio-console pod/logging-minio-mc \
      --ignore-not-found --wait=true
    $KC -n "$NS" delete daemonset,deploy,statefulset,job,cronjob,service,ingress,prometheusrule,servicemonitor,podmonitor \
      -l app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging --ignore-not-found --wait=true
    $KC -n "$NS" delete elasticsearch,kibana -l app.kubernetes.io/managed-by=kubeauto --ignore-not-found --wait=true
    $KC -n "$NS" delete secret,configmap,serviceaccount -l app.kubernetes.io/managed-by=kubeauto --ignore-not-found --wait=true
    if (( CLEAN_CLUSTER_SCOPED == 1 )); then
      $KC delete clusterrole,clusterrolebinding -l app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging --ignore-not-found --wait=true
    fi
    $KC delete namespace "$NS" --ignore-not-found --wait=true
    if (( CLEAN_CLUSTER_SCOPED == 1 )); then
      $KC delete namespace "$SMOKE_NS" --ignore-not-found --wait=true
    fi
  fi
fi
# ECK is vendored for this isolated branch. Remove its operator only after all
# logging CRs are gone; CRDs are retained as cluster prerequisites for a
# possible second route test and are verified instead of blanket-deleted.
if (( VERIFY_ONLY == 0 && CLEAN_CLUSTER_SCOPED == 1 )); then
  if [[ -e /var/tmp/kubeauto-logging-storage-default.before ]]; then
    previous_default="$(cat /var/tmp/kubeauto-logging-storage-default.before)"
    if [[ "$previous_default" == true ]]; then
      $KC annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite >/dev/null 2>&1 || true
    else
      $KC annotate storageclass local-path storageclass.kubernetes.io/is-default-class- >/dev/null 2>&1 || true
    fi
    rm -f /var/tmp/kubeauto-logging-storage-default.before
  fi
  # Restore all disposable control-plane taints. Kafka-buffer may temporarily
  # use these three nodes for the concurrent 8Gi Elasticsearch replicas.
  for node in logging-master-243 logging-master-246 logging-master-217; do
    $KC taint node "$node" node.kubernetes.io/unschedulable:NoSchedule --overwrite >/dev/null 2>&1 || true
    $KC cordon "$node" >/dev/null 2>&1 || true
  done
  # Restore node-local registry configuration created by the no-Docker lab
  # bridge. Backups are per-node and are never committed to the repository.
  for ip in $($KC get nodes -o wide --no-headers 2>/dev/null | awk '{print $6}'); do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$ip" bash -s <<'NODE_CLEAN'
set -Eeuo pipefail
if [[ -e /var/tmp/kubeauto-logging-hosts.before ]]; then
  cp /var/tmp/kubeauto-logging-hosts.before /etc/hosts
  rm -f /var/tmp/kubeauto-logging-hosts.before
fi
config_dir='/etc/containerd/certs.d/registry.talkschool.cn:5000'
if [[ -e "$config_dir/hosts.toml.before" ]]; then
  cp "$config_dir/hosts.toml.before" "$config_dir/hosts.toml"
  rm -f "$config_dir/hosts.toml.before"
fi
rm -f "$config_dir/.kubeauto-logging-runtime"
if [[ -d "$config_dir" && ! -e "$config_dir/hosts.toml" ]]; then
  rmdir "$config_dir" 2>/dev/null || true
fi
NODE_CLEAN
  done
  if command -v nerdctl >/dev/null 2>&1; then
    nerdctl rm -f "$RUNTIME_REGISTRY_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$RUNTIME_REGISTRY_DATA"
  $KC -n elastic-system delete statefulset elastic-operator --ignore-not-found --wait=true 2>/dev/null || true
  $KC delete namespace elastic-system --ignore-not-found --wait=true 2>/dev/null || true
fi
! $KC get namespace "$NS" >/dev/null 2>&1
if (( CLEAN_CLUSTER_SCOPED == 1 )); then
  ! $KC get namespace "$SMOKE_NS" >/dev/null 2>&1
  ! $KC get namespace elastic-system >/dev/null 2>&1 || ! $KC -n elastic-system get statefulset elastic-operator >/dev/null 2>&1
fi
echo LOGGING_CLEAN_VERIFY_PASS
