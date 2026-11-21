#!/usr/bin/env bash
# The same physical six-node pool uses exactly the existing shared locks.
set -euo pipefail
[[ ( $# == 1 || $# == 2 ) && "$1" =~ ^v1366-harbor-[a-z0-9-]{1,32}$ ]] || exit 2
mode=${2:-qualify}
[[ "$mode" =~ ^(qualify|portable|final)$ ]] || exit 2
state=/var/tmp/kubeauto-v1366-lab
python=/usr/local/kubeauto/.venv/bin/python
cd "$state/target-product"
"$python" -B -m upgrade.lab.harbor_session plan --run-id "$1" --mode "$mode"
exec 9>/var/lock/kubeauto-ceph-priority.lock
flock -n 9
exec 7>/var/lock/kubeauto-ceph-compute.lock
flock -n 7
exec 8>/var/lock/kubeauto-redis-core-pool.lock
flock -n 8
exec 6>/var/lock/kubeauto-v1366-lab.lock
flock -n 6
exec "$python" -B -m upgrade.lab.harbor_session run --run-id "$1" --mode "$mode"
