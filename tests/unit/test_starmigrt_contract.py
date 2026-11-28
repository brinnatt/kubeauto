"""StarMigrtCli 的独立 CLI 合同；驱动 fixture 不代表真实 StarRocks 验收。"""
import datetime as dt
from decimal import Decimal
import gzip
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/starrocks/starmigration/StarMigrtCli.py"
with tempfile.TemporaryDirectory(prefix="starmigrt-import-") as isolated:
    with patch.dict(os.environ, {"KUBEAUTO_LOG_DIR": isolated}):
        spec = importlib.util.spec_from_file_location("starmigrt_test_subject", SCRIPT)
        dump = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dump)

DDL = '''CREATE TABLE `events` (
  `id` bigint NOT NULL,
  `text` varchar(200) COMMENT 'keep this unchanged'
) ENGINE=OLAP
DUPLICATE KEY(`id`)
DISTRIBUTED BY HASH(`id`) BUCKETS 3
PROPERTIES (
"replication_num" = "3",
"dynamic_partition.enable" = "true",
"dynamic_partition.replication_num" = "3"
)'''

# This fixture implements only the SQL requests actually used by the product.
DRIVER = r'''
import hashlib, json, os, re, socket
from decimal import Decimal

def event(text):
    with open(os.environ['FAKE_EVENTS'], 'a', encoding='utf-8') as f: f.write(text+'\n')

class SSCursor: pass
class cursors: SSCursor = SSCursor
class Cursor:
    def __init__(self, conn, stream=False):
        self.conn, self.stream, self.rows, self.exhausted = conn, stream, [], False
        self.connection = conn
        self.warning_count = 0
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def execute(self, sql, args=None):
        self.rows = []
        if sql.startswith('SET '): event('SESSION '+sql); return
        if sql == 'SELECT current_version()':
            self.rows = [(self.conn.config.get('version', '3.5.7-test'),)]; return
        if sql == 'SHOW DATABASES':
            self.rows = [(db,) for db in self.conn.config['databases']]; return
        if 'information_schema.tables' in sql:
            self.rows = [(name,t.get('kind','BASE TABLE'),t.get('engine','StarRocks'))
                         for name,t in sorted(self.conn.config['databases'][args[0]].items())]; return
        identifiers = [x.replace('``','`') for x in re.findall(r'`((?:``|[^`])+)`', sql)]
        if sql.startswith('DESC '):
            db,name = identifiers[-2:]
            t = self.conn.config['databases'].get(db,{}).get(name,{})
            self.rows = [(c[0],c[1],'YES','false',None,c[2]) for c in t.get('columns',[])]; return
        if sql.startswith('SHOW CREATE TABLE'):
            db,name = identifiers[-2:]
            self.rows = [(name,self.conn.config['databases'][db][name]['ddl'])]; return
        if sql.startswith('SELECT COUNT(*)'):
            db,name = identifiers[-2:]; t = self.conn.config['databases'][db][name]
            self.rows = [(t.get('n',len(t.get('rows',[]))),)]; return
        if sql.startswith('SELECT '):
            db,name = identifiers[-2:]; t = self.conn.config['databases'][db][name]
            event('READ '+db+'.'+name)
            if t.get('block'):
                self.conn._sock.recv(1)
                raise RuntimeError('cancelled blocked read')
            if t.get('fail'):
                raise RuntimeError(1234,'private-row-value DO_NOT_PRINT_PASSWORD')
            self.rows = ((i,'row-'+str(i)) for i in range(t['n'])) if 'n' in t else [tuple(r) for r in t.get('rows',[])]
            return
        if sql.startswith(('CREATE ', 'INSERT ')):
            event('WRITE '+sql.split()[0])
            raise RuntimeError(1234, 'private-row-value DO_NOT_PRINT_PASSWORD')
        raise AssertionError(sql)
    def fetchall(self): return tuple(self.rows)
    def __iter__(self):
        for r in self.rows:
            if self.conn.closed: raise RuntimeError('closed')
            yield r
        self.exhausted = True
    def close(self):
        if self.stream and not self.exhausted and self.connection is not None: event('DRAIN_BUG')

class Connection:
    def __init__(self, **options):
        safe = {k:v for k,v in options.items() if k != 'password'}
        safe['password_sha256'] = hashlib.sha256(options['password'].encode()).hexdigest()
        self.config = json.load(open(os.environ['FAKE_CONFIG'], encoding='utf-8'))
        self.closed = False
        self._sock,self.other = socket.socketpair()
        if options.get('ssl_ca'):
            assert options.get('ssl_verify_cert') and options.get('ssl_verify_identity')
        self.server_capabilities = 0 if self.config.get('no_tls') else 1
        self._request_authentication()
        event('CONNECT '+json.dumps(safe,sort_keys=True))
    def _request_authentication(self): event('AUTH')
    def cursor(self, cls=None): return Cursor(self, cls is SSCursor)
    @property
    def open(self): return not self.closed
    def close(self):
        if self.closed: raise RuntimeError('Already closed')
        self.closed = True
        self._sock.close(); self.other.close()
def connect(**options): return Connection(**options)
class connections: Connection = Connection
class Client: SSL = 1
class constants: CLIENT = Client
'''


