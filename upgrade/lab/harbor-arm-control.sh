#!/usr/bin/env bash
# Native ARM test frontend. No release, Kubernetes operation or arbitrary host.
set -euo pipefail
[[ $# -ge 2 ]]
operation=$1 run_id=$2; shift 2
[[ "$run_id" =~ ^v1366-harbor-arm-[a-z0-9-]{1,28}$ ]] || exit 2
mode=qualify
if [[ $# == 1 && "$operation" =~ ^(plan|launch)$ ]]; then mode=$1; shift; fi
[[ $# == 0 && "$mode" =~ ^(qualify|final)$ ]] || exit 2
control=root@192.168.47.130
state=/var/tmp/kubeauto-v1366-lab
SSH=(ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
case "$operation" in
  stage)
    # Verify the real scope before copying anything. Ordinary source pushes
    # cannot launch this runner; the explicit launch operation is separate.
    .venv/bin/python -B -m upgrade.lab.harbor_arm_session plan --run-id "$run_id"
    "${SSH[@]}" "$control" bash -s -- "$run_id" <<'ADMIT'
set -euo pipefail
state=/var/tmp/kubeauto-v1366-lab
test ! -L "$state"
if test -e "$state"; then
 test "$(stat -c '%u:%a' "$state")" = 0:700
 test ! -e "$state/owner.json" && test ! -e "$state/runs"
 test -f "$state/staging-run" && test "$(cat "$state/staging-run")" = "$1"
else
 install -d -m 0700 "$state"
 printf '%s\n' "$1" > "$state/staging-run"
 chmod 0600 "$state/staging-run"
fi
install -d -m 0700 "$state/target-product" "$state/target-product/down"
ADMIT
    rsync -az --no-owner --no-group --safe-links --chmod=D700,F600 \
      --files-from=<(git ls-files -c -o --exclude-standard | sort -u) \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' ./ "$control:$state/target-product/"
    # Prove the native node's actual transfer protocol before claiming it or
    # starting Docker. The ARM distribution need not provide rsync.
    "${SSH[@]}" "$control" 'install -d -m 0700 /var/tmp/kubeauto-v1366-lab/target-product/extra-bin && scp -p root@192.168.37.101:/usr/libexec/docker/cli-plugins/docker-compose /var/tmp/kubeauto-v1366-lab/target-product/extra-bin/docker-compose && echo HARBOR_ARM_SCP_PREFLIGHT_PASS'
    rsync -az --no-owner --no-group --safe-links --chmod=D700,F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
      upgrade/v1.36.6/evidence/raw/ "$control:$state/target-product/upgrade/v1.36.6/evidence/raw/"
    rsync -az --no-owner --no-group --chmod=F600 \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
      /home/brinnatt/.cache/kubeauto/harbor/harbor-offline-installer-v2.14.4-iabs-arm64.tgz \
      "$control:$state/target-product/down/harbor-offline-installer-v2.14.4.tgz"
    for version in v2.13.0 v2.16.0; do
      rsync -az --no-owner --no-group --chmod=F600 \
        -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
        "/home/brinnatt/.cache/kubeauto/harbor/harbor-offline-installer-$version-arm64.tgz" \
        "$control:$state/target-product/down/harbor-offline-installer-$version.tgz"
    done
    "${SSH[@]}" "$control" bash -s -- "$run_id" <<'INPUTS'
set -euo pipefail
umask 077
state=/var/tmp/kubeauto-v1366-lab
python=/usr/local/kubeauto/.venv/bin/python
"$python" -B - "$1" <<'PY'
import os,shutil,subprocess,sys,json,hashlib
from pathlib import Path
s=Path('/var/tmp/kubeauto-v1366-lab'); root=s/'target-product'
sys.path.insert(0,str(root))
from upgrade.lab.harbor_arm_session import CONTROL_BINARIES,sha,PACKAGES
for directory,names in CONTROL_BINARIES.items():
 (root/directory).mkdir(exist_ok=True)
 for name in names:
  shutil.copy2(Path('/usr/local/kubeauto')/directory/name,root/directory/name)
  (root/directory/name).chmod(0o700)
for version,checksum in PACKAGES.items():
 assert sha(root/'down'/('harbor-offline-installer-'+version+'.tgz'))==checksum,version
files={str(p.relative_to(s)):sha(p) for p in root.rglob('*') if p.is_file()}
(s/'arm-inputs.json').write_text(json.dumps({'run_id':sys.argv[1],'host':'192.168.37.101','files':files},indent=2)+'\n')
print('HARBOR_ARM_STAGED_INPUTS_PASS packages=3 builds=0')
PY
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" PYTHONPATH="$state/target-product"
export KUBEAUTO_LOG_DIR="$state/control-logs" KUBEAUTO_BASE_PATH="$state/target-product"
cd "$state/target-product"
"$python" -B kubecli.py --help >/dev/null
"$python" -B - <<'NATIVE'
from common.ansible_python import probe_installed_ansible_core,format_ansible_core_compatibility_failure
import subprocess,time
p=probe_installed_ansible_core(); reason=format_ansible_core_compatibility_failure(p)
assert not reason,reason
assert p.executable_path=='/usr/bin/ansible' and p.version==(2,16) and p.package_owner.startswith('rpm:'),p
epoch=float(subprocess.check_output(['ssh','-T','-o','BatchMode=yes','root@192.168.37.101','date +%s'],text=True))
assert abs(time.time()-epoch)<=120,'control/ARM clock delta exceeds certificate admission'
from service.cluster.manager import ClusterManager
from pathlib import Path
import tempfile
manager=ClusterManager()
manager.clusters_dir.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='harbor-control-preflight-',dir=manager.clusters_dir) as directory:
 Path(directory,'hosts').write_text('[harbor]\n192.168.37.101\n')
 manager._show_component_versions(Path(directory).name)
print('HARBOR_ARM_NATIVE_CONTROL_PREFLIGHT_PASS')
NATIVE
"$python" -B -m upgrade.lab.harbor_arm_session plan --run-id "$1"
INPUTS
    ;;
  plan|launch)
    "${SSH[@]}" "$control" bash -s -- "$operation" "$run_id" "$mode" <<'LAUNCH'
set -euo pipefail
state=/var/tmp/kubeauto-v1366-lab
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" PYTHONPATH="$state/target-product"
export KUBEAUTO_LOG_DIR="$state/control-logs"
cd "$state/target-product"
python=/usr/local/kubeauto/.venv/bin/python
"$python" -B -m upgrade.lab.harbor_arm_session plan --run-id "$2" --mode "$3"
if test "$1" = launch; then
 test ! -e "$state/owner.json" && test ! -e "$state/$2.pid" && test ! -e "$state/runs/$2"
 nohup bash tests/helpers/run-durable-gate.sh "$state/$2" HARBOR_ARM_SESSION_EXIT \
   "$python" -B -m upgrade.lab.harbor_arm_session run --run-id "$2" --mode "$3" > "$state/$2.log" 2>&1 < /dev/null &
 echo "HARBOR_ARM_LAUNCHED run_id=$2 pid=$!"
fi
LAUNCH
    ;;
  status|follow)
    "${SSH[@]}" "$control" bash -s -- "$operation" "$run_id" <<'STATUS'
set -euo pipefail
state=/var/tmp/kubeauto-v1366-lab
for suffix in exit finalized; do if test -f "$state/$2.$suffix"; then printf '%s=' "$suffix"; cat "$state/$2.$suffix"; fi; done
if test "$1" = follow; then tail -n 60 "$state/$2.log"; else tail -n 15 "$state/$2.log"; fi
STATUS
    ;;
  cleanup-uninstalled)
    "${SSH[@]}" "$control" bash -s -- "$run_id" <<'REPAIR_ADMIT'
set -euo pipefail
python3 -B - "$1" <<'PY'
import json,sys
from pathlib import Path
s=Path('/var/tmp/kubeauto-v1366-lab');r=s/'runs'/sys.argv[1]
assert json.loads((s/'owner.json').read_text())['run_id']==sys.argv[1]
assert (s/(sys.argv[1]+'.exit')).read_text().strip()=='1'
assert (s/(sys.argv[1]+'.finalized')).read_text().strip()=='1'
assert json.loads((r/'harbor-source.status.json').read_text())['rc']==1
assert not (r/'source-created.json').exists() and not (r/'harbor-migration.status.json').exists()
p=r/'uninstalled-fixture-recovery'
if p.exists():
 for suffix in ['exit','finalized']:
  assert (s/(sys.argv[1]+'.fixture-cleanup.'+suffix)).read_text().strip()=='1'
 count=1
 while (r/('uninstalled-fixture-recovery-failed-'+str(count))).exists():count+=1
 saved=r/('uninstalled-fixture-recovery-failed-'+str(count));p.rename(saved)
 for suffix in ['log','exit','finalized']:
  (s/(sys.argv[1]+'.fixture-cleanup.'+suffix)).rename(saved/('session.'+suffix))
p.mkdir(mode=0o700)
PY
REPAIR_ADMIT
    rsync -az --no-owner --no-group --chmod=F600 -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
      upgrade/lab/harbor_arm_session.py "$control:$state/runs/$run_id/uninstalled-fixture-recovery/cleanup.py"
    "${SSH[@]}" "$control" bash -s -- "$run_id" <<'REPAIR_LAUNCH'
set -euo pipefail
state=/var/tmp/kubeauto-v1366-lab
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" PYTHONPATH="$state/target-product" KUBEAUTO_LOG_DIR="$state/control-logs"
cd "$state/target-product"
nohup bash tests/helpers/run-durable-gate.sh "$state/$1.fixture-cleanup" HARBOR_ARM_FIXTURE_CLEANUP_EXIT \
 "/usr/local/kubeauto/.venv/bin/python" -B "$state/runs/$1/uninstalled-fixture-recovery/cleanup.py" cleanup-uninstalled --run-id "$1" \
 > "$state/$1.fixture-cleanup.log" 2>&1 < /dev/null &
echo "HARBOR_ARM_UNINSTALLED_CLEANUP_LAUNCHED run_id=$1 pid=$!"
REPAIR_LAUNCH
    ;;
  fetch)
    destination="upgrade/v1.36.6/evidence/raw/harbor-arm64/$run_id"
    mkdir -p "$destination"
    # Public run evidence only. Private recovery archives and cluster config
    # remain root-only on the controller and are never copied into Git.
    rsync -az --no-owner --no-group --safe-links --exclude='bootstrap-*.tar' \
      -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
      "$control:$state/runs/$run_id/" "$destination/"
    for suffix in log exit finalized; do
      rsync -az --ignore-missing-args --no-owner --no-group -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
        "$control:$state/$run_id.$suffix" "$destination/session.$suffix"
      rsync -az --ignore-missing-args --no-owner --no-group -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
        "$control:$state/$run_id.fixture-cleanup.$suffix" "$destination/fixture-cleanup.$suffix"
    done
    ;;
  archive)
    # Archive only a fully cleaned node, or a refusal before node admission.
    # This never deletes a recovery point or repairs/cleans a failed product.
    "${SSH[@]}" "$control" bash -s -- "$run_id" <<'ARCHIVE'
