#!/usr/bin/env python3
"""Read-only Sentinel protocol diagnostics, including during an owned fault."""
from contextlib import ExitStack
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace

base = Path('/usr/local/kubeauto')
spec = importlib.util.spec_from_file_location('redis_focused', base / 'tests/helpers/redis-focused-gate.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix='redis-probe-') as temporary, ExitStack() as stack:
    gate = module.Gate(SimpleNamespace(base=base), Path(temporary), stack)
    owner = json.loads((gate.cluster / 'redis-lab-owner.json').read_text())
    system = json.loads(gate.k('get', 'namespace', 'kube-system', '-o', 'json'))
    if owner.get('cluster_uid') != system['metadata']['uid']:
        raise ValueError('probe refuses changed cluster identity')
    for key in ('ca.crt', 'tls.crt'):
        (Path(temporary) / key).write_bytes(gate.tls[key])
    for command in (['openssl', 'version'], ['openssl', 'x509', '-in', str(Path(temporary) / 'tls.crt'),
                    '-noout', '-issuer', '-subject', '-ext', 'authorityKeyIdentifier'],
                    ['openssl', 'verify', '-CAfile', str(Path(temporary) / 'ca.crt'), str(Path(temporary) / 'tls.crt')]):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        print('TLS_PUBLIC_CERT_DIAGNOSTIC rc=' + str(result.returncode), result.stdout.strip(), result.stderr.strip())
    policies = gate.get('networkpolicy')['items']
    print(json.dumps({'policies': [{k: p[k] for k in ('metadata', 'spec')} for p in policies]}))
    health_spec = importlib.util.spec_from_file_location('redis_probe_health', base / 'roles/cluster-addon/files/redis-health.py')
    health = importlib.util.module_from_spec(health_spec)
    health_spec.loader.exec_module(health)
    for role, pods in gate.members().items():
        for pod in pods:
            name = pod['metadata']['name']
            container = pod['spec']['containers'][0]
            auth_names = sorted(e['name'] for e in container.get('env', [])
                                if e['name'] in ('REDISCLI_AUTH', 'REDIS_PASSWORD'))
            try:
                result = subprocess.run(gate.kubectl + ['-n', gate.ns, 'exec', name, '-c', container['name'], '--',
                    *health.AUTH_CLI_PREFIX, '-e', '--raw', '--no-auth-warning', '--tls', '--cacert', '/tls/ca.crt',
                    '--cert', '/tls/tls.crt', '--key', '/tls/tls.key', '-p',
                    '6379' if role == 'replication' else '26379', 'PING'],
                    capture_output=True, text=True, timeout=45)
                reply = re.search(r'\b(PONG|NOAUTH|WRONGPASS)\b', result.stdout + result.stderr)
                print('PRODUCT_CLI_AUTH_DIAGNOSTIC pod=' + name + ' env-keys=' + ','.join(auth_names) +
                      ' rc=' + str(result.returncode) + ' reply=' + (reply[1] if reply else 'unclassified'))
            except (OSError, subprocess.TimeoutExpired) as exc:
                print('PRODUCT_CLI_AUTH_UNAVAILABLE pod=' + name + ' reason=' + type(exc).__name__)
            try:
                client = gate.client(name, 6379 if role == 'replication' else 26379, fresh=True)
                if role == 'replication':
                    print(name, client.command('INFO', 'replication').decode())
                else:
                    print(name, repr(client.command('SENTINEL', 'MASTER', 'mymaster')))
                    print(name, repr(client.command('SENTINEL', 'REPLICAS', 'mymaster')))
                    try: print(name, repr(client.command('SENTINEL', 'CKQUORUM', 'mymaster')))
                    except module.RedisReplyError as exc: print(name, exc.code)
                container = pod['spec']['containers'][0]['name']
                print(gate.k('-n', gate.ns, 'logs', name, '-c', container, '--tail=20'))
            except Exception as exc:
                print(name, 'PROBE_ERROR', type(exc).__name__)
                if getattr(exc, 'verify_code', None) is not None:
                    print(name, 'TLS_VERIFY', exc.verify_code, exc.verify_message)
                container = pod['spec']['containers'][0]['name']
                try:
                    print(gate.k('-n', gate.ns, 'logs', name, '-c', container, '--tail=20'))
                except Exception:
                    print(name, 'PROBE_LOGS_UNAVAILABLE')
