#!/usr/bin/env bash
# Wipe leftover kube/runtime state on lab nodes before reserved full-matrix regression.
# Usage: bash tests/helpers/lab-wipe-nodes.sh [--verify [--rocky-only <rocky-ip> ...] | --rocky-only <rocky-ip> ...]
set -euo pipefail

ROCKY_IPS=(192.168.47.131 192.168.47.132 192.168.47.133 192.168.47.134 192.168.47.135 192.168.47.136 192.168.47.137)
JUMPER=192.168.47.130
DEBIAN_IP=192.168.47.128
CONTROL=192.168.47.138
WIPE_DEBIAN=true
WIPE_CONTROL=true
MODE=wipe

# A topology may reuse Rocky nodes in the same regression (for example,
# dedicated-etcd followed by HA).  Etcd data is cluster-specific and must not
# survive that transition.  This mode intentionally leaves the Debian and AIO
# scenarios intact while resetting only the named Rocky nodes.
if [[ "${1:-}" == "--verify" ]]; then
  MODE=verify
  shift
  if [[ "${1:-}" == "--rocky-only" ]]; then
    shift
    [[ "$#" -gt 0 ]] || {
      echo "Usage: $0 --verify --rocky-only <rocky-ip> ..." >&2
      exit 2
    }
    ROCKY_IPS=("$@")
    WIPE_DEBIAN=false
    WIPE_CONTROL=false
  elif [[ "$#" -ne 0 ]]; then
    echo "Usage: $0 [--verify [--rocky-only <rocky-ip> ...] | --rocky-only <rocky-ip> ...]" >&2
    exit 2
  fi
elif [[ "${1:-}" == "--rocky-only" ]]; then
  shift
  [[ "$#" -gt 0 ]] || {
    echo "Usage: $0 [--rocky-only <rocky-ip> ...]" >&2
    exit 2
  }
  ROCKY_IPS=("$@")
  WIPE_DEBIAN=false
  WIPE_CONTROL=false
fi

# Prefer the development host's direct key-authenticated route.  Some lab
# failures can leave a node accepting TCP/22 without returning an SSH banner
# on that route.  The jumper provides an independent source path to the same
# sshd; OpenSSH ProxyJump keeps authentication end-to-end with the local key.
rocky_ssh() {
  local ip="$1" remote_cmd="$2"
  local ssh_opts=(
    -o BatchMode=yes
    -o StrictHostKeyChecking=no
    -o ConnectTimeout=8
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2
  )

  if ssh "${ssh_opts[@]}" "root@$ip" "$remote_cmd"; then
    return 0
  fi

  echo "WARN direct SSH failed $ip; retrying through jumper $JUMPER" >&2
  ssh -J "root@$JUMPER" "${ssh_opts[@]}" "root@$ip" "$remote_cmd"
}

# A successful cleanup must be demonstrated before a new scenario starts.
# Do not use `pgrep -f`: its pattern can match the verification command itself.
verify_cmd='
bad=0
for unit in kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf "unit_%s=%s\\n" "$unit" "${state:-unknown}"
  case "$state" in inactive|unknown) ;; *) bad=1 ;; esac
done
for path in /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/docker /var/lib/etcd /etc/kubernetes /etc/cni /etc/containerd /etc/docker /etc/crictl.yaml /etc/kube-lb; do
  if test -e "$path"; then echo "residue=$path"; bad=1; fi
done
if findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | grep -q .; then
  findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | sed "s/^/residue_mount=/"
  bad=1
fi
if test -e /run/cilium; then echo "residue=/run/cilium"; bad=1; fi
if test -e /run/calico; then echo "residue=/run/calico"; bad=1; fi
# `pgrep -x` only matches the kernel 15-character comm field on some
# distributions.  `ps comm` plus an exact awk comparison is portable and
# cannot match this verification command itself.
shim_count=$(ps -eo comm= | awk "\$1 == \"containerd-shim-runc-v2\" {count++} END {print count+0}")
echo "containerd_shim_count=$shim_count"
test "$shim_count" -eq 0 || bad=1
if hostname -I 2>/dev/null | tr " " "\\n" | grep -qx "192.168.47.137"; then
  for path in /var/data /var/log/harbor; do
    if test -e "$path"; then echo "residue=$path"; bad=1; fi
  done
fi
exit "$bad"
'

verify_control_cmd='
bad=0
for unit in kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  printf "unit_%s=%s\\n" "$unit" "${state:-unknown}"
  case "$state" in inactive|unknown) ;; *) bad=1 ;; esac
done
for path in /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/etcd /etc/kubernetes /etc/cni /etc/containerd /etc/crictl.yaml /etc/kube-lb /data/registry /var/snap/docker/common/kubeauto-registry; do
  if test -e "$path"; then echo "residue=$path"; bad=1; fi
