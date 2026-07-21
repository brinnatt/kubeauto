"""nerdctl pin: shipped via ext-bin, installed on containerd master/worker nodes."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from common.constants import KubeConstant

KA = Path(__file__).resolve().parents[2]
ROOT = KA.parent
EXT_BIN = ROOT / "kubeauto-ext-bin-dockerfile"
CONTAINERD_ROLE = KA / "roles" / "containerd" / "tasks" / "main.yml"


class TestNerdctlPin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_version_pinned_semver(self):
        self.assertRegex(self.kc.v_nerdctl, r"^\d+\.\d+\.\d+$")

    def test_download_url_minimal_tarball(self):
        url = self.kc.nerdctl_bin_url()
        self.assertIn("containerd/nerdctl/releases/download/", url)
        self.assertIn(f"v{self.kc.v_nerdctl}/", url)
        self.assertTrue(url.endswith(f"nerdctl-{self.kc.v_nerdctl}-linux-amd64.tar.gz")
                        or url.endswith(f"nerdctl-{self.kc.v_nerdctl}-linux-arm64.tar.gz"))
        self.assertNotIn("nerdctl-full", url)

    def test_containerd_role_distributes_nerdctl(self):
        text = CONTAINERD_ROLE.read_text()
        self.assertIn("extra-bin/nerdctl", text)
        self.assertIn("dest={{ bin_dir }}/nerdctl", text)
        self.assertIn("nerdctl completion", text)

    def test_runtime_playbook_covers_master_and_node(self):
        """Master and worker both get roles/containerd when CONTAINER_RUNTIME=containerd."""
        pb = (KA / "playbooks" / "03.runtime.yml").read_text()
        self.assertIn("kube_master", pb)
        self.assertIn("kube_node", pb)
        self.assertIn("role: containerd", pb)

    @unittest.skipUnless(EXT_BIN.is_dir(), "ext-bin sibling not checked out")
    def test_ext_bin_dockerfile_pins_match_constants(self):
        df = (EXT_BIN / "Dockerfile").read_text()
        sh = (EXT_BIN / "multi-platform-download.sh").read_text()

        m_ver = re.search(r"NERDCTL_VER=([^\s]+)", df)
        self.assertIsNotNone(m_ver, "NERDCTL_VER missing from ext-bin Dockerfile")
        self.assertEqual(m_ver.group(1).lstrip("v"), self.kc.v_nerdctl.lstrip("v"))

        m_pack = re.search(r"EXT_BIN_VER=([^\s]+)", df)
        self.assertIsNotNone(m_pack)
        self.assertEqual(m_pack.group(1).lstrip("v"), self.kc.v_extra_bin.lstrip("v"))

        # Must download minimal package only (avoid overwriting project containerd/runc).
        self.assertIn("nerdctl-${NERDCTL_VER}-linux-${ARCH}.tar.gz", sh)
        self.assertNotRegex(sh, r"wget[^\n]*nerdctl-full")
        self.assertIn("mv /tmp/nerdctl /ext-bin/nerdctl", sh)

        # Still compatible with current containerd pin (2.1.x).
        m_ct = re.search(r"CONTAINERD_VER=([^\s]+)", df)
        self.assertIsNotNone(m_ct)
        self.assertTrue(m_ct.group(1).startswith("2.1."), m_ct.group(1))


if __name__ == "__main__":
    unittest.main()
