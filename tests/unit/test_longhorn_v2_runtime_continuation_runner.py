"""Execute original lease/launch boundary entirely in temporary filesystem."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'tests/helpers'

class FixedRunnerContinuation(unittest.TestCase):
    def test_static_child_does_not_inherit_runtime_request_but_parent_retains_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tests/helpers').mkdir(parents=True)
            (root/'.venv/bin').mkdir(parents=True)
            (root/'.venv/bin/python').symlink_to(shutil.which('python3'))
            (root/'tests/helpers/longhorn-static-reuse.py').write_text('pass\n')
            (root/'tests/helpers/longhorn-static.sh').write_text('''set -euo pipefail
[[ ! -v LONGHORN_V2_CONTINUATION_REQUEST ]]
echo LONGHORN_STATIC_PASS
''')
            script='''set -Eeuo pipefail
source "$FIXTURE_HERE/longhorn-runner.sh"
ROOT="$FIXTURE_ROOT"
export LONGHORN_V2_CONTINUATION_REQUEST=logs/longhorn-cases/v2-continuation.fixture/request.json
longhorn_local_gate static LONGHORN_STATIC_PASS
[[ "$LONGHORN_V2_CONTINUATION_REQUEST" == logs/longhorn-cases/v2-continuation.fixture/request.json ]]
echo FIXTURE_STATIC_ENVIRONMENT_ISOLATED_PARENT_REQUEST_RETAINED
'''
            result=subprocess.run(['bash','-c',script],text=True,capture_output=True,timeout=5,
                env={**os.environ,'FIXTURE_HERE':str(HERE),'FIXTURE_ROOT':str(root)})
            self.assertEqual(0,result.returncode,result.stdout+'\n'+result.stderr)
            self.assertIn('FIXTURE_STATIC_ENVIRONMENT_ISOLATED_PARENT_REQUEST_RETAINED',result.stdout)

    def test_cleanup_rejects_request_before_transfer_or_remote_activity(self):
        # Runtime create deliberately rejects an existing output. Recovery owns
        # the persisted ref in its preparation journal and accepts no request.
        script='''set -Eeuo pipefail
source "$FIXTURE_HERE/longhorn-runner.sh"
ROOT=/nonexistent-private-fixture
export LONGHORN_V2_CONTINUATION_REQUEST=logs/longhorn-cases/v2-continuation.fixture/request.json
longhorn_local_gate() { echo UNEXPECTED_LOCAL_GATE; return 99; }
ssh_longhorn() { echo UNEXPECTED_REMOTE; return 99; }
if longhorn_launch v2-upgrade-cleanup LONGHORN_V2_UPGRADE_CLEANUP_PASS; then
  exit 98
else
  [[ "$?" == 2 ]]
fi
echo FIXTURE_CLEANUP_REQUEST_REFUSED_BEFORE_IO
'''
        result=subprocess.run(['bash','-c',script],text=True,capture_output=True,timeout=5,
            env={**os.environ,'FIXTURE_HERE':str(HERE)})
        self.assertEqual(0,result.returncode,result.stdout+'\n'+result.stderr)
        self.assertEqual('FIXTURE_CLEANUP_REQUEST_REFUSED_BEFORE_IO\n',result.stdout)
        self.assertEqual('',result.stderr)

    def test_actual_lease_handshake_transfers_request_and_exports_created_runtime_to_original_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            area=Path(directory);local=area/'local';remote=area/'remote'
            for base in (local,remote):
                for name in ('tests/helpers','logs/longhorn-cases/v2-continuation.fixture','.venv/bin','tmp','var/tmp','var/lock'):
                    (base/name).mkdir(parents=True,exist_ok=True)
                (base/'.venv/bin/python').symlink_to(shutil.which('python3'))
            (local/'logs/longhorn-cases/v2-continuation.fixture/request.json').write_text('{}')
            helper='''import argparse,json,os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root');p.add_argument('mode');p.add_argument('--request');p.add_argument('--files');a=p.parse_args()
if a.mode=='transfer':Path(a.files).write_text(a.request+'\\n')
else:
 assert os.environ.get('LONGHORN_PARENT_LEASE_HELD')=='yes'
 root=Path(__file__).resolve().parents[2];request=root/a.request;assert request.is_file()
 output=request.with_name('runtime.json');output.write_text('{"fixture":true}');output.chmod(0o600)
 print('FIXTURE_REAL_CREATE_UNDER_LEASE')
'''
            for base in (local,remote):(base/'tests/helpers/longhorn-v2-runtime-continuation.py').write_text(helper)
            sync=local/'tests/helpers/sync-kubeauto.sh';sync.write_text('#!/bin/bash\n[[ "$KUBEAUTO_SYNC_SKIP_CONTROL_SETUP" == 1 && "$1" == root@192.168.47.130 ]]\n')
            live=(HERE/'longhorn-live.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(remote)).replace('/var/lock/',str(remote/'var/lock')+'/')
            (remote/'tests/helpers/longhorn-live.sh').write_text(live)
            shutil.copy(ROOT/'tests/helpers/run-durable-gate.sh',remote/'tests/helpers/run-durable-gate.sh')
            (remote/'tests/helpers/longhorn-v2-upgrade-lifecycle.sh').write_text('''#!/bin/bash
set -euo pipefail
[[ "$1" == v2-upgrade-lifecycle ]]
[[ "$LONGHORN_PARENT_LEASE_HELD" == yes ]]
[[ "$LONGHORN_V2_RUNTIME_CONTINUATION" == "$FIXTURE_REMOTE/logs/longhorn-cases/v2-continuation.fixture/runtime.json" ]]
[[ -f "$LONGHORN_V2_RUNTIME_CONTINUATION" ]]
echo FIXTURE_UPGRADE_OBSERVED_EXPLICIT_RUNTIME
 echo LONGHORN_V2_UPGRADE_GATE_PASS
''')
            script=r'''set -Eeuo pipefail
source "$FIXTURE_HERE/longhorn-runner.sh"
ROOT="$FIXTURE_LOCAL"
longhorn_local_gate() { :; }
ssh_longhorn() {
  local command="$1"
  command="${command//\/usr\/local\/kubeauto/$FIXTURE_REMOTE}"
  command="${command//\/var\/lock\//$FIXTURE_REMOTE/var/lock/}"
  command="${command//\/var\/tmp\//$FIXTURE_REMOTE/var/tmp/}"
  command="${command//\/tmp\/kubeauto/$FIXTURE_REMOTE/tmp/kubeauto}"
  bash -c "$command"
}
rsync() {
 "$ROOT/.venv/bin/python" - "$@" <<'INNER'
import os,sys,shutil
from pathlib import Path
files=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--files-from='))
for name in Path(files).read_text().splitlines():
 source=Path(os.environ['FIXTURE_LOCAL'])/name;target=Path(os.environ['FIXTURE_REMOTE'])/name
 target.parent.mkdir(mode=0o700,parents=True,exist_ok=True);shutil.copyfile(source,target);target.chmod(0o600)
INNER
}
monitor_remote_job() {
  local i
  for i in {1..100}; do
    if [[ -f "$FIXTURE_REMOTE/tmp/kubeauto-longhorn-gate.finalized" ]]; then
      [[ "$(cat "$FIXTURE_REMOTE/tmp/kubeauto-longhorn-gate.finalized")" == 0 ]]
      return
    fi
    sleep .05
  done
  return 1
}
remote_job_summary() { cat "$FIXTURE_REMOTE/tmp/kubeauto-longhorn-gate.exit"; }
export LONGHORN_V2_CONTINUATION_REQUEST=logs/longhorn-cases/v2-continuation.fixture/request.json
longhorn_launch v2-upgrade-lifecycle LONGHORN_V2_UPGRADE_GATE_PASS
[[ -s "$ROOT/logs/longhorn-cases/v2-continuation.fixture/runtime.json" ]]
[[ "$(cat "$FIXTURE_REMOTE/tmp/kubeauto-longhorn-gate.exit")" == 0 ]]
[[ "$(cat "$FIXTURE_REMOTE/tmp/kubeauto-longhorn-gate.finalized")" == 0 ]]
grep -Fxq FIXTURE_UPGRADE_OBSERVED_EXPLICIT_RUNTIME "$LONGHORN_LAST_DIRECTORY/gate.log"
grep -Fxq FIXTURE_REAL_CREATE_UNDER_LEASE "$LONGHORN_LAST_DIRECTORY/gate.log"
echo FIXTURE_FIXED_RUNNER_CONTINUATION_PASS
'''
            result=subprocess.run(['bash','-c',script],text=True,capture_output=True,timeout=15,env={**os.environ,'FIXTURE_HERE':str(HERE),'FIXTURE_LOCAL':str(local),'FIXTURE_REMOTE':str(remote)})
            self.assertEqual(0,result.returncode,result.stdout+'\n'+result.stderr)
            self.assertIn('FIXTURE_FIXED_RUNNER_CONTINUATION_PASS',result.stdout)

if __name__=='__main__':unittest.main(verbosity=2)
