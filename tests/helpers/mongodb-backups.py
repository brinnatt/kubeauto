#!/usr/bin/env python3
"""Product backup requests against a UID-owned, TLS-verified S3 fixture.

The fixture uses the already published MinIO/mc release pins. Successful backup
artifacts remain on its independent PVC for the separate restore scenario.
Only the product configuration and kubecli may configure PBM or request backups.
"""
import argparse
import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import threading

import yaml

REFUSAL_TARGET_DATA = "const d=c.getDB('appdb');const col=d.getCollection(prefix+'_documents');assert.eq(1,col.countDocuments({}));assert.eq('must-disappear',col.findOne({_id:'after-backup'}).value);assert.eq(null,col.findOne({_id:'backup'}));print('MONGODB_BACKUP_REFUSAL_TARGET_DATA_PASS original-marker=unchanged');"

SHARD_UUID_VERIFY = """
globalThis.mongodbGateStage='shard-local-uuid';
for(const shard of ['rs0','rs1']){
 const host=targetName+'-'+shard+'-0.'+targetName+'-'+shard+'.mongodb-gate.svc.cluster.local:27017';
 const direct=new Mongo('mongodb://'+host+'/admin?directConnection=true&readPreference=secondaryPreferred&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000');
 // Custom mongos users are not local shard identities. The pinned Operator
 // provisions monitor on each replica set, with listCollections and directShardOperations.
 assert(direct.getDB('admin').auth({user:process.env.MONGODB_CLUSTER_MONITOR_USER,pwd:process.env.MONGODB_CLUSTER_MONITOR_PASSWORD}));
 const actual=assert.commandWorked(direct.getDB(dbname).runCommand({listCollections:1,filter:{name:'documents'}})).cursor.firstBatch;
 assert.eq(1,actual.length);assert.eq(metadata.uuid,actual[0].info.uuid);
}
"""


def cleanup_probe_result(output, outcome, returncode, signalled):
    lines = output.splitlines()
    expected = {'success': 0, 'failure': 1, 'interruption': 143}[outcome]
    failures = [line for line in lines if re.match(r'^(?:\[FAIL\]|MONGODB_[A-Z_]*(?:FAIL|FIRST_FAILURE)(?: |$))', line)]
    expected_failures = ['MONGODB_BACKUP_FAIL reason=intentional MongoDB cleanup qualification failure'] if outcome == 'failure' else []
    if (returncode != expected or failures != expected_failures or
            outcome == 'interruption' and not signalled or
            any(lines.count(marker) != 1 for marker in ('MONGODB_CLEANUP_PROBE_BUSINESS_PASS',
                'MONGODB_CLEANUP_PROBE_READY', 'MONGODB_CLEANUP_PROBE_SCOPED_CLEAN_PASS'))):
        raise ValueError('MongoDB cleanup child lacks its exact outcome and complete cleanup evidence')

LABELS = {'kubeauto.io/component': 'mongodb-backup-fixture', 'kubeauto.io/managed-by': 'kubeauto'}
KINDS = {'pod': 'pods', 'pvc': 'persistentvolumeclaims', 'secret': 'secrets', 'service': 'services'}
TARGET_APIS = {'psmdb': '/apis/psmdb.percona.com/v1', 'psmdb-restore': '/apis/psmdb.percona.com/v1',
               'psmdb-backup': '/apis/psmdb.percona.com/v1',
               'statefulset': '/apis/apps/v1', 'pdb': '/apis/policy/v1',
               'networkpolicy': '/apis/networking.k8s.io/v1',
               **{k: '/api/v1' for k in ('pod', 'pvc', 'secret', 'service', 'configmap')}}
TARGET_PLURALS = KINDS | {'psmdb': 'perconaservermongodbs', 'psmdb-restore': 'perconaservermongodbrestores',
                         'psmdb-backup': 'perconaservermongodbbackups',
                         'statefulset': 'statefulsets', 'pdb': 'poddisruptionbudgets',
                         'networkpolicy': 'networkpolicies', 'configmap': 'configmaps'}


def restore_timeline(objects, target):
    """Keep recovery identities and endpoint publication, never arbitrary spec/status."""
    rows = []
    for obj in objects['items']:
        meta, kind = obj['metadata'], obj['kind']
        if meta.get('namespace') != 'mongodb-gate':
            continue
        labels, name = meta.get('labels', {}), meta['name']
        if not (name == target or name.startswith(target + '-') or
                labels.get('kubernetes.io/service-name') == target + '-rs0'):
            continue
        row = {'kind': kind, 'name': name, 'uid': meta['uid'],
               'resource_version': meta['resourceVersion'], 'deleting': meta.get('deletionTimestamp')}
        spec, status = obj.get('spec', {}), obj.get('status', {})
        if kind == 'PerconaServerMongoDB':
            row.update(generation=meta.get('generation'), observed=status.get('observedGeneration'),
                       state=status.get('state'), ready=status.get('ready'),
                       sizes={rs['name']: rs['size'] for rs in spec.get('replsets', [])})
        elif kind == 'Pod':
            row.update(node=spec.get('nodeName'), ip=status.get('podIP'), phase=status.get('phase'),
                       containers=[{key: c.get(key) for key in ('name', 'ready', 'restartCount')}
                                   for c in status.get('containerStatuses', [])])
        elif kind == 'StatefulSet':
            row.update(replicas=spec.get('replicas'), ready=status.get('readyReplicas'),
                       restore_annotation=meta.get('annotations', {}).get('percona.com/restore-in-progress'))
        elif kind == 'Service':
            row.update(publish_not_ready=spec.get('publishNotReadyAddresses'), selector=spec.get('selector'))
        elif kind == 'EndpointSlice':
            row['endpoints'] = [{key: e.get(key) for key in ('addresses', 'hostname', 'conditions', 'targetRef')}
                                for e in obj.get('endpoints', [])]
        else:
            continue
        rows.append(row)
    return rows


def target_resource_owned(obj, target, run_id):
    """Recognize only this target's product objects and Operator children."""
    meta = obj['metadata']
    labels = meta.get('labels', {})
    if meta.get('namespace') != 'mongodb-gate':
        return False
    name = meta['name']
    if not (name == target or name.startswith(target + '-') or name == 'internal-' + target + '-users' or
            name.startswith('mongod-data-' + target + '-')):
        return False
    if labels.get('kubeauto.io/test-run') == run_id:
        return all(labels.get(k) == v for k, v in LABELS.items())
    if labels.get('app.kubernetes.io/instance') == target:
        return labels.get('app.kubernetes.io/managed-by') == 'percona-server-mongodb-operator'
    request = (obj.get('kind') in ('PerconaServerMongoDBRestore', 'PerconaServerMongoDBBackup') and
               name in [target + '-' + run_id] + [target + '-' + run_id + '-' + phase for phase in
                   ('credentials', 'storage', 'quorum', 'oplog')])
    return ((name in (target, target + '-access', target + '-metrics') or request) and
            labels.get('app.kubernetes.io/managed-by') == 'kubeauto' and labels.get('kubeauto.io/component') == 'mongodb')


def json_messages(output):
    """mc renders JSON messages; support both indented and compact records."""
    decoder, rows = json.JSONDecoder(), []
    remaining = output.strip()
    while remaining:
        try:
            row, end = decoder.raw_decode(remaining)
        except ValueError:
            raise ValueError('MongoDB object listing is not valid JSON') from None
        if not isinstance(row, dict) or row.get('status') != 'success':
            raise ValueError('MongoDB object listing contains a failed response')
        rows.append(row)
        remaining = remaining[end:].lstrip()
    return rows


def refusal_cause(phase, error):
    patterns = {'credentials': r'InvalidAccessKeyId|SignatureDoesNotMatch|AccessDenied|Access Denied|access key.*does not exist',
                'storage': r'connection refused|i/o timeout|no route to host|connect: network is unreachable',
                'oplog': r'there is no oplogs that can cover the date/time or no oplogs at all'}
    if phase not in patterns or not re.search(patterns[phase], str(error), re.IGNORECASE):
        raise ValueError('MongoDB backup refusal has a different causal layer: ' + phase)
    return hashlib.sha256(str(error).encode()).hexdigest()


def refusal_reason(phase, status):
    """Require a pinned native cause; a generic timeout is never S3/oplog evidence."""
    if status.get('state') not in ('error', 'rejected'):
        raise ValueError('MongoDB backup refusal lacks a terminal native rejection')
    return refusal_cause(phase, status.get('error', ''))


def backup_objects(run_id, namespace):
    if not re.fullmatch('[0-9a-f]{16}', run_id) or namespace != 'mongodb-gate':
        raise ValueError('MongoDB backup fixture identity is outside its lab boundary')
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    name = 'mongodb-backup-' + run_id
    host = name + '.' + namespace + '.svc.cluster.local'
    labels = LABELS | {'kubeauto.io/test-run': run_id}
    def obj(kind, suffix=''):
        return {'apiVersion': 'v1', 'kind': kind, 'metadata': {
            'name': name + suffix, 'namespace': namespace, 'labels': dict(labels)}}
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name + '-ca')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
          .not_valid_after(now+timedelta(days=7)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=7))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False).sign(ca_key, hashes.SHA256()))
    credentials = obj('Secret', '-credentials')
    credentials.update(type='Opaque', stringData={'AWS_ACCESS_KEY_ID': secrets.token_hex(16),
                                                'AWS_SECRET_ACCESS_KEY': secrets.token_hex(32)})
    tls = obj('Secret', '-tls')
    tls.update(type='Opaque', stringData={'ca.crt': ca.public_bytes(serialization.Encoding.PEM).decode(),
        'public.crt': cert.public_bytes(serialization.Encoding.PEM).decode(),
        'private.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()).decode()})
    claim = obj('PersistentVolumeClaim', '-data')
    claim['spec'] = {'storageClassName': 'mongodb-longhorn', 'accessModes': ['ReadWriteOnce'],
                     'resources': {'requests': {'storage': '8Gi'}}}
    service = obj('Service')
    service['spec'] = {'selector': labels | {'kubeauto.io/fixture-role': 'server'},
                       'ports': [{'name': 'https', 'port': 9000, 'targetPort': 9000}]}
    def secret_env(name, key):
        return {'name': name, 'valueFrom': {'secretKeyRef': {'name': credentials['metadata']['name'], 'key': key}}}
    server = obj('Pod')
    server['metadata']['labels']['kubeauto.io/fixture-role'] = 'server'
    server['spec'] = {'restartPolicy': 'Always', 'automountServiceAccountToken': False,
        'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000},
        'containers': [{'name': 'minio',
            'image': 'registry.talkschool.cn:5000/mongodb-lab/brinnatt/minio:RELEASE.2025-04-08T15-41-24Z',
            'command': ['minio', 'server', '--address', ':9000', '--console-address', ':9001', '--certs-dir', '/certs', '/data'],
            'env': [secret_env('MINIO_ROOT_USER', 'AWS_ACCESS_KEY_ID'), secret_env('MINIO_ROOT_PASSWORD', 'AWS_SECRET_ACCESS_KEY')],
            'readinessProbe': {'tcpSocket': {'port': 9000}, 'periodSeconds': 3},
            'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '1', 'memory': '1Gi'}},
            'volumeMounts': [{'name': 'data', 'mountPath': '/data'}, {'name': 'tls', 'mountPath': '/certs', 'readOnly': True}]}],
        'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': claim['metadata']['name']}},
                    {'name': 'tls', 'secret': {'secretName': tls['metadata']['name'], 'items': [
                        {'key': 'public.crt', 'path': 'public.crt'}, {'key': 'private.key', 'path': 'private.key'}]}}]}
    client = obj('Pod', '-client')
    client['spec'] = {'restartPolicy': 'Always', 'automountServiceAccountToken': False,
        'containers': [{'name': 'mc',
            'image': 'registry.talkschool.cn:5000/mongodb-lab/brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z',
            'command': ['sh', '-c', 'trap "exit 0" TERM INT; sleep 86400 & wait'],
            'env': [secret_env('S3_ACCESS', 'AWS_ACCESS_KEY_ID'), secret_env('S3_SECRET', 'AWS_SECRET_ACCESS_KEY'),
                    {'name': 'MC_HOST_gate', 'value': 'https://$(S3_ACCESS):$(S3_SECRET)@' + host + ':9000'}],
            'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}},
            'volumeMounts': [{'name': 'ca', 'mountPath': '/tmp/mc/certs', 'readOnly': True}]}],
        'volumes': [{'name': 'ca', 'secret': {'secretName': tls['metadata']['name'],
                    'items': [{'key': 'ca.crt', 'path': 'CAs/gate.crt'}]}}]}
    return [credentials, tls, claim, service, server, client]


def backup_metadata(meta, kind, *, expected_version='8.0.32-14', expected_replsets=None):
    """PBM 2.15 BackupMeta; do not retain its credential-bearing store field."""
    if (meta.get('type') != kind or meta.get('status') != 'done' or meta.get('error') or
            not meta.get('name') or not meta.get('replsets') or
            any(rs.get('status') != 'done' or rs.get('error') for rs in meta['replsets'])):
        raise ValueError('MongoDB remote backup metadata is incomplete or failed')
    result = {key: meta.get(key) for key in ('type', 'name', 'mongodb_version', 'pbm_version', 'fcv',
                                            'start_ts', 'last_write_ts', 'size')}
    names = [rs.get('name') for rs in meta['replsets']]
    if expected_replsets is not None and (sorted(names) != sorted(expected_replsets) or
                                         len(names) != len(set(names))):
        raise ValueError('MongoDB backup replica-set topology differs from the declared fixture')
    if kind == 'physical':
        if meta.get('mongodb_version') != expected_version or names != (expected_replsets or ['rs0']):
            raise ValueError('MongoDB physical backup version or replica-set topology differs from the fixed fixture')
        result['replsets'] = names
    return result


def encryption_fingerprints(logs):
    """Fixed PSMDB encryption_extension.c/keydb error branches, per log file."""
    explicit = (
        'Decrypted data integrity check has failed. Probably the encryption key was wrong.',
        'EncryptionKeyDB: WiredTiger metadata corruption detected or an invalid encryption key is used',
    )
    return {'explicit_key_error': any(text in logs for text in explicit),
            'keydb_context': '/data/db/keydb' in logs or 'EncryptionKeyDB' in logs,
            'openssl_bad_decrypt': any('libcrypto:' in line and 'bad decrypt' in line for line in logs.splitlines()),
            'wiredtiger_panic': 'setting return code to WT_PANIC' in logs}


def encryption_failure(status, logs):
    """Require a failed named restore and its native key-database evidence."""
    flags = encryption_fingerprints(logs)
    return (status.get('state') == 'error' and bool(status.get('pbmName')) and
            (flags['explicit_key_error'] or
             all(flags[key] for key in ('keydb_context', 'openssl_bad_decrypt', 'wiredtiger_panic'))))


def scheduled_backup_record(obj, source, storage, started):
    """Identity emitted by pinned BackupFromTask and ScheduledBackupLabels."""
    meta, spec = obj['metadata'], obj['spec']
    labels = meta.get('labels', {})
    created = datetime.fromisoformat(meta['creationTimestamp'].replace('Z', '+00:00'))
    start = datetime.fromisoformat(started)
    if (obj.get('kind') != 'PerconaServerMongoDBBackup' or meta.get('namespace') != 'mongodb-gate' or
            not meta.get('uid') or not re.fullmatch('cron-' + re.escape(source[:16]) + r'-[0-9]{14}-[a-z0-9]{5}', meta['name']) or
            any(labels.get(k) != v for k, v in {'percona.com/cluster': source,
                'percona.com/backup-ancestor': 'daily-full', 'percona.com/backup-type': 'cron'}.items()) or
            'percona.com/delete-backup' not in meta.get('finalizers', []) or
            any(spec.get(k) != v for k, v in {'clusterName': source, 'storageName': storage,
                'type': 'logical', 'compressionType': 'gzip'}.items()) or
            created.tzinfo is None or start.tzinfo is None or created < start - timedelta(seconds=3)):
        raise ValueError('MongoDB retention refuses a foreign or changed scheduled backup')
    return {'name': meta['name'], 'uid': meta['uid'], 'created': meta['creationTimestamp'],
            'spec_sha256': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()}


def stop_retention(base, *, mode='retention', proc=Path('/proc'), state=Path('/tmp/kubeauto-mongodb-gate.pid')):
    """Stop a verified backup subtree; preserve its durable parent and journals."""
    if mode not in ('retention', 'backup-refusal', 'rollback', 'stability', 'stability-faults'):
        raise ValueError('MongoDB backup stop refuses an unsupported scenario')
    pid = int(state.read_text().strip())
    processes = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():continue
        try:
            args = (entry / 'cmdline').read_bytes().decode().strip('\0').split('\0')
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, ValueError):continue
        processes[int(entry.name)] = {'args': args, 'parent': int(stat[1]), 'start': stat[19]}
    expected = [str(base / 'tests/helpers/run-durable-gate.sh'), '/tmp/kubeauto-mongodb-gate',
                'MONGODB_GATE_EXIT', 'bash', str(base / 'tests/helpers/mongodb-install-gate.sh'), '--' + mode]
    if processes.get(pid, {}).get('args', [])[1:] != expected:
        raise ValueError('MongoDB retention stop refuses an unrelated durable process')
    descendants = {pid}
    while True:
        expanded = descendants | {p for p, row in processes.items() if row['parent'] in descendants}
        if expanded == descendants:break
        descendants = expanded
    expected = [str(base / 'tests/helpers/mongodb-backups.py'), mode, '--base', str(base)]
    roots = [p for p in descendants if processes[p]['args'][1:] == expected]
    if len(roots) != 1:
        raise ValueError('MongoDB retention stop requires its unique active scenario')
    pending = {roots[0]}
    while True:
        expanded = pending | {p for p, row in processes.items() if row['parent'] in pending}
        if expanded == pending:break
        pending = expanded
    for p in pending:
        stat = (proc / str(p) / 'stat').read_text().rsplit(')', 1)[1].split()
        if stat[19] != processes[p]['start']:
            raise ValueError('MongoDB retention stop process identity changed')
    # Stop the scenario first so its finally block cannot launch a second
    # product command while we stop the already recorded command descendants.
    for p in [roots[0], *sorted(pending - {roots[0]}, reverse=True)]:
        try:
            stat = (proc / str(p) / 'stat').read_text().rsplit(')', 1)[1].split()
            if stat[19] != processes[p]['start']:
                raise ValueError('MongoDB retention stop process identity changed')
            os.kill(p, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):pass
    print('MONGODB_' + mode.upper().replace('-', '_') + '_STOPPED recovery=required journal=preserved', flush=True)


