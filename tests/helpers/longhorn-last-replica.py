#!/usr/bin/env python3
"""LONGHORN-23: last-replica deletion refusal and explicit V1 backup recovery.

Only native replica processes are lost. No disk, filesystem or failedAt is
modified, and no salvage request selects a supposed last-good replica. The
separate maintenance gate owns last-replica drain/PDB qualification.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import signal
import sys
import time

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v1_last_replica'
INTENT = 'longhorn-last-replica-intent.json'
COMPLETE = 'longhorn-last-replica-complete.json'
OWNER = 'last_replica_owner'
STORE = ('longhorn-backup-store.json', 'longhorn-backup-store')
FOREIGN = ('longhorn-request-input.json', 'longhorn-reclaim-class.json', 'longhorn-recurring-credentials.json',
    'longhorn-recurring-runtime.json', 'longhorn-encryption-key.json', 'longhorn-security-tls.json',
    'longhorn-network-probes.json', 'longhorn-backup-refusal.json', 'longhorn-dr-backup-consumer.json',
    'longhorn-ownership-csi.json', 'longhorn-ownership-preinstall.json', 'longhorn-ownership-class.json',
    'longhorn-ownership-controller.json', 'longhorn-ownership-request.json', 'longhorn-maintenance.json',
    'longhorn-node-failure.json', 'longhorn-system-recovery.json', 'longhorn-v1-dr.json',
    'longhorn-upgrade-lifecycle.json', 'longhorn-v1-upgrade-lifecycle.json', 'longhorn-v2-linked-clone.json',
    'longhorn-v2-linked-lifecycle.json')


def load(name):
    spec = importlib.util.spec_from_file_location('last23_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def emit(marker, value):
    print(marker+' '+json.dumps(value, sort_keys=True), flush=True)


def save(lab, journal):
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def present(path):
    return path.exists() or path.is_symlink()


def read_owner(lab, path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('last-replica ownership receipt is not its regular file')
    row = json.loads(path.read_text())
    state = lab.owner()
    if (row.get('run_id') != state['run_id'] or row.get('cluster_uid') != state['cluster_uid'] or
            not isinstance(row.get('token'), str) or len(row['token']) != 32 or
            any(c not in '0123456789abcdef' for c in row['token'])):
        raise ValueError('last-replica ownership receipt differs from the current lease')
    return row


def owner_id(row):
    return {key: row[key] for key in ('run_id', 'cluster_uid', 'token')}


def foreign_absent(lab):
    if any(present(lab.DIRECTORY/name) for name in FOREIGN):
        raise ValueError('last-replica scope contains another operation receipt')


def materials_absent(lab):
    return not any(present(lab.DIRECTORY/name) for name in (RECORD, *STORE))


def begin(lab):
    foreign_absent(lab)
    if present(lab.DIRECTORY/INTENT) or not materials_absent(lab):
        raise ValueError('last-replica admission refuses existing intent, business or backup material')
    backups = load('longhorn-backups.py')
    if (any(lab.get(kind, namespace=NS)['items'] for kind in ('volumes.longhorn.io', *backups.BACKUP_RESOURCES)) or
            any(row.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for row in lab.get('pv')['items'])):
        raise ValueError('last-replica admission refuses existing native data or backup consumers')
    completed = lab.DIRECTORY/COMPLETE
    if present(completed):
        if read_owner(lab, completed).get('stage') != 'complete':
            raise ValueError('last-replica retired receipt is incomplete')
        completed.unlink()
    state = lab.owner()
    row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'token': secrets.token_hex(16),
           'stage': 'store-prepare-intent'}
    lab.write_private(lab.DIRECTORY/INTENT, row)
    return row


class StoreLab:
    """Tag the private Store's FIRST receipt write, including partial prepare."""
    def __init__(self, lab, identity):
        self.original, self.identity = lab, identity

    def __getattr__(self, name):
        return getattr(self.original, name)

    def write_private(self, path, row):
        if path == self.original.DIRECTORY/STORE[0]:
            if OWNER in row and row[OWNER] != self.identity:
                raise ValueError('last-replica private store receipt changed owner')
            row[OWNER] = self.identity
        self.original.write_private(path, row)


