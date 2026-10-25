"""TLS authentication completion, exact refusals and bounded HTTP/2 framing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('security_probe', ROOT/'tests/helpers/longhorn-security.py')
security = importlib.util.module_from_spec(spec); spec.loader.exec_module(security)
spec = importlib.util.spec_from_file_location('security_lab_fixture', ROOT/'tests/helpers/longhorn-lab.py')
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)


class TLSContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.material = lab.tls_fixture()
        cls.foreign = lab.tls_fixture()

    def exchange(self, *, client='valid', frame=None, alpn='h2'):
        material = self.material
        if frame is None: frame = b'\x00\x00\x06\x04\x00\x00\x00\x00\x00\x00\x03\x00\x00\x00\x01'
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as pool:
            root = Path(directory)
            for name in ('ca.crt', 'tls.crt', 'tls.key'):
                path = root/name; path.write_text(material[name]); path.chmod(0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(str(root/'tls.crt'), str(root/'tls.key'))
            context.load_verify_locations(cadata=material['ca.crt'])
            context.verify_mode = ssl.CERT_REQUIRED
            context.set_alpn_protocols([alpn])
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); listener.listen(1); listener.settimeout(5)
                def serve():
                    connection, _ = listener.accept(); connection.settimeout(5)
                    try:
                        with context.wrap_socket(connection, server_side=True) as secured:
                            # Match pinned gRPC: server SETTINGS precedes the
                            # client's preface, after server-side authentication.
                            for offset in range(0, len(frame), 3): secured.sendall(frame[offset:offset+3])
                            preface = b''
                            while len(preface) < 33:
                                part = secured.recv(33-len(preface))
                                if not part: return 'client-closed'
                                preface += part
                            self.assertEqual(preface, b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' +
                                             b'\x00\x00\x00\x04\x00\x00\x00\x00\x00')
                            return 'accepted'
                    except ssl.SSLError:
                        return 'server-tls-refusal'
                    except OSError:
                        return 'client-closed'
                    finally:
                        connection.close()
                future = pool.submit(serve)
                configuration = {'address': '127.0.0.1', 'port': listener.getsockname()[1], 'ca': material['ca.crt']}
                if client != 'missing':
                    selected = self.foreign if client == 'untrusted' else material
                    configuration.update(certificate=selected['tls.crt'], key=selected['tls.key'])
                observation = security.tls_exchange(configuration)
                server_result = future.result(timeout=8)
            return observation, server_result

    def test_real_tls13_requires_authenticated_http2_and_exact_server_certificate(self):
        observation, server = self.exchange()
        self.assertEqual(server, 'accepted')
        self.assertTrue(security.accepted(observation, self.material['tls.crt']))
        self.assertFalse(security.accepted(observation, self.foreign['tls.crt']))
        self.assertEqual(observation['server_sha256'],
            hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.material['tls.crt'])).hexdigest())

    def test_missing_and_foreign_client_certificates_get_native_tls_alerts(self):
        for variant in ('missing', 'untrusted'):
            with self.subTest(variant=variant):
                observation, server = self.exchange(client=variant)
                self.assertEqual(server, 'server-tls-refusal')
                self.assertTrue(security.refused(observation, variant), observation)
                self.assertFalse(security.accepted(observation, self.material['tls.crt']))

    def test_bad_http2_frame_or_missing_alpn_cannot_prove_grpc_success(self):
        frames = (b'\x00\x00\x00\x00\x00\x00\x00\x00\x00',  # DATA, not SETTINGS
                  b'\x00\x00\x00\x04\x01\x00\x00\x00\x00',  # ACK before server SETTINGS
                  b'\x00\x00\x01\x04\x00\x00\x00\x00\x00x',
                  b'\x00\x40\x02\x04\x00\x00\x00\x00\x00',
                  b'\x00\x00\x00\x04\x00\x00\x00\x00\x01')
        for frame in frames:
            with self.subTest(frame=frame):
                observation, _ = self.exchange(frame=frame)
                self.assertFalse(security.accepted(observation, self.material['tls.crt']))
                self.assertIn(observation['state'], ('invalid-http2', 'closed'))
        observation, _ = self.exchange(alpn='http/1.1')
        self.assertFalse(security.accepted(observation, self.material['tls.crt']))

    def test_closed_timeout_bad_server_or_generic_ssl_error_never_count_as_client_refusal(self):
        for variant in security.CLIENT_ALERTS:
            for observation in ({'state': 'closed'}, {'state': 'timeout'},
                    {'state': 'network-error', 'type': 'ConnectionRefusedError'},
                    {'state': 'tls-error', 'reason': 'CERTIFICATE_VERIFY_FAILED'},
                    {'state': 'tls-error', 'reason': 'UNEXPECTED_EOF_WHILE_READING'},
                    {'state': 'tls-error', 'reason': 'TLSV1_ALERT_PROTOCOL_VERSION'}):
                with self.subTest(variant=variant, observation=observation):
                    self.assertFalse(security.refused(observation, variant))
        self.assertFalse(security.refused({'state': 'tls-error', 'reason': 'SSLV3_ALERT_BAD_CERTIFICATE'}, 'unknown'))

    def test_network_errors_are_distinct_and_destination_validates_before_connect(self):
        config = {'address': '127.0.0.1', 'ca': self.material['ca.crt']}
        for error, state in ((TimeoutError(), 'timeout'), (ConnectionRefusedError(), 'network-error')):
            with patch.object(security.socket, 'create_connection', side_effect=error):
                self.assertEqual(security.tls_exchange(config)['state'], state)
        for update in ({'address': 'foreign.example'}, {'port': True}, {'port': 0}):
            with patch.object(security.socket, 'create_connection') as connect:
                with self.assertRaises(ValueError): security.tls_exchange(dict(config, **update))
                connect.assert_not_called()

    def test_transmitted_probe_is_self_contained_and_does_not_print_credentials(self):
        program = security.probe_program()
        compile(program, 'remote-tls-probe', 'exec')
        self.assertIn('json.load(sys.stdin)', program)
        self.assertNotIn('str(error)', program)
        self.assertNotIn('print(configuration', program)

    def test_server_settings_are_read_before_the_first_application_write(self):
        context = Mock(); secured = Mock()
        context.wrap_socket.return_value = nullcontext(secured)
        secured.getpeercert.return_value = ssl.PEM_cert_to_DER_cert(self.material['tls.crt'])
        secured.selected_alpn_protocol.return_value = 'h2'; secured.version.return_value = 'TLSv1.3'
        read = []
        def receive(_count):
            read.append(True)
            return b'\x00\x00\x00\x04\x00\x00\x00\x00\x00'
        def send(_preface):
            if not read: raise ssl.SSLError('early application write hid the authentication alert')
        secured.recv.side_effect = receive; secured.sendall.side_effect = send
        with patch.object(security.ssl, 'create_default_context', return_value=context), \
                patch.object(security.socket, 'create_connection', return_value=nullcontext(Mock())):
            observed = security.tls_exchange({'address': '127.0.0.1', 'ca': self.material['ca.crt']})
        self.assertTrue(security.accepted(observed, self.material['tls.crt']))
        secured.sendall.assert_called_once()


class RotationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = security.encode(lab.tls_fixture())
        cls.replacement = security.replacement_material()

    def fixture(self, directory):
        catalog = Path(directory)/'roles/longhorn/files/images.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({'1.12.1': {'longhorn-instance-manager': 'v1.12.1',
                                                'longhorn-manager': 'v1.12.1'}}))
        state = {'run_id': 'owned-run', 'namespace_uid': 'namespace-uid', 'tls_uid': 'tls-uid'}
        secret = {'metadata': {'uid': 'tls-uid', 'resourceVersion': '17'}, 'data': copy.deepcopy(self.original)}
        volume = {'metadata': {'name': 'pvc-owned', 'uid': 'volume-uid'},
                  'spec': {'dataEngine': 'v1', 'nodeID': ''}, 'status': {'state': 'detached'}}
        workers = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
        instances = [{'metadata': {'name': 'im-'+address.rsplit('.', 1)[1], 'uid': address+'-uid'},
                      'spec': {'nodeID': 'longhorn-node-'+address.rsplit('.', 1)[1], 'dataEngine': 'v1'}} for address in workers]
        manager = {'metadata': {'uid': 'daemonset-uid', 'labels': {'app.kubernetes.io/instance': 'longhorn'}}}
        def get(kind, name='', namespace=''):
            if kind == 'namespace': return {'metadata': {'uid': state['namespace_uid']}}
            if kind == 'secret': return copy.deepcopy(secret)
            if kind == 'daemonset': return copy.deepcopy(manager)
            if kind == 'instancemanagers.longhorn.io': return {'items': copy.deepcopy(instances)}
            if kind == 'volumes.longhorn.io': return {'items': [copy.deepcopy(volume)]}
            raise AssertionError(kind)
        def write(path, value):
            path.write_text(json.dumps(value)); path.chmod(0o600)
        def kubectl(*args, **kwargs):
            self.assertIn('patch', args)
            operations = json.loads(kwargs['stdin'])
            self.assertEqual(operations[:3], [{'op': 'test', 'path': '/metadata/uid', 'value': 'tls-uid'},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'},
                {'op': 'test', 'path': '/data', 'value': secret['data']}])
            self.assertEqual(operations[3]['path'], '/data')
            secret['data'] = operations[3]['value']
        fixture = SimpleNamespace(BASE=Path(directory), DIRECTORY=Path(directory), WORKERS=workers, owner=Mock(return_value=state),
            NODES=('192.168.122.135', '192.168.122.40', '192.168.122.72')+workers,
            get=Mock(side_effect=get), write_private=Mock(side_effect=write), kubectl=Mock(side_effect=kubectl))
        rotation = security.Rotation(fixture)
        with patch.object(security, 'replacement_material', return_value=copy.deepcopy(self.replacement)):
            row = rotation.begin({'volume': 'pvc-owned', 'volume_uid': 'volume-uid',
                'namespace': 'business', 'namespace_uid': 'business-uid',
                'objects': {'PersistentVolumeClaim/data': 'claim-uid'}}, 'probe-owned')
        return rotation, fixture, secret, volume, instances, row

    def test_generated_leaves_share_a_real_ca_but_have_distinct_peer_identity(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        legitimate, wrong = self.replacement
        ca = x509.load_pem_x509_certificate(legitimate['ca.crt'].encode())
        for material, expected_peer in ((legitimate, 'longhorn-backend'), (wrong, 'unauthorized-longhorn-peer')):
            certificate = x509.load_pem_x509_certificate(material['tls.crt'].encode())
            self.assertEqual(certificate.issuer, ca.subject)
            ca.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256())
            self.assertEqual(certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value, expected_peer)
            names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            self.assertIn(expected_peer, names)
            if material is wrong: self.assertNotIn('longhorn-backend', names)
            self.assertIn(ExtendedKeyUsageOID.CLIENT_AUTH,
                          certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        self.assertNotEqual(legitimate['tls.key'], wrong['tls.key'])

    def test_stage_updates_are_uid_resource_version_and_original_data_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            rotation, fixture, secret, _volume, _instances, row = self.fixture(directory)
            old = security.decode(row['materials']['original'])
            new = security.decode(row['materials']['new-only'])
            for stage in ('old-bundle', 'new-bundle', 'new-only'):
                rotation.patch(stage)
                self.assertEqual(secret['data'], row['materials'][stage])
                self.assertTrue(rotation.read()[0]['mutation_started'])
            self.assertEqual(fixture.kubectl.call_count, 3)
            for stage in ('old-bundle', 'new-bundle'):
                bundle = security.decode(row['materials'][stage])['ca.crt']
                self.assertIn(old['ca.crt'].strip(), bundle)
                self.assertIn(new['ca.crt'].strip(), bundle)
            self.assertNotIn(old['ca.crt'].strip(), new['ca.crt'])

    def test_foreign_material_escrow_or_parent_prevents_mutation_and_preserves_record(self):
        for defect in ('secret-data', 'secret-uid', 'run', 'mode', 'digest', 'symlink', 'parent'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, secret, _volume, instances, row = self.fixture(directory)
                if defect == 'secret-data': secret['data'] = {'foreign': 'must-not-be-overwritten'}
                if defect == 'secret-uid': secret['metadata']['uid'] = 'replacement'
                if defect == 'run': row['run_id'] = 'foreign'
                if defect == 'digest': row['digests']['original'] = 'bad'
                if defect in ('run', 'digest'): fixture.write_private(rotation.record, row)
                if defect == 'mode': rotation.record.chmod(0o644)
                if defect == 'parent': instances[0]['metadata']['uid'] = 'foreign-parent'
                if defect == 'symlink':
                    target = Path(directory)/'private-original'; rotation.record.rename(target); rotation.record.symlink_to(target)
                with self.assertRaises(ValueError): rotation.patch('new-only')
                fixture.kubectl.assert_not_called()
                self.assertTrue(rotation.record.exists())

    def test_attached_requested_attached_or_foreign_volume_blocks_certificate_change(self):
        for defect in ('attached', 'requested', 'foreign-uid', 'foreign-name', 'v2'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, volume, _instances, _row = self.fixture(directory)
                if defect == 'attached': volume['status']['state'] = 'attached'
                if defect == 'requested': volume['spec']['nodeID'] = 'longhorn-node-212'
                if defect == 'foreign-uid': volume['metadata']['uid'] = 'foreign'
                if defect == 'foreign-name': volume['metadata']['name'] = 'pvc-foreign'
                if defect == 'v2': volume['spec']['dataEngine'] = 'v2'
                with self.assertRaises(ValueError): rotation.patch('new-only')
                fixture.kubectl.assert_not_called()

    def test_restore_uses_staged_trust_and_stops_on_first_failed_component_stage(self):
        for stage in ('original', 'old-bundle', 'new-bundle', 'new-only'):
            for failure in ('none', 'first'):
                with self.subTest(stage=stage, failure=failure), tempfile.TemporaryDirectory() as directory:
                    rotation, fixture, secret, _volume, _instances, row = self.fixture(directory)
                    row['mutation_started'] = stage != 'original'; secret['data'] = row['materials'][stage]
                    fixture.write_private(rotation.record, row)
                    stages = []
                    def restart(target):
                        stages.append(target)
                        if failure == 'first': raise ValueError('first component did not recover')
                        secret['data'] = row['materials'][target]
                    rotation.restart = Mock(side_effect=restart)
                    with redirect_stdout(io.StringIO()):
                        if failure == 'first' and stage != 'original':
                            with self.assertRaisesRegex(ValueError, 'first component'): rotation.restore()
                        else: rotation.restore()
                    expected = [] if stage == 'original' else ['old-bundle', 'original'] if stage == 'old-bundle' else ['new-bundle', 'old-bundle', 'original']
                    self.assertEqual(stages, expected[:1] if failure == 'first' else expected)
                    if failure == 'first' and stage != 'original': self.assertTrue(rotation.record.exists())
                    else:
                        self.assertEqual(secret['data'], self.original)
                        self.assertFalse(rotation.record.exists())

    def test_detach_wait_observes_requested_attachment_without_permitting_certificate_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            rotation, fixture, _secret, volume, _instances, row = self.fixture(directory)
            volume['spec']['nodeID'] = 'longhorn-node-212'
            self.assertFalse(rotation.detached(row))
            with self.assertRaises(ValueError): rotation.patch('old-bundle')
            fixture.kubectl.assert_not_called()
            volume['spec']['nodeID'] = ''; volume['status']['state'] = 'detaching'
            self.assertFalse(rotation.detached(row))
            volume['status']['state'] = 'detached'
            self.assertTrue(rotation.detached(row))

    def test_active_native_processes_block_component_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
            components = {'instance-manager': [{'metadata': {'uid': 'pod-uid'}}], 'longhorn-manager': []}
            fixture.wait = Mock(side_effect=lambda label, timeout, check: self.assertTrue(check()))
            fixture.remove = Mock()
            rotation.patch = Mock(return_value=row); rotation.mounts_ready = Mock(return_value=True)
            rotation.components = Mock(return_value=components); rotation.empty = Mock(return_value=False)
            with self.assertRaisesRegex(ValueError, 'active or foreign'): rotation.restart('old-bundle')
            rotation.patch.assert_not_called()
            fixture.remove.assert_not_called()

    def test_native_empty_map_may_be_json_null_after_protobuf_decoding(self):
        for payload, expected in (('{}', True), ('null', True), ('[]', False),
                                  ('{"foreign-process":{"status":{"state":"running"}}}', False)):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
                fixture.kubectl = Mock(return_value=SimpleNamespace(stdout=payload))
                self.assertEqual(rotation.empty(row, {'instance-manager': [{'metadata': {'name': 'im'}}]}), expected)

    def test_native_tls_refusal_requires_healthy_paired_controls_for_each_target(self):
        for defect in ('none', 'timeout', 'bad-control'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, _fixture, _secret, _volume, _instances, _row = self.fixture(directory)
                rotation.components = Mock(return_value={'instance-manager': [{'metadata': {'name': 'im', 'uid': 'im-pod'}}]})
                success = {'state': 'accepted', 'server_sha256': hashlib.sha256(
                    ssl.PEM_cert_to_DER_cert(security.decode(self.original)['tls.crt'])).hexdigest(), 'tls': 'TLSv1.3', 'alpn': 'h2'}
                failure = {'state': 'tls-error', 'reason': 'TLSV13_ALERT_CERTIFICATE_REQUIRED'}
                rotation.exchange = Mock(side_effect=[{'state': 'timeout'} if defect == 'bad-control' else success,
                    {'state': 'timeout'} if defect == 'timeout' else failure, success])
                with redirect_stdout(io.StringIO()):
                    if defect == 'none': rotation.verify_tls('original', variant='missing')
                    else:
                        with self.assertRaises(ValueError): rotation.verify_tls('original', variant='missing')
                self.assertEqual(rotation.exchange.call_count, {'none': 3, 'timeout': 2, 'bad-control': 1}[defect])

    def test_components_bind_parent_node_image_secret_and_readiness(self):
        control_spec = importlib.util.spec_from_file_location('security_component_contract', ROOT/'tests/helpers/longhorn-control-faults.py')
        control = importlib.util.module_from_spec(control_spec); control_spec.loader.exec_module(control)
        for defect in ('none', 'pod-parent', 'pod-node', 'image', 'secret', 'not-ready', 'missing',
                       'unscheduled', 'terminating-overlap', 'foreign-terminating', 'duplicate-active',
                       'adopted', 'old-im-after-adopt', 'new-manager-after-adopt'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
                pods = []
                for index, address in enumerate(fixture.WORKERS):
                    node = 'longhorn-node-'+address.rsplit('.', 1)[1]
                    for container, kind, name in (('longhorn-manager', 'DaemonSet', 'longhorn-manager'),
                                                  ('instance-manager', 'InstanceManager', 'im-'+address.rsplit('.', 1)[1])):
                        parent = row['parents'][kind+'/'+name]
                        pods.append({'metadata': {'name': 'manager-'+str(index) if kind == 'DaemonSet' else name,
                            'uid': container+'-'+str(index), 'labels': {'app': 'longhorn-manager'} if kind == 'DaemonSet' else {},
                            'ownerReferences': [{'kind': kind, 'uid': parent['uid'], 'controller': True}]},
                            'spec': {'nodeName': node, 'volumes': [{'secret': {'secretName': security.SECRET}}],
                                'containers': [{'name': container, 'image': 'official/longhorn-'+
                                    ('instance-manager' if kind == 'InstanceManager' else 'manager')+':v1.12.1'}]},
                            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}})
                if defect in ('adopted', 'old-im-after-adopt', 'new-manager-after-adopt'):
                    (fixture.BASE/'roles/longhorn/files/images.json').write_text(json.dumps({'1.12.1':
                        {'longhorn-instance-manager': 'v1.12.1-kubeauto.1', 'longhorn-manager': 'v1.12.1'}}))
                    for pod in pods:
                        if pod['spec']['containers'][0]['name'] == 'instance-manager':
                            pod['spec']['containers'][0]['image'] += '-kubeauto.1'
                    if defect == 'old-im-after-adopt':
                        pods[-1]['spec']['containers'][0]['image'] = 'official/longhorn-instance-manager:v1.12.1'
                    if defect == 'new-manager-after-adopt':
                        pods[0]['spec']['containers'][0]['image'] += '-kubeauto.1'
                if defect == 'pod-parent': pods[-1]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                if defect == 'pod-node': pods[-1]['spec']['nodeName'] = pods[1]['spec']['nodeName']
                if defect == 'image': pods[-1]['spec']['containers'][0]['image'] = 'official/longhorn-instance-manager:v1.12.0'
                if defect == 'secret': pods[-1]['spec']['volumes'][0]['secret']['secretName'] = 'foreign'
                if defect == 'not-ready': pods[-1]['status']['conditions'][0]['status'] = 'False'
                if defect == 'missing': pods.pop()
                if defect == 'unscheduled': pods[0]['spec'].pop('nodeName')
                if defect in ('terminating-overlap', 'foreign-terminating', 'duplicate-active'):
                    extra = copy.deepcopy(pods[0])
                    extra['metadata'].update(name='replacement-manager', uid='replacement-manager-uid')
                    pods.append(extra)
                    if defect != 'duplicate-active': pods[0]['metadata']['deletionTimestamp'] = '2026-10-21T02:01:00Z'
                    if defect == 'foreign-terminating': pods[0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                original_get = fixture.get.side_effect
                def get(kind, name='', namespace=''):
                    if kind == 'pods': return {'items': copy.deepcopy(pods)}
                    if kind == 'pod': return next((copy.deepcopy(pod) for pod in pods if pod['metadata']['name'] == name), None)
                    return original_get(kind, name, namespace)
                fixture.get.side_effect = get; fixture.load_helper = Mock(return_value=control)
                if defect in ('pod-parent', 'pod-node', 'image', 'secret', 'foreign-terminating', 'duplicate-active',
                               'old-im-after-adopt', 'new-manager-after-adopt'):
                    with self.assertRaises(ValueError): rotation.children(row)
                elif defect in ('not-ready', 'missing', 'unscheduled', 'terminating-overlap'):
                    self.assertIsNone(rotation.children(row))
                else:
                    self.assertEqual({key: len(group) for key, group in rotation.children(row).items()},
                                     {'instance-manager': 3, 'longhorn-manager': 3})

    def test_stopped_control_node_crs_are_preserved_and_never_selected_for_restart(self):
        for defect in ('none', 'running', 'pod', 'process', 'foreign-node', 'duplicate-node', 'v2'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, instances, _row = self.fixture(directory)
                stopped = {'metadata': {'name': 'dormant-control', 'uid': 'dormant-uid'},
                    'spec': {'nodeID': 'longhorn-node-135', 'dataEngine': 'v1'},
                    'status': {'currentState': 'stopped', 'instanceEngines': {}, 'instanceReplicas': {}}}
                if defect == 'running': stopped['status']['currentState'] = 'running'
                if defect == 'process': stopped['status']['instanceReplicas'] = {'unexpected': {}}
                if defect == 'foreign-node': stopped['spec']['nodeID'] = 'unowned-node'
                if defect == 'v2': stopped['spec']['dataEngine'] = 'v2'
                instances.append(stopped)
                if defect == 'duplicate-node':
                    duplicate = copy.deepcopy(stopped); duplicate['metadata'] = {'name': 'duplicate', 'uid': 'duplicate'}
                    instances.append(duplicate)
                original_get = fixture.get.side_effect
                def get(kind, name='', namespace=''):
                    if kind == 'pod': return {'metadata': {'uid': 'unexpected-pod'}} if defect == 'pod' else None
                    return original_get(kind, name, namespace)
                fixture.get.side_effect = get
                if defect == 'none':
                    parents = rotation.parents()
                    self.assertEqual(sum(key.startswith('InstanceManager/') for key in parents), 3)
                    self.assertEqual(parents['DormantInstanceManager/dormant-control']['uid'], 'dormant-uid')
                else:
                    with self.assertRaises(ValueError): rotation.parents()

    def test_projection_requires_all_original_materials_before_restart(self):
        for defect in ('none', 'stale', 'duplicate', 'foreign-path'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
                components = {'instance-manager': [{'metadata': {'name': 'im'}}],
                              'longhorn-manager': [{'metadata': {'name': 'manager'}}]}
                rotation.children = Mock(return_value=components)
                lines = [hashlib.sha256(value.encode()).hexdigest()+'  /tls-files/'+name
                         for name, value in security.decode(row['materials']['original']).items()]
                if defect == 'stale': lines[-1] = 'a'*64+'  /tls-files/tls.key'
                if defect == 'duplicate': lines.append(lines[-1])
                if defect == 'foreign-path': lines[-1] = 'a'*64+'  /foreign/tls.key'
                fixture.kubectl = Mock(return_value=SimpleNamespace(stdout='\n'.join(lines)))
                if defect in ('duplicate', 'foreign-path'):
                    with self.assertRaises(ValueError): rotation.mounts_ready(row, 'original')
                else: self.assertEqual(rotation.mounts_ready(row, 'original'), defect == 'none')

    def test_tenant_requires_native_forbidden_not_network_error_or_an_accidental_allow(self):
        for outcome in ('forbidden', 'network', 'allowed', 'different-user', 'create-allowed', 'create-network'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, _row = self.fixture(directory)
                identity = 'system:serviceaccount:business:unprivileged-storage-client'
                binding = {'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'longhorn-role'},
                    'subjects': [{'kind': 'ServiceAccount', 'name': 'longhorn-service-account', 'namespace': security.NAMESPACE}]}
                manager = {'spec': {'template': {'spec': {'serviceAccountName': 'longhorn-service-account',
                    'containers': [{'name': 'longhorn-manager', 'image': 'official/longhorn-manager:v1.12.1',
                                    'securityContext': {'privileged': True}}]}}}}
                fixture.get = Mock(side_effect=lambda kind, *args: binding if kind == 'clusterrolebinding' else manager)
                create = Mock(return_value={'metadata': {'name': 'unprivileged-storage-client'}})
                def kubectl(*args, **kwargs):
                    if not args[0].startswith('--as='): return SimpleNamespace(returncode=0, stdout='secret/name', stderr='')
                    creation = args[3] == 'create'
                    resource = 'pods' if creation else args[4].split('.')[0]
                    if creation:
                        self.assertIn('--dry-run=server', args)
                        request = json.loads(kwargs['stdin'])
                        self.assertEqual(request['metadata']['namespace'], security.NAMESPACE)
                        self.assertTrue(request['spec']['containers'][0]['securityContext']['privileged'])
                    user = 'other-user' if outcome == 'different-user' else identity
                    error = 'Error from server (Forbidden): User "'+user+'" cannot '+('create' if creation else 'list')+' resource "'+resource+'"'
                    return SimpleNamespace(returncode=0 if outcome == 'allowed' or creation and outcome == 'create-allowed' else 1,
                        stdout='', stderr='connection refused' if outcome == 'network' or creation and outcome == 'create-network' else error)
                fixture.kubectl = Mock(side_effect=kubectl)
                with redirect_stdout(io.StringIO()):
                    if outcome == 'forbidden': rotation.verify_rbac({'namespace': 'business'}, create)
                    else:
                        with self.assertRaisesRegex(ValueError, 'native authorization'): rotation.verify_rbac({'namespace': 'business'}, create)
                create.assert_called_once()
                if outcome == 'forbidden': self.assertEqual(fixture.kubectl.call_count, 5)

    def test_new_ca_business_proof_detaches_its_client_even_when_data_or_readiness_fails(self):
        for failure in ('none', 'data', 'ready', 'client-uid'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, secret, _volume, _instances, row = self.fixture(directory)
                secret['data'] = row['materials']['new-only']
                fixture.scoped_cleanup = lab.scoped_cleanup
                fixture.client_pod = Mock(return_value={'kind': 'Pod'})
                fixture.payload_program = Mock(return_value='read-original-bytes')
                fixture.replica_topology = Mock(return_value=True)
                fixture.load_helper = Mock(return_value=SimpleNamespace(ready=lambda pod: bool(pod)))
                fixture.kubectl = Mock(return_value=SimpleNamespace(stdout='wrong' if failure == 'data' else 'confirmed'))
                fixture.get = Mock(return_value={'metadata': {'uid': 'foreign' if failure == 'client-uid' else 'client-uid'}})
                rotation.read = Mock(return_value=(row, secret))
                events = []
                def wait(label, _timeout, check):
                    events.append(label)
                    if failure == 'ready' and label == 'security-new-ca-client-ready': raise ValueError('ready')
                    self.assertTrue(check())
                fixture.wait = Mock(side_effect=wait)
                rotation.detach_new_data = Mock(side_effect=lambda: events.append('detach-owned-client'))
                with redirect_stdout(io.StringIO()):
                    if failure == 'none': rotation.verify_new_data({'namespace': 'business'}, Mock(
                        return_value={'metadata': {'uid': 'client-uid'}}), 'payload', 'confirmed')
                    else:
                        with self.assertRaises(ValueError): rotation.verify_new_data({'namespace': 'business'}, Mock(
                            return_value={'metadata': {'uid': 'client-uid'}}), 'payload', 'confirmed')
                self.assertTrue(row['new_data_client'])
                self.assertEqual(events[-1], 'detach-owned-client')
                rotation.detach_new_data.assert_called_once()

    def test_interrupted_new_ca_client_cleanup_is_uid_bound_and_stops_before_trust_changes(self):
        for defect in ('none', 'namespace', 'claim', 'client-uid', 'untracked', 'symlink', 'active'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
                row['new_data_client'] = True; fixture.write_private(rotation.record, row)
                journal = {'namespace': 'business', 'namespace_uid': 'business-uid',
                    'volume': 'pvc-owned', 'volume_uid': 'volume-uid',
                    'objects': {'PersistentVolumeClaim/data': 'claim-uid', 'Pod/security-new-ca': 'client-uid'}}
                if defect == 'namespace': journal['namespace_uid'] = 'foreign'
                if defect == 'claim': journal['objects']['PersistentVolumeClaim/data'] = 'foreign'
                if defect == 'untracked': journal['objects'].pop('Pod/security-new-ca')
                path = fixture.DIRECTORY/'longhorn-install-business.json'; path.write_text(json.dumps(journal))
                if defect == 'symlink':
                    original = path.with_suffix('.original'); path.rename(original); path.symlink_to(original)
                pod = {'metadata': {'uid': 'foreign' if defect == 'client-uid' else 'client-uid'}}
                original_get = fixture.get.side_effect
                fixture.get.side_effect = lambda kind, *args: (None if fixture.remove.called else pod) if kind == 'pod' else original_get(kind, *args)
                fixture.remove = Mock(); fixture.check_business_graph = Mock()
                fixture.wait = Mock(side_effect=lambda label, timeout, check: self.assertTrue(check()))
                rotation.detached = Mock(return_value=True); rotation.components = Mock(return_value={})
                rotation.empty = Mock(return_value=defect != 'active')
                if defect == 'none':
                    rotation.detach_new_data()
                    self.assertFalse(rotation.read()[0]['new_data_client'])
                else:
                    with self.assertRaises((ValueError, AssertionError)): rotation.detach_new_data()
                    self.assertTrue(rotation.read()[0]['new_data_client'])
                self.assertEqual(fixture.remove.call_count, int(defect in ('none', 'active')))
                fixture.kubectl.assert_not_called()

    def test_failed_new_ca_detach_preserves_escrow_and_blocks_certificate_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            rotation, fixture, secret, _volume, _instances, row = self.fixture(directory)
            row['new_data_client'] = True; row['mutation_started'] = True
            secret['data'] = row['materials']['new-only']; fixture.write_private(rotation.record, row)
            rotation.detach_new_data = Mock(side_effect=ValueError('own client could not detach'))
            rotation.restart = Mock()
            with self.assertRaisesRegex(ValueError, 'could not detach'): rotation.restore()
            rotation.restart.assert_not_called()
            self.assertTrue(rotation.record.exists())

    def test_rotation_orchestration_stops_at_first_fault_and_restores_before_probe_cleanup(self):
        from contextlib import contextmanager
        for failure in ('none', 'preflight', 'old-bundle', 'new-bundle', 'new-only', 'new-data', 'restore-failure'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                rotation, fixture, _secret, _volume, _instances, row = self.fixture(directory)
                rotation.record.unlink()
                rotation.identity = Mock(return_value=(fixture.owner.return_value, _secret))
                events = []
                @contextmanager
                def cleanup(action):
                    try: yield
                    finally: action()
                fixture.scoped_cleanup = cleanup
                fixture.wait = Mock(side_effect=lambda label, timeout, check: self.assertTrue(check()))
                fixture.get = Mock(return_value={'metadata': {'uid': 'probe-uid'}})
                fixture.remove = Mock()
                network = Mock(); network.record = Path(directory)/'network-record'
                network.create_system.return_value = {'metadata': {'uid': 'probe-uid'}}
                network.cleanup.side_effect = lambda: events.append('probe-cleanup')
                helper = SimpleNamespace(Probe=Mock(return_value=network), ready=Mock(return_value=True),
                    policy_identity=Mock(return_value={'longhorn-manager': {}, 'instance-manager': {}}))
                fixture.load_helper = Mock(return_value=helper)
                fixture.tls_fixture = Mock(return_value={}); fixture.client_pod = Mock(return_value={})
                fixture.payload_program = Mock(return_value='verify-original-data')
                fixture.kubectl = Mock(return_value=SimpleNamespace(stdout='confirmed'))
                fixture.replica_topology = Mock(return_value=True)
                rotation.begin = Mock(return_value=row)
                rotation.mounts_ready = Mock(side_effect=ValueError('preflight') if failure == 'preflight' else None,
                                             return_value=True)
                rotation.verify_rbac = Mock(); rotation.verify_tls = Mock(); rotation.verify_plaintext = Mock()
                rotation.verify_new_data = Mock(side_effect=ValueError('new-data') if failure == 'new-data' else None)
                rotation.detached = Mock(return_value=True); rotation.empty = Mock(return_value=True)
                rotation.components = Mock(return_value={})
                def restore():
                    events.append('restore-original-tls')
                    if failure == 'restore-failure':
                        rotation.record.touch()
                        raise ValueError('restore-failure')
                rotation.restore = Mock(side_effect=restore)
                def restart(stage):
                    events.append(stage)
                    if stage == failure: raise ValueError(stage)
                rotation.restart = Mock(side_effect=restart)
                def get(kind, name='', namespace=''):
                    return {'metadata': {'uid': 'writer-uid' if name == 'writer' else 'probe-uid'}} if name != 'writer' or not fixture.remove.called else None
                fixture.get.side_effect = get
                journal = {'namespace': 'business', 'volume': 'pvc-owned', 'volume_uid': 'volume-uid',
                           'objects': {'Pod/writer': 'writer-uid'}}
                with redirect_stdout(io.StringIO()):
                    if failure == 'none': self.assertEqual(rotation.verify(journal, Mock(), 'payload', 'confirmed'), 'security-recovered')
                    else:
                        with self.assertRaisesRegex(ValueError, failure): rotation.verify(journal, Mock(), 'payload', 'confirmed')
                stages = ['old-bundle', 'new-bundle', 'new-only']
                completed = stages if failure in ('none', 'new-data', 'restore-failure') else [] if failure == 'preflight' else stages[:stages.index(failure)+1]
                self.assertEqual(events, completed+['restore-original-tls']+
                                 ([] if failure == 'restore-failure' else ['probe-cleanup']))
                if failure == 'restore-failure': self.assertTrue(rotation.record.exists())
                self.assertEqual(fixture.remove.call_count, int(failure != 'preflight'))
                self.assertEqual(rotation.verify_new_data.call_count, int(failure in ('none', 'new-data', 'restore-failure')))


if __name__ == '__main__':
    unittest.main()
