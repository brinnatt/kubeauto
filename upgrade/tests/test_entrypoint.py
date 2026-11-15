"""The target branch must not route a Kubernetes minor upgrade to playbook 93."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upgrade_entrypoint", ROOT / "upgrade/product/entrypoint.py")
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class EntryPointContracts(unittest.TestCase):
    def test_pending_first_hop_prerequisites_refuse_before_stage_callback(self):
        manager = object()
        with self.assertRaisesRegex(ValueError, "UPG-03|UPG-05|execution remains disabled|source cluster"):
            entrypoint.run_minor_upgrade(manager, "dev")

    def test_target_entrypoint_binds_no_legacy_93_playbook(self):
        calls = []

        def fake_run(path, *, runner, required):
            calls.append((path, list(required)))
            return "admitted"

        with patch.object(entrypoint, "admit_and_run", side_effect=fake_run):
            result = entrypoint.run_minor_upgrade(object(), "dev")
        self.assertEqual(result, "admitted")
        self.assertEqual(calls[0][0], entrypoint.DEFAULT_MATRIX)
        self.assertEqual(calls[0][1], ["UPG-01", "UPG-02", "UPG-03", "UPG-04", "UPG-05"])


if __name__ == "__main__":
    unittest.main()
