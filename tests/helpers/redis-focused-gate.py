#!/usr/bin/env python3
"""Focused Redis customer data/security gates on a verified product baseline."""
import argparse
import base64
from contextlib import ExitStack, contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import signal
import ssl
import subprocess
import tempfile
import time
import uuid

import yaml


class RedisReplyError(Exception):
    def __init__(self, reply):
        # Store only the machine error code, not arbitrary server text or keys.
        self.code = reply.split(b' ', 1)[0].decode('ascii', errors='replace')
        self.redirect = None
        if self.code in ('MOVED', 'ASK'):
            match = re.fullmatch(rb'(?:MOVED|ASK) ([0-9]{1,5}) ([A-Za-z0-9.-]{1,253}):([0-9]{1,5})', reply)
            if match:
                slot, host, port = int(match[1]), match[2].decode('ascii'), int(match[3])
                try:
                    if re.fullmatch(r'[0-9.]+', host):
                        ipaddress.IPv4Address(host)
                    elif not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                 for label in host.split('.')):
                        raise ValueError('invalid redirect hostname')
                    if 0 <= slot < 16384 and 0 < port < 65536:
                        self.redirect = (slot, host, port)
                except ValueError:
                    pass
        super().__init__(self.code)


def resource_identities(objects, *, operator_history=False):
    """Helm revision journals change on upgrade; customer resource identities must not."""
    result = []
    for obj in objects:
        metadata = obj['metadata']
        labels = metadata.get('labels', {})
        if (operator_history and obj.get('type') == 'helm.sh/release.v1' and
                labels.get('owner') == 'helm' and labels.get('name') == 'redis-operator' and
                re.fullmatch(r'sh\.helm\.release\.v1\.redis-operator\.v[1-9][0-9]*', metadata['name'])):
            continue
        result.append((metadata['name'], metadata['uid']))
    return sorted(result)


