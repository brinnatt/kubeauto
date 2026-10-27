"""Product credential reference, private transport and interrupted recovery."""
import base64
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recurring_credentials',
    ROOT / 'tests/helpers/longhorn-recurring-rotation.py')
rotation = importlib.util.module_from_spec(spec); spec.loader.exec_module(rotation)


class RecurringCredentialContracts(unittest.TestCase):
    def test_scheduled_refusal_requires_new_owned_backup_error_and_rejects_any_false_completion(self):
        original={'metadata':{'uid':'new-backup'},'status':{'state':'Error','progress':0,
            'error':'InvalidAccessKeyId: The AWS Access Key Id does not exist'}}
        self.assertEqual(rotation.credential_failure({'backups':[original]},set()),[original])
        self.assertIsNone(rotation.credential_failure({'backups':[original]},{'new-backup'}))
        self.assertIsNone(rotation.credential_failure({'backups':[]},set()))
        for state,error in (('Error','connection refused'),('Unknown','InvalidAccessKeyId'),
                            ('Error','prefixInvalidAccessKeyIdSuffix'),('Error',None),
                            ('Completed','InvalidAccessKeyId')):
            value=copy.deepcopy(original);value['status'].update(state=state,error=error)
            with self.subTest(state=state,error=error),self.assertRaises(ValueError):
                rotation.credential_failure({'backups':[value]},set())
        for key,value in (('progress',100),('backupCreatedAt','2026-10-19T00:00:00Z')):
            backup=copy.deepcopy(original);backup['status'][key]=value
            with self.assertRaisesRegex(ValueError,'completed backup'):
                rotation.credential_failure({'backups':[original,backup]},set())

    def fixture(self, directory):
        root = Path(directory)
        data = {key: base64.b64encode(value.encode()).decode() for key,value in
                {'AWS_ACCESS_KEY_ID':'root-user','AWS_SECRET_ACCESS_KEY':'root-private',
                 'AWS_CERT':'private-ca','AWS_ENDPOINTS':'https://approved-endpoint'}.items()}
        objects = {('namespace', 'longhorn-system'): {'metadata': {'uid':'namespace-uid'}},
            ('secret','original-secret'): {'kind':'Secret','metadata':{'name':'original-secret','uid':'root-uid'},'data':data},
            ('backuptargets.longhorn.io','default'): {'metadata':{'uid':'target-uid'},
                'spec':{'backupTargetURL':'s3://owned-bucket@us-east-1/','credentialSecret':'original-secret',
                        'pollInterval':'5m0s','syncRequestedAt':'mutable'},'status':{'available':True}}}
        target = objects[('backuptargets.longhorn.io','default')]
        binding = {'target_uid':'target-uid','namespace_uid':'namespace-uid','secret_name':'original-secret',
            'secret_uid':'root-uid','secret_digest':rotation.data_digest(data),
            'original_spec':dict(target['spec'],backupTargetURL='',credentialSecret=''),
            'fixture_spec':copy.deepcopy(target['spec'])}
        store_row = {'run_id':'run','cluster_uid':'cluster','container_id':'container','product_binding':binding}
        accounts = {}; count = 0
        def create_account():
            nonlocal count
            count += 1; identifier='lh'+format(count,'018x')
            accounts[identifier]={'phase':'ready','secret':'SyntheticSecret+12/34='}
            return identifier
        clients = SimpleNamespace(initialize=Mock(),context=Mock(),create=Mock(side_effect=create_account),
            read=Mock(side_effect=lambda:{'accounts':copy.deepcopy(accounts)}),verify=Mock(),cleanup=Mock())
        def private(path,value):
            path.write_text(json.dumps(value)); path.chmod(0o600)
        def create_secret(*argv, stdin):
            self.assertEqual(argv,('create','-f','-','-o','json'))
            value=json.loads(stdin);name=value['metadata']['name']
            if ('secret',name) in objects: raise ValueError('already exists')
            value['metadata']['uid']='secret-uid-'+name
            objects[('secret',name)]=copy.deepcopy(value)
            return SimpleNamespace(stdout=json.dumps(value))
        def remove(obj):
            name=obj['metadata']['name']
            self.assertEqual(objects[('secret',name)]['metadata']['uid'],obj['metadata']['uid'])
            del objects[('secret',name)]
        schedule_row={'name':'owned-plan','uid':'schedule-uid','spec':{'cron':'* * * * *'},
                      'backup_configuration':{'longhorn_backup_target':binding['fixture_spec']['backupTargetURL'],
                                              'longhorn_backup_secret':'original-secret'}}
        schedule=SimpleNamespace(record=root/'longhorn-recurring-runtime.json',
            read=Mock(side_effect=lambda:copy.deepcopy(schedule_row)),
            save=Mock(side_effect=lambda value:schedule_row.update(copy.deepcopy(value))),
            declaration=Mock(side_effect=lambda row,cron:{'name':row['name'],'cron':cron}))
        schedule.record.write_text('{}')
        def product(_pid, requests, **options):
            self.assertEqual(requests,[])
            configuration=options['backup_configuration']
            target['spec']['credentialSecret']=configuration['longhorn_backup_secret']
            target['spec']['backupTargetURL']=configuration['longhorn_backup_target']
        helpers={'longhorn-accounts.py':SimpleNamespace(Accounts=lambda *_args:clients),
            'longhorn-backups.py':SimpleNamespace(target_configuration=lambda value:
                {key:item for key,item in value.items() if key!='syncRequestedAt'}),
            'longhorn-recurring.py':SimpleNamespace(Schedule=lambda _lab:schedule)}
        lab=SimpleNamespace(DIRECTORY=root,owner=Mock(return_value={'run_id':'run','cluster_uid':'cluster'}),
            write_private=private,load_helper=Mock(side_effect=lambda name:helpers[name]),
            get=Mock(side_effect=lambda kind,name,namespace=None:copy.deepcopy(objects.get((kind,name)))),
            kubectl=Mock(side_effect=create_secret),remove=Mock(side_effect=remove),
            run_requests=Mock(side_effect=product),restore_request_input=Mock(),
            wait=Mock(side_effect=lambda _label,_timeout,check:self.assertTrue(check())))
        fixture=rotation.Credentials(lab,SimpleNamespace(receipt=Mock(return_value=store_row), recover_transport=Mock()))
        fixture.initialize()
        return fixture,lab,clients,objects,schedule,schedule_row

    def test_distinct_private_secrets_switch_only_through_product_and_restore_before_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture,lab,accounts,objects,schedule,row=self.fixture(directory)
            first,second=fixture.create(),fixture.create()
            a,b=fixture.configuration(first),fixture.configuration(second)
            self.assertNotEqual(a['longhorn_backup_secret'],b['longhorn_backup_secret'])
            fixture.select(123,schedule,first);fixture.select(123,schedule,second)
            self.assertEqual(row['backup_configuration'],b)
            lab.run_requests.assert_called_with(123,[],recurring_jobs=[{'name':'owned-plan','cron':'* * * * *'}],
                                                backup_configuration=b)
            with self.assertRaisesRegex(ValueError,'restored reference'):fixture.cleanup()
            with redirect_stdout(io.StringIO()) as output:fixture.restore_root(123)
            self.assertEqual(output.getvalue(),'LONGHORN_RECURRING_CREDENTIAL_REFERENCE_RESTORED\n')
            self.assertEqual(row['backup_configuration'],fixture.configuration())
            with self.assertRaisesRegex(ValueError,'stopped schedule'):fixture.cleanup()
            schedule.record.unlink()
            with redirect_stdout(io.StringIO()):fixture.cleanup()
            accounts.cleanup.assert_called_once()
            self.assertFalse(fixture.record.exists())
            self.assertEqual({name for kind,name in objects if kind=='secret'},{'original-secret'})
            for call in lab.kubectl.call_args_list:
                self.assertNotIn('SyntheticSecret',' '.join(call.args))
                request=json.loads(call.kwargs['stdin'])
                self.assertEqual(request['data']['AWS_CERT'],objects[('secret','original-secret')]['data']['AWS_CERT'])

    def test_interrupted_product_selection_retains_reference_then_restores_with_original_valid_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture,lab,_accounts,objects,schedule,row=self.fixture(directory)
            identifier=fixture.create();product=lab.run_requests.side_effect
            def lost_response(*args,**kwargs):
                product(*args,**kwargs);raise RuntimeError('lost product response')
            lab.run_requests.side_effect=lost_response
            with self.assertRaisesRegex(RuntimeError,'lost product response'):fixture.select(123,schedule,identifier)
            self.assertEqual(row['backup_configuration'],fixture.configuration(identifier))
            lab.run_requests.side_effect=product
            with redirect_stdout(io.StringIO()):fixture.restore_root(123)
            self.assertEqual(objects[('backuptargets.longhorn.io','default')]['spec']['credentialSecret'],'original-secret')
            lab.restore_request_input.assert_called_once()

    def test_replaced_or_modified_objects_refuse_before_product_or_cleanup(self):
        for changed in ('target','root','namespace','data','label','secret','reference','private-mode','symlink'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                fixture,lab,accounts,objects,_schedule,_row=self.fixture(directory)
                identifier=fixture.create();name=fixture.configuration(identifier)['longhorn_backup_secret']
                if changed=='target':objects[('backuptargets.longhorn.io','default')]['metadata']['uid']='foreign'
                elif changed=='root':objects[('secret','original-secret')]['metadata']['uid']='foreign'
                elif changed=='namespace':objects[('namespace','longhorn-system')]['metadata']['uid']='foreign'
                elif changed=='data':objects[('secret',name)]['data']['AWS_SECRET_ACCESS_KEY']='foreign'
                elif changed=='label':objects[('secret',name)]['metadata']['labels'].clear()
                elif changed=='secret':objects[('secret',name)]['metadata']['uid']='foreign'
                elif changed=='reference':objects[('backuptargets.longhorn.io','default')]['spec']['credentialSecret']='foreign'
                elif changed=='private-mode':fixture.record.chmod(0o644)
                elif changed=='symlink':
                    original=fixture.record.with_suffix('.original');fixture.record.rename(original);fixture.record.symlink_to(original)
                lab.run_requests.reset_mock();lab.remove.reset_mock()
                with self.assertRaises(ValueError):fixture.restore_root(123)
                with self.assertRaises(ValueError):fixture.cleanup()
                lab.run_requests.assert_not_called();lab.remove.assert_not_called();accounts.cleanup.assert_not_called()

    def test_lost_secret_creation_response_never_adopts_same_name_from_current_api(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture,lab,accounts,_objects,_schedule,_row=self.fixture(directory)
            original=lab.kubectl.side_effect
            def lose(*args,**kwargs):
                original(*args,**kwargs);raise RuntimeError('lost secret creation response')
            lab.kubectl.side_effect=lose
            with self.assertRaisesRegex(RuntimeError,'lost secret'):fixture.create()
            self.assertEqual(fixture.record.stat().st_mode & 0o777,0o600)
            with self.assertRaisesRegex(ValueError,'identity or data'):fixture.cleanup()
            lab.remove.assert_not_called();accounts.cleanup.assert_not_called()

    def test_pending_schedule_update_blocks_unproven_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture,lab,_accounts,_objects,_schedule,row=self.fixture(directory)
            row['pending_spec']={'cron':'0 0 1 1 *'}
            with self.assertRaisesRegex(ValueError,'original recovery'):fixture.restore_root(123)
            lab.run_requests.assert_not_called()
            fixture.store.recover_transport.assert_not_called()

    def test_generic_account_and_store_cleanup_cannot_bypass_credential_reference_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'longhorn-recurring-credentials.json').write_text('{}')
            for filename,classname in (('longhorn-accounts.py','Accounts'),('longhorn-backups.py','Store')):
                spec=importlib.util.spec_from_file_location('guarded_fixture',ROOT/'tests/helpers'/filename)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                fixture=SimpleNamespace(lab=SimpleNamespace(DIRECTORY=root),record=root/'fixture.json',
                                        restore_product=Mock())
                with self.assertRaisesRegex(ValueError,'references must recover'):
                    getattr(module,classname).cleanup(fixture)
                fixture.restore_product.assert_not_called()

    def test_business_cleanup_recovers_valid_reference_before_stopping_jobs_and_removing_secrets(self):
        spec=importlib.util.spec_from_file_location('credential_cleanup_lab',ROOT/'tests/helpers/longhorn-lab.py')
        lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'longhorn-recurring-credentials.json').write_text('{}')
            schedule_record=root/'longhorn-recurring-runtime.json';schedule_record.write_text('{}')
            for interrupted in (False,True):
                events=[]
                def restore(_pid):
                    events.append('restore')
                    if interrupted:raise RuntimeError('recovery refused')
                credentials=SimpleNamespace(restore_root=Mock(side_effect=restore),
                    cleanup=Mock(side_effect=lambda:events.append('secrets-and-accounts')))
                schedule=SimpleNamespace(cleanup=Mock(side_effect=lambda _pid:events.append('stop-jobs')))
                helpers={'longhorn-recurring-rotation.py':SimpleNamespace(Credentials=lambda *_args:credentials),
                         'longhorn-recurring.py':SimpleNamespace(Schedule=lambda *_args:schedule)}
                with patch.object(lab,'DIRECTORY',root),patch.object(lab,'owner',return_value={}), \
                        patch.object(lab,'load_helper',side_effect=lambda name:helpers[name]), \
                        patch.object(lab,'backup_store'),patch.object(lab,'restore_request_input'), \
                        patch.object(lab,'cleanup_reclaim_class'),patch.dict('sys.modules',{'credential_cleanup_lab':lab}):
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError,'recovery refused'):lab.cleanup_business(relay_pid=123)
                        self.assertEqual(events,['restore']);schedule.cleanup.assert_not_called();credentials.cleanup.assert_not_called()
                    else:
                        lab.cleanup_business(relay_pid=123)
                        self.assertEqual(events,['restore','stop-jobs','secrets-and-accounts'])


if __name__=='__main__':unittest.main()
