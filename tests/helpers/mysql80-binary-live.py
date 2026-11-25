#!/usr/bin/env python3
"""Owned native systemd MySQL fixtures; clients/PXB use one pinned tool image.

Probe is qualification only. Acceptance requires both native OS hosts and every
version/GTID combination. No system package installation or production access.
"""
import argparse
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
HOSTS = {'rocky8': ('root@192.168.47.137', '8.10'), 'rocky9': ('root@192.168.122.216', '9.8')}
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
RUNTIME = Path('/tmp/kubeauto-starrocks-compose-runtime.tar.gz')
TOPOLOGIES = ('standalone', 'primary-replica', 'dual-primary')
VERSIONS = ('8.0.33', '8.0.46')
SOURCES = 'tests/evidence/mysql80-binary-sources.json'
FILES = ['tools/mysqltools/MyBackupCli.py', SOURCES, 'tests/helpers/mysql80-binary-live.py',
         'tests/helpers/mysql80-binary-artifacts.py', 'tests/helpers/starrocks-compose-live.py',
         'tests/helpers/run-durable-gate.sh', 'tests/unit/test_mysql80_binary_live_contract.py',
         'tests/unit/test_mybackup_replication_contract.py', 'tests/unit/test_mybackup_offline_contract.py',
         'tests/unit/test_mybackup_cli_contract.py', 'tests/unit/test_mysql80_binary_artifact_contract.py',
         'tests/unit/test_mybackup_native_build_contract.py', 'tests/evidence/mysql80-physical-images-20261125.json',
         'tests/run_tools_regression.sh', 'tests/helpers/build-mybackup-native.py', 'tests/evidence/mybackup-native-build-library.json', 'dist/MyBackupCli-rocky8', 'dist/mysql-backup-build.json']


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): digest.update(block)
    return digest.hexdigest()


def inputs():
    return {name: sha(ROOT/name) for name in FILES}


def scenarios(stage):
    if stage == 'probe': return [('8.0.33', 'OFF', 'standalone')]
    if stage == 'replication-probe': return [('8.0.33','OFF',t) for t in TOPOLOGIES[1:]]
    if stage not in (*TOPOLOGIES, 'full'): raise ValueError('BINARY80_STAGE_REFUSED')
    return [(version, gtid, topology) for version in VERSIONS for gtid in ('OFF', 'ON')
            for topology in (TOPOLOGIES if stage == 'full' else (stage,))]


def unit_text(base, prefix, name):
    if not re.fullmatch(r'/tmp/b80-[a-f0-9]{12}', str(base)) or name not in ('a', 'b'):
        raise ValueError('BINARY80_UNIT_SCOPE_REFUSED')
    return ('[Unit]\nDescription=Owned binary80 regression '+name+'\n[Service]\nType=simple\n'
            'User=nobody\nGroup=nobody\nWorkingDirectory='+str(base)+'\n'
            'ExecStart='+str(prefix)+'/usr/sbin/mysqld --defaults-file='+str(base/name/'my.cnf')+'\n'
            'TimeoutStopSec=45\nRestart=no\nLimitNOFILE=8192\n')


def frozen_identity(required=True):
    try:
        build=json.loads((ROOT/'dist/mysql-backup-build.json').read_text())
        if build['inputs']['tools/mysqltools/MyBackupCli.py']!=sha(ROOT/'tools/mysqltools/MyBackupCli.py'):
            raise ValueError('stale source')
        if build['artifacts']['MyBackupCli']!=sha(ROOT/'dist/MyBackupCli-rocky8'):
            raise ValueError('stale artifact')
        for path,expected in build['inputs'].items():
            candidate=ROOT/path
            if path=='tests/helpers/build-tools-rocky8.sh' and sha(candidate)!=expected:
                candidate=ROOT/'logs/tools-build-inputs'/(expected+'.sh')
            if not candidate.is_file() or sha(candidate)!=expected: raise ValueError('stale build input')
        result=json.loads((ROOT/build['native_mybackup_build']).read_text())
        if result['artifact']!=build['artifacts']['MyBackupCli'] or any(sha(ROOT/path)!=expected for path,expected in result['inputs'].items()):
            raise ValueError('stale native provenance')
        return build['artifacts']['MyBackupCli']
    except (OSError,ValueError,KeyError,TypeError):
        if required: raise ValueError('BINARY80_FROZEN_BUILD_REQUIRED')
        return None


def final_prerequisites():
    """A matrix label cannot replace current owning raw evidence."""
    import yaml
    cases = {case['id']: case for case in yaml.safe_load(
        (ROOT/'tests/tools-test-matrix.yaml').read_text())['test_cases']}
    required = ['TL-MBK-%02d'%n for n in range(1,13)]+['TL-X-06']
    refused = [key for key in required if cases.get(key, {}).get('status') != 'pass']
    if refused:
        raise ValueError('BINARY80_FINAL_PREREQUISITES_REFUSED '+','.join(refused))
    physical = load('mysql80-physical-live')
    physical.documentation()
    for stage in ('8.0.33', '8.0.46', 'full'):
        physical.verify_record(ROOT/('tests/evidence/mysql80-physical-'+stage+'.json'))
    audit = load('tools-production-audit')
    for owner in ('mysql84-physical-requal-20261125', 'mysql-backup-portability-20261125'):
        audit.verify_record(owner)
    for topology in TOPOLOGIES:
        verify_record(ROOT/('tests/evidence/mysql80-binary-'+topology+'.json'))


