#!/usr/bin/env python3
"""All-replica refusal followed by recovery from a restore-tested backup.

Never clears failedAt, requests salvage or chooses an alleged last replica.
The confirmed prefix is frozen before the backup. After native refusal, fence
the original consumer and detach the source before restoring hardware; a new
CSI volume restores that exact backup boundary.
"""
import argparse
import copy
import errno
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v2_all_replicas_fault'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def persist(lab, journal, proof):
    journal[KEY] = proof
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def all_disks(lab, journal):
    disk = load('longhorn-v2-disk-faults.py')
    # This proves there is only the source volume and its native SPDK instances.
    original = disk.boundary(lab, journal, 'single-disk')
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    replicas = lab.get('replicas.longhorn.io', namespace=NS)['items']
    result = {}
    for replica in replicas:
        name = replica['metadata']['name']
        native = original['runtime']['instances']['replicas.longhorn.io'][name]
        node = replica['spec']['nodeID']
        address = load('longhorn-v2-runtime-faults.py').address(lab, node)
        declarations = config['longhorn_block_disks'][address]
        if len(declarations) != 1 or native['uid'] != replica['metadata']['uid']:
            raise ValueError('all-replica fault requires original replicas on exclusive disks')
        declaration = declarations[0]
        current = lab.get('nodes.longhorn.io', node, NS)
        spec = current['spec']['disks']
        status = current['status']['diskStatus'][declaration['name']]
        owned = lab.owner()['raw_disks'][address]
        if (set(spec) != {declaration['name']} or spec[declaration['name']].get('diskType') != 'block' or
                spec[declaration['name']].get('diskDriver') != 'aio' or
                spec[declaration['name']].get('path') != owned['path'] or
                declaration['path'] != owned['path'] or declaration['identity'] != owned['identity'] or
                replica['spec']['diskID'] != status.get('diskUUID')):
            raise ValueError('all-replica native disk or stable hardware identity differs')
        proof = dict(original, address=address, node=node, node_uid=lab.get('node', node)['metadata']['uid'],
            declaration=copy.deepcopy(declaration), disk_uuid=status['diskUUID'], replica=name,
            replica_uid=replica['metadata']['uid'])
        proof['host'] = disk.host_call(lab, proof, 'probe')
        if (proof['host']['state'] != 'running' or proof['host']['disk']['size_bytes'] != owned['size_bytes'] or
                proof['host']['machine_id'] != owned['machine_id']):
            raise ValueError('all-replica fault requires the original running raw disks')
        result[address] = proof
    if len(result) != 3 or set(result) != set(lab.WORKERS):
        raise ValueError('all-replica fault requires all three leased failure domains')
    return result


def policy(lab, expected=None):
    row = lab.get('settings.longhorn.io', 'auto-salvage', NS)
    if not row or row.get('value') != 'false' or expected and row['metadata']['uid'] != expected['uid']:
        raise ValueError('all-replica refusal requires the original product autoSalvage=false policy')
    return {'uid': row['metadata']['uid'], 'value': row['value']}


def faulted(lab, journal, proof):
    policy(lab, proof['policy'])
    volume = load('longhorn-v2-disk-faults.py').volume(lab, journal)
    replicas = lab.get('replicas.longhorn.io', namespace=NS)['items']
    expected = {p['replica']: p['replica_uid'] for p in proof['disks'].values()}
    selected = [r for r in replicas if r['spec'].get('volumeName') == journal['volume']]
    if {r['metadata']['name']: r['metadata']['uid'] for r in selected} != expected:
        raise ValueError('all-replica refusal replaced an original replica')
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items']
               if e['spec'].get('volumeName') == journal['volume']]
    original = next(iter(next(iter(proof['disks'].values()))['runtime']['instances']['engines.longhorn.io'].values()))['uid']
    if len(engines) != 1 or engines[0]['metadata']['uid'] != original:
        raise ValueError('all-replica refusal original engine changed')
    engine = engines[0]
    if engine['spec'].get('salvageRequested') or engine.get('status', {}).get('salvageExecuted'):
        raise ValueError('all-replica scenario unexpectedly requested or executed salvage')
    if (volume.get('status', {}).get('robustness') != 'faulted' or
            engine.get('status', {}).get('currentState') not in ('error', 'stopped') or
            not all(r['spec'].get('healthyAt') for r in selected)):
        return False
    return {'volume_uid': journal['volume_uid'], 'robustness': 'faulted', 'engine_uid': original,
            'engine_state': engine.get('status', {}).get('currentState'), 'salvageRequested': False,
            'salvageExecuted': False, 'autoSalvage': False,
            'replicas': {r['metadata']['name']: {'uid': r['metadata']['uid'], 'failedAt': r['spec'].get('failedAt', ''),
                'healthyAt': r['spec']['healthyAt']} for r in selected}}


