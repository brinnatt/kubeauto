import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
UNIT_RUNNER = (ROOT / "tests" / "run_unit_tests.sh").read_text()
LAB_WIPE = (ROOT / "tests" / "helpers" / "lab-wipe-nodes.sh").read_text()
AIO_PREP = (ROOT / "tests" / "helpers" / "regression-aio-prep-full.sh").read_text()
GAPS_FULLCHAIN = (
    ROOT / "tests" / "helpers" / "delivery-gaps-fullchain.sh"
).read_text()
HANDBOOK = (ROOT / "tests" / "README.md").read_text()
GITIGNORE = (ROOT / ".gitignore").read_text()
SOURCE_SYNC = (ROOT / "tests" / "helpers" / "sync-kubeauto.sh").read_text()
CONTROL_SSH_BOOTSTRAP = (
    ROOT / "tests" / "helpers" / "lab-control-ssh-bootstrap.sh"
).read_text()
PRODUCTION_SMOKE = (
    ROOT / "tests" / "helpers" / "kubernetes-production-smoke.sh"
).read_text()
ANOLIS_CONTAINER_GATE = (
    ROOT / "tests" / "helpers" / "ansible-anolis-container-gate.sh"
).read_text()
ROCKY8_TOOLS_BUILD = (
    ROOT / "tests" / "helpers" / "build-tools-rocky8.sh"
).read_text()
TIER3_TOOLS_GATE = (
    ROOT / "tests" / "helpers" / "tier3-tools-gate.sh"
).read_text()
REGISTRY_REBOOT_GATE = (
    ROOT / "tests" / "helpers" / "registry-reboot-gate.sh"
).read_text()
BUILD_ENTRYPOINT = (ROOT / "build.py").read_text()


