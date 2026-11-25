#!/usr/bin/env python3
"""Admit exact native MySQL artifacts before any binary-topology lab mutation.

This is the supply-chain prerequisite, not lifecycle acceptance. Live execution
remains closed until its runner and the declared source set are qualified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / 'tests/evidence/mysql80-binary-sources.json'
DOMESTIC = frozenset(('mirrors.huaweicloud.com', 'repo.huaweicloud.com',
                      'mirrors.cloud.tencent.com', 'mirrors.aliyun.com',
                      'mirrors.tuna.tsinghua.edu.cn', 'mirrors.163.com', 'mirrors.nju.edu.cn'))
PROFILES = {'rocky8': '8.10', 'rocky9': '9.8'}
VERSIONS = ('8.0.33', '8.0.46')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def domestic_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in DOMESTIC or
            parsed.username or parsed.password or parsed.port not in (None, 443) or
            parsed.fragment or parsed.query):
        raise ValueError('BINARY80_DOMESTIC_SOURCE_REFUSED')
    return url


class DomesticRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        domestic_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def catalog(path=CATALOG):
    path = Path(path)
    if not path.is_file():
        raise ValueError('BINARY80_SOURCES_MISSING: need exact 8.0.33/8.0.46 native artifacts for Rocky 8.10/9.8; no SSH or lab mutation')
    data = json.loads(path.read_text())
    records = data.get('artifacts', {})
    expected = {os_name + '/' + version for os_name in PROFILES for version in VERSIONS}
    if set(records) != expected:
        raise ValueError('BINARY80_SOURCE_SET_INCOMPLETE ' + ','.join(sorted(expected - set(records))))
    for name, record in records.items():
        os_name, version = name.split('/')
        if (record.get('version') != version or record.get('os') != PROFILES[os_name] or
                record.get('architecture') != 'x86_64' or
                not re.fullmatch('[a-f0-9]{64}', record.get('sha256', '')) or
                not record.get('checksum_source') or record.get('format') not in ('tar.xz', 'tar.gz', 'rpm')):
            raise ValueError('BINARY80_SOURCE_IDENTITY_REFUSED ' + name)
        domestic_url(record['url'])
        if record.get('abi') not in (('glibc2.17', 'glibc2.28', 'el7', 'el8') if os_name == 'rocky8'
                                     else ('glibc2.17', 'glibc2.28', 'glibc2.34', 'el9')):
            raise ValueError('BINARY80_SOURCE_ABI_REFUSED ' + name)
        companions = record.get('companions', {})
        if not isinstance(companions, dict):
            raise ValueError('BINARY80_COMPANION_SET_REFUSED ' + name)
        for companion in companions.values():
            if (not re.fullmatch('[a-f0-9]{64}', companion.get('sha256', '')) or
                    companion.get('format') != 'rpm' or not companion.get('checksum_source')):
                raise ValueError('BINARY80_COMPANION_IDENTITY_REFUSED ' + name)
            domestic_url(companion['url'])
    return records


def prepare(records, cache):
    cache = Path(cache); cache.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(DomesticRedirect())
    downloads = dict(records)
    for name, record in records.items():
        downloads.update({name+'/'+key: value for key, value in record.get('companions', {}).items()})
    for name, record in downloads.items():
        destination = cache / (record['sha256'] + '.' + record['format'])
        if destination.is_file() and sha(destination) == record['sha256']:
            print('BINARY80_ARTIFACT_REUSED ' + name, flush=True)
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, suffix='.partial', delete=False) as stream:
                temporary = Path(stream.name)
                with opener.open(domestic_url(record['url']), timeout=30) as response:
                    domestic_url(response.geturl())
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
            if sha(temporary) != record['sha256']:
                raise ValueError('BINARY80_DOWNLOAD_CHECKSUM_REFUSED ' + name)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print('BINARY80_ARTIFACT_PREPARED ' + name, flush=True)


def preflight():
    subprocess.run([sys.executable, '-B', ROOT/'tests/helpers/validate_tools_test_matrix.py',
                    ROOT/'tests/tools-test-matrix.yaml'], check=True)
    with tempfile.TemporaryDirectory(prefix='mysql80-binary-contract-') as temporary:
        subprocess.run([sys.executable, '-B', '-m', 'unittest',
                        'tests.unit.test_mybackup_replication_contract',
                        'tests.unit.test_mybackup_cli_contract',
                        'tests.unit.test_mybackup_offline_contract',
                        'tests.unit.test_mysql80_binary_artifact_contract', '-q'], cwd=ROOT,
                       env=dict(os.environ, KUBEAUTO_LOG_DIR=temporary,
                                MYSQLBACKUP_LOG_FILE=temporary+'/product.log', PYTHONDONTWRITEBYTECODE='1'),
                       check=True)
    print('BINARY80_LOCAL_PREFLIGHT_PASS; live runner and lifecycle evidence remain pending', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('preflight', 'plan', 'prepare', 'standalone',
                                         'primary-replica', 'dual-primary', 'probe', 'replication-probe', 'build', 'full'))
    parser.add_argument('--catalog', type=Path, default=CATALOG)
    parser.add_argument('--cache', type=Path,
                        default=Path.home()/'.cache/kubeauto/mysql80-binary')
    args = parser.parse_args()
    if args.stage == 'preflight':
        preflight()
        return
    records = catalog(args.catalog)
    if args.stage == 'build':
        preflight()
        with tempfile.TemporaryDirectory(prefix='mybackup-build-contract-') as temporary:
            subprocess.run([sys.executable, '-B', '-m', 'unittest',
                            'tests.unit.test_mybackup_native_build_contract', '-q'], cwd=ROOT,
                           env=dict(os.environ, KUBEAUTO_LOG_DIR=temporary), check=True)
        subprocess.run([sys.executable, '-B', ROOT/'tests/helpers/build-mybackup-native.py', 'local'], check=True)
    elif args.stage == 'prepare':
        prepare(records, args.cache)
    elif args.stage == 'plan':
        print(json.dumps({'artifacts': records, 'topologies': ['standalone', 'primary-replica', 'dual-primary'],
                          'gtid': ['OFF', 'ON'], 'live_qualified': False}, ensure_ascii=False, indent=2))
    else:
        subprocess.run([sys.executable, '-B', ROOT/'tests/helpers/mysql80-binary-live.py', args.stage], check=True)


if __name__ == '__main__':
    main()
