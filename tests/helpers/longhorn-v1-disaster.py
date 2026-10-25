#!/usr/bin/env python3
"""LONGHORN-18: V1 standby, planned fencing, failback and renewed standby.

The target is an explicitly configured single-node V1 filesystem fixture beside
its preserved V2 installation. No site HA, power fencing or sudden-loss claim.
Native DR declarations follow Longhorn 1.12.1 manager 8320238242e8.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import uuid
from urllib.parse import parse_qs, urlsplit

NS = 'longhorn-system'


def load(name):
    spec = importlib.util.spec_from_file_location('v1_dr_' + name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


native = load('longhorn-v2-disaster.py')
handoff = load('longhorn-v2-dr-failover.py')


def configuration(holder):
    binding = holder.receipt()['product_binding']
    return {'longhorn_backup_target': binding['fixture_spec']['backupTargetURL'],
            'longhorn_backup_secret': binding['secret_name']}


def standby_manifest(lab, name, url, replicas=1):
    result = native.standby_manifest(lab, name, url)
    result['spec'].update(dataEngine='v1', numberOfReplicas=replicas)
    return result


def standby_restored(lab, name, uid, backup, disk_uuid):
    volume = native.original(lab, 'volumes.longhorn.io', name, uid)
    spec, status = volume['spec'], volume.get('status', {})
    if (spec.get('dataEngine', 'v1') != 'v1' or spec.get('numberOfReplicas') != 1 or
            spec.get('Standby') is not True or spec.get('frontend') or not spec.get('fromBackup')):
        raise ValueError('V1 DR standby declaration changed')
    if any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == name for p in lab.get('pv')['items']):
        raise ValueError('V1 DR standby acquired a PV before activation')
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items'] if e['spec'].get('volumeName') == name]
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items'] if r['spec'].get('volumeName') == name]
    if len(engines) != 1 or len(replicas) != 1:
        return None
    engine, replica = engines[0], replicas[0]
    if (engine['spec'].get('dataEngine', 'v1') != 'v1' or replica['spec'].get('dataEngine', 'v1') != 'v1' or
            replica['spec'].get('nodeID') not in ('', None, 'longhorn-node-149') or
            replica['spec'].get('diskID') not in ('', None, disk_uuid)):
        raise ValueError('V1 DR standby escaped its approved filesystem disk')
    if engine['status'].get('endpoint') or (engine['spec'].get('frontend') and not engine['spec'].get('disableFrontend')):
        raise ValueError('V1 DR standby exposes a business frontend')
    restore = engine['status'].get('restoreStatus') or {}
    if any(r.get('error') for r in restore.values()):
        raise ValueError('V1 DR standby restore error')
    if (replica['spec'].get('nodeID') != 'longhorn-node-149' or replica['spec'].get('diskID') != disk_uuid or
            status.get('isStandby') is not True or status.get('state') != 'attached' or
            status.get('robustness') != 'healthy' or status.get('lastBackup') != backup or not status.get('restoreRequired') or
            engine['status'].get('currentState') != 'running' or engine['spec'].get('requestedBackupRestore') != backup or
            engine['status'].get('lastRestoredBackup') != backup or any(r.get('isRestoring') for r in restore.values()) or
            replica['status'].get('currentState') != 'running' or not replica['spec'].get('healthyAt') or replica['spec'].get('failedAt')):
        return None
    return {'volume_uid': uid, 'engine_uid': engine['metadata']['uid'], 'replica_uid': replica['metadata']['uid'],
            'disk_uuid': disk_uuid, 'last_restored_backup': backup, 'observed_at': datetime.now(timezone.utc).isoformat()}


def fenced(lab, journal):
    volume = native.original(lab, 'volumes.longhorn.io', journal['volume'], journal['volume_uid'])
    if (volume['spec'].get('dataEngine', 'v1') != 'v1' or volume['spec'].get('frontend') != 'blockdev' or
            volume['spec'].get('disableFrontend') is not True or volume['spec'].get('nodeID') or
            volume['status'].get('currentNodeID') or volume['status'].get('state') != 'detached'):
        raise ValueError('V1 DR source is not detached with its frontend disabled')
    if (any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == journal['volume'] for p in lab.get('pv')['items']) or
            lab.get('pvc', 'data', journal['namespace']) or lab.get('pod', namespace=journal['namespace'])['items']):
        raise ValueError('V1 DR source still has a writer, claim or PV')
    if any(v['spec'].get('volume') == journal['volume'] and v['spec'].get('attachmentTickets')
           for v in lab.get('volumeattachments.longhorn.io', namespace=NS)['items']):
        raise ValueError('V1 DR source still has attachment tickets')
    stopped = {}
    for engine in lab.get('engines.longhorn.io', namespace=NS)['items']:
        if engine['spec'].get('volumeName') != journal['volume']:
            continue
        if (engine['spec'].get('dataEngine', 'v1') != 'v1' or engine['spec'].get('desireState') != 'stopped' or
                engine['status'].get('currentState') != 'stopped' or engine['status'].get('endpoint')):
            raise ValueError('V1 DR source engine is not stopped without an endpoint')
        stopped[engine['metadata']['name']] = engine['metadata']['uid']
    if not stopped:
        raise ValueError('V1 DR fencing lacks its retained stopped engine identity')
    return {'cluster_uid': lab.owner()['cluster_uid'], 'volume_uid': volume['metadata']['uid'],
            'spec_sha256': hashlib.sha256(json.dumps(volume['spec'], sort_keys=True).encode()).hexdigest(),
            'stopped_engines': stopped}


def fence(lab, journal):
    lab.check_business_graph(journal)
    for kind, name, uid in (('pod', 'writer', journal['objects']['Pod/writer']),
                            ('pvc', 'data', journal['objects']['PersistentVolumeClaim/data']),
                            ('pv', journal['pv'], journal['pv_uid'])):
        namespace = '' if kind == 'pv' else journal['namespace']
        current = lab.get(kind, name, namespace)
        if current:
            if current['metadata']['uid'] != uid:
                raise ValueError('V1 DR source consumer identity changed before fencing')
            if kind == 'pv' and (current['spec'].get('persistentVolumeReclaimPolicy') != 'Retain' or
                    current['spec'].get('claimRef', {}).get('uid') != journal['objects']['PersistentVolumeClaim/data'] or
                    current['spec'].get('csi', {}).get('volumeHandle') != journal['volume']):
                raise ValueError('V1 DR source retained PV binding changed')
            lab.remove(current)
        lab.wait('v1-dr-fence-' + kind, 180, lambda kind=kind, name=name, namespace=namespace: lab.get(kind, name, namespace) is None)
        if kind == 'pod':
            lab.wait('v1-dr-source-detached', 300, lambda:
                native.original(lab, 'volumes.longhorn.io', journal['volume'], journal['volume_uid'])['status'].get('state') == 'detached')
    current = native.original(lab, 'volumes.longhorn.io', journal['volume'], journal['volume_uid'])
    if current['spec'].get('disableFrontend') is not True:
        native.guarded_patch(lab, 'volumes.longhorn.io', current, {'disableFrontend': True})
    return fenced(lab, journal)


def activate(source, journal, target, name, uid, point, disk_uuid, proof, initial_url):
    if not proof or proof != fenced(source, journal):
        raise ValueError('V1 DR activation requires current source fencing proof')
    if not standby_restored(target, name, uid, point['name'], disk_uuid):
        raise ValueError('V1 DR activation requires completed approved backup restore')
    volume = native.original(target, 'volumes.longhorn.io', name, uid)
    if volume['spec'].get('fromBackup') != initial_url or volume['spec'].get('size') != str(2 * 1024**3):
        raise ValueError('V1 DR standby original restore declaration changed')
    native.guarded_patch(target, 'volumes.longhorn.io', volume, {'Standby': False, 'frontend': 'blockdev'})


def activated(lab, name, uid):
    volume = native.original(lab, 'volumes.longhorn.io', name, uid)
    spec, status = volume['spec'], volume.get('status', {})
    if spec.get('dataEngine', 'v1') != 'v1' or spec.get('Standby') is not False or spec.get('frontend') != 'blockdev':
        raise ValueError('V1 DR activated declaration changed')
    return (status.get('restoreInitiated') is True and not status.get('isStandby') and not status.get('restoreRequired') and
            status.get('state') == 'detached' and not status.get('currentNodeID'))


class Standby(native.Standby):
    """Reuse engine-independent object journals, cache retirement and store leases."""
    def __init__(self, source, target, backups, shared, target_fixture):
        super().__init__(source, target, backups, shared)
        self.path = source.DIRECTORY / 'longhorn-v1-dr.json'
        self.target_fixture = target_fixture

    def source_workload(self):
        lab = self.source
        namespace = 'longhorn-business-dr-' + uuid.uuid4().hex[:16]
        ns = self.create('source', {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})
        journal = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
                   'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
        lab.write_private(self.business_path, journal)
        journal['defaults'] = lab.wait('v1-dr-source-defaults', 60, lambda: lab.namespace_defaults(namespace))
        lab.write_private(self.business_path, journal)
        self.create('source', {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'data', 'namespace': namespace},
            'spec': {'storageClassName': 'longhorn', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '2Gi'}}}}, journal)
        lab.wait('v1-dr-source-claim', 600, lambda: (lab.get('pvc', 'data', namespace) or {}).get('status', {}).get('phase') == 'Bound')
        lab.bind_business_volume(journal, self.business_path)
        volume = native.original(lab, 'volumes.longhorn.io', journal['volume'], journal['volume_uid'])
        if volume['spec'].get('dataEngine', 'v1') != 'v1' or volume['spec'].get('numberOfReplicas') != 3:
            raise ValueError('V1 DR original source is not a three-replica filesystem volume')
        node = 'longhorn-node-' + lab.WORKERS[0].rsplit('.', 1)[1]
        created = self.create('source', lab.client_pod(namespace, 'writer', 'data', node), journal)
        def ready():
            pod = lab.get('pod', 'writer', namespace)
            if not pod or pod['metadata']['uid'] != created['metadata']['uid']:
                raise ValueError('V1 DR source writer changed')
            return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', []))
        lab.wait('v1-dr-source-writer', 600, ready)
        lab.wait('v1-dr-source-three-replicas', 600, lambda: lab.healthy_replicas(journal['volume']))
        return journal

    def create_standby(self, point):
        self.discover(point)
        name = 'dr-v1-standby-' + uuid.uuid4().hex[:20]
        manifest = standby_manifest(self.target, name, point['url'])
        row = self.receipt()
        manifest['metadata']['labels'] = {'kubeauto.io/longhorn-dr-run': row['id']}
        row['volumes'][name] = {'uid': None, 'from_backup': point['url']}
        self.save(row)
        created = self.create('target', manifest)
        row = self.receipt(); row['volumes'][name]['uid'] = created['metadata']['uid']; self.save(row)
        return name, created['metadata']['uid']

    def restored(self, name, uid, point):
        disk = self.target_fixture.ready()['v1_disk_uuid']
        result = self.target.wait('v1-dr-standby-restored-' + point['name'], 900,
            lambda: standby_restored(self.target, name, uid, point['name'], disk))
        row = self.receipt()
        previous = [p['restored'] for p in row['points'].values() if p.get('restored', {}).get('volume_uid') == uid]
        for old in previous:
            if any(old[key] != result[key] for key in ('volume_uid', 'engine_uid', 'replica_uid', 'disk_uuid')):
                raise ValueError('V1 DR standby identity changed between restored points')
        row['points'][point['name']]['restored'] = result; self.save(row)
        print('LONGHORN_V1_DR_STANDBY_RESTORED ' + json.dumps(result, sort_keys=True), flush=True)
        return result


class Disaster(handoff.Failover):
    def __init__(self, source, target, backups, shared, source_pid, target_pid):
        self.source_pid, self.target_pid = source_pid, target_pid
        self.target_module = load('longhorn-v1-recovery-target.py')
        self.target_fixture = self.target_module.Target(target, load('longhorn-v2-dr-target-install.py'))
        dr = Standby(source, target, backups, shared, self.target_fixture)
        parent = SimpleNamespace(original=native.original, guarded_patch=native.guarded_patch,
            sync_target=native.sync_target, discovered_backup=native.discovered_backup)
        super().__init__(dr, parent, None)
        self.upgrade = load('longhorn-upgrade-lifecycle.py')

    def point(self, side, journal, path, payload, mode, pid, holder):
        lab = self.source if side == 'source' else self.target
        name = 'dr-v1-' + mode + '-' + uuid.uuid4().hex[:18]
        snapshot = 'dr-v1-snap-' + uuid.uuid4().hex[:18]
        snapshot_request = {'kind': 'snapshot', 'name': snapshot, 'volume': journal['volume']}
        request = {'kind': 'backup', 'name': name, 'snapshot': snapshot, 'mode': mode}
        journal.setdefault('native_snapshots', {})[snapshot] = {'request': snapshot_request}
        journal.setdefault('backups', {})[name] = {'request': request}
        lab.write_private(path, journal)
        lab.run_requests(pid, [snapshot_request, request], backup_configuration=configuration(holder))
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        for kind, object_name, expected, row in (('snapshots.longhorn.io', snapshot, snapshot_request, journal['native_snapshots'][snapshot]),
                ('backups.longhorn.io', name, request, journal['backups'][name])):
            obj = lab.get(kind, object_name, NS)
            if not obj: raise ValueError('V1 DR product request outcome is unknown')
            product.check_request(obj, product.request_object(expected))
            row['uid'] = obj['metadata']['uid']
        lab.write_private(path, journal)
        backup = lab.get('backups.longhorn.io', name, NS); status = backup.get('status', {})
        url = status.get('url', ''); parts = urlsplit(url); query = parse_qs(parts.query)
        if (status.get('state') != 'Completed' or status.get('progress') != 100 or status.get('error') or
                parts.scheme != 's3' or parts.netloc != urlsplit(configuration(holder)['longhorn_backup_target']).netloc or
                query.get('backup') != [name] or query.get('volume') != [journal['volume']]):
            raise ValueError('V1 DR backup did not complete for its approved volume/store')
        receipt = self.store.backup_receipt(journal['volume'], name, 2 * 1024**3)
        if receipt['incremental'] is not (mode == 'incremental') or receipt['newly_uploaded_bytes'] <= 0:
            raise ValueError('V1 DR backup did not prove its changed full/incremental data')
        journal['backups'][name]['object_receipt'] = receipt; lab.write_private(path, journal)
        point = {'name': name, 'source_volume': journal['volume'], 'url': url, side + '_backup_uid': backup['metadata']['uid'],
                 'receipt': receipt, 'sha256': self.data(lab, journal, payload, True), 'observed_at': datetime.now(timezone.utc).isoformat()}
        row = self.dr.receipt(); row['points'][name] = point; self.dr.save(row)
        print('LONGHORN_V1_DR_BACKUP_POINT ' + json.dumps(point, sort_keys=True), flush=True)
        return point

    def mount(self, role, name, uid, replicas, node):
        lab = self.context_lab(role); path = self.context_path(role)
        if path.exists() or path.is_symlink(): raise ValueError('V1 DR context requires owned recovery first')
        volume = native.original(lab, 'volumes.longhorn.io', name, uid)
        if volume['spec'].get('Standby') or volume['spec'].get('dataEngine', 'v1') != 'v1':
            raise ValueError('V1 DR standby cannot bind business PV/PVC')
        row = self.dr.receipt()
        if role in row.get('contexts', {}): raise ValueError('V1 DR context already journaled')
        namespace = 'longhorn-business-dr-' + role + '-' + uuid.uuid4().hex[:16]
        pv = 'dr-v1-' + role + '-' + uuid.uuid4().hex[:18]
        row.setdefault('contexts', {})[role] = {'namespace': namespace, 'pv': pv, 'volume': name, 'volume_uid': uid}; self.dr.save(row)
        self.dr.create('target' if role == 'target' else 'source', {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})
        journal = self.recover_context(role)
        storage_class = self.target_module.STORAGE if role == 'target' else 'longhorn'
        journal['storage_class'] = storage_class; self.save_context(role, journal)
        claim, _ = handoff.static_manifests(namespace, 'data', pv, name, '', replicas)
        claim['spec']['storageClassName'] = storage_class
        created = self.create_bound(role, journal, claim)
        _, persistent = handoff.static_manifests(namespace, 'data', pv, name, created['metadata']['uid'], replicas)
        persistent['spec']['storageClassName'] = storage_class
        self.create_bound(role, journal, persistent)
        def bound():
            current = lab.get('pvc', 'data', namespace)
            if not current or current['metadata']['uid'] != created['metadata']['uid']: raise ValueError('V1 DR static claim changed')
            return current.get('status', {}).get('phase') == 'Bound'
        lab.wait('v1-dr-' + role + '-bound', 300, bound)
        lab.bind_business_volume(journal, path)
        if journal.get('volume') != name or journal.get('volume_uid') != uid: raise ValueError('V1 DR static claim bound another volume')
        pod = self.create_bound(role, journal, lab.client_pod(namespace, 'writer', 'data', node))
        def ready():
            current = lab.get('pod', 'writer', namespace)
            if not current or current['metadata']['uid'] != pod['metadata']['uid']: raise ValueError('V1 DR restored writer changed')
            return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in current.get('status', {}).get('conditions', []))
        lab.wait('v1-dr-' + role + '-writer', 600, ready)
        lab.wait('v1-dr-' + role + '-replicas', 600, lambda: self.replica_ready(role, journal, replicas))
        return journal

    def replica_ready(self, role, journal, count):
        lab = self.context_lab(role)
        volume = native.original(lab, 'volumes.longhorn.io', journal['volume'], journal['volume_uid'])
        if volume['spec'].get('dataEngine', 'v1') != 'v1' or volume['spec'].get('numberOfReplicas') != count:
            raise ValueError('V1 DR restored replica declaration changed')
        if role == 'failback': return lab.replica_topology(journal, count)
        disk = self.target_fixture.ready()['v1_disk_uuid']
        replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items'] if r['spec'].get('volumeName') == journal['volume']]
        if len(replicas) != 1: return None
        replica = replicas[0]
        if (replica['spec'].get('dataEngine', 'v1') != 'v1' or replica['spec'].get('nodeID') != 'longhorn-node-149' or
                replica['spec'].get('diskID') != disk): raise ValueError('V1 DR activated replica escaped its filesystem')
        return volume['status'].get('robustness') == 'healthy' and replica['status'].get('currentState') == 'running' and bool(
            replica['spec'].get('healthyAt')) and not replica['spec'].get('failedAt')

    def restore_return(self, point):
        self.discover_return(point)
        name = 'dr-v1-failback-' + uuid.uuid4().hex[:18]
        row = self.dr.receipt(); row['failback_volume'] = {'name': name, 'uid': None, 'from_backup': point['url']}; self.dr.save(row)
        manifest = standby_manifest(self.source, name, point['url'], replicas=3)
        manifest['spec'].update(Standby=False, frontend='blockdev')
        manifest['metadata']['labels'] = {'kubeauto.io/longhorn-dr-run': row['id']}
        created = self.dr.create('source', manifest)
        row = self.dr.receipt(); row['failback_volume']['uid'] = created['metadata']['uid']; self.dr.save(row)
        def restored():
            current = native.original(self.source, 'volumes.longhorn.io', name, created['metadata']['uid'])
            if current['spec'].get('fromBackup') != point['url'] or current['spec'].get('numberOfReplicas') != 3:
                raise ValueError('V1 DR return restore declaration changed')
            return activated(self.source, name, created['metadata']['uid'])
        self.source.wait('v1-dr-failback-restored', 900, restored)
        return name, created['metadata']['uid']

    def cleanup(self):
        if not self.dr.path.exists() and not self.dr.path.is_symlink():
            return
        self.dr.validate_intents(); row = self.dr.receipt()
        self.source.restore_request_input(); self.target.restore_request_input()
        for role in ('failback', 'target'): self.retire_context(role)
        for name, record in row['volumes'].items():
            volume = self.target.get('volumes.longhorn.io', name, NS)
            if not volume: continue
            if (not record['uid'] or volume['metadata']['uid'] != record['uid'] or volume['spec'].get('fromBackup') != record['from_backup'] or
                    volume['metadata'].get('labels', {}).get('kubeauto.io/longhorn-dr-run') != row['id'] or
                    volume['spec'].get('dataEngine', 'v1') != 'v1' or
                    any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == name for p in self.target.get('pv')['items'])):
                raise ValueError('V1 DR cleanup refuses foreign target or surviving consumer')
            activation = row.get('activation', {})
            if volume['spec'].get('Standby') is not True and not (
                    activation.get('volume') == name and activation.get('volume_uid') == record['uid'] and
                    volume['spec'].get('Standby') is False and volume['spec'].get('frontend') == 'blockdev'):
                raise ValueError('V1 DR target activated outside its recorded handoff')
            self.target.remove(volume)
            self.target.wait('v1-dr-target-volume-retired', 600, lambda name=name: self.target.get('volumes.longhorn.io', name, NS) is None)
        record = row.get('failback_volume')
        if record:
            current = self.source.get('volumes.longhorn.io', record['name'], NS)
            if current:
                if (not record['uid'] or current['metadata']['uid'] != record['uid'] or current['spec'].get('fromBackup') != record['from_backup'] or
                        current['metadata'].get('labels', {}).get('kubeauto.io/longhorn-dr-run') != row['id'] or
                        any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == record['name'] for p in self.source.get('pv')['items'])):
                    raise ValueError('V1 DR cleanup failback identity or consumer changed')
                self.source.remove(current)
                self.source.wait('v1-dr-return-volume-retired', 600, lambda: self.source.get('volumes.longhorn.io', record['name'], NS) is None)
        for side, holder, lab in (('target', self.consumer, self.target), ('source', self.store, self.source)):
            if holder.record.exists() or holder.record.is_symlink():
                binding = holder.receipt().get('product_binding')
                if binding: self.dr.retain_caches(lab, binding, side)
        # The common binder owns the original default journal. Secondary
        # contexts retire their own request receipts only after API absence.
        for role in ('failback', 'target'):
            journal = self.recover_context(role)
            if journal is None: continue
            lab = self.context_lab(role)
            for field, kind in (('native_snapshots', 'snapshots.longhorn.io'), ('backups', 'backups.longhorn.io')):
                for name, request in journal.get(field, {}).items():
                    def absent(kind=kind, name=name, request=request, lab=lab):
                        current = lab.get(kind, name, NS)
                        if current and (not request.get('uid') or current['metadata']['uid'] != request['uid']):
                            raise ValueError('V1 DR secondary request was replaced during cleanup')
                        return current is None
                    lab.wait('v1-dr-secondary-request-retired-' + name, 180, absent)
                journal.pop(field, None)
            self.save_context(role, journal)
            lab.cleanup_business(self.context_path(role))
        baseline = self.dr.receipt().get('source_baseline')
        if row.get('target_fixture_intent'):
            if self.target_fixture.path.exists() or self.target_fixture.path.is_symlink():
                fixture = self.target_fixture.read()
                if row.get('target_fixture_id', fixture['host']['id']) != fixture['host']['id']:
                    raise ValueError('V1 DR target filesystem fixture changed')
            self.target_fixture.clean(self.target_pid)
        self.dr.cleanup()
        if baseline and self.upgrade.retained_identity(self.source, {}) != baseline:
            raise ValueError('V1 DR source original shared identity changed')
        print('LONGHORN_V1_DR_SCOPED_CLEAN_PASS source_original=true target_raw_original=true', flush=True)

    def qualify(self):
        gate = load('longhorn-v1-upgrade-prepare.py')
        gate.healthy(self.source, 'v1', '1.12.1')
        for lab in (self.source, self.target): gate.empty(lab, self.dr.backups)
        if self.source.owner()['cluster_uid'] == self.target.owner()['cluster_uid']:
            raise ValueError('V1 DR requires independent cluster identities')
        if self.target_fixture.path.exists() or self.target_fixture.path.is_symlink():
            raise ValueError('V1 DR target filesystem has a different owning scenario')
        self.dr.initialize()
        row = self.dr.receipt(); row['source_baseline'] = self.upgrade.retained_identity(self.source, {}); self.dr.save(row)
        row = self.dr.receipt(); row['target_fixture_intent'] = True; self.dr.save(row)
        self.target_fixture.prepare(self.target_pid)
        row = self.dr.receipt(); row['target_fixture_id'] = self.target_fixture.read()['host']['id']; self.dr.save(row)
        self.store.prepare(); self.consumer.initialize(); self.consumer.qualify(self.target_pid); self.store.qualify()
        self.store.product_configuration()  # One original product binding, reused below.
        journal = self.dr.source_workload(); payload = self.source.owner()['run_id']
        self.data(self.source, journal, payload, False)
        first = self.point('source', journal, self.dr.business_path, payload, 'full', self.source_pid, self.store)
        name, uid = self.dr.create_standby(first); self.dr.restored(name, uid, first)
        payload += '-second-confirmed-point'; self.data(self.source, journal, payload, False)
        last = self.point('source', journal, self.dr.business_path, payload, 'incremental', self.source_pid, self.store)
        self.dr.discover(last); self.dr.restored(name, uid, last)
        disk = self.target_fixture.ready()['v1_disk_uuid']
        try: activate(self.source, journal, self.target, name, uid, last, disk, None, first['url'])
        except ValueError as error:
            if str(error) != 'V1 DR activation requires current source fencing proof': raise
        else: raise ValueError('V1 DR workflow accepted activation without fencing')
        print('LONGHORN_V1_DR_UNFENCED_ACTIVATION_REFUSED native_api_not_called=true', flush=True)
        unbacked = payload + '-unbacked-tail'; tail_digest = self.data(self.source, journal, unbacked, False)
        proof = fence(self.source, journal)
        row = self.dr.receipt(); row['activation'] = {'volume': name, 'volume_uid': uid, 'approved_backup': last['name'], 'fence': proof}
        row['rpo'] = {'approved_backup': last['name'], 'restored_sha256': last['sha256'], 'unbacked_tail_sha256': tail_digest}
        self.dr.save(row)
        activate(self.source, journal, self.target, name, uid, last, disk, proof, first['url'])
        self.target.wait('v1-dr-activated-detached', 900, lambda: activated(self.target, name, uid))
        restored = self.mount('target', name, uid, 1, 'longhorn-node-149')
        digest = self.data(self.target, restored, payload, True)
        recovery_payload = payload + '-recovery-write'; self.data(self.target, restored, recovery_payload, False)
        if proof != fenced(self.source, journal): raise ValueError('V1 DR original source fence changed')
        print('LONGHORN_V1_DR_ACTIVATED_BUSINESS ' + json.dumps({'volume_uid': uid, 'backup': last['name'],
            'restored_sha256': digest, 'unbacked_tail_excluded': digest != tail_digest, 'source_fenced': True}, sort_keys=True), flush=True)
        point = self.point('target', restored, self.context_path('target'), recovery_payload, 'full', self.target_pid, self.consumer)
        back_name, back_uid = self.restore_return(point)
        reverse = fence(self.target, restored)
        if back_uid in (journal['volume_uid'], uid): raise ValueError('V1 DR failback reused a previous native identity')
        row = self.dr.receipt(); row['return_fence'] = reverse; self.dr.save(row)
        if proof != fenced(self.source, journal): raise ValueError('V1 DR obsolete source fence changed')
        returned = self.mount('failback', back_name, back_uid, 3, 'longhorn-node-' + self.source.WORKERS[-1].rsplit('.', 1)[1])
        self.data(self.source, returned, recovery_payload, True)
        final_payload = recovery_payload + '-return-write'; final_digest = self.data(self.source, returned, final_payload, False)
        if reverse != fenced(self.target, restored): raise ValueError('V1 DR recovery-site fence changed')
        final_point = self.point('source', returned, self.context_path('failback'), final_payload, 'full', self.source_pid, self.store)
        renewed_name, renewed_uid = self.dr.create_standby(final_point)
        if renewed_uid == uid: raise ValueError('V1 DR renewed standby reused an activated volume identity')
        self.dr.restored(renewed_name, renewed_uid, final_point)
        if proof != fenced(self.source, journal) or reverse != fenced(self.target, restored):
            raise ValueError('V1 DR an obsolete writer became available during standby renewal')
        self.data(self.source, returned, final_payload, True)
        row = self.dr.receipt(); row['verified'] = {'failback_uid': back_uid, 'final_sha256': final_digest,
            'renewed_standby_uid': renewed_uid, 'renewed_backup': final_point['name'], 'old_writers_fenced': True}; self.dr.save(row)
        print('LONGHORN_V1_DR_FAILBACK_RENEWED ' + json.dumps(row['verified'], sort_keys=True), flush=True)


def capture_failure(source, target, error):
    """Preserve first-failure state before owned recovery can remove its objects."""
    work = Path(tempfile.mkdtemp(prefix='kubeauto-longhorn-v1-dr-failure-', dir='/var/tmp'))
    source.record_failure(error)
    commands = {
        'pods': ('get', 'pods', '-A', '-o', 'wide'),
        'events': ('get', 'events', '-A', '--sort-by=.lastTimestamp'),
        'pod-descriptions': ('describe', 'pods', '-n', NS),
        'manager-logs': ('logs', '-n', NS, '-l', 'app=longhorn-manager', '--all-containers=true',
                         '--tail=200', '--max-log-requests=10'),
        'native': ('get', 'volumes.longhorn.io,engines.longhorn.io,replicas.longhorn.io,'
                   'volumeattachments.longhorn.io,backups.longhorn.io', '-n', NS, '-o', 'json'),
    }
    for side, lab in (('source', source), ('target', target)):
        for name, args in commands.items():
            try:
                result = lab.kubectl('--request-timeout=5s', *args, check=False, timeout=15)
                record = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
            except BaseException as diagnostic:
                record = {'diagnostic_error': type(diagnostic).__name__}
            source.write_private(work / (side + '-' + name + '.json'), record)
        # Exact scenario journals and secret-free rendered input; never copy the
        # target fixture's original config credential material or kubeconfig.
        for name in ('longhorn-v1-dr.json', 'longhorn-install-business.json',
                     'longhorn-dr-failback-business.json', 'yml/longhorn/input.json'):
            path = lab.DIRECTORY / name
            if path.is_file() and not path.is_symlink():
                try:
                    source.write_private(work / (side + '-' + path.name), json.loads(path.read_text()))
                except (OSError, ValueError):
                    pass
    print('LONGHORN_V1_DR_PRIVATE_FAILURE_STATE path=' + str(work), file=sys.stderr, flush=True)


class Output:
    """Give reused engine-independent lifecycle events the owning V1 prefix."""
    def __init__(self, stream): self.stream = stream
    def write(self, text):
        self.stream.write(text.replace('LONGHORN_V2_DR_', 'LONGHORN_V1_DR_'))
        return len(text)
    def flush(self): self.stream.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('qualify', 'cleanup'))
    parser.add_argument('--source-relay-pid', type=int, required=True)
    parser.add_argument('--target-relay-pid', type=int, required=True)
    args = parser.parse_args()
    if min(args.source_relay_pid, args.target_relay_pid) <= 0: parser.error('both supervised relays are required')
    source, target, backups, shared = load('longhorn-v1-upgrade-lifecycle.py').fixture()
    source.require_lease(); target.require_lease()
    probe = Disaster(source, target, backups, shared, args.source_relay_pid, args.target_relay_pid)
    with redirect_stdout(Output(sys.stdout)):
        if args.mode == 'cleanup': probe.cleanup()
        else:
            with source.scoped_cleanup(probe.cleanup):
                try:
                    probe.qualify()
                except BaseException as error:
                    try:
                        capture_failure(source, target, error)
                    except BaseException as diagnostic:
                        print('LONGHORN_V1_DR_DIAGNOSTIC_FAILURE reason=' + type(diagnostic).__name__, file=sys.stderr, flush=True)
                    raise
            print('LONGHORN_V1_DR_BUSINESS_PASS', flush=True)


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        traceback.print_exc(); sys.exit(1)
