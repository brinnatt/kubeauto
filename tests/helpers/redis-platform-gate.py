#!/usr/bin/env python3
"""Owned Redis platform prerequisites: DNS, failure domains, clocks and CSI I/O."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import time


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def node_contract(nodes):
    expected = {f'redis-node-{i}': f'192.168.47.{i}' for i in range(131, 137)}
    actual = {}
    domains = set()
    for node in nodes:
        name = node['metadata']['name']
        addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if name not in expected or addresses != [expected[name]] or name in actual:
            raise ValueError('platform prerequisite refuses foreign or duplicate node')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise ValueError('platform prerequisite contains an unhealthy node')
        if any(c['type'] in ('MemoryPressure', 'DiskPressure', 'PIDPressure') and c['status'] != 'False'
               for c in node['status']['conditions']):
            raise ValueError('platform prerequisite contains node pressure')
        if name in {f'redis-node-{i}' for i in range(131, 134)}:
            if node['spec'].get('unschedulable') or any(t['effect'] == 'NoSchedule'
                                                     for t in node['spec'].get('taints', [])):
                raise ValueError('platform lacks three schedulable workers')
            domain = node['metadata'].get('labels', {}).get('kubernetes.io/hostname')
            if not domain or domain in domains:
                raise ValueError('platform failure domains are not independent')
            domains.add(domain)
        actual[name] = addresses[0]
    if actual != expected or len(domains) != 3:
        raise ValueError('platform differs from the six leased nodes')
    return actual


def fixture_objects(namespace, name, run_id):
    if namespace != 'redis-gate' or not name.startswith('redis-platform-'):
        raise ValueError('CSI probe must stay in its owned Redis fixture boundary')
    metadata = {'name': name, 'namespace': namespace, 'labels': {
        'kubeauto.io/component': 'redis-test', 'kubeauto.io/managed-by': 'kubeauto',
        'kubeauto.io/test-run': run_id}}
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': metadata,
           'spec': {'storageClassName': 'redis-longhorn', 'accessModes': ['ReadWriteOnce'],
                    'resources': {'requests': {'storage': '32Mi'}}}}
    pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': metadata,
           'spec': {'restartPolicy': 'Never', 'nodeSelector': {'kubeauto.io/redis-storage': 'true'},
                    'containers': [{'name': 'probe', 'image': 'registry.talkschool.cn:5000/brinnatt/redis:8.10.2',
                                    'command': ['sh', '-c', 'trap : TERM INT; sleep 3600 & wait'],
                                    'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}},
                                    'volumeMounts': [{'name': 'data', 'mountPath': '/probe'}]}],
                    'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': name}}]}}
    return pvc, pod


def execute(source):
    nodes = json.loads(source.k('get', 'nodes', '-o', 'json'))['items']
    addresses = node_contract(nodes)
    for address in addresses.values():
        before = time.time()
        clock = source.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                            'root@' + address, 'date +%s'], timeout=15)
        if not clock.isdigit() or not before - 3 <= int(clock) <= time.time() + 3:
            raise ValueError('Redis host wall clock is outside the three-second boundary')
    print('REDIS_PLATFORM_NODE_PASS nodes=6 schedulable-domains=3 pressure=none clock-skew-bounded=3s', flush=True)
    sc = json.loads(source.k('get', 'storageclass', 'redis-longhorn', '-o', 'json'))
    if (sc['provisioner'] != 'driver.longhorn.io' or sc.get('allowVolumeExpansion') is not True or
            sc.get('volumeBindingMode') != 'WaitForFirstConsumer'):
        raise ValueError('Redis CSI prerequisite changed')
    name = 'redis-platform-' + source.run_id[:16]
    objects = fixture_objects(source.ns, name, source.run_id)
    created = []
    pv_name = None
    for kind in ('pod', 'pvc'):
        if source.k('-n', source.ns, 'get', kind, name, '--ignore-not-found', '-o', 'name'):
            raise ValueError('platform probe name already exists')
    try:
        for obj in objects:
            source.k('create', '-f', '-', input_text=json.dumps(obj))
            created.append(obj['kind'].lower())
        source.k('-n', source.ns, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s', timeout=200)
        source.k('-n', source.ns, 'exec', name, '--', 'sh', '-ec',
                 'getent hosts kubernetes.default.svc.cluster.local >/dev/null; '
                 'getent hosts redis-gate-data-master.redis-gate.svc.cluster.local >/dev/null; '
                 'printf redis-csi-business-verified > /probe/verified; sync; '
                 'test "$(cat /probe/verified)" = redis-csi-business-verified')
        claim = source.get('pvc', name)
        if claim['status'].get('phase') != 'Bound' or not claim['spec'].get('volumeName'):
            raise ValueError('CSI probe lacks a bound PV')
        pv_name = claim['spec']['volumeName']
        source.k('-n', source.ns, 'patch', 'pvc', name, '--type=merge',
                 '-p', '{"spec":{"resources":{"requests":{"storage":"64Mi"}}}}')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if source.get('pvc', name)['status'].get('capacity', {}).get('storage') == '64Mi':
                break
            print('REDIS_PLATFORM_HEARTBEAT stage=csi-expand waiting=true', flush=True)
            time.sleep(3)
        else:
            raise ValueError('CSI expansion did not converge to 64Mi')
        source.k('-n', source.ns, 'exec', name, '--', 'sh', '-ec',
                 'test "$(cat /probe/verified)" = redis-csi-business-verified; '
                 'printf retained-after-expand >> /probe/verified; sync; '
                 'test "$(cat /probe/verified)" = redis-csi-business-verifiedretained-after-expand')
        print('REDIS_PLATFORM_PASS namespace=owned dns=resolved csi=read-write-expand data=retained', flush=True)
    finally:
        for kind in reversed(created):
            obj = source.get(kind, name)
            if kind == 'persistentvolumeclaim':
                pv_name = obj['spec'].get('volumeName') or pv_name
            if obj['metadata'].get('labels', {}).get('kubeauto.io/test-run') != source.run_id:
                raise ValueError('platform cleanup refuses changed fixture ownership')
            source.k('-n', source.ns, 'delete', kind, name, '--wait=true', '--timeout=180s', timeout=200)
        remaining = source.k('-n', source.ns, 'get', 'pod,pvc', '-l',
                             'kubeauto.io/test-run=' + source.run_id, '-o', 'name')
        if remaining:
            raise ValueError('platform probe fixture remains')
        if pv_name:
            source.k('wait', '--for=delete', 'pv/' + pv_name, '--timeout=180s', timeout=200)
        print('REDIS_PLATFORM_CLEAN_PASS pod-and-pvc-and-pv=absent', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('platform',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_platform_focused')
    with tempfile.TemporaryDirectory(prefix='redis-platform-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        execute(source)
        source.baseline()
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=platform', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=platform', flush=True)


if __name__ == '__main__':
    main()
