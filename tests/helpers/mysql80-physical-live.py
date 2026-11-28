#!/usr/bin/env python3
"""Qualify MyBackupCli on the two authorized native Rocky container hosts."""
import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
HOSTS = {'rocky8': ('root@192.168.47.137', '8.10'),
         'rocky9': ('root@192.168.122.216', '9.8')}
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
VERSIONS = ('8.0.33', '8.0.46')
IDS = {'8.0.33': 'TL-MBK-07', '8.0.46': 'TL-MBK-08', 'full': 'TL-MBK-09'}
RUNTIME = Path('/tmp/kubeauto-starrocks-compose-runtime.tar.gz')
ARTIFACTS = 'tests/evidence/mysql80-physical-images-20261125.json'
FILES = ['tools/mysqltools/MyBackupCli.py', 'tools-onefile.spec',
         ARTIFACTS,
         'tests/helpers/build-tools-rocky8.sh', 'tests/run_tools_regression.sh',
         'tests/helpers/mysql80-physical-live.py',
         'tests/helpers/mybackup-cli-live-regression.sh',
         'tests/helpers/mybackup-lifecycle.sh',
         'tests/helpers/starrocks-compose-live.py', 'tests/helpers/run-durable-gate.sh',
         'tests/unit/test_mysql80_physical_contract.py', 'tests/unit/test_mybackup_cli_contract.py',
         'tests/unit/test_mysql_backup_gate_contract.py',
         'dist/MyBackupCli-rocky8', 'dist/mysql-backup-build.json']