set -euo pipefail
python3 -B - "$1" <<'PY'
import json,subprocess,sys
from pathlib import Path
s=Path('/var/tmp/kubeauto-v1366-lab');run=sys.argv[1];r=s/'runs'/run
assert not s.is_symlink() and s.stat().st_mode&0o777==0o700
code=int((s/(run+'.exit')).read_text());assert (s/(run+'.finalized')).read_text().strip()==str(code)
if code==0:
 assert not (s/'owner.json').exists()
 assert json.loads((r/'harbor-clean.status.json').read_text())['rc']==0
 assert 'HARBOR_ARM_SCOPED_CLEAN_VERIFY_PASS' in (s/(run+'.log')).read_text()
else:
 assert json.loads((s/'owner.json').read_text())['run_id']==run
 assert json.loads((r/'harbor-source.status.json').read_text())['rc']==1
 if (r/'node-admission.json').exists():
  for suffix in ['exit','finalized']:
   assert (s/(run+'.fixture-cleanup.'+suffix)).read_text().strip()=='0'
  assert 'HARBOR_ARM_UNINSTALLED_FIXTURE_CLEANED' in (s/(run+'.fixture-cleanup.log')).read_text()
 assert not list(r.glob('harbor-migration.*'))
probe='''from pathlib import Path
import subprocess
for name in ['/var/tmp/kubeauto-v1366-node-owner','/var/tmp/kubeauto-harbor-v1366','/var/data','/var/log/harbor','/etc/kubernetes','/usr/local/bin/docker','/usr/local/bin/docker-compose']:
 p=Path(name);assert not p.exists() and not p.is_symlink(),name
for unit in ['docker','docker.socket','containerd']:
 assert subprocess.check_output(['systemctl','show',unit,'--property=ActiveState','--value'],text=True).strip()=='inactive',unit
'''
subprocess.run(['ssh','-T','-o','BatchMode=yes','root@192.168.37.101','python3 -B -'],input=probe,text=True,check=True,timeout=30)
parent=Path('/home/kubeauto-harbor-arm-archives')
assert not parent.is_symlink();parent.mkdir(mode=0o700,exist_ok=True)
assert parent.stat().st_uid==0 and parent.stat().st_mode&0o777==0o700
destination=parent/run;assert not destination.exists() and not destination.is_symlink()
s.rename(destination)
print('HARBOR_ARM_CONTROL_ARCHIVED run_id='+run+' directory='+str(destination))
PY
ARCHIVE
    ;;
  *) exit 2 ;;
esac
