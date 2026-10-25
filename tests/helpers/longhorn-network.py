"""Default Longhorn ingress policies: paired reachability and UID-scoped probes.

No policy, node-network or product setting is patched by this helper. Internal
metric reachability is separate from the Prometheus integration acceptance.
"""
import http.client
import inspect
import ipaddress
import json
import secrets
import socket


NAMESPACE = 'longhorn-system'


def connection(address, port, path=''):
    """A native socket timeout is the only accepted network-denial observation."""
    ipaddress.ip_address(address)
    if type(port) is not int or not 1 <= port <= 65535 or path not in ('', '/v1', '/metrics', '/control'):
        raise ValueError('invalid network probe destination')
    try:
        if not path:
            with socket.create_connection((address, port), timeout=3):
                return {'state': 'connected'}
        client = http.client.HTTPConnection(address, port, timeout=3)
        try:
            client.request('GET', path)
            response = client.getresponse()
            payload = response.read(4 * 1024**2 + 1)
            if response.status != 200 or len(payload) > 4 * 1024**2:
                return {'state': 'invalid-http', 'status': response.status}
            valid = (b'longhorn_' in payload if path == '/metrics' else
                     payload == b'kubeauto-network-control' if path == '/control' else
                     isinstance(json.loads(payload), dict))
            return {'state': 'connected' if valid else 'invalid-payload'}
        finally:
            client.close()
    except TimeoutError:
        return {'state': 'timeout'}
    except (OSError, http.client.HTTPException, ValueError) as error:
        # ConnectionRefused/NoRoute, invalid JSON and broken clients do not
        # establish policy isolation. Do not expose response bodies or URLs.
        return {'state': 'error', 'type': type(error).__name__}


def policy_identity(lab):
    return {obj['metadata']['name']: {'uid': obj['metadata']['uid'], 'spec': obj['spec']}
            for obj in lab.get('networkpolicies', namespace=NAMESPACE)['items']}


def ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and
                any(row.get('type') == 'Ready' and row.get('status') == 'True'
                    for row in pod.get('status', {}).get('conditions', [])))


