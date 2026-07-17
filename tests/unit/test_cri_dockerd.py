"""cri-dockerd pin used when CONTAINER_RUNTIME=docker (K8s >=1.24)."""
import unittest

from common.constants import KubeConstant


class TestCriDockerdPin(unittest.TestCase):
    def setUp(self):
        self.kc = KubeConstant()

    def test_version_pinned(self):
        self.assertTrue(self.kc.v_cri_dockerd)
        self.assertRegex(self.kc.v_cri_dockerd, r"^\d+\.\d+\.\d+$")

    def test_download_url(self):
        url = self.kc.cri_dockerd_bin_url(self.kc.v_cri_dockerd)
        self.assertIn("Mirantis/cri-dockerd", url)
        self.assertIn(f"cri-dockerd-{self.kc.v_cri_dockerd}.", url)
        self.assertTrue(url.endswith(".tgz"))


if __name__ == "__main__":
    unittest.main()
