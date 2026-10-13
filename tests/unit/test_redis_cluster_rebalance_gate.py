"""Shard changes preserve independent inputs and absolute expiry semantics."""
import importlib.util
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_cluster_rebalance_contract', ROOT / 'tests/helpers/redis-cluster-rebalance-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RedisClusterRebalanceTests(unittest.TestCase):
    def test_function_fixture_has_strict_uuid_and_business_get_semantics(self):
        library, source = gate.function_fixture('a' * 32)
        self.assertEqual(library, 'kubeauto_resize_' + 'a' * 32)
        self.assertTrue(source.startswith(('#!lua name=' + library).encode()))
        self.assertIn((library + '_get').encode(), source)
        self.assertIn(b'redis.call("GET", keys[1])', source)
        for value in ('foreign', 'a' * 31, 'a' * 33, '../' + 'a' * 32):
            with self.assertRaises(ValueError): gate.function_fixture(value)

    def test_function_reply_requires_literal_name_code_and_unique_identity(self):
        self.assertEqual(gate.function_sources([[b'library_name', b'owned', b'library_code', b'code']]),
                         {b'owned': b'code'})
        for reply in (None, [[b'library_name']], [[b'library_name', b'owned']],
                      [[b'library_name', b'owned', b'library_code', 'not-bytes']],
                      [[b'library_name', b'owned', b'library_code', b'code']] * 2):
            with self.assertRaises(ValueError): gate.function_sources(reply)

    def test_function_cleanup_validates_every_primary_before_deletion(self):
        cluster = Mock()
        cluster.topology.return_value = [(0, 8191, 'primary-a', 'id-a'), (8192, 16383, 'primary-b', 'id-b')]
        first, second = Mock(), Mock()
        cluster.client.side_effect = [first, second]
        first.command.return_value = [[b'library_name', b'owned', b'library_code', b'expected']]
        second.command.return_value = [[b'library_name', b'owned', b'library_code', b'foreign']]
        with self.assertRaises(ValueError): gate.cleanup_functions(cluster, 'owned', b'expected')
        self.assertEqual(first.command.call_count, 1)
        self.assertEqual(second.command.call_count, 1)
        self.assertFalse(any('DELETE' in call.args or 'FLUSH' in call.args
                             for client in (first, second) for call in client.command.call_args_list))

    def test_function_cleanup_keeps_foreign_libraries_and_requires_fsync(self):
        cluster = Mock()
        cluster.topology.return_value = [(0, 16383, 'primary', 'id')]
        client = cluster.client.return_value
        foreign = [b'library_name', b'foreign', b'library_code', b'foreign-code']
        client.command.side_effect = [[[b'library_name', b'owned', b'library_code', b'expected'], foreign],
                                      b'OK', [1, 2], [foreign]]
        gate.cleanup_functions(cluster, 'owned', b'expected')
        self.assertEqual(client.command.call_args_list[1].args, ('FUNCTION', 'DELETE', 'owned'))
        self.assertEqual(client.command.call_args_list[2].args, ('WAITAOF', 1, 2, 5000))

    def test_seed_mode_is_allowlisted_and_does_not_claim_the_full_scenario(self):
        helper = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        allowlist = helper.split('case "$module" in ', 1)[1].split(') ;;', 1)[0].split('|')
        self.assertIn('cluster-rebalance-seed', allowlist)
        self.assertIn('cluster-rebalance|cluster-rebalance-seed) script=redis-cluster-rebalance-gate.py', helper)
        self.assertIn('"$MODE" == --redis-cluster-rebalance-seed',
                      (ROOT / 'tests/run_enterprise_regression.sh').read_text())
        self.assertNotIn("'cluster-rebalance-seed':", (ROOT / 'tests/helpers/redis-evidence.py').read_text())

    def test_seed_failure_is_inside_the_same_finally_as_owned_key_cleanup(self):
        tree = ast.parse((ROOT / 'tests/helpers/redis-cluster-rebalance-gate.py').read_text())
        protected = next(node for node in ast.walk(tree) if isinstance(node, ast.Try) and
            any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and
                child.func.attr == 'cleanup' for statement in node.finalbody for child in ast.walk(statement)))
        sets = [node for statement in protected.body for node in ast.walk(statement)
                if isinstance(node, ast.Call) and any(isinstance(arg, ast.Constant) and arg.value == 'SET'
                                                      for arg in node.args)]
        self.assertEqual(len(sets), 2, 'both initial writes and durable ACK writes require owned cleanup')

    def test_scale_changes_only_product_shard_count(self):
        config = {'redis_mode': 'cluster', 'redis_name': 'redis-gate-cluster-data',
                  'redis_cluster_shards': 3, 'redis_cluster_replicas': 2,
                  'redis_tls_secret': 'owned-tls', 'unrelated': {'preserve': True}}
        original = yaml.safe_dump(config).encode()
        result = yaml.safe_load(gate.scale_input(original, 4))
        self.assertEqual(result.pop('redis_cluster_shards'), 4)
        self.assertEqual(result, {k: v for k, v in config.items() if k != 'redis_cluster_shards'})
        self.assertEqual(yaml.safe_load(original), config)
        for shards in (0, 1, 2, 5):
            with self.assertRaises(ValueError): gate.scale_input(original, shards)
        for key, value in (('redis_mode', 'sentinel'), ('redis_name', 'foreign'),
                           ('redis_cluster_shards', 4), ('redis_cluster_replicas', 1)):
            with self.assertRaises(ValueError):
                gate.scale_input(yaml.safe_dump(dict(config, **{key: value})).encode(), 4)

    def test_migration_ttl_is_checked_against_absolute_expiry(self):
        cluster = Mock()
        cluster.command.side_effect = [b'value', 15000]
        gate.verify_dataset(cluster, {'key': b'value'}, 20000, now=lambda: 5000)
        for replies in ([b'wrong', 15000], [b'value', -1], [b'value', 30000]):
            cluster.command.side_effect = replies
            with self.assertRaises(ValueError):
                gate.verify_dataset(cluster, {'key': b'value'}, 20000, now=lambda: 5000)

    def test_resize_is_owned_by_product_setup_and_complete_case_evidence(self):
        source = (ROOT / 'tests/helpers/redis-cluster-rebalance-gate.py').read_text()
        self.assertIn("'setup', 'redis-cluster-gate', '07', '--tags', 'redis'", source)
        self.assertNotIn("'scale', 'statefulset'", source)
        self.assertIn('--redis-cluster-rebalance', (ROOT / 'tests/run_enterprise_regression.sh').read_text())
        self.assertIn('redis-cluster-rebalance-gate.py', (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text())
        self.assertIn("'cluster-rebalance': ('REDIS-15', 'REDIS_CLUSTER_REBALANCE_PASS')",
                      (ROOT / 'tests/helpers/redis-evidence.py').read_text())


if __name__ == '__main__':
    unittest.main()
