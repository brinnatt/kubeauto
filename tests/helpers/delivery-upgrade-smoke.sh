#!/bin/bash
set -euo pipefail
export PYTHONPATH=/usr/local/kubeauto PATH=/usr/local/bin:/usr/bin:$PATH
BASE=/usr/local/kubeauto
C=deliver-docker
NODE=192.168.47.141
LOG=/var/log/kubeauto-upgrade-smoke-$(date +%Y%m%d%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
echo "LOG=$LOG"

ST=/home/ubuntu/k8s1335
if [ ! -x "$ST/kubelet" ] || ! "$ST/kubelet" --version 2>&1 | grep -q 1.33.5; then
  ST=/tmp/k8s1335
  rm -rf "$ST" && mkdir -p "$ST"
  echo "Downloading k8s v1.33.5 from dl.k8s.io ..."
  for b in kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy kubectl; do
    echo "  $b"
    curl -fsSL --retry 3 --connect-timeout 30 -o "$ST/$b" "https://dl.k8s.io/v1.33.5/bin/linux/amd64/$b"
    chmod +x "$ST/$b"
  done
else
  echo "Using pre-staged binaries in $ST"
fi
"$ST/kube-apiserver" --version
"$ST/kubelet" --version

echo "Ensure control has 1.33.6"
"$BASE/kube-bin/kube-apiserver" --version | grep 1.33.6
cp -f "$BASE"/kube-bin/* /usr/local/bin/
/usr/local/bin/kube-apiserver --version | grep 1.33.6

echo "Install 1.33.5 on node"
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE '
  systemctl stop kube-apiserver kube-controller-manager kube-scheduler kubelet kube-proxy 2>/dev/null || true
  sleep 2
'
sshpass -p 123456 scp -o StrictHostKeyChecking=no \
  "$ST/kube-apiserver" "$ST/kube-controller-manager" "$ST/kube-scheduler" \
  "$ST/kubelet" "$ST/kube-proxy" "$ST/kubectl" root@$NODE:/usr/local/bin/
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@$NODE '
  systemctl start kube-apiserver kube-controller-manager kube-scheduler kubelet
  # kube-proxy may be a static pod; ignore if unit missing
  systemctl start kube-proxy 2>/dev/null || true
  sleep 8
  kube-apiserver --version
  kubelet --version
'
export KUBECONFIG=$BASE/clusters/$C/kubectl.kubeconfig
for i in $(seq 1 30); do
  st=$(kubectl get node --no-headers 2>/dev/null | awk '{print $2}')
  echo "node=$st"
  [ "$st" = Ready ] && break
  sleep 5
done
kubectl get nodes -o wide
before=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')
echo "BEFORE=$before"
echo "$before" | grep -q 1.33.5

echo "Running kubecli upgrade"
kubecli upgrade "$C" </dev/null
for i in $(seq 1 36); do
  after=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null || true)
  echo "AFTER_try$i=$after"
  echo "$after" | grep -q 1.33.6 && break
  sleep 10
done
kubectl get nodes -o wide
kubectl get pods -A -o wide
after=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')
echo "AFTER=$after"
echo "$after" | grep -q 1.33.6
bad=$(kubectl get pods -A --no-headers | awk '$4!="Running" && $4!="Completed"{print}' | wc -l)
echo "bad_pods=$bad"
[ "$bad" -eq 0 ]
echo UPGRADE_SMOKE_PASS
