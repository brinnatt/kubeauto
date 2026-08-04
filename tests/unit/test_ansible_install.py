"""Ansible control-node native-package installation regression tests."""

import unittest
from pathlib import Path
from unittest.mock import call, patch

from common.ansible_python import AnsibleCoreProbeResult
from common.mirrors import install_ansible_with_system_pm
from service.cluster.downloader import DownloadManager


class TestCrossDistributionAnsibleInstall(unittest.TestCase):
    def test_product_installer_has_no_global_pip_path(self):
        source = (Path(__file__).parents[2] / "common" / "mirrors.py").read_text()
        self.assertNotIn("pip install", source)
        self.assertNotIn("PIP_BREAK_SYSTEM_PACKAGES", source)
        self.assertNotIn("pypi", source.lower())

    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_ubuntu_uses_native_apt_package(self, run_command, os_release, mirror):
        os_release.return_value = {"ID": "ubuntu", "ID_LIKE": "debian", "VERSION_ID": "22.04"}

        install_ansible_with_system_pm()

        mirror.assert_called_once_with()
        self.assertEqual(
            run_command.call_args_list,
            [
                call(["apt-get", "update"], capture_output=False),
                call(["apt-get", "-y", "install", "ansible"], capture_output=False),
            ],
        )

    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_rocky_uses_epel_then_native_rpm(self, run_command, os_release, mirror):
        os_release.return_value = {"ID": "rocky", "ID_LIKE": "rhel", "VERSION_ID": "8.10"}

        install_ansible_with_system_pm()

        self.assertEqual(mirror.call_count, 2)
        self.assertEqual(
            run_command.call_args_list,
            [
                call(["dnf", "-y", "install", "epel-release"], capture_output=False),
                call(["dnf", "-y", "install", "ansible"], capture_output=False),
            ],
        )

    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_openeuler_uses_native_repo_without_epel(self, run_command, os_release, _mirror):
        os_release.return_value = {"ID": "openeuler", "ID_LIKE": "rhel", "VERSION_ID": "22.03"}

        install_ansible_with_system_pm()

        self.assertEqual(
            run_command.call_args_list,
            [call(["dnf", "-y", "install", "ansible"], capture_output=False)],
        )

    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_anolis_does_not_mix_epel_into_the_distribution(self, run_command, os_release, _mirror):
        os_release.return_value = {"ID": "anolis", "ID_LIKE": "", "VERSION_ID": "23.3"}

        install_ansible_with_system_pm()

        self.assertEqual(
            run_command.call_args_list,
            [call(["dnf", "-y", "install", "ansible"], capture_output=False)],
        )

    @patch("common.mirrors.apply_huawei_mirror")
    @patch("common.mirrors.platform.freedesktop_os_release")
    @patch("common.mirrors.run_command")
    def test_suse_uses_native_zypper_package(self, run_command, os_release, _mirror):
        os_release.return_value = {"ID": "opensuse-leap", "ID_LIKE": "suse", "VERSION_ID": "16.0"}

        install_ansible_with_system_pm()

        self.assertEqual(
            run_command.call_args_list,
            [call(["zypper", "--non-interactive", "install", "ansible"], capture_output=False)],
        )


class TestDownloadAnsibleGate(unittest.TestCase):
    @patch("service.cluster.downloader.RegistryManager._ensure_image_local")
    def test_execution_environment_uses_standard_dual_registry_pull(self, ensure_image):
        manager = DownloadManager.__new__(DownloadManager)
        manager.kube_constant = type(
            "Constants", (), {"ansible_execution_image": "brinnatt/ansible:2.18.6"}
        )()
        manager.registry = type(
            "Registry", (), {"_ensure_image_local": ensure_image}
        )()

        manager.get_ansible_execution_env()

        ensure_image.assert_called_once_with("brinnatt/ansible:2.18.6")

    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch("service.cluster.downloader.shutil.which", return_value=None)
    @patch("service.cluster.downloader.probe_installed_ansible_core")
    def test_missing_ansible_installs_and_verifies_native_package(self, probe, _which, install):
        probe.side_effect = [
            AnsibleCoreProbeResult(version=None, attempts=()),
            AnsibleCoreProbeResult(
                version=(2, 18),
                attempts=(),
                control_python_version=(3, 13),
                executable_path="/usr/bin/ansible",
                package_owner="rpm:ansible-core-2.18.3",
            ),
        ]

        DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_called_once_with()

    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch(
        "service.cluster.downloader.probe_installed_ansible_core",
        return_value=AnsibleCoreProbeResult(
            version=(2, 9),
            attempts=(),
            control_python_version=(3, 9),
            executable_path="/usr/bin/ansible",
            package_owner="rpm:ansible-2.9.27",
        ),
    )
    def test_keeps_native_legacy_ansible(self, _probe, install):
        DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_not_called()

    @patch("service.cluster.downloader.install_ansible_with_system_pm")
    @patch("service.cluster.downloader.shutil.which", return_value="/usr/local/bin/ansible")
    @patch(
        "service.cluster.downloader.probe_installed_ansible_core",
        return_value=AnsibleCoreProbeResult(
            version=(2, 17),
            attempts=(),
            control_python_version=(3, 10),
            executable_path="/usr/local/bin/ansible",
            package_owner=None,
        ),
    )
    def test_rejects_unowned_pip_ansible_without_overwriting(self, _probe, _which, install):
        with self.assertRaisesRegex(RuntimeError, "not owned by the system RPM/deb"):
            DownloadManager.__new__(DownloadManager).get_ansible_env()

        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
