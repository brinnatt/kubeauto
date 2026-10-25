#!/usr/bin/env bash
# Narrow missing-block refusal/recovery; never discard an unresolved escrow.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
BACKUPS="$BASE/tests/helpers/longhorn-v2-backups.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-broken-restore.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-broken-restore-qualification|v2-broken-restore-cleanup) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
baseline=
if [[ "$mode" == v2-broken-restore-qualification ]]; then
  journal="$BASE/clusters/longhorn-v2-gate/longhorn-v2-broken-restore.json"
  if [[ -e "$journal" || -L "$journal" ]]; then
    echo 'LONGHORN_PREFLIGHT_FAIL class=test-gate reason=broken-restore-journal-needs-owned-cleanup' >&2
    exit 1
  fi
  "$PY" "$BACKUPS" qualification-current
  baseline="$("$PY" "$BACKUPS" baseline)"
  printf '%s\n' "$baseline"
fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-broken-restore.XXXXXX)"
chmod 0700 "$work"
phase=relay
relay_pid=
restore_exit() {
  local rc=$? cleanup_rc=0 current
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    kubectl --request-timeout=5s -n longhorn-system get volumes.longhorn.io,engines.longhorn.io,replicas.longhorn.io -o json > "$work/restore-state.json" 2>&1 || true
  fi
  # Failed fault recovery blocks all business/store deletion. Preserve both
  # escrow and its credentials/transport when the outcome is unknown.
  "$PY" "$DRIVER" clean || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$BACKUPS" cleanup || cleanup_rc=$?; fi
  if [[ "$cleanup_rc" == 0 ]]; then
    current="$("$PY" "$BACKUPS" baseline)" || cleanup_rc=$?
    printf '%s\n' "$current"
    if [[ -n "$baseline" && "$cleanup_rc" == 0 ]]; then
      [[ "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$baseline")" == \
         "$(grep '^LONGHORN_V2_BACKUP_BASELINE_VERIFIED ' <<<"$current")" ]] || cleanup_rc=1
    fi
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
trap restore_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-broken-restore-cleanup ]]; then phase=owned-fault-recovery; exit 0; fi
phase=broken-chain-business
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
for required in LONGHORN_V2_BROKEN_RESTORE_BUSINESS_PASS LONGHORN_V2_BROKEN_RESTORE_FAULT_CLEAN_PASS \
    LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS LONGHORN_BACKUP_STORE_CLEAN_PASS; do
  grep -Fxq "$required" "$work/business.log"
done
"$PY" - "$work/business.log" <<'PY'
import json, re, sys
from pathlib import Path
lines = Path(sys.argv[1]).read_text().splitlines()
rows = [json.loads(x.split(' ', 1)[1]) for x in lines if x.startswith('LONGHORN_V2_BROKEN_CHAIN_NATIVE_REFUSED ')]
isolated = [json.loads(x.split(' ', 1)[1]) for x in lines if x.startswith('LONGHORN_V2_RESTORE_BLOCK_ISOLATED ')]
assert len(rows) == len(isolated) == 1
r, fault = rows[0], isolated[0]
assert r['block'] == fault['block'] and r['backup'] == fault['backup']
assert r['volume_uid'] and r['engine_uid'] and r['backup_uid']
assert r['restore_required'] is True and r['block_absent'] is True and r['last_restored_backup_matches'] is False
assert re.fullmatch('[a-f0-9]{64}', r['error_sha256'])
restores = [x for x in lines if x.startswith('LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED ')]
assert len(restores) == 2 and 'mode=full ' in restores[0] and 'mode=incremental ' in restores[1]
assert 'LONGHORN_FSYNC_REMOUNT_VERIFIED ' in '\n'.join(lines)
assert not any(re.match(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', x) for x in lines)
PY
