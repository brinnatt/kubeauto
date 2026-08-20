"""Multi-source image pull: talkedu first, Docker Hub fallback."""
import unittest
from unittest.mock import MagicMock, patch

from common.exceptions import CommandExecutionError
from service.cluster.registry import RegistryManager, _talkedu_mirror


class TestRegistryPullSources(unittest.TestCase):
    def test_brinnatt_candidates_talkedu_first(self):
        registry = "hub.talkedu.cn/kubeauto"
        image = "brinnatt/kubeauto:v0.1.1"
        talkedu = _talkedu_mirror(image, registry)
        candidates = [talkedu, image] if talkedu else [image]
        self.assertEqual(
            candidates,
            [
                "hub.talkedu.cn/kubeauto/kubeauto:v0.1.1",
                "brinnatt/kubeauto:v0.1.1",
            ],
        )

    @patch("service.cluster.registry.logger")
    def test_intermediate_failure_warning_final_error(self, mock_logger):
        rm = RegistryManager.__new__(RegistryManager)
        rm.kube_constant = MagicMock()
        rm.kube_constant.v_talkedu_registry = "hub.talkedu.cn/kubeauto"
        rm.docker = MagicMock()
        rm.docker.image_exists.return_value = False

        talkedu = "hub.talkedu.cn/kubeauto/kubeauto:v0.1.1"
        hub = "brinnatt/kubeauto:v0.1.1"

        def execute_pull(ref):
            if ref == talkedu:
                raise CommandExecutionError("talkedu unavailable", 1)

        rm.docker._execute_pull.side_effect = execute_pull
        rm.docker.tag_image = MagicMock()

        rm._ensure_image_local(hub)

        rm.docker._execute_pull.assert_any_call(talkedu)
        rm.docker._execute_pull.assert_any_call(hub)
        mock_logger.warning.assert_called_once()
        mock_logger.error.assert_not_called()

    @patch("service.cluster.registry.logger")
    def test_all_sources_fail_logs_error(self, mock_logger):
        rm = RegistryManager.__new__(RegistryManager)
        rm.kube_constant = MagicMock()
        rm.kube_constant.v_talkedu_registry = "hub.talkedu.cn/kubeauto"
        rm.docker = MagicMock()
        rm.docker.image_exists.return_value = False
        rm.docker._execute_pull.side_effect = CommandExecutionError("all failed", 1)

        image = "brinnatt/kubeauto:v0.1.1"
        with self.assertRaises(CommandExecutionError):
            rm._ensure_image_local(image)

        self.assertEqual(mock_logger.warning.call_count, 1)
        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        self.assertIn("All pull sources failed", error_msg)

    @patch("service.cluster.registry.logger")
    def test_upstream_fallback_after_talkedu_and_hub(self, mock_logger):
        """Migrated images: talkedu → brinnatt hub → upstream origin."""
        rm = RegistryManager.__new__(RegistryManager)
        rm.kube_constant = MagicMock()
        rm.kube_constant.v_talkedu_registry = "hub.talkedu.cn/kubeauto"
        rm.docker = MagicMock()
        rm.docker.image_exists.return_value = False

        talkedu = "hub.talkedu.cn/kubeauto/calico-node:v3.28.4"
        hub = "brinnatt/calico-node:v3.28.4"
        upstream = "calico/node:v3.28.4"

        def execute_pull(ref):
            if ref in (talkedu, hub):
                raise CommandExecutionError("not published yet", 1)

        rm.docker._execute_pull.side_effect = execute_pull
        rm._ensure_image_local(hub)

        self.assertEqual(
            [c.args[0] for c in rm.docker._execute_pull.call_args_list],
            [talkedu, hub, upstream],
        )
        rm.docker.tag_image.assert_called_once_with(upstream, hub)
        self.assertEqual(mock_logger.warning.call_count, 2)

    @patch("service.cluster.registry.logger")
    def test_percona_falls_back_only_to_official_upstream(self, mock_logger):
        rm = RegistryManager.__new__(RegistryManager)
        rm.kube_constant = MagicMock()
        rm.kube_constant.v_talkedu_registry = "hub.talkedu.cn/kubeauto"
        rm.docker = MagicMock()
        rm.docker.image_exists.return_value = False
        rm.docker._execute_pull.side_effect = [
            CommandExecutionError("talkedu missing", 1),
            CommandExecutionError("brinnatt missing", 1),
            None,
        ]

        image = "brinnatt/percona-xtradb-cluster:8.4.8-8.1"
        rm._ensure_image_local(image)

        self.assertEqual(
            [call.args[0] for call in rm.docker._execute_pull.call_args_list],
            [
                "hub.talkedu.cn/kubeauto/percona-xtradb-cluster:8.4.8-8.1",
                image,
                "percona/percona-xtradb-cluster:8.4.8-8.1",
            ],
        )
        rm.docker.tag_image.assert_called_once_with(
            "percona/percona-xtradb-cluster:8.4.8-8.1",
            image,
        )
        self.assertEqual(mock_logger.warning.call_count, 2)


if __name__ == "__main__":
    unittest.main()
