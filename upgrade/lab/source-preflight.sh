#!/usr/bin/env bash
# First-hop source admission runner. Execute on the fixed Rocky jumper (130).
# It creates and owns one disposable v1.33.6 source cluster, captures a
# private source/recovery receipt, and leaves the healthy cluster leased for
# the following upgrade stages. No target component is mutated here.
set -euo pipefail

BASE=/usr/local/kubeauto
CLUSTER=upgrade-v1336-source
STATE=/var/tmp/kubeauto-upgrade-v1336-source
LEASE=${STATE}.lease
RECEIPT=${STATE}.receipt.json
LOG=${STATE}.log
KUBECONFIG_PATH="$BASE/clusters/$CLUSTER/kubectl.kubeconfig"
export PYTHONPATH="$BASE"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
umask 077

fail() { echo "UPGRADE_SOURCE_PREFLIGHT_FAIL: $*" >&2; exit 1; }
pass() { echo "UPGRADE_SOURCE_PREFLIGHT_PASS: $*"; }

[[ "$(id -u)" == 0 ]] || fail "runner requires the fixed jumper root account"
[[ -x "$BASE/.venv/bin/python" ]] || fail "project runtime is unavailable"
[[ ! -e "$LEASE" ]] || fail "source lease already exists"
[[ ! -e "$BASE/clusters/$CLUSTER" ]] || fail "owned cluster directory already exists"

mkdir -m 0700 "$STATE"
touch "$LOG"
exec > >(tee -a "$LOG") 2>&1
cleanup_on_failure() {
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    kubecli destroy "$CLUSTER" </dev/null >/dev/null 2>&1 || true
    rm -rf "$BASE/clusters/$CLUSTER" "$STATE"
  fi
  exit "$rc"
}
trap cleanup_on_failure EXIT INT TERM

kubecli new "$CLUSTER" </dev/null
cat > "$BASE/clusters/$CLUSTER/hosts" <<'HOSTS'
[etcd]
192.168.47.134
192.168.47.135
192.168.47.136

[kube_master]
192.168.47.134 k8s_nodename='upgrade-master-134'
192.168.47.135 k8s_nodename='upgrade-master-135'
192.168.47.136 k8s_nodename='upgrade-master-136'

[kube_node]
192.168.47.131 k8s_nodename='upgrade-worker-131'
192.168.47.132 k8s_nodename='upgrade-worker-132'
192.168.47.133 k8s_nodename='upgrade-worker-133'

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
cluster_dir="{{ base_dir }}/clusters/upgrade-v1336-source"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
# Reserved-resource qualification belongs to the dedicated 137/138 paths;
# this six-node source fixture keeps the customer sizing path independent.
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/$CLUSTER/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/$CLUSTER/config.yml"
kubecli setup "$CLUSTER" 90 </dev/null

export KUBECONFIG="$KUBECONFIG_PATH"
for attempt in $(seq 1 30); do
  ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/ {count++} END {print count+0}')
  total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
  echo "[WAIT] source nodes_ready=${ready}/${total} attempt=${attempt}/30"
  if [[ "$ready" -eq 6 && "$total" -eq 6 ]]; then break; fi
  [[ "$attempt" -lt 30 ]] || fail "source nodes did not reach 6/6 Ready"
  sleep 10
done
kubectl get nodes -o wide
bad_pods=$(kubectl get pods -A --no-headers | awk '$4!="Running" && $4!="Completed"{print}' | wc -l)
[[ "$bad_pods" -eq 0 ]] || fail "source has non-running pods: $bad_pods"
[[ "$(kubectl version -o json | "$BASE/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')" == v1.33.6 ]] || fail "source API is not v1.33.6"
[[ "$(kubectl get nodes -o json | "$BASE/.venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print(sorted({x["status"]["nodeInfo"]["kubeletVersion"] for x in d["items"]})[0])')" == v1.33.6 ]] || fail "source kubelets are not v1.33.6"

# Use the project's documented etcd backup entry point; it writes snapshot.db
# beside the source cluster and preserves the exact TLS/member identity.
kubecli backup "$CLUSTER" </dev/null
snapshot="$BASE/clusters/$CLUSTER/backup/snapshot.db"
[[ -s "$snapshot" ]] || fail "etcd snapshot is missing or empty"

"$BASE/.venv/bin/python" - "$RECEIPT" "$snapshot" <<'PY'
import hashlib, json, os, socket, subprocess, sys
from pathlib import Path
out, snapshot = map(Path, sys.argv[1:])
def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
nodes = json.loads(run('kubectl', 'get', 'nodes', '-o', 'json'))['items']
namespace = json.loads(run('kubectl', 'get', 'namespace', 'kube-system', '-o', 'json'))
value = {
    'schema_version': 1, 'source': 'v1.33.6', 'target': 'v1.34.13',
    'cluster': 'upgrade-v1336-source', 'host': socket.gethostname(),
    'mutation': 'none-after-setup', 'lease': str(out.with_suffix('.lease')),
    'cluster_identity': {'namespace': 'kube-system', 'uid': namespace['metadata']['uid']},
    'nodes': sorted([{'name': n['metadata']['name'], 'uid': n['metadata']['uid'],
                      'kubeletVersion': n['status']['nodeInfo']['kubeletVersion']}
                     for n in nodes], key=lambda x: x['name']),
    'api_backends': ['https://192.168.47.134:6443','https://192.168.47.135:6443','https://192.168.47.136:6443'],
    'recovery_point': {'kind': 'etcd-v3-snapshot', 'path': str(snapshot),
                       'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                       'bytes': snapshot.stat().st_size},
}
tmp = out.with_suffix('.tmp'); tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n'); tmp.chmod(0o600); os.replace(tmp, out)
PY
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LEASE"
chmod 0600 "$LEASE" "$RECEIPT"
trap - EXIT INT TERM
pass "source=v1.33.6 recovery=etcd-snapshot lease=$LEASE"
echo "UPGRADE_SOURCE_PREFLIGHT_PASS"
