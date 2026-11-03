#!/usr/bin/env bash
# Complete LONGHORN-48 using its independently qualified product workflows.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
[[ "${1:-}" == v2-data-protection ]] || exit 2
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
source_boundary() { "$PY" "$BASE/tests/helpers/longhorn-v2-dr-target.py" source-boundary; }
target_boundary() { "$PY" "$BASE/tests/helpers/longhorn-v2-dr-store.py" target-baseline; }
boundary_marker() {
  local label="$1" text="$2"
  [[ "$(grep -Ec "^$label sha256=[a-f0-9]{64}$" <<<"$text")" == 1 ]] || return 1
  grep -E "^$label sha256=[a-f0-9]{64}$" <<<"$text"
}
source_before="$(source_boundary)"
target_before="$(target_boundary)"
printf '%s\n%s\n' "$source_before" "$target_before"
source_identity="$(boundary_marker LONGHORN_V2_DR_SOURCE_BOUNDARY "$source_before")"
target_identity="$(boundary_marker LONGHORN_V2_DR_TARGET_STORE_BASELINE "$target_before")"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-data-protection.XXXXXX)"
chmod 0700 "$work"
phase=preflight
module_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    # Each child owns its fixtures and recovery. Do not run another child's
    # cleanup or repeat a failed scenario merely to collect secondary failures.
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  else
    rm -rf -- "$work"
    echo LONGHORN_V2_DATA_PROTECTION_SCOPED_CLEAN_PASS
    echo LONGHORN_V2_DATA_PROTECTION_PASS
  fi
  exit "$rc"
}
trap module_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# The changed negative restore path runs first. Integrity includes native/CSI
# snapshot restoration. SystemBackup includes
# both full/incremental independent backup restores. Failback includes shared
# TLS, same-volume two-point standby, missing-fence refusal and activation.
while read -r helper branch; do
  phase="$branch"
  marker="LONGHORN_${branch^^}";marker="${marker//-/_}"
  echo "LONGHORN_V2_DATA_PROTECTION_STAGE_START stage=$branch"
  reused=3
  if [[ "${LONGHORN_FULL_RESUME:-no}" == yes && -n "${LONGHORN_FULL_EVIDENCE_DIR:-}" ]]; then
    reused=0
    "$PY" "$BASE/tests/helpers/longhorn-full-evidence.py" resume-protection "$branch" > "$work/$branch.log" 2>&1 || reused=$?
    if [[ "$reused" == 0 ]]; then
      cat "$work/$branch.log"
    elif [[ "$reused" != 3 ]]; then
      cat "$work/$branch.log" >&2
      exit "$reused"
    fi
  fi
  if [[ "$reused" == 3 ]]; then
    bash "$BASE/tests/helpers/$helper.sh" "$branch" 2>&1 | tee "$work/$branch.log"
  fi
  grep -Fxq "${marker}_SCOPED_CLEAN_PASS" "$work/$branch.log"
  grep -Fxq "${marker}_PASS" "$work/$branch.log"
  if grep -Eq '^LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)' "$work/$branch.log"; then exit 1; fi
  echo "LONGHORN_V2_DATA_PROTECTION_STAGE_PASS stage=$branch"
done <<'STAGES'
longhorn-v2-broken-restore v2-broken-restore-qualification
longhorn-v2-volumes v2-integrity-qualification
longhorn-v2-backups v2-backup-refusal-qualification
longhorn-v2-recurring v2-recurring-selector-qualification
longhorn-v2-recurring v2-recurring-snapshot-qualification
longhorn-v2-recurring v2-recurring-backup-qualification
longhorn-v2-recurring v2-recurring-rotation-qualification
longhorn-v2-detached-backup v2-detached-backup-qualification
longhorn-v2-systembackup v2-systembackup-qualification
longhorn-v2-systemrestore v2-systemrestore-qualification
longhorn-v2-dr-failover v2-dr-failback-qualification
STAGES
phase=final-baselines
source_after="$(source_boundary)"
target_after="$(target_boundary)"
printf '%s\n%s\n' "$source_after" "$target_after"
[[ "$source_identity" == "$(boundary_marker LONGHORN_V2_DR_SOURCE_BOUNDARY "$source_after")" ]]
[[ "$target_identity" == "$(boundary_marker LONGHORN_V2_DR_TARGET_STORE_BASELINE "$target_after")" ]]
