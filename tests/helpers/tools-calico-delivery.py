#!/usr/bin/env python3
"""Launch and collect the tools-owned Calico fixture through its durable fence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
HOST='root@192.168.47.130'
REMOTE='/tmp/tools-calico-delivery'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',HOST]


def run(command,**kwargs):
    return subprocess.run([*SSH,command],check=True,timeout=30,**kwargs)


def align_clocks():
    """Repair only the active leased fixture's clocks, never a foreign cluster."""
    owner=json.loads(run('cat /tmp/tools-calico-lab/owner.json',capture_output=True,text=True).stdout)
    nodes=['192.168.47.'+str(n) for n in range(131,137)]
    if owner.get('nodes')!=nodes:raise ValueError('TOOLS_CALICO_CLOCK_OWNER_REFUSED')
    # The durable launcher must still own the fixture. A leftover owner file
    # by itself is not authorization to change any machine's clock.
    run('test ! -e '+REMOTE+'/state.finalized && pid=$(cat '+REMOTE+'/state.pid) && test -e /proc/$pid && grep -aq TOOLS_CALICO_EXIT /proc/$pid/cmdline')
    epoch=str(int(time.time()))
    for host in [HOST,*['root@'+n for n in nodes]]:
        subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',host,
                        'date -u -s @'+epoch],check=True,timeout=20)
    print('TOOLS_CALICO_CLOCK_REPAIR_PASS hosts=7 basis=development-clock',flush=True)


def clear_failed():
    outcome=run('cat '+REMOTE+'/state.exit '+REMOTE+'/state.finalized',capture_output=True,text=True).stdout.split()
    if len(outcome)!=2 or outcome[0]!=outcome[1] or outcome[0]=='0':
        raise ValueError('TOOLS_CALICO_FAILED_CLEAN_STATE_REFUSED')
    run('test ! -e /tmp/tools-calico-lab')
    expected=run('sha256sum '+REMOTE+'/console.log',capture_output=True,text=True).stdout.split()[0]
    archive=run('sha256sum /tmp/tools-calico-fixture-evidence.tar.gz',capture_output=True,text=True).stdout.split()[0]
    matches=[p for p in (ROOT/'logs').glob('tools-calico-live-*/console.log')
             if hashlib.sha256(p.read_bytes()).hexdigest()==expected and
             (p.parent/'evidence.tar.gz').is_file() and
             hashlib.sha256((p.parent/'evidence.tar.gz').read_bytes()).hexdigest()==archive]
    if not matches:raise ValueError('TOOLS_CALICO_FAILED_EVIDENCE_NOT_COLLECTED')
    run("python3 -c \"import shutil,pathlib; shutil.rmtree('"+REMOTE+"'); pathlib.Path('/tmp/tools-calico-fixture-evidence.tar.gz').unlink()\"")
    print('TOOLS_CALICO_FAILED_SCOPED_CLEAN_PASS evidence='+str(matches[0].relative_to(ROOT)),flush=True)


