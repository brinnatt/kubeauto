#!/usr/bin/env python3
"""LONGHORN-30: snapshot checksum and isolated single-replica recovery.

Longhorn 1.12.1 export-from-replica/corrupted-replica and engine commit
1fa902f0061b84b92af445c38392bfeb99faffb2, package/launch-simple-longhorn.
Corruption is confined to an offline copy; live replicas are never modified.
"""
import argparse
from datetime import datetime, timezone
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
RECORD = 'longhorn-integrity.json'
BUSINESS = 'longhorn-install-business.json'
SETTINGS = ('snapshot-data-integrity', 'snapshot-data-integrity-immediate-check-after-snapshot-creation',
            'snapshot-data-integrity-cronjob')


def source_files(path, *, files_only=False):
    """Stopped-replica inventory; do not follow links or tolerate open users."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat

    path = Path(path)
    if path.resolve() != path or not path.is_dir(): raise ValueError('replica directory is not canonical')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit(): continue
        try:
            handles = [process/'cwd']+list((process/'fd').iterdir())
            for handle in handles:
                try: target = Path(os.readlink(handle))
                except FileNotFoundError: continue
                if target == path or path in target.parents:
                    raise ValueError('replica directory retains an active process user')
        except (FileNotFoundError, ProcessLookupError): continue
    rows = {}
    for file in path.iterdir():
        info = file.lstat()
        if not stat.S_ISREG(info.st_mode): raise ValueError('non-regular replica data entry')
        with file.open('rb') as stream:
            digest = hashlib.sha512()
            for block in iter(lambda: stream.read(1024*1024), b''):
                digest.update(block)
            checksum = digest.hexdigest()
        after = file.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('offline replica changed while hashing')
        rows[file.name] = {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
                           'mtime_ns': info.st_mtime_ns, 'sha512': checksum}
    # A journaled retirement may already have removed volume.meta. Its caller
    # must compare this exact remaining inventory with the frozen final files.
    if files_only: return {'files': rows}
    if 'volume.meta' not in rows: raise ValueError('replica lacks volume metadata')
    meta = json.loads((path/'volume.meta').read_text())
    if meta.get('Size') != 2*1024**3 or meta.get('BackingFileName') or meta.get('Rebuilding'):
        raise ValueError('export requires a complete 2 GiB standalone replica')
    return {'files': rows, 'meta': meta}


def host_export(request, action):
    """Private rescue/corrupt copies and a read-only, no-journal-replay mount."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import subprocess

    def run(argv, *, check=True):
        return subprocess.run(argv, check=check, capture_output=True, text=True, timeout=180)
    base = Path(request['base']); directory = base/('integrity-'+request['id'])
    if (base.resolve() != base or not re.fullmatch('[a-f0-9-]{36}', request['id']) or
            base.stat().st_dev == Path('/').stat().st_dev):
        raise ValueError('export requires its independent leased filesystem')
    fs = json.loads(run(['findmnt', '-J', '-M', str(base), '-o', 'TARGET,UUID']).stdout)['filesystems']
    if len(fs) != 1 or fs[0]['target'] != str(base) or fs[0].get('uuid') != request['uuid']:
        raise ValueError('export parent filesystem identity changed')
    receipt = directory/'fixture.json'; rescue = directory/'rescue'; corrupt = directory/'corrupt'; mount = directory/'mount'
    def persist(row):
        temporary = directory/'fixture.new'
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(receipt)
    if action == 'create':
        original = source_files(request['source'])
        if original != request['original']: raise ValueError('export source differs from offline consensus')
        if os.statvfs(base).f_bavail*os.statvfs(base).f_frsize < 8*1024**3:
            raise ValueError('export requires 8 GiB copy safety headroom')
        directory.mkdir(mode=0o700); rescue.mkdir(mode=0o700); corrupt.mkdir(mode=0o700); mount.mkdir(mode=0o700)
        row = {'request': request, 'phase': 'copy-intent', 'copies': {}}; persist(row)
        for destination in (rescue, corrupt):
            for name in original['files']:
                row['copy_intent'] = {'directory': destination.name, 'file': name}; persist(row)
                run(['cp', '--sparse=always', '--reflink=never', '--no-clobber', '--', str(Path(request['source'])/name), str(destination/name)])
            copied = source_files(destination)
            if {k: (v['size'], v['sha512']) for k, v in copied['files'].items()} != {
                    k: (v['size'], v['sha512']) for k, v in original['files'].items()}:
                raise ValueError('isolated replica copy differs from its original')
            row['copies'][destination.name] = copied; persist(row)
        if source_files(request['source']) != original: raise ValueError('export source changed during copy')
        snapshot = corrupt/request['snapshot_file']
        if snapshot.name not in original['files'] or not snapshot.name.endswith('.img'):
            raise ValueError('corruption target is outside its owned snapshot copy')
        fd = os.open(snapshot, os.O_RDWR|os.O_NOFOLLOW)
        try:
            offset = os.lseek(fd, 0, os.SEEK_DATA); before = os.pread(fd, 1, offset)
            if len(before) != 1: raise ValueError('snapshot copy has no allocated corruption target')
            row['corruption_intent'] = {'file': snapshot.name, 'offset': offset, 'original_byte': before.hex()}; persist(row)
            if os.pwrite(fd, bytes([before[0]^1]), offset) != 1: raise ValueError('short isolated corruption write')
            os.fsync(fd)
        finally: os.close(fd)
        corrupted = source_files(corrupt)
        changed = [name for name, entry in corrupted['files'].items() if entry['sha512'] != original['files'][name]['sha512']]
        if changed != [snapshot.name]: raise ValueError('isolated corruption exceeded one snapshot data file')
        row.update(phase='ready', corruption={'file': snapshot.name, 'offset': offset,
            'original_sha512': original['files'][snapshot.name]['sha512'],
            'corrupt_sha512': corrupted['files'][snapshot.name]['sha512']})
        row['copies']['corrupt'] = corrupted; persist(row)
    if directory.resolve() != directory or receipt.is_symlink(): raise ValueError('symlink export receipt')
    row = json.loads(receipt.read_text())
    if row['request'] != request or row['phase'] not in ('ready', 'mount-intent', 'mounted', 'unmount-intent'):
        raise ValueError('foreign or partial export fixture; retain for diagnosis')
    device = '/dev/longhorn/'+request['export_name']
    def mounted():
        result = run(['findmnt', '-J', '-M', str(mount), '-o', 'TARGET,SOURCE,OPTIONS'], check=False)
        if result.returncode == 1: return None
        if result.returncode: raise ValueError('cannot inspect export mount')
        rows = json.loads(result.stdout)['filesystems']
        if len(rows) != 1 or rows[0]['target'] != str(mount) or 'ro' not in rows[0]['options'].split(','):
            raise ValueError('export mount is not the exact read-only fixture')
        if Path(rows[0]['source']).resolve() != Path(device).resolve(): raise ValueError('export mount device changed')
        return rows[0]
    if action == 'read':
        if row['phase'] != 'ready': raise ValueError('export mount intent already exists; do not replay')
        info = Path(device).stat()
        if not stat.S_ISBLK(info.st_mode): raise ValueError('native export did not create its block device')
        fstype = run(['blkid', '-p', '-s', 'TYPE', '-o', 'value', device]).stdout.strip()
        if fstype != 'ext4': raise ValueError('export filesystem is not the product ext4 volume')
        row.update(phase='mount-intent', device={'major': os.major(info.st_rdev), 'minor': os.minor(info.st_rdev)})
        persist(row); run(['mount', '-t', 'ext4', '-o', 'ro,noload,nodev,nosuid', device, str(mount)])
        row['phase'] = 'mounted'; persist(row); observed = mounted()
        with (mount/'confirmed').open('rb') as stream:
            checksum = hashlib.sha256()
            for block in iter(lambda: stream.read(1024*1024), b''):
                checksum.update(block)
            digest = checksum.hexdigest()
        if digest != request['sha256'] or (mount/'confirmed').stat().st_size != request['payload_bytes']:
            raise ValueError('single-replica export differs from acknowledged application bytes')
        row['readback'] = {'sha256': digest, 'bytes': request['payload_bytes'], 'mount': observed}; persist(row)
    elif action == 'unmount':
        if mounted():
            info = Path(device).stat()
            if row.get('device') != {'major': os.major(info.st_rdev), 'minor': os.minor(info.st_rdev)}:
                raise ValueError('export block-device identity changed before unmount')
            row['phase'] = 'unmount-intent'; persist(row); run(['umount', str(mount)])
        if mounted(): raise ValueError('native export remains mounted')
        row['phase'] = 'ready'; persist(row)
    elif action == 'remove':
        if mounted() or Path(device).exists() or Path(device).is_symlink():
            raise ValueError('native export still owns a device or mount')
        if source_files(request['source']) != request['original']:
            raise ValueError('source replica changed before isolation cleanup')
        def identity(path):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode): raise ValueError('export copy directory identity changed')
            return [info.st_dev, info.st_ino]

        if set(row['copies']) != {'rescue', 'corrupt'}:
            raise ValueError('export retirement requires both original copies')
        preparing = 'removal' not in row
        if preparing:
            # The former remover persisted corrupt's exact final inventory
            # before unlinking it. Accept only that fully completed old step.
            legacy = (not corrupt.exists() and not corrupt.is_symlink() and
                      row.get('retired_copies') == {'corrupt': row['copies']['corrupt']})
            expected = {'fixture.json', 'rescue', 'mount'} | (set() if legacy else {'corrupt'})
            if set(p.name for p in directory.iterdir()) != expected:
                raise ValueError('foreign or missing object in private export directory')
            retirement = {'directories': {'root': identity(directory), 'mount': identity(mount)},
                          'copies': {}, 'deleted': [], 'removed_dirs': ['corrupt'] if legacy else []}
            if any(mount.iterdir()): raise ValueError('export mount directory is not empty')
            for name, copy in row['copies'].items():
                if name == 'corrupt' and legacy: continue
                target = directory/name
                retirement['directories'][name] = identity(target)
                names = {p.name for p in target.iterdir()}
                expected = set(copy['files'])
                if name == 'rescue' and names == expected | {'revision.counter'}:
                    # Engine 1fa902f... revision_counter.go initializes this
                    # private replica metadata to decimal zero + NUL padding.
                    # Only the acknowledged, read-only export may add it.
                    counter = target/'revision.counter'; info = counter.lstat()
                    readback = row.get('readback', {})
                    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or
                            info.st_size != 4096 or counter.read_bytes() != b'0'+b'\0'*4095 or
                            readback.get('sha256') != request['sha256'] or
                            readback.get('bytes') != request['payload_bytes']):
                        raise ValueError('unrecognized native revision counter or export readback')
                    expected.add('revision.counter')
                if names != expected: raise ValueError('export copy acquired foreign or missing paths')
                current = source_files(target)
                if any((entry['size'], entry['sha512']) !=
                       (copy['files'][filename]['size'], copy['files'][filename]['sha512'])
                       for filename, entry in current['files'].items() if filename.endswith('.img')):
                    raise ValueError('native read-only export changed replica image bytes')
                retirement['copies'][name] = current
            # Validate every copy before the first deletion; never delete the
            # healthy peer merely to discover the other's foreign entry.
            row['removal'] = retirement; persist(row)
        retirement = row['removal']
        if identity(directory) != retirement['directories']['root']:
            raise ValueError('private export directory was replaced')
        allowed = {'fixture.json'}
        for name in ('rescue', 'corrupt', 'mount'):
            target = directory/name
            if name in retirement['removed_dirs']:
                if target.exists() or target.is_symlink(): raise ValueError('retired export directory reappeared')
                continue
            if not target.exists() and not target.is_symlink() and retirement.get('rmdir_intent') == name:
                continue
            if identity(target) != retirement['directories'][name]:
                raise ValueError('export retirement directory identity changed')
            allowed.add(name)
            if name == 'mount':
                if any(target.iterdir()): raise ValueError('export mount directory is not empty')
                continue
            files = retirement['copies'][name]['files']
            expected = {filename for filename in files if name+'/'+filename not in retirement['deleted']}
            missing = expected - {p.name for p in target.iterdir()}
            if missing and {name+'/'+filename for filename in missing} != {retirement.get('unlink_intent')}:
                raise ValueError('unrecorded export file disappearance')
            if {p.name for p in target.iterdir()} != expected - missing:
                raise ValueError('export retirement acquired foreign paths')
            # Preserve the active-user, regular-file and byte guards even
            # when a previous journaled unlink already removed volume.meta.
            current = (files if preparing else source_files(target, files_only=True)['files'])
            if current != {filename: files[filename] for filename in expected - missing}:
                raise ValueError('export retirement file identity or content changed')
            if 'revision.counter' in current and stat.S_IMODE((target/'revision.counter').lstat().st_mode) != 0o600:
                raise ValueError('native revision counter mode changed')
        if {p.name for p in directory.iterdir()} != allowed:
            raise ValueError('foreign object in retiring export directory')
        for name in ('corrupt', 'rescue', 'mount'):
            if name in retirement['removed_dirs']: continue
            target = directory/name
            files = retirement['copies'].get(name, {}).get('files', {})
            for filename, saved in files.items():
                key = name+'/'+filename
                if key in retirement['deleted']: continue
                file = target/filename
                retirement['unlink_intent'] = key; persist(row)
                if file.exists() or file.is_symlink():
                    info = file.lstat()
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                            saved['device'], saved['inode'], saved['size'], saved['mtime_ns']):
                        raise ValueError('export retirement file changed before unlink')
                    file.unlink()
                retirement['deleted'].append(key); retirement.pop('unlink_intent', None); persist(row)
            retirement['rmdir_intent'] = name; persist(row)
            if target.exists() or target.is_symlink(): target.rmdir()
            retirement['removed_dirs'].append(name); retirement.pop('rmdir_intent', None); persist(row)
        # A hard interruption after unlinking this last receipt has no
        # remaining proof: retain the missing-receipt refusal on retry.
        receipt.unlink()
        try:
            directory.rmdir()
        except OSError:
            # Keep the completed journal available if final rmdir fails.
            persist(row)
            raise
        return {'removed': True}
    elif action not in ('create', 'probe'): raise ValueError('unsupported export action')
    return row


