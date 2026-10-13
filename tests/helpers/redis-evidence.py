#!/usr/bin/env python3
"""Bind completed focused gates to durable logs and current production inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

CASES = {
    'cleanup-qualification': ('REDIS-23', 'REDIS_CLEANUP_QUALIFICATION_PASS'),
    'platform': ('REDIS-05', 'REDIS_PLATFORM_PASS'),
    'sentinel-data': ('REDIS-07', 'REDIS_SENTINEL_CLIENT_PASS'),
    'sentinel-security': ('REDIS-10', 'REDIS_SENTINEL_SECURITY_PASS'),
    'sentinel-failure': ('REDIS-08', 'REDIS_SENTINEL_FAILOVER_PASS'),
    'sentinel-quorum': ('REDIS-09', 'REDIS_SENTINEL_QUORUM_PASS'),
    'backup': ('REDIS-17', 'REDIS_INDEPENDENT_RDB_RESTORE_PASS'),
    'performance': ('REDIS-20', 'REDIS_PERFORMANCE_PASS'),
    'observability': ('REDIS-19', 'REDIS_OBSERVABILITY_PASS'),
    'capacity': ('REDIS-18', 'REDIS_CAPACITY_PASS'),
    'cluster-install': ('REDIS-12', 'REDIS_CLUSTER_INSTALL_PASS'),
    'cluster-data': ('REDIS-13', 'REDIS_CLUSTER_CLIENT_PASS'),
    'cluster-failure': ('REDIS-14', 'REDIS_CLUSTER_FAILURE_PASS'),
    'cluster-rebalance': ('REDIS-15', 'REDIS_CLUSTER_REBALANCE_PASS'),
    'stability': ('REDIS-21', 'REDIS_STABILITY_PASS'),
}
PARTIAL = {
    'cluster-snapshot': ('REDIS-17', 'REDIS_CLUSTER_SNAPSHOT_PASS'),
    'crd-upgrade': ('REDIS-22', 'REDIS_CRD_PRODUCT_UPGRADE_PASS'),
    'upgrade-cluster': ('REDIS-22', 'REDIS_CLUSTER_VERSION_QUALIFICATION_PASS'),
    'upgrade-sentinel': ('REDIS-22', 'REDIS_SENTINEL_VERSION_UPGRADE_PASS'),
    'maintenance-rolling': ('REDIS-11', 'REDIS_ROLLING_MAINTENANCE_PASS'),
    'maintenance-drain': ('REDIS-11', 'REDIS_NODE_DRAIN_PASS'),
    'tls-rotation': ('REDIS-11', 'REDIS_TLS_ROTATION_PASS'),
    'password-rotation': ('REDIS-11', 'REDIS_PASSWORD_ROTATION_PASS'),
    'idempotence': ('REDIS-11', 'REDIS_IDEMPOTENCE_PASS'),
    'persistence': ('REDIS-16', 'REDIS_AOF_PERSISTENCE_PASS'),
}
EXTRA_MARKERS = {
    'password-rotation': ('REDIS_ROTATION_CONSUMER_PREFLIGHT_PASS',),
    'tls-rotation': ('REDIS_ROTATION_CONSUMER_PREFLIGHT_PASS',),
    'capacity': ('REDIS_CAPACITY_CONFIGURATION_RESTORED_PASS',),
    'observability': ('REDIS_OBSERVABILITY_EVENTS_PASS',),
    'crd-upgrade': ('REDIS_CRD_UPGRADE_PASS',),
    'upgrade-cluster': ('REDIS_CLUSTER_VERSION_UPGRADE_PASS', 'REDIS_CLUSTER_SUPPORTED_ROLLBACK_PASS',
                        'REDIS_CLUSTER_UPGRADE_CLEAN_PASS'),
    'cleanup-qualification': ('REDIS_CLEANUP_NORMAL_PASS', 'REDIS_CLEANUP_REFUSAL_PASS',
                              'REDIS_CLEANUP_INTERRUPT_PASS', 'REDIS_CLEANUP_FIXTURES_CLEAN_PASS'),
    'upgrade-sentinel': ('REDIS_OPERATOR_VERSION_UPGRADE_PASS', 'REDIS_SERVER_VERSION_UPGRADE_PASS',
                         'REDIS_OPERATOR_SUPPORTED_ROLLBACK_PASS', 'REDIS_SERVER_SUPPORTED_ROLLBACK_PASS',
                         'REDIS_UPGRADE_SCOPED_CLEAN_PASS'),
    'platform': ('REDIS_PLATFORM_NODE_PASS', 'REDIS_PLATFORM_CLEAN_PASS'),
    'backup': ('REDIS_OFFLINE_RDB_VERIFY_PASS',),
    'cluster-rebalance': ('REDIS_CLUSTER_FUNCTIONS_RESIZE_PASS', 'REDIS_CLUSTER_FUNCTIONS_CLEAN_PASS'),
    'stability': ('REDIS_STABILITY_POD_RESTART_PASS', 'REDIS_STABILITY_OPERATOR_RESTART_PASS',
                  'REDIS_STABILITY_NODE_RESTART_PASS', 'REDIS_STABILITY_RESTART_CLEAN_PASS'),
}
DEPENDENCIES = ('roles/cluster-addon/tasks/redis.yml',
                'common/constants.py', 'conf/config.yml', 'service/cluster/manager.py',
                'roles/cluster-addon/templates/redis/instances.yaml.j2',
                'roles/cluster-addon/templates/redis/config.yaml.j2',
                'roles/cluster-addon/templates/redis/operator-values.yaml.j2',
                'roles/cluster-addon/files/redis-health.py',
                'roles/cluster-addon/files/redis-snapshot.py',
                'roles/cluster-addon/files/redis-operator-rbac.py',
                'roles/cluster-addon/files/redis-operator-rbac.sh',
                'tests/helpers/redis-focused-gate.py', 'tests/helpers/redis-focused-gate.sh')
MODULE_DEPENDENCIES = {
    'cluster-snapshot': ('tests/helpers/redis-cluster-snapshot-gate.py', 'tests/helpers/redis-cluster-client-gate.py'),
    'crd-upgrade': ('tests/helpers/redis-crd-upgrade-gate.py', 'roles/cluster-addon/files/redis-crd.py'),
    'upgrade-cluster': ('tests/helpers/redis-cluster-upgrade-gate.py', 'tests/helpers/redis-upgrade-gate.py'),
    'cleanup-qualification': ('tests/helpers/redis-cleanup-gate.py', 'tests/helpers/redis-cleanup.sh'),
    'upgrade-sentinel': ('tests/helpers/redis-upgrade-gate.py', 'roles/cluster-addon/files/redis-crd.py'),
    'maintenance-rolling': ('tests/helpers/redis-maintenance-gate.py', 'tests/helpers/redis-stability-gate.py'),
    'maintenance-drain': ('tests/helpers/redis-maintenance-gate.py', 'tests/helpers/redis-stability-gate.py',
                          'tests/helpers/redis-lab-storage.py'),
    'platform': ('tests/helpers/redis-platform-gate.py', 'tests/helpers/redis-lab-storage.py'),
    'stability': ('tests/helpers/redis-stability-gate.py', 'tests/helpers/redis-cluster-client-gate.py'),
    'tls-rotation': ('tests/helpers/redis-rotation-gate.py', 'tests/helpers/redis-lab-config.py'),
    'password-rotation': ('tests/helpers/redis-rotation-gate.py', 'tests/helpers/redis-lab-config.py'),
    'backup': ('tests/helpers/redis-backup-gate.py', 'docs/middleware/redis/operations-manual.md'),
    'performance': ('tests/helpers/redis-performance-gate.py',),
    'observability': ('tests/helpers/redis-observability-gate.py', 'tests/evidence/redis-observability-images-20261013.json'),
    'capacity': ('tests/helpers/redis-capacity-gate.py', 'tests/helpers/redis-lab-storage.py'),
    'cluster-install': ('tests/helpers/redis-cluster-gate.py', 'tests/helpers/redis-lab-config.py'),
    'cluster-data': ('tests/helpers/redis-cluster-client-gate.py',),
    'cluster-failure': ('tests/helpers/redis-cluster-failure-gate.py', 'tests/helpers/redis-cluster-client-gate.py',
                        'tests/helpers/redis-cluster-process.py'),
    'cluster-rebalance': ('tests/helpers/redis-cluster-rebalance-gate.py', 'tests/helpers/redis-cluster-client-gate.py',
                          'tests/helpers/redis-capacity-gate.py'),
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_dependencies(root, module):
    return {'schema': 1, 'module': module,
            'dependencies': {p: digest(root / p) for p in dependency_paths(root, module)}}


def dependency_paths(root, module):
    if module == 'full':
        paths = set(DEPENDENCIES)
        for directory in ('common', 'controller', 'model', 'service', 'conf', 'playbooks', 'roles'):
            for path in (root / directory).rglob('*'):
                if path.is_file() and path.suffix in ('.py', '.yml', '.yaml', '.j2', '.sh', '.json', '.tgz', '.sha256'):
                    paths.add(str(path.relative_to(root)))
        for pattern in ('tests/helpers/redis-*', 'tests/fixtures/redis-*'):
            paths.update(str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file())
        paths.update(('kubecli.py', 'runtime_hook.py', 'tests/run_enterprise_regression.sh',
                      'tests/helpers/sync-kubeauto.sh', 'tests/helpers/run-durable-gate.sh',
                      'tests/helpers/lab-wipe-nodes.sh'))
        paths.update(str(p.relative_to(root)) for p in (root / 'docs/middleware/redis').glob('*.md'))
        return tuple(sorted(paths))
    return DEPENDENCIES + MODULE_DEPENDENCIES.get(module, ())


def frozen_dependencies(path, module, root=None):
    doc = json.loads(path.read_text())
    expected = set(dependency_paths(root or Path(__file__).resolve().parents[2], module))
    values = doc.get('dependencies', {})
    if (doc.get('schema') != 1 or doc.get('module') != module or set(values) != expected or
            any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in values.values())):
        raise ValueError('launch source manifest has a wrong module, dependency inventory or SHA256')
    return values


def verify_log(module, log, state):
    case, marker = (CASES | PARTIAL)[module]
    required = [marker, 'REDIS_FOCUSED_SCOPED_CLEAN_PASS module=' + module,
                'REDIS_FOCUSED_MODULE_PASS module=' + module, 'REDIS_GATE_EXIT rc=0']
    required.extend(EXTRA_MARKERS.get(module, ()))
    lines = log.splitlines()
    # Ansible debug stdout_lines uses JSON string entries for product output.
    # Decode only complete JSON strings; prose/substrings never become markers.
    for line in log.splitlines():
        candidate = line.strip().removesuffix(',')
        if not candidate.startswith('"REDIS_'):
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, str) and '\n' not in value and '\r' not in value:
            lines.append(value)
    if (not re.search(r'\bstate=exited\b.*\brc=0\b', state) or
            any(not any(line == m or line.startswith(m + ' ') for line in lines) for m in required) or
            re.search(r'\[FAIL\]|REDIS_[A-Z_]*(?:FAIL|FIRST_FAILURE)\b', log)):
        raise ValueError('focused evidence lacks durable success, complete markers or clean failure state')
    if module == 'stability':
        results = re.findall(r'^REDIS_STABILITY_PASS seconds=([0-9]+) samples=([0-9]+) '
                             r'business-errors=0 restarts=pod\+operator\+node data-and-pvc=retained$',
                             log, re.MULTILINE)
        if len(results) != 1 or int(results[0][0]) < 1800 or int(results[0][1]) <= 0:
            raise ValueError('stability evidence requires a complete thirty-minute error-free soak')
    return case, [line for line in lines if line.startswith('REDIS_') and
                  ('PASS' in line or line.startswith('REDIS_GATE_EXIT'))]


def matrix_with_evidence(text, case, path):
    pattern = rf'(?m)^([ \t]*- \{{id: {case},[^\n]*status: )(pending|pass)(, evidence: )("[^"\n]*")(\}}[ \t]*)$'
    value = str(path) + '; current focused product gate, durable rc=0, zero failure markers and scoped-clean'
    result, count = re.subn(pattern, lambda m: m[1] + 'pass' + m[3] + json.dumps(value) + m[5], text)
    if count != 1:
        raise ValueError('matrix evidence update requires exactly one recognized pending/pass case')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('module', nargs='?')
    parser.add_argument('directory', type=Path, nargs='?')
    parser.add_argument('output', type=Path, nargs='?')
    parser.add_argument('--capture', metavar='MODULE')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--update-matrix', action='store_true')
    args = parser.parse_args()
    root = args.root or Path(__file__).resolve().parents[2]
    if args.capture:
        print(json.dumps(capture_dependencies(root, args.capture), indent=2))
        return
    if args.module not in CASES | PARTIAL or args.directory is None or args.output is None:
        parser.error('module, diagnostics directory and output are required')
    log_path, state_path = args.directory / 'first-run.log', args.directory / 'durable-status.txt'
    log, state = log_path.read_text(), state_path.read_text()
    case, markers = verify_log(args.module, log, state)
    doc = {'schema': 1, 'case_ids': [case], 'module': args.module,
           'acceptance': 'complete module' if args.module in CASES else 'partial matrix item',
           'runner': 'bash tests/run_enterprise_regression.sh --redis-' + args.module,
           'durable_rc': 0, 'failure_markers': 0, 'markers': markers,
           'log_sha256': digest(log_path), 'durable_status_sha256': digest(state_path),
           'dependencies': frozen_dependencies(args.directory / 'launch-source.json', args.module),
           'launch_source_sha256': digest(args.directory / 'launch-source.json'),
           'cleanup_scope': 'Runner-owned UUID fixture keys, fault policies, temporary Pods, '
                            'private files and client/port-forward processes; healthy shared core and CSI retained'}
    if args.module == 'performance':
        results = [json.loads(line.removeprefix('REDIS_PERFORMANCE_RESULT ')) for line in log.splitlines()
                   if line.startswith('REDIS_PERFORMANCE_RESULT ')]
        if len(results) != 1 or results[0]['errors'] != 0 or results[0]['operations'] <= 0:
            raise ValueError('performance evidence lacks one complete error-free result')
        doc['performance'] = results[0]
    with args.output.open('x') as stream:
        json.dump(doc, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    if args.update_matrix and args.module in CASES:
        relative = args.output.resolve().relative_to(root)
        matrix = root / 'tests/redis-test-matrix.yaml'
        original = matrix.read_text()
        updated = matrix_with_evidence(original, case, relative)
        with tempfile.NamedTemporaryFile(mode='w', prefix='.redis-matrix-', dir=matrix.parent, delete=False) as out:
            temporary = Path(out.name)
            try:
                out.write(updated)
                out.flush()
                os.fsync(out.fileno())
                temporary.chmod(matrix.stat().st_mode & 0o777)
                if matrix.read_text() != original:
                    raise ValueError('concurrent matrix mutation; evidence saved but update refused')
                temporary.replace(matrix)
            finally:
                temporary.unlink(missing_ok=True)
    print('REDIS_FOCUSED_EVIDENCE_PASS module=' + args.module + ' acceptance=' + doc['acceptance'])


if __name__ == '__main__':
    main()