def admit(stage):
    if stage == 'full':
        final_prerequisites()
    frozen_identity(required=stage!='probe')
    artifacts = load('mysql80-binary-artifacts')
    records = artifacts.catalog()
    cache = Path.home()/'.cache/kubeauto/mysql80-binary'
    for record in records.values():
        for item in (record, *record.get('companions', {}).values()):
            path = cache/(item['sha256']+'.rpm')
            if not path.is_file() or sha(path) != item['sha256']:
                raise ValueError('BINARY80_ARTIFACT_NOT_PREPARED')
    source_data = json.loads((ROOT/SOURCES).read_text())
    for name, expected in source_data['source_files'].items():
        if sha(ROOT/name) != expected:
            raise ValueError('BINARY80_SOURCE_EVIDENCE_STALE '+name)
    publication = json.loads((ROOT/'tests/evidence/mysql80-physical-images-20261125.json').read_text())
    image = publication['images']['pxb']
    if (publication['conclusion'] != 'success' or not publication.get('files') or
            image['talkedu'] != image['dockerhub'] or
            image['talkedu'] != {'digest': image['digest'], 'platforms': image['platforms']} or
            set(image['platforms']) != {'linux/amd64', 'linux/arm64'} or
            not re.fullmatch('sha256:[a-f0-9]{64}', image['digest'])):
        raise ValueError('BINARY80_PXB_PROVENANCE_REFUSED')
    for name, expected in publication['files'].items():
        if sha(ROOT/name) != expected:
            raise ValueError('BINARY80_PXB_PUBLICATION_STALE '+name)
    runtime = json.loads(RUNTIME.with_suffix('.json').read_text())
    if sha(RUNTIME) != runtime['archive_sha256']: raise ValueError('BINARY80_RUNTIME_REFUSED')
    return records, {'pxb': image}, runtime


def qualify():
    load('mysql80-binary-artifacts').preflight()
    import tempfile
    with tempfile.TemporaryDirectory(prefix='binary80-runner-contract-') as temporary:
        run([sys.executable, '-B', '-m', 'unittest', 'tests.unit.test_mysql80_binary_live_contract', '-q'],
            cwd=ROOT, env=dict(os.environ, KUBEAUTO_LOG_DIR=temporary, MYSQLBACKUP_LOG_FILE=temporary+'/product.log'))
    print('BINARY80_RUNNER_PREFLIGHT_PASS', flush=True)