def remote(lab, address, function, *args):
    functions = [source_files] if function == source_files else [source_files, host_export]
    source = '\n'.join(inspect.getsource(f) for f in functions)
    source += '\nimport json,sys\na=json.load(sys.stdin)\nprint(json.dumps('+function.__name__+'(*a),sort_keys=True))\n'
    return json.loads(lab.remote(address, ['python3', '-c', source], stdin=json.dumps(args), timeout=300).stdout)


def save(lab, row): lab.write_private(lab.DIRECTORY/RECORD, row)


def receipt(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink(): raise ValueError('symlink integrity journal')
    row = json.loads(path.read_text()); state = lab.owner()
    if any(row[k] != state[k] for k in ('run_id', 'cluster_uid')):
        raise ValueError('foreign integrity journal')
    return row


def ready(pod):
    return bool(pod and any(c.get('type') == 'Ready' and c.get('status') == 'True'
        for c in pod.get('status', {}).get('conditions', [])))


def settings(lab):
    result = {}
    for name in SETTINGS:
        obj = lab.get('settings.longhorn.io', name, NS)
        if not obj: raise ValueError('missing snapshot integrity setting')
        result[name] = {'uid': obj['metadata']['uid'], 'value': obj['value']}
    return result


def patch_policy(lab, business, value, request=None):
    obj = lab.get('volumes.longhorn.io', business['volume'], NS)
    if not obj or obj['metadata']['uid'] != business['volume_uid']: raise ValueError('integrity source volume replaced')
    desired = dict(obj['spec'], snapshotDataIntegrity=value)
    if request is not None: desired['snapshotHashingRequestedAt'] = request
    lab.kubectl('-n', NS, 'patch', 'volumes.longhorn.io', business['volume'], '--type=json',
        '--patch-file=/dev/stdin', stdin=json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': business['volume_uid']},
            {'op': 'test', 'path': '/spec', 'value': obj['spec']},
            {'op': 'replace', 'path': '/spec', 'value': desired}]))


