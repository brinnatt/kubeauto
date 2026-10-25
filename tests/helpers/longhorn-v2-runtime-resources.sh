#!/usr/bin/env bash
# Remaining runtime resource profiles only; the complete platform owns row 44.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-runtime-resources.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-runtime-resources|v2-runtime-resources-cleanup) ;; *) exit 2;; esac
RECORD="$BASE/clusters/longhorn-v2-gate/longhorn-v2-runtime-resources.json"
BUSINESS="$BASE/clusters/longhorn-v2-gate/longhorn-install-business.json"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-runtime-resources.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V2_RUNTIME_RESOURCES_DURABLE_DIRECTORY path=$work"
# The product 09 path and diagnostics use the explicit V2 kubeconfig above.
# Do not restore the shared V1 context during disjoint pool execution.
phase=preflight
transport=no attempted=no
relay_pid=
runtime_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true

  fi
  if [[ "$rc" != 0 && "$attempted" == yes && ( -e "$RECORD" || -L "$RECORD" ) ]]; then
    "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -Fxq LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS "$work/cleanup.log" || cleanup_rc=$?; fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$transport" == yes ]]; then "$PY" "$INSTALL" relay-clean || cleanup_rc=$?; fi
  if [[ "$attempted" == yes ]]; then
    "$PY" "$CORE" verify-core || cleanup_rc=$?
    "$PY" "$INSTALL" health || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  printf '%s\n' "$rc" > "$work/exit"
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == v2-runtime-resources-cleanup ]]; then
      echo LONGHORN_V2_RUNTIME_RESOURCES_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_V2_RUNTIME_RESOURCES_CLEANUP_PASS
    else
      echo LONGHORN_V2_RUNTIME_RESOURCES_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_V2_RUNTIME_RESOURCES_PASS
    fi
  fi
  exit "$rc"
}
trap runtime_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$CORE" verify-transport
"$PY" "$CORE" verify-core
if [[ "$mode" == v2-runtime-resources-cleanup ]]; then
  [[ ! -L "$RECORD" && -f "$RECORD" ]] || exit 1
else
  [[ ! -e "$RECORD" && ! -L "$RECORD" && ! -e "$BUSINESS" && ! -L "$BUSINESS" ]] || exit 1
fi
phase=relay
transport=yes
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
attempted=yes
if [[ "$mode" == v2-runtime-resources-cleanup ]]; then
  phase=owned-recovery
  "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" | tee "$work/cleanup.log"
  grep -Fxq LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS "$work/cleanup.log"
  exit 0
fi
phase=runtime-profiles-through-product
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_V2_RUNTIME_RESOURCES_BUSINESS_PASS "$work/business.log"
grep -Fxq LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS "$work/business.log"
grep -q '^LONGHORN_V2_RUNTIME_RESOURCES_RESULT ' "$work/business.log"
