"""Check the real etcdv3 manifests against the pinned upstream pairings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'roles/calico'
SNAPSHOTS = ROOT / 'upgrade/official-snapshots/2026-11-14-first-hop'
SOURCE_SHA = '940c9020144a88b370cba36793a4d7fad6a347ab'


class CalicoManifestContracts(unittest.TestCase):
    def render(self, version, backend='bird', overlay='Always', *, original=None):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROLE/'templates'),
                                 undefined=jinja2.StrictUndefined, trim_blocks=True)
        template = (env.from_string(original) if original is not None else
                    env.get_template(f'calico-{version.rsplit(".", 1)[0]}.yaml.j2'))
        return template.render(calico_ver=version, ETCD_ENDPOINTS='https://etcd.test:2379',
            CALICO_NETWORKING_BACKEND=backend, CALICO_ENABLE_OVERLAY=overlay,
            CLUSTER_CIDR='10.244.0.0/16', ca_dir='/etc/kubernetes/ssl',
            IP_AUTODETECTION_METHOD='interface=ens.*', NODE_PORT_RANGE='30000-32767')

    @staticmethod
    def objects(text):
        return {(o['kind'], o['metadata']['name']): o
                for o in yaml.safe_load_all(text) if o}

    def test_source_is_byte_equal_to_the_immutable_ga_template(self):
        original = subprocess.check_output(['git', 'show',
            SOURCE_SHA+':roles/calico/templates/calico-v3.28.yaml.j2'], cwd=ROOT, text=True)
        for backend, overlay in [('bird', 'Always'), ('bird', 'Never'), ('vxlan', 'CrossSubnet')]:
            self.assertEqual(self.render('v3.28.4', backend, overlay),
                             self.render('v3.28.4', backend, overlay, original=original))

    def test_target_rbac_matches_the_exact_official_etcd_manifest(self):
        for version, snapshot in [('v3.31.7', '3317'), ('v3.32.4', '3324')]:
            official = self.objects((SNAPSHOTS/f'calico-{snapshot}-manifest-api.txt').read_text())
            actual = self.objects(self.render(version))
            for key, obj in official.items():
                if key[0] in ['ClusterRole', 'ClusterRoleBinding', 'ServiceAccount', 'PodDisruptionBudget']:
                    self.assertEqual(actual[key], obj, (version, key))
            # The original product owns this TLS Secret. Never replace its
            # contents with the empty credentials in the upstream example.
            self.assertNotIn(('Secret', 'calico-etcd-secrets'), actual)

    def test_network_state_paths_selectors_and_existing_controller_scope_are_preserved(self):
        for backend, overlay in [('bird', 'Always'), ('bird', 'Never'), ('vxlan', 'CrossSubnet')]:
            source = self.objects(self.render('v3.28.4', backend, overlay))
            for version in ['v3.31.7', 'v3.32.4']:
                actual = self.objects(self.render(version, backend, overlay))
                for key in source:
                    self.assertEqual(source[key]['metadata'], actual[key]['metadata'])
                cm = actual[('ConfigMap', 'calico-config')]['data']
                old_cm = source[('ConfigMap', 'calico-config')]['data']
                self.assertEqual({k: v for k, v in cm.items() if k != 'cni_network_config'},
                                 {k: v for k, v in old_cm.items() if k != 'cni_network_config'})
                cni = json.loads(cm['cni_network_config'].replace('__CNI_MTU__', '0'))
                old_cni = json.loads(old_cm['cni_network_config'].replace('__CNI_MTU__', '0'))
                self.assertEqual(cni['cniVersion'], '1.0.0')
                self.assertEqual(cni['plugins'], old_cni['plugins'])
                for kind, name in [('DaemonSet', 'calico-node'), ('Deployment', 'calico-kube-controllers')]:
                    key = (kind, name)
                    before, after = source[key]['spec'], actual[key]['spec']
                    self.assertEqual(before['selector'], after['selector'])
                    pod = after['template']['spec']
                    self.assertTrue(pod['hostNetwork'])
                    old_env = before['template']['spec']['containers'][0]['env']
                    new_env = pod['containers'][0]['env']
                    for field in old_env:
                        self.assertIn(field, new_env)
                    self.assertEqual(pod['securityContext']['seccompProfile']['type'], 'RuntimeDefault')
                node = actual[('DaemonSet', 'calico-node')]['spec']['template']['spec']
                env = {e['name']: e.get('value') for e in node['containers'][0]['env']}
                self.assertEqual(env['FELIX_BPFENABLED'], 'false')
                self.assertEqual(env['FELIX_NFTABLESMODE'], 'Disabled')
                self.assertEqual(actual[('DaemonSet', 'calico-node')]['spec']['updateStrategy'],
                                 source[('DaemonSet', 'calico-node')]['spec']['updateStrategy'])

    def test_target_runtime_setup_and_images_match_upstream_without_enabling_loadbalancer(self):
        for version, snapshot in [('v3.31.7', '3317'), ('v3.32.4', '3324')]:
            official = self.objects((SNAPSHOTS/f'calico-{snapshot}-manifest-api.txt').read_text())
            actual = self.objects(self.render(version))
            for kind, name in [('DaemonSet', 'calico-node'), ('Deployment', 'calico-kube-controllers')]:
                pod = actual[(kind, name)]['spec']['template']['spec']
                upstream = official[(kind, name)]['spec']['template']['spec']
                self.assertEqual(pod['volumes'], upstream['volumes'])
                for field in ['containers', 'initContainers']:
                    self.assertEqual([c['name'] for c in pod.get(field, [])],
                                     [c['name'] for c in upstream.get(field, [])])
                    for container, expected in zip(pod.get(field, []), upstream.get(field, [])):
                        self.assertEqual(container.get('command'), expected.get('command'))
                        self.assertEqual(container.get('volumeMounts'), expected.get('volumeMounts'))
                        self.assertEqual(container['image'].split(':')[-1], version)
                        self.assertTrue(container['image'].startswith('registry.talkschool.cn:5000/brinnatt/calico-'))
            controller = actual[('Deployment', 'calico-kube-controllers')]['spec']['template']['spec']['containers'][0]
            self.assertNotIn('loadbalancer', next(e['value'] for e in controller['env'] if e['name']=='ENABLED_CONTROLLERS'))

    def test_real_role_refuses_unreviewed_pair_before_files_or_commands(self):
        for version, minor in [('v3.31.6', 'v3.31'), ('v3.32.3', 'v3.32'), ('v3.32.4', 'v3.28')]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                book = root/'fixture.yml'
                book.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local',
                    'gather_facts': False, 'vars': {'calico_ver': version, 'calico_ver_main': minor,
                        'cluster_dir': str(root/'untouched'), 'CHANGE_CA': False},
                    'tasks': [{'ansible.builtin.include_tasks': str(ROLE/'tasks/main.yml')}]}]))
                run = subprocess.run([str(Path(sys.executable).parent/'ansible-playbook'),
                    '-i', 'localhost,', str(book)], capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': str(root/'ansible-local')})
                self.assertEqual(run.returncode, 2, run.stdout+run.stderr)
                self.assertIn('requires its reviewed exact version', run.stdout)
                self.assertNotIn('create calico CSR', run.stdout)
                self.assertFalse((root/'untouched').exists())


if __name__ == '__main__':
    unittest.main()
