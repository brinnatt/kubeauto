#!/usr/bin/env python3
"""Owned SCSI disk loss and active V2 rebuild QoS, with confirmed I/O.

Linux 6.8 scsi_sysfs.c permits running/offline transitions and revalidates the
same device when returned to running. No disk is removed, overwritten or
reformatted. QoS uses the pinned manager's actual CR fields, not the obsolete
field names on the 1.12.1 website.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v2_disk_fault'
SETTING = 'replica-rebuilding-bandwidth-limit'
FIELD = 'replicaRebuildingBandwidthLimit'
CONFIG = 'longhorn_v2_rebuild_bandwidth_limit'
CASES = ('single-disk', 'rebuild-qos')
FILL_BYTES = 1024**3


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host_script():
    # Ansible's script action transfers this product checker temporarily; the
    # worker does not contain the control host's repository. Carry the same
    # frozen checker in memory rather than assuming a persistent remote copy.
    checker = (Path(__file__).resolve().parents[2]/'roles/longhorn/files/block-check.py').read_text()
    return '''import hashlib,json,os,sys
from pathlib import Path
check={'__name__':'longhorn_block_check'}
exec(compile(%r,'<longhorn-block-check>','exec'),check)
r=json.load(sys.stdin)
def inspect():
 marker=Path('/etc/kubeauto/longhorn-block-disks.json')
 if marker.is_symlink():raise ValueError('symlink raw disk receipt')
 raw=marker.read_bytes();receipt=json.loads(raw)
 if receipt['cluster_uid']!=r['cluster_uid'] or receipt['node_uid']!=r['node_uid'] or set(receipt['disks'])!={r['disk']['name']}:
  raise ValueError('raw disk receipt differs from original exclusive node')
 disk=check['inspect_disk'](r['disk'],1,initial=False)
 if disk!=receipt['disks'][r['disk']['name']]:raise ValueError('raw disk receipt identity differs')
 device=Path(r['disk']['path']).resolve(strict=True)
 state=(Path('/sys/class/block')/device.name/'device/state').resolve(strict=True)
 if not str(state).startswith('/sys/devices/') or state.name!='state':raise ValueError('not a physical SCSI state')
 return {'machine_id':Path('/etc/machine-id').read_text().strip(),
         'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
         'receipt_sha256':hashlib.sha256(raw).hexdigest(),'disk':disk,
         'state_path':str(state),'state':state.read_text().strip()}
current=inspect()
if current['machine_id']!=r['machine_id']:raise ValueError('raw disk host differs from lease')
if r['operation']!='probe':
 expected=r['expected']
 if {k:v for k,v in current.items() if k!='state'}!={k:v for k,v in expected.items() if k!='state'}:
  raise ValueError('raw disk identity changed before state transition')
 desired={'offline':'offline','restore':'running'}.get(r['operation'])
 if desired is None:raise ValueError('unknown SCSI operation')
 if current['state'] not in ('running','offline'):raise ValueError('unexpected original SCSI state')
 if r['operation']=='offline' and current!=expected:raise ValueError('do not replay an uncertain offline write')
 if inspect()!=current:raise ValueError('raw disk changed during state check')
 if current['state']!=desired:Path(current['state_path']).write_text(desired+'\\n')
 current=inspect()
 if current['state']!=desired:raise ValueError('SCSI state transition did not converge')
print(json.dumps(current,sort_keys=True))
''' % checker


def host_call(lab, proof, operation):
    if proof['address'] not in lab.WORKERS:
        raise ValueError('raw disk fault host is outside the lease')
    state = lab.owner()
    request = {'operation': operation, 'cluster_uid': state['cluster_uid'],
        'node_uid': proof['node_uid'], 'machine_id': state['raw_disks'][proof['address']]['machine_id'],
        'disk': proof['declaration']}
    if operation != 'probe':
        request['expected'] = proof['host']
    return json.loads(lab.remote(proof['address'], ['python3', '-c', host_script()],
                                 stdin=json.dumps(request), timeout=90).stdout)


def persist(lab, journal, proof):
    journal[KEY] = proof
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def volume(lab, journal):
    row = lab.get('volumes.longhorn.io', journal['volume'], NS)
    if not row or row['metadata']['uid'] != journal['volume_uid'] or row['spec'].get('dataEngine') != 'v2':
        raise ValueError('disk fault original V2 volume changed')
    return row


def boundary(lab, journal, case):
    if case not in CASES:
        raise ValueError('unsupported raw disk fault case')
    runtime = load('longhorn-v2-manager-loss.py').runtime(lab, journal)
    load('longhorn-v2-runtime-faults.py').native_consumers(lab, runtime)
    _, remote = load('longhorn-v2-control-faults.py').boundary(lab, journal)
    node = remote['parent']['node']
    addresses = [ip for ip in lab.WORKERS if node == 'longhorn-node-'+ip.rsplit('.', 1)[1]]
    if len(addresses) != 1:
        raise ValueError('disk fault node is outside the leased pool')
    address = addresses[0]
    config_path = lab.DIRECTORY/'config.yml'
    if config_path.is_symlink():
        raise ValueError('symlink disk fault configuration')
    config = yaml.safe_load(config_path.read_text())
    declarations = config['longhorn_block_disks'][address]
    native_node = lab.get('nodes.longhorn.io', node, NS)
    node_uid = lab.get('node', node)['metadata']['uid']
    replica = lab.get('replicas.longhorn.io', remote['replica'], NS)
    if len(declarations) != 1 or set(native_node['spec']['disks']) != {declarations[0]['name']}:
        raise ValueError('disk fault requires one exclusive declared raw disk')
    declaration = declarations[0]
    disk = native_node['spec']['disks'][declaration['name']]
    status = native_node['status']['diskStatus'][declaration['name']]
    owned = lab.owner()['raw_disks'][address]
    if (declaration['path'] != owned['path'] or declaration['identity'] != owned['identity'] or
            disk.get('path') != owned['path'] or disk.get('diskType') != 'block' or
            disk.get('diskDriver') != 'aio' or status.get('diskUUID') != replica['spec']['diskID'] or
            replica['metadata']['uid'] != remote['replica_uid']):
        raise ValueError('disk fault raw path or native disk/replica identity changed')
    proof = {'case': case, 'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'],
        'volume_uid': journal['volume_uid'], 'runtime': runtime, 'address': address,
        'node': node, 'node_uid': node_uid, 'declaration': copy.deepcopy(declaration),
        'disk_uuid': status['diskUUID'], 'replica': remote['replica'], 'replica_uid': remote['replica_uid']}
    proof['host'] = host_call(lab, proof, 'probe')
    if proof['host']['state'] != 'running' or proof['host']['disk']['size_bytes'] != owned['size_bytes']:
        raise ValueError('disk fault requires the original running leased capacity')
    return proof


def public_proof(proof):
    return {k: v for k, v in proof.items() if k != 'qos'}


def atomic(path, text):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


def setting(lab, proof):
    row = lab.get('settings.longhorn.io', SETTING, NS)
    if not row or row['metadata']['uid'] != proof['qos']['setting_uid']:
        raise ValueError('original rebuild QoS Setting was replaced')
    return json.loads(row['value'])


def apply_global(lab, journal, proof, relay_pid, limit):
    """Every global change goes through config.yml plus longhorn-setup."""
    qos = proof['qos']
    path = lab.DIRECTORY/'config.yml'
    current = path.read_text()
    if path.is_symlink() or current not in qos['allowed_inputs']:
        raise ValueError('QoS product input changed outside the owning transaction')
    config = yaml.safe_load(current)
    config[CONFIG] = limit
    desired = yaml.safe_dump(config, sort_keys=False)
    if desired not in qos['allowed_inputs']:
        qos['allowed_inputs'].append(desired)
    persist(lab, journal, proof)
    atomic(path, desired)
    lab.run_requests(relay_pid, [])
    observed = setting(lab, proof)
    if observed != dict(qos['setting_original'], v2=str(limit)):
        raise ValueError('product rebuild QoS did not converge or changed another engine')


def patch_override(lab, journal, expected, desired):
    current = volume(lab, journal)
    if current['spec'].get(FIELD, 0) != expected:
        raise ValueError('volume rebuild QoS changed outside the owning transaction')
    patches = [{'op': 'test', 'path': '/metadata/uid', 'value': journal['volume_uid']},
               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
               {'op': 'add', 'path': '/spec/'+FIELD, 'value': desired}]
    lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', journal['volume'], '--type=json',
                '-p', json.dumps(patches))
    if volume(lab, journal)['spec'].get(FIELD, 0) != desired:
        raise ValueError('volume rebuild QoS override did not converge')


def qos_prepare(lab, journal, proof, relay_pid):
    row = lab.get('settings.longhorn.io', SETTING, NS)
    original = json.loads(row['value'])
    if not original or 'v2' not in original or not str(original['v2']).isdigit():
        raise ValueError('invalid original V2 rebuild QoS setting')
    path = lab.DIRECTORY/'config.yml'
    current_volume = volume(lab, journal)
    proof['qos'] = {'input_original': path.read_text(), 'allowed_inputs': [path.read_text()],
        'setting_uid': row['metadata']['uid'], 'setting_original': original,
        'override_present': FIELD in current_volume['spec'], 'override_original': current_volume['spec'].get(FIELD, 0),
        'override_expected': current_volume['spec'].get(FIELD, 0), 'observations': []}
    persist(lab, journal, proof)
    apply_global(lab, journal, proof, relay_pid, 1)
    # Record both possible outcomes before the native write. Recovery refuses
    # any third value and never overwrites an outside operator's change.
    proof['qos']['override_expected'] = 2
    persist(lab, journal, proof)
    patch_override(lab, journal, proof['qos']['override_original'], 2)


def filler_program(read_only=False):
    return '''import hashlib,json,os,shutil,stat
path='/data/rebuild-qos-filler';size=%d;block=4*1024**2
digest=hashlib.sha256()
if %r:
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_size!=size:raise ValueError('rebuild filler size differs')
  for offset in range(0,size,block):
   data=os.pread(fd,block,offset)
   if len(data)!=block:raise ValueError('short rebuild filler read')
   digest.update(data)
 finally:os.close(fd)
else:
 if shutil.disk_usage('/data').free<size+256*1024**2:raise ValueError('insufficient bounded rebuild divergence budget')
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  for offset in range(0,size,block):
   data=os.urandom(block)
   if os.pwrite(fd,data,offset)!=block:raise ValueError('short rebuild filler write')
   digest.update(data)
   # Dirty page cache is charged to the 256 MiB client cgroup too.
   # Bound it without reducing the full rebuild divergence or its checksum.
   os.fsync(fd)
   os.posix_fadvise(fd,offset,block,os.POSIX_FADV_DONTNEED)
  os.fsync(fd)
 finally:os.close(fd)
 directory=os.open('/data',os.O_RDONLY|os.O_DIRECTORY)
 try:os.fsync(directory)
 finally:os.close(directory)
print(json.dumps({'bytes':size,'sha256':digest.hexdigest()},sort_keys=True))
''' % (FILL_BYTES, read_only)


def degraded(lab, journal, proof):
    row = lab.get('replicas.longhorn.io', proof['replica'], NS)
    engines = lab.get('engines.longhorn.io', namespace=NS)['items']
    if row and row['metadata']['uid'] != proof['replica_uid']:
        raise ValueError('failed raw-disk replica was replaced before observation')
    selected = [e for e in engines if e['spec'].get('volumeName') == journal['volume']]
    if len(selected) != 1 or selected[0]['metadata']['uid'] != next(iter(proof['runtime']['instances']['engines.longhorn.io'].values()))['uid']:
        raise ValueError('raw-disk fault original engine changed')
    mode = selected[0].get('status', {}).get('replicaModeMap', {}).get(proof['replica'])
    failed_at = row['spec'].get('failedAt') if row else None
    if mode != 'ERR' and not failed_at:
        return False
    proof['degraded'] = {'replica_mode': mode, 'failed_at': failed_at, 'engine_uid': selected[0]['metadata']['uid']}
    persist(lab, journal, proof)
    return True


def inject(lab, journal, case, relay_pid):
    if KEY in journal:
        raise ValueError('prior disk fault requires recovery; do not replay')
    proof = boundary(lab, journal, case)
    if boundary(lab, journal, case) != proof:
        raise ValueError('raw disk boundary changed before mutation')
    persist(lab, journal, proof)
    if case == 'rebuild-qos':
        qos_prepare(lab, journal, proof, relay_pid)
        # Product reconciliation must not replace a data-plane identity.
        if load('longhorn-v2-manager-loss.py').runtime(lab, journal) != proof['runtime']:
            raise ValueError('QoS product setup unexpectedly replaced native instances')
    proof['offline_intent'] = True
    persist(lab, journal, proof)
    proof['offline'] = host_call(lab, proof, 'offline')
    persist(lab, journal, proof)
    lab.wait('v2-raw-disk-native-replica-failure', 180, lambda: degraded(lab, journal, proof))
    if case == 'rebuild-qos':
        result = lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c',
                             filler_program(), timeout=240)
        proof['filler'] = json.loads(result.stdout)
        if proof['filler']['bytes'] != FILL_BYTES or not re.fullmatch('[a-f0-9]{64}', proof['filler']['sha256']):
            raise ValueError('rebuild divergence lacks a real fsync-confirmed filler')
        persist(lab, journal, proof)
    print('LONGHORN_V2_DISK_FAULT_INJECTED '+json.dumps(public_proof(proof), sort_keys=True), flush=True)
    return proof


def validate(lab, journal, proof):
    state = lab.owner()
    if (proof.get('case') not in CASES or proof.get('run_id') != state['run_id'] or
            proof.get('cluster_uid') != state['cluster_uid'] or proof.get('volume_uid') != journal['volume_uid'] or
            proof['address'] not in lab.WORKERS or proof['node'] != 'longhorn-node-'+proof['address'].rsplit('.', 1)[1] or
            proof['host']['machine_id'] != state['raw_disks'][proof['address']]['machine_id'] or
            proof['declaration']['path'] != state['raw_disks'][proof['address']]['path'] or
            proof['declaration']['identity'] != state['raw_disks'][proof['address']]['identity'] or
            lab.get('node', proof['node'])['metadata']['uid'] != proof['node_uid']):
        raise ValueError('disk fault journal differs from its original leased boundary')
    volume(lab, journal)


def restore_disk(lab, journal, proof):
    validate(lab, journal, proof)
    if proof.get('offline_intent'):
        observed = host_call(lab, proof, 'restore')
        if observed != proof['host']:
            raise ValueError('restored SCSI disk differs from original running identity')
        proof['disk_restored'] = True
        persist(lab, journal, proof)


def active_qos(lab, journal, proof, limit):
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items']
               if e['spec'].get('volumeName') == journal['volume']]
    if len(engines) != 1 or engines[0]['metadata']['uid'] != proof['degraded']['engine_uid']:
        raise ValueError('QoS original engine identity changed')
    entries = engines[0].get('status', {}).get('rebuildStatus') or {}
    for endpoint, row in entries.items():
        if (row and row.get('isRebuilding') is True and not row.get('error') and
                row.get('appliedRebuildingMBps') == limit):
            observation = {'engine_uid': engines[0]['metadata']['uid'], 'endpoint': endpoint,
                'isRebuilding': True, 'appliedRebuildingMBps': limit, 'progress': row.get('progress'),
                'global': setting(lab, proof), 'volume_override': volume(lab, journal)['spec'].get(FIELD, 0)}
            proof['qos']['observations'].append(observation)
            persist(lab, journal, proof)
            print('LONGHORN_V2_ACTIVE_REBUILD_QOS '+json.dumps(observation, sort_keys=True), flush=True)
            return True
    if lab.replica_topology(journal, 3):
        raise ValueError('rebuild completed before required native active QoS observation')
    return False


def qualify_qos(lab, journal, proof, relay_pid):
    qos = proof['qos']
    if qos.get('qualified'):
        return
    lab.wait('v2-rebuild-positive-volume-override', 240, lambda: active_qos(lab, journal, proof, 2))
    qos['override_expected'] = 0
    persist(lab, journal, proof)
    patch_override(lab, journal, 2, 0)
    lab.wait('v2-rebuild-zero-inherits-global', 120, lambda: active_qos(lab, journal, proof, 1))
    apply_global(lab, journal, proof, relay_pid, 3)
    lab.wait('v2-rebuild-dynamic-global-update', 120, lambda: active_qos(lab, journal, proof, 3))
    qos['qualified'] = True
    persist(lab, journal, proof)


def recovered(lab, journal, proof, relay_pid, *, qualify=True, emit=True):
    restore_disk(lab, journal, proof)
    if proof['case'] == 'rebuild-qos' and qualify:
        qualify_qos(lab, journal, proof, relay_pid)
    replicas = lab.replica_topology(journal, 3)
    if not replicas or not lab.healthy_replicas(journal['volume']):
        return False
    node = lab.get('nodes.longhorn.io', proof['node'], NS)
    if node['status']['diskStatus'][proof['declaration']['name']]['diskUUID'] != proof['disk_uuid']:
        raise ValueError('rebuild changed original native raw disk UUID')
    proof['recovered'] = {'replicas': replicas, 'disk_uuid': proof['disk_uuid'], 'host': host_call(lab, proof, 'probe')}
    persist(lab, journal, proof)
    if emit:
        print('LONGHORN_V2_DISK_FAULT_RECOVERED '+json.dumps(
            dict(case=proof['case'], volume_uid=journal['volume_uid'], **proof['recovered']), sort_keys=True), flush=True)
    return True


def restore_qos(lab, journal, proof, relay_pid):
    qos = proof.get('qos')
    if not qos or qos.get('restored'):
        return
    # An interrupted longhorn-setup owns a nested product input receipt.
    # Recover it first, while retaining the outer disk/settings transaction.
    lab.restore_request_input()
    original = qos['override_original']
    current = volume(lab, journal)['spec'].get(FIELD, 0)
    if current not in (original, 2, 0):
        raise ValueError('foreign per-volume QoS value; retain recovery journal')
    patch_override(lab, journal, current, original)
    apply_global(lab, journal, proof, relay_pid, int(qos['setting_original']['v2']))
    if setting(lab, proof) != qos['setting_original']:
        raise ValueError('original native rebuild setting did not restore')
    path = lab.DIRECTORY/'config.yml'
    if path.is_symlink() or path.read_text() not in qos['allowed_inputs']:
        raise ValueError('foreign product config during QoS restoration')
    atomic(path, qos['input_original'])
    qos['restored'] = True
    persist(lab, journal, proof)
    print('LONGHORN_V2_REBUILD_QOS_RESTORED', flush=True)


def cleanup(lab, original, record=None, *, relay_pid, **kwargs):
    record = record or lab.DIRECTORY/RECORD
    if record.is_symlink():
        raise ValueError('symlink disk fault journal')
    if record.exists():
        journal = json.loads(record.read_text())
        proof = journal.get(KEY)
        if proof:
            if sys.exc_info()[0] is not None:
                try:
                    load('longhorn-v2-manager-loss.py').diagnostics(lab, journal)
                except Exception as error:
                    print('LONGHORN_PRIVATE_DIAGNOSTIC_UNAVAILABLE type='+type(error).__name__, flush=True)
            validate(lab, journal, proof)
            lab.check_business_graph(journal)
            restore_disk(lab, journal, proof)
            # Cleanup recovers faults without replaying a failed qualification.
            if not proof.get('recovered'):
                recover_oom_client(lab, journal, proof, record)
                lab.wait('v2-disk-restore-before-business-cleanup', 1200,
                    lambda: recovered(lab, journal, proof, relay_pid, qualify=False, emit=False))
            if not proof.get('recovered'):
                raise ValueError('raw-disk recovery unproven; retain business and journal')
            restore_qos(lab, journal, proof, relay_pid)
            if host_call(lab, proof, 'probe') != proof['host']:
                raise ValueError('raw disk identity/state changed after recovery')
            lab.installation_health()
            print('LONGHORN_V2_DISK_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)
    return original(record, relay_pid=relay_pid, **kwargs)


def recover_oom_client(lab, journal, proof, record):
    """Reattach only this failed bare client so native replica recovery can run."""
    current = volume(lab, journal)
    name = 'disk-fault-recovery'
    key = 'Pod/'+name
    if (current.get('status', {}).get('state') != 'detached' or current['spec'].get('nodeID')) and key not in journal['objects']:
        return
    writer = lab.get('pod', 'writer', journal['namespace'])
    if (not writer or writer['metadata']['uid'] != journal['objects'].get('Pod/writer') or
            writer['metadata'].get('ownerReferences') or writer.get('status', {}).get('phase') != 'Failed'):
        raise ValueError('detached disk recovery requires its original failed bare client')
    statuses = writer['status'].get('containerStatuses', [])
    if (len(statuses) != 1 or statuses[0].get('state', {}).get('terminated', {}).get('reason') != 'OOMKilled' or
            statuses[0]['state']['terminated'].get('exitCode') != 137):
        raise ValueError('detached disk recovery is restricted to the recorded client OOM')
    node = proof['runtime']['client_node']
    pod = lab.get('pod', name, journal['namespace'])
    if key not in journal['objects']:
        if pod:
            raise ValueError('foreign disk recovery client exists')
        pod = lab.create_business_object(journal, record, lab.client_pod(journal['namespace'], name, 'data', node))
    if (not pod or pod['metadata']['uid'] != journal['objects'][key] or pod['spec'].get('nodeSelector') !=
            {'kubernetes.io/hostname': node} or pod['metadata'].get('ownerReferences') or
            [v.get('persistentVolumeClaim', {}).get('claimName') for v in pod['spec']['volumes']
             if 'persistentVolumeClaim' in v] != ['data']):
        raise ValueError('disk recovery client identity, node or claim changed')
    def ready():
        current = lab.get('pod', name, journal['namespace'])
        if not current or current['metadata']['uid'] != journal['objects'][key]:
            raise ValueError('disk recovery client replaced while attaching')
        return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in current.get('status', {}).get('conditions', []))
    lab.wait('disk-fault-oom-recovery-client-ready', 600, ready)
    print('LONGHORN_V2_DISK_FAULT_OOM_CLIENT_REATTACHED cleanup_only=true', flush=True)


def remount(lab, journal, client, ledger, original):
    original(lab, journal, client, ledger)
    proof = journal[KEY]
    if proof['case'] == 'rebuild-qos':
        observed = json.loads(lab.kubectl('-n', journal['namespace'], 'exec', client, '--', 'python3', '-c',
                                        filler_program(read_only=True), timeout=180).stdout)
        if observed != proof['filler']:
            raise ValueError('rebuilt filler changed after independent cross-node remount')
        print('LONGHORN_V2_REBUILD_FILLER_REMOUNT_VERIFIED '+json.dumps(observed, sort_keys=True), flush=True)


def business(lab, relay_pid, case):
    if case not in CASES:
        raise ValueError('unsupported disk fault business case')
    original, original_cleanup = lab.load_helper, lab.cleanup_business
    control = original('longhorn-control-faults.py')
    original_remount = control.verify_remount
    control.inject = lambda current, journal, ignored: inject(current, journal, case, relay_pid)
    control.recovered = lambda current, journal, proof: recovered(current, journal, proof, relay_pid)
    control.verify_remount = lambda current, journal, client, ledger: remount(current, journal, client, ledger, original_remount)
    lab.load_helper = lambda name: control if name == 'longhorn-control-faults.py' else original(name)
    lab.cleanup_business = lambda record=None, **kw: cleanup(lab, original_cleanup, record, **kw)
    try:
        lab.run_requests(relay_pid, [])
        lab.business('control_failure', relay_pid=relay_pid, fault_variant='instance-manager')
    finally:
        lab.load_helper, lab.cleanup_business = original, original_cleanup
    print('LONGHORN_V2_DISK_FAULT_BUSINESS_PASS case='+case, flush=True)


def verify_log(log, case):
    def one(prefix):
        rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines() if s.startswith(prefix+' ')]
        if len(rows) != 1:
            raise ValueError('disk fault requires one observation: '+prefix)
        return rows[0]
    if case not in CASES:
        raise ValueError('unknown disk fault log scope')
    proof = one('LONGHORN_V2_DISK_FAULT_INJECTED')
    restored = one('LONGHORN_V2_DISK_FAULT_RECOVERED')
    result = one('LONGHORN_CONTROL_FAULT_RESULT')
    ledger = one('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS')
    remounted = one('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED')
    if (proof['case'] != case or restored['case'] != case or proof['volume_uid'] != restored['volume_uid'] or
            not proof.get('offline_intent') or proof['offline']['state'] != 'offline' or
            proof['host']['state'] != 'running' or restored['host'] != proof['host'] or
            restored['disk_uuid'] != proof['disk_uuid'] or not proof.get('degraded') or
            proof['degraded'].get('replica_mode') != 'ERR' and not proof['degraded'].get('failed_at') or
            result['variant'] != 'instance-manager' or result['client_rc'] != 0 or
            result['recovery_mode'] != 'native-component-recreation-original-client' or
            not 0 <= result['recovery_s'] <= 1200 or not isinstance(result['retries'], list)):
        raise ValueError('disk fault lacks actual owned disk loss/native failure/recovery')
    replicas = restored['replicas']
    if (len(replicas) != 3 or len({r['node'] for r in replicas.values()}) != 3 or
            len({r['uid'] for r in replicas.values()}) != 3):
        raise ValueError('disk fault recovery lacks healthy three-node replicas')
    faults, control = load('longhorn-faults.py'), load('longhorn-control-faults.py')
    if (ledger['client'] != 'writer' or not ledger['token'].endswith(':instance-manager') or
            ledger['fresh']['token'] != ledger['token']+':recovered' or
            ledger['fresh'].get('path') != '/data/control-recovered-ledger'):
        raise ValueError('disk fault client/ledger scope differs')
    for field, records in (('confirmed', ledger), ('fresh_confirmed', ledger['fresh'])):
        checked = control.confirmed(records['records'], records['token'], faults)['summary']
        if records['summary'] != checked or result[field] != checked:
            raise ValueError('disk fault fsync acknowledgement prefix differs')
    if remounted != {'original': result['confirmed'], 'recovered': result['fresh_confirmed']}:
        raise ValueError('disk fault remount omitted an acknowledged ledger')
    token = ledger['token'].removesuffix(':instance-manager')
    matches = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    if (len(matches) != 1 or not token or int(matches[0][0]) != len(token.encode())*262144 or
            matches[0][1] != hashlib.sha256(token.encode()*262144).hexdigest() or
            set(matches[0][2].split(',')) != {r['node'] for r in proof['runtime']['replicas'].values()}):
        raise ValueError('disk fault original payload cross-node remount differs')
    required = ['LONGHORN_CONTROL_FAILURE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
        'LONGHORN_V2_DISK_FAULT_RESTORED_BEFORE_CLEANUP', 'LONGHORN_V2_DISK_FAULT_BUSINESS_PASS case='+case]
    if case == 'rebuild-qos':
        rows = [json.loads(s.split(' ', 1)[1]) for s in log.splitlines() if s.startswith('LONGHORN_V2_ACTIVE_REBUILD_QOS ')]
        if (len(rows) != 3 or [r['appliedRebuildingMBps'] for r in rows] != [2, 1, 3] or
                [r['volume_override'] for r in rows] != [2, 0, 0] or
                [r['global']['v2'] for r in rows] != ['1', '1', '3'] or
                any(r['isRebuilding'] is not True or r['engine_uid'] != proof['degraded']['engine_uid'] for r in rows)):
            raise ValueError('QoS requires all three actual active rebuild resolutions')
        filler = one('LONGHORN_V2_REBUILD_FILLER_REMOUNT_VERIFIED')
        if filler != proof['filler'] or filler['bytes'] != FILL_BYTES:
            raise ValueError('QoS diverged data lacks independent cross-node checksum')
        required.append('LONGHORN_V2_REBUILD_QOS_RESTORED')
    if any(log.splitlines().count(marker) != 1 for marker in required):
        raise ValueError('disk fault lacks its business and scoped recovery/cleanup exit')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('disk fault log contains a failure marker')
    return {'case': case, 'recovery_s': result['recovery_s'], 'retries': result['retries'],
            'confirmed': result['confirmed'], 'fresh_confirmed': result['fresh_confirmed']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify', 'cleanup'))
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.case or not args.log:
            parser.error('verify requires --case and --log')
        verify_log(args.log.read_text(), args.case)
        return
    if args.relay_pid <= 0 or args.mode == 'business' and not args.case:
        parser.error('mutation requires a live relay and business --case')
    _, lab = load('longhorn-v2-install.py').fixture()
    lab.require_lease()
    if args.mode == 'cleanup':
        cleanup(lab, lab.cleanup_business, relay_pid=args.relay_pid)
    else:
        business(lab, args.relay_pid, args.case)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-disk-fault-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            diagnostic = stream.name
        print('LONGHORN_V2_DISK_FAULT_FAIL diagnostic='+diagnostic, flush=True)
        sys.exit(1)
