#!/usr/bin/env python3
"""Owned Prometheus prerequisite for LONGHORN-53.

The unchanged product template and setup 07 install the complete HA stack.
Only its test PVCs use a declared one-replica V2 StorageClass. Requests and
actual host memory are separate admission constraints; limits are not summed
as scheduler requests. No generated values or deployed workloads are patched.
"""
import argparse
import base64
import copy
from datetime import datetime
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

import jinja2
import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-v2-monitoring.json'
CLASS = 'longhorn-monitoring-one-replica'
DATA_ENGINE = 'v2'
RELAY_HELPER = 'longhorn-v2-install.py'
MARKER = 'LONGHORN_V2_MONITORING'
FIELDS = ('prom_install', 'prom_namespace', 'prom_storage_class', 'prom_chart_ver',
          'longhorn_monitoring_enabled', 'longhorn_monitoring_namespace',
          'longhorn_monitoring_selector', 'longhorn_monitoring_release')


class RenderLoader(yaml.SafeLoader):
    """CRD enums may contain a bare '=' (a PyYAML 1.1 implicit value tag)."""


RenderLoader.add_constructor('tag:yaml.org,2002:value', RenderLoader.construct_scalar)


def render_documents(raw):
    return [d for d in yaml.load_all(raw, Loader=RenderLoader) if d and
            not (d.get('metadata', {}).get('annotations') or {}).get('helm.sh/hook', '').startswith('test')]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def quantity(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([a-zA-Z]*)', str(value))
    factors = {'': 1, 'm': .001, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3,
               'Ti': 1024**4, 'k': 1000, 'M': 1000**2, 'G': 1000**3, 'n': 1e-9, 'u': 1e-6}
    if not match or match[2] not in factors:
        raise ValueError('unsupported resource quantity')
    return float(match[1])*factors[match[2]]


def requests(pod):
    """Kubernetes scheduling: sum app containers, max init, plus overhead."""
    total = {'cpu': 0., 'memory': 0.}
    for container in pod.get('containers', []):
        for key in total:
            resources = container.get('resources', {})
            total[key] += quantity(resources.get('requests', {}).get(key, resources.get('limits', {}).get(key, 0)))
    # This pinned chart does not generate restartable init sidecars. Refuse
    # rather than undercount a changed scheduling model.
    for container in pod.get('initContainers', []):
        if container.get('restartPolicy') == 'Always':
            raise ValueError('restartable init sidecar needs explicit budget handling')
        for key in total:
            resources = container.get('resources', {})
            total[key] = max(total[key], quantity(resources.get('requests', {}).get(key, resources.get('limits', {}).get(key, 0))))
    for key in total:
        total[key] += quantity(pod.get('overhead', {}).get(key, 0))
    return total


def node_allowed(spec, node):
    labels = node['metadata'].get('labels', {})
    if any(labels.get(k) != v for k, v in spec.get('nodeSelector', {}).items()):
        return False
    affinity = spec.get('affinity', {}).get('nodeAffinity', {})
    required = affinity.get('requiredDuringSchedulingIgnoredDuringExecution')
    if required:
        def requirement(row, data):
            key = row['key']; op = row['operator']; values = row.get('values', [])
            if op == 'In': return key in data and data[key] in values
            if op == 'NotIn': return key not in data or data[key] not in values
            if op == 'Exists': return key in data
            if op == 'DoesNotExist': return key not in data
            if op in ('Gt', 'Lt') and len(values) == 1:
                try: return int(data[key]) > int(values[0]) if op == 'Gt' else int(data[key]) < int(values[0])
                except (KeyError, ValueError): return False
            raise ValueError('unsupported monitoring node affinity expression')
        terms = required.get('nodeSelectorTerms', [])
        if not any((term.get('matchExpressions') or term.get('matchFields')) and
            all(requirement(r, labels) for r in term.get('matchExpressions', [])) and
            all(requirement(r, {'metadata.name': node['metadata']['name']}) for r in term.get('matchFields', []))
            for term in terms):
            return False
    for taint in node['spec'].get('taints', []):
        if taint['effect'] not in ('NoSchedule', 'NoExecute'):
            continue
        if not any((not t.get('effect') or t['effect'] == taint['effect']) and
                   (not t.get('key') or t['key'] == taint['key']) and
                   (t.get('operator', 'Equal') == 'Exists' or t.get('value', '') == taint.get('value', ''))
                   for t in spec.get('tolerations', [])):
            return False
    return True


def planned_pods(documents, nodes):
    """Include operator-generated HA Pods, not just Helm Deployments."""
    rows = []
    for doc in documents:
        kind = doc.get('kind'); spec = doc.get('spec', {}); name = doc['metadata']['name']
        if kind in ('Deployment', 'StatefulSet', 'DaemonSet', 'Job'):
            template = spec['template']['spec']
            if kind == 'DaemonSet':
                for node in nodes:
                    if node_allowed(template, node):
                        rows.append({'name': name+'@'+node['metadata']['name'], 'group': name,
                                     'spec': template, 'pinned': node['metadata']['name']})
                continue
            replicas = spec.get('replicas', 1) if kind != 'Job' else spec.get('parallelism', 1)
            hard = bool(template.get('affinity', {}).get('podAntiAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution'))
        elif kind in ('Prometheus', 'Alertmanager'):
            # Reserve 200m/100Mi per generated reloader conservatively. This
            # is a fixture admission allowance, not a claim about operator
            # defaults. Actual generated Pod requests are checked after setup.
            image = spec['image']
            template = {'containers': [{'name': kind.lower(), 'image': image, 'resources': spec['resources']},
                {'name': 'config-reloader', 'resources': {'requests': {'cpu': '200m', 'memory': '100Mi'}}}],
                'nodeSelector': spec.get('nodeSelector', {}), 'tolerations': spec.get('tolerations', []),
                'affinity': spec.get('affinity', {})}
            anti = spec.get('affinity', {}).get('podAntiAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution', [])
            if any(a.get('topologyKey') != 'kubernetes.io/hostname' for a in anti):
                raise ValueError('monitoring anti-affinity requires hostname-level budget handling')
            replicas = spec.get('replicas', 1); hard = bool(anti) or spec.get('podAntiAffinity') == 'hard'
            if kind == 'Prometheus':
                replicas *= spec.get('shards', 1)
        else:
            continue
        if type(replicas) is not int or not 1 <= replicas <= 6:
            raise ValueError('unexpected monitoring workload replica count')
        for i in range(replicas):
            rows.append({'name': name+'-'+str(i), 'group': name if hard else '', 'spec': template})
    if not any(d.get('kind') == 'Prometheus' and d['spec'].get('replicas') == 2 for d in documents):
        raise ValueError('render must preserve two Prometheus replicas')
    if not any(d.get('kind') == 'Alertmanager' and d['spec'].get('replicas') == 3 for d in documents):
        raise ValueError('render must preserve three Alertmanager replicas')
    return rows


def placement(nodes, pods, planned):
    """Small exact search with hard anti-affinity, taints, requests and headroom."""
    available = {n['metadata']['name']: {k: quantity(n['status']['allocatable'][k]) for k in ('cpu', 'memory')} for n in nodes}
    by_name = {n['metadata']['name']: n for n in nodes}
    for pod in pods:
        if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        name = pod['spec'].get('nodeName')
        if not name:
            raise ValueError('existing pending workload invalidates monitoring budget')
        if name not in available:
            raise ValueError('existing Pod runs outside the owned nodes')
        for key, value in requests(pod['spec']).items():
            available[name][key] -= value
    # A real non-Pod headroom budget is additional to kubelet's allocatable.
    # SPDK hugepages have already been subtracted by kubelet, never twice.
    for value in available.values():
        value['memory'] -= 256*1024**2
        if min(value.values()) < 0:
            raise ValueError('current requests leave no monitoring headroom')
    candidates = {}
    for row in planned:
        candidates[row['name']] = [name for name, node in by_name.items()
            if (not row.get('pinned') or row['pinned'] == name) and node_allowed(row['spec'], node)]
    ordered = sorted(planned, key=lambda r: (len(candidates[r['name']]), -requests(r['spec'])['memory']))
    result = {}; groups = {}
    def solve(index):
        if index == len(ordered):
            return True
        row = ordered[index]; cost = requests(row['spec']); group = row.get('group')
        for name in candidates[row['name']]:
            if group and name in groups.get(group, set()):
                continue
            if any(available[name][k] < cost[k] for k in cost):
                continue
            for k in cost: available[name][k] -= cost[k]
            result[row['name']] = name
            if group: groups.setdefault(group, set()).add(name)
            if solve(index+1): return True
            if group: groups[group].remove(name)
            result.pop(row['name'])
            for k in cost: available[name][k] += cost[k]
        return False
    if not solve(0):
        raise ValueError('complete monitoring render does not fit the current owned nodes')
    return {'placement': result, 'remaining_requests': available, 'host_headroom_bytes_per_node': 256*1024**2,
            'boundary': 'Scheduler request admission; separate runtime MemAvailable and pressure checks remain mandatory.'}


def sample_vector(payload, now, *, allow_empty=False):
    if payload.get('status') != 'success' or payload.get('data', {}).get('resultType') != 'vector':
        raise ValueError('Prometheus did not return a successful instant vector')
    rows = payload['data']['result']
    if not rows and not allow_empty:
        raise ValueError('Prometheus samples are missing; absence is not zero')
    result = []
    for row in rows:
        stamp, text = row['value']; value = float(text)
        if not 0 <= now-float(stamp) <= 90 or not __import__('math').isfinite(value):
            raise ValueError('Prometheus sample is stale, future or non-finite')
        result.append({'labels': row['metric'], 'timestamp': stamp, 'value': value})
    return result


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('symlink monitoring ownership receipt')
    row = json.loads(path.read_text())
    if any(row[k] != lab.owner()[k] for k in ('run_id', 'cluster_uid', 'namespace_uid')):
        raise ValueError('monitoring receipt belongs to another cluster or installation')
    return row


def namespace_identity(lab, row):
    current = lab.get('namespace', row['namespace'])
    if current and (current['metadata']['uid'] != row.get('monitor_namespace_uid') or
                    current['metadata'].get('labels', {}).get('kubeauto.io/longhorn-monitoring-run') != row['run_id']):
        raise ValueError('monitoring namespace was replaced or has no successful create receipt')
    return current


def persist(lab, row):
    lab.write_private(lab.DIRECTORY/RECORD, row)


def product(lab, pid, step):
    """Keep setup 07 on the same exact relay supervision contract as setup 09."""
    if step == 'longhorn-setup':
        lab.supervise_core(pid, step)
        return
    if step != '07':
        raise ValueError('unexpected monitoring product step')
    original = lab.process_identity(pid)
    required = [str(Path(__file__).with_name(RELAY_HELPER).resolve()).encode(), b'relay-maintain']
    if not original or original[0][-2:] != required:
        raise ValueError('monitoring setup requires the exact live V2 Registry supervisor')
    argv = [sys.executable, str(lab.BASE/'kubecli.py'), 'setup', lab.CLUSTER, '07']
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
    start = heartbeat = time.monotonic()
    with lab.scoped_cleanup(lambda: lab.stop_product(process, argv)):
        while process.poll() is None:
            if lab.process_identity(pid) != original:
                raise ValueError('monitoring relay exited during product setup')
            now = time.monotonic()
            if now-start > 3600:
                raise TimeoutError('monitoring product setup exceeded one hour')
            if now-heartbeat >= 15:
                print(MARKER+'_PRODUCT_HEARTBEAT elapsed='+str(int(now-start)), flush=True)
                heartbeat = now
            time.sleep(1)
        if process.returncode:
            raise ValueError('product setup 07 failed; retain its first failure and owned namespace')


def apply(lab, row, config):
    path = lab.DIRECTORY/'config.yml'
    if path.is_symlink() or path.read_text() not in row['allowed_inputs']:
        raise ValueError('monitoring refuses changed product input')
    desired = yaml.safe_dump(config, sort_keys=False)
    if desired not in row['allowed_inputs']:
        row['allowed_inputs'].append(desired)
    persist(lab, row)
    load('longhorn-v2-disk-faults.py').atomic(path, desired)


def render(lab, config):
    source = lab.BASE/'roles/cluster-addon/templates/prometheus/values.yaml.j2'
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    values = environment.from_string(source.read_text()).render(**dict(config, groups=inventory_groups(lab)))
    chart = lab.BASE/'roles/cluster-addon/files/kube-prometheus-stack-88.0.0.tgz'
    with tempfile.NamedTemporaryFile('w', suffix='.yaml', dir='/var/tmp') as stream:
        stream.write(values); stream.flush()
        result = lab.execute([str(lab.BASE/'extra-bin/helm'), 'template', 'prometheus', str(chart),
            '--namespace', config['prom_namespace'], '--include-crds', '--kube-version', config['K8S_VER'],
            '-f', stream.name], timeout=120)
    docs = render_documents(result.stdout)
    if any(d.get('kind') in ('Prometheus', 'Alertmanager') and
           d['spec'].get('storage', d['spec'].get('storageSpec', {})).get('volumeClaimTemplate', {}).get('spec', {}).get('storageClassName') != CLASS
           for d in docs):
        raise ValueError('monitoring render does not use its explicitly declared single-replica fixture class')
    return docs, values, hashlib.sha256(chart.read_bytes()).hexdigest()


def inventory_groups(lab):
    return {'kube_master': list(lab.NODES), 'kube_node': list(lab.NODES), 'etcd': list(lab.NODES)}


def desired(original, namespace):
    config = yaml.safe_load(original)
    if (config['prom_install'] != 'no' or config['longhorn_monitoring_enabled'] or
            config['longhorn_requests'] or config.get('longhorn_data_engines', ['v1']) != [DATA_ENGINE] or
            any(config.get(k, 'no') != 'no' for k in ('prom_thanos_install', 'prom_adapter_install', 'prom_blackbox_install'))):
        raise ValueError('monitoring requires the unused independent V2 product baseline')
    if any(s['name'] == CLASS for s in config['longhorn_storage_classes']):
        raise ValueError('monitoring fixture StorageClass is already declared')
    config.update(prom_install='yes', prom_namespace=namespace, prom_chart_ver='88.0.0', prom_storage_class=CLASS,
        longhorn_monitoring_enabled=True, longhorn_monitoring_namespace=namespace,
        longhorn_monitoring_selector={'app.kubernetes.io/name': 'prometheus'}, longhorn_monitoring_release='prometheus')
    storage = copy.deepcopy(config['longhorn_storage_classes'][0])
    storage.update(name=CLASS, replicas=1, reclaim_policy='Delete', default=False, encrypted=False,
                   encryption_secret='', recurring_jobs=[], data_engine=DATA_ENGINE)
    config['longhorn_storage_classes'].append(storage)
    return config


def host_memory(lab):
    rows = {}
    script = '''import json,re
from pathlib import Path
s=Path('/proc/meminfo').read_text()
print(json.dumps({k:int(v)*1024 for k,v in re.findall(r'^(MemAvailable|MemTotal|Hugepagesize):\\s+(\\d+) kB',s,re.M)}))
'''
    for address in lab.NODES:
        row = json.loads(lab.remote(address, ['python3', '-c', script], timeout=30).stdout)
        # The runtime low watermark protects control plane plus a 2Gi page
        # allocation. It is not a sum of container limits or a production SLA.
        if row['MemAvailable'] < 768*1024**2:
            raise ValueError('monitoring runtime MemAvailable is below the 768Mi stop watermark')
        rows[address] = row
    return rows


def storage_budget(lab):
    setting = lab.get('settings.longhorn.io', 'storage-minimal-available-percentage', NS)
    minimal = int(setting['value'])
    if not 0 <= minimal <= 100:
        raise ValueError('invalid actual storage scheduling threshold')
    result = {}
    for node in lab.get('nodes.longhorn.io', namespace=NS)['items']:
        disks = node['spec']['disks']; statuses = node.get('status', {}).get('diskStatus', {})
        rows = []
        for name, disk in disks.items():
            state = statuses.get(name, {})
            if disk.get('diskType') != 'block' or not disk.get('allowScheduling') or not state.get('diskUUID'):
                continue
            maximum = state.get('storageMaximum', 0); available = state.get('storageAvailable', 0)
            schedulable = min(available-maximum*minimal/100,
                              maximum-disk.get('storageReserved', 0)-state.get('storageScheduled', 0))
            rows.append({'disk_uuid': state['diskUUID'], 'schedulable_bytes': schedulable,
                         'maximum': maximum, 'available': available})
        # Worst placement on one node is Prom20 + Graf20 + Alert10 GiB.
        # Four GiB additional headroom covers this bounded workload's data.
        if len(rows) != 1 or rows[0]['schedulable_bytes'] < 54*1024**3:
            raise ValueError('single-replica monitoring fixtures lack their worst-node raw-disk budget')
        result[node['metadata']['name']] = rows[0]
    if len(result) != 3:
        raise ValueError('monitoring storage budget requires the three owned V2 nodes')
    return result


def admission(lab, pid=0):
    lab.owner(); lab.installation_health()
    path = lab.DIRECTORY/'config.yml'
    config = desired(path.read_text(), 'longhorn-monitor-admission')
    docs, values, digest = render(lab, config)
    nodes = lab.get('nodes')['items']; pods = lab.get('pods', namespace='*')['items']
    result = placement(nodes, pods, planned_pods(docs, nodes))
    result.update(chart_sha256=digest, values_sha256=hashlib.sha256(values.encode()).hexdigest(),
                  runtime_memory=host_memory(lab), storage=storage_budget(lab))
    print(MARKER+'_ADMISSION '+json.dumps(result, sort_keys=True), flush=True)
    return result


def cache(lab, *, reconcile=None):
    evidence = json.loads((lab.BASE/'tests/evidence/longhorn-prometheus-images-20261021.json').read_text())
    rows = evidence['images']
    if evidence['status'] != 'pass' or len(rows) != 11 or len({r['key'] for r in rows}) != 11:
        raise ValueError('monitoring requires its exact published eleven-image inventory')
    original = lab.publication_records
    def publications():
        production, support = original()
        selected = {r['key']: r for r in rows}
        identity = ('digest', 'source_digest', 'platforms', 'talkedu_ref', 'dockerhub_ref',
                    'source_ref', 'fingerprint')
        for existing in production+support:
            pinned = selected.get(existing['key'])
            if pinned is not None and any(existing.get(field) != pinned[field] for field in identity):
                raise ValueError('conflicting monitoring artifact identity: '+existing['key'])
        # The cache chooses its repository prefix from support membership.
        # These exact artifacts must use the product root during this scope;
        # keep the original support inventory and its existing cache untouched.
        return ([r for r in production if r['key'] not in selected]+rows,
                [r for r in support if r['key'] not in selected])
    lab.publication_records = publications
    try:
        if reconcile is not None:
            lab.cache_artifacts(keys={'brinnatt/busybox:1.37'}, reconcile=reconcile)
        lab.cache_artifacts(keys={r['key'] for r in rows})
    finally:
        lab.publication_records = original


def setup(lab, pid, *, cache_reconcile=None):
    if (lab.DIRECTORY/RECORD).exists() or (lab.DIRECTORY/RECORD).is_symlink():
        raise ValueError('prior monitoring fixture requires its scoped cleanup')
    if lab.get('volumes.longhorn.io', namespace=NS)['items']:
        raise ValueError('monitoring setup requires no other V2 consumers')
    admission(lab, pid)
    if lab.get('storageclass', CLASS):
        raise ValueError('unowned monitoring fixture StorageClass exists')
    if lab.get('prometheuses.monitoring.coreos.com', namespace='*')['items'] if any(
            c['metadata']['name'] == 'prometheuses.monitoring.coreos.com' for c in lab.get('crds')['items']) else False:
        raise ValueError('monitoring refuses another Prometheus installation')
    cache(lab, reconcile=cache_reconcile)
    original = (lab.DIRECTORY/'config.yml').read_text()
    namespace = 'longhorn-monitor-'+secrets.token_hex(8)
    if lab.get('namespace', namespace):
        raise ValueError('monitoring namespace collision')
    row = {k: lab.owner()[k] for k in ('run_id', 'cluster_uid', 'namespace_uid')}
    docs, values, digest = render(lab, desired(original, namespace))
    row.update(namespace=namespace, original_input=original, allowed_inputs=[original],
               original_crds={c['metadata']['name']: c['metadata']['uid'] for c in lab.get('crds')['items']},
               expected_crds=[c['metadata']['name'] for c in docs if c['kind'] == 'CustomResourceDefinition'],
               namespace_intent=True, install_intent=False)
    persist(lab, row)
    obj = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace,
        'labels': {'kubeauto.io/longhorn-monitoring-run': row['run_id']}}}
    created = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
    row['monitor_namespace_uid'] = created['metadata']['uid']; persist(lab, row)
    config = desired(original, namespace)
    # First declare the fixture class through the storage product; monitoring
    # stays disabled until its CRD prerequisite exists.
    storage = copy.deepcopy(config)
    storage['longhorn_monitoring_enabled'] = False
    apply(lab, row, storage); product(lab, pid, 'longhorn-setup')
    sc = lab.get('storageclass', CLASS)
    if not sc or sc.get('reclaimPolicy') != 'Delete' or sc.get('parameters', {}).get('numberOfReplicas') != '1':
        raise ValueError('product did not converge the single-replica monitoring fixture class')
    row['storage_class_uid'] = sc['metadata']['uid']; row['install_intent'] = True; persist(lab, row)
    try:
        product(lab, pid, '07')
    finally:
        # Capture actual UIDs even when Helm fails partway. A later cleanup
        # cannot delete an unreceipted replacement merely by matching a name.
        row['created_crds'] = {c['metadata']['name']: c['metadata']['uid'] for c in lab.get('crds')['items']
            if c['metadata']['name'] in row['expected_crds'] and c['metadata']['name'] not in row['original_crds']}
        persist(lab, row)
    docs, values, digest = render(lab, config)
    generated = lab.DIRECTORY/'yml/prom-values.yaml'
    if yaml.safe_load(generated.read_text()) != yaml.safe_load(values):
        raise ValueError('product-rendered monitoring values differ from admitted inputs')
    apply(lab, row, config); product(lab, pid, 'longhorn-setup')
    row['ready_inputs'] = (lab.DIRECTORY/'config.yml').read_text()
    row['chart_sha256'] = digest
    row['created_crds'] = {c['metadata']['name']: c['metadata']['uid'] for c in lab.get('crds')['items']
                           if c['metadata']['name'] not in row['original_crds']}
    persist(lab, row)
    verify(lab, pid)
    print(MARKER+'_SETUP_PASS', flush=True)