def owned_materials(lab, identity):
    """Validate ALL shared receipts before restoring or deleting any object."""
    foreign_absent(lab)
    for name in (RECORD, STORE[0]):
        path = lab.DIRECTORY/name
        if present(path):
            if path.is_symlink() or not path.is_file() or json.loads(path.read_text()).get(OWNER) != identity:
                raise ValueError('last-replica cleanup refuses foreign shared material')
    directory = lab.DIRECTORY/STORE[1]
    if directory.is_symlink() or present(directory) and not present(lab.DIRECTORY/STORE[0]):
        raise ValueError('last-replica store directory has no owned receipt')


def replicas(lab, journal):
    return [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == journal['volume']]


def volume(lab, journal):
    obj = lab.get('volumes.longhorn.io', journal['volume'], NS)
    if not obj or obj['metadata']['uid'] != journal['volume_uid'] or obj['spec'].get('dataEngine') != 'v1':
        raise ValueError('last-replica source volume identity changed')
    return obj


def last_replica_guard(lab, journal):
    """A server dry-run never destroys data even if upstream rejects incorrectly."""
    maintenance = load('longhorn-maintenance.py')
    # This isolated helper instance writes its policy intent into our journal.
    maintenance.remember = save
    maintenance.temporary_replica_balance(lab, journal, journal)
    maintenance.scale(lab, journal, 1)
    selected = lab.wait('last23-only-native-replica', 300,
        lambda: replicas(lab, journal) if len(replicas(lab, journal)) == 1 else None)
    row = selected[0]
    if (row['spec'].get('failedAt') or not row['spec'].get('healthyAt') or
            row.get('status', {}).get('currentState') != 'running' or row['metadata'].get('deletionTimestamp')):
        raise ValueError('last-replica deletion guard requires the one healthy original replica')
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'dryRun': ['All'],
        'preconditions': {'uid': row['metadata']['uid'], 'resourceVersion': row['metadata']['resourceVersion']}}
    result = lab.kubectl('delete', '--raw', '/apis/longhorn.io/v1beta2/namespaces/'+NS+'/replicas/'+row['metadata']['name'],
                         '-f', '-', stdin=json.dumps(options), check=False)
    expected = ('no other healthy replica available for volume '+journal['volume']+', cannot delete replica '+
                row['metadata']['name']+' since it may still contain data for recovery')
    if result.returncode == 0 or expected not in result.stdout+result.stderr:
        raise ValueError('last-replica webhook did not explicitly refuse the UID-guarded dry-run deletion')
    current = replicas(lab, journal)
    if (len(current) != 1 or current[0]['metadata']['uid'] != row['metadata']['uid'] or
            current[0]['metadata'].get('deletionTimestamp')):
        raise ValueError('last-replica dry-run changed the protected replica')
    proof = {'volume_uid': journal['volume_uid'], 'replica': row['metadata']['name'],
             'replica_uid': row['metadata']['uid'], 'dryRun': ['All'], 'refusal': expected}
    journal['last_replica_deletion_guard'] = proof
    save(lab, journal)
    maintenance.scale(lab, journal, 3)
    maintenance.restore_replica_balance(lab, journal, journal)
    emit('LONGHORN_LAST_REPLICA_DELETE_REFUSED', proof)
    return proof


def node_state(lab):
    result = {}
    for ip in lab.WORKERS:
        name = 'longhorn-node-'+ip.rsplit('.', 1)[1]
        row = lab.get('nodes.longhorn.io', name, NS)
        if not row or row['metadata'].get('deletionTimestamp') or not isinstance(row['spec'].get('allowScheduling'), bool):
            raise ValueError('last-replica fault requires original Longhorn scheduling identities')
        result[name] = {'uid': row['metadata']['uid'], 'allowScheduling': row['spec']['allowScheduling']}
    return result


