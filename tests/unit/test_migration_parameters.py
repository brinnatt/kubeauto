"""Migration CLI options must reach real task, dump and report boundaries."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]


class MigrationParameters(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='migration-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        with mock.patch.dict(os.environ,MYSQL_MIGRATION_LOG_DIR=self.tmp.name):
            spec=importlib.util.spec_from_file_location('migration_parameters',ROOT/'tools/mysqltools/MigrationCli.py')
            self.m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.m;spec.loader.exec_module(self.m)

    def invoke(self,*flags):
        tasks=[];m=self.m
        def execute(manager,task):
            tasks.append(task)
            return dict(task_id=task.source.database+'->'+task.target.database,status='completed')
        with mock.patch.object(sys,'argv',['MigrationCli',*map(str,flags)]), \
                mock.patch.object(m,'check_dependencies'), mock.patch.object(m,'setup_signal_handlers'), \
                mock.patch.object(m.MigrationManager,'execute_migration',execute), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                m.main(); code=0
            except SystemExit as error:
                code=error.code
        self.assertEqual(code,0)
        return tasks

    def test_all_cli_options_reach_tasks_and_real_dump_command(self):
        m=self.m
        task,=self.invoke('-s','source.invalid:13306:source_db:user:private:secret',
            '-t','target.invalid:23306:target_db:user:private',
            '--dry-run','--keep-dump-files','--max-workers','3','--max-workers-per-target-host','2',
            '--no-per-table','--no-compress','--dump-timeout','123','--import-timeout','456',
            '--complete-insert','--gtid-mode','commented','--replication-target','--ssl-mode','REQUIRED',
            '--add-drop-table','--force-overwrite','--skip-target-empty-check','--no-fix-definer',
            '--rollback-on-failure','--exact-row-count','--table-checksum','--skip-version-check',
            '--report-dir',self.base,'--native-option','mysqldump=--net-buffer-length=32768')
        opts=task.options
        expected=dict(dry_run=True,keep_dump_files=True,max_workers=3,max_workers_per_target_host=2,
            per_table=False,compress_dump=False,dump_timeout=123,import_timeout=456,complete_insert=True,
            replication_target=True,add_drop_table=True,force_overwrite=True,skip_target_empty_check=True,
            fix_definer=False,rollback_on_failure=True,exact_row_count=True,table_checksum=True,
            skip_version_check=True,report_dir=str(self.base))
        for key,value in expected.items():
            self.assertEqual(getattr(opts,key),value,key)
        self.assertEqual(opts.gtid_mode.value,'commented')
        self.assertEqual(opts.ssl_mode.value,'REQUIRED')
        self.assertEqual(task.source.password,'private:secret')
        manager=m.MySQLDumpManager(m.MigrationLogger(),opts,m.ProcessRegistry())
        self.addCleanup(manager.cleanup)
        manager.set_dump_context(gtid_purged='COMMENTED',skip_lock_tables=False)
        argv=manager.build_mysqldump_command(task.source,str(self.base/'private.cnf'),str(self.base/'dump.sql'))
        for flag in ('--complete-insert','--add-drop-table','--set-gtid-purged=COMMENTED',
                     '--net-buffer-length=32768','--protocol=TCP','-P13306'):
            self.assertIn(flag,argv)
        self.assertNotIn('private:secret',' '.join(argv))
        report=json.loads(next(self.base.glob('migration_report_*.json')).read_text())
        self.assertEqual(report['summary']['success'],1)

    def test_structure_only_defaults_and_config_task_precedence(self):
        m=self.m
        task,=self.invoke('--source','source.invalid:3306:source_db:user:secret',
            '--target','target.invalid:3306:target_db:user:secret','--structure-only','--report-dir',self.base)
        self.assertEqual(task.source.mode,m.MigrationMode.STRUCTURE_ONLY)
        self.assertTrue(task.options.per_table)
        self.assertTrue(task.options.compress_dump)
        self.assertTrue(task.options.fix_definer)
        self.assertEqual(task.options.max_workers,2)
        config=self.base/'migration.json'
        endpoint=dict(host='source.invalid',port=3306,database='fixture',user='u',password='${FIXTURE_SECRET}')
        config.write_text(json.dumps(dict(options=dict(max_workers=1,compress_dump=False),
            migrations=[dict(source=endpoint,target=dict(endpoint,host='target.invalid'),
                             options=dict(max_workers=2,dump_timeout=90))])))
        with mock.patch.dict(os.environ,FIXTURE_SECRET='private'):
            task,=self.invoke('--config',config,'--max-workers','3','--report-dir',self.base)
        self.assertEqual(task.options.max_workers,3)
        self.assertEqual(task.options.dump_timeout,90)
        self.assertFalse(task.options.compress_dump)
        self.assertEqual(task.source.password,'private')

    def test_nonpositive_cli_limits_refuse_before_dependency_or_database_access(self):
        for flag in ('--max-workers','--max-workers-per-target-host','--dump-timeout','--import-timeout'):
            with self.subTest(flag=flag),mock.patch.object(sys,'argv',['MigrationCli',flag,'0']), \
                    mock.patch.object(self.m,'check_dependencies') as dependency:
                with self.assertRaises(SystemExit) as error:
                    self.m.main()
                self.assertNotEqual(error.exception.code,0)
                dependency.assert_not_called()


if __name__=='__main__':
    unittest.main()
