#!/usr/bin/env bash
# Final first-hop regression owned by upgrade/v1.34.13.
# Run on the fixed jumper (192.168.47.130) after source synchronization.
# It owns only the two disposable clusters named below and always records a
# durable result before returning.  It does not touch customer clusters.
set -uo pipefail

BASE=/usr/local/kubeauto
STATE=/var/tmp/kubeauto-upgrade-v13413-final
SOURCE=upgrade-v1336-source
TARGET=upgrade-v13413-clean
RUN_ID=upgrade-v13413-final-20261115
LOG=$STATE/run.log
EXIT_FILE=$STATE/exit
FINALIZED_FILE=$STATE/finalized
LOCK=$STATE/lock
export PYTHONPATH=$BASE
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
umask 077

fail() { echo "UPGRADE_FINAL_FAIL: $*" >&2; return 1; }

repair_metrics_server_emptydir() {
  # The upgrade path intentionally keeps local-data protection enabled.  The
  # metrics-server cache is the one reviewed, stateless emptyDir consumer in
  # this frozen source fixture; delete only that owned Pod, then wait for its
  # Deployment to recreate it before resuming the active journal step.
  local kubeconfig="$BASE/clusters/$SOURCE/kubectl.kubeconfig"
  local kubectl="$BASE/kube-bin/kubectl"
  local state_json pod_name
  state_json=$(mktemp "$STATE/metrics-server-repair.XXXXXX") || return 1
  if ! "$kubectl" --kubeconfig="$kubeconfig" -n kube-system \
      get pods -l k8s-app=metrics-server -o json >"$state_json"; then
    rm -f "$state_json"
    return 1
  fi
  pod_name=$("$BASE/.venv/bin/python" - "$state_json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
items = data.get("items", [])
if len(items) != 1:
    raise SystemExit("expected exactly one metrics-server pod")
pod = items[0]
meta = pod.get("metadata", {})
if meta.get("namespace") != "kube-system" or meta.get("labels", {}).get("k8s-app") != "metrics-server":
    raise SystemExit("metrics-server pod identity mismatch")
owners = meta.get("ownerReferences", [])
if not any(o.get("kind") == "ReplicaSet" and o.get("name", "").startswith("metrics-server-")
           for o in owners):
    raise SystemExit("metrics-server pod is not owned by its reviewed ReplicaSet")
for volume in pod.get("spec", {}).get("volumes", []):
    if "emptyDir" in volume:
        continue
    if "projected" in volume and volume.get("name", "").startswith("kube-api-access-"):
        continue
    raise SystemExit("metrics-server pod has a non-ephemeral volume")
print(meta.get("name", ""))
PY
  ) || { rm -f "$state_json"; return 1; }
  rm -f "$state_json"
  [[ -n "$pod_name" ]] || return 1
  echo "UPGRADE_FINAL_METRICS_REPAIR_BEGIN pod=$pod_name"
  "$kubectl" --kubeconfig="$kubeconfig" -n kube-system \
    delete pod "$pod_name" --wait=true --timeout=120s || return 1
  for attempt in $(seq 1 30); do
    available=$("$kubectl" --kubeconfig="$kubeconfig" -n kube-system \
      get deployment metrics-server -o json 2>/dev/null |
      "$BASE/.venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); s=d.get("status", {}); print(int(s.get("availableReplicas", 0) == 1 and s.get("updatedReplicas", 0) == 1))' || echo 0)
    if [[ "$available" == 1 ]]; then
      echo UPGRADE_FINAL_METRICS_REPAIR_PASS
      return 0
    fi
    sleep 2
  done
  echo UPGRADE_FINAL_METRICS_REPAIR_FAIL >&2
  return 1
}
mkdir -p "$STATE" || exit 1
if [[ -e "$LOCK" ]]; then
  echo "UPGRADE_FINAL_REFUSED: active run exists" >&2
  exit 2
fi
printf '%s\n' "$$" > "$LOCK"
exec > >(tee -a "$LOG") 2>&1

rc=1
cleanup() {
  set +e
  echo "UPGRADE_FINAL_CLEANUP_BEGIN"
  if [[ "${SOURCE_BIN_ACTIVE:-0}" -eq 1 && -d "$TARGET_BIN" ]]; then
    rm -rf "$BASE/kube-bin"
    mv "$TARGET_BIN" "$BASE/kube-bin"
    SOURCE_BIN_ACTIVE=0
  fi
  # The names are fixed and the setup function below owns their inventories.
  # Cleanup is best effort but the final marker is withheld unless both
  # directories disappear and all declared hosts are free of this run's
  # cluster kubeconfigs.
  if [[ -d "$BASE/clusters/$TARGET" ]]; then
    "$BASE/.venv/bin/python" "$BASE/kubecli.py" destroy "$TARGET" </dev/null || true
  fi
  if [[ -d "$BASE/clusters/$SOURCE" ]]; then
    "$BASE/.venv/bin/python" "$BASE/kubecli.py" destroy "$SOURCE" </dev/null || true
  fi
  rm -f /var/tmp/kubeauto-upgrade-v1336-source.lease \
        /var/tmp/kubeauto-upgrade-v1336-source.receipt.json
  rm -rf /var/tmp/kubeauto-upgrade-v1336-source \
         /var/tmp/kubeauto-upgrade-v13413-clean
  if [[ ! -e "$BASE/clusters/$SOURCE" && ! -e "$BASE/clusters/$TARGET" &&
        ! -e /var/tmp/kubeauto-upgrade-v1336-source.lease &&
        ! -e /var/tmp/kubeauto-upgrade-v1336-source.receipt.json ]]; then
    echo LAB_CLEAN_VERIFY_PASS
    cleanup_rc=0
  else
    echo UPGRADE_FINAL_CLEANUP_FAIL
    cleanup_rc=1
  fi
  printf '%s\n' "$rc" > "$EXIT_FILE"
  if [[ "$rc" -eq 0 && "$cleanup_rc" -eq 0 ]]; then
    echo UPGRADE_FINAL_SCOPE_PASS
    printf '0\n' > "$FINALIZED_FILE"
  else
    printf '%s\n' "${rc:-1}" > "$FINALIZED_FILE"
  fi
  rm -f "$LOCK"
}
trap cleanup EXIT INT TERM

echo "RUN_ID=$RUN_ID"
echo "SOURCE=$SOURCE TARGET=$TARGET"
echo "FINAL_PREREQUISITE_CHECK"
cd "$BASE" || { rc=1; exit 1; }
"$BASE/.venv/bin/python" -B "$BASE/upgrade/validate.py" \
  "$BASE/upgrade/v1.34.13/test-matrix.yaml" --final-prerequisites || { rc=1; exit 1; }
echo UPGRADE_FINAL_PREREQUISITES_PASS

# Previous focused runs may leave exactly these two disposable clusters.  The
# final run owns them and removes only these names before creating fresh state.
for cluster in "$SOURCE" "$TARGET"; do
  if [[ -d "$BASE/clusters/$cluster" ]]; then
    "$BASE/.venv/bin/python" "$BASE/kubecli.py" destroy "$cluster" </dev/null || { rc=1; exit 1; }
  fi
done
rm -f /var/tmp/kubeauto-upgrade-v1336-source.lease \
      /var/tmp/kubeauto-upgrade-v1336-source.receipt.json
rm -rf /var/tmp/kubeauto-upgrade-v1336-source /var/tmp/kubeauto-upgrade-v13413-clean

echo FINAL_STEP=source-clean-setup-and-minor-upgrade
# The target branch carries v1.34.13 in kube-bin.  A source admission must
# consume the immutable v1.33.6 image instead of silently installing target
# binaries and calling the result a source cluster.  Keep the target files in
# a run-owned directory and restore them before the minor-upgrade command.
SOURCE_IMAGE=brinnatt/kubeauto-k8s-bin:v1.33.6
SOURCE_TAR="$BASE/down/k8s_bin_v1.33.6.tar"
SOURCE_BIN="$STATE/kube-bin-source"
TARGET_BIN="$STATE/kube-bin-target"
[[ -s "$SOURCE_TAR" ]] || { echo "UPGRADE_FINAL_FAIL: source binary pack missing"; rc=1; exit 1; }
rm -rf "$SOURCE_BIN" "$TARGET_BIN"
mkdir -m 700 "$SOURCE_BIN"
docker load -i "$SOURCE_TAR" >/dev/null || { rc=1; exit 1; }
source_container=$(docker create "$SOURCE_IMAGE") || { rc=1; exit 1; }
docker cp "$source_container:/k8s/." "$SOURCE_BIN/" || { docker rm -f "$source_container" >/dev/null 2>&1; rc=1; exit 1; }
docker rm -f "$source_container" >/dev/null || { rc=1; exit 1; }
test -x "$SOURCE_BIN/kube-apiserver" && test -x "$SOURCE_BIN/kubelet" || { rc=1; exit 1; }
mv "$BASE/kube-bin" "$TARGET_BIN"
mv "$SOURCE_BIN" "$BASE/kube-bin"
SOURCE_BIN_ACTIVE=1
if [[ -f "$BASE/clusters/$SOURCE/config.yml" ]]; then
  sed -i 's/^K8S_VER: "1\.34\.13"/K8S_VER: "1.33.6"/' "$BASE/clusters/$SOURCE/config.yml"
fi
bash "$BASE/upgrade/lab/source-preflight.sh" || { rc=1; exit 1; }
rm -rf "$BASE/kube-bin"
mv "$TARGET_BIN" "$BASE/kube-bin"
SOURCE_BIN_ACTIVE=0
# The product-owned first-hop coordinator deliberately commits the Kubernetes
# control-plane boundary first.  A second invocation resumes the same durable
# journal and advances each node boundary; both calls stay within this single
# source lease and the same product entry point.
"$BASE/.venv/bin/python" "$BASE/kubecli.py" upgrade "$SOURCE" </dev/null || { rc=1; exit 1; }
# Each worker can receive a newly recreated metrics-server Pod after the
# previous worker was repaired.  Resume the same durable journal after each
# individually admitted repair, with a hard bound equal to the six-node source
# topology so a different failure cannot loop indefinitely.
MAX_METRICS_REPAIRS=6
metrics_repairs=0
while :; do
  if "$BASE/.venv/bin/python" "$BASE/kubecli.py" upgrade "$SOURCE" </dev/null; then
    break
  fi
  if ! grep -q 'cannot delete Pods with local storage' "$LOG" ||
     ! grep -q 'metrics-server-' "$LOG"; then
    rc=1
    exit 1
  fi
  metrics_repairs=$((metrics_repairs + 1))
  if (( metrics_repairs > MAX_METRICS_REPAIRS )); then
    echo "UPGRADE_FINAL_FAIL: metrics-server repair bound exceeded" >&2
    rc=1
    exit 1
  fi
  repair_metrics_server_emptydir || { rc=1; exit 1; }
done

export KUBECONFIG="$BASE/clusters/$SOURCE/kubectl.kubeconfig"
"$BASE/kube-bin/kubectl" get nodes -o wide
test "$("$BASE/kube-bin/kubectl" get nodes --no-headers | wc -l)" -eq 6 || { rc=1; exit 1; }
test "$("$BASE/kube-bin/kubectl" get nodes --no-headers | awk '$5 == "v1.34.13" {n++} END {print n+0}')" -eq 6 || { rc=1; exit 1; }
"$BASE/kube-bin/kubectl" get --raw=/readyz?verbose >/dev/null || { rc=1; exit 1; }
test "$("$BASE/kube-bin/kubectl" get pods -A --no-headers | awk '$4 != "Running" && $4 != "Completed" {n++} END {print n+0}')" -eq 0 || { rc=1; exit 1; }
echo FINAL_SOURCE_UPGRADE_PASS

echo FINAL_STEP=target-clean-setup-and-idempotence
"$BASE/.venv/bin/python" "$BASE/kubecli.py" new "$TARGET" </dev/null || { rc=1; exit 1; }
cat > "$BASE/clusters/$TARGET/hosts" <<'HOSTS'
[etcd]
192.168.122.217
192.168.122.243
192.168.122.246

[kube_master]
192.168.122.217 k8s_nodename='clean-master-217'
192.168.122.243 k8s_nodename='clean-master-243'
192.168.122.246 k8s_nodename='clean-master-246'

[kube_node]
192.168.122.193 k8s_nodename='clean-worker-193'
192.168.122.210 k8s_nodename='clean-worker-210'

[harbor]
[ex_lb]
[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.73.0.0/16"
CLUSTER_CIDR="172.26.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/upgrade-v13413-clean"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
sed -i '1iREGISTRY_HOST_IP: "192.168.122.2"' "$BASE/clusters/$TARGET/config.yml"
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/$TARGET/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/$TARGET/config.yml"
"$BASE/.venv/bin/python" "$BASE/kubecli.py" setup "$TARGET" 90 </dev/null || { rc=1; exit 1; }
"$BASE/.venv/bin/python" "$BASE/kubecli.py" setup "$TARGET" 90 </dev/null || { rc=1; exit 1; }
export KUBECONFIG="$BASE/clusters/$TARGET/kubectl.kubeconfig"
"$BASE/kube-bin/kubectl" get nodes -o wide
test "$("$BASE/kube-bin/kubectl" get nodes --no-headers | awk '$5 == "v1.34.13" {n++} END {print n+0}')" -eq 5 || { rc=1; exit 1; }
"$BASE/kube-bin/kubectl" get --raw=/readyz?verbose >/dev/null || { rc=1; exit 1; }
"$BASE/kube-bin/kubectl" get --raw=/apis/metrics.k8s.io/v1beta1/nodes >/dev/null || { rc=1; exit 1; }
test "$("$BASE/kube-bin/kubectl" get pods -A --no-headers | awk '$4 != "Running" && $4 != "Completed" {n++} END {print n+0}')" -eq 0 || { rc=1; exit 1; }
echo FINAL_TARGET_INSTALL_PASS

echo UPGRADE_FINAL_COMPLETE_PLAN_PASS
rc=0
exit 0
