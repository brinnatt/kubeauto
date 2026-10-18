#!/usr/bin/env bash
# Sourced by the fixed top-level runner after its durable monitor functions.
ssh_mongodb() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 root@192.168.47.130 "$@"
}

mongodb_full_suite() {
  local base="$1" directory="$2" focus="${3:-}" module marker rc expected_steps step=0
  local python="$base/.venv/bin/python"
  [[ -z "$focus" || "$focus" == artifacts ]] || return 2
  [[ ! -e "$base/clusters/mongodb-gate" ]] || {
    echo 'MONGODB_FULL_PREFLIGHT_FAIL reason=requires-clean-start' >&2; return 1;
  }
  "$python" - "$base" "$directory/full-plan.tsv" "$focus" <<'PY' || return
import hashlib,json,sys
from pathlib import Path
import yaml
base,plan=map(Path,sys.argv[1:3]);focus=sys.argv[3]
matrix=yaml.safe_load((base/'tests/mongodb-test-matrix.yaml').read_text())
cases={c['group']:c for c in matrix['cases']}
for c in cases.values():
    if c['group']=='full':continue
    if c['status']!='pass' or not (base/c['evidence']).is_file():
        raise SystemExit('MONGODB_FULL_PREFLIGHT_FAIL unfinished='+c['id'])
static=json.loads((base/cases['static']['evidence']).read_text())
for name,digest in static['dependencies'].items():
    if hashlib.sha256((base/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit('MONGODB_FULL_PREFLIGHT_FAIL stale-static='+name)
modules='''artifacts upgrade clean install platform preflight storage data transactions security
failover quorum rebuild maintenance rotation logical-backup logical-restore physical-backup physical-restore
pitr encryption backup-refusal retention disaster-recovery network sharding-install sharding-data
sharding-failure sharding-scale sharding-clean sharding-restore replica-scale capacity observability
performance stability rollback idempotence ownership cleanup clean'''.split()
required=set(cases)-{'static','disabled','validation','documentation','full'}
if set(modules)-{'clean','sharding-clean'}!=required:
    raise SystemExit('MONGODB_FULL_PREFLIGHT_FAIL incomplete-runtime-plan')
rows=[]
for module in modules:
    marker=({'clean':'MONGODB_CLEAN_VERIFY_PASS','sharding-clean':'MONGODB_SHARDING_CLEAN_PASS'}.get(module)
            or cases[module]['success_marker'])
    rows.append(module+'\t'+marker)
plan.write_text('\n'.join(row for row in rows if not focus or row.startswith(focus+'\t'))+'\n')
print('MONGODB_FULL_PREFLIGHT_PASS prerequisite-cases=42 runtime-plan=complete static-inputs=current focus='+(focus or 'full'))
PY
  bash "$base/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
    192.168.122.135 192.168.122.40 192.168.122.72 \
    192.168.122.212 192.168.122.165 192.168.122.238 || return
  expected_steps=$(wc -l < "$directory/full-plan.tsv") || return
  while IFS=$'\t' read -r module marker; do
    step=$((step + 1))
    echo "MONGODB_FULL_MODULE_BEGIN step=$step module=$module"
    rc=0
    # Child SSH/clients must never read the parent's remaining module plan.
    bash "$base/tests/helpers/mongodb-install-gate.sh" "--$module" </dev/null \
      | tee "$directory/full-$step-$module.log" || rc=$?
    "$python" - "$module" "$marker" "$rc" "$directory/full-$step-$module.log" <<'PY' || return
import hashlib,json,re,sys
from pathlib import Path
module,marker,rc,filename=sys.argv[1:];raw=Path(filename).read_bytes();lines=raw.decode().splitlines()
failures=[s for s in lines if re.match(r'^(?:\[FAIL\]|MONGODB_[A-Z_]*(?:FAIL|FIRST_FAILURE)(?: |$))',s)]
result={'module':module,'rc':int(rc),'failure_markers':len(failures),'marker':marker,
        'marker_present':any(s == marker or s.startswith(marker + ' ') for s in lines),
        'log_sha256':hashlib.sha256(raw).hexdigest()}
with Path(filename).with_suffix('.status.json').open('w') as f:json.dump(result,f,sort_keys=True)
print('MONGODB_FULL_MODULE_RESULT '+json.dumps(result,sort_keys=True),flush=True)
if (result['rc'] or failures or not result['marker_present'] or
        (module=='clean' and 'LAB_CLEAN_VERIFY_PASS' not in lines)):
    raise SystemExit('MONGODB_FULL_MODULE_FAIL module='+module)
PY
  done < "$directory/full-plan.tsv"
  [[ "$step" -eq "$expected_steps" ]] || {
    echo 'MONGODB_FULL_FAIL reason=incomplete-runtime-plan' >&2; return 1;
  }
  [[ ! -e "$base/clusters/mongodb-gate" ]] || {
    echo 'MONGODB_FULL_FAIL reason=retained-cluster-directory' >&2; return 1;
  }
  if [[ "$focus" == artifacts ]]; then
    echo MONGODB_FULL_ARTIFACTS_PASS
  else
    echo MONGODB_FULL_PASS
  fi
}

mongodb_launch() {
  local module="$1" marker="$2" ready launcher_pid out_fd in_fd rc=0
  local recovery_client="${MONGODB_SHARDING_RECOVERY_CLIENT:-}"
  if [[ -n "$recovery_client" ]] && { [[ "$module" != sharding-clean ]] ||
      [[ ! "$recovery_client" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12},mongodb_gate_[0-9a-f]{16}$ ]]; }; then
    echo 'MONGODB_PREFLIGHT_FAIL class=test-gate reason=invalid-client-recovery-evidence' >&2
    return 1
  fi
  bash "$ROOT/tests/helpers/mongodb-static.sh" || return
  # Acquire the authoritative shared leases BEFORE synchronizing the control
  # source. The durable child inherits them until its cleanup has completed.
  coproc MONGODB_LEASE {
    ssh_mongodb 'set -eu
exec 9>/var/lock/kubeauto-ceph-priority.lock
flock -n 9
exec 7>/var/lock/kubeauto-ceph-compute.lock
flock -n 7
exec 8>/var/lock/kubeauto-redis-core-pool.lock
flock -n 8
for file in /tmp/kubeauto-*.pid; do
  test -f "$file" || continue
  pid=$(cat "$file")
  case "$pid" in ""|*[!0-9]*) exit 1;; esac
  if kill -0 "$pid" 2>/dev/null; then exit 1; fi
done
printf "MONGODB_LEASE_READY\n"
IFS= read -r mode
IFS= read -r recovery_client
if test -n "$recovery_client"; then
  test "$mode" = --sharding-clean
  [[ "$recovery_client" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12},mongodb_gate_[0-9a-f]{16}$ ]]
fi
case "$mode" in --full|--full-artifacts|--upgrade|--upgrade-existing|--upgrade-recover|--core|--core-resume|--install|--install-retry|--install-business|--reconcile|--registry-qualification|--registry-recover|--cleanup-qualification|--clean|--artifacts|--platform|--preflight|--storage|--data|--transactions|--security|--network-client|--network|--quorum|--failover|--idempotence|--maintenance|--rebuild|--rotation|--observability|--observability-scrape|--observability-lag|--observability-capacity|--observability-backup|--observability-recover|--performance|--performance-node|--performance-client|--performance-recover|--ownership|--ownership-resources|--ownership-resources-recover|--ownership-pvc|--ownership-pvc-recover|--replica-scale|--replica-scale-recover|--storage-reclaim|--logging|--capacity-expand|--capacity-recover|--capacity-pressure|--capacity-pressure-recover|--capacity-lag|--capacity-lag-recover|--capacity|--backup-clients-recover|--cleanup|--cleanup-success|--cleanup-failure|--cleanup-recover|--logical-backup|--physical-backup|--disaster-recovery|--stability|--stability-faults|--stability-recover|--rollback|--sharding-restore|--backup-refusal|--backup-refusal-tail|--backup-refusal-oplog|--retention|--retention-recover|--encryption|--pitr|--logical-restore|--physical-restore|--logical-restore-inplace|--physical-restore-inplace|--backup-clean|--restore-clean|--sharding-install|--sharding-scale|--sharding-data|--sharding-failure|--sharding-recover|--sharding-clean) ;; *) exit 2;; esac
if test -f /tmp/kubeauto-mongodb-live.log; then
  install -d -m 0700 /var/tmp/kubeauto-mongodb-history
  archive=$(mktemp -d /var/tmp/kubeauto-mongodb-history/run.XXXXXX)
  cp /tmp/kubeauto-mongodb-live.log "$archive/"
  for suffix in pid exit finalized; do
    if test -f "/tmp/kubeauto-mongodb-gate.$suffix"; then cp "/tmp/kubeauto-mongodb-gate.$suffix" "$archive/"; fi
  done
