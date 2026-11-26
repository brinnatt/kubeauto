"""Prevent both stale physical PASS reuse and unrelated evidence invalidation."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mysql80_dependency_gate', ROOT/'tests/helpers/mysql80-physical-live.py')
GATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GATE)


class EvidenceDependencyContracts(unittest.TestCase):
    def test_only_proven_unrelated_launcher_or_verifier_edits_preserve_execution(self):
        runner = 'tests/run_tools_regression.sh'
        helper = 'tests/helpers/mysql80-physical-live.py'
        product = 'tools/mysqltools/MyBackupCli.py'
        fixture = 'tests/helpers/mybackup-cli-live-regression.sh'
        originals = {name: (ROOT/name).read_text() for name in (runner, helper, product, fixture)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root/'logs/mysql80-execution-inputs'; saved.mkdir(parents=True)
            recorded = {}
            for name, text in originals.items():
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
                recorded[name] = GATE.sha(path)
                (saved/(recorded[name]+path.suffix)).write_text(text)
            def inputs():
                return {name: GATE.sha(root/name) for name in originals}
            with mock.patch.object(GATE, 'ROOT', root), mock.patch.object(GATE, 'inputs', side_effect=inputs):
                self.assertTrue(GATE.compatible_inputs(recorded))
                (root/runner).write_text(originals[runner].replace("MIGRATION_FINAL_INPUT_BINDING_INCOMPLETE", "MIGRATION_UNRELATED_NEW_ERROR"))
                self.assertTrue(GATE.compatible_inputs(recorded))
                (root/runner).write_text(originals[runner].replace('MYSQL_BACKUP_STAGE#physical80-', 'MYSQL_BACKUP_STAGE#wrong-'))
                self.assertFalse(GATE.compatible_inputs(recorded))
                (root/runner).write_text(originals[runner].replace('set -euo pipefail', 'set +e'))
                self.assertFalse(GATE.compatible_inputs(recorded))
                (root/runner).write_text(originals[runner])
                (root/helper).write_text(originals[helper].replace("'MYSQL80_HOST_PASS os='", "'WRONG_HOST_PASS os='"))
                self.assertFalse(GATE.compatible_inputs(recorded))
                (root/helper).write_text(originals[helper])
                (root/product).write_text(originals[product]+'\n# changed product\n')
                self.assertFalse(GATE.compatible_inputs(recorded))
                (root/product).write_text(originals[product])
                (root/fixture).write_text(originals[fixture]+'\n# legacy branch only\n')
                self.assertTrue(GATE.compatible_inputs(recorded))
                (root/fixture).write_text(originals[fixture].replace('LIFECYCLE="${MYBACKUP_LIFECYCLE:-0}"', 'LIFECYCLE=0'))
                self.assertFalse(GATE.compatible_inputs(recorded))
                (root/fixture).write_text(originals[fixture])
                (root/runner).write_text(originals[runner].replace("MIGRATION_FINAL_INPUT_BINDING_INCOMPLETE", "MIGRATION_UNRELATED_NEW_ERROR"))
                snapshot = saved/(recorded[runner]+'.sh')
                snapshot.write_text('wrong snapshot')
                self.assertFalse(GATE.compatible_inputs(recorded))


if __name__ == '__main__':
    unittest.main()
