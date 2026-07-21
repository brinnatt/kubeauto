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
        self.assertIsNone(_talkedu_mirror("registry.k8s.io/pause:3.10", self.registry))
        self.assertIsNone(_talkedu_mirror("quay.io/prometheus/prometheus:v3.4.2", self.registry))

    def test_maps_migrated_defaults(self):
        self.assertEqual(
            _talkedu_mirror("brinnatt/calico-node:v3.28.4", self.registry),
            "hub.talkedu.cn/kubeauto/calico-node:v3.28.4",
        )
        self.assertEqual(
            _talkedu_mirror("brinnatt/coredns:1.12.4", self.registry),
            "hub.talkedu.cn/kubeauto/coredns:1.12.4",
        )

    def test_default_tag_latest(self):
        self.assertEqual(
            _talkedu_mirror("brinnatt/kubeauto", self.registry),
            "hub.talkedu.cn/kubeauto/kubeauto:latest",
        )

    def test_pull_order_talkedu_before_dockerhub(self):
        """CN production: hub.talkedu.cn first, Docker Hub brinnatt/* as fallback."""
        tag = KubeConstant().v_extra_bin
        image = f"brinnatt/kubeauto-ext-bin:{tag}"
        talkedu = _talkedu_mirror(image, self.registry)
        candidates = [talkedu, image] if talkedu else [image]
        self.assertEqual(
            candidates,
            [
                f"hub.talkedu.cn/kubeauto/kubeauto-ext-bin:{tag}",
                f"brinnatt/kubeauto-ext-bin:{tag}",
            ],
        )


if __name__ == "__main__":
    unittest.main()
