#!/usr/bin/env bash
# LONGHORN-24 retains the verified runner-owned V1 cluster and storage disks.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-maintenance.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-maintenance}"
case "$mode" in maintenance|maintenance-cleanup) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
"$PY" "$LAB" verify-transport
"$PY" "$LAB" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-maintenance.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_MAINTENANCE_DURABLE_DIRECTORY path=$work"
phase=relay
relay_pid=
maintenance_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    # Service restoration precedes checks that require the iSCSI client.
    "$PY" "$DRIVER" clean --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$LAB" relay-clean || cleanup_rc=$?
  "$PY" "$LAB" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  printf '%s\n' "$rc" > "$work/exit"
  if [[ "$rc" == 0 ]]; then
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap maintenance_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain &
relay_pid=$!
if [[ "$mode" == maintenance-cleanup ]]; then
  phase=owned-fixture-recovery
  journal="$BASE/clusters/longhorn-gate/longhorn-maintenance.json"
  [[ -f "$journal" && ! -L "$journal" ]] || exit 1
  "$PY" "$DRIVER" clean --relay-pid "$relay_pid"
  exit 0
fi
phase=maintenance-product-path
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
phase=finalize
"$PY" "$DRIVER" finish | tee -a "$work/business.log"
