#!/usr/bin/env bash
# Focused installation gate; owns its cluster, CSI and fixture cleanup.
set -Eeuo pipefail
MODE="${1:---fresh}"
[[ "$MODE" == --fresh || "$MODE" == --repair-core ]] || exit 2
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PY="$BASE/.venv/bin/python"
CLUSTER=redis-gate
KC="$BASE/clusters/$CLUSTER/kubectl.kubeconfig"
HELM="$BASE/extra-bin/helm"
NODES=(192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136)
STATE="$BASE/clusters/$CLUSTER/redis-lab-owner.json"
CLIENT=redis-gate-client
lab_mutated=no
export KUBECONFIG="$KC" KUBEAUTO_CLUSTER="$CLUSTER"
export REDIS_NAMESPACE=redis-gate REDIS_OPERATOR_NAMESPACE=redis-gate-operator
export REDIS_CRD_OWNERSHIP_FILE="$BASE/clusters/$CLUSTER/redis-owned-crds"
export REDIS_HELM="$HELM"
control_config="${REDIS_CONTROL_KUBECONFIG:-$HOME/.kube/config}"

lease="${REDIS_LAB_LEASE_FILE:-/var/lock/kubeauto-redis-core-pool.lock}"
if [[ "${REDIS_PARENT_LEASE_HELD:-no}" == yes ]]; then
  [[ /proc/self/fd/9 -ef "$lease" ]] || exit 1
else
  exec 9>"$lease"
fi
flock -n 9 || { echo 'REDIS_PREFLIGHT_FAIL class=environment reason=core-pool-leased' >&2; exit 1; }
work="$(mktemp -d /tmp/kubeauto-redis-install.XXXXXX)"
chmod 0700 "$work"
mkdir "$work/bin"
cat > "$work/bin/kubecli" <<'WRAPPER'
#!/usr/bin/env bash
set -eu
cd "${KUBEAUTO_BASE:?}"
export PYTHONPATH="$KUBEAUTO_BASE"
exec "$KUBEAUTO_BASE/.venv/bin/python" "$KUBEAUTO_BASE/kubecli.py" "$@"
WRAPPER
chmod 0755 "$work/bin/kubecli"
export KUBEAUTO_BASE="$BASE" PATH="$work/bin:$BASE/kube-bin:$BASE/extra-bin:$PATH"
KUBECLI="$work/bin/kubecli"
if [[ -e "$control_config" ]]; then
  cp -a "$control_config" "$work/control-kubeconfig"
fi

on_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT
  set +e
  if [[ "$rc" -ne 0 && "$lab_mutated" == yes ]]; then
    echo "REDIS_FIRST_FAILURE class=unclassified rc=$rc; collecting before cleanup" >&2
    if [[ -f "$KC" ]]; then
      kubectl --kubeconfig="$KC" get nodes -o wide
      for ns in redis-gate redis-gate-operator redis-lab-storage; do
        kubectl --kubeconfig="$KC" -n "$ns" get pods,pvc,svc -o wide
        kubectl --kubeconfig="$KC" -n "$ns" get events --sort-by=.lastTimestamp
        kubectl --kubeconfig="$KC" -n "$ns" describe pods
      done
      kubectl --kubeconfig="$KC" -n redis-gate-operator logs deployment/redis-operator --all-containers --tail=200
      bash "$BASE/tests/helpers/redis-cleanup.sh" || cleanup_rc=$?
    fi
    printf 'rc=%s cleanup_rc=%s\n' "$rc" "$cleanup_rc" > "$work/first-failure.status"
    # Preserve first failure and diagnostics. A failed baseline is not reusable.
    [[ ! -f "$STATE" ]] || "$PY" - "$STATE" "$rc" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);state=json.loads(p.read_text());state['healthy']=False;state['failure_rc']=int(sys.argv[2]);p.write_text(json.dumps(state))
PY
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    mkdir -p "$(dirname "$control_config")"
    cp -a "$work/control-kubeconfig" "$control_config"
  else
    rm -f "$control_config"
  fi
  # Credentials are generated in memory; this directory contains diagnostics only.
  [[ "$rc" -ne 0 ]] || rm -rf -- "$work"
  exit "$rc"
}
trap on_exit EXIT

echo 'REDIS_STAGE_BEGIN stage=lab-baseline'
if [[ -f "$STATE" ]]; then
  "$PY" - "$STATE" "$MODE" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]))
assert s['nodes']==[f'192.168.47.{i}' for i in range(131,137)]
assert s['cluster']=='redis-gate'
if sys.argv[2]=='--repair-core':
 assert s.get('healthy') is False and s.get('failure_rc')==1
