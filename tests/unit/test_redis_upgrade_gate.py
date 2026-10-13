"""Pinned migration inputs and local immutable aliases before a version gate."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_upgrade_contract', ROOT / 'tests/helpers/redis-upgrade-gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class UpgradeTests(unittest.TestCase):
    def test_prepare_legacy_controller_retains_current_schema_instead_of_downgrading_crds(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = object.__new__(gate.Upgrade)
            instance.journal = Path(tmp) / 'fault.json'
            instance.source = Mock(run_id='a' * 32)
            instance.original = b'original config'
            instance.prepare_artifacts = Mock()
            instance.declare = Mock()
            instance.business = Mock()
            instance.execute(True)
            self.assertEqual(instance.declare.call_args.args[0], gate.LEGACY)
            self.assertFalse(instance.declare.call_args.kwargs.get('crd', False))
            self.assertEqual(instance.business.call_count, 2)

    def test_prepare_seeds_durable_current_business_before_legacy_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = object.__new__(gate.Upgrade)
            instance.journal = Path(tmp) / 'fault.json'
            instance.source = Mock(run_id='a' * 32)
            instance.original = b'original config'
            instance.prepare_artifacts = Mock()
            instance.business = Mock()
            def check_seed(*args, **kwargs):
                instance.business.assert_called_once_with()
                self.assertTrue(instance.seeded)
            instance.declare = Mock(side_effect=check_seed)
            instance.execute(True)

    def test_migration_changes_only_the_qualified_versions_and_explicit_schema_request(self):
        config = {'redis_namespace':'redis-gate', 'redis_mode':'sentinel', 'redis_name':'redis-gate-data',
                  'redis_storage_class':'redis-longhorn', 'redis_password_secret':'owned',
                  'redis_crd_upgrade_requested':False, 'redis_ver':'8.10.2'}
        result = yaml.safe_load(gate.version_input(yaml.safe_dump(config), gate.LEGACY, True))
        self.assertEqual(result['redis_password_secret'], 'owned')
        self.assertEqual(result['redis_storage_class'], 'redis-longhorn')
        self.assertTrue(result['redis_crd_upgrade_requested'])
        self.assertEqual(result['redis_ver'], '8.10.1')
        for bad in (dict(config, redis_namespace='foreign'), dict(config, redis_mode='cluster')):
            with self.assertRaises(ValueError):
                gate.version_input(yaml.safe_dump(bad), gate.LEGACY)
        for versions in ({'redis_ver':'latest'}, {'redis_operator_ver':'0.25.0'}, {'unrelated':'value'}):
            with self.assertRaises(ValueError):
                gate.version_input(yaml.safe_dump(config), versions)

    def test_fixture_aliases_use_published_exact_child_digests_and_never_change_upstream_refs(self):
        evidence = json.loads((ROOT / 'tests/evidence/redis-images-20261012.json').read_text())
        before = deepcopy(evidence)
        rows = gate.fixture_aliases(evidence)
        self.assertEqual(evidence, before)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[1] for r in rows}, {'127.0.0.1:5000/brinnatt/redis:8.10.1',
                                             '127.0.0.1:5000/brinnatt/redis-operator:0.26.0'})
        for origin, target, expected in rows:
            self.assertTrue(origin.startswith('hub.talkedu.cn/kubeauto/'))
            self.assertTrue(origin.endswith('@' + expected))
        fixture = next(r for r in evidence['images'] if 'redis-upgrade-fixture' in r['key'])
        for mutate in (lambda e: e.update(durable_rc=1), lambda e: e['images'].remove(fixture),
                       lambda e: e['images'].append(deepcopy(fixture))):
            bad = deepcopy(evidence)
            mutate(bad)
            with self.assertRaises(ValueError):
                gate.fixture_aliases(bad)

    def test_rollback_does_not_apply_legacy_crd_and_source_keys_remain_until_current_revalidated(self):
        source = (ROOT / 'tests/helpers/redis-upgrade-gate.py').read_text()
        lines = [line for line in source.splitlines() if 'self.declare(' in line and 'SUPPORTED_ROLLBACK' in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all('crd=True' not in line for line in lines))
        cleanup = source[source.index('    def cleanup(self):'):]
        self.assertLess(cleanup.index('self.declare(CURRENT, crd=True)'), cleanup.index('self.source.cleanup_keys()'))
        self.assertLess(cleanup.index('self.source.cleanup_keys()'), cleanup.index('self.journal.unlink()'))
        self.assertIn("'setup', 'redis-gate', '07', '--tags', 'redis'", source)
        self.assertNotIn('helm upgrade', source)
