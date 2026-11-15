"""Offline configuration contracts; never start a runtime or contact its socket."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

import jinja2
import yaml

from upgrade.tests.test_control_plane import NO_CONNECTION


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT/'roles/containerd'
SOURCE_SHA = '940c9020144a88b370cba36793a4d7fad6a347ab'
ARCHIVES = {
    '2.2.10': 'e7bbdb6c1e4d76d501783b25cddb63e5cc29d0e16209b216c4a561548e3c0e3c',
    '2.3.7': '828db4f3720a37361a803ece1633feca2b130a68579cdb4a9ea4fad03df35a33',
}


def render(schema=None, original=None):
    text = original if original is not None else (ROLE/'templates/config.toml.j2').read_text()
    values = dict(CONTAINERD_STORAGE_DIR='/fixture/containerd-data',
        SANDBOX_IMAGE='registry.talkschool.cn:5000/brinnatt/pause:3.10',
        inventory_hostname='node-one', groups={'kube_master': ['node-one']})
    if schema is not None:
        values['CONTAINERD_CONFIG_VERSION'] = schema
    return jinja2.Template(text, undefined=jinja2.StrictUndefined, trim_blocks=True).render(**values)


class ContainerdConfigurationContracts(unittest.TestCase):
    def test_default_render_preserves_the_immutable_ga_configuration(self):
        original = subprocess.check_output(['git', 'show',
            SOURCE_SHA+':roles/containerd/templates/config.toml.j2'], cwd=ROOT, text=True)
        self.assertEqual(render(), render(original=original))

    def test_explicit_target_schema_preserves_every_existing_plugin_value(self):
        before, after = tomllib.loads(render()), tomllib.loads(render(3))
        self.assertEqual(before.pop('version'), 2)
        self.assertEqual(after.pop('version'), 3)
        self.assertEqual(before, after)
        runtime = after['plugins']['io.containerd.cri.v1.runtime']
        self.assertEqual(runtime['cni']['bin_dirs'], ['/opt/cni/bin'])
        self.assertTrue(runtime['containerd']['runtimes']['runc']['options']['SystemdCgroup'])
        self.assertEqual(after['root'], '/fixture/containerd-data')

    def test_real_role_refuses_unknown_or_noninteger_schema_before_mutation(self):
        for schema in [1, 4, '3', 3.0, True, None]:
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root/'connections').mkdir()
                (root/'connections/upgrade_test_forbidden.py').write_text(NO_CONNECTION)
                (root/'library').mkdir()
                # Parse the existing collection-owned task without installing
                # a collection. This module must never be invoked: the test
                # proves only the new, connection-free admission refusal.
                (root/'library/modprobe.py').write_text('raise RuntimeError("TEST_FORBIDDEN_MODULE")\n')
                book = root/'fixture.yml'
                book.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'upgrade_test_forbidden',
                    'gather_facts': False, 'vars': {'CONTAINERD_CONFIG_VERSION': schema},
                    'tasks': [{'ansible.builtin.include_tasks': str(ROLE/'tasks/main.yml')}]}]))
                run = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
                    '-i', 'localhost,', str(book)], capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': str(root/'ansible-local'),
                         'ANSIBLE_LIBRARY': str(root/'library'),
                         'ANSIBLE_CONNECTION_PLUGINS': str(root/'connections')} )
                self.assertEqual(run.returncode, 2, run.stdout+run.stderr)
                self.assertIn('schema must be the reviewed integer', run.stdout)
                self.assertNotIn('prepare containerd-related directories', run.stdout)
                self.assertNotIn('TEST_REMOTE_CONNECTION_REFUSED', run.stdout+run.stderr)

    @unittest.skipUnless(os.environ.get('UPGRADE_CONTAINERD_FIXTURE_DIR'),
                         'checksum-qualified official containerd offline binaries required')
    def test_real_bridge_and_target_binaries_dump_and_migrate_the_target_configuration(self):
        fixtures = Path(os.environ['UPGRADE_CONTAINERD_FIXTURE_DIR'])
        for version, archive_hash in ARCHIVES.items():
            directory = fixtures/version
            provenance = json.loads((directory/'provenance.json').read_text())
            binary = directory/'containerd'
            self.assertEqual(provenance['version'], version)
            self.assertEqual(provenance['archive_sha256'], archive_hash)
            self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), provenance['binary_sha256'])
            identity = subprocess.check_output([str(binary), '--version'], text=True, timeout=10)
            self.assertIn(' v'+version+' ', identity)
            with tempfile.TemporaryDirectory() as temporary:
                config = Path(temporary)/'config.toml'
                config.write_text(render(3))
                for command in ['dump', 'migrate']:
                    result = subprocess.run([str(binary), '--config', str(config), 'config', command],
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    actual = tomllib.loads(result.stdout)
                    # 2.3's official loader migrates compatible schema 3
                    # input to schema 4 and moves listener settings to plugins.
                    self.assertEqual(actual['version'], 3 if version == '2.2.10' else 4)
                    self.assertEqual(actual['root'], '/fixture/containerd-data')
                    self.assertEqual(actual['state'], '/run/containerd')
                    grpc = (actual['grpc'] if version == '2.2.10' else
                            actual['plugins']['io.containerd.server.v1.grpc'])
                    for key, value in tomllib.loads(render(3))['grpc'].items():
                        if not key.startswith('tcp_'):
                            self.assertEqual(grpc[key], value, (version, command, key))
                    if version == '2.3.7':
                        self.assertEqual(actual['plugins']['io.containerd.server.v1.grpc-tcp']['address'], '')
                    runtime = actual['plugins']['io.containerd.cri.v1.runtime']
                    self.assertEqual(runtime['cni']['bin_dirs'], ['/opt/cni/bin'])
                    self.assertEqual(runtime['cni']['conf_dir'], '/etc/cni/net.d')
                    self.assertEqual(runtime['containerd']['default_runtime_name'], 'runc')
                    self.assertEqual(runtime['containerd']['runtimes']['runc']['runtime_type'], 'io.containerd.runc.v2')
                    self.assertTrue(runtime['containerd']['runtimes']['runc']['options']['SystemdCgroup'])
                    self.assertEqual(actual['plugins']['io.containerd.cri.v1.images']['registry']['config_path'],
                                     '/etc/containerd/certs.d')
                    self.assertEqual(actual['plugins']['io.containerd.cri.v1.images']['pinned_images']['sandbox'],
                        'registry.talkschool.cn:5000/brinnatt/pause:3.10')


if __name__ == '__main__':
    unittest.main()
