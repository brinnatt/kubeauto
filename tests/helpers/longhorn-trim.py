#!/usr/bin/env python3
"""V1 filesystem trim: distinguish guest free space, snapshots and host allocation."""
import hashlib
import inspect
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess


SIZE = 64 * 1024**2


def filler(path, action, identity=None):
    """Create/fsync/read real bytes, or unlink only that exact regular file."""
    if action not in ('create', 'remove'):
        raise ValueError('unknown trim filler action')
    if action == 'create':
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            digest = hashlib.sha256()
            for offset in range(0, SIZE, 1024**2):
                data = os.urandom(1024**2)
                if os.pwrite(descriptor, data, offset) != len(data):
                    raise ValueError('short trim filler write')
                digest.update(data)
            os.fsync(descriptor)
            actual = hashlib.sha256()
            for offset in range(0, SIZE, 1024**2):
                actual.update(os.pread(descriptor, 1024**2, offset))
            if actual.digest() != digest.digest():
                raise ValueError('trim filler readback differs')
            info = os.fstat(descriptor)
            result = {'device': info.st_dev, 'inode': info.st_ino,
                      'size': info.st_size, 'sha256': digest.hexdigest()}
        finally:
            os.close(descriptor)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or
                    {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size} !=
                    {key: identity[key] for key in ('device', 'inode', 'size')}):
                raise ValueError('trim filler identity changed')
            digest = hashlib.sha256()
            for offset in range(0, SIZE, 1024**2):
                digest.update(os.pread(descriptor, 1024**2, offset))
            current = os.stat(path, follow_symlinks=False)
            if (digest.hexdigest() != identity['sha256'] or not stat.S_ISREG(current.st_mode) or
                    (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, info.st_size)):
                raise ValueError('trim filler bytes or path changed')
            os.unlink(path)
        finally:
            os.close(descriptor)
        result = dict(identity)
    parent = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
        space = os.fstatvfs(parent)
        result['free_bytes'] = space.f_frsize * space.f_bavail
    finally:
        os.close(parent)
    return result


def allocated(path, mount):
    """Read actual allocated blocks; never follow a link or open replica data."""
    root = Path(path); base = Path(mount)
    if (base.resolve() != base or root.resolve() != root or root.parent != base / 'replicas' or
            not root.is_dir() or base.stat().st_dev == Path('/').stat().st_dev):
        raise ValueError('trim measurement is outside its independent replica directory')
    result = 0
    for entry in root.iterdir():
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('unexpected non-file in trim replica directory')
        result += info.st_blocks * 512
    return result


def mounted_allocation(path, mount, expected_uuid):
    result = subprocess.run(['findmnt', '--json', '--mountpoint', mount, '-o', 'TARGET,UUID,FSTYPE'],
                            check=True, capture_output=True, text=True, timeout=30)
    mounted = json.loads(result.stdout)['filesystems']
    if (not expected_uuid or len(mounted) != 1 or mounted[0].get('target') != mount or
            mounted[0].get('uuid') != expected_uuid or mounted[0].get('fstype') not in ('ext4', 'xfs')):
        raise ValueError('trim replica filesystem identity changed')
    return allocated(path, mount)


