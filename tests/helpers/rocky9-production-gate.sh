#!/usr/bin/env bash
# Current-dev prepare/IPVS regression on three exclusively leased Rocky 9.8 nodes.
# The outer fixed runner holds both Ceph pool locks through final verification.
set -euo pipefail
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
source "$BASE/tests/helpers/delivery-gap-retest.sh"
CLUSTER=dev-rocky9-production
WORK="$BASE/clusters/$CLUSTER"
HOSTS=(192.168.122.212 192.168.122.165 192.168.122.238)
SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10)
created=0
NFS_RUN=''
relay_owned=0
relay_pid=''
relay_path=/var/lib/kubeauto-ceph-test/ssh-relay.json
evidence=$(umask 077; mktemp -d /var/tmp/kubeauto-rocky9-production.XXXXXX)

finish_rocky9() {
  local initial_rc=$? cleanup_rc=0
  trap - EXIT
  if (( initial_rc != 0 && created )); then
    kubectl --kubeconfig="$WORK/kubectl.kubeconfig" get pods -A -o wide \
      >"$evidence/pods.log" 2>&1 || true
    kubectl --kubeconfig="$WORK/kubectl.kubeconfig" get events -A \
      >"$evidence/events.log" 2>&1 || true
  fi
  if (( created )); then
    kubecli destroy "$CLUSTER" </dev/null || cleanup_rc=1
    [[ ! -e "$WORK" ]] || cleanup_rc=1
  fi
  if [[ -n "$NFS_RUN" && ! -e "$WORK" ]]; then
    "${SSH[@]}" root@192.168.122.238 "python3 - cleanup '$NFS_RUN'" \
      <"$BASE/tests/helpers/nfs-production-fixture.py" || cleanup_rc=1
  fi
  if [[ -n "$relay_pid" ]]; then
    kill "$relay_pid" 2>/dev/null || true
    wait "$relay_pid" || cleanup_rc=1
  fi
  if (( relay_owned )); then
    python3 "$BASE/tests/helpers/ceph_ssh_fixture.py" cleanup --path "$relay_path" || cleanup_rc=1
    [[ ! -e "$relay_path" ]] || cleanup_rc=1
  fi
  chmod 0600 "$evidence"/* 2>/dev/null || true
  echo "ROCKY9_PRODUCTION_EVIDENCE directory=$evidence"
  if (( initial_rc == 0 && cleanup_rc == 0 )); then echo ROCKY9_PRODUCTION_SCOPED_CLEAN_PASS; else exit 1; fi
}
trap finish_rocky9 EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[[ ! -e "$WORK" ]] || { echo ROCKY9_PRODUCTION_REFUSED_FOREIGN_CLUSTER; exit 1; }
[[ ! -e "$relay_path" ]] || { echo ROCKY9_PRODUCTION_REFUSED_FOREIGN_RELAY; exit 1; }

for host in "${HOSTS[@]}"; do
  "${SSH[@]}" "root@$host" 'set -eu
    . /etc/os-release
    test "$ID" = rocky; test "$VERSION_ID" = 9.8
    # Recreate the customer prerequisite even if an earlier lab install removed
    # firewalld. Enable without starting it, keeping remote API/SSH reachable.
    dnf install -y firewalld >/dev/null
    systemctl unmask firewalld.service
    systemctl enable firewalld.service
    printf "ROCKY9_HOST os=%s version=%s kernel=%s\n" "$ID" "$VERSION_ID" "$(uname -r)"
    rpm -qa --qf "%{NAME}.%{ARCH}\n" | LC_ALL=C sort -u' >"$evidence/$host.before"
done
for component in -D -X; do kubecli download "$component" </dev/null; done
kubecli download -E network-check </dev/null
install -d -m 0700 /var/lib/kubeauto-ceph-test
# The outer runner owns both Ceph locks. Reuse the already qualified fixture;
# its journal is written before the first mutation, including partial failures.
relay_owned=1
python3 "$BASE/tests/helpers/ceph_ssh_fixture.py" setup --path "$relay_path" --profile rocky9
python3 "$BASE/tests/helpers/ceph_ssh_fixture.py" maintain --path "$relay_path" >"$evidence/registry-relay.log" 2>&1 &
relay_pid=$!
for host in "${HOSTS[@]}"; do
  "${SSH[@]}" "root@$host" 'set -eu
    for image in pause:3.10 json-mock:v1.3.1; do
      curl --fail --silent --show-error --noproxy "*" --max-time 10 \
        -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
        "http://192.168.47.130:5000/v2/brinnatt/${image%:*}/manifests/${image##*:}" >/dev/null
    done'
done
kill -0 "$relay_pid"
echo ROCKY9_NODE_REGISTRY_PREFLIGHT_PASS
kubecli new "$CLUSTER" </dev/null
created=1
cat >"$WORK/hosts" <<'HOSTS'
[etcd]
192.168.122.212
[kube_master]
192.168.122.212 k8s_nodename='rocky9-master'
[kube_node]
192.168.122.165 k8s_nodename='rocky9-worker1'
192.168.122.238 k8s_nodename='rocky9-worker2'
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.79.0.0/16"
CLUSTER_CIDR="172.29.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/dev-rocky9-production"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
for key in KUBE_RESERVED_ENABLED SYS_RESERVED_ENABLED SYS_RESERVED_ENFORCE; do
  set_cfg "$WORK/config.yml" "$key" '"no"'
done
set_ha_addons_disabled "$WORK/config.yml"
set_cfg "$WORK/config.yml" K8S_VER '"1.33.6"'
set_cfg "$WORK/config.yml" openebs_install '"no"'
set_cfg "$WORK/config.yml" local_path_provisioner_install '"no"'
cp "$WORK/hosts" "$WORK/config.yml" "$evidence/"
kubecli setup "$CLUSTER" 01 </dev/null
kubecli setup "$CLUSTER" 90 </dev/null
for host in "${HOSTS[@]}"; do
  "${SSH[@]}" "root@$host" 'set -eu
    systemctl unmask firewalld.service
    systemctl enable firewalld.service'
done
kubecli setup "$CLUSTER" 01 </dev/null
for host in "${HOSTS[@]}"; do
  "${SSH[@]}" "root@$host" 'set -eu
    test -f /opt/kubeauto_prepare_tasks
    for command in ipset nft iptables iptables-save iptables-restore ip6tables ip6tables-save ip6tables-restore; do "$command" --version; done
    if rpm -q firewalld >/dev/null 2>&1; then
      test "$(systemctl is-active firewalld.service || true)" = inactive
      test "$(systemctl is-enabled firewalld.service || true)" = masked
    fi
    rpm -qa --qf "%{NAME}.%{ARCH}\n" | LC_ALL=C sort -u' >"$evidence/$host.after"
  # Version lines are evidence, while only exact RPM identities are compared.
  sed -n '/ROCKY9_HOST/!p' "$evidence/$host.before" >"$evidence/$host.packages-before"
  "${SSH[@]}" "root@$host" 'rpm -qa --qf "%{NAME}.%{ARCH}\n" | LC_ALL=C sort -u' >"$evidence/$host.packages-after"
  LC_ALL=C comm -23 "$evidence/$host.packages-before" "$evidence/$host.packages-after" >"$evidence/$host.removed"
  [[ ! -s "$evidence/$host.removed" ]] || { echo "ROCKY9_PACKAGE_RETENTION_FAIL host=$host"; exit 1; }
  echo "ROCKY9_PREPARE_PASS host=$host retained_packages=yes repeated_prepare=yes network_tools=8"
done
export KUBECONFIG="$WORK/kubectl.kubeconfig"
nodes_ready "$CLUSTER" 60
[[ $(kubectl get nodes --no-headers | wc -l) -eq 3 ]]
PRODUCTION_SMOKE_IMAGE=registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1 \
PRODUCTION_SMOKE_SERVER_NODE=rocky9-worker1 PRODUCTION_SMOKE_CLIENT_NODE=rocky9-worker2 \
  bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"
if [[ "${ROCKY9_GATE_MODE:-network}" == nfs ]]; then
  source "$BASE/tests/helpers/nfs-production-gate.sh"
  NFS_RUN="nfs-$(date -u +%Y%m%dT%H%M%S)-$$"
  verify_nfs_production "$CLUSTER" "$WORK/config.yml" "$NFS_RUN"
fi
sha256sum "$evidence"/* >"$evidence/SHA256SUMS"
echo ROCKY9_PRODUCTION_FOCUSED_PASS
