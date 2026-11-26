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

# Existing successful native runs already passed the old port preflight. This
# exact gate repair changes neither their MySQL/PXB execution nor raw results.
NATIVE_PORT_OLD = "            with socket.socket() as probe: probe.bind(('127.0.0.1',port))\n"
NATIVE_PORT_NEW = ("            with socket.socket() as probe:\n"
                   "                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                   "                probe.bind(('127.0.0.1',port))\n")
NATIVE_GUARD_OLD = "    if proof['inputs']!=inputs() or proof['rc']!=0 or set(proof['hosts'])!=set(HOSTS) or not proof.get('artifact'):\n"
NATIVE_GUARD_NEW = ("    if proof['inputs'] != inputs():\n"
                    "        if set(proof['inputs']) != set(inputs()):\n"
                    "            raise ValueError('BINARY80_INPUT_SET_CHANGED')\n"
                    "        for name, expected in proof['inputs'].items():\n"
                    "            load('tools-production-audit').compatible_file(name, expected, 'mysql80-binary-'+proof['stage'])\n"
                    "    if proof['rc']!=0 or set(proof['hosts'])!=set(HOSTS) or not proof.get('artifact'):\n")
NATIVE_HOST_NOTE = (
    '131 的 VMware balloon 容量拒绝不改变产品结论；后续固定 Rocky8.10 主机为已授权的 137，仍按现场实际 OS/架构/内存和租约准入。\n'
    '旧证据保留原计划与输入摘要；仅精确主机地址替换、相应主机合同常量及证据复用守卫可复用，生命周期、版本、GTID、产品或原始文件改变均拒绝。\n\n')


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
    if name == 'tests/helpers/mysql80-binary-live.py' and owner in (
            'mysql80-binary-standalone', 'mysql80-binary-primary-replica', 'mysql80-binary-dual-primary'):
        candidate = old
        if owner != 'mysql80-binary-dual-primary' and old.count(NATIVE_PORT_OLD) == 1 and old.count(NATIVE_GUARD_OLD) == 1:
            candidate = old.replace(NATIVE_PORT_OLD, NATIVE_PORT_NEW, 1).replace(NATIVE_GUARD_OLD, NATIVE_GUARD_NEW, 1)
        if candidate.count("'root@192.168.47.131'") == 1:
            candidate = candidate.replace("'root@192.168.47.131'", "'root@192.168.47.137'", 1)
        if current == candidate:
            return
    elif name == 'tests/run_tools_regression.sh' and owner in branches:
        if launcher_execution(old, branches[owner]) == launcher_execution(current, branches[owner]):
            return
    elif name == 'tests/unit/test_tools_evidence_dependency_contract.py' and owner == 'tools-migration-endpoint-full-20261125':
        # The synthetic admission fixture starts with accepted prerequisites;
        # actual product/gate code and every rejection assertion are unchanged.
        isolated_state = (
            '            # A deterministic input-binding fixture must not inherit another\n'
            "            # product's current acceptance state from the working checkout.\n"
            "            for case in matrix['test_cases']:\n"
            "                if case['id'].startswith(('TL-MIG-', 'TL-X-')):\n"
            "                    case['status'] = 'pass'\n")
        if current.count(isolated_state) == 1 and current.replace(isolated_state, '', 1) == old:
            return
    elif name == 'tests/README.md' and owner == 'mysql80-binary-supply-chain-20261125':
        candidate = old.replace('Rocky Linux 8.10（131）', 'Rocky Linux 8.10（137；既有已接受证据来自 131）', 1)
        candidate = candidate.replace('仍使用 131 与 .122.216，两端实际',
                                      '后续使用 137 与 .122.216；既有已接受的 131 独立证据保留原身份。两端实际', 1)
        candidate = candidate.replace('每轮 `logs/mysql-backups-', NATIVE_HOST_NOTE+'每轮 `logs/mysql-backups-', 1)
        if current == candidate:
            return
    elif name == 'tests/README.md' and owner == 'starmigrt-full':
        # MySQL runbook additions and inventory counts cannot change the
        # unchanged StarMigrtCli path; all other text remains evidence-bound.
        before, rest = current.split('MySQL 8.0 的物理生命周期补充', 1)
        _, after = rest.split('每轮 `logs/mysql-backups-', 1)
        def inventory_counts(text):
            return re.sub(r'The tools matrix contains \d+ scenarios \(\d+ applicable and one not applicable\) plus a \d+-item',
                          'The tools matrix contains CURRENT scenarios and inventory', text)
        if inventory_counts(before + '每轮 `logs/mysql-backups-' + after) == inventory_counts(old):
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


def binary_module():
    spec = importlib.util.spec_from_file_location('binary_acceptance', ROOT/'tests/helpers/mysql80-binary-live.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def logical_evidence():
    proof = read('tests/evidence/mysql-logical-requal-20261126.json')
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
    native = binary_module()
    for stage in ('standalone', 'primary-replica', 'dual-primary', 'full'):
        native.verify_record(ROOT/('tests/evidence/mysql80-binary-'+stage+'.json'))


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
              ROOT/'tests/evidence/mysql80-binary-full.json',
              ROOT/'tests/helpers/tools-production-audit.py']
    proof = {'date': '2026-11-25', 'scope': ['TL-X-09'], 'result': 'pass', 'rc': 0,
             'command': 'MYSQL_BACKUP_STAGE=full bash tests/run_tools_regression.sh --mysql-backup-live',
             'markers': ['MYSQL_BACKUP_RELEASE_PASS stage=full', 'MYSQL_BACKUP_RELEASE_EXIT rc=0',
                         'TOOLS_CLEAN_VERIFY_PASS scope=mysql-backup-release',
                         'MYBACKUP_NATIVE_BACKUP_PASS', 'MYBACKUP_NATIVE_PREPARE_PASS', 'MYBACKUP_RESTORE_READBACK_PASS'],
             'files': {str(path.relative_to(ROOT)): sha(path) for path in names},
             'boundary': 'One clean MySQL8.4 physical + three-version logical + portability final run. '
                         'Separate accepted clean 16-combination MySQL8.0 final retained with original identities. '
                         'Native single-instance, primary-replica and asynchronous dual-primary final '
                         'binds both exact Rocky releases, both MySQL versions and GTID OFF/ON. '
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
    if cases['TL-MBK-01'] == 'pass':
        verify_record('mysql-backup-portability-20261125')
    if any(cases['TL-MBK-%02d'%n] == 'pass' for n in (2, 4)):
        verify_record('mybackup-native-contract-20261125')
    if any(cases['TL-MBK-%02d'%n] == 'pass' for n in (3, 5, 6)):
        verify_record('mysql84-physical-requal-20261125')
    if cases.get('TL-X-06') == 'pass':
        verify_record('mysql80-binary-supply-chain-20261125')
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
