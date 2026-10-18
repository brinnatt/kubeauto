#!/usr/bin/env python3
"""Percona MongoDB configuration, ownership and read-only convergence checks.

The Operator alone owns member configuration, elections, backup agents and data.
This helper never changes a member's role or repairs database files.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

LABELS = {'app.kubernetes.io/managed-by': 'kubeauto', 'kubeauto.io/component': 'mongodb'}
DNS = re.compile(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?')
SYSTEM_ROLES = ('BACKUP', 'DATABASE_ADMIN', 'CLUSTER_ADMIN', 'CLUSTER_MONITOR', 'USER_ADMIN')


def name(value, *, limit=63):
    if not isinstance(value, str) or len(value) > limit or not DNS.fullmatch(value):
        raise ValueError('invalid MongoDB resource name')


def flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ('yes', 'no', 'true', 'false'):
        return value.lower() in ('yes', 'true')
    raise ValueError('MongoDB boolean must be true/false or yes/no')


def integer(value, low, high):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or not low <= int(value) <= high:
        raise ValueError('MongoDB integer outside permitted range')
    return int(value)


def quantity(value, kind):
    match = re.fullmatch(r'([0-9]+(?:[.][0-9]+)?)([A-Za-z]*)', str(value))
    factors = {'': 1, 'm': 0.001} if kind == 'cpu' else {'': 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4}
    if not match or match[2] not in factors or float(match[1]) <= 0:
        raise ValueError('invalid positive MongoDB resource quantity')
    return float(match[1]) * factors[match[2]]


def validate(c):
    if not isinstance(c, dict):
        raise ValueError('MongoDB configuration must be a mapping')
    supported = {'operator_ver': ('1.23.0', '1.23.1'), 'operator_chart_ver': ('1.23.1', '1.23.2'),
                 'ver': ('8.0.26-11', '8.0.32-14'), 'backup_ver': ('2.15.0',),
                 'fluentbit_ver': ('5.0.9-1', '5.1.2-1'), 'exporter_ver': ('0.53.0',)}
    if any(c['mongodb_' + field] not in versions for field, versions in supported.items()):
        raise ValueError('MongoDB version is outside the pinned production/upgrade artifact set')
    if c['mongodb_operator_ver'] != {'1.23.1': '1.23.0', '1.23.2': '1.23.1'}[c['mongodb_operator_chart_ver']]:
        raise ValueError('MongoDB Operator and Chart versions must match the pinned upstream composition')
    for field in ('namespace', 'name', 'storage_class', 'users_secret',
                  'tls_secret', 'tls_internal_secret', 'encryption_key_secret', 'monitoring_namespace',
                  'monitoring_release', 'backup_storage_name'):
        name(c['mongodb_' + field])
    if len(c['mongodb_name']) > 40:
        raise ValueError('MongoDB cluster name too long')
    if len({c['mongodb_' + k] for k in ('users_secret', 'tls_secret', 'tls_internal_secret', 'encryption_key_secret')}) != 4:
        raise ValueError('MongoDB system, TLS and encryption Secrets must be distinct')
    for field in ('sharding_enabled', 'exporter_enabled', 'logcollector_enabled', 'backup_enabled',
                  'pitr_enabled', 'service_monitor_enabled', 'crd_upgrade', 'backup_force_path_style'):
        flag(c['mongodb_' + field])
    flag(c['mongodb_network_policy_enabled'])
    flag(c.get('mongodb_backup_schedule_enabled', True))
    flag(c.get('mongodb_volume_expansion_enabled', False))
    rotation = c.get('mongodb_logrotate', {})
    if not isinstance(rotation, dict):
        raise ValueError('MongoDB log rotation must be an explicit mapping')
    if rotation:
        if set(rotation) != {'interval_minutes', 'max_size_mb', 'retain'} or not flag(c['mongodb_logcollector_enabled']):
            raise ValueError('MongoDB log rotation requires log collection and interval_minutes, max_size_mb, retain')
        integer(rotation['interval_minutes'], 1, 30)
        integer(rotation['max_size_mb'], 1, 1024)
        integer(rotation['retain'], 1, 30)
    namespaces = c['mongodb_client_namespaces']
    if not isinstance(namespaces, list) or len(namespaces) > 32 or len(set(namespaces)) != len(namespaces):
        raise ValueError('MongoDB client namespaces must be an explicit unique list')
    for namespace in namespaces:
        name(namespace)
    selector = c['mongodb_client_selector']
    if not isinstance(selector, dict) or not selector:
        raise ValueError('MongoDB client selector must contain explicit labels')
    for key, value in selector.items():
        parts = key.split('/') if isinstance(key, str) else []
        if not 1 <= len(parts) <= 2 or any(not part for part in parts):
            raise ValueError('invalid MongoDB client label key')
        if len(parts) == 2 and (len(parts[0]) > 253 or any(not DNS.fullmatch(part) for part in parts[0].split('.'))):
            raise ValueError('invalid MongoDB client label prefix')
        label = r'[A-Za-z0-9](?:[-._A-Za-z0-9]*[A-Za-z0-9])?'
        if len(parts[-1]) > 63 or not re.fullmatch(label, parts[-1]) or not isinstance(value, str) or len(value) > 63 or (value and not re.fullmatch(label, value)):
            raise ValueError('invalid MongoDB client label name/value')
    sets = c['mongodb_replsets']
    sharded = flag(c['mongodb_sharding_enabled'])
    if not isinstance(sets, list) or not sets or (not sharded and len(sets) != 1):
        raise ValueError('replica-set mode requires one replica set; sharding requires explicit replica sets')
    names = []
    for rs in sets:
        if not isinstance(rs, dict) or set(rs) != {'name', 'size'}:
            raise ValueError('each MongoDB replica set requires only name and size')
        name(rs['name'], limit=16)
        if rs['name'] == 'cfg' or rs['name'] in names or len(c['mongodb_name'] + rs['name']) > 50:
            raise ValueError('reserved, duplicate or overlong MongoDB replica-set name')
        names.append(rs['name'])
        if integer(rs['size'], 3, 7) % 2 != 1:
            raise ValueError('MongoDB requires three, five or seven data-bearing voters')
    if integer(c['mongodb_config_size'], 3, 7) % 2 != 1:
        raise ValueError('MongoDB config members require an odd quorum')
    integer(c['mongodb_mongos_size'], 3, 32)
    integer(c['mongodb_ready_timeout'], 60, 14400)
    integer(c['mongodb_request_timeout'], 60, 86400)
    integer(c['mongodb_write_timeout_ms'], 1000, 120000)
    integer(c['mongodb_backup_retention_count'], 1, 365)
    integer(c['mongodb_pitr_oplog_span_min'], 1, 60)
    if not re.fullmatch(r'[1-9][0-9]*(Gi|Ti)', str(c['mongodb_pvc_size'])):
        raise ValueError('MongoDB PVC size must use a positive Gi/Ti quantity')
    for kind in ('cpu', 'memory'):
        for prefix in ('', 'mongos_'):
            request = quantity(c[f'mongodb_{prefix}{kind}_request'], kind)
            limit = quantity(c[f'mongodb_{prefix}{kind}_limit'], kind)
            if request > limit:
                raise ValueError('MongoDB resource request exceeds its limit')
    quantity(c['mongodb_exporter_cpu_request'], 'cpu')
    quantity(c['mongodb_exporter_memory_request'], 'memory')
    cache = c['mongodb_cache_size_gb']
    if isinstance(cache, bool) or not 0.25 <= float(cache) <= 1024:
        raise ValueError('MongoDB cache size must be between 0.25 and 1024 GiB')
    if float(cache) * 1024**3 > quantity(c['mongodb_memory_limit'], 'memory') / 2:
        raise ValueError('MongoDB WiredTiger cache must leave at least half of the memory limit for other allocations')
    if c['mongodb_backup_storage_type'] not in ('s3', 'minio') or c['mongodb_backup_schedule_type'] not in ('logical', 'physical'):
        raise ValueError('MongoDB backup supports s3/minio and logical/physical full backups')
    if flag(c['mongodb_pitr_enabled']) and not flag(c['mongodb_backup_enabled']):
        raise ValueError('MongoDB PITR requires backup and a full backup baseline')
    if flag(c['mongodb_service_monitor_enabled']) and not flag(c['mongodb_exporter_enabled']):
        raise ValueError('ServiceMonitor requires the MongoDB exporter')
    if flag(c['mongodb_backup_enabled']):
        name(c['mongodb_backup_credentials_secret'])
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', c['mongodb_backup_bucket']):
            raise ValueError('MongoDB backup requires an S3 bucket name without embedded prefix')
        endpoint = c['mongodb_backup_endpoint']
        parsed = urlsplit(endpoint if c['mongodb_backup_storage_type'] == 's3' else 'https://' + endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('MongoDB backup endpoint must use TLS, without credentials/path/query')
        if c['mongodb_backup_storage_type'] == 'minio' and '://' in endpoint:
            raise ValueError('MinIO endpoint must be hostname[:port], without scheme; secure=true is enforced')
        if c['mongodb_backup_ca_secret']:
            if c['mongodb_backup_storage_type'] != 'minio':
                raise ValueError('private CA bundle requires the upstream minio storage interface')
            name(c['mongodb_backup_ca_secret'])
        if len(c['mongodb_backup_schedule'].split()) != 5 or '\n' in c['mongodb_backup_schedule']:
            raise ValueError('MongoDB backup schedule requires five cron fields')
    users = c['mongodb_users']
    if not isinstance(users, list):
        raise ValueError('MongoDB users must be a list of explicit Secret references')
    seen = set()
    for user in users:
        if not isinstance(user, dict) or set(user) != {'name', 'db', 'passwordSecretRef', 'roles'}:
            raise ValueError('MongoDB user requires name, db, passwordSecretRef and roles')
        identity = (user['db'], user['name'])
        if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', v) for v in identity) or identity in seen:
            raise ValueError('invalid or duplicate MongoDB user identity')
        seen.add(identity)
        ref = user['passwordSecretRef']
        if not isinstance(ref, dict) or set(ref) != {'name', 'key'}:
            raise ValueError('MongoDB user password must reference an explicit Secret/key')
        name(ref['name'])
        if not isinstance(ref['key'], str) or not re.fullmatch(r'[-._A-Za-z0-9]+', ref['key']):
            raise ValueError('invalid MongoDB password Secret key')
        if not user['roles'] or any(not isinstance(r, dict) or set(r) != {'name', 'db'} for r in user['roles']):
            raise ValueError('MongoDB users require explicit database roles')
    requests = c['mongodb_requests']
    if not isinstance(requests, list) or len(requests) > 20:
        raise ValueError('MongoDB requests must be a list with at most twenty immutable operations')
    seen = set()
    for request in requests:
        operation_object(c, request)
        identity = (request['kind'], request['name'])
        if identity in seen:
            raise ValueError('duplicate MongoDB operation request')
        seen.add(identity)


def operation_object(c, request):
    if not isinstance(request, dict) or request.get('kind') not in ('backup', 'restore'):
        raise ValueError('MongoDB operation must be backup or restore')
    name(request.get('name'))
    if not request['name'].startswith(c['mongodb_name'] + '-'):
        raise ValueError('MongoDB operation name must start with the target cluster name and a hyphen')
    if not flag(c['mongodb_backup_enabled']):
        raise ValueError('MongoDB operations require configured backup storage')
    spec = {'clusterName': c['mongodb_name']}
    if request['kind'] == 'backup':
        if set(request) != {'kind', 'name', 'type'} or request['type'] not in ('logical', 'physical'):
            raise ValueError('MongoDB backup request requires kind, name and logical/physical type')
        kind = 'PerconaServerMongoDBBackup'
        # The CRD defaults the deadline to 120 and the controller's CheckFields
        # defaults compression to gzip. Declare both before hashing identity.
        spec.update(type=request['type'], storageName=c['mongodb_backup_storage_name'],
                    startingDeadlineSeconds=120, compressionType='gzip')
    else:
        allowed = {'kind', 'name', 'cluster_uid', 'backup_name', 'destination', 'type', 'target_utc'}
        if set(request) - allowed or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(request.get('cluster_uid', ''))):
            raise ValueError('MongoDB restore requires an exact target CR UID and known fields')
        if bool(request.get('backup_name')) == bool(request.get('destination')):
            raise ValueError('MongoDB restore requires exactly one backup_name or external destination')
        kind = 'PerconaServerMongoDBRestore'
        if request.get('backup_name'):
            name(request['backup_name'])
            spec['backupName'] = request['backup_name']
            if 'type' in request:
                raise ValueError('MongoDB named backup determines its own type')
        else:
            if request.get('type') not in ('logical', 'physical'):
                raise ValueError('external MongoDB restore requires logical/physical backup type')
            destination = urlsplit(request['destination'])
            if (destination.scheme != 's3' or destination.netloc != c['mongodb_backup_bucket'] or
                    not destination.path.strip('/') or destination.query or destination.fragment or
                    '..' in destination.path.split('/')):
                raise ValueError('MongoDB restore destination must identify a backup inside the configured S3 bucket')
            storage_type = c['mongodb_backup_storage_type']
            storage = {'bucket': c['mongodb_backup_bucket'], 'prefix': c['mongodb_backup_prefix'],
                       'region': c['mongodb_backup_region'], 'endpointUrl': c['mongodb_backup_endpoint'],
                       'credentialsSecret': c['mongodb_backup_credentials_secret'],
                       'insecureSkipTLSVerify': False, 'forcePathStyle': flag(c['mongodb_backup_force_path_style'])}
            if storage_type == 'minio':
                storage['secure'] = True
                if c['mongodb_backup_ca_secret']:
                    storage['caBundle'] = {'name': c['mongodb_backup_ca_secret'], 'key': c['mongodb_backup_ca_key']}
            spec['backupSource'] = {'type': request['type'], 'destination': request['destination'], storage_type: storage}
        if 'target_utc' in request:
            try:
                target = datetime.strptime(request['target_utc'], '%Y-%m-%d %H:%M:%S')
                if target.strftime('%Y-%m-%d %H:%M:%S') != request['target_utc']:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('MongoDB PITR target_utc requires an exact UTC YYYY-MM-DD HH:MM:SS timestamp') from None
            spec['pitr'] = {'type': 'date', 'date': request['target_utc']}
    obj = {'apiVersion': 'psmdb.percona.com/v1', 'kind': kind,
           'metadata': {'name': request['name'], 'namespace': c['mongodb_namespace'], 'labels': dict(LABELS)}, 'spec': spec}
    canonical = {'spec': spec, 'target_uid': request.get('cluster_uid')}
    obj['metadata']['annotations'] = {'kubeauto.io/request-sha256': hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()}
    # Retain backup data by default. Deletion/retention is a separate explicit action.
    return obj


def check_operation_identity(c, request, desired, existing, cluster):
    if not cluster or not owned(cluster):
        raise ValueError('MongoDB request refuses a foreign target cluster')
    if request['kind'] == 'restore' and request['cluster_uid'] != cluster['metadata']['uid']:
        raise ValueError('MongoDB restore target UID changed; refusing destructive operation')
    if existing and (not owned(existing) or existing['spec'] != desired['spec'] or
                     existing['metadata'].get('annotations', {}).get('kubeauto.io/request-sha256') !=
                     desired['metadata']['annotations']['kubeauto.io/request-sha256']):
        raise ValueError('MongoDB operation name is foreign or reused with different inputs')


def operation_complete(kind, status):
    """Operator 1.23.1 physical restore sets ready/pbmName, not completed.

    Ready is reached only after PBM and every replica set report done. Member
    availability is checked independently by health after the request returns.
    Backup still requires its completion time and external destination.
    """
    required = ('completed', 'destination') if kind == 'backup' else ('pbmName',)
    return (status.get('state') == 'ready' and not status.get('error') and
            all(isinstance(status.get(k), str) and status[k].strip() for k in required))


def operations(c, api):
    for request in c['mongodb_requests']:
        obj = operation_object(c, request)
        kind, ns = obj['kind'].lower(), c['mongodb_namespace']
        cluster = api.get('psmdb', c['mongodb_name'], ns)
        existing = api.get(kind, request['name'], ns, optional=True)
        check_operation_identity(c, request, obj, existing, cluster)
        if existing is None:
            with tempfile.TemporaryDirectory(prefix='kubeauto-mongodb-request-') as directory:
                path = Path(directory) / 'request.json'
                path.write_text(json.dumps(obj))
                api.run('create', '-f', str(path))
        deadline = time.monotonic() + int(c['mongodb_request_timeout'])
        api.deadline = deadline
        while True:
            current = api.get(kind, request['name'], ns)
            check_operation_identity(c, request, obj, current, api.get('psmdb', c['mongodb_name'], ns))
            status = current.get('status', {})
            state = status.get('state', '')
            if state in ('error', 'rejected'):
                raise ValueError('MongoDB operation failed: ' + request['name'] + ' state=' + state)
            if state == 'ready':
                if not operation_complete(request['kind'], status):
                    raise ValueError('MongoDB operation ready lacks completion/destination or PBM identity evidence')
                print('MONGODB_REQUEST_READY name=' + request['name'] + ' kind=' + request['kind'], flush=True)
                break
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB operation deadline exceeded: ' + request['name'])
            print('MONGODB_REQUEST_HEARTBEAT name=' + request['name'] + ' state=' + state, flush=True)
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        api.deadline = None
    print('MONGODB_REQUESTS_CONVERGED count=' + str(len(c['mongodb_requests'])), flush=True)


class API:
    def __init__(self, kubeconfig):
        self.command = ['kubectl', '--kubeconfig=' + str(kubeconfig)]
        self.deadline = None

    def run(self, *args, timeout=30):
        # Never echo command stderr: clients and admission errors can contain credentials.
        if self.deadline is not None:
            timeout = min(timeout, max(0.1, self.deadline - time.monotonic()))
        result = subprocess.run([*self.command, *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError('MongoDB Kubernetes/client request failed; inspect namespace events and redacted controller logs')
        return result.stdout

    def get(self, kind, object_name='', namespace='', *, optional=False):
        args = (['-n', namespace] if namespace else []) + ['get', kind]
        if object_name:
            args.append(object_name)
        if optional:
            args.append('--ignore-not-found')
        text = self.run(*args, '-o', 'json')
        return json.loads(text) if text.strip() else None


def owned(obj):
    labels = obj.get('metadata', {}).get('labels', {})
    return all(labels.get(k) == v for k, v in LABELS.items())


def secret_data(api, namespace, object_name, keys):
    data = api.get('secret', object_name, namespace).get('data', {})
    try:
        decoded = {k: base64.b64decode(data[k], validate=True) for k in keys}
    except (KeyError, ValueError) as exc:
        raise ValueError('MongoDB referenced Secret is missing a required key or valid base64') from None
    if any(not v for v in decoded.values()):
        raise ValueError('MongoDB referenced Secret contains an empty required value')
    return decoded


def operator_trust(c, api):
    """Keep public backup CAs for every owned consumer of the namespace Operator."""
    refs = set()
    def add(ref):
        if not ref:
            return
        secret, key = ref['name'], ref.get('key') or 'ca.crt'
        name(secret)
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._-]+', key):
            raise ValueError('MongoDB backup CA key is invalid')
        refs.add((secret, key))
    if flag(c['mongodb_backup_enabled']) and c['mongodb_backup_ca_secret']:
        add({'name': c['mongodb_backup_ca_secret'], 'key': c['mongodb_backup_ca_key']})
    clusters = {c['mongodb_name']}
    ns = c['mongodb_namespace']
    if api.get('crd', 'perconaservermongodbs.psmdb.percona.com', optional=True):
        for cr in api.get('psmdb', namespace=ns)['items']:
            if not owned(cr):
                continue
            clusters.add(cr['metadata']['name'])
            for storage in cr['spec'].get('backup', {}).get('storages', {}).values():
                add(storage.get('minio', {}).get('caBundle'))
    if api.get('crd', 'perconaservermongodbbackups.psmdb.percona.com', optional=True):
        for backup in api.get('psmdb-backup', namespace=ns)['items']:
            if backup['spec'].get('clusterName') in clusters:
                add(backup.get('status', {}).get('minio', {}).get('caBundle'))
    material = [(secret, key, hashlib.sha256(secret_data(api, ns, secret, (key,))[key]).hexdigest())
                for secret, key in sorted(refs)]
    return {'refs': [{'name': secret, 'key': key} for secret, key in sorted(refs)],
            'sha256': hashlib.sha256(json.dumps(material).encode()).hexdigest()}


def operator_manifest(c, objects, trust):
    """Helm post-rendering preserves the official Chart and its release ownership."""
    if not trust.get('refs'):
        return objects
    result = json.loads(json.dumps(objects))
    deployments = [o for o in result if o.get('kind') == 'Deployment']
    if (len(deployments) != 1 or deployments[0]['metadata'].get('name') != 'mongodb-operator' or
            deployments[0]['metadata'].get('namespace') != c['mongodb_namespace'] or
            not re.fullmatch(r'[0-9a-f]{64}', trust.get('sha256', ''))):
        raise ValueError('MongoDB Operator post-render identity differs from the pinned Chart')
    template = deployments[0]['spec']['template']
    spec = template['spec']
    containers = spec['containers']
    if (len(containers) != 1 or containers[0]['name'] != 'psmdb-operator' or
            containers[0]['image'] != 'registry.talkschool.cn:5000/brinnatt/percona-server-mongodb-operator:' + c['mongodb_operator_ver']):
        raise ValueError('MongoDB Operator post-render container differs from the pinned Chart')
    container = containers[0]
    env = {e['name']: e.get('value') for e in container.get('env', [])}
    volume, directory = 'kubeauto-backup-ca', '/etc/kubeauto/mongodb-backup-ca'
    if (env.get('WATCH_NAMESPACE') != c['mongodb_namespace'] or 'SSL_CERT_DIR' in env or
            any(v['name'] == volume for v in spec.get('volumes', [])) or
            any(v['name'] == volume for v in container.get('volumeMounts', []))):
        raise ValueError('MongoDB Operator post-render refuses conflicting trust or namespace settings')
    sources = []
    for ref in trust['refs']:
        name(ref['name'])
        if not re.fullmatch(r'[A-Za-z0-9._-]+', ref['key']):
            raise ValueError('MongoDB Operator CA projection key is invalid')
        filename = hashlib.sha256((ref['name'] + '/' + ref['key']).encode()).hexdigest() + '.crt'
        sources.append({'secret': {'name': ref['name'], 'items': [{'key': ref['key'], 'path': filename}]}})
    spec.setdefault('volumes', []).append({'name': volume, 'projected': {'defaultMode': 292, 'sources': sources}})
    container.setdefault('volumeMounts', []).append({'name': volume, 'mountPath': directory, 'readOnly': True})
    container.setdefault('env', []).append({'name': 'SSL_CERT_DIR',
        'value': '/etc/pki/tls/certs:/etc/ssl/certs:' + directory})
    template.setdefault('metadata', {}).setdefault('annotations', {})['kubeauto.io/backup-ca-sha256'] = trust['sha256']
    return result


def preflight(c, api):
    validate(c)
    ns, cluster = c['mongodb_namespace'], c['mongodb_name']
    if not owned(api.get('namespace', ns)):
        raise ValueError('database namespace must be explicitly labelled for kubeauto MongoDB ownership')
    users = secret_data(api, ns, c['mongodb_users_secret'],
                        [f'MONGODB_{role}_{field}' for role in SYSTEM_ROLES for field in ('USER', 'PASSWORD')])
    if len({users[f'MONGODB_{role}_USER'] for role in SYSTEM_ROLES}) != len(SYSTEM_ROLES):
        raise ValueError('MongoDB system users must be distinct')
    material = {'users': {k: base64.b64encode(v).decode() for k, v in users.items()}}
    for field in ('tls_secret', 'tls_internal_secret'):
        values = secret_data(api, ns, c['mongodb_' + field], ('ca.crt', 'tls.crt', 'tls.key'))
        if b'BEGIN CERTIFICATE' not in values['ca.crt'] or b'BEGIN CERTIFICATE' not in values['tls.crt']:
            raise ValueError('MongoDB TLS Secret must contain PEM CA and certificate')
        material[field] = {k: hashlib.sha256(v).hexdigest() for k, v in values.items()}
    key = secret_data(api, ns, c['mongodb_encryption_key_secret'], ('encryption-key',))['encryption-key']
    try:
        if len(base64.b64decode(key.strip(), validate=True)) != 32:
            raise ValueError()
    except ValueError:
        raise ValueError('MongoDB encryption-key must contain a base64-encoded 32-byte key') from None
    key_hash = hashlib.sha256(key).hexdigest()
    for user in c['mongodb_users']:
        ref = user['passwordSecretRef']
        secret_data(api, ns, ref['name'], (ref['key'],))
    storage = api.get('storageclass', c['mongodb_storage_class'])
    if storage.get('volumeBindingMode') not in ('Immediate', 'WaitForFirstConsumer'):
        raise ValueError('MongoDB requires an explicit dynamic StorageClass binding mode')
    expansion = flag(c.get('mongodb_volume_expansion_enabled', False))
    if expansion and storage.get('allowVolumeExpansion') is not True:
        raise ValueError('MongoDB volume expansion requires StorageClass allowVolumeExpansion=true')
    nodes = api.get('nodes')['items']
    needed = max(int(rs['size']) for rs in c['mongodb_replsets'])
    if flag(c['mongodb_sharding_enabled']):
        needed = max(needed, int(c['mongodb_config_size']), int(c['mongodb_mongos_size']))
    schedulable = [n for n in nodes if not n['spec'].get('unschedulable') and
                   not any(t.get('effect') in ('NoSchedule', 'NoExecute') for t in n['spec'].get('taints', [])) and
                   any(v['type'] == 'Ready' and v['status'] == 'True' for v in n['status'].get('conditions', []))]
    if len(schedulable) < needed:
        raise ValueError('MongoDB requires enough Ready, schedulable hostname failure domains')
    if flag(c['mongodb_backup_enabled']):
        secret_data(api, ns, c['mongodb_backup_credentials_secret'], ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'))
        if c['mongodb_backup_ca_secret']:
            secret_data(api, ns, c['mongodb_backup_ca_secret'], (c['mongodb_backup_ca_key'],))
    if flag(c['mongodb_service_monitor_enabled']):
        api.get('crd', 'servicemonitors.monitoring.coreos.com')
        api.get('namespace', c['mongodb_monitoring_namespace'])
        monitor = api.get('servicemonitor', ns + '-' + cluster, c['mongodb_monitoring_namespace'], optional=True)
        if monitor and not owned(monitor):
            raise ValueError('refusing to adopt a foreign MongoDB ServiceMonitor')
    for kind, object_name in (('service', cluster + '-metrics'), ('networkpolicy', cluster + '-access')):
        obj = api.get(kind, object_name, ns, optional=True)
        if obj and not owned(obj):
            raise ValueError('refusing to adopt a foreign MongoDB ' + kind)
    # A CRD may be absent on the first installation; never turn an API error into absence.
    crd = api.get('crd', 'perconaservermongodbs.psmdb.percona.com', optional=True)
    current = api.get('psmdb', cluster, ns, optional=True) if crd else None
    if current:
        if not owned(current):
            raise ValueError('refusing to adopt a foreign MongoDB cluster')
        spec = current['spec']
        # The old pins are upgrade inputs, not an automated downgrade policy.
        # Equal FCV does not establish binary or controller compatibility.
        for layer, previous, desired, versions in (
                ('server', spec.get('image', '').rsplit(':', 1)[-1], c['mongodb_ver'],
                 ('8.0.26-11', '8.0.32-14')),
                ('operator', spec.get('crVersion'), c['mongodb_operator_ver'], ('1.23.0', '1.23.1'))):
            if previous not in versions:
                raise ValueError('MongoDB existing ' + layer + ' version is outside the pinned transition set')
            if versions.index(desired) < versions.index(previous):
                raise ValueError('MongoDB ' + layer + ' downgrade is refused; restore a compatible backup into a separate cluster')
        annotation = current['metadata'].get('annotations', {}).get('kubeauto.io/encryption-key-sha256')
        if annotation != key_hash or spec['secrets']['encryptionKey'] != c['mongodb_encryption_key_secret']:
            raise ValueError('changing a MongoDB encryption key requires independent data migration, not a rollout')
        if bool(spec.get('sharding', {}).get('enabled')) != flag(c['mongodb_sharding_enabled']):
            raise ValueError('topology conversion requires a separate cluster and verified migration')
        if any(rs['volumeSpec']['persistentVolumeClaim']['storageClassName'] != c['mongodb_storage_class'] for rs in spec['replsets']):
            raise ValueError('changing StorageClass requires a separate cluster and verified migration')
        previous_sets = list(spec['replsets'])
        if spec.get('sharding', {}).get('enabled'):
            previous_sets.append(spec['sharding']['configsvrReplSet'])
        requested = quantity(c['mongodb_pvc_size'], 'memory')
        for rs in previous_sets:
            previous = quantity(rs['volumeSpec']['persistentVolumeClaim']['resources']['requests']['storage'], 'memory')
            if requested < previous:
                raise ValueError('MongoDB PVC shrink is refused; migrate to a separate cluster')
            if requested > previous and not expansion:
                raise ValueError('MongoDB PVC growth requires explicit mongodb_volume_expansion_enabled')
    # The pinned Operator's ensurePVCs accepts an existing name, and its volume
    # reconciler may rewrite labels. Reject collisions before publishing a CR.
    for claim in api.get('pvc', namespace=ns)['items']:
        meta = claim['metadata']
        if not meta['name'].startswith('mongod-data-' + cluster + '-'):
            continue
        labels = meta.get('labels', {})
        if (not current or meta.get('namespace') != ns or meta.get('deletionTimestamp') or
                labels.get('app.kubernetes.io/instance') != cluster or
                labels.get('app.kubernetes.io/managed-by') != 'percona-server-mongodb-operator' or
                claim['spec'].get('storageClassName') != c['mongodb_storage_class']):
            raise ValueError('refusing to adopt an existing or foreign MongoDB PVC')
        volume = claim['spec'].get('volumeName')
        if volume:
            pv = api.get('pv', volume)
            ref = pv['spec'].get('claimRef', {})
            if (pv['spec'].get('storageClassName') != c['mongodb_storage_class'] or
                    (ref.get('namespace'), ref.get('name'), ref.get('uid')) != (ns, meta['name'], meta['uid'])):
                raise ValueError('MongoDB existing PVC has a foreign or changed PV binding')
    return {'secret_revision': hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
            'encryption_revision': key_hash, 'operator_trust': operator_trust(c, api)}


def ready(pod):
    return (not pod['metadata'].get('deletionTimestamp') and
            any(x['type'] == 'Ready' and x['status'] == 'True' for x in pod.get('status', {}).get('conditions', [])))


def crd_changes(c, api, desired):
    """CRDs are cluster scoped: changing them requires an explicit, isolated request."""
    current, changed = [], []
    for obj in desired:
        live = api.get('crd', obj['metadata']['name'], optional=True)
        if live is None:
            # Helm owns first creation of missing CRDs. Never adopt an existing one.
            continue
        current.append(live)
        if live['spec'].get('versions') != obj['spec'].get('versions'):
            changed.append(obj)
    if not changed:
        return current, []
    if not flag(c['mongodb_crd_upgrade']):
        raise ValueError('MongoDB CRD schema differs; explicit mongodb_crd_upgrade is required')
    if c['mongodb_operator_chart_ver'] != '1.23.2':
        raise ValueError('MongoDB CRD schema rollback is refused; retain the forward schema')
    for kind in ('perconaservermongodbs', 'perconaservermongodbbackups',
                 'perconaservermongodbrestores', 'perconaservermongodbclustersyncs'):
        crd = kind + '.psmdb.percona.com'
        if api.get('crd', crd, optional=True) is None:
            continue
        consumers = json.loads(api.run('get', kind + '.psmdb.percona.com', '--all-namespaces', '-o', 'json'))['items']
        for obj in consumers:
            if (kind.endswith('clustersyncs') or obj['metadata']['namespace'] != c['mongodb_namespace'] or
                    not owned(obj) or
                    (obj['metadata']['name'] if kind == 'perconaservermongodbs' else obj['spec'].get('clusterName')) != c['mongodb_name']):
                raise ValueError('MongoDB CRD upgrade refuses shared or foreign consumers')
    return current, changed


def kubectl_json_objects(output):
    """kubectl create prints one JSON document per visited manifest object."""
    decoder, objects = json.JSONDecoder(), []
    remaining = output.strip()
    while remaining:
        document, end = decoder.raw_decode(remaining)
        if not isinstance(document, dict):
            raise ValueError('MongoDB CRD rendering returned a non-object document')
        objects.extend(document['items'] if document.get('kind') == 'List' else [document])
        remaining = remaining[end:].lstrip()
    if not objects or any(not isinstance(obj, dict) for obj in objects):
        raise ValueError('MongoDB CRD rendering returned no valid objects')
    return objects


def reconcile_crds(c, api, chart, config_path):
    checksum = chart.with_name(chart.name + '.sha256').read_text().split()[0]
    if hashlib.sha256(chart.read_bytes()).hexdigest() != checksum:
        raise ValueError('MongoDB CRD source Chart checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='kubeauto-mongodb-crds-') as directory:
        source = Path(directory) / 'crds.yaml'
        with tarfile.open(chart) as archive:
            source.write_bytes(archive.extractfile('psmdb-operator/crds/crd.yaml').read())
        desired = kubectl_json_objects(api.run('create', '--dry-run=client', '--validate=false', '-f', str(source), '-o', 'json'))
        expected = {'perconaservermongodbs', 'perconaservermongodbbackups',
                    'perconaservermongodbrestores', 'perconaservermongodbclustersyncs'}
        if {d['metadata']['name'] for d in desired} != {n + '.psmdb.percona.com' for n in expected}:
            raise ValueError('MongoDB Chart contains an unexpected CRD scope')
        current, changed = crd_changes(c, api, desired)
        if changed:
            previous_chart = chart.with_name('psmdb-operator-1.23.1.tgz')
            previous_checksum = previous_chart.with_name(previous_chart.name + '.sha256').read_text().split()[0]
            if hashlib.sha256(previous_chart.read_bytes()).hexdigest() != previous_checksum:
                raise ValueError('MongoDB previous CRD source Chart checksum mismatch')
            previous_source = Path(directory) / 'previous-crds.yaml'
            with tarfile.open(previous_chart) as archive:
                previous_source.write_bytes(archive.extractfile('psmdb-operator/crds/crd.yaml').read())
            previous = {obj['metadata']['name']: obj for obj in kubectl_json_objects(api.run(
                'create', '--dry-run=client', '--validate=false', '-f', str(previous_source), '-o', 'json'))}
            existing = {obj['metadata']['name']: obj for obj in current}
            updates = []
            version_label = 'app.kubernetes.io/version'
            for obj in changed:
                name = obj['metadata']['name']
                live, old = existing[name], previous[name]
                meta = live['metadata']
                if (live['spec'].get('versions') != old['spec']['versions'] or
                        meta.get('labels', {}).get(version_label) != old['metadata']['labels'][version_label] or
                        not meta.get('uid') or not meta.get('resourceVersion')):
                    raise ValueError('MongoDB CRD upgrade requires the pinned previous schema, version label and identity')
                # Helm creates CRDs but does not upgrade them. Transfer only the
                # explicitly approved schema and version label, retaining all
                # other fields and rejecting a concurrent resource replacement.
                updates.append({'apiVersion': obj['apiVersion'], 'kind': obj['kind'],
                    'metadata': {'name': name, 'uid': meta['uid'], 'resourceVersion': meta['resourceVersion'],
                                 'labels': {version_label: obj['metadata']['labels'][version_label]}},
                    'spec': {'versions': obj['spec']['versions']}})
            source = Path(directory) / 'changed.json'
            source.write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': updates}))
            # A partial SSA intent is validated after merging on the server;
            # kubectl's full-object validator would reject omitted CRD fields.
            command = ('apply', '--server-side', '--force-conflicts', '--validate=false',
                       '--field-manager=kubeauto-mongodb-crds')
            api.run(*command, '--dry-run=server', '-f', str(source), timeout=120)
            backup = config_path.parent / ('crds-before-' + str(time.time_ns()) + '.json')
            backup.write_text(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': current}))
            backup.chmod(0o600)
            api.run(*command, '-f', str(source), timeout=120)
            for obj in changed:
                api.run('wait', '--for=condition=Established', 'crd/' + obj['metadata']['name'], '--timeout=120s', timeout=130)
    print('MONGODB_CRD_CONTRACT_PASS changed=' + str(len(changed)), flush=True)


def check_members(status, configuration, expected):
    members = status.get('members', [])
    configured = configuration.get('members', [])
    if status.get('ok') != 1 or len(members) != expected or len(configured) != expected:
        raise ValueError('MongoDB replica-set member count differs from declaration')
    if sum(m.get('state') == 1 for m in members) != 1 or any(m.get('state') not in (1, 2) or m.get('health') != 1 for m in members):
        raise ValueError('MongoDB requires one primary and healthy secondaries')
    if any(m.get('arbiterOnly', False) or m.get('votes', 1) != 1 or m.get('hidden', False) for m in configured):
        raise ValueError('MongoDB members must be visible data-bearing voters')
    if {m['name'] for m in members} != {m['host'] for m in configured}:
        raise ValueError('MongoDB replica-set status/configuration identities disagree')


def backup_agents_ready(c, pods):
    sets = {rs['name']: int(rs['size']) for rs in c['mongodb_replsets']}
    if flag(c['mongodb_sharding_enabled']):
        sets['cfg'] = int(c['mongodb_config_size'])
    for rs, count in sets.items():
        members = [p for p in pods if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == c['mongodb_name'] and
                   p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == rs]
        if len(members) != count or any(not any(s['name'] == 'backup-agent' and s.get('ready')
                for s in p.get('status', {}).get('containerStatuses', [])) for p in members):
            return False
    return True


def prepare_backup(c, api, manifest):
    """Install agents before a cron can acquire a lock that blocks SmartUpdate."""
    if not (flag(c['mongodb_backup_enabled']) and flag(c.get('mongodb_backup_schedule_enabled', True))):
        return
    current = api.get('psmdb', c['mongodb_name'], c['mongodb_namespace'], optional=True)
    if current and not owned(current):
        raise ValueError('MongoDB backup preparation refuses a foreign cluster')
    if current and flag(current['spec']['backup']['enabled']) and backup_agents_ready(
            c, api.get('pods', namespace=c['mongodb_namespace'])['items']):
        return
    staged = json.loads(api.run('create', '--dry-run=client', '-f', str(manifest), '-o', 'json'))
    if (staged.get('kind') != 'PerconaServerMongoDB' or not owned(staged) or
            staged['metadata']['name'] != c['mongodb_name'] or
            staged['metadata']['namespace'] != c['mongodb_namespace'] or not staged['spec']['backup']['enabled']):
        raise ValueError('MongoDB backup preparation manifest identity differs from configuration')
    for task in staged['spec']['backup'].get('tasks', []):
        task['enabled'] = False
    staged['spec']['backup']['pitr']['enabled'] = False
    with tempfile.TemporaryDirectory(prefix='kubeauto-mongodb-backup-') as directory:
        path = Path(directory) / 'cluster.json'
        path.write_text(json.dumps(staged));path.chmod(0o600)
        api.run('apply', '--server-side', '--field-manager=kubeauto-mongodb', '-f', str(path), timeout=120)
    wait_health(c, api)
    print('MONGODB_BACKUP_AGENTS_READY schedule=deferred-until-agents-ready', flush=True)


def health(c, api):
    ns, cluster = c['mongodb_namespace'], c['mongodb_name']
    cr = api.get('psmdb', cluster, ns)
    if not owned(cr) or cr.get('status', {}).get('state') != 'ready' or cr['status'].get('observedGeneration') != cr['metadata']['generation']:
        raise ValueError('MongoDB controller has not reconciled the current generation')
    pods = api.get('pods', namespace=ns)['items']
    if flag(c['mongodb_backup_enabled']) and not backup_agents_ready(c, pods):
        raise ValueError('MongoDB backup agents are not ready on every data member')
    claims = {p['metadata']['name']: p for p in api.get('pvc', namespace=ns)['items']}
    sets = list(c['mongodb_replsets'])
    if flag(c['mongodb_sharding_enabled']):
        sets.append({'name': 'cfg', 'size': c['mongodb_config_size']})
    for rs in sets:
        members = [p for p in pods if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == cluster and
                   p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == rs['name']]
        expected = int(rs['size'])
        if len(members) != expected or not all(ready(p) for p in members) or len({p['spec'].get('nodeName') for p in members}) != expected:
            raise ValueError('MongoDB members are not ready in distinct hostname failure domains')
        for pod in members:
            volumes = [v['persistentVolumeClaim']['claimName'] for v in pod['spec']['volumes'] if 'persistentVolumeClaim' in v]
            if not volumes or any(claims.get(v, {}).get('status', {}).get('phase') != 'Bound' for v in volumes):
                raise ValueError('MongoDB member has an unbound data volume')
            if flag(c.get('mongodb_volume_expansion_enabled', False)):
                for volume in volumes:
                    claim = claims[volume]
                    if (quantity(claim['status'].get('capacity', {}).get('storage', '0'), 'memory') <
                            quantity(c['mongodb_pvc_size'], 'memory') or
                            any(condition.get('status') == 'True' and condition.get('type') in
                                ('Resizing', 'FileSystemResizePending') for condition in claim['status'].get('conditions', []))):
                        raise ValueError('MongoDB data volume expansion has not completed')
        pod = members[0]['metadata']['name']
        host = f'{pod}.{cluster}-{rs["name"]}.{ns}.svc.cluster.local'
        script = '''const a=db.getSiblingDB('admin');
if(!a.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}))throw Error('authentication failed');
const s=a.runCommand({replSetGetStatus:1}), r=a.runCommand({replSetGetConfig:1}), e=a.serverStatus().encryptionAtRest;
print(JSON.stringify({status:s,config:r.config,encryption:e}));'''
        result = api.run('-n', ns, 'exec', pod, '-c', 'mongod', '--', 'mongosh', '--quiet', '--norc',
                         '--host', host, '--tls', '--tlsCAFile', '/etc/mongodb-ssl/ca.crt', '--eval', script, timeout=60)
        state = json.loads(result.strip().splitlines()[-1])
        check_members(state['status'], state['config'], expected)
        if state.get('encryption', {}).get('encryptionEnabled') is not True:
            raise ValueError('MongoDB data-at-rest encryption is not enabled')
    if flag(c['mongodb_sharding_enabled']):
        routers = [p for p in pods if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == cluster and
                   p['metadata'].get('labels', {}).get('app.kubernetes.io/component') == 'mongos']
        if len(routers) != int(c['mongodb_mongos_size']) or not all(ready(p) for p in routers):
            raise ValueError('MongoDB routers are not ready')
    print('MONGODB_DATA_PLANE_READY current-generation=true tls=verified encrypted=true', flush=True)


def wait_health(config, api):
    deadline = time.monotonic() + int(config['mongodb_ready_timeout'])
    api.deadline = deadline
    try:
        while True:
            try:
                health(config, api)
                return
            except (ValueError, subprocess.TimeoutExpired) as exc:
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB readiness deadline exceeded: ' + str(exc)) from None
                print('MONGODB_READY_HEARTBEAT waiting=' + str(exc), flush=True)
                time.sleep(min(10, max(0, deadline - time.monotonic())))
    finally:
        api.deadline = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('validate', 'preflight', 'crds', 'health', 'operations', 'prepare-backup', 'operator-render'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--kubeconfig', type=Path)
    parser.add_argument('--chart', type=Path)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        validate(config)
        if args.operation == 'validate':
            print('MONGODB_CONFIG_VALIDATION_PASS')
            return 0
        if args.operation == 'operator-render':
            import yaml  # Already part of the kubeauto control-node requirements.
            if args.manifest is None:
                parser.error('--manifest is required for Operator post-rendering')
            try:
                trust = yaml.safe_load(args.manifest.read_text())['kubeautoBackupTrust']
                objects = [o for o in yaml.safe_load_all(sys.stdin) if o]
            except yaml.YAMLError:
                raise ValueError('MongoDB Operator post-render input is not valid YAML') from None
            for obj in operator_manifest(config, objects, trust):
                print(json.dumps(obj) + '\n---')
            return 0
        if args.kubeconfig is None:
            parser.error('--kubeconfig is required for API checks')
        api = API(args.kubeconfig)
        if args.operation == 'preflight':
            print(json.dumps(preflight(config, api)))
            return 0
        if args.operation == 'crds':
            if args.chart is None:
                parser.error('--chart is required for CRD checks')
            reconcile_crds(config, api, args.chart, args.config)
            return 0
        if args.operation == 'operations':
            operations(config, api)
            return 0
        if args.operation == 'prepare-backup':
            if args.manifest is None:
                parser.error('--manifest is required for backup preparation')
            prepare_backup(config, api, args.manifest)
            return 0
        wait_health(config, api)
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        # Only our validation messages and field names are emitted, never Secret values.
        print('MONGODB_PRODUCT_FAIL ' + str(exc), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
