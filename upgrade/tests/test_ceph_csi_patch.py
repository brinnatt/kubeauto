"""Qualify the selected CSI patch through the owning role's actual inputs."""
import json
from pathlib import Path
import re
import unittest

from jinja2 import Environment
import yaml


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = ROOT / 'upgrade/official-snapshots/2026-11-14-first-hop'
PINS = {
    'ceph_csi_registrar_ver': 'v2.16.0',
    'ceph_csi_provisioner_ver': 'v6.2.0',
    'ceph_csi_attacher_ver': 'v4.11.0',
    'ceph_csi_resizer_ver': 'v2.1.0',
    'ceph_csi_snapshotter_ver': 'v8.5.0',
}


class CSIPatchContracts(unittest.TestCase):
    def target_allowed(self, version, **overrides):
        tasks = yaml.safe_load((ROOT / 'roles/ceph/tasks/csi.yml').read_text())
        # Evaluate the actual version/pairing assertions, without connecting to
        # the inventory or running any subsequent mutating task.
        assertions = tasks[0]['ansible.builtin.assert']['that']
        variables = dict(PINS, ceph_csi_ver=version, **overrides)
        return all(Environment().compile_expression(expression)(**variables)
                   for expression in assertions if 'ceph_csi_ver' in expression)

    def test_source_and_selected_patch_accepted_other_versions_refused(self):
        for version in ('v3.17.1', 'v3.17.2', 'v3.17.0', 'v3.18.2', 'v3.17.3',
                        'v3.17.2-rc.1', 'latest', '3.17.2'):
            with self.subTest(version=version):
                self.assertEqual(self.target_allowed(version),
                                 version in ('v3.17.1', 'v3.17.2'))

    def test_target_refuses_mixed_official_sidecars(self):
        for key in PINS:
            with self.subTest(sidecar=key):
                self.assertFalse(self.target_allowed('v3.17.2', **{key: 'latest'}))

    def test_official_patch_changes_only_the_two_chart_driver_defaults(self):
        comparison = json.loads((SNAPSHOTS / 'cephcsi-3172-compare.txt').read_text())
        self.assertEqual(len(comparison['commits']), comparison['total_commits'])
        files = [f for f in comparison['files'] if f['filename'].startswith('charts/')]
        self.assertEqual({f['filename'] for f in files}, {
            f'charts/ceph-csi-{name}/values.yaml' for name in ('rbd', 'cephfs')})
        for file in files:
            self.assertEqual((file['additions'], file['deletions']), (1, 1))
            self.assertIn('-      tag: v3.17.1\n+      tag: v3.17.2', file['patch'])

    def test_both_actual_values_templates_preserve_identity_and_official_pairing(self):
        env = Environment(autoescape=False)
        env.filters['from_json'] = json.loads
        env.filters['regex_replace'] = lambda v, p, r: re.sub(p, r, v)
        variables = dict(PINS, ceph_cluster_fsid={'stdout': '01234567-89ab-cdef-0123-456789abcdef'},
                         ceph_mon_dump={'stdout': json.dumps({'mons': [
                             {'public_addrs': {'addrvec': [{'type': 'v2', 'addr': 'mon.example:3300/0'}]}}
                         ]})}, ceph_csi_provisioner_replicas=3,
                         ceph_csi_rbd_storage_class='ceph-rbd', ceph_csi_rbd_pool='kubernetes-rbd',
                         ceph_csi_cephfs_storage_class='cephfs', ceph_csi_cephfs_name='kubernetes-cephfs')
        for name in ('rbd', 'cephfs'):
            with self.subTest(driver=name):
                template = env.from_string((ROOT / f'roles/ceph/templates/csi-{name}-values.yml.j2').read_text())
                before = yaml.safe_load(template.render(**variables, ceph_csi_ver='v3.17.1'))
                after = yaml.safe_load(template.render(**variables, ceph_csi_ver='v3.17.2'))
                official = yaml.safe_load((SNAPSHOTS / f'cephcsi-3172-{name}-values.txt').read_text())
                for key in ('registrar', 'plugin'):
                    self.assertEqual(after['nodeplugin'][key]['image']['tag'],
                                     official['nodeplugin'][key]['image']['tag'])
                for key in ('provisioner', 'attacher', 'resizer', 'snapshotter'):
                    self.assertEqual(after['provisioner'][key]['image']['tag'],
                                     official['provisioner'][key]['image']['tag'])
                after['nodeplugin']['plugin']['image']['tag'] = 'v3.17.1'
                self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
