#!/usr/bin/env bash
# Read-only ownership/capacity inventory; this does not acquire a host lease.
set -euo pipefail
ceph_targets=(
  root@192.168.122.135 root@192.168.122.40 root@192.168.122.72
  root@192.168.122.212 root@192.168.122.165 root@192.168.122.238
)
targets=(
  root@192.168.47.130 root@192.168.47.131 root@192.168.47.132
  root@192.168.47.133 root@192.168.47.134 root@192.168.47.135
  root@192.168.47.136 root@192.168.47.137 ubuntu@192.168.47.138
  brinnatt@192.168.47.128 root@192.168.47.141 root@192.168.47.142 root@192.168.47.143
  root@192.168.122.2 root@192.168.122.217 root@192.168.122.243 root@192.168.122.246
  root@192.168.122.193 root@192.168.122.210 root@192.168.122.216
  "${ceph_targets[@]}"
)
case "${1:-}" in
  '') (( $# == 0 )) || exit 2;;
  --ceph-only) (( $# == 1 )) || exit 2; targets=("${ceph_targets[@]}");;
  *) echo 'Usage: startup-pool-inventory.sh [--ceph-only]' >&2; exit 2;;
esac
work=$(mktemp -d /tmp/kubeauto-startup-inventory.XXXXXX)
trap 'rm -rf "$work"' EXIT
probe() {
  local target=$1 command='bash -s'
  [[ "$target" == root@* ]] || command='sudo -n bash -s'
  timeout --signal=TERM --kill-after=5s 45s ssh -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o ConnectTimeout=8 "$target" "$command" <<'REMOTE'
set -u
hostname
. /etc/os-release
printf 'OS=%s VERSION=%s KERNEL=%s\n' "$ID" "$VERSION_ID" "$(uname -r)"
awk '/MemTotal:|MemAvailable:/ {print}' /proc/meminfo
for service in kubelet etcd containerd docker ceph.target; do
  printf '%s=' "$service"; systemctl is-active "$service" 2>/dev/null || true
done
systemctl list-units 'ceph*' --state=active --no-legend --no-pager
lslocks --noheadings --output PID,COMMAND,PATH 2>/dev/null || true
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
for file in /tmp/kubeauto-*.pid; do
  test -f "$file" || continue
  pid=$(cat "$file")
  case "$pid" in ''|*[!0-9]*) continue;; esac
  if kill -0 "$pid" 2>/dev/null; then
    printf 'ACTIVE_JOB=%s pid=%s\n' "$file" "$pid"
    ps -p "$pid" -o comm=,lstart=
  fi
done
python3 - <<'PY'
from pathlib import Path
import hashlib, re
root=Path('/usr/local/kubeauto/clusters')
for inventory in sorted(root.glob('*/hosts')):
    nodes=[]
    for line in inventory.read_text().splitlines():
        address=line.split()[0] if line.split() else ''
        if re.fullmatch(r'192\.168\.(47|122)\.\d+', address): nodes.append(address)
    print('CLUSTER_INVENTORY',inventory.parent.name,'hosts_sha256='+hashlib.sha256(inventory.read_bytes()).hexdigest(),
          'nodes='+','.join(sorted(set(nodes))))
PY
if test -s /root/.kube/config && command -v kubectl >/dev/null 2>&1; then
  kubectl --request-timeout=8s get namespace kube-system -o jsonpath='cluster_namespace_uid={.metadata.uid}{"\n"}' || true
  kubectl --request-timeout=8s get nodes -o wide || true
  kubectl --request-timeout=8s get pods -A -o wide || true
fi
REMOTE
}
failures=0
for ((start=0; start<${#targets[@]}; start+=4)); do
  pids=()
  for target in "${targets[@]:start:4}"; do
    probe "$target" >"$work/${target#*@}" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then failures=$((failures+1)); fi
  done
  for target in "${targets[@]:start:4}"; do
    printf 'STARTUP_INVENTORY target=%s\n' "$target"
    cat "$work/${target#*@}"
  done
done
echo "STARTUP_INVENTORY_COMPLETE unreachable_or_timeout=$failures mutation=none lease=none"
(( failures == 0 ))
