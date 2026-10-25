#!/usr/bin/env python3
"""LONGHORN-28: bounded V1 capacity fault, never fill a leased physical disk.

Official 1.12.1 scheduling, node-space-usage and full-disk recovery; manager
8320238242e8c804325d9588ec1db10feb9f36b8, IsSchedulableToDisk. Native disk
changes are fault fixtures; install and snapshots use config.yml + kubecli.
"""
import argparse
import errno
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import secrets
import signal
import sys
import uuid

NS = 'longhorn-system'
RECORD = 'longhorn-capacity.json'
BUSINESS = 'longhorn-capacity-business.json'
LIMIT = 4 * 1024**3


def host_fixture(request, action):
    """A private 4 GiB ext4 loop disk and an inode-bound allocation file."""
    import errno
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import subprocess

    def run(argv):
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=120)
    base = Path(request['base']); directory = base/('capacity-'+request['id'])
    if (not re.fullmatch('[a-f0-9-]{36}', request['id']) or base.resolve() != base or
            request['limit'] != 4*1024**3 or base.stat().st_dev == Path('/').stat().st_dev):
        raise ValueError('capacity fixture requires a leased independent filesystem')
    mounted = json.loads(run(['findmnt', '-J', '-M', str(base), '-o', 'TARGET,UUID,FSTYPE']).stdout)['filesystems']
    if (len(mounted) != 1 or mounted[0]['target'] != str(base) or mounted[0].get('uuid') != request['uuid'] or
            mounted[0].get('fstype') not in ('ext4', 'xfs')):
        raise ValueError('capacity parent filesystem identity changed')
    record = directory/'fixture.json'; image = directory/'disk.img'; mount = directory/'mount'
    if action == 'verify-removed':
        if directory.exists() or directory.is_symlink():
            raise ValueError('capacity host directory remains after cleanup')
        mounts = json.loads(run(['findmnt', '-J', '--list', '-o', 'TARGET,UUID']).stdout)['filesystems']
        if any(entry.get('target') == str(mount) or entry.get('uuid') == request['id'] for entry in mounts):
            raise ValueError('capacity host mount remains after cleanup')
        loops = json.loads(run(['losetup', '-J', '--list', '--output', 'NAME,BACK-FILE']).stdout).get('loopdevices') or []
        if any(entry.get('back-file') in (str(image), str(image)+' (deleted)') for entry in loops):
            raise ValueError('capacity host loop remains after cleanup')
        return {'uuid': request['id'], 'parent_uuid': request['uuid'],
                'directory_absent': True, 'mount_absent': True, 'loop_absent': True}
    def identity(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode): raise ValueError('capacity file is not regular')
        return {'device': info.st_dev, 'inode': info.st_ino}
    def persist(row):
        temporary = directory/'fixture.new'
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(record)
    if action == 'create':
        if os.statvfs(base).f_bavail*os.statvfs(base).f_frsize < 2*request['limit']:
            raise ValueError('capacity parent lacks 8 GiB safety headroom')
        directory.mkdir(mode=0o700); mount.mkdir(mode=0o700)
        row = {'request': request, 'phase': 'create-intent'}; persist(row)
        fd = os.open(image, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        try: os.ftruncate(fd, request['limit']); os.fsync(fd)
        finally: os.close(fd)
        row.update(image=identity(image), phase='format-intent'); persist(row)
        run(['mkfs.ext4', '-q', '-F', '-m', '0', '-U', request['id'], str(image)])
        row['phase'] = 'mount-intent'; persist(row)
        run(['mount', '-o', 'loop,nodev,nosuid', str(image), str(mount)])
        row['phase'] = 'ready'; persist(row)
    if directory.resolve() != directory or record.is_symlink(): raise ValueError('symlink capacity host journal')
    row = json.loads(record.read_text())
    if row['request'] != request or row['image'] != identity(image) or image.stat().st_size != request['limit']:
        raise ValueError('capacity host journal or backing image changed')
    if row['phase'] != 'ready': raise ValueError('partial capacity fixture needs diagnosis; do not replay')
    fs = json.loads(run(['findmnt', '-J', '-M', str(mount), '-o', 'TARGET,SOURCE,UUID,FSTYPE']).stdout)['filesystems']
    if (len(fs) != 1 or fs[0]['target'] != str(mount) or fs[0].get('uuid') != request['id'] or
            fs[0].get('fstype') != 'ext4' or not re.fullmatch('/dev/loop[0-9]+', fs[0].get('source', ''))):
        raise ValueError('bounded capacity mount identity changed')
    loop = json.loads(run(['losetup', '-J', '--list', '--output', 'NAME,BACK-FILE', fs[0]['source']]).stdout)['loopdevices']
    if len(loop) != 1 or loop[0]['back-file'] != str(image): raise ValueError('capacity loop backing file changed')
    filler = mount/'kubeauto-capacity-filler'
    def remove_filler():
        if 'filler' not in row:
            if filler.exists() or filler.is_symlink(): raise ValueError('foreign capacity filler')
            return
        if filler.exists() or filler.is_symlink():
            if identity(filler) != row['filler']: raise ValueError('capacity filler inode changed')
            filler.unlink(); fd = os.open(mount, os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        row.pop('filler'); persist(row)
    enospc = None
    if action in ('low', 'full'):
        if 'filler' not in row:
            fd = os.open(filler, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
            os.close(fd); row['filler'] = identity(filler); persist(row)
        if identity(filler) != row['filler']: raise ValueError('capacity filler identity changed')
        fd = os.open(filler, os.O_RDWR|os.O_NOFOLLOW)
        try:
            chunk = 16*1024**2
            while True:
                space = os.fstatvfs(fd); available = space.f_bavail*space.f_frsize
                target = space.f_blocks*space.f_frsize//10 if action == 'low' else 0
                if available <= target and action == 'low': break
                amount = min(chunk, available-target)
                amount = amount//space.f_frsize*space.f_frsize
                if not amount and action == 'low': break
                # statfs excludes ext4's internal reserve from available, not
                # free. Even zero available bytes must reach an actual syscall.
                amount = max(space.f_frsize, amount)
                offset = os.fstat(fd).st_size
                try: os.posix_fallocate(fd, offset, amount)
                except OSError as error:
                    if error.errno != errno.ENOSPC: raise
                    if amount <= space.f_frsize:
                        enospc = {'errno': error.errno, 'offset': offset, 'length': amount}
                        break
                    chunk = max(space.f_frsize, amount//2)
            os.fsync(fd)
        finally: os.close(fd)
    elif action == 'release': remove_filler()
    elif action == 'remove':
        remove_filler()
        # Longhorn must retire all owned replicas before the fault disk goes.
        allowed = {'lost+found', 'longhorn-disk.cfg', 'replicas', 'backing-images'}
        if any(p.is_symlink() or p.name not in allowed for p in mount.iterdir()):
            raise ValueError('foreign object on bounded capacity disk')
        for name in ('replicas', 'backing-images'):
            path = mount/name
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise ValueError('capacity disk retains native consumers')
        run(['umount', str(mount)])
        if json.loads(run(['losetup', '-J', '--list', '--output', 'NAME,BACK-FILE']).stdout).get('loopdevices') and any(
                entry.get('back-file') == str(image) for entry in json.loads(
                    run(['losetup', '-J', '--list', '--output', 'NAME,BACK-FILE']).stdout)['loopdevices']):
            raise ValueError('capacity backing file retains a loop consumer')
        if identity(image) != row['image']: raise ValueError('capacity image changed before unlink')
        image.unlink(); mount.rmdir(); record.unlink(); directory.rmdir()
        return {'removed': True}
    elif action not in ('probe', 'create'): raise ValueError('unsupported capacity host action')
    space = os.statvfs(mount)
    return {'mount': str(mount), 'uuid': request['id'], 'total': space.f_blocks*space.f_frsize,
            'available': space.f_bavail*space.f_frsize, 'free': space.f_bfree*space.f_frsize,
            'block_size': space.f_frsize, 'enospc': enospc,
            'filler_bytes': filler.stat().st_size if filler.exists() else 0}


def host(lab, row, action):
    code = inspect.getsource(host_fixture)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(host_fixture(r["request"],r["action"]),sort_keys=True))\n'
    return json.loads(lab.remote(row['address'], ['python3', '-c', code],
        stdin=json.dumps({'request': row['host'], 'action': action}), timeout=180).stdout)


def save(lab, row): lab.write_private(lab.DIRECTORY/RECORD, row)


def full_disk(lab, row):
    full = host(lab, row, 'full')
    row['full'] = full; save(lab, row)
    # Reserved metadata blocks can remain in f_bfree after user allocations
    # fail. A large failed allocation or a space counter alone is insufficient.
    probe = full.get('enospc') or {}
    if (probe.get('errno') != errno.ENOSPC or full.get('block_size', 0) <= 0 or
            probe.get('length') != full['block_size']):
        raise ValueError('bounded disk did not produce block-sized ENOSPC allocation')
    return full


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink capacity journal')
    row = json.loads(path.read_text()); state = lab.owner()
    if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
            row['address'] not in lab.WORKERS or row['host']['uuid'] != state['disks'][row['address']]['uuid']):
        raise ValueError('foreign capacity lease')
    return row


def native_node(lab, row):
    node = lab.get('nodes.longhorn.io', row['node'], NS)
    if not node or node['metadata']['uid'] != row['node_uid']:
        raise ValueError('capacity native node replaced')
    disks = node['spec']['disks']; expected = row['original_disks']
    for key, original in expected.items():
        current = disks.get(key)
        if not current or {k: v for k, v in current.items() if k != 'allowScheduling'} != {
                k: v for k, v in original.items() if k != 'allowScheduling'}:
            raise ValueError('capacity original disk configuration changed')
    if set(disks)-set(expected)-{row['disk']}:
        raise ValueError('capacity node acquired a foreign disk')
    if row['disk'] in disks and {k: v for k, v in disks[row['disk']].items() if k != 'allowScheduling'} != {
            k: v for k, v in row['extra'].items() if k != 'allowScheduling'}:
        raise ValueError('capacity extra disk configuration changed')
    return node


def disk_flags(lab, row, *, restore=False, isolate=False, remove=False):
    node = native_node(lab, row); desired = dict(node['spec']['disks'])
    for key, original in row['original_disks'].items():
        desired[key] = dict(original, allowScheduling=original.get('allowScheduling', True) if restore else False)
    if remove: desired.pop(row['disk'], None)
    else: desired[row['disk']] = dict(row['extra'], allowScheduling=not isolate)
    if desired != node['spec']['disks']:
        lab.kubectl('-n', NS, 'patch', 'nodes.longhorn.io', row['node'], '--type=json',
            '--patch-file=/dev/stdin', stdin=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': row['node_uid']},
                {'op': 'test', 'path': '/spec/disks', 'value': node['spec']['disks']},
                {'op': 'replace', 'path': '/spec/disks', 'value': desired}]))


