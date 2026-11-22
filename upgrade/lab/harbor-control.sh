#!/usr/bin/env bash
# Narrow frontend; staging/status/fetch remain shared with next-control.sh.
set -euo pipefail
[[ $# -ge 2 ]]
operation=$1 run_id=$2; shift 2
[[ "$run_id" =~ ^v1366-harbor-[a-z0-9-]{1,32}$ ]] || exit 2
if [[ "$run_id" == v1366-harbor-arm-* ]]; then
  exec bash upgrade/lab/harbor-arm-control.sh "$operation" "$run_id" "$@"
fi
control=root@192.168.122.2
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
case "$operation" in
  supply-paused-packages|resume-supplied-supervisor)
    [[ $# == 2 && "$1" =~ ^v1366-final-[a-z0-9-]+$ && "$2" =~ ^[1-9][0-9]*$ ]]
    parent=$1 supervisor=$2
    repair="/var/tmp/kubeauto-v1366-lab/runs/$run_id/package-fixture-repair"
    if [[ "$operation" == supply-paused-packages ]]; then
      "${SSH[@]}" "$control" bash -s -- "$run_id" "$repair" <<'PREPARE'
set -eu
/usr/local/kubeauto/.venv/bin/python -B - "$@" <<'PY'
import json,sys
from pathlib import Path
s=Path('/var/tmp/kubeauto-v1366-lab')
assert json.loads((s/'owner.json').read_text())['run_id']==sys.argv[1]
p=Path(sys.argv[2]); assert p.parent==s/'runs'/sys.argv[1]
p.mkdir(mode=0o700)
PY
PREPARE
      rsync -a --chmod=F600 -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
        upgrade/lab/harbor_session.py "$control:$repair/harbor_session.py"
    fi
    "${SSH[@]}" "$control" bash -s -- "$repair" "$operation" "$run_id" "$parent" "$supervisor" <<'SUPPLY'
set -eu
state=/var/tmp/kubeauto-v1366-lab
cd "$state/target-product"
export PYTHONPATH="$state/target-product"
/usr/local/kubeauto/.venv/bin/python -B "$1/harbor_session.py" "$2" \
  --run-id "$3" --parent-run "$4" --supervisor-pid "$5"
SUPPLY
    ;;
  status|fetch|follow|acceptance)
    [[ $# == 0 ]]
    exec bash upgrade/lab/next-control.sh "$operation" "$run_id"
    ;;
  plan|launch)
    [[ $# == 0 || $# == 1 ]]
    mode=${1:-qualify}
    [[ "$mode" =~ ^(qualify|portable|final)$ ]]
    "${SSH[@]}" "$control" bash -s -- "$operation" "$run_id" "$mode" <<'LAUNCH'
set -eu
state=/var/tmp/kubeauto-v1366-lab
python=/usr/local/kubeauto/.venv/bin/python
cd "$state/target-product"
"$python" -B -m upgrade.lab.harbor_session plan --run-id "$2" --mode "$3"
if test "$1" = launch; then
  test ! -e "$state/owner.json" && test ! -L "$state/owner.json"
  test ! -e "$state/runs/$2" && test ! -e "$state/$2.pid"
  nohup bash tests/helpers/run-durable-gate.sh "$state/$2" HARBOR_SESSION_EXIT \
    bash upgrade/lab/harbor-session.sh "$2" "$3" > "$state/$2.log" 2>&1 < /dev/null &
  echo "HARBOR_SESSION_LAUNCHED run_id=$2 pid=$!"
fi
LAUNCH
    ;;
  signal)
    [[ $# == 2 ]]
    phase=$1 action=$2
    [[ "$phase" =~ ^harbor-(source|migration|restore|new-install|portable|clean)$ ]]
    [[ "$action" =~ ^(advance|retry|cleanup)$ ]]
    "${SSH[@]}" "$control" bash -s -- "$run_id" "$phase" "$action" <<'SIGNAL'
set -eu
cd /var/tmp/kubeauto-v1366-lab/target-product
/usr/local/kubeauto/.venv/bin/python -B - "$@" <<'PY'
import json,sys
from upgrade.lab.next_session import STATE,write
run,phase,action=sys.argv[1:]
assert json.loads((STATE/'owner.json').read_text())['run_id']==run
path=STATE/'runs'/run/'action.json'
assert not path.exists() and not path.is_symlink()
write(path,{'run_id':run,'phase':phase,'action':action})
PY
SIGNAL
    ;;
  repair-portable-inputs|repair-portable-backup-inputs)
    [[ $# == 0 ]]
    "${SSH[@]}" "$control" bash -s -- "$run_id" "$operation" <<'BOUNDARY'
set -eu
cd /var/tmp/kubeauto-v1366-lab/target-product
/usr/local/kubeauto/.venv/bin/python -B - "$1" "$2" <<'PY'
import hashlib,json,sys
from pathlib import Path
s=Path('/var/tmp/kubeauto-v1366-lab'); run=sys.argv[1]; r=s/'runs'/run
owner=json.loads((s/'owner.json').read_text())
assert owner['run_id']==run and owner['mode']=='portable'
assert json.loads((r/'harbor-source.status.json').read_text())['rc']==0
failed=json.loads((r/'harbor-portable.status.json').read_text())
assert failed['rc']==1 and failed['attempt']==1
assert 'NEXT_SESSION_WAIT phase=harbor-portable failed=true lease=held' in (s/(run+'.log')).read_text()
paths=['upgrade/product/library/harbor_migration.py','upgrade/lab/harbor_session.py',
       'upgrade/lab/harbor-control.sh','upgrade/lab/next_session.py','upgrade/lab/ingress_qualification.py',
       'upgrade/tests/test_harbor_migration.py','upgrade/tests/test_harbor_session.py',
       'upgrade/tests/test_next_session.py','upgrade/tests/test_ingress_qualification.py']
if sys.argv[2]=='repair-portable-backup-inputs':
    # Admission uses the previous product and does not mutate the nodes.
    for suffix in ['exit','finalized']:
        assert (r/('harbor-portable-repair.attempt-1.'+suffix)).read_text().strip()=='1'
    assert 'the stopped queue has no valid RDB snapshot header' in (r/'harbor-portable-repair.attempt-1.log').read_text()
    archive=r/'valkey-repair-original-inputs'
else:archive=r/'export-repair-original-inputs'
archive.mkdir(mode=0o700)
hashes={}
for name in paths:
    data=Path(name).read_bytes();p=archive/name;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    p.write_bytes(data);p.chmod(0o600);hashes[name]=hashlib.sha256(data).hexdigest()
(archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
PY
BOUNDARY
    rsync -azR --no-owner --no-group --chmod=F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
      upgrade/product/library/harbor_migration.py upgrade/lab/harbor_session.py \
      upgrade/lab/harbor-control.sh upgrade/lab/next_session.py upgrade/lab/ingress_qualification.py \
      upgrade/tests/test_harbor_migration.py upgrade/tests/test_harbor_session.py \
      upgrade/tests/test_next_session.py upgrade/tests/test_ingress_qualification.py \
      "$control:/var/tmp/kubeauto-v1366-lab/target-product/"
    ;;
  repair-new-install|repair-cleanup|repair-portable|repair-portable-backup)
    [[ $# == 0 || ( $# == 1 && "$operation" == repair-cleanup ) ]]
    attempt=${1:-1}
    [[ "$attempt" =~ ^[123]$ ]]
    "${SSH[@]}" "$control" bash -s -- "$run_id" "$operation" "$attempt" <<'REPAIR'
set -eu
state=/var/tmp/kubeauto-v1366-lab
cd "$state/target-product"
if test "$2" = repair-new-install; then
/usr/local/kubeauto/.venv/bin/python -B - "$1" <<'PY'
import sys
from upgrade.lab.harbor_session import HarborSession
HarborSession(sys.argv[1]).repair_new_install_boundary()
PY
fi
prefix="$state/runs/$1/harbor-${2#repair-}-repair.attempt-$3"
if test "$3" != 1; then
  previous="$state/runs/$1/harbor-${2#repair-}-repair.attempt-$(($3-1))"
  test "$(cat "$previous.exit")" = 1
  test "$(cat "$previous.finalized")" = 1
fi
test ! -e "$prefix.log" && test ! -e "$prefix.pid"
nohup bash tests/helpers/run-durable-gate.sh "$prefix" HARBOR_NEW_INSTALL_REPAIR_EXIT \
  /usr/local/kubeauto/.venv/bin/python -B -m upgrade.lab.harbor_session "$2" --run-id "$1" \
  > "$prefix.log" 2>&1 < /dev/null &
echo "HARBOR_NEW_INSTALL_REPAIR_LAUNCHED run_id=$1 pid=$!"
REPAIR
    ;;
  *) exit 2 ;;
esac
