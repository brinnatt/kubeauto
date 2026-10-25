#!/usr/bin/env python3
"""Pinned 1.12.0 -> 1.12.1 upgrade, precheck refusal and isolated recovery.

The runner must first finish the current source-pool scenarios and provide a
clean old installation. The independent .149 installation is NEVER downgraded,
uninstalled or adopted as an old-version source by this helper.
"""
import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit
import zipfile

import yaml

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
OLD = '1.12.0'
NEW = '1.12.1'
OLD_MANAGER_COMMIT = 'fcba150e04d609c944b53cb92f7cd8adf32d7585'


def load(name):
    spec = importlib.util.spec_from_file_location('upgrade_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def atomic_config(lab, config):
    path = lab.DIRECTORY/'config.yml'
    if path.is_symlink(): raise ValueError('symlink upgrade configuration')
    with tempfile.NamedTemporaryFile('w', dir=lab.DIRECTORY, delete=False) as stream:
        yaml.safe_dump(config, stream, sort_keys=False); stream.flush(); os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def release(lab):
    rows = json.loads(lab.execute(['helm', 'list', '-A', '-o', 'json',
        '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig')]).stdout)
    matches = [row for row in rows if row['chart'].startswith('longhorn-')]
    if len(matches) != 1 or (matches[0]['name'], matches[0]['namespace']) != ('longhorn', NS):
        raise ValueError('upgrade requires exactly its owned Longhorn release')
    return matches[0]


def control_identity(lab):
    """Stable precheck signature: ignore controller heartbeat/status churn."""
    result = {'release': release(lab), 'objects': {}}
    for kind, namespace in (('customresourcedefinitions', ''), ('deployments', NS),
            ('daemonsets', NS), ('instancemanagers.longhorn.io', NS), ('pods', NS)):
        for obj in lab.get(kind, namespace=namespace)['items']:
            name = obj['metadata']['name']
            if kind == 'customresourcedefinitions' and not name.endswith('.longhorn.io'): continue
            # Jobs and backup transport are not the storage control plane.
            if kind == 'pods' and not (name.startswith('longhorn-manager-') or name.startswith('instance-manager-')): continue
            result['objects'][kind+'/'+name] = {'uid': obj['metadata']['uid'],
                'spec_sha256': hashlib.sha256(json.dumps(obj.get('spec'), sort_keys=True).encode()).hexdigest()}
    return result


def retained_identity(lab, workloads):
    lab.core_identity()
    state = lab.owner(); output = {key: state[key] for key in ('run_id', 'cluster_uid', 'namespace_uid', 'node_uids')}
    output['credentials'] = {}
    for name in ('longhorn-grpc-tls', 'longhorn-volume-key'):
        obj = lab.get('secret', name, NS)
        if not obj: raise ValueError('upgrade credential disappeared')
        output['credentials'][name] = {'uid': obj['metadata']['uid'],
            'data_sha256': hashlib.sha256(json.dumps(obj.get('data'), sort_keys=True).encode()).hexdigest()}
    output['disks'] = {}
    for node in lab.get('nodes.longhorn.io', namespace=NS)['items']:
        output['disks'][node['metadata']['name']] = {'uid': node['metadata']['uid'], 'disks': {
            key: {'uuid': value.get('diskUUID'), 'declaration': node['spec']['disks'].get(key)}
            for key, value in node.get('status', {}).get('diskStatus', {}).items()}}
    output['workloads'] = {name: workload.identities() for name, workload in workloads.items()}
    output['crd_uids'] = {obj['metadata']['name']: obj['metadata']['uid']
        for obj in lab.get('customresourcedefinitions')['items'] if obj['metadata']['name'].endswith('.longhorn.io')}
    return output


def verify_retained(current, original, runtime):
    """Keep every original UID; admit only the pinned patch's two new CRDs."""
    if current == original:
        return
    before = original.get('crd_uids', {}); after = current.get('crd_uids', {})
    verified = {name: row['uid'] for name, row in runtime.get('crds', {}).items()}
    if (runtime.get('version') != NEW or not before or after != verified or
            set(after)-set(before) != {'shards.longhorn.io', 'shardgroups.longhorn.io'} or
            any(after.get(name) != uid for name, uid in before.items()) or
            dict(current, crd_uids=before) != original):
        raise ValueError('upgrade replaced retained identity or introduced an unverified CRD')


def runtime_identity(lab, version):
    """Compare actual Manager runtime and CRD schema to the pinned Chart."""
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
    tags = json.loads((product.FILES/'images.json').read_text())
    expected = config['longhorn_image_prefix']+'/longhorn-manager:'+tags[version]['longhorn-manager']
    # control.yml derives clients from kube_node, not the customer config.
    # Read that actual product input and bind it to the owned inventory/version.
    input_path = lab.DIRECTORY/'yml/longhorn/input.json'
    if input_path.is_symlink() or not input_path.is_file():
        raise ValueError('upgrade requires the real generated Longhorn input')
    rendered = json.loads(input_path.read_text())
    if (rendered.get('longhorn_image_prefix') != config['longhorn_image_prefix'] or
            str(rendered.get('longhorn_version')) != version or
            str(config.get('longhorn_version')) != version or
            rendered.get('longhorn_namespace') != NS or
            set(rendered.get('longhorn_inventory_nodes', [])) != set(lab.NODES)):
        raise ValueError('upgrade generated input differs from its owned inventory/version')
    clients = rendered.get('longhorn_client_nodes', [])
    if (not isinstance(clients, list) or not clients or len(set(clients)) != len(clients) or
            set(clients) != set(lab.WORKERS) or not set(clients) <= set(lab.NODES)):
        raise ValueError('upgrade Manager clients differ from the owned inventory')
    targets = set()
    nodes = lab.get('nodes')['items']
    for address in clients:
        matches = [node['metadata']['name'] for node in nodes if any(
            entry.get('type') == 'InternalIP' and entry.get('address') == address
            for entry in node.get('status', {}).get('addresses', []))]
        if len(matches) != 1 or matches[0] in targets:
            raise ValueError('upgrade Manager client does not map to one distinct node')
        targets.add(matches[0])
    managers = []
    observed = set()
    for pod in lab.get('pods', namespace=NS)['items']:
        if pod['metadata'].get('deletionTimestamp'): continue
        containers = [c for c in pod['spec']['containers'] if c['name'] == 'longhorn-manager']
        if not containers: continue
        node = pod['spec'].get('nodeName')
        if node not in targets or node in observed:
            raise ValueError('upgrade Manager node differs from the configured clients')
        observed.add(node)
        statuses = [c for c in pod.get('status', {}).get('containerStatuses', []) if c['name'] == 'longhorn-manager']
        if (containers[0]['image'] != expected or len(statuses) != 1 or not statuses[0].get('imageID') or
                not statuses[0].get('ready') or not statuses[0].get('state', {}).get('running')):
            raise ValueError('upgrade Manager runtime does not match its pinned version')
        managers.append({'uid': pod['metadata']['uid'], 'image': expected, 'image_id': statuses[0]['imageID']})
    if observed != targets: raise ValueError('upgrade lacks one real Manager per configured client')
    product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
    chart = product.FILES/('longhorn-'+version+'.tgz')
    if hashlib.sha256(chart.read_bytes()).hexdigest() != product.CHART_HASHES[version]:
        raise ValueError('upgrade Chart checksum differs')
    rendered = lab.execute(['helm', 'template', 'longhorn', str(chart), '-n', NS, '--include-crds',
        '-f', str(lab.DIRECTORY/'yml/longhorn/values.json')]).stdout
    schemas = {}
    for obj in yaml.safe_load_all(rendered):
        if not obj or obj.get('kind') != 'CustomResourceDefinition': continue
        name = obj['metadata']['name']; current = lab.get('crd', name)
        if not current or not product.contains(current['spec'], obj['spec']):
            raise ValueError('upgrade actual CRD differs from the pinned Chart: '+name)
        schemas[name] = {'uid': current['metadata']['uid'],
            'spec_sha256': hashlib.sha256(json.dumps(current['spec'], sort_keys=True).encode()).hexdigest()}
    if 'volumes.longhorn.io' not in schemas: raise ValueError('pinned upgrade Chart rendered no Volume CRD')
    return {'version': version, 'managers': managers, 'crds': schemas}


