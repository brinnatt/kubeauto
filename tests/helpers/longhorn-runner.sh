#!/usr/bin/env bash
# Sourced only by the authoritative enterprise runner.
longhorn_local_gate() {
  local mode="$1" marker="$2" rc=0 directory
  directory="$ROOT/logs/longhorn-cases/$mode-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$directory"
  if [[ "$mode" == static ]]; then
    if [[ "${LONGHORN_STATIC_REUSE_ALLOWED:-no}" == yes ]] &&
       "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-static-reuse.py" reuse --root "$ROOT" 2>/dev/null; then
      return 0
    fi
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-static-reuse.py" begin --root "$ROOT" --directory "$directory" || return
  fi
  case "$mode" in
    ownership-evidence)
      [[ -n "${LONGHORN_OWNERSHIP_IMPORT_REQUEST:-}" && -n "${LONGHORN_OWNERSHIP_IMPORT_SHA256:-}" &&
         -n "${LONGHORN_OWNERSHIP_ORIGINAL_ARCHIVE:-}" && -n "${LONGHORN_OWNERSHIP_CONTINUATION_ARCHIVE:-}" ]] || return 2
      bash "$ROOT/tests/helpers/run-durable-gate.sh" "$directory/ownership" LONGHORN_OWNERSHIP_EXIT \
        env PYTHONDONTWRITEBYTECODE=1 "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-ownership-evidence.py" \
        --root "$ROOT" --request "$LONGHORN_OWNERSHIP_IMPORT_REQUEST" --sha256 "$LONGHORN_OWNERSHIP_IMPORT_SHA256" \
        --original-archive "$LONGHORN_OWNERSHIP_ORIGINAL_ARCHIVE" \
        --continuation-archive "$LONGHORN_OWNERSHIP_CONTINUATION_ARCHIVE" 2>&1 | tee "$directory/gate.log" || rc=$?
      ;;
    # Runtime continuation belongs to the parent launch, not static fixtures.
    static) env -u LONGHORN_V2_CONTINUATION_REQUEST bash "$ROOT/tests/helpers/longhorn-static.sh" 2>&1 | tee "$directory/gate.log" || rc=$? ;;
    disabled) "$ROOT/.venv/bin/python" -m unittest tests.unit.test_longhorn_delivery.InputContracts.test_disabled_cli_never_uses_cluster_or_helm -v 2>&1 | tee "$directory/gate.log" || rc=$? ;;
    artifacts) "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-artifacts.py" 2>&1 | tee "$directory/gate.log" || rc=$? ;;
    v2-pool-inputs) "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-pool.py" 2>&1 | tee "$directory/gate.log" || rc=$? ;;
    documentation) "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-documentation.py" 2>&1 | tee "$directory/gate.log" || rc=$? ;;
    *) return 2 ;;
  esac
  if [[ "$rc" == 0 && "$mode" == disabled ]]; then
    printf '%s\n' LONGHORN_DISABLED_SCOPED_CLEAN_PASS "$marker" | tee -a "$directory/gate.log"
  fi
  "$ROOT/.venv/bin/python" - "$directory" "$rc" "$marker" "$mode" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
path=Path(sys.argv[1]);rc=int(sys.argv[2]);marker=sys.argv[3]
raw=(path/'gate.log').read_bytes();lines=raw.decode().splitlines()
failure_markers=sum(bool(re.match(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)',s)) for s in lines)
found=any(s==marker or s.startswith(marker+' ') for s in lines)
if sys.argv[4] == 'ownership-evidence':
    required=('LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS', marker, 'LONGHORN_OWNERSHIP_EXIT rc=0')
    found=all(lines.count(value)==1 for value in required)
    found=found and all((path/('ownership'+suffix)).read_text().strip()=='0'
                        for suffix in ('.exit','.finalized'))
result={'rc':rc,'failure_markers':failure_markers,'marker':marker,'marker_present':found,
        'log_sha256':hashlib.sha256(raw).hexdigest()}
(path/'status.json').write_text(json.dumps(result,sort_keys=True)+'\n')
print('LONGHORN_LOCAL_RESULT '+json.dumps(result,sort_keys=True))
if rc or failure_markers or not found: sys.exit(1)
PY
  local result_rc=$?
  [[ "$result_rc" == 0 ]] || return "$result_rc"
  if [[ "$mode" == static ]]; then
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-static-reuse.py" finish --root "$ROOT" --directory "$directory" || return
  fi
}

ssh_longhorn() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=2 root@192.168.47.130 "$@"
}

longhorn_sync_retirement_evidence() {
  # Normal source sync deliberately excludes logs. Carry only the immutable
  # sources/contracts referenced by the current finite retirement repair.
  local work rc=0
  [[ -f "$ROOT/tests/evidence/longhorn-manager-pin-gate-impact-20261024.json" ]] || return 0
  work="$(mktemp -d "$ROOT/logs/longhorn-cases/retirement-transfer.XXXXXX")" || return
  "$ROOT/.venv/bin/python" - "$ROOT" "$work/files" <<'PY' || rc=$?
import importlib.util,json,re,sys
from pathlib import Path
root=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('revision_transfer',root/'tests/helpers/longhorn-im-revision-3-adopt.py')
revision=importlib.util.module_from_spec(spec);spec.loader.exec_module(revision);revision.BASE=root
repair=json.loads((root/revision.RETIREMENT_REPAIR).read_text())
if (repair.get('scope')!='v2-upgrade-retirement-gc-repair' or
        repair.get('product_behavior_changed') is not False or
        set(repair.get('changes',{}))!=set(revision.RETIREMENT_GATES) or
        set(repair.get('contract_receipts',{}))!={'log','exit','finalized'}):
    raise ValueError('retirement evidence transfer exceeds its finite repair scope')
refs=list(repair['contract_receipts'].values())
for name,edge in repair['changes'].items():
    if revision.digest(root/name)!=edge['sha256'] or not edge['origins']:
        raise ValueError('retirement evidence transfer current gate differs')
    for sha,ref in edge['origins'].items():
        if ref['sha256']!=sha:raise ValueError('retirement original identity differs')
        refs.append(ref)
files=set()
for ref in refs:
    path=revision.referenced_path(ref)
    if (not path.resolve().is_relative_to(root.resolve()) or
            not re.fullmatch(r'logs/longhorn-cases/[A-Za-z0-9_./-]+',ref['path'])):
        raise ValueError('retirement evidence transfer is outside its raw evidence scope')
    files.add(ref['path'])
Path(sys.argv[2]).write_text(''.join(name+'\n' for name in sorted(files)))
PY
  if [[ "$rc" == 0 ]]; then
    rsync -rt --ignore-existing --chmod=D700,F600 --files-from="$work/files" \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' "$ROOT/" root@192.168.47.130:/usr/local/kubeauto/ || rc=$?
  fi
  rm -rf -- "$work"
  return "$rc"
}

