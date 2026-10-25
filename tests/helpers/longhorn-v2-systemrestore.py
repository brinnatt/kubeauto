#!/usr/bin/env python3
"""Absent-volume SystemRestore qualification on an owned V2 AIO baseline.

A separate restore ledger retains original UIDs and data boundaries. This is
single-cluster reconstruction, never evidence of an independent DR site.
"""
import argparse
import base64
import copy
from datetime import datetime
import hashlib
import gzip
import json
from pathlib import Path
import posixpath
import re
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit

BASE = Path('/usr/local/kubeauto')
NAMESPACE = 'longhorn-system'
RESTORED = 'longhorn.io/last-system-restored'
RESTORED_AT = 'longhorn.io/last-system-restored-at'
RESTORED_BACKUP = 'longhorn.io/last-system-restored-backup'
RESTORE_ANNOTATIONS = {RESTORED, RESTORED_AT, RESTORED_BACKUP,
                       'longhorn.io/last-skipped-system-restored',
                       'longhorn.io/last-skipped-system-restored-at'}


def system_url(target, name):
    url = urlsplit(target)
    # GetSystemBackupURL uses filepath.Join, which cleans the URI's trailing
    # slash before the manager writes last-system-restored annotations.
    path = posixpath.join(url.path, 'backupstore/system-backups/v1.12.1', name)
    return urlunsplit(url._replace(path=path))


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('restore timestamp lacks a time zone')
    return parsed


def validate_reconstruction(original, current, request, backup_url, bundle_url, *, data_engine='v2', replicas=3):
    """Names alone cannot authorize handing reconstructed objects to cleanup."""
    if data_engine not in ('v1', 'v2') or type(replicas) is not int or replicas < 1:
        raise ValueError('unsupported expected restore engine or replica count')
    if request.get('status', {}).get('state') != 'Completed':
        raise ValueError('SystemRestore has not completed')
    start = timestamp(request['metadata']['creationTimestamp'])
    for kind in ('Volume', 'PersistentVolume', 'PersistentVolumeClaim'):
        before, after = original[kind], current[kind]
        old, new = before['metadata'], after['metadata']
        annotations = new.get('annotations', {})
        if (new['name'] != old['name'] or new.get('namespace', '') != old.get('namespace', '') or
                not new.get('uid') or new['uid'] == old['uid'] or
                timestamp(new['creationTimestamp']) < start or
                annotations.get(RESTORED) != bundle_url or not annotations.get(RESTORED_AT)):
            raise ValueError('restored '+kind+' lacks new identity or rollout provenance')
        timestamp(annotations[RESTORED_AT])
    if len({obj['metadata']['annotations'][RESTORED_AT] for obj in current.values()}) != 1:
        raise ValueError('restored resources came from different rollout times')
    volume, pv, pvc = (current[k] for k in ('Volume', 'PersistentVolume', 'PersistentVolumeClaim'))
    spec = volume['spec']; ref = pv['spec'].get('claimRef', {})
    if (spec.get('dataEngine') != data_engine or int(spec['size']) != 2*1024**3 or
            spec.get('numberOfReplicas') != replicas or spec.get('fromBackup') != backup_url or
            volume['metadata']['annotations'].get(RESTORED_BACKUP) != backup_url or
            volume.get('status', {}).get('restoreRequired') or
            any(c.get('type') == 'Restore' and c.get('status') == 'True'
                for c in (volume.get('status', {}).get('conditions') or [])) or
            pv['spec'].get('persistentVolumeReclaimPolicy') != 'Retain' or
            pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
            pv['spec']['csi'].get('volumeHandle') != volume['metadata']['name'] or
            ref.get('uid') != pvc['metadata']['uid'] or ref.get('name') != pvc['metadata']['name'] or
            ref.get('namespace') != pvc['metadata']['namespace'] or
            pvc['spec'].get('volumeName') != pv['metadata']['name'] or
            pvc.get('status', {}).get('phase') != 'Bound'):
        raise ValueError('restored data source, capacity or PV/PVC binding differs')
    for obj in (pv, pvc):
        old = original[obj['kind']]
        if any(obj['spec'].get(key) != old['spec'].get(key)
               for key in ('storageClassName', 'accessModes', 'volumeMode')):
            raise ValueError('restored class or access mode differs')
    if (pv['spec'].get('capacity') != original['PersistentVolume']['spec'].get('capacity') or
            pvc['spec'].get('resources') != original['PersistentVolumeClaim']['spec'].get('resources') or
            pvc.get('status', {}).get('capacity') != original['PersistentVolumeClaim'].get('status', {}).get('capacity') or
            not pv['spec'].get('capacity') or not pvc.get('status', {}).get('capacity')):
        raise ValueError('restored PV/PVC capacity differs')
    return {kind: current[kind]['metadata']['uid'] for kind in current}