def ready(pod):
    return bool(pod and any(c.get('type') == 'Ready' and c.get('status') == 'True'
        for c in pod.get('status', {}).get('conditions', [])))


def prepare(lab, row):
    ns = 'longhorn-business-'+secrets.token_hex(8); token = secrets.token_hex(16)
    row['namespace_intent'] = {'name': ns, 'token': token}; save(lab, row)
    obj = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns,
            'annotations': {'kubeauto.io/capacity-token': token}}})).stdout)
    business = {'namespace': ns, 'namespace_uid': obj['metadata']['uid'], 'objects': {},
                'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
    lab.write_private(lab.DIRECTORY/BUSINESS, business)
    business['defaults'] = lab.wait('capacity-namespace-defaults', 60, lambda: lab.namespace_defaults(ns))
    lab.write_private(lab.DIRECTORY/BUSINESS, business)
    claim(lab, business, 'data')
    return business


def claim(lab, business, name):
    record = lab.DIRECTORY/BUSINESS
    lab.create_business_object(business, record, {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
        'metadata': {'name': name, 'namespace': business['namespace']}, 'spec': {'accessModes': ['ReadWriteOnce'],
            'storageClassName': 'longhorn', 'resources': {'requests': {'storage': '2Gi'}}}})
    def bound():
        obj = lab.get('pvc', name, business['namespace'])
        if not obj or obj.get('status', {}).get('phase') != 'Bound': return False
        lab.bind_business_volume(business, record, name)
        return True
    lab.wait('capacity-claim-bound-'+name, 600, bound)


def check_data(lab, business, row):
    digest = lab.kubectl('-n', business['namespace'], 'exec', row['client'], '--', 'python3', '-c',
        lab.payload_program(row['run_id'], read_only=True)).stdout.strip()
    if digest != row['digest']: raise ValueError('capacity fault changed acknowledged application data')


def replicas(lab, business):
    return [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == business['volume']]


def disk_observation(lab, row):
    node = native_node(lab, row); status = node.get('status', {}).get('diskStatus', {}).get(row['disk'])
    if not status or not status.get('diskUUID'): return None
    return status


def retire_failed(lab, business, replica):
    """Only a proven failed owned replica with two independent RW peers."""
    current = lab.get('replicas.longhorn.io', replica['metadata']['name'], NS)
    if current is None: return
    if (current['metadata']['uid'] != replica['metadata']['uid'] or
            current['spec'].get('volumeName') != business['volume'] or
            not (current['spec'].get('failedAt') or current.get('status', {}).get('currentState') == 'error')):
        raise ValueError('capacity failed replica changed identity or is not failed')
    peers = [r for r in replicas(lab, business) if r['metadata']['uid'] != current['metadata']['uid'] and
             r['spec'].get('healthyAt') and not r['spec'].get('failedAt') and
             r.get('status', {}).get('currentState') == 'running']
    engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items'] if
               e['spec'].get('volumeName') == business['volume'] and e.get('status', {}).get('currentState') == 'running']
    if (len(peers) < 2 or len({r['spec']['nodeID'] for r in peers}) < 2 or len(engines) != 1 or
            any(engines[0]['status'].get('replicaModeMap', {}).get(r['metadata']['name']) != 'RW' for r in peers)):
        raise ValueError('capacity refuses replica retirement without two independent healthy RW peers')
    lab.remove(current)


