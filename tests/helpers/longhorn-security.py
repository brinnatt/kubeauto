"""Native TLS observations for the pinned Instance Manager gRPC endpoint.

Credentials enter on stdin and leave neither files nor diagnostic text behind.
An HTTP/2 SETTINGS frame proves that client authentication completed; a TCP
connection or the TLS 1.3 client-side handshake alone cannot prove that.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import tempfile


CLIENT_ALERTS = {
    'missing': {'TLSV13_ALERT_CERTIFICATE_REQUIRED'},
    'untrusted': {'TLSV1_ALERT_UNKNOWN_CA', 'SSLV3_ALERT_BAD_CERTIFICATE'},
    'wrong-name': {'SSLV3_ALERT_BAD_CERTIFICATE'},
}
NAMESPACE = 'longhorn-system'
SECRET = 'longhorn-grpc-tls'


def tls_exchange(configuration):
    """Read a real gRPC server's first HTTP/2 frame after mutual TLS."""
    address = configuration['address']; ipaddress.ip_address(address)
    port = configuration.get('port', 8500)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid gRPC probe port')
    with tempfile.TemporaryDirectory(prefix='longhorn-tls-') as directory:
        context = ssl.create_default_context(cadata=configuration['ca'])
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols(['h2'])
        if configuration.get('certificate'):
            for name, key in (('client.crt', 'certificate'), ('client.key', 'key')):
                target = Path(directory) / name
                with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                    stream.write(configuration[key])
            context.load_cert_chain(str(Path(directory)/'client.crt'), str(Path(directory)/'client.key'))
        try:
            with socket.create_connection((address, port), timeout=5) as connection:
                with context.wrap_socket(connection, server_hostname='longhorn-backend') as secured:
                    identity = hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest()
                    # The pinned gRPC server flushes its SETTINGS before
                    # reading the client preface. Read first so a TLS 1.3
                    # client-auth alert cannot be hidden by an early write.
                    header = b''
                    while len(header) < 9:
                        part = secured.recv(9 - len(header))
                        if not part:
                            return {'state': 'closed'}
                        header += part
                    length = int.from_bytes(header[:3], 'big')
                    if (header[3] != 4 or header[4] != 0 or header[5:] != bytes(4) or
                            length > 16384 or length % 6 or secured.selected_alpn_protocol() != 'h2'):
                        return {'state': 'invalid-http2'}
                    payload = b''
                    while len(payload) < length:
                        part = secured.recv(length - len(payload))
                        if not part:
                            return {'state': 'closed'}
                        payload += part
                    secured.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + b'\x00\x00\x00\x04\x00\x00\x00\x00\x00')
                    return {'state': 'accepted', 'server_sha256': identity, 'tls': secured.version(), 'alpn': 'h2'}
        except ssl.SSLError as error:
            return {'state': 'tls-error', 'reason': error.reason}
        except TimeoutError:
            return {'state': 'timeout'}
        except OSError as error:
            return {'state': 'network-error', 'type': type(error).__name__}


def accepted(observation, certificate):
    identity = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
    return observation == {'state': 'accepted', 'server_sha256': identity, 'tls': 'TLSv1.3', 'alpn': 'h2'}


def refused(observation, variant):
    return (variant in CLIENT_ALERTS and observation.get('state') == 'tls-error' and
            observation.get('reason') in CLIENT_ALERTS[variant])


def plaintext_exchange(address):
    ipaddress.ip_address(address)
    try:
        with socket.create_connection((address, 8500), timeout=5) as connection:
            connection.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
            return {'state': 'closed' if connection.recv(24) == b'' else 'unexpected-response'}
    except ConnectionResetError:
        return {'state': 'closed'}
    except TimeoutError:
        return {'state': 'timeout'}
    except OSError as error:
        return {'state': 'network-error', 'type': type(error).__name__}