def validate_rollout(request, job, pods):
    """Bind a successful rollout through Pod -> Job -> original request UID."""
    expected = 'longhorn-system-rollout-' + request['metadata']['name']
    if (not job or job['metadata']['name'] != expected or
            not any(o.get('kind') == 'SystemRestore' and o.get('name') == request['metadata']['name'] and
                    o.get('uid') == request['metadata']['uid'] and o.get('controller') is True
                    for o in job['metadata'].get('ownerReferences', [])) or
            not job.get('status', {}).get('succeeded') or
            not any(c.get('type') == 'Complete' and c.get('status') == 'True'
                    for c in (job.get('status', {}).get('conditions') or []))):
        raise ValueError('SystemRestore rollout job lacks original ownership or completion')
    if not pods:
        raise ValueError('SystemRestore rollout has no bound Pod')
    for pod in pods:
        if (not any(o.get('kind') == 'Job' and o.get('name') == expected and
                    o.get('uid') == job['metadata']['uid'] and o.get('controller') is True
                    for o in pod['metadata'].get('ownerReferences', [])) or
                pod.get('status', {}).get('phase') != 'Succeeded' or
                not pod['status'].get('containerStatuses') or
                any(c.get('state', {}).get('terminated', {}).get('exitCode') != 0
                    for c in pod['status']['containerStatuses'])):
            raise ValueError('SystemRestore rollout Pod failed or changed ownership')
    return {'job_uid': job['metadata']['uid'], 'pod_uids': sorted(p['metadata']['uid'] for p in pods)}


def reconstruction_ready(original, current, request, backup_url, bundle_url, *, data_engine='v2', replicas=3):
    """Rollout creation completion precedes asynchronous Kubernetes binding."""
    if any(obj is None for obj in current.values()):
        return None
    volume, pv, pvc = (current[k] for k in ('Volume', 'PersistentVolume', 'PersistentVolumeClaim'))
    if (pvc.get('status', {}).get('phase') != 'Bound' or not pv.get('spec', {}).get('claimRef') or
            volume.get('status', {}).get('restoreRequired') or
            any(c.get('type') == 'Restore' and c.get('status') == 'True'
                for c in (volume.get('status', {}).get('conditions') or []))):
        return None
    validate_reconstruction(original, current, request, backup_url, bundle_url, data_engine=data_engine, replicas=replicas)
    return current


def resource_signature(obj):
    """Ignore runtime status and only the five documented rollout annotations."""
    body = {key: value for key, value in obj.items() if key not in ('metadata', 'status')}
    if obj['kind'] == 'Secret':
        body.pop('data', None)
        body['data_sha256'] = hashlib.sha256(json.dumps(obj.get('data', {}), sort_keys=True).encode()).hexdigest()
    meta = obj['metadata']
    return {'uid': meta['uid'], 'labels': meta.get('labels', {}),
            'annotations': {k:v for k,v in meta.get('annotations', {}).items() if k not in RESTORE_ANNOTATIONS},
            'body': body}


def helm_release_profile(obj):
    """Decode only Longhorn's owned Helm storage, retaining declared content.

    Helm 3 stores gzip/base64 release JSON. Upgrade changes revision, status,
    deployment times and hook last_run; chart, values, manifests and hook
    declarations remain the same when the product reasserts its target profile.
    Application Secrets never enter this path.
    """
    meta = obj['metadata']; labels = meta.get('labels', {})
    if (obj.get('kind') != 'Secret' or obj.get('type') != 'helm.sh/release.v1' or
            meta.get('namespace') != NAMESPACE or not meta.get('uid') or
            labels.get('owner') != 'helm' or labels.get('name') != 'longhorn'):
        raise ValueError('unowned Longhorn Helm storage')
    raw = base64.b64decode(base64.b64decode(obj['data']['release'], validate=True), validate=True)
    release = json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b\x08') else raw)
    version = release.get('version'); info = release.get('info', {})
    if (type(version) is not int or version < 1 or labels.get('version') != str(version) or
            meta['name'] != 'sh.helm.release.v1.longhorn.v'+str(version) or
            release.get('name') != 'longhorn' or release.get('namespace') != NAMESPACE or
            release.get('chart', {}).get('metadata', {}).get('name') != 'longhorn' or
            info.get('status') not in ('deployed', 'superseded') or labels.get('status') != info['status']):
        raise ValueError('Longhorn Helm release identity or status differs')
    hooks = [{k:v for k,v in hook.items() if k != 'last_run'} for hook in release.get('hooks', [])]
    profile = {key:release[key] for key in ('name', 'namespace', 'chart', 'config', 'manifest')}
    profile.update(hooks=hooks, notes=info.get('notes', ''))
    return info['status'], hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


