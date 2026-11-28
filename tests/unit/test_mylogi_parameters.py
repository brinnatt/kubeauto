"""CLI behavior with native client/systemd fixtures and real archive I/O.

Root and mount admission are supplied fixture preconditions; their real-host
qualification remains with the owning logical-backup runner.
"""
import contextlib
import importlib.util
import io
import json
import os
import shlex
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


class MyLogiParameters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mylogi-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location('mylogi_parameters', ROOT/'tools/mysqltools/MyLogiBackupCli.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.trace = self.base/'calls.jsonl'
        client = self.base/'native'
        client.write_text('#!'+sys.executable+'\n'+r'''
import json,os,pathlib,sys
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\n')
args=sys.argv[1:]
if '--version' in args: print('mysql Ver 8.0.46')
elif args and args[0]=='print':
    name=next(a.split('=',1)[1] for a in args if a.startswith('--login-path='))
    print('['+name+']\nhost = 127.0.0.1\nport = 13306')
elif args and args[0]=='set': pathlib.Path(os.environ['MYLOGI_LOGIN_FILE']).write_bytes(b'encrypted-fixture')
elif any(a.startswith('--execute=') for a in args):
    sql=next(a.split('=',1)[1] for a in args if a.startswith('--execute='))
    if '@@version' in sql: print('8.0.46')
    elif 'SHOW DATABASES' in sql: print('app\naudit\nexcluded\nmysql\nsys\ninformation_schema\nperformance_schema')
    elif 'GROUP BY table_schema' in sql: print('app\t100\t100\naudit\t100\t100')
elif any(a.startswith('--result-file=') for a in args):
    name=next(a.split('=',1)[1] for a in args if a.startswith('--result-file='))
    pathlib.Path(name).write_text('-- fixture\nCREATE TABLE t(id int);\nINSERT INTO t VALUES(1);\n-- Dump completed on 2026-11-28 00:00:00\n')
''')
        client.chmod(0o700)
        login = self.base/'.mylogin.cnf'
        login.write_bytes(b'fixture')
        login.chmod(0o600)
        self.env = dict(CALLS=str(self.trace),MYLOGI_MYSQL_BIN=str(client),
                        MYLOGI_MYSQLDUMP_BIN=str(client),MYLOGI_MYSQL_CONFIG_EDITOR=str(client),
                        MYLOGI_LOGIN_FILE=str(login),MYLOGI_BACKUP_MOUNT=str(self.base),
                        MYLOGI_LOCK_FILE=str(self.base/'backup.lock'),MYLOGI_LOG_FILE=str(self.base/'backup.log'),
                        MYLOGI_SYSTEMD_UNIT_DIR=str(self.base/'units'),MYLOGI_SYSTEMCTL=str(client),
                        MYLOGI_SYSTEMD_ANALYZE=str(client))

    def cli(self, *args, expected=0):
        module = self.module
        output = io.StringIO()
        with mock.patch.dict(os.environ,self.env), mock.patch.object(module,'require_root'), \
                mock.patch.object(module,'login_file_preflight'), mock.patch.object(module.os.path,'ismount',return_value=True), \
                mock.patch.object(module.os,'chown'), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                rc=module.main(list(args))
            except SystemExit as exc:
                rc=exc.code
        for handler in list(module.LOGGER.handlers):
            handler.close()
            module.LOGGER.removeHandler(handler)
        self.assertEqual(rc,expected,output.getvalue()[-4000:])
        return output.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_setup_endpoint_user_and_login_path_reach_editor_without_password_value(self):
        out = self.cli('setup-login','--host','127.0.0.2','--port','13307','--user','backup-user','--login-path','custom-login')
        call=self.calls()[0]
        self.assertEqual(call,['set','--login-path=custom-login','--host=127.0.0.2','--port=13307','--user=backup-user','--password'])
        self.assertIn('MYSQL_BACKUP_LOGIN_PATH_OK',out)
        self.assertEqual((self.base/'.mylogin.cnf').stat().st_mode & 0o777,0o600)
        before=len(self.calls())
        self.cli('setup-login','--host','bad.invalid','--user','u',expected=1)
        self.assertEqual(len(self.calls()),before)
        for port in ('0','65536','bad'):
            self.cli('setup-login','--host','127.0.0.1','--port',port,'--user','u',expected=1)

    def test_backup_scope_native_options_and_each_compression_have_archive_readback(self):
        for compression in ('gz','bz2','xz'):
            out=self.cli('backup','--login-path','custom-login','--database','app','audit',
                         '--exclude-database','audit','--compression',compression,
                         '--native-option','mysqldump=--extended-insert',
                         '--native-option','mysql=--connect-timeout=12')
            self.assertIn('MYSQL_BACKUP_OK',out)
            archives=list(self.base.glob('mysql-*/mysql-*.tar.'+compression))
            self.assertEqual(len(archives),1)
            with tarfile.open(archives[0]) as archive:
                sql=next(m for m in archive.getmembers() if m.name.endswith('.sql'))
                self.assertIn(b'INSERT INTO t VALUES(1)',archive.extractfile(sql).read())
            dump=next(call for call in reversed(self.calls()) if any(a.startswith('--result-file=') for a in call))
            self.assertEqual(dump[-2:],['--databases','app'])
            self.assertIn('--login-path=custom-login',dump)
            self.assertIn('--extended-insert',dump)
            self.assertNotIn('--connect-timeout=12',dump)
            queries=[c for c in self.calls() if any(a.startswith('--execute=') for a in c)]
            self.assertTrue(all('--connect-timeout=12' in c for c in queries))
        self.assertFalse(list(self.base.rglob('*.partial')))

    def test_preflight_defaults_repeated_scope_and_exclusion_do_not_dump(self):
        out=self.cli('preflight')
        self.assertIn('MYSQL_BACKUP_PREFLIGHT_OK',out)
        self.assertIn('"app", "audit", "excluded", "mysql"',out)
        self.assertTrue(any('--login-path=mysql-backup' in c for c in self.calls()))
        out=self.cli('preflight','--login-path','other','--database','app','--database','audit',
                     '--exclude-database','app','--compression','xz','--native-option','mysql=--connect-timeout=22')
        self.assertIn('["audit"]',out)
        self.assertIn('--login-path=other',self.calls()[-1])
        self.assertIn('--connect-timeout=22',self.calls()[-1])
        self.assertFalse(any(any(a.startswith('--result-file=') for a in c) for c in self.calls()))
        self.assertFalse(list(self.base.glob('mysql-*/*.tar.*')))
        for command in ('preflight','backup'):
            for scope in (['--database','app','app'],['--database','absent'],['--database','sys'],
                          ['--database','app','--exclude-database','app']):
                self.cli(command,*scope,expected=1)

    def test_timer_preserves_scope_time_native_options_and_executable_then_removes_units(self):
        executable=self.base/'tool with spaces'
        executable.write_text('#!/bin/sh\nexit 0\n');executable.chmod(0o700)
        for flags,at,compression in (([], '01:01','gz'),
                                     (['--at','23:59','--login-path','timer-login','--database','app','audit',
                                       '--exclude-database','audit','--compression','xz',
                                       '--native-option','mysqldump=--extended-insert'],'23:59','xz')):
            self.cli('timer','install','--exec-path',str(executable),*flags)
            service=(self.base/'units/mysql-logi-backup.service').read_text()
            timer=(self.base/'units/mysql-logi-backup.timer').read_text()
            argv=shlex.split(next(line.split('=',1)[1] for line in service.splitlines() if line.startswith('ExecStart=')))
            self.assertIn(str(executable),service)
            self.assertEqual(argv[argv.index('--compression')+1],compression)
            self.assertIn(at+':00',timer)
            if flags:
                self.assertEqual(argv[argv.index('--login-path')+1],'timer-login')
                self.assertEqual(argv[argv.index('--database')+1:argv.index('--database')+3],['app','audit'])
                self.assertEqual(argv[argv.index('--exclude-database')+1],'audit')
                self.assertIn('--native-option',argv)
            self.assertIn('MYSQL_BACKUP_TIMER_OK',self.cli('timer','status'))
            self.cli('timer','remove')
            self.assertFalse(list((self.base/'units').iterdir()))
        before=len(self.calls())
        for at in ('24:00','12:60','1:00','bad'):
            self.cli('timer','install','--exec-path',str(executable),'--at',at,expected=1)
        self.cli('timer','install','--exec-path','relative-path',expected=1)
        self.assertEqual(len(self.calls()),before)


if __name__ == '__main__':
    unittest.main()
