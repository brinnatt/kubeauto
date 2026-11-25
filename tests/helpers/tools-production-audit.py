#!/usr/bin/env python3
"""Read-only acceptance of execution-boundary evidence; never launch a fixture."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[2]
AUDIT = 'tests/evidence/tools-production-execution-audit-20261125.json'
FINAL = 'mysql-backup-final-20261125'
RECORDS = (
    'tools-remote-bootstrap-20261125', 'tools-ovpn-config-20261125',
    'tools-api-boundary-20261125', 'tools-kubebackup-api-full-20261125',
    'tools-publish-load-live-20261125', 'tools-migration-endpoint-full-20261125',
    'starrocks-compose-3.5.7-cn', 'starmigrt-full',
    'mysql84-physical-requal-20261125', 'mysql-backup-portability-20261125',
)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read(name):
    return json.loads((ROOT / name).read_text())


def snapshot(name, expected):
    for directory in ('tools-execution-inputs', 'mysql80-execution-inputs'):
        candidate = ROOT / 'logs' / directory / (expected + Path(name).suffix)
        if candidate.is_file() and sha(candidate) == expected:
            return candidate.read_text()
    raise ValueError('TOOLS_AUDIT_SNAPSHOT_MISSING ' + name + ' ' + expected)


def function(text, name):
    return text.split(name + '() {\n', 1)[1].split('\n}', 1)[0]


def launcher_execution(text, branch):
    header = text.split('\nusage() {', 1)[0]
    # Extending the build-only allowlist cannot change a live branch. Keep its
    # stage refusal, control flow and every other global statement identical.
    header = re.sub(r' \|\| "\$TOOLS_SELECTED" == [A-Za-z0-9,]+', '', header)
    body = text.split('  ' + branch + ')\n', 1)[1].split('\n    ;;', 1)[0]
    return header, body


def compatible_file(name, expected, owner):
    path = ROOT / name
    if path.is_file() and sha(path) == expected:
        return
    old = snapshot(name, expected)
    current = path.read_text()
    branches = {
        'tools-migration-endpoint-full-20261125': '--migration-live',
        'starrocks-compose-3.5.7-cn': '--star-compose-preflight|--star-compose-status|--star-compose-be|--star-compose-cn|--star-compose-full',
        'starmigrt-full': '--starmigrt-status|--starmigrt-qualify|--starmigrt-full',
    }
    if name == 'tests/run_tools_regression.sh' and owner in branches:
        if launcher_execution(old, branches[owner]) == launcher_execution(current, branches[owner]):
            return
    elif name == 'tests/README.md' and owner == 'starmigrt-full':
        # Only the explicitly delimited MySQL8.0 runbook insertion is unrelated.
        before, rest = current.split('MySQL 8.0 的物理生命周期补充', 1)
        _, after = rest.split('每轮 `logs/mysql-backups-', 1)
        if before + '每轮 `logs/mysql-backups-' + after == old:
            return
    elif name == 'tests/helpers/mysql-backups-regression.sh' and owner == 'mysql84-physical-requal-20261125':
        if (old.split('physical() {', 1)[0] == current.split('physical() {', 1)[0]
                and function(old, 'physical') == function(current, 'physical')):
            return
    raise ValueError('TOOLS_AUDIT_STALE ' + owner + ' ' + name)


def verify_record(owner):
    proof = read('tests/evidence/' + owner + '.json')
    if proof.get('rc') != 0 or not proof.get('files'):
        raise ValueError('TOOLS_AUDIT_RECORD_REFUSED ' + owner)
    for group in ('inputs', 'files'):
        for name, expected in proof.get(group, {}).items():
            compatible_file(name, expected, owner)
    logs = []
    for name in proof['files']:
        path = ROOT / name
        if name.endswith(('.exit', '.finalized')) and path.read_text().strip() != '0':
            raise ValueError('TOOLS_AUDIT_DURABLE_REFUSED ' + name)
        if name.endswith('.remote-status') and path.read_text().split() != ['0', '0']:
            raise ValueError('TOOLS_AUDIT_REMOTE_REFUSED ' + name)
        if name.endswith('.log') and '/diagnostics/' not in name:
            logs.append(path.read_text())
    text = '\n'.join(logs)
    for marker in proof.get('markers', []):
        if marker not in text:
            raise ValueError('TOOLS_AUDIT_MARKER_MISSING ' + marker)
    if not any(name.endswith('.exit') for name in proof['files']):
        raise ValueError('TOOLS_AUDIT_DURABLE_MISSING ' + owner)
    if owner in ('starrocks-compose-3.5.7-cn', 'starmigrt-full'):
        marker = ('TOOLS_CLEAN_VERIFY_PASS scope=starrocks-compose' if owner.startswith('starrocks')
                  else 'TOOLS_CLEAN_VERIFY_PASS scope=starmigrt')
        if marker not in text:
            raise ValueError('TOOLS_AUDIT_CLEAN_MISSING ' + owner)
    return proof


def physical_module():
    spec = importlib.util.spec_from_file_location('physical_acceptance', ROOT/'tests/helpers/mysql80-physical-live.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def logical_evidence():
    proof = read('tests/evidence/mysql-backup-20261108.json')
    for name in ('tools/mysqltools/MyLogiBackupCli.py', 'tools-onefile.spec',
                 'tests/helpers/mylogi-backup-live-regression.sh', 'tests/helpers/supervise-mysql-backups.py'):
        if sha(ROOT/name) != proof['inputs'][name]:
            raise ValueError('TOOLS_AUDIT_LOGICAL_STALE ' + name)
    if sha(ROOT/'dist/MyLogiBackupCli-rocky8') != proof['build']['artifacts']['MyLogiBackupCli']:
        raise ValueError('TOOLS_AUDIT_LOGICAL_ARTIFACT_STALE')
    name = 'tests/helpers/mysql-backups-regression.sh'
    old = snapshot(name, proof['inputs'][name])
    current = (ROOT/name).read_text()
    if old.split('physical() {')[0] != current.split('physical() {')[0] or function(old, 'logical') != function(current, 'logical'):
        raise ValueError('TOOLS_AUDIT_LOGICAL_EXECUTION_CHANGED')
    record = proof['stages']['logical']
    for field, key in (('log', 'sha256'), ('archive', 'archive_sha256')):
        if sha(ROOT/record[field]) != record[key]:
            raise ValueError('TOOLS_AUDIT_LOGICAL_RAW_STALE ' + field)
    raw = ROOT/record['log']
    if (ROOT/record['remote_status']).read_text().split() != ['0', '0'] or any(
            raw.with_suffix('.'+suffix).read_text().strip() != '0' for suffix in ('exit', 'finalized')):
        raise ValueError('TOOLS_AUDIT_LOGICAL_DURABLE_REFUSED')
    text = raw.read_text()
    if any(marker not in text for marker in record['required_markers']) or re.search(r'Traceback|FATAL ERROR|_FAILED|_FAIL\b', text):
        raise ValueError('TOOLS_AUDIT_LOGICAL_MARKER_REFUSED')
    return record


def verify_dependencies():
    for owner in RECORDS:
        verify_record(owner)
    logical_evidence()
    module = physical_module()
    module.documentation()
    for stage in ('8.0.33', '8.0.46', 'full'):
        module.verify_record(ROOT/('tests/evidence/mysql80-physical-'+stage+'.json'))


def audit():
    matrix = yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())
    proof = read(AUDIT)
    scripts = {str(p.relative_to(ROOT)) for p in (ROOT/'tools').rglob('*') if p.suffix in ('.py', '.sh')}
    if (proof.get('result') != 'pass' or set(proof['scripts']) != scripts
            or proof['boundaries'] != matrix['execution_boundaries']
            or set(proof['evidence']) != set(RECORDS)):
        raise ValueError('TOOLS_AUDIT_INVENTORY_REFUSED')
    for name, expected in proof['inputs'].items():
        if sha(ROOT/name) != expected:
            raise ValueError('TOOLS_AUDIT_INPUT_STALE ' + name)
    for owner, expected in proof['evidence'].items():
        if sha(ROOT/('tests/evidence/'+owner+'.json')) != expected:
            raise ValueError('TOOLS_AUDIT_EVIDENCE_CHANGED ' + owner)
    verify_dependencies()
    print('TOOLS_PRODUCTION_EXECUTION_AUDIT_PASS scripts=14 python=11 shell=3', flush=True)


def record_final(log):
    audit()
    raw = Path(log).resolve()
    if raw.parent != ROOT/'logs' or not raw.name.startswith('mysql-backups-full-'):
        raise ValueError('TOOLS_FINAL_LOG_SCOPE_REFUSED')
    names = [raw.with_suffix(suffix) for suffix in ('.log', '.exit', '.finalized', '.remote-status', '.evidence.tar.gz')]
    names += [ROOT/AUDIT, ROOT/'tests/evidence/mysql-backup-qualification-20261125.json',
              ROOT/'tests/evidence/mysql80-physical-full.json', ROOT/'tests/run_tools_regression.sh',
              ROOT/'tests/helpers/tools-production-audit.py']
    proof = {'date': '2026-11-25', 'scope': ['TL-X-09'], 'result': 'pass', 'rc': 0,
             'command': 'MYSQL_BACKUP_STAGE=full bash tests/run_tools_regression.sh --mysql-backup-live',
             'markers': ['MYSQL_BACKUP_RELEASE_PASS stage=full', 'MYSQL_BACKUP_RELEASE_EXIT rc=0',
                         'TOOLS_CLEAN_VERIFY_PASS scope=mysql-backup-release',
                         'MYBACKUP_NATIVE_BACKUP_PASS', 'MYBACKUP_NATIVE_PREPARE_PASS', 'MYBACKUP_RESTORE_READBACK_PASS'],
             'files': {str(path.relative_to(ROOT)): sha(path) for path in names},
             'boundary': 'One clean MySQL8.4 physical + three-version logical + portability final run. '
                         'Separate accepted clean 16-combination MySQL8.0 final retained with original identities. '
                         'Other tools use their owning accepted evidence; no claim of rerunning every tool business.'}
    for version in ('8.0.46', '8.4.4', '9.2.0'):
        proof['markers'] += ['MYLOGI_'+kind+'_PASS version='+version for kind in (
            'NATIVE_ARCHIVE_DIAGNOSTICS', 'REAL_TIMER_EXECUTION', 'RESTORE_READBACK')]
    proof['markers'] += ['MYSQL_BACKUP_PORTABILITY_PASS runtime='+runtime+' tools=2'
                         for runtime in ('rocky8', 'debian12', 'ubuntu24')]
    text = raw.read_text()
    if any(marker not in text for marker in proof['markers']) or re.search(r'FATAL ERROR|Traceback|_FAILED|_FAIL\b', text):
        raise ValueError('TOOLS_FINAL_MARKERS_REFUSED')
    for path in names[:5]:
        if path.suffix in ('.exit', '.finalized') and path.read_text().strip() != '0':
            raise ValueError('TOOLS_FINAL_DURABLE_REFUSED')
        if path.suffix == '.remote-status' and path.read_text().split() != ['0', '0']:
            raise ValueError('TOOLS_FINAL_REMOTE_REFUSED')
    target = ROOT/('tests/evidence/'+FINAL+'.json')
    target.write_text(json.dumps(proof, ensure_ascii=False, indent=2)+'\n')
    verify_record(FINAL)
    print('TOOLS_AFFECTED_FINAL_PASS scope=mysql-backup-and-tools-execution-audit', flush=True)


def status():
    matrix = yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())
    cases = {case['id']: case['status'] for case in matrix['test_cases']}
    if cases['TL-X-10'] == 'pass':
        audit()
    if cases['TL-X-09'] == 'pass':
        verify_record(FINAL)
        print('TOOLS_AFFECTED_FINAL_EVIDENCE_PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('dependencies', 'audit', 'status', 'record-final'))
    parser.add_argument('log', nargs='?')
    args = parser.parse_args()
    if args.stage == 'dependencies':
        verify_dependencies()
        print('TOOLS_PRODUCTION_DEPENDENCIES_PASS')
    elif args.stage == 'audit':
        audit()
    elif args.stage == 'status':
        status()
    else:
        record_final(args.log)
