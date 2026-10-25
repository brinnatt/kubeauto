#!/usr/bin/env bash
# Two existing narrow qualifications, one frozen source sync and parent lease.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
[[ "${1:-}" == v2-interrupt-manager-qualification ]] || exit 2
for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
  read -r fd filename <<< "$entry"
  [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || exit 1
  flock -n "$fd" || exit 1
done
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-interrupt-manager.XXXXXX)"
chmod 0700 "$work"
phase=preflight
batch_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
  else
    echo "LONGHORN_V2_INTERRUPT_MANAGER_DURABLE_DIRECTORY path=$work"
    echo LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_SCOPED_CLEAN_PASS
    echo LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS
  fi
  # Each original child restores its own faults and verifies its own baseline.
  # Retain per-child logs and terminal fences for independent evidence import.
  exit "$rc"
}
trap batch_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for branch in v2-interrupt-qualification v2-manager-loss-qualification; do
  phase="$branch"
  marker="LONGHORN_${branch^^}"; marker="${marker//-/_}"
  prefix="$work/$branch"
  echo "LONGHORN_V2_INTERRUPT_MANAGER_STAGE_START stage=$branch"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$prefix" "${marker}_EXIT" \
    bash "$BASE/tests/helpers/longhorn-v2-volumes.sh" "$branch" 2>&1 | tee "$prefix.log"
  [[ -f "$prefix.exit" && ! -L "$prefix.exit" && "$(cat "$prefix.exit")" == 0 ]]
  [[ -f "$prefix.finalized" && ! -L "$prefix.finalized" && "$(cat "$prefix.finalized")" == 0 ]]
  for expected in "${marker}_SCOPED_CLEAN_PASS" "${marker}_PASS" "${marker}_EXIT rc=0"; do
    [[ "$(grep -Fxc "$expected" "$prefix.log")" == 1 ]]
  done
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$prefix.log"; then exit 1; fi
  echo "LONGHORN_V2_INTERRUPT_MANAGER_STAGE_PASS stage=$branch durable_rc=0 finalized=0"
done
