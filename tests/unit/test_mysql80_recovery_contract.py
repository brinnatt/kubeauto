"""Only the exact failed capacity boundary may use native-host recovery."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('recover_contract', ROOT/'tests/helpers/mysql80-physical-recover.py')
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


class RecoveryContracts(unittest.TestCase):
    def test_failed_host_scope_inputs_and_durable_failure_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root/'logs/mysql80-full-rocky8-owned'; directory.mkdir(parents=True)
            plan = {'stage':'full', 'os':'8.10', 'inputs':{'product':'current'}}
            (directory/'plan.json').write_text(json.dumps(plan))
            for suffix in ('exit','finalized'):(directory/('state.'+suffix)).write_text('1\n')
            (directory/'console.log').write_text('MYSQL80_CAPACITY_REFUSED')
            with mock.patch.object(RECOVERY.GATE, 'ROOT', root), mock.patch.object(RECOVERY.GATE, 'inputs', return_value={'product':'current'}):
                self.assertEqual(RECOVERY.admit_failed_branch(directory,'rocky8'), plan)
                with self.assertRaises(ValueError):RECOVERY.admit_failed_branch(directory,'rocky9')
                (directory/'state.exit').write_text('0\n')
                with self.assertRaises(ValueError):RECOVERY.admit_failed_branch(directory,'rocky8')
                (directory/'state.exit').write_text('1\n')
                plan['inputs']={'product':'changed'}; (directory/'plan.json').write_text(json.dumps(plan))
                with self.assertRaises(ValueError):RECOVERY.admit_failed_branch(directory,'rocky8')


if __name__ == '__main__':unittest.main()
