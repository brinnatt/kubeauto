"""Focused Harbor runner refusal, real handoffs and scoped cleanup contracts."""
import json
import os
import shutil
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from upgrade.lab import harbor_session as lab, harbor_node as node
from upgrade.product import launcher
from upgrade.tests import test_launcher
from upgrade.tests import test_next_session


class HarborSessionContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.session=object.__new__(lab.HarborSession)
        s=self.session
        s.source=self.root/'source';s.target=self.root/'target';s.evidence=self.root/'evidence'
        s.evidence.mkdir();s.source.mkdir();s.target.mkdir()
        s.source_cluster='source-fixture';s.target_cluster='target-fixture'
        s.run_id='v1366-harbor-fixture';s.mode='qualify'
        s.plan={'phases':lab.PHASES,'leased_hosts':['192.168.122.'+h for h in ['217','243','246','193','210','216']]}
        s.cli=Mock();s.command=Mock();s.node=Mock(return_value={'marker':'fixture'})
        s.host_operation=Mock()

    def test_real_admission_refuses_pending_without_a_remote_callback(self):
        fixture=test_launcher.LauncherContracts('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch.object(lab,'MATRIX_PATH',fixture.matrix):
            with self.assertRaisesRegex(launcher.UpgradeAdmissionRefused,'UPG-03'):
                lab.plan('v1366-harbor-fixture')
        with self.assertRaisesRegex(ValueError,'identity'):lab.plan('v1366-foreign')

    def test_enabled_execution_and_exact_focused_scope_are_required(self):
        fixture=test_next_session.NextAdmissionContracts('runTest')
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.accept_static()
        matrix=fixture.fixture.matrix
        scope={'enabled':True,'runner':'upgrade/lab/harbor-session.sh',
               'phases':lab.PHASES,'active_hosts':[lab.SOURCE_HOST,lab.TARGET_HOST]}
        path=matrix.parent/'acceptance-plan.yaml'
        path.write_text(yaml.safe_dump({'harbor_native_amd64_qualification':scope}))
        with patch.object(lab,'MATRIX_PATH',matrix):
            result=lab.plan('v1366-harbor-fixture')
            self.assertEqual(result['builds'],[])
            self.assertEqual(result['active_hosts'],[lab.SOURCE_HOST,lab.TARGET_HOST])
            self.assertEqual(result['kubernetes_mutation'],'none')
            scope['active_hosts']=[lab.TARGET_HOST]
            path.write_text(yaml.safe_dump({'harbor_native_amd64_qualification':scope}))
            with self.assertRaisesRegex(ValueError,'scope must be frozen'):lab.plan('v1366-harbor-fixture')
            fixture.fixture.data['meta']['execution_enabled']=False;fixture.fixture.save()
            with self.assertRaisesRegex(ValueError,'explicitly enabled'):lab.plan('v1366-harbor-fixture')

    def test_logical_plan_has_exact_qualified_scope_and_no_builds(self):
        fixture=test_next_session.NextAdmissionContracts('runTest')
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.accept_static()
        matrix=fixture.fixture.matrix
        scope={'enabled':True,'runner':'upgrade/lab/harbor-session.sh',
               'phases':lab.PORTABLE_PHASES,'active_hosts':[lab.SOURCE_HOST,lab.TARGET_HOST]}
        path=matrix.parent/'acceptance-plan.yaml'
        path.write_text(yaml.safe_dump({'harbor_logical_qualification':scope}))
        with patch.object(lab,'MATRIX_PATH',matrix):
            result=lab.plan(self.session.run_id,'portable')
            self.assertEqual(result['phases'],lab.PORTABLE_PHASES)
            self.assertEqual(result['builds'],[])
            self.assertIn('without native execution',result['architecture'])
            scope['enabled']=False;path.write_text(yaml.safe_dump({'harbor_logical_qualification':scope}))
            with self.assertRaisesRegex(ValueError,'scope must be frozen'):lab.plan(self.session.run_id,'portable')

    def test_source_uses_product_setup_before_fixture_creation(self):
        s=self.session;trace=[]
        s.host_operation.side_effect=lambda op:trace.append(op)
        s.bootstrap=Mock(side_effect=lambda host:trace.append('bootstrap'))
        s.configure=Mock(side_effect=lambda: (setattr(s,'base',s.source),setattr(s,'cluster',s.source_cluster)))
        s.packages=Mock(side_effect=lambda host:trace.append('qualified-packages'))
        s.cli.side_effect=lambda base,code,*args:trace.append(args)
        config={'hostname':'harbor.upgrade.invalid','https':{'port':8443},'harbor_admin_password':'private'}
        def fixture(action,**kwargs):
            trace.append(action)
            return {'configuration':yaml.safe_dump(config)} if action=='configuration' else {'marker':'fixture'}
        s.node.side_effect=fixture
        s.source_phase()
        self.assertEqual(trace[:3],['admit','claim','bootstrap'])
        self.assertLess(trace.index(('setup','source-fixture','11')),trace.index('create'))
        self.assertEqual([row for row in trace if isinstance(row,tuple)],
                         [('setup','source-fixture',n) for n in ['01','03','11']])

    def test_migration_and_restore_handoff_use_original_checkpoint_and_read_only_ledger(self):
        s=self.session;trace=[]
        s.cli.side_effect=lambda base,code,*args:trace.append(args)
        s.record_node=Mock(side_effect=lambda name:trace.append(('verify',name)))
        s.migration_phase();s.restore_phase()
        self.assertEqual([t[3] for t in trace if t[0]=='upgrade'],['backup','upgrade','upgrade','restore'])
        for row in trace:
            if row[0]=='upgrade':
                self.assertIn(lab.NODE_STATE+'/backup-original',row)
        self.assertEqual([t for t in trace if t[0]=='verify'],
                         [('verify','migrated'),('verify','idempotent'),('verify','restored')])
        self.assertFalse(s.node.called)

    def test_a_failed_product_operation_stops_before_business_assertion(self):
        s=self.session;s.record_node=Mock();s.cli.side_effect=RuntimeError('first failure')
        with self.assertRaisesRegex(RuntimeError,'first failure'):s.migration_phase()
        s.record_node.assert_not_called();self.assertEqual(s.cli.call_count,1)

    def test_new_install_repair_uses_original_prepared_target_and_only_setup11(self):
        s=self.session;s.repair_new_install_boundary=Mock()
        s.finish_new_install=Mock();s.record_node=Mock()
        s.select(target=True)
        s.repair_new_install()
        s.cli.assert_called_once_with(s.target,s.target,'setup',s.target_cluster,'11')
        s.finish_new_install.assert_called_once_with()
        s.record_node.assert_called_once_with('repair-source-preserved')

    def test_repair_boundary_requires_real_leases_exact_first_failure_and_unchanged_config(self):
        from upgrade.lab import next_session
        s=self.session;s.owner=self.root/'owner.json';s.owner.write_text(json.dumps(s.plan))
        locks=self.root/'locks';locks.mkdir();handles=[]
        for name in next_session.LOCKS.values():
            handle=(locks/name).open('w');handles.append(handle);self.addCleanup(handle.close)
            lab.fcntl.flock(handle,lab.fcntl.LOCK_EX|lab.fcntl.LOCK_NB)
        for phase in ['harbor-source','harbor-migration','harbor-restore']:
            (s.evidence/(phase+'.status.json')).write_text('{"rc":0}')
        status={'rc':1,'attempt':1,'log':'original-failure.log'}
        (s.evidence/'harbor-new-install.status.json').write_text(json.dumps(status))
        failure={'rc':127,'stderr':'/bin/sh: line 1: tar: command not found',
                 'cmd':'cd /var/data && tar zxf harbor-offline-installer-v2.16.0.tgz'}
        log=s.evidence/status['log'];log.write_text('fatal: [host]: FAILED! => '+json.dumps(failure)+'\n')
        config=s.target/'clusters'/s.target_cluster/'config.yml';config.parent.mkdir(parents=True)
        config.write_text('original: config\n')
        (s.evidence/'target-config.json').write_text(json.dumps({'path':str(config),'sha256':lab.sha(config)}))
        s.node.return_value={'containers':[]}
        with patch.object(next_session,'LOCK_DIRECTORY',locks):
            s.repair_new_install_boundary()
            config.write_text('changed: config\n')
            with self.assertRaisesRegex(ValueError,'configuration changed'):s.repair_new_install_boundary()
            config.write_text('original: config\n')
            s.node.return_value={'containers':[{'Id':'unexpected'}]}
            with self.assertRaisesRegex(ValueError,'no Harbor containers'):s.repair_new_install_boundary()
            s.node.return_value={'containers':[]}
            failure['rc']=2;log.write_text('fatal: [host]: FAILED! => '+json.dumps(failure)+'\n')
            with self.assertRaisesRegex(ValueError,'proven missing archive'):s.repair_new_install_boundary()
            failure['rc']=127;log.write_text('fatal: [host]: FAILED! => '+json.dumps(failure)+'\n')
            handles[0].close()
            with self.assertRaisesRegex(ValueError,'retain all shared leases'):s.repair_new_install_boundary()
        s.cli.assert_not_called()

    def test_clean_target_install_uses_target_product_then_repeats_without_recreating_ledger(self):
        s=self.session;s.bootstrap=Mock();s.preserve=Mock();s.record_node=Mock()
        def configure(target=False):s.select(target)
        s.configure=Mock(side_effect=configure)
        config={'hostname':'harbor.upgrade.invalid','https':{'port':8443},'harbor_admin_password':'private'}
        s.node.side_effect=lambda action,**kwargs:({'configuration':yaml.safe_dump(config)}
            if action=='configuration' else {'marker':'fixture'})
        s.new_install_phase()
        self.assertEqual([c.args for c in s.cli.call_args_list],
            [(s.target,s.target,'setup',s.target_cluster,stage) for stage in ['01','03','11','11']])
        s.bootstrap.assert_called_once_with(lab.TARGET_HOST)
        self.assertEqual([c for c in s.node.call_args_list if c.args==('create',)],
                         [unittest.mock.call('create',host=lab.TARGET_HOST)])
        self.assertEqual(s.record_node.call_args_list,[
            unittest.mock.call('target-installed',host=lab.TARGET_HOST),
            unittest.mock.call('target-idempotent',host=lab.TARGET_HOST)])

    def test_target_cleanup_does_not_wipe_or_verify_the_still_live_source(self):
        s=self.session
        archive=s.evidence/'saved-target';archive.mkdir();(archive/'confirmed').write_bytes(b'original')
        s.preserve=Mock(return_value=archive)
        s.clean_phase(target=True)
        s.cli.assert_not_called()
        self.assertIn(unittest.mock.call('runtime-clean-admission',host=lab.TARGET_HOST),s.node.call_args_list)
        s.command.assert_called_once_with(['bash',lab.ROOT/'tests/helpers/lab-wipe-nodes.sh',
                                           '--rocky-only',lab.TARGET_HOST])
        self.assertNotIn(unittest.mock.call('clean'),s.host_operation.call_args_list)
        self.assertTrue((s.evidence/'target-harbor-node-clean.json').exists())

    def test_preserved_node_archive_has_exact_manifest_and_target_host_identity(self):
        s=self.session
        def transfer(args,**kwargs):
            destination=Path(args[-1]);(destination/'recovery').write_bytes(b'confirmed original state')
        s.command.side_effect=transfer
        archive=s.preserve('fixture',1,host=lab.TARGET_HOST)
        receipt=json.loads((s.evidence/(archive.name+'.manifest.json')).read_text())
        self.assertEqual(receipt['host'],lab.TARGET_HOST)
        self.assertEqual(receipt['files'],{'recovery':lab.sha(archive/'recovery')})
        self.assertEqual(s.command.call_args.args[0][-2],'root@'+lab.TARGET_HOST+':'+lab.NODE_STATE+'/')

    def test_archive_reuses_completed_same_host_snapshot_without_copying_large_unchanged_files(self):
        s=self.session
        source=self.root/'node-state';source.mkdir(mode=0o700)
        (source/'recovery').write_bytes(b'original recovery')
        (source/'changed').write_bytes(b'new-content')
        for p in source.iterdir():p.chmod(0o600)
        candidates={}
        for name,host,complete in [('harbor-source',lab.SOURCE_HOST,True),
                ('harbor-restore',lab.SOURCE_HOST,True),('foreign-target',lab.TARGET_HOST,True),
                ('partial-transfer',lab.SOURCE_HOST,False)]:
            p=s.evidence/(name+'.attempt-1.node-state');p.mkdir(mode=0o700)
            candidates[name]=p
            if name!='harbor-source':
                for f in source.iterdir():shutil.copy2(f,p/f.name)
                # Same size and mtime is insufficient to reuse changed content.
                if name=='harbor-restore':
                    (p/'changed').write_bytes(b'old-content')
                    stat=(source/'changed').stat();os.utime(p/'changed',ns=(stat.st_atime_ns,stat.st_mtime_ns))
            if complete:
                (s.evidence/(p.name+'.manifest.json')).write_text(json.dumps({'host':host,'directory':str(p)}))
        def transfer(args,**kwargs):
            self.assertEqual(set(a for a in args if a.startswith('--link-dest=')),
                {'--link-dest='+str(candidates[n]) for n in ['harbor-source','harbor-restore']})
            actual=list(args);actual[-2]=str(source)+'/'
            subprocess.run(actual,check=True,capture_output=True)
        s.command.side_effect=transfer
        archive=s.preserve('harbor-clean',3)
        self.assertEqual((archive/'recovery').stat().st_ino,
                         (candidates['harbor-restore']/'recovery').stat().st_ino)
        self.assertEqual((archive/'changed').read_bytes(),b'new-content')
        self.assertEqual((candidates['harbor-restore']/'changed').read_bytes(),b'old-content')

    def test_cleanup_frontend_rejects_invalid_attempt_without_connecting(self):
        # Exercise real argument validation; this proves no remote handoff for
        # an unsupported attempt, not that a simulated cleanup succeeded.
        bin_dir=self.root/'bin';bin_dir.mkdir()
        marker=self.root/'connected'
        ssh=bin_dir/'ssh';ssh.write_text('#!/bin/sh\ntouch "$PROBE_CONNECTED"\nexit 97\n');ssh.chmod(0o700)
        env={**os.environ,'PATH':str(bin_dir)+':'+os.environ['PATH'],'PROBE_CONNECTED':str(marker)}
        for operation,attempt in [('repair-cleanup','0'),('repair-cleanup','4'),('repair-new-install','2')]:
            result=subprocess.run(['bash',str(lab.ROOT/'upgrade/lab/harbor-control.sh'),
                operation,self.session.run_id,attempt],env=env,capture_output=True)
            self.assertNotEqual(result.returncode,0);self.assertFalse(marker.exists())

    def test_portable_handoff_preserves_original_source_and_never_recreates_business(self):
        s=self.session;trace=[]
        s.bootstrap=Mock();s.configure=Mock(side_effect=lambda target=False:s.select(target))
        s.packages=Mock();s.preserve=Mock();s.record_node=Mock()
        s.transfer_export=Mock(side_effect=lambda *a,**k:trace.append(('transfer',a,k)))
        s.cli.side_effect=lambda base,code,*args:trace.append(('cli',base,args))
        s.node.side_effect=lambda action,**kwargs:({'containers':[{'State':{'Running':False}}]}
            if action=='inventory' else {'marker':'fixture'})
        s.portable_phase()
        operations=[row[2][3] for row in trace if row[0]=='cli' and row[2][0]=='upgrade']
        self.assertEqual(operations,['backup','export','import','backup','export','abort-import',
                                    'import','abort-import','abort-import','restore'])
        installs=[row[2] for row in trace if row[0]=='cli' and row[2][0]=='setup']
        self.assertEqual(installs,[('setup',s.target_cluster,'01'),('setup',s.target_cluster,'03')])
        self.assertNotIn('create',[c.args[0] for c in s.node.call_args_list])
        self.assertEqual(s.record_node.call_args_list,[
            unittest.mock.call('portable213-imported',host=lab.TARGET_HOST),
            unittest.mock.call('portable216-imported',host=lab.TARGET_HOST),
            unittest.mock.call('portable-source-restored')])
        for row in trace:
            if row[0]=='cli' and row[2][:1]==('upgrade',) and row[2][3]=='import':
                self.assertEqual(row[1],s.target)
                self.assertIn('--harbor-transfer-dir',row[2])

    def test_portable_failed_import_cannot_continue_to_ledger_or_source_restore(self):
        s=self.session;s.bootstrap=Mock();s.packages=Mock();s.preserve=Mock();s.record_node=Mock()
        s.configure=Mock(side_effect=lambda target=False:s.select(target));s.transfer_export=Mock()
        def invoke(base,code,*args):
            if '--harbor' in args and args[3]=='import':raise ValueError('import failed')
        s.cli.side_effect=invoke
        with self.assertRaisesRegex(ValueError,'import failed'):s.portable_phase()
        s.record_node.assert_not_called()
        self.assertFalse(any(c.args[2:6]==('upgrade',s.source_cluster,'--harbor','restore')
                             for c in s.cli.call_args_list))

    def test_both_portable_handoffs_prepare_packages_before_stopping_source(self):
        for prepared in [False,True]:
            with self.subTest(prepared=prepared):
                s=self.session;trace=[]
                marker=s.evidence/'target-config.json'
                if prepared:marker.write_text('{}')
                s.bootstrap=Mock();s.configure=Mock(side_effect=lambda target=False:s.select(target))
                s.packages=Mock(side_effect=lambda host:trace.append(('packages',host)))
                s.node=Mock();s.transfer_export=Mock();s.record_node=Mock();s.finish_portable=Mock()
                s.maintenance=Mock(side_effect=lambda op,**kw:trace.append((op,kw)))
                s.assert_source_stopped=Mock()
                s.portable_phase()
                self.assertEqual(trace[0],('packages',lab.TARGET_HOST))
                self.assertEqual(trace[1],('backup',{'backup':'backup-portable213'}))
                s.packages.assert_called_once_with(lab.TARGET_HOST)
                if prepared:
                    s.bootstrap.assert_not_called();s.configure.assert_not_called()
                s.packages.side_effect=ValueError('missing qualified package')
                s.maintenance.reset_mock()
                with self.assertRaisesRegex(ValueError,'missing qualified package'):s.portable_phase()
                s.maintenance.assert_not_called()

    def test_package_transfer_validates_owner_bytes_and_atomic_pending_handoff(self):
        from upgrade.lab import harbor_inputs
        s=self.session;state=self.root/'node';state.mkdir()
        (state/'harbor_node.py').write_text('def owner(run):\n assert run=='+repr(s.run_id)+'\n')
        down=s.target/'down';down.mkdir();name='qualified.tgz';payload=b'qualified-offline-fixture'
        (down/name).write_bytes(payload)
        import hashlib
        digest=hashlib.sha256(payload).hexdigest()
        def remote(host,args,*,data):
            subprocess.run([sys.executable,'-B','-'],input=data,text=True,check=True,capture_output=True)
        def transfer(args,**kwargs):shutil.copy2(args[-2],state/Path(args[-1]).name)
        s.remote=Mock(side_effect=remote);s.command.side_effect=transfer
        with patch.object(lab,'NODE_STATE',str(state)),patch.object(lab,'PACKAGES',{'fixture':name}),\
                patch.object(harbor_inputs,'PACKAGES',{'fixture':digest}):
            s.packages(lab.TARGET_HOST)
            self.assertEqual((state/name).read_bytes(),payload)
            self.assertFalse((state/(name+'.pending')).exists())
            (state/name).write_bytes(b'foreign');s.command.reset_mock()
            with self.assertRaises(subprocess.CalledProcessError):s.packages(lab.TARGET_HOST)
            s.command.assert_not_called();(state/name).unlink()
            (state/(name+'.pending')).write_bytes(b'interrupted')
            with self.assertRaises(subprocess.CalledProcessError):s.packages(lab.TARGET_HOST)
            (state/(name+'.pending')).unlink()
            (down/name).write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'qualified original'):s.packages(lab.TARGET_HOST)
            with self.assertRaisesRegex(ValueError,'fixed Harbor pool'):s.packages('foreign')
            s.command.assert_not_called()

    def test_paused_supply_checks_real_supervisor_locks_inputs_and_first_attempts(self):
        from upgrade.lab import next_session
        s=self.session;s.mode='final';s.owner=self.root/'owner.json'
        s.owner.write_text(json.dumps(s.plan));parent='v1366-final-fixture';pid=12345
        pause={'run_id':parent,'child':s.run_id,'supervisor_pid':pid,'signal':'SIGSTOP'}
        (s.evidence/'preemptive-fixture-pause.json').write_text(json.dumps(pause))
        proc=self.root/'proc';process=proc/str(pid);(process/'fd').mkdir(parents=True)
        command=['/usr/local/kubeauto/.venv/bin/python','-B','-m','upgrade.lab.next_session',
                 'run','--mode','final','--run-id',parent]
        (process/'cmdline').write_bytes(b'\0'.join(x.encode() for x in command)+b'\0')
        (process/'status').write_text('State:\tT (stopped)\n')
        locks=self.root/'locks';locks.mkdir();handles=[]
        for descriptor,name in next_session.LOCKS.items():
            path=locks/name;handle=path.open('w');handles.append(handle);self.addCleanup(handle.close)
            lab.fcntl.flock(handle,lab.fcntl.LOCK_EX|lab.fcntl.LOCK_NB)
            (process/'fd'/str(descriptor)).symlink_to(path)
        for phase in ['harbor-source','harbor-migration','harbor-restore','harbor-new-install']:
            (s.evidence/(phase+'.status.json')).write_text('{"rc":0,"attempt":1}')
            for suffix in ['exit','finalized']:(s.evidence/(phase+'.attempt-1.'+suffix)).write_text('0\n')
        for target in [False,True]:
            base=s.target if target else s.source;cluster=s.target_cluster if target else s.source_cluster
            config=base/'clusters'/cluster/'config.yml';config.parent.mkdir(parents=True);config.write_text('original')
            (s.evidence/('target-config.json' if target else 'source-config.json')).write_text(
                json.dumps({'path':str(config),'sha256':lab.sha(config)}))
        with patch.object(lab,'PROC',proc),patch.object(next_session,'LOCK_DIRECTORY',locks),\
                patch.object(next_session,'verify_inputs') as verify:
            lab.paused_package_boundary(s,parent,pid);verify.assert_called_once()
            (process/'status').write_text('State:\tS (sleeping)\n')
            with self.assertRaisesRegex(ValueError,'remain stopped'):lab.paused_package_boundary(s,parent,pid)
            (process/'status').write_text('State:\tT (stopped)\n')
            (s.evidence/'harbor-portable.status.json').write_text('{"rc":1}')
            with self.assertRaisesRegex(ValueError,'pre-import'):lab.paused_package_boundary(s,parent,pid)
            (s.evidence/'harbor-portable.status.json').unlink()
            config.write_text('changed')
            with self.assertRaisesRegex(ValueError,'configuration changed'):lab.paused_package_boundary(s,parent,pid)
            config.write_text('original');handles[0].close()
            with self.assertRaisesRegex(ValueError,'retain all shared leases'):lab.paused_package_boundary(s,parent,pid)

    def test_paused_supply_records_only_success_and_resume_binds_exact_helper(self):
        s=self.session;s.remote=Mock();s.packages=Mock(side_effect=ValueError('package transfer refused'))
        parent='v1366-final-fixture';pid=12345
        with patch.object(lab,'paused_package_boundary') as boundary,patch.object(lab.os,'kill') as kill:
            with self.assertRaisesRegex(ValueError,'package transfer refused'):
                lab.supply_paused_packages(s,parent,pid)
            self.assertFalse((s.evidence/'paused-package-supply.json').exists());kill.assert_not_called()
            s.packages.side_effect=None
            lab.supply_paused_packages(s,parent,pid)
            record=s.evidence/'paused-package-supply.json';saved=json.loads(record.read_text())
            self.assertEqual(saved['helper_sha256'],lab.sha(Path(lab.__file__)))
            lab.resume_supplied_supervisor(s,parent,pid)
            boundary.assert_called_with(s,parent,pid,supplied=True)
            kill.assert_called_once_with(pid,lab.signal.SIGCONT)
            saved['supervisor_pid']+=1;record.write_text(json.dumps(saved));kill.reset_mock()
            with self.assertRaisesRegex(ValueError,'completed exact fixture supply'):
                lab.resume_supplied_supervisor(s,parent,pid)
            kill.assert_not_called()

    def test_paused_supply_boundary_refusal_never_touches_nodes(self):
        s=self.session;s.remote=Mock();s.packages=Mock()
        with patch.object(lab,'paused_package_boundary',side_effect=ValueError('unsafe pause')):
            with self.assertRaisesRegex(ValueError,'unsafe pause'):
                lab.supply_paused_packages(s,'v1366-final-fixture',12345)
        s.remote.assert_not_called();s.packages.assert_not_called()

    def test_export_repair_reuses_complete_backup_and_never_repeats_setup(self):
        s=self.session;s.repair_portable_boundary=Mock();s.record_node=Mock();s.preserve=Mock()
        s.transfer_export=Mock();s.assert_source_stopped=Mock()
        s.portable_phase(resume_export=True)
        s.repair_portable_boundary.assert_called_once_with()
        operations=[c.args[5] for c in s.cli.call_args_list]
        self.assertEqual(operations,['export','import','backup','export','abort-import','import',
                                     'abort-import','abort-import','restore'])
        self.assertFalse(any('setup' in c.args for c in s.cli.call_args_list))
        s.repair_portable_boundary.side_effect=ValueError('unknown checkpoint')
        s.cli.reset_mock()
        with self.assertRaisesRegex(ValueError,'unknown checkpoint'):s.portable_phase(resume_export=True)
        s.cli.assert_not_called()

    def test_export_repair_checks_original_owner_locks_configs_and_exact_failed_boundary(self):
        from upgrade.lab import next_session
        s=self.session;s.mode='portable';s.owner=self.root/'owner.json'
        s.owner.write_text(json.dumps(s.plan));s.assert_source_stopped=Mock()
        for suffix in ['exit','finalized']:(s.evidence/('harbor-source.attempt-1.'+suffix)).write_text('0\n')
        failed=s.evidence/'harbor-portable.status.json';failed.write_text(json.dumps({'rc':1,'attempt':1}))
        for target in [False,True]:
            s.select(target);p=s.base/'clusters'/s.cluster/'config.yml';p.parent.mkdir(parents=True)
            p.write_text('original private configuration')
            (s.evidence/('target-config.json' if target else 'source-config.json')).write_text(
                json.dumps({'path':str(p),'sha256':lab.sha(p)}))
        locks=self.root/'locks';locks.mkdir()
        for name in next_session.LOCKS.values():(locks/name).touch()
        s.node.return_value={'containers':[]}
        s.remote=Mock(side_effect=['HARBOR_EXPORT_REPAIR_ADMISSION_PASS\n','HARBOR_EMPTY_TARGET_PASS\n'])
        with patch.object(next_session,'LOCK_DIRECTORY',locks),patch.object(lab.fcntl,'flock',side_effect=BlockingIOError):
            s.repair_portable_boundary()
            for call in s.remote.call_args_list:
                program=call.kwargs['data'];compile(program,'repair-admission','exec')
                self.assertNotIn('write_text',program)
            self.assertIn("r['phase']=='portable-export-intent'",s.remote.call_args_list[0].kwargs['data'])
            s.remote.reset_mock();failed.write_text(json.dumps({'rc':1,'attempt':2}))
            with self.assertRaisesRegex(ValueError,'first export failure'):s.repair_portable_boundary()
            s.remote.assert_not_called()
            failed.write_text(json.dumps({'rc':1,'attempt':1}));p.write_text('changed')
            with self.assertRaisesRegex(ValueError,'configuration changed'):s.repair_portable_boundary()
            s.remote.assert_not_called()

    def test_valkey_backup_repair_reuses_first_import_and_stops_at_failed_backup(self):
        s=self.session;s.repair_portable_boundary=Mock();s.record_node=Mock();s.preserve=Mock()
        s.transfer_export=Mock();s.assert_source_stopped=Mock()
        s.portable_phase(resume_backup=True)
        s.repair_portable_boundary.assert_called_once_with(stopped_backup=True)
        self.assertEqual([c.args[5] for c in s.cli.call_args_list],
            ['backup','export','abort-import','import','abort-import','abort-import','restore'])
        self.assertNotIn(unittest.mock.call('portable213-imported',host=lab.TARGET_HOST),s.record_node.call_args_list)
        s.cli.reset_mock();s.record_node.reset_mock();s.cli.side_effect=ValueError('backup refused')
        with self.assertRaisesRegex(ValueError,'backup refused'):s.portable_phase(resume_backup=True)
        self.assertEqual(s.cli.call_count,1);s.record_node.assert_not_called()

    def test_valkey_boundary_executes_actual_checkpoint_and_ledger_refusals(self):
        import types
        s=self.session;prefix=s.evidence/'harbor-portable-repair.attempt-1'
        for suffix in ['exit','finalized']:Path(str(prefix)+'.'+suffix).write_text('1\n')
        log=Path(str(prefix)+'.log');log.write_text('the stopped queue has no valid RDB snapshot header')
        accepted={'marker':'HARBOR_BUSINESS_LEDGER_PASS','project_id':2,'artifact_id':1,
                  'digest':'sha256:confirmed','payload_sha256':'confirmed-payload'}
        (s.evidence/'portable213-imported-business.json').write_text(json.dumps(accepted))
        s.node.return_value={'containers':[{'State':{'Running':False}}]}
        state=self.root/'node';state.mkdir();backup=state/'backup-portable216';backup.mkdir()
        receipt={'phase':'stopped','source':'v2.16.0','files':{},'tls':{},'queue':{'engine':'valkey','version':'9.0.3'}}
        (backup/'receipt.json').write_text(json.dumps(receipt))
        imported=state/'backup-import213';imported.mkdir()
        (imported/'receipt.json').write_text('{"phase":"v2.16.0-ready","kind":"harbor-portable-target"}')
        (state/'ledger.json').write_text(json.dumps(accepted))
        data=self.root/'data';(data/'redis').mkdir(parents=True);rdb=data/'redis/dump.rdb';rdb.write_bytes(b'VALKEY080')
        def execute(host,args,*,data):
            self.assertEqual(host,lab.TARGET_HOST)
            program=data.replace(lab.NODE_STATE,str(state)).replace('/var/data',str(self.root/'data'))
            with patch.dict(sys.modules,{'harbor_node':types.SimpleNamespace(owner=lambda run:self.assertEqual(run,s.run_id))}):
                exec(compile(program,'valkey-admission','exec'),{})
            return 'HARBOR_STOPPED_VALKEY_REPAIR_ADMISSION_PASS'
        s.remote=Mock(side_effect=execute)
        s.stopped_backup_boundary()
        for name in ['data.tar','images.tar','portable.json','registry.dump']:
            path=backup/name;path.write_text('partial')
            with self.assertRaises(AssertionError):s.stopped_backup_boundary()
            path.unlink()
        rdb.write_bytes(b'VALKEY081')
        with self.assertRaises(AssertionError):s.stopped_backup_boundary()
        rdb.write_bytes(b'VALKEY080');wrong=dict(accepted,digest='changed')
        (state/'ledger.json').write_text(json.dumps(wrong))
        with self.assertRaises(AssertionError):s.stopped_backup_boundary()
        (state/'ledger.json').write_text(json.dumps(accepted));log.write_text('other failure')
        with self.assertRaisesRegex(ValueError,'proven Valkey'):s.stopped_backup_boundary()
        s.cli.assert_not_called()

    def test_repair_input_transfer_refuses_wrong_attempt_before_copying_any_file(self):
        s=self.session;state=self.root/'state';r=state/'runs'/s.run_id;r.mkdir(parents=True)
        (state/'owner.json').write_text(json.dumps({'run_id':s.run_id,'mode':'portable'}))
        (r/'harbor-source.status.json').write_text('{"rc":0}')
        (r/'harbor-portable.status.json').write_text('{"rc":1,"attempt":2}')
        bin_dir=self.root/'bin';bin_dir.mkdir();copied=self.root/'copied'
        ssh=bin_dir/'ssh'
        ssh.write_text('#!'+sys.executable+'\nimport os,subprocess,sys\n'
            'text=sys.stdin.read().replace("/var/tmp/kubeauto-v1366-lab",os.environ["FIXTURE_STATE"])'
            '.replace("/usr/local/kubeauto/.venv/bin/python",sys.executable)\n'
            'raise SystemExit(subprocess.run(["bash","-s","--",os.environ["FIXTURE_RUN"]],input=text,text=True).returncode)\n')
        ssh.chmod(0o700)
        rsync=bin_dir/'rsync';rsync.write_text('#!/bin/sh\ntouch "$FIXTURE_COPIED"\n');rsync.chmod(0o700)
        result=subprocess.run(['bash',str(lab.ROOT/'upgrade/lab/harbor-control.sh'),
            'repair-portable-inputs',s.run_id],env={**os.environ,'PATH':str(bin_dir)+':'+os.environ['PATH'],
            'FIXTURE_STATE':str(state),'FIXTURE_RUN':s.run_id,'FIXTURE_COPIED':str(copied)},capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(copied.exists());self.assertFalse((r/'export-repair-original-inputs').exists())

    def test_original_source_must_still_be_stopped(self):
        s=self.session
        for rows in [[],[{'State':{'Running':True}}]]:
            s.node.return_value={'containers':rows}
            with self.assertRaisesRegex(ValueError,'stopped boundary'):s.assert_source_stopped()

    def test_final_real_admission_requires_all_prerequisites_before_any_scope_read(self):
        fixture=test_next_session.NextAdmissionContracts('runTest')
        fixture.setUp();self.addCleanup(fixture.doCleanups);fixture.accept_static()
        with patch.object(lab,'MATRIX_PATH',fixture.fixture.matrix):
            with self.assertRaises(launcher.UpgradeAdmissionRefused):lab.plan(self.session.run_id,'final')

    def test_new_archive_files_use_separate_capacity_and_manifest_absolute_directory(self):
        s=self.session;s.archive_directory=self.root/'archive-volume';s.archive_directory.mkdir()
        def transfer(args,**kwargs):(Path(args[-1])/'original').write_bytes(b'recovery')
        s.command.side_effect=transfer
        archive=s.preserve('source',1)
        self.assertEqual(archive.parent,s.archive_directory)
        self.assertEqual(json.loads((s.evidence/(archive.name+'.manifest.json')).read_text())['directory'],str(archive))

    def test_transfer_uses_real_private_tar_stream_and_failure_propagates(self):
        import shlex
        s=self.session;source=self.root/'source-node';target=self.root/'target-node'
        source.mkdir();target.mkdir();backup=source/'backup-portable213';backup.mkdir()
        (backup/'portable.json').write_bytes(b'private manifest');(backup/'registry.dump').write_bytes(b'PGDMP')
        destination=target/'transfer-213';destination.mkdir()
        binary=self.root/'bin';binary.mkdir();ssh=binary/'ssh'
        ssh.write_text('#!/usr/bin/env python3\nimport os,sys,subprocess\n'
            'host=sys.argv[-2]; command=sys.argv[-1]\n'
            'root=os.environ["SOURCE_NODE"] if host=="root@192.168.122.217" else os.environ["TARGET_NODE"]\n'
            'command=command.replace("/var/tmp/kubeauto-harbor-v1366",root)\n'
            'sys.exit(subprocess.run(command,shell=True).returncode)\n');ssh.chmod(0o700)
        s.remote=Mock(return_value=json.dumps({'settings.json':'{}','ledger.json':'{}'}))
        s.node.return_value={'files':['portable.json','registry.dump']}
        environment={**os.environ,'PATH':str(binary)+':'+os.environ['PATH'],
                     'SOURCE_NODE':str(source),'TARGET_NODE':str(target)}
        s.command.side_effect=lambda args:subprocess.run(args,env=environment,check=True,capture_output=True)
        s.transfer_export('backup-portable213','transfer-213')
        self.assertEqual((destination/'registry.dump').read_bytes(),b'PGDMP')
        self.assertEqual((destination/'portable.json').read_bytes(),b'private manifest')
        (backup/'registry.dump').unlink()
        with self.assertRaises(subprocess.CalledProcessError):s.transfer_export('backup-portable213','transfer-213')

    def test_actual_phase_supervisor_preserves_failure_and_cannot_reach_restore_or_release(self):
        s=self.session;s.owner_check=Mock();s.preserve=Mock()
        s.source_phase=Mock();s.migration_phase=Mock(side_effect=ValueError('first product failure'))
        s.restore_phase=Mock();s.clean_phase=Mock()
        s.wait=Mock(side_effect=['advance',RuntimeError('stop isolated supervisor')])
        with patch.object(lab,'phase_log',side_effect=lambda path:nullcontext()), \
                patch.object(lab.signal,'signal'),patch.object(lab,'admit_static'):
            with self.assertRaisesRegex(RuntimeError,'stop isolated supervisor'):s.run()
        s.restore_phase.assert_not_called();s.clean_phase.assert_not_called()
        status=json.loads((s.evidence/'harbor-migration.status.json').read_text())
        self.assertEqual(status['reason'],'first product failure');self.assertEqual(status['rc'],1)
        self.assertEqual(status['lease'],'held')
        self.assertTrue((s.evidence/'harbor-migration.failure-1.json').exists())
        self.assertNotIn(unittest.mock.call('release'),s.host_operation.call_args_list)
        self.assertEqual((s.evidence/'harbor-source.attempt-1.exit').read_text(),'0\n')

    def test_configuration_is_fixed_native_host_and_tls_is_not_bypassed(self):
        s=self.session
        (s.source/'conf').mkdir()
        (s.source/'conf/hosts.allinone').write_text((lab.ROOT/'conf/hosts.allinone').read_text())
        def new(base,code,*args):
            d=base/'clusters'/args[1];d.mkdir(parents=True)
            (d/'config.yml').write_text('HARBOR_VER: v2.13.0\n')
        s.cli.side_effect=new;s.configure()
        d=s.source/'clusters'/s.source_cluster
        config=yaml.safe_load((d/'config.yml').read_text())
        self.assertEqual(config['INSECURE_REG'],[])
        self.assertFalse(config['ENABLE_MIRROR_REGISTRY'])
        self.assertEqual(config['HARBOR_PATH'],'/var/data')
        self.assertEqual(config['HARBOR_VER'],'v2.13.0')
        # Parse the actual generated INI with Ansible instead of regex-only stubs.
        result=subprocess.run([str(lab.ROOT/'.venv/bin/ansible-inventory'),'-i',str(d/'hosts'),'--list'],
                              text=True,capture_output=True,check=True)
        inventory=json.loads(result.stdout)
        self.assertEqual(inventory['harbor']['hosts'],[lab.SOURCE_HOST])
        self.assertEqual(inventory['kube_master']['hosts'],[lab.SOURCE_HOST])
        self.assertEqual(inventory['_meta']['hostvars'][lab.SOURCE_HOST]['base_dir'],str(s.source))
        self.assertEqual(inventory['_meta']['hostvars'][lab.SOURCE_HOST]['CONTAINER_RUNTIME'],'docker')

    def test_cleanup_requires_owned_data_and_verified_durable_archive_before_runtime_wipe(self):
        s=self.session;trace=[]
        archive=s.evidence/'saved';archive.mkdir();(archive/'confirmed').write_bytes(b'original recovery')
        s.preserve=Mock(return_value=archive)
        s.node.side_effect=lambda action,**kwargs:trace.append(action) or {'marker':'fixture'}
        s.cli.side_effect=lambda *args:trace.append('product-destroy')
        s.command.side_effect=lambda args,**kwargs:trace.append('runtime-wipe')
        s.clean_phase()
        self.assertEqual(trace,['stop','clean-data','runtime-clean-admission','runtime-wipe','clean-state'])
        s.cli.assert_not_called()
        # Re-entry after a release failure cannot delete/recreate the archives.
        trace.clear();s.clean_phase();self.assertEqual(trace,[])
        (archive/'confirmed').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'archive changed'):s.clean_phase()
        self.assertEqual(trace,[])

    def test_foreign_ssh_host_refuses_before_command(self):
        with self.assertRaisesRegex(ValueError,'pool'):
            self.session.remote('192.168.122.1',['true'])
        self.session.command.assert_not_called()

    def test_runtime_clean_probe_preserves_archived_node_helper_and_binds_owner(self):
        s=self.session;s.remote=Mock(return_value='{"marker":"HARBOR_RUNTIME_ONLY_CLEAN_ADMISSION_PASS"}')
        result=lab.HarborSession.node(s,'runtime-clean-admission',host=lab.TARGET_HOST)
        self.assertEqual(result['marker'],'HARBOR_RUNTIME_ONLY_CLEAN_ADMISSION_PASS')
        self.assertEqual(s.remote.call_args.args,(lab.TARGET_HOST,['python3','-B','-']))
        program=s.remote.call_args.kwargs['data'];compile(program,'runtime-clean-probe','exec')
        self.assertIn('owner('+repr(s.run_id)+')',program)
        self.assertNotIn('write_text',program)


