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


def check_files(directory, files):
    for name, digest in files.items():
        path = directory/name
        if path.is_symlink() or sha(path) != digest:
            raise ValueError('qualified input content differs: '+str(path))


def stage(output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('use a new private staging directory; existing inputs are never overwritten')
    records = ROOT/'upgrade/v1.35.10/evidence'
    source_extra = Path('/tmp/kubeauto-extbin-source-qualified/amd64/extra')
    overlay = Path('/tmp/kubeauto-extbin-overlay-qualified/amd64/extra')
    binaries = {'source-product': (Path('/tmp/kubeauto-upgrade-k8s13413-native'),
                    ROOT/'upgrade/v1.34.13/evidence/k8s-binary-content.json'),
                'target-product': (Path('/tmp/kubeauto-k8s13510-qualified/amd64/k8s'),
                    records/'k8s-binary-content.json')}
    check_files(source_extra, json.loads((records/'source-pack-content.json').read_text())['files'])
    pins = json.loads((ROOT/'upgrade/v1.35.10/artifact-pins.json').read_text())
    for item in pins['runtime']['amd64'].values():
        check_files(overlay/item['directory'], item['sha256'])
    check_files(overlay, pins['etcd']['amd64'])
    check_files(overlay, {'calicoctl': pins['calicoctl']['v3.32.4']['amd64'],
                        'upgrade/calicoctl-v3.31.7': pins['calicoctl']['v3.31.7']['amd64']})
    binary_files = {}
    for name, (directory, record) in binaries.items():
        info = next(a for a in json.loads(record.read_text())['architectures'] if a['architecture']=='amd64')
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
    if raw.is_dir():shutil.copytree(raw, target/'upgrade/v1.35.10/evidence/raw', dirs_exist_ok=True)
    for product in [source, target]:
        shutil.copytree(source_extra, product/'extra-bin')
    shutil.copytree(overlay, target/'extra-bin', dirs_exist_ok=True)
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
    stage(parser.parse_args().output)
