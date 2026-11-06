#!/usr/bin/env python3
"""Owned loop filesystem fixture, following the qualified Longhorn V1 fixture.

No raw device is accepted. Identity binds the host, UUID, file inode, loop
association and exact fstab line; cleanup refuses native product receipts.
This proves CSI integration, not physical disk performance.
"""

def host_filesystem(request, action, root='/'):
    """Own the file inode, loop association, mount UUID and exact fstab line."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import shutil
    import stat
    import subprocess
    import uuid

    root = Path(root)
    token = str(uuid.UUID(request['id']))
    fsid = str(uuid.UUID(request['uuid']))
    base = root/'var/lib/kubeauto-longhorn-elasticsearch-v1'
    directory = base/token
    image = directory/'disk.img'
    record = directory/'owner.json'
    mount = root/'data/kubeauto-longhorn-elasticsearch-v1'
    fstab = root/'etc/fstab'
    line = str(image)+' '+str(mount)+' ext4 loop,defaults 0 0 # kubeauto-longhorn-elasticsearch-v1:'+token+'\n'
    if action not in ('prepare', 'check', 'disk-identity', 'remove'):
        raise ValueError('unknown Elasticsearch filesystem action')
    for path in (base, directory, image, record, mount.parent, mount, fstab):
        if path.is_symlink(): raise ValueError('symlink Elasticsearch fixture')
    machine = (root/'etc/machine-id').read_text().strip()
    if machine != request['machine_id'] or request['size'] != 32*1024**3:
        raise ValueError('Elasticsearch host or capacity changed')

    def run(argv, *, check=True):
        return subprocess.run(argv, check=check, text=True, capture_output=True, timeout=120)

    def identity(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('foreign Elasticsearch file')
        return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size}

    def save(row):
        temporary = directory/'owner.new'
        fd = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(row, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(record)

    def config():
        lines = fstab.read_text().splitlines(True)
        if lines.count(line) > 1 or any(str(mount) in s and s != line and not s.lstrip().startswith('#') for s in lines):
            raise ValueError('foreign or duplicate Elasticsearch fstab entry')
        return lines

    def filesystem():
        result = run(['findmnt', '-J', '-M', str(mount), '-o', 'TARGET,SOURCE,FSTYPE,UUID'], check=False)
        if result.returncode == 1: return None
        if result.returncode: raise ValueError('cannot inspect Elasticsearch mount')
        rows = json.loads(result.stdout)['filesystems']
        if len(rows) != 1 or any(rows[0].get(k) != v for k, v in (
                ('target', str(mount)), ('fstype', 'ext4'), ('uuid', fsid))):
            raise ValueError('Elasticsearch mount identity changed')
        loops = json.loads(run(['losetup', '-J', '-l', '-O', 'NAME,BACK-FILE', rows[0]['source']]).stdout)['loopdevices']
        if len(loops) != 1 or loops[0].get('back-file') != str(image):
            raise ValueError('Elasticsearch loop backing file changed')
        return rows[0]

    if action == 'prepare' and not directory.exists():
        if mount.exists() or line in config(): raise ValueError('Elasticsearch path already exists')
        for path in (root/'etc/kubeauto/longhorn-disk.json',
                     root/'etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'):
            if path.exists() or path.is_symlink(): raise ValueError('pre-existing V1 host ownership')
        if shutil.disk_usage(root/'var/lib').free < request['size'] + 8*1024**3:
            raise ValueError('Elasticsearch requires its full 32 GiB image plus 8 GiB backing-filesystem headroom')
        base.mkdir(mode=0o700, exist_ok=True); directory.mkdir(mode=0o700)
        save({'id': token, 'uuid': fsid, 'machine_id': machine, 'phase': 'intent'})
    if not record.exists():
        if (action == 'remove' and not directory.exists() and not mount.exists()
                and line not in config() and not filesystem()):
            return {'removed': True, 'never_created': True, 'machine_id': machine, 'uuid': fsid}
        raise ValueError('missing Elasticsearch filesystem ownership')
    row = json.loads(record.read_text())
    if any(row.get(k) != v for k, v in (('id', token), ('uuid', fsid), ('machine_id', machine))):
        raise ValueError('foreign Elasticsearch journal')
    parent = row.get('created_parent')
    if parent:
        info = mount.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                parent != {'device': info.st_dev, 'inode': info.st_ino}):
            raise ValueError('Elasticsearch parent directory changed')
    elif row.get('parent_intent') and mount.parent.exists():
        raise ValueError('unconfirmed Elasticsearch parent creation')
    if action == 'prepare':
        if row['phase'] == 'intent':
            # A lost creation response leaves the exact inode in this host journal.
            fd = os.open(image, os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream: stream.truncate(request['size']); os.fsync(stream.fileno())
            row.update(file=identity(image), phase='file'); save(row)
        if identity(image) != row['file']: raise ValueError('Elasticsearch file inode changed')
        if row['phase'] == 'file':
            run(['mkfs.ext4', '-F', '-U', fsid, str(image)])
            row['phase'] = 'formatted'; save(row)
        if row['phase'] == 'formatted':
            if mount.exists(): raise ValueError('Elasticsearch mount path appeared')
            if not mount.parent.exists():
                row['parent_intent'] = True; save(row)
                mount.parent.mkdir(mode=0o755)
                info = mount.parent.lstat()
                row['created_parent'] = {'device': info.st_dev, 'inode': info.st_ino}; save(row)
            mount.mkdir(mode=0o755)
            (mount/'.es-fixture-owner').write_text(token+'\n')
            lines = config()
            if line not in lines:
                with fstab.open('a') as stream: stream.write(line); stream.flush(); os.fsync(stream.fileno())
            row['phase'] = 'mount-intent'; save(row)
        if row['phase'] == 'mount-intent':
            if not filesystem(): run(['mount', str(mount)])
            row['phase'] = 'mounted'; save(row)
    if image.exists() and identity(image) != row.get('file'):
        raise ValueError('Elasticsearch backing file replaced')
    if action in ('check', 'prepare', 'disk-identity'):
        current = filesystem()
        if not current or line not in config() or row['phase'] != 'mounted':
            raise ValueError('Elasticsearch filesystem not persistently mounted')
        if shutil.disk_usage(mount).total < 30*1024**3:
            raise ValueError('Elasticsearch usable filesystem capacity is below 30 GiB')
        result = {'uuid': fsid, 'file': row['file'], 'mount': current, 'kind': 'loop-test-fixture'}
        if action == 'disk-identity':
            filename = mount/'longhorn-disk.cfg'
            if filename.is_symlink() or not filename.is_file():
                raise ValueError('missing or foreign native filesystem identity')
            native = json.loads(filename.read_text())
            result['disk_uuid'] = str(uuid.UUID(native['diskUUID']))
        return result
    # Product host receipts must have been released, and native references must
    # have been proved absent by the controller before this local phase.
    for path in (root/'etc/kubeauto/longhorn-disk.json',
                 root/'etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'):
        if path.exists() or path.is_symlink(): raise ValueError('Elasticsearch V1 host receipt remains')
    if filesystem(): run(['umount', str(mount)])
    loops = json.loads(run(['losetup', '-J', '-l', '-j', str(image)]).stdout)['loopdevices'] if image.exists() else []
    if loops: raise ValueError('Elasticsearch backing file retains a loop consumer')
    if mount.exists():
        owner = mount/'.es-fixture-owner'
        if {p.name for p in mount.iterdir()} != {owner.name} or owner.read_text() != token+'\n':
            raise ValueError('Elasticsearch underlay has foreign data')
        owner.unlink(); mount.rmdir()
    lines = config()
    if line in lines:
        temporary = fstab.with_name('.fstab.elasticsearch-v1-'+token)
        info = fstab.stat()
        with temporary.open('x') as stream:
            stream.writelines(s for s in lines if s != line); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode)); os.chown(temporary, info.st_uid, info.st_gid)
        if fstab.read_text().splitlines(True) != lines: raise ValueError('fstab changed during Elasticsearch cleanup')
        temporary.replace(fstab)
    if image.exists(): image.unlink()
    if {p.name for p in directory.iterdir()} != {record.name}:
        raise ValueError('foreign Elasticsearch fixture file remains')
    record.unlink(); directory.rmdir()
    if row.get('created_parent') and not any(mount.parent.iterdir()):
        mount.parent.rmdir()
    return {'removed': True, 'machine_id': machine, 'uuid': fsid}



def host_sysctl(request=None, action='snapshot', root='/'):
    """Lease-owned sysctl rollback; refuse changes made by another operator."""
    import base64
    import os
    from pathlib import Path
    import re
    import stat
    import subprocess
    import tempfile

    root = Path(root)
    path = root / 'etc/sysctl.d/99-kubeauto-elasticsearch.conf'
    machine = (root / 'etc/machine-id').read_text().strip()

    def current():
        result = subprocess.run(['sysctl', '-n', 'vm.max_map_count'], check=True, text=True, capture_output=True)
        return int(result.stdout.strip())

    def original_file():
        if path.is_symlink() or path.exists() and not path.is_file():
            raise ValueError('sysctl ownership refuses non-regular file')
        if not path.exists(): return None
        info = path.stat()
        if info.st_size > 16384: raise ValueError('sysctl file exceeds ownership boundary')
        return {'data': base64.b64encode(path.read_bytes()).decode(), 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}

    if action == 'snapshot':
        saved = original_file()
        runtime = current()
        numbers = re.findall(rb'^vm.max_map_count=([0-9]+)$', base64.b64decode(saved['data']) if saved else b'', re.M)
        if len(numbers) > 1: raise ValueError('ambiguous existing sysctl value')
        target = max(runtime, int(numbers[0]) if numbers else 0, 1048576)
        return {'machine_id': machine, 'runtime': runtime, 'file': saved, 'expected_runtime': target,
                'expected_data': base64.b64encode(('vm.max_map_count=' + str(target) + '\n').encode()).decode()}
    if action != 'restore' or not request or request['machine_id'] != machine:
        raise ValueError('sysctl lease or host differs')
    present, runtime = original_file(), current()
    if present == request['file'] and runtime == request['runtime']:
        return {'restored': True}
    product_file = present and present['data'] == request['expected_data']
    if ((not product_file and present != request['file']) or
            runtime not in (request['runtime'], request['expected_runtime'])):
        raise ValueError('sysctl changed outside the owning fixture; preserve it')
    if request['file'] is None:
        path.unlink(missing_ok=True)
    else:
        prior = request['file']
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.es-restore-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(base64.b64decode(prior['data'])); stream.flush(); os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), prior['mode']); os.fchown(stream.fileno(), prior['uid'], prior['gid'])
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    subprocess.run(['sysctl', '-w', 'vm.max_map_count=' + str(request['runtime'])], check=True, capture_output=True)
    if original_file() != request['file'] or current() != request['runtime']:
        raise ValueError('sysctl rollback readback differs')
    return {'restored': True}
