#!/usr/bin/env bash
# LONGHORN-49 narrow negative/positive admission boundary, not complete clone GA.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-incomplete-clone.py"
JOURNAL="$BASE/clusters/longhorn-v2-gate/longhorn-v2-incomplete-clone.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in v2-incomplete-clone-qualification|v2-incomplete-clone-cleanup) ;; *) exit 2;; esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-incomplete.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_DURABLE_WORK path=$work"
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
  if [[ "$attempted" == yes ]]; then
    if [[ -e "$JOURNAL" || -L "$JOURNAL" ]]; then
      "$PY" "$DRIVER" clean --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
      if [[ "$cleanup_rc" == 0 ]]; then
        grep -Fxq LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS "$work/cleanup.log" || cleanup_rc=1
      fi
    elif [[ "$rc" == 0 ]]; then
      grep -Fxq LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS "$work/business.log" || cleanup_rc=1
    fi
    if [[ "$cleanup_rc" == 0 ]]; then
      "$PY" "$INSTALL" volume-baseline | tee "$work/baseline-after.log" || cleanup_rc=$?
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  "$PY" "$CORE" relay-clean || cleanup_rc=$?
  "$PY" "$CORE" verify-core || cleanup_rc=$?
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained diagnostics=$work" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
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
"$PY" "$CORE" verify-transport
if [[ "$mode" == v2-incomplete-clone-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || { echo LONGHORN_INCOMPLETE_RECOVERY_REQUIRES_OWNED_JOURNAL >&2; exit 1; }
else
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
  "$PY" "$INSTALL" volume-baseline | tee "$work/baseline-before.log"
fi
phase=relay
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == v2-incomplete-clone-cleanup ]]; then phase=owned-fixture-cleanup; exit 0; fi
phase=real-incomplete-clone-negative-and-restored-positive
"$PY" "$DRIVER" business --relay-pid "$relay_pid" | tee "$work/business.log"
grep -q '^LONGHORN_V2_INCOMPLETE_CLONE_RESULT ' "$work/business.log"
grep -Fxq LONGHORN_V2_INCOMPLETE_CLONE_FIXTURES_CLEAN_PASS "$work/business.log"
grep -Fxq LONGHORN_V2_INCOMPLETE_CLONE_BUSINESS_PASS "$work/business.log"
