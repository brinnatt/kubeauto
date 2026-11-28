#!/usr/bin/env python3
"""Scoped StarMigrtCli gate: two disposable 3.5.7 instances, never another tool's cases."""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = 'tools/starrocks/starmigration/StarMigrtCli.py'
HOST = 'root@192.168.122.216'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
RUNTIME = Path('/tmp/kubeauto-starrocks-compose-runtime.tar.gz')
WHEELS = Path('/tmp/starmigrt-wheels')
IDS = ['TL-SD-%02d' % i for i in range(1, 8)]
# Verified against the official PyPI JSON release digests.
WHEEL_PINS = {
    "setuptools-75.8.2-py3-none-any.whl": "558e47c15f1811c1fa7adbd0096669bf76c1d3f433f58324df69f3f5ecac4e8f",
    "zipp-3.23.0-py3-none-any.whl": "071652d6115ed432f5ce1d34c336c0adfd6a884660d1e9712a256d3d3bd4b14e",
    "importlib_metadata-8.7.0-py3-none-any.whl": "e5dd1551894c77868a30651cef00984d50e1002d06942a7101d34870c5f02afd",
    "packaging-25.0-py3-none-any.whl": "29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
    "altgraph-0.17.4-py2.py3-none-any.whl": "642743b4750de17e655e6711601b077bc6598dbfa3ba5fa2b2a35ce12b508dff",
    "pyinstaller_hooks_contrib-2025.9-py3-none-any.whl": "ccbfaa49399ef6b18486a165810155e5a8d4c59b41f20dc5da81af7482aaf038",
    "pyinstaller-6.16.0-py3-none-manylinux2014_x86_64.whl": "ba618a61627ee674d6d68e5de084ba17c707b59a4f2a856084b3999bdffbd3f0",
    "pymysql-1.1.2-py3-none-any.whl": "e6b1d89711dd51f8f74b1631fe08f039e7d76cf67a42a323d3178f0f25762ed9"
}

