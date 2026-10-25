#!/usr/bin/env python3
"""LONGHORN-24: PDB-protected planned drain with immutable I/O receipts.

Longhorn manager v1.12.1 (8320238242e8c804325d9588ec1db10feb9f36b8),
InstanceManagerController.canDeleteInstanceManagerPDB: last healthy replicas
need a PDB-protected peer on a schedulable node; attached engines also block.
Never bypass eviction or alter a PDB to make maintenance succeed.
"""
import argparse
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import secrets
import signal
import sys

NS = 'longhorn-system'
RECORD = 'longhorn-maintenance.json'
BUSINESS = 'longhorn-maintenance-business.json'
POLICY = 'block-if-contains-last-replica'
DATA_ENGINE = 'v1'
STORAGE_CLASS = 'longhorn'


def empty(lab):
    if DATA_ENGINE == 'v1':
        lab.load_helper('longhorn-platform.py').empty(lab)
    elif DATA_ENGINE == 'v2':
        lab.load_helper('longhorn-v2-install.py').volume_baseline(lab)
    else:
        raise ValueError('unsupported maintenance data engine')


def ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and any(
        c.get('type') == 'Ready' and c.get('status') == 'True'
        for c in pod.get('status', {}).get('conditions', [])))


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink maintenance journal')
    row = json.loads(path.read_text())
    if row['run_id'] != lab.owner()['run_id']:
        raise ValueError('foreign maintenance journal')
    return row


def remember(lab, row):
    lab.write_private(lab.DIRECTORY/RECORD, row)


def node(lab, row, name):
    if name not in row['nodes']:
        raise ValueError('maintenance node is outside the worker lease')
    obj = lab.get('node', name)
    saved = row['nodes'][name]
    # The node controller mirrors cordon with this built-in taint. It is
    # restored asynchronously; do not mistake it for a foreign user taint.
    taints = [t for t in obj.get('spec', {}).get('taints', []) if not (
        t.get('key') == 'node.kubernetes.io/unschedulable' and t.get('effect') == 'NoSchedule')] if obj else []
    if (not obj or obj['metadata']['uid'] != saved['uid'] or taints != saved['taints']):
        raise ValueError('maintenance node identity or taints changed')
    return obj


def scheduling(lab, row, name, unschedulable):
    obj = node(lab, row, name)
    if bool(obj['spec'].get('unschedulable', False)) != unschedulable:
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                 {'op': 'add', 'path': '/spec/unschedulable', 'value': unschedulable}]
        lab.kubectl('patch', 'node', name, '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
    return node(lab, row, name)


def policy(lab, row):
    obj = lab.get('settings.longhorn.io', 'node-drain-policy', NS)
    if not obj or {'uid': obj['metadata']['uid'], 'value': obj.get('value')} != row['policy']:
        raise ValueError('maintenance drain policy identity or value changed')
    return obj


def scale(lab, business, count):
    obj = lab.get('volumes.longhorn.io', business['volume'], NS)
    if not obj or obj['metadata']['uid'] != business['volume_uid']:
        raise ValueError('maintenance volume identity changed')
    original = obj['spec']['numberOfReplicas']
    if original != count:
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': business['volume_uid']},
                 {'op': 'test', 'path': '/spec/numberOfReplicas', 'value': original},
                 {'op': 'replace', 'path': '/spec/numberOfReplicas', 'value': count}]
        lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', business['volume'],
                    '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
    return lab.wait('maintenance-healthy-replicas-'+str(count), 900,
                    lambda: lab.replica_topology(business, count))


def temporary_replica_balance(lab, business, row):
    """Enable native reduction only on this fixture volume; never alter a global setting."""
    obj = lab.get('volumes.longhorn.io', business['volume'], NS)
    if (DATA_ENGINE not in ('v1', 'v2') or not obj or obj['metadata']['uid'] != business['volume_uid'] or
            obj['spec'].get('dataEngine') != DATA_ENGINE or row.get('replica_balance')):
        raise ValueError('maintenance replica balance lacks its original volume')
    original = obj['spec'].get('replicaAutoBalance')
    if original not in ('ignored', 'disabled', 'least-effort', 'best-effort'):
        raise ValueError('maintenance replica balance has an unknown original policy')
    row['replica_balance'] = {'volume': business['volume'], 'uid': business['volume_uid'],
                              'original': original, 'temporary': 'least-effort'}
    remember(lab, row)  # Persist intent before any API response can be lost.
    if original != 'least-effort':
        lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', business['volume'],
            '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': business['volume_uid']},
                {'op': 'test', 'path': '/spec/replicaAutoBalance', 'value': original},
                {'op': 'replace', 'path': '/spec/replicaAutoBalance', 'value': 'least-effort'}]))