def infrastructure():
    spec = importlib.util.spec_from_file_location('mysql80_infrastructure', Path(__file__).with_name('starrocks-compose-live.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def inputs():
    return {name: sha(ROOT / name) for name in FILES}


def documentation():
    path = ROOT / 'tests/evidence/mysql80-physical-documentation-20261125.json'
    if not path.is_file():
        raise ValueError('MYSQL80_DOCUMENTATION_MISSING')
    review = json.loads(path.read_text())
    if review.get('result') != 'author-reviewed' or not review.get('checks') or not review.get('files'):
        raise ValueError('MYSQL80_DOCUMENTATION_REFUSED')
    for name, expected in review['files'].items():
        if not (ROOT / name).is_file() or sha(ROOT / name) != expected:
            raise ValueError('MYSQL80_DOCUMENTATION_STALE ' + name)


def admit(stage):
    import yaml
    matrix = yaml.safe_load((ROOT / 'tests/tools-test-matrix.yaml').read_text())
    cases = {c['id']: c for c in matrix['test_cases']}
    required = ['TL-MBK-01', 'TL-MBK-02', 'TL-MBK-04', 'TL-MBK-05', 'TL-MBK-06']
    if stage == 'full':
        required += ['TL-MBK-07', 'TL-MBK-08']
    refused = [name for name in required if cases[name]['status'] != 'pass']
    if refused:
        raise ValueError('MYSQL80_PREREQUISITES_REFUSED ' + ','.join(refused))
    if stage == 'full':
        documentation()
    build = json.loads((ROOT / 'dist/mysql-backup-build.json').read_text())
    for path, expected in build['inputs'].items():
        candidate = ROOT / path
        if path == 'tests/helpers/build-tools-rocky8.sh' and sha(candidate) != expected:
            candidate = ROOT / 'logs/tools-build-inputs' / (expected + '.sh')
        if not candidate.is_file() or sha(candidate) != expected:
            raise ValueError('MYSQL80_BUILD_INPUT_STALE ' + path)
    if sha(ROOT / 'dist/MyBackupCli-rocky8') != build['artifacts']['MyBackupCli']:
        raise ValueError('MYSQL80_BUILD_ARTIFACT_STALE')
    if stage == 'full':
        for version in VERSIONS:
            verify_record(ROOT / ('tests/evidence/mysql80-physical-' + version + '.json'))
    publication = json.loads((ROOT / ARTIFACTS).read_text())
    if (publication['conclusion'] != 'success' or not publication.get('files')
            or set(publication['images']) != {'8.0.33', '8.0.46', 'pxb'}):
        raise ValueError('MYSQL80_IMAGE_EVIDENCE_REFUSED')
    for image in publication['images'].values():
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image['digest']):
            raise ValueError('MYSQL80_IMAGE_DIGEST_MISSING')
        if (set(image['platforms']) != {'linux/amd64', 'linux/arm64'}
                or image['talkedu'] != image['dockerhub']
                or image['talkedu'] != {'digest': image['digest'], 'platforms': image['platforms']}):
            raise ValueError('MYSQL80_DUAL_REGISTRY_REFUSED')
    for path, expected in publication['files'].items():
        if sha(ROOT / path) != expected:
            raise ValueError('MYSQL80_IMAGE_EVIDENCE_STALE ' + path)
    proof = json.loads(RUNTIME.with_suffix('.json').read_text())
    if sha(RUNTIME) != proof['archive_sha256']:
        raise ValueError('MYSQL80_RUNTIME_ARCHIVE_STALE')
    return publication['images'], proof


def scenarios(stage):
    if stage == 'probe':
        return [('8.0.33', 'host', 'OFF')]
    return [(version, mode, gtid)
            for version in (VERSIONS if stage == 'full' else (stage,))
            for mode in ('host', 'container') for gtid in ('OFF', 'ON')]


def markers(stage, label):
    expected = ['MYSQL80_HOST_PASS os=' + HOSTS[label][1],
                'TOOLS_CLEAN_VERIFY_PASS scope=mysql80-physical', 'MYSQL80_EXIT rc=0']
    for version, mode, gtid in scenarios(stage):
        expected.append('MYBACKUP_LIFECYCLE_PASS version=' + version + ' gtid=' + gtid + ' mode=' + mode)
    return expected


def verify_host(record, stage, label):
    if record['markers'] != markers(stage, label) or len(record['durable']) != 2:
        raise ValueError('MYSQL80_HOST_SCOPE_REFUSED ' + label)
    for name, expected in record['files'].items():
        if sha(ROOT / name) != expected:
            raise ValueError('MYSQL80_RAW_EVIDENCE_STALE ' + name)
    for name in record['durable']:
        if (ROOT / name).read_text().strip() != '0':
            raise ValueError('MYSQL80_DURABLE_REFUSED ' + name)
    text = (ROOT / record['log']).read_text()
    for marker in record['markers']:
        if marker not in text:
            raise ValueError('MYSQL80_MARKER_MISSING ' + marker)
    if re.search(r'Traceback|_FAILED\b|_FAIL\b|FALSE_SUCCESS|MYSQL80_EXIT rc=[1-9]', text):
        raise ValueError('MYSQL80_FAILURE_MARKER ' + record['log'])


def compatible_inputs(recorded):
    """Reuse execution evidence only across proven unrelated gate edits.

    Keep the actual historical hashes. A checksum-verified source snapshot must
    prove that the physical dispatch and every execution function are unchanged.
    Product, fixture, artifact and contract changes still invalidate the record.
    """
    current = inputs()
    if recorded.keys() != current.keys():
        return False
    for name, expected in recorded.items():
        if expected == current[name]:
            continue
        if name == 'tests/unit/test_mysql80_physical_contract.py':
            snapshot = ROOT / 'logs/mysql80-execution-inputs' / (expected + '.py')
            if (snapshot.is_file() and sha(snapshot) == expected and
                    snapshot.read_text().count('root@192.168.47.131') == 1 and
                    (ROOT / name).read_text() == snapshot.read_text().replace('root@192.168.47.131', 'root@192.168.47.137', 1)):
                continue
            return False
        if name not in ('tests/run_tools_regression.sh', 'tests/helpers/build-tools-rocky8.sh', 'tests/helpers/mysql80-physical-live.py',
                        'tests/helpers/mybackup-cli-live-regression.sh'):
            return False
        snapshot = ROOT / 'logs/mysql80-execution-inputs' / (expected + Path(name).suffix)
        if not snapshot.is_file() or sha(snapshot) != expected:
            return False
        before, after = snapshot.read_text(), (ROOT / name).read_text()
        if name == 'tests/helpers/mybackup-cli-live-regression.sh':
            # LIFECYCLE=1 sources the lifecycle fixture and exits before this
            # legacy-only branch. Preserve every byte of its actual prefix.
            if '\necho MYBACKUP_STAGE full\n' not in before or '\necho MYBACKUP_STAGE full\n' not in after:
                return False
            if before.split('\necho MYBACKUP_STAGE full\n', 1)[0] != after.split('\necho MYBACKUP_STAGE full\n', 1)[0]:
                return False
        elif name == 'tests/helpers/build-tools-rocky8.sh':
            # These selections do not rebuild or execute MyBackupCli.
            def unchanged_builder(source):
                for selection in ('OvpnUserCli', 'NetCheckCli', 'CalicoPolicyCli',
                        'NetCheckCli,CalicoPolicyCli,OvpnUserCli,KubePublishCli,KafkaCli,MigrationCli,StarCli'):
                    source = source.replace(' || "$TOOLS_SELECTED" == '+selection+' ||', ' ||')
                return source
            if unchanged_builder(before) != unchanged_builder(after):
                return False
        elif name.endswith('.sh'):
            dispatch = '    if [[ "${MYSQL_BACKUP_STAGE:-}" == physical80-* ]]; then\n'
            if dispatch not in before or dispatch not in after:
                return False
            def physical_dispatch(source):
                header = source.split('\nusage() {', 1)[0]
                header = re.sub(r' \|\| "\$TOOLS_SELECTED" == [A-Za-z0-9,]+', '', header)
                # Other independently gated MySQL launchers may precede this
                # branch. Compare the actual physical80 handoff, not the first
                # unrelated `if` inside the shared case arm.
                branch = source.split(dispatch, 1)[1].split('    fi\n', 1)[0]
                return header, branch
            if physical_dispatch(before) != physical_dispatch(after):
                return False
        else:
            import ast
            def execution_tree(source):
                # A capacity-driven move between the two authorized Rocky8.10
                # hosts changes no MySQL command, fixture or accepted OS profile.
                source = source.replace("'root@192.168.47.131'", "'root@192.168.47.137'")
                source = source.replace("saved['inputs'] == inputs()", "compatible_inputs(saved['inputs'])")
                tree = ast.parse(source)
                tree.body = [node for node in tree.body if not (
                    isinstance(node, ast.FunctionDef) and node.name in ('compatible_inputs', 'verify_record'))]
                return ast.dump(tree, include_attributes=False)
            if execution_tree(before) != execution_tree(after):
                return False
    return True


def verify_record(path):
    proof = json.loads(path.read_text())
    if not compatible_inputs(proof['inputs']) or proof['rc'] != 0 or set(proof['hosts']) != set(HOSTS):
        raise ValueError('MYSQL80_QUALIFICATION_STALE ' + str(path))
    for label, record in proof['hosts'].items():
        verify_host(record, proof['scope'], label)
    return proof


def qualify():
    run([sys.executable, '-B', ROOT / 'tests/helpers/validate_tools_test_matrix.py', ROOT / 'tests/tools-test-matrix.yaml'])
    for name in ('mybackup-cli-live-regression.sh', 'mybackup-lifecycle.sh'):
        run(['bash', '-n', ROOT / 'tests/helpers' / name])
    with tempfile.TemporaryDirectory(prefix='kubeauto-mysql80-contract-') as temporary:
        run([sys.executable, '-B', '-m', 'unittest', 'tests.unit.test_mybackup_cli_contract',
             'tests.unit.test_mysql80_physical_contract', 'tests.unit.test_mysql_backup_gate_contract',
             'tests.unit.test_tools_matrix_contract', '-q'], cwd=ROOT,
            env=dict(os.environ, KUBEAUTO_LOG_DIR=temporary, PYTHONDONTWRITEBYTECODE='1'))
    print('MYSQL80_PREFLIGHT_PASS', flush=True)


def launch_host(label, stage, images, runtime):
    host, os_version = HOSTS[label]
    for accepted in sorted((ROOT / 'logs').glob('mysql80-' + stage + '-' + label + '-*/accepted.json'), reverse=True):
        saved = json.loads(accepted.read_text())
        if compatible_inputs(saved['inputs']) and saved['stage'] == stage and saved['host'] == label:
            verify_host(saved['record'], stage, label)
            # These branches share no database or daemon. Reuse only a proven
            # completed boundary after read-only checks of the native host.
            probe = ('import pathlib,subprocess,platform; '
                     'p=pathlib.Path; v=dict(x.split("=",1) for x in p("/etc/os-release").read_text().splitlines() if "=" in x); '
                     'assert v["ID"].strip(chr(34))=="rocky" and v["VERSION_ID"].strip(chr(34))==' + repr(os_version) + '; '
                     'assert platform.machine()=="x86_64"; '
                     'assert not p("/tmp/tools-mbk-live").exists() and not p("/tmp/tools-mybackup-file.log").exists(); '
                     'assert all(subprocess.run(["systemctl","is-active","--quiet",s]).returncode!=0 for s in ("docker","containerd","kubelet"))')
            run([*SSH, host, 'python3 -c ' + shlex.quote(probe)])
            print('MYSQL80_HOST_EVIDENCE_REUSED host=' + label + ' path=' + str(accepted), flush=True)
            return saved['record']
    ident = uuid.uuid4().hex[:12]
    remote = '/tmp/mb80-' + ident
    directory = ROOT / 'logs' / ('mysql80-' + stage + '-' + label + '-' + ident)
    directory.mkdir(mode=0o700, parents=True)
    plan = {'id': ident, 'base': remote, 'stage': stage, 'os': os_version,
            'images': images, 'runtime': runtime, 'inputs': inputs()}
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    run([*SSH, host, 'mkdir', '-m', '700', remote])
    transfer = [(Path(__file__), 'gate.py'),
                (ROOT / 'tests/helpers/starrocks-compose-live.py', 'starrocks-compose-live.py'),
                (RUNTIME, 'runtime.tar.gz'), (directory / 'plan.json', 'plan.json'),
                (ROOT / 'tests/helpers/run-durable-gate.sh', 'durable.sh'),
                (ROOT / 'tests/helpers/mybackup-cli-live-regression.sh', 'mybackup-cli-live-regression.sh'),
                (ROOT / 'tests/helpers/mybackup-lifecycle.sh', 'mybackup-lifecycle.sh'),
                (ROOT / 'dist/MyBackupCli-rocky8', 'MyBackupCli-rocky8')]
    for source, name in transfer:
        run(['scp', '-q', '-o', 'BatchMode=yes', source, host + ':' + remote + '/' + name])
    command = (f'cd {remote} || exit; exec 3<&0; nohup bash durable.sh state MYSQL80_EXIT '
               f'python3 gate.py remote <&3 >console.log 2>&1 &')
    run([*SSH, host, command], input=json.dumps(infrastructure().credential()).encode())
    started = last_progress = time.monotonic()
    offset = 1
    with (directory / 'console.log').open('wb') as log:
        while time.monotonic() - started < 5400:
            result = run([*SSH, host, f'tail -c +{offset} {remote}/console.log'], capture_output=True)
            if result.stdout:
                log.write(result.stdout); log.flush()
                sys.stdout.buffer.write(result.stdout); sys.stdout.buffer.flush()
                offset += len(result.stdout); last_progress = time.monotonic()
            status = subprocess.run([*SSH, host, f'test -s {remote}/state.finalized'], stdout=subprocess.DEVNULL)
            if status.returncode == 0:
                break
            if time.monotonic() - last_progress > 45:
                print(f'MYSQL80_HEARTBEAT host={label} stage={stage} elapsed={int(time.monotonic()-started)} next=durable-and-cleanup', flush=True)
                last_progress = time.monotonic()
            time.sleep(5)
        else:
            raise TimeoutError('MYSQL80_TIMEOUT; preserve remote run ' + host + ':' + remote)
    # Copy the canonical terminal log after the finalized fence, not the last poll.
    for name in ('console.log', 'state.exit', 'state.finalized', 'result.json', 'evidence.tar.gz', 'dockerd.log'):
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', host + ':' + remote + '/' + name, directory / name],
                       check=name in ('console.log', 'state.exit', 'state.finalized'), stderr=subprocess.DEVNULL)
    for suffix in ('exit', 'finalized'):
        if (directory / ('state.' + suffix)).read_text().strip() != '0':
            raise ValueError('MYSQL80_REMOTE_FAILURE ' + str(directory))
    text = (directory / 'console.log').read_text()
    expected = markers(stage, label)
    if any(marker not in text for marker in expected) or re.search(r'Traceback|_FAIL(?:ED)?\b|FALSE_SUCCESS', text):
        raise ValueError('MYSQL80_TERMINAL_EVIDENCE_REFUSED ' + str(directory))
    for name in ('result.json', 'evidence.tar.gz', 'dockerd.log'):
        if not (directory / name).is_file():
            raise ValueError('MYSQL80_RAW_EVIDENCE_MISSING ' + name)
    result = {'files': {str(p.relative_to(ROOT)): sha(p) for p in directory.iterdir() if p.is_file()},
              'durable': [str((directory / ('state.' + x)).relative_to(ROOT)) for x in ('exit', 'finalized')],
              'log': str((directory / 'console.log').relative_to(ROOT)), 'markers': expected}
    if inputs() != plan['inputs']:
        raise ValueError('MYSQL80_INPUT_CHANGED_DURING_RUN ' + str(directory))
    # Raw evidence is durable locally; only now remove this exact completed job.
    run([*SSH, host, 'python3 -c ' + shlex.quote('import shutil; shutil.rmtree(' + repr(remote) + ')')])
    verify_host(result, stage, label)
    (directory / 'accepted.json').write_text(json.dumps({'inputs': inputs(), 'stage': stage, 'host': label,
                                                       'record': result}, indent=2) + '\n')
    print('MYSQL80_HOST_EVIDENCE_ACCEPTED host=' + label + ' path=' + str(directory), flush=True)
    return result