longhorn_launch() {
  local module="$1" marker="$2" ready launcher_pid out_fd in_fd rc=0 directory
  local cache_payload= cache_sha=
  local focused_work= focused_remote=
  if [[ "$module" == v2-integrity-qualification && -n "${LONGHORN_FULL_RESUME_DIRECTORY:-}" ]]; then
    focused_work="$(cd "$LONGHORN_FULL_RESUME_DIRECTORY" && pwd)" || return
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" focused-material \
      --directory "$focused_work" || return
    focused_remote="/usr/local/kubeauto/${focused_work#"$ROOT"/}"
  fi
  if [[ -n "${LONGHORN_MONITORING_CACHE_RECONCILE_PROOF:-}" || -n "${LONGHORN_MONITORING_CACHE_RECONCILE_SHA256:-}" ]]; then
    [[ ( "$module" == v2-observability-monitoring || "$module" == v1-operations-remaining-v2-monitoring ) && -f "${LONGHORN_MONITORING_CACHE_RECONCILE_PROOF:-}" &&
       ! -L "${LONGHORN_MONITORING_CACHE_RECONCILE_PROOF:-}" &&
       "${LONGHORN_MONITORING_CACHE_RECONCILE_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || return 2
    cache_sha="$LONGHORN_MONITORING_CACHE_RECONCILE_SHA256"
    cache_payload="$("$ROOT/.venv/bin/python" - "$LONGHORN_MONITORING_CACHE_RECONCILE_PROOF" "$cache_sha" <<'PYPROOF'
import base64,hashlib,json,pathlib,sys
try:
    with pathlib.Path(sys.argv[1]).open('rb') as stream:
        raw=stream.read(32769)
    if len(raw)>32768 or hashlib.sha256(raw).hexdigest()!=sys.argv[2]:
        raise ValueError('proof size or SHA256 differs')
    if not isinstance(json.loads(raw),dict):
        raise ValueError('proof must be a JSON object')
except (OSError,ValueError,UnicodeError):
    raise SystemExit('LONGHORN_CACHE_RECONCILE_INPUT_REJECTED') from None
print(base64.b64encode(raw).decode())
PYPROOF
)" || return
  fi
  # The proof file is a local launch input. Unit fixtures must not inherit
  # its SHA as if it were the remote payload; stdin uses captured values.
  unset LONGHORN_MONITORING_CACHE_RECONCILE_PROOF LONGHORN_MONITORING_CACHE_RECONCILE_SHA256
  local continuation_request="${LONGHORN_V2_CONTINUATION_REQUEST:-}" continuation_files= continuation_copy=
  if [[ -n "${LONGHORN_V2_CONTINUATION_REQUEST+x}" ]]; then
    # Cleanup uses the original preparation journal, never a new request/create.
    [[ "$module" == v2-upgrade-lifecycle && "$continuation_request" =~ ^logs/longhorn-cases/v2-continuation\.[A-Za-z0-9_]+/request\.json$ ]] || return 2
    continuation_files="$ROOT/${continuation_request%/request.json}/transfer.list"
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-runtime-continuation.py" --root "$ROOT" \
      transfer --request "$continuation_request" --files "$continuation_files" || return
  fi
  if [[ "$module" == node-lifecycle ]]; then
    [[ "${LONGHORN_V2_RELEASE_PROOF:-}" =~ ^/var/tmp/kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+/(recovery\.[A-Za-z0-9]+/)?release-proofs/longhorn-v2-source-released-192\.168\.47\.148\.json$ ]] || {
      echo LONGHORN_NODE_REQUIRES_RELEASED_V2_PROOF >&2; return 1;
    }
  fi
  if [[ "$module" == v2-disk-lifecycle || "$module" == node-lifecycle ]]; then
    [[ "${LONGHORN_V2_TARGET_RELEASE_PROOF:-}" =~ ^/var/tmp/kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+/(recovery\.[A-Za-z0-9]+/)?release-proofs/longhorn-v2-target-released-192\.168\.47\.149\.json$ ]] || {
      echo LONGHORN_V2_DISK_REQUIRES_TARGET_RELEASE >&2; return 1;
    }
  fi
  if [[ "$module" == v2-cleanup-recovery || "$module" == v2-cleanup-target-recovery ]]; then
    [[ "${LONGHORN_V2_RETIREMENT_JOURNAL:-}" =~ ^/var/tmp/kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+/retirement\.json$ ]] || {
      echo LONGHORN_V2_CLEANUP_REQUIRES_ORIGINAL_JOURNAL >&2; return 1;
    }
  fi
  # Full already completed these exact gates locally; verify their immutable
  # receipts again before taking a lease, rather than executing them twice.
  if [[ "$module" == full ]]; then
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" verify-preflight \
      --directory "${LONGHORN_FULL_LOCAL_DIRECTORY:?}" || return
  else
    local LONGHORN_STATIC_REUSE_ALLOWED=yes
    # Reuse only complete identical static inputs; artifacts still read live registries.
    # Level 2 must finish before source sync, lease acquisition or host changes.
    longhorn_local_gate static LONGHORN_STATIC_PASS || return
    if [[ "$module" != registry-recover && "$module" != core-host-recover && "$module" != core-host-preflight ]]; then
      longhorn_local_gate artifacts LONGHORN_ARTIFACTS_PASS || return
    fi
  fi
  case "$module" in
    v2-source-target-im-revision-3-adopt-qualification)
      for engine in v2 v2-dr-target; do
        "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-im-revision-3-adopt.py" preflight --engine "$engine" --base "$ROOT" || return
      done
      ;;
    v1-im-revision-3-adopt-qualification|v2-im-revision-3-adopt-qualification|v2-dr-target-im-revision-3-adopt-qualification)
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-im-revision-3-adopt.py" preflight --engine "${module%-im-revision-3-adopt-qualification}" --base "$ROOT" || return
      ;;
  esac
  case "$module" in full|v2-linked-acceptance|v1-backup-v2-data-protection|v1-operations-v2-disk-tail|v1-operations-v2-worker-tail|v1-ownership-tail-v2-failure-tail|cleanup|cleanup-qualification|v1-ownership-operations-v2-failure-platform|v1-ownership-v2-failure-remaining|v2-failure-remaining|v1-operations-v2-platform-install|v1-ownership-v2-linked-acceptance|v2-incomplete-clone-qualification|v2-incomplete-clone-cleanup|v2-disk-lifecycle|v2-disk-lifecycle-cleanup|v1-post-revision-backups-v2-failure-acceptance|v2-upgrade-lifecycle|v2-upgrade-cleanup|v1-operations-remaining-v2-monitoring|v1-system-restore-v2-runtime-resources|v1-recovery-tail-v2-install-tail|v1-dr-tail-v2-install-tail|v1-upgrade-tail-v2-install-tail|v1-monitoring-v2-resource-tail|v1-upgrade-tail-v2-maintenance-tail|v1-stability-v2-observability|v2-runtime-resources|v2-runtime-resources-cleanup|v2-source-target-im-revision-3-adopt-qualification|v2-source-target-im-revision-2-adopt-qualification|v1-im-revision-2-adopt-qualification|v1-im-revision-3-adopt-qualification|v2-im-revision-2-adopt-qualification|v2-im-revision-3-adopt-qualification|v2-dr-target-im-revision-2-adopt-qualification|v2-dr-target-im-revision-3-adopt-qualification|v2-maintenance-drain-qualification|v2-maintenance-drain-cleanup|v2-coexist-qualification|v2-coexist-cleanup|v2-observability|v2-observability-monitoring|v2-observability-business|v2-middleware|v2-middleware-cleanup|v2-cleanup|v2-cleanup-target|v2-cleanup-recovery|v2-cleanup-target-recovery|v1-backups-v2-linked-failure-acceptance|v1-backups-v2-linked-acceptance|v1-platform-propagation-v2-im-adopt-qualification|platform-propagation-qualification|v2-failure|v2-failure-cleanup|v2-failure-replica-im|v2-failure-manager|v2-failure-attached-spdk|v2-failure-remote-spdk|v2-failure-replica-network|v2-failure-worker-reboot|v2-failure-single-disk|v2-failure-rebuild-qos|v2-failure-all-replicas|v1-remaining-v2-linked-acceptance|v1-backups-remaining|v2-linked-lifecycle-qualification|v2-linked-lifecycle-cleanup|v1-platform-v2-environment-recovery|v2-source-target-environment-recovery|v2-post-reboot-environment-recovery|v2-dr-target-post-reboot-environment-recovery|v1-backups|v2-source-target-reboot-recovery|v2-source-target-im-adopt-qualification|v2-resources-reboot-recovery|v2-dr-target-resources-reboot-recovery|v2-dr-target-im-adopt-qualification|ownership|v1-v2-im-adopt-qualification|v1-im-adopt-qualification|v2-im-adopt-qualification|v1-backups-v2-linked-qualification|v1-v2-control-qualification|v2-dr-target-registry-qualification|v2-dr-target-core|v2-dr-target-core-recovery|v2-dr-target-install-qualification|v2-dr-target-install-recovery|v2-dr-store-qualification|v2-dr-standby-qualification|v2-data-protection|v2-dr-activation-qualification|v2-dr-failback-qualification|v2-detached-backup-recovery|v2-detached-backup-qualification|v2-systemrestore-recovery|v2-systemrestore-qualification|v2-systembackup-cleanup|v2-systembackup-recovery|v2-systembackup-qualification|v2-broken-restore-cleanup|v2-broken-restore-qualification|v2-linked-clone-cleanup|v2-linked-clone-qualification|v2-interrupt-manager-qualification|v2-manager-loss-cleanup|v2-manager-loss-qualification|v2-interrupt-cleanup|v2-interrupt-qualification|v2-replica-im-loss-cleanup|v2-replica-im-loss-qualification|v2-full-copy-lifecycle-qualification|v2-full-copy-clone-qualification|v2-integrity-qualification|v2-recurring-selector-qualification|v2-recurring-snapshot-qualification|v2-recurring-backup-qualification|v2-recurring-rotation-qualification|v2-backup-refusal-qualification|v2-backup-store-qualification|v2-backup-qualification|v2-snapshot-qualification|v2-volumes|v2-trim-qualification|v2-encryption-qualification|v2-reclaim-qualification|v2-expansion-qualification|v2-volume-modes-qualification|v2-host-recovery|v2-host-qualification|v2-aio-install-qualification|v2-resources-recovery|v2-resources-qualification|v2-core|v2-registry-qualification|pool-probe|registry-qualification|registry-recover|core-host-preflight|core-host-recover) ;;
    middleware|middleware-business|middleware-cleanup|middleware-reentry-cleanup|last-replica|last-replica-cleanup|disk-failure|disk-failure-cleanup|network-failure|network-failure-cleanup|system-restore|system-restore-cleanup|system-restore-prepared-recovery|disaster-recovery|disaster-recovery-cleanup|node-failure|node-failure-cleanup|observability|observability-business|observability-support|observability-image-cache|observability-cleanup|v1-upgrade-lifecycle|v1-upgrade-cleanup|stability|stability-cleanup|node-lifecycle|node-lifecycle-cleanup|maintenance|maintenance-cleanup|capacity|capacity-cleanup|integrity|integrity-cleanup|disk-lifecycle|disk-lifecycle-cleanup|platform|core|disk-qualification|ownership-request-refusal-qualification|ownership-controller-refusal-qualification|ownership-class-refusal-qualification|ownership-interruption-qualification|ownership-preinstall-refusal-qualification|ownership-csi-refusal-qualification|ownership-csi-probe|install|business-clean|idempotence|rwo|rwx|performance|replica-scale|replica-failure|engine-failure|encryption|encryption-block|encryption-backup|network-policy|security|clone|expansion|snapshot|reclaim|trim|backup|backup-refusal|core-smoke|cache-qualification|preflight-qualification|backup-store-qualification|backup-accounts-qualification|recurring-declarations-qualification|recurring-snapshot-qualification|recurring-selector-qualification|recurring-backup-qualification|recurring-rotation-qualification|recurring|uninstall-refusal-qualification|uninstall-backup-qualification|uninstall|recover) "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-lab.py" verify-transport || return ;;
    *) return 2;; esac
  if [[ "$module" == v1-monitoring-v2-resource-tail || "$module" == v1-upgrade-tail-v2-maintenance-tail || "$module" == v1-upgrade-tail-v2-install-tail || "$module" == v1-dr-tail-v2-install-tail || "$module" == v1-system-restore-v2-runtime-resources || "$module" == full || "$module" == v1-backup-v2-data-protection || "$module" == v1-operations-remaining-v2-monitoring || "$module" == v1-operations-v2-disk-tail || "$module" == v1-operations-v2-worker-tail || "$module" == v1-ownership-tail-v2-failure-tail || "$module" == v1-ownership-operations-v2-failure-platform || "$module" == v1-ownership-v2-failure-remaining || "$module" == v1-operations-v2-platform-install || "$module" == v1-ownership-v2-linked-acceptance || "$module" == v1-im-revision-2-adopt-qualification || "$module" == v1-im-revision-3-adopt-qualification || "$module" == v1-backups-v2-linked-failure-acceptance || "$module" == v1-backups-v2-linked-acceptance || "$module" == v1-platform-propagation-v2-im-adopt-qualification || "$module" == platform-propagation-qualification || "$module" == v1-remaining-v2-linked-acceptance || "$module" == v1-backups-remaining || "$module" == v1-platform-v2-environment-recovery || "$module" == v1-backups || "$module" == v1-v2-control-qualification || "$module" == v1-backups-v2-linked-qualification || "$module" == v1-v2-im-adopt-qualification || "$module" == v1-im-adopt-qualification || "$module" == ownership ]]; then
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-lab.py" verify-transport || return
  fi
  echo "LONGHORN_LADDER completed=2 evidence=static-contracts-and-current-published-images next=3 focused=$module"
  coproc LONGHORN_LEASE {
    ssh_longhorn 'set -eu
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
printf "LONGHORN_LEASE_READY\n"
IFS= read -r mode
case "$mode" in full|v2-linked-acceptance|v1-backup-v2-data-protection|v1-operations-v2-disk-tail|v1-operations-v2-worker-tail|v1-ownership-tail-v2-failure-tail|cleanup|cleanup-qualification|v1-ownership-operations-v2-failure-platform|v1-ownership-v2-failure-remaining|v2-failure-remaining|v1-operations-v2-platform-install|v1-ownership-v2-linked-acceptance|v2-incomplete-clone-qualification|v2-incomplete-clone-cleanup|v2-disk-lifecycle|v2-disk-lifecycle-cleanup|v1-post-revision-backups-v2-failure-acceptance|v2-upgrade-lifecycle|v2-upgrade-cleanup|v1-operations-remaining-v2-monitoring|v1-system-restore-v2-runtime-resources|v1-recovery-tail-v2-install-tail|v1-dr-tail-v2-install-tail|v1-upgrade-tail-v2-install-tail|v1-monitoring-v2-resource-tail|v1-upgrade-tail-v2-maintenance-tail|v1-stability-v2-observability|v2-runtime-resources|v2-runtime-resources-cleanup|v2-source-target-im-revision-3-adopt-qualification|v2-source-target-im-revision-2-adopt-qualification|middleware|middleware-business|middleware-cleanup|middleware-reentry-cleanup|last-replica|last-replica-cleanup|disk-failure|disk-failure-cleanup|network-failure|network-failure-cleanup|system-restore|system-restore-cleanup|system-restore-prepared-recovery|disaster-recovery|disaster-recovery-cleanup|node-failure|node-failure-cleanup|observability|observability-business|observability-support|observability-image-cache|observability-cleanup|v1-im-revision-2-adopt-qualification|v1-im-revision-3-adopt-qualification|v2-im-revision-2-adopt-qualification|v2-im-revision-3-adopt-qualification|v2-dr-target-im-revision-2-adopt-qualification|v2-dr-target-im-revision-3-adopt-qualification|v2-maintenance-drain-qualification|v2-maintenance-drain-cleanup|v1-upgrade-lifecycle|v1-upgrade-cleanup|stability-cleanup|v2-coexist-qualification|v2-coexist-cleanup|v2-observability|v2-observability-monitoring|v2-observability-business|v2-middleware|v2-middleware-cleanup|v2-cleanup|v2-cleanup-target|v2-cleanup-recovery|v2-cleanup-target-recovery|stability|node-lifecycle|node-lifecycle-cleanup|v1-backups-v2-linked-failure-acceptance|v1-backups-v2-linked-acceptance|v1-platform-propagation-v2-im-adopt-qualification|platform-propagation-qualification|v2-failure|v2-failure-cleanup|v2-failure-replica-im|v2-failure-manager|v2-failure-attached-spdk|v2-failure-remote-spdk|v2-failure-replica-network|v2-failure-worker-reboot|v2-failure-single-disk|v2-failure-rebuild-qos|v2-failure-all-replicas|v1-remaining-v2-linked-acceptance|v1-backups-remaining|v2-linked-lifecycle-qualification|v2-linked-lifecycle-cleanup|v1-platform-v2-environment-recovery|v2-source-target-environment-recovery|v2-post-reboot-environment-recovery|v2-dr-target-post-reboot-environment-recovery|v1-backups|v2-source-target-reboot-recovery|v2-source-target-im-adopt-qualification|v2-resources-reboot-recovery|v2-dr-target-resources-reboot-recovery|v2-dr-target-im-adopt-qualification|ownership|v1-v2-im-adopt-qualification|v1-im-adopt-qualification|v2-im-adopt-qualification|v1-backups-v2-linked-qualification|v1-v2-control-qualification|v2-dr-target-registry-qualification|v2-dr-target-core|v2-dr-target-core-recovery|v2-dr-target-install-qualification|v2-dr-target-install-recovery|v2-dr-store-qualification|v2-dr-standby-qualification|v2-data-protection|v2-dr-activation-qualification|v2-dr-failback-qualification|v2-detached-backup-recovery|v2-detached-backup-qualification|v2-systemrestore-recovery|v2-systemrestore-qualification|v2-systembackup-cleanup|v2-systembackup-recovery|v2-systembackup-qualification|v2-broken-restore-cleanup|v2-broken-restore-qualification|v2-linked-clone-cleanup|v2-linked-clone-qualification|v2-interrupt-manager-qualification|v2-manager-loss-cleanup|v2-manager-loss-qualification|v2-interrupt-cleanup|v2-interrupt-qualification|v2-replica-im-loss-cleanup|v2-replica-im-loss-qualification|v2-full-copy-lifecycle-qualification|v2-full-copy-clone-qualification|v2-integrity-qualification|v2-recurring-selector-qualification|v2-recurring-snapshot-qualification|v2-recurring-backup-qualification|v2-recurring-rotation-qualification|v2-backup-refusal-qualification|v2-backup-store-qualification|v2-backup-qualification|v2-snapshot-qualification|v2-volumes|v2-trim-qualification|v2-encryption-qualification|v2-reclaim-qualification|v2-expansion-qualification|v2-volume-modes-qualification|v2-host-recovery|v2-host-qualification|v2-aio-install-qualification|v2-resources-recovery|v2-resources-qualification|v2-core|v2-registry-qualification|pool-probe|registry-qualification|registry-recover|core-host-preflight|core-host-recover|maintenance|maintenance-cleanup|capacity|capacity-cleanup|integrity|integrity-cleanup|disk-lifecycle|disk-lifecycle-cleanup|platform|core|disk-qualification|ownership-request-refusal-qualification|ownership-controller-refusal-qualification|ownership-class-refusal-qualification|ownership-interruption-qualification|ownership-preinstall-refusal-qualification|ownership-csi-refusal-qualification|ownership-csi-probe|install|business-clean|idempotence|rwo|rwx|performance|replica-scale|replica-failure|engine-failure|encryption|encryption-block|encryption-backup|network-policy|security|clone|expansion|snapshot|reclaim|trim|backup|backup-refusal|core-smoke|cache-qualification|preflight-qualification|backup-store-qualification|backup-accounts-qualification|recurring-declarations-qualification|recurring-snapshot-qualification|recurring-selector-qualification|recurring-backup-qualification|recurring-rotation-qualification|recurring|uninstall-refusal-qualification|uninstall-backup-qualification|uninstall|recover) ;; *) exit 2;; esac
if test "$mode" = v2-observability-monitoring || test "$mode" = v1-operations-remaining-v2-monitoring; then
  IFS= read -r LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD
  IFS= read -r LONGHORN_MONITORING_CACHE_RECONCILE_SHA256
  export LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD LONGHORN_MONITORING_CACHE_RECONCILE_SHA256
fi
if test "$mode" = v2-integrity-qualification; then
  IFS= read -r focused_work
  if test -n "$focused_work"; then
    /usr/local/kubeauto/.venv/bin/python /usr/local/kubeauto/tests/helpers/longhorn-full-evidence.py focused-material --directory "$focused_work" >&2
    export LONGHORN_FULL_EVIDENCE_DIR="$focused_work/evidence"
  fi
fi
if test "$mode" = full; then
  IFS= read -r full_work
  case "$full_work" in /usr/local/kubeauto/logs/longhorn-full/run.*) ;; *) exit 2;; esac
  case "$full_work" in *[!a-zA-Z0-9/_.-]*|*/../*) exit 2;; esac
  test -d "$full_work/evidence" && test ! -L "$full_work"
  IFS= read -r full_resume
  case "$full_resume" in
    yes) export LONGHORN_FULL_RESUME=yes ;;
    no) test ! -e "$full_work/stages" ;;
    *) exit 2 ;;
  esac
  export LONGHORN_FULL_EVIDENCE_DIR="$full_work/evidence"
fi
if test "$mode" = v2-upgrade-lifecycle || test "$mode" = v2-upgrade-cleanup; then
  IFS= read -r LONGHORN_V2_CONTINUATION_REQUEST
  if test -n "$LONGHORN_V2_CONTINUATION_REQUEST"; then
    case "$LONGHORN_V2_CONTINUATION_REQUEST" in logs/longhorn-cases/v2-continuation.*/request.json) ;; *) exit 2;; esac
    case "$LONGHORN_V2_CONTINUATION_REQUEST" in *[!A-Za-z0-9_./-]*) exit 2;; esac
    export LONGHORN_V2_CONTINUATION_REQUEST
  fi
