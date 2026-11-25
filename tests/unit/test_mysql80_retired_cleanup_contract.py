"""Retired staging removal requires exact scope and durable local raw files."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('retired_cleanup', ROOT/'tests/helpers/mysql80-retired-staging-cleanup.py')
GATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GATE)


class RetiredCleanupContracts(unittest.TestCase):
    def test_wrong_host_scope_and_missing_fence_are_refused(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); ident = 'a'*12
            base = root/'logs'/('mysql80-full-rocky8-'+ident); base.mkdir(parents=True)
            plan = {'id':ident, 'stage':'full', 'os':'8.10', 'base':'/tmp/mb80-'+ident}
            (base/'plan.json').write_text(json.dumps(plan)); (base/'console.log').write_text('first failure')
            (base/'state.exit').write_text('1'); (base/'state.finalized').write_text('1')
            record = GATE.archived_run(base)
            self.assertEqual(record['base'], plan['base'])
            self.assertIn('console.log', record['files'])
            (base/'state.finalized').unlink()
            with self.assertRaises(FileNotFoundError): GATE.archived_run(base)
            (base/'state.finalized').write_text('1'); plan['os']='9.8'
            (base/'plan.json').write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, 'SCOPE_REFUSED'): GATE.archived_run(base)

    def test_remote_program_compiles_and_checks_before_removal(self):
        compile(GATE.REMOTE, '<remote-cleanup>', 'exec')
        self.assertLess(GATE.REMOTE.index('hashlib.sha256'), GATE.REMOTE.index('shutil.rmtree'))
        self.assertIn("'/proc/self/mountinfo'", GATE.REMOTE)
        self.assertIn("'systemctl','start','tuned'", GATE.REMOTE)
