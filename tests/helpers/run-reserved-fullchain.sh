#!/usr/bin/env bash
# Sequential: docker reserved gate → wipe 133 → full enterprise regression (incl. G11 reserved).
# Run on Ubuntu aio control 138 as ubuntu (sudo used for regression).
set -euo pipefail
BASE=/usr/local/kubeauto
export PATH=/usr/local/bin:$PATH PYTHONPATH=$BASE
cd "$BASE"
LOG=/tmp/kubeauto-reserved-fullchain-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"
docker start local_registry >/dev/null 2>&1 || true

echo "==== DOCKER RESERVED GATE ===="
bash "$BASE/tests/helpers/delivery-docker-gate.sh"

echo "==== DESTROY docker cluster before full matrix ===="
kubecli destroy deliver-docker </dev/null 2>/dev/null || rm -rf "$BASE/clusters/deliver-docker"
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.133 \
  'set +e; systemctl stop kubelet docker cri-dockerd; rm -rf /var/lib/kubelet /var/lib/docker /etc/kubernetes /etc/docker /etc/cni /etc/systemd/system/podruntime.slice; echo 133_clean'

echo "==== FULL REGRESSION (G0-G11, tools skip) ===="
sudo -E env PATH="/usr/local/bin:$PATH" PYTHONPATH="$BASE" \
  bash "$BASE/tests/helpers/regression-full.sh"

echo FULLCHAIN_COMPLETE
echo "LOG=$LOG"