class Probe:
    def __init__(self, lab, *, data_engine='v2', replicas=3):
        self.lab = lab
        self.data_engine, self.replicas = data_engine, replicas
        self.system = lab.load_helper('longhorn-v2-systembackup.py')
        self.bundle = self.system.Probe(lab, data_engine=data_engine)
        self.store = lab.backup_store()
        self.record = lab.DIRECTORY/'longhorn-systemrestore-qualification.json'
        self.business_record = lab.DIRECTORY/'longhorn-systemrestore-business.json'

    def save(self, row):
        self.lab.write_private(self.record, row)

    def journal(self):
        if self.record.is_symlink() or self.business_record.is_symlink():
            raise ValueError('symlink system restore journal')
        row = json.loads(self.record.read_text())
        if any(row.get(key) != value for key, value in self.bundle.identity().items()):
            raise ValueError('SystemRestore fixture cluster, target or store changed')
        return row

    def shared(self):
        objects = {}; helm_profiles = []
        for kind in ('deployments', 'daemonsets', 'services', 'configmaps', 'secrets',
                     'serviceaccounts', 'roles', 'rolebindings', 'settings.longhorn.io'):
            for obj in self.lab.get(kind, namespace=NAMESPACE)['items']:
                if (obj['kind'] == 'Secret' and
                        obj['metadata']['name'].startswith('sh.helm.release.v1.longhorn.v')):
                    status, profile = helm_release_profile(obj)
                    if status == 'deployed': helm_profiles.append(profile)
                    continue
                key = obj['kind']+'/'+NAMESPACE+'/'+obj['metadata']['name']
                objects[key] = resource_signature(obj)
        for kind in ('storageclasses', 'clusterroles', 'clusterrolebindings', 'customresourcedefinitions'):
            for obj in self.lab.get(kind)['items']:
                name = obj['metadata']['name']
                if ('longhorn' in name or obj['metadata'].get('labels', {}).get('kubeauto.io/component') == 'longhorn'):
                    objects[obj['kind']+'//'+name] = resource_signature(obj)
        if len(helm_profiles) != 1:
            raise ValueError('SystemRestore requires one deployed Longhorn Helm release')
        return {'objects': objects, 'snapshot': self.lab.shared_snapshot_identity(),
                'helm_profile_sha256': helm_profiles[0]}

    def graph(self, journal, row):
        lab = self.lab; ns = lab.get('namespace', journal['namespace'])
        if not ns or ns['metadata']['uid'] != journal['namespace_uid']:
            raise ValueError('SystemRestore business namespace replaced')
        allowed = set(journal['objects'].values()) | set(journal.get('defaults', []))
        historical = {o['metadata']['uid'] for o in row.get('original', {}).values()}
        resources = lab.kubectl('api-resources', '--verbs=list', '--namespaced=true', '-o', 'name').stdout.split()
        if not resources:
            raise ValueError('SystemRestore namespace discovery is empty')
        for resource in sorted(set(resources)):
            for obj in lab.get(resource, namespace=journal['namespace'])['items']:
                if resource == 'pods.metrics.k8s.io' and obj.get('apiVersion') == 'metrics.k8s.io/v1beta1' and obj.get('kind') == 'PodMetrics':
                    continue
                uid = obj.get('metadata', {}).get('uid')
                if uid and uid in allowed:
                    continue
                ref = obj.get('involvedObject', obj.get('regarding', {}))
                # Original UIDs authorize only historical Events, never a live
                # object. They remain separate from defaults/current objects.
                if (resource in ('events', 'events.events.k8s.io') and obj.get('kind') == 'Event' and uid and
                        ref.get('uid') in allowed | historical | {journal['namespace_uid']}):
                    continue
                raise ValueError('foreign resource in SystemRestore namespace: '+resource)

    def current(self, kind, name, namespace, uid):
        obj = self.lab.get(kind, name, namespace)
        if obj and (not uid or obj['metadata']['uid'] != uid):
            raise ValueError('SystemRestore fixture object replaced or unconfirmed: '+kind)
        return obj

    def remove(self, obj):
        if obj:
            kind, meta = obj['kind'], obj['metadata']
            current = self.current(kind, meta['name'], meta.get('namespace', ''), meta['uid'])
            if current is None:
                return
            self.lab.remove(current)
            self.lab.wait('systemrestore-remove-'+kind, 600, lambda:
                          self.current(kind, meta['name'], meta.get('namespace', ''), meta['uid']) is None)

    def client(self, journal, name, worker):
        lab = self.lab
        pod = lab.create_business_object(journal, self.business_record,
            lab.client_pod(journal['namespace'], name, 'data', 'longhorn-node-'+worker.rsplit('.', 1)[1]))
        def ready():
            obj = self.current('pod', name, journal['namespace'], pod['metadata']['uid'])
            return obj if obj and any(c.get('type') == 'Ready' and c.get('status') == 'True'
                                      for c in (obj.get('status', {}).get('conditions') or [])) else None
        return lab.wait('systemrestore-client-'+name, 900, ready)

    def data(self, journal, client, payload, *, read_only=False):
        digest = self.lab.kubectl('-n', journal['namespace'], 'exec', client, '--', 'python3', '-c',
                                 self.lab.payload_program(payload, read_only=read_only)).stdout.strip()
        if digest != hashlib.sha256(payload.encode()*262144).hexdigest():
            raise ValueError('SystemRestore confirmed data boundary differs')
        return digest

    def create_source(self, row):
        lab = self.lab; namespace = 'longhorn-business-restore-'+secrets.token_hex(8)
        if lab.get('namespace', namespace):
            raise ValueError('SystemRestore source namespace already exists')
        row['namespace_intent'] = namespace; self.save(row)
        ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(
            {'apiVersion':'v1', 'kind':'Namespace', 'metadata':{'name':namespace}})).stdout)
        journal = {'namespace':namespace, 'namespace_uid':ns['metadata']['uid'], 'objects':{},
                   'storage_class':'longhorn', 'reclaim_policy':'Retain'}
        lab.write_private(self.business_record, journal)
        journal['defaults'] = lab.wait('systemrestore-namespace-defaults', 60, lambda:lab.namespace_defaults(namespace))
        lab.write_private(self.business_record, journal)
        lab.create_business_object(journal, self.business_record, {'apiVersion':'v1', 'kind':'PersistentVolumeClaim',
            'metadata':{'name':'data', 'namespace':namespace}, 'spec':{'accessModes':['ReadWriteOnce'],
            'storageClassName':'longhorn', 'resources':{'requests':{'storage':'2Gi'}}}})
        lab.wait('systemrestore-source-bound', 600, lambda:
            (o if o and o.get('status', {}).get('phase') == 'Bound' else None)
            if (o := lab.get('pvc', 'data', namespace)) else None)
        lab.bind_business_volume(journal, self.business_record)
        if not journal.get('volume_uid'):
            raise ValueError('SystemRestore source binding not confirmed')
        self.client(journal, 'writer', lab.WORKERS[0])
        lab.wait('systemrestore-source-replicas', 600, lambda:lab.healthy_replicas(journal['volume']))
        return journal

    def protect(self, row, journal, pid, configuration):
        lab = self.lab; product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        payload = lab.owner()['run_id']+'-systemrestore-point'
        digest = self.data(journal, 'writer', payload)
        snapshot = {'kind':'snapshot', 'name':'restore-snapshot-'+secrets.token_hex(12), 'volume':journal['volume']}
        backup = {'kind':'backup', 'name':'restore-backup-'+secrets.token_hex(12), 'snapshot':snapshot['name'], 'mode':'full'}
        row.update(snapshot_request=snapshot, backup_request=backup, payload=payload, backup_digest=digest)
        self.save(row)
        lab.run_requests(pid, [snapshot, backup], backup_configuration=configuration)
        for kind, request, field in (('snapshots.longhorn.io', snapshot, 'snapshot'), ('backups.longhorn.io', backup, 'backup')):
            obj = lab.get(kind, request['name'], NAMESPACE)
            product.check_request(obj, product.request_object(request))
            row[field] = obj
        if row['backup'].get('status', {}).get('state') != 'Completed' or not row['backup']['status'].get('url'):
            raise ValueError('SystemRestore source backup not completed')
        row['backup_url'] = row['backup']['status']['url']
        row['backup_receipt'] = self.store.backup_receipt(journal['volume'], backup['name'], 2*1024**3)
        if row['backup_receipt']['incremental'] is not False:
            raise ValueError('SystemRestore requires its full starting backup')
        def last_backup():
            volume = self.current('volumes.longhorn.io', journal['volume'], NAMESPACE, journal['volume_uid'])
            return volume if volume and volume.get('status', {}).get('lastBackup') == backup['name'] else None
        lab.wait('systemrestore-source-last-backup', 300, last_backup)
        volumes = [o for o in lab.get('backupvolumes.longhorn.io', namespace=NAMESPACE)['items']
                   if o['spec'].get('volumeName') == journal['volume'] and o['spec'].get('backupTargetName') == 'default']
        if len(volumes) != 1:
            raise ValueError('SystemRestore source backup volume not unique')
        row['backup_volume'] = volumes[0]
        row['original'] = {kind:lab.get(kind, name, ns) for kind,name,ns in (
            ('Volume', journal['volume'], NAMESPACE), ('PersistentVolume', journal['pv'], ''),
            ('PersistentVolumeClaim', 'data', journal['namespace']), ('Pod', 'writer', journal['namespace']))}
        if row['original']['PersistentVolume']['spec'].get('persistentVolumeReclaimPolicy') != 'Retain':
            raise ValueError('SystemRestore source must use Retain')
        sc = lab.get('storageclass', journal['storage_class'])
        bundle = dict(self.bundle.identity(), name='system-probe-'+secrets.token_hex(12))
        bundle['request'] = {'kind':'system-backup', 'name':bundle['name'], 'volume_backup_policy':'disabled'}
        lab.write_private(self.bundle.record, bundle)
        lab.run_requests(pid, [bundle['request']], backup_configuration=configuration)
        obj = lab.get('systembackups.longhorn.io', bundle['name'], NAMESPACE)
        product.check_request(obj, product.request_object(bundle['request']))
        if obj.get('status', {}).get('state') != 'Ready':
            raise ValueError('SystemRestore source bundle is not Ready')
        bundle['uid'] = obj['metadata']['uid']; lab.write_private(self.bundle.record, bundle)
        row['bundle_expected'] = {'name':bundle['name'], 'manager_commit':self.system.manager_commit(obj['status']['managerImage']),
            'target':configuration['longhorn_backup_target'], 'namespace_uid':row['namespace_uid'],
            'manager_image':obj['status']['managerImage'], 'volume':journal['volume'], 'last_backup':backup['name'],
            'resource_uids':[[kind, o['metadata'].get('namespace', ''), o['metadata']['name'], o['metadata']['uid']]
                             for kind,o in row['original'].items() if kind != 'Pod'] +
                            [['StorageClass', '', sc['metadata']['name'], sc['metadata']['uid']]]}
        row['bundle_url'] = system_url(configuration['longhorn_backup_target'], bundle['name'])
        row['bundle_result'] = self.read_bundle(row)
        row['post_payload'] = payload+'-after-protected-point'
        row['post_digest'] = self.data(journal, 'writer', row['post_payload'])
        if row['backup_digest'] == row['post_digest']:
            raise ValueError('SystemRestore source time boundaries are indistinguishable')
        self.save(row)

    def read_bundle(self, row):
        expected = copy.deepcopy(row['bundle_expected'])
        expected['resource_uids'] = {(kind,ns,name):uid for kind,ns,name,uid in expected['resource_uids']}
        return self.bundle.readback(self.bundle.journal(), expected)

    def delete_source(self, row, journal):
        lab = self.lab; self.graph(journal, row)
        snapshots = [o for o in lab.get('snapshots.longhorn.io', namespace=NAMESPACE)['items']
                     if o.get('spec', {}).get('volume') == journal['volume']]
        if ({o['metadata']['uid'] for o in snapshots} != {row['snapshot']['metadata']['uid']} or
                any(o.get('spec', {}).get('source', {}).get('volumeHandle') == journal['volume']
                    for o in lab.get('volumesnapshotcontents.snapshot.storage.k8s.io')['items'])):
            raise ValueError('SystemRestore source has an unowned snapshot consumer')
        # Before the first destructive DELETE, persist exact old identities,
        # both data hashes, the external object receipts and rollback material.
        row['phase'] = 'deleting-source'; self.save(row)
        for kind in ('Pod', 'PersistentVolumeClaim'):
            obj = row['original'][kind]
            self.remove(self.current(kind, obj['metadata']['name'], obj['metadata'].get('namespace', ''), obj['metadata']['uid']))
        volume = lab.wait('systemrestore-source-detached', 300, lambda:
            (o if o and o.get('status', {}).get('state') == 'detached' else None)
            if (o := self.current('Volume', journal['volume'], NAMESPACE, journal['volume_uid'])) else None)
        pv = self.current('PersistentVolume', journal['pv'], '', journal['pv_uid'])
        if (not pv or pv.get('status', {}).get('phase') != 'Released' or
                pv['spec'].get('claimRef', {}).get('uid') != row['original']['PersistentVolumeClaim']['metadata']['uid'] or
                any(p['spec'].get('csi', {}).get('volumeHandle') == journal['volume'] and p['metadata']['uid'] != journal['pv_uid']
                    for p in lab.get('pv')['items']) or
                any(v['metadata']['uid'] != journal['volume_uid'] or v.get('status', {}).get('state') != 'detached'
                    for v in lab.get('volumes.longhorn.io', namespace=NAMESPACE)['items'])):
            raise ValueError('SystemRestore source Retain/consumer/detached boundary changed')
        self.remove(pv)
        self.remove(volume)
        for name in ('Volume', 'PersistentVolume', 'PersistentVolumeClaim'):
            obj = row['original'][name]
            if lab.get(name, obj['metadata']['name'], obj['metadata'].get('namespace', '')):
                raise ValueError('SystemRestore source resource did not disappear')
        # Source volume deletion can garbage-collect its snapshot; never force
        # a finalizer or pretend the lost snapshot is a remaining data source.
        lab.wait('systemrestore-original-snapshot-retired', 300, lambda:
                 self.current('snapshots.longhorn.io', row['snapshot_request']['name'], NAMESPACE,
                              row['snapshot']['metadata']['uid']) is None)
        if self.read_bundle(row) != row['bundle_result'] or self.store.backup_receipt(
                journal['volume'], row['backup_request']['name'], 2*1024**3) != row['backup_receipt']:
            raise ValueError('SystemRestore external recovery material changed after source loss')
        row['phase'] = 'source-deleted'; self.save(row)
        print('LONGHORN_V2_SYSTEMRESTORE_SOURCE_LOSS_VERIFIED originals_absent=3 external_material=readback', flush=True)

    def restore(self, row, journal, pid, configuration):
        lab = self.lab; request = {'kind':'system-restore', 'name':'restore-probe-'+secrets.token_hex(12),
                                  'source':row['bundle_expected']['name']}
        if lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']:
            raise ValueError('SystemRestore has a foreign restore consumer')
        row.update(restore_request=request, phase='restoring'); self.save(row)
        lab.run_requests(pid, [request], backup_configuration=configuration)
        obj = lab.get('systemrestores.longhorn.io', request['name'], NAMESPACE)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(obj, product.request_object(request))
        if obj.get('status', {}).get('state') != 'Completed':
            raise ValueError('SystemRestore product request not completed')
        row['restore'] = obj; self.save(row)
        self.verify_restored(row, journal, obj)

    def verify_restored(self, row, journal, obj, *, reader='restored-reader', remount='restored-remount'):
        lab = self.lab; request = row['restore_request']
        job = lab.get('job', 'longhorn-system-rollout-'+request['name'], NAMESPACE)
        pods = [p for p in lab.get('pods', namespace=NAMESPACE)['items'] if any(
            ref.get('uid') == (job or {}).get('metadata', {}).get('uid') for ref in p['metadata'].get('ownerReferences', []))]
        row['rollout'] = validate_rollout(obj, job, pods)
        row['job'] = job; row['rollout_pods'] = pods; self.save(row)
        def reconstructed():
            current = {kind:lab.get(kind, old['metadata']['name'], old['metadata'].get('namespace', ''))
                       for kind,old in row['original'].items() if kind != 'Pod'}
            return reconstruction_ready(row['original'], current, obj, row['backup_url'], row['bundle_url'],
                                        data_engine=self.data_engine, replicas=self.replicas)
        current = lab.wait('systemrestore-new-resource-binding', 600, reconstructed)
        new_uids = validate_reconstruction(row['original'], current, obj, row['backup_url'], row['bundle_url'],
                                           data_engine=self.data_engine, replicas=self.replicas)
        row.update(reconstructed=current, phase='reconstructed'); self.save(row)
        # This is the explicit old -> new identity handoff. Old identities
        # remain in the restore ledger and authorize only historical Events.
        journal.update(volume_uid=new_uids['Volume'], pv_uid=new_uids['PersistentVolume'])
        journal['objects']['PersistentVolumeClaim/data'] = new_uids['PersistentVolumeClaim']
        lab.write_private(self.business_record, journal)
        self.graph(journal, row)
        self.client(journal, reader, lab.WORKERS[0])
        lab.wait('systemrestore-recovered-replicas', 600, lambda:lab.healthy_replicas(journal['volume']))
        self.data(journal, reader, row['payload'], read_only=True)
        old = self.current('pod', reader, journal['namespace'], journal['objects']['Pod/'+reader])
        self.remove(old)
        self.client(journal, remount, lab.WORKERS[1])
        digest = self.data(journal, remount, row['payload'], read_only=True)
        nodes = lab.wait('systemrestore-remount-replicas', 600, lambda:lab.healthy_replicas(journal['volume']))
        if (lab.get('namespace', journal['namespace'])['metadata']['uid'] != journal['namespace_uid'] or
                self.shared() != row['shared']):
            raise ValueError('SystemRestore changed shared declarations, keys or namespace identity')
        row['phase'] = 'verified'; self.save(row)
        observed = {'restore_uid':obj['metadata']['uid'], 'new_uids':new_uids,
            'old_uids':{kind:row['original'][kind]['metadata']['uid'] for kind in new_uids},
            'rollout':row['rollout'], 'backup':row['backup_request']['name'],
            'backup_digest':row['backup_digest'], 'post_backup_digest':row['post_digest'],
            'recovered_digest':digest, 'recovered_bytes':len(row['payload'])*262144,
            'replica_nodes':nodes, 'bundle':row['bundle_result'],
            'external_objects':len(row['backup_receipt']['objects']),
            'source_objects_absent_before_restore':3, 'shared_declarations_unchanged':True,
            'independent_dr_proven':False}
        print('LONGHORN_V2_SYSTEMRESTORE_OBSERVED '+json.dumps(observed, sort_keys=True), flush=True)

    def helm_recovery_state(self):
        """Resume only the archived post-readback Helm bookkeeping rejection."""
        evidence = json.loads((BASE/'tests/evidence/longhorn-v2-systemrestore-helm-first-failure-20261025.json').read_text())
        paths = (self.record, self.business_record, self.bundle.record)
        if (evidence.get('status') != 'failed' or evidence.get('classification') != 'test-gate' or
                evidence.get('rc') != 1 or evidence.get('defect') != 'helm-release-bookkeeping' or
                set(evidence['journals_sha256']) != {p.name for p in paths} or
                any(p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != evidence['journals_sha256'][p.name]
                    for p in paths)):
            raise ValueError('SystemRestore Helm recovery differs from archived journals')
        row = self.journal(); journal = json.loads(self.business_record.read_text())
        if row['phase'] != 'reconstructed':
            raise ValueError('SystemRestore Helm recovery is not the archived phase')
        request = self.current('systemrestores.longhorn.io', row['restore']['metadata']['name'], NAMESPACE,
                               row['restore']['metadata']['uid'])
        if not request or request['spec'] != row['restore']['spec'] or request.get('status', {}).get('state') != 'Completed':
            raise ValueError('SystemRestore Helm recovery request changed')
        current = {kind:self.lab.get(kind, old['metadata']['name'], old['metadata'].get('namespace', ''))
                   for kind,old in row['reconstructed'].items()}
        identities = validate_reconstruction(row['original'], current, request, row['backup_url'], row['bundle_url'],
                                            data_engine=self.data_engine, replicas=self.replicas)
        if any(identities[k] != old['metadata']['uid'] for k,old in row['reconstructed'].items()):
            raise ValueError('SystemRestore Helm recovery reconstructed identity changed')
        shared = self.shared()
        if (hashlib.sha256(json.dumps(shared, sort_keys=True).encode()).hexdigest() != evidence['shared_sha256'] or
                shared['helm_profile_sha256'] != evidence['helm_profile_sha256'] or
                self.lab.get('namespace', journal['namespace'])['metadata']['uid'] != journal['namespace_uid']):
            raise ValueError('SystemRestore Helm recovery shared declarations changed')
        restores = self.lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']
        if len(restores) != 1 or restores[0]['metadata']['uid'] != request['metadata']['uid']:
            raise ValueError('SystemRestore Helm recovery has another request')
        self.graph(journal, row)
        return row, journal, request, evidence

    def recovery_state(self):
        """Only the archived URL assertion failure can resume this fixture."""
        evidence = json.loads((BASE/'tests/evidence/longhorn-v2-systemrestore-url-first-failure-20261020.json').read_text())
        row = self.journal()
        if row.get('phase') == 'reconstructed':
            return self.helm_recovery_state()
        journal = json.loads(self.business_record.read_text())
        saved = evidence['journals']
        if (evidence['status'] != 'failed' or evidence['classification'] != 'test-gate' or evidence['rc'] != 1 or
                row != saved[self.record.name] or row['phase'] != 'restoring' or
                journal != saved[self.business_record.name] or self.bundle.journal() != saved[self.bundle.record.name]):
            raise ValueError('SystemRestore recovery differs from archived journals')
        request = self.current('systemrestores.longhorn.io', row['restore_request']['name'], NAMESPACE,
                               evidence['request']['metadata']['uid'])
        if (not request or request['spec'] != evidence['request']['spec'] or
                request['metadata']['creationTimestamp'] != evidence['request']['metadata']['creationTimestamp'] or
                request.get('status', {}).get('state') != 'Completed'):
            raise ValueError('SystemRestore recovery request changed')
        for kind, old in evidence['native'].items():
            obj = self.current(kind, old['metadata']['name'], old['metadata'].get('namespace', ''), old['metadata']['uid'])
            if not obj or resource_signature(obj) != resource_signature(old):
                raise ValueError('SystemRestore recovery reconstructed object changed')
        restores = self.lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']
        if len(restores) != 1 or restores[0]['metadata']['uid'] != request['metadata']['uid']:
            raise ValueError('SystemRestore recovery has another request')
        if self.shared() != row['shared']:
            raise ValueError('SystemRestore recovery shared declarations changed')
        return row, journal, request, evidence

    def recover(self, pid):
        row, journal, request, evidence = self.recovery_state()
        binding = self.store.receipt()['product_binding']
        secret = self.lab.get('secret', binding['secret_name'], NAMESPACE)
        if (not secret or secret['metadata']['uid'] != binding['secret_uid'] or
                hashlib.sha256(json.dumps(secret.get('data', {}), sort_keys=True).encode()).hexdigest() != binding['secret_digest']):
            raise ValueError('SystemRestore recovery credential changed')
        if (self.read_bundle(row) != row['bundle_result'] or self.store.backup_receipt(
                journal['volume'], row['backup_request']['name'], 2*1024**3) != row['backup_receipt']):
            raise ValueError('SystemRestore recovery external materials changed')
        corrected_url = system_url(binding['fixture_spec']['backupTargetURL'], row['bundle_expected']['name'])
        helm_recovery = evidence.get('defect') == 'helm-release-bookkeeping'
        if row['bundle_url'] != (corrected_url if helm_recovery else corrected_url + '/'):
            raise ValueError('SystemRestore recovery is not the archived URL defect')
        configuration = {'longhorn_backup_target':binding['fixture_spec']['backupTargetURL'],
                         'longhorn_backup_secret':binding['secret_name']}
        self.lab.run_requests(pid, [row['restore_request']], backup_configuration=configuration)
        current = self.current('systemrestores.longhorn.io', request['metadata']['name'], NAMESPACE, request['metadata']['uid'])
        product = self.lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.check_request(current, product.request_object(row['restore_request']))
        if current.get('status', {}).get('state') != 'Completed':
            raise ValueError('SystemRestore recovery replay did not complete')
        row['bundle_url'] = corrected_url
        if helm_recovery:
            # The archived raw storage snapshot hands off only after all original
            # journals, new native UIDs, external bytes and current profile match.
            row['shared'] = self.shared()
            self.save(row)
            self.remove(self.current('pod', 'restored-remount', journal['namespace'],
                                     journal['objects']['Pod/restored-remount']))
            self.verify_restored(row, journal, current, reader='recovery-reader', remount='recovery-remount')
            return
        self.save(row)
        self.verify_restored(row, journal, current)

    def business(self, pid):
        lab = self.lab
        if any(p.exists() or p.is_symlink() for p in (self.record, self.business_record, self.bundle.record)):
            raise ValueError('SystemRestore previous fixture needs owning recovery')
        self.store.prepare(); self.store.qualify()
        configuration = self.store.product_configuration()
        lab.run_requests(pid, [], backup_configuration=configuration)
        row = dict(self.bundle.identity(), phase='source')
        self.save(row)
        journal = self.create_source(row)
        self.protect(row, journal, pid, configuration)
        row['shared'] = self.shared(); self.save(row)
        self.delete_source(row, journal)
        self.restore(row, journal, pid, configuration)

    def cleanup(self):
        if not self.record.exists() and not self.record.is_symlink():
            if self.business_record.exists() or self.business_record.is_symlink() or self.bundle.record.exists():
                raise ValueError('SystemRestore fixture lost its owning ledger')
            self.store.cleanup()
            return
        row = self.journal()
        # A failure after source deletion must preserve ALL recovery materials,
        # including a partially reconstructed target. Cleanup is not rollback.
        if row['phase'] != 'verified':
            raise ValueError('SystemRestore not verified; preserve original recovery materials and journals')
        journal = json.loads(self.business_record.read_text())
        self.graph(journal, row)
        current = self.current('systemrestores.longhorn.io', row['restore_request']['name'], NAMESPACE,
                               row['restore']['metadata']['uid'])
        if current:
            if current.get('status', {}).get('state') != 'Completed' or current['spec'] != row['restore']['spec']:
                raise ValueError('SystemRestore changed before cleanup')
            job = self.current('job', row['job']['metadata']['name'], NAMESPACE, row['rollout']['job_uid'])
            pods = [self.current('pod', p['metadata']['name'], NAMESPACE, p['metadata']['uid'])
                    for p in row['rollout_pods']]
            validate_rollout(current, job, pods)
            self.remove(current)
        self.lab.wait('systemrestore-rollout-job-clean', 300, lambda:
            self.current('job', row['job']['metadata']['name'], NAMESPACE, row['rollout']['job_uid']) is None)
        for pod in row['rollout_pods']:
            self.lab.wait('systemrestore-rollout-pod-clean', 300, lambda:
                self.current('pod', pod['metadata']['name'], NAMESPACE, pod['metadata']['uid']) is None)
        self.bundle.cleanup()
        for key, uid in journal['objects'].items():
            kind,name = key.split('/', 1)
            if kind == 'Pod':
                self.remove(self.current(kind, name, journal['namespace'], uid))
        self.remove(self.current('pvc', 'data', journal['namespace'], journal['objects']['PersistentVolumeClaim/data']))
        self.remove(self.current('pv', journal['pv'], '', journal['pv_uid']))
        volume = self.current('Volume', journal['volume'], NAMESPACE, journal['volume_uid'])
        if volume:
            volume = self.lab.wait('systemrestore-clean-volume-detached', 300, lambda:
                (o if o and o.get('status', {}).get('state') == 'detached' else None)
                if (o := self.current('Volume', journal['volume'], NAMESPACE, journal['volume_uid'])) else None)
            if any(p['spec'].get('csi', {}).get('volumeHandle') == journal['volume'] for p in self.lab.get('pv')['items']):
                raise ValueError('SystemRestore recovered volume has another PV consumer')
            self.remove(volume)
        self.remove(self.current('backups.longhorn.io', row['backup_request']['name'], NAMESPACE, row['backup']['metadata']['uid']))
        self.remove(self.current('backupvolumes.longhorn.io', row['backup_volume']['metadata']['name'], NAMESPACE,
                                 row['backup_volume']['metadata']['uid']))
        self.graph(journal, row)
        if self.shared() != row['shared']:
            raise ValueError('SystemRestore shared declarations changed during cleanup')
        self.remove(self.current('namespace', journal['namespace'], '', journal['namespace_uid']))
        self.business_record.unlink(); self.record.unlink()
        self.store.cleanup()
        print('LONGHORN_V2_SYSTEMRESTORE_FIXTURES_CLEAN_PASS', flush=True)