class BackupGate:
    def __init__(self, base, kind, *, pitr=False):
        spec = importlib.util.spec_from_file_location('mongodb_backup_scenarios', base / 'tests/helpers/mongodb-scenarios.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.scenario = module.Scenario(base)
        self.scenario_module = module
        self.canonical_snapshot = module.canonical_snapshot
        self.lab, self.base, self.kind = self.scenario.lab, base, kind
        self.run_id = secrets.token_hex(8)
        self.path = self.lab.directory / ('mongodb-backup-' + ('pitr' if pitr else kind) + '.json')
        self.config_path = self.lab.directory / 'config.yml'
        self.state = None
        self.diagnostic_directory = None

    def save(self):
        self.scenario.fixture.write_private(self.path, self.state)

    def api_input(self, *args, obj):
        result = subprocess.run([*self.lab.command, *args], input=json.dumps(obj),
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError('MongoDB backup fixture API request failed; preserve private runner diagnostics')
        return result.stdout

    def owned(self, obj):
        labels = obj['metadata'].get('labels', {})
        return all(labels.get(k) == v for k, v in LABELS.items()) and labels.get('kubeauto.io/test-run') == self.state['run_id']

    def mc(self, *args):
        result = subprocess.run([*self.lab.command, '-n', self.scenario.ns, 'exec', self.state['name'] + '-client', '--',
                                 'mc', '--config-dir', '/tmp/mc', *args], capture_output=True, text=True, timeout=180)
        if result.returncode:
            self.diagnostic_path()
            hidden = []
            try:
                for suffix in ('-credentials', '-tls'):
                    obj = self.lab.get('secret', self.state['name'] + suffix, self.scenario.ns)
                    for value in obj['data'].values():
                        hidden.extend((value, base64.b64decode(value).decode('utf-8', errors='ignore')))
                output = self.scenario.fixture.redact(result.stdout + result.stderr, hidden)
            except (ValueError, KeyError, TypeError, subprocess.SubprocessError):
                output = 'Client output withheld because credential redaction inputs were unavailable.\n'
            path = self.diagnostic_directory / 'object-storage-client.txt'
            path.write_text(output);path.chmod(0o600)
            action = args[0] if args and args[0] in ('ready', 'mb', 'ls', 'cat') else 'unknown'
            raise ValueError('MongoDB object storage client failed action=' + action + ' rc=' + str(result.returncode) +
                             ' diagnostics=' + str(self.diagnostic_directory))
        return result.stdout.strip()

    def diagnostic_path(self):
        if self.diagnostic_directory is None:
            self.diagnostic_directory = Path(tempfile.mkdtemp(prefix='kubeauto-mongodb-install.backup-'))

    def diagnostics(self):
        """Collect first-failure state while its owned storage still exists."""
        self.diagnostic_path()
        self.lab.diagnostics(self.diagnostic_directory)
        print('MONGODB_BACKUP_DIAGNOSTICS_SAVED directory=' + str(self.diagnostic_directory), flush=True)

    def setup_product(self):
        result = subprocess.run(['kubecli', 'setup', self.scenario.ns, '07', '--tags', 'mongodb'],
                                cwd=self.base, timeout=5400)
        if result.returncode:
            raise ValueError('MongoDB backup product configuration/request failed')

    def prepare(self):
        if self.path.exists():
            raise ValueError('MongoDB backup fixture already exists; use its owning restore or cleanup path')
        owner = self.lab.verify()
        if owner.get('healthy') is not True or list(self.lab.directory.glob('*fault*.json')):
            raise ValueError('MongoDB backup requires the healthy owned product baseline')
        self.scenario.health()
        objects = backup_objects(self.run_id, self.scenario.ns)
        for obj in objects:
            kind = 'pvc' if obj['kind'] == 'PersistentVolumeClaim' else obj['kind'].lower()
            if self.lab.get(kind, obj['metadata']['name'], self.scenario.ns):
                raise ValueError('MongoDB backup fixture refuses an existing resource')
        name = objects[-2]['metadata']['name']
        self.state = {'run_id': self.run_id, 'cluster_uid': owner['cluster_uid'], 'name': name,
                      'bucket': name, 'resources': [], 'kind': self.kind, 'backup': None,
                      'source_prefix': self.scenario.prefix, 'client_attempted': False,
                      'client_uid': None, 'workload_started': False}
        self.save()
        for obj in objects:
            kind = 'pvc' if obj['kind'] == 'PersistentVolumeClaim' else obj['kind'].lower()
            record = {'kind': kind, 'name': obj['metadata']['name'], 'uid': None}
            self.state['resources'].append(record);self.save()
            result = json.loads(self.api_input('create', '-f', '-', '-o', 'json', obj=obj))
            record['uid'] = result['metadata']['uid'];self.save()
        for pod in (name, name + '-client'):
            self.lab.run('-n', self.scenario.ns, 'wait', '--for=condition=Ready', 'pod/' + pod, '--timeout=600s', timeout=620)
        self.mc('ready', 'gate')
        self.mc('mb', 'gate/' + self.state['bucket'])
        print('MONGODB_BACKUP_STORAGE_READY tls=ca-and-hostname-verified', flush=True)

    def configure(self, restore_request=None, *, pitr=False, schedule=False):
        config = yaml.safe_load(self.config_path.read_text())
        name = self.state['name']
        settings = {'mongodb_backup_enabled': True, 'mongodb_pitr_enabled': pitr,
                    'mongodb_backup_storage_type': 'minio', 'mongodb_backup_bucket': self.state['bucket'],
                    'mongodb_backup_prefix': 'backup', 'mongodb_backup_endpoint': name + '.' + self.scenario.ns + '.svc.cluster.local:9000',
                    'mongodb_backup_credentials_secret': name + '-credentials', 'mongodb_backup_ca_secret': name + '-tls',
                    'mongodb_backup_ca_key': 'ca.crt', 'mongodb_backup_schedule_enabled': False,
                    'mongodb_request_timeout': 1800, 'mongodb_requests': [
                        {'kind': 'backup', 'name': self.scenario.fixture.NAME + '-' + self.kind + '-' + self.state['run_id'], 'type': self.kind}]}
        if pitr:settings['mongodb_pitr_oplog_span_min'] = 1
        if schedule:
            settings.update(mongodb_backup_schedule_enabled=True, mongodb_backup_schedule='* * * * *',
                            mongodb_backup_schedule_type='logical', mongodb_backup_retention_count=2)
        if restore_request:
            settings['mongodb_requests'] = [restore_request]
        self.state['original_config'] = {key: config.get(key) for key in settings}
        self.state['original_absent'] = [key for key in settings if key not in config]
        self.state['applied_config'] = settings
        operation = 'restore' if restore_request else 'backup'
        request = settings['mongodb_requests'][0]
        request_key = 'restore_request' if restore_request else 'request'
        desired_key = 'desired_restore' if restore_request else 'desired_request'
        self.state[request_key] = request
        self.state['active_operation'] = operation
        source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        self.state['source_cluster_uid'] = source['metadata']['uid']
        # Use the product's canonical immutable request identity for recovery.
        product_config = json.loads((self.lab.directory / 'yml/mongodb/input.json').read_text())
        self.state[desired_key] = self.scenario.product.operation_object(product_config | settings, request)
        self.save()  # Persist the recovery inputs before changing product configuration.
        config.update(settings)
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False));self.config_path.chmod(0o600)
        self.setup_product()

    def restore_configuration(self):
        if not self.state or 'original_config' not in self.state:
            return
        self.validate_identity()
        source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        if not source or source['metadata']['uid'] != self.state['source_cluster_uid']:
            raise ValueError('MongoDB backup configuration recovery refuses a changed source cluster')
        config = yaml.safe_load(self.config_path.read_text())
        current = {key: config.get(key) for key in self.state['applied_config']}
        allowed = [self.state['original_config'], self.state['applied_config']]
        pause_from = self.state.get('retention', {}).get('pause_from')
        if pause_from and pause_from | {'mongodb_backup_schedule_enabled': False} == self.state['applied_config']:
            allowed.append(pause_from)
        if current not in allowed:
            raise ValueError('MongoDB backup configuration recovery refuses concurrent edits')
        self.remove_request()  # A pending backup must stop before disabling PBM.
        config.update(self.state['original_config'])
        for key in self.state.get('original_absent', []):
            config.pop(key, None)
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False));self.config_path.chmod(0o600)
        self.setup_product()
        self.scenario.health()
        self.state.pop('original_config');self.state.pop('applied_config')
        self.state.pop('original_absent', None);self.save()

    def verify_objects(self, persist=True, *, request_name=None, prefix='backup', expected_replsets=None):
        if prefix not in ('backup', 'disaster/' + self.state['run_id'], 'sharding/' + self.state['run_id']):
            raise ValueError('MongoDB backup verification refuses a foreign prefix')
        backup = self.lab.get('psmdb-backup', request_name or self.state['request']['name'], self.scenario.ns)
        status = backup.get('status', {})
        pbm_name = status.get('pbmName', '')
        if (status.get('state') != 'ready' or not status.get('completed') or
                not re.fullmatch(r'[0-9T:Z.-]+', pbm_name) or
                status.get('destination') != 's3://' + self.state['bucket'] + '/' + prefix + '/' + pbm_name):
            raise ValueError('MongoDB backup lacks the completed product request result')
        # mc's JSON keys are relative to its requested path (generateContentMessages
        # in the pinned ls.go). List at the bucket boundary so both accepted and
        # disaster prefixes retain their canonical full object keys.
        listing = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
        rows = [r for r in listing if r.get('type') == 'file' and
                (prefix == 'backup' or str(r.get('key', '')).startswith(prefix + '/'))]
        if (not rows or any(r.get('status') != 'success' or not r.get('size') or
                           not str(r.get('key', '')).startswith(prefix + '/') or '..' in r['key'].split('/') for r in rows)):
            raise ValueError('MongoDB backup has unreadable, empty or unexpected S3 objects')
        metadata = [r for r in rows if r['key'] == prefix + '/' + pbm_name + '.pbm.json']
        data = [r for r in rows if r['key'].startswith(prefix + '/' + pbm_name + '/')]
        if len(metadata) != 1 or not data:
            raise ValueError('MongoDB backup requires remote metadata and data objects')
        raw_meta = json.loads(self.mc('cat', 'gate/' + self.state['bucket'] + '/' + metadata[0]['key']))
        meta = (backup_metadata(raw_meta, self.kind) if expected_replsets is None else
                backup_metadata(raw_meta, self.kind, expected_replsets=expected_replsets))
        if meta['name'] != pbm_name or meta['pbm_version'] != '2.15.0' or meta['fcv'] != '8.0':
            raise ValueError('MongoDB backup metadata identity or version differs from the fixed request')
        checksums = []
        for row in metadata + data:
            target = 'gate/' + self.state['bucket'] + '/' + row['key']
            digest = self.lab.run('-n', self.scenario.ns, 'exec', self.state['name'] + '-client', '--',
                'bash', '-o', 'pipefail', '-ec', 'mc --config-dir /tmp/mc cat "$1" | sha256sum', 'backup-checksum', target, timeout=180).split()[0]
            if not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('MongoDB backup object checksum is invalid')
            checksums.append({'key': row['key'], 'size': row['size'], 'sha256': digest})
        verified = {'uid': backup['metadata']['uid'], 'destination': status['destination'],
                    'metadata': meta, 'objects': checksums, 'source_prefix': self.state['source_prefix']}
        if persist:
            self.state['backup'] = verified
            self.save()
        print('MONGODB_BACKUP_OBJECTS_VERIFIED count=' + str(len(checksums)) + ' checksums=sha256', flush=True)
        return verified

    def validate_identity(self):
        state = self.state
        if (state.get('kind') != self.kind or not re.fullmatch('[0-9a-f]{16}', str(state.get('run_id', ''))) or
                state.get('name') != 'mongodb-backup-' + state['run_id'] or
                self.lab.verify()['cluster_uid'] != state.get('cluster_uid')):
            raise ValueError('MongoDB backup recovery requires matching lab identity')

    def client_boundary(self):
        boundary = []
        for row in self.state['resources']:
            if row['kind'] == 'pod' and row['name'] == self.state['name'] + '-client':continue
            current = self.lab.get(row['kind'], row['name'], self.scenario.ns)
            if not current or current['metadata']['uid'] != row['uid'] or not self.owned(current):
                raise ValueError('MongoDB backup client recovery storage graph changed')
            item = {'kind': row['kind'], 'name': row['name'], 'uid': row['uid'],
                    'labels': current['metadata'].get('labels', {}), 'spec': current.get('spec'),
                    'data_sha256': hashlib.sha256(json.dumps(current.get('data', {}), sort_keys=True).encode()).hexdigest()}
            if row['kind'] == 'pvc':
                volume = self.lab.get('pv', current['spec']['volumeName'])
                ref = volume['spec'].get('claimRef', {}) if volume else {}
                if (not volume or current['spec'].get('storageClassName') != 'mongodb-longhorn' or
                        volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
                        volume['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                        (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                        (self.scenario.ns, row['name'], row['uid'])):
                    raise ValueError('MongoDB backup client recovery volume binding changed')
                item['pv'] = {'uid': volume['metadata']['uid'], 'spec': volume['spec']}
            boundary.append(item)
        return hashlib.sha256(json.dumps(boundary, sort_keys=True).encode()).hexdigest()

    def recover_storage_client(self):
        """Replace only the stateless, UID-bound helper; retain all backup data."""
        self.state = json.loads(self.path.read_text());self.validate_identity();self.scenario.health()
        if ('target' in self.state or list(self.lab.directory.glob('*fault*.json')) or
                self.state.get('backup') is None or self.state.get('original_config')):
            raise ValueError('MongoDB backup client recovery requires an accepted idle backup')
        name = self.state['name'] + '-client'
        row = next(r for r in self.state['resources'] if r['kind'] == 'pod' and r['name'] == name)
        current = self.lab.get('pod', name, self.scenario.ns)
        receipt = self.state.get('client_recovery')
        boundary = self.client_boundary();source = self.source_identity()
        if source['cr_uid'] != self.state.get('source_cluster_uid'):
            raise ValueError('MongoDB backup client recovery source cluster identity changed')
        if receipt:
            if (receipt['boundary'] != boundary or receipt['source'] != source or
                    hashlib.sha256(json.dumps(receipt['document'], sort_keys=True).encode()).hexdigest() != receipt['document_sha256'] or
                    receipt['document']['metadata'] != {'name': name, 'namespace': self.scenario.ns,
                        'labels': LABELS | {'kubeauto.io/test-run': self.state['run_id']}}):
                raise ValueError('MongoDB backup client recovery receipt changed')
        else:
            if not current or current['metadata']['uid'] != row['uid'] or not self.owned(current):
                raise ValueError('MongoDB backup client recovery original Pod identity changed')
            document = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': self.scenario.ns,
                'labels': dict(current['metadata']['labels'])}, 'spec': current['spec'] | {'restartPolicy': 'Always'}}
            receipt = {'old_uid': row['uid'], 'new_uid': None, 'document': document,
                       'document_sha256': hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest(),
                       'boundary': boundary, 'source': source}
            if current['spec']['restartPolicy'] != 'Always':
                self.state['client_recovery'] = receipt;self.save()
        if 'client_recovery' in self.state:
            if current and current['metadata']['uid'] == receipt['old_uid']:
                if row['uid'] != receipt['old_uid'] or receipt['new_uid'] is not None or not self.owned(current):
                    raise ValueError('MongoDB backup client recovery deletion receipt changed')
                self.lab.delete_uid('/api/v1/namespaces/' + self.scenario.ns + '/pods/' + name, receipt['old_uid'])
                self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', 'pod/' + name, '--timeout=120s', timeout=140)
                current = None
            if current and (not receipt['new_uid'] or current['metadata']['uid'] != receipt['new_uid'] or
                    row['uid'] != receipt['new_uid'] or not self.owned(current)):
                raise ValueError('MongoDB backup client recovery refuses an unrecorded replacement')
            if current is None:
                if receipt['new_uid'] is not None:
                    raise ValueError('MongoDB backup client recovery recorded replacement disappeared')
                created = json.loads(self.api_input('create', '-f', '-', '-o', 'json', obj=receipt['document']))
                row['uid'] = receipt['new_uid'] = created['metadata']['uid'];self.save()
        self.lab.run('-n', self.scenario.ns, 'wait', '--for=condition=Ready', 'pod/' + self.state['name'], '--timeout=600s', timeout=620)
        self.lab.run('-n', self.scenario.ns, 'wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s', timeout=200)
        self.mc('ready', 'gate');self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup'] or self.client_boundary() != boundary or self.source_identity() != source:
            raise ValueError('MongoDB backup client recovery changed data, volumes, credentials or source')
        self.state.pop('client_recovery', None);self.save()
        print('MONGODB_BACKUP_CLIENT_RECOVERED kind=' + ('pitr' if 'pitr' in self.path.name else self.kind) +
              ' stateless-pod=ready backup-objects=checksums-verified storage-and-source=unchanged', flush=True)

    def request_for_cleanup(self, operation='backup'):
        request_key = 'restore_request' if operation == 'restore' else 'request'
        desired_key = 'desired_restore' if operation == 'restore' else 'desired_request'
        if request_key not in self.state:
            return None
        request, desired = self.state[request_key], self.state[desired_key]
        expected_name = self.scenario.fixture.NAME + '-' + self.kind + '-' + self.state['run_id']
        if operation == 'restore': expected_name += '-restore'
        if request.get('name') != expected_name or desired['metadata']['name'] != expected_name:
            raise ValueError('MongoDB backup journal has a foreign request')
        current = self.lab.get('psmdb-restore' if operation == 'restore' else 'psmdb-backup', expected_name, self.scenario.ns)
        if current:
            source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
            if not source or source['metadata']['uid'] != self.state['source_cluster_uid']:
                raise ValueError('MongoDB backup cleanup refuses a changed source cluster')
            config = {'mongodb_name': self.scenario.fixture.NAME}
            self.scenario.product.check_operation_identity(config, request, desired, current, source)
            if operation == 'backup' and self.state.get('backup') and current['metadata']['uid'] != self.state['backup']['uid']:
                raise ValueError('MongoDB backup cleanup refuses a replaced request')
        return current

    def delete_request(self, current):
        if not current:
            return
        if current.get('kind') not in ('PerconaServerMongoDBBackup', 'PerconaServerMongoDBRestore'):
            raise ValueError('MongoDB backup cleanup refuses an unknown request kind')
        restoring = current['kind'] == 'PerconaServerMongoDBRestore'
        resource = 'perconaservermongodbrestores' if restoring else 'perconaservermongodbbackups'
        self.api_input('delete', '--raw', '/apis/psmdb.percona.com/v1/namespaces/' + self.scenario.ns +
            '/' + resource + '/' + current['metadata']['name'], '-f', '-',
            obj={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': current['metadata']['uid']}})
        self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', resource + '/' + current['metadata']['name'],
                     '--timeout=180s', timeout=200)

    def remove_request(self):
        if self.state.get('active_operation') == 'restore':
            if not self.state.get('restore_complete'):
                raise ValueError('MongoDB restore requires its owning fault recovery before configuration rollback')
            self.delete_request(self.request_for_cleanup('restore'))
            return
        # Successful artifacts keep their CR until their owning restore finishes.
        if not self.state.get('backup'):
            self.delete_request(self.request_for_cleanup())

    def cleanup_client(self):
        if self.state and self.state.get('client_attempted'):
            prefix = self.state['source_prefix']
            if not re.fullmatch('mongodb_gate_[0-9a-f]{16}', prefix):
                raise ValueError('MongoDB backup client journal has a foreign workload prefix')
            self.scenario.prefix = prefix
            self.scenario.client_attempted = True
            self.scenario.client_uid = self.state.get('client_uid')
            self.scenario.workload_started = self.state.get('workload_started', False)
            current = self.lab.get('pod', self.scenario.client, self.scenario.ns)
            if not current:
                self.scenario.client_uid = None
                if self.scenario.workload_started:
                    # An interruption can land after deleting the client but
                    # before saving its clean state. Re-establish a bounded
                    # client to verify removal of the same run's collections.
                    self.scenario.client_create()
                    self.state['client_uid'] = self.scenario.client_uid;self.save()
                else:
                    self.scenario.client_attempted = False
        self.scenario.cleanup()
        if self.state:
            self.state.update(client_attempted=False, client_uid=None, workload_started=False)
            self.save()

    def cleanup(self):
        if not self.path.exists():
            return
        state = json.loads(self.path.read_text())
        self.state = state
        self.validate_identity()
        self.stability_cleanup(interrupted=True)
        self.cleanup_target()
        self.cleanup_disaster()
        self.cleanup_sharding_restore()
        self.refusal_prefix_cleanup()
        fault_path = self.lab.directory / 'mongodb-fault-restore.json'
        if fault_path.exists():
            fault = json.loads(fault_path.read_text())
            if (not state.get('restore_complete') or fault.get('cluster_uid') != state['cluster_uid'] or
                    fault.get('run_id') != state['run_id'] or fault.get('source_uid') != state.get('source_cluster_uid') or
                    fault.get('backup_journal') != self.path.name or fault.get('type') != self.kind):
                raise ValueError('MongoDB incomplete restore requires data recovery before backup cleanup')
        volumes, plan = set(), []
        expected = {('secret', state['name'] + '-credentials'), ('secret', state['name'] + '-tls'),
                    ('pvc', state['name'] + '-data'), ('service', state['name']),
                    ('pod', state['name']), ('pod', state['name'] + '-client')}
        seen = set()
        for record in reversed(state['resources']):
            kind, name = record['kind'], record['name']
            if (kind, name) not in expected or (kind, name) in seen:
                raise ValueError('MongoDB backup cleanup journal has a foreign resource')
            seen.add((kind, name))
            obj = self.lab.get(kind, name, self.scenario.ns)
            if kind == 'pvc':
                volume = obj['spec'].get('volumeName') if obj else record.get('volume_name')
                if volume:
                    claim_uid = obj['metadata']['uid'] if obj else record['uid']
                    live_volume = self.lab.get('pv', volume)
                    if live_volume:
                        ref = live_volume['spec'].get('claimRef', {})
                        if (live_volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
                                (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                                (self.scenario.ns, name, claim_uid)):
                            raise ValueError('MongoDB backup cleanup refuses a foreign volume')
                    record['volume_name'] = volume
                    record['uid'] = claim_uid
                    volumes.add(volume)
            if not obj:
                continue
            if not self.owned(obj) or record['uid'] not in (None, obj['metadata']['uid']):
                raise ValueError('MongoDB backup cleanup refuses changed resource ownership')
            plan.append((kind, name, obj['metadata']['uid']))
        # Validate the complete journal and CR before the first deletion.
        request = self.request_for_cleanup()
        self.save()  # Retain PV identity across interruption after PVC deletion.
        if 'original_config' in state:
            self.restore_configuration()
            request = self.request_for_cleanup()
        self.cleanup_client()
        self.delete_request(request)
        for kind, name, uid in plan:
            self.api_input('delete', '--raw', '/api/v1/namespaces/' + self.scenario.ns + '/' + KINDS[kind] + '/' + name,
                '-f', '-', obj={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
            self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', kind + '/' + name, '--timeout=180s', timeout=200)
        for volume in volumes:
            self.lab.run('wait', '--for=delete', 'pv/' + volume, '--timeout=300s', timeout=320)
        if fault_path.exists(): fault_path.unlink()
        self.path.unlink()
        print('MONGODB_BACKUP_FIXTURE_CLEAN_PASS resources-pv=absent', flush=True)

    def pitr_window(self):
        result = self.scenario.js("""const before=assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'before-pitr',value:'include'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
const a=c.getDB('admin'),beforeTime=assert.commandWorked(a.runCommand({hello:1})).localTime;
const target=Math.floor(beforeTime.getTime()/1000)+2;
while(assert.commandWorked(a.runCommand({hello:1})).localTime.getTime()<(target+1)*1000){sleep(250);}
assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'after-pitr',value:'exclude'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
const afterTime=assert.commandWorked(a.runCommand({hello:1})).localTime;
assert(afterTime.getTime()>=(target+1)*1000);
print('MONGODB_PITR_BOUNDARY '+JSON.stringify({target_utc:new Date(target*1000).toISOString().slice(0,19).replace('T',' '),target_epoch:target,before_utc:beforeTime.toISOString(),after_utc:afterTime.toISOString()}));
print('MONGODB_PITR_BOUNDARY_PASS');""", 'MONGODB_PITR_BOUNDARY_PASS', app=True)
        rows = [json.loads(row.split(' ', 1)[1]) for row in result.splitlines() if row.startswith('MONGODB_PITR_BOUNDARY ')]
        if len(rows) != 1:raise ValueError('MongoDB PITR requires one exact server-clock boundary')
        boundary = rows[0]
        target = datetime.strptime(boundary['target_utc'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        if target.timestamp() != boundary['target_epoch']:
            raise ValueError('MongoDB PITR boundary timestamp identity differs')
        deadline = time.monotonic() + 480
        while True:
            backup = self.lab.get('psmdb-backup', self.state['request']['name'], self.scenario.ns)
            latest = backup.get('status', {}).get('latestRestorableTime')
            if latest and datetime.fromisoformat(latest.replace('Z', '+00:00')).timestamp() > target.timestamp()+1:
                break
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB PITR oplog coverage did not reach the before/after boundary')
            print('MONGODB_PITR_HEARTBEAT stage=oplog-coverage', flush=True)
            time.sleep(5)
        listing = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
        objects = [r for r in listing if r.get('type') == 'file' and r.get('size', 0) > 0 and
                   str(r.get('key', '')).startswith('backup/pbmPitr/')]
        if not objects or any(not r['key'].startswith('backup/pbmPitr/rs0/') or '..' in r['key'].split('/') for r in objects):
            raise ValueError('MongoDB PITR requires readable rs0 oplog objects')
        checksums = []
        for row in objects:
            digest = self.lab.run('-n', self.scenario.ns, 'exec', self.state['name'] + '-client', '--',
                'bash', '-o', 'pipefail', '-ec', 'mc --config-dir /tmp/mc cat "$1" | sha256sum',
                'pitr-checksum', 'gate/' + self.state['bucket'] + '/' + row['key'], timeout=180).split()[0]
            if not re.fullmatch('[0-9a-f]{64}', digest):raise ValueError('MongoDB PITR oplog checksum is invalid')
            checksums.append({'key':row['key'],'size':row['size'],'sha256':digest})
        self.state['pitr'] = boundary | {'latest_restorable_time':latest,'oplog_objects':checksums}
        self.save()
        print('MONGODB_PITR_OPLOG_PASS objects=' + str(len(checksums)) + ' target-covered=true checksums=sha256', flush=True)

    def execute(self, *, pitr=False):
        first_error = None
        try:
            self.prepare()
            self.state['client_attempted'] = True;self.save()
            self.scenario.client_create()
            self.state.update(client_uid=self.scenario.client_uid, workload_started=True);self.save()
            self.scenario.workload_started = True
            self.scenario.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'backup',value:NumberDecimal('19.95')}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'backup_value'}]}));print('MONGODB_BACKUP_SEED_PASS');",
                             'MONGODB_BACKUP_SEED_PASS', app=True)
            self.configure(pitr=True) if pitr else self.configure()
            self.verify_objects()
            if pitr:self.pitr_window()
        except BaseException as exc:
            first_error = exc
            try:
                self.diagnostics()
            except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
                print('MONGODB_BACKUP_DIAGNOSTICS_UNAVAILABLE', file=sys.stderr)
            raise
        finally:
            try:
                self.restore_configuration()
                self.cleanup_client()
                if first_error is not None and self.state is not None:
                    self.cleanup()
            except BaseException:
                print('MONGODB_BACKUP_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None: raise
        self.scenario.health()
        if pitr:
            self.restore_independent(pitr=True)
            print('MONGODB_PITR_SCOPED_CLEAN_PASS retained=verified-pitr-backup', flush=True)
            print('MONGODB_PITR_PASS', flush=True)
            return
        print('MONGODB_' + self.kind.upper() + '_BACKUP_SCOPED_CLEAN_PASS retained=owned-backup-artifact', flush=True)
        print('MONGODB_' + self.kind.upper() + '_BACKUP_PASS', flush=True)

    def source_identity(self):
        source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        claims = self.lab.get('pvc', ns=self.scenario.ns)['items']
        result = {'cr_uid': source['metadata']['uid'], 'claims': {}, 'secrets': {}}
        for claim in claims:
            if claim['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.scenario.fixture.NAME:
                result['claims'][claim['metadata']['name']] = {
                    'uid': claim['metadata']['uid'], 'volume': claim['spec']['volumeName']}
        if len(result['claims']) != 3:
            raise ValueError('MongoDB independent restore requires exactly three source claims')
        for suffix in ('users', 'app', 'key', 'tls', 'tls-internal'):
            obj = self.lab.get('secret', self.scenario.fixture.NAME + '-' + suffix, self.scenario.ns)
            result['secrets'][suffix] = {'uid': obj['metadata']['uid'],
                'sha256': hashlib.sha256(json.dumps(obj['data'], sort_keys=True).encode()).hexdigest()}
        return result

    def target_setup(self):
        deployment = self.state['target']['deployment']
        result = subprocess.run(['kubecli', 'setup', deployment, '07', '--tags', 'mongodb'],
                                cwd=self.base, timeout=5400)
        self.capture_target()
        if result.returncode:
            raise ValueError('MongoDB independent target product configuration/request failed')

    def target_request(self, request):
        target = self.state['target']
        path = self.base / 'clusters' / target['deployment'] / 'config.yml'
        config = yaml.safe_load(path.read_text())
        target['request'] = request
        target['desired_request'] = self.scenario.product.operation_object(config, request)
        kind = 'psmdb-backup' if request['kind'] == 'backup' else 'psmdb-restore'
        target.setdefault('operation_requests', {})[kind] = {
            'request': request, 'desired': target['desired_request']}
        self.save()
        config['mongodb_requests'] = [request]
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        self.target_setup()
        current = self.lab.get(kind, request['name'], self.scenario.ns)
        self.scenario.product.check_operation_identity(config, request, target['desired_request'], current,
            self.lab.get('psmdb', target['name'], self.scenario.ns))
        if not self.scenario.product.operation_complete(request['kind'], current.get('status', {})):
            raise ValueError('MongoDB disaster request did not complete')
        return current

    def disaster_absent(self):
        lost = self.state['disaster']['lost']
        for record in lost['resources']:
            if self.lab.get(record['kind'], record['name'], self.scenario.ns):
                raise ValueError('MongoDB disaster source still has a Kubernetes resource')
            if record['kind'] == 'pvc' and self.lab.get('pv', record['volume_name']):
                raise ValueError('MongoDB disaster source still has a data volume')

    def disaster_objects(self):
        """Verify off-source recovery material without a source CR or Backup CR."""
        disaster = self.state['disaster'];prefix = 'disaster/' + self.state['run_id']
        if disaster.get('prefix') != prefix:
            raise ValueError('MongoDB disaster has a foreign object prefix')
        expected = disaster['backup']['objects']
        listing = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
        # PBM 2.15.0 defs.StorInitFile and defs.PhysRestoresDir hold storage
        # initialization and mutable restore coordination, not backup payload.
        # Keep the same metadata/data boundary as verify_objects(), while still
        # rejecting any unexpected payload, missing object or duplicate key.
        rows = [r for r in listing if r.get('type') == 'file' and
                str(r.get('key', '')).startswith(prefix + '/') and
                r['key'] != prefix + '/.pbm.init' and
                not r['key'].startswith(prefix + '/.pbm.restore/')]
        if ({(r.get('key'), r.get('size')) for r in rows} != {(r['key'], r['size']) for r in expected} or
                len(rows) != len(expected) or not expected or
                any(r.get('status') != 'success' for r in rows)):
            raise ValueError('MongoDB disaster backup object inventory changed after source loss')
        for row in expected:
            if not row['key'].startswith(prefix + '/') or '..' in row['key'].split('/'):
                raise ValueError('MongoDB disaster refuses a foreign recovery object')
            target = 'gate/' + self.state['bucket'] + '/' + row['key']
            digest = self.lab.run('-n', self.scenario.ns, 'exec', self.state['name'] + '-client', '--',
                'bash', '-o', 'pipefail', '-ec', 'mc --config-dir /tmp/mc cat "$1" | sha256sum',
                'disaster-checksum', target, timeout=180).split()[0]
            if digest != row['sha256']:
                raise ValueError('MongoDB disaster backup object checksum changed after source loss')
        print('MONGODB_DISASTER_OBJECTS_PASS source-backup-cr=unnecessary sha256=unchanged', flush=True)

    def cleanup_disaster(self):
        if not self.state or 'disaster' not in self.state:return
        self.validate_identity()
        if 'target' in self.state:
            raise ValueError('MongoDB disaster cleanup requires its target to be absent')
        disaster = self.state['disaster'];prefix = 'disaster/' + self.state['run_id']
        escrow = self.lab.directory / ('mongodb-disaster-escrow-' + self.state['run_id'] + '.json')
        if (disaster.get('prefix') != prefix or escrow.is_symlink() or
                (escrow.exists() and (escrow.stat().st_mode & 0o777 != 0o600 or
                 hashlib.sha256(escrow.read_bytes()).hexdigest() != disaster.get('escrow_sha256')))):
            raise ValueError('MongoDB disaster cleanup refuses foreign recovery material')
        target = 'gate/' + self.state['bucket'] + '/' + prefix + '/'
        self.mc('rm', '--recursive', '--force', target)
        if any(r.get('type') == 'file' for r in json_messages(self.mc('ls', '--recursive', '--json', target))):
            raise ValueError('MongoDB disaster cleanup left recovery objects')
        if escrow.exists():escrow.unlink()
        self.state.pop('disaster');self.save()

    def cleanup_sharding_restore(self):
        if not self.state or 'sharding_restore' not in self.state:return
        self.validate_identity()
        prefix = 'sharding/' + self.state['run_id']
        if 'target' in self.state or self.state['sharding_restore']['prefix'] != prefix:
            raise ValueError('MongoDB sharded recovery cleanup refuses an active target or foreign prefix')
        location = 'gate/' + self.state['bucket'] + '/' + prefix + '/'
        self.mc('rm', '--recursive', '--force', location)
        if any(r.get('type') == 'file' for r in json_messages(self.mc('ls', '--recursive', '--json', location))):
            raise ValueError('MongoDB sharded recovery objects remain after cleanup')
        self.state.pop('sharding_restore');self.save()

    def upgrade_versions(self, operator, server):
        cr = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        deployment = self.lab.get('deployment', 'mongodb-operator', self.scenario.ns)
        if (cr['spec']['crVersion'] != operator or
                not cr['spec']['image'].endswith(':' + server) or
                not deployment['spec']['template']['spec']['containers'][0]['image'].endswith(':' + operator)):
            raise ValueError('MongoDB upgrade declared controller or server version differs')
        pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items']
                if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.scenario.fixture.NAME and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
        if len(pods) != 3 or any(not self.scenario.product.ready(p) or not any(
                c['name'] == 'mongod' and c['image'].endswith(':' + server)
                for c in p['spec']['containers']) for p in pods):
            raise ValueError('MongoDB upgrade requires three actual pinned server containers')
        self.scenario.js("""const version=""" + json.dumps(server) + """;
for(let i=0;i<3;i++){
 const uri='mongodb://mongodb-gate-data-rs0-'+i+'.mongodb-gate-data-rs0.mongodb-gate.svc.cluster.local:27017/admin?directConnection=true&tls=true&tlsCAFile=/tls/ca.crt&serverSelectionTimeoutMS=10000';
 const a=new Mongo(uri).getDB('admin');
 assert(a.auth({user:process.env.MONGODB_DATABASE_ADMIN_USER,pwd:process.env.MONGODB_DATABASE_ADMIN_PASSWORD}));
 assert.eq(version,assert.commandWorked(a.runCommand({buildInfo:1})).version);
 const f=assert.commandWorked(a.runCommand({getParameter:1,featureCompatibilityVersion:1})).featureCompatibilityVersion;
 assert.eq('8.0',f.version);assert(!f.targetVersion&&!f.previousVersion);
}
print('MONGODB_UPGRADE_BINARY_FCV_PASS');""", 'MONGODB_UPGRADE_BINARY_FCV_PASS')
        return {p['metadata']['name']: p['metadata']['uid'] for p in pods}

    def upgrade_crds(self, chart):
        import tarfile
        with tarfile.open(self.base / 'roles/cluster-addon/files' / ('psmdb-operator-' + chart + '.tgz')) as archive:
            desired = list(yaml.safe_load_all(archive.extractfile('psmdb-operator/crds/crd.yaml')))
        result = {}
        for obj in desired:
            name = obj['metadata']['name'];live = self.lab.get('crd', name)
            if not live or live['spec']['versions'] != obj['spec']['versions']:
                raise ValueError('MongoDB upgrade requires actual CRD schema from pinned Chart ' + chart)
            result[name] = {'uid': live['metadata']['uid'], 'versions_sha256':
                hashlib.sha256(json.dumps(live['spec']['versions'], sort_keys=True).encode()).hexdigest()}
        return result

    @contextmanager
    def upgrade_observe(self):
        stop = threading.Event();samples = [];errors = []
        def sample():
            pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items']
                    if p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == self.scenario.fixture.NAME and
                    p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
            rows = [{'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
                     'ready': self.scenario.product.ready(p), 'images':
                     [c['image'] for c in p['spec']['containers'] if c['name'] == 'mongod']} for p in pods]
            if (not 2 <= len(rows) <= 3 or sum(r['ready'] for r in rows) < 2 or
                    any(r['name'] not in {self.scenario.fixture.NAME + '-rs0-' + str(i) for i in range(3)} for r in rows)):
                raise ValueError('MongoDB upgrade rolling observation lost two-ready-member boundary')
            samples.append({'time': time.time(), 'members': rows})
        def observe():
            while not stop.wait(2):
                try:sample()
                except BaseException as exc:errors.append(exc);return
        sample();thread = threading.Thread(target=observe, daemon=True);thread.start();first_error = None
        try:yield
        except BaseException as exc:first_error = exc;raise
        finally:
            stop.set();thread.join(timeout=65)
            self.scenario.fixture.write_private(self.lab.directory / 'mongodb-upgrade-observations.json', samples)
            if first_error is None:
                if thread.is_alive():raise ValueError('MongoDB upgrade observation did not stop')
                if errors:raise errors[0]
                sample()
                print('MONGODB_UPGRADE_ROLLING_PASS samples=' + str(len(samples)) + ' ready-members>=2', flush=True)

    def bind_upgrade_crds(self, q):
        """Advance cleanup receipts only for the original, pinned forward CRDs."""
        owner = self.lab.verify()
        identity = owner.get('product_identity', {})
        old, new = q['old_crds'], q['new_crds']
        if (owner['cluster_uid'] != q['cluster_uid'] or
                self.source_identity() != q['source'] or identity.get('source_uid') != q['source']['cr_uid'] or
                set(old) != set(new) or any(old[n]['uid'] != new[n]['uid'] for n in old) or
                not any(old[n]['versions_sha256'] != new[n]['versions_sha256'] for n in old) or
                self.upgrade_crds('1.23.2') != new or identity.get('crds') not in (old, new)):
            raise ValueError('MongoDB upgrade cleanup refuses changed source or unproven forward CRDs')
        identity['crds'] = new
        self.scenario.fixture.write_private(self.lab.owner_path, owner)

    def upgrade_recover(self):
        """Restore an unapplied stage, or retire a proven forward fixture without downgrade."""
        journal = self.lab.directory / 'mongodb-upgrade.json'
        q = json.loads(journal.read_text())
        owner = self.lab.verify()
        original = q['original_config']
        expected = {'mongodb_operator_ver': '1.23.0', 'mongodb_operator_chart_ver': '1.23.1',
                    'mongodb_ver': '8.0.26-11', 'mongodb_fluentbit_ver': '5.0.9-1'}
        applied = original | {'mongodb_operator_ver': '1.23.1',
                              'mongodb_operator_chart_ver': '1.23.2', 'mongodb_crd_upgrade': True}
        completed = q.get('stage') == 'post-upgrade-backup-restored'
        forward = q.get('stage') == 'server' or completed
        if forward:
            applied |= {'mongodb_ver': '8.0.32-14', 'mongodb_fluentbit_ver': '5.1.2-1',
                        'mongodb_crd_upgrade': False}
        if (q.get('stage') not in ('operator', 'server', 'post-upgrade-backup-restored') or q.get('client_uid') is not None or
                owner.get('healthy') is not True or q.get('cluster_uid') != owner['cluster_uid'] or
                any(original.get(k) != v for k, v in expected.items()) or
                q.get('applied_config') != applied or
                yaml.safe_load(self.config_path.read_text()) not in ((applied,) if forward else (original, applied)) or
                not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', q.get('workload', '')) or
                list(self.lab.directory.glob('*fault*.json')) or
                self.source_identity() != q['source'] or
                self.upgrade_crds('1.23.2' if forward else '1.23.1') != q.get('new_crds' if forward else 'old_crds')):
            raise ValueError('MongoDB upgrade recovery refuses changed versions, identities, configuration or stage')
        if completed:
            if list(self.lab.directory.glob('mongodb-backup-*.json')):
                raise ValueError('MongoDB completed upgrade cleanup refuses surviving backup fixtures')
            self.scenario.health()
            try:
                self.scenario.client_create()
                self.upgrade_versions('1.23.1', '8.0.32-14')
            finally:
                self.scenario.delete_client()
            self.lab.bind_product()
            self.bind_upgrade_crds(q)
            self.lab.cleanup_product()
            journal.unlink()
            (self.lab.directory / 'mongodb-upgrade-observations.json').unlink(missing_ok=True)
            print('MONGODB_UPGRADE_RECOVER_SCOPED_CLEAN_PASS forward-product-volumes-crds=absent core-csi=retained', flush=True)
            print('MONGODB_UPGRADE_RECOVER_PASS', flush=True)
            return
        self.state = json.loads(self.path.read_text())
        self.validate_identity()
        if self.state['cluster_uid'] != q['cluster_uid']:
            raise ValueError('MongoDB upgrade recovery backup belongs to another core')
        self.scenario.health()
        self.scenario.prefix = q['workload']
        try:
            self.scenario.client_create()
            self.upgrade_versions('1.23.1', '8.0.32-14') if forward else self.upgrade_versions('1.23.0', '8.0.26-11')
            self.verify_objects(False)
            self.scenario.clean_workload()
        finally:
            self.scenario.delete_client()
        restored = applied if forward else original
        self.config_path.write_text(yaml.safe_dump(restored, sort_keys=False));self.config_path.chmod(0o600)
        self.setup_product();self.scenario.health()
        if self.source_identity() != q['source']:
            raise ValueError('MongoDB upgrade recovery changed source identity')
        self.cleanup()
        if forward:
            self.bind_upgrade_crds(q)
            self.lab.cleanup_product()
        elif self.upgrade_crds('1.23.1') != q['old_crds']:
            raise ValueError('MongoDB upgrade recovery changed the old CRDs')
        journal.unlink()
        (self.lab.directory / 'mongodb-upgrade-observations.json').unlink(missing_ok=True)
        scope = ('forward-product-volumes-crds=absent core-csi=retained' if forward else
                 'old-schema-binaries-volumes-secrets=retained failed-fixtures=absent')
        print('MONGODB_UPGRADE_RECOVER_SCOPED_CLEAN_PASS ' + scope, flush=True)
        print('MONGODB_UPGRADE_RECOVER_PASS', flush=True)

    def upgrade(self):
        """An isolated old-CRD install is upgraded through two product changes."""
        journal = self.lab.directory / 'mongodb-upgrade.json'
        if (self.path.exists() or journal.exists() or list(self.lab.directory.glob('*fault*.json')) or
                any(self.lab.directory.glob('mongodb-backup-*.json'))):
            raise ValueError('MongoDB upgrade requires a fresh isolated old-version fixture')
        config = yaml.safe_load(self.config_path.read_text())
        expected = {'mongodb_operator_ver': '1.23.0', 'mongodb_operator_chart_ver': '1.23.1',
                    'mongodb_ver': '8.0.26-11', 'mongodb_fluentbit_ver': '5.0.9-1'}
        if any(config.get(k) != v for k, v in expected.items()):
            raise ValueError('MongoDB upgrade old configuration is not the pinned version pair')
        self.scenario.health();old_crds = self.upgrade_crds('1.23.1')
        # This creates and verifies the pre-upgrade backup via config + kubecli.
        self.execute()
        source = self.source_identity();before = self.verify_objects(False)
        q = {'cluster_uid': self.state['cluster_uid'], 'source': source, 'old_crds': old_crds,
             'workload': 'mongodb_gate_' + secrets.token_hex(8), 'client_uid': None,
             'original_config': yaml.safe_load(self.config_path.read_text()), 'stage': 'backup-verified'}
        def save():self.scenario.fixture.write_private(journal, q)
        save();first_error = None
        try:
            self.scenario.prefix = q['workload']
            try:self.scenario.client_create()
            finally:q['client_uid'] = self.scenario.client_uid;save()
            old_pods = self.upgrade_versions('1.23.0', '8.0.26-11')
            self.scenario.js("const documents=Array.from({length:4096},(_,i)=>({_id:i,payload:'x'.repeat(1024)}));assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents,writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{payload:1},name:'upgrade_payload'}]}));print('MONGODB_UPGRADE_SEED_PASS');", 'MONGODB_UPGRADE_SEED_PASS', app=True)
            def inject():
                for stage,changes in (
                    ('operator', {'mongodb_operator_ver': '1.23.1', 'mongodb_operator_chart_ver': '1.23.2', 'mongodb_crd_upgrade': True}),
                    ('server', {'mongodb_ver': '8.0.32-14', 'mongodb_fluentbit_ver': '5.1.2-1', 'mongodb_crd_upgrade': False})):
                    if self.scenario.worker.poll() is not None:
                        raise ValueError('MongoDB upgrade workload ended before its product change')
                    current = yaml.safe_load(self.config_path.read_text())
                    if current != q.get('applied_config', q['original_config']):
                        raise ValueError('MongoDB upgrade refuses concurrent configuration changes')
                    q.update(stage=stage, applied_config=current | changes);save()
                    self.config_path.write_text(yaml.safe_dump(q['applied_config'], sort_keys=False));self.config_path.chmod(0o600)
                    self.setup_product();self.scenario.health()
                    if self.source_identity() != source:raise ValueError('MongoDB upgrade changed source volumes or credentials')
                    current_crds = self.upgrade_crds('1.23.2')
                    if ({n: x['uid'] for n,x in current_crds.items()} != {n: x['uid'] for n,x in old_crds.items()} or
                            all(current_crds[n]['versions_sha256'] == old_crds[n]['versions_sha256'] for n in old_crds)):
                        raise ValueError('MongoDB upgrade did not update the actual original CRD schema')
                    q['new_crds'] = current_crds;save()
                    if self.scenario.worker.poll() is not None:
                        raise ValueError('MongoDB upgrade workload did not span its product change')
                    print('MONGODB_UPGRADE_STAGE_PASS stage=' + stage + ' source-identities=retained crd=forward', flush=True)
            with self.upgrade_observe():
                result = self.scenario_module.Performance(self.scenario).phase(
                    {'phase': 'upgrade', 'concurrency': 4, 'seconds': 3600, 'fault': True}, inject=inject)
            new_pods = self.upgrade_versions('1.23.1', '8.0.32-14')
            if old_pods.keys() != new_pods.keys() or any(new_pods[n] == old_pods[n] for n in old_pods):
                raise ValueError('MongoDB upgrade did not roll every member')
            self.scenario.js("const rows=d.getCollection(prefix+'_documents').find({}).sort({_id:1}).toArray();assert.eq(4096,rows.length);for(let i=0;i<4096;i++){assert.eq(i,rows[i]._id);assert.eq('x'.repeat(1024),rows[i].payload);}assert.eq(1,d.getCollection(prefix+'_documents').getIndexes().filter(x=>x.name==='upgrade_payload'&&x.key.payload===1).length);print('MONGODB_UPGRADE_DATA_INDEX_PASS');", 'MONGODB_UPGRADE_DATA_INDEX_PASS', app=True)
            q.update(stage='data-verified', result=result);save()
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:
                self.scenario.prefix = q['workload'];self.scenario.client_uid = q['client_uid']
                self.scenario.stop_worker();self.scenario.wait_health(600)
                self.scenario.clean_workload();self.scenario.delete_client()
                q['client_uid'] = None;save()
            except BaseException:
                print('MONGODB_UPGRADE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if self.verify_objects(False) != before:raise ValueError('MongoDB upgrade changed its pre-upgrade backup')
        self.restore_independent();self.cleanup()
        # A fresh backup and real independent restore also exercise new-version PBM.
        fresh = BackupGate(self.base, 'logical');fresh.execute();fresh.restore_independent();fresh.cleanup()
        if self.source_identity() != source:raise ValueError('MongoDB upgrade post-backup changed source identity')
        q['stage'] = 'post-upgrade-backup-restored';save()
        # bind-product runs only after these checks, so cleanup records the
        # verified forward CRD schema, never an arbitrary observed schema.
        self.bind_upgrade_crds(q);self.lab.cleanup_product()
        journal.unlink()
        (self.lab.directory / 'mongodb-upgrade-observations.json').unlink()
        print('MONGODB_UPGRADE_SCOPED_CLEAN_PASS old-fixture-data-clients-backups-volumes-crds=absent', flush=True)
        print('MONGODB_UPGRADE_PASS', flush=True)

    def stability_boundary(self):
        result = self.scenario.snapshot();result.pop('pods')
        operator = self.lab.get('deployment', 'mongodb-operator', self.scenario.ns)
        result['operator'] = {'uid': operator['metadata']['uid'], 'spec': operator['spec']}
        return result

    def stability_health(self):
        self.scenario.wait_health(600)
        if self.state['stability']['topology'] == 'sharded':
            deadline = time.monotonic() + 600
            while True:
                try:self.target_health();return
                except (ValueError, subprocess.TimeoutExpired):
                    if time.monotonic() >= deadline:raise
                    print('MONGODB_STABILITY_HEARTBEAT stage=sharded-recovery', flush=True);time.sleep(5)

    def stability_pod_recover(self, *, replacement=False):
        q = self.state['stability'];fault = q.get('pod_fault')
        if not fault:return
        deadline = time.monotonic() + 600
        while True:
            owner = self.lab.get(fault['owner_kind'], fault['owner_name'], self.scenario.ns)
            if not owner or owner['metadata']['uid'] != fault['owner_uid']:
                raise ValueError('MongoDB stability Pod controller identity changed')
            if fault.get('operator_uid'):
                operator = self.lab.get('deployment', 'mongodb-operator', self.scenario.ns)
                if (not operator or {'uid': operator['metadata']['uid'], 'spec': operator['spec']} !=
                        q['before']['operator']):
                    raise ValueError('MongoDB stability Operator deployment identity changed')
            pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items'] if any(
                r.get('uid') == fault['owner_uid'] and r.get('controller') is True
                for r in p['metadata'].get('ownerReferences', []))]
            expected = 1 if fault['owner_kind'] == 'replicaset' else 3
            if (len(pods) == expected and all(self.scenario.product.ready(p) for p in pods) and
                    (not replacement or not any(p['metadata']['uid'] == fault['uid'] for p in pods))):
                self.stability_health()
                if q['topology'] == 'sharded' and fault['owner_kind'] == 'statefulset':
                    records = [r for r in self.state['target']['resources']
                               if r['kind'] == 'pod' and r['name'] == fault['pod']]
                    replacements = [p for p in pods if p['metadata']['name'] == fault['pod']]
                    if (len(records) != 1 or len(replacements) != 1 or records[0]['uid'] not in
                            (fault['uid'], replacements[0]['metadata']['uid'])):
                        raise ValueError('MongoDB stability target Pod recovery receipt changed')
                    records[0]['uid'] = replacements[0]['metadata']['uid']
                q.pop('pod_fault');self.save()
                return
            if time.monotonic() >= deadline:raise ValueError('MongoDB stability Pod replacement deadline exceeded')
            print('MONGODB_STABILITY_HEARTBEAT stage=pod-replacement', flush=True);time.sleep(5)

    def stability_restart(self, kind):
        q = self.state['stability'];self.stability_health()
        if q.get('pod_fault'):raise ValueError('MongoDB stability requires its prior Pod fault recovered')
        if kind == 'member':
            instance = self.state['target']['name'] if q['topology'] == 'sharded' else self.scenario.fixture.NAME
            owner_kind, owner_name = 'statefulset', instance + '-rs0'
            owner = self.lab.get(owner_kind, owner_name, self.scenario.ns)
            pod = self.lab.get('pod', owner_name + '-0', self.scenario.ns)
            cr_uid = self.state['target']['cr_uid'] if q['topology'] == 'sharded' else self.state['source_cluster_uid']
            if not owner or not any(r.get('uid') == cr_uid and r.get('controller') is True
                    for r in owner['metadata'].get('ownerReferences', [])):
                raise ValueError('MongoDB stability member controller does not belong to its source CR')
            if (not pod or pod['metadata'].get('labels', {}).get('app.kubernetes.io/instance') != instance):
                raise ValueError('MongoDB stability refuses a foreign member')
            operator_uid = None
        elif kind == 'operator':
            operator = self.lab.get('deployment', 'mongodb-operator', self.scenario.ns)
            if {'uid': operator['metadata']['uid'], 'spec': operator['spec']} != q['before']['operator']:
                raise ValueError('MongoDB stability Operator identity changed before fault')
            replicas = [r for r in self.lab.get('replicasets', ns=self.scenario.ns)['items'] if
                any(o.get('uid') == operator['metadata']['uid'] and o.get('controller') is True
                    for o in r['metadata'].get('ownerReferences', [])) and r['spec'].get('replicas') == 1]
            if len(replicas) != 1:raise ValueError('MongoDB stability requires one owned Operator ReplicaSet')
            owner = replicas[0];owner_kind, owner_name = 'replicaset', owner['metadata']['name']
            pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items'] if any(
                r.get('uid') == owner['metadata']['uid'] and r.get('controller') is True
                for r in p['metadata'].get('ownerReferences', []))]
            if len(pods) != 1:raise ValueError('MongoDB stability requires one owned Operator Pod')
            pod, operator_uid = pods[0], operator['metadata']['uid']
        else:raise ValueError('MongoDB stability unknown Pod fault')
        if not owner or not self.scenario.product.ready(pod) or not any(
                r.get('uid') == owner['metadata']['uid'] and r.get('controller') is True
                for r in pod['metadata'].get('ownerReferences', [])):
            raise ValueError('MongoDB stability Pod owner or readiness is unproven')
        q['pod_fault'] = {'pod': pod['metadata']['name'], 'uid': pod['metadata']['uid'],
            'owner_kind': owner_kind, 'owner_name': owner_name, 'owner_uid': owner['metadata']['uid'],
            'operator_uid': operator_uid}
        self.save()
        self.lab.delete_uid('/api/v1/namespaces/' + self.scenario.ns + '/pods/' + pod['metadata']['name'], pod['metadata']['uid'])
        self.stability_pod_recover(replacement=True)
        print('MONGODB_STABILITY_RESTART_PASS topology=' + q['topology'] + ' role=' + kind + ' replacement-uid=verified', flush=True)

    def stability_recover_init(self):
        """Recreate the recorded disposable router after the proven init replay bug.

        Only the router's emptyDir is replaced; all database volumes remain.
        This repairs a failed fixture and never emits stability acceptance.
        """
        q = self.state['stability']
        if q['topology'] != 'sharded' or q.get('pod_fault'):return
        target = self.state['target'];name = target['name'] + '-mongos'
        pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items']
                if re.fullmatch(re.escape(name) + '-[0-2]', p['metadata']['name']) and
                any(c['name'] == 'mongo-init' and c.get('state', {}).get('waiting', {}).get('reason') == 'CrashLoopBackOff'
                    for c in p['status'].get('initContainerStatuses', []))]
        if not pods:return
        if len(pods) != 1:raise ValueError('MongoDB init recovery requires one proven disposable router')
        pod = pods[0];owner = self.lab.get('statefulset', name, self.scenario.ns)
        expected = {('pod', pod['metadata']['name']): pod['metadata']['uid'],
                    ('statefulset', name): owner['metadata']['uid']}
        records = {(r['kind'], r['name']): r['uid'] for r in target['resources']}
        if (any(records.get(key) != uid for key, uid in expected.items()) or
                not any(r.get('controller') is True and r.get('uid') == owner['metadata']['uid']
                        for r in pod['metadata'].get('ownerReferences', [])) or
                not any(r.get('controller') is True and r.get('uid') == target['cr_uid']
                        for r in owner['metadata'].get('ownerReferences', []))):
            raise ValueError('MongoDB init recovery router or controller identity changed')
        log = self.lab.run('-n', self.scenario.ns, 'logs', pod['metadata']['name'],
                           '-c', 'mongo-init', '--tail=40', timeout=35)
        expected_error = "chown: changing ownership of '/opt/percona/logcollector/logrotate/conf.d': Operation not permitted"
        if expected_error not in log:
            raise ValueError('MongoDB init recovery refuses an unproven initialization failure')
        q['pod_fault'] = {'pod': pod['metadata']['name'], 'uid': pod['metadata']['uid'],
            'owner_kind': 'statefulset', 'owner_name': name, 'owner_uid': owner['metadata']['uid'], 'operator_uid': None}
        self.save()
        self.lab.delete_uid('/api/v1/namespaces/' + self.scenario.ns + '/pods/' + pod['metadata']['name'], pod['metadata']['uid'])
        self.stability_pod_recover(replacement=True)
        print('MONGODB_STABILITY_INIT_RECOVER_PASS disposable-router-emptydir=recreated volumes=retained', flush=True)

    def stability_cleanup(self, *, interrupted=False):
        if 'stability' not in self.state:return
        q = self.state['stability']
        if (not re.fullmatch(r'mongodb_gate_[0-9a-f]{16}', q['workload']) or
                q['topology'] not in ('source', 'sharded') or q['source'] != self.source_identity()):
            raise ValueError('MongoDB stability recovery identity changed')
        fault = self.lab.directory / 'mongodb-fault-stability.json'
        expected = {'cluster_uid': self.state['cluster_uid'], 'run_id': self.state['run_id'],
                    'workload': q['workload'], 'topology': q['topology']}
        if fault.is_symlink() or (fault.exists() and json.loads(fault.read_text()) != expected):
            raise ValueError('MongoDB stability recovery refuses a foreign fault journal')
        self.scenario.prefix = q['workload'];self.scenario.client_uid = q.get('client_uid')
        self.scenario_module.WorkerRestart(self.scenario).recover()
        self.validate_identity()
        if interrupted:self.stability_recover_init()
        self.stability_pod_recover();self.stability_health()
        before = self.stability_boundary()
        if before != q['before']:raise ValueError('MongoDB stability source or target data identities changed')
        current = self.lab.get('pod', self.scenario.client, self.scenario.ns)
        if current and (current['metadata']['uid'] != q.get('client_uid') or
                current['metadata'].get('labels', {}).get('kubeauto.io/test-run') != q['workload']):
            raise ValueError('MongoDB stability recovery refuses a replaced client')
        if not current and q.get('client_uid') and not q.get('client_deletion_authorized'):
            raise ValueError('MongoDB stability recovery lost its original client')
        if (interrupted or self.scenario.worker is not None) and current and not q.get('data_cleaned'):
            q['client_deletion_authorized'] = True;self.save()
            self.scenario.delete_client();q['client_uid'] = None;self.save();current = None
        if self.scenario.worker is not None:
            if self.scenario.worker.poll() is None:self.scenario.worker.kill()
            self.scenario.worker.communicate(timeout=10);self.scenario.worker = None
        if not q.get('data_cleaned'):
            if not current:
                name = self.state['target']['name'] if q['topology'] == 'sharded' else None
                self.scenario.client_create(name, sharded=q['topology'] == 'sharded')
                q['client_uid'] = self.scenario.client_uid;q.pop('client_deletion_authorized', None);self.save()
            self.scenario.clean_workload();q['data_cleaned'] = True;self.save()
        q['client_deletion_authorized'] = True;self.save();self.scenario.delete_client()
        q['client_uid'] = None;self.save()
        after = self.stability_boundary()
        if after != q['before']:raise ValueError('MongoDB stability cleanup changed retained data identities')
        sharded = q['topology'] == 'sharded'
        if sharded:self.cleanup_target()
        if fault.exists():fault.unlink()
        self.state.pop('stability');self.save();self.scenario.health()
        print('MONGODB_STABILITY_PHASE_SCOPED_CLEAN_PASS topology=' + ('sharded' if sharded else 'source'), flush=True)

    def stability(self, *, focused=False):
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (self.kind != 'logical' or any(k in self.state for k in ('target', 'original_config', 'stability')) or
                list(self.lab.directory.glob('*fault*.json'))):
            raise ValueError('MongoDB stability requires its clean accepted logical backup baseline')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(False) != self.state['backup']:
            raise ValueError('MongoDB stability original backup changed')
        results = [];seconds = 180 if focused else 1800
        for topology in ('source', 'sharded'):
            first_error = None
            try:
                if topology == 'sharded':
                    self.prepare_target(sharded=True)
                    path = self.base / 'clusters' / self.state['target']['deployment'] / 'config.yml'
                    config = yaml.safe_load(path.read_text());name = self.state['target']['name']
                    config['mongodb_users'] = [{'name': 'application', 'db': 'appdb',
                        'passwordSecretRef': {'name': name + '-app', 'key': 'password'},
                        'roles': [{'name': 'readWrite', 'db': 'appdb'}]}]
                    path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600);self.target_setup()
                before = self.stability_boundary()
                self.scenario.prefix = 'mongodb_gate_' + secrets.token_hex(8)
                self.state['stability'] = {'topology': topology, 'workload': self.scenario.prefix,
                    'source': self.source_identity(), 'before': before, 'client_uid': None}
                self.save()
                self.scenario.fixture.write_private(self.lab.directory / 'mongodb-fault-stability.json', {
                    'cluster_uid': self.state['cluster_uid'], 'run_id': self.state['run_id'],
                    'workload': self.scenario.prefix, 'topology': topology})
                try:
                    self.scenario.client_create(self.state['target']['name'] if topology == 'sharded' else None,
                        sharded=topology == 'sharded')
                finally:
                    self.state['stability']['client_uid'] = self.scenario.client_uid;self.save()
                if topology == 'sharded':
                    self.scenario.js("""const m=new Mongo(process.env.MONGODB_URI),admin=m.getDB('admin');
assert(admin.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));
assert.eq(['rs0','rs1'],assert.commandWorked(admin.runCommand({listShards:1})).shards.map(s=>s._id).sort());
const ns='appdb.'+prefix+'_documents';
assert.commandWorked(admin.runCommand({shardCollection:ns,key:{_id:1}}));
assert.commandWorked(admin.runCommand({split:ns,middle:{_id:2048}}));
for(const [id,to] of [[0,'rs0'],[2048,'rs1']]){
 const cfg=m.getDB('config'),meta=cfg.collections.findOne({_id:ns});
 const chunk=cfg.chunks.findOne({uuid:meta.uuid,$expr:{$and:[{$lte:['$min._id',id]},{$gt:['$max._id',id]}]}});
 assert(chunk);if(chunk.shard!==to)assert.commandWorked(admin.runCommand({moveChunk:ns,find:{_id:id},to,_waitForDelete:true,maxTimeMS:60000}));
}
assert.commandWorked(admin.runCommand({shardCollection:'appdb.'+prefix+'_ledger',key:{_id:'hashed'}}));
print('MONGODB_STABILITY_SHARDED_LAYOUT_PASS');""", 'MONGODB_STABILITY_SHARDED_LAYOUT_PASS')
                self.scenario.js("const documents=Array.from({length:4096},(_,i)=>({_id:i,payload:'x'.repeat(1024)}));assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents,writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_STABILITY_SEED_PASS');", 'MONGODB_STABILITY_SEED_PASS', app=True)
                performance = self.scenario_module.Performance(self.scenario)
                def inject():
                    started = time.monotonic()
                    for offset, fault in zip((0, 30, 60) if focused else (300, 600, 900), ('member', 'operator', 'node')):
                        while time.monotonic() - started < offset:
                            if self.scenario.worker.poll() is not None:raise ValueError('MongoDB stability workload exited before its fault')
                            print('MONGODB_STABILITY_HEARTBEAT topology=' + topology + ' next=' + fault, flush=True);time.sleep(5)
                        if self.scenario.worker.poll() is not None:raise ValueError('MongoDB stability workload exited before its fault')
                        if fault == 'node':
                            performance.restart.start();performance.restart.recover();self.stability_health()
                        else:self.stability_restart(fault)
                result = performance.phase({'phase': 'stability-' + topology, 'concurrency': 4,
                    'seconds': seconds, 'fault': True, 'sharded': topology == 'sharded',
                    'memory_profile': True, 'shared_ca': True}, inject=inject)
                self.stability_health()
                self.scenario.js("const rows=d.getCollection(prefix+'_documents').find({}).sort({_id:1}).toArray();assert.eq(4096,rows.length);for(let i=0;i<rows.length;i++){assert.eq(i,rows[i]._id);assert.eq('x'.repeat(1024),rows[i].payload);}print('MONGODB_STABILITY_FINAL_DATA_PASS');", 'MONGODB_STABILITY_FINAL_DATA_PASS', app=True)
                results.append(result)
            except BaseException as exc:
                first_error = exc
                try:self.diagnostics()
                except (ValueError, OSError, subprocess.SubprocessError):pass
                raise
            finally:
                try:
                    self.stability_cleanup(interrupted=first_error is not None)
                    self.cleanup_target()
                except BaseException:
                    print('MONGODB_STABILITY_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                    if first_error is None:raise
        if self.verify_objects(False) != self.state['backup']:
            raise ValueError('MongoDB stability original backup changed after workloads')
        self.scenario.health();self.state['stability_result'] = results;self.save()
        print('MONGODB_STABILITY_PROFILE ' + json.dumps(results, sort_keys=True), flush=True)
        print('MONGODB_STABILITY_SCOPED_CLEAN_PASS two-topology-data-clients-faults-target=absent source-backup=retained', flush=True)
        print('MONGODB_STABILITY_FAULTS_PASS' if focused else 'MONGODB_STABILITY_PASS', flush=True)

    def sharded_client_create(self):
        self.scenario.client_create(self.state['target']['name'], sharded=True)
        self.state['target']['client_uid'] = self.scenario.client_uid;self.save()

    def sharded_client_delete(self):
        self.scenario.delete_client()
        self.state['target']['client_uid'] = None;self.save()

    def sharding_restore(self):
        """Coordinated logical PITR on a disposable two-shard product cluster."""
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (self.kind != 'logical' or 'sharding_restore' in self.state or 'target' in self.state or
                'original_config' in self.state or list(self.lab.directory.glob('*fault*.json'))):
            raise ValueError('MongoDB sharded recovery requires a clean accepted logical backup fixture')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(False) != self.state['backup']:
            raise ValueError('MongoDB sharded recovery original logical backup changed')
        prefix = 'sharding/' + self.state['run_id']
        location = 'gate/' + self.state['bucket'] + '/' + prefix + '/'
        if any(r.get('type') == 'file' for r in json_messages(self.mc('ls', '--recursive', '--json', location))):
            raise ValueError('MongoDB sharded recovery refuses an existing prefix')
        self.state['sharding_restore'] = {'prefix': prefix};self.save()
        first_error = None
        try:
            spec = importlib.util.spec_from_file_location('mongodb_restore_sharding', self.base / 'tests/helpers/mongodb-sharding.py')
            sharding = importlib.util.module_from_spec(spec);spec.loader.exec_module(sharding)
            self.prepare_target(fresh_credentials=True, backup_prefix=prefix, sharded=True)
            target = self.state['target'];q = self.state['sharding_restore']
            path = self.base / 'clusters' / target['deployment'] / 'config.yml'
            config = yaml.safe_load(path.read_text())
            config.update(mongodb_pitr_enabled=True, mongodb_pitr_oplog_span_min=1)
            path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600);self.target_setup()
            self.sharded_client_create()
            self.scenario.js(sharding.SHARD_LAYOUT, 'MONGODB_SHARDING_LAYOUT_PASS')
            self.scenario.js(sharding.SHARD_DATA, 'MONGODB_SHARDING_CRUD_TRANSACTION_PASS')
            self.scenario.js(sharding.SHARD_MIGRATE, 'MONGODB_SHARDING_POPULATED_MIGRATION_PASS')
            self.scenario.js(sharding.SHARD_VERIFY, 'MONGODB_SHARDING_POST_MIGRATION_PASS')
            self.sharded_client_delete()
            request = {'kind': 'backup', 'name': target['name'] + '-' + self.state['run_id'], 'type': 'logical'}
            self.target_request(request)
            q['backup'] = self.verify_objects(False, request_name=request['name'], prefix=prefix,
                                               expected_replsets=['cfg', 'rs0', 'rs1'])
            self.save()
            self.sharded_client_create()
            boundary = self.scenario.js("""const d=c.getDB(prefix+'_sharded'),wc={w:'majority',j:true,wtimeout:10000};
for(const tenant of [-1000,1000])assert.commandWorked(d.runCommand({insert:'documents',
 documents:[{_id:'before-'+tenant,tenant,value:'include'}],writeConcern:wc}));
const clock=c.getDB('admin'),beforeTime=assert.commandWorked(clock.runCommand({hello:1})).localTime;
const target=Math.floor(beforeTime.getTime()/1000)+2;
while(assert.commandWorked(clock.runCommand({hello:1})).localTime.getTime()<(target+1)*1000){sleep(250);}
for(const tenant of [-1001,1001])assert.commandWorked(d.runCommand({insert:'documents',
 documents:[{_id:'after-'+tenant,tenant,value:'exclude'}],writeConcern:wc}));
print('MONGODB_SHARDING_PITR_BOUNDARY '+JSON.stringify({target_utc:new Date(target*1000).toISOString().slice(0,19).replace('T',' '),target_epoch:target}));
print('MONGODB_SHARDING_PITR_BOUNDARY_PASS');""", 'MONGODB_SHARDING_PITR_BOUNDARY_PASS')
            rows = [json.loads(r.split(' ', 1)[1]) for r in boundary.splitlines()
                    if r.startswith('MONGODB_SHARDING_PITR_BOUNDARY ')]
            if len(rows) != 1:raise ValueError('MongoDB sharded PITR lacks one server-clock boundary')
            q['boundary'] = rows[0]
            moment = datetime.strptime(q['boundary']['target_utc'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if moment.timestamp() != q['boundary']['target_epoch']:
                raise ValueError('MongoDB sharded PITR timestamp identity differs')
            self.save()
            # Deliberately change post-cutoff catalog placement as well as data.
            self.scenario.js(sharding.SHARD_ADMIN + """
for(const [tenant,to] of [[-1,'rs0'],[1,'rs1']])assert.commandWorked(admin.runCommand({moveChunk:ns,find:{tenant},to,_waitForDelete:true,maxTimeMS:60000}));
const metadata=manager.getDB('config').collections.findOne({_id:ns});
assert.eq(['rs0','rs1'],manager.getDB('config').chunks.find({uuid:metadata.uuid}).sort({'min.tenant':1}).toArray().map(r=>r.shard));
print('MONGODB_SHARDING_POST_CUTOFF_CATALOG_PASS');""", 'MONGODB_SHARDING_POST_CUTOFF_CATALOG_PASS')
            self.sharded_client_delete()
            deadline = time.monotonic() + 480
            while True:
                current = self.lab.get('psmdb-backup', request['name'], self.scenario.ns)
                latest = current.get('status', {}).get('latestRestorableTime')
                if latest and datetime.fromisoformat(latest.replace('Z', '+00:00')).timestamp() > moment.timestamp()+1:break
                if time.monotonic() >= deadline:raise ValueError('MongoDB sharded PITR global timeline did not cover the cutoff')
                print('MONGODB_SHARDING_RESTORE_HEARTBEAT stage=global-oplog-coverage', flush=True);time.sleep(5)
            listing = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
            oplogs = [r for r in listing if r.get('type') == 'file' and str(r.get('key', '')).startswith(prefix + '/pbmPitr/')]
            if ({r['key'].removeprefix(prefix + '/pbmPitr/').split('/')[0] for r in oplogs} != {'cfg', 'rs0', 'rs1'} or
                    any(not r.get('size') or '..' in r['key'].split('/') for r in oplogs)):
                raise ValueError('MongoDB sharded PITR requires nonzero oplog objects for cfg and both shards')
            q['oplog_objects'] = []
            for row in oplogs:
                digest = self.lab.run('-n', self.scenario.ns, 'exec', self.state['name'] + '-client', '--',
                    'bash', '-o', 'pipefail', '-ec', 'mc --config-dir /tmp/mc cat "$1" | sha256sum',
                    'sharding-pitr-checksum', 'gate/' + self.state['bucket'] + '/' + row['key'], timeout=180).split()[0]
                if not re.fullmatch('[0-9a-f]{64}', digest):raise ValueError('MongoDB sharded oplog checksum is invalid')
                q['oplog_objects'].append({'key': row['key'], 'size': row['size'], 'sha256': digest})
            q['latest_restorable_time'] = latest;self.save()
            print('MONGODB_SHARDING_OPLOG_PASS replica-sets=cfg,rs0,rs1 checksums=sha256 global-target=covered', flush=True)
            config = yaml.safe_load(path.read_text());config.update(mongodb_pitr_enabled=False, mongodb_requests=[])
            path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600);self.target_setup()
            restore = {'kind': 'restore', 'name': target['name'] + '-' + self.state['run_id'],
                'cluster_uid': target['cr_uid'], 'destination': q['backup']['destination'],
                'type': 'logical', 'target_utc': q['boundary']['target_utc']}
            with self.trace_restore():self.target_request(restore);self.target_health()
            self.sharded_client_create()
            self.scenario.js("""const d=c.getDB(prefix+'_sharded'),col=d.getCollection('documents');
assert.eq(201,col.countDocuments({}));
for(const tenant of [-1000,1000])assert.eq('include',col.findOne({_id:'before-'+tenant}).value);
for(const tenant of [-1001,1001])assert.eq(null,col.findOne({_id:'after-'+tenant}));
assert.commandWorked(d.runCommand({delete:'documents',deletes:[{q:{_id:{$type:'string'}},limit:0}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));
print('MONGODB_SHARDING_RESTORED_BOUNDARY_PASS both-shards-before=included both-shards-after=excluded');""",
                'MONGODB_SHARDING_RESTORED_BOUNDARY_PASS both-shards-before=included both-shards-after=excluded')
            self.scenario.js(sharding.SHARD_ADMIN + """
const config=manager.getDB('config'),metadata=config.collections.findOne({_id:ns});
assert(metadata&&!metadata.dropped);assert.eq({tenant:1},metadata.key);assert.eq(true,metadata.unique);
const chunks=config.chunks.find({uuid:metadata.uuid}).sort({'min.tenant':1}).toArray();
assert.eq(2,chunks.length);assert.eq(['rs1','rs0'],chunks.map(r=>r.shard));
assert.eq({tenant:0},chunks[0].max);assert.eq(chunks[0].max,chunks[1].min);
const targetName=""" + json.dumps(target['name']) + ';\n' + SHARD_UUID_VERIFY +
                "print('MONGODB_SHARDING_RESTORED_CATALOG_PASS chunk-ranges=true placement=true per-shard-uuid=true');",
                'MONGODB_SHARDING_RESTORED_CATALOG_PASS chunk-ranges=true placement=true per-shard-uuid=true')
            self.scenario.js(sharding.SHARD_VERIFY, 'MONGODB_SHARDING_POST_MIGRATION_PASS')
            self.scenario.js(sharding.SHARD_CLEAN, 'MONGODB_SHARDING_DATABASE_CLEAN_PASS')
            self.sharded_client_delete()
            if self.verify_objects(False, request_name=request['name'], prefix=prefix,
                                   expected_replsets=['cfg', 'rs0', 'rs1']) != q['backup']:
                raise ValueError('MongoDB sharded recovery changed its logical backup payload')
            self.state['sharding_restore_result'] = q.copy();self.save()
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.cleanup_target();self.cleanup_sharding_restore()
            except BaseException:
                print('MONGODB_SHARDING_RESTORE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if self.verify_objects(False) != self.state['backup']:
            raise ValueError('MongoDB sharded recovery changed the original logical backup')
        self.scenario.health()
        print('MONGODB_SHARDING_RESTORE_SCOPED_CLEAN_PASS target-volumes-oplog-prefix=absent source=healthy', flush=True)
        print('MONGODB_SHARDING_RESTORE_PASS', flush=True)

    def disaster(self):
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (self.kind != 'physical' or 'disaster' in self.state or 'target' in self.state or
                'original_config' in self.state or list(self.lab.directory.glob('*fault*.json'))):
            raise ValueError('MongoDB disaster requires a clean accepted physical backup fixture')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB disaster original physical backup changed')
        prefix = 'disaster/' + self.state['run_id']
        escrow_path = self.lab.directory / ('mongodb-disaster-escrow-' + self.state['run_id'] + '.json')
        if escrow_path.exists() or escrow_path.is_symlink():
            raise ValueError('MongoDB disaster refuses an existing escrow file')
        if any(r.get('type') == 'file' for r in json_messages(self.mc('ls', '--recursive', '--json',
                'gate/' + self.state['bucket'] + '/' + prefix + '/'))):
            raise ValueError('MongoDB disaster refuses an existing recovery prefix')
        self.state['disaster'] = {'prefix': prefix};self.save()
        first_error = None
        try:
            self.prepare_target(fresh_credentials=True, backup_prefix=prefix)
            target = self.state['target'];name = target['name']
            path = self.base / 'clusters' / target['deployment'] / 'config.yml'
            config = yaml.safe_load(path.read_text())
            config['mongodb_users'] = [{'name': 'application', 'db': 'appdb',
                'passwordSecretRef': {'name': name + '-app', 'key': 'password'},
                'roles': [{'name': 'readWrite', 'db': 'appdb'}]}]
            path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600);self.target_setup()
            self.scenario.client_create(name)
            seed = "const col=prefix+'_documents',wc={w:'majority',j:true,wtimeout:10000};assert.commandWorked(d.runCommand({delete:col,deletes:[{q:{},limit:0}],writeConcern:wc}));assert.commandWorked(d.runCommand({insert:col,documents:[{_id:'dr-1',value:NumberDecimal('19.95'),payload:'off-source'},{_id:'dr-2',value:NumberLong('9007199254740993'),payload:'escrow'}],writeConcern:wc}));assert.commandWorked(d.runCommand({createIndexes:col,indexes:[{key:{payload:1},name:'disaster_payload',unique:true}],writeConcern:wc}));print('MONGODB_DISASTER_SEED_PASS');"
            self.scenario.js(seed, 'MONGODB_DISASTER_SEED_PASS', app=True);self.scenario.delete_client()
            # The credential escrow survives deletion of every source Secret.
            escrow = {suffix: self.lab.get('secret', name + '-' + suffix, self.scenario.ns)['data']
                      for suffix in ('users', 'app', 'key', 'tls', 'tls-internal')}
            self.scenario.fixture.write_private(escrow_path, escrow)
            self.state['disaster']['escrow_sha256'] = hashlib.sha256(escrow_path.read_bytes()).hexdigest()
            self.save()
            request = {'kind': 'backup', 'name': name + '-' + self.state['run_id'], 'type': 'physical'}
            current = self.target_request(request)
            if set(current['metadata'].get('finalizers', [])) - {'internal.percona.com/release-lock'}:
                raise ValueError('MongoDB disaster backup unexpectedly deletes remote data on CR removal')
            self.state['disaster']['backup'] = self.verify_objects(False, request_name=request['name'], prefix=prefix)
            self.save()
            self.delete_request(current)
            self.capture_target()
            self.state['disaster']['lost'] = {'cr_uid': target['cr_uid'], 'resources': target['resources']}
            self.save();self.cleanup_target();self.disaster_absent();self.disaster_objects()
            print('MONGODB_DISASTER_SOURCE_LOSS_PASS cr-pods-secrets-pvc-pv=absent backup-cr=absent', flush=True)
            # Read only the escrowed bytes after the source graph and data have
            # disappeared. No source Secret or old claim is used for recovery.
            if (escrow_path.is_symlink() or escrow_path.stat().st_mode & 0o777 != 0o600 or
                    hashlib.sha256(escrow_path.read_bytes()).hexdigest() != self.state['disaster']['escrow_sha256']):
                raise ValueError('MongoDB disaster escrow changed after source loss')
            recovered = json.loads(escrow_path.read_text())
            self.prepare_target(escrow=recovered, backup_prefix=prefix)
            target = self.state['target'];lost = self.state['disaster']['lost']
            old_uids = {r['uid'] for r in lost['resources']}
            old_volumes = {r['volume_name'] for r in lost['resources'] if r['kind'] == 'pvc'}
            if (target['cr_uid'] == lost['cr_uid'] or
                    any(r['uid'] in old_uids or r.get('volume_name') in old_volumes for r in target['resources'])):
                raise ValueError('MongoDB disaster target reused a lost source identity or volume')
            request = {'kind': 'restore', 'name': target['name'] + '-' + self.state['run_id'],
                'cluster_uid': target['cr_uid'], 'destination': self.state['disaster']['backup']['destination'],
                'type': 'physical'}
            with self.trace_restore():self.target_request(request);self.target_health()
            self.scenario.client_create(target['name'])
            check = "const col=d.getCollection(prefix+'_documents');assert.eq(2,col.countDocuments({}));assert.eq('19.95',col.findOne({_id:'dr-1'}).value.toString());assert.eq('9007199254740993',col.findOne({_id:'dr-2'}).value.toString());assert.eq(null,col.findOne({_id:'after-backup'}));const indexes=col.getIndexes();assert.eq(1,indexes.filter(i=>i.name==='disaster_payload'&&i.unique===true&&i.key.payload===1&&Object.keys(i.key).length===1).length);print('MONGODB_DISASTER_RECOVERED_DATA_PASS decimal128=true int64=true unique-index=true user=true target-only-marker=absent');"
            self.scenario.js(check, 'MONGODB_DISASTER_RECOVERED_DATA_PASS decimal128=true int64=true unique-index=true user=true target-only-marker=absent', app=True)
            self.scenario.delete_client();self.disaster_objects()
            self.state['disaster_result'] = {'source_uid': lost['cr_uid'], 'target_uid': target['cr_uid'],
                'backup': self.state['disaster']['backup'], 'lost_resources': lost['resources'],
                'recovery_material': 'off-source-object-storage-and-private-escrow',
                'scope': 'database-cluster-loss; Kubernetes/control/object-store remain available'}
            self.save()
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.cleanup_target();self.cleanup_disaster()
            except BaseException:
                print('MONGODB_DISASTER_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB disaster changed the accepted physical backup')
        self.scenario.health()
        print('MONGODB_DISASTER_RECOVERY_SCOPED_CLEAN_PASS disposable-clusters-volumes-escrow-prefix=absent source=healthy', flush=True)
        print('MONGODB_DISASTER_RECOVERY_PASS', flush=True)

    def cleanup_probe(self, outcome):
        """Child process: exercise the real product graph and its exit cleanup."""
        path = self.lab.directory / 'mongodb-cleanup-qualification.json'
        qualification = json.loads(path.read_text())
        token = sys.stdin.readline().strip()
        if (path.is_symlink() or token != qualification['run_id'] or
                qualification.get('child_pid') != os.getpid() or qualification.get('active') != outcome):
            raise ValueError('MongoDB cleanup child lacks its parent process receipt')
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if qualification['backup_run_id'] != self.state['run_id']:
            raise ValueError('MongoDB cleanup child backup identity changed')
        first_error = None
        try:
            self.prepare_target()
            self.scenario.client_create(self.state['target']['name'])
            qualification['client_uid'] = self.scenario.client_uid
            self.scenario.fixture.write_private(path, qualification)
            self.scenario.js("const d=c.getDB('appdb');assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'cleanup-seed',value:prefix}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.eq(prefix,d.getCollection(prefix+'_documents').findOne({_id:'cleanup-seed'}).value);print('MONGODB_CLEANUP_PROBE_BUSINESS_PASS');",
                             'MONGODB_CLEANUP_PROBE_BUSINESS_PASS')
            print('MONGODB_CLEANUP_PROBE_READY', flush=True)
            if outcome == 'failure':
                raise ValueError('intentional MongoDB cleanup qualification failure')
            if outcome == 'interruption':
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    print('MONGODB_CLEANUP_PROBE_HEARTBEAT awaiting-parent-signal=true', flush=True)
                    time.sleep(3)
                raise ValueError('MongoDB cleanup child did not receive its parent signal')
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            try:
                self.cleanup_target()
                print('MONGODB_CLEANUP_PROBE_SCOPED_CLEAN_PASS', flush=True)
            except BaseException:
                print('MONGODB_CLEANUP_PROBE_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise

    def cleanup_qualification_finish(self):
        path = self.lab.directory / 'mongodb-cleanup-qualification.json'
        q = json.loads(path.read_text())
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (path.is_symlink() or not re.fullmatch('[0-9a-f]{16}', q['run_id']) or
                q['backup_run_id'] != self.state['run_id'] or
                q['name'] != 'mongodb-cleanup-independent-' + q['run_id']):
            raise ValueError('MongoDB cleanup qualification journal has foreign identity')
        self.scenario.prefix = self.state['source_prefix']
        self.scenario.client_uid = q.get('client_uid')
        self.cleanup_target()
        if list(self.lab.directory.glob('*fault*.json')) or self.lab.get('pod', self.scenario.client, self.scenario.ns):
            raise ValueError('MongoDB cleanup qualification retains a fault or business client')
        if self.source_identity() != q['source'] or {
                n['metadata']['name']: {'uid': n['metadata']['uid'], 'spec': n['spec']}
                for n in self.lab.get('nodes')['items']} != q['nodes']:
            raise ValueError('MongoDB cleanup qualification changed source or node scheduling')
        current = self.lab.get('configmap', q['name'], self.scenario.ns)
        if current != q['foreign']:
            raise ValueError('MongoDB cleanup changed the independently owned resource')
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB cleanup qualification changed its retained backup')
        self.lab.delete_uid('/api/v1/namespaces/' + self.scenario.ns + '/configmaps/' + q['name'], q['foreign']['metadata']['uid'])
        if self.lab.get('configmap', q['name'], self.scenario.ns):
            raise ValueError('MongoDB cleanup qualification independent fixture remains')
        self.scenario.health();path.unlink()
        print('MONGODB_CLEANUP_QUALIFICATION_SCOPED_CLEAN_PASS clients-targets-volumes-faults=absent foreign=unchanged nodes=unchanged', flush=True)

    def cleanup_qualification(self, *, only=None):
        if only not in (None, 'success', 'failure'):
            raise ValueError('MongoDB cleanup qualification has an unsupported focused outcome')
        path = self.lab.directory / 'mongodb-cleanup-qualification.json'
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (path.exists() or 'target' in self.state or list(self.lab.directory.glob('*fault*.json')) or
                self.lab.get('pod', self.scenario.client, self.scenario.ns)):
            raise ValueError('MongoDB cleanup qualification requires a clean owned scope')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB cleanup qualification backup is not intact')
        run_id = secrets.token_hex(8);name = 'mongodb-cleanup-independent-' + run_id
        if self.lab.get('configmap', name, self.scenario.ns):
            raise ValueError('MongoDB cleanup independent fixture already exists')
        q = {'run_id': run_id, 'name': name, 'backup_run_id': self.state['run_id'],
             'source': self.source_identity(), 'nodes': {
                 n['metadata']['name']: {'uid': n['metadata']['uid'], 'spec': n['spec']}
                 for n in self.lab.get('nodes')['items']}, 'foreign': None, 'active': None, 'child_pid': None}
        self.scenario.fixture.write_private(path, q)
        obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': self.scenario.ns,
               'labels': {'app.kubernetes.io/managed-by': 'independent-resource-owner', 'kubeauto.io/test-run': run_id}},
               'data': {'receipt': run_id}}
        q['foreign'] = json.loads(self.api_input('create', '-f', '-', '-o', 'json', obj=obj))
        self.scenario.fixture.write_private(path, q)
        child = None;first_error = None
        directory = Path(tempfile.mkdtemp(prefix='kubeauto-mongodb-install.cleanup-'))
        try:
            for outcome in ((only,) if only else ('success', 'failure', 'interruption')):
                # PV deletion and the CSI capacity publisher reconcile separately.
                # Wait for the next child's prerequisite before it can mutate.
                self.target_capacity(wait_timeout=180)
                q.update(active=outcome, child_pid=None, client_uid=None)
                self.scenario.fixture.write_private(path, q)
                log = directory / (outcome + '.log')
                with log.open('wb') as stream:
                    log.chmod(0o600)
                    child = subprocess.Popen([sys.executable, str(self.base / 'tests/helpers/mongodb-backups.py'),
                        'cleanup-probe', '--base', str(self.base), '--outcome', outcome],
                        stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT)
                    q['child_pid'] = child.pid;self.scenario.fixture.write_private(path, q)
                    child.stdin.write((run_id + '\n').encode());child.stdin.close()
                    deadline = time.monotonic() + 1800;signalled = False
                    while child.poll() is None:
                        if outcome == 'interruption' and not signalled and 'MONGODB_CLEANUP_PROBE_READY' in log.read_text().splitlines():
                            child.terminate();signalled = True
                        if time.monotonic() >= deadline:
                            raise ValueError('MongoDB cleanup qualification child deadline exceeded')
                        print('MONGODB_CLEANUP_HEARTBEAT outcome=' + outcome + ' child=running', flush=True)
                        time.sleep(10)
                    try:cleanup_probe_result(log.read_text(), outcome, child.returncode, signalled)
                    except ValueError:
                        raise ValueError('MongoDB cleanup qualification child outcome or cleanup proof differs; diagnostics=' + str(directory)) from None
                self.state = json.loads(self.path.read_text())
                if 'target' in self.state or list(self.lab.directory.glob('*fault*.json')):
                    raise ValueError('MongoDB cleanup qualification child retained its target or fault')
                if self.lab.get('configmap', name, self.scenario.ns) != q['foreign']:
                    raise ValueError('MongoDB cleanup qualification child changed the independent resource')
                print('MONGODB_CLEANUP_OUTCOME_PASS outcome=' + outcome + ' child-rc=' + str(child.returncode) + ' owned-pv=absent foreign=unchanged', flush=True)
                child = None
        except BaseException as exc:
            first_error = exc
            raise
        finally:
            if child and child.poll() is None:
                child.terminate()
                deadline = time.monotonic() + 900
                while child.poll() is None and time.monotonic() < deadline:
                    print('MONGODB_CLEANUP_HEARTBEAT outcome=parent-exit awaiting-child-cleanup=true', flush=True)
                    time.sleep(10)
            try:
                if child and child.poll() is None:
                    raise ValueError('MongoDB cleanup child still owns its target; journal retained')
                self.cleanup_qualification_finish()
            except BaseException:
                print('MONGODB_CLEANUP_QUALIFICATION_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        shutil.rmtree(directory)
        print('MONGODB_CLEANUP_' + only.upper() + '_PASS' if only else 'MONGODB_CLEANUP_PASS', flush=True)

    def expected_target_failure(self, phase):
        """Keep expected rejection output private and out of the failure-marker stream."""
        target = self.state['target']
        result = subprocess.run(['kubecli', 'setup', target['deployment'], '07', '--tags', 'mongodb'],
            cwd=self.base, capture_output=True, text=True, timeout=5400)
        self.capture_target()
        output = result.stdout + result.stderr
        target[phase + '_rejection'] = {'rc': result.returncode,
            'output_sha256': hashlib.sha256(output.encode()).hexdigest()}
        self.state.setdefault('encryption_rejections', {})[phase] = dict(target[phase + '_rejection'])
        self.save()
        if result.returncode == 0:
            raise ValueError('MongoDB target negative control unexpectedly succeeded: ' + phase)
        return output

    def refusal_config(self, settings, request=None, *, expected=False):
        target = self.state['target'];state = target['refusal']
        path = self.base / 'clusters' / target['deployment'] / 'config.yml'
        current = yaml.safe_load(path.read_text())
        if current not in (state['original_config'], state.get('applied_config')):
            raise ValueError('MongoDB refusal refuses concurrent target configuration changes')
        config = state['original_config'] | settings | {'mongodb_requests': [request] if request else []}
        retired = []
        if config.get('mongodb_network_policy_enabled') is False:
            retired = [r for r in target['resources'] if r['kind'] == 'networkpolicy' and
                       r['name'] == target['name'] + '-access']
            if len(retired) > 1:raise ValueError('MongoDB refusal has duplicate product policy identities')
            for record in retired:
                policy = self.lab.get('networkpolicy', record['name'], self.scenario.ns)
                if (not policy or policy['metadata']['uid'] != record['uid'] or
                        not target_resource_owned(policy, target['name'], self.state['run_id'])):
                    raise ValueError('MongoDB refusal product policy identity changed before disabling it')
        if request:
            desired = self.scenario.product.operation_object(config, request)
            target.setdefault('refusal_requests', {})[request['name']] = {'request': request, 'desired': desired}
        state['applied_config'] = config;self.save()
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        if expected:
            state['started_at'] = datetime.now(timezone.utc).isoformat();self.save()
            return self.expected_target_failure('refusal_' + state['phase'])
        self.target_setup()
        # Disabling the product rule deliberately deletes it. Retire its exact
        # old UID only after absence is proven, so later product re-enablement
        # can journal the newly created policy instead of rejecting its new UID.
        for record in retired:
            if self.lab.get('networkpolicy', record['name'], self.scenario.ns):
                raise ValueError('MongoDB refusal product policy remains after disabling it')
        if retired:
            target['resources'] = [r for r in target['resources'] if r not in retired];self.save()

    def refusal_preflight(self, phase, request):
        """Operator 1.23.1 keeps the CR initializing when a PBM storage probe fails.

        The product must stop before request creation. A fresh, target/member-bound
        native agent error is mandatory; a readiness timeout alone proves nothing.
        """
        target = self.state['target'];state = target['refusal'];config = state['applied_config']
        cr = self.lab.get('psmdb', target['name'], self.scenario.ns)
        path = self.base / 'clusters' / target['deployment'] / 'config.yml'
        if (phase not in ('credentials', 'storage') or
                target.get('refusal_' + phase + '_rejection', {}).get('rc', 0) == 0 or
                yaml.safe_load(path.read_text()) != config or not cr or
                cr['metadata']['uid'] != target['cr_uid'] or not self.scenario.product.owned(cr) or
                self.lab.get('psmdb-backup', request['name'], self.scenario.ns)):
            raise ValueError('MongoDB refusal preflight lacks the owned rejected product boundary')
        backup = cr['spec'].get('backup', {})
        storage = backup.get('storages', {}).get(config['mongodb_backup_storage_name'], {})
        details = storage.get(config['mongodb_backup_storage_type'], {})
        if (backup.get('enabled') is not True or storage.get('type') != config['mongodb_backup_storage_type'] or
                details.get('insecureSkipTLSVerify') is not False or any(
                    details.get(field) != config[key] for field, key in (
                        ('credentialsSecret', 'mongodb_backup_credentials_secret'), ('prefix', 'mongodb_backup_prefix'),
                        ('bucket', 'mongodb_backup_bucket'), ('endpointUrl', 'mongodb_backup_endpoint')))):
            raise ValueError('MongoDB refusal storage configuration has not reached the target')
        started = datetime.fromisoformat(state['started_at'])
        output = self.lab.run('-n', self.scenario.ns, 'logs', 'deployment/mongodb-operator',
            '--all-containers', '--since-time=' + started.isoformat(), timeout=60)
        expected = {target['name'] + '-rs0-' + str(i) + '.' + target['name'] +
                    '-rs0.' + self.scenario.ns + '.svc.cluster.local:27017' for i in range(3)}
        for line in reversed(output.splitlines()):
            if '\tAgent is not OK\t' not in line:continue
            try:
                timestamp = datetime.fromisoformat(line.split('\t', 1)[0].replace('Z', '+00:00'))
                record = json.loads(line.rsplit('\t', 1)[1])
            except (ValueError, TypeError):continue
            if (timestamp < started or record.get('controller') != 'psmdb-controller' or
                    record.get('PerconaServerMongoDB') != {'name': target['name'], 'namespace': self.scenario.ns} or
                    record.get('node') not in expected):continue
            try:digest = refusal_cause(phase, record.get('err', ''))
            except ValueError:continue
            return {'error_sha256': digest, 'state': 'blocked-before-request', 'request_created': False,
                    'generation': cr['metadata']['generation'], 'native_observed_at': timestamp.isoformat()}
        raise ValueError('MongoDB refusal lacks a fresh target-bound native PBM agent cause')

    def refusal_fault_recover(self):
        """Validate the entire temporary fault before restoring any part of it."""
        target = self.state['target'];fault = target.get('refusal', {}).get('fault')
        if not fault:return
        if self.source_identity() != target['source_identity']:
            raise ValueError('MongoDB refusal recovery source identity changed')
        if fault['kind'] == 'service':
            obj = self.lab.get('service', self.state['name'], self.scenario.ns)
            if (not obj or not self.owned(obj) or obj['metadata']['uid'] != fault['uid'] or
                    obj['spec']['selector'] not in (fault['original'], fault['applied'])):
                raise ValueError('MongoDB refusal storage service identity changed')
            if obj['spec']['selector'] != fault['original']:
                self.api_input('-n', self.scenario.ns, 'patch', 'service', self.state['name'], '--type=json',
                    '--patch-file=/dev/stdin', obj=[
                        {'op': 'test', 'path': '/metadata/uid', 'value': fault['uid']},
                        {'op': 'test', 'path': '/spec/selector', 'value': fault['applied']},
                        {'op': 'replace', 'path': '/spec/selector', 'value': fault['original']}])
            self.mc('ready', 'gate')
        elif fault['kind'] == 'quorum':
            key = 'kubeauto.io/mongodb-quorum-fault'
            prefix = fault.get('run_id', '')
            if prefix != self.state['source_prefix'] or not re.fullmatch('mongodb_gate_[0-9a-f]{16}', prefix):
                raise ValueError('MongoDB refusal quorum fault has a foreign run identity')
            members = [self.lab.get('pod', r['name'], self.scenario.ns) for r in fault['members']]
            policy = self.lab.get('networkpolicy', fault['name'], self.scenario.ns)
            normalized = lambda spec: spec | {key: spec.get(key, []) for key in ('ingress', 'egress')}
            if (any(not obj or obj['metadata']['uid'] != row['uid'] or
                    obj['metadata'].get('labels', {}).get(key) not in (None, prefix)
                    for obj, row in zip(members, fault['members'])) or
                    (policy and (policy['metadata']['uid'] != fault.get('uid') or
                     normalized(policy['spec']) != normalized(fault['spec']) or not target_resource_owned(
                         policy, target['name'], self.state['run_id'])))):
                raise ValueError('MongoDB refusal quorum recovery ownership changed')
            for obj in members:
                if key in obj['metadata'].get('labels', {}):
                    self.api_input('-n', self.scenario.ns, 'patch', 'pod', obj['metadata']['name'], '--type=json',
                        '--patch-file=/dev/stdin', obj=[
                            {'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
                            {'op': 'test', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': prefix},
                            {'op': 'remove', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault'}])
            if policy:
                self.api_input('delete', '--raw', '/apis/networking.k8s.io/v1/namespaces/' + self.scenario.ns +
                    '/networkpolicies/' + fault['name'], '-f', '-', obj={
                        'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': fault['uid']}})
                self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', 'networkpolicy/' + fault['name'],
                             '--timeout=60s', timeout=80)
        else:raise ValueError('MongoDB refusal has an unknown fault')
        target['refusal'].pop('fault');self.save()

    def refusal_quorum(self):
        target = self.state['target'];state = target['refusal']
        # Disable the product ingress rule first. A later product execution must
        # not recreate an additive allowance over the owned deny fixture.
        self.refusal_config({'mongodb_network_policy_enabled': False})
        pods = [p for p in self.lab.get('pods', ns=self.scenario.ns)['items'] if
                p['metadata'].get('labels', {}).get('app.kubernetes.io/instance') == target['name'] and
                p['metadata'].get('labels', {}).get('app.kubernetes.io/replset') == 'rs0']
        if len(pods) != 3 or not all(self.scenario.product.ready(p) for p in pods):
            raise ValueError('MongoDB refusal quorum requires three healthy target members')
        key = 'kubeauto.io/mongodb-quorum-fault'
        if any(key in p['metadata'].get('labels', {}) for p in pods):
            raise ValueError('MongoDB refusal refuses existing target fault labels')
        obj = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': target['name'] + '-refusal-deny', 'namespace': self.scenario.ns,
                         'labels': LABELS | {'kubeauto.io/test-run': self.state['run_id']}},
            'spec': {'podSelector': {'matchLabels': {key: self.scenario.prefix}},
                     'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': []}}
        if self.lab.get('networkpolicy', obj['metadata']['name'], self.scenario.ns):
            raise ValueError('MongoDB refusal refuses an existing deny fixture')
        state['fault'] = {'kind': 'quorum', 'name': obj['metadata']['name'], 'uid': None, 'spec': obj['spec'],
            'run_id': self.scenario.prefix,
            'members': [{'name': p['metadata']['name'], 'uid': p['metadata']['uid']} for p in pods[:2]]}
        self.save()
        created = json.loads(self.api_input('create', '-f', '-', '-o', 'json', obj=obj))
        state['fault']['uid'] = created['metadata']['uid'];self.save();self.capture_target()
        for row in state['fault']['members']:
            self.api_input('-n', self.scenario.ns, 'patch', 'pod', row['name'], '--type=json',
                '--patch-file=/dev/stdin', obj=[
                    {'op': 'test', 'path': '/metadata/uid', 'value': row['uid']},
                    {'op': 'add', 'path': '/metadata/labels/kubeauto.io~1mongodb-quorum-fault', 'value': self.scenario.prefix}])
        self.scenario.restart_isolated_members(state['fault']['members'])
        deadline = time.monotonic() + 90
        script = "const a=db.getSiblingDB('admin');assert(a.auth({user:process.env.MONGODB_CLUSTER_ADMIN_USER,pwd:process.env.MONGODB_CLUSTER_ADMIN_PASSWORD}));print(JSON.stringify(a.runCommand({hello:1})));"
        while True:
            observations = []
            for row in pods:
                output = self.lab.run('-n', self.scenario.ns, 'exec', row['metadata']['name'], '-c', 'mongod', '--',
                    'mongosh', '--quiet', '--norc', '--host', 'localhost', '--tls', '--tlsCAFile',
                    '/etc/mongodb-ssl/ca.crt', '--eval', script, timeout=30)
                observations.append(json.loads(output.splitlines()[-1]))
            if all(o.get('ok') == 1 and not o.get('isWritablePrimary', False) for o in observations):break
            if time.monotonic() >= deadline:raise ValueError('MongoDB refusal target retained a writable primary')
            print('MONGODB_BACKUP_REFUSAL_HEARTBEAT phase=quorum-demotion', flush=True);time.sleep(3)
        request = {'kind': 'backup', 'name': target['name'] + '-' + self.state['run_id'] + '-quorum', 'type': 'logical'}
        self.refusal_config({'mongodb_network_policy_enabled': False, 'mongodb_ready_timeout': 60,
                             'mongodb_request_timeout': 60}, request, expected=True)
        if self.lab.get('psmdb-backup', request['name'], self.scenario.ns):
            raise ValueError('MongoDB unhealthy target reached backup request creation')
        state['results']['quorum'] = {'writable_primaries': 0, 'request_created': False};self.save()
        print('MONGODB_BACKUP_REFUSAL_QUORUM_PASS primary=absent request=absent product-rc=nonzero', flush=True)

    def refusal(self, *, focus='all'):
        if focus not in ('all', 'tail', 'oplog'):
            raise ValueError('MongoDB refusal focus is invalid')
        self.state = json.loads(self.path.read_text());self.validate_identity()
        if (self.kind != 'logical' or list(self.lab.directory.glob('*fault*.json')) or
                'original_config' in self.state or 'target' in self.state or not self.state.get('backup')):
            raise ValueError('MongoDB refusal requires a clean accepted logical backup')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB refusal original backup objects changed')
        listing = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
        if any(r.get('type') == 'file' and r.get('key', '').startswith('backup/pbmPitr/') for r in listing):
            raise ValueError('MongoDB missing-oplog control requires a baseline with no archived oplog')
        self.state['refusal_pending'] = True;self.save()
        first_error = None
        try:
            self.prepare_target()
            target = self.state['target'];path = self.base / 'clusters' / target['deployment'] / 'config.yml'
            target['refusal'] = {'original_config': yaml.safe_load(path.read_text()), 'results': {}, 'phase': 'prepare'}
            self.save()
            credential = self.lab.get('secret', self.state['name'] + '-credentials', self.scenario.ns)
            wrong = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
                'name': target['name'] + '-refusal-credentials', 'namespace': self.scenario.ns,
                'labels': LABELS | {'kubeauto.io/test-run': self.state['run_id']}},
                'type': 'Opaque', 'stringData': {'AWS_ACCESS_KEY_ID': secrets.token_hex(16),
                                               'AWS_SECRET_ACCESS_KEY': secrets.token_hex(32)}}
            if not credential or self.lab.get('secret', wrong['metadata']['name'], self.scenario.ns):
                raise ValueError('MongoDB refusal credential fixture conflicts with existing resources')
            self.api_input('create', '-f', '-', '-o', 'json', obj=wrong);self.capture_target()
            for phase in (('credentials', 'storage') if focus == 'all' else ()):
                state = target['refusal'];state['phase'] = phase;self.save()
                if phase == 'storage':
                    obj = self.lab.get('service', self.state['name'], self.scenario.ns)
                    if not self.owned(obj):raise ValueError('MongoDB refusal storage service is foreign')
                    applied = obj['spec']['selector'] | {'kubeauto.io/refusal-unavailable': self.state['run_id']}
                    state['fault'] = {'kind': 'service', 'uid': obj['metadata']['uid'],
                                      'original': obj['spec']['selector'], 'applied': applied};self.save()
                    self.api_input('-n', self.scenario.ns, 'patch', 'service', self.state['name'], '--type=json',
                        '--patch-file=/dev/stdin', obj=[
                            {'op': 'test', 'path': '/metadata/uid', 'value': obj['metadata']['uid']},
                            {'op': 'test', 'path': '/spec/selector', 'value': state['fault']['original']},
                            {'op': 'replace', 'path': '/spec/selector', 'value': applied}])
                    deadline = time.monotonic() + 45
                    while True:
                        endpoint = self.lab.get('endpoints', self.state['name'], self.scenario.ns)
                        if endpoint and not any(s.get('addresses') or s.get('notReadyAddresses') for s in endpoint.get('subsets', [])):
                            break
                        if time.monotonic() >= deadline:
                            raise ValueError('MongoDB unavailable-storage fixture still has Service backends')
                        time.sleep(2)
                request = {'kind': 'backup', 'name': target['name'] + '-' + self.state['run_id'] + '-' + phase,
                           'type': 'logical'}
                settings = {'mongodb_backup_prefix': 'refusal/' + self.state['run_id'],
                            'mongodb_ready_timeout': 90, 'mongodb_request_timeout': 120}
                if phase == 'credentials':settings['mongodb_backup_credentials_secret'] = wrong['metadata']['name']
                self.refusal_config(settings, request, expected=True)
                current = self.lab.get('psmdb-backup', request['name'], self.scenario.ns)
                if current:
                    digest = refusal_reason(phase, current.get('status', {}))
                    state['results'][phase] = {'error_sha256': digest, 'state': current['status']['state']}
                else:state['results'][phase] = self.refusal_preflight(phase, request)
                self.save()
                self.refusal_fault_recover()
                if current:self.delete_request(current)
                self.refusal_config({})
                print('MONGODB_BACKUP_REFUSAL_' + phase.upper() + '_PASS native-cause=verified product-rc=nonzero', flush=True)
            if focus != 'oplog':
                target['refusal']['phase'] = 'quorum';self.save();self.refusal_quorum()
                self.refusal_fault_recover();self.refusal_config({})
            # No source object is deleted. A baseline never configured for PITR
            # supplies the independent negative control for missing history.
            state = target['refusal'];state['phase'] = 'oplog';self.save()
            timestamp = int(self.state['backup']['metadata']['last_write_ts']['T']) + 30
            request = {'kind': 'restore', 'name': target['name'] + '-' + self.state['run_id'] + '-oplog',
                'cluster_uid': target['cr_uid'], 'destination': self.state['backup']['destination'], 'type': 'logical',
                'target_utc': datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
            self.refusal_config({'mongodb_request_timeout': 120}, request, expected=True)
            current = self.lab.get('psmdb-restore', request['name'], self.scenario.ns)
            digest = refusal_reason('oplog', (current or {}).get('status', {}))
            if current['status'].get('pbmName'):
                raise ValueError('MongoDB missing oplog control unexpectedly dispatched a restore')
            state['results']['oplog'] = {'error_sha256': digest, 'restore_dispatched': False};self.save()
            self.delete_request(current);self.refusal_config({});self.target_health()
            self.scenario.client_create(target['name'])
            self.scenario.js(REFUSAL_TARGET_DATA, 'MONGODB_BACKUP_REFUSAL_TARGET_DATA_PASS original-marker=unchanged')
            self.scenario.delete_client()
            self.state['refusal_results'] = dict(state['results']);self.save()
            print('MONGODB_BACKUP_REFUSAL_OPLOG_PASS native-cause=verified target-data=unchanged', flush=True)
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:
                if 'target' in self.state:self.refusal_fault_recover()
                self.cleanup_target()
                self.refusal_prefix_cleanup()
            except BaseException:
                print('MONGODB_BACKUP_REFUSAL_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        required = {'all': {'credentials', 'storage', 'quorum', 'oplog'}, 'tail': {'quorum', 'oplog'}, 'oplog': {'oplog'}}[focus]
        if set(self.state.get('refusal_results', {})) != required:
            raise ValueError('MongoDB refusal did not prove every required negative control')
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB refusal changed the retained source backup')
        self.scenario.health()
        print('MONGODB_BACKUP_REFUSAL_SCOPED_CLEAN_PASS target-faults-prefix=absent source-backup=unchanged', flush=True)
        print({'all': 'MONGODB_BACKUP_REFUSAL_PASS', 'tail': 'MONGODB_BACKUP_REFUSAL_TAIL_PASS',
               'oplog': 'MONGODB_BACKUP_REFUSAL_OPLOG_ONLY_PASS'}[focus], flush=True)

    def refusal_prefix_cleanup(self):
        if not self.state.get('refusal_pending'):return
        self.validate_identity()
        if 'target' in self.state:
            raise ValueError('MongoDB refusal prefix cleanup requires its target to be absent')
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB refusal prefix cleanup original backup changed')
        prefix = 'refusal/' + self.state['run_id'] + '/'
        self.mc('rm', '--recursive', '--force', 'gate/' + self.state['bucket'] + '/' + prefix)
        if json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/' + prefix)):
            raise ValueError('MongoDB refusal has surviving failed backup objects')
        self.state.pop('refusal_pending');self.save()

    def retention_records(self, *, verify=False):
        state = self.state['retention']
        if self.source_identity() != state['source_identity']:
            raise ValueError('MongoDB retention source identity changed')
        current = []
        for obj in self.lab.get('psmdb-backup', ns=self.scenario.ns)['items']:
            meta = obj['metadata'];labels = meta.get('labels', {})
            if (labels.get('percona.com/cluster') != self.scenario.fixture.NAME or
                    labels.get('percona.com/backup-ancestor') != 'daily-full'):
                continue
            previous = state['records'].get(meta['name'])
            candidate = obj
            if meta.get('deletionTimestamp') and 'percona.com/delete-backup' not in meta.get('finalizers', []):
                if not previous:continue  # Already removed by the scheduler; never adopt it for deletion.
                candidate = obj | {'metadata': meta | {'finalizers': ['percona.com/delete-backup']}}
            record = scheduled_backup_record(candidate, self.scenario.fixture.NAME,
                self.state['desired_request']['spec']['storageName'], state['started'])
            if previous and any(previous[key] != value for key, value in record.items()):
                raise ValueError('MongoDB retention scheduled request identity changed')
            if not previous:
                previous = state['records'][meta['name']] = record
            previous['state'] = obj.get('status', {}).get('state')
            self.save()
            if verify and previous['state'] == 'ready' and not meta.get('deletionTimestamp') and 'backup' not in previous:
                previous['backup'] = self.verify_objects(persist=False, request_name=meta['name'])
                self.save()
            current.append(obj)
        return current

    def retention_pause(self, *, cancel_pending=False):
        if 'original_config' not in self.state:
            return
        state, applied = self.state['retention'], self.state['applied_config']
        state.setdefault('pause_from', dict(applied))
        applied['mongodb_backup_schedule_enabled'] = False
        self.save()  # Both sides of this one-bit transition remain recoverable.
        config = yaml.safe_load(self.config_path.read_text())
        if {key: config.get(key) for key in applied} not in (applied, state['pause_from'], self.state['original_config']):
            raise ValueError('MongoDB retention pause refuses concurrent configuration edits')
        config.update(applied)
        self.config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        if cancel_pending:
            # Product owns the schedule transition. Cancellation is a runner fixture action,
            # performed only AFTER the controller sees that this task is disabled.
            process = subprocess.Popen(['kubecli', 'setup', self.scenario.ns, '07', '--tags', 'mongodb'],
                                       cwd=self.base, start_new_session=True)
            try:
                deadline = time.monotonic() + 180
                while True:
                    source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
                    if source['metadata']['uid'] != self.state['source_cluster_uid']:
                        raise ValueError('MongoDB retention cancellation source identity changed')
                    tasks = source['spec']['backup'].get('tasks', [])
                    if all(not task.get('enabled') for task in tasks):break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise ValueError('MongoDB retention product did not disable the schedule before cancellation')
                    print('MONGODB_RETENTION_HEARTBEAT stage=wait-product-schedule-stop', flush=True)
                    time.sleep(2)
                current = self.retention_records()
                for obj in current:
                    if obj.get('status', {}).get('state') not in ('ready', 'error', 'rejected'):
                        self.delete_request(obj)
                if process.wait(timeout=5400):
                    raise ValueError('MongoDB retention schedule recovery product command failed')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:process.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL);process.wait(timeout=10)
        else:self.setup_product()
        deadline = time.monotonic() + 600
        while any(o.get('status', {}).get('state') not in ('ready', 'error', 'rejected')
                  for o in self.retention_records()):
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB retention pending backup did not settle after schedule stop')
            print('MONGODB_RETENTION_HEARTBEAT stage=pending-backup-settle', flush=True)
            time.sleep(5)

    def retention_expired(self, backup):
        pbm = backup['metadata']['name']
        rows = json_messages(self.mc('ls', '--recursive', '--json', 'gate/' + self.state['bucket'] + '/'))
        return not any(row.get('type') == 'file' and (row['key'] == 'backup/' + pbm + '.pbm.json' or
            row['key'].startswith('backup/' + pbm + '/')) for row in rows)

    def retention_fault(self, *, clear=False):
        path = self.lab.directory / 'mongodb-fault-retention.json'
        expected = {'cluster_uid': self.state['cluster_uid'], 'run_id': self.state['run_id'],
                    'backup_journal': self.path.name, 'source_uid': self.state['source_cluster_uid']}
        if path.exists() and json.loads(path.read_text()) != expected:
            raise ValueError('MongoDB retention refuses a foreign recovery journal')
        if clear:
            if path.exists():path.unlink()
        else:self.scenario.fixture.write_private(path, expected)

    def retention_recover(self):
        if not self.state or 'retention' not in self.state:
            return
        self.validate_identity()
        self.retention_fault()  # Validate and retain recovery ownership until every boundary is clean.
        self.recover_retention_request()
        self.retention_pause(cancel_pending=True)
        self.cleanup_target()
        self.cleanup_client()
        current = self.retention_records()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB retention changed the unrelated retained manual backup')
        if current and 'original_config' not in self.state:
            # Recover the old gate's already-restored backup=false transition.
            # Native deletion consults the current CR's MainStorage even for a
            # retained Backup CR; re-enable only this journal's product storage.
            self.configure()
            self.retention_pause(cancel_pending=True)
            current = self.retention_records()
        # Validate the whole set before deleting its first remaining request.
        for obj in current:
            record = self.state['retention']['records'][obj['metadata']['name']]
            if obj['metadata']['uid'] != record['uid']:
                raise ValueError('MongoDB retention cleanup refuses a replaced request')
        for obj in current:self.delete_request(obj)
        if self.retention_records():
            raise ValueError('MongoDB retention cleanup has surviving scheduled requests')
        for record in self.state['retention']['records'].values():
            if record.get('backup') and not self.retention_expired(record['backup']):
                raise ValueError('MongoDB retention cleanup has surviving scheduled backup objects')
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB retention changed the unrelated retained manual backup')
        self.restore_configuration()  # MainStorage must survive the last native deletion.
        self.scenario.health()
        self.state['retention']['cleaned'] = True;self.save()
        self.retention_fault(clear=True)
        print('MONGODB_RETENTION_SCOPED_CLEAN_PASS scheduled-cr-objects=absent manual-backup=unchanged source=healthy', flush=True)

    def recover_retention_request(self):
        """Recover the recorded request from the pre-fix constructor-ID defect only."""
        expected = self.scenario.fixture.NAME + '-' + self.kind + '-' + self.state['run_id']
        recovery = self.state['retention'].get('abandoned_request')
        if self.state['request']['name'] == expected and not recovery:
            return
        if self.source_identity() != self.state['retention']['source_identity']:
            raise ValueError('MongoDB retention request recovery source material changed')
        source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        if source['metadata']['uid'] != self.state['source_cluster_uid']:
            raise ValueError('MongoDB retention request recovery source identity changed')
        if self.verify_objects(persist=False, request_name=expected) != self.state['backup']:
            raise ValueError('MongoDB retention request recovery original backup changed')
        if recovery is None:
            request, desired = self.state['request'], self.state['desired_request']
            if ('original_config' in self.state or 'target' in self.state or self.kind != 'logical' or
                    not re.fullmatch(re.escape(self.scenario.fixture.NAME) + r'-logical-[0-9a-f]{16}', request['name'])):
                raise ValueError('MongoDB retention request recovery refuses unclassified configuration')
            current = self.lab.get('psmdb-backup', request['name'], self.scenario.ns)
            if (not current or current['metadata']['uid'] == self.state['backup']['uid'] or
                    current.get('status', {}).get('state') != 'ready'):
                raise ValueError('MongoDB retention abandoned request must be an independent completed backup')
            self.scenario.product.check_operation_identity({'mongodb_name': self.scenario.fixture.NAME},
                request, desired, current, source)
            recovery = {'request': request, 'desired': desired, 'uid': current['metadata']['uid'],
                        'backup': self.verify_objects(persist=False, request_name=request['name'])}
            self.state['retention']['abandoned_request'] = recovery;self.save()
        if recovery.get('cleaned'):
            return
        if 'original_config' not in self.state:
            self.configure()  # Journal ID reuses the original CR; enables agents for native deletion.
        else:
            config = yaml.safe_load(self.config_path.read_text())
            if {k: config.get(k) for k in self.state['applied_config']} != self.state['applied_config']:
                raise ValueError('MongoDB retention request recovery refuses concurrent edits')
            self.setup_product()
        current = self.lab.get('psmdb-backup', recovery['request']['name'], self.scenario.ns)
        if current:
            self.scenario.product.check_operation_identity({'mongodb_name': self.scenario.fixture.NAME},
                recovery['request'], recovery['desired'], current, source)
            if current['metadata']['uid'] != recovery['uid']:
                raise ValueError('MongoDB retention abandoned request UID changed')
            if (not current['metadata'].get('deletionTimestamp') and
                    self.verify_objects(persist=False, request_name=current['metadata']['name']) != recovery['backup']):
                raise ValueError('MongoDB retention abandoned objects changed before deletion')
            finalizers = current['metadata'].get('finalizers', [])
            if set(finalizers) - {'percona.com/delete-backup', 'internal.percona.com/release-lock'}:
                raise ValueError('MongoDB retention abandoned request has an unknown finalizer')
            if 'percona.com/delete-backup' not in finalizers:
                self.api_input('-n', self.scenario.ns, 'patch', 'psmdb-backup', current['metadata']['name'],
                    '--type=json', '--patch-file=/dev/stdin', obj=[
                        {'op': 'test', 'path': '/metadata/uid', 'value': recovery['uid']},
                        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
                        {'op': 'add', 'path': '/metadata/finalizers', 'value': finalizers + ['percona.com/delete-backup']}])
            self.delete_request(current)  # Operator/PBM remove data AND internal backup metadata.
        if not self.retention_expired(recovery['backup']):
            raise ValueError('MongoDB retention abandoned backup objects remain')
        recovery['cleaned'] = True;self.save()
        print('MONGODB_RETENTION_REQUEST_RECOVERED original-request=preserved abandoned-cr-objects=absent', flush=True)

    def retention(self):
        self.state = json.loads(self.path.read_text())
        self.validate_identity()
        if (self.kind != 'logical' or not self.lab.verify().get('healthy') or
                list(self.lab.directory.glob('*fault*.json')) or 'target' in self.state or
                'original_config' in self.state or not self.state.get('backup') or
                ('retention' in self.state and not self.state['retention'].get('cleaned'))):
            raise ValueError('MongoDB retention requires accepted logical backup and healthy clean source')
        self.scenario.health();self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB retention refuses changed manual backup objects')
        existing = self.lab.get('psmdb-backup', ns=self.scenario.ns)['items']
        if any(o['metadata'].get('labels', {}).get('percona.com/cluster') == self.scenario.fixture.NAME and
               o['metadata'].get('labels', {}).get('percona.com/backup-ancestor') == 'daily-full' for o in existing):
            raise ValueError('MongoDB retention refuses an existing scheduled backup set')
        self.state['retention'] = {'started': datetime.now(timezone.utc).isoformat(),
            'source_identity': self.source_identity(), 'records': {}}
        self.save();self.retention_fault()
        first_error = None
        try:
            self.scenario.prefix = self.state['source_prefix']
            self.state['client_attempted'] = True;self.save()
            self.scenario.client_create()
            self.state.update(client_uid=self.scenario.client_uid, workload_started=True);self.save()
            self.scenario.workload_started = True
            self.scenario.js("assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'backup',value:NumberDecimal('19.95')}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));assert.commandWorked(d.runCommand({createIndexes:prefix+'_documents',indexes:[{key:{value:1},name:'backup_value'}]}));print('MONGODB_RETENTION_SEED_PASS');",
                             'MONGODB_RETENTION_SEED_PASS', app=True)
            self.configure(schedule=True)
            deadline = time.monotonic() + 900
            while True:
                current = self.retention_records(verify=True)
                if any(o.get('status', {}).get('state') in ('error', 'rejected') for o in current):
                    raise ValueError('MongoDB scheduled retention backup failed')
                verified = sorted((r for r in self.state['retention']['records'].values() if r.get('backup')),
                                  key=lambda r: (r['created'], r['name']))
                ready = [o for o in current if o.get('status', {}).get('state') == 'ready' and not o['metadata'].get('deletionTimestamp')]
                names = {o['metadata']['name'] for o in current}
                if (len(verified) >= 3 and len(ready) == 2 and verified[0]['name'] not in names and
                        self.retention_expired(verified[0]['backup'])):
                    self.state['retention']['expired'] = verified[0]['name'];self.save()
                    break
                if time.monotonic() >= deadline:
                    raise ValueError('MongoDB retention did not prove three scheduled backups and count-two expiry')
                print('MONGODB_RETENTION_HEARTBEAT stage=schedule-and-prune verified=' + str(len(verified)), flush=True)
                time.sleep(5)
            print('MONGODB_RETENTION_PRUNE_PASS schedule=real count-two=verified expired-cr-objects=absent', flush=True)
            self.retention_pause()
            current = self.retention_records(verify=True)
            if not current or any(o.get('status', {}).get('state') != 'ready' for o in current):
                raise ValueError('MongoDB retention stopped without a healthy retained backup')
            newest = max(current, key=lambda o: (o['metadata']['creationTimestamp'], o['metadata']['name']))
            record = self.state['retention']['records'][newest['metadata']['name']]
            self.state['retention']['retained'] = {'name': record['name'], 'backup': record['backup']};self.save()
            self.cleanup_client();self.retention_fault(clear=True)
            self.restore_independent(retention=True)
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.retention_recover()
            except BaseException:
                print('MONGODB_RETENTION_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        print('MONGODB_RETENTION_PASS', flush=True)

    def target_declaration(self):
        target = self.state['target']
        objects = [self.lab.get('psmdb', target['name'], self.scenario.ns)]
        objects += [self.lab.get('pvc', r['name'], self.scenario.ns)
                    for r in target['resources'] if r['kind'] == 'pvc']
        if len(objects) != 4 or any(not obj for obj in objects):
            raise ValueError('MongoDB encryption control requires target CR and three claims')
        return [{'uid': o['metadata']['uid'], 'generation': o['metadata'].get('generation'),
                 'spec': o['spec']} for o in objects]

    def observe_encryption_failure(self, restore):
        """Read actual native files before cleanup; retain only safe, durable facts."""
        target = self.state['target']
        status = restore.get('status', {})
        observation = {'target_uid': target['cr_uid'],
            'restore_uid': restore.get('metadata', {}).get('uid'),
            'restore_state': status.get('state'), 'pbm_name': status.get('pbmName'),
            'rejection': target.get('wrong_key_rejection'), 'members': []}
        self.state['encryption_observation'] = observation
        self.save()
        proven = False
        for pod in self.lab.get('pod', ns=self.scenario.ns)['items']:
            meta = pod['metadata']
            if meta.get('labels', {}).get('app.kubernetes.io/instance') != target['name']:
                continue
            if not target_resource_owned(pod, target['name'], self.state['run_id']):
                raise ValueError('MongoDB encryption diagnostics refuse foreign target Pod')
            member = {'name': meta['name'], 'uid': meta['uid'], 'files': []}
            observation['members'].append(member)
            for path in ('/data/db/pbm.restore.log', '/tmp/mongod.log', '/tmp/pbm-agent.log'):
                result = subprocess.run([*self.lab.command, '-n', self.scenario.ns, 'exec', meta['name'],
                    '-c', 'mongod', '--', 'tail', '-n', '4000', path],
                    capture_output=True, text=True, timeout=30)
                row = {'path': path, 'rc': result.returncode}
                if result.returncode == 0:
                    row.update(sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),
                               fingerprints=encryption_fingerprints(result.stdout))
                    proven = encryption_failure(status, result.stdout) or proven
                member['files'].append(row)
                self.save()
        observation['native_failure_proven'] = proven
        self.save()
        self.diagnostic_path()
        self.scenario.fixture.write_private(self.diagnostic_directory / 'encryption-observation.json', observation)
        return proven

    def missing_key(self):
        target = self.state['target']
        path = self.base / 'clusters' / target['deployment'] / 'config.yml'
        original = path.read_text()
        config = yaml.safe_load(original)
        missing = target['name'] + '-absent-' + self.state['run_id']
        if self.lab.get('secret', missing, self.scenario.ns):
            raise ValueError('MongoDB missing-key control refuses an existing Secret')
        before = self.target_declaration()
        try:
            config['mongodb_encryption_key_secret'] = missing
            path.write_text(yaml.safe_dump(config, sort_keys=False))
            output = self.expected_target_failure('missing_key')
            if ('核验 MongoDB 资源所有权、凭据及故障域' not in output or
                    'MongoDB Kubernetes/client request failed' not in output or
                    '安装或升级限定命名空间的 MongoDB Operator' in output or
                    self.target_declaration() != before or
                    self.lab.get('secret', missing, self.scenario.ns) or
                    self.source_identity() != target['source_identity']):
                raise ValueError('MongoDB missing-key rejection did not stop at the unchanged product preflight')
            print('MONGODB_ENCRYPTION_MISSING_KEY_PASS preflight=refused cr-pvc=unchanged', flush=True)
        finally:
            path.write_text(original)
            self.target_setup()
            self.target_health()

    def encryption(self):
        self.state = json.loads(self.path.read_text())
        self.validate_identity()
        if (self.kind != 'physical' or not self.lab.verify().get('healthy') or
                list(self.lab.directory.glob('*fault*.json')) or 'original_config' in self.state or
                'target' in self.state or not self.state.get('backup')):
            raise ValueError('MongoDB encryption recovery requires accepted physical backup and healthy clean source')
        self.scenario.health()
        self.request_for_cleanup()
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB encryption recovery refuses changed backup objects')
        first_error = None
        try:
            self.prepare_target(wrong_key=True)
            self.missing_key()
            target = self.state['target']
            path = self.base / 'clusters' / target['deployment'] / 'config.yml'
            config = yaml.safe_load(path.read_text())
            request = {'kind': 'restore', 'name': target['name'] + '-' + self.state['run_id'],
                'cluster_uid': target['cr_uid'], 'destination': self.state['backup']['destination'], 'type': 'physical'}
            target.update(request=request, desired_request=self.scenario.product.operation_object(config, request))
            self.save()
            config['mongodb_requests'] = [request]
            path.write_text(yaml.safe_dump(config, sort_keys=False))
            with self.trace_restore():
                self.expected_target_failure('wrong_key')
            current = self.lab.get('psmdb-restore', request['name'], self.scenario.ns)
            self.scenario.product.check_operation_identity(config, request, target['desired_request'], current,
                self.lab.get('psmdb', target['name'], self.scenario.ns))
            if not self.observe_encryption_failure(current):
                raise ValueError('MongoDB wrong-key restore lacks the pinned native encryption failure evidence')
            target['key_failure_proven'] = True;self.save()
            print('MONGODB_ENCRYPTION_WRONG_KEY_PASS restore=error native-key-error=verified', flush=True)
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.cleanup_target()
            except BaseException:
                print('MONGODB_TARGET_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if self.verify_objects(persist=False) != self.state['backup']:
            raise ValueError('MongoDB encryption negative control changed backup objects')
        self.restore_independent()
        print('MONGODB_ENCRYPTION_ESCROW_KEY_PASS bson-index-user-boundary=verified', flush=True)
        print('MONGODB_ENCRYPTION_SCOPED_CLEAN_PASS failed-and-correct-targets=absent source=healthy', flush=True)
        print('MONGODB_ENCRYPTION_PASS', flush=True)

    @contextmanager
    def trace_restore(self):
        if self.kind != 'physical':
            yield
            return
        self.diagnostic_path()
        with tempfile.NamedTemporaryFile(dir=self.diagnostic_directory,
                prefix='physical-restore-timeline-', suffix='.jsonl', delete=False) as trace:
            path = Path(trace.name)
        target = self.state['target']['name']
        stop = threading.Event()
        def capture():
            with path.open('w') as stream:
                while not stop.is_set():
                    record = {'utc': datetime.now(timezone.utc).isoformat()}
                    try:
                        output = self.lab.run('-n', self.scenario.ns, 'get',
                            'psmdb,pods,statefulsets,services,endpointslices.discovery.k8s.io',
                            '-o', 'json', timeout=5)
                        record['objects'] = restore_timeline(json.loads(output), target)
                    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
                        record['capture_error'] = type(exc).__name__
                    stream.write(json.dumps(record) + '\n');stream.flush()
                    stop.wait(2)
        worker = threading.Thread(target=capture, name='mongodb-restore-diagnostics')
        worker.start()
        print('MONGODB_RESTORE_TIMELINE_STARTED directory=' + str(self.diagnostic_directory), flush=True)
        try:
            yield
        finally:
            stop.set()
            worker.join()  # Every API sample has its own five-second deadline.

    def capture_target(self):
        target = self.state['target']
        name = target['name']
        cr = self.lab.get('psmdb', name, self.scenario.ns)
        if cr:
            if (not target_resource_owned(cr, name, self.state['run_id']) or
                    cr['metadata'].get('annotations', {}).get('kubeauto.io/encryption-key-sha256') != target['key_sha256'] or
                    cr['spec']['secrets']['users'] != name + '-users' or
                    target.get('cr_uid') not in (None, cr['metadata']['uid'])):
                raise ValueError('MongoDB independent target CR identity changed')
            target['cr_uid'] = cr['metadata']['uid'];self.save()
        known = {(r['kind'], r['name']): r for r in target['resources']}
        for kind in TARGET_APIS:
            for obj in self.lab.get(kind, ns=self.scenario.ns)['items']:
                meta = obj['metadata']
                candidate = (meta['name'] == name or meta['name'].startswith(name + '-') or
                             meta['name'] == 'internal-' + name + '-users' or meta['name'].startswith('mongod-data-' + name + '-'))
                if not candidate:
                    continue
                if not target_resource_owned(obj, name, self.state['run_id']):
                    raise ValueError('MongoDB independent target contains a foreign resource')
                if kind in ('psmdb-restore', 'psmdb-backup'):
                    receipt = (target.get('refusal_requests', {}).get(meta['name']) if kind=='psmdb-restore' else None)
                    receipt = receipt or target.get('operation_requests', {}).get(kind)
                    request = receipt['request'] if receipt else target.get('request')
                    desired = receipt['desired'] if receipt else target.get('desired_request')
                    # Recover the first sharded run's pre-receipt journal only
                    # from its recorded UID, completed backup location and fixed
                    # product inputs. Never infer ownership from live spec.
                    if kind=='psmdb-backup' and request and request.get('kind')=='restore':
                        backup=self.state.get('sharding_restore',{}).get('backup')
                        previous=known.get((kind,meta['name']))
                        if (not target.get('sharded') or not backup or not previous or
                                previous['uid']!=meta['uid'] or meta['name']!=name+'-'+self.state['run_id'] or
                                obj.get('status',{}).get('destination')!=backup['destination']):
                            raise ValueError('MongoDB sharded legacy request receipt cannot be reconstructed')
                        config=yaml.safe_load((self.base/'clusters'/target['deployment']/'config.yml').read_text())
                        request={'kind':'backup','name':meta['name'],'type':'logical'}
                        desired=self.scenario.product.operation_object(config,request)
                    if not cr or not request or not desired or request['name'] != meta['name']:
                        raise ValueError('MongoDB independent target contains an unrecorded operation request')
                    if request.get('kind') != ('backup' if kind=='psmdb-backup' else 'restore'):
                        raise ValueError('MongoDB independent target operation kind differs from its receipt')
                    self.scenario.product.check_operation_identity({}, request, desired, obj, cr)
                key = (kind, meta['name'])
                previous = known.get(key)
                if previous and previous['uid'] not in (None, meta['uid']):
                    # Physical restores delete StatefulSets; their Pod and PDB
                    # dependants are recreated under the verified new owner.
                    replacement = kind in ('pod', 'statefulset', 'pdb') and target.get('request')
                    stability = self.state.get('stability', {})
                    # A reboot may evict router/data Pods before their node
                    # returns. Retire only ephemeral Pods under the original
                    # StatefulSet, after the owning phase has cleaned its data
                    # and client; never accept a changed controller or volume.
                    if (kind == 'pod' and target.get('sharded') and stability.get('topology') == 'sharded' and
                            stability.get('data_cleaned') is True and stability.get('client_uid') is None):
                        controllers = [r for r in meta.get('ownerReferences', []) if r.get('controller') is True]
                        replacement = len(controllers) == 1 and controllers[0].get('kind') == 'StatefulSet' and any(
                            r['kind'] == 'statefulset' and r['name'] == controllers[0].get('name') and
                            r['uid'] == controllers[0].get('uid') for r in known.values())
                    owners = {r['uid'] for r in meta.get('ownerReferences', [])}
                    allowed_owners = {target.get('cr_uid')} | {
                        r['uid'] for r in known.values() if r['kind'] == 'statefulset'}
                    if not replacement or not owners.intersection(allowed_owners):
                        raise ValueError('MongoDB independent target resource was replaced')
                record = {'kind': kind, 'name': meta['name'], 'uid': meta['uid']}
                if kind == 'pvc':
                    if obj['spec'].get('storageClassName') != 'mongodb-longhorn':
                        raise ValueError('MongoDB independent target has a foreign StorageClass')
                    record['volume_name'] = obj['spec'].get('volumeName')
                known[key] = record
        target['resources'] = list(known.values());self.save()

    def target_health(self):
        target = self.state['target']
        directory = self.base / 'clusters' / target['deployment']
        config = json.loads((directory / 'yml/mongodb/input.json').read_text())
        self.scenario.product.health(config, self.scenario.product.API(directory / 'kubectl.kubeconfig'))
        claims = [r for r in target['resources'] if r['kind'] == 'pvc']
        source = target['source_identity']['claims'].values()
        count = 9 if target.get('sharded') else 3
        if (len(claims) != count or len({r.get('volume_name') for r in claims}) != count or
                any(not r.get('volume_name') for r in claims) or
                {r['uid'] for r in claims}.intersection(r['uid'] for r in source) or
                {r['volume_name'] for r in claims}.intersection(r['volume'] for r in source)):
            raise ValueError('MongoDB independent target must have distinct PVC/PVs for its full topology, separate from source')

    def target_capacity(self, pvc_size=None, *, wait_timeout=0):
        """Check the dedicated CSI fixture's advertised per-host volume limit.

        Kubernetes 1.33 uses maximumVolumeSize when present, else capacity.
        Free filesystem bytes alone do not include Longhorn reservations.
        This is a prerequisite snapshot, not a promise of future allocation.
        """
        config = yaml.safe_load(self.config_path.read_text())
        if config['mongodb_storage_class'] != 'mongodb-longhorn':
            raise ValueError('MongoDB restore capacity check requires the owned CSI fixture')
        requested = self.scenario.product.quantity(pvc_size or config['mongodb_pvc_size'], 'memory')
        workers = {'mongodb-node-' + ip.rsplit('.', 1)[1] for ip in self.scenario.fixture.NODES[3:]}
        deadline = time.monotonic() + wait_timeout
        while True:
            limits = {}
            objects = self.lab.get('csistoragecapacities', ns='mongodb-lab-storage')['items']
            for obj in objects:
                if obj.get('storageClassName') != 'mongodb-longhorn':
                    continue
                topology = obj.get('nodeTopology', {})
                labels = topology.get('matchLabels', {})
                host = labels.get('kubernetes.io/hostname')
                if (host not in workers or len(labels) != 1 or topology.get('matchExpressions') or host in limits):
                    raise ValueError('MongoDB restore CSI capacity has unexpected or duplicate host topology')
                value = obj.get('maximumVolumeSize', obj.get('capacity'))
                limits[host] = 0 if value in (None, '0', 0) else self.scenario.product.quantity(value, 'memory')
            if set(limits) == workers and all(limit >= requested for limit in limits.values()):break
            print('MONGODB_RESTORE_CAPACITY_OBSERVED requested-bytes=' + str(int(requested)) +
                  ' host-limits=' + json.dumps(limits, sort_keys=True), flush=True)
            if time.monotonic() >= deadline:
                raise ValueError('MongoDB restore requires advertised CSI capacity on all three worker failure domains')
            time.sleep(5)
        print('MONGODB_RESTORE_CAPACITY_PASS worker-domains=3 requested-bytes=' + str(int(requested)), flush=True)

    def prepare_target(self, *, wrong_key=False, fresh_credentials=False, escrow=None, backup_prefix='backup', sharded=False):
        if wrong_key and self.kind != 'physical':
            raise ValueError('MongoDB wrong-key fixture requires a physical backup')
        if sum((bool(wrong_key), bool(fresh_credentials), escrow is not None)) > 1:
            raise ValueError('MongoDB target credential modes are mutually exclusive')
        if type(sharded) is not bool or (sharded and self.kind != 'logical'):
            raise ValueError('MongoDB sharded restore fixture requires an explicit logical topology')
        if backup_prefix not in ('backup', 'disaster/' + self.state['run_id'], 'sharding/' + self.state['run_id']):
            raise ValueError('MongoDB target refuses a foreign backup prefix')
        name = 'mongodb-gate-' + self.kind + '-restore'
        deployment = 'mongodb-restore-' + self.kind
        directory = self.base / 'clusters' / deployment
        if directory.exists() or 'target' in self.state:
            raise ValueError('MongoDB independent target already exists; recover its owning journal')
        # Logical restore reconstructs this fixture's small data set on new
        # volumes; it must not inherit earlier source expansion/pressure tests.
        # Physical restore retains the source size required by its backup.
        if self.kind == 'logical':self.target_capacity('2Gi')
        else:self.target_capacity()
        for kind in TARGET_APIS:
            for obj in self.lab.get(kind, ns=self.scenario.ns)['items']:
                n = obj['metadata']['name']
                if n == name or n.startswith(name + '-') or n == 'internal-' + name + '-users' or n.startswith('mongod-data-' + name + '-'):
                    raise ValueError('MongoDB independent restore refuses existing target resources')
        objects = self.scenario.fixture.credentials(name)['items']
        if escrow is not None:
            expected = {o['metadata']['name'].removeprefix(name + '-'): set(o['stringData']) for o in objects}
            if (not isinstance(escrow, dict) or set(escrow) != set(expected) or
                    any(not isinstance(escrow[k], dict) or set(escrow[k]) != expected[k] or
                        any(not isinstance(v, str) or not v for v in escrow[k].values()) for k in expected)):
                raise ValueError('MongoDB target escrow is incomplete')
            for values in escrow.values():
                for value in values.values():base64.b64decode(value, validate=True)
        for obj in objects:
            suffix = obj['metadata']['name'].removeprefix(name + '-')
            if escrow is not None:
                obj.pop('stringData');obj['data'] = dict(escrow[suffix])
            elif suffix in ('users', 'app', 'key') and not fresh_credentials:
                source = self.lab.get('secret', self.scenario.fixture.NAME + '-' + suffix, self.scenario.ns)
                obj.pop('stringData');obj['data'] = dict(source['data'])
                if suffix == 'key' and wrong_key:
                    raw = bytearray(base64.b64decode(base64.b64decode(source['data']['encryption-key'])))
                    if len(raw) != 32:raise ValueError('MongoDB source escrow key has an invalid length')
                    raw[0] ^= 1
                    obj['data']['encryption-key'] = base64.b64encode(base64.b64encode(raw)).decode()
            obj['metadata']['labels'] = LABELS | {'kubeauto.io/test-run': self.state['run_id']}
        # Normalize generated and escrowed material before fingerprinting it.
        for obj in objects:
            if 'stringData' in obj:
                obj['data'] = {k: base64.b64encode(v.encode()).decode() for k, v in obj.pop('stringData').items()}
        key_obj = next(o for o in objects if o['metadata']['name'] == name + '-key')
        self.state['target'] = {'name': name, 'deployment': deployment, 'cr_uid': None, 'resources': [],
            'source_identity': self.source_identity(),
            'key_sha256': hashlib.sha256(base64.b64decode(key_obj['data']['encryption-key'])).hexdigest()}
        if sharded:self.state['target']['sharded'] = True
        self.save()
        self.scenario.fixture.write_private(self.lab.directory / ('mongodb-fault-target-' + self.kind + '.json'), {
            'cluster_uid': self.state['cluster_uid'], 'run_id': self.state['run_id'],
            'backup_journal': self.path.name, 'target': name})
        result = subprocess.run(['kubecli', 'new', deployment], cwd=self.base, timeout=120)
        if result.returncode:
            raise ValueError('MongoDB independent target deployment creation failed')
        config = yaml.safe_load(self.config_path.read_text())
        config.update(CLUSTER_NAME=deployment, mongodb_name=name, mongodb_users=[], mongodb_requests=[],
            mongodb_users_secret=name + '-users', mongodb_tls_secret=name + '-tls',
            mongodb_tls_internal_secret=name + '-tls-internal', mongodb_encryption_key_secret=name + '-key',
            mongodb_backup_enabled=True, mongodb_pitr_enabled=False, mongodb_backup_storage_type='minio',
            mongodb_backup_bucket=self.state['bucket'], mongodb_backup_prefix=backup_prefix,
            mongodb_backup_endpoint=self.state['name'] + '.' + self.scenario.ns + '.svc.cluster.local:9000',
            mongodb_backup_credentials_secret=self.state['name'] + '-credentials',
            mongodb_backup_ca_secret=self.state['name'] + '-tls', mongodb_backup_ca_key='ca.crt',
            mongodb_backup_schedule_enabled=False, mongodb_request_timeout=1800)
        if self.kind == 'logical':
            config.update(mongodb_pvc_size='2Gi', mongodb_volume_expansion_enabled=False)
        if sharded:
            config.update(mongodb_sharding_enabled=True,
                mongodb_replsets=[{'name': 'rs0', 'size': 3}, {'name': 'rs1', 'size': 3}],
                mongodb_config_size=3, mongodb_mongos_size=3)
        path = directory / 'config.yml'
        path.write_text(yaml.safe_dump(config, sort_keys=False));path.chmod(0o600)
        directory.joinpath('hosts').write_text(self.scenario.fixture.inventory(deployment))
        directory.joinpath('hosts').chmod(0o600)
        shutil.copyfile(self.lab.directory / 'kubectl.kubeconfig', directory / 'kubectl.kubeconfig')
        directory.joinpath('kubectl.kubeconfig').chmod(0o600)
        for obj in objects:
            record = {'kind': 'secret', 'name': obj['metadata']['name'], 'uid': None}
            self.state['target']['resources'].append(record);self.save()
            created = json.loads(self.api_input('create', '-f', '-', '-o', 'json', obj=obj))
            record['uid'] = created['metadata']['uid'];self.save()
        self.target_setup()
        self.target_health()
        self.scenario.prefix = self.state['source_prefix']
        self.sharded_client_create() if sharded else self.scenario.client_create(name)
        self.scenario.js("const d=c.getDB('appdb');assert.eq(0,d.getCollection(prefix+'_documents').countDocuments({}));print('MONGODB_TARGET_EMPTY_PASS');", 'MONGODB_TARGET_EMPTY_PASS')
        self.scenario.js("const c=new Mongo(process.env.MONGODB_URI);let rejected=false;try{rejected=!c.getDB('appdb').auth({user:'application',pwd:process.env.APP_PASSWORD});}catch(e){assert.eq(18,e.code);rejected=true;}assert(rejected);print('MONGODB_TARGET_APPLICATION_ABSENT_PASS');", 'MONGODB_TARGET_APPLICATION_ABSENT_PASS', authenticate=False)
        self.scenario.js("const d=c.getDB('appdb');assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'after-backup',value:'must-disappear'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_TARGET_POST_BACKUP_MARKER_PASS');", 'MONGODB_TARGET_POST_BACKUP_MARKER_PASS')
        self.sharded_client_delete() if sharded else self.scenario.delete_client()
        print('MONGODB_TARGET_READY independent-cr=true independent-pvc=true independent-ca=true application-absent=true', flush=True)

    def cleanup_target(self):
        if not self.state or 'target' not in self.state:
            return
        stability = self.state.get('stability')
        if stability and (not stability.get('data_cleaned') or stability.get('client_uid')):
            raise ValueError('MongoDB target cleanup requires the owning stability recovery first')
        self.validate_identity()
        target = self.state['target']
        expected_name = 'mongodb-gate-' + self.kind + '-restore'
        if target.get('name') != expected_name or target.get('deployment') != 'mongodb-restore-' + self.kind:
            raise ValueError('MongoDB independent target journal has a foreign deployment')
        fault = self.lab.directory / ('mongodb-fault-target-' + self.kind + '.json')
        if fault.exists() and json.loads(fault.read_text()) != {
                'cluster_uid': self.state['cluster_uid'], 'run_id': self.state['run_id'],
                'backup_journal': self.path.name, 'target': expected_name}:
            raise ValueError('MongoDB independent target has a foreign fault journal')
        directory = self.base / 'clusters' / target['deployment']
        if directory.is_symlink():
            raise ValueError('MongoDB independent target directory is a symlink')
        if target.get('sharded'):
            client = self.lab.get('pod', self.scenario.client, self.scenario.ns)
            if client and (not target.get('client_uid') or client['metadata']['uid'] != target['client_uid']):
                raise ValueError('MongoDB sharded recovery client identity changed')
            self.scenario.prefix = self.state['source_prefix']
            self.scenario.client_uid = target.get('client_uid')
        self.refusal_fault_recover()
        self.capture_target()
        volumes, plan = set(), []
        for record in target['resources']:
            kind, name = record['kind'], record['name']
            if kind not in TARGET_APIS:
                raise ValueError('MongoDB independent target cleanup has an unknown resource')
            obj = self.lab.get(kind, name, self.scenario.ns)
            if obj and (not target_resource_owned(obj, expected_name, self.state['run_id']) or
                        record['uid'] not in (None, obj['metadata']['uid'])):
                raise ValueError('MongoDB independent target cleanup refuses changed ownership')
            if kind == 'pvc' and record.get('volume_name'):
                volume = self.lab.get('pv', record['volume_name'])
                if volume:
                    ref = volume['spec'].get('claimRef', {})
                    if (volume['spec'].get('storageClassName') != 'mongodb-longhorn' or
                            (ref.get('namespace'), ref.get('name'), ref.get('uid')) !=
                            (self.scenario.ns, name, record['uid'])):
                        raise ValueError('MongoDB independent target cleanup refuses a rebound volume')
                volumes.add(record['volume_name'])
            if obj:
                plan.append((kind, name, obj['metadata']['uid']))
        if self.source_identity() != target['source_identity']:
            raise ValueError('MongoDB independent target cleanup source identity changed')
        rendered_path = self.lab.directory / 'yml/mongodb/input.json'
        rendered = json.loads(rendered_path.read_text()) if rendered_path.exists() else {}
        if rendered.get('mongodb_name') != self.scenario.fixture.NAME or rendered.get('mongodb_namespace') != self.scenario.ns:
            config = yaml.safe_load(self.config_path.read_text())
            if config.get('mongodb_name') != self.scenario.fixture.NAME or config.get('mongodb_namespace') != self.scenario.ns:
                raise ValueError('MongoDB source configuration recovery refuses changed deployment identity')
            self.setup_product()
        self.scenario.delete_client()
        # Stop the target controller graph before deleting retained PVCs.
        plan.sort(key=lambda r: (r[0] not in ('psmdb-restore', 'psmdb-backup'), r[0] != 'psmdb', r[0] == 'pvc'))
        for kind, name, uid in plan:
            current = self.lab.get(kind, name, self.scenario.ns)
            if not current:
                continue
            if current['metadata']['uid'] != uid:
                raise ValueError('MongoDB independent target changed during cleanup')
            self.resume_target_deletion(kind, current)
            endpoint = TARGET_APIS[kind] + '/namespaces/' + self.scenario.ns + '/' + TARGET_PLURALS[kind] + '/' + name
            if not current['metadata'].get('deletionTimestamp'):
                self.api_input('delete', '--raw', endpoint, '-f', '-', obj={
                    'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
            if kind == 'psmdb' and target.get('key_failure_proven'):
                deleting = self.lab.get(kind, name, self.scenario.ns)
                if deleting:self.resume_target_deletion(kind, deleting)
            self.lab.run('-n', self.scenario.ns, 'wait', '--for=delete', kind + '/' + name, '--timeout=600s', timeout=620)
        for volume in volumes:
            self.lab.run('wait', '--for=delete', 'pv/' + volume, '--timeout=300s', timeout=320)
        if directory.exists(): shutil.rmtree(directory)
        self.scenario.health()
        self.state.pop('target');self.save()
        if fault.exists(): fault.unlink()
        print('MONGODB_RESTORE_TARGET_CLEAN_PASS source-pvc-and-secrets=preserved target-pv=absent', flush=True)

    def resume_target_deletion(self, kind, current):
        """Resume disposal of a failed target whose ordered shutdown is stuck.

        Operator 1.23.1 queries every member before electing ordinal zero for
        deletion. An unreachable restored member can block this indefinitely.
        This is limited to an already deleting disposable target, after the
        complete cleanup plan and source identities have been verified.
        """
        if kind != 'psmdb' or not current['metadata'].get('deletionTimestamp'):
            return
        target = self.state['target']
        meta = current['metadata']
        finalizers = meta.get('finalizers', [])
        ordered = 'percona.com/delete-psmdb-pods-in-order'
        if ordered not in finalizers:
            return
        if (meta['name'] != target['name'] or meta['uid'] != target['cr_uid'] or
                not target_resource_owned(current, target['name'], self.state['run_id']) or
                finalizers != [ordered] or self.source_identity() != target['source_identity']):
            raise ValueError('MongoDB target deletion recovery refuses changed ownership or finalizers')
        self.api_input('-n', self.scenario.ns, 'patch', 'psmdb', meta['name'], '--type=json',
            '--patch-file=/dev/stdin', obj=[
                {'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
                {'op': 'test', 'path': '/metadata/finalizers', 'value': [ordered]},
                {'op': 'remove', 'path': '/metadata/finalizers/0'}])
        print('MONGODB_TARGET_DELETION_RESUMED disposable-target=true source-preserved=true', flush=True)

    def retention_storage_paused(self):
        """Allow only the exact product-converged retention transition during restore."""
        applied = self.state.get('applied_config', {})
        if (applied.get('mongodb_backup_enabled') is not True or
                applied.get('mongodb_backup_schedule_enabled') is not False or
                not self.state.get('retention', {}).get('retained')):
            return False
        config = yaml.safe_load(self.config_path.read_text())
        if {key: config.get(key) for key in applied} != applied:
            return False
        source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
        if (not source or not self.scenario.product.owned(source) or
                source['metadata'].get('namespace') != self.scenario.ns or
                source['metadata']['uid'] != self.state['source_cluster_uid']):
            return False
        backup = source['spec'].get('backup', {})
        storage_name = self.state['desired_request']['spec']['storageName']
        storages = backup.get('storages', {})
        if (backup.get('enabled') is not True or set(storages) != {storage_name} or
                any(task.get('enabled') for task in backup.get('tasks', []))):
            return False
        storage = storages[storage_name]
        obj = storage.get('minio', {})
        fields = {'bucket': 'mongodb_backup_bucket', 'prefix': 'mongodb_backup_prefix',
                  'endpointUrl': 'mongodb_backup_endpoint', 'credentialsSecret': 'mongodb_backup_credentials_secret'}
        return (storage.get('type') == 'minio' and obj.get('secure') is True and
                obj.get('insecureSkipTLSVerify', False) is False and
                all(obj.get(key) == applied.get(setting) for key, setting in fields.items()) and
                obj.get('caBundle') == {'name': applied.get('mongodb_backup_ca_secret'),
                                        'key': applied.get('mongodb_backup_ca_key')})

    def rollback_snapshot(self):
        target = self.state['target']
        rows = {kind: [obj for obj in self.lab.get(kind, ns=self.scenario.ns)['items']
                if target_resource_owned(obj, target['name'], self.state['run_id'])]
                for kind in ('pods', 'pvc', 'secrets', 'psmdb')}
        if (len(rows['psmdb']) != 1 or rows['psmdb'][0]['metadata']['uid'] != target['cr_uid'] or
                len(rows['pods']) != 3 or len(rows['pvc']) != 3 or len(rows['secrets']) < 5):
            raise ValueError('MongoDB rollback target resource graph is incomplete')
        result = self.canonical_snapshot(rows)
        operator = self.lab.get('deployment', 'mongodb-operator', self.scenario.ns)
        result['operator'] = {'uid': operator['metadata']['uid'], 'spec': operator['spec']}
        return result

    def rollback_refusals(self):
        """Reject both downgrades through kubecli without changing the target."""
        target = self.state['target']
        path = self.base / 'clusters' / target['deployment'] / 'config.yml'
        original = yaml.safe_load(path.read_text())
        if (original['mongodb_ver'], original['mongodb_operator_ver']) != ('8.0.32-14', '1.23.1'):
            raise ValueError('MongoDB rollback refusal requires the current pinned target')
        self.scenario.client_create(target['name'])
        fcv = "const f=assert.commandWorked(a.runCommand({getParameter:1,featureCompatibilityVersion:1})).featureCompatibilityVersion;assert.eq('8.0',f.version);assert.eq(undefined,f.targetVersion);assert.eq(undefined,f.previousVersion);const d=c.getDB('appdb');assert.eq('must-disappear',d.getCollection(prefix+'_documents').findOne({_id:'after-backup'}).value);print('MONGODB_ROLLBACK_FCV_PASS version=8.0 transition=absent target-data=unchanged');"
        try:
            before = self.rollback_snapshot()
            for layer, values in (
                    ('server', {'mongodb_ver': '8.0.26-11'}),
                    ('operator', {'mongodb_operator_ver': '1.23.0', 'mongodb_operator_chart_ver': '1.23.1'})):
                self.scenario.js(fcv, 'MONGODB_ROLLBACK_FCV_PASS version=8.0 transition=absent target-data=unchanged')
                path.write_text(yaml.safe_dump(original | values, sort_keys=False));path.chmod(0o600)
                try:
                    result = subprocess.run(['kubecli', 'setup', target['deployment'], '07', '--tags', 'mongodb'],
                        cwd=self.base, capture_output=True, text=True, timeout=5400)
                    output = result.stdout + result.stderr
                    expected = 'MongoDB ' + layer + ' downgrade is refused; restore a compatible backup into a separate cluster'
                    if result.returncode == 0 or expected not in output or 'MONGODB_DATA_PLANE_READY' in output:
                        raise ValueError('MongoDB rollback did not reach its exact product refusal: ' + layer)
                    if self.rollback_snapshot() != before:
                        raise ValueError('MongoDB rollback refusal changed target or Operator: ' + layer)
                    self.scenario.js(fcv, 'MONGODB_ROLLBACK_FCV_PASS version=8.0 transition=absent target-data=unchanged')
                    print('MONGODB_ROLLBACK_REFUSAL_PASS layer=' + layer + ' fcv=8.0 resources-and-data=unchanged', flush=True)
                finally:
                    path.write_text(yaml.safe_dump(original, sort_keys=False));path.chmod(0o600)
        finally:
            self.scenario.delete_client()

    def restore_independent(self, *, pitr=False, retention=False, rollback=False):
        self.state = json.loads(self.path.read_text())
        self.validate_identity()
        if (list(self.lab.directory.glob('*fault*.json')) or
                ('original_config' in self.state and not (retention and self.retention_storage_paused())) or
                'target' in self.state or not self.state.get('backup')):
            raise ValueError('MongoDB independent restore requires accepted backup and no pending recovery')
        self.scenario.health()
        self.request_for_cleanup()
        selected = self.state.get('retention', {}).get('retained') if retention else None
        if retention and (pitr or not selected or self.kind != 'logical'):
            raise ValueError('MongoDB retention restore requires its verified logical scheduled backup')
        if selected:
            current = self.retention_records()
            record = self.state['retention']['records'].get(selected['name'], {})
            if (selected['name'] not in {o['metadata']['name'] for o in current} or
                    record.get('backup') != selected['backup']):
                raise ValueError('MongoDB retention restore selection is no longer journal-bound')
        backup = selected['backup'] if selected else self.state['backup']
        verified = (self.verify_objects(persist=False, request_name=selected['name']) if selected else
                    self.verify_objects(persist=False))
        if verified != backup:
            raise ValueError('MongoDB independent restore refuses changed backup objects')
        first_error = None
        try:
            self.prepare_target()
            if rollback:self.rollback_refusals()
            target = self.state['target']
            directory = self.base / 'clusters' / target['deployment']
            config = yaml.safe_load(directory.joinpath('config.yml').read_text())
            request = {'kind': 'restore', 'name': target['name'] + '-' + self.state['run_id'],
                'cluster_uid': target['cr_uid'], 'destination': backup['destination'], 'type': self.kind}
            if pitr:request['target_utc'] = self.state['pitr']['target_utc']
            target['request'] = request
            target['desired_request'] = self.scenario.product.operation_object(config, request)
            self.save()
            config['mongodb_requests'] = [request]
            directory.joinpath('config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
            with self.trace_restore():
                self.target_setup()
                current = self.lab.get('psmdb-restore', request['name'], self.scenario.ns)
                self.scenario.product.check_operation_identity(config, request, target['desired_request'], current,
                    self.lab.get('psmdb', target['name'], self.scenario.ns))
                if not self.scenario.product.operation_complete('restore', current.get('status', {})):
                    raise ValueError('MongoDB independent restore request has not completed')
                self.target_health()
            self.scenario.client_create(target['name'])
            self.scenario.workload_started = True
            check = "const col=d.getCollection(prefix+'_documents');assert.eq(" + ('2' if pitr else '1') + ",col.countDocuments({}));assert.eq('19.95',col.findOne({_id:'backup'}).value.toString());assert.eq(null,col.findOne({_id:'after-backup'}));assert.eq(1,col.getIndexes().filter(i=>i.name==='backup_value').length);"
            if pitr:
                check += "assert.eq('include',col.findOne({_id:'before-pitr'}).value);assert.eq(null,col.findOne({_id:'after-pitr'}));print('MONGODB_PITR_RESTORED_BOUNDARY_PASS before=included after=excluded');"
            check += "print('MONGODB_TARGET_RESTORE_DATA_PASS bson=true index=true user-restored=true boundary=true');"
            output = self.scenario.js(check, 'MONGODB_TARGET_RESTORE_DATA_PASS bson=true index=true user-restored=true boundary=true', app=True)
            if pitr:
                marker = 'MONGODB_PITR_RESTORED_BOUNDARY_PASS before=included after=excluded'
                if marker not in output.splitlines():raise ValueError('MongoDB PITR restored boundary was not proven')
                print(marker, flush=True)
            if self.source_identity() != target['source_identity']:
                raise ValueError('MongoDB independent restore changed source identities')
            self.scenario.cleanup()
        except BaseException as exc:
            first_error = exc
            try:self.diagnostics()
            except (ValueError, OSError, subprocess.SubprocessError):pass
            raise
        finally:
            try:self.cleanup_target()
            except BaseException:
                print('MONGODB_TARGET_RECOVERY_FAIL journal=preserved', file=sys.stderr)
                if first_error is None:raise
        if rollback and self.verify_objects(persist=False) != backup:
            raise ValueError('MongoDB rollback recovery changed backup objects')
        print('MONGODB_' + self.kind.upper() + '_RESTORE_SCOPED_CLEAN_PASS retained=verified-source-backup', flush=True)
        print('MONGODB_' + self.kind.upper() + '_RESTORE_PASS', flush=True)
        if rollback:
            print('MONGODB_ROLLBACK_SCOPED_CLEAN_PASS rejected-target=disposed backup=unchanged source=healthy', flush=True)
            print('MONGODB_ROLLBACK_PASS', flush=True)

    def restore(self):
        if not self.path.exists():
            raise ValueError('MongoDB restore requires the accepted backup fixture')
        self.state = json.loads(self.path.read_text())
        self.validate_identity()
        faults = list(self.lab.directory.glob('*fault*.json'))
        if faults or 'original_config' in self.state or not self.state.get('backup'):
            raise ValueError('MongoDB restore refuses pending faults or an incomplete backup')
        if not self.lab.verify().get('healthy'):
            raise ValueError('MongoDB restore requires the verified healthy baseline')
        self.scenario.health()
        for record in self.state['resources']:
            obj = self.lab.get(record['kind'], record['name'], self.scenario.ns)
            if not obj or not self.owned(obj) or obj['metadata']['uid'] != record['uid']:
                raise ValueError('MongoDB restore fixture ownership has changed')
        self.request_for_cleanup()  # Verify source CR and immutable backup identity.
        verified = self.verify_objects(persist=False)
        if verified != self.state['backup']:
            raise ValueError('MongoDB backup object checksums changed before restore')
        self.scenario.prefix = self.state['source_prefix']
        fault_path = self.lab.directory / 'mongodb-fault-restore.json'
        try:
            self.state['client_attempted'] = True;self.save()
            self.scenario.client_create()
            self.state.update(client_uid=self.scenario.client_uid, workload_started=True);self.save()
            self.scenario.workload_started = True
            self.scenario.js("assert.eq(0,d.getCollection(prefix+'_documents').countDocuments({}));assert.commandWorked(d.runCommand({insert:prefix+'_documents',documents:[{_id:'after-backup',value:'must-disappear'}],writeConcern:{w:'majority',j:true,wtimeout:10000}}));print('MONGODB_RESTORE_PRECONDITION_PASS');",
                             'MONGODB_RESTORE_PRECONDITION_PASS', app=True)
            source = self.lab.get('psmdb', self.scenario.fixture.NAME, self.scenario.ns)
            if source['metadata']['uid'] != self.state['source_cluster_uid']:
                raise ValueError('MongoDB restore source UID changed')
            self.scenario.fixture.write_private(fault_path, {'cluster_uid': self.state['cluster_uid'],
                'source_uid': self.state['source_cluster_uid'], 'run_id': self.state['run_id'],
                'backup_journal': self.path.name, 'type': self.kind})
            request = {'kind': 'restore', 'name': self.state['request']['name'] + '-restore',
                       'cluster_uid': self.state['source_cluster_uid'], 'backup_name': self.state['request']['name']}
            self.configure(request)
            self.scenario.js("const col=d.getCollection(prefix+'_documents');assert.eq(1,col.countDocuments({}));assert.eq('19.95',col.findOne({_id:'backup'}).value.toString());assert.eq(null,col.findOne({_id:'after-backup'}));assert.eq(1,col.getIndexes().filter(i=>i.name==='backup_value').length);print('MONGODB_RESTORE_DATA_PASS decimal=true index=true post-backup-write=absent');",
                             'MONGODB_RESTORE_DATA_PASS decimal=true index=true post-backup-write=absent', app=True)
            self.scenario.health()
            self.state['restore_complete'] = True;self.save()
            self.restore_configuration()
            self.cleanup_client()
            fault_path.unlink()
        except BaseException:
            if not fault_path.exists():
                self.cleanup_client()
            else:
                print('MONGODB_RESTORE_RECOVERY_REQUIRED journal=preserved data-consistency=unverified', file=sys.stderr)
            raise
        print('MONGODB_' + self.kind.upper() + '_RESTORE_INPLACE_SCOPED_CLEAN_PASS retained=verified-backup-artifact', flush=True)
        print('MONGODB_' + self.kind.upper() + '_RESTORE_INPLACE_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('upgrade', 'upgrade-recover', 'backup-clients-recover', 'cleanup', 'cleanup-success', 'cleanup-failure', 'cleanup-probe', 'cleanup-recover', 'disaster-recovery', 'stability', 'stability-faults', 'stability-recover', 'rollback', 'rollback-stop', 'stability-stop', 'stability-faults-stop', 'sharding-restore', 'encryption', 'backup-refusal', 'backup-refusal-tail', 'backup-refusal-oplog', 'backup-refusal-stop', 'retention', 'retention-recover', 'retention-stop', 'logical-backup', 'physical-backup', 'logical-restore', 'physical-restore', 'logical-restore-inplace', 'physical-restore-inplace', 'pitr', 'cleanup-logical', 'cleanup-physical', 'cleanup-pitr', 'cleanup-target-logical', 'cleanup-target-physical', 'cleanup-target-pitr'))
    parser.add_argument('--base', type=Path, default=Path('/usr/local/kubeauto'))
    parser.add_argument('--outcome', choices=('success', 'failure', 'interruption'))
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    if args.mode in ('retention-stop', 'backup-refusal-stop', 'rollback-stop', 'stability-stop', 'stability-faults-stop'):
        stop_retention(args.base, mode=args.mode.removesuffix('-stop'))
        return 0
    kind = args.mode.removeprefix('cleanup-target-').removeprefix('cleanup-').removesuffix('-backup').removesuffix('-restore-inplace').removesuffix('-restore')
    if args.mode in ('upgrade', 'upgrade-recover'):kind = 'logical'
    if args.mode in ('encryption', 'disaster-recovery'):kind = 'physical'
    if args.mode in ('stability', 'stability-faults', 'stability-recover', 'rollback', 'sharding-restore', 'retention', 'retention-recover', 'backup-refusal', 'backup-refusal-tail', 'backup-refusal-oplog'):kind = 'logical'
    if args.mode in ('cleanup', 'cleanup-success', 'cleanup-failure', 'cleanup-probe', 'cleanup-recover'):kind = 'logical'
    try:
        if args.mode == 'backup-clients-recover':
            for dependency in ('logical', 'physical', 'pitr'):
                candidate = BackupGate(args.base, 'logical' if dependency == 'pitr' else dependency, pitr=dependency == 'pitr')
                if candidate.path.exists():candidate.recover_storage_client()
            print('MONGODB_BACKUP_CLIENTS_RECOVER_SCOPED_CLEAN_PASS data-volumes-secrets=retained old-helper-pods=retired', flush=True)
            print('MONGODB_BACKUP_CLIENTS_RECOVER_PASS', flush=True)
            return 0
        pitr = kind == 'pitr'
        gate = BackupGate(args.base, 'logical' if pitr else kind, pitr=pitr)
        if args.mode in ('cleanup', 'cleanup-success', 'cleanup-failure'):
            gate.cleanup_qualification(only=None if args.mode == 'cleanup' else args.mode.removeprefix('cleanup-'))
        elif args.mode == 'cleanup-probe':
            if args.outcome is None:raise ValueError('MongoDB cleanup child requires its outcome')
            gate.cleanup_probe(args.outcome)
        elif args.mode == 'cleanup-recover':
            q = json.loads((gate.lab.directory / 'mongodb-cleanup-qualification.json').read_text())
            pid = q.get('child_pid')
            if type(pid) is not int or pid <= 1:raise ValueError('MongoDB cleanup recovery child PID is invalid')
            try:os.kill(pid, 0)
            except ProcessLookupError:pass
            else:raise ValueError('MongoDB cleanup recovery refuses a live child PID')
            gate.cleanup_qualification_finish()
            print('MONGODB_CLEANUP_RECOVER_PASS', flush=True)
        elif args.mode == 'upgrade':gate.upgrade()
        elif args.mode == 'upgrade-recover':gate.upgrade_recover()
        elif args.mode == 'disaster-recovery':gate.disaster()
        elif args.mode == 'sharding-restore':gate.sharding_restore()
        elif args.mode == 'rollback':gate.restore_independent(rollback=True)
        elif args.mode in ('stability', 'stability-faults'):gate.stability(focused=args.mode == 'stability-faults')
        elif args.mode == 'stability-recover':
            gate.state = json.loads(gate.path.read_text());gate.stability_cleanup(interrupted=True)
            gate.cleanup_target();print('MONGODB_STABILITY_RECOVER_PASS', flush=True)
        elif args.mode == 'encryption':gate.encryption()
        elif args.mode == 'backup-refusal':gate.refusal()
        elif args.mode == 'backup-refusal-tail':gate.refusal(focus='tail')
        elif args.mode == 'backup-refusal-oplog':gate.refusal(focus='oplog')
        elif args.mode == 'retention':gate.retention()
        elif args.mode == 'retention-recover':
            gate.state = json.loads(gate.path.read_text())
            if 'retention' not in gate.state:raise ValueError('MongoDB retention recovery journal is absent')
            gate.retention_recover()
            print('MONGODB_RETENTION_RECOVER_PASS', flush=True)
        elif args.mode.startswith('cleanup-target-'):
            if gate.path.exists():
                gate.state = json.loads(gate.path.read_text())
                gate.cleanup_target()
                gate.cleanup_disaster()
                gate.cleanup_sharding_restore()
                gate.refusal_prefix_cleanup()
        elif args.mode.startswith('cleanup-'): gate.cleanup()
        elif args.mode.endswith('-restore-inplace'): gate.restore()
        elif args.mode.endswith('-restore'): gate.restore_independent()
        else: gate.execute(pitr=True) if pitr else gate.execute()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print('MONGODB_BACKUP_FAIL reason=' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