def restore_replica_balance(lab, business, row):
    saved = row.get('replica_balance')
    if not saved:
        return  # Original journals predate the temporary-policy fixture.
    obj = lab.get('volumes.longhorn.io', business['volume'], NS)
    if (saved['volume'] != business['volume'] or saved['uid'] != business['volume_uid'] or
            not obj or obj['metadata']['uid'] != saved['uid'] or
            obj['spec'].get('replicaAutoBalance') not in (saved['original'], saved['temporary'])):
        raise ValueError('maintenance replica balance ownership or policy changed')
    if obj['spec']['replicaAutoBalance'] != saved['original']:
        lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', business['volume'],
            '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': saved['uid']},
                {'op': 'test', 'path': '/spec/replicaAutoBalance', 'value': saved['temporary']},
                {'op': 'replace', 'path': '/spec/replicaAutoBalance', 'value': saved['original']}]))
    current = lab.get('volumes.longhorn.io', business['volume'], NS)
    if (not current or current['metadata']['uid'] != saved['uid'] or
            current['spec'].get('replicaAutoBalance') != saved['original']):
        raise ValueError('maintenance replica balance restoration did not converge')


def generated(lab, business):
    """Bind only the Deployment -> ReplicaSet -> Pod controller UID chain.

    Keep retired Pod UIDs too: their Events may remain until namespace cleanup.
    A same-name replacement is never silently admitted.
    """
    deployment_uid = business['objects'].get('Deployment/application')
    if not deployment_uid:
        return []
    ns = business['namespace']; replica_sets = set(); pods = []
    for kind in ('ReplicaSet', 'Pod'):
        parents = {deployment_uid} if kind == 'ReplicaSet' else replica_sets
        for obj in lab.get(kind.lower(), namespace=ns)['items']:
            references = [r for r in obj['metadata'].get('ownerReferences', []) if r.get('controller')]
            if len(references) != 1 or references[0].get('uid') not in parents:
                # Bare clients have their own create receipt; foreign objects
                # remain unowned and check_business_graph refuses cleanup.
                continue
            expected = 'Deployment' if kind == 'ReplicaSet' else 'ReplicaSet'
            if references[0].get('kind') != expected:
                raise ValueError('maintenance generated object controller kind differs')
            key = kind+'/'+obj['metadata']['name']; uid = obj['metadata']['uid']
            if key in business['objects'] and business['objects'][key] != uid:
                raise ValueError('maintenance generated object was replaced')
            business['objects'][key] = uid
            if kind == 'ReplicaSet': replica_sets.add(uid)
            else: pods.append(obj)
    lab.write_private(lab.DIRECTORY/BUSINESS, business)
    lab.check_business_graph(business)
    return pods


