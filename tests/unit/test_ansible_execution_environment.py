"""Conditional Ansible execution-environment product contract."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.ansible_python import ansible_python_policy_for_core
from common.exceptions import NoCompatibleAnsibleTargetPython
from service.cluster.manager import (
    ClusterManager,
    _effective_user_home,
    _effective_user_name,
    _prepare_inventory_with_python,
)


class TestInventoryCompatibilityGate(unittest.TestCase):
    def test_local_connection_uses_native_distribution_controller_contract(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            inventory = Path(tmp_dir) / "hosts"
            inventory.write_text(
                "[etcd]\n192.168.47.138 ansible_connection=local\n"
                "[kube_master]\n192.168.47.138 ansible_connection=local\n"
                "[all:vars]\n",
                encoding="utf-8",
            )
            with (
                patch("service.cluster.manager._ensure_local_ansible_python") as local_probe,
                patch("service.cluster.manager._ensure_ansible_python") as remote_probe,
            ):
                prepared = _prepare_inventory_with_python(
                    inventory, ansible_python_policy_for_core((2, 10))
                )

        self.assertEqual(prepared, inventory)
        local_probe.assert_not_called()
        remote_probe.assert_not_called()

    def test_execution_environment_reaches_explicit_local_target_over_ssh(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            inventory = Path(tmp_dir) / "hosts"
            inventory.write_text(
                "[etcd]\n"
                "192.168.47.138 ansible_connection=local ansible_become=true\n"
                "[all:vars]\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "service.cluster.manager._effective_user_name",
                    return_value="ubuntu",
                ),
                patch(
                    "service.cluster.manager._ensure_ansible_python",
                    return_value="/usr/bin/python3.10",
                ) as remote_probe,
            ):
                prepared = _prepare_inventory_with_python(
                    inventory,
                    ansible_python_policy_for_core((2, 18)),
                    execution_environment=True,
                )

            try:
                content = prepared.read_text(encoding="utf-8")
            finally:
                prepared.unlink(missing_ok=True)

        self.assertIn("ansible_connection=ssh", content)
        self.assertIn("ansible_user=ubuntu", content)
        self.assertIn("ansible_python_interpreter=/usr/bin/python3.10", content)
        self.assertNotIn("ansible_connection=local", content)
        remote_probe.assert_called_once()
        self.assertEqual(remote_probe.call_args.args[2], "ubuntu")

    def test_incompatible_target_fails_before_ansible_auto_discovery(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            inventory = Path(tmp_dir) / "hosts"
            inventory.write_text(
                "[etcd]\n192.168.47.128\n[kube_master]\n192.168.47.128\n"
                "[kube_node]\n192.168.47.128\n\n[all:vars]\n"
                "ansible_user=brinnatt\n",
                encoding="utf-8",
            )
            with patch(
                "service.cluster.manager._ensure_ansible_python", return_value=None
            ):
                with self.assertRaises(NoCompatibleAnsibleTargetPython) as ctx:
                    _prepare_inventory_with_python(
                        inventory, ansible_python_policy_for_core((2, 10))
                    )

        self.assertEqual(ctx.exception.hosts, ("192.168.47.128",))
        self.assertIn("3.5-3.9", str(ctx.exception))


class TestExecutionEnvironmentRunner(unittest.TestCase):
    def test_effective_user_name_uses_effective_uid(self):
        with (
            patch("service.cluster.manager.os.geteuid", return_value=1000),
            patch(
                "service.cluster.manager.pwd.getpwuid",
                return_value=SimpleNamespace(pw_name="ubuntu"),
            ) as getpwuid,
        ):
            self.assertEqual(_effective_user_name(), "ubuntu")

        getpwuid.assert_called_once_with(1000)

    def test_ssh_home_uses_effective_uid_not_preserved_sudo_home(self):
        with (
            patch("service.cluster.manager.os.geteuid", return_value=0),
            patch(
                "service.cluster.manager.pwd.getpwuid",
                return_value=SimpleNamespace(pw_dir="/root"),
            ) as getpwuid,
            patch("service.cluster.manager.Path.home", return_value=Path("/home/ubuntu")),
        ):
            self.assertEqual(_effective_user_home(), Path("/root"))

        getpwuid.assert_called_once_with(0)

    def test_runner_uses_official_container_private_data_layout(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            user_home = root / "user-home"
            (user_home / ".ssh").mkdir(parents=True)
            inventory = root / "source.hosts"
            inventory.write_text("[all]\n192.168.47.128\n", encoding="utf-8")
            manager = ClusterManager.__new__(ClusterManager)
            manager.base_path = root
            manager.kube_constant = SimpleNamespace(
                v_ansible_core="2.18.6",
                ansible_execution_image="brinnatt/ansible:2.18.6",
            )
            runner_result = SimpleNamespace(rc=0)
            with (
                patch.object(
                    manager,
                    "_ensure_ansible_execution_image",
                    return_value="brinnatt/ansible:2.18.6",
                ),
                patch(
                    "service.cluster.manager.ansible_runner.run",
                    return_value=runner_result,
                ) as run,
                patch(
                    "service.cluster.manager._effective_user_home",
                    return_value=user_home,
                ),
            ):
                result = manager._run_playbook_in_execution_environment(
                    tmp_dir=tmp_dir,
                    playbook="/usr/local/kubeauto/playbooks/90.setup.yml",
                    inventory=inventory,
                    extravars={},
                    cmdline="",
                    envvars={},
                    kubeconfig=root / "kubectl.kubeconfig",
                )

            kwargs = run.call_args.kwargs
            self.assertIs(result, runner_result)
            self.assertTrue(kwargs["process_isolation"])
            self.assertEqual(kwargs["process_isolation_executable"], "docker")
            self.assertEqual(kwargs["container_image"], "brinnatt/ansible:2.18.6")
            self.assertEqual(kwargs["inventory"], "hosts")
            self.assertEqual(kwargs["envvars"]["ANSIBLE_CONFIG"], "/runner/ansible.cfg")
            self.assertEqual(kwargs["envvars"]["HOME"], str(user_home))
            self.assertIn("host", kwargs["container_options"])
            self.assertIn(
                f"{user_home / '.ssh'}:{user_home / '.ssh'}:ro",
                kwargs["container_volume_mounts"],
            )
            self.assertIn(
                f"{user_home / '.kube'}:{user_home / '.kube'}:rw",
                kwargs["container_volume_mounts"],
            )
            self.assertTrue((user_home / ".kube").is_dir())
            self.assertNotIn(
                f"{user_home / '.ssh'}:/runner/.ssh:ro",
                kwargs["container_volume_mounts"],
            )
            self.assertTrue((root / "inventory" / "hosts").is_file())

    def test_native_runner_is_preserved_when_target_policy_is_compatible(self):
        manager = ClusterManager.__new__(ClusterManager)
        manager.clusters_dir = Path("/unused")
        manager.base_path = Path("/usr/local/kubeauto")
        manager.kube_constant = SimpleNamespace(v_ansible_core="2.18.6")
        inventory = Path("/tmp/native-compatible.hosts")
        native_result = SimpleNamespace(rc=0)
        with (
            patch("service.cluster.manager.ansible_python_policy") as native_policy,
            patch(
                "service.cluster.manager._prepare_inventory_with_python",
                return_value=inventory,
            ),
            patch.object(manager, "_write_ansible_cfg"),
            patch.object(manager, "_ansible_runner_envvars", return_value={}),
            patch(
                "service.cluster.manager.ansible_runner.run",
                return_value=native_result,
            ) as run,
            patch.object(manager, "_run_playbook_in_execution_environment") as ee_run,
            patch("service.cluster.manager.get_host_ip", return_value="192.168.47.138"),
        ):
            result = manager._run_playbook(
                "cluster", Path("/tmp/playbook.yml"), inventory=inventory, extra_vars={}
            )

        self.assertIs(result, native_result)
        native_policy.assert_called_once_with()
        run.assert_called_once()
        ee_run.assert_not_called()

    def test_incompatible_native_policy_switches_to_218_execution_environment(self):
        manager = ClusterManager.__new__(ClusterManager)
        manager.clusters_dir = Path("/unused")
        manager.base_path = Path("/usr/local/kubeauto")
        manager.kube_constant = SimpleNamespace(v_ansible_core="2.18.6")
        inventory = Path("/tmp/debian-incompatible.hosts")
        ee_inventory = Path("/tmp/debian-ee.hosts")
        native_policy = ansible_python_policy_for_core((2, 10))
        ee_result = SimpleNamespace(rc=0)
        prepare_results = [
            NoCompatibleAnsibleTargetPython(
                "2.10", "3.5-3.9", ["192.168.47.128"]
            ),
            ee_inventory,
        ]
        with (
            patch(
                "service.cluster.manager.ansible_python_policy",
                return_value=native_policy,
            ),
            patch(
                "service.cluster.manager._prepare_inventory_with_python",
                side_effect=prepare_results,
            ) as prepare,
            patch.object(manager, "_write_ansible_cfg"),
            patch.object(manager, "_ansible_runner_envvars", return_value={}),
            patch.object(
                manager,
                "_run_playbook_in_execution_environment",
                return_value=ee_result,
            ) as ee_run,
            patch("service.cluster.manager.ansible_runner.run") as native_run,
            patch("service.cluster.manager.get_host_ip", return_value="192.168.47.138"),
        ):
            result = manager._run_playbook(
                "cluster", Path("/tmp/playbook.yml"), inventory=inventory, extra_vars={}
            )

        self.assertIs(result, ee_result)
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(prepare.call_args_list[0].args, (inventory, native_policy))
        self.assertEqual(prepare.call_args_list[1].args[1].core_version, (2, 18))
        self.assertTrue(
            prepare.call_args_list[1].kwargs["execution_environment"]
        )
        native_run.assert_not_called()
        ee_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
