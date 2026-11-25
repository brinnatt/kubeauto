"""Stopped native instances must be restorable without a guessed SQL version."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.unit.test_mybackup_cli_contract import MOD, manager_for


class OfflineRestoreContracts(unittest.TestCase):
    def config(self,directory,topology='primary-replica',**updates):
        root=Path(directory); defaults=root/'my.cnf'; defaults.write_text('[mysqld]\n')
        data=dict(backup_base=str(root/'backup'),mysql_datadir=str(root/'data'),mysql_binlog_dir=str(root/'logs'),
                  mysql_binlog_index=str(root/'logs/mysql-bin.index'),mysql_binlog_prefix='mysql-bin',
                  mysql_socket=str(root/'data/mysql.sock'),mysql_password='fixture',mysql_service='fixture.service',
                  mysql_defaults_file=str(defaults),mysql_topology=topology)
        data.update(updates); path=root/'backup.json'; path.write_text(json.dumps(data)); path.chmod(0o600)
        return path

    def commands(self,directory,**changes):
        root=Path(directory)
        options={'datadir':str(root/'data')+'/', 'socket':str(root/'data/mysql.sock'),
                 'log-bin':str(root/'logs/mysql-bin'),'log-bin-index':str(root/'logs/mysql-bin.index'),
                 'skip-replica-start':'TRUE','super-read-only':'TRUE','gtid-mode':'ON'}
        options.update(changes)
        def command(argv,**kwargs):
            if argv[:3]==['systemctl','show','--property=ExecStart']:
                stdout='{ path=/usr/sbin/mysqld ; argv[]=/usr/sbin/mysqld --defaults-file='+str(root/'my.cnf')+' ; ignore_errors=no ; pid=0 ; }\n'
                return mock.Mock(returncode=0,stdout=stdout)
            if argv[:2]==['systemctl','is-active']: return mock.Mock(returncode=3,stdout='')
            if argv==['/usr/sbin/mysqld','--version']: return mock.Mock(returncode=0,stdout='/usr/sbin/mysqld Ver 8.0.33 for Linux on x86_64')
            if argv[0]=='/usr/sbin/mysqld' and argv[-2:]==['--verbose','--help']:
                return mock.Mock(returncode=0,stdout='\n'.join(k+'   '+v for k,v in options.items()))
            raise AssertionError('unexpected external command: '+repr(argv))
        return command

    def test_offline_config_uses_real_unit_binary_and_never_queries_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.config(directory)
            with mock.patch.object(MOD.subprocess,'run',side_effect=self.commands(directory)), mock.patch.object(MOD.Config,'_mysql_cmd',side_effect=AssertionError('SQL accessed')):
                config=MOD.Config(str(path),offline=True)
            self.assertEqual(config.mysql_version,(8,0)); self.assertEqual(config.mysql_version_text,'8.0.33')
            self.assertEqual(config.offline_state['gtid'],'ON')

    def test_missing_defaults_container_and_path_mismatch_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            for updates in ({'mysql_defaults_file':''},{'mysql_container':'target-mysql'}):
                path=self.config(directory,**updates)
                with self.assertRaises(SystemExit): MOD.Config(str(path),offline=True)
            path=self.config(directory)
            with mock.patch.object(MOD.subprocess,'run',side_effect=self.commands(directory,datadir='/wrong')):
                with self.assertRaises(SystemExit): MOD.Config(str(path),offline=True)

    def test_replica_requires_both_persistent_startup_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.config(directory)
            for key in ('skip-replica-start','super-read-only'):
                with mock.patch.object(MOD.subprocess,'run',side_effect=self.commands(directory,**{key:'FALSE'})):
                    with self.assertRaises(SystemExit): MOD.Config(str(path),offline=True)

    def test_persisted_configuration_cannot_override_startup_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.config(directory); data=Path(directory)/'data'; data.mkdir()
            (data/'mysqld-auto.cnf').write_text(json.dumps({'Version':2,'mysql_dynamic_variables':{'super_read_only':{'Value':'OFF'}}}))
            with mock.patch.object(MOD.subprocess,'run',side_effect=self.commands(directory)):
                with self.assertRaises(SystemExit): MOD.Config(str(path),offline=True)

    def test_wrapper_or_active_target_is_refused_before_help(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.config(directory)
            for wrapper in (True,False):
                command=self.commands(directory)
                def execute(argv,**kwargs):
                    result=command(argv,**kwargs)
                    if wrapper and argv[:2]==['systemctl','show']: result.stdout=result.stdout.replace(' --defaults-file',' --extra --defaults-file')
                    if not wrapper and argv[:2]==['systemctl','is-active']: result.returncode=0
                    return result
                with mock.patch.object(MOD.subprocess,'run',side_effect=execute):
                    with self.assertRaises(SystemExit): MOD.Config(str(path),offline=True)

    def test_offline_metadata_checks_exact_patch_gtid_and_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=manager_for(directory); manager.config.offline=True
            manager.config.mysql_topology='primary-replica'; manager.config.mysql_version_text='8.0.33'; manager.config.offline_state={'gtid':'ON'}
            path=Path(directory)/'.mysql_topology.json'
            data={'topology':'primary-replica','source':{'version':'8.0.33','gtid':'ON'}}
            path.write_text(json.dumps(data)); manager._offline_backup_check(directory)
            for key,value in (('version','8.0.46'),('gtid','OFF')):
                changed=json.loads(json.dumps(data)); changed['source'][key]=value; path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(RuntimeError,'metadata'): manager._offline_backup_check(directory)

    def test_copyback_preserves_target_persisted_configuration_and_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=manager_for(directory); data=manager.config.mysql_datadir; data.mkdir()
            (data/'mysqld-auto.cnf').write_text('target-safe-configuration')
            def copy(argv,**kwargs):
                if '--copy-back' in argv: (data/'mysqld-auto.cnf').write_text('donor-unsafe-configuration')
            manager.run_cmd=copy; manager._restore_datadir({'full':directory+'/full'})
            self.assertEqual((data/'mysqld-auto.cnf').read_text(),'target-safe-configuration')
            (data/'mysqld-auto.cnf').unlink(); manager._restore_datadir({'full':directory+'/full'})
            self.assertFalse((data/'mysqld-auto.cnf').exists())

    def test_offline_restore_starts_stopped_target_after_copyback(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=manager_for(directory); manager.ensure_dirs(); manager.config.offline=True
            plan={'full':directory+'/full','incr':[],'binlog':[]}; events=[]
            manager._load_restore_plan=mock.Mock(return_value=(plan,Path(directory)/'plan'))
            manager._offline_backup_check=mock.Mock(); manager.restore_workspace=lambda p:contextlib.nullcontext(p)
            manager.manage_mysql_service=lambda action,**kwargs:events.append(action) or action!='status'
            manager.run_cmd=mock.Mock(); manager.mysql_cmd=mock.Mock(side_effect=RuntimeError('stopped'))
            for name in ('_decompress_backup','_binlog_replay_files','_prepare_backup','binlog_backup','_apply_binlog','_update_restore_plan'):
                setattr(manager,name,mock.Mock())
            manager._restore_datadir=lambda p:events.append('copyback')
            manager.restore('2026-11-25',confirm_destructive=True)
            self.assertEqual(events,['status','copyback','start'])


if __name__=='__main__': unittest.main()