def scheduling(lab, name, saved, desired):
    row = lab.get('nodes.longhorn.io', name, NS)
    if (not row or row['metadata']['uid'] != saved['uid'] or
            row['spec'].get('allowScheduling') not in (False, saved['allowScheduling'])):
        raise ValueError('last-replica scheduling ownership or state changed')
    if row['spec']['allowScheduling'] != desired:
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': saved['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': row['metadata']['resourceVersion']},
                 {'op': 'test', 'path': '/spec/allowScheduling', 'value': row['spec']['allowScheduling']},
                 {'op': 'replace', 'path': '/spec/allowScheduling', 'value': desired}]
        lab.kubectl('-n', NS, 'patch', 'nodes.longhorn.io', name, '--type=json',
                    '--patch-file=/dev/stdin', stdin=json.dumps(patch))


def restore_scheduling(lab, journal):
    proof = journal.get(KEY)
    if not proof:
        return
    state = lab.owner()
    if any(proof.get(key) != state[key] for key in ('run_id', 'cluster_uid', 'namespace_uid')):
        raise ValueError('last-replica recovery lease identity differs')
    current = node_state(lab)
    if proof['volume_uid'] != journal['volume_uid'] or set(proof['nodes']) != set(current):
        raise ValueError('last-replica scheduling journal differs from original source')
    for name, saved in proof['nodes'].items():
        if (current[name]['uid'] != saved['uid'] or current[name]['allowScheduling'] not in (False, saved['allowScheduling'])):
            raise ValueError('last-replica scheduling recovery has a foreign node or policy')
    for name, saved in proof['nodes'].items():
        if name in proof['node_intents']:
            scheduling(lab, name, saved, saved['allowScheduling'])
    if node_state(lab) != proof['nodes']:
        raise ValueError('last-replica original scheduling restoration unproven')
    proof['scheduling_restored'] = True
    save(lab, journal)
    print('LONGHORN_LAST_REPLICA_SCHEDULING_RESTORED count=3', flush=True)


def faulted(lab, journal):
    proof = journal[KEY]
    load('longhorn-v2-all-replicas.py').policy(lab, proof['policy'])
    current = volume(lab, journal)
    selected = replicas(lab, journal)
    if {r['metadata']['name']: r['metadata']['uid'] for r in selected} != {
            name: item['replica_uid'] for name, item in proof['processes'].items()}:
        raise ValueError('all-replica refusal replaced or deleted an original replica')
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items']
               if e['spec'].get('volumeName') == journal['volume']]
    if len(engines) != 1 or engines[0]['metadata']['uid'] != proof['engine']['uid']:
        raise ValueError('all-replica refusal original engine changed')
    engine = engines[0]
    if engine['spec'].get('salvageRequested') or engine.get('status', {}).get('salvageExecuted'):
        raise ValueError('all-replica scenario unexpectedly selected a salvage candidate')
    if current.get('status', {}).get('robustness') != 'faulted' or not all(
            r['spec'].get('failedAt') and r['spec'].get('healthyAt') for r in selected):
        return None
    return {'volume_uid': journal['volume_uid'], 'robustness': 'faulted', 'engine_uid': proof['engine']['uid'],
        'autoSalvage': False, 'salvageRequested': False, 'salvageExecuted': False,
        'replicas': {r['metadata']['name']: {'uid': r['metadata']['uid'], 'healthyAt': r['spec']['healthyAt'],
            'failedAt': r['spec']['failedAt']} for r in selected}}