class Probe:
    def __init__(self, lab, journal, topology):
        self.lab = lab
        self.journal = journal
        self.topology = topology
        self.snapshot = ''
        self.removed_only = False

    def engine(self):
        volume = self.lab.get('volumes.longhorn.io', self.journal['volume'], 'longhorn-system')
        if (not volume or volume['metadata']['uid'] != self.journal['volume_uid'] or
                volume['spec'].get('dataEngine') != 'v1' or volume['spec'].get('encrypted') or
                volume['spec'].get('accessMode') != 'rwo' or
                self.lab.replica_topology(self.journal, 3) != self.topology):
            raise ValueError('trim requires its original healthy unencrypted V1 RWO replicas')
        engines = [obj for obj in self.lab.get('engines.longhorn.io', namespace='longhorn-system')['items']
                   if obj['spec'].get('volumeName') == self.journal['volume'] and
                   obj['spec'].get('nodeID') == volume['status'].get('currentNodeID') and
                   obj.get('status', {}).get('currentState') == 'running']
        if len(engines) != 1:
            raise ValueError('trim requires one running engine')
        engine = engines[0]
        if any(info.get('usercreated') and name != self.snapshot
               for name, info in engine.get('status', {}).get('snapshots', {}).items()):
            raise ValueError('trim refuses an unowned user snapshot')
        snapshot = engine.get('status', {}).get('snapshots', {}).get(self.snapshot)
        if self.removed_only and snapshot and not snapshot.get('removed'):
            raise ValueError('trim original snapshot is still a valid recovery point')
        if any(row.get('error') for row in engine.get('status', {}).get('purgeStatus', {}).values()):
            raise ValueError('trim native purge reported an error')
        return engine

    def action(self, action):
        if action not in ('trimFilesystem', 'snapshotPurge'):
            raise ValueError('unsupported native trim action')
        engine = self.engine()
        namespace = self.lab.get('namespace', 'longhorn-system')
        daemonset = self.lab.get('daemonset', 'longhorn-manager', 'longhorn-system')
        if (not namespace or namespace['metadata']['uid'] != self.lab.owner()['namespace_uid'] or not daemonset):
            raise ValueError('trim manager namespace identity changed')
        managers = [pod for pod in self.lab.get('pods', namespace='longhorn-system')['items']
                    if pod['metadata'].get('labels', {}).get('app') == 'longhorn-manager' and
                    pod['spec'].get('nodeName') == engine['spec']['nodeID'] and
                    any(ref.get('controller') and ref.get('kind') == 'DaemonSet' and
                        ref.get('uid') == daemonset['metadata']['uid']
                        for ref in pod['metadata'].get('ownerReferences', []))]
        if len(managers) != 1:
            raise ValueError('trim requires one original manager on the attached node')
        manager = managers[0]
        if (manager['metadata'].get('deletionTimestamp') or manager.get('status', {}).get('phase') != 'Running' or
                not any(row.get('type') == 'Ready' and row.get('status') == 'True'
                        for row in manager.get('status', {}).get('conditions', []))):
            raise ValueError('trim manager is not ready')
        address = ipaddress.ip_address(manager['status'].get('podIP', ''))
        current = self.lab.get('pod', manager['metadata']['name'], 'longhorn-system')
        if not current or current['metadata']['uid'] != manager['metadata']['uid'] or current['status'] != manager['status']:
            raise ValueError('trim manager was replaced or changed before the native action')
        host = '[' + str(address) + ']' if address.version == 6 else str(address)
        path = 'http://' + host + ':9500/v1/volumes/' + self.journal['volume'] + '?action=' + action
        # The default upstream policy admits manager Pods, not API Server
        # Service proxy traffic. Use the image's curl without relaxing ingress.
        result = json.loads(self.lab.kubectl('--request-timeout=170s', '-n', 'longhorn-system', 'exec', '-i',
                    manager['metadata']['name'], '-c', 'longhorn-manager', '--', 'curl', '--fail', '--silent',
                    '--show-error', '--max-time', '150', '--request', 'POST', '--header', 'Content-Type: application/json',
                    '--data-binary', '@-', path, stdin='{}', timeout=180).stdout)
        if result.get('id') != self.journal['volume']:
            raise ValueError('native trim action returned a different volume')

    def sample(self, stage):
        self.engine()
        replicas = {obj['metadata']['name']: obj for obj in
                    self.lab.get('replicas.longhorn.io', namespace='longhorn-system')['items']
                    if obj['metadata']['name'] in self.topology}
        result = {}
        for name, original in self.topology.items():
            replica = replicas[name]
            node = replica['spec']['nodeID']
            allowed = {'longhorn-node-' + ip.rsplit('.', 1)[1]: ip for ip in self.lab.WORKERS}
            directory = replica['spec'].get('dataDirectoryName', '')
            if (replica['metadata']['uid'] != original['uid'] or node != original['node'] or node not in allowed or
                    replica['spec'].get('diskPath') != self.lab.DATA or not directory or
                    Path(directory).name != directory or directory in ('.', '..')):
                raise ValueError('trim replica allocation identity changed')
            disk = self.lab.owner()['disks'].get(allowed[node], {})
            if disk.get('path') != self.lab.DATA or disk.get('phase') != 'mounted' or not disk.get('uuid'):
                raise ValueError('trim has no current owned filesystem receipt')
            code = ('import json,stat,subprocess,sys\nfrom pathlib import Path\n' + inspect.getsource(allocated) +
                    '\n' + inspect.getsource(mounted_allocation) +
                    '\np=json.load(sys.stdin);print(mounted_allocation(p["path"],p["mount"],p["uuid"]))')
            value = self.lab.remote(allowed[node], ['python3', '-c', code],
                       stdin=json.dumps({'path': self.lab.DATA + '/replicas/' + directory, 'mount': self.lab.DATA, 'uuid': disk['uuid']}))
            result[name] = int(value.stdout.strip())
        print('LONGHORN_TRIM_ALLOCATION stage=' + stage + ' bytes=' + json.dumps(result, sort_keys=True), flush=True)
        return result

    def file(self, action, identity=None):
        code = ('import hashlib,json,os,stat,sys\nSIZE=' + str(SIZE) + '\n' + inspect.getsource(filler) +
                '\np=json.load(sys.stdin);print(json.dumps(filler("/data/trim-filler",p["action"],p["identity"]),sort_keys=True))')
        return json.loads(self.lab.kubectl('-n', self.journal['namespace'], 'exec', '-i', 'writer', '--',
                    'python3', '-c', code, stdin=json.dumps({'action': action, 'identity': identity}), timeout=180).stdout)

    def prepare(self, relay_pid):
        volume = self.lab.get('volumes.longhorn.io', self.journal['volume'], 'longhorn-system')
        original = volume['spec']['unmapMarkSnapChainRemoved']
        self.engine()
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': self.journal['volume_uid']},
                 {'op': 'test', 'path': '/spec/unmapMarkSnapChainRemoved', 'value': original},
                 {'op': 'replace', 'path': '/spec/unmapMarkSnapChainRemoved', 'value': 'disabled'}]
        self.lab.kubectl('-n', 'longhorn-system', 'patch', 'volumes.longhorn.io', self.journal['volume'],
                         '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        self.action('trimFilesystem')
        baseline = self.sample('baseline')
        identity = self.file('create')
        self.snapshot = 'trim-' + secrets.token_hex(12)
        request = {'kind': 'snapshot', 'name': self.snapshot, 'volume': self.journal['volume']}
        self.journal.setdefault('native_snapshots', {})[self.snapshot] = {'request': request}
        self.lab.write_private(self.lab.DIRECTORY / 'longhorn-install-business.json', self.journal)
        self.lab.run_requests(relay_pid, [request])
        self.lab.bind_snapshots(self.journal)
        written = self.sample('snapshot-retained')
        if any(written[name] - value < SIZE // 2 for name, value in baseline.items()):
            raise ValueError('trim filler did not allocate actual blocks on all replicas')
        removed = self.file('remove', identity)
        if removed['free_bytes'] - identity['free_bytes'] < SIZE // 2:
            raise ValueError('trim filler deletion did not release guest filesystem space')
        self.action('trimFilesystem')
        retained = self.sample('deleted-file-valid-snapshot')
        if any(retained[name] - value < SIZE // 2 for name, value in baseline.items()):
            raise ValueError('valid snapshot did not retain the filler allocation')
        snapshot = self.lab.get('snapshots.longhorn.io', self.snapshot, 'longhorn-system')
        if not snapshot or snapshot['metadata']['uid'] != self.journal['native_snapshots'][self.snapshot]['uid']:
            raise ValueError('trim snapshot was replaced before removal')
        self.lab.remove(snapshot)
        def marked():
            info = self.engine().get('status', {}).get('snapshots', {}).get(self.snapshot)
            return info is None or info.get('removed')
        self.lab.wait('trim-snapshot-marked-removed', 180, marked)
        self.action('snapshotPurge')
        self.lab.wait('trim-native-purge', 600, lambda: not any(
            row.get('isPurging') for row in self.engine().get('status', {}).get('purgeStatus', {}).values()))
        return {'baseline': baseline, 'retained': retained,
                'snapshot': {'name': self.snapshot, 'uid': snapshot['metadata']['uid']},
                'guest_freed_bytes': removed['free_bytes'] - identity['free_bytes'],
                'filler_sha256': identity['sha256'], 'replicas': self.topology}

    def finish(self, proof, payload, digest):
        # The normal business path has now remounted on another worker. This
        # forgets ext4's previously trimmed ranges that snapshots had retained.
        snapshot = proof.get('snapshot', {})
        original = self.journal.get('native_snapshots', {}).get(snapshot.get('name'), {})
        request = {'kind': 'snapshot', 'name': snapshot.get('name'), 'volume': self.journal['volume']}
        if (not snapshot.get('uid') or original.get('uid') != snapshot['uid'] or original.get('request') != request):
            raise ValueError('trim snapshot proof differs from its original request')
        native = self.lab.get('snapshots.longhorn.io', snapshot['name'], 'longhorn-system')
        if native and (native['metadata']['uid'] != snapshot['uid'] or not native['metadata'].get('deletionTimestamp')):
            raise ValueError('trim original snapshot was replaced or is not deleting')
        # A removed V1 volume-head parent can remain after native purge. Keep
        # its original identity across phases; never admit a valid/foreign one.
        self.snapshot = snapshot['name']
        self.removed_only = True
        self.action('trimFilesystem')
        def reclaimed():
            current = self.sample('after-purge-remount-trim')
            return current if all(proof['retained'][name] - value >= SIZE // 2
                                  for name, value in current.items()) else None
        after = self.lab.wait('trim-actual-block-reclaim', 600, reclaimed)
        actual = self.lab.kubectl('-n', self.journal['namespace'], 'exec', 'remount-reader', '--', 'python3', '-c',
                                 self.lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest:
            raise ValueError('trim changed original confirmed bytes')
        proof.update(after=after, filler_bytes=SIZE, confirmed_sha256=digest)
        print('LONGHORN_TRIM_RESULT ' + json.dumps(proof, sort_keys=True), flush=True)


def prepare(lab, journal, relay_pid):
    topology = lab.wait('trim-original-replicas', 600, lambda: lab.replica_topology(journal, 3))
    probe = Probe(lab, journal, topology)
    return probe.prepare(relay_pid)


def finish(lab, journal, proof, payload, digest):
    Probe(lab, journal, proof['replicas']).finish(proof, payload, digest)
