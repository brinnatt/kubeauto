"""Render the production kubelet template with actual Ansible filters and tests."""
from pathlib import Path
import unittest
from ansible.template import Templar
from ansible.parsing.dataloader import DataLoader
from tests.unit import test_kube_reserved as baseline
import yaml

ROOT = Path(__file__).resolve().parents[2]


class Kubelet135ConfigContracts(unittest.TestCase):
    def render(self, version, **extra):
        values = baseline.TestKubeReservedTemplates()._base_ctx()
        values.update(K8S_VER=version, ca_dir='/etc/kubernetes/ssl', CGROUP_DRIVER='systemd')
        values.update(extra)
        return yaml.safe_load(Templar(loader=DataLoader(), variables=values).template(
            (ROOT/'roles/kube-node/templates/kubelet-config.yaml.j2').read_text()))

    def test_official_default_and_explicit_legacy_opt_in(self):
        for version in ['1.35.10', 'v1.35.10']:
            with self.subTest(version=version):
                self.assertIs(self.render(version)['failCgroupV1'], True)
                self.assertIs(self.render(version, KUBELET_FAIL_CGROUP_V1=False)['failCgroupV1'], False)
        self.assertNotIn('failCgroupV1', self.render('1.34.13'))
        self.assertNotIn('failCgroupV1', self.render('v1.33.6'))

    def test_existing_tls_dns_and_reservations_are_preserved(self):
        old, new = self.render('1.34.13'), self.render('1.35.10')
        self.assertIs(new.pop('failCgroupV1'), True)
        self.assertEqual(old, new)
