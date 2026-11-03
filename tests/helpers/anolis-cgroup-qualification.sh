#!/usr/bin/env bash
# Environment-only qualification. Never installs Kubernetes or changes CPU policy.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 root@192.168.47.141)
# FHS: /var/tmp survives reboot; /tmp may be a tmpfs cleared at boot.
REMOTE=/var/tmp/kubeauto-anolis-cgroup-fixture.py
RUN_ID=cpu-$(date +%Y%m%dT%H%M%S)-$$
owns_fixture=0
prepared=0
restored=0
restore_fixture() {
  local restore_rc=0 output attempt
  (( owns_fixture )) || return 0
  (( ! restored )) || return 0
  "${SSH[@]}" 'test -f /var/tmp/kubeauto-anolis-rt-fixture.json' || restore_rc=$?
  if (( restore_rc == 1 )); then
    (( ! prepared )) || return 1
    return 0
  fi
  (( restore_rc == 0 )) || return "$restore_rc"
  output=$("${SSH[@]}" "python3 $REMOTE restore $RUN_ID") || restore_rc=$?
  if (( restore_rc == 0 )); then
    [[ "$output" == ANOLIS_CPU_RESTORE_PASS ]] || return 1
    restored=1
    echo "$output"
    "${SSH[@]}" "rm -f $REMOTE"
    return 0
  fi
  (( restore_rc == 42 )) || return "$restore_rc"
  [[ "$output" == ANOLIS_CPU_RESTORE_REBOOT_REQUIRED ]] || return 1
  echo 'ANOLIS_CPU_RESTORE_WAIT reboot=required node=192.168.47.141'
  # A disconnect is possible after systemd accepts the reboot. Only a new boot
  # verified by the original journal can complete restoration.
  "${SSH[@]}" 'systemctl reboot' || true
  for attempt in $(seq 1 60); do
    restore_rc=0
    output=$("${SSH[@]}" "python3 $REMOTE restore $RUN_ID" 2>/dev/null) || restore_rc=$?
    if (( restore_rc == 0 )) && [[ "$output" == ANOLIS_CPU_RESTORE_PASS ]]; then
      restored=1
      echo "$output"
      "${SSH[@]}" "rm -f $REMOTE"
      return 0
    fi
    echo "ANOLIS_CPU_RESTORE_WAIT attempt=$attempt/60 next_check=5s rc=$restore_rc"
    sleep 5
  done
  echo ANOLIS_CPU_RESTORE_FAIL >&2
  return 1
}
finish() {
  local initial_rc=$? recovery_rc=0
  trap - EXIT INT TERM
  restore_fixture || recovery_rc=$?
  (( recovery_rc == 0 )) || exit 1
  exit "$initial_rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bash "$ROOT/tests/helpers/lab-wipe-nodes.sh" --verify --rocky-only 192.168.47.141
scp -o BatchMode=yes -o ConnectTimeout=10 "$ROOT/tests/helpers/lab-anolis-cgroup.py" root@192.168.47.141:"$REMOTE"
"${SSH[@]}" 'test ! -e /var/tmp/kubeauto-anolis-rt-fixture.json'
owns_fixture=1
"${SSH[@]}" "python3 $REMOTE prepare $RUN_ID"
prepared=1
restore_fixture
trap - EXIT INT TERM
echo ANOLIS_CPU_QUALIFICATION_PASS