fi
if test "$mode" = node-lifecycle; then
  IFS= read -r LONGHORN_V2_RELEASE_PROOF
  case "$LONGHORN_V2_RELEASE_PROOF" in /var/tmp/kubeauto-longhorn-v2-retirement.*/release-proofs/longhorn-v2-source-released-192.168.47.148.json) ;; *) exit 2;; esac
  test -f "$LONGHORN_V2_RELEASE_PROOF" && test ! -L "$LONGHORN_V2_RELEASE_PROOF"
  export LONGHORN_V2_RELEASE_PROOF
fi
if test "$mode" = v2-disk-lifecycle || test "$mode" = node-lifecycle; then
  IFS= read -r LONGHORN_V2_TARGET_RELEASE_PROOF
  case "$LONGHORN_V2_TARGET_RELEASE_PROOF" in /var/tmp/kubeauto-longhorn-v2-retirement.*/release-proofs/longhorn-v2-target-released-192.168.47.149.json) ;; *) exit 2;; esac
  test -f "$LONGHORN_V2_TARGET_RELEASE_PROOF" && test ! -L "$LONGHORN_V2_TARGET_RELEASE_PROOF"
  export LONGHORN_V2_TARGET_RELEASE_PROOF
fi
if test "$mode" = v2-cleanup-recovery || test "$mode" = v2-cleanup-target-recovery; then
  IFS= read -r LONGHORN_V2_RETIREMENT_JOURNAL
  case "$LONGHORN_V2_RETIREMENT_JOURNAL" in /var/tmp/kubeauto-longhorn-v2-retirement.*/retirement.json) ;; *) exit 2;; esac
  test -f "$LONGHORN_V2_RETIREMENT_JOURNAL" && test ! -L "$LONGHORN_V2_RETIREMENT_JOURNAL"
  export LONGHORN_V2_RETIREMENT_JOURNAL
fi
if test -f /tmp/kubeauto-longhorn-live.log; then
  install -d -m 0700 /var/tmp/kubeauto-longhorn-history
  archive=$(mktemp -d /var/tmp/kubeauto-longhorn-history/run.XXXXXX)
  cp /tmp/kubeauto-longhorn-live.log "$archive/"
  for suffix in pid exit finalized; do
    if test -f "/tmp/kubeauto-longhorn-gate.$suffix"; then cp "/tmp/kubeauto-longhorn-gate.$suffix" "$archive/"; fi
  done
fi
nohup env LONGHORN_PARENT_LEASE_HELD=yes bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh /tmp/kubeauto-longhorn-gate LONGHORN_GATE_EXIT bash /usr/local/kubeauto/tests/helpers/longhorn-live.sh "$mode" >/tmp/kubeauto-longhorn-live.log 2>&1 </dev/null &
printf "LONGHORN_DURABLE_LAUNCHED\n"'
  }
  launcher_pid=$LONGHORN_LEASE_PID
  out_fd=${LONGHORN_LEASE[0]}
  in_fd=${LONGHORN_LEASE[1]}
  if ! read -r -t 30 ready <&"$out_fd" || [[ "$ready" != LONGHORN_LEASE_READY ]]; then
    echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=shared-pool-leased-or-active-control-job' >&2
    wait "$launcher_pid" || true
    return 1
  fi
  # A resumed full run may continue from a runner-owned target recovery.  Its
  # target core intentionally remains installed, so the initial empty-host
  # admission/bootstrap must not be replayed against that owned state.  The
  # resumed stages still verify root access, core identity and scoped cleanup;
  # fresh full runs and standalone target gates retain the strict bootstrap.
  if [[ ("$module" == full && "${LONGHORN_FULL_RESUME:-no}" != yes) ||
        "$module" == v2-dr-target-registry-qualification ||
        "$module" == v2-dr-target-core ]]; then
    if ! LONGHORN_TARGET_PARENT_LEASE_HELD=yes "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-dr-target.py" bootstrap; then
      printf 'CANCEL\n' >&"$in_fd"
      wait "$launcher_pid" || true
      return 1
    fi
  fi
  if ! KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=1 bash "$ROOT/tests/helpers/sync-kubeauto.sh" root@192.168.47.130; then
    printf 'CANCEL\n' >&"$in_fd"
    wait "$launcher_pid" || true
    return 1
  fi
  # The finite retirement evidence transfer belongs only to upgrade/adoption
  # paths that consume that reviewed repair. Host residue recovery and registry
  # probes have independent inputs and must not be blocked by an unrelated
  # historical SHA map.
  case "$module" in
    core-host-preflight|core-host-recover|registry-recover|registry-qualification|pool-probe|maintenance|maintenance-cleanup|install)
      ;;
    *)
      if ! longhorn_sync_retirement_evidence; then
        printf 'CANCEL\n' >&"$in_fd"
        wait "$launcher_pid" || true
        return 1
      fi
      ;;
  esac
  if [[ -n "$continuation_request" ]]; then
    if ! rsync -rt --chmod=D700,F600 --files-from="$continuation_files" \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' "$ROOT/" root@192.168.47.130:/usr/local/kubeauto/; then
      printf 'CANCEL\n' >&"$in_fd"
      wait "$launcher_pid" || true
      return 1
    fi
  fi
  if [[ "$module" == full ]]; then
    local relative="${LONGHORN_FULL_LOCAL_DIRECTORY#"$ROOT"/}" remote
    [[ "$relative" =~ ^logs/longhorn-full/run\.[A-Za-z0-9-]+$ ]] || return 1
    remote="/usr/local/kubeauto/$relative"
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" verify-preflight \
      --directory "$LONGHORN_FULL_LOCAL_DIRECTORY" || return
    if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then
      ssh_longhorn "umask 077; mkdir -p '$remote'" || return
    else
      ssh_longhorn "umask 077; mkdir -p /usr/local/kubeauto/logs/longhorn-full; test ! -e '$remote' && mkdir '$remote'" || return
    fi
    rsync -rt --chmod=D700,F600 -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2' \
      "$LONGHORN_FULL_LOCAL_DIRECTORY/" "root@192.168.47.130:$remote/" || return
    # Transport only the immutable refs returned by canonical49, never scan logs.
    # Existing remote paths may be stale from an earlier run; checksum-based
    # replacement is required so the remote verifier reads the exact current
    # frozen bytes. The file list remains finite and runner-owned.
    rsync -rt --checksum --chmod=D700,F600 --files-from="$LONGHORN_FULL_LOCAL_DIRECTORY/full-copy-files.txt" \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2' \
      "$ROOT/" root@192.168.47.130:/usr/local/kubeauto/ || return
    ssh_longhorn "cd /usr/local/kubeauto && .venv/bin/python tests/helpers/longhorn-full-evidence.py verify-preflight --directory '$remote'" || return
  fi
  if [[ -n "$focused_work" ]]; then
    ssh_longhorn "umask 077; mkdir -p '$focused_remote'" || return
    rsync -rt --checksum --chmod=D700,F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
      "$focused_work/" "root@192.168.47.130:$focused_remote/" || return
  fi
  printf '%s\n' "$module" >&"$in_fd"
  if [[ "$module" == v2-integrity-qualification ]]; then printf '%s\n' "$focused_remote" >&"$in_fd"; fi
  if [[ "$module" == full ]]; then
    printf '%s\n%s\n' "$remote" "${LONGHORN_FULL_RESUME:-no}" >&"$in_fd"
  fi
  if [[ "$module" == v2-observability-monitoring || "$module" == v1-operations-remaining-v2-monitoring ]]; then
    printf '%s\n%s\n' "$cache_payload" "$cache_sha" >&"$in_fd"
  fi
  if [[ "$module" == v2-upgrade-lifecycle || "$module" == v2-upgrade-cleanup ]]; then printf '%s\n' "$continuation_request" >&"$in_fd"; fi
  if [[ "$module" == node-lifecycle ]]; then printf '%s\n' "$LONGHORN_V2_RELEASE_PROOF" >&"$in_fd"; fi
  if [[ "$module" == v2-disk-lifecycle || "$module" == node-lifecycle ]]; then printf '%s\n' "$LONGHORN_V2_TARGET_RELEASE_PROOF" >&"$in_fd"; fi
  if [[ "$module" == v2-cleanup-recovery || "$module" == v2-cleanup-target-recovery ]]; then
    printf '%s\n' "$LONGHORN_V2_RETIREMENT_JOURNAL" >&"$in_fd"
  fi
  read -r -t 30 ready <&"$out_fd" || return 1
  [[ "$ready" == LONGHORN_DURABLE_LAUNCHED ]] || return 1
  wait "$launcher_pid" || return
  directory="$ROOT/logs/longhorn-cases/$module-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$directory"
  monitor_remote_job longhorn ssh_longhorn '' /tmp/kubeauto-longhorn-gate \
    /tmp/kubeauto-longhorn-live.log "$marker" || rc=$?
  remote_job_summary ssh_longhorn '' /tmp/kubeauto-longhorn-gate /tmp/kubeauto-longhorn-live.log > "$directory/durable-status.txt"
  ssh_longhorn 'cat /tmp/kubeauto-longhorn-live.log' > "$directory/gate.log"
  ssh_longhorn 'cat /tmp/kubeauto-longhorn-gate.exit' > "$directory/durable-exit" || rc=$?
  ssh_longhorn 'cat /tmp/kubeauto-longhorn-gate.finalized' > "$directory/durable-finalized" || rc=$?
  if [[ -n "$continuation_request" ]]; then
    continuation_copy="$(mktemp "$ROOT/${continuation_request%/request.json}/.runtime.XXXXXX")" || return
    if ssh_longhorn "cat /usr/local/kubeauto/${continuation_request%/request.json}/runtime.json" > "$continuation_copy"; then
      if [[ -e "$ROOT/${continuation_request%/request.json}/runtime.json" ]]; then
        cmp -s "$continuation_copy" "$ROOT/${continuation_request%/request.json}/runtime.json" || rc=1
        rm -f -- "$continuation_copy"
      else
        mv -- "$continuation_copy" "$ROOT/${continuation_request%/request.json}/runtime.json" || rc=$?
      fi
    else
      rm -f -- "$continuation_copy"
      [[ "$rc" != 0 ]] || rc=1
    fi
  fi
  if [[ "$module" == full ]]; then
    rsync -rt --chmod=D700,F600 -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2' \
      "root@192.168.47.130:$remote/" "$LONGHORN_FULL_LOCAL_DIRECTORY/" || rc=1
    if [[ "$rc" == 0 ]]; then
      "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" import-result \
        --directory "$LONGHORN_FULL_LOCAL_DIRECTORY" --outer-directory "$directory" || rc=$?
    fi
  fi
  LONGHORN_LAST_DIRECTORY="$directory"
  return "$rc"
}

# Add beside longhorn_launch in tests/helpers/longhorn-runner.sh.
# longhorn_launch additionally sets LONGHORN_LAST_DIRECTORY only after collecting
# the explicit run's gate/status and durable-exit/durable-finalized (see wiring).
longhorn_v2_platform_install() {
  local kind="$1" module marker work
  case "$kind" in
    platform) module=v2-runtime-resources; marker=LONGHORN_V2_RUNTIME_RESOURCES_PASS ;;
    install) module=v2-coexist-qualification; marker=LONGHORN_V2_COEXIST_QUALIFICATION_PASS ;;
    *) return 2 ;;
  esac
  mkdir -p "$ROOT/logs/longhorn-cases" || return
  work="$(mktemp -d "$ROOT/logs/longhorn-cases/v2-$kind-prerequisites.XXXXXX")" || return
  chmod 0700 "$work" || return
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-platform-install.py" \
    preflight "$kind" --root "$ROOT" --receipt "$work/prerequisites.json" \
    | tee "$work/preflight.log" || return
  LONGHORN_LAST_DIRECTORY=
  longhorn_launch "$module" "$marker" || return
  [[ -n "$LONGHORN_LAST_DIRECTORY" ]] || return 1
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-platform-install.py" \
    finish "$kind" --root "$ROOT" --receipt "$work/prerequisites.json" \
    --directory "$LONGHORN_LAST_DIRECTORY" \
    | tee "$work/acceptance.log"
}

longhorn_v2_maintenance() {
  if [[ -z "${LONGHORN_V2_MAINTENANCE_EVIDENCE_JSON:-}" ]]; then
    echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=seven-explicit-maintenance-scopes-required-no-automatic-replay' >&2
    return 1
  fi
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-maintenance.py" \
    --root "$ROOT" --evidence "$LONGHORN_V2_MAINTENANCE_EVIDENCE_JSON"
}

# Canonical 49 composes explicit completed material; it never replays linked scopes.
longhorn_v2_clone() {
  if [[ -z "${LONGHORN_V2_CLONE_DIRECTORY:-}" ]]; then
    echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=explicit-clone-material-directory-required-no-automatic-replay' >&2
    return 1
  fi
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-v2-clone.py" finish \
    --root "$ROOT" --directory "$LONGHORN_V2_CLONE_DIRECTORY"
}


longhorn_full_launch() (
  set -Eeuo pipefail
  umask 077
  mkdir -p "$ROOT/logs/longhorn-full"
  export LONGHORN_FULL_LOCAL_DIRECTORY
  if [[ -n "${LONGHORN_FULL_RESUME_DIRECTORY:-}" ]]; then
    LONGHORN_FULL_LOCAL_DIRECTORY="$(cd "$LONGHORN_FULL_RESUME_DIRECTORY" && pwd)"
    [[ "$LONGHORN_FULL_LOCAL_DIRECTORY" =~ ^$ROOT/logs/longhorn-full/run\.[A-Za-z0-9-]+$ && -d "$LONGHORN_FULL_LOCAL_DIRECTORY" ]]
    export LONGHORN_FULL_RESUME=yes
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" verify-preflight \
      --directory "$LONGHORN_FULL_LOCAL_DIRECTORY"
  else
    LONGHORN_FULL_LOCAL_DIRECTORY="$(mktemp -d "$ROOT/logs/longhorn-full/run.XXXXXXXX")"
    "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-full-evidence.py" ready
    bash "$ROOT/tests/helpers/longhorn-full.sh" local "$LONGHORN_FULL_LOCAL_DIRECTORY"
  fi
  "$ROOT/.venv/bin/python" "$ROOT/tests/helpers/longhorn-lab.py" verify-transport
  echo 'LONGHORN_LADDER completed=3 evidence=all-affected-focused-gates next=4 full=clean-v1-v2'
  longhorn_launch full LONGHORN_DELIVERY_BRANCH_PASS
)

