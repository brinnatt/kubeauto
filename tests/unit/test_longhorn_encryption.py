"""Closed-mapping preconditions, private key restoration and native refusal attribution."""
import base64
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('encryption', ROOT/'tests/helpers/longhorn-encryption.py')
encryption = importlib.util.module_from_spec(spec); spec.loader.exec_module(encryption)


class EncryptionContracts(unittest.TestCase):
    def fixture(self, directory):
        key = {'metadata': {'uid': 'key-uid', 'resourceVersion': '1'}, 'data': {
            'CRYPTO_KEY_PROVIDER': base64.b64encode(b'secret').decode(),
            'CRYPTO_KEY_VALUE': base64.b64encode(b'original-private-key').decode()}}
        volume = {'metadata': {'name': 'pvc-owned', 'uid': 'volume-uid'},
                  'spec': {'encrypted': True, 'dataEngine': 'v1'}, 'status': {'state': 'detached'}}
        namespace = {'metadata': {'uid': 'ns-uid'}}
        state = {'run_id': 'run-1', 'namespace_uid': 'ns-uid', 'encryption_uid': 'key-uid'}
        volumes = [volume]
        def get(kind, *_args, **_kwargs):
            return copy.deepcopy({'secret': key, 'namespace': namespace, 'volumes.longhorn.io': {'items': volumes}}[kind])
        def private(path, row):
            path.write_text(json.dumps(row)); path.chmod(0o600)
        def kubectl(*args, **kwargs):
            self.assertEqual(args, ('-n', 'longhorn-system', 'patch', 'secret', 'longhorn-volume-key',
                                    '--type=json', '--patch-file=/dev/stdin'))
            operations = json.loads(kwargs['stdin'])
            self.assertEqual(operations[:3], [
                {'op': 'test', 'path': '/metadata/uid', 'value': key['metadata']['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': key['metadata']['resourceVersion']},
                {'op': 'test', 'path': '/data', 'value': key['data']}])
            key['data'] = operations[3]['value']
            key['metadata']['resourceVersion'] = str(int(key['metadata']['resourceVersion'])+1)
        lab = SimpleNamespace(DIRECTORY=Path(directory), get=Mock(side_effect=get), owner=Mock(return_value=state),
                              kubectl=Mock(side_effect=kubectl), write_private=private)
        probe = encryption.Probe(lab); probe.closed = Mock(return_value=True)
        return probe, lab, key, volume, volumes, namespace, state

    def test_wrong_and_missing_key_restore_original_data_and_uid_without_logging_material(self):
        for variant in ('wrong', 'missing'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                probe, lab, key, *_ = self.fixture(directory)
                original = copy.deepcopy(key['data'])
                probe.activate({'volume': 'pvc-owned', 'volume_uid': 'volume-uid'}, variant)
                self.assertNotEqual(key['data'], original)
                self.assertEqual('CRYPTO_KEY_VALUE' in key['data'], variant == 'wrong')
                self.assertEqual(probe.record.stat().st_mode & 0o777, 0o600)
                with redirect_stdout(io.StringIO()) as output:
                    probe.restore(); probe.restore()
                self.assertEqual(key['data'], original)
                self.assertEqual(key['metadata']['uid'], 'key-uid')
                self.assertFalse(probe.record.exists())
                self.assertNotIn(original['CRYPTO_KEY_VALUE'], output.getvalue())
                self.assertEqual(lab.kubectl.call_count, 2)

    def test_activation_refuses_other_consumers_open_mapping_and_changed_identity_before_patch(self):
        for defect in ('foreign-volume', 'uid', 'namespace', 'attached', 'plain', 'v2', 'open', 'missing'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, key, volume, volumes, ns, _state = self.fixture(directory)
                if defect == 'foreign-volume': volumes.append({'metadata': {'name': 'other', 'uid': 'foreign'}})
                if defect == 'uid': key['metadata']['uid'] = 'replaced'
                if defect == 'namespace': ns['metadata']['uid'] = 'replaced'
                if defect == 'attached': volume['status']['state'] = 'attached'
                if defect == 'plain': volume['spec']['encrypted'] = False
                if defect == 'v2': volume['spec']['dataEngine'] = 'v2'
                if defect == 'open': probe.closed.return_value = False
                if defect == 'missing': key['data'].pop('CRYPTO_KEY_VALUE')
                with self.assertRaises(ValueError):
                    probe.activate({'volume': 'pvc-owned', 'volume_uid': 'volume-uid'}, 'wrong')
                lab.kubectl.assert_not_called()
                self.assertFalse(probe.record.exists())

    def test_recovery_rejects_foreign_key_material_escrow_and_permissions(self):
        for defect in ('foreign-data', 'key-uid', 'run', 'digest', 'public', 'symlink', 'not-applied'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                probe, lab, key, _volume, _volumes, _ns, state = self.fixture(directory)
                original = copy.deepcopy(key['data'])
                probe.activate({'volume': 'pvc-owned', 'volume_uid': 'volume-uid'}, 'wrong')
                lab.kubectl.reset_mock()
                if defect == 'foreign-data': key['data']['CRYPTO_KEY_VALUE'] = 'Zm9yZWlnbg=='
                if defect == 'key-uid': key['metadata']['uid'] = 'replaced'
                if defect == 'run': state['run_id'] = 'foreign'
                if defect == 'digest':
                    row = json.loads(probe.record.read_text()); row['original']['CRYPTO_KEY_VALUE'] = 'bad'
                    probe.record.write_text(json.dumps(row))
                if defect == 'public': probe.record.chmod(0o644)
                if defect == 'symlink':
                    target = Path(directory)/'replacement'; probe.record.rename(target); probe.record.symlink_to(target)
                if defect == 'not-applied': key['data'] = original
                with redirect_stdout(io.StringIO()):
                    if defect == 'not-applied':
                        probe.restore(); self.assertFalse(probe.record.exists())
                    else:
                        with self.assertRaises(ValueError): probe.restore()
                        self.assertTrue(probe.record.exists())
                lab.kubectl.assert_not_called()

    def test_only_native_failed_mount_for_original_pod_proves_refusal(self):
        for variant, message in (('wrong', 'No key available with this passphrase'),
                                 ('missing', 'missing passphrase for encrypted volume pvc-owned')):
            event = {'involvedObject': {'uid': 'pod-uid'}, 'reason': 'FailedMount', 'message': message}
            self.assertTrue(encryption.refusal_event([event], 'pod-uid', variant))
            self.assertFalse(encryption.refusal_event([event], 'other-pod', variant))
            event['reason'] = 'FailedScheduling'
            self.assertFalse(encryption.refusal_event([event], 'pod-uid', variant))
            event.update(reason='FailedMount', message='network timeout')
            self.assertFalse(encryption.refusal_event([event], 'pod-uid', variant))

    def test_native_refusal_reason_is_bound_to_volume_mode_and_original_pod(self):
        for block, reason in ((False, 'FailedMount'), (True, 'FailedMapVolume')):
            for variant, message in (('wrong', 'No key available with this passphrase'),
                                     ('missing', 'missing passphrase for encrypted volume pvc-owned')):
                event = {'involvedObject': {'uid': 'pod-uid'}, 'reason': reason, 'message': message}
                with self.subTest(block=block, variant=variant):
                    self.assertTrue(encryption.refusal_event([event], 'pod-uid', variant, block=block))
                    self.assertFalse(encryption.refusal_event([event], 'foreign-pod', variant, block=block))
                    self.assertFalse(encryption.refusal_event([event], 'pod-uid', variant, block=not block))
                    for invalid in ('FailedScheduling', 'FailedAttachVolume', 'Failed'):
                        bad = dict(event, reason=invalid)
                        self.assertFalse(encryption.refusal_event([bad], 'pod-uid', variant, block=block))
                    self.assertFalse(encryption.refusal_event([dict(event, message='network timeout')],
                                                              'pod-uid', variant, block=block))

    def test_device_probe_is_read_only_and_checks_luks_header_mapping_and_size(self):
        for defect in ('none', 'plain', 'mapping', 'size', 'regular', 'name'):
            def run(args, **kwargs):
                values = {'blkid': 'ext4' if defect == 'plain' else 'crypto_LUKS',
                          'cryptsetup': ('device: /dev/foreign' if defect == 'mapping' else 'device: /dev/longhorn/pvc-owned')
                          if args[1] == 'status' else 'luks-uuid',
                          'blockdev': str(2*1024**3 + (0 if '/mapper/' in args[-1] or defect == 'size' else 16*1024**2))}
                return SimpleNamespace(stdout=values[args[0]])
            def device_stat(path):
                return SimpleNamespace(st_mode=stat.S_IFREG if defect == 'regular' else stat.S_IFBLK,
                                       st_rdev=os.makedev(8, 16 if str(path) == '/dev/foreign' else 0))
            with self.subTest(defect=defect), patch.object(Path, 'stat', device_stat), patch.object(Path, 'resolve', lambda p: p), \
                    patch.object(encryption.subprocess, 'run', side_effect=run) as command:
                if defect == 'none':
                    row = encryption.device_probe('pvc-owned', True)
                    self.assertEqual(row['mapped_bytes'], 2*1024**3)
                else:
                    with self.assertRaises(ValueError): encryption.device_probe('../foreign' if defect == 'name' else 'pvc-owned', True)
                self.assertTrue(all(call.args[0][1] in ('-s', 'status', 'luksUUID', '--getsize64') for call in command.call_args_list))

    def test_mapping_identity_uses_block_device_number_for_distinct_device_nodes(self):
        # Longhorn duplicates the iSCSI device node with mknod; these paths
        # have different names/inodes while referring to the same device.
        for defect in ('none', 'foreign-device', 'regular-device'):
            def device_stat(path):
                return SimpleNamespace(st_mode=stat.S_IFREG if defect == 'regular-device' and str(path) == '/dev/sda' else stat.S_IFBLK,
                    st_rdev=os.makedev(8, 16 if defect == 'foreign-device' and str(path) == '/dev/sda' else 0))
            def command(args, **kwargs):
                if args[0] == 'blkid': value = 'crypto_LUKS'
                elif args[0] == 'cryptsetup': value = 'device: /dev/sda' if args[1] == 'status' else 'original-uuid'
                else: value = str(2*1024**3 + (0 if '/mapper/' in args[-1] else encryption.HEADER))
                return SimpleNamespace(stdout=value)
            with self.subTest(defect=defect), patch.object(Path, 'stat', device_stat), \
                    patch.object(Path, 'resolve', lambda path: path), patch.object(encryption.subprocess, 'run', side_effect=command):
                if defect == 'none':
                    self.assertEqual(encryption.device_probe('pvc-owned', True)['luks_uuid'], 'original-uuid')
                else:
                    with self.assertRaises(ValueError): encryption.device_probe('pvc-owned', True)

    def test_v2_probe_uses_encrypted_suffix_and_does_not_confuse_frontend_mapper_with_luks(self):
        def device_stat(path):
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(253, 1))
        def command(args, **kwargs):
            if args[0] == 'blkid': value = 'crypto_LUKS'
            elif args[0] == 'cryptsetup':
                value = 'device: /dev/mapper/pvc-owned' if args[1] == 'status' else 'v2-luks-uuid'
            else: value = str(2*1024**3 + (0 if args[-1].endswith('-encrypted') else encryption.HEADER))
            return SimpleNamespace(stdout=value)
        with patch.object(Path, 'stat', device_stat), patch.object(encryption.subprocess, 'run', side_effect=command) as run:
            row = encryption.device_probe('pvc-owned', True, data_engine='v2')
        self.assertEqual(row, {'luks_uuid': 'v2-luks-uuid', 'mapped_bytes': 2*1024**3,
                               'raw_bytes': 2*1024**3+encryption.HEADER})
        self.assertIn(('cryptsetup', 'status', '/dev/mapper/pvc-owned-encrypted'), [c.args[0] for c in run.call_args_list])
        with patch.object(Path, 'exists', lambda path: str(path) == '/dev/mapper/pvc-owned'):
            self.assertEqual(encryption.device_probe('pvc-owned', False, data_engine='v2'), {'closed': True})
            self.assertEqual(encryption.device_probe('pvc-owned', False), {'closed': False})
        with patch.object(encryption.subprocess, 'run') as run, self.assertRaises(ValueError):
            encryption.device_probe('pvc-owned', True, data_engine='unknown')
        run.assert_not_called()

    def test_v2_key_refusal_requires_explicit_engine_selection_and_original_secret_escrow(self):
        with tempfile.TemporaryDirectory() as directory:
            _, lab, key, volume, *_ = self.fixture(directory)
            volume['spec']['dataEngine'] = 'v2'
            original = copy.deepcopy(key['data'])
            probe = encryption.Probe(lab, data_engine='v2'); probe.closed = Mock(return_value=True)
            probe.activate({'volume': 'pvc-owned', 'volume_uid': 'volume-uid'}, 'wrong')
            self.assertNotEqual(key['data'], original)
            probe.restore(); self.assertEqual(key['data'], original)
            volume['spec']['dataEngine'] = 'v1'; lab.kubectl.reset_mock()
            with self.assertRaises(ValueError): probe.activate({'volume': 'pvc-owned', 'volume_uid': 'volume-uid'}, 'missing')
            lab.kubectl.assert_not_called()
            with self.assertRaises(ValueError): encryption.Probe(lab, data_engine='unknown')

    def test_encrypted_backup_checks_stored_header_capacity_and_encryption_label(self):
        for defect in ('none', 'label', 'volume', 'size', 'receipt-size'):
            size = 2*1024**3; raw_size = size + encryption.HEADER
            store = SimpleNamespace(backup_receipt=Mock(return_value={'size': raw_size if defect != 'receipt-size' else size}),
                receipt=Mock(return_value={'bucket': 'owned'}), mc=Mock(return_value=json.dumps({
                    'Name': 'foreign' if defect == 'volume' else 'pvc-owned',
                    'Size': str(size if defect == 'size' else raw_size),
                    'Labels': {'volume-encrypted': 'disabled' if defect == 'label' else 'enabled'}})))
            lab = SimpleNamespace(load_helper=Mock(return_value=SimpleNamespace(volume_prefix=lambda name: 'prefix/'+name+'/')))
            if defect == 'none':
                result = encryption.backup_receipt(lab, store, {'volume': 'pvc-owned'}, 'backup', size)
                self.assertEqual((result['size'], result['stored_size'], result['encrypted']), (size, raw_size, True))
                store.backup_receipt.assert_called_once_with('pvc-owned', 'backup', raw_size)
            else:
                with self.assertRaises(ValueError): encryption.backup_receipt(lab, store, {'volume': 'pvc-owned'}, 'backup', size)

    def test_restored_backup_requires_original_luks_uuid_encrypted_flag_and_key_reference(self):
        for defect in ('none', 'plain', 'key', 'volume', 'pv', 'header'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                device = {'luks_uuid': 'original-luks-uuid', 'mapped_bytes': 2*1024**3,
                          'raw_bytes': 2*1024**3+encryption.HEADER}
                volume = {'metadata': {'uid': 'new-native'}, 'spec': {'encrypted': defect != 'plain'}}
                pv = {'metadata': {'uid': 'new-pv-uid'}, 'spec': {'csi': {'nodeStageSecretRef': {
                    'name': 'foreign' if defect == 'key' else 'longhorn-volume-key', 'namespace': 'longhorn-system'}}}}
                if defect == 'volume': volume['metadata']['uid'] = 'replaced'
                if defect == 'pv': pv['metadata']['uid'] = 'replaced'
                after = dict(device, luks_uuid='reformatted') if defect == 'header' else device
                lab = SimpleNamespace(DIRECTORY=Path(directory), WORKERS=('worker0', 'worker1'),
                    get=Mock(side_effect=lambda kind, *_args, **_kwargs: volume if kind == 'volumes.longhorn.io' else pv))
                with patch.object(encryption.Probe, 'host_probe', return_value=after), redirect_stdout(io.StringIO()):
                    args = (lab, {'encrypted_backup_device': device},
                            {'volume': 'new-volume', 'volume_uid': 'new-native', 'pv': 'new-pv', 'pv_uid': 'new-pv-uid'}, 'restored-client')
                    if defect == 'none': encryption.verify_restored(*args)
                    else:
                        with self.assertRaises(ValueError): encryption.verify_restored(*args)

    def test_full_probe_orders_detach_refusal_restore_and_data_verification_for_both_modes(self):
        for block in (False, True):
            for defect in ('none', 'unexpected-mount', 'foreign-event', 'data'):
                with self.subTest(block=block, defect=defect), tempfile.TemporaryDirectory() as directory:
                    probe, lab, key, *_ = self.fixture(directory)
                    original = copy.deepcopy(key['data'])
                    lab.WORKERS = ('192.168.122.212', '192.168.122.165', '192.168.122.238')
                    journal = {'volume': 'pvc-owned', 'volume_uid': 'volume-uid', 'namespace': 'owned', 'objects': {}}
                    lab.scoped_cleanup = __import__('contextlib').contextmanager(lambda cleanup: self.cleanup_context(cleanup))
                    def pod(namespace, name, claim, node, **kwargs):
                        self.assertEqual(kwargs, {'block': block})
                        return {'metadata': {'name': name}}
                    lab.client_pod = Mock(side_effect=pod)
                    def create(obj):
                        name = obj['metadata']['name']; obj['metadata']['uid'] = name+'-uid'
                        journal['objects']['Pod/'+name] = name+'-uid'
                        return obj
                    native_get = lab.get.side_effect
                    def get(kind, *args, **kwargs):
                        if kind == 'events':
                            return {'items': [{'involvedObject': {'uid': 'foreign' if defect == 'foreign-event' else 'key-'+variant+'-uid'},
                                'reason': 'FailedMapVolume' if block else 'FailedMount', 'message': message} for variant, message in (
                                    ('wrong', 'No key available with this passphrase'),
                                    ('missing', 'missing passphrase for encrypted volume'))]}
                        return native_get(kind, *args, **kwargs)
                    lab.get.side_effect = get
                    def wait(label, timeout, condition):
                        if not condition(): raise TimeoutError(label)
                    lab.wait = Mock(side_effect=wait)
                    native_patch = lab.kubectl.side_effect
                    def kubectl(*args, **kwargs):
                        if 'patch' in args: return native_patch(*args, **kwargs)
                        return SimpleNamespace(stdout='wrong' if defect == 'data' else 'confirmed-digest')
                    lab.kubectl.side_effect = kubectl
                    lab.payload_program = Mock(return_value='read-data')
                    lab.replica_topology = Mock(return_value={'healthy': True})
                    probe.detach = Mock()
                    probe.host_probe = Mock(return_value={'luks_uuid': 'same'})
                    probe.attached = Mock(side_effect=lambda _j, client: client.endswith('-recovered') or defect == 'unexpected-mount')
                    with redirect_stdout(io.StringIO()):
                        if defect == 'none':
                            self.assertEqual(probe.verify(journal, create, 'payload', 'confirmed-digest', block=block), 'key-missing-recovered')
                            self.assertEqual([c.args[1] for c in probe.detach.call_args_list],
                                             ['writer', 'key-wrong', 'key-wrong-recovered', 'key-missing'])
                            self.assertTrue(all(c.kwargs == {'block': block, 'read_only': True}
                                                for c in lab.payload_program.call_args_list))
                        else:
                            with self.assertRaises((ValueError, TimeoutError)):
                                probe.verify(journal, create, 'payload', 'confirmed-digest', block=block)
                    self.assertEqual(key['data'], original)
                    self.assertFalse(probe.record.exists())

    @staticmethod
    def cleanup_context(cleanup):
        try:
            yield
        finally:
            cleanup()


if __name__ == '__main__':
    unittest.main()