def timestamp(value):
    # Helm's RFC3339Nano timestamps exceed Python 3.8's six-digit parser.
    value = re.sub(r'\.(\d+)', lambda match: '.'+(match[1]+'000000')[:6], value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def admission_hooks(objects, row, namespace):
    """Read executed hooks from Helm's stored release, never a name prefix."""
    hooks = copy.deepcopy(row.get('admission_hooks', {}))
    releases = [obj for obj in objects.values() if obj['kind'] == 'Secret' and
                obj['metadata']['name'].startswith('sh.helm.release.v1.prometheus.v')]
    # This fixture installs once in a fresh namespace. A chart retry/upgrade
    # needs its own run history qualification, not overwritten hook windows.
    history = row.get('qualified_release_history')
    if history and releases:
        actual = sorted([dict(name=o['metadata']['name'], uid=o['metadata']['uid'],
            secret_release_sha256=hashlib.sha256(o.get('data', {}).get('release', '').encode()).hexdigest())
            for o in releases], key=lambda o: o['name'])
        if actual != history:
            raise ValueError('monitoring qualified release history changed')
    elif len(releases) > 1:
        raise ValueError('monitoring requires one fresh Helm release revision')
    for obj in releases:
        meta = obj['metadata']
        if obj['kind'] != 'Secret' or not meta['name'].startswith('sh.helm.release.v1.prometheus.v'):
            continue
        if obj.get('type') != 'helm.sh/release.v1' or meta.get('labels', {}).get('owner') != 'helm':
            raise ValueError('foreign Helm release secret')
        try:
            data = base64.b64decode(base64.b64decode(obj['data']['release']))
            release = json.loads(gzip.decompress(data) if data.startswith(b'\x1f\x8b\x08') else data)
            if (release['name'] != 'prometheus' or release['namespace'] != row['namespace'] or
                    (release['version'] != 1 and not history) or
                    meta['name'] != 'sh.helm.release.v1.prometheus.v'+str(release['version']) or
                    release['chart']['metadata']['name'] != 'kube-prometheus-stack' or
                    release['chart']['metadata']['version'] != '88.0.0'):
                raise ValueError('monitoring Helm release identity differs')
            for hook in release.get('hooks', []):
                if hook['kind'] != 'Job':
                    continue
                action = {'prometheus-kube-prometheus-admission-create': 'create',
                          'prometheus-kube-prometheus-admission-patch': 'patch'}.get(hook['name'])
                manifest = yaml.safe_load(hook['manifest'])
                if (not action or manifest['apiVersion'] != 'batch/v1' or manifest['kind'] != 'Job' or
                        manifest['metadata']['name'] != hook['name'] or
                        manifest['metadata']['namespace'] != row['namespace'] or
                        hook['last_run']['phase'] != 'Succeeded' or
                        'hook-succeeded' not in hook['delete_policies']):
                    raise ValueError('monitoring admission hook identity differs')
                containers = manifest['spec']['template']['spec']['containers']
                if (len(containers) != 1 or containers[0]['name'] != action or
                        containers[0]['args'][0] != action or
                        '--namespace='+row['namespace'] not in containers[0]['args'] or
                        '--secret-name=prometheus-kube-prometheus-admission' not in containers[0]['args'] or
                        not containers[0]['image'].endswith('/brinnatt/prometheus-webhook-certgen:1.8.5')):
                    raise ValueError('monitoring admission hook command differs')
                started = timestamp(hook['last_run']['started_at'])
                completed = timestamp(hook['last_run']['completed_at'])
                if not timestamp(namespace['metadata']['creationTimestamp']) <= started <= completed:
                    raise ValueError('monitoring hook predates its owned namespace')
                key = hook['name'] if release['version'] == 1 else hook['name']+'@'+str(release['version'])
                hooks[key] = dict(start=int(started), end=int(completed)+1, container=action)
                if release['version'] != 1:
                    hooks[key]['name'] = hook['name']
        except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
            raise ValueError('cannot establish monitoring admission hook history') from exc
    return hooks


def historical_hook_events(objects, row, hooks):
    """Bind deleted Job/Pod events to actual hook runs and controller records."""
    namespace = row['namespace']
    events = [obj for obj in objects.values() if obj['kind'] == 'Event']
    subjects = copy.deepcopy(row.get('hook_event_subjects', {}))

    def subject(event):
        return event.get('involvedObject', event.get('regarding', {}))

    def matches(event, reference, run):
        ref = subject(event)
        source = event.get('reportingController') or event.get('source', {}).get('component')
        reasons = ({'SuccessfulCreate', 'Completed'} if reference['kind'] == 'Job' else
                   {'Scheduled'} if source == 'default-scheduler' else {'Pulling', 'Pulled', 'Created', 'Started'})
        controller = ('job-controller' if reference['kind'] == 'Job' else
                      'default-scheduler' if event.get('reason') == 'Scheduled' else 'kubelet')
        return (all(ref.get(key) == value for key, value in reference.items()) and
                event['metadata'].get('namespace') == namespace and event.get('type') == 'Normal' and
                source == controller and event.get('reason') in reasons and
                run['start'] <= timestamp(event['metadata']['creationTimestamp']) <= run['end'] and
                (ref.get('fieldPath', '') in ('', 'spec.containers{'+run['container']+'}')))

    for key, run in hooks.items():
        name = run.get('name', key)
        reference = dict(apiVersion='batch/v1', kind='Job', name=name, namespace=namespace)
        job_events = [event for event in events if matches(event, reference, run)]
        uids = {subject(event).get('uid') for event in job_events}
        if not uids:
            continue
        if len(uids) != 1 or not next(iter(uids)):
            raise ValueError('ambiguous monitoring hook Job UID')
        uid = next(iter(uids))
        subjects[uid] = dict(reference=dict(reference, uid=uid), run=run)
        # Job-controller names the concrete created Pod, rather than merely
        # authorizing every Pod with the hook's prefix after garbage collection.
        names = {event.get('message', event.get('note', ''))[len('Created pod: '):]
                 for event in job_events if event['reason'] == 'SuccessfulCreate' and
                 event.get('message', event.get('note', '')).startswith('Created pod: ')}
        for pod_name in names:
            ref = dict(apiVersion='v1', kind='Pod', name=pod_name, namespace=namespace)
            pod_uids = {subject(event).get('uid') for event in events if matches(event, ref, run)}
            if len(pod_uids) > 1 or None in pod_uids:
                raise ValueError('ambiguous monitoring hook Pod UID')
            for pod_uid in pod_uids:
                subjects[pod_uid] = dict(reference=dict(ref, uid=pod_uid), run=run)
    accepted = {event['metadata']['uid'] for event in events
                if (record := subjects.get(subject(event).get('uid'))) and
                matches(event, record['reference'], record['run'])}
    return accepted, subjects


def controller_endpoints(obj, objects, allowed):
    """Kubernetes 1.33 copies Service labels and Pod refs, not owner refs."""
    meta = obj['metadata']; namespace = meta.get('namespace')
    service = next((o for uid, o in objects.items() if uid in allowed and o['kind'] == 'Service' and
                    o['metadata']['name'] == meta['name'] and o['metadata'].get('namespace') == namespace), None)
    if not service or not service.get('spec', {}).get('selector') or meta.get('ownerReferences'):
        return False
    labels = dict(meta.get('labels') or {})
    if labels.pop('endpoints.kubernetes.io/managed-by', None) != 'endpoint-controller':
        return False
    labels.pop('service.kubernetes.io/headless', None)
    if labels != (service['metadata'].get('labels') or {}):
        return False
    for subset in obj.get('subsets', []):
        for address in subset.get('addresses', []) + subset.get('notReadyAddresses', []):
            ref = address.get('targetRef', {}); pod = objects.get(ref.get('uid'))
            if (not pod or ref.get('uid') not in allowed or pod['kind'] != 'Pod' or
                    ref.get('kind') != 'Pod' or ref.get('namespace') != namespace or
                    ref.get('name') != pod['metadata']['name'] or
                    any(pod['metadata'].get('labels', {}).get(k) != value
                        for k, value in service['spec']['selector'].items())):
                return False
    return True


def admission_secret(obj, objects, allowed, row, hooks):
    """Certgen creates an unlabelled Secret consumed by the owned TLS workload."""
    meta = obj['metadata']; name = 'prometheus-kube-prometheus-admission'
    if (meta['name'] != name or meta.get('namespace') != row['namespace'] or
            meta.get('ownerReferences') or obj.get('type') != 'Opaque' or
            set(obj.get('data', {})) != {'ca', 'cert', 'key'}):
        return False
    original = row.get('admission_secret_uid')
    if original:
        if meta['uid'] != original:
            raise ValueError('monitoring admission Secret UID changed')
        return True
    run = hooks.get(name+'-create')
    if not run or not run['start'] <= timestamp(meta['creationTimestamp']) <= run['end']:
        return False
    return any(uid in allowed and obj['kind'] == 'Deployment' and
               obj['metadata'].get('namespace') == row['namespace'] and
               obj['metadata']['name'] == 'prometheus-kube-prometheus-operator-webhook' and
               any(volume.get('secret', {}).get('secretName') == name
                   for volume in obj.get('spec', {}).get('template', {}).get('spec', {}).get('volumes', []))
               for uid, obj in objects.items())


def namespace_graph(lab, row):
    """Prove all live objects descend from this Helm release or product roots."""
    identity = namespace_identity(lab, row)
    if not identity:
        return []
    namespace = row['namespace']
    resources = lab.kubectl('api-resources', '--verbs=list', '--namespaced=true', '-o', 'name').stdout.split()
    if not resources:
        raise ValueError('cannot discover monitoring namespace resources')
    objects = {}
    for kind in sorted(set(resources)):
        if kind == 'pods.metrics.k8s.io':
            continue
        for obj in lab.get(kind, namespace=namespace)['items']:
            uid = obj['metadata'].get('uid')
            if not uid:
                raise ValueError('monitoring object has no persistent ownership UID')
            objects[uid] = obj
    allowed = {row['monitor_namespace_uid']}
    hooks = admission_hooks(objects, row, identity)
    hook_events, hook_subjects = historical_hook_events(objects, row, hooks)
    event_subjects = set(row.get('known_object_uids', []))
    for uid, obj in objects.items():
        meta = obj['metadata']; annotations = meta.get('annotations') or {}
        if (obj['kind'] != 'Endpoints' and
                (obj['kind'], meta['name']) != ('Secret', 'prometheus-kube-prometheus-admission') and
                annotations.get('meta.helm.sh/release-name') == 'prometheus' and
                annotations.get('meta.helm.sh/release-namespace') == namespace):
            allowed.add(uid)
        elif obj['kind'] == 'Secret' and meta['name'].startswith('sh.helm.release.v1.prometheus.v'):
            if meta.get('labels', {}).get('owner') != 'helm' or meta.get('labels', {}).get('name') != 'prometheus':
                raise ValueError('foreign Helm release secret')
            allowed.add(uid)
        elif ((obj['kind'], meta['name']) in (('ServiceAccount', 'default'), ('ConfigMap', 'kube-root-ca.crt')) or
              obj['kind'] == 'Secret' and meta['name'] in ('grafana-admin', 'etcd-client-cert')):
            # These four known roots are created by namespace controllers or
            # the product inside the fresh, exclusively leased namespace.
            allowed.add(uid)
        elif obj['kind'] == 'PersistentVolumeClaim':
            labels = meta.get('labels', {})
            if (obj['spec'].get('storageClassName') == CLASS and
                (labels.get('app.kubernetes.io/instance') == 'prometheus' or
                 labels.get('app') in ('prometheus', 'alertmanager') or
                 labels.get('app.kubernetes.io/name') in ('prometheus', 'alertmanager'))):
                allowed.add(uid)
    while True:
        previous = set(allowed)
        for uid, obj in objects.items():
            refs = obj['metadata'].get('ownerReferences', [])
            if (obj['kind'] != 'Endpoints' and
                    (obj['kind'], obj['metadata']['name']) != ('Secret', 'prometheus-kube-prometheus-admission') and
                    any(r.get('uid') in allowed for r in refs)):
                allowed.add(uid)
            if obj['kind'] == 'Endpoints' and controller_endpoints(obj, objects, allowed):
                allowed.add(uid)
            if (obj['kind'] == 'Secret' and obj['metadata']['name'] == 'prometheus-kube-prometheus-admission' and
                    admission_secret(obj, objects, allowed, row, hooks)):
                allowed.add(uid)
            if obj['kind'] == 'Event' and (uid in hook_events or
                    obj.get('involvedObject', obj.get('regarding', {})).get('uid') in allowed | event_subjects):
                allowed.add(uid)
        if previous == allowed:
            break
    unknown = [o['kind']+'/'+o['metadata']['name'] for uid, o in objects.items() if uid not in allowed]
    if unknown:
        raise ValueError('foreign object in monitoring namespace: '+','.join(sorted(unknown)))
    row['known_object_uids'] = sorted(set(row.get('known_object_uids', [])) | set(objects))
    row['admission_hooks'] = hooks
    row['hook_event_subjects'] = hook_subjects
    for obj in objects.values():
        if obj['kind'] == 'Secret' and obj['metadata']['name'] == 'prometheus-kube-prometheus-admission':
            row['admission_secret_uid'] = obj['metadata']['uid']
    persist(lab, row)
    return list(objects.values())


def scrape_health(lab, prometheus):
    samples = prometheus.query('longhorn_node_status{condition="ready"}', allow_empty=True)
    return samples if len({s['labels'].get('node') for s in samples}) == 3 and all(s['value'] == 1 for s in samples) else None


def verify(lab, pid=0):
    row = receipt(lab)
    namespace_identity(lab, row)
    if (lab.DIRECTORY/'config.yml').read_text() != row.get('ready_inputs'):
        raise ValueError('monitoring product input changed since installation')
    namespace_graph(lab, row)
    prometheus = Prometheus(lab, row)
    lab.wait('two-product-prometheus-pods-ready', 1200, prometheus.pods)
    lab.wait('owned-longhorn-targets-scraped', 180, lambda: scrape_health(lab, prometheus))
    def all_ready():
        pods = lab.get('pods', namespace=row['namespace'])['items']
        if not pods:
            return False
        for pod in pods:
            if pod.get('status', {}).get('phase') == 'Succeeded':
                continue
            if pod.get('status', {}).get('phase') == 'Failed':
                raise ValueError('the product monitoring stack contains a failed Pod')
            if not load('longhorn-v2-control-faults.py').pod_ready(pod):
                return False
        return True
    lab.wait('full-product-monitoring-stack-ready', 1200, all_ready)
    placement(lab.get('nodes')['items'], lab.get('pods', namespace='*')['items'], [])
    host_memory(lab)
    sc = lab.get('storageclass', CLASS)
    if not sc or sc['metadata']['uid'] != row['storage_class_uid']:
        raise ValueError('monitoring StorageClass identity changed')
    claims = lab.wait('six-product-monitoring-pvcs-bound', 600, lambda:
        (claims if len(claims) == 6 and all(c.get('status', {}).get('phase') == 'Bound' for c in claims) else None)
        if (claims := lab.get('pvc', namespace=row['namespace'])['items']) else None)
    for claim in claims:
        if claim['spec'].get('storageClassName') != CLASS or claim.get('status', {}).get('phase') != 'Bound':
            raise ValueError('monitoring claim is not bound through the fixture class')
        pv = lab.get('pv', claim['spec']['volumeName'])
        if (not pv or pv['spec']['claimRef']['uid'] != claim['metadata']['uid'] or
                pv['spec'].get('persistentVolumeReclaimPolicy') != 'Delete'):
            raise ValueError('monitoring claim binding or reclaim policy differs')
        def volume_ready():
            volume = lab.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], NS)
            if volume and (volume['spec'].get('dataEngine') != DATA_ENGINE or volume['spec'].get('numberOfReplicas') != 1):
                raise ValueError('monitoring volume differs from the declared one-replica V2 scope')
            return volume and volume.get('status', {}).get('robustness') == 'healthy'
        lab.wait('monitoring-fixture-volume-healthy', 600, volume_ready)
    row['claims'] = {p['metadata']['name']: {'uid': p['metadata']['uid'], 'pv': p['spec']['volumeName'],
        'pv_uid': lab.get('pv', p['spec']['volumeName'])['metadata']['uid']} for p in claims}
    persist(lab, row)
    print(MARKER+'_VERIFY_PASS scope=HA-product-single-replica-test-PVCs', flush=True)


