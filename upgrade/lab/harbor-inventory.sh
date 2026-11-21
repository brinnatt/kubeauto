#!/usr/bin/env bash
# Read-only Harbor host admission inventory. Does not lease or change a host.
set -euo pipefail
[[ $# == 0 ]] || exit 2
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
for target in root@192.168.47.137 ubuntu@192.168.47.138; do
  printf 'HARBOR_INVENTORY target=%s\n' "$target"
  command=(bash -s)
  if [[ "$target" != root@* ]]; then command=(sudo -n bash -s); fi
  "${SSH[@]}" "$target" "${command[@]}" <<'PROBE'
set -eu
hostname
uname -m
uname -r
cat /etc/os-release
nproc
free -m | head -2
df -Pm /var/tmp /var/lib
for unit in docker containerd kubelet etcd; do
  printf '%s=' "$unit"
  systemctl show "$unit" --property=ActiveState --value
done
lslocks --noheadings --output PID,COMMAND,PATH
for path in /etc/kubernetes /var/lib/etcd /var/lib/kubelet /var/lib/docker /var/data/harbor /var/tmp/kubeauto-harbor-v1366; do
  if test -e "$path" || test -L "$path"; then stat -c '%F %u:%g:%a %n' "$path"; fi
done
if command -v docker >/dev/null && systemctl is-active --quiet docker; then
  docker ps -a --format '{{.ID}} {{.Names}} {{.Status}}'
fi
PROBE
done
echo HARBOR_INVENTORY_PASS mutation=none lease=none