def archive_clean_failure(lab, row):
    """Revalidate an already cleaned failed attempt before preserving its receipt."""
    if row.get('passed'):
        raise ValueError('capacity successful attempt is not a failed recovery receipt')
    path = lab.DIRECTORY/RECORD
    archived = path.with_name('longhorn-capacity-recovered-'+row['host']['id']+'.json')
    if archived.exists() or archived.is_symlink():
        raise ValueError('capacity recovery archive already exists')
    business = lab.DIRECTORY/BUSINESS
    if business.exists() or business.is_symlink():
        raise ValueError('capacity business receipt remains after cleanup')
    intent = row.get('namespace_intent')
    if intent and lab.get('namespace', intent['name']) is not None:
        raise ValueError('capacity namespace remains after cleanup')
    lab.load_helper('longhorn-platform.py').empty(lab)
    node = native_node(lab, row)
    if (node['spec']['disks'] != row['original_disks'] or
            row['disk'] in node.get('status', {}).get('diskStatus', {})):
        raise ValueError('capacity original disk flags or temporary disk status remain')
    for name, saved in row['settings'].items():
        obj = lab.get('settings.longhorn.io', name, NS)
        if not obj or {'uid': obj['metadata']['uid'], 'value': obj['value']} != saved:
            raise ValueError('capacity original setting changed before recovery')
    if (lab.shared_snapshot_identity() != row['shared'] or
            hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != row['config_sha256']):
        raise ValueError('capacity shared objects or original input changed before recovery')
    removed = host(lab, row, 'verify-removed')
    if removed != {'uuid': row['host']['id'], 'parent_uuid': row['host']['uuid'],
                   'directory_absent': True, 'mount_absent': True, 'loop_absent': True}:
        raise ValueError('capacity host cleanup verification identity differs')
    row['recovery_verified'] = {'host': removed, 'node_uid': node['metadata']['uid'],
                               'namespace_absent': True, 'native_objects_absent': True,
                               'original_flags_settings_and_input': True}
    save(lab, row)
    path.rename(archived)


