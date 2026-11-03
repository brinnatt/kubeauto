#!/usr/bin/env bash
# Focused current-dev product qualification on the leased 137/128/142-143 pool.
# The development-host runner owns the lease, clean checks and durable wrapper.
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
source "$BASE/tests/helpers/delivery-gap-retest.sh"
source "$BASE/tests/helpers/nacos-tls-refusal-gate.sh"
CLUSTER=dev-nacos-startup
WORK="$BASE/clusters/$CLUSTER"
created=0

finish_startup() {
  local initial_rc=$? cleanup_rc=0
  trap - EXIT
  if (( initial_rc != 0 )) && [[ -f "$WORK/kubectl.kubeconfig" ]]; then
    export KUBECONFIG="$WORK/kubectl.kubeconfig"
    capture_nacos_failure || true
  fi
  if (( created )); then
    kubecli destroy "$CLUSTER" </dev/null || cleanup_rc=1
    [[ ! -e "$WORK" ]] || cleanup_rc=1
  fi
  if (( cleanup_rc == 0 )); then
    echo NACOS_STARTUP_SCOPED_CLEAN_PASS
  fi
  (( initial_rc == 0 && cleanup_rc == 0 )) || exit 1
}
trap finish_startup EXIT

[[ ! -e "$WORK" ]] || { echo 'NACOS_STARTUP_FAIL reason=foreign-cluster-path'; exit 1; }
kubecli download -D </dev/null
kubecli download -X </dev/null
kubecli download -E local-path-provisioner </dev/null
kubecli download -E nacos </dev/null
kubecli new "$CLUSTER" </dev/null
created=1
cat > "$WORK/hosts" <<'HOSTS'
[etcd]
192.168.47.137
[kube_master]
192.168.47.137 k8s_nodename='master-137'
[kube_node]
192.168.47.128 k8s_nodename='worker-debian' ansible_user=brinnatt ansible_become=true
192.168.47.142 k8s_nodename='worker-openeuler'
192.168.47.143 k8s_nodename='worker-opensuse'
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.78.0.0/16"
CLUSTER_CIDR="172.28.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/dev-nacos-startup"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
for key in KUBE_RESERVED_ENABLED SYS_RESERVED_ENABLED; do
  set_cfg "$WORK/config.yml" "$key" '"no"'
  grep -qx "$key: \"no\"" "$WORK/config.yml" || exit 1
done
set_cfg "$WORK/config.yml" SYS_RESERVED_ENFORCE '"no"'
set_ha_addons_disabled "$WORK/config.yml"
set_cfg "$WORK/config.yml" local_path_provisioner_install '"yes"'
set_cfg "$WORK/config.yml" openebs_install '"no"'
set_cfg "$WORK/config.yml" K8S_VER '"1.33.6"'
echo NACOS_STARTUP_CAPACITY_PASS kube_reserved=no sys_reserved=no jvm_heap=512m
sha256sum "$WORK/config.yml" "$WORK/hosts"
# Qualify the owning prepare path before installing dependent runtime/network
# components. A missing distribution dependency must stop in setup 01.
kubecli setup "$CLUSTER" 01 </dev/null
echo NACOS_STARTUP_PREPARE_PASS
kubecli setup "$CLUSTER" 90 </dev/null
export KUBECONFIG="$WORK/kubectl.kubeconfig"
nodes_ready "$CLUSTER" 60 || exit 1
[[ "$(kubectl get nodes --no-headers | wc -l)" -eq 4 ]] || exit 1
# Re-enter the actual prepare path with its completion marker present.
kubecli setup "$CLUSTER" 01 </dev/null
run_nacos_gate "$CLUSTER" startup
echo NACOS_STARTUP_FOCUSED_PASS
