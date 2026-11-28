"""CLI handoff contracts complement the owning storage and live tests."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location('parameter_'+name, ROOT/'tools/mysqltools'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MyBackupParameterRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load('MyBackupCli')

    def cli(self, *args):
        module = self.module
        with mock.patch.object(sys, 'argv', ['MyBackupCli', *args]), \
                mock.patch.object(module, 'Config') as config, \
                mock.patch.object(module, 'BackupManager') as manager, \
                contextlib.redirect_stdout(io.StringIO()):
            manager.return_value.acquire_lock.return_value = None
            manager.return_value.fail.side_effect = RuntimeError
            module.main()
            return config, manager.return_value

    def test_commands_defaults_and_config_aliases_reach_owner(self):
        for command, method in (('full','full_backup'), ('incr','incr_backup'),
                                ('binlog','binlog_backup'), ('checksum','checksum'),
                                ('restore-dry-run','restore_audit'), ('purge','purge')):
            with self.subTest(command=command):
                config, manager = self.cli(command, '-c', '/fixture/backup.conf')
                config.assert_called_once_with('/fixture/backup.conf', native_options={}, offline=False)
                getattr(manager, method).assert_called_once()
                if command == 'purge':
                    manager.purge.assert_called_once_with(15,30)
                elif command in ('checksum','restore-dry-run'):
                    getattr(manager, method).assert_called_once_with(None)
        config, _ = self.cli('full','--config','/fixture/long.conf')
        self.assertEqual(config.call_args.args, ('/fixture/long.conf',))

    def test_date_pitr_confirmations_and_offline_are_forwarded_exactly(self):
        for command, method in (('checksum','checksum'), ('restore-dry-run','restore_audit')):
            config, manager = self.cli(command,'--date','2026-01-02','--offline-restore')
            self.assertTrue(config.call_args.kwargs['offline'])
            getattr(manager,method).assert_called_once_with('2026-01-02')
        config, manager = self.cli('restore','--date','2026-01-02',
                                  '--binlog-start-time','2026-01-02 01:02:03',
                                  '--binlog-stop-time','2026-01-02 02:03:04',
                                  '--confirm-destructive-restore','--confirm-replication-isolated',
                                  '--offline-restore')
        self.assertTrue(config.call_args.kwargs['offline'])
        manager.restore.assert_called_once_with('2026-01-02','2026-01-02 01:02:03','2026-01-02 02:03:04',
                                                confirm_destructive=True, confirm_replication_isolated=True)
        _, manager = self.cli('restore','--binlog-start-time','2026-01-02 01:02:03')
        manager.restore.assert_called_once_with(None,'2026-01-02 01:02:03',None,
                                                confirm_destructive=False,confirm_replication_isolated=False)

    def test_retention_and_phase_native_options_are_consumed(self):
        _, manager = self.cli('purge','--backup-days','7','--binlog-days','45')
        manager.purge.assert_called_once_with(7,45)
        config, manager = self.cli('full','--native-option','xtrabackup-backup=--throttle=10',
                                  '--native-option','xtrabackup-prepare=--use-memory=128M')
        self.assertEqual(config.call_args.kwargs['native_options'],
                         {'xtrabackup-backup':['--throttle=10'],
                          'xtrabackup-prepare':['--use-memory=128M']})
        manager.full_backup.assert_called_once()

    def test_bad_commands_and_offline_scope_refuse_before_config(self):
        for args in (['bad-command'],['full','--offline-restore'],['incr','--offline-restore'],
                     ['purge','--backup-days','bad'],['full','--native-option','mysql=--force']):
            with self.subTest(args=args), mock.patch.object(self.module, 'Config') as config, \
                    mock.patch.object(sys,'argv',['MyBackupCli',*args]), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exit_state:
                    self.module.main()
                self.assertNotEqual(exit_state.exception.code,0)
                config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