def probe_program():
    return ('import hashlib,ipaddress,json,os,socket,ssl,sys,tempfile\nfrom pathlib import Path\n' +
            inspect.getsource(tls_exchange) +
            '\nprint(json.dumps(tls_exchange(json.load(sys.stdin)),sort_keys=True))\n')


def replacement_material():
    """Create a short-lived CA plus authorized and wrong-name client leaves."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Longhorn rotation ' + secrets.token_hex(8))])
    ca = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
          .not_valid_after(now+timedelta(days=7)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .sign(ca_key, hashes.SHA256()))
    def leaf(peer):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        dns = [peer]
        if peer == 'longhorn-backend':
            dns += [service+suffix for service in ('longhorn-backend', 'longhorn-frontend', 'longhorn-engine-manager',
                    'longhorn-replica-manager', 'longhorn-csi') for suffix in
                    ('.longhorn-system', '.longhorn-system.svc', '.longhorn-system.svc.cluster.local')]
            dns += ['longhorn-frontend', 'longhorn-engine-manager', 'longhorn-replica-manager', 'longhorn-csi']
        certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, peer)]))
            .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in dns]+
                [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
        return {'ca.crt': ca.public_bytes(serialization.Encoding.PEM).decode(),
                'tls.crt': certificate.public_bytes(serialization.Encoding.PEM).decode(),
                'tls.key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode()}
    return leaf('longhorn-backend'), leaf('unauthorized-longhorn-peer')


def encode(material):
    return {key: base64.b64encode(value.encode()).decode() for key, value in material.items()}


def decode(data):
    return {key: base64.b64decode(data[key], validate=True).decode() for key in ('ca.crt', 'tls.crt', 'tls.key')}


class Rotation:
    """A private, UID-bound escrow; every stage retains a reversible trust path.

    This class never restarts components with attached volumes. Its caller owns
    an independent workload, the allowed probe and the product reconciliation.
    """
    def __init__(self, lab):
        self.lab = lab
        self.record = lab.DIRECTORY / 'longhorn-security-tls.json'

    def identity(self):
        state = self.lab.owner()
        namespace = self.lab.get('namespace', NAMESPACE)
        secret = self.lab.get('secret', SECRET, NAMESPACE)
        if (not namespace or namespace['metadata']['uid'] != state['namespace_uid'] or
                not secret or secret['metadata']['uid'] != state['tls_uid']):
            raise ValueError('TLS namespace or Secret identity changed')
        return state, secret

    def read(self):
        if self.record.is_symlink():
            raise ValueError('TLS rotation escrow is a symlink')
        info = self.record.stat()
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError('TLS rotation escrow is not private')
        row = json.loads(self.record.read_text()); state, secret = self.identity()
        if (row['run_id'] != state['run_id'] or row['namespace_uid'] != state['namespace_uid'] or
                row['secret_uid'] != secret['metadata']['uid'] or
                secret.get('data') not in row['materials'].values()):
            raise ValueError('TLS escrow or current material changed outside its operation')
        for key, data in row['materials'].items():
            if hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() != row['digests'][key]:
                raise ValueError('TLS escrow material integrity differs')
        return row, secret

    def begin(self, journal, probe):
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous TLS rotation needs scoped recovery')
        state, secret = self.identity()
        original = secret['data']; old = decode(original)
        new, wrong_name = replacement_material()
        bundle = old['ca.crt'].rstrip()+'\n'+new['ca.crt']
        materials = {'original': original,
                     'old-bundle': dict(original, **encode(dict(old, **{'ca.crt': bundle}))),
                     'new-bundle': dict(original, **encode(dict(new, **{'ca.crt': bundle}))),
                     'new-only': dict(original, **encode(new))}
        row = {'run_id': state['run_id'], 'namespace_uid': state['namespace_uid'], 'secret_uid': secret['metadata']['uid'],
               'volume': journal['volume'], 'volume_uid': journal['volume_uid'], 'probe': probe,
               'business_namespace': journal['namespace'], 'business_namespace_uid': journal['namespace_uid'],
               'business_claim_uid': journal['objects']['PersistentVolumeClaim/data'], 'new_data_client': False,
               'materials': materials, 'wrong_name': wrong_name, 'mutation_started': False,
               'digests': {key: hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
                           for key, data in materials.items()}}
        row['parents'] = self.parents()
        self.lab.write_private(self.record, row)
        return row

    def parents(self):
        manager = self.lab.get('daemonset', 'longhorn-manager', NAMESPACE)
        if not manager or manager['metadata'].get('labels', {}).get('app.kubernetes.io/instance') != 'longhorn':
            raise ValueError('TLS rotation manager ownership differs')
        expected = {'longhorn-node-'+address.rsplit('.', 1)[1] for address in self.lab.WORKERS}
        all_managers = self.lab.get('instancemanagers.longhorn.io', namespace=NAMESPACE)['items']
        managers = [obj for obj in all_managers if obj['spec'].get('nodeID') in expected]
        if (len(managers) != len(expected) or {obj['spec'].get('nodeID') for obj in managers} != expected or
                any(obj['spec'].get('dataEngine') != 'v1' or obj['metadata'].get('deletionTimestamp') for obj in managers)):
            raise ValueError('TLS rotation requires one original V1 instance-manager per worker')
        controls = {'longhorn-node-'+address.rsplit('.', 1)[1] for address in self.lab.NODES if address not in self.lab.WORKERS}
        dormant = [obj for obj in all_managers if obj['spec'].get('nodeID') not in expected]
        if len({obj['spec'].get('nodeID') for obj in dormant}) != len(dormant):
            raise ValueError('TLS rotation found duplicate stopped instance-managers')
        for obj in dormant:
            status = obj.get('status', {})
            if (obj['spec'].get('nodeID') not in controls or obj['spec'].get('dataEngine') != 'v1' or
                    obj['metadata'].get('deletionTimestamp') or status.get('currentState') != 'stopped' or
                    any(status.get(key) for key in ('instanceEngines', 'instanceReplicas', 'instanceEngineFrontends',
                                                  'instanceShards', 'instanceShardGroups', 'backingImages')) or
                    self.lab.get('pod', obj['metadata']['name'], NAMESPACE)):
                raise ValueError('TLS rotation found an active or foreign control-node instance-manager')
        return {'DaemonSet/longhorn-manager': {'uid': manager['metadata']['uid']},
                **{'InstanceManager/'+obj['metadata']['name']: {'uid': obj['metadata']['uid'], 'node': obj['spec']['nodeID']}
                   for obj in managers},
                **{'DormantInstanceManager/'+obj['metadata']['name']: {'uid': obj['metadata']['uid'], 'node': obj['spec']['nodeID']}
                   for obj in dormant}}

    def detached(self, row):
        volumes = self.lab.get('volumes.longhorn.io', namespace=NAMESPACE)['items']
        if (len(volumes) != 1 or volumes[0]['metadata']['name'] != row['volume'] or
                volumes[0]['metadata']['uid'] != row['volume_uid'] or
                volumes[0]['spec'].get('dataEngine') != 'v1'):
            raise ValueError('TLS rotation refuses foreign volumes')
        if self.parents() != row['parents']:
            raise ValueError('TLS rotation component parent identity changed')
        return not volumes[0]['spec'].get('nodeID') and volumes[0]['status'].get('state') == 'detached'

    def patch(self, stage):
        row, secret = self.read()
        if stage not in row['materials'] or not self.detached(row):
            raise ValueError('TLS update requires its detached volume and known stage')
        row['mutation_started'] = True
        self.lab.write_private(self.record, row)
        desired = row['materials'][stage]
        if secret['data'] != desired:
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': secret['metadata']['uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': secret['metadata']['resourceVersion']},
                     {'op': 'test', 'path': '/data', 'value': secret['data']},
                     {'op': 'replace', 'path': '/data', 'value': desired}]
            self.lab.kubectl('-n', NAMESPACE, 'patch', 'secret', SECRET, '--type=json',
                             '--patch-file=/dev/stdin', stdin=json.dumps(patch))
        if self.identity()[1]['data'] != desired:
            raise ValueError('TLS material update was not observed')
        return row

    def children(self, row):
        if self.parents() != row['parents']:
            raise ValueError('TLS component parent was replaced')
        control = self.lab.load_helper('longhorn-control-faults.py')
        expected = {'longhorn-node-'+address.rsplit('.', 1)[1] for address in self.lab.WORKERS}
        pods = self.lab.get('pods', namespace=NAMESPACE)['items']
        managers = [pod for pod in pods if pod['metadata'].get('labels', {}).get('app') == 'longhorn-manager']
        if any(pod['spec'].get('nodeName') and pod['spec']['nodeName'] not in expected for pod in managers):
            raise ValueError('TLS manager Pod placement differs')
        instances = []
        for key, parent in row['parents'].items():
            kind, name = key.split('/', 1)
            if kind != 'InstanceManager': continue
            pod = self.lab.get('pod', name, NAMESPACE)
            if pod:
                if not control.controlled(pod, kind, parent['uid']) or pod['spec'].get('nodeName') != parent['node']:
                    raise ValueError('TLS instance-manager Pod parent differs')
                instances.append(pod)
        for pod in managers:
            if not control.controlled(pod, 'DaemonSet', row['parents']['DaemonSet/longhorn-manager']['uid']):
                raise ValueError('TLS manager Pod parent differs')
        # The DaemonSet creates a node-affinity-targeted Pod before the
        # scheduler binds nodeName; a terminating predecessor may still exist.
        # Neither transition proves readiness. Validate ownership first, then
        # wait for all predecessors to disappear and replacements to bind.
        if any(pod['metadata'].get('deletionTimestamp') or not pod['spec'].get('nodeName') for pod in managers):
            return None
        if len(managers) > len(expected):
            raise ValueError('TLS manager Pod placement differs')
        if (len(instances) != len(expected) or len(managers) != len(expected) or
                {pod['spec'].get('nodeName') for pod in managers} != expected or
                {pod['spec'].get('nodeName') for pod in instances} != expected or
                not all(control.ready(pod) for pod in (*instances, *managers))):
            return None
        tags = json.loads((self.lab.BASE/'roles/longhorn/files/images.json').read_text())['1.12.1']
        for container, group in (('instance-manager', instances), ('longhorn-manager', managers)):
            for pod in group:
                selected = [item for item in pod['spec']['containers'] if item['name'] == container]
                component = 'longhorn-instance-manager' if container == 'instance-manager' else 'longhorn-manager'
                image = '/'+component+':'+tags[component]
                if (len(selected) != 1 or not selected[0]['image'].endswith(image) or
                        not any(volume.get('secret', {}).get('secretName') == SECRET for volume in pod['spec'].get('volumes', []))):
                    raise ValueError('TLS component lacks the pinned image or original Secret mount')
        return {'instance-manager': sorted(instances, key=lambda pod: pod['metadata']['name']),
                'longhorn-manager': sorted(managers, key=lambda pod: pod['metadata']['name'])}

    def components(self, row):
        return self.lab.wait('tls-components-ready', 300, lambda: self.children(row))

    def empty(self, row, components):
        if not self.detached(row):
            raise ValueError('TLS restart requires its detached volume')
        for pod in components['instance-manager']:
            processes = json.loads(self.lab.kubectl('-n', NAMESPACE, 'exec', pod['metadata']['name'],
                '-c', 'instance-manager', '--', 'longhorn-instance-manager', '--tls-dir', '/tls-files/',
                'process', 'list', timeout=30).stdout)
            # ProcessList returns the protobuf map directly. An empty map can
            # decode as nil and PrintJSON then emits null rather than {}.
            if processes not in ({}, None):
                return False
        return True

    def mounts_ready(self, row, stage):
        components = self.children(row)
        if not components: return False
        expected = {name: hashlib.sha256(value.encode()).hexdigest()
                    for name, value in decode(row['materials'][stage]).items()}
        for container, pods in components.items():
            for pod in pods:
                result = self.lab.kubectl('-n', NAMESPACE, 'exec', pod['metadata']['name'], '-c', container,
                    '--', 'sha256sum', *('/tls-files/'+name for name in expected), timeout=30)
                observed = {}
                for line in result.stdout.splitlines():
                    fields = line.split()
                    if len(fields) != 2 or fields[1] not in {'/tls-files/'+name for name in expected}:
                        raise ValueError('TLS mount digest response differs')
                    name = fields[1].removeprefix('/tls-files/')
                    if name in observed: raise ValueError('TLS mount digest repeats a file')
                    observed[name] = fields[0]
                if observed != expected: return False
        return True

    def exchange(self, row, pod, material, client=None):
        network = self.lab.load_helper('longhorn-network.py').Probe(self.lab)
        tracked = network.check()['pods'].get(row['probe'])
        if not tracked or not tracked['uid']:
            raise ValueError('TLS allowed probe identity is absent')
        configuration = {'address': pod['status']['podIP'], 'ca': material['ca.crt']}
        if client is not None:
            configuration.update(certificate=client['tls.crt'], key=client['tls.key'])
        result = self.lab.kubectl('-n', NAMESPACE, 'exec', '-i', row['probe'], '--',
            'python3', '-c', probe_program(), stdin=json.dumps(configuration), timeout=30)
        return json.loads(result.stdout)

    def verify_tls(self, stage, *, variant=None, client=None):
        row, secret = self.read()
        if secret['data'] != row['materials'][stage]:
            raise ValueError('TLS probe stage differs from current Secret')
        material = decode(row['materials'][stage])
        components = self.components(row)
        for pod in components['instance-manager']:
            if not accepted(self.exchange(row, pod, material, material), material['tls.crt']):
                raise ValueError('TLS legal control did not authenticate the expected server')
            if variant:
                result = self.exchange(row, pod, material, client)
                if not refused(result, variant):
                    raise ValueError('TLS client refusal was not the expected native alert')
                if not accepted(self.exchange(row, pod, material, material), material['tls.crt']):
                    raise ValueError('TLS legal control failed after client refusal')
                print('LONGHORN_MTLS_REFUSAL_VERIFIED '+json.dumps({'stage': stage, 'variant': variant,
                    'instance_manager': pod['metadata']['name'], 'pod_uid': pod['metadata']['uid'],
                    'reason': result['reason']}, sort_keys=True), flush=True)
        identity = hashlib.sha256(ssl.PEM_cert_to_DER_cert(material['tls.crt'])).hexdigest()
        print('LONGHORN_MTLS_ALLOWED_VERIFIED stage='+stage+' instances='+str(len(components['instance-manager']))+
              ' server_sha256='+identity+' tls=TLSv1.3', flush=True)

    def restart(self, stage):
        current, _ = self.read()
        if not self.empty(current, self.components(current)):
            raise ValueError('TLS rotation refuses active or foreign native processes before updating trust')
        row = self.patch(stage)
        self.lab.wait('tls-secret-projection-'+stage, 300, lambda: self.mounts_ready(row, stage))
        for container in ('instance-manager', 'longhorn-manager'):
            originals = self.components(row)[container]
            for original in originals:
                current = self.components(row)
                if not self.empty(row, current):
                    raise ValueError('TLS rotation refuses active or foreign native processes')
                before = next((pod for pod in current[container]
                               if pod['metadata']['uid'] == original['metadata']['uid']), None)
                if before is None:
                    raise ValueError('TLS restart Pod was replaced before deletion')
                self.lab.remove(before)
                def replaced():
                    observed = self.children(row)
                    return observed and all(pod['metadata']['uid'] != before['metadata']['uid']
                                            for group in observed.values() for pod in group)
                self.lab.wait('tls-component-restarted-'+container, 300, replaced)
        self.verify_tls(stage)
        if not self.empty(row, self.components(row)):
            raise ValueError('TLS rotation found native processes after a detached restart')
        print('LONGHORN_MTLS_STAGE_VERIFIED stage='+stage+' volume=detached native_processes=empty', flush=True)

    def restore(self):
        if not self.record.exists() and not self.record.is_symlink(): return
        row, secret = self.read()
        if row.get('new_data_client'):
            self.detach_new_data()
            row, secret = self.read()
        if row['mutation_started']:
            new_leaf = row['materials']['new-only']['tls.crt']
            stages = ['new-bundle', 'old-bundle', 'original'] if secret['data']['tls.crt'] == new_leaf else ['old-bundle', 'original']
            for stage in stages: self.restart(stage)
        if self.identity()[1]['data'] != row['materials']['original']:
            raise ValueError('TLS rotation did not restore the original Secret')
        self.record.unlink()
        print('LONGHORN_MTLS_ORIGINAL_MATERIAL_RESTORED', flush=True)

    def verify_plaintext(self, stage):
        row, _ = self.read(); material = decode(row['materials'][stage])
        program = ('import ipaddress,json,socket,sys\n'+inspect.getsource(plaintext_exchange)+
                   '\nprint(json.dumps(plaintext_exchange(json.load(sys.stdin)["address"]),sort_keys=True))\n')
        for pod in self.components(row)['instance-manager']:
            if not accepted(self.exchange(row, pod, material, material), material['tls.crt']):
                raise ValueError('TLS control failed before plaintext probe')
            result = self.lab.kubectl('-n', NAMESPACE, 'exec', '-i', row['probe'], '--', 'python3', '-c', program,
                                     stdin=json.dumps({'address': pod['status']['podIP']}), timeout=30)
            if json.loads(result.stdout) != {'state': 'closed'}:
                raise ValueError('plaintext was not closed by the paired healthy TLS endpoint')
            if not accepted(self.exchange(row, pod, material, material), material['tls.crt']):
                raise ValueError('TLS control failed after plaintext probe')
        print('LONGHORN_MTLS_PLAINTEXT_CLOSED_VERIFIED instances=3 paired_tls=true', flush=True)

    def verify_rbac(self, journal, create):
        binding = self.lab.get('clusterrolebinding', 'longhorn-bind')
        expected_subject = {'kind': 'ServiceAccount', 'name': 'longhorn-service-account', 'namespace': NAMESPACE}
        manager = self.lab.get('daemonset', 'longhorn-manager', NAMESPACE)
        template = manager.get('spec', {}).get('template', {}).get('spec', {}) if manager else {}
        if (not binding or binding.get('roleRef') != {'apiGroup': 'rbac.authorization.k8s.io',
                'kind': 'ClusterRole', 'name': 'longhorn-role'} or binding.get('subjects') != [expected_subject] or
                template.get('serviceAccountName') != 'longhorn-service-account' or
                not any(container.get('name') == 'longhorn-manager' and
                        container.get('securityContext', {}).get('privileged') is True
                        for container in template.get('containers', []))):
            raise ValueError('Longhorn privileged service-account boundary differs')
        account = create({'apiVersion': 'v1', 'kind': 'ServiceAccount',
            'metadata': {'name': 'unprivileged-storage-client', 'namespace': journal['namespace']},
            'automountServiceAccountToken': False})
        identity = 'system:serviceaccount:'+journal['namespace']+':'+account['metadata']['name']
        self.lab.kubectl('-n', NAMESPACE, 'get', 'secret', SECRET, '-o', 'name')
        for resource in ('secrets', 'pods', 'volumes.longhorn.io'):
            result = self.lab.kubectl('--as='+identity, '-n', NAMESPACE, 'get', resource, '-o', 'name', check=False)
            if (result.returncode == 0 or 'Error from server (Forbidden)' not in result.stderr or
                    'User "'+identity+'"' not in result.stderr or 'cannot list resource "'+resource.split('.')[0]+'"' not in result.stderr):
                raise ValueError('tenant access was not refused by native authorization')
        privileged = next(container for container in template['containers'] if container['name'] == 'longhorn-manager')
        request = {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': 'tenant-privilege-check', 'namespace': NAMESPACE},
            'spec': {'restartPolicy': 'Never', 'hostPID': True,
                     'containers': [{'name': 'unauthorized', 'image': privileged['image'],
                                     'securityContext': {'privileged': True}}]}}
        result = self.lab.kubectl('--as='+identity, '-n', NAMESPACE, 'create', '--dry-run=server', '-f', '-', '-o', 'name',
                                 stdin=json.dumps(request), check=False)
        if (result.returncode == 0 or 'Error from server (Forbidden)' not in result.stderr or
                'User "'+identity+'"' not in result.stderr or 'cannot create resource "pods"' not in result.stderr):
            raise ValueError('tenant privileged Pod creation was not refused by native authorization')
        print('LONGHORN_SECURITY_RBAC_REFUSAL_VERIFIED resources=secrets,pods,volumes privileged_pod_create=forbidden no_token_material=true', flush=True)

    def detach_new_data(self):
        row, _ = self.read()
        if not row.get('new_data_client'): return
        path = self.lab.DIRECTORY/'longhorn-install-business.json'
        if path.is_symlink(): raise ValueError('new-CA business journal is a symlink')
        journal = json.loads(path.read_text())
        if (journal.get('namespace') != row['business_namespace'] or
                journal.get('namespace_uid') != row['business_namespace_uid'] or
                journal.get('volume') != row['volume'] or journal.get('volume_uid') != row['volume_uid'] or
                journal.get('objects', {}).get('PersistentVolumeClaim/data') != row['business_claim_uid']):
            raise ValueError('new-CA business journal identity changed')
        self.lab.check_business_graph(journal)
        name = 'security-new-ca'; pod = self.lab.get('pod', name, journal['namespace'])
        if pod:
            if pod['metadata']['uid'] != journal['objects'].get('Pod/'+name):
                raise ValueError('new-CA client has no original creation UID')
            self.lab.remove(pod)
        self.lab.wait('security-new-ca-client-gone', 180,
                      lambda: self.lab.get('pod', name, journal['namespace']) is None)
        self.lab.wait('security-new-ca-volume-detached', 300, lambda: self.detached(row))
        self.lab.wait('security-new-ca-processes-empty', 180, lambda: self.empty(row, self.components(row)))
        row['new_data_client'] = False
        self.lab.write_private(self.record, row)

    def verify_new_data(self, journal, create, payload, digest):
        """Exercise fresh Manager/CSI gRPC work while only the new CA is trusted."""
        name = 'security-new-ca'
        row, secret = self.read()
        if secret['data'] != row['materials']['new-only']:
            raise ValueError('new-CA business proof requires new-only trust')
        row['new_data_client'] = True; self.lab.write_private(self.record, row)
        with self.lab.scoped_cleanup(self.detach_new_data):
            client = create(self.lab.client_pod(journal['namespace'], name, 'data',
                             'longhorn-node-'+self.lab.WORKERS[0].rsplit('.', 1)[1]))
            ready = self.lab.load_helper('longhorn-network.py').ready
            self.lab.wait('security-new-ca-client-ready', 600,
                          lambda: ready(self.lab.get('pod', name, journal['namespace'])))
            current = self.lab.get('pod', name, journal['namespace'])
            if not current or current['metadata']['uid'] != client['metadata']['uid']:
                raise ValueError('new-CA client was replaced before data proof')
            actual = self.lab.kubectl('-n', journal['namespace'], 'exec', name, '--', 'python3', '-c',
                                     self.lab.payload_program(payload, read_only=True)).stdout.strip()
            if actual != digest:
                raise ValueError('new-CA Manager/CSI path did not preserve confirmed data')
            self.lab.wait('security-new-ca-replicas-healthy', 600, lambda: self.lab.replica_topology(journal, 3))
        print('LONGHORN_SECURITY_NEW_CA_DATA_VERIFIED sha256='+digest+' detached=true native_processes=empty', flush=True)

    def verify(self, journal, create, payload, digest):
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('previous TLS rotation needs recovery')
        helper = self.lab.load_helper('longhorn-network.py'); network = helper.Probe(self.lab)
        if network.record.exists() or network.record.is_symlink():
            raise ValueError('previous system network probes need recovery')
        state, _ = self.identity(); name = 'security-allowed-'+secrets.token_hex(8)
        policies = helper.policy_identity(self.lab)
        if not {'longhorn-manager', 'instance-manager'} <= set(policies):
            raise ValueError('security probe requires original internal network policies')
        self.lab.write_private(network.record, {'run_id': state['run_id'], 'namespace_uid': state['namespace_uid'],
                                              'policies': policies, 'pods': {}})
        def cleanup_probes():
            # A failed TLS restore still needs its allowed client. The fixed
            # recovery path retries TLS before touching the probe journal.
            if not self.record.exists() and not self.record.is_symlink():
                network.cleanup()
        with self.lab.scoped_cleanup(cleanup_probes):
            pod = network.create_system(network.pod(NAMESPACE, name,
                {'longhorn.io/job-task': 'kubeauto-security-probe', 'longhorn.io/component': 'backing-image-data-source'}))
            self.lab.wait('security-probe-ready', 180, lambda: helper.ready(self.lab.get('pod', name, NAMESPACE)))
            if self.lab.get('pod', name, NAMESPACE)['metadata']['uid'] != pod['metadata']['uid']:
                raise ValueError('security probe was replaced')
            row = self.begin(journal, name)
            with self.lab.scoped_cleanup(self.restore):
                self.lab.wait('security-original-secret-projection', 300, lambda: self.mounts_ready(row, 'original'))
                self.verify_rbac(journal, create)
                foreign = self.lab.tls_fixture()
                self.verify_tls('original', variant='missing')
                self.verify_tls('original', variant='untrusted', client=foreign)
                self.verify_plaintext('original')
                writer = self.lab.get('pod', 'writer', journal['namespace'])
                if not writer or writer['metadata']['uid'] != journal['objects']['Pod/writer']:
                    raise ValueError('security writer identity changed before detach')
                self.lab.remove(writer)
                self.lab.wait('security-writer-gone', 180,
                              lambda: self.lab.get('pod', 'writer', journal['namespace']) is None)
                self.lab.wait('security-volume-detached', 300, lambda: self.detached(row))
                self.lab.wait('security-native-processes-empty', 180, lambda: self.empty(row, self.components(row)))
                for stage in ('old-bundle', 'new-bundle', 'new-only'):
                    self.restart(stage)
                self.verify_tls('new-only', variant='wrong-name', client=row['wrong_name'])
                self.verify_tls('new-only', variant='missing')
                self.verify_tls('new-only', variant='untrusted', client=decode(row['materials']['original']))
                self.verify_plaintext('new-only')
                self.verify_new_data(journal, create, payload, digest)
        client = 'security-recovered'
        create(self.lab.client_pod(journal['namespace'], client, 'data',
                                  'longhorn-node-'+self.lab.WORKERS[0].rsplit('.', 1)[1]))
        self.lab.wait('security-original-material-remount', 600, lambda:
                      helper.ready(self.lab.get('pod', client, journal['namespace'])))
        actual = self.lab.kubectl('-n', journal['namespace'], 'exec', client, '--', 'python3', '-c',
                                 self.lab.payload_program(payload, read_only=True)).stdout.strip()
        if actual != digest or not self.lab.replica_topology(journal, 3):
            raise ValueError('TLS rotation did not recover original confirmed data and replicas')
        print('LONGHORN_SECURITY_ROTATION_DATA_VERIFIED sha256='+digest+' original_secret=true', flush=True)
        return client
