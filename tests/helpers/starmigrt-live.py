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
    files = [PRODUCT, 'tools-onefile.spec', 'tests/helpers/starmigrt-live.py',
             'tests/helpers/starrocks-compose-live.py', 'tests/helpers/run-durable-gate.sh',
             'tests/unit/test_starmigrt_contract.py', 'tests/unit/test_starmigrt_gate_contract.py',
             'tests/helpers/validate_tools_test_matrix.py', 'tests/evidence/starmigrt-images.json']
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
    if proof['inputs'] != inputs() or proof['rc'] != 0 or case['id'] not in proof['ids']:
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
    ident = uuid.uuid4().hex
    remote = '/tmp/ksd-' + ident[:16]
    directory = ROOT / 'logs' / ('starmigrt-' + stage + '-' + ident)
    directory.mkdir(mode=0o700)
    plan = dict(id=ident, base=remote, stage=stage, images={'allin1': image}, runtime=proof,
                inputs=inputs(), wheels={p.name: sha(p) for p in wheels})
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    with infra.control_leases():
        run([*SSH, HOST, 'mkdir', '-m', '700', remote])
        sources = [(Path(__file__), 'gate.py'), (Path(infra.__file__), 'starrocks-compose-live.py'),
                   (RUNTIME, 'runtime.tar.gz'), (ROOT / PRODUCT, 'StarMigrtCli.py'),
                   (ROOT / 'tools-onefile.spec', 'tools-onefile.spec'),
                   (ROOT / 'tests/helpers/run-durable-gate.sh', 'durable.sh'),
                   (directory / 'plan.json', 'plan.json')]
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
                if time.monotonic()-started > 2700:
                    raise TimeoutError('StarMigrtCli gate exceeds 2700s')
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
            for name in ('console.log', 'state.exit', 'state.finalized', 'result.json', 'StarMigrtCli', 'freeze.log', 'failure.json', 'dockerd.log', 'firewall-before.rules', 'firewall-after.rules', 'scenario.log', 'fixture.json', 'network-source.json', 'network-target.json', 'failure-sd-source.log', 'failure-sd-target.log'):
                subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', HOST + ':' + remote + '/' + name, directory / name], stderr=subprocess.DEVNULL)
        for name in ('state.exit', 'state.finalized'):
            if (directory / name).read_text().strip() != '0':
                raise ValueError('First failure preserved in ' + str(directory / 'console.log'))
        log = (directory / 'console.log').read_text()
        required = ['STARMIGRT_CASE_PASS ' + i for i in IDS[:-1]] + ['TOOLS_CLEAN_VERIFY_PASS scope=starmigrt', 'STARMIGRT_GATE_PASS stage=' + stage]
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


