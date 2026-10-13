"""Protocol, credential and process cleanup contracts before focused live gates."""
import importlib.util
import io
import ssl
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_focused', ROOT / 'tests/helpers/redis-focused-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RedisFocusedTests(unittest.TestCase):
    def test_redirect_retains_only_valid_slot_ip_port_without_server_text(self):
        for code in ('MOVED', 'ASK'):
            error = gate.RedisReplyError(f'{code} 123 10.84.1.2:6379'.encode())
            self.assertEqual(error.redirect, (123, '10.84.1.2', 6379))
            self.assertEqual(str(error), code)
        for reply in (b'MOVED 16384 10.1.1.1:6379', b'ASK 1 999.1.1.1:6379',
                      b'MOVED 1 10.1.1.1:0', b'ASK 1 -foreign:6379',
                      b'MOVED 1 10.1.1.1:6379 secret'):
            self.assertIsNone(gate.RedisReplyError(reply).redirect)
        self.assertEqual(gate.RedisReplyError(b'MOVED 123 data-0.data-headless.redis.svc.cluster.local:6379').redirect,
                         (123, 'data-0.data-headless.redis.svc.cluster.local', 6379))

    def test_quorum_fault_preserves_only_verified_actual_resolver(self):
        instance = object.__new__(gate.Gate)
        instance.ns = 'redis-gate'
        instance.config = {'ENABLE_LOCAL_DNS_CACHE': True, 'LOCAL_DNS_CACHE': '169.254.20.10'}
        instance.get = Mock(return_value={'spec': {'containers': [{'name': 'owned-member'}]}})
        instance.k = Mock(return_value='search cluster.local\nnameserver 169.254.20.10\noptions ndots:5')
        self.assertEqual(instance.resolvers(['data-0', 'sentinel-0']), ('169.254.20.10',))
        self.assertEqual(instance.k.call_count, 2)
        for reply in ('', 'nameserver 10.84.0.2', 'nameserver 169.254.20.10\nnameserver 1.1.1.1'):
            instance.k.return_value = reply
            with self.assertRaises(ValueError): instance.resolvers(['sentinel-0'])
        source = (ROOT / 'tests/helpers/redis-focused-gate.py').read_text()
        self.assertIn('self.isolated(targets, dns_addresses=self.resolvers(targets))', source)

    def test_protocol_diagnostic_still_reads_logs_after_tls_verification_failure(self):
        import json
        import runpy
        import tempfile
        fake = Mock()
        fake.tls = {'ca.crt': b'public-ca', 'tls.crt': b'public-leaf'}
        fake.client.side_effect = ssl.SSLCertVerificationError('controlled')
        fake.members.return_value = {'replication': [{'metadata': {'name': 'data-0'},
                                                     'spec': {'containers': [{'name': 'redis'}]}}]}
        fake.get.return_value = {'items': []}
        fake.kubectl = ['kubectl']
        fake.k.side_effect = [json.dumps({'metadata': {'uid': 'own'}}), 'safe-server-log']
        with tempfile.TemporaryDirectory() as tmp:
            fake.cluster = Path(tmp)
            (fake.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'own'}))
            spec = Mock()
            module = SimpleNamespace(Gate=Mock(return_value=fake))
            with patch('importlib.util.spec_from_file_location', return_value=spec), \
                    patch('importlib.util.module_from_spec', side_effect=[module, SimpleNamespace(AUTH_CLI_PREFIX=[])]), \
                    patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                runpy.run_path(str(ROOT / 'tests/helpers/redis-sentinel-probe.py'))
            self.assertIn('safe-server-log', output.getvalue())
            self.assertIn('SSLCertVerificationError', output.getvalue())
        self.assertIn('logs', fake.k.call_args.args)

    def test_resp_handles_binary_nested_null_and_machine_error(self):
        self.assertEqual(gate.encode_command('SET', 'key', b'\x00\r\n'),
                         b'*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$3\r\n\x00\r\n\r\n')
        self.assertEqual(gate.read_reply(io.BytesIO(b'*3\r\n+OK\r\n:2\r\n$-1\r\n')), [b'OK', 2, None])
        self.assertEqual(gate.read_reply(io.BytesIO(b'$3\r\n\x00\r\n\r\n')), b'\x00\r\n')
        with self.assertRaises(gate.RedisReplyError) as error:
            gate.read_reply(io.BytesIO(b'-WRONGPASS sensitive server text\r\n'))
        self.assertEqual(str(error.exception), 'WRONGPASS')

    def test_auth_diagnostic_timeout_preserves_independent_business_and_logs(self):
        import json
        import runpy
        import subprocess
        import tempfile
        fake = Mock(kubectl=['kubectl'], ns='redis-gate')
        fake.tls = {'ca.crt': b'public-ca', 'tls.crt': b'public-leaf'}
        fake.client.return_value.command.return_value = b'role:master'
        fake.members.return_value = {'replication': [{'metadata': {'name': 'data-0'},
            'spec': {'containers': [{'name': 'redis', 'env': [{'name': 'REDIS_PASSWORD'}]}]}}]}
        fake.get.return_value = {'items': []}
        fake.k.side_effect = [json.dumps({'metadata': {'uid': 'own'}}), 'safe-server-log']
        with tempfile.TemporaryDirectory() as tmp:
            fake.cluster = Path(tmp)
            (fake.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'own'}))
            spec = Mock()
            modules = [SimpleNamespace(Gate=Mock(return_value=fake)), SimpleNamespace(AUTH_CLI_PREFIX=[])]
            results = [SimpleNamespace(returncode=0, stdout='', stderr='')] * 3
            results.append(subprocess.TimeoutExpired(['kubectl'], 45))
            with patch('importlib.util.spec_from_file_location', return_value=spec), \
                    patch('importlib.util.module_from_spec', side_effect=modules), \
                    patch('subprocess.run', side_effect=results), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                runpy.run_path(str(ROOT / 'tests/helpers/redis-sentinel-probe.py'))
            self.assertIn('PRODUCT_CLI_AUTH_UNAVAILABLE pod=data-0 reason=TimeoutExpired', output.getvalue())
            self.assertIn('role:master', output.getvalue())
            self.assertIn('safe-server-log', output.getvalue())

    def test_resp_rejects_truncation_malformed_and_unbounded_reply(self):
        for reply in (b'', b'+OK\n', b'$4\r\nab', b'$2\r\nabxx'):
            with self.assertRaises(EOFError):
                gate.read_reply(io.BytesIO(reply))
        for reply in (b'$-2\r\n', b'$67108865\r\n', b'?invalid\r\n', b':x\r\n'):
            with self.assertRaises(ValueError):
                gate.read_reply(io.BytesIO(reply))

    def test_tls_failure_closes_raw_socket_without_authentication(self):
        raw, context = Mock(), Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('controlled')
        with patch.object(gate.socket, 'create_connection', return_value=raw):
            with self.assertRaises(ssl.SSLCertVerificationError):
                gate.Client(1234, context, 'redis-master', b'secret')
        raw.close.assert_called_once()
        raw.sendall.assert_not_called()

    def test_auth_normalizes_crlf_and_wrong_auth_closes_tls_stream(self):
        raw, context, secured = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = secured
        secured.makefile.return_value = io.BytesIO(b'+OK\r\n')
        with patch.object(gate.socket, 'create_connection', return_value=raw):
            client = gate.Client(1234, context, 'redis-master', b'private\r\n')
            secured.sendall.assert_called_once_with(gate.encode_command('AUTH', 'default', b'private'))
            client.close()
            client.close()
        stream = io.BytesIO(b'-WRONGPASS arbitrary private content\r\n')
        secured.makefile.return_value = stream
        secured.reset_mock()
        with patch.object(gate.socket, 'create_connection', return_value=raw):
            with self.assertRaises(gate.RedisReplyError):
                gate.Client(1234, context, 'redis-master', b'private')
        self.assertTrue(stream.closed)
        secured.close.assert_called_once()

    def test_sentinel_discovery_accepts_exact_upstream_fqdn_and_rejects_split_view(self):
        instance = object.__new__(gate.Gate)
        instance.ns = 'redis-gate'
        instance.members = lambda: {'replication': [
            {'metadata': {'name': 'data-0'}, 'spec': {'hostname': 'data-0', 'subdomain': 'data-headless'},
             'status': {'podIP': '10.1.1.1'}}],
            'sentinel': [{'metadata': {'name': f'sentinel-{i}'}} for i in range(3)]}
        client = Mock()
        client.command.return_value = [b'data-0.data-headless.redis-gate.svc.cluster.local', b'6379']
        instance.client = Mock(return_value=client)
        self.assertEqual(instance.primary(), 'data-0')
        client.command.side_effect = [[b'10.1.1.1', b'6379'], [b'foreign', b'6379'], [b'10.1.1.1', b'6379']]
        with self.assertRaises(ValueError):
            instance.primary()

    def test_port_forward_process_is_reaped_when_fixture_unwinds(self):
        import tempfile
        instance = object.__new__(gate.Gate)
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            instance.work, instance.stack = Path(tmp), stack
            instance.forwards, instance.kubectl, instance.ns = {}, ['kubectl'], 'redis-gate'
            def start(*args, **kwargs):
                kwargs['stdout'].write('Forwarding from 127.0.0.1:43210 -> 6379\n')
                kwargs['stdout'].flush()
                return process
            with patch.object(gate.subprocess, 'Popen', side_effect=start) as launch:
                self.assertEqual(instance.forward('data-0', 6379), 43210)
                self.assertEqual(instance.forward('data-0', 6379), 43210)
                self.assertEqual(launch.call_count, 1)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_negative_control_rejects_wrong_failure_class(self):
        instance = object.__new__(gate.Gate)
        for action in (lambda: None, lambda: (_ for _ in ()).throw(gate.RedisReplyError(b'NOAUTH'))):
            with self.assertRaises(ValueError):
                instance.reject(action, gate.RedisReplyError, 'WRONGPASS')

    def test_focused_dispatch_preserves_launch_boundary(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        start = runner.index('if [[ "$MODE" == --redis-sentinel-data')
        end = runner.index('if [[ "$MODE" == "--redis-install-watch"', start)
        branch = runner[start:end]
        self.assertLess(runner.index('monitor_remote_job() {'), start)
        self.assertLess(branch.index('redis-static.sh'), branch.index('ssh_redis'))
        self.assertLess(branch.index('kill -0'), branch.index('sync-kubeauto.sh'))
        self.assertIn('REDIS_FOCUSED_MODULE_PASS module=$redis_module', branch)
        self.assertNotIn('cancel_remote_job', branch)
        self.assertNotIn('redis-cleanup.sh', branch)

    def test_rejected_tls_connections_do_not_share_the_positive_forward(self):
        instance = object.__new__(gate.Gate)
        instance.forward = Mock(return_value=1234)
        instance.context, instance.server_name = Mock(), 'redis-master'
        instance.passwords = {'password': b'private'}
        instance.stack = Mock()
        with patch.object(gate, 'Client', side_effect=ssl.SSLCertVerificationError('controlled')):
            with self.assertRaises(ssl.SSLCertVerificationError):
                instance.client('data-0', fresh=True)
        instance.forward.assert_called_once_with('data-0', 6379, reuse=False)
        instance.forward.reset_mock()
        with patch.object(gate, 'Client', return_value=Mock()):
            instance.client('data-0')
        instance.forward.assert_called_once_with('data-0', 6379, reuse=True)

    def test_identity_exclusion_is_limited_to_operator_helm_revision_journals(self):
        import copy
        journal = {'metadata': {'name': 'sh.helm.release.v1.redis-operator.v2', 'uid': 'revision-2',
                   'labels': {'owner': 'helm', 'name': 'redis-operator'}}, 'type': 'helm.sh/release.v1'}
        credential = {'metadata': {'name': 'operator-credential', 'uid': 'credential-1'}, 'type': 'Opaque'}
        self.assertEqual(gate.resource_identities([journal, credential], operator_history=True),
                         [('operator-credential', 'credential-1')])
        self.assertEqual(len(gate.resource_identities([journal, credential])), 2)
        for mutate in (lambda d: d.update(type='Opaque'),
                       lambda d: d['metadata']['labels'].update(owner='customer'),
                       lambda d: d['metadata'].update(name='customer-secret')):
            wrong = copy.deepcopy(journal)
            mutate(wrong)
            self.assertEqual(len(gate.resource_identities([wrong], operator_history=True)), 1)

    def test_network_fault_requires_exact_owned_member_selector(self):
        import copy
        member = {'metadata': {'name': 'data-0', 'labels': {
            'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto',
            'statefulset.kubernetes.io/pod-name': 'data-0'}}}
        members = {'replication': [member], 'sentinel': []}
        policy = gate.isolation_policy('redis-gate', 'a' * 32, ['data-0'], members)
        self.assertEqual(policy['metadata']['namespace'], 'redis-gate')
        self.assertEqual(policy['spec']['podSelector']['matchExpressions'], [{
            'key': 'statefulset.kubernetes.io/pod-name', 'operator': 'In', 'values': ['data-0']}])
        self.assertEqual(policy['spec']['ingress'], [])
        self.assertEqual(policy['spec']['egress'], [])
        dns_policy = gate.isolation_policy('redis-gate', 'a' * 32, ['data-0'], members, dns_addresses=('169.254.20.10',))
        self.assertEqual(dns_policy['spec']['ingress'], [])
        self.assertEqual(dns_policy['spec']['egress'], [{'to': [{
            'ipBlock': {'cidr': '169.254.20.10/32'}}],
            'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}])
        self.assertEqual(policy, gate.isolation_policy('redis-gate', 'a' * 32, ['data-0'], members,
                                                     dns_addresses=()))
        for targets in ([], ['foreign'], ['data-0', 'data-0']):
            with self.assertRaises(ValueError):
                gate.isolation_policy('redis-gate', 'a' * 32, targets, members)
        foreign = copy.deepcopy(member)
        foreign['metadata']['labels']['kubeauto.io/managed-by'] = 'customer'
        with self.assertRaises(ValueError):
            gate.isolation_policy('redis-gate', 'a' * 32, ['data-0'], {'replication': [foreign]})

    def test_fault_scope_restores_on_failure_and_refuses_changed_ownership(self):
        import json
        import tempfile
        from contextlib import redirect_stdout
        instance = object.__new__(gate.Gate)
        instance.ns, instance.run_id = 'redis-gate', 'a' * 32
        instance.members = lambda: {'replication': [{'metadata': {'name': 'data-0', 'labels': {
            'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto',
            'statefulset.kubernetes.io/pod-name': 'data-0'}}}], 'sentinel': []}
        policy = gate.isolation_policy(instance.ns, instance.run_id, ['data-0'], instance.members())
        instance.health = Mock()
        instance.fault_diagnostics = Mock()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            instance.cluster = Path(tmp)
            (instance.cluster / 'redis-lab-owner.json').write_text(json.dumps({'cluster_uid': 'own-core'}))
            instance.k = Mock(side_effect=['created', json.dumps(policy), 'deleted', ''])
            with self.assertRaisesRegex(RuntimeError, 'first failure'):
                with instance.isolated(['data-0']):
                    self.assertTrue((instance.cluster / 'redis-focused-fault.json').exists())
                    raise RuntimeError('first failure')
            self.assertFalse((instance.cluster / 'redis-focused-fault.json').exists())
            instance.health.assert_called_once()
            instance.fault_diagnostics.assert_called_once()
            policy['metadata']['labels']['kubeauto.io/test-run'] = 'foreign-owner'
            instance.k = Mock(side_effect=['created', json.dumps(policy)])
            with self.assertRaisesRegex(ValueError, 'refuses changed ownership'):
                with instance.isolated(['data-0']):
                    pass
            self.assertTrue((instance.cluster / 'redis-focused-fault.json').exists())
            self.assertFalse(any('delete' in call.args for call in instance.k.call_args_list))

    def test_connection_reset_closes_all_peer_types_but_keeps_admin_connection(self):
        instance = object.__new__(gate.Gate)
        client = Mock()
        client.command.side_effect = [3, 3, 2]
        instance.client = Mock(return_value=client)
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            instance.disconnect_existing('data-0')
        self.assertEqual([call.args for call in client.command.call_args_list], [
            ('CLIENT', 'KILL', 'TYPE', kind, 'SKIPME', 'yes') for kind in ('normal', 'pubsub', 'replica')])
        instance.client.assert_called_once_with('data-0', 6379, fresh=True)

    def test_persistence_gate_requires_every_new_process_and_retained_pvc(self):
        import copy
        from contextlib import redirect_stdout
        for defect in (None, 'pod-not-replaced', 'pvc-replaced'):
            instance = object.__new__(gate.Gate)
            instance.ns, instance.name, instance.key, instance.value = 'redis-gate', 'redis-gate-data', 'own-key', b'own-value'
            instance.forwards, instance.health = {}, Mock()
            before = [{'metadata': {'name': f'data-{i}', 'uid': f'old-{i}'}} for i in range(3)]
            current = copy.deepcopy(before)
            instance.members = lambda: {'replication': current}
            claim = {'metadata': {'name': 'own-pvc', 'uid': 'owned-volume'}}
            instance.get = lambda kind: {'items': [claim]}
            def mutate(*args, **kwargs):
                if 'status' in args:
                    for i, pod in enumerate(current):
                        if defect != 'pod-not-replaced' or i != 0:
                            pod['metadata']['uid'] = f'new-{i}'
                    if defect == 'pvc-replaced':
                        claim['metadata']['uid'] = 'replacement-volume'
            instance.k = Mock(side_effect=mutate)
            client = Mock()
            def command(*parts):
                return {'WAITAOF': [1, 2], 'SAVE': b'OK', 'SET': b'OK', 'GET': b'own-value', 'TTL': 1000,
                        'INFO': b'aof_enabled:1\r\naof_last_write_status:ok\r\nrdb_last_bgsave_status:ok\r\n'}[parts[0]]
            client.command.side_effect = command
            instance.client = Mock(return_value=client)
            instance.primary = lambda: 'data-0'
            instance.seeded_primary = lambda: ('data-0', client)
            with redirect_stdout(io.StringIO()) as output:
                if defect:
                    with self.assertRaises(ValueError): instance.persistence()
                else:
                    instance.persistence()
            self.assertEqual('REDIS_AOF_PERSISTENCE_PASS' in output.getvalue(), defect is None)
            self.assertEqual(instance.k.call_args_list[0].args, (
                '-n', 'redis-gate', 'rollout', 'restart', 'statefulset/redis-gate-data'))

    def test_dns_repair_cannot_remove_foreign_data_or_replace_pvc(self):
        from contextlib import redirect_stdout
        instance = object.__new__(gate.Gate)
        instance.base, instance.ns, instance.name = Path('/owned'), 'redis-gate', 'redis-gate-data'
        instance.forwards, instance.health, instance.run = {}, Mock(), Mock()
        instance.get = Mock(return_value={'items': [{'metadata': {'name': 'pvc', 'uid': 'same-pvc'}}]})
        instance.primary = lambda: 'data-1'
        client = Mock()
        instance.client = Mock(return_value=client)
        foreign = b'kubeauto:redis:focused:' + b'a' * 32
        client.command.side_effect = [[b'0', [foreign]], b'customer-value']
        with redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'unproven fixture key'):
                instance.repair_sentinel()
        self.assertFalse(any(c.args[0] == 'DEL' for c in client.command.call_args_list))
        self.assertIn('kubecli.py', str(instance.run.call_args.args[0][1]))
        self.assertEqual(instance.run.call_args.args[0][-2:], ['--tags', 'redis'])


if __name__ == '__main__':
    unittest.main()
