#!/usr/bin/env python3
"""Validate MongoDB coverage and evidence state without touching a cluster."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml

REQUIRED_GROUPS = frozenset('''static disabled validation artifacts platform preflight storage install data
transactions failover quorum rebuild maintenance security rotation encryption network logical-backup
logical-restore physical-backup physical-restore pitr backup-refusal retention disaster-recovery
sharding-install sharding-data sharding-failure sharding-scale sharding-restore replica-scale capacity
observability performance stability upgrade rollback idempotence ownership cleanup documentation full'''.split())
FIELDS = ('id', 'group', 'desc', 'status', 'product_entry', 'success_marker', 'official',
          'fixture', 'cleanup', 'disproof')


def validate_documentation(root: Path) -> list[str]:
    """Verify the reviewed text is still current; this does not review prose."""
    evidence = root / 'tests/evidence/mongodb-documentation.json'
    review = json.loads(evidence.read_text())
    directory = root / 'docs/middleware/mongodb'
    expected = {'operations-manual.md', 'technical-whitepaper.md', 'development-manual.md'}
    if {p.name for p in directory.iterdir()} != expected:
        return ['customer documentation set differs from the three formal manuals']
    if (review.get('content_review') != 'pass' or review.get('open_defects') != [] or
            review.get('reader_paths') != {'novice': 'pass', 'experienced': 'pass'}):
        return ['documentation content and both reader paths require completed review']
    expected_paths = {'docs/middleware/mongodb/' + name for name in expected}
    if set(review.get('documents', {})) != expected_paths:
        return ['documentation review must bind exactly the three manuals']
    errors = []
    for name, digest in review['documents'].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            errors.append('documentation review is stale: ' + name)
    if not review.get('figures') or any(f.get('rendered') is not True or f.get('visually_checked') is not True
                                         for f in review['figures']):
        errors.append('documentation figures require actual rendering and visual review')
    if review.get('static_check_rc') != 0 or not review.get('operation_evidence'):
        errors.append('documentation static checks and applicable product evidence are required')
    for item in review.get('operation_evidence', []):
        path = root / item['path']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            errors.append('documentation product evidence changed: ' + item['path'])
        accepted = json.loads(path.read_text())
        if (accepted.get('durable_rc') != 0 or accepted.get('failure_markers') != 0 or
                item['case_id'] not in accepted.get('case_ids', [])):
            errors.append('documentation product evidence lacks matching acceptance: ' + item['path'])
    return errors


def validate(path: Path, require_pass: bool = False) -> list[str]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return ['matrix root must be a mapping']
    errors = []
    meta, cases = data.get('meta'), data.get('cases')
    if not isinstance(meta, dict) or meta.get('component') != 'mongodb':
        errors.append('meta.component must be mongodb')
        meta = meta if isinstance(meta, dict) else {}
    if not isinstance(cases, list) or not cases:
        return errors + ['cases must be a non-empty list']
    groups, ids, markers = set(), set(), set()
    all_pass = True
    for case in cases:
        if not isinstance(case, dict):
            errors.append('each case must be a mapping')
            all_pass = False
            continue
        label = case.get('id', '<missing>')
        for field in FIELDS:
            if not isinstance(case.get(field), str) or not case[field].strip():
                errors.append(f'{label}: {field} must be non-empty text')
        for key, seen in (('id', ids), ('group', groups), ('success_marker', markers)):
            value = case.get(key)
            if isinstance(value, str):
                if value in seen:
                    errors.append(f'{label}: duplicate {key}')
                seen.add(value)
        if not re.fullmatch(r'MONGODB-[0-9]{2}', str(label)):
            errors.append(f'{label}: invalid case id')
        group = case.get('group', '')
        expected_marker = 'MONGODB_' + str(group).upper().replace('-', '_') + '_PASS'
        if case.get('success_marker') != expected_marker:
            errors.append(f'{label}: marker differs from module identity')
        expected_command = 'bash tests/run_enterprise_regression.sh --mongodb-' + ('only' if group == 'full' else str(group))
        if case.get('disproof') != expected_command:
            errors.append(f'{label}: disproof must use fixed runner and exact module')
        if not str(case.get('official', '')).startswith('https://docs.percona.com/'):
            errors.append(f'{label}: official Percona reference required')
        status, evidence = case.get('status'), case.get('evidence')
        if status not in ('pending', 'pass', 'fail'):
            errors.append(f'{label}: invalid status')
        if not isinstance(evidence, str):
            errors.append(f'{label}: evidence must be text')
        if status == 'pending' and evidence:
            errors.append(f'{label}: pending case cannot carry accepted evidence')
        if status in ('pass', 'fail') and not evidence:
            errors.append(f'{label}: completed case requires current evidence')
        all_pass &= status == 'pass'
    if missing := REQUIRED_GROUPS - groups:
        errors.append('missing coverage: ' + ', '.join(sorted(missing)))
    if extra := groups - REQUIRED_GROUPS:
        errors.append('unknown modules: ' + ', '.join(sorted(extra)))
    result = meta.get('result')
    if result not in ('pending', 'PASS', 'FAIL'):
        errors.append('meta.result must be pending, PASS or FAIL')
    if result == 'PASS' and not all_pass:
        errors.append('PASS requires all cases to pass')
    if require_pass and (result != 'PASS' or not all_pass):
        errors.append('--require-pass requires completed current matrix')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix', type=Path)
    parser.add_argument('--require-pass', action='store_true')
    parser.add_argument('--documentation', action='store_true', help='check current manually reviewed documentation evidence')
    args = parser.parse_args()
    try:
        errors = validate(args.matrix, args.require_pass)
        if args.documentation:
            errors += validate_documentation(args.matrix.resolve().parents[1])
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        errors = [str(exc)]
    if errors:
        print('MONGODB_MATRIX_VALIDATION_FAIL\n' + '\n'.join(errors))
        return 1
    print('MONGODB_MATRIX_VALIDATION_PASS')
    if args.documentation:
        print('MONGODB_DOCUMENTATION_PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