def source_fenced(lab, journal, proof):
    # Device recovery may legitimately reattach previously healthy replicas.
    # Fence the original consumer and wait for native detach before restoring
    # hardware; never rely on a fictitious persistent FailedAt after engine loss.
    policy(lab, proof['policy'])
    if lab.get('pod', 'writer', journal['namespace']) is not None:
        return False
    volume = load('longhorn-v2-disk-faults.py').volume(lab, journal)
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items']
               if e['spec'].get('volumeName') == journal['volume']]
    uid = proof['refusal']['engine_uid']
    if len(engines) != 1 or engines[0]['metadata']['uid'] != uid:
        raise ValueError('source fencing replaced the original engine')
    engine = engines[0]
    if engine['spec'].get('salvageRequested') or engine.get('status', {}).get('salvageExecuted'):
        raise ValueError('source fencing unexpectedly used salvage')
    status = volume.get('status', {})
    if (status.get('state') != 'detached' or status.get('currentNodeID') or status.get('currentEngineNodeID') or
            engine['spec'].get('desireState') != 'stopped' or engine.get('status', {}).get('currentState') != 'stopped'):
        return False
    return {'volume_uid': journal['volume_uid'], 'engine_uid': uid,
            'writer_uid': journal['objects']['Pod/writer'], 'writer_absent': True,
            'volume_state': 'detached', 'engine_state': 'stopped', 'engine_desire': 'stopped',
            'current_node': '', 'current_engine_node': '', 'salvageRequested': False, 'salvageExecuted': False}



def fence_source(lab, journal, proof, original_writer):
    old = lab.get('pod', 'writer', journal['namespace'])
    if not old or old['metadata']['uid'] != original_writer['metadata']['uid']:
        raise ValueError('all-replica original writer changed before fencing')
    lab.remove(old)
    lab.wait('all-replica-original-writer-fenced', 180,
             lambda: lab.get('pod', 'writer', journal['namespace']) is None)
    fenced = lab.wait('all-replica-source-native-detached', 240, lambda: source_fenced(lab, journal, proof))
    proof['source_fenced'] = fenced
    persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED '+json.dumps(fenced, sort_keys=True), flush=True)
    return fenced


def source_read_program():
    # The checkpoint is immutable. Bypass the warm page cache without writing
    # data or atime; an anonymous mmap supplies the O_DIRECT-aligned buffer.
    return '''import json,mmap,os,time
fd=os.open('/data/confirmed',os.O_RDONLY|os.O_DIRECT|os.O_NOATIME|os.O_NOFOLLOW)
results=[]
try:
 with mmap.mmap(-1,4096) as buffer:
  for attempt in range(3):
   try: result={'errno':0,'read_bytes':os.preadv(fd,[buffer],0)}
   except OSError as error: result={'errno':error.errno,'read_bytes':0}
   results.append(result)
   if attempt<2: time.sleep(1)
finally: os.close(fd)
print(json.dumps({'attempts':results}))
'''


def source_io_failed(observed):
    # NOTSUPP can be an intermediate block/NVMe result while a failed backend
    # transitions. It never proves storage failure alone: two later reads must
    # return actual storage errors, followed by the separate native CR refusal.
    attempts = observed.get('attempts', [])
    storage_errors = (errno.EIO, errno.ENODEV, errno.ENXIO, errno.ETIMEDOUT)
    return (set(observed) == {'attempts'} and len(attempts) == 3 and
            all(set(item) == {'errno', 'read_bytes'} and item['read_bytes'] == 0 and
                item['errno'] in storage_errors + (errno.EOPNOTSUPP,) for item in attempts) and
            all(item['errno'] in storage_errors for item in attempts[1:]))


