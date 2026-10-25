#!/usr/bin/env bash
# One monitoring owner; focused setup may retain its verified healthy baseline.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-monitoring.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-v2-monitoring}"
case "$mode" in v2-monitoring|v2-monitoring-setup|v2-monitoring-cleanup) ;; *) exit 2;; esac
"$PY" "$CORE" verify-transport
"$PY" "$CORE" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-monitoring.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_MONITORING_DURABLE_DIRECTORY path=$work"
relay_pid=
phase=relay
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
    if [[ "$rc" != 0 || "$mode" != v2-monitoring-setup ]]; then
      "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" || cleanup_rc=$?
    fi
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$INSTALL" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  printf '%s\n' "$rc" > "$work/exit"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == v2-monitoring-setup ]]; then
      echo LONGHORN_V2_MONITORING_BASELINE_READY
    else
      echo LONGHORN_V2_MONITORING_SCOPED_CLEAN_PASS
      echo LONGHORN_V2_MONITORING_PASS
    fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$mode" != v2-monitoring-cleanup ]]; then
  phase=monitoring-product-setup
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/setup" LONGHORN_V2_MONITORING_SETUP_EXIT \
    "$PY" "$DRIVER" setup --relay-pid "$relay_pid" 2>&1 | tee "$work/setup.log"
  [[ "$(cat "$work/setup.exit")" == 0 && "$(cat "$work/setup.finalized")" == 0 ]]
  [[ "$(grep -Fxc 'LONGHORN_V2_MONITORING_SETUP_EXIT rc=0' "$work/setup.log")" == 1 ]]
  [[ "$(grep -Fxc 'LONGHORN_V2_MONITORING_SETUP_PASS' "$work/setup.log")" == 1 ]]
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$work/setup.log"; then exit 1; fi
  phase=monitoring-business-health
  "$PY" "$DRIVER" verify --relay-pid "$relay_pid"
fi
