import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
UNIT_RUNNER = (ROOT / "tests" / "run_unit_tests.sh").read_text()
LAB_WIPE = (ROOT / "tests" / "helpers" / "lab-wipe-nodes.sh").read_text()
AIO_PREP = (ROOT / "tests" / "helpers" / "regression-aio-prep-full.sh").read_text()
GAPS_FULLCHAIN = (
    ROOT / "tests" / "helpers" / "delivery-gaps-fullchain.sh"
).read_text()
HANDBOOK = (ROOT / "tests" / "README.md").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()
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
DURABLE_GATE = ROOT / "tests" / "helpers" / "run-durable-gate.sh"


class TestDeliveryProcessContract(unittest.TestCase):
    def test_lab_cleanup_removes_owned_dropins_and_verifier_refuses_them(self):
        # Execute the actual shell fragments against a temporary systemd tree.
        for wipe_name, verify_name, docker_owned in (
                ('wipe_cmd', 'verify_cmd', True),
                ('CTRL_WIPE', 'verify_control_cmd', False)):
            with self.subTest(scope=wipe_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fragments = {}
                for name in (wipe_name, verify_name):
                    block = LAB_WIPE.split(name + "='\n", 1)[1].split("\n'\n", 1)[0]
                    start = block.index('for unit in ', block.index('# Remove only overrides' if name == wipe_name else '# A removed main unit'))
                    end = block.index('\ndone', start) + len('\ndone')
                    fragments[name] = block[start:end].replace('/etc/systemd/system', str(root/'etc')).replace('/run/systemd/system', str(root/'run'))
                for base in ('etc', 'run'):
                    for unit in ('kube-apiserver', 'kubelet', 'docker', 'sshd'):
                        directory = root/base/(unit+'.service.d')
                        directory.mkdir(parents=True)
                        (directory/'90-stale.conf').write_text('[Service]\nExecStart=\nExecStart=/old/server\n')
                    (root/base/'etcd.service.d').symlink_to(root/'missing')
                def verify():
                    return subprocess.run(['bash', '-eu', '-c', 'bad=0\n'+fragments[verify_name]+'\nexit "$bad"'], capture_output=True, text=True, timeout=5)
                self.assertEqual(1, verify().returncode)
                result = subprocess.run(['bash', '-eu', '-c', fragments[wipe_name]], capture_output=True, text=True, timeout=5)
                self.assertEqual(0, result.returncode, result.stderr)
                result = verify()
                self.assertEqual(0, result.returncode, result.stdout+result.stderr)
                for base in ('etc', 'run'):
                    self.assertTrue((root/base/'sshd.service.d/90-stale.conf').is_file())
                    self.assertEqual(not docker_owned, (root/base/'docker.service.d').exists())
                    self.assertFalse((root/base/'etcd.service.d').is_symlink())
                    self.assertFalse((root/base/'kube-apiserver.service.d').exists())

    def test_unit_tests_do_not_dirty_the_tracked_deploy_log(self):
        self.assertIn("mktemp -d /tmp/kubeauto-unit-logs.", UNIT_RUNNER)
        self.assertIn('export KUBEAUTO_LOG_DIR="$UNIT_LOG_DIR"', UNIT_RUNNER)
        self.assertIn("trap 'rm -rf", UNIT_RUNNER)

    def test_runtime_logs_are_ignored_but_policy_is_tracked(self):
        for name in ("logs/runtime.log", "logs/runtime.out", "logs/runtime.pid",
                     "logs/run/evidence.json", "logs/README.md"):
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", name],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(1 if name == "logs/README.md" else 0,
                             result.returncode, result.stderr)

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

    def test_supervisor_counts_ceph_failures_even_with_zero_durable_exit(self):
        function = "remote_job_summary()" + RUNNER.split("remote_job_summary()", 1)[1].split("remote_log_tail()", 1)[0]
        with tempfile.TemporaryDirectory(prefix="kubeauto-supervisor-") as temporary:
            directory = Path(temporary)
            prefix = directory / "state"
            for suffix, content in (("pid", "999999999"), ("exit", "0"), ("finalized", "0")):
                (directory / f"state.{suffix}").write_text(content)
            log = directory / "live.log"
            for marker in ("CEPH_STAGE_FAIL id=CEPH-13 class=test-gate", "CEPH_CONTRACT_FAIL reason=fixture", "[FAIL] fixture"):
                with self.subTest(marker=marker):
                    log.write_text(f"{marker}\nCEPH_DELIVERY_PASS\n")
                    script = function + f"\nlocal_ssh() {{ bash -lc \"$1\"; }}\nremote_job_summary local_ssh '' '{prefix}' '{log}'\n"
                    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("rc=0", result.stdout)
                    self.assertIn("failure_markers=1", result.stdout)

    def test_jumper_prep_finalizes_success_and_failure_for_the_supervisor(self):
        prep = (ROOT / "tests/helpers/regression-jumper-prep.sh").read_text()
        startup = prep.split("\n# -a installs", 1)[0]
        summary = "remote_job_summary()" + RUNNER.split("remote_job_summary()", 1)[1].split("remote_log_tail()", 1)[0]
        with tempfile.TemporaryDirectory(prefix="kubeauto-jumper-finalizer-") as temporary:
            directory = Path(temporary)
            prefix = directory / "state"
            for rc in (0, 7):
                with self.subTest(rc=rc):
                    for suffix in ("exit", "finalized", "finalized.part"):
                        (directory / f"state.{suffix}").write_text("42\n")
                    script = startup.replace(
                        "STATE_PREFIX=/tmp/kubeauto-regression-jumper", f'STATE_PREFIX="{prefix}"')
                    script += '\ntest ! -e "${STATE_PREFIX}.exit"\ntest ! -e "${STATE_PREFIX}.finalized"\n'
                    script += 'test ! -e "${STATE_PREFIX}.finalized.part"\n'
                    result = subprocess.run(["bash", "-c", script + f"exit {rc}\n"],
                                            text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, rc, result.stderr)
                    for suffix in ("exit", "finalized"):
                        self.assertEqual((directory / f"state.{suffix}").read_text(), f"{rc}\n")
                    self.assertFalse((directory / "state.finalized.part").exists())
                    log = directory / "live.log"
                    log.write_text(f"REGRESSION_JUMPER_EXIT rc={rc}\n")
                    script = summary + f'\nlocal_ssh() {{ bash -c "$1"; }}\nremote_job_summary local_ssh "" "{prefix}" "{log}"\n'
                    result = subprocess.run(["bash", "-c", script], text=True,
                                            capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("state=exited", result.stdout)
                    self.assertIn(f"rc={rc}", result.stdout)

    def test_supervisor_process_snapshot_cannot_block_on_stuck_pstree(self):
        function = "remote_process_tree()" + RUNNER.split("remote_process_tree()", 1)[1].split("remote_diagnostics()", 1)[0]
        with tempfile.TemporaryDirectory(prefix="kubeauto-process-snapshot-") as temporary:
            directory = Path(temporary)
            pstree = directory / "pstree"
            pstree.write_text("#!/bin/sh\nsleep 30\n")
            pstree.chmod(0o755)
            function = function.replace("timeout -k 1s 5s pstree", f"timeout -k 1s 5s {pstree}")
            prefix = directory / "state"
            script = function + f"""
printf '%s\n' $$ >'{prefix}.pid'
local_ssh() {{ eval "$1"; }}
remote_process_tree local_ssh '' '{prefix}'
"""
            started = time.monotonic()
            result = subprocess.run(["bash", "-c", script], text=True,
                                    capture_output=True, timeout=9)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 8)
            self.assertIn("process_tree_snapshot=unavailable_or_timed_out", result.stdout)

    def test_cancel_does_not_treat_an_exited_zombie_as_a_live_process(self):
        function = "cancel_remote_job()" + RUNNER.split("cancel_remote_job()", 1)[1].split("matrix_counts()", 1)[0]
        with tempfile.TemporaryDirectory(prefix="kubeauto-cancel-") as temporary:
            prefix = Path(temporary) / "state"
            process = subprocess.Popen(["bash", "-c", "exit 0"])
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    state = subprocess.run(["ps", "-p", str(process.pid), "-o", "stat="], text=True, capture_output=True).stdout.strip()
                    if state.startswith("Z"):
                        break
                    time.sleep(0.01)
                self.assertTrue(state.startswith("Z"), state)
                Path(f"{prefix}.pid").write_text(str(process.pid))
                script = function + f"\nlocal_ssh() {{ bash -lc \"$1\"; }}\ncancel_remote_job fixture local_ssh '' '{prefix}'\n"
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("state=not-running", result.stdout)
            finally:
                process.wait(timeout=3)

    def test_durable_terminal_fence_waits_for_child_exit_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="kubeauto-durable-gate-") as temporary:
            workdir = Path(temporary)
            state_prefix = workdir / "state"
            trap_started = workdir / "trap-started"
            child = workdir / "child.sh"
            child.write_text(
                "#!/usr/bin/env bash\n"
                "trap 'touch \"$1\"; sleep 0.3; echo CHILD_EXIT_DIAGNOSTICS' EXIT\n"
                "exit 7\n"
            )
            child.chmod(0o755)
            process = subprocess.Popen(
                [
                    "bash",
                    str(DURABLE_GATE),
                    str(state_prefix),
                    "DURABLE_TEST_EXIT",
                    "bash",
                    str(child),
                    str(trap_started),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 3
            while not trap_started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(trap_started.exists(), "child EXIT trap did not start")
            self.assertFalse((workdir / "state.finalized").exists())
            output, _ = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 7)
            self.assertEqual((workdir / "state.exit").read_text().strip(), "7")
            self.assertEqual((workdir / "state.finalized").read_text().strip(), "7")
            self.assertLess(output.index("CHILD_EXIT_DIAGNOSTICS"), output.index("DURABLE_TEST_EXIT rc=7"))

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
        self.assertIn(
            'surface" == logging ]] && matrix="$ROOT/tests/logging-test-matrix.yaml"',
            RUNNER,
        )

    def test_delivery_runner_validates_matrix_before_gates(self):
        validator = "tests/helpers/validate-test-matrix.py"
        self.assertIn(validator, RUNNER)
        self.assertLess(
            RUNNER.index(validator), RUNNER.index('if [[ "$MODE" == "--mysql-only" ]]')
        )
        self.assertIn("KUBEAUTO_MATRIX_WIP", RUNNER)
        self.assertIn("--require-pass", RUNNER)

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
        )
        positions = [all_delivery.index(mode) for mode in expected_modes]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('bash "$0" "$delivery_mode"', all_delivery)
        self.assertIn("ENTERPRISE_DELIVERY_ALL_PASS", all_delivery)
        self.assertNotIn("--tier3-tools-only", all_delivery)
        self.assertNotIn("run_tools_regression.sh", all_delivery)

    def test_core_progress_excludes_requalification_stage_ids(self):
        function = "matrix_counts()" + RUNNER.split("matrix_counts()", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests/enterprise-test-matrix.yaml").write_text(
                "items:\n"
                "  - {id: T0-01, status: pass}\n"
                "  - {id: PROM-01, status: pass}\n"
                "  - {id: X-G6-01, status: pass}\n"
                "  - {id: T1-G2-01, status: pending}\n"
                "  - {id: T2-AD-13, status: fail}\n"
                "stages:\n  - {id: static, status: pass}\n"
                "  - {id: nacos, status: pending}\n"
            )
            result = subprocess.run(
                ["bash", "-c", 'ROOT="$1"; MODE=--nacos-startup-only\n' + function + "matrix_counts", "test", str(root)],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "matrix_pass=3 matrix_pending=1 matrix_fail=1")

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
            ROOT / "tests" / "ceph-test-matrix.yaml",
            ROOT / "tests" / "unit" / "test_delivery_process_contract.py",
            ROOT / "tests" / "unit" / "test_registry_ready.py",
        }
        offenders = []
        ceph_qualification_paths = {
            ROOT / "tests/run_enterprise_regression.sh",
            ROOT / "tests/helpers/ceph_contract.py",
            ROOT / "tests/helpers/ceph-qualification-topology.sh",
            ROOT / "tests/helpers/ceph_os_qualification.py",
            ROOT / "tests/helpers/ceph_ssh_fixture.py",
        }
        for path in (ROOT / "tests").rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path in allowed_paths:
                continue
            text = path.read_text(errors="ignore")
            # .147 was subsequently authorized for the isolated Longhorn V2
            # branch. Keep the exception scoped to its helpers and evidence.
            longhorn_path = (
                (path.parent == ROOT / 'tests/helpers' and path.name.startswith('longhorn-')) or
                (path.parent == ROOT / 'tests/unit' and path.name.startswith('test_longhorn_')) or
                (path.parent == ROOT / 'tests/fixtures' and path.name.startswith('longhorn-') and path.name.endswith('.py.txt')) or
                (path.parent == ROOT / 'tests/evidence' and path.name.startswith('longhorn-') and path.suffix == '.json')
            )
            for address in ("192.168.47.140", "192.168.47.147"):
                if address == "192.168.47.147" and (path in ceph_qualification_paths or longhorn_path):
                    continue
                if address in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {address}")
        self.assertEqual([], offenders)

    def test_longhorn_source_fixture_address_exception_preserves_forbidden_hosts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);fixture=root/'tests/fixtures/longhorn-reviewed.py.txt'
            fixture.parent.mkdir(parents=True)
            with patch.dict(globals(),{'ROOT':root}):
                fixture.write_text('192.168.47.147')
                self.test_active_test_code_has_no_retired_lab_address()
                fixture.write_text('192.168.47.140')
                with self.assertRaises(AssertionError):
                    self.test_active_test_code_has_no_retired_lab_address()
                fixture.unlink()
                other=root/'tests/fixtures/foreign.py.txt';other.write_text('192.168.47.147')
                with self.assertRaises(AssertionError):
                    self.test_active_test_code_has_no_retired_lab_address()

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

    def test_sudo_bootstrap_admits_authorized_ubuntu24_and_rejects_unknown_account(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            ssh = base / 'ssh'
            ssh.write_text('''#!/bin/bash
set -eu
if [[ ! -f "$BOOTSTRAP_TEST_STATE" ]]; then
  touch "$BOOTSTRAP_TEST_STATE"
  exit 1
fi
if [[ "${!#}" == *"sudo -S"* ]]; then
  read -r supplied
  [[ "$supplied" == "$LAB_SUDO_PASSWORD" ]]
fi
''')
            ssh.chmod(0o700)
            for account, expected in (('ubuntu24', 0), ('unknown', 2)):
                state = base / account
                environment = dict(os.environ, PATH=str(base) + ':' + os.environ['PATH'],
                                   LAB_SUDO_PASSWORD='synthetic-test-value', BOOTSTRAP_TEST_STATE=str(state))
                result = subprocess.run(['bash', str(ROOT / 'tests/helpers/lab-sudo-bootstrap.sh'),
                                         account + '@192.0.2.1'], env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(expected, result.returncode, result.stderr)
                self.assertNotIn(environment['LAB_SUDO_PASSWORD'], result.stdout + result.stderr)
                if not expected:
                    self.assertIn('LAB_SUDO_BOOTSTRAP_PASS', result.stdout)

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
        self.assertIn("tests/enterprise-test-matrix.yaml", AGENTS)
        self.assertIn("LAB_CLEAN_VERIFY_PASS", AGENTS)
        self.assertIn("完成一次六仓信息审计", AGENTS)

    def test_ext_images_rule_preserves_incremental_scope_and_artifact_admission(self):
        self.assertNotIn("### Tools fixture and external-artifact supply chain", AGENTS)
        scope = AGENTS.split("### 增量交付与范围判定", 1)[1].split("### 企业文档质量契约", 1)[0]
        for required in (
            "六仓是兼容性审计单元",
            "不能将本组件消费的全部镜像直接当成重建集合",
            "网络、认证或证据读取失败先定位原因",
            "实际发布集合包含于允许集合",
            "仅在用户明确授权本次整仓全量范围后使用",
            "新增中间件仍须一次最终干净完整回归",
        ):
            self.assertIn(required, scope)
        rules = AGENTS.split("## Non-negotiable delivery rules", 1)[1]
        artifacts = rules.split("- Prefer `hub.talkedu.cn`", 1)[0]
        for required in (
            "inventory its production, upgrade, rollback, backup, performance and test-infrastructure dependencies",
            "Follow the scope and reuse rules above",
            "official-source, version-pinned Dockerfiles",
            "Reused and newly published artifacts must both have verified TalkEdu and Docker Hub manifests/digests",
        ):
            self.assertIn(required, artifacts)

    def test_test_gate_change_ladder_is_a_permanent_delivery_contract(self):
        for required in (
            "Mandatory test-gate change ladder",
            "Classify the proposed change before running it",
            "Complete the static preflight",
            "narrowest fixed-runner branch",
            "Finish the affected module gates before starting one final clean full",
            "retain healthy shared prerequisites",
            "not automatic reuse or blanket invalidation",
            "prerequisite: never spend the remaining stages",
            "completed ladder level",
        ):
            self.assertIn(required, AGENTS)
        for required in (
            "## Test-gate change protocol",
            "lowest-cost checks first",
            "deterministic regression test for a proven test-gate defect",
            "A static or focused failure blocks the full middleware gate",
            "Abort after a failed prerequisite",
            "completed ladder level",
        ):
            self.assertIn(required, HANDBOOK)

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
