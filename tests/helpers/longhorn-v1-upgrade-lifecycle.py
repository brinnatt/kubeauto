#!/usr/bin/env python3
"""LONGHORN-35/36 V1 Manager/engine upgrade and isolated backup recovery.

Consumes the frozen upgrade primitives without changing the V2 qualification.
The runner retires the current empty storage installation before configure-old;
the independent .149 target remains on its existing supported V2 installation.
"""
import argparse
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import yaml

_spec = importlib.util.spec_from_file_location('v1_upgrade_shared', Path(__file__).with_name('longhorn-upgrade-lifecycle.py'))
upgrade = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(upgrade)
NS = upgrade.NS
SOURCE_NODES = ('192.168.122.135', '192.168.122.40', '192.168.122.72',
                '192.168.122.212', '192.168.122.165', '192.168.122.238')


def fixture():
    source = upgrade.load('longhorn-lab.py')
    # The proven V1 business/store helpers resolve their owning module here.
    sys.modules[source.__name__] = source
    backups = source.load_helper('longhorn-backups.py')
    shared = upgrade.load('longhorn-v2-dr-store.py')
    install = upgrade.load('longhorn-v2-dr-target-install.py')
    _, target = install.fixture()
    return source, target, backups, shared


def configure_old(lab, pid):
    """Clean old installation only; never point an active new cluster backwards."""
    if tuple(lab.NODES) != SOURCE_NODES:
        raise ValueError('V1 old installation requires its original six-node source')
    rows = json.loads(lab.execute(['helm', 'list', '-A', '-o', 'json',
        '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig')]).stdout)
    if (any(row['chart'].startswith('longhorn-') for row in rows) or
            any(obj['metadata']['name'].endswith('.longhorn.io') for obj in lab.get('crds')['items'])):
        raise ValueError('old V1 installation refuses an existing Longhorn release or CRD; no downgrade')
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    if config.get('longhorn_data_engines', ['v1']) != ['v1'] or config.get('longhorn_block_disks'):
        raise ValueError('V1 upgrade source must keep its filesystem-only configuration')
    config.update(longhorn_version=upgrade.OLD, longhorn_upgrade_id='', longhorn_upgrade_system_backup='')
    upgrade.atomic_config(lab, config); lab.supervise_core(pid, 'longhorn-setup')
    if upgrade.release(lab)['app_version'].removeprefix('v') != upgrade.OLD:
        raise ValueError('V1 old installation did not converge')
    lab.installation_health()
    print('LONGHORN_V1_UPGRADE_OLD_INSTALL_PASS version=1.12.0', flush=True)


class CleanupOutput:
    """Keep nested cleanup progress streaming with the owning V1 marker."""
    def __init__(self, stream): self.stream = stream
    def write(self, value):
        self.stream.write(value.replace('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS',
                                       'LONGHORN_V1_UPGRADE_SCOPED_CLEAN_PASS'))
        return len(value)
    def flush(self): self.stream.flush()