class Probe:
    def __init__(self, lab):
        self.lab = lab
        self.record = lab.DIRECTORY / 'longhorn-network-probes.json'

    def check(self):
        row = json.loads(self.record.read_text())
        state = self.lab.owner()
        namespace = self.lab.get('namespace', NAMESPACE)
        if (row['run_id'] != state['run_id'] or row['namespace_uid'] != state['namespace_uid'] or
                not namespace or namespace['metadata']['uid'] != row['namespace_uid'] or
                policy_identity(self.lab) != row['policies']):
            raise ValueError('network probe baseline identity or policy changed')
        for name, original in row['pods'].items():
            pod = self.lab.get('pod', name, NAMESPACE)
            if not pod:
                continue
            container = pod['spec']['containers'][0]
            if (pod['metadata'].get('ownerReferences') or pod['metadata'].get('labels', {}) != original['labels'] or
                    pod['metadata'].get('annotations', {}).get('kubeauto.io/network-probe') != original['nonce'] or
                    container['image'] != original['image'] or container['command'] != original['command'] or
                    (original['uid'] and pod['metadata']['uid'] != original['uid'])):
                raise ValueError('network probe Pod was replaced, adopted or relabeled')
            if not original['uid']:
                # Only a Pod carrying the random create intent can recover
                # an interrupted response; a matching name alone is refused.
                original['uid'] = pod['metadata']['uid']
                self.lab.write_private(self.record, row)
        return row

    def cleanup(self):
        if self.record.is_symlink():
            raise ValueError('network probe journal is a symlink')
        if not self.record.exists():
            return
        row = self.check()
        # Validate every identity before deleting any of the known Pods.
        for name in row['pods']:
            pod = self.lab.get('pod', name, NAMESPACE)
            if pod:
                self.lab.remove(pod)
        self.lab.wait('network-system-probes-clean', 180, lambda:
                      all(self.lab.get('pod', name, NAMESPACE) is None for name in row['pods']))
        self.check()
        self.record.unlink()
        print('LONGHORN_NETWORK_PROBES_CLEAN_PASS', flush=True)

    def pod(self, namespace, name, labels=None, *, control=False):
        node = 'longhorn-node-' + self.lab.WORKERS[1].rsplit('.', 1)[1]
        pod = self.lab.client_pod(namespace, name, 'unused', node)
        pod['metadata']['labels'] = labels or {}
        pod['spec'].pop('volumes')
        pod['spec']['automountServiceAccountToken'] = False
        container = pod['spec']['containers'][0]
        container.pop('volumeMounts')
        if control:
            container['command'] = ['python3', '-u', '-c',
                'from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\n'
                'class Handler(BaseHTTPRequestHandler):\n'
                ' def do_GET(self):\n'
                '  self.send_response(200);self.end_headers();self.wfile.write(b"kubeauto-network-control")\n'
                ' def log_message(self,*args): pass\n'
                'ThreadingHTTPServer(("0.0.0.0",18080),Handler).serve_forever()\n']
        return pod

    def create_system(self, obj):
        row = self.check(); name = obj['metadata']['name']
        if name in row['pods'] or self.lab.get('pod', name, NAMESPACE):
            raise ValueError('network probe name is not unused')
        nonce = secrets.token_hex(24)
        obj['metadata']['annotations'] = {'kubeauto.io/network-probe': nonce}
        container = obj['spec']['containers'][0]
        row['pods'][name] = {'uid': None, 'labels': obj['metadata']['labels'], 'nonce': nonce,
                             'image': container['image'], 'command': container['command']}
        self.lab.write_private(self.record, row)
        result = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        observed = self.check()
        if observed['pods'][name]['uid'] != result['metadata']['uid']:
            raise ValueError('network create response differs from original intent')
        return result

    def request(self, namespace, client, address, port, path=''):
        code = ('import http.client,ipaddress,json,socket,sys\n' + inspect.getsource(connection) +
                '\nprint(json.dumps(connection(**json.load(sys.stdin)),sort_keys=True))\n')
        result = self.lab.kubectl('-n', namespace, 'exec', '-i', client, '--', 'python3', '-c', code,
            stdin=json.dumps({'address': address, 'port': port, 'path': path}), timeout=30)
        row = json.loads(result.stdout)
        if not isinstance(row, dict) or row.get('state') not in ('connected', 'timeout', 'error', 'invalid-http', 'invalid-payload'):
            raise ValueError('invalid network probe result')
        return row

    def expect(self, namespace, client, address, port, path='', *, state='connected'):
        result = self.request(namespace, client, address, port, path)
        if result != {'state': state}:
            raise ValueError('network reachability differs: client=' + client + ' expected=' + state +
                             ' observed=' + json.dumps(result, sort_keys=True))

    def await_denial(self, client, allowed, control, target):
        """Allow bounded CNI policy propagation, keeping both controls live."""
        def converged():
            self.check()
            self.expect(NAMESPACE, allowed, target['address'], target['port'])
            self.expect(client['namespace'], client['name'], control, 18080, '/control')
            result = self.request(client['namespace'], client['name'], target['address'], target['port'])
            if result not in ({'state': 'connected'}, {'state': 'timeout'}):
                raise ValueError('policy convergence encountered a different network failure')
            return result == {'state': 'timeout'}
        self.lab.wait('network-policy-converged-' + client['name'] + '-' + target['name'], 60, converged)

    def targets(self):
        expected_nodes = {'longhorn-node-' + ip.rsplit('.', 1)[1] for ip in self.lab.WORKERS}
        pods = self.lab.get('pods', namespace=NAMESPACE)['items']
        groups = {}
        for label, value, key, port in (('app', 'longhorn-manager', 'manager', 9500),
                                      ('longhorn.io/component', 'instance-manager', 'instance-manager', 8500)):
            selected = [p for p in pods if p['metadata'].get('labels', {}).get(label) == value and ready(p)]
            if {p['spec']['nodeName'] for p in selected} != expected_nodes or len(selected) != len(expected_nodes):
                raise ValueError('network target placement or readiness differs')
            groups[key] = [{'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
                            'address': p['status']['podIP'], 'port': port} for p in selected]
        return groups

    def verify_ha(self, journal):
        name = 'network-dry-run-' + secrets.token_hex(8)
        obj = {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Snapshot',
               'metadata': {'name': name, 'namespace': NAMESPACE},
               'spec': {'volume': journal['volume'], 'createSnapshot': True, 'labels': {}}}
        if self.lab.get('snapshots.longhorn.io', name, NAMESPACE):
            raise ValueError('network admission diagnostic name exists')
        for address in self.lab.NODES[:3]:
            result = self.lab.kubectl('--server=https://' + address + ':6443', 'create', '--dry-run=server',
                '-f', '-', '-o', 'json', stdin=json.dumps(obj), timeout=45)
            returned = json.loads(result.stdout)
            if returned.get('spec', {}).get('volume') != journal['volume'] or returned.get('metadata', {}).get('name') != name:
                raise ValueError('HA API-server admission diagnostic response differs')
            print('LONGHORN_NETWORK_HA_ADMISSION_VERIFIED server=' + address, flush=True)
        if self.lab.get('snapshots.longhorn.io', name, NAMESPACE):
            raise ValueError('network dry-run diagnostic persisted a Snapshot')

    def verify(self, journal, create):
        state = self.lab.owner()
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous network probes need scoped recovery')
        policies = policy_identity(self.lab)
        if not {'longhorn-manager', 'instance-manager', 'longhorn-webhook'} <= set(policies):
            raise ValueError('required default ingress policies are absent')
        self.lab.write_private(self.record, {'run_id': state['run_id'], 'namespace_uid': state['namespace_uid'],
                                            'policies': policies, 'pods': {}})
        with self.lab.scoped_cleanup(self.cleanup):
            suffix = secrets.token_hex(8)
            allowed_labels = {'longhorn.io/job-task': 'kubeauto-network-probe',
                              'longhorn.io/component': 'backing-image-data-source'}
            allowed = self.create_system(self.pod(NAMESPACE, 'network-allowed-' + suffix, allowed_labels))
            denied = self.create_system(self.pod(NAMESPACE, 'network-denied-' + suffix))
            foreign = create(self.pod(journal['namespace'], 'network-foreign', allowed_labels))
            control = create(self.pod(journal['namespace'], 'network-control', control=True))
            clients = (allowed, denied, foreign)
            for pod in (*clients, control):
                meta = pod['metadata']
                self.lab.wait('network-probe-ready-' + meta['name'], 180,
                    lambda meta=meta: ready(self.lab.get('pod', meta['name'], meta['namespace'])))
            control_address = self.lab.get('pod', 'network-control', journal['namespace'])['status']['podIP']
            targets = self.targets()
            for meta in (p['metadata'] for p in clients):
                self.lab.wait('network-positive-control-' + meta['name'], 60,
                    lambda meta=meta: self.request(meta['namespace'], meta['name'], control_address, 18080, '/control') == {'state': 'connected'})
            for target in targets['manager']:
                for path in ('/v1', '/metrics'):
                    self.expect(NAMESPACE, allowed['metadata']['name'], target['address'], target['port'], path)
            for group in targets.values():
                for target in group:
                    for pod in (denied, foreign):
                        self.await_denial(pod['metadata'], allowed['metadata']['name'], control_address, target)
            refusals = 0
            for attempt in range(3):
                for group in targets.values():
                    for target in group:
                        self.check()
                        self.expect(NAMESPACE, allowed['metadata']['name'], target['address'], target['port'])
                        for pod in (denied, foreign):
                            meta = pod['metadata']
                            self.expect(meta['namespace'], meta['name'], control_address, 18080, '/control')
                            self.expect(meta['namespace'], meta['name'], target['address'], target['port'], state='timeout')
                            refusals += 1
                        self.expect(NAMESPACE, allowed['metadata']['name'], target['address'], target['port'])
                print('LONGHORN_NETWORK_DENIAL_ROUND_VERIFIED round=' + str(attempt + 1), flush=True)
            if self.targets() != targets:
                raise ValueError('network target identities changed during paired probes')
            self.verify_ha(journal)
            print('LONGHORN_NETWORK_POLICY_OBSERVATION ' + json.dumps({'targets': targets,
                'round_timeout_observations': refusals, 'rounds': 3,
                'metrics_boundary': 'same-namespace authorized client; external Prometheus acceptance is separate',
                'webhook_boundary': 'default empty source CIDRs; no webhook source-denial claim'}, sort_keys=True), flush=True)
