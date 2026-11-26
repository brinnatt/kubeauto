#!/usr/bin/env python3
"""Build only MyBackupCli on the authorized native Rocky 8.10 host.

Uses the established onefile spec and a disposable venv with pinned PyInstaller.
No images are built/published and no other tool artifact is replaced.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
HOST='root@192.168.47.131'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3']
LIBRARY='tests/evidence/mybackup-native-build-library.json'
FILES=['tools/mysqltools/MyBackupCli.py','tools-onefile.spec','tests/helpers/build-mybackup-native.py',LIBRARY]
REQUIREMENTS=['PyInstaller==6.16.0','pyinstaller-hooks-contrib==2025.9','packaging==25.0','altgraph==0.17.4','setuptools==80.9.0']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args,**kwargs): return subprocess.run([str(x) for x in args],check=True,**kwargs)


def validate_plan(plan,base):
    if str(base)!=plan['base'] or not re.fullmatch(r'/tmp/b80build-[a-f0-9]{12}',str(base)):
        raise ValueError('MYBACKUP_NATIVE_BUILD_SCOPE_REFUSED')
    if plan['requirements']!=REQUIREMENTS or plan['selected']!=['MyBackupCli']:
        raise ValueError('MYBACKUP_NATIVE_BUILD_SELECTION_REFUSED')
    for name,expected in plan['inputs'].items():
        if sha(base/name)!=expected: raise ValueError('MYBACKUP_NATIVE_BUILD_INPUT_REFUSED '+name)


def remote():
    import fcntl
    base=Path.cwd(); plan=json.loads((base/'plan.json').read_text()); validate_plan(plan,base)
    os_release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if os_release['ID'].strip('"')!='rocky' or os_release['VERSION_ID'].strip('"')!='8.10' or os.uname().machine!='x86_64':
        raise ValueError('MYBACKUP_NATIVE_BUILD_OS_REFUSED')
    if run(['getconf','GNU_LIBC_VERSION'],capture_output=True,text=True).stdout.strip()!='glibc 2.28':
        raise ValueError('MYBACKUP_NATIVE_BUILD_ABI_REFUSED')
    with open('/var/lock/kubeauto-mysql-backup-release.lock','w') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            library=json.loads((base/LIBRARY).read_text()); rpm=base/(library['sha256']+'.rpm')
            if sha(rpm)!=library['sha256']: raise ValueError('MYBACKUP_NATIVE_LIBRARY_CHECKSUM')
            run(['rpmkeys','--checksig',rpm])
            native=base/'native'; native.mkdir()
            converter=subprocess.Popen(['rpm2cpio',str(rpm)],stdout=subprocess.PIPE)
            try: run(['cpio','-idm','--quiet','--no-absolute-filenames'],cwd=native,stdin=converter.stdout)
            finally: converter.stdout.close()
            if converter.wait()!=0: raise ValueError('MYBACKUP_NATIVE_LIBRARY_EXTRACTION')
            build_env=dict(os.environ,LD_LIBRARY_PATH=str(native/'usr/lib64'),PYTHONDONTWRITEBYTECODE='1')
            run(['python3.12','-c','import ssl,pyexpat; print(ssl.OPENSSL_VERSION)'],env=build_env)
            run(['python3.12','-m','venv',base/'venv'],env=build_env)
            python=base/'venv/bin/python'
            run([python,'-m','pip','install','--disable-pip-version-check','--no-input','--index-url',
                 'https://repo.huaweicloud.com/repository/pypi/simple',*REQUIREMENTS],env=build_env)
            run([python,'-m','pip','check'],env=build_env)
            freeze=run([python,'-m','pip','freeze','--all'],capture_output=True,text=True,env=build_env).stdout
            (base/'dependencies.txt').write_text(freeze)
            run([python,'-m','PyInstaller','--clean','--noconfirm',base/'tools-onefile.spec'],
                env=dict(build_env,TOOLS_SELECTED='MyBackupCli'),cwd=base)
            output=base/'dist/MyBackupCli'
            run([output,'--help'],stdout=subprocess.DEVNULL)
            run([output,'--help-native'],stdout=subprocess.DEVNULL)
            result=dict(inputs=plan['inputs'],artifact=sha(output),runtime='Rocky 8.10; glibc 2.28; Python 3.12; PyInstaller 6.16.0',
                        dependencies=freeze.splitlines())
            (base/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        finally:
            for name in ('venv','build','tools','__pycache__','native'):
                if (base/name).exists(): shutil.rmtree(base/name)
    print('MYBACKUP_NATIVE_BUILD_PASS count=1 tool=MyBackupCli',flush=True)


def infrastructure():
    spec=importlib.util.spec_from_file_location('build_infra',ROOT/'tests/helpers/starrocks-compose-live.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def launch():
    ident=uuid.uuid4().hex[:12]; base='/tmp/b80build-'+ident
    directory=ROOT/'logs'/('mybackup-native-build-'+ident); directory.mkdir(mode=0o700,parents=True)
    plan=dict(base=base,selected=['MyBackupCli'],requirements=REQUIREMENTS,inputs={name:sha(ROOT/name) for name in FILES})
    (directory/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    print('MYBACKUP_NATIVE_BUILD_START run='+ident+' host='+HOST+' selected=MyBackupCli',flush=True)
    with infrastructure().control_leases():
        run([*SSH,HOST,'mkdir','-m','700',base])
        for name in FILES:
            target=str(Path(base)/name)
            run([*SSH,HOST,'mkdir -p '+shlex.quote(str(Path(target).parent))])
            run(['scp','-q','-o','BatchMode=yes',ROOT/name,HOST+':'+target])
        library=json.loads((ROOT/LIBRARY).read_text()); rpm=Path.home()/'.cache/kubeauto/mysql80-binary'/(library['sha256']+'.rpm')
        if sha(rpm)!=library['sha256']: raise ValueError('MYBACKUP_NATIVE_LIBRARY_NOT_PREPARED')
        run(['scp','-q','-o','BatchMode=yes',rpm,HOST+':'+base+'/'+rpm.name])
        for source,name in ((directory/'plan.json','plan.json'),(ROOT/'tests/helpers/run-durable-gate.sh','durable.sh')):
            run(['scp','-q','-o','BatchMode=yes',source,HOST+':'+base+'/'+name])
        run([*SSH,HOST,'cd '+base+' && nohup bash durable.sh state MYBACKUP_NATIVE_BUILD_EXIT python3 -B tests/helpers/build-mybackup-native.py remote >console.log 2>&1 </dev/null &'])
        start=time.monotonic(); offset=1; progress=start
        while time.monotonic()-start<1800:
            result=run([*SSH,HOST,'tail -c +'+str(offset)+' '+base+'/console.log'],capture_output=True)
            if result.stdout:
                sys.stdout.buffer.write(result.stdout); sys.stdout.buffer.flush(); offset+=len(result.stdout); progress=time.monotonic()
            if subprocess.run([*SSH,HOST,'test -s '+base+'/state.finalized']).returncode==0: break
            if time.monotonic()-progress>40:
                print('MYBACKUP_NATIVE_BUILD_HEARTBEAT elapsed='+str(int(time.monotonic()-start))+' next=PyInstaller-and-CLI-smoke',flush=True); progress=time.monotonic()
            time.sleep(5)
        else: raise TimeoutError('MYBACKUP_NATIVE_BUILD_TIMEOUT preserve='+base)
        for name in ('console.log','state.exit','state.finalized'):
            run(['scp','-q','-o','BatchMode=yes',HOST+':'+base+'/'+name,directory/name])
        if any((directory/('state.'+suffix)).read_text().strip()!='0' for suffix in ('exit','finalized')):
            raise ValueError('MYBACKUP_NATIVE_BUILD_FAILED evidence='+str(directory))
        for name in ('result.json','dependencies.txt','dist/MyBackupCli'):
            run(['scp','-q','-o','BatchMode=yes',HOST+':'+base+'/'+name,directory/Path(name).name])
        result=json.loads((directory/'result.json').read_text())
        if result['inputs']!={name:sha(ROOT/name) for name in FILES} or result['artifact']!=sha(directory/'MyBackupCli'):
            raise ValueError('MYBACKUP_NATIVE_BUILD_CHANGED_OR_CORRUPT')
        target=ROOT/'dist/MyBackupCli-rocky8'
        # Preserve the previous frozen artifact as immutable historical evidence.
        previous=directory/('previous-'+sha(target))
        if target.exists(): shutil.copy2(target,previous)
        shutil.copy2(directory/'MyBackupCli',target); target.chmod(0o755)
        proof_path=ROOT/'dist/mysql-backup-build.json'; proof=json.loads(proof_path.read_text())
        proof['inputs']['tools/mysqltools/MyBackupCli.py']=plan['inputs']['tools/mysqltools/MyBackupCli.py']
        proof['inputs']['tools-onefile.spec']=plan['inputs']['tools-onefile.spec']
        proof['inputs']['tests/helpers/build-mybackup-native.py']=plan['inputs']['tests/helpers/build-mybackup-native.py']
        proof['artifacts']['MyBackupCli']=result['artifact']; proof['native_mybackup_build']=str((directory/'result.json').relative_to(ROOT))
        proof_path.write_text(json.dumps(proof,indent=2)+'\n')
        run([*SSH,HOST,'python3 -c '+shlex.quote('import shutil; shutil.rmtree('+repr(base)+')')])
    print('TOOLS_CLEAN_VERIFY_PASS scope=mybackup-native-build',flush=True)
    print('MYBACKUP_NATIVE_BUILD_ACCEPTED sha256='+result['artifact'],flush=True)


def recover(directory):
    directory=Path(directory).resolve()
    if directory.parent!=ROOT/'logs': raise ValueError('MYBACKUP_BUILD_RECOVERY_LOCAL_SCOPE')
    plan=json.loads((directory/'plan.json').read_text()); base=plan['base']
    if not re.fullmatch(r'/tmp/b80build-[a-f0-9]{12}',base): raise ValueError('MYBACKUP_BUILD_RECOVERY_REMOTE_SCOPE')
    code="""import pathlib,json,shutil
base=pathlib.Path(BASE)
assert json.loads((base/'plan.json').read_text())['base']==str(base)
assert (base/'state.finalized').read_text().strip()!='0'
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try: command=path.read_bytes()
 except (FileNotFoundError,ProcessLookupError,PermissionError): continue
 assert str(base/'venv').encode() not in command
assert not (base/'venv').exists() and not (base/'build').exists()
shutil.rmtree(base)
""".replace('BASE',repr(base))
    with infrastructure().control_leases(): run([*SSH,HOST,'python3 -c '+shlex.quote(code)])
    (directory/'cleanup.log').write_text('MYBACKUP_FAILED_BUILD_CLEAN_VERIFY_PASS\n')
    print('MYBACKUP_FAILED_BUILD_CLEAN_VERIFY_PASS',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('stage',choices=('local','remote','recover'))
    parser.add_argument('--run-directory',type=Path)
    args=parser.parse_args()
    if args.stage=='remote': remote()
    elif args.stage=='recover': recover(args.run_directory)
    else: launch()
