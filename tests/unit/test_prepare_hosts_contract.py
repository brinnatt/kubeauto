import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepare_hosts_contract", ROOT / "tests/helpers/prepare_hosts_contract.py")
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class PrepareHostsContractTests(unittest.TestCase):
    def test_delegated_shared_file_writes_are_serialized_for_every_node(self):
        task = HELPER.hosts_task(ROOT)
        self.assertEqual(task["throttle"], 1)
        self.assertEqual(task["delegate_to"], "{{ groups.kube_master[0] }}")
        self.assertEqual(task["lineinfile"]["dest"], "/etc/hosts")
        self.assertIn("{{ inventory_hostname }}", task["lineinfile"]["line"])
        self.assertNotIn("run_once", task)

    def test_ceph_fingerprint_includes_shared_host_resolution(self):
        runner = (ROOT / "tests/run_enterprise_regression.sh").read_text()
        fingerprint = runner.split("ceph_gate_fingerprint() {", 1)[1].split("require_ceph_focused_evidence", 1)[0]
        self.assertIn("roles/prepare/tasks/main.yml", fingerprint)


if __name__ == "__main__":
    unittest.main()