fi
nohup env MONGODB_PARENT_LEASE_HELD=yes MONGODB_SHARDING_RECOVERY_CLIENT="$recovery_client" bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-mongodb-gate MONGODB_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/mongodb-install-gate.sh "$mode" >/tmp/kubeauto-mongodb-live.log 2>&1 </dev/null &
printf "MONGODB_DURABLE_LAUNCHED\n"'
  }
  launcher_pid=$MONGODB_LEASE_PID
  out_fd=${MONGODB_LEASE[0]}
  in_fd=${MONGODB_LEASE[1]}
  if ! read -r -t 30 ready <&"$out_fd" || [[ "$ready" != MONGODB_LEASE_READY ]]; then
    echo 'MONGODB_PREFLIGHT_FAIL class=environment reason=pool-lease-or-active-control-job' >&2
    wait "$launcher_pid" || true
    return 1
  fi
  if ! KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 bash "$ROOT/tests/helpers/sync-kubeauto.sh" root@192.168.47.130; then
    printf 'CANCEL\n\n' >&"$in_fd"
    wait "$launcher_pid" || true
    return 1
  fi
  printf '%s\n' "--$module" "$recovery_client" >&"$in_fd"
  read -r -t 30 ready <&"$out_fd" || return 1
  [[ "$ready" == MONGODB_DURABLE_LAUNCHED ]] || return 1
  wait "$launcher_pid" || return
  local directory="$ROOT/logs/mongodb-cases/$module-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$directory"
  monitor_remote_job mongodb ssh_mongodb '' /tmp/kubeauto-mongodb-gate \
    /tmp/kubeauto-mongodb-live.log "$marker" || rc=$?
  remote_job_summary ssh_mongodb '' /tmp/kubeauto-mongodb-gate /tmp/kubeauto-mongodb-live.log > "$directory/durable-status.txt"
  ssh_mongodb 'cat /tmp/kubeauto-mongodb-live.log' > "$directory/first-run.log"
  if rg -q '^\[FAIL\]|^MONGODB_[A-Z_]*(FAIL|FIRST_FAILURE)( |$)' "$directory/first-run.log"; then rc=1; fi
  echo "MONGODB_DIAGNOSTICS_SAVED directory=$directory"
  return "$rc"
}