def clean(lab, relay_pid, *, failed=False):
    path = lab.DIRECTORY/RECORD
    if not path.exists(): return
    row = receipt(lab)
    if row.get('clean'):
        lab.installation_health()
        if failed: archive_clean_failure(lab, row)
        return
    if row.get('host_ready'): host(lab, row, 'release')
    if row.get('flags_intent'): disk_flags(lab, row, restore=True, isolate=True)
    record = lab.DIRECTORY/BUSINESS
    if not record.exists() and row.get('namespace_intent'):
        intent = row['namespace_intent']; ns = lab.get('namespace', intent['name'])
        if ns:
            if ns['metadata'].get('annotations', {}).get('kubeauto.io/capacity-token') != intent['token']:
                raise ValueError('capacity namespace intent ownership differs')
            lab.write_private(record, {'namespace': intent['name'], 'namespace_uid': ns['metadata']['uid'],
                'objects': {}, 'storage_class': 'longhorn', 'reclaim_policy': 'Retain',
                'defaults': lab.wait('capacity-recovery-defaults', 60, lambda: lab.namespace_defaults(intent['name']))})
    if record.exists():
        business = json.loads(record.read_text())
        if row.get('digest'):
            for replica in replicas(lab, business):
                if replica['spec'].get('nodeID') == row['node'] and replica['spec'].get('diskPath') == row['extra']['path']:
                    # Isolate the full disk, then retire only this volume's
                    # replica. Healthy peers remain the recovery authority.
                    if not replica['spec'].get('failedAt') and replica.get('status', {}).get('currentState') == 'running':
                        continue
                    retire_failed(lab, business, replica)
            lab.wait('capacity-three-replicas-restored', 900, lambda: lab.replica_topology(business, 3))
            check_data(lab, business, row)
        lab.cleanup_business(record, relay_pid=relay_pid)
    if row.get('flags_intent'):
        disk_flags(lab, row, restore=True, isolate=True, remove=True)
        lab.wait('capacity-native-disk-retired', 120, lambda: row['disk'] not in native_node(lab, row).get('status', {}).get('diskStatus', {}))
        if native_node(lab, row)['spec']['disks'] != row['original_disks']:
            raise ValueError('capacity original disk flags were not restored')
    if row.get('host_ready'): host(lab, row, 'remove')
    elif row.get('host_intent'):
        raise ValueError('capacity host creation response uncertain; retain journal for diagnosis')
    lab.installation_health(); row['clean'] = True; save(lab, row)
    if failed:
        path.rename(path.with_name('longhorn-capacity-recovered-'+row['host']['id']+'.json'))


