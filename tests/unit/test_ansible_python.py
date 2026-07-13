"""Unit tests for ansible-core matrix and Python interpreter policy."""

import unittest
from unittest.mock import patch

from common.ansible_python import (
    AnsibleCoreDetectionError,
    ansible_python_policy,
    clear_ansible_python_policy_cache,
    format_ansible_core_detection_failure,
    parse_ansible_core_version,
    probe_installed_ansible_core,
    python_meets_spec,
    validate_local_interpreter,
    validate_remote_interpreter_version,
    _lookup_matrix,
)
from common.exceptions import AnsibleCoreDetectionError as DetectionErrorAlias


class TestAnsibleCoreParsing(unittest.TestCase):
    def test_parse_ansible_core_version(self):
        text = "ansible [core 2.16.3]\n  config file = /etc/ansible/ansible.cfg"
        self.assertEqual(parse_ansible_core_version(text), (2, 16))

    def test_parse_ansible_core_version_missing(self):
        self.assertIsNone(parse_ansible_core_version("ansible 2.10.0"))


class TestSupportMatrix(unittest.TestCase):
    def test_matrix_2_16_target_runtime_min(self):
        policy = _lookup_matrix((2, 16))
        self.assertEqual(policy.target_module_runtime_min, (3, 8))

    def test_matrix_2_17_target_runtime_min(self):
        policy = _lookup_matrix((2, 17))
        self.assertEqual(policy.target_module_runtime_min, (3, 9))


class TestAnsiblePythonPolicy(unittest.TestCase):
    def tearDown(self):
        clear_ansible_python_policy_cache()

    @patch("common.ansible_python.run_command")
    @patch("common.ansible_python.shutil.which")
    def test_policy_resolves_from_ansible_version(self, mock_which, mock_run):
        mock_which.side_effect = lambda name: "/usr/bin/ansible" if name == "ansible" else None
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "ansible [core 2.16.3]\n"
        mock_run.return_value.stderr = ""

        policy = ansible_python_policy()
        self.assertEqual(policy.core_version, (2, 16))
        self.assertEqual(policy.target_module_runtime_min, (3, 8))

    @patch("common.ansible_python.run_command")
    @patch("common.ansible_python.shutil.which")
    def test_policy_fails_fast_when_detection_breaks(self, mock_which, mock_run):
        mock_which.side_effect = lambda name: "/usr/bin/ansible" if name == "ansible" else None
        mock_run.side_effect = RuntimeError("broken subprocess")

        with self.assertRaises(AnsibleCoreDetectionError) as ctx:
            ansible_python_policy()
        self.assertIn("Cannot detect ansible-core", str(ctx.exception))
        self.assertIsInstance(ctx.exception, DetectionErrorAlias)

    @patch("common.ansible_python.shutil.which")
    def test_probe_reports_missing_ansible_in_path(self, mock_which):
        mock_which.return_value = None
        result = probe_installed_ansible_core()
        self.assertIsNone(result.version)
        message = format_ansible_core_detection_failure(result)
        self.assertIn("not found in PATH", message)


class TestInterpreterValidation(unittest.TestCase):
    def test_python_meets_spec(self):
        self.assertTrue(python_meets_spec(3, 9, (3, 8), (3, 12)))
        self.assertFalse(python_meets_spec(3, 7, (3, 8), (3, 12)))

    def test_validate_remote_interpreter_version(self):
        policy = _lookup_matrix((2, 16))
        self.assertTrue(validate_remote_interpreter_version("3.9\n", policy))
        self.assertFalse(validate_remote_interpreter_version("3.6\n", policy))

    @patch("common.ansible_python.run_command")
    def test_validate_local_interpreter(self, mock_run):
        policy = _lookup_matrix((2, 16))
        mock_run.return_value.stdout = "3.10\n"
        self.assertTrue(validate_local_interpreter("/usr/bin/python3.10", policy))


if __name__ == "__main__":
    unittest.main()
