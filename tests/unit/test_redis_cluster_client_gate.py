"""Cluster redirects must validate ownership and ASKING's one-command scope."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tests/helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

gate = load('redis_cluster_client_contract', 'redis-cluster-client-gate.py')
focused = load('redis_cluster_client_protocol_contract', 'redis-focused-gate.py')


class RedisClusterClientTests(unittest.TestCase):
    def test_startup_recovery_refuses_foreign_name_and_value_before_any_deletion(self):
        cluster = self.cluster()
        cluster.topology = Mock(return_value=[(0, 16383, 'primary', 'id')])
        cluster.health = Mock()
        cluster.command = Mock(side_effect=[1, 0])
        key = b'kubeauto:redis:cluster:' + b'a' * 32 + b'{startup-31}'
        client = Mock()
        cluster.client = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as tmp:
            cluster.cluster = Path(tmp)
            for invalid in (key.replace(b'31', b'32'), key.replace(b'startup', b'customer')):
                client.command.side_effect = [[b'0', [key, invalid]], b'verified-' + b'a' * 32]
                with self.assertRaisesRegex(ValueError, 'name'):
                    cluster.recover_failure_keys(startup=True)
                cluster.command.assert_not_called()
            client.command.side_effect = [[b'0', [key]], b'customer-value']
            with self.assertRaisesRegex(ValueError, 'value'):
                cluster.recover_failure_keys(startup=True)
            cluster.command.assert_not_called()
            client.command.side_effect = [[b'0', [key]], b'verified-' + b'a' * 32]
            cluster.recover_failure_keys(startup=True)
            self.assertIn("redis.call('GET',KEYS[1]) == ARGV[1]", cluster.command.call_args_list[0].args[2])
            cluster.health.assert_called_once()

    def test_recovery_validates_all_keys_before_mutation_and_compare_deletes(self):
        cluster = self.cluster()
        cluster.topology = Mock(return_value=[(0, 16383, 'primary', 'id')])
        cluster.health = Mock()
        cluster.command = Mock(side_effect=[1, 0])
        key = b'kubeauto:redis:cluster:' + b'a' * 32 + b'{failure-47}'
        client = Mock()
        cluster.client = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as tmp:
            cluster.cluster = Path(tmp)
            for invalid in (b'foreign', key.replace(b'47', b'48')):
                client.command.side_effect = [[b'0', [key, invalid]], b'retained-' + b'a' * 32]
                with self.assertRaisesRegex(ValueError, 'name'):
                    cluster.recover_failure_keys()
                cluster.command.assert_not_called()
            client.command.side_effect = [[b'0', [key]], b'customer-value']
            with self.assertRaisesRegex(ValueError, 'value'):
                cluster.recover_failure_keys()
            cluster.command.assert_not_called()
            client.command.side_effect = [[b'0', [key]], b'retained-' + b'a' * 32]
            cluster.recover_failure_keys()
            self.assertIn("redis.call('GET',KEYS[1]) == ARGV[1]", cluster.command.call_args_list[0].args[2])
            cluster.health.assert_called_once()

    def test_official_preferred_hostname_maps_only_actual_owned_member_fqdn(self):
        groups = {'leader': [{'metadata': {'name': 'data-0'},
                   'status': {'podIP': '10.84.1.2'},
                   'spec': {'hostname': 'data-0', 'subdomain': 'data-headless'}}]}
        addresses = gate.member_addresses(groups, 'redis-gate')
        self.assertEqual(addresses, {'10.84.1.2': 'data-0',
            'data-0.data-headless.redis-gate.svc.cluster.local': 'data-0'})
        self.assertNotIn('data-1.data-headless.redis-gate.svc.cluster.local', addresses)
        self.assertNotIn('data-0.data-headless.other.svc.cluster.local', addresses)
        cluster = self.cluster()
        cluster.ns = 'redis-gate'
        cluster.members.return_value = groups
        source, target = Mock(), Mock()
        source.command.side_effect = focused.RedisReplyError(
            f'MOVED {gate.key_slot("key")} data-0.data-headless.redis-gate.svc.cluster.local:6379'.encode())
        target.command.return_value = b'value'
        cluster.client = Mock(side_effect=[source, target])
        self.assertEqual(cluster.command('key', 'GET', 'key'), b'value')
        cluster.client.assert_called_with('data-0')

    def test_baseline_refuses_unrecovered_ask_or_scale_journal_before_api_mutation(self):
        for name in ('redis-cluster-ask-fault.json', 'redis-cluster-scale-fault.json',
                     'redis-cluster-process-fault-owned-0.json'):
            with tempfile.TemporaryDirectory() as tmp:
                source = Mock()
                source.base = source.cluster = Path(tmp)
                (source.cluster / name).write_text('{}')
                with self.assertRaisesRegex(ValueError, 'stale'):
                    gate.Cluster(source, focused, Path(tmp))
                source.get.assert_not_called()
                source.k.assert_not_called()

    def test_pause_requires_host_state_and_keeps_exact_identity_for_cleanup(self):
        import json
        cluster = object.__new__(gate.Cluster)
        obj = {'metadata': {'uid': 'pod-uid', 'labels': {'app': gate.NAME + '-leader',
               'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'}},
               'spec': {'nodeName': 'worker-1', 'containers': [{'name': 'redis'}]},
               'status': {'containerStatuses': [{'name': 'redis', 'containerID': 'containerd://' + 'a' * 64}]}}
        cluster.get = Mock(return_value=obj)
        cluster.k = Mock(return_value=json.dumps({'status': {'addresses': [
            {'type': 'InternalIP', 'address': '192.168.47.131'}]}}))
        cluster.host_process = Mock(side_effect=[{'pid': 8123, 'pid_start': '1234'},
            {'state': 'T', 'outcome': 'paused'}, {'outcome': 'resumed'}])
        cluster.run_id, cluster.ns, cluster.journal = 'run-id', 'redis-gate', {'cr_uid': 'owned'}
        with tempfile.TemporaryDirectory() as tmp:
            cluster.cluster = Path(tmp)
            with cluster.paused('owned-0'):
                journals = list(cluster.cluster.glob('redis-cluster-process-fault-*.json'))
                self.assertEqual(len(journals), 1)
                record = json.loads(journals[0].read_text())
                self.assertEqual((record['pid'], record['pid_start']), (8123, '1234'))
                self.assertEqual(record['container_id'], 'a' * 64)
            self.assertEqual(list(cluster.cluster.glob('redis-cluster-process-fault-*.json')), [])
        calls = cluster.host_process.call_args_list
        self.assertEqual([call.args[1]['action'] for call in calls], ['inspect', 'stop', 'continue'])
        self.assertEqual(calls[-1].args[1]['pid_start'], '1234')
        self.assertNotIn('exec', [call.args[0] for call in cluster.k.call_args_list])

    def test_host_signal_rejects_outside_lab_before_ssh(self):
        cluster = object.__new__(gate.Cluster)
        cluster.source = Mock()
        for host in ('192.168.47.129', '192.168.122.1', '192.168.47.130'):
            with self.assertRaisesRegex(ValueError, 'allowlisted'):
                cluster.host_process(host, {})
        cluster.source.run.assert_not_called()

    def test_topology_seed_excludes_the_deliberately_isolated_member(self):
        cluster = object.__new__(gate.Cluster)
        cluster.excluded = {'isolated'}
        cluster.ns = 'redis-gate'
        groups = {'leader': [{'metadata': {'name': name}, 'status': {'podIP': ip}}
                            for name, ip in (('isolated', '10.1.1.1'), ('healthy', '10.1.1.2'))]}
        cluster.members = Mock(return_value=groups)
        cluster.client = Mock()
        cluster.client.return_value.command.return_value = [[0, 16383, [b'10.1.1.2', 6379, b'id']]]
        self.assertEqual(cluster.topology(), [(0, 16383, 'healthy', 'id')])
        cluster.client.assert_called_once_with('healthy')

    def test_official_crc16_hash_tags_and_empty_tag_behavior(self):
        self.assertEqual(gate.key_slot('123456789'), 12739)
        self.assertEqual(gate.key_slot('a{user1000}:x'), gate.key_slot('b{user1000}:y'))
        self.assertEqual(gate.key_slot('a{foo}{bar}'), gate.key_slot('foo'))
        self.assertEqual(gate.key_slot('{}abc'), 5980)
        self.assertNotEqual(gate.key_slot('different'), gate.key_slot('foo'))

    def cluster(self):
        cluster = object.__new__(gate.Cluster)
        cluster.focused = focused
        cluster.ns = 'redis-gate'
        cluster.redirects = {'MOVED': 0, 'ASK': 0}
        cluster.owner = Mock(return_value='source')
        cluster.members = Mock(return_value={'leader': [
            {'status': {'podIP': '10.84.1.2'}, 'metadata': {'name': 'target'}}]})
        return cluster

    def test_ask_sends_asking_and_original_command_on_same_authenticated_connection(self):
        cluster = self.cluster()
        source, target = Mock(), Mock()
        source.command.side_effect = focused.RedisReplyError(f'ASK {gate.key_slot("key")} 10.84.1.2:6379'.encode())
        target.command.side_effect = [b'OK', b'value']
        cluster.client = Mock(side_effect=[source, target])
        self.assertEqual(cluster.command('key', 'GET', 'key'), b'value')
        self.assertEqual([c.args for c in target.command.call_args_list], [('ASKING',), ('GET', 'key')])
        self.assertEqual(cluster.redirects, {'MOVED': 0, 'ASK': 1})

    def test_redirect_rejects_foreign_endpoint_wrong_slot_and_wrong_port(self):
        for reply in (f'MOVED {gate.key_slot("key")} 10.84.1.3:6379',
                      f'ASK {gate.key_slot("key")} 10.84.1.2:26379',
                      'MOVED 123 10.84.1.2:6379'):
            cluster = self.cluster()
            client = Mock()
            client.command.side_effect = focused.RedisReplyError(reply.encode())
            cluster.client = Mock(return_value=client)
            with self.assertRaises((ValueError, focused.RedisReplyError)):
                cluster.command('key', 'GET', 'key')
            self.assertEqual(cluster.client.call_count, 1)

    def test_moved_follows_only_owned_address_and_redirect_loop_is_bounded(self):
        cluster = self.cluster()
        source, target = Mock(), Mock()
        error = focused.RedisReplyError(f'MOVED {gate.key_slot("key")} 10.84.1.2:6379'.encode())
        source.command.side_effect = error
        target.command.return_value = b'value'
        cluster.client = Mock(side_effect=[source, target])
        self.assertEqual(cluster.command('key', 'GET', 'key'), b'value')
        cluster.client.assert_called_with('target')
        cluster.client = Mock(return_value=source)
        with self.assertRaisesRegex(ValueError, 'budget'):
            cluster.command('key', 'GET', 'key')
        self.assertEqual(cluster.client.call_count, 5)

    def test_dispatch_and_evidence_bind_full_data_scenario(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        helper = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        self.assertIn('--redis-cluster-data', runner)
        self.assertIn('redis-cluster-client-gate.py', helper)
        evidence = load('redis_cluster_client_evidence', 'redis-evidence.py')
        self.assertEqual(evidence.CASES['cluster-data'], ('REDIS-13', 'REDIS_CLUSTER_CLIENT_PASS'))


if __name__ == '__main__':
    unittest.main()
