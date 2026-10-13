#!/usr/bin/env python3
"""Fixed TLS business workload with latency and actual kubelet resource samples."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import signal
import tempfile
import time


def percentile(samples, fraction):
    if not samples or not 0 < fraction <= 1:
        raise ValueError('invalid latency percentile input')
    return sorted(samples)[math.ceil(len(samples) * fraction) - 1]


def resource_sample(summary, namespace, pods):
    result = {}
    for item in summary.get('pods', []):
        identity = item['podRef']
        if identity['namespace'] != namespace or identity['name'] not in pods:
            continue
        if identity['uid'] != pods[identity['name']]:
            raise ValueError('performance summary contains a stale Pod identity')
        cpu = sum(c['cpu']['usageCoreNanoSeconds'] for c in item['containers'])
        memory = sum(c['memory']['workingSetBytes'] for c in item['containers'])
        disk = sum(v['usedBytes'] for v in item.get('volume', []) if 'pvcRef' in v)
        network = item['network']
        result[identity['name']] = {'uid': identity['uid'], 'cpu_ns': cpu, 'memory_bytes': memory,
                                  'pvc_used_bytes': disk, 'rx_bytes': network['rxBytes'],
                                  'tx_bytes': network['txBytes']}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('performance',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    spec = importlib.util.spec_from_file_location('redis_perf_focused', args.base / 'tests/helpers/redis-focused-gate.py')
    focused = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(focused)
    with tempfile.TemporaryDirectory(prefix='redis-performance-') as temporary, ExitStack() as stack:
        gate = focused.Gate(args, Path(temporary), stack)
        gate.baseline()
        pods = gate.members()['replication']
        names = {p['metadata']['name']: p['metadata']['uid'] for p in pods}
        nodes = {p['spec']['nodeName'] for p in pods}
        claims = focused.resource_identities(gate.get('pvc')['items'])
        keys = [gate.key + ':' + str(i) for i in range(100)]
        payload = (gate.value + b'x' * 1024)[:1024]
        failure = None
        def sample():
            result = {}
            for node in sorted(nodes):
                summary = json.loads(gate.k('get', '--raw', '/api/v1/nodes/' + node + '/proxy/stats/summary'))
                result.update(resource_sample(summary, gate.ns, names))
            if set(result) != set(names) or any(v['pvc_used_bytes'] <= 0 for v in result.values()):
                raise ValueError('performance resource sample lacks owned members or PVC statistics')
            return result
        try:
            primary = gate.primary()
            client = gate.client(primary)
            for key in keys:
                if client.command('SET', key, payload, 'EX', 7200) != b'OK':
                    raise ValueError('performance warmup write failed')
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('performance warmup is not durably replicated')
            before = sample()
            latencies = []
            started = time.monotonic()
            heartbeat = started
            operations = 0
            # One persistent TLS connection, 100 keys, 1024-byte values,
            # alternating SET/GET, no pipelining. Includes client transport cost.
            while time.monotonic() - started < 60:
                key = keys[(operations // 2) % len(keys)]
                begin = time.perf_counter()
                answer = (client.command('SET', key, payload, 'KEEPTTL') if operations % 2 == 0
                          else client.command('GET', key))
                latencies.append((time.perf_counter() - begin) * 1000)
                if answer != (b'OK' if operations % 2 == 0 else payload):
                    raise ValueError('performance business operation or digest failed')
                operations += 1
                if time.monotonic() - heartbeat >= 10:
                    print(f'REDIS_PERFORMANCE_HEARTBEAT operations={operations} errors=0', flush=True)
                    heartbeat = time.monotonic()
            elapsed = time.monotonic() - started
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('performance writes did not persist on all members')
            for pod in pods:
                member = gate.client(pod['metadata']['name'], fresh=True)
                for key in keys:
                    if member.command('GET', key) != payload or member.command('TTL', key) <= 0:
                        raise ValueError('performance final replicated digest or TTL mismatch')
            after = sample()
            profile = {}
            for name in names:
                if before[name]['uid'] != after[name]['uid']:
                    raise ValueError('performance sampling crossed a member restart')
                delta = {k: after[name][k] - before[name][k] for k in ('cpu_ns', 'rx_bytes', 'tx_bytes')}
                if any(v < 0 for v in delta.values()):
                    raise ValueError('performance resource counters reset')
                profile[name] = {**delta, 'memory_bytes': after[name]['memory_bytes'],
                                 'pvc_used_bytes': after[name]['pvc_used_bytes']}
            gate.health()
            if focused.resource_identities(gate.get('pvc')['items']) != claims:
                raise ValueError('performance replaced a data PVC')
            report = {'duration_seconds': elapsed, 'operations': operations, 'errors': 0,
                      'operations_per_second': operations / elapsed,
                      'latency_ms': {str(p): percentile(latencies, p / 100) for p in (50, 95, 99)},
                      'workload': {'connections': 1, 'keys': 100, 'value_bytes': 1024,
                                   'write_read_ratio': '1:1', 'pipelining': False, 'tls': True,
                                   'transport': 'managed kubectl port-forward; includes transport overhead'},
                      'payload_sha256': hashlib.sha256(payload).hexdigest(), 'resources': profile}
            print('REDIS_PERFORMANCE_RESULT ' + json.dumps(report, sort_keys=True), flush=True)
            print('REDIS_PERFORMANCE_PASS percentiles=50,95,99 errors=0 replica-digests=verified '
                  'resources=cpu,memory,pvc,network', flush=True)
        except BaseException as exc:
            failure = exc
            try:
                gate.fault_diagnostics()
            except Exception:
                print('REDIS_PERFORMANCE_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            try:
                client = gate.client(gate.primary(), fresh=True)
                client.command('DEL', *keys)
                if client.command('EXISTS', *keys) != 0:
                    raise ValueError('performance fixture keys remain')
            except Exception:
                if failure is None:
                    raise
                print('REDIS_PERFORMANCE_CLEANUP_FAIL first failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=performance', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=performance', flush=True)


if __name__ == '__main__':
    main()
