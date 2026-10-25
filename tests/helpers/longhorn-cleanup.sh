#!/usr/bin/env bash
# Exact scenario recovery only. Native retirement remains the LONGHORN-54 gate.
set -Eeuo pipefail
umask 077
BASE=/usr/local/kubeauto
cd "$BASE"
[[ ( "${1:-}" == cleanup || "${1:-}" == cleanup-qualification ) && "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 2
for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
  read -r fd filename <<< "$entry"
  [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || exit 1
  flock -n "$fd" || exit 1
done
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export LONGHORN_CLEANUP_WORK
LONGHORN_CLEANUP_WORK="$(mktemp -d /var/tmp/kubeauto-longhorn-cleanup.XXXXXX)"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$LONGHORN_CLEANUP_WORK/control-kubeconfig"
helper_pid=
mode="$1"
finish() {
  local rc=$? restore_rc=0
  trap - EXIT INT TERM
  if [[ -n "$helper_pid" ]]; then
    kill -TERM "$helper_pid" 2>/dev/null || true
    wait "$helper_pid" || true
  fi
  if [[ -f "$LONGHORN_CLEANUP_WORK/control-kubeconfig" ]]; then
    cp -a "$LONGHORN_CLEANUP_WORK/control-kubeconfig" "$control_config" || restore_rc=$?
  else
    rm -f -- "$control_config" || restore_rc=$?
  fi
  if [[ "$restore_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL reason=control-context-restore rc=$restore_rc" >&2
    [[ "$rc" != 0 ]] || rc=$restore_rc
  fi
  printf '%s\n' "$rc" > "$LONGHORN_CLEANUP_WORK/exit"
  printf '%s\n' "$restore_rc" > "$LONGHORN_CLEANUP_WORK/finalized"
  echo "LONGHORN_CLEANUP_DURABLE_WORK path=$LONGHORN_CLEANUP_WORK"
  if [[ "$rc" == 0 ]]; then
    if [[ "$mode" == cleanup ]]; then echo LONGHORN_CLEANUP_PASS; else echo LONGHORN_CLEANUP_QUALIFICATION_PASS; fi
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
helper_args=()
[[ "$mode" != cleanup-qualification ]] || helper_args+=(--qualify)
"$BASE/.venv/bin/python" "$BASE/tests/helpers/longhorn-cleanup.py" "${helper_args[@]}" &
helper_pid=$!
helper_rc=0
wait "$helper_pid" || helper_rc=$?
helper_pid=
[[ "$helper_rc" == 0 ]] || exit "$helper_rc"
