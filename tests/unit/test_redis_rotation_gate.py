"""Credential rotation ownership and first-failure contracts; no lab access."""
import copy
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_rotation', ROOT / 'tests/helpers/redis-rotation-gate.py')
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)


def secret():
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'data': rotation.encoded({'password': b'original-private'}),
            'metadata': {'name': 'redis-gate-auth', 'namespace': 'redis-gate',
                         'uid': 'owned-uid', 'resourceVersion': '42', 'labels': {
                             'kubeauto.io/component': 'redis-test', 'kubeauto.io/managed-by': 'kubeauto'}}}


class RedisRotationTests(unittest.TestCase):
    def test_shared_cluster_password_or_acl_blocks_sentinel_only_rotation_before_mutation(self):
        gate = Mock(name='source')
        gate.name = 'redis-gate-data'
        obj = {'kind': 'RedisCluster', 'metadata': {'name': 'separate'}, 'spec': {}}
        for field in ({'kubernetesConfig': {'redisSecret': {'name': 'redis-gate-auth'}}},
                      {'acl': {'secret': {'secretName': 'redis-gate-acl'}}}):
            obj['spec'] = field
            gate.get.return_value = {'items': [obj]}
            with self.assertRaisesRegex(ValueError, 'shared'):
                rotation.verify_rotation_consumers(gate, 'password-rotation')
            gate.k.assert_not_called()
        # Cluster's independent CA does not block Sentinel CA rotation.
        obj['spec'] = {'TLS': {'secret': {'secretName': 'redis-gate-cluster-tls'}}}
        with redirect_stdout(io.StringIO()):
            rotation.verify_rotation_consumers(gate, 'tls-rotation')
        obj['spec']['TLS']['secret']['secretName'] = 'redis-gate-tls'
        with self.assertRaisesRegex(ValueError, 'shared'):
            rotation.verify_rotation_consumers(gate, 'tls-rotation')
        gate.get.return_value = {'items': [
            {'kind': kind, 'metadata': {'name': name}, 'spec': {'kubernetesConfig': {
                'redisSecret': {'name': 'redis-gate-auth'}}}}
            for kind, name in [('RedisReplication', gate.name), ('RedisSentinel', gate.name + '-sentinel')]]}
        with redirect_stdout(io.StringIO()):
            rotation.verify_rotation_consumers(gate, 'password-rotation')
        source = (ROOT / 'tests/helpers/redis-rotation-gate.py').read_text()
        self.assertLess(source.index('verify_rotation_consumers(gate, args.module)'),
                        source.index('originals = {name: gate.get'))

    def test_forced_rollback_preserves_healthy_members_and_uses_uid_preconditions(self):
        from types import SimpleNamespace
        members = {'replication': [], 'sentinel': []}
        for name, role in (('data', 'replication'), ('data-sentinel-sentinel', 'sentinel')):
            for i in range(3):
                members[role].append({'metadata': {'name': f'{name}-{i}', 'namespace': 'redis-gate',
                    'uid': f'{name}-pod-{i}', 'resourceVersion': '100', 'labels': {
                    'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'},
                    'ownerReferences': [{'controller': True, 'kind': 'StatefulSet', 'name': name, 'uid': name + '-sts'}]},
                    'status': {'conditions': [{'type': 'Ready', 'status': 'False' if i == 2 else 'True'}]}})
        gate = SimpleNamespace(name='data', ns='redis-gate', identity=Mock(), k=Mock(),
            members=Mock(return_value=members), get=Mock(side_effect=lambda kind, name: {'metadata': {'uid': name + '-sts'}}))
        with redirect_stdout(io.StringIO()):
            rotation.rebuild_broken_members(gate)
        self.assertEqual(gate.k.call_count, 2)
        for call in gate.k.call_args_list:
            self.assertIn('/pods/', call.args[2])
            self.assertTrue(call.args[2].endswith('-2'))
            options = json.loads(call.kwargs['input_text'])
            self.assertEqual(options['preconditions']['resourceVersion'], '100')
            self.assertIn('pod-2', options['preconditions']['uid'])
        gate.k.reset_mock()
        members['sentinel'][2]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'foreign'):
            rotation.rebuild_broken_members(gate)
        gate.k.assert_not_called()

    def test_two_ca_generations_verify_both_leaves_and_identify_their_issuer(self):
        spec = importlib.util.spec_from_file_location('rotation_fixture', ROOT / 'tests/helpers/redis-lab-config.py')
        fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixtures)
        old, new = (fixtures.credentials()[2]['stringData'] for _ in range(2))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bundle.pem').write_text(old['ca.crt'] + '\n' + new['ca.crt'])
            subjects = []
            for index, values in enumerate((old, new)):
                (root / f'ca-{index}.pem').write_text(values['ca.crt'])
                leaf = root / f'leaf-{index}.pem'
                leaf.write_text(values['tls.crt'])
                subjects.append(subprocess.run(['openssl', 'x509', '-in', str(root / f'ca-{index}.pem'),
                    '-noout', '-subject'], check=True, capture_output=True, text=True).stdout)
                extensions = subprocess.run(['openssl', 'x509', '-in', str(leaf), '-noout', '-text'],
                    check=True, capture_output=True, text=True).stdout
                self.assertIn('X509v3 Authority Key Identifier', extensions)
                self.assertIn('X509v3 Subject Key Identifier', extensions)
                result = subprocess.run(['openssl', 'verify', '-CAfile', str(root / 'bundle.pem'), str(leaf)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(*subjects)
            rotation.verify_tls_material(root, {k: v.encode() for k, v in old.items()},
                                         {k: v.encode() for k, v in new.items()})
            rejected = subprocess.run(['openssl', 'verify', '-CAfile', str(root / 'ca-0.pem'),
                str(root / 'leaf-1.pem')], capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)

    def test_runtime_issuer_preflight_failure_blocks_secret_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(rotation.subprocess, 'run') as verify:
            verify.return_value.returncode = 2
            with self.assertRaisesRegex(ValueError, 'before Secret mutation'):
                rotation.verify_tls_material(Path(tmp), {'ca.crt': b'old', 'tls.crt': b'old-leaf'},
                                             {'ca.crt': b'new', 'tls.crt': b'new-leaf'})
            self.assertEqual(verify.call_count, 1)
            self.assertNotIn('tls.key', repr(verify.call_args))

    def test_cas_retains_resource_version_and_rejects_changed_identity_or_data(self):
        original = secret()
        document = rotation.replace_document(original, original, original['data'], {'password': b'next'})
        self.assertEqual(document['metadata']['resourceVersion'], '42')
        for field in ('uid', 'name', 'namespace'):
            current = copy.deepcopy(original)
            current['metadata'][field] = 'foreign'
            with self.assertRaises(ValueError):
                rotation.replace_document(current, original, original['data'], {})
        for field in ('kubeauto.io/component', 'kubeauto.io/managed-by'):
            current = copy.deepcopy(original)
            current['metadata']['labels'][field] = 'foreign'
            with self.assertRaises(ValueError):
                rotation.replace_document(current, original, original['data'], {})
        current = copy.deepcopy(original)
        current['data'] = rotation.encoded({'password': b'concurrent-private'})
        with self.assertRaises(ValueError):
            rotation.replace_document(current, original, original['data'], {})

    def test_accepted_write_timeout_is_read_back_then_original_is_restored(self):
        original, gate = secret(), Mock()
        name = original['metadata']['name']
        accepted = copy.deepcopy(original)
        accepted['data'] = rotation.encoded({'password': b'next-private'})
        accepted['metadata']['resourceVersion'] = '43'
        gate.get.side_effect = [original, accepted, accepted]
        timeout = subprocess.TimeoutExpired('kubectl', 90)
        gate.k.side_effect = [timeout, 'replaced']
        scope = rotation.SecretRotation(gate, {name: original})
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired) as error:
            scope.replace(name, {'password': b'next-private'})
        self.assertIs(error.exception, timeout)
        self.assertEqual(scope.expected[name], accepted['data'])
        scope.restore()
        self.assertEqual(scope.expected[name], original['data'])
        args, kwargs = gate.k.call_args
        self.assertEqual(args, ('replace', '-f', '-'))
        self.assertNotIn('private', repr(args))
        restored = json.loads(kwargs['input_text'])
        self.assertEqual(restored['data'], original['data'])
        self.assertEqual(restored['metadata']['resourceVersion'], '43')

    def test_ambiguous_foreign_write_is_never_rolled_back(self):
        original, gate = secret(), Mock()
        name = original['metadata']['name']
        foreign = copy.deepcopy(original)
        foreign['data'] = rotation.encoded({'password': b'foreign-private'})
        gate.get.side_effect = [original, foreign, foreign]
        gate.k.side_effect = subprocess.TimeoutExpired('kubectl', 90)
        scope = rotation.SecretRotation(gate, {name: original})
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            scope.replace(name, {'password': b'next-private'})
        with self.assertRaises(ValueError):
            scope.restore()
        self.assertEqual(gate.k.call_count, 1)

    def test_diagnostic_failure_does_not_prevent_rollback_or_hide_first_failure(self):
        gate, scope, roll = Mock(), Mock(), Mock()
        gate.fault_diagnostics.side_effect = RuntimeError('diagnostic-private')
        scope.mutated = ['redis-gate-auth']
        first = ValueError('first owning failure')
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ValueError) as error:
            try:
                raise first
            except BaseException:
                rotation.recover(gate, scope, roll)
                raise
        self.assertIs(error.exception, first)
        scope.restore.assert_called_once()
        roll.assert_called_once()
        self.assertNotIn('private', output.getvalue())
        scope.restore.side_effect = RuntimeError('rollback-private')
        roll.reset_mock()
        with redirect_stdout(output):
            rotation.recover(gate, scope, roll)
        roll.assert_not_called()
        self.assertIn('REDIS_ROTATION_ROLLBACK_FAIL', output.getvalue())


if __name__ == '__main__':
    unittest.main()