class Fixture:
    def __init__(self, worker, prefix, version, gtid, topology, evidence):
        self.worker, self.base, self.prefix = worker, worker.base, prefix
        self.version, self.gtid, self.topology, self.evidence = version, gtid, topology, evidence
        self.nodes = ('a',) if topology == 'standalone' else ('a', 'b')
        self.password = secrets.token_hex(16)
        self.env = dict(worker.env, PATH=str(self.base/'bin')+':'+worker.env['PATH'],
                        MYSQLBACKUP_LOG_FILE=str(evidence/'product.log'), TZ='Asia/Shanghai')
        self.units = []

    def sql(self, name, sql, password=True):
        cmd = [self.base/'bin/mysql', '--no-defaults', '-uroot', '--socket='+str(self.base/name/'data/mysql.sock'), '-NB']
        if password: cmd.append('--password='+self.password)
        result = subprocess.run([str(x) for x in cmd], input=sql, capture_output=True, text=True, env=self.env)
        if result.returncode:
            raise RuntimeError('BINARY80_SQL_ERROR '+name+' '+result.stderr.replace(self.password, '***'))
        return result.stdout.strip()

    def tool(self, name, operation, *args, refuse=None, repository=None):
        path = self.base/name/'backup.json'
        if repository:
            config = json.loads(path.read_text()); config['backup_base']=str(repository); path.write_text(json.dumps(config))
        entry=[str(self.base/'MyBackupCli')] if self.worker.plan.get('frozen') else ['python3','-B',str(self.base/'MyBackupCli.py')]
        cmd = [*entry, operation, '-c', str(path), *args]
        log = self.evidence/(name+'-'+operation+'-'+str(time.time_ns())+'.log')
        print('BINARY80_TOOL node='+name+' operation='+operation+' topology='+self.topology, flush=True)
        with log.open('w') as stream:
            result = subprocess.run(cmd, env=self.env, stdout=stream, stderr=subprocess.STDOUT)
        output = log.read_text()
        if refuse:
            if result.returncode == 0 or refuse not in output:
                raise RuntimeError('BINARY80_REFUSAL_CONTRACT '+str(log))
        elif result.returncode:
            print(output[-6000:], flush=True)
            raise RuntimeError('BINARY80_CLI_ERROR log='+str(log)+' rc='+str(result.returncode))
        return output

    def service(self, name, action):
        unit = 'binary80-'+self.worker.plan['id']+'-'+name+'.service'
        run(['systemctl', action, unit], stdout=subprocess.DEVNULL)
        if action in ('start', 'restart'):
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                try:
                    self.sql(name, 'SELECT 1'); return
                except RuntimeError: time.sleep(1)
            raise TimeoutError('BINARY80_SQL_READINESS '+name)

    def adapters(self, image):
        directory=self.base/'bin'; directory.mkdir(exist_ok=True)
        for command in ('mysql', 'mysqlbinlog', 'xtrabackup'):
            # Identical absolute path inside the tool container; no datadir
            # alias, argument rewriting or systemctl replacement is permitted.
            argv = [str(self.worker.runtime/'docker'), 'run', '--rm', '-i', '--pull=never', '--network=host',
                    '--label', 'kubeauto.binary80='+self.worker.plan['id'], '--user', '0', '-e', 'TZ=Asia/Shanghai',
                    '-v', str(self.base)+':'+str(self.base),
                    '-v', '/usr/share/zoneinfo/Asia/Shanghai:/usr/share/zoneinfo/Asia/Shanghai:ro',
                    '--entrypoint', command, image]
            path=directory/command
            fault = ''
            if command=='xtrabackup':
                fault = ('case " ${*} " in *" --backup "*) if [ "${MYBACKUP_INJECT_ENOSPC:-0}" = 1 ]; then echo "No space left on device (fixture)" >&2; exit 28; fi;; esac\n'
                         'case " ${*} " in *" --prepare "*) if [ "${MYBACKUP_INJECT_SIGINT:-0}" = 1 ]; then kill -INT "$PPID"; exit 130; fi;; esac\n')
            path.write_text('#!/bin/sh\n'+fault+'exec '+shlex.join(argv)+' "$@"\n'); path.chmod(0o755)
        for command in ('mysql','mysqlbinlog','xtrabackup'):
            run([directory/command, '--version'], env=self.env, stdout=subprocess.DEVNULL)

    def server_config(self, name, number):
        node=self.base/name; port=16330+number
        return ('[mysqld]\nuser=nobody\nbasedir='+str(self.prefix/'usr')+'\n'
                  'plugin-dir='+str(self.prefix/'usr/lib64/mysql/plugin')+'\n'
                  'datadir='+str(node/'data')+'\nsocket='+str(node/'data/mysql.sock')+'\n'
                  'log-error='+str(node/'logs/error.log')+'\nlog-bin='+str(node/'logs/mysql-bin')+'\n'
                  'log-bin-index='+str(node/'logs/mysql-bin.index')+'\ntmpdir='+str(node/'tmp')+'\n'
                  'pid-file='+str(node/'data/mysqld.pid')+'\nport='+str(port)+'\nbind-address=127.0.0.1\n'
                  'secure-file-priv=NULL\nmysqlx=OFF\nserver-id='+str(number)+'\ninnodb-buffer-pool-size=64M\n'
                  'innodb-redo-log-capacity=64M\nmax-connections=30\ntable-open-cache=200\n'
                  'performance-schema=ON\nlower-case-table-names=1\ndefault-time-zone=+08:00\n'
                  'binlog-format=ROW\nlog-replica-updates=ON\nreplica-parallel-workers=2\n'
                  'skip-replica-start=ON\ngtid-mode='+self.gtid+'\nenforce-gtid-consistency='+self.gtid+'\n')

    def setup(self):
        for number,name in enumerate(self.nodes,1):
            node=self.base/name; node.mkdir(mode=0o755)
            for sub in ('data','logs','tmp'):
                path=node/sub; path.mkdir(); os.chown(path,65534,65534)
            port=16330+number
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1',port))
            config=self.server_config(name,number)
            (node/'my.cnf').write_text(config)
            with (self.evidence/(name+'-initialize.log')).open('w') as stream:
                run([self.prefix/'usr/sbin/mysqld', '--defaults-file='+str(node/'my.cnf'), '--initialize-insecure'], stdout=stream, stderr=stream)
            unit=Path('/run/systemd/system')/('binary80-'+self.worker.plan['id']+'-'+name+'.service')
            if unit.exists(): raise ValueError('BINARY80_FOREIGN_UNIT')
            unit.write_text(unit_text(self.base,self.prefix,name)); self.units.append(unit)
            run(['systemctl','daemon-reload']); run(['systemctl','start',unit.name])
            deadline=time.monotonic()+60
            while True:
                try: self.sql(name,'SELECT 1',password=False); break
                except RuntimeError:
                    if time.monotonic()>deadline: raise TimeoutError('BINARY80_INITIAL_READINESS '+name)
                    time.sleep(1)
            self.sql(name, "SET SESSION sql_log_bin=0; ALTER USER 'root'@'localhost' IDENTIFIED BY '"+self.password+"'; CREATE USER 'replication'@'127.0.0.1' IDENTIFIED WITH mysql_native_password BY '"+self.password+"'; GRANT REPLICATION SLAVE ON *.* TO 'replication'@'127.0.0.1';", password=False)
            storage=self.sql(name,'SELECT @@datadir,@@log_bin_basename,@@log_bin_index,@@socket')
            (self.evidence/(name+'-storage.tsv')).write_text(storage+'\n')
            actual=self.sql(name,'SELECT VERSION()')
            if not actual.startswith(self.version): raise ValueError('BINARY80_VERSION_REFUSED '+actual)
            cfg=dict(backup_base=str(node/'backup'), mysql_user='root',mysql_password=self.password,
                     mysql_socket=str(node/'data/mysql.sock'), mysql_service=unit.name,mysql_container='',
                     mysql_defaults_file=str(node/'my.cnf'),
                     mysql_topology=self.topology,mysql_datadir=str(node/'data'),mysql_binlog_dir=str(node/'logs'),
                     mysql_binlog_prefix='mysql-bin',mysql_binlog_index=str(node/'logs/mysql-bin.index'),
                     xtrabackup_parallel=1,xtrabackup_compress_threads=1,xtrabackup_use_memory='64M',xtrabackup_lock_ddl='AUTO')
            path=node/'backup.json'; path.write_text(json.dumps(cfg)); path.chmod(0o600)
        print('BINARY80_NATIVE_READY version='+self.version+' gtid='+self.gtid+' topology='+self.topology,flush=True)

    def position(self,name):
        fields=self.sql(name,'SHOW MASTER STATUS').split('\t'); return fields[0],int(fields[1])

    def connect(self,target,source,position=None):
        port=16331 if source=='a' else 16332
        auto = 'SOURCE_AUTO_POSITION=1' if self.gtid=='ON' else "SOURCE_AUTO_POSITION=0,SOURCE_LOG_FILE='"+position[0]+"',SOURCE_LOG_POS="+str(position[1])
        self.sql(target,"CHANGE REPLICATION SOURCE TO SOURCE_HOST='127.0.0.1',SOURCE_PORT="+str(port)+",SOURCE_USER='replication',SOURCE_PASSWORD='"+self.password+"',SOURCE_CONNECT_RETRY=1,"+auto+"; START REPLICA;")

    def wait_equal(self):
        expected=self.snapshot('a'); deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                if self.snapshot('b')==expected:
                    state=self.sql('b', "SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE SERVICE_STATE='ON'")
                    if state=='1': return
            except RuntimeError: pass
            time.sleep(1)
        raise TimeoutError('BINARY80_REPLICATION_READBACK_MISMATCH')

    def snapshot(self,name):
        return self.sql(name,'SELECT id,amount,HEX(payload),HEX(bytes),CAST(doc AS CHAR) FROM binary80.ledger ORDER BY id')

    def seed(self):
        if self.topology!='standalone':
            self.connect('b','a',self.position('a'))
            if self.topology=='dual-primary': self.connect('a','b',self.position('b'))
        self.sql('a',"CREATE DATABASE binary80; CREATE TABLE binary80.ledger(id INT PRIMARY KEY,amount DECIMAL(20,6),payload VARCHAR(100) CHARACTER SET utf8mb4,bytes BLOB,doc JSON) ENGINE=InnoDB; INSERT INTO binary80.ledger VALUES(1,1.123456,'中文全量',UNHEX('00FF7F'),JSON_OBJECT('n',1));")
        if self.topology!='standalone': self.wait_equal()
        if self.topology=='dual-primary':
            self.sql('b',"INSERT INTO binary80.ledger VALUES(2,2,'双主写入',UNHEX('FF00'),JSON_OBJECT('n',2))")
            deadline=time.monotonic()+60
            while self.sql('a','SELECT COUNT(*) FROM binary80.ledger')!='2':
                if time.monotonic()>deadline: raise TimeoutError('BINARY80_DUAL_INITIAL_WRITE')
                time.sleep(1)
            self.wait_equal()
        if self.topology=='primary-replica': self.sql('b','SET GLOBAL super_read_only=ON')

    def checkpoint_chain(self,name):
        root=self.base/name/'backup'
        full=(root/'full/latest_raw').resolve(); latest=(root/'incr/latest').resolve()
        paths=[full,*sorted((root/'incr').glob('*/*'))]
        paths=[p for p in paths if p.is_dir() and not p.is_symlink() and
               (p==full or (re.fullmatch(r'\d{4}-\d{2}-\d{2}',p.parent.name) and re.fullmatch(r'\d{6}',p.name)))]
        previous=0
        if len(paths)!=3 or latest not in paths: raise ValueError('BINARY80_INCREMENT_COUNT')
        for path in paths:
            values=dict(line.split(' = ',1) for line in (path/'xtrabackup_checkpoints').read_text().splitlines() if ' = ' in line)
            if int(values['from_lsn'])!=previous: raise ValueError('BINARY80_LSN_GAP')
            previous=int(values['to_lsn'])
            shutil.copy2(path/'xtrabackup_checkpoints',self.evidence/(name+'-'+path.parent.name+'-'+path.name+'-checkpoint'))
        print('BINARY80_LSN_CHAIN_PASS node='+name,flush=True)

    def lifecycle(self):
        identities={name:self.sql(name,'SELECT @@server_uuid') for name in self.nodes}
        before=self.snapshot('a')
        self.env['MYBACKUP_INJECT_ENOSPC']='1'
        try: self.tool('a','full',refuse='No space left on device')
        finally: self.env.pop('MYBACKUP_INJECT_ENOSPC',None)
        if list((self.base/'a/backup/full').rglob('.backup_ok')) or self.snapshot('a')!=before:
            raise ValueError('BINARY80_PARTIAL_BACKUP_PUBLISHED')
        for name in self.nodes: self.tool(name,'full')
        for key in (3,4):
            self.sql('a',"INSERT INTO binary80.ledger VALUES("+str(key)+",123.456,'增量',UNHEX('0100FF'),JSON_OBJECT('n',"+str(key)+")); UPDATE binary80.ledger SET amount=amount+1 WHERE id=1;")
            if len(self.nodes)==2: self.wait_equal()
            for name in self.nodes: self.tool(name,'incr')
            time.sleep(1)
        for name in self.nodes: self.checkpoint_chain(name)
        time.sleep(2); start=self.sql('a',"SELECT DATE_FORMAT(NOW(),'%Y-%m-%d %H:%i:%s')")
        self.sql('a',"INSERT INTO binary80.ledger VALUES(5,5,'包含',X'FF',JSON_OBJECT('n',5));")
        if len(self.nodes)==2: self.wait_equal()
        expected=self.snapshot('a'); (self.evidence/'pitr.expected').write_text(expected+'\n')
        time.sleep(2); stop=self.sql('a',"SELECT DATE_FORMAT(NOW(),'%Y-%m-%d %H:%i:%s')")
        self.sql('a',"INSERT INTO binary80.ledger VALUES(6,6,'排除',X'00',JSON_OBJECT('n',6));")
        if len(self.nodes)==2: self.wait_equal()
        if self.topology!='standalone':
            for name in self.nodes:
                if name=='b' or self.topology=='dual-primary': self.sql(name,'STOP REPLICA')
                with (self.base/name/'my.cnf').open('a') as stream: stream.write('read-only=ON\nsuper-read-only=ON\n')
                self.service(name,'restart')
        for name in self.nodes:
            self.sql(name,'FLUSH LOGS;'); self.tool(name,'binlog'); self.tool(name,'checksum'); self.tool(name,'restore-dry-run')
            args=['--confirm-destructive-restore','--binlog-start-time',start,'--binlog-stop-time',stop]
            if self.topology!='standalone':
                self.tool(name,'restore',*args,refuse='confirm-replication-isolated')
                args.append('--confirm-replication-isolated')
            self.env['MYBACKUP_INJECT_SIGINT']='1'
            healthy=self.snapshot(name)
            try: self.tool(name,'restore',*args,refuse='Operation cancelled by user')
            finally: self.env.pop('MYBACKUP_INJECT_SIGINT',None)
            if self.snapshot(name)!=healthy: raise ValueError('BINARY80_INTERRUPT_CHANGED_LIVE_DATA')
            self.tool(name,'restore',*args)
            actual=self.snapshot(name); (self.evidence/(name+'-pitr.actual')).write_text(actual+'\n')
            if actual!=expected or self.sql(name,'SELECT @@server_uuid')!=identities[name]:
                raise ValueError('BINARY80_PITR_OR_IDENTITY_MISMATCH '+name)
            self.service(name,'stop')
            self.tool(name,'checksum','--offline-restore')
            self.tool(name,'restore-dry-run','--offline-restore')
            self.tool(name,'restore',*args,'--offline-restore')
            if self.snapshot(name)!=expected or self.sql(name,'SELECT @@server_uuid')!=identities[name]:
                raise ValueError('BINARY80_OFFLINE_RESTORE_MISMATCH '+name)
            print('BINARY80_OFFLINE_RESTORE_PASS node='+name,flush=True)
            if self.topology!='standalone':
                if self.sql(name,'SELECT @@super_read_only')!='1': raise ValueError('BINARY80_RESTORED_NOT_READ_ONLY')
                self.sql(name,'RESET REPLICA ALL')
        print('BINARY80_PITR_UUID_PASS topology='+self.topology+' include=5 exclude=6',flush=True)
        if self.topology!='standalone':
            # Both old histories stay isolated. Rebuild B from restored A using
            # the product CLI, preserving B identity; never attach stale B.
            self.tool('a','full'); self.sql('a','FLUSH LOGS'); self.tool('a','binlog')
            repository=self.base/'a/backup'
            self.tool('b','restore-dry-run',repository=repository)
            self.tool('b','restore','--confirm-destructive-restore','--confirm-replication-isolated',
                      '--binlog-start-time','2099-01-01 00:00:00')
            self.sql('b','RESET REPLICA ALL')
            if self.snapshot('b')!=expected or self.sql('b','SELECT @@server_uuid')!=identities['b']:
                raise ValueError('BINARY80_REBUILD_IDENTITY_MISMATCH')
            self.connect('b','a',self.position('a'))
            if self.topology=='dual-primary': self.connect('a','b',self.position('b'))
            self.sql('a','SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF')
            self.sql('a',"INSERT INTO binary80.ledger VALUES(7,7,'重接后A',X'7F',JSON_OBJECT('n',7))")
            self.wait_equal()
            if self.topology=='dual-primary':
                self.sql('b','SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF')
                self.sql('b',"INSERT INTO binary80.ledger VALUES(8,8,'重接后B',X'FF',JSON_OBJECT('n',8))")
                deadline=time.monotonic()+60
                while self.sql('a','SELECT COUNT(*) FROM binary80.ledger WHERE id=8')!='1':
                    if time.monotonic()>deadline: raise TimeoutError('BINARY80_DUAL_REJOIN_WRITE')
                    time.sleep(1)
                self.wait_equal()
            elif self.sql('b','SELECT @@super_read_only')!='1': raise ValueError('BINARY80_REPLICA_WRITE_PROTECTION')
            (self.evidence/'rejoin.actual').write_text(self.snapshot('a')+'\n')
            print('BINARY80_REJOIN_READBACK_PASS topology='+self.topology,flush=True)
        # Return every node to its own repository, then prove a scheduled
        # full/incremental cycle after the restored/rejoined history.
        for name in self.nodes:
            config_path=self.base/name/'backup.json'
            config=json.loads(config_path.read_text()); config['backup_base']=str(self.base/name/'backup')
            config_path.write_text(json.dumps(config)); self.tool(name,'full')
        self.sql('a',"INSERT INTO binary80.ledger VALUES(9,9,'恢复后新周期',X'0100',JSON_OBJECT('n',9))")
        if len(self.nodes)==2: self.wait_equal()
        for name in self.nodes:
            self.tool(name,'incr')
            repository=self.base/name/'backup'
            full=(repository/'full/latest_raw').resolve(); increment=(repository/'incr/latest').resolve()
            def checkpoint(path,key):
                return next(int(line.split(' = ',1)[1]) for line in (path/'xtrabackup_checkpoints').read_text().splitlines() if line.startswith(key+' = '))
            if checkpoint(full,'to_lsn')!=checkpoint(increment,'from_lsn'):
                raise ValueError('BINARY80_NEW_CYCLE_LSN_MISMATCH')
        print('BINARY80_NEW_FULL_CYCLE_PASS topology='+self.topology,flush=True)
        print('BINARY_LIFECYCLE_PASS topology='+self.topology+' version='+self.version+' gtid='+self.gtid,flush=True)

    def cleanup(self):
        for unit in reversed(self.units):
            subprocess.run(['systemctl','stop',unit.name],check=True)
            if subprocess.run(['systemctl','is-active','--quiet',unit.name]).returncode==0:
                raise ValueError('BINARY80_UNIT_STILL_ACTIVE')
            unit.unlink(); subprocess.run(['systemctl','reset-failed',unit.name],stderr=subprocess.DEVNULL)
        if self.units: run(['systemctl','daemon-reload'])
        for name in self.nodes:
            node=self.base/name
            if node.exists():
                for error in node.glob('logs/error.log'): shutil.copy2(error,self.evidence/(name+'-mysqld.log'))
                shutil.rmtree(node)
        for line in Path('/proc/net/unix').read_text().splitlines():
            if str(self.base/'a/data/mysql.sock') in line or str(self.base/'b/data/mysql.sock') in line:
                raise ValueError('BINARY80_SOCKET_RESIDUE')
        print('BINARY80_SCOPED_CLEAN_PASS topology='+self.topology,flush=True)


