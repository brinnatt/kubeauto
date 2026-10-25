#!/usr/bin/env bash
# LONGHORN-37 owns database consumers; monitor cleanup depends on their recovery.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
MONITOR="$BASE/tests/helpers/longhorn-v1-observability.py"
DRIVER="$BASE/tests/helpers/longhorn-v1-middleware.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-middleware}"
case "$mode" in middleware|middleware-business|middleware-cleanup|middleware-reentry-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v1-middleware.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_MIDDLEWARE_DURABLE_DIRECTORY path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
relay_pid= transport=no database_active=no monitor_active=no phase=preflight
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$database_active" == yes ]]; then
      "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" 2>&1 | tee "$work/database-cleanup.log" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        [[ "$(grep -Fxc LONGHORN_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS "$work/database-cleanup.log")" == 1 ]] || cleanup_rc=1
      fi
    fi
    # Preserve monitoring when database recovery is incomplete. Cleanup-only
    # executes recovery before requiring an empty consumer baseline.
    if [[ "$cleanup_rc" == 0 && "$monitor_active" == yes && "$mode" != middleware-business ]]; then
      "$PY" "$MONITOR" monitor-cleanup --relay-pid "$relay_pid" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$MONITOR" baseline --relay-pid "$relay_pid" || cleanup_rc=$?; fi
    fi
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$transport" == yes ]]; then "$PY" "$LAB" relay-clean || cleanup_rc=$?; fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  "$PY" "$LAB" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc receipt=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  printf '%s\n' "$rc" > "$work/exit"
  printf '%s\n' "$cleanup_rc" > "$work/finalized"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == middleware-business ]]; then
      echo LONGHORN_MIDDLEWARE_BUSINESS_BASELINE_RETAINED
    else
      echo LONGHORN_MIDDLEWARE_SCOPED_CLEAN_PASS
      if [[ "$mode" == middleware ]]; then echo LONGHORN_MIDDLEWARE_PASS; fi
      if [[ "$mode" == middleware-cleanup || "$mode" == middleware-reentry-cleanup ]]; then echo LONGHORN_MIDDLEWARE_CLEANUP_PASS; fi
    fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" verify-transport
"$PY" "$LAB" verify-core
transport=yes
phase=relay
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain &
relay_pid=$!
stage() {
  local name="$1" marker="$2"
  shift 2
  phase="$name"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$name" LONGHORN_MIDDLEWARE_STAGE_EXIT \
    "$@" 2>&1 | tee "$work/$name.log"
  [[ -f "$work/$name.exit" && ! -L "$work/$name.exit" && "$(cat "$work/$name.exit")" == 0 ]]
  [[ -f "$work/$name.finalized" && ! -L "$work/$name.finalized" && "$(cat "$work/$name.finalized")" == 0 ]]
  [[ "$(grep -Fxc 'LONGHORN_MIDDLEWARE_STAGE_EXIT rc=0' "$work/$name.log")" == 1 ]]
  [[ "$(grep -Fxc "$marker" "$work/$name.log")" == 1 ]]
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$work/$name.log"; then exit 1; fi
}
if [[ "$mode" == middleware-reentry-cleanup ]]; then
  phase=owned-reentry-qualification
  stage reentry-qualification LONGHORN_MIDDLEWARE_REENTRY_QUALIFICATION_PASS "$PY" "$DRIVER" qualify-reentry --relay-pid "$relay_pid"
fi
if [[ "$mode" == middleware-cleanup || "$mode" == middleware-reentry-cleanup ]]; then
  database_active=yes monitor_active=yes phase=owned-consumer-recovery
  exit 0
fi
if [[ "$mode" == middleware ]]; then
  "$PY" "$DRIVER" database-empty --relay-pid "$relay_pid"
  "$PY" "$MONITOR" baseline --relay-pid "$relay_pid"
  monitor_active=yes
  stage monitoring-product-setup LONGHORN_V1_MONITORING_SETUP_PASS "$PY" "$MONITOR" setup --relay-pid "$relay_pid"
  "$PY" "$DRIVER" baseline --relay-pid "$relay_pid"
  database_active=yes
  stage database-product-setup LONGHORN_MIDDLEWARE_SETUP_PASS "$PY" "$DRIVER" setup --relay-pid "$relay_pid"
fi
database_active=yes
stage database-native-recovery LONGHORN_MIDDLEWARE_BUSINESS_PASS "$PY" "$DRIVER" business --relay-pid "$relay_pid"
phase=verify-native-recovery
"$PY" "$DRIVER" verify --log "$work/database-native-recovery.log"