def application(lab, business, *, excluded=None):
    candidates = [p for p in generated(lab, business) if ready(p)]
    if len(candidates) != 1:
        return None
    pod = candidates[0]
    if excluded and pod['spec'].get('nodeName') == excluded:
        return None
    if pod['spec'].get('nodeName') not in {
            'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in lab.WORKERS}:
        raise ValueError('maintenance application left the leased workers')
    return pod


def check_data(lab, business, client, row):
    actual = lab.kubectl('-n', business['namespace'], 'exec', client, '--', 'python3', '-c',
                         lab.payload_program(row['payload'], read_only=True)).stdout.strip()
    if actual != row['digest']:
        raise ValueError('maintenance changed acknowledged business bytes')
    if row.get('ledger'):
        lab.load_helper('longhorn-faults.py').verify_client_ledger(lab, business, client, row['ledger'])


def ledger(lab, business, client, row):
    helper = lab.load_helper('longhorn-faults.py')
    token = row['run_id']+':maintenance'
    # Bounded acknowledged ledger, completed before eviction. It deliberately
    # makes no claim about uninterrupted writes while Kubernetes moves a Pod.
    code = ('import errno,hashlib,json,os,stat,time\nBLOCK=65536\n' +
            '\n'.join(inspect.getsource(function) for function in
                      (helper.ledger_block, helper.verify_ledger, helper.write_ledger)) +
            '\nwrite_ledger("/data/fault-ledger",%r,duration=3)\n' % token)
    output = lab.kubectl('-n', business['namespace'], 'exec', client, '--',
                         'python3', '-u', '-c', code, timeout=60).stdout
    records = [json.loads(line) for line in output.splitlines()]
    acknowledgements = [r for r in records if r.get('type') == 'ack']
    complete = [r for r in records if r.get('type') == 'complete']
    if (len(complete) != 1 or len(acknowledgements) < 4 or complete[0]['retries'] or
            complete[0]['confirmed'] != len(acknowledgements)):
        raise ValueError('maintenance has no complete immutable fsync ledger')
    row['ledger'] = {'token': token, 'records': acknowledgements,
                     'summary': {k: complete[0][k] for k in ('confirmed', 'verified_bytes', 'sha256')}}
    remember(lab, row)
    print('LONGHORN_MAINTENANCE_ACKNOWLEDGEMENTS '+json.dumps(row['ledger'], sort_keys=True), flush=True)
    check_data(lab, business, client, row)


def replica_manager(lab, business, placement):
    name, saved = next(iter(placement.items()))
    replica = lab.get('replicas.longhorn.io', name, NS)
    if (not replica or replica['metadata']['uid'] != saved['uid'] or
            replica['spec'].get('volumeName') != business['volume'] or not any(
                r.get('kind') == 'Volume' and r.get('uid') == business['volume_uid'] and
                r.get('name') == business['volume'] for r in replica['metadata'].get('ownerReferences', []))):
        raise ValueError('maintenance last replica identity changed')
    manager = lab.get('instancemanagers.longhorn.io', replica['status']['instanceManagerName'], NS)
    pod = lab.get('pod', manager['metadata']['name'], NS) if manager else None
    if (not manager or manager['spec'].get('nodeID') != saved['node'] or
            manager['spec'].get('dataEngine') != DATA_ENGINE or not ready(pod) or
            pod['spec'].get('nodeName') != saved['node'] or
            not any(r.get('kind') == 'InstanceManager' and r.get('uid') == manager['metadata']['uid']
                    for r in pod['metadata'].get('ownerReferences', []))):
        raise ValueError('maintenance last replica instance-manager chain differs')
    return manager, pod


def pdb(lab, pod):
    matches = []
    for obj in lab.get('pdb', namespace=NS)['items']:
        selector = obj.get('spec', {}).get('selector', {})
        labels = selector.get('matchLabels', {})
        if selector.get('matchExpressions') or not labels:
            continue
        if all(pod['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()):
            matches.append(obj)
    if len(matches) != 1:
        return None
    obj = matches[0]
    if (obj.get('status', {}).get('observedGeneration') != obj['metadata'].get('generation') or
            obj.get('status', {}).get('disruptionsAllowed') != 0 or obj['spec'].get('minAvailable') != 1):
        return None
    return obj


def drain(lab, name, *, pod_selector=None, refusal=False):
    args = ['drain', name, '--ignore-daemonsets', '--delete-emptydir-data',
            '--timeout='+('25s' if refusal else '900s')]
    if pod_selector:
        args += ['--pod-selector='+pod_selector]
    result = lab.kubectl(*args, check=False, timeout=60 if refusal else 960)
    print('LONGHORN_MAINTENANCE_DRAIN_RESULT '+json.dumps({
        'node': name, 'refusal_expected': refusal, 'rc': result.returncode,
        'stdout': result.stdout, 'stderr': result.stderr}, sort_keys=True), flush=True)
    if refusal:
        output = (result.stdout+'\n'+result.stderr).lower()
        if (result.returncode == 0 or 'disruption budget' not in output or
                ('cannot evict' not in output and 'would violate' not in output)):
            raise ValueError('maintenance did not observe the actual PDB eviction refusal')
    elif result.returncode:
        raise ValueError('maintenance allowed drain failed; host maintenance is forbidden')


def drained(lab, business, name, original_uid):
    pod = application(lab, business, excluded=name)
    if not pod or pod['metadata']['uid'] == original_uid:
        return None
    volume = lab.get('volumes.longhorn.io', business['volume'], NS)
    if not volume or volume['metadata']['uid'] != business['volume_uid']:
        raise ValueError('maintenance volume changed during drain')
    if (volume.get('status', {}).get('currentNodeID') != pod['spec']['nodeName'] or
            volume['status'].get('state') != 'attached' or volume['status'].get('robustness') not in ('healthy', 'degraded')):
        return None
    peers = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
             if r['spec'].get('volumeName') == business['volume'] and r['spec'].get('nodeID') != name
             and r.get('status', {}).get('currentState') == 'running' and r['spec'].get('healthyAt')
             and not r['spec'].get('failedAt')]
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items']
               if e['spec'].get('volumeName') == business['volume'] and
               e['spec'].get('nodeID') == pod['spec']['nodeName'] and
               e.get('status', {}).get('currentState') == 'running']
    peer_nodes = {p['spec'].get('nodeID') for p in peers}
    allowed = {'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in lab.WORKERS}
    if (len(peer_nodes) < 2 or not peer_nodes <= allowed-{name} or len(engines) != 1 or
            any(engines[0].get('status', {}).get('replicaModeMap', {}).get(p['metadata']['name']) != 'RW'
                for p in peers)):
        return None
    attachments = [a for a in lab.get('volumeattachments.storage.k8s.io')['items']
                   if a.get('spec', {}).get('source', {}).get('persistentVolumeName') == business['pv']]
    active = [a for a in attachments if a.get('status', {}).get('attached')]
    if (len(active) != 1 or active[0]['spec'].get('attacher') != 'driver.longhorn.io' or
            active[0]['spec'].get('nodeName') != pod['spec']['nodeName']):
        return None
    for kind in ('engines.longhorn.io', 'replicas.longhorn.io'):
        if any(o['spec'].get('nodeID') == name and o.get('status', {}).get('currentState') == 'running'
               for o in lab.get(kind, namespace=NS)['items']):
            return None
    for manager in lab.get('instancemanagers.longhorn.io', namespace=NS)['items']:
        if manager['spec'].get('nodeID') != name: continue
        if any(manager.get('status', {}).get(k) for k in ('instanceEngines', 'instanceReplicas', 'instanceEngineFrontends')):
            return None
        p = lab.get('pod', manager['metadata']['name'], NS)
        if p and p.get('status', {}).get('phase') in ('Running', 'Pending'):
            return None
    return pod


def original_object(lab, business, kind, name):
    obj = lab.get(kind.lower(), name, business['namespace'])
    if obj and obj['metadata']['uid'] != business['objects'].get(kind+'/'+name):
        raise ValueError('maintenance object replaced before recovery or cleanup')
    return obj


def retire_controllers(lab, business):
    generated(lab, business)
    # Kubernetes background GC removes ReplicaSets and Pods asynchronously.
    # Delete only their original Deployment; never race GC with a second DELETE.
    for key in list(business['objects']):
        if not key.startswith('Deployment/'): continue
        name = key.split('/', 1)[1]
        obj = original_object(lab, business, 'Deployment', name)
        if obj and not obj['metadata'].get('deletionTimestamp'):
            lab.remove(obj)
        lab.wait('maintenance-controller-retired', 180, lambda:
                 original_object(lab, business, 'Deployment', name) is None)
    for key in list(business['objects']):
        if not key.startswith('ReplicaSet/'): continue
        name = key.split('/', 1)[1]
        lab.wait('maintenance-replicaset-retired', 180, lambda:
                 original_object(lab, business, 'ReplicaSet', name) is None)
    def dependents_retired():
        lab.check_business_graph(business)
        parents = {uid for key, uid in business['objects'].items() if key.startswith('ReplicaSet/')}
        pending = False
        for pod in lab.get('pod', namespace=business['namespace'])['items']:
            if pod['metadata']['uid'] != business['objects'].get('Pod/'+pod['metadata']['name']):
                raise ValueError('maintenance Pod replaced or foreign during controller retirement')
            if any(ref.get('kind') == 'ReplicaSet' and ref.get('uid') in parents
                   for ref in pod['metadata'].get('ownerReferences', [])):
                pending = True
        return not pending
    lab.wait('maintenance-generated-pods-retired', 180, dependents_retired)


def recovery_client(lab, business):
    if 'Deployment/application' in business['objects']:
        deployment = original_object(lab, business, 'Deployment', 'application')
        if deployment and not deployment['metadata'].get('deletionTimestamp'):
            return lab.wait('maintenance-recovery-application', 600, lambda: application(lab, business))
        # A retired controller cannot produce a Ready application. Complete its
        # GC boundary before attaching a surviving or newly created bare client.
        retire_controllers(lab, business)
    lab.check_business_graph(business)
    clients = [p for p in lab.get('pod', namespace=business['namespace'])['items'] if ready(p)
               and business['objects'].get('Pod/'+p['metadata']['name']) == p['metadata']['uid']]
    if len(clients) > 1:
        raise ValueError('maintenance recovery has multiple owned Ready clients')
    return clients[0] if clients else None


def clean(lab, relay_pid, *, failed=False):
    path = lab.DIRECTORY/RECORD
    if not path.exists(): return
    row = receipt(lab); policy(lab, row)
    # Restore the fault/scheduling boundary before any data fixture deletion.
    for name in row.get('touched', []):
        scheduling(lab, row, name, row['nodes'][name]['unschedulable'])
    record = lab.DIRECTORY/BUSINESS
    if record.is_symlink(): raise ValueError('symlink maintenance business journal')
    if not record.exists() and row.get('namespace_intent'):
        intent = row['namespace_intent']; ns = lab.get('namespace', intent['name'])
        if ns:
            if ns['metadata'].get('annotations', {}).get('kubeauto.io/maintenance-token') != intent['token']:
                raise ValueError('maintenance namespace intent token differs')
            recovered = {'namespace': intent['name'], 'namespace_uid': ns['metadata']['uid'], 'objects': {},
                         'storage_class': STORAGE_CLASS, 'reclaim_policy': 'Retain',
                         'defaults': lab.wait('maintenance-recovered-defaults', 60,
                                              lambda: lab.namespace_defaults(intent['name']))}
            lab.write_private(record, recovered)
    if record.exists():
        business = json.loads(record.read_text())
        if row.get('digest'):
            client = recovery_client(lab, business)
            if client is None:
                name = 'recovery-'+secrets.token_hex(6)
                lab.create_business_object(business, record, lab.client_pod(
                    business['namespace'], name, 'data', next(iter(row['nodes']))))
                lab.wait('maintenance-recovery-client', 600, lambda:
                         ready(original_object(lab, business, 'Pod', name)))
                client = original_object(lab, business, 'Pod', name)
            scale(lab, business, 3)
            restore_replica_balance(lab, business, row)
            check_data(lab, business, client['metadata']['name'], row)
        retire_controllers(lab, business)
        lab.cleanup_business(record, relay_pid=relay_pid)
    lab.installation_health()
    if failed:
        row['recovered'] = True; remember(lab, row)
        path.rename(lab.DIRECTORY/('longhorn-maintenance-recovered-'+secrets.token_hex(8)+'.json'))


def prepare(lab, row):
    namespace = 'longhorn-business-'+secrets.token_hex(8)
    record = lab.DIRECTORY/BUSINESS
    if record.exists(): raise ValueError('previous maintenance business needs scoped recovery')
    # Persist intent before create. A lost response is recovered only through
    # this per-run namespace token, never a namespace-name prefix alone.
    token = secrets.token_hex(16); row['namespace_intent'] = {'name': namespace, 'token': token}
    remember(lab, row)
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace,
        'annotations': {'kubeauto.io/maintenance-token': token}}})).stdout)
    business = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
                'storage_class': STORAGE_CLASS, 'reclaim_policy': 'Retain'}
    lab.write_private(record, business)
    business['defaults'] = lab.wait('maintenance-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
    lab.write_private(record, business)
    def create(obj): return lab.create_business_object(business, record, obj)
    create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'data', 'namespace': namespace},
        'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': STORAGE_CLASS,
                 'resources': {'requests': {'storage': '2Gi'}}}})
    def bound():
        claim = lab.get('pvc', 'data', namespace)
        if not claim or claim.get('status', {}).get('phase') != 'Bound': return None
        lab.bind_business_volume(business, record)
        return business if business.get('volume_uid') else None
    lab.wait('maintenance-business-bound', 600, bound)
    create(lab.client_pod(namespace, 'writer', 'data', 'longhorn-node-'+lab.WORKERS[0].rsplit('.', 1)[1]))
    lab.wait('maintenance-writer', 600, lambda: ready(lab.get('pod', 'writer', namespace)))
    row['payload'] = row['run_id']
    row['digest'] = lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c',
                               lab.payload_program(row['payload'])).stdout.strip()
    if row['digest'] != hashlib.sha256(row['payload'].encode()*262144).hexdigest():
        raise ValueError('maintenance initial fsync acknowledgement differs')
    remember(lab, row)
    return business, create


