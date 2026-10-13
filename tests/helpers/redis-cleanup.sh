#!/usr/bin/env bash
# Redis laboratory cleanup, never a customer data-destruction entry point.
set -Eeuo pipefail
KC="${KUBECONFIG:-${HOME}/.kube/config}"
kubectl_cmd=(kubectl --kubeconfig="$KC")
namespace="${REDIS_NAMESPACE:-redis}"
operator_namespace="${REDIS_OPERATOR_NAMESPACE:-redis-operator}"
helm="${REDIS_HELM:-/usr/local/kubeauto/extra-bin/helm}"
crd_journal="${REDIS_CRD_OWNERSHIP_FILE:-/var/tmp/kubeauto-redis-owned-crds}"
for ns in "$namespace" "$operator_namespace"; do [[ "$ns" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; done
[[ "$namespace" != "$operator_namespace" ]]

namespace_owned() {
  local ns="$1" labels
  labels="$("${kubectl_cmd[@]}" get namespace "$ns" -o jsonpath='{.metadata.labels.kubeauto\.io/component}:{.metadata.labels.kubeauto\.io/managed-by}')"
  [[ "$labels" == redis:kubeauto ]]
}
if [[ -f "$crd_journal" ]]; then
  # Validate the entire journal before the first mutation. These names come
  # from the vendored v0.27.0 CRDs, including the repeated redis DNS segment.
  while IFS= read -r crd; do
    [[ "$crd" =~ ^(redis|redisclusters|redisreplications|redissentinels)\.redis\.redis\.opstreelabs\.in$ ]] || {
      echo '[FAIL] invalid Redis CRD ownership journal' >&2; exit 1;
    }
  done < "$crd_journal"
fi
verify() {
  local ns present
  for ns in "$namespace" "$operator_namespace"; do
    present="$("${kubectl_cmd[@]}" get namespace "$ns" --ignore-not-found -o name)"
    [[ -z "$present" ]] || { echo "[FAIL] Redis namespace remains: $ns" >&2; return 1; }
  done
  # Helm owns cluster-scoped RBAC; CRDs are handled only from the preinstall journal.
  # Helm's release namespace is part of cluster-scoped RBAC ownership.
  # A same-name release in another namespace is outside this cleanup scope.
  present="$("${kubectl_cmd[@]}" get clusterrole,clusterrolebinding -o json)"
  printf '%s' "$present" | python3 -c '
import json,sys
namespace=sys.argv[1]
for obj in json.load(sys.stdin)["items"]:
 annotations=obj["metadata"].get("annotations",{})
 if (annotations.get("meta.helm.sh/release-name")=="redis-operator" and
     annotations.get("meta.helm.sh/release-namespace")==namespace):
  raise SystemExit("[FAIL] scoped Redis Helm RBAC remains")' "$operator_namespace"

  [[ ! -f "$crd_journal" ]] || { echo '[FAIL] Redis CRD ownership journal remains' >&2; return 1; }
  echo REDIS_CLEAN_VERIFY_PASS
}
[[ "${1:-}" != --verify ]] || { verify; exit; }
# Validate both namespaces before deleting anything.
for ns in "$namespace" "$operator_namespace"; do
  present="$("${kubectl_cmd[@]}" get namespace "$ns" --ignore-not-found -o name)"
  if [[ -n "$present" ]] && ! namespace_owned "$ns"; then
    echo "[FAIL] refusing foreign Redis namespace: $ns" >&2; exit 1
  fi
done
api_resources="$("${kubectl_cmd[@]}" api-resources --api-group=redis.redis.opstreelabs.in -o name)"
if [[ -n "$("${kubectl_cmd[@]}" get namespace "$namespace" --ignore-not-found -o name)" ]]; then
  for resource in $api_resources; do
    objects="$("${kubectl_cmd[@]}" -n "$namespace" get "$resource" -o json)"
    printf '%s' "$objects" | python3 -c '
import json,sys
for obj in json.load(sys.stdin)["items"]:
 labels=obj["metadata"].get("labels",{})
 if labels.get("kubeauto.io/component")!="redis" or labels.get("kubeauto.io/managed-by")!="kubeauto":
  raise SystemExit("[FAIL] refusing foreign Redis custom resource")'
  done
  # Keep the controller alive until every finalizer finishes.
  for resource in $api_resources; do
    "${kubectl_cmd[@]}" -n "$namespace" delete "$resource" --all --wait=true --timeout=5m
  done
  "${kubectl_cmd[@]}" delete namespace "$namespace" --wait=true --timeout=10m
fi
if [[ -n "$("${kubectl_cmd[@]}" get namespace "$operator_namespace" --ignore-not-found -o name)" ]]; then
  releases="$(KUBECONFIG="$KC" "$helm" list --all -n "$operator_namespace" --filter '^redis-operator$' -o json)"
  release_present="$(printf '%s' "$releases" | python3 -c '
import json,sys
rows=json.load(sys.stdin)
assert isinstance(rows,list)
assert all(row["name"]=="redis-operator" for row in rows)
assert len(rows)<=1
print("yes" if rows else "no")')"
  if [[ "$release_present" == yes ]]; then
    KUBECONFIG="$KC" "$helm" uninstall redis-operator -n "$operator_namespace" --wait --timeout=5m
  fi
  "${kubectl_cmd[@]}" delete namespace "$operator_namespace" --wait=true --timeout=5m
fi
if [[ -f "$crd_journal" ]]; then
  while IFS= read -r crd; do
    present="$("${kubectl_cmd[@]}" get crd "$crd" --ignore-not-found -o name)"
    [[ -n "$present" ]] || continue
    resource="${crd%%.*}"
    present="$("${kubectl_cmd[@]}" get "$resource" --all-namespaces -o name)"
    [[ -z "$present" ]] || { echo '[FAIL] shared Redis CRD still has consumers' >&2; exit 1; }
    "${kubectl_cmd[@]}" delete crd "$crd" --wait=true --timeout=5m
  done < "$crd_journal"
  rm -f -- "$crd_journal"
fi
verify
echo REDIS_CLEANUP_PASS
