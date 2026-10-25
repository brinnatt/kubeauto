#!/usr/bin/env python3
"""Bind the completed author review to the current formal manuals; no lab work."""
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
REPORT = 'tests/evidence/longhorn-documentation-current-content-review-20261024.json'
DOCUMENTS = {'docs/storage/longhorn/'+name+'.md' for name in
             ('operations-manual', 'technical-whitepaper', 'development-manual')}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_file(root, name):
    path = root/name
    if (Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink() or
            not path.is_file() or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError('review references an unsafe or missing file: '+name)
    return path


def verify(root=ROOT):
    report_path = local_file(root, REPORT)
    report = json.loads(report_path.read_text())
    if report['schema'] != 1 or report['status'] != 'author-content-review-pass':
        raise ValueError('completed author review is required')
    for key in ('source_coverage', 'content_review', 'checks', 'operation_evidence_integrity'):
        if report[key]['status'] != 'pass':
            raise ValueError('incomplete review: '+key)
    if (report['source_coverage']['version'] != '1.12.1' or
            report['source_coverage']['unclassified'] != 0 or
            report['content_review']['open_blocking_document_defects'] or
            not report['content_review']['coverage'] or
            any(item['result'] != 'pass' for item in report['content_review']['coverage']) or
            any(report['reader_paths'][reader]['status'] != 'pass' for reader in ('novice', 'experienced'))):
        raise ValueError('source, content or reader-path review is incomplete')
    if set(report['document_sha256']) != DOCUMENTS:
        raise ValueError('review must cover exactly the three formal manuals')
    for name, expected in {**report['files'], **report['document_sha256']}.items():
        if sha256(local_file(root, name)) != expected:
            raise ValueError('reviewed input changed: '+name)
    for item in report['operation_evidence']:
        if (report['files'].get(item['path']) != item['sha256'] or
                not set(item['documents']).issubset(DOCUMENTS)):
            raise ValueError('operation evidence is not bound to the reviewed files')
    rendering = report['checks']['rendering']
    actual = {(name, index): hashlib.sha256(block.strip().encode()).hexdigest()
              for name in DOCUMENTS for index, block in enumerate(re.findall(
                  r'(?ms)^```mermaid\s*$\n(.*?)^```\s*$', (root/name).read_text()))}
    diagrams = rendering['diagrams']
    reviewed = {(item['document'], item['index']): item['source_sha256'] for item in diagrams}
    if (rendering['status'] != 'pass' or not actual or actual != reviewed or
            len(diagrams) != len(actual) or rendering['total'] != len(actual)):
        raise ValueError('current diagrams lack matching render/review evidence')
    return {'report_sha256': sha256(report_path), 'document_sha256': report['document_sha256'],
            'diagrams': len(actual), 'boundary': 'Author content/source/reader-path acceptance; no customer signature or live delivery PASS'}


if __name__ == '__main__':
    print('LONGHORN_DOCUMENTATION_REVIEW_VERIFIED '+json.dumps(verify(), sort_keys=True))
    print('LONGHORN_DOCUMENTATION_SCOPED_CLEAN_PASS')
    print('LONGHORN_DOCUMENTATION_PASS')
