#!/usr/bin/env python3
"""Off-cluster TLS backup fixture, owned by the Longhorn runner on its jumper.

MinIO d0cada583fce88f60cb276ddfb06f5cb16820069, mc
e929f89ceeedc48a45611382be9882db0bf1921d; reuse published artifacts.
The bucket and host data belong to this run, never to an existing application.
"""
import json
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import NamedTuple


HOST = '192.168.47.130'
PORT = 23521
CONSOLE_PORT = 23522
TRANSPORT_NODE = '192.168.122.135'
TRANSPORT_PORT = 23523
TUNNEL_PORT = 23524
IMAGE = 'brinnatt/minio:RELEASE.2025-04-08T15-41-24Z'
TRANSPORT_IMAGE = 'brinnatt/redis-documentation-client:3.11.17-redis6.4.0-cli8.10.2'
TRANSPORT_IMAGE_REF = 'registry.talkschool.cn:5000/longhorn-lab/' + TRANSPORT_IMAGE
BACKUP_RESOURCES = ('backups.longhorn.io', 'backupvolumes.longhorn.io',
                    'backupbackingimages.longhorn.io', 'systembackups.longhorn.io')


class Endpoints(NamedTuple):
    node: str
    node_name: str
    api_port: int
    console_port: int
    transport_port: int
    tunnel_port: int
    refusal_port: int


V1_ENDPOINTS = Endpoints(TRANSPORT_NODE, 'longhorn-node-135', PORT, CONSOLE_PORT,
                         TRANSPORT_PORT, TUNNEL_PORT, 23525)
V2_ENDPOINTS = Endpoints('192.168.47.146', 'longhorn-node-146', 23621, 23622,
                         23623, 23624, 23625)


