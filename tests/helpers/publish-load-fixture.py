#!/usr/bin/env python3
"""Real Docker/nerdctl SSH load failures, retries and frozen/source CLI readback."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

TARGET = '192.168.122.2'
SOURCE = 'hub.talkedu.cn/kubeauto/pause@sha256:1d048b53f4285cc9d20fbb8d7be785c50e9e4ccf4cf1194d9b176001862d900a'


def assert_failed_load(result):
    if result.returncode == 0 or '镜像加载失败' not in result.stdout+result.stderr:
        raise ValueError('PUBLISH_LOAD_EXPECTED_FAILURE_MISSING')


def main():
    root=Path.cwd()
    if root.parent!=Path('/tmp') or not root.name.startswith('tools-publish-'):
        raise ValueError('PUBLISH_LOAD_SCOPE_REFUSED')
    work=root/'fixture'; logs=root/'diagnostics'
    if work.exists():raise ValueError('PUBLISH_LOAD_FOREIGN_FIXTURE_REFUSED')
    work.mkdir(mode=0o700);logs.mkdir(mode=0o700)
    namespace=root.name
    tag=namespace+':fixture'
    ssh=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=no','-o','ConnectTimeout=10','-i',str(root/'key'),'root@'+TARGET]
    def command(argv,name,input=None,check=True):
        r=subprocess.run([str(x) for x in argv],input=input,text=True,capture_output=True,timeout=180)
        (logs/(name+'.log')).write_text(r.stdout+r.stderr)
        if check and r.returncode:raise ValueError('PUBLISH_LOAD_COMMAND_REFUSED '+name+' '+(r.stdout+r.stderr)[-2000:])
        return r
    def remote(text,name,**kwargs):return command([*ssh,text],name,**kwargs)
    def runtime_args(runtime,*args):return [runtime,*(['-n',namespace] if runtime=='nerdctl' else []),*args]
    def absent(runtime,name):
        r=remote(' '.join(runtime_args(runtime,'image','inspect',tag)),name,check=False)
        if r.returncode==0:raise ValueError('PUBLISH_LOAD_FOREIGN_IMAGE_REFUSED')
    # Refuse an existing namespace before registering cleanup of anything remote.
    command(['docker','info'],'docker-info')
    remote('docker info >/dev/null && nerdctl info >/dev/null','target-info')
    existing=remote('ctr namespaces list -q','namespaces-before').stdout.splitlines()
    if namespace in existing:raise ValueError('PUBLISH_LOAD_FOREIGN_NAMESPACE_REFUSED')
    if command(['docker','image','inspect',tag],'local-before',check=False).returncode==0:
        raise ValueError('PUBLISH_LOAD_FOREIGN_IMAGE_REFUSED')
    for runtime in ('docker','nerdctl'):absent(runtime,'before-'+runtime)
    def interrupted(signum, frame):
        raise InterruptedError('PUBLISH_LOAD_INTERRUPTED '+str(signum))
    for signum in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(signum,interrupted)
    try:
        for mode in ('source','frozen'):
            cli=([sys.executable,'-B',str(root/'KubePublishCli.py')] if mode=='source' else [str(root/'KubePublishCli')])
            command([*cli,'--download',SOURCE],mode+'-download')
            source_id=command(['docker','image','inspect',SOURCE,'--format','{{.Id}}'],mode+'-source-id').stdout.strip()
            command(['docker','tag',SOURCE,tag],mode+'-tag')
            pack=work/(mode+'-pack');pack.mkdir()
            command([*cli,'--pack',tag,'--output-dir',pack],mode+'-pack')
            archives=list(pack.glob('images_batch_*.tar'))
            if len(archives)!=1:raise ValueError('PUBLISH_LOAD_ARCHIVE_MISSING')
            for runtime in ('docker','nerdctl'):
                stage=work/(mode+'-'+runtime);stage.mkdir()
                invalid=stage/'000-invalid.tar';invalid.write_bytes(b'invalid archive: fixture-owned first load must fail')
                valid=stage/'999-valid.tar';shutil.copy2(archives[0],valid)
                before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir()}
                args=[*cli,'--distribute',TARGET,'--output-dir',stage,'--remote-runtime',runtime,
                      '--namespace',namespace,'--ssh-key',root/'key','--disable-ssh-host-check']
                result=command(args,mode+'-'+runtime+'-first-failure',check=False);assert_failed_load(result)
                absent(runtime,mode+'-'+runtime+'-no-second-load')
                if before!={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir()}:
                    raise ValueError('PUBLISH_LOAD_CUSTOMER_ARCHIVE_MUTATED')
                invalid.unlink()
                command(args,mode+'-'+runtime+'-retry')
                actual=remote(' '.join(runtime_args(runtime,'image','inspect',tag,'--format',"'{{.Id}}'")),mode+'-'+runtime+'-readback').stdout.strip()
                if actual!=source_id:raise ValueError('PUBLISH_LOAD_IMAGE_ID_MISMATCH')
                command([*cli,'--delete',tag,'--delete-hosts',TARGET,'--remote-runtime',runtime,
                         '--namespace',namespace,'--ssh-key',root/'key','--disable-ssh-host-check'],mode+'-'+runtime+'-delete',input='yes\n')
                absent(runtime,mode+'-'+runtime+'-deleted')
                print('PUBLISH_LOAD_READBACK_PASS mode='+mode+' runtime='+runtime+' first=refused second=not-loaded retry=id-matched customer=unchanged',flush=True)
            command([*cli,'--delete',tag],mode+'-delete-local',input='yes\n')
        print('PUBLISH_LOAD_LIVE_PASS modes=2 runtimes=2 ssh=real',flush=True)
    finally:
        command(['docker','image','rm',tag],'cleanup-local',check=False)
        for runtime in ('docker','nerdctl'):
            remote(' '.join(runtime_args(runtime,'image','rm',tag)),'cleanup-'+runtime,check=False)
            absent(runtime,'clean-verify-'+runtime)
        # The only containerd namespace admitted above belongs to this fixture.
        remote('ctr namespaces remove '+namespace,'cleanup-namespace',check=False)
        if namespace in remote('ctr namespaces list -q','namespace-final').stdout.splitlines():
            raise ValueError('PUBLISH_LOAD_NAMESPACE_CLEAN_FAILED')
        remote('docker info >/dev/null && nerdctl info >/dev/null','target-health-after')
        shutil.rmtree(work)
        print('PUBLISH_LOAD_SCOPED_CLEAN_PASS archives=removed images=absent namespace=absent runtimes=healthy',flush=True)


if __name__=='__main__':main()
