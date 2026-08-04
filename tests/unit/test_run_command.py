"""Unit tests for common.utils.run_command (subprocess wrapper consistency)."""

import os
import sys
import unittest
from unittest.mock import patch

from common.utils import _system_subprocess_env, run_command, CommandExecutionError


class TestRunCommand(unittest.TestCase):
    def test_capture_output_default_captures_stdout(self):
        result = run_command(["echo", "kubeauto"], check=False)
        self.assertEqual(result.stdout.strip(), "kubeauto")

    def test_capture_alias_is_supported(self):
        """Regression: capture=True must not leak to subprocess.run as invalid kwarg."""
        result = run_command(["echo", "alias-ok"], capture=True, check=False)
        self.assertEqual(result.stdout.strip(), "alias-ok")

    def test_capture_output_explicit(self):
        result = run_command(["echo", "explicit"], capture_output=True, check=False)
        self.assertEqual(result.stdout.strip(), "explicit")

    @patch("common.utils.subprocess.run")
    def test_frozen_linux_child_restores_original_library_path(self, subprocess_run):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch("common.utils.sys.platform", "linux"),
            patch.dict(
                os.environ,
                {
                    "LD_LIBRARY_PATH": "/tmp/_MEI-test",
                    "LD_LIBRARY_PATH_ORIG": "/customer/lib",
                },
                clear=True,
            ),
        ):
            run_command(["zypper", "--version"])

        child_env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(child_env["LD_LIBRARY_PATH"], "/customer/lib")

    @patch("common.utils.subprocess.run")
    def test_frozen_linux_child_clears_bundle_path_without_original(self, subprocess_run):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch("common.utils.sys.platform", "linux"),
            patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI-test"}, clear=True),
        ):
            run_command(["bash", "-c", "true"])

        child_env = subprocess_run.call_args.kwargs["env"]
        self.assertNotIn("LD_LIBRARY_PATH", child_env)

    @patch("common.utils.subprocess.run")
    def test_explicit_environment_is_owned_by_caller(self, subprocess_run):
        explicit_env = {
            "LD_LIBRARY_PATH": "/caller/lib",
            "LD_LIBRARY_PATH_ORIG": "/host/lib",
            "PYTHONNOUSERSITE": "1",
        }
        with (
            patch.object(sys, "frozen", True, create=True),
            patch("common.utils.sys.platform", "linux"),
        ):
            run_command(["ansible-playbook", "--version"], env=explicit_env)

        self.assertIs(subprocess_run.call_args.kwargs["env"], explicit_env)

    def test_env_update_adapter_clears_inherited_bundle_path(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch("common.utils.sys.platform", "linux"),
            patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI-test"}, clear=True),
        ):
            self.assertEqual(
                _system_subprocess_env(for_env_update=True),
                {"LD_LIBRARY_PATH": ""},
            )


if __name__ == "__main__":
    unittest.main()
