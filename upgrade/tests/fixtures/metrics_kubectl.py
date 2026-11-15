"""Offline process adapter for the real MetricsStage kubectl command boundary."""
from copy import deepcopy
from datetime import datetime, timedelta
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
    print('SENSITIVE_FIXTURE_SENTINEL_4097, never included in the exception', file=sys.stderr)
    raise SystemExit(42)
if args == ['get', '--raw=/version']:
    out = {'gitVersion': state.get('api', 'v1.34.13')}
elif args == ['get', '--raw=/readyz']:
    print('ok')
    raise SystemExit(0)
elif args == ['get', 'nodes', '-o', 'json']:
    out = {'items': state['nodes']}
elif args == ['get', 'namespace', 'kube-system', '-o', 'json']:
    out = {'metadata': {'uid': state.get('cluster_uid', 'cluster-uid')},
           'status': {'phase': 'Active'}}
elif args == ['get', '--raw=/api/v1/namespaces/business/pods/ledger']:
    out = state['pod']
    if state.get('pod_race') and state.get('pod_checks', 0) >= 1:
        out['metadata']['uid'] = 'replacement'
    state['pod_checks'] = state.get('pod_checks', 0)+1
elif args[0] == 'get' and args[1].startswith('--raw=/apis/metrics.k8s.io/v1beta1/'):
    resource = args[1].rsplit('/', 1)[1]
    out = deepcopy(state[resource+'_metrics'])
    if (state['objects']['deployment/metrics-server']['spec']['template']['spec']
            ['containers'][0]['image'].endswith(':v0.9.1') and not state.get('frozen_samples')):
        state['ticks'] = state.get('ticks', 0)+1
        for item in out['items']:
            stamp = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
            item['timestamp'] = (stamp+timedelta(seconds=state['ticks'])).isoformat().replace('+00:00', 'Z')
elif args[0] == 'get':
    out = state['objects'][args[1]]
elif args[0] == 'patch':
    assert args[:4] == ['patch', 'deployment/metrics-server', '--type=json', '-p']
    obj = state['objects'][args[1]]
    ops = json.loads(args[4])
    path = '/spec/template/spec/containers/0/image'
    assert ops == [
        {'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
        {'op': 'test', 'path': path,
         'value': 'registry.talkschool.cn:5000/brinnatt/metrics-server:v0.8.0'},
        {'op': 'replace', 'path': path,
         'value': 'registry.talkschool.cn:5000/brinnatt/metrics-server:v0.9.1'}]
    out = deepcopy(obj)
    out['spec']['template']['spec']['containers'][0]['image'] = ops[-1]['value']
    if args[5:]:
        assert args[5:] == ['--dry-run=server', '-o', 'json']
        if state.get('dry_run_mutation'):
            out['metadata']['annotations'] = {'foreign': 'admission'}
    else:
        if state.get('conflict'):
            raise SystemExit(42)
        out['metadata'].update(resourceVersion='2', generation=2)
        out['status']['observedGeneration'] = 2
        if state.get('stalled'):
            out['status']['readyReplicas'] = 0
        state['objects'][args[1]] = out
        if state.get('post_patch_drift'):
            state['objects']['service/metrics-server']['metadata']['annotations'] = {'foreign': 'change'}
else:
    raise AssertionError(args)
(root/'state.json').write_text(json.dumps(state))
print(json.dumps(out))
