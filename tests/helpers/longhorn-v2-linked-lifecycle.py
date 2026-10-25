#!/usr/bin/env python3
"""LONGHORN-49 dependency-aware eviction and independent backup restoration.

Complements, rather than replaces, the three-replica linked-clone qualifier.
This two-replica graph leaves a third fault domain for real replica eviction.
All configuration changes use config.yml and the supervised kubecli entry.
"""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile


BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
LEGACY_LABEL = 'longhorn.io/legacy-linked-clone'
_spec = importlib.util.spec_from_file_location('linked_base', Path(__file__).with_name('longhorn-v2-linked-clones.py'))
linked = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(linked)


def ancestry(source, child, replicas):
    """Check the completed graph, including healthy same-disk parent replicas."""
    state = child.get('status', {}).get('cloneStatus', {})
    if (source['spec'].get('dataEngine') != 'v2' or child['spec'].get('dataEngine') != 'v2' or
            child['spec'].get('cloneMode') != 'linked-clone' or state.get('state') != 'completed' or
            state.get('sourceVolume') != source['metadata']['name'] or not state.get('snapshot') or
            child['metadata'].get('labels', {}).get(linked.LABEL) != state['snapshot']):
        raise ValueError('linked lifecycle lacks completed native ancestry')
    groups = [[r for r in replicas if r['spec'].get('volumeName') == v['metadata']['name']]
              for v in (source, child)]
    if any(len(group) != 2 or len({r['spec'].get('nodeID') for r in group}) != 2 for group in groups):
        raise ValueError('linked lifecycle requires two replicas in distinct fault domains')
    parents = {r['metadata']['name']: r for r in groups[0]}
    result = {}; used = set()
    for replica in groups[1]:
        spec = replica['spec']; name = spec.get('linkedCloneSrcReplicaName'); parent = parents.get(name)
        if (not parent or name in used or replica['metadata']['uid'] == parent['metadata']['uid'] or
                any(not spec.get(k) or spec[k] != parent['spec'].get(k) for k in ('nodeID', 'diskID'))):
            raise ValueError('linked lifecycle replica lost same-disk healthy parent')
        for obj in (replica, parent):
            if (obj['metadata'].get('deletionTimestamp') or not obj['spec'].get('healthyAt') or
                    obj['spec'].get('failedAt') or obj.get('status', {}).get('currentState') != 'running'):
                raise ValueError('linked lifecycle replica is not healthy')
        used.add(name)
        result[replica['metadata']['name']] = {'uid': replica['metadata']['uid'], 'parent': name,
            'parent_uid': parent['metadata']['uid'], 'node': spec['nodeID'], 'disk': spec['diskID']}
    return result


def migrated_graph(before, after, node):
    """Reject a drained flag without actual replacement of all three replicas."""
    for name in ('source', 'clone', 'nested'):
        old = before[name]; new = after[name]
        evicted = [row for row in old.values() if row['node'] == node]
        if len(evicted) != 1 or any(row['node'] == node for row in new.values()):
            raise ValueError('linked graph did not evacuate the selected fault domain')
        old_uids = {row['uid'] for row in old.values()}
        if (len(new) != 2 or len({row['node'] for row in new.values()}) != 2 or
                len({row['uid'] for row in new.values()} - old_uids) != 1 or
                evicted[0]['uid'] in {row['uid'] for row in new.values()}):
            raise ValueError('linked eviction lacks one real replacement per volume')


def independent_restore(volume, forbidden):
    if (volume['spec'].get('dataEngine') != 'v2' or
            volume['metadata']['uid'] in forbidden or
            volume['spec'].get('cloneMode') == 'linked-clone' or
            volume.get('status', {}).get('cloneStatus', {}).get('sourceVolume') or
            volume['metadata'].get('labels', {}).get(linked.LABEL)):
        raise ValueError('backup restore retains a linked ancestry or source identity')


