#!/usr/bin/env bash
# Reuse only the healthy Redis-owned baseline; never reinstall shared prerequisites.
set -Eeuo pipefail
module="${1:?focused module required}"
case "$module" in sentinel-data|sentinel-security|idempotence|sentinel-failure|sentinel-quorum|persistence|sentinel-repair|tls-rotation|password-rotation|rotation-repair|backup|performance|observability|capacity|capacity-lag|cluster-install|cluster-clean|cluster-config|cluster-config-repair|cluster-data|cluster-failure|cluster-primary-fault|cluster-repair|cluster-rebalance|cluster-rebalance-seed|stability-faults|stability|crd-upgrade|platform|maintenance-rolling|maintenance-drain|maintenance-repair|upgrade-prepare|upgrade-sentinel|cleanup-qualification|upgrade-cluster|upgrade-cluster-repair|runtime-modules|cluster-snapshot) ;; *) exit 2;; esac
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
KC="$BASE/clusters/redis-gate/kubectl.kubeconfig"
export KUBECONFIG="$KC" PATH="$BASE/kube-bin:$BASE/extra-bin:$PATH" PYTHONPATH="$BASE"
lease="${REDIS_LAB_LEASE_FILE:-/var/lock/kubeauto-redis-core-pool.lock}"
if [[ "${REDIS_PARENT_LEASE_HELD:-no}" == yes ]]; then
  [[ /proc/self/fd/9 -ef "$lease" ]] || exit 1
else
  exec 9>"$lease"
fi
flock -n 9 || { echo 'REDIS_PREFLIGHT_FAIL class=environment reason=core-pool-leased' >&2; exit 1; }
work="$(mktemp -d /tmp/kubeauto-redis-focused-control.XXXXXX)"
control_config="${REDIS_CONTROL_KUBECONFIG:-$HOME/.kube/config}"
[[ ! -e "$control_config" ]] || cp -a "$control_config" "$work/control-kubeconfig"
on_exit() {
  local rc=$?
  trap - EXIT
  set +e
  if [[ "$rc" -ne 0 ]]; then
    echo "REDIS_FIRST_FAILURE module=$module rc=$rc; preserving baseline and collecting diagnostics" >&2
    kubectl --kubeconfig="$KC" get nodes -o wide
    for ns in redis-gate redis-gate-operator; do
      kubectl --kubeconfig="$KC" -n "$ns" get pods,pvc,svc -o wide
      kubectl --kubeconfig="$KC" -n "$ns" get events --sort-by=.lastTimestamp
      kubectl --kubeconfig="$KC" -n "$ns" describe pods
    done
    kubectl --kubeconfig="$KC" -n redis-gate-operator logs deployment/redis-operator --all-containers --tail=100
  fi
  if [[ -f "$work/control-kubeconfig" ]]; then
    mkdir -p "$(dirname "$control_config")"
    cp -a "$work/control-kubeconfig" "$control_config"
  else
    rm -f -- "$control_config"
  fi
  rm -rf -- "$work"
  exit "$rc"
}
trap on_exit EXIT
cd "$BASE"
script=redis-focused-gate.py
case "$module" in tls-rotation|password-rotation|rotation-repair) script=redis-rotation-gate.py;; esac
[[ "$module" != backup ]] || script=redis-backup-gate.py
[[ "$module" != performance ]] || script=redis-performance-gate.py
[[ "$module" != observability ]] || script=redis-observability-gate.py
[[ "$module" != capacity && "$module" != capacity-lag ]] || script=redis-capacity-gate.py
case "$module" in cluster-install|cluster-clean|cluster-config|cluster-config-repair) script=redis-cluster-gate.py;; esac
[[ "$module" != cluster-data ]] || script=redis-cluster-client-gate.py
case "$module" in cluster-failure|cluster-primary-fault|cluster-repair) script=redis-cluster-failure-gate.py;; esac
case "$module" in cluster-rebalance|cluster-rebalance-seed) script=redis-cluster-rebalance-gate.py;; esac
case "$module" in stability-faults|stability) script=redis-stability-gate.py;; esac
[[ "$module" != crd-upgrade ]] || script=redis-crd-upgrade-gate.py
[[ "$module" != platform ]] || script=redis-platform-gate.py
case "$module" in maintenance-rolling|maintenance-drain|maintenance-repair) script=redis-maintenance-gate.py;; esac
case "$module" in upgrade-prepare|upgrade-sentinel) script=redis-upgrade-gate.py;; esac
[[ "$module" != cleanup-qualification ]] || script=redis-cleanup-gate.py
case "$module" in upgrade-cluster|upgrade-cluster-repair) script=redis-cluster-upgrade-gate.py;; esac
[[ "$module" != cluster-snapshot ]] || script=redis-cluster-snapshot-gate.py
[[ "$module" != runtime-modules ]] || script=redis-runtime-module-gate.py
"$BASE/.venv/bin/python" -u "$BASE/tests/helpers/$script" "$module" --base "$BASE"