def cleanup(lab, pid):
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink():
        return
    row = receipt(lab)
    if (lab.DIRECTORY/'longhorn-install-business.json').exists():
        raise ValueError('observability workload must restore its faults and owned business before monitoring cleanup')
    lab.restore_request_input()
    current = (lab.DIRECTORY/'config.yml').read_text()
    if current not in row['allowed_inputs']:
        raise ValueError('monitoring cleanup refuses foreign input')
    namespace = namespace_identity(lab, row)
    objects = namespace_graph(lab, row) if namespace else []
    # Remove the product ServiceMonitor/network-policy reference before its
    # Prometheus namespace or CRD. Keep fixture storage declared until empty.
    original = yaml.safe_load(row['original_input'])
    interim = yaml.safe_load(current)
    for key in FIELDS:
        interim[key] = original[key]
    apply(lab, row, interim); product(lab, pid, 'longhorn-setup')
    if namespace:
        claims = [o for o in objects if o['kind'] == 'PersistentVolumeClaim']
        bindings = {}
        for claim in claims:
            if claim['spec'].get('storageClassName') != CLASS:
                raise ValueError('unowned claim prevents monitoring cleanup')
            name = claim['spec'].get('volumeName')
            if not name:
                continue
            pv = lab.get('pv', name)
            if (not pv or pv['spec']['claimRef']['uid'] != claim['metadata']['uid'] or
                    pv['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
                    pv['spec'].get('storageClassName') != CLASS):
                raise ValueError('monitoring cleanup PVC/PV binding differs')
            bindings[name] = {'uid': pv['metadata']['uid'], 'volume': pv['spec']['csi']['volumeHandle']}
        release = lab.execute([str(lab.BASE/'extra-bin/helm'), 'status', 'prometheus', '-n', row['namespace'],
            '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig'), '-o', 'json'], check=False)
        if release.returncode == 0:
            namespace_graph(lab, row)
            lab.execute([str(lab.BASE/'extra-bin/helm'), 'uninstall', 'prometheus', '-n', row['namespace'],
                '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig'), '--wait', '--timeout', '10m'], timeout=660)
        elif 'release: not found' not in release.stderr:
            raise ValueError('cannot verify monitoring Helm release before cleanup')
        lab.wait('monitoring-pods-gone', 600, lambda: not lab.get('pods', namespace=row['namespace'])['items'])
        for claim in claims:
            obj = lab.get('pvc', claim['metadata']['name'], row['namespace'])
            if obj:
                if obj['metadata']['uid'] != claim['metadata']['uid']:
                    raise ValueError('monitoring PVC replaced before cleanup')
                lab.remove(obj)
        for name, bound in bindings.items():
            def gone():
                pv = lab.get('pv', name)
                if pv and pv['metadata']['uid'] != bound['uid']:
                    raise ValueError('monitoring PV replaced during CSI Delete')
                return not pv and not lab.get('volumes.longhorn.io', bound['volume'], NS)
            lab.wait('monitoring-csi-delete-'+name, 600, gone)
        namespace_graph(lab, row)
        lab.remove(namespace_identity(lab, row))
        lab.wait('monitoring-namespace-gone', 180, lambda: namespace_identity(lab, row) is None)
    apply(lab, row, original); product(lab, pid, 'longhorn-setup')
    # The Longhorn product creates declared classes but does not remove a
    # removed declaration. Delete only this captured immutable fixture class.
    sc = lab.get('storageclass', CLASS)
    if sc:
        if sc['metadata']['uid'] != row.get('storage_class_uid') or any(
                p['spec'].get('storageClassName') == CLASS for p in lab.get('pvc', namespace='*')['items']):
            raise ValueError('monitoring class replaced or still has consumers')
        lab.remove(sc)
    for name in row['expected_crds']:
        obj = lab.get('crd', name)
        if not obj or name in row['original_crds']:
            continue
        if obj['metadata']['uid'] != row.get('created_crds', {}).get(name):
            raise ValueError('monitoring CRD replaced or has no successful create receipt')
        if not name.endswith('.monitoring.coreos.com') or lab.get(name, namespace='*')['items']:
            raise ValueError('monitoring CRD has foreign consumers')
        lab.remove(obj)
    if (lab.DIRECTORY/'config.yml').read_text() != yaml.safe_dump(original, sort_keys=False):
        raise ValueError('monitoring input restoration differs')
    # Restore the exact byte representation as well as semantic input.
    load('longhorn-v2-disk-faults.py').atomic(lab.DIRECTORY/'config.yml', row['original_input'])
    lab.installation_health()
    path.unlink()
    print(MARKER+'_SCOPED_CLEAN_PASS', flush=True)