def inject(lab, journal, point):
    if KEY in journal:
        raise ValueError('prior last-replica fault intent requires recovery; never replay')
    shared = load('longhorn-v2-all-replicas.py')
    if (not point.get('preflight_restore_verified') or not point.get('preflight_volume_uid') or
            point['preflight_volume_uid'] == journal['volume_uid'] or point['source_volume_uid'] != journal['volume_uid']):
        raise ValueError('last-replica fault requires this exact restore-tested backup')
    receipt = journal['backups'][point['backup']]['object_receipt']
    backup = lab.get('backups.longhorn.io', point['backup'], NS)
    if (not backup or backup['metadata']['uid'] != point['backup_uid'] or
            receipt.get('volume') != journal['volume'] or receipt.get('backup') != point['backup'] or
            hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest() != point['object_receipt_sha256']):
        raise ValueError('last-replica restore-tested backup objects changed')
    checked = load('longhorn-control-faults.py').confirmed(point['checkpoint']['records'],
        point['checkpoint']['token'], load('longhorn-faults.py'))
    if checked['summary'] != point['checkpoint']['summary']:
        raise ValueError('last-replica backup ACK prefix differs')
    common = load('longhorn-v1-stability.py')
    engine = common.source(lab, journal)
    selected = replicas(lab, journal)
    faults = load('longhorn-faults.py')
    processes = {r['metadata']['name']: faults.replica_process(lab, journal, r) for r in selected}
    if len(processes) != 3 or len({p['node'] for p in processes.values()}) != 3:
        raise ValueError('last-replica fault requires three independent original replica processes')
    # Entire read-only scope is resolved twice before any mutation. Each delete
    # performs another double read and supplies the native UUID precondition.
    if processes != {r['metadata']['name']: faults.replica_process(lab, journal, r) for r in selected}:
        raise ValueError('all-replica native UUID scope changed before injection')
    proof = {key: lab.owner()[key] for key in ('run_id', 'cluster_uid', 'namespace_uid')}
    proof.update(volume_uid=journal['volume_uid'], policy=shared.policy(lab), engine=engine,
                 processes=processes, nodes=node_state(lab), node_intents=[], process_intents=[], results={}, backup=point)
    journal[KEY] = proof
    save(lab, journal)
    for name, saved in proof['nodes'].items():
        proof['node_intents'].append(name)
        save(lab, journal)
        scheduling(lab, name, saved, False)
    if any(item['allowScheduling'] for item in node_state(lab).values()):
        raise ValueError('all-replica rebuild scheduling not isolated')
    proof['process_intents'] = list(processes)
    save(lab, journal)
    first_error = None
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(faults.stop_replica_process, lab, journal, row): row['metadata']['name'] for row in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                native = future.result()
                if native != processes[name]:
                    raise ValueError('all-replica deleted process differs from captured UUID')
                proof['results'][name] = native
            except Exception as error:
                proof.setdefault('process_errors', {})[name] = type(error).__name__
                first_error = first_error or error
            save(lab, journal)
    if first_error:
        raise first_error
    emit('LONGHORN_LAST_REPLICA_PROCESSES_STOPPED', proof['results'])
    return proof


def refusal_program(token):
    return '''import json,os
token='''+repr(token)+'''
print(json.dumps({'token':token,'phase':'begin'}),flush=True)
try:
 fd=os.open('/data/unacknowledged-'+token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 try:
  os.write(fd,b'unconfirmed-attempt');os.fsync(fd)
 finally:os.close(fd)
 directory=os.open('/data',os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(directory)
 finally:os.close(directory)
 print(json.dumps({'token':token,'phase':'ack'}),flush=True)
except OSError as error:
 print(json.dumps({'token':token,'phase':'refused','errno':error.errno}),flush=True)
'''


def io_refusal(lab, journal, writer_uid):
    current = lab.get('pod', 'writer', journal['namespace'])
    if not current or current['metadata']['uid'] != writer_uid:
        raise ValueError('all-replica I/O probe writer identity changed')
    token = secrets.token_hex(16)
    result = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-u', '-c',
                         refusal_program(token), timeout=75)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    if (len(rows) != 2 or rows[0] != {'token': token, 'phase': 'begin'} or
            rows[1].get('token') != token or rows[1].get('phase') != 'refused' or
            rows[1].get('errno') not in (5, 19, 30, 107, 110)):
        raise ValueError('all-replica I/O refusal lacks an explicit storage error without ACK')
    if lab.get('pod', 'writer', journal['namespace'])['metadata']['uid'] != writer_uid:
        raise ValueError('all-replica I/O probe writer was replaced')
    return rows[1]