def checksum(lab, business, row, relay_pid):
    name = 'integrity-'+secrets.token_hex(12)
    request = {'kind': 'snapshot', 'name': name, 'volume': business['volume']}
    business['native_snapshots'] = {name: {'request': request}}
    lab.write_private(lab.DIRECTORY/BUSINESS, business)
    lab.run_requests(relay_pid, [request]); lab.bind_snapshots(business)
    obj = lab.get('snapshots.longhorn.io', name, NS)
    if not obj or not obj.get('status', {}).get('readyToUse'): raise ValueError('product integrity snapshot is not ready')
    volume = lab.get('volumes.longhorn.io', business['volume'], NS)
    if volume['spec'].get('dataEngine') != 'v1': raise ValueError('integrity requires the V1 native export engine')
    previous = volume['spec'].get('snapshotHashingRequestedAt', '')
    if previous != volume.get('status', {}).get('lastOnDemandSnapshotHashingCompleteAt', ''):
        raise ValueError('integrity cannot replace another checksum request')
    requested = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    row['checksum'] = {'snapshot': name, 'uid': obj['metadata']['uid'], 'requested_at': requested,
        'original_policy': volume['spec'].get('snapshotDataIntegrity', 'ignored')}
    save(lab, row); patch_policy(lab, business, 'fast-check', requested)
    def done():
        current = lab.get('volumes.longhorn.io', business['volume'], NS)
        snapshot = lab.get('snapshots.longhorn.io', name, NS)
        if (not current or current['metadata']['uid'] != business['volume_uid'] or
                current['spec'].get('snapshotHashingRequestedAt') != requested or
                not snapshot or snapshot['metadata']['uid'] != row['checksum']['uid']):
            raise ValueError('integrity checksum request or snapshot replaced')
        if current.get('status', {}).get('lastOnDemandSnapshotHashingCompleteAt') != requested: return None
        if not snapshot.get('status', {}).get('checksum') or not snapshot['status'].get('checksumCalculatedAt'):
            raise ValueError('snapshot checksum completion lacks checksum and timestamp')
        return snapshot
    result = lab.wait('integrity-native-checksum', 900, done)
    row['checksum'].update(checksum=result['status']['checksum'], calculated_at=result['status']['checksumCalculatedAt'],
        full_rehash_proven=False)
    patch_policy(lab, business, row['checksum']['original_policy']); save(lab, row)
    print('LONGHORN_INTEGRITY_SNAPSHOT_CHECKSUM '+json.dumps(row['checksum'], sort_keys=True), flush=True)
    return name