def infrastructure():
    spec = importlib.util.spec_from_file_location('starmigrt_infrastructure', Path(__file__).with_name('starrocks-compose-live.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def inputs():
    files = [PRODUCT, 'tools/starrocks/starmigration/README.md', 'tools-onefile.spec', 'tests/helpers/starmigrt-live.py',
             'tests/helpers/starrocks-compose-live.py', 'tests/helpers/run-durable-gate.sh',
             'tests/unit/test_starmigrt_contract.py', 'tests/unit/test_starmigrt_gate_contract.py',
             'tests/helpers/validate_tools_test_matrix.py', 'tests/evidence/starmigrt-images.json', 'tests/evidence/starmigrt-sources.json']
    return {f: sha(ROOT / f) for f in files}


def matrix():
    import yaml
    data = yaml.safe_load((ROOT / 'tests/tools-test-matrix.yaml').read_text())
    cases = [c for c in data['test_cases'] if c['tool'] == 'StarMigrtCli']
    if [c['id'] for c in cases] != IDS:
        raise ValueError('StarMigrtCli case inventory changed')
    if data['starmigrt_review']['review_status'] != 'approved':
        raise ValueError('StarMigrtCli review not approved')
    for case in cases:
        for field in ('command', 'expected', 'official', 'fixture', 'cleanup', 'disproof'):
            if not case.get(field):
                raise ValueError(case['id'] + ' missing ' + field)
    return data, cases


def evidence(case):
    proof = json.loads((ROOT / case['evidence']).read_text())
    # Build reuse depends on the unchanged product and spec, not new business
    # assertions. Other cases bind the complete current test/document inputs.
    current = inputs()
    relevant = [PRODUCT, 'tools-onefile.spec'] if case['id'] == 'TL-SD-01' else list(current)
    if any(proof['inputs'].get(k) != current[k] for k in relevant) or proof['rc'] != 0 or case['id'] not in proof['ids']:
        raise ValueError(case['id'] + ' stale inputs/exit/scope')
    for name, digest in proof['files'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('Changed evidence: ' + name)
    for name in proof['durable']:
        if (ROOT / name).read_text().strip() != '0':
            raise ValueError('Nonzero durable state')
    result = json.loads((ROOT / proof['result']).read_text())
    if result['inputs'] != proof['inputs'] or not result['clean']:
        raise ValueError('Remote identity/cleanup differs')
    if sha(ROOT / proof['binary']) != result['binary_sha256']:
        raise ValueError('Frozen binary changed')
    return proof


def frozen_input():
    _, cases = matrix()
    proof = evidence(cases[0])
    path = ROOT / proof['binary']
    return path, dict(sha256=sha(path), product_sha256=proof['inputs'][PRODUCT],
                      spec_sha256=proof['inputs']['tools-onefile.spec'])


def status():
    _, cases = matrix()
    for c in cases:
        if c['status'] == 'pass':
            evidence(c)
    p = sum(c['status'] == 'pass' for c in cases)
    f = sum(c['status'] == 'fail' for c in cases)
    print('矩阵验收 %d/7 PASS，待验收 %d 项（失败 %d 项）；能力阶段 %d/1（StarMigrtCli 逻辑迁移）' % (p, 7-p, f, int(p == 7)))
    for c in cases:
        print(c['id'], c['scenario'], c['status'])


def admit(stage):
    data, cases = matrix()
    if stage == 'full':
        for case in cases[:-1]:
            if case['status'] != 'pass':
                raise ValueError('STARMIGRT_PREREQUISITE_REFUSED ' + case['id'])
            evidence(case)
    return data


def preflight():
    matrix()
    with tempfile.TemporaryDirectory(prefix='starmigrt-gate-') as tmp:
        env = dict(os.environ, KUBEAUTO_LOG_DIR=tmp, PYTHONDONTWRITEBYTECODE='1')
        run([sys.executable, '-B', '-m', 'unittest', 'tests.unit.test_starmigrt_contract',
             'tests.unit.test_starmigrt_gate_contract', '-v'], cwd=ROOT, env=env)
    print('STARMIGRT_GATE_PREFLIGHT_PASS', flush=True)


def current_images(data, infra):
    # The fixed sibling readback workflow has upstream access. Its mode refuses
    # publication and downloads no image layers; consume its exact scoped result.
    proof = json.loads((ROOT / 'tests/evidence/starmigrt-images.json').read_text())
    for name, digest in proof['files'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('Readback evidence changed: ' + name)
    plan = json.loads((ROOT / proof['plan']).read_text())
    image = data['starrocks_compose']['images']['allin1']
    if plan['mode'] != 'readback' or plan['mutation'] != 'none' or plan['items'] or plan['shards'] or len(plan['observed']) != 1:
        raise ValueError('Readback scope differs')
    observed = plan['observed'][0]
    if observed['row']['image'] + ':' + observed['row']['tag'] != image['fallback']:
        raise ValueError('Wrong readback image')
    for name in ('dockerhub', 'talkedu'):
        if observed[name] != dict(digest=image['digest'], platforms=image['platforms']):
            raise ValueError('Registry index/platform identity differs')
    print('STARMIGRT_DUAL_REGISTRY_PASS run_id=' + str(proof['run_id']) + ' ' + image['digest'], flush=True)
    return image


def record(stage, directory):
    import yaml
    accepted = IDS if stage == 'full' else IDS[:-1]
    files = [p for p in directory.iterdir() if p.is_file()]
    proof = dict(ids=accepted, inputs=inputs(), rc=0,
                 files={str(p.relative_to(ROOT)): sha(p) for p in files},
                 durable=[str((directory / n).relative_to(ROOT)) for n in ('state.exit', 'state.finalized')],
                 binary=str((directory / 'StarMigrtCli').relative_to(ROOT)),
                 result=str((directory / 'result.json').relative_to(ROOT)))
    target = ROOT / 'tests/evidence' / ('starmigrt-' + stage + '.json')
    target.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    path = ROOT / 'tests/tools-test-matrix.yaml'
    text = path.read_text()
    for ident in accepted:
        pattern = r'(?m)^(  - \{id: ' + re.escape(ident) + r',.*?status: )(?:pending|fail|pass)(.*?\})$'
        text, count = re.subn(pattern, lambda m: m[1] + 'pass' + re.sub(r', evidence: [^,}]+', '', m[2])[:-1] + ', evidence: ' + str(target.relative_to(ROOT)) + '}', text)
        if count != 1:
            raise ValueError('Cannot update scoped case ' + ident)
    # Summary is derived, not an acceptance override.
    data = yaml.safe_load(text)
    counts = {s: sum(c['status'] == s for c in data['test_cases']) for s in ('pass', 'pending', 'fail', 'na')}
    for name, count in counts.items():
        text = re.sub(r'(?m)^(  ' + name + r': )\d+$', lambda m: m[1] + str(count), text, count=1)
    path.write_text(text)
    print('STARMIGRT_EVIDENCE_ACCEPTED', ','.join(accepted), flush=True)


def poll_remote(remote):
    command = f'tail -n 5 {remote}/console.log; if test -f {remote}/state.finalized; then echo STARMIGRT_REMOTE_FINALIZED; cat {remote}/state.finalized; fi'
    for attempt in range(3):
        result = subprocess.run([*SSH, HOST, command], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        if result.returncode != 255:
            raise ValueError('Remote status command failed: ' + str(result.returncode))
        print('STARMIGRT_SSH_RETRY attempt=%d/3; remote durable gate remains supervised' % (attempt+1), flush=True)
        if attempt < 2:
            time.sleep(3)
    raise ConnectionError('Remote SSH status unavailable after three bounded attempts')


def recover(ident):
    if not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('Invalid recovery run ID')
    directories = list((ROOT / 'logs').glob('starmigrt-qualify-' + ident))
    if len(directories) != 1:
        raise ValueError('Owned failed run not found')
    directory = directories[0]
    plan = json.loads((directory / 'plan.json').read_text())
    remote = plan['base']
    if plan['id'] != ident or remote != '/tmp/ksd-' + ident[:16]:
        raise ValueError('Recovery ownership differs')
    script = """import json, pathlib, fcntl, shutil, os
b=pathlib.Path(BASE)
p=json.loads((b/'plan.json').read_text())
assert p['id']==IDENT and p['base']==str(b)
assert (b/'state.exit').read_text().strip()==(b/'state.finalized').read_text().strip()
assert (b/'state.finalized').read_text().strip()!='0'
lease=open('/var/lock/kubeauto-starrocks-compose.lock','w')
fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
for name in ('runtime','work','docker-data','docker-exec','e','config','deps','freeze'):
 assert not (b/name).exists(), 'Unclean owned directory: '+name
assert str(b) not in pathlib.Path('/proc/self/mountinfo').read_text()
assert (b/'firewall-before.rules').read_bytes()==(b/'firewall-after.rules').read_bytes()
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try: command=path.read_bytes()
 except (FileNotFoundError,PermissionError,ProcessLookupError): continue
 assert str(b/'runtime').encode() not in command and str(b/'docker-data').encode() not in command, 'Owned runtime process remains'
print('STARMIGRT_FAILED_RUN_CLEAN_VERIFIED')
shutil.rmtree(b)
print('STARMIGRT_FAILED_RUN_REMOVED')
""".replace('BASE', repr(remote)).replace('IDENT', repr(ident))
    with infrastructure().control_leases():
        for name in ('failure-sd-source.log', 'failure-sd-target.log'):
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', HOST + ':' + remote + '/' + name, directory / name], stderr=subprocess.DEVNULL)
        run([*SSH, HOST, 'python3 -c ' + shlex.quote(script)])


def launch(stage):
    data = admit(stage)  # Refusal must precede preflight, lease or SSH.
    preflight()
    infra = infrastructure()
    image = current_images(data, infra)
    proof = json.loads(RUNTIME.with_suffix('.json').read_text())
    if sha(RUNTIME) != proof['archive_sha256']:
        raise ValueError('Runtime archive differs')
    wheels = sorted(WHEELS.glob('*.whl'))
    if {p.name: sha(p) for p in wheels} != WHEEL_PINS:
        raise ValueError('Official pinned offline wheels differ')
    binary, frozen = frozen_input()
    ident = uuid.uuid4().hex
    remote = '/tmp/ksd-' + ident[:16]
    directory = ROOT / 'logs' / ('starmigrt-' + stage + '-' + ident)
    directory.mkdir(mode=0o700)
    plan = dict(id=ident, base=remote, stage=stage, images={'allin1': image}, runtime=proof,
                inputs=inputs(), wheels={p.name: sha(p) for p in wheels}, frozen=frozen)
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    with infra.control_leases():
        run([*SSH, HOST, 'mkdir', '-m', '700', remote])
        sources = [(Path(__file__), 'gate.py'), (Path(infra.__file__), 'starrocks-compose-live.py'),
                   (RUNTIME, 'runtime.tar.gz'), (ROOT / PRODUCT, 'StarMigrtCli.py'),
                   (ROOT / 'tools-onefile.spec', 'tools-onefile.spec'),
                   (ROOT / 'tests/helpers/run-durable-gate.sh', 'durable.sh'),
                   (directory / 'plan.json', 'plan.json'), (binary, 'StarMigrtCli')]
        for p in wheels:
            sources.append((p, p.name))
        for source, name in sources:
            run(['scp', '-q', '-o', 'BatchMode=yes', source, HOST + ':' + remote + '/' + name])
        command = f'cd {remote} || exit; exec 3<&0; nohup bash durable.sh state STARMIGRT_EXIT python3 -B gate.py remote <&3 >console.log 2>&1 &'
        run([*SSH, HOST, command], input=json.dumps(infra.credential()).encode())
        started = time.monotonic()
        try:
            while True:
                output = poll_remote(remote)
                print('STARMIGRT_SUPERVISOR elapsed=%ds stage=%s\n%s' % (time.monotonic()-started, stage, output), end='', flush=True)
                if 'STARMIGRT_REMOTE_FINALIZED' in output:
                    break
                if time.monotonic()-started > 7200:
                    raise TimeoutError('StarMigrtCli gate exceeds 7200s')
                time.sleep(15)
        except BaseException:
            script = f"import os,signal; from pathlib import Path; b=Path({remote!r}); p=int((b/'worker.pid').read_text()); assert Path('/proc/'+str(p)+'/cwd').resolve()==b; os.kill(p,signal.SIGTERM)"
            subprocess.run([*SSH, HOST, 'python3 -c ' + shlex.quote(script)])
            for _ in range(30):
                if subprocess.run([*SSH, HOST, 'test', '-f', remote + '/state.finalized']).returncode == 0:
                    break
                time.sleep(3)
            raise
        finally:
            for name in ('console.log', 'state.exit', 'state.finalized', 'result.json', 'StarMigrtCli', 'freeze.log', 'failure.json', 'dockerd.log', 'firewall-before.rules', 'firewall-after.rules', 'scenario.log', 'fixture.json', 'network-source.json', 'network-target.json', 'performance.json', 'performance-dump.log', 'performance-load.log', 'failure-sd-source.log', 'failure-sd-target.log'):
                subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', HOST + ':' + remote + '/' + name, directory / name], stderr=subprocess.DEVNULL)
        for name in ('state.exit', 'state.finalized'):
            if (directory / name).read_text().strip() != '0':
                raise ValueError('First failure preserved in ' + str(directory / 'console.log'))
        log = (directory / 'console.log').read_text()
        required = ['STARMIGRT_CASE_PASS ' + i for i in IDS[:-1]] + ['STARMIGRT_PARAMETERS_REAL_PASS', 'STARMIGRT_PERFORMANCE_PASS rows=10000001', 'TOOLS_CLEAN_VERIFY_PASS scope=starmigrt', 'STARMIGRT_GATE_PASS stage=' + stage]
        if not all(m in log for m in required) or re.search(r'Traceback|_FAIL(?:ED)?\b', log):
            raise ValueError('Missing markers or unexpected failure')
        if inputs() != plan['inputs']:
            raise ValueError('Inputs changed during gate')
        record(stage, directory)
        run([*SSH, HOST, 'python3 -c ' + shlex.quote('import shutil; shutil.rmtree(' + repr(remote) + ')')])
    status()


# Explicit IPAM must not intercept the controller's SNAT address (172.18.0.2
# in this lab). Docker's second default bridge previously did exactly that.
SUBNETS = {'source': '10.251.240.0/28', 'target': '10.251.240.16/28'}


def guard_subnet(subnet, routes, peer):
    candidate = ipaddress.ip_network(subnet)
    if peer and ipaddress.ip_address(peer) in candidate:
        raise ValueError('Fixture subnet overlaps management peer')
    for route in routes:
        destination = route.get('dst', 'default')
        if destination != 'default' and candidate.overlaps(ipaddress.ip_network(destination, strict=False)):
            raise ValueError('Fixture subnet overlaps existing route: ' + destination)
    return str(candidate)


class Scenario:
    def __init__(self, worker):
        self.w = worker
        self.base = worker.base
        self.product = [sys.executable, '-B', str(self.base / 'StarMigrtCli.py')]
        self.env = dict(os.environ, PYTHONPATH=str(self.base / 'deps'), SR_PASSWORD='')
        self.source = None
        self.target = None
        self.metrics = []
        self.log = (self.base / 'scenario.log').open('w')

    def cli(self, command, *args, host=None, expected='STARMIGRT', rc=0, product=None, user='root', password=None, env_extra=None):
        argv = [*(product or self.product), command, *map(str, args)]
        if host:
            argv += ['--host', host, '--user', user]
        started = time.monotonic()
        env = dict(self.env)
        if password is not None:
            env['SR_PASSWORD'] = password
        if env_extra:
            env.update(env_extra)
        if argv[0] == str(self.base / 'StarMigrtCli'):
            env.pop('PYTHONPATH', None)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=240)
        self.log.write('$ ' + shlex.join(argv) + '\n' + result.stdout + result.stderr)
        self.log.flush()
        if result.returncode != rc or expected not in result.stdout + result.stderr:
            raise ValueError('CLI result differs: ' + command + ' rc=' + str(result.returncode) + ' (see scenario.log)')
        self.metrics.append(dict(command=command, seconds=round(time.monotonic()-started, 3), rc=result.returncode))
        return result

    def sql(self, host, sql):
        import pymysql
        with pymysql.connect(host=host, port=9030, user='root', password='', charset='utf8mb4', autocommit=True, read_timeout=600) as conn:
            with conn.cursor() as cursor:
                cursor.execute('SET query_timeout = 600')
                cursor.execute('SET insert_timeout = 600')
                cursor.execute(sql)
                return cursor.fetchall()

    def start(self, role, ref):
        network = 'sd-' + role
        routes = json.loads(run(['ip', '-j', '-4', 'route', 'show'], capture_output=True, text=True).stdout)
        peer = os.environ.get('SSH_CONNECTION', '').split()
        subnet = guard_subnet(SUBNETS[role], routes, peer[0] if peer else '')
        (self.base / ('network-' + role + '.json')).write_text(json.dumps(dict(subnet=subnet, management_peer=peer[0] if peer else '', routes=routes), indent=2) + '\n')
        print('STARMIGRT_NETWORK_PREFLIGHT_PASS role=' + role + ' subnet=' + subnet, flush=True)
        self.w.docker('network', 'create', '--internal', '--subnet', subnet, '--label', 'kubeauto.starrocks.run=' + self.w.plan['id'], network, capture_output=True)
        name = 'sd-' + role
        conf = self.base / ('conf-' + role)
        conf.mkdir()
        (conf / 'fe.conf').write_text('JAVA_OPTS="-Xmx1024m -XX:+UseG1GC"\ndefault_replication_num=1\nhttp_port=8030\nrpc_port=9020\nquery_port=9030\nedit_log_port=9010\nmysql_service_nio_enabled=true\n')
        (conf / 'be.conf').write_text('mem_limit=2G\nstorage_root_path=/data/deploy/starrocks/be/storage\n')
        self.w.docker('run', '-d', '--name', name, '-v', str(conf / 'fe.conf') + ':/data/deploy/starrocks/fe/conf/fe.conf', '-v', str(conf / 'be.conf') + ':/data/deploy/starrocks/be/conf/be.conf', '--network', network, '--label', 'kubeauto.starrocks.run=' + self.w.plan['id'], '--memory', '6g', '--ulimit', 'nofile=65535:65535', ref, capture_output=True)
        host = self.w.docker('inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', name, capture_output=True, text=True).stdout.strip()
        if ipaddress.ip_address(host) not in ipaddress.ip_network(subnet):
            raise ValueError('Container IP differs from admitted subnet')
        deadline = time.monotonic() + 360
        last = None
        while time.monotonic() < deadline:
            try:
                version = self.sql(host, 'SELECT current_version()')[0][0]
                import pymysql
                with pymysql.connect(host=host, port=9030, user='root', cursorclass=pymysql.cursors.DictCursor) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute('SHOW BACKENDS')
                        rows = cursor.fetchall()
                if rows and all(str(row['Alive']).lower() == 'true' for row in rows):
                    print('STARMIGRT_INSTANCE_READY', role, version, flush=True)
                    return host
            except Exception as error:
                last = type(error).__name__
            print('STARMIGRT_INSTANCE_WAIT', role, last, flush=True)
            time.sleep(10)
        raise TimeoutError('StarRocks startup: ' + role)

    def fixture(self):
        for db in ('sd_a', 'sd_b', 'sd_ignored'):
            self.sql(self.source, 'CREATE DATABASE ' + db)
        ddl = 'CREATE TABLE {} (id BIGINT NOT NULL, txt VARCHAR(1024), deci DECIMAL(38,9), huge LARGEINT, js JSON, bin VARBINARY, day DATE, tm DATETIME, yes BOOLEAN, fl DOUBLE) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")'
        self.sql(self.source, ddl.format('sd_a.a_types'))
        self.sql(self.source, "INSERT INTO sd_a.a_types VALUES (1,'中文\\nline\\\\quote\\\'',12345678901234567890123456789.123456789,CAST('170141183460469231731687303715884105727' AS LARGEINT),parse_json('{\"b\":[1,null],\"a\":\"中文\"}'),unhex('00ff27'),'2024-02-29','2024-02-29 12:34:56',true,1.25),(2,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),(3,'',-0.000000001,CAST('-170141183460469231731687303715884105727' AS LARGEINT),parse_json('null'),unhex(''),'1970-01-01','1970-01-01 00:00:00',false,-1.5)")
        self.sql(self.source, 'CREATE TABLE sd_a.z_bulk (id BIGINT, txt VARCHAR(512)) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
        self.sql(self.source, "INSERT INTO sd_a.z_bulk SELECT generate_series, repeat('中xyz',50) FROM TABLE(generate_series(1,65536))")
        self.sql(self.source, 'CREATE TABLE sd_b.primary_t (id INT NOT NULL, txt VARCHAR(32)) PRIMARY KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
        self.sql(self.source, "INSERT INTO sd_b.primary_t VALUES (1,'old'),(2,'value')")
        self.sql(self.source, "INSERT INTO sd_b.primary_t VALUES (1,'new')")
        self.sql(self.source, 'CREATE TABLE sd_b.aggregate_t (id INT, n BIGINT SUM) AGGREGATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
        self.sql(self.source, 'INSERT INTO sd_b.aggregate_t VALUES (1,10),(1,20),(2,30)')
        self.sql(self.source, 'ADMIN SET FRONTEND CONFIG ("dynamic_partition_enable"="false")')
        self.sql(self.source, 'CREATE TABLE sd_b.partition_t (day DATE, id INT, txt VARCHAR(32)) DUPLICATE KEY(day,id) PARTITION BY RANGE(day) (PARTITION p_history VALUES LESS THAN ("2025-01-01"), PARTITION p_future VALUES LESS THAN ("2030-01-01")) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1", "dynamic_partition.enable"="true", "dynamic_partition.time_unit"="DAY", "dynamic_partition.start"="-3", "dynamic_partition.end"="3", "dynamic_partition.prefix"="p", "dynamic_partition.buckets"="1")')
        self.sql(self.source, "INSERT INTO sd_b.partition_t VALUES ('2024-02-29',1,'historical'),('2028-01-01',2,'future')")
        self.sql(self.source, 'CREATE TABLE sd_b.skip_array (id INT, a ARRAY<INT>) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
        self.sql(self.source, 'CREATE VIEW sd_a.skip_view AS SELECT id FROM sd_a.a_types')
        self.sql(self.source, 'CREATE TABLE sd_b.decimal_t (id INT, v DECIMAL(38,38)) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
        self.sql(self.source, 'INSERT INTO sd_b.decimal_t VALUES (1,0.00000000012345678901234567890123456789),(2,0.00000000000000000000000000000000000001)')
        snapshots = {db + '.' + name: self.sql(self.source, 'SHOW CREATE TABLE ' + db + '.' + name)[0][1]
                     for db, names in [('sd_a', ['a_types', 'z_bulk']), ('sd_b', ['primary_t', 'aggregate_t', 'partition_t', 'decimal_t'])]
                     for name in names}
        (self.base / 'fixture.json').write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + '\n')
        print('STARMIGRT_FIXTURE_PASS rows=65547 databases=2', flush=True)

    def import_sql(self, path):
        # External mysql client is the documented consumer, not replacement tool evidence.
        self.w.docker('exec', '-i', 'sd-target', 'mysql', '--protocol=TCP', '-h', '127.0.0.1', '-P', '9030', '-u', 'root', '--default-character-set=utf8mb4', input=Path(path).read_bytes(), stdout=self.log, stderr=self.log)

    def freeze(self):
        binary = self.base / 'StarMigrtCli'
        expected = self.w.plan['frozen']
        if (sha(binary) != expected['sha256'] or sha(self.base / 'StarMigrtCli.py') != expected['product_sha256']
                or sha(self.base / 'tools-onefile.spec') != expected['spec_sha256']):
            raise ValueError('Frozen reuse source/spec/artifact identity differs')
        (self.base / 'freeze.log').write_text('FROZEN_REUSE ' + json.dumps(expected, sort_keys=True) + '\n')
        binary.chmod(0o755)
        self.binary = [str(binary)]
        self.cli('--help', expected='StarRocks', product=self.binary)
        self.cli('dump', '--check', '--database', 'sd_b', '--exclude-table', 'skip_*', host=self.source, expected='STARMIGRT_CHECK_OK', product=self.binary)
        print('STARMIGRT_CASE_PASS TL-SD-01 frozen_sha256=' + sha(binary), flush=True)

    def cases(self):
        self.freeze()
        common = ['--databases', 'sd_a', 'sd_b', '--exclude-table', '*.skip_*']
        self.cli('dump', '--check', *common, host=self.source, expected='STARMIGRT_CHECK_OK')
        self.cli('dump', '--database', 'sd_b', '--output', self.base/'work/invalid.sql', host=self.source, expected='未适配数据类型', rc=1)
        assert not (self.base/'work/invalid.sql').exists()
        self.cli('dump', '--check', '--database', 'sd_a', '--table', 'skip_view', host=self.source, expected='普通内表', rc=1)
        self.cli('dump', '--host', self.source, '--user', 'root', '--output', self.base/'work/none.sql', expected='required', rc=2)
        print('STARMIGRT_CASE_PASS TL-SD-02', flush=True)
        dumpfile = self.base/'work/all.sql.gz'
        selection = ['--all-databases', '--exclude-database', 'sd_ignored', '--exclude-table', '*.skip_*',
                     '--database-map', 'sd_a=dst_a', '--database-map', 'sd_b=dst_b']
        self.cli('dump', *selection, '--batch-rows', '250', '--output', dumpfile, host=self.source, expected='STARMIGRT_DUMP_OK', product=self.binary)
        assert dumpfile.stat().st_mode & 0o777 == 0o600
        for mode in ('schema-only', 'data-only'):
            path = self.base/('work/'+mode+'.sql')
            self.cli('dump', '--database', 'sd_b', '--table', 'primary_*', '--target-database', 'mode_b',
                     '--'+mode, '--output', path, host=self.source, expected='STARMIGRT_DUMP_OK')
            self.cli('load', '--input', path, host=self.target, expected='STARMIGRT_LOAD_OK')
        assert self.sql(self.target, 'SELECT * FROM mode_b.primary_t ORDER BY id') == ((1, 'new'), (2, 'value'))
        print('STARMIGRT_CASE_PASS TL-SD-03 rows=65547 batch=250 gzip=true', flush=True)
        security(self)
        print('STARMIGRT_CASE_PASS TL-SD-04', flush=True)
        recovery(self)
        print('STARMIGRT_CASE_PASS TL-SD-05', flush=True)
        self.cli('load', '--input', dumpfile, host=self.target, expected='STARMIGRT_LOAD_OK', product=self.binary)
        assert '"dynamic_partition.enable" = "false"' in self.sql(self.target, 'SHOW CREATE TABLE dst_b.partition_t')[0][1]
        self.cli('load', '--input', dumpfile, host=self.target, expected='已存在', rc=1, product=self.binary)
        # A normal MySQL client consumes the same standalone file without preprocessing.
        plain = self.base/'work/mysql.sql'
        self.cli('dump', '--database', 'sd_a', '--table', 'a_types', '--target-database', 'mysql_dst',
                 '--output', plain, host=self.source, expected='STARMIGRT_DUMP_OK')
        self.import_sql(plain)
        assert self.sql(self.target, 'SELECT COUNT(*) FROM mysql_dst.a_types')[0][0] == 3
        assert self.sql(self.target, 'SELECT * FROM mysql_dst.a_types ORDER BY id') == self.sql(self.source, 'SELECT * FROM sd_a.a_types ORDER BY id')
        mismatch=self.base/'work/mismatch.sql'
        self.cli('dump', '--database', 'sd_b', '--table', 'primary_t', '--target-database', 'mismatch_dst',
                 '--output', mismatch, host=self.source, expected='STARMIGRT_DUMP_OK')
        # A deliberately inconsistent trusted fixture has a valid file checksum,
        # but different row content; automatic readback must reject it.
        body=mismatch.read_bytes().split(b'-- StarMigrtCli complete sha256=')[0]
        assert b"'new'" in body
        body=body.replace(b"'new'", b"'bad'")
        mismatch.write_bytes(body+b'-- StarMigrtCli complete sha256='+hashlib.sha256(body).hexdigest().encode()+b'\n')
        self.cli('load', '--input', mismatch, host=self.target, expected='内容校验不一致', rc=1)
        print('STARMIGRT_CASE_PASS TL-SD-06 tool_load_content_and_mysql_client', flush=True)
        parameter_cases(self)
        performance(self)


def parameter_cases(s):
    """Missing options use the real product CLI; refusals must precede writes."""
    password = uuid.uuid4().hex
    s.sql(s.source, "CREATE USER sd_parameter IDENTIFIED BY '" + password + "'")
    s.sql(s.source, 'GRANT SELECT ON ALL TABLES IN DATABASE sd_b TO USER sd_parameter')
    common = ['--database', 'sd_b', '--table', 'primary_t', '--check',
              '--connect-timeout', '2', '--query-timeout', '7']
    s.cli('dump', *common, '--password-env', 'SD_CUSTOM_PASSWORD', host=s.source, user='sd_parameter',
          env_extra={'SR_PASSWORD': 'deliberately-wrong', 'SD_CUSTOM_PASSWORD': password}, expected='STARMIGRT_CHECK_OK')
    secret = s.base/'work/parameter.password'
    secret.write_text(password+'\r\n'); secret.chmod(0o600)
    try:
        s.cli('dump', *common, '--password-env', 'SD_MISSING', '--password-file', secret,
              host=s.source, user='sd_parameter', password='deliberately-wrong', expected='STARMIGRT_CHECK_OK')
        secret.chmod(0o644)
        s.cli('dump', *common, '--password-file', secret, host=s.source, expected='密码文件必须属于', rc=1)
        secret.chmod(0o600)
        os.chown(secret,65534,65534)
        s.cli('dump', *common, '--password-file', secret, host=s.source, expected='密码文件必须属于', rc=1)
    finally:
        os.chown(secret,os.getuid(),os.getgid());secret.unlink()
    s.cli('dump', *common, '--password-env', 'SD_MISSING', host=s.source, expected='非交互运行', rc=1)
    # A real socket that accepts TCP but sends no handshake proves the driver's
    # read deadline (query-timeout + 30s), without blackholing a lab address.
    import threading
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(1)
    stop=threading.Event();accepted=[]
    def silent_peer():
        while not stop.is_set():
            try:client,_=listener.accept()
            except socket.timeout:continue
            except OSError:break
            accepted.append(client);stop.wait(40);client.close()
    thread=threading.Thread(target=silent_peer);thread.start()
    started=time.monotonic()
    try:
        s.cli('dump', *common[:-4], '--query-timeout','1','--connect-timeout','1',
              '--port',listener.getsockname()[1],host='127.0.0.1',expected='2013',rc=1)
        elapsed=time.monotonic()-started
        assert accepted and 29<=elapsed<=40, ('read deadline',elapsed)
    finally:
        stop.set();listener.close();thread.join(3)
        for client in accepted:client.close()
    # Closed localhost port gives a genuine connect failure, without external IPs.
    closed=socket.socket();closed.bind(('127.0.0.1',0));port=closed.getsockname()[1];closed.close()
    s.cli('dump', *common, '--port',port, host='127.0.0.1',expected='2003',rc=1)
    s.sql(s.source,'CREATE DATABASE sd_parameter')
    s.sql(s.source,'CREATE TABLE sd_parameter.wide_t (id INT, a VARCHAR(800000), b VARCHAR(800000)) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 1 PROPERTIES("replication_num"="1")')
    s.sql(s.source,"INSERT INTO sd_parameter.wide_t SELECT generate_series,repeat('中',100000),repeat('文',100000) FROM TABLE(generate_series(1,5))")
    output=s.base/'work/bytes.sql'
    s.cli('dump','--database','sd_parameter','--table','wide_t','--target-database','bytes_dst',
          '--batch-rows','100','--batch-mib','1','--replication-num','1','--output',output,
          host=s.source,expected='STARMIGRT_DUMP_OK')
    # Each ~600 KiB row must occupy a separate INSERT under the 1 MiB budget.
    inserts=[line for line in output.open() if line.startswith('INSERT ')]
    assert len(inserts)==5,len(inserts)
    s.cli('load','--input',output,'--connect-timeout','2','--query-timeout','30',host=s.target,expected='STARMIGRT_LOAD_OK')
    assert self_replication(s.sql(s.target,'SHOW CREATE TABLE bytes_dst.wide_t')[0][1])==1
    s.sql(s.source,"INSERT INTO sd_parameter.wide_t VALUES (6,repeat('中',200000),repeat('文',200000))")
    failed=s.base/'work/too-wide.sql'
    s.cli('dump','--database','sd_parameter','--table','wide_t','--batch-mib','1','--output',failed,
          host=s.source,expected='单行超过 --batch-mib',rc=1)
    assert not failed.exists() and not failed.with_name(failed.name+'.partial').exists()
    # The shared connection flags must also work on the write-side entry point.
    empty=s.base/'work/load.password';empty.write_text('');empty.chmod(0o600)
    for name,auth,environment in [('env',['--password-env','SD_LOAD_PASSWORD'],{'SR_PASSWORD':'wrong','SD_LOAD_PASSWORD':''}),
                                  ('file',['--password-file',empty],{'SR_PASSWORD':'wrong'})]:
        path=s.base/('work/load-'+name+'.sql')
        s.cli('dump','--database','sd_b','--table','primary_t','--target-database','load_'+name,
              '--output',path,host=s.source,expected='STARMIGRT_DUMP_OK')
        s.cli('load','--input',path,*auth,env_extra=environment,host=s.target,expected='STARMIGRT_LOAD_OK')
    empty.unlink()
    assert password not in (s.base/'scenario.log').read_text()
    print('STARMIGRT_PARAMETERS_REAL_PASS credentials file_owner byte_batches replication timeouts',flush=True)


def self_replication(ddl):
    return int(re.search(r'"replication_num"\s*=\s*"(\d+)"',ddl)[1])


def measured_cli(s, name, command, *args, timeout=5400):
    """Run the normal source CLI and sample its own RSS/CPU, never a wrapper."""
    argv=[*s.product,command,*map(str,args)]
    path=s.base/('performance-'+name+'.log')
    started=time.monotonic();peak=0;cpu=0;next_heartbeat=started
    with path.open('w') as log:
        proc=subprocess.Popen(argv,env=s.env,stdout=log,stderr=subprocess.STDOUT)
        try:
            while proc.poll() is None:
                try:
                    status=Path('/proc/%d/status'%proc.pid).read_text()
                    match=re.search(r'^VmRSS:\s+(\d+) kB',status,re.M)
                    if match:peak=max(peak,int(match[1])*1024)
                    values=Path('/proc/%d/stat'%proc.pid).read_text().split(') ',1)[1].split()
                    cpu=(int(values[11])+int(values[12]))/os.sysconf('SC_CLK_TCK')
                except (FileNotFoundError,ProcessLookupError):pass
                now=time.monotonic()
                if now>=next_heartbeat:
                    tail=path.read_text()[-1400:]
                    print('STARMIGRT_PERFORMANCE_HEARTBEAT phase=%s elapsed=%.1fs peak_rss_mib=%.1f\n%s'%
                          (name,now-started,peak/2**20,tail),flush=True)
                    next_heartbeat=now+15
                if now-started>timeout:raise TimeoutError('Performance '+name+' exceeds %ds'%timeout)
                time.sleep(.2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
    output=path.read_text()
    expected='STARMIGRT_DUMP_OK' if command=='dump' else 'STARMIGRT_LOAD_OK'
    if proc.returncode!=0 or expected not in output:
        raise ValueError('Performance '+name+' rc='+str(proc.returncode)+'; see '+path.name)
    return dict(seconds=round(time.monotonic()-started,3),peak_rss_bytes=peak,cpu_seconds=round(cpu,3),
                rc=proc.returncode,log=path.name)


def performance(s):
    rows=10000001
    s.sql(s.source,'CREATE DATABASE sd_performance')
    s.sql(s.source,'CREATE TABLE sd_performance.large_t (id BIGINT NOT NULL, txt VARCHAR(32768), js JSON) DUPLICATE KEY(id) DISTRIBUTED BY HASH(id) BUCKETS 4 PROPERTIES("replication_num"="1")')
    # Every row has variable text and nested JSON; every thousandth row also
    # carries ~28 KiB of long text/JSON. No NULL-only or empty-payload shortcut.
    expression="SELECT generate_series,concat(if(mod(generate_series,1000)=0,repeat('长文本',2048),repeat('数据abc',8)),cast(generate_series AS VARCHAR)),json_object('id',generate_series,'nested',parse_json('{\"active\":true,\"tags\":[\"一\",\"two\"]}'),'payload',if(mod(generate_series,1000)=0,repeat('json字段',1024),'普通值')) FROM TABLE(generate_series(1,%d))"
    print('STARMIGRT_PERFORMANCE_FIXTURE creating_rows=%d'%rows,flush=True)
    s.sql(s.source,'INSERT INTO sd_performance.large_t '+expression%rows)
    stats=s.sql(s.source,'SELECT count(*),sum(length(txt)),max(length(txt)),sum(length(cast(js AS VARCHAR))),max(length(cast(js AS VARCHAR))) FROM sd_performance.large_t')[0]
    assert stats[0]==rows and stats[2]>16000 and stats[4]>8000,stats
    # Compare equal 100k-row payloads before the full-size tuned migration.
    s.sql(s.source,'CREATE TABLE sd_performance.sample_t LIKE sd_performance.large_t')
    s.sql(s.source,'INSERT INTO sd_performance.sample_t '+expression%100000)
    samples={}
    for name,batch in [('default',[]),('tuned',['--batch-rows','10000','--batch-mib','8'])]:
        path=s.base/('work/sample-'+name+'.sql')
        begun=time.monotonic()
        s.cli('dump','--database','sd_performance','--table','sample_t','--target-database','sample_'+name,
              '--output',path,*batch,host=s.source,expected='STARMIGRT_DUMP_OK')
        export=time.monotonic()-begun;begun=time.monotonic()
        s.cli('load','--input',path,host=s.target,expected='STARMIGRT_LOAD_OK')
        samples[name]=dict(rows=100000,dump_seconds=round(export,3),load_including_verification_seconds=round(time.monotonic()-begun,3),sql_bytes=path.stat().st_size)
        path.unlink()
    path=s.base/'work/ten-million.sql'
    dumped=measured_cli(s,'dump','dump','--host',s.source,'--user','root','--database','sd_performance','--table','large_t',
                        '--target-database','large_dst','--output',path,'--batch-rows','10000','--batch-mib','8')
    loaded=measured_cli(s,'load','load','--host',s.target,'--user','root','--input',path)
    actual=s.sql(s.target,'SELECT count(*),sum(length(txt)),max(length(txt)),sum(length(cast(js AS VARCHAR))),max(length(cast(js AS VARCHAR))) FROM large_dst.large_t')[0]
    assert actual==stats,(actual,stats)
    assert max(dumped['peak_rss_bytes'],loaded['peak_rss_bytes'])<512*2**20,'CLI RSS exceeds 512 MiB budget'
    report=dict(rows=rows,source_stats=list(stats),target_stats=list(actual),sql_bytes=path.stat().st_size,
                batch_rows=10000,batch_mib=8,dump=dumped,load_including_file_audit_and_content_verification=loaded,
                samples=samples,host=dict(cpu=os.cpu_count(),mem_total_kib=int(re.search(r'MemTotal:\s+(\d+)',Path('/proc/meminfo').read_text())[1]),
                os_release=Path('/etc/os-release').read_text(),kernel=os.uname().release),
                topology='Two isolated allin1 3.5.7 instances on one x86_64 host; each FE 1GiB/BE 2GiB; container limit 6GiB; four buckets, one replica',
                transfer_included=False,source_writes_paused=True)
    (s.base/'performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    path.unlink()
    print('STARMIGRT_PERFORMANCE_PASS rows=%d dump_seconds=%.3f load_with_verification_seconds=%.3f sql_bytes=%d'%
          (rows,dumped['seconds'],loaded['seconds'],report['sql_bytes']),flush=True)


def security(s):
    # Least-privilege real account: metadata, dump and readback need only SELECT.
    password = uuid.uuid4().hex
    s.sql(s.source, "CREATE USER sd_reader IDENTIFIED BY '" + password + "'")
    s.cli('dump', '--check', '--database', 'sd_b', host=s.source, user='sd_reader', password=password, expected='不存在或不可见', rc=1)
    s.sql(s.source, 'GRANT SELECT ON ALL TABLES IN DATABASE sd_b TO USER sd_reader')
    p = s.base / 'work/reader.sql'
    s.cli('dump', '--database', 'sd_b', '--table', 'primary_t', '--output', p,
          host=s.source, user='sd_reader', password=password, expected='STARMIGRT_DUMP_OK', product=s.binary)
    denied = s.cli('dump', '--check', '--database', 'sd_b', host=s.source, user='sd_reader', password=password+'wrong', expected='STARMIGRT_ERROR', rc=1)
    assert password not in denied.stdout + denied.stderr
    # Actual authenticated StarRocks traffic through TLS, with verified CA/hostname.
    import ssl
    import struct
    import threading
    cert = s.base / 'ca.pem'
    key = s.base / 'key.pem'
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key, '-out', cert,
         '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    threads = []
    errors = []
    tls_refusals = []
    upstream_host = [s.source]
    def packet(conn):
        def take(n):
            data = b''
            while len(data) < n:
                block = conn.recv(n-len(data))
                if not block:
                    raise EOFError()
                data += block
            return data
        head = take(4)
        return head, take(int.from_bytes(head[:3], 'little'))
    def handle(client):
        try:
            with client, socket.create_connection((upstream_host[0], 9030), timeout=20) as upstream:
                head, greeting = packet(upstream)
                data = bytearray(greeting)
                offset = data.index(0, 1) + 1 + 4 + 8 + 1
                data[offset:offset+2] = (int.from_bytes(data[offset:offset+2], 'little') | 2048).to_bytes(2, 'little')
                client.sendall(head + data)
                packet(client)  # Client SSLRequest; plaintext driver auth has not happened.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                with context.wrap_socket(client, server_side=True) as secure:
                    h, auth = packet(secure)
                    auth = (int.from_bytes(auth[:4], 'little') & ~2048).to_bytes(4, 'little') + auth[4:]
                    upstream.sendall(h[:3] + b'\x01' + auth)
                    h, response = packet(upstream)
                    secure.sendall(h[:3] + b'\x03' + response)
                    def copy(src, dst):
                        try:
                            while True:
                                block = src.recv(65536)
                                if not block:
                                    break
                                dst.sendall(block)
                        except (OSError, ssl.SSLError):
                            pass
                        with contextlib.suppress(OSError):
                            dst.shutdown(socket.SHUT_WR)
                    t = threading.Thread(target=copy, args=(secure, upstream))
                    t.start()
                    copy(upstream, secure)
                    t.join(5)
        except ssl.SSLError as error:
            tls_refusals.append(error.reason)
        except (EOFError, ConnectionError, OSError):
            pass
        except Exception as e:
            errors.append(type(e).__name__)
    def accept():
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=handle, args=(client,))
            threads.append(t)
            t.start()
    thread = threading.Thread(target=accept)
    thread.start()
    try:
        s.cli('dump', '--check', '--database', 'sd_b', '--table', 'primary_t', '--port', port, '--ssl-ca', cert, host='localhost', user='sd_reader', password=password, expected='STARMIGRT_CHECK_OK', product=s.binary)
        tls_sql=s.base/'work/tls.sql'
        s.cli('dump','--database','sd_b','--table','primary_t','--target-database','tls_dst','--output',tls_sql,
              '--port',port,'--ssl-ca',cert,host='localhost',user='sd_reader',password=password,expected='STARMIGRT_DUMP_OK')
        upstream_host[0]=s.target
        s.cli('load','--input',tls_sql,'--port',port,'--ssl-ca',cert,host='localhost',expected='STARMIGRT_LOAD_OK')
        upstream_host[0]=s.source
        s.cli('dump', '--check', '--database', 'sd_b', '--port', port, '--ssl-ca', cert, host='127.0.0.1', expected='STARMIGRT_ERROR', rc=1)
        other = s.base / 'other.pem'
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', s.base / 'other.key', '-out', other, '-days', '1', '-subj', '/CN=other'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s.cli('dump', '--check', '--database', 'sd_b', '--port', port, '--ssl-ca', other, host='localhost', expected='STARMIGRT_ERROR', rc=1)
        s.cli('dump', '--check', '--database', 'sd_b', '--ssl-ca', cert, host=s.source, expected='STARMIGRT_ERROR', rc=1)
        assert not errors, errors
        assert 'SSLV3_ALERT_BAD_CERTIFICATE' in tls_refusals, tls_refusals
        assert 'TLSV1_ALERT_UNKNOWN_CA' in tls_refusals, tls_refusals
    finally:
        stop.set()
        listener.close()
        thread.join(5)
        for t in threads:
            t.join(5)
    print('STARMIGRT_TLS_REAL_DRIVER_PASS verified_ca hostname_refusal ca_refusal plaintext_refusal', flush=True)


def recovery(s):
    p = s.base/'work/interrupted.sql'
    argv = ['dump', '--host', s.source, '--user', 'root', '--database', 'sd_a', '--exclude-table', 'skip_*',
            '--batch-rows', '10', '--output', str(p)]
    partial = p.with_name(p.name+'.partial')
    with (s.base/'recovery.log').open('w') as log:
        process = subprocess.Popen([*s.product, *argv], env=s.env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+90
            while time.monotonic()<deadline:
                if partial.exists() and partial.stat().st_size > 1024*1024: break
                if process.poll() is not None: raise ValueError('Dump exited before interruption boundary')
                time.sleep(.005)
            else: raise TimeoutError('No streaming output before interruption')
            process.terminate()
            assert process.wait(timeout=15) == 130
        finally:
            if process.poll() is None: process.kill();process.wait()
    assert not p.exists() and not partial.exists()
    s.cli(*argv, expected='STARMIGRT_DUMP_OK')
    broken=s.base/'work/broken.sql'
    broken.write_bytes(p.read_bytes()[:-80])
    s.cli('load', '--input', broken, host=s.target, expected='STARMIGRT_ERROR', rc=1)
    assert not self_table_exists(s, 'sd_a', 'a_types')
    # The account can create a target database/table but cannot INSERT. A real
    # import failure must stop and leave the existing target protected on retry.
    s.sql(s.target, "CREATE USER sd_writer IDENTIFIED BY ''")
    s.sql(s.target, 'CREATE DATABASE failed_dst')
    s.sql(s.target, 'GRANT CREATE TABLE ON DATABASE failed_dst TO USER sd_writer')
    s.sql(s.target, 'GRANT CREATE DATABASE ON CATALOG default_catalog TO USER sd_writer')
    s.sql(s.target, 'GRANT SELECT ON ALL TABLES IN DATABASE failed_dst TO USER sd_writer')
    fail=s.base/'work/failed-import.sql'
    s.cli('dump', '--database', 'sd_b', '--table', 'primary_t', '--target-database', 'failed_dst',
          '--output', fail, host=s.source, expected='STARMIGRT_DUMP_OK')
    s.cli('load', '--input', fail, host=s.target, user='sd_writer', expected='STARMIGRT_ERROR', rc=1)
    assert s.sql(s.target,'SELECT COUNT(*) FROM failed_dst.primary_t')[0][0] == 0
    s.cli('load', '--input', fail, host=s.target, expected='已存在', rc=1)
    print('STARMIGRT_RECOVERY_REAL_PASS signal_atomic_cleanup corrupt_refusal first_error_no_replay', flush=True)


def self_table_exists(s, db, name):
    return bool(s.sql(s.target, "SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA='%s' AND TABLE_NAME='%s'" % (db,name)))


def remote():
    plan = json.loads(Path('plan.json').read_text())
    if str(Path.cwd()) != plan['base'] or not re.fullmatch(r'/tmp/ksd-[a-f0-9]{16}', plan['base']):
        raise ValueError('Remote ownership mismatch')
    Path('worker.pid').write_text(str(os.getpid()))
    infra = infrastructure()
    worker = infra.Worker(plan)
    worker.creds = json.load(sys.stdin)
    def interrupted(sig, frame):
        raise InterruptedError('StarMigrtCli gate interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if plan['wheels'] != WHEEL_PINS:
        raise ValueError('Remote wheel allowlist differs')
    deps = worker.base / 'deps'
    deps.mkdir()
    for name, digest in plan['wheels'].items():
        path = worker.base / name
        if sha(path) != digest:
            raise ValueError('Wheel changed')
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.startswith('/') or '..' in Path(member).parts:
                    raise ValueError('Unsafe wheel')
            archive.extractall(deps)
    sys.path.insert(0, str(deps))
    with open('/var/lock/kubeauto-starrocks-compose.lock', 'w') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            worker.prepare()
            ref = worker.pull('allin1')
            s = Scenario(worker)
            s.source = s.start('source', ref)
            s.target = s.start('target', ref)
            s.fixture()
            s.cases()
        except BaseException as e:
            Path('failure.json').write_text(json.dumps(dict(type=type(e).__name__, detail=str(e))))
            for name in ('sd-source', 'sd-target'):
                with open('failure-' + name + '.log', 'w') as log:
                    with contextlib.suppress(Exception):
                        worker.docker('logs', '--tail', '150', name, stdout=log, stderr=log)
            raise
        finally:
            if worker.daemon is not None and worker.daemon.poll() is None:
                for name in ('sd-source', 'sd-target'):
                    # Only objects in this private daemon and this run's label may be removed.
                    ids = worker.docker('ps', '-aq', '--filter', 'label=kubeauto.starrocks.run=' + plan['id'], '--filter', 'name=^/' + name + '$', capture_output=True, text=True).stdout.split()
                    if ids:
                        worker.docker('rm', '-f', *ids, capture_output=True)
                    nets = worker.docker('network', 'ls', '-q', '--filter', 'label=kubeauto.starrocks.run=' + plan['id'], '--filter', 'name=^' + name + '$', capture_output=True, text=True).stdout.split()
                    if nets:
                        worker.docker('network', 'rm', *nets, capture_output=True)
            worker.cleanup()
            for name in ('deps', 'freeze', 'conf-source', 'conf-target'):
                shutil.rmtree(worker.base / name, ignore_errors=True)
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            command = proc.read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if str(worker.runtime).encode() in command or str(worker.base / 'docker-data').encode() in command:
            raise ValueError('Owned runtime process remains after cleanup')
    print('TOOLS_CLEAN_VERIFY_PASS scope=starmigrt', flush=True)
    Path('result.json').write_text(json.dumps(dict(id=plan['id'], stage=plan['stage'], clean=True, inputs=plan['inputs'],
        binary_sha256=sha('StarMigrtCli'), python=sys.version, platform=os.uname()._asdict() if hasattr(os.uname(), '_asdict') else list(os.uname()),
        libc=run(['getconf', 'GNU_LIBC_VERSION'], capture_output=True, text=True).stdout.strip(), metrics=s.metrics), indent=2) + '\n')
    print('STARMIGRT_GATE_PASS stage=' + plan['stage'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['status', 'preflight', 'qualify', 'full', 'remote'])
    parser.add_argument('--recover-run', help='Verify finalized failed run cleanup, archive diagnostics and remove only its directory')
    args = parser.parse_args()
    if args.recover_run:
        recover(args.recover_run)
        return
    if args.stage == 'remote':
        remote()
    elif args.stage == 'status':
        status()
    elif args.stage == 'preflight':
        preflight()
    else:
        launch(args.stage)


if __name__ == '__main__':
    main()