def upgrade_v1_engine(lab, workload, payload):
    """The official UI's EngineUpgrade action, after config+kubecli Manager upgrade.

    V2 MUST NOT call this API in the 1.12 patch path. Proxy the official UI,
    which is allowed by the pinned Manager NetworkPolicy, in the original
    authenticated Kubernetes context. Direct backend proxying is not allowed.
    """
    if release(lab)['app_version'].removeprefix('v') != NEW:
        raise ValueError('V1 engine upgrade requires the already upgraded Manager')
    row = workload.journal(); obj = lab.get('volumes.longhorn.io', row['volume'], NS)
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    old_image = config['longhorn_image_prefix']+'/longhorn-engine:v'+OLD
    new_image = config['longhorn_image_prefix']+'/longhorn-engine:v'+NEW
    if (not obj or obj['metadata']['uid'] != row['volume_uid'] or obj['spec'].get('dataEngine', 'v1') != 'v1' or
            obj['spec'].get('frontend') != 'blockdev' or obj['spec'].get('image') != old_image or
            obj.get('status', {}).get('robustness') != 'healthy'):
        raise ValueError('V1 native engine action requires its original healthy blockdev source and old image')
    before = workload.identities(); digest = workload.data(payload, read=True)
    endpoint = '/api/v1/namespaces/'+NS+'/services/http:longhorn-frontend:80/proxy/v1/volumes/'+quote(row['volume'], safe='')+'?action=engineUpgrade'
    lab.kubectl('create', '--raw', endpoint, '-f', '-', stdin=json.dumps({'image': new_image}))
    def completed():
        current = lab.get('volumes.longhorn.io', row['volume'], NS)
        if not current or current['metadata']['uid'] != row['volume_uid']: raise ValueError('V1 engine action replaced its volume')
        return (current['spec'].get('image') == new_image and current.get('status', {}).get('currentImage') == new_image and
                current['status'].get('robustness') == 'healthy' and lab.replica_topology(row, 3))
    lab.wait('upgrade-v1-native-engine-action-completed', 1800, completed)
    if workload.identities() != before: raise ValueError('V1 engine action changed retained bindings')
    workload.data(payload, read=True)
    print('LONGHORN_V1_NATIVE_ENGINE_UPGRADE_VERIFIED volume_uid='+row['volume_uid']+' sha256='+digest, flush=True)


def validate_system_bundle(config_bytes, archive_bytes, expected):
    """Read a bounded official old-version bundle without extracting secrets."""
    if len(config_bytes) > 1024**2 or not 0 < len(archive_bytes) <= 64*1024**2:
        raise ValueError('upgrade SystemBackup exceeds its bounded readback')
    config = json.loads(config_bytes)
    for key, value in {'Name': expected['name'], 'LonghornVersion': 'v'+OLD,
                       'LonghornGitCommit': OLD_MANAGER_COMMIT,
                       'Checksum': hashlib.sha256(archive_bytes).hexdigest(),
                       'BackupTargetURL': expected['target'], 'ManagerImage': expected['manager_image']}.items():
        if config.get(key) != value: raise ValueError('upgrade SystemBackup field differs: '+key)
    objects = {}; metadata = None
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
        rows = bundle.infolist()
        if (not rows or len(rows) > 256 or len({row.filename for row in rows}) != len(rows) or
                sum(row.file_size for row in rows) > 128*1024**2):
            raise ValueError('upgrade SystemBackup member count or size differs')
        for row in rows:
            path = PurePosixPath(row.filename); parts = row.filename.rstrip('/').split('/')
            mode = row.external_attr >> 16
            if (path.is_absolute() or '\\' in row.filename or parts[0] != expected['name'] or
                    any(part in ('', '.', '..') for part in parts) or row.flag_bits & 1 or
                    stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('unsafe upgrade SystemBackup member')
            if row.is_dir(): continue
            if path.suffix != '.yaml': raise ValueError('upgrade SystemBackup unexpected member')
            document = yaml.safe_load(bundle.read(row))
            relative = str(path.relative_to(expected['name']))
            if relative == 'metadata.yaml': metadata = document; continue
            if (not relative.startswith('yamls/') or not isinstance(document, dict) or
                    not document.get('kind', '').endswith('List') or not isinstance(document.get('items'), list) or
                    document['kind'] in ('SecretList', 'PodList', 'NodeList')):
                raise ValueError('upgrade SystemBackup resource schema differs')
            for obj in document['items']:
                key = (document['kind'].removesuffix('List'), obj['metadata'].get('namespace', ''), obj['metadata']['name'])
                if key in objects: raise ValueError('duplicate upgrade SystemBackup resource')
                objects[key] = obj
    if (not isinstance(metadata, dict) or metadata.get('longhornversion') != 'v'+OLD or
            metadata.get('longhorngitcommit') != OLD_MANAGER_COMMIT or
            metadata.get('longhornnamespaceuuid') != expected['namespace_uid'] or
            metadata.get('managerimage') != expected['manager_image']):
        raise ValueError('upgrade SystemBackup source metadata differs')
    for kind, namespace, name, uid in expected['resource_uids']:
        if objects.get((kind, namespace, name), {}).get('metadata', {}).get('uid') != uid:
            raise ValueError('upgrade SystemBackup lacks an original resource UID')
    return {'version': OLD, 'config_sha256': hashlib.sha256(config_bytes).hexdigest(),
            'zip_sha256': hashlib.sha256(archive_bytes).hexdigest(), 'zip_size': len(archive_bytes),
            'resource_uids_verified': len(expected['resource_uids'])}


class Workload:
    """One ordinary or encrypted retained CSI volume with its own cleanup file."""
    def __init__(self, lab, name):
        self.lab = lab; self.name = name
        self.path = lab.DIRECTORY/('longhorn-upgrade-business-'+name+'.json')

    def journal(self):
        if self.path.is_symlink(): raise ValueError('symlink upgrade workload')
        return json.loads(self.path.read_text())

    def save(self, row): self.lab.write_private(self.path, row)

    def create(self, obj):
        return self.lab.create_business_object(self.journal(), self.path, obj)

    def prepare(self, storage_class='longhorn', snapshot=None):
        lab = self.lab
        if self.path.exists() or self.path.is_symlink(): raise ValueError('upgrade workload already exists')
        namespace = 'longhorn-business-upgrade-'+secrets.token_hex(8)
        if lab.get('namespace', namespace): raise ValueError('upgrade workload namespace already exists')
        # An unconfirmed namespace creation remains visible in the owning
        # parent intent. Never infer deletion authority from its random name.
        row = {'namespace': namespace, 'namespace_uid': None, 'objects': {},
               'storage_class': storage_class, 'reclaim_policy': 'Retain'}
        self.save(row)
        obj = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(
            {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})).stdout)
        row['namespace_uid'] = obj['metadata']['uid']
        self.save(row)
        row['defaults'] = lab.wait('upgrade-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace)); self.save(row)
        if snapshot:
            snap_name = 'upgrade-backup-'+secrets.token_hex(10); content = snap_name
            snap = self.create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
                'metadata': {'name': snap_name, 'namespace': namespace}, 'spec': {'volumeSnapshotClassName': 'longhorn-snapshot',
                'source': {'volumeSnapshotContentName': content}}})
            self.create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshotContent',
                'metadata': {'name': content}, 'spec': {'driver': 'driver.longhorn.io', 'deletionPolicy': 'Retain',
                'volumeSnapshotClassName': 'longhorn-snapshot', 'volumeSnapshotRef': {'name': snap_name,
                'namespace': namespace, 'uid': snap['metadata']['uid']},
                'source': {'snapshotHandle': 'bak://'+snapshot['volume']+'/'+snapshot['name']}}})
            row = self.journal(); row['restore_snapshot'] = {'name': snap_name, 'content': content}; self.save(row)
            lab.wait('upgrade-csi-backup-ready', 600, lambda:
                (lab.get('volumesnapshots.snapshot.storage.k8s.io', snap_name, namespace) or {}).get('status', {}).get('readyToUse'))
        claim = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'data', 'namespace': namespace},
            'spec': {'storageClassName': storage_class, 'accessModes': ['ReadWriteOnce'],
                     'resources': {'requests': {'storage': '2Gi'}}}}
        if snapshot: claim['spec']['dataSource'] = {'kind': 'VolumeSnapshot', 'apiGroup': 'snapshot.storage.k8s.io', 'name': snap_name}
        self.create(claim)
        lab.wait('upgrade-claim-bound', 900, lambda: (lab.get('pvc', 'data', namespace) or {}).get('status', {}).get('phase') == 'Bound')
        row = self.journal(); lab.bind_business_volume(row, self.path); self.mount('writer', 0)
        count = int(lab.get('storageclass', storage_class)['parameters']['numberOfReplicas'])
        lab.wait('upgrade-source-healthy', 900, lambda: lab.replica_topology(row, count))
        return self.journal()

    def mount(self, name, index):
        lab = self.lab; row = self.journal()
        definition = lab.client_pod(row['namespace'], name, 'data', 'longhorn-node-'+lab.WORKERS[index].rsplit('.', 1)[1])
        # Upgrade/recovery fixtures can survive focused runs and approvals.
        # The owning runner bounds each action and retires every recorded Pod.
        definition['spec']['containers'][0]['command'] = ['python3', '-c', 'import signal; signal.pause()']
        pod = self.create(definition)
        def ready():
            obj = lab.get('pod', name, row['namespace'])
            if not obj or obj['metadata']['uid'] != pod['metadata']['uid']: raise ValueError('upgrade client replaced')
            return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in obj.get('status', {}).get('conditions', []))
        lab.wait('upgrade-client-ready', 900, ready)
        row = self.journal(); row['client'] = name; self.save(row)

    def detach(self):
        lab = self.lab; row = self.journal()
        for key, uid in row['objects'].items():
            if not key.startswith('Pod/'): continue
            name = key.split('/', 1)[1]; obj = lab.get('pod', name, row['namespace'])
            if obj:
                if obj['metadata']['uid'] != uid: raise ValueError('upgrade client identity changed')
                lab.remove(obj)
        def detached():
            volume = lab.get('volumes.longhorn.io', row['volume'], NS)
            if not volume or volume['metadata']['uid'] != row['volume_uid']: raise ValueError('upgrade volume replaced during detach')
            return volume.get('status', {}).get('state') == 'detached' and not volume['spec'].get('nodeID')
        lab.wait('upgrade-detached', 600, detached)

    def data(self, payload, *, read=False):
        row = self.journal(); actual = self.lab.kubectl('-n', row['namespace'], 'exec', row['client'], '--', 'python3', '-c',
            self.lab.payload_program(payload, read_only=read)).stdout.strip()
        expected = hashlib.sha256(payload.encode()*262144).hexdigest()
        if actual != expected: raise ValueError('upgrade application confirmed bytes differ')
        return actual

    def identities(self):
        row = self.journal(); result = {}
        for kind, name, namespace, uid in (('volumes.longhorn.io', row['volume'], NS, row['volume_uid']),
                ('pv', row['pv'], '', row['pv_uid']), ('pvc', 'data', row['namespace'], row['objects']['PersistentVolumeClaim/data'])):
            obj = self.lab.get(kind, name, namespace)
            if not obj or obj['metadata']['uid'] != uid: raise ValueError('upgrade retained binding identity changed')
            result[kind] = {'name': name, 'uid': uid}
        claim = self.lab.get('pvc', 'data', row['namespace']); pv = self.lab.get('pv', row['pv'])
        if (claim['spec'].get('volumeName') != row['pv'] or claim['spec'].get('storageClassName') != row['storage_class'] or
                pv['spec'].get('claimRef', {}).get('uid') != row['objects']['PersistentVolumeClaim/data'] or
                pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                pv['spec']['csi'].get('volumeHandle') != row['volume']):
            raise ValueError('upgrade retained CSI binding declaration changed')
        return result

    def cleanup(self):
        if not self.path.exists(): return
        lab = self.lab; row = self.journal()
        restore = row.get('restore_snapshot')
        if restore:
            for kind, key, object_kind, namespace in (
                    ('volumesnapshots.snapshot.storage.k8s.io', 'name', 'VolumeSnapshot', row['namespace']),
                    ('volumesnapshotcontents.snapshot.storage.k8s.io', 'content', 'VolumeSnapshotContent', '')):
                obj = lab.get(kind, restore[key], namespace); uid = row['objects'].get(object_kind+'/'+restore[key])
                if obj:
                    if not uid or obj['metadata']['uid'] != uid: raise ValueError('upgrade CSI restore object replaced')
                    lab.remove(obj)
                    lab.wait('upgrade-csi-restore-clean', 180, lambda kind=kind, restore=restore, key=key, namespace=namespace:
                        lab.get(kind, restore[key], namespace) is None)
        lab.cleanup_business(self.path)