mongodb_dispatch() {
  local mode="$1"
  case "$mode" in
    --mongodb-retention-stop|--mongodb-backup-refusal-stop|--mongodb-rollback-stop|--mongodb-stability-stop|--mongodb-stability-faults-stop)
      # Execute only the validated process-control path over stdin, without syncing
      # source or acquiring the active scenario's cluster lease.
      local stop_mode="${mode#--mongodb-}"
      ssh_mongodb "/usr/local/kubeauto/.venv/bin/python - $stop_mode" < "$ROOT/tests/helpers/mongodb-backups.py"
      ;;
    --mongodb-cleanup-qualification|--mongodb-reconcile|--mongodb-core-resume|--mongodb-registry-qualification|--mongodb-registry-recover|--mongodb-platform|--mongodb-preflight|--mongodb-storage|--mongodb-data|--mongodb-transactions|--mongodb-security|--mongodb-network-client|--mongodb-network|--mongodb-quorum|--mongodb-failover|--mongodb-idempotence|--mongodb-maintenance|--mongodb-rebuild|--mongodb-rotation|--mongodb-observability|--mongodb-observability-scrape|--mongodb-observability-lag|--mongodb-observability-capacity|--mongodb-observability-backup|--mongodb-observability-recover|--mongodb-performance|--mongodb-performance-node|--mongodb-performance-client|--mongodb-performance-recover|--mongodb-ownership|--mongodb-ownership-resources|--mongodb-ownership-resources-recover|--mongodb-ownership-pvc|--mongodb-ownership-pvc-recover|--mongodb-replica-scale|--mongodb-replica-scale-recover|--mongodb-storage-reclaim|--mongodb-logging|--mongodb-capacity-expand|--mongodb-capacity-recover|--mongodb-capacity-pressure|--mongodb-capacity-pressure-recover|--mongodb-capacity-lag|--mongodb-capacity-lag-recover|--mongodb-capacity|--mongodb-upgrade-recover|--mongodb-backup-clients-recover|--mongodb-cleanup|--mongodb-cleanup-success|--mongodb-cleanup-failure|--mongodb-cleanup-recover|--mongodb-logical-backup|--mongodb-physical-backup|--mongodb-disaster-recovery|--mongodb-stability|--mongodb-stability-faults|--mongodb-stability-recover|--mongodb-rollback|--mongodb-sharding-restore|--mongodb-backup-refusal|--mongodb-backup-refusal-tail|--mongodb-backup-refusal-oplog|--mongodb-retention|--mongodb-retention-recover|--mongodb-encryption|--mongodb-pitr|--mongodb-logical-restore|--mongodb-physical-restore|--mongodb-logical-restore-inplace|--mongodb-physical-restore-inplace|--mongodb-backup-clean|--mongodb-restore-clean|--mongodb-sharding-install|--mongodb-sharding-scale|--mongodb-sharding-data|--mongodb-sharding-failure|--mongodb-sharding-recover|--mongodb-sharding-clean)
      local module="${mode#--mongodb-}" marker
      marker="MONGODB_${module^^}_PASS"
      marker="${marker//-/_}"
      [[ "$module" != backup-refusal-oplog ]] || marker=MONGODB_BACKUP_REFUSAL_OPLOG_ONLY_PASS
      mongodb_launch "$module" "$marker"
      ;;
    --mongodb-status)
      remote_job_summary ssh_mongodb '' /tmp/kubeauto-mongodb-gate /tmp/kubeauto-mongodb-live.log
      ;;
    --mongodb-follow)
      ssh_mongodb 'tail -n 100 /tmp/kubeauto-mongodb-live.log'
      ;;
    --mongodb-install-diagnostics)
      # Read-only and deliberately no source synchronization while a gate runs.
      ssh_mongodb '/usr/local/kubeauto/.venv/bin/python -' <<'PY'
