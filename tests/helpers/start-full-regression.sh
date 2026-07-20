#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/bin:$PATH PYTHONPATH=/usr/local/kubeauto
BASE=/usr/local/kubeauto
cd "$BASE"

echo "verify root ssh"
sudo ssh -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.47.141 hostname

echo "clean cluster dirs for G2"
sudo rm -rf \
  "$BASE/clusters/test141" \
  "$BASE/clusters/debian150" \
  "$BASE/clusters/test-ded-etcd" \
  "$BASE/clusters/test-ha" \
  "$BASE/clusters/deliver-docker"

for ip in 130 131 132 133 140 141 142; do
  sudo ssh -o StrictHostKeyChecking=no root@192.168.47.$ip \
    'set +e; systemctl stop kubelet containerd docker cri-dockerd etcd 2>/dev/null; rm -rf /var/lib/kubelet /var/lib/containerd /var/lib/docker /etc/kubernetes /etc/cni /etc/containerd /etc/docker /etc/systemd/system/podruntime.slice /opt/kubeauto_prepare_tasks; systemctl daemon-reload; echo wiped' || true
done

LOG=/tmp/kubeauto-regression-full-$(date +%Y%m%d-%H%M%S).log
echo "REG_LOG=$LOG"
nohup sudo -E env PATH=/usr/local/bin:$PATH PYTHONPATH=/usr/local/kubeauto \
  bash "$BASE/tests/helpers/regression-147-full.sh" >"$LOG" 2>&1 &
echo "PID=$!"
sleep 25
tail -40 "$LOG"