def launch(stage,resume=False):
    if stage not in ('stage','probe','live'):raise ValueError('TOOLS_CALICO_STAGE_REFUSED')
    artifact=ROOT/'dist/CalicoPolicyCli-rocky8'
    build=json.loads((ROOT/'dist/calico-build.json').read_text())
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    if (sha(artifact)!=build['artifacts']['CalicoPolicyCli'] or
            sha(ROOT/'tools/k8stools/CalicoPolicyCli.py')!=build['inputs']['tools/k8stools/CalicoPolicyCli.py']):
        raise ValueError('TOOLS_CALICO_BUILD_STALE')
    output=ROOT/'logs'/('tools-calico-'+stage+'-'+time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(mode=0o700)
    if not resume:run('mkdir -m 700 '+REMOTE)
    files=[(ROOT/'tests/helpers/tools-calico-lab.py','fixture.py'),
           (ROOT/'tests/helpers/calico-live-regression.sh','calico-live-regression.sh'),
           (ROOT/'tests/helpers/run-durable-gate.sh','durable.sh'),(artifact,'CalicoPolicyCli')]
    for path,name in files:
        if not resume:subprocess.run(['scp','-q','-o','BatchMode=yes',str(path),HOST+':'+REMOTE+'/'+name],check=True,timeout=90)
        if run('sha256sum '+REMOTE+'/'+name,capture_output=True,text=True).stdout.split()[0]!=sha(path):
            raise ValueError('TOOLS_CALICO_UPLOAD_STALE '+name)
    argument={'stage':' --stage-only','probe':' --probe-only','live':''}[stage]
    if not resume:
        run('cd '+REMOTE+' && { nohup bash durable.sh state TOOLS_CALICO_EXIT /usr/local/kubeauto/.venv/bin/python -B fixture.py'+argument+' >console.log 2>&1 </dev/null & }')
    started=time.monotonic();offset=1
    with (output/'console.log').open('wb') as raw:
        while time.monotonic()-started<5400:
            data=run('tail -c +'+str(offset)+' '+REMOTE+'/console.log',capture_output=True).stdout
            raw.write(data);raw.flush();sys.stdout.buffer.write(data);sys.stdout.buffer.flush();offset+=len(data)
            status=subprocess.run([*SSH,'test -s '+REMOTE+'/state.finalized && cat '+REMOTE+'/state.exit '+REMOTE+'/state.finalized'],capture_output=True,timeout=25)
            if status.returncode==0:
                outcome=status.stdout.decode().split()
                remaining=run('tail -c +'+str(offset)+' '+REMOTE+'/console.log',capture_output=True).stdout
                raw.write(remaining);raw.flush();sys.stdout.buffer.write(remaining);sys.stdout.buffer.flush()
                for suffix in ('exit','finalized'):(output/('state.'+suffix)).write_text(outcome[0 if suffix=='exit' else 1]+'\n')
                if stage=='live':
                    subprocess.run(['scp','-q','-o','BatchMode=yes',HOST+':/tmp/tools-calico-fixture-evidence.tar.gz',str(output/'evidence.tar.gz')],check=True,timeout=90)
                if outcome!=['0','0']:raise ValueError('TOOLS_CALICO_REMOTE_NONZERO '+repr(outcome)+' diagnostics='+str(output))
                text=(output/'console.log').read_text()
                markers={'stage':['TOOLS_CALICO_BINARY_STAGE_PASS'],
                         'probe':['TOOLS_CALICO_PROBE_SSH_PASS'],
                         'live':['TOOLS_CALICO_FIXTURE_READY','CALICO_LIVE_REGRESSION_PASS','TOOLS_CALICO_FIXTURE_SCOPED_CLEAN_PASS']}[stage]
                if any(m not in text for m in markers):raise ValueError('TOOLS_CALICO_TERMINAL_MARKER_MISSING')
                run("python3 -c \"import shutil,pathlib; shutil.rmtree('"+REMOTE+"'); p=pathlib.Path('/tmp/tools-calico-fixture-evidence.tar.gz'); p.unlink() if p.exists() else None\"")
                run('test ! -e '+REMOTE+' && test ! -e /tmp/tools-calico-lab')
                print('TOOLS_CLEAN_VERIFY_PASS scope=calico stage='+stage,flush=True)
                return
            print('TOOLS_CALICO_HEARTBEAT stage='+stage+' elapsed='+str(int(time.monotonic()-started))+' bytes='+str(offset-1),flush=True)
            time.sleep(10)
    raise TimeoutError('TOOLS_CALICO_MONITOR_TIMEOUT; remote durable state retained')


if __name__=='__main__':
    if sys.argv[1:]==['align-clocks']:align_clocks()
    elif sys.argv[1:]==['clear-failed']:clear_failed()
    else:launch(sys.argv[1],resume=sys.argv[2:]==['--resume'])
