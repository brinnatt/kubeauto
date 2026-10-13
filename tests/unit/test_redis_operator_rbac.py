"""The image revision gets node-label discovery without unrelated RBAC changes."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'roles/cluster-addon/files/redis-operator-rbac.py'
spec = importlib.util.spec_from_file_location('redis_operator_rbac', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def chart(version='0.27.0-kubeauto.2'):
    return [
        {'kind': 'Deployment', 'metadata': {'name': 'redis-operator', 'namespace': 'operators'},
         'spec': {'template': {'spec': {'containers': [{'name': 'redis-operator', 'image': 'brinnatt/redis-operator:' + version}]}}}},
        {'kind': 'ClusterRole', 'metadata': {'name': 'redis-operator'},
         'rules': [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list']}]},
        {'kind': 'ClusterRoleBinding', 'metadata': {'name': 'redis-operator'},
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'redis-operator'},
         'subjects': [{'kind': 'ServiceAccount', 'name': 'redis-operator', 'namespace': 'operators'}]},
        {'kind': 'ClusterRole', 'metadata': {'name': 'foreign'}, 'rules': []},
    ]


class RedisOperatorRBACTests(unittest.TestCase):
    def test_helm_wrapper_uses_explicit_ansible_python_with_no_system_dependency(self):
        wrapper = SCRIPT.with_suffix('.sh')
        result = subprocess.run(['bash', str(wrapper)], input=yaml.safe_dump_all(chart()),
                                env={**os.environ, 'REDIS_POST_RENDER_PYTHON': sys.executable},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(yaml.safe_load_all(result.stdout)), module.node_discovery(chart()))
        for interpreter in ('', '/missing-python'):
            result = subprocess.run(['bash', str(wrapper)], input=yaml.safe_dump_all(chart()),
                                    env={**os.environ, 'REDIS_POST_RENDER_PYTHON': interpreter},
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.stdout)
        tasks = yaml.safe_load((ROOT / 'roles/cluster-addon/tasks/redis.yml').read_text())
        installation = next(task for task in tasks if task['name'] == '安装或升级 Redis Operator')
        self.assertEqual(installation['environment']['REDIS_POST_RENDER_PYTHON'], '{{ ansible_playbook_python }}')

    def test_only_list_nodes_is_added_to_current_operator_role_and_is_idempotent(self):
        documents = chart()
        original = copy.deepcopy(documents)
        result = module.node_discovery(documents)
        self.assertEqual(result[1]['rules'], original[1]['rules'] + [
            {'apiGroups': [''], 'resources': ['nodes'], 'verbs': ['list']}])
        for index in (0, 2, 3):
            self.assertEqual(result[index], original[index])
        self.assertEqual(module.node_discovery(copy.deepcopy(result)), result)

    def test_legacy_and_original_operator_permissions_remain_exact(self):
        for version in ('0.26.0', '0.27.0', '0.27.0-kubeauto.1'):
            documents = chart(version)
            self.assertEqual(module.node_discovery(copy.deepcopy(documents)), documents)

    def test_foreign_bindings_namespace_scope_and_broad_node_rules_are_rejected(self):
        for defect in ('role', 'namespace', 'subject', 'broad', 'duplicate'):
            documents = chart()
            if defect == 'role':
                documents[1]['kind'] = 'Role'
            elif defect == 'namespace':
                documents[2]['subjects'][0]['namespace'] = 'foreign'
            elif defect == 'subject':
                documents[2]['subjects'][0]['name'] = 'foreign'
            elif defect == 'broad':
                documents[1]['rules'].append({'apiGroups': [''], 'resources': ['nodes'], 'verbs': ['*']})
            else:
                documents.append(copy.deepcopy(documents[1]))
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                module.node_discovery(documents)

    def test_actual_cli_contract_preserves_stdout_and_rejects_bad_input(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], input=yaml.safe_dump_all(chart()),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(yaml.safe_load_all(result.stdout)), module.node_discovery(chart()))
        for malformed in ('', '- invalid\n', '[broken', 'kind: Deployment\n'):
            result = subprocess.run([sys.executable, str(SCRIPT)], input=malformed,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.stdout)
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
