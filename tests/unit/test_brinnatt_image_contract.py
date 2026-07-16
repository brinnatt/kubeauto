"""Ensure component/default images follow brinnatt/* talkedu design."""
import unittest

from common.constants import KubeConstant
from service.cluster.registry import _talkedu_mirror


class TestBrinnattImageContract(unittest.TestCase):
    def setUp(self):
        self.kc = KubeConstant()
        self.registry = self.kc.v_talkedu_registry

    def test_all_component_images_are_brinnatt(self):
        for component, images in self.kc.component_images.items():
            for image in images:
                with self.subTest(component=component, image=image):
                    self.assertTrue(
                        image.startswith("brinnatt/"),
                        f"{component}: {image} must be brinnatt/<name>:<tag>",
                    )
                    self.assertIsNotNone(
                        _talkedu_mirror(image, self.registry),
                        f"{component}: {image} must map to talkedu",
                    )

    def test_default_images_are_brinnatt(self):
        defaults = [
            f"brinnatt/calico-cni:{self.kc.v_calico}",
            f"brinnatt/calico-kube-controllers:{self.kc.v_calico}",
            f"brinnatt/calico-node:{self.kc.v_calico}",
            f"brinnatt/coredns:{self.kc.v_coredns}",
            f"brinnatt/k8s-dns-node-cache:{self.kc.v_dnsnodecache}",
            f"brinnatt/metrics-server:{self.kc.v_metricsserver}",
            f"brinnatt/pause:{self.kc.v_pause}",
        ]
        for image in defaults:
            with self.subTest(image=image):
                self.assertIsNotNone(_talkedu_mirror(image, self.registry))


if __name__ == "__main__":
    unittest.main()
