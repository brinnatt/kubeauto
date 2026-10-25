"""Owned-key refusal probes; never format, rekey or close a device directly."""
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess


NAMESPACE = 'longhorn-system'
KEY = 'longhorn-volume-key'
HEADER = 16 * 1024**2


def backup_receipt(lab, store, journal, backup, logical_size):
    """The backup stores the V1 LUKS header as well as logical payload bytes."""
    receipt = store.backup_receipt(journal['volume'], backup, logical_size + HEADER)
    prefix = lab.load_helper('longhorn-backups.py').volume_prefix(journal['volume'])
    config = json.loads(store.mc('cat', 'lh/' + store.receipt()['bucket'] + '/' + prefix + 'volume.cfg'))
    if (config.get('Name') != journal['volume'] or int(config['Size']) != logical_size + HEADER or
            config.get('Labels', {}).get('volume-encrypted') != 'enabled' or
            receipt['size'] != logical_size + HEADER):
        raise ValueError('encrypted backup metadata does not prove its LUKS capacity and label')
    receipt.update(size=logical_size, stored_size=logical_size + HEADER, encrypted=True)
    return receipt


def verify_restored(lab, journal, restored, client):
    volume = lab.get('volumes.longhorn.io', restored['volume'], NAMESPACE)
    pv = lab.get('pv', restored['pv'])
    expected = {'name': KEY, 'namespace': NAMESPACE}
    if (not volume or volume['metadata']['uid'] != restored['volume_uid'] or not volume['spec'].get('encrypted') or
            not pv or pv['metadata']['uid'] != restored['pv_uid'] or pv['spec']['csi'].get('nodeStageSecretRef') != expected):
        raise ValueError('encrypted backup restore lost its encryption or credential reference')
    probe = Probe(lab)
    result = probe.host_probe(lab.WORKERS[1], restored, True)
    if result != journal['encrypted_backup_device']:
        raise ValueError('encrypted backup restore did not preserve its LUKS header identity and capacity')
    print('LONGHORN_ENCRYPTED_BACKUP_DEVICE_VERIFIED ' + json.dumps({'client': client,
        'independent_volume': restored['volume'], **result}, sort_keys=True), flush=True)


def refusal_event(events, pod_uid, variant, *, block=False):
    needles = {'wrong': 'No key available with this passphrase',
               'missing': 'missing passphrase for encrypted volume'}
    needle = needles[variant]
    reason = 'FailedMapVolume' if block else 'FailedMount'
    return any(e.get('involvedObject', {}).get('uid') == pod_uid and
               e.get('reason') == reason and needle in e.get('message', '') for e in events)