class Upgrade:
    def __init__(self, source, target, backups, shared, source_pid, target_pid):
        self.source = source; self.target = target; self.source_pid = source_pid; self.target_pid = target_pid
        self.store = source.backup_store(); self.consumer = shared.Consumer(target, self.store, backups)
        self.backups = backups
        self.path = source.DIRECTORY/'longhorn-upgrade-lifecycle.json'
        self.workloads = {name: Workload(source, name) for name in ('plain', 'encrypted')}
        self.restores = {}; self.row = None

    def save(self): self.source.write_private(self.path, self.row)

    def product(self, changes, *, refusal=None):
        lab = self.source; path = lab.DIRECTORY/'config.yml'
        original = path.read_text(); config = yaml.safe_load(original)
        allowed = {'longhorn_version', 'longhorn_upgrade_id', 'longhorn_upgrade_system_backup', 'longhorn_storage_classes'}
        if set(changes) - allowed: raise ValueError('upgrade configuration exceeded its declared scope')
        rendered = lab.DIRECTORY/'yml/longhorn/input.json'
        rendered_before = None
        if refusal:
            if rendered.is_symlink() or not rendered.is_file():
                raise ValueError('upgrade refusal requires its original rendered input')
            rendered_before = rendered.read_bytes()
        config.update(changes)
        self.row['attempt'] = {'changes': changes, 'expected_refusal': refusal, 'control_before': control_identity(lab)}; self.save()
        atomic_config(lab, config)
        try:
            lab.supervise_core(self.source_pid, 'longhorn-setup', expected_refusal=refusal)
            if refusal:
                if control_identity(lab) != self.row['attempt']['control_before']:
                    raise ValueError('upgrade precheck refusal changed control/CRD/runtime identities')
                for name, workload in self.workloads.items():
                    if not workload.path.exists(): continue
                    journal = workload.journal()
                    pod = lab.get('pod', journal.get('client', ''), journal['namespace']) if journal.get('client') else None
                    if pod:
                        if pod['metadata']['uid'] != journal['objects'].get('Pod/'+journal['client']):
                            raise ValueError('upgrade refusal replaced an original client')
                        workload.data(self.row['payloads'][name], read=True)
                self.row.setdefault('refusals', []).append(copy.deepcopy(self.row['attempt']))
            else: self.row['successful_configuration'] = config
            self.save()
        finally:
            # Refusal is a read-only preflight. A failed real rollout keeps
            # its new desired version and diagnostics; never imply rollback.
            if refusal:
                path.write_text(original)
                rendered.write_bytes(rendered_before)

    def protect(self, name, label, configuration):
        workload = self.workloads[name]; journal = workload.journal(); lab = self.source
        snapshot = {'kind': 'snapshot', 'name': 'upgrade-snapshot-'+secrets.token_hex(10), 'volume': journal['volume']}
        backup = {'kind': 'backup', 'name': 'upgrade-backup-'+secrets.token_hex(10), 'snapshot': snapshot['name'], 'mode': 'full'}
        key = name+'-'+label; point = {'volume': journal['volume'], 'request': backup, 'snapshot_request': snapshot,
            'name': backup['name'], 'payload': self.row['payloads'][name]}
        self.row.setdefault('points', {})[key] = point; self.save()
        lab.run_requests(self.source_pid, [snapshot, backup], backup_configuration=configuration)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py')
        for kind, request, field in (('snapshots.longhorn.io', snapshot, 'snapshot'), ('backups.longhorn.io', backup, 'backup')):
            obj = lab.get(kind, request['name'], NS); product.check_request(obj, product.request_object(request))
            if not obj: raise ValueError('upgrade protection request missing')
            point[field+'_uid'] = obj['metadata']['uid']
        obj = lab.get('backups.longhorn.io', backup['name'], NS); status = obj.get('status', {})
        url = urlsplit(status.get('url', '')); query = parse_qs(url.query)
        if (status.get('state') != 'Completed' or status.get('progress') != 100 or status.get('error') or
                url.scheme != 's3' or url.netloc != urlsplit(configuration['longhorn_backup_target']).netloc or
                query.get('volume') != [journal['volume']] or query.get('backup') != [backup['name']]):
            raise ValueError('upgrade backup completion/source/target differs')
        point['receipt'] = self.store.backup_receipt(journal['volume'], backup['name'], 2*1024**3)
        if point['receipt']['incremental'] is not False: raise ValueError('upgrade starting backup is not full')
        point['url'] = status['url']; point['sha256'] = workload.data(point['payload'], read=True); self.save()
        return key

    def independent_restore(self, key):
        point = self.row['points'][key]; target = self.target
        dr = load('longhorn-v2-disaster.py')
        dr.sync_target(target, self.consumer.receipt()['product_binding'])
        discovered = target.wait('upgrade-independent-backup-discovery', 600, lambda:
            dr.discovered_backup(target, point['name'], point['volume'], point['url']))
        point['target_backup_uid'] = discovered['metadata']['uid']; self.save()
        # Encrypted points use a distinct target class/key; the target's
        # existing DR key is never overwritten by source credentials.
        storage_class = 'longhorn'
        if key.startswith('encrypted-'): storage_class = self.encrypted_target_class()
        workload = Workload(target, key); self.restores[key] = workload
        self.row.setdefault('restores', {})[key] = {'workload': key}; self.save()
        workload.prepare(storage_class, point)
        row = workload.journal(); volume = target.get('volumes.longhorn.io', row['volume'], NS)
        if (row['volume_uid'] in {w.journal()['volume_uid'] for w in self.workloads.values()} or
                volume['spec'].get('cloneMode') == 'linked-clone' or
                volume.get('status', {}).get('cloneStatus', {}).get('sourceVolume')):
            raise ValueError('upgrade recovery reused source identity/ancestry')
        target.verify_restore_capacity({'claims': {'data': row}, **row}, 'data', 2*1024**3)
        workload.data(point['payload'], read=True)
        fresh = point['payload']+'-independent'; workload.data(fresh)
        self.workloads[key.split('-', 1)[0]].data(point['payload'], read=True)
        workload.detach(); workload.mount('recovery-remount', 0); workload.data(fresh, read=True)
        self.row['restores'][key].update(volume_uid=row['volume_uid'], confirmed_sha256=point['sha256'],
            independent_write_sha256=hashlib.sha256(fresh.encode()*262144).hexdigest(), remount=True)
        self.save(); workload.cleanup()
        print('LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '+json.dumps({
            'point': key, **self.row['restores'][key]}, sort_keys=True), flush=True)

    def encrypted_target_class(self):
        if self.row.get('target_key'): return self.row['target_key']['class']['name']
        source = self.source.get('secret', 'longhorn-volume-key', NS)
        name = 'longhorn-upgrade-key-'+secrets.token_hex(8)
        config = yaml.safe_load((self.target.DIRECTORY/'config.yml').read_text())
        sc = copy.deepcopy(next(c for c in config['longhorn_storage_classes'] if c['name'] == 'longhorn'))
        sc.update(name=name, encrypted=True, encryption_secret=name, default=False)
        self.row['target_key'] = {'name': name, 'class': sc, 'source_uid': source['metadata']['uid'],
            'data_sha256': hashlib.sha256(json.dumps(source['data'], sort_keys=True).encode()).hexdigest()}; self.save()
        obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': NS,
            'labels': {'kubeauto.io/longhorn-upgrade-run': self.row['id']}}, 'type': 'Opaque', 'data': source['data']}
        created = json.loads(self.target.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        self.row['target_key']['uid'] = created['metadata']['uid']; self.save()
        self.target.run_requests(self.target_pid, [], storage_classes=config['longhorn_storage_classes']+[sc])
        self.row['target_key']['class_uid'] = self.target.get('storageclass', name)['metadata']['uid']; self.save()
        return name

    def system_backup(self, configuration):
        lab = self.source; name = 'upgrade-system-'+secrets.token_hex(10)
        for name_key, workload in self.workloads.items():
            source_row = workload.journal(); backup_name = self.row['points'][name_key+'-before']['name']
            lab.wait('upgrade-source-last-backup-'+name_key, 300, lambda source_row=source_row, backup_name=backup_name:
                (lab.get('volumes.longhorn.io', source_row['volume'], NS) or {}).get('status', {}).get('lastBackup') == backup_name)
        request = {'kind': 'system-backup', 'name': name, 'volume_backup_policy': 'disabled'}
        self.row['system'] = {'name': name, 'request': request}; self.save()
        lab.run_requests(self.source_pid, [request], backup_configuration=configuration)
        obj = lab.get('systembackups.longhorn.io', name, NS)
        product = lab.load_helper('../../roles/longhorn/files/longhorn.py'); product.check_request(obj, product.request_object(request))
        if not obj or obj.get('status', {}).get('state') != 'Ready' or obj['status'].get('version') != 'v'+OLD:
            raise ValueError('upgrade requires actual source-version SystemBackup Ready')
        self.row['system'].update(uid=obj['metadata']['uid'], manager_image=obj['status']['managerImage']); self.save()
        prefix = 'backupstore/system-backups/v'+OLD+'/'+name+'/'
        bucket = self.store.receipt()['bucket']; config_key = prefix+'system-backup.cfg'; zip_key = prefix+'system-backup.zip'
        raw = self.store.mc('cat', 'lh/'+bucket+'/'+config_key).encode()
        temporary = '/tmp/longhorn-upgrade-'+secrets.token_hex(16)+'.zip'
        container = self.store.receipt()['container_id']
        self.row['system']['container_temporary'] = temporary; self.save()
        try:
            self.store.mc('cp', 'lh/'+bucket+'/'+zip_key, temporary)
            lab.execute(['docker', 'exec', container, 'chmod', '0600', temporary])
            size = int(lab.execute(['docker', 'exec', container, 'stat', '--format=%s', temporary]).stdout)
            if not 0 < size <= 64*1024**2: raise ValueError('upgrade SystemBackup exceeds readback budget')
            with tempfile.TemporaryDirectory(dir=lab.DIRECTORY) as directory:
                target = Path(directory)/'bundle.zip'
                lab.execute(['docker', 'cp', container+':'+temporary, str(target)]); target.chmod(0o600)
                archive = target.read_bytes()
            expected = {'name': name, 'target': configuration['longhorn_backup_target'],
                'namespace_uid': lab.owner()['namespace_uid'], 'manager_image': obj['status']['managerImage'], 'resource_uids': []}
            for workload in self.workloads.values():
                row = workload.journal()
                expected['resource_uids'] += [['Volume', NS, row['volume'], row['volume_uid']],
                    ['PersistentVolume', '', row['pv'], row['pv_uid']],
                    ['PersistentVolumeClaim', row['namespace'], 'data', row['objects']['PersistentVolumeClaim/data']]]
            self.row['system']['receipt'] = validate_system_bundle(raw, archive, expected)
            if self.store.mc('cat', 'lh/'+bucket+'/'+config_key).encode() != raw:
                raise ValueError('upgrade SystemBackup changed during readback')
            self.save()
        finally:
            lab.execute(['docker', 'exec', container, 'rm', '-f', '--', temporary])

    def wait_detached(self):
        lab = self.source
        names = {workload.journal()['volume']: workload.journal()['volume_uid'] for workload in self.workloads.values()}
        legacy = self.row.get('legacy', {})
        if legacy.get('volume') and not legacy.get('retired'):
            metadata = legacy['volume']['metadata']; names[metadata['name']] = metadata['uid']
        def stopped():
            volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
            if {v['metadata']['name']: v['metadata']['uid'] for v in volumes} != names:
                raise ValueError('upgrade stopped-instance boundary lost or replaced an owned volume')
            for volume in volumes:
                if (volume.get('status', {}).get('state') != 'detached' or volume['spec'].get('nodeID') or
                        volume['spec'].get('migrationNodeID')): return False
            return all(obj.get('status', {}).get('currentState') == 'stopped' for kind in ('engines', 'replicas')
                for obj in lab.get(kind+'.longhorn.io', namespace=NS)['items'] if obj['spec'].get('volumeName') in names)
        lab.wait('upgrade-v2-all-instances-stopped', 900, stopped)

    def prepare_legacy(self):
        """Retain a real one-replica 1.12.0 clone across native upgrade marking."""
        lab = self.source; parent = self.workloads['plain']; row = parent.journal()
        if release(lab)['app_version'].removeprefix('v') != OLD:
            raise ValueError('legacy fixture requires the actual pinned old Manager')
        config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
        sc = copy.deepcopy(next(c for c in config['longhorn_storage_classes'] if c['name'] == 'longhorn'))
        # The old architecture admits exactly one replica, unlike 1.12.1.
        sc.update(name='longhorn-upgrade-legacy-'+secrets.token_hex(6), clone_mode='linked-clone', replicas=1, default=False)
        self.row['legacy'] = {'class': sc, 'namespace': row['namespace']}; self.save()
        lab.run_requests(self.source_pid, [], storage_classes=config['longhorn_storage_classes']+[sc])
        self.row['legacy']['class_uid'] = lab.get('storageclass', sc['name'])['metadata']['uid']; self.save()
        claim = lab.clone_claim(row['namespace'], 'legacy', '2Gi'); claim['spec']['storageClassName'] = sc['name']
        claim['spec']['dataSource']['name'] = 'data'
        obj = parent.create(claim); self.row['legacy']['claim_uid'] = obj['metadata']['uid']; self.save()
        def bound():
            current = lab.get('pvc', 'legacy', row['namespace'])
            data_source = (current or {}).get('spec', {}).get('dataSource', {})
            if current and (current['metadata']['uid'] != obj['metadata']['uid'] or
                    current['spec'].get('storageClassName') != sc['name'] or
                    data_source.get('kind') != 'PersistentVolumeClaim' or data_source.get('name') != 'data' or
                    data_source.get('apiGroup') not in (None, '')):
                raise ValueError('old legacy CSI claim identity or source changed')
            return current if current and current.get('status', {}).get('phase') == 'Bound' else None
        bound_claim = lab.wait('upgrade-real-legacy-bound', 900, bound)
        pv = lab.get('pv', bound_claim['spec']['volumeName'])
        if (not pv or pv['spec'].get('claimRef', {}).get('uid') != obj['metadata']['uid'] or
                pv['spec'].get('storageClassName') != sc['name'] or
                pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io'):
            raise ValueError('old legacy CSI PV binding differs')
        volume = lab.get('volumes.longhorn.io', pv['spec']['csi']['volumeHandle'], NS)
        if not volume: raise ValueError('old legacy CSI handle has no native volume')
        if (volume['spec'].get('cloneMode') != 'linked-clone' or volume['spec'].get('dataEngine') != 'v2' or
                volume['spec'].get('numberOfReplicas') != 1 or
                volume['metadata'].get('labels', {}).get('longhorn.io/legacy-linked-clone')):
            raise ValueError('old CSI fixture is not a real linked clone')
        self.row['legacy'].update(pv=pv, volume=volume, created_version=OLD); self.save()
        pod = parent.create(lab.client_pod(row['namespace'], 'legacy-writer', 'legacy',
            'longhorn-node-'+lab.WORKERS[0].rsplit('.', 1)[1]))
        lab.wait('upgrade-legacy-client-ready', 900, lambda: any(c.get('type') == 'Ready' and c.get('status') == 'True'
            for c in (lab.get('pod', 'legacy-writer', row['namespace']) or {}).get('status', {}).get('conditions', [])))
        def cloned():
            current = lab.get('volumes.longhorn.io', volume['metadata']['name'], NS)
            if not current or current['metadata']['uid'] != volume['metadata']['uid']:
                raise ValueError('old legacy native volume replaced')
            status = current.get('status', {}).get('cloneStatus', {})
            if status.get('sourceVolume') and status['sourceVolume'] != row['volume']:
                raise ValueError('old legacy clone has a different native parent')
            if status.get('state') != 'completed' or status.get('sourceVolume') != row['volume']: return None
            return lab.replica_topology({'volume': volume['metadata']['name'], 'volume_uid': volume['metadata']['uid']}, 1)
        self.row['legacy']['replicas_before'] = lab.wait('upgrade-legacy-one-healthy-replica', 900, cloned)
        digest = lab.kubectl('-n', row['namespace'], 'exec', 'legacy-writer', '--', 'python3', '-c',
            lab.payload_program(self.row['payloads']['plain'], read_only=True)).stdout.strip()
        expected = hashlib.sha256(self.row['payloads']['plain'].encode()*262144).hexdigest()
        if digest != expected: raise ValueError('real old legacy clone lacks original business bytes')
        self.row['legacy']['confirmed_sha256'] = digest; self.save()
        current_pod = lab.get('pod', 'legacy-writer', row['namespace'])
        if current_pod['metadata']['uid'] != pod['metadata']['uid']: raise ValueError('legacy client replaced')
        lab.remove(current_pod)
        lab.wait('upgrade-legacy-client-retired', 180, lambda: lab.get('pod', 'legacy-writer', row['namespace']) is None)
        def detached():
            obj = lab.get('volumes.longhorn.io', volume['metadata']['name'], NS)
            return (obj if obj and obj['metadata']['uid'] == volume['metadata']['uid'] and
                    obj.get('status', {}).get('state') == 'detached' and not obj['spec'].get('nodeID') else None)
        lab.wait('upgrade-legacy-detached', 900, detached)
        self.row['legacy']['source_volume'] = row['volume']
        self.row['legacy']['source_volume_uid'] = row['volume_uid']
        self.save()

    def verify_legacy_upgrade(self):
        """Consume the linked-lifecycle gate's four refusals, without reattach."""
        legacy = self.row['legacy']; lab = self.source
        if legacy.get('created_version') != OLD or release(lab)['app_version'].removeprefix('v') != NEW:
            raise ValueError('legacy refusal requires the real old-to-new upgrade')
        saved = legacy['volume']['metadata']
        def marked():
            current = lab.get('volumes.longhorn.io', saved['name'], NS)
            if not current or current['metadata']['uid'] != saved['uid']:
                raise ValueError('legacy upgrade replaced its original clone')
            return (current if current['metadata'].get('labels', {}).get('longhorn.io/legacy-linked-clone') == 'true' and
                    current['metadata'].get('labels', {}).get('longhorn.io/clone-source-volume') == legacy['source_volume'] and
                    current.get('status', {}).get('state') == 'detached' and not current['spec'].get('nodeID') and
                    not current['spec'].get('migrationNodeID') else None)
        current = lab.wait('upgrade-real-legacy-native-marker', 300, marked)
        claim = lab.get('pvc', 'legacy', legacy['namespace']); pv = lab.get('pv', legacy['pv']['metadata']['name'])
        data_source = (claim or {}).get('spec', {}).get('dataSource', {})
        if (not claim or claim['metadata']['uid'] != legacy['claim_uid'] or not pv or
                pv['metadata']['uid'] != legacy['pv']['metadata']['uid'] or
                claim['spec'].get('volumeName') != pv['metadata']['name'] or
                claim['spec'].get('storageClassName') != legacy['class']['name'] or
                data_source.get('kind') != 'PersistentVolumeClaim' or data_source.get('name') != 'data' or
                data_source.get('apiGroup') not in (None, '') or
                pv['spec'].get('csi', {}).get('driver') != 'driver.longhorn.io' or
                pv['spec'].get('csi', {}).get('volumeHandle') != saved['name'] or
                pv['spec'].get('claimRef', {}).get('uid') != legacy['claim_uid']):
            raise ValueError('legacy upgrade changed its original CSI bindings')
        origin = {'created_version': OLD, 'upgraded_version': NEW, 'volume_name': saved['name'],
            'volume_uid': saved['uid'], 'source_volume': legacy['source_volume'],
            'source_volume_uid': legacy['source_volume_uid']}
        # The native validator checks the CSI capacity request before its
        # legacy guard. Request expansion on this disposable PVC first; the
        # legacy Volume must remain at its original size and is retired below.
        requested = str(int(current['spec']['size'])+1024**3)
        expansion = {'claim_uid': legacy['claim_uid'], 'volume_uid': saved['uid'],
            'original_bytes': current['spec']['size'], 'requested_bytes': requested}
        if legacy.get('expansion_request', expansion) != expansion:
            raise ValueError('legacy expansion request changed its original boundary')
        legacy['expansion_request'] = expansion; self.save()
        original = str(current['spec']['size'])
        actual = claim['spec']['resources']['requests']['storage']
        if actual not in ('2Gi', original, requested):
            raise ValueError('legacy PVC has an unowned capacity request')
        if actual != requested:
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': legacy['claim_uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': claim['metadata']['resourceVersion']},
                {'op': 'replace', 'path': '/spec/resources/requests/storage', 'value': requested}]
            lab.kubectl('-n', legacy['namespace'], 'patch', 'pvc', 'legacy', '--type=json',
                '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        claim = lab.get('pvc', 'legacy', legacy['namespace'])
        if (claim['metadata']['uid'] != legacy['claim_uid'] or
                claim['spec']['resources']['requests']['storage'] != requested):
            raise ValueError('legacy PVC expansion request did not persist by original UID')
        load('longhorn-v2-linked-lifecycle.py').legacy_refusals(lab, origin)
        legacy.update(refusal_verified=True, legacy_origin=origin, upgraded_native_uid=current['metadata']['uid'])
        self.save()
        print('LONGHORN_V2_UPGRADE_LEGACY_VERIFIED '+json.dumps(origin, sort_keys=True), flush=True)

    def retire_legacy(self):
        legacy = self.row.get('legacy')
        if not legacy or legacy.get('retired'): return
        lab = self.source; parent = self.workloads['plain']; journal = parent.journal()
        claim = lab.get('pvc', 'legacy', legacy['namespace'])
        if claim:
            uid = legacy.get('claim_uid')
            if not uid or claim['metadata']['uid'] != uid: raise ValueError('legacy claim unconfirmed/replaced')
            lab.remove(claim); lab.wait('upgrade-legacy-claim-clean', 180, lambda: lab.get('pvc', 'legacy', legacy['namespace']) is None)
        for kind, field in (('pv', 'pv'), ('volumes.longhorn.io', 'volume')):
            saved = legacy.get(field)
            if not saved: continue
            obj = lab.get(kind, saved['metadata']['name'], NS if field == 'volume' else '')
            if obj:
                if obj['metadata']['uid'] != saved['metadata']['uid']: raise ValueError('legacy object replaced')
                lab.remove(obj)
                lab.wait('upgrade-legacy-'+field+'-clean', 600, lambda kind=kind, saved=saved, field=field:
                    lab.get(kind, saved['metadata']['name'], NS if field == 'volume' else '') is None)
        # Keep its historical UID in the namespace event allowlist, but the
        # generic mixed-class binder must not try adopting its deleted PV.
        journal.get('claims', {}).pop('legacy', None)
        parent.save(journal)
        legacy['retired'] = True; self.save()

    def business(self):
        source = self.source; target = self.target
        if self.path.exists() or self.path.is_symlink(): raise ValueError('upgrade needs scoped recovery first')
        old_release = release(source); new_release = release(target)
        if (old_release['app_version'].removeprefix('v') != OLD or new_release['app_version'].removeprefix('v') != NEW or
                old_release.get('status') != 'deployed' or new_release.get('status') != 'deployed'):
            raise ValueError('upgrade needs actual old source and unchanged new independent target')
        if tuple(target.NODES) != ('192.168.47.149',): raise ValueError('upgrade requires its independently leased existing DR target')
        for lab in (source, target):
            if (any(lab.get(kind, namespace=NS)['items'] for kind in ('volumes.longhorn.io', *self.backups.BACKUP_RESOURCES)) or
                    any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
                    any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in
                        ('longhorn-install-business.json', 'longhorn-backup-store.json', 'longhorn-dr-backup-consumer.json', 'longhorn-request-input.json'))):
                raise ValueError('upgrade requires clean independent business and external-store baselines')
        if source.owner()['cluster_uid'] == target.owner()['cluster_uid']: raise ValueError('upgrade recovery target is not independent')
        self.row = {'id': secrets.token_hex(16), 'source_identity': retained_identity(source, {}),
            'target_identity': retained_identity(target, {}), 'payloads': {}, 'stage': 'source-1.12.0', 'points': {}}
        self.save()
        self.row['old_runtime'] = runtime_identity(source, OLD); self.save()
        # No automatic source deletion on failure: preserve the original
        # source, validated remote backups and exact rollout boundary.
        self.store.prepare(); self.store.qualify(); configuration = self.store.product_configuration()
        self.consumer.initialize(); self.consumer.qualify(self.target_pid)
        for name, workload in self.workloads.items():
            workload.prepare('longhorn-encrypted' if name == 'encrypted' else 'longhorn')
            payload = self.row['id']+'-'+name; self.row['payloads'][name] = payload
            workload.data(payload); self.save()
        encryption = source.load_helper('longhorn-encryption.py').Probe(source, data_engine='v2')
        encrypted = self.workloads['encrypted'].journal()
        self.row['old_encrypted_device'] = encryption.host_probe(source.WORKERS[0], encrypted, True); self.save()
        for name in self.workloads:
            key = self.protect(name, 'before', configuration); self.independent_restore(key)
        self.system_backup(configuration)
        changes = {'longhorn_version': NEW, 'longhorn_upgrade_id': 'upgrade-'+self.row['id'],
                   'longhorn_upgrade_system_backup': self.row['system']['name']}
        # Resource-setting admission runs before upgrade-specific admission.
        # Detach first so the missing-backup check can actually be reached.
        for workload in self.workloads.values(): workload.detach()
        self.wait_detached()
        self.product(dict(changes, longhorn_upgrade_system_backup='missing-'+self.row['id']),
                     refusal='Longhorn upgrade system backup is not Ready for the source version')
        self.workloads['plain'].mount('attached-upgrade-refusal', 0)
        self.workloads['plain'].data(self.row['payloads']['plain'], read=True)
        if source.get('settings.longhorn.io', 'data-engine-number-of-cpu-cores', NS):
            raise ValueError('old V2 upgrade fixture unexpectedly has the target-only CPU setting')
        self.product(changes, refusal='V2 resource-setting changes require all V2 volumes detached: data-engine-number-of-cpu-cores')
        for workload in self.workloads.values(): workload.detach()
        self.wait_detached(); self.prepare_legacy()
        for name, workload in self.workloads.items():
            workload.mount('before-upgrade-readback', 0); workload.data(self.row['payloads'][name], read=True); workload.detach()
        self.wait_detached()
        self.row['retained_before'] = retained_identity(source, self.workloads)
        self.row['stage'] = 'upgrade-starting'; self.save(); self.product(changes)
        if release(source)['app_version'].removeprefix('v') != NEW: raise ValueError('upgraded release is not 1.12.1')
        source.installation_health()
        self.row['new_runtime'] = runtime_identity(source, NEW); self.save()
        self.finish_business(configuration)

    def finish_business(self, configuration):
        source, target = self.source, self.target
        encryption = source.load_helper('longhorn-encryption.py').Probe(source, data_engine='v2')
        encrypted = self.workloads['encrypted'].journal()
        self.verify_legacy_upgrade()
        self.retire_legacy()
        verify_retained(retained_identity(source, self.workloads), self.row['retained_before'], self.row['new_runtime'])
        for name, workload in self.workloads.items():
            workload.mount('after-upgrade', 0); workload.data(self.row['payloads'][name], read=True)
        current = encryption.host_probe(source.WORKERS[0], encrypted, True)
        if any(current[k] != self.row['old_encrypted_device'][k] for k in ('luks_uuid', 'mapped_bytes', 'raw_bytes')):
            raise ValueError('upgrade silently resized or replaced the old encrypted device')
        self.row['new_encrypted_device'] = current; self.row['stage'] = 'upgraded-1.12.1'; self.save()
        for name, workload in self.workloads.items():
            self.row['payloads'][name] += '-after-upgrade'; workload.data(self.row['payloads'][name]); self.save()
            key = self.protect(name, 'after', configuration); self.independent_restore(key)
        self.product({'longhorn_version': OLD}, refusal='unsupported Longhorn upgrade or direct downgrade')
        if retained_identity(target, {}) != self.row['target_identity']:
            raise ValueError('independent target cluster/disk/credential identity changed')
        self.row['stage'] = 'business-pass'; self.save()
        print('LONGHORN_V2_UPGRADE_BUSINESS_PASS source=1.12.0->1.12.1 independent_target=1.12.1 target_replicas=1', flush=True)

    def resume_preflight(self):
        """Resume only a completed rollout stopped before legacy acceptance."""
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('post-upgrade continuation requires its original journal')
        self.row = json.loads(self.path.read_text()); row = self.row
        before = {'plain-before', 'encrypted-before'}
        if (row.get('stage') != 'upgrade-starting' or
                row.get('legacy', {}).get('refusal_verified') or row.get('legacy', {}).get('retired') or
                set(row.get('points', {})) != before or set(row.get('restores', {})) != before or
                any(not r.get('remount') or not r.get('volume_uid') or
                    r.get('confirmed_sha256') != row['points'][key].get('sha256')
                    for key, r in row.get('restores', {}).items()) or
                row.get('attempt', {}).get('expected_refusal') is not None or
                row.get('attempt', {}).get('changes') != {'longhorn_version': NEW,
                    'longhorn_upgrade_id': 'upgrade-'+row['id'],
                    'longhorn_upgrade_system_backup': row['system']['name']}):
            raise ValueError('post-upgrade continuation is outside the completed rollout checkpoint')
        if row.get('resume_from_journal_sha256'):
            # A prior attempt may have recorded the handoff and failed before
            # changing any business state. Require the exact original bytes.
            original = {key: value for key, value in row.items() if key != 'resume_from_journal_sha256'}
            if hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() != row['resume_from_journal_sha256']:
                raise ValueError('post-upgrade handoff already changed business state')
        self.retained_backup_configuration()
        prepare = load('longhorn-v2-upgrade-prepare.py')
        pending = prepare.Preparation(self.source, self.target, self.backups, None).read()
        if (pending['stage'] != 'old-ready' or
                prepare.raw_identity(self.source) != pending['source_raw'] or
                prepare.target_identity(self.target) != pending['target_identity'] or
                yaml.safe_load((self.source.DIRECTORY/'config.yml').read_text()) != row['successful_configuration'] or
                row['successful_configuration'].get('longhorn_version') != NEW or
                runtime_identity(self.source, NEW) != row['new_runtime'] or
                retained_identity(self.target, {}) != row['target_identity']):
            raise ValueError('post-upgrade continuation changed runtime, configuration, disk or target identity')
        verify_retained(retained_identity(self.source, self.workloads), row['retained_before'], row['new_runtime'])
        verify_retained(retained_identity(self.source, {}), row['source_identity'], row['new_runtime'])
        for lab in (self.source, self.target): lab.installation_health()
        self.store.receipt(); self.consumer.receipt()
        for workload in self.workloads.values():
            journal = workload.journal(); workload.identities()
            volume = self.source.get('volumes.longhorn.io', journal['volume'], NS)
            if (volume['metadata']['uid'] != journal['volume_uid'] or volume['status'].get('state') != 'detached' or
                    volume['spec'].get('nodeID') or volume['spec'].get('migrationNodeID') or
                    any(self.source.get('pod', key.split('/', 1)[1], journal['namespace'])
                        for key in journal['objects'] if key.startswith('Pod/'))):
                raise ValueError('post-upgrade continuation requires original detached sources without consumers')
        if self.target.get('volumes.longhorn.io', namespace=NS)['items']:
            raise ValueError('post-upgrade continuation retains an unfinished independent restore')
        print('LONGHORN_V2_UPGRADE_POST_ROLLOUT_RESUME_READY', flush=True)

    def retained_backup_configuration(self):
        """Read the original active binding; never initialize it a second time."""
        _, binding, target, secret = self.backups.Refusals(self.source, self.store).identities()
        if (self.backups.target_configuration(target['spec']) !=
                self.backups.target_configuration(binding['fixture_spec']) or
                target.get('status', {}).get('available') is not True or
                hashlib.sha256(json.dumps(secret['data'], sort_keys=True).encode()).hexdigest() != binding['secret_digest']):
            raise ValueError('post-upgrade backup binding is inactive or its credential changed')
        return {'longhorn_backup_target': binding['fixture_spec']['backupTargetURL'],
                'longhorn_backup_secret': binding['secret_name']}

    def resume(self):
        self.resume_preflight()
        configuration = self.retained_backup_configuration()
        if not self.row.get('resume_from_journal_sha256'):
            self.row['resume_from_journal_sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest(); self.save()
        print('LONGHORN_V2_UPGRADE_RESUMED_FROM_CHECKPOINT sha256='+self.row['resume_from_journal_sha256'], flush=True)
        self.finish_business(configuration)

    def retire_caches(self, lab, holder, side):
        """Reuse the proven DR cache retirement, preserving remote bytes."""
        if not holder.record.exists() and not holder.record.is_symlink(): return
        dr = load('longhorn-v2-disaster.py')
        points = {p['name']: dict(p, source_volume=p['volume'], source_backup_uid=p['backup_uid'])
                  for p in self.row['points'].values()}
        def view(): return {'points': points, 'cache_identities': self.row.setdefault('cache_identities', {})}
        def save(view_row):
            self.row['cache_identities'] = view_row['cache_identities']; self.save()
        def backup_receipt(volume, name, logical_size):
            point = points[name]
            if point['source_volume'] != volume: raise ValueError('upgrade cache receipt changed its source')
            if point['receipt'].get('encrypted') is True:
                return self.source.load_helper('longhorn-encryption.py').backup_receipt(
                    self.source, self.store, {'volume': volume}, name, logical_size)
            return self.store.backup_receipt(volume, name, logical_size)
        adapter = SimpleNamespace(receipt=view, save=save, backups=self.backups,
                                  store=SimpleNamespace(backup_receipt=backup_receipt))
        binding = holder.receipt().get('product_binding')
        if binding: dr.Standby.retain_caches(adapter, lab, binding, side)

    def restore_refused_render(self):
        """Reconcile only the observed old render left by a refused downgrade."""
        lab = self.source
        path = lab.DIRECTORY/'yml/longhorn/input.json'
        if path.is_symlink() or not path.is_file(): raise ValueError('upgrade cleanup lacks its rendered input')
        rendered = json.loads(path.read_text())
        config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
        if str(rendered.get('longhorn_version')) == str(config.get('longhorn_version')): return
        refusals = self.row.get('refusals', [])
        if (self.row['stage'] != 'cleanup-started' or str(rendered.get('longhorn_version')) != OLD or
                str(config.get('longhorn_version')) != NEW or config.get('longhorn_requests') or
                config.get('longhorn_action', 'present') != 'present' or
                release(lab)['app_version'].removeprefix('v') != NEW or not refusals or
                refusals[-1].get('expected_refusal') != 'unsupported Longhorn upgrade or direct downgrade'):
            raise ValueError('upgrade cleanup refuses an unknown rendered/configuration mismatch')
        before = control_identity(lab)
        lab.supervise_core(self.source_pid, 'longhorn-setup')
        if (control_identity(lab) != before or
                json.loads(path.read_text()).get('longhorn_version') != NEW):
            raise ValueError('refused-render reconciliation changed storage control identity or failed to render')
        print('LONGHORN_UPGRADE_REFUSED_RENDER_RECONCILED version=1.12.1 control_identity=unchanged', flush=True)

    def abort_preflight(self):
        """Admit fixture removal only before any real upgrade was attempted."""
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('pre-upgrade abort requires its original regular journal')
        self.row = json.loads(self.path.read_text()); row = self.row
        attempt = row.get('attempt', {})
        if (row.get('stage') not in ('source-1.12.0', 'abort-cleanup-started') or
                any(key in row for key in ('new_runtime', 'legacy', 'retained_before', 'successful_configuration')) or
                not attempt.get('expected_refusal') or attempt.get('changes', {}).get('longhorn_version') != NEW):
            raise ValueError('pre-upgrade abort refuses an actual or unknown rollout boundary')
        prepare = load('longhorn-v2-upgrade-prepare.py')
        preparation = self.source.DIRECTORY/prepare.RECORD
        prepare.digest(preparation); pending = json.loads(preparation.read_text())
        if (pending.get('stage') != 'old-ready' or prepare.core(self.source) != pending['source_core'] or
                prepare.core(self.target) != pending['target_core'] or
                self.source.owner()['namespace_uid'] != pending['source_namespace_uid'] or
                prepare.raw_identity(self.source) != pending['source_raw']):
            raise ValueError('pre-upgrade abort changed original preparation or raw ownership')
        expected = yaml.safe_load(pending['original_config'])
        expected.update(longhorn_version=OLD, longhorn_upgrade_id='', longhorn_upgrade_system_backup='')
        if (yaml.safe_load((self.source.DIRECTORY/'config.yml').read_text()) != expected or
                prepare.target_identity(self.target) != pending['target_identity'] or
                retained_identity(self.source, {}) != row['source_identity'] or
                retained_identity(self.target, {}) != row['target_identity'] or
                runtime_identity(self.source, OLD) != row['old_runtime'] or
                control_identity(self.source) != attempt['control_before']):
            raise ValueError('pre-upgrade abort requires unchanged old runtime, configuration and independent target')
        for lab in (self.source, self.target):
            lab.installation_health()
        if self.store.record.exists(): self.store.receipt()
        if self.consumer.record.exists(): self.consumer.receipt()
        for name, workload in self.workloads.items():
            if workload.path.exists():
                workload.identities()
                journal = workload.journal()
                pod = self.source.get('pod', journal.get('client', ''), journal['namespace'])
                if pod:
                    if pod['metadata']['uid'] != journal['objects'].get('Pod/'+journal['client']):
                        raise ValueError('pre-upgrade abort original writer replaced')
                    workload.data(row['payloads'][name], read=True)
            elif row['stage'] == 'source-1.12.0':
                raise ValueError('pre-upgrade abort lost an original source journal')
        print('LONGHORN_V2_UPGRADE_ABORT_PREFLIGHT_PASS source_version=1.12.0 rollout_attempted=false', flush=True)

    def cleanup(self, *, abort_before_upgrade=False):
        if self.path.is_symlink(): raise ValueError('symlink upgrade recovery receipt')
        if not self.path.exists(): return
        self.row = json.loads(self.path.read_text())
        if abort_before_upgrade:
            self.abort_preflight()
        else:
            if self.row['stage'] not in ('business-pass', 'cleanup-started'):
                raise ValueError('incomplete upgrade retains original source, external backups and rollout diagnostics; recover owning stage first')
            verify_retained(retained_identity(self.source, {}), self.row['source_identity'], self.row['new_runtime'])
        if retained_identity(self.target, {}) != self.row['target_identity']:
            raise ValueError('upgrade cleanup identity changed')
        self.row['stage'] = 'abort-cleanup-started' if abort_before_upgrade else 'cleanup-started'; self.save()
        for key in self.row.get('restores', {}): Workload(self.target, key).cleanup()
        self.retire_legacy()
        system = self.row.get('system')
        if system and not self.row.get('system_cleanup_complete'):
            obj = self.source.get('systembackups.longhorn.io', system['name'], NS)
            if obj:
                if obj['metadata']['uid'] != system.get('uid'): raise ValueError('upgrade SystemBackup identity changed')
                self.source.remove(obj)
                self.source.wait('upgrade-system-backup-clean', 600, lambda:
                    self.source.get('systembackups.longhorn.io', system['name'], NS) is None)
            prefix = 'backupstore/system-backups/v'+OLD+'/'+system['name']+'/'
            bucket = self.store.receipt()['bucket']
            def remote_gone():
                entries = [json.loads(line) for line in self.store.mc('ls', '--recursive', '--json', 'lh/'+bucket).splitlines() if line.strip()]
                if any(obj.get('status') != 'success' for obj in entries): raise ValueError('upgrade system-backup cleanup listing failed')
                return not any(obj.get('key', '').startswith(prefix) for obj in entries)
            self.source.wait('upgrade-system-backup-remote-clean', 600, remote_gone)
            # The independent target may have discovered the source bundle.
            # Only after its exact remote prefix is empty may its cached CR
            # retire. Never delete a target cache while it could delete the
            # only remaining remote SystemBackup.
            cached = self.target.get('systembackups.longhorn.io', system['name'], NS)
            if cached:
                if cached.get('status', {}).get('version') != 'v'+OLD:
                    raise ValueError('target SystemBackup cache differs from the verified old bundle')
                self.row['target_system_cache_uid'] = cached['metadata']['uid']; self.save()
                self.target.remove(cached)
                self.target.wait('upgrade-target-system-cache-clean', 600, lambda:
                    self.target.get('systembackups.longhorn.io', system['name'], NS) is None)
            self.row['system_cleanup_complete'] = True; self.save()
        self.retire_caches(self.target, self.consumer, 'target')
        self.consumer.cleanup()
        self.retire_caches(self.source, self.store, 'source')
        # No generic binder may remove a protected legacy parent. Its child
        # was explicitly retired and proved absent above before this step.
        for workload in self.workloads.values(): workload.cleanup()
        for point in self.row['points'].values():
            name = point['snapshot_request']['name']; obj = self.source.get('snapshots.longhorn.io', name, NS)
            if obj:
                if obj['metadata']['uid'] != point.get('snapshot_uid'): raise ValueError('upgrade snapshot replaced')
                self.source.remove(obj)
            self.source.wait('upgrade-native-snapshot-clean', 600, lambda name=name:
                self.source.get('snapshots.longhorn.io', name, NS) is None)
        for lab, declaration in ((self.source, self.row.get('legacy')), (self.target, self.row.get('target_key'))):
            if not declaration: continue
            config = declaration['class']; sc = lab.get('storageclass', config['name'])
            if sc:
                product = lab.load_helper('../../roles/longhorn/files/longhorn.py'); product.require_owned(sc)
                desired = product.storage_class(config)
                if (sc['metadata']['uid'] != declaration.get('class_uid') or
                        any(sc.get(k) != desired.get(k) for k in ('parameters', 'provisioner', 'reclaimPolicy', 'allowVolumeExpansion', 'volumeBindingMode')) or
                        any(obj['spec'].get('storageClassName') == config['name']
                            for kind in ('pvc', 'pv') for obj in lab.get(kind, namespace='*' if kind == 'pvc' else '')['items'])):
                    raise ValueError('upgrade fixture class changed or has consumers')
                lab.remove(sc)
                lab.wait('upgrade-fixture-class-clean', 60, lambda lab=lab, config=config: lab.get('storageclass', config['name']) is None)
        key = self.row.get('target_key')
        if key:
            obj = self.target.get('secret', key['name'], NS)
            if obj:
                if (obj['metadata']['uid'] != key.get('uid') or
                        hashlib.sha256(json.dumps(obj.get('data'), sort_keys=True).encode()).hexdigest() != key['data_sha256']):
                    raise ValueError('upgrade temporary recovery key replaced')
                self.target.remove(obj)
                self.target.wait('upgrade-temporary-key-clean', 60, lambda: self.target.get('secret', key['name'], NS) is None)
        self.store.cleanup()
        self.restore_refused_render()
        self.source.installation_health(); self.target.installation_health()
        verify_retained(retained_identity(self.source, {}), self.row['source_identity'], self.row['old_runtime' if abort_before_upgrade else 'new_runtime'])
        if retained_identity(self.target, {}) != self.row['target_identity']:
            raise ValueError('upgrade cleanup changed a shared retained identity')
        if abort_before_upgrade:
            prepare = load('longhorn-v2-upgrade-prepare.py')
            for lab, version in ((self.source, OLD), (self.target, NEW)):
                prepare.healthy(lab, version)
                # The owning upgrade journal is archived only after every
                # other fixture has retired; never hide it from empty().
                for name in prepare.JOURNALS:
                    if name == self.path.name: continue
                    path = lab.DIRECTORY/name
                    if path.exists() or path.is_symlink(): raise ValueError('pre-upgrade abort retains another fixture journal')
                if (any(lab.get(kind, namespace=NS)['items'] for kind in ('volumes.longhorn.io', *self.backups.BACKUP_RESOURCES)) or
                        any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items'])):
                    raise ValueError('pre-upgrade abort retains storage consumers')
        # Preserve the completed result independently of the active recovery
        # receipt so a scoped-clean retry cannot pretend to rerun the upgrade.
        result = self.path.with_name(('longhorn-upgrade-aborted-' if abort_before_upgrade else 'longhorn-upgrade-result-')+self.row['id']+'.json')
        self.row['stage'] = 'aborted-before-upgrade' if abort_before_upgrade else 'complete'; self.source.write_private(result, self.row)
        self.path.unlink()
        if abort_before_upgrade:
            print('LONGHORN_V2_UPGRADE_ABORT_SCOPED_CLEAN_PASS source_version=1.12.0 rollout_attempted=false', flush=True)
        else:
            print('LONGHORN_V2_UPGRADE_SCOPED_CLEAN_PASS target_cluster_identity=unchanged source_version=1.12.1', flush=True)


def configure_old(lab, pid):
    """Owning runner calls this only AFTER normal empty-storage retirement."""
    if tuple(lab.NODES) != ('192.168.47.146', '192.168.47.147', '192.168.47.148'):
        raise ValueError('V2 old installation requires the original three-node source; target .149 is preserved')
    rows = json.loads(lab.execute(['helm', 'list', '-A', '-o', 'json', '--kubeconfig', str(lab.DIRECTORY/'kubectl.kubeconfig')]).stdout)
    if (any(row['chart'].startswith('longhorn-') for row in rows) or
            any(obj['metadata']['name'].endswith('.longhorn.io') for obj in lab.get('crds')['items'])):
        raise ValueError('old-version setup refuses an existing Longhorn installation; never downgrade the active source')
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    config.update(longhorn_version=OLD, longhorn_upgrade_id='', longhorn_upgrade_system_backup='')
    atomic_config(lab, config); lab.supervise_core(pid, 'longhorn-setup')
    if release(lab)['app_version'].removeprefix('v') != OLD: raise ValueError('old installation did not converge')
    lab.installation_health()
    print('LONGHORN_UPGRADE_OLD_INSTALL_PASS version=1.12.0', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('configure-old', 'business', 'resume', 'resume-check', 'clean', 'recovery-check', 'abort-clean'))
    parser.add_argument('--source-relay-pid', type=int, required=True)
    parser.add_argument('--target-relay-pid', type=int, default=0)
    args = parser.parse_args()
    shared = load('longhorn-v2-dr-store.py')
    _, _, source, backups, _, _, target = shared.fixture()
    source.require_lease()
    if args.source_relay_pid <= 0: parser.error('a supervised source relay is required')
    if args.mode == 'configure-old': configure_old(source, args.source_relay_pid)
    else:
        target.require_lease()
        if args.target_relay_pid <= 0: parser.error('business requires the independent target relay')
        scenario = Upgrade(source, target, backups, shared, args.source_relay_pid, args.target_relay_pid)
        if args.mode == 'resume-check': scenario.resume_preflight()
        elif args.mode == 'resume':
            scenario.resume(); scenario.cleanup()
            print('LONGHORN_V2_UPGRADE_PASS', flush=True)
        elif args.mode == 'recovery-check':
            if scenario.path.is_symlink() or not scenario.path.is_file(): raise ValueError('missing owning upgrade journal')
            if json.loads(scenario.path.read_text()).get('stage') in ('source-1.12.0', 'abort-cleanup-started'):
                scenario.abort_preflight()
            else:
                prepare = load('longhorn-v2-upgrade-prepare.py')
                prepare.Preparation(source, target, backups, None).read()
        elif args.mode == 'abort-clean': scenario.cleanup(abort_before_upgrade=True)
        elif args.mode == 'clean': scenario.cleanup()
        else:
            scenario.business(); scenario.cleanup()
            print('LONGHORN_V2_UPGRADE_PASS', flush=True)


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-upgrade-failure-', suffix='.txt', dir='/var/tmp', delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_UPGRADE_FAIL private_diagnostic='+diagnostic.name, file=sys.stderr)
        sys.exit(1)
