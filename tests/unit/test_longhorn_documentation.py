"""The document gate consumes completed reviews and rejects stale evidence."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_docs', ROOT/'tests/helpers/longhorn-documentation.py')
docs = importlib.util.module_from_spec(spec); spec.loader.exec_module(docs)


class DocumentationEvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        documents = {}
        for name in docs.DOCUMENTS:
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Manual\n```mermaid\nflowchart LR\n A --> B\n```\n')
            documents[name] = docs.sha256(path)
        self.report = {'schema': 1, 'status': 'author-content-review-pass',
            'source_coverage': {'status': 'pass', 'version': '1.12.1', 'unclassified': 0},
            'content_review': {'status': 'pass', 'open_blocking_document_defects': [], 'coverage': [{'result': 'pass'}]},
            'reader_paths': {reader: {'status': 'pass'} for reader in ('novice', 'experienced')},
            'checks': {'status': 'pass', 'rendering': {'status': 'pass', 'total': 3,
                'diagrams': [{'document': name, 'index': 0, 'source_sha256': hashlib.sha256(b'flowchart LR\n A --> B').hexdigest()} for name in documents]}},
            'operation_evidence_integrity': {'status': 'pass'}, 'operation_evidence': [],
            'document_sha256': documents, 'files': {}}

    def run_report(self, report=None):
        path = self.root/docs.REPORT; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report if report is None else report))
        return docs.verify(self.root)

    def test_completed_current_review_is_accepted(self):
        self.assertEqual(self.run_report()['diagrams'], 3)

    def test_stale_document_is_rejected(self):
        (self.root/next(iter(docs.DOCUMENTS))).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'input changed'): self.run_report()

    def test_diagram_binding_uses_the_archived_trimmed_source_convention(self):
        # Renderer maps record strip(), not the Markdown fence's trailing LF.
        self.assertEqual(self.run_report()['diagrams'], 3)
        self.report['checks']['rendering']['diagrams'][0]['source_sha256'] = hashlib.sha256(
            b'flowchart LR\n A --> B\n').hexdigest()
        with self.assertRaisesRegex(ValueError, 'diagrams'): self.run_report()

    def test_incomplete_source_content_or_reader_review_is_rejected(self):
        for key in ('source_coverage', 'content_review', 'checks', 'operation_evidence_integrity'):
            row = copy.deepcopy(self.report); row[key]['status'] = 'pending'
            with self.subTest(key=key), self.assertRaises(ValueError): self.run_report(row)
        row = copy.deepcopy(self.report); row['reader_paths']['novice']['status'] = 'pending'
        with self.assertRaises(ValueError): self.run_report(row)
        row = copy.deepcopy(self.report); row['content_review']['open_blocking_document_defects'] = ['open']
        with self.assertRaises(ValueError): self.run_report(row)

    def test_missing_diagram_or_unbound_operation_evidence_is_rejected(self):
        row = copy.deepcopy(self.report); row['checks']['rendering']['diagrams'].pop()
        with self.assertRaisesRegex(ValueError, 'diagrams'): self.run_report(row)
        row = copy.deepcopy(self.report); row['operation_evidence'] = [{'path': 'unbound', 'sha256': 'abc', 'documents': []}]
        with self.assertRaisesRegex(ValueError, 'not bound'): self.run_report(row)

    def test_outside_or_symlink_review_file_is_rejected(self):
        row = copy.deepcopy(self.report); row['files']['../outside'] = 'abc'
        with self.assertRaisesRegex(ValueError, 'unsafe'): self.run_report(row)
        target = self.root/'alias'; target.symlink_to(self.root/next(iter(docs.DOCUMENTS)))
        row = copy.deepcopy(self.report); row['files']['alias'] = 'abc'
        with self.assertRaisesRegex(ValueError, 'unsafe'): self.run_report(row)
