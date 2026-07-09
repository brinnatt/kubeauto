#!/bin/bash
# Create a single-node cluster inventory on 192.168.47.133
# Usage: mk-cluster-133.sh <cluster_name> [calico|flannel|cilium|kube-router|kube-ovn] [ipvs|iptables]
set -euo pipefail
NAME="${1:?cluster name}"
NET="${2:-calico}"
PROXY="${3:-ipvs}"
BASE="/usr/local/kubeauto/clusters/${NAME}"
mkdir -p "$BASE"
cat > "${BASE}/hosts" <<EOF
[etcd]
192.168.47.133

[kube_master]
192.168.47.133 k8s_nodename='master-133'

[kube_node]
192.168.47.133

[harbor]

[ex_lb]

[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="${NET}"
PROXY_MODE="${PROXY}"
SERVICE_CIDR="10.70.0.0/16"
CLUSTER_CIDR="172.23.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/${NAME}"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
if [ ! -f "${BASE}/config.yml" ]; then
  cp /usr/local/kubeauto/conf/config.yml "${BASE}/config.yml"
  sed -i "s/__k8s_ver__/1.33.6/g" "${BASE}/config.yml"
fi
echo "Created ${BASE}/hosts (${NET}/${PROXY})"
echo "Note: run 'kubecli new ${NAME}' first so config.yml placeholders are resolved"
