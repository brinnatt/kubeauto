import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class TestJumperRegressionContract(unittest.TestCase):
    def test_jumper_cluster_is_created_from_clean_state(self):
        script = (ROOT / "tests/helpers/regression-jumper.sh").read_text()

        self.assertIn('rm -rf "$BASE/clusters/k8s-dev"', script)
        self.assertIn("kubecli new k8s-dev", script)
        self.assertIn('cat > "$BASE/clusters/k8s-dev/hosts"', script)
        self.assertLess(
            script.index("kubecli new k8s-dev"),
            script.index("kubecli setup k8s-dev 90"),
        )

    def test_jumper_uses_current_authoritative_four_node_topology(self):
        script = (ROOT / "tests/helpers/regression-jumper.sh").read_text()

        for address in (
            "192.168.47.131",
            "192.168.47.134",
            "192.168.47.135",
            "192.168.47.136",
        ):
            self.assertIn(address, script)
        self.assertIn("$2 ~ /^Ready/", script)
        self.assertIn('ready" -eq 4', script)
        self.assertIn('total" -eq 4', script)

    def test_reserved_is_disabled_on_small_jumper_nodes(self):
        script = (ROOT / "tests/helpers/regression-jumper.sh").read_text()

        self.assertIn('KUBE_RESERVED_ENABLED: "no"', script)
        self.assertIn('SYS_RESERVED_ENABLED: "no"', script)

    def test_outer_runner_always_cleans_after_jumper(self):
        script = (ROOT / "tests/run_enterprise_regression.sh").read_text()
        monitor = script.index("jumper-130 ssh130")
        cleanup = script.index('run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"', monitor)
        failure = script.index('fail "G7 jumper regression failed', cleanup)

        self.assertLess(monitor, cleanup)
        self.assertLess(cleanup, failure)

    def test_outer_runner_stops_durable_stale_job_before_preflight_wipe(self):
        script = (ROOT / "tests/run_enterprise_regression.sh").read_text()
        phase = script.index('echo "========== PHASE 0b: clean stale lab state')
        cancel = script.index("run cancel_remote_job jumper-130", phase)
        wipe = script.index(
            'run bash "$ROOT/tests/helpers/lab-wipe-nodes.sh"', cancel
        )

        self.assertIn("test -r '${state_prefix}.pid'", script)
        self.assertIn('descendants() {', script)
        self.assertLess(cancel, wipe)

    def test_monitor_uses_final_ansible_recap_not_ignored_fatal_lines(self):
        script = (ROOT / "tests/run_enterprise_regression.sh").read_text()

        self.assertNotIn("fatal: .*FAILED|Traceback", script)
        self.assertIn("failed=[1-9]", script)


if __name__ == "__main__":
    unittest.main()
