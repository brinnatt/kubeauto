"""Actual pre-upgrade refusal order and bounded fixture abort contracts."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml
from tests.unit.test_longhorn_upgrade_lifecycle import MODULE
from tests.unit.test_longhorn_v2_upgrade_prepare import MODULE as PREPARE, PreparationContracts
import importlib.util
_BACKUP_SPEC = importlib.util.spec_from_file_location('upgrade_existing_backups', Path(MODULE.__file__).with_name('longhorn-backups.py'))
BACKUPS = importlib.util.module_from_spec(_BACKUP_SPEC); _BACKUP_SPEC.loader.exec_module(BACKUPS)


class BeforeUpgradeRecovery(unittest.TestCase):
    def fixture(self, directory):
        source, target, preparation = PreparationContracts().fixture(directory)
        identities = lambda lab, _: {'cluster': lab.owner()['cluster_uid']}
        with patch.object(PREPARE.impl, 'retained_identity', side_effect=identities), patch.object(PREPARE, 'healthy'), redirect_stdout(io.StringIO()):
            preparation.preflight()
        pending = json.loads(preparation.path.read_text())
        pending.update(stage='old-ready', source_namespace_uid=source.owner()['namespace_uid'])
        preparation.save(pending)
        config = yaml.safe_load(pending['original_config']); config.update(longhorn_version=MODULE.OLD)
        (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config))
        (source.DIRECTORY/'yml/longhorn/input.json').write_text(json.dumps({'longhorn_version':MODULE.OLD}))
        store = SimpleNamespace(record=source.DIRECTORY/'longhorn-backup-store.json', receipt=Mock())
        consumer = SimpleNamespace(record=target.DIRECTORY/'longhorn-dr-backup-consumer.json', receipt=Mock())
        for holder in (store, consumer):
            holder.record.write_text('{}')
            holder.cleanup = Mock(side_effect=lambda holder=holder: holder.record.unlink(missing_ok=True))
        source.backup_store = Mock(return_value=store)
        scenario = MODULE.Upgrade(source, target, SimpleNamespace(BACKUP_RESOURCES=()), SimpleNamespace(Consumer=lambda *a: consumer), 21, 22)
        scenario.workloads = {}
        for name in ('plain', 'encrypted'):
            path=source.DIRECTORY/('longhorn-upgrade-business-'+name+'.json'); path.write_text('{}')
            workload=SimpleNamespace(path=path, identities=Mock(), journal=lambda: {'namespace':'business','client':'writer','objects':{'Pod/writer':'writer-uid'}}, data=Mock())
            workload.cleanup=Mock(side_effect=lambda path=path:path.unlink(missing_ok=True))
            scenario.workloads[name]=workload
        row={'id':'original-attempt','stage':'source-1.12.0','source_identity':identities(source,{}),
            'target_identity':identities(target,{}),'old_runtime':{'version':MODULE.OLD},'points':{},
            'payloads':{'plain':'plain-data','encrypted':'encrypted-data'},
            'attempt':{'changes':{'longhorn_version':MODULE.NEW},'expected_refusal':'missing backup','control_before':{'original':'control'}}}
        scenario.row=row;scenario.save()
        original_get=source.get.side_effect
        source.get.side_effect=lambda kind,*a,**kw: {'metadata':{'uid':'writer-uid'}} if kind=='pod' else original_get(kind,*a,**kw)
        return source,target,preparation,scenario

    def guards(self, stack):
        stack.enter_context(patch.object(MODULE,'load',return_value=PREPARE))
        stack.enter_context(patch.object(MODULE,'retained_identity',side_effect=lambda lab,_:{'cluster':lab.owner()['cluster_uid']}))
        stack.enter_context(patch.object(PREPARE.impl,'retained_identity',side_effect=lambda lab,_:{'cluster':lab.owner()['cluster_uid']}))
        stack.enter_context(patch.object(MODULE,'runtime_identity',return_value={'version':MODULE.OLD}))
        stack.enter_context(patch.object(MODULE,'control_identity',return_value={'original':'control'}))
        stack.enter_context(patch.object(PREPARE,'healthy'))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_real_abort_guard_reads_old_sources_before_any_fixture_mutation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source,target,preparation,s=self.fixture(directory);self.guards(stack)
            s.abort_preflight()
            for name,w in s.workloads.items():
                w.identities.assert_called_once();w.data.assert_called_once_with(name+'-data',read=True);w.cleanup.assert_not_called()
            s.store.receipt.assert_called_once();s.consumer.receipt.assert_called_once()
            source.recover_installation.assert_not_called();source.supervise_core=Mock()

    def test_actual_rollout_identity_configuration_raw_disk_and_writer_changes_refuse_before_cleanup(self):
        for defect in ('stage','new_runtime','legacy','retained_before','successful_configuration','actual-rollout','wrong-target-version','source-config','namespace','raw','control','runtime','writer','lost-source'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                source,target,preparation,s=self.fixture(directory);self.guards(stack)
                row=json.loads(s.path.read_text())
                if defect=='stage':row['stage']='upgrade-starting'
                elif defect in ('new_runtime','legacy','retained_before','successful_configuration'):row[defect]={}
                elif defect=='actual-rollout':row['attempt']['expected_refusal']=None
                elif defect=='wrong-target-version':(target.DIRECTORY/'config.yml').write_text('longhorn_version: 1.12.0')
                elif defect=='source-config':(source.DIRECTORY/'config.yml').write_text('longhorn_version: 1.12.1')
                elif defect=='namespace':source.owner.return_value['namespace_uid']='replaced'
                elif defect=='raw':
                    path=source.DIRECTORY/'yml/longhorn/block-disks.json';raw=json.loads(path.read_text());raw['nodes'][source.NODES[0]]['disks']['raw']['disk_uuid']='foreign';path.write_text(json.dumps(raw))
                elif defect=='control':row['attempt']['control_before']={'replaced':'control'}
                elif defect=='runtime':row['old_runtime']={'version':MODULE.NEW}
                elif defect=='writer':source.get.side_effect=lambda *a,**kw:{'metadata':{'uid':'replacement'}}
                elif defect=='lost-source':s.workloads['plain'].path.unlink()
                s.path.write_text(json.dumps(row))
                with self.assertRaises((ValueError,KeyError)):s.cleanup(abort_before_upgrade=True)
                s.store.cleanup.assert_not_called();s.consumer.cleanup.assert_not_called()
                for w in s.workloads.values():w.cleanup.assert_not_called()
                source.recover_installation.assert_not_called()

    def test_abort_reuses_native_cleanup_archives_abort_and_preserves_healthy_old_preparation(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            source,target,preparation,s=self.fixture(directory);self.guards(stack)
            before=preparation.path.read_bytes();s.retire_caches=Mock()
            # The real cleanup checks owned native source/cache removal and
            # retained identity; final empty-state checks must see no fixtures.
            s.cleanup(abort_before_upgrade=True)
            self.assertFalse(s.path.exists());self.assertEqual(before,preparation.path.read_bytes())
            result=json.loads((source.DIRECTORY/'longhorn-upgrade-aborted-original-attempt.json').read_text())
            self.assertEqual('aborted-before-upgrade',result['stage'])
            self.assertFalse(list(source.DIRECTORY.glob('longhorn-upgrade-result-*')))
            for w in s.workloads.values():w.cleanup.assert_called_once()
            s.store.cleanup.assert_called_once();s.consumer.cleanup.assert_called_once()
            source.recover_installation.assert_not_called();source.prepare_credentials.assert_not_called()

    def test_real_product_refusal_order_detaches_before_missing_backup_then_reads_attached_source(self):
        source=(Path(MODULE.__file__)).read_text()
        # Execute the real business method from system-backup through legacy
        # handoff; fake no success markers or live identity acceptance.
        start=source.index('        self.system_backup(configuration)\n',source.index('    def business(self):'))
        end=source.index('        self.wait_detached(); self.prepare_legacy()',start)+len('        self.wait_detached(); self.prepare_legacy()')
        code='def refusal_sequence(self, source, configuration):\n'+source[start:end]+'\n'
        scope={'NEW':MODULE.NEW,'NS':MODULE.NS};exec(code,scope)
        calls=[]
        workloads={name:SimpleNamespace(detach=lambda name=name:calls.append(('detach',name)),mount=lambda *a:calls.append(('mount',*a)),data=lambda *a,**kw:calls.append(('read',a,kw))) for name in ('plain','encrypted')}
        s=SimpleNamespace(row={'id':'attempt','payloads':{'plain':'data'},'system':{'name':'bundle'}},workloads=workloads,
            system_backup=lambda _:calls.append(('system',)),wait_detached=lambda:calls.append(('detached',)),
            prepare_legacy=lambda:calls.append(('legacy',)),product=lambda changes,**kw:calls.append(('product',kw['refusal'])))
        scope['refusal_sequence'](s,SimpleNamespace(get=lambda *a:None),{})
        self.assertEqual([('system',),('detach','plain'),('detach','encrypted'),('detached',)],calls[:4])
        self.assertEqual(('product','Longhorn upgrade system backup is not Ready for the source version'),calls[4])
        self.assertEqual(('mount','attached-upgrade-refusal',0),calls[5]);self.assertEqual('read',calls[6][0])
        self.assertEqual(('product','V2 resource-setting changes require all V2 volumes detached: data-engine-number-of-cpu-cores'),calls[7])
        self.assertEqual([('detach','plain'),('detach','encrypted'),('detached',),('legacy',)],calls[8:])



class PostUpgradeRecovery(unittest.TestCase):
    def fixture(self, directory):
        source,target,preparation,s=BeforeUpgradeRecovery().fixture(directory)
        source.write_private=lambda path,row:path.write_text(json.dumps(row,sort_keys=True))
        row=s.row
        row.update(stage='upgrade-starting',new_runtime={'version':MODULE.NEW},retained_before=row['source_identity'],
            legacy={'created_version':MODULE.OLD},system={'name':'system-backup'})
        row['points']={key:{'sha256':'a'*64} for key in ('plain-before','encrypted-before')}
        row['restores']={key:{'volume_uid':key,'remount':True,'confirmed_sha256':'a'*64} for key in row['points']}
        row['attempt']={'expected_refusal':None,'changes':{'longhorn_version':MODULE.NEW,
            'longhorn_upgrade_id':'upgrade-'+row['id'],'longhorn_upgrade_system_backup':'system-backup'}}
        row['successful_configuration']={'longhorn_version':MODULE.NEW}
        (source.DIRECTORY/'config.yml').write_text(yaml.safe_dump(row['successful_configuration']))
        def journal(name):return {'volume':name,'volume_uid':name+'-uid','namespace':'business','objects':{'Pod/writer':'writer-uid'}}
        for name,w in s.workloads.items():w.journal=lambda name=name:journal(name)
        source.get.side_effect=lambda kind,name='',*a,**kw: None if kind=='pod' else {
            'metadata':{'uid':name+'-uid'},'status':{'state':'detached'},'spec':{}}
        data = {'AWS_ACCESS_KEY_ID': 'fixture-key', 'AWS_SECRET_ACCESS_KEY': 'fixture-value'}
        import hashlib
        binding = {'target_uid': 'target-uid', 'namespace_uid': source.owner()['namespace_uid'],
            'secret_name': 'backup-secret', 'secret_uid': 'secret-uid',
            'original_spec': {'backupTargetURL': '', 'credentialSecret': '', 'pollInterval': '5m0s'},
            'fixture_spec': {'backupTargetURL': 's3://fixture@us-east-1/', 'credentialSecret': 'backup-secret', 'pollInterval': '5m0s'},
            'secret_digest': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()}
        s.backups = BACKUPS
        s.store.receipt.return_value = {'run_id': source.owner()['run_id'], 'product_binding': binding}
        s.store.product_configuration = Mock(side_effect=lambda: BACKUPS.Store.product_configuration(s.store))
        original_get = source.get.side_effect
        def get(kind, name='', *args, **kw):
            if kind == 'backuptargets.longhorn.io':
                return {'metadata': {'uid': 'target-uid'}, 'spec': binding['fixture_spec'], 'status': {'available': True}}
            if kind == 'namespace': return {'metadata': {'uid': source.owner()['namespace_uid']}}
            if kind == 'secret':
                return {'metadata': {'uid': 'secret-uid', 'labels': {'kubeauto.io/longhorn-backup-run': source.owner()['run_id']}}, 'data': data}
            return original_get(kind, name, *args, **kw)
        source.get.side_effect = get
        s.save();return source,target,preparation,s

    def guards(self, stack, preparation):
        stack.enter_context(patch.object(MODULE,'load',return_value=PREPARE))
        stack.enter_context(patch.object(MODULE,'runtime_identity',return_value={'version':MODULE.NEW}))
        stack.enter_context(patch.object(MODULE,'retained_identity',side_effect=lambda lab,_:{'cluster':lab.owner()['cluster_uid']}))
        stack.enter_context(patch.object(PREPARE,'target_identity',return_value=json.loads(preparation.path.read_text())['target_identity']))
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_read_only_checkpoint_admission_and_resume_never_repeat_setup_or_old_restore_points(self):
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            source,target,preparation,s=self.fixture(directory);self.guards(stack,preparation)
            original=s.path.read_bytes();s.resume_preflight();self.assertEqual(original,s.path.read_bytes())
            s.finish_business=Mock();s.resume()
            import hashlib
            self.assertEqual(hashlib.sha256(original).hexdigest(),s.row['resume_from_journal_sha256'])
            s.finish_business.assert_called_once_with({'longhorn_backup_target':'s3://fixture@us-east-1/', 'longhorn_backup_secret':'backup-secret'});s.store.product_configuration.assert_not_called();source.recover_installation.assert_not_called();source.prepare_credentials.assert_not_called()
            for w in s.workloads.values():w.cleanup.assert_not_called();w.data.assert_not_called()

    def test_real_existing_binding_handoff_runs_actual_tail_without_second_initialization(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            source,target,preparation,s=self.fixture(directory);self.guards(stack,preparation)
            with self.assertRaisesRegex(ValueError, 'previous backup product binding'):
                s.store.product_configuration()
            s.store.product_configuration.reset_mock()
            s.row['old_encrypted_device'] = {'luks_uuid':'luks', 'mapped_bytes':100, 'raw_bytes':200}
            s.save();s.verify_legacy_upgrade=Mock();s.retire_legacy=Mock();source.WORKERS=source.NODES
            source.load_helper=Mock(return_value=SimpleNamespace(Probe=lambda *a,**kw:SimpleNamespace(
                host_probe=lambda *a:dict(s.row['old_encrypted_device']))))
            for w in s.workloads.values():w.mount=Mock()
            s.protect=Mock(side_effect=lambda name,label,configuration:name+'-'+label)
            s.independent_restore=Mock();s.product=Mock()
            original=s.path.read_bytes()
            import hashlib
            s.row['resume_from_journal_sha256']=hashlib.sha256(original).hexdigest();s.save()
            s.resume()  # Same journal plus handoff only is the failed no-business checkpoint.
            self.assertEqual('business-pass',s.row['stage']);s.store.product_configuration.assert_not_called()
            configuration={'longhorn_backup_target':'s3://fixture@us-east-1/', 'longhorn_backup_secret':'backup-secret'}
            self.assertEqual([(('plain','after',configuration),{}),(('encrypted','after',configuration),{})],s.protect.call_args_list)
            self.assertEqual([(('plain-after',),{}),(('encrypted-after',),{})],s.independent_restore.call_args_list)
            s.product.assert_called_once_with({'longhorn_version':MODULE.OLD},refusal='unsupported Longhorn upgrade or direct downgrade')

    def test_changed_existing_backup_binding_refuses_without_recording_handoff(self):
        for defect in ('target','namespace','secret-uid','secret-data','target-spec','unavailable','handoff-business-change'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                source,target,preparation,s=self.fixture(directory);self.guards(stack,preparation)
                previous=source.get.side_effect
                def get(kind,*a,**kw):
                    obj=copy.deepcopy(previous(kind,*a,**kw))
                    if kind=='backuptargets.longhorn.io':
                        if defect=='target':obj['metadata']['uid']='foreign'
                        if defect=='target-spec':obj['spec']['backupTargetURL']='s3://foreign@us-east-1/'
                        if defect=='unavailable':obj['status']['available']=False
                    if kind=='namespace' and defect=='namespace':obj['metadata']['uid']='foreign'
                    if kind=='secret':
                        if defect=='secret-uid':obj['metadata']['uid']='foreign'
                        if defect=='secret-data':obj['data']['AWS_SECRET_ACCESS_KEY']='changed'
                    return obj
                source.get.side_effect=get
                if defect=='handoff-business-change':
                    import hashlib
                    s.row['resume_from_journal_sha256']=hashlib.sha256(s.path.read_bytes()).hexdigest()
                    s.row['legacy']['expansion_request']={'size':'changed'};s.save()
                original=s.path.read_bytes();s.finish_business=Mock()
                with self.assertRaises(ValueError):s.resume()
                self.assertEqual(original,s.path.read_bytes());s.finish_business.assert_not_called()
                s.store.product_configuration.assert_not_called()

    def test_changed_or_incomplete_rollout_checkpoint_refuses_before_any_operation(self):
        for defect in ('stage','already-resumed','legacy-passed','before-missing','restore-incomplete','wrong-attempt','config','runtime','disk','source-attached','foreign-volume','writer','target-volume'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                source,target,preparation,s=self.fixture(directory);self.guards(stack,preparation)
                row=s.row
                if defect=='stage':row['stage']='upgraded-1.12.1'
                elif defect=='already-resumed':row['resume_from_journal_sha256']='a'*64
                elif defect=='legacy-passed':row['legacy']['refusal_verified']=True
                elif defect=='before-missing':row['points'].pop('plain-before')
                elif defect=='restore-incomplete':row['restores']['plain-before']['remount']=False
                elif defect=='wrong-attempt':row['attempt']['changes']['longhorn_upgrade_id']='foreign'
                elif defect=='config':(source.DIRECTORY/'config.yml').write_text('longhorn_version: 1.12.0')
                elif defect=='runtime':row['new_runtime']={'version':MODULE.OLD}
                elif defect=='disk':
                    p=source.DIRECTORY/'yml/longhorn/block-disks.json';r=json.loads(p.read_text());r['nodes'][source.NODES[0]]['disks']['raw']['disk_uuid']='foreign';p.write_text(json.dumps(r))
                elif defect in ('source-attached','foreign-volume','writer'):
                    source.get.side_effect=lambda kind,name='',*a,defect=defect,**kw: ({'metadata':{'uid':'writer'}} if defect=='writer' else None) if kind=='pod' else {
                        'metadata':{'uid':'foreign' if defect=='foreign-volume' else name+'-uid'},'status':{'state':'attached' if defect=='source-attached' else 'detached'},'spec':{}}
                elif defect=='target-volume':target.get.side_effect=lambda *a,**kw:{'items':[{'metadata':{'uid':'foreign'}}]}
                s.save();original=s.path.read_bytes();s.finish_business=Mock()
                with self.assertRaises((ValueError,KeyError)):s.resume()
                self.assertEqual(original,s.path.read_bytes());s.finish_business.assert_not_called()
                source.recover_installation.assert_not_called()


if __name__=='__main__':unittest.main()