class TestDeliveryProcessContract(unittest.TestCase):
    def test_unit_tests_do_not_dirty_the_tracked_deploy_log(self):
        self.assertIn("mktemp -d /tmp/kubeauto-unit-logs.", UNIT_RUNNER)
        self.assertIn('export KUBEAUTO_LOG_DIR="$UNIT_LOG_DIR"', UNIT_RUNNER)
        self.assertIn("trap 'rm -rf", UNIT_RUNNER)

    def test_runtime_logs_are_ignored_but_policy_is_tracked(self):
        for pattern in ("logs/*.log", "logs/*.out", "logs/*.pid"):
            self.assertIn(pattern, GITIGNORE)
        self.assertIn("!logs/README.md", GITIGNORE)

    def test_supervisor_contract_is_visible_and_durable(self):
        for required in (
            "event=attached interval=10s heartbeat=30s silence_diagnostic=60s",
            "event=log-stream-disconnected action=reattach",
            "event=no-new-log-for-",
            "failure_markers=0",
        ):
            self.assertIn(required, RUNNER)
        self.assertIn('lab-wipe-nodes.sh" --verify', RUNNER)
        self.assertIn("LAB_CLEAN_VERIFY_PASS", LAB_WIPE)

    def test_status_reports_rocky8_customer_binary_build(self):
        status = RUNNER.split('if [[ "$MODE" == "--status" ]]', 1)[1].split(
            'if [[ "$MODE" == "--ansible-anolis-probe" ]]', 1
        )[0]
        self.assertIn("138 Rocky 8 customer-binary build", status)
        self.assertIn("/tmp/kubeauto-rocky8-build-gate", status)
        self.assertIn("/tmp/kubeauto-rocky8-build-live.log", status)
        for label in (
            "debian-128",
            "rocky-130",
            "anolis-141",
            "openeuler-142",
            "opensuse-143",
        ):
            self.assertIn(label, status)
        self.assertIn("/tmp/kubeauto-ansible-native-gate", status)

    def test_rocky8_build_collects_failure_evidence_before_cleanup(self):
        build = RUNNER.split('if [[ "$MODE" == "--build-rocky8-kubecli" ]]', 1)[1]
        evidence = build.index("ROCKY8_BUILD_FAILURE_EVIDENCE")
        cleanup = build.index(
            'ssh138 "sudo rm -rf /tmp/kubeauto-rocky8-build-source', evidence
        )
        self.assertIn("remote_log_tail ssh138 sudo", build)
        self.assertIn('| tee -a "$LOG"', build)
        self.assertLess(evidence, cleanup)

    def test_matrix_progress_counts_only_test_items(self):
        self.assertIn(
            "^[[:space:]]*-[[:space:]]*\\{id:.*status: pass",
            RUNNER,
        )
        self.assertIn(
            "^[[:space:]]*-[[:space:]]*\\{id:.*status: pending",
            RUNNER,
        )

    def test_delivery_runner_validates_matrix_before_gates(self):
        validator = "tests/helpers/validate-test-matrix.py"
        self.assertIn(validator, RUNNER)
        self.assertLess(
            RUNNER.index(validator), RUNNER.index('if [[ "$MODE" == "--mysql-only" ]]')
        )

    def test_all_delivery_mode_composes_every_signoff_gate(self):
        start = RUNNER.index('if [[ "$MODE" == "--all-delivery" ]]')
        end = RUNNER.index('if [[ "$MODE" == "--status" ]]')
        all_delivery = RUNNER[start:end]
        expected_modes = (
            "--registry-reboot-only",
            "--jumper-only",
            "--nerdctl-only",
            "--docker-only",
            "--upgrade-only",
            "--gaps-only",
            "--build-rocky8-kubecli",
            "--ansible-os-probe",
            "--ansible-os-only",
            "--ansible-ee-debian-probe",
            "--ansible-anolis-container-probe",
            "--tier3-tools-only",
        )
        positions = [all_delivery.index(mode) for mode in expected_modes]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('bash "$0" "$delivery_mode"', all_delivery)
        self.assertIn("ENTERPRISE_DELIVERY_ALL_PASS", all_delivery)

    def test_all_delivery_daemon_keeps_local_durable_state(self):
        daemon = RUNNER.split(
            'if [[ "$MODE" == "--all-delivery-daemon" ]]', 1
        )[1].split('if [[ "$MODE" == "--all-delivery" ]]', 1)[0]
        self.assertIn("/tmp/kubeauto-all-delivery", daemon)
        self.assertIn("setsid nohup", daemon)
        self.assertIn("run-durable-gate.sh", daemon)
        self.assertIn("ENTERPRISE_DELIVERY_ALL_EXIT", daemon)
        self.assertIn('bash "$0" --all-delivery', daemon)

    def test_delivery_progress_is_compact_and_has_a_fixed_follow_interval(self):
        progress = RUNNER.split('if [[ "$MODE" == "--progress" ]]', 1)[1].split(
            'if [[ "$MODE" == "--follow-delivery" ]]', 1
        )[0]
        self.assertIn("delivery progress", progress)
        self.assertIn("remote_job_summary", progress)
        self.assertIn("matrix_counts", progress)
        follow = RUNNER.split('if [[ "$MODE" == "--follow-delivery" ]]', 1)[1]
        self.assertIn('bash "$0" --progress', follow)
        self.assertIn("sleep 30", follow)

    def test_registry_reboot_gate_requires_docker_owned_recovery_and_persistent_data(self):
        for required in (
            "RestartPolicy.Name",
            "REGISTRY_REBOOT_PREP_PASS",
            "REGISTRY_HOST_REBOOT_PASS",
            "Docker must restore the container itself",
            "registry fixture changed across reboot",
        ):
            self.assertIn(required, REGISTRY_REBOOT_GATE)
        self.assertIn('if [[ "$MODE" == "--registry-reboot-only" ]]', RUNNER)
        self.assertIn('ssh138 "sudo systemctl reboot"', RUNNER)

    def test_anolis_gate_preserves_terminal_evidence_before_remote_cleanup(self):
        gate = RUNNER.split(
            'if [[ "$MODE" == "--ansible-anolis-container-probe" ]]', 1
        )[1].split('if [[ "$MODE" == "--ansible-os-only" ]]', 1)[0]
        evidence = gate.index("remote_log_tail ssh141 ''")
        cleanup = gate.index('ssh141 "rm -f /tmp/kubecli-anolis-container-gate', evidence)
        self.assertIn('| tee -a "$LOG"', gate)
        self.assertLess(evidence, cleanup)

    def test_clean_aio_preflight_stages_harbor_before_setup_11(self):
        self.assertIn("kubecli download -R </dev/null", AIO_PREP)
        self.assertLess(
            AIO_PREP.index("kubecli download -R </dev/null"),
            AIO_PREP.index("bash /tmp/regression-full.sh"),
        )

    def test_gaps_preflight_stages_harbor_before_authoritative_topology(self):
        self.assertIn("kubecli download -R </dev/null", GAPS_FULLCHAIN)
        self.assertLess(
            GAPS_FULLCHAIN.index("kubecli download -R </dev/null"),
            GAPS_FULLCHAIN.index("regression-full.sh"),
        )

    def test_lab_authority_and_reserved_roles_are_documented(self):
        for address in (
            "192.168.47.128",
            "192.168.47.129",
            "192.168.47.130",
            "192.168.47.131-133",
            "192.168.47.134-136",
            "192.168.47.137",
            "192.168.47.138",
            "192.168.47.141",
            "192.168.47.142",
            "192.168.47.143",
        ):
            self.assertIn(address, HANDBOOK)
        self.assertIn("reserved disabled", HANDBOOK)

    def test_active_test_code_has_no_retired_lab_address(self):
        allowed_paths = {
            ROOT / "tests" / "README.md",
            ROOT / "tests" / "unit" / "test_delivery_process_contract.py",
            ROOT / "tests" / "unit" / "test_registry_ready.py",
        }
        offenders = []
        for path in (ROOT / "tests").rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path in allowed_paths:
                continue
            text = path.read_text(errors="ignore")
            for address in ("192.168.47.140", "192.168.47.147"):
                if address in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {address}")
        self.assertEqual([], offenders)

    def test_cleanup_script_uses_current_authority(self):
        self.assertIn("JUMPER=192.168.47.130", LAB_WIPE)
        self.assertIn("DEBIAN_IP=192.168.47.128", LAB_WIPE)
        self.assertIn("CONTROL=192.168.47.138", LAB_WIPE)
        self.assertIn("192.168.47.137", LAB_WIPE)

    def test_snapshot_key_bootstrap_keeps_password_runtime_only(self):
        helper = (ROOT / "tests" / "helpers" / "lab-ssh-bootstrap.sh").read_text()
        self.assertIn("LAB_SSH_PASSWORD", helper)
        self.assertIn("sshpass -e ssh", helper)
        self.assertIn("UserKnownHostsFile=/dev/null", helper)
        self.assertIn("authorized_keys", helper)
        self.assertIn("BatchMode=yes", helper)
        self.assertIn('read -r -s -p "LAB_SSH_PASSWORD: "', helper)
        self.assertNotIn("123456", helper)

    def test_control_to_node_bootstrap_is_key_only_and_runner_owned(self):
        for required in (
            "id_ed25519",
            "authorized_keys",
            "BatchMode=yes",
            "LAB_CONTROL_SSH_BOOTSTRAP_PASS",
        ):
            self.assertIn(required, CONTROL_SSH_BOOTSTRAP)
        self.assertIn("ssh-keygen -lf", CONTROL_SSH_BOOTSTRAP)
        self.assertNotIn("123456", CONTROL_SSH_BOOTSTRAP)
        self.assertNotIn("scp ", CONTROL_SSH_BOOTSTRAP)
        self.assertGreaterEqual(
            RUNNER.count('tests/helpers/lab-control-ssh-bootstrap.sh'), 5
        )

        nerdctl_gate = (
            ROOT / "tests" / "helpers" / "nerdctl-gate.sh"
        ).read_text()
        self.assertIn("control-to-worker key access missing", nerdctl_gate)
        self.assertNotIn("id_rsa.pub", nerdctl_gate)
        self.assertNotIn("--password", nerdctl_gate)

    def test_snapshot_sudo_bootstrap_keeps_password_runtime_only(self):
        helper = (ROOT / "tests" / "helpers" / "lab-sudo-bootstrap.sh").read_text()
        self.assertIn("LAB_SUDO_PASSWORD", helper)
        self.assertIn('read -r -s -p "LAB_SUDO_PASSWORD: "', helper)
        self.assertIn("sudo -S -p ''", helper)
        self.assertIn("sudo -n true", helper)
        self.assertNotIn("123456", helper)

    def test_native_ansible_gate_sync_needs_no_remote_rsync(self):
        start = RUNNER.index('if [[ "$MODE" == "--ansible-os-only" ]]')
        end = RUNNER.index('if [[ "$MODE" == "--build-rocky8-kubecli" ]]')
        native_gate = RUNNER[start:end]
        self.assertIn("scp -r", native_gate)
        self.assertIn('"$ROOT/playbooks" "$ROOT/roles" "$ROOT/conf"', native_gate)
        self.assertNotIn("rsync -a", native_gate)
        self.assertNotIn("tar -C", native_gate)

    def test_native_ansible_syntax_gate_supplies_lifecycle_runtime_vars(self):
        helper = (ROOT / "tests" / "helpers" / "ansible-native-package-gate.sh").read_text()
        self.assertIn("-e NODE_TO_ADD=192.168.1.1", helper)
        self.assertIn("-e NODE_TO_DEL=192.168.1.1", helper)
        self.assertIn("-e CLUSTER=syntax-check", helper)

    def test_native_ansible_gate_rejects_frozen_library_leaks(self):
        helper = (ROOT / "tests" / "helpers" / "ansible-native-package-gate.sh").read_text()
        self.assertIn("lib(tinfo|crypto|ssl)", helper)
        self.assertIn("PYINSTALLER_EXTERNAL_LIB_CLEAN_PASS", helper)

    def test_anolis_container_gate_uses_customer_docker_and_restores_baseline(self):
        self.assertIn('"$KUBECLI" download -d </dev/null', ANOLIS_CONTAINER_GATE)
        self.assertIn("clean_kubeauto_docker", ANOLIS_CONTAINER_GATE)
        self.assertIn("/usr/local/kubeauto/docker-bin/*", ANOLIS_CONTAINER_GATE)
        self.assertIn("/data/docker", ANOLIS_CONTAINER_GATE)
        self.assertNotIn('"$KUBECLI" docker -e', ANOLIS_CONTAINER_GATE)
        self.assertIn("ANSIBLE_ANOLIS_CONTAINER_CLEAN_PASS", ANOLIS_CONTAINER_GATE)
        self.assertIn("ANSIBLE_ANOLIS_CONTAINER_GATE_PASS", ANOLIS_CONTAINER_GATE)
        self.assertIn("monitor_remote_job", RUNNER)

    def test_customer_binary_build_is_pinned_to_oldest_supported_glibc(self):
        helper = (ROOT / "tests" / "helpers" / "build-kubecli-rocky8.sh").read_text()
        self.assertIn("rockylinux/rockylinux:8.10", helper)
        self.assertIn("swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io", helper)
        self.assertIn('glibc 2.28', helper)
        self.assertIn("repo.huaweicloud.com/repository/pypi/simple", helper)
        self.assertIn("ROCKY8_KUBECLI_BUILD_PASS", helper)

    def test_tier3_tools_build_and_real_rocky_gate_are_runner_owned(self):
        expected_tools = (
            "CalicoPolicyCli",
            "NetCheckCli",
            "KafkaCli",
            "MyBackupCli",
            "MigrationCli",
            "StarCli",
            "KubeBackupCli",
            "KubePublishCli",
            "OvpnUserCli",
        )
        for tool in expected_tools:
            self.assertIn(tool, ROCKY8_TOOLS_BUILD)
            self.assertIn(tool, TIER3_TOOLS_GATE)
        self.assertIn("docker.sparkcr.cn/rockylinux/rockylinux:8.10", ROCKY8_TOOLS_BUILD)
        self.assertIn("swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io", ROCKY8_TOOLS_BUILD)
        self.assertLess(
            ROCKY8_TOOLS_BUILD.index("docker.sparkcr.cn/rockylinux/rockylinux:8.10"),
            ROCKY8_TOOLS_BUILD.index("swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io"),
        )
        self.assertIn('glibc 2.28', ROCKY8_TOOLS_BUILD)
        self.assertIn('glibc 2.28', TIER3_TOOLS_GATE)
        self.assertIn("python3 build.py --tools-only", ROCKY8_TOOLS_BUILD)
        self.assertIn("TIER3_TOOLS_GATE_PASS", TIER3_TOOLS_GATE)
        self.assertIn('if [[ "$MODE" == "--tier3-tools-only" ]]', RUNNER)
        self.assertIn("monitor_remote_job", RUNNER)
        self.assertIn("TIER3_SCOPE_CLEAN_PASS", RUNNER)
        self.assertIn("TIER3_TOOLS_DELIVERY_PASS", RUNNER)

    def test_build_entrypoint_can_select_one_release_surface(self):
        self.assertIn('target.add_argument("--kubecli-only"', BUILD_ENTRYPOINT)
        self.assertIn('target.add_argument("--tools-only"', BUILD_ENTRYPOINT)
        self.assertIn("if not args.tools_only", BUILD_ENTRYPOINT)
        self.assertIn("if not args.kubecli_only", BUILD_ENTRYPOINT)

    def test_frozen_build_uses_single_runtime_requirements_source(self):
        requirements = (ROOT / "requirements").read_text()

        self.assertIn('project_root / "requirements"', BUILD_ENTRYPOINT)
        self.assertIn('"-r"', BUILD_ENTRYPOINT)
        self.assertIn("ansible-runner==2.4.2", requirements)
        self.assertIn("paramiko==3.5.1", requirements)
        self.assertNotIn("ansible-runner==", BUILD_ENTRYPOINT)
        self.assertNotIn("paramiko==", BUILD_ENTRYPOINT)

    def test_lab_docker_bootstrap_uses_product_huawei_path_without_global_pip(self):
        helper = (ROOT / "tests" / "helpers" / "lab-docker-bootstrap.sh").read_text()
        self.assertIn("huawei-mirror-debian.sh", helper)
        self.assertIn("repo.huaweicloud.com/repository/pypi/simple", helper)
        self.assertIn("python3 -m venv", helper)
        self.assertIn("kubecli.py\" download -d", helper)
        self.assertIn("groupadd -r docker", helper)
        self.assertIn("/usr/sbin", helper)
        self.assertIn("LAB_DOCKER_BOOTSTRAP_PASS", helper)

    def test_source_sync_isolates_control_dependencies_from_system_ansible(self):
        self.assertIn('VENV=/usr/local/kubeauto/.venv', SOURCE_SYNC)
        self.assertIn('"\\$PY" -m venv "\\$VENV"', SOURCE_SYNC)
        self.assertIn(
            'exec /usr/local/kubeauto/.venv/bin/python /usr/local/kubeauto/kubecli.py',
            SOURCE_SYNC,
        )
        self.assertNotIn('"${SUDO[@]}" $PY -m pip install', SOURCE_SYNC)
        self.assertIn('--clean-legacy-global-pip', SOURCE_SYNC)
        self.assertIn('pip list --path "\\$GLOBAL_SITE"', SOURCE_SYNC)
        self.assertIn('LEGACY_GLOBAL_PIP_CLEAN_PASS', SOURCE_SYNC)
        self.assertIn("PYTHONNOUSERSITE=1", SOURCE_SYNC)

    def test_remote_source_gates_use_the_isolated_project_python(self):
        for relative in (
            "nerdctl-gate.sh",
            "delivery-docker-gate.sh",
            "regression-full.sh",
            "regression.sh",
            "regression-jumper.sh",
        ):
            source = (ROOT / "tests" / "helpers" / relative).read_text()
            self.assertIn(".venv/bin/python", source, relative)
            self.assertNotIn("python3 -c \"\nfrom common", source, relative)
            self.assertNotIn("python3 -c 'from common", source, relative)

    def test_repository_has_root_agent_contract(self):
        agents = (ROOT / "AGENTS.md").read_text()
        self.assertIn("tests/enterprise-test-matrix.yaml", agents)
        self.assertIn("LAB_CLEAN_VERIFY_PASS", agents)
        self.assertIn("six sibling repositories", agents)

    def test_live_cluster_pass_requires_application_read_write(self):
        jumper = (ROOT / "tests" / "helpers" / "regression-jumper.sh").read_text()
        self.assertIn("kubernetes-production-smoke.sh", jumper)
        for required in (
            "kind: Deployment",
            "kind: Service",
            "kind: Job",
            "get endpointslice",
            ".svc.cluster.local",
            "kubeauto-production-write",
            "PRODUCTION_SMOKE_HTTP_RW_OK",
            "KUBERNETES_PRODUCTION_SMOKE_PASS",
        ):
            self.assertIn(required, PRODUCTION_SMOKE)
        self.assertLess(
            jumper.index("kubernetes-production-smoke.sh"),
            jumper.index("G7_JUMPER_PASS"),
        )


if __name__ == "__main__":
    unittest.main()
