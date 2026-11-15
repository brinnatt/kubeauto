"""Protect delivered Longhorn behavior while adding the selected patch."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

from common.constants import KubeConstant
from tests.unit.test_longhorn_delivery import configuration, product, FakeAPI
from tests.unit.test_longhorn_v2 import v2_configuration


ROOT = Path(__file__).resolve().parents[2]


class LonghornPatchContracts(unittest.TestCase):
    def test_new_patch_is_explicitly_supported_but_future_and_prerelease_refused(self):
        for version in ('1.12.0', '1.12.1', '1.12.2', '1.12.3', '1.13.0', '1.12.2-rc1'):
            with self.subTest(version=version):
                c = configuration(); c['longhorn_version'] = version
                if version in ('1.12.0', '1.12.1', '1.12.2'):
                    product.validate(c)
                else:
                    with self.assertRaises(ValueError):
                        product.validate(c)

    def test_official_chart_checksum_and_exact_thirteen_image_composition(self):
        path = ROOT / 'roles/longhorn/files/longhorn-1.12.2.tgz'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), product.CHART_HASHES['1.12.2'])
        with tarfile.open(path) as tar:
            metadata = yaml.safe_load(tar.extractfile('longhorn/Chart.yaml'))
            self.assertEqual((metadata['version'], metadata['appVersion']), ('1.12.2', 'v1.12.2'))
            official = yaml.safe_load(metadata['annotations']['helm.sh/images'])
        tags = KubeConstant.longhorn_image_tags('1.12.2')
        self.assertEqual(len(tags), len(official))
        self.assertEqual(len(tags), 13)
        names = {'backing-image-manager': 'longhorn-backing-image-manager',
                 'support-bundle-kit': 'longhorn-support-bundle-kit'}
        for row in official:
            name = names.get(row['name'], row['name'])
            if not name.startswith('longhorn-'):
                name = 'longhorn-' + name
            expected = row['image'].rsplit(':', 1)[1]
            if name in ('longhorn-manager', 'longhorn-instance-manager'):
                expected += '-kubeauto.1'
            self.assertEqual(tags[name], expected, name)
        mapping = json.loads((ROOT / 'roles/longhorn/files/images.json').read_text())
        self.assertEqual(mapping['1.12.2'], tags)

    def test_source_pins_and_default_remain_unchanged(self):
        mapping = json.loads((ROOT / 'roles/longhorn/files/images.json').read_text())
        for version in ('1.12.0', '1.12.1'):
            self.assertEqual(mapping[version], KubeConstant.longhorn_image_tags(version))
        self.assertEqual(KubeConstant().v_longhorn, '1.12.1')
        self.assertEqual(mapping['1.12.1']['longhorn-manager'], 'v1.12.1-kubeauto.2')
        self.assertEqual(mapping['1.12.1']['longhorn-instance-manager'], 'v1.12.1-kubeauto.3')

    def test_target_chart_renders_actual_v1_v2_pairing_and_preserves_drain_protection(self):
        helm = shutil.which('helm')
        if not helm:
            self.skipTest('project-pinned Helm v3.19.0 required for target render')
        for configure in (configuration, v2_configuration):
            with self.subTest(profile=configure.__name__), tempfile.TemporaryDirectory() as directory:
                c = configure(); c['longhorn_version'] = '1.12.2'
                values = product.chart_values(c)
                self.assertEqual(values['defaultSettings']['nodeDrainPolicy'], 'block-if-contains-last-replica')
                path = Path(directory) / 'values.yaml'
                path.write_text(yaml.safe_dump(values))
                result = subprocess.run([helm, 'template', 'longhorn',
                    str(ROOT / 'roles/longhorn/files/longhorn-1.12.2.tgz'),
                    '--namespace', product.NAMESPACE, '--values', str(path)],
                    capture_output=True, text=True, timeout=30, check=True)
                objects = [o for o in yaml.safe_load_all(result.stdout) if o]
                self.assertTrue(any(o['kind'] == 'DaemonSet' and
                    o['metadata']['name'] == 'longhorn-manager' for o in objects))
                for name, tag in KubeConstant.longhorn_image_tags('1.12.2').items():
                    self.assertIn(c['longhorn_image_prefix'] + '/' + name + ':' + tag, result.stdout)
                self.assertTrue(values['preUpgradeChecker']['upgradeVersionCheck'])
                self.assertFalse(values['defaultSettings']['allowVolumeCreationWithDegradedAvailability'])

    def test_v2_patch_preserves_cpu_isolation_iobuf_and_native_policy(self):
        c = v2_configuration()
        c.update(longhorn_version='1.12.2', longhorn_v2_cpu_cores=2,
                 longhorn_v2_cpu_isolation_enabled=True, longhorn_v2_iobuf_large_pool_size=4096)
        product.validate(c)
        values = product.chart_values(c)['defaultSettings']
        for name, expected in (('dataEngineNumberOfCPUCores', '2'),
                               ('dataEngineIobufLargePoolSize', '4096'),
                               ('dataEngineIobufSmallPoolSize', '8192'),
                               ('dataEngineCPUIsolationEnabled', 'true')):
            self.assertEqual(values[name], {'v2': expected})
        del c['longhorn_v2_cpu_isolation_enabled']
        api = FakeAPI()
        api.put('settings.longhorn.io', {'metadata': {'name': 'data-engine-cpu-isolation-enabled'},
                                       'value': '{"v2":"true"}'}, product.NAMESPACE)
        self.assertTrue(product.v2_cpu_isolation(c, api, {'spec': {}}))
        self.assertFalse(product.v2_cpu_isolation(c, api, {
            'spec': {'dataEngineSpec': {'v2': {'cpuIsolationEnabled': 'false'}}}}))
        self.assertEqual(api.writes, [])

    def test_patch_downgrade_is_refused_before_chart_or_any_api_write(self):
        for target in ('1.12.0', '1.12.1'):
            with self.subTest(target=target):
                c = configuration(); c.update(longhorn_version=target, longhorn_snapshot_controller='disabled')
                api = FakeAPI()
                release = {'name': 'longhorn', 'namespace': product.NAMESPACE,
                           'chart': 'longhorn-1.12.2', 'app_version': '1.12.2'}
                with patch.object(product, 'run', return_value=json.dumps([release])) as run, \
                     patch.object(product, 'check_installation_identity'), \
                     patch.object(product, 'node_map', return_value={}), patch.object(product, 'check_secrets'):
                    with self.assertRaisesRegex(ValueError, 'downgrade'):
                        product.preflight(c, api, 'helm', '/unused')
                self.assertEqual(run.call_count, 1)
                self.assertEqual(api.writes, [])

    def test_v2_attached_volume_refused_during_patch_update(self):
        c = configuration()
        c.update(longhorn_version='1.12.2', longhorn_data_engines=['v2'],
                 longhorn_upgrade_id='first-hop', longhorn_upgrade_system_backup='before-patch',
                 longhorn_snapshot_controller='disabled')
        api = FakeAPI()
        api.put('systembackups.longhorn.io', {'metadata': {'name': 'before-patch'},
                'status': {'state': 'Ready', 'version': 'v1.12.1'}}, product.NAMESPACE)
        api.listing('volumes.longhorn.io', [{'spec': {'dataEngine': 'v2', 'nodeID': 'worker'},
                    'status': {'robustness': 'healthy', 'state': 'attached'}}], product.NAMESPACE)
        release = {'name': 'longhorn', 'namespace': product.NAMESPACE,
                   'chart': 'longhorn-1.12.1', 'app_version': '1.12.1'}
        with patch.object(product, 'run', return_value=json.dumps([release])) as run, \
             patch.object(product, 'check_installation_identity'), \
             patch.object(product, 'node_map', return_value={}), patch.object(product, 'check_secrets'):
            with self.assertRaisesRegex(ValueError, 'requires detached'):
                product.preflight(c, api, 'helm', '/unused')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(api.writes, [])


if __name__ == '__main__':
    unittest.main()