def prepare(lab, row):
    namespace = 'longhorn-business-'+secrets.token_hex(8); token = secrets.token_hex(16)
    row['namespace_intent'] = {'name': namespace, 'token': token}; save(lab, row)
    ns = json.loads(lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps({'apiVersion': 'v1',
        'kind': 'Namespace', 'metadata': {'name': namespace, 'annotations': {'kubeauto.io/integrity-token': token}}})).stdout)
    business = {'namespace': namespace, 'namespace_uid': ns['metadata']['uid'], 'objects': {},
        'storage_class': 'longhorn', 'reclaim_policy': 'Retain'}
    record = lab.DIRECTORY/BUSINESS; lab.write_private(record, business)
    business['defaults'] = lab.wait('integrity-namespace-defaults', 60, lambda: lab.namespace_defaults(namespace))
    lab.write_private(record, business)
    lab.create_business_object(business, record, {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim',
        'metadata': {'name': 'data', 'namespace': namespace}, 'spec': {'accessModes': ['ReadWriteOnce'],
        'storageClassName': 'longhorn', 'resources': {'requests': {'storage': '2Gi'}}}})
    def bound():
        claim = lab.get('pvc', 'data', namespace)
        if not claim or claim.get('status', {}).get('phase') != 'Bound': return False
        lab.bind_business_volume(business, record); return bool(business.get('volume_uid'))
    lab.wait('integrity-claim-bound', 600, bound)
    return business


