#!/usr/bin/env bash
# Narrow resource qualification: reserve, admit, restore, then business smoke.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
RESOURCES="$BASE/tests/helpers/longhorn-v2-resources.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
MODE="${1:?mode required}"
case "$MODE" in v2-resources-qualification|v2-resources-recovery) ;; *) exit 2;; esac
marker="LONGHORN_${MODE^^}"
marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
"$PY" "$CORE" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-resources.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
resources_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  "$PY" "$RESOURCES" restore || cleanup_rc=$?
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$CORE" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap resources_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$CORE" relay-ensure
"$PY" "$CORE" relay-maintain &
relay_pid=$!
if [[ "$MODE" == v2-resources-recovery ]]; then
  phase=diagnosed-write-completion
  "$PY" "$RESOURCES" resolve-completed-write
else
  phase=hugepage-prepare
  "$PY" "$RESOURCES" prepare
  phase=native-resource-admission
  "$PY" "$RESOURCES" verify
fi
phase=hugepage-restore
"$PY" "$RESOURCES" restore
phase=production-smoke-after-resource-recovery
"$PY" "$CORE" smoke
