"""Cleanup qualification must reach the real CLI and preserve exact fixture identity."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_cleanup_qualification_contract', ROOT / 'tests/helpers/redis-cleanup-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class CleanupTests(unittest.TestCase):
    def test_qualification_prefix_and_owned_foreign_boundaries_are_explicit(self):
        owned = gate.namespace('redis-clean-owned', 'run')
        foreign = gate.namespace('redis-clean-canary', 'run', False)
        self.assertEqual(owned['metadata']['labels']['kubeauto.io/component'], 'redis')
        self.assertEqual(foreign['metadata']['labels']['kubeauto.io/component'], 'redis-clean-canary')
        with self.assertRaises(ValueError):
            gate.namespace('customer-namespace', 'run')

    def test_fixture_deletion_uses_exact_namespace_uid_resource_version_and_run_identity(self):
        source = Mock()
        source.run_id = 'owned'
        obj = gate.namespace('redis-clean-owned', 'owned')
        obj['metadata'].update(uid='uid', resourceVersion='revision')
        gate.exact_delete(source, obj)
        args, kwargs = source.k.call_args
        self.assertEqual(args[:3], ('delete', '--raw', '/api/v1/namespaces/redis-clean-owned'))
        self.assertEqual(json.loads(kwargs['input_text'])['preconditions'], {'uid':'uid', 'resourceVersion':'revision'})
        source.k.reset_mock()
        obj['metadata']['labels']['kubeauto.io/test-run'] = 'foreign'
        with self.assertRaises(ValueError):
            gate.exact_delete(source, obj)
        source.k.assert_not_called()

    def test_interruption_reaches_real_deletion_and_then_resumes_same_cli(self):
        source = (ROOT / 'tests/helpers/redis-cleanup-gate.py').read_text()
        self.assertIn("['bash', str(self.script)]", source)
        self.assertIn("obj['status']['phase'] == 'Terminating'", source)
        self.assertIn('os.killpg(self.process.pid, signal.SIGTERM)', source)
        self.assertNotIn('pkill', source)
        self.assertNotIn('--force', source)
        self.assertLess(source.index("obj['status']['phase'] == 'Terminating'"),
                        source.index('os.killpg(self.process.pid, signal.SIGTERM)'))
        self.assertIn("result = self.cli()", source[source.index('self.release_blocker()'):])
