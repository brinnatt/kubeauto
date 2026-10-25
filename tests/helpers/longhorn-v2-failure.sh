#!/usr/bin/env bash
# Full V2 failure module, or one focused branch, on one exclusively leased pool.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
INSTALL="$BASE/tests/helpers/longhorn-v2-install.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in
  v2-failure) cases=(replica-im manager attached-spdk remote-spdk replica-network worker-reboot single-disk rebuild-qos all-replicas);;
  v2-failure-remaining) cases=(remote-spdk replica-network worker-reboot single-disk rebuild-qos all-replicas);;
  v2-failure-cleanup) cases=();;
  v2-failure-replica-im|v2-failure-manager|v2-failure-attached-spdk|v2-failure-remote-spdk|v2-failure-replica-network|v2-failure-worker-reboot|v2-failure-single-disk|v2-failure-rebuild-qos|v2-failure-all-replicas)
    cases=("${mode#v2-failure-}");;
  *) exit 2;;
esac
marker="LONGHORN_${mode^^}"; marker="${marker//-/_}"
"$PY" "$CORE" verify-transport
if [[ "$mode" != v2-failure-cleanup ]]; then "$PY" "$INSTALL" volume-baseline; fi
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-failure.XXXXXX)"
chmod 0700 "$work"
phase=transport
relay_pid=
active_helper=
failure_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  if [[ -n "$active_helper" ]]; then
    "$PY" "$active_helper" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
  elif [[ "$mode" == v2-failure-cleanup ]]; then
    # Select the actual journal owner. Calling an unrelated restorer could
    # otherwise fall through to business deletion before restoring its fault.
    local selected
    if selected="$("$PY" - <<'PY'
import json
from pathlib import Path
path=Path('/usr/local/kubeauto/clusters/longhorn-v2-gate/longhorn-install-business.json')
if path.is_symlink(): raise ValueError('symlink fault journal')
if path.exists():
    row=json.loads(path.read_text())
    owners={'v2_all_replicas_fault':'longhorn-v2-all-replicas.py','v2_disk_fault':'longhorn-v2-disk-faults.py',
            'v2_runtime_fault':'longhorn-v2-runtime-faults.py','v2_manager_loss':'longhorn-v2-manager-loss.py',
            'v2_replica_im_loss':'longhorn-v2-control-faults.py'}
    choices=[helper for key,helper in owners.items() if key in row]
    if len(choices)!=1: raise ValueError('no unique failure-module journal owner; do not delete business')
    print(choices[0])
PY
)"; then
      if [[ -n "$selected" ]]; then
        "$PY" "$BASE/tests/helpers/$selected" cleanup --relay-pid "${relay_pid:-0}" || cleanup_rc=$?
      fi
    else cleanup_rc=1; fi
  fi
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
    echo "LONGHORN_V2_FAILURE_DURABLE_DIRECTORY path=$work"
    echo "${marker}_SCOPED_CLEAN_PASS"
    echo "${marker}_PASS"
  fi
  exit "$rc"
}
trap failure_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$INSTALL" relay-ensure
"$PY" "$INSTALL" relay-maintain &
relay_pid=$!
if [[ "$mode" == v2-failure-cleanup ]]; then phase=owned-fault-recovery; exit 0; fi
for branch in "${cases[@]}"; do
  args=()
  case "$branch" in
    replica-im) helper=longhorn-v2-control-faults.py;;
    manager) helper=longhorn-v2-manager-loss.py;;
    attached-spdk|remote-spdk|replica-network|worker-reboot)
      helper=longhorn-v2-runtime-faults.py; args=(--case "$branch");;
    single-disk|rebuild-qos) helper=longhorn-v2-disk-faults.py; args=(--case "$branch");;
    all-replicas) helper=longhorn-v2-all-replicas.py;;
    *) exit 2;;
  esac
  active_helper="$BASE/tests/helpers/$helper"
  phase="$branch"
  prefix="$work/$branch"
  echo "LONGHORN_V2_FAILURE_STAGE_START case=$branch"
  bash "$BASE/tests/helpers/run-durable-gate.sh" "$prefix" LONGHORN_V2_FAILURE_STAGE_EXIT \
    "$PY" "$active_helper" business --relay-pid "$relay_pid" "${args[@]}" 2>&1 | tee "$prefix.log"
  [[ -f "$prefix.exit" && ! -L "$prefix.exit" && "$(cat "$prefix.exit")" == 0 ]]
  [[ -f "$prefix.finalized" && ! -L "$prefix.finalized" && "$(cat "$prefix.finalized")" == 0 ]]
  [[ "$(grep -Fxc 'LONGHORN_V2_FAILURE_STAGE_EXIT rc=0' "$prefix.log")" == 1 ]]
  "$PY" "$active_helper" verify --log "$prefix.log" "${args[@]}"
  if grep -Eq '^(LONGHORN_[A-Z0-9_]*(FAIL|FIRST_FAILURE)( |$)|FAILED|ERROR[:!]|FAIL:)' "$prefix.log"; then exit 1; fi
  "$PY" "$INSTALL" volume-baseline
  echo "LONGHORN_V2_FAILURE_STAGE_PASS case=$branch durable_rc=0 finalized=0"
  active_helper=
done
