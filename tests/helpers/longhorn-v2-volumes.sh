#!/usr/bin/env bash
# Qualify V2 volume operations on the owned healthy AIO installation.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-manager-loss-cleanup|v2-manager-loss-qualification|v2-interrupt-cleanup|v2-interrupt-qualification|v2-replica-im-loss-cleanup|v2-replica-im-loss-qualification|v2-full-copy-lifecycle-qualification|v2-full-copy-clone-qualification|v2-integrity-qualification|v2-snapshot-qualification|v2-volumes|v2-volume-modes-qualification|v2-expansion-qualification|v2-reclaim-qualification|v2-encryption-qualification|v2-trim-qualification) ;; *) exit 2;; esac
if [[ "$mode" == v2-volumes ]]; then
  for branch in volume-modes expansion reclaim encryption trim; do
    bash "$BASE/tests/helpers/longhorn-v2-volumes.sh" "v2-$branch-qualification"
  done
  echo LONGHORN_V2_VOLUMES_SCOPED_CLEAN_PASS
  echo LONGHORN_V2_VOLUMES_PASS
  exit 0
fi
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
if [[ "$mode" == v2-integrity-qualification ]]; then
  "$PY" "$BASE/tests/helpers/longhorn-manager-adopt.py" --pending
fi
if [[ "$mode" != v2-manager-loss-cleanup && "$mode" != v2-replica-im-loss-cleanup && "$mode" != v2-interrupt-cleanup ]]; then "$PY" "$INSTALL" volume-baseline; fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-volumes.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
volumes_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ "$mode" == v2-manager-loss-qualification || "$mode" == v2-manager-loss-cleanup ]]; then
    "$PY" "$BASE/tests/helpers/longhorn-v2-manager-loss.py" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  elif [[ "$mode" == v2-interrupt-qualification || "$mode" == v2-interrupt-cleanup ]]; then
    "$PY" "$BASE/tests/helpers/longhorn-v2-interrupt.py" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  elif [[ "$mode" == v2-replica-im-loss-qualification || "$mode" == v2-replica-im-loss-cleanup ]]; then
    "$PY" "$BASE/tests/helpers/longhorn-v2-control-faults.py" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  else
    "$PY" "$INSTALL" business-clean --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" == 0 ]]; then
    "$PY" "$INSTALL" volume-baseline || cleanup_rc=$?
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$CORE" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap volumes_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-manager-loss-cleanup ]]; then phase=owned-v2-manager-recovery; exit 0; fi
if [[ "$mode" == v2-manager-loss-qualification ]]; then
  phase=v2-manager-pod-loss
  "$PY" "$BASE/tests/helpers/longhorn-v2-manager-loss.py" business --relay-pid "$relay_pid" | tee "$work/manager.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-manager-loss.py" verify --log "$work/manager.log"
  exit 0
fi
if [[ "$mode" == v2-interrupt-cleanup ]]; then phase=owned-interrupt-profile-recovery; exit 0; fi
if [[ "$mode" == v2-interrupt-qualification ]]; then
  phase=v2-interrupt-roundtrip
  "$PY" "$BASE/tests/helpers/longhorn-v2-interrupt.py" business --relay-pid "$relay_pid" | tee "$work/interrupt.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-interrupt.py" verify --log "$work/interrupt.log"
  exit 0
fi
if [[ "$mode" == v2-replica-im-loss-cleanup ]]; then phase=owned-v2-replica-im-fault-recovery; exit 0; fi
if [[ "$mode" == v2-replica-im-loss-qualification ]]; then
  phase=remote-v2-replica-instance-manager-pod-loss
  "$PY" "$BASE/tests/helpers/longhorn-v2-control-faults.py" business --relay-pid "$relay_pid" | tee "$work/control.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-control-faults.py" verify --log "$work/control.log"
  exit 0
fi
if [[ "$mode" == v2-full-copy-lifecycle-qualification ]]; then
  phase=full-copy-source-retirement
  "$PY" "$BASE/tests/helpers/longhorn-v2-clones.py" lifecycle-business --relay-pid "$relay_pid" | tee "$work/lifecycle.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-clones.py" lifecycle-verify --log "$work/lifecycle.log"
  exit 0
