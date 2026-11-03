#!/usr/bin/env python3
"""Private in-memory validation for the fixed Longhorn 1.12.1 SystemBackup bundle.

No extraction and no raw resource values are returned. This is a candidate for
an owning focused gate, not evidence of successful system recovery.
"""
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import stat
import zipfile

import yaml

MAX_ARCHIVE = 64 * 1024**2
MAX_CONTENT = 128 * 1024**2


def manager_commit(image):
    """Expected build identity for the qualified 1.12.1 Manager artifacts."""
    commits = {
        'longhorn-manager:v1.12.1': '8320238242e8c804325d9588ec1db10feb9f36b8',
        'longhorn-manager:v1.12.1-kubeauto.1':
            '8320238242e8c804325d9588ec1db10feb9f36b8-kubeauto.9275735a33fe',
        'longhorn-manager:v1.12.1-kubeauto.2':
            '8320238242e8c804325d9588ec1db10feb9f36b8-kubeauto.07485cfc14e1',
    }
    # Installation verifies the registry/digest. SystemBackup records the
    # mirrored tag and the binary's meta.GitCommit, including its patch identity.
    try:
        return commits[image.rsplit('/', 1)[-1]]
    except KeyError:
        raise ValueError('unqualified system backup Manager image') from None


def validate_bundle(config_bytes, archive_bytes, expected, *, data_engine='v2'):
    """Validate exact remote object identity, SHA256 and declared source UIDs."""
    if data_engine not in ('v1', 'v2'): raise ValueError('unsupported expected backup data engine')
    if len(config_bytes) > 1024**2 or not 0 < len(archive_bytes) <= MAX_ARCHIVE:
        raise ValueError('system backup object sizes outside qualification boundary')
    config = json.loads(config_bytes)
    name = expected['name']
    if not re.fullmatch(r'[a-z0-9][-a-z0-9]{0,62}', name):
        raise ValueError('system backup invalid expected name')
    fields = {'Name': name, 'LonghornVersion': 'v1.12.1',
              'LonghornGitCommit': expected['manager_commit'],
              'BackupTargetURL': expected['target'], 'ManagerImage': expected['manager_image'],
              'Checksum': hashlib.sha256(archive_bytes).hexdigest()}
    mismatches = [key for key, value in fields.items() if config.get(key) != value]
    if not config.get('CreatedAt'):
        mismatches.append('CreatedAt')
    if mismatches:
        # Field names aid diagnostics without exposing target URLs or resources.
        raise ValueError('system backup configuration differs: '+', '.join(mismatches))
    documents = {}
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
        rows = bundle.infolist()
        if (not rows or len(rows) > 256 or len({row.filename for row in rows}) != len(rows) or
                sum(row.file_size for row in rows) > MAX_CONTENT):
            raise ValueError('system backup member count, duplicates or uncompressed size differs')
        for row in rows:
            path = PurePosixPath(row.filename)
            parts = row.filename.rstrip('/').split('/')
            mode = row.external_attr >> 16
            if (path.is_absolute() or '\\' in row.filename or
                    any(part in ('', '.', '..') for part in parts) or parts[0] != name or
                    row.flag_bits & 1 or stat.S_ISLNK(mode) or
                    (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError('unsafe system backup member')
            if row.is_dir():
                continue
            if path.suffix != '.yaml':
                raise ValueError('unexpected system backup member format')
            # ZipFile.read validates the CRC and declared decompressed length.
            document = yaml.safe_load(bundle.read(row))
            relative = str(path.relative_to(name))
            if relative == 'metadata.yaml':
                metadata = document
                # EncodeToYAMLFile uses yaml.v2 directly, which ignores json
                # tags and lowercases Go field names in systemBackupMeta.
                fields = {'longhornversion': 'v1.12.1',
                          'longhorngitcommit': expected['manager_commit'],
                          'longhornnamespaceuuid': expected['namespace_uid'],
                          'kubernetesversion': 'v1.33.6', 'managerimage': expected['manager_image']}
                if not isinstance(metadata, dict):
                    raise ValueError('system backup metadata lacks a mapping')
                mismatches = [key for key, value in fields.items() if metadata.get(key) != value]
                if not metadata.get('systembackupcreatedat'):
                    mismatches.append('systembackupcreatedat')
                if mismatches:
                    raise ValueError('system backup metadata differs: '+', '.join(mismatches))
                documents[relative] = metadata
                continue
            if not relative.startswith('yamls/') or not isinstance(document, dict):
                raise ValueError('system backup resource member differs')
            if not document.get('apiVersion') or not document.get('kind', '').endswith('List') or not isinstance(document.get('items'), list):
                raise ValueError('system backup resource member lacks native list schema')
            if document['kind'] in ('SecretList', 'PodList', 'NodeList'):
                raise ValueError('system backup unexpectedly includes excluded resources')
            documents[relative] = document
    if 'metadata.yaml' not in documents:
        raise ValueError('system backup lacks source metadata')
    declarations = {}
    for key, document in documents.items():
        if key == 'metadata.yaml':
            continue
        kind = document['kind'].removesuffix('List')
        for row in document['items']:
            identity = (kind, row['metadata'].get('namespace', ''), row['metadata']['name'])
            if identity in declarations:
                raise ValueError('duplicate resource identity in system backup')
            declarations[identity] = row
    for identity, uid in expected['resource_uids'].items():
        obj = declarations.get(identity)
        if not obj or obj['metadata'].get('uid') != uid:
            raise ValueError('system backup lacks an original declared resource')
    volume = declarations[('Volume', 'longhorn-system', expected['volume'])]
    if (volume['spec'].get('dataEngine') != data_engine or int(volume['spec']['size']) != 2*1024**3 or
            volume.get('status', {}).get('lastBackup') != expected['last_backup']):
        raise ValueError('system backup source data engine, capacity or last backup differs')
    return {'name': name, 'zip_size': len(archive_bytes), 'zip_sha256': config['Checksum'],
            'zip_checksum_algorithm': 'sha256',
            'config_size': len(config_bytes), 'config_sha256': hashlib.sha256(config_bytes).hexdigest(),
            'members': len(documents), 'resource_count': len(declarations),
            'original_resources_verified': len(expected['resource_uids']),
            'secret_pod_node_excluded': True, 'volume_backup_policy': 'disabled',
            'new_volume_data_backup_proven': False, 'system_restore_proven': False}

# The fixture journal deliberately retains unconfirmed create outcomes.
import secrets
import shutil
from pathlib import Path

BASE = Path('/usr/local/kubeauto')
NAMESPACE = 'longhorn-system'


class Probe:
    def __init__(self, lab, *, data_engine='v2'):
        self.lab = lab
        self.data_engine = data_engine
        self.store = lab.backup_store()
        self.record = lab.DIRECTORY/'longhorn-systembackup-qualification.json'

    def identity(self):
        lab = self.lab
        row = self.store.receipt()
        binding = row.get('product_binding', {})
        container = self.store.inspect(row)
        state = lab.owner()
        target = lab.get('backuptargets.longhorn.io', 'default', NAMESPACE)
        namespace = lab.get('namespace', NAMESPACE)
        if (not container or not container.get('State', {}).get('Running') or
                not namespace or namespace['metadata']['uid'] != state['namespace_uid'] or
                binding.get('namespace_uid') != state['namespace_uid'] or not target or
                target['metadata']['uid'] != binding.get('target_uid') or
                target['spec'].get('backupTargetURL') != binding.get('fixture_spec', {}).get('backupTargetURL') or
                target['spec'].get('credentialSecret') != binding.get('secret_name') or
                {k:v for k,v in target['spec'].items() if k != 'syncRequestedAt'} !=
                {k:v for k,v in binding.get('fixture_spec', {}).items() if k != 'syncRequestedAt'}):
            raise ValueError('SystemBackup fixture target, container or namespace changed')
        return {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
                'namespace_uid': state['namespace_uid'], 'target_uid': target['metadata']['uid'],
                'store_name': row['name'], 'container_id': container['Id']}

    def journal(self):
        if self.record.is_symlink():
            raise ValueError('symlink SystemBackup journal')
        row = json.loads(self.record.read_text())
        if any(row.get(key) != value for key, value in self.identity().items()):
            raise ValueError('SystemBackup journal belongs to a different fixture')
        if (not re.fullmatch(r'system-probe-[a-f0-9]{24}', row.get('name', '')) or
                row.get('request') != {'kind': 'system-backup', 'name': row['name'], 'volume_backup_policy': 'disabled'}):
            raise ValueError('SystemBackup journal request differs')
        return row

    def bound_object(self, row):
        obj = self.lab.get('systembackups.longhorn.io', row['name'], NAMESPACE)
        if not obj:
            return None
        if not row.get('uid') or obj['metadata']['uid'] != row['uid']:
            raise ValueError('SystemBackup create outcome unconfirmed or identity replaced')
        product = self.lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(obj, product.request_object(row['request']))
        return obj

    def prefix(self, row):
        return 'backupstore/system-backups/v1.12.1/'+row['name']+'/'

    def object_keys(self, row):
        bucket = self.store.receipt()['bucket']
        lines = self.store.mc('ls', '--recursive', '--json', 'lh/'+bucket).splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        if any(item.get('status') != 'success' for item in entries):
            raise ValueError('SystemBackup object listing did not succeed')
        return sorted(item['key'] for item in entries if item.get('key', '').startswith(self.prefix(row)))

    def cleanup(self):
        if not self.record.exists() and not self.record.is_symlink():
            return
        row = self.journal()
        if any(obj['spec'].get('systemBackup') == row['name'] for obj in
               self.lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']):
            raise ValueError('SystemBackup still has a SystemRestore consumer')
        keys = self.object_keys(row)
        allowed = {self.prefix(row)+name for name in ('system-backup.cfg', 'system-backup.zip')}
        if set(keys) - allowed:
            raise ValueError('foreign object in SystemBackup cleanup prefix')
        self.cleanup_readback(row)
        obj = self.bound_object(row)
        if obj:
            self.lab.remove(obj)
        def retired():
            if self.bound_object(row):
                return False
            return not self.object_keys(row)
        self.lab.wait('systembackup-native-and-remote-clean', 300, retired)
        self.record.unlink()
        print('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS', flush=True)

    def cleanup_readback(self, row):
        name = row.get('container_temporary')
        if name:
            if not re.fullmatch(r'/tmp/longhorn-system-object-[a-f0-9]{32}\.zip', name):
                raise ValueError('foreign SystemBackup temporary object path')
            self.lab.execute(['docker', 'exec', row['container_id'], 'rm', '-f', '--', name])
        directory = self.store.directory/'system-bundle-readback'
        if directory.is_symlink() or directory.exists() and any(p.is_symlink() for p in directory.rglob('*')):
            raise ValueError('symlink in SystemBackup private readback')
        if directory.exists():
            shutil.rmtree(directory)

    def readback(self, row, expected):
        keys = [self.prefix(row)+name for name in ('system-backup.cfg', 'system-backup.zip')]
        if self.object_keys(row) != sorted(keys):
            raise ValueError('SystemBackup backend does not contain its two exact objects')
        raw = self.store.mc('cat', 'lh/'+row['store_name']+'/'+keys[0]).encode()
        temporary = '/tmp/longhorn-system-object-'+secrets.token_hex(16)+'.zip'
        directory = self.store.directory/'system-bundle-readback'
        if directory.exists() or directory.is_symlink():
            raise ValueError('previous private SystemBackup readback remains')
        row['container_temporary'] = temporary
        self.lab.write_private(self.record, row)
        directory.mkdir(mode=0o700)
        archive = None
        try:
            self.store.mc('cp', 'lh/'+row['store_name']+'/'+keys[1], temporary)
            self.lab.execute(['docker', 'exec', row['container_id'], 'chmod', '0600', temporary])
            size = int(self.lab.execute(['docker', 'exec', row['container_id'], 'stat', '--format=%s', temporary]).stdout)
            if not 0 < size <= MAX_ARCHIVE:
                raise ValueError('SystemBackup ZIP outside private readback limit')
            target = directory/'bundle.zip'
            self.lab.execute(['docker', 'cp', row['container_id']+':'+temporary, str(target)])
            target.chmod(0o600)
            archive = target.read_bytes()
            if len(archive) != size:
                raise ValueError('SystemBackup private copy size differs')
            result = validate_bundle(raw, archive, expected, data_engine=self.data_engine)
            # A second exact configuration read rejects concurrent remote replacement.
            if self.store.mc('cat', 'lh/'+row['store_name']+'/'+keys[0]).encode() != raw:
                raise ValueError('SystemBackup remote configuration changed during readback')
            return result
        except Exception:
            # Preserve bounded, exact failed inputs privately before deleting
            # remote fixtures; no resource content is echoed into the log.
            import os
            import tempfile
            failure = Path(tempfile.mkdtemp(prefix='longhorn-system-bundle-failure-', dir='/var/tmp'))
            for name, value in (('config.json', raw), ('bundle.zip', archive)):
                if value is None:
                    continue
                descriptor = os.open(failure/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(value)
            print('LONGHORN_V2_SYSTEMBACKUP_PRIVATE_BUNDLE directory='+str(failure), flush=True)
            raise
        finally:
            self.cleanup_readback(row)

    def verify(self, journal, relay_pid, configuration):
        lab = self.lab
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('SystemBackup previous journal requires owning recovery')
        if any(lab.get(kind, namespace=NAMESPACE)['items'] for kind in
               ('systembackups.longhorn.io', 'systemrestores.longhorn.io')):
            raise ValueError('SystemBackup baseline has foreign system recovery requests')
        points = {row['request']['mode']: name for name, row in journal['backups'].items()}
        if set(points) != {'full', 'incremental'}:
            raise ValueError('SystemBackup requires its two previously verified volume backups')
        def source():
            obj = lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
            if not obj or obj['metadata']['uid'] != journal['volume_uid'] or obj['spec'].get('dataEngine') != 'v2':
                raise ValueError('SystemBackup source identity/engine changed')
            if not lab.replica_topology(journal, 3) or not lab.healthy_replicas(journal['volume']):
                return None
            return obj if obj.get('status', {}).get('lastBackup') == points['incremental'] else None
        volume = lab.wait('systembackup-source-last-backup', 300, source)
        pv = lab.get('pv', journal['pv'])
        pvc = lab.get('pvc', 'data', journal['namespace'])
        sc = lab.get('storageclass', journal['storage_class'])
        expected_uids = {('Volume', NAMESPACE, journal['volume']): journal['volume_uid'],
                         ('PersistentVolume', '', journal['pv']): journal['pv_uid'],
                         ('PersistentVolumeClaim', journal['namespace'], 'data'): journal['objects']['PersistentVolumeClaim/data'],
                         ('StorageClass', '', sc['metadata']['name']): sc['metadata']['uid']}
        if pv['metadata']['uid'] != journal['pv_uid'] or pvc['metadata']['uid'] != expected_uids[('PersistentVolumeClaim', journal['namespace'], 'data')]:
            raise ValueError('SystemBackup source PV/PVC identity differs')
        row = dict(self.identity(), name='system-probe-'+secrets.token_hex(12))
        row['request'] = {'kind': 'system-backup', 'name': row['name'], 'volume_backup_policy': 'disabled'}
        self.lab.write_private(self.record, row)
        before = {obj['metadata']['uid'] for obj in lab.get('backups.longhorn.io', namespace=NAMESPACE)['items']}
        lab.run_requests(relay_pid, [row['request']], backup_configuration=configuration)
        obj = lab.get('systembackups.longhorn.io', row['name'], NAMESPACE)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(obj, product.request_object(row['request']))
        if (not obj or obj.get('status', {}).get('state') != 'Ready' or
                obj['status'].get('version') != 'v1.12.1'):
            raise ValueError('SystemBackup lacks confirmed native Ready/version')
        row['uid'] = obj['metadata']['uid']
        lab.write_private(self.record, row)
        expected = {'name': row['name'], 'manager_commit': manager_commit(obj['status']['managerImage']),
                    'target': configuration['longhorn_backup_target'], 'namespace_uid': row['namespace_uid'],
                    'manager_image': obj['status']['managerImage'], 'volume': journal['volume'],
                    'last_backup': points['incremental'], 'resource_uids': expected_uids}
        result = self.readback(row, expected)
        request = {'kind': 'system-restore', 'name': 'restore-'+row['name'], 'source': row['name']}
        lab.run_requests(relay_pid, [request], backup_configuration=configuration,
                         expected_refusal='system restore requires all volumes detached')
        if lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']:
            raise ValueError('SystemRestore was created despite attached-volume refusal')
        if ({obj['metadata']['uid'] for obj in lab.get('backups.longhorn.io', namespace=NAMESPACE)['items']} != before or
                self.bound_object(row)['status'].get('state') != 'Ready' or not source()):
            raise ValueError('SystemBackup disabled policy changed backups or original source')
        result.update(uid=row['uid'], attached_restore_refused=True,
                      last_backup=points['incremental'], volume_uid=journal['volume_uid'],
                      volume_backup_count_unchanged=True)
        print('LONGHORN_V2_SYSTEMBACKUP_OBSERVED '+json.dumps(result, sort_keys=True), flush=True)
        self.cleanup()


def business(lab, relay_pid):
    probe = Probe(lab)
    verify_backup = lab.verify_backup
    cleanup_business = lab.cleanup_business
    def verify(journal, create, pid, payload, digest, configuration, **options):
        result = verify_backup(journal, create, pid, payload, digest, configuration, **options)
        probe.verify(journal, pid, configuration)
        return result
    def cleanup(*args, **options):
        probe.cleanup()
        return cleanup_business(*args, **options)
    lab.verify_backup = verify
    lab.cleanup_business = cleanup
    store = lab.backup_store()
    try:
        # Keep the independent store if native system ownership is uncertain.
        with lab.scoped_cleanup(lambda: (cleanup(), store.cleanup())):
            store.prepare()
            store.qualify()
            lab.business('backup', relay_pid, backup_configuration=store.product_configuration())
    finally:
        lab.verify_backup = verify_backup
        lab.cleanup_business = cleanup_business

import argparse
import importlib.util
import sys


def fixture():
    spec = importlib.util.spec_from_file_location('systembackup_base', BASE/'tests/helpers/longhorn-v2-backups.py')
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)
    install, lab, store_module = backup.fixture()
    return backup, install, lab, store_module


def baseline(backup, install, lab, store_module):
    backup.baseline(install, lab, store_module)
    if ((lab.DIRECTORY/'longhorn-systembackup-qualification.json').exists() or
            (lab.DIRECTORY/'longhorn-systembackup-qualification.json').is_symlink() or
            lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']):
        raise ValueError('V2 system recovery baseline retains a journal or restore')


def cleanup(lab):
    lab.restore_request_input()
    Probe(lab).cleanup()
    lab.cleanup_business()
    lab.backup_store().cleanup()


def cleanup_state(lab):
    """Only retire the exact fixtures archived after the checksum gate failure."""
    evidence = json.loads((BASE/'tests/evidence/longhorn-v2-systembackup-checksum-first-failure-20261020.json').read_text())
    original = json.loads((BASE/'tests/evidence/longhorn-v2-systembackup-null-conditions-first-failure-20261020.json').read_text())
    state = evidence['owned_state']
    record = lab.DIRECTORY/'longhorn-install-business.json'
    if record.is_symlink():
        raise ValueError('symlink SystemBackup cleanup business journal')
    journal = json.loads(record.read_text())
    probe = Probe(lab)
    identity = probe.identity()
    if (evidence['status'] != 'failed' or evidence['rc'] != 1 or
            journal != state['source_journal'] or
            any(identity.get(k) != v for k, v in original['recovery_journal'].items() if k in identity) or
            any(identity[k] != state[k] for k in ('cluster_uid', 'namespace_uid')) or
            probe.record.exists() or probe.record.is_symlink() or
            any(lab.get(kind, namespace=NAMESPACE)['items'] for kind in
                ('systembackups.longhorn.io', 'systemrestores.longhorn.io', 'backups.longhorn.io')) or
            probe.object_keys(original['recovery_journal'])):
        raise ValueError('SystemBackup cleanup no longer matches archived owned fixtures')
    return original['original_backup_baseline']


def write_failure_diagnostic(error):
    """Retain native API stderr privately; public logs contain only the path."""
    import tempfile
    import traceback
    with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-systembackup-failure-', suffix='.txt',
                                     dir='/var/tmp', delete=False) as diagnostic:
        traceback.print_exception(type(error), error, error.__traceback__, file=diagnostic)
        seen = set()
        current = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            for field in ('returncode', 'stderr'):
                value = getattr(current, field, None)
                if value is not None:
                    diagnostic.write('\n'+field+': '+str(value)+'\n')
            current = current.__cause__ or current.__context__
        return diagnostic.name


def recovery_state(lab):
    """Bind recovery to the archived first failure; never adopt by name alone."""
    evidence = json.loads((BASE/'tests/evidence/longhorn-v2-systembackup-null-conditions-first-failure-20261020.json').read_text())
    probe = Probe(lab)
    row = probe.journal()
    original = evidence['recovery_journal']
    if (evidence['status'] != 'failed' or evidence['classification'] != 'product' or
            evidence['rc'] != 1 or row != original or 'uid' in row):
        raise ValueError('SystemBackup recovery does not match the archived unconfirmed creation')
    obj = lab.get('systembackups.longhorn.io', row['name'], NAMESPACE)
    saved = evidence['native']
    if (not obj or obj['metadata']['uid'] != saved['metadata']['uid'] or
            obj['metadata'].get('creationTimestamp') != saved['metadata']['creationTimestamp'] or
            obj['metadata'].get('ownerReferences') != saved['metadata'].get('ownerReferences') or
            obj['spec'] != saved['spec'] or obj.get('status', {}).get('state') != 'Ready' or
            lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']):
        raise ValueError('SystemBackup recovery native identity, request or consumers changed')
    product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
    product.check_request(obj, product.request_object(row['request']))
    journal = json.loads((lab.DIRECTORY/'longhorn-install-business.json').read_text())
    if any(journal.get(key) != value for key, value in evidence['source_identity'].items()):
        raise ValueError('SystemBackup recovery source fixture identity changed')
    return probe, row, obj, journal, evidence


def recover(lab, relay_pid):
    probe, row, obj, journal, evidence = recovery_state(lab)
    binding = probe.store.receipt()['product_binding']
    secret = lab.get('secret', binding['secret_name'], NAMESPACE)
    if (not secret or secret['metadata']['uid'] != binding.get('secret_uid') or
            hashlib.sha256(json.dumps(secret.get('data', {}), sort_keys=True).encode()).hexdigest() != binding.get('secret_digest')):
        raise ValueError('SystemBackup recovery original fixture credential changed')
    configuration = {'longhorn_backup_target': binding['fixture_spec']['backupTargetURL'],
                     'longhorn_backup_secret': binding['secret_name']}
    # Replay exactly the original config+kubecli request. The product checks
    # immutable request identity and polls the original native UID.
    lab.run_requests(relay_pid, [row['request']], backup_configuration=configuration)
    current = lab.get('systembackups.longhorn.io', row['name'], NAMESPACE)
    if not current or current['metadata']['uid'] != obj['metadata']['uid'] or current['status'].get('state') != 'Ready':
        raise ValueError('SystemBackup replay did not confirm the original native resource')
    row['uid'] = obj['metadata']['uid']
    lab.write_private(probe.record, row)
    points = {item['request']['mode']: name for name, item in journal['backups'].items()}
    sc = lab.get('storageclass', journal['storage_class'])
    result = probe.readback(row, {
        'name': row['name'], 'manager_commit': '8320238242e8c804325d9588ec1db10feb9f36b8',
        'target': configuration['longhorn_backup_target'], 'namespace_uid': row['namespace_uid'],
        'manager_image': current['status']['managerImage'], 'volume': journal['volume'],
        'last_backup': points['incremental'], 'resource_uids': {
            ('Volume', NAMESPACE, journal['volume']): journal['volume_uid'],
            ('PersistentVolume', '', journal['pv']): journal['pv_uid'],
            ('PersistentVolumeClaim', journal['namespace'], 'data'): journal['objects']['PersistentVolumeClaim/data'],
            ('StorageClass', '', sc['metadata']['name']): sc['metadata']['uid']}})
    writer = lab.get('pod', 'writer', journal['namespace'])
    if (not writer or writer['metadata']['uid'] != journal['objects']['Pod/writer'] or
            not lab.replica_topology(journal, 3)):
        raise ValueError('SystemBackup recovery source client or healthy topology changed')
    payload = row['run_id']+'-after-full-after-incremental'
    digest = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
                         lab.payload_program(payload, read_only=True)).stdout.strip()
    if digest != evidence['source_sha256'] or digest != hashlib.sha256(payload.encode()*262144).hexdigest():
        raise ValueError('SystemBackup recovery original confirmed source bytes changed')
    # This is source-failure recovery, not the complete qualification. All
    # original native recovery material remains until the independent readback.
    probe.cleanup()
    lab.cleanup_business()
    probe.store.cleanup()
    result.update(uid=row['uid'], source_sha256=digest, source_bytes=len(payload)*262144,
                  original_create_replayed=True, qualified_system_restore=False)
    print('LONGHORN_V2_SYSTEMBACKUP_RECOVERY_OBSERVED '+json.dumps(result, sort_keys=True), flush=True)


def verify_recovery_log(log):
    rows = re.findall(r'^LONGHORN_V2_SYSTEMBACKUP_RECOVERY_OBSERVED (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('SystemBackup recovery lacks one complete observation')
    result = json.loads(rows[0])
    if (not result['uid'] or result['source_bytes'] <= 0 or
            not re.fullmatch('[a-f0-9]{64}', result['source_sha256']) or
            not re.fullmatch('[a-f0-9]{64}', result['zip_sha256']) or
            result['zip_checksum_algorithm'] != 'sha256' or
            result['original_create_replayed'] is not True or result['qualified_system_restore'] is not False):
        raise ValueError('SystemBackup recovery lacks original identity, content or replay')
    for marker in ('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS',
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS',
                   'LONGHORN_BACKUP_STORE_CLEAN_PASS', 'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('SystemBackup recovery lacks cleanup: '+marker)
    return result


def verify_log(log):
    rows = re.findall(r'^LONGHORN_V2_SYSTEMBACKUP_OBSERVED (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('SystemBackup qualification lacks one complete observation')
    result = json.loads(rows[0])
    if (not result['uid'] or not result['volume_uid'] or not result['last_backup'] or
            result['config_size'] <= 0 or result['zip_size'] <= 0 or result['members'] < 5 or
            result['original_resources_verified'] != 4 or result['resource_count'] < 4 or
            result['attached_restore_refused'] is not True or result['volume_backup_count_unchanged'] is not True or
            result['secret_pod_node_excluded'] is not True or result['system_restore_proven'] is not False or
            result['new_volume_data_backup_proven'] is not False or result['volume_backup_policy'] != 'disabled' or
            not re.fullmatch('[a-f0-9]{64}', result['zip_sha256']) or
            result['zip_checksum_algorithm'] != 'sha256' or
            not all(re.fullmatch('[a-f0-9]{64}', result[key]) for key in ('config_sha256', 'zip_sha256'))):
        raise ValueError('SystemBackup result lacks original resource/backup identity, content or refusal')
    receipts = re.findall(r'^LONGHORN_BACKUP_OBJECT_RECEIPT mode=(full|incremental) (.+)$', log, re.M)
    if [mode for mode,_ in receipts] != ['full', 'incremental']:
        raise ValueError('SystemBackup requires full and incremental external backup results')
    for mode,raw in receipts:
        receipt = json.loads(raw)
        if (receipt['size'] != 2*1024**3 or receipt['incremental'] is not (mode == 'incremental') or
                not receipt['objects'] or not all(o['size'] > 0 and re.fullmatch('[a-f0-9]{64}', o['sha256']) for o in receipt['objects'].values()) or
                mode == 'incremental' and receipt['backup'] != result['last_backup']):
            raise ValueError('SystemBackup lacks exact source backup object readback')
    boundaries = re.findall(r'^LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=(full|incremental) backup_sha256=([a-f0-9]{64}) source_sha256=([a-f0-9]{64}) independent_volume=(\S+)$', log, re.M)
    remounts = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if ([row[0] for row in boundaries] != ['full', 'incremental'] or
            len({row[1] for row in boundaries}) != 2 or len({row[2] for row in boundaries}) != 1 or
            len({row[3] for row in boundaries}) != 2 or any(row[1] == row[2] for row in boundaries) or
            len(remounts) != 1 or int(remounts[0][0]) <= 0 or remounts[0][1] != boundaries[0][2] or
            len(set(remounts[0][2].split(','))) != 3):
        raise ValueError('SystemBackup lacks independent restore time boundaries and source cross-node data')
    for marker in ('LONGHORN_V2_SYSTEMBACKUP_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED',
                   'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
                   'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED','LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS',
                   'LONGHORN_BACKUP_STORE_CLEAN_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_BACKUP_BUSINESS_PASS'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('SystemBackup lacks business/cleanup result: '+marker)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'business', 'cleanup', 'verify', 'verify-recovery', 'recovery-baseline', 'recover', 'cleanup-baseline'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode in ('verify', 'verify-recovery'):
        if not args.log:
            parser.error('verify requires --log')
        (verify_log if args.mode == 'verify' else verify_recovery_log)(args.log.read_text())
        return
    backup, install, lab, store_module = fixture()
    lab.require_lease()
    if args.mode == 'cleanup-baseline':
        print(cleanup_state(lab), flush=True)
    elif args.mode == 'recovery-baseline':
        _, _, _, _, evidence = recovery_state(lab)
        print(evidence['original_backup_baseline'], flush=True)
    elif args.mode == 'recover':
        if args.relay_pid <= 0:
            parser.error('recover requires positive --relay-pid')
        recover(lab, args.relay_pid)
    elif args.mode == 'baseline':
        baseline(backup, install, lab, store_module)
    elif args.mode == 'cleanup':
        cleanup(lab)
    else:
        if args.relay_pid <= 0:
            parser.error('business requires positive --relay-pid')
        backup.qualification_current(lab)
        business(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        diagnostic = write_failure_diagnostic(error)
        print('LONGHORN_V2_SYSTEMBACKUP_FAIL private_diagnostic='+diagnostic, file=sys.stderr)
        sys.exit(1)
