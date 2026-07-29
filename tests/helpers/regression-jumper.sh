#!/bin/bash
# Enterprise G7 regression on the Rocky jumper (192.168.47.130).
# Run remotely after source sync: bash /tmp/regression-jumper.sh
set -euo pipefail

BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE"
export PATH="/usr/local/bin:/usr/bin:$PATH"
PY="$(command -v python3.12 || command -v python3)"

"$PY" -c '
from common.utils import run_command
from common.ansible_python import ansible_python_policy, format_policy_summary
r = run_command(["ansible", "--version"], capture=True, check=False)
assert r.returncode == 0, r.stderr or r.stdout
p = ansible_python_policy()
assert p.core_version == (2, 16), p.core_version
print(format_policy_summary(p))
'

bash "$BASE/tests/run_unit_tests.sh"
kubecli system -a --user root --password 123456 192.168.47.131-137 </dev/null

# k8s-dev is disposable test state, not a prerequisite carried by the jumper.
# Build the inventory deterministically so a clean delivery host exercises the
# same topology every time.  Keep reserved enforcement on the dedicated
# 137/138 gates; these smaller shared nodes intentionally run with it disabled.
# The outer runner has just completed LAB_CLEAN_VERIFY_PASS on every node.
# Only the controller-side disposable configuration can remain from an older
# run, so remove that exact directory without running teardown against clean
# hosts (which would emit misleading ignored service-not-found failures).
rm -rf "$BASE/clusters/k8s-dev"
kubecli new k8s-dev </dev/null
cat > "$BASE/clusters/k8s-dev/hosts" <<'EOF'
[etcd]
192.168.47.134
192.168.47.135
192.168.47.136

[kube_master]
192.168.47.134 k8s_nodename='master-134'
192.168.47.135 k8s_nodename='master-135'
192.168.47.136 k8s_nodename='master-136'

[kube_node]
192.168.47.131 k8s_nodename='worker-131'

[harbor]

[ex_lb]

[chrony]

[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.72.0.0/16"
CLUSTER_CIDR="172.25.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/k8s-dev"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
EOF
sed -i 's/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/' "$BASE/clusters/k8s-dev/config.yml"
sed -i 's/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$BASE/clusters/k8s-dev/config.yml"
kubecli setup k8s-dev 90 </dev/null

export KUBECONFIG="$BASE/clusters/k8s-dev/kubectl.kubeconfig"
for attempt in $(seq 1 30); do
  # Control-plane nodes are intentionally cordoned and are rendered by
  # kubectl as Ready,SchedulingDisabled.  The Ready condition is still true.
  ready=$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/ {count++} END {print count+0}')
  total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)
  echo "[WAIT] cluster=k8s-dev nodes_ready=${ready}/${total} attempt=${attempt}/30"
  if [[ "$ready" -eq 4 && "$total" -eq 4 ]]; then
    break
  fi
  [[ "$attempt" -lt 30 ]] || {
    kubectl get nodes -o wide || true
    echo "k8s-dev did not reach 4/4 Ready" >&2
    exit 1
  }
  sleep 10
done

kubecli destroy k8s-dev </dev/null
echo "G7_JUMPER_PASS"
