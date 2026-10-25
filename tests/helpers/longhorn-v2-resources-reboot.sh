#!/usr/bin/env bash
# Environmental recovery preserves the installed baseline and resource ledger.
set -Eeuo pipefail
BASE=/usr/local/kubeauto
cd "$BASE"
PY="$BASE/.venv/bin/python"
CORE="$BASE/tests/helpers/longhorn-v2-core.py"
DRIVER="$BASE/tests/helpers/longhorn-v2-resources-reboot.py"
export PYTHONPATH="$BASE" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH"
[[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || exit 1
mode="${1:-}"
case "$mode" in
  v2-resources-reboot-recovery)
    export KUBECONFIG="$BASE/clusters/longhorn-v2-gate/kubectl.kubeconfig"
    check=("$PY" "$CORE" verify-core)
    ;;
  v2-dr-target-resources-reboot-recovery)
    export KUBECONFIG="$BASE/clusters/longhorn-v2-dr-target/kubectl.kubeconfig"
    check=("$PY" "$BASE/tests/helpers/longhorn-v2-dr-target.py" core-identity)
    ;;
  *) exit 2 ;;
esac
marker="LONGHORN_${mode^^}"
marker="${marker//-/_}"
work="$(mktemp -d /var/tmp/kubeauto-longhorn-v2-resources-reboot.XXXXXX)"
chmod 0700 "$work"
echo "${marker}_DURABLE_DIRECTORY path=$work"
phase=preflight
reboot_exit() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$rc" != 0 ]]; then
    printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
    echo "LONGHORN_FIRST_FAILURE phase=$phase rc=$rc diagnostics=$work resources=retained" >&2
    kubectl --request-timeout=5s get nodes -o yaml > "$work/nodes.yaml" 2>&1 || true
    kubectl --request-timeout=5s get pods -A -o wide > "$work/pods.txt" 2>&1 || true
    kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp > "$work/events.txt" 2>&1 || true
  fi
  printf '%s\n' "$rc" > "$work/exit"
  if [[ "$rc" == 0 ]]; then echo "${marker}_PASS"; fi
  exit "$rc"
}
trap reboot_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$mode" == v2-resources-reboot-recovery ]]; then "$PY" "$CORE" verify-transport; fi
"${check[@]}"
phase=restore-volatile-pages-and-original-health
"$PY" "$DRIVER" "$mode" | tee "$work/recovery.log"
phase=final-core-identity
"${check[@]}"
