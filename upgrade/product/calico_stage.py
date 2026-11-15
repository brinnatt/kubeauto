"""Internal etcdv3 Calico edges; never replay the certificate/CNI install role.

The outer config + kubecli path owns the lock, immutable source observation,
artifact admission, leases and coordinated recovery. Callbacks must qualify the
real hosts, datastore/policies, traffic and matching calicoctl. This module has
no standalone admission entry point and its result is not parent acceptance.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import re
import time

import jinja2
import yaml

from upgrade.product.kubernetes_preflight import endpoint


NODE = 'daemonset/calico-node'
CONTROLLER = 'deployment/calico-kube-controllers'
SECRET = 'secret/calico-etcd-secrets'
EDGES = {('v3.28.4', 'v3.31.7'): 'v1.33.6',
         ('v3.31.7', 'v3.32.4'): 'v1.34.13'}
OWNER = 'kubeauto.io/upgrade-owner'


class CalicoRefused(ValueError):
    """A reviewed identity, prerequisite or convergence boundary differs."""


class CalicoCommandFailed(CalicoRefused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'Calico command failed (rc={rc}); retain private diagnostics')


def content(obj):
    meta = obj.get('metadata', {})
    if not meta.get('uid') or meta.get('deletionTimestamp'):
        raise CalicoRefused('Calico object is unidentified or deleting')
    value = {k: deepcopy(v) for k, v in obj.items() if k not in {'metadata', 'status'}}
    value['metadata'] = {k: deepcopy(v) for k, v in meta.items() if k not in
                         {'resourceVersion', 'generation', 'managedFields', 'creationTimestamp'}}
    return value


def created_content(obj):
    copy = deepcopy(obj)
    copy.setdefault('metadata', {})['uid'] = 'dry-run-identity'
    value = content(copy)
    value['metadata'].pop('uid')
    return value


def named(items):
    return (bool(items) and all(isinstance(x, dict) and isinstance(x.get('name'), str)
                               for x in items) and
            len({x['name'] for x in items}) == len(items))


def matches(live, declared):
    """Allow API defaults in maps; never silently accept extra list members."""
    if isinstance(declared, dict):
        return isinstance(live, dict) and all(k in live and matches(live[k], v)
                                             for k, v in declared.items())
    if isinstance(declared, list):
        if not isinstance(live, list) or len(live) != len(declared):
            return False
        if named(declared) and named(live):
            values = {x['name']: x for x in live}
            return all(x['name'] in values and matches(values[x['name']], x) for x in declared)
        return all(matches(a, b) for a, b in zip(live, declared))
    return type(live) is type(declared) and live == declared


def delta(live, before, after):
    """Transplant the reviewed declaration delta while retaining API defaults."""
    if before == after:
        return deepcopy(live)
    if isinstance(before, dict) and isinstance(after, dict):
        result = deepcopy(live)
        for key in before.keys() - after.keys():
            del result[key]
        for key, value in after.items():
            if key in before:
                result[key] = delta(live[key], before[key], value)
            else:
                if key in live and live[key] != value:
                    raise CalicoRefused('new Calico field conflicts with existing configuration')
                result[key] = deepcopy(value)
        return result
    if isinstance(before, list) and isinstance(after, list) and named(before) and named(after):
        old = {x['name']: x for x in before}
        current = {x['name']: x for x in live}
        return [delta(current[x['name']], old[x['name']], x) if x['name'] in old
                else deepcopy(x) for x in after]
    return deepcopy(after)


def manifests(variables, version):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(
        Path(__file__).resolve().parents[2]/'roles/calico/templates'),
        undefined=jinja2.StrictUndefined, trim_blocks=True)
    text = env.get_template(f'calico-{version.rsplit(".", 1)[0]}.yaml.j2').render(
        **{**variables, 'calico_ver': version})
    return {obj['kind'].lower()+'/'+obj['metadata']['name']: obj
            for obj in yaml.safe_load_all(text) if obj}


def new_container_defaults(before, desired):
    """Kubernetes v1 core defaults for newly added, non-latest init images.

    Existing defaults are copied from the source. Unknown admission mutations
    are refused by the exact server dry-run comparison, not silently adopted.
    """
    if desired['kind'] != 'DaemonSet':
        return
    names = {c['name'] for c in before['spec']['template']['spec']['initContainers']}
    for container in desired['spec']['template']['spec']['initContainers']:
        if container['name'] not in names:
            for key, value in {'imagePullPolicy': 'IfNotPresent', 'resources': {},
                    'terminationMessagePath': '/dev/termination-log',
                    'terminationMessagePolicy': 'File'}.items():
                container.setdefault(key, value)


def healthy(obj, node_count):
    meta, spec, status = obj['metadata'], obj['spec'], obj.get('status', {})
    if status.get('observedGeneration', -1) < meta.get('generation', 0):
        return False
    if obj['kind'] == 'DaemonSet':
        desired = node_count
        keys = ['desiredNumberScheduled', 'currentNumberScheduled',
                'updatedNumberScheduled', 'numberReady', 'numberAvailable']
        if status.get('numberMisscheduled', 0) != 0:
            return False
    else:
        desired = spec.get('replicas', 1)
        keys = ['replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas']
        if status.get('unavailableReplicas', 0) != 0:
            return False
    return type(desired) is int and desired > 0 and all(status.get(k) == desired for k in keys)


class CalicoStage:
    def __init__(self, kubectl, kubeconfig, source, variables, edge, *, run_id,
                 timeout=600, progress=None):
        if (tuple(edge) not in EDGES or not isinstance(run_id, str) or
                not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}', run_id)):
            raise CalicoRefused('an exact reviewed Calico edge and outer run identity are required')
        self.edge, self.platform = tuple(edge), EDGES[tuple(edge)]
        self.before = manifests(variables, edge[0])
        self.after = manifests(variables, edge[1])
        self.new = sorted(self.after.keys()-self.before.keys())
        if self.new not in [[], ['clusterrole/calico-tier-getter',
                                'clusterrolebinding/calico-tier-getter']]:
            raise CalicoRefused('unreviewed Calico object additions')
        owner = hashlib.sha256(json.dumps([run_id, source.get('cluster_identity'), self.after],
                                         sort_keys=True).encode()).hexdigest()[:32]
        for ref in self.new:
            self.after[ref]['metadata']['labels'] = {OWNER: owner}
        self.source = deepcopy(source)
        self.backends = [endpoint(b['server']) for b in source.get('api_backends', [])]
        nodes = source.get('nodes', [])
        self.nodes = {n['name']: n['uid'] for n in nodes}
        if (source.get('source') != 'v1.33.6' or source.get('target') != 'v1.34.13' or
                source.get('mutation') != 'none' or
                source.get('cluster_identity', {}).get('namespace') != 'kube-system' or
                not source.get('cluster_identity', {}).get('uid') or
                not self.backends or len(set(self.backends)) != len(self.backends) or
                not nodes or len(self.nodes) != len(nodes) or not all(self.nodes.values()) or
                len(set(self.nodes.values())) != len(nodes) or
                type(timeout) is not int or not 30 <= timeout <= 900):
            raise CalicoRefused('exact cluster/node identity and bounded timeout are required')
        self.kubectl, self.kubeconfig = str(Path(kubectl).resolve()), str(Path(kubeconfig).resolve())
        self.timeout = timeout
        self.progress = progress or (lambda message: print(message, flush=True))

    def command(self, args, server=None, *, payload=None):
        argv = [self.kubectl, '--kubeconfig', self.kubeconfig, '--namespace', 'kube-system',
                '--server', server or self.backends[0], '--request-timeout=10s',
                '--insecure-skip-tls-verify=false', *args]
        try:
            result = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise CalicoCommandFailed(124) from None
        if result.returncode:
            raise CalicoCommandFailed(result.returncode)
        return result.stdout

    def read(self, args, server=None, *, payload=None):
        try:
            obj = json.loads(self.command(args, server, payload=payload))
        except ValueError as error:
            if isinstance(error, CalicoRefused):
                raise
            raise CalicoRefused('invalid Calico observation JSON') from None
        if not isinstance(obj, dict):
            raise CalicoRefused('Calico observation must be an object')
        return obj

    def barrier(self):
        for server in self.backends:
            if self.read(['get', '--raw=/version'], server).get('gitVersion') != self.platform:
                raise CalicoRefused('Calico edge is outside its exact platform barrier')
            self.command(['get', '--raw=/readyz'], server)
            namespace = self.read(['get', 'namespace', 'kube-system', '-o', 'json'], server)
            if (namespace.get('metadata', {}).get('uid') != self.source['cluster_identity']['uid'] or
                    namespace.get('metadata', {}).get('deletionTimestamp') or
                    namespace.get('status', {}).get('phase') != 'Active'):
                raise CalicoRefused('Calico API backend cluster identity differs')
        nodes = self.read(['get', 'nodes', '-o', 'json']).get('items', [])
        if (len(nodes) != len(self.nodes) or
                {n.get('metadata', {}).get('name'): n.get('metadata', {}).get('uid')
                 for n in nodes} != self.nodes or any(
                    n['metadata'].get('deletionTimestamp') or n.get('spec', {}).get('unschedulable') or
                    n.get('status', {}).get('nodeInfo', {}).get('kubeletVersion') != self.platform or
                    [c.get('status') for c in n.get('status', {}).get('conditions', [])
                     if c.get('type') == 'Ready'] != ['True'] for n in nodes)):
            raise CalicoRefused('complete Calico node identity/health/platform barrier differs')

    def check(self, expected):
        observed = {}
        for ref, wanted in expected.items():
            obj = self.read(['get', ref, '-o', 'json'])
            if content(obj) != content(wanted):
                raise CalicoRefused('Calico configuration, Secret or ownership drift: '+ref)
            observed[ref] = obj
        return observed

    @staticmethod
    def patch(current, desired, field):
        meta = current['metadata']
        if not meta.get('resourceVersion'):
            raise CalicoRefused('Calico JSON patch requires a current resourceVersion')
        return [{'op': 'test', 'path': '/metadata/'+k, 'value': meta[k]}
                for k in ['uid', 'resourceVersion']] + [
                    {'op': 'test', 'path': '/'+field, 'value': current[field]},
                    {'op': 'replace', 'path': '/'+field, 'value': desired[field]}]

    def wait(self, expected):
        deadline = time.monotonic()+self.timeout
        while True:
            objects = self.check(expected)
            if all(healthy(objects[ref], len(self.nodes)) for ref in [NODE, CONTROLLER]):
                return
            if time.monotonic() >= deadline:
                raise CalicoRefused('Calico rollout exceeded its bounded window')
            self.progress('Calico: waiting for observed generations and complete node/controller readiness')
            time.sleep(5)

    def run(self, reviewed, verify_hosts, verify_datastore, verify_traffic, install_client):
        if not all(callable(g) for g in [verify_hosts, verify_datastore, verify_traffic, install_client]):
            raise CalicoRefused('real host/datastore/traffic/client handoffs are required')
        if not isinstance(reviewed, dict) or set(reviewed) != set(self.before) | {SECRET}:
            raise CalicoRefused('the complete private source object set including Secret is required')
        desired = deepcopy(reviewed)
        for ref, declared in self.before.items():
            if not matches(reviewed[ref], declared):
                raise CalicoRefused('source Calico declaration/customization differs: '+ref)
            desired[ref] = delta(reviewed[ref], declared, self.after[ref])
            new_container_defaults(reviewed[ref], desired[ref])
        secret = reviewed[SECRET]
        if (secret.get('kind') != 'Secret' or secret['metadata'].get('namespace') != 'kube-system' or
                secret['metadata'].get('name') != 'calico-etcd-secrets' or
                set(secret.get('data', {})) != {'etcd-ca', 'etcd-cert', 'etcd-key'} or
                not all(secret['data'].values())):
            raise CalicoRefused('complete original etcdv3 TLS Secret is required')
        # Only the reviewed field delta may be patched. All other objects are
        # monitored for drift, including immutable bindings, SAs and the Secret.
        order = [(ref, None) for ref in self.new] + [('clusterrole/calico-kube-controllers', 'rules'),
                 ('configmap/calico-config', 'data'), (NODE, 'spec'), (CONTROLLER, 'spec')]
        expected = deepcopy(reviewed)
        pending = []
        for ref, field in order:
            if field is None:
                desired[ref] = deepcopy(self.after[ref])
                raw = self.command(['get', ref, '--ignore-not-found', '-o', 'json'])
                if raw.strip():
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        raise CalicoRefused('invalid new Calico RBAC observation') from None
                    if created_content(obj) != desired[ref]:
                        raise CalicoRefused('foreign Calico tier RBAC; adoption refused')
                    content(obj)
                    expected[ref] = obj
                else:
                    canonical = self.read(['create', '-f', '-', '--dry-run=server', '-o', 'json'],
                                          payload=json.dumps(desired[ref]))
                    if created_content(canonical) != desired[ref]:
                        raise CalicoRefused('server dry-run changed new Calico RBAC')
                    pending.append((ref, field))
                continue
            if reviewed[ref][field] == desired[ref][field]:
                continue
            obj = self.read(['get', ref, '-o', 'json'])
            if content(obj) == content(desired[ref]):
                expected[ref] = deepcopy(desired[ref])
            elif content(obj) == content(reviewed[ref]):
                operations = self.patch(obj, desired[ref], field)
                canonical = self.read(['patch', ref, '--type=json', '-p', json.dumps(operations),
                                       '--dry-run=server', '-o', 'json'])
                if content(canonical) != content(desired[ref]):
                    raise CalicoRefused('server dry-run changed reviewed Calico content')
                desired[ref] = canonical
                pending.append((ref, field))
            else:
                raise CalicoRefused('Calico object is outside the saved transition: '+ref)
        # A durable interruption permits only a prefix of the exact plan.
        changed = [(ref, field) for ref, field in order
                   if field is None or reviewed[ref][field] != desired[ref][field]]
        flags = [(ref, field) in pending for ref, field in changed]
        if flags != sorted(flags):
            raise CalicoRefused('Calico progress is not a prefix of the reviewed order')
        self.check(expected); self.barrier(); verify_hosts(); verify_datastore()
        self.wait(expected)
        verify_traffic()
        for ref, field in pending:
            self.barrier(); verify_hosts(); verify_datastore()
            if field is None:
                self.check(expected)
                obj = self.read(['create', '-f', '-', '-o', 'json'], payload=json.dumps(desired[ref]))
                if created_content(obj) != desired[ref]:
                    raise CalicoRefused('created Calico RBAC differs; retain for diagnosis')
                content(obj)
                expected[ref] = obj
                self.check(expected); verify_datastore(); verify_traffic()
                self.progress('UPGRADE_CALICO_BOUNDARY_VERIFIED '+ref)
                continue
            current = self.check(expected)[ref]
            self.command(['patch', ref, '--type=json', '-p',
                          json.dumps(self.patch(current, desired[ref], field))])
            expected[ref] = desired[ref]
            self.wait(expected); verify_traffic(); verify_datastore(); self.check(expected)
            self.progress('UPGRADE_CALICO_BOUNDARY_VERIFIED '+ref)
        self.wait(expected)
        install_client(self.edge[1])  # Exact same-version binary, never an old calicoctl.
        verify_datastore(); verify_traffic(); self.check(expected); self.barrier()
        return {'scope': 'calico-etcdv3-transition', 'source': self.edge[0], 'target': self.edge[1],
                'objects': {ref: obj['metadata']['uid'] for ref, obj in expected.items()},
                'parent_acceptance': False}