longhorn_dispatch() {
  case "$1" in
    --longhorn-only) longhorn_full_launch ;;
    --longhorn-v2-clone) longhorn_v2_clone ;;
    --longhorn-v2-linked-acceptance) longhorn_launch v2-linked-acceptance LONGHORN_V2_LINKED_ACCEPTANCE_PASS ;;
    --longhorn-v1-backup-v2-data-protection) longhorn_launch v1-backup-v2-data-protection LONGHORN_V1_BACKUP_V2_DATA_PROTECTION_PASS ;;
    --longhorn-ownership-evidence-import) longhorn_local_gate ownership-evidence LONGHORN_OWNERSHIP_PASS ;;
    --longhorn-v1-operations-v2-disk-tail) longhorn_launch v1-operations-v2-disk-tail LONGHORN_V1_OPERATIONS_V2_DISK_TAIL_PASS ;;
    --longhorn-v1-operations-v2-worker-tail) longhorn_launch v1-operations-v2-worker-tail LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL_PASS ;;
    --longhorn-v1-ownership-tail-v2-failure-tail) longhorn_launch v1-ownership-tail-v2-failure-tail LONGHORN_V1_OWNERSHIP_TAIL_V2_FAILURE_TAIL_PASS ;;
    --longhorn-v2-platform) longhorn_v2_platform_install platform ;;
    --longhorn-v2-install) longhorn_v2_platform_install install ;;
    --longhorn-cleanup) longhorn_launch cleanup LONGHORN_CLEANUP_PASS ;;
    --longhorn-cleanup-qualification) longhorn_launch cleanup-qualification LONGHORN_CLEANUP_QUALIFICATION_PASS ;;
    --longhorn-v1-ownership-operations-v2-failure-platform) longhorn_launch v1-ownership-operations-v2-failure-platform LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS ;;
    --longhorn-v1-ownership-v2-failure-remaining) longhorn_launch v1-ownership-v2-failure-remaining LONGHORN_V1_OWNERSHIP_V2_FAILURE_REMAINING_PASS ;;
    --longhorn-v2-failure-remaining) longhorn_launch v2-failure-remaining LONGHORN_V2_FAILURE_REMAINING_PASS ;;
    --longhorn-v1-operations-v2-platform-install) longhorn_launch v1-operations-v2-platform-install LONGHORN_V1_OPERATIONS_V2_PLATFORM_INSTALL_PASS ;;
    --longhorn-v1-ownership-v2-linked-acceptance) longhorn_launch v1-ownership-v2-linked-acceptance LONGHORN_V1_OWNERSHIP_V2_LINKED_ACCEPTANCE_PASS ;;
    --longhorn-v2-incomplete-clone-qualification) longhorn_launch v2-incomplete-clone-qualification LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION_PASS ;;
    --longhorn-v2-incomplete-clone-cleanup) longhorn_launch v2-incomplete-clone-cleanup LONGHORN_V2_INCOMPLETE_CLONE_CLEANUP_PASS ;;
    --longhorn-v2-disk-lifecycle) longhorn_launch v2-disk-lifecycle LONGHORN_V2_DISK_LIFECYCLE_PASS ;;
    --longhorn-v2-disk-lifecycle-cleanup) longhorn_launch v2-disk-lifecycle-cleanup LONGHORN_V2_DISK_LIFECYCLE_CLEANUP_PASS ;;
    --longhorn-upgrade|--longhorn-upgrade-recovery) longhorn_launch v1-upgrade-lifecycle LONGHORN_V1_UPGRADE_GATE_PASS ;;
    --longhorn-v2-source-target-im-revision-3-adopt-qualification) longhorn_launch v2-source-target-im-revision-3-adopt-qualification LONGHORN_V2_SOURCE_TARGET_IM_REVISION_3_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-source-target-im-revision-2-adopt-qualification) longhorn_launch v2-source-target-im-revision-2-adopt-qualification LONGHORN_V2_SOURCE_TARGET_IM_REVISION_2_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-runtime-resources) longhorn_launch v2-runtime-resources LONGHORN_V2_RUNTIME_RESOURCES_PASS ;;
    --longhorn-v2-runtime-resources-cleanup) longhorn_launch v2-runtime-resources-cleanup LONGHORN_V2_RUNTIME_RESOURCES_CLEANUP_PASS ;;
    --longhorn-v1-post-revision-backups-v2-failure-acceptance) longhorn_launch v1-post-revision-backups-v2-failure-acceptance LONGHORN_V1_POST_REVISION_BACKUPS_V2_FAILURE_ACCEPTANCE_PASS ;;
    --longhorn-v1-operations-remaining-v2-monitoring) longhorn_launch v1-operations-remaining-v2-monitoring LONGHORN_V1_OPERATIONS_REMAINING_V2_MONITORING_PASS ;;
    --longhorn-v1-stability-v2-observability) longhorn_launch v1-stability-v2-observability LONGHORN_V1_STABILITY_V2_OBSERVABILITY_PASS ;;
    --longhorn-v2-upgrade|--longhorn-v2-upgrade-lifecycle) longhorn_launch v2-upgrade-lifecycle LONGHORN_V2_UPGRADE_GATE_PASS ;;
    --longhorn-v2-upgrade-cleanup) longhorn_launch v2-upgrade-cleanup LONGHORN_V2_UPGRADE_CLEANUP_PASS ;;
    --longhorn-middleware) longhorn_launch middleware LONGHORN_MIDDLEWARE_PASS ;;
    --longhorn-middleware-business) longhorn_launch middleware-business LONGHORN_MIDDLEWARE_BUSINESS_BASELINE_RETAINED ;;
    --longhorn-middleware-cleanup) longhorn_launch middleware-cleanup LONGHORN_MIDDLEWARE_CLEANUP_PASS ;;
    --longhorn-middleware-reentry-cleanup) longhorn_launch middleware-reentry-cleanup LONGHORN_MIDDLEWARE_CLEANUP_PASS ;;
    --longhorn-v2-observability-performance) longhorn_launch v2-observability LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS ;;
    --longhorn-last-replica) longhorn_launch last-replica LONGHORN_LAST_REPLICA_GATE_PASS ;;
    --longhorn-last-replica-cleanup) longhorn_launch last-replica-cleanup LONGHORN_LAST_REPLICA_CLEANUP_PASS ;;
    --longhorn-disk-failure) longhorn_launch disk-failure LONGHORN_DISK_FAILURE_PASS ;;
    --longhorn-disk-failure-cleanup) longhorn_launch disk-failure-cleanup LONGHORN_DISK_FAILURE_CLEANUP_PASS ;;
    --longhorn-network-failure) longhorn_launch network-failure LONGHORN_NETWORK_FAILURE_PASS ;;
    --longhorn-network-failure-cleanup) longhorn_launch network-failure-cleanup LONGHORN_NETWORK_FAILURE_CLEANUP_PASS ;;
    --longhorn-v1-dr-tail-v2-install-tail) longhorn_launch v1-dr-tail-v2-install-tail LONGHORN_V1_DR_TAIL_V2_INSTALL_TAIL_PASS ;;
    --longhorn-v1-upgrade-tail-v2-install-tail) longhorn_launch v1-upgrade-tail-v2-install-tail LONGHORN_V1_UPGRADE_TAIL_V2_INSTALL_TAIL_PASS ;;
    --longhorn-v1-monitoring-v2-resource-tail) longhorn_launch v1-monitoring-v2-resource-tail LONGHORN_V1_MONITORING_V2_RESOURCE_TAIL_PASS ;;
    --longhorn-v1-upgrade-tail-v2-maintenance-tail) longhorn_launch v1-upgrade-tail-v2-maintenance-tail LONGHORN_V1_UPGRADE_TAIL_V2_MAINTENANCE_TAIL_PASS ;;
    --longhorn-v1-recovery-tail-v2-install-tail) longhorn_launch v1-recovery-tail-v2-install-tail LONGHORN_V1_RECOVERY_TAIL_V2_INSTALL_TAIL_PASS ;;
    --longhorn-v1-system-restore-v2-runtime-resources) longhorn_launch v1-system-restore-v2-runtime-resources LONGHORN_V1_SYSTEM_RESTORE_V2_RUNTIME_RESOURCES_PASS ;;
    --longhorn-system-restore) longhorn_launch system-restore LONGHORN_SYSTEM_RESTORE_GATE_PASS ;;
    --longhorn-system-restore-prepared-recovery) longhorn_launch system-restore-prepared-recovery LONGHORN_SYSTEM_RESTORE_GATE_PASS ;;
    --longhorn-system-restore-cleanup) longhorn_launch system-restore-cleanup LONGHORN_SYSTEM_RESTORE_CLEANUP_PASS ;;
    --longhorn-disaster-recovery) longhorn_launch disaster-recovery LONGHORN_DISASTER_RECOVERY_PASS ;;
    --longhorn-disaster-recovery-cleanup) longhorn_launch disaster-recovery-cleanup LONGHORN_DISASTER_RECOVERY_CLEANUP_PASS ;;
    --longhorn-node-failure) longhorn_launch node-failure LONGHORN_NODE_FAILURE_GATE_PASS ;;
    --longhorn-node-failure-cleanup) longhorn_launch node-failure-cleanup LONGHORN_NODE_FAILURE_CLEANUP_PASS ;;
    --longhorn-observability) longhorn_launch observability LONGHORN_OBSERVABILITY_PASS ;;
    --longhorn-observability-business) longhorn_launch observability-business LONGHORN_OBSERVABILITY_BUSINESS_BASELINE_RETAINED ;;
    --longhorn-observability-support) longhorn_launch observability-support LONGHORN_OBSERVABILITY_SUPPORT_PASS ;;
    --longhorn-observability-image-cache) longhorn_launch observability-image-cache LONGHORN_OBSERVABILITY_IMAGE_CACHE_PASS ;;
    --longhorn-observability-cleanup) longhorn_launch observability-cleanup LONGHORN_OBSERVABILITY_CLEANUP_PASS ;;
    --longhorn-v1-im-revision-3-adopt-qualification) longhorn_launch v1-im-revision-3-adopt-qualification LONGHORN_V1_IM_REVISION_3_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v1-im-revision-2-adopt-qualification) longhorn_launch v1-im-revision-2-adopt-qualification LONGHORN_V1_IM_REVISION_2_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-im-revision-3-adopt-qualification) longhorn_launch v2-im-revision-3-adopt-qualification LONGHORN_V2_IM_REVISION_3_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-im-revision-2-adopt-qualification) longhorn_launch v2-im-revision-2-adopt-qualification LONGHORN_V2_IM_REVISION_2_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-target-im-revision-3-adopt-qualification) longhorn_launch v2-dr-target-im-revision-3-adopt-qualification LONGHORN_V2_DR_TARGET_IM_REVISION_3_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-target-im-revision-2-adopt-qualification) longhorn_launch v2-dr-target-im-revision-2-adopt-qualification LONGHORN_V2_DR_TARGET_IM_REVISION_2_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v1-upgrade-lifecycle) longhorn_launch v1-upgrade-lifecycle LONGHORN_V1_UPGRADE_GATE_PASS ;;
    --longhorn-v1-upgrade-cleanup) longhorn_launch v1-upgrade-cleanup LONGHORN_V1_UPGRADE_CLEANUP_PASS ;;
    --longhorn-v2-maintenance) longhorn_v2_maintenance ;;
    --longhorn-v2-maintenance-drain-qualification) longhorn_launch v2-maintenance-drain-qualification LONGHORN_V2_MAINTENANCE_DRAIN_QUALIFICATION_PASS ;;
    --longhorn-v2-maintenance-drain-cleanup) longhorn_launch v2-maintenance-drain-cleanup LONGHORN_V2_MAINTENANCE_DRAIN_CLEANUP_PASS ;;
    --longhorn-v2-coexist-qualification) longhorn_launch v2-coexist-qualification LONGHORN_V2_COEXIST_QUALIFICATION_PASS ;;
    --longhorn-v2-coexist-cleanup) longhorn_launch v2-coexist-cleanup LONGHORN_V2_COEXIST_CLEANUP_PASS ;;
    --longhorn-v2-observability) longhorn_launch v2-observability LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS ;;
    --longhorn-v2-observability-monitoring) longhorn_launch v2-observability-monitoring LONGHORN_V2_OBSERVABILITY_MONITORING_PASS ;;
    --longhorn-v2-observability-business) longhorn_launch v2-observability-business LONGHORN_V2_OBSERVABILITY_BUSINESS_BASELINE_RETAINED ;;
    --longhorn-v2-cleanup-recovery) longhorn_launch v2-cleanup-recovery LONGHORN_V2_CLEANUP_PASS ;;
    --longhorn-v2-cleanup-target-recovery) longhorn_launch v2-cleanup-target-recovery LONGHORN_V2_TARGET_CLEANUP_PASS ;;
    --longhorn-v2-middleware) longhorn_launch v2-middleware LONGHORN_V2_MIDDLEWARE_PASS ;;
    --longhorn-v2-middleware-cleanup) longhorn_launch v2-middleware-cleanup LONGHORN_V2_MIDDLEWARE_CLEANUP_PASS ;;
    --longhorn-v2-cleanup) longhorn_launch v2-cleanup LONGHORN_V2_CLEANUP_PASS ;;
    --longhorn-v2-cleanup-target) longhorn_launch v2-cleanup-target LONGHORN_V2_TARGET_CLEANUP_PASS ;;
    --longhorn-stability-cleanup) longhorn_launch stability-cleanup LONGHORN_STABILITY_SCOPED_CLEAN_PASS ;;
    --longhorn-stability) longhorn_launch stability LONGHORN_STABILITY_PASS ;;
    --longhorn-node-lifecycle) longhorn_launch node-lifecycle LONGHORN_NODE_LIFECYCLE_GATE_PASS ;;
    --longhorn-node-lifecycle-cleanup) longhorn_launch node-lifecycle-cleanup LONGHORN_NODE_LIFECYCLE_CLEANUP_PASS ;;
    --longhorn-v2-source-target-reboot-recovery) longhorn_launch v2-source-target-reboot-recovery LONGHORN_V2_SOURCE_TARGET_REBOOT_RECOVERY_PASS ;;
    --longhorn-v2-source-target-im-adopt-qualification) longhorn_launch v2-source-target-im-adopt-qualification LONGHORN_V2_SOURCE_TARGET_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v1-backups-v2-linked-failure-acceptance) longhorn_launch v1-backups-v2-linked-failure-acceptance LONGHORN_V1_BACKUPS_V2_LINKED_FAILURE_ACCEPTANCE_PASS ;;
    --longhorn-v1-backups-v2-linked-acceptance) longhorn_launch v1-backups-v2-linked-acceptance LONGHORN_V1_BACKUPS_V2_LINKED_ACCEPTANCE_PASS ;;
    --longhorn-v1-remaining-v2-linked-acceptance) longhorn_launch v1-remaining-v2-linked-acceptance LONGHORN_V1_REMAINING_V2_LINKED_ACCEPTANCE_PASS ;;
    --longhorn-v1-backups-remaining) longhorn_launch v1-backups-remaining LONGHORN_PARALLEL_V1_BACKUPS_REMAINING_PASS ;;
    --longhorn-v1-backups) longhorn_launch v1-backups LONGHORN_PARALLEL_V1_BACKUPS_PASS ;;
    --longhorn-v1-platform-v2-environment-recovery) longhorn_launch v1-platform-v2-environment-recovery LONGHORN_V1_PLATFORM_V2_ENVIRONMENT_RECOVERY_PASS ;;
    --longhorn-v2-source-target-environment-recovery) longhorn_launch v2-source-target-environment-recovery LONGHORN_V2_SOURCE_TARGET_ENVIRONMENT_RECOVERY_PASS ;;
    --longhorn-v2-post-reboot-environment-recovery) longhorn_launch v2-post-reboot-environment-recovery LONGHORN_V2_POST_REBOOT_ENVIRONMENT_RECOVERY_PASS ;;
    --longhorn-v2-dr-target-post-reboot-environment-recovery) longhorn_launch v2-dr-target-post-reboot-environment-recovery LONGHORN_V2_DR_TARGET_POST_REBOOT_ENVIRONMENT_RECOVERY_PASS ;;
    --longhorn-v1-platform-propagation-v2-im-adopt-qualification) longhorn_launch v1-platform-propagation-v2-im-adopt-qualification LONGHORN_V1_PLATFORM_PROPAGATION_V2_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-platform-propagation-qualification) longhorn_launch platform-propagation-qualification LONGHORN_PLATFORM_PROPAGATION_QUALIFICATION_PASS ;;
    --longhorn-platform) longhorn_launch platform LONGHORN_PLATFORM_PASS ;;
    --longhorn-documentation) longhorn_local_gate documentation LONGHORN_DOCUMENTATION_PASS ;;
    --longhorn-ownership) longhorn_launch ownership LONGHORN_OWNERSHIP_PASS ;;
    --longhorn-v1-backups-v2-linked-qualification) longhorn_launch v1-backups-v2-linked-qualification LONGHORN_V1_BACKUPS_V2_LINKED_QUALIFICATION_PASS ;;
    --longhorn-v1-im-adopt-qualification) longhorn_launch v1-im-adopt-qualification LONGHORN_V1_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v1-v2-im-adopt-qualification) longhorn_launch v1-v2-im-adopt-qualification LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-im-adopt-qualification) longhorn_launch v2-im-adopt-qualification LONGHORN_V2_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v2-resources-reboot-recovery) longhorn_launch v2-resources-reboot-recovery LONGHORN_V2_RESOURCES_REBOOT_RECOVERY_PASS ;;
    --longhorn-v2-dr-target-resources-reboot-recovery) longhorn_launch v2-dr-target-resources-reboot-recovery LONGHORN_V2_DR_TARGET_RESOURCES_REBOOT_RECOVERY_PASS ;;
    --longhorn-v2-dr-target-im-adopt-qualification) longhorn_launch v2-dr-target-im-adopt-qualification LONGHORN_V2_DR_TARGET_IM_ADOPT_QUALIFICATION_PASS ;;
    --longhorn-v1-v2-control-qualification) longhorn_launch v1-v2-control-qualification LONGHORN_V1_V2_CONTROL_QUALIFICATION_PASS ;;
    --longhorn-v2-pool-inputs) longhorn_local_gate v2-pool-inputs LONGHORN_V2_POOL_INPUTS_PASS ;;
    --longhorn-v2-registry-qualification) longhorn_launch v2-registry-qualification LONGHORN_V2_REGISTRY_QUALIFICATION_PASS ;;
    --longhorn-v2-core) longhorn_launch v2-core LONGHORN_V2_CORE_PASS ;;
    --longhorn-v2-recurring-selector-qualification) longhorn_launch v2-recurring-selector-qualification LONGHORN_V2_RECURRING_SELECTOR_QUALIFICATION_PASS ;;
    --longhorn-v2-recurring-snapshot-qualification) longhorn_launch v2-recurring-snapshot-qualification LONGHORN_V2_RECURRING_SNAPSHOT_QUALIFICATION_PASS ;;
    --longhorn-v2-recurring-backup-qualification) longhorn_launch v2-recurring-backup-qualification LONGHORN_V2_RECURRING_BACKUP_QUALIFICATION_PASS ;;
    --longhorn-v2-recurring-rotation-qualification) longhorn_launch v2-recurring-rotation-qualification LONGHORN_V2_RECURRING_ROTATION_QUALIFICATION_PASS ;;
    --longhorn-v2-backup-refusal-qualification) longhorn_launch v2-backup-refusal-qualification LONGHORN_V2_BACKUP_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-v2-backup-store-qualification) longhorn_launch v2-backup-store-qualification LONGHORN_V2_BACKUP_STORE_QUALIFICATION_PASS ;;
    --longhorn-v2-backup-qualification) longhorn_launch v2-backup-qualification LONGHORN_V2_BACKUP_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-target-registry-qualification) longhorn_launch v2-dr-target-registry-qualification LONGHORN_V2_DR_TARGET_REGISTRY_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-target-core) longhorn_launch v2-dr-target-core LONGHORN_V2_DR_TARGET_CORE_PASS ;;
    --longhorn-v2-dr-target-core-recovery) longhorn_launch v2-dr-target-core-recovery LONGHORN_V2_DR_TARGET_CORE_RECOVERY_PASS ;;
    --longhorn-v2-data-protection) longhorn_launch v2-data-protection LONGHORN_V2_DATA_PROTECTION_PASS ;;
    --longhorn-v2-dr-activation-qualification) longhorn_launch v2-dr-activation-qualification LONGHORN_V2_DR_ACTIVATION_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-failback-qualification) longhorn_launch v2-dr-failback-qualification LONGHORN_V2_DR_FAILBACK_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-standby-qualification) longhorn_launch v2-dr-standby-qualification LONGHORN_V2_DR_STANDBY_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-store-qualification) longhorn_launch v2-dr-store-qualification LONGHORN_V2_DR_STORE_QUALIFICATION_PASS ;;
    --longhorn-v2-dr-target-install-recovery) longhorn_launch v2-dr-target-install-recovery LONGHORN_V2_DR_TARGET_INSTALL_RECOVERY_PASS ;;
    --longhorn-v2-dr-target-install-qualification) longhorn_launch v2-dr-target-install-qualification LONGHORN_V2_DR_TARGET_INSTALL_QUALIFICATION_PASS ;;
    --longhorn-v2-detached-backup-recovery) longhorn_launch v2-detached-backup-recovery LONGHORN_V2_DETACHED_BACKUP_RECOVERY_PASS ;;
    --longhorn-v2-detached-backup-qualification) longhorn_launch v2-detached-backup-qualification LONGHORN_V2_DETACHED_BACKUP_QUALIFICATION_PASS ;;
    --longhorn-v2-systemrestore-recovery) longhorn_launch v2-systemrestore-recovery LONGHORN_V2_SYSTEMRESTORE_RECOVERY_PASS ;;
    --longhorn-v2-systemrestore-qualification) longhorn_launch v2-systemrestore-qualification LONGHORN_V2_SYSTEMRESTORE_QUALIFICATION_PASS ;;
    --longhorn-v2-systembackup-cleanup) longhorn_launch v2-systembackup-cleanup LONGHORN_V2_SYSTEMBACKUP_CLEANUP_PASS ;;
    --longhorn-v2-systembackup-recovery) longhorn_launch v2-systembackup-recovery LONGHORN_V2_SYSTEMBACKUP_RECOVERY_PASS ;;
    --longhorn-v2-systembackup-qualification) longhorn_launch v2-systembackup-qualification LONGHORN_V2_SYSTEMBACKUP_QUALIFICATION_PASS ;;
    --longhorn-v2-broken-restore-cleanup) longhorn_launch v2-broken-restore-cleanup LONGHORN_V2_BROKEN_RESTORE_CLEANUP_PASS ;;
    --longhorn-v2-broken-restore-qualification) longhorn_launch v2-broken-restore-qualification LONGHORN_V2_BROKEN_RESTORE_QUALIFICATION_PASS ;;
    --longhorn-v2-failure) longhorn_launch v2-failure LONGHORN_V2_FAILURE_PASS ;;
    --longhorn-v2-failure-cleanup) longhorn_launch v2-failure-cleanup LONGHORN_V2_FAILURE_CLEANUP_PASS ;;
    --longhorn-v2-failure-replica-im) longhorn_launch v2-failure-replica-im LONGHORN_V2_FAILURE_REPLICA_IM_PASS ;;
    --longhorn-v2-failure-manager) longhorn_launch v2-failure-manager LONGHORN_V2_FAILURE_MANAGER_PASS ;;
    --longhorn-v2-failure-attached-spdk) longhorn_launch v2-failure-attached-spdk LONGHORN_V2_FAILURE_ATTACHED_SPDK_PASS ;;
    --longhorn-v2-failure-remote-spdk) longhorn_launch v2-failure-remote-spdk LONGHORN_V2_FAILURE_REMOTE_SPDK_PASS ;;
    --longhorn-v2-failure-replica-network) longhorn_launch v2-failure-replica-network LONGHORN_V2_FAILURE_REPLICA_NETWORK_PASS ;;
    --longhorn-v2-failure-worker-reboot) longhorn_launch v2-failure-worker-reboot LONGHORN_V2_FAILURE_WORKER_REBOOT_PASS ;;
    --longhorn-v2-failure-single-disk) longhorn_launch v2-failure-single-disk LONGHORN_V2_FAILURE_SINGLE_DISK_PASS ;;
    --longhorn-v2-failure-rebuild-qos) longhorn_launch v2-failure-rebuild-qos LONGHORN_V2_FAILURE_REBUILD_QOS_PASS ;;
    --longhorn-v2-failure-all-replicas) longhorn_launch v2-failure-all-replicas LONGHORN_V2_FAILURE_ALL_REPLICAS_PASS ;;
    --longhorn-v2-linked-lifecycle-qualification) longhorn_launch v2-linked-lifecycle-qualification LONGHORN_V2_LINKED_LIFECYCLE_QUALIFICATION_PASS ;;
    --longhorn-v2-linked-lifecycle-cleanup) longhorn_launch v2-linked-lifecycle-cleanup LONGHORN_V2_LINKED_LIFECYCLE_CLEANUP_PASS ;;
    --longhorn-v2-linked-clone-cleanup) longhorn_launch v2-linked-clone-cleanup LONGHORN_V2_LINKED_CLONE_CLEANUP_PASS ;;
    --longhorn-v2-linked-clone-qualification) longhorn_launch v2-linked-clone-qualification LONGHORN_V2_LINKED_CLONE_QUALIFICATION_PASS ;;
    --longhorn-v2-interrupt-manager-qualification) longhorn_launch v2-interrupt-manager-qualification LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS ;;
    --longhorn-v2-manager-loss-cleanup) longhorn_launch v2-manager-loss-cleanup LONGHORN_V2_MANAGER_LOSS_CLEANUP_PASS ;;
    --longhorn-v2-manager-loss-qualification) longhorn_launch v2-manager-loss-qualification LONGHORN_V2_MANAGER_LOSS_QUALIFICATION_PASS ;;
    --longhorn-v2-interrupt-cleanup) longhorn_launch v2-interrupt-cleanup LONGHORN_V2_INTERRUPT_CLEANUP_PASS ;;
    --longhorn-v2-interrupt-qualification) longhorn_launch v2-interrupt-qualification LONGHORN_V2_INTERRUPT_QUALIFICATION_PASS ;;
    --longhorn-v2-replica-im-loss-cleanup) longhorn_launch v2-replica-im-loss-cleanup LONGHORN_V2_REPLICA_IM_LOSS_CLEANUP_PASS ;;
    --longhorn-v2-replica-im-loss-qualification) longhorn_launch v2-replica-im-loss-qualification LONGHORN_V2_REPLICA_IM_LOSS_QUALIFICATION_PASS ;;
    --longhorn-v2-full-copy-lifecycle-qualification) longhorn_launch v2-full-copy-lifecycle-qualification LONGHORN_V2_FULL_COPY_LIFECYCLE_QUALIFICATION_PASS ;;
    --longhorn-v2-full-copy-clone-qualification) longhorn_launch v2-full-copy-clone-qualification LONGHORN_V2_FULL_COPY_CLONE_QUALIFICATION_PASS ;;
    --longhorn-v2-integrity-qualification) longhorn_launch v2-integrity-qualification LONGHORN_V2_INTEGRITY_QUALIFICATION_PASS ;;
    --longhorn-v2-snapshot-qualification) longhorn_launch v2-snapshot-qualification LONGHORN_V2_SNAPSHOT_QUALIFICATION_PASS ;;
    --longhorn-v2-volumes) longhorn_launch v2-volumes LONGHORN_V2_VOLUMES_PASS ;;
    --longhorn-v2-trim-qualification) longhorn_launch v2-trim-qualification LONGHORN_V2_TRIM_QUALIFICATION_PASS ;;
    --longhorn-v2-encryption-qualification) longhorn_launch v2-encryption-qualification LONGHORN_V2_ENCRYPTION_QUALIFICATION_PASS ;;
    --longhorn-v2-reclaim-qualification) longhorn_launch v2-reclaim-qualification LONGHORN_V2_RECLAIM_QUALIFICATION_PASS ;;
    --longhorn-v2-expansion-qualification) longhorn_launch v2-expansion-qualification LONGHORN_V2_EXPANSION_QUALIFICATION_PASS ;;
    --longhorn-v2-volume-modes-qualification) longhorn_launch v2-volume-modes-qualification LONGHORN_V2_VOLUME_MODES_QUALIFICATION_PASS ;;
    --longhorn-v2-host-recovery) longhorn_launch v2-host-recovery LONGHORN_V2_HOST_RECOVERY_PASS ;;
    --longhorn-v2-host-qualification) longhorn_launch v2-host-qualification LONGHORN_V2_HOST_QUALIFICATION_PASS ;;
    --longhorn-v2-aio-install-qualification) longhorn_launch v2-aio-install-qualification LONGHORN_V2_AIO_INSTALL_QUALIFICATION_PASS ;;
    --longhorn-v2-resources-recovery) longhorn_launch v2-resources-recovery LONGHORN_V2_RESOURCES_RECOVERY_PASS ;;
    --longhorn-v2-resources-qualification) longhorn_launch v2-resources-qualification LONGHORN_V2_RESOURCES_QUALIFICATION_PASS ;;
    --longhorn-v2-host-inputs)
      # Read-only host inventory; no source sync, cluster use or host changes.
      # Keep the historical general inventory independent of the new V2 pool.
      python3 - <<'PY'
