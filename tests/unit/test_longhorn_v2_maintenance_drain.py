"""Explicit V2 profile preserves default V1 behavior and exact raw-class checks."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import yaml
from tests.unit.test_longhorn_v2 import v2_configuration

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_drain_contract', ROOT/'tests/helpers/longhorn-v2-maintenance-drain.py')
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


class ProfileContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = v2_configuration(); self.save()
        self.storage = {'provisioner': 'driver.longhorn.io', 'parameters': {'dataEngine': 'v2', 'numberOfReplicas': '3'}, 'reclaimPolicy': 'Retain'}
        self.lab = SimpleNamespace(DIRECTORY=self.directory, get=Mock(return_value=self.storage))

    def save(self): (self.directory/'config.yml').write_text(yaml.safe_dump(self.config))

    def test_profile_binds_exact_class_engine_and_separate_journals(self):
        module = M.profile(self.lab)
        self.assertEqual('v2', module.DATA_ENGINE)
        self.assertEqual(self.config['longhorn_storage_classes'][0]['name'], module.STORAGE_CLASS)
        self.assertEqual('longhorn-v2-maintenance-drain.json', module.RECORD)
        self.assertEqual('longhorn-v2-maintenance-drain-business.json', module.BUSINESS)
        default = M.load('longhorn-maintenance.py')
        self.assertEqual(('v1', 'longhorn', 'longhorn-maintenance.json'), (default.DATA_ENGINE, default.STORAGE_CLASS, default.RECORD))

    def test_disabled_engine_or_unfinished_product_request_is_rejected(self):
        for update in ({'longhorn_data_engines': ['v1','v2']}, {'longhorn_requests': [{'kind': 'snapshot'}]}):
            with self.subTest(update=update):
                original = dict(self.config); self.config.update(update); self.save()
                with self.assertRaisesRegex(ValueError, 'idle V2-only'): M.profile(self.lab)
                self.config = original

    def test_wrong_actual_class_or_unsupported_declared_profile_is_rejected(self):
        for field, value in (('dataEngine','v1'), ('numberOfReplicas','1')):
            with self.subTest(field=field):
                previous = self.storage['parameters'][field]; self.storage['parameters'][field] = value
                with self.assertRaisesRegex(ValueError, 'differs from product'): M.profile(self.lab)
                self.storage['parameters'][field] = previous
        self.config['longhorn_storage_classes'][0]['encrypted'] = True; self.save()
        with self.assertRaisesRegex(ValueError, 'explicit three-replica'): M.profile(self.lab)

    def test_v2_preparation_checks_volume_engine_and_original_raw_replicas(self):
        business = {'volume': 'native'}; native = {'spec': {'dataEngine': 'v2'}}
        shared = SimpleNamespace(prepare=Mock(return_value=(business, Mock())))
        self.lab.get.side_effect = lambda kind,*a: native if kind == 'volumes.longhorn.io' else self.storage
        self.lab.healthy_replicas = Mock(return_value={'original': True})
        self.lab.wait = lambda label,timeout,fn: fn()
        with patch.object(M,'load',return_value=shared): module = M.profile(self.lab)
        module.prepare(self.lab, {})
        self.lab.healthy_replicas.assert_called_once_with('native')
        native['spec']['dataEngine'] = 'v1'
        with self.assertRaisesRegex(ValueError, 'another volume engine'): module.prepare(self.lab,{})

    def test_empty_checks_select_own_engine_qualification(self):
        module = M.profile(self.lab)
        upstream = SimpleNamespace(volume_baseline=Mock(), empty=Mock())
        self.lab.load_helper = Mock(return_value=upstream)
        module.empty(self.lab)
        self.lab.load_helper.assert_called_once_with('longhorn-v2-install.py')
        upstream.volume_baseline.assert_called_once_with(self.lab)
        upstream.empty.assert_not_called()


if __name__ == '__main__': unittest.main()