done
if findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | grep -q .; then
  findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | sed "s/^/residue_mount=/"
  bad=1
fi
if test -e /run/cilium; then echo "residue=/run/cilium"; bad=1; fi
if test -e /run/calico; then echo "residue=/run/calico"; bad=1; fi
shim_count=$(ps -eo comm= | awk "\$1 == \"containerd-shim-runc-v2\" {count++} END {print count+0}")
echo "containerd_shim_count=$shim_count"
test "$shim_count" -eq 0 || bad=1
docker_state=$(systemctl is-active snap.docker.dockerd.service 2>/dev/null || systemctl is-active docker 2>/dev/null || true)
docker_state=$(echo "$docker_state" | tail -n 1)
echo "local_docker=${docker_state:-absent}"
# A rebuilt control host may not have Docker until source sync followed by
# `kubecli download -D`. Missing/inactive Docker is still a clean preflight
# state when the disposable registry paths above are absent; live gates prove
# Docker and registry readiness before cluster setup.
if test -n "$docker_state"; then
  case "$docker_state" in active|inactive|unknown) ;; *) bad=1 ;; esac
fi
if docker container inspect local_registry >/dev/null 2>&1; then echo "residue=local_registry"; bad=1; fi
exit "$bad"
'

if [[ "$MODE" == verify ]]; then
  echo "== verify rocky nodes are clean =="
  for ip in "${ROCKY_IPS[@]}"; do
    echo "--- $ip ---"
    rocky_ssh "$ip" \
      "timeout --signal=TERM --kill-after=10s 180s bash -lc $(printf '%q' "$verify_cmd")"
  done
  if [[ "$WIPE_DEBIAN" == true ]]; then
    echo "== verify Debian 128 is clean =="
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "brinnatt@$DEBIAN_IP" \
      "sudo timeout --signal=TERM --kill-after=10s 180s bash -lc $(printf '%q' "$verify_cmd")"
  fi
  if [[ "$WIPE_CONTROL" == true ]]; then
    echo "== verify Ubuntu aio control 138 is clean =="
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "ubuntu@$CONTROL" \
      "sudo timeout --signal=TERM --kill-after=10s 180s bash -lc $(printf '%q' "$verify_control_cmd")"
  fi
  echo "LAB_CLEAN_VERIFY_PASS"
  exit 0
fi

wipe_cmd='
set +e
systemctl stop kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd 2>/dev/null
systemctl disable kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd docker cri-dockerd 2>/dev/null
# The runtime units intentionally use KillMode=process so Kubernetes does not
# lose container shims during ordinary service restarts.  A lab wipe is
# different: every shim is test residue and must be terminated before deleting
# its state, otherwise orphaned shims consume the host PID cgroup across runs.
systemctl kill --kill-who=all --signal=SIGKILL containerd docker cri-dockerd 2>/dev/null
# Cilium auto-mounts cgroup2 on the host at /run/cilium/cgroupv2. Detach
# nested targets deepest-first before rm -rf reaches cgroup control files.
findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | sort -r | xargs -r -n1 umount -l 2>/dev/null
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
  /etc/calico /var/lib/calico /run/calico /etc/cilium /run/cilium /sys/fs/bpf/cilium \
  /opt/kubeauto_prepare_tasks /root/.kube/config \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/docker.service \
  /etc/systemd/system/cri-dockerd.service /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/kube-apiserver.service /etc/systemd/system/kube-controller-manager.service \
  /etc/systemd/system/kube-scheduler.service /etc/systemd/system/etcd.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
# 137 is the dedicated Harbor test host. Harbor data and logs must not survive
# a Docker cleanup, or an inactive installation is falsely treated as valid.
if hostname -I 2>/dev/null | tr " " "\\n" | grep -qx "192.168.47.137"; then
  rm -rf /var/data /var/log/harbor
fi
systemctl daemon-reload 2>/dev/null
# Some unit files have just been removed.  Reset all failed records so systemd
# also drops historical failures whose units no longer exist (not-found).
systemctl reset-failed 2>/dev/null
if findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | grep -q . || test -e /run/cilium || test -e /run/calico; then
  echo "WIPE_FAILED CNI mount residue on $(hostname)" >&2
  exit 1
fi
echo WIPE_OK $(hostname)
'

echo "== wipe rocky nodes =="
wipe_failed=0
for ip in "${ROCKY_IPS[@]}"; do
  echo "--- $ip ---"
  if ! rocky_ssh "$ip" \
    "timeout --signal=TERM --kill-after=10s 180s bash -lc $(printf '%q' "$wipe_cmd")"; then
    echo "ERROR wipe failed $ip" >&2
    wipe_failed=1
  fi