def business(lab, relay_pid):
    lab.load_helper('longhorn-platform.py').empty(lab)
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink(): raise ValueError('previous capacity fixture needs recovery')
    lab.supervise_core(relay_pid, 'longhorn-setup'); lab.installation_health()
    state = lab.owner(); address = lab.WORKERS[0]; node_name = 'longhorn-node-'+address.rsplit('.', 1)[1]
    node = lab.get('nodes.longhorn.io', node_name, NS); identifier = str(uuid.uuid4())
    if not node or not node['spec'].get('allowScheduling') or not node['spec'].get('disks'):
        raise ValueError('capacity needs an existing schedulable storage worker')
    row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'address': address,
        'node': node_name, 'node_uid': node['metadata']['uid'], 'original_disks': node['spec']['disks'],
        'disk': 'capacity-'+identifier, 'host': {'id': identifier, 'limit': LIMIT, 'base': lab.DATA,
            'uuid': state['disks'][address]['uuid']}, 'settings': {}, 'shared': lab.shared_snapshot_identity(),
        'config_sha256': hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()}
    for setting in ('storage-minimal-available-percentage', 'storage-over-provisioning-percentage'):
        obj = lab.get('settings.longhorn.io', setting, NS)
        row['settings'][setting] = {'uid': obj['metadata']['uid'], 'value': obj['value']}
    if int(row['settings']['storage-minimal-available-percentage']['value']) <= 10:
        raise ValueError('capacity requires the unchanged production low-space threshold above 10%')
    row['extra'] = {'path': lab.DATA+'/capacity-'+identifier+'/mount', 'diskType': 'filesystem', 'diskDriver': '',
        'allowScheduling': True, 'evictionRequested': False, 'storageReserved': 0, 'tags': []}
    save(lab, row)
    with lab.scoped_cleanup(lambda: clean(lab, relay_pid)):
        row['host_intent'] = True; save(lab, row); initial = host(lab, row, 'create')
        row['host_ready'] = True; save(lab, row)
        row['flags_intent'] = True; save(lab, row); disk_flags(lab, row)
        lab.wait('capacity-extra-disk-ready', 180, lambda: disk_observation(lab, row))
        fixture = prepare(lab, row); record = lab.DIRECTORY/BUSINESS
        lab.create_business_object(fixture, record, lab.client_pod(fixture['namespace'], 'writer', 'data', node_name))
        row['client'] = 'writer'; save(lab, row)
        lab.wait('capacity-writer-ready', 600, lambda: ready(lab.get('pod', 'writer', fixture['namespace'])))
        lab.wait('capacity-original-three-replicas', 600, lambda: lab.replica_topology(fixture, 3))
        selected = [r for r in replicas(lab, fixture) if r['spec'].get('nodeID') == node_name]
        if len(selected) != 1 or selected[0]['spec'].get('diskPath') != row['extra']['path']:
            raise ValueError('capacity business replica did not use its bounded disk')
        row['replica'] = {'name': selected[0]['metadata']['name'], 'uid': selected[0]['metadata']['uid']}
        row['digest'] = lab.kubectl('-n', fixture['namespace'], 'exec', 'writer', '--', 'python3', '-c',
            lab.payload_program(row['run_id'])).stdout.strip()
        if row['digest'] != hashlib.sha256(row['run_id'].encode()*262144).hexdigest():
            raise ValueError('capacity initial application acknowledgement differs')
        save(lab, row)
        scheduled = lab.wait('capacity-scheduled-bytes', 120, lambda: (s := disk_observation(lab, row)) and
            int(s.get('storageScheduled', 0)) == 2*1024**3 and s)
        if initial['total'] >= scheduled['storageScheduled'] and initial['total']-initial['available'] >= scheduled['storageScheduled']:
            raise ValueError('capacity cannot distinguish scheduled and actual allocation')
        row['baseline'] = {'host': host(lab, row, 'probe'), 'native': scheduled}; save(lab, row)
        low = host(lab, row, 'low')
        def unschedulable():
            observed = disk_observation(lab, row)
            if not observed: return None
            conditions = [c for c in observed.get('conditions', []) if c.get('type') == 'Schedulable']
            return observed if len(conditions) == 1 and conditions[0].get('status') == 'False' and int(
                observed.get('storageAvailable', -1)) <= int(observed['storageMaximum'])*int(
                    row['settings']['storage-minimal-available-percentage']['value'])//100 else None
        refused = lab.wait('capacity-low-space-native-refusal', 180, unschedulable)
        claim(lab, fixture, 'probe')
        probe_volume = fixture['claims']['probe']['volume']
        def refused_replica():
            obj = lab.get('volumes.longhorn.io', probe_volume, NS)
            conditions = [c for c in obj.get('status', {}).get('conditions', []) if c.get('type') == 'Scheduled']
            assigned = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items'] if r['spec'].get('volumeName') == probe_volume]
            return obj if (len(conditions) == 1 and conditions[0].get('status') == 'False' and
                any(not r['spec'].get('nodeID') for r in assigned) and not any(
                    r['spec'].get('nodeID') == node_name for r in assigned)) else None
        refusal_volume = lab.wait('capacity-new-replica-refused', 180, refused_replica)
        check_data(lab, fixture, row)
        row['low'] = {'host': low, 'native': refused, 'volume_uid': refusal_volume['metadata']['uid']}; save(lab, row)
        print('LONGHORN_CAPACITY_LOW_SPACE_REFUSAL '+json.dumps(row['low'], sort_keys=True), flush=True)
        full_disk(lab, row)
        # Existing volume has two other healthy replicas. The owned file is a
        # bounded fault probe and its write result is evidence, never a PASS.
        code = 'import os\nf=os.open("/data/capacity-write",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)\ntry:\n for _ in range(64): os.write(f,os.urandom(1024**2))\n os.fsync(f)\nfinally: os.close(f)\n'
        write = lab.kubectl('-n', fixture['namespace'], 'exec', 'writer', '--', 'python3', '-c', code,
            timeout=180, check=False)
        def failed_replica():
            matches = [r for r in replicas(lab, fixture) if r['metadata']['uid'] == row['replica']['uid']]
            if not matches: raise ValueError('capacity original error replica disappeared before evidence')
            replica = matches[0]
            return replica if replica['spec'].get('failedAt') or replica.get('status', {}).get('currentState') == 'error' else None
        error = lab.wait('capacity-original-replica-error', 180, failed_replica)
        row['failure'] = {'replica': error, 'client_rc': write.returncode}; save(lab, row)
        print('LONGHORN_CAPACITY_FULL_DISK_REPLICA_ERROR '+json.dumps(row['failure'], sort_keys=True), flush=True)
        host(lab, row, 'release'); disk_flags(lab, row, restore=True, isolate=True)
        retire_failed(lab, fixture, error)
        lab.wait('capacity-healthy-rebuild', 900, lambda: lab.replica_topology(fixture, 3))
        check_data(lab, fixture, row)
        row['passed'] = True; save(lab, row)
        print('LONGHORN_CAPACITY_APPLICATION_RECOVERY sha256='+row['digest'], flush=True)
    print('LONGHORN_CAPACITY_BUSINESS_PASS', flush=True)


