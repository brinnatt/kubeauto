#!/usr/bin/env python3
"""Owned replica restart, primary isolation, majority refusal and exact recovery."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
import time


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_until(check, *, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        print('REDIS_CLUSTER_FAILURE_HEARTBEAT stage=' + label, flush=True)
        time.sleep(3)
    raise ValueError('Cluster failure convergence timeout: ' + label)


def disconnect(cluster, pod):
    client = cluster.client(pod, fresh=True)
    for kind in ('normal', 'pubsub', 'replica', 'master'):
        client.command('CLIENT', 'KILL', 'TYPE', kind, 'SKIPME', 'yes')


def cluster_state(reply):
    fields = dict(line.split(':', 1) for line in reply.decode().splitlines() if ':' in line)
    state = fields.get('cluster_state')
    if state not in ('ok', 'fail'):
        raise ValueError('Cluster majority probe lacks a real state')
    return state


def seed(cluster):
    keys = ['kubeauto:redis:cluster:' + cluster.run_id + '{failure-' + str(i) + '}' for i in range(48)]
    selected = {}
    for key in keys:
        selected.setdefault(cluster.owner(key), key)
        if len(selected) == 3:
            break
    if len(selected) != 3:
        raise ValueError('Cluster fault dataset did not reach all three shards')
    journal = cluster.cluster / 'redis-cluster-failure-keys.json'
    with journal.open('x') as stream:
        journal.chmod(0o600)
        json.dump({'run_id': cluster.run_id, 'cr_uid': cluster.journal['cr_uid'],
                   'keys': list(selected.values())}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    cluster.failure_keys_owned = True
    for pod, key in selected.items():
        cluster.keys.add(key)
        client = cluster.client(pod, fresh=True)
        if client.command('SET', key, 'retained-' + cluster.run_id, 'EX', 7200) != b'OK':
            raise ValueError('Cluster fault seed rejected')
        if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
            raise ValueError('Cluster fault seed is not durable on both replicas')
    return selected


def readback(cluster):
    for key in cluster.keys:
        if cluster.command(key, 'GET', key) != ('retained-' + cluster.run_id).encode():
            raise ValueError('Cluster failure lost acknowledged data')
        if cluster.command(key, 'TTL', key) <= 0:
            raise ValueError('Cluster failure lost expiry boundary')


def failure_gate(cluster, only_primary=False):
    claims = cluster.focused.resource_identities(cluster.get('pvc')['items'])
    keys = seed(cluster)
    first_primary, key = next(iter(keys.items()))
    slots = cluster.client(first_primary, fresh=True).command('CLUSTER', 'SLOTS')
    row = next(row for row in slots if row[0] <= cluster.focused_slot(key) <= row[1])
    clients = load(cluster.base, 'redis-cluster-client-gate.py', 'redis_cluster_failure_endpoints')
    by_ip = clients.member_addresses(cluster.members(), cluster.ns)
    replica = by_ip[row[3][0].decode()]
    obj = cluster.get('pod', replica)
    labels = obj['metadata'].get('labels', {})
    if labels.get('kubeauto.io/component') != 'redis' or labels.get('kubeauto.io/managed-by') != 'kubeauto':
        raise ValueError('Cluster replica restart refuses foreign ownership')
    uid = obj['metadata']['uid']
    if not only_primary:
        cluster.k('delete', '--raw', f'/api/v1/namespaces/{cluster.ns}/pods/{replica}', '-f', '-',
                  input_text=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
                  'preconditions': {'uid': uid, 'resourceVersion': obj['metadata']['resourceVersion']}}))
    def restarted():
        raw = cluster.k('-n', cluster.ns, 'get', 'pod', replica, '--ignore-not-found', '-o', 'json')
        if not raw:
            return False
        current = json.loads(raw)
        return (current['metadata']['uid'] != uid and
                any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in current.get('status', {}).get('conditions', [])))
    if not only_primary:
        wait_until(restarted, timeout=180, label='replica-restart')
        cluster.health()
        readback(cluster)
        print('REDIS_CLUSTER_REPLICA_RESTART_PASS pod-uid=replaced pvc-uids=retained data=retained', flush=True)
    started = time.monotonic()
    with cluster.isolated([first_primary]):
        time.sleep(3)
        disconnect(cluster, first_primary)
        def promoted():
            return cluster.owner(key) != first_primary
        with cluster.paused(first_primary):
            wait_until(promoted, timeout=150, label='primary-promotion')
        replacement = cluster.owner(key)
        client = cluster.client(replacement, fresh=True)
        if client.command('GET', key) != ('retained-' + cluster.run_id).encode():
            raise ValueError('Cluster promoted replica lost durable value')
        if client.command('SET', key, 'retained-' + cluster.run_id, 'KEEPTTL') != b'OK':
            raise ValueError('Cluster new primary cannot continue writes')
        old = cluster.client(first_primary, fresh=True)
        try:
            old.command('SET', key, 'unsafe', 'KEEPTTL')
        except cluster.focused.RedisReplyError as exc:
            if exc.code not in ('CLUSTERDOWN', 'NOREPLICAS', 'MOVED', 'READONLY'):
                raise
        else:
            raise ValueError('Isolated old Cluster primary accepted unsafe write')
        print('REDIS_CLUSTER_PRIMARY_FAILOVER_PASS elapsed-seconds=' + str(round(time.monotonic() - started, 3)), flush=True)
    cluster.health()
    readback(cluster)
    if only_primary:
        return
    primaries = sorted({row[2] for row in cluster.topology()})
    isolated, survivor = primaries[:2], primaries[2]
    survivor_key = next(key for key in cluster.keys if cluster.owner(key) == survivor)
    observed = 0
    with cluster.isolated(isolated):
        time.sleep(3)
        for pod in isolated:
            disconnect(cluster, pod)
        with ExitStack() as pauses:
            for pod in isolated:
                pauses.enter_context(cluster.paused(pod))
            start = time.monotonic()
            while time.monotonic() - start < 75:
                client = cluster.client(survivor, fresh=True)
                if cluster_state(client.command('CLUSTER', 'INFO')) == 'fail':
                    try:
                        client.command('SET', survivor_key, 'unsafe', 'KEEPTTL')
                    except cluster.focused.RedisReplyError as exc:
                        if exc.code != 'CLUSTERDOWN':
                            raise
                        observed += 1
                    else:
                        raise ValueError('Cluster without primary majority accepted a write')
                print('REDIS_CLUSTER_MAJORITY_HEARTBEAT elapsed=' + str(round(time.monotonic() - start)) +
                      ' rejected=' + str(observed), flush=True)
                time.sleep(5)
        if observed < 6:
            raise ValueError('Cluster primary-majority refusal lacks a stable observation window')
    cluster.health()
    readback(cluster)
    if cluster.focused.resource_identities(cluster.get('pvc')['items']) != claims:
        raise ValueError('Cluster fault replaced a persistent volume identity')
    print('REDIS_CLUSTER_FAILURE_PASS replica-restart=verified primary-failover=verified '
          'majority-loss=CLUSTERDOWN recovery=full data-and-ttl=retained pvc-uids=retained', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cluster-failure', 'cluster-primary-fault', 'cluster-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cluster_failure_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_cluster_failure_clients')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-failure-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        cluster = clients.Cluster(source, focused, Path(temporary))
        cluster.focused_slot = clients.key_slot
        cluster.health()
        failure = None
        try:
            if args.module == 'cluster-repair':
                cluster.recover_failure_keys()
            else:
                if (cluster.cluster / 'redis-cluster-failure-keys.json').exists():
                    raise ValueError('Cluster fault refuses unrecovered failure keys')
                failure_gate(cluster, only_primary=args.module == 'cluster-primary-fault')
        except BaseException as exc:
            failure = exc
            try:
                cluster.fault_diagnostics()
            except Exception:
                print('REDIS_CLUSTER_FAILURE_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            try:
                cluster.cleanup()
            except BaseException as exc:
                if failure is None:
                    raise
                print('REDIS_CLUSTER_FAILURE_CLEANUP_FAIL first failure retained type=' + type(exc).__name__ +
                      ' code=' + str(getattr(exc, 'code', 'none')), flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
