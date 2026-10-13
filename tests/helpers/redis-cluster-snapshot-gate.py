#!/usr/bin/env python3
"""Exercise the product Cluster RDB export without changing its source config."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import signal
import tempfile

import yaml


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def snapshot_inventory(text, name):
    old = 'cluster_dir="{{ base_dir }}/clusters/redis-cluster-gate"'
    if not re.fullmatch(r'redis-snapshot-[0-9a-f]{32}', name) or text.count(old) != 1:
        raise ValueError('snapshot refuses an unexplained product inventory or request name')
    return text.replace(old, 'cluster_dir="{{ base_dir }}/clusters/' + name + '"')


def verify_exports(directory, manifest, image):
    if (manifest.get('mode') != 'cluster' or manifest.get('image') != image or
            manifest.get('restore_verified') is not False or len(manifest.get('files', [])) != 3):
        raise ValueError('Cluster export must contain three original shard artifacts')
    ids, slots = set(), set()
    for row in manifest['files']:
        identifier = row.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{40}', identifier) or identifier in ids or row['file'] != identifier + '.rdb':
            raise ValueError('Cluster export shard identity is invalid or duplicated')
        ids.add(identifier)
        for token in row['slots']:
            if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?', token):
                raise ValueError('Cluster export contains an unstable slot range')
            bounds = [int(n) for n in token.split('-')]
            start, end = bounds[0], bounds[-1]
            if not 0 <= start <= end < 16384:
                raise ValueError('Cluster export slot range is invalid')
            owned = set(range(start, end + 1))
            if slots & owned:
                raise ValueError('Cluster export slot ranges overlap')
            slots.update(owned)
        path = directory / row['file']
        if path.is_symlink() or not path.is_file():
            raise ValueError('Cluster export requires regular original RDB files')
        data = path.read_bytes()
        if (len(data) < 16 or data[:5] != b'REDIS' or len(data) != row['size'] or
                hashlib.sha256(data).hexdigest() != row['sha256']):
            raise ValueError('Cluster export size/header/digest differs')
    if slots != set(range(16384)):
        raise ValueError('Cluster export does not cover all slots')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cluster-snapshot',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_snapshot_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_snapshot_cluster')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-snapshot-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack); source.baseline()
        cluster = clients.Cluster(source, focused, Path(tmp)); cluster.health()
        name = 'redis-snapshot-' + source.run_id
        target = source.base / 'clusters' / name
        original = (cluster.target / 'config.yml').read_bytes()
        claims = focused.resource_identities(source.get('pvc')['items'])
        config = yaml.safe_load(original)
        config.update(CLUSTER_NAME=name, redis_snapshot_requested=True,
                      redis_snapshot_directory=str(Path(tmp) / 'export'))
        image = 'registry.talkschool.cn:5000/brinnatt/redis:' + config['redis_cluster_image_ver']
        target.mkdir(mode=0o700)
        try:
            (target / 'owner.json').write_text(json.dumps({'run_id':source.run_id}))
            (target / 'config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
            (target / 'config.yml').chmod(0o600)
            (target / 'hosts').write_text(snapshot_inventory((cluster.target / 'hosts').read_text(), name))
            shutil.copyfile(source.kubeconfig, target / 'kubectl.kubeconfig')
            (target / 'kubectl.kubeconfig').chmod(0o600)
            source.run([str(source.base / '.venv/bin/python'), str(source.base / 'kubecli.py'),
                        'setup', name, '07', '--tags', 'redis-snapshot'], timeout=2000, capture=False)
            directory = Path(config['redis_snapshot_directory'])
            verify_exports(directory, json.loads((directory / 'manifest.json').read_text()), image)
            if original != (cluster.target / 'config.yml').read_bytes():
                raise ValueError('Cluster snapshot changed source product configuration')
            if claims != focused.resource_identities(source.get('pvc')['items']):
                raise ValueError('Cluster snapshot replaced a customer PVC')
            cluster.health(); source.health()
            if any(p['metadata'].get('labels', {}).get('kubeauto.io/component') == 'redis-snapshot'
                   for p in source.get('pods')['items']):
                raise ValueError('Cluster snapshot left its temporary export Pod')
            print('REDIS_CLUSTER_SNAPSHOT_PASS shards=3 slots=16384 crc=verified sha256=verified source-pvcs=retained', flush=True)
        finally:
            if (target.is_symlink() or json.loads((target / 'owner.json').read_text()) != {'run_id':source.run_id}):
                raise ValueError('snapshot cleanup refuses a changed private request directory')
            shutil.rmtree(target)
        print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=cluster-snapshot', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=cluster-snapshot', flush=True)


if __name__ == '__main__':
    main()