def isolation_policy(namespace, run_id, pods, members, *, dns_addresses=()):
    owned = {p['metadata']['name']: p for values in members.values() for p in values}
    if not pods or len(set(pods)) != len(pods) or not set(pods) <= set(owned):
        raise ValueError('fault targets must be unique owned Redis members')
    for name in pods:
        labels = owned[name]['metadata'].get('labels', {})
        if (labels.get('kubeauto.io/component') != 'redis' or
                labels.get('kubeauto.io/managed-by') != 'kubeauto' or
                labels.get('statefulset.kubernetes.io/pod-name') != name):
            raise ValueError('fault target labels do not prove exact pod ownership')
    policy = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': 'redis-gate-fault-' + run_id[:16], 'namespace': namespace,
                         'labels': {'kubeauto.io/component': 'redis-test-fault',
                                    'kubeauto.io/managed-by': 'kubeauto', 'kubeauto.io/test-run': run_id}},
            'spec': {'podSelector': {'matchExpressions': [
                {'key': 'statefulset.kubernetes.io/pod-name', 'operator': 'In', 'values': sorted(pods)}]},
                'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
    if dns_addresses:
        networks = [str(ipaddress.ip_network(str(address) + '/32', strict=True)) for address in dns_addresses]
        policy['spec']['egress'] = [{'to': [{'ipBlock': {'cidr': network}} for network in networks],
            'ports': [{'protocol': protocol, 'port': 53} for protocol in ('UDP', 'TCP')]}]
    return policy


def encode_command(*parts):
    values = [p if isinstance(p, bytes) else str(p).encode() for p in parts]
    return b'*' + str(len(values)).encode() + b'\r\n' + b''.join(
        b'$' + str(len(p)).encode() + b'\r\n' + p + b'\r\n' for p in values)


def read_reply(stream):
    line = stream.readline()
    if not line.endswith(b'\r\n'):
        raise EOFError('truncated Redis reply')
    kind, payload = line[:1], line[1:-2]
    if kind == b'+': return payload
    if kind == b'-': raise RedisReplyError(payload)
    if kind == b':': return int(payload)
    if kind in (b'$', b'*'):
        length = int(payload)
        if length == -1: return None
        if not 0 <= length <= 64 * 1024 * 1024:
            raise ValueError('Redis reply exceeds fixture bound')
        if kind == b'*': return [read_reply(stream) for _ in range(length)]
        value = stream.read(length)
        if len(value) != length or stream.read(2) != b'\r\n':
            raise EOFError('truncated Redis bulk reply')
        return value
    raise ValueError('unexpected Redis protocol type')


class Client:
    def __init__(self, port, context, server_name, password, user='default'):
        raw = socket.create_connection(('127.0.0.1', port), timeout=5)
        try:
            self.socket = context.wrap_socket(raw, server_hostname=server_name)
        except BaseException:
            raw.close()
            raise
        self.stream = self.socket.makefile('rb')
        try:
            if self.command('AUTH', user, password.replace(b'\r', b'').replace(b'\n', b'')) != b'OK':
                raise ValueError('Redis fixture authentication failed')
        except BaseException:
            self.close()
            raise

    def command(self, *parts):
        self.socket.sendall(encode_command(*parts))
        return read_reply(self.stream)

    def close(self):
        self.stream.close()
        self.socket.close()


class Gate:
    def __init__(self, args, work, stack):
        self.args, self.work, self.stack = args, work, stack
        self.base = args.base
        self.cluster = self.base / 'clusters/redis-gate'
        self.config = yaml.safe_load((self.cluster / 'config.yml').read_text())
        self.kubeconfig = self.cluster / 'kubectl.kubeconfig'
        self.ns, self.name = self.config['redis_namespace'], self.config['redis_name']
        if (self.ns != 'redis-gate' or self.name != 'redis-gate-data' or
                self.config['redis_mode'] != 'sentinel'):
            raise ValueError('focused gate requires the owned Sentinel fixture')
        self.kubectl = ['kubectl', '--kubeconfig=' + str(self.kubeconfig)]
        self.passwords = self.secret('redis-gate-auth')
        self.tls = self.secret('redis-gate-tls')
        for key in ('ca.crt', 'tls.crt', 'tls.key'):
            (work / key).write_bytes(self.tls[key])
            (work / key).chmod(0o600)
        self.context = ssl.create_default_context(cafile=str(work / 'ca.crt'))
        self.context.load_cert_chain(str(work / 'tls.crt'), str(work / 'tls.key'))
        self.server_name = self.name + '-master'
        self.forwards = {}
        self.clients = []
        self.run_id = uuid.uuid4().hex
        self.key = 'kubeauto:redis:focused:' + self.run_id
        self.value = ('verified-' + self.run_id).encode()

    def run(self, argv, *, input_text=None, timeout=90, capture=True):
        result = subprocess.run(argv, input=input_text, text=True, capture_output=capture,
                                timeout=timeout, cwd=self.base)
        if result.returncode:
            raise ValueError('focused command failed: ' + argv[0])
        return result.stdout.strip() if capture else ''

    def k(self, *argv, **kwargs):
        return self.run([*self.kubectl, *argv], **kwargs)

    def get(self, kind, name=None):
        return json.loads(self.k('-n', self.ns, 'get', kind, *([name] if name else []), '-o', 'json'))

    def secret(self, name):
        return {key: base64.b64decode(value, validate=True)
                for key, value in self.get('secret', name)['data'].items()}

    def identity(self):
        owner = json.loads((self.cluster / 'redis-lab-owner.json').read_text())
        namespace = json.loads(self.k('get', 'namespace', 'kube-system', '-o', 'json'))
        if (not owner.get('healthy') or owner.get('cluster_uid') != namespace['metadata']['uid'] or
                owner.get('nodes') != [f'192.168.47.{i}' for i in range(131,137)]):
            raise ValueError('Redis focused baseline ownership/identity is invalid')
        for ns in (self.ns, self.config['redis_operator_namespace']):
            labels = self.get('namespace', ns)['metadata'].get('labels', {})
            if labels.get('kubeauto.io/component') != 'redis' or labels.get('kubeauto.io/managed-by') != 'kubeauto':
                raise ValueError('foreign Redis focused namespace')

    def baseline(self):
        self.identity()
        if any((self.cluster / name).exists() for name in
               ('redis-stability-fault.json', 'redis-stability-fault.tmp', 'redis-crd-upgrade-fault.json', 'redis-maintenance-fault.json', 'redis-upgrade-fault.json', 'redis-cluster-upgrade-fault.json')):
            raise ValueError('focused baseline has an unrecovered stability or CRD mutation journal')
        policies = self.get('networkpolicy')['items']
        if policies:
            raise ValueError('stale fault or foreign network policy in focused baseline')
        self.health()
        self.run([str(self.base / '.venv/bin/python'),
                  str(self.base / 'tests/helpers/redis-lab-storage.py'), 'health', '--base', str(self.base)],
                 capture=False)
        print('REDIS_FOCUSED_BASELINE_PASS identity=current faults=none', flush=True)

    def repair_baseline(self):
        self.identity()
        if self.get('networkpolicy')['items']:
            raise ValueError('repair refuses stale/foreign network policies')
        pods = self.members()
        if len(pods['replication']) != 3 or len(pods['sentinel']) != 3:
            raise ValueError('repair requires exactly the owned six-member fixture')
        statuses = {}
        for pod in pods['replication']:
            name = pod['metadata']['name']
            labels = pod['metadata'].get('labels', {})
            if (labels.get('kubeauto.io/managed-by') != 'kubeauto' or labels.get('kubeauto.io/component') != 'redis' or
                    labels.get('app') != self.name):
                raise ValueError('repair refuses foreign Redis data members')
            text = self.client(name).command('INFO', 'replication').decode()
            statuses[name] = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
        masters = [name for name, status in statuses.items() if status.get('role') == 'master']
        declared = self.get('redisreplication', self.name).get('status', {}).get('masterNode')
        if masters != [declared] or statuses[declared].get('connected_slaves') != '2':
            raise ValueError('repair refuses inconsistent real/controller primary or replication')
        primary = next(p for p in pods['replication'] if p['metadata']['name'] == declared)
        addresses = {primary['status']['podIP'], declared,
                     f"{primary['spec']['hostname']}.{primary['spec']['subdomain']}.{self.ns}.svc.cluster.local"}
        for name, status in statuses.items():
            if name != declared and (status.get('role') != 'slave' or status.get('master_link_status') != 'up' or
                    status.get('master_host') not in addresses or status.get('master_sync_in_progress') != '0'):
                raise ValueError('repair refuses unsynchronized data replicas')
        self.run([str(self.base / '.venv/bin/python'), str(self.base / 'tests/helpers/redis-lab-storage.py'),
                  'health', '--base', str(self.base)], capture=False)
        print('REDIS_REPAIR_DATA_BOUNDARY_PASS identity=current sole-primary=verified replicas=2', flush=True)

    def repair_sentinel(self):
        # Only the known product declaration changes; no imperative Sentinel
        # RESET/FAILOVER or replica role mutation is permitted here.
        before = resource_identities(self.get('pvc')['items'])
        self.run([str(self.base / '.venv/bin/python'), str(self.base / 'kubecli.py'),
                  'setup', 'redis-gate', '07', '--tags', 'redis'], timeout=900, capture=False)
        self.forwards.clear()
        self.health()
        if before != resource_identities(self.get('pvc')['items']):
            raise ValueError('DNS repair replaced a data PVC')
        primary = self.client(self.primary(), fresh=True)
        cursor, cleaned = b'0', 0
        while True:
            cursor, keys = primary.command('SCAN', cursor, 'MATCH', 'kubeauto:redis:focused:*', 'COUNT', 100)
            for key in keys:
                suffix = key.decode().removeprefix('kubeauto:redis:focused:')
                if not re.fullmatch(r'[0-9a-f]{32}', suffix) or primary.command('GET', key) != ('verified-' + suffix).encode():
                    raise ValueError('repair refuses an unproven fixture key')
                primary.command('DEL', key)
                cleaned += 1
            if cursor == b'0':
                break
            if cleaned > 1000:
                raise ValueError('repair fixture key scope exceeds bound')
        print(f'REDIS_SENTINEL_DNS_REPAIR_PASS discovery=current pvc-uids=retained owned-keys-cleaned={cleaned}', flush=True)

    def health(self):
        self.run([str(self.base / '.venv/bin/python'),
                  str(self.base / 'roles/cluster-addon/files/redis-health.py'),
                  '--kubeconfig', str(self.kubeconfig), '--namespace', self.ns,
                  '--name', self.name, '--mode', 'sentinel', '--tls', '--wait', '300'],
                 timeout=350, capture=False)

    def forward(self, pod, remote_port, *, reuse=True):
        key = (pod, remote_port)
        if reuse and key in self.forwards:
            return self.forwards[key]
        output = self.work / f'{pod}-{remote_port}-{uuid.uuid4().hex[:8]}.forward.log'
        stream = output.open('w')
        process = subprocess.Popen([*self.kubectl, '-n', self.ns, 'port-forward',
                                    'pod/' + pod, f':{remote_port}', '--address=127.0.0.1'],
                                   stdout=stream, stderr=subprocess.STDOUT, text=True)

        def cleanup():
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            stream.close()

        self.stack.callback(cleanup)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for line in output.read_text().splitlines():
                if line.startswith('Forwarding from 127.0.0.1:'):
                    port = int(line.split(':', 1)[1].split(' ')[0])
                    if reuse:
                        self.forwards[key] = port
                    return port
            if process.poll() is not None:
                raise ValueError('focused port-forward exited before readiness')
            time.sleep(0.2)
        raise ValueError('focused port-forward readiness timeout')

    def client(self, pod, port=6379, *, context=None, server_name=None, password=None, user='default', fresh=False):
        client = Client(self.forward(pod, port, reuse=not fresh), context or self.context,
                        server_name or self.server_name,
                        password if password is not None else self.passwords['password'], user)
        self.stack.callback(client.close)
        return client

    def members(self):
        pods = self.get('pods')['items']
        return {role: [p for p in pods if p['metadata'].get('labels', {}).get('role') == role]
                for role in ('replication', 'sentinel')}

    def primary(self):
        pods = self.members()
        by_ip = {p['status']['podIP']: p['metadata']['name'] for p in pods['replication']}
        for pod in pods['replication']:
            hostname = pod['spec'].get('hostname', pod['metadata']['name'])
            subdomain = pod['spec'].get('subdomain')
            if subdomain:
                by_ip[f'{hostname}.{subdomain}.{self.ns}.svc.cluster.local'] = pod['metadata']['name']
        discovered = []
        for pod in pods['sentinel']:
            client = self.client(pod['metadata']['name'], 26379)
            answer = client.command('SENTINEL', 'get-master-addr-by-name', 'mymaster')
            if not answer or len(answer) != 2 or answer[1] != b'6379':
                raise ValueError('Sentinel discovery did not return a Redis data endpoint')
            discovered.append(answer[0].decode())
        if len(discovered) != 3 or len(set(discovered)) != 1 or discovered[0] not in by_ip:
            raise ValueError('Sentinel members disagree on primary identity')
        return by_ip[discovered[0]]

    def data(self):
        primary = self.primary()
        client = self.client(primary)
        if client.command('SET', self.key, self.value, 'EX', '600') != b'OK':
            raise ValueError('authenticated TLS business write failed')
        if client.command('WAIT', '2', '5000') != 2:
            raise ValueError('business write did not reach both replicas')
        if client.command('GET', self.key) != self.value or not 590 <= client.command('TTL', self.key) <= 600:
            raise ValueError('business read/digest/TTL mismatch')
        for pod in self.members()['replication']:
            replica = self.client(pod['metadata']['name'])
            if replica.command('GET', self.key) != self.value:
                raise ValueError('replicated business digest mismatch')
        client.close()
        client = self.client(self.primary())
        if client.command('GET', self.key) != self.value:
            raise ValueError('reconnected Sentinel client business digest mismatch')
        client.command('DEL', self.key)
        print('REDIS_SENTINEL_CLIENT_PASS discovery=three-views replicas=2 tls-hostname=verified '
              'sha256=' + hashlib.sha256(self.value).hexdigest(), flush=True)

    def reject(self, action, exception, code=None):
        try:
            action()
        except exception as exc:
            if code and getattr(exc, 'code', None) != code:
                raise ValueError('negative test failed with an unrelated Redis error') from None
            return
        raise ValueError('negative test unexpectedly succeeded')

    def security(self):
        primary = self.primary()
        good = self.client(primary)
        if good.command('PING') != b'PONG':
            raise ValueError('security positive control failed')
        self.reject(lambda: self.client(primary, password=b'deliberately-invalid', fresh=True), RedisReplyError, 'WRONGPASS')
        self.reject(lambda: self.client(primary, server_name='wrong-host.invalid', fresh=True), ssl.SSLCertVerificationError)
        wrong_ca = ssl.create_default_context()
        wrong_ca.load_cert_chain(str(self.work / 'tls.crt'), str(self.work / 'tls.key'))
        self.reject(lambda: self.client(primary, context=wrong_ca, fresh=True), ssl.SSLCertVerificationError)
        no_cert = ssl.create_default_context(cafile=str(self.work / 'ca.crt'))
        self.reject(lambda: self.client(primary, context=no_cert, fresh=True), ssl.SSLError)
        app = self.client(primary, password=self.passwords['app-password'], user='gate-app')
        if app.command('SET', self.key, self.value, 'EX', '600') != b'OK' or app.command('GET', self.key) != self.value:
            raise ValueError('ACL positive command/key control failed')
        self.reject(lambda: app.command('CONFIG', 'GET', 'maxmemory'), RedisReplyError, 'NOPERM')
        self.reject(lambda: app.command('GET', 'other-business:' + self.run_id), RedisReplyError, 'NOPERM')
        app.command('DEL', self.key)
        print('REDIS_SENTINEL_SECURITY_PASS wrong-password=denied wrong-ca=denied wrong-san=denied '
              'client-cert=required acl-command=denied acl-key=denied', flush=True)

    def idempotence(self):
        primary = self.primary()
        client = self.client(primary)
        client.command('SET', self.key, self.value, 'EX', '600')
        if client.command('WAIT', '2', '5000') != 2:
            raise ValueError('idempotence baseline is not replicated')
        kinds = ('redisreplication', 'redissentinel', 'statefulsets', 'pods', 'pvc', 'services', 'secrets')
        def identities():
            result = {kind: resource_identities(self.get(kind)['items']) for kind in kinds}
            for kind in ('deployments', 'pods', 'services', 'secrets'):
                objects = json.loads(self.k('-n', self.config['redis_operator_namespace'],
                                            'get', kind, '-o', 'json'))['items']
                result['operator-' + kind] = resource_identities(objects, operator_history=kind == 'secrets')
            return result
        before = identities()
        self.run([str(self.base / '.venv/bin/python'), str(self.base / 'kubecli.py'),
                  'setup', 'redis-gate', '07', '--tags', 'redis'], timeout=900, capture=False)
        after = identities()
        if after != before:
            print('REDIS_IDEMPOTENCE_IDENTITY_DIFF ' + json.dumps(
                {kind: {'before': before[kind], 'after': after[kind]} for kind in before
                 if before[kind] != after[kind]}, sort_keys=True), flush=True)
            raise ValueError('identical product input replaced a member, Secret, PVC or Service')
        if client.command('GET', self.key) != self.value:
            raise ValueError('identical product input changed business data')
        client.command('DEL', self.key)
        print('REDIS_IDEMPOTENCE_PASS resource-uids=unchanged business-digest=unchanged', flush=True)

    def cleanup_keys(self):
        # This UUID key belongs only to the invocation. Preserve every other key.
        client = self.client(self.primary())
        client.command('DEL', self.key)
        if client.command('EXISTS', self.key) != 0:
            raise ValueError('focused business key remains')
        print('REDIS_FOCUSED_KEYS_CLEAN_PASS', flush=True)

    @contextmanager
    def isolated(self, pods, *, dns_addresses=()):
        policy = isolation_policy(self.ns, self.run_id, pods, self.members(), dns_addresses=dns_addresses)
        name = policy['metadata']['name']
        journal = self.cluster / 'redis-focused-fault.json'
        with journal.open('x') as out:
            os.chmod(journal, 0o600)
            json.dump({'policy': policy, 'cluster_uid': json.loads(
                (self.cluster / 'redis-lab-owner.json').read_text())['cluster_uid']}, out)
            out.flush()
            os.fsync(out.fileno())
        failure = None
        try:
            self.k('create', '-f', '-', input_text=json.dumps(policy))
            print('REDIS_FAULT_APPLIED targets=' + ','.join(sorted(pods)), flush=True)
            yield
        except BaseException as exc:
            failure = exc
            try:
                self.fault_diagnostics()
            except Exception as diagnostic_error:
                print('REDIS_FAULT_DIAGNOSTIC_ERROR class=' + type(diagnostic_error).__name__, flush=True)
            raise
        finally:
            try:
                existing = self.k('-n', self.ns, 'get', 'networkpolicy', name, '--ignore-not-found', '-o', 'json')
                if existing:
                    metadata = json.loads(existing)['metadata']
                    if (metadata['name'] != name or metadata.get('namespace') != self.ns or
                            metadata.get('labels', {}) != policy['metadata']['labels']):
                        raise ValueError('fault cleanup refuses changed ownership')
                    self.k('-n', self.ns, 'delete', 'networkpolicy', name, '--wait=true', '--timeout=60s')
                if self.k('-n', self.ns, 'get', 'networkpolicy', name, '--ignore-not-found', '-o', 'name'):
                    raise ValueError('fault policy remains after cleanup')
                journal.unlink()
                print('REDIS_FOCUSED_FAULT_CLEAN_PASS', flush=True)
                self.health()
            except BaseException:
                if failure is None:
                    raise
                print('REDIS_FOCUSED_FAULT_CLEANUP_FAIL original failure retained', flush=True)

    def fault_diagnostics(self):
        print('REDIS_FIRST_FAULT_STATE_BEGIN before=cleanup', flush=True)
        print(json.dumps({'policies': [p['spec'] for p in self.get('networkpolicy')['items']]}), flush=True)
        for role, pods in self.members().items():
            for pod in pods:
                name = pod['metadata']['name']
                try:
                    client = self.client(name, 6379 if role == 'replication' else 26379, fresh=True)
                    if role == 'replication':
                        print(name, client.command('INFO', 'replication').decode(), flush=True)
                    else:
                        reply = client.command('SENTINEL', 'MASTER', 'mymaster')
                        fields = dict(zip(reply[::2], reply[1::2]))
                        print(name, json.dumps({k.decode(): fields.get(k, b'').decode() for k in (
                            b'ip', b'flags', b'num-slaves', b'num-other-sentinels', b'quorum')}), flush=True)
                    container = pod['spec']['containers'][0]['name']
                    print(self.k('-n', self.ns, 'logs', name, '-c', container, '--tail=30'), flush=True)
                except Exception as exc:
                    print(name, 'REDIS_FAULT_MEMBER_DIAGNOSTIC_ERROR class=' + type(exc).__name__, flush=True)
        print('REDIS_FIRST_FAULT_STATE_END', flush=True)

    def disconnect_existing(self, pod, port=6379):
        # NetworkPolicy does not portably close established TCP streams. Close
        # only this dedicated lab member's connections so all peers re-connect
        # through the active policy. The current administrative socket is kept.
        client = self.client(pod, port, fresh=True)
        counts = {}
        for kind in ('normal', 'pubsub', 'replica'):
            count = client.command('CLIENT', 'KILL', 'TYPE', kind, 'SKIPME', 'yes')
            if not isinstance(count, int) or count < 0:
                raise ValueError('fault connection reset did not return a count')
            counts[kind] = count
        print('REDIS_FAULT_CONNECTION_RESET pod=' + pod + ' counts=' + json.dumps(counts), flush=True)
        return client

    def seeded_primary(self):
        primary = self.primary()
        client = self.client(primary)
        if client.command('SET', self.key, self.value, 'EX', '1200') != b'OK' or client.command('WAIT', 2, 5000) != 2:
            raise ValueError('fault baseline business write is not on both replicas')
        return primary, client

    def resolvers(self, pods):
        if not self.config.get('ENABLE_LOCAL_DNS_CACHE'):
            raise ValueError('fault fixture requires its verified NodeLocal DNS path')
        expected = self.config['LOCAL_DNS_CACHE']
        ipaddress.IPv4Address(expected)
        for pod in pods:
            container = self.get('pod', pod)['spec']['containers'][0]['name']
            text = self.k('exec', pod, '-n', self.ns, '-c', container, '--', 'cat', '/etc/resolv.conf')
            addresses = [line.split()[1] for line in text.splitlines() if line.startswith('nameserver ')]
            if addresses != [expected]:
                raise ValueError('fault fixture refuses an unexplained actual DNS resolver')
        return (expected,)

    def failover(self):
        before, old = self.seeded_primary()
        started = time.monotonic()
        after = None
        with self.isolated([before]):
            time.sleep(3)
            old = self.disconnect_existing(before)
            while time.monotonic() - started < 150:
                try:
                    candidate = self.primary()
                    if candidate != before:
                        after = candidate
                        break
                except (ValueError, OSError, EOFError, RedisReplyError):
                    pass
                print(f'REDIS_FAILOVER_HEARTBEAT elapsed={int(time.monotonic()-started)} state=waiting', flush=True)
                time.sleep(5)
            if after is None:
                raise ValueError('single primary isolation did not elect a new primary')
            new = self.client(after)
            if new.command('ROLE')[0] != b'master' or new.command('GET', self.key) != self.value:
                raise ValueError('new primary role or retained data is incorrect')
            # The isolated old primary must refuse new writes after its lag limit.
            time.sleep(max(0, 12 - (time.monotonic() - started)))
            self.reject(lambda: old.command('SET', self.key, self.value), RedisReplyError, 'NOREPLICAS')
            if new.command('SET', self.key, self.value, 'EX', '1200') != b'OK' or new.command('WAIT', 1, 5000) < 1:
                raise ValueError('new primary cannot safely resume writes')
        restored = self.client(self.primary(), fresh=True)
        if restored.command('GET', self.key) != self.value or restored.command('TTL', self.key) <= 900:
            raise ValueError('failover/recovery business digest or TTL is incorrect')
        if restored.command('SET', self.key, self.value, 'KEEPTTL') != b'OK' or restored.command('WAIT', 2, 5000) != 2:
            raise ValueError('recovered old primary has not caught up as a replica')
        for pod in self.members()['replication']:
            if self.client(pod['metadata']['name'], fresh=True).command('GET', self.key) != self.value:
                raise ValueError('recovered member business digest mismatch')
        print(f'REDIS_SENTINEL_FAILOVER_PASS before={before} after={after} '
              f'elapsed={int(time.monotonic()-started)} replicas=2 digest=verified stale-primary-write=denied', flush=True)

    def quorum_loss(self):
        before, old = self.seeded_primary()
        sentinels = sorted(p['metadata']['name'] for p in self.members()['sentinel'])
        targets = [before, *sentinels[:2]]
        with self.isolated(targets, dns_addresses=self.resolvers(targets)):
            time.sleep(3)
            old = self.disconnect_existing(before)
            for sentinel in sentinels:
                self.disconnect_existing(sentinel, 26379)
            survivor = self.client(sentinels[2], 26379, fresh=True)
            started = time.monotonic()
            refused = False
            # Observe longer than the configured 60 s failover timeout.
            while time.monotonic() - started < 75:
                try:
                    survivor.command('SENTINEL', 'CKQUORUM', 'mymaster')
                except RedisReplyError as exc:
                    if exc.code != 'NOQUORUM':
                        raise
                    refused = True
                for pod in self.members()['replication']:
                    role = self.client(pod['metadata']['name'], fresh=True).command('ROLE')[0]
                    if role != (b'master' if pod['metadata']['name'] == before else b'slave'):
                        raise ValueError('unsafe primary promotion during Sentinel majority loss')
                print(f'REDIS_QUORUM_HEARTBEAT elapsed={int(time.monotonic()-started)} '
                      f'refused={str(refused).lower()} primary=unchanged', flush=True)
                time.sleep(5)
            if not refused:
                raise ValueError('majority-loss fixture failed to demonstrate NOQUORUM')
            self.reject(lambda: old.command('SET', self.key, self.value), RedisReplyError, 'NOREPLICAS')
        primary = self.client(self.primary(), fresh=True)
        if primary.command('GET', self.key) != self.value or primary.command('SET', self.key, self.value, 'KEEPTTL') != b'OK':
            raise ValueError('quorum restoration did not retain data and restore safe writes')
        if primary.command('WAIT', 2, 5000) != 2:
            raise ValueError('quorum restoration has incomplete replication')
        print('REDIS_SENTINEL_QUORUM_PASS majority=lost unsafe-promotion=denied '
              'stale-primary-write=denied restored=healthy digest=verified', flush=True)

    def persistence(self):
        primary, client = self.seeded_primary()
        if client.command('WAITAOF', 1, 2, 5000) != [1, 2]:
            raise ValueError('business write is not durably fsynced on all three members')
        pods_before = {p['metadata']['name']: p['metadata']['uid'] for p in self.members()['replication']}
        claims_before = resource_identities(self.get('pvc')['items'])
        for pod in self.members()['replication']:
            member = self.client(pod['metadata']['name'])
            if member.command('SAVE') != b'OK':
                raise ValueError('RDB checkpoint failed before restart')
            info = dict(line.split(':', 1) for line in member.command('INFO', 'persistence').decode().splitlines()
                        if ':' in line and not line.startswith('#'))
            if (info.get('aof_enabled') != '1' or info.get('aof_last_write_status') != 'ok' or
                    info.get('rdb_last_bgsave_status') != 'ok'):
                raise ValueError('AOF/RDB persistence status is not healthy')
        self.k('-n', self.ns, 'rollout', 'restart', 'statefulset/' + self.name)
        self.k('-n', self.ns, 'rollout', 'status', 'statefulset/' + self.name, '--timeout=600s', timeout=620,
               capture=False)
        self.forwards.clear()
        self.health()
        pods_after = {p['metadata']['name']: p['metadata']['uid'] for p in self.members()['replication']}
        if set(pods_after) != set(pods_before) or any(pods_after[p] == pods_before[p] for p in pods_before):
            raise ValueError('persistence fixture did not replace every original Redis process')
        if claims_before != resource_identities(self.get('pvc')['items']):
            raise ValueError('rolling restart replaced a source PVC')
        for pod in self.members()['replication']:
            member = self.client(pod['metadata']['name'], fresh=True)
            if member.command('GET', self.key) != self.value or member.command('TTL', self.key) <= 500:
                raise ValueError('disk recovery lost the business digest or TTL')
        restored = self.client(self.primary(), fresh=True)
        if restored.command('SET', self.key, self.value, 'KEEPTTL') != b'OK' or restored.command('WAITAOF', 1, 2, 5000) != [1, 2]:
            raise ValueError('disk recovery did not restore durable writes')
        print('REDIS_AOF_PERSISTENCE_PASS fsync=three-members checkpoint=rdb-ok '
              'process-uids=all-replaced pvc-uids=retained digest=verified ttl=verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', choices=('sentinel-data', 'sentinel-security', 'idempotence',
                                         'sentinel-failure', 'sentinel-quorum', 'persistence', 'sentinel-repair'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    args = parser.parse_args()
    # SIGTERM must unwind ExitStack and the invocation's business key cleanup.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    failure = None
    with tempfile.TemporaryDirectory(prefix='kubeauto-redis-focused-') as temporary:
        with ExitStack() as stack:
            gate = Gate(args, Path(temporary), stack)
            if (gate.cluster / 'redis-focused-fault.json').exists():
                raise ValueError('interrupted fault journal requires scoped recovery before reuse')
            if args.module == 'sentinel-repair':
                gate.repair_baseline()
            else:
                gate.baseline()
            try:
                {'sentinel-data': gate.data, 'sentinel-security': gate.security,
                 'idempotence': gate.idempotence, 'sentinel-failure': gate.failover,
                 'sentinel-quorum': gate.quorum_loss, 'persistence': gate.persistence,
                 'sentinel-repair': gate.repair_sentinel}[args.module]()
            except BaseException as exc:
                failure = exc
                raise
            finally:
                try:
                    gate.cleanup_keys()
                except Exception:
                    if failure is None:
                        raise
                    print('REDIS_FOCUSED_CLEANUP_FAIL original failure retained', flush=True)
        # ExitStack has now closed every socket and terminated every port-forward.
    print('REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + args.module, flush=True)
    print('REDIS_FOCUSED_MODULE_PASS module=' + args.module, flush=True)


if __name__ == '__main__':
    main()
