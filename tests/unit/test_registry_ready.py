"""Local registry readiness (wait after start; mirrors Docker daemon wait pattern)."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from common.exceptions import CommandExecutionError
from service.cluster.registry import RegistryManager


class TestRegistryReady(unittest.TestCase):
    def test_registry_data_path_uses_snap_common_for_snap_docker(self):
        rm = RegistryManager()
        rm.docker = MagicMock()
        rm.docker._run_docker.return_value.stdout = "/var/snap/docker/common/var-lib-docker\n"
        self.assertEqual(
            rm._registry_data_path(),
            Path("/var/snap/docker/common/kubeauto-registry"),
        )

    def test_registry_data_path_uses_base_data_for_native_docker(self):
        rm = RegistryManager()
        rm.docker = MagicMock()
        rm.docker._run_docker.return_value.stdout = "/var/lib/docker\n"
        self.assertEqual(rm._registry_data_path(), rm.base_data_path / "registry")

    @patch("service.cluster.registry.time.sleep")
    def test_wait_succeeds_on_first_probe(self, mock_sleep):
        rm = RegistryManager()
        with patch.object(rm, "_registry_http_ok", return_value=True):
            self.assertTrue(rm._wait_for_registry_ready(timeout=5, interval=0.1))
        mock_sleep.assert_not_called()

    @patch("service.cluster.registry.time.sleep")
    def test_wait_retries_until_ok(self, mock_sleep):
        rm = RegistryManager()
        attempts = {"n": 0}

        def probe():
            attempts["n"] += 1
            return attempts["n"] >= 3

        with patch.object(rm, "_registry_http_ok", side_effect=probe):
            self.assertTrue(rm._wait_for_registry_ready(timeout=5, interval=0.1))
        self.assertGreaterEqual(mock_sleep.call_count, 2)

    @patch("service.cluster.registry.time.sleep")
    def test_wait_times_out(self, mock_sleep):
        rm = RegistryManager()
        with patch.object(rm, "_registry_http_ok", return_value=False):
            self.assertFalse(rm._wait_for_registry_ready(timeout=0.2, interval=0.1))

    def test_start_stopped_registry_waits_before_return(self):
        """Regression: start stopped container then push must not race GET /v2/ EOF."""
        rm = RegistryManager()
        rm.docker = MagicMock()
        rm.docker.is_container_running.return_value = False
        rm.docker.container_exists.return_value = True

        with patch.object(rm, "_ensure_local_registry_hostname"), patch.object(
            rm, "_wait_for_registry_ready", return_value=True
        ) as wait:
            rm.start_local_registry()
            rm.docker.start_container.assert_called_once_with("local_registry")
            rm.docker.set_container_restart_policy.assert_called_once_with("local_registry", "always")
            wait.assert_called_once()

    def test_start_raises_when_not_ready(self):
        rm = RegistryManager()
        rm.docker = MagicMock()
        rm.docker.is_container_running.return_value = True

        with patch.object(rm, "_ensure_local_registry_hostname"), patch.object(
            rm, "_wait_for_registry_ready", return_value=False
        ):
            with self.assertRaises(CommandExecutionError) as ctx:
                rm.start_local_registry()
            self.assertIn(":5000", str(ctx.exception))

    def test_already_running_still_waits(self):
        """Container 'running' is not enough — HTTP must answer before upload."""
        rm = RegistryManager()
        rm.docker = MagicMock()
        rm.docker.is_container_running.return_value = True

        with patch.object(rm, "_ensure_local_registry_hostname"), patch.object(
            rm, "_wait_for_registry_ready", return_value=True
        ) as wait:
            rm.start_local_registry()
            rm.docker.start_container.assert_not_called()
            rm.docker.set_container_restart_policy.assert_called_once_with("local_registry", "always")
            wait.assert_called_once()

    def test_local_registry_hostname_replaces_retired_mapping(self):
        rm = RegistryManager()
        rm.docker = MagicMock()
        with TemporaryDirectory() as temp_dir:
            hosts = Path(temp_dir) / "hosts"
            hosts.write_text("127.0.0.1 localhost\n192.168.47.147 registry.talkschool.cn\n")
            rm._ensure_local_registry_hostname(hosts)
            content = rm.docker._write_privileged_file.call_args.args[1]
        self.assertNotIn("192.168.47.147", content)
        self.assertIn("127.0.0.1  registry.talkschool.cn", content)

    def test_control_host_push_uses_loopback_not_registry_dns(self):
        rm = RegistryManager()
        rm.docker = MagicMock()
        with patch.object(rm, "start_local_registry"), patch.object(rm, "_ensure_image_local"):
            rm.upload_to_registry(["brinnatt/calico-cni:v3.28.4"])
        rm.docker.tag_image.assert_called_once_with(
            "brinnatt/calico-cni:v3.28.4", "127.0.0.1:5000/brinnatt/calico-cni:v3.28.4"
        )
        rm.docker.push_image.assert_called_once_with("127.0.0.1:5000/brinnatt/calico-cni:v3.28.4")


if __name__ == "__main__":
    unittest.main()
