#!/usr/bin/env python3
"""Real Cluster client semantics against the separate product-installed fixture."""
import argparse
import base64
import binascii
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import signal
import shlex
import ssl
import tempfile

NAME = 'redis-gate-cluster-data'
FIXTURE = 'redis-cluster-gate'


def key_slot(key):
    value = key if isinstance(key, bytes) else key.encode()
    start = value.find(b'{')
    end = value.find(b'}', start + 1) if start >= 0 else -1
    if end > start + 1:
        value = value[start + 1:end]
    return binascii.crc_hqx(value, 0) % 16384


def member_addresses(groups, namespace):
    result = {}
    for group in groups.values():
        for pod in group:
            name = pod['metadata']['name']
            addresses = [pod['status']['podIP']]
            if pod.get('spec', {}).get('hostname') and pod['spec'].get('subdomain'):
                addresses.append(f"{pod['spec']['hostname']}.{pod['spec']['subdomain']}.{namespace}.svc.cluster.local")
            for address in addresses:
                if address in result and result[address] != name:
                    raise ValueError('Cluster address is shared by different member identities')
                result[address] = name
    return result


def load(base, filename, name):
    spec = importlib.util.spec_from_file_location(name, base / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Cluster:
    def __init__(self, source, focused, work):
        self.source, self.focused, self.work = source, focused, work
        self.config = source.config
        self.base, self.ns, self.cluster = source.base, source.ns, source.cluster
        self.run_id = source.run_id
        self.keys, self.redirects = set(), {'MOVED': 0, 'ASK': 0}
        self.excluded = set()
        self.target = self.base / 'clusters' / FIXTURE
        if (any((self.cluster / name).exists() for name in
                ('redis-cluster-ask-fault.json', 'redis-cluster-scale-fault.json')) or
                any(self.cluster.glob('redis-cluster-process-fault-*.json'))):
            raise ValueError('Cluster client refuses a stale owned mutation journal')
        journal = json.loads((self.cluster / 'redis-cluster-fixture.json').read_text())
        owner = json.loads((self.cluster / 'redis-lab-owner.json').read_text())
        if (not journal.get('healthy') or journal['cluster_uid'] != owner['cluster_uid'] or
                journal['name'] != NAME or journal['namespace'] != self.ns or
                hashlib.sha256((self.target / 'config.yml').read_bytes()).hexdigest() != journal['config_sha256']):
            raise ValueError('Cluster client refuses an unverified product fixture')
        self.journal = journal
        cr = source.get('rediscluster', NAME)
        if cr['metadata']['uid'] != journal['cr_uid']:
            raise ValueError('Cluster declaration identity changed')
        tls = source.get('secret', journal['tls_secret'])
        if tls['metadata']['uid'] != journal['tls_uid']:
            raise ValueError('Cluster TLS identity changed')
        directory = work / 'cluster-tls'
        directory.mkdir(mode=0o700)
        for key in ('ca.crt', 'tls.crt', 'tls.key'):
            (directory / key).write_bytes(base64.b64decode(tls['data'][key], validate=True))
            (directory / key).chmod(0o600)
        self.context = ssl.create_default_context(cafile=str(directory / 'ca.crt'))
        self.context.load_cert_chain(str(directory / 'tls.crt'), str(directory / 'tls.key'))

    def k(self, *argv, **kwargs):
        return self.source.k(*argv, **kwargs)

    def get(self, kind, name=None):
        return self.source.get(kind, name)

    def members(self):
        return {role: [p for p in self.get('pods')['items']
                       if p['metadata'].get('labels', {}).get('app') == NAME + '-' + role]
                for role in ('leader', 'follower')}

    def client(self, pod, *, fresh=False):
        obj = self.get('pod', pod)
        if obj['metadata'].get('labels', {}).get('app') not in (NAME + '-leader', NAME + '-follower'):
            raise ValueError('Cluster client refuses a foreign member')
        hostname = f"{obj['spec']['hostname']}.{obj['spec']['subdomain']}.{self.ns}.svc.cluster.local"
        return self.source.client(pod, context=self.context, server_name=hostname, fresh=fresh)

    def health(self, shards=3):
        self.source.run([str(self.base / '.venv/bin/python'),
                         str(self.base / 'roles/cluster-addon/files/redis-health.py'),
                         '--kubeconfig', str(self.source.kubeconfig), '--namespace', self.ns,
                         '--name', NAME, '--mode', 'cluster', '--shards', str(shards),
                         '--replicas-per-shard', '2', '--tls', '--wait', '300'],
                        timeout=350, capture=False)
        self.source.health()

    @contextmanager
    def isolated(self, pods):
        previous = self.excluded.copy()
        self.excluded.update(pods)
        try:
            with self.focused.Gate.isolated(self, pods, dns_addresses=self.source.resolvers(pods)):
                yield
        finally:
            self.excluded = previous

    def host_process(self, host, request):
        if host not in {f'192.168.47.{i}' for i in range(131, 137)}:
            raise ValueError('Cluster process fault refuses a non-allowlisted host')
        code = (self.base / 'tests/helpers/redis-cluster-process.py').read_text()
        response = self.source.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            'root@' + host, 'python3 -c ' + shlex.quote(code)],
            input_text=json.dumps(request), timeout=35)
        return json.loads(response)

    @contextmanager
    def paused(self, pod):
        obj = self.get('pod', pod)
        labels = obj['metadata'].get('labels', {})
        if (labels.get('app') not in (NAME + '-leader', NAME + '-follower') or
                labels.get('kubeauto.io/component') != 'redis' or
                labels.get('kubeauto.io/managed-by') != 'kubeauto'):
            raise ValueError('Cluster pause refuses a foreign member')
        container = obj['spec']['containers'][0]['name']
        status = next(item for item in obj['status']['containerStatuses'] if item['name'] == container)
        identifier = status['containerID']
        if not re.fullmatch(r'containerd://[0-9a-f]{64}', identifier):
            raise ValueError('Cluster pause requires an exact containerd identity')
        node = json.loads(self.k('get', 'node', obj['spec']['nodeName'], '-o', 'json'))
        addresses = [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP']
        if len(addresses) != 1:
            raise ValueError('Cluster pause requires a unique node InternalIP')
        host = addresses[0]
        request = {'run_id': self.run_id, 'pod': pod, 'uid': obj['metadata']['uid'],
                   'namespace': self.ns, 'container': container, 'container_id': identifier.split('://')[1]}
        identity = self.host_process(host, {**request, 'action': 'inspect'})
        request.update({'pid': identity['pid'], 'pid_start': identity['pid_start']})
        journal = self.cluster / ('redis-cluster-process-fault-' + pod + '.json')
        with journal.open('x') as stream:
            journal.chmod(0o600)
            json.dump({**request, 'host': host, 'cr_uid': self.journal['cr_uid']}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        failure = None
        try:
            stopped = self.host_process(host, {**request, 'action': 'stop'})
            if stopped.get('state') != 'T' or stopped.get('outcome') != 'paused':
                raise ValueError('Cluster fault did not stop the real host process')
            print('REDIS_CLUSTER_PROCESS_PAUSED pod=' + pod + ' host-state=T identity=verified', flush=True)
            yield
        except BaseException as exc:
            failure = exc
            try:
                self.fault_diagnostics()
            except Exception:
                print('REDIS_CLUSTER_PROCESS_DIAGNOSTICS_FAIL first failure retained', flush=True)
            raise
        finally:
            try:
                result = self.host_process(host, {**request, 'action': 'continue'})
                if result.get('outcome') not in ('resumed', 'original-process-gone', 'original-process-replaced'):
                    raise ValueError('Cluster process recovery lacks a proven outcome')
                journal.unlink()
                print('REDIS_CLUSTER_PROCESS_FAULT_CLEAN_PASS outcome=' + result['outcome'], flush=True)
            except BaseException as exc:
                if failure is None:
                    raise
                print('REDIS_CLUSTER_PROCESS_CLEANUP_FAIL journal=retained type=' + type(exc).__name__, flush=True)

    def fault_diagnostics(self):
        print('REDIS_CLUSTER_FIRST_FAULT_STATE_BEGIN before=cleanup', flush=True)
        for group in self.members().values():
            for pod in group:
                if pod['metadata']['name'] in self.excluded:
                    continue
                try:
                    print(pod['metadata']['name'], self.client(pod['metadata']['name'], fresh=True)
                          .command('CLUSTER', 'NODES').decode(), flush=True)
                except Exception as exc:
                    print(pod['metadata']['name'], type(exc).__name__, flush=True)
        print(self.k('-n', self.ns, 'get', 'events', '--sort-by=.lastTimestamp'), flush=True)

    def topology(self):
        seed = next(p['metadata']['name'] for group in self.members().values() for p in group
                    if p['metadata']['name'] not in self.excluded)
        client = self.client(seed)
        try:
            slots = client.command('CLUSTER', 'SLOTS')
        finally:
            client.close()
        pods = member_addresses(self.members(), self.ns)
        result = []
        for row in slots:
            address = row[2][0].decode()
            if address not in pods or row[2][1] != 6379:
                raise ValueError('Cluster slot endpoint is outside the product members')
            result.append((row[0], row[1], pods[address], row[2][2].decode()))
        if sum(high - low + 1 for low, high, _, _ in result) != 16384:
            raise ValueError('Cluster client lacks complete slot coverage')
        return result

    def owner(self, key):
        slot = key_slot(key)
        owners = [pod for low, high, pod, _ in self.topology() if low <= slot <= high]
        if len(owners) != 1:
            raise ValueError('Cluster key has no unique owner')
        return owners[0]

    def command(self, key, *parts, start=None):
        current = start or self.owner(key)
        for _ in range(5):
            client = self.client(current)
            try:
                return client.command(*parts)
            except self.focused.RedisReplyError as error:
                if error.code not in ('MOVED', 'ASK') or not error.redirect or error.redirect[0] != key_slot(key):
                    raise
                pods = member_addresses(self.members(), self.ns)
                _, address, port = error.redirect
                if address not in pods or port != 6379:
                    raise ValueError('Cluster redirect points outside owned endpoints')
                current = pods[address]
                self.redirects[error.code] += 1
                if error.code == 'ASK':
                    temporary = self.client(current)
                    try:
                        if temporary.command('ASKING') != b'OK':
                            raise ValueError('Cluster ASKING was rejected')
                        return temporary.command(*parts)
                    finally:
                        temporary.close()
            finally:
                client.close()
        raise ValueError('Cluster redirect budget exceeded')

    def cleanup(self):
        for key in sorted(self.keys):
            try:
                self.command(key, 'DEL', key)
                if self.command(key, 'EXISTS', key) != 0:
                    raise ValueError('Cluster fixture key remains')
            except Exception as exc:
                print('REDIS_CLUSTER_KEY_CLEANUP_ERROR type=' + type(exc).__name__ +
                      ' code=' + str(getattr(exc, 'code', 'none')), flush=True)
                raise
        self.health()
        if getattr(self, 'failure_keys_owned', False):
            (self.cluster / 'redis-cluster-failure-keys.json').unlink()
        print('REDIS_CLUSTER_KEYS_CLEAN_PASS uuid-keys=absent topology=healthy', flush=True)

    def recover_failure_keys(self, *, startup=False):
        """Recover only proven legacy failure fixtures, never arbitrary prefix data."""
        found = {}
        for pod in sorted({row[2] for row in self.topology()}):
            client = self.client(pod, fresh=True)
            try:
                cursor, rounds = b'0', 0
                while True:
                    cursor, keys = client.command('SCAN', cursor, 'MATCH',
                        'kubeauto:redis:cluster:*{' + ('startup' if startup else 'failure') + '-*}', 'COUNT', 100)
                    rounds += 1
                    for key in keys:
                        pattern = (rb'kubeauto:redis:cluster:([0-9a-f]{32})\{startup-([0-9]|[12][0-9]|3[01])\}' if startup else
                                   rb'kubeauto:redis:cluster:([0-9a-f]{32})\{failure-([0-9]|[1-3][0-9]|4[0-7])\}')
                        match = re.fullmatch(pattern, key)
                        if not match:
                            raise ValueError('Cluster repair refuses an unproven fixture key name')
                        expected = (b'verified-' if startup else b'retained-') + match[1]
                        if client.command('GET', key) != expected:
                            raise ValueError('Cluster repair refuses an unproven fixture key value')
                        found[key] = expected
                    if len(found) > 144 or rounds > 1000:
                        raise ValueError('Cluster repair fixture scope exceeds bound')
                    if cursor == b'0':
                        break
            finally:
                client.close()
        # Validate the entire boundary before deleting any key; Lua prevents a
        # value changed since that read from being deleted.
        for key, expected in found.items():
            if self.command(key, 'EVAL',
                    "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return -1 end",
                    1, key, expected) != 1:
                raise ValueError('Cluster repair fixture value changed before deletion')
            if self.command(key, 'EXISTS', key) != 0:
                raise ValueError('Cluster repaired fixture key remains')
        self.health()
        if not startup:
            (self.cluster / 'redis-cluster-failure-keys.json').unlink(missing_ok=True)
        marker = 'REDIS_CLUSTER_STARTUP_KEYS_RECOVERED_PASS' if startup else 'REDIS_CLUSTER_FAILURE_KEYS_RECOVERED_PASS'
        print(marker + ' validated-and-removed=' + str(len(found)), flush=True)


def data_gate(cluster):
    prefix = 'kubeauto:redis:cluster:' + cluster.run_id
    key = prefix + '{transaction}:a'
    other = prefix + '{transaction}:b'
    cluster.keys.update((key, other))
    owner = cluster.owner(key)
    wrong = next(pod for _, _, pod, _ in cluster.topology() if pod != owner)
    value = ('cluster-' + cluster.run_id).encode()
    if cluster.command(key, 'SET', key, value, 'EX', 1800, start=wrong) != b'OK':
        raise ValueError('Cluster MOVED SET failed')
    if cluster.command(key, 'GET', key, start=wrong) != value:
        raise ValueError('Cluster redirected GET lost data')
    ttl = cluster.command(key, 'TTL', key)
    if not 0 < ttl <= 1800 or cluster.redirects['MOVED'] < 2:
        raise ValueError('Cluster MOVED or TTL evidence missing')
    client = cluster.client(owner, fresh=True)
    if client.command('MULTI') != b'OK' or client.command('SET', other, value, 'EX', 1800) != b'QUEUED':
        raise ValueError('Cluster same-slot transaction did not queue')
    if client.command('GET', key) != b'QUEUED' or client.command('EXEC') != [b'OK', value]:
        raise ValueError('Cluster same-slot transaction did not commit')
    cross = prefix + '{different-slot}:x'
    if key_slot(cross) == key_slot(key):
        raise ValueError('Cross-slot fixture accidentally shares a slot')
    cluster.source.reject(lambda: cluster.client(owner, fresh=True).command('MGET', key, cross),
                          cluster.focused.RedisReplyError, 'CROSSSLOT')
    # An empty slot is a bounded protocol fixture, independent of product scale.
    ask_key = next(prefix + '{ask-' + str(i) + '}:missing' for i in range(1000)
                   if key_slot(prefix + '{ask-' + str(i) + '}:missing') != key_slot(key))
    slot = key_slot(ask_key)
    source_pod = cluster.owner(ask_key)
    source_row = next(row for row in cluster.topology() if row[2] == source_pod)
    target_row = next(row for row in cluster.topology() if row[2] != source_pod)
    source, target = cluster.client(source_pod, fresh=True), cluster.client(target_row[2], fresh=True)
    if source.command('CLUSTER', 'COUNTKEYSINSLOT', slot) != 0 or target.command('CLUSTER', 'COUNTKEYSINSLOT', slot) != 0:
        raise ValueError('ASK fixture requires a proven empty slot on both nodes')
    journal = cluster.cluster / 'redis-cluster-ask-fault.json'
    with journal.open('x') as stream:
        journal.chmod(0o600)
        json.dump({'run_id': cluster.run_id, 'cr_uid': cluster.journal['cr_uid'], 'slot': slot,
                   'source': source_pod, 'target': target_row[2], 'owner_id': source_row[3]}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    failure = None
    try:
        if target.command('CLUSTER', 'SETSLOT', slot, 'IMPORTING', source_row[3]) != b'OK':
            raise ValueError('ASK import fixture rejected')
        if source.command('CLUSTER', 'SETSLOT', slot, 'MIGRATING', target_row[3]) != b'OK':
            raise ValueError('ASK migration fixture rejected')
        if cluster.command(ask_key, 'GET', ask_key, start=source_pod) is not None or cluster.redirects['ASK'] != 1:
            raise ValueError('Cluster client did not follow ASK for the missing key')
        cluster.source.reject(lambda: target.command('GET', ask_key), cluster.focused.RedisReplyError, 'MOVED')
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # These are only temporary migration flags; slot ownership never changes.
        errors = []
        for pod in (source_pod, target_row[2]):
            try:
                if cluster.get('rediscluster', NAME)['metadata']['uid'] != cluster.journal['cr_uid']:
                    raise ValueError('ASK cleanup refuses a changed declaration identity')
                if cluster.client(pod, fresh=True).command('CLUSTER', 'SETSLOT', slot, 'STABLE') != b'OK':
                    raise ValueError('ASK fixture flag cleanup rejected')
            except BaseException as exc:
                errors.append(exc)
        if errors:
            if failure is None:
                raise errors[0]
            print('REDIS_CLUSTER_ASK_CLEANUP_FAIL first failure retained journal=retained', flush=True)
        else:
            cluster.health()
            if cluster.owner(ask_key) != source_pod:
                raise ValueError('ASK fixture changed the slot owner')
            journal.unlink()
            print('REDIS_CLUSTER_ASK_FIXTURE_CLEAN_PASS empty-slot=retained owner=unchanged', flush=True)
    if cluster.command(other, 'GET', other) != value:
        raise ValueError('Cluster transaction data changed after ASK fixture')
    print('REDIS_CLUSTER_CLIENT_PASS moved=verified ask=verified same-slot-transaction=committed '
          'cross-slot=rejected ttl=positive digest=' + hashlib.sha256(value).hexdigest(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('cluster-data',))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    focused = load(args.base, 'redis-focused-gate.py', 'redis_cluster_client_focused')
    with tempfile.TemporaryDirectory(prefix='redis-cluster-client-') as temporary, ExitStack() as stack:
        source = focused.Gate(args, Path(temporary), stack)
        source.baseline()
        cluster = Cluster(source, focused, Path(temporary))
        if (source.cluster / 'redis-cluster-ask-fault.json').exists():
            raise ValueError('Cluster client refuses stale ASK flags journal')
        cluster.health()
        failure = None
        try:
            data_gate(cluster)
        except BaseException as exc:
            failure = exc
            print(source.k('-n', source.ns, 'get', 'events', '--sort-by=.lastTimestamp'), flush=True)
            raise
        finally:
            try:
                cluster.cleanup()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_CLUSTER_CLIENT_CLEANUP_FAIL first failure retained', flush=True)
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=cluster-data', flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=cluster-data', flush=True)


if __name__ == '__main__':
    main()
