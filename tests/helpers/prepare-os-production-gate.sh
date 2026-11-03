#!/usr/bin/env bash
# Only changed prepare behavior on Ubuntu 138 and Anolis 141; no runtime install.
# Run on 138 under the core pool lease; Registry/Docker remain control prerequisites.
set -euo pipefail
umask 077
BASE=/usr/local/kubeauto
export PYTHONPATH="$BASE" PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
source "$BASE/tests/helpers/delivery-gap-retest.sh"
CLUSTER=dev-prepare-compat
WORK="$BASE/clusters/$CLUSTER"
created=0
evidence=$(umask 077; mktemp -d /var/tmp/kubeauto-prepare-compat.XXXXXX)
target_command() {
  local host="$1" script="$2"
  if [[ "$host" == 192.168.47.138 ]]; then
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 ubuntu@192.168.47.138 \
      "sudo -n bash -c $(printf '%q' "$script")"
  else
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@192.168.47.141 \
      "bash -c $(printf '%q' "$script")"
  fi
}
verify_idle() {
  local host="$1"
  target_command "$host" 'set -eu
    for unit in kubelet etcd containerd kube-apiserver kube-proxy; do
      state=$(systemctl is-active "$unit" 2>/dev/null || true)
      case "$state" in inactive|unknown) ;; *) exit 1;; esac
    done
    for path in /etc/kubernetes /etc/containerd /var/lib/kubelet /var/lib/containerd /var/lib/etcd /opt/kubeauto_prepare_tasks; do test ! -e "$path"; done
    ps -eww -o cgroup= | awk '\''/(^|[,:\/])(kubepods|podruntime)([.]slice|\/|$)/ {busy=1} END {exit busy}'\'''
}
cleanup_prepare_only() {
  local host expected
  # No API/runtime was installed. Never invoke a full-cluster teardown whose
  # data-protection guard correctly requires a reachable API. Remove only the
  # prepare artifacts admitted below; keep OS packages and control Registry.
  for host in 192.168.47.138 192.168.47.141; do
    # setup 01 reissues the admin client certificate on its control host, while
    # the prepare marker preserves the first copy on a worker. Bind both to
    # this run's unique CA, server and contexts before admitting their hashes.
    target_command "$host" 'test ! -e /root/.kube/config || { test ! -L /root/.kube/config && cat /root/.kube/config; }' \
      >"$evidence/$host.cleanup-kubeconfig" || return 1
    expected=$("$BASE/.venv/bin/python" - "$WORK/kubectl.kubeconfig" "$evidence/$host.cleanup-kubeconfig" <<'PYCFG'
import hashlib, pathlib, sys, yaml
current = yaml.safe_load(pathlib.Path(sys.argv[1]).read_bytes())
raw = pathlib.Path(sys.argv[2]).read_bytes()
if raw:
    candidate = yaml.safe_load(raw)
    for key in ('clusters', 'contexts', 'current-context'):
        assert candidate[key] == current[key], 'foreign kubeconfig identity'
    assert [u['name'] for u in candidate['users']] == [u['name'] for u in current['users']]
print(hashlib.sha256(raw).hexdigest())
PYCFG
    ) || return 1
    target_command "$host" "set -eu
      for unit in kubelet etcd containerd kube-apiserver kube-proxy; do
        state=\$(systemctl is-active \"\$unit\" 2>/dev/null || true)
        case \"\$state\" in inactive|unknown) ;; *) exit 1;; esac
      done
      for path in /etc/containerd /var/lib/kubelet /var/lib/containerd /var/lib/etcd; do test ! -e \"\$path\"; done
      ps -eww -o cgroup= | awk '/(^|[,:\/])(kubepods|podruntime)([.]slice|\/|$)/ {busy=1} END {exit busy}'
      test ! -L /etc/kubernetes
      test ! -L /opt/kubeauto_prepare_tasks
      if test -e /opt/kubeauto_prepare_tasks; then test \"\$(cat /opt/kubeauto_prepare_tasks)\" = done; fi
      if test -e /root/.kube/config; then
        test ! -L /root/.kube/config
        test \"\$(sha256sum /root/.kube/config | cut -d ' ' -f1)\" = '$expected'
      fi
      if test -d /etc/kubernetes; then
        test -z \"\$(find /etc/kubernetes -type f -o -type l)\"
        test ! -d /etc/kubernetes/ssl || rmdir /etc/kubernetes/ssl
        rmdir /etc/kubernetes
      fi
      rm -f /opt/kubeauto_prepare_tasks /root/.kube/config" || return 1
    verify_idle "$host" || return 1
  done
  rm -rf -- "$WORK"
}
finish_prepare_os() {
  local initial_rc=$? cleanup_rc=0
  trap - EXIT
  if (( created )); then cleanup_prepare_only || cleanup_rc=1; fi
  chmod 0600 "$evidence"/* 2>/dev/null || true
  if (( initial_rc == 0 && cleanup_rc == 0 )); then
    echo "PREPARE_OS_SCOPED_CLEAN_PASS evidence=$evidence"
    echo LAB_CLEAN_VERIFY_PASS
  else
    echo "PREPARE_OS_DIAGNOSTICS directory=$evidence"
    exit 1
  fi
}
trap finish_prepare_os EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "${PREPARE_OS_CLEAN_ONLY:-0}" == 1 ]]; then
  test -f "$WORK/hosts" && test -f "$WORK/config.yml"
  # This fixed recovery is for the recorded prepare-only failure, never an
  # arbitrary existing cluster; a runtime/data path on either node refuses it.
  test "$(sed -n '/^192[.]/{s/ .*//;p}' "$WORK/hosts" | sort -u | tr '\n' ' ')" = '192.168.47.138 192.168.47.141 '
  cleanup_prepare_only
  echo PREPARE_OS_CLEAN_RECOVERY_PASS
  exit 0
fi
[[ ! -e "$WORK" ]] || exit 1
for host in 192.168.47.138 192.168.47.141; do
  verify_idle "$host"
  target_command "$host" 'set -eu
    . /etc/os-release
    printf "OS=%s VERSION=%s\n" "$ID" "$VERSION_ID"
    if command -v dpkg-query >/dev/null; then
      dpkg-query -W -f='\''${Package} ${db:Status-Status}\n'\'' | awk '\''$2=="installed" {print $1}'\'' | LC_ALL=C sort -u
    else rpm -qa --qf "%{NAME}.%{ARCH}\n" | LC_ALL=C sort -u; fi' >"$evidence/$host.before"
done
kubecli new "$CLUSTER" </dev/null
created=1
cat >"$WORK/hosts" <<'HOSTS'
[etcd]
192.168.47.138 ansible_user=ubuntu ansible_become=true
[kube_master]
192.168.47.138 k8s_nodename='master-aio' ansible_user=ubuntu ansible_become=true
[kube_node]
192.168.47.141 k8s_nodename='anolis-141'
[harbor]
[ex_lb]
[chrony]
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.80.0.0/16"
CLUSTER_CIDR="172.30.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/dev-prepare-compat"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
for key in KUBE_RESERVED_ENABLED SYS_RESERVED_ENABLED SYS_RESERVED_ENFORCE; do set_cfg "$WORK/config.yml" "$key" '"no"'; done
set_ha_addons_disabled "$WORK/config.yml"
set_cfg "$WORK/config.yml" K8S_VER '"1.33.6"'
kubecli setup "$CLUSTER" 01 </dev/null
kubecli setup "$CLUSTER" 01 </dev/null
for host in 192.168.47.138 192.168.47.141; do
  target_command "$host" 'set -eu
    test -f /opt/kubeauto_prepare_tasks
    for command in ipset nft iptables iptables-save iptables-restore ip6tables ip6tables-save ip6tables-restore; do "$command" --version; done
    for service in firewalld ufw; do
      state=$(systemctl show "$service.service" --property=LoadState --value)
      if test "$state" != not-found; then
        test "$(systemctl is-active "$service.service" || true)" = inactive
        enabled=$(systemctl is-enabled "$service.service" || true)
        if test "$service" = firewalld; then test "$enabled" = masked; else case "$enabled" in disabled|masked) ;; *) exit 1;; esac; fi
      fi
    done' >"$evidence/$host.runtime"
  target_command "$host" 'set -eu
    if command -v dpkg-query >/dev/null; then
      dpkg-query -W -f='\''${Package} ${db:Status-Status}\n'\'' | awk '\''$2=="installed" {print $1}'\'' | LC_ALL=C sort -u
    else rpm -qa --qf "%{NAME}.%{ARCH}\n" | LC_ALL=C sort -u; fi' >"$evidence/$host.after"
  sed '/^OS=/d' "$evidence/$host.before" >"$evidence/$host.packages-before"
  LC_ALL=C comm -23 "$evidence/$host.packages-before" "$evidence/$host.after" >"$evidence/$host.removed"
  [[ ! -s "$evidence/$host.removed" ]]
  echo "PREPARE_OS_HOST_PASS host=$host repeated_prepare=yes retained_packages=yes network_tools=8"
done
sha256sum "$evidence"/* >"$evidence/SHA256SUMS"
echo PREPARE_OS_FOCUSED_PASS