fi
if [[ "$mode" == v2-full-copy-clone-qualification ]]; then
  phase=full-copy-clone
  "$PY" "$BASE/tests/helpers/longhorn-v2-clones.py" business --relay-pid "$relay_pid" | tee "$work/clone.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-clones.py" verify --log "$work/clone.log"
  exit 0
fi
if [[ "$mode" == v2-integrity-qualification ]]; then
  phase=integrity
  "$PY" "$BASE/tests/helpers/longhorn-v2-integrity.py" business --relay-pid "$relay_pid" | tee "$work/integrity.log"
  "$PY" "$BASE/tests/helpers/longhorn-v2-integrity.py" verify --log "$work/integrity.log"
  exit 0
fi
if [[ "$mode" == v2-snapshot-qualification ]]; then
  phase=snapshot
  "$PY" "$INSTALL" business-snapshot --relay-pid "$relay_pid" | tee "$work/snapshot.log"
  "$PY" - "$work/snapshot.log" <<'PY'
import re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text()
rows = re.findall(r'^LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type=(\S+) snapshot_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64})$', log, re.M)
assert len(rows) == 2 and {r[0] for r in rows} == {'csi', 'native'}
assert len({r[1] for r in rows}) == len({r[2] for r in rows}) == 1 and rows[0][1] != rows[0][2]
remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
assert len(remounts) == 1 and int(remounts[0][0]) > 0 and remounts[0][1] == rows[0][2]
assert len(set(remounts[0][2].split(','))) == 3
PY
  [[ "$(grep -Fxc LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED "$work/snapshot.log")" == 1 ]]
  grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/snapshot.log"
  grep -Fxq LONGHORN_SNAPSHOT_BUSINESS_PASS "$work/snapshot.log"
  exit 0
fi
if [[ "$mode" == v2-trim-qualification ]]; then
  phase=trim
  "$PY" "$INSTALL" business-trim --relay-pid "$relay_pid" | tee "$work/trim.log"
  "$PY" - "$work/trim.log" <<'PY'
import json, re, sys
from pathlib import Path
prefix = 'LONGHORN_V2_TRIM_RESULT '
log = Path(sys.argv[1]).read_text()
rows = [json.loads(line[len(prefix):]) for line in log.splitlines() if line.startswith(prefix)]
assert len(rows) == 1
r = rows[0]
assert r['filler_bytes'] == 64*1024**2 and r['guest_freed_bytes'] >= 32*1024**2
assert len(r['replicas']) == 3 and set(r['baseline']) == set(r['retained']) == set(r['after']) == set(r['replicas'])
assert all(r['retained'][name] - r['baseline'][name] >= 32*1024**2 and
           r['retained'][name] - r['after'][name] >= 32*1024**2 for name in r['replicas'])
assert r['snapshot']['uid'] and r['snapshot']['name'].startswith('trim-')
assert all(row['uid'] and row['node'] for row in r['replicas'].values())
assert len({row['node'] for row in r['replicas'].values()}) == 3
assert all(re.fullmatch('[a-f0-9]{64}', r[key]) for key in ('confirmed_sha256', 'filler_sha256'))
remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
assert len(remounts) == 1 and remounts[0][0] == r['confirmed_sha256']
assert set(remounts[0][1].split(',')) == {row['node'] for row in r['replicas'].values()}
PY
  grep -Eq '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]{64} replica_nodes=' "$work/trim.log"
  grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/trim.log"
  grep -Fxq LONGHORN_TRIM_BUSINESS_PASS "$work/trim.log"
  exit 0
fi
if [[ "$mode" == v2-encryption-qualification ]]; then
  phase=encryption
  "$PY" "$INSTALL" business-encryption --relay-pid "$relay_pid" | tee "$work/encryption.log"
  "$PY" - "$work/encryption.log" <<'PY'