def business(lab, relay_pid):
    empty(lab)
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('previous maintenance needs scoped recovery')
    # Explicit product path, retaining the accepted idle runner-owned baseline.
    lab.supervise_core(relay_pid, 'longhorn-setup'); lab.installation_health()
    setting = lab.get('settings.longhorn.io', 'node-drain-policy', NS)
    if not setting or setting.get('value') != POLICY:
        raise ValueError('maintenance requires the unchanged product drain policy')
    nodes = {}
    for ip in lab.WORKERS:
        name = 'longhorn-node-'+ip.rsplit('.', 1)[1]; obj = lab.get('node', name)
        if not obj or obj['spec'].get('unschedulable') or obj['spec'].get('taints'):
            raise ValueError('maintenance requires schedulable untainted receiving workers')
        nodes[name] = {'uid': obj['metadata']['uid'], 'unschedulable': False, 'taints': []}
    row = {'run_id': lab.owner()['run_id'], 'nodes': nodes, 'touched': [], 'passed': False,
           'policy': {'uid': setting['metadata']['uid'], 'value': setting['value']},
           'config_sha256': hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest(),
           'shared': lab.shared_snapshot_identity()}
    remember(lab, row)
    with lab.scoped_cleanup(lambda: clean(lab, relay_pid)):
        fixture, create = prepare(lab, row); ns = fixture['namespace']
        scale(lab, fixture, 3)
        temporary_replica_balance(lab, fixture, row)
        single = scale(lab, fixture, 1)
        target = next(iter(single.values()))['node']; manager, im_pod = replica_manager(lab, fixture, single)
        client = 'writer'; writer = lab.get('pod', client, ns)
        if writer['spec']['nodeName'] == target:
            lab.remove(writer); lab.wait('maintenance-writer-detached', 180, lambda: lab.get('pod', client, ns) is None)
            client = 'refusal-reader'; receiving = next(n for n in nodes if n != target)
            create(lab.client_pod(ns, client, 'data', receiving))
            lab.wait('maintenance-refusal-reader', 600, lambda: ready(lab.get('pod', client, ns)))
        check_data(lab, fixture, client, row); ledger(lab, fixture, client, row)
        lab.wait('maintenance-last-replica-without-local-engine', 180, lambda:
                 lab.get('volumes.longhorn.io', fixture['volume'], NS)['status'].get('currentNodeID') != target)
        original_pdb = lab.wait('maintenance-last-replica-pdb', 120, lambda: pdb(lab, im_pod))
        row['touched'].append(target); remember(lab, row)
        selector = im_pod['metadata'].get('labels', {})
        if not selector: raise ValueError('maintenance instance-manager has no selectable identity')
        selected = [p for p in lab.get('pod', namespace=NS)['items'] if all(
            p['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())]
        if len(selected) != 1 or selected[0]['metadata']['uid'] != im_pod['metadata']['uid']:
            raise ValueError('maintenance refusal selector is not the unique protected instance')
        drain(lab, target, pod_selector=','.join(k+'='+v for k, v in sorted(selector.items())), refusal=True)
        current_pod = lab.get('pod', im_pod['metadata']['name'], NS)
        current_pdb = pdb(lab, im_pod)
        if (not current_pod or current_pod['metadata']['uid'] != im_pod['metadata']['uid'] or
                not current_pdb or current_pdb['metadata']['uid'] != original_pdb['metadata']['uid'] or
                lab.replica_topology(fixture, 1) != single):
            raise ValueError('PDB refusal changed the last healthy replica or protected instance')
        check_data(lab, fixture, client, row); scheduling(lab, row, target, False)
        restored = scale(lab, fixture, 3)
        restore_replica_balance(lab, fixture, row)
        print('LONGHORN_MAINTENANCE_LAST_REPLICA_REFUSAL_PASS '+json.dumps({
            'node': target, 'pdb_uid': current_pdb['metadata']['uid'], 'replicas': single,
            'restored': restored}, sort_keys=True), flush=True)
        old = lab.get('pod', client, ns); lab.remove(old)
        lab.wait('maintenance-bare-client-retired', 180, lambda: lab.get('pod', client, ns) is None)
        template = lab.client_pod(ns, 'unused', 'data', target)['spec']
        template.pop('nodeSelector'); template['restartPolicy'] = 'Always'
        template['affinity'] = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchExpressions': [{'key': 'kubernetes.io/hostname', 'operator': 'In',
                'values': list(nodes)}]}]}}}
        create({'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'application', 'namespace': ns},
            'spec': {'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': 'maintenance'}},
                     'template': {'metadata': {'labels': {'app': 'maintenance'}}, 'spec': template}}})
        before = lab.wait('maintenance-controller-application', 600, lambda: application(lab, fixture))
        check_data(lab, fixture, before['metadata']['name'], row)
        target = before['spec']['nodeName']
        if target not in row['touched']: row['touched'].append(target)
        remember(lab, row); policy(lab, row); node(lab, row, target)
        drain(lab, target)
        after = lab.wait('maintenance-workload-and-instances-moved', 600,
                         lambda: drained(lab, fixture, target, before['metadata']['uid']))
        check_data(lab, fixture, after['metadata']['name'], row)
        row['migration'] = {'node': target, 'before': {'uid': before['metadata']['uid'], 'node': target},
                            'after': {'uid': after['metadata']['uid'], 'node': after['spec']['nodeName']}}
        scheduling(lab, row, target, False); scale(lab, fixture, 3)
        check_data(lab, fixture, after['metadata']['name'], row)
        row['passed'] = True; remember(lab, row)
        print('LONGHORN_MAINTENANCE_MIGRATION_VERIFIED '+json.dumps(row['migration'], sort_keys=True), flush=True)
    print('LONGHORN_MAINTENANCE_BUSINESS_PASS', flush=True)