PY
  if [[ "$MODE" != --repair-core ]]; then
    echo 'REDIS_PREFLIGHT_FAIL class=test-gate reason=use-focused-modules-for-existing-baseline' >&2; exit 1
  fi
  [[ "$(kubectl --kubeconfig="$KC" get --raw /readyz)" == ok ]]
  kubectl --kubeconfig="$KC" get nodes -o json > "$work/nodes.json"
  kubectl --kubeconfig="$KC" get namespaces -o json > "$work/namespaces.json"
  "$PY" "$BASE/tests/helpers/redis-lab-config.py" verify-core --allow-missing-worker \
    --nodes-json "$work/nodes.json" --namespaces-json "$work/namespaces.json" --owner-json "$STATE"
  if ! kubectl --kubeconfig="$KC" get node redis-node-133 --ignore-not-found -o name | grep -q .; then
    ssh -o BatchMode=yes -o ConnectTimeout=10 root@192.168.47.133 \
      '/usr/bin/python3.9 -I -c "import base64,re,enum; print(\"REDIS_NODE_PYTHON_IMPORT_PASS\")"'
    lab_mutated=yes
    # Resume through kubecli; retain the five healthy nodes and all core artifacts.
    "$KUBECLI" setup "$CLUSTER" 01 --limit localhost,192.168.47.133 </dev/null
    for step in 03 05 06; do
      "$KUBECLI" setup "$CLUSTER" "$step" --limit 192.168.47.133 </dev/null
    done
  fi
else
  [[ "$MODE" == --fresh && ! -e "$BASE/clusters/$CLUSTER" ]] || {
    echo 'REDIS_PREFLIGHT_FAIL class=environment reason=foreign-or-unverified-cluster-directory' >&2; exit 1;
  }
  bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
  lab_mutated=yes
  "$KUBECLI" new "$CLUSTER" </dev/null
  "$PY" "$BASE/tests/helpers/redis-lab-config.py" configure --core-only --config "$BASE/clusters/$CLUSTER/config.yml"
  "$PY" - "$STATE" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);p.write_text(json.dumps({'cluster':'redis-gate','nodes':[f'192.168.47.{i}' for i in range(131,137)],'healthy':False}));p.chmod(0o600)
PY
  "$KUBECLI" download -D </dev/null
  "$KUBECLI" download -E redis </dev/null
  "$KUBECLI" setup "$CLUSTER" 90 </dev/null
fi
kubectl --kubeconfig="$KC" wait --for=condition=Ready nodes --all --timeout=10m
if [[ -f "$BASE/clusters/$CLUSTER/redis-storage-owned.json" ]]; then
  "$PY" "$BASE/tests/helpers/redis-lab-storage.py" health --base "$BASE"
else
  "$PY" "$BASE/tests/helpers/redis-lab-storage.py" prepare --base "$BASE"
fi
lab_mutated=yes
"$PY" "$BASE/tests/helpers/redis-lab-config.py" configure --config "$BASE/clusters/$CLUSTER/config.yml"

echo 'REDIS_STAGE_BEGIN stage=credentials'
# Every namespace must be absent before fixture creation; never overwrite labels.
for ns in redis-gate redis-gate-operator; do
  [[ -z "$(kubectl --kubeconfig="$KC" get namespace "$ns" --ignore-not-found -o name)" ]]
  kubectl --kubeconfig="$KC" create namespace "$ns"
  kubectl --kubeconfig="$KC" label namespace "$ns" kubeauto.io/component=redis kubeauto.io/managed-by=kubeauto
done
"$PY" "$BASE/tests/helpers/redis-lab-config.py" credentials |
  kubectl --kubeconfig="$KC" create -f - >/dev/null
# Journal only CRDs that were absent before product Helm. Do not own shared CRDs.
: > "$REDIS_CRD_OWNERSHIP_FILE"
for resource in redis redisclusters redisreplications redissentinels; do
  crd="$resource.redis.redis.opstreelabs.in"
  if [[ -z "$(kubectl --kubeconfig="$KC" get crd "$crd" --ignore-not-found -o name)" ]]; then
    printf '%s\n' "$crd" >> "$REDIS_CRD_OWNERSHIP_FILE"
  fi
done
echo 'REDIS_STAGE_BEGIN stage=product-install'
bash "$BASE/tests/helpers/redis-regression.sh" --install-only
kubectl --kubeconfig="$KC" -n redis-gate delete pod "$CLIENT" --wait=true --timeout=120s
[[ -z "$(kubectl --kubeconfig="$KC" -n redis-gate get pod -l kubeauto.io/component=redis-test -o name)" ]]
"$PY" - "$STATE" "$KC" <<'PY'
import json,subprocess,sys
from pathlib import Path
p=Path(sys.argv[1]);s=json.loads(p.read_text())
n=json.loads(subprocess.check_output(['kubectl','--kubeconfig='+sys.argv[2],'get','namespace','kube-system','-o','json']))
s.update(healthy=True,cluster_uid=n['metadata']['uid']);p.write_text(json.dumps(s));p.chmod(0o600)
PY
echo REDIS_INSTALL_SCOPED_CLEAN_PASS
echo REDIS_INSTALL_MODULE_PASS