import json,shlex,subprocess
probe="""import json,platform,re,subprocess
from pathlib import Path
kernel=platform.release(); version=re.match(r"(\\d+)\\.(\\d+)",kernel)
info=Path("/proc/meminfo").read_text()
modules={name:Path("/sys/module",name).exists() for name in ("vfio_pci","uio_pci_generic","nvme_tcp")}
pages=Path("/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages")
disks=subprocess.run(["lsblk","--json","--bytes","--output","NAME,TYPE,SIZE,RO,FSTYPE,MOUNTPOINTS,WWN,SERIAL"],text=True,capture_output=True,timeout=15)
print(json.dumps({"kernel":kernel,"kernel_ge_6_7":bool(version and tuple(map(int,version.groups()))>=(6,7)),
 "architecture":platform.machine(),"online_cpus":Path("/sys/devices/system/cpu/online").read_text().strip(),
 "memory_kib":{key:int(value) for key,value in re.findall(r"^(MemTotal|MemAvailable):\\s+(\\d+) kB$",info,re.M)},
 "loaded_modules":modules,"hugepages_2mi":int(pages.read_text()) if pages.exists() else 0,
 "lsblk_rc":disks.returncode,"disks":json.loads(disks.stdout) if disks.returncode==0 else None},sort_keys=True))
"""
hosts=("root@192.168.122.135","root@192.168.122.40","root@192.168.122.72",
       "root@192.168.122.212","root@192.168.122.165","root@192.168.122.238",
       "brinnatt@192.168.47.128","ubuntu@192.168.47.138","root@192.168.47.143",
       "root@192.168.122.2","root@192.168.122.217","root@192.168.122.243",
       "root@192.168.122.246","root@192.168.122.193","root@192.168.122.210","root@192.168.122.216",
       "root@192.168.47.130","root@192.168.47.131","root@192.168.47.132",
       "root@192.168.47.133","root@192.168.47.134","root@192.168.47.135",
       "root@192.168.47.136","root@192.168.47.137","root@192.168.47.141","root@192.168.47.142")
