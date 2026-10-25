#!/usr/bin/env bash
# LONGHORN-35/36 share one old->new chain and four isolated restores.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
SOURCE="$BASE/tests/helpers/longhorn-lab.py"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
PREPARE="$BASE/tests/helpers/longhorn-v1-upgrade-prepare.py"
UPGRADE="$BASE/tests/helpers/longhorn-v1-upgrade-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-v1-upgrade-lifecycle.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v1-upgrade-lifecycle|v1-upgrade-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v1-upgrade.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V1_UPGRADE_DURABLE_WORK path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
source_pid= target_pid= phase=preflight business_attempted=no source_transport=no target_transport=no
finish() {
  local rc=$? cleanup_rc=0 pid cluster
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    for cluster in longhorn-gate longhorn-v2-dr-target; do
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get pods -A -o wide > "$work/$cluster-pods.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/$cluster-events.txt" 2>&1 || true
    done
  fi
  if [[ "$business_attempted" == yes && -f "$JOURNAL" && ! -L "$JOURNAL" ]]; then
    "$PY" "$UPGRADE" clean --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q '^LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS ' "$work/cleanup.log" || cleanup_rc=$?; fi
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
  if [[ "$rc" == 0 && "$cleanup_rc" == 0 ]]; then
    "$PY" "$PREPARE" finalize | tee "$work/finalize.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q '^LONGHORN_V1_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS ' "$work/finalize.log" || cleanup_rc=$?; fi
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == v1-upgrade-cleanup ]]; then
      echo LONGHORN_V1_UPGRADE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_V1_UPGRADE_CLEANUP_PASS
    else
      echo LONGHORN_V1_UPGRADE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_V1_UPGRADE_GATE_PASS
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
if [[ "$mode" == v1-upgrade-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo LONGHORN_V1_UPGRADE_RECOVERY_REQUIRES_OWNED_JOURNAL >&2; exit 1; }
else
  "$PY" "$PREPARE" preflight | tee "$work/preflight.log"
  grep -Fxq LONGHORN_V1_UPGRADE_TRANSITION_PREFLIGHT_PASS "$work/preflight.log"
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
if [[ "$mode" == v1-upgrade-cleanup ]]; then business_attempted=yes; phase=owned-upgrade-recovery; exit 0; fi
phase=empty-store-transition-to-old
"$PY" "$PREPARE" prepare --source-relay-pid "$source_pid" | tee "$work/prepare.log"
grep -Fxq LONGHORN_V1_UPGRADE_TRANSITION_OLD_READY "$work/prepare.log"
phase=manager-engine-upgrade-and-isolated-recovery
business_attempted=yes
"$PY" "$UPGRADE" business --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_UPGRADE_PASS "$work/business.log"
grep -Fxq LONGHORN_UPGRADE_RECOVERY_PASS "$work/business.log"
grep -q '^LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS ' "$work/business.log"
restores="$(grep -c '^LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED ' "$work/business.log")"
if [[ "$restores" == 2 ]]; then
  [[ "$(grep -Fxc 'LONGHORN_V1_UPGRADE_BEFORE_RESTORES_REVALIDATED count=2 original_points=true original_manager_rollout=true' "$work/business.log")" == 1 ]]
  [[ "$(grep -c '^LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED .*"point": "plain-after"' "$work/business.log")" == 1 ]]
  [[ "$(grep -c '^LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED .*"point": "encrypted-after"' "$work/business.log")" == 1 ]]
else
  [[ "$restores" == 4 ]]
fi