class Prometheus:
    def __init__(self, lab, receipt):
        self.lab = lab; self.receipt = receipt

    def pods(self):
        namespace = self.lab.get('namespace', self.receipt['namespace'])
        if not namespace or namespace['metadata']['uid'] != self.receipt['monitor_namespace_uid']:
            raise ValueError('monitoring namespace identity changed')
        pods = [p for p in self.lab.get('pods', namespace=self.receipt['namespace'])['items']
                if p['metadata'].get('labels', {}).get('app.kubernetes.io/name') == 'prometheus']
        if len(pods) != 2 or any(not load('longhorn-v2-control-faults.py').pod_ready(p) for p in pods):
            return None
        return sorted(pods, key=lambda p: p['metadata']['name'])

    def api(self, endpoint, params=None):
        pods = self.pods()
        if not pods:
            raise ValueError('the two product Prometheus replicas are not ready')
        pod = pods[0]
        path = '/api/v1/namespaces/'+self.receipt['namespace']+'/pods/'+pod['metadata']['name']+':9090/proxy'+endpoint
        if params: path += '?'+urlencode(params)
        row = json.loads(self.lab.kubectl('get', '--raw='+path, timeout=30).stdout)
        current = self.lab.get('pod', pod['metadata']['name'], self.receipt['namespace'])
        if not current or current['metadata']['uid'] != pod['metadata']['uid']:
            raise ValueError('Prometheus Pod changed during observation')
        return row

    def query(self, expression, *, allow_empty=False):
        return sample_vector(self.api('/api/v1/query', {'query': expression}), time.time(), allow_empty=allow_empty)

    def alert(self, name, active):
        data = self.api('/api/v1/alerts')
        if data.get('status') != 'success':
            raise ValueError('Prometheus alerts endpoint failed')
        rows = [a for a in data['data']['alerts'] if a['labels'].get('alertname') == name]
        if active:
            return rows if rows and all(r['state'] == 'firing' for r in rows) else None
        return not rows


