#!/usr/bin/env bash
# Fixed .122.2 supervisor (shared leases live on this control). All six nodes stay leased through focused handoffs,
# diagnosis, and cleanup; losing a frontend connection cannot release the pool.
set -euo pipefail
mode="${1:?qualify or final required}" run_id="${2:?run ID required}"
[[ "$mode" == qualify || "$mode" == final ]] || exit 2
[[ "$run_id" =~ ^v13510-[a-z0-9-]{1,48}$ ]] || exit 2
state=/var/tmp/kubeauto-v13510-lab
root="$state/target-product"
python=/usr/local/kubeauto/.venv/bin/python
[[ -x "$python" && -f "$root/upgrade/lab/next_session.py" ]]
cd "$root"
# Read-only admission, before locks, ownership files, namespaces or lab changes.
"$python" -B -m upgrade.lab.next_session plan --mode "$mode" --run-id "$run_id"
exec 9>/var/lock/kubeauto-ceph-priority.lock
flock -n 9
exec 7>/var/lock/kubeauto-ceph-compute.lock
flock -n 7
exec 8>/var/lock/kubeauto-redis-core-pool.lock
flock -n 8
exec 6>/var/lock/kubeauto-v13510-lab.lock
flock -n 6
# The Python session refuses foreign ownership. The fixed durable helper wraps
# this entire shell, so exit/finalized appear only after the lease is released.
exec "$python" -B -m upgrade.lab.next_session run --mode "$mode" --run-id "$run_id"
