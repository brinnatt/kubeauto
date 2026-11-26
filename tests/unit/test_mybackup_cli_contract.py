import importlib.util
import json
import os
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "mybackupcli", ROOT / "tools/mysqltools/MyBackupCli.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)


def manager_for(tmpdir):
    config = mock.Mock()
    config.backup_base = Path(tmpdir) / "backup"
    config.mysql_datadir = Path(tmpdir) / "mysql"
    config.mysql_binlog_prefix = "mysql-bin"
    config.mysql_binlog_dir = config.mysql_datadir
    config.mysql_binlog_index = config.mysql_binlog_dir / 'mysql-bin.index'
    config.mysql_container = ''
    config.offline = False
    config.mysql_topology = 'standalone'
    config.lock_file = config.backup_base / "lock/mysqlbackup.lock"
    config.audit_base = config.backup_base / "restore_audit"
    manager = MOD.BackupManager.__new__(MOD.BackupManager)
    manager.config = config
    manager.today = "2026-09-11"
    manager.now = "120000"
    manager.logger = logging.getLogger("mybackup-contract")
    # These tests isolate storage/argv handling; real replication checks have
    # their own rejection and restore-order contracts.
    manager._replication_backup_options = mock.Mock(return_value=[])
    manager._replication_restore_check = mock.Mock()
    manager._replication_state = mock.Mock(return_value={'channels': 0})
    manager._donor_restore = mock.Mock(return_value=False)
    return manager


