"""Add checksum-qualified Harbor/Docker payloads to a new local staged tree.

No network, build, tag overwrite or lab operation is performed here.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile

from upgrade.lab.next_inputs import stage
from upgrade.lab.next_session import SOURCE_COMMIT, sha, write, verify_inputs


PACKAGES = {
    'v2.13.0': '4be66658984a97190529280f462d5acbdc967583e358af5369f619d60ac9bd76',
    'v2.14.6': 'f3adaff30c4531c58f9d6ee8c5164f95f08582651c12b698b92be9e98df55cf6',
    'v2.16.0': '643c137d8e829c857ef0ba45c46b5d6d8553077340bbd4929ded7e92808e77ff',
}
DOCKER_SHA = 'ea90cfd12e1eeb12aa1c971741adb8bd4ed88e2a574eaac13f5029a1dbc6300d'
DOCKER_FILES = {'docker','dockerd','docker-init','docker-proxy','containerd',
                'containerd-shim-runc-v2','ctr','runc'}


def verify_payloads(output):
    output=Path(output)
    docker=output/'target-product/down/docker-28.5.2.tgz'
    if docker.is_symlink() or sha(docker)!=DOCKER_SHA:
        raise ValueError('qualified original Docker archive required')
    expected={}
    with tarfile.open(docker) as archive:
        for member in archive:
            path=Path(member.name)
            if member.isdir() and path==Path('docker'):continue
            if not member.isfile() or path.parts[0]!='docker' or len(path.parts)!=2 or path.name not in DOCKER_FILES:
                raise ValueError('unexpected Docker archive member')
            with archive.extractfile(member) as stream:
                expected[path.name]=hashlib.file_digest(stream,'sha256').hexdigest()
    if set(expected)!=DOCKER_FILES:raise ValueError('Docker archive binary set differs')
    for name in ['source-product','target-product']:
        directory=output/name/'docker-bin'
        if {p.name for p in directory.iterdir()}!=DOCKER_FILES | {'cri-dockerd'}:
            raise ValueError('staged Docker binary set differs')
        for filename,digest in expected.items():
            path=directory/filename
            if path.is_symlink() or sha(path)!=digest:raise ValueError('staged Docker bytes differ')
        bridge=output/name/'extra-bin/cri-dockerd'
        if bridge.is_symlink() or (directory/'cri-dockerd').is_symlink() or sha(directory/'cri-dockerd')!=sha(bridge):
            raise ValueError('staged CRI bridge differs from the qualified ext-bin payload')
        versions=['v2.13.0'] if name=='source-product' else ['v2.14.6','v2.16.0']
        for version in versions:
            path=output/name/'down'/('harbor-offline-installer-'+version+'.tgz')
            if path.is_symlink() or sha(path)!=PACKAGES[version]:raise ValueError('staged Harbor bytes differ')


def add_payloads(output, payloads):
    output, payloads = Path(output), Path(payloads)
    docker = payloads/'docker-28.5.2-official.tgz'
    if docker.is_symlink() or sha(docker) != DOCKER_SHA:
        raise ValueError('Docker payload differs from its qualified content identity')
    packages = {version: payloads/('harbor-offline-'+version+'-amd64.tgz') for version in PACKAGES}
    for version,path in packages.items():
        if path.is_symlink() or sha(path) != PACKAGES[version]:
            raise ValueError('Harbor official AMD64 payload differs: '+version)
    for name in ['source-product','target-product']:
        bridge=output/name/'extra-bin/cri-dockerd'
        if bridge.is_symlink() or not bridge.is_file():
            raise ValueError('qualified ext-bin CRI bridge required by the Docker role')
    for name in ['source-product','target-product']:
        root=output/name
        destination=root/'docker-bin'
        if destination.exists() or destination.is_symlink():
            raise ValueError('Docker staging cannot overwrite an existing payload')
        destination.mkdir()
        with tarfile.open(docker) as archive:
            for member in archive:
                path=Path(member.name)
                if member.isdir() and path==Path('docker'):continue
                if (not member.isfile() or len(path.parts)!=2 or path.parts[0]!='docker' or
                        path.name in ['.','..']):
                    raise ValueError('unexpected Docker archive member')
                with archive.extractfile(member) as stream,(destination/path.name).open('xb') as target:
                    shutil.copyfileobj(stream,target)
                (destination/path.name).chmod(0o755)
        shutil.copy2(root/'extra-bin/cri-dockerd',destination/'cri-dockerd')
        (destination/'cri-dockerd').chmod(0o755)
        down=root/'down';down.mkdir(exist_ok=True)
        versions=['v2.13.0'] if name=='source-product' else ['v2.14.6','v2.16.0']
        for version in versions:
            target=down/('harbor-offline-installer-'+version+'.tgz')
            if target.exists() or target.is_symlink():raise ValueError('Harbor staged payload already exists')
            shutil.copy2(packages[version],target)
    shutil.copy2(docker,output/'target-product/down/docker-28.5.2.tgz')
    verify_payloads(output)
    trees={name:{str(p.relative_to(output/name)):sha(p) for p in sorted((output/name).rglob('*')) if p.is_file()}
           for name in ['source-product','target-product']}
    write(output/'input-manifest.json',{'schema_version':1,'source_commit':SOURCE_COMMIT,'trees':trees})
    verify_inputs(output,output/'target-product')
    print('HARBOR_LOCAL_INPUTS_PASS official-amd64=2130,2146,2160 docker=28.5.2 mutation=none')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    for option in ['source-extra','source-k8s','target-k8s','payloads']:
        parser.add_argument('--'+option,type=Path,required=True)
    args=parser.parse_args()
    stage(args.output,source_extra=args.source_extra,source_k8s=args.source_k8s,target_k8s=args.target_k8s)
    add_payloads(args.output,args.payloads)


if __name__=='__main__':main()
