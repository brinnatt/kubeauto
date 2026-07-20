#!/usr/bin/env bash
# Wipe leftover kube/runtime state on lab nodes before reserved full-matrix regression.
# Usage: bash tests/helpers/lab-wipe-nodes.sh
set -euo pipefail

PW="${LAB_SSH_PASSWORD:-123456}"
ROCKY_IPS=(192.168.47.130 192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.140 192.168.47.141 192.168.47.142)
DEBIAN_IP=192.168.47.150
CONTROL=192.168.47.147

wipe_cmd='
set +e
systemctl stop kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd 2>/dev/null
systemctl disable kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd 2>/dev/null
# unmount residual mounts
mount | awk "/kubelet|containerd|docker|kube-/ {print \$3}" | xargs -r umount -l 2>/dev/null
# drop CNI/vxlan leftovers
fuser -k 8472/udp 2>/dev/null
for i in $(ip -o link show 2>/dev/null | awk -F": " "{print \$2}" | cut -d@ -f1 | grep -E "cilium|lxc|flannel|vxlan|cali|nodelocaldns|kube-ipvs" || true); do
  ip link set "$i" down 2>/dev/null
  ip link delete "$i" 2>/dev/null
done
ipvsadm -C 2>/dev/null
# remove units/data (keep OS)
rm -rf /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/docker /var/lib/etcd \
  /etc/kubernetes /etc/cni /etc/containerd /etc/docker /etc/crictl.yaml \
  /etc/calico /var/lib/calico /etc/cilium /run/cilium /sys/fs/bpf/cilium \
  /opt/kubeauto_prepare_tasks /root/.kube/config \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/docker.service \
  /etc/systemd/system/cri-dockerd.service /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/kube-apiserver.service /etc/systemd/system/kube-controller-manager.service \
  /etc/systemd/system/kube-scheduler.service /etc/systemd/system/etcd.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
systemctl daemon-reload 2>/dev/null
echo WIPE_OK $(hostname)
'

echo "== wipe rocky nodes =="
for ip in "${ROCKY_IPS[@]}"; do
  echo "--- $ip ---"
  sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 "root@$ip" "$wipe_cmd" || echo "WARN wipe failed $ip"
done

echo "== wipe debian 150 =="
sshpass -p "$PW" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 "brinnatt@$DEBIAN_IP" "sudo bash -lc $(printf '%q' "$wipe_cmd")" || echo "WARN wipe failed 150"

echo "== wipe control 147 k8s leftovers (KEEP docker + local registry :5000) =="
# Control node hosts registry.talkschool.cn:5000 via docker — never wipe dockerd/registry here.
CTRL_WIPE='
set +e
systemctl stop kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd 2>/dev/null
systemctl disable kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd 2>/dev/null
mount | awk "/kubelet|containerd\\/io.containerd|kube-/ {print \$3}" | xargs -r umount -l 2>/dev/null
fuser -k 8472/udp 2>/dev/null
for i in $(ip -o link show 2>/dev/null | awk -F": " "{print \$2}" | cut -d@ -f1 | grep -E "cilium|lxc|flannel|vxlan|cali|nodelocaldns|kube-ipvs" || true); do
  ip link set "$i" down 2>/dev/null
  ip link delete "$i" 2>/dev/null
done
ipvsadm -C 2>/dev/null
rm -rf /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/etcd \
  /etc/kubernetes /etc/cni /etc/containerd /etc/crictl.yaml \
  /etc/calico /var/lib/calico /etc/cilium /run/cilium /sys/fs/bpf/cilium \
  /opt/kubeauto_prepare_tasks /root/.kube/config \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/cri-dockerd.service \
  /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/kube-apiserver.service /etc/systemd/system/kube-controller-manager.service \
  /etc/systemd/system/kube-scheduler.service /etc/systemd/system/etcd.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
# DO NOT remove docker.service /var/lib/docker — local registry depends on them
systemctl daemon-reload 2>/dev/null
echo CTRL_WIPE_OK $(hostname)
'
sshpass -p "$PW" ssh -o StrictHostKeyChecking=no "ubuntu@$CONTROL" \
  "sudo bash -lc $(printf '%q' "$CTRL_WIPE"); sudo rm -rf /usr/local/kubeauto/clusters/*; ls /usr/local/kubeauto/clusters || true"

echo "LAB_WIPE_DONE"