def launch(stage):
    images, runtime = admit(stage)  # Refuse before lease/SSH or any mutation.
    qualify()
    with infrastructure().control_leases():
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            jobs = {label: executor.submit(launch_host, label, stage, images, runtime) for label in HOSTS}
            results, failures = {}, {}
            for job in concurrent.futures.as_completed(jobs.values()):
                label = next(label for label, candidate in jobs.items() if candidate is job)
                try:
                    results[label] = job.result()
                except Exception as error:
                    failures[label] = str(error)
                    print('MYSQL80_BRANCH_ERROR host=' + label + ' reason=' + str(error), flush=True)
            if failures:
                # Each successful host has its own accepted.json and immutable
                # raw evidence, even when its independent peer fails.
                raise ValueError('MYSQL80_ATTEMPT_INCOMPLETE ' + json.dumps(failures))
    proof = {'date': '2026-11-25', 'scope': stage, 'inputs': inputs(), 'rc': 0, 'hosts': results,
             'boundaries': ['AMD64; native Rocky 8.10 and 9.8; standalone MySQL containers; unencrypted InnoDB; GTID OFF/ON',
                            'No production access; no replication, encryption/keyring or capacity SLA claim']}
    target = ROOT / ('tests/evidence/mysql80-physical-' + stage + '.json')
    target.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    verify_record(target)
    import yaml
    path = ROOT / 'tests/tools-test-matrix.yaml'; text = path.read_text()
    if stage == 'probe':
        print('MYSQL80_PROBE_PASS evidence=' + str(target.relative_to(ROOT)), flush=True)
        return
    ident = IDS[stage]
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if 'id: ' + ident + ',' in line:
            line = line.replace('status: pending', 'status: pass').replace('status: fail', 'status: pass')
            line = re.sub(r'evidence: "[^"]*"', 'evidence: "' + str(target.relative_to(ROOT)) + '"', line)
            lines[index] = line
    text = '\n'.join(lines) + '\n'; data = yaml.safe_load(text)
    for status in ('pass', 'pending', 'fail'):
        count = sum(c['status'] == status for c in data['test_cases'])
        text = re.sub(r'(?m)^  ' + status + r': \d+$', '  ' + status + ': ' + str(count), text)
    path.write_text(text)
    print('MYSQL80_ACCEPTED id=' + ident + ' evidence=' + str(target.relative_to(ROOT)), flush=True)


