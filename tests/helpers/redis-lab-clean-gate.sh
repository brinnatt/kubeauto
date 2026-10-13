#!/usr/bin/env bash
# Remove only the leased Redis -> PV -> CSI -> core boundary, in dependency order.
set -Eeuo pipefail
MODE="${1:---clean}"
[[ "$MODE" == --clean || "$MODE" == --verify ]] || exit 2
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PY="$BASE/.venv/bin/python"
CLUSTER="$BASE/clusters/redis-gate"
KC="$CLUSTER/kubectl.kubeconfig"
NODES=(192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136)
lease="${REDIS_LAB_LEASE_FILE:-/var/lock/kubeauto-redis-core-pool.lock}"
if [[ "${REDIS_PARENT_LEASE_HELD:-no}" == yes ]]; then
  [[ /proc/self/fd/9 -ef "$lease" ]] || exit 1
else
  exec 9>"$lease"
fi
flock -n 9 || { echo 'REDIS_PREFLIGHT_FAIL class=environment reason=core-pool-leased' >&2; exit 1; }
export PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH" KUBECONFIG="$KC"
verify() {
  [[ ! -e "$CLUSTER" && ! -e "$BASE/clusters/redis-cluster-gate" ]]
  bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
  echo REDIS_LAB_CLEAN_VERIFY_PASS
}
[[ "$MODE" != --verify ]] || { verify; exit; }
if [[ ! -e "$CLUSTER" ]]; then
  verify
  echo REDIS_LAB_CLEANUP_PASS
  exit
fi
# Refuse foreign directories, nodes, workload namespaces or unrecovered host
# faults before any mutation. This file never touches another cluster directory.
"$PY" - "$BASE" "$KC" <<'PY'
import hashlib,json,subprocess,sys
from pathlib import Path
import yaml
base=Path(sys.argv[1]);root=base/'clusters/redis-gate'
assert not root.is_symlink()
owner=json.loads((root/'redis-lab-owner.json').read_text())
assert owner['cluster']=='redis-gate' and owner['nodes']==[f'192.168.47.{i}' for i in range(131,137)]
config=yaml.safe_load((root/'config.yml').read_text())
assert config['CLUSTER_NAME']=='redis-gate' and config['redis_namespace']=='redis-gate'
def get(kind):
 return json.loads(subprocess.check_output(['kubectl','--kubeconfig='+sys.argv[2],'get',kind,'-o','json']))['items']
namespaces=get('namespace')
assert next(n['metadata']['uid'] for n in namespaces if n['metadata']['name']=='kube-system')==owner['cluster_uid']
allowed={'default','kube-system','kube-public','kube-node-lease'}
for ns in namespaces:
 name=ns['metadata']['name'];labels=ns['metadata'].get('labels',{})
 if name in allowed: continue
 expected={'redis-gate':'redis','redis-gate-operator':'redis','redis-lab-storage':'redis-test-storage'}
 assert name in expected and labels.get('kubeauto.io/component')==expected[name] and labels.get('kubeauto.io/managed-by')=='kubeauto'
addresses={a['address'] for n in get('nodes') for a in n['status']['addresses'] if a['type']=='InternalIP'}
assert addresses==set(owner['nodes'])
assert not list(root.glob('*fault*.json')) and not list(root.glob('*fault*.tmp'))
target=base/'clusters/redis-cluster-gate';journal=root/'redis-cluster-fixture.json'
if target.exists():
 assert not target.is_symlink() and journal.is_file()
 state=json.loads(journal.read_text())
 assert state['cluster_uid']==owner['cluster_uid'] and state['name']=='redis-gate-cluster-data' and state['namespace']=='redis-gate'
 assert json.loads((target/'redis-fixture-owner.json').read_text())['run_id']==state['run_id']
 assert hashlib.sha256((target/'config.yml').read_bytes()).hexdigest()==state['config_sha256']
else: assert not journal.exists()
print('REDIS_LAB_CLEAN_OWNERSHIP_PASS cluster-and-nodes=verified faults=none')
PY
export REDIS_NAMESPACE=redis-gate REDIS_OPERATOR_NAMESPACE=redis-gate-operator
export REDIS_HELM="$BASE/extra-bin/helm" REDIS_CRD_OWNERSHIP_FILE="$CLUSTER/redis-owned-crds"
bash "$BASE/tests/helpers/redis-cleanup.sh"
# Delete reclaim policy is fixture-only. Wait for the owned storage dependency
# to vanish before uninstalling its controller or deleting host data.
"$PY" - "$KC" <<'PY'
import json,subprocess,sys,time
cmd=['kubectl','--kubeconfig='+sys.argv[1]]
deadline=time.monotonic()+300
while time.monotonic()<deadline:
 pvs=json.loads(subprocess.check_output([*cmd,'get','pv','-o','json']))['items']
 if not any(p['spec'].get('storageClassName')=='redis-longhorn' for p in pvs): break
 print('REDIS_LAB_CLEAN_HEARTBEAT stage=pv-release',flush=True);time.sleep(3)
else: raise SystemExit('REDIS_LAB_CLEAN_FAIL owned PV dependency remains')
print('REDIS_OWNED_PV_CLEAN_PASS')
PY
"$PY" "$BASE/tests/helpers/redis-lab-storage.py" cleanup --base "$BASE"
"$PY" "$BASE/tests/helpers/redis-lab-storage.py" verify --base "$BASE"
bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${NODES[@]}"
bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
"$PY" - "$BASE" <<'PY'
from pathlib import Path
import shutil,sys
base=Path(sys.argv[1])
# All owner/UID checks ran before cleanup; use the two exact non-symlink paths.
for name in ('redis-cluster-gate','redis-gate'):
 path=base/'clusters'/name
 assert not path.is_symlink()
 if path.exists(): shutil.rmtree(path)
PY
verify
echo REDIS_LAB_CLEANUP_PASS