def finish(lab):
    row = receipt(lab)
    if not row.get('passed') or not row.get('clean') or (lab.DIRECTORY/BUSINESS).exists():
        raise ValueError('capacity business or cleanup incomplete')
    for name, saved in row['settings'].items():
        obj = lab.get('settings.longhorn.io', name, NS)
        if not obj or {'uid': obj['metadata']['uid'], 'value': obj['value']} != saved:
            raise ValueError('capacity changed original global thresholds')
    if (lab.shared_snapshot_identity() != row['shared'] or
            hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != row['config_sha256']):
        raise ValueError('capacity changed shared objects or original input')
    lab.load_helper('longhorn-platform.py').empty(lab); lab.installation_health()
    (lab.DIRECTORY/RECORD).unlink()
    print('LONGHORN_CAPACITY_FIXTURES_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('business', 'clean', 'finish'))
    parser.add_argument('--relay-pid', type=int, default=0); args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('longhorn_capacity_lab', Path(__file__).with_name('longhorn-lab.py'))
    lab = importlib.util.module_from_spec(spec); sys.modules[spec.name] = lab; spec.loader.exec_module(lab)
    lab.require_lease()
    if args.action == 'business': business(lab, args.relay_pid)
    elif args.action == 'clean': clean(lab, args.relay_pid, failed=True)
    else: finish(lab)


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    main()
