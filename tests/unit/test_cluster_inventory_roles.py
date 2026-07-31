"""Regression tests for exact cluster inventory role membership."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from service.cluster.manager import ClusterManager, _iter_host_entries


HOSTS = """\
[etcd]
192.168.47.134

[kube_master]
192.168.47.134 k8s_nodename='master-01'
192.168.47.135 k8s_nodename='master-02'

[kube_node]
192.168.47.131 k8s_nodename='worker-01'

[all:vars]
ansible_user=root
"""


class TestClusterInventoryRoles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"KUBEAUTO_BASE_PATH": self.tmp.name},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        self.manager = ClusterManager()
        self.cluster_dir = Path(self.tmp.name) / "clusters" / "dev"
        self.cluster_dir.mkdir(parents=True)
        self.hosts_file = self.cluster_dir / "hosts"
        self.hosts_file.write_text(HOSTS, encoding="utf-8")
        (self.cluster_dir / "config.yml").write_text("{}\n", encoding="utf-8")

    def role_ips(self, role):
        return [ip for _, ip in _iter_host_entries(self.hosts_file, role)]

    def test_add_master_does_not_implicitly_add_worker_role(self):
        with (
            mock.patch.object(self.manager, "_validate_for_setup"),
            mock.patch.object(self.manager, "_run_playbook"),
            mock.patch.object(self.manager, "_restart_load_balancers"),
        ):
            self.manager.add_node("dev", "192.168.47.136", "master", "master-03")

        self.assertIn("192.168.47.136", self.role_ips("master"))
        self.assertNotIn("192.168.47.136", self.role_ips("node"))

    def test_del_master_preserves_an_explicit_worker_role(self):
        self.hosts_file.write_text(
            HOSTS.replace(
                "192.168.47.131 k8s_nodename='worker-01'",
                "192.168.47.131 k8s_nodename='worker-01'\n192.168.47.135",
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.object(self.manager, "_validate_for_setup"),
            mock.patch.object(self.manager, "_run_playbook"),
            mock.patch.object(self.manager, "_reconfigure_kubeconfig"),
            mock.patch.object(self.manager, "_kubectl_del_node"),
            mock.patch.object(self.manager, "_restart_load_balancers"),
        ):
            self.manager.remove_node("dev", "192.168.47.135", "master")

        self.assertNotIn("192.168.47.135", self.role_ips("master"))
        self.assertIn("192.168.47.135", self.role_ips("node"))

    def test_add_etcd_on_master_changes_only_etcd_membership(self):
        before_nodes = self.role_ips("node")
        with (
            mock.patch.object(self.manager, "_validate_for_setup"),
            mock.patch.object(self.manager, "_run_playbook"),
            mock.patch.object(self.manager, "_notify_etcd_apiserver"),
            mock.patch("service.cluster.manager.logger.warning") as warning,
        ):
            self.manager.add_node("dev", "192.168.47.135", "etcd", "etcd-02")

        self.assertIn("192.168.47.135", self.role_ips("etcd"))
        self.assertEqual(before_nodes, self.role_ips("node"))
        self.assertIn("k8s_nodename='master-02'", self.hosts_file.read_text())
        warning.assert_called_once()
        self.assertIn("ignoring k8s_nodename", warning.call_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()
