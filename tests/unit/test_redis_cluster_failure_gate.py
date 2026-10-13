"""Failure fixtures must prove current state and bounded waits before live faults."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_cluster_failure_contract', ROOT / 'tests/helpers/redis-cluster-failure-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RedisClusterFailureTests(unittest.TestCase):
    def test_first_failure_probe_includes_both_leader_election_candidates(self):
        probe = (ROOT / 'tests/helpers/redis-cluster-probe.py').read_text()
        self.assertIn("'deployment/redis-operator', '--all-pods=true', '--all-containers'", probe)

    def test_repair_reaches_script_dispatch_and_never_signs_off_failure(self):
        helper = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        allowlist = helper.split('case "$module" in ', 1)[1].split(') ;;', 1)[0].split('|')
        self.assertIn('cluster-repair', allowlist)
        self.assertIn('cluster-primary-fault|cluster-repair) script=redis-cluster-failure-gate.py', helper)
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        self.assertIn('"$MODE" == --redis-cluster-repair', runner)
        evidence = (ROOT / 'tests/helpers/redis-evidence.py').read_text()
        self.assertNotIn("'cluster-repair':", evidence)

    def test_majority_state_requires_a_real_server_field(self):
        self.assertEqual(gate.cluster_state(b'cluster_state:ok\r\ncluster_slots_ok:16384\r\n'), 'ok')
        self.assertEqual(gate.cluster_state(b'cluster_state:fail\r\n'), 'fail')
        for reply in (b'', b'cluster_state:unknown\r\n', b'NOAUTH denied'):
            with self.assertRaises(ValueError): gate.cluster_state(reply)

    def test_fault_disconnects_all_real_client_replication_types(self):
        cluster = Mock()
        gate.disconnect(cluster, 'owned-0')
        self.assertEqual([call.args for call in cluster.client.return_value.command.call_args_list],
            [('CLIENT', 'KILL', 'TYPE', kind, 'SKIPME', 'yes')
             for kind in ('normal', 'pubsub', 'replica', 'master')])

    def test_failed_prerequisite_stops_at_bounded_wait(self):
        check = Mock(return_value=False)
        with patch.object(gate.time, 'monotonic', side_effect=[0, 0, 11]), patch.object(gate.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'replica-restart'):
                gate.wait_until(check, timeout=10, label='replica-restart')
        check.assert_called_once()

    def test_seed_requires_all_shards_and_durable_replica_acknowledgment(self):
        cluster = Mock()
        cluster.run_id = 'fixture'
        cluster.keys = set()
        cluster.owner.side_effect = ['p0', 'p1', 'p2']
        cluster.client.return_value.command.side_effect = [b'OK', [1, 2]] * 3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cluster.cluster = Path(tmp.name)
        cluster.journal = {'cr_uid': 'owned'}
        selected = gate.seed(cluster)
        self.assertEqual(set(selected), {'p0', 'p1', 'p2'})
        self.assertEqual(cluster.keys, set(selected.values()))
        cluster.owner.side_effect = None
        cluster.owner.return_value = 'p0'
        with self.assertRaisesRegex(ValueError, 'three shards'): gate.seed(cluster)
        (cluster.cluster / 'redis-cluster-failure-keys.json').unlink()
        cluster.owner.side_effect = ['p0', 'p1', 'p2']
        cluster.client.return_value.command.side_effect = [b'OK', [1, 1]]
        with self.assertRaisesRegex(ValueError, 'durable'): gate.seed(cluster)

    def test_dispatch_has_dedicated_full_failure_evidence(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        helper = (ROOT / 'tests/helpers/redis-focused-gate.sh').read_text()
        self.assertIn('--redis-cluster-failure', runner)
        self.assertIn('redis-cluster-failure-gate.py', helper)
        self.assertIn("'cluster-failure': ('REDIS-14', 'REDIS_CLUSTER_FAILURE_PASS')",
                      (ROOT / 'tests/helpers/redis-evidence.py').read_text())


if __name__ == '__main__':
    unittest.main()
