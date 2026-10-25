#!/usr/bin/env bash
# One real 30-minute source-pool window; V2 uses its independent leased pool.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-v1-stability.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-stability}"
case "$mode" in stability|stability-cleanup) ;; *) exit 2;; esac
"$PY" "$LAB" verify-transport
"$PY" "$BASE/tests/helpers/longhorn-v1-reboot-relay.py" recover-current
"$PY" "$LAB" verify-core
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v1-stability.XXXXXX)"
chmod 0700 "$work"
echo "LONGHORN_V1_STABILITY_DURABLE_DIRECTORY path=$work"
phase=relay
relay_pid=
active=no
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
    if [[ "$active" == yes ]]; then "$PY" "$DRIVER" cleanup --relay-pid "$relay_pid" || cleanup_rc=$?; fi
    if [[ "$cleanup_rc" == 0 ]]; then "$PY" "$DRIVER" baseline || cleanup_rc=$?; fi
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
  printf '%s\n' "$cleanup_rc" > "$work/finalized"
  if [[ "$rc" == 0 ]]; then
    echo LONGHORN_STABILITY_SCOPED_CLEAN_PASS
    if [[ "$mode" == stability ]]; then echo LONGHORN_STABILITY_PASS; fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain &
relay_pid=$!
if [[ "$mode" == stability-cleanup ]]; then phase=owned-stability-recovery; active=yes; exit 0; fi
"$PY" "$DRIVER" baseline
active=yes
phase=confirmed-write-fault-window
bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/business" LONGHORN_V1_STABILITY_EXIT \
  "$PY" "$DRIVER" business --relay-pid "$relay_pid" 2>&1 | tee "$work/business.log"
[[ -f "$work/business.exit" && ! -L "$work/business.exit" && "$(cat "$work/business.exit")" == 0 ]]
[[ -f "$work/business.finalized" && ! -L "$work/business.finalized" && "$(cat "$work/business.finalized")" == 0 ]]
[[ "$(grep -Fxc 'LONGHORN_V1_STABILITY_EXIT rc=0' "$work/business.log")" == 1 ]]
"$PY" "$DRIVER" verify --log "$work/business.log"
