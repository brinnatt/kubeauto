"""Enforce six-repo version consistency (kubeauto + 5 dockerfile siblings).

Fail loud when kubeauto pins a tag that auxiliary CI/Dockerfile still publishes
under a different tag — that breaks talkedu/Docker Hub pull order.
"""
from __future__ import annotations

import re
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

    def test_json_mock_ci_tag_matches_constant(self):
        """v_json_mock must match ext-images CI matrix tag (talkedu dual-push)."""
        ci = (EXT_IMAGES / ".github/workflows/build.yml").read_text()
        m = re.search(
            r"dockerfile:\s*\./json-mock/Dockerfile.*?tag:\s*([^\n#]+)",
            ci,
            re.S,
        )
        self.assertIsNotNone(m, "json-mock missing from ext-images CI matrix")
        tag = m.group(1).strip().strip("'\"")
        self.assertEqual(tag, self.kc.v_json_mock)

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
        self.assertEqual(env(ext, "NERDCTL_VER"), self.kc.v_nerdctl.lstrip("v"))
        self.assertEqual(env(sp1, "EXT_BIN_SP1_VER"), self.kc.v_extra_bin_sp1.lstrip("v"))
        m = re.search(r"kubeauto-ext-bin-sp1:([^\s]+)", ext)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lstrip("v"), self.kc.v_extra_bin_sp1.lstrip("v"))

        # nerdctl must be minimal-only in the download script (no full-bundle collision).
        sh = (EXT_BIN / "multi-platform-download.sh").read_text()
        self.assertIn("nerdctl-${NERDCTL_VER}-linux-${ARCH}.tar.gz", sh)
        self.assertNotRegex(sh, r"wget[^\n]*nerdctl-full")

    def test_json_mock_dockerfile_pins_node20(self):
        """Dockerfile must stay on Node >=18.13 + pinned json-server (v1.3.1 contract)."""
        df = (EXT_IMAGES / "json-mock" / "Dockerfile").read_text()
        self.assertIn("node:20.", df)
        self.assertIn("json-server@0.17.4", df)

    def _ci_tag(self, dockerfile_dir: str) -> str:
        ci = (EXT_IMAGES / ".github/workflows/build.yml").read_text()
        m = re.search(
            rf"dockerfile:\s*\./{re.escape(dockerfile_dir)}/Dockerfile.*?tag:\s*([^\n#]+)",
            ci,
            re.S,
        )
        self.assertIsNotNone(m, f"{dockerfile_dir} missing from ext-images CI")
        return m.group(1).strip().strip("'\"")

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
        """k8s-bin sibling is ARG-driven; pack tags property must include current default."""
        self.assertIn(self.kc.v_k8s_bin, self.kc.k8s_bin_pack_tags)
        df = (ROOT / "kubeauto-k8s-bin-dockerfile" / "Dockerfile").read_text()
        self.assertIn("ARG K8S_VER", df)

    def test_component_images_tags_exist_in_ext_images_ci(self):
        """Every brinnatt image pin in component_images must have matching CI tag (no local fork)."""
        ci = (EXT_IMAGES / ".github/workflows/build.yml").read_text()
        # Matrix rows: image: brinnatt/<name> … tag: <tag> (talkedu_image may sit between).
        ci_pins: dict[str, str] = {}
        for m in re.finditer(
            r"image:\s*brinnatt/([\w.-]+).*?^\s*tag:\s*([^\n#]+)",
            ci,
            re.M | re.S,
        ):
            ci_pins[m.group(1)] = m.group(2).strip().strip("'\"")

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
                got = ci_pins[name]
                if got != tag and got != tag.lstrip("v") and f"v{got}" != tag:
                    bad.append(f"{component}:{name} ci={got} const={tag}")
        self.assertEqual(bad, [], msg=f"component_images vs CI drift: {bad}")


if __name__ == "__main__":
    unittest.main()
