"""Journal-bound handoff between the actual control-plane and node playbooks.

Internal product stage, with no standalone admission or CLI. The outer config +
kubecli path still owns combination/artifact admission, leases, recovery points,
storage maintenance and business checks. Completion here is never a GA claim.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from upgrade.product.kubernetes_preflight import NODE_NAME, Refused, endpoint, mapping, query


SOURCE, TARGET = 'v1.33.6', 'v1.34.13'
CONTROL = 'kubernetes/control-plane'
PRODUCT = Path(__file__).parent


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


class StageFailed(Refused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'Kubernetes stage failed (rc={rc}); retain owned diagnostics and journal')


class KubernetesStage:
    def __init__(self, source, hosts, variables, kubectl, kubeconfig, run_playbook):
        self.source = deepcopy(source)
        self.hosts = deepcopy(hosts)  # Ordered inventory host -> Kubernetes node name.
        self.variables = deepcopy(variables)
        self.kubectl, self.kubeconfig = Path(kubectl), Path(kubeconfig)
        self.run_playbook = run_playbook
        if (source.get('source') != SOURCE or source.get('target') != TARGET or
                source.get('mutation') != 'none' or
                source.get('cluster_identity', {}).get('namespace') != 'kube-system' or
                not source.get('cluster_identity', {}).get('uid') or
                variables.get('UPGRADE_SOURCE_VERSION') != SOURCE or
                variables.get('K8S_VER') not in [TARGET, TARGET[1:]] or
                variables.get('UPGRADE_PRECONDITIONS_VERIFIED') is not True or
                not callable(run_playbook)):
            raise Refused('exact source receipt and outer admission are required')
        self.variables['K8S_VER'] = TARGET  # config uses 1.x.y; internal books require v1.x.y.
        if (not isinstance(hosts, dict) or not hosts or
                not all(isinstance(host, str) and host and isinstance(name, str) and
                        NODE_NAME.fullmatch(name) for host, name in hosts.items()) or
                len(set(hosts.values())) != len(hosts)):
            raise Refused('complete unique inventory host to node mapping is required')
        nodes = source.get('nodes', [])
        self.nodes = {node['name']: node for node in nodes}
        if (len(self.nodes) != len(nodes) or set(self.nodes) != set(hosts.values()) or
                any(not node.get('uid') or node.get('Ready') is not True or
                    type(node.get('originally_schedulable')) is not bool or
                    node.get('kubeletVersion') != SOURCE for node in nodes) or
                len({node['uid'] for node in nodes}) != len(nodes)):
            raise Refused('source nodes must bind every UID and original healthy scheduling state')
        self.backends = [endpoint(b['server']) for b in source.get('api_backends', [])]
        if (not self.backends or len(set(self.backends)) != len(self.backends) or
                any(b.get('gitVersion') != SOURCE or b.get('readyz_rc') != 0
                    for b in source['api_backends']) or
                self.backends != variables.get('UPGRADE_API_BACKENDS')):
            raise Refused('complete exact API backend binding is required')
        if (not self.kubectl.is_file() or
                hashlib.sha256(self.kubectl.read_bytes()).hexdigest() != source.get('kubectl_sha256') or
                not self.kubeconfig.is_file()):
            raise Refused('source observer client and kubeconfig are required')
        self.steps = [CONTROL, *['kubernetes/node/' + name for name in hosts.values()]]
        self.binding = {
            'cluster_uid': source['cluster_identity']['uid'],
            'kubernetes_inputs_sha256': fingerprint({'source': source, 'hosts': list(hosts.items()),
                'variables': self.variables, 'kubeconfig_sha256':
                hashlib.sha256(self.kubeconfig.read_bytes()).hexdigest(),
                'implementation': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [Path(__file__), PRODUCT/'kubernetes_preflight.py', PRODUCT/'journal.py',
                              *[PRODUCT/name for name in ['kubernetes-observe.yml', 'control-plane.yml',
                                 'node.yml', 'containerd-admission.yml', 'containerd.yml',
                                 'containerd-edge.yml', 'docker-admission.yml', 'docker.yml']]]}})}

    def read(self, args, server):
        return mapping(query(self.kubectl, self.kubeconfig, server, args))

    def observe(self, journal, *, after=None):
        """Reobserve the complete identity and allowed progress, never trust a marker."""
        completed = set(journal.data['completed']) | ({after} if after else set())
        active = None if after else journal.data['active']
        allowed_apis = [TARGET] if CONTROL in completed else (
            [SOURCE, TARGET] if active == CONTROL else [SOURCE])
        all_target = True
        for server in self.backends:
            version = self.read(['get', '--raw=/version'], server).get('gitVersion')
            if version not in allowed_apis:
                raise Refused('API version is outside the journal-owned boundary')
            all_target &= version == TARGET
            query(self.kubectl, self.kubeconfig, server, ['get', '--raw=/readyz'])
            namespace = self.read(['get', 'namespace', 'kube-system', '-o', 'json'], server)
            if (namespace.get('metadata', {}).get('uid') != self.binding['cluster_uid'] or
                    namespace.get('metadata', {}).get('deletionTimestamp') or
                    namespace.get('status', {}).get('phase') != 'Active'):
                raise Refused('API backend cluster identity or namespace health changed')
        items = self.read(['get', 'nodes', '-o', 'json'], self.backends[0]).get('items')
        if not isinstance(items, list) or len(items) != len(self.nodes):
            raise Refused('complete node inventory changed')
        seen = set()
        allowed_nodes = {}
        for node in items:
            meta, status = node.get('metadata', {}), node.get('status', {})
            name = meta.get('name')
            if name not in self.nodes or name in seen:
                raise Refused('unknown or duplicate node')
            seen.add(name)
            step = 'kubernetes/node/' + name
            versions = [TARGET] if step in completed else (
                [SOURCE, TARGET] if step == active else [SOURCE])
            if (meta.get('uid') != self.nodes[name]['uid'] or meta.get('deletionTimestamp') or
                    [c.get('status') for c in status.get('conditions', [])
                     if c.get('type') == 'Ready'] != ['True'] or
                    status.get('nodeInfo', {}).get('kubeletVersion') not in versions or
                    (node.get('spec', {}).get('unschedulable', False) !=
                     (not self.nodes[name]['originally_schedulable']) and step != active)):
                raise Refused('node identity, readiness, version or foreign cordon changed')
            allowed_nodes[name] = versions
        return all_target, allowed_nodes

    def book(self, name, variables):
        result = self.run_playbook(PRODUCT/name, extra_vars=variables)
        if type(result.rc) is not int or result.rc != 0:
            raise StageFailed(result.rc if type(result.rc) is int and result.rc else 1)

    def run(self, journal, verify_business, *, stop_after=None):
        if (journal.fd is None or not callable(verify_business) or
                any(journal.binding.get(k) != v for k, v in self.binding.items()) or
                journal.binding.get('run_id') != self.variables.get('UPGRADE_RUN_ID')):
            raise Refused('locked matching journal and a real business boundary check are required')
        start = journal.steps.index(CONTROL) if CONTROL in journal.steps else -1
        if start < 0 or journal.steps[start:start+len(self.steps)] != self.steps:
            raise Refused('journal must contain the contiguous exact Kubernetes stage order')
        if len(journal.data['completed']) < start:
            raise Refused('earlier admitted platform stages are not complete')
        all_target, allowed_nodes = self.observe(journal)
        controller_versions = [TARGET] if CONTROL in journal.data['completed'] else (
            [SOURCE, TARGET] if journal.data['active'] == CONTROL and all_target else [SOURCE])
        self.book('kubernetes-observe.yml', {**self.variables,
            'UPGRADE_EXPECTED_CONTROLLER_VERSIONS': controller_versions,
            'UPGRADE_EXPECTED_NODE_VERSIONS': {host: allowed_nodes[name]
                                              for host, name in self.hosts.items()}})
        verify_business()
        for step in self.steps:
            if step in journal.data['completed']:
                continue
            resume = journal.data['active'] == step
            journal.begin(step)
            try:
                # Admission and business checks occur while this boundary is durable.
                self.observe(journal)
                variables = deepcopy(self.variables)
                if step == CONTROL:
                    self.book('control-plane.yml', variables)
                else:
                    name = step.removeprefix('kubernetes/node/')
                    host = next(host for host, node in self.hosts.items() if node == name)
                    variables.update(UPGRADE_NODE_HOST=host, UPGRADE_NODE_NAME=name,
                        UPGRADE_NODE_UID=self.nodes[name]['uid'],
                        UPGRADE_ORIGINALLY_SCHEDULABLE=self.nodes[name]['originally_schedulable'],
                        UPGRADE_NODE_RESUME=resume)
                    self.book('node.yml', variables)
                self.observe(journal, after=step)
                verify_business()
            except BaseException as error:
                # Keep the real first exit; no rollback, uncordon or broad cleanup.
                journal.failed(step, getattr(error, 'rc', 130 if isinstance(error, KeyboardInterrupt) else 1),
                               'unclassified')
                raise
            journal.complete(step)
            if stop_after == step:
                return
