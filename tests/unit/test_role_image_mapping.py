"""Static contract: role deploy paths must match brinnatt/* + Dockerfile FROM mapping.

Catches chart composition mistakes (e.g. Cilium operator-generic-generic) before
customer delivery. Does not require a live cluster.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXT_IMAGES = Path("/projects/kubeauto-ext-images-dockerfile")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestCiliumOperatorImageComposition(unittest.TestCase):
    """Cilium chart helper: repository-cloud-suffix:tag (cloud defaults to generic)."""

    def test_repository_excludes_generic_suffix(self):
        text = _read("roles/cilium/templates/values.yaml.j2")
        m = re.search(
            r"operator:\s*\n(?:.*\n)*?\s+image:\s*\n(?:.*\n)*?\s+repository:\s*\"([^\"]+)\"",
            text,
        )
        self.assertIsNotNone(m, "operator.image.repository not found")
        repo = m.group(1)
        self.assertTrue(
            repo.endswith("/brinnatt/cilium-operator"),
            f"expected .../brinnatt/cilium-operator (chart appends -generic), got {repo}",
        )
        self.assertFalse(
            repo.endswith("cilium-operator-generic"),
            "repository must NOT include -generic; chart appends cloud=generic",
        )

    def test_composed_image_matches_download_list(self):
        from common.constants import KubeConstant

        text = _read("roles/cilium/templates/values.yaml.j2")
        repo = re.search(r'operator:[\s\S]*?repository:\s*"([^"]+)"', text).group(1)
        # Simulate chart helper with cloud=generic, empty suffix
        composed = f"{repo}-generic:v1.19.5"
        name = composed.split("/")[-1].split(":")[0]
        tag = composed.rsplit(":", 1)[-1]
        expected = f"brinnatt/{name}:{tag}" if not name.startswith("brinnatt/") else f"{name}:{tag}"
        # download list uses brinnatt/cilium-operator-generic
        images = KubeConstant().component_images["cilium"]
        self.assertIn(
            f"brinnatt/cilium-operator-generic:{KubeConstant().v_cilium}",
            images,
        )
        self.assertEqual(name, "cilium-operator-generic")


class TestRoleDeployPathsAreBrinnatt(unittest.TestCase):
    """Changed role templates should deploy via registry.../brinnatt/*."""

    ROLE_FILES = [
        "roles/calico/templates/calico-v3.28.yaml.j2",
        "roles/flannel/templates/kube-flannel.yaml.j2",
        "roles/cilium/templates/values.yaml.j2",
        "roles/kube-router/templates/kuberouter.yaml.j2",
        "roles/kube-ovn/templates/install.sh.j2",
        "roles/kube-ovn/templates/coredns.yaml.j2",
        "roles/cluster-addon/templates/dns/coredns.yaml.j2",
        "roles/cluster-addon/templates/local-storage/local-path-storage.yaml.j2",
        "roles/cluster-addon/templates/dashboard/dashboard-values.yaml.j2",
        "roles/cluster-addon/templates/minio/operator-values.yaml.j2",
        "roles/cluster-addon/templates/minio/tenant-values.yaml.j2",
        "roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2",
        "roles/cluster-addon/templates/openebs/values.yaml.j2",
        "roles/cluster-addon/templates/prometheus/values.yaml.j2",
        "roles/cluster-addon/templates/rocketmq/rocketmq_cluster.yaml.j2",
        "roles/cluster-addon/templates/prometheus/dingtalk-webhook.yaml",
    ]

    # Legacy non-brinnatt paths that must not remain in deploy templates
    FORBIDDEN = (
        "registry.talkschool.cn:5000/calico/",
        "registry.talkschool.cn:5000/flannel/",
        "registry.talkschool.cn:5000/coredns/",
        "registry.talkschool.cn:5000/cilium/",
        "registry.talkschool.cn:5000/kubeovn/",
        "registry.talkschool.cn:5000/cloudnativelabs/",
        "registry.talkschool.cn:5000/rancher/",
        "registry.talkschool.cn:5000/kubernetesui/",
        "registry.talkschool.cn:5000/minio/",
        "registry.talkschool.cn:5000/nacos/",
        "registry.talkschool.cn:5000/rocketmq/",
        "quay.io/prometheus",
        "quay.io/minio/",
        "ghcr.io/flannel-io/",
    )

    def test_no_legacy_registry_paths(self):
        for rel in self.ROLE_FILES:
            # Strip YAML/shell comments so upstream reference comments do not fail the contract.
            text = "\n".join(
                ln for ln in _read(rel).splitlines() if not ln.lstrip().startswith("#")
            )
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, text, f"{rel} still contains legacy path {bad}")

    def test_openebs_pins_provisioner_tag(self):
        text = _read("roles/cluster-addon/templates/openebs/values.yaml.j2")
        self.assertIn("repository: brinnatt/provisioner-localpv", text)
        self.assertRegex(
            text,
            r"provisioner-localpv[\s\S]{0,160}tag:\s*\"4\.3\.0\"",
            "provisioner-localpv must pin 4.3.0 (umbrella chart 4.3.2; upstream has no :4.3.2)",
        )


class TestDockerfileFromMatchesComponentImages(unittest.TestCase):
    """If ext-images Dockerfile exists, FROM should be the official upstream image."""

    # brinnatt name -> expected FROM prefix (registry/org)
    EXPECTED_FROM = {
        "calico-cni": "docker.io/calico/cni:",
        "calico-node": "docker.io/calico/node:",
        "calico-kube-controllers": "docker.io/calico/kube-controllers:",
        "coredns": "docker.io/coredns/coredns:",
        "flannel": "docker.io/flannel/flannel:",
        # Cilium official publish location for current tags is quay.io
        "cilium": "quay.io/cilium/cilium:",
        "cilium-operator-generic": "quay.io/cilium/operator-generic:",
        "hubble-relay": "quay.io/cilium/hubble-relay:",
        "hubble-ui": "quay.io/cilium/hubble-ui:",
        "hubble-ui-backend": "quay.io/cilium/hubble-ui-backend:",
        "kube-router": "docker.io/cloudnativelabs/kube-router:",
        "kube-ovn": "docker.io/kubeovn/kube-ovn:",
        "local-path-provisioner": "docker.io/rancher/local-path-provisioner:",
        "provisioner-localpv": "docker.io/openebs/provisioner-localpv:4.3.0",
        "prometheus": "quay.io/prometheus/prometheus:",
        "prometheus-operator": "quay.io/prometheus-operator/prometheus-operator:",
        "alertmanager": "quay.io/prometheus/alertmanager:",
        "minio-operator": "quay.io/minio/operator:",
        "prometheus-webhook-dingtalk": "docker.io/timonwong/prometheus-webhook-dingtalk:v2.1.0",
    }

    def test_openebs_kubectl_uses_official_dl_k8s_io(self):
        df = EXT_IMAGES / "openebs-kubectl" / "Dockerfile"
        if not df.is_file():
            self.skipTest("ext-images not mounted")
        text = df.read_text(encoding="utf-8")
        self.assertIn("dl.k8s.io/release/", text)
        # Operational refs must not use third-party kubectl images
        for line in text.splitlines():
            s = line.strip()
            if s.upper().startswith("FROM ") or s.upper().startswith("RUN "):
                self.assertNotIn("bitnami", s.lower())

    def test_openebs_chart_hook_only_needs_kubectl_cli(self):
        """Official OpenEBS 4.3.2 pre-upgrade hook runs kubectl client commands only.

        Evidence: roles/cluster-addon/files/openebs-4.3.2.tgz
        templates/pre-upgrade-hook.yaml args are:
          kubectl annotate ... CRDs && kubectl delete deploy ...
        So any image whose PATH provides a Kubernetes-project kubectl binary
        (dl.k8s.io) is functionally equivalent; Bitnami packaging is not required.
        """
        import tarfile

        tgz = ROOT / "roles/cluster-addon/files/openebs-4.3.2.tgz"
        self.assertTrue(tgz.is_file(), "openebs chart tgz missing")
        with tarfile.open(tgz, "r:gz") as tf:
            member = tf.extractfile("openebs/templates/pre-upgrade-hook.yaml")
            self.assertIsNotNone(member)
            hook = member.read().decode("utf-8")
        self.assertIn("kubectl annotate", hook)
        self.assertIn("kubectl -n", hook)
        self.assertIn("delete deploy", hook)
        # Hook image is configurable; chart default is third-party, we override in values.
        values = _read("roles/cluster-addon/templates/openebs/values.yaml.j2")
        self.assertIn("brinnatt/openebs-kubectl", values)
        self.assertIn("dl.k8s.io", (EXT_IMAGES / "openebs-kubectl" / "Dockerfile").read_text())

    def test_no_bitnami_in_registry_fallbacks(self):
        from service.cluster import registry as reg

        for name, upstream in reg._BRINATT_UPSTREAM_FALLBACKS.items():
            self.assertNotIn("bitnami", name.lower())
            self.assertNotIn("bitnami", upstream.lower())
        self.assertNotIn(
            "brinnatt/openebs-kubectl",
            reg._BRINATT_UPSTREAM_FALLBACKS,
            "openebs-kubectl must not map to any third-party container upstream",
        )

    def test_provisioner_localpv_tag_is_430_not_432(self):
        df = EXT_IMAGES / "provisioner-localpv" / "Dockerfile"
        if not df.is_file():
            self.skipTest("ext-images not mounted")
        text = df.read_text(encoding="utf-8")
        self.assertIn("provisioner-localpv:4.3.0", text)
        self.assertNotIn("provisioner-localpv:4.3.2", text)
        # kubeauto values must pin the same official chart default
        values = (
            Path(__file__).resolve().parents[2]
            / "roles/cluster-addon/templates/openebs/values.yaml.j2"
        )
        v = values.read_text(encoding="utf-8")
        self.assertIn('tag: "4.3.0"', v)

    @unittest.skipUnless(EXT_IMAGES.is_dir(), "ext-images dockerfile repo not mounted")
    def test_dockerfile_from_lines(self):
        missing = []
        wrong = []
        for name, prefix in self.EXPECTED_FROM.items():
            df = EXT_IMAGES / name / "Dockerfile"
            if not df.is_file():
                missing.append(name)
                continue
            text = df.read_text(encoding="utf-8")
            from_lines = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip().upper().startswith("FROM ")
            ]
            self.assertTrue(from_lines, f"{name}: no FROM")
            if not any(prefix in ln for ln in from_lines):
                wrong.append(f"{name}: expected FROM containing {prefix}, got {from_lines}")
        self.assertFalse(missing, f"missing Dockerfiles: {missing}")
        self.assertFalse(wrong, "\n".join(wrong))


if __name__ == "__main__":
    unittest.main()