def finish(lab):
    row = receipt(lab)
    if not row['passed'] or (lab.DIRECTORY/BUSINESS).exists():
        raise ValueError('maintenance business or fixture cleanup incomplete')
    policy(lab, row)
    for name, saved in row['nodes'].items():
        def original_node():
            obj = node(lab, row, name)
            return (bool(obj['spec'].get('unschedulable', False)) == saved['unschedulable'] and
                    obj['spec'].get('taints', []) == saved['taints'])
        lab.wait('maintenance-original-node-scheduling', 120, original_node)
    if (lab.shared_snapshot_identity() != row['shared'] or
            hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != row['config_sha256']):
        raise ValueError('maintenance changed shared objects or original product input')
    empty(lab); lab.installation_health()
    (lab.DIRECTORY/RECORD).unlink()
    print('LONGHORN_MAINTENANCE_FIXTURES_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('business', 'clean', 'finish'))
    parser.add_argument('--relay-pid', type=int, default=0); args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('longhorn_maintenance_lab', Path(__file__).with_name('longhorn-lab.py'))
    lab = importlib.util.module_from_spec(spec); sys.modules[spec.name] = lab; spec.loader.exec_module(lab)
    lab.require_lease()
    if args.action == 'business': business(lab, args.relay_pid)
    elif args.action == 'clean': clean(lab, args.relay_pid, failed=True)
    else: finish(lab)


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    main()
