"""Disprove foreign node, weak PDB and rolling readiness shortcuts before live drain."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_maintenance_contract', ROOT / 'tests/helpers/redis-maintenance-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def spare():
    return {'metadata': {'name': 'redis-node-134', 'uid': 'owned', 'labels': {}},
            'spec': {'unschedulable': True, 'taints': [{'key':'node-role.kubernetes.io/control-plane', 'effect':'NoSchedule'}]},
            'status': {'addresses': [{'type':'InternalIP', 'address':'192.168.47.134'}],
                       'conditions': [{'type':'Ready', 'status':'True'}]}}


def budgets():
    return [{'metadata': {'name': app}, 'spec': {'minAvailable': 2, 'selector': {'matchLabels': {'app': app}}}}
            for app in ('redis-gate-data', 'redis-gate-data-sentinel-sentinel')]


class MaintenanceTests(unittest.TestCase):
    def test_longhorn_delete_waits_for_manager_status_and_zero_engines_and_replicas(self):
        for blocked in ('pods', 'ready', 'reason', 'scheduling', 'replicas', 'engines'):
            with self.subTest(blocked=blocked):
                instance = object.__new__(gate.Maintenance)
                instance.source = Mock()
                rounds = [0]
                def command(*args):
                    first = rounds[0] == 0
                    if 'pods' in args:
                        rounds[0] += 1
                        first = rounds[0] == 1
                        return json.dumps({'items': [{}] if first and blocked == 'pods' else []})
                    first = rounds[0] == 1
                    if 'nodes.longhorn.io' in args:
                        return json.dumps({'spec': {'allowScheduling': first and blocked == 'scheduling'},
                            'status': {'conditions': [{'type':'Ready',
                                'status':'True' if first and blocked == 'ready' else 'False',
                                'reason':'Foreign' if first and blocked == 'reason' else 'ManagerPodMissing'}]}})
                    kind = 'replicas' if 'replicas.longhorn.io' in args else 'engines'
                    return json.dumps({'items': [{'spec': {'nodeID':'redis-node-134'}}]
                                       if first and blocked == kind else []})
                instance.source.k.side_effect = command
                with patch.object(gate.time, 'sleep') as sleep:
                    instance.wait_spare_removable()
                self.assertEqual(rounds[0], 2)
                sleep.assert_called_once_with(2)

    def test_repair_refuses_foreign_uid_and_symlink_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = object.__new__(gate.Maintenance)
            instance.journal = Path(tmp) / 'journal.json'
            instance.cluster = SimpleNamespace(journal={'cluster_uid':'cluster'})
            instance.source = Mock()
            state = {'run_id':'a' * 32, 'cluster_uid':'cluster', 'worker_uid':'worker',
                     'spare': {'uid':'spare', 'unschedulable':True, 'taints':[],
                               'host': {'installed':'no', 'active':'inactive'}}}
            instance.journal.write_text(json.dumps(state))
            instance.source.k.return_value = json.dumps({'metadata': {'uid':'foreign'},
                'status': {'addresses':[{'type':'InternalIP', 'address':'192.168.47.131'}]}})
            with self.assertRaisesRegex(ValueError, 'changed node identity'):
                instance.repair_identity()
            self.assertTrue(all(call.args[0] == 'get' for call in instance.source.k.call_args_list))
            target = Path(tmp) / 'target'; instance.journal.rename(target)
            instance.journal.symlink_to(target)
            instance.source.k.reset_mock()
            with self.assertRaisesRegex(ValueError, 'regular owned journal'):
                instance.repair_identity()
            instance.source.k.assert_not_called()

    def test_restore_checks_upstream_preconditions_before_node_deletion(self):
        source = (ROOT / 'tests/helpers/redis-maintenance-gate.py').read_text()
        restore = source.split('    def restore(self):', 1)[1]
        self.assertLess(restore.index('self.wait_spare_removable()'),
                        restore.index("'delete', 'nodes.longhorn.io'"))

    def test_cordoned_product_master_has_the_official_controller_unschedulable_taint(self):
        node = spare()
        taint = {'key':'node.kubernetes.io/unschedulable', 'effect':'NoSchedule',
                 'timeAdded':'2026-10-12T14:08:40Z'}
        node['spec']['taints'] = [taint]
        self.assertEqual(gate.spare_contract(node)['taints'], [taint])
        node['spec']['unschedulable'] = False
        with self.assertRaises(ValueError):
            gate.spare_contract(node)

    def test_spare_is_exact_healthy_leased_master_and_preserves_prior_scheduling(self):
        node = spare()
        before = deepcopy(node)
        prior = gate.spare_contract(node)
        self.assertEqual(node, before)
        self.assertTrue(prior['unschedulable'])
        self.assertEqual(prior['taints'], before['spec']['taints'])
        for mutate in (lambda n: n['metadata'].update(name='redis-node-131'),
                       lambda n: n['status']['addresses'][0].update(address='192.168.47.129'),
                       lambda n: n['metadata']['labels'].update({'kubeauto.io/redis-storage':'true'}),
                       lambda n: n['spec']['taints'][0].update(key='foreign'),
                       lambda n: n['status']['conditions'][0].update(status='False')):
            bad = deepcopy(node)
            mutate(bad)
            with self.assertRaises(ValueError):
                gate.spare_contract(bad)

    def test_both_sentinel_budgets_are_required_and_cannot_be_weakened(self):
        self.assertEqual(len(gate.pdb_contract(budgets())), 2)
        for mutate in (lambda p: p.pop(), lambda p: p.append(deepcopy(p[0])),
                       lambda p: p[0]['spec'].update(minAvailable=1),
                       lambda p: p[0]['spec']['selector']['matchLabels'].update(app='foreign')):
            bad = budgets()
            mutate(bad)
            with self.assertRaises(ValueError):
                gate.pdb_contract(bad)

    def test_rolling_gate_rejects_two_unavailable_members_before_accepting_replacements(self):
        instance = object.__new__(gate.Maintenance)
        instance.source = Mock()
        instance.source.ns = 'redis-gate'
        pods = [{'metadata': {'name':f'data-{i}', 'uid':f'old-{i}', 'labels': {'app':'data'}},
                 'status': {'conditions': [{'type':'Ready', 'status':'True'}]}} for i in range(3)]
        broken = deepcopy(pods)
        for pod in broken[:2]:
            pod['status']['conditions'][0]['status'] = 'False'
        instance.source.members.side_effect = [{'replication':pods, 'sentinel':pods}, {'replication':broken}]
        instance.source.get.return_value = {'spec': {'updateStrategy': {'type':'RollingUpdate'}}}
        instance.verify = Mock()
        with self.assertRaisesRegex(ValueError, 'two-of-three'):
            instance.rolling()
        instance.verify.assert_not_called()

    def test_drain_is_eviction_based_and_host_cleanup_follows_controller_replica_evacuation(self):
        source = (ROOT / 'tests/helpers/redis-maintenance-gate.py').read_text()
        self.assertNotIn('--disable-eviction', source)
        self.assertNotIn('--force', source)
        self.assertLess(source.index('evictionRequested'), source.index('rm -rf --'))
        self.assertLess(source.index('self.storage.health()'), source.index('self.journal.unlink()'))