def cleanup(lab, pid):
    intent = lab.DIRECTORY/INTENT
    if not present(intent):
        retired = read_owner(lab, lab.DIRECTORY/COMPLETE)
        foreign_absent(lab)
        if retired.get('stage') != 'complete' or not materials_absent(lab):
            raise ValueError('last-replica repeated cleanup lacks retired ownership and an absent shared scope')
        print('LONGHORN_LAST_REPLICA_RECOVERY_SCOPED_CLEAN_PASS scheduling=true materials=absent retired_owner=true', flush=True)
        return
    ownership = read_owner(lab, intent)
    identity = owner_id(ownership)
    owned_materials(lab, identity)
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():
        raise ValueError('symlink last-replica recovery journal')
    if not path.exists():
        lab.backup_store().cleanup()
        if not materials_absent(lab):
            raise ValueError('last-replica partial store cleanup retains shared material')
        ownership['stage'] = 'complete'
        lab.write_private(intent, ownership)
        intent.rename(lab.DIRECTORY/COMPLETE)
        print('LONGHORN_LAST_REPLICA_RECOVERY_SCOPED_CLEAN_PASS scheduling=true materials=absent', flush=True)
        return
    journal = json.loads(path.read_text())
    lab.check_business_graph(journal)
    restore_scheduling(lab, journal)
    load('longhorn-maintenance.py').restore_replica_balance(lab, journal, journal)
    if journal.get('backups') and not journal.get(KEY, {}).get('verified'):
        print('LONGHORN_LAST_REPLICA_MATERIALS_RETAINED recovery_unverified=true', flush=True)
        raise ValueError('last-replica recovery unverified; preserve source, ACK and backup material')
    lab.installation_health()
    lab.cleanup_business(relay_pid=pid)
    lab.backup_store().cleanup()
    if not materials_absent(lab):
        raise ValueError('last-replica cleanup retains owned business/store material')
    ownership['stage'] = 'complete'
    lab.write_private(intent, ownership)
    intent.rename(lab.DIRECTORY/COMPLETE)
    print('LONGHORN_LAST_REPLICA_RECOVERY_SCOPED_CLEAN_PASS scheduling=true materials=owned', flush=True)