def probe_source_io(lab, journal, proof):
    lab.check_business_graph(journal)
    pod = lab.get('pod', 'writer', journal['namespace'])
    if (not pod or pod['metadata']['uid'] != journal['objects']['Pod/writer'] or
            pod['metadata'].get('ownerReferences') or pod.get('status', {}).get('phase') != 'Running'):
        raise ValueError('all-replica direct read requires the original running bare writer')
    intent = {'pod_uid': pod['metadata']['uid'], 'volume_uid': journal['volume_uid'],
              'path': '/data/confirmed', 'offset': 0, 'length': 4096, 'direct': True, 'read_only': True}
    proof['source_io_intent'] = intent
    persist(lab, journal, proof)
    # A timed-out remote read is fenced by the owning cleanup's Pod deletion,
    # after restoring the physical disks. A timeout never proves refusal.
    result = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
                         source_read_program(), check=False, timeout=120)
    # Preserve the actual bounded request result before any assertion rejects it.
    # This is diagnostic evidence, never a successful refusal marker.
    proof['source_io_observed'] = {'returncode': result.returncode, 'stdout': result.stdout}
    persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_SOURCE_IO_OBSERVED '+json.dumps(
        proof['source_io_observed'], sort_keys=True), flush=True)
    if result.returncode:
        raise ValueError('all-replica direct read did not return its actual I/O result')
    observed = json.loads(result.stdout)
    if not source_io_failed(observed):
        raise ValueError('all-replica direct read did not observe a storage I/O failure')
    proof['source_io'] = dict(intent, **observed)
    persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_SOURCE_IO '+json.dumps(proof['source_io'], sort_keys=True), flush=True)


def inject(lab, journal, backup_proof):
    if KEY in journal:
        raise ValueError('prior all-replica intent requires recovery; never replay')
    if (not backup_proof.get('preflight_restore_verified') or not backup_proof.get('checkpoint') or
            not backup_proof.get('backup') or backup_proof.get('source_volume_uid') != journal['volume_uid'] or
            not backup_proof.get('preflight_volume_uid') or backup_proof['preflight_volume_uid'] == journal['volume_uid']):
        raise ValueError('all-replica fault requires this exact restore-tested confirmed backup')
    current_backup = lab.get('backups.longhorn.io', backup_proof['backup'], NS)
    stored = journal.get('backups', {}).get(backup_proof['backup'], {})
    receipt = stored.get('object_receipt')
    if (not current_backup or current_backup['metadata']['uid'] != backup_proof['backup_uid'] or
            not receipt or receipt.get('volume') != journal['volume'] or receipt.get('backup') != backup_proof['backup'] or
            hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest() != backup_proof['object_receipt_sha256']):
        raise ValueError('restore-tested backup identity or referenced objects changed')
    control, faults = load('longhorn-control-faults.py'), load('longhorn-faults.py')
    checkpoint = backup_proof['checkpoint']
    if control.confirmed(checkpoint['records'], checkpoint['token'], faults)['summary'] != checkpoint['summary']:
        raise ValueError('all-replica backup checkpoint has an invalid confirmed prefix')
    disks = all_disks(lab, journal)
    if all_disks(lab, journal) != disks:
        raise ValueError('all-replica boundary changed before injection')
    proof = {'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'],
             'volume_uid': journal['volume_uid'], 'policy': policy(lab), 'disks': disks, 'backup': backup_proof}
    persist(lab, journal, proof)
    disk = load('longhorn-v2-disk-faults.py')
    for address, item in disks.items():
        # Intent precedes even an ambiguous response. Cleanup restores all
        # intended original disks; it never replays an offline operation.
        item['offline_intent'] = True
        persist(lab, journal, proof)
        item['offline'] = disk.host_call(lab, item, 'offline')
        persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE '+json.dumps({
        address: {'original': item['host'], 'offline': item['offline'],
                  'replica_uid': item['replica_uid'], 'disk_uuid': item['disk_uuid']}
        for address, item in disks.items()}, sort_keys=True), flush=True)
    # SCSI offline changes admission of future I/O; an idle AIO-backed RAID
    # need not discover the loss until a request reaches its actual data path.
    # RAID1 fails only the initially selected member after read retries;
    # the bounded probe performs one request per original failure domain.
    probe_source_io(lab, journal, proof)
    refusal = lab.wait('v2-all-original-replicas-native-faulted', 240, lambda: faulted(lab, journal, proof))
    proof['refusal'] = refusal
    persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_REFUSED '+json.dumps(refusal, sort_keys=True), flush=True)
    return proof


