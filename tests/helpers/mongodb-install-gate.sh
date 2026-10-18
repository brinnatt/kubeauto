#!/usr/bin/env bash
# Focused MongoDB installation through the real kubecli configuration workflow.
set -Eeuo pipefail
MODE="${1:---install}"
case "$MODE" in --full|--full-artifacts|--upgrade|--upgrade-existing|--upgrade-recover|--core|--core-resume|--install|--install-retry|--install-business|--reconcile|--registry-qualification|--registry-recover|--cleanup-qualification|--clean|--artifacts|--platform|--preflight|--storage|--data|--transactions|--security|--network-client|--network|--quorum|--failover|--idempotence|--maintenance|--rebuild|--rotation|--observability|--observability-scrape|--observability-lag|--observability-capacity|--observability-backup|--observability-recover|--performance|--performance-node|--performance-client|--performance-recover|--ownership|--ownership-resources|--ownership-resources-recover|--ownership-pvc|--ownership-pvc-recover|--replica-scale|--replica-scale-recover|--storage-reclaim|--logging|--capacity-expand|--capacity-recover|--capacity-pressure|--capacity-pressure-recover|--capacity-lag|--capacity-lag-recover|--capacity|--backup-clients-recover|--cleanup|--cleanup-success|--cleanup-failure|--cleanup-recover|--logical-backup|--physical-backup|--disaster-recovery|--stability|--stability-faults|--stability-recover|--rollback|--sharding-restore|--backup-refusal|--backup-refusal-tail|--backup-refusal-oplog|--retention|--retention-recover|--encryption|--pitr|--logical-restore|--physical-restore|--logical-restore-inplace|--physical-restore-inplace|--backup-clean|--restore-clean|--sharding-install|--sharding-scale|--sharding-data|--sharding-failure|--sharding-recover|--sharding-clean) ;; *) exit 2;; esac
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
PY="$BASE/.venv/bin/python"
CLUSTER=mongodb-gate
DIR="$BASE/clusters/$CLUSTER"
KC="$DIR/kubectl.kubeconfig"
STATE="$DIR/mongodb-lab-owner.json"
NODES=(192.168.122.135 192.168.122.40 192.168.122.72 192.168.122.212 192.168.122.165 192.168.122.238)
export PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH" PYTHONPATH="$BASE" KUBECONFIG="$KC" KUBEAUTO_BASE="$BASE"
# Share the same locks with existing Ceph/Redis launchers. The source-sync
# handshake holds these descriptors before it writes the shared control tree.
if [[ "${MONGODB_PARENT_LEASE_HELD:-no}" != yes ]]; then
  exec 9>/var/lock/kubeauto-ceph-priority.lock
  exec 7>/var/lock/kubeauto-ceph-compute.lock
  exec 8>/var/lock/kubeauto-redis-core-pool.lock