def legacy_refusals(lab, origin):
    """Consume a REAL 1.12.0 fixture retained by the upgrade scenario.

    This never labels a new volume as legacy. The caller owns the frozen
    upgrade provenance and the legacy fixture's detach/delete cleanup.
    """
    if origin.get('created_version') != '1.12.0' or origin.get('upgraded_version') != '1.12.1':
        raise ValueError('legacy checks require the real supported upgrade boundary')
    volume = lab.get('volumes.longhorn.io', origin['volume_name'], NS)
    source = lab.get('volumes.longhorn.io', origin['source_volume'], NS)
    if (not volume or not source or volume['metadata']['uid'] != origin['volume_uid'] or
            source['metadata']['uid'] != origin['source_volume_uid'] or
            volume['metadata'].get('labels', {}).get(LEGACY_LABEL) != 'true' or
            volume['spec'].get('dataEngine') != 'v2' or volume['spec'].get('cloneMode') != 'linked-clone'):
        raise ValueError('legacy fixture identity, native marker or source differs')
    labels = copy.deepcopy(volume['metadata']['labels'])
    recurring = 'recurring-job.longhorn.io/legacy-guard-'+secrets.token_hex(6)
    labels[recurring] = 'enabled'
    cases = (('/spec/numberOfReplicas', 1 if volume['spec']['numberOfReplicas'] != 1 else 2,
              'cannot change replica count for legacy linked-clone volumes'),
             ('/spec/size', str(int(volume['spec']['size'])+1024**3), 'cannot expand legacy linked-clone volumes'),
             ('/spec/migrationNodeID', 'longhorn-node-'+lab.NODES[-1].rsplit('.', 1)[1],
              'cannot migrate legacy linked-clone volumes'),
             ('/metadata/labels', labels, 'cannot add recurring jobs to legacy linked-clone volumes'))
    for path, value, reason in cases:
        if path == '/spec/migrationNodeID' and volume['spec'].get('migrationNodeID') == value:
            value = 'longhorn-node-'+lab.NODES[0].rsplit('.', 1)[1]
        current = lab.get('volumes.longhorn.io', origin['volume_name'], NS)
        if (current['metadata']['uid'] != origin['volume_uid'] or current['spec'] != volume['spec'] or
                current['metadata'].get('labels') != volume['metadata']['labels']):
            raise ValueError('legacy fixture changed before refusal')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': origin['volume_uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
                 {'op': 'add', 'path': path, 'value': value}]
        result = lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', origin['volume_name'], '--type=json',
                            '--dry-run=server', '--patch-file=/dev/stdin', stdin=json.dumps(patch), check=False)
        if not result.returncode or reason not in result.stdout+result.stderr:
            raise ValueError('legacy native admission returned the wrong result: '+reason)
        current = lab.get('volumes.longhorn.io', origin['volume_name'], NS)
        if (current['metadata']['uid'] != origin['volume_uid'] or current['spec'] != volume['spec'] or
                current['metadata'].get('labels') != volume['metadata']['labels']):
            raise ValueError('legacy dry-run changed its source fixture')
    print('LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true', flush=True)


