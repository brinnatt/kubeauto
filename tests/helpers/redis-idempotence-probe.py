#!/usr/bin/env python3
"""Read-only resource quantity and ownership diagnostics; never read Secret data."""
import json
from pathlib import Path
import subprocess
import yaml

base = Path('/usr/local/kubeauto/clusters/redis-gate')
rendered = list(yaml.safe_load_all((base / 'yml/redis/instances.yaml').read_text()))
for document in rendered:
    kind, name = document['kind'], document['metadata']['name']
    live = json.loads(subprocess.check_output([
        'kubectl', '--kubeconfig=' + str(base / 'kubectl.kubeconfig'), '-n', 'redis-gate',
        'get', kind, name, '-o', 'json', '--show-managed-fields']))
    desired = document['spec']['kubernetesConfig']['resources']
    current = live['spec']['kubernetesConfig']['resources']
    print(json.dumps({'kind': kind, 'desired': desired, 'current': current,
                      'desired_cpu_type': type(desired['limits']['cpu']).__name__,
                      'current_cpu_type': type(current['limits']['cpu']).__name__,
                      'managers': [{'manager': m['manager'], 'operation': m['operation'],
                          'resource_fields': m.get('fieldsV1', {}).get('f:spec', {}).get(
                              'f:kubernetesConfig', {}).get('f:resources', {})}
                          for m in live['metadata'].get('managedFields', [])]}, sort_keys=True))