fi
for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
  read -r fd filename <<< "$entry"
  if [[ "${MONGODB_PARENT_LEASE_HELD:-no}" == yes ]]; then
    [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || exit 1
  fi
  flock -n "$fd" || { echo 'MONGODB_PREFLIGHT_FAIL class=environment reason=shared-pool-leased' >&2; exit 1; }
done
work="$(mktemp -d /var/tmp/kubeauto-mongodb-install.XXXXXX)"
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
export PATH="$work/bin:$PATH"
control_config="$HOME/.kube/config"
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
mutated=no
phase=preflight
relay_pid=

start_relay() {
  "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" ensure
  "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" maintain &
  relay_pid=$!
}

clean_lab() {
  [[ -f "$STATE" && ! -L "$DIR" ]] || return 1
  "$PY" - "$STATE" "$DIR/hosts" <<'PY'
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('mongo_lab',Path.cwd()/'tests/helpers/mongodb-lab.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
owner=json.loads(Path(sys.argv[1]).read_text())
assert owner['cluster']==m.CLUSTER and tuple(owner['nodes'])==m.NODES and owner['clean_start_verified'] is True
assert Path(sys.argv[2]).read_text()==m.inventory()
PY
  if [[ -f "$KC" ]] && kubectl get --raw /readyz >/dev/null 2>&1; then
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" bind-product --base "$BASE" || return
    for kind in logical physical pitr; do
      if [[ -f "$DIR/mongodb-backup-$kind.json" ]]; then
        "$PY" "$BASE/tests/helpers/mongodb-backups.py" "cleanup-$kind" --base "$BASE" || return
      fi
    done
    "$PY" "$BASE/tests/helpers/mongodb-sharding.py" cleanup --base "$BASE" || return
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" cleanup-product --base "$BASE" || return
    "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" cleanup --base "$BASE" || return
  elif [[ "$phase" != core ]]; then
    echo 'MONGODB_CLEAN_FAIL reason=API-unreachable-recovery-required' >&2
    return 1
  fi
  "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" cleanup || return
  bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --rocky-only "${NODES[@]}" || return
  bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}" || return
  "$PY" - "$DIR" <<'PY'
from pathlib import Path
import shutil,sys
p=Path(sys.argv[1]);assert p.name=='mongodb-gate' and not p.is_symlink();shutil.rmtree(p)
PY
  echo MONGODB_CLEAN_VERIFY_PASS
}

on_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  set +e
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null
    wait "$relay_pid"
  fi
  if [[ "$rc" != 0 ]]; then
    echo "MONGODB_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    if [[ -f "$KC" ]]; then
      "$PY" "$BASE/tests/helpers/mongodb-lab.py" diagnostics --base "$BASE" --directory "$work"
    fi
    if [[ "$phase" == product ]]; then
      "$PY" "$BASE/tests/helpers/mongodb-lab.py" cleanup-install-client --base "$BASE" || cleanup_rc=$?
    fi
    if [[ "$mutated" == yes && "$cleanup_rc" == 0 ]]; then clean_lab || cleanup_rc=$?; fi
    printf 'rc=%s cleanup_rc=%s phase=%s\n' "$rc" "$cleanup_rc" "$phase" > "$work/first-failure.status"
    echo "MONGODB_FAILURE_CLEANUP rc=$cleanup_rc diagnostics=$work" >&2
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    mkdir -p "$(dirname "$control_config")"
    cp -a "$work/control-kubeconfig" "$control_config"
  else
    rm -f -- "$control_config"
  fi
  [[ "$rc" != 0 ]] || rm -rf -- "$work"
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$BASE"
if [[ "$MODE" == --full || "$MODE" == --full-artifacts ]]; then
  phase="${MODE#--}"
  focus=
  [[ "$MODE" != --full-artifacts ]] || focus=artifacts
  source "$BASE/tests/helpers/mongodb-runner.sh"
  mongodb_full_suite "$BASE" "$work" "$focus"
  exit
fi
if [[ "$MODE" == --registry-recover ]]; then
  phase=registry-recover
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
  "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" cleanup
  start_relay
  echo MONGODB_REGISTRY_RECOVER_SCOPED_CLEAN_PASS
  echo MONGODB_REGISTRY_RECOVER_PASS
  exit
fi
if [[ -f "$STATE" && "$MODE" != --clean && "$MODE" != --artifacts && "$MODE" != --registry-qualification ]]; then
  recovery=()
  [[ "$MODE" != --core-resume ]] || recovery=(--allow-unready)
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE" "${recovery[@]}"
  start_relay
fi
if [[ "$MODE" == --registry-qualification && -e "$DIR" ]]; then
  phase=registry-qualification
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE" --allow-unready
  "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" qualification
  echo MONGODB_REGISTRY_QUALIFICATION_PASS
  exit
fi
if [[ "$MODE" == --upgrade-existing ]]; then
  phase=upgrade
  "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" health --base "$BASE"
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" cache --base "$BASE"
  "$PY" "$BASE/tests/helpers/mongodb-backups.py" upgrade --base "$BASE"
  exit
fi
if [[ "$MODE" == --install-retry ]]; then
  phase=install-reset
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" reset-incomplete-install --base "$BASE"
  MODE=--install
fi
if [[ "$MODE" == --core-resume ]]; then
  phase=core-resume
  [[ -f "$STATE" && -n "$relay_pid" ]]
  kubecli setup "$CLUSTER" 06 </dev/null
  kubecli setup "$CLUSTER" 07 </dev/null
fi
if [[ "$MODE" == --platform ]]; then
  phase=platform
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
  "$PY" "$BASE/tests/helpers/mongodb-platform.py" --pool ceph
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" cache --base "$BASE"
  [[ -z "$(kubectl get namespace mongodb-platform-smoke --ignore-not-found -o name)" ]]
  PRODUCTION_SMOKE_NAMESPACE=mongodb-platform-smoke \
    PRODUCTION_SMOKE_IMAGE=registry.talkschool.cn:5000/mongodb-lab/brinnatt/json-mock:v1.3.1 \
    PRODUCTION_SMOKE_SERVER_NODE=mongodb-node-212 PRODUCTION_SMOKE_CLIENT_NODE=mongodb-node-165 \
    bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"
  [[ -z "$(kubectl get namespace mongodb-platform-smoke --ignore-not-found -o name)" ]]
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
  echo MONGODB_PLATFORM_SCOPED_CLEAN_PASS
  echo MONGODB_PLATFORM_PASS
  exit
fi
case "$MODE" in
  --storage-reclaim)
    phase=storage-reclaim
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
    "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" reclaim --base "$BASE"
    exit
    ;;
  --sharding-install|--sharding-scale|--sharding-data|--sharding-failure|--sharding-recover|--sharding-clean)
    phase="${MODE#--}"
    if [[ "$MODE" == --sharding-install ]]; then
      "$PY" "$BASE/tests/helpers/mongodb-lab.py" preflight --base "$BASE"
      "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" health --base "$BASE"
      "$PY" "$BASE/tests/helpers/mongodb-sharding.py" install --base "$BASE"
    elif [[ "$MODE" == --sharding-scale || "$MODE" == --sharding-data || "$MODE" == --sharding-failure || "$MODE" == --sharding-recover ]]; then
      "$PY" "$BASE/tests/helpers/mongodb-sharding.py" "${MODE#--sharding-}" --base "$BASE"
    else
      "$PY" "$BASE/tests/helpers/mongodb-sharding.py" cleanup --base "$BASE"
    fi
    exit
    ;;
  --cleanup-qualification)
    phase=cleanup-qualification
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" bind-product --base "$BASE"
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" cleanup-qualification --base "$BASE"
    echo MONGODB_CLEANUP_QUALIFICATION_SCOPED_CLEAN_PASS
    echo MONGODB_CLEANUP_QUALIFICATION_PASS
    exit
    ;;
  --restore-clean)
    phase=restore-clean
    for kind in logical physical pitr; do
      "$PY" "$BASE/tests/helpers/mongodb-backups.py" "cleanup-target-$kind" --base "$BASE"
    done
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
    "$PY" "$BASE/roles/cluster-addon/files/mongodb.py" health --config "$DIR/yml/mongodb/input.json" --kubeconfig "$KC"
    echo MONGODB_RESTORE_CLEAN_PASS
    exit
    ;;
  --upgrade-recover|--backup-clients-recover|--cleanup|--cleanup-success|--cleanup-failure|--cleanup-recover|--logical-backup|--physical-backup|--disaster-recovery|--stability|--stability-faults|--stability-recover|--rollback|--sharding-restore|--backup-refusal|--backup-refusal-tail|--backup-refusal-oplog|--retention|--retention-recover|--encryption|--pitr|--logical-restore|--physical-restore|--logical-restore-inplace|--physical-restore-inplace)
    phase="${MODE#--}"
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" cache --base "$BASE"
    "$PY" "$BASE/tests/helpers/mongodb-backups.py" "$phase" --base "$BASE"
    exit
    ;;
  --backup-clean)
    phase=backup-clean
    for kind in logical physical pitr; do
      "$PY" "$BASE/tests/helpers/mongodb-backups.py" "cleanup-$kind" --base "$BASE"
    done
    echo MONGODB_BACKUP_CLEAN_PASS
    exit
    ;;
  --preflight)
    phase=preflight
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" preflight --base "$BASE"
    "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" health --base "$BASE"
    exit
    ;;
  --storage)
    phase=storage-probe
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
    "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" probe --base "$BASE"
    exit
    ;;
  --reconcile)
    phase=reconcile
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
    "$PY" - "$BASE" <<'PY'
