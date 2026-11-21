#!/usr/bin/env bash
# Fixed frontend for the 1.35.10 -> 1.36.6 lab only. No arbitrary host/command.
set -euo pipefail
operation="${1:?operation required}"; shift
control=root@192.168.122.2
state=/var/tmp/kubeauto-v1366-lab
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
case "$operation" in
  stage-status)
    [[ $# == 0 ]]
    "${SSH[@]}" "$control" bash -s <<'STAGING_STATUS'
set -eu
state=/var/tmp/kubeauto-v1366-lab
stat -c '%u:%g:%a %F' "$state"
for path in "$state/owner.json" "$state/input-manifest.json"; do
  if test -e "$path" || test -L "$path"; then ls -ld "$path"; fi
done
du -sh "$state"
STAGING_STATUS
    ;;
  stage)
    [[ $# == 1 && -d "$1/source-product" && -d "$1/target-product" && -f "$1/input-manifest.json" ]]
    # Staging never changes a cluster. Refuse active ownership before rsync;
    # the session's real fd6 lock excludes transfer while a pool is owned.
    manifest_sha=$(sha256sum "$1/input-manifest.json"); manifest_sha=${manifest_sha%% *}
    "${SSH[@]}" "$control" bash -s -- "$manifest_sha" <<'STAGE'
set -eu
state=/var/tmp/kubeauto-v1366-lab
test ! -L "$state"
mkdir -p "$state"
test ! -e "$state/owner.json" && test ! -L "$state/owner.json"
# Repair only the interrupted transfer of this exact manifest. Earlier rsync
# -a preserved the developer uid on the staging root; it never owned a cluster.
if test "$(stat -c %u "$state")" != 0; then
  test -f "$state/input-manifest.json" && test ! -L "$state/input-manifest.json"
  actual=$(sha256sum "$state/input-manifest.json")
  test "${actual%% *}" = "$1"
  chown root:root "$state"
fi
test "$(stat -c %u "$state")" = 0
chmod 0700 "$state"
test ! -e "$state/owner.json" && test ! -L "$state/owner.json"
STAGE
    rsync -aHz --no-owner --no-group --partial --info=progress2 --safe-links --chmod=D700,F600 --exclude=owner.json --exclude=runs/ --exclude=clusters/ \
      --rsync-path='flock -n /var/lock/kubeauto-v1366-lab.lock sh -c '\''test ! -e /var/tmp/kubeauto-v1366-lab/owner.json && exec rsync "$@"'\'' staging-rsync' \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' "$1/" "$control:$state/"
    "${SSH[@]}" "$control" bash -s <<'VERIFY'
set -eu
state=/var/tmp/kubeauto-v1366-lab
test ! -e "$state/owner.json"
for product in source-product target-product; do
  find "$state/$product/kube-bin" "$state/$product/extra-bin" -type d -exec chmod 0755 {} +
  find "$state/$product/kube-bin" "$state/$product/extra-bin" -type f -exec chmod 0755 {} +
  if test -d "$state/$product/docker-bin"; then
    find "$state/$product/docker-bin" -type d -exec chmod 0755 {} +
    find "$state/$product/docker-bin" -type f -exec chmod 0755 {} +
  fi
done
cd "$state/target-product"
/usr/local/kubeauto/.venv/bin/python -B -c 'from pathlib import Path; from upgrade.lab.next_session import verify_inputs; verify_inputs(Path("/var/tmp/kubeauto-v1366-lab"), Path.cwd()); print("NEXT_SESSION_STAGED_INPUTS_PASS")'
VERIFY
    ;;
  qualify-control)
    [[ $# == 0 ]]
    "${SSH[@]}" "$control" bash -s <<'ISOLATION'
set -eu
state=/var/tmp/kubeauto-v1366-lab
test ! -e "$state/owner.json"
test ! -e /root/.kube/kubeauto-v1366-isolation-probe
before=absent
if test -f /root/.kube/config; then before=$(sha256sum /root/.kube/config); fi
bash "$state/target-product/upgrade/lab/isolate-control.sh" "$state" bash -ec '
  test -d /root/.ssh
  findmnt -n -o OPTIONS /root/.ssh | tr , "\n" | grep -Fx ro
  mkdir -p /root/.kube
  printf "isolated\n" > /root/.kube/kubeauto-v1366-isolation-probe
'
test ! -e /root/.kube/kubeauto-v1366-isolation-probe
after=absent
if test -f /root/.kube/config; then after=$(sha256sum /root/.kube/config); fi
test "$before" = "$after"
test "$(cat "$state/control-home/.kube/kubeauto-v1366-isolation-probe")" = isolated
rm "$state/control-home/.kube/kubeauto-v1366-isolation-probe"
echo NEXT_SESSION_CONTROL_ISOLATION_PASS
ISOLATION
    ;;
  acceptance)
    [[ $# == 1 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    # Focused handoffs update acceptance records only. Product/staged binaries
    # remain untouched while the remote session retains its shared-pool locks.
    "${SSH[@]}" "$control" bash -s -- "$1" <<'ACCEPT_OWNER'
set -eu
cd /var/tmp/kubeauto-v1366-lab/target-product
/usr/local/kubeauto/.venv/bin/python -B - "$1" <<'PY'
import json,sys
from upgrade.lab.next_session import STATE
assert json.loads((STATE/'owner.json').read_text())['run_id']==sys.argv[1]
PY
ACCEPT_OWNER
    .venv/bin/python -B upgrade/validate.py upgrade/v1.36.6/test-matrix.yaml
    rsync -az --no-owner --no-group --safe-links --chmod=D700,F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
      upgrade/v1.36.6/evidence/ "$control:$state/target-product/upgrade/v1.36.6/evidence/"
    rsync -az --no-owner --no-group --chmod=F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
      upgrade/v1.36.6/test-matrix.yaml "$control:$state/target-product/upgrade/v1.36.6/test-matrix.yaml"
    "${SSH[@]}" "$control" "cd $state/target-product && /usr/local/kubeauto/.venv/bin/python -B upgrade/validate.py upgrade/v1.36.6/test-matrix.yaml"
    ;;
  repair-admission|repair-calico-defaults)
    [[ $# == 1 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    # Exact proven repairs, only at the healthy source handoff.
    # A created mutation journal makes this path refuse before any code copy.
    "${SSH[@]}" "$control" bash -s -- "$1" <<'REPAIR_BOUNDARY'
set -eu
cd /var/tmp/kubeauto-v1366-lab/target-product
/usr/local/kubeauto/.venv/bin/python -B - "$1" <<'PY'
import json,sys
from pathlib import Path
from upgrade.lab.next_session import STATE
run=sys.argv[1]
plan=json.loads((STATE/'owner.json').read_text())
assert plan['run_id']==run
evidence=STATE/'runs'/run
assert json.loads((evidence/'source.status.json').read_text())['rc']==0
assert not (evidence/'control.status.json').exists()
assert not list(evidence.glob('control.attempt-*'))
assert not (Path('/var/tmp/kubeauto-upgrade-'+plan['clusters'][0])/'journal').exists()
assert 'NEXT_SESSION_WAIT phase=source failed=false lease=held' in (STATE/(run+'.log')).read_text()
PY
REPAIR_BOUNDARY
    if [[ "$operation" == repair-calico-defaults ]]; then
      repair_files=(upgrade/product/calico_stage.py upgrade/tests/test_calico_stage.py
        upgrade/tests/fixtures/calico_kubectl.py upgrade/lab/next-control.sh upgrade/tests/test_next_session.py)
    else
      repair_files=(upgrade/product/next_api.py upgrade/lab/next-control.sh upgrade/lab/next_session.py
        upgrade/tests/test_next_api.py upgrade/tests/test_next_session.py)
    fi
    rsync -azR --no-owner --no-group --chmod=F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
      "${repair_files[@]}" \
      "$control:$state/target-product/"
    ;;
  repair-source-pack)
    [[ $# == 1 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    "${SSH[@]}" "$control" bash -s -- "$1" <<'REPAIR_PACK'
set -eu
/usr/local/kubeauto/.venv/bin/python -B - "$1" <<'PY'
import ast,hashlib,json,os,shutil,sys
from pathlib import Path
state=Path('/var/tmp/kubeauto-v1366-lab')
run=sys.argv[1]
assert not state.is_symlink()
plan=json.loads((state/'owner.json').read_text())
assert plan['run_id']==run
evidence=state/'runs'/run
status=json.loads((evidence/'source.status.json').read_text())
reason=status['reason']
# copytree can aggregate SameFileError's string argument as individual
# characters. Normalize that exact stdlib representation for diagnosis only.
if reason.startswith('['):
    characters=ast.literal_eval(reason)
    assert isinstance(characters,list) and all(isinstance(c,str) and len(c)==1 for c in characters)
    reason=''.join(characters)
assert status['rc']==1 and 'are the same file' in reason
assert (evidence/'source-health.json').is_file() and (evidence/'ledger-namespace.json').is_file()
assert not (evidence/'prepared-boundary.json').exists()
assert not (Path('/var/tmp/kubeauto-upgrade-'+plan['clusters'][0])/'journal').exists()
receipt=evidence/'source-pack-inode-repair.json'
assert not receipt.exists()
manifest=json.loads((state/'input-manifest.json').read_text())
def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
files=[]
for relative,expected in manifest['trees']['target-product'].items():
    if not relative.startswith('extra-bin/'):continue
    assert not Path(relative).is_absolute() and '..' not in Path(relative).parts
    source=state/'target-product'/relative
    destination=state/'source-product'/relative
    assert not source.is_symlink() and digest(source)==expected
    if destination.exists() and os.path.samefile(source,destination):files.append((source,destination,expected))
assert files
for source,destination,expected in files:
    temporary=destination.with_name(destination.name+'.inode-repair')
    assert not temporary.exists()
    shutil.copy2(source,temporary);temporary.replace(destination)
    assert digest(destination)==expected and not os.path.samefile(source,destination)
with receipt.open('x') as stream:
    json.dump({'run_id':run,'scope':'controller input inodes only; no cluster mutation',
        'files':[{'path':str(p.relative_to(state)),'sha256':h} for _,p,h in files]},stream,indent=2)
print('NEXT_SOURCE_PACK_INODE_REPAIR_PASS count='+str(len(files)))
PY
REPAIR_PACK
    ;;
  qualify-handoff)
    [[ $# == 1 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    "${SSH[@]}" "$control" "bash $state/target-product/upgrade/lab/next-session.sh final $1 check-handoff"
    ;;
  plan|launch)
    [[ $# == 2 && ( "$1" == qualify || "$1" == final ) && "$2" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    mode="$1" run_id="$2"
    if [[ "$operation" == plan ]]; then
      "${SSH[@]}" "$control" "cd $state/target-product && /usr/local/kubeauto/.venv/bin/python -B -m upgrade.lab.next_session plan --mode $mode --run-id $run_id"
    else
      "${SSH[@]}" "$control" bash -s -- "$mode" "$run_id" <<'LAUNCH'
set -eu
state=/var/tmp/kubeauto-v1366-lab
test ! -e "$state/owner.json"
test ! -e "$state/runs/$2" && test ! -e "$state/$2.pid"
cd "$state/target-product"
/usr/local/kubeauto/.venv/bin/python -B -m upgrade.lab.next_session plan --mode "$1" --run-id "$2"
nohup bash tests/helpers/run-durable-gate.sh "$state/$2" NEXT_SESSION_EXIT \
  bash upgrade/lab/next-session.sh "$1" "$2" > "$state/$2.log" 2>&1 < /dev/null &
echo "NEXT_SESSION_LAUNCHED run_id=$2 pid=$!"
LAUNCH
    fi
    ;;
  status|fetch|follow)
    [[ $# == 1 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    run_id="$1"
    if [[ "$operation" == fetch ]]; then
      destination="upgrade/v1.36.6/private/$run_id"
      mkdir -p "$destination"; chmod 0700 "$destination"
      archive_filter=()
      # Full stopped recovery archives remain on the private controller with
      # per-file SHA256 manifests. Status collection must not duplicate GBs.
      if [[ "$run_id" == v1366-harbor-* ]]; then archive_filter=(--exclude='*.node-state/***'); fi
      rsync -a "${archive_filter[@]}" -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' "$control:$state/runs/$run_id/" "$destination/"
    elif [[ "$operation" == follow ]]; then
      "${SSH[@]}" "$control" "tail -n 50 -f $state/$run_id.log"
    else
      "${SSH[@]}" "$control" bash -s -- "$run_id" <<'STATUS'
set -eu
state=/var/tmp/kubeauto-v1366-lab
for suffix in exit finalized; do
  file="$state/$1.$suffix"
  if test -f "$file"; then printf '%s=' "$suffix"; cat "$file"; fi
done
for file in "$state/runs/$1/"*.status.json; do
  if test -f "$file"; then
    /usr/local/kubeauto/.venv/bin/python -B - "$file" <<'PY'
import json,sys
value=json.load(open(sys.argv[1]))
if 'reason' in value:value['reason']=value['reason'][:200]
print(json.dumps(value,ensure_ascii=False))
PY
  fi
done
tail -n 12 "$state/$1.log" | cut -c 1-500
STATUS
    fi
    ;;
  qualify-ingress)
    [[ ( $# == 1 || $# == 2 ) && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    run_id=$1
    attempt=${2:-1}
    [[ "$attempt" =~ ^[123]$ ]]
    # Add only the independently qualified probe and its receipt; frozen
    # Kubernetes product inputs and the running session remain untouched.
    rsync -azR --no-owner --no-group --chmod=F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o ProxyJump=root@192.168.47.130' \
      upgrade/lab/ingress_qualification.py upgrade/tests/test_ingress_qualification.py \
      upgrade/v1.36.6/acceptance-plan.yaml \
      upgrade/v1.36.6/evidence/ingress-focused-contracts.json \
      upgrade/v1.36.6/evidence/ingress-registry-supply.json \
      upgrade/v1.36.6/evidence/raw/local-qualification/ingress-focused-contracts.log \
      upgrade/v1.36.6/evidence/raw/local-qualification/ingress-config-handoff-contracts.log \
      upgrade/v1.36.6/evidence/raw/local-qualification/ingress-frontend-handoff-contracts.log \
      "$control:$state/target-product/"
    "${SSH[@]}" "$control" bash -s -- "$run_id" "$attempt" <<'INGRESS'
set -eu
state=/var/tmp/kubeauto-v1366-lab
python=/usr/local/kubeauto/.venv/bin/python
cd "$state/target-product"
"$python" -B - "$1" "$2" <<'PY'
import sys
from upgrade.lab.ingress_qualification import boundary,retry_boundary
from upgrade.lab.next_session import Session
session=Session('qualify',sys.argv[1])
boundary(session)
retry_boundary(session,int(sys.argv[2]))
PY
prefix=ingress
if test "$2" != 1; then prefix="ingress-attempt-$2"; fi
mkdir "$state/runs/$1/$prefix-launch"
nohup bash tests/helpers/run-durable-gate.sh "$state/runs/$1/$prefix" UPGRADE_INGRESS_EXIT \
  "$python" -B -m upgrade.lab.ingress_qualification --run-id "$1" --attempt "$2" \
  > "$state/runs/$1/$prefix.log" 2>&1 < /dev/null &
echo "UPGRADE_INGRESS_LAUNCHED run_id=$1 attempt=$2 pid=$!"
INGRESS
    ;;
  signal)
    [[ $# == 3 && "$1" =~ ^v1366-[a-z0-9-]{1,48}$ ]]
    [[ "$2" =~ ^(source|control|nodes-protection|nodes|source-clean|new-install|target-clean)$ ]]
    [[ "$3" =~ ^(advance|retry|cleanup)$ ]]
    "${SSH[@]}" "$control" bash -s -- "$1" "$2" "$3" <<'SIGNAL'
set -eu
cd /var/tmp/kubeauto-v1366-lab/target-product
/usr/local/kubeauto/.venv/bin/python -B - "$@" <<'PY'
import json,sys
from pathlib import Path
from upgrade.lab.next_session import STATE,write
run,phase,action=sys.argv[1:]
assert json.loads((STATE/'owner.json').read_text())['run_id']==run
path=STATE/'runs'/run/'action.json'
assert not path.exists()
write(path,{'run_id':run,'phase':phase,'action':action})
PY
SIGNAL
    ;;
  *) echo 'Use stage/qualify-control/plan/launch/status/follow/fetch/signal' >&2; exit 2 ;;
esac