import json,subprocess
command=['/usr/local/kubeauto/kube-bin/kubectl','--kubeconfig=/usr/local/kubeauto/clusters/mongodb-gate/kubectl.kubeconfig']
result=subprocess.run(command+['get','pods','--all-namespaces','-o','json'],capture_output=True,text=True,timeout=30)
if result.returncode:raise SystemExit('MONGODB_DIAGNOSTICS_UNAVAILABLE API=unreachable')
for pod in json.loads(result.stdout)['items']:
 meta,status=pod['metadata'],pod.get('status',{})
 containers=[{'name':c['name'],'ready':c.get('ready'), 'restartCount':c.get('restartCount'),
              'reason':next(iter(c.get('state',{}).values()),{}).get('reason')} for c in
             status.get('initContainerStatuses',[])+status.get('containerStatuses',[])]
 print(json.dumps({'namespace':meta['namespace'],'name':meta['name'],'node':pod['spec'].get('nodeName'),
                   'phase':status.get('phase'),'conditions':status.get('conditions',[]),'containers':containers}))
result=subprocess.run(command+['get','events','--all-namespaces','-o','json'],capture_output=True,text=True,timeout=30)
if not result.returncode:
 for event in json.loads(result.stdout)['items']:
  if event.get('type')=='Warning':
   print(json.dumps({'namespace':event['metadata']['namespace'],'object':event['involvedObject']['name'],
                     'reason':event.get('reason'),'message':event.get('message')}))
