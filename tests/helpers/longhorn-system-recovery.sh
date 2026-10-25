#!/usr/bin/env bash
# Independent V1 system restore; no DR activation or HA claim.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
SOURCE="$BASE/tests/helpers/longhorn-lab.py"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-dr-target-install.py"
DRIVER="$BASE/tests/helpers/longhorn-system-recovery.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-system-recovery.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in system-restore|system-restore-cleanup|system-restore-prepared-recovery) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-system-recovery.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_SYSTEM_RECOVERY_DURABLE_WORK path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
source_pid= target_pid= source_transport=no target_transport=no attempted=no phase=preflight
finish() {
  local rc=$? cleanup_rc=0 pid cluster
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    for cluster in longhorn-gate longhorn-v2-dr-target; do
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get pods -A -o wide > "$work/$cluster-pods.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/$cluster-events.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s -n longhorn-system get systemrestores.longhorn.io,volumes.longhorn.io -o yaml > "$work/$cluster-restore.yaml" 2>&1 || true
    done
  fi
  if [[ "$attempted" == yes && -f "$JOURNAL" && ! -L "$JOURNAL" ]]; then
    "$PY" "$DRIVER" clean --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q '^LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS ' "$work/cleanup.log" || cleanup_rc=$?; fi
  fi
  for pid in "$source_pid" "$target_pid"; do
    [[ -n "$pid" ]] || continue
    if [[ "$rc" == 0 ]] && ! kill -0 "$pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" || { [[ "$rc" != 0 ]] || rc=1; }
  done
  if [[ "$source_transport" == yes ]]; then "$PY" "$SOURCE" relay-clean || cleanup_rc=$?; fi
  if [[ "$target_transport" == yes ]]; then "$PY" "$TARGET" relay-clean || cleanup_rc=$?; fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" == 0 && "$attempted" == yes ]]; then
    "$PY" "$SOURCE" verify-core || cleanup_rc=$?
    "$PY" "$TARGET" core-identity || cleanup_rc=$?
    "$PY" "$SOURCE" health || cleanup_rc=$?
    "$PY" "$INSTALL" health || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc recovery_material=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == system-restore-cleanup ]]; then
      echo LONGHORN_SYSTEM_RESTORE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_SYSTEM_RESTORE_CLEANUP_PASS
    else
      echo LONGHORN_SYSTEM_RESTORE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_SYSTEM_RESTORE_GATE_PASS
    fi
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$SOURCE" verify-transport
if [[ "$mode" == system-restore-cleanup || "$mode" == system-restore-prepared-recovery ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo LONGHORN_SYSTEM_RECOVERY_REQUIRES_OWNED_JOURNAL >&2; exit 1; }
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
fi
phase=transport
source_transport=yes
"$PY" "$SOURCE" relay-ensure
"$PY" "$SOURCE" relay-maintain > "$work/source-relay.log" 2>&1 &
source_pid=$!
target_transport=yes
"$PY" "$TARGET" relay-ensure
"$PY" "$TARGET" relay-maintain > "$work/target-relay.log" 2>&1 &
target_pid=$!
attempted=yes
if [[ "$mode" == system-restore-cleanup ]]; then phase=owned-recovery; exit 0; fi
phase=independent-system-restore
action=business
if [[ "$mode" == system-restore-prepared-recovery ]]; then action=resume-prepared; fi
"$PY" "$DRIVER" "$action" --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_SYSTEM_RESTORE_PASS "$work/business.log"
grep -q '^LONGHORN_SYSTEM_RESTORE_ATTACHED_REFUSAL_PASS ' "$work/business.log"
grep -q '^LONGHORN_SYSTEM_RESTORE_INDEPENDENT_OBSERVED ' "$work/business.log"
grep -q '^LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS ' "$work/business.log"