for host in hosts:
 try:
  result=subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=5",host,
                         shlex.join(["python3","-c",probe])],text=True,capture_output=True,timeout=25)
  row={"host":host,"rc":result.returncode,"inventory":json.loads(result.stdout) if result.returncode==0 else None}
 except (subprocess.TimeoutExpired,ValueError) as error:
  row={"host":host,"error_type":type(error).__name__}
 print(json.dumps(row,sort_keys=True),flush=True)
PY
      ;;
    --longhorn-recurring-diagnostics)
      # Read the original runtime journal and public selector/owner state only.
      # No synchronization, credential output or mutation of the active run.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json
from pathlib import Path
spec=importlib.util.spec_from_file_location("lab",Path("tests/helpers/longhorn-lab.py"))
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
lab.owner()
record=lab.DIRECTORY/"longhorn-recurring-runtime.json"
assert record.is_file() and not record.is_symlink()
row=json.loads(record.read_text());state=lab.owner()
assert row["run_id"]==state["run_id"] and row["cluster_uid"]==state["cluster_uid"]
source=lab.get("volumes.longhorn.io",row["volume"],"longhorn-system")
assert source and source["metadata"]["uid"]==row["volume_uid"]
print(json.dumps({"name":row["name"],"uid":row["uid"],"group":row["group"],
 "source_uid":row["volume_uid"],"source_labels":source["metadata"].get("labels",{}),
 "source_state":source.get("status",{}).get("state")},sort_keys=True))
for kind in ("recurringjobs.longhorn.io","cronjob"):
 obj=lab.get(kind,row["name"],"longhorn-system")
 metadata={key:obj["metadata"].get(key) for key in
  ("name","uid","resourceVersion","deletionTimestamp","finalizers","ownerReferences")} if obj else None
 print(json.dumps({"resource":kind,"metadata":metadata},sort_keys=True))
snapshots=lab.get("snapshots.longhorn.io",namespace="longhorn-system")["items"]
snapshot_names={obj["metadata"]["name"] for obj in snapshots}
for obj in lab.get("backups.longhorn.io",namespace="longhorn-system")["items"]:
 if obj.get("spec",{}).get("labels",{}).get("RecurringJob")!=row["name"]: continue
 name=obj["metadata"]["name"];specification=obj["spec"];status=obj.get("status",{});labels=obj["metadata"].get("labels",{})
 print(json.dumps({"backup":name,"uid":obj["metadata"]["uid"],"recorded_uid":row.get("backups",{}).get(name),
  "source_matches":labels.get("backup-volume")==row["volume"],"target_matches":labels.get("backup-target")=="default",
  "spec_snapshot":specification.get("snapshotName"),"status_snapshot":status.get("snapshotName"),
  "snapshot_recorded":specification.get("snapshotName") in row["snapshots"],
  "snapshot_present":specification.get("snapshotName") in snapshot_names,
  "state":status.get("state"),"error_present":bool(status.get("error")),
  "created":obj["metadata"].get("creationTimestamp")},sort_keys=True))
 if status.get("state")=="Completed":
  store=lab.load_helper("longhorn-backups.py").Store(lab);fixture=store.receipt()
  prefix=lab.load_helper("longhorn-backups.py").volume_prefix(row["volume"])
  raw=store.mc("cat","lh/"+fixture["bucket"]+"/"+prefix+"backups/backup_"+name+".cfg")
  metadata=json.loads(raw)
  import hashlib
  print(json.dumps({"backup_metadata_for":name,"sha256":hashlib.sha256(raw.encode()).hexdigest(),
   "name_matches":metadata.get("Name")==name,"volume_matches":metadata.get("VolumeName")==row["volume"],
   "snapshot_matches":metadata.get("SnapshotName")==specification.get("snapshotName"),
   "job_matches":metadata.get("Labels",{}).get("RecurringJob")==row["name"],
   "created_present":bool(metadata.get("CreatedTime")),"blocks_present":bool(metadata.get("Blocks"))},sort_keys=True))
PY'
      ;;
    --longhorn-business-diagnostics)
      # Read only the current owned business namespace and public identities.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json
from pathlib import Path
spec=importlib.util.spec_from_file_location("lab",Path("tests/helpers/longhorn-lab.py"))
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
lab.owner()
row=json.loads((lab.DIRECTORY/"longhorn-install-business.json").read_text())
namespace=row["namespace"]
assert namespace.startswith("longhorn-business-")
assert lab.get("namespace",namespace)["metadata"]["uid"]==row["namespace_uid"]
print(json.dumps({key:row[key] for key in ("namespace","namespace_uid","objects")},sort_keys=True))
for kind in ("pods","persistentvolumeclaims","events","events.events.k8s.io"):
 for obj in lab.get(kind,namespace=namespace)["items"]:
  print(json.dumps({"resource":kind,"metadata":{key:obj["metadata"].get(key) for key in ("name","uid","creationTimestamp")},
   "reference":obj.get("involvedObject",obj.get("regarding",{})),"reason":obj.get("reason")},sort_keys=True))
for kind in ("volumes.longhorn.io","replicas.longhorn.io","snapshots.longhorn.io","backups.longhorn.io",
             "backupvolumes.longhorn.io","backuptargets.longhorn.io","instancemanagers.longhorn.io"):
 for obj in lab.get(kind,namespace="longhorn-system")["items"]:
  state=obj.get("status",{});specification=obj.get("spec",{})
  print(json.dumps({"resource":kind,"metadata":{key:obj["metadata"].get(key) for key in ("name","uid","deletionTimestamp")},
   "spec":{key:specification[key] for key in ("volumeName","volume","snapshotName","nodeID","desiredState","failedAt") if key in specification},
   "status":{key:state[key] for key in ("state","currentState","ownerID","progress","robustness","available","readyToUse","lastSyncedAt") if key in state},
   "error_present":bool(state.get("error"))},sort_keys=True))
if (lab.DIRECTORY/"longhorn-backup-store.json").is_file():
 store=lab.load_helper("longhorn-backups.py").Store(lab)
 original=store.receipt();container=store.inspect(original);binding=store.transport_receipt(original)
 print(json.dumps({"backup_store_live":store.live(),"container_present":bool(container),
                   "transport_listener_present":bool(binding and binding.get("listener"))},sort_keys=True))
 ca=(store.directory/"cas/ca.crt").read_text()
 probe="import json,ssl,sys,urllib.request\nc=json.load(sys.stdin)\ntry:\n ctx=ssl.create_default_context(cadata=c[\"ca\"]);op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx));r=op.open(c[\"url\"],timeout=5);assert r.status==200\nexcept Exception as e:\n reason=getattr(e,\"reason\",None);print(json.dumps({\"error_type\":type(e).__name__,\"reason_type\":type(reason).__name__,\"errno\":getattr(reason,\"errno\",None)}));sys.exit(1)"
 for node in lab.NODES:
  result=lab.remote(node,["python3","-c",probe],stdin=json.dumps({"ca":ca,"url":store.product_endpoint+"/minio/health/live"}),check=False,timeout=20)
  diagnostic=json.loads(result.stdout) if result.stdout.strip() else None
  print(json.dumps({"backup_transport_node":node,"tls_health_rc":result.returncode,"diagnostic":diagnostic},sort_keys=True))
PY'
      ;;
    --longhorn-uninstall-diagnostics)
      # Read the original namespace boundary after native removal, without sync.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json
from pathlib import Path
spec=importlib.util.spec_from_file_location("lab",Path("tests/helpers/longhorn-lab.py"))
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
state=lab.owner();namespace=lab.get("namespace","longhorn-system")
assert namespace and namespace["metadata"]["uid"]==state["namespace_uid"]
print(json.dumps({"namespace_uid":state["namespace_uid"],"native_uninstalled":state.get("native_uninstalled"),
 "longhorn_crds":[obj["metadata"]["name"] for obj in lab.get("crds")["items"] if obj["metadata"]["name"].endswith(".longhorn.io")]},sort_keys=True))
for kind in ("leases.coordination.k8s.io","pods","jobs","serviceaccounts","secrets","configmaps"):
 for obj in lab.get(kind,namespace="longhorn-system")["items"]:
  metadata={key:obj["metadata"].get(key) for key in ("name","uid","creationTimestamp","deletionTimestamp","ownerReferences")}
  print(json.dumps({"resource":kind,"metadata":metadata,
   "lease_spec":obj.get("spec") if kind=="leases.coordination.k8s.io" else None},sort_keys=True))
if (lab.DIRECTORY/"longhorn-backup-store.json").is_file():
 store=lab.load_helper("longhorn-backups.py").Store(lab);row=store.receipt()
 print(json.dumps({"backup_store_live":store.live(),"container_present":bool(store.inspect(row)),
  "product_binding_present":bool(row.get("product_binding")),"transport_present":bool(row.get("transport"))},sort_keys=True))
if state.get("host_state"):
 import hashlib
 journal=state["host_state"];paths=sorted(lab.host_files())
 assert set(journal["expected"])==set(paths) and set(journal["nodes"])==set(lab.NODES)
 probe="""import hashlib,json,pathlib,sys
rows={}
for name in json.load(sys.stdin):
 p=pathlib.Path(name)
 rows[name]={"symlink":p.is_symlink(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None}
print(json.dumps(rows))
"""
 for node in lab.NODES:
  current=json.loads(lab.remote(node,["python3","-c",probe],stdin=json.dumps(paths)).stdout)
  print(json.dumps({"host":node,"host_files":current,"journal_expected":journal["expected"],
   "original_files":journal["nodes"][node]["files"],
   "source_expected":{name:hashlib.sha256(data).hexdigest() for name,data in lab.host_files().items()}},sort_keys=True))
PY'
      ;;
    --longhorn-source-fingerprint)
      # Read the synchronized inputs of the active durable run; no source sync.
      ssh_longhorn 'cd /usr/local/kubeauto && .venv/bin/python - <<'"'"'PY'"'"'
import hashlib,json
from pathlib import Path
root=Path.cwd()
paths=set(root.glob("roles/longhorn/**/*"))
for pattern in ("tests/helpers/longhorn-*", "tests/helpers/validate_longhorn_matrix.py", "playbooks/*.yml"):
 paths.update(root.glob(pattern))
for name in ("common/constants.py", "conf/config.yml", "controller/cluster/cli.py", "service/cluster/manager.py",
             "service/cluster/downloader.py", "tests/run_enterprise_regression.sh"):
 paths.add(root/name)
print(json.dumps({str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(paths) if path.is_file() and "__pycache__" not in path.parts},sort_keys=True))
PY'
      ;;
    --longhorn-recurring-patch-diagnostics)
      # Retain the installed source; reproduce only the failed update admission.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json,subprocess
from pathlib import Path
root=Path("/usr/local/kubeauto");directory=root/"clusters/longhorn-gate"
receipt=directory/"longhorn-recurring-qualification.json"
if not receipt.is_file(): raise SystemExit("Original recurring qualification receipt is absent; no mutation attempted")
row=json.loads(receipt.read_text())
spec=importlib.util.spec_from_file_location("original_product",root/"roles/longhorn/files/longhorn.py")
product=importlib.util.module_from_spec(spec);spec.loader.exec_module(product)
api=product.API(str(directory/"kubectl.kubeconfig"))
current=api.get("recurringjobs.longhorn.io",row["name"],"longhorn-system")
if not current: raise SystemExit("Original recurring qualification object is absent; no mutation attempted")
assert current["metadata"]["uid"]==row["uid"]
print(json.dumps({"uid":current["metadata"]["uid"],"spec":current["spec"],
 "managedFields":current["metadata"].get("managedFields",[])},sort_keys=True),flush=True)