def check_data(lab, business, row):
    actual = lab.kubectl('-n', business['namespace'], 'exec', row['client'], '--', 'python3', '-c',
        lab.payload_program(row['run_id'], read_only=True)).stdout.strip()
    if actual != row['digest']: raise ValueError('integrity changed original acknowledged bytes')


def detached(lab, business):
    obj = lab.get('volumes.longhorn.io', business['volume'], NS)
    if not obj or obj['metadata']['uid'] != business['volume_uid']: raise ValueError('integrity detached volume replaced')
    if obj.get('status', {}).get('state') != 'detached' or obj['spec'].get('nodeID'):
        return False
    for kind in ('engines.longhorn.io', 'replicas.longhorn.io'):
        if any(o['spec'].get('volumeName') == business['volume'] and (
                o.get('status', {}).get('currentState') != 'stopped' or o['spec'].get('desireState') != 'stopped')
                for o in lab.get(kind, namespace=NS)['items']): return False
    return True


def export_pod(business, row):
    request = row['export']; directory = request['base']+'/integrity-'+request['id']
    # Engine commit 1fa902f0061b84b92af445c38392bfeb99faffb2 ships a
    # comma-separated port prefix that the IM CLI parses as two StringSlice
    # entries. Patch only this exact upstream helper in the disposable Pod.
    launch = """helper=$(command -v launch-simple-longhorn)
printf '%s  %s\\n' efe69a0768dc05575e452d69edfb890e0a307fc1526db676fd6394a47196ce36 "$helper" | sha256sum -c -
copy=$(mktemp /tmp/longhorn-integrity-export.XXXXXX)
trap 'rm -f -- "$copy"' EXIT
sed 's/--listen,localhost:/--listen=localhost:/g' "$helper" > "$copy"
exec bash "$copy" "$@"
"""
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'native-export', 'namespace': business['namespace']},
        'spec': {'nodeName': row['node'], 'hostPID': True, 'restartPolicy': 'Never',
            'containers': [{'name': 'engine', 'image': row['engine_image'], 'securityContext': {'privileged': True},
                'command': ['bash', '-ec', launch, 'longhorn-native-export'],
                'args': [request['export_name'], str(2*1024**3)],
                'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '2', 'memory': '512Mi'}},
                'volumeMounts': [{'name': n, 'mountPath': p} for n, p in (
                    ('dev', '/host/dev'), ('proc', '/host/proc'), ('copy', '/volume'))]}],
            'volumes': [{'name': 'dev', 'hostPath': {'path': '/dev', 'type': 'Directory'}},
                {'name': 'proc', 'hostPath': {'path': '/proc', 'type': 'Directory'}},
                {'name': 'copy', 'hostPath': {'path': directory+'/rescue', 'type': 'Directory'}}]}}


