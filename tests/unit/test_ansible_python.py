"""Unit tests for ansible-core matrix and Python interpreter policy."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common.ansible_python import (
    AnsibleCoreProbeResult,
    AnsibleCoreDetectionError,
    ansible_core_probe_is_compatible,
    ansible_python_policy,
    clear_ansible_python_policy_cache,
    format_ansible_core_detection_failure,
    parse_ansible_control_python_version,
    parse_ansible_core_version,
    probe_installed_ansible_core,
    detect_target_python_cmd,
    python_meets_spec,
    validate_local_interpreter,
    validate_remote_interpreter_version,
    _lookup_matrix,
    _query_native_package_owner,
)
from common.exceptions import AnsibleCoreDetectionError as DetectionErrorAlias


class TestAnsibleCoreParsing(unittest.TestCase):
    def test_parse_ansible_core_version(self):
        text = "ansible [core 2.16.3]\n  config file = /etc/ansible/ansible.cfg"
        self.assertEqual(parse_ansible_core_version(text), (2, 16))

    def test_parse_ansible_core_version_missing(self):
        self.assertIsNone(parse_ansible_core_version("python version = 3.10.0"))

    def test_parse_legacy_ansible_version(self):
        self.assertEqual(parse_ansible_core_version("ansible 2.9.27"), (2, 9))

    def test_parse_control_python_version(self):
        text = "ansible [core 2.17.14]\n  python version = 3.10.12 (main) [/usr/bin/python3]"
        self.assertEqual(parse_ansible_control_python_version(text), (3, 10))


class TestSupportMatrix(unittest.TestCase):
    def test_legacy_29_policy_matches_official_python_floor(self):
        policy = _lookup_matrix((2, 9))
        self.assertEqual(policy.control_min, (3, 5))
        self.assertEqual(policy.target_module_runtime_min, (3, 5))
        self.assertEqual(policy.target_documented_max, (3, 8))

    def test_legacy_210_target_range_matches_official_matrix(self):
        policy = _lookup_matrix((2, 10))
        self.assertTrue(validate_remote_interpreter_version("3.9", policy))
        self.assertFalse(validate_remote_interpreter_version("3.10", policy))
        self.assertFalse(validate_remote_interpreter_version("3.13", policy))

    def test_218_accepts_debian_13_python(self):
        policy = _lookup_matrix((2, 18))
        self.assertTrue(validate_remote_interpreter_version("3.13", policy))

    def test_detection_command_enforces_upper_bound(self):
        command = detect_target_python_cmd(_lookup_matrix((2, 10)))
        self.assertIn("sys.version_info[:2] >= (3, 5)", command)
        self.assertIn("sys.version_info[:2] <= (3, 9)", command)

    def test_matrix_2_16_target_runtime_min(self):
        policy = _lookup_matrix((2, 16))
        self.assertEqual(policy.target_module_runtime_min, (3, 8))

    def test_matrix_2_17_target_runtime_min(self):
        policy = _lookup_matrix((2, 17))
        self.assertEqual(policy.target_module_runtime_min, (3, 9))

    def test_unknown_core_is_not_guessed_from_latest_matrix_row(self):
        with self.assertRaises(AnsibleCoreDetectionError):
            _lookup_matrix((2, 99))

    def test_native_package_uses_distribution_control_python_contract(self):
        compatible = AnsibleCoreProbeResult(
            version=(2, 17), attempts=(), control_python_version=(3, 10),
            executable_path="/usr/bin/ansible", package_owner="rpm:ansible-core",
        )
        distro_patched = AnsibleCoreProbeResult(
            version=(2, 17), attempts=(), control_python_version=(3, 9),
            executable_path="/usr/bin/ansible", package_owner="rpm:ansible-core",
        )
        self.assertTrue(ansible_core_probe_is_compatible(compatible))
        self.assertTrue(ansible_core_probe_is_compatible(distro_patched))

    def test_unowned_pip_runtime_is_incompatible(self):
        probe = AnsibleCoreProbeResult(
            version=(2, 17), attempts=(), control_python_version=(3, 10),
            executable_path="/usr/local/bin/ansible", package_owner=None,
        )
        self.assertFalse(ansible_core_probe_is_compatible(probe))


class TestAnsiblePythonPolicy(unittest.TestCase):
    def tearDown(self):
        clear_ansible_python_policy_cache()

    @patch("common.ansible_python.run_command")
    @patch("common.ansible_python.shutil.which")
    @patch("common.ansible_python._query_native_package_owner", return_value="rpm:ansible-core")
    def test_policy_resolves_from_ansible_version(self, _owner, mock_which, mock_run):
        mock_which.side_effect = lambda name: "/usr/bin/ansible" if name == "ansible" else None
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = (
            "ansible [core 2.16.3]\n"
            "  python version = 3.12.4 (main) [/usr/bin/python3.12]\n"
        )
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


class TestNativePackageOwnership(unittest.TestCase):
    @patch("common.ansible_python.run_command")
    @patch("common.ansible_python.shutil.which")
    def test_detects_rpm_owner(self, which, run_command):
        which.side_effect = lambda name: "/usr/bin/rpm" if name == "rpm" else None
        run_command.return_value = SimpleNamespace(returncode=0, stdout="ansible-core-2.18.3\n")

        self.assertEqual(
            _query_native_package_owner("/usr/bin/ansible"),
            "rpm:ansible-core-2.18.3",
        )

    @patch("common.ansible_python.run_command")
    @patch("common.ansible_python.shutil.which")
    def test_detects_deb_owner(self, which, run_command):
        which.side_effect = lambda name: "/usr/bin/dpkg-query" if name == "dpkg-query" else None
        run_command.return_value = SimpleNamespace(returncode=0, stdout="ansible-core: /usr/bin/ansible\n")

        self.assertEqual(
            _query_native_package_owner("/usr/bin/ansible"),
            "deb:ansible-core",
        )


if __name__ == "__main__":
    unittest.main()
