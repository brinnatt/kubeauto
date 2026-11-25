"""Evidence reuse must reject changed execution, raw bytes and durable failures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('production_audit', ROOT/'tests/helpers/tools-production-audit.py')
GATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GATE)


class ProductionAuditContracts(unittest.TestCase):
    def test_unrelated_branch_and_build_allowlist_do_not_change_owning_execution(self):
        text = (ROOT/'tests/run_tools_regression.sh').read_text()
        branch = '--migration-live'
        baseline = GATE.launcher_execution(text, branch)
        self.assertEqual(baseline, GATE.launcher_execution(text.replace('TOOLS_KUBE_BACKUP_EXIT', 'UNRELATED'), branch))
        self.assertEqual(baseline, GATE.launcher_execution(text.replace(' || "$TOOLS_SELECTED" == KafkaCli,StarCli', ''), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('set -euo pipefail', 'set +e'), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('stage="${MIGRATION_STAGE:-full}"', 'stage=wrong'), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('&& "${1:---preflight}" == --build-only', '&& true'), branch))

    def test_tampered_snapshot_cannot_rescue_changed_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory)
            path = root/'tools/product.py'; path.parent.mkdir(); path.write_text('new')
            expected = hashlib.sha256(b'old').hexdigest()
            saved = root/'logs/tools-execution-inputs'/(expected+'.py'); saved.parent.mkdir(parents=True)
            saved.write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'SNAPSHOT_MISSING'):
                GATE.compatible_file('tools/product.py', expected, 'owner')
            saved.write_text('old')
            with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                GATE.compatible_file('tools/product.py', expected, 'owner')

    def test_raw_digest_success_marker_and_durable_outcome_are_independent_requirements(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); (root/'logs').mkdir(); (root/'tests/evidence').mkdir(parents=True)
            log = root/'logs/run.log'; log.write_text('OWNING_PASS\nSCOPED_CLEAN_PASS\n')
            fence = root/'logs/run.exit'; fence.write_text('0\n')
            proof = {'rc': 0, 'markers': ['OWNING_PASS', 'SCOPED_CLEAN_PASS'], 'files': {}}
            def save():
                proof['files'] = {str(p.relative_to(root)): GATE.sha(p) for p in (log, fence)}
                (root/'tests/evidence/owner.json').write_text(json.dumps(proof))
            save(); GATE.verify_record('owner')
            log.write_text('tampered')
            with self.assertRaises(ValueError): GATE.verify_record('owner')
            save()
            with self.assertRaisesRegex(ValueError, 'MARKER_MISSING'): GATE.verify_record('owner')
            log.write_text('OWNING_PASS\nSCOPED_CLEAN_PASS\n'); fence.write_text('1\n'); save()
            with self.assertRaisesRegex(ValueError, 'DURABLE_REFUSED'): GATE.verify_record('owner')

    def test_physical_evidence_reuse_does_not_ignore_cleanup_or_backup_body_changes(self):
        text = (ROOT/'tests/helpers/mysql-backups-regression.sh').read_text()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); name = 'tests/helpers/mysql-backups-regression.sh'
            path = root/name; path.parent.mkdir(parents=True); path.write_text(text)
            old = text.replace('select_digest_image() {', 'unrelated_portability_selector() {')
            expected = hashlib.sha256(old.encode()).hexdigest()
            saved = root/'logs/tools-execution-inputs'/(expected+'.sh'); saved.parent.mkdir(parents=True); saved.write_text(old)
            GATE.compatible_file(name, expected, 'mysql84-physical-requal-20261125')
            for value in (text.replace('MYBACKUP_NATIVE_BACKUP_PASS', 'WRONG'), text.replace('trap cleanup EXIT', 'trap true EXIT')):
                path.write_text(value)
                with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                    GATE.compatible_file(name, expected, 'mysql84-physical-requal-20261125')