def restore_disks(lab, journal, proof):
    state = lab.owner()
    if (proof.get('run_id') != state['run_id'] or proof.get('cluster_uid') != state['cluster_uid'] or
            proof.get('volume_uid') != journal['volume_uid'] or set(proof['disks']) != set(lab.WORKERS)):
        raise ValueError('all-replica journal differs from original leased source')
    disk = load('longhorn-v2-disk-faults.py')
    for item in proof['disks'].values():
        disk.validate(lab, journal, item)
        if item.get('offline_intent'):
            if disk.host_call(lab, item, 'restore') != item['host']:
                raise ValueError('all-replica original disk restoration unproven')
            item['disk_restored'] = True
            persist(lab, journal, proof)
    proof['disks_restored'] = True
    persist(lab, journal, proof)
    print('LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3', flush=True)


def ready(lab, namespace, name, uid):
    pod = lab.get('pod', name, namespace)
    if not pod or pod['metadata']['uid'] != uid:
        raise ValueError('all-replica client was replaced')
    return load('longhorn-v2-control-faults.py').pod_ready(pod)


def ledger(lab, journal, client, token, path):
    faults, control = load('longhorn-faults.py'), load('longhorn-control-faults.py')
    code = faults.ledger_program(token, duration=5).replace('/data/fault-ledger', path).replace('stop=ledger_stop_requested', 'stop=None')
    output = lab.kubectl('-n', journal['namespace'], 'exec', client, '--', 'python3', '-u', '-c', code, timeout=45)
    rows = [json.loads(s) for s in output.stdout.splitlines()]
    if len([r for r in rows if r.get('type') == 'complete']) != 1:
        raise ValueError('all-replica finite ledger did not complete')
    result = control.confirmed(rows, token, faults)
    result.update(path=path, retries=[r for r in rows if r.get('type') == 'retry'])
    return result


def verify_ledger(lab, journal, client, checkpoint):
    faults = load('longhorn-faults.py')
    def kubectl(*args, **kw):
        args = tuple(a.replace('/data/fault-ledger', checkpoint['path']) if isinstance(a, str) else a for a in args)
        return lab.kubectl(*args, **kw)
    from types import SimpleNamespace
    faults.verify_client_ledger(SimpleNamespace(kubectl=kubectl), journal, client, checkpoint)


def create_claim(lab, journal, create, name, snapshot=None):
    spec = {'accessModes': ['ReadWriteOnce'], 'storageClassName': journal['storage_class'],
            'resources': {'requests': {'storage': '2Gi'}}}
    if snapshot:
        spec['dataSource'] = {'kind': 'VolumeSnapshot', 'apiGroup': 'snapshot.storage.k8s.io', 'name': snapshot}
    claim = create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
        'metadata': {'name': name, 'namespace': journal['namespace']}, 'spec': spec})
    def bound():
        obj = lab.get('pvc', name, journal['namespace'])
        if not obj or obj['metadata']['uid'] != claim['metadata']['uid']:
            raise ValueError('all-replica original claim was replaced')
        return obj if obj.get('status', {}).get('phase') == 'Bound' else None
    lab.wait('all-replica-claim-bound-'+name, 900, bound)
    lab.bind_business_volume(journal, lab.DIRECTORY/RECORD, name)
    row = journal if name == 'data' else journal['claims'][name]
    lab.wait('all-replica-native-volume-'+name, 120, lambda: lab.get('volumes.longhorn.io', row['volume'], NS))
    lab.bind_business_volume(journal, lab.DIRECTORY/RECORD, name)
    if not row.get('volume_uid'):
        raise ValueError('all-replica claim lacks native volume identity')
    return row


def client(lab, journal, create, name, claim, worker):
    pod = create(lab.client_pod(journal['namespace'], name, claim, 'longhorn-node-'+worker.rsplit('.', 1)[1]))
    lab.wait('all-replica-client-'+name, 900, lambda: ready(lab, journal['namespace'], name, pod['metadata']['uid']))
    return pod


