"""TalkEdu CN registry naming and pull-priority helpers."""
import unittest

from common.constants import KubeConstant
from service.cluster.registry import _talkedu_mirror


class TestTalkeduMirror(unittest.TestCase):
    def setUp(self):
        self.registry = KubeConstant.v_talkedu_registry

    def test_maps_brinnatt_to_talkedu(self):
        self.assertEqual(
            _talkedu_mirror("brinnatt/kubeauto:v0.1.1", self.registry),
            "hub.talkedu.cn/kubeauto/kubeauto:v0.1.1",
        )
        self.assertEqual(
            _talkedu_mirror("brinnatt/pause:3.10", self.registry),
            "hub.talkedu.cn/kubeauto/pause:3.10",
        )

    def test_ignores_non_brinnatt(self):
        self.assertIsNone(_talkedu_mirror("calico/node:v3.28.4", self.registry))
        self.assertIsNone(_talkedu_mirror("registry:2", self.registry))

    def test_default_tag_latest(self):
        self.assertEqual(
            _talkedu_mirror("brinnatt/kubeauto", self.registry),
            "hub.talkedu.cn/kubeauto/kubeauto:latest",
        )

    def test_pull_order_talkedu_before_dockerhub(self):
        """CN production: hub.talkedu.cn first, Docker Hub brinnatt/* as fallback."""
        image = "brinnatt/kubeauto-ext-bin:1.13.0"
        talkedu = _talkedu_mirror(image, self.registry)
        candidates = [talkedu, image] if talkedu else [image]
        self.assertEqual(
            candidates,
            [
                "hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.13.0",
                "brinnatt/kubeauto-ext-bin:1.13.0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