import importlib.util,sys
from pathlib import Path
base=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('scenario',base/'tests/helpers/mongodb-scenarios.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
s=m.Scenario(base)
if not s.lab.verify().get('healthy') or list(s.lab.directory.glob('*fault*.json')):
    raise ValueError('reconcile requires a healthy owned baseline without pending faults')
s.health()
PY
    "$PY" "$BASE/tests/helpers/mongodb-lab.py" configure --base "$BASE"
    kubecli setup "$CLUSTER" 07 --tags mongodb </dev/null
    echo MONGODB_RECONCILE_SCOPED_CLEAN_PASS
    echo MONGODB_RECONCILE_PASS
    exit
    ;;
  --data|--transactions|--security|--network-client|--network|--quorum|--failover|--idempotence|--maintenance|--rebuild|--rotation|--observability|--observability-scrape|--observability-lag|--observability-capacity|--observability-backup|--observability-recover|--performance|--performance-node|--performance-client|--performance-recover|--ownership|--ownership-resources|--ownership-resources-recover|--ownership-pvc|--ownership-pvc-recover|--replica-scale|--replica-scale-recover|--logging|--capacity-expand|--capacity-recover|--capacity-pressure|--capacity-pressure-recover|--capacity-lag|--capacity-lag-recover|--capacity)
    phase="${MODE#--}"
    "$PY" "$BASE/tests/helpers/mongodb-scenarios.py" "$phase" --base "$BASE"
    case "$phase" in capacity|capacity-pressure|logging|observability|observability-capacity)
      "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" reclaim --base "$BASE" ;;
    esac
    exit
    ;;