class V1Upgrade(upgrade.Upgrade):
    PROTECTION_FIELDS = {'id', 'source_identity', 'target_identity', 'payloads', 'points', 'stage',
                         'old_runtime', 'old_encrypted_device', 'restores'}
    MANAGER_FIELDS = PROTECTION_FIELDS | {'attempt', 'new_runtime', 'refusals', 'retained_before',
                                         'successful_configuration', 'system', 'target_key'}

    def __init__(self, source, target, backups, shared, source_pid, target_pid):
        super().__init__(source, target, backups, shared, source_pid, target_pid)
        self.path = source.DIRECTORY/'longhorn-v1-upgrade-lifecycle.json'

    def bound_configuration(self, holder, *, require_available=True):
        """Read an existing owned target binding without creating credentials."""
        binding = holder.receipt()['product_binding']; lab = holder.lab
        target = lab.get('backuptargets.longhorn.io', 'default', NS)
        namespace = lab.get('namespace', NS)
        secret = lab.get('secret', binding['secret_name'], NS)
        if (not target or target['metadata']['uid'] != binding['target_uid'] or
                not namespace or namespace['metadata']['uid'] != binding['namespace_uid'] or
                self.backups.target_configuration(target['spec']) != self.backups.target_configuration(binding['fixture_spec']) or
                (require_available and (not target.get('status', {}).get('available') or any(
                    c.get('type') == 'Unavailable' and c.get('status') == 'True'
                    for c in (target.get('status', {}).get('conditions') or [])))) or
                not secret or secret['metadata']['uid'] != binding['secret_uid'] or
                hashlib.sha256(json.dumps(secret.get('data', {}), sort_keys=True).encode()).hexdigest() != binding['secret_digest']):
            raise ValueError('V1 protection re-entry changed its original backup target or credential')
        return {'longhorn_backup_target': binding['fixture_spec']['backupTargetURL'],
                'longhorn_backup_secret': binding['secret_name']}

    def lost_protection_transport(self):
        """Accept only the observed lost tunnel, preserving its original server."""
        binding = self.store.transport_receipt(self.store.receipt())
        if (not binding or Path(binding['socket']).exists() or
                self.store.transport_listener() is not None or
                not self.source.get('pod', binding['pod_name'], NS)):
            raise ValueError('V1 protection re-entry requires its original lost tunnel and owned Pod')
        log = self.store.directory/'transport.log'
        archive = self.store.directory/('transport-before-v1-resume-'+self.row['id']+'.log')
        if log.is_symlink() or not log.is_file() or archive.exists() or archive.is_symlink():
            raise ValueError('V1 protection transport log cannot be safely archived')
        return log, archive

    def expired_client(self, workload):
        """Recognize only the original UID-bound two-hour client completing normally."""
        lab = self.source; saved = workload.journal()
        pod = lab.get('pod', saved['client'], saved['namespace'])
        if not pod or pod['metadata']['uid'] != saved['objects'].get('Pod/'+saved['client']):
            raise ValueError('V1 continuation client identity changed')
        if pod.get('status', {}).get('phase') != 'Succeeded': return False
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        expected = lab.client_pod(saved['namespace'], saved['client'], 'data',
                                  'longhorn-node-'+lab.WORKERS[0].rsplit('.', 1)[1])
        # Admission injects the service-account projection and its mount. Compare
        # the declared data volume/mount, while retaining strict client fields.
        actual = copy.deepcopy(pod['spec'])
        actual['volumes'] = [v for v in actual.get('volumes', []) if v.get('name') == 'data']
        for container in actual.get('containers', []):
            container['volumeMounts'] = [v for v in container.get('volumeMounts', []) if v.get('name') == 'data']
        statuses = pod['status'].get('containerStatuses', [])
        if (saved['client'] != 'writer' or not product.contains(actual, expected['spec']) or
                len(statuses) != 1 or statuses[0].get('restartCount') != 0 or
                statuses[0].get('state', {}).get('terminated', {}).get('exitCode') != 0 or
                statuses[0]['state']['terminated'].get('reason') != 'Completed'):
            raise ValueError('V1 continuation refuses a failed or changed expired client')
        return True

    def expired_source(self, volume, saved):
        if (volume.get('status', {}).get('state') != 'detached' or volume['spec'].get('nodeID') or
                volume['status'].get('currentImage') != volume['spec']['image'] or
                volume['spec'].get('numberOfReplicas') != 3): return False
        replicas = [r for r in self.source.get('replicas.longhorn.io', namespace=NS)['items']
                    if r['spec'].get('volumeName') == saved['volume']]
        nodes = {r['spec'].get('nodeID') for r in replicas}
        return (len(replicas) == len(nodes) == 3 and
                nodes == {'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in self.source.WORKERS} and
                all(not r['metadata'].get('deletionTimestamp') and r['spec'].get('healthyAt') and
                    not r['spec'].get('failedAt') and r['spec'].get('image') == volume['spec']['image'] and
                    r.get('status', {}).get('currentState') == 'stopped' for r in replicas))

    def resume_preflight(self, *, allow_expired_clients=False):
        """Read-only re-entry at the proven V1 protection and Manager boundaries."""
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('missing original V1 protection journal')
        row = json.loads(self.path.read_text())
        manager = row.get('stage') in ('upgrade-starting', 'manager-upgraded-engine-pending')
        restores = {'v1-plain-before', 'v1-encrypted-before'} if manager else {'v1-plain-before'}
        if (set(row) != (self.MANAGER_FIELDS if manager else self.PROTECTION_FIELDS) or
                not re.fullmatch('[a-f0-9]{32}', row.get('id', '')) or
                row['stage'] not in ('source-1.12.0', 'upgrade-starting', 'manager-upgraded-engine-pending') or
                set(row['payloads']) != {'plain', 'encrypted'} or
                set(row['points']) != {'plain-before', 'encrypted-before'} or
                set(row['restores']) != restores or
                any(not row['restores'][key].get('remount') for key in restores)):
            raise ValueError('V1 re-entry requires its original pre-upgrade protection boundary')
        for lab, engine, version, identity in ((self.source, 'v1', upgrade.NEW if manager else upgrade.OLD, row['source_identity']),
                (self.target, 'v2', upgrade.NEW, row['target_identity'])):
            config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
            release = upgrade.release(lab)
            if (config.get('longhorn_data_engines', ['v1']) != [engine] or config.get('longhorn_requests') or
                    str(config.get('longhorn_version')) != version or release.get('status') != 'deployed' or
                    release['app_version'].removeprefix('v') != version):
                raise ValueError('V1 protection re-entry changed its original idle engine, version or identity')
            current = upgrade.retained_identity(lab, {})
            if manager and lab is self.source:
                upgrade.verify_retained(current, identity, row['new_runtime'])
            elif current != identity:
                raise ValueError('V1 protection re-entry changed its original retained identity')
            lab.installation_health()
        if (tuple(self.source.NODES) != SOURCE_NODES or tuple(self.target.NODES) != ('192.168.47.149',) or
                row['source_identity']['cluster_uid'] == row['target_identity']['cluster_uid'] or
                upgrade.runtime_identity(self.source, upgrade.NEW if manager else upgrade.OLD) != row['new_runtime' if manager else 'old_runtime']):
            raise ValueError('V1 protection re-entry changed its original runtime or independent inventories')
        volume_uids = set(); self.expired_clients = []
        for name, workload in self.workloads.items():
            saved = workload.journal(); workload.identities()
            self.source.check_business_graph(saved)
            volume = self.source.get('volumes.longhorn.io', saved['volume'], NS)
            point = row['points'][name+'-before']
            expired = (allow_expired_clients and row['stage'] == 'manager-upgraded-engine-pending' and
                       self.expired_client(workload))
            if expired: self.expired_clients.append(name)
            if (not volume or volume['spec'].get('dataEngine', 'v1') != 'v1' or volume['spec'].get('backingImage') or
                    volume['spec'].get('image') != yaml.safe_load((self.source.DIRECTORY/'config.yml').read_text())['longhorn_image_prefix']+'/longhorn-engine:v'+upgrade.OLD or
                    not (self.expired_source(volume, saved) if expired else self.source.replica_topology(saved, 3)) or point['volume'] != saved['volume'] or
                    point['payload'] != row['payloads'][name] or
                    row['payloads'][name] != row['id']+'-'+name):
                raise ValueError('V1 protection re-entry changed a source, payload or protection point')
            if not expired and row['stage'] == 'manager-upgraded-engine-pending' and (
                    volume.get('status', {}).get('currentImage') != volume['spec']['image'] or
                    volume['status'].get('state') != 'attached' or volume['status'].get('robustness') != 'healthy'):
                raise ValueError('V1 engine action re-entry requires both original attached healthy old engines')
            if not expired: workload.data(row['payloads'][name], read=True)
            volume_uids.add(saved['volume_uid'])
        if ({v['metadata']['uid'] for v in self.source.get('volumes.longhorn.io', namespace=NS)['items']} != volume_uids or
                self.target.get('volumes.longhorn.io', namespace=NS)['items'] or
                any((self.target.DIRECTORY/('longhorn-upgrade-business-'+key+'.json')).exists()
                    for key in row['restores'])):
            raise ValueError('V1 protection re-entry retains a foreign or unfinished volume')
        encryption = self.source.load_helper('longhorn-encryption.py').Probe(self.source, data_engine='v1')
        if ('encrypted' not in self.expired_clients and
                encryption.host_probe(self.source.WORKERS[0], self.workloads['encrypted'].journal(), True) != row['old_encrypted_device']):
            raise ValueError('V1 protection re-entry changed the original encrypted device')
        self.row = row
        if manager:
            self.verify_manager_boundary()
        else:
            self.lost_protection_transport()
        self.bound_configuration(self.store, require_available=manager)
        self.bound_configuration(self.consumer)
        container = self.store.inspect(self.store.receipt())
        if not container or not container['State']['Running']:
            raise ValueError('V1 protection re-entry lost its original backup server')
        if not self.store.live(): raise ValueError('V1 protection re-entry requires its original external store')
        return row

    def verify_manager_boundary(self):
        """Consume completed protection and rollout; never replay the upgrade."""
        row = self.row; source = self.source
        expected = {'longhorn_version': upgrade.NEW, 'longhorn_upgrade_id': 'upgrade-'+row['id'],
                    'longhorn_upgrade_system_backup': row['system']['name']}
        config = yaml.safe_load((source.DIRECTORY/'config.yml').read_text())
        if (row['successful_configuration'] != config or row['attempt']['expected_refusal'] is not None or
                any(config.get(k) != value for k, value in expected.items()) or
                [r.get('expected_refusal') for r in row['refusals']] != [
                    'Longhorn upgrade requires change ID and verified system backup',
                    'Longhorn upgrade system backup is not Ready for the source version']):
            raise ValueError('V1 Manager continuation lacks its original completed rollout and refusals')
        upgrade.verify_retained(upgrade.retained_identity(source, self.workloads), row['retained_before'], row['new_runtime'])
        binding = self.store.transport_receipt(self.store.receipt())
        if (not binding or not Path(binding['socket']).exists() or
                self.store.transport_listener() != binding['listener'] or
                not source.get('pod', binding['pod_name'], NS)):
            raise ValueError('V1 Manager continuation requires its original live backup transport')
        system = source.get('systembackups.longhorn.io', row['system']['name'], NS)
        if (not system or system['metadata']['uid'] != row['system']['uid'] or
                system.get('status', {}).get('state') != 'Ready' or
                system['status'].get('version') != 'v'+upgrade.OLD or
                system['status'].get('managerImage') != row['system']['manager_image']):
            raise ValueError('V1 Manager continuation changed its source-version system protection')
        key = 'backupstore/system-backups/v'+upgrade.OLD+'/'+row['system']['name']+'/system-backup.cfg'
        raw = self.store.mc('cat', 'lh/'+self.store.receipt()['bucket']+'/'+key).encode()
        receipt = row['system']['receipt']
        if (hashlib.sha256(raw).hexdigest() != receipt['config_sha256'] or
                json.loads(raw)['Checksum'] != receipt['zip_sha256'] or receipt['version'] != upgrade.OLD or
                receipt['resource_uids_verified'] != 6):
            raise ValueError('V1 Manager continuation changed its verified system bundle metadata')
        product = source.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(system, product.request_object(row['system']['request']))
        key = row['target_key']
        secret = self.target.get('secret', key['name'], NS)
        original = source.get('secret', 'longhorn-volume-key', NS)
        storage_class = self.target.get('storageclass', key['class']['name'])
        if (not secret or secret['metadata']['uid'] != key['uid'] or
                not original or original['metadata']['uid'] != key['source_uid'] or
                any(hashlib.sha256(json.dumps(obj.get('data', {}), sort_keys=True).encode()).hexdigest() != key['data_sha256']
                    for obj in (original, secret)) or
                not storage_class or storage_class['metadata']['uid'] != key['class_uid'] or
                not product.contains(storage_class, product.storage_class(key['class']))):
            raise ValueError('V1 Manager continuation replaced its original encrypted recovery key or class')
        for name, workload in self.workloads.items():
            point = row['points'][name+'-before']; restored = row['restores']['v1-'+name+'-before']
            for kind, field in (('snapshots.longhorn.io', 'snapshot'), ('backups.longhorn.io', 'backup')):
                request = point['snapshot_request' if field == 'snapshot' else 'request']
                obj = source.get(kind, request['name'], NS)
                if not obj or obj['metadata']['uid'] != point[field+'_uid']:
                    raise ValueError('V1 Manager continuation replaced original protection')
                product.check_request(obj, product.request_object(request))
                if field == 'backup' and (obj.get('status', {}).get('state') != 'Completed' or
                        obj['status'].get('progress') != 100 or obj['status'].get('error') or obj['status'].get('url') != point['url']):
                    raise ValueError('V1 Manager continuation lost its completed original backup')
            if (restored.get('confirmed_sha256') != point['sha256'] or
                    not re.fullmatch('[a-f0-9]{64}', restored.get('independent_write_sha256', '')) or
                    restored.get('volume_uid') in {w.journal()['volume_uid'] for w in self.workloads.values()} or
                    (name == 'encrypted' and restored.get('encrypted_device') != row['old_encrypted_device'])):
                raise ValueError('V1 Manager continuation lacks its independent before restore proof')

    def validate_baseline(self):
        source = self.source; target = self.target
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('V1 upgrade requires recovery of its existing owned journal')
        if tuple(source.NODES) != SOURCE_NODES or tuple(target.NODES) != ('192.168.47.149',):
            raise ValueError('V1 upgrade requires the original source and independent existing target')
        if source.owner()['cluster_uid'] == target.owner()['cluster_uid']:
            raise ValueError('V1 recovery target is not independent')
        for lab, version, engine in ((source, upgrade.OLD, 'v1'), (target, upgrade.NEW, 'v2')):
            release = upgrade.release(lab)
            config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
            if (release['app_version'].removeprefix('v') != version or release.get('status') != 'deployed' or
                    config.get('longhorn_data_engines', ['v1']) != [engine]):
                raise ValueError('V1 upgrade needs actual pinned old V1 source and preserved new V2 target')
            if (any(lab.get(kind, namespace=NS)['items'] for kind in ('volumes.longhorn.io', *self.backups.BACKUP_RESOURCES)) or
                    any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
                    any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in
                        ('longhorn-install-business.json', 'longhorn-backup-store.json', 'longhorn-dr-backup-consumer.json',
                         'longhorn-request-input.json', 'longhorn-upgrade-lifecycle.json'))):
                raise ValueError('V1 upgrade requires clean source/target business and external-store baselines')
            lab.installation_health()

    def protect(self, name, label, configuration):
        """Preserve V1's distinct logical capacity and stored LUKS header."""
        workload = self.workloads[name]; journal = workload.journal(); lab = self.source
        key = name+'-'+label
        point = self.row.setdefault('points', {}).get(key)
        if point is None:
            snapshot = {'kind': 'snapshot', 'name': 'upgrade-snapshot-'+secrets.token_hex(10), 'volume': journal['volume']}
            backup = {'kind': 'backup', 'name': 'upgrade-backup-'+secrets.token_hex(10), 'snapshot': snapshot['name'], 'mode': 'full'}
            point = {'volume': journal['volume'], 'request': backup, 'snapshot_request': snapshot,
                'name': backup['name'], 'payload': self.row['payloads'][name]}
            self.row['points'][key] = point; self.save()
        snapshot, backup = point['snapshot_request'], point['request']
        if (point['volume'] != journal['volume'] or point['payload'] != self.row['payloads'][name] or
                snapshot != {'kind': 'snapshot', 'name': snapshot['name'], 'volume': journal['volume']} or
                backup != {'kind': 'backup', 'name': point['name'], 'snapshot': snapshot['name'], 'mode': 'full'}):
            raise ValueError('V1 protection request differs from its original source')
        for kind, request, field in (('snapshots.longhorn.io', snapshot, 'snapshot'), ('backups.longhorn.io', backup, 'backup')):
            if field+'_uid' in point:
                obj = lab.get(kind, request['name'], NS)
                if not obj or obj['metadata']['uid'] != point[field+'_uid']:
                    raise ValueError('V1 protection request original identity changed')
        lab.run_requests(self.source_pid, [snapshot, backup], backup_configuration=configuration)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        for kind, request, field in (('snapshots.longhorn.io', snapshot, 'snapshot'), ('backups.longhorn.io', backup, 'backup')):
            obj = lab.get(kind, request['name'], NS)
            if not obj: raise ValueError('V1 upgrade protection request disappeared')
            product.check_request(obj, product.request_object(request)); point[field+'_uid'] = obj['metadata']['uid']
        status = lab.get('backups.longhorn.io', backup['name'], NS).get('status', {})
        url = urlsplit(status.get('url', '')); query = parse_qs(url.query)
        if (status.get('state') != 'Completed' or status.get('progress') != 100 or status.get('error') or
                url.scheme != 's3' or url.netloc != urlsplit(configuration['longhorn_backup_target']).netloc or
                query.get('volume') != [journal['volume']] or query.get('backup') != [backup['name']]):
            raise ValueError('V1 upgrade backup completion/source/target differs')
        point['receipt'] = (lab.load_helper('longhorn-encryption.py').backup_receipt(
            lab, self.store, journal, backup['name'], 2*1024**3) if name == 'encrypted' else
            self.store.backup_receipt(journal['volume'], backup['name'], 2*1024**3))
        if point['receipt']['incremental'] is not False: raise ValueError('V1 upgrade protection requires a full backup')
        point['url'] = status['url']; point['sha256'] = workload.data(point['payload'], read=True); self.save()
        return key

    def independent_restore(self, key):
        """A real V1 backup restored by CSI as independent V2, including LUKS."""
        point = self.row['points'][key]; target = self.target
        dr = upgrade.load('longhorn-v2-disaster.py')
        dr.sync_target(target, self.consumer.receipt()['product_binding'])
        discovered = target.wait('v1-upgrade-independent-backup-discovery', 600, lambda:
            dr.discovered_backup(target, point['name'], point['volume'], point['url']))
        point['target_backup_uid'] = discovered['metadata']['uid']; self.save()
        storage_class = self.encrypted_target_class() if key.startswith('encrypted-') else 'longhorn'
        workload = upgrade.Workload(target, 'v1-'+key); self.restores[key] = workload
        # The base cleanup reconstructs Workload from this declared name.
        # Its keys therefore remain the same names as the corresponding receipts.
        self.row.setdefault('restores', {})['v1-'+key] = {'workload': 'v1-'+key}; self.save()
        workload.prepare(storage_class, point)
        row = workload.journal(); volume = target.get('volumes.longhorn.io', row['volume'], NS)
        encrypted = key.startswith('encrypted-')
        if (volume['spec'].get('dataEngine') != 'v2' or bool(volume['spec'].get('encrypted')) != encrypted or
                row['volume_uid'] in {w.journal()['volume_uid'] for w in self.workloads.values()} or
                volume['spec'].get('cloneMode') == 'linked-clone' or
                volume.get('status', {}).get('cloneStatus', {}).get('sourceVolume')):
            raise ValueError('V1 independent recovery lost its target engine, encryption or independent identity')
        target.verify_restore_capacity({'claims': {'data': row}, **row}, 'data', 2*1024**3)
        device = None
        if encrypted:
            device = target.load_helper('longhorn-encryption.py').Probe(target, data_engine='v2').host_probe(
                target.WORKERS[0], row, True)
            if device != self.row['old_encrypted_device']:
                raise ValueError('V1-to-V2 backup recovery changed the original LUKS header or logical/raw capacity')
        workload.data(point['payload'], read=True)
        fresh = point['payload']+'-isolated-target'; workload.data(fresh)
        self.workloads[key.split('-', 1)[0]].data(point['payload'], read=True)
        workload.detach(); workload.mount('recovery-remount', 0); digest = workload.data(fresh, read=True)
        self.row['restores']['v1-'+key].update(volume_uid=row['volume_uid'], confirmed_sha256=point['sha256'],
            independent_write_sha256=digest, remount=True, source_engine='v1', target_engine='v2', encrypted=encrypted)
        if device: self.row['restores']['v1-'+key]['encrypted_device'] = device
        self.save()
        print('LONGHORN_V1_UPGRADE_ISOLATED_RESTORE_VERIFIED '+json.dumps({
            'point': key, **self.row['restores']['v1-'+key]}, sort_keys=True), flush=True)
        workload.cleanup()

    def business(self):
        source = self.source
        if self.path.exists() or self.path.is_symlink():
            self.resume_preflight(allow_expired_clients=True)
            if self.expired_clients:
                for name in self.expired_clients:
                    workload = self.workloads[name]
                    # Keep the completed original Pod and every binding UID.
                    workload.mount('writer-resumed', 0)
                    source.wait('upgrade-expired-client-three-replicas', 600,
                                lambda workload=workload: source.replica_topology(workload.journal(), 3))
                    workload.data(self.row['payloads'][name], read=True)
                self.resume_preflight()
                print('LONGHORN_V1_UPGRADE_EXPIRED_CLIENTS_REATTACHED original_volumes=true original_data=true', flush=True)
            if self.row['stage'] in ('upgrade-starting', 'manager-upgraded-engine-pending'):
                configuration = self.bound_configuration(self.store)
                encryption = source.load_helper('longhorn-encryption.py').Probe(source, data_engine='v1')
                print('LONGHORN_V1_UPGRADE_BEFORE_RESTORES_REVALIDATED count=2 original_points=true original_manager_rollout=true', flush=True)
                self.finish_manager_upgrade(configuration, encryption, self.workloads['encrypted'].journal(), verified=True)
                return
            log, archive = self.lost_protection_transport()
            configuration = self.bound_configuration(self.store, require_available=False)
            self.store.cleanup_transport()
            log.rename(archive)
            self.store.qualify()
            source.run_requests(self.source_pid, [], backup_configuration=configuration)
            self.bound_configuration(self.store)
            print('LONGHORN_V1_UPGRADE_PROTECTION_REENTRY_VERIFIED original_sources=true upgrade_not_started=true', flush=True)
        else:
            configuration = self.start_business()
        encryption = source.load_helper('longhorn-encryption.py').Probe(source, data_engine='v1')
        encrypted_row = self.workloads['encrypted'].journal()
        for name in self.workloads:
            self.independent_restore(self.protect(name, 'before', configuration))
        self.upgrade_and_recover(configuration, encryption, encrypted_row)

    def start_business(self):
        self.validate_baseline()
        source = self.source; target = self.target
        self.row = {'id': secrets.token_hex(16), 'source_identity': upgrade.retained_identity(source, {}),
            'target_identity': upgrade.retained_identity(target, {}), 'payloads': {}, 'points': {}, 'stage': 'source-1.12.0'}
        self.save(); self.row['old_runtime'] = upgrade.runtime_identity(source, upgrade.OLD); self.save()
        self.store.prepare(); self.store.qualify(); configuration = self.store.product_configuration()
        self.consumer.initialize(); self.consumer.qualify(self.target_pid)
        for name, workload in self.workloads.items():
            workload.prepare('longhorn-encrypted' if name == 'encrypted' else 'longhorn')
            row = workload.journal(); volume = source.get('volumes.longhorn.io', row['volume'], NS)
            if volume['spec'].get('dataEngine', 'v1') != 'v1' or volume['spec'].get('backingImage'):
                raise ValueError('V1 upgrade source used another engine or a backing-image dependency')
            payload = self.row['id']+'-'+name; self.row['payloads'][name] = payload
            workload.data(payload); self.save()
        encryption = source.load_helper('longhorn-encryption.py').Probe(source, data_engine='v1')
        encrypted_row = self.workloads['encrypted'].journal()
        self.row['old_encrypted_device'] = encryption.host_probe(source.WORKERS[0], encrypted_row, True); self.save()
        return configuration

    def upgrade_and_recover(self, configuration, encryption, encrypted_row):
        source = self.source; target = self.target
        self.system_backup(configuration)
        changes = {'longhorn_version': upgrade.NEW, 'longhorn_upgrade_id': 'upgrade-'+self.row['id'],
            'longhorn_upgrade_system_backup': self.row['system']['name']}
        self.product(dict(changes, longhorn_upgrade_id=''),
                     refusal='Longhorn upgrade requires change ID and verified system backup')
        self.product(dict(changes, longhorn_upgrade_system_backup='missing-'+self.row['id']),
                     refusal='Longhorn upgrade system backup is not Ready for the source version')
        print('LONGHORN_V1_UPGRADE_PRECHECK_BOUNDARY_VERIFIED old_system_intact=true readable_sources=2 refusals=2', flush=True)
        self.row['retained_before'] = upgrade.retained_identity(source, self.workloads)
        self.row['stage'] = 'upgrade-starting'; self.save(); self.product(changes)
        self.finish_manager_upgrade(configuration, encryption, encrypted_row)

    def finish_manager_upgrade(self, configuration, encryption, encrypted_row, *, verified=False):
        source = self.source; target = self.target
        if upgrade.release(source)['app_version'].removeprefix('v') != upgrade.NEW:
            raise ValueError('V1 Manager upgrade did not converge to 1.12.1')
        if not verified:
            source.installation_health(); self.row['new_runtime'] = upgrade.runtime_identity(source, upgrade.NEW); self.save()
        upgrade.verify_retained(upgrade.retained_identity(source, self.workloads), self.row['retained_before'], self.row['new_runtime'])
        self.row['stage'] = 'manager-upgraded-engine-pending'; self.save()
        for name, workload in self.workloads.items():
            upgrade.upgrade_v1_engine(source, workload, self.row['payloads'][name])
            self.row.setdefault('engines_upgraded', []).append(name); self.save()
        current = encryption.host_probe(source.WORKERS[0], encrypted_row, True)
        if current != self.row['old_encrypted_device']:
            raise ValueError('V1 engine upgrade changed the retained LUKS identity or device capacity')
        self.row['new_encrypted_device'] = current; self.save()
        upgrade.verify_retained(upgrade.retained_identity(source, self.workloads), self.row['retained_before'], self.row['new_runtime'])
        self.row['stage'] = 'upgraded-1.12.1'; self.save()
        for name, workload in self.workloads.items():
            workload.data(self.row['payloads'][name], read=True)
            self.row['payloads'][name] += '-after-upgrade'; workload.data(self.row['payloads'][name]); self.save()
            self.independent_restore(self.protect(name, 'after', configuration))
        self.product({'longhorn_version': upgrade.OLD}, refusal='unsupported Longhorn upgrade or direct downgrade')
        if upgrade.retained_identity(target, {}) != self.row['target_identity']:
            raise ValueError('V1 isolated recovery changed target cluster, disk or original credentials')
        self.row['stage'] = 'business-pass'; self.save()
        print('LONGHORN_V1_UPGRADE_BUSINESS_PASS manager=1.12.1 engines=1.12.1 retained_sources=2', flush=True)
        print('LONGHORN_V1_UPGRADE_RECOVERY_BUSINESS_PASS precheck=unchanged completed_upgrade=downgrade-refused isolated_restores=4', flush=True)

    def cleanup(self):
        # Reuse the identical UID-bound native/cache/remote-prefix cleanup.
        # Its streaming marker is relabelled for this V1-owned scenario only.
        with redirect_stdout(CleanupOutput(sys.stdout)):
            super().cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('configure-old', 'business', 'clean'))
    parser.add_argument('--source-relay-pid', type=int, required=True)
    parser.add_argument('--target-relay-pid', type=int, default=0)
    args = parser.parse_args()
    source, target, backups, shared = fixture(); source.require_lease()
    if args.source_relay_pid <= 0: parser.error('a supervised V1 source relay is required')
    if args.mode == 'configure-old': configure_old(source, args.source_relay_pid)
    else:
        target.require_lease()
        if args.target_relay_pid <= 0: parser.error('an independent target relay is required')
        scenario = V1Upgrade(source, target, backups, shared, args.source_relay_pid, args.target_relay_pid)
        if args.mode == 'clean': scenario.cleanup()
        else:
            scenario.business(); scenario.cleanup()
            print('LONGHORN_UPGRADE_PASS', flush=True)
            print('LONGHORN_UPGRADE_RECOVERY_PASS', flush=True)


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v1-upgrade-failure-', suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V1_UPGRADE_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
