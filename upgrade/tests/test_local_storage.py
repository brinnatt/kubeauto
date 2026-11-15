"""Qualify the changed helper without changing existing volume identities."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import jinja2
import yaml

from common.constants import KubeConstant


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = 'roles/cluster-addon/templates/local-storage/local-path-storage.yaml.j2'
SOURCE_SHA = '940c9020144a88b370cba36793a4d7fad6a347ab'


class LocalStorageContracts(unittest.TestCase):
    def render(self, version, original=None):
        text = original if original is not None else (ROOT/TEMPLATE).read_text()
        return {o['kind']: o for o in yaml.safe_load_all(
            jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text).render(
                local_path_provisioner_ver=version, local_path_storage_class='local-path',
                local_path_provisioner_dir='/opt/local-path-provisioner')) if o}

    def test_candidate_only_changes_provisioner_and_helper_images(self):
        original = subprocess.check_output(['git', 'show', SOURCE_SHA+':'+TEMPLATE],
                                           cwd=ROOT, text=True)
        source = self.render('v0.0.31', original)
        target = self.render('v0.0.37')
        for kind in source:
            if kind == 'Deployment':
                source[kind]['spec']['template']['spec']['containers'][0]['image'] = (
                    'registry.talkschool.cn:5000/brinnatt/local-path-provisioner:v0.0.37')
            if kind == 'ConfigMap':
                old = yaml.safe_load(source[kind]['data']['helperPod.yaml'])
                new = yaml.safe_load(target[kind]['data']['helperPod.yaml'])
                old['spec']['containers'][0]['image'] = new['spec']['containers'][0]['image']
                self.assertEqual(old, new)
                source[kind]['data']['helperPod.yaml'] = target[kind]['data']['helperPod.yaml']
        self.assertEqual(source, target)

    def test_helper_is_fixed_shell_image_and_inventory_contains_it(self):
        helper = yaml.safe_load(self.render('v0.0.37')['ConfigMap']['data']['helperPod.yaml'])
        image = helper['spec']['containers'][0]['image']
        constant = KubeConstant()
        key = 'brinnatt/busybox:'+constant.v_busybox
        self.assertEqual(image, 'registry.talkschool.cn:5000/'+key)
        self.assertIn(key, constant.component_images['local-path-provisioner'])
        self.assertEqual(len(helper['spec']['containers']), 1)
        self.assertEqual(set(helper['spec']), {'priorityClassName', 'tolerations', 'containers'})
        self.assertEqual(set(helper['spec']['containers'][0]), {'name','image','imagePullPolicy'})

    def test_actual_setup_teardown_scripts_roundtrip_only_owned_directory(self):
        data = self.render('v0.0.37')['ConfigMap']['data']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = root/'owned-volume'
            sentinel = root/'foreign-volume';sentinel.mkdir()
            for script in ['setup','teardown']:
                result = subprocess.run(['/bin/sh', '-eu', '-c', data[script]],
                    env={'VOL_DIR':str(owned)}, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(owned.exists(), script == 'setup')
                self.assertTrue(sentinel.is_dir())


if __name__ == '__main__':
    unittest.main()