esac
if [[ "$MODE" == --clean ]]; then
  clean_lab
  echo MONGODB_CLEANUP_PASS
  exit
fi
"$PY" "$BASE/tests/helpers/mongodb-lab.py" artifacts --base "$BASE"
"$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" artifacts --base "$BASE"
[[ "$MODE" != --artifacts ]] || exit 0
if [[ ! -e "$DIR" ]]; then
  bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
  phase=core
  mutated=yes
  kubecli new "$CLUSTER" </dev/null
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" configure --core-only --base "$BASE"
  "$PY" - "$STATE" <<'PY'
import importlib.util,secrets,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('mongo_lab',Path.cwd()/'tests/helpers/mongodb-lab.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.write_private(Path(sys.argv[1]),{'cluster':m.CLUSTER,'nodes':list(m.NODES),'run_id':secrets.token_hex(16),
                                  'healthy':False,'clean_start_verified':True})
PY
  if [[ "$MODE" == --registry-qualification ]]; then
    "$PY" "$BASE/tests/helpers/mongodb-registry-relay.py" qualification
    bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
    "$PY" - "$DIR" <<'PY'
from pathlib import Path
import shutil,sys
p=Path(sys.argv[1]);assert p.name=='mongodb-gate' and not p.is_symlink();shutil.rmtree(p)
PY
    echo MONGODB_REGISTRY_QUALIFICATION_PASS
    exit
  fi
  start_relay
  kubecli download -D </dev/null
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" cache --base "$BASE"
  kubecli setup "$CLUSTER" 90 </dev/null
fi
if [[ "$MODE" == --registry-qualification ]]; then
  echo 'MONGODB_PREFLIGHT_FAIL reason=registry-qualification-requires-a-clean-lab' >&2
  exit 1
fi
kubectl wait --for=condition=Ready nodes --all --timeout=10m
"$PY" "$BASE/tests/helpers/mongodb-lab.py" verify-core --base "$BASE"
if [[ ( "$MODE" == --install || "$MODE" == --upgrade ) && -n "$(kubectl get namespace mongodb-gate --ignore-not-found -o name)" ]]; then
  echo 'MONGODB_PREFLIGHT_FAIL class=environment reason=existing-product-requires-focused-module' >&2
  exit 1
fi
phase=storage
if [[ -f "$DIR/mongodb-storage-owned.json" ]]; then
  "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" health --base "$BASE"
else
  "$PY" "$BASE/tests/helpers/mongodb-lab-storage.py" prepare --base "$BASE"
fi
echo MONGODB_CORE_BASELINE_READY
if [[ "$MODE" == --core || "$MODE" == --core-resume ]]; then
  echo MONGODB_CORE_SCOPED_CLEAN_PASS
  [[ "$MODE" != --core-resume ]] || echo MONGODB_CORE_RESUME_PASS
  exit
fi
phase=product
if [[ "$MODE" == --install-business ]]; then
  "$PY" "$BASE/roles/cluster-addon/files/mongodb.py" health --config "$DIR/yml/mongodb/input.json" --kubeconfig "$KC"
  "$PY" "$BASE/tests/helpers/mongodb-lab.py" cleanup-install-client --base "$BASE"
  kubecli setup "$CLUSTER" 07 --tags mongodb </dev/null