class Probe(linked.Probe):
    def __init__(self, lab, relay_pid=0):
        super().__init__(lab)
        self.record = lab.DIRECTORY/'longhorn-v2-linked-lifecycle.json'
        self.relay_pid = relay_pid

    def config(self, **changes):
        """Reuse the runner's durable reversible config receipt and supervisor."""
        lab = self.lab; path = lab.DIRECTORY/'config.yml'
        receipt = lab.DIRECTORY/'longhorn-request-input.json'
        if receipt.exists() or receipt.is_symlink() or path.is_symlink():
            raise ValueError('linked configuration requires an unclaimed product input')
        if set(changes) - {'longhorn_storage_classes', 'longhorn_block_disks'}:
            raise ValueError('linked lifecycle configuration exceeded its boundary')
        original = path.read_text(); config = lab.yaml.safe_load(original)
        if config['longhorn_requests']:
            raise ValueError('linked lifecycle cannot replace customer requests')
        config.update(changes)
        desired = lab.yaml.safe_dump(config, sort_keys=False)
        lab.write_private(receipt, {'run_id': lab.owner()['run_id'], 'original': original, 'desired': desired})
        with lab.scoped_cleanup(lab.restore_request_input):
            with tempfile.NamedTemporaryFile('w', dir=lab.DIRECTORY, delete=False) as stream:
                stream.write(desired); stream.flush()
                os.fsync(stream.fileno()); temporary = Path(stream.name)
            temporary.replace(path)
            lab.supervise_core(self.relay_pid, 'longhorn-setup')

    def expected_class(self, name):
        return (self.journal['source_class']['name'] if name == 'source' else
                'longhorn' if name.startswith('restored-') else self.journal['class_name'])

    def bind(self, name):
        lab = self.lab; j = self.journal
        uid = j['objects'].get('PersistentVolumeClaim/'+name)
        if not uid: return None
        claim = self.current('pvc', name, uid, j['namespace'])
        expected = self.expected_class(name)
        if claim and claim['spec'].get('storageClassName') != expected:
            raise ValueError('linked lifecycle claim class changed')
        matches = [p for p in lab.get('pv')['items'] if p.get('spec', {}).get('claimRef', {}).get('uid') == uid]
        if len(matches) > 1: raise ValueError('multiple PVs refer to lifecycle claim')
        if not matches: return None
        pv = matches[0]; spec = pv['spec']; ref = spec['claimRef']
        if (ref.get('namespace') != j['namespace'] or ref.get('name') != name or
                spec.get('storageClassName') != expected or spec.get('persistentVolumeReclaimPolicy') != 'Retain' or
                spec.get('csi', {}).get('driver') != 'driver.longhorn.io'):
            raise ValueError('linked lifecycle PV binding differs')
        row = j['claims'].setdefault(name, {})
        identity = {'pv': pv['metadata']['name'], 'pv_uid': pv['metadata']['uid'], 'volume': spec['csi']['volumeHandle']}
        volume = lab.get('volumes.longhorn.io', identity['volume'], NS)
        if volume:
            if volume['spec'].get('dataEngine') != 'v2': raise ValueError('lifecycle engine changed')
            identity['volume_uid'] = volume['metadata']['uid']
        if any(k in row and row[k] != value for k, value in identity.items()):
            raise ValueError('linked lifecycle binding was replaced')
        row.update(identity); self.save()
        return row if volume else None

    def claim(self, name, parent=None):
        lab = self.lab; j = self.journal
        obj = lab.clone_claim(j['namespace'], name, '2Gi')
        obj['spec']['storageClassName'] = self.expected_class(name)
        if parent: obj['spec']['dataSource']['name'] = parent
        else: del obj['spec']['dataSource']
        self.create(obj)
        def bound():
            row = self.bind(name); claim = lab.get('pvc', name, j['namespace'])
            return row if row and claim and claim.get('status', {}).get('phase') == 'Bound' else None
        row = lab.wait('linked-lifecycle-bound-'+name, 900, bound)
        if parent:
            def completed():
                obj = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
                state = obj.get('status', {}).get('cloneStatus', {}).get('state')
                if state == 'failed': raise ValueError('linked lifecycle clone failed')
                return obj if state == 'completed' else None
            lab.wait('linked-lifecycle-completed-'+name, 900, completed)
        self.client(name, {'source': 0, 'clone': 1, 'nested': 2}[name])
        lab.wait('linked-lifecycle-healthy-'+name, 900, lambda: lab.replica_topology(row, 2))
        return row

    def graph(self):
        lab = self.lab; j = self.journal; result = {}
        replicas = lab.get('replicas.longhorn.io', namespace=NS)['items']
        volumes = {}
        for name in ('source', 'clone', 'nested'):
            row = j['claims'][name]
            obj = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
            if not obj or not lab.replica_topology(row, 2): raise ValueError('linked graph not healthy')
            volumes[name] = obj
            result[name] = {r['metadata']['name']: {'uid': r['metadata']['uid'],
                'node': r['spec']['nodeID'], 'disk': r['spec']['diskID']} for r in replicas
                if r['spec'].get('volumeName') == row['volume']}
        for child, parent in (('clone', 'source'), ('nested', 'clone')):
            ancestry(volumes[parent], volumes[child], replicas)
        return result

    def restore_disks(self):
        j = self.journal
        if not j.get('maintenance'): return
        if self.relay_pid <= 0: raise ValueError('disk flag recovery requires the supervised relay')
        # Native guard requires cancellation with scheduling still disabled,
        # then a second reconciliation may enable scheduling again.
        disks = copy.deepcopy(j['original_disks']); host = j['maintenance']['host']
        for disk in disks[host]: disk.update(allow_scheduling=False, eviction_requested=False)
        self.config(longhorn_block_disks=disks)
        self.config(longhorn_block_disks=j['original_disks'])
        del j['maintenance']; self.save()

    def evacuate(self):
        lab = self.lab; j = self.journal; before = self.graph()
        common = set.intersection(*({row['node'] for row in before[name].values()}
                                    for name in ('source', 'clone', 'nested')))
        if not common: raise ValueError('linked graph lacks a shared replica fault domain')
        node = sorted(common)[0]; host = next(ip for ip in lab.NODES if node == 'longhorn-node-'+ip.rsplit('.', 1)[1])
        disks = copy.deepcopy(j['original_disks'])
        if len(disks[host]) != 1: raise ValueError('lifecycle only owns the single admitted disk per host')
        j['maintenance'] = {'host': host, 'node': node}; self.save()
        disks[host][0].update(allow_scheduling=False, eviction_requested=True)
        self.config(longhorn_block_disks=disks)
        after = self.graph(); migrated_graph(before, after, node)
        self.restore_disks()
        for name in ('source', 'clone', 'nested'): self.io(name, j['payloads'][name], read=True)
        j['eviction'] = {'before': before, 'after': after, 'evacuated_node': node,
                         'replicas_per_volume': 2, 'product_disk_drain_completed': True}
        self.save()
        print('LONGHORN_V2_LINKED_DEPENDENCY_REBUILD_VERIFIED '+json.dumps(j['eviction'], sort_keys=True), flush=True)

    def backup(self, name, store, configuration):
        lab = self.lab; j = self.journal; row = j['claims'][name]
        snapshot = 'linked-backup-'+secrets.token_hex(10); backup = 'linked-'+secrets.token_hex(10)
        request = {'kind': 'snapshot', 'name': snapshot, 'volume': row['volume']}
        backup_request = {'kind': 'backup', 'name': backup, 'snapshot': snapshot, 'mode': 'full'}
        owned = {'source': name, 'volume': row['volume'], 'volume_uid': row['volume_uid'],
                 'snapshot': snapshot, 'backup': backup, 'request': request, 'backup_request': backup_request}
        j.setdefault('backups', {})[name] = owned; self.save()
        lab.run_requests(self.relay_pid, [request, backup_request], backup_configuration=configuration)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        for kind, key, desired in (('snapshots.longhorn.io', 'snapshot', request),
                                   ('backups.longhorn.io', 'backup', backup_request)):
            obj = lab.get(kind, owned[key], NS)
            if not obj: raise ValueError('product backup request disappeared')
            product.check_request(obj, product.request_object(desired))
            owned[key+'_uid'] = obj['metadata']['uid']; self.save()
        owned['receipt'] = store.backup_receipt(row['volume'], backup, 2*1024**3)
        if owned['receipt']['incremental'] is not False: raise ValueError('linked full backup reported incremental')
        owned['sha256'] = self.io(name, j['payloads'][name], read=True); self.save()

    def restore(self, name):
        """Use the established CSI bak:// API, with this graph's own journal.

        The shared verifier fixes its journal filename and source namespace;
        retaining this small adaptation avoids two competing cleanup owners.
        """
        lab = self.lab; j = self.journal; backup = j['backups'][name]
        content_name = 'linked-restore-'+secrets.token_hex(10); snap_name = content_name
        entry = {'snapshot': snap_name, 'content': content_name}
        j.setdefault('restores', {})[name] = entry; self.save()
        snapshot = self.create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
            'metadata': {'name': snap_name, 'namespace': j['namespace']},
            'spec': {'volumeSnapshotClassName': 'longhorn-snapshot', 'source': {'volumeSnapshotContentName': content_name}}})
        content = self.create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshotContent',
            'metadata': {'name': content_name}, 'spec': {'driver': 'driver.longhorn.io', 'deletionPolicy': 'Retain',
            'volumeSnapshotClassName': 'longhorn-snapshot', 'volumeSnapshotRef': {'name': snap_name,
            'namespace': j['namespace'], 'uid': snapshot['metadata']['uid']},
            'source': {'snapshotHandle': 'bak://'+backup['volume']+'/'+backup['backup']}}})
        def ready():
            obj = self.current('volumesnapshots.snapshot.storage.k8s.io', snap_name, snapshot['metadata']['uid'], j['namespace'])
            current = self.current('volumesnapshotcontents.snapshot.storage.k8s.io', content_name,
                                   content['metadata']['uid'])
            expected_ref = {'name': snap_name, 'namespace': j['namespace'], 'uid': snapshot['metadata']['uid']}
            if (not current or current['spec'].get('volumeSnapshotRef') != expected_ref or
                    current['spec'].get('source') != {'snapshotHandle': 'bak://'+backup['volume']+'/'+backup['backup']}):
                raise ValueError('linked backup snapshot content binding changed')
            status = (obj or {}).get('status', {})
            if status.get('error'):
                # v8.6.0 syncUnreadySnapshot retries its informer cache miss.
                if status['error'].get('message') == 'VolumeSnapshotContent is missing': return False
                raise ValueError('linked backup snapshot returned an error')
            return status.get('readyToUse') and status.get('boundVolumeSnapshotContentName') == content_name
        lab.wait('linked-backup-snapshot-ready-'+name, 600, ready)
        claim_name = 'restored-'+name
        self.create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
            'metadata': {'name': claim_name, 'namespace': j['namespace']}, 'spec': {'storageClassName': 'longhorn',
            'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '2Gi'}},
            'dataSource': {'kind': 'VolumeSnapshot', 'apiGroup': 'snapshot.storage.k8s.io', 'name': snap_name}}})
        def bound():
            row = self.bind(claim_name); obj = lab.get('pvc', claim_name, j['namespace'])
            return row if row and obj and obj.get('status', {}).get('phase') == 'Bound' else None
        row = lab.wait('linked-backup-restore-bound-'+name, 900, bound)
        self.client(claim_name, 1)
        lab.wait('linked-backup-restore-healthy-'+name, 900, lambda: lab.replica_topology(row, 3))
        volume = self.current('volumes.longhorn.io', row['volume'], row['volume_uid'], NS)
        lab.verify_restore_capacity(j, claim_name, backup['receipt']['size'])
        independent_restore(volume, {j['claims'][key]['volume_uid'] for key in ('source', 'clone', 'nested')})
        if (int(volume['spec']['size']) != backup['receipt']['size'] or
                any(r['spec'].get('linkedCloneSrcReplicaName') for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                    if r['spec'].get('volumeName') == row['volume'])):
            raise ValueError('restored volume retains a parent replica or has wrong capacity')
        self.io(claim_name, j['payloads'][name], read=True)
        changed = j['payloads'][name]+'-restored'; entry['sha256'] = self.io(claim_name, changed)
        for source in ('source', 'clone', 'nested'): self.io(source, j['payloads'][source], read=True)
        entry['payload'] = changed; self.save()

    def cleanup_backup_objects(self):
        lab = self.lab; j = self.journal
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        for row in j.get('backups', {}).values():
            for kind, key, request in (('backups.longhorn.io', 'backup', row['backup_request']),
                                       ('snapshots.longhorn.io', 'snapshot', row['request'])):
                obj = lab.get(kind, row[key], NS)
                if not obj: continue
                product.check_request(obj, product.request_object(request))
                if row.get(key+'_uid', obj['metadata']['uid']) != obj['metadata']['uid']:
                    raise ValueError('linked backup resource identity changed')
                row[key+'_uid'] = obj['metadata']['uid']; self.save(); lab.remove(obj)
                lab.wait('linked-backup-clean-'+row[key], 600, lambda kind=kind, key=key, row=row:
                    self.current(kind, row[key], row[key+'_uid'], NS) is None)
            volumes = [v for v in lab.get('backupvolumes.longhorn.io', namespace=NS)['items']
                       if v['spec'].get('volumeName') == row['volume']]
            if len(volumes) > 1: raise ValueError('multiple linked backup volume objects')
            if volumes:
                obj = volumes[0]
                if obj['spec'].get('backupTargetName') != 'default': raise ValueError('foreign linked backup target')
                if any(b['metadata'].get('labels', {}).get('backup-volume') == row['volume']
                       for b in lab.get('backups.longhorn.io', namespace=NS)['items']):
                    raise ValueError('backup volume retains a consumer')
                identity = {'name': obj['metadata']['name'], 'uid': obj['metadata']['uid']}
                if row.get('backup_volume', identity) != identity: raise ValueError('linked backup volume replaced')
                row['backup_volume'] = identity; self.save(); lab.remove(obj)
                lab.wait('linked-backup-volume-clean', 600, lambda:
                    self.current('backupvolumes.longhorn.io', identity['name'], identity['uid'], NS) is None)

    def cleanup(self):
        lab = self.lab; lab.restore_request_input()
        if self.record.is_symlink(): raise ValueError('symlink linked lifecycle receipt')
        if not self.record.exists():
            lab.backup_store().cleanup(); return
        self.journal = json.loads(self.record.read_text()); j = self.journal
        if j['run_id'] != lab.owner()['run_id'] or not j['namespace'].startswith('longhorn-linked-'):
            raise ValueError('foreign linked lifecycle receipt')
        self.restore_disks()
        ns = self.current('namespace', j['namespace'], j.get('namespace_uid'))
        if ns:
            lab.check_business_graph(j)
            for key in j['objects']:
                if key.startswith('Pod/'): self.delete_pod(key.split('/', 1)[1])
            for name in ('restored-clone', 'restored-nested'): self.retire(name)
            for row in j.get('restores', {}).values():
                for kind, key, object_kind, namespace in (
                        ('volumesnapshots.snapshot.storage.k8s.io', 'snapshot', 'VolumeSnapshot', j['namespace']),
                        ('volumesnapshotcontents.snapshot.storage.k8s.io', 'content', 'VolumeSnapshotContent', '')):
                    uid = j['objects'].get(object_kind+'/'+row[key])
                    obj = lab.get(kind, row[key], namespace)
                    if obj and not uid: raise ValueError('unbound linked restore object requires diagnosis')
                    if obj:
                        self.current(kind, row[key], uid, namespace); lab.remove(obj)
                        lab.wait('linked-restore-object-clean', 180, lambda kind=kind, row=row, key=key, uid=uid, namespace=namespace:
                            self.current(kind, row[key], uid, namespace) is None)
        if ns:
            # Native leaf-to-root volume retirement also removes its snapshots;
            # detached engines cannot service standalone snapshot deletion.
            for name in ('nested', 'clone', 'source'): self.retire(name)
        self.cleanup_backup_objects()
        if ns:
            # Keep the receipt until both classes and the backup store are gone.
            lab.check_business_graph(j); lab.remove(ns)
            lab.wait('linked-lifecycle-namespace-clean', 180, lambda: lab.get('namespace', j['namespace']) is None)
        if (lab.get('volumes.longhorn.io', namespace=NS)['items'] or
                any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items'])):
            raise ValueError('linked lifecycle cleanup retains native volumes or PVs')
        for config, uid_key in ((j['class_config'], 'class_uid'), (j['source_class'], 'source_class_uid')):
            sc = lab.get('storageclass', config['name'])
            if not sc: continue
            product = lab.load_helper('../../roles/longhorn/files/longhorn.py'); product.require_owned(sc)
            desired = product.storage_class(config)
            if (sc['metadata']['uid'] != j.get(uid_key) or any(sc.get(k) != desired.get(k) for k in
                    ('parameters', 'provisioner', 'reclaimPolicy', 'allowVolumeExpansion', 'volumeBindingMode')) or
                    any(p['spec'].get('storageClassName') == config['name']
                        for kind in ('pv', 'pvc') for p in lab.get(kind, namespace='*' if kind == 'pvc' else '')['items'])):
                raise ValueError('linked lifecycle storage class changed or retains consumers')
            lab.remove(sc)
            lab.wait('linked-lifecycle-class-clean', 60, lambda config=config: lab.get('storageclass', config['name']) is None)
        lab.backup_store().cleanup()
        self.record.unlink()
        print('LONGHORN_V2_LINKED_LIFECYCLE_SCOPED_CLEAN_PASS', flush=True)

    def business(self, relay_pid):
        lab = self.lab; self.relay_pid = relay_pid
        if self.record.exists() or self.record.is_symlink(): raise ValueError('linked lifecycle requires scoped recovery')
        config = lab.yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
        if any(lab.get(kind, namespace=NS)['items'] for kind in ('volumes.longhorn.io', 'backups.longhorn.io', 'backupvolumes.longhorn.io')):
            raise ValueError('linked lifecycle requires empty business baseline')
        state = lab.owner(); template = copy.deepcopy(next(c for c in config['longhorn_storage_classes'] if c['name'] == 'longhorn'))
        source = dict(template, name='longhorn-linked-source-'+secrets.token_hex(6), replicas=2,
                      default=False, reclaim_policy='Retain', clone_mode='full-copy')
        child = dict(source, name='longhorn-linked-child-'+secrets.token_hex(6), clone_mode='linked-clone')
        self.journal = {'run_id': state['run_id'], 'namespace': 'longhorn-linked-'+secrets.token_hex(8),
            'objects': {}, 'claims': {}, 'defaults': [], 'class_name': child['name'], 'class_config': child,
            'source_class': source, 'original_disks': copy.deepcopy(config['longhorn_block_disks'])}
        for host, disks in self.journal['original_disks'].items():
            node = lab.get('nodes.longhorn.io', 'longhorn-node-'+host.rsplit('.', 1)[1], NS)
            if len(disks) != 1 or not node: raise ValueError('lifecycle requires one admitted native disk per node')
            native = node.get('spec', {}).get('disks', {}).get(disks[0]['name'])
            if not native or native.get('allowScheduling') is not True or native.get('evictionRequested') is not False:
                raise ValueError('lifecycle disk baseline has an existing maintenance operation')
            disks[0].update(allow_scheduling=True, eviction_requested=False)
        self.save()
        with lab.scoped_cleanup(self.cleanup):
            self.config(longhorn_storage_classes=config['longhorn_storage_classes']+[source, child])
            self.journal['source_class_uid'] = lab.get('storageclass', source['name'])['metadata']['uid']
            self.journal['class_uid'] = lab.get('storageclass', child['name'])['metadata']['uid']; self.save()
            obj = json.loads(lab.kubectl('create', 'namespace', self.journal['namespace'], '-o', 'json').stdout)
            self.journal['namespace_uid'] = obj['metadata']['uid']; self.save()
            self.journal['defaults'] = lab.wait('linked-lifecycle-defaults', 60, lambda: lab.namespace_defaults(self.journal['namespace'])); self.save()
            self.journal['payloads'] = {}; self.save()
            for name, parent in (('source', None), ('clone', 'source'), ('nested', 'clone')):
                self.claim(name, parent)
                if parent: self.io(name, self.journal['payloads'][parent], read=True)
                payload = state['run_id']+'-'+name; self.io(name, payload)
                self.journal['payloads'][name] = payload; self.save()
            self.evacuate()
            store = lab.backup_store(); store.prepare(); store.qualify()
            configuration = store.product_configuration()
            for name in ('clone', 'nested'): self.backup(name, store, configuration)
            for name in ('clone', 'nested'): self.restore(name)
            for name in ('nested', 'clone', 'source'):
                self.delete_pod(name); self.retire(name)
            for name in ('clone', 'nested'):
                claim = 'restored-'+name; self.delete_pod(claim)
                pod = self.client(claim, 2, '-remount')
                self.io(pod, self.journal['restores'][name]['payload'], read=True)
            print('LONGHORN_V2_LINKED_INDEPENDENT_RESTORE_VERIFIED '+json.dumps({
                'backups': self.journal['backups'], 'restores': self.journal['restores'],
                'source_graph_retired_before_cross_node_readback': True}, sort_keys=True), flush=True)
        print('LONGHORN_V2_LINKED_LIFECYCLE_PASS', flush=True)


def interrupted(signum, frame):
    """Unwind product supervision and scoped recovery on either stop signal."""
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, required=True)
    args = parser.parse_args()
    if args.relay_pid <= 0: parser.error('a live supervised relay PID is required')
    spec = importlib.util.spec_from_file_location('linked_backups', BASE/'tests/helpers/longhorn-v2-backups.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    install, lab, backups = module.fixture(); lab.require_lease()
    probe = Probe(lab, args.relay_pid)
    if args.mode == 'business':
        module.baseline(install, lab, backups)
        probe.business(args.relay_pid)
    else: probe.cleanup()


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-linked-lifecycle-failure-', suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_LINKED_LIFECYCLE_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