desired=dict(current["spec"],cron="0 1 1 1 *")
obj=product.resource("RecurringJob",row["name"],desired)
obj["metadata"].update(uid=row["uid"],resourceVersion=current["metadata"]["resourceVersion"])
argv=api.prefix+["apply","--server-side","--field-manager=kubeauto-longhorn","--dry-run=server","-f","-","-o","json"]
result=subprocess.run(argv,input=json.dumps(obj),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=45)
print("LONGHORN_RECURRING_UPDATE_DRY_RUN rc="+str(result.returncode)+" stderr="+result.stderr.strip(),flush=True)
# Test the proposed native PUT independently, preserving original metadata.
replacement={key:current[key] for key in ("apiVersion","kind","metadata")}
replacement["spec"]=desired
argv=api.prefix+["replace","--dry-run=server","-f","-","-o","json"]
put=subprocess.run(argv,input=json.dumps(replacement),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=45)
print("LONGHORN_RECURRING_REPLACE_DRY_RUN rc="+str(put.returncode)+" stderr="+put.stderr.strip(),flush=True)
if put.returncode==0:
 response=json.loads(put.stdout)
 assert response["metadata"]["uid"]==row["uid"] and response["spec"]["cron"]=="0 1 1 1 *"
 after=api.get("recurringjobs.longhorn.io",row["name"],"longhorn-system")
 assert after["metadata"]["uid"]==row["uid"] and after["spec"]==current["spec"]
 print("LONGHORN_RECURRING_REPLACE_DRY_RUN_VERIFIED persisted_change=false",flush=True)
raise SystemExit(put.returncode)
PY'
      ;;
    --longhorn-storage-diagnostics)
      ssh_longhorn 'set -u
export KUBECONFIG=/usr/local/kubeauto/clusters/longhorn-gate/kubectl.kubeconfig
export PATH=/usr/local/kubeauto/kube-bin:$PATH
test -f "$KUBECONFIG" || exit 1
kubectl --request-timeout=10s -n longhorn-system get pods,daemonsets,deployments -o wide
kubectl --request-timeout=10s -n longhorn-system get events --sort-by=.lastTimestamp
kubectl --request-timeout=10s -n longhorn-system get nodes.longhorn.io -o wide
kubectl --request-timeout=10s -n longhorn-system describe pods
kubectl --request-timeout=10s -n longhorn-system logs -l app=longhorn-manager --all-containers --prefix --tail=100 --max-log-requests=6
kubectl --request-timeout=10s -n longhorn-system logs -l app=longhorn-driver-deployer --all-containers --prefix --tail=100
for component in csi-attacher csi-provisioner csi-resizer csi-snapshotter longhorn-csi-plugin; do
  kubectl --request-timeout=10s -n longhorn-system logs -l app="$component" --all-containers --prefix --tail=60 --max-log-requests=6
done
for node in 192.168.122.135 192.168.122.212; do
  printf "LONGHORN_RUNTIME_DIAGNOSTIC node=%s\n" "$node"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 root@"$node" '\''df -h /var/lib/containerd /var/lib/kubelet; free -m; /usr/local/bin/crictl --runtime-endpoint unix:///run/containerd/containerd.sock images; journalctl -u containerd -u kubelet --no-pager -n 100'\''
done'
      ;;
    --longhorn-backup-host-probe)
      # Read-only prerequisite for the off-cluster backup failure domain.
      ssh_longhorn 'python3 - <<'"'"'PY'"'"'
import json,shutil,socket,subprocess
print(json.dumps({"host":"192.168.47.130","runtime_paths":{
 name:shutil.which(name) for name in ("docker","podman","nerdctl","skopeo")}},sort_keys=True))
for name in ("docker","podman","nerdctl"):
 path=shutil.which(name)
 if path:
  result=subprocess.run([path,"version"],capture_output=True,text=True,timeout=15)
  print(json.dumps({"runtime":name,"rc":result.returncode,"version":result.stdout[:2000]},sort_keys=True))
for port in (23521,23522):
 sock=socket.socket();sock.settimeout(2)
 print(json.dumps({"host":"127.0.0.1","port":port,"listening":sock.connect_ex(("127.0.0.1",port))==0}))
 sock.close()
PY'
      ;;
    --longhorn-node-patch-diagnostics)
      # Reproduce the original read/patch boundary with server dry-run only.
      # Original remote source/inputs remain intact; no reconcile or host work.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:/usr/local/kubeauto/extra-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json,os,subprocess
from pathlib import Path
root=Path("/usr/local/kubeauto");directory=root/"clusters/longhorn-gate"
spec=importlib.util.spec_from_file_location("longhorn_product",root/"roles/longhorn/files/longhorn.py")
product=importlib.util.module_from_spec(spec);spec.loader.exec_module(product)
config=directory/"yml/longhorn/input.json";c=json.loads(config.read_text())
product.validate(c);os.environ["KUBECONFIG"]=str(directory/"kubectl.kubeconfig")
api=product.API(os.environ["KUBECONFIG"])
nodes,release=product.preflight(c,api,str(root/"extra-bin/helm"),config.with_name("values.json"))
for address,node in nodes.items():
 selected=address in c["longhorn_storage_nodes"]
 desired={"metadata":{"resourceVersion":node["metadata"]["resourceVersion"],"labels":{
  "node.longhorn.io/create-default-disk":"config" if selected else "false",**product.OWNER}}}
 if selected:
  desired["metadata"]["annotations"]={"node.longhorn.io/default-disks-config":json.dumps([
   {"name":"kubeauto-data","path":c["longhorn_data_path"],"allowScheduling":True,
    "storageReserved":int(c["longhorn_disk_receipts"][address]["total_bytes"])//10}])}
 current=api.get("node",node["metadata"]["name"])
 argv=api.prefix+["patch","node",node["metadata"]["name"],"--type=merge","--patch-file=/dev/stdin","--dry-run=server","-o","name"]
 result=subprocess.run(argv,input=json.dumps(desired),text=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=45)
 print(json.dumps({"node":node["metadata"]["name"],"read_resource_version":node["metadata"]["resourceVersion"],
  "current_resource_version":current["metadata"]["resourceVersion"],"dry_run_rc":result.returncode,"stderr":result.stderr.strip()}),flush=True)
 if result.returncode: raise SystemExit(1)
PY'
      ;;
    --longhorn-preflight-diagnostics)
      # Invoke the exact installed read-only preflight, without rewriting its
      # inputs. Report only the failed argv/stack; never print API payloads.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:/usr/local/kubeauto/extra-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json,os,shlex,subprocess,traceback
from pathlib import Path
root=Path("/usr/local/kubeauto")
directory=root/"clusters/longhorn-gate"
config=directory/"yml/longhorn/input.json"
values=config.with_name("values.json")
spec=importlib.util.spec_from_file_location("longhorn_product",root/"roles/longhorn/files/longhorn.py")
product=importlib.util.module_from_spec(spec);spec.loader.exec_module(product)
run=product.run
def traced_run(argv,**kwargs):
 try: return run(argv,**kwargs)
 except Exception:
  print("LONGHORN_PREFLIGHT_FAILED_COMMAND "+shlex.join([str(x) for x in argv]),flush=True)
  if Path(argv[0]).name=="kubectl" and [str(x) for x in argv[-5:]]==["-A","get","deployments","-o","json"]:
   result=subprocess.run(argv,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=45)
   print("LONGHORN_PREFLIGHT_GET_ERROR "+result.stderr.strip(),flush=True)
  raise
product.run=traced_run
os.environ["KUBECONFIG"]=str(directory/"kubectl.kubeconfig")
try:
 c=json.loads(config.read_text());product.validate(c)
 product.preflight(c,product.API(os.environ["KUBECONFIG"]),str(root/"extra-bin/helm"),values)
except Exception:
 traceback.print_exc();raise SystemExit(1)
print("LONGHORN_PREFLIGHT_READ_ONLY_OK")
PY'
      ;;
    --longhorn-image-diagnostics)
      ssh_longhorn 'cd /usr/local/kubeauto && .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json
from pathlib import Path
spec=importlib.util.spec_from_file_location("longhorn_lab", "tests/helpers/longhorn-lab.py")
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
production,support=lab.publication_records()
for row in production+support:
 expected=row["platforms"]["linux/amd64"]
 local="docker://127.0.0.1:5000/"+("longhorn-lab/" if row in support else "")+row["key"]
 source="docker://"+row["talkedu_ref"].rsplit(":",1)[0]+"@"+expected
 def inspect(reference,config=False):
  command=["skopeo","inspect","--config" if config else "--raw"]
  if reference==local: command += ["--tls-verify=false"]
  return json.loads(lab.execute(command+[reference],timeout=120).stdout)
 actual,wanted=inspect(local),inspect(source)
 if lab.equivalent_manifest(actual,wanted):
  print("LONGHORN_IMAGE_CONTENT_EQUAL image="+row["key"],flush=True);continue
 actual_config,wanted_config=inspect(local,True),inspect(source,True)
 print(json.dumps({"image":row["key"],"expected_manifest":expected,
   "actual_manifest":actual,"published_manifest":wanted,
   "actual_architecture":actual_config.get("architecture"),"published_architecture":wanted_config.get("architecture"),
   "actual_diff_ids":actual_config.get("rootfs",{}).get("diff_ids"),
   "published_diff_ids":wanted_config.get("rootfs",{}).get("diff_ids")},sort_keys=True),flush=True)
PY'
      ;;
    --longhorn-backup-patch-diagnostics)
      # Reproduce JSON Patch admission only; no source sync or persisted write.
      ssh_longhorn 'cd /usr/local/kubeauto && PATH=/usr/local/kubeauto/kube-bin:$PATH .venv/bin/python - <<'"'"'PY'"'"'
import importlib.util,json,subprocess
from pathlib import Path
spec=importlib.util.spec_from_file_location("lab",Path("tests/helpers/longhorn-lab.py"))
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
state=lab.owner()
assert lab.get("namespace","longhorn-system")["metadata"]["uid"]==state["namespace_uid"]
before=lab.get("backuptargets.longhorn.io","default","longhorn-system")
assert not before["spec"].get("backupTargetURL") and not before["spec"].get("credentialSecret")
assert not any(lab.get(kind,namespace="longhorn-system")["items"] for kind in
 ("backups.longhorn.io","backupvolumes.longhorn.io","backupbackingimages.longhorn.io","systembackups.longhorn.io"))
for stale in (False,True):
 current=lab.get("backuptargets.longhorn.io","default","longhorn-system")
 assert current["metadata"]["uid"]==before["metadata"]["uid"] and current["spec"]==before["spec"]
 operations=[{"op":"test","path":"/metadata/uid","value":current["metadata"]["uid"]},
  {"op":"test","path":"/metadata/resourceVersion","value":"0" if stale else current["metadata"]["resourceVersion"]},
  {"op":"replace","path":"/spec","value":current["spec"]}]
 argv=[str(lab.BASE/"kube-bin/kubectl"),"--kubeconfig="+str(lab.DIRECTORY/"kubectl.kubeconfig"),
  "-n","longhorn-system","patch","backuptargets.longhorn.io","default","--type=json","--patch-file=/dev/stdin","--dry-run=server","-o","json"]
 result=subprocess.run(argv,input=json.dumps(operations),text=True,capture_output=True,timeout=30)
 generic="The request is invalid: the server rejected our request due to an error in our request"
 assert (result.returncode!=0 and result.stderr.strip()==generic) if stale else result.returncode==0
 after=lab.get("backuptargets.longhorn.io","default","longhorn-system")
 assert after["metadata"]["uid"]==before["metadata"]["uid"] and after["spec"]==before["spec"]
 print(json.dumps({"stale_resource_version":stale,"rc":result.returncode,"generic_patch_refusal":result.stderr.strip()==generic,
                   "persisted_configuration_unchanged":True},sort_keys=True))
PY'
      ;;
    --longhorn-command-error)
      # Classify only stderr files named by this run. Never emit raw stderr,
      # which may contain credentials or API patch input.
      ssh_longhorn 'python3 - <<'"'"'PY'"'"'
import hashlib,json,os,re,stat
from pathlib import Path
log=Path("/tmp/kubeauto-longhorn-live.log").read_text()
paths=re.findall(r"^LONGHORN_PRIVATE_DIAGNOSTIC file=(/usr/local/kubeauto/clusters/longhorn-gate/fixture-failure-[a-f0-9]{32}\.stderr)$",log,re.M)
if not paths: raise SystemExit("No current Longhorn command diagnostic was recorded")
patterns={"conflict":r"Conflict|object has been modified", "patch_test":r"test failed|test operation|testing value",
          "invalid_request":r"Invalid|invalid request|server rejected our request", "forbidden":r"Forbidden|forbidden",
          "connection":r"connection refused|deadline exceeded|timed out", "not_found":r"NotFound|not found"}
host_assertions=("unstable iSCSI service", "unsupported iSCSI load state", "unsupported iSCSI enable state",
 "symlink host fixture", "non-file host fixture", "foreign host file", "host journal file set changed",
 "original host file disappeared", "iSCSI session prevents service restore", "iSCSI active state not restored",
 "iSCSI enable state not restored", "host file changed during restore", "host file restoration differs")
for name in dict.fromkeys(paths):
 path=Path(name);info=path.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=1048576
 raw=path.read_bytes();content=raw.decode(errors="replace")
 print(json.dumps({"file":name,"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw),
                   "host_assertions":[value for value in host_assertions if "AssertionError: "+value in content.splitlines()],
                   "categories":{key:bool(re.search(value,content,re.I)) for key,value in patterns.items()}},sort_keys=True))
PY'
      ;;
    --longhorn-account-error)
      # Read only private account output named by this run's owned traceback.
      # Report fixed native codes/categories, never messages or signed URLs.
      ssh_longhorn 'python3 - <<'"'"'PY'"'"'
import hashlib,json,os,re,stat
from pathlib import Path
def private(path):
 info=path.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=1048576
 return path.read_text()
log=Path("/tmp/kubeauto-longhorn-live.log").read_text()
traces=re.findall(r"^LONGHORN_PRIVATE_DIAGNOSTIC file=(/var/tmp/kubeauto-longhorn-failure-[A-Za-z0-9_]+\.txt)$",log,re.M)
paths=[]
for name in dict.fromkeys(traces):
 paths.extend(re.findall(r"evidence=(/usr/local/kubeauto/clusters/longhorn-gate/longhorn-account-failure-[a-f0-9]{24}\.json)",private(Path(name))))
