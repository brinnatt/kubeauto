"""Contracts for the native fixture before acquiring any remote lease."""
import importlib.util
import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('binary80_live',ROOT/'tests/helpers/mysql80-binary-live.py')
GATE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(GATE)


class NativeRunnerContracts(unittest.TestCase):
    def test_scenarios_cover_only_authorized_topologies_and_both_versions_gtid(self):
        self.assertEqual(len(GATE.scenarios('full')),12)
        self.assertEqual({s[2] for s in GATE.scenarios('full')},set(GATE.TOPOLOGIES))
        self.assertEqual(GATE.scenarios('probe'),[('8.0.33','OFF','standalone')])
        self.assertEqual(GATE.scenarios('replication-probe'),[('8.0.33','OFF','primary-replica'),('8.0.33','OFF','dual-primary')])
        for topology in GATE.TOPOLOGIES: self.assertEqual(len(GATE.scenarios(topology)),4)
        with self.assertRaises(ValueError): GATE.scenarios('galera')

    def test_unit_is_native_and_scoped_without_restart(self):
        text=GATE.unit_text(Path('/tmp/b80-0123456789ab'),Path('/tmp/b80-0123456789ab/mysql-8.0.33'),'a')
        self.assertIn('ExecStart=/tmp/b80-0123456789ab/mysql-8.0.33/usr/sbin/mysqld',text)
        self.assertIn('User=nobody',text); self.assertIn('Restart=no',text)
        self.assertNotIn('docker',text)
        with self.assertRaises(ValueError): GATE.unit_text(Path('/var/lib/mysql'),Path('/usr'),'a')

    def fixture(self,directory,topology='standalone'):
        worker=mock.Mock(); worker.base=Path(directory); worker.env={'PATH':'/usr/bin:/bin'}
        worker.runtime=Path(directory)/'runtime'; worker.plan={'id':'0123456789ab'}
        evidence=Path(directory)/'evidence'; evidence.mkdir()
        return GATE.Fixture(worker,Path(directory)/'prefix','8.0.33','OFF',topology,evidence)

    def test_backup_adapter_preserves_argv_and_paths_never_replaces_systemctl(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=self.fixture(directory)
            with mock.patch.object(GATE,'run'): fixture.adapters('hub.talkedu.cn/kubeauto/pxb@sha256:'+'a'*64)
            self.assertEqual({p.name for p in (Path(directory)/'bin').iterdir()},{'mysql','mysqlbinlog','xtrabackup'})
            text=(Path(directory)/'bin/xtrabackup').read_text()
            self.assertIn(directory+':'+directory,text); self.assertIn('"$@"',text)
            self.assertNotIn('/var/lib/mysql',text)
            for path in (Path(directory)/'bin').iterdir():
                subprocess.run(['sh','-n',str(path)],check=True)

    def test_unpacked_rpm_does_not_depend_on_system_mysql_files_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=self.fixture(directory)
            config=fixture.server_config('a',1)
            self.assertIn('secure-file-priv=NULL\n',config)
            self.assertIn('skip-replica-start=ON\n',config)
            self.assertIn('datadir='+directory+'/a/data\n',config)
            self.assertNotIn('/var/lib/mysql-files',config)
            self.assertIn('log-bin='+directory+'/a/logs/mysql-bin\n',config)
            self.assertIn('log-bin-index='+directory+'/a/logs/mysql-bin.index\n',config)

    def test_tool_failure_stops_caller_and_retains_first_output(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=self.fixture(directory)
            def execute(argv,**kwargs):
                kwargs['stdout'].write('first causal failure\n')
                return subprocess.CompletedProcess(argv,42)
            with mock.patch.object(GATE.subprocess,'run',side_effect=execute):
                with self.assertRaisesRegex(RuntimeError,'CLI_ERROR'):
                    fixture.tool('a','full')
            self.assertIn('first causal failure',next(fixture.evidence.glob('a-full-*.log')).read_text())

    def test_exact_refusal_is_required_not_merely_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=self.fixture(directory)
            with mock.patch.object(GATE.subprocess,'run',return_value=subprocess.CompletedProcess([],2)):
                with self.assertRaisesRegex(RuntimeError,'REFUSAL_CONTRACT'):
                    fixture.tool('a','restore',refuse='confirm-replication-isolated')

    def test_full_requires_current_prerequisites_before_remote_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); matrix=root/'tests/tools-test-matrix.yaml'; matrix.parent.mkdir()
            cases=[{'id':'TL-MBK-%02d'%n,'status':'pass'} for n in range(1,13)]
            cases.append({'id':'TL-X-06','status':'pass'})
            for bad in ('pending','fail','missing'):
                current=[dict(case) for case in cases]
                if bad=='missing': current=[case for case in current if case['id']!='TL-MBK-11']
                else: current[10]['status']=bad
                matrix.write_text(json.dumps({'test_cases':current}))
                with mock.patch.object(GATE,'ROOT',root), mock.patch.object(GATE,'run') as remote, mock.patch.object(GATE,'load') as dependency:
                    with self.assertRaisesRegex(ValueError,'PREREQUISITES_REFUSED TL-MBK-11'):
                        GATE.launch('full')
                    remote.assert_not_called(); dependency.assert_not_called()

    def test_full_real_launcher_rejects_stale_document_before_any_remote_action(self):
        physical=GATE.load('mysql80-physical-live')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); evidence=root/'tests/evidence'; evidence.mkdir(parents=True)
            cases=[{'id':'TL-MBK-%02d'%n,'status':'pass'} for n in range(1,13)]+[{'id':'TL-X-06','status':'pass'}]
            (root/'tests/tools-test-matrix.yaml').write_text(json.dumps({'test_cases':cases}))
            (root/'manual.md').write_text('changed after author review')
            (evidence/'mysql80-physical-documentation-20261125.json').write_text(json.dumps({
                'result':'author-reviewed','checks':['reviewed'],'files':{'manual.md':'a'*64}}))
            with mock.patch.object(GATE,'ROOT',root), mock.patch.object(physical,'ROOT',root), mock.patch.object(GATE,'load',return_value=physical), mock.patch.object(GATE,'run') as remote:
                with self.assertRaisesRegex(ValueError,'DOCUMENTATION_STALE'):
                    GATE.launch('full')
                remote.assert_not_called()

    def test_full_requires_each_owning_raw_record_after_matrix_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); matrix=root/'tests/tools-test-matrix.yaml'; matrix.parent.mkdir()
            cases=[{'id':'TL-MBK-%02d'%n,'status':'pass'} for n in range(1,13)]+[{'id':'TL-X-06','status':'pass'}]
            matrix.write_text(json.dumps({'test_cases':cases}))
            physical=mock.Mock(); audit=mock.Mock()
            def dependency(name): return physical if name=='mysql80-physical-live' else audit
            with mock.patch.object(GATE,'ROOT',root), mock.patch.object(GATE,'load',side_effect=dependency), mock.patch.object(GATE,'verify_record') as native:
                GATE.final_prerequisites()
                physical.documentation.assert_called_once()
                self.assertEqual([call.args[0].name for call in physical.verify_record.call_args_list],
                    ['mysql80-physical-8.0.33.json','mysql80-physical-8.0.46.json','mysql80-physical-full.json'])
                self.assertEqual([call.args[0] for call in audit.verify_record.call_args_list],
                    ['mysql84-physical-requal-20261125','mysql-backup-portability-20261125'])
                self.assertEqual(len(native.call_args_list),3)
                physical.verify_record.side_effect=ValueError('stale owning raw record')
                with mock.patch.object(GATE,'run') as remote:
                    with self.assertRaisesRegex(ValueError,'stale owning raw record'): GATE.launch('full')
                    remote.assert_not_called()

    def test_lsn_chain_does_not_traverse_latest_symlink_as_an_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=self.fixture(directory)
            root=Path(directory)/'a/backup'; full=root/'full/2026-11-25/100000/backup'
            one=root/'incr/2026-11-25/100010'; two=root/'incr/2026-11-25/100020'
            for path,first,last in ((full,0,10),(one,10,20),(two,20,30)):
                path.mkdir(parents=True); (path/'binary80').mkdir()
                (path/'xtrabackup_checkpoints').write_text('from_lsn = '+str(first)+'\nto_lsn = '+str(last)+'\n')
            (root/'full/latest_raw').symlink_to(full)
            (root/'incr/latest').symlink_to(two)
            fixture.checkpoint_chain('a')
            (two/'xtrabackup_checkpoints').write_text('from_lsn = 21\nto_lsn = 30\n')
            with self.assertRaisesRegex(ValueError,'LSN_GAP'): fixture.checkpoint_chain('a')

    def test_busy_control_lease_refuses_recovery_before_any_remote_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); job=root/'logs/binary80-owned'; job.mkdir(parents=True)
            (job/'plan.json').write_text(json.dumps({'os':'8.10','id':'0123456789ab','base':'/tmp/b80-0123456789ab'}))
            infrastructure=mock.Mock(); infrastructure.control_leases.side_effect=ValueError('busy lease')
            with mock.patch.object(GATE,'ROOT',root), mock.patch.object(GATE,'load',return_value=infrastructure), mock.patch.object(GATE,'run') as remote:
                with self.assertRaisesRegex(ValueError,'busy lease'): GATE.recover(job)
                remote.assert_not_called()

    def test_interrupt_refusal_matches_actual_cli_handler(self):
        from tests.unit.test_mybackup_cli_contract import MOD
        output=io.StringIO()
        with mock.patch.object(MOD,'parse_arguments',return_value=mock.Mock()), mock.patch.object(MOD,'Config',side_effect=KeyboardInterrupt), contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped: MOD.main()
        self.assertEqual(stopped.exception.code,130)
        self.assertIn('Operation cancelled by user',output.getvalue())
        self.assertIn("refuse='Operation cancelled by user'",(ROOT/'tests/helpers/mysql80-binary-live.py').read_text())

    def test_cleanup_only_unmounts_owned_private_nsfs(self):
        base=Path('/tmp/b80-0123456789ab')
        line='592 96 0:4 net:[42] '+str(base)+'/e/netns/default rw - nsfs nsfs rw'
        self.assertEqual(GATE.owned_mounts(line,base),[base/'e/netns/default'])
        self.assertEqual(GATE.owned_mounts(line,Path('/tmp/b80-ffffffffffff')),[])
        with self.assertRaisesRegex(ValueError,'UNEXPECTED_OWNED_MOUNT'):
            GATE.owned_mounts(line.replace('/e/netns/default','/foreign'),base)

    def test_native_fixture_handoff_requires_skip_start_and_real_sql(self):
        source=(ROOT/'tests/helpers/mysql80-binary-live.py').read_text()
        self.assertIn('skip-replica-start=ON',source)
        self.assertIn("fixture.setup(); fixture.seed(); fixture.lifecycle()",source)
        self.assertIn("fixture.cleanup(); fixture=None",source)
        self.assertIn("'--checksig'",source)
        self.assertIn("'ldd'",source)
        self.assertIn("self.sql(name,'RESET REPLICA ALL')",source)


if __name__=='__main__': unittest.main()
