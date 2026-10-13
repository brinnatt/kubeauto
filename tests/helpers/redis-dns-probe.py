#!/usr/bin/env python3
"""Read-only actual resolver diagnostics on the identity-verified Redis lab."""
from pathlib import Path
import json
import subprocess

base = Path('/usr/local/kubeauto')
cluster = base / 'clusters/redis-gate'
owner = json.loads((cluster / 'redis-lab-owner.json').read_text())
k = ['kubectl', '--kubeconfig=' + str(cluster / 'kubectl.kubeconfig')]
def get(*argv):
    return subprocess.check_output([*k, *argv], text=True, timeout=30)
system = json.loads(get('get', 'namespace', 'kube-system', '-o', 'json'))
if owner.get('cluster_uid') != system['metadata']['uid']:
    raise ValueError('DNS probe refuses changed cluster identity')
pods = json.loads(get('-n', 'redis-gate', 'get', 'pods', '-l', 'role=replication', '-o', 'json'))['items']
for pod in pods:
    name = pod['metadata']['name']
    container = pod['spec']['containers'][0]['name']
    print(name, get('-n', 'redis-gate', 'exec', name, '-c', container, '--', 'cat', '/etc/resolv.conf'), flush=True)
print(get('-n', 'kube-system', 'get', 'services', '-o', 'wide'))
print(get('-n', 'kube-system', 'get', 'pods', '-l', 'k8s-app=kube-dns', '-o', 'wide'))
print('REDIS_DNS_PROBE_PASS identity=verified mutation=none')