def runtime_cache_proof(action, enabled):
    """Consume a bounded hash-bound runtime input only on explicit setup repair."""
    if not enabled:
        return None
    if action != 'setup' or os.environ.get('LONGHORN_MONITORING_CACHE_RECONCILE_FOCUS') != 'v2-observability-monitoring':
        raise ValueError('cache reconcile is restricted to explicit focused monitoring setup')
    payload = os.environ.get('LONGHORN_MONITORING_CACHE_RECONCILE_PAYLOAD', '')
    expected = os.environ.get('LONGHORN_MONITORING_CACHE_RECONCILE_SHA256', '')
    try:
        if not payload or len(payload) > 43692 or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('missing or oversized runtime proof')
        raw = base64.b64decode(payload, validate=True)
        if len(raw) > 32768 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('runtime proof size or SHA256 differs')
        proof = json.loads(raw)
        if not isinstance(proof, dict):
            raise ValueError('runtime proof must be a JSON object')
    except (ValueError, UnicodeError) as error:
        raise ValueError('invalid focused cache reconcile runtime proof') from error
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('admission', 'setup', 'cleanup', 'verify'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--reconcile-cache', action='store_true')
    args = parser.parse_args()
    proof = runtime_cache_proof(args.action, args.reconcile_cache)
    _, lab = load('longhorn-v2-install.py').fixture()
    lab.require_lease()
    if args.action == 'setup':
        setup(lab, args.relay_pid, cache_reconcile=proof)
    else:
        globals()[args.action](lab, args.relay_pid)


if __name__ == '__main__':
    main()