def tls_fixture(endpoints=V1_ENDPOINTS):
    """Short-lived backup-only chain valid under RFC 5280 strict verification.

    Python 3.13+ enables VERIFY_X509_STRICT by default. CA SKI/KeyUsage and
    leaf AKI are required; keep the separately qualified gRPC fixture intact.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Longhorn backup ' + secrets.token_hex(8))])
    ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256()))
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')]))
        .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
            *(x509.IPAddress(ipaddress.ip_address(ip)) for ip in ('127.0.0.1', HOST, endpoints.node))]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))
    return {'ca.crt': ca.public_bytes(serialization.Encoding.PEM).decode(),
            'tls.crt': cert.public_bytes(serialization.Encoding.PEM).decode(),
            'tls.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode()}


def transport_program(endpoints=V1_ENDPOINTS):
    """TLS passthrough only: no credentials, decoding or persistent host changes."""
    return f'''import select,socket,socketserver
class Relay(socketserver.BaseRequestHandler):
 def handle(self):
  with socket.create_connection(('127.0.0.1',{endpoints.tunnel_port}),timeout=5) as upstream:
   upstream.settimeout(30);self.request.settimeout(30)
   sockets=[self.request,upstream]
   while True:
    ready,_,_=select.select(sockets,[],[],30)
    if not ready: return
    for source in ready:
     data=source.recv(65536)
     if not data: return
     sockets[1-sockets.index(source)].sendall(data)
class Server(socketserver.ThreadingTCPServer):
 daemon_threads=True
 allow_reuse_address=True
with Server(({endpoints.node!r},{endpoints.transport_port}),Relay) as server:
 server.serve_forever()
'''


def volume_prefix(volume):
    if not re.fullmatch(r'[a-z0-9][-a-z0-9]{0,62}', volume):
        raise ValueError('invalid backup volume identity')
    # backupstore.util.GetChecksum uses truncated SHA512 despite its SHA256 comment.
    checksum = hashlib.sha512(volume.encode()).hexdigest()[:64]
    return 'backupstore/volumes/' + checksum[:2] + '/' + checksum[2:4] + '/' + volume + '/'


def target_configuration(spec):
    # The native poller updates syncRequestedAt; it is not fixture configuration.
    return {key: value for key, value in spec.items() if key != 'syncRequestedAt'}


def referenced_blocks(metadata, volume, backup, size):
    if (metadata.get('Name') != backup or metadata.get('VolumeName') != volume or
            not metadata.get('CreatedTime') or not metadata.get('Blocks')):
        raise ValueError('backup metadata identity or completion differs')
    # Manager 1.12.1 emits 2Mi or 16Mi; older backups omit Parameters.
    parameter = (metadata.get('Parameters') or {}).get('backup-block-size', '')
    sizes = {'': 2*1024**2, '0': 2*1024**2, '2Mi': 2*1024**2, '16Mi': 16*1024**2,
             '2097152': 2*1024**2, '16777216': 16*1024**2}
    if not isinstance(parameter, str) or parameter not in sizes:
        raise ValueError('unsupported backup metadata block size')
    block_size = sizes[parameter]
    if type(size) is not int or size <= 0 or size % block_size:
        raise ValueError('backup capacity is not aligned to its block size')
    offsets, checksums = set(), set()
    for block in metadata['Blocks']:
        offset, checksum = block.get('Offset'), block.get('BlockChecksum', '')
        if (type(offset) is not int or offset < 0 or offset >= size or offset % block_size or
                offset in offsets or not re.fullmatch('[a-f0-9]{64}', checksum)):
            raise ValueError('invalid backup block mapping')
        offsets.add(offset); checksums.add(checksum)
    return [volume_prefix(volume) + 'blocks/' + checksum[:2] + '/' + checksum[2:4] + '/' + checksum + '.blk'
            for checksum in sorted(checksums)]


def validate_container(obj, receipt, directory, endpoints=V1_ENDPOINTS):
    """Bind name, immutable image, run label, ports and every host mount."""
    expected_mounts = {str(directory / 'data'): '/data', str(directory / 'certs'): '/certs',
                       str(directory / 'cas'): '/tmp/longhorn-mc/certs/CAs'}
    mounts = obj.get('Mounts', [])
    if (obj.get('Name') != '/' + receipt['name'] or obj.get('Image') != receipt['image_id'] or
            obj.get('Config', {}).get('Labels', {}).get('kubeauto.io/longhorn-backup-run') != receipt['run_id'] or
            len(mounts) != len(expected_mounts) or
            {m.get('Source'): m.get('Destination') for m in mounts} != expected_mounts or
            any(m.get('Type') != 'bind' for m in mounts) or
            any(m.get('RW') != (m.get('Destination') == '/data') for m in mounts) or
            obj.get('HostConfig', {}).get('PortBindings') != {
                '9000/tcp': [{'HostIp': HOST, 'HostPort': str(endpoints.api_port)}],
                '9001/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(endpoints.console_port)}]} or
            receipt.get('container_id', obj.get('Id')) != obj.get('Id')):
        raise ValueError('backup container identity or mount/port boundary changed')
    return obj['Id']


class Refusals:
    """Temporary invalid credentials belong only to the off-cluster fixture."""
    def __init__(self, lab, store):
        self.lab = lab
        self.store = store
        self.record = lab.DIRECTORY / 'longhorn-backup-refusal.json'

    def identities(self):
        row = self.store.receipt(); binding = row['product_binding']
        target = self.lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
        namespace = self.lab.get('namespace', 'longhorn-system')
        secret = self.lab.get('secret', binding['secret_name'], 'longhorn-system')
        if (not target or not namespace or not secret or row['run_id'] != self.lab.owner()['run_id'] or
                namespace['metadata']['uid'] != binding['namespace_uid'] or
                target['metadata']['uid'] != binding['target_uid'] or
                target_configuration(target['spec']) not in
                (target_configuration(binding['original_spec']), target_configuration(binding['fixture_spec'])) or
                secret['metadata']['uid'] != binding['secret_uid'] or
                secret['metadata'].get('labels', {}).get('kubeauto.io/longhorn-backup-run') != row['run_id']):
            raise ValueError('backup refusal fixture identity or configuration changed')
        return row, binding, target, secret

    def activate(self, kind):
        if kind not in ('credential', 'ca', 'endpoint') or self.record.exists() or self.record.is_symlink():
            raise ValueError('invalid or unrecovered backup refusal')
        row, binding, target, secret = self.identities()
        original = secret['data']
        if (hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest() != binding['secret_digest'] or
                target_configuration(target['spec']) != target_configuration(binding['original_spec']) or
                target.get('status', {}).get('available') or
                any(self.lab.get(key, namespace='longhorn-system')['items'] for key in BACKUP_RESOURCES)):
            raise ValueError('backup refusal requires unused original target and valid fixture credential')
        desired = dict(original)
        if kind == 'credential':
            key, value = 'AWS_SECRET_ACCESS_KEY', secrets.token_hex(32)
        elif kind == 'ca':
            key, value = 'AWS_CERT', self.lab.tls_fixture()['ca.crt'].strip()
        else:
            # A closed port on the already qualified node, not a new host or
            # a firewall rule. Refuse an unexpected listener before changing input.
            endpoint = self.store.endpoints
            probe = ('import socket; s=socket.socket(); s.settimeout(3); '
                     f'r=s.connect_ex(({endpoint.node!r},{endpoint.refusal_port})); s.close(); assert r!=0')
            for node in self.lab.NODES:
                self.lab.remote(node, ['python3', '-c', probe])
            key, value = 'AWS_ENDPOINTS', f'https://{endpoint.node}:{endpoint.refusal_port}'
        desired[key] = base64.b64encode(value.encode()).decode()
        if desired == original:
            raise ValueError('backup refusal did not change the selected credential field')
        receipt = {'run_id': row['run_id'], 'store_name': row['name'], 'kind': kind,
                   'secret_uid': binding['secret_uid'], 'target_uid': binding['target_uid'],
                   'namespace_uid': binding['namespace_uid'], 'original': original, 'desired': desired}
        self.lab.write_private(self.record, receipt)
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': secret['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': secret['metadata']['resourceVersion']},
                 {'op': 'test', 'path': '/data', 'value': original},
                 {'op': 'replace', 'path': '/data', 'value': desired}]
        self.lab.kubectl('-n', 'longhorn-system', 'patch', 'secret', binding['secret_name'],
                         '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        if self.identities()[3].get('data') != desired:
            raise ValueError('backup refusal credential update was not observed')

    def restore(self):
        if self.record.is_symlink():
            raise ValueError('symlink backup refusal journal')
        if not self.record.exists():
            return
        receipt = json.loads(self.record.read_text())
        row, binding, _target, secret = self.identities()
        expected = {'run_id': row['run_id'], 'store_name': row['name'], 'secret_uid': binding['secret_uid'],
                    'target_uid': binding['target_uid'], 'namespace_uid': binding['namespace_uid']}
        if (any(receipt.get(key) != value for key, value in expected.items()) or
                receipt.get('kind') not in ('credential', 'ca', 'endpoint') or
                hashlib.sha256(json.dumps(receipt['original'], sort_keys=True).encode()).hexdigest() != binding['secret_digest'] or
                secret.get('data') not in (receipt['original'], receipt['desired'])):
            raise ValueError('backup refusal journal or credential changed outside its owning operation')
        if secret['data'] != receipt['original']:
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': binding['secret_uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': secret['metadata']['resourceVersion']},
                     {'op': 'test', 'path': '/data', 'value': receipt['desired']},
                     {'op': 'replace', 'path': '/data', 'value': receipt['original']}]
            self.lab.kubectl('-n', 'longhorn-system', 'patch', 'secret', binding['secret_name'],
                             '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        if self.identities()[3].get('data') != receipt['original']:
            raise ValueError('backup refusal credential restoration was not observed')
        self.record.unlink()
        print('LONGHORN_BACKUP_REFUSAL_CREDENTIAL_RESTORED kind=' + receipt['kind'], flush=True)

    def disconnect(self):
        """Reset the empty fixture target between cases; never remove backup CRs."""
        self.restore()
        for attempt in range(3):
            if any(self.lab.get(key, namespace='longhorn-system')['items'] for key in BACKUP_RESOURCES):
                raise ValueError('backup refusal cannot disconnect a nonempty or changed fixture')
            # The native controller updates status after credential restoration.
            # Read the patch preconditions after the slower consumer queries.
            _row, binding, target, secret = self.identities()
            if hashlib.sha256(json.dumps(secret['data'], sort_keys=True).encode()).hexdigest() != binding['secret_digest']:
                raise ValueError('backup refusal cannot disconnect a nonempty or changed fixture')
            if target_configuration(target['spec']) == target_configuration(binding['original_spec']):
                break
            original = dict(binding['original_spec'])
            if 'syncRequestedAt' in target['spec']:
                original['syncRequestedAt'] = target['spec']['syncRequestedAt']
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': binding['target_uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': target['metadata']['resourceVersion']},
                     {'op': 'replace', 'path': '/spec', 'value': original}]
            result = self.lab.kubectl('-n', 'longhorn-system', 'patch', 'backuptargets.longhorn.io', 'default',
                             '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch), check=False)
            if result.returncode == 0:
                break
            generic = 'The request is invalid: the server rejected our request due to an error in our request'
            if result.returncode != 1 or result.stderr.strip() != generic:
                raise ValueError('backup refusal target disconnect was rejected outside a JSON Patch precondition')
            current = self.identities()[2]
            if attempt == 2 or current['metadata']['resourceVersion'] == target['metadata']['resourceVersion']:
                raise ValueError('backup refusal target disconnect did not converge within its version retry boundary')
        def disconnected():
            _row, current_binding, current, _secret = self.identities()
            if target_configuration(current['spec']) != target_configuration(current_binding['original_spec']):
                raise ValueError('backup refusal target changed during disconnect')
            return not current.get('status', {}).get('available')
        self.lab.wait('backup-refusal-disconnected', 180, disconnected)

    def verify(self, journal, relay_pid, configuration):
        patterns = {'credential': r'signature|access.?denied|invalidaccesskey',
                    'ca': r'x509|certificate|tls', 'endpoint': r'refused|timeout|timed out|deadline|dial tcp'}
        for kind, pattern in patterns.items():
            snapshot_name = 'refused-snapshot-' + secrets.token_hex(12)
            backup_name = 'refused-backup-' + secrets.token_hex(12)
            snapshot = {'kind': 'snapshot', 'name': snapshot_name, 'volume': journal['volume']}
            backup = {'kind': 'backup', 'name': backup_name, 'snapshot': snapshot_name, 'mode': 'full'}
            journal.setdefault('native_snapshots', {})[snapshot_name] = {'request': snapshot}
            journal.setdefault('backups', {})[backup_name] = {'request': backup}
            self.lab.write_private(self.lab.DIRECTORY / 'longhorn-install-business.json', journal)
            with self.lab.scoped_cleanup(self.restore):
                self.activate(kind)
                self.lab.run_requests(relay_pid, [snapshot, backup], backup_configuration=configuration,
                    ready_timeout=60, expected_refusal='Longhorn timeout: backup-target-ready')
                _row, _binding, target, _secret = self.identities()
                conditions = target.get('status', {}).get('conditions', [])
                if (target.get('status', {}).get('available') is not False or
                        not any(c.get('type') == 'Unavailable' and c.get('status') == 'True' and
                                re.search(pattern, c.get('message', ''), re.I) for c in conditions) or
                        self.lab.get('snapshots.longhorn.io', snapshot_name, 'longhorn-system') or
                        self.lab.get('backups.longhorn.io', backup_name, 'longhorn-system') or
                        any(self.lab.get(key, namespace='longhorn-system')['items'] for key in BACKUP_RESOURCES)):
                    raise ValueError('backup refusal did not prove its owning failure and request absence')
                objects = [json.loads(line) for line in self.store.mc('ls', '--recursive', '--json',
                           'lh/' + self.store.receipt()['bucket']).splitlines() if line.strip()]
                if (not any(obj.get('key') == 'qualification-canary' for obj in objects) or
                        any(obj.get('status') != 'success' or obj.get('key', '').startswith('backupstore/') for obj in objects)):
                    raise ValueError('refused backup wrote objects or listing failed')
                print('LONGHORN_BACKUP_REFUSAL_VERIFIED kind=' + kind +
                      ' available=false request_created=false backup_objects=0', flush=True)
            self.disconnect()


class Store:
    def __init__(self, lab, *, endpoints=V1_ENDPOINTS):
        if endpoints not in (V1_ENDPOINTS, V2_ENDPOINTS):
            raise ValueError('unapproved backup fixture endpoints')
        if endpoints == V2_ENDPOINTS and tuple(lab.NODES) != (
                '192.168.47.146', '192.168.47.147', '192.168.47.148'):
            raise ValueError('V2 backup fixture requires its exact three-node pool')
        self.endpoints = endpoints
        self.lab = lab
        self.record = lab.DIRECTORY / 'longhorn-backup-store.json'
        self.directory = lab.DIRECTORY / 'longhorn-backup-store'
        self.endpoint = 'https://' + HOST + ':' + str(self.endpoints.api_port)
        self.product_endpoint = 'https://' + self.endpoints.node + ':' + str(self.endpoints.transport_port)

    def transport_listener(self):
        # Reuse the proven process/boot/start-tick probe without changing the
        # separately qualified Registry transport's script or fingerprint.
        script = self.lab.listener_script().replace('assert port==23520', 'assert port==' + str(self.endpoints.tunnel_port))
        return json.loads(self.lab.remote(self.endpoints.node, ['python3', '-c', script],
                         stdin=json.dumps({'port': self.endpoints.tunnel_port, 'action': 'probe'})).stdout)

    def transport_receipt(self, row):
        binding = row.get('transport')
        if not binding:
            return None
        expected_socket = str(self.directory / 'transport.sock')
        if (binding.get('node') != self.endpoints.node or binding.get('socket') != expected_socket or
                binding.get('pod_name') != row['name'] + '-transport'):
            raise ValueError('foreign backup transport journal')
        path = self.lab.DIRECTORY / 'longhorn-backup-store' / 'transport.sock'
        if path.is_symlink():
            raise ValueError('symlink backup transport socket')
        namespace = self.lab.get('namespace', 'longhorn-system')
        if not namespace or namespace['metadata']['uid'] != binding['namespace_uid']:
            raise ValueError('backup transport namespace replaced')
        pod = self.lab.get('pod', binding['pod_name'], 'longhorn-system')
        if pod and (pod['metadata']['uid'] != binding.get('pod_uid') or pod.get('spec') != binding.get('pod_spec')):
            raise ValueError('unconfirmed or replaced backup transport Pod')
        listener = self.transport_listener()
        if listener and listener != binding.get('listener'):
            raise ValueError('unconfirmed or replaced backup transport listener')
        if path.exists():
            info = path.stat()
            if binding.get('socket_identity') != [info.st_dev, info.st_ino]:
                raise ValueError('backup transport socket identity changed')
        return binding

    def prepare_transport(self):
        row = self.receipt()
        if row.get('transport'):
            raise ValueError('previous backup transport needs recovery')
        namespace = self.lab.get('namespace', 'longhorn-system')
        pod_name = row['name'] + '-transport'; path = self.directory / 'transport.sock'
        if not namespace or self.lab.get('pod', pod_name, 'longhorn-system') or path.exists() or path.is_symlink():
            raise ValueError('foreign backup transport prerequisite')
        self.lab.remote(self.endpoints.node, ['python3', '-c',
            f'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(({self.endpoints.node!r},{self.endpoints.transport_port})); s.close()'])
        if self.transport_listener():
            raise ValueError('foreign backup tunnel listener')
        binding = {'node': self.endpoints.node, 'socket': str(path), 'pod_name': pod_name,
                   'namespace_uid': namespace['metadata']['uid']}
        row['transport'] = binding; self.lab.write_private(self.record, row)
        log = self.directory / 'transport.log'
        with log.open('x') as stream:
            os.chmod(stream.name, 0o600)
        started = time.time()
        self.lab.execute(self.lab.SSH + ['-E', str(log), '-M', '-S', str(path), '-o', 'ExitOnForwardFailure=yes',
            '-f', '-N', '-R', '127.0.0.1:' + str(self.endpoints.tunnel_port) + ':' + HOST + ':' + str(self.endpoints.api_port), 'root@' + self.endpoints.node], timeout=40)
        listener = self.transport_listener()
        if (not listener or listener['binding'] != '127.0.0.1:' + str(self.endpoints.tunnel_port) or listener['uid'] != 0 or
                listener['command'] not in ('sshd-session: root', 'sshd: root') or
                not started - 5 <= listener['start_time'] <= time.time() + 5):
            raise ValueError('backup tunnel listener outside this creation window')
        info = path.stat(); binding.update(listener=listener, socket_identity=[info.st_dev, info.st_ino])
        self.lab.write_private(self.record, row)
        self.lab.cache_artifacts(keys=[TRANSPORT_IMAGE])
        self.stage_transport_image()
        pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': pod_name, 'namespace': 'longhorn-system',
               'labels': {'kubeauto.io/longhorn-backup-run': row['run_id']}}, 'spec': {
               'nodeName': self.endpoints.node_name, 'hostNetwork': True, 'dnsPolicy': 'Default',
               'automountServiceAccountToken': False, 'restartPolicy': 'Never',
               'tolerations': [{'operator': 'Exists'}], 'containers': [{'name': 'transport',
               'image': TRANSPORT_IMAGE_REF,
               'command': ['python3', '-u', '-c', transport_program(self.endpoints)],
               'securityContext': {'runAsUser': 1000, 'runAsNonRoot': True, 'allowPrivilegeEscalation': False,
                                   'capabilities': {'drop': ['ALL']}},
               'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'},
                             'limits': {'cpu': '250m', 'memory': '128Mi'}},
               'readinessProbe': {'tcpSocket': {'host': self.endpoints.node, 'port': self.endpoints.transport_port},
                                  'periodSeconds': 2, 'timeoutSeconds': 2}}]}}
        result = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(pod)).stdout)
        binding.update(pod_uid=result['metadata']['uid'], pod_spec=result['spec'])
        self.lab.write_private(self.record, row)
        def ready():
            self.transport_receipt(self.receipt())
            current = self.lab.get('pod', pod_name, 'longhorn-system')
            return current and any(c['type'] == 'Ready' and c['status'] == 'True'
                                   for c in current.get('status', {}).get('conditions', []))
        self.lab.wait('backup-tls-passthrough-ready', 180, ready)
        print('LONGHORN_BACKUP_TRANSPORT_READY tls=passthrough data=off-cluster', flush=True)

    @staticmethod
    def _node_image_digest(output):
        target = re.search(r'(?m)^\s*Target:\s*(sha256:[0-9a-f]{64})\s*$', output)
        if target:
            return target.group(1)
        reference = re.search(r'@(sha256:[0-9a-f]{64})', output)
        return reference.group(1) if reference else None

    def stage_transport_image(self):
        """Admit the reviewed immutable transport image on its actual node."""
        _, support = self.lab.publication_records()
        row = next((item for item in support if item.get('key') == TRANSPORT_IMAGE), None)
        expected = row.get('platforms', {}).get('linux/amd64') if row else None
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected or ''):
            raise ValueError('transport image lacks the reviewed linux/amd64 digest')
        inspect = self.lab.remote(self.endpoints.node, ['ctr', '-n', 'k8s.io', 'images', 'inspect',
                                                         TRANSPORT_IMAGE_REF], check=False, timeout=60)
        if inspect.returncode == 0:
            actual = self._node_image_digest(inspect.stdout)
            if actual != expected:
                raise ValueError('transport node image digest mismatch: expected ' + expected + ' got ' + str(actual))
        else:
            pull = self.lab.remote(self.endpoints.node,
                ['ctr', '-n', 'k8s.io', 'images', 'pull', '--hosts-dir', '/etc/containerd/certs.d',
                 '--platform', 'linux/amd64', TRANSPORT_IMAGE_REF],
                check=False, timeout=900)
            if pull.returncode:
                raise ValueError('transport node image pull failed rc=' + str(pull.returncode))
            inspect = self.lab.remote(self.endpoints.node, ['ctr', '-n', 'k8s.io', 'images', 'inspect',
                                                             TRANSPORT_IMAGE_REF], check=False, timeout=60)
            actual = self._node_image_digest(inspect.stdout) if inspect.returncode == 0 else None
            if actual != expected:
                raise ValueError('transport node image digest mismatch after pull: expected ' + expected +
                                 ' got ' + str(actual))
        print('LONGHORN_TRANSPORT_IMAGE_READY node=' + self.endpoints.node + ' digest=' + expected, flush=True)

    def cleanup_transport(self):
        row = self.receipt(); binding = self.transport_receipt(row)
        if not binding:
            return
        pod = self.lab.get('pod', binding['pod_name'], 'longhorn-system')
        if pod:
            self.lab.remove(pod)
        def retired():
            self.transport_receipt(self.receipt())
            return self.lab.get('pod', binding['pod_name'], 'longhorn-system') is None
        self.lab.wait('backup-transport-pod-clean', 180, retired)
        self.lab.remote(self.endpoints.node, ['python3', '-c',
            f'import subprocess; assert not subprocess.check_output(["ss","-H","-lnt","sport = :{self.endpoints.transport_port}"]).strip()'])
        path = self.directory / 'transport.sock'
        if path.exists():
            self.lab.execute(self.lab.SSH + ['-S', str(path), '-O', 'exit', 'root@' + self.endpoints.node])
        # A broken SSH connection can retire the local master socket while its
        # server-side forwarding session remains. Reuse the Registry fixture's
        # boot/PID/start-tick/UID/socket guard; never kill a replacement listener.
        listener = self.transport_listener()
        if listener:
            if listener != binding.get('listener'):
                raise ValueError('replacement backup listener refuses cleanup')
            script = self.lab.listener_script().replace('assert port==23520', 'assert port==' + str(self.endpoints.tunnel_port))
            self.lab.remote(self.endpoints.node, ['python3', '-c', script],
                            stdin=json.dumps({'port': self.endpoints.tunnel_port, 'action': 'stop', 'expected': listener}))
        self.lab.wait('backup-tunnel-listener-clean', 30, lambda: self.transport_listener() is None and not path.exists())
        del row['transport']; self.lab.write_private(self.record, row)
        print('LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS', flush=True)

    def recover_transport(self):
        binding = self.transport_receipt(self.receipt())
        if not binding:
            return
        live = self.lab.execute(self.lab.SSH + ['-S', binding['socket'], '-O', 'check',
                                'root@' + binding['node']], check=False)
        if live.returncode == 0:
            return
        log = self.directory / 'transport.log'
        if log.is_symlink() or not log.is_file():
            raise ValueError('backup transport recovery requires its original log')
        # Reuse the exact Pod/socket/listener ownership checks and preserve the
        # first disconnect log before recreating this run's transport only.
        self.cleanup_transport()
        log.rename(self.directory / ('transport-interrupted-' + secrets.token_hex(16) + '.log'))
        self.prepare_transport()
        print('LONGHORN_BACKUP_TRANSPORT_RECOVERED', flush=True)

    def receipt(self):
        if self.record.is_symlink() or self.directory.is_symlink():
            raise ValueError('symlink backup fixture')
        row = json.loads(self.record.read_text())
        state = self.lab.owner()
        expected_name = 'longhorn-backup-' + state['run_id'].replace('-', '')[:16]
        if (row.get('endpoints', V1_ENDPOINTS._asdict()) != self.endpoints._asdict() or
                row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
                row['name'] != expected_name or row['bucket'] != expected_name):
            raise ValueError('backup fixture belongs to another run or cluster')
        if self.directory.exists():
            info = self.directory.stat()
            if row.get('directory_identity') != [info.st_dev, info.st_ino]:
                raise ValueError('backup fixture directory identity changed')
        elif row.get('directory_identity'):
            raise ValueError('backup fixture directory disappeared')
        return row

    def inspect(self, row):
        found = self.lab.execute(['docker', 'container', 'ls', '-a', '--filter',
                                 'name=^/' + row['name'] + '$', '--format', '{{.ID}}']).stdout.split()
        if not found:
            return None
        if len(found) != 1:
            raise ValueError('ambiguous backup container')
        obj = json.loads(self.lab.execute(['docker', 'inspect', '--type=container', found[0]]).stdout)[0]
        validate_container(obj, row, self.directory, self.endpoints)
        return obj

    def mc(self, *args, timeout=120):
        row = self.receipt(); obj = self.inspect(row)
        if not obj or not obj.get('State', {}).get('Running'):
            raise ValueError('backup store container is not running')
        return self.lab.execute(['docker', 'exec', '--env-file', str(self.directory / 'mc.env'),
                                 obj['Id'], 'mc', *args], timeout=timeout).stdout

    def prepare(self):
        self.lab.core_identity()
        if self.record.exists() or self.record.is_symlink() or self.directory.exists() or self.directory.is_symlink():
            raise ValueError('backup fixture already exists; use its owning recovery')
        state = self.lab.owner()
        _, support = self.lab.publication_records()
        image = next(row for row in support if row['key'] == IMAGE)
        reference = '127.0.0.1:5000/longhorn-lab/' + IMAGE
        self.lab.cache_artifacts(keys=[IMAGE])
        self.lab.execute(['docker', 'pull', reference], timeout=300)
        image_id = self.lab.published_manifest(image, image['platforms']['linux/amd64'])['config']['digest']
        actual = json.loads(self.lab.execute(['docker', 'image', 'inspect', reference]).stdout)[0]
        if actual.get('Id') != image_id:
            raise ValueError('backup runtime image differs from the published config')
        name = 'longhorn-backup-' + state['run_id'].replace('-', '')[:16]
        if self.lab.execute(['docker', 'container', 'ls', '-a', '--filter', 'name=^/' + name + '$',
                             '--format', '{{.ID}}']).stdout.strip():
            raise ValueError('foreign backup fixture container name')
        for host, port in ((HOST, self.endpoints.api_port), ('127.0.0.1', self.endpoints.console_port)):
            with socket.socket() as probe:
                probe.bind((host, port))
        row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'], 'name': name,
               'image_id': image_id, 'bucket': name, 'directory_identity': None,
               'endpoints': self.endpoints._asdict()}
        self.lab.write_private(self.record, row)
        self.directory.mkdir(mode=0o700)
        info = self.directory.stat(); row['directory_identity'] = [info.st_dev, info.st_ino]
        self.lab.write_private(self.record, row)
        for name in ('data', 'certs', 'cas'):
            (self.directory / name).mkdir(mode=0o700)
        certs = tls_fixture(self.endpoints)
        user, password = secrets.token_hex(16), secrets.token_hex(32)
        files = {'certs/public.crt': certs['tls.crt'], 'certs/private.key': certs['tls.key'],
                 'cas/ca.crt': certs['ca.crt'],
                 'server.env': 'MINIO_ROOT_USER=' + user + '\nMINIO_ROOT_PASSWORD=' + password + '\n',
                 'mc.env': 'MC_CONFIG_DIR=/tmp/longhorn-mc\nMC_HOST_lh=https://' + user + ':' + password + '@127.0.0.1:9000\n'}
        for filename, text in files.items():
            with (self.directory / filename).open('x') as stream:
                os.chmod(stream.name, 0o600); stream.write(text)
        argv = ['docker', 'run', '--detach', '--pull=never', '--name', row['name'],
                '--label', 'kubeauto.io/longhorn-backup-run=' + row['run_id'], '--restart=no',
                '--memory=1g', '--cpus=1', '--env-file', str(self.directory / 'server.env')]
        for source, target, mode in (('data', '/data', 'Z'), ('certs', '/certs', 'ro,Z'),
                                     ('cas', '/tmp/longhorn-mc/certs/CAs', 'ro,Z')):
            argv += ['--volume', str(self.directory / source) + ':' + target + ':' + mode]
        argv += ['--publish', HOST + ':' + str(self.endpoints.api_port) + ':9000', '--publish',
                 '127.0.0.1:' + str(self.endpoints.console_port) + ':9001', reference, 'server', '/data',
                 '--address', ':9000', '--console-address', ':9001', '--certs-dir', '/certs']
        row['container_id'] = self.lab.execute(argv, timeout=120).stdout.strip()
        self.lab.write_private(self.record, row)
        self.lab.wait('external-backup-tls-ready', 90, self.live)
        self.mc('mb', 'lh/' + row['bucket'])
        print('LONGHORN_EXTERNAL_BACKUP_STORE_READY tls=verified host=' + HOST, flush=True)

    def live(self):
        try:
            context = ssl.create_default_context(cafile=str(self.directory / 'cas/ca.crt'))
            with urllib.request.urlopen(self.endpoint + '/minio/health/live', context=context, timeout=3) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def qualify(self):
        row = self.receipt(); obj = self.inspect(row)
        # Use fixed literal stdin for the fixture object, never credentials in argv.
        self.lab.execute(['docker', 'exec', '-i', obj['Id'], 'sh', '-c', 'cat > /tmp/longhorn-canary'], stdin=row['run_id'])
        key = 'lh/' + row['bucket'] + '/qualification-canary'
        self.mc('cp', '/tmp/longhorn-canary', key)
        if self.mc('cat', key) != row['run_id']:
            raise ValueError('TLS object store changed confirmed fixture bytes')
        self.prepare_transport()
        ca = (self.directory / 'cas/ca.crt').read_text()
        script = "import json,ssl,sys,urllib.request;c=json.load(sys.stdin);ctx=ssl.create_default_context(cadata=c['ca']);op=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx));r=op.open(c['url'],timeout=5);assert r.status==200"
        for node in self.lab.NODES:
            self.lab.remote(node, ['python3', '-c', script], stdin=json.dumps({'ca': ca, 'url': self.product_endpoint + '/minio/health/live'}))
        print(f'LONGHORN_BACKUP_STORE_QUALIFIED clients={len(self.lab.NODES)} tls=verified object_readback=exact', flush=True)

    def object_digest(self, key):
        row = self.receipt(); obj = self.inspect(row)
        if (not key.startswith('backupstore/volumes/') or any(part in ('', '.', '..') for part in key.split('/'))):
            raise ValueError('foreign backup object path')
        temporary = '/tmp/longhorn-backup-object-' + secrets.token_hex(16)
        with self.lab.scoped_cleanup(lambda: self.lab.execute(['docker', 'exec', obj['Id'], 'rm', '-f', '--', temporary])):
            self.mc('cp', 'lh/' + row['bucket'] + '/' + key, temporary)
            size = int(self.lab.execute(['docker', 'exec', obj['Id'], 'stat', '--format=%s', temporary]).stdout)
            digest = self.lab.execute(['docker', 'exec', obj['Id'], 'sha256sum', temporary]).stdout.split()[0]
            if size <= 0 or not re.fullmatch('[a-f0-9]{64}', digest):
                raise ValueError('backup object is empty or unreadable')
            return {'size': size, 'sha256': digest}

    def backup_origin(self, volume, backup, snapshot, recurring_job):
        """Read native provenance after recurring cleanup has removed a snapshot.

        This is an identity receipt only. backup_receipt still reads every
        referenced block separately before accepting data protection.
        """
        if not re.fullmatch(r'[a-z0-9][-a-z0-9]{0,62}', backup):
            raise ValueError('invalid backup identity')
        row = self.receipt()
        key = volume_prefix(volume) + 'backups/backup_' + backup + '.cfg'
        raw = self.mc('cat', 'lh/' + row['bucket'] + '/' + key)
        metadata = json.loads(raw)
        if (metadata.get('Name') != backup or metadata.get('VolumeName') != volume or
                metadata.get('SnapshotName') != snapshot or
                (metadata.get('Labels') or {}).get('RecurringJob') != recurring_job or
                not metadata.get('CreatedTime') or not metadata.get('Blocks')):
            raise ValueError('recurring backup backend provenance differs')
        return hashlib.sha256(raw.encode()).hexdigest()

    def backup_receipt(self, volume, backup, size):
        if not re.fullmatch(r'[a-z0-9][-a-z0-9]{0,62}', backup):
            raise ValueError('invalid backup identity')
        row = self.receipt(); prefix = volume_prefix(volume)
        config_key = prefix + 'backups/backup_' + backup + '.cfg'
        raw = self.mc('cat', 'lh/' + row['bucket'] + '/' + config_key)
        metadata = json.loads(raw)
        volume_config = json.loads(self.mc('cat', 'lh/' + row['bucket'] + '/' + prefix + 'volume.cfg'))
        if volume_config.get('Name') != volume or int(volume_config['Size']) != size:
            raise ValueError('backup volume configuration identity or capacity differs')
        keys = [config_key, *referenced_blocks(metadata, volume, backup, size)]
        # Read each referenced object; an aggregate prefix listing cannot prove this.
        objects = {key: self.object_digest(key) for key in keys}
        if objects[config_key] != {'size': len(raw.encode()), 'sha256': hashlib.sha256(raw.encode()).hexdigest()}:
            raise ValueError('backup metadata changed during object verification')
        return {'volume': volume, 'backup': backup, 'size': size, 'incremental': metadata.get('IsIncremental'),
                'newly_uploaded_bytes': int(metadata.get('NewlyUploadedDataSize', '0')),
                'reuploaded_bytes': int(metadata.get('ReUploadedDataSize', '0')), 'objects': objects}

    def product_configuration(self):
        """Declare an empty target's temporary fixture; kubecli performs activation."""
        row = self.receipt()
        if 'product_binding' in row:
            raise ValueError('previous backup product binding needs recovery')
        target = self.lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
        namespace = self.lab.get('namespace', 'longhorn-system')
        if (not target or not namespace or target['spec'].get('backupTargetURL') or
                target['spec'].get('credentialSecret') or
                any(self.lab.get(kind, namespace='longhorn-system')['items'] for kind in BACKUP_RESOURCES)):
            raise ValueError('backup fixture requires an unused empty product target')
        if self.lab.get('secret', row['name'], 'longhorn-system'):
            raise ValueError('foreign backup fixture credential name')
        url = 's3://' + row['bucket'] + '@us-east-1/'
        binding = {'target_uid': target['metadata']['uid'], 'namespace_uid': namespace['metadata']['uid'],
                   'original_spec': target['spec'], 'fixture_spec': dict(target['spec'],
                   backupTargetURL=url, credentialSecret=row['name'], pollInterval='5m0s'),
                   'secret_name': row['name']}
        row['product_binding'] = binding
        self.lab.write_private(self.record, row)
        environment = dict(line.split('=', 1) for line in (self.directory/'server.env').read_text().splitlines())
        values = {'AWS_ACCESS_KEY_ID': environment['MINIO_ROOT_USER'],
                  'AWS_SECRET_ACCESS_KEY': environment['MINIO_ROOT_PASSWORD'],
                  'AWS_ENDPOINTS': self.product_endpoint, 'AWS_CERT': (self.directory/'cas/ca.crt').read_text().strip(),
                  'VIRTUAL_HOSTED_STYLE': 'false'}
        data = {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}
        obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': row['name'], 'namespace': 'longhorn-system',
               'labels': {'kubeauto.io/longhorn-backup-run': row['run_id']}}, 'type': 'Opaque', 'data': data}
        result = json.loads(self.lab.kubectl('create', '-f', '-', '-o', 'json', stdin=json.dumps(obj)).stdout)
        binding.update(secret_uid=result['metadata']['uid'],
                       secret_digest=hashlib.sha256(json.dumps(result['data'], sort_keys=True).encode()).hexdigest())
        self.lab.write_private(self.record, row)
        return {'longhorn_backup_target': url, 'longhorn_backup_secret': row['name']}

    def restore_product(self):
        """Undo only this fixture's declared target/Secret, before host cleanup."""
        row = self.receipt(); binding = row.get('product_binding')
        if not binding:
            return
        target = self.lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
        namespace = self.lab.get('namespace', 'longhorn-system')
        secret = self.lab.get('secret', binding['secret_name'], 'longhorn-system')
        if (not target or not namespace or target['metadata']['uid'] != binding['target_uid'] or
                namespace['metadata']['uid'] != binding['namespace_uid'] or
                target_configuration(target['spec']) not in
                (target_configuration(binding['original_spec']), target_configuration(binding['fixture_spec']))):
            raise ValueError('backup target identity or configuration changed before recovery')
        if secret and (secret['metadata']['uid'] != binding.get('secret_uid') or
                       hashlib.sha256(json.dumps(secret.get('data', {}), sort_keys=True).encode()).hexdigest() != binding.get('secret_digest')):
            raise ValueError('backup fixture credential identity or data changed')
        if any(self.lab.get(kind, namespace='longhorn-system')['items'] for kind in BACKUP_RESOURCES):
            raise ValueError('backup resources must finish scoped recovery before target disconnect')
        if target_configuration(target['spec']) != target_configuration(binding['original_spec']):
            original = dict(binding['original_spec'])
            if 'syncRequestedAt' in target['spec']:
                original['syncRequestedAt'] = target['spec']['syncRequestedAt']
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': binding['target_uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': target['metadata']['resourceVersion']},
                     {'op': 'replace', 'path': '/spec', 'value': original}]
            self.lab.kubectl('-n', 'longhorn-system', 'patch', 'backuptargets.longhorn.io', 'default',
                             '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        def disconnected():
            current = self.lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
            if (not current or current['metadata']['uid'] != binding['target_uid'] or
                    target_configuration(current['spec']) != target_configuration(binding['original_spec'])):
                raise ValueError('backup target replaced during disconnect')
            return not current.get('status', {}).get('available')
        self.lab.wait('backup-fixture-target-disconnected', 180, disconnected)
        if secret:
            self.lab.remove(secret)
        def retired():
            current = self.lab.get('secret', binding['secret_name'], 'longhorn-system')
            if current and current['metadata']['uid'] != binding['secret_uid']:
                raise ValueError('backup fixture credential replaced during cleanup')
            return current is None
        self.lab.wait('backup-fixture-credential-clean', 90, retired)
        del row['product_binding']; self.lab.write_private(self.record, row)
        print('LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED', flush=True)

    def cleanup(self, *, after_uninstall=False):
        if type(after_uninstall) is not bool:
            raise ValueError('invalid native-uninstall cleanup intent')
        credentials = self.record.parent / 'longhorn-recurring-credentials.json'
        if credentials.exists() or credentials.is_symlink():
            raise ValueError('recurring credential references must recover before store teardown')
        if not self.record.exists():
            if self.directory.exists() or self.record.is_symlink() or self.directory.is_symlink():
                raise ValueError('unowned backup fixture path')
            return
        if after_uninstall:
            row = self.receipt()
            if (row.get('product_binding') or row.get('transport') or
                    self.lab.owner().get('native_uninstalled') is not True or
                    self.lab.get('namespace', 'longhorn-system') or
                    any(o['metadata']['name'].endswith('.longhorn.io') for o in self.lab.get('crds')['items'])):
                raise ValueError('external store cleanup lacks proven native uninstall or retains consumers')
            self.lab.core_identity()
            target = None
        else:
            self.restore_product()
            row = self.receipt()
            target = self.lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
        if target and target.get('spec', {}).get('backupTargetURL', '').startswith('s3://' + row['bucket'] + '@'):
            raise ValueError('backup fixture is still consumed by the product')
        accounts = self.directory / 'accounts.json'
        if accounts.exists() or accounts.is_symlink():
            self.lab.load_helper('longhorn-accounts.py').Accounts(self.lab, self).cleanup()
        self.cleanup_transport()
        obj = self.inspect(row)
        if obj:
            self.lab.execute(['docker', 'rm', '--force', obj['Id']], timeout=60)
        if self.inspect(row):
            raise ValueError('backup fixture container remains')
        self.receipt()
        if self.directory.exists():
            if any(p.is_symlink() for p in self.directory.rglob('*')):
                raise ValueError('symlink in backup fixture directory')
            shutil.rmtree(self.directory)
        self.record.unlink()
        print('LONGHORN_BACKUP_STORE_CLEAN_PASS', flush=True)