class MyBackupCliContractTest(unittest.TestCase):
    def test_unusable_binlog_client_refuses_restore_before_service_or_data_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=manager_for(directory)
            manager._load_restore_plan=mock.Mock(return_value=({},Path(directory)/'plan.json'))
            manager.manage_mysql_service=mock.Mock(side_effect=AssertionError('service touched before client check'))
            manager.run_cmd=mock.Mock(side_effect=RuntimeError('mysqlbinlog unavailable'))
            with self.assertRaisesRegex(RuntimeError,'mysqlbinlog unavailable'):
                manager.restore('2026-09-11','2026-09-11 00:00:00',confirm_destructive=True)
            manager.manage_mysql_service.assert_not_called()
            manager.run_cmd.assert_called_once_with(['mysqlbinlog','--version'])

    def test_new_full_starts_a_new_incremental_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.ensure_dirs()
            old = manager.config.backup_base / 'incr/2026-09-11/115959'
            old.mkdir(parents=True); (old / '.backup_ok').touch()
            latest = manager.config.backup_base / 'incr/latest'; latest.symlink_to(old)
            manager.backup_lock_ddl = mock.Mock(return_value='ON')
            def backup(argv, check=True):
                target = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--target-dir=')))
                for name in ('xtrabackup_checkpoints', 'xtrabackup_info'):
                    (target / name).write_text('from_lsn = 0\nto_lsn = 200\n')
                return ''
            manager.run_cmd = backup
            manager.full_backup()
            manager.now = '120001'
            manager.run_cmd = mock.Mock(side_effect=RuntimeError('command boundary'))
            with self.assertRaisesRegex(RuntimeError, 'command boundary'):
                manager.incr_backup()
            argv = manager.run_cmd.call_args.args[0]
            expected = manager.config.backup_base / 'full/2026-09-11/120000/backup'
            self.assertIn('--incremental-basedir=' + str(expected), argv)

    def test_purge_does_not_delete_old_mtime_files_inside_recent_backup(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            today = MOD.datetime.now().date()
            full = manager.config.backup_base / 'full' / str(today) / '120000/backup'
            full.mkdir(parents=True); (full / '.backup_ok').touch()
            data = full / 'unchanged.ibd'; data.write_bytes(b'valid snapshot of an old table')
            os.utime(data, (0, 0))
            expired = manager.config.backup_base / 'full/2000-01-01'; expired.mkdir()
            manager.purge()
            self.assertTrue(data.is_file(), 'Retention must apply to backup generations, not table file mtimes')
            self.assertFalse(expired.exists())
            base_day = str(today - timedelta(days=18))
            previous = manager.config.backup_base / 'full' / base_day / '120000/backup'
            previous.mkdir(parents=True); (previous / '.backup_ok').touch()
            delta = manager.config.backup_base / 'incr' / str(today-timedelta(days=15)) / '120001'
            delta.mkdir(parents=True); (delta / '.backup_ok').touch()
            manager.purge()
            self.assertTrue(previous.is_dir(), 'Keep the full base needed by increments inside the retention window')
            self.assertTrue(delta.is_dir())

    def test_new_full_preserves_old_binlogs_and_archives_reused_names_after_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            manager.config.mysql_datadir.mkdir()
            manager.config.mysql_binlog_index.write_text('mysql-bin.000001\n')
            source = manager.config.mysql_binlog_dir / 'mysql-bin.000001'
            manager.backup_lock_ddl = mock.Mock(return_value='ON')
            def backup(argv, check=True):
                target = Path(next(a.split('=', 1)[1] for a in argv if a.startswith('--target-dir=')))
                for name in ('xtrabackup_checkpoints', 'xtrabackup_info'):
                    (target / name).write_text('from_lsn = 0\nto_lsn = 100\n')
                return ''
            manager.run_cmd = backup
            source.write_bytes(b'before restore')
            manager.full_backup(); manager.binlog_backup(backall=True)
            archived = list((manager.config.backup_base / 'binlog').rglob(source.name))
            self.assertEqual(len(archived), 1)
            before = archived[0]
            source.write_bytes(b'new history with the same filename')
            manager.now = '120001'
            manager.full_backup(); manager.binlog_backup(backall=True)
            archived = list((manager.config.backup_base / 'binlog').rglob(source.name))
            self.assertEqual(len(archived), 2, 'A new full must not reuse an archive cursor from the restored history')
            self.assertEqual(before.read_bytes(), b'before restore')
            after = next(path for path in archived if path != before)
            self.assertEqual(after.read_bytes(), source.read_bytes())

    def test_audit_selects_latest_full_and_only_its_lsn_chain_across_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            base = manager.config.backup_base
            records = [('full/2026-09-11/110000/backup', 0, 100),
                       ('incr/2026-09-11/110001', 100, 150),
                       ('full/2026-09-11/120000/backup', 0, 200),
                       ('incr/2026-09-11/120001', 200, 250),
                       ('incr/2026-09-12/000001', 250, 300)]
            for name, start, end in records:
                path = base / name; path.mkdir(parents=True)
                (path / '.backup_ok').touch()
                (path / 'xtrabackup_checkpoints').write_text('from_lsn = {}\nto_lsn = {}\n'.format(start, end))
            (base / 'binlog/mysql-bin.000001').write_bytes(b'log')
            manager.restore_audit('2026-09-11')
            plan = json.loads((manager.config.audit_base / '2026-09-11/restore.plan.json').read_text())
            self.assertEqual(plan['full'], str(base / records[2][0]))
            self.assertEqual(plan['incr'], [str(base / records[i][0]) for i in (3, 4)])

    def test_copyback_preserves_external_non_binlog_files_and_restores_container_index(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            data = manager.config.mysql_datadir; data.mkdir()
            (data / 'original').write_bytes(b'original')
            owner = data.stat()
            logs = Path(directory) / 'logs'; logs.mkdir()
            manager.config.mysql_binlog_dir = logs
            manager.config.mysql_binlog_index = logs / 'mysql-bin.index'
            manager.config.mysql_container = 'production-mysql'
            manager.config.mysql_binlog_index.write_text('/var/lib/mysql-logs/mysql-bin.000009\n')
            (logs / 'mysql-bin.000009').write_bytes(b'old log')
            (logs / 'error.log').write_bytes(b'keep error log')
            commands = []
            def command(argv, check=True):
                commands.append(argv)
                if '--copy-back' in argv:
                    (data / 'restored').write_bytes(b'restored')
                    (logs / 'mysql-bin.000001').write_bytes(b'backup log')
                    manager.config.mysql_binlog_index.write_text(str(logs / 'mysql-bin.000001') + '\n')
                return ''
            manager.run_cmd = command
            manager._restore_datadir({'full': str(Path(directory) / 'backup'),
                                      'server_binlog_dir': '/var/lib/mysql-logs'})
            self.assertIn('--log-bin=' + str(logs / 'mysql-bin'), commands[0])
            self.assertIn('--log-bin-index=' + str(manager.config.mysql_binlog_index), commands[0])
            self.assertEqual(manager.config.mysql_binlog_index.read_text(), '/var/lib/mysql-logs/mysql-bin.000001\n')
            self.assertFalse((logs / 'mysql-bin.000009').exists())
            self.assertEqual((logs / 'error.log').read_bytes(), b'keep error log')
            self.assertIn(['chown', '-R', '{}:{}'.format(owner.st_uid, owner.st_gid), str(data)], commands)

    def test_failed_copyback_puts_original_datadir_and_external_binlogs_back(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            data = manager.config.mysql_datadir; data.mkdir()
            (data / 'original').write_bytes(b'original')
            logs = Path(directory) / 'logs'; logs.mkdir()
            manager.config.mysql_binlog_dir = logs
            manager.config.mysql_binlog_index = logs / 'mysql-bin.index'
            manager.config.mysql_binlog_index.write_text('/var/lib/mysql-logs/mysql-bin.000009\n')
            (logs / 'mysql-bin.000009').write_bytes(b'old log')
            def fail_copyback(argv, check=True):
                if '--copy-back' in argv:
                    (data / 'partial').write_bytes(b'partial')
                    raise RuntimeError('copy-back failed')
                return ''
            manager.run_cmd = fail_copyback
            with self.assertRaisesRegex(RuntimeError, 'copy-back failed'):
                manager._restore_datadir({'full': str(Path(directory) / 'backup')})
            self.assertEqual((data / 'original').read_bytes(), b'original')
            self.assertFalse((data / 'partial').exists())
            self.assertEqual((logs / 'mysql-bin.000009').read_bytes(), b'old log')
            self.assertEqual(manager.config.mysql_binlog_index.read_text(), '/var/lib/mysql-logs/mysql-bin.000009\n')

    def test_wrong_container_mapping_blocks_full_and_incremental_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            manager.config.mysql_container = 'production-mysql'
            manager.manage_mysql_service = mock.Mock(return_value=True)
            manager._verify_container_mounts = mock.Mock(side_effect=RuntimeError('wrong mount'))
            manager.run_cmd = mock.Mock()
            for operation in (manager.full_backup, manager.incr_backup):
                with self.assertRaisesRegex(RuntimeError, 'wrong mount'):
                    operation()
            manager.run_cmd.assert_not_called()

    def test_container_stop_uses_pinned_docker_identity_instead_of_systemctl(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.config.mysql_container = 'production-mysql'
            identity = 'a' * 64
            commands = []
            def command(argv, check=True):
                commands.append(argv)
                if argv[:2] == ['docker', 'inspect']:
                    return identity + (' exited' if any(c[:2] == ['docker', 'stop'] for c in commands) else ' running')
                return ''
            manager.run_cmd = command
            self.assertTrue(manager.manage_mysql_service('stop'))
            self.assertIn(['docker', 'stop', '--timeout', '30', identity], commands)
            self.assertTrue(all(c[0] == 'docker' for c in commands))

    def test_container_start_waits_for_mysql_sql_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.config.mysql_container = 'production-mysql'
            manager.run_cmd = mock.Mock(side_effect=['a' * 64 + ' exited', '', 'a' * 64 + ' running', 'a' * 64 + ' running'])
            manager.mysql_cmd = mock.Mock(side_effect=[RuntimeError('not ready'), '1'])
            with mock.patch.object(MOD.time, 'sleep'):
                self.assertTrue(manager.manage_mysql_service('start'))
            self.assertEqual(manager.mysql_cmd.call_count, 2)

    def test_full_and_incremental_pass_host_visible_datadir_to_xtrabackup(self):
        class CommandBoundary(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.config.mysql_datadir = Path(directory) / 'container-bind-source'
            manager.config.mysql_datadir.mkdir()
            manager.config.mysql_binlog_dir = Path(directory) / 'separate-logs'
            manager.config.mysql_binlog_index = manager.config.mysql_binlog_dir / 'mysql-bin.index'
            manager.backup_lock_ddl = mock.Mock(return_value='ON')
            manager.run_cmd = mock.Mock(side_effect=CommandBoundary)
            with self.assertRaises(CommandBoundary):
                manager.full_backup()
            full_command = manager.run_cmd.call_args.args[0]
            expected = '--datadir=' + str(manager.config.mysql_datadir)
            self.assertIn(expected, full_command)
            self.assertIn('--log-bin=' + str(manager.config.mysql_binlog_dir / 'mysql-bin'), full_command)
            self.assertIn('--log-bin-index=' + str(manager.config.mysql_binlog_index), full_command)
            self.assertEqual(sum(arg.startswith('--datadir') for arg in full_command), 1)

            full = manager.config.backup_base / 'full/2026-09-11/120000/backup'
            full.mkdir(parents=True)
            (full / '.backup_ok').touch()
            (manager.config.backup_base / 'full/latest_raw').symlink_to(full)
            with self.assertRaises(CommandBoundary):
                manager.incr_backup()
            incremental_command = manager.run_cmd.call_args.args[0]
            self.assertIn(expected, incremental_command)
            self.assertIn('--log-bin=' + str(manager.config.mysql_binlog_dir / 'mysql-bin'), incremental_command)
            self.assertIn('--log-bin-index=' + str(manager.config.mysql_binlog_index), incremental_command)
            self.assertEqual(sum(arg.startswith('--datadir') for arg in incremental_command), 1)
            self.assertIn('--incremental-basedir=' + str(full), incremental_command)

    def test_archive_reads_host_visible_external_binlog_directory_and_custom_index(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.ensure_dirs()
            manager.config.mysql_binlog_dir = Path(directory) / 'host-logs'
            manager.config.mysql_binlog_dir.mkdir()
            manager.config.mysql_binlog_index = manager.config.mysql_binlog_dir / 'custom.index'
            manager.config.mysql_binlog_index.write_text('/var/lib/mysql-logs/mysql-bin.000001\n')
            (manager.config.mysql_binlog_dir / 'mysql-bin.000001').write_bytes(b'binlog')
            manager.binlog_backup(backall=True)
            self.assertEqual((manager.config.backup_base / 'binlog' / manager.today / 'mysql-bin.000001').read_bytes(), b'binlog')

    def test_ignore_error_cannot_publish_a_partial_dump(self):
        for target in MOD.NATIVE_TARGETS:
            for token in ("--ignore-error=1146", "--loose-ignore_error=1146", "--ignore-err=1146"):
                with self.subTest(target=target, token=token), self.assertRaises(ValueError):
                    MOD.native_arguments(target, token)

    def test_customer_help_works_without_mysql_credentials_or_writable_log(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'never.log'
            for arguments in (['--help'], ['full', '--help'], ['--help-native']):
                result = subprocess.run([sys.executable, str(ROOT/'tools/mysqltools/MyBackupCli.py')] + arguments,
                    env=dict(os.environ, PATH='/nonexistent', MYSQL_BACKUP_PASSWORD='', MYSQLBACKUP_LOG_FILE=str(log)),
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertIn('--native-option', result.stdout)
                self.assertFalse(log.exists())
            for target in MOD.NATIVE_TARGETS:
                self.assertIn(target, result.stdout)
            for option in MOD.NATIVE_PROTECTED:
                self.assertIn('--' + option, result.stdout)
            self.assertIn('正常入口', result.stdout)

    def test_native_help_examples_are_accepted_by_real_argument_parser(self):
        import shlex
        for line in MOD.native_option_help().splitlines():
            if line.strip().startswith('MyBackupCli '):
                with self.subTest(example=line), mock.patch('sys.argv', shlex.split(line)):
                    args = MOD.parse_arguments()
                    self.assertTrue(args.native_options)

    def test_binlog_index_absolute_entries_are_normalized_to_datadir_names(self):
        manager = MOD.BackupManager.__new__(MOD.BackupManager)
        manager.config = mock.Mock()
        manager.config.mysql_datadir = Path("/var/lib/mysql")
        manager.config.mysql_binlog_prefix = "mysql-bin"
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "mysql"
            datadir.mkdir()
            index = datadir / "mysql-bin.index"
            index.write_text("/var/lib/mysql/mysql-bin.000001\n./mysql-bin.000002\n")
            self.assertEqual(
                [Path(line.strip()).name for line in index.read_text().splitlines()],
                ["mysql-bin.000001", "mysql-bin.000002"],
            )

    def test_failed_external_command_stderr_is_visible(self):
        import io
        import sys
        manager = manager_for('/tmp')
        manager.config.mysql_password = 'secret'
        output = io.StringIO()
        manager.logger = logging.Logger('failed-external')
        manager.logger.addHandler(logging.StreamHandler(output))
        with self.assertRaisesRegex(RuntimeError, 'rc=17'):
            manager.run_cmd([sys.executable, '-c', 'import sys;print("denied secret",file=sys.stderr);sys.exit(17)'])
        self.assertIn('denied ***', output.getvalue())
        self.assertNotIn('secret', output.getvalue())

    def test_config_rejects_group_or_world_readable_password_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({"mysql_password": "secret"}))
            cfg.chmod(0o644)
            with self.assertRaises(SystemExit):
                MOD.Config(str(cfg))

    def test_config_rejects_non_positive_xtrabackup_threads(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({
                "mysql_password": "secret", "xtrabackup_parallel": 0
            }))
            cfg.chmod(0o600)
            with mock.patch.object(MOD.Config, "_detect_mysql_version", return_value=(8, 4)):
                with self.assertRaises(SystemExit):
                    MOD.Config(str(cfg))

    def test_config_rejects_mysql_nine_for_physical_backup(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "backup.json"
            cfg.write_text(json.dumps({"mysql_password": "secret"}))
            cfg.chmod(0o600)
            with mock.patch.object(MOD.Config, "_detect_mysql_version", return_value=(9, 0)):
                with self.assertRaises(SystemExit):
                    MOD.Config(str(cfg))

    def test_command_redacts_inline_password(self):
        self.assertNotIn(
            "s3cret",
            MOD.BackupManager._redact_command(
                ["mysql", "--password=s3cret", "--socket=/run/mysql.sock"]
            ),
        )
        self.assertIn("--password=***", MOD.BackupManager._redact_command(["--password=s3cret"]))

    def test_lock_conflict_is_nonzero_failure(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            mgr.ensure_dirs()
            first = mgr.acquire_lock()
            try:
                with self.assertRaises(SystemExit) as raised:
                    mgr.acquire_lock()
                self.assertEqual(raised.exception.code, 1)
            finally:
                MOD.fcntl.flock(first, MOD.fcntl.LOCK_UN)
                os.close(first)

    def test_latest_full_date_ignores_symlink_and_non_date_directories(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full"
            (full / "2026-09-10").mkdir(parents=True)
            (full / "not-a-date").mkdir()
            (full / "latest_raw").symlink_to(full / "2026-09-10")
            self.assertEqual(mgr.latest_full_date(), "2026-09-10")

    def test_incremental_refuses_unmarked_latest_full(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            (mgr.config.backup_base / "full/latest_raw").symlink_to(full)
            with self.assertRaises(SystemExit):
                mgr.incr_backup()

    def test_binlog_archive_does_not_advance_past_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            mgr.config.mysql_datadir.mkdir(parents=True)
            (mgr.config.mysql_datadir / "mysql-bin.000001").write_bytes(b"one")
            (mgr.config.mysql_datadir / "mysql-bin.000001.index").write_text("")
            (mgr.config.mysql_datadir / "mysql-bin.index").write_text(
                "./mysql-bin.000001\n./mysql-bin.000002\n./mysql-bin.000003\n"
            )
            mgr.ensure_dirs()
            (mgr.config.mysql_datadir / "mysql-bin.000003").write_bytes(b"three")
            with self.assertRaisesRegex(RuntimeError, "missing"):
                mgr.binlog_backup(backall=True)
            state = mgr.config.backup_base / "binlog/state/last_archived"
            self.assertFalse(state.exists())
            self.assertTrue((mgr.config.backup_base / "binlog/2026-09-11/mysql-bin.000001").exists())

    def test_binlog_replay_uses_last_incremental_position_and_rejects_missing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            full, increment = Path(directory) / 'full', Path(directory) / 'increment'
            full.mkdir(); increment.mkdir()
            (full / 'xtrabackup_binlog_info').write_text('mysql-bin.000001 10\n')
            (increment / 'xtrabackup_binlog_info').write_text('mysql-bin.000002 123 uuid:1-5\n')
            archive = manager.config.backup_base / 'binlog'
            for number in (1, 2, 3):
                (archive / ('mysql-bin.%06d' % number)).write_bytes(b'log')
            plan = {'full': str(full), 'incr': [str(increment)],
                    'binlog': ['mysql-bin.%06d' % n for n in (1, 2, 3)]}
            position, files = manager._binlog_replay_files(plan)
            self.assertEqual(position, 123)
            self.assertEqual([p.name for p in files], ['mysql-bin.000002', 'mysql-bin.000003'])
            files[-1].unlink()
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                manager._binlog_replay_files(plan)
            plan['binlog'] = ['mysql-bin.000003']
            with self.assertRaisesRegex(RuntimeError, 'snapshot'):
                manager._binlog_replay_files(plan)

    def test_container_mount_verification_rejects_wrong_daemon_readonly_and_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.config.mysql_socket = str(manager.config.mysql_datadir / 'mysql.sock')
            manager._mysql_container_id = 'a' * 64
            manager.mysql_cmd = mock.Mock(return_value='/var/lib/mysql/\t/var/lib/mysql/mysql-bin\t/var/lib/mysql/mysql-bin.index\t/var/lib/mysql/mysql.sock\n')
            mounts = [{'Type': 'bind', 'RW': True, 'Source': str(manager.config.mysql_datadir), 'Destination': '/var/lib/mysql'}]
            manager.run_cmd = lambda *args, **kwargs: json.dumps(mounts)
            plan = {}; manager._verify_container_mounts(plan)
            self.assertEqual(plan['server_binlog_dir'], '/var/lib/mysql')
            mounts[0]['Source'] = str(Path(directory) / 'foreign')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                manager._verify_container_mounts({})
            mounts[0]['Source'] = str(manager.config.mysql_datadir); mounts[0]['RW'] = False
            with self.assertRaisesRegex(RuntimeError, 'writable bind'):
                manager._verify_container_mounts({})
            mounts[0]['RW'] = True
            manager.mysql_cmd.return_value = manager.mysql_cmd.return_value.replace('/mysql-bin\t', '/different-prefix\t')
            with self.assertRaisesRegex(RuntimeError, 'prefix'):
                manager._verify_container_mounts({})

    def test_legacy_binlog_lookup_excludes_new_generation_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            archive = manager.config.backup_base / 'binlog'
            (archive / 'mysql-bin.000001').write_bytes(b'legacy')
            other = archive / 'generation/2026-09-12/000000/2026-09-12'; other.mkdir(parents=True)
            (other / 'mysql-bin.000001').write_bytes(b'another history')
            self.assertEqual(manager.check_binlog_sequence(archive), ['mysql-bin.000001'])

    def test_restored_history_refuses_incremental_and_archival_until_new_full(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            (manager.config.backup_base / 'lock/restore-needs-full').touch()
            for operation in (manager.incr_backup, manager.binlog_backup):
                with self.assertRaisesRegex(RuntimeError, 'new full'):
                    operation()

    def test_binlog_sequence_is_numeric_and_rejects_gap(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            directory = Path(td) / "binlog"
            directory.mkdir()
            for name in ("mysql-bin.000010", "mysql-bin.000009"):
                (directory / name).write_bytes(b"x")
            self.assertEqual(
                mgr.check_binlog_sequence(directory),
                ["mysql-bin.000009", "mysql-bin.000010"],
            )
            (directory / "mysql-bin.000012").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                mgr.check_binlog_sequence(directory)

    def test_restore_requires_explicit_destructive_confirmation(self):
        mgr = manager_for(tempfile.mkdtemp())
        with self.assertRaises(SystemExit):
            mgr.restore(date="2026-09-11", binlog_start_time="2026-09-11 00:00:00")

    def test_restore_refuses_running_mysql_when_configured_service_is_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory)
            manager.run_cmd = mock.Mock()
            manager.config.backup_base.mkdir()
            full = Path(directory) / 'full'; full.mkdir()
            manager._load_restore_plan = mock.Mock(return_value=({'full': str(full), 'incr': [], 'binlog': []}, Path(directory) / 'plan.json'))
            manager.manage_mysql_service = mock.Mock(return_value=False)
            manager.mysql_cmd = mock.Mock(return_value='1')
            for name in ('binlog_backup', '_decompress_backup', '_binlog_replay_files', '_prepare_backup', '_restore_datadir', '_apply_binlog', '_update_restore_plan'):
                setattr(manager, name, mock.Mock())
            with self.assertRaisesRegex(RuntimeError, 'still accepting SQL'):
                manager.restore(date='2026-09-11', binlog_start_time='2099-01-01 00:00:00', confirm_destructive=True)
            manager.binlog_backup.assert_not_called()
            manager._restore_datadir.assert_not_called()

    def test_prepare_interrupt_preserves_running_service_before_stop(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            mgr.run_cmd = mock.Mock()
            mgr.config.backup_base.mkdir()
            full = Path(td) / 'full'; full.mkdir()
            plan_file = Path(td) / "restore.plan.json"
            service_calls = []

            def service(action, timeout=30):
                service_calls.append((action, timeout))
                # status before restore, stop, status during recovery, start
                return [True, True, False, True][len(service_calls) - 1]

            with mock.patch.object(
                mgr, "_load_restore_plan", return_value=({'full': str(full), 'incr': [], "binlog": []}, plan_file)
            ), mock.patch.object(mgr, "manage_mysql_service", side_effect=service), mock.patch.object(mgr, 'mysql_cmd', side_effect=RuntimeError('server stopped')), mock.patch.object(
                mgr, "binlog_backup"
            ), mock.patch.object(
                mgr, "_decompress_backup", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    mgr.restore(
                        date="2026-09-11",
                        binlog_start_time="2026-09-11 00:00:00",
                        confirm_destructive=True,
                    )

            self.assertEqual(service_calls, [('status', 30)])

    def test_compressed_binlog_coordinates_are_validated_in_workspace_before_stop(self):
        for missing_archive in (False, True):
            with self.subTest(missing=missing_archive), tempfile.TemporaryDirectory() as directory:
                manager = manager_for(directory); manager.ensure_dirs()
                manager.run_cmd = mock.Mock()
                full = manager.config.backup_base / 'full/2026-09-11/120000/backup'; full.mkdir(parents=True)
                (full / '.backup_ok').touch()
                (full / 'xtrabackup_checkpoints').write_text('from_lsn = 0\nto_lsn = 100\n')
                (full / 'xtrabackup_binlog_info.zst').write_bytes(b'compressed fixture')
                plan = {'date': '2026-09-11', 'status': 'READY', 'full': str(full), 'incr': [], 'binlog': ['mysql-bin.000001']}
                plan_file = manager.config.audit_base / '2026-09-11/restore.plan.json'
                plan_file.parent.mkdir(parents=True); plan_file.write_text(json.dumps(plan))
                if not missing_archive:
                    (manager.config.backup_base / 'binlog/mysql-bin.000001').write_bytes(b'log')
                events = []
                def decompress(working):
                    events.append('decompress')
                    path = Path(working['full'])
                    self.assertNotEqual(path, full)
                    self.assertTrue((path / 'xtrabackup_binlog_info.zst').is_file())
                    (path / 'xtrabackup_binlog_info').write_text('mysql-bin.000001 123\n')
                manager._decompress_backup = decompress
                manager._prepare_backup = lambda _: events.append('prepare')
                manager.manage_mysql_service = lambda action, **kwargs: events.append(action) or True
                manager.mysql_cmd = mock.Mock(side_effect=RuntimeError('stopped'))
                for name in ('binlog_backup', '_restore_datadir', '_apply_binlog', '_update_restore_plan'):
                    setattr(manager, name, mock.Mock())
                if missing_archive:
                    with self.assertRaisesRegex(RuntimeError, 'missing'):
                        manager.restore('2026-09-11', '2099-01-01 00:00:00', confirm_destructive=True)
                    self.assertEqual(events, ['status', 'decompress'])
                else:
                    manager.restore('2026-09-11', '2099-01-01 00:00:00', confirm_destructive=True)
                    self.assertLess(events.index('decompress'), events.index('stop'))
                    self.assertNotEqual(manager._apply_binlog.call_args.args[0]['full'], str(full))
                self.assertFalse((full / 'xtrabackup_binlog_info').exists())

    def test_prepare_and_copyback_operate_on_workspace_without_consuming_raw_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = manager_for(directory); manager.ensure_dirs()
            manager.run_cmd = mock.Mock()
            full = manager.config.backup_base / 'full/raw'; full.mkdir()
            original = full / 'data'; original.write_bytes(b'raw data')
            manager._load_restore_plan = mock.Mock(return_value=({'full': str(full), 'incr': [], 'binlog': []}, Path(directory) / 'plan.json'))
            manager.manage_mysql_service = mock.Mock(return_value=True)
            manager.mysql_cmd = mock.Mock(side_effect=RuntimeError('stopped'))
            manager.binlog_backup = mock.Mock()
            manager._decompress_backup = mock.Mock()
            manager._binlog_replay_files = mock.Mock()
            manager._prepare_backup = mock.Mock(side_effect=lambda plan: (Path(plan['full']) / 'data').write_bytes(b'prepared data'))
            manager._restore_datadir = mock.Mock()
            manager._apply_binlog = mock.Mock()
            manager._update_restore_plan = mock.Mock()
            manager.restore(date='2026-09-11', binlog_start_time='2099-01-01 00:00:00', confirm_destructive=True)
            self.assertEqual(original.read_bytes(), b'raw data')
            working = Path(manager._restore_datadir.call_args.args[0]['full'])
            self.assertNotEqual(working, full)
            self.assertFalse(working.exists())

    def test_restore_plan_rejects_path_outside_backup_base(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(
                '{"date":"2026-09-11","status":"READY",'
                '"full":"/tmp/outside-backup","incr":[]}'
            )
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_restore_plan_rejects_missing_incremental_directory(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            missing = mgr.config.backup_base / "incr/missing"
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(json.dumps({
                "date": "2026-09-11", "status": "READY",
                "full": str(full), "incr": [str(missing)]
            }))
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_restore_plan_rejects_binlog_path_like_name(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            full = mgr.config.backup_base / "full/2026-09-11/120000/backup"
            full.mkdir(parents=True)
            plan_file = mgr.config.audit_base / "2026-09-11/restore.plan.json"
            plan_file.parent.mkdir(parents=True)
            plan_file.write_text(json.dumps({
                "date": "2026-09-11", "status": "READY",
                "full": str(full), "incr": [], "binlog": ["../secret"]
            }))
            with self.assertRaises(SystemExit):
                mgr._load_restore_plan("2026-09-11")

    def test_incremental_prepare_follows_official_apply_log_only_order(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = manager_for(td)
            calls = []
            mgr.run_cmd = lambda cmd, check=True: calls.append(cmd)
            mgr._prepare_backup({
                "full": "/backup/full",
                "incr": ["/backup/inc1", "/backup/inc2"],
            })
            self.assertIn("--apply-log-only", calls[0])
            self.assertIn("--apply-log-only", calls[1])
            self.assertNotIn("--apply-log-only", calls[2])
            self.assertEqual(calls[1][calls[1].index("--incremental-dir") + 1], "/backup/inc1")
            self.assertEqual(calls[2][calls[2].index("--incremental-dir") + 1], "/backup/inc2")


class XtraBackupStreamingTests(unittest.TestCase):
    """Prove visibility while the native child is still waiting, through the CLI."""

    def test_full_cli_exposes_native_output_before_child_exit(self):
        import selectors
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'bin'
            binary.mkdir()
            mysql = binary / 'mysql'
            mysql.write_text('#!' + sys.executable + '\nimport sys,json\n'
                             'print(json.dumps(dict(channels=0,running=0,group_members=0,datadir="/var/lib/mysql",'
                             'binlog="/var/lib/mysql/mysql-bin",index="/var/lib/mysql/mysql-bin.index")) '
                             'if any("JSON_OBJECT" in a for a in sys.argv) else "8.0.33")\n')
            mysql.chmod(0o755)
            xb = binary / 'xtrabackup'
            xb.write_text('#!' + sys.executable + '\nimport os,pathlib,sys,time\n'
                          'if "--help" in sys.argv: print("  --lock-ddl Boolean option"); sys.exit(0)\n'
                          'root=pathlib.Path(os.environ["STREAM_FIXTURE"])\n'
                          'print("Copying app/items.ibd: secret-with-&-$-chars", file=sys.stderr, flush=True)\n'
                          'print("[Warning] fixture warning secret-with-&-$-chars", file=sys.stderr, flush=True)\n'
                          '(root/"child-ready").touch()\n'
                          'deadline=time.monotonic()+12\n'
                          'while not (root/"release-child").exists() and time.monotonic()<deadline: time.sleep(.02)\n'
                          'target=pathlib.Path(next(a.split("=",1)[1] for a in sys.argv if a.startswith("--target-dir=")))\n'
                          'for name in ("xtrabackup_info","xtrabackup_checkpoints"): (target/name).write_text("fixture")\n'
                          'print("completed OK!",file=sys.stderr,flush=True)\n')
            xb.chmod(0o755)
            config = root / 'config.json'
            config.write_text(json.dumps({'backup_base': str(root / 'backup'),
                                          'mysql_password': 'secret-with-&-$-chars'}))
            config.chmod(0o600)
            env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                       MYSQLBACKUP_LOG_FILE=str(root / 'product.log'), STREAM_FIXTURE=directory)
            child = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tools/mysqltools/MyBackupCli.py'),
                                      'full', '-c', str(config)], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env)
            captured = b''
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 5
                    while b'Copying app/items.ibd' not in captured and time.monotonic() < deadline:
                        for key, _ in selector.select(.1):
                            block = os.read(key.fd, 65536)
                            if not block:
                                break
                            captured += block
                self.assertIn(b'Copying app/items.ibd', captured,
                              'Native progress was hidden while the XtraBackup child was running')
                self.assertIsNone(child.poll())
                self.assertTrue((root / 'child-ready').exists())
                self.assertIn('Copying app/items.ibd', (root / 'product.log').read_text())
            finally:
                (root / 'release-child').touch()
                output, _ = child.communicate(timeout=10)
                captured += output
            self.assertEqual(child.returncode, 0, captured.decode(errors='replace'))
            self.assertIn(b'completed OK!', captured)
            for text in (captured.decode(errors='replace'), (root / 'product.log').read_text()):
                self.assertNotIn('XtraBackup[stderr]', text)
                self.assertIn('XtraBackup 阶段=backup | Copying app/items.ibd: ***', text)
                self.assertIn('[WARNING] [mysqlbackup.manager] XtraBackup 阶段=backup | [Warning] fixture warning ***', text)
            self.assertNotIn(b'secret-with-&-$-chars', captured)
            self.assertNotIn('secret-with-&-$-chars', (root / 'product.log').read_text())
            self.assertTrue(list((root / 'backup').rglob('.backup_ok')))

    def native_run(self, code, check=True, password='secret-with-&-$-chars'):
        import io
        import sys
        manager = manager_for('/tmp')
        manager.config.mysql_password = password
        output = io.StringIO()
        manager.logger = logging.Logger('stream-contract')
        manager.logger.addHandler(logging.StreamHandler(output))
        command = [sys.executable, '-c', code, '--prepare']
        result = manager._stream_xtrabackup(command, check)
        return result, output.getvalue()

    def test_both_pipes_large_output_and_split_utf8_are_drained(self):
        result, output = self.native_run(
            'import os,sys\n'
            'for i in range(2000):\n'
            ' print("out-%s secret-with-&-$-chars"%i,flush=True)\n'
            ' print("err-%s secret-with-&-$-chars"%i,file=sys.stderr,flush=True)\n'
            'os.write(2,b"x"*65530+b"secret-with-&-$-chars"+b"y"*100000)\n'
            'os.write(2,b"\\nUTF8: \\xe7");os.write(2,b"\\x89\\xa9\\xe7\\x90\\x86\\xff\\n")\n')
        self.assertEqual(result, '', 'Native diagnostics must not be accumulated as return data')
        for marker in ('out-0 ', 'out-1999 ', 'err-0 ', 'err-1999 ', '物理', 'rc=0'):
            self.assertIn(marker, output)
        self.assertNotIn('secret-with-&-$-chars', output)
        self.assertNotIn('secret-with-', output)

    def test_silent_child_reports_liveness_without_claiming_progress(self):
        with mock.patch.object(MOD, 'XTRABACKUP_HEARTBEAT_SECONDS', .1):
            _, output = self.native_run('import time; time.sleep(1.3)')
        self.assertIn('still running: phase=prepare pid=', output)
        self.assertIn('no native log line for', output)
        self.assertIn('finished: phase=prepare rc=0', output)

    def test_failure_preserves_tail_exit_status_and_redaction(self):
        code = 'import sys;sys.stderr.write("failed secret-with-&-$-chars tail-without-newline");sys.exit(23)'
        _, output = self.native_run(code, check=False)
        self.assertIn('failed *** tail-without-newline', output)
        self.assertIn('rc=23', output)
        self.assertNotIn('secret-with-&-$-chars', output)
        with self.assertRaisesRegex(RuntimeError, 'COMMAND FAILED rc=23'):
            self.native_run(code)

    def test_multiline_password_is_redacted_on_both_native_lines(self):
        _, output = self.native_run('print("first-private-line\\nsecond-private-line")',
                                    password='first-private-line\nsecond-private-line')
        self.assertNotIn('first-private-line', output)
        self.assertNotIn('second-private-line', output)

    def test_interrupt_reaps_native_child_before_returning(self):
        import subprocess
        import sys
        manager = manager_for('/tmp')
        manager.config.mysql_password = 'secret'
        manager.logger = mock.Mock()
        real_popen = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(MOD.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(KeyboardInterrupt):
                manager._stream_xtrabackup([sys.executable, '-c',
                    'import os,signal,time;os.kill(os.getppid(),signal.SIGINT);time.sleep(60)'], True)
        self.assertIsNotNone(children[0].poll())


class CommandObservabilityTests(unittest.TestCase):
    def test_native_severity_and_source_do_not_depend_on_output_channel(self):
        import sys
        cases = (
            ('2026-11-26T22:22:07.549265+08:00 1 [Note] [MY-011825] [Xtrabackup] log scanned up to (123)', logging.INFO),
            ('2026-11-26T22:22:07Z 0 [ERROR] [MY-011825] cannot my_setwd /missing/', logging.ERROR),
            ('mysql: [Warning] Using a password on the command line interface can be insecure.', logging.WARNING),
            ('ERROR 2002 (HY000): connection refused', logging.ERROR),
            ('FATAL ERROR: COMMAND FAILED', logging.ERROR),
            ('[Warning] original warning', logging.WARNING),
            ('261126 22:22:07 [Warning] original warning', logging.WARNING),
            ('[FATAL] explicit native error', logging.ERROR),
            ('Copying app/ERROR_records.ibd', logging.INFO),
            ('Copying app/[ERROR].ibd', logging.INFO),
            ('unclassified diagnostic', logging.INFO),
        )
        for channel in ('stdout', 'stderr'):
            for line, level in cases:
                with self.subTest(channel=channel, line=line):
                    logger = logging.Logger('native-severity')
                    with self.assertLogs(logger, level='INFO') as recorded:
                        MOD._run_external(
                            [sys.executable, '-c', 'import sys; print(sys.argv[1], file=sys.' + channel + ')', line],
                            logger, show_stdout=True, label='XtraBackup', phase='prepare')
                    diagnostic = [r for r in recorded.records
                                  if line in r.getMessage() and not r.getMessage().startswith('RUN:')]
                    self.assertEqual(len(diagnostic), 1)
                    self.assertEqual(diagnostic[0].levelno, level)
                    self.assertEqual(diagnostic[0].getMessage(), 'XtraBackup 阶段=prepare | ' + line)
                    self.assertEqual(diagnostic[0].native_stream, channel)

    def test_native_error_line_does_not_override_exit_status(self):
        import sys
        manager, output = self.manager()
        result = MOD._run_external([sys.executable, '-c', 'print("[ERROR] diagnostic with zero exit")'],
                                   manager.logger, show_stdout=True)
        self.assertEqual(result.returncode, 0)
        with self.assertRaisesRegex(RuntimeError, 'COMMAND FAILED rc=23'):
            MOD._run_external([sys.executable, '-c', 'import sys;print("completed OK!");sys.exit(23)'],
                              manager.logger, show_stdout=True)

    def test_unwritable_log_falls_back_to_one_console_copy(self):
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch('sys.stdout', stdout), mock.patch('sys.stderr', stderr):
                logger = MOD.setup_logger('mysqlbackup-fallback-contract', log_file=directory)
                try:
                    logger.info('one fallback diagnostic', extra={'to_stdout': True})
                finally:
                    for handler in logger.handlers[:]:
                        logger.removeHandler(handler)
                        handler.close()
        self.assertEqual((stdout.getvalue() + stderr.getvalue()).count('one fallback diagnostic'), 1)

    def test_logged_command_masks_sql_and_password(self):
        result = MOD._command_description(['mysql', '--password=fixture-secret', '-e',
                                           "SELECT 'PRIVATE_SQL_DATA';"], 'fixture-secret')
        self.assertIn('--password=*** -e <SQL>', result)
        self.assertNotIn('PRIVATE_SQL_DATA', result)
        self.assertNotIn('fixture-secret', result)

    def manager(self):
        import io
        manager = manager_for('/tmp')
        manager.config.mysql_password = 'fixture-secret'
        output = io.StringIO()
        manager.logger = logging.Logger('command-observability')
        handler = logging.StreamHandler(output)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        manager.logger.addHandler(handler)
        return manager, output

    def test_metadata_stdout_is_returned_unchanged_and_never_logged(self):
        import sys
        manager, output = self.manager()
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'metadata.py'
            script.write_text('import sys;print("PRIVATE_SQL_DATA");print("warning fixture-secret",file=sys.stderr)')
            result = MOD._run_external([sys.executable, str(script)], manager.logger,
                                       manager.config.mysql_password, quiet=True)
        self.assertEqual(result.stdout, 'PRIVATE_SQL_DATA\n')
        self.assertIn('warning ***', output.getvalue())
        self.assertNotIn('PRIVATE_SQL_DATA', output.getvalue())
        self.assertNotIn('fixture-secret', output.getvalue())

    def test_all_non_metadata_commands_stream_both_pipes_and_silent_commands_heartbeat(self):
        import sys
        manager, output = self.manager()
        manager.run_cmd([sys.executable, '-c',
                         'import os;os.write(1,b"stdout fixture-secret\\n"*6000);'
                         'os.write(2,b"stderr fixture-secret\\n"*6000+b"tail")'])
        self.assertIn('stdout ***', output.getvalue())
        self.assertIn('stderr ***', output.getvalue())
        self.assertIn('tail', output.getvalue())
        self.assertNotIn('fixture-secret', output.getvalue())
        with mock.patch.object(MOD, 'OPERATION_HEARTBEAT_SECONDS', .1):
            manager.run_cmd([sys.executable, '-c', 'import time;time.sleep(.35)'])
        self.assertIn('still running: phase=command pid=', output.getvalue())

    def test_timeout_and_interrupt_reap_external_client(self):
        import subprocess
        import sys
        manager, output = self.manager()
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs); children.append(child); return child
        with mock.patch.object(MOD.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                MOD._run_external([sys.executable, '-c', 'import time;time.sleep(60)'],
                                  manager.logger, timeout=.1)
            with self.assertRaises(KeyboardInterrupt):
                manager.run_cmd([sys.executable, '-c',
                                 'import os,signal,time;os.kill(os.getppid(),signal.SIGINT);time.sleep(60)'])
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_filesystem_phase_heartbeat_and_failure_never_report_completion(self):
        import time
        import threading
        manager, output = self.manager()
        with mock.patch.object(MOD, 'OPERATION_HEARTBEAT_SECONDS', .03):
            with self.assertRaisesRegex(OSError, 'copy failed'):
                with MOD._operation_progress(manager.logger, 'archive fixture-binlog'):
                    time.sleep(.09)
                    raise OSError('copy failed')
        text = output.getvalue()
        self.assertIn('STAGE still running: archive fixture-binlog', text)
        self.assertIn('[ERROR] STAGE interrupted or failed:', text)
        self.assertNotIn('STAGE completed:', text)
        self.assertFalse(any(t.name == 'mysqlbackup-progress' for t in threading.enumerate()))

    def test_pipeline_drains_both_errors_stops_on_either_failure_and_reaps_on_interrupt(self):
        import signal
        import subprocess
        import sys
        import time
        driver = '''import importlib.util,logging,os,sys
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('backup',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
manager=m.BackupManager(SimpleNamespace(mysql_user='fixture',mysql_password='fixture-secret',mysql_socket='/fixture',native_options={}))
manager._binlog_replay_files=lambda plan:(123,[Path('/fixture/mysql-bin.000001')])
m.OPERATION_HEARTBEAT_SECONDS=.1
manager._apply_binlog({},'2099-01-01 00:00:00',None)
'''
        for case in ('large', 'producer-failure', 'consumer-failure', 'consumer-missing', 'interrupt', 'silent'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                producer = ('import os,pathlib,sys,time,signal\n'
                            'root=pathlib.Path(os.environ["PIPE_FIXTURE"])\n'
                            '(root/"producer.pid").write_text(str(os.getpid()))\n')
                consumer = ('import os,pathlib,sys,time\n'
                            'root=pathlib.Path(os.environ["PIPE_FIXTURE"])\n'
                            '(root/"consumer.pid").write_text(str(os.getpid()))\n')
                if case == 'large':
                    producer += 'os.write(2,b"producer fixture-secret\\n"*12000);os.write(1,b"PRIVATE_SQL_DATA\\n"*12000)\n'
                    consumer += 'os.write(2,b"consumer fixture-secret\\n"*12000);data=sys.stdin.buffer.read();print("PRIVATE_SQL_RESULT");assert data.startswith(b"PRIVATE_SQL_DATA")\n'
                elif case == 'producer-failure':
                    producer += 'sys.stderr.write("decoder tail fixture-secret");sys.exit(23)\n'
                    consumer += 'sys.stdin.read();time.sleep(60)\n'
                elif case == 'consumer-failure':
                    producer += 'while True: os.write(1,b"sql"*10000)\n'
                    consumer += 'sys.stderr.write("consumer tail fixture-secret");sys.exit(17)\n'
                elif case == 'interrupt':
                    producer += 'while not (root/"consumer.pid").exists(): time.sleep(.01)\nos.kill(os.getppid(),signal.SIGINT);time.sleep(60)\n'
                    consumer += 'sys.stdin.read();time.sleep(60)\n'
                elif case == 'silent':
                    producer += 'time.sleep(.5);print("PRIVATE_SQL_DATA")\n'
                    consumer += 'sys.stdin.read()\n'
                else:
                    producer += 'time.sleep(60)\n'
                for name, code in (('mysqlbinlog', producer), ('mysql', consumer)):
                    if name == 'mysql' and case == 'consumer-missing':
                        continue
                    path = root / name; path.write_text('#!' + sys.executable + '\n' + code); path.chmod(0o755)
                env = dict(os.environ, PATH=str(root), PIPE_FIXTURE=directory,
                           MYSQLBACKUP_LOG_FILE=str(root / 'product.log'))
                with subprocess.Popen([sys.executable, '-B', '-c', driver,
                                       str(ROOT / 'tools/mysqltools/MyBackupCli.py')],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      start_new_session=True) as child:
                    try:
                        data = child.communicate(timeout=5)[0].decode(errors='replace')
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.communicate()
                        self.fail('Pipeline blocked: ' + case)
                self.assertEqual(child.returncode == 0, case in ('large', 'silent'), data[-1500:])
                for path in root.glob('*.pid'):
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(path.read_text()), 0)
                for text in (data, (root / 'product.log').read_text()):
                    self.assertNotIn('fixture-secret', text)
                    self.assertNotIn('PRIVATE_SQL_DATA', text)
                    self.assertNotIn('PRIVATE_SQL_RESULT', text)
                if case == 'large':
                    self.assertIn('producer ***', data); self.assertIn('consumer ***', data)
                    self.assertIn('mysqlbinlog 阶段=replay | producer ***', data)
                    self.assertIn('mysql 阶段=replay | consumer ***', data)
                    self.assertNotIn('Binlog[', data)
                    self.assertIn('mysqlbinlog_rc=0 mysql_rc=0', data)
                elif case == 'producer-failure':
                    self.assertIn('decoder tail ***', data); self.assertIn('mysqlbinlog_rc=23', data)
                    self.assertIn('[ERROR] [mysqlbackup.manager] BINLOG REPLAY finished:', data)
                elif case == 'consumer-failure':
                    self.assertIn('consumer tail ***', data); self.assertIn('mysql_rc=17', data)
                    self.assertIn('[ERROR] [mysqlbackup.manager] BINLOG REPLAY finished:', data)
                elif case == 'silent':
                    self.assertIn('Binlog still running: phase=replay', data)

    def test_configuration_mysql_diagnostic_is_visible_before_exit_without_sql_data(self):
        import selectors
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'bin'; binary.mkdir()
            mysql = binary / 'mysql'
            mysql.write_text('#!' + sys.executable + '\nimport os,pathlib,sys,time\n'
                             'root=pathlib.Path(os.environ["COMMAND_FIXTURE"])\n'
                             'print("connection diagnostic fixture-secret",file=sys.stderr,flush=True)\n'
                             '(root/"ready").touch()\n'
                             'while not (root/"release").exists(): time.sleep(.02)\n'
                             'print("8.0.33 PRIVATE_SQL_RESULT")\n')
            mysql.chmod(0o755)
            config = root / 'backup.json'
            config.write_text(json.dumps({'backup_base': str(root / 'backup'),
                                          'mysql_password': 'fixture-secret'}))
            config.chmod(0o600)
            env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                       MYSQLBACKUP_LOG_FILE=str(root / 'product.log'), COMMAND_FIXTURE=directory)
            with subprocess.Popen([sys.executable, '-B', str(ROOT / 'tools/mysqltools/MyBackupCli.py'),
                                   'purge', '-c', str(config)], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env=env) as child:
                captured = b''
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(child.stdout, selectors.EVENT_READ)
                        deadline = time.monotonic() + 2
                        while b'connection diagnostic' not in captured and time.monotonic() < deadline:
                            for key, _ in selector.select(.05):
                                block = os.read(key.fd, 65536)
                                if not block:
                                    break
                                captured += block
                    self.assertIn(b'connection diagnostic ***', captured,
                                  'Configuration mysql diagnostics hidden until child exit')
                    self.assertIsNone(child.poll())
                    self.assertIn('connection diagnostic ***', (root / 'product.log').read_text())
                finally:
                    (root / 'release').touch()
                    captured += child.communicate(timeout=5)[0]
                self.assertEqual(child.returncode, 0, captured.decode(errors='replace'))
                self.assertNotIn(b'fixture-secret', captured)
                self.assertNotIn(b'PRIVATE_SQL_RESULT', captured)
                self.assertNotIn('PRIVATE_SQL_RESULT', (root / 'product.log').read_text())


class NativeAndCapabilityTests(unittest.TestCase):
    def test_capability_uses_actual_help_and_not_mysql_version(self):
        manager = manager_for('/tmp')
        manager.config.mysql_version = (8, 0)
        for help_text, expected in (
            ('  --lock-ddl Issue LOCK INSTANCE FOR BACKUP\n  --lock-ddl-timeout=# timeout\n', 'ON'),
            ('  --lock-ddl[=name] Possible options:\n ON OFF REDUCED\n  --lock-ddl-timeout=# timeout\n', 'REDUCED'),
        ):
            manager.config.xtrabackup_lock_ddl = 'AUTO'
            proc = mock.Mock(returncode=0, stdout=help_text, stderr='')
            with mock.patch.object(MOD, '_run_external', return_value=proc):
                self.assertEqual(manager.backup_lock_ddl(), expected)
        manager.config.xtrabackup_lock_ddl = 'REDUCED'
        with mock.patch.object(MOD, '_run_external', return_value=mock.Mock(returncode=0, stdout='--lock-ddl boolean', stderr='')):
            with self.assertRaisesRegex(RuntimeError, 'does not advertise'):
                manager.backup_lock_ddl()

    def test_success_stderr_is_logged_and_secret_is_redacted(self):
        import io
        import sys
        manager = manager_for('/tmp')
        manager.config.mysql_password = 's3cret'
        output = io.StringIO()
        manager.logger = logging.Logger('successful-external')
        manager.logger.addHandler(logging.StreamHandler(output))
        manager.run_cmd([sys.executable, '-c', 'import sys;print("warning s3cret ignored",file=sys.stderr)'])
        self.assertIn('warning *** ignored', output.getvalue())
        self.assertNotIn('s3cret', output.getvalue())

    def test_native_args_kwargs_repeat_and_target_isolation(self):
        self.assertEqual(MOD.native_arguments('mysql', '--ssl-mode=VERIFY_IDENTITY', connect_timeout=15),
                         ['--ssl-mode=VERIFY_IDENTITY', '--connect-timeout=15'])
        self.assertEqual(MOD.native_arguments('xtrabackup-backup', throttle=20, debug_sync=['a', 'b']),
                         ['--throttle=20', '--debug-sync=a', '--debug-sync=b'])
        manager = manager_for('/tmp')
        manager.config.mysql_password = 'secret'
        manager.config.native_options = {'xtrabackup-backup': ['--throttle=20'], 'xtrabackup-prepare': ['--use-memory=128M']}
        with mock.patch.object(manager, '_stream_xtrabackup', return_value='') as run:
            manager.run_cmd(['xtrabackup', '--prepare'])
        command = run.call_args.args[0]
        self.assertIn('--use-memory=128M', command)
        self.assertNotIn('--throttle=20', command)

    def test_native_protection_includes_mysql_parser_aliases(self):
        for token in ('--target-dir=/tmp', '--target=/tmp', '--target_dir=/tmp', '--loose-skip-target-dir=/tmp',
                      '--lock-ddl=OFF', '--disable-lock-ddl', '--password=secret', '-psecret', '--stream=xbstream',
                      '--no-server-version-check', '--defaults-extra-file=/tmp/x', '--incremental-lsn=0', '--force', 'table'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                MOD.native_arguments('xtrabackup-backup', token)

    def test_version_detection_failure_does_not_assume_compatibility(self):
        config = MOD.Config.__new__(MOD.Config)
        config.logger = mock.Mock()
        with mock.patch.object(config, '_mysql_cmd', side_effect=RuntimeError('connection refused')):
            with self.assertRaises(SystemExit):
                config._detect_mysql_version()

    def test_real_cli_legacy_boolean_selection_and_native_option(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); binary=root/'bin'; binary.mkdir()
            mysql=binary/'mysql'
            mysql.write_text('#!'+sys.executable+'\nimport sys,json\n'
                             'print(json.dumps(dict(channels=0,running=0,group_members=0,datadir="/var/lib/mysql",'
                             'binlog="/var/lib/mysql/mysql-bin",index="/var/lib/mysql/mysql-bin.index")) '
                             'if any("JSON_OBJECT" in a for a in sys.argv) else "8.0.33")\n')
            mysql.chmod(0o755)
            xb=binary/'xtrabackup'
            xb.write_text('#!'+sys.executable+'\nimport json,os,pathlib,sys\n'
                          'if "--help" in sys.argv: print("  --lock-ddl Boolean option\\n  --lock-ddl-timeout=# Timeout"); sys.exit(0)\n'
                          'pathlib.Path(os.environ["ARGV_RECORD"]).write_text(json.dumps(sys.argv[1:]))\n'
                          'target=pathlib.Path(next(x.split("=",1)[1] for x in sys.argv if x.startswith("--target-dir=")))\n'
                          'for name in ("xtrabackup_checkpoints","xtrabackup_info"): (target/name).write_text("fixture")\n'
                          'print("warning: observable-success-warning",file=sys.stderr)\n')
            xb.chmod(0o755)
            config=root/'config.json'; config.write_text(json.dumps({'backup_base':str(root/'backup'),'mysql_password':'secret'})); config.chmod(0o600)
            env=dict(os.environ, PATH=str(binary)+':'+os.environ['PATH'], ARGV_RECORD=str(root/'argv.json'), MYSQLBACKUP_LOG_FILE=str(root/'product.log'))
            script=ROOT/'tools/mysqltools/MyBackupCli.py'
            result=subprocess.run([sys.executable,str(script),'full','-c',str(config),'--native-option','xtrabackup-backup=--throttle=20'],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            command=json.loads((root/'argv.json').read_text())
            self.assertIn('--lock-ddl=ON',command)
            self.assertNotIn('--lock-ddl=REDUCED',command)
            self.assertIn('--throttle=20',command)
            self.assertIn('observable-success-warning',(root/'product.log').read_text())

    def test_usage_exit_one_is_supported_only_with_real_option_section(self):
        manager = manager_for('/tmp')
        manager.config.xtrabackup_lock_ddl = 'AUTO'
        manager.config.mysql_password = 'secret'
        for returncode, text, expected in (
            (1, 'Usage: xtrabackup --backup [OPTIONS]\n  --lock-ddl[=name] ON OFF REDUCED\n', 'REDUCED'),
            (1, '  --lock-ddl[=name] ON OFF REDUCED\n', None),
            (2, 'Usage: xtrabackup\n  --lock-ddl bool\n', None),
        ):
            with mock.patch.object(MOD, '_run_external', return_value=mock.Mock(returncode=returncode, stdout=text, stderr='')):
                if expected:
                    self.assertEqual(manager.backup_lock_ddl(), expected)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Unable to inspect'):
                        manager.backup_lock_ddl()


if __name__ == "__main__":
    unittest.main()
