#!/usr/bin/env bash
# LONGHORN-19/22 own an exact remote data disk or replica TCP endpoint.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
LAB="$BASE/tests/helpers/longhorn-lab.py"
DRIVER="$BASE/tests/helpers/longhorn-v1-storage-faults.py"
JOURNAL="$BASE/clusters/longhorn-gate/longhorn-install-business.json"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in
  network-failure|network-failure-cleanup) fault_case=network; marker=LONGHORN_NETWORK_FAILURE ;;
  disk-failure|disk-failure-cleanup) fault_case=disk; marker=LONGHORN_DISK_FAILURE ;;
  *) exit 2;;
esac
work="$(mktemp -d /var/tmp/kubeauto-longhorn-${fault_case}-failure.XXXXXX)"
chmod 0700 "$work"
echo "${marker}_DURABLE_WORK path=$work"
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
    "$PY" "$DRIVER" cleanup --case "$fault_case" --relay-pid "$relay_pid" | tee "$work/cleanup.log" || cleanup_rc=$?
    if [[ "$cleanup_rc" == 0 ]]; then grep -Fxq "LONGHORN_V1_STORAGE_${fault_case^^}_SCOPED_CLEAN_PASS" "$work/cleanup.log" || cleanup_rc=$?; fi
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
    "$PY" "$DRIVER" baseline --case "$fault_case" || cleanup_rc=$?
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
    if [[ "$mode" == *-cleanup ]]; then
      echo "${marker}_CLEANUP_SCOPED_CLEAN_PASS"
      echo "${marker}_CLEANUP_PASS"
    else
      echo "${marker}_SCOPED_CLEAN_PASS"
      echo "${marker}_PASS"
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
if [[ "$mode" == *-cleanup ]]; then
  [[ -f "$JOURNAL" && ! -L "$JOURNAL" ]] || exit 1
  "$PY" "$DRIVER" cleanup-preflight --case "$fault_case"
else
  "$PY" "$LAB" verify-core
  [[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]
  "$PY" "$DRIVER" baseline --case "$fault_case"
fi
phase=transport
transport=yes
"$PY" "$LAB" relay-ensure
"$PY" "$LAB" relay-maintain > "$work/relay.log" 2>&1 &
relay_pid=$!
attempted=yes
if [[ "$mode" == *-cleanup ]]; then phase=owned-recovery; exit 0; fi
phase=exact-replica-data-$fault_case-fault
"$PY" "$DRIVER" business --case "$fault_case" --relay-pid "$relay_pid" | tee "$work/business.log"
"$PY" "$DRIVER" verify --case "$fault_case" --log "$work/business.log"