def table(rows=None, **values):
    result = dict(ddl=DDL, columns=[['id', 'bigint', ''], ['text', 'varchar', '']],
                  rows=rows if rows is not None else [[1, "中文'\\\n"], [2, None]])
    result.update(values)
    return result


class LiteralContracts(unittest.TestCase):
    def test_null_empty_and_literal_null_are_distinct(self):
        self.assertEqual([dump.literal(v, 'varchar') for v in (None, '', 'NULL')], ['NULL', "''", "'NULL'"])

    def test_controls_unicode_and_quotes(self):
        self.assertEqual(dump.literal("中文'\\\n\r\t\0\x1a", 'varchar'), "'中文\\'\\\\\\n\\r\\t\\0\\Z'")
        self.assertEqual(dump.ident('a`b'), '`a``b`')
        with self.assertRaises(dump.MigrationError): dump.ident('bad\nname')

    def test_decimal_largeint_json_and_binary_keep_precision(self):
        value = '-170141183460469231731687303715884105728'
        self.assertEqual(dump.literal(value, 'largeint'), "CAST('"+value+"' AS LARGEINT)")
        self.assertEqual(dump.literal(Decimal('99999999999999999999.123456789012345678'), 'decimal128'),
                         '99999999999999999999.123456789012345678')
        self.assertEqual(dump.literal('null', 'json'), "parse_json('null')")
        self.assertEqual(dump.literal('{"n":9007199254740993}', 'json'), 'parse_json(\'{"n":9007199254740993}\')')
        self.assertEqual(dump.literal(b'\x00\xff\x27\x5c', 'varbinary'), "unhex('00ff275c')")

    def test_invalid_values_are_rejected(self):
        for value in (float('nan'), float('inf'), '1); DROP TABLE x; --'):
            with self.assertRaises(dump.MigrationError): dump.literal(value, 'double')
        with self.assertRaises(dump.MigrationError): dump.literal('text', 'varbinary')
        with self.assertRaises(dump.MigrationError): dump.row_sql((1,), [{'type': 'int'}, {'type': 'int'}])

    def test_microseconds_and_boolean(self):
        self.assertEqual(dump.literal(dt.datetime(2026,11,18,12,30,15,123456), 'datetime'), "'2026-11-18 12:30:15.123456'")
        self.assertEqual(dump.literal(1, 'boolean'), 'TRUE')
        with self.assertRaises(dump.MigrationError): dump.literal('truthy', 'boolean')

    def test_replication_preserved_unless_explicit(self):
        ddl, dynamic = dump.prepare_ddl(DDL, 'new', 'events')
        self.assertTrue(dynamic)
        self.assertIn('"replication_num" = "3"', ddl)
        self.assertIn('"dynamic_partition.enable" = "false"', ddl)
        ddl, _ = dump.prepare_ddl(DDL, 'new', 'events', 1)
        self.assertIn('"replication_num" = "1"', ddl)
        self.assertIn('"dynamic_partition.replication_num" = "1"', ddl)
        ddl, dynamic = dump.prepare_ddl(DDL, 'new', 'events', with_data=False)
        self.assertFalse(dynamic)
        self.assertIn('"dynamic_partition.enable" = "true"', ddl)

    def test_properties_in_comments_and_defaults_are_untouched(self):
        source = DDL.replace('keep this unchanged', 'comment\nPROPERTIES (\n"replication_num" = "17"\ntext')
        ddl, _ = dump.prepare_ddl(source, 'new', 'events', 1)
        self.assertIn('"replication_num" = "17"', ddl)
        spaced = DDL.replace('PROPERTIES (', 'properties\n( /* property comment */')
        ddl, _ = dump.prepare_ddl(spaced, 'new', 'events', 2)
        self.assertIn('"replication_num" = "2"', ddl)

    def test_ttl_data_refused_schema_allowed(self):
        source = DDL.replace('"replication_num" = "3",', '"partition_ttl_number" = "7",\n"replication_num" = "3",', 1)
        with self.assertRaisesRegex(dump.MigrationError, 'partition_ttl_number'):
            dump.prepare_ddl(source, 'new', 'events')
        dump.prepare_ddl(source, 'new', 'events', with_data=False)

    def test_real_pymysql_abort_does_not_drain_or_close_twice(self):
        try:
            import pymysql
        except ImportError:
            self.skipTest('固定 runner 的虚拟环境含 PyMySQL 1.1.2')
        conn = pymysql.connections.Connection(defer_connect=True)
        conn._sock, peer = socket.socketpair()
        cursor = conn.cursor(pymysql.cursors.SSCursor)
        result = SimpleNamespace(unbuffered_active=True, connection=conn)
        cursor._result = conn._result = result
        try:
            dump.abort_stream(conn,cursor)
            cursor.close()
            self.assertFalse(conn.open)
            self.assertFalse(result.unbuffered_active)
            self.assertIsNone(cursor.connection)
        finally:
            peer.close()


    def test_native_half_open_range_ddl_properties(self):
        native = '''CREATE TABLE `range_t` (`k` int, `v` int)
ENGINE=OLAP DUPLICATE KEY(`k`)
PARTITION BY RANGE(`v`) (
PARTITION p1 VALUES [("-2147483648"), ("0")),
PARTITION p2 VALUES [("0"), ("10")),
PARTITION p3 VALUES [("10"), ("20")))
DISTRIBUTED BY HASH(`k`)
PROPERTIES ("replication_num"="3")'''
        target, deferred = dump.prepare_ddl(native, 'dest', 'range_t', 1)
        self.assertIn('VALUES [("0"), ("10"))', target)
        self.assertIn('"replication_num"="1"', target)
        self.assertFalse(deferred)

    def test_decimal_scientific_notation_preserves_all_digits(self):
        value = Decimal('1.2345678901234567890123456789E-10')
        result = dump.literal(value, 'decimal128')
        self.assertEqual(Decimal(result), value)
        self.assertIn("SET sql_mode = '';", dump.SESSION_SQL)

    def test_native_types_avoid_mysql_information_schema_aliases(self):
        def metadata(conn, query, args=None):
            if query.startswith('DESC '):
                return [('n', 'LARGEINT', 'YES', 'false', None, ''),
                        ('b', 'BOOLEAN', 'YES', 'false', None, ''),
                        ('d', 'DECIMAL(38,38)', 'YES', 'false', None, '')]
            return [('n', 'bigint unsigned', ''), ('b', 'tinyint', ''), ('d', 'decimal', '')]
        with patch.object(dump, 'all_rows', side_effect=metadata):
            columns = dump.columns_for(object(), 'db', 'table')
        self.assertEqual([c['type'] for c in columns], ['largeint', 'boolean', 'decimal'])
        self.assertIn('AS LARGEINT', dump.literal(2**127-1, columns[0]['type']))


