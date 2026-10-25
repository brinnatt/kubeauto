#!/usr/bin/env bash
# LONGHORN-52 retains a real old clone and verifies four isolated restores.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
SOURCE="$BASE/tests/helpers/longhorn-v2-install.py"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
TARGET="$BASE/tests/helpers/longhorn-v2-dr-target.py"
PREPARE="$BASE/tests/helpers/longhorn-v2-upgrade-prepare.py"
UPGRADE="$BASE/tests/helpers/longhorn-upgrade-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-v2-gate/longhorn-upgrade-lifecycle.json"
PREPARATION="$BASE/clusters/longhorn-v2-gate/longhorn-v2-upgrade-preparation.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-upgrade-lifecycle|v2-upgrade-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-upgrade.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_UPGRADE_DURABLE_WORK path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
source_pid= target_pid= phase=preflight business_attempted=no source_transport=no target_transport=no
cleanup_action=clean cleanup_marker=LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS
business_action=business restore_count=4
finish() {
  local rc=$? cleanup_rc=0 pid cluster
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    for cluster in longhorn-v2-gate longhorn-v2-dr-target; do
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get pods -A -o wide > "$work/$cluster-pods.txt" 2>&1 || true
      kubectl --kubeconfig="$BASE/clusters/$cluster/kubectl.kubeconfig" --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/$cluster-events.txt" 2>&1 || true
    done
  fi
  if [[ "$business_attempted" == yes && -f "$JOURNAL" && ! -L "$JOURNAL" ]]; then
    "$PY" "$UPGRADE" "$cleanup_action" --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q "^${cleanup_marker} " "$work/cleanup.log" || cleanup_rc=$?; fi
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
  if [[ "$rc" == 0 && "$cleanup_rc" == 0 && "$cleanup_action" != abort-clean ]]; then
    "$PY" "$PREPARE" finalize | tee "$work/finalize.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q '^LONGHORN_V2_UPGRADE_TRANSITION_SCOPED_CLEAN_PASS ' "$work/finalize.log" || cleanup_rc=$?; fi
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
    if [[ "$mode" == v2-upgrade-cleanup ]]; then
      echo LONGHORN_V2_UPGRADE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_V2_UPGRADE_CLEANUP_PASS
    else
      echo LONGHORN_V2_UPGRADE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_V2_UPGRADE_GATE_PASS
    fi
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$CORE" verify-transport
if [[ "$mode" == v2-upgrade-cleanup ]]; then
  [[ -f "$PREPARATION" && ! -L "$PREPARATION" ]] || { echo LONGHORN_V2_UPGRADE_RECOVERY_REQUIRES_OWNED_PREPARATION >&2; exit 1; }
  "$PY" "$UPGRADE" recovery-check --source-relay-pid 1 --target-relay-pid 1 | tee "$work/recovery-check.log"
  if grep -q '^LONGHORN_V2_UPGRADE_ABORT_PREFLIGHT_PASS ' "$work/recovery-check.log"; then
    cleanup_action=abort-clean
    cleanup_marker=LONGHORN_V2_UPGRADE_ABORT_SCOPED_CLEAN_PASS
  fi
else
  if [[ -e "$PREPARATION" || -L "$PREPARATION" ]]; then
    [[ -f "$PREPARATION" && ! -L "$PREPARATION" ]] || exit 1
    "$PY" "$PREPARE" resume-preflight | tee "$work/preflight.log"
    grep -Fxq LONGHORN_V2_UPGRADE_RETIREMENT_RESUME_PREFLIGHT_PASS "$work/preflight.log"
    if grep -Fxq LONGHORN_V2_UPGRADE_POST_ROLLOUT_RESUME_READY "$work/preflight.log"; then
      business_action=resume restore_count=2
    fi
  else
    "$PY" "$PREPARE" preflight | tee "$work/preflight.log"
    grep -Fxq LONGHORN_V2_UPGRADE_TRANSITION_PREFLIGHT_PASS "$work/preflight.log"
  fi
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
if [[ "$mode" == v2-upgrade-cleanup ]]; then business_attempted=yes; phase=owned-upgrade-recovery; exit 0; fi
phase=empty-store-transition-to-old
if [[ "$business_action" == business ]]; then
  "$PY" "$PREPARE" prepare --source-relay-pid "$source_pid" | tee "$work/prepare.log"
  grep -Fxq LONGHORN_V2_UPGRADE_TRANSITION_OLD_READY "$work/prepare.log"
fi
phase=manager-engine-upgrade-and-isolated-recovery
business_attempted=yes
"$PY" "$UPGRADE" "$business_action" --source-relay-pid "$source_pid" --target-relay-pid "$target_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_V2_UPGRADE_PASS "$work/business.log"
grep -q '^LONGHORN_V2_UPGRADE_LEGACY_VERIFIED ' "$work/business.log"
grep -Fxq 'LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true' "$work/business.log"
grep -q '^LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS ' "$work/business.log"
[[ "$(grep -c '^LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED ' "$work/business.log")" == "$restore_count" ]]
