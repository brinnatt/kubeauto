"""Serial, local-only kubectl fixture for the Calico product stage contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sys


root = Path(sys.argv[0]).parent
state = json.loads((root/'state.json').read_text())
args = sys.argv[1:]
assert args[:4] == ['--kubeconfig', str(root/'kubeconfig'), '--namespace', 'kube-system']
assert args[4] == '--server' and args[6:8] == [
    '--request-timeout=10s', '--insecure-skip-tls-verify=false']
server, args = args[5], args[8:]
with (root/'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args)+'\n')
if args == ['get', '--raw=/version']:
    result = {'gitVersion': state.get('apis', {}).get(server, state['platform'])}
elif args == ['get', '--raw=/readyz']:
    print('ok')
    raise SystemExit(0)
elif args == ['get', 'namespace', 'kube-system', '-o', 'json']:
    result = {'metadata': {'uid': state.get('cluster_uid', 'cluster-uid')},
              'status': {'phase': 'Active'}}
elif args == ['get', 'nodes', '-o', 'json']:
    result = {'items': state['nodes']}
elif args[0] == 'get':
    if args[1] not in state['objects'] and '--ignore-not-found' in args:
        raise SystemExit(0)
    result = state['objects'][args[1]]
elif args[0] == 'create':
    assert args[:3] == ['create', '-f', '-']
    result = json.load(sys.stdin)
    ref = result['kind'].lower()+'/'+result['metadata']['name']
    if ref in state['objects']:
        raise SystemExit(1)
    result['metadata'].update(uid=ref+'-new-uid', resourceVersion='1', generation=1)
    if '--dry-run=server' not in args:
        state['objects'][ref] = result
elif args[0] == 'patch':
    ref = args[1]
    assert args[2:4] == ['--type=json', '-p']
    ops = json.loads(args[4])
    assert [op['op'] for op in ops] == ['test', 'test', 'test', 'replace']
    assert ops[0]['path'] == '/metadata/uid' and ops[1]['path'] == '/metadata/resourceVersion'
    assert ops[2]['path'] == ops[3]['path'] and ops[2]['path'] in ['/spec', '/rules', '/data']
    result = deepcopy(state['objects'][ref])
    for op in ops[:3]:
        current = result
        for key in op['path'].strip('/').split('/'):
            current = current[key]
        assert current == op['value']
    result[ops[3]['path'][1:]] = ops[3]['value']
    template_changed = (result.get('spec', {}).get('template') !=
                        state['objects'][ref].get('spec', {}).get('template'))
    if result['kind'] == 'DaemonSet' and template_changed:
        key = 'deprecated.daemonset.template.generation'
        annotations = result['metadata'].setdefault('annotations', {})
        annotations[key] = str(int(annotations.get(key, '1')) + 1)
    # Exact core/v1 default, including newly added hostPath volumes. The
    # returned pointer to HostPathUnset serializes as type:"", not omission.
    for volume in result.get('spec', {}).get('template', {}).get('spec', {}).get('volumes', []):
        if 'hostPath' in volume:
            volume['hostPath'].setdefault('type', '')
    if args[5:]:
        assert args[5:] == ['--dry-run=server', '-o', 'json']
        if state.get('admission_mutation') == ref:
            result['metadata']['annotations'] = {'foreign': 'injected'}
    else:
        if state.get('conflict') == ref:
            raise SystemExit(42)
        result['metadata']['resourceVersion'] = '2'
        result['metadata']['generation'] = 2
        if result['kind'] == 'Deployment' and template_changed:
            # Deployment controller acts after persistence, never in dry-run.
            key = 'deployment.kubernetes.io/revision'
            annotations = result['metadata'].setdefault('annotations', {})
            annotations[key] = str(int(annotations.get(key, '1')) + 1)
        if result['kind'] in ['DaemonSet', 'Deployment']:
            result['status']['observedGeneration'] = 2
            if state.get('stalled') == ref:
                result['status']['numberReady'] = result['status']['readyReplicas'] = 0
        state['objects'][ref] = result
        if state.get('drift_after') == ref:
            state['objects']['secret/calico-etcd-secrets']['data']['etcd-key'] = 'foreign-key'
else:
    raise AssertionError(args)
(root/'state.json').write_text(json.dumps(state))
print(json.dumps(result))
