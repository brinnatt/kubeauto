#!/usr/bin/env bash
# Executed on .47.130 through the fixed runner. A durable guardian retains
# its existing shared-pool/control locks until the matching .122.2 run exits.
set -Eeuo pipefail
run_id="${1:?run ID required}" durable_sha="${2:?durable helper identity required}"
[[ "$run_id" =~ ^[a-f0-9]{32}$ && "$durable_sha" =~ ^[a-f0-9]{64}$ ]] || exit 2
actual=$(sha256sum /usr/local/kubeauto/tests/helpers/run-durable-gate.sh)
[[ "${actual%% *}" == "$durable_sha" ]]
# Prove cross-control authentication before handing any source run to this
# guardian. The fixed lab bootstrap owns key provisioning, not this helper.
ssh -n -o BatchMode=yes -o ConnectTimeout=10 root@192.168.122.2 true
exec 9>/var/lock/kubeauto-ceph-priority.lock
flock -n 9
exec 7>/var/lock/kubeauto-ceph-compute.lock
flock -n 7
exec 8>/var/lock/kubeauto-redis-core-pool.lock
flock -n 8
for file in /tmp/kubeauto-*.pid; do
  test -f "$file" || continue
  pid=$(cat "$file")
  case "$pid" in ""|*[!0-9]*) exit 1;; esac
  if kill -0 "$pid" 2>/dev/null; then exit 1; fi
done
supervise_source() {
  local run_id="$1" state='' started=no attempts=0
  while :; do
    state=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
      root@192.168.122.2 bash -s -- "$run_id" <<'SOURCE_STATUS'
set -eu
if ! test -f /tmp/kubeauto-elasticsearch-live.log || ! grep -q "^ELASTICSEARCH_MODULE_BEGIN .*run_id=$1$" /tmp/kubeauto-elasticsearch-live.log; then
  echo waiting
elif test -f /tmp/kubeauto-elasticsearch-gate.exit && test -f /tmp/kubeauto-elasticsearch-gate.finalized; then
  rc=$(cat /tmp/kubeauto-elasticsearch-gate.exit)
  finalized=$(cat /tmp/kubeauto-elasticsearch-gate.finalized)
  case "$rc:$finalized" in *[!0-9:]*|:*) exit 1;; esac
  if test "$finalized" != "$rc"; then echo done:1; else echo "done:$rc"; fi
else
  echo running
fi
SOURCE_STATUS
    ) || state=unreachable
    printf 'ELASTICSEARCH_SECONDARY_LEASE_HEARTBEAT run_id=%s state=%s\n' "$run_id" "$state"
    case "$state" in
      running) started=yes ;;
      done:*) return "${state#done:}" ;;
      waiting)
        attempts=$((attempts+1))
        # No source run ever started, so failed sync/launch can release safely.
        if [[ "$started" == no && "$attempts" -ge 24 ]]; then return 1; fi ;;
      unreachable) : ;; # Fail closed: a lost route does not release active hosts.
      *) return 1 ;;
    esac
    sleep 5
  done
}
export -f supervise_source
nohup bash /usr/local/kubeauto/tests/helpers/run-durable-gate.sh \
  /tmp/kubeauto-elasticsearch-secondary ELASTICSEARCH_SECONDARY_EXIT \
  bash -c 'supervise_source "$1"' bash "$run_id" \
  >/tmp/kubeauto-elasticsearch-secondary.log 2>&1 </dev/null &
printf 'ELASTICSEARCH_SECONDARY_LEASE_READY\n'
