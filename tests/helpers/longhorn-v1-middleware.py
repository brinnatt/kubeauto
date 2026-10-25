#!/usr/bin/env python3
"""LONGHORN-37: product PXC + six monitoring claims, native logical recovery.

This bounded integration uses three PXC members and three HAProxy instances.
It does not repeat PXC qualification, physical backup/PITR or performance GA.
"""
import argparse
import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-v1-middleware.json'
CLASS = 'longhorn-middleware-three-replica'
DATA_ENGINE = 'v1'
STORAGE_NODES = ('192.168.122.212','192.168.122.165','192.168.122.238')
CLUSTER = 'longhorn-pxc'
PXC_CRDS = ('perconaxtradbclusters.pxc.percona.com', 'perconaxtradbclusterbackups.pxc.percona.com',
            'perconaxtradbclusterrestores.pxc.percona.com')


def load(name):
    spec = importlib.util.spec_from_file_location('v1_middleware_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def monitor():
    return load('longhorn-v1-observability.py').monitoring()


def disk_rows(lab):
    return load('longhorn-v1-observability.py').filesystem_rows(lab)


def emit(marker, row):
    print(marker+' '+json.dumps(row, sort_keys=True), flush=True)


def persist(lab, row):
    lab.write_private(lab.DIRECTORY/RECORD, row)


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('middleware refuses a symlink ownership receipt')
    row = json.loads(path.read_text())
    if any(row.get(k) != lab.owner()[k] for k in ('run_id', 'cluster_uid', 'namespace_uid')):
        raise ValueError('middleware ownership belongs to another storage installation')
    return row


def monitor_identity(lab, row):
    current = monitor().receipt(lab)
    identity = {k: current[k] for k in ('monitor_namespace_uid', 'storage_class_uid', 'claims')}
    if row.get('monitoring_identity') != identity:
        raise ValueError('middleware original monitoring namespace/class/PVC/PV identities changed')
    return current


def desired(original, namespace, operator_namespace):
    config = yaml.safe_load(original)
    if (config.get('mysql_install') != 'no' or config.get('longhorn_data_engines', ['v1']) != [DATA_ENGINE] or
            config.get('longhorn_requests') or not config.get('longhorn_monitoring_enabled') or
            config.get('prom_install') != 'yes' or any(c['name'] == CLASS for c in config['longhorn_storage_classes'])):
        raise ValueError('middleware requires its owned monitoring baseline and unused database branch')
    fixture = copy.deepcopy(config['longhorn_storage_classes'][0])
    fixture.update(name=CLASS, replicas=3, reclaim_policy='Delete', data_engine=DATA_ENGINE, default=False,
                   encrypted=False, encryption_secret='', recurring_jobs=[])
    config['longhorn_storage_classes'].append(fixture)
    # setup 07 reconciles every enabled addon. Keep the accepted monitoring
    # stack running while selecting only the database installation branch.
    config.update(prom_install='no', mysql_install='yes', mysql_namespace=namespace, mysql_operator_namespace=operator_namespace,
        mysql_cluster_name=CLUSTER, mysql_storage_class=CLASS, mysql_pvc_size='2Gi', mysql_pxc_size=3,
        mysql_haproxy_size=3, mysql_pxc_cpu_request='1', mysql_pxc_memory_request='2Gi', mysql_pxc_cpu_limit='3',
        mysql_pxc_memory_limit='4Gi', mysql_haproxy_cpu_request='100m', mysql_haproxy_memory_request='128Mi',
        mysql_haproxy_cpu_limit='1', mysql_haproxy_memory_limit='512Mi', mysql_gcache_size='128M',
        mysql_tls_enabled=True, mysql_logcollector_enabled=False, mysql_backup_enabled=False, mysql_pitr_enabled=False,
        mysql_secrets_name='', pxc_operator_ver='1.20.0', pxc_ver='8.4.8-8.1',
        pxc_xtrabackup_ver='8.4.0-5.1', pxc_haproxy_ver='2.8.18-1', pxc_fluentbit_ver='5.0.6-1')
    return config


def apply(lab, row, text):
    path = lab.DIRECTORY/'config.yml'
    if path.is_symlink() or path.read_text() not in row['allowed_inputs']:
        raise ValueError('middleware refuses a changed product configuration')
    if text not in row['allowed_inputs']: row['allowed_inputs'].append(text)
    persist(lab, row)
    load('longhorn-v2-disk-faults.py').atomic(path, text)


def planned(config):
    rows = []
    for component, size in (('pxc', 3), ('haproxy', 3)):
        resources = {kind: {resource: config['mysql_'+component+'_'+resource+'_'+suffix]
            for resource in ('cpu', 'memory')} for kind, suffix in (('requests', 'request'), ('limits', 'limit'))}
        for i in range(size):
            rows.append({'name': component+'-'+str(i), 'group': component,
                         'spec': {'containers': [{'resources': resources}]}})
    rows.append({'name': 'operator', 'group': '', 'spec': {'containers': [{'resources': {
        'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '512Mi'}}}]}})
    return rows


def admission(lab, pid):
    module = monitor(); module.verify(lab, pid)
    original = (lab.DIRECTORY/'config.yml').read_text()
    config = desired(original, 'longhorn-db-admission', 'longhorn-db-operator-admission')
    result = module.placement(lab.get('nodes')['items'], lab.get('pods', namespace='*')['items'], planned(config))
    hosts = module.host_memory(lab)
    for address in lab.WORKERS:
        if hosts[address]['MemAvailable'] < 5*1024**3:
            raise ValueError('middleware runtime requires five GiB available per storage worker before database startup')
    disk = disk_rows(lab)
    minimal = int(lab.get('settings.longhorn.io', 'storage-minimal-available-percentage', NS)['value'])
    if not 0 <= minimal <= 100: raise ValueError('invalid actual filesystem scheduling watermark')
    if any(min(r['available']-r['maximum']*minimal/100,
               r['maximum']-r['reserved']-r['scheduled']) < 10*1024**3 for r in disk.values()):
        raise ValueError('middleware lacks three two-GiB replicas plus four-GiB disk stop budget')
    emit('LONGHORN_MIDDLEWARE_ADMISSION', {'placement': result, 'host_memory': hosts,
        'test_sizing': {k: v for k, v in config.items() if k.startswith('mysql_') and ('request' in k or 'limit' in k)},
        'boundary': 'Bounded integration sizing, not the production database recommendation; current monitoring requests are included.'})


def cache(lab):
    publication = json.loads((lab.BASE/'tests/evidence/longhorn-pxc-images-20261021.json').read_text())
    if publication['durable_rc'] != 0 or publication['failure_markers'] != 0 or len(publication['images']) != 5:
        raise ValueError('middleware fixed artifact prerequisite is incomplete')
    original = lab.publication_records
    def records():
        production, support = original()
        return production+publication['images'], support
    lab.publication_records = records
    try: lab.cache_artifacts(keys={r['key'] for r in publication['images']})
    finally: lab.publication_records = original


def namespace_identity(lab, row, key):
    obj = lab.get('namespace', row[key])
    expected = row.get(key+'_uid')
    if obj and (obj['metadata']['uid'] != expected or
                obj['metadata'].get('labels', {}).get('kubeauto.io/longhorn-middleware-run') != row['run_id']):
        raise ValueError('middleware namespace ownership changed')
    return obj


WEBHOOK = 'percona-xtradbcluster-webhook'


def recover_orphan_webhook(lab):
    """Retire only the archived incomplete cleanup's exact cluster object."""
    obj = lab.get('validatingwebhookconfiguration', WEBHOOK)
    if not obj: return
    proof_path = lab.BASE/'tests/evidence/longhorn-middleware-global-webhook-first-failure-20261022.json'
    if proof_path.is_symlink() or not proof_path.is_file():
        raise ValueError('middleware refuses an unjournaled cluster webhook')
    proof = json.loads(proof_path.read_text())
    state = lab.owner(); binding = proof.get('orphan_webhook_recovery', {})
    if (DATA_ENGINE != 'v1' or binding.get('identity') != {k:state[k] for k in ('run_id','cluster_uid','namespace_uid')} or
            proof.get('schema') != 1 or proof.get('case') != 'LONGHORN-37' or
            proof.get('status') != 'failed' or proof.get('classification') != 'test-gate' or
            binding.get('durable') != {'remote_exit':1,'remote_finalized':1,'module_exit':1,'module_finalized':0}):
        raise ValueError('middleware orphan webhook recovery is not its archived completed failure')
    hooks = copy.deepcopy(obj.get('webhooks', []))
    for hook in hooks:
        config = hook.get('clientConfig', {})
        config['caBundleSHA256'] = hashlib.sha256(config.pop('caBundle', '').encode()).hexdigest()
    actual = {'uid':obj['metadata']['uid'], 'owners':obj['metadata'].get('ownerReferences', []),
        'webhooks_sha256':hashlib.sha256(json.dumps(hooks,sort_keys=True).encode()).hexdigest()}
    if actual != binding.get('webhook'):
        raise ValueError('middleware archived orphan webhook UID, owner or rules changed')
    namespaces = binding.get('absent_namespaces', [])
    if (len(namespaces) != 3 or len(set(namespaces)) != 3 or
            any(not isinstance(n,str) or not re.fullmatch(r'longhorn-db-(?:operator-)?[a-f0-9]{16}',n) for n in namespaces) or
            any(lab.get('namespace',name) for name in namespaces) or
            any(c['metadata']['name'] in PXC_CRDS for c in lab.get('crds')['items']) or
            any(lab.get(kind,'pxc-operator') for kind in ('clusterrole','clusterrolebinding'))):
        raise ValueError('middleware orphan webhook still has an operator or native consumer boundary')
    deployment_uid = actual['owners'][0]['uid'] if len(actual['owners']) == 1 else None
    if not deployment_uid or any(d['metadata']['uid'] == deployment_uid for d in lab.get('deployments',namespace='*')['items']):
        raise ValueError('middleware original webhook controller has not been retired')
    lab.remove(obj)
    def absent():
        current = lab.get('validatingwebhookconfiguration',WEBHOOK)
        if current and current['metadata']['uid'] != actual['uid']:
            raise ValueError('middleware orphan webhook replaced during native retirement')
        return current is None
    lab.wait('middleware-archived-orphan-webhook-retired',120,absent)
    print('LONGHORN_MIDDLEWARE_ORPHAN_WEBHOOK_SCOPED_CLEAN_PASS',flush=True)


def database_empty(lab, pid=0):
    """Reject stale or foreign database state before installing monitoring."""
    if (lab.DIRECTORY/RECORD).exists() or (lab.DIRECTORY/RECORD).is_symlink():
        raise ValueError('middleware has an unfinished ownership receipt')
    if lab.get('validatingwebhookconfiguration', WEBHOOK):
        raise ValueError('middleware requires no existing database webhook')
    if lab.get('storageclass', CLASS):
        raise ValueError('middleware fixture class remains')
    if any(c['metadata']['name'] in PXC_CRDS for c in lab.get('crds')['items']):
        raise ValueError('middleware requires the original database-free baseline')
    for kind in ('clusterrole', 'clusterrolebinding'):
        if lab.get(kind, 'pxc-operator'):
            raise ValueError('middleware operator cluster RBAC already exists')
    print('LONGHORN_MIDDLEWARE_DATABASE_EMPTY_PASS', flush=True)


def cluster_webhook(lab, row):
    """Bind the uncollectable cluster object to its exact current controller."""
    obj = lab.get('validatingwebhookconfiguration', WEBHOOK)
    saved = row.get('cluster_webhook')
    if not obj:
        if saved and not row.get('webhook_retire_intent'):
            raise ValueError('middleware original cluster webhook disappeared')
        return None
    if row.get('webhook_retired'):
        raise ValueError('middleware cluster webhook reappeared after retirement')
    controller = lab.get('deployment', 'pxc-operator', row['operator_namespace'])
    secret = lab.get('secret', 'pxc-webhook-ssl', row['operator_namespace'])
    annotations = controller['metadata'].get('annotations', {}) if controller else {}
    if (not controller or annotations.get('meta.helm.sh/release-name') != 'pxc-operator' or
            annotations.get('meta.helm.sh/release-namespace') != row['operator_namespace']):
        raise ValueError('middleware webhook lacks its chart-owned original operator')
    owners = obj['metadata'].get('ownerReferences', [])
    expected_owner = {'apiVersion':'apps/v1', 'kind':'Deployment', 'name':'pxc-operator',
        'uid':controller['metadata']['uid'], 'controller':True}
    if len(owners) != 1 or any(owners[0].get(k) != v for k,v in expected_owner.items()):
        raise ValueError('middleware webhook original controller identity differs')
    hooks = obj.get('webhooks', [])
    service = {'name':'percona-xtradb-cluster-operator', 'namespace':row['operator_namespace'],
        'path':'/validate-percona-xtradbcluster', 'port':443}
    if (not secret or secret.get('type') != 'Opaque' or
            set(secret.get('data', {})) != {'ca.crt','tls.crt','tls.key'} or len(hooks) != 1):
        raise ValueError('middleware webhook certificate or hook shape differs')
    hook = hooks[0]; client = hook.get('clientConfig', {})
    if (hook.get('name') != 'validationwebhook.pxc.percona.com' or
            hook.get('failurePolicy') != 'Fail' or hook.get('sideEffects') != 'None' or
            hook.get('admissionReviewVersions') != ['v1'] or
            set(client) != {'service','caBundle'} or client['service'] != service or
            not client.get('caBundle') or client['caBundle'] != secret['data']['ca.crt']):
        raise ValueError('middleware webhook endpoint or CA differs from its owned operator')
    rules = hook.get('rules', [])
    if (len(rules) != 1 or rules[0].get('apiGroups') != ['pxc.percona.com'] or
            rules[0].get('apiVersions') != ['*'] or rules[0].get('resources') != ['perconaxtradbclusters/*'] or
            set(rules[0].get('operations', [])) != {'CREATE','UPDATE'} or rules[0].get('scope', '*') != '*'):
        raise ValueError('middleware webhook admission rules changed')
    current = {'uid':obj['metadata']['uid'], 'operator_uid':controller['metadata']['uid'],
        'secret_uid':secret['metadata']['uid'],
        'spec_sha256':hashlib.sha256(json.dumps(hooks,sort_keys=True).encode()).hexdigest()}
    if saved and saved != current:
        raise ValueError('middleware original cluster webhook was replaced or changed')
    row['cluster_webhook'] = current; persist(lab, row)
    return obj


def retire_webhook(lab, row):
    obj = cluster_webhook(lab, row)
    if obj:
        row['webhook_retire_intent'] = True; persist(lab, row)
        lab.remove(obj)
        def absent():
            current = lab.get('validatingwebhookconfiguration', WEBHOOK)
            if current and current['metadata']['uid'] != row['cluster_webhook']['uid']:
                raise ValueError('middleware webhook replaced during native retirement')
            return current is None
        lab.wait('middleware-cluster-webhook-retired', 120, absent)
    row['webhook_retired'] = True; persist(lab, row)


def operator_lease(obj, objects, allowed, row):
    """controller-runtime's unowned leader lease must name an owned operator Pod."""
    meta = obj['metadata']
    if meta['name'] != '08db1feb.percona.com' or meta.get('namespace') != row['operator_namespace']:
        return False
    original = row.get('operator_lease_uid')
    if original and original != meta['uid']:
        raise ValueError('middleware original operator leader Lease was replaced')
    holder = obj.get('spec', {}).get('holderIdentity', '')
    name, separator, identity = holder.partition('_')
    if not separator or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', identity):
        return False
    return any(uid in allowed and pod['kind'] == 'Pod' and
        pod['metadata']['name'] == name and pod['metadata'].get('namespace') == row['operator_namespace'] and
        pod['metadata'].get('labels', {}).get('app.kubernetes.io/name') == 'pxc-operator'
        for uid, pod in objects.items())


def graph(lab, row, key):
    if not namespace_identity(lab, row, key): return []
    namespace = row[key]
    kinds = lab.kubectl('api-resources', '--verbs=list', '--namespaced=true', '-o', 'name').stdout.split()
    if not kinds: raise ValueError('middleware cannot discover namespace ownership graph')
    objects = {}
    for kind in sorted(set(kinds)-{'pods.metrics.k8s.io'}):
        for obj in lab.get(kind, namespace=namespace)['items']:
            objects[obj['metadata']['uid']] = obj
    allowed = {row[key+'_uid']} | ({row['pxc_uid']} if row.get('pxc_uid') else set())
    known = set(row.get('known_object_uids', []))
    for uid, obj in objects.items():
        meta = obj['metadata']; labels = meta.get('labels') or {}; annotations = meta.get('annotations') or {}
        root = (obj['kind'], meta['name'])
        if (root in (('ServiceAccount', 'default'), ('ConfigMap', 'kube-root-ca.crt')) or
                uid in known and obj['kind'] != 'Secret' or annotations.get('meta.helm.sh/release-name') == 'pxc-operator' and
                annotations.get('meta.helm.sh/release-namespace') == row['operator_namespace']):
            allowed.add(uid)
        elif obj['kind'] == 'Secret' and key == 'operator_namespace' and meta['name'].startswith('sh.helm.release.v1.pxc-operator.v'):
            if labels.get('owner') != 'helm' or labels.get('name') != 'pxc-operator':
                raise ValueError('middleware found a foreign operator Helm secret')
            allowed.add(uid)
        elif key == 'database_namespace' and obj['kind'] == 'Secret' and meta['name'] in (
                CLUSTER+'-secrets', 'internal-'+CLUSTER, CLUSTER+'-ssl', CLUSTER+'-ssl-internal'):
            # Operator 1.20.0 reconcileUsersSecret creates the system Secret
            # without an ownerReference, with the exact cluster labels.
            if meta['name'] == 'internal-'+CLUSTER:
                source = lab.get('secret', CLUSTER+'-secrets', row['database_namespace'])
                current = pxc(lab, row)
                original_source = row.get('internal_source_secret_uid')
                original_copy = row.get('system_secret_uids', {}).get(meta['name'])
                bound = (current['metadata']['uid'] == row.get('pxc_uid') and
                    current['spec'].get('secretsName') == CLUSTER+'-secrets') if current else (
                    bool(original_source) and original_copy == uid)
                if (not bound or not source or source.get('type') != 'Opaque' or obj.get('type') != 'Opaque' or
                        original_source and original_source != source['metadata']['uid'] or
                        not source.get('data') or obj.get('data') != source['data']):
                    raise ValueError('middleware internal Secret is not the original cluster system copy')
                row['internal_source_secret_uid'] = source['metadata']['uid']
            elif labels.get('app.kubernetes.io/instance') != CLUSTER:
                raise ValueError('middleware system/TLS Secret lacks original cluster labels')
            saved = row.setdefault('system_secret_uids', {}).get(meta['name'])
            if saved and saved != uid: raise ValueError('middleware original system/TLS Secret was replaced')
            row['system_secret_uids'][meta['name']] = uid
            allowed.add(uid)
        elif key == 'operator_namespace' and obj['kind'] == 'Secret' and meta['name'] == 'pxc-webhook-ssl':
            # Operator 1.20.0 setupCertificates creates this unlabelled root.
            deployment = next((o for o in objects.values() if o['kind'] == 'Deployment' and
                o['metadata']['name'] == 'pxc-operator'), None)
            saved = row.setdefault('operator_secret_uids', {}).get(meta['name'])
            controller = (deployment['metadata'].get('annotations', {}).get('meta.helm.sh/release-name') == 'pxc-operator' and
                deployment['metadata'].get('annotations', {}).get('meta.helm.sh/release-namespace') == row[key]) if deployment else saved == uid
            if (not controller or obj.get('type') != 'Opaque' or set(obj.get('data', {})) != {'ca.crt', 'tls.crt', 'tls.key'} or
                    meta.get('ownerReferences')):
                raise ValueError('middleware operator webhook Secret differs from its chart-owned controller')
            if saved and saved != uid: raise ValueError('middleware original operator webhook Secret was replaced')
            row['operator_secret_uids'][meta['name']] = uid
            allowed.add(uid)
        elif key == 'database_namespace' and obj['kind'] == 'PersistentVolumeClaim':
            if (obj['spec'].get('storageClassName') == CLASS and labels.get('app.kubernetes.io/instance') == CLUSTER):
                allowed.add(uid)
    while True:
        old = set(allowed)
        for uid, obj in objects.items():
            if any(r.get('uid') in allowed for r in obj['metadata'].get('ownerReferences', [])):
                allowed.add(uid)
            if obj['kind'] == 'Event' and obj.get('involvedObject', obj.get('regarding', {})).get('uid') in allowed | known:
                allowed.add(uid)
            if obj['kind'] == 'Endpoints' and monitor().controller_endpoints(obj, objects, allowed):
                allowed.add(uid)
            if key == 'operator_namespace' and obj['kind'] == 'Lease' and operator_lease(obj, objects, allowed, row):
                row['operator_lease_uid'] = uid
                allowed.add(uid)
        if old == allowed: break
    unknown = [o['kind']+'/'+o['metadata']['name'] for uid, o in objects.items() if uid not in allowed]
    if unknown: raise ValueError('foreign object in middleware namespace: '+','.join(sorted(unknown)))
    row['known_object_uids'] = sorted(known | set(objects)); persist(lab, row)
    return list(objects.values())


def pxc(lab, row):
    obj = lab.get('perconaxtradbclusters.pxc.percona.com', CLUSTER, row['database_namespace'])
    if not obj: return None
    if row.get('pxc_uid') and obj['metadata']['uid'] != row['pxc_uid']:
        raise ValueError('middleware database CR was replaced')
    if (obj['spec']['pxc']['volumeSpec']['persistentVolumeClaim']['storageClassName'] != CLASS or
            obj['spec']['pxc']['size'] != 3 or obj['spec']['haproxy']['size'] != 3):
        raise ValueError('middleware database storage class or replica declarations changed')
    return obj


def bindings(lab, row, *, healthy=True):
    claims = lab.get('pvc', namespace=row['database_namespace'])['items']
    if len(claims) != 3: raise ValueError('middleware requires exactly three database PVCs')
    disks = disk_rows(lab)
    result = {}
    for claim in claims:
        name = claim['metadata']['name']; pv = lab.get('pv', claim['spec'].get('volumeName', ''))
        if (claim['spec'].get('storageClassName') != CLASS or claim.get('status', {}).get('phase') != 'Bound' or
                not pv or pv['spec']['claimRef']['uid'] != claim['metadata']['uid'] or
                pv['spec'].get('storageClassName') != CLASS or pv['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
                pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io'):
            raise ValueError('middleware PVC/PV native CSI binding differs')
        volume_name = pv['spec']['csi']['volumeHandle']; volume = lab.get('volumes.longhorn.io', volume_name, NS)
        if (not volume or volume['spec'].get('dataEngine') != DATA_ENGINE or volume['spec'].get('numberOfReplicas') != 3):
            raise ValueError('middleware native Volume engine or replica policy differs')
        quantity = monitor().quantity
        if (quantity(claim['spec']['resources']['requests']['storage']) != 2*1024**3 or
                quantity(pv['spec']['capacity']['storage']) != 2*1024**3 or
                int(volume['spec'].get('size', 0)) != 2*1024**3):
            raise ValueError('middleware PVC/PV/Volume two-GiB capacity differs')
        bound = {'claim_uid': claim['metadata']['uid'], 'pv': pv['metadata']['name'], 'pv_uid': pv['metadata']['uid'],
            'volume': volume_name, 'volume_uid': volume['metadata']['uid']}
        previous = row.get('bindings', {}).get(name)
        if previous and any(previous[k] != v for k, v in bound.items()):
            raise ValueError('middleware original PVC/PV/Volume was replaced')
        if healthy:
            topology = lab.replica_topology(bound, 3)
            engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items'] if e['spec'].get('volumeName') == volume_name]
            replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items'] if r['spec'].get('volumeName') == volume_name]
            if (not topology or len(engines) != 1 or engines[0]['spec'].get('dataEngine') != DATA_ENGINE or
                    not any(r.get('kind') == 'Volume' and r.get('uid') == bound['volume_uid'] for r in engines[0]['metadata'].get('ownerReferences', [])) or
                    len(replicas) != 3 or any(r['spec'].get('dataEngine') != DATA_ENGINE or
                        r['spec'].get('nodeID') not in disks or
                        r['spec'].get('diskID') != disks[r['spec']['nodeID']]['disk_uuid'] or
                        not any(ref.get('kind') == 'Volume' and ref.get('uid') == bound['volume_uid']
                            for ref in r['metadata'].get('ownerReferences', [])) for r in replicas)):
                raise ValueError('middleware native Engine/Replica/storage disk identities differ')
            bound.update(engine_uid=engines[0]['metadata']['uid'], replicas=topology)
        result[name] = bound
    return result


def setup(lab, pid):
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('prior middleware integration requires scoped recovery')
    database_empty(lab, pid)
    admission(lab, pid); cache(lab)
    if lab.get('storageclass', CLASS): raise ValueError('middleware fixture class already exists')
    if any(c['metadata']['name'] in PXC_CRDS for c in lab.get('crds')['items']):
        raise ValueError('middleware refuses an existing database operator/CRD installation')
    for kind in ('clusterrole', 'clusterrolebinding'):
        if lab.get(kind, 'pxc-operator'): raise ValueError('middleware operator cluster RBAC already exists')
    token = secrets.token_hex(8); original = (lab.DIRECTORY/'config.yml').read_text()
    row = {k: lab.owner()[k] for k in ('run_id', 'cluster_uid', 'namespace_uid')}
    row.update(database_namespace='longhorn-db-'+token, operator_namespace='longhorn-db-operator-'+token,
        original_input=original, allowed_inputs=[original], original_crds={c['metadata']['name']: c['metadata']['uid'] for c in lab.get('crds')['items']})
    current_monitor = monitor().receipt(lab)
    row['monitoring_identity'] = {k: current_monitor[k] for k in ('monitor_namespace_uid', 'storage_class_uid', 'claims')}
    persist(lab, row)
    for key in ('database_namespace', 'operator_namespace'):
        if lab.get('namespace', row[key]): raise ValueError('middleware namespace collision')
        row[key+'_intent'] = True; persist(lab, row)
        obj = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace',
            'metadata': {'name': row[key], 'labels': {'kubeauto.io/longhorn-middleware-run': row['run_id']}}})).stdout)
        row[key+'_uid'] = obj['metadata']['uid']; persist(lab, row)
    config = desired(original, row['database_namespace'], row['operator_namespace'])
    text = yaml.safe_dump(config, sort_keys=False); apply(lab, row, text)
    monitor().product(lab, pid, 'longhorn-setup')
    sc = lab.get('storageclass', CLASS)
    if not sc or sc['parameters'].get('dataEngine') != DATA_ENGINE or sc['parameters'].get('numberOfReplicas') != '3' or sc['reclaimPolicy'] != 'Delete':
        raise ValueError('product did not declare the database three-replica StorageClass')
    row['storage_class_uid'] = sc['metadata']['uid']; row['install_intent'] = True; persist(lab, row)
    try: monitor().product(lab, pid, '07')
    finally:
        current = lab.get('perconaxtradbclusters.pxc.percona.com', CLUSTER, row['database_namespace']) if any(c['metadata']['name'] == PXC_CRDS[0] for c in lab.get('crds')['items']) else None
        if current and not row.get('pxc_uid'):
            row['pxc_uid'] = current['metadata']['uid']
        row['created_crds'] = {c['metadata']['name']: c['metadata']['uid'] for c in lab.get('crds')['items'] if c['metadata']['name'] in PXC_CRDS and c['metadata']['name'] not in row['original_crds']}
        row['cluster_rbac'] = {kind: obj['metadata']['uid'] for kind in ('clusterrole', 'clusterrolebinding') if (obj := lab.get(kind, 'pxc-operator'))}
        persist(lab, row)
    obj = pxc(lab, row)
    if not obj or obj.get('status', {}).get('state') != 'ready': raise ValueError('product database did not converge')
    if not cluster_webhook(lab, row): raise ValueError('product database webhook is missing')
    row['bindings'] = bindings(lab, row); persist(lab, row)
    for key in ('database_namespace', 'operator_namespace'): graph(lab, row, key)
    generated = yaml.safe_load((lab.DIRECTORY/'yml/percona-pxc/cluster.yaml').read_text())
    if generated['spec']['pxc']['volumeSpec']['persistentVolumeClaim']['storageClassName'] != CLASS:
        raise ValueError('product-rendered database manifest differs from explicit Longhorn input')
    row['rendered_sha256'] = hashlib.sha256((lab.DIRECTORY/'yml/percona-pxc/cluster.yaml').read_bytes()).hexdigest(); persist(lab, row)
    apply(lab, row, original)
    monitor().verify(lab, pid)
    monitor_identity(lab, row)
    print('LONGHORN_MIDDLEWARE_SETUP_PASS', flush=True)


def credential(lab, row):
    secret = lab.get('secret', CLUSTER+'-secrets', row['database_namespace'])
    if not secret or row.get('secret_uid', secret['metadata']['uid']) != secret['metadata']['uid']:
        raise ValueError('middleware database system credential identity changed')
    row['secret_uid'] = secret['metadata']['uid']; persist(lab, row)
    password = base64.b64decode(secret['data']['root'], validate=True).decode()
    if not password or '\n' in password or '\r' in password: raise ValueError('unsupported database password framing')
    return password


def client(lab, row, args, data='', pod=None):
    # Credentials are sent only over exec stdin and held in process memory.
    # Neither argv, product configuration, receipt nor the public log contains them.
    password = credential(lab, row)
    pod = pod or CLUSTER+'-pxc-0'
    # Galera certification does not imply every member has already applied
    # a write. A new HAProxy connection must wait for causal reads, including
    # the dump connection and the independent local-member observations.
    args = [args[0], '--init-command=SET SESSION wsrep_sync_wait=1', *args[1:]]
    command = ['sh', '-eu', '-c', 'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec "$@"', 'sh', *args]
    return lab.kubectl('-n', row['database_namespace'], 'exec', '-i', pod, '-c', 'pxc', '--', *command,
                       stdin=password+'\n'+data, timeout=180).stdout


def sql(lab, row, query, pod=None):
    return client(lab, row, ['mysql', '--user=root', '--protocol=TCP', '-h', CLUSTER+'-haproxy.'+row['database_namespace']+'.svc',
        '--ssl-mode=VERIFY_CA', '--ssl-ca=/etc/mysql/ssl-internal/ca.crt', '--batch', '--skip-column-names'], query, pod).strip()


def readback(lab, row, schema, expected, pod=None):
    result = sql(lab, row, 'SELECT id,HEX(payload) FROM '+schema+'.marker ORDER BY id;', pod)
    digest = hashlib.sha256(result.encode()).hexdigest()
    if result != expected: raise ValueError('middleware native SQL readback differs from the confirmed application rows')
    return {'rows': len(result.splitlines()), 'sha256': digest}


def backup_file(lab, row):
    proof = row.get('logical_backup')
    if not proof: return None
    path = lab.DIRECTORY/proof['name']
    if path.parent != lab.DIRECTORY or path.is_symlink(): raise ValueError('middleware backup path changed')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
            info.st_uid != os.geteuid() or (info.st_dev, info.st_ino, info.st_size) !=
            (proof['device'], proof['inode'], proof['bytes']) or hashlib.sha256(path.read_bytes()).hexdigest() != proof['sha256']):
        raise ValueError('middleware native logical backup inode, bytes or privacy changed')
    return path


def business(lab, pid):
    row = receipt(lab); monitor().verify(lab, pid)
    current_monitor = monitor_identity(lab, row)
    obj = pxc(lab, row)
    if not obj or obj.get('status', {}).get('state') != 'ready': raise ValueError('middleware database prerequisite is not ready')
    original = bindings(lab, row)
    emit('LONGHORN_MIDDLEWARE_PRODUCT_CONSUMERS', {'database_uid': row['pxc_uid'], 'database_storage_class': CLASS,
        'database_claims': original, 'monitoring_claims': current_monitor['claims'],
        'monitoring_storage_class': monitor().CLASS, 'rendered_sha256': row['rendered_sha256']})
    schema = 'kubeauto_'+row['run_id'].replace('-', '')[:16]
    if not re.fullmatch('[a-z0-9_]+', schema): raise ValueError('invalid owned database schema')
    restored = schema+'_restore'
    if sql(lab, row, "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME IN ('"+schema+"','"+restored+"');"):
        raise ValueError('middleware native application schema already exists')
    row.update(schema=schema, restored_schema=restored, sql_intent=True); persist(lab, row)
    values = [(i, hashlib.sha256((row['run_id']+':'+str(i)).encode()).hexdigest().upper()) for i in range(1, 1025)]
    expected = '\n'.join(str(i)+'\t'+value for i, value in values)
    query = 'CREATE DATABASE '+schema+'; CREATE TABLE '+schema+'.marker(id INT PRIMARY KEY,payload VARBINARY(32)) ENGINE=InnoDB; INSERT INTO '+schema+'.marker VALUES '+','.join('('+str(i)+",UNHEX('"+value+"'))" for i, value in values)+';'
    sql(lab, row, query); source = readback(lab, row, schema, expected)
    raw = client(lab, row, ['mysqldump', '--user=root', '--protocol=TCP', '-h', CLUSTER+'-haproxy.'+row['database_namespace']+'.svc',
        '--ssl-mode=VERIFY_CA', '--ssl-ca=/etc/mysql/ssl-internal/ca.crt', '--single-transaction', '--quick',
        '--set-gtid-purged=OFF', '--no-tablespaces', '--column-statistics=0', '--skip-add-locks', '--skip-comments', '--hex-blob', schema, 'marker'])
    data = raw.encode()
    if not 100 < len(data) <= 4*1024**2 or 'CREATE TABLE' not in raw or 'INSERT INTO' not in raw:
        raise ValueError('middleware native mysqldump result is incomplete or unbounded')
    name = 'longhorn-middleware-backup-'+secrets.token_hex(8)+'.sql'; path = lab.DIRECTORY/name
    row['backup_create_intent'] = name; persist(lab, row)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    try:
        with os.fdopen(fd, 'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno()); info = os.fstat(stream.fileno())
    except BaseException:
        current = path.lstat()
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino): path.unlink()
        raise
    finally: del data
    row['logical_backup'] = {'name': name, 'device': info.st_dev, 'inode': info.st_ino, 'bytes': info.st_size, 'sha256': hashlib.sha256(raw.encode()).hexdigest()}; persist(lab, row)
    sql(lab, row, 'INSERT INTO '+schema+".marker VALUES (1025,UNHEX('"+'EE'*32+"')); CREATE DATABASE "+restored+';')
    # A dump without --databases contains no CREATE DATABASE/USE statements;
    # load it into a separate schema, preserving the still-live source.
    native = backup_file(lab, row).read_text()
    client(lab, row, ['mysql', '--user=root', '--protocol=TCP', '-h', CLUSTER+'-haproxy.'+row['database_namespace']+'.svc',
        '--ssl-mode=VERIFY_CA', '--ssl-ca=/etc/mysql/ssl-internal/ca.crt', restored], native)
    restored_proof = readback(lab, row, restored, expected)
    latest = expected+'\n1025\t'+'EE'*32; readback(lab, row, schema, latest)
    sql(lab, row, 'INSERT INTO '+restored+".marker VALUES (2048,UNHEX('"+'DD'*32+"'));")
    readback(lab, row, schema, latest)
    restored_latest = expected+'\n2048\t'+'DD'*32
    readback(lab, row, restored, restored_latest)
    emit('LONGHORN_MIDDLEWARE_NATIVE_LOGICAL_RECOVERY', {'database_uid': row['pxc_uid'], 'source': source,
        'restored': restored_proof, 'backup': row['logical_backup'], 'source_after_backup_rows': 1025,
        'restored_independent_rows': 1025, 'isolation_verified': True,
        'boundary': 'Single-transaction InnoDB mysqldump and independent-schema restore; not physical backup, PITR, site DR or volume snapshot consistency.'})
    pod = lab.get('pod', CLUSTER+'-pxc-1', row['database_namespace'])
    sts = lab.get('statefulset', CLUSTER+'-pxc', row['database_namespace'])
    common = load('longhorn-v2-control-faults.py')
    if not common.pod_ready(pod) or not common.owner(pod, 'StatefulSet', sts['metadata']['uid']):
        raise ValueError('middleware restart refuses a foreign/unready database Pod')
    row['restart'] = {'name': pod['metadata']['name'], 'original_uid': pod['metadata']['uid'], 'statefulset_uid': sts['metadata']['uid'], 'intent': True}; persist(lab, row)
    lab.remove(pod)
    def replacement():
        current = lab.get('pod', pod['metadata']['name'], row['database_namespace'])
        return current if current and current['metadata']['uid'] != pod['metadata']['uid'] and common.pod_ready(current) and common.owner(current, 'StatefulSet', sts['metadata']['uid']) else None
    current = lab.wait('middleware-original-database-pod-recreated', 1800, replacement)
    lab.wait('middleware-pxc-three-ready', 600, lambda: pxc(lab, row).get('status', {}).get('state') == 'ready')
    observed = {}
    for i in range(3):
        name = CLUSTER+'-pxc-'+str(i)
        # Verify each database member locally, not a service that could route
        # all three observations to one healthy member.
        query = 'SELECT @@wsrep_on; SHOW GLOBAL STATUS WHERE Variable_name IN (\'wsrep_cluster_size\',\'wsrep_cluster_status\',\'wsrep_local_state_comment\');'
        state = client(lab, row, ['mysql', '--user=root', '--protocol=TCP', '-h', '127.0.0.1', '--batch', '--skip-column-names'], query, name)
        if not all(term in state.splitlines() for term in ('1', 'wsrep_cluster_size\t3', 'wsrep_cluster_status\tPrimary', 'wsrep_local_state_comment\tSynced')):
            raise ValueError('middleware database member did not regain original three-member Primary/Synced state')
        for db, text in ((schema, latest), (restored, restored_latest)):
            actual = client(lab, row, ['mysql', '--user=root', '--protocol=TCP', '-h', '127.0.0.1', '--batch', '--skip-column-names'], 'SELECT id,HEX(payload) FROM '+db+'.marker ORDER BY id;', name).strip()
            if actual != text: raise ValueError('middleware member persistence differs after Pod recreation')
        observed[name] = {'source_rows': 1025, 'source_sha256': hashlib.sha256(latest.encode()).hexdigest(),
            'restored_rows': 1025, 'restored_sha256': hashlib.sha256(restored_latest.encode()).hexdigest()}
    row['restart']['replacement_uid'] = current['metadata']['uid']; persist(lab, row)
    final = bindings(lab, row)
    if {n: {k: r[k] for k in ('claim_uid', 'pv_uid', 'volume_uid')} for n, r in final.items()} != {n: {k: r[k] for k in ('claim_uid', 'pv_uid', 'volume_uid')} for n, r in original.items()}:
        raise ValueError('middleware persistence changed its original database volume identities')
    emit('LONGHORN_MIDDLEWARE_MEMBER_PERSISTENCE', {'restart': row['restart'], 'members': observed, 'bindings': final})
    print('LONGHORN_MIDDLEWARE_BUSINESS_PASS', flush=True)


def cleanup(lab, pid):
    path = lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink():
        recover_orphan_webhook(lab)
        print('LONGHORN_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS', flush=True); return
    row = receipt(lab)
    if (lab.DIRECTORY/'config.yml').read_text() not in row['allowed_inputs']:
        raise ValueError('middleware cleanup refuses a changed product input')
    database = namespace_identity(lab, row, 'database_namespace')
    operator = namespace_identity(lab, row, 'operator_namespace')
    objects = graph(lab, row, 'database_namespace') if database else []
    if operator: graph(lab, row, 'operator_namespace')
    # The database operator must remain alive while its ordered-deletion
    # finalizer fences members. Never remove finalizers or delete the operator
    # first to make a stuck database disappear.
    current = pxc(lab, row) if database and PXC_CRDS[0] in row.get('created_crds', {}) else None
    if current:
        if not row.get('pxc_uid'):
            raise ValueError('middleware database create outcome lacks its original UID receipt')
        lab.remove(current)
        lab.wait('middleware-database-native-finalizer-finished', 1800,
            lambda: pxc(lab, row) is None)
    if database:
        lab.wait('middleware-database-pods-fenced', 600, lambda: not lab.get('pods', namespace=row['database_namespace'])['items'])
        # Capture partial setup bindings without pretending unbound claims
        # already own a PV. Only exact CSI Delete bindings authorize waiting
        # for removal of native Longhorn data.
        for claim in [o for o in objects if o['kind'] == 'PersistentVolumeClaim']:
            actual = lab.get('pvc', claim['metadata']['name'], row['database_namespace'])
            if not actual: continue
            if actual['metadata']['uid'] != claim['metadata']['uid'] or actual['spec'].get('storageClassName') != CLASS:
                raise ValueError('middleware PVC changed before consumer cleanup')
            name = actual['spec'].get('volumeName')
            if name:
                pv = lab.get('pv', name)
                if (not pv or pv['spec']['claimRef']['uid'] != actual['metadata']['uid'] or
                        pv['spec'].get('storageClassName') != CLASS or pv['spec'].get('persistentVolumeReclaimPolicy') != 'Delete' or
                        pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io'):
                    raise ValueError('middleware partial cleanup CSI binding differs')
                volume = lab.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], NS)
                original = row.get('bindings', {}).get(claim['metadata']['name'])
                if original and (original['claim_uid'] != actual['metadata']['uid'] or original['pv'] != name or
                        original['pv_uid'] != pv['metadata']['uid'] or original['volume'] != pv['spec']['csi']['volumeHandle'] or
                        volume and original['volume_uid'] != volume['metadata']['uid']):
                    raise ValueError('middleware original binding changed before native data cleanup')
                bound = {'pv_uid': pv['metadata']['uid'], 'volume': pv['spec']['csi']['volumeHandle'],
                    'volume_uid': volume['metadata']['uid'] if volume else original['volume_uid'] if original else None}
                prior = row.setdefault('cleanup_bindings', {}).get(name)
                if prior and prior != bound: raise ValueError('middleware original CSI deletion boundary changed')
                row['cleanup_bindings'][name] = bound
                # Persist each CSI deletion boundary before removing its PVC;
                # interruption must not lose the last reference to native data.
                persist(lab, row)
            lab.remove(actual)
    for name, bound in row.get('cleanup_bindings', {}).items():
        def gone():
            pv = lab.get('pv', name); volume = lab.get('volumes.longhorn.io', bound['volume'], NS)
            if pv and pv['metadata']['uid'] != bound['pv_uid'] or volume and volume['metadata']['uid'] != bound['volume_uid']:
                raise ValueError('middleware original data was replaced during CSI Delete cleanup')
            residual = any(any(o['spec'].get('volumeName') == bound['volume'] for o in lab.get(kind, namespace=NS)['items'])
                for kind in ('engines.longhorn.io', 'replicas.longhorn.io'))
            return not pv and not volume and not residual
        lab.wait('middleware-csi-delete-native-data-'+name, 900, gone)
    if operator:
        # Namespace graph is rechecked immediately before Helm's mutating
        # entry point; replacement or foreign objects stop dependent deletion.
        graph(lab, row, 'operator_namespace')
        retire_webhook(lab, row)
        command = [str(lab.BASE/'extra-bin/helm')]
        args = ['pxc-operator', '-n', row['operator_namespace'], '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig')]
        status = lab.execute(command+['status']+args+['-o', 'json'], check=False)
        if status.returncode == 0:
            for kind, uid in row.get('cluster_rbac', {}).items():
                obj = lab.get(kind, 'pxc-operator')
                if obj and obj['metadata']['uid'] != uid: raise ValueError('middleware operator cluster RBAC was replaced')
            lab.execute(command+['uninstall']+args+['--wait', '--timeout', '10m'], timeout=660)
        elif 'release: not found' not in status.stderr: raise ValueError('middleware cannot prove operator release absence')
    if lab.get('validatingwebhookconfiguration', WEBHOOK):
        raise ValueError('middleware cluster webhook remains after operator retirement')
    for key in ('database_namespace', 'operator_namespace'):
        obj = namespace_identity(lab, row, key)
        if obj:
            graph(lab, row, key); lab.remove(obj)
            lab.wait('middleware-owned-namespace-gone-'+key, 300, lambda key=key: namespace_identity(lab, row, key) is None)
    for name, uid in row.get('created_crds', {}).items():
        obj = lab.get('crd', name)
        if obj:
            if name not in PXC_CRDS or obj['metadata']['uid'] != uid or lab.get(name, namespace='*')['items']:
                raise ValueError('middleware CRD replaced or still has native consumers')
            lab.remove(obj)
    for kind, uid in row.get('cluster_rbac', {}).items():
        obj = lab.get(kind, 'pxc-operator')
        if obj: raise ValueError('middleware operator left cluster RBAC; retain receipt for diagnosis')
    sc = lab.get('storageclass', CLASS)
    if sc:
        if sc['metadata']['uid'] != row.get('storage_class_uid') or any(c['spec'].get('storageClassName') == CLASS for c in lab.get('pvc', namespace='*')['items']):
            raise ValueError('middleware fixture class was replaced or has foreign consumers')
        lab.remove(sc)
    try:
        native = backup_file(lab, row)
    except FileNotFoundError:
        # Native consumers and CRDs have already been retired. A previous
        # cleanup may have unlinked this private fixture before health failed.
        native = None
    if native: native.unlink()
    elif row.get('backup_create_intent') and (lab.DIRECTORY/row['backup_create_intent']).exists():
        raise ValueError('middleware backup create outcome is unknown; refuse blind unlink')
    apply(lab, row, row['original_input'])
    monitor().verify(lab, pid)
    monitor_identity(lab, row)
    lab.installation_health()
    path.unlink()
    print('LONGHORN_MIDDLEWARE_CONSUMERS_SCOPED_CLEAN_PASS', flush=True)


def baseline(lab, pid):
    database_empty(lab, pid)
    monitor().verify(lab, pid)
    current = monitor().receipt(lab)
    expected = set()
    for claim in current['claims'].values():
        pv = lab.get('pv', claim['pv']); expected.add(pv['spec']['csi']['volumeHandle'])
    if {v['metadata']['name'] for v in lab.get('volumes.longhorn.io', namespace=NS)['items']} != expected:
        raise ValueError('middleware empty database baseline has foreign Longhorn volumes')
    print('LONGHORN_MIDDLEWARE_BASELINE_VERIFIED', flush=True)


def qualify_reentry(lab, pid):
    """Recover only the archived same-input addon reentry, never a retry grant."""
    evidence = json.loads((lab.BASE/'tests/evidence/longhorn-middleware-reentry-first-failure-20261022.json').read_text())
    proof = evidence['reentry_proof']
    row = receipt(lab); module = monitor(); monitoring = module.receipt(lab)
    if (any(proof.get(k) != row.get(k) for k in ('run_id', 'cluster_uid', 'namespace_uid',
            'database_namespace', 'database_namespace_uid', 'operator_namespace', 'operator_namespace_uid')) or
            proof['monitor_namespace'] != monitoring['namespace'] or
            proof['monitor_namespace_uid'] != monitoring['monitor_namespace_uid'] or
            row['original_input'] != monitoring['ready_inputs'] or
            (lab.DIRECTORY/'config.yml').read_text() not in row['allowed_inputs']):
        raise ValueError('middleware reentry recovery does not match the archived owning run')
    for key in ('database_namespace', 'operator_namespace'):
        if not namespace_identity(lab, row, key):
            raise ValueError('middleware reentry original namespace is absent')
    namespace = module.namespace_identity(lab, monitoring)
    records = proof['release_history']
    if (len(records) != 2 or [r['revision'] for r in records] != [1, 2] or
            [r['status'] for r in records] != ['superseded', 'deployed'] or
            len({r['config_sha256'] for r in records}) != 1):
        raise ValueError('middleware recovery requires the archived same-input two-revision history')
    actual = []
    for obj in lab.get('secrets', namespace=monitoring['namespace'])['items']:
        meta = obj['metadata']
        if not meta['name'].startswith('sh.helm.release.v1.prometheus.v'): continue
        raw = base64.b64decode(base64.b64decode(obj['data']['release']))
        release = json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b\x08') else raw)
        actual.append(dict(name=meta['name'], uid=meta['uid'],
            secret_release_sha256=hashlib.sha256(obj['data']['release'].encode()).hexdigest(),
            revision=release['version'], status=release['info']['status'],
            chart=release['chart']['metadata']['name'], chart_version=release['chart']['metadata']['version'],
            config_sha256=hashlib.sha256(json.dumps(release['config'], sort_keys=True).encode()).hexdigest(),
            hook_names=[h['name'] for h in release.get('hooks', [])]))
    if sorted(actual, key=lambda r: r['revision']) != records:
        raise ValueError('middleware reentry actual release identities differ from archived proof')
    monitoring['qualified_release_history'] = sorted([{k: r[k] for k in
        ('name', 'uid', 'secret_release_sha256')} for r in records], key=lambda r: r['name'])
    # Validate all live roots and both actual hook execution windows before
    # the namespace graph persists this explicit, immutable recovery journal.
    module.namespace_graph(lab, monitoring)
    print('LONGHORN_MIDDLEWARE_REENTRY_QUALIFICATION_PASS', flush=True)


def verify_log(text):
    lines = text.splitlines()
    if re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', text, re.M):
        raise ValueError('middleware log contains its first failure')
    def one(marker):
        rows = [json.loads(l[len(marker)+1:]) for l in lines if l.startswith(marker+' ')]
        if len(rows) != 1: raise ValueError('middleware lacks one complete observation: '+marker)
        return rows[0]
    consumers = one('LONGHORN_MIDDLEWARE_PRODUCT_CONSUMERS')
    if (not consumers.get('database_uid') or consumers.get('database_storage_class') != CLASS or
            len(consumers.get('database_claims', {})) != 3 or len(consumers.get('monitoring_claims', {})) != 6 or
            consumers.get('monitoring_storage_class') != monitor().CLASS or not re.fullmatch('[a-f0-9]{64}', consumers.get('rendered_sha256', ''))):
        raise ValueError('middleware product consumers or explicit classes are incomplete')
    expected = {'longhorn-node-'+a.rsplit('.', 1)[1] for a in STORAGE_NODES}
    def native(rows):
        if len(rows) != 3: raise ValueError('middleware native bindings lack three volumes')
        for bound in rows.values():
            if any(not bound.get(k) for k in ('claim_uid', 'pv', 'pv_uid', 'volume', 'volume_uid', 'engine_uid')):
                raise ValueError('middleware native bindings lack original identities')
            replicas = bound.get('replicas', {})
            if len(replicas) != 3 or {r.get('node') for r in replicas.values()} != expected or any(not r.get('uid') for r in replicas.values()):
                raise ValueError('middleware native replicas omit original storage nodes')
    native(consumers['database_claims'])
    recovery = one('LONGHORN_MIDDLEWARE_NATIVE_LOGICAL_RECOVERY')
    backup = recovery['backup']
    if (recovery.get('database_uid') != consumers['database_uid'] or recovery.get('isolation_verified') is not True or
            recovery.get('source_after_backup_rows') != 1025 or recovery.get('restored_independent_rows') != 1025 or
            recovery.get('source') != recovery.get('restored') or recovery.get('source', {}).get('rows') != 1024 or
            not re.fullmatch('[a-f0-9]{64}', recovery['source'].get('sha256', '')) or
            not 100 < backup.get('bytes', 0) <= 4*1024**2 or not re.fullmatch('[a-f0-9]{64}', backup.get('sha256', ''))):
        raise ValueError('middleware lacks actual native SQL recovery-point and write-isolation evidence')
    persistence = one('LONGHORN_MIDDLEWARE_MEMBER_PERSISTENCE'); native(persistence['bindings'])
    if ({n: {k: b[k] for k in ('claim_uid', 'pv_uid', 'volume_uid')} for n,b in persistence['bindings'].items()} !=
            {n: {k: b[k] for k in ('claim_uid', 'pv_uid', 'volume_uid')} for n,b in consumers['database_claims'].items()}):
        raise ValueError('middleware original volume identities changed after restart')
    restart = persistence['restart']; members = persistence['members']
    if (not restart.get('statefulset_uid') or not restart.get('original_uid') or not restart.get('replacement_uid') or
            restart['original_uid'] == restart['replacement_uid'] or set(members) != {CLUSTER+'-pxc-'+str(i) for i in range(3)} or
            any(m.get('source_rows') != 1025 or m.get('restored_rows') != 1025 or
                not all(re.fullmatch('[a-f0-9]{64}', m.get(k, '')) for k in ('source_sha256','restored_sha256')) for m in members.values()) or
            len({m['source_sha256'] for m in members.values()}) != 1 or len({m['restored_sha256'] for m in members.values()}) != 1):
        raise ValueError('middleware persistence omits the recreated member and local three-member SQL readback')
    if lines.count('LONGHORN_MIDDLEWARE_BUSINESS_PASS') != 1:
        raise ValueError('middleware business success marker is incomplete')
    print('LONGHORN_MIDDLEWARE_LOG_VERIFIED', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('database-empty','baseline','admission','setup','business','cleanup','verify','qualify-reentry'))
    parser.add_argument('--relay-pid', type=int, default=0); parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.action == 'verify':
        if not args.log: parser.error('--log is required')
        verify_log(args.log.read_text()); return
    if args.relay_pid <= 0: parser.error('a supervised fixed Registry relay is required')
    lab = load('longhorn-lab.py'); sys.modules[lab.__name__] = lab; lab.require_lease()
    globals()[args.action.replace('-', '_')](lab, args.relay_pid)


if __name__ == '__main__':
    for number in (signal.SIGINT, signal.SIGTERM): signal.signal(number, lambda signum, frame: sys.exit(128+signum))
    main()
