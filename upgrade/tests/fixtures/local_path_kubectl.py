"""Isolated process adapter; simulates only LocalPathStage's Kubernetes boundary."""
from copy import deepcopy
import json
from pathlib import Path
import sys

root = Path(__file__).parent
state = json.loads((root/'state.json').read_text())
args = sys.argv[1:]
assert args[:8] == ['--kubeconfig', str(root/'kubeconfig'), '--namespace', 'kube-system',
                    '--server', 'https://master:6443', '--request-timeout=10s',
                    '--insecure-skip-tls-verify=false']
args = args[8:]
with (root/'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args)+'\n')
if state.get('command_failure') == args:
    print('SENSITIVE_LOCALPATH_SENTINEL_8042', file=sys.stderr)
    raise SystemExit(42)
if args == ['get', '--raw=/version']:
    out = {'gitVersion': state.get('api', 'v1.34.13')}
elif args == ['get', '--raw=/readyz']:
    print('ok')
    raise SystemExit(0)
elif args == ['get', 'namespace', 'kube-system', '-o', 'json']:
    out = {'metadata': {'uid': state.get('cluster_uid', 'cluster-uid')}, 'status': {'phase': 'Active'}}
elif args == ['get', 'nodes', '-o', 'json']:
    out = {'items': state['nodes']}
elif args[0] == 'get':
    ref = args[1].removeprefix('--raw=')
    out = state['objects'][ref]
elif args[0] == 'patch':
    assert args[:1]+args[2:4] == ['patch', '--type=json', '-p']
    ref = args[1]
    assert ref in ['configmap/local-path-config', 'deployment/local-path-provisioner']
    obj = state['objects'][ref]
    ops = json.loads(args[4])
    path = '/data/helperPod.yaml' if ref.startswith('configmap') else '/spec/template/spec/containers/0/image'
    assert ops[:2] == [{'op': 'test', 'path': '/metadata/'+k, 'value': obj['metadata'][k]}
                      for k in ['uid', 'resourceVersion']]
    assert len(ops) == 4 and ops[2]['op'] == 'test' and ops[3]['op'] == 'replace'
    assert ops[2]['path'] == ops[3]['path'] == path
    out = deepcopy(obj)
    parent = out
    keys = path.strip('/').split('/')
    for key in keys[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    assert parent[keys[-1]] == ops[2]['value']
    parent[keys[-1]] = ops[3]['value']
    if args[5:]:
        assert args[5:] == ['--dry-run=server', '-o', 'json']
        if state.get('dry_mutation') == ref:
            out['metadata']['annotations'] = {'injected': 'unreviewed'}
    else:
        if state.get('conflict') == ref:
            print('SENSITIVE_LOCALPATH_SENTINEL_8042', file=sys.stderr)
            raise SystemExit(42)
        out['metadata']['resourceVersion'] = str(int(out['metadata']['resourceVersion'])+1)
        if out.get('kind') == 'Deployment':
            out['metadata']['generation'] += 1
            out['status']['observedGeneration'] += 1
            if state.get('stalled'):
                out['status']['readyReplicas'] = 0
        state['objects'][ref] = out
        if state.get('volume_drift'):
            volume = next(v for v in state['objects'].values() if v.get('kind') == 'PersistentVolume')
            volume['spec']['hostPath']['path'] = '/foreign'
else:
    raise AssertionError(args)
(root/'state.json').write_text(json.dumps(state))
print(json.dumps(out))
