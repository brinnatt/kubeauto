#!/usr/bin/env bash
# LONGHORN-20 owns its attached-worker fence and controlled Deployment only.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-node-failure.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-node-failure.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in node-failure|node-failure-cleanup) ;; *) exit 2;; esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-node-failure.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_NODE_FAILURE_DURABLE_WORK path=$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
relay_pid= transport=no attempted=no phase=preflight
finish() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    kubectl --request-timeout=5s -n longhorn-system get volumes.longhorn.io,replicas.longhorn.io,engines.longhorn.io -o yaml > "$work/storage.txt" 2>&1 || true
  fi
  if [[ "$attempted" == yes && -f "$JOURNAL" && ! -L "$JOURNAL" ]]; then
    "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -q '^LONGHORN_NODE_FAILURE_RECOVERY_SCOPED_CLEAN_PASS ' "$work/cleanup.log" || cleanup_rc=$?; fi
  fi
  if [[ -n "$relay_pid" ]]; then
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
  if [[ "$attempted" == yes && "$cleanup_rc" == 0 ]]; then
    "$PY" "$LAB" verify-core || cleanup_rc=$?
    "$PY" "$LAB" health || cleanup_rc=$?
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
    if [[ "$mode" == node-failure-cleanup ]]; then
      echo LONGHORN_NODE_FAILURE_CLEANUP_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_FAILURE_CLEANUP_PASS
    else
      echo LONGHORN_NODE_FAILURE_GATE_SCOPED_CLEAN_PASS
      echo LONGHORN_NODE_FAILURE_GATE_PASS
    fi
  fi
  printf 'rc=%s phase=%s cleanup_rc=%s\n' "$rc" "$phase" "$cleanup_rc" > "$work/status.tmp"
  mv "$work/status.tmp" "$work/status"
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" verify-transport
"$PY" "$BASE/tests/helpers/longhorn-v1-reboot-relay.py" recover-current
"$PY" "$LAB" verify-core
if [[ "$mode" == node-failure-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || exit 1
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
fi
phase=transport
transport=yes
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == node-failure-cleanup ]]; then phase=owned-recovery; exit 0; fi
phase=attached-worker-loss
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_NODE_FAILURE_PASS "$work/business.log"
grep -q '^LONGHORN_NODE_FAILURE_OBSERVED ' "$work/business.log"
grep -q '^LONGHORN_NODE_FAILURE_SCOPED_CLEAN_PASS ' "$work/business.log"
