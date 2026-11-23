"""StarMigrtCli 的独立 CLI 合同；驱动 fixture 不代表真实 StarRocks 验收。"""
import datetime as dt
from decimal import Decimal
import gzip
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
import json, os, re, socket
from decimal import Decimal

def event(text):
    with open(os.environ['FAKE_EVENTS'], 'a', encoding='utf-8') as f: f.write(text+'\n')

class SSCursor: pass
class cursors: SSCursor = SSCursor
class Cursor:
    def __init__(self, conn, stream=False):
        self.conn, self.stream, self.rows, self.exhausted = conn, stream, [], False
        self.connection = conn
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def execute(self, sql, args=None):
        self.rows = []
        if sql.startswith('SET '): return
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
        self.config = json.load(open(os.environ['FAKE_CONFIG'], encoding='utf-8'))
        self.closed = False
        self._sock,self.other = socket.socketpair()
        if options.get('ssl_ca'):
            assert options.get('ssl_verify_cert') and options.get('ssl_verify_identity')
        self.server_capabilities = 0 if self.config.get('no_tls') else 1
        self._request_authentication()
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

    def test_atomic_failure_never_publishes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'test'
            with self.assertRaises(RuntimeError):
                with dump.atomic_output(path) as output:
                    output.write('secret')
                    raise RuntimeError('fail')
            self.assertEqual(list(Path(directory).iterdir()), [])

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


class CliContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='starmigrt-contract-')
        self.root = Path(self.temp.name)
        (self.root/'pymysql.py').write_text(DRIVER, encoding='utf-8')
        self.config = self.root/'fixture.json'
        self.events = self.root/'events'
        self.package = self.root/'backup'
        self.env = dict(os.environ, PYTHONPATH=str(self.root), PYTHONDONTWRITEBYTECODE='1',
                        SR_PASSWORD='DO_NOT_PRINT_PASSWORD', KUBEAUTO_LOG_DIR=str(self.root/'logs'),
                        FAKE_CONFIG=str(self.config), FAKE_EVENTS=str(self.events))
        self.fixture = dict(databases={'db1': {'events': table()}, 'db2': {'events': table([[3,'other']])}})
        self.save()

    def tearDown(self): self.temp.cleanup()

    def save(self): self.config.write_text(json.dumps(self.fixture), encoding='utf-8')

    def cli(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, arguments)],
                                env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout+'\n'+result.stderr)
        self.assertNotIn('DO_NOT_PRINT_PASSWORD', result.stdout+result.stderr)
        return result

    def source(self, command='dump', *extra, expected=0):
        args = [command, '--host','fixture.invalid','--user','reader','--databases','db1','db2']
        if command == 'dump': args += ['--source-quiesced','--output',self.package]
        return self.cli(*args, *extra, expected=expected)

    def manifest(self): return json.loads((self.package/'manifest.json').read_text(encoding='utf-8'))

    def reads(self): return self.events.read_text().splitlines() if self.events.exists() else []

    def test_help_version_and_explicit_scope(self):
        help_text = self.cli('--help').stdout
        self.assertIn('离线逻辑迁移', help_text)
        self.assertIn('StarMigrtCli.py', help_text)
        self.assertEqual(self.cli('--version').stdout.strip(), 'StarMigrtCli 2.0.0')
        self.cli('dump','--host','x','--user','u','--output',self.package, expected=2)
        self.cli('dump','--host','x','--user','u','--database','db1','--output',self.package, expected=2)
        self.cli('check','--host','x','--user','u','--all-databases','--database','db1', expected=2)
        self.assertFalse(self.package.exists())

    def test_check_is_metadata_only_and_mapping_is_shown(self):
        result = self.source('check', '--database-map','db1=new1')
        self.assertIn('db1.events → new1.events', result.stderr)
        self.assertIn('STARMIGRT_CHECK_PASS', result.stdout)
        self.assertFalse(self.package.exists())
        self.assertFalse(any(x.startswith('READ ') for x in self.reads()))

    def test_multi_database_export_merge_and_offline_verify(self):
        self.assertIn('STARMIGRT_PASS', self.source().stdout)
        manifest = self.manifest()
        self.assertEqual(manifest['tool'], 'StarMigrtCli')
        self.assertEqual(manifest['status'], 'complete')
        self.assertEqual(sum(x['rows'] for x in manifest['completed'].values()), 3)
        self.assertIn('"replication_num" = "3"', (self.package/'schema.sql').read_text())
        self.assertNotIn('DO_NOT_PRINT_PASSWORD', json.dumps(manifest))
        self.assertIn('STARMIGRT_VERIFY_PASS', self.cli('verify','--input',self.package).stdout)
        merged = self.root/'migration.sql'
        self.assertIn('STARMIGRT_MERGE_PASS', self.cli('merge','--input',self.package,'--output',merged).stdout)
        sql = merged.read_text(encoding='utf-8')
        self.assertIn('CREATE TABLE `db1`.`events`', sql)
        self.assertIn('INSERT INTO `db2`.`events`', sql)
        self.assertNotIn('DROP TABLE', sql)
        self.cli('merge','--input',self.package,'--output',merged, expected=1)
        if os.name != 'nt':
            self.assertEqual(self.package.stat().st_mode & 0o777, 0o700)
            for path in self.package.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_all_databases_and_explicit_filters(self):
        self.fixture['databases']['information_schema'] = {'ignored': table()}
        self.fixture['databases']['db1']['archive'] = table()
        self.save()
        self.cli('dump','--host','x','--user','u','--all-databases','--exclude-database','db2',
                 '--exclude-table','archive','--output',self.package,'--source-quiesced')
        self.assertEqual([t['database']+'.'+t['name'] for t in self.manifest()['plan']['tables']], ['db1.events'])

    def test_unknown_table_unsupported_object_and_type_fail_closed(self):
        self.source('check','--table','missing', expected=1)
        self.fixture['databases']['db1']['view1'] = table(kind='VIEW', engine='')
        self.save()
        self.source(expected=1)
        self.assertFalse(self.package.exists())
        self.source('check','--exclude-table','view1')
        self.fixture['databases']['db1']['events']['columns'][1][1] = 'array'
        self.save()
        self.source('check','--exclude-table','view1',expected=1)
        self.source('check','--schema-only','--exclude-table','view1')

    def test_mapping_collision_and_unknown_mapping_refused(self):
        self.source('check','--database-map','db1=db2',expected=1)
        self.source('check','--database-map','absent=new',expected=1)
        self.source('check','--target-database','new',expected=1)

    def test_gzip_schema_only_and_data_only(self):
        self.source('dump','--gzip','--schema-only')
        self.assertFalse(any(x.startswith('READ ') for x in self.reads()))
        self.assertTrue(all(name.endswith('.gz') for name in self.manifest()['completed']))
        self.cli('verify','--input',self.package)
        result = self.cli('verify','--input',self.package,'--host','target','--user','reader')
        self.assertIn('scope=target-columns',result.stdout)
        self.cli('verify','--input',self.package,'--host','target','--user','reader','--content',expected=1)
        self.cli('merge','--input',self.package,'--output',self.root/'schema.sql')
        self.assertNotIn('INSERT INTO', (self.root/'schema.sql').read_text())
        self.package = self.root/'data-only'
        self.source('dump','--gzip','--data-only')
        self.assertNotIn('CREATE TABLE', (self.package/'schema.sql').read_text())
        self.assertNotIn('CREATE DATABASE', (self.package/'schema.sql').read_text())
        for name in self.manifest()['completed']:
            with gzip.open(self.package/name,'rt') as data: self.assertIn('INSERT INTO',data.read())

    def test_resume_keeps_completed_table_and_restarts_failed_table(self):
        self.fixture['databases']['db2']['events']['fail'] = True
        self.save()
        self.source('dump','--workers','1', expected=1)
        manifest = self.manifest()
        self.assertEqual(manifest['status'], 'incomplete')
        self.assertEqual(list(manifest['completed']), ['table-000001.sql'])
        path = self.package/'table-000001.sql'
        before = path.stat().st_mtime_ns
        self.assertNotIn('DRAIN_BUG',self.reads())
        self.cli('verify','--input',self.package,expected=1)
        self.fixture['databases']['db2']['events']['fail'] = False
        self.save()
        self.source('dump','--resume','--workers','2')
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(self.reads().count('READ db1.events'), 1)
        self.assertEqual(self.manifest()['status'], 'complete')
        self.source('dump','--resume')
        self.assertEqual(self.reads().count('READ db1.events'), 1)

    def test_failure_stops_next_unlaunched_table_and_redacts_error(self):
        self.fixture['databases']['db1'] = {'a': table(fail=True), 'b': table()}
        self.save()
        result = self.source('dump','--workers','1',expected=1)
        self.assertNotIn('private-row-value',result.stderr)
        self.assertNotIn('READ db1.b',self.reads())
        self.assertEqual(list(self.package.glob('*.partial')), [])

    def test_corruption_and_plan_changes_refuse_resume(self):
        self.source()
        self.source('dump','--resume','--replication-num','1',expected=1)
        path = self.package/'table-000001.sql'
        path.write_text('corrupted')
        self.cli('verify','--input',self.package,expected=1)
        self.source('dump','--resume',expected=1)
        self.assertEqual(path.read_text(), 'corrupted')

    def test_target_rows_and_content_detect_changed_values(self):
        self.source()
        self.cli('verify','--input',self.package,'--host','target','--user','reader','--content')
        self.fixture['databases']['db1']['events']['rows'][0][1] = 'changed'
        self.save()
        self.cli('verify','--input',self.package,'--host','target','--user','reader')
        self.cli('verify','--input',self.package,'--host','target','--user','reader','--content',expected=1)
        self.fixture['databases']['db1']['events']['rows'].append([4,'extra'])
        self.save()
        self.cli('verify','--input',self.package,'--host','target','--user','reader',expected=1)

    def test_symlink_and_manifest_path_traversal_are_refused(self):
        self.source()
        manifest = self.manifest()
        original = self.package/'table-000001.sql'
        moved = self.root/'moved.sql'
        original.rename(moved)
        original.symlink_to(moved)
        self.cli('verify','--input',self.package,expected=1)
        manifest['completed']['../moved.sql'] = manifest['completed'].pop('table-000001.sql')
        (self.package/'manifest.json').write_text(json.dumps(manifest))
        self.cli('verify','--input',self.package,expected=1)
        self.assertTrue(moved.exists())

    def test_sql_batches_preserve_all_rows_and_bound_bytes(self):
        self.fixture['databases'] = {'db1': {'events': table(n=10001)}}
        self.save()
        self.cli('dump','--host','x','--user','u','--database','db1','--source-quiesced',
                 '--output',self.package,'--batch-rows','500')
        record = self.manifest()['completed']['table-000001.sql']
        self.assertEqual(record['rows'],10001)
        sql = (self.package/'table-000001.sql').read_text()
        self.assertEqual(sql.count('INSERT INTO'),21)
        self.assertEqual(sql.count("'row-"),10001)
        self.cli('verify','--input',self.package,'--host','target','--user','reader','--content')

    def test_byte_limit_splits_without_splitting_a_row(self):
        self.fixture['databases'] = {'db1': {'events': table([[i,'中' * 200000] for i in range(3)])}}
        self.save()
        self.cli('dump','--host','x','--user','u','--database','db1','--source-quiesced',
                 '--output',self.package,'--batch-rows','500','--batch-mib','1')
        sql = (self.package/'table-000001.sql').read_text()
        statements = ['INSERT INTO' + part for part in sql.split('INSERT INTO')[1:]]
        self.assertEqual(len(statements),3)
        self.assertTrue(all(len(s.encode('utf-8')) <= 1024*1024 for s in statements))

    def test_labels_survive_resume_and_are_unique_between_packages(self):
        self.source()
        first = self.manifest()['package_id']
        sql = (self.package/'table-000001.sql').read_text()
        self.assertIn('WITH LABEL `sr_'+first+'_t000001_b000000000001`',sql)
        self.source('dump','--resume')
        self.assertEqual((self.package/'table-000001.sql').read_text(),sql)
        self.package = self.root/'another'
        self.source()
        self.assertNotEqual(self.manifest()['package_id'],first)

    def test_oversized_row_leaves_incomplete_package_not_truncated_data(self):
        self.fixture['databases']['db1']['events']['rows'] = [[1,'x'*(1024*1024)]]
        self.save()
        self.source('dump','--workers','1',expected=1)
        self.assertFalse((self.package/'table-000001.sql').exists())
        self.assertEqual(self.manifest()['status'],'incomplete')
        self.assertEqual(list(self.package.glob('*.partial')), [])

    def test_password_file_and_tls_flags(self):
        password = self.root/'password'
        password.write_text('DO_NOT_PRINT_PASSWORD\n')
        password.chmod(0o600)
        self.source('check','--password-file',password,'--ssl-ca',self.root/'ca.pem')
        if os.name != 'nt':
            password.chmod(0o644)
            self.source('check','--password-file',password,expected=1)

    def test_tls_refuses_downgrade_before_authentication(self):
        self.fixture['no_tls'] = True
        self.save()
        result = self.source('check','--ssl-ca',self.root/'ca.pem',expected=1)
        self.assertIn('拒绝降级',result.stderr)
        self.assertNotIn('AUTH',self.reads())

    def test_signal_wakes_blocked_query_and_retains_resumable_state(self):
        if os.name == 'nt': self.skipTest('POSIX signal contract')
        self.fixture['databases']['db1']['events']['block'] = True
        self.save()
        process = subprocess.Popen([sys.executable,'-B',str(SCRIPT),'dump','--host','x','--user','u',
                                    '--database','db1','--source-quiesced','--workers','1','--output',str(self.package)],
                                   env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+5
            while 'READ db1.events' not in self.reads() and time.monotonic()<deadline:
                time.sleep(0.02)
            self.assertIn('READ db1.events',self.reads())
            process.send_signal(signal.SIGTERM)
            stdout,stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode,130,stdout+stderr)
            self.assertEqual(list(self.package.glob('*.partial')), [])
            self.assertEqual(self.manifest()['status'],'incomplete')
            self.assertNotIn('DRAIN_BUG',self.reads())
        finally:
            if process.poll() is None: process.kill(); process.communicate()


if __name__ == '__main__': unittest.main()