def owned_mounts(text, base):
    result=[]
    for line in text.splitlines():
        fields=line.split()
        if len(fields)<8: continue
        path=Path(fields[4])
        if str(base) not in str(path): continue
        if path!=base/'e/netns/default' or ' - nsfs ' not in line:
            raise ValueError('BINARY80_UNEXPECTED_OWNED_MOUNT '+str(path))
        result.append(path)
    return result


def cleanup_runtime_mounts(base):
    for path in owned_mounts(Path('/proc/self/mountinfo').read_text(),base):
        run(['umount','--',path])
    if str(base) in Path('/proc/self/mountinfo').read_text():
        raise ValueError('BINARY80_MOUNT_RESIDUE')


def remote_recover():
    base=Path.cwd(); plan=json.loads((base/'plan.json').read_text())
    if str(base)!=plan['base'] or str(base)!='/tmp/b80-'+plan['id'] or not re.fullmatch('[a-f0-9]{12}',plan['id']):
        raise ValueError('BINARY80_RECOVERY_SCOPE_REFUSED')
    if not (base/'state.finalized').is_file(): raise ValueError('BINARY80_RECOVERY_NOT_FINALIZED')
    for process in Path('/proc').glob('[0-9]*/cmdline'):
        try: command=process.read_bytes()
        except (FileNotFoundError,ProcessLookupError,PermissionError): continue
        if str(base/'runtime').encode() in command or str(base/'mysql-').encode() in command:
            raise ValueError('BINARY80_RECOVERY_PROCESS_ACTIVE')
    for name in ('a','b'):
        unit='binary80-'+plan['id']+'-'+name+'.service'
        if Path('/run/systemd/system',unit).exists() or (base/name).exists():
            raise ValueError('BINARY80_RECOVERY_FIXTURE_NOT_CLEAN')
    cleanup_runtime_mounts(base)
    worker=load('starrocks-compose-live').Worker(plan)
    if worker.firewall_rules()!=(base/'evidence/firewall-before').read_bytes():
        raise ValueError('BINARY80_RECOVERY_FIREWALL_CHANGED')
    for path in (worker.work,worker.runtime,base/'docker-data',base/'e',base/'config',base/'rpmdb',base/'bin',*base.glob('mysql-8.0.*')):
        if path.exists(): shutil.rmtree(path)
    with tarfile.open('evidence.tar.gz','w:gz') as archive: archive.add(base/'evidence',arcname='evidence')
    print('BINARY80_FAILED_RUN_CLEAN_VERIFY_PASS id='+plan['id'],flush=True)


