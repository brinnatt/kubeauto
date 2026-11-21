"""Stage exact source code and already qualified packs; no fetch/build/publish."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from upgrade.lab.next_session import ROOT, SOURCE_COMMIT, sha, verify_inputs, write
from upgrade.product.hop import SOURCE, TARGET, DIRECTORY


def check_files(directory, files):
    for name, digest in files.items():
        path = directory/name
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                path.is_symlink() or not path.is_file() or sha(path) != digest):
            raise ValueError('qualified input content differs: '+str(path))


def stage(output, *, source_extra, source_k8s, target_k8s):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('use a new private staging directory; existing inputs are never overwritten')
    records = DIRECTORY/'evidence'
    source_extra = Path(source_extra)
    binaries = {'source-product': (Path(source_k8s),
                    ROOT/'upgrade'/SOURCE/'evidence/k8s-binary-content.json'),
                'target-product': (Path(target_k8s),
                    records/'k8s-binary-content.json')}
    pack=json.loads((records/'source-pack-content.json').read_text())
    if pack['image'] != 'hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.17.1':
        raise ValueError('source must use the accepted maintenance pack, not the historical migration pack')
    if source_extra.is_symlink() or {str(p.relative_to(source_extra)) for p in source_extra.rglob('*')
            if not p.is_dir() or p.is_symlink()} != set(pack['files']):
        raise ValueError('source pack contains missing, extra or symbolic-link inputs')
    check_files(source_extra, pack['files'])
    binary_files = {}
    for name, (directory, record) in binaries.items():
        binary_record=json.loads(record.read_text())
        version=SOURCE if name=='source-product' else TARGET
        if binary_record['image'] != 'hub.talkedu.cn/kubeauto/kubeauto-k8s-bin:'+version:
            raise ValueError('Kubernetes package version differs from the active adjacent hop')
        info = next(a for a in binary_record['architectures'] if a['architecture']=='amd64')
        binary_files[name] = {n: v['sha256'] for n,v in info['files'].items() if n!='official.sha256'}
        check_files(directory, binary_files[name])
    output.mkdir(mode=0o700, parents=True)
    source = output/'source-product'; target = output/'target-product'
    source.mkdir(); target.mkdir()
    archive = subprocess.check_output(['git','archive',SOURCE_COMMIT], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter='data')
    del archive
    current = subprocess.check_output(['git','ls-files','-c','-o','--exclude-standard','-z'], cwd=ROOT)
    paths = sorted(set(Path(p.decode()) for p in current.split(b'\0') if p))
    for relative in paths:
        path = ROOT/relative
        if not path.is_file():continue  # removed tracked input
        if path.is_symlink():raise ValueError('unexpected product symlink: '+str(relative))
        destination = target/relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    # Current acceptance may reference ignored raw evidence. Copy that narrow
    # evidence tree privately; never collect arbitrary HOME or registry state.
    raw = records/'raw'
    if raw.is_dir():shutil.copytree(raw, target/'upgrade'/TARGET/'evidence/raw', dirs_exist_ok=True)
    for product in [source, target]:
        shutil.copytree(source_extra, product/'extra-bin')
    for name, (directory, _) in binaries.items():
        destination=output/name/'kube-bin';destination.mkdir()
        for filename in binary_files[name]:
            shutil.copy2(directory/filename,destination/filename)
            (destination/filename).chmod(0o755)
    trees={name:{str(p.relative_to(output/name)):sha(p) for p in sorted((output/name).rglob('*')) if p.is_file()}
           for name in ['source-product','target-product']}
    write(output/'input-manifest.json', {'schema_version':1,'source_commit':SOURCE_COMMIT,'trees':trees})
    verify_inputs(output,target)
    print('NEXT_SESSION_LOCAL_INPUTS_PASS source_commit='+SOURCE_COMMIT+' staging='+str(output))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--source-extra',type=Path,required=True,
                        help='Verified ext-bin 1.17.1 /extra directory; reused by both products')
    parser.add_argument('--source-k8s',type=Path,required=True,
                        help='Verified v1.35.10 six-binary directory')
    parser.add_argument('--target-k8s',type=Path,required=True,
                        help='Verified v1.36.6 six-binary directory')
    args=parser.parse_args()
    stage(args.output,source_extra=args.source_extra,source_k8s=args.source_k8s,target_k8s=args.target_k8s)