done

if [[ "$WIPE_DEBIAN" == true ]]; then
  echo "== wipe Debian 128 =="
  if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "brinnatt@$DEBIAN_IP" \
    "sudo timeout --signal=TERM --kill-after=10s 180s bash -lc $(printf '%q' "$wipe_cmd")"; then
    echo "ERROR wipe failed 128" >&2
    wipe_failed=1
  fi
fi

if [[ "$WIPE_CONTROL" == true ]]; then
echo "== wipe Ubuntu aio control 138 (KEEP Docker; RESET local registry data) =="
# Keep Docker itself because 138 is the image control host.  Remove the
# disposable local_registry container and its data so the next download gate
# must recreate a complete registry instead of inheriting cached test state.
CTRL_WIPE='
set +e
systemctl stop kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd 2>/dev/null
systemctl disable kubelet kube-proxy kube-apiserver kube-controller-manager kube-scheduler kube-lb etcd containerd cri-dockerd 2>/dev/null
systemctl kill --kill-who=all --signal=SIGKILL containerd cri-dockerd 2>/dev/null
findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | sort -r | xargs -r -n1 umount -l 2>/dev/null
mount | awk "/kubelet|containerd\\/io.containerd|kube-/ {print \$3}" | xargs -r umount -l 2>/dev/null
fuser -k 8472/udp 2>/dev/null
for i in $(ip -o link show 2>/dev/null | awk -F": " "{print \$2}" | cut -d@ -f1 | grep -E "cilium|lxc|flannel|vxlan|cali|nodelocaldns|kube-ipvs" || true); do
  ip link set "$i" down 2>/dev/null
  ip link delete "$i" 2>/dev/null
done
ipvsadm -C 2>/dev/null
rm -rf /var/lib/kubelet /var/lib/kube-proxy /var/lib/containerd /var/lib/etcd \
  /etc/kubernetes /etc/cni /etc/containerd /etc/crictl.yaml \
  /etc/calico /var/lib/calico /run/calico /etc/cilium /run/cilium /sys/fs/bpf/cilium \
  /opt/kubeauto_prepare_tasks /root/.kube/config \
  /etc/systemd/system/kubelet.service /etc/systemd/system/kube-proxy.service \
  /etc/systemd/system/containerd.service /etc/systemd/system/cri-dockerd.service \
  /etc/systemd/system/kube-lb.service \
  /etc/systemd/system/kube-apiserver.service /etc/systemd/system/kube-controller-manager.service \
  /etc/systemd/system/kube-scheduler.service /etc/systemd/system/etcd.service \
  /etc/systemd/system/podruntime.slice /etc/kube-lb
# DO NOT remove docker.service /var/lib/docker; the registry container/data is
# reset explicitly by the outer cleanup command after Kubernetes teardown.
systemctl daemon-reload 2>/dev/null
systemctl reset-failed 2>/dev/null
if findmnt -rn -o TARGET | grep -E "^/run/(cilium|calico)(/|$)" | grep -q . || test -e /run/cilium || test -e /run/calico; then
  echo "CTRL_WIPE_FAILED CNI mount residue on $(hostname)" >&2
  exit 1
fi
echo CTRL_WIPE_OK $(hostname)
'
if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
  -o ServerAliveInterval=5 -o ServerAliveCountMax=2 "ubuntu@$CONTROL" \
  "sudo timeout --signal=TERM --kill-after=10s 300s bash -lc $(printf '%q' "$CTRL_WIPE"); ctrl_rc=\$?; sudo docker exec local_registry sh -c 'rm -rf /var/lib/registry/docker' >/dev/null 2>&1 || true; sudo docker rm -f local_registry >/dev/null 2>&1 || true; sudo rm -rf /data/registry /var/snap/docker/common/kubeauto-registry; sudo sed -i '/registry.talkschool.cn/d; /master-aio.*flag by kubeauto/d' /etc/hosts; echo '127.0.0.1  registry.talkschool.cn' | sudo tee -a /etc/hosts >/dev/null; echo '192.168.47.138    master-aio # flag by kubeauto' | sudo tee -a /etc/hosts >/dev/null; if sudo systemctl is-active --quiet snap.docker.dockerd.service; then sudo systemctl restart snap.docker.dockerd.service; else sudo systemctl restart docker; fi; sudo rm -rf /usr/local/kubeauto/clusters/*; ls /usr/local/kubeauto/clusters || true; exit \$ctrl_rc"
then
  echo "ERROR wipe failed $CONTROL" >&2
  wipe_failed=1
fi
fi

if [[ "$wipe_failed" -ne 0 ]]; then
  echo "LAB_WIPE_FAILED" >&2
  exit 1
fi
echo "LAB_WIPE_DONE"
