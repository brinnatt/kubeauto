#!/usr/bin/env bash
# Product core setup on the separate, already transport-qualified V2 pool.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
DIR="$BASE/clusters/longhorn-v2-gate"
HELPER="$BASE/tests/helpers/longhorn-v2-core.py"
NODES=(192.168.47.146 192.168.47.147 192.168.47.148)
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
export KUBECONFIG="$DIR/kubectl.kubeconfig"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
[[ "${1:?mode required}" == v2-core ]] || exit 2
"$PY" "$HELPER" verify-transport
[[ ! -e "$DIR" && ! -L "$DIR" ]] || {
  echo 'LONGHORN_PREFLIGHT_FAIL class=environment reason=v2-core-requires-clean-start' >&2; exit 1;
}
"$PY" "$BASE/tests/helpers/longhorn-v2-lab.py" host-inputs
bash "$BASE/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only "${NODES[@]}"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-core.XXXXXX)"
chmod 0700 "$work"
control_config=/root/.kube/config
[[ ! -L "$control_config" ]] || exit 1
[[ ! -f "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
phase=configure
owned=no
relay_pid=
v2_core_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work" >&2
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    if [[ -f "$KUBECONFIG" ]]; then
      kubectl --request-timeout=5s get nodes -o wide > "$work/nodes.txt" 2>&1 || true
      kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
      kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
    fi
  fi
  if [[ -n "$relay_pid" ]]; then
    if [[ "$rc" == 0 ]] && ! kill -0 "$relay_pid" 2>/dev/null; then rc=1; fi
    kill -TERM "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || { [[ "$rc" != 0 ]] || rc=1; }
  fi
  if [[ "$owned" == yes ]]; then
    "$PY" "$HELPER" relay-clean || cleanup_rc=$?
    if [[ "$rc" != 0 && "$cleanup_rc" == 0 ]]; then
      "$PY" "$HELPER" cleanup || cleanup_rc=$?
    fi
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    cp -a "$work/control-kubeconfig" "$control_config" || cleanup_rc=$?
  else
    rm -f -- "$control_config" || cleanup_rc=$?
  fi
  if [[ "$cleanup_rc" != 0 ]]; then
    echo "LONGHORN_CLEANUP_FAIL phase=$phase rc=$cleanup_rc journal=retained" >&2
    [[ "$rc" != 0 ]] || rc=$cleanup_rc
  elif [[ "$rc" == 0 ]]; then
    rm -rf -- "$work"
    echo LONGHORN_V2_CORE_SCOPED_CLEAN_PASS
    echo LONGHORN_V2_CORE_PASS
  fi
  exit "$rc"
}
trap v2_core_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$PY" "$BASE/kubecli.py" new longhorn-v2-gate </dev/null
owned=yes
"$PY" "$HELPER" configure
phase=transport
"$PY" "$HELPER" relay-ensure
"$PY" "$HELPER" relay-maintain &
relay_pid=$!
phase=core-artifacts
"$PY" "$HELPER" supervise --relay-pid "$relay_pid" --step download
phase=core-setup
"$PY" "$HELPER" supervise --relay-pid "$relay_pid" --step setup
kubectl wait --for=condition=Ready nodes --all --timeout=10m
"$PY" "$HELPER" verify-core
phase=production-smoke
"$PY" "$HELPER" smoke
