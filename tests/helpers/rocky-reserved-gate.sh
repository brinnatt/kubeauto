#!/usr/bin/env bash
# Rocky 8 cgroup v1 hybrid reserved enablement gate (run on 147).
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH=$BASE PATH=/usr/local/bin:$PATH
cd "$BASE"
echo "=== rocky reserved gate ==="
docker start local_registry >/dev/null 2>&1 || true
curl -sf http://127.0.0.1:5000/v2/brinnatt/pause/tags/list
echo
# Ansible uses SSH keys from control node (same as regression-147-full G5).
kubecli system -a --user root --password 123456 192.168.47.141 </dev/null

sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.141 \
  'set +e; systemctl stop kubelet containerd; rm -rf /var/lib/kubelet /etc/kubernetes /etc/cni /var/lib/containerd /etc/containerd /etc/systemd/system/kubelet.service /etc/systemd/system/containerd.service /etc/systemd/system/podruntime.slice /opt/kubeauto_prepare_tasks; systemctl daemon-reload; sed -i "/registry.talkschool.cn/d" /etc/hosts; echo "192.168.47.147    registry.talkschool.cn" >> /etc/hosts; echo 141_clean'

rm -rf "$BASE/clusters/test141"
kubecli new test141 </dev/null
test -d "$BASE/clusters/test141"
cat > "$BASE/clusters/test141/hosts" <<'EOF'
[etcd]
192.168.47.141
[kube_master]
192.168.47.141 k8s_nodename='master-141'
[kube_node]
192.168.47.141
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.70.0.0/16"
CLUSTER_CIDR="172.23.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/test141"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/__k8s_ver__/1.33.6/g' "$BASE/clusters/test141/config.yml"
grep -E 'RESERVED' "$BASE/clusters/test141/config.yml"
echo "=== setup test141 ==="
kubecli setup test141 90 </dev/null
export KUBECONFIG="$BASE/clusters/test141/kubectl.kubeconfig"
for i in $(seq 1 72); do
  if kubectl get nodes --no-headers 2>/dev/null | grep -q ' Ready'; then break; fi
  sleep 5
done
kubectl get nodes -o wide
bash "$BASE/tests/helpers/verify-node-reserved.sh" "$KUBECONFIG"
sshpass -p 123456 ssh -o StrictHostKeyChecking=no root@192.168.47.141 \
  'systemctl show kubelet containerd kube-proxy -p Slice --value; ls -d /sys/fs/cgroup/systemd/podruntime.slice /sys/fs/cgroup/memory/podruntime.slice; test ! -d /sys/fs/cgroup/systemd/podruntime.slice.slice && echo NO_DOUBLE_SLICE; findmnt -no FSTYPE /sys/fs/cgroup; test ! -f /sys/fs/cgroup/cgroup.controllers && echo HYBRID_V1; grep -E "kubeReservedCgroup|systemReservedCgroup" /var/lib/kubelet/config.yaml'
echo ROCKY_RESERVED_GATE_PASS
echo RESERVED_DUAL_CGROUP_GATE_PASS
