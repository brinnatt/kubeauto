#!/usr/bin/env python3
"""Read-only independent Cluster prerequisites and first-failure diagnostics."""
import json
from pathlib import Path
import subprocess

base = Path('/usr/local/kubeauto')
source = base / 'clusters/redis-gate'
k = ['kubectl', '--kubeconfig=' + str(source / 'kubectl.kubeconfig')]
def get(*argv):
    return subprocess.check_output([*k, *argv], text=True, timeout=45)
owner = json.loads((source / 'redis-lab-owner.json').read_text())
system = json.loads(get('get', 'namespace', 'kube-system', '-o', 'json'))
if owner.get('cluster_uid') != system['metadata']['uid']:
    raise ValueError('Cluster probe refuses a changed lab identity')
for node in json.loads(get('get', 'nodes', '-o', 'json'))['items']:
    print(json.dumps({'node': node['metadata']['name'],
                      'unschedulable': node['spec'].get('unschedulable', False),
                      'taints': node['spec'].get('taints', [])}), flush=True)
print(get('-n', 'redis-gate', 'get', 'rediscluster', 'redis-gate-cluster-data', '-o', 'json'))
for kind in ('pods', 'pvc', 'statefulsets', 'services', 'pdb'):
    print(get('-n', 'redis-gate', 'get', kind, '-o', 'wide'))
print(get('-n', 'redis-gate-operator', 'logs', 'deployment/redis-operator', '--all-pods=true', '--all-containers', '--tail=100'))
pods = json.loads(get('-n', 'redis-gate', 'get', 'pods', '-o', 'json'))['items']
for pod in pods:
    if pod['metadata'].get('labels', {}).get('app') not in ('redis-gate-cluster-data-leader',
                                                          'redis-gate-cluster-data-follower'):
        continue
    name = pod['metadata']['name']
    print(get('-n', 'redis-gate', 'describe', 'pod', name))
    for container in pod['spec']['containers'] + pod['spec'].get('initContainers', []):
        result = subprocess.run([*k, '-n', 'redis-gate', 'logs', name, '-c', container['name'], '--tail=200'],
                                capture_output=True, text=True, timeout=35)
        print(name, container['name'], 'rc=' + str(result.returncode), result.stdout, flush=True)
    container = pod['spec']['containers'][0]['name']
    for command in (('CLUSTER', 'NODES'), ('CLUSTER', 'SLOTS'), ('CLUSTER', 'INFO'), ('INFO', 'replication'),
                    ('CONFIG', 'GET', 'cluster-migration-barrier', 'cluster-allow-replica-migration'),
                    ('--cluster', 'check', '127.0.0.1:6379')):
        result = subprocess.run([*k, '-n', 'redis-gate', 'exec', name, '-c', container, '--',
            'sh', '-c', 'REDISCLI_AUTH="$(printf %s "$REDISCLI_AUTH" | tr -d "\\r\\n")"; '
            'export REDISCLI_AUTH; exec redis-cli "$@"', 'redis-cli', '-e',
            '--json' if command == ('CLUSTER', 'SLOTS') else '--raw',
            '--tls', '--cacert', '/tls/ca.crt', '--cert', '/tls/tls.crt', '--key', '/tls/tls.key',
            *command], capture_output=True, text=True, timeout=35)
        print(name, ' '.join(command), 'rc=' + str(result.returncode), result.stdout, flush=True)
print(get('-n', 'redis-gate', 'get', 'events', '--sort-by=.lastTimestamp'))
print('REDIS_CLUSTER_PROBE_PASS identity=verified mutation=none')
