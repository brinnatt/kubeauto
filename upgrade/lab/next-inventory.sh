#!/usr/bin/env bash
# Read-only prerequisite inventory; no lease, cleanup or cluster mutation.
set -euo pipefail
[[ $# == 0 || ( $# == 1 && "$1" == --control-only ) ]] || exit 2
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
for control in 192.168.47.130 192.168.122.2; do
  printf 'CONTROL=%s\n' "$control"
"${SSH[@]}" "root@$control" bash -s <<'CONTROL_PROBE'
set -eu
hostname
for path in /usr/local/kubeauto/.venv/bin/python /usr/local/kubeauto/down /usr/local/kubeauto/kube-bin /usr/local/kubeauto/extra-bin /root/.ssh; do test ! -e "$path" || ls -ld "$path"; done
for pattern in /usr/local/kubeauto/down/*bin* /usr/local/kubeauto/.venv/bin/ansible-playbook; do test ! -e "$pattern" || ls -lh "$pattern"; done
/usr/local/kubeauto/.venv/bin/python -c "import sys,importlib.util; print(sys.version); print({m:importlib.util.find_spec(m) is not None for m in ['ansible','kubernetes','ansible_runner','taskflow']})"
command -v ansible-playbook || true
if command -v ansible-playbook >/dev/null; then ansible-playbook --version | head -2; fi
unshare --mount --fork true
for unit in kubelet etcd containerd docker; do systemctl show "$unit" --property=ActiveState --value; done
CONTROL_PROBE
done
if [[ "${1:-}" == --control-only ]]; then exit 0; fi
for host in 192.168.122.217 192.168.122.243 192.168.122.246 192.168.122.193 192.168.122.210 192.168.122.216; do
  printf 'HOST=%s\n' "$host"
  "${SSH[@]}" -n "root@$host" 'set -eu
uname -r
stat -fc %T /sys/fs/cgroup
for unit in kubelet etcd containerd docker; do systemctl show "$unit" --property=ActiveState --value; done
for path in /etc/kubernetes /var/lib/etcd /var/lib/kubelet /opt/local-path-provisioner; do if test -e "$path"; then ls -ld "$path"; fi; done
free -m | head -2
'
done
printf 'NEXT_HOP_READONLY_INVENTORY_PASS mutation=none lease=none\n'