def backup(lab, journal, relay_pid, store, configuration, checkpoint):
    snapshot = 'all-replicas-snapshot-'+secrets.token_hex(8)
    name = 'all-replicas-backup-'+secrets.token_hex(8)
    snap_request = {'kind': 'snapshot', 'name': snapshot, 'volume': journal['volume']}
    backup_request = {'kind': 'backup', 'name': name, 'snapshot': snapshot, 'mode': 'full'}
    journal.setdefault('native_snapshots', {})[snapshot] = {'request': snap_request}
    journal.setdefault('backups', {})[name] = {'request': backup_request}
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    lab.run_requests(relay_pid, [snap_request, backup_request], backup_configuration=configuration)
    lab.bind_snapshots(journal)
    lab.bind_backups(journal)
    obj = lab.get('backups.longhorn.io', name, NS)
    status = obj.get('status', {})
    if status.get('state') != 'Completed' or status.get('progress') != 100 or status.get('error'):
        raise ValueError('all-replica full backup did not complete')
    receipt = store.backup_receipt(journal['volume'], name, 2*1024**3)
    if receipt.get('incremental') is not False:
        raise ValueError('all-replica backup is not the declared full backup')
    journal['backups'][name]['object_receipt'] = receipt
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    return {'backup': name, 'backup_uid': obj['metadata']['uid'], 'source_volume_uid': journal['volume_uid'],
            'object_receipt_sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest(),
            'checkpoint': checkpoint}


def restore(lab, journal, create, point, name, payload, digest):
    snapshot_name, content_name = name+'-snapshot', name+'-content'
    journal.setdefault('snapshots', {})[snapshot_name] = {'backup': point['backup']}
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    snapshot = create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
        'metadata': {'name': snapshot_name, 'namespace': journal['namespace']},
        'spec': {'volumeSnapshotClassName': 'longhorn-snapshot', 'source': {'volumeSnapshotContentName': content_name}}})
    content = create({'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshotContent',
        'metadata': {'name': content_name}, 'spec': {'driver': 'driver.longhorn.io', 'deletionPolicy': 'Retain',
        'volumeSnapshotClassName': 'longhorn-snapshot', 'volumeSnapshotRef': {'name': snapshot_name,
        'namespace': journal['namespace'], 'uid': snapshot['metadata']['uid']},
        'source': {'snapshotHandle': 'bak://'+journal['volume']+'/'+point['backup']}}})
    journal['snapshots'][snapshot_name].update(content=content_name, content_uid=content['metadata']['uid'])
    lab.write_private(lab.DIRECTORY/RECORD, journal)
    def snapshot_ready():
        current = lab.get('volumesnapshots.snapshot.storage.k8s.io', snapshot_name, journal['namespace'])
        if not current or current['metadata']['uid'] != snapshot['metadata']['uid']:
            raise ValueError('all-replica restore snapshot identity changed')
        state = current.get('status', {})
        if state.get('error'):
            raise ValueError('all-replica restore snapshot failed')
        return state.get('readyToUse') and state.get('boundVolumeSnapshotContentName') == content_name
    lab.wait('all-replica-restore-snapshot-'+name, 600, snapshot_ready)
    lab.bind_snapshots(journal)
    row = create_claim(lab, journal, create, name, snapshot_name)
    if row['volume'] == journal['volume'] or row['volume_uid'] == journal['volume_uid']:
        raise ValueError('all-replica restoration reused the failed source identity')
    client(lab, journal, create, name, name, lab.WORKERS[1])
    lab.wait('all-replica-restored-three-raw-replicas-'+name, 900, lambda: lab.healthy_replicas(row['volume']))
    lab.verify_restore_capacity(journal, name, 2*1024**3)
    actual = lab.kubectl('-n', journal['namespace'], 'exec', name, '--', 'python3', '-c',
                         lab.payload_program(payload, read_only=True)).stdout.strip()
    if actual != digest:
        raise ValueError('all-replica backup restoration changed the original payload')
    verify_ledger(lab, journal, name, point['checkpoint'])
    return row


def retire_preflight(lab, journal, name):
    """Use the declared Delete class; never force-delete a native volume."""
    row = journal['claims'][name]
    for kind, object_name in (('pod', name), ('pvc', name)):
        current = lab.get(kind, object_name, journal['namespace'])
        expected = journal['objects'][('Pod/' if kind == 'pod' else 'PersistentVolumeClaim/')+object_name]
        if current:
            if current['metadata']['uid'] != expected:
                raise ValueError('preflight restore object replaced before native cleanup')
            lab.remove(current)
        lab.wait('all-replica-preflight-'+kind+'-retired', 180,
                 lambda: lab.get(kind, object_name, journal['namespace']) is None)
    lab.wait('all-replica-preflight-delete-pv', 180, lambda: lab.get('pv', row['pv']) is None)
    lab.wait('all-replica-preflight-delete-native', 180,
             lambda: lab.get('volumes.longhorn.io', row['volume'], NS) is None)
    print('LONGHORN_V2_ALL_REPLICAS_PREFLIGHT_RESTORE_RETIRED', flush=True)


