"""Internal, image-only DNS transition after the platform's target barrier.

The caller owns frozen artifact admission, cluster lease, recovery and journal.
Reviewed objects are the private, admitted source observation, including API
defaults. This stage never applies an installation manifest or deletes objects.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

from upgrade.product.kubernetes_preflight import endpoint


PREFIX = 'registry.talkschool.cn:5000/brinnatt/'
CORE = 'deployment/coredns'
CACHE = 'daemonset/node-local-dns'
EDGES = {CORE: ('coredns', PREFIX + 'coredns:1.12.4', PREFIX + 'coredns:1.15.0'),
         CACHE: ('node-cache', PREFIX + 'k8s-dns-node-cache:1.26.4',
                 PREFIX + 'k8s-dns-node-cache:1.26.8')}
CORE_OBJECTS = {CORE, 'configmap/coredns', 'service/kube-dns', 'serviceaccount/coredns',
                'clusterrole/system:coredns', 'clusterrolebinding/system:coredns'}
CACHE_OBJECTS = {CACHE, 'configmap/node-local-dns', 'service/kube-dns-upstream',
                 'service/node-local-dns', 'serviceaccount/node-local-dns'}


class DNSRefused(ValueError):
    """A DNS boundary is unknown, stale or did not converge."""


class DNSCommandFailed(DNSRefused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'DNS command failed (rc={rc}); retain private runner diagnostics')


def identity_and_content(obj):
    """Retain server-defaulted content, excluding only volatile API metadata."""
    meta = obj.get('metadata', {})
    if not meta.get('uid') or meta.get('deletionTimestamp'):
        raise DNSRefused('DNS object identity is absent or deleting')
    value = {key: deepcopy(item) for key, item in obj.items() if key not in {'metadata', 'status'}}
    value['metadata'] = {key: deepcopy(meta[key]) for key in ['uid', 'name', 'namespace', 'labels']
                         if key in meta}
    return value


def healthy(obj):
    meta, spec, status = obj['metadata'], obj['spec'], obj.get('status', {})
    if status.get('observedGeneration', -1) < meta.get('generation', 0):
        return False
    if obj['kind'] == 'Deployment':
        desired = spec.get('replicas', 1)
        keys = ['replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas']
    else:
        desired = status.get('desiredNumberScheduled', 0)
        keys = ['currentNumberScheduled', 'updatedNumberScheduled', 'numberReady', 'numberAvailable']
        if status.get('numberMisscheduled', 0) != 0:
            return False
    return (type(desired) is int and desired > 0 and
            all(status.get(key, 0) == desired for key in keys) and
            status.get('unavailableReplicas', 0) == 0)


class DNSStage:
    def __init__(self, kubectl, kubeconfig, api_backends, nodes, *, timeout=600, progress=None,
                 target_version='v1.34.13', originally_schedulable=None):
        if target_version not in ['v1.34.13', 'v1.35.10']:
            raise DNSRefused('exact reviewed platform target required')
        self.target_version = target_version
        self.schedulable = ({name: True for name in nodes} if originally_schedulable is None
                            else deepcopy(originally_schedulable))
        if set(self.schedulable) != set(nodes) or any(type(v) is not bool for v in self.schedulable.values()):
            raise DNSRefused('complete original scheduling state required')
        self.kubectl = str(Path(kubectl).resolve())
        self.kubeconfig = str(Path(kubeconfig).resolve())
        self.backends = [endpoint(server) for server in api_backends]
        self.nodes = deepcopy(nodes)  # Complete name -> UID map from admitted inventory.
        if (not self.backends or len(self.backends) != len(set(self.backends)) or
                not isinstance(nodes, dict) or not nodes or
                not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in nodes.items()) or
                type(timeout) is not int or not 30 <= timeout <= 900):
            raise DNSRefused('complete API/node identities and a bounded timeout are required')
        self.timeout = timeout
        self.progress = progress or (lambda message: print(message, flush=True))

    def command(self, arguments, server=None):
        args = [self.kubectl, '--kubeconfig', self.kubeconfig, '--namespace', 'kube-system',
                '--server', server or self.backends[0], '--request-timeout=10s',
                '--insecure-skip-tls-verify=false', *arguments]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise DNSCommandFailed(124) from None
        if result.returncode:
            raise DNSCommandFailed(result.returncode)
        return result.stdout

    def read(self, arguments, server=None):
        try:
            value = json.loads(self.command(arguments, server))
        except ValueError as error:
            if isinstance(error, DNSRefused):
                raise
            raise DNSRefused('DNS observation is not valid JSON') from None
        if not isinstance(value, dict):
            raise DNSRefused('DNS observation must be an object')
        return value

    def platform_barrier(self):
        for backend in self.backends:
            if self.read(['get', '--raw=/version'], backend).get('gitVersion') != self.target_version:
                raise DNSRefused('every API backend must reach the exact target before DNS')
            self.command(['get', '--raw=/readyz'], backend)
        nodes = self.read(['get', 'nodes', '-o', 'json']).get('items')
        if not isinstance(nodes, list) or len(nodes) != len(self.nodes):
            raise DNSRefused('node inventory differs')
        names = []
        for node in nodes:
            meta, status = node.get('metadata', {}), node.get('status', {})
            name = meta.get('name')
            if (name not in self.nodes or meta.get('uid') != self.nodes[name] or
                    meta.get('deletionTimestamp') or
                    node.get('spec', {}).get('unschedulable', False) != (not self.schedulable[name]) or
                    status.get('nodeInfo', {}).get('kubeletVersion') != self.target_version or
                    [c.get('status') for c in status.get('conditions', [])
                     if c.get('type') == 'Ready'] != ['True']):
                raise DNSRefused('node identity, target version, readiness or scheduling differs')
            names.append(name)
        if len(set(names)) != len(names):
            raise DNSRefused('duplicate node identity')

    def check_objects(self, expected):
        observed = {}
        for ref, wanted in expected.items():
            obj = self.read(['get', ref, '-o', 'json'])
            if identity_and_content(obj) != identity_and_content(wanted):
                raise DNSRefused('DNS object identity or reviewed configuration drift: ' + ref)
            observed[ref] = obj
        return observed

    def wait(self, ref, expected):
        deadline = time.monotonic() + self.timeout
        while True:
            observed = self.check_objects(expected)
            if healthy(observed[ref]):
                return
            if time.monotonic() >= deadline:
                raise DNSRefused('DNS rollout exceeded the fixed timeout: ' + ref)
            self.progress('DNS: waiting for observed generation and all replicas of ' + ref)
            time.sleep(5)

    def run(self, reviewed, verify_queries):
        """Roll out reviewed images; caller's real query gate must raise on failure.

        The same saved source observation is required on resume. A healthy target
        workload is reverified and not patched again. No automatic downgrade is
        attempted after a failed rollout or query.
        """
        if (not isinstance(reviewed, dict) or set(reviewed) not in
                (CORE_OBJECTS, CORE_OBJECTS | CACHE_OBJECTS) or not callable(verify_queries)):
            raise DNSRefused('complete reviewed DNS objects and a business query gate are required')
        expected = deepcopy(reviewed)
        refs = [CORE] + ([CACHE] if CACHE in reviewed else [])
        for ref in refs:
            wanted = reviewed[ref]
            container, source, _ = EDGES[ref]
            containers = wanted.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
            if (wanted.get('apiVersion') != 'apps/v1' or
                    wanted.get('kind') != ('Deployment' if ref == CORE else 'DaemonSet') or
                    wanted.get('metadata', {}).get('namespace') != 'kube-system' or
                    wanted.get('metadata', {}).get('name') != ref.split('/')[1] or
                    len(containers) != 1 or containers[0].get('name') != container or
                    containers[0].get('image') not in EDGES[ref][1:]):
                raise DNSRefused('reviewed DNS workload differs from the exact source')
        # Only the image may already be target on an interrupted invocation.
        for ref in refs:
            obj = self.read(['get', ref, '-o', 'json'])
            image = obj.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0].get('image')
            if image not in EDGES[ref][1:]:
                raise DNSRefused('DNS image is outside the reviewed transition')
            if (reviewed[ref]['spec']['template']['spec']['containers'][0]['image'] == EDGES[ref][2]
                    and image != EDGES[ref][2]):
                raise DNSRefused('an admitted target DNS workload regressed')
            expected[ref]['spec']['template']['spec']['containers'][0]['image'] = image
        observed = self.check_objects(expected)
        for ref in refs:
            if (observed[ref]['spec']['template']['spec']['containers'][0]['image'] == EDGES[ref][1]
                    and not healthy(observed[ref])):
                raise DNSRefused('source DNS workload is unhealthy: ' + ref)
        # An advanced cache is only valid after the CoreDNS target boundary.
        if (CACHE in expected and expected[CACHE]['spec']['template']['spec']['containers'][0]['image'] == EDGES[CACHE][2]
                and expected[CORE]['spec']['template']['spec']['containers'][0]['image'] != EDGES[CORE][2]):
            raise DNSRefused('NodeLocal advanced before CoreDNS')
        self.platform_barrier()
        for ref in refs:
            # A pre-existing unhealthy source is not repaired by changing its image.
            current = observed[ref]
            if not healthy(current):
                if current['spec']['template']['spec']['containers'][0]['image'] == EDGES[ref][1]:
                    raise DNSRefused('source DNS workload is unhealthy: ' + ref)
                self.wait(ref, expected)
            self.platform_barrier()
            current = self.check_objects(expected)[ref]
            _, source, target = EDGES[ref]
            if current['spec']['template']['spec']['containers'][0]['image'] != target:
                meta = current['metadata']
                if not meta.get('resourceVersion'):
                    raise DNSRefused('DNS patch requires the current resourceVersion')
                operations = [{'op': 'test', 'path': '/metadata/' + key, 'value': meta[key]}
                              for key in ['uid', 'resourceVersion']]
                path = '/spec/template/spec/containers/0/image'
                operations += [{'op': 'test', 'path': path, 'value': source},
                               {'op': 'replace', 'path': path, 'value': target}]
                self.command(['patch', ref, '--type=json', '-p', json.dumps(operations)])
                expected[ref]['spec']['template']['spec']['containers'][0]['image'] = target
            self.wait(ref, expected)
            # Rollout readiness is not a DNS business result. Query refusal stops
            # the dependent cache stage, with all current objects left for diagnosis.
            verify_queries(ref)
            self.check_objects(expected)
            self.progress('UPGRADE_DNS_WORKLOAD_VERIFIED ' + ref)
            observed = self.check_objects(expected)
        return {'scope': 'dns-image-rollout-and-caller-query-handoff',
                'objects': {ref: obj['metadata']['uid'] for ref, obj in observed.items()},
                'images': {ref: EDGES[ref][2] for ref in refs}, 'parent_acceptance': False}