if not paths: raise SystemExit("No current private account diagnostic was recorded")
known={"AccessDenied","InvalidAccessKeyId","SignatureDoesNotMatch","InvalidRequest","InvalidArgument","NotImplemented","MissingContentLength","XAmzContentSHA256Mismatch","RequestTimeTooSkewed","NoSuchBucket","NoSuchKey","BadDigest","InvalidDigest"}
for name in dict.fromkeys(paths):
 raw=private(Path(name));row=json.loads(raw)
 content=row["stdout"]+"\n"+row["stderr"]
 codes=sorted(code for code in known if re.search(r"[\"\x27](?:Code|code)[\"\x27]\s*:\s*[\"\x27]"+code+r"[\"\x27]",content))
 try: response=json.loads(row["stdout"])
 except (TypeError,ValueError): response={}
 error=response.get("error",{}) if isinstance(response,dict) else {}
 cause=error.get("cause",{}) if isinstance(error,dict) else {}
 native=cause.get("error",{}) if isinstance(cause,dict) else {}
 target=native.get("Path","") if isinstance(native,dict) else ""
 owned_path=isinstance(target,str) and bool(re.fullmatch(r"https://127\.0\.0\.1:9000/longhorn-backup-[a-f0-9]{16}-denied/authorization-canary",target))
 mapped_permission=(owned_path and response.get("status")=="error" and error.get("type")=="fatal" and
  error.get("message")=="Unable to read from `lh/"+target.split("/",3)[3]+"`." and
  cause.get("message")=="Insufficient permissions to access this path `"+target+"`" and set(native)=={"Path"})
 print(json.dumps({"file":name,"sha256":hashlib.sha256(raw.encode()).hexdigest(),"rc":row["rc"],"native_codes":codes,
  "mapped_permission_exact_owned_target":bool(mapped_permission),
  "categories":{key:bool(re.search(pattern,content,re.I)) for key,pattern in {"streaming":r"streaming|chunked|trailer","signature":r"signature|sha256","authorization":r"access.?denied|permission","transport":r"connection refused|timed out|x509|certificate"}.items()}},sort_keys=True))
PY'
      ;;
    --longhorn-failure)
      # Read only the current run's Python traceback, never arbitrary private
      # fixture stderr, credentials or historical runs belonging to others.
      ssh_longhorn 'python3 - <<'"'"'PY'"'"'
import os,re,stat
from pathlib import Path
log=Path("/tmp/kubeauto-longhorn-live.log").read_text()
paths=re.findall(r"^LONGHORN_PRIVATE_DIAGNOSTIC file=(/var/tmp/kubeauto-longhorn-failure-[A-Za-z0-9_]+\.txt)$",log,re.M)
if not paths: raise SystemExit("No current Longhorn traceback was recorded")
for name in dict.fromkeys(paths):
 path=Path(name);info=path.lstat()
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600
 print(path.read_text(),end="")
PY'
      ;;
    --longhorn-host-inputs)
      ssh_longhorn 'set -eu
for node in 192.168.122.135 192.168.122.40 192.168.122.72 192.168.122.212 192.168.122.165 192.168.122.238; do
  printf "LONGHORN_HOST_INPUT node=%s\n" "$node"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 root@"$node" '\''for unit in kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd; do
    systemctl show "$unit" --property=FragmentPath,DropInPaths,ActiveState
    for directory in /etc/systemd/system/"$unit".service.d /run/systemd/system/"$unit".service.d /usr/lib/systemd/system/"$unit".service.d; do
      if test -d "$directory"; then find "$directory" -maxdepth 1 -type f -exec sha256sum {} +; fi
    done
  done
  for path in /data /data/kubeauto-longhorn /etc/kubeauto/longhorn-disk.json /etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf; do
    if test -L "$path"; then state=symlink; elif test -d "$path"; then state=directory; elif test -e "$path"; then state=present; else state=absent; fi
    printf "LONGHORN_DISK_PATH path=%s state=%s\n" "$path" "$state"
  done'\''
done'
      ;;
    --longhorn-diagnostics)
      # Read the running boundary without source sync, leases or remote writes.
      ssh_longhorn 'set -u
export KUBECONFIG=/usr/local/kubeauto/clusters/longhorn-gate/kubectl.kubeconfig
export PATH=/usr/local/kubeauto/kube-bin:$PATH
test -f "$KUBECONFIG" || exit 1
kubectl --request-timeout=10s get nodes -o wide
kubectl --request-timeout=10s get pods -A -o wide
kubectl --request-timeout=10s get events -A --sort-by=.lastTimestamp
kubectl --request-timeout=10s get servicecidrs -o wide
for node in 192.168.122.135 192.168.122.40 192.168.122.72; do
  printf "LONGHORN_CONTROL_INPUT node=%s\n" "$node"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 root@"$node" systemctl show kube-apiserver --property=ExecStart,FragmentPath,DropInPaths,MainPID,ActiveState
done
kubectl --request-timeout=10s -n kube-system describe pods -l k8s-app=calico-node
kubectl --request-timeout=10s -n kube-system logs -l k8s-app=calico-node --all-containers --prefix --tail=80 --max-log-requests=6'
      ;;
    --longhorn-status)
      remote_job_summary ssh_longhorn '' /tmp/kubeauto-longhorn-gate /tmp/kubeauto-longhorn-live.log
      matrix_counts longhorn
      ;;
    --longhorn-log) remote_log_tail ssh_longhorn '' /tmp/kubeauto-longhorn-live.log 80 ;;
    --longhorn-cancel-product)
      # Leave the durable wrapper and its EXIT recovery alive. Stop only the
      # foreground product subtree owned by this Longhorn runner.
      ssh_longhorn 'python3 - <<'"'"'PY'"'"'
import os,signal
from pathlib import Path
root=int(Path("/tmp/kubeauto-longhorn-gate.pid").read_text())
def command(pid): return Path("/proc") .joinpath(str(pid),"cmdline").read_bytes().replace(b"\0",b" ").decode()
assert "run-durable-gate.sh /tmp/kubeauto-longhorn-gate " in command(root)
parents={}
for entry in Path("/proc").iterdir():
 if entry.name.isdigit():
  try: parents[int(entry.name)]=int((entry/"stat").read_text().rsplit(")",1)[1].split()[1])
  except (OSError,ValueError): pass
owned={root}
while True:
 expanded=owned|{pid for pid,parent in parents.items() if parent in owned}
 if expanded==owned: break
 owned=expanded
def is_product(pid):
 try:
  argv=Path("/proc").joinpath(str(pid),"cmdline").read_bytes().split(b"\0")
 except FileNotFoundError:
  return False
 return (len(argv)==6 and argv[1:4]==[b"/usr/local/kubeauto/kubecli.py",b"setup",b"longhorn-gate"]
         and argv[4] in (b"07",b"09",b"90") and argv[5]==b"")
products=[pid for pid in owned if is_product(pid)]
assert len(products)==1, "no unique owned setup product process"
target=products[0];children={target}
while True:
 expanded=children|{pid for pid,parent in parents.items() if parent in children}
 if expanded==children: break
 children=expanded
for pid in sorted(children-{target},reverse=True):
 try: os.kill(pid,signal.SIGTERM)
 except ProcessLookupError: pass
os.kill(target,signal.SIGTERM)
print("LONGHORN_PRODUCT_CANCELLED runner_recovery=retained")
PY'
      ;;
    --longhorn-static-only) longhorn_local_gate static LONGHORN_STATIC_PASS ;;
    --longhorn-disabled) longhorn_local_gate disabled LONGHORN_DISABLED_PASS ;;
    --longhorn-artifacts) longhorn_local_gate artifacts LONGHORN_ARTIFACTS_PASS ;;
    --longhorn-recurring-declarations-qualification) longhorn_launch recurring-declarations-qualification LONGHORN_RECURRING_DECLARATIONS_QUALIFICATION_PASS ;;
    --longhorn-recurring) longhorn_launch recurring LONGHORN_RECURRING_PASS ;;
    --longhorn-uninstall) longhorn_launch uninstall LONGHORN_UNINSTALL_PASS ;;
    --longhorn-uninstall-backup-qualification) longhorn_launch uninstall-backup-qualification LONGHORN_UNINSTALL_BACKUP_QUALIFICATION_PASS ;;
    --longhorn-uninstall-refusal-qualification) longhorn_launch uninstall-refusal-qualification LONGHORN_UNINSTALL_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-recurring-rotation-qualification) longhorn_launch recurring-rotation-qualification LONGHORN_RECURRING_ROTATION_QUALIFICATION_PASS ;;
    --longhorn-recurring-backup-qualification) longhorn_launch recurring-backup-qualification LONGHORN_RECURRING_BACKUP_QUALIFICATION_PASS ;;
    --longhorn-recurring-selector-qualification) longhorn_launch recurring-selector-qualification LONGHORN_RECURRING_SELECTOR_QUALIFICATION_PASS ;;
    --longhorn-recurring-snapshot-qualification) longhorn_launch recurring-snapshot-qualification LONGHORN_RECURRING_SNAPSHOT_QUALIFICATION_PASS ;;
    --longhorn-backup-accounts-qualification) longhorn_launch backup-accounts-qualification LONGHORN_BACKUP_ACCOUNTS_QUALIFICATION_PASS ;;
    --longhorn-backup-store-qualification) longhorn_launch backup-store-qualification LONGHORN_BACKUP_STORE_QUALIFICATION_PASS ;;
    --longhorn-pool-probe) longhorn_launch pool-probe LONGHORN_POOL_PROBE_PASS ;;
    --longhorn-registry-qualification) longhorn_launch registry-qualification LONGHORN_REGISTRY_QUALIFICATION_PASS ;;
    --longhorn-registry-recover) longhorn_launch registry-recover LONGHORN_REGISTRY_RECOVERY_PASS ;;
    --longhorn-core-host-preflight) longhorn_launch core-host-preflight LONGHORN_CORE_HOST_PREFLIGHT_PASS ;;
    --longhorn-core-host-recover) longhorn_launch core-host-recover LONGHORN_CORE_HOST_RECOVER_PASS ;;
    --longhorn-core-smoke) longhorn_launch core-smoke LONGHORN_CORE_SMOKE_PASS ;;
    --longhorn-cache-qualification) longhorn_launch cache-qualification LONGHORN_CACHE_QUALIFICATION_PASS ;;
    --longhorn-preflight-qualification) longhorn_launch preflight-qualification LONGHORN_PREFLIGHT_QUALIFICATION_PASS ;;
    --longhorn-core) longhorn_launch core LONGHORN_CORE_PASS ;;
    --longhorn-disk-qualification) longhorn_launch disk-qualification LONGHORN_DISK_QUALIFICATION_PASS ;;
    --longhorn-ownership-request-refusal-qualification) longhorn_launch ownership-request-refusal-qualification LONGHORN_OWNERSHIP_REQUEST_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-ownership-controller-refusal-qualification) longhorn_launch ownership-controller-refusal-qualification LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-ownership-class-refusal-qualification) longhorn_launch ownership-class-refusal-qualification LONGHORN_OWNERSHIP_CLASS_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-ownership-interruption-qualification) longhorn_launch ownership-interruption-qualification LONGHORN_OWNERSHIP_INTERRUPTION_QUALIFICATION_PASS ;;
    --longhorn-ownership-preinstall-refusal-qualification) longhorn_launch ownership-preinstall-refusal-qualification LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-ownership-csi-refusal-qualification) longhorn_launch ownership-csi-refusal-qualification LONGHORN_OWNERSHIP_CSI_REFUSAL_QUALIFICATION_PASS ;;
    --longhorn-ownership-csi-probe) longhorn_launch ownership-csi-probe LONGHORN_OWNERSHIP_CSI_PROBE_PASS ;;
    --longhorn-maintenance) longhorn_launch maintenance LONGHORN_MAINTENANCE_PASS ;;
    --longhorn-maintenance-cleanup) longhorn_launch maintenance-cleanup LONGHORN_MAINTENANCE_CLEANUP_PASS ;;
    --longhorn-capacity) longhorn_launch capacity LONGHORN_CAPACITY_PASS ;;
    --longhorn-capacity-cleanup) longhorn_launch capacity-cleanup LONGHORN_CAPACITY_CLEANUP_PASS ;;
    --longhorn-integrity) longhorn_launch integrity LONGHORN_INTEGRITY_PASS ;;
    --longhorn-integrity-cleanup) longhorn_launch integrity-cleanup LONGHORN_INTEGRITY_CLEANUP_PASS ;;
    --longhorn-disk-lifecycle) longhorn_launch disk-lifecycle LONGHORN_DISK_LIFECYCLE_GATE_PASS ;;
    --longhorn-disk-lifecycle-cleanup) longhorn_launch disk-lifecycle-cleanup LONGHORN_DISK_LIFECYCLE_CLEANUP_PASS ;;
    --longhorn-install) longhorn_launch install LONGHORN_INSTALL_PASS ;;
    --longhorn-business-recover) longhorn_launch business-clean LONGHORN_BUSINESS_CLEAN_PASS ;;
    --longhorn-idempotence) longhorn_launch idempotence LONGHORN_IDEMPOTENCE_PASS ;;
    --longhorn-rwo) longhorn_launch rwo LONGHORN_RWO_PASS ;;
    --longhorn-rwx) longhorn_launch rwx LONGHORN_RWX_PASS ;;
    --longhorn-replica-scale) longhorn_launch replica-scale LONGHORN_REPLICA_SCALE_PASS ;;
    --longhorn-replica-failure) longhorn_launch replica-failure LONGHORN_REPLICA_FAILURE_PASS ;;
    --longhorn-performance) longhorn_launch performance LONGHORN_PERFORMANCE_PASS ;;
    --longhorn-clone) longhorn_launch clone LONGHORN_CLONE_PASS ;;
    --longhorn-expansion) longhorn_launch expansion LONGHORN_EXPANSION_PASS ;;
    --longhorn-snapshot) longhorn_launch snapshot LONGHORN_SNAPSHOT_PASS ;;
    --longhorn-backup) longhorn_launch backup LONGHORN_BACKUP_PASS ;;
    --longhorn-backup-refusal) longhorn_launch backup-refusal LONGHORN_BACKUP_REFUSAL_PASS ;;
    --longhorn-reclaim) longhorn_launch reclaim LONGHORN_RECLAIM_PASS ;;
    --longhorn-trim) longhorn_launch trim LONGHORN_TRIM_PASS ;;
    --longhorn-engine-failure) longhorn_launch engine-failure LONGHORN_ENGINE_FAILURE_PASS ;;
    --longhorn-encryption) longhorn_launch encryption LONGHORN_ENCRYPTION_PASS ;;
    --longhorn-encryption-block) longhorn_launch encryption-block LONGHORN_ENCRYPTION_BLOCK_PASS ;;
    --longhorn-encryption-backup) longhorn_launch encryption-backup LONGHORN_ENCRYPTION_BACKUP_PASS ;;
    --longhorn-network-policy) longhorn_launch network-policy LONGHORN_NETWORK_POLICY_PASS ;;
    --longhorn-security) longhorn_launch security LONGHORN_SECURITY_PASS ;;
    --longhorn-install-recover) longhorn_launch recover LONGHORN_RECOVER_PASS ;;
    *) echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=live-implementation-not-yet-qualified' >&2; return 1 ;;
  esac
}