class SQLContracts(unittest.TestCase):
    def test_split_sql_strings_comments_and_backticks(self):
        sql = "-- header\nCREATE TABLE `a;b` (`c` varchar(20) COMMENT 'semi;colon');\nINSERT INTO `a;b` VALUES ('a;\\\'b', '双引号;'); -- end\n"
        result = list(dump.statements(io.StringIO(sql)))
        self.assertEqual(len(result), 2)
        self.assertIn("semi;colon", result[0])
        self.assertIn("双引号;", result[1])
        for sql in ("SELECT 'unfinished", '/* unfinished', 'SELECT 1'):
            with self.assertRaises(dump.MigrationError): list(dump.statements(io.StringIO(sql)))

    def test_output_atomic_failure_compression_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for suffix in ('.sql', '.sql.gz'):
                p = root/('data'+suffix)
                with self.assertRaises(RuntimeError):
                    with dump.sql_output(p) as out:
                        out.write('secret')
                        raise RuntimeError()
                self.assertFalse(p.exists())
                self.assertFalse(p.with_name(p.name+'.partial').exists())
                with dump.sql_output(p) as out: out.write('中文\n')
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                with dump.sql_input(p) as data: self.assertEqual(data.read(), '中文\n')
                with self.assertRaises(dump.MigrationError):
                    with dump.sql_output(p): pass
            p = root/'race.sql'
            with self.assertRaises(FileExistsError):
                with dump.sql_output(p) as out:
                    out.write('new')
                    p.write_text('foreign')
            self.assertEqual(p.read_text(), 'foreign')

    def test_dump_integrity_missing_footer_and_tamper(self):
        plan = dict(mode='schema-and-data', tables=[dict(target_database='db', name='t', columns=[dict(name='id',type='int')])])
        data = io.StringIO()
        writer = dump.SQLWriter(data)
        writer.write(dump.SIGNATURE)
        writer.metadata('plan', plan)
        writer.write('SELECT 1;\n')
        writer.metadata('table', dict(table=0, rows=1, row_sha256_sum='0'*64))
        writer.finish()
        text = data.getvalue()
        self.assertEqual(dump.read_dump(io.StringIO(text))[0], plan)
        for broken in (text.replace('SELECT 1','SELECT 2'),text[:text.index(dump.FOOTER)],text+'\n'):
            with self.assertRaises(dump.MigrationError): dump.read_dump(io.StringIO(broken))

    def test_target_checks_all_tables_before_write(self):
        conn = SimpleNamespace()
        plan = dict(mode='schema-and-data',databases=[dict(target='db')],tables=[dict(target_database='db',name='new'),dict(target_database='db',name='existing')])
        def query(conn, sql, args=None):
            if sql == 'SHOW DATABASES': return [('db',)]
            if sql.startswith('SELECT TABLE_NAME'): return [('existing','BASE TABLE','OLAP')]
            self.fail('unexpected mutation: '+sql)
        with patch.object(dump,'all_rows',side_effect=query),self.assertRaisesRegex(dump.MigrationError,'已存在'):
            dump.preflight_target(conn,plan)

    def test_warning_and_committed_stop_before_next_statement(self):
        for warning, message in ((1,b''),(0,b"{'status':'COMMITTED'}")):
            cursor = unittest.mock.MagicMock()
            cursor.__enter__.return_value = cursor
            cursor.warning_count = warning
            cursor._result.message = message
            conn = SimpleNamespace(cursor=lambda:cursor)
            with patch.object(dump,'preflight_target'),patch.object(dump,'verify_rows') as verify:
                with self.assertRaisesRegex(dump.MigrationError,'SQL #1'):
                    dump.load_sql(None,conn,io.StringIO('INSERT INTO t VALUES (1);\nINSERT INTO t VALUES (2);'),{},[],SimpleNamespace(update=lambda **kw:None))
                self.assertEqual(cursor.execute.call_count,1)
                verify.assert_not_called()

    def test_content_mismatch_with_same_row_count_fails(self):
        cursor=unittest.mock.MagicMock()
        cursor.__iter__.return_value=iter([(2,)])
        cursor.close=lambda:None
        conn=SimpleNamespace(cursor=lambda _:cursor)
        columns=[dict(name='id',type='int',native_type='int',extra='')]
        plan=dict(mode='schema-and-data',tables=[dict(target_database='db',name='t',columns=columns)])
        expected=int.from_bytes(hashlib.sha256(b'(1)').digest(),'big')
        with patch.object(dump,'columns_for',return_value=columns),self.assertRaisesRegex(dump.MigrationError,'内容'):
            dump.verify_rows(SimpleNamespace(driver=SimpleNamespace(cursors=SimpleNamespace(SSCursor=None))),conn,plan,[dict(rows=1,row_sha256_sum='%064x'%expected)],SimpleNamespace(update=lambda **kw:None,add_rows=lambda n:None))


class CLIContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='starmigrt-cli-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'pymysql.py').write_text(DRIVER)
        self.config = self.root/'config.json'
        self.events = self.root/'events'
        self.env = dict(os.environ,PYTHONPATH=str(self.root),PYTHONDONTWRITEBYTECODE='1',
                        KUBEAUTO_LOG_DIR=str(self.root),FAKE_CONFIG=str(self.config),FAKE_EVENTS=str(self.events),SR_PASSWORD='DO_NOT_PRINT_PASSWORD')
        self.data = dict(databases=dict(sales=dict(events=table())))
        self.config.write_text(json.dumps(self.data))

    def cli(self,*args,rc=0):
        p = subprocess.run([sys.executable,'-B',str(SCRIPT),*map(str,args)],env=self.env,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,rc,p.stdout+p.stderr)
        self.assertNotIn('DO_NOT_PRINT_PASSWORD',p.stdout+p.stderr)
        return p

    def export(self,path=None,*extra):
        path = path or self.root/'sales.sql'
        self.cli('dump','--host','source','--user','reader','--database','sales','--output',path,*extra)
        return path

    def test_public_help_and_two_step_flow(self):
        text=self.cli('--help').stdout
        self.assertIn('{dump,load}',text)
        for obsolete in ('source-quiesced','allow-inconsistent','merge','manifest','resume'):
            self.assertNotIn(obsolete,text)
        for command in ('dump','load'):
            self.assertIn('--host',self.cli(command,'--help').stdout)
        self.assertIn('3.0.0',self.cli('--version').stdout)

    def test_dump_single_file_plain_and_gzip(self):
        for suffix in ('.sql','.sql.gz'):
            p=self.export(self.root/('sales'+suffix))
            with dump.sql_input(p) as data:
                plan,records=dump.read_dump(data)
                data.seek(0)
                sql=data.read()
                self.assertIn("CREATE TABLE `sales`.`events`",sql)
                self.assertIn('INSERT INTO `sales`.`events` WITH LABEL',sql)
                self.assertNotIn('DROP TABLE',sql)
            self.assertEqual(records[0]['rows'],2)
            self.assertEqual(plan['tables'][0]['columns'][0]['native_type'],'bigint')
        self.assertFalse(list(self.root.glob('*.partial')))
        self.assertNotIn('DRAIN_BUG',self.events.read_text())

    def test_check_creates_no_file_and_reads_no_data(self):
        self.cli('dump','--host','source','--user','reader','--database','sales','--check')
        self.assertNotIn('READ ',self.events.read_text())
        self.assertFalse(list(self.root.glob('*.sql')))

    def test_schema_data_selection_and_mapping(self):
        for mode in ('schema-only','data-only'):
            p=self.export(self.root/(mode+'.sql'),'--'+mode,'--target-database','target')
            text=p.read_text()
            self.assertIn('CREATE TABLE `target`' if mode=='schema-only' else 'INSERT INTO `target`',text)
            self.assertNotIn('INSERT INTO' if mode=='schema-only' else '\nCREATE TABLE',text)

    def test_invalid_arguments_fail_before_authentication(self):
        common=['--host','source','--user','reader']
        for args in (['dump',*common,'--output','x.sql'],['dump',*common,'--database','sales'],
                     ['dump',*common,'--database','sales','--check','--batch-rows','0'],
                     ['load',*common,'--input','x.sql','--port','0']):
            self.cli(*args,rc=2)
        self.assertFalse(self.events.exists())

    def test_corrupt_input_refused_before_authentication(self):
        p=self.root/'broken.sql'
        p.write_text(dump.SIGNATURE+'SELECT 1;\n')
        self.cli('load','--host','target','--user','writer','--input',p,rc=1)
        self.assertFalse(self.events.exists())

    def test_existing_target_refused_without_writes(self):
        p=self.export()
        result=self.cli('load','--host','target','--user','writer','--input',p,rc=1)
        self.assertIn('已存在',result.stderr)
        self.assertNotIn('WRITE',self.events.read_text())

    def test_import_first_error_stops_and_redacts_sql(self):
        p=self.export()
        self.data['databases']={}
        self.config.write_text(json.dumps(self.data))
        result=self.cli('load','--host','target','--user','writer','--input',p,rc=1)
        self.assertIn('SQL #4',result.stderr)
        self.assertNotIn('private-row-value',result.stderr)
        self.assertEqual(self.events.read_text().count('WRITE '),1)

    def test_unsupported_objects_fail_without_output(self):
        self.data['databases']['sales']['events']['kind']='VIEW'
        self.config.write_text(json.dumps(self.data))
        path=self.root/'no.sql'
        result=self.cli('dump','--host','source','--user','reader','--database','sales','--output',path,rc=1)
        self.assertIn('普通内表',result.stderr)
        self.assertFalse(path.exists())

    def test_error_cleans_partial_and_does_not_publish(self):
        self.data['databases']['sales']['events']['fail']=True
        self.config.write_text(json.dumps(self.data))
        path=self.root/'failed.sql'
        result=self.cli('dump','--host','source','--user','reader','--database','sales','--output',path,rc=1)
        self.assertNotIn('private-row-value',result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name+'.partial').exists())
        self.assertNotIn('DRAIN_BUG',self.events.read_text())

    def test_signal_reclaims_partial_and_exits_nonzero(self):
        self.data['databases']['sales']['events']['block']=True
        self.config.write_text(json.dumps(self.data))
        path=self.root/'signal.sql'
        with (self.root/'signal.log').open('w') as log:
            proc=subprocess.Popen([sys.executable,'-B',str(SCRIPT),'dump','--host','source','--user','reader','--database','sales','--output',str(path)],env=self.env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if self.events.exists() and 'READ ' in self.events.read_text(): break
                    if proc.poll() is not None: self.fail('exited before signal')
                    time.sleep(.02)
                else: self.fail('no read boundary')
                proc.terminate()
                self.assertEqual(proc.wait(timeout=5),130)
            finally:
                if proc.poll() is None: proc.kill();proc.wait()
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name+'.partial').exists())

    def test_tls_downgrade_refused_before_authentication(self):
        self.data['no_tls']=True
        self.config.write_text(json.dumps(self.data))
        self.cli('dump','--host','source','--user','reader','--database','sales','--check','--ssl-ca',self.root/'ca.pem',rc=1)
        self.assertFalse(self.events.exists())

    def test_connection_defaults_and_explicit_values_reach_driver(self):
        common = ['dump','--host','source','--user','reader','--database','sales','--check']
        self.cli(*common)
        record = json.loads(next(x[8:] for x in self.events.read_text().splitlines() if x.startswith('CONNECT ')))
        self.assertEqual({k:record[k] for k in ('port','connect_timeout','read_timeout','write_timeout')},
                         dict(port=9030,connect_timeout=15,read_timeout=3630,write_timeout=30))
        self.events.unlink()
        self.cli(*common,'--port','19030','--connect-timeout','2','--query-timeout','7')
        lines = self.events.read_text().splitlines()
        record = json.loads(next(x[8:] for x in lines if x.startswith('CONNECT ')))
        self.assertEqual(record['host'],'source')
        self.assertEqual(record['user'],'reader')
        self.assertEqual((record['port'],record['connect_timeout'],record['read_timeout']),(19030,2,37))
        self.assertIn('SESSION SET query_timeout = 7',lines)

    def test_numeric_limits_and_mutual_exclusion_before_auth(self):
        common = ['dump','--host','source','--user','reader','--database','sales','--check']
        limits = {'--port':(1,65535),'--connect-timeout':(1,120),'--query-timeout':(1,86400),
                  '--batch-rows':(1,1000000),'--batch-mib':(1,256),'--replication-num':(1,32767)}
        for option,(low,high) in limits.items():
            for value in (low-1,high+1,'bad'):
                with self.subTest(option=option,value=value):
                    self.cli(*common,option,value,rc=2)
        for extra in (['--schema-only','--data-only'],['--data-only','--replication-num','1'],
                      ['--databases','sales'],['--all-databases'],['--output','unused.sql']):
            self.cli(*common,*extra,rc=2)
        self.assertFalse(self.events.exists())
        for option,(low,high) in limits.items():
            for value in (low,high):
                with self.subTest(option=option,value=value):
                    self.cli(*common,option,value)

    def test_password_env_file_precedence_and_refusals(self):
        common = ['dump','--host','source','--user','reader','--database','sales','--check']
        self.env['CUSTOM_SR_SECRET'] = 'custom-private-value'
        secret = self.root/'secret'
        secret.write_text('file-private-value\r\n')
        secret.chmod(0o600)
        for extra,value in ((['--password-env','CUSTOM_SR_SECRET'],'custom-private-value'),
                            (['--password-env','CUSTOM_SR_SECRET','--password-file',secret],'file-private-value')):
            self.events.unlink(missing_ok=True)
            result = self.cli(*common,*extra)
            record = json.loads(next(x[8:] for x in self.events.read_text().splitlines() if x.startswith('CONNECT ')))
            self.assertEqual(record['password_sha256'],hashlib.sha256(value.encode()).hexdigest())
            self.assertNotIn(value,result.stdout+result.stderr)
        self.events.unlink()
        self.cli(*common,'--password-env','MISSING_SR_TEST_SECRET',rc=1)
        secret.chmod(0o644)
        self.cli(*common,'--password-file',secret,rc=1)
        secret.chmod(0o600)
        link = self.root/'secret-link'; link.symlink_to(secret)
        for path in (link,self.root,self.root/'missing'):
            self.cli(*common,'--password-file',path,rc=1)
        secret.write_text('x'*65537)
        self.cli(*common,'--password-file',secret,rc=1)
        self.assertFalse(self.events.exists())
        secret.write_text('')
        self.cli(*common,'--password-file',secret)
        record = json.loads(next(x[8:] for x in self.events.read_text().splitlines() if x.startswith('CONNECT ')))
        self.assertEqual(record['password_sha256'],hashlib.sha256(b'').hexdigest())

    def test_scope_patterns_repeated_options_and_mapping_refusals(self):
        self.data['databases'].update(other=dict(a=table(),b=table()), sys=dict(hidden=table()), ignored=dict(a=table()))
        self.config.write_text(json.dumps(self.data))
        common=['dump','--host','source','--user','reader']
        output=self.root/'scope.sql'
        self.cli(*common,'--database','sales','--database','other','--table','sales.*','--table','other.*',
                 '--exclude-table','other.b','--database-map','sales=dst_sales','--database-map','other=dst_other',
                 '--output',output)
        with dump.sql_input(output) as data: plan,records=dump.read_dump(data)
        self.assertEqual([(t['database'],t['name'],t['target_database']) for t in plan['tables']],
                         [('sales','events','dst_sales'),('other','a','dst_other')])
        self.cli(*common,'--all-databases','--exclude-database','ignored','--exclude-database','other',
                 '--exclude-table','none*','--check')
        for extra in (['--database','missing'],['--database','sys'],['--database','sales','--table','missing*'],
                      ['--database','sales','--exclude-table','*'],['--database','sales','--target-database','sys'],
                      ['--databases','sales','other','--target-database','target'],
                      ['--database','sales','--database-map','missing=dst'],
                      ['--database','sales','--database-map','bad'],
                      ['--databases','sales','other','--database-map','sales=same','--database-map','other=same']):
            with self.subTest(extra=extra):self.cli(*common,*extra,'--check',rc=1)

    def test_batch_rows_bytes_replication_and_oversize_row(self):
        # Wide Unicode rows prove the byte budget, rather than character counting.
        self.data['databases']['sales']['events']['rows'] = [[i,'中'*100000] for i in range(7)]
        self.config.write_text(json.dumps(self.data))
        output=self.export(self.root/'bytes.sql','--batch-rows','100','--batch-mib','1','--replication-num','2')
        with dump.sql_input(output) as data:
            plan,records=dump.read_dump(data); data.seek(0)
            inserts=[s for s in dump.statements(data) if s.startswith('INSERT ')]
        self.assertEqual(records[0]['rows'],7)
        self.assertEqual(len(inserts),3)
        self.assertTrue(all(len(s.encode())+1<=1024*1024 for s in inserts))
        self.assertIn('"replication_num" = "2"',plan['tables'][0]['ddl'])
        output=self.export(self.root/'rows.sql','--batch-rows','2','--batch-mib','8')
        with dump.sql_input(output) as data:
            self.assertEqual(sum(s.startswith('INSERT ') for s in dump.statements(data)),4)
        self.data['databases']['sales']['events']['rows']=[[1,'中'*350000]]
        self.config.write_text(json.dumps(self.data))
        failed=self.root/'oversize.sql'
        result=self.cli('dump','--host','source','--user','reader','--database','sales',
                        '--batch-mib','1','--output',failed,rc=1)
        self.assertIn('单行超过 --batch-mib',result.stderr)
        self.assertFalse(failed.exists()); self.assertFalse(failed.with_suffix('.sql.partial').exists())

    def test_load_timeout_sets_server_and_driver_before_first_write(self):
        path=self.export()
        self.data['databases']={};self.config.write_text(json.dumps(self.data));self.events.unlink()
        self.cli('load','--host','target','--user','writer','--input',path,
                 '--connect-timeout','3','--query-timeout','9',rc=1)
        lines=self.events.read_text().splitlines()
        record=json.loads(next(x[8:] for x in lines if x.startswith('CONNECT ')))
        self.assertEqual((record['connect_timeout'],record['read_timeout']),(3,39))
        self.assertLess(lines.index('SESSION SET insert_timeout = 9'),lines.index('WRITE CREATE'))

    def test_interactive_password_is_not_echoed(self):
        import errno
        import pty
        import select
        pid,master=pty.fork()
        if pid==0:
            env=dict(self.env);env.pop('SR_PASSWORD',None)
            os.execve(sys.executable,[sys.executable,'-B',str(SCRIPT),'dump','--host','source','--user','reader',
                                     '--database','sales','--check'],env)
        output=b'';status=None;sent=False
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try:block=os.read(master,65536)
                    except OSError as error:
                        if error.errno==errno.EIO:break
                        raise
                    if not block:break
                    output+=block
                    if '密码'.encode() in output and not sent:
                        os.write(master,b'interactive-private-value\n');sent=True
                done,current=os.waitpid(pid,os.WNOHANG)
                if done:status=current;break
            if status is None:
                done,current=os.waitpid(pid,os.WNOHANG)
                if done:status=current
            self.assertIsNotNone(status,'interactive CLI did not exit')
            self.assertEqual(os.waitstatus_to_exitcode(status),0,output.decode(errors='replace'))
            self.assertTrue(sent)
            self.assertNotIn(b'interactive-private-value',output)
            record=json.loads(next(x[8:] for x in self.events.read_text().splitlines() if x.startswith('CONNECT ')))
            self.assertEqual(record['password_sha256'],hashlib.sha256(b'interactive-private-value').hexdigest())
        finally:
            os.close(master)
            if status is None:
                with __import__('contextlib').suppress(ProcessLookupError):os.kill(pid,signal.SIGKILL)
                with __import__('contextlib').suppress(ChildProcessError):os.waitpid(pid,0)

    def test_all_parameters_have_owned_contract_and_matrix_mapping(self):
        import yaml
        parser=dump.make_parser()
        sub=next(a for a in parser._actions if isinstance(a,__import__('argparse')._SubParsersAction))
        options={s for p in [parser,*sub.choices.values()] for a in p._actions for s in a.option_strings if s.startswith('--')}
        review=yaml.safe_load((ROOT/'tests/tools-test-matrix.yaml').read_text())['starmigrt_review']
        rows=review['parameter_coverage']
        self.assertEqual(options,{r['option'] for r in rows})
        self.assertEqual(len(rows),len(options))
        classes=(CLIContracts,LiteralContracts,SQLContracts)
        for row in rows:
            with self.subTest(option=row['option']):
                cls,method=row['contract'].split('.')
                owner=next(c for c in classes if c.__name__==cls)
                self.assertTrue(callable(getattr(owner,method)))
                self.assertTrue(row['case_ids'])
                self.assertTrue(row['normal'] and row['boundary'])


if __name__ == '__main__':
    unittest.main()