import json, sys
from pathlib import Path
prefix = 'LONGHORN_ENCRYPTION_REFUSAL_VERIFIED '
rows = [json.loads(line[len(prefix):]) for line in Path(sys.argv[1]).read_text().splitlines() if line.startswith(prefix)]
assert len(rows) == 4
assert {(r['mode'], r['variant']) for r in rows} == {(mode, variant) for mode in ('Filesystem', 'Block') for variant in ('wrong', 'missing')}
assert all(r['key_restored'] is True and r['mapping_closed_before_refusal'] is True for r in rows)
PY
  [[ "$(grep -c '^LONGHORN_ENCRYPTION_ORIGINAL_KEY_RESTORED variant=' "$work/encryption.log")" == 4 ]]
  [[ "$(grep -c '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]\{64\} replica_nodes=' "$work/encryption.log")" == 2 ]]
  [[ "$(grep -Fxc LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/encryption.log")" == 2 ]]
  grep -Fxq LONGHORN_V2_ENCRYPTION_BUSINESS_PASS "$work/encryption.log"
  exit 0
fi
if [[ "$mode" == v2-reclaim-qualification ]]; then
  phase=reclaim
  "$PY" "$INSTALL" business-reclaim --relay-pid "$relay_pid" | tee "$work/reclaim.log"
  grep -Eq '^LONGHORN_RETAIN_REBIND_VERIFIED pv=.+ sha256=[a-f0-9]{64}$' "$work/reclaim.log"
  grep -Eq '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]{64} replica_nodes=' "$work/reclaim.log"
  [[ "$(grep -Fxc LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/reclaim.log")" == 2 ]]
  [[ "$(grep -Fxc LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED "$work/reclaim.log")" == 2 ]]
  grep -Fxq LONGHORN_RECLAIM_BUSINESS_PASS "$work/reclaim.log"
  exit 0
fi
if [[ "$mode" == v2-expansion-qualification ]]; then
  phase=expansion
  "$PY" "$INSTALL" business-expansion --relay-pid "$relay_pid" | tee "$work/expansion.log"
  for variant in 'mode=Filesystem encrypted=false' 'mode=Block encrypted=false' 'mode=Filesystem encrypted=true'; do
    grep -Fq "LONGHORN_ONLINE_EXPANSION_VERIFIED $variant measurement=" "$work/expansion.log"
  done
  grep -Eq '^LONGHORN_V2_ENCRYPTED_EXPANSION_CAPACITY_VERIFIED .*"mapped_bytes": 4294967296.*"raw_bytes": 4311744512' "$work/expansion.log"
  [[ "$(grep -c '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]\{64\} replica_nodes=' "$work/expansion.log")" == 3 ]]
  [[ "$(grep -Fxc LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/expansion.log")" == 3 ]]
  grep -Fxq LONGHORN_EXPANSION_BUSINESS_PASS "$work/expansion.log"
  exit 0
fi
phase=rwo-rwop-block
"$PY" "$INSTALL" business-rwo --relay-pid "$relay_pid" | tee "$work/rwo.log"
for variant in 'access=ReadWriteOnce mode=Filesystem' 'access=ReadWriteOncePod mode=Filesystem' 'access=ReadWriteOnce mode=Block'; do
  grep -Fxq "LONGHORN_EXCLUSIVE_ATTACHMENT_VERIFIED $variant" "$work/rwo.log"
done
[[ "$(grep -c '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]\{64\} replica_nodes=' "$work/rwo.log")" == 3 ]]
[[ "$(grep -Fxc LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/rwo.log")" == 3 ]]
grep -Fxq LONGHORN_RWO_BUSINESS_PASS "$work/rwo.log"
"$PY" "$INSTALL" volume-baseline
phase=rwx
"$PY" "$INSTALL" business-rwx --relay-pid "$relay_pid" | tee "$work/rwx.log"
grep -Fxq 'LONGHORN_RWX_LOCKS_VERIFIED exclusion=two-clients release=verified' "$work/rwx.log"
grep -Eq '^LONGHORN_RWX_RECOVERY_VERIFIED old_uid=.+ new_uid=.+ share_manager_ready_s=' "$work/rwx.log"
[[ "$(grep -c '^LONGHORN_RWX_CLIENT_RESULT ' "$work/rwx.log")" == 2 ]]
grep -Eq '^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256=[a-f0-9]{64} replica_nodes=' "$work/rwx.log"
grep -Fxq LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS "$work/rwx.log"
grep -Fxq LONGHORN_RWX_BUSINESS_PASS "$work/rwx.log"
