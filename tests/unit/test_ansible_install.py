"""Ansible control-node installation regression tests."""

import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from common.ansible_python import AnsibleCoreProbeResult, ansible_python_policy_for_core
from common.exceptions import CommandExecutionError
from common.mirrors import (
    ANSIBLE_CORE_SPEC,
    HUAWEI_PYPI,
    UPSTREAM_PYPI,
    _ensure_pip,
    _find_preferred_control_python,
    _install_supported_ansible_core,
    install_ansible_with_system_pm,
)
from service.cluster.downloader import DownloadManager


class TestCrossDistributionAnsibleInstall(unittest.TestCase):
    @patch("common.mirrors.run_command")
    @patch("common.mirrors.shutil.which")
    def test_selects_python_from_official_control_range(self, which, run_command):
        which.side_effect = lambda name: {
            "python3.12": None,
            "python3.11": None,
            "python3.10": "/usr/bin/python3.10",
            "python3": "/usr/bin/python3",
        }.get(name)
        run_command.return_value = SimpleNamespace(returncode=0, stdout="3.10\n")

        python = _find_preferred_control_python(ansible_python_policy_for_core((2, 17)))

        self.assertEqual(python, "/usr/bin/python3.10")

    @patch("common.mirrors.run_command")
    @patch("common.mirrors._python_has_pip", side_effect=[False, False, True])
    def test_rhel_installs_pip_for_selected_python(self, _has_pip, run_command):
        _ensure_pip("/usr/bin/python3.12", "rhel", "dnf")

        self.assertIn(
            call(["dnf", "-y", "install", "python3.12-pip"], capture_output=False),
            run_command.call_args_list,
        )

    @patch("common.mirrors._ensure_pip")
    @patch("common.mirrors._find_preferred_control_python", return_value="/usr/bin/python3.10")
    @patch("common.mirrors.run_command")
    def test_installs_pinned_core_from_huawei_first(self, run_command, _find, ensure_pip):
        _install_supported_ansible_core("debian", "apt-get")

        ensure_pip.assert_called_once_with("/usr/bin/python3.10", "debian", "apt-get")
        pip_call = run_command.call_args_list[0]
        self.assertEqual(pip_call.args[0][-2:], [HUAWEI_PYPI, ANSIBLE_CORE_SPEC])
        self.assertEqual(pip_call.kwargs["env"]["PIP_BREAK_SYSTEM_PACKAGES"], "1")

    @patch("common.mirrors._ensure_pip")
    @patch("common.mirrors._find_preferred_control_python", return_value="/usr/bin/python3.10")
    @patch("common.mirrors.run_command")
    def test_falls_back_to_upstream_pypi(self, run_command, _find, _ensure_pip):
        def result(command, **_kwargs):
            if HUAWEI_PYPI in command:
                raise CommandExecutionError("Huawei mirror unavailable")

        run_command.side_effect = result
        _install_supported_ansible_core("suse", "zypper")

        indexes = [
            item.args[0][-2]
            for item in run_command.call_args_list
            if "pip" in item.args[0]
        ]
        self.assertEqual(indexes, [HUAWEI_PYPI, UPSTREAM_PYPI])

    @patch("common.mirrors._install_supported_ansible_core")
    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_ubuntu_uses_common_pinned_installer(
        self, run_command, os_release, _apply_mirror, install_core
    ):
        os_release.return_value = {"ID": "ubuntu", "ID_LIKE": "debian", "VERSION_ID": "22.04"}

        install_ansible_with_system_pm()

        run_command.assert_called_once_with(["apt-get", "update"], capture_output=False)
        install_core.assert_called_once_with("debian", "apt-get")

    @patch("common.mirrors._install_supported_ansible_core")
    @patch("common.mirrors._installed_ansible_is_compatible", return_value=False)
    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_rhel_falls_back_when_repo_ansible_is_incompatible(
        self, run_command, os_release, _mirror, _compatible, install_core
    ):
        os_release.return_value = {"ID": "rocky", "ID_LIKE": "rhel", "VERSION_ID": "8.10"}

        install_ansible_with_system_pm()

        self.assertIn(call(["dnf", "-y", "install", "ansible"], capture_output=False), run_command.call_args_list)
        install_core.assert_called_once_with("rhel", "dnf")

    @patch("common.mirrors._install_supported_ansible_core")
    @patch("common.mirrors._installed_ansible_is_compatible", return_value=False)
    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_openeuler_uses_native_repo_without_epel(
        self, run_command, os_release, _mirror, _compatible, _install_core
    ):
        os_release.return_value = {"ID": "openeuler", "ID_LIKE": "rhel", "VERSION_ID": "22.03"}

        install_ansible_with_system_pm()

        commands = [item.args[0] for item in run_command.call_args_list]
        self.assertNotIn(["dnf", "-y", "install", "epel-release"], commands)

    @patch("common.mirrors._install_supported_ansible_core")
    @patch("common.mirrors._installed_ansible_is_compatible", return_value=False)
    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_suse_falls_back_when_repo_ansible_is_incompatible(
        self, run_command, os_release, _mirror, _compatible, install_core
    ):
        os_release.return_value = {"ID": "opensuse-leap", "ID_LIKE": "suse", "VERSION_ID": "15.6"}

        install_ansible_with_system_pm()

        self.assertIn(
            call(["zypper", "--non-interactive", "install", "ansible"], capture_output=False),
            run_command.call_args_list,
        )
        install_core.assert_called_once_with("suse", "zypper")


class TestDownloadAnsibleGate(unittest.TestCase):
    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch("service.cluster.downloader.shutil.which", return_value="/usr/bin/ansible")
    @patch("service.cluster.downloader.probe_installed_ansible_core")
    def test_replaces_legacy_ansible_without_core_version(self, probe, _which, install):
        probe.side_effect = [
            AnsibleCoreProbeResult(version=None, attempts=()),
            AnsibleCoreProbeResult(version=(2, 17), attempts=(), control_python_version=(3, 10)),
        ]

        DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_called_once_with()

    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch(
        "service.cluster.downloader.probe_installed_ansible_core",
        return_value=AnsibleCoreProbeResult(
            version=(2, 17), attempts=(), control_python_version=(3, 10)
        ),
    )
    def test_keeps_compatible_ansible_core(self, _probe, install):
        DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_not_called()

    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch("service.cluster.downloader.shutil.which", return_value="/usr/bin/ansible")
    @patch("service.cluster.downloader.probe_installed_ansible_core")
    def test_replaces_core_running_on_unsupported_control_python(self, probe, _which, install):
        probe.side_effect = [
            AnsibleCoreProbeResult(
                version=(2, 17), attempts=(), control_python_version=(3, 9)
            ),
            AnsibleCoreProbeResult(
                version=(2, 17), attempts=(), control_python_version=(3, 10)
            ),
        ]

        DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
