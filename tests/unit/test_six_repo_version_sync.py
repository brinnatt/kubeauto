"""Enforce six-repo version consistency (kubeauto + 5 dockerfile siblings).

Fail loud when kubeauto pins a tag that auxiliary CI/Dockerfile still publishes
under a different tag — that breaks talkedu/Docker Hub pull order.
"""
from __future__ import annotations

import re
import json
import unittest
from pathlib import Path

from common.constants import KubeConstant

ROOT = Path(__file__).resolve().parents[2].parent  # /projects
KA = Path(__file__).resolve().parents[2]  # /projects/kubeauto
EXT_IMAGES = ROOT / "kubeauto-ext-images-dockerfile"
EXT_BIN = ROOT / "kubeauto-ext-bin-dockerfile"
EXT_BIN_SP1 = ROOT / "kubeauto-ext-bin-sp1-dockerfile"


def _sibling_present() -> bool:
    return EXT_IMAGES.is_dir() and EXT_BIN.is_dir() and EXT_BIN_SP1.is_dir()


@unittest.skipUnless(_sibling_present(), "sibling dockerfile repos not checked out beside kubeauto")
class TestSixRepoVersionSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()
        cls.catalog = json.loads((EXT_IMAGES / "catalog.json").read_text())

    def test_json_mock_ci_tag_matches_constant(self):
        """v_json_mock must match the dual-push catalog tag."""
        self.assertEqual(self._ci_tag("json-mock"), self.kc.v_json_mock)

    def test_ansible_execution_image_ci_tag_matches_constant(self):
        """Conditional EE must keep the TalkEdu/Docker Hub CI tag in sync."""
        self.assertEqual(self._ci_tag("alpine-ansible"), self.kc.v_ansible_core)
        self.assertEqual(self._ci_tag("python314"), self.kc.v_ansible_core_py314)
        self.assertEqual(
            self.kc.ansible_execution_image,
            f"brinnatt/ansible:{self.kc.v_ansible_core}",
        )
        self.assertEqual(
            self.kc.ansible_py314_execution_image,
            f"brinnatt/ansible:{self.kc.v_ansible_core_py314}",
        )

    def test_json_mock_templates_match_constant(self):
        """No leftover hardcoded json-mock tags in role templates."""
        bad = []
        for p in (KA / "roles").rglob("*.j2"):
            text = p.read_text(errors="ignore")
            for m in re.finditer(r"brinnatt/json-mock:([^\s\"']+)", text):
                if m.group(1) != self.kc.v_json_mock:
                    bad.append(f"{p.relative_to(KA)}:{m.group(1)}")
        self.assertEqual(bad, [], msg=f"stale json-mock tags: {bad}")

    def test_ext_bin_env_matches_constants(self):
        ext = (EXT_BIN / "Dockerfile").read_text()
        sp1 = (EXT_BIN_SP1 / "Dockerfile").read_text()

        def env(text: str, name: str) -> str:
            m = re.search(rf"{name}=([^\s]+)", text)
            self.assertIsNotNone(m, f"{name} missing")
            return m.group(1).lstrip("v")

        self.assertEqual(env(ext, "EXT_BIN_VER"), self.kc.v_extra_bin.lstrip("v"))
        self.assertEqual(env(ext, "DOCKER_COMPOSE_VER"), self.kc.v_docker_compose.lstrip("v"))
        self.assertEqual(env(ext, "DOCKER_BUILDX_VER"), self.kc.v_docker_buildx.lstrip("v"))
        self.assertEqual(env(ext, "CRI_DOCKERD_VER"), self.kc.v_cri_dockerd.lstrip("v"))
        self.assertEqual(env(ext, "NERDCTL_VER"), self.kc.v_nerdctl.lstrip("v"))
        self.assertEqual(env(sp1, "EXT_BIN_SP1_VER"), self.kc.v_extra_bin_sp1.lstrip("v"))
        m = re.search(r"kubeauto-ext-bin-sp1:([^\s]+)", ext)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lstrip("v"), self.kc.v_extra_bin_sp1.lstrip("v"))

        # nerdctl must be minimal-only in the download script (no full-bundle collision).
        sh = (EXT_BIN / "multi-platform-download.sh").read_text()
        self.assertIn("nerdctl-${NERDCTL_VER}-linux-${ARCH}.tar.gz", sh)
        self.assertNotRegex(sh, r"wget[^\n]*nerdctl-full")
        self.assertIn("buildx-${DOCKER_BUILDX_VER}.linux-${ARCH}", sh)
        self.assertIn("cri-dockerd-${CRI_DOCKERD_VER}.${ARCH}.tgz", sh)
        self.assertIn("docker-runtime-artifacts.sha256", ext)
        self.assertIn("sha256sum docker-compose docker-buildx cri-dockerd", ext)

    def test_ceph_release_set_matches_ext_images_and_ext_bin(self):
        ext = (EXT_BIN / "Dockerfile").read_text()
        self.assertIn(
            "Kubernetes 1.34-1.36",
            KubeConstant.__dataclass_fields__["v_ceph_csi"].metadata["description"],
        )
        documentation = (KA / "docs/storage/ceph/02-cephadm.md").read_text()
        self.assertIn("Kubernetes 1.33.6 不在该列表中", documentation)
        self.assertIn(f"ENV CEPHADM_VER={self.kc.v_ceph}\n", ext)
        self.assertIn(f"ENV CEPHADM_SHA256={self.kc.v_cephadm_sha256}\n", ext)
        self.assertEqual(self.kc.v_cephadm_sha256, "5b78c8d5772ef7c5c8619dac6ee0b36716b829338ea7a11c9f2b896626ab354f")
        expected = {
            "ceph": {f"v{self.kc.v_ceph}", f"v{self.kc.v_ceph_upgrade_source}"},
            "ceph-csi": {self.kc.v_ceph_csi},
            "ceph-csi-node-driver-registrar": {self.kc.v_ceph_csi_registrar},
            "ceph-csi-provisioner": {self.kc.v_ceph_csi_provisioner},
            "ceph-csi-attacher": {self.kc.v_ceph_csi_attacher},
            "ceph-csi-resizer": {self.kc.v_ceph_csi_resizer},
            "ceph-csi-snapshotter": {self.kc.v_ceph_csi_snapshotter},
        }
        for directory, tags in expected.items():
            if directory == "ceph":
                actual = {self._ci_tag("ceph"), self._ci_tag("ceph-20.2.3")}
            else:
                actual = {self._ci_tag(directory)}
            self.assertEqual(actual, tags, f"Ceph release drift for {directory}")

    def test_json_mock_dockerfile_pins_node20(self):
        """Dockerfile must stay on Node >=18.13 + pinned json-server (v1.3.1 contract)."""
        df = (EXT_IMAGES / "tooling/test-support/json-mock" / "Dockerfile").read_text()
        self.assertIn("node:20.", df)
        self.assertIn("json-server@0.17.4", df)

    def _ci_tag(self, dockerfile_dir: str) -> str:
        entries = [row for row in self.catalog
                   if Path(row["dockerfile"]).parent.name == dockerfile_dir]
        self.assertEqual(len(entries), 1, f"{dockerfile_dir} missing or duplicated in ext-images catalog")
        return entries[0]["tag"]

    def test_ecosystem_ci_tags_match_kubeauto_constants(self):
        """No six-repo fragmentation: CNI/DNS/control-plane/monitor pins must match constants."""
        expect = {
            "calico-node": self.kc.v_calico,
            "calico-cni": self.kc.v_calico,
            "calico-kube-controllers": self.kc.v_calico,
            "flannel": self.kc.v_flannel,
            "flannel-cni-plugin": self.kc.v_flannel_cni,
            "cilium": self.kc.v_cilium,
            "cilium-operator-generic": self.kc.v_cilium,
            "hubble-relay": self.kc.v_cilium,
            "hubble-ui": self.kc.v_cilium_hubble_ui,
            "hubble-ui-backend": self.kc.v_cilium_hubble_ui,
            "coredns": self.kc.v_coredns,
            "k8s-dns-node-cache": self.kc.v_dnsnodecache,
            "metrics-server": self.kc.v_metricsserver,
            "pause": self.kc.v_pause,
            "kube-apiserver": self.kc.v_k8s_bin,
            "kube-controller-manager": self.kc.v_k8s_bin,
            "kube-scheduler": self.kc.v_k8s_bin,
            "kube-proxy": self.kc.v_k8s_bin,
            "kube-router": self.kc.v_kuberouter,
            "kube-ovn": self.kc.v_kubeovn,
            "ingress-nginx-controller": self.kc.v_ingress_nginx_controller,
            "kube-state-metrics": self.kc.v_kube_state_metrics,
            "local-path-provisioner": self.kc.v_localpathprovisioner,
            "nfs-subdir-external-provisioner": self.kc.v_nfsprovisioner,
            "prometheus-webhook-dingtalk": "v2.1.0",
        }

        bad = []
        for dirname, want in expect.items():
            got = self._ci_tag(dirname)
            if got != want and got != want.lstrip("v") and f"v{got}" != want:
                bad.append(f"{dirname}: ci={got} const={want}")
        self.assertEqual(bad, [], msg=f"six-repo ecosystem tag drift: {bad}")

    def test_k8s_bin_pack_default_matches_constant(self):
        """k8s-bin dual-push CI must publish current and test-fixture artifacts."""
        self.assertIn(self.kc.v_k8s_bin, self.kc.k8s_bin_pack_tags)
        k8s_bin = ROOT / "kubeauto-k8s-bin-dockerfile"
        df = (k8s_bin / "Dockerfile").read_text()
        self.assertIn("ARG K8S_VER", df)
        ci = (k8s_bin / ".github/workflows/build.yml").read_text()
        published = set(re.findall(r"version:\s*(v?[0-9]+\.[0-9]+\.[0-9]+)", ci))
        upgrade_gate = (ROOT / "kubeauto" / "tests" / "helpers" / "delivery-upgrade-smoke.sh").read_text()
        upgrade_source = re.search(r"^OLD_VERSION=(v?[0-9]+\.[0-9]+\.[0-9]+)$", upgrade_gate, re.MULTILINE)
        self.assertIsNotNone(upgrade_source, msg="upgrade gate must pin its source fixture")
        required = {*self.kc.k8s_bin_pack_tags, upgrade_source.group(1)}
        self.assertEqual(
            required - published,
            set(),
            msg="k8s-bin CI must dual-push every required current/upgrade tag",
        )

    def test_component_images_tags_exist_in_ext_images_ci(self):
        """Every brinnatt image pin in component_images must have matching CI tag (no local fork)."""
        ci_pins: dict[str, set[str]] = {}
        for row in self.catalog:
            ci_pins.setdefault(row["image"].removeprefix("brinnatt/"), set()).add(row["tag"])

        bad = []
        for component, images in self.kc.component_images.items():
            for ref in images:
                m = re.match(r"brinnatt/([^:]+):(.+)$", ref)
                if not m:
                    bad.append(f"{component}:{ref} not brinnatt/name:tag")
                    continue
                name, tag = m.group(1), m.group(2)
                if name not in ci_pins:
                    bad.append(f"{component}:{name} missing from ext-images CI")
                    continue
                # rocketmq-operator:latest is transitional; CI may pin a concrete tag
                if tag == "latest":
                    continue
                published = ci_pins[name]
                if not any(
                    got == tag or got == tag.lstrip("v") or f"v{got}" == tag
                    for got in published
                ):
                    bad.append(f"{component}:{name} ci={sorted(published)} const={tag}")
        self.assertEqual(bad, [], msg=f"component_images vs CI drift: {bad}")

    def test_nacos_mysql_fixture_is_published_by_ext_images_ci(self):
        """Every Nacos MySQL fixture reference must match one dual-pushed image."""
        tag = self._ci_tag("mysql")
        rows = [row for row in self.catalog
                if row["dockerfile"] == "./middleware/nacos/test-support/mysql/Dockerfile"]
        self.assertEqual(rows, [{"dockerfile": "./middleware/nacos/test-support/mysql/Dockerfile",
                                 "ctx": "./middleware/nacos/test-support/mysql/",
                                 "image": "brinnatt/mysql",
                                 "talkedu_image": "hub.talkedu.cn/kubeauto/mysql", "tag": tag}])
        self.assertIn(
            f"FROM docker.io/library/mysql:{tag}",
            (EXT_IMAGES / "middleware/nacos/test-support/mysql" / "Dockerfile").read_text(),
        )

        fixture_tags = set()
        for relative in (
            "tests/helpers/delivery-gap-retest.sh",
            "tests/helpers/delivery-gap-smoke.sh",
        ):
            gate = (KA / relative).read_text()
            for image in (
                "hub.talkedu.cn/kubeauto/mysql",
                "brinnatt/mysql",
                "docker.io/library/mysql",
                "registry.talkschool.cn:5000/brinnatt/mysql",
            ):
                match = re.search(rf"{re.escape(image)}:([0-9]+\.[0-9]+\.[0-9]+)", gate)
                self.assertIsNotNone(match, f"{relative} missing {image}")
                fixture_tags.add(match.group(1))
        self.assertEqual(fixture_tags, {tag}, msg="Nacos MySQL fixture tag drift")


if __name__ == "__main__":
    unittest.main()
