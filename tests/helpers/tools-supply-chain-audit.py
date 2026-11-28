#!/usr/bin/env python3
"""Verify current standalone builds and the narrowly reused MySQL package proofs."""
import hashlib
import json
from pathlib import Path
import tarfile

ROOT=Path(__file__).resolve().parents[2]


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def input_file(name,expected):
    path=ROOT/name
    if path.is_file() and sha(path)==expected:return path
    if name!='tests/helpers/build-tools-rocky8.sh':raise ValueError('TOOLS_BUILD_INPUT_STALE '+name)
    for directory in ('tools-build-inputs','mysql80-execution-inputs','tools-execution-inputs'):
        path=ROOT/'logs'/directory/(expected+'.sh')
        if path.is_file() and sha(path)==expected:return path
    raise ValueError('TOOLS_BUILDER_SNAPSHOT_MISSING '+expected)


def builds():
    tools={p.stem for p in (ROOT/'tools').rglob('*.py')}-{'StarMigrtCli'}
    found=set();files={}
    for path in sorted((ROOT/'dist').glob('*build.json')):
        proof=json.loads(path.read_text())
        files[str(path.relative_to(ROOT))]=sha(path)
        for name,digest in proof['inputs'].items():
            actual=input_file(name,digest);files[str(actual.relative_to(ROOT))]=digest
        for tool,digest in proof['artifacts'].items():
            artifact=ROOT/'dist'/(tool+'-rocky8')
            if tool not in tools or tool in found or sha(artifact)!=digest:
                raise ValueError('TOOLS_BUILD_ARTIFACT_REFUSED '+tool)
            found.add(tool);files[str(artifact.relative_to(ROOT))]=digest
        if 'glibc 2.28' not in proof['runtime']:raise ValueError('TOOLS_BUILD_RUNTIME_REFUSED')
    if found!=tools:raise ValueError('TOOLS_BUILD_INVENTORY_REFUSED')
    print('TOOLS_CURRENT_BUILD_PROVENANCE_PASS tools='+str(len(found)),flush=True)
    return files


def packages():
    # This record contributes only immutable RPM metadata, signatures and raw
    # archives. Its tool/build assertions are deliberately not consumed.
    proof=json.loads((ROOT/'tests/evidence/mysql80-binary-supply-chain-20261125.json').read_text())
    files={name:digest for name,digest in proof['files'].items()
           if name.startswith(('logs/mysql80-binary-standalone-','logs/mysql80-binary-sources-'))
           or name=='tests/evidence/mysql80-binary-sources.json'}
    for name,digest in files.items():
        if sha(ROOT/name)!=digest:raise ValueError('MYSQL_PACKAGE_RAW_STALE '+name)
    sources=json.loads((ROOT/'tests/evidence/mysql80-binary-sources.json').read_text())
    expected={}
    for identity,artifact in sources['artifacts'].items():
        host=identity.split('/')[0]
        for package in [artifact,*artifact['companions'].values()]:expected[(host,package['sha256'])]=package
    records=proof['rpm_signatures']
    if len(records)!=16 or {(r['host'],r['rpm_sha256']) for r in records}!=set(expected):
        raise ValueError('MYSQL_PACKAGE_SIGNATURE_INVENTORY_REFUSED')
    for record in records:
        with tarfile.open(ROOT/record['archive']) as bundle:
            result=bundle.extractfile(record['member']).read().decode().strip()
        if result!=record['result'] or not result.endswith('digests signatures OK'):
            raise ValueError('MYSQL_PACKAGE_SIGNATURE_REFUSED')
    print('BINARY80_PACKAGE_SIGNATURES_PASS count=16',flush=True)
    return files


def audit():
    files={**builds(),**packages()}
    # The StarMigrt artifact uses its own pinned runtime; it is not one of the
    # glibc2.28 onefile outputs. Verify its owning clean business proof intact.
    proof=json.loads((ROOT/'tests/evidence/starmigrt-full.json').read_text())
    if proof['rc']!=0:raise ValueError('STARMIGRT_SUPPLY_PROOF_REFUSED')
    for group in ('inputs','files'):
        for name,digest in proof[group].items():
            if sha(ROOT/name)!=digest:raise ValueError('STARMIGRT_SUPPLY_INPUT_STALE '+name)
    for name in proof['durable']:
        if (ROOT/name).read_text().strip()!='0':raise ValueError('STARMIGRT_SUPPLY_DURABLE_REFUSED')
    print('TOOLS_CURRENT_SUPPLY_CHAIN_REVIEW_PASS tools=11 packages=16',flush=True)
    return files


if __name__=='__main__':audit()
