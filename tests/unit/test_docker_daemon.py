"""Docker daemon readiness and pull execution."""
import unittest
from unittest.mock import MagicMock, patch

from docker.errors import APIError

from common.exceptions import CommandExecutionError
from service.cluster.docker import DockerManager, _SDK_CLI_FALLBACK_MSG


class TestDockerDaemonReady(unittest.TestCase):
    @patch("service.cluster.docker.time.sleep")
    def test_wait_for_daemon_ready_succeeds_on_first_ping(self, mock_sleep):
        dm = DockerManager()
        with patch.object(dm, "_docker_info_ok", return_value=True), patch.object(
            dm, "_initialize_docker_client", side_effect=lambda: setattr(dm, "_client", MagicMock())
        ):
            self.assertTrue(dm._wait_for_daemon_ready(timeout=5, interval=0.1))
        mock_sleep.assert_not_called()

    @patch("service.cluster.docker.time.sleep")
    def test_wait_for_daemon_ready_retries_until_sdk_ok(self, mock_sleep):
        dm = DockerManager()
        attempts = {"n": 0}

        def info_ok():
            attempts["n"] += 1
            return attempts["n"] >= 2

        def init_client():
            if attempts["n"] >= 2:
                dm._client = MagicMock()

        with patch.object(dm, "_docker_info_ok", side_effect=info_ok), patch.object(
            dm, "_initialize_docker_client", side_effect=init_client
        ):
            self.assertTrue(dm._wait_for_daemon_ready(timeout=5, interval=0.1))
        self.assertGreaterEqual(mock_sleep.call_count, 1)

    @patch("service.cluster.docker.time.sleep")
    def test_wait_for_daemon_ready_times_out(self, mock_sleep):
        dm = DockerManager()
        with patch.object(dm, "_docker_info_ok", return_value=False):
            self.assertFalse(dm._wait_for_daemon_ready(timeout=0.2, interval=0.1))


class TestPullImage(unittest.TestCase):
    def test_execute_pull_sdk_fallback_uses_warning(self):
        dm = DockerManager()
        dm._client = MagicMock()
        dm._client.api.pull.side_effect = APIError("simulated pull failure")

        with patch("service.cluster.docker.os.geteuid", return_value=0), patch.object(
            dm, "_run_docker"
        ) as mock_cli, patch("service.cluster.docker.logger") as mock_logger:
            dm._execute_pull("brinnatt/pause:3.10")
            mock_logger.warning.assert_called()
            mock_cli.assert_called_once()

    def test_pull_image_logs_error_on_failure(self):
        dm = DockerManager()
        with patch.object(
            dm, "_execute_pull", side_effect=CommandExecutionError("pull failed", 1)
        ), patch("service.cluster.docker.logger") as mock_logger:
            with self.assertRaises(CommandExecutionError):
                dm.pull_image("brinnatt/pause:3.10")
            mock_logger.error.assert_called_once()

    def test_sdk_fallback_message_is_not_init_failure(self):
        msg = _SDK_CLI_FALLBACK_MSG.lower()
        self.assertIn("retrying via docker cli", msg)
        self.assertNotIn("initialize", msg)


if __name__ == "__main__":
    unittest.main()