def cleanup(lab, record=None, *, relay_pid):
    record = record or lab.DIRECTORY/RECORD
    if record.is_symlink():
        raise ValueError('symlink all-replica recovery journal')
    if record.exists():
        journal = json.loads(record.read_text())
        proof = journal.get(KEY)
        if proof:
            if sys.exc_info()[0] is not None:
                try:
                    load('longhorn-v2-manager-loss.py').diagnostics(lab, journal)
                except Exception as error:
                    print('LONGHORN_PRIVATE_DIAGNOSTIC_UNAVAILABLE type='+type(error).__name__, flush=True)
            lab.check_business_graph(journal)
            restore_disks(lab, journal, proof)
            lab.installation_health()
            print('LONGHORN_V2_ALL_REPLICAS_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)
    # Deletion does not require choosing a failed replica. The fixed cleanup
    # proves original UID ownership, fences all clients and lets CSI Delete
    # reclaim the failed source only after our physical fault was restored.
    lab.cleanup_business(record, relay_pid=relay_pid)


def scenario(lab, relay_pid, store, configuration):
    lab.installation_health()
    if lab.get('volumes.longhorn.io', namespace=NS)['items']:
        raise ValueError('all-replica scenario requires an empty exclusive business boundary')
    sc = lab.get('storageclass', 'longhorn-delete')
    if (not sc or sc.get('provisioner') != 'driver.longhorn.io' or sc.get('reclaimPolicy') != 'Delete' or
            sc.get('parameters', {}).get('dataEngine') != 'v2' or
            sc.get('metadata', {}).get('labels', {}).get('kubeauto.io/component') != 'longhorn'):
        raise ValueError('all-replica scenario requires the product V2 Delete class')
    record = lab.DIRECTORY/RECORD
    if record.exists() or record.is_symlink():
        raise ValueError('prior business journal requires scoped recovery')
    namespace = 'longhorn-business-'+secrets.token_hex(8)
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})).stdout)
    journal = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
               'storage_class': 'longhorn-delete', 'reclaim_policy': 'Delete'}
    lab.write_private(record, journal)
    create = lambda obj: lab.create_business_object(journal, record, obj)
    with lab.scoped_cleanup(lambda: cleanup(lab, relay_pid=relay_pid)):
        journal['defaults'] = lab.wait('all-replica-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
        lab.write_private(record, journal)
        create_claim(lab, journal, create, 'data')
        original_writer = client(lab, journal, create, 'writer', 'data', lab.WORKERS[0])
        payload = lab.owner()['run_id']
        digest = hashlib.sha256(payload.encode()*262144).hexdigest()
        actual = lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c', lab.payload_program(payload)).stdout.strip()
        if actual != digest:
            raise ValueError('all-replica original payload was not fsync confirmed')
        lab.wait('all-replica-original-three-raw-replicas', 600, lambda: lab.healthy_replicas(journal['volume']))
        checkpoint = ledger(lab, journal, 'writer', payload+':all-replicas', '/data/fault-ledger')
        # No source writes occur after this finite checkpoint. Backup RPO is
        # exactly the acknowledged prefix, not an earlier unknown point.
        point = backup(lab, journal, relay_pid, store, configuration, checkpoint)
        preflight = restore(lab, journal, create, point, 'preflight-restore', payload, digest)
        retire_preflight(lab, journal, 'preflight-restore')
        point['preflight_restore_verified'] = True
        point['preflight_volume_uid'] = preflight['volume_uid']
        print('LONGHORN_V2_ALL_REPLICAS_BACKUP_RESTORE_TESTED '+json.dumps({
            'source_volume_uid': journal['volume_uid'], 'backup': point['backup'], 'backup_uid': point['backup_uid'],
            'preflight_volume_uid': preflight['volume_uid'], 'checkpoint': checkpoint['summary']}, sort_keys=True), flush=True)
        proof = inject(lab, journal, point)
        # inject() has proved actual I/O failure and native refusal. The
        # controller may immediately move faulted -> unknown while detaching
        # usable replicas; that transition is not a salvage operation.
        fenced = fence_source(lab, journal, proof, original_writer)
        restore_disks(lab, journal, proof)
        restored = restore(lab, journal, create, point, 'postfault-restore', payload, digest)
        fresh = ledger(lab, journal, 'postfault-restore', payload+':all-replicas:recovered', '/data/all-replicas-recovered-ledger')
        verify_ledger(lab, journal, 'postfault-restore', fresh)
        pod = lab.get('pod', 'postfault-restore', namespace)
        if not pod or pod['metadata']['uid'] != journal['objects']['Pod/postfault-restore']:
            raise ValueError('all-replica restored client changed before cross-node remount')
        lab.remove(pod)
        lab.wait('all-replica-restored-writer-fenced', 180, lambda: lab.get('pod', 'postfault-restore', namespace) is None)
        client(lab, journal, create, 'remount-reader', 'postfault-restore', lab.WORKERS[2])
        actual = lab.kubectl('-n', namespace, 'exec', 'remount-reader', '--', 'python3', '-c',
                             lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest:
            raise ValueError('all-replica restored original bytes changed after cross-node remount')
        verify_ledger(lab, journal, 'remount-reader', checkpoint)
        verify_ledger(lab, journal, 'remount-reader', fresh)
        if source_fenced(lab, journal, proof) != fenced:
            raise ValueError('original source lost its proven consumer and native detach fence')
        outcome = {'source_volume_uid': journal['volume_uid'], 'restored_volume_uid': restored['volume_uid'],
            'backup': point['backup'], 'backup_uid': point['backup_uid'], 'preflight_restore_verified': True,
            'preflight_volume_uid': point['preflight_volume_uid'],
            'original': checkpoint, 'fresh': fresh, 'payload_bytes': len(payload.encode())*262144,
            'payload_sha256': digest, 'source_fenced': fenced, 'cross_node': True,
            'restore_node': 'longhorn-node-'+lab.WORKERS[1].rsplit('.', 1)[1],
            'remount_node': 'longhorn-node-'+lab.WORKERS[2].rsplit('.', 1)[1],
            'refusal': proof['refusal']}
        print('LONGHORN_V2_ALL_REPLICAS_RECOVERY '+json.dumps(outcome, sort_keys=True), flush=True)
    lab.installation_health()
    print('LONGHORN_V2_ALL_REPLICAS_BUSINESS_PASS', flush=True)


def business(lab, relay_pid):
    store = lab.backup_store()
    # Keep backup data and transport until fault and business recovery finish.
    with lab.scoped_cleanup(store.cleanup):
        store.prepare()
        store.qualify()
        scenario(lab, relay_pid, store, store.product_configuration())


def verify_log(log):
    def one(prefix):
        rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines() if s.startswith(prefix+' ')]
        if len(rows) != 1:
            raise ValueError('all-replica requires one complete observation: '+prefix)
        return rows[0]
    refused = one('LONGHORN_V2_ALL_REPLICAS_REFUSED')
    offline = one('LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE')
    backup = one('LONGHORN_V2_ALL_REPLICAS_BACKUP_RESTORE_TESTED')
    source_io = one('LONGHORN_V2_ALL_REPLICAS_SOURCE_IO')
    outcome = one('LONGHORN_V2_ALL_REPLICAS_RECOVERY')
    fenced = one('LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED')
    if (not source_io.get('pod_uid') or source_io.get('volume_uid') != refused.get('volume_uid') or
            source_io.get('path') != '/data/confirmed' or source_io.get('offset') != 0 or
            source_io.get('length') != 4096 or source_io.get('direct') is not True or
            source_io.get('read_only') is not True or
            not source_io_failed({'attempts': source_io.get('attempts', [])}) or
            not (log.index('LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE ') <
                 log.index('LONGHORN_V2_ALL_REPLICAS_SOURCE_IO ') <
                 log.index('LONGHORN_V2_ALL_REPLICAS_REFUSED '))):
        raise ValueError('all-replica lacks actual uncached source I/O before native refusal')
    if (not refused.get('volume_uid') or refused['robustness'] != 'faulted' or refused['autoSalvage'] is not False or
            refused['salvageRequested'] is not False or refused['salvageExecuted'] is not False or
            len(refused['replicas']) != 3 or len({r['uid'] for r in refused['replicas'].values()}) != 3 or
            refused.get('engine_state') not in ('error', 'stopped') or
            any(not r.get('healthyAt') for r in refused['replicas'].values()) or
            outcome['source_volume_uid'] != refused['volume_uid'] or not outcome.get('restored_volume_uid') or
            outcome['restored_volume_uid'] == outcome['source_volume_uid'] or not outcome.get('backup_uid') or
            outcome['preflight_restore_verified'] is not True or outcome.get('source_fenced') != fenced or
            outcome['cross_node'] is not True or outcome['restore_node'] == outcome['remount_node'] or
            outcome.get('refusal') != refused):
        raise ValueError('all-replica lacks actual faulted refusal or independent backup recovery')
    if (set(offline) != {'192.168.47.146', '192.168.47.147', '192.168.47.148'} or
            {r['replica_uid'] for r in offline.values()} != {r['uid'] for r in refused['replicas'].values()} or
            any(r['original']['state'] != 'running' or r['offline'] != dict(r['original'], state='offline') for r in offline.values()) or
            backup != {'source_volume_uid': outcome['source_volume_uid'], 'backup': outcome['backup'],
                'backup_uid': outcome['backup_uid'], 'preflight_volume_uid': outcome['preflight_volume_uid'],
                'checkpoint': outcome['original']['summary']} or
            backup['preflight_volume_uid'] in (outcome['source_volume_uid'], outcome['restored_volume_uid']) or
            log.index('LONGHORN_V2_ALL_REPLICAS_BACKUP_RESTORE_TESTED ') > log.index('LONGHORN_V2_ALL_REPLICAS_DISKS_OFFLINE ')):
        raise ValueError('all-replica lacks all three actual disk losses or its own prior restore-tested backup')
    expected_fence = {'volume_uid': refused['volume_uid'], 'engine_uid': refused['engine_uid'],
        'writer_uid': source_io['pod_uid'], 'writer_absent': True, 'volume_state': 'detached',
        'engine_state': 'stopped', 'engine_desire': 'stopped', 'current_node': '', 'current_engine_node': '',
        'salvageRequested': False, 'salvageExecuted': False}
    if (fenced != expected_fence or not (log.index('LONGHORN_V2_ALL_REPLICAS_REFUSED ') <
            log.index('LONGHORN_V2_ALL_REPLICAS_SOURCE_FENCED ') <
            log.index('LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3'))):
        raise ValueError('all-replica source was not natively fenced before hardware recovery')
    faults, control = load('longhorn-faults.py'), load('longhorn-control-faults.py')
    original, fresh = outcome['original'], outcome['fresh']
    if (not original['token'].endswith(':all-replicas') or fresh['token'] != original['token']+':recovered' or
            original['path'] != '/data/fault-ledger' or fresh['path'] != '/data/all-replicas-recovered-ledger'):
        raise ValueError('all-replica backup and recovered ledgers differ')
    for records in (original, fresh):
        checked = control.confirmed(records['records'], records['token'], faults)['summary']
        if records['summary'] != checked or not isinstance(records['retries'], list):
            raise ValueError('all-replica fsync acknowledgement prefix differs')
    token = original['token'].removesuffix(':all-replicas')
    if (outcome['payload_bytes'] != len(token.encode())*262144 or
            outcome['payload_sha256'] != hashlib.sha256(token.encode()*262144).hexdigest()):
        raise ValueError('all-replica original payload differs from confirmed run identity')
    required = ['LONGHORN_V2_ALL_REPLICAS_PREFLIGHT_RESTORE_RETIRED',
        'LONGHORN_V2_ALL_REPLICAS_FAULT_RESTORED_BEFORE_CLEANUP', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
        'LONGHORN_BACKUP_STORE_CLEAN_PASS', 'LONGHORN_V2_ALL_REPLICAS_BUSINESS_PASS']
    if (any(log.splitlines().count(marker) != 1 for marker in required) or
            'LONGHORN_V2_ALL_REPLICAS_DISKS_RESTORED count=3' not in log.splitlines() or
            re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M)):
        raise ValueError('all-replica lacks recovery-before-cleanup or has failure markers')
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify', 'cleanup'))
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log:
            parser.error('verify requires --log')
        verify_log(args.log.read_text())
        return
    if args.relay_pid <= 0:
        parser.error('mutation requires a live relay')
    _, lab, _ = load('longhorn-v2-backups.py').fixture()
    lab.require_lease()
    if args.mode == 'cleanup':
        cleanup(lab, relay_pid=args.relay_pid)
        lab.backup_store().cleanup()
    else:
        business(lab, args.relay_pid)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-all-replicas-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            diagnostic = stream.name
        print('LONGHORN_V2_ALL_REPLICAS_FAIL diagnostic='+diagnostic, flush=True)
        sys.exit(1)
