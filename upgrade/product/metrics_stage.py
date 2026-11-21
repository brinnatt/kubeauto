"""Image-only Metrics Server edge after every API and node reaches 1.34.13.

Internal product stage: the outer config + kubecli path owns admission, immutable
source observations, artifacts, lease, recovery and journal. No install role is
replayed. Pod readiness alone cannot accept this edge.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import subprocess
import time

from kubernetes.utils.quantity import parse_quantity

from upgrade.product.calico_stage import CalicoRefused, content
from upgrade.product.dns_stage import DNSStage, DNSRefused, healthy


WORKLOAD = 'deployment/metrics-server'
API = 'apiservice/v1beta1.metrics.k8s.io'
SOURCE = 'registry.talkschool.cn:5000/brinnatt/metrics-server:v0.8.0'
TARGET = 'registry.talkschool.cn:5000/brinnatt/metrics-server:v0.9.1'
OBJECTS = {WORKLOAD, API, 'service/metrics-server', 'serviceaccount/metrics-server',
           'clusterrole/system:aggregated-metrics-reader', 'clusterrole/system:metrics-server',
           'rolebinding/metrics-server-auth-reader',
           'clusterrolebinding/metrics-server:system:auth-delegator',
           'clusterrolebinding/system:metrics-server'}
NAME = re.compile(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?')
QUANTITY = re.compile(r'[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[numkMGTPE]|[KMGTPE]i|[eE][+-]?\d+)?')
DURATION = re.compile(r'(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|μs|ms|s|m|h)')
UNITS = {'ns': Decimal('1e-9'), 'us': Decimal('1e-6'), 'µs': Decimal('1e-6'),
         'μs': Decimal('1e-6'), 'ms': Decimal('1e-3'), 's': Decimal(1),
         'm': Decimal(60), 'h': Decimal(3600)}


class MetricsRefused(ValueError):
    """An identity, configuration, API or sample boundary differs."""


class MetricsNotReady(MetricsRefused):
    """The bounded target convergence loop may wait for this condition."""


class MetricsCommandFailed(MetricsRefused):
    def __init__(self, rc):
        self.rc = rc
        super().__init__(f'Metrics command failed (rc={rc}); retain private diagnostics')


def sample_time(obj, now):
    stamp, window = obj.get('timestamp'), obj.get('window')
    if (not isinstance(stamp, str) or not re.fullmatch(
            r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', stamp) or
            not isinstance(window, str) or len(window) > 96):
        raise MetricsRefused('metrics timestamp/window has an invalid wire format')
    try:
        observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        raise MetricsRefused('metrics timestamp is not a real UTC time') from None
    parts = list(DURATION.finditer(window))
    if not parts or ''.join(p[0] for p in parts) != window:
        raise MetricsRefused('metrics window must be a positive Go duration')
    seconds = sum((Decimal(p[1]) * UNITS[p[2]] for p in parts), Decimal(0))
    if seconds <= 0 or seconds > 120:
        raise MetricsRefused('metrics window is outside the explicit 0–120s qualification bound')
    age = (now - observed).total_seconds()
    if age < -10:
        raise MetricsRefused('metrics timestamp is more than 10s in the future')
    if age > 120:
        raise MetricsNotReady('metrics sample is older than the explicit 120s bound')
    return observed


def usage(value):
    if not isinstance(value, dict) or not {'cpu', 'memory'} <= set(value):
        raise MetricsRefused('both CPU and memory usage are required')
    for key in ['cpu', 'memory']:
        raw = value[key]
        if not isinstance(raw, str) or len(raw) > 96 or not QUANTITY.fullmatch(raw):
            raise MetricsRefused('metrics usage is not a nonnegative Kubernetes quantity')
        try:
            number = parse_quantity(raw)
        except (ValueError, InvalidOperation, OverflowError):
            raise MetricsRefused('metrics quantity could not be parsed') from None
        if not number.is_finite() or number < 0:
            raise MetricsRefused('metrics quantity is not finite and nonnegative')


def object_content(obj):
    try:
        return content(obj)
    except CalicoRefused:
        raise MetricsRefused('metrics object is unidentified or deleting') from None


class MetricsStage:
    def __init__(self, kubectl, kubeconfig, source, pods, *, timeout=600, progress=None):
        nodes = source.get('nodes', [])
        self.cluster_uid = source.get('cluster_identity', {}).get('uid')
        if ((source.get('source'), source.get('target')) not in {
                ('v1.33.6', 'v1.34.13'), ('v1.34.13', 'v1.35.10'), ('v1.35.10', 'v1.36.6')} or
                source.get('mutation') != 'none' or not self.cluster_uid or
                source.get('cluster_identity', {}).get('namespace') != 'kube-system' or
                not isinstance(nodes, list) or not nodes or
                len({n.get('name') for n in nodes}) != len(nodes) or
                not all(n.get('name') and n.get('uid') for n in nodes) or
                len({n['uid'] for n in nodes}) != len(nodes) or
                not isinstance(pods, dict) or not pods):
            raise MetricsRefused('complete source identity and live business Pod identities are required')
        self.nodes = {n['name']: n['uid'] for n in nodes}
        for key, pod in pods.items():
            names = key.split('/')
            containers = pod.get('containers', [])
            if (len(names) != 2 or not all(NAME.fullmatch(n) for n in names) or
                    not pod.get('uid') or pod.get('node') not in self.nodes or
                    not isinstance(containers, list) or not containers or
                    len(set(containers)) != len(containers) or
                    not all(isinstance(n, str) and NAME.fullmatch(n) for n in containers)):
                raise MetricsRefused('Pod identity, node and complete container names are required')
        self.pods = deepcopy(pods)
        self.transport = DNSStage(kubectl, kubeconfig,
            [b['server'] for b in source.get('api_backends', [])], self.nodes,
            timeout=timeout, progress=progress, target_version=source['target'],
            originally_schedulable={n['name']: n.get('originally_schedulable', True) for n in nodes})
        self.kubectl, self.kubeconfig = str(Path(kubectl).resolve()), str(Path(kubeconfig).resolve())
        self.timeout = timeout
        self.progress = progress or (lambda message: print(message, flush=True))

    def command(self, arguments):
        argv = [self.kubectl, '--kubeconfig', self.kubeconfig, '--namespace', 'kube-system',
                '--server', self.transport.backends[0], '--request-timeout=10s',
                '--insecure-skip-tls-verify=false', *arguments]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise MetricsCommandFailed(124) from None
        if result.returncode:
            raise MetricsCommandFailed(result.returncode)
        return result.stdout

    def read(self, arguments):
        try:
            value = json.loads(self.command(arguments))
        except json.JSONDecodeError:
            raise MetricsRefused('metrics observation is not JSON') from None
        if not isinstance(value, dict):
            raise MetricsRefused('metrics observation must be a JSON object')
        return value

    def barrier(self):
        try:
            self.transport.platform_barrier()
            for server in self.transport.backends:
                obj = self.transport.read(['get', 'namespace', 'kube-system', '-o', 'json'], server)
                if (obj.get('metadata', {}).get('uid') != self.cluster_uid or
                        obj.get('metadata', {}).get('deletionTimestamp') or
                        obj.get('status', {}).get('phase') != 'Active'):
                    raise MetricsRefused('metrics API backend belongs to a different/unhealthy cluster')
        except DNSRefused as error:
            if hasattr(error, 'rc'):
                raise MetricsCommandFailed(error.rc) from None
            raise MetricsRefused(str(error)) from None

    def check(self, expected):
        observed = {}
        for ref, wanted in expected.items():
            obj = self.read(['get', ref, '-o', 'json'])
            if object_content(obj) != object_content(wanted):
                raise MetricsRefused('metrics object identity/configuration changed: ' + ref)
            observed[ref] = obj
        return observed

    def check_pods(self):
        for key, wanted in self.pods.items():
            namespace, name = key.split('/')
            obj = self.read(['get', '--raw=/api/v1/namespaces/'+namespace+'/pods/'+name])
            meta, spec, status = obj.get('metadata', {}), obj.get('spec', {}), obj.get('status', {})
            if (meta.get('uid') != wanted['uid'] or meta.get('name') != name or
                    meta.get('namespace') != namespace or meta.get('deletionTimestamp') or
                    spec.get('nodeName') != wanted['node'] or status.get('phase') != 'Running' or
                    [c.get('status') for c in status.get('conditions', [])
                     if c.get('type') == 'Ready'] != ['True'] or
                    sorted(c.get('name', '') for c in spec.get('containers', [])) !=
                    sorted(wanted['containers'])):
                raise MetricsRefused('business Pod identity, readiness, node or containers changed')

    def samples(self, objects):
        if [c.get('status') for c in objects[API].get('status', {}).get('conditions', [])
                if c.get('type') == 'Available'] != ['True']:
            raise MetricsNotReady('metrics APIService is not Available')
        self.check_pods()
        values, stamps = {}, {}
        for resource, kind in [('nodes', 'NodeMetricsList'), ('pods', 'PodMetricsList')]:
            result = self.read(['get', '--raw=/apis/metrics.k8s.io/v1beta1/'+resource])
            if (result.get('apiVersion') != 'metrics.k8s.io/v1beta1' or
                    result.get('kind') != kind or not isinstance(result.get('items'), list)):
                raise MetricsRefused('metrics list has an unexpected API version/kind/items')
            entries = {}
            for obj in result['items']:
                meta = obj.get('metadata', {})
                key = meta.get('name') if resource == 'nodes' else (
                    str(meta.get('namespace', ''))+'/'+str(meta.get('name', '')))
                if not key or key in entries:
                    raise MetricsRefused('duplicate or unnamed metrics entry')
                entries[key] = obj
            wanted = self.nodes if resource == 'nodes' else self.pods
            if resource == 'nodes' and set(entries) - set(wanted):
                raise MetricsRefused('metrics includes nodes outside the admitted inventory')
            if set(wanted) - set(entries):
                raise MetricsNotReady('metrics missing admitted '+resource)
            for key in wanted:
                obj = entries[key]
                stamps[resource+'/'+key] = sample_time(obj, datetime.now(timezone.utc))
                if resource == 'nodes':
                    usage(obj.get('usage'))
                else:
                    containers = obj.get('containers')
                    if (not isinstance(containers, list) or
                            sorted(c.get('name', '') for c in containers) !=
                            sorted(wanted[key]['containers'])):
                        raise MetricsRefused('Pod metrics must include every admitted container exactly once')
                    for container in containers:
                        usage(container.get('usage'))
                values[resource+'/'+key] = deepcopy(obj)
        self.check_pods()  # Metrics metadata does not guarantee the Pod UID.
        return values, stamps

    def run(self, reviewed):
        if not isinstance(reviewed, dict) or set(reviewed) != OBJECTS:
            raise MetricsRefused('complete reviewed metrics Deployment/Service/APIService/RBAC required')
        expected = deepcopy(reviewed)
        source = reviewed[WORKLOAD]
        containers = source.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        if (source.get('apiVersion') != 'apps/v1' or source.get('kind') != 'Deployment' or
                source.get('metadata', {}).get('namespace') != 'kube-system' or
                source.get('metadata', {}).get('name') != 'metrics-server' or
                len(containers) != 1 or containers[0].get('name') != 'metrics-server' or
                containers[0].get('image') not in [SOURCE, TARGET] or
                containers[0].get('args', []).count('--metric-resolution=15s') != 1):
            raise MetricsRefused('metrics source deployment/image/explicit resolution differs')
        self.barrier()
        current = self.read(['get', WORKLOAD, '-o', 'json'])
        image = current.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0].get('image')
        if image not in [SOURCE, TARGET]:
            raise MetricsRefused('metrics image is outside the exact transition')
        if containers[0]['image'] == TARGET and image != TARGET:
            raise MetricsRefused('an admitted target metrics workload regressed')
        expected[WORKLOAD]['spec']['template']['spec']['containers'][0]['image'] = image
        observed = self.check(expected)
        baseline = None
        if image == SOURCE:
            if not healthy(observed[WORKLOAD]):
                raise MetricsRefused('source metrics workload is unhealthy')
            _, baseline = self.samples(observed)  # Never repair an unhealthy source by changing its image.
            meta = observed[WORKLOAD]['metadata']
            if not meta.get('resourceVersion'):
                raise MetricsRefused('metrics patch requires current resourceVersion')
            path = '/spec/template/spec/containers/0/image'
            ops = [{'op': 'test', 'path': '/metadata/'+key, 'value': meta[key]}
                   for key in ['uid', 'resourceVersion']]
            ops += [{'op': 'test', 'path': path, 'value': SOURCE},
                    {'op': 'replace', 'path': path, 'value': TARGET}]
            patch = ['patch', WORKLOAD, '--type=json', '-p', json.dumps(ops)]
            desired = deepcopy(expected[WORKLOAD])
            desired['spec']['template']['spec']['containers'][0]['image'] = TARGET
            qualified = self.read([*patch, '--dry-run=server', '-o', 'json'])
            if object_content(qualified) != object_content(desired):
                raise MetricsRefused('server dry-run introduced an unreviewed metrics change')
            self.barrier()
            self.check(expected)
            self.command(patch)
            expected[WORKLOAD] = desired
        deadline = time.monotonic() + self.timeout
        while True:
            self.barrier()
            observed = self.check(expected)
            try:
                if not healthy(observed[WORKLOAD]):
                    raise MetricsNotReady('metrics rollout is not fully observed/available')
                values, stamps = self.samples(observed)
                if baseline is None:
                    baseline = stamps  # Resume must also demonstrate a subsequent fresh scrape.
                elif all(stamps[key] > baseline[key] for key in baseline):
                    self.check(expected)
                    self.progress('UPGRADE_METRICS_API_VERIFIED nodes='+str(len(self.nodes))+
                                  ' pods='+str(len(self.pods)))
                    return {'scope': 'metrics-image-rollout-and-live-node-pod-api',
                            'objects': {ref: obj['metadata']['uid'] for ref, obj in observed.items()},
                            'image': TARGET, 'samples': values, 'parent_acceptance': False}
            except MetricsNotReady:
                pass
            if time.monotonic() >= deadline:
                raise MetricsRefused('metrics rollout/fresh node and Pod samples exceeded fixed timeout')
            self.progress('Metrics: waiting for rollout, Available APIService and advancing node/Pod samples')
            time.sleep(5)