def scenario(lab, pid, store, configuration):
    shared = load('longhorn-v2-all-replicas.py')
    original_policy = shared.policy(lab)  # Reject foreign policy before product reassertion.
    lab.run_requests(pid, [], backup_configuration=configuration)
    policy = shared.policy(lab, original_policy)
    lab.installation_health()
    sc = lab.get('storageclass', 'longhorn-delete')
    if (lab.get('volumes.longhorn.io', namespace=NS)['items'] or not sc or
            sc.get('provisioner') != 'driver.longhorn.io' or sc.get('reclaimPolicy') != 'Delete' or
            sc.get('parameters', {}).get('dataEngine') != 'v1' or
            sc['metadata'].get('labels', {}).get('kubeauto.io/component') != 'longhorn'):
        raise ValueError('last-replica gate requires the empty original product V1 Delete class')
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink():
        raise ValueError('prior last-replica journal requires scoped recovery')
    namespace = 'longhorn-business-'+secrets.token_hex(8)
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})).stdout)
    journal = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
               'storage_class': 'longhorn-delete', 'reclaim_policy': 'Delete',
               OWNER: owner_id(read_owner(lab, lab.DIRECTORY/INTENT))}
    save(lab, journal)
    create = lambda obj: lab.create_business_object(journal, path, obj)
    journal['defaults'] = lab.wait('last23-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
    save(lab, journal)
    shared.create_claim(lab, journal, create, 'data')
    writer = shared.client(lab, journal, create, 'writer', 'data', lab.WORKERS[0])
    payload = lab.owner()['run_id']
    digest = hashlib.sha256(payload.encode()*262144).hexdigest()
    if lab.kubectl('-n', namespace, 'exec', 'writer', '--', 'python3', '-c', lab.payload_program(payload)).stdout.strip() != digest:
        raise ValueError('last-replica original bytes lack fsync confirmation')
    last_replica_guard(lab, journal)
    checkpoint = shared.ledger(lab, journal, 'writer', payload+':all-replicas', '/data/fault-ledger')
    point = shared.backup(lab, journal, pid, store, configuration, checkpoint)
    preflight = shared.restore(lab, journal, create, point, 'preflight-restore', payload, digest)
    shared.retire_preflight(lab, journal, 'preflight-restore')
    point.update(preflight_restore_verified=True, preflight_volume_uid=preflight['volume_uid'])
    emit('LONGHORN_LAST_REPLICA_BACKUP_RESTORE_TESTED', point)
    proof = inject(lab, journal, point)
    if proof['policy'] != policy:
        raise ValueError('last-replica original product autoSalvage policy replaced')
    # The write creates outstanding I/O, so V1's last-replica timeout is
    # exercised. A timed-out exec never counts as a refusal.
    refusal = io_refusal(lab, journal, writer['metadata']['uid'])
    proof['refusal'] = lab.wait('last23-original-native-faulted', 240, lambda: faulted(lab, journal))
    proof['io_refusal'] = refusal
    save(lab, journal)
    emit('LONGHORN_LAST_REPLICA_IO_REFUSED', dict(proof['refusal'], io=refusal))
    restore_scheduling(lab, journal)
    for _ in range(3):
        if faulted(lab, journal) != proof['refusal']:
            raise ValueError('last-replica fault selected an unproven recovery candidate')
        time.sleep(2)
    old = lab.get('pod', 'writer', namespace)
    if not old or old['metadata']['uid'] != writer['metadata']['uid']:
        raise ValueError('last-replica source writer changed before normal retirement')
    lab.remove(old)
    lab.wait('last23-source-writer-retired', 180, lambda: lab.get('pod', 'writer', namespace) is None)
    restored = shared.restore(lab, journal, create, point, 'postfault-restore', payload, digest)
    fresh = shared.ledger(lab, journal, 'postfault-restore', payload+':all-replicas:recovered', '/data/all-replicas-recovered-ledger')
    shared.verify_ledger(lab, journal, 'postfault-restore', fresh)
    pod = lab.get('pod', 'postfault-restore', namespace)
    if not pod or pod['metadata']['uid'] != journal['objects']['Pod/postfault-restore']:
        raise ValueError('last-replica restored writer changed before remount')
    lab.remove(pod)
    lab.wait('last23-restored-writer-retired', 180, lambda: lab.get('pod', 'postfault-restore', namespace) is None)
    shared.client(lab, journal, create, 'remount-reader', 'postfault-restore', lab.WORKERS[2])
    if lab.kubectl('-n', namespace, 'exec', 'remount-reader', '--', 'python3', '-c',
                   lab.payload_program(payload, read_only=True)).stdout.strip() != digest:
        raise ValueError('last-replica restored bytes changed across worker remount')
    shared.verify_ledger(lab, journal, 'remount-reader', checkpoint)
    shared.verify_ledger(lab, journal, 'remount-reader', fresh)
    if faulted(lab, journal) != proof['refusal']:
        raise ValueError('last-replica source changed after independent restoration')
    if store.backup_receipt(journal['volume'], point['backup'], 2*1024**3) != journal['backups'][point['backup']]['object_receipt']:
        raise ValueError('last-replica external backup bytes changed')
    proof.update(verified=True, restored_volume_uid=restored['volume_uid'], original=checkpoint, fresh=fresh,
        payload_sha256=digest, payload_bytes=len(payload.encode())*262144, cross_node=True,
        restore_node='longhorn-node-'+lab.WORKERS[1].rsplit('.', 1)[1],
        remount_node='longhorn-node-'+lab.WORKERS[2].rsplit('.', 1)[1])
    save(lab, journal)
    emit('LONGHORN_LAST_REPLICA_RECOVERY', proof)
    cleanup(lab, pid)
    shared.policy(lab, policy)
    print('LONGHORN_LAST_REPLICA_SCOPED_CLEAN_PASS original_policy=true scheduling=true', flush=True)
    print('LONGHORN_LAST_REPLICA_PASS', flush=True)


def business(lab, pid):
    ownership = begin(lab)
    store = lab.backup_store()
    store.lab = StoreLab(lab, owner_id(ownership))
    store.prepare()
    store.qualify()
    scenario(lab, pid, store, store.product_configuration())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('business', 'clean'))
    parser.add_argument('--relay-pid', type=int, required=True)
    args = parser.parse_args()
    if args.relay_pid <= 0:
        parser.error('supervised relay PID required')
    lab = load('longhorn-lab.py')
    lab.require_lease()
    if args.action == 'business':
        business(lab, args.relay_pid)
    else:
        cleanup(lab, args.relay_pid)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(128+number))
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-last-replica-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            diagnostic = stream.name
        print('LONGHORN_LAST_REPLICA_FAIL diagnostic='+diagnostic, flush=True)
        sys.exit(1)
