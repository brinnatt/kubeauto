#!/usr/bin/env python3
"""Narrow product CRD update gate; does not sign off the complete upgrade item."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile

import yaml


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def requested_config(original):
    data = yaml.safe_load(original)
    if (data.get('redis_mode') != 'sentinel' or data.get('redis_namespace') != 'redis-gate' or
            data.get('redis_crd_upgrade_requested', False) is not False):
        raise ValueError('CRD gate requires its owned baseline and an inactive request')
    return yaml.safe_dump(dict(data, redis_crd_upgrade_requested=True), sort_keys=False).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('crd-upgrade',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_crd_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_crd_clients')
    capacity = load(args.base, 'redis-capacity-gate.py', 'redis_crd_inputs')
    with tempfile.TemporaryDirectory(prefix='redis-crd-gate-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        cluster = clients.Cluster(source, focused, Path(temporary))
        cluster.health()
        config = source.cluster / 'config.yml'
        original = config.read_bytes()
        requested = requested_config(original)
        claims = focused.resource_identities(source.get('pvc')['items'])
        dataset = {'kubeauto:redis:cluster:' + source.run_id + '{crd-' + str(i) + '}':
                    ('retained-crd-' + str(i)).encode() for i in range(12)}
        cluster.keys.update(dataset)
        fault = source.cluster / 'redis-crd-upgrade-fault.json'
        failure = None
        changed = False
        try:
            client = source.client(source.primary())
            if client.command('SET', source.key, source.value, 'EX', 3600) != b'OK':
                raise ValueError('CRD gate Sentinel seed rejected')
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('CRD gate Sentinel seed lacks durable copies')
            for key, value in dataset.items():
                member = cluster.client(cluster.owner(key), fresh=True)
                if (member.command('SET', key, value, 'EX', 3600) != b'OK' or
                        member.command('WAITAOF', 1, 2, 5000) != [1, 2]):
                    raise ValueError('CRD gate Cluster seed lacks durable copies')
            with fault.open('x') as out:
                fault.chmod(0o600)
                json.dump({'run_id': source.run_id, 'cluster_uid': cluster.journal['cluster_uid'],
                           'original_config_sha256': hashlib.sha256(original).hexdigest()}, out)
                out.flush()
                os.fsync(out.fileno())
            changed = True
            capacity.replace_config(config, [original], requested)
            source.run([str(args.base / '.venv/bin/python'), str(args.base / 'kubecli.py'),
                        'setup', 'redis-gate', '07', '--tags', 'redis'], timeout=1100, capture=False)
            cluster.health()
            if source.client(source.primary(), fresh=True).command('GET', source.key) != source.value:
                raise ValueError('CRD update changed acknowledged Sentinel data')
            if any(cluster.command(key, 'GET', key) != value for key, value in dataset.items()):
                raise ValueError('CRD update changed acknowledged Cluster data')
            if claims != focused.resource_identities(source.get('pvc')['items']):
                raise ValueError('CRD update replaced a customer PVC')
            print('REDIS_CRD_PRODUCT_UPGRADE_PASS entry=config+kubecli data-and-pvc=retained '
                  'acceptance=partial-no-version-migration', flush=True)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                if changed:
                    capacity.replace_config(config, [original, requested], original)
                    if json.loads(fault.read_text())['run_id'] != source.run_id:
                        raise ValueError('CRD gate mutation journal identity changed')
                cluster.cleanup()
                source.cleanup_keys()
                if changed:
                    fault.unlink()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_CRD_GATE_CLEANUP_FAIL original failure retained journal=retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=crd-upgrade product-schema-backups=retained', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=crd-upgrade', flush=True)


if __name__ == '__main__':
    main()
