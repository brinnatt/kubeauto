#!/usr/bin/env python3
"""Discover the real CLI grammar and reject unsupported parameter sign-offs.

Discovery is offline: stop at parse_args, before any command dispatch. A help
page or a parsed value is deliberately not accepted as behavioral evidence.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / 'tests/tools-parameter-coverage.json'
PROBE = r'''
import argparse, json, runpy, sys
def walk(parser, command=()):
    result = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                result.extend(walk(child, command + (name,)))
            continue
        def plain(value):
            if value is None or isinstance(value, (str, int, float, bool, list, tuple)):
                return value
            return getattr(value, '__name__', str(value))
        result.append(dict(command=list(command), options=action.option_strings,
            dest=action.dest, action=type(action).__name__, default=plain(action.default),
            required=action.required, nargs=action.nargs,
            choices=(dict(range=[action.choices.start, action.choices.stop, action.choices.step])
                     if isinstance(action.choices, range) else
                     list(action.choices) if action.choices is not None else None),
            type=plain(action.type)))
    return result
def discover(parser, *args, **kwargs):
    print('PARAMETER_TREE=' + json.dumps(walk(parser), ensure_ascii=False))
    raise SystemExit(0)
argparse.ArgumentParser.parse_args = discover
sys.argv = [sys.argv[1], '--help']
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def discover():
    result = {}
    # All tool imports and incidental logs are confined to this temporary path.
    with tempfile.TemporaryDirectory(prefix='tools-parameters-') as directory:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for key in ('KUBEAUTO_LOG_DIR', 'NETCHECK_LOG_DIR', 'GENOVPN_LOG_DIR',
                    'KAFKA_LOG_DIR', 'STARROCKS_LOG_DIR', 'MYSQL_MIGRATION_LOG_DIR'):
            env[key] = directory
        env['MYSQLBACKUP_LOG_FILE'] = str(Path(directory) / 'backup.log')
        env.pop('MYSQL_BACKUP_PASSWORD', None)
        for path in sorted((ROOT / 'tools').rglob('*.py')):
            process = subprocess.run([sys.executable, '-B', '-c', PROBE, str(path)],
                                     cwd=directory, env=env, text=True,
                                     capture_output=True, timeout=20)
            lines = [line for line in process.stdout.splitlines()
                     if line.startswith('PARAMETER_TREE=')]
            if process.returncode or len(lines) != 1:
                raise ValueError('PARAMETER_DISCOVERY_REFUSED ' + path.name + ' ' + process.stderr[-2000:])
            result[path.stem] = dict(path=str(path.relative_to(ROOT)),
                                     parameters=json.loads(lines[0].split('=', 1)[1]))
    return result


def key(row):
    return '/'.join([*row['command'], row['dest']])


def validate(data, actual):
    errors = []
    if set(data['tools']) != set(actual):
        errors.append('tool inventory differs from executable parsers')
    for tool, found in actual.items():
        saved = data['tools'].get(tool, {})
        rows = saved.get('parameters', [])
        if [r['grammar'] for r in rows] != found['parameters']:
            errors.append(tool + ': parser grammar changed or parameter missing')
        for row in rows:
            identity = tool + ':' + key(row['grammar'])
            if row['status'] not in ('pending', 'pass'):
                errors.append(identity + ': invalid status')
            if row['status'] == 'pass':
                # Never infer behavioral PASS from option text, help, parsing,
                # or a generic test-suite success marker.
                checks = row.get('checks', [])
                if not checks or any(not c.get('test') or not c.get('assertion') or
                                     c.get('kind') not in ('behavior', 'rejection', 'offline-help')
                                     for c in checks):
                    errors.append(identity + ': behavioral assertion missing')
                if all(c.get('kind') == 'offline-help' for c in checks) and row['grammar']['dest'] not in (
                        'help', 'version', 'help_config', 'help_native', 'guide'):
                    errors.append(identity + ': help cannot accept a functional parameter')
                if not row.get('evidence'):
                    errors.append(identity + ': current evidence missing')
                for name in row.get('evidence', []):
                    try:
                        verify_evidence(name, tool, row)
                    except (OSError, ValueError, KeyError) as exc:
                        errors.append(identity + ': ' + str(exc))
    return errors


def verify_evidence(name, tool, row):
    proof = json.loads((ROOT/name).read_text())
    if proof['tool'] != tool or proof['rc'] != 0:
        raise ValueError('PARAMETER_EVIDENCE_REFUSED ' + name)
    for path, expected in {**proof['inputs'], **proof['files']}.items():
        if sha(ROOT/path) != expected:
            raise ValueError('PARAMETER_EVIDENCE_STALE ' + path)
    required = ('plan.json', 'contracts.log', 'state.exit', 'cleanup.log', 'result.json')
    owned = {Path(path).name: ROOT/path for path in proof['files']}
    if not all(part in owned for part in required):
        raise ValueError('PARAMETER_DURABLE_MISSING ' + name)
    if owned['state.exit'].read_text().strip() != '0':
        raise ValueError('PARAMETER_DURABLE_REFUSED ' + name)
    if owned['cleanup.log'].read_text().strip() != 'TOOLS_CLEAN_VERIFY_PASS scope=parameter-contracts':
        raise ValueError('PARAMETER_CLEANUP_REFUSED ' + name)
    plan = json.loads(owned['plan.json'].read_text())
    if dict(grammar=row['grammar'], checks=row['checks']) not in plan:
        raise ValueError('PARAMETER_PLAN_CHANGED ' + key(row['grammar']))
    outcome = json.loads(owned['result.json'].read_text())
    if (outcome['skipped'] or outcome['errors'] or outcome['failures'] or
            set(outcome['executed']) != set(proof['tests'])):
        raise ValueError('PARAMETER_EXECUTION_INCOMPLETE ' + name)
    for check in row['checks']:
        if check['test'] not in proof['tests']:
            raise ValueError('PARAMETER_TEST_NOT_EXECUTED ' + check['test'])
    return proof


def run_contracts(tool):
    data = json.loads(INVENTORY.read_text())
    record = data['tools'][tool]
    checks = [c for row in record['parameters'] for c in row['checks']]
    if any(not row['checks'] for row in record['parameters']):
        raise ValueError('PARAMETER_CHECKS_INCOMPLETE ' + tool)
    tests = sorted({c['test'] for c in checks})
    token = uuid.uuid4().hex
    directory = ROOT/'logs'/('tools-parameters-'+tool+'-'+token)
    directory.mkdir(parents=True)
    inputs = {record['path']: sha(ROOT/record['path']),
              'tests/helpers/tools-parameter-audit.py': sha(__file__)}
    # Offline help discovers every executable parser. Bind everything it reads,
    # including shipped configuration and documentation, to the same run.
    inputs.update({str(p.relative_to(ROOT)): sha(p) for p in (ROOT/'tools').rglob('*')
                   if p.is_file() and '__pycache__' not in p.parts})
    pending = []
    for test in tests:
        path = test.rsplit('.', 2)[0].replace('.', '/')+'.py'
        pending.append(path)
    while pending:
        path = pending.pop()
        if path in inputs:
            continue
        inputs[path] = sha(ROOT/path)
        for node in ast.walk(ast.parse((ROOT/path).read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('tests.'):
                dependency = node.module.replace('.', '/')+'.py'
                if (ROOT/dependency).is_file() and dependency not in inputs:
                    pending.append(dependency)
    # Capture the immutable grammar and check plan independently of mutable
    # acceptance labels to avoid a self-referential evidence checksum.
    plan = directory/'plan.json'
    plan.write_text(json.dumps([dict(grammar=r['grammar'], checks=r['checks'])
                               for r in record['parameters']], ensure_ascii=False, indent=2)+'\n')
    inputs[str(plan.relative_to(ROOT))] = sha(plan)
    raw = directory/'contracts.log'
    with tempfile.TemporaryDirectory(prefix='tools-parameter-contracts-') as work, raw.open('w') as stream:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for key in ('KUBEAUTO_LOG_DIR', 'NETCHECK_LOG_DIR', 'GENOVPN_LOG_DIR',
                    'KAFKA_LOG_DIR', 'STARROCKS_LOG_DIR', 'MYSQL_MIGRATION_LOG_DIR'):
            env[key] = work
        env['MYSQLBACKUP_LOG_FILE'] = str(Path(work)/'backup.log')
        runner = '''import json,sys,unittest
class Result(unittest.TextTestResult):
    def startTest(self, test):
        self.executed.append(test.id())
        super().startTest(test)
    executed = []
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[2:])
result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
with open(sys.argv[1], 'w') as output:
    json.dump(dict(executed=result.executed, skipped=len(result.skipped),
                   errors=len(result.errors), failures=len(result.failures)), output)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
'''
        try:
            result = subprocess.run([sys.executable, '-B', '-c', runner, str(directory/'result.json'), *tests],
                                    cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=600)
            rc = result.returncode
        except subprocess.TimeoutExpired:
            stream.write('\nPARAMETER_CONTRACTS_TIMEOUT seconds=600\n')
            rc = 124
    (directory/'state.exit').write_text(str(rc)+'\n')
    print(raw.read_text(), end='', flush=True)
    if rc:
        raise ValueError('PARAMETER_CONTRACTS_REFUSED ' + str(raw))
    (directory/'cleanup.log').write_text('TOOLS_CLEAN_VERIFY_PASS scope=parameter-contracts\n')
    proof = dict(tool=tool, run_id=token, rc=0, tests=tests, inputs=inputs,
                 files={str(p.relative_to(ROOT)): sha(p) for p in directory.iterdir() if p.is_file()})
    target = ROOT/'tests/evidence'/('tools-parameters-'+tool+'.json')
    target.write_text(json.dumps(proof, ensure_ascii=False, indent=2)+'\n')
    print('TOOLS_PARAMETER_CONTRACTS_PASS tool='+tool+' run_id='+token, flush=True)


def status():
    data = json.loads(INVENTORY.read_text())
    errors = validate(data, discover())
    if errors:
        raise ValueError('PARAMETER_AUDIT_REFUSED\n' + '\n'.join(errors))
    total = passed = 0
    for tool, record in data['tools'].items():
        rows = record['parameters']
        count = sum(row['status'] == 'pass' for row in rows)
        total += len(rows)
        passed += count
        print('{}: {}/{} 参数上下文已验收'.format(tool, count, len(rows)))
        for row in rows:
            if row['status'] != 'pass':
                print('  pending ' + key(row['grammar']))
    print('TOOLS_PARAMETER_AUDIT accepted={} total={} pending={}'.format(passed, total, total-passed))
    if passed != total:
        raise ValueError('PARAMETER_ACCEPTANCE_INCOMPLETE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('discover', 'status', 'run'))
    parser.add_argument('--tool')
    args = parser.parse_args()
    if args.stage == 'discover':
        print(json.dumps(discover(), ensure_ascii=False, indent=2))
    elif args.stage == 'status':
        status()
    else:
        run_contracts(args.tool)


if __name__ == '__main__':
    main()
