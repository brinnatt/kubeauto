"""Grow-only planning and exact bounded-pressure fixture cleanup contracts."""
import ast
import copy
import importlib.util
from pathlib import Path
import subprocess
import re
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tests/helpers/redis-capacity-gate.py'
spec = importlib.util.spec_from_file_location('redis_capacity', SCRIPT)
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class RedisCapacityTests(unittest.TestCase):
    def test_memory_restore_requires_target_template_rollout_and_actual_runtime(self):
        gate = Mock(name='gate'); gate.name = 'redis-gate-data'; gate.ns = 'redis-gate'
        container = {'name':gate.name, 'env':[{'name':'REDIS_MAX_MEMORY', 'value':'375809638'}],
                     'resources':{'limits':{'memory':'512Mi'}}}
        sts = {'metadata':{'name':gate.name, 'namespace':gate.ns, 'uid':'owner', 'generation':2},
               'spec':{'replicas':3, 'template':{'spec':{'containers':[container]}}},
               'status':{'observedGeneration':2, 'currentRevision':'new', 'updateRevision':'new',
                         'readyReplicas':3, 'updatedReplicas':3}}
        pods = [{'metadata':{'name':gate.name+'-'+str(i), 'namespace':gate.ns,
                             'labels':{'controller-revision-hash':'new'},
                             'ownerReferences':[{'kind':'StatefulSet', 'uid':'owner', 'name':gate.name}]},
                 'spec':{'containers':[container]},
                 'status':{'phase':'Running', 'conditions':[{'type':'Ready', 'status':'True'}]}}
                for i in range(3)]
        config = {'redis_memory_limit':'512Mi', 'redis_maxmemory_percent':70}
        gate.get.return_value = sts; gate.members.return_value = {'replication':pods}
        gate.client.return_value.command.return_value = [b'maxmemory', b'375809638']
        self.assertTrue(capacity.memory_converged(gate, config))
        self.assertEqual(gate.client.call_count, 3)
        for change in ('old-template', 'old-pod', 'unobserved', 'rolling', 'not-ready', 'runtime', 'foreign'):
            with self.subTest(change=change):
                current = copy.deepcopy(sts); members = copy.deepcopy(pods)
                if change == 'old-template': current['spec']['template']['spec']['containers'][0]['env'][0]['value'] = '268435456'
                elif change == 'old-pod': members[0]['spec']['containers'][0]['env'][0]['value'] = '268435456'
                elif change == 'unobserved': current['status']['observedGeneration'] = 1
                elif change == 'rolling': current['status']['currentRevision'] = 'old'
                elif change == 'not-ready': members[0]['status']['conditions'][0]['status'] = 'False'
                elif change == 'foreign': members[0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                gate.get.return_value = current; gate.members.return_value = {'replication':members}
                gate.client.return_value.command.return_value = [b'maxmemory', b'1' if change == 'runtime' else b'375809638']
                self.assertFalse(capacity.memory_converged(gate, config))

    def test_dns_fixture_uses_actual_verified_resolver_and_refuses_unknown_paths(self):
        self.assertEqual(capacity.resolver_addresses('search cluster.local\nnameserver 169.254.20.10\n',
                                                   '169.254.20.10'), ('169.254.20.10',))
        for text in ('', 'nameserver 10.84.0.2\n', 'nameserver 169.254.20.10\nnameserver 1.1.1.1\n',
                     'nameserver 169.254.20.10\nnameserver 169.254.20.10\n'):
            with self.assertRaises(ValueError):
                capacity.resolver_addresses(text, '169.254.20.10')

    def test_replica_fault_kills_actual_upstream_link_and_refuses_unproven_fault(self):
        client = Mock()
        client.command.return_value = 1
        capacity.sever_replication(client)
        client.command.assert_called_once_with('CLIENT', 'KILL', 'TYPE', 'master', 'SKIPME', 'yes')
        for result in (0, None, b'OK'):
            client.command.return_value = result
            with self.assertRaises(ValueError):
                capacity.sever_replication(client)

    def test_config_replacement_preserves_mode_and_refuses_concurrent_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.yml'
            path.write_bytes(b'original')
            path.chmod(0o600)
            capacity.replace_config(path, [b'original'], b'grown')
            self.assertEqual(path.read_bytes(), b'grown')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                capacity.replace_config(path, [b'original'], b'overwrite')
            self.assertEqual(path.read_bytes(), b'grown')
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_growth_planning_refuses_foreign_claims_or_excessive_lab_capacity(self):
        claim = {'metadata': {'namespace': 'redis-gate', 'labels': {
            'kubeauto.io/component': 'redis', 'kubeauto.io/managed-by': 'kubeauto'}},
            'spec': {'storageClassName': 'redis-longhorn', 'resources': {'requests': {'storage': '2Gi'}}}}
        self.assertEqual(capacity.growth_target(claim, 'redis-gate'), '3Gi')
        for edit in (lambda c: c['metadata'].update(namespace='customer'),
                     lambda c: c['metadata']['labels'].update({'kubeauto.io/managed-by': 'foreign'}),
                     lambda c: c['spec'].update(storageClassName='foreign'),
                     lambda c: c['spec']['resources']['requests'].update(storage='5Gi')):
            bad = copy.deepcopy(claim)
            edit(bad)
            with self.assertRaises(ValueError):
                capacity.growth_target(bad, 'redis-gate')

    def test_pressure_cleanup_refuses_foreign_owner_symlink_and_only_removes_owned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pressure'
            owner = Path(str(path) + '.owner')
            path.write_bytes(b'bounded-data')
            owner.write_text('foreign')
            def remove():
                return subprocess.run(['sh', '-ec', capacity.PRESSURE_REMOVE, 'pressure', str(path), 'owned'],
                                      capture_output=True).returncode
            self.assertNotEqual(remove(), 0)
            self.assertTrue(path.exists())
            owner.write_text('owned')
            original = Path(tmp) / 'foreign-data'
            original.write_bytes(b'preserve')
            path.unlink()
            path.symlink_to(original)
            self.assertNotEqual(remove(), 0)
            self.assertTrue(path.is_symlink())
            self.assertEqual(original.read_bytes(), b'preserve')
            path.unlink()
            path.write_bytes(b'bounded-data')
            self.assertEqual(remove(), 0)
            self.assertFalse(path.exists())
            self.assertFalse(owner.exists())
            self.assertEqual(remove(), 0)
            path.symlink_to(Path(tmp) / 'missing')
            self.assertNotEqual(remove(), 0)
            self.assertTrue(path.is_symlink())

    def test_capacity_is_reachable_only_through_static_guarded_product_runner(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        start = runner.index('if [[ "$MODE" == --redis-sentinel-data')
        end = runner.index('if [[ "$MODE" == "--redis-install-watch"', start)
        branch = runner[start:end]
        self.assertIn('"$MODE" == --redis-capacity', branch)
        patterns = re.findall(r'^\s+(sentinel-data\|[^)\n]+)\)', branch, re.M)
        self.assertEqual(len(patterns), 1)
        self.assertIn('capacity', patterns[0].split('|'))
        self.assertLess(branch.index('redis-static.sh'), branch.index('nohup'))
        static = (ROOT / 'tests/helpers/redis-static.sh').read_text()
        self.assertIn('capacity-gate', static)
        self.assertIn('test_redis_capacity_gate', static)
        self.assertIn('gate.isolated([replica], dns_addresses=resolvers)', SCRIPT.read_text())

    def test_inline_container_scripts_parse_before_live_execution(self):
        count = 0
        for node in ast.walk(ast.parse(SCRIPT.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for index, arg in enumerate(node.args):
                if isinstance(arg, ast.Constant) and arg.value == '-ec' and index + 1 < len(node.args):
                    following = node.args[index + 1]
                    code = following.value if isinstance(following, ast.Constant) else capacity.PRESSURE_REMOVE
                    result = subprocess.run(['sh', '-n', '-c', code], capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    count += 1
        self.assertGreaterEqual(count, 3)
        tree = ast.parse(SCRIPT.read_text())
        mutations = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                     isinstance(n.func, ast.Attribute) and n.func.attr == 'k' and n.args and
                     isinstance(n.args[0], ast.Constant) and n.args[0].value in ('replace', 'patch')]
        self.assertEqual(mutations, [], 'PVC expansion must use config.yml plus kubecli, not imperative PVC edits')


if __name__ == '__main__':
    unittest.main()