def remote():
    plan = json.loads(Path('plan.json').read_text())
    if (str(Path.cwd()) != plan['base'] or not re.fullmatch('[a-f0-9]{12}', plan['id'])
            or plan['base'] != '/tmp/mb80-' + plan['id'] or plan['stage'] not in (*VERSIONS, 'full', 'probe')):
        raise ValueError('MYSQL80_REMOTE_SCOPE_REFUSED')
    for name, source in {
        'gate.py': 'tests/helpers/mysql80-physical-live.py',
        'starrocks-compose-live.py': 'tests/helpers/starrocks-compose-live.py',
        'MyBackupCli-rocky8': 'dist/MyBackupCli-rocky8',
        'mybackup-cli-live-regression.sh': 'tests/helpers/mybackup-cli-live-regression.sh',
        'mybackup-lifecycle.sh': 'tests/helpers/mybackup-lifecycle.sh',
        'durable.sh': 'tests/helpers/run-durable-gate.sh',
    }.items():
        if sha(name) != plan['inputs'][source]:
            raise ValueError('MYSQL80_TRANSFER_CHECKSUM_REFUSED ' + name)
    if sha('runtime.tar.gz') != plan['runtime']['archive_sha256']:
        raise ValueError('MYSQL80_RUNTIME_ARCHIVE_STALE')
    credentials = json.load(sys.stdin)
    infra = infrastructure()
    worker = infra.Worker(plan)
    worker.creds = credentials
    owned = worker.base
    locks = []
    for name in ('kubeauto-starrocks-compose.lock', 'kubeauto-mysql-backup-release.lock'):
        lease = open('/var/lock/' + name, 'w'); locks.append(lease)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    version = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if version['ID'].strip('"') != 'rocky' or version['VERSION_ID'].strip('"') != plan['os'] or os.uname().machine != 'x86_64':
        raise ValueError('MYSQL80_HOST_OS_REFUSED')
    for service in ('docker', 'containerd', 'kubelet'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0:
            raise ValueError('MYSQL80_FOREIGN_RUNTIME_REFUSED ' + service)
    if Path('/tmp/tools-mbk-live').exists() or Path('/tmp/tools-mybackup-file.log').exists():
        raise ValueError('MYSQL80_FOREIGN_FIXTURE_REFUSED')
    if shutil.disk_usage(owned).free < 12 * 1024**3 or int(re.search(r'MemAvailable:\s+(\d+)', Path('/proc/meminfo').read_text())[1]) < 2 * 1024**2:
        raise ValueError('MYSQL80_CAPACITY_REFUSED')
    evidence = owned / 'evidence'; evidence.mkdir(mode=0o700)
    (evidence / 'os-release').write_text(Path('/etc/os-release').read_text())
    (evidence / 'kernel').write_text(os.uname().release + '\n')
    worker.firewall = worker.firewall_rules()
    process_log = (owned / 'dockerd.log').open('w')
    def interrupted(signum, frame):
        raise InterruptedError('MYSQL80 interrupted ' + str(signum))
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    try:
        worker.runtime.mkdir(); worker.work.mkdir()
        with tarfile.open('runtime.tar.gz') as archive:
            for member in archive:
                if not member.isfile() or member.name not in plan['runtime']['files'] or '/' in member.name:
                    raise ValueError('MYSQL80_RUNTIME_MEMBER_REFUSED')
                target = worker.runtime / member.name
                target.write_bytes(archive.extractfile(member).read()); target.chmod(0o755)
                if sha(target) != plan['runtime']['files'][member.name]:
                    raise ValueError('MYSQL80_RUNTIME_CHECKSUM_REFUSED')
        run([worker.runtime / 'dockerd', '--version'])
        worker.daemon = subprocess.Popen(worker.daemon_args(), env=worker.env, stdout=process_log, stderr=process_log)
        for _ in range(60):
            if worker.daemon.poll() is not None:
                raise ValueError('MYSQL80_PRIVATE_DAEMON_EXITED')
            if subprocess.run([worker.runtime / 'docker', 'info'], env=worker.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError('MYSQL80_PRIVATE_DAEMON_READINESS')
        selected = sorted({version for version, _, _ in scenarios(plan['stage'])})
        refs = {key: worker.pull(key) for key in [*selected, 'pxb']}
        for mysql_version, mode, gtid in scenarios(plan['stage']):
            output = evidence / (mysql_version + '-' + mode + '-' + gtid); output.mkdir(mode=0o700)
            env = dict(worker.env, MYBACKUP_TOOL=str(owned / 'MyBackupCli-rocky8'),
                       MYBACKUP_VERSION=mysql_version, MYBACKUP_GTID=gtid,
                       MYBACKUP_EXECUTION_MODE=mode,
                       MYBACKUP_LIFECYCLE='1', MYBACKUP_EVIDENCE_DIR=str(output),
                       MYBACKUP_MYSQL_IMAGE=refs[mysql_version].split('@')[0] + ':' + mysql_version,
                       MYBACKUP_MYSQL_DIGEST=plan['images'][mysql_version]['digest'],
                       MYBACKUP_PXB_IMAGE=refs['pxb'].split('@')[0] + ':8.0.35-36',
                       MYBACKUP_PXB_DIGEST=plan['images']['pxb']['digest'])
            print('MYSQL80_SCENARIO_START os=' + plan['os'] + ' version=' + mysql_version + ' gtid=' + gtid + ' mode=' + mode, flush=True)
            run(['bash', owned / 'mybackup-cli-live-regression.sh'], env=env)
            if Path('/tmp/tools-mbk-live').exists() or worker.docker('ps', '-aq', capture_output=True, text=True).stdout.strip():
                raise ValueError('MYSQL80_SCENARIO_CLEANUP_FAILED')
            Path('/tmp/tools-mybackup-file.log').unlink(missing_ok=True)
            print('MYSQL80_SCENARIO_CLEAN_PASS version=' + mysql_version + ' gtid=' + gtid + ' mode=' + mode, flush=True)
    finally:
        # Only this private daemon and run directory are owned; no host service changes.
        if worker.daemon is not None and worker.daemon.poll() is None:
            remaining = subprocess.run([worker.runtime / 'docker', 'ps', '-aq'], env=worker.env, capture_output=True, text=True).stdout.split()
            if remaining:
                worker.docker('rm', '-f', *remaining)
            worker.daemon.terminate(); worker.daemon.wait(timeout=40)
        process_log.close()
        after = worker.firewall_rules()
        (evidence / 'firewall-before').write_bytes(worker.firewall)
        (evidence / 'firewall-after').write_bytes(after)
        if after != worker.firewall or str(owned) in Path('/proc/self/mountinfo').read_text():
            raise ValueError('MYSQL80_HOST_CLEANUP_FAILED')
        product_log = Path('/tmp/tools-mybackup-file.log')
        if product_log.exists():
            shutil.copy2(product_log, evidence / 'last-product.log')
            product_log.unlink()
        fixture = Path('/tmp/tools-mbk-live')
        if fixture.exists():
            shutil.rmtree(fixture)
        for path in (worker.work, worker.runtime, owned / 'docker-data', owned / 'e', owned / 'config'):
            if path.exists(): shutil.rmtree(path)
        with tarfile.open('evidence.tar.gz', 'w:gz') as archive:
            archive.add(evidence, arcname='evidence')
        print('MYSQL80_SCOPED_CLEAN_PASS os=' + plan['os'], flush=True)
    Path('result.json').write_text(json.dumps({'os': plan['os'], 'stage': plan['stage'], 'rc': 0}) + '\n')
    print('TOOLS_CLEAN_VERIFY_PASS scope=mysql80-physical', flush=True)
    print('MYSQL80_HOST_PASS os=' + plan['os'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=(*VERSIONS, 'full', 'probe', 'preflight', 'status', 'remote'))
    args = parser.parse_args()
    if args.stage == 'remote': remote()
    elif args.stage == 'preflight': qualify()
    elif args.stage == 'status':
        import yaml
        cases = yaml.safe_load((ROOT / 'tests/tools-test-matrix.yaml').read_text())['test_cases']
        for stage, ident in IDS.items():
            if next(c for c in cases if c['id'] == ident)['status'] == 'pass':
                verify_record(ROOT / ('tests/evidence/mysql80-physical-' + stage + '.json'))
    else: launch(args.stage)


if __name__ == '__main__':
    main()
