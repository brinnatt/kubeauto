"""Static delivery contracts for the independent Ceph branch."""

from __future__ import annotations

import hashlib
import importlib.util
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import yaml
from jinja2 import Environment

from common.constants import KubeConstant
from service.cluster.downloader import DownloadManager


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT.parent
CONTRACT_PATH = ROOT / "tests/helpers/ceph_contract.py"
CONTRACT_SPEC = importlib.util.spec_from_file_location("ceph_contract", CONTRACT_PATH)
assert CONTRACT_SPEC and CONTRACT_SPEC.loader
CEPH_CONTRACT = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(CEPH_CONTRACT)
RUNTIME_PATH = ROOT / "tests/helpers/ceph_runtime_cleanup.py"
RUNTIME_SPEC = importlib.util.spec_from_file_location("ceph_runtime_cleanup", RUNTIME_PATH)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
CEPH_RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(CEPH_RUNTIME)
S3_PATH = ROOT / "tests/helpers/ceph_s3_put.py"
S3_SPEC = importlib.util.spec_from_file_location("ceph_s3_put", S3_PATH)
assert S3_SPEC and S3_SPEC.loader
S3_CLIENT = importlib.util.module_from_spec(S3_SPEC)
S3_SPEC.loader.exec_module(S3_CLIENT)
KERNEL_PATH = ROOT / "roles/ceph/files/kernel-client-check.py"
KERNEL_SPEC = importlib.util.spec_from_file_location("ceph_kernel_check", KERNEL_PATH)
assert KERNEL_SPEC and KERNEL_SPEC.loader
CEPH_KERNEL = importlib.util.module_from_spec(KERNEL_SPEC)
KERNEL_SPEC.loader.exec_module(CEPH_KERNEL)


class TestCephDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.constants = KubeConstant()
        cls.config = (ROOT / "conf/config.yml").read_text(encoding="utf-8")
        cls.inventory = (ROOT / "conf/hosts.multi-node").read_text(encoding="utf-8")
        cls.manager = (ROOT / "service/cluster/manager.py").read_text(encoding="utf-8")
        cls.cli = (ROOT / "controller/cluster/cli.py").read_text(encoding="utf-8")
        cls.role = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "roles/ceph/tasks").glob("*.yml"))
        )
        cls.runner = (ROOT / "tests/run_enterprise_regression.sh").read_text(encoding="utf-8")
        cls.regression = (ROOT / "tests/helpers/ceph-regression.sh").read_text(encoding="utf-8")
        cls.cleanup = (ROOT / "tests/helpers/ceph-cleanup.sh").read_text(encoding="utf-8")
        cls.lab_bootstrap = (
            ROOT / "tests/helpers/ceph-lab-hostname-bootstrap.sh"
        ).read_text(encoding="utf-8")
        cls.matrix = yaml.safe_load((ROOT / "tests/ceph-test-matrix.yaml").read_text(encoding="utf-8"))

    def test_versions_are_exact_official_pins(self) -> None:
        self.assertEqual(self.constants.v_ceph, "20.2.4")
        self.assertEqual(self.constants.v_ceph_csi, "v3.17.1")
        self.assertEqual(self.constants.v_ceph_csi_provisioner, "v6.2.0")
        self.assertEqual(self.constants.v_ceph_csi_attacher, "v4.11.0")
        self.assertEqual(self.constants.v_ceph_csi_resizer, "v2.1.0")
        self.assertEqual(self.constants.v_ceph_csi_snapshotter, "v8.5.0")
        self.assertEqual(self.constants.v_ceph_csi_registrar, "v2.16.0")

    def test_product_entry_is_independent_step(self) -> None:
        self.assertRegex(self.manager, r'"08":\s*"08\.ceph\.yml"')
        self.assertRegex(self.manager, r'"ceph":\s*"08\.ceph\.yml"')
        self.assertIn('"08"', self.cli)
        self.assertIn('"ceph"', self.cli)
        self.assertTrue((ROOT / "playbooks/08.ceph.yml").is_file())
        self.assertNotIn("import_tasks: ceph.yml", (ROOT / "roles/cluster-addon/tasks/main.yml").read_text())

    def test_csi_local_play_preserves_remote_delegation(self) -> None:
        plays = yaml.safe_load((ROOT / "playbooks/08.ceph.yml").read_text())
        csi_play = next(play for play in plays if play["hosts"] == "localhost")
        # Implicit localhost is local; forcing the play local also affects delegates.
        self.assertNotIn("connection", csi_play)
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/csi.yml").read_text())
        tasks += yaml.safe_load((ROOT / "roles/ceph/tasks/client-preflight.yml").read_text())
        delegated = [task for task in tasks if "delegate_to" in task]
        self.assertEqual(len(delegated), 5)
        for task in delegated:
            with self.subTest(task=task["name"]):
                if task.get("register") == "ceph_kernel_clients":
                    self.assertEqual(task["delegate_to"], "{{ item }}")
                    continue
                self.assertEqual(task["delegate_to"], "{{ groups['ceph_bootstrap'][0] }}")
                self.assertNotIn("connection", task)
                self.assertTrue(task["become"])

    def test_disabled_ceph_plays_do_not_gather_facts(self) -> None:
        plays = yaml.safe_load((ROOT / "playbooks/08.ceph.yml").read_text())
        for ceph, csi in (("no", "no"), ("no", "yes"), ("yes", "no"), ("yes", "yes")):
            for play in plays:
                with self.subTest(ceph=ceph, csi=csi, play=play["hosts"]):
                    expected = ceph == "yes" and (play["hosts"] != "localhost" or csi == "yes")
                    expression = play.get("gather_facts", "true")
                    actual = Environment().from_string(str(expression)).render(
                        ceph_install=ceph, ceph_csi_install=csi,
                    ).lower() == "true"
                    self.assertEqual(actual, expected)

    def test_csi_rollout_stops_at_first_failed_workload(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/csi.yml").read_text())
        task = next(task for task in tasks if task["name"] == "Wait for every Ceph-CSI workload")
        self.assertNotIn("loop", task)
        workloads = [
            "deployment/ceph-csi-rbd-provisioner", "daemonset/ceph-csi-rbd-nodeplugin",
            "deployment/ceph-csi-cephfs-provisioner", "daemonset/ceph-csi-cephfs-nodeplugin",
        ]
        variables = {"cluster_dir": "/tmp/cluster with spaces;false", "ceph_csi_namespace": "ceph-csi"}
        argv = [Environment().from_string(arg).render(**variables)
                for arg in task["ansible.builtin.command"]["argv"]]
        for failed in (*workloads, "none"):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / "calls"
                wrapper = """
kubectl() {
  [[ "$1" == '--kubeconfig=/tmp/cluster with spaces;false/kubectl.kubeconfig' &&
     "$2" == -n && "$3" == ceph-csi && "$4" == rollout && "$5" == status &&
     "$7" == --timeout=10m ]] || return 97
  printf '%s\n' "$6" >>"$CEPH_WAIT_TEST_LOG"
  [[ "$6" != "$CEPH_WAIT_TEST_FAILED" ]] || return 23
}
export -f kubectl
exec "$@"
"""
                result = subprocess.run(
                    ["bash", "-c", wrapper, "--", *argv], text=True, capture_output=True,
                    env={**os.environ, "CEPH_WAIT_TEST_LOG": str(log), "CEPH_WAIT_TEST_FAILED": failed},
                )
                self.assertEqual(result.returncode, 0 if failed == "none" else 23, result.stderr)
                count = len(workloads) if failed == "none" else workloads.index(failed) + 1
                self.assertEqual(log.read_text().splitlines(), workloads[:count])

    def test_csi_stdin_apply_arguments_are_scalar_dashes(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/csi.yml").read_text())
        commands = [task["ansible.builtin.command"] for task in tasks
                    if isinstance(task.get("ansible.builtin.command"), dict)
                    and "stdin" in task["ansible.builtin.command"]
                    and "apply" in task["ansible.builtin.command"]["argv"]]
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertEqual(command["argv"][-2:], ["-f", "-"])

    def test_csi_helm_loop_renders_values_filename_not_dict_method(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/csi.yml").read_text())
        task = next(task for task in tasks if task.get("register") == "ceph_csi_helm")
        variables = {
            "base_dir": "/usr/local/kubeauto",
            "role_path": "/usr/local/kubeauto/roles/ceph",
            "cluster_dir": "/usr/local/kubeauto/clusters/fixture",
            "ceph_csi_namespace": "ceph-csi",
        }
        environment = Environment(autoescape=False)
        for item in task["loop"]:
            with self.subTest(release=item["release"]):
                argv = [environment.from_string(argument).render(**variables, item=item)
                        for argument in task["ansible.builtin.command"]["argv"]]
                self.assertEqual(argv[argv.index("--values") + 1],
                                 f"{variables['cluster_dir']}/{item['values']}")

    def test_csi_preflight_rejects_dict_method_path_before_invoking_helm(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/csi.yml").read_text())
        task = next(task for task in tasks if task.get("register") == "ceph_csi_helm")
        argv = task["ansible.builtin.command"]["argv"]
        argv[argv.index("--values") + 1] = "{{ cluster_dir }}/{{ item.values }}"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "conf").mkdir()
            (base / "conf/config.yml").write_text(self.config)
            (base / "roles/ceph/tasks").mkdir(parents=True)
            (base / "roles/ceph/tasks/csi.yml").write_text(yaml.safe_dump([task]))
            with patch.object(CEPH_CONTRACT.subprocess, "run", wraps=subprocess.run) as commands:
                with self.assertRaisesRegex(ValueError, "Helm values path"):
                    CEPH_CONTRACT.render_csi_preflight(base, "helm")
                self.assertNotIn("helm", [call.args[0][0] for call in commands.call_args_list])

    def test_task_preflight_rejects_non_string_command_arguments(self) -> None:
        for argument in ([None], None, {}, False, 42, "-"):
            with self.subTest(argument=argument), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                task_dir = base / "roles/ceph/tasks"
                task_dir.mkdir(parents=True)
                (task_dir / "csi.yml").write_text(yaml.safe_dump([{
                    "name": "Apply stdin fixture",
                    "ansible.builtin.command": {"argv": ["kubectl", "-f", argument]},
                }]))
                if isinstance(argument, str):
                    self.assertGreater(CEPH_CONTRACT.parse_task_templates(base), 0)
                else:
                    with self.assertRaisesRegex(ValueError, "command argv.*strings"):
                        CEPH_CONTRACT.parse_task_templates(base)

    def test_ceph_artifact_does_not_change_the_delivered_core_downloader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DownloadManager.__new__(DownloadManager)
            manager.docker = SimpleNamespace(is_docker_installed=True)
            manager.kube_constant = self.constants
            manager.extra_bin_dir = Path(directory)
            manager.image_dir = Path(directory) / "images"
            manager._DownloadManager__handle_image = Mock()
            manager._DownloadManager__handle_files = Mock()
            (manager.extra_bin_dir / "etcdctl").touch()
            manager.get_ext_bin()
            manager._DownloadManager__handle_image.assert_not_called()
            manager._DownloadManager__handle_files.assert_not_called()
            (manager.extra_bin_dir / "etcdctl").unlink()
            manager.get_ext_bin()
            image = f"brinnatt/kubeauto-ext-bin:{self.constants.v_extra_bin}"
            manager._DownloadManager__handle_image.assert_called_once_with(
                manager.image_dir, f"ext_bin_{self.constants.v_extra_bin}.tar", image,
            )
            manager._DownloadManager__handle_files.assert_called_once_with(
                image, "/extra", manager.extra_bin_dir, create_symlink=False,
            )
        source = (ROOT / "service/cluster/downloader.py").read_text()
        self.assertNotIn("_ensure_cephadm", source)
        self.assertNotIn("require_cephadm", source)
        self.assertNotIn("download.ceph.com", source)
        self.assertNotIn("urllib.request", source)

    def test_ext_bin_cephadm_is_atomic_checksum_gated(self) -> None:
        self.assertEqual(self.constants.v_extra_bin, "1.16.0")
        downloader = (PROJECTS / "kubeauto-ext-bin-dockerfile/multi-platform-download.sh").read_text()
        block = downloader.split("# The official cephadm zip application is architecture-independent.\n", 1)[1]
        self.assertIn('https://download.ceph.com/rpm-${CEPHADM_VER}/el9/noarch/cephadm', block)
        payload = b"official cephadm fixture"
        expected = hashlib.sha256(payload).hexdigest()
        for fault in ("none", "corrupt", "network"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                artifact = base / "cephadm"
                artifact.write_bytes(b"existing artifact")
                fixture = base / "payload"
                fixture.write_bytes(payload if fault == "none" else b"corrupt")
                wget = base / "wget"
                wget.write_text(
                    '#!/bin/sh\n'
                    'test "$1" = -O || exit 97\n'
                    'test "$3" = https://download.ceph.com/rpm-20.2.4/el9/noarch/cephadm || exit 98\n'
                    'test "$CEPHADM_TEST_FAULT" != network || exit 8\n'
                    'cp "$CEPHADM_TEST_PAYLOAD" "$2"\n'
                )
                wget.chmod(0o755)
                result = subprocess.run(
                    ["sh", "-c", block.replace("/ext-bin", directory)],
                    text=True, capture_output=True,
                    env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                         "CEPHADM_VER": "20.2.4", "CEPHADM_SHA256": expected,
                         "CEPHADM_TEST_FAULT": fault, "CEPHADM_TEST_PAYLOAD": str(fixture)},
                )
                if fault == "none":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(artifact.read_bytes(), payload)
                    self.assertEqual(artifact.stat().st_mode & 0o777, 0o755)
                    self.assertEqual((base / "cephadm.sha256").read_text(), f"{expected}  cephadm\n")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(artifact.read_bytes(), b"existing artifact")
                    self.assertFalse((base / "cephadm.sha256").exists())
                self.assertEqual(list(base.glob(".cephadm.*")), [])

    def test_ceph_image_download_preserves_the_binary_pack_boundary(self) -> None:
        manager = DownloadManager.__new__(DownloadManager)
        manager.docker = SimpleNamespace(is_docker_installed=True)
        manager.kube_constant = self.constants
        manager.registry = Mock()
        manager.get_ext_bin = Mock()
        manager.get_extra_images("ceph")
        manager.get_ext_bin.assert_not_called()
        manager.registry.upload_to_registry.assert_called_once_with(
            self.constants.component_images["ceph"], fail_fast=True
        )
        manager.get_ext_bin.reset_mock()
        manager.registry.reset_mock()
        manager.get_extra_images("mysql")
        manager.get_ext_bin.assert_not_called()
        self.assertFalse(manager.registry.upload_to_registry.call_args.kwargs["fail_fast"])

    def test_every_fsid_match_accepts_uuid_and_bootstrap_creates_parent_first(self) -> None:
        patterns = re.findall(r"is match\('(\^\[0-9a-f\].*?\$)'\)", self.role)
        self.assertGreaterEqual(len(patterns), 3)
        for pattern in patterns:
            self.assertIsNotNone(re.fullmatch(pattern, "11111111-2222-3333-4444-555555555555"))
            self.assertIsNone(re.fullmatch(pattern, "11111111-2222-3333-555555555555"))
        cluster = (ROOT / "roles/ceph/tasks/cluster.yml").read_text()
        self.assertLess(cluster.index("Create Ceph configuration directory"), cluster.index("Record the exact Ceph FSID"))

    def test_feature_defaults_disabled_and_has_no_plaintext_secret(self) -> None:
        data = yaml.safe_load(self.config)
        self.assertEqual(data["ceph_install"], "no")
        self.assertEqual(data["ceph_csi_install"], "no")
        self.assertNotRegex(self.config, r"(?im)^ceph_.*(?:password|secret|key):\s*[^\s\"']+")

    def test_inventory_has_explicit_ceph_failure_domains(self) -> None:
        for group in ("ceph", "ceph_bootstrap", "ceph_mon", "ceph_mgr", "ceph_osd"):
            self.assertIn(f"[{group}]", self.inventory)
        self.assertIn("ceph_devices=", self.inventory)
        self.assertIn("/dev/disk/by-id/", self.inventory)

    def test_role_rejects_implicit_or_unstable_osd_selection(self) -> None:
        self.assertIn("/dev/disk/by-id/", self.role)
        self.assertIn("/dev/mapper/kubeauto-ceph-", self.role)
        self.assertIn("ceph_allow_test_mappers", self.role)
        self.assertNotIn("ceph_allow_test_paths", self.role)
        self.assertNotRegex(self.role, r"192\.168\.122\.(135|40|72|212|165|238)")
        self.assertNotIn("all-available-devices", self.role)
        self.assertIn("serial", self.role.lower())
        self.assertRegex(self.role, r"wwn|wwid")

    def test_production_disk_gate_rejects_root_partitions_and_probe_errors(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/prepare.yml").read_text())
        names = [task["name"] for task in tasks]
        self.assertLess(names.index("Install official cephadm host prerequisites"), names.index("Verify every declared disk identity and unused state"))
        body = next(task["ansible.builtin.command"]["argv"][2] for task in tasks if task["name"] == "Verify every declared disk identity and unused state")
        body = Environment(autoescape=False).from_string(body).render()
        for fault in (
            "none", "root", "partition", "mounted", "filesystem", "lsblk-error", "pvs-error",
            "owned", "owned-empty", "owned-multiple", "owned-foreign",
        ):
            with self.subTest(fault=fault):
                script = f"""
test() {{
  if [[ "$1" == -b ]]; then return 0; fi
  if [[ "$1" == -x ]]; then [[ '{fault}' == owned* ]]; return; fi
  builtin test "$@"
}}
readlink() {{ echo /dev/fixture; }}
findmnt() {{ echo /dev/root-fixture; }}
lsblk() {{
  case "$*" in
    *FSTYPE*)
      [[ '{fault}' != filesystem ]] || echo xfs
      [[ '{fault}' != owned* ]] || echo LVM2_member ;;
    *TYPE*) echo disk ;;
    *MOUNTPOINTS*) return 5 ;;
    *MOUNTPOINT*) [[ '{fault}' != mounted ]] || echo /data ;;
    *SERIAL*) echo fixture-serial ;;
    *WWN*) : ;;
    *PATH*) [[ '{fault}' == root ]] && echo /dev/fixture || echo /dev/other ;;
    *-J*)
      [[ '{fault}' != lsblk-error ]] || return 5
      if [[ '{fault}' == partition ]]; then echo '{{"blockdevices":[{{"name":"fixture","children":[{{"name":"fixture1"}}]}}]}}'; else echo '{{"blockdevices":[{{"name":"fixture"}}]}}'; fi ;;
  esac
  return 0
}}
pvs() {{
  [[ '{fault}' != pvs-error ]] || return 5
  echo '{{"report":[{{"pv":[]}}]}}'
}}
find() {{
  [[ '{fault}' != owned-empty ]] || return 0
  echo 12345678-1234-1234-1234-123456789abc
  [[ '{fault}' != owned-multiple ]] || echo 12345678-1234-1234-1234-123456789abd
  return 0
}}
function /usr/local/sbin/cephadm() {{
  local fsid=12345678-1234-1234-1234-123456789abc
  [[ '{fault}' != owned-foreign ]] || fsid=12345678-1234-1234-1234-123456789abd
  printf '{{"0":[{{"tags":{{"ceph.cluster_fsid":"%s"}}}}]}}' "$fsid"
}}
set -- /dev/disk/by-id/fixture fixture-serial 12345678-1234-1234-1234-123456789abc
{body}
"""
                result = subprocess.run(["bash", "-ceu", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault in {"none", "owned"}, result.stdout + result.stderr)

    def test_ceph_native_prerequisites_cover_requested_distribution_versions(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/host-prerequisites.yml").read_text())
        profile = next(task["ansible.builtin.set_fact"] for task in tasks
                       if "ansible.builtin.set_fact" in task)
        environment = Environment()
        for family, distribution, major in (
            ("RedHat", "Rocky", "8"), ("RedHat", "Rocky", "9"), ("RedHat", "Rocky", "10"),
            ("Debian", "Ubuntu", "20"), ("Debian", "Ubuntu", "22"),
            ("Debian", "Ubuntu", "24"), ("Debian", "Ubuntu", "26"),
            ("Debian", "Debian", "13"), ("Suse", "openSUSE Leap", "16"),
            ("RedHat", "Anolis", "23"), ("RedHat", "openEuler", "22"),
        ):
            for requested in ("auto", "podman", "docker"):
                for existing in ("", "podman", "docker"):
                    with self.subTest(distribution=distribution, major=major,
                                      requested=requested, existing=existing):
                        variables = dict(
                            ansible_os_family=family, ansible_distribution=distribution,
                            ansible_distribution_major_version=major,
                            ceph_container_runtime=requested,
                            ceph_existing_runtime={"stdout": existing},
                        )
                        packages = ast.literal_eval(environment.from_string(
                            profile["ceph_host_packages"]).render(**variables))
                        runtime = environment.from_string(profile["ceph_host_runtime"]).render(
                            **variables).strip()
                        expected = requested if requested != "auto" else (
                            existing or ("docker" if distribution == "Ubuntu" and major == "20" else "podman")
                        )
                        self.assertEqual(runtime, expected)
                        self.assertIn("dmsetup" if family == "Debian" else "device-mapper", packages)
                        self.assertEqual("python39" in packages, family == "RedHat" and major == "8")
                        self.assertNotIn("podman", packages)
        rpm = next(task for task in tasks if task["name"].endswith("native RPM CLI"))
        self.assertIn("ansible.builtin.command", rpm)
        self.assertNotIn("ansible.builtin.package", rpm)
        self.assertIn("name: docker", (ROOT / "roles/ceph/tasks/host-prerequisites.yml").read_text())

    def test_ceph_launcher_does_not_replace_system_python_or_official_artifact(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cephadm.yml").read_text())
        launcher = next(task["ansible.builtin.copy"] for task in tasks
                        if task["name"] == "Install the interpreter-specific cephadm launcher")
        environment = Environment()
        import shlex
        environment.filters["quote"] = shlex.quote
        for runtime in ("podman", "docker"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / "arguments"
                executable = Path(directory) / "python fixture"
                executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$CEPH_LAUNCHER_LOG"\n')
                executable.chmod(0o755)
                content = environment.from_string(launcher["content"]).render(
                    cephadm_python_probe={"stdout": str(executable)}, ceph_host_runtime=runtime)
                result = subprocess.run(["sh", "-c", content, "--", "shell", "--", "a b", "$(false)"],
                                        env={**os.environ, "CEPH_LAUNCHER_LOG": str(log)},
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ["/usr/local/libexec/kubeauto/cephadm"]
                if runtime == "docker":
                    expected.append("--docker")
                self.assertEqual(log.read_text().splitlines(), expected + ["shell", "--", "a b", "$(false)"])
        self.assertNotIn("dest: /usr/bin/python", self.role)
        self.assertIn("mgr/cephadm/mode = cephadm-package",
                      (ROOT / "roles/ceph/templates/bootstrap.conf.j2").read_text())
        exposure = next(task["ansible.builtin.file"] for task in tasks
                        if task["name"] == "Expose the owned cephadm launcher to the upstream orchestrator")
        self.assertEqual(exposure["dest"], "/usr/bin/cephadm")
        self.assertEqual(exposure["src"], "/usr/local/sbin/cephadm")

    def test_cephadm_probe_executes_candidates_and_rejects_an_unusable_artifact(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cephadm.yml").read_text())
        probe = next(task["ansible.builtin.command"]["argv"][2] for task in tasks
                     if task.get("register") == "cephadm_python_probe")
        for failure in ("none", "version", "artifact", "missing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                preferred = Path(directory) / "preferred python"
                fallback = Path(directory) / "fallback-python"
                # Replace only candidate paths; execute the actual production loop.
                body = probe.replace("/usr/bin/python3", str(fallback))
                for path, usable in ((preferred, failure == "none"), (fallback, failure != "missing")):
                    path.write_text(
                        '#!/bin/sh\n'
                        + (f'test "$1" != -c || exit {0 if failure != "version" or path == fallback else 1}\n')
                        + f'test "$1" = -c || exit {0 if usable else 1}\n'
                    )
                    path.chmod(0o755)
                if failure == "missing":
                    preferred.unlink()
                    fallback.unlink()
                result = subprocess.run(["bash", "-ceu", body, "--", str(preferred)],
                                        text=True, capture_output=True)
                if failure == "missing":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("No installed Python", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(preferred if failure == "none" else fallback))

    def test_cephadm_refuses_foreign_entrypoints_and_preserves_them(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cephadm.yml").read_text())
        guard = next(task["ansible.builtin.command"]["argv"][2] for task in tasks
                     if task["name"] == "Refuse a distribution-owned or foreign cephadm entry point")
        for fixture in ("absent", "owned", "package", "foreign-link", "foreign-local", "local-link", "legacy"):
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as directory:
                system = Path(directory) / "system-cephadm"
                local = Path(directory) / "local-cephadm"
                if fixture in {"owned", "foreign-local"}:
                    local.write_text(
                        '#!/bin/sh\n# kubeauto-owned launcher; the official artifact remains checksum-identical.\n'
                        if fixture == "owned" else "foreign executable\n")
                    system.symlink_to(local)
                elif fixture == "package":
                    system.write_text("distribution executable\n")
                elif fixture == "foreign-link":
                    system.symlink_to(Path(directory) / "other")
                elif fixture == "local-link":
                    local.symlink_to(Path(directory) / "other")
                elif fixture == "legacy":
                    local.write_text("legacy official fixture\n")
                body = guard.replace("/usr/bin/cephadm", str(system)).replace("/usr/local/sbin/cephadm", str(local))
                if fixture == "legacy":
                    body = body.replace(self.constants.v_cephadm_sha256, hashlib.sha256(local.read_bytes()).hexdigest())
                before = {path: path.read_bytes() for path in (system, local) if path.is_file()}
                result = subprocess.run(["bash", "-ceu", body], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fixture in {"absent", "owned", "legacy"}, result.stderr)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
        prepare = yaml.safe_load((ROOT / "roles/ceph/tasks/prepare.yml").read_text())
        install = next(i for i, task in enumerate(prepare) if task.get("ansible.builtin.import_tasks") == "cephadm.yml")
        disks = next(i for i, task in enumerate(prepare) if task["name"] == "Verify every declared disk identity and unused state")
        self.assertLess(install, disks)

    def test_deb_indexes_and_existing_docker_have_explicit_prerequisite_contracts(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/host-prerequisites.yml").read_text())
        update = next(i for i, task in enumerate(tasks)
                      if task.get("ansible.builtin.command", {}).get("argv") == ["apt-get", "update"])
        install = next(i for i, task in enumerate(tasks) if task["name"].endswith("native DEB CLI"))
        self.assertLess(update, install)
        docker = next(task for task in tasks if "ansible.builtin.include_role" in task)
        self.assertIn("ceph_docker_executable.rc == 1", docker["when"])

    def test_kernel_secure_client_checks_vendor_backports_without_security_downgrade(self) -> None:
        import gzip
        import lzma
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".ko", ".ko.xz", ".ko.gz"):
                for msgr2, aes256k in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(suffix=suffix, msgr2=msgr2, aes256k=aes256k):
                        path = Path(directory) / ("libceph" + suffix)
                        contents = b"kernel fixture\x00" + (b"ms_mode=secure,\x00" if msgr2 else b"legacy\x00")
                        if aes256k:
                            contents += b"crypto_krb5_find_enctype\x00crypto_krb5_prepare_encryption\x00"
                        path.write_bytes(lzma.compress(contents) if suffix == ".ko.xz" else
                                         gzip.compress(contents) if suffix == ".ko.gz" else contents)
                        self.assertEqual(CEPH_KERNEL.kernel_client_features(str(path)),
                                         {"secure_msgr2": msgr2, "aes256k": aes256k})
            path = Path(directory) / "libceph.ko.zst"
            with patch.object(CEPH_KERNEL.subprocess, "check_output", return_value=contents) as decompress:
                self.assertEqual(CEPH_KERNEL.kernel_client_features(str(path)),
                                 {"secure_msgr2": True, "aes256k": True})
                decompress.assert_called_once_with(["zstd", "--decompress", "--stdout", str(path)])
            for contents in (b"krb5 aes256k", b"crypto_krb5_find_enctype\x00", b"crypto_krb5_prepare_encryption\x00"):
                path = Path(directory) / "libceph.ko"
                path.write_bytes(contents)
                self.assertFalse(CEPH_KERNEL.kernel_client_features(str(path))["aes256k"])
        with patch.object(CEPH_KERNEL.platform, "release", return_value="5.15.0-ubuntu"), \
             patch.object(CEPH_KERNEL.subprocess, "check_call", side_effect=subprocess.CalledProcessError(1, "modprobe")):
            with self.assertRaises(subprocess.CalledProcessError):
                CEPH_KERNEL.main()
        self.assertIn("kernelMountOptions: ms_mode=secure", (ROOT / "roles/ceph/templates/csi-cephfs-values.yml.j2").read_text())

    def test_kernel_client_rejects_msgr2_without_aes256k_on_any_release(self) -> None:
        for release in ("4.18.0-553.el8_10", "5.15.0-ubuntu", "6.12.0-rocky10", "7.0.0"):
            with self.subTest(release=release), \
                 patch.object(CEPH_KERNEL.platform, "release", return_value=release), \
                 patch.object(CEPH_KERNEL.subprocess, "check_call"), \
                 patch.object(CEPH_KERNEL.subprocess, "check_output", return_value="/fixture/libceph.ko"), \
                 patch.object(CEPH_KERNEL, "kernel_client_features", return_value={"secure_msgr2": True, "aes256k": False}):
                with self.assertRaisesRegex(ValueError, "lacks CephX aes256k"):
                    CEPH_KERNEL.main()

    def test_legacy_aes_kernel_client_requires_secure_msgr2_but_not_aes256k(self) -> None:
        import io
        from contextlib import redirect_stdout
        for msgr2 in (False, True):
            with self.subTest(msgr2=msgr2), \
                 patch.object(CEPH_KERNEL.platform, "release", return_value="4.18.0-rocky8"), \
                 patch.object(CEPH_KERNEL.subprocess, "check_call"), \
                 patch.object(CEPH_KERNEL.subprocess, "check_output", return_value="/fixture/libceph.ko"), \
                 patch.object(CEPH_KERNEL, "kernel_client_features", return_value={"secure_msgr2": msgr2, "aes256k": False}):
                if not msgr2:
                    with self.assertRaisesRegex(ValueError, "lacks secure msgr2"):
                        CEPH_KERNEL.main("aes")
                else:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        CEPH_KERNEL.main("aes")
                    self.assertEqual(json.loads(output.getvalue())["aes256k"], False)

    def test_legacy_aes_is_explicit_and_scoped_to_csi_identities(self) -> None:
        config = yaml.safe_load((ROOT / "conf/config.yml").read_text())
        self.assertEqual(config["ceph_csi_client_key_type"], "aes256k")
        self.assertEqual(config["ceph_csi_legacy_aes_risk_accepted"], "no")
        cluster = (ROOT / "roles/ceph/tasks/cluster.yml").read_text()
        self.assertIn("'--key-type=' ~ (ceph_csi_client_key_type | default('aes256k'))", cluster)
        self.assertIn("mon_auth_allow_insecure_key", cluster)
        self.assertIn("auth_service_cipher", cluster)
        self.assertIn("auth_preferred_cipher", cluster)
        preflight = (ROOT / "roles/ceph/tasks/client-preflight.yml").read_text()
        self.assertIn("ceph_csi_legacy_aes_risk_accepted", preflight)
        self.assertIn("ceph_csi_client_key_type", preflight)
        self.assertIn("ceph_legacy_source_release", cluster)
        self.assertIn("ceph_image.endswith(':v20.2.3')", cluster)
        self.assertIn("ternary([], ['--key-type='", cluster)

    def test_cephfs_csi_subvolume_group_is_created_before_business_workload(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        names = [task["name"] for task in tasks]
        volume = names.index("Create the CephFS volume")
        group = names.index("Create the Ceph-CSI subvolume group")
        identity = names.index("Create only the selected Ceph-CSI client key type")
        self.assertLess(volume, group)
        self.assertLess(group, identity)
        self.assertEqual(tasks[group]["ansible.builtin.command"]["argv"][-2:],
                         ["{{ ceph_csi_cephfs_name }}", "csi"])
        self.assertIn("subvolumeGroup: csi", (ROOT / "roles/ceph/templates/csi-cephfs-values.yml.j2").read_text())
        self.assertIn("CEPH-09 product \"Ceph-CSI subvolume group csi is missing\"",
                      (ROOT / "tests/helpers/ceph-regression.sh").read_text())

    def test_csi_key_type_guard_rejects_missing_or_mismatched_existing_key(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        query = next(task for task in tasks if task["name"] == "Read CephX key types without exposing credentials")
        guard = next(task for task in tasks if task["name"] == "Refuse to reuse Ceph-CSI credentials of a different key type")
        self.assertTrue(query["no_log"])
        expression = guard["ansible.builtin.assert"]["that"][0]
        environment = Environment()
        environment.filters["from_json"] = json.loads
        template = environment.from_string("{{ " + expression + " }}")
        for key_type, expected in (("aes", "True"), ("aes256k", "False"), (None, "False")):
            secrets = [] if key_type is None else [{"entity": {"type_str": "client", "id": "csi-rbd"},
                                                      "auth": {"key": {"type_str": key_type}}}]
            result = template.render(ceph_csi_auth_dump={"stdout": json.dumps({"data": {"secrets": secrets}})},
                                     item="csi-rbd", ceph_csi_client_key_type="aes")
            self.assertEqual(result, expected)

    def test_rgw_put_uses_fixed_sigv4_probe_and_independent_mc_readback(self) -> None:
        runner = (ROOT / "tests/helpers/ceph-regression.sh").read_text()
        self.assertIn('python3 "$BASE/tests/helpers/ceph_s3_put.py"', runner)
        self.assertIn('s3_sigv4_put marker', runner)
        self.assertIn('s3_sigv4_put delete-check', runner)
        self.assertIn('mc cp ceph/kubeauto-ceph-test/marker /tmp/readback', runner)
        self.assertNotIn("mc cp /tmp/marker ceph/kubeauto-ceph-test/", runner)
        self.assertNotIn("alpine-curl", runner)
        self.assertNotIn("brinnatt/alpine-curl:v7.85.0", self.constants.component_images["ceph"])
        self.assertIn('"$ROOT/tests/helpers/ceph_s3_put.py"', self.runner)

    def test_s3_sigv4_put_invokes_scoped_client_without_secret_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / "python3"
            calls = Path(directory) / "client-calls"
            client.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >"$MOCK_S3_CALLS"\nprintf RGW_SIGV4_PUT_PASS\n')
            client.chmod(0o755)
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
BASE={directory}
RGW_ACCESS=fixture-access
RGW_SECRET=fixture-secret
RGW_ENDPOINT=127.0.0.1:8080
s3_sigv4_put marker
"""
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=5,
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                     "MOCK_S3_CALLS": str(calls)},
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = calls.read_text().splitlines()
            self.assertEqual(args, [f"{directory}/tests/helpers/ceph_s3_put.py", "--endpoint",
                                    "http://127.0.0.1:8080", "--bucket", "kubeauto-ceph-test",
                                    "--key", "marker"])
            self.assertNotIn("fixture-secret", " ".join(args) + result.stdout + result.stderr)

    def test_s3_signer_canonical_request_and_safe_error_reporting(self) -> None:
        request = S3_CLIENT.signed_request(
            "http://127.0.0.1:8080", "kubeauto-ceph-test", "marker",
            "fixture-access", "fixture-secret", datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.data, b"kubeauto-ceph-s3-v1")
        self.assertEqual(headers["host"], "127.0.0.1:8080")
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(request.data).hexdigest())
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertIn("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date",
                      headers["authorization"])
        self.assertNotIn("fixture-secret", str(request.header_items()))
        self.assertEqual(S3_CLIENT.error_code(
            b"<Error><Code>SignatureDoesNotMatch</Code><Secret>fixture-secret</Secret></Error>"),
            "SignatureDoesNotMatch")
        response = io.StringIO()
        with patch.dict(os.environ, {"RGW_ACCESS": "fixture-access", "RGW_SECRET": "fixture-secret"}), \
             patch.object(sys, "argv", ["ceph_s3_put.py", "--endpoint", "http://127.0.0.1:8080",
                                        "--bucket", "kubeauto-ceph-test", "--key", "marker"]), \
             patch.object(S3_CLIENT, "build_opener") as opener, redirect_stderr(response):
            opener.return_value.open.side_effect = HTTPError(
                "http://127.0.0.1:8080", 403, "Forbidden", {},
                io.BytesIO(b"<Error><Code>SignatureDoesNotMatch</Code><Secret>fixture-secret</Secret></Error>"),
            )
            self.assertEqual(S3_CLIENT.main(), 1)
        self.assertIn("RGW_S3_PUT_FAIL http=403 code=SignatureDoesNotMatch", response.getvalue())
        self.assertNotIn("fixture-secret", response.getvalue())

    def test_mixed_workload_uses_the_verified_s3_writer(self) -> None:
        self.assertIn('s3_sigv4_put "latency-${suffix}" >/dev/null', self.regression)
        self.assertNotIn('mc cp /tmp/latency ceph/', self.regression)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "s3-object"
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
ceph_shell() {{ cat >/dev/null; }}
K=(kubectl_mock)
kubectl_mock() {{ return 0; }}
s3_sigv4_put() {{ printf '%s' "$1" >'{target}'; }}
business_once healthy-1
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(target.read_text(), "latency-healthy-1")

    def test_kernel_client_success_reports_both_features(self) -> None:
        import io
        from contextlib import redirect_stdout
        for msgr2 in (False, True):
            with self.subTest(msgr2=msgr2), \
                 patch.object(CEPH_KERNEL.platform, "release", return_value="4.18.0-vendor-backport"), \
                 patch.object(CEPH_KERNEL.subprocess, "check_call"), \
                 patch.object(CEPH_KERNEL.subprocess, "check_output", return_value="/fixture/libceph.ko"), \
                 patch.object(CEPH_KERNEL, "kernel_client_features", return_value={"secure_msgr2": msgr2, "aes256k": True}):
                if not msgr2:
                    with self.assertRaisesRegex(ValueError, "lacks secure msgr2"):
                        CEPH_KERNEL.main()
                    continue
                output = io.StringIO()
                with redirect_stdout(output):
                    CEPH_KERNEL.main()
                self.assertEqual(json.loads(output.getvalue()),
                                 {"kernel": "4.18.0-vendor-backport", "secure_msgr2": True, "aes256k": True})

    def test_kernel_client_preflight_precedes_storage_mutation(self) -> None:
        prepare = yaml.safe_load((ROOT / "roles/ceph/tasks/prepare.yml").read_text())
        preflight = next(task for task in prepare if task.get("ansible.builtin.import_tasks") == "client-preflight.yml")
        prerequisites = next(task for task in prepare if task.get("ansible.builtin.import_tasks") == "host-prerequisites.yml")
        self.assertLess(prepare.index(preflight), prepare.index(prerequisites))
        self.assertIn("ceph_csi_install", preflight["when"])
        self.assertTrue(preflight["run_once"])
        plays = yaml.safe_load((ROOT / "playbooks/08.ceph.yml").read_text())
        self.assertTrue(plays[0]["any_errors_fatal"])
        focused = self.regression.split("prepare_focused_environment() {", 1)[1].split("main() {", 1)[0]
        self.assertLess(focused.index("verify_compute_kernel_clients"), focused.index("prepare_fault_mappers"))

    def test_kernel_client_cli_rejects_legacy_module_and_accepts_backport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            module = base / "libceph.ko"
            for name, body in (("modprobe", "exit 0"), ("modinfo", f"printf '%s\\n' '{module}'")):
                path = base / name
                path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            for aes256k in (False, True):
                with self.subTest(aes256k=aes256k):
                    contents = b"ms_mode=secure,\x00"
                    if aes256k:
                        contents += b"crypto_krb5_find_enctype\x00crypto_krb5_prepare_encryption\x00"
                    module.write_bytes(contents)
                    result = subprocess.run([sys.executable, str(KERNEL_PATH)], text=True, capture_output=True,
                                            env={**os.environ, "PATH": str(base) + os.pathsep + os.environ["PATH"]})
                    self.assertEqual(result.returncode, 0 if aes256k else 1, result.stderr)
                    if aes256k:
                        payload = json.loads(result.stdout)
                        self.assertTrue(payload["secure_msgr2"])
                        self.assertTrue(payload["aes256k"])
                    else:
                        self.assertIn("CEPH_KERNEL_CLIENT_REJECT", result.stderr)
                        self.assertIn("lacks CephX aes256k", result.stderr)
                        self.assertEqual(result.stdout, "")

    def test_focused_kernel_preflight_failure_stops_before_artifacts_and_storage(self) -> None:
        script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
BASE='{ROOT}'
KUBE_HOSTS=(unit-fixture)
FSID_FILE=/nonexistent/unit-fixture-fsid
ssh_node() {{ return 1; }}
verify_supply_chain() {{ echo FORBIDDEN_SUPPLY_CHAIN; }}
prepare_fault_mappers() {{ echo FORBIDDEN_STORAGE_MUTATION; }}
prepare_focused_environment
"""
        result = subprocess.run(["bash", "-ceu", script], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("class=environment", result.stderr)
        self.assertNotIn("FORBIDDEN_", result.stdout)
        self.assertNotIn("CEPH_COMPUTE_KERNEL_CLIENT_PASS", result.stdout)

    def test_task_template_preflight_rejects_shell_jinja_collisions(self) -> None:
        self.assertGreater(CEPH_CONTRACT.parse_task_templates(ROOT), 100)
        for body, valid in (("echo ${#fsids[@]}", False), ("echo ${fsids[*]}", True)):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                tasks = base / "roles/ceph/tasks"
                tasks.mkdir(parents=True)
                (tasks / "prepare.yml").write_text(yaml.safe_dump([
                    {"name": "fixture", "ansible.builtin.command": {"argv": ["bash", "-ceu", body]}}
                ]))
                if valid:
                    self.assertGreater(CEPH_CONTRACT.parse_task_templates(base), 0)
                else:
                    with self.assertRaisesRegex(ValueError, "Missing end of comment tag"):
                        CEPH_CONTRACT.parse_task_templates(base)
        preflight = self.runner.split("verify_ceph_ansible_syntax() {", 1)[1].split("\nceph_gate_fingerprint()", 1)[0]
        self.assertIn("ceph_contract.py task-templates", preflight)

    def test_task_preflight_rejects_unquoted_colons_in_flow_scalars(self) -> None:
        prefix = "- name: fixture\n  ansible.builtin.command:\n"
        for declaration, valid in (
            ("    argv: [cephadm, --mount, /a:/a:ro]\n", False),
            ('    argv: [cephadm, --mount, "/a:/a:ro"]\n', True),
            ('    argv:\n      - cephadm\n      - --mount\n      - "/a:/a:ro"\n', True),
        ):
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                tasks = base / "roles/ceph/tasks"
                tasks.mkdir(parents=True)
                (tasks / "cluster.yml").write_text(prefix + declaration)
                if valid:
                    self.assertGreater(CEPH_CONTRACT.parse_task_templates(base), 0)
                else:
                    with self.assertRaisesRegex(ValueError, "unquoted colon.*flow scalar"):
                        CEPH_CONTRACT.parse_task_templates(base)
        preflight = self.runner.split("verify_ceph_ansible_syntax() {", 1)[1].split("\nceph_gate_fingerprint()", 1)[0]
        self.assertIn("from ansible.parsing.dataloader import DataLoader", preflight)
        self.assertIn("CEPH_NATIVE_TASK_YAML_PASS", preflight)

    def test_service_spec_files_are_explicitly_mounted_into_cephadm_shell(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        checked = 0
        for task in tasks:
            argv = task.get("ansible.builtin.command", {}).get("argv", [])
            if isinstance(argv, list) and "shell" in argv and "-i" in argv:
                path = argv[argv.index("-i") + 1]
                if path.startswith("/"):
                    self.assertIn("--mount", argv)
                    self.assertEqual(argv[argv.index("--mount") + 1], f"{path}:{path}:ro")
                    self.assertLess(argv.index("--mount"), argv.index("--"))
                    checked += 1
        self.assertEqual(checked, 2)

    def test_cephadm_bootstrap_and_service_spec_are_pinned(self) -> None:
        self.assertIn("'cephadm version ' + ceph_ver", self.role)
        self.assertIn("--skip-monitoring-stack", self.role)
        self.assertIn("--ssh-private-key", self.role)
        self.assertIn("--image", self.role)
        self.assertIn("--skip-dashboard", self.role)
        self.assertIn("ceph_dashboard_install", self.role)
        spec = (ROOT / "roles/ceph/templates/cluster-spec.yml.j2").read_text()
        self.assertIn("service_type: mon", spec)
        self.assertIn("service_type: mgr", spec)
        self.assertIn("service_type: osd", spec)
        self.assertIn("data_devices:", spec)
        self.assertIn("paths:", spec)
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        probe = next(task for task in tasks if task["name"] == "Inspect the exact requested Ceph cluster")
        argv = probe["ansible.builtin.command"]["argv"]
        self.assertEqual(argv[1:4], ["--image", "{{ ceph_image }}", "shell"])

    def test_bootstrap_avoids_upstream_interactive_key_redistribution(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        bootstrap = next(task for task in tasks if task["name"] == "Bootstrap the pinned cephadm cluster")
        environment = Environment(autoescape=False)
        environment.filters["ternary"] = lambda condition, yes, no: yes if condition else no
        argv = ast.literal_eval(environment.from_string(
            bootstrap["ansible.builtin.command"]["argv"]
        ).render(
            ceph_image="hub.talkedu.cn/kubeauto/ceph:v20.2.4",
            ceph_requested_fsid={"stdout": "12345678-1234-1234-1234-123456789abc"},
            ansible_host="10.20.0.11", inventory_hostname="ceph-01",
            ceph_cluster_network="", ceph_dashboard_install="yes",
        ))
        self.assertNotIn("--apply-spec", argv)
        self.assertEqual(argv[argv.index("--ssh-private-key") + 1], "/etc/ceph/kubeauto-cephadm")
        self.assertEqual(argv[argv.index("--ssh-public-key") + 1], "/etc/ceph/kubeauto-cephadm.pub")
        self.assertEqual(argv[argv.index("--ssh-user") + 1], "cephadm")
        authorize = next(task for task in tasks if task["name"] == "Authorize only the dedicated cephadm public key")
        self.assertEqual(authorize["ansible.builtin.lineinfile"]["path"], "/home/cephadm/.ssh/authorized_keys")
        self.assertEqual(authorize["ansible.builtin.lineinfile"]["owner"], "cephadm")
        self.assertEqual(authorize["loop"], "{{ groups['ceph'] }}")
        reconcile = next(task for task in tasks if task["name"] == "Reconcile the declarative cephadm service specification")
        self.assertEqual(reconcile["ansible.builtin.command"]["argv"][-4:], [
            "orch", "apply", "-i", "/etc/ceph/kubeauto-cluster-spec.yml",
        ])
        self.assertLess(tasks.index(authorize), tasks.index(bootstrap))
        self.assertLess(tasks.index(bootstrap), tasks.index(reconcile))

    def test_package_mode_identity_refuses_foreign_accounts_groups_homes_and_sudoers(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cephadm.yml").read_text())
        guard = next(task["ansible.builtin.command"]["argv"][2] for task in tasks
                     if task["name"] == "Inspect the package-mode SSH account before taking ownership")
        for fault in ("none", "owned", "foreign-account", "foreign-group", "foreign-home", "foreign-sudoers",
                      "bad-marker", "relocated", "shell", "getent-error", "symlink"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                marker, home, sudoers = base / "owner", base / "home", base / "sudoers"
                if fault in {"owned", "bad-marker", "relocated", "shell", "symlink"}:
                    marker.write_text(f"cephadm:{home}\n" if fault != "bad-marker" else "foreign\n")
                if fault == "foreign-home":
                    home.mkdir()
                if fault == "foreign-sudoers":
                    sudoers.write_text("foreign\n")
                if fault == "symlink":
                    home.symlink_to(base)
                body = guard.replace("/etc/ceph/kubeauto-owned-ssh-user", str(marker)).replace(
                    "/home/cephadm", str(home)).replace("/etc/sudoers.d/kubeauto-cephadm", str(sudoers))
                account_home = str(home) if fault != "relocated" else "/other"
                shell = "/bin/bash" if fault != "shell" else "/sbin/nologin"
                wrapper = f"""
getent() {{
  [[ '{fault}' != getent-error ]] || return 3
  if [[ "$1" == passwd && '{fault}' =~ ^(owned|foreign-account|relocated|shell)$ ]]; then
    echo 'cephadm:x:987:987::{account_home}:{shell}'
  elif [[ "$1" == group && '{fault}' =~ ^(owned|foreign-group)$ ]]; then
    echo cephadm:x:987:
  else return 2
  fi
}}
{body}
"""
                result = subprocess.run(["bash", "-ceu", wrapper], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault in {"none", "owned"}, result.stderr)
        account = next(task["ansible.builtin.user"] for task in tasks if "ansible.builtin.user" in task)
        self.assertEqual(account["name"], "cephadm")
        self.assertNotIn("password", account)
        sudoers = next(task["ansible.builtin.copy"] for task in tasks
                       if task.get("ansible.builtin.copy", {}).get("dest") == "/etc/sudoers.d/kubeauto-cephadm")
        self.assertEqual(sudoers["content"], "cephadm ALL=(root) NOPASSWD: ALL\n")
        self.assertEqual(sudoers["validate"], "/usr/sbin/visudo -cf %s")

    def test_account_cleanup_requires_exact_owner_and_home_before_deletion(self) -> None:
        block = self.cleanup.split("    if test -e /etc/ceph/kubeauto-owned-ssh-user; then", 1)[1].split(
            "    for path in /etc/ceph/kubeauto-cephadm", 1)[0]
        block = "if test -e /etc/ceph/kubeauto-owned-ssh-user; then" + block
        for fault in ("none", "foreign-marker", "foreign-home", "foreign-shell", "busy-user"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                marker, home, sudoers = base / "owner", base / "home", base / "sudoers"
                marker.write_text(f"cephadm:{home}\n" if fault != "foreign-marker" else "foreign\n")
                home.mkdir()
                sudoers.write_text("owned fixture\n")
                body = block.replace("/etc/ceph/kubeauto-owned-ssh-user", str(marker)).replace(
                    "/home/cephadm", str(home)).replace("/etc/sudoers.d/kubeauto-cephadm", str(sudoers))
                account_home = str(home) if fault != "foreign-home" else "/other"
                shell = "/bin/bash" if fault != "foreign-shell" else "/bin/false"
                wrapper = f"""
getent() {{
  [[ "$1" != group ]] || return 2
  echo 'cephadm:x:987:987::{account_home}:{shell}'
}}
userdel() {{ test '{fault}' != busy-user; }}
{body}
"""
                result = subprocess.run(["bash", "-ceu", wrapper], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault == "none", result.stderr)
                self.assertEqual(home.exists(), fault != "none")
                self.assertEqual(marker.exists(), fault != "none")
                self.assertEqual(sudoers.exists(), fault != "none")

    def test_product_waits_for_actual_daemons_and_mon_quorum_before_pools(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        daemon_wait = next(task for task in tasks if task.get("register") == "ceph_daemon_state")
        self.assertIn("--refresh", daemon_wait["ansible.builtin.command"]["argv"])
        self.assertGreaterEqual(daemon_wait["retries"], 60)
        conditions = "\n".join(daemon_wait["until"])
        for role in ("mon", "mgr", "osd"):
            self.assertIn(f"'daemon_type', 'equalto', '{role}'", conditions)
        self.assertIn("ceph_expected_osds", conditions)
        quorum = next(task for task in tasks if task.get("register") == "ceph_quorum_state")
        self.assertIn("quorum_status", quorum["ansible.builtin.command"]["argv"])
        self.assertIn("quorum_names", "\n".join(quorum["until"]))
        pool = next(task for task in tasks if task["name"] == "List existing pools")
        self.assertLess(tasks.index(daemon_wait), tasks.index(quorum))
        self.assertLess(tasks.index(quorum), tasks.index(pool))

    def test_csi_charts_are_official_and_checksum_gated(self) -> None:
        expected = {
            "ceph-csi-rbd-3.17.1.tgz": "ceph-csi-rbd-3.17.1.tgz.sha256",
            "ceph-csi-cephfs-3.17.1.tgz": "ceph-csi-cephfs-3.17.1.tgz.sha256",
        }
        files = ROOT / "roles/ceph/files"
        for archive, checksum_file in expected.items():
            checksum = (files / checksum_file).read_text().split()[0]
            self.assertEqual(hashlib.sha256((files / archive).read_bytes()).hexdigest(), checksum)
        for name in ("csi-rbd-values.yml.j2", "csi-cephfs-values.yml.j2"):
            values = (ROOT / "roles/ceph/templates" / name).read_text()
            self.assertIn("repository: registry.talkschool.cn:5000/brinnatt/cephcsi", values)
            self.assertIn("tag: {{ ceph_csi_ver }}", values)
            self.assertIn("storageClass:", values)
            self.assertIn("create: true", values)

        expected_resources = {
            "ceph-csi-rbd-3.17.1.tgz": ("ceph-csi-rbd", "ceph-csi-rbd-provisioner", "ceph-csi-rbd-nodeplugin"),
            "ceph-csi-cephfs-3.17.1.tgz": ("ceph-csi-cephfs", "ceph-csi-cephfs-provisioner", "ceph-csi-cephfs-nodeplugin"),
        }
        csi_tasks = (ROOT / "roles/ceph/tasks/csi.yml").read_text()
        for archive, (chart_name, deployment, daemonset) in expected_resources.items():
            with tarfile.open(files / archive, "r:gz") as chart:
                chart_yaml = yaml.safe_load(chart.extractfile(f"{chart_name}/Chart.yaml"))
                helpers = chart.extractfile(f"{chart_name}/templates/_helpers.tpl").read().decode()
            self.assertEqual(chart_yaml["name"], chart_name)
            self.assertIn("if contains $name .Release.Name", helpers)
            self.assertIn(f"deployment/{deployment}", csi_tasks)
            self.assertIn(f"daemonset/{daemonset}", csi_tasks)

    def test_csi_values_render_against_pinned_chart_schema(self) -> None:
        env = Environment(autoescape=False)
        env.filters["from_json"] = json.loads
        env.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(
            pattern, replacement, value
        )
        fsid = "01234567-89ab-cdef-0123-456789abcdef"
        variables = {
            "ceph_cluster_fsid": {"stdout": fsid},
            "ceph_mon_dump": {"stdout": json.dumps({"mons": [
                {"public_addrs": {"addrvec": [{"type": "v2", "addr": "10.1.0.1:3300/0"}]}}
            ]})},
            "ceph_csi_ver": "v3.17.1",
            "ceph_csi_registrar_ver": "v2.16.0",
            "ceph_csi_provisioner_ver": "v6.2.0",
            "ceph_csi_attacher_ver": "v4.11.0",
            "ceph_csi_resizer_ver": "v2.1.0",
            "ceph_csi_snapshotter_ver": "v8.5.0",
            "ceph_csi_provisioner_replicas": 3,
            "ceph_csi_rbd_storage_class": "ceph-rbd",
            "ceph_csi_rbd_pool": "kubernetes-rbd",
            "ceph_csi_cephfs_storage_class": "cephfs",
            "ceph_csi_cephfs_name": "kubernetes-cephfs",
        }
        configmap_owners = {}
        for component in ("rbd", "cephfs"):
            with self.subTest(component=component):
                template = (ROOT / f"roles/ceph/templates/csi-{component}-values.yml.j2").read_text()
                rendered = yaml.safe_load(env.from_string(template).render(**variables))
                with tarfile.open(ROOT / f"roles/ceph/files/ceph-csi-{component}-3.17.1.tgz") as chart:
                    chart_values = yaml.safe_load(chart.extractfile(f"ceph-csi-{component}/values.yaml"))

                def check_keys(actual: dict, schema: dict, prefix: str = "") -> None:
                    for key, value in actual.items():
                        self.assertIn(key, schema, f"unknown Chart value {prefix}{key}")
                        if isinstance(value, dict) and isinstance(schema[key], dict) and schema[key]:
                            check_keys(value, schema[key], f"{prefix}{key}.")

                check_keys(rendered, chart_values)
                self.assertEqual(rendered["csiConfig"][0]["clusterID"], fsid)
                self.assertEqual(rendered["csiConfig"][0]["monitors"], ["10.1.0.1:3300"])
                self.assertEqual(rendered["storageClass"]["clusterID"], fsid)
                if component == "cephfs":
                    self.assertEqual(rendered["storageClass"].get("kernelMountOptions"), "ms_mode=secure")
                for key in ("configMapName", "cephConfConfigMapName", "kmsConfigMapName"):
                    name = rendered.get(key, chart_values[key])
                    self.assertNotIn(name, configmap_owners, f"ConfigMap {name} is owned by both CSI releases")
                    configmap_owners[name] = component

    def test_csi_credentials_never_enter_values_or_logs(self) -> None:
        for path in (ROOT / "roles/ceph/templates").glob("csi-*-values.yml.j2"):
            text = path.read_text()
            self.assertNotRegex(text, r"(?m)^\s*(?:userKey|adminKey):")
        self.assertIn("no_log: true", self.role)
        self.assertIn("Build Ceph-CSI Secret manifests in memory", self.role)
        self.assertIn("Apply Ceph-CSI Secrets through stdin", self.role)
        tasks = yaml.safe_load((ROOT / "roles/ceph/tasks/cluster.yml").read_text())
        bootstrap = next(task for task in tasks if task.get("register") == "ceph_bootstrap_result")
        self.assertTrue(bootstrap.get("no_log"), "official bootstrap output contains the initial Dashboard password")

    def test_csi_render_rejects_collisions_and_unowned_configmaps(self) -> None:
        configmap = {"kind": "ConfigMap", "metadata": {"name": "rbd-config"}}
        deployment = {
            "kind": "Deployment", "metadata": {"name": "rbd-provisioner"},
            "spec": {"template": {"spec": {"volumes": [
                {"configMap": {"name": "rbd-config"}},
            ]}}},
        }
        CEPH_CONTRACT.assert_csi_resource_ownership({"rbd": [configmap, deployment]}, "ceph-csi")
        with self.assertRaisesRegex(ValueError, "ownership collision"):
            CEPH_CONTRACT.assert_csi_resource_ownership({"rbd": [configmap], "cephfs": [configmap]}, "ceph-csi")
        with self.assertRaisesRegex(ValueError, "unowned ConfigMap"):
            CEPH_CONTRACT.assert_csi_resource_ownership({"rbd": [deployment]}, "ceph-csi")
        with self.assertRaisesRegex(ValueError, "wrong namespace"):
            CEPH_CONTRACT.assert_csi_resource_ownership({"rbd": [
                {"kind": "ConfigMap", "metadata": {"name": "config", "namespace": "other"}},
            ]}, "ceph-csi")
        self.assertIn("ceph_contract.py csi-render", self.runner)

    def test_matrix_status_summary_and_slow_osd_causal_chain(self) -> None:
        cases = self.matrix["cases"]
        self.assertTrue(cases)
        summary = self.matrix["coverage_summary"]
        self.assertEqual(summary["total"], len(cases))
        for status in ("pending", "pass", "fail"):
            self.assertEqual(summary[status], sum(case["status"] == status for case in cases))
        for case in cases:
            self.assertIn(case["status"], {"pending", "pass", "fail"})
            for key in ("product_command", "expected_marker", "official_reference", "fixture_boundary", "cleanup_scope", "disproof_command"):
                self.assertTrue(case.get(key), f"{case['id']} missing {key}")
            if case["status"] == "pass":
                for fact in (case["expected_marker"], "run_id=", "CEPH_GATE_EXIT rc=0", "failure_markers=0", "LAB_CLEAN_VERIFY_PASS"):
                    self.assertIn(fact, case.get("evidence", ""))
        if self.matrix["meta"]["result"] == "PASS":
            self.assertEqual(summary["pass"], len(cases))
        body = (ROOT / "tests/ceph-test-matrix.yaml").read_text()
        for fact in (
            "dm-delay", "await", "queue depth", "commit_latency", "apply_latency",
            "slow ops", "p95", "p99", "RBD", "CephFS", "S3", "hash",
            "AWS", "Azure", "Oracle", "Alibaba", "Huawei",
        ):
            self.assertIn(fact, body)
        alignment = self.matrix["documentation_alignment"]
        self.assertEqual(set(alignment), {
            "01-architecture.md", "02-cephadm.md", "03-rados.md", "04-cephfs.md",
            "05-rbd.md", "06-radosgw.md", "07-mgr.md", "08-mgr-dashboard.md",
            "09-monitoring.md",
        })
        case_ids = {case["id"] for case in cases}
        self.assertTrue(all(set(case_list) <= case_ids for case_list in alignment.values()))

    def test_matrix_commands_and_markers_match_executable_runner(self) -> None:
        scripts = self.runner + self.regression + self.cleanup + (
            ROOT / "tests/helpers/ceph-host-probe.sh"
        ).read_text(encoding="utf-8")
        for case in self.matrix["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["product_command"].startswith(
                    "bash tests/run_enterprise_regression.sh --ceph-"
                ))
                mode = case["product_command"].split()[-1]
                self.assertIn(mode, self.runner)
                self.assertIn(case["expected_marker"], scripts)
                self.assertNotIn("ceph-lab", case["product_command"] + case["disproof_command"])

    def test_runner_embedded_matrix_parser_compiles(self) -> None:
        match = re.search(
            r"verify_matrix_contract\(\) \{.*?<<'PY'\n(.*?)\nPY",
            self.regression,
            re.S,
        )
        self.assertIsNotNone(match)
        blocks = re.findall(r"<<'PY'\n(.*?)\nPY", self.regression, re.S)
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            ast.parse(block)

    def test_os_qualification_requires_all_real_profiles_and_current_bound_evidence(self) -> None:
        import copy
        self.assertEqual(CEPH_CONTRACT.qualify_os_matrix(ROOT / "tests/ceph-test-matrix.yaml"), 6)
        fingerprint = "a" * 64
        with self.assertRaisesRegex(ValueError, "incomplete"):
            CEPH_CONTRACT.qualify_os_matrix(ROOT / "tests/ceph-test-matrix.yaml", fingerprint)
        markers = [
            "CEPH_HOST_VERSION_PASS", "CEPH_CHECK_HOST_PASS", "CEPH_ORCH_REMOTE_PASS",
            "CEPH_CSI_RBD_PASS", "CEPH_CSI_CEPHFS_PASS", "CEPH_IDEMPOTENCE_PASS",
            "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS",
        ]
        for fault in ("none", "missing-profile", "duplicate", "stale", "artifact", "identity", "rc",
                      "failure", "marker", "container", "insecure", "no-aes256k", "no-risk", "no-kernel",
                      "bypass", "checksum", "outside"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base / "tests").mkdir()
                (base / "logs").mkdir()
                data = copy.deepcopy(self.matrix)
                rows = data["os_qualification"]["profiles"]
                for index, row in enumerate(rows):
                    payload = dict(
                        id=row["id"], os_id=row["os_id"], major=row["major"], source_sha256=fingerprint,
                        cephadm_sha256=self.constants.v_cephadm_sha256, durable_rc=0, failure_markers=0,
                        fixture_kind="real-host", secure_msgr2=True, aes256k=True, run_id="unit-fixture",
                        client_key_type=row["client_key_type"], legacy_aes_risk_accepted=row["client_key_type"] == "aes",
                        host="authorized-host-fixture", kernel="vendor-kernel", python="vendor-python",
                        runtime="podman", product_command="kubecli setup fixture 08", markers=markers[:],
                    )
                    if index == 0:
                        changes = {
                            "stale": ("source_sha256", "b" * 64), "artifact": ("cephadm_sha256", "b" * 64),
                            "identity": ("major", "99"), "rc": ("durable_rc", 1),
                            "failure": ("failure_markers", 1), "marker": ("markers", markers[:-1]),
                            "container": ("fixture_kind", "container"), "insecure": ("secure_msgr2", False),
                            "no-risk": ("legacy_aes_risk_accepted", False), "no-kernel": ("kernel", ""),
                            "bypass": ("product_command", "ansible-playbook fixture"),
                        }
                        if fault in changes:
                            key, value = changes[fault]
                            payload[key] = value
                    if index == 1 and fault == "no-aes256k":
                        payload["aes256k"] = False
                    raw = json.dumps(payload).encode()
                    path = base / "logs" / (row["id"] + ".json")
                    path.write_bytes(raw)
                    row["status"] = "pass"
                    row["evidence"] = {"path": str(path.relative_to(base)), "sha256": hashlib.sha256(raw).hexdigest()}
                    if index == 0 and fault == "checksum":
                        path.write_bytes(raw + b" ")
                    if index == 0 and fault == "outside":
                        row["evidence"]["path"] = "tests/outside.json"
                if fault == "missing-profile":
                    rows.pop()
                elif fault == "duplicate":
                    rows[-1] = copy.deepcopy(rows[0])
                matrix = base / "tests/ceph-test-matrix.yaml"
                matrix.write_text(yaml.safe_dump(data))
                if fault == "none":
                    self.assertEqual(CEPH_CONTRACT.qualify_os_matrix(matrix, fingerprint), 6)
                else:
                    with self.assertRaises(ValueError):
                        CEPH_CONTRACT.qualify_os_matrix(matrix, fingerprint)
        full = self.runner.split('if [[ "$MODE" == "--ceph-only" ]]', 1)[1].split('\nfi', 1)[0]
        self.assertLess(full.index("os-qualification"), full.index("cancel_remote_job"))

    def test_os_probe_is_read_only_and_cannot_mark_qualification_pass(self) -> None:
        script = (ROOT / "tests/helpers/ceph-os-probe.sh").read_text()
        self.assertIn("BatchMode=yes", script)
        self.assertIn("kernel-client-check.py", script)
        self.assertIn("CEPH_OS_PROBE_PASS profiles=$count qualification=pending", script)
        self.assertNotIn("CEPH_OS_QUALIFICATION_PASS", script)
        self.assertNotIn("kubecli setup", script)
        self.assertIn('if [[ "$MODE" == "--ceph-os-probe" ]]', self.runner)
        self.assertEqual({row["ssh_user"] for row in self.matrix["os_qualification"]["profiles"]},
                         {"root", "ubuntu", "ly"})

    def test_os_probe_python_heredocs_are_valid(self) -> None:
        script = (ROOT / "tests/helpers/ceph-os-probe.sh").read_text()
        blocks = re.findall(r"<<'PY'[^\n]*\n(.*?)^PY$", script, re.MULTILINE | re.DOTALL)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            with self.subTest(block=block.splitlines()[0]):
                compile(block, "ceph-os-probe.sh heredoc", "exec")

    def test_os_probe_metadata_ssh_cannot_consume_profile_input(self) -> None:
        script = (ROOT / "tests/helpers/ceph-os-probe.sh").read_text()
        metadata = script.split('  metadata="$("${SSH[@]}"', 1)[1].split('  features=', 1)[0]
        self.assertIn('</dev/null)', metadata)

    def test_cleanup_failure_cannot_return_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cluster = Path(temporary)
            (cluster / "kubectl.kubeconfig").touch()
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
CLUSTER_DIR='{cluster}'
FSID_FILE='{cluster}/missing-fsid'
K=(mock_kubectl)
mock_kubectl() {{
  [[ "$*" == 'get --raw=/readyz' ]] && return 0
  return 1
}}
cleanup_business_fixtures
"""
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_latency_sample_has_no_unbound_label_and_propagates_business_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kubeauto-ceph-sample-") as temporary:
            for business_rc in (0, 7):
                with self.subTest(business_rc=business_rc):
                    script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{temporary}'
unset label
business_once() {{ return {business_rc}; }}
summary="$(sample_business_latency fixture 1)"
printf '%s\\n' "$summary"
"""
                    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertNotIn("unbound variable", result.stderr)
                    if business_rc:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("mixed workload failed", result.stderr)
                        self.assertNotIn("count=", result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("count=1", result.stdout)

    def test_mixed_business_stops_after_each_failed_product_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kubeauto-ceph-business-") as temporary:
            calls = Path(temporary) / "calls"
            for failing_path in ("rados", "storage-client", "s3-sigv4-put"):
                with self.subTest(failing_path=failing_path):
                    calls.write_text("")
                    script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
ceph_shell() {{ echo rados >>'{calls}'; [[ '{failing_path}' != rados ]]; }}
mock_kubectl() {{
  [[ "$*" == *"exec storage-client "* ]] || return 99
  echo storage-client >>'{calls}'
  [[ '{failing_path}' != storage-client ]]
}}
s3_sigv4_put() {{
  echo s3-sigv4-put >>'{calls}'
  [[ '{failing_path}' != s3-sigv4-put ]]
}}
K=(mock_kubectl)
if business_once fixture; then exit 0; else exit 1; fi
"""
                    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    expected = ["rados", "storage-client", "s3-sigv4-put"]
                    self.assertEqual(calls.read_text().splitlines(), expected[:expected.index(failing_path) + 1])

    def test_slow_health_is_observed_while_workload_is_in_flight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kubeauto-ceph-health-") as temporary:
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{temporary}'
OSD_ID=7
ensure_performance_baseline() {{ BASELINE_P99=100; }}
mapper_table() {{ :; }}
ssh_node() {{
  if [[ "$*" == *iostat* ]]; then
    if [[ -f "$STATE_DIR/in-flight" ]]; then
      touch "$STATE_DIR/iostat-seen"
      echo IOSTAT_IN_FLIGHT
    fi
  fi
}}
wait_clean() {{ :; }}
sample_business_latency() {{
  echo 2000 >"$STATE_DIR/slow-fixed.latency"
  touch "$STATE_DIR/in-flight"
  for _ in $(seq 1 100); do
    if [[ -f "$STATE_DIR/iostat-seen" && -f "$STATE_DIR/osd-perf-seen" && -f "$STATE_DIR/health-seen" ]]; then break; fi
    command sleep 0.01
  done
  test -f "$STATE_DIR/iostat-seen" && test -f "$STATE_DIR/osd-perf-seen" && test -f "$STATE_DIR/health-seen" || return 1
  unlink "$STATE_DIR/in-flight"
  echo count=15
}}
ceph_shell() {{
  case "$*" in
    'ceph health detail')
      if [[ -f "$STATE_DIR/in-flight" ]]; then
        touch "$STATE_DIR/health-seen"
        echo SLOW_OPS
      else echo HEALTH_OK; fi ;;
    'ceph osd dump') echo 'osd.7 up   in weight 1' ;;
    'ceph daemon osd.7 dump_historic_ops') echo HISTORIC_OPS_FIXTURE ;;
    'ceph osd perf')
      if [[ -f "$STATE_DIR/in-flight" ]]; then
        touch "$STATE_DIR/osd-perf-seen"
        echo OSD_PERF_IN_FLIGHT
      fi ;;
  esac
}}
sleep() {{ command sleep 0.01; }}
run_fixed_slow_osd
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SLOW_OPS", result.stdout)
            self.assertIn("HISTORIC_OPS_FIXTURE", result.stdout)
            self.assertIn("IOSTAT_IN_FLIGHT", result.stdout)
            self.assertIn("OSD_PERF_IN_FLIGHT", result.stdout)
            self.assertIn("CEPH_SLOW_OSD_FIXED_PASS", result.stdout)
            self.assertFalse((Path(temporary) / "slow-fixed.summary").exists())

    def test_io_throttle_samples_before_restoration_and_requires_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{directory}'
CONTRACT_SCRIPT='{CONTRACT_PATH}'
FSID_FILE="$STATE_DIR/fsid"
printf '%s\\n' 25000000-0000-4000-8000-000000000001 >"$FSID_FILE"
OSD_ID=7
limited=false
awk() {{ echo /dev/disk/by-id/fixture; }}
ssh_node() {{
  case "$*" in
    *set-property*)
      if [[ "$*" == *'IOWriteIOPSMax=/dev/disk/by-id/fixture 25'* ]]; then limited=true; else limited=false; fi ;;
    *lsblk*) echo 252:16 ;;
    *io.max*)
      value=max
      [[ "$limited" != true ]] || value=25
      echo "252:16 rbps=max wbps=max riops=$value wiops=$value" ;;
    *systemctl*show*)
      if [[ "$limited" = true ]]; then echo '/dev/disk/by-id/fixture 25'; fi ;;
    *iostat*) test "$limited" = true; echo IOSTAT_WHILE_LIMITED ;;
    *) return 1 ;;
  esac
}}
sample_business_latency() {{ echo count=12; }}
run_throttle_phase() {{
  rate=1000; latency=0.01
  case "$1" in
    limited) ssh_node "$SLOW_HOST" iostat; rate=24; latency=0.2 ;;
    recovered) test "$limited" = false; rate=900; latency=0.015 ;;
    healthy) test "$limited" = false ;;
  esac
  printf '{{"write_iops":%s,"average_latency":%s}}\\n' "$rate" "$latency" >"$STATE_DIR/throttle-$1.json"
}}
run_osd_throttle
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("IOSTAT_WHILE_LIMITED", result.stdout)
            self.assertIn("CEPH_IOPS_EFFECT_PASS", result.stdout)
            self.assertIn("CEPH_OSD_THROTTLE_CLASSIFICATION_PASS", result.stdout)

    def test_network_fault_refuses_loopback_route(self) -> None:
        script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
ssh_node() {{ echo lo; }}
run_network_differential
"""
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid or loopback interface", result.stderr)

    def test_cgroup_io_parsers_bind_the_exact_physical_device(self) -> None:
        raw = "253:2 rbytes=10 wbytes=20 rios=1 wios=2\n252:16 wios=8 rios=4 wbytes=40 rbytes=30\n"
        snapshot = CEPH_CONTRACT.cgroup_io_snapshot(raw, "252:16")
        self.assertEqual(snapshot["device"], "252:16")
        self.assertEqual(snapshot["wios"], 8)
        for corrupted in ("", raw + raw, raw.replace("wios=8", "wios=-1")):
            with self.subTest(raw=corrupted), self.assertRaises((ValueError, KeyError)):
                CEPH_CONTRACT.cgroup_io_snapshot(corrupted, "252:16")
        limit = "252:16 rbps=max wbps=max riops=25 wiops=25"
        CEPH_CONTRACT.assert_cgroup_iops_limit(limit, "252:16", "25")
        CEPH_CONTRACT.assert_cgroup_iops_limit("", "252:16", "max")
        for corrupted in ("", limit.replace("252:16", "253:2"), limit.replace("wiops=25", "wiops=max")):
            with self.subTest(limit=corrupted), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_cgroup_iops_limit(corrupted, "252:16", "25")
        with self.assertRaises(ValueError):
            CEPH_CONTRACT.assert_cgroup_iops_limit(limit, "252:16", "max")

    def test_throttle_requires_sustained_io_actual_impact_and_recovery(self) -> None:
        before = {"device": "252:16", "time": 10, "wios": 10}
        after = {"device": "252:16", "time": 40, "wios": 730}
        benchmark = {"bench": {"total_time_run": "30", "total_writes_made": "500", "average_latency": "0.2"}}
        evidence = CEPH_CONTRACT.throttle_phase_evidence(benchmark, before, after)
        self.assertEqual(evidence["write_iops"], 24)
        for changed in ({"device": "253:2"}, {"time": 20}, {"wios": 10}, {"time": float("nan")}):
            with self.subTest(snapshot=changed), self.assertRaises(ValueError):
                CEPH_CONTRACT.throttle_phase_evidence(benchmark, before, {**after, **changed})
        for changed in ({"total_time_run": "5"}, {"total_time_run": "nan"}, {"total_writes_made": "0"}, {"average_latency": "nan"}):
            with self.subTest(benchmark=changed), self.assertRaises(ValueError):
                CEPH_CONTRACT.throttle_phase_evidence({"bench": {**benchmark["bench"], **changed}}, before, after)
        healthy = {"write_iops": 1000, "average_latency": .01}
        recovered = {"write_iops": 900, "average_latency": .015}
        CEPH_CONTRACT.assert_throttle_effect(healthy, evidence, recovered, 25)
        for first, second, third in (
            ({**healthy, "write_iops": 20}, evidence, recovered),
            (healthy, {**evidence, "write_iops": 100}, recovered),
            (healthy, {**evidence, "average_latency": .015}, recovered),
            (healthy, evidence, {**recovered, "write_iops": 30}),
            (healthy, evidence, {**recovered, "average_latency": .15}),
            (healthy, {**evidence, "write_iops": float("nan")}, recovered),
        ):
            with self.subTest(phases=(first, second, third)), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_throttle_effect(first, second, third, 25)

    def test_throttle_phase_samples_during_the_bounded_demand_workload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{directory}'
CONTRACT_SCRIPT='{CONTRACT_PATH}'
THROTTLE_MAJOR_MINOR=252:16
read_osd_cgroup_io() {{ echo IO_STAT_FIXTURE; }}
python3() {{
  if [[ "$2" == io-snapshot ]]; then
    cat >/dev/null
    stamp=10; writes=10
    if [[ -f "$STATE_DIR/bench-finished" ]]; then stamp=40; writes=730; fi
    printf '{{"device":"252:16","time":%s,"wios":%s}}\\n' "$stamp" "$writes"
  else command python3 "$@"; fi
}}
ceph_shell() {{
  case "$*" in
    *'bench 30 write -b 4096 --object-size 4194304 -t 32'*)
      command sleep .5
      touch "$STATE_DIR/bench-finished"
      echo '{{"bench":{{"total_time_run":30,"total_writes_made":500,"average_latency":0.2}}}}' ;;
    'ceph osd perf') kill -0 "$INJECTOR_PID"; echo OSD_PERF_IN_FLIGHT ;;
    *'cleanup --run-name kubeauto-throttle-limited') touch "$STATE_DIR/bench-cleaned" ;;
    *) return 1 ;;
  esac
}}
ssh_node() {{ kill -0 "$INJECTOR_PID"; echo IOSTAT_IN_FLIGHT; }}
sample_business_latency() {{ kill -0 "$INJECTOR_PID"; echo count=4; }}
run_throttle_phase limited
test -f "$STATE_DIR/bench-cleaned"
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("IOSTAT_IN_FLIGHT", result.stdout)
            self.assertIn("OSD_PERF_IN_FLIGHT", result.stdout)
            self.assertIn('"write_iops": 24.0', result.stdout)

    def test_s3_wrong_key_targets_the_real_gateway(self) -> None:
        self.assertIn('RGW_ENDPOINT="${rgw_ip}:8080"', self.regression)
        self.assertIn('MC_HOST_bad="http://${RGW_ACCESS}:wrong@${RGW_ENDPOINT}"', self.regression)
        self.assertNotIn('MC_HOST_bad="http://${RGW_ACCESS}:wrong@127.0.0.1', self.regression)
        self.assertIn("authentication denial", self.regression)
        self.assertIn("--gen-access-key --gen-secret", self.regression)
        self.assertIn('"MC_HOST_ceph":f"http://{access}:{secret}@{endpoint}"', self.regression)
        self.assertNotIn('"RGW_ACCESS":access,"RGW_SECRET":secret', self.regression)
        self.assertIn("secretKeyRef: {name: rgw-client, key: MC_HOST_ceph}", self.regression)
        self.assertNotIn("--from-literal=secret=", self.regression)

    def test_pg_activity_requires_real_work_not_waiting_or_partial_scrub(self) -> None:
        row = {"pgid": "1.0", "state": "active+recovering", "acting": [7, 8, 9]}
        CEPH_CONTRACT.assert_pg_activity({"pg_stats": [row]}, "recovery", osd_id=7)
        CEPH_CONTRACT.assert_pg_activity(
            {"pg_stats": [{**row, "state": "active+clean+scrubbing+deep"}]}, "deep-scrub"
        )
        for state in ("active+clean", "active+recovery_wait", "active+backfill_wait", "active+forced_recovery"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_pg_activity({"pg_stats": [{**row, "state": state}]}, "recovery", osd_id=7)
        for state in ("active+clean", "active+clean+scrubbing", "active+clean+deep"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_pg_activity({"pg_stats": [{**row, "state": state}]}, "deep-scrub")
        with self.assertRaises(ValueError):
            CEPH_CONTRACT.assert_pg_activity({"pg_stats": [row]}, "recovery", osd_id=10)
        for document in ([], {}, {"pg_stats": []}):
            with self.subTest(document=document), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_pg_activity(document, "recovery")

    def test_recovery_target_is_another_replica_of_a_populated_slow_osd_pg(self) -> None:
        row = {"pgid": "1.0", "state": "active+clean", "acting": [7, 8, 9], "stat_sum": {"num_bytes": 4194304}}
        self.assertEqual(CEPH_CONTRACT.recovery_target({"pg_stats": [row]}, 7), 8)
        for changed in ({"acting": [7]}, {"acting": [8, 9, 10]}, {"stat_sum": {"num_bytes": 0}}, {"state": "active+degraded"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                CEPH_CONTRACT.recovery_target({"pg_stats": [{**row, **changed}]}, 7)

    def test_contention_gate_rejects_activity_outside_the_business_sample(self) -> None:
        for overlap in (False, True):
            with self.subTest(overlap=overlap), tempfile.TemporaryDirectory() as directory:
                script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{directory}'
CONTRACT_SCRIPT='{CONTRACT_PATH}'
sample_business_latency() {{
  touch "$STATE_DIR/business-running"
  for ((attempt=0; attempt<200; attempt++)); do
    [[ ! -f "$STATE_DIR/activity-checked" ]] || break
    command sleep .01
  done
  [[ -f "$STATE_DIR/activity-checked" ]] || return 1
  unlink "$STATE_DIR/business-running"
  echo count=12
}}
ceph_shell() {{
  for ((attempt=0; attempt<200; attempt++)); do
    [[ ! -f "$STATE_DIR/business-running" ]] || break
    command sleep .01
  done
  [[ -f "$STATE_DIR/business-running" ]] || return 1
  state=active+clean
  if [[ '{overlap}' = True && -f "$STATE_DIR/business-running" ]]; then state=active+clean+scrubbing+deep; fi
  printf '{{"pg_stats":[{{"pgid":"1.0","state":"%s","acting":[7,8,9]}}]}}\\n' "$state"
}}
python3() {{
  if [[ "${{2:-}}" == pg-activity ]]; then touch "$STATE_DIR/activity-checked"; fi
  command python3 "$@"
}}
sleep() {{ command sleep .01; }}
sample_during_pg_activity CEPH-18 scrub-contention deep-scrub fixture-pool
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=8)
                self.assertEqual(result.returncode == 0, overlap, result.stdout + result.stderr)
                if overlap:
                    self.assertIn("CEPH_PG_ACTIVITY_OVERLAP", result.stdout)
                    self.assertIn("count=12", result.stdout)
                else:
                    self.assertIn("no PG activity overlapped", result.stderr)

    def test_contention_fixtures_are_bounded_and_use_independent_recovery_osd(self) -> None:
        recovery = self.regression.split("run_slow_recovery() {", 1)[1].split("\n}", 1)[0]
        scrub = self.regression.split("run_scrub_contention() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("--max-objects 128", recovery)
        self.assertIn('ceph osd out "$RECOVERY_OSD_ID"', recovery)
        self.assertNotIn('ceph osd out "$OSD_ID"', recovery)
        self.assertIn('ceph osd in "$RECOVERY_OSD_ID"', recovery)
        self.assertIn("--max-objects 1024", scrub)
        self.assertIn("pg_autoscale_mode off", scrub)
        self.assertIn("sample_during_pg_activity", recovery + scrub)

    def test_runtime_cleanup_selects_only_kubernetes_shims_and_their_descendants(self) -> None:
        processes = {
            10: (1, ["/usr/local/bin/containerd-shim-runc-v2", "-namespace", "k8s.io"]),
            11: (10, ["/pause"]),
            12: (10, ["/node-cache"]),
            13: (12, ["/fixture-child"]),
            20: (1, ["/usr/bin/conmon"]),
            21: (20, ["ceph-osd"]),
        }
        self.assertEqual(CEPH_RUNTIME.fixture_processes(processes), {10, 11, 12, 13})

    def test_runtime_probe_supports_native_rocky8_python_without_pip(self) -> None:
        tree = ast.parse(RUNTIME_PATH.read_text(), feature_version=(3, 6))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "__future__")
            if isinstance(node, ast.Call):
                self.assertFalse({"capture_output", "text"} & {arg.arg for arg in node.keywords})
        compute_probe = self.regression.split("verify_compute_hosts() {", 1)[1].split(
            "\n}\n", 1
        )[0]
        self.assertIn("exec /usr/libexec/platform-python -", compute_probe)
        self.assertIn("exec /usr/libexec/platform-python - --clean", self.cleanup)
        self.assertNotIn("dnf install", compute_probe)
        for argv in (["containerd-shim-runc-v2"], ["containerd-shim-runc-v2", "-namespace"], ["containerd-shim-runc-v2", "-namespace", "moby"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                CEPH_RUNTIME.fixture_processes({10: (1, argv)})

    def test_runtime_verify_rejects_orphan_shims_and_nodelocaldns_without_mutation(self) -> None:
        stopped = SimpleNamespace(returncode=3, stdout="inactive\n")
        shim = {10: (1, ["containerd-shim-runc-v2", "-namespace", "k8s.io"])}
        for processes, links in ((shim, []), ({}, ["nodelocaldns"]), ({}, [])):
            with self.subTest(processes=processes, links=links), \
                 patch.object(CEPH_RUNTIME.Path, "exists", return_value=False), \
                 patch.object(CEPH_RUNTIME.subprocess, "run", return_value=stopped), \
                 patch.object(CEPH_RUNTIME, "read_processes", return_value=processes), \
                 patch.object(CEPH_RUNTIME, "cni_links", return_value=links), \
                 patch.object(CEPH_RUNTIME.os, "kill") as kill:
                if processes or links:
                    with self.assertRaisesRegex(ValueError, "fixture residue"):
                        CEPH_RUNTIME.run(False)
                else:
                    CEPH_RUNTIME.run(False)
                kill.assert_not_called()

    def test_runtime_clean_refuses_active_services_and_foreign_namespace(self) -> None:
        for state, processes in (
            (SimpleNamespace(returncode=0, stdout="active\n"), {}),
            (SimpleNamespace(returncode=3, stdout="inactive\n"), {10: (1, ["containerd-shim-runc-v2", "-namespace", "moby"])}),
        ):
            with self.subTest(state=state, processes=processes), \
                 patch.object(CEPH_RUNTIME.Path, "exists", return_value=False), \
                 patch.object(CEPH_RUNTIME.subprocess, "run", return_value=state), \
                 patch.object(CEPH_RUNTIME, "read_processes", return_value=processes), \
                 patch.object(CEPH_RUNTIME.os, "kill") as kill:
                with self.assertRaises(ValueError):
                    CEPH_RUNTIME.run(True)
                kill.assert_not_called()

    def test_runtime_clean_freezes_then_kills_only_the_owned_process_tree(self) -> None:
        processes = {10: (1, ["containerd-shim-runc-v2", "-namespace", "k8s.io"]), 11: (10, ["/node-cache"])}
        stopped = SimpleNamespace(returncode=3, stdout="inactive\n")
        with patch.object(CEPH_RUNTIME.Path, "exists", return_value=False), \
             patch.object(CEPH_RUNTIME.subprocess, "run", return_value=stopped), \
             patch.object(CEPH_RUNTIME, "read_processes", side_effect=[processes, processes, {}, {}]), \
             patch.object(CEPH_RUNTIME, "cni_links", side_effect=[["nodelocaldns"], []]), \
             patch.object(CEPH_RUNTIME.os, "kill") as kill:
            CEPH_RUNTIME.run(True)
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {10, 11})
            signals = [call.args[1] for call in kill.call_args_list]
            self.assertLess(signals.index(CEPH_RUNTIME.signal.SIGSTOP), signals.index(CEPH_RUNTIME.signal.SIGKILL))
        self.assertIn('python3 - --clean', self.cleanup)
        verify = self.cleanup.split("verify_clean() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('ceph_runtime_cleanup.py', verify)
        self.assertIn('ceph_runtime_cleanup.py', self.runner.split("ceph_gate_fingerprint() {", 1)[1].split("\n}\n", 1)[0])

    def test_production_reconciliation_is_bound_to_owned_fsid(self) -> None:
        cluster = (ROOT / "roles/ceph/tasks/cluster.yml").read_text()
        prepare = (ROOT / "roles/ceph/tasks/prepare.yml").read_text()
        csi = (ROOT / "roles/ceph/tasks/csi.yml").read_text()
        self.assertIn("unowned Ceph state exists; refusing takeover", cluster)
        self.assertIn("'--fsid', ceph_requested_fsid.stdout", cluster)
        self.assertIn("ceph_existing_fsid.stdout | trim == ceph_requested_fsid.stdout", cluster)
        self.assertIn("ceph_owned_fsid", prepare)
        self.assertIn("ceph_csi_owned_fsid", csi)
        self.assertNotIn("--from-literal=userKey=", csi)
        self.assertIn("--from-file=userKey=/dev/stdin", csi)
        self.assertLess(
            csi.index("Refuse to take over a foreign Ceph-CSI namespace"),
            csi.index("Create the Ceph-CSI namespace declaratively"),
        )
        self.assertIn("kubeauto.io/ceph-fsid", csi)

    def test_runner_owns_durable_ceph_lifecycle(self) -> None:
        for mode in (
            "--ceph-only", "--ceph-status", "--ceph-follow", "--ceph-cancel",
            "--ceph-clean-only", "--ceph-lab-bootstrap", "--ceph-probe",
        ):
            self.assertIn(mode, self.runner)
        for marker in ("CEPH_DELIVERY_PASS", "CEPH_CLEAN_VERIFY_PASS", "LAB_CLEAN_VERIFY_PASS"):
            self.assertIn(marker, self.regression + self.cleanup + self.runner)
        self.assertIn("run-durable-gate.sh", self.runner)
        for mode in (
            "--ceph-slow-osd-fixed", "--ceph-slow-osd-intermittent", "--ceph-slow-vs-down",
            "--ceph-osd-throttle", "--ceph-bluefs-slow", "--ceph-network-vs-disk",
        ):
            self.assertIn(mode, self.runner)
        self.assertIn("kubeauto-ceph-lab-", self.regression)
        self.assertNotIn("ceph_allow_test_paths", self.regression)
        self.assertIn("ceph_allow_test_mappers=true", self.regression)
        self.assertRegex(self.cleanup, r"kubeauto-ceph-\(slow\|throttle\|lab\)")

    def test_compute_python_bootstrap_preserves_native_package_and_alias_contracts(self) -> None:
        function = "bootstrap_compute_python() {" + self.regression.split(
            "bootstrap_compute_python() {", 1
        )[1].split("\nvalidate_allowlist_shape()", 1)[0]
        payload = re.search(r"<<'PYTHON_BOOTSTRAP'\n(.*?)\nPYTHON_BOOTSTRAP", function, re.S).group(1)
        self.assertNotIn("alternatives", payload)
        self.assertIn("seq 1 100", payload)
        self.assertIn("seq 1 30", function)
        for fault in ("none", "os", "foreign-os", "install", "owner", "integrity", "native-alias", "foreign-alias", "import"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                identity = "ubuntu" if fault == "foreign-os" else "rocky"
                script = payload.replace(". /etc/os-release", "ID=" + identity + "; VERSION_ID=" + ("9.8" if fault == "os" else "8.10"))
                script = script.replace("  /usr/bin/python3.12 -c", "  mock_python -c").replace("/usr/bin/python3.12 --version", "mock_python --version")
                mocks = f"""
dnf() {{ [[ "$*" == 'install -y python3.12' && '{fault}' != install ]]; }}
rpm() {{
  [[ "$1" != -qf || '{fault}' != owner ]] &&
  [[ "$1" != -V || '{fault}' != integrity ]]
}}
readlink() {{
  case '{fault}' in
    native-alias) echo /usr/bin/python3.12 ;;
    foreign-alias) echo /usr/local/bin/python3 ;;
    *) echo /usr/bin/python3.9 ;;
  esac
}}
mock_python() {{ [[ '{fault}' != import ]]; }}
"""
                result = subprocess.run(["bash", "-euc", mocks + script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault in {"none", "native-alias"}, result.stderr)
        bootstrap = self.runner.split('if [[ "$MODE" == "--ceph-lab-python-bootstrap" ]]', 1)[1].split("\nfi", 1)[0]
        self.assertLess(bootstrap.index("--ceph-static-only"), bootstrap.index("sync-kubeauto.sh"))
        self.assertIn("--compute-python-bootstrap", bootstrap)
        self.assertNotIn("roles/ceph", bootstrap)

    def test_compute_python_bootstrap_stops_at_failed_clean_install_or_ping(self) -> None:
        function = "bootstrap_compute_python() {" + self.regression.split(
            "bootstrap_compute_python() {", 1
        )[1].split("\nvalidate_allowlist_shape()", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "tests/helpers").mkdir(parents=True)
            (base / "tests/helpers/ceph_runtime_cleanup.py").touch()
            for fault in ("none", "clean", "install", "ping"):
                with self.subTest(fault=fault):
                    script = f"""
set -euo pipefail
BASE='{base}'
KUBE_HOSTS=(192.168.47.134)
stage() {{ :; }}
fail() {{ echo CEPH_STAGE_FAIL >&2; return 1; }}
ssh_node() {{
  if [[ "$*" == *'bash -s' ]]; then
    while IFS= read -r line; do :; done
    [[ '{fault}' != install ]]
  else [[ '{fault}' != clean ]]; fi
}}
ansible() {{ [[ '{fault}' != ping ]]; }}
{function}
bootstrap_compute_python
"""
                    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                    self.assertEqual(result.returncode == 0, fault == "none", result.stderr)
                    self.assertEqual("CEPH_COMPUTE_PYTHON_BOOTSTRAP_PASS" in result.stdout, fault == "none")

    def test_fresh_control_prepares_product_artifacts_before_ansible_preflight(self) -> None:
        function = "prepare_ceph_control_artifacts() {" + self.runner.split(
            "prepare_ceph_control_artifacts() {", 1
        )[1].split("\nverify_ceph_ansible_syntax()", 1)[0]
        self.assertIn(".venv/bin/python kubecli.py download -D", function)
        focused = self.runner.split('case "$MODE" in\n  --ceph-slow-osd-fixed', 1)[1].split('\nesac', 1)[0]
        self.assertLess(focused.index("prepare_ceph_control_artifacts"),
                        focused.index("verify_ceph_ansible_syntax"))
        self.assertLess(focused.index("verify_ceph_ansible_syntax"),
                        focused.index("lab-control-ssh-bootstrap.sh"))
        self.assertIn('CEPH_TEST_HOST="${CEPH_TEST_HOST:-root@192.168.47.130}"', self.runner)

    def test_control_probes_expat_import_and_updates_only_the_failed_dependency(self) -> None:
        function = "prepare_ceph_control_environment() {" + self.runner.split(
            "prepare_ceph_control_environment() {", 1
        )[1].split("\nprepare_ceph_control_artifacts()", 1)[0]
        for broken in (False, True):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(["bash", "-c", f"""
set -euo pipefail
python3.12() {{
  if [[ "$*" == '-c import pyexpat' && '{broken}' == True && ! -f '{directory}/fixed' ]]; then
    echo 'undefined symbol: XML_SetBillionLaughsAttackProtectionMaximumAmplification' >&2
    return 1
  fi
}}
skopeo() {{ :; }}
dnf() {{ [[ "$*" == 'upgrade -y expat' ]]; touch '{directory}/fixed'; echo EXPAT_UPDATED; }}
ssh_ceph() {{ bash -c "$(declare -f python3.12 skopeo dnf); $1"; }}
{function}
prepare_ceph_control_environment
"""], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("EXPAT_UPDATED" in result.stdout, broken)
                self.assertIn("CEPH_CONTROL_PYTHON_PASS", result.stdout)

    def test_disabled_product_gate_checks_facts_and_remote_ownership(self) -> None:
        function = "verify_disabled_product() {" + self.regression.split(
            "verify_disabled_product() {", 1
        )[1].split("\nprepare_product_cluster()", 1)[0]
        for fault in ("none", "facts", "owner", "fsid", "command"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(["bash", "-c", f"""
set -euo pipefail
STATE_DIR='{directory}'
FSID_FILE='{directory}/fsid'
CLUSTER=fixture
CEPH_HOSTS=(storage)
KUBECLI=(fixture_kubecli)
fail() {{ return 1; }}
fixture_kubecli() {{
  [[ "$*" == 'setup fixture 08' ]] || return 97
  [[ '{fault}' != command ]] || return 23
  if [[ '{fault}' == facts ]]; then echo 'TASK [Gathering Facts]'; fi
}}
ssh_node() {{ [[ "$2" == 'test ! -e /etc/ceph/kubeauto-owned-fsid' && '{fault}' != owner ]]; }}
if [[ '{fault}' == fsid ]]; then printf fsid >"$FSID_FILE"; fi
{function}
verify_disabled_product
"""], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault == "none", result.stderr)
                self.assertEqual("CEPH_DEFAULT_DISABLED_PASS" in result.stdout, fault == "none")

    def test_new_kubernetes_cluster_requires_shared_business_smoke(self) -> None:
        prepare = self.regression.split("prepare_product_cluster() {", 1)[1].split("wait_clean()", 1)[0]
        self.assertIn('bash "$BASE/tests/helpers/kubernetes-production-smoke.sh"', prepare)
        self.assertIn('KUBECONFIG="${CLUSTER_DIR}/kubectl.kubeconfig"', prepare)
        self.assertIn("PRODUCTION_SMOKE_IMAGE=registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1", prepare)
        self.assertIn("brinnatt/json-mock:v1.3.1", self.constants.component_images["ceph"])
        self.assertIn("'json-mock|v1.3.1'", self.regression)
        self.assertLess(prepare.index("kubernetes-production-smoke.sh"), prepare.index("echo CEPH_CLUSTER_INSTALL_PASS"))

    def test_lab_inventory_has_disjoint_compute_and_storage_groups(self) -> None:
        assignments = self.regression.split('SSH=(ssh', 1)[0]
        function = "write_inventory() {" + self.regression.split("write_inventory() {", 1)[1].split(
            "\nconfigure_product()", 1
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "hosts"
            result = subprocess.run(["bash", "-c", f"""
set -euo pipefail
{assignments}
ssh_node() {{ echo "ceph-${{1##*.}}"; }}
inventory_line() {{ printf '%s ceph_hostname=fixture' "$1"; }}
{function}
write_inventory '{inventory}'
"""], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            groups = {}
            for line in inventory.read_text().splitlines():
                if line.startswith("["):
                    group = line.strip("[]")
                    groups[group] = set()
                elif re.match(r"\d+\.", line):
                    groups[group].add(line.split()[0])
            compute = groups["kube_master"] | groups["kube_node"]
            storage = groups["ceph"]
            self.assertEqual(len(compute), 6)
            self.assertEqual(len(storage), 6)
            self.assertFalse(compute & storage)
            self.assertEqual(groups["etcd"], groups["kube_master"])
            self.assertTrue(all(host.startswith("192.168.47.") for host in compute))
            self.assertTrue(all(host.startswith("192.168.122.") for host in storage))
            for host in compute | storage:
                self.assertIn(host, self.cleanup)
                self.assertIn(host, self.runner.split("ceph_nodes=(", 1)[1].split(")", 1)[0])

    def test_cleanup_inventory_refuses_unknown_or_reassigned_hosts(self) -> None:
        compute = [f"192.168.47.{number}" for number in (134, 135, 136, 131, 132, 137)]
        storage = [f"192.168.122.{number}" for number in (135, 40, 72, 212, 165, 238)]
        def inventory():
            return {"_meta": {"hostvars": dict.fromkeys(compute + storage, {})},
                    "kube_master": {"hosts": compute[:3]}, "etcd": {"hosts": compute[:3]},
                    "kube_node": {"hosts": compute[3:]}, "ceph": {"hosts": storage}}
        CEPH_CONTRACT.assert_lab_inventory(inventory(), compute, storage)
        for fault in ("foreign", "swapped", "mixed", "child"):
            with self.subTest(fault=fault):
                data = inventory()
                if fault == "foreign":
                    data["_meta"]["hostvars"]["192.168.122.243"] = {}
                elif fault == "swapped":
                    data["kube_master"]["hosts"] = compute[3:]
                elif fault == "mixed":
                    data["kube_node"]["hosts"] = storage[:3]
                else:
                    data["kube_node"]["children"] = ["foreign"]
                with self.assertRaises(ValueError):
                    CEPH_CONTRACT.assert_lab_inventory(data, compute, storage)
        self.assertLess(self.cleanup.index("lab-inventory"),
                        self.cleanup.index('"${kubecli[@]}" destroy'))

    def test_failed_preflight_does_not_mutate_unowned_cluster(self) -> None:
        function = "failure_cleanup() {" + self.regression.split("failure_cleanup() {", 1)[1].split(
            "\nprepare_focused_environment()", 1
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(["bash", "-c", f"""
set -euo pipefail
OWNER_FILE='{directory}/missing'
ceph_shell() {{ echo FOREIGN_MUTATION >&2; }}
{function}
trap failure_cleanup EXIT
exit 19
"""], text=True, capture_output=True)
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertNotIn("FOREIGN_MUTATION", result.stdout + result.stderr)

    def test_cleanup_runtime_never_zaps_compute_disks(self) -> None:
        function = "cleanup_kubernetes_runtime() {" + self.cleanup.split(
            "cleanup_kubernetes_runtime() {", 1
        )[1].split("\nverify_clean()", 1)[0]
        self.assertIn('"${KUBE_HOSTS[@]}"', function)
        self.assertNotIn('"${CEPH_HOSTS[@]}"', function)
        self.assertIn('for host in "${CEPH_HOSTS[@]}"; do\n    echo "CEPH_STAGE_BEGIN id=CEPH-28 action=remove-fsid', self.cleanup)
        no_owner = self.cleanup.split('if [[ ! -f "$OWNER_FILE" ]]', 1)[1].split('\nfi', 1)[0]
        self.assertNotIn("cleanup_kubernetes_runtime", no_owner)

    def test_compute_lease_refuses_concurrent_owner_before_writing_state(self) -> None:
        function = "acquire_host_lease() {" + self.regression.split("acquire_host_lease() {", 1)[1].split(
            "\nssh_node()", 1
        )[0]
        for locked in (False, True):
            with self.subTest(locked=locked), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(["bash", "-c", f"""
set -euo pipefail
STATE_DIR='{directory}/state'
LEASE_FILE='{directory}/storage.lock'
COMPUTE_LEASE_FILE='{directory}/compute.lock'
CLUSTER=fixture
CEPH_HOSTS=(storage)
KUBE_HOSTS=(compute)
fail() {{ return 1; }}
flock() {{ [[ '{locked}' != True || "${{@: -1}}" != 7 ]]; }}
{function}
acquire_host_lease
"""], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, not locked, result.stderr)
                self.assertEqual((Path(directory) / "state/lease-owner").exists(), not locked)

    def test_lab_hostname_bootstrap_only_restores_authoritative_identity(self) -> None:
        expected = {
            "192.168.122.135": "ceph-01", "192.168.122.40": "ceph-02",
            "192.168.122.72": "ceph-03", "192.168.122.212": "mceph-01",
            "192.168.122.165": "mceph-02", "192.168.122.238": "mceph-03",
        }
        for address, hostname in expected.items():
            self.assertIn(f"[{address}]={hostname}", self.lab_bootstrap)
        self.assertIn("localhost|localhost.localdomain)", self.lab_bootstrap)
        self.assertIn("refusing identity overwrite", self.lab_bootstrap)
        self.assertIn("CEPH_LAB_HOSTNAME_BOOTSTRAP_PASS", self.lab_bootstrap)
        self.assertNotIn("CEPH_HOSTS:-", self.lab_bootstrap)

    def test_each_focused_mode_dispatches_only_one_scenario(self) -> None:
        self.assertIn('run_slow_device_scenario "$MODE"', self.regression)
        self.assertNotIn("run_slow_device_scenarios", self.regression)
        self.assertIn("CEPH_CASE_LEDGER_PASS", self.regression)
        self.assertIn("assert_cases_complete", self.regression)

    def test_focused_install_uses_target_version_except_upgrade(self) -> None:
        configure = "configure_product() {" + self.regression.split(
            "configure_product() {", 1
        )[1].split("\nverify_disabled_product()", 1)[0]
        function = "prepare_product_cluster() {" + self.regression.split(
            "prepare_product_cluster() {", 1
        )[1].split("\nwait_clean()", 1)[0]
        for mode, expected, actual in (
            ("--slow-osd-fixed", "20.2.4", "20.2.4"),
            ("--bluefs-slow", "20.2.4", "20.2.4"),
            ("--upgrade", "20.2.3", "20.2.3"),
            ("full", "20.2.3", "20.2.3"),
            ("--slow-osd-fixed", "20.2.4", "20.2.3"),
        ):
            with self.subTest(mode=mode, actual=actual), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "state"
                state.mkdir()
                topology = [{"daemon_type": role, "status_desc": "running"}
                            for role, count in (("mon", 3), ("mgr", 2), ("osd", 12))
                            for _ in range(count)]
                compute = [f"compute-{index}" for index in range(6)]
                storage = [f"storage-{index}" for index in range(6)]
                inventory = {"_meta": {"hostvars": dict.fromkeys(compute + storage, {})},
                             "kube_master": {"hosts": compute[:3]}, "etcd": {"hosts": compute[:3]},
                             "kube_node": {"hosts": compute[3:]}, "ceph": {"hosts": storage}}
                script = f"""
set -euo pipefail
STATE_DIR='{state}'
CLUSTER_DIR='{directory}/cluster'
OWNER_FILE="$STATE_DIR/owner"
FSID_FILE="$STATE_DIR/fsid"
BASE='{ROOT}'
CONTRACT_SCRIPT='{CONTRACT_PATH}'
MODE='{mode}'
CLUSTER=fixture
CEPH_CURRENT_IMAGE=hub.talkedu.cn/kubeauto/ceph:v20.2.4
CEPH_SOURCE_IMAGE=hub.talkedu.cn/kubeauto/ceph:v20.2.3
KUBECLI=(fixture_kubecli)
KUBE_HOSTS=({' '.join(compute)})
CEPH_HOSTS=({' '.join(storage)})
ansible-inventory() {{ echo '{json.dumps(inventory)}'; }}
python3() {{ '{sys.executable}' "$@"; }}
fixture_kubecli() {{
  if [[ "$1" == new ]]; then
    mkdir "$CLUSTER_DIR"
    printf '{{}}\\n' >"$CLUSTER_DIR/config.yml"
  fi
  if [[ "$1" == setup ]]; then
    python3 - "$CLUSTER_DIR/config.yml" "$3" <<'PY_PHASE'
import sys, yaml
with open(sys.argv[1]) as stream:
    config = yaml.safe_load(stream)
expected = "no" if sys.argv[2] == "08" else "yes"
assert config["ceph_csi_install"] == expected, config["ceph_csi_install"]
print("IMAGE=" + config["ceph_image"])
print("CSI_PHASE=" + sys.argv[2] + ":" + expected)
PY_PHASE
    printf 'PRODUCT_COMMAND=%s\\n' "$*"
  fi
}}
stage() {{ :; }}
pass() {{ :; }}
fail() {{ echo "$*" >&2; return 1; }}
write_inventory() {{ :; }}
verify_disabled_product() {{ echo CEPH_DEFAULT_DISABLED_PASS; }}
bash() {{ :; }}
ceph_shell() {{
  case "$2" in
    fsid) echo 12345678-1234-1234-1234-123456789abc ;;
    versions) echo '{{"overall": {{"ceph version {actual} (fixture) tentacle (stable)": 17}}}}' ;;
    orch) echo '{json.dumps(topology)}' ;;
    *) return 1 ;;
  esac
}}
{configure}
{function}
prepare_product_cluster
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, actual == expected, result.stderr)
                self.assertIn(f"IMAGE=hub.talkedu.cn/kubeauto/ceph:v{expected}", result.stdout)
                standalone = "PRODUCT_COMMAND=setup fixture 08 -e ceph_allow_test_mappers=true"
                combined = "PRODUCT_COMMAND=setup fixture 90 -e ceph_allow_test_mappers=true"
                self.assertLess(result.stdout.index(standalone), result.stdout.index(combined))
                self.assertIn("CSI_PHASE=08:no", result.stdout)
                self.assertIn("CSI_PHASE=90:yes", result.stdout)
                self.assertLess(result.stdout.index("CEPH_DEFAULT_DISABLED_PASS"),
                                result.stdout.index(standalone))
                if actual != expected:
                    self.assertNotIn("CEPH_CLUSTER_INSTALL_PASS", result.stdout)

    def test_full_gate_requires_current_successful_focused_source(self) -> None:
        function = 'require_ceph_focused_evidence() {' + self.runner.split(
            'require_ceph_focused_evidence() {', 1
        )[1].split('\nstage_cephadm_artifact()', 1)[0]
        for evidence in (None, "stale --slow-osd-fixed", "current --upgrade", "current --slow-osd-fixed"):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as directory:
                logs = Path(directory) / "logs"
                logs.mkdir()
                if evidence is not None:
                    (logs / "ceph-focused-green").write_text(evidence + "\n")
                script = f"ROOT='{directory}'\nceph_gate_fingerprint() {{ echo current; }}\n{function}\nrequire_ceph_focused_evidence"
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, evidence == "current --slow-osd-fixed", result.stderr)
        full = self.runner.split('if [[ "$MODE" == "--ceph-only" ]]', 1)[1].split('\nfi', 1)[0]
        self.assertLess(full.index("require_ceph_focused_evidence"), full.index("cancel_remote_job"))
        focus = self.runner.split('case "$MODE" in\n  --ceph-slow-osd-fixed', 1)[1].split('\nesac', 1)[0]
        self.assertLess(focus.index('if [[ "$focused_rc" -ne 0'), focus.index('>"$ROOT/logs/ceph-focused-green"'))

    def test_ceph_source_fingerprint_ignores_bytecode_but_tracks_runtime_dependencies(self) -> None:
        function = "ceph_gate_fingerprint() {" + self.runner.split(
            "ceph_gate_fingerprint() {", 1)[1].split("\nrequire_ceph_focused_evidence()", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            references = re.findall(r'\$ROOT(/[^"\s]+)', function)
            for relative in references:
                path = root / relative.lstrip("/")
                if "*" in relative or relative in {"/roles/ceph", "/roles/docker"}:
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            for role in ("ceph", "docker"):
                role_dir = root / "roles" / role
                role_dir.mkdir(parents=True, exist_ok=True)
                (role_dir / "main.yml").write_text("fixture\n")
            (root / "tests/helpers/ceph-fixture.sh").write_text("fixture\n")
            def fingerprint():
                result = subprocess.run(["bash", "-ceu", f"ROOT='{root}'\n{function}\nceph_gate_fingerprint"],
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            original = fingerprint()
            cache = root / "roles/ceph/__pycache__"
            cache.mkdir()
            (cache / "kernel-check.cpython-312.pyc").write_bytes(b"cache")
            self.assertEqual(original, fingerprint())
            (root / "roles/docker/main.yml").write_text("changed dependency\n")
            self.assertNotEqual(original, fingerprint())

    def test_fault_injection_and_cleanup_are_bounded(self) -> None:
        self.assertIn("dmsetup", self.regression)
        self.assertIn("kubeauto-ceph-slow-", self.regression)
        self.assertIn("CEPH_DISK_ALLOWLIST", self.regression + self.cleanup)
        self.assertRegex(self.cleanup, r"serial|SERIAL")
        self.assertRegex(self.cleanup, r"wwn|WWN|wwid")
        self.assertIn("CEPH_TEST_FSID", self.cleanup)
        self.assertNotRegex(self.cleanup, r"wipefs\s+-a\s+/dev/\$|wipefs\s+-a\s+\$\{")

    def test_cleanup_is_serialized_and_signals_are_nonzero(self) -> None:
        self.assertIn('exec 8>"${STATE_DIR}/cleanup.lock"', self.cleanup)
        self.assertIn("flock -w 1200 8", self.cleanup)
        self.assertLess(self.cleanup.index("flock -w 1200 8"), self.cleanup.index("validate_allowlist()"))
        self.assertNotIn("trap failure_cleanup EXIT INT TERM", self.regression)
        for signal, rc in (("INT", 130), ("TERM", 143)):
            self.assertIn(f"trap 'exit {rc}' {signal}", self.regression)

    def test_cleanup_stops_all_owned_managers_before_purging_any_host(self) -> None:
        function = "quiesce_owned_managers() {" + self.cleanup.split(
            "quiesce_owned_managers() {", 1
        )[1].split('\nif [[ "$MODE"', 1)[0]
        self.assertLess(
            self.cleanup.index('  quiesce_owned_managers "$fsid"'),
            self.cleanup.index('action=remove-fsid'),
        )
        fsid = "11111111-2222-3333-4444-555555555555"
        for fault in ("none", "list", "stop", "still-active"):
            with self.subTest(fault=fault):
                script = f"""
set -euo pipefail
CEPH_HOSTS=(node-a node-b)
SSH=(fixture_ssh)
fail() {{ echo "$*" >&2; exit 1; }}
fixture_ssh() {{
  shift
  bash -c "$(declare -f systemctl); $1"
}}
systemctl() {{
  [[ "${{@: -1}}" == 'ceph-{fsid}@mgr.*.service' || "$1" == stop ]]
  if [[ "$1" == stop ]]; then
    [[ "$2" == 'ceph-{fsid}@mgr.fixture.service' ]]
    [[ '{fault}' != stop ]] || return 1
    echo 'OWNED_MGR_STOPPED' >&2
  elif [[ "$*" == *--all* ]]; then
    [[ '{fault}' != list ]] || return 1
    echo 'ceph-{fsid}@mgr.fixture.service loaded active running owned mgr'
  elif [[ '{fault}' == still-active ]]; then
    echo 'ceph-{fsid}@mgr.fixture.service loaded active running owned mgr'
  fi
  return 0
}}
{function}
quiesce_owned_managers '{fsid}'
echo PURGE_UNLOCKED
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, fault == "none", result.stderr)
                self.assertEqual("PURGE_UNLOCKED" in result.stdout, fault == "none")
                if fault == "none":
                    self.assertEqual(result.stderr.count("OWNED_MGR_STOPPED"), 2)

    def test_cleanup_keeps_owner_until_residue_preverify(self) -> None:
        self.assertLess(
            self.cleanup.index('CEPH_CLEAN_PREVERIFY=1 CEPH_TEST_FSID="$fsid" verify_clean'),
            self.cleanup.index('unlink /etc/ceph/kubeauto-owned-fsid'),
        )
        self.assertIn('foreign or malformed owner marker', self.regression)

    def test_cleanup_detects_real_fsid_directories(self) -> None:
        pattern = re.search(r"-regex '([^']+)'", self.cleanup).group(1)
        with tempfile.TemporaryDirectory() as directory:
            fsid = "11111111-2222-3333-4444-555555555555"
            (Path(directory) / fsid).mkdir()
            result = subprocess.run(
                ["find", directory, "-mindepth", "1", "-maxdepth", "1", "-type", "d",
                 "-regextype", "posix-extended", "-regex", pattern, "-printf", "%f\\n"],
                text=True, capture_output=True, check=True,
            )
            self.assertEqual(result.stdout.strip(), fsid)

    def test_cleanup_verify_rejects_residue_and_inspection_errors(self) -> None:
        body = self.cleanup.split('    "${SSH[@]}" "root@${host}" "\n', 1)[1].split('\n    " || fail', 1)[0]
        for residue in ("none", "mapper", "alias", "units", "key", "kubelet", "fsid", "dm-error", "units-error", "find-error"):
            with self.subTest(residue=residue):
                script = f"""
set -euo pipefail
fsid=11111111-2222-3333-4444-555555555555
host=192.168.122.135
CEPH_HOSTS=(192.168.122.135)
SSH=(run_remote)
run_remote() {{
  test() {{
    case "$*" in
      '-f /root/.ssh/authorized_keys'|'-d /var/lib/ceph') return 0 ;;
      '-L /dev/mapper/kubeauto-ceph-'*) [[ '{residue}' == alias ]] ;;
      *) builtin test "$@" ;;
    esac
  }}
  systemctl() {{
    if [[ "$1" == list-unit-files ]]; then
      [[ '{residue}' != units-error ]] || return 5
      [[ '{residue}' != units ]] || echo ceph-$fsid.target
      return 0
    fi
    [[ '{residue}' == kubelet ]]
  }}
  dmsetup() {{
    [[ '{residue}' != dm-error ]] || return 5
    [[ '{residue}' != mapper ]] || echo 'kubeauto-ceph-slow-data0 (253:3)'
    return 0
  }}
  grep() {{
    if [[ "$*" == *authorized_keys* ]]; then [[ '{residue}' == key ]]; else command grep "$@"; fi
  }}
  find() {{
    [[ '{residue}' != find-error ]] || return 5
    [[ '{residue}' != fsid ]] || echo /var/lib/ceph/$fsid
    return 0
  }}
  eval "$2"
}}
"${{SSH[@]}}" "root@${{host}}" "
{body}
"
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, residue == "none", result.stderr)

    def test_cleanup_verify_rejects_storage_signatures(self) -> None:
        body = self.cleanup.split('        if blkid', 1)[1].split('\n      " </dev/null || fail', 1)[0]
        body = '        if blkid' + body
        for residue in ("none", "signature", "pv", "pvs-error"):
            with self.subTest(residue=residue):
                script = f"""
set -euo pipefail
stable_path=/dev/disk/by-id/fixture
remote_real=/dev/sdb
blkid() {{ [[ '{residue}' == signature ]]; }}
pvs() {{
  [[ '{residue}' != pvs-error ]] || return 5
  [[ '{residue}' != pv ]] || echo /dev/sdb
  return 0
}}
eval "{body}"
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, residue == "none", result.stderr)

    def test_cleanup_owner_preverify_is_bootstrap_only(self) -> None:
        block = re.search(
            r"      if test '\$\{CEPH_CLEAN_PREVERIFY.*?\n      fi", self.cleanup, re.DOTALL
        ).group(0)
        block = block.replace('\\"', '"').replace('\\$(cat', '$(cat')
        for host in ("192.168.122.135", "192.168.122.40"):
            with self.subTest(host=host):
                script = f"""
set -euo pipefail
CEPH_CLEAN_PREVERIFY=1
CEPH_HOSTS=(192.168.122.135)
host={host}
fsid=11111111-2222-3333-4444-555555555555
cat() {{ test "$host" = "${{CEPH_HOSTS[0]}}" && echo "$fsid"; }}
{block}
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_probe_parser_accepts_only_three_whole_unused_disks(self) -> None:
        preamble = [
            "HOST_META|ceph-01|rocky|9.8|8|16777216",
            "ROOT_DEVICE|/dev/vda2",
            "ROOT_DEVICE|/dev/vda",
        ]
        devices = [
            {
                "name": "vda", "path": "/dev/vda", "type": "disk", "size": 100,
                "ro": False, "mountpoints": [None], "fstype": None,
                "children": [{"name": "vda2", "path": "/dev/vda2", "type": "part", "mountpoints": ["/"], "fstype": "xfs"}],
            }
        ]
        for number in range(1, 4):
            path = f"/dev/vd{chr(ord('a') + number)}"
            stable = f"/dev/disk/by-id/wwn-0x5000{number}"
            preamble.append(f"BYID|{path}|{stable}")
            devices.append({
                "name": path.removeprefix("/dev/"), "path": path, "type": "disk",
                "size": 10_000, "ro": False, "mountpoints": [None], "fstype": None,
                "serial": f"SERIAL{number}", "wwn": f"0x5000{number}", "children": None,
            })
        raw = "\n".join([*preamble, json.dumps({"blockdevices": devices})])
        rows = CEPH_CONTRACT.qualify_host_probe(raw, "192.168.122.135", "ceph-01", 3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("/dev/disk/by-id/wwn-" in row for row in rows))

        devices[-1]["children"] = [{"name": "vdd1", "type": "part", "mountpoints": [None], "fstype": None}]
        dirty = "\n".join([*preamble, json.dumps({"blockdevices": devices})])
        with self.assertRaisesRegex(ValueError, "expected 3 unused data disks, found 2"):
            CEPH_CONTRACT.qualify_host_probe(dirty, "192.168.122.135", "ceph-01", 3)

    def test_host_probe_accepts_pci_identity_only_in_fixed_lab_mode(self) -> None:
        preamble = [
            "HOST_META|ceph-01|rocky|9.8|8|16777216",
            "ROOT_DEVICE|/dev/vda",
        ]
        devices = [{
            "name": "vda", "path": "/dev/vda", "type": "disk", "size": 100,
            "ro": False, "mountpoints": [None], "fstype": None,
        }]
        for number, letter in enumerate(("b", "c", "d"), start=6):
            path_id = f"pci-0000:0{number}:00.0"
            preamble.append(f"BYPATH|/dev/vd{letter}|/dev/disk/by-path/{path_id}|{path_id}")
            devices.append({
                "name": f"vd{letter}", "path": f"/dev/vd{letter}", "type": "disk",
                "size": 10_000, "ro": False, "mountpoints": [None],
                "fstype": None, "serial": None, "wwn": None, "children": None,
            })
        raw = "\n".join([*preamble, json.dumps({"blockdevices": devices})])
        with self.assertRaisesRegex(ValueError, "lacks a safe serial/WWN"):
            CEPH_CONTRACT.qualify_host_probe(raw, "192.168.122.135", "ceph-01", 3)
        rows = CEPH_CONTRACT.qualify_host_probe(
            raw, "192.168.122.135", "ceph-01", 3, allow_test_paths=True
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all("|/dev/disk/by-path/pci-" in row and "|path:pci-" in row for row in rows))

    def test_probe_does_not_require_runtime_before_product_prepare(self) -> None:
        probe = (ROOT / "tests/helpers/ceph-host-probe.sh").read_text()
        prepare = (ROOT / "roles/ceph/tasks/prepare.yml").read_text()
        self.assertIn('command -v lvm >/dev/null', probe)
        self.assertNotIn('command -v podman >/dev/null || command -v docker >/dev/null', probe)
        self.assertIn('host-prerequisites.yml', prepare)

    def test_ceph_volume_device_to_osd_parser_is_exact(self) -> None:
        fixture = {
            "7": [{"devices": ["/dev/mapper/kubeauto-ceph-slow-data0"], "tags": {"ceph.cluster_fsid": "fixture"}}],
            "8": [{"devices": ["/dev/sdc"], "tags": {"ceph.db_device": "/dev/mapper/kubeauto-ceph-slow-db0"}}],
            "9": [{"devices": ["/dev/sdd"], "tags": {"ceph.db_device": "/dev/mapper/kubeauto-ceph-slow-db0"}}],
        }
        self.assertEqual(
            CEPH_CONTRACT.osd_ids_for_device(fixture, "/dev/mapper/kubeauto-ceph-slow-data0"),
            ["7"],
        )
        self.assertEqual(
            CEPH_CONTRACT.osd_ids_for_device(fixture, "/dev/mapper/kubeauto-ceph-slow-db0"),
            ["8", "9"],
        )
        self.assertEqual(CEPH_CONTRACT.osd_ids_for_device(fixture, "/dev/mapper/foreign"), [])

    def test_native_osd_fault_target_requires_fsid_type_and_disk_identity(self) -> None:
        path = "/dev/ceph-01234567-89ab-cdef-0123-456789abcdef/osd-block-01234567-89ab-cdef-0123-456789abcdef"
        entry = {"type": "block", "lv_path": path, "devices": ["/dev/vdb"], "tags": {"ceph.cluster_fsid": "owned-fsid"}}
        self.assertEqual(CEPH_CONTRACT.osd_lv_for_device({"7": [entry]}, "7", "block", "owned-fsid", "/dev/vdb"), path)
        for fsid, device, kind in (("foreign", "/dev/vdb", "block"), ("owned-fsid", "/dev/vda", "block"), ("owned-fsid", "/dev/vdb", "db")):
            with self.subTest(fsid=fsid, device=device, kind=kind), self.assertRaises(ValueError):
                CEPH_CONTRACT.osd_lv_for_device({"7": [entry]}, "7", kind, fsid, device)
        self.assertNotIn("dmsetup create", self.regression)
        self.assertIn("ln -s '$backing'", self.regression)
        self.assertIn("cephadm ceph-volume -- lvm list", self.regression)

    def test_disk_alias_fixture_refuses_foreign_existing_paths(self) -> None:
        for state in ("new", "owned", "foreign", "not-symlink"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
MAPPER_FILE='{directory}/aliases'
ssh_node() {{
  test() {{
    case "$1" in
      -b) return 0 ;;
      -e) [[ '{state}' != new ]] ;;
      -L) [[ '{state}' == owned || '{state}' == foreign ]] ;;
      *) builtin test "$@" ;;
    esac
  }}
  readlink() {{ if [[ '{state}' == foreign ]]; then echo /dev/foreign; else echo /dev/disk/by-path/pci-fixture; fi; }}
  ln() {{ echo ALIAS_CREATED; }}
  eval "$2"
}}
create_mapper 192.168.122.135 kubeauto-ceph-slow-data0 /dev/disk/by-path/pci-fixture
"""
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, state in {"new", "owned"}, result.stdout + result.stderr)
                manifest = Path(directory) / "aliases"
                self.assertEqual(manifest.exists(), state in {"new", "owned"})
                self.assertEqual("ALIAS_CREATED" in result.stdout, state == "new")

    def test_native_delay_table_preserves_sector_range_backing_and_offset(self) -> None:
        original = "0 209707008 linear 252:16 8192"
        self.assertEqual(CEPH_CONTRACT.delayed_linear_table(original, 3000), "0 209707008 delay 252:16 8192 3000 252:16 8192 3000")
        for table in ("", "0 100 error", "0 0 linear 252:16 0", "0 100 linear /dev/vda 0", "0 100 delay 252:16 0 1"):
            with self.subTest(table=table), self.assertRaises(ValueError):
                CEPH_CONTRACT.delayed_linear_table(table, 3000)
        with self.assertRaises(ValueError):
            CEPH_CONTRACT.delayed_linear_table(original, 0)
        self.assertIn('linear) table="$original"', self.regression)
        self.assertIn("trap 'dmsetup resume $native' EXIT", self.regression)

    def test_ceph_versions_parser_rejects_mixed_daemon_versions(self) -> None:
        version = "ceph version 20.2.4 (7f793731f1b39eb4f465e960113d2363c311b964) tentacle (stable)"
        CEPH_CONTRACT.assert_ceph_versions({"mon": {version: 3}, "osd": {version: 12}, "overall": {version: 15}}, "20.2.4")
        with self.assertRaisesRegex(ValueError, "unexpected daemon version"):
            CEPH_CONTRACT.assert_ceph_versions(
                {"mon": {version: 3}, "osd": {"ceph version 20.2.3 old tentacle (stable)": 1}, "overall": {version: 3}},
                "20.2.4",
            )

    def test_pg_clean_parser_rejects_partial_or_incomplete_recovery(self) -> None:
        CEPH_CONTRACT.assert_clean_pgs({
            "pg_ready": True,
            "pg_summary": {"num_pgs": 128, "num_pg_by_state": [{"name": "active+clean", "num": 128}]},
        })
        for other_state in ("peering", "inactive", "stale", "active+clean+remapped", "active+clean+scrubbing", "active+undersized+degraded"):
            with self.subTest(state=other_state), self.assertRaisesRegex(ValueError, "not fully recovered"):
                CEPH_CONTRACT.assert_clean_pgs({
                    "pg_ready": True,
                    "pg_summary": {
                        "num_pgs": 128,
                        "num_pg_by_state": [{"name": "active+clean", "num": 127}, {"name": other_state, "num": 1}],
                    },
                })
        for invalid in (
            {}, [],
            {"pg_ready": True},
            {"pg_ready": True, "pg_summary": None},
            {"pg_ready": True, "pg_summary": []},
            {"pg_ready": True, "num_pgs": 128, "num_pg_by_state": [{"name": "active+clean", "num": 128}]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_clean_pgs(invalid)
        for invalid_summary in (
            {}, {"num_pgs": 0, "num_pg_by_state": []},
            {"num_pgs": 128, "num_pg_by_state": [{"name": "active+clean", "num": 127}]},
            {"num_pgs": True, "num_pg_by_state": [{"name": "active+clean", "num": 1}]},
            {"num_pgs": 1, "num_pg_by_state": [None]},
            {"num_pgs": 1, "num_pg_by_state": [{"name": "active+clean", "num": True}]},
            {"num_pgs": 1, "num_pg_by_state": [{"name": "active+clean", "num": 0}]},
        ):
            with self.subTest(summary=invalid_summary), self.assertRaises(ValueError):
                CEPH_CONTRACT.assert_clean_pgs({"pg_ready": True, "pg_summary": invalid_summary})
        wait = self.regression.split("wait_clean() {", 1)[1].split("verify_rados_data()", 1)[0]
        self.assertIn("ceph pg stat --format json", wait)
        self.assertIn('"$CONTRACT_SCRIPT" pg-clean', wait)

    def test_pg_clean_cli_reads_official_summary_and_rejects_unready_mgr(self) -> None:
        # Actual v20.2.4 CLI shape, including PGMapDigest's capacity fields.
        fixture = {
            "pg_ready": True,
            "pg_summary": {
                "num_pg_by_state": [{"name": "active+clean", "num": 689}],
                "num_pgs": 689, "num_bytes": 472471,
                "total_bytes": 1932634619904, "total_avail_bytes": 1288387756032,
                "total_used_bytes": 644246863872, "total_used_raw_bytes": 644246863872,
            },
        }
        for ready in (True, False, None, 1, "true"):
            with self.subTest(pg_ready=ready):
                result = subprocess.run(
                    [sys.executable, str(CONTRACT_PATH), "pg-clean"],
                    input=json.dumps({**fixture, "pg_ready": ready}),
                    text=True, capture_output=True,
                )
                if ready is True:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "CEPH_PG_CLEAN_JSON_PASS\n")
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("CEPH_CONTRACT_FAIL reason=PG map is not ready", result.stderr)

    def test_upgrade_asserts_exact_source_before_changing_the_cluster(self) -> None:
        upgrade = self.regression.split("run_upgrade() {", 1)[1].split("mapper_table()", 1)[0]
        self.assertLess(upgrade.index("versions --expected 20.2.3"), upgrade.index("ceph osd set noout"))
        self.assertIn("versions --expected 20.2.4", upgrade)

    def test_cleanup_refuses_unknown_owner_and_unstable_disk(self) -> None:
        cleanup = ROOT / "tests/helpers/ceph-cleanup.sh"
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "owner").write_text("foreign-owner\n")
            result = subprocess.run(
                ["bash", str(cleanup)],
                env={**os.environ, "CEPH_STATE_DIR": directory, "CEPH_SSH_BIN": "/bin/false"},
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("owner marker mismatch", result.stderr)

            (state / "owner").write_text("kubeauto-ceph-regression\n")
            (state / "disk-allowlist").write_text(
                "192.168.122.135|/dev/sdb|/dev/sdb|SERIAL|WWN|WWN|1000\n"
            )
            result = subprocess.run(
                ["bash", str(cleanup)],
                env={**os.environ, "CEPH_STATE_DIR": directory, "CEPH_SSH_BIN": "/bin/false"},
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unstable allowlist path", result.stderr)

    def test_cleanup_does_not_consume_allowlist_stdin_over_ssh(self) -> None:
        cleanup = (ROOT / "tests/helpers/ceph-cleanup.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(cleanup.count('" </dev/null)'), 2)
        self.assertIn("wc -l < /var/lib/kubeauto-ceph-test/disk-allowlist", self.runner)

    def test_fault_mapper_creation_cannot_lose_rows_to_ssh_stdin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kubeauto-ceph-mappers-") as temporary:
            state = Path(temporary)
            allowlist = state / "disk-allowlist"
            hosts = ["135", "40", "72", "212", "165", "238"]
            rows = [f"192.168.122.{host}|/dev/sd{disk}|/dev/disk/by-id/SERIAL{disk}|SERIAL{disk}||SERIAL{disk}|1000" for host in hosts for disk in "bcd"]
            allowlist.write_text("\n".join(rows) + "\n")
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
STATE_DIR='{state}'
ALLOWLIST='{allowlist}'
OWNER_FILE='{state}/owner'
MAPPER_FILE='{state}/mappers'
ssh_node() {{ cat >/dev/null; printf '1000\\n'; }}
prepare_fault_mappers
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            mappings = [row.split("|") for row in (state / "mappers").read_text().splitlines()]
            self.assertEqual(len(mappings), 18)
            self.assertEqual(len({(host, path) for host, _, path in mappings}), 18)
            self.assertEqual({(host, path) for host, _, path in mappings}, {(row.split("|")[0], row.split("|")[2]) for row in rows})
        mapper_cleanup = self.cleanup.split('if [[ -s "$MAPPER_FILE" ]]', 1)[1].split('for host in "${CEPH_HOSTS[@]}"', 1)[0]
        self.assertIn('" </dev/null || fail "failed to remove mapper', mapper_cleanup)

    def test_s3_client_reuses_existing_minio_mc_publication(self) -> None:
        tag = "RELEASE.2025-04-08T15-39-49Z"
        image = "brinnatt/minio-mc"
        catalog = PROJECTS / "kubeauto-ext-images-dockerfile"
        workflow = yaml.safe_load((catalog / ".github/workflows/build.yml").read_text())
        entries = workflow["jobs"]["docker"]["strategy"]["matrix"]["include"]
        publications = [entry for entry in entries if entry["image"] == image]
        self.assertEqual(len(publications), 1)
        publication = publications[0]
        self.assertEqual(publication["tag"], tag)
        self.assertEqual(publication["talkedu_image"], "hub.talkedu.cn/kubeauto/minio-mc")
        self.assertIn(f"{image}:{tag}", self.constants.component_images["ceph"])
        self.assertIn(f"'minio-mc|{tag}'", self.regression)
        self.assertIn(f"image: registry.talkschool.cn:5000/{image}:{tag}", self.regression)
        self.assertNotIn("ceph-s3-client", self.regression)
        self.assertNotRegex(self.regression, r"\bcmp\b")
        self.assertFalse((catalog / "middleware/ceph/s3-client/Dockerfile").exists())

    def test_s3_hash_check_works_without_cmp_and_rejects_corrupt_readback(self) -> None:
        script = self.regression.split("<<'S3_DATA_PATH'\n", 2)[2].split("\nS3_DATA_PATH", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = script.replace("/tmp/marker", f"{directory}/marker").replace(
                "/tmp/readback", f"{directory}/readback"
            )
            (Path(directory) / "marker").write_text("kubeauto-ceph-s3-v1")
            mc = Path(directory) / "mc"
            mc.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$1" >>"$MOCK_MC_CALLS"\n'
                'case "$1:$2" in\n'
                'cp:ceph/kubeauto-ceph-test/marker) printf "%s" "$MOCK_READBACK" >"$3" ;;\n'
                'stat:ceph/kubeauto-ceph-test/delete-check) exit 1 ;;\n'
                'esac\n'
            )
            mc.chmod(0o755)
            for tool in ("sh", "sha256sum"):
                resolved = shutil.which(tool)
                self.assertIsNotNone(resolved)
                (Path(directory) / tool).symlink_to(resolved)
            for readback, success in (("kubeauto-ceph-s3-v1", True), ("corrupt", False)):
                calls = Path(directory) / "calls"
                calls.write_text("")
                result = subprocess.run(
                    [str(Path(directory) / "sh"), "-seu"], input=script,
                    text=True, capture_output=True,
                    env={**os.environ, "PATH": directory, "MOCK_READBACK": readback, "MOCK_MC_CALLS": str(calls)},
                )
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertIn("cp\n", calls.read_text())

    def test_remote_ceph_python_crashes_have_tracebacks_without_changing_exit_policy(self) -> None:
        assignment = next(line for line in self.runner.splitlines()
                          if line.startswith("printf -v ceph_artifact_env "))
        script = assignment + f"\nenv $ceph_artifact_env '{sys.executable}' -c 'import faulthandler; print(faulthandler.is_enabled())'"
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                env={**os.environ, "CEPH_ARTIFACT_MODE": "manual-talkedu"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")
        self.assertIn("CEPH_ARTIFACT_MODE=%q", assignment)
        self.assertIn("PYTHONFAULTHANDLER=1", assignment)

    def test_supply_chain_modes_fail_closed_and_report_actual_publication(self) -> None:
        digest = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "extra-bin").mkdir()
            artifact = base / "extra-bin/cephadm"
            artifact.write_text('print("cephadm version 20.2.4 (fixture) tentacle (stable)")\n')
            checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
            cases = (
                ("manual-talkedu", "ok", True), ("dual", "ok", True),
                ("unknown", "ok", False), ("manual-talkedu", "missing", False),
                ("manual-talkedu", "bad-digest", False), ("dual", "mismatch", False),
                ("dual", "missing-pack", False), ("manual-talkedu", "bad-sha", False),
            )
            for mode, fault, success in cases:
                with self.subTest(mode=mode, fault=fault):
                    script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
BASE={directory}
ARTIFACT_MODE={mode}
CEPHADM_SHA256={'0' * 64 if fault == 'bad-sha' else checksum}
skopeo() {{ :; }}
manifest_digest() {{
  printf '%s\\n' "$1" >>'{directory}/probes'
  case "{fault}:$1" in
    missing:*) return 1 ;;
    bad-digest:*) echo invalid; return ;;
    mismatch:docker.io/*) echo sha256:{'b' * 64}; return ;;
    missing-pack:*kubeauto-ext-bin*) return 1 ;;
  esac
  echo {digest}
}}
verify_supply_chain
"""
                    probes = base / "probes"
                    probes.write_text("")
                    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
                    self.assertEqual(result.returncode == 0, success, result.stderr)
                    self.assertEqual("CEPH_SUPPLY_CHAIN_PASS" in result.stdout, success)
                    if success:
                        self.assertIn(f"CEPH_SUPPLY_CHAIN_PASS mode={mode}", result.stdout)
                        image_count = len(self.constants.component_images["ceph"])
                        self.assertEqual(len(probes.read_text().splitlines()), image_count if mode == "manual-talkedu" else image_count * 2 + 2)
                    if mode == "manual-talkedu":
                        self.assertNotIn("docker.io/", probes.read_text())
                        self.assertNotIn("CEPH_EXT_BIN_MANIFEST_PASS", result.stdout)

    def test_corrupt_cephadm_is_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "extra-bin").mkdir()
            (base / "extra-bin/cephadm").write_text(
                f'open("{directory}/executed", "w").close()\n'
            )
            script = f"""
source <(sed '$d' '{ROOT / 'tests/helpers/ceph-regression.sh'}')
BASE={directory}
ARTIFACT_MODE=manual-talkedu
skopeo() {{ :; }}
verify_supply_chain
"""
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256 mismatch", result.stderr)
            self.assertFalse((base / "executed").exists())

    def test_cephadm_installation_and_runtime_overrides_are_checksum_gated(self) -> None:
        prepare = yaml.safe_load((ROOT / "roles/ceph/tasks/cephadm.yml").read_text())
        checksum = "5b78c8d5772ef7c5c8619dac6ee0b36716b829338ea7a11c9f2b896626ab354f"
        check_index = next(i for i, task in enumerate(prepare) if checksum in str(task))
        copy_index = next(i for i, task in enumerate(prepare) if task.get("ansible.builtin.copy", {}).get("dest") == "/usr/local/libexec/kubeauto/cephadm")
        self.assertLess(check_index, copy_index)
        self.assertIn(checksum, self.regression)
        self.assertIn(checksum, self.runner)
        self.assertIn("--ceph-supply-chain-only", self.runner)
        self.assertIn("printf -v ceph_artifact_env", self.runner)
        self.assertEqual(self.runner.count("nohup env $ceph_artifact_env"), 2)
        self.assertEqual(self.runner.count('KUBEAUTO_SSH_JUMP="$CEPH_TEST_JUMPER" KUBEAUTO_SYNC_SKIP_CONTROL_SETUP=0'), 2)
        self.assertIn('"$playbook" --syntax-check -e @conf/config.yml', self.runner)

    def test_cephadm_staging_rejects_corruption_before_any_remote_command(self) -> None:
        function = "stage_cephadm_artifact() {" + self.runner.split(
            "stage_cephadm_artifact() {", 1
        )[1].split("\n}\n", 1)[0] + "\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "cephadm"
            artifact.write_text("corrupt")
            script = function + f"""
CEPHADM_FILE='{artifact}'
ssh_ceph() {{ touch '{directory}/remote'; }}
stage_cephadm_artifact
"""
            result = subprocess.run(["bash", "-euc", script], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing transfer", result.stderr)
            self.assertFalse((Path(directory) / "remote").exists())

    def test_sync_pip_override_is_runtime_only_and_shell_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            injected = base / "injected"
            override = f"https://example.invalid/simple/'; touch {injected}; #"
            ssh = base / "ssh"
            ssh.write_text(
                '#!/bin/bash\nlast="${@: -1}"\n'
                'case "$last" in\n'
                'env*) command="${last%bash -s}env"; '
                'bash -c "$command" | sed -n "s/^PIP_INDEX_URL=//p" ;;\n'
                'esac\n'
            )
            ssh.chmod(0o755)
            rsync = base / "rsync"
            rsync.write_text("#!/bin/sh\nexit 0\n")
            rsync.chmod(0o755)
            result = subprocess.run(
                ["bash", str(ROOT / "tests/helpers/sync-kubeauto.sh"), "root@fixture"],
                input="", text=True, capture_output=True,
                env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                     "KUBEAUTO_SYNC_PIP_INDEX_URL": override},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(override, result.stdout)
            self.assertFalse(injected.exists())

    def test_six_repository_supply_chain_is_registered(self) -> None:
        ext_bin = (PROJECTS / "kubeauto-ext-bin-dockerfile/Dockerfile").read_text()
        self.assertIn(f"EXT_BIN_VER={self.constants.v_extra_bin}", ext_bin)
        self.assertEqual(self.constants.v_cephadm_sha256, "5b78c8d5772ef7c5c8619dac6ee0b36716b829338ea7a11c9f2b896626ab354f")
        self.assertIn(f"CEPHADM_VER={self.constants.v_ceph}", ext_bin)
        self.assertIn(f"CEPHADM_SHA256={self.constants.v_cephadm_sha256}", ext_bin)
        ext_workflow = (PROJECTS / "kubeauto-ext-bin-dockerfile/.github/workflows/build.yml").read_text()
        self.assertIn("kubeauto/kubeauto-ext-bin", ext_workflow)
        self.assertIn("brinnatt/kubeauto-ext-bin", ext_workflow)
        self.assertIn("linux/amd64,linux/arm64", ext_workflow)
        for registry in ("hub.talkedu.cn/kubeauto", "docker.io/brinnatt"):
            self.assertIn(f"{registry}/kubeauto-ext-bin:{self.constants.v_extra_bin}", self.regression)
        workflow = (PROJECTS / "kubeauto-ext-images-dockerfile/.github/workflows/build.yml").read_text()
        for context in (
            "middleware/ceph/ceph", "middleware/ceph/ceph-csi",
            "middleware/ceph/ceph-csi-provisioner", "middleware/ceph/ceph-csi-attacher",
            "middleware/ceph/ceph-csi-resizer", "middleware/ceph/ceph-csi-snapshotter",
            "middleware/ceph/ceph-csi-node-driver-registrar", "middleware/ceph/ceph-prometheus",
            "middleware/ceph/ceph-alertmanager", "middleware/ceph/ceph-node-exporter",
            "middleware/ceph/ceph-grafana",
        ):
            self.assertIn(context, workflow)


if __name__ == "__main__":
    unittest.main()
