#!/usr/bin/env python3
"""Reuse one complete local static result only for identical inputs and tools.

The final clean full runner calls longhorn-static.sh directly and never reads
this index. No live/module result or registry availability is cached here.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

BASE=Path(__file__).resolve().parents[2]
FAILURE=re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')

def sha(path):
    if path.is_symlink() or not path.is_file():raise ValueError('static input is not a regular file')
    return hashlib.sha256(path.read_bytes()).hexdigest()

def fingerprint(root):
    files={}
    siblings=('kubeauto','kubeauto-dockerfile','kubeauto-k8s-bin-dockerfile','kubeauto-ext-bin-dockerfile',
              'kubeauto-ext-bin-sp1-dockerfile','kubeauto-ext-images-dockerfile')
    for name in siblings:
        repo=root if name=='kubeauto' else root.parent/name
        listed=subprocess.run(['git','ls-files','-c','-o','--exclude-standard','-z'],cwd=repo,
            capture_output=True,check=True).stdout.decode().split('\0')
        paths={repo/p for p in listed if p}
        # Include ignored chart/schema fixtures consumed by the static suite.
        if name=='kubeauto':
            for prefix in ('roles','playbooks','tests/fixtures'):
                paths.update(p for p in (repo/prefix).rglob('*') if p.is_file())
        for path in sorted(paths):
            if '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'):continue
            files[name+'/'+str(path.relative_to(repo))]=sha(path)
    tools={}
    for executable in (sys.executable,os.environ.get('LONGHORN_HELM','helm'),
                       os.environ.get('LONGHORN_ANSIBLE_PLAYBOOK','ansible-playbook'),'bash'):
        resolved=shutil.which(executable)
        if not resolved:raise ValueError('missing static executable')
        path=Path(resolved).resolve(strict=True);tools[str(path)]=sha(path)
    packages=sorted((d.metadata['Name'],d.version,d.read_text('RECORD')) for d in importlib.metadata.distributions())
    env={k:v for k,v in os.environ.items() if k not in ('_','SHLVL','OLDPWD')}
    row={'sources':files,'tools':tools,'packages':packages,'environment':env,'python':sys.version,
         'root':str(root.resolve()),'boot':Path('/proc/sys/kernel/random/boot_id').read_text()}
    # Store only the digest; environment may contain runtime credentials.
    return hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()

def write(path,row):
    temporary=path.with_name(path.name+'.new')
    with temporary.open('x') as stream:json.dump(row,stream,sort_keys=True);stream.write('\n')
    os.chmod(temporary,0o600);temporary.replace(path)

def verify(directory,current):
    for name in ('static-inputs.json','status.json','gate.log'):sha(directory/name)
    if json.loads((directory/'static-inputs.json').read_text())!={'fingerprint':current}:
        raise ValueError('static inputs or execution environment changed')
    status=json.loads((directory/'status.json').read_text());raw=(directory/'gate.log').read_bytes();lines=raw.decode().splitlines()
    if (status.get('rc')!=0 or status.get('failure_markers')!=0 or not status.get('marker_present') or
            status.get('marker')!='LONGHORN_STATIC_PASS' or status.get('log_sha256')!=hashlib.sha256(raw).hexdigest() or
            any(FAILURE.match(line) for line in lines) or
            any(lines.count(marker)!=1 for marker in ('LONGHORN_STATIC_PASS','LONGHORN_STATIC_SCOPED_CLEAN_PASS'))):
        raise ValueError('static execution is incomplete, failed or changed')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('begin','finish','reuse'))
    parser.add_argument('--root',type=Path,default=BASE);parser.add_argument('--directory',type=Path)
    args=parser.parse_args();root=args.root.resolve();index=root/'logs/longhorn-cases/static-reuse-index.json'
    current=fingerprint(root)
    if args.action=='reuse':
        if not index.exists():raise ValueError('no prior static execution')
        sha(index);row=json.loads(index.read_text());directory=root/row['directory']
        if directory.parent!=index.parent or not directory.name.startswith('static-') or directory.is_symlink():
            raise ValueError('static reuse points outside its local owning directory')
        verify(directory,current)
        print('LONGHORN_STATIC_REUSED directory='+str(directory)+' fingerprint='+current)
    elif args.action=='begin':write(args.directory/'static-inputs.json',{'fingerprint':current})
    else:
        verify(args.directory,current)
        write(index,{'directory':str(args.directory.relative_to(root))})

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        print('LONGHORN_STATIC_REUSE_UNAVAILABLE',file=sys.stderr);sys.exit(1)
