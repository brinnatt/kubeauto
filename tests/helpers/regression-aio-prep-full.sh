#!/bin/bash
# Persistent 138 regression preflight: fill the local registry, then run G2+.
# Started by tests/run_enterprise_regression.sh; output is written by nohup.
set -euo pipefail

BASE=/usr/local/kubeauto
STATE_PREFIX=/tmp/kubeauto-regression-aio
export PYTHONPATH="$BASE"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# Publish durable state for the development-host supervisor.  The PID alone is
# insufficient because a finished process can disappear between two polls;
# the EXIT record preserves the exact result even after SSH reconnects.
rm -f "${STATE_PREFIX}.exit"
printf '%s\n' "$$" > "${STATE_PREFIX}.pid"
finish() {
  local rc=$?
  trap - EXIT
  printf '%s\n' "$rc" > "${STATE_PREFIX}.exit"
  exit "$rc"
}
trap finish EXIT

# Every Kubernetes sandbox requires pause.  Preloading all default images before
# setup prevents a node from being permanently NotReady when its CNI DaemonSet
# first creates a sandbox against the local delivery registry.
kubecli download -D </dev/null
kubecli download -X </dev/null
for component in flannel cilium kube-router kube-ovn prometheus ingress-nginx \
  network-check local-path-provisioner; do
  kubecli download -E "$component" </dev/null || true
done

set +e
# The foreground supervisor provides 30s heartbeat and 60s silence diagnostics;
# this outer deadline guarantees that a live-but-deadlocked child cannot occupy
# the delivery lab forever.
timeout --signal=TERM --kill-after=30s 14400s bash /tmp/regression-full.sh
rc=$?
set -e
echo "REGRESSION_AIO_EXIT rc=$rc"
exit "$rc"