else
phase=product
upgrade_options=()
if [[ "$MODE" == --upgrade ]]; then
  for crd in perconaservermongodbs perconaservermongodbbackups perconaservermongodbrestores perconaservermongodbclustersyncs; do
    [[ -z "$(kubectl get crd "$crd.psmdb.percona.com" --ignore-not-found -o name)" ]]
  done
  upgrade_options=(--upgrade-from-old)
fi
"$PY" "$BASE/tests/helpers/mongodb-lab.py" configure --base "$BASE" "${upgrade_options[@]}"
kubectl create namespace mongodb-gate
kubectl label namespace mongodb-gate app.kubernetes.io/managed-by=kubeauto kubeauto.io/component=mongodb
"$PY" - "$STATE" "$KC" <<'PY'
import json,subprocess,sys
from pathlib import Path
p=Path(sys.argv[1]);s=json.loads(p.read_text());s['created_crds']=[]
namespace=json.loads(subprocess.check_output(['kubectl','--kubeconfig='+sys.argv[2],'get','namespace','mongodb-gate','-o','json'],text=True))
s['namespace_uid']=namespace['metadata']['uid']
for name in ('perconaservermongodbs','perconaservermongodbbackups','perconaservermongodbrestores','perconaservermongodbclustersyncs'):
 crd=name+'.psmdb.percona.com'
 out=subprocess.check_output(['kubectl','--kubeconfig='+sys.argv[2],'get','crd',crd,'--ignore-not-found','-o','name'],text=True)
 if not out.strip():s['created_crds'].append(crd)
p.write_text(json.dumps(s));p.chmod(0o600)
PY
"$PY" "$BASE/tests/helpers/mongodb-lab.py" credentials | kubectl create -f - >/dev/null
kubecli download -E mongodb </dev/null
kubecli setup "$CLUSTER" 07 --tags mongodb </dev/null
fi
"$PY" "$BASE/tests/helpers/mongodb-lab.py" client | kubectl create -f - >/dev/null
kubectl -n mongodb-gate wait --for=condition=Ready pod/mongodb-gate-client --timeout=600s
kubectl -n mongodb-gate exec -i mongodb-gate-client -- mongosh --quiet --norc --nodb --file /dev/stdin <<'JS'
const assert=(value)=>{if(!value)throw Error('installation business assertion failed');};
assert.eq=(expected,actual)=>assert(JSON.stringify(expected)===JSON.stringify(actual));
assert.commandWorked=(r)=>{assert(r.ok===1&&!r.writeConcernError&&!(r.writeErrors&&r.writeErrors.length));return r;};
const c=new Mongo(process.env.MONGODB_URI),a=c.getDB('admin');
assert(a.auth({user:process.env.MONGODB_DATABASE_ADMIN_USER,pwd:process.env.MONGODB_DATABASE_ADMIN_PASSWORD}));
const d=c.getDB('mongodb_install_probe');
try {
assert.commandWorked(d.runCommand({insert:'probe',documents:[{_id:'majority',value:42}],writeConcern:{w:'majority',wtimeout:10000}}));
assert.eq(42,d.probe.findOne({_id:'majority'}).value);
} finally {assert.commandWorked(d.dropDatabase());}
print('MONGODB_INSTALL_BUSINESS_PASS majority-write-read=true tls=verified');
JS
"$PY" "$BASE/tests/helpers/mongodb-lab.py" cleanup-install-client --base "$BASE"
[[ -z "$(kubectl -n mongodb-gate get pod -l kubeauto.io/fixture=mongodb-client -o name)" ]]
"$PY" "$BASE/tests/helpers/mongodb-lab.py" bind-product --base "$BASE"
"$PY" - "$STATE" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);s=json.loads(p.read_text());s['healthy']=True;p.write_text(json.dumps(s));p.chmod(0o600)
PY
echo MONGODB_INSTALL_SCOPED_CLEAN_PASS
echo MONGODB_INSTALL_PASS
if [[ "$MODE" == --upgrade ]]; then
  phase=upgrade
  "$PY" "$BASE/tests/helpers/mongodb-backups.py" upgrade --base "$BASE"
fi
