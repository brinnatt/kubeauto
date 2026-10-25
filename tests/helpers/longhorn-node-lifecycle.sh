#!/usr/bin/env bash
# LONGHORN-25. The V2 source owner must release .148 before this branch starts.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-node-lifecycle.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-node-lifecycle.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in node-lifecycle|node-lifecycle-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-node-lifecycle.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_NODE_DURABLE_WORK path=$work"
phase=preflight
relay_pid=
attempted=no
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ "$attempted" == yes && -n "$relay_pid" ]]; then
    "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then
      grep -Fxq LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS "$work/cleanup.log" || cleanup_rc=1
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    kill -0 "$relay_pid" 2>/dev/null || { [[ "$rc" != 0 ]] || rc=1; }
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
    "$PY" "$DRIVER" relay-clean || cleanup_rc=$?
    "$PY" "$DRIVER" verify-core || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  fi
  if [[ "$rc" == 0 ]]; then
    "$PY" "$DRIVER" finalize || rc=$?
  fi
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == node-lifecycle-cleanup ]]; then
      echo LONGHORN_NODE_LIFECYCLE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_LIFECYCLE_CLEANUP_PASS
    else
      echo LONGHORN_NODE_LIFECYCLE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_LIFECYCLE_GATE_PASS
    fi
  fi
  if [[ "$rc" != 0 && ! -f "$work/first-failure.status" ]]; then
    printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" verify-transport
if [[ "$mode" == node-lifecycle-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo LONGHORN_NODE_RECOVERY_REQUIRES_OWNED_JOURNAL >&2; exit 1; }
  "$PY" "$DRIVER" recover-partial-join | tee "$work/partial-join-recovery.log"
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
  proof="${LONGHORN_V2_RELEASE_PROOF:-}"
  [[ "$proof" == /* && -f "$proof" && ! -L "$proof" ]] || { echo LONGHORN_NODE_REQUIRES_RELEASED_V2_PROOF >&2; exit 1; }
  phase=receiving-host-admission
  "$PY" "$DRIVER" admit --released-v2-proof "$proof" | tee "$work/admission.log"
fi
"$PY" "$DRIVER" relay-ensure
"$PY" "$DRIVER" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == node-lifecycle-cleanup ]]; then phase=owned-node-recovery; exit 0; fi
phase=add-node-data-expansion-guarded-retirement
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -q '^LONGHORN_NODE_DATA_EXPANSION_VERIFIED ' "$work/business.log"
grep -Fxq 'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-node' "$work/business.log"
grep -Fxq 'LONGHORN_NODE_PRODUCT_REMOVAL_REFUSAL step=del-master' "$work/business.log"
grep -q '^LONGHORN_NODE_NATIVE_EVACUATION_RETIRED ' "$work/business.log"
grep -Fxq LONGHORN_NODE_LIFECYCLE_SCOPED_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_NODE_LIFECYCLE_PASS "$work/business.log"
