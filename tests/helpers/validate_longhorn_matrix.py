#!/usr/bin/env python3
"""Validate the independent Longhorn acceptance plan without claiming live PASS."""
import argparse
from pathlib import Path
import re
import sys

import yaml


def validate(path):
    data = yaml.safe_load(Path(path).read_text())
    cases = data['cases']
    if len(cases) != 54 or {c['id'] for c in cases} != {'LONGHORN-%02d' % n for n in range(1, 55)}:
        raise ValueError('Longhorn requires all 54 independently tracked V1 and V2 acceptance cases')
    if set(data['boundaries']['data_engines']) != {'v1', 'v2'}:
        raise ValueError('Longhorn requires both filesystem V1 and replicated block V2 coverage')
    groups, commands, markers = set(), set(), set()
    for case in cases:
        for field in ('id', 'group', 'desc', 'status', 'command', 'smallest_disproof', 'success_marker',
                      'fixture_boundary', 'cleanup_scope', 'official_reference', 'implementation_status', 'evidence_requirements'):
            if not case.get(field): raise ValueError('missing ' + field + ' in ' + case['id'])
        if case['status'] not in ('pending', 'pass') or case['implementation_status'] not in ('pending', 'implemented'):
            raise ValueError('invalid state in ' + case['id'])
        if not re.fullmatch(r'LONGHORN_[A-Z0-9_]+_PASS', case['success_marker']):
            raise ValueError('invalid marker in ' + case['id'])
        if not re.fullmatch(r'bash tests/run_enterprise_regression.sh --longhorn-[a-z][a-z0-9-]* </dev/null', case['command']):
            raise ValueError('case must use the independent fixed runner')
        if case['group'].startswith('v2-'):
            if case.get('data_engines') != ['v2']:
                raise ValueError('V2 acceptance must explicitly bind the V2 data engine')
            for field in ('official_behavior', 'project_assertion', 'prerequisites', 'failure_paths'):
                if not case.get(field):
                    raise ValueError('V2 case lacks official guidance or integration boundary: ' + field)
        for value, seen in ((case['group'], groups), (case['command'], commands), (case['success_marker'], markers)):
            if value in seen: raise ValueError('duplicate case identity')
            seen.add(value)
        if case['status'] == 'pass':
            if case['implementation_status'] != 'implemented' or not case.get('evidence'):
                raise ValueError('PASS requires implemented gate and current evidence')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('matrix', nargs='?', default='tests/longhorn-test-matrix.yaml')
    args = parser.parse_args()
    data = validate(args.matrix)
    print('LONGHORN_MATRIX_VALID cases=' + str(len(data['cases'])))


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, OSError) as error:
        print('LONGHORN_MATRIX_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)