def verify_deferred_restore(path):
    text = '\n'.join(line for line in Path(path).read_text().splitlines() if not line.lstrip().startswith('--')).strip()
    pattern = r'ALTER\s+TABLE\s+`dst_b`\.`partition_t`\s+SET\s*\(\s*"dynamic_partition\.enable"\s*=\s*"true"\s*\)\s*;'
    if not re.fullmatch(pattern, text, re.I):
        raise ValueError('Deferred partition restore target/statement differs')


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

    def cli(self, command, *args, host=None, expected='STARMIGRT', rc=0, product=None, user='root', password=None):
        argv = [*(product or self.product), command, *map(str, args)]
        if host:
            argv += ['--host', host, '--user', user]
        started = time.monotonic()
        env = dict(self.env)
        if password is not None:
            env['SR_PASSWORD'] = password
        if argv[0] == str(self.base / 'StarMigrtCli'):
            env.pop('PYTHONPATH', None)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=240)
        self.log.write('$ ' + shlex.join(argv) + '\n' + result.stdout + result.stderr)
        self.log.flush()
        if (result.returncode != 0) != (rc != 0) or expected not in result.stdout + result.stderr:
            raise ValueError('CLI result differs: ' + command + ' rc=' + str(result.returncode) + ' (see scenario.log)')
        self.metrics.append(dict(command=command, seconds=round(time.monotonic()-started, 3), rc=result.returncode))
        return result

    def sql(self, host, sql):
        import pymysql
        with pymysql.connect(host=host, port=9030, user='root', password='', charset='utf8mb4', autocommit=True, read_timeout=90) as conn:
            with conn.cursor() as cursor:
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
        project = self.base / 'freeze'
        script = project / 'tools/starrocks/starmigration/StarMigrtCli.py'
        script.parent.mkdir(parents=True)
        shutil.copyfile(self.base / 'StarMigrtCli.py', script)
        shutil.copyfile(self.base / 'tools-onefile.spec', project / 'tools-onefile.spec')
        env = dict(self.env, TOOLS_SELECTED='StarMigrtCli')
        with (self.base / 'freeze.log').open('w') as log:
            run([sys.executable, '-m', 'PyInstaller', '--clean', 'tools-onefile.spec'], cwd=project, env=env, stdout=log, stderr=log)
        binary = self.base / 'StarMigrtCli'
        shutil.copyfile(project / 'dist/StarMigrtCli', binary)
        binary.chmod(0o755)
        self.binary = [str(binary)]
        self.cli('--help', expected='StarRocks', product=self.binary)
        self.cli('check', '--database', 'sd_b', '--exclude-table', 'skip_*', host=self.source, expected='STARMIGRT_CHECK_PASS', product=self.binary)
        print('STARMIGRT_CASE_PASS TL-SD-01 frozen_sha256=' + sha(binary), flush=True)

    def cases(self):
        self.freeze()
        self.cli('check', '--databases', 'sd_a', 'sd_b', '--exclude-table', '*.skip_*', host=self.source, expected='STARMIGRT_CHECK_PASS')
        self.cli('dump', '--database', 'sd_b', '--output', self.base / 'must-not-exist', host=self.source, expected='source-quiesced', rc=2)
        assert not (self.base / 'must-not-exist').exists()
        self.cli('check', '--database', 'sd_b', '--table', 'skip_array', host=self.source, expected='未适配数据类型', rc=1)
        self.cli('check', '--database', 'sd_a', '--table', 'skip_view', host=self.source, expected='普通内表', rc=1)
        print('STARMIGRT_CASE_PASS TL-SD-02', flush=True)
        package = self.base / 'work/package'
        selection = ['--all-databases', '--exclude-database', 'sd_ignored', '--exclude-table', '*.skip_*', '--database-map', 'sd_a=dst_a', '--database-map', 'sd_b=dst_b']
        self.cli('dump', *selection, '--source-quiesced', '--gzip', '--workers', '2', '--batch-rows', '250', '--output', package, host=self.source, expected='STARMIGRT_PASS', product=self.binary)
        self.cli('verify', '--input', package, expected='scope=local-files', product=self.binary)
        assert (package.stat().st_mode & 0o777) == 0o700
        assert all((p.stat().st_mode & 0o777) == 0o600 for p in package.iterdir())
        # Select/schema-only/data-only each execute the real tool against the real source.
        for mode in ('schema-only', 'data-only'):
            p = self.base / ('work/' + mode)
            args = ['--source-quiesced'] if mode == 'data-only' else []
            self.cli('dump', '--database', 'sd_b', '--table', 'primary_*', '--target-database', 'mode_b', '--' + mode, *args, '--output', p, host=self.source, expected='STARMIGRT_PASS')
            merged = self.base / ('work/' + mode + '.sql')
            self.cli('merge', '--input', p, '--output', merged, expected='STARMIGRT_MERGE_PASS')
            self.import_sql(merged)
            self.cli('verify', '--input', p, host=self.target, expected='STARMIGRT_VERIFY_PASS')
        print('STARMIGRT_CASE_PASS TL-SD-03 rows=65547 workers=2 batch=250 gzip=true', flush=True)
        self.cli('check', '--database', 'sd_b', '--ssl-ca', self.base / 'ca.pem', host=self.source, rc=1, expected='STARMIGRT_FAIL')
        # The positive/negative TLS handshake is exercised by a runner-owned proxy below.
        security(self)
        print('STARMIGRT_CASE_PASS TL-SD-04', flush=True)
        recovery(self)
        print('STARMIGRT_CASE_PASS TL-SD-05', flush=True)
        merged = self.base / 'work/all.sql'
        self.cli('merge', '--input', package, '--output', merged, expected='STARMIGRT_MERGE_PASS', product=self.binary)
        self.import_sql(merged)
        assert '"dynamic_partition.enable" = "false"' in self.sql(self.target, 'SHOW CREATE TABLE dst_b.partition_t')[0][1]
        verify_deferred_restore(package / 'restore_dynamic_partitions.sql')
        self.cli('verify', '--input', package, '--content', host=self.target, expected='scope=target-content', product=self.binary)
        self.sql(self.target, "UPDATE dst_b.primary_t SET txt='changed' WHERE id=1")
        self.cli('verify', '--input', package, '--content', host=self.target, expected='STARMIGRT_FAIL', rc=1)
        self.sql(self.target, "UPDATE dst_b.primary_t SET txt='new' WHERE id=1")
        self.cli('verify', '--input', package, '--content', host=self.target, expected='scope=target-content')
        print('STARMIGRT_CASE_PASS TL-SD-06', flush=True)