def archive_clean_failure(lab, row):
    """Retire only a proven empty, restored failed receipt; preserve its evidence."""
    if not row.get('clean') or 'host_intent' in row or 'host_ready' in row:
        raise ValueError('integrity receipt is not an empty cleaned failed attempt')
    if row.get('passed'):
        # Business readback may finish before a later cleanup fails. Retire
        # that failed attempt only after the same empty-boundary checks below;
        # keep its business fact without signing the incomplete module PASS.
        exported, readback = row.get('export', {}), row.get('export_readback', {})
        if (not exported.get('sha256') or exported['sha256'] != row.get('digest') or
                readback.get('sha256') != exported['sha256'] or
                not exported.get('payload_bytes') or readback.get('bytes') != exported['payload_bytes']):
            raise ValueError('passed business lacks its acknowledged export readback')
    business = lab.DIRECTORY/BUSINESS
    if business.exists() or business.is_symlink():
        raise ValueError('integrity business receipt remains after cleanup')
    intent = row.get('namespace_intent')
    if intent and lab.get('namespace', intent['name']) is not None:
        raise ValueError('integrity namespace remains after cleanup')
    if (settings(lab) != row['settings'] or lab.shared_snapshot_identity() != row['shared'] or
            hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != row['config_sha256']):
        raise ValueError('integrity original settings, shared objects or input changed before recovery')
    lab.load_helper('longhorn-platform.py').empty(lab); lab.installation_health()
    row['recovery_verified'] = {'namespace_absent': True, 'native_objects_absent': True,
        'host_intent_absent': True, 'original_settings_shared_and_input': True,
        'scope': 'failed-attempt-cleanup-only', 'delivery_pass': False}
    path = lab.DIRECTORY/RECORD
    archived = path.with_name('longhorn-integrity-recovered-'+secrets.token_hex(8)+'.json')
    if archived.exists() or archived.is_symlink():
        raise ValueError('integrity recovery archive already exists')
    save(lab, row); path.rename(archived)


