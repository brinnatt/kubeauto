"""Backup refusal fixture identity, atomic restoration and negative-result contracts."""
import base64
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('backup_refusals', ROOT / 'tests/helpers/longhorn-backups.py')
backups = importlib.util.module_from_spec(spec); spec.loader.exec_module(backups)
spec = importlib.util.spec_from_file_location('backup_refusal_lab', ROOT / 'tests/helpers/longhorn-lab.py')
lab_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab_module)


class BackupRefusalContracts(unittest.TestCase):
    def fixture(self, directory):
        data = {key: base64.b64encode(value.encode()).decode() for key, value in {
            'AWS_ACCESS_KEY_ID': 'fixture-user', 'AWS_SECRET_ACCESS_KEY': 'fixture-private-key',
            'AWS_CERT': 'fixture-original-ca', 'AWS_ENDPOINTS': 'https://192.168.122.135:23523'}.items()}
        original_spec = {'backupTargetURL': '', 'credentialSecret': '', 'pollInterval': '5m0s'}
        binding = {'target_uid': 'target-uid', 'namespace_uid': 'namespace-uid', 'secret_uid': 'secret-uid',
                   'secret_name': 'fixture', 'original_spec': original_spec,
                   'fixture_spec': dict(original_spec, backupTargetURL='s3://fixture@us-east-1/', credentialSecret='fixture'),
                   'secret_digest': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
        row = {'run_id': 'run-id', 'name': 'fixture', 'bucket': 'fixture', 'product_binding': binding}
        objects = {('namespace', 'longhorn-system'): {'metadata': {'uid': 'namespace-uid'}},
                   ('backuptargets.longhorn.io', 'default'): {'metadata': {'uid': 'target-uid', 'resourceVersion': '1'},
                    'spec': copy.deepcopy(original_spec), 'status': {'available': False}},
                   ('secret', 'fixture'): {'metadata': {'uid': 'secret-uid', 'resourceVersion': '1',
                    'labels': {'kubeauto.io/longhorn-backup-run': 'run-id'}}, 'data': data}}
        for kind in backups.BACKUP_RESOURCES:
            objects[kind, ''] = {'items': []}
        def get(kind, name='', *_args, **_kwargs):
            return copy.deepcopy(objects.get((kind, name)))
        def kubectl(*args, stdin=None, check=True):
            kind, name = args[3:5]; obj = objects[kind, name]
            for operation in json.loads(stdin):
                parts = operation['path'].strip('/').split('/'); parent = obj
                for part in parts[:-1]: parent = parent[part]
                key = parts[-1]
                if operation['op'] == 'test':
                    if parent.get(key) != operation['value']:
                        if not check:
                            return SimpleNamespace(returncode=1, stdout='', stderr='The request is invalid: the server rejected our request due to an error in our request\n')
                        raise ValueError('API JSON test failed')
                elif operation['op'] == 'replace':
                    parent[key] = copy.deepcopy(operation['value'])
                else:
                    raise AssertionError('unexpected mutation')
            obj['metadata']['resourceVersion'] = str(int(obj['metadata']['resourceVersion']) + 1)
            return SimpleNamespace(returncode=0, stdout='patched', stderr='')
        def write_private(path, value):
            path.write_text(json.dumps(value)); path.chmod(0o600)
        lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=('192.168.122.135', '192.168.122.212'),
                              get=Mock(side_effect=get), kubectl=Mock(side_effect=kubectl), owner=lambda: {'run_id': 'run-id'},
                              remote=Mock(), write_private=write_private, scoped_cleanup=lab_module.scoped_cleanup,
                              tls_fixture=Mock(return_value={'ca.crt': 'unrelated valid PEM CA\n'}),
                              wait=Mock(side_effect=lambda _name, _timeout, check: self.assertTrue(check())))
        store = SimpleNamespace(endpoints=backups.V1_ENDPOINTS, receipt=lambda: copy.deepcopy(row),
                                mc=Mock(return_value='{"status":"success","key":"qualification-canary"}\n'))
        return backups.Refusals(lab, store), lab, store, objects, row

    def test_v2_closed_endpoint_refusal_stays_within_its_own_three_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            fault, lab, store, objects, _row = self.fixture(directory)
            store.endpoints = backups.V2_ENDPOINTS
            lab.NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
            fault.activate('endpoint')
            value = base64.b64decode(objects['secret', 'fixture']['data']['AWS_ENDPOINTS']).decode()
            self.assertEqual(value, 'https://192.168.47.146:23625')
            self.assertEqual([c.args[0] for c in lab.remote.call_args_list], list(lab.NODES))
            for call in lab.remote.call_args_list:
                self.assertIn("('192.168.47.146',23625)", call.args[1][-1])
                self.assertNotIn('192.168.122.135', call.args[1][-1])
            with redirect_stdout(io.StringIO()): fault.restore()

    def test_each_fault_changes_one_field_and_restores_without_logging_credential_data(self):
        for kind, field in (('credential', 'AWS_SECRET_ACCESS_KEY'), ('ca', 'AWS_CERT'), ('endpoint', 'AWS_ENDPOINTS')):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, _row = self.fixture(directory)
                original = copy.deepcopy(objects['secret', 'fixture'])
                with redirect_stdout(io.StringIO()) as output:
                    fault.activate(kind)
                    self.assertEqual(fault.record.stat().st_mode & 0o777, 0o600)
                    changed = objects['secret', 'fixture']['data']
                    self.assertEqual({key for key in changed if changed[key] != original['data'][key]}, {field})
                    fault.restore()
                self.assertEqual(objects['secret', 'fixture']['data'], original['data'])
                self.assertEqual(objects['secret', 'fixture']['metadata']['uid'], original['metadata']['uid'])
                self.assertFalse(fault.record.exists())
                self.assertEqual(lab.remote.call_count, len(lab.NODES) if kind == 'endpoint' else 0)
                for value in original['data'].values(): self.assertNotIn(value, output.getvalue())
                for call in lab.kubectl.call_args_list:
                    self.assertIn('--patch-file=/dev/stdin', call.args)
                    self.assertNotIn('fixture-private-key', ' '.join(call.args))

    def test_identity_empty_target_and_original_digest_refusals_precede_any_mutation(self):
        for kind in ('namespace', 'target', 'secret', 'label', 'spec', 'run', 'data', 'available', 'nonempty'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, row = self.fixture(directory)
                if kind == 'namespace': objects['namespace', 'longhorn-system']['metadata']['uid'] = 'foreign'
                if kind == 'target': objects['backuptargets.longhorn.io', 'default']['metadata']['uid'] = 'foreign'
                if kind == 'secret': objects['secret', 'fixture']['metadata']['uid'] = 'foreign'
                if kind == 'label': objects['secret', 'fixture']['metadata']['labels'] = {}
                if kind == 'spec': objects['backuptargets.longhorn.io', 'default']['spec']['credentialSecret'] = 'foreign'
                if kind == 'run': row['run_id'] = 'foreign'
                if kind == 'data': objects['secret', 'fixture']['data']['AWS_CERT'] = 'foreign'
                if kind == 'available': objects['backuptargets.longhorn.io', 'default']['status']['available'] = True
                if kind == 'nonempty': objects['backups.longhorn.io', '']['items'] = [{}]
                with self.assertRaises(ValueError): fault.activate('credential')
                lab.kubectl.assert_not_called()
                self.assertFalse(fault.record.exists())

    def test_lost_patch_response_restores_original_data_from_durable_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            fault, lab, _store, objects, _row = self.fixture(directory)
            original = copy.deepcopy(objects['secret', 'fixture']['data'])
            actual_patch = lab.kubectl.side_effect
            def lost(*args, **kwargs):
                actual_patch(*args, **kwargs)
                raise RuntimeError('API response lost after commit')
            lab.kubectl.side_effect = lost
            with self.assertRaisesRegex(RuntimeError, 'response lost'): fault.activate('credential')
            self.assertNotEqual(objects['secret', 'fixture']['data'], original)
            self.assertTrue(fault.record.exists())
            lab.kubectl.side_effect = actual_patch
            with redirect_stdout(io.StringIO()): fault.restore()
            self.assertEqual(objects['secret', 'fixture']['data'], original)
            self.assertFalse(fault.record.exists())

    def test_foreign_replacement_data_journal_or_symlink_never_authorizes_restoration(self):
        for kind in ('replacement', 'data', 'journal', 'original', 'symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, _row = self.fixture(directory)
                fault.activate('credential'); lab.kubectl.reset_mock()
                if kind == 'replacement': objects['secret', 'fixture']['metadata']['uid'] = 'replacement'
                if kind == 'data': objects['secret', 'fixture']['data']['AWS_CERT'] = 'foreign'
                if kind in ('journal', 'original'):
                    receipt = json.loads(fault.record.read_text())
                    if kind == 'journal': receipt['run_id'] = 'foreign'
                    else: receipt['original']['AWS_CERT'] = 'foreign'
                    fault.record.write_text(json.dumps(receipt))
                if kind == 'symlink':
                    actual = fault.record.with_suffix('.original'); fault.record.rename(actual); fault.record.symlink_to(actual)
                with self.assertRaises(ValueError): fault.restore()
                lab.kubectl.assert_not_called()

    def test_target_disconnect_preserves_uid_sync_and_rejects_any_backup_consumer(self):
        for nonempty in (False, True):
            with self.subTest(nonempty=nonempty), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, row = self.fixture(directory)
                target = objects['backuptargets.longhorn.io', 'default']
                target['spec'] = dict(row['product_binding']['fixture_spec'], syncRequestedAt='2026-10-19T00:00:00Z')
                if nonempty:
                    objects['backups.longhorn.io', '']['items'] = [{}]
                    with self.assertRaisesRegex(ValueError, 'nonempty'): fault.disconnect()
                    lab.kubectl.assert_not_called()
                else:
                    fault.disconnect()
                    self.assertEqual(target['spec']['syncRequestedAt'], '2026-10-19T00:00:00Z')
                    self.assertEqual(backups.target_configuration(target['spec']),
                                     backups.target_configuration(row['product_binding']['original_spec']))
                    self.assertEqual(target['metadata']['uid'], 'target-uid')
                    for call in lab.kubectl.call_args_list: self.assertNotIn('delete', call.args)

    def test_target_disconnect_retries_only_proven_status_race_and_keeps_current_native_sync(self):
        for defect in ('one-race', 'permanent-race', 'forbidden', 'unchanged-version', 'foreign-spec', 'new-consumer'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, row = self.fixture(directory)
                target = objects['backuptargets.longhorn.io', 'default']
                target['spec'] = dict(row['product_binding']['fixture_spec'], syncRequestedAt='original-sync')
                original = lab.kubectl.side_effect; calls = []
                def race(*args, **kwargs):
                    calls.append(args)
                    if len(calls) == 1 or defect == 'permanent-race':
                        target['metadata']['resourceVersion'] = str(int(target['metadata']['resourceVersion'])+1)
                        target['spec']['syncRequestedAt'] = 'current-sync'
                        if defect == 'foreign-spec': target['spec']['credentialSecret'] = 'foreign'
                        if defect == 'new-consumer': objects['backups.longhorn.io', '']['items'] = [{}]
                        if defect == 'forbidden': return SimpleNamespace(returncode=1, stdout='', stderr='Forbidden')
                        if defect == 'unchanged-version':
                            target['metadata']['resourceVersion'] = json.loads(kwargs['stdin'])[1]['value']
                            return SimpleNamespace(returncode=1, stdout='', stderr='The request is invalid: the server rejected our request due to an error in our request\n')
                    return original(*args, **kwargs)
                lab.kubectl.side_effect = race
                if defect == 'one-race':
                    fault.disconnect()
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(target['spec']['syncRequestedAt'], 'current-sync')
                    self.assertEqual(target['spec']['backupTargetURL'], '')
                else:
                    with self.assertRaises(ValueError): fault.disconnect()
                    self.assertEqual(len(calls), 3 if defect == 'permanent-race' else 1)

    def verify_fixture(self, directory, defect=None):
        fault, lab, store, objects, row = self.fixture(directory)
        journal = {'volume': 'owned-volume'}
        configuration = {'longhorn_backup_target': 's3://fixture@us-east-1/', 'longhorn_backup_secret': 'fixture'}
        messages = {'AWS_SECRET_ACCESS_KEY': 'SignatureDoesNotMatch', 'AWS_CERT': 'x509 certificate signed by unknown authority',
                    'AWS_ENDPOINTS': 'dial tcp connection refused'}
        original = copy.deepcopy(objects['secret', 'fixture']['data'])
        def refused(_relay, requests, **kwargs):
            self.assertEqual(kwargs, {'backup_configuration': configuration, 'ready_timeout': 60,
                                      'expected_refusal': 'Longhorn timeout: backup-target-ready'})
            self.assertEqual([r['kind'] for r in requests], ['snapshot', 'backup'])
            target = objects['backuptargets.longhorn.io', 'default']
            target['spec'] = copy.deepcopy(row['product_binding']['fixture_spec'])
            changed = next(key for key in original if objects['secret', 'fixture']['data'][key] != original[key])
            target['status'] = {'available': False, 'conditions': [
                {'type': 'Unavailable', 'status': 'True', 'message': messages[changed]}]}
            if defect == 'wrong-reason': target['status']['conditions'][0]['message'] = 'unrelated controller error'
            if defect == 'available': target['status']['available'] = True
            if defect == 'completed': objects['backups.longhorn.io', requests[1]['name']] = {'status': {'state': 'Completed'}}
            if defect == 'global-consumer': objects['backupvolumes.longhorn.io', '']['items'] = [{}]
        lab.run_requests = Mock(side_effect=refused)
        if defect == 'objects': store.mc.return_value += '{"status":"success","key":"backupstore/volumes/data"}\n'
        if defect == 'listing': store.mc.return_value = '{"status":"error"}\n'
        if defect == 'empty': store.mc.return_value = ''
        return fault, lab, store, objects, journal, configuration, original

    def test_three_product_refusals_require_owning_error_absent_requests_and_empty_backup_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            fault, lab, _store, objects, journal, configuration, original = self.verify_fixture(directory)
            with redirect_stdout(io.StringIO()) as output:
                fault.verify(journal, 123, configuration)
            self.assertEqual(lab.run_requests.call_count, 3)
            self.assertEqual(output.getvalue().count('LONGHORN_BACKUP_REFUSAL_VERIFIED'), 3)
            self.assertEqual(objects['secret', 'fixture']['data'], original)
            self.assertEqual(objects['backuptargets.longhorn.io', 'default']['spec']['backupTargetURL'], '')
            self.assertFalse(fault.record.exists())
            self.assertEqual(len(journal['backups']), 3)

    def test_false_completion_wrong_reason_global_consumer_or_bad_object_listing_stops_at_first_case(self):
        for defect in ('wrong-reason', 'available', 'completed', 'global-consumer', 'objects', 'listing', 'empty'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                fault, lab, _store, objects, journal, configuration, original = self.verify_fixture(directory, defect)
                with redirect_stdout(io.StringIO()) as output:
                    with self.assertRaises(ValueError): fault.verify(journal, 123, configuration)
                self.assertEqual(lab.run_requests.call_count, 1)
                self.assertEqual(objects['secret', 'fixture']['data'], original)
                self.assertNotIn('LONGHORN_BACKUP_REFUSAL_VERIFIED', output.getvalue())
                # Leave the target connected with restored credentials so
                # runner-owned native backup cleanup can retire partial data.
                self.assertEqual(objects['backuptargets.longhorn.io', 'default']['spec']['backupTargetURL'], configuration['longhorn_backup_target'])

    def test_product_failure_restores_secret_before_outer_native_cleanup_and_preserves_first_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            fault, lab, _store, objects, journal, configuration, original = self.verify_fixture(directory)
            lab.run_requests.side_effect = RuntimeError('first product prerequisite failed')
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'first product prerequisite'):
                    fault.verify(journal, 123, configuration)
            self.assertEqual(objects['secret', 'fixture']['data'], original)
            self.assertFalse(fault.record.exists())

    def test_short_product_timeout_is_scoped_and_restores_the_entire_original_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yml'
            original = 'longhorn_requests: []\nlonghorn_ready_timeout: 1800\n'
            path.write_text(original)
            with patch.object(lab_module, 'DIRECTORY', Path(directory)), patch.object(lab_module, 'owner', return_value={'run_id': 'run-id'}), \
                    patch.object(lab_module, 'supervise_core') as product:
                for timeout, refusal in ((59, 'Longhorn timeout: backup-target-ready'), (60, 'different'), (1800, None)):
                    with self.assertRaises(ValueError): lab_module.run_requests(123, [], ready_timeout=timeout, expected_refusal=refusal)
                    self.assertEqual(path.read_text(), original)
                def observe(*_args, **_kwargs):
                    self.assertEqual(lab_module.yaml.safe_load(path.read_text())['longhorn_ready_timeout'], 60)
                product.side_effect = observe
                with redirect_stdout(io.StringIO()):
                    lab_module.run_requests(123, [], ready_timeout=60, expected_refusal='Longhorn timeout: backup-target-ready')
                product.assert_called_once()
                self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
