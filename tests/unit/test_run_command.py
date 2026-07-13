"""Unit tests for common.utils.run_command (subprocess wrapper consistency)."""

import unittest

from common.utils import run_command, CommandExecutionError


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


if __name__ == "__main__":
    unittest.main()