def security(s):
    # Least-privilege real account: metadata, dump and readback need only SELECT.
    password = uuid.uuid4().hex
    s.sql(s.source, "CREATE USER sd_reader IDENTIFIED BY '" + password + "'")
    s.cli('check', '--database', 'sd_b', host=s.source, user='sd_reader', password=password, expected='不存在或不可见', rc=1)
    s.sql(s.source, 'GRANT SELECT ON ALL TABLES IN DATABASE sd_b TO USER sd_reader')
    p = s.base / 'work/reader'
    s.cli('dump', '--database', 'sd_b', '--table', 'primary_t', '--source-quiesced', '--output', p,
          host=s.source, user='sd_reader', password=password, expected='STARMIGRT_PASS', product=s.binary)
    s.cli('verify', '--input', p, '--content', host=s.source, user='sd_reader', password=password, expected='scope=target-content')
    denied = s.cli('check', '--database', 'sd_b', host=s.source, user='sd_reader', password=password+'wrong', expected='STARMIGRT_FAIL', rc=1)
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
            with client, socket.create_connection((s.source, 9030), timeout=20) as upstream:
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
        s.cli('check', '--database', 'sd_b', '--table', 'primary_t', '--port', port, '--ssl-ca', cert, host='localhost', user='sd_reader', password=password, expected='STARMIGRT_CHECK_PASS', product=s.binary)
        s.cli('check', '--database', 'sd_b', '--port', port, '--ssl-ca', cert, host='127.0.0.1', expected='STARMIGRT_FAIL', rc=1)
        other = s.base / 'other.pem'
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', s.base / 'other.key', '-out', other, '-days', '1', '-subj', '/CN=other'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s.cli('check', '--database', 'sd_b', '--port', port, '--ssl-ca', other, host='localhost', expected='STARMIGRT_FAIL', rc=1)
        s.cli('check', '--database', 'sd_b', '--ssl-ca', cert, host=s.source, expected='STARMIGRT_FAIL', rc=1)
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
    p = s.base / 'work/recovery'
    args = ['dump', '--host', s.source, '--user', 'root', '--database', 'sd_a', '--exclude-table', 'skip_*', '--source-quiesced', '--workers', '1', '--batch-rows', '10', '--output', str(p)]
    with (s.base / 'recovery.log').open('w') as log:
        process = subprocess.Popen([*s.product, *args], env=s.env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    manifest = json.loads((p / 'manifest.json').read_text())
                    if len(manifest['completed']) == 1:
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                if process.poll() is not None:
                    raise ValueError('Dump completed before interruption boundary')
                time.sleep(.005)
            else:
                raise TimeoutError('No completed table before interruption')
            process.terminate()
            assert process.wait(timeout=15) != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    manifest = json.loads((p / 'manifest.json').read_text())
    assert manifest['status'] == 'incomplete' and len(manifest['completed']) == 1
    completed = next(iter(manifest['completed']))
    stamp = (p / completed).stat().st_mtime_ns
    digest = sha(p / completed)
    assert not list(p.glob('*.partial'))
    s.cli(*args, '--resume', expected='STARMIGRT_PASS')
    assert sha(p / completed) == digest and (p / completed).stat().st_mtime_ns == stamp
    broken = s.base / 'work/broken'
    shutil.copytree(p, broken)
    with (broken / completed).open('ab') as f:
        f.write(b'corrupt')
    s.cli('verify', '--input', broken, expected='STARMIGRT_FAIL', rc=1)
    s.cli(*args, '--resume', '--gzip', expected='STARMIGRT_FAIL', rc=1)
    s.cli('merge', '--input', p, '--output', p / 'outside.sql', expected='STARMIGRT_FAIL', rc=1)
    print('STARMIGRT_RECOVERY_REAL_PASS signal resume_reuses_table corrupt_refusal plan_refusal', flush=True)


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