def clean(lab, relay_pid, *, failed=False):
    path = lab.DIRECTORY/RECORD
    if not path.exists(): return
    row = receipt(lab)
    if row.get('clean'):
        lab.installation_health()
        if failed: archive_clean_failure(lab, row)
        return
    record = lab.DIRECTORY/BUSINESS
    if not record.exists() and row.get('namespace_intent'):
        intent = row['namespace_intent']; ns = lab.get('namespace', intent['name'])
        if ns:
            if ns['metadata'].get('annotations', {}).get('kubeauto.io/integrity-token') != intent['token']:
                raise ValueError('integrity namespace intent differs')
            lab.write_private(record, {'namespace': intent['name'], 'namespace_uid': ns['metadata']['uid'], 'objects': {},
                'storage_class': 'longhorn', 'reclaim_policy': 'Retain',
                'defaults': lab.wait('integrity-recovery-defaults', 60, lambda: lab.namespace_defaults(intent['name']))})
    if record.exists():
        business = json.loads(record.read_text()); lab.check_business_graph(business)
        if row.get('host_ready'):
            remote(lab, row['address'], host_export, row['export'], 'unmount')
        pod = lab.get('pod', 'native-export', business['namespace'])
        if pod:
            if pod['metadata']['uid'] != business['objects'].get('Pod/native-export'):
                raise ValueError('native export Pod was replaced')
            lab.remove(pod); lab.wait('integrity-export-pod-retired', 180,
                lambda: lab.get('pod', 'native-export', business['namespace']) is None)
        if row.get('host_ready'):
            lab.wait('integrity-native-export-device-retired', 180, lambda: lab.remote(row['address'],
                ['python3', '-c', 'import os,sys;sys.exit(os.path.lexists(sys.argv[1]))',
                    '/dev/longhorn/'+row['export']['export_name']], check=False).returncode == 0)
            remote(lab, row['address'], host_export, row['export'], 'remove')
            row.pop('host_ready'); row.pop('host_intent', None); save(lab, row)
        elif row.get('host_intent'):
            raise ValueError('partial export copy needs diagnosis before original reattachment')
        if row.get('checksum'):
            patch_policy(lab, business, row['checksum']['original_policy'])
        if row.get('digest'):
            client = lab.get('pod', row['client'], business['namespace'])
            if not client:
                row['client'] = 'recovery-'+secrets.token_hex(6); save(lab, row)
                lab.create_business_object(business, record, lab.client_pod(
                    business['namespace'], row['client'], 'data', row['node']))
            lab.wait('integrity-original-client-recovered', 600, lambda: ready(lab.get('pod', row['client'], business['namespace'])))
            lab.wait('integrity-original-three-replicas', 900, lambda: lab.replica_topology(business, 3))
            check_data(lab, business, row)
        lab.cleanup_business(record, relay_pid=relay_pid)
    lab.installation_health(); row['clean'] = True; save(lab, row)
    if failed: archive_clean_failure(lab, row)


