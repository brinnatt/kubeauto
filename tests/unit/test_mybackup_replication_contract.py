"""Replication restores must refuse unsafe startup before touching data/services."""
import json
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.test_mybackup_cli_contract import MOD, manager_for


class ReplicationContracts(unittest.TestCase):
    def manager(self, directory, topology='primary-replica', **state):
        manager = manager_for(directory)
        del manager._replication_backup_options
        del manager._replication_restore_check
        del manager._replication_state
        manager.config.mysql_topology = topology
        self.state = dict(channels=1, running=2, skip_start='ON', log_updates='ON',
                          group_members=0, uuid='source-uuid', gtid='ON',
                          datadir=str(manager.config.mysql_datadir),
                          binlog=str(manager.config.mysql_binlog_dir / manager.config.mysql_binlog_prefix),
                          index=str(manager.config.mysql_binlog_index))
        self.state.update(state)
        manager.mysql_cmd = mock.Mock(side_effect=lambda *a, **k: json.dumps(self.state))
        return manager

    def test_replica_backup_records_coordinates_and_stops_sql_thread_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            self.assertEqual(manager._replication_backup_options(), ['--slave-info', '--safe-slave-backup'])
            self.assertIn('replication_connection_configuration', manager.mysql_cmd.call_args.args[0])

    def test_primary_and_standalone_do_not_receive_replica_options(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('standalone', 'primary-replica'):
                manager = self.manager(directory, mode, channels=0, running=0)
                self.assertEqual(manager._replication_backup_options(), [])

    def test_local_namespace_mismatch_refuses_even_when_sql_connects(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory, datadir='/wrong-instance')
            with self.assertRaisesRegex(RuntimeError, 'local paths'):
                manager._replication_backup_options()

    def test_dual_primary_cannot_be_signed_as_a_replica_without_a_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory, 'dual-primary', channels=0, running=0)
            with self.assertRaisesRegex(RuntimeError, 'single configured'):
                manager._replication_backup_options()

    def test_restored_read_only_authority_can_start_new_full_before_rejoin(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory, 'dual-primary', channels=0, running=0, super_read_only=1)
            manager.ensure_dirs()
            marker=manager.config.backup_base/'lock/restore-needs-full'
            marker.write_text('new full required after restore\n')
            self.assertEqual(manager._replication_backup_options(), [])
            self.state['super_read_only']=0
            with self.assertRaisesRegex(RuntimeError, 'single configured'):
                manager._replication_backup_options()

    def test_missing_upstream_coordinates_cannot_publish_a_successful_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager._replication_backup_options()
            with self.assertRaisesRegex(RuntimeError, 'upstream replication coordinates'):
                manager._record_replication_backup(Path(directory))
            (Path(directory)/'xtrabackup_slave_info').write_text('CHANGE REPLICATION SOURCE TO ...;')
            manager._record_replication_backup(Path(directory))
            saved = json.loads((Path(directory)/'.mysql_topology.json').read_text())
            self.assertEqual(saved['source']['uuid'], 'source-uuid')

    def test_native_options_cannot_disable_automatic_replication_protection(self):
        for option in ('--safe-slave-backup=OFF', '--skip-safe-slave-backup', '--slave-info=OFF', '--galera-info'):
            with self.subTest(option=option), self.assertRaises(ValueError):
                MOD.native_arguments('xtrabackup-backup', option)

    def test_copyback_keeps_target_uuid_instead_of_donor_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            data = manager.config.mysql_datadir; data.mkdir()
            (data/'auto.cnf').write_text('[auto]\nserver-uuid=target-uuid\n')
            def command(argv, **kwargs):
                if '--copy-back' in argv:
                    (data/'auto.cnf').write_text('[auto]\nserver-uuid=donor-uuid\n')
            manager.run_cmd = command
            manager._restore_datadir({'full': directory+'/full'})
            self.assertIn('target-uuid', (data/'auto.cnf').read_text())

    def test_systemd_active_is_not_sql_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory, 'standalone', channels=0, running=0)
            manager.config.mysql_service = 'fixture-mysql.service'
            manager.run_cmd = mock.Mock(return_value='active')
            manager.mysql_cmd = mock.Mock(side_effect=[RuntimeError('not ready'), '1'])
            with mock.patch.object(MOD.time, 'sleep'):
                self.assertTrue(manager.manage_mysql_service('start'))
            self.assertEqual(manager.mysql_cmd.call_count, 2)

    def test_systemd_active_at_timeout_cannot_hide_sql_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory, 'standalone', channels=0, running=0)
            manager.config.mysql_service = 'fixture-mysql.service'
            manager.run_cmd = mock.Mock(return_value='active')
            manager.mysql_cmd = mock.Mock(side_effect=RuntimeError('not ready'))
            with mock.patch.object(MOD.time, 'monotonic', side_effect=[0, 31]):
                self.assertFalse(manager.manage_mysql_service('start', timeout=30))

    def test_pitr_failure_restores_super_read_only_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.ensure_dirs()
            plan = dict(full=directory+'/full', incr=[], binlog=[])
            manager._load_restore_plan = mock.Mock(return_value=(plan, Path(directory)/'plan'))
            manager.restore_workspace = mock.Mock(return_value=contextlib.nullcontext(plan))
            manager.run_cmd = mock.Mock()
            manager._replication_restore_check = mock.Mock()
            events = []
            for name in ('_decompress_backup', '_binlog_replay_files', '_prepare_backup', 'binlog_backup'):
                setattr(manager, name, mock.Mock())
            def service(action, **kwargs):
                events.append(action)
                return True
            manager.manage_mysql_service = service
            manager._restore_datadir = lambda p: events.append('copy-back')
            def query(sql, **kwargs):
                events.append(sql)
                if sql == 'SELECT 1;':
                    raise RuntimeError('server stopped')
            manager.mysql_cmd = query
            def apply(*args):
                events.append('pitr')
                raise RuntimeError('injected PITR failure')
            manager._apply_binlog = apply
            with self.assertRaisesRegex(RuntimeError, 'injected PITR failure'):
                manager.restore(date='2026-11-25', confirm_destructive=True,
                                confirm_replication_isolated=True)
            self.assertLess(events.index('stop'), events.index('copy-back'))
            self.assertLess(events.index('copy-back'), events.index('start'))
            self.assertLess(events.index('start'), events.index('pitr'))
            self.assertGreater(events.index('SET GLOBAL super_read_only=ON;'), events.index('pitr'))
            self.assertTrue((manager.config.backup_base/'lock/restore-needs-full').exists())

    def test_wrong_topology_unhealthy_replication_and_unlogged_updates_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = [('standalone', {}, 'topology'), ('primary-replica', {'running': 1}, 'healthy'),
                     ('primary-replica', {'log_updates': 'OFF'}, 'log_replica_updates'),
                     ('primary-replica', {'channels': 2}, 'channel'),
                     ('primary-replica', {'group_members': 3}, 'Group Replication')]
            for mode, state, message in cases:
                with self.subTest(mode=mode, state=state):
                    manager = self.manager(directory, mode, **state)
                    with self.assertRaisesRegex(RuntimeError, message):
                        manager._replication_backup_options()

    def test_confirmation_and_persistent_skip_start_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            with self.assertRaisesRegex(RuntimeError, 'confirm-replication-isolated'):
                manager._replication_restore_check(False)
            self.state['skip_start'] = 'OFF'
            with self.assertRaisesRegex(RuntimeError, 'skip_replica_start'):
                manager._replication_restore_check(True)

    def test_startup_verification_refuses_running_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            with self.assertRaisesRegex(RuntimeError, 'replication threads'):
                manager._replication_restore_check(True, restarted=True)
            self.state['running'] = 0
            manager._replication_restore_check(True, restarted=True)

    def test_restore_refuses_before_service_or_workspace_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager._load_restore_plan = mock.Mock(return_value=({}, Path(directory)/'plan'))
            manager.run_cmd = mock.Mock()
            manager.manage_mysql_service = mock.Mock(side_effect=AssertionError('service touched'))
            manager.restore_workspace = mock.Mock(side_effect=AssertionError('workspace touched'))
            with self.assertRaisesRegex(RuntimeError, 'confirm-replication-isolated'):
                manager.restore(date='2026-11-25', confirm_destructive=True)

    def test_donor_restore_does_not_append_target_binlogs_to_donor_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=self.manager(directory); manager.ensure_dirs()
            del manager._donor_restore
            full=Path(directory)/'full'; full.mkdir()
            (full/'.mysql_topology.json').write_text(json.dumps({'source':{'uuid':'donor-uuid'}}))
            plan=dict(full=str(full),incr=[],binlog=[])
            manager._load_restore_plan=mock.Mock(return_value=(plan,Path(directory)/'plan'))
            manager.restore_workspace=lambda p:contextlib.nullcontext(p)
            manager._replication_restore_check=mock.Mock(return_value={'uuid':'target-uuid'})
            manager.manage_mysql_service=mock.Mock(return_value=True); manager.run_cmd=mock.Mock()
            def query(sql,**kwargs):
                if sql=='SELECT 1;': raise RuntimeError('stopped')
            manager.mysql_cmd=query
            for name in ('_decompress_backup','_binlog_replay_files','_prepare_backup','_restore_datadir','_apply_binlog','_update_restore_plan'):
                setattr(manager,name,mock.Mock())
            manager.binlog_backup=mock.Mock(side_effect=AssertionError('mixed histories'))
            manager.restore('2026-11-25',confirm_destructive=True,confirm_replication_isolated=True)
            manager.binlog_backup.assert_not_called(); manager._restore_datadir.assert_called_once()
            self.assertFalse(manager._donor_restore(full,{'uuid':'donor-uuid'}))
            self.assertTrue(manager._donor_restore(full,{'uuid':'target-uuid'}))
            (full/'.mysql_topology.json').unlink()
            with self.assertRaisesRegex(RuntimeError,'source identity'): manager._donor_restore(full,{'uuid':'target-uuid'})

    def test_unknown_mode_is_rejected_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'backup.json'
            config.write_text(json.dumps({'mysql_topology': '', 'mysql_password': 'fixture'}))
            config.chmod(0o600)
            with mock.patch.object(MOD.Config, '_detect_mysql_version', side_effect=AssertionError('database accessed')):
                with self.assertRaises(SystemExit):
                    MOD.Config(str(config))

    def test_json_comments_are_valid_data_and_do_not_change_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'backup.json'
            config.write_text(json.dumps({'_comments': {'mysql_topology': '说明'},
                                         'mysql_password': 'fixture', 'mysql_topology': 'dual-primary'}))
            config.chmod(0o600)
            with mock.patch.object(MOD.Config, '_detect_mysql_version', return_value=(8, 0)):
                loaded = MOD.Config(str(config))
            self.assertEqual(loaded.mysql_topology, 'dual-primary')


if __name__ == '__main__':
    unittest.main()
