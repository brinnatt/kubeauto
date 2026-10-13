#!/usr/bin/env python3
"""Thirty-minute business soak and bounded owned Pod/Operator/worker restarts."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker_identity(node):
    metadata = node['metadata']
    name = metadata['name']
    addresses = [row['address'] for row in node['status']['addresses'] if row['type'] == 'InternalIP']
    if (not re.fullmatch(r'redis-node-13[123]', name) or
            addresses != ['192.168.47.' + name.rsplit('-', 1)[1]] or
            node['spec'].get('unschedulable') or
            not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions'])):
        raise ValueError('stability restart refuses a foreign, reserved or unhealthy worker')
    return addresses[0], metadata['uid']


def replacement_ready(before, after):
    return (after['metadata']['name'] == before['metadata']['name'] and
            after['metadata']['uid'] != before['metadata']['uid'] and
            not after['metadata'].get('deletionTimestamp') and
            any(c['type'] == 'Ready' and c['status'] == 'True'
                for c in after.get('status', {}).get('conditions', [])))


def reboot_proven(before, result):
    return (result.returncode == 0 and
            re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', result.stdout.strip()) is not None and
            result.stdout.strip() != before)


def operator_selector(deployment):
    metadata, spec = deployment['metadata'], deployment['spec']
    if (metadata['name'] != 'redis-operator' or spec.get('replicas') != 2 or
            metadata.get('annotations', {}).get('meta.helm.sh/release-name') != 'redis-operator'):
        raise ValueError('stability refuses a foreign or incorrectly sized Operator deployment')
    selector = spec['selector']
    labels = selector.get('matchLabels', {})
    if (not labels or selector.get('matchExpressions') or
            any(not isinstance(k, str) or not isinstance(v, str) or
                not re.fullmatch(r'[A-Za-z0-9_./-]+', k) or
                not re.fullmatch(r'[A-Za-z0-9_.-]+', v) for k, v in labels.items()) or
            any(spec['template']['metadata']['labels'].get(k) != v for k, v in labels.items())):
        raise ValueError('stability Operator selector does not identify its Pod template')
    return ','.join(k + '=' + labels[k] for k in sorted(labels))


def write_journal(path, data, *, create=False):
    if create:
        with path.open('x') as stream:
            path.chmod(0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return
    prior = json.loads(path.read_text())
    if any(prior.get(key) != data.get(key) for key in ('run_id', 'cluster_uid', 'cr_uid')):
        raise ValueError('restart journal ownership changed before update')
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as stream:
        try:
            temporary.chmod(0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class Stability:
    def __init__(self, source, cluster, focused):
        self.source, self.cluster, self.focused = source, cluster, focused
        self.key = 'kubeauto:redis:focused:' + source.run_id
        self.expiry = int(time.time() * 1000) + 7200000
        self.dataset = {'kubeauto:redis:cluster:' + source.run_id + '{soak-' + str(i) + '}':
                        ('soak-' + str(i) + '-' + source.run_id).encode() for i in range(16)}
        cluster.keys.update(self.dataset)
        self.journal = source.cluster / 'redis-stability-fault.json'
        if self.journal.exists() or self.journal.with_suffix('.tmp').exists():
            raise ValueError('stability gate refuses an unrecovered prior restart journal')
        self.state = {'run_id': source.run_id, 'cluster_uid': cluster.journal['cluster_uid'],
                      'cr_uid': cluster.journal['cr_uid'], 'stage': 'seed'}
        self.claims = focused.resource_identities(source.get('pvc')['items'])
        # Prove the exact official Deployment selector before seeding data or
        # injecting any fault. Helm release labels need not exist on its Pods.
        namespace = source.config['redis_operator_namespace']
        deployment = json.loads(source.k('-n', namespace, 'get', 'deployment', 'redis-operator', '-o', 'json'))
        if deployment['metadata']['namespace'] != namespace:
            raise ValueError('stability Operator namespace changed')
        self.operator_uid = deployment['metadata']['uid']
        self.operator_labels = operator_selector(deployment)
        pods = self.operator_pods()
        if len(pods) != 2 or any(p['metadata'].get('deletionTimestamp') for p in pods):
            raise ValueError('stability Operator baseline is not two active replicas')

    def operator_pods(self):
        namespace = self.source.config['redis_operator_namespace']
        return json.loads(self.source.k('-n', namespace, 'get', 'pods', '-l',
                                        self.operator_labels, '-o', 'json'))['items']

    def sample(self):
        client = self.source.client(self.source.primary())
        try:
            if client.command('SET', self.key, self.source.value, 'PXAT', self.expiry) != b'OK':
                raise ValueError('stability business write rejected')
            if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                raise ValueError('stability business write lacks three durable copies')
            if client.command('GET', self.key) != self.source.value:
                raise ValueError('stability Sentinel business digest changed')
            for key, value in self.dataset.items():
                if self.cluster.command(key, 'SET', key, value, 'PXAT', self.expiry) != b'OK':
                    raise ValueError('stability Cluster business write rejected')
                if self.cluster.command(key, 'GET', key) != value:
                    raise ValueError('stability Cluster business digest changed')
                ttl = self.cluster.command(key, 'PTTL', key)
                if ttl <= 0 or abs(ttl - (self.expiry - int(time.time() * 1000))) > 5000:
                    raise ValueError('stability absolute expiry changed')
            routes = {key: self.cluster.owner(key) for key in self.dataset}
            for pod in set(routes.values()):
                key = next(key for key, owner in routes.items() if owner == pod)
                member = self.cluster.client(pod)
                try:
                    if member.command('SET', key, self.dataset[key], 'PXAT', self.expiry) != b'OK':
                        raise ValueError('stability durable Cluster write rejected')
                    if member.command('WAITAOF', 1, 2, 5000) != [1, 2]:
                        raise ValueError('stability Cluster write lacks its declared durable copies')
                finally:
                    member.close()
        finally:
            client.close()

    def verify_retained(self):
        self.source.forwards.clear()
        self.cluster.health()
        client = self.source.client(self.source.primary(), fresh=True)
        try:
            if client.command('GET', self.key) != self.source.value:
                raise ValueError('restart lost acknowledged Sentinel data')
        finally:
            client.close()
        for key, value in self.dataset.items():
            if self.cluster.command(key, 'GET', key) != value:
                raise ValueError('restart lost acknowledged Cluster data')
        if self.claims != self.focused.resource_identities(self.source.get('pvc')['items']):
            raise ValueError('restart replaced a customer PVC')
        self.sample()

    def pod_restart(self):
        primary = self.source.primary()
        before = next(p for p in self.source.members()['replication'] if p['metadata']['name'] != primary)
        metadata = before['metadata']
        if metadata.get('labels', {}).get('app') != self.source.name:
            raise ValueError('stability restart refuses a foreign data Pod')
        self.state.update(stage='pod', pod=metadata['name'], pod_uid=metadata['uid'])
        write_journal(self.journal, self.state)
        self.source.k('delete', '--raw', '/api/v1/namespaces/' + self.source.ns + '/pods/' + metadata['name'],
                      '-f', '-', input_text=json.dumps({'apiVersion': 'v1', 'kind': 'DeleteOptions',
                      'preconditions': {'uid': metadata['uid'], 'resourceVersion': metadata['resourceVersion']}}))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pods = self.source.members()['replication']
            current = next((p for p in pods if p['metadata']['name'] == metadata['name']), None)
            if current and replacement_ready(before, current):
                self.verify_retained()
                print('REDIS_STABILITY_POD_RESTART_PASS uid=replaced data-and-pvc=retained', flush=True)
                return
            print('REDIS_STABILITY_HEARTBEAT stage=pod-restart waiting=true', flush=True)
            time.sleep(5)
        raise ValueError('stability data Pod replacement did not converge')

    def operator_restart(self):
        namespace = self.source.config['redis_operator_namespace']
        deployment = json.loads(self.source.k('-n', namespace, 'get', 'deployment', 'redis-operator', '-o', 'json'))
        if (deployment['metadata']['uid'] != self.operator_uid or
                operator_selector(deployment) != self.operator_labels):
            raise ValueError('stability Operator identity or selector changed before restart')
        before = {p['metadata']['uid'] for p in self.operator_pods()}
        if len(before) != 2:
            raise ValueError('stability Operator baseline is not two replicas')
        self.state.update(stage='operator', operator_uids=sorted(before))
        write_journal(self.journal, self.state)
        self.source.k('-n', namespace, 'rollout', 'restart', 'deployment/redis-operator')
        self.source.k('-n', namespace, 'rollout', 'status', 'deployment/redis-operator',
                      '--timeout=300s', timeout=320, capture=False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            after = self.operator_pods()
            if (len(after) == 2 and not before & {p['metadata']['uid'] for p in after} and
                    all(not p['metadata'].get('deletionTimestamp') and
                        any(c['type'] == 'Ready' and c['status'] == 'True'
                            for c in p.get('status', {}).get('conditions', [])) for p in after)):
                self.verify_retained()
                print('REDIS_STABILITY_OPERATOR_RESTART_PASS processes=2 data-and-pvc=retained', flush=True)
                return
            print('REDIS_STABILITY_HEARTBEAT stage=operator-restart waiting=true', flush=True)
            time.sleep(5)
        raise ValueError('stability did not restart both Operator processes')

    def worker_restart(self):
        node = json.loads(self.source.k('get', 'node', 'redis-node-131', '-o', 'json'))
        address, uid = worker_identity(node)
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'root@' + address]
        boot_id = self.source.run([*ssh, 'cat /proc/sys/kernel/random/boot_id'])
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot_id):
            raise ValueError('stability worker boot identity malformed')
        self.state.update(stage='worker', host=address, node_uid=uid, boot_id=boot_id)
        write_journal(self.journal, self.state)
        # Host reboot is a bounded runner-owned fault. The controller remains on
        # .130; no inventory, storage settings or customer config are edited.
        self.source.run([*ssh, "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 </dev/null &"], timeout=20)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                result = subprocess.run([*ssh, 'cat /proc/sys/kernel/random/boot_id'],
                                        capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                print('REDIS_STABILITY_HEARTBEAT stage=node-restart probe=temporarily-unreachable', flush=True)
                continue
            if reboot_proven(boot_id, result):
                current = json.loads(self.source.k('get', 'node', node['metadata']['name'], '-o', 'json'))
                if current['metadata']['uid'] != uid:
                    raise ValueError('stability worker was replaced instead of rebooted')
                if any(c['type'] == 'Ready' and c['status'] == 'True'
                       for c in current['status']['conditions']):
                    self.verify_retained()
                    print('REDIS_STABILITY_NODE_RESTART_PASS boot-id=changed node-uid=retained data-and-pvc=retained', flush=True)
                    return
            print('REDIS_STABILITY_HEARTBEAT stage=node-restart waiting=true', flush=True)
            time.sleep(5)
        raise ValueError('stability worker reboot did not recover within its budget')

    def execute(self, seconds):
        write_journal(self.journal, self.state, create=True)
        self.sample()
        self.pod_restart()
        self.operator_restart()
        self.worker_restart()
        if seconds:
            start, samples = time.monotonic(), 0
            while time.monotonic() - start < seconds:
                self.sample()
                samples += 1
                print(f'REDIS_STABILITY_HEARTBEAT stage=soak elapsed={int(time.monotonic()-start)} '
                      f'required={seconds} samples={samples} business-errors=0', flush=True)
                time.sleep(5)
            self.verify_retained()
            print(f'REDIS_STABILITY_PASS seconds={int(time.monotonic()-start)} samples={samples} '
                  'business-errors=0 restarts=pod+operator+node data-and-pvc=retained', flush=True)
        else:
            print('REDIS_STABILITY_FAULTS_PASS acceptance=partial-no-soak', flush=True)

    def cleanup(self):
        self.verify_retained()
        self.cluster.cleanup()
        self.source.cleanup_keys()
        state = json.loads(self.journal.read_text())
        if state['run_id'] != self.source.run_id or state['cluster_uid'] != self.state['cluster_uid']:
            raise ValueError('stability cleanup refuses a changed restart journal')
        self.journal.unlink()
        print('REDIS_STABILITY_RESTART_CLEAN_PASS faults=none data-keys=absent', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('stability-faults', 'stability'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    parser.add_argument('--seconds', type=int, default=1800)
    args = parser.parse_args()
    if args.module == 'stability' and args.seconds < 1800:
        parser.error('complete stability acceptance requires at least 1800 seconds')
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_stability_focused')
    clients = load(args.base, 'redis-cluster-client-gate.py', 'redis_stability_cluster')
    with tempfile.TemporaryDirectory(prefix='redis-stability-') as tmp, ExitStack() as stack:
        source = focused.Gate(args, Path(tmp), stack)
        source.baseline()
        cluster = clients.Cluster(source, focused, Path(tmp))
        cluster.health()
        gate = Stability(source, cluster, focused)
        failure = None
        try:
            gate.execute(args.seconds if args.module == 'stability' else 0)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                gate.cleanup()
            except Exception:
                if failure is None:
                    raise
                print('REDIS_STABILITY_CLEANUP_FAIL original failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
