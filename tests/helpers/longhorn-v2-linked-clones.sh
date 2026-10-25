#!/usr/bin/env bash
# Independent narrow qualifier; no generic snapshot-first cleanup of this graph.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-linked-clones.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-linked-clone-qualification|v2-linked-clone-cleanup) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
# Recovery must reach its own journal even when a failed attempt left volumes.
if [[ "$mode" == v2-linked-clone-qualification ]]; then "$PY" "$INSTALL" volume-baseline; fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-linked.XXXXXX)"
chmod 0700 "$work"
phase=relay
relay_pid=
linked_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  "$PY" "$DRIVER" clean || cleanup_rc=$?
  if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$INSTALL" volume-baseline || cleanup_rc=$?; fi
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
trap linked_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-linked-clone-cleanup ]]; then phase=owned-fixture-cleanup; exit 0; fi
phase=linked-nested-business
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -Fxq LONGHORN_V2_LINKED_FIXTURES_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_V2_LINKED_NESTED_BUSINESS_PASS "$work/business.log"
