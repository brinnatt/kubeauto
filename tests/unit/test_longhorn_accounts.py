"""Account policy, lost-response recovery and private transport contracts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_accounts', ROOT / 'tests/helpers/longhorn-accounts.py')
accounts = importlib.util.module_from_spec(spec); spec.loader.exec_module(accounts)


class AccountContracts(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory)
        root = path / 'server.env'
        root.write_text('MINIO_ROOT_USER=root-user\nMINIO_ROOT_PASSWORD=root-private\n'); root.chmod(0o600)
        (path / 'mc.env').write_text('private root environment\n')
        state = {'run_id': 'run-id', 'cluster_uid': 'cluster-uid', 'bucket': 'longhorn-backup-' + 'a'*16}
        buckets = {state['bucket']}
        container = {'Id': 'container-id', 'State': {'Running': True}}
        server, observations, objects = {}, [], {}
        def write_private(filename, value):
            filename.write_text(json.dumps(value)); filename.chmod(0o600)
        def execute(argv, *, stdin=None, **_kwargs):
            observations.append((list(argv), stdin))
            if 'mc' not in argv:
                raise AssertionError('account commands must not create temporary container files')
            args = argv[argv.index('mc') + 2:]
            if args[:2] == ['pipe', '--json']:
                args = [args[0], *args[2:]]
            environment = Path(argv[argv.index('--env-file')+1])
            if environment.name != 'mc.env':
                self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
                self.assertIn('serverGeneratedPrivateKey12345', environment.read_text())
                key = re.search(r'https://([^:]+):', environment.read_text())[1]
                error = None
                if key not in server: error = 'InvalidAccessKeyId'
                elif args == ['admin', 'user', 'list', 'lh/']: error = 'AccessDenied'
                elif len(args) > 1 and args[1].startswith('lh/') and args[1] != 'lh/' and args[1].split('/')[1] != state['bucket']:
                    error = 'AccessDenied'
                elif args == ['ls', 'lh/']:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'success',
                        'type': 'folder', 'key': state['bucket'] + '/'}), stderr='')
                elif args[0] == 'pipe': objects[args[1]] = stdin
                elif args[0] == 'cat':
                    return SimpleNamespace(returncode=0, stdout=objects[args[1]], stderr='')
                elif args[0] == 'rm': del objects[args[1]]
                elif args[0] == 'stat' and args[1] not in objects: error = 'NoSuchKey'
                elif args[0] == 'ls':
                    return SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps({'status': 'success',
                        'type': 'file', 'key': key.removeprefix(args[1])})
                        for key in objects if key.startswith(args[1])), stderr='')
                else: raise AssertionError('unexpected restricted command')
                if error:
                    cause = {'error': {'Code': error}}
                    message = ''
                    if error == 'AccessDenied' and args[0] == 'cat':
                        path = 'https://127.0.0.1:9000/' + args[1][3:]
                        cause = {'message': 'Insufficient permissions to access this path `'+path+'`',
                                 'error': {'Path': path}}
                        message = 'Unable to read from `'+args[1]+'`.'
                    elif error == 'NoSuchKey' and args[0] == 'stat':
                        cause = {'message': 'Object does not exist', 'error': {}}
                        message = 'Unable to stat `'+args[1]+'`.'
                    return SimpleNamespace(returncode=1, stdout=json.dumps({'status': 'error',
                        'error': {'type': 'fatal', 'message': message, 'cause': cause}}), stderr='')
                return SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'success'}), stderr='')
            if args[:3] == ['admin', 'accesskey', 'list']:
                # Pinned MinIO omits users with no service accounts for
                # svcacc-only; mc therefore emits no JSON despite rc=0.
                if '--svcacc-only' in args and not server:
                    return SimpleNamespace(returncode=0, stdout='', stderr='')
                result = {'status': 'success', 'user': 'root-user',
                          'stsKeys': None,
                          'svcaccs': [{'accessKey': key} for key in server]}
            elif args[:3] == ['admin', 'accesskey', 'create']:
                key = args[args.index('--access-key') + 1]
                server[key] = {'status': 'success', 'accessKey': key, 'parentUser': 'root-user',
                    'name': args[args.index('--name')+1], 'description': args[args.index('--description')+1],
                    'policy': json.loads(stdin),
                    'accountStatus': 'on', 'impliedPolicy': False}
                result = {'status': 'success', 'accessKey': key, 'secretKey': 'server-generated-private-key'}
                # The real pinned mc uses a base64-compatible secret alphabet.
                result['secretKey'] = 'serverGeneratedPrivateKey12345'
            elif args[:3] == ['admin', 'accesskey', 'info']:
                result = copy.deepcopy(server[args[-1]])
            elif args[:3] == ['admin', 'accesskey', 'remove']:
                server.pop(args[-1])
                result = {'status': 'success', 'accessKey': args[-1]}
            elif args == ['ls', 'lh/']:
                return SimpleNamespace(returncode=0, stdout='\n'.join(json.dumps({'status': 'success',
                    'type': 'folder', 'key': bucket + '/'}) for bucket in sorted(buckets)), stderr='')
            elif args == ['admin', 'user', 'list', 'lh/']:
                result = {'status': 'success'}
            elif args[0] == 'mb':
                bucket = args[1].split('/')[1]
                self.assertNotIn(bucket, buckets)
                buckets.add(bucket); result = {'status': 'success'}
            elif args[0] == 'rb':
                self.assertFalse(any(key.startswith(args[1] + '/') for key in objects))
                buckets.remove(args[1].split('/')[1]); result = {'status': 'success'}
            elif args[0] == 'pipe':
                objects[args[1]] = stdin; result = {'status': 'success'}
            elif args[0] == 'cat':
                return SimpleNamespace(returncode=0, stdout=objects[args[1]], stderr='')
            elif args[0] == 'rm':
                del objects[args[1]]; result = {'status': 'success'}
            else:
                raise AssertionError('unexpected account command')
            return SimpleNamespace(returncode=0, stdout=json.dumps(result, indent=2)+'\n', stderr='')
        lab = SimpleNamespace(DIRECTORY=path, write_private=write_private, execute=Mock(side_effect=execute))
        store = SimpleNamespace(directory=path, receipt=Mock(side_effect=lambda: copy.deepcopy(state)),
                                inspect=Mock(side_effect=lambda _: copy.deepcopy(container)))
        fixture = accounts.Accounts(lab, store)
        fixture.initialize()
        return fixture, lab, store, state, container, server, observations

    def test_exact_bucket_policy_has_no_global_or_admin_permission(self):
        policy = accounts.bucket_policy('longhorn-backup-' + 'a'*16)
        self.assertEqual(len(policy['Statement']), 2)
        for statement in policy['Statement']:
            self.assertEqual(statement['Effect'], 'Allow')
            self.assertTrue(all(action.startswith('s3:') and '*' not in action for action in statement['Action']))
            self.assertNotIn('s3:ListAllMyBuckets', statement['Action'])
            self.assertTrue(all(resource.startswith('arn:aws:s3:::longhorn-backup-'+'a'*16)
                                for resource in statement['Resource']))
        for bucket in ('', 'another-bucket', 'longhorn-backup-'+'a'*16+'/*'):
            with self.subTest(bucket=bucket), self.assertRaises(ValueError): accounts.bucket_policy(bucket)

    def test_empty_native_account_set_preserves_authenticated_parent_record(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _, _, _, _, observations = self.fixture(directory)
            self.assertEqual(fixture.listing(), set())
            argv = observations[-1][0]
            self.assertIn('--self', argv)
            self.assertNotIn('--svcacc-only', argv)
            key = fixture.create()
            self.assertEqual(fixture.listing(), {key})
            fixture.revoke(key)
            self.assertEqual(fixture.listing(), set())

    def test_generated_account_identifier_respects_pinned_service_account_limit(self):
        # IAMSys.NewServiceAccount calls CreateNewCredentialsWithMetadata,
        # whose access key maximum is 20 even though IsAccessKeyValid only
        # enforces a minimum for other credential paths.
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, _ = self.fixture(directory)
            original = lab.execute.side_effect
            def validate_service_account(argv, **kwargs):
                if 'create' in argv and 'accesskey' in argv:
                    key = argv[argv.index('--access-key') + 1]
                    if not 3 <= len(key) <= 20:
                        raise ValueError('pinned service account access key length exceeds 20')
                return original(argv, **kwargs)
            lab.execute.side_effect = validate_service_account
            key = fixture.create()
            self.assertEqual(len(key), 20)
            fixture.cleanup()

    def test_failed_private_command_preserves_native_output_without_logging_material(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, _ = self.fixture(directory)
            raw = json.dumps({'status': 'error', 'error': {'message': 'Unable to add service account.',
                              'cause': {'message': 'private-key-and-signed-url'}}})
            lab.execute.return_value = SimpleNamespace(returncode=1, stdout=raw, stderr='private-stderr')
            lab.execute.side_effect = None
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(ValueError) as raised:
                fixture.command('admin', 'accesskey', 'create', 'lh/')
            self.assertEqual(output.getvalue(), '')
            self.assertIn('stage=create-request', str(raised.exception))
            self.assertNotIn('private-key-and-signed-url', str(raised.exception))
            diagnostic, = Path(directory).glob('longhorn-account-failure-*.json')
            self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(diagnostic.read_text()), {'rc': 1, 'stdout': raw, 'stderr': 'private-stderr'})
            for raw in ('private-key', '{}', '[]', json.dumps({'error': 'private-key'}),
                        json.dumps({'error': {'message': 'private-key'}})):
                self.assertEqual(accounts.failure_stage(raw), 'unclassified')

    def test_policy_normalization_accepts_order_and_singletons_but_rejects_extra_grants(self):
        original = accounts.bucket_policy('longhorn-backup-' + 'a'*16)
        changed = copy.deepcopy(original)
        changed['Statement'].reverse()
        for statement in changed['Statement']:
            statement['Sid'] = ''
            statement['Action'].reverse()
            statement['Resource'] = statement['Resource'][0]
        self.assertEqual(accounts.normalized_policy(original), accounts.normalized_policy(json.dumps(changed)))
        for mutation in ('condition', 'principal', 'deny', 'wildcard', 'extra', 'version'):
            changed = copy.deepcopy(original)
            if mutation == 'condition': changed['Statement'][0]['Condition'] = {}
            elif mutation == 'principal': changed['Statement'][0]['Principal'] = '*'
            elif mutation == 'deny': changed['Statement'][0]['Effect'] = 'Deny'
            elif mutation == 'wildcard': changed['Statement'][0]['Resource'] = '*'
            elif mutation == 'extra': changed['Statement'][0]['Action'].append('admin:*')
            else: changed['Version'] = 'unexpected'
            with self.subTest(mutation=mutation):
                try: normalized = accounts.normalized_policy(changed)
                except ValueError: continue
                self.assertNotEqual(normalized, accounts.normalized_policy(original))

    def test_indented_json_streams_are_supported_without_accepting_nonobjects(self):
        self.assertEqual(accounts.documents('{\n "status": "success"\n}\n {"next":true}\n'),
                         [{'status': 'success'}, {'next': True}])
        for raw in ('[]', 'null', 'true', '{}garbage'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, json.JSONDecodeError)):
                accounts.documents(raw)

    def test_create_and_cleanup_bind_identity_policy_and_keep_secrets_out_of_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, server, observations = self.fixture(directory)
            with redirect_stdout(io.StringIO()) as output:
                key = fixture.create()
                self.assertIn(key, server)
                self.assertEqual(fixture.record.stat().st_mode & 0o777, 0o600)
                self.assertEqual(fixture.read()['accounts'][key]['phase'], 'ready')
                fixture.cleanup()
            self.assertFalse(server)
            self.assertFalse(fixture.record.exists())
            self.assertEqual(output.getvalue(), 'LONGHORN_BACKUP_ACCOUNTS_CLEAN_PASS\n')
            for argv, _ in observations:
                self.assertNotIn('--secret-key', argv)
                self.assertNotIn('serverGeneratedPrivateKey12345', ' '.join(argv))
                self.assertNotIn('root-private', ' '.join(argv))
                self.assertNotIn('sh', argv)
                if 'create' in argv:
                    self.assertEqual(argv[argv.index('--policy')+1], '/dev/stdin')
                    self.assertIn('-i', argv)
            lab.execute.assert_called()

    def test_lost_creation_response_leaves_intent_that_can_revoke_exact_server_account(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, server, _ = self.fixture(directory)
            original = lab.execute.side_effect
            def lose_response(argv, **kwargs):
                result = original(argv, **kwargs)
                if 'create' in argv and 'accesskey' in argv:
                    raise RuntimeError('response lost after server commit')
                return result
            lab.execute.side_effect = lose_response
            with self.assertRaisesRegex(RuntimeError, 'response lost'): fixture.create()
            self.assertEqual(len(server), 1)
            self.assertEqual(next(iter(fixture.read()['accounts'].values()))['phase'], 'intent')
            lab.execute.side_effect = original
            fixture.cleanup()
            self.assertFalse(server)
            self.assertFalse(fixture.record.exists())

    def test_account_mismatch_blocks_revocation_and_preserves_material(self):
        for field, changed in (('name', 'foreign'), ('description', 'foreign'), ('parentUser', 'foreign'),
                               ('impliedPolicy', True), ('policy', {'Version': '2012-10-17', 'Statement': []}),
                               ('accountStatus', 'off')):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, _, _, server, _ = self.fixture(directory)
                key = fixture.create(); server[key][field] = changed
                lab.execute.reset_mock()
                with self.assertRaises(ValueError): fixture.cleanup()
                self.assertIn(key, server)
                self.assertTrue(fixture.record.exists())
                self.assertFalse(any('remove' in call.args[0] for call in lab.execute.call_args_list))

    def test_recreated_revoked_key_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, server, _ = self.fixture(directory)
            key = fixture.create(); saved = server[key]
            fixture.revoke(key); server[key] = saved
            lab.execute.reset_mock()
            with self.assertRaisesRegex(ValueError, 'recreated'): fixture.cleanup()
            self.assertFalse(any('remove' in call.args[0] for call in lab.execute.call_args_list))

    def test_replaced_store_and_journal_identity_reject_before_account_commands(self):
        for field in ('run', 'cluster', 'container', 'bucket', 'policy', 'mode', 'symlink'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, state, container, _, _ = self.fixture(directory)
                if field == 'run': state['run_id'] = 'foreign'
                elif field == 'cluster': state['cluster_uid'] = 'foreign'
                elif field == 'container': container['Id'] = 'foreign'
                elif field == 'bucket': state['bucket'] = 'longhorn-backup-' + 'b'*16
                elif field == 'policy':
                    row = json.loads(fixture.record.read_text()); row['policy']['Statement'][0]['Resource'] = '*'
                    lab.write_private(fixture.record, row)
                elif field == 'mode': fixture.record.chmod(0o644)
                elif field == 'symlink':
                    other = fixture.record.with_suffix('.original'); fixture.record.rename(other)
                    fixture.record.symlink_to(other)
                lab.execute.reset_mock()
                with self.assertRaises(ValueError): fixture.cleanup()
                lab.execute.assert_not_called()

    def test_private_client_environment_is_removed_on_success_and_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, _, _, _, _ = self.fixture(directory)
                key = fixture.create()
                def client(argv, **_kwargs):
                    path = Path(argv[argv.index('--env-file')+1])
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertIn('serverGeneratedPrivateKey12345', path.read_text())
                    self.assertNotIn('serverGeneratedPrivateKey12345', ' '.join(argv))
                    if failure: raise RuntimeError('client failed')
                    return SimpleNamespace(returncode=0, stdout='exact business bytes', stderr='')
                lab.execute.side_effect = client
                if failure:
                    with self.assertRaisesRegex(RuntimeError, 'client failed'):
                        fixture.command('cat', 'lh/bucket/object', account=key)
                else:
                    self.assertEqual(fixture.command('cat', 'lh/bucket/object', account=key).stdout, 'exact business bytes')
                self.assertFalse((Path(directory)/(key+'.env')).exists())

    def test_pinned_mc_env_alias_preserves_generated_secret_special_characters(self):
        for material in ('SyntheticKey+12345678', 'SyntheticKey/12345678', 'SyntheticKey=12345678'):
            with self.subTest(material=material), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, _, _, _, _ = self.fixture(directory)
                key = fixture.create(); row = fixture.read()
                row['accounts'][key]['secret'] = material; lab.write_private(fixture.record,row)
                def client(argv, **kwargs):
                    path = Path(argv[argv.index('--env-file')+1])
                    alias = dict(line.split('=',1) for line in path.read_text().splitlines())['MC_HOST_lh']
                    # config.go:parseEnvURLStr captures raw groups before
                    # url.Parse, so percent encoding changes the signing key.
                    parsed = re.fullmatch(r'(https?://)(.*?):(.*)@(.*?)',alias)
                    self.assertEqual(parsed[3],material)
                    self.assertNotIn(material,' '.join(argv))
                    return SimpleNamespace(returncode=0,stdout='confirmed',stderr='')
                lab.execute.side_effect = client
                self.assertEqual(fixture.command('cat','lh/bucket/object',account=key).stdout,'confirmed')
                self.assertFalse((Path(directory)/(key+'.env')).exists())

    def test_account_environment_rejects_secret_delimiters_before_creating_file_or_command(self):
        for material in ('SyntheticKey\nMC_HOST_other=bad','SyntheticKey@bad','SyntheticKey:token',None):
            with self.subTest(material=material), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, _, _, _, _ = self.fixture(directory)
                key = fixture.create(); row = fixture.read()
                row['accounts'][key]['secret'] = material; lab.write_private(fixture.record,row)
                lab.execute.reset_mock()
                with self.assertRaisesRegex(ValueError,'alphabet|not a ready'):
                    fixture.command('cat','lh/bucket/object',account=key)
                lab.execute.assert_not_called()
                self.assertFalse((Path(directory)/(key+'.env')).exists())

    def test_private_error_output_never_becomes_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, _ = self.fixture(directory)
            lab.execute.return_value = SimpleNamespace(returncode=1, stdout='private-output', stderr='private-secret')
            lab.execute.side_effect = None
            with self.assertRaises(ValueError) as caught: fixture.command('admin', 'user', 'list', 'lh/')
            self.assertIn('private account command failed rc=1 stage=unclassified evidence=', str(caught.exception))
            self.assertNotIn('private-output', str(caught.exception))
            self.assertNotIn('private-secret', str(caught.exception))

    def test_malformed_success_json_is_retained_privately_without_leaking_output(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _, _, _, _, _ = self.fixture(directory)
            result = SimpleNamespace(returncode=0, stdout='0 private-signed-url', stderr='private-secret')
            with self.assertRaisesRegex(ValueError, 'rc=0 stage=response-json evidence=') as caught:
                fixture.json_response(result)
            self.assertNotIn('private-signed-url', str(caught.exception))
            self.assertNotIn('private-secret', str(caught.exception))
            paths = list(Path(directory).glob('longhorn-account-failure-*.json'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(paths[0].read_text()),
                             {'rc': 0, 'stdout': result.stdout, 'stderr': result.stderr})

    def test_unexpected_refusal_preserves_exact_response_and_keeps_stage_private(self):
        for rc in (0,1):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as directory:
                fixture, _, _, _, _, _, _ = self.fixture(directory)
                result = SimpleNamespace(returncode=rc,stdout='unexpected-private-response',stderr='private-key')
                with self.assertRaisesRegex(ValueError,'native authorization refusal \\(admin-list\\)') as caught:
                    fixture.require_refusal(result,{'AccessDenied'},'native authorization refusal (admin-list)')
                self.assertNotIn('unexpected-private-response',str(caught.exception))
                self.assertNotIn('private-key',str(caught.exception))
                paths = list(Path(directory).glob('longhorn-account-failure-*.json'))
                self.assertEqual(len(paths),1)
                self.assertEqual(paths[0].stat().st_mode & 0o777,0o600)
                self.assertEqual(json.loads(paths[0].read_text())['stdout'],result.stdout)

    def test_qualification_proves_real_key_rotation_contract_and_preserves_foreign_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _, _, _, server, _ = self.fixture(directory)
            server['foreign-canary'] = {'name': 'do-not-touch'}
            with redirect_stdout(io.StringIO()) as output:
                fixture.qualify()
                self.assertEqual(server, {'foreign-canary': {'name': 'do-not-touch'}})
                self.assertEqual({row['phase'] for row in fixture.read()['accounts'].values()}, {'revoked'})
                fixture.cleanup()
            self.assertIn('authorization_refusals=4 revoked_key_refusals=1 sha256=', output.getvalue())
            self.assertNotIn('serverGeneratedPrivateKey12345', output.getvalue())

    def test_pipe_sets_command_local_json_to_disable_pinned_progress_output(self):
        # pipe-main.go reads ctx.Bool("json") for its progress reader, while
        # printMsg uses globalJSON. A global-only flag can mix both formats.
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, observations = self.fixture(directory)
            original = lab.execute.side_effect
            def pinned_pipe(argv, **kwargs):
                result = original(argv, **kwargs)
                args = argv[argv.index('mc') + 2:]
                if args[0] == 'pipe' and args[1] != '--json':
                    result.stdout = '\r0 B / ? ' + result.stdout
                return result
            lab.execute.side_effect = pinned_pipe
            with redirect_stdout(io.StringIO()):
                fixture.qualify()
                fixture.cleanup()
            pipes = [argv for argv, _ in observations if 'pipe' in argv]
            self.assertTrue(pipes)
            for argv in pipes:
                self.assertEqual(argv[argv.index('pipe') + 1], '--json')

    def test_qualification_rejects_unfiltered_bucket_listing_and_generic_foreign_object_errors(self):
        for defect in ('unfiltered', 'transport-error'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                fixture, lab, _, state, _, _, _ = self.fixture(directory)
                original = lab.execute.side_effect
                def broken(argv, **kwargs):
                    result = original(argv, **kwargs)
                    args = argv[argv.index('mc') + 2:]
                    restricted = Path(argv[argv.index('--env-file') + 1]).name != 'mc.env'
                    if restricted and defect == 'unfiltered' and args == ['ls', 'lh/']:
                        result.stdout += '\n' + json.dumps({'status': 'success', 'type': 'folder',
                            'key': state['bucket'] + '-denied/'})
                    if restricted and defect == 'transport-error' and args[0] == 'cat' and '-denied/' in args[1]:
                        result.stdout = 'EOF'
                    return result
                lab.execute.side_effect = broken
                with self.assertRaisesRegex(ValueError, 'escaped|native authorization refusal'):
                    fixture.qualify()
                fixture.cleanup()

    def test_store_removal_is_blocked_until_product_and_accounts_are_restored(self):
        backup_spec = importlib.util.spec_from_file_location('account_store', ROOT/'tests/helpers/longhorn-backups.py')
        backups = importlib.util.module_from_spec(backup_spec); backup_spec.loader.exec_module(backups)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'store.json').write_text('{}'); (root/'accounts.json').write_text('{}')
            order = []
            account = SimpleNamespace(cleanup=Mock(side_effect=lambda: (_ for _ in ()).throw(ValueError('account changed'))))
            lab = SimpleNamespace(load_helper=Mock(return_value=SimpleNamespace(Accounts=Mock(return_value=account))),
                                  execute=Mock(), get=Mock(return_value=None))
            store = SimpleNamespace(lab=lab, directory=root, record=root/'store.json',
                restore_product=Mock(side_effect=lambda: order.append('product-restored')),
                receipt=Mock(return_value={'run_id': 'run-id'}), inspect=Mock(), cleanup_transport=Mock())
            with self.assertRaisesRegex(ValueError, 'account changed'): backups.Store.cleanup(store)
            self.assertEqual(order, ['product-restored'])
            account.cleanup.assert_called_once()
            lab.execute.assert_not_called(); store.cleanup_transport.assert_not_called()
            self.assertTrue(store.record.exists())

    def test_only_exact_native_error_code_counts_as_authorization_or_credential_refusal(self):
        for code in ('AccessDenied', 'InvalidAccessKeyId', 'SignatureDoesNotMatch'):
            result = SimpleNamespace(returncode=1, stdout=json.dumps({'status': 'error',
                'error': {'cause': {'error': {'Code': code}}}}), stderr='')
            self.assertEqual(accounts.native_refusal(result, {'AccessDenied'}), code == 'AccessDenied')
            self.assertEqual(accounts.native_refusal(result, {'InvalidAccessKeyId'}), code == 'InvalidAccessKeyId')
            result.returncode = 0
            self.assertFalse(accounts.native_refusal(result, {code}))
        for raw in ('', 'EOF', 'AccessDenied', '{}', '{"status":"error","error":"AccessDenied"}',
                    '{"status":"error","error":{"cause":{"error":null}}}'):
            result = SimpleNamespace(returncode=1, stdout=raw, stderr='AccessDenied')
            self.assertFalse(accounts.native_refusal(result, {'AccessDenied'}))

    def test_pinned_head_error_mapping_requires_exact_operation_target_type_and_cause(self):
        target = 'lh/longhorn-backup-' + 'a'*16 + '-denied/authorization-canary'
        path = 'https://127.0.0.1:9000/' + target[3:]
        permission = {'status':'error','error':{'type':'fatal',
            'message':'Unable to read from `'+target+'`.',
            'cause':{'message':'Insufficient permissions to access this path `'+path+'`','error':{'Path':path}}}}
        missing = {'status':'error','error':{'type':'fatal','message':'Unable to stat `'+target+'`.',
            'cause':{'message':'Object does not exist','error':{}}}}
        for original,operation,code in ((permission,'cat','AccessDenied'),(missing,'stat','NoSuchKey')):
            for mutation in ('none','success','target','operation','code','message','cause','type','native','multi'):
                response = copy.deepcopy(original); rc=1; expected_target=target; expected_operation=operation; codes={code}
                if mutation == 'success': rc=0
                elif mutation == 'target': expected_target += '-foreign'
                elif mutation == 'operation': expected_operation='ls'
                elif mutation == 'code': codes={'SignatureDoesNotMatch'}
                elif mutation == 'message': response['error']['message'] += 'unexpected'
                elif mutation == 'cause': response['error']['cause']['message'] += 'unexpected'
                elif mutation == 'type': response['error']['type']='unexpected'
                elif mutation == 'native': response['error']['cause']['error']['unexpected']='value'
                raw=json.dumps(response) + ('\n{}' if mutation == 'multi' else '')
                result=SimpleNamespace(returncode=rc,stdout=raw,stderr='')
                with self.subTest(operation=operation,mutation=mutation):
                    self.assertEqual(accounts.native_refusal(result,codes,operation=expected_operation,
                                                             target=expected_target),mutation=='none')
            result=SimpleNamespace(returncode=1,stdout=json.dumps(original),stderr='')
            self.assertFalse(accounts.native_refusal(result,{code}))
            self.assertFalse(accounts.native_refusal(result,{'InvalidAccessKeyId'},operation=operation,target=target))

    def test_limited_client_refuses_revoked_or_interrupted_environment_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, _ = self.fixture(directory)
            key = fixture.create(); fixture.revoke(key)
            lab.execute.reset_mock()
            with self.assertRaisesRegex(ValueError, 'not a ready'):
                fixture.command('cat', 'lh/bucket/object', account=key)
            lab.execute.assert_not_called()
            row = fixture.read(); row['accounts'][key]['phase'] = 'ready'
            lab.write_private(fixture.record, row)
            environment = Path(directory) / (key+'.env'); environment.write_text('interrupted private material')
            with self.assertRaisesRegex(ValueError, 'needs recovery'):
                fixture.command('cat', 'lh/bucket/object', account=key)
            self.assertEqual(environment.read_text(), 'interrupted private material')
            lab.execute.assert_not_called()

    def test_replaced_client_environment_is_preserved_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, lab, _, _, _, _, _ = self.fixture(directory)
            key = fixture.create(); environment = Path(directory) / (key+'.env')
            def replace(_argv, **_kwargs):
                environment.rename(environment.with_suffix('.original'))
                environment.write_text('foreign replacement')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            lab.execute.side_effect = replace
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                fixture.command('cat', 'lh/bucket/object', account=key)
            self.assertEqual(environment.read_text(), 'foreign replacement')


if __name__ == '__main__':
    unittest.main()