class HarborNodeContracts(unittest.TestCase):
    def test_runtime_only_cleanup_refuses_kubernetes_service_data_or_remaining_containers(self):
        with patch.object(node,'command',return_value='active'):
            with self.assertRaisesRegex(ValueError,'Kubernetes service'):node.runtime_clean_admission()
        with patch.object(node,'command',return_value='inactive'), \
                patch.object(node.Path,'exists',side_effect=lambda:True):
            with self.assertRaisesRegex(ValueError,'Kubernetes data'):node.runtime_clean_admission()
        with patch.object(node,'command',return_value='inactive'),patch.object(node.Path,'exists',return_value=False), \
                patch.object(node.Path,'is_symlink',return_value=False),patch.object(node,'owned_containers',return_value=[{}]):
            with self.assertRaisesRegex(ValueError,'container cleanup'):node.runtime_clean_admission()
        with patch.object(node,'command',return_value='inactive'),patch.object(node.Path,'exists',return_value=False), \
                patch.object(node.Path,'is_symlink',return_value=False),patch.object(node,'owned_containers',return_value=[]):
            self.assertEqual(node.runtime_clean_admission()['marker'],'HARBOR_RUNTIME_ONLY_CLEAN_ADMISSION_PASS')

    def test_foreign_container_refuses_before_stop_or_data_cleanup(self):
        foreign={'Id':'foreign','Config':{'Labels':{'com.docker.compose.project.working_dir':'/unrelated',
                 'com.docker.compose.service':'core'}}}
        with patch.object(node,'command',side_effect=['foreign',json.dumps([foreign])]) as command:
            with self.assertRaisesRegex(ValueError,'foreign container'):node.stop()
            self.assertEqual(command.call_count,2)

    def test_wrong_run_or_machine_refuses_before_any_operation(self):
        with patch.object(node,'command') as command:
            with self.assertRaisesRegex(ValueError,'identity'):node.guard('foreign')
            command.assert_not_called()

    def test_live_or_foreign_data_cannot_be_removed(self):
        with patch.object(node,'owned_containers',return_value=[{'State':{'Running':True}}]), \
                patch.object(node.shutil,'rmtree') as remove:
            with self.assertRaisesRegex(ValueError,'stop'):node.clean_data()
            remove.assert_not_called()

    def test_archive_acknowledgement_is_exact_before_source_recovery_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state';state.mkdir();(state/'snapshot').write_bytes(b'original')
            with patch.object(node,'STATE',state),patch.object(node,'DATA',root/'absent'), \
                    patch.object(node,'command',return_value='inactive'),patch.object(node.shutil,'rmtree') as remove:
                with self.assertRaisesRegex(ValueError,'archive differs'):node.clean_state({'snapshot':'0'*64})
                remove.assert_not_called()

    def test_verified_recovery_archive_is_hashed_in_bounded_chunks_before_removal(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state';state.mkdir()
            content=b'original recovery'*(128*1024)
            (state/'snapshot').write_bytes(content)
            with patch.object(node,'STATE',state),patch.object(node,'DATA',root/'absent'), \
                    patch.object(node,'command',return_value='inactive'), \
                    patch.object(Path,'read_bytes',side_effect=AssertionError('unbounded archive read')):
                result=node.clean_state({'snapshot':hashlib.sha256(content).hexdigest()})
            self.assertFalse(state.exists())
            self.assertEqual(result['marker'],'HARBOR_NODE_CLEAN_VERIFY_PASS')

    def test_logical_export_inventory_and_same_host_transfer_verify_real_bytes(self):
        import hashlib
        from upgrade.lab.harbor_fixture import private_json
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);backup=root/'backup-portable216';backup.mkdir(mode=0o700)
            (backup/'data.tar').write_bytes(b'stopped original data');(backup/'data.tar').chmod(0o600)
            (backup/'registry.dump').write_bytes(b'PGDMP original');(backup/'registry.dump').chmod(0o600)
            files={n:hashlib.sha256((backup/n).read_bytes()).hexdigest() for n in ['data.tar','registry.dump']}
            private_json(backup/'portable.json',{'files':files})
            with patch.object(node,'STATE',root),patch.object(node,'owned_containers',return_value=[]):
                self.assertEqual(set(node.export_files('backup-portable216')),set(files)|{'portable.json'})
                node.transfer_operation('transfer-directory',{'label':'transfer-216'})
                node.transfer_operation('link-transfer',{'label':'transfer-216','backup':'backup-portable216'})
                self.assertTrue(os.path.samefile(backup/'data.tar',root/'transfer-216/data.tar'))
                (backup/'registry.dump').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'bytes changed'):node.export_files('backup-portable216')
                with self.assertRaisesRegex(ValueError,'identity'):node.transfer_path('../foreign')

    def test_transfer_verification_identity_is_exact_and_existing_identity_is_not_adopted(self):
        from upgrade.lab.harbor_fixture import private_json
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'transfer-213').mkdir(mode=0o700)
            private_json(root/'settings.json',{'original':'identity'})
            with patch.object(node,'STATE',root),patch.object(node,'settings') as configure:
                with self.assertRaisesRegex(ValueError,'active verification'):
                    node.transfer_operation('adopt-fixture',{'label':'transfer-213'})
                configure.assert_not_called()
                with self.assertRaisesRegex(ValueError,'original verification'):
                    node.transfer_operation('transfer-fixture',{'label':'transfer-213','files':{'foreign':'{}'}})

    def test_previous_restore_quarantine_retains_bytes_without_changing_original_receipt(self):
        from upgrade.lab.harbor_fixture import private_json
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);state=root/'state';state.mkdir();backup=state/'backup-original';backup.mkdir()
            data=root/'data';data.mkdir();quarantine=root/'data.before-restore-fixed';quarantine.mkdir()
            (quarantine/'confirmed').write_bytes(b'previous target bytes')
            private_json(backup/'receipt.json',{'phase':'restored','data_directory':str(data),'quarantine':str(quarantine)})
            original=(backup/'receipt.json').read_bytes()
            with patch.object(node,'STATE',state),patch.object(node,'DATA',data):node.retain_restore_quarantine()
            self.assertEqual((state/'retained-restored-target/confirmed').read_bytes(),b'previous target bytes')
            self.assertEqual((backup/'receipt.json').read_bytes(),original)
            self.assertTrue(data.is_dir());self.assertFalse(quarantine.exists())


if __name__=='__main__':unittest.main()