PY
      ;;
    --mongodb-runtime-diagnostics)
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool ceph --runtime-diagnostics
      ;;
    --mongodb-artifacts)
      mongodb_launch artifacts MONGODB_STORAGE_ARTIFACTS_PASS
      ;;
    --mongodb-core)
      mongodb_launch core MONGODB_CORE_SCOPED_CLEAN_PASS
      ;;
    --mongodb-install)
      mongodb_launch install MONGODB_INSTALL_PASS
      ;;
    --mongodb-upgrade)
      mongodb_launch upgrade MONGODB_UPGRADE_PASS
      ;;
    --mongodb-upgrade-existing)
      mongodb_launch upgrade-existing MONGODB_UPGRADE_PASS
      ;;
    --mongodb-only)
      mongodb_launch full MONGODB_FULL_PASS
      ;;
    --mongodb-full-artifacts)
      mongodb_launch full-artifacts MONGODB_FULL_ARTIFACTS_PASS
      ;;
    --mongodb-install-retry)
      mongodb_launch install-retry MONGODB_INSTALL_PASS
      ;;
    --mongodb-install-business)
      mongodb_launch install-business MONGODB_INSTALL_PASS
      ;;
    --mongodb-clean)
      mongodb_launch clean MONGODB_CLEANUP_PASS
      ;;
    --mongodb-pool-prepare)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool ceph --reset-stale-unit || return
      mongodb_dispatch --mongodb-ceph-pool-probe
      ;;
    --mongodb-ceph-pool-probe)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool ceph --control || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool ceph || return
      bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
        192.168.122.135 192.168.122.40 192.168.122.72 \
        192.168.122.212 192.168.122.165 192.168.122.238
      ;;
    --mongodb-core-probe)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool core --control || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool core --inventory || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --pool core || return
      bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
        192.168.47.131 192.168.47.132 192.168.47.133 \
        192.168.47.134 192.168.47.135 192.168.47.136
      ;;
    --mongodb-documentation)
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/validate_mongodb_matrix.py" \
        "$ROOT/tests/mongodb-test-matrix.yaml" --documentation
      ;;
    --mongodb-static|--mongodb-disabled|--mongodb-validation)
      bash "$ROOT/tests/helpers/mongodb-static.sh"
      ;;
    --mongodb-cpu-probe)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py"
      ;;
    --mongodb-lab-probe)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --control || return
      bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only \
        192.168.122.217 192.168.122.243 192.168.122.246 \
        192.168.122.193 192.168.122.210 192.168.122.216
      ;;
    --mongodb-inventory-probe)
      "$ROOT/.venv/bin/python" -m unittest tests.unit.test_mongodb_platform -v || return
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/mongodb-platform.py" --inventory
      ;;
    *)
      echo "MONGODB_GATE_FAIL class=test-gate reason=module-not-yet-implemented mode=$mode" >&2
      return 2
      ;;
  esac
}
