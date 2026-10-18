"""CPU gate must reject common VM-masked instruction sets before mutation."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
import json
import ast
import tempfile

spec = importlib.util.spec_from_file_location('mongodb_platform', Path(__file__).resolve().parents[1] / 'helpers/mongodb-platform.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
relay_spec = importlib.util.spec_from_file_location('mongodb_relay', Path(__file__).resolve().parents[1] / 'helpers/mongodb-registry-relay.py')
relay = importlib.util.module_from_spec(relay_spec)
relay_spec.loader.exec_module(relay)


class MongoDBRelayTests(unittest.TestCase):
    def test_stability_reboot_receipt_uses_its_bound_backup_and_fault_journals(self):
        for topology in ('source','sharded'):
            for drift in (None,'core','run','client','topology','journal','ambiguous','symlink'):
                with self.subTest(topology=topology,drift=drift),tempfile.TemporaryDirectory() as tmp:
                    directory=Path(tmp);address=relay.NODES[3];name='mongodb-node-'+address.rsplit('.',1)[1]
                    fault={'address':address,'node':name,'node_uid':'node','node_uids':{name:'node'},'issued':True,
                        'cluster_uid':'core','run_id':'mongodb_gate_0123456789abcdef','client_uid':'client',
                        'boot_id':'01234567-0123-0123-0123-0123456789ab'}
                    backup={'cluster_uid':'core','run_id':'a'*16,'stability':{
                        'workload':fault['run_id'],'client_uid':'client','topology':topology}}
                    journal={'cluster_uid':'core','run_id':'a'*16,'workload':fault['run_id'],'topology':topology}
                    if drift in ('core','run','client'):fault[{'core':'cluster_uid','run':'run_id','client':'client_uid'}[drift]]='foreign'
                    if drift=='topology':backup['stability']['topology']='foreign'
                    if drift=='journal':journal['run_id']='b'*16
                    for filename,document in [('mongodb-fault-node.json',fault),('mongodb-backup-logical.json',backup),
                            ('mongodb-fault-stability.json',journal),('mongodb-lab-owner.json',{
                                'cluster_uid':'core','nodes':list(relay.NODES),'clean_start_verified':True})]:
                        (directory/filename).write_text(json.dumps(document))
                    if drift=='symlink':
                        (directory/'mongodb-backup-logical.json').rename(directory/'linked')
                        (directory/'mongodb-backup-logical.json').symlink_to(directory/'linked')
                    if drift=='ambiguous':
                        (directory/'mongodb-performance.json').write_text(json.dumps({k:fault[k] for k in ('cluster_uid','run_id','client_uid')}))
                    node={'metadata':{'uid':'node'},'status':{'addresses':[{'type':'InternalIP','address':address}]}}
                    with patch.object(relay,'DIRECTORY',directory),patch.object(relay,'execute',return_value=SimpleNamespace(stdout=json.dumps(node))):
                        if drift:
                            with self.assertRaises(ValueError):relay.reboot_receipt(address)
                        else:self.assertEqual(relay.reboot_receipt(address),fault)

    def test_reboot_receipt_requires_matching_workload_owner_and_current_node(self):
        for drift in (None,'core','run','client','issued','address','node-uid','boot'):
            with self.subTest(drift=drift),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);address=relay.NODES[3];name='mongodb-node-'+address.rsplit('.',1)[1]
                fault={'address':address,'node':name,'node_uid':'node','node_uids':{name:'node'},'issued':True,
                    'cluster_uid':'core','run_id':'mongodb_gate_0123456789abcdef','client_uid':'client',
                    'boot_id':'01234567-0123-0123-0123-0123456789ab'}
                workload={k:fault[k] for k in ('cluster_uid','run_id','client_uid')}
                if drift in ('core','run','client'):fault[{'core':'cluster_uid','run':'run_id','client':'client_uid'}[drift]]='foreign'
                if drift=='issued':fault['issued']=False
                if drift=='address':fault['address']='192.168.122.1'
                if drift=='boot':fault['boot_id']='invalid'
                directory.joinpath('mongodb-fault-node.json').write_text(json.dumps(fault))
                directory.joinpath('mongodb-performance.json').write_text(json.dumps(workload))
                directory.joinpath('mongodb-lab-owner.json').write_text(json.dumps({'cluster_uid':'core',
                    'nodes':list(relay.NODES),'clean_start_verified':True}))
                node={'metadata':{'uid':'changed' if drift=='node-uid' else 'node'},
                      'status':{'addresses':[{'type':'InternalIP','address':address}]}}
                with patch.object(relay,'DIRECTORY',directory),patch.object(relay,'execute',return_value=SimpleNamespace(stdout=json.dumps(node))) as api:
                    if drift:
                        with self.assertRaises(ValueError):relay.reboot_receipt(address)
                    else:
                        self.assertEqual(relay.reboot_receipt(address),fault)
                        command=api.call_args.args[0];self.assertEqual(command[-5:],['get','node',name,'-o','json'])

    def test_expected_reboot_ssh_loss_is_bounded_without_creating_listener_or_rule(self):
        address=relay.NODES[3];row={'node':address,'socket':'/private/owned.sock'}
        receipt={'boot_id':'01234567-0123-0123-0123-0123456789ab'}
        for expired in (False,True):
            with self.subTest(expired=expired),patch.object(relay,'journal',return_value=[row]), \
                    patch.object(relay,'execute',return_value=SimpleNamespace(returncode=1)), \
                    patch.object(relay,'reboot_receipt',return_value=receipt), \
                    patch.object(relay,'remote',return_value=SimpleNamespace(returncode=255,stdout='')) as remote, \
                    patch.object(relay,'connect') as connect,patch.object(relay.time,'monotonic',return_value=301 if expired else 10):
                pending={address:0} if expired else {}
                if expired:
                    with self.assertRaisesRegex(ValueError,'deadline'):relay.refresh(pending)
                else:
                    relay.refresh(pending);self.assertIn(address,pending)
                connect.assert_not_called();self.assertEqual(remote.call_args.args[1],'cat /proc/sys/kernel/random/boot_id')

    def test_boot_change_reconnects_only_owned_missing_rule_and_rejects_foreign_listener(self):
        row={'node':relay.NODES[3],'socket':'/private/owned.sock'}
        for missing,foreign in ((False,False),(True,False),(True,True)):
            with self.subTest(missing=missing,foreign=foreign):
                calls=[]
                def remote(node,command,check=True):
                    calls.append(command)
                    if command.startswith('test -z') and foreign:raise ValueError('foreign listener')
                    return SimpleNamespace(returncode=1 if missing and command==relay.rule('-C') else 0,
                        stdout='11234567-0123-0123-0123-0123456789ab\n')
                with patch.object(relay,'journal',return_value=[row]), \
                        patch.object(relay,'execute',return_value=SimpleNamespace(returncode=1)), \
                        patch.object(relay,'reboot_receipt',return_value={'boot_id':'01234567-0123-0123-0123-0123456789ab'}), \
                        patch.object(relay,'remote',side_effect=remote),patch.object(relay,'connect') as connect, \
                        patch.object(relay,'probe',return_value=True):
                    if foreign:
                        with self.assertRaisesRegex(ValueError,'foreign listener'):relay.refresh({})
                        connect.assert_not_called();self.assertNotIn(relay.rule('-A'),calls)
                    else:
                        relay.refresh({});connect.assert_called_once_with(row)
                        self.assertEqual(relay.rule('-A') in calls,missing)

    def test_unrecorded_transport_loss_still_fails_without_recreating_nat(self):
        row={'node':relay.NODES[3],'socket':'/private/owned.sock'}
        with patch.object(relay,'journal',return_value=[row]), \
                patch.object(relay,'execute',return_value=SimpleNamespace(returncode=1)), \
                patch.object(relay,'reboot_receipt',return_value=None),patch.object(relay,'remote',side_effect=ValueError('missing rule')), \
                patch.object(relay,'connect') as connect:
            with self.assertRaisesRegex(ValueError,'missing rule'):relay.refresh({})
            connect.assert_not_called()

    def test_rule_and_listener_are_exactly_scoped_and_exclude_ceph_state(self):
        command = relay.rule('-A')
        for part in ('OUTPUT', '192.168.47.130/32', '--dport 5000', 'kubeauto-mongodb-registry', '--to-ports 23510'):
            self.assertIn(part, command)
        self.assertNotIn('ceph', command)
        with self.assertRaises(ValueError):
            relay.rule('-F')
        with patch.object(relay, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'foreign host'):
                relay.remote('192.168.122.1', 'true')
        execute.assert_not_called()

    def test_connect_uses_loopback_reverse_forward_and_requires_forward_success(self):
        row = {'node': relay.NODES[0], 'socket': '/private/owned.sock'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(relay, 'DIRECTORY', Path(tmp)), \
                patch.object(relay, 'execute') as execute:
            relay.connect(row)
            log = Path(tmp) / ('mongodb-registry-' + row['node'] + '.log')
            self.assertTrue(log.is_file())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertEqual(execute.call_args.args[0][execute.call_args.args[0].index('-E') + 1], str(log))
        command = execute.call_args.args[0]
        self.assertIn('127.0.0.1:23510:127.0.0.1:5000', command)
        self.assertIn('ExitOnForwardFailure=yes', command)
        self.assertIn('BatchMode=yes', command)
        self.assertNotIn('0.0.0.0', ' '.join(command))

    def test_failed_remote_command_retains_node_and_rc_without_exposing_output(self):
        for rc in (1, 255):
            with self.subTest(rc=rc), patch.object(relay.subprocess, 'run',
                    return_value=SimpleNamespace(returncode=rc, stdout='private-output', stderr='private-error')):
                with self.assertRaises(ValueError) as raised:
                    relay.remote(relay.NODES[0], relay.rule('-C'))
                message = str(raised.exception)
                self.assertIn('node=' + relay.NODES[0], message)
                self.assertIn('rc=' + str(rc), message)
                self.assertNotIn('private-output', message)
                self.assertNotIn('private-error', message)

    def test_relay_log_refuses_symlink_before_connecting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(relay, 'DIRECTORY', Path(tmp)), \
                patch.object(relay, 'execute') as execute:
            target = Path(tmp) / 'foreign';target.write_text('preserved')
            log = Path(tmp) / ('mongodb-registry-' + relay.NODES[0] + '.log')
            log.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                relay.connect({'node': relay.NODES[0], 'socket': '/private/owned.sock'})
            execute.assert_not_called()
            self.assertEqual(target.read_text(), 'preserved')

    def test_relay_log_keeps_existing_diagnostics_private(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(relay, 'DIRECTORY', Path(tmp)), \
                patch.object(relay, 'execute'):
            log = Path(tmp) / ('mongodb-registry-' + relay.NODES[0] + '.log')
            log.write_text('first failure\n');log.chmod(0o644)
            relay.connect({'node': relay.NODES[0], 'socket': '/private/owned.sock'})
            self.assertEqual(log.read_text(), 'first failure\n')
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_entire_journal_is_validated_before_any_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'mongodb-registry-relay.json'
            rows = [{'node': relay.NODES[0], 'socket': str(directory / ('mongodb-registry-' + relay.NODES[0] + '.sock'))},
                    {'node': '192.168.122.1', 'socket': '/foreign'}]
            state.write_text(json.dumps(rows))
            with patch.object(relay, 'DIRECTORY', directory), patch.object(relay, 'STATE', state), \
                    patch.object(relay, 'remote') as remote, patch.object(relay, 'execute') as execute:
                with self.assertRaisesRegex(ValueError, 'foreign journal'):
                    relay.cleanup()
            remote.assert_not_called()
            execute.assert_not_called()
            self.assertTrue(state.exists())

    def test_setup_persists_owner_before_opening_listener_and_preserves_partial_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'mongodb-registry-relay.json'
            directory.joinpath('mongodb-lab-owner.json').write_text(json.dumps({
                'cluster': 'mongodb-gate', 'nodes': list(relay.NODES), 'clean_start_verified': True}))
            def connect(row):
                self.assertEqual(json.loads(state.read_text()), [row])
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                raise ValueError('first-listener-failure')
            def remote(node, command, check=True):
                return SimpleNamespace(returncode=1 if command == relay.rule('-C') else 0)
            with patch.object(relay, 'DIRECTORY', directory), patch.object(relay, 'STATE', state), \
                    patch.object(relay, 'probe', return_value=False), patch.object(relay, 'remote', side_effect=remote), \
                    patch.object(relay, 'connect', side_effect=connect):
                with self.assertRaisesRegex(ValueError, 'first-listener-failure'):
                    relay.setup()
            self.assertEqual(json.loads(state.read_text())[0]['node'], relay.NODES[0])

    def test_unknown_rule_inspection_failure_never_adds_a_rule_or_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.joinpath('mongodb-lab-owner.json').write_text(json.dumps({
                'cluster': 'mongodb-gate', 'nodes': list(relay.NODES), 'clean_start_verified': True}))
            with patch.object(relay, 'DIRECTORY', directory), patch.object(relay, 'STATE', directory / 'state.json'), \
                    patch.object(relay, 'probe', return_value=False), \
                    patch.object(relay, 'remote', return_value=SimpleNamespace(returncode=2)), patch.object(relay, 'connect') as connect:
                with self.assertRaisesRegex(ValueError, 'unknown inspection failure'):
                    relay.setup()
            connect.assert_not_called()

    def test_qualification_always_cleans_its_own_partially_created_state(self):
        with patch.object(relay, 'setup', side_effect=ValueError('first-setup-failure')), \
                patch.object(relay, 'cleanup') as cleanup:
            with self.assertRaisesRegex(ValueError, 'first-setup-failure'):
                relay.qualification()
        cleanup.assert_called_once()


class CPUPreflightTests(unittest.TestCase):
    def test_v3_requires_virtual_cpu_flags_and_uses_image_loader(self):
        fixture = {'architecture': 'x86_64', 'flags': sorted(module.V3 | {'abm'}), 'glibc_v3': True, 'cpu_count': 4}
        module.validate_cpu(fixture)
        module.validate_cpu(fixture | {'glibc_v3': False})
        for change in ({'flags': sorted(module.V3 - {'avx2'})},
                       {'architecture': 'aarch64'}, {'cpu_count': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate_cpu(fixture | change)

    def test_pool_is_exact_and_excludes_control_and_forbidden_host(self):
        self.assertEqual(set(module.NODES), {'192.168.122.217','192.168.122.243','192.168.122.246',
                                            '192.168.122.193','192.168.122.210','192.168.122.216'})
        self.assertEqual(module.POOLS['core'], ('192.168.47.130', tuple(f'192.168.47.{i}' for i in range(131,137))))
        self.assertEqual(set(module.POOLS['ceph'][1]), {'192.168.122.135', '192.168.122.40', '192.168.122.72',
                                                     '192.168.122.212', '192.168.122.165', '192.168.122.238'})

    def test_core_probe_uses_only_authorized_control(self):
        with patch.object(module.sys, 'argv', ['probe', '--pool', 'core', '--control']), \
                patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"active_jobs":[]}')) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertIn('root@192.168.47.130', run.call_args.args[0])

    def test_control_probe_preserves_active_job_and_uses_only_fixed_control(self):
        for active in ([], [{'state_file': 'kubeauto-other-gate.pid', 'pid': 123}]):
            payload = json.dumps({'active_jobs': active})
            with patch.object(module.sys, 'argv', ['probe', '--control']), \
                    patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=payload)) as run, \
                    redirect_stdout(StringIO()):
                self.assertEqual(module.main(), int(bool(active)))
            self.assertIn('root@192.168.122.2', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['input'], module.CONTROL_PROBE)

    def test_inventory_probe_is_read_only_and_decodes_metadata(self):
        payload = json.dumps({'directory':'old-fixture','inventory_ips':[],'owners':[]})
        with patch.object(module.sys,'argv',['probe','--inventory']), \
                patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=payload)) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(module.main(),0)
        self.assertEqual(run.call_args.kwargs['input'],module.INVENTORY_PROBE)
        tree = ast.parse(module.INVENTORY_PROBE)
        self.assertFalse(any(isinstance(n,ast.Name) and n.id in ('subprocess','exec','eval') for n in ast.walk(tree)))

    def test_stale_unit_recovery_is_fixed_target_under_existing_pool_leases(self):
        with patch.object(module.sys, 'argv', ['probe', '--pool', 'ceph', '--reset-stale-unit']), \
                patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='')) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(module.main(), 0)
        args = run.call_args.args[0]
        self.assertIn('root@192.168.47.130', args)
        self.assertIn('kubeauto-ceph-priority.lock', args[-1])
        self.assertIn('kubeauto-ceph-compute.lock', args[-1])
        self.assertIn('root@192.168.122.72', args[-1])
        self.assertEqual(run.call_args.kwargs['input'], module.RESET_STALE_UNIT)
        tree = ast.parse(module.RESET_STALE_UNIT)
        commands = [n.args[0] for n in ast.walk(tree) if isinstance(n, ast.Call) and
                    isinstance(n.func, ast.Attribute) and n.func.attr == 'run']
        literal_commands = [ast.literal_eval(n) for n in commands if all(isinstance(e, ast.Constant) for e in n.elts)]
        self.assertIn(['systemctl', 'reset-failed', 'kube-apiserver'], literal_commands)
        self.assertFalse(any('stop' in c or 'rm' in c or 'delete' in c for c in literal_commands))
