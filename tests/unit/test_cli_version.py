"""Offline version inventory through the real CLI parser and entry point."""

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from controller.cluster.cli import KubeautoCLI


ROOT = Path(__file__).resolve().parents[2]


class TestCliVersion(unittest.TestCase):
    def setUp(self):
        self.cli = KubeautoCLI()

    def run_version(self, *options):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["kubecli", "version", *options]), redirect_stdout(output):
            self.cli.run()
        return output.getvalue()

    def test_grouped_inventory_uses_release_pins_and_aligns_columns(self):
        versions = self.cli.kube_constant
        # A version branch must display its own pins, not copied version strings.
        versions.v_k8s_bin = "v1.34.13"
        versions.v_etcd = "v3.6.99"
        versions.v_containerd = "2.1.99"
        versions.v_coredns = "1.13.99"
        versions.v_ceph = "20.2.99"
        versions.v_dashboard = "7.99.0"
        versions.v_ingressnginx = "4.99.0"
        versions.v_ingress_nginx_controller = "v1.99.0"
        before = vars(versions).copy()
        output = self.run_version()
        self.assertTrue(output.startswith(f"kubeauto {versions.v_kubeauto}\n"))
        self.assertIn("not live cluster versions", output)
        self.assertIn("Optional components may be absent", output)
        self.assertIn("cluster settings may override defaults", output)
        rows = {}
        columns = set()
        for line in output.splitlines():
            if line.startswith("  "):
                name, version = line.strip().rsplit(None, 1)
                self.assertNotIn(name, rows, f"duplicate component: {name}")
                rows[name] = version
                columns.add(line.rfind(version))
        self.assertEqual(len(columns), 1, "version columns must align across groups")
        for name in ("kube-apiserver", "kube-controller-manager", "kube-scheduler",
                     "kubelet", "kube-proxy", "kubectl", "kubeauto-k8s-bin"):
            self.assertEqual(rows[name], versions.v_k8s_bin)
        for name, version in (
            ("etcd / etcdctl / etcdutl", versions.v_etcd),
            ("containerd", versions.v_containerd),
            ("CoreDNS", versions.v_coredns),
            ("Ceph", versions.v_ceph),
            ("Dashboard (chart)", versions.v_dashboard),
            ("ingress-nginx (chart)", versions.v_ingressnginx),
            ("ingress-nginx controller", versions.v_ingress_nginx_controller),
            ("Elasticsearch (logging)", versions.v_elasticsearch),
            ("Elasticsearch (business)", versions.v_business_elasticsearch),
        ):
            with self.subTest(component=name):
                self.assertEqual(rows[name], version)
        self.assertEqual(vars(versions), before)
        self.assertNotIn("sha256", output)

    def test_image_tags_follow_catalog_without_assuming_image_order(self):
        images = self.cli.kube_constant.component_images
        for key in ("minio", "nacos", "rocketmq"):
            images[key] = list(reversed(images[key]))
        images["nacos"] = ["brinnatt/nacos-peer-finder-plugin:1.1", "brinnatt/nacos-server:v9.8.7"]
        with mock.patch.object(type(self.cli.kube_constant), "component_images",
                               new_callable=mock.PropertyMock, return_value=images):
            output = self.run_version()
        self.assertRegex(output, r"Nacos \(image tag\) +v9\.8\.7\n")
        for key, name in (("minio", "minio"), ("rocketmq", "rocketmq-broker")):
            tag = next(image.rsplit(":", 1)[1] for image in images[key]
                       if image.startswith(f"brinnatt/{name}:"))
            self.assertIn(tag, output)

    def test_short_returns_only_kubeauto_version(self):
        self.assertEqual(self.run_version("--short"), self.cli.kube_constant.v_kubeauto + "\n")

    def test_version_does_not_enter_cluster_download_or_runtime_operations(self):
        with mock.patch("controller.cluster.cli.ClusterManager", side_effect=AssertionError("cluster access")), \
                mock.patch("controller.cluster.cli.DownloadManager", side_effect=AssertionError("download")), \
                mock.patch.object(self.cli.docker, "_initialize_docker_client",
                                  side_effect=AssertionError("Docker access")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("external command")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("network access")):
            self.run_version()
            self.run_version("--short")
        self.assertIsNone(self.cli.docker._client)

    def test_real_entry_point_without_config_binaries_or_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            env = {**os.environ, "PATH": "", "KUBEAUTO_BASE_PATH": str(base / "absent"),
                   "KUBEAUTO_LOG_DIR": str(base / "logs"), "KUBECONFIG": str(base / "no-kubeconfig")}
            for options in ([], ["--short"], ["--help"]):
                with self.subTest(options=options):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "kubecli.py"), "version", *options],
                        cwd=directory, env=env, capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    if options == ["--short"]:
                        self.assertEqual(result.stdout, self.cli.kube_constant.v_kubeauto + "\n")
                    elif not options:
                        self.assertIn("kube-apiserver", result.stdout)
                        self.assertIn("CoreDNS", result.stdout)
                        self.assertNotIn("[INFO]", result.stdout)
                    else:
                        self.assertIn("--short", result.stdout)
                        self.assertIn("without querying a cluster", result.stdout)
            self.assertFalse((base / "absent").exists())


if __name__ == "__main__":
    unittest.main()
