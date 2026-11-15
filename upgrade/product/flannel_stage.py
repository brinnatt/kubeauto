"""Internal Flannel patch transition; never replay the destructive install role.

The outer config + kubecli path owns artifact admission, leases, recovery and
the journal. Its host qualifier checks real CNI/module/8081 prerequisites and its
business gate checks actual traffic; neither is replaced by rollout readiness.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

from upgrade.product.kubernetes_preflight import endpoint


DS = 'daemonset/kube-flannel-ds'
CM = 'configmap/kube-flannel-cfg'
OBJECTS = {DS, CM, 'serviceaccount/flannel', 'clusterrole/flannel', 'clusterrolebinding/flannel'}
PREFIX = 'registry.talkschool.cn:5000/brinnatt/'


class FlannelRefused(ValueError):
    """Inputs, observed ownership or a transition boundary are not admitted."""


class FlannelCommandFailed(FlannelRefused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'Flannel command failed (rc={rc}); retain private diagnostics')


def content(obj):
    meta = obj.get('metadata', {})
    if not meta.get('uid') or meta.get('deletionTimestamp'):
        raise FlannelRefused('Flannel identity is missing or deleting')
    value = {k: deepcopy(v) for k, v in obj.items() if k not in {'metadata', 'status'}}
    value['metadata'] = {k: deepcopy(v) for k, v in meta.items() if k not in
                         {'resourceVersion', 'generation', 'managedFields', 'creationTimestamp'}}
    return value


def healthy(obj):
    meta, status = obj['metadata'], obj.get('status', {})
    desired = status.get('desiredNumberScheduled', 0)
    return (type(desired) is int and desired > 0 and
            status.get('observedGeneration', -1) >= meta.get('generation', 0) and
            status.get('numberMisscheduled', 0) == 0 and
            all(status.get(k) == desired for k in ['currentNumberScheduled',
                'updatedNumberScheduled', 'numberReady', 'numberAvailable']))


def target_objects(reviewed):
    """Apply only the exact reviewed 0.28.4 -> 0.28.10 changes to saved live data."""
    if not isinstance(reviewed, dict) or set(reviewed) != OBJECTS:
        raise FlannelRefused('the complete saved Flannel object set is required')
    result = deepcopy(reviewed)
    for ref, obj in result.items():
        content(obj)
        kind, name = ref.split('/')
        if (obj.get('kind', '').lower() != kind or obj['metadata'].get('name') != name or
                (kind not in ['clusterrole', 'clusterrolebinding'] and
                 obj['metadata'].get('namespace') != 'kube-system')):
            raise FlannelRefused('Flannel resource mapping differs')
    try:
        pod = result[DS]['spec']['template']['spec']
        agent, = pod['containers']
        plugin, installer = pod['initContainers']
        config = json.loads(result[CM]['data']['cni-conf.json'])
        if (result[DS].get('apiVersion') != 'apps/v1' or not pod.get('hostNetwork') or
                pod.get('serviceAccountName') != 'flannel' or
                agent['name'] != 'kube-flannel' or
                agent['image'] != PREFIX+'flannel:v0.28.4' or
                agent['command'] != ['/opt/bin/flanneld'] or
                agent['args'] != ['--ip-masq', '--kube-subnet-mgr'] or
                'livenessProbe' in agent or 'readinessProbe' in agent or
                plugin['name'] != 'install-cni-plugin' or
                plugin['image'] != PREFIX+'flannel-cni-plugin:v1.8.0-flannel1' or
                plugin['command'] != ['cp'] or
                plugin['args'] != ['-f', '/flannel', '/opt/cni/bin/flannel'] or
                installer['name'] != 'install-cni' or
                installer['image'] != PREFIX+'flannel:v0.28.4' or
                installer['command'] != ['cp'] or
                installer['args'] != ['-f', '/etc/kube-flannel/cni-conf.json',
                                       '/etc/cni/net.d/10-flannel.conflist'] or
                config.get('cniVersion') != '0.3.1' or
                [p.get('type') for p in config['plugins']] != ['flannel', 'portmap']):
            raise FlannelRefused('source Flannel pairing or custom installer/probes require review')
        json.loads(result[CM]['data']['net-conf.json'])  # Preserve the exact original network data.
        agent['image'] = installer['image'] = PREFIX+'flannel:v0.28.10'
        plugin['image'] = PREFIX+'flannel-cni-plugin:v1.9.1-flannel3'
        installer['command'] = ['/opt/bin/install-conf']
        installer['args'] = installer['args'][1:]
        agent['args'] += ['--healthz-ip=127.0.0.1', '--healthz-port=8081']
        for name, path, initial, period in [('livenessProbe', '/healthz', 10, 30),
                                            ('readinessProbe', '/readyz', 5, 10)]:
            # Include server defaults so post-write comparison is exact.
            agent[name] = {'httpGet': {'host': '127.0.0.1', 'path': path, 'port': 8081,
                                     'scheme': 'HTTP'}, 'initialDelaySeconds': initial,
                           'periodSeconds': period, 'timeoutSeconds': 1,
                           'successThreshold': 1, 'failureThreshold': 3}
        config['cniVersion'] = '1.0.0'
        result[CM]['data']['cni-conf.json'] = json.dumps(config, indent=2)+'\n'
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, FlannelRefused):
            raise
        raise FlannelRefused('incomplete or malformed saved Flannel source') from None
    return result


class FlannelStage:
    def __init__(self, kubectl, kubeconfig, source, *, timeout=600, progress=None):
        self.kubectl, self.kubeconfig = str(Path(kubectl).resolve()), str(Path(kubeconfig).resolve())
        self.source = deepcopy(source)
        self.backends = [endpoint(b['server']) for b in source.get('api_backends', [])]
        nodes = source.get('nodes', [])
        self.nodes = {n['name']: n['uid'] for n in nodes}
        if (source.get('source') != 'v1.33.6' or source.get('target') != 'v1.34.13' or
                source.get('mutation') != 'none' or
                not source.get('cluster_identity', {}).get('uid') or
                not self.backends or len(set(self.backends)) != len(self.backends) or
                not nodes or len(self.nodes) != len(nodes) or
                len(set(self.nodes.values())) != len(nodes) or
                not all(self.nodes.values()) or type(timeout) is not int or not 30 <= timeout <= 900):
            raise FlannelRefused('exact source identity and bounded timeout are required')
        self.timeout = timeout
        self.progress = progress or (lambda message: print(message, flush=True))

    def command(self, arguments, server=None):
        args = [self.kubectl, '--kubeconfig', self.kubeconfig, '--namespace', 'kube-system',
                '--server', server or self.backends[0], '--request-timeout=10s',
                '--insecure-skip-tls-verify=false', *arguments]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise FlannelCommandFailed(124) from None
        if result.returncode:
            raise FlannelCommandFailed(result.returncode)
        return result.stdout

    def read(self, arguments, server=None):
        try:
            result = json.loads(self.command(arguments, server))
        except ValueError as error:
            if isinstance(error, FlannelRefused):
                raise
            raise FlannelRefused('invalid Flannel observation JSON') from None
        if not isinstance(result, dict):
            raise FlannelRefused('Flannel observation must be an object')
        return result

    def platform_barrier(self):
        for server in self.backends:
            if self.read(['get', '--raw=/version'], server).get('gitVersion') != 'v1.33.6':
                raise FlannelRefused('Flannel edge must finish before the first API update')
            self.command(['get', '--raw=/readyz'], server)
            ns = self.read(['get', 'namespace', 'kube-system', '-o', 'json'], server)
            if (ns.get('metadata', {}).get('uid') != self.source['cluster_identity']['uid'] or
                    ns.get('metadata', {}).get('deletionTimestamp') or
                    ns.get('status', {}).get('phase') != 'Active'):
                raise FlannelRefused('cluster namespace identity changed')
        nodes = self.read(['get', 'nodes', '-o', 'json']).get('items', [])
        if (len(nodes) != len(self.nodes) or
                {n.get('metadata', {}).get('name'): n.get('metadata', {}).get('uid')
                 for n in nodes} != self.nodes or any(
                    n['metadata'].get('deletionTimestamp') or n.get('spec', {}).get('unschedulable') or
                    n.get('status', {}).get('nodeInfo', {}).get('kubeletVersion') != 'v1.33.6' or
                    [c.get('status') for c in n.get('status', {}).get('conditions', [])
                     if c.get('type') == 'Ready'] != ['True'] for n in nodes)):
            raise FlannelRefused('source node identity or health changed')

    def check(self, expected):
        observed = {}
        for ref, wanted in expected.items():
            obj = self.read(['get', ref, '-o', 'json'])
            if content(obj) != content(wanted):
                raise FlannelRefused('Flannel source/configuration/ownership drift: '+ref)
            if ref == DS and obj.get('status', {}).get('desiredNumberScheduled') != len(self.nodes):
                raise FlannelRefused('Flannel does not cover the complete admitted node set')
            observed[ref] = obj
        return observed

    def wait(self, expected):
        deadline = time.monotonic()+self.timeout
        while True:
            if healthy(self.check(expected)[DS]):
                return
            if time.monotonic() >= deadline:
                raise FlannelRefused('Flannel rollout exceeded its bounded window')
            self.progress('Flannel: waiting for observed generation and all source nodes Ready')
            time.sleep(5)

    def run(self, reviewed, verify_hosts, verify_traffic):
        if not callable(verify_hosts) or not callable(verify_traffic):
            raise FlannelRefused('actual host/CNI/port qualification and traffic gates are required')
        target = target_objects(reviewed)
        expected = deepcopy(reviewed)
        states = {}
        for ref in [CM, DS]:
            obj = self.read(['get', ref, '-o', 'json'])
            if content(obj) == content(target[ref]):
                expected[ref] = target[ref]; states[ref] = 'target'
            elif content(obj) == content(reviewed[ref]):
                states[ref] = 'source'
            else:
                raise FlannelRefused('Flannel boundary is outside the saved transition')
        if states[DS] == 'target' and states[CM] != 'target':
            raise FlannelRefused('Flannel DaemonSet advanced before its ConfigMap')
        observed = self.check(expected)
        if states[DS] == 'source' and not healthy(observed[DS]):
            raise FlannelRefused('source Flannel DaemonSet is unhealthy')
        self.platform_barrier(); verify_hosts(); verify_traffic()
        for ref, field in [(CM, 'data'), (DS, 'spec')]:
            self.platform_barrier(); verify_hosts()
            current = self.check(expected)[ref]
            if states[ref] != 'target':
                meta = current['metadata']
                if not meta.get('resourceVersion'):
                    raise FlannelRefused('Flannel patch requires a current resourceVersion')
                operations = [{'op': 'test', 'path': '/metadata/'+key, 'value': meta[key]}
                              for key in ['uid', 'resourceVersion']]
                operations += [{'op': 'test', 'path': '/'+field, 'value': current[field]},
                               {'op': 'replace', 'path': '/'+field, 'value': target[ref][field]}]
                self.command(['patch', ref, '--type=json', '-p', json.dumps(operations)])
                expected[ref] = target[ref]
            self.wait(expected)
            verify_traffic()
            self.check(expected)
            self.progress('UPGRADE_FLANNEL_BOUNDARY_VERIFIED '+ref)
        return {'scope': 'flannel-patch-transition', 'source': 'v0.28.4', 'target': 'v0.28.10',
                'objects': {ref: obj['metadata']['uid'] for ref, obj in self.check(expected).items()},
                'parent_acceptance': False}
