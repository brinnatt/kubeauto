"""Contracts for the standalone, supervised Kubernetes upgrade gate."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
GATE = (ROOT / "tests/helpers/delivery-upgrade-smoke.sh").read_text()
RUNNER = (ROOT / "tests/run_enterprise_regression.sh").read_text()


class TestUpgradeGate(unittest.TestCase):
    def test_gate_uses_large_reserved_host_and_real_patch_transition(self):
        self.assertIn("UPGRADE_GATE_NODE:-192.168.47.137", GATE)
        self.assertIn("OLD_VERSION=v1.33.5", GATE)
        self.assertIn("NEW_VERSION=v1.33.6", GATE)
        self.assertIn('run $K upgrade "$C" </dev/null', GATE)
        self.assertIn('BEFORE=$before', GATE)
        self.assertIn('AFTER=$after', GATE)
        self.assertIn("generated config contains unresolved component-version placeholders", GATE)
        self.assertIn("dbuser|yourpassword", GATE)
        self.assertNotIn('cp "$BASE/conf/config.yml"', GATE)

    def test_old_binaries_are_offline_and_officially_pinned(self):
        self.assertIn("OFFICIAL", GATE.upper())
        self.assertIn('OLD_PRIVATE_IMAGE="hub.talkedu.cn/kubeauto/kubeauto-k8s-bin:$OLD_VERSION"', GATE)
        self.assertIn('OLD_FALLBACK_IMAGE="brinnatt/kubeauto-k8s-bin:$OLD_VERSION"', GATE)
        self.assertLess(GATE.index('"$OLD_PRIVATE_IMAGE"'), GATE.index('"$OLD_FALLBACK_IMAGE"'))
        self.assertIn("mktemp -d /tmp/kubeauto-k8s-1335.", GATE)
        self.assertIn('run docker cp "$OLD_CONTAINER:/k8s/." "$OLD_BIN/"', GATE)
        self.assertIn('sha256sum -c "$OLD_STAGE/official.sha256"', GATE)
        self.assertIn("trap cleanup_fixture EXIT", GATE)
        self.assertNotIn("curl ", GATE)
        self.assertNotIn("/home/ubuntu/k8s1335", GATE)

    def test_gate_requires_ready_pods_versions_runtime_and_services(self):
        self.assertIn("healthy_pods", GATE)
        self.assertIn("assert_remote_version", GATE)
        self.assertIn("kubectl version --client=true -o yaml", GATE)
        self.assertIn("gitVersion: $expected", GATE)
        self.assertIn("runtime changed during upgrade", GATE)
        self.assertIn("docker cri-dockerd", GATE)
        self.assertIn("UPGRADE_GATE_PASS", GATE)
        self.assertNotIn('cp -f "$BASE"/kube-bin/* /usr/local/bin/', GATE)
        self.assertLess(GATE.index('run $K download -X'), GATE.index('if ! $K setup'))
        self.assertIn("baseline automatic diagnostics", GATE)
        self.assertIn("kubectl get events -A", GATE)
        self.assertIn("crictl ps -a", GATE)

    def test_pod_health_waits_with_visible_progress_before_diagnostics(self):
        healthy = GATE.split("healthy_pods(){", 1)[1].split("\n}", 1)[0]
        self.assertIn('tries="${1:-48}"', healthy)
        self.assertIn('for attempt in $(seq 1 "$tries")', healthy)
        self.assertIn('[WAIT] pods status=not-ready', healthy)
        self.assertIn('attempt=$attempt/$tries next_check=10s', healthy)
        self.assertIn('sleep 10', healthy)
        self.assertIn('baseline_diagnostics', healthy)
        self.assertLess(healthy.index('sleep 10'), healthy.index('baseline_diagnostics'))
        self.assertNotIn('if [ "$bad" -ne 0 ]; then', healthy)

    def test_runner_supervises_and_cleans_upgrade_gate(self):
        self.assertIn('--upgrade-only', RUNNER)
        self.assertIn('/tmp/kubeauto-upgrade-gate.exit', RUNNER)
        self.assertIn('UPGRADE_GATE_PASS', RUNNER)
        branch = RUNNER.split('if [[ "$MODE" == "--upgrade-only" ]]', 1)[1]
        branch = branch.split("\nfi", 1)[0]
        self.assertGreaterEqual(branch.count("lab-wipe-nodes.sh"), 4)
        self.assertIn("monitor_remote_job", branch)


if __name__ == "__main__":
    unittest.main()
