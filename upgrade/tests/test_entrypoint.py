"""The target branch must not route a Kubernetes minor upgrade to playbook 93."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("upgrade_entrypoint", ROOT / "upgrade/product/entrypoint.py")
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class EntryPointContracts(unittest.TestCase):
    def test_pending_prerequisites_refuse_before_stage_callback(self):
        data = yaml.safe_load(entrypoint.DEFAULT_MATRIX.read_text())
        for case in data['cases']:
            case.update(status='pending', evidence=None)
        # Keep the real version directory's combination inputs, but isolate
        # mutable acceptance state from the delivered matrix and lab leases.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                dir=entrypoint.DEFAULT_MATRIX.parent) as matrix:
            yaml.safe_dump(data, matrix)
            matrix.flush()
            with patch.object(entrypoint, 'DEFAULT_MATRIX', Path(matrix.name)), \
                 patch.object(entrypoint, 'run_next_hop') as coordinator:
                with self.assertRaisesRegex(ValueError, 'required matrix prerequisites are not pass: UPG-01'):
                    entrypoint.run_minor_upgrade(object(), 'dev')
                coordinator.assert_not_called()

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
