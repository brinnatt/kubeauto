import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
UNIT_RUNNER = (ROOT / "tests" / "run_unit_tests.sh").read_text()
LAB_WIPE = (ROOT / "tests" / "helpers" / "lab-wipe-nodes.sh").read_text()
HANDBOOK = (ROOT / "tests" / "README.md").read_text()
GITIGNORE = (ROOT / ".gitignore").read_text()


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

    def test_all_delivery_mode_composes_every_signoff_gate(self):
        start = RUNNER.index('if [[ "$MODE" == "--all-delivery" ]]')
        end = RUNNER.index('if [[ "$MODE" == "--status" ]]')
        all_delivery = RUNNER[start:end]
        expected_modes = (
            "--jumper-only",
            "--nerdctl-only",
            "--docker-only",
            "--upgrade-only",
            "--gaps-only",
        )
        positions = [all_delivery.index(mode) for mode in expected_modes]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('bash "$0" "$delivery_mode"', all_delivery)
        self.assertIn("ENTERPRISE_DELIVERY_ALL_PASS", all_delivery)

    def test_lab_authority_and_reserved_roles_are_documented(self):
        for address in (
            "192.168.47.128",
            "192.168.47.129",
            "192.168.47.130",
            "192.168.47.131-133",
            "192.168.47.134-136",
            "192.168.47.137",
            "192.168.47.138",
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
            for address in ("192.168.47.140", "192.168.47.141", "192.168.47.142", "192.168.47.147"):
                if address in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {address}")
        self.assertEqual([], offenders)

    def test_cleanup_script_uses_current_authority(self):
        self.assertIn("JUMPER=192.168.47.130", LAB_WIPE)
        self.assertIn("DEBIAN_IP=192.168.47.128", LAB_WIPE)
        self.assertIn("CONTROL=192.168.47.138", LAB_WIPE)
        self.assertIn("192.168.47.137", LAB_WIPE)

    def test_repository_has_root_agent_contract(self):
        agents = (ROOT / "AGENTS.md").read_text()
        self.assertIn("tests/enterprise-test-matrix.yaml", agents)
        self.assertIn("LAB_CLEAN_VERIFY_PASS", agents)
        self.assertIn("six sibling repositories", agents)


if __name__ == "__main__":
    unittest.main()
