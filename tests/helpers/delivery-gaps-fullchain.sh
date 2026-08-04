#!/bin/bash
# Build the authoritative live topologies, then run every remaining G10 gate.
# The development-host runner owns pre/post lab wipe and durable supervision.
set -euo pipefail

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

echo "========== gaps full-chain image preflight =========="
kubecli download -D </dev/null
# Harbor's official offline flow requires the versioned installer archive to
# be downloaded before installation.  ``download -D`` intentionally covers the
# core delivery set only, while G2 runs the optional ``setup 11`` Harbor role;
# stage that archive explicitly on every clean control host.
# https://goharbor.io/docs/2.13.0/install-config/download-installer/
kubecli download -R </dev/null
kubecli download -X </dev/null
for component in flannel cilium kube-router kube-ovn prometheus ingress-nginx \
  network-check local-path-provisioner openebs nfs-provisioner rocketmq \
  cilium-hubble nacos minio dashboard prometheus-dingtalk; do
  kubecli download -E "$component" </dev/null || true
done

echo "========== gaps full-chain authoritative topology =========="
timeout --signal=TERM --kill-after=30s 14400s \
  bash "$BASE/tests/helpers/regression-full.sh"
echo GAPS_BASELINE_REGRESSION_PASS

echo "========== gaps full-chain focused delivery gates =========="
timeout --signal=TERM --kill-after=30s 21600s \
  bash "$BASE/tests/helpers/delivery-gap-retest.sh"

echo DELIVERY_GAPS_FULLCHAIN_PASS
