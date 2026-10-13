#!/usr/bin/env python3
"""Product 3->4->3 shard changes preserve acknowledged data and expiry."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import tempfile
import time

import yaml


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scale_input(original, shards):
    config = yaml.safe_load(original)
    if (shards not in (3, 4) or config.get('redis_mode') != 'cluster' or
            config.get('redis_name') != 'redis-gate-cluster-data' or
            config.get('redis_cluster_shards') != 3 or config.get('redis_cluster_replicas') != 2):
        raise ValueError('Cluster resize requires the exact owned three-shard input')
    return yaml.safe_dump(dict(config, redis_cluster_shards=shards), sort_keys=False).encode()


def verify_dataset(cluster, dataset, expiry, *, now):
    for key, value in dataset.items():
        if cluster.command(key, 'GET', key) != value:
            raise ValueError('Cluster migration lost a retained key value')
        remaining = cluster.command(key, 'PTTL', key)
        expected = expiry - now()
        if remaining <= 0 or abs(remaining - expected) > 10000:
            raise ValueError('Cluster migration changed absolute key expiry')


def function_fixture(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Cluster Functions fixture requires its exact UUID')
    library = 'kubeauto_resize_' + run_id
    source = ('#!lua name=' + library + '\nredis.register_function("' + library +
              '_get", function(keys, args) return redis.call("GET", keys[1]) end)').encode()
    return library, source


def function_sources(reply):
    if not isinstance(reply, list):
        raise ValueError('Cluster Function library response is malformed')
    libraries = {}
    for row in reply:
        if not isinstance(row, list) or len(row) % 2:
            raise ValueError('Cluster Function library fields are malformed')
        fields = dict(zip(row[::2], row[1::2]))
        name, source = fields.get(b'library_name'), fields.get(b'library_code')
        if not isinstance(name, bytes) or not isinstance(source, bytes) or name in libraries:
            raise ValueError('Cluster Function library identity is malformed')
        libraries[name] = source
    return libraries


def verify_functions(cluster, dataset, library, source, protection):
    for pod in sorted({row[2] for row in cluster.topology()}):
        client = cluster.client(pod, fresh=True)
        if function_sources(client.command('FUNCTION', 'LIST', 'WITHCODE')).get(library.encode()) != source:
            raise ValueError('Cluster resize did not preserve the Function library')
        if client.command('CONFIG', 'GET', 'min-replicas-to-write') != [b'min-replicas-to-write', str(protection).encode()]:
            raise ValueError('Cluster resize changed the replica write barrier')
    for key, value in dataset.items():
        if cluster.command(key, 'FCALL', library + '_get', 1, key) != value:
            raise ValueError('Cluster resize changed Function business results')


def cleanup_functions(cluster, library, source):
    clients = []
    for pod in sorted({row[2] for row in cluster.topology()}):
        client = cluster.client(pod, fresh=True)
        current = function_sources(client.command('FUNCTION', 'LIST', 'WITHCODE')).get(library.encode())
        if current is not None and current != source:
            raise ValueError('Cluster cleanup refuses an unproven Function library')
        if current is not None:
            clients.append(client)
    # Prove all identities before the first deletion. Delete one UUID-owned
    # library; never use FUNCTION FLUSH or touch a customer's libraries.
    for client in clients:
        if client.command('FUNCTION', 'DELETE', library) != b'OK':
            raise ValueError('Cluster owned Function cleanup was rejected')
        if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
            raise ValueError('Cluster Function cleanup lacks durable replication')
        if library.encode() in function_sources(client.command('FUNCTION', 'LIST', 'WITHCODE')):
            raise ValueError('Cluster owned Function library remains')
    print('REDIS_CLUSTER_FUNCTIONS_CLEAN_PASS uuid-library=absent', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cluster-rebalance', 'cluster-rebalance-seed'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cluster_rebalance_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_cluster_rebalance_clients')
    capacity = load(args.base, 'redis-capacity-gate.py', 'redis_cluster_rebalance_inputs')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-rebalance-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        cluster = clients.Cluster(source, focused, Path(temporary))
        cluster.health()
        config_path = cluster.target / 'config.yml'
        original = config_path.read_bytes()
        enlarged = scale_input(original, 4)
        journal_path = source.cluster / 'redis-cluster-fixture.json'
        journal_bytes = journal_path.read_bytes()
        fault_path = source.cluster / 'redis-cluster-scale-fault.json'
        before_claims = focused.resource_identities(cluster.get('pvc')['items'])
        dataset = {'kubeauto:redis:cluster:' + cluster.run_id + '{resize-' + str(i) + '}':
                   ('retained-' + str(i) + '-' + cluster.run_id).encode() for i in range(32)}
        cluster.keys.update(dataset)
        expiry = int(time.time() * 1000) + 7200000
        now = lambda: int(time.time() * 1000)
        failure = None
        changed = False
        library, function_source = function_fixture(cluster.run_id)
        functions_owned = False
        protection = yaml.safe_load(original).get('redis_min_replicas_to_write', 1)
        def setup(shards):
            source.run([str(args.base / '.venv/bin/python'), str(args.base / 'kubecli.py'),
                        'setup', 'redis-cluster-gate', '07', '--tags', 'redis'], timeout=1100, capture=False)
            cluster.health(shards)
        try:
            for key, value in dataset.items():
                if cluster.command(key, 'SET', key, value, 'PXAT', expiry) != b'OK':
                    raise ValueError('Cluster resize seed rejected')
            for pod in sorted({row[2] for row in cluster.topology()}):
                # WAITAOF is connection-relative, so issue one retained write on
                # this same connection before verifying both replica fsync ACKs.
                key = next(key for key in dataset if cluster.owner(key) == pod)
                client = cluster.client(pod, fresh=True)
                client.command('SET', key, dataset[key], 'PXAT', expiry)
                if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                    raise ValueError('Cluster resize dataset lacks durable replication')
            functions_owned = True
            for pod in sorted({row[2] for row in cluster.topology()}):
                client = cluster.client(pod, fresh=True)
                if client.command('FUNCTION', 'LOAD', function_source) != library.encode():
                    raise ValueError('Cluster owned Function library seed was rejected')
                if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                    raise ValueError('Cluster Function seed lacks durable replication')
            verify_functions(cluster, dataset, library, function_source, protection)
            print('REDIS_CLUSTER_FUNCTIONS_SEED_PASS UUID-library=verified business-results=verified write-barrier=retained', flush=True)
            if args.module == 'cluster-rebalance-seed':
                verify_dataset(cluster, dataset, expiry, now=now)
                print('REDIS_CLUSTER_REBALANCE_SEED_PASS durable-data=verified', flush=True)
            else:
                before_slots = {key: cluster.owner(key) for key in dataset}
                with fault_path.open('x') as stream:
                    fault_path.chmod(0o600)
                    json.dump({'run_id': cluster.run_id, 'cr_uid': cluster.journal['cr_uid'],
                               'original_config_sha256': hashlib.sha256(original).hexdigest(),
                               'desired_shards': 4}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                changed = True
                capacity.replace_config(config_path, [original], enlarged)
                print('REDIS_CLUSTER_RESIZE_BEGIN from=3 to=4 product-entry=config+kubecli', flush=True)
                setup(4)
                verify_dataset(cluster, dataset, expiry, now=now)
                verify_functions(cluster, dataset, library, function_source, protection)
                after_slots = {key: cluster.owner(key) for key in dataset}
                if len(set(after_slots.values())) != 4 or not any(after_slots[key] != before_slots[key] for key in dataset):
                    raise ValueError('Cluster scale-out did not really migrate retained key slots')
                if cluster.get('rediscluster', clients.NAME)['metadata']['uid'] != cluster.journal['cr_uid']:
                    raise ValueError('Cluster resize replaced its declaration')
                print('REDIS_CLUSTER_SCALE_OUT_PASS shards=4 members=12 retained-slots=migrated data-and-expiry=verified', flush=True)
                capacity.replace_config(config_path, [enlarged], original)
                print('REDIS_CLUSTER_RESIZE_BEGIN from=4 to=3 product-entry=config+kubecli', flush=True)
                setup(3)
                verify_dataset(cluster, dataset, expiry, now=now)
                verify_functions(cluster, dataset, library, function_source, protection)
                current = focused.resource_identities(cluster.get('pvc')['items'])
                if any(identity not in current for identity in before_claims):
                    raise ValueError('Cluster resize replaced an existing PVC')
                if journal_path.read_bytes() != journal_bytes:
                    raise ValueError('Cluster resize refuses a concurrent fixture journal change')
                updated = dict(cluster.journal, claims=[list(item) for item in current if
                               item[0].startswith(clients.NAME) or item[0].startswith('node-conf-' + clients.NAME)])
                journal_path.write_text(json.dumps(updated, indent=2) + '\n')
                fault_path.unlink()
                changed = False
                print('REDIS_CLUSTER_FUNCTIONS_RESIZE_PASS product-scale=3-4-3 library-and-business-results=retained write-barrier=retained', flush=True)
                print('REDIS_CLUSTER_REBALANCE_PASS product-scale=3-4-3 slots=16384 data-digest=' +
                      hashlib.sha256(b''.join(dataset.values())).hexdigest() +
                      ' absolute-expiry=retained existing-pvc-uids=retained', flush=True)
        except BaseException as exc:
            failure = exc
            cluster.fault_diagnostics()
            raise
        finally:
            if changed:
                try:
                    capacity.replace_config(config_path, [original, enlarged], original)
                    setup(3)
                    verify_dataset(cluster, dataset, expiry, now=now)
                    fault_path.unlink()
                    print('REDIS_CLUSTER_RESIZE_ROLLBACK_PASS shards=3 data=verified', flush=True)
                except BaseException:
                    print('REDIS_CLUSTER_RESIZE_ROLLBACK_FAIL first failure retained journal=retained', flush=True)
            try:
                if functions_owned:
                    cleanup_functions(cluster, library, function_source)
                cluster.cleanup()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_CLUSTER_REBALANCE_CLEANUP_FAIL first failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module + ' retained-pvcs=owned-for-final-cleanup', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