def device_probe(volume, attached, *, data_engine='v1'):
    """Run read-only on a leased worker, outside the application container."""
    if not volume.startswith('pvc-') or Path(volume).name != volume:
        raise ValueError('invalid encrypted device name')
    if data_engine not in ('v1', 'v2'):
        raise ValueError('unknown encrypted volume data engine')
    # V2 already uses the unsuffixed mapper for its NVMe/TCP frontend.
    mapper = Path('/dev/mapper') / (volume + ('-encrypted' if data_engine == 'v2' else ''))
    if not attached:
        return {'closed': not mapper.exists()}
    raw = Path('/dev/longhorn') / volume
    if not all(stat.S_ISBLK(path.stat().st_mode) for path in (raw, mapper)):
        raise ValueError('encrypted volume does not have block devices')
    def command(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if command('blkid', '-s', 'TYPE', '-o', 'value', str(raw)) != 'crypto_LUKS':
        raise ValueError('raw volume is not LUKS')
    status = command('cryptsetup', 'status', str(mapper))
    devices = [line.split(':', 1)[1].strip() for line in status.splitlines()
               if line.strip().startswith('device:')]
    if len(devices) != 1:
        raise ValueError('crypto mapping does not refer to one raw device')
    backing = Path(devices[0]).stat()
    # Longhorn's mknod alias is a distinct inode, not necessarily a symlink
    # to cryptsetup's /dev/sdX. The block device number identifies the target.
    if not stat.S_ISBLK(backing.st_mode) or backing.st_rdev != raw.stat().st_rdev:
        raise ValueError('crypto mapping does not refer to the owned raw device')
    row = {'luks_uuid': command('cryptsetup', 'luksUUID', str(raw)),
           'raw_bytes': int(command('blockdev', '--getsize64', str(raw))),
           'mapped_bytes': int(command('blockdev', '--getsize64', str(mapper)))}
    if not row['luks_uuid'] or row['raw_bytes'] < row['mapped_bytes'] + 16 * 1024**2:
        raise ValueError('encrypted device header/capacity differs')
    return row


class Probe:
    def __init__(self, lab, *, data_engine='v1'):
        if data_engine not in ('v1', 'v2'):
            raise ValueError('unknown encryption probe data engine')
        self.lab = lab
        self.data_engine = data_engine
        self.record = lab.DIRECTORY / 'longhorn-encryption-key.json'

    def identity(self):
        state = self.lab.owner()
        namespace = self.lab.get('namespace', NAMESPACE)
        key = self.lab.get('secret', KEY, NAMESPACE)
        if (not namespace or namespace['metadata']['uid'] != state['namespace_uid'] or
                not key or key['metadata']['uid'] != state['encryption_uid']):
            raise ValueError('encryption namespace or key was replaced')
        return state, key

    def patch(self, key, desired):
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': key['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': key['metadata']['resourceVersion']},
                 {'op': 'test', 'path': '/data', 'value': key['data']},
                 {'op': 'replace', 'path': '/data', 'value': desired}]
        self.lab.kubectl('-n', NAMESPACE, 'patch', 'secret', KEY, '--type=json',
                         '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        if self.identity()[1].get('data') != desired:
            raise ValueError('encryption key update was not observed')

    def activate(self, journal, variant):
        if variant not in ('wrong', 'missing') or self.record.exists() or self.record.is_symlink():
            raise ValueError('invalid or unrecovered encryption refusal')
        state, key = self.identity()
        volumes = self.lab.get('volumes.longhorn.io', namespace=NAMESPACE)['items']
        if [(v['metadata']['name'], v['metadata']['uid']) for v in volumes] != [(journal['volume'], journal['volume_uid'])]:
            raise ValueError('encryption refusal rejects other volume consumers')
        volume = volumes[0]
        if (not volume['spec'].get('encrypted') or volume['spec'].get('dataEngine') != self.data_engine or
                volume['status'].get('state') != 'detached'):
            raise ValueError('key refusal requires its detached encrypted ' + self.data_engine + ' volume')
        if not self.closed(journal):
            raise ValueError('key refusal requires all previous mappings closed')
        original = key.get('data', {})
        if not base64.b64decode(original.get('CRYPTO_KEY_VALUE', ''), validate=True):
            raise ValueError('original encryption key is missing')
        desired = dict(original)
        if variant == 'wrong':
            desired['CRYPTO_KEY_VALUE'] = base64.b64encode(secrets.token_urlsafe(48).encode()).decode()
        else:
            desired.pop('CRYPTO_KEY_VALUE')
        self.lab.write_private(self.record, {'run_id': state['run_id'], 'namespace_uid': state['namespace_uid'],
            'key_uid': key['metadata']['uid'], 'variant': variant, 'original': original, 'desired': desired,
            'original_sha256': hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()})
        self.patch(key, desired)

    def restore(self):
        if self.record.is_symlink():
            raise ValueError('symlink encryption key escrow')
        if not self.record.exists():
            return
        info = self.record.stat()
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError('encryption key escrow is not private')
        row = json.loads(self.record.read_text()); state, key = self.identity()
        if (row.get('run_id') != state['run_id'] or row.get('namespace_uid') != state['namespace_uid'] or
                row.get('key_uid') != key['metadata']['uid'] or row.get('variant') not in ('wrong', 'missing') or
                hashlib.sha256(json.dumps(row['original'], sort_keys=True).encode()).hexdigest() != row['original_sha256'] or
                key.get('data') not in (row['original'], row['desired'])):
            raise ValueError('encryption escrow or current key changed outside its operation')
        if key['data'] != row['original']:
            self.patch(key, row['original'])
        self.record.unlink()
        print('LONGHORN_ENCRYPTION_ORIGINAL_KEY_RESTORED variant=' + row['variant'], flush=True)

    def host_probe(self, node, journal, attached):
        code = ('import json,stat,subprocess\nfrom pathlib import Path\n' + inspect.getsource(device_probe) +
                '\nprint(json.dumps(device_probe(%r,%r,data_engine=%r),sort_keys=True))\n' %
                (journal['volume'], attached, self.data_engine))
        return json.loads(self.lab.remote(node, ['python3', '-c', code], timeout=120).stdout)

    def closed(self, journal):
        return all(self.host_probe(node, journal, False).get('closed') is True for node in self.lab.WORKERS)

    def detach(self, journal, client):
        pod = self.lab.get('pod', client, journal['namespace'])
        if not pod or pod['metadata']['uid'] != journal['objects']['Pod/' + client]:
            raise ValueError('encryption client identity changed before detach')
        self.lab.remove(pod)
        self.lab.wait('encryption-client-gone-' + client, 180,
                      lambda: self.lab.get('pod', client, journal['namespace']) is None)
        def detached():
            volume = self.lab.get('volumes.longhorn.io', journal['volume'], NAMESPACE)
            if not volume or volume['metadata']['uid'] != journal['volume_uid']:
                raise ValueError('encrypted volume was replaced during detach')
            return volume['status'].get('state') == 'detached' and self.closed(journal)
        self.lab.wait('encryption-device-closed', 300, detached)

    def attached(self, journal, client):
        pod = self.lab.get('pod', client, journal['namespace'])
        if not pod or pod['metadata']['uid'] != journal['objects']['Pod/' + client]:
            raise ValueError('encryption client was replaced')
        return any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   for c in pod.get('status', {}).get('conditions', []))

    def verify(self, journal, create, payload, digest, *, block=False):
        node = self.lab.WORKERS[0]
        initial = self.host_probe(node, journal, True)
        current_client = 'writer'
        for variant in ('wrong', 'missing'):
            self.detach(journal, current_client)
            with self.lab.scoped_cleanup(self.restore):
                self.activate(journal, variant)
                bad_name = 'key-' + variant
                bad = create(self.lab.client_pod(journal['namespace'], bad_name, 'data',
                    'longhorn-node-' + node.rsplit('.', 1)[1], block=block))
                def refused():
                    if self.attached(journal, bad_name):
                        raise ValueError('invalid key unexpectedly mounted the encrypted volume')
                    events = self.lab.get('events', namespace=journal['namespace'])['items']
                    # Match the owning Pod UID and exact native failure layer;
                    # scheduling, image-pull and network errors do not qualify.
                    return refusal_event(events, bad['metadata']['uid'], variant, block=block)
                self.lab.wait('encryption-refused-' + variant, 360, refused)
                if not self.closed(journal):
                    raise ValueError('invalid key left an open encrypted mapping')
                self.detach(journal, bad_name)
            current_client = bad_name + '-recovered'
            create(self.lab.client_pod(journal['namespace'], current_client, 'data',
                'longhorn-node-' + node.rsplit('.', 1)[1], block=block))
            self.lab.wait('encryption-original-key-remount', 600, lambda: self.attached(journal, current_client))
            current = self.host_probe(node, journal, True)
            if current != initial:
                raise ValueError('original LUKS identity or mapped capacity changed after key restoration')
            actual = self.lab.kubectl('-n', journal['namespace'], 'exec', current_client, '--', 'python3', '-c',
                self.lab.payload_program(payload, block=block, read_only=True)).stdout.strip()
            if actual != digest or not self.lab.replica_topology(journal, 3):
                raise ValueError('restored key did not recover original data and healthy replicas')
            print('LONGHORN_ENCRYPTION_REFUSAL_VERIFIED ' + json.dumps({'variant': variant,
                'mode': 'Block' if block else 'Filesystem', 'original_device': initial, 'data_sha256': digest,
                'mapping_closed_before_refusal': True, 'key_restored': True}, sort_keys=True), flush=True)
        return current_client