def business(lab, relay_pid):
    lab.load_helper('longhorn-platform.py').empty(lab)
    if any((lab.DIRECTORY/n).exists() or (lab.DIRECTORY/n).is_symlink() for n in (RECORD, BUSINESS)):
        raise ValueError('previous integrity fixture needs scoped recovery')
    lab.supervise_core(relay_pid, 'longhorn-setup'); lab.installation_health(); state = lab.owner()
    row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'settings': settings(lab),
        'node': 'longhorn-node-'+lab.WORKERS[0].rsplit('.', 1)[1], 'address': lab.WORKERS[0],
        'shared': lab.shared_snapshot_identity(),
        'config_sha256': hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()}
    save(lab, row)
    with lab.scoped_cleanup(lambda: clean(lab, relay_pid)):
        fixture = prepare(lab, row); record = lab.DIRECTORY/BUSINESS
        row['client'] = 'writer'; save(lab, row)
        lab.create_business_object(fixture, record, lab.client_pod(fixture['namespace'], 'writer', 'data', row['node']))
        lab.wait('integrity-writer-ready', 600, lambda: ready(lab.get('pod', 'writer', fixture['namespace'])))
        placement = lab.wait('integrity-three-replicas', 600, lambda: lab.replica_topology(fixture, 3))
        row['digest'] = lab.kubectl('-n', fixture['namespace'], 'exec', 'writer', '--', 'python3', '-c',
            lab.payload_program(row['run_id'])).stdout.strip()
        if row['digest'] != hashlib.sha256(row['run_id'].encode()*262144).hexdigest():
            raise ValueError('integrity initial fsync acknowledgement differs')
        save(lab, row); snapshot = checksum(lab, fixture, row, relay_pid)
        engines = [e for e in lab.get('engines.longhorn.io', namespace=NS)['items'] if e['spec'].get('volumeName') == fixture['volume']]
        if len(engines) != 1 or not engines[0]['spec']['image'].endswith('/longhorn-engine:v1.12.1'):
            raise ValueError('native recovery requires the accepted pinned engine image')
        row['engine_image'] = engines[0]['spec']['image']; save(lab, row)
        writer = lab.get('pod', 'writer', fixture['namespace']); lab.remove(writer)
        lab.wait('integrity-original-writer-retired', 180, lambda: lab.get('pod', 'writer', fixture['namespace']) is None)
        lab.wait('integrity-source-detached', 300, lambda: detached(lab, fixture))
        inventories = {}; sources = {}
        for name, saved in placement.items():
            replica = lab.get('replicas.longhorn.io', name, NS)
            if (not replica or replica['metadata']['uid'] != saved['uid'] or
                    replica['spec'].get('diskPath') != lab.DATA or replica['spec'].get('volumeName') != fixture['volume']):
                raise ValueError('integrity original replica identity or disk changed')
            leaf = replica['spec']['dataDirectoryName']
            if Path(leaf).name != leaf or not leaf.startswith(fixture['volume']+'-'):
                raise ValueError('integrity replica directory is outside owned source')
            address = next((ip for ip in lab.WORKERS if saved['node'] == 'longhorn-node-'+ip.rsplit('.', 1)[1]), None)
            if not address: raise ValueError('integrity source is outside the worker lease')
            source = lab.DATA+'/replicas/'+leaf; sources[address] = source
            inventories[address] = remote(lab, address, source_files, source)
        snapshot_file = 'volume-snap-'+snapshot+'.img'
        digests = {entry['files'][snapshot_file]['sha512'] for entry in inventories.values()}
        if len(inventories) != 3 or len(digests) != 1:
            raise ValueError('offline snapshot replicas do not have an unambiguous three-peer consensus')
        row['offline_consensus'] = {'snapshot_file': snapshot_file, 'sha512': next(iter(digests)),
            'replicas': {address: {'path': sources[address], 'snapshot': entry['files'][snapshot_file]} for address, entry in inventories.items()}}
        identifier = str(uuid.uuid4())
        row['export'] = {'id': identifier, 'base': lab.DATA, 'uuid': state['disks'][row['address']]['uuid'],
            'source': sources[row['address']], 'original': inventories[row['address']], 'snapshot_file': snapshot_file,
            'export_name': 'integrity-'+identifier, 'sha256': row['digest'], 'payload_bytes': len(row['run_id'].encode())*262144}
        row['host_intent'] = True; save(lab, row)
        result = remote(lab, row['address'], host_export, row['export'], 'create')
        row['host_ready'] = True; row['corruption'] = dict(result['corruption'],
            copy_path=lab.DATA+'/integrity-'+identifier+'/corrupt',
            copy_file=result['copies']['corrupt']['files'][snapshot_file]); save(lab, row)
        print('LONGHORN_INTEGRITY_ISOLATED_CORRUPTION '+json.dumps({
            'offline_consensus': row['offline_consensus'], 'isolated_copy': row['corruption'],
            'live_replica_modified': False}, sort_keys=True), flush=True)
        lab.create_business_object(fixture, record, export_pod(fixture, row))
        def device_ready():
            pod = lab.get('pod', 'native-export', fixture['namespace'])
            if not pod or pod['metadata']['uid'] != fixture['objects']['Pod/native-export']:
                raise ValueError('native recovery Pod identity changed')
            if pod.get('status', {}).get('phase') == 'Failed': raise ValueError('native recovery engine failed')
            return lab.remote(row['address'], ['test', '-b', '/dev/longhorn/'+row['export']['export_name']], check=False).returncode == 0
        lab.wait('integrity-native-export-block-device', 300, device_ready)
        exported = remote(lab, row['address'], host_export, row['export'], 'read')
        row['export_readback'] = exported['readback']; row['passed'] = True; save(lab, row)
        print('LONGHORN_INTEGRITY_NATIVE_EXPORT_READBACK '+json.dumps(row['export_readback'], sort_keys=True), flush=True)
    print('LONGHORN_INTEGRITY_BUSINESS_PASS', flush=True)


def finish(lab):
    row = receipt(lab)
    if not row.get('passed') or not row.get('clean') or (lab.DIRECTORY/BUSINESS).exists():
        raise ValueError('integrity business or cleanup incomplete')
    if (settings(lab) != row['settings'] or lab.shared_snapshot_identity() != row['shared'] or
            hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest() != row['config_sha256']):
        raise ValueError('integrity changed global settings, shared objects or original input')
    lab.load_helper('longhorn-platform.py').empty(lab); lab.installation_health()
    (lab.DIRECTORY/RECORD).unlink()
    print('LONGHORN_INTEGRITY_FIXTURES_CLEAN_PASS', flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('business', 'clean', 'finish'))
    parser.add_argument('--relay-pid', type=int, default=0); args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('longhorn_integrity_lab', Path(__file__).with_name('longhorn-lab.py'))
    lab = importlib.util.module_from_spec(spec); sys.modules[spec.name] = lab; spec.loader.exec_module(lab)
    lab.require_lease()
    if args.action == 'business': business(lab, args.relay_pid)
    elif args.action == 'clean': clean(lab, args.relay_pid, failed=True)
    else: finish(lab)


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    main()
