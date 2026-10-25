#!/usr/bin/env bash
# Complete ownership acceptance from a verified fresh or post-uninstall core.
set -Eeuo pipefail
umask 077

verify_stage() {
  "${PY:?}" - "$1" "$2" <<'PY'
import os
from pathlib import Path
import re
import stat
import sys

directory = Path(sys.argv[1])
stage = sys.argv[2]
stages = (
    'ownership-csi-refusal-qualification',
    'ownership-preinstall-refusal-qualification',
    'install',
    'ownership-class-refusal-qualification',
    'ownership-controller-refusal-qualification',
    'ownership-request-refusal-qualification',
)
if stage not in stages or directory.is_symlink():
    raise ValueError('unknown or unsafe ownership stage')
prefix = directory / stage
for suffix in ('.exit', '.finalized', '.log'):
    path = prefix.with_suffix(suffix)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise ValueError('ownership evidence must be owner-private regular files')
    if suffix != '.log' and path.read_text().strip() != '0':
        raise ValueError('ownership stage did not finalize successfully')
lines = prefix.with_suffix('.log').read_text().splitlines()
failure = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')
if any(failure.match(line) for line in lines):
    raise ValueError('ownership stage contains failure markers')
marker = 'LONGHORN_' + stage.upper().replace('-', '_')
for expected in (marker + '_SCOPED_CLEAN_PASS', marker + '_PASS', marker + '_EXIT rc=0'):
    if lines.count(expected) != 1:
        raise ValueError('ownership stage terminal markers differ')
print('LONGHORN_OWNERSHIP_STAGE_VERIFIED stage=' + stage + ' durable_rc=0 finalized=0')
PY
}

ownership_main() {
  [[ "$#" == 1 && "$1" == ownership ]] || return 2
  BASE=/usr/local/kubeauto
  cd "$BASE"
  [[ "${LONGHORN_PARENT_LEASE_HELD:-no}" == yes ]] || return 1
  for entry in '9 kubeauto-ceph-priority.lock' '7 kubeauto-ceph-compute.lock' '8 kubeauto-redis-core-pool.lock'; do
    read -r fd filename <<< "$entry"
    [[ "/proc/self/fd/$fd" -ef "/var/lock/$filename" ]] || return 1
    flock -n "$fd" || return 1
  done
  PY="$BASE/.venv/bin/python"
  work="$(mktemp -d /var/tmp/kubeauto-longhorn-ownership.XXXXXX)"
  chmod 0700 "$work"
  phase=preinstall
  ownership_exit() {
    local rc=$?
    trap - EXIT INT TERM
    if [[ "$rc" != 0 ]]; then
      printf 'rc=%s phase=%s\n' "$rc" "$phase" > "$work/first-failure.status"
      echo "LONGHORN_FIRST_FAILURE phase=ownership-$phase rc=$rc diagnostics=$work" >&2
    fi
    echo "LONGHORN_OWNERSHIP_DURABLE_DIRECTORY path=$work"
    exit "$rc"
  }
  trap ownership_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # The first two probes verify a clean never-installed or post-uninstall core.
  # Both retain the real absence, receipt and canary ownership checks before
  # the original product install and installed-state probes.
  for phase in ownership-csi-refusal-qualification ownership-preinstall-refusal-qualification install \
      ownership-class-refusal-qualification ownership-controller-refusal-qualification \
      ownership-request-refusal-qualification; do
    marker="LONGHORN_${phase^^}"
    marker="${marker//-/_}"
    echo "LONGHORN_OWNERSHIP_STAGE_START stage=$phase"
    bash "$BASE/tests/helpers/run-durable-gate.sh" "$work/$phase" "${marker}_EXIT" \
      bash "$BASE/tests/helpers/longhorn-live.sh" "$phase" 2>&1 | tee "$work/$phase.log"
    verify_stage "$work" "$phase"
  done
  echo LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS
  echo LONGHORN_OWNERSHIP_PASS
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  ownership_main "$@"
fi
