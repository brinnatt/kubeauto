#!/usr/bin/env bash
# Isolated live acceptance fixture for the standalone KubeBackup CLI.
set -euo pipefail

TOOL="${1:?KubeBackup CLI path is required}"
HOST="${2:?authorized control host is required}"
CONTEXT="${3:?Kubernetes context is required}"
PYTHON_BIN="${PYTHON:-$(command -v python3.12 || command -v python3)}"
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$HOST")
SOURCE_NS="kb-fixture"
TARGET_NS="kb-restore"
CRD="kbwidgets.tools.example.com"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/kubeauto-kube-backup-live.XXXXXX")"
KUBECONFIG_FILE="$WORKDIR/kubeconfig"

cleanup_remote() {
  "${SSH[@]}" "kubectl delete namespace '$SOURCE_NS' '$TARGET_NS' --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true; kubectl delete crd '$CRD' --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true" || true
}
cleanup() {
  cleanup_remote
  if [[ -d "$WORKDIR" ]]; then
    find "$WORKDIR" -depth -delete 2>/dev/null || true
    rmdir "$WORKDIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

[[ "$HOST" == root@192.168.122.243 ]] || { echo "KUBE_BACKUP_FIXTURE_UNAUTHORIZED_HOST" >&2; exit 2; }
[[ -f "$TOOL" ]] || { echo "KUBE_BACKUP_FIXTURE_MISSING_TOOL" >&2; exit 2; }
"$PYTHON_BIN" -c 'import kubernetes, tenacity, yaml'
cleanup_remote
"${SSH[@]}" 'cat /root/.kube/config' >"$KUBECONFIG_FILE"
chmod 0600 "$KUBECONFIG_FILE"

"${SSH[@]}" 'kubectl apply -f -' <<'YAML'
apiVersion: v1
kind: Namespace
metadata: {name: kb-fixture}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: kb-config, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
data: {DB_HOST: db.kb-fixture}
---
apiVersion: v1
kind: Secret
metadata: {name: kb-secret, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
type: Opaque
stringData: {TOKEN: fixture-only-secret}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: kb-demo, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
spec:
  replicas: 1
  selector: {matchLabels: {app: kb-demo}}
  template:
    metadata: {labels: {app: kb-demo}}
    spec:
      containers:
      - name: app
        image: registry.talkschool.cn:5000/brinnatt/busybox:1.37
        command: ["sh", "-c", "sleep 3600"]
        env:
        - {name: DOMAIN_NAME, value: api.kb-fixture}
        - {name: DEPLOY_ENV, value: kb-fixture}
        - name: SECRET_TOKEN
          valueFrom: {secretKeyRef: {name: kb-secret, key: TOKEN}}
---
apiVersion: v1
kind: Service
metadata: {name: kb-service, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
spec: {selector: {app: kb-demo}, ports: [{port: 80, targetPort: 80}]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: kb-role, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
rules: [{apiGroups: [""], resources: [configmaps], verbs: [get, list]}]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: kb-binding, namespace: kb-fixture, labels: {app: kb-demo, tier: backend}}
subjects: [{kind: ServiceAccount, name: default, namespace: kb-fixture}]
roleRef: {kind: Role, name: kb-role, apiGroup: rbac.authorization.k8s.io}
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: kbwidgets.tools.example.com}
spec:
  group: tools.example.com
  scope: Namespaced
  names: {plural: kbwidgets, singular: kbwidget, kind: KbWidget}
  versions:
  - name: v1
    served: true
    storage: true
    schema: {openAPIV3Schema: {type: object, properties: {spec: {type: object, properties: {value: {type: string}}}}}}
YAML
"${SSH[@]}" "kubectl wait --for=condition=Established crd/$CRD --timeout=60s"
"${SSH[@]}" 'kubectl apply -f -' <<'YAML'
apiVersion: tools.example.com/v1
kind: KbWidget
metadata: {name: demo-widget, namespace: kb-fixture}
spec: {value: fixture}
YAML

run_tool() { "$PYTHON_BIN" "$TOOL" "$@"; }
common=(--kubeconfig "$KUBECONFIG_FILE" --context "$CONTEXT")
primary="$WORKDIR/primary"

run_tool backup "${common[@]}" --namespace "$SOURCE_NS" --resources deployments,services,configmaps,secrets,roles,rolebindings --label-selector 'app=kb-demo,tier=backend' --tar --backup-name primary --output-dir "$WORKDIR" --max-workers 8
test -f "$primary.tar.gz"
tar -tzf "$primary.tar.gz" | grep -q 'backup-metadata.json'
"$PYTHON_BIN" - "$primary" <<'PY'
import pathlib, sys, yaml
base = pathlib.Path(sys.argv[1])
deployment = yaml.safe_load(next(base.rglob('Deployment-kb-demo.yaml')).read_text())
service = yaml.safe_load(next(base.rglob('Service-kb-service.yaml')).read_text())
secret = yaml.safe_load(next(base.rglob('Secret-kb-secret.yaml')).read_text())
assert not ({'uid', 'resourceVersion', 'managedFields', 'creationTimestamp'} & set(deployment['metadata']))
assert 'clusterIP' not in service.get('spec', {})
assert secret['metadata']['name'] == 'kb-secret'
PY

run_tool backup "${common[@]}" --namespace "$SOURCE_NS" --resources deployments,services --include-names kb-demo,kb-service --backup-name include --output-dir "$WORKDIR"
test -f "$WORKDIR/include/$SOURCE_NS/Deployment/Deployment-kb-demo.yaml"
run_tool backup "${common[@]}" --namespace "$SOURCE_NS" --resources deployments --exclude-names kb-demo --backup-name exclude --output-dir "$WORKDIR"
test ! -e "$WORKDIR/exclude/$SOURCE_NS/Deployment/Deployment-kb-demo.yaml"
run_tool backup "${common[@]}" --all-namespaces --resources configmaps --include-names kb-config --backup-name all-ns --output-dir "$WORKDIR"
test -f "$WORKDIR/all-ns/$SOURCE_NS/ConfigMap/ConfigMap-kb-config.yaml"
run_tool backup "${common[@]}" --namespace "$SOURCE_NS" --resources configmaps --backup-name dry --output-dir "$WORKDIR" --dry-run
test ! -e "$WORKDIR/dry"

run_tool backup "${common[@]}" --namespace "$SOURCE_NS" --resources configmaps --include-crds --include-names "$CRD,demo-widget" --backup-name crd --output-dir "$WORKDIR"
test -f "$WORKDIR/crd/cluster-scoped/CustomResourceDefinition/CustomResourceDefinition-kbwidgets_tools_example_com.yaml"
test -f "$WORKDIR/crd/$SOURCE_NS/KbWidget/KbWidget-demo-widget.yaml"
test ! -e "$WORKDIR/crd/monitor/KbWidget/KbWidget-demo-widget.yaml"

run_tool restore "${common[@]}" --backup-dir "$primary" --namespace-mapping "$SOURCE_NS=$TARGET_NS" --env-mapping 'DEPLOY_ENV=@k8s:metadata.namespace NEW_VALUE=present' --image-mapping 'registry.talkschool.cn:5000/=registry.talkschool.cn:5000/' --merge-patch '{"spec":{"replicas":2}}' --merge-patch-kind Deployment --create-namespaces --dry-run
! "${SSH[@]}" "kubectl get namespace '$TARGET_NS' >/dev/null 2>&1"
"${SSH[@]}" "kubectl delete namespace '$SOURCE_NS' --wait=true --timeout=120s"
run_tool restore "${common[@]}" --backup-dir "$primary" --namespace-mapping "$SOURCE_NS=$TARGET_NS" --env-mapping 'DEPLOY_ENV=@k8s:metadata.namespace NEW_VALUE=present' --image-mapping 'registry.talkschool.cn:5000/=registry.talkschool.cn:5000/' --merge-patch '{"spec":{"replicas":2}}' --merge-patch-kind Deployment --create-namespaces
for resource in \
  'configmap kb-config' 'secret kb-secret' 'service kb-service' \
  'role kb-role' 'rolebinding kb-binding' 'deployment kb-demo'; do
  # Query each type explicitly; kubectl does not accept mixed resource types in one get.
  "${SSH[@]}" "kubectl -n '$TARGET_NS' get $resource >/dev/null"
done
"${SSH[@]}" "kubectl -n '$TARGET_NS' get deployment kb-demo -o jsonpath='{.spec.replicas}' | grep -qx 2"
"${SSH[@]}" "kubectl -n '$TARGET_NS' get deployment kb-demo -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"DOMAIN_NAME\")].value}' | grep -qx api.kb-restore"
"${SSH[@]}" "kubectl -n '$TARGET_NS' get deployment kb-demo -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"DEPLOY_ENV\")].valueFrom.fieldRef.fieldPath}' | grep -qx metadata.namespace"

run_tool restore "${common[@]}" --backup-dir "$WORKDIR/crd" --namespace-mapping "$SOURCE_NS=$TARGET_NS" --skip-crds --skip-cluster-scoped --create-namespaces
"${SSH[@]}" "kubectl -n '$TARGET_NS' get kbwidget demo-widget -o jsonpath='{.spec.value}' | grep -qx fixture"

bad="$primary/$SOURCE_NS/ConfigMap/ConfigMap-invalid.yaml"
printf '%s\n' 'apiVersion: v1' 'kind: ConfigMap' 'metadata:' '  name: invalid_name' "  namespace: $SOURCE_NS" >"$bad"
if run_tool restore "${common[@]}" --backup-dir "$primary" --namespace-mapping "$SOURCE_NS=$TARGET_NS" --create-namespaces; then
  echo "KUBE_BACKUP_FIXTURE_EXPECTED_PARTIAL_FAILURE_MISSING" >&2
  exit 1
fi
"${SSH[@]}" "kubectl -n '$TARGET_NS' get deployment kb-demo >/dev/null"
find "$bad" -delete
run_tool restore "${common[@]}" --backup-dir "$primary" --namespace-mapping "$SOURCE_NS=$TARGET_NS" --create-namespaces
"${SSH[@]}" "test \$(kubectl -n '$TARGET_NS' get deployment kb-demo --no-headers | wc -l) -eq 1"

if run_tool backup "${common[@]}" --namespace "$TARGET_NS" --backup-name '../escape' --output-dir "$WORKDIR"; then
  echo "KUBE_BACKUP_FIXTURE_EXPECTED_PATH_REJECTION_MISSING" >&2
  exit 1
fi
if run_tool restore "${common[@]}" --backup-dir "$primary" --env-mapping 'BAD-NAME=value'; then
  echo "KUBE_BACKUP_FIXTURE_EXPECTED_MAPPING_REJECTION_MISSING" >&2
  exit 1
fi

"${SSH[@]}" "kubectl -n '$TARGET_NS' create serviceaccount kubeauto-kb-limited >/dev/null; kubectl -n '$TARGET_NS' create role kubeauto-kb-limited --verb=get --resource=configmaps >/dev/null; kubectl -n '$TARGET_NS' create rolebinding kubeauto-kb-limited --role=kubeauto-kb-limited --serviceaccount=$TARGET_NS:kubeauto-kb-limited >/dev/null"
server="$(awk '/^[[:space:]]*server:/{print $2; exit}' "$KUBECONFIG_FILE")"
ca_data="$(awk '/^[[:space:]]*certificate-authority-data:/{print $2; exit}' "$KUBECONFIG_FILE")"
token="$("${SSH[@]}" "kubectl -n '$TARGET_NS' create token kubeauto-kb-limited --duration=10m")"
limited="$WORKDIR/limited-kubeconfig"
cat >"$limited" <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster: {certificate-authority-data: $ca_data, server: $server}
  name: fixture
contexts:
- context: {cluster: fixture, namespace: $TARGET_NS, user: limited}
  name: limited
current-context: limited
users:
- name: limited
  user: {token: $token}
EOF
chmod 0600 "$limited"
if run_tool backup --kubeconfig "$limited" --context limited --namespace "$TARGET_NS" --resources configmaps --backup-name denied --output-dir "$WORKDIR"; then
  echo "KUBE_BACKUP_FIXTURE_EXPECTED_RBAC_DENIAL_MISSING" >&2
  exit 1
fi

cleanup_remote
! "${SSH[@]}" "kubectl get namespace '$SOURCE_NS' '$TARGET_NS' >/dev/null 2>&1"
! "${SSH[@]}" "kubectl get crd '$CRD' >/dev/null 2>&1"
echo "KUBE_BACKUP_CLEAN_VERIFY_PASS scope=$SOURCE_NS,$TARGET_NS,$CRD"
echo "KUBE_BACKUP_LIVE_REGRESSION_PASS host=$HOST context=$CONTEXT"