def remote():
    plan=json.loads(Path('plan.json').read_text()); base=Path.cwd()
    if str(base)!=plan['base'] or not re.fullmatch(r'/tmp/b80-[a-f0-9]{12}',str(base)):
        raise ValueError('BINARY80_REMOTE_SCOPE_REFUSED')
    if plan['os'] not in ('8.10','9.8') or plan['stage'] not in ('probe','replication-probe',*TOPOLOGIES,'full'):
        raise ValueError('BINARY80_REMOTE_PLAN_REFUSED')
    credentials=json.load(sys.stdin)
    for local,expected in plan['transfers'].items():
        if sha(base/local)!=expected: raise ValueError('BINARY80_TRANSFER_REFUSED '+local)
    actual=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if actual['ID'].strip('"')!='rocky' or actual['VERSION_ID'].strip('"')!=plan['os'] or os.uname().machine!='x86_64':
        raise ValueError('BINARY80_NATIVE_OS_REFUSED')
    locks=[]
    for name in ('kubeauto-starrocks-compose.lock','kubeauto-mysql-backup-release.lock'):
        stream=open('/var/lock/'+name,'w'); locks.append(stream); fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    for service in ('docker','containerd','kubelet'):
        if subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0:
            raise ValueError('BINARY80_FOREIGN_RUNTIME '+service)
    if shutil.disk_usage(base).free<8*1024**3: raise ValueError('BINARY80_DISK_BUDGET')
    base.chmod(0o755)
    evidence=base/'evidence'; evidence.mkdir(mode=0o700)
    (evidence/'os-release').write_text(Path('/etc/os-release').read_text())
    (evidence/'kernel').write_text(os.uname().release+'\n')
    worker=load('starrocks-compose-live').Worker(plan); worker.creds=credentials
    worker.firewall=worker.firewall_rules()
    def interrupted(signum,frame): raise InterruptedError('BINARY80_INTERRUPT '+str(signum))
    signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
    fixture=None; process_log=(base/'dockerd.log').open('w')
    try:
        rpmdb=base/'rpmdb'; rpmdb.mkdir()
        run(['rpm','--dbpath',rpmdb,'--initdb'])
        for name in plan['keys']:
            run(['rpmkeys','--dbpath',rpmdb,'--import',base/name])
        worker.runtime.mkdir(); worker.work.mkdir()
        with tarfile.open('runtime.tar.gz') as archive:
            for member in archive:
                if not member.isfile() or member.name not in plan['runtime']['files'] or '/' in member.name:
                    raise ValueError('BINARY80_RUNTIME_MEMBER_REFUSED')
                path=worker.runtime/member.name; path.write_bytes(archive.extractfile(member).read()); path.chmod(0o755)
                if sha(path)!=plan['runtime']['files'][member.name]: raise ValueError('BINARY80_RUNTIME_MEMBER_CHECKSUM')
        worker.daemon=subprocess.Popen(worker.daemon_args(),env=worker.env,stdout=process_log,stderr=process_log)
        deadline=time.monotonic()+60
        while subprocess.run([worker.runtime/'docker','info'],env=worker.env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            if worker.daemon.poll() is not None or time.monotonic()>deadline: raise TimeoutError('BINARY80_DAEMON_READINESS')
            time.sleep(1)
        image=worker.pull('pxb')
        for version in sorted({v for v,_,_ in scenarios(plan['stage'])}):
            prefix=base/('mysql-'+version); prefix.mkdir(mode=0o755)
            record=plan['records'][version]
            for item in (record,*record['companions'].values()):
                rpm=base/(item['sha256']+'.rpm')
                signed=run(['rpmkeys','--dbpath',rpmdb,'--checksig',rpm],capture_output=True,text=True)
                (evidence/('signature-'+rpm.name+'.txt')).write_text(signed.stdout+signed.stderr)
                if 'OK' not in signed.stdout or re.search(r'NOT OK|NOKEY|BAD',signed.stdout+signed.stderr):
                    raise ValueError('BINARY80_RPM_SIGNATURE_REFUSED')
                converter=subprocess.Popen(['rpm2cpio',str(rpm)],stdout=subprocess.PIPE)
                try: run(['cpio','-idm','--quiet','--no-absolute-filenames'],cwd=prefix,stdin=converter.stdout)
                finally: converter.stdout.close()
                if converter.wait()!=0: raise ValueError('BINARY80_RPM_EXTRACTION')
            elf=run(['ldd',prefix/'usr/sbin/mysqld'],capture_output=True,text=True).stdout
            (evidence/(version+'-ldd.txt')).write_text(elf)
            if 'not found' in elf: raise ValueError('BINARY80_NATIVE_LIBRARY_MISSING '+elf)
            for v,gtid,topology in scenarios(plan['stage']):
                if v!=version: continue
                output=evidence/(version+'-'+gtid+'-'+topology); output.mkdir(mode=0o700)
                fixture=Fixture(worker,prefix,version,gtid,topology,output)
                print('BINARY80_SCENARIO_START os='+plan['os']+' version='+version+' gtid='+gtid+' topology='+topology,flush=True)
                try:
                    fixture.adapters(image); fixture.setup(); fixture.seed(); fixture.lifecycle()
                finally:
                    fixture.cleanup(); fixture=None
            shutil.rmtree(prefix)
    finally:
        if fixture is not None: fixture.cleanup()
        if worker.daemon is not None and worker.daemon.poll() is None:
            remaining=worker.docker('ps','-aq',capture_output=True,text=True).stdout.split()
            if remaining: worker.docker('rm','-f',*remaining)
            worker.daemon.terminate(); worker.daemon.wait(timeout=40)
        process_log.close()
        after=worker.firewall_rules()
        (evidence/'firewall-before').write_bytes(worker.firewall); (evidence/'firewall-after').write_bytes(after)
        cleanup_runtime_mounts(base)
        if after!=worker.firewall:
            raise ValueError('BINARY80_HOST_CLEANUP_REFUSED')
        for path in (worker.work,worker.runtime,base/'docker-data',base/'e',base/'config',base/'rpmdb',base/'bin',*base.glob('mysql-8.0.*')):
            if path.exists(): shutil.rmtree(path)
        with tarfile.open('evidence.tar.gz','w:gz') as archive: archive.add(evidence,arcname='evidence')
    print('TOOLS_CLEAN_VERIFY_PASS scope=mysql80-binary',flush=True)
    print('BINARY80_HOST_PASS os='+plan['os'],flush=True)


def expected_markers(stage,label):
    return ['BINARY_LIFECYCLE_PASS topology='+t+' version='+v+' gtid='+g for v,g,t in scenarios(stage)]+[
        'TOOLS_CLEAN_VERIFY_PASS scope=mysql80-binary','BINARY80_HOST_PASS os='+HOSTS[label][1],'BINARY80_EXIT rc=0']


def verify_host(record,stage,label):
    if record.get('execution') not in ('source','frozen'): raise ValueError('BINARY80_EXECUTION_IDENTITY_MISSING')
    for path,expected in record['files'].items():
        if sha(ROOT/path)!=expected: raise ValueError('BINARY80_RAW_EVIDENCE_STALE '+path)
    for path in record['durable']:
        if (ROOT/path).read_text().strip()!='0': raise ValueError('BINARY80_DURABLE_REFUSED')
    text=(ROOT/record['log']).read_text()
    if any(marker not in text for marker in expected_markers(stage,label)) or re.search(r'Traceback|BINARY80_\w*(?:ERROR|MISMATCH|REFUSED)|_FALSE_SUCCESS',text):
        raise ValueError('BINARY80_RESULT_REFUSED '+record['log'])


def verify_record(path):
    proof=json.loads(path.read_text())
    if proof['inputs'] != inputs():
        if set(proof['inputs']) != set(inputs()):
            raise ValueError('BINARY80_INPUT_SET_CHANGED')
        for name, expected in proof['inputs'].items():
            load('tools-production-audit').compatible_file(name, expected, 'mysql80-binary-'+proof['stage'])
    if proof['rc']!=0 or set(proof['hosts'])!=set(HOSTS) or not proof.get('artifact'):
        raise ValueError('BINARY80_EVIDENCE_STALE '+str(path))
    for label,record in proof['hosts'].items():
        if record.get('execution')!='frozen': raise ValueError('BINARY80_SOURCE_IS_NOT_RELEASE_EVIDENCE')
        verify_host(record,proof['stage'],label)
    return proof


def launch_host(label,stage,records,images,runtime):
    host,os_version=HOSTS[label]; ident=uuid.uuid4().hex[:12]; remote='/tmp/b80-'+ident
    directory=ROOT/'logs'/('mysql80-binary-'+stage+'-'+label+'-'+ident); directory.mkdir(mode=0o700,parents=True)
    selected={v:records[label+'/'+v] for v,_,_ in scenarios(stage)}
    transfer=[(Path(__file__),'mysql80-binary-live.py'),(ROOT/'tools/mysqltools/MyBackupCli.py','MyBackupCli.py'),
              (ROOT/'tests/helpers/starrocks-compose-live.py','starrocks-compose-live.py'),
              (ROOT/'tests/helpers/run-durable-gate.sh','durable.sh'),(RUNTIME,'runtime.tar.gz')]
    frozen=frozen_identity(required=stage!='probe')
    if frozen: transfer.append((ROOT/'dist/MyBackupCli-rocky8','MyBackupCli'))
    cache=Path.home()/'.cache/kubeauto/mysql80-binary'
    for record in selected.values():
        for item in (record,*record['companions'].values()):
            name=item['sha256']+'.rpm'; transfer.append((cache/name,name))
    keys=json.loads((ROOT/SOURCES).read_text())['keys']
    for name in keys: transfer.append((ROOT/'logs/mysql80-binary-sources-20261125'/name,name))
    plan=dict(id=ident,base=remote,stage=stage,os=os_version,images=images,runtime=runtime,records=selected,
              keys=keys,inputs=inputs(),frozen=frozen,transfers={name:sha(source) for source,name in transfer})
    (directory/'plan.json').write_text(json.dumps(plan,indent=2)+'\n')
    print('BINARY80_RUN id='+ident+' host='+label+' stage='+stage+' path='+str(directory),flush=True)
    run([*SSH,host,'mkdir','-m','700',remote])
    for source,name in [*transfer,(directory/'plan.json','plan.json')]:
        run(['scp','-q','-o','BatchMode=yes',source,host+':'+remote+'/'+name])
    command='cd '+remote+' || exit; exec 3<&0; nohup bash durable.sh state BINARY80_EXIT python3 mysql80-binary-live.py remote <&3 >console.log 2>&1 &'
    run([*SSH,host,command],input=json.dumps(load('starrocks-compose-live').credential()).encode())
    start=progress=time.monotonic(); offset=1
    with (directory/'console.log').open('wb') as stream:
        while time.monotonic()-start<7200:
            result=run([*SSH,host,'tail -c +'+str(offset)+' '+remote+'/console.log'],capture_output=True)
            if result.stdout:
                stream.write(result.stdout); stream.flush(); sys.stdout.buffer.write(result.stdout); sys.stdout.buffer.flush()
                offset+=len(result.stdout); progress=time.monotonic()
            if subprocess.run([*SSH,host,'test -s '+remote+'/state.finalized']).returncode==0: break
            if time.monotonic()-progress>40:
                print('BINARY80_HEARTBEAT host='+label+' run='+ident+' elapsed='+str(int(time.monotonic()-start))+' next=CLI-result-and-cleanup',flush=True); progress=time.monotonic()
            time.sleep(5)
        else: raise TimeoutError('BINARY80_TIMEOUT preserve='+host+':'+remote)
    for name in ('console.log','state.exit','state.finalized','evidence.tar.gz','dockerd.log'):
        run(['scp','-q','-o','BatchMode=yes',host+':'+remote+'/'+name,directory/name])
    record=dict(execution='frozen' if frozen else 'source',files={str(p.relative_to(ROOT)):sha(p) for p in directory.iterdir() if p.is_file()},
                durable=[str((directory/('state.'+x)).relative_to(ROOT)) for x in ('exit','finalized')],
                log=str((directory/'console.log').relative_to(ROOT)))
    verify_host(record,stage,label)
    if inputs()!=plan['inputs']: raise ValueError('BINARY80_INPUT_CHANGED_DURING_RUN')
    run([*SSH,host,'python3 -c '+shlex.quote('import shutil; shutil.rmtree('+repr(remote)+')')])
    (directory/'accepted.json').write_text(json.dumps(dict(inputs=inputs(),stage=stage,host=label,record=record),indent=2)+'\n')
    print('BINARY80_HOST_ACCEPTED host='+label+' run='+ident,flush=True)
    return record


def launch(stage):
    records,images,runtime=admit(stage); qualify()
    hosts=('rocky8',) if stage=='probe' else tuple(HOSTS)
    results={}
    with load('starrocks-compose-live').control_leases():
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as executor:
            jobs={label:executor.submit(launch_host,label,stage,records,images,runtime) for label in hosts}
            failures={}
            for label,job in jobs.items():
                try: results[label]=job.result()
                except Exception as error: failures[label]=str(error); print('BINARY80_BRANCH_ERROR host='+label+' '+str(error),flush=True)
            if failures: raise ValueError('BINARY80_ATTEMPT_INCOMPLETE '+json.dumps(failures))
    proof=dict(date='2026-11-25',stage=stage,inputs=inputs(),rc=0,hosts=results,
               artifact=frozen_identity(required=stage!='probe'),
               boundary='native mysqld/systemd; unencrypted InnoDB; GTID OFF/ON; no GR/Galera, multi-source or cross-version restore')
    path=ROOT/('tests/evidence/mysql80-binary-'+stage+'.json'); path.write_text(json.dumps(proof,indent=2)+'\n')
    if stage in ('probe','replication-probe'):
        print('BINARY80_PROBE_PASS evidence='+str(path.relative_to(ROOT)),flush=True)
        return
    verify_record(path)
    ident={'standalone':'TL-MBK-10','primary-replica':'TL-MBK-11','dual-primary':'TL-MBK-12','full':'TL-MBK-13'}[stage]
    matrix=ROOT/'tests/tools-test-matrix.yaml'; text=matrix.read_text(); lines=text.splitlines()
    for index,line in enumerate(lines):
        if '"id": "'+ident+'"' in line:
            line=line.replace('"status": "pending"','"status": "pass"').replace('"status": "fail"','"status": "pass"')
            if '"evidence":' not in line: line=line[:-1]+', "evidence": "'+str(path.relative_to(ROOT))+'"}'
            lines[index]=line
    text='\n'.join(lines)+'\n'
    import yaml
    data=yaml.safe_load(text)
    for status in ('pass','pending','fail'):
        count=sum(case['status']==status for case in data['test_cases'])
        text=re.sub(r'(?m)^  '+status+r': \d+$','  '+status+': '+str(count),text)
    matrix.write_text(text)
    print('BINARY80_ACCEPTED id='+ident+' evidence='+str(path.relative_to(ROOT)),flush=True)
    if stage=='full': print('BINARY_FINAL_PASS',flush=True)


def recover(directory):
    directory=Path(directory).resolve()
    if directory.parent!=ROOT/'logs': raise ValueError('BINARY80_RECOVERY_LOCAL_SCOPE')
    plan=json.loads((directory/'plan.json').read_text())
    labels=[label for label,(_,version) in HOSTS.items() if version==plan['os']]
    if len(labels)!=1: raise ValueError('BINARY80_RECOVERY_HOST')
    host=HOSTS[labels[0]][0]; base=plan['base']
    if base!='/tmp/b80-'+plan['id'] or not re.fullmatch('[a-f0-9]{12}',plan['id']): raise ValueError('BINARY80_RECOVERY_ID')
    with load('starrocks-compose-live').control_leases():
        run(['scp','-q','-o','BatchMode=yes',Path(__file__),host+':'+base+'/recovery.py'])
        result=run([*SSH,host,'cd '+base+' && python3 -B recovery.py remote-recover'],capture_output=True,text=True)
        (directory/'recovery.log').write_text(result.stdout+result.stderr)
        print(result.stdout,flush=True)
        for name in ('evidence.tar.gz','dockerd.log'):
            run(['scp','-q','-o','BatchMode=yes',host+':'+base+'/'+name,directory/name])
        run([*SSH,host,'python3 -c '+shlex.quote('import shutil; shutil.rmtree('+repr(base)+')')])


def status():
    import yaml
    cases=yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())['test_cases']
    for stage,ident in (('standalone','TL-MBK-10'),('primary-replica','TL-MBK-11'),('dual-primary','TL-MBK-12'),('full','TL-MBK-13')):
        if next(case for case in cases if case['id']==ident)['status']=='pass':
            verify_record(ROOT/('tests/evidence/mysql80-binary-'+stage+'.json'))


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('stage',choices=('status','probe','replication-probe','remote','remote-recover','recover','preflight',*TOPOLOGIES,'full'))
    parser.add_argument('--run-directory',type=Path)
    args=parser.parse_args()
    if args.stage=='status': status()
    elif args.stage=='remote-recover': remote_recover()
    elif args.stage=='recover': recover(args.run_directory)
    elif args.stage=='remote': remote()
    elif args.stage=='preflight': qualify()
    else: launch(args.stage)


if __name__=='__main__': main()