def baseline(backup, install, lab, store_module):
    backup.baseline(install, lab, store_module)
    for name in ('longhorn-systemrestore-business.json', 'longhorn-systemrestore-qualification.json',
                 'longhorn-systembackup-qualification.json'):
        p = lab.DIRECTORY/name
        if p.exists() or p.is_symlink():
            raise ValueError('SystemRestore baseline retains a journal')
    if lab.get('systemrestores.longhorn.io', namespace=NAMESPACE)['items']:
        raise ValueError('SystemRestore baseline retains a request')


def verify_log(log):
    rows = re.findall(r'^LONGHORN_V2_SYSTEMRESTORE_OBSERVED (.+)$', log, re.M)
    if len(rows) != 1:
        raise ValueError('SystemRestore requires exactly one observation')
    row = json.loads(rows[0])
    if (set(row['old_uids']) != {'Volume','PersistentVolume','PersistentVolumeClaim'} or
            set(row['new_uids']) != set(row['old_uids']) or
            any(not row['new_uids'][k] or row['new_uids'][k] == old for k,old in row['old_uids'].items()) or
            row['recovered_digest'] != row['backup_digest'] or row['post_backup_digest'] == row['backup_digest'] or
            not all(re.fullmatch('[a-f0-9]{64}', row[k]) for k in ('recovered_digest','backup_digest','post_backup_digest')) or
            row['recovered_bytes'] <= 0 or len(set(row['replica_nodes'])) != 3 or row['external_objects'] <= 0 or
            not row['restore_uid'] or not row['rollout']['job_uid'] or not row['rollout']['pod_uids'] or
            row['source_objects_absent_before_restore'] != 3 or row['shared_declarations_unchanged'] is not True or
            row['independent_dr_proven'] is not False):
        raise ValueError('SystemRestore lacks new identities, data boundary or scope')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline','business','cleanup','verify','recovery-baseline','recover'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        verify_log(args.log.read_text())
        print('LONGHORN_V2_SYSTEMRESTORE_LOG_VERIFIED')
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location('restore_system', BASE/'tests/helpers/longhorn-v2-systembackup.py')
    system = importlib.util.module_from_spec(spec); spec.loader.exec_module(system)
    backup, install, lab, store_module = system.fixture()
    lab.require_lease()
    if args.mode == 'baseline':
        baseline(backup, install, lab, store_module)
    elif args.mode == 'cleanup':
        lab.restore_request_input()
        Probe(lab).cleanup()
    elif args.mode == 'recovery-baseline':
        _, _, _, evidence = Probe(lab).recovery_state()
        print(evidence['original_backup_baseline'], flush=True)
    elif args.mode == 'recover':
        if args.relay_pid <= 0:
            parser.error('recover requires positive --relay-pid')
        Probe(lab).recover(args.relay_pid)
    else:
        backup.qualification_current(lab)
        Probe(lab).business(args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import importlib.util
        spec = importlib.util.spec_from_file_location('restore_error', BASE/'tests/helpers/longhorn-v2-systembackup.py')
        system = importlib.util.module_from_spec(spec); spec.loader.exec_module(system)
        path = system.write_failure_diagnostic(error)
        print('LONGHORN_V2_SYSTEMRESTORE_FAIL private_diagnostic='+path, file=sys.stderr)
        sys.exit(1)
