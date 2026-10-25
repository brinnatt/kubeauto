"""Qualified disk, recovery and transport inputs must fail before live mutation."""
import copy
import base64
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_lab', ROOT / 'tests/helpers/longhorn-lab.py')
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
spec = importlib.util.spec_from_file_location('longhorn_backups', ROOT / 'tests/helpers/longhorn-backups.py')
backups = importlib.util.module_from_spec(spec); spec.loader.exec_module(backups)


def allowlist():
    rows = []
    for node in lab.NODES:
        for letter, pci in zip('bcd', ('08', '09', '0a')):
            rows.append('|'.join((node, '/dev/vd' + letter, '/dev/disk/by-path/pci-0000:' + pci + ':00.0', '', '',
                                  'path:pci-0000:' + pci + ':00.0', str(100 * 1024**3))))
    return '\n'.join(rows) + '\n'


class LonghornLabContracts(unittest.TestCase):
    def test_recurring_selector_recovery_is_explicit_and_delegated_to_schedule_stop(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'longhorn-recurring-runtime.json').write_text('{}')
                schedule = SimpleNamespace(recover_selector=Mock(), cleanup=Mock())
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner', return_value={}), \
                        patch.object(lab, 'load_helper', return_value=SimpleNamespace(Schedule=lambda _lab: schedule)), \
                        patch.dict(lab.sys.modules, {lab.__name__: lab}), patch.object(lab, 'restore_request_input'), \
                        patch.object(lab, 'cleanup_reclaim_class'):
                    lab.cleanup_business(relay_pid=12, recover_recurring_selector=explicit)
                schedule.recover_selector.assert_not_called()
                if explicit: schedule.cleanup.assert_called_once_with(12, recover_selector=True)
                else: schedule.cleanup.assert_called_once_with(12)
        result = subprocess.run([lab.sys.executable, str(ROOT/'tests/helpers/longhorn-lab.py'),
                                 'business-recurring-snapshot', '--recover-recurring-selector'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('restricted to explicit business cleanup', result.stderr)

    def test_account_qualification_never_changes_product_and_cleans_accounts_before_store(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for failure in ('none', 'prepare', 'initialize', 'qualify', 'account-cleanup'):
            with self.subTest(failure=failure):
                calls = []
                def step(name):
                    def action():
                        calls.append(name)
                        if name == failure:
                            raise ValueError(name)
                    return Mock(side_effect=action)
                store = SimpleNamespace(prepare=step('prepare'), cleanup=step('store-cleanup'))
                account = SimpleNamespace(initialize=step('initialize'), qualify=step('qualify'),
                                          cleanup=step('account-cleanup'))
                helper = SimpleNamespace(Accounts=Mock(return_value=account))
                with patch.object(lab, 'backup_store', return_value=store), \
                        patch.object(lab, 'load_helper', return_value=helper), \
                        patch.dict(lab.sys.modules, {lab.__name__: lab}), patch.object(lab, 'business') as business:
                    if failure == 'none': lab.qualify_backup_accounts()
                    else:
                        with self.assertRaises(ValueError): lab.qualify_backup_accounts()
                business.assert_not_called()
                self.assertEqual(calls[-1], 'store-cleanup')
                if failure == 'none':
                    self.assertEqual(calls, ['prepare', 'initialize', 'qualify', 'account-cleanup', 'store-cleanup'])
                elif failure in ('qualify', 'account-cleanup'):
                    self.assertLess(calls.index('account-cleanup'), calls.index('store-cleanup'))

    def test_encryption_focused_phases_do_not_repeat_unrelated_filesystem_work(self):
        from unittest.mock import Mock
        for phase in ('block', 'backup', 'foreign'):
            with self.subTest(phase=phase):
                store = Mock(); store.product_configuration.return_value = {'fixture': True}
                with patch.object(lab, 'business') as business, patch.object(lab, 'backup_store', return_value=store) as factory, \
                        redirect_stdout(io.StringIO()) as output:
                    if phase == 'foreign':
                        with self.assertRaises(ValueError): lab.encryption_suite(123, phase=phase)
                    else: lab.encryption_suite(123, phase=phase)
                if phase == 'block':
                    business.assert_called_once_with('encryption', block=True, storage_class='longhorn-encrypted')
                    factory.assert_not_called()
                elif phase == 'backup':
                    business.assert_called_once_with('backup', 123, storage_class='longhorn-encrypted',
                                                     backup_configuration={'fixture': True})
                    store.prepare.assert_called_once(); store.qualify.assert_called_once(); store.cleanup.assert_called_once()
                else:
                    business.assert_not_called(); factory.assert_not_called()
                self.assertEqual('LONGHORN_ENCRYPTION_'+phase.upper()+'_BUSINESS_PASS' in output.getvalue(), phase != 'foreign')

    def test_encryption_suite_stops_at_failed_boundary_and_owns_backup_cleanup(self):
        from unittest.mock import Mock
        for failure in ('none', 'filesystem', 'block', 'prepare', 'backup'):
            with self.subTest(failure=failure):
                calls = []
                def business(scenario, *args, **kwargs):
                    stage = 'backup' if scenario == 'backup' else 'block' if kwargs['block'] else 'filesystem'
                    calls.append(stage)
                    self.assertEqual(kwargs['storage_class'], 'longhorn-encrypted')
                    if stage == failure: raise ValueError(stage)
                    if stage == 'backup':
                        self.assertEqual(args, (123,)); self.assertEqual(kwargs['backup_configuration'], {'fixture': True})
                def prepare():
                    calls.append('prepare')
                    if failure == 'prepare': raise ValueError('prepare')
                store = Mock()
                store.prepare.side_effect = prepare
                store.product_configuration.return_value = {'fixture': True}
                with patch.object(lab, 'business', side_effect=business), patch.object(lab, 'backup_store', return_value=store), \
                        redirect_stdout(io.StringIO()):
                    if failure == 'none': lab.encryption_suite(123)
                    else:
                        with self.assertRaisesRegex(ValueError, failure): lab.encryption_suite(123)
                stages = ['filesystem', 'block', 'prepare', 'backup']
                self.assertEqual(calls, stages if failure == 'none' else stages[:stages.index(failure)+1])
                self.assertEqual(store.cleanup.call_count, int(failure not in ('filesystem', 'block')))

    def test_business_recovery_restores_private_encryption_key_before_other_cleanup(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'longhorn-encryption-key.json').touch()
            calls = []; probe = Mock(); probe.restore.side_effect = lambda: calls.append('restore-key')
            helper = Mock(); helper.Probe.return_value = probe
            with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner'), \
                    patch.object(lab, 'load_helper', return_value=helper), patch.dict('sys.modules', {lab.__name__: lab}), \
                    patch.object(lab, 'restore_request_input', side_effect=lambda: calls.append('restore-input')), \
                    patch.object(lab, 'cleanup_reclaim_class', side_effect=lambda: calls.append('class-clean')):
                lab.cleanup_business()
            self.assertEqual(calls, ['restore-key', 'restore-input', 'class-clean'])

    def test_recurring_recovery_stops_schedules_before_business_and_requires_runner_relay(self):
        from unittest.mock import Mock
        for outcome in ('success', 'failure', 'missing-relay'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'longhorn-recurring-runtime.json').touch()
                order = []; helper = Mock()
                def cleanup(_relay):
                    order.append('recurring')
                    if outcome == 'failure': raise ValueError('recurring identity changed')
                helper.Schedule.return_value.cleanup.side_effect = cleanup
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner'), \
                        patch.object(lab, 'load_helper', return_value=helper), patch.dict('sys.modules', {lab.__name__: lab}), \
                        patch.object(lab, 'restore_request_input', side_effect=lambda: order.append('input')), \
                        patch.object(lab, 'cleanup_reclaim_class', side_effect=lambda: order.append('class')):
                    if outcome == 'success': lab.cleanup_business(relay_pid=123)
                    else:
                        with self.assertRaisesRegex(ValueError, 'recurring'):
                            lab.cleanup_business(relay_pid=123 if outcome == 'failure' else None)
                self.assertEqual(order, ['recurring', 'input', 'class'] if outcome == 'success'
                                 else ['recurring'] if outcome == 'failure' else [])

    def test_network_recovery_runs_after_key_restore_and_before_business_cleanup(self):
        from unittest.mock import Mock
        for outcome in ('success', 'failure', 'symlink'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'longhorn-encryption-key.json').touch()
                record = root/'longhorn-network-probes.json'
                if outcome == 'symlink': record.symlink_to(root/'absent-journal')
                else: record.touch()
                calls = []
                key = Mock(); key.Probe.return_value.restore.side_effect = lambda: calls.append('key')
                network = Mock()
                def clean():
                    calls.append('network')
                    if outcome != 'success': raise ValueError('network identity changed')
                network.Probe.return_value.cleanup.side_effect = clean
                def helper(name):
                    return {'longhorn-encryption.py': key, 'longhorn-network.py': network}[name]
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner'), \
                        patch.object(lab, 'load_helper', side_effect=helper), patch.dict('sys.modules', {lab.__name__: lab}), \
                        patch.object(lab, 'restore_request_input', side_effect=lambda: calls.append('input')), \
                        patch.object(lab, 'cleanup_reclaim_class', side_effect=lambda: calls.append('class')):
                    if outcome == 'success': lab.cleanup_business()
                    else:
                        with self.assertRaisesRegex(ValueError, 'network identity'): lab.cleanup_business()
                self.assertEqual(calls, ['key', 'network', 'input', 'class'] if outcome == 'success' else ['key', 'network'])
                network.Probe.return_value.cleanup.assert_called_once()

    def test_tls_recovery_precedes_probe_and_business_cleanup_and_stops_on_failure(self):
        from unittest.mock import Mock
        for outcome in ('success', 'failure', 'symlink'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'longhorn-encryption-key.json').touch()
                (root/'longhorn-network-probes.json').touch()
                record = root/'longhorn-security-tls.json'
                if outcome == 'symlink': record.symlink_to(root/'absent-escrow')
                else: record.touch()
                calls = []
                key = Mock(); key.Probe.return_value.restore.side_effect = lambda: calls.append('key')
                security = Mock()
                def restore():
                    calls.append('tls')
                    if outcome != 'success': raise ValueError('TLS identity changed')
                security.Rotation.return_value.restore.side_effect = restore
                network = Mock(); network.Probe.return_value.cleanup.side_effect = lambda: calls.append('network')
                helpers = {'longhorn-encryption.py': key, 'longhorn-security.py': security, 'longhorn-network.py': network}
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner'), \
                        patch.object(lab, 'load_helper', side_effect=helpers.__getitem__), \
                        patch.dict('sys.modules', {lab.__name__: lab}), \
                        patch.object(lab, 'restore_request_input', side_effect=lambda: calls.append('input')), \
                        patch.object(lab, 'cleanup_reclaim_class', side_effect=lambda: calls.append('class')):
                    if outcome == 'success': lab.cleanup_business()
                    else:
                        with self.assertRaisesRegex(ValueError, 'TLS identity'): lab.cleanup_business()
                self.assertEqual(calls, ['key', 'tls', 'network', 'input', 'class'] if outcome == 'success' else ['key', 'tls'])
                security.Rotation.return_value.restore.assert_called_once()

    def test_filesystem_performance_measures_four_modes_and_verifies_every_block(self):
        helper = importlib.util.spec_from_file_location(
            'performance_fixture', ROOT / 'tests/helpers/longhorn-performance.py')
        module = importlib.util.module_from_spec(helper); helper.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            result = module.benchmark(directory, 'contract', dataset_bytes=1024**2,
                                      sequential_operations=3, random_operations=8)
            self.assertEqual(result['verified_blocks'], 256)
            self.assertEqual(result['errors'], 0)
            self.assertTrue(result['buffered_io'])
            self.assertTrue(result['write_fsync_per_operation'])
            self.assertEqual([r['mode'] for r in result['results']],
                             ['sequential-write', 'random-write', 'sequential-read', 'random-read'])
            for row, operations, size in zip(result['results'], (3, 8, 3, 8), (1024**2, 4096, 1024**2, 4096)):
                self.assertEqual(row['operations'], operations)
                self.assertEqual(row['transferred_bytes'], operations * size)
                self.assertAlmostEqual(row['iops'] * row['elapsed_s'], operations)
                self.assertAlmostEqual(row['throughput_bytes_s'] * row['elapsed_s'], operations * size)
                self.assertLessEqual(row['latency_ms']['50'], row['latency_ms']['95'])
                self.assertLessEqual(row['latency_ms']['95'], row['latency_ms']['99'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_filesystem_performance_stops_on_corruption_short_io_and_foreign_file(self):
        spec = importlib.util.spec_from_file_location('performance_fixture', ROOT / 'tests/helpers/longhorn-performance.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for outcome in ('corrupt', 'short-read', 'short-write', 'foreign'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'kubeauto-performance'
                if outcome == 'foreign':
                    path.write_bytes(b'foreign-canary')
                    with self.assertRaises(FileExistsError):
                        module.benchmark(directory, 'contract', dataset_bytes=4096, sequential_operations=1, random_operations=1)
                    self.assertEqual(path.read_bytes(), b'foreign-canary')
                    continue
                method = 'pwrite' if outcome == 'short-write' else 'pread'
                value = 0 if outcome == 'short-write' else (b'' if outcome == 'short-read' else b'\0' * 4096)
                with patch.object(module.os, method, return_value=value), self.assertRaisesRegex(ValueError, 'short|corruption'):
                    module.benchmark(directory, 'contract', dataset_bytes=4096, sequential_operations=1, random_operations=1)
                self.assertFalse(path.exists())

    def test_performance_profile_requires_fresh_complete_metrics_and_excludes_credentials(self):
        from datetime import datetime, timedelta, timezone
        for outcome in ('valid', 'missing', 'stale', 'future', 'container'):
            with self.subTest(outcome=outcome):
                now = datetime.now(timezone.utc)
                timestamp = now - timedelta(seconds=180) if outcome == 'stale' else (
                    now + timedelta(seconds=180) if outcome == 'future' else now)
                pod = {'metadata': {'name': 'writer', 'uid': 'pod-uid', 'annotations': {'secret': 'DO-NOT-PRINT'}},
                       'spec': {'nodeName': 'node', 'containers': [{'name': 'client', 'image': 'fixed-image',
                           'env': [{'name': 'SECRET', 'value': 'DO-NOT-PRINT'}], 'resources': {'requests': {'cpu': '100m'}}}]}}
                metric = {'metadata': {'name': 'writer'}, 'timestamp': timestamp.isoformat(), 'window': '15s',
                          'containers': [{'name': 'other' if outcome == 'container' else 'client',
                                          'usage': {'cpu': '123n', 'memory': '456Ki'}}]}
                def get(kind, *args, **kwargs):
                    return {'items': [pod] if kind == 'pods' else ([] if outcome == 'missing' else [metric])}
                with patch.object(lab, 'get', side_effect=get):
                    if outcome == 'valid':
                        row = lab.performance_profile({'namespace': 'owned'})
                        self.assertEqual(len(row), 2)
                        self.assertNotIn('DO-NOT-PRINT', json.dumps(row))
                        self.assertEqual(row['owned/writer']['containers'][0]['usage'], {'cpu': '123n', 'memory': '456Ki'})
                    else:
                        with self.assertRaises(ValueError): lab.performance_profile({'namespace': 'owned'})
                        if outcome == 'missing':
                            self.assertIsNone(lab.performance_profile({'namespace': 'owned'}, allow_missing=True))
                        else:
                            with self.assertRaises(ValueError):
                                lab.performance_profile({'namespace': 'owned'}, allow_missing=True)

    def test_filesystem_performance_refuses_to_remove_a_replacement_file(self):
        spec = importlib.util.spec_from_file_location('performance_fixture', ROOT / 'tests/helpers/longhorn-performance.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'kubeauto-performance'
            original = module.os.pread
            changed = False
            def replace(fd, length, offset):
                nonlocal changed
                if not changed:
                    path.rename(path.with_name('original-benchmark'))
                    path.write_bytes(b'foreign-replacement')
                    changed = True
                return original(fd, length, offset)
            with patch.object(module.os, 'pread', side_effect=replace), self.assertRaisesRegex(ValueError, 'replaced'):
                module.benchmark(directory, 'contract', dataset_bytes=4096, sequential_operations=1, random_operations=1)
            self.assertEqual(path.read_bytes(), b'foreign-replacement')

    def test_performance_orchestration_rejects_incomplete_results_changed_identity_and_data(self):
        from unittest.mock import Mock
        from types import SimpleNamespace
        for outcome in ('valid', 'initial-missing', 'incomplete', 'replacement', 'corrupt', 'degraded'):
            with self.subTest(outcome=outcome), redirect_stdout(io.StringIO()) as output:
                helper = SimpleNamespace(benchmark=lambda: None)
                row = {'errors': 0, 'verified_blocks': 16384, 'dataset_bytes': 64 * 1024**2,
                       'results': [{'mode': m} for m in ('sequential-write', 'random-write', 'sequential-read', 'random-read')]}
                if outcome == 'incomplete': row['results'].pop()
                before = {'component': {'uid': 'original'}}
                after = {'component': {'uid': 'replacement'}} if outcome == 'replacement' else before
                commands = [subprocess.CompletedProcess([], 0, json.dumps(row), ''),
                            subprocess.CompletedProcess([], 0, 'different' if outcome == 'corrupt' else 'digest', '')]
                with patch.object(lab, 'load_helper', return_value=helper), \
                        patch.object(lab.inspect, 'getsource', return_value='def benchmark(*args): pass'), \
                        patch.object(lab, 'owner', return_value={'run_id': 'fixed-run'}), \
                        patch.object(lab, 'performance_profile', side_effect=([None, before, after]
                                     if outcome == 'initial-missing' else [before, after])) as profile, \
                        patch.object(lab.time, 'sleep'), \
                        patch.object(lab, 'kubectl', side_effect=commands) as command, \
                        patch.object(lab, 'replica_topology', return_value=None if outcome == 'degraded' else {'replica': {}}):
                    if outcome in ('valid', 'initial-missing'):
                        lab.verify_performance({'namespace': 'owned'}, 'payload', 'digest')
                        self.assertIn('LONGHORN_PERFORMANCE_RESULT ', output.getvalue())
                        self.assertEqual(command.call_args_list[0].kwargs['timeout'], 600)
                        compile(command.call_args_list[0].args[-1], 'performance-client', 'exec')
                        self.assertTrue(profile.call_args_list[0].kwargs['allow_missing'])
                        self.assertEqual(profile.call_args_list[-1].kwargs, {})
                    else:
                        with self.assertRaises(ValueError): lab.verify_performance({'namespace': 'owned'}, 'payload', 'digest')
                        self.assertNotIn('LONGHORN_PERFORMANCE_RESULT ', output.getvalue())

    def test_health_reconstructs_only_the_exact_owned_legacy_client_inventory(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for clients in (None, list(lab.WORKERS), list(lab.NODES)):
            with self.subTest(clients=clients), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'yml/longhorn').mkdir(parents=True)
                config = {'longhorn_storage_nodes': list(lab.WORKERS)}
                if clients is not None: config['longhorn_client_nodes'] = clients
                path = root/'yml/longhorn/input.json'; path.write_text(json.dumps(config))
                original = path.read_bytes()
                state = {'namespace_uid': 'namespace', 'tls_uid': 'tls', 'encryption_uid': 'key'}
                healthy = Mock(return_value=True)
                product = SimpleNamespace(API=Mock(return_value='api'), node_map=Mock(return_value='nodes'), healthy=healthy)
                objects = {'namespace': 'namespace', 'longhorn-grpc-tls': 'tls', 'longhorn-volume-key': 'key'}
                def get(kind, name, *args): return {'metadata': {'uid': objects['namespace' if kind=='namespace' else name]}}
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'core_identity'), \
                        patch.object(lab, 'owner', return_value=state), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab.importlib.util, 'spec_from_file_location', return_value=SimpleNamespace(loader=Mock())), \
                        patch.object(lab.importlib.util, 'module_from_spec', return_value=product), \
                        patch.object(lab, 'write_private') as save, redirect_stdout(io.StringIO()):
                    if clients == list(lab.NODES):
                        with self.assertRaisesRegex(ValueError, 'client inventory'): lab.installation_health()
                        healthy.assert_not_called(); save.assert_not_called()
                    else:
                        lab.installation_health()
                        self.assertEqual(healthy.call_args.args[0]['longhorn_client_nodes'], list(lab.WORKERS))
                        save.assert_called_once()
                self.assertEqual(path.read_bytes(), original, 'baseline health must not rewrite product input')

    def test_replica_scale_topology_requires_uid_independent_nodes_and_engine_rw(self):
        journal = {'volume': 'source', 'volume_uid': 'volume-uid'}
        for fault in ('none', 'uid', 'desired', 'policy', 'degraded', 'duplicate-node', 'foreign-node',
                      'failed', 'stopped', 'deleting', 'extra', 'engine-count', 'engine-mode'):
            with self.subTest(fault=fault):
                volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {'numberOfReplicas': 2, 'replicaAutoBalance': 'least-effort'},
                          'status': {'robustness': 'healthy', 'currentNodeID': 'writer'}}
                replicas = [{'metadata': {'name': 'replica-' + str(i), 'uid': 'uid-' + str(i)},
                             'spec': {'volumeName': 'source', 'nodeID': 'longhorn-node-' + address.rsplit('.', 1)[1], 'healthyAt': 'confirmed'},
                             'status': {'currentState': 'running'}} for i, address in enumerate(lab.WORKERS[:2])]
                engine = {'spec': {'volumeName': 'source', 'nodeID': 'writer'},
                          'status': {'currentState': 'running', 'replicaModeMap': {r['metadata']['name']: 'RW' for r in replicas}}}
                engines = [engine]
                if fault == 'uid': volume['metadata']['uid'] = 'replacement'
                if fault == 'desired': volume['spec']['numberOfReplicas'] = 3
                if fault == 'policy': volume['spec']['replicaAutoBalance'] = 'disabled'
                if fault == 'degraded': volume['status']['robustness'] = 'degraded'
                if fault == 'duplicate-node': replicas[1]['spec']['nodeID'] = replicas[0]['spec']['nodeID']
                if fault == 'foreign-node': replicas[1]['spec']['nodeID'] = 'foreign'
                if fault == 'failed': replicas[0]['spec']['failedAt'] = 'failed'
                if fault == 'stopped': replicas[0]['status']['currentState'] = 'stopped'
                if fault == 'deleting': replicas[0]['metadata']['deletionTimestamp'] = 'pending'
                if fault == 'extra': replicas.append(copy.deepcopy(replicas[0]))
                if fault == 'engine-count': engines.append(copy.deepcopy(engine))
                if fault == 'engine-mode': engine['status']['replicaModeMap']['replica-0'] = 'WO'
                objects = {'volumes.longhorn.io': volume, 'replicas.longhorn.io': {'items': replicas}, 'engines.longhorn.io': {'items': engines}}
                with patch.object(lab, 'get', side_effect=lambda kind, *args, **kwargs: objects[kind]):
                    if fault == 'uid':
                        with self.assertRaisesRegex(ValueError, 'identity changed'): lab.replica_topology(journal, 2, 'least-effort')
                    elif fault == 'none': self.assertEqual(len(lab.replica_topology(journal, 2, 'least-effort')), 2)
                    else: self.assertIsNone(lab.replica_topology(journal, 2, 'least-effort'))

    def test_replica_scale_preserves_retained_replicas_data_and_product_refusal(self):
        from unittest.mock import Mock
        for fault in ('none', 'uid', 'data', 'retained-uid'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'config.yml').write_text(lab.yaml.safe_dump({'longhorn_storage_classes': [{'replicas': 3}]}))
                journal = {'volume': 'source', 'volume_uid': 'volume-uid', 'namespace': 'owned'}
                volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {'numberOfReplicas': 3, 'replicaAutoBalance': 'ignored'}}
                placement = {'r'+str(i): {'uid': 'uid'+str(i), 'node': 'node'+str(i)} for i in range(3)}
                def topology(journal, count, policy=None):
                    result = copy.deepcopy(placement)
                    if count == 2:
                        del result['r2']
                        if fault == 'retained-uid': result['r1']['uid'] = 'replacement'
                    return result
                reads = 0
                def get(*args):
                    nonlocal reads
                    reads += 1
                    result = copy.deepcopy(volume)
                    if fault == 'uid' and reads > 1: result['metadata']['uid'] = 'replacement'
                    return result
                def command(*args, stdin=None):
                    if 'patch' in args:
                        operations = json.loads(stdin)
                        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'volume-uid'})
                        for item in operations[1:]:
                            field = item['path'].split('/')[-1]
                            if item['op'] == 'test': self.assertEqual(item['value'], volume['spec'][field])
                            else: volume['spec'][field] = item['value']
                        return subprocess.CompletedProcess(args, 0, '{}', '')
                    return subprocess.CompletedProcess(args, 0, 'wrong' if fault == 'data' else 'digest', '')
                request = Mock()
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'replica_topology', side_effect=topology), patch.object(lab, 'kubectl', side_effect=command) as cli, \
                        patch.object(lab, 'wait', side_effect=lambda label, timeout, ready: ready()), \
                        patch.object(lab, 'run_requests', request), redirect_stdout(io.StringIO()) as output:
                    if fault == 'none':
                        lab.verify_replica_scale(journal, 123, 'payload', 'digest')
                        self.assertEqual(volume['spec'], {'numberOfReplicas': 3, 'replicaAutoBalance': 'ignored'})
                        self.assertEqual(request.call_args.kwargs['storage_classes'][0]['replicas'], 4)
                        self.assertIn('insufficient independent storage nodes', request.call_args.kwargs['expected_refusal'])
                        self.assertIn('LONGHORN_REPLICA_SCALE_REFUSAL_VERIFIED', output.getvalue())
                    else:
                        with self.assertRaises(ValueError): lab.verify_replica_scale(journal, 123, 'payload', 'digest')
                        request.assert_not_called()
                        if fault == 'uid': cli.assert_not_called()
                        self.assertNotIn('REFUSAL_VERIFIED', output.getvalue())

    def test_backup_qualification_uses_verified_tls_transport_before_six_client_probes(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            client = SimpleNamespace(DIRECTORY=Path(directory), NODES=lab.NODES, remote=Mock(), execute=Mock())
            store = backups.Store(client); (store.directory/'cas').mkdir(parents=True)
            (store.directory/'cas/ca.crt').write_text('public-ca')
            with patch.object(store, 'receipt', return_value={'run_id': 'run', 'bucket': 'owned'}), \
                    patch.object(store, 'inspect', return_value={'Id': 'owned'}), \
                    patch.object(store, 'mc', side_effect=['', 'run']), patch.object(store, 'prepare_transport') as prepare, \
                    redirect_stdout(io.StringIO()):
                client.remote.side_effect = lambda *args, **kwargs: self.assertTrue(prepare.called)
                store.qualify()
            prepare.assert_called_once()
            self.assertEqual([call.args[0] for call in client.remote.call_args_list], list(lab.NODES))
            for call in client.remote.call_args_list:
                self.assertIn('ProxyHandler({})', call.args[1][-1])
                self.assertIn('ssl.create_default_context', call.args[1][-1])
                self.assertEqual(json.loads(call.kwargs['stdin']), {'ca': 'public-ca',
                    'url': store.product_endpoint+'/minio/health/live'})
                self.assertNotEqual(store.endpoint, store.product_endpoint)

    def test_backup_transport_retains_end_to_end_tls_and_rejects_wrong_ca(self):
        import http.server
        import socket
        import ssl
        import sys
        import threading
        import urllib.request
        for endpoints in (backups.V1_ENDPOINTS, backups.V2_ENDPOINTS):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); certificates = backups.tls_fixture(endpoints)
                for name, value in certificates.items(): (root/name).write_text(value)
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200); self.end_headers(); self.wfile.write(b'confirmed-object-bytes')
                    def log_message(self, *args): pass
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(root/'tls.crt', root/'tls.key')
                server.socket = context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0)); front_port = reservation.getsockname()[1]
                code = backups.transport_program(endpoints._replace(node='127.0.0.1',
                    transport_port=front_port, tunnel_port=server.server_port))
                process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic()+5
                    while True:
                        try:
                            with socket.create_connection(('127.0.0.1', front_port), timeout=.2): break
                        except OSError:
                            if time.monotonic() > deadline: raise
                            time.sleep(.01)
                    context = ssl.create_default_context(cafile=str(root/'ca.crt'))
                    context.verify_flags |= ssl.VERIFY_X509_STRICT
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
                    url = 'https://127.0.0.1:'+str(front_port)+'/'
                    with opener.open(url, timeout=5) as response:
                        self.assertEqual(response.read(), b'confirmed-object-bytes')
                    wrong = ssl.create_default_context(cadata=lab.tls_fixture()['ca.crt'])
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=wrong))
                    with self.assertRaises(urllib.error.URLError): opener.open(url, timeout=5)
                finally:
                    process.terminate(); process.wait(timeout=5); server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_backup_transport_refuses_replaced_pod_socket_listener_and_namespace_before_cleanup(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('none', 'pod', 'spec', 'namespace', 'listener', 'socket', 'journal'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = SimpleNamespace(DIRECTORY=root, remote=Mock(), execute=Mock(), SSH=lab.SSH,
                    write_private=lab.write_private, remove=Mock())
                store = backups.Store(client); store.directory.mkdir()
                path = store.directory/'transport.sock'; path.write_text('fixture-socket')
                info = path.stat(); listener = {'pid': 123, 'start_ticks': 111}
                pod = {'metadata': {'uid': 'pod'}, 'spec': {'original': True}}
                binding = {'node': backups.TRANSPORT_NODE, 'socket': str(path), 'pod_name': 'owned-transport',
                    'namespace_uid': 'namespace', 'pod_uid': 'pod', 'pod_spec': {'original': True},
                    'listener': listener, 'socket_identity': [info.st_dev, info.st_ino]}
                if fault == 'pod': pod['metadata']['uid'] = 'replacement'
                if fault == 'spec': pod['spec'] = {'foreign': True}
                if fault == 'socket': binding['socket_identity'][1] += 1
                if fault == 'journal': binding['node'] = lab.WORKERS[0]
                row = {'name': 'owned', 'transport': binding}; store.record.write_text(json.dumps(row))
                def get(kind, name, namespace=''):
                    if kind == 'namespace': return {'metadata': {'uid': 'replacement' if fault == 'namespace' else 'namespace'}}
                    return pod if not client.remove.called else None
                client.get = Mock(side_effect=get)
                def command(*args, **kwargs): path.unlink()
                client.execute.side_effect = command
                client.wait = Mock(side_effect=lambda stage, timeout, ready: self.assertTrue(ready()))
                def probe():
                    if client.execute.called: return None
                    return {'foreign': True} if fault == 'listener' else listener
                with patch.object(store, 'receipt', side_effect=lambda: json.loads(store.record.read_text())), \
                        patch.object(store, 'transport_listener', side_effect=probe), redirect_stdout(io.StringIO()):
                    if fault == 'none':
                        store.cleanup_transport()
                        client.remove.assert_called_once_with(pod)
                        self.assertNotIn('transport', json.loads(store.record.read_text()))
                    else:
                        with self.assertRaises(ValueError): store.cleanup_transport()
                        client.remove.assert_not_called(); client.execute.assert_not_called(); client.remote.assert_not_called()

    def test_backup_transport_cleans_only_the_original_orphan_listener_after_socket_exit(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for socket_present in (False, True):
            for fault in ('none', 'replacement-after-exit', 'stop-refused'):
                with self.subTest(socket_present=socket_present,fault=fault), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    client = SimpleNamespace(DIRECTORY=root,SSH=lab.SSH,execute=Mock(),remove=Mock(),
                        write_private=lab.write_private,listener_script=lab.listener_script)
                    store = backups.Store(client);store.directory.mkdir()
                    path=store.directory/'transport.sock'
                    if socket_present: path.touch()
                    listener={'pid':123,'boot_id':'original-boot','start_ticks':111,'uid':0,
                              'command':'sshd: root','binding':'127.0.0.1:23524'}
                    binding={'node':backups.TRANSPORT_NODE,'socket':str(path),'pod_name':'owned-transport',
                        'namespace_uid':'namespace','pod_uid':'pod','pod_spec':{},'listener':listener}
                    if socket_present:
                        info=path.stat();binding['socket_identity']=[info.st_dev,info.st_ino]
                    row={'name':'owned','transport':binding};store.record.write_text(json.dumps(row))
                    client.get=Mock(side_effect=lambda kind,*args: {'metadata':{'uid':'namespace'}} if kind=='namespace' else None)
                    client.execute.side_effect=lambda *args,**kwargs: path.unlink()
                    stopped=False
                    probes=0
                    def probe():
                        nonlocal probes
                        probes+=1
                        if stopped:return None
                        if fault=='replacement-after-exit' and probes>2:return dict(listener,pid=999)
                        return listener
                    def remote(node,argv,**kwargs):
                        nonlocal stopped
                        if kwargs.get('stdin'):
                            self.assertEqual(node,backups.TRANSPORT_NODE)
                            self.assertEqual(json.loads(kwargs['stdin']),{'port':23524,'action':'stop','expected':listener})
                            self.assertIn('assert result==request[\'expected\']',argv[-1])
                            self.assertIn('assert port==23524',argv[-1])
                            if fault=='stop-refused':raise ValueError('listener changed before signal')
                            stopped=True
                    client.remote=Mock(side_effect=remote)
                    client.wait=Mock(side_effect=lambda stage,timeout,ready:self.assertTrue(ready()))
                    with patch.object(store,'receipt',side_effect=lambda:json.loads(store.record.read_text())), \
                            patch.object(store,'transport_listener',side_effect=probe), redirect_stdout(io.StringIO()):
                        if fault=='none':
                            store.cleanup_transport()
                            self.assertTrue(stopped)
                            self.assertNotIn('transport',json.loads(store.record.read_text()))
                        else:
                            with self.assertRaisesRegex(ValueError,'listener'):store.cleanup_transport()
                            self.assertFalse(stopped)
                            self.assertIn('transport',json.loads(store.record.read_text()))
                    self.assertEqual(client.execute.call_count,int(socket_present))

    def test_backup_transport_preflights_before_journaling_and_binds_admitted_pod(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('none', 'foreign-pod', 'foreign-listener', 'failed-create'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); objects = {}
                client = SimpleNamespace(DIRECTORY=root, SSH=lab.SSH, remote=Mock(), cache_artifacts=Mock(),
                    write_private=lab.write_private, remove=Mock())
                store = backups.Store(client); store.directory.mkdir()
                store.record.write_text(json.dumps({'name': 'owned', 'run_id': 'run'}))
                def get(kind, name, namespace=''):
                    if kind == 'namespace': return {'metadata': {'uid': 'namespace'}}
                    return {'metadata': {'uid': 'foreign'}} if fault == 'foreign-pod' else objects.get(name)
                client.get = Mock(side_effect=get)
                listener = {'binding': '127.0.0.1:23524', 'uid': 0, 'command': 'sshd: root', 'start_time': time.time()}
                def execute(argv, **kwargs):
                    (store.directory/'transport.sock').write_text('fixture-socket')
                    return subprocess.CompletedProcess(argv, 0, '', '')
                client.execute = Mock(side_effect=execute)
                def create(*args, stdin):
                    if fault == 'failed-create': raise ValueError('admission-rejected')
                    obj = json.loads(stdin); obj['metadata']['uid'] = 'original'
                    self.assertTrue(obj['spec']['hostNetwork'])
                    self.assertFalse(obj['spec']['automountServiceAccountToken'])
                    self.assertEqual(obj['spec']['nodeName'], 'longhorn-node-135')
                    self.assertEqual(obj['spec']['containers'][0]['command'], ['python3', '-u', '-c', backups.transport_program()])
                    obj['spec']['serviceAccountName'] = 'default'
                    obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}
                    objects[obj['metadata']['name']] = obj
                    return subprocess.CompletedProcess(args, 0, json.dumps(obj), '')
                client.kubectl = Mock(side_effect=create)
                client.wait = Mock(side_effect=lambda stage, timeout, ready: self.assertTrue(ready()))
                listener_calls = 0
                def probe():
                    nonlocal listener_calls
                    listener_calls += 1
                    return listener if listener_calls > 1 or fault == 'foreign-listener' else None
                with patch.object(store, 'receipt', side_effect=lambda: json.loads(store.record.read_text())), \
                        patch.object(store, 'transport_listener', side_effect=probe), redirect_stdout(io.StringIO()):
                    if fault == 'none':
                        store.prepare_transport()
                        binding = json.loads(store.record.read_text())['transport']
                        self.assertEqual(binding['pod_uid'], 'original')
                        self.assertEqual(binding['pod_spec'], objects['owned-transport']['spec'])
                    else:
                        with self.assertRaises(ValueError): store.prepare_transport()
                        if fault.startswith('foreign-'):
                            client.execute.assert_not_called(); client.kubectl.assert_not_called()
                            self.assertNotIn('transport', json.loads(store.record.read_text()))
                        else:
                            self.assertNotIn('pod_uid', json.loads(store.record.read_text())['transport'])
                            self.assertIn('listener', json.loads(store.record.read_text())['transport'])

    def test_backup_uses_full_then_incremental_product_requests_and_distinct_restore_points(self):
        from unittest.mock import Mock
        for fault in ('none', 'encrypted', 'mode', 'url', 'incomplete', 'object-read', 'recovery-full'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); payload = 'confirmed'; digest = hashlib.sha256(payload.encode()*262144).hexdigest()
                journal = {'namespace': 'owned', 'volume': 'pvc-owned', 'objects': {}}; objects = {}; calls = []
                config = {'longhorn_backup_target': 's3://owned@us-east-1/', 'longhorn_backup_secret': 'own-secret'}
                def requests(pid, declarations, *, backup_configuration):
                    self.assertEqual(backup_configuration, config); self.assertEqual(pid, 123)
                    snapshot, backup = declarations; calls.append(backup['mode'])
                    self.assertEqual(snapshot['volume'], 'pvc-owned'); self.assertEqual(backup['snapshot'], snapshot['name'])
                    objects[backup['name']] = {'status': {'state': 'InProgress' if fault == 'incomplete' else 'Completed', 'progress': 100,
                        'url': 's3://owned@us-east-1/?volume=' + ('foreign' if fault == 'url' else 'pvc-owned') + '&backup=' + backup['name']}}
                store = Mock()
                def receipt(volume, name, size):
                    self.assertEqual(volume, 'pvc-owned'); self.assertEqual(size, 2*1024**3)
                    if fault == 'object-read': raise ValueError('object-read-failed')
                    return {'incremental': False if fault == 'mode' else name.startswith('backup-incremental-')}
                store.backup_receipt.side_effect = receipt
                helper = Mock()
                helper.Probe.return_value.host_probe.return_value = {'luks_uuid': 'source-luks'}
                helper.backup_receipt.side_effect = lambda _lab, _store, _journal, name, size: receipt('pvc-owned', name, size)
                if fault == 'encrypted': journal['storage_class'] = 'longhorn-encrypted'
                def command(*args, **kwargs):
                    text = args[-1]; current = payload + ('-after-full-after-incremental' if 'after-incremental' in text else '-after-full')
                    return subprocess.CompletedProcess(args, 0, hashlib.sha256(current.encode()*262144).hexdigest(), '')
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'backup_store', return_value=store), \
                        patch.object(lab, 'run_requests', side_effect=requests), patch.object(lab, 'bind_snapshots'), \
                        patch.object(lab, 'bind_backups'), patch.object(lab, 'get', side_effect=lambda kind, name, *_: objects[name]), \
                        patch.object(lab, 'kubectl', side_effect=command), patch.object(lab, 'verify_backup_restore') as restore, \
                        patch.object(lab, 'load_helper', return_value=helper), \
                        patch.dict('sys.modules', {lab.__name__: lab}), \
                        redirect_stdout(io.StringIO()):
                    if fault == 'recovery-full':
                        result = lab.verify_backup(journal, Mock(), 123, payload, digest, config, modes=('full',))
                        self.assertEqual(calls, ['full']); self.assertEqual(restore.call_count, 1)
                        point = restore.call_args.args
                        self.assertEqual(point[4], payload)
                        self.assertEqual(point[6], payload + '-after-full')
                        self.assertEqual(result[0], point[6])
                    elif fault in ('none', 'encrypted'):
                        result = lab.verify_backup(journal, Mock(), 123, payload, digest, config)
                        self.assertEqual(calls, ['full', 'incremental']); self.assertEqual(restore.call_count, 2)
                        points = [call.args for call in restore.call_args_list]
                        self.assertEqual([row[2] for row in points], ['full', 'incremental'])
                        self.assertEqual([row[4] for row in points], [payload, payload+'-after-full'])
                        self.assertEqual([row[6] for row in points], [payload+'-after-full-after-incremental']*2)
                        self.assertEqual(result[0], points[0][6])
                        if fault == 'encrypted':
                            self.assertEqual(helper.backup_receipt.call_count, 2)
                            self.assertEqual(journal['encrypted_backup_device'], {'luks_uuid': 'source-luks'})
                            store.backup_receipt.assert_not_called()
                        else:
                            helper.backup_receipt.assert_not_called()
                    else:
                        with self.assertRaises(ValueError): lab.verify_backup(journal, Mock(), 123, payload, digest, config)
                        restore.assert_not_called()

    def test_backup_suite_preserves_first_failure_and_attempts_owned_recovery(self):
        from unittest.mock import Mock
        for fault in ('none', 'prepare', 'qualify', 'product_configuration', 'business', 'business-and-clean'):
            store = Mock(); store.product_configuration.return_value = {'target': 'fixture'}
            if fault in ('prepare', 'qualify', 'product_configuration'):
                getattr(store, fault).side_effect = ValueError('first-' + fault)
            if fault == 'business-and-clean': store.cleanup.side_effect = ValueError('cleanup')
            with self.subTest(fault=fault), patch.object(lab, 'backup_store', return_value=store), \
                    patch.object(lab, 'business', side_effect=ValueError('first-business') if fault.startswith('business') else None) as business, \
                    redirect_stderr(io.StringIO()):
                if fault == 'none':
                    lab.backup_suite(123)
                    business.assert_called_once_with('backup', 123, backup_configuration={'target': 'fixture'})
                else:
                    with self.assertRaisesRegex(ValueError, 'first-'): lab.backup_suite(123)
                store.cleanup.assert_called_once()

    def test_restore_capacity_checks_actual_objects_and_identity_before_accepting_size(self):
        for fault in ('none', 'claim-uid', 'pv-uid', 'volume-uid', 'claim-binding', 'pv-binding',
                      'request-size', 'missing-pvc-size', 'missing-pv-size', 'missing-volume-size'):
            with self.subTest(fault=fault):
                journal = {'namespace': 'owned', 'objects': {'PersistentVolumeClaim/restored': 'claim-uid'},
                           'claims': {'restored': {'pv': 'pv', 'pv_uid': 'pv-uid', 'volume': 'volume', 'volume_uid': 'volume-uid'}}}
                claim = {'metadata': {'uid': 'claim-uid'}, 'spec': {'volumeName': 'pv',
                         'resources': {'requests': {'storage': '2Gi'}}}, 'status': {'capacity': {'storage': '2Gi'}}}
                pv = {'metadata': {'uid': 'pv-uid'}, 'spec': {'csi': {'volumeHandle': 'volume'}, 'capacity': {'storage': '2Gi'}}}
                volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {'size': '2147483648'}}
                if fault == 'claim-uid': claim['metadata']['uid'] = 'replacement'
                if fault == 'pv-uid': pv['metadata']['uid'] = 'replacement'
                if fault == 'volume-uid': volume['metadata']['uid'] = 'replacement'
                if fault == 'claim-binding': claim['spec']['volumeName'] = 'foreign'
                if fault == 'pv-binding': pv['spec']['csi']['volumeHandle'] = 'foreign'
                if fault == 'request-size': claim['spec']['resources']['requests']['storage'] = '4Gi'
                if fault == 'missing-pvc-size': del claim['status']['capacity']
                if fault == 'missing-pv-size': del pv['spec']['capacity']
                if fault == 'missing-volume-size': del volume['spec']['size']
                objects = {'pvc': claim, 'pv': pv, 'volumes.longhorn.io': volume}
                with patch.object(lab, 'get', side_effect=lambda kind, *_: objects[kind]):
                    if fault == 'none': lab.verify_restore_capacity(journal, 'restored', 2*1024**3)
                    else:
                        with self.assertRaises(ValueError): lab.verify_restore_capacity(journal, 'restored', 2*1024**3)

    def test_backup_restore_requires_backup_boundary_distinct_identity_and_source_isolation(self):
        from unittest.mock import Mock
        for fault in ('none', 'encrypted', 'encrypted-header', 'wrong-point', 'source-pv', 'source-volume', 'source-write', 'snapshot-error', 'capacity',
                      'cache-miss', 'cache-miss-ready', 'cache-miss-persistent', 'cache-miss-near', 'snapshot-replaced',
                      'missing-size', 'wrong-content', 'receipt-source', 'receipt-backup', 'receipt-size',
                      'receipt-empty', 'pvc-size', 'pv-size', 'native-size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                payload = 'backup-point'; source_payload = 'later-source'
                digest = hashlib.sha256(payload.encode()*262144).hexdigest()
                source_digest = hashlib.sha256(source_payload.encode()*262144).hexdigest()
                journal = {'namespace': 'owned', 'volume': 'source-volume', 'pv': 'source-pv', 'objects': {},
                           'backups': {'own-backup': {'uid': 'backup-uid', 'object_receipt': {
                               'volume': 'foreign' if fault == 'receipt-source' else 'source-volume',
                               'backup': 'foreign' if fault == 'receipt-backup' else 'own-backup',
                               'size': 4*1024**3 if fault == 'receipt-size' else 2*1024**3,
                               'objects': {} if fault == 'receipt-empty' else {'verified-object': {'size': 1, 'sha256': 'a'*64}}}}}}
                helper = Mock()
                if fault.startswith('encrypted'):
                    journal['storage_class'] = 'longhorn-encrypted'
                if fault == 'encrypted-header': helper.verify_restored.side_effect = ValueError('changed LUKS header')
                objects = {}; created = []
                def create(obj):
                    obj = copy.deepcopy(obj); name = obj['metadata']['name']; kind = obj['kind']
                    obj['metadata']['uid'] = name+'-uid'; journal['objects'][kind+'/'+name] = name+'-uid'
                    created.append(obj)
                    if kind == 'VolumeSnapshot':
                        obj['status'] = {'readyToUse': True, 'restoreSize': '4Gi' if fault == 'capacity' else '2Gi'}
                        if fault == 'missing-size': del obj['status']['restoreSize']
                        if fault == 'snapshot-error': obj['status']['error'] = {'message': 'error'}
                        if fault.startswith('cache-miss'):
                            obj['status']['error'] = {'message': 'VolumeSnapshotContent is missing' + (' elsewhere' if fault == 'cache-miss-near' else '')}
                            obj['status']['readyToUse'] = fault == 'cache-miss-ready'
                        objects['volumesnapshots.snapshot.storage.k8s.io', name] = obj
                    if kind == 'VolumeSnapshotContent':
                        if fault == 'snapshot-replaced':
                            key = ('volumesnapshots.snapshot.storage.k8s.io', 'full')
                            objects[key] = copy.deepcopy(objects[key])
                            objects[key]['metadata']['uid'] = 'replacement'
                        objects['volumesnapshots.snapshot.storage.k8s.io', 'full']['status']['boundVolumeSnapshotContentName'] = (
                            'foreign' if fault == 'wrong-content' else name)
                    if kind == 'PersistentVolumeClaim':
                        obj['status'] = {'phase': 'Bound', 'capacity': {'storage': '4Gi' if fault == 'pvc-size' else '2Gi'}}; obj['spec']['volumeName'] = 'restored-pv'
                        objects['pvc', name] = obj; objects['pv', 'restored-pv'] = {'metadata': {'uid': 'restored-pv-uid'}, 'spec': {
                            'capacity': {'storage': '4Gi' if fault == 'pv-size' else '2Gi'}, 'csi': {'volumeHandle': 'restored-volume'}}}
                        objects['volumes.longhorn.io', 'restored-volume'] = {'metadata': {'uid': 'restored-native'},
                            'spec': {'size': str(4*1024**3 if fault == 'native-size' else 2*1024**3)}}
                    if kind == 'Pod':
                        obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}; objects['pod', name] = obj
                    return obj
                def bind(journal, record, claim):
                    journal['claims'] = {claim: {'pv_uid': 'restored-pv-uid', 'volume_uid': 'restored-native', 'pv': 'source-pv' if fault == 'source-pv' else 'restored-pv',
                                                'volume': 'source-volume' if fault == 'source-volume' else 'restored-volume'}}
                def command(*args, **kwargs):
                    pod = args[args.index('exec')+1]
                    if pod == 'writer': actual = digest if fault == 'source-write' else source_digest
                    elif args[-1] == lab.payload_program(payload, read_only=True): actual = source_digest if fault == 'wrong-point' else digest
                    else: actual = hashlib.sha256((payload+'-restored-full').encode()*262144).hexdigest()
                    return subprocess.CompletedProcess(args, 0, actual, '')
                def wait(label, timeout, ready):
                    if label.startswith('backup-csi-snapshot-ready-') and fault in ('cache-miss', 'cache-miss-ready', 'cache-miss-persistent'):
                        self.assertEqual(timeout, 600)
                        self.assertFalse(ready(), 'the exact informer miss must wait, even if ready is stale')
                        self.assertFalse(any(obj['kind'] == 'PersistentVolumeClaim' for obj in created))
                        if fault == 'cache-miss-persistent': raise TimeoutError(label)
                        status = objects['volumesnapshots.snapshot.storage.k8s.io', 'full']['status']
                        status.pop('error'); status['readyToUse'] = True
                    result = ready()
                    if not result: raise TimeoutError(label)
                    return result
                with patch.object(lab, 'DIRECTORY', Path(directory)), patch.object(lab, 'bind_snapshots'), \
                        patch.object(lab, 'get', side_effect=lambda kind, name='', *_: objects.get((kind, name))), \
                        patch.object(lab, 'bind_business_volume', side_effect=bind), patch.object(lab, 'wait', side_effect=wait), \
                        patch.object(lab, 'healthy_replicas', return_value=['a', 'b', 'c']), patch.object(lab, 'kubectl', side_effect=command), \
                        patch.object(lab, 'load_helper', return_value=helper), \
                        patch.dict('sys.modules', {lab.__name__: lab}), \
                        redirect_stdout(io.StringIO()):
                    if fault in ('none', 'missing-size', 'encrypted', 'cache-miss', 'cache-miss-ready'):
                        lab.verify_backup_restore(journal, create, 'full', 'own-backup', payload, digest, source_payload, source_digest)
                        content = next(obj for obj in created if obj['kind'] == 'VolumeSnapshotContent')
                        claim = next(obj for obj in created if obj['kind'] == 'PersistentVolumeClaim')
                        self.assertEqual(content['spec']['deletionPolicy'], 'Retain')
                        self.assertEqual(content['spec']['source']['snapshotHandle'], 'bak://source-volume/own-backup')
                        self.assertEqual(claim['spec']['dataSource']['name'], 'full')
                        self.assertEqual(claim['spec']['storageClassName'], 'longhorn-encrypted' if fault == 'encrypted' else 'longhorn')
                        self.assertEqual(helper.verify_restored.call_count, int(fault == 'encrypted'))
                    else:
                        expected = TimeoutError if fault == 'cache-miss-persistent' else ValueError
                        with self.assertRaises(expected):
                            lab.verify_backup_restore(journal, create, 'full', 'own-backup', payload, digest, source_payload, source_digest)

    def test_backup_request_configuration_uses_product_entry_and_restores_original_input(self):
        from unittest.mock import Mock
        fixture = {'longhorn_backup_target': 's3://owned@us-east-1/', 'longhorn_backup_secret': 'own-secret'}
        for fault in ('none', 'product', 'existing-target', 'unknown-field'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = {'longhorn_requests': [], 'longhorn_backup_target': 's3://foreign@us-east-1/' if fault == 'existing-target' else '',
                          'longhorn_backup_secret': ''}
                original = lab.yaml.safe_dump(config); (root/'config.yml').write_text(original)
                def product(*args, **kwargs):
                    current = lab.yaml.safe_load((root/'config.yml').read_text())
                    self.assertEqual({key: current[key] for key in fixture}, fixture)
                    if fault == 'product': raise ValueError('first-product-failure')
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner', return_value={'run_id': 'owned'}), \
                        patch.object(lab, 'supervise_core', side_effect=product) as entry, redirect_stdout(io.StringIO()):
                    values = dict(fixture, foreign='invalid') if fault == 'unknown-field' else fixture
                    if fault == 'none': lab.run_requests(123, [], backup_configuration=values)
                    else:
                        with self.assertRaises(ValueError): lab.run_requests(123, [], backup_configuration=values)
                    self.assertEqual((root/'config.yml').read_text(), original)
                    self.assertFalse((root/'longhorn-request-input.json').exists())
                    if fault in ('existing-target', 'unknown-field'): entry.assert_not_called()
                    else: entry.assert_called_once()

    def test_backup_csi_binding_never_treats_backup_handle_as_a_native_snapshot(self):
        for fault in ('none', 'foreign-volume', 'foreign-backup'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                row = {'content': 'own-content', 'content_uid': 'content-uid', 'backup': 'own-backup'}
                journal = {'namespace': 'owned', 'volume': 'pvc-owned', 'objects': {'VolumeSnapshot/restore': 'snapshot-uid'},
                           'snapshots': {'restore': row}, 'backups': {'own-backup': {'uid': 'backup-uid'}}}
                handle = 'bak://' + ('foreign' if fault == 'foreign-volume' else 'pvc-owned') + '/' + ('foreign' if fault == 'foreign-backup' else 'own-backup')
                snapshot = {'metadata': {'uid': 'snapshot-uid'}}
                content = {'metadata': {'name': 'own-content', 'uid': 'content-uid'}, 'spec': {
                    'driver': 'driver.longhorn.io', 'deletionPolicy': 'Retain', 'source': {'snapshotHandle': handle},
                    'volumeSnapshotRef': {'uid': 'snapshot-uid', 'name': 'restore', 'namespace': 'owned'}}}
                def get(kind, name='', namespace=''):
                    if kind == 'volumesnapshots.snapshot.storage.k8s.io': return snapshot
                    if kind == 'volumesnapshotcontents.snapshot.storage.k8s.io': return content if name else {'items': [content]}
                    raise AssertionError('backup must not be queried as native Snapshot: '+kind)
                with patch.object(lab, 'DIRECTORY', Path(directory)), patch.object(lab, 'get', side_effect=get):
                    if fault == 'none':
                        lab.bind_snapshots(journal); self.assertFalse(journal.get('native_snapshots'))
                    else:
                        with self.assertRaisesRegex(ValueError, 'foreign CSI backup handle'): lab.bind_snapshots(journal)

    def test_recurring_backup_handoff_keeps_uid_spec_source_and_target_bound(self):
        from unittest.mock import Mock
        for fault in ('none', 'timestamp', 'gone', 'uid', 'spec', 'label', 'source', 'target'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                identity = {'snapshotName': 'observed-snapshot', 'backupMode': 'incremental',
                            'labels': {'RecurringJob': 'owned-schedule'}}
                journal = {'volume': 'pvc-owned', 'backups': {'backup-owned': {
                    'uid': 'backup-uid', 'recurring_job': 'owned-schedule', 'spec': copy.deepcopy(identity)}}}
                backup = {'metadata': {'uid': 'backup-uid', 'labels': {
                    'backup-target': 'default', 'backup-volume': 'pvc-owned'}}, 'spec': copy.deepcopy(identity)}
                if fault == 'timestamp': backup['spec']['syncRequestedAt'] = 'new-poll'
                elif fault == 'uid': backup['metadata']['uid'] = 'replacement'
                elif fault == 'spec': backup['spec']['snapshotName'] = 'foreign-snapshot'
                elif fault == 'label': backup['spec']['labels']['RecurringJob'] = 'foreign-schedule'
                elif fault == 'source': backup['metadata']['labels']['backup-volume'] = 'foreign-volume'
                elif fault == 'target': backup['metadata']['labels']['backup-target'] = 'foreign-target'
                def get(kind, name='', namespace=''):
                    if kind == 'backups.longhorn.io': return None if fault == 'gone' else backup
                    if kind == 'backupvolumes.longhorn.io': return {'items': []}
                    raise AssertionError(kind)
                product = Mock()
                with patch.object(lab, 'DIRECTORY', Path(directory)), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'load_helper', return_value=product), patch.object(lab, 'remove') as delete:
                    if fault in ('none', 'timestamp', 'gone'):
                        lab.bind_backups(journal)
                        persisted = json.loads((Path(directory) / 'longhorn-install-business.json').read_text())
                        self.assertEqual(persisted['backups']['backup-owned']['uid'], 'backup-uid')
                        self.assertEqual(persisted['backups']['backup-owned']['spec'], identity)
                    else:
                        with self.assertRaises(ValueError): lab.bind_backups(journal)
                        self.assertFalse((Path(directory) / 'longhorn-install-business.json').exists())
                    product.check_request.assert_not_called()
                    delete.assert_not_called()

    def test_backup_cleanup_checks_all_uids_before_native_delete_and_waits_for_recovery(self):
        from unittest.mock import Mock
        for fault in ('none', 'backup-replaced', 'volume-replaced', 'target-changed'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                journal = {'volume': 'pvc-owned', 'backups': {'backup-owned': {'uid': 'backup-uid', 'request': {'kind': 'backup'}}},
                           'backup_volume': {'name': 'native-volume', 'uid': 'volume-uid'}}
                backup = {'metadata': {'name': 'backup-owned', 'uid': 'replacement' if fault == 'backup-replaced' else 'backup-uid',
                    'labels': {'backup-target': 'default', 'backup-volume': 'pvc-owned'}}}
                volume = {'metadata': {'name': 'native-volume', 'uid': 'replacement' if fault == 'volume-replaced' else 'volume-uid'},
                    'spec': {'volumeName': 'pvc-owned', 'backupTargetName': 'foreign' if fault == 'target-changed' else 'default'}}
                objects = {'backups.longhorn.io': backup, 'backupvolumes.longhorn.io': volume}
                def get(kind, name='', namespace=''):
                    return objects[kind] if name else {'items': [objects[kind]] if objects[kind] else []}
                def remove(obj):
                    kind = 'backups.longhorn.io' if obj['metadata']['name'] == 'backup-owned' else 'backupvolumes.longhorn.io'
                    objects[kind] = None
                with patch.object(lab, 'DIRECTORY', Path(directory)), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'load_helper', return_value=Mock()), patch.object(lab, 'remove', side_effect=remove) as delete, \
                        patch.object(lab, 'wait', side_effect=lambda label, timeout, ready: self.assertTrue(ready())), \
                        redirect_stdout(io.StringIO()):
                    if fault == 'none':
                        lab.cleanup_backups(journal); self.assertEqual(delete.call_count, 2)
                        self.assertFalse(any(objects.values()))
                    else:
                        with self.assertRaises(ValueError): lab.cleanup_backups(journal)
                        delete.assert_not_called()

    def test_backup_volume_delete_refreshes_only_exact_rv_conflicts(self):
        import copy
        journal = {'volume': 'pvc-owned'}
        row = {'name': 'native-volume', 'uid': 'owned-uid'}
        original = {'metadata': {'uid': 'owned-uid', 'resourceVersion': '810769'},
                    'spec': {'volumeName': 'pvc-owned', 'backupTargetName': 'default'}}
        changed = copy.deepcopy(original)
        changed['metadata']['resourceVersion'] = '810774'
        changed['status'] = {'lastSyncedAt': 'current'}
        conflict = RuntimeError('private native delete error')
        conflict.stderr = ('Error from server (Conflict): Operation cannot be fulfilled on BackupVolume.longhorn.io '
            '"native-volume": the ResourceVersion in the precondition (810769) does not match the ResourceVersion '
            'in record (810774). The object might have been modified')
        for fault in ('none', 'uid', 'volume', 'target', 'other-error', 'forbidden', 'uid-conflict', 'absent'):
            with self.subTest(fault=fault):
                current = copy.deepcopy(changed)
                if fault == 'uid': current['metadata']['uid'] = 'replacement'
                elif fault == 'volume': current['spec']['volumeName'] = 'foreign'
                elif fault == 'target': current['spec']['backupTargetName'] = 'foreign'
                error = RuntimeError('other native failure') if fault == 'other-error' else copy.copy(conflict)
                if fault == 'forbidden': error.stderr = 'Error from server (Forbidden)'
                elif fault == 'uid-conflict': error.stderr = conflict.stderr.replace('ResourceVersion', 'UID')
                observed = []
                def bounded_wait(label, timeout, ready):
                    self.assertEqual(timeout, 60)
                    self.assertFalse(ready())
                    self.assertTrue(ready())
                def remove(obj):
                    observed.append(copy.deepcopy(obj))
                    if len(observed) == 1: raise error
                with patch.object(lab, 'get', side_effect=[original, None if fault == 'absent' else current]), \
                        patch.object(lab, 'remove', side_effect=remove), patch.object(lab, 'wait', side_effect=bounded_wait):
                    if fault in ('none', 'absent'):
                        lab.remove_backup_volume(journal, row)
                        self.assertEqual(len(observed), 1 if fault == 'absent' else 2)
                        if fault == 'none': self.assertEqual(observed[1]['metadata']['resourceVersion'], '810774')
                    else:
                        with self.assertRaises((ValueError, RuntimeError)):
                            lab.remove_backup_volume(journal, row)
                        self.assertEqual(len(observed), 1)

    def test_backup_volume_delete_never_retries_without_timeout(self):
        def wait(label, timeout, ready):
            self.assertEqual(timeout, 60)
            for _ in range(3): self.assertFalse(ready())
            raise TimeoutError('bounded conflict')
        current = {'metadata': {'uid': 'owned-uid', 'resourceVersion': '1'},
                   'spec': {'volumeName': 'pvc-owned', 'backupTargetName': 'default'}}
        error = RuntimeError('conflict')
        error.stderr = ('Error from server (Conflict): Operation cannot be fulfilled on BackupVolume.longhorn.io '
            '"native-volume": the ResourceVersion in the precondition (1) does not match the ResourceVersion in record (2)')
        with patch.object(lab, 'get', return_value=current), patch.object(lab, 'remove', side_effect=error), \
                patch.object(lab, 'wait', side_effect=wait):
            with self.assertRaises(TimeoutError):
                lab.remove_backup_volume({'volume': 'pvc-owned'}, {'name': 'native-volume', 'uid': 'owned-uid'})

    def test_backup_paths_match_pinned_sha512_layout_and_deduplicate_blocks(self):
        prefix = 'backupstore/volumes/12/14/pvc-example/'
        self.assertEqual(backups.volume_prefix('pvc-example'), prefix)
        metadata = {'Name': 'backup-example', 'VolumeName': 'pvc-example', 'CreatedTime': '2026-10-19T00:00:00Z',
                    'Blocks': [{'Offset': offset, 'BlockChecksum': 'a'*64} for offset in (0, 2*1024**2)]}
        self.assertEqual(backups.referenced_blocks(metadata, 'pvc-example', 'backup-example', 2*1024**3),
                         [prefix + 'blocks/aa/aa/' + 'a'*64 + '.blk'])
        for name in ('../volume', '', 'volume/name'):
            with self.subTest(name=name), self.assertRaises(ValueError): backups.volume_prefix(name)

    def test_backup_metadata_checks_identity_offsets_and_native_block_size(self):
        metadata = {'Name': 'backup-example', 'VolumeName': 'pvc-example', 'CreatedTime': '2026-10-19T00:00:00Z',
                    'Blocks': [{'Offset': 0, 'BlockChecksum': 'a'*64}]}
        for fault in ('name', 'volume', 'incomplete', 'empty', 'boolean', 'negative', 'past-end', 'unaligned',
                      'duplicate', 'checksum', 'block-size', 'misaligned-16m'):
            changed = copy.deepcopy(metadata)
            if fault == 'name': changed['Name'] = 'foreign'
            if fault == 'volume': changed['VolumeName'] = 'foreign'
            if fault == 'incomplete': changed['CreatedTime'] = ''
            if fault == 'empty': changed['Blocks'] = []
            if fault == 'boolean': changed['Blocks'][0]['Offset'] = False
            if fault == 'negative': changed['Blocks'][0]['Offset'] = -1
            if fault == 'past-end': changed['Blocks'][0]['Offset'] = 2*1024**3
            if fault == 'unaligned': changed['Blocks'][0]['Offset'] = 1
            if fault == 'duplicate': changed['Blocks'] *= 2
            if fault == 'checksum': changed['Blocks'][0]['BlockChecksum'] = '../foreign'
            if fault == 'block-size': changed['Parameters'] = {'backup-block-size': 'bad'}
            if fault == 'misaligned-16m':
                changed['Parameters'] = {'backup-block-size': '16Mi'}
                changed['Blocks'][0]['Offset'] = 2*1024**2
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                backups.referenced_blocks(changed, 'pvc-example', 'backup-example', 2*1024**3)
        metadata['Parameters'] = {'backup-block-size': '16Mi'}
        metadata['Blocks'][0]['Offset'] = 16*1024**2
        self.assertEqual(len(backups.referenced_blocks(metadata, 'pvc-example', 'backup-example', 2*1024**3)), 1)

    def test_backup_object_readback_is_nonempty_and_always_removes_only_its_temporary(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        key = backups.volume_prefix('pvc-example') + 'backups/backup_example.cfg'
        for fault in ('none', 'copy', 'stat', 'empty', 'checksum', 'copy-and-clean'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                calls = []
                def execute(argv):
                    calls.append(argv)
                    if 'stat' in argv:
                        if fault == 'stat': raise ValueError('first-stat-failure')
                        output = '0' if fault == 'empty' else '17'
                    elif 'sha256sum' in argv: output = 'bad' if fault == 'checksum' else 'a'*64 + '  temporary\n'
                    else:
                        if fault == 'copy-and-clean': raise ValueError('cleanup-failure')
                        output = ''
                    return subprocess.CompletedProcess(argv, 0, output, '')
                client = SimpleNamespace(DIRECTORY=Path(directory), execute=execute, scoped_cleanup=lab.scoped_cleanup)
                store = backups.Store(client)
                with patch.object(store, 'receipt', return_value={'bucket': 'owned'}), \
                        patch.object(store, 'inspect', return_value={'Id': 'original'}), \
                        patch.object(store, 'mc', side_effect=ValueError('first-copy-failure') if fault.startswith('copy') else None) as mc, \
                        redirect_stderr(io.StringIO()):
                    if fault == 'none': self.assertEqual(store.object_digest(key), {'size': 17, 'sha256': 'a'*64})
                    else:
                        with self.assertRaisesRegex(ValueError, 'first-copy-failure' if fault.startswith('copy') else
                                                    'first-stat-failure' if fault == 'stat' else 'empty or unreadable'):
                            store.object_digest(key)
                    destination = mc.call_args.args[-1]
                    self.assertTrue(destination.startswith('/tmp/longhorn-backup-object-'))
                    self.assertEqual(calls[-1], ['docker', 'exec', 'original', 'rm', '-f', '--', destination])

    def test_recurring_backup_origin_reads_exact_native_config_and_rejects_each_foreign_field(self):
        from types import SimpleNamespace
        for fault in ('none', 'Name', 'VolumeName', 'SnapshotName', 'Labels', 'CreatedTime', 'Blocks'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                store = backups.Store(SimpleNamespace(DIRECTORY=Path(directory)))
                metadata = {'Name': 'backup-example', 'VolumeName': 'pvc-example', 'SnapshotName': 'snapshot-example',
                            'Labels': {'RecurringJob': 'job-example'}, 'CreatedTime': '2026-10-19T00:00:00Z',
                            'Blocks': [{'Offset': 0, 'BlockChecksum': 'a'*64}]}
                if fault != 'none': metadata[fault] = {} if fault == 'Labels' else [] if fault == 'Blocks' else 'foreign' if fault != 'CreatedTime' else ''
                raw = json.dumps(metadata)
                with patch.object(store, 'receipt', return_value={'bucket': 'owned'}), \
                        patch.object(store, 'mc', return_value=raw) as mc, patch.object(store, 'object_digest') as blocks:
                    if fault == 'none':
                        self.assertEqual(store.backup_origin('pvc-example', 'backup-example', 'snapshot-example', 'job-example'),
                                         hashlib.sha256(raw.encode()).hexdigest())
                    else:
                        with self.assertRaisesRegex(ValueError, 'backend provenance differs'):
                            store.backup_origin('pvc-example', 'backup-example', 'snapshot-example', 'job-example')
                    mc.assert_called_once_with('cat', 'lh/owned/' + backups.volume_prefix('pvc-example') + 'backups/backup_backup-example.cfg')
                    blocks.assert_not_called()

    def test_backup_receipt_reads_each_referenced_object_and_rejects_changed_metadata(self):
        from types import SimpleNamespace
        for fault in ('none', 'metadata-changed', 'wrong-volume', 'wrong-size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                store = backups.Store(SimpleNamespace(DIRECTORY=Path(directory)))
                metadata = {'Name': 'backup-example', 'VolumeName': 'pvc-example', 'CreatedTime': '2026-10-19T00:00:00Z',
                            'IsIncremental': True, 'NewlyUploadedDataSize': '42', 'ReUploadedDataSize': '7',
                            'Blocks': [{'Offset': offset, 'BlockChecksum': char*64} for offset, char in ((0, 'a'), (2*1024**2, 'b'))]}
                raw = json.dumps(metadata); keys = []
                def digest(key):
                    keys.append(key)
                    return {'size': len(raw.encode()), 'sha256': hashlib.sha256(raw.encode()).hexdigest() if fault != 'metadata-changed' else 'c'*64}
                volume = {'Name': 'foreign' if fault == 'wrong-volume' else 'pvc-example',
                          'Size': '1' if fault == 'wrong-size' else str(2*1024**3)}
                with patch.object(store, 'receipt', return_value={'bucket': 'owned'}), \
                        patch.object(store, 'mc', side_effect=[raw, json.dumps(volume)]), \
                        patch.object(store, 'object_digest', side_effect=digest):
                    if fault == 'none':
                        receipt = store.backup_receipt('pvc-example', 'backup-example', 2*1024**3)
                        self.assertTrue(receipt['incremental']); self.assertEqual(receipt['newly_uploaded_bytes'], 42)
                        self.assertEqual(len(receipt['objects']), 3)
                        self.assertEqual(keys[1:], backups.referenced_blocks(metadata, 'pvc-example', 'backup-example', 2*1024**3))
                    else:
                        with self.assertRaises(ValueError): store.backup_receipt('pvc-example', 'backup-example', 2*1024**3)
                        if fault.startswith('wrong-'): self.assertFalse(keys)

    def test_recurring_qualification_uses_product_updates_preserves_uid_and_restores_baseline(self):
        for fault in ('none', 'replacement', 'lost-response'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root/'config.yml').write_text('longhorn_recurring_jobs: []\n')
                objects = {}; calls = []
                def get(kind, name='', namespace=''):
                    if kind == 'recurringjobs.longhorn.io':
                        return copy.deepcopy(objects.get(name)) if name else {'items': copy.deepcopy(list(objects.values()))}
                    if kind == 'cronjobs':
                        return {'items': [{'metadata': {'ownerReferences': [{'uid': obj['metadata']['uid']}]},
                                           'spec': {'schedule': obj['spec']['cron']}} for obj in objects.values()]}
                    raise AssertionError(kind)
                def product(pid, requests, *, recurring_jobs):
                    calls.append(copy.deepcopy(recurring_jobs))
                    if not recurring_jobs:
                        objects.clear(); return
                    job = recurring_jobs[0]; name = job['name']
                    uid = 'replacement' if fault == 'replacement' and len(calls) == 2 else 'original'
                    objects[name] = {'metadata': {'name': name, 'uid': uid,
                        'labels': {'app.kubernetes.io/managed-by': 'kubeauto'}}, 'spec': job}
                    if fault == 'lost-response': raise ValueError('first-product-failure')
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'installation_health'), \
                        patch.object(lab, 'recurring_update_dry_run'), \
                        patch.object(lab, 'owner', return_value={'run_id': 'run', 'cluster_uid': 'cluster'}), \
                        patch.object(lab, 'restore_request_input'), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'run_requests', side_effect=product), \
                        patch.object(lab, 'wait', side_effect=lambda label, timeout, ready: self.assertTrue(ready())), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    if fault == 'none':
                        lab.qualify_recurring_declarations(12)
                        self.assertFalse(objects); self.assertEqual(calls[-1], [])
                        self.assertFalse((root/'longhorn-recurring-qualification.json').exists())
                    else:
                        with self.assertRaisesRegex(ValueError, 'first-product-failure' if fault == 'lost-response' else 'identity changed'):
                            lab.qualify_recurring_declarations(12)
                        self.assertTrue(objects)
                        self.assertTrue((root/'longhorn-recurring-qualification.json').exists())
                        self.assertNotIn([], calls)

    def test_recurring_native_update_dry_run_requires_original_uid_and_does_not_persist(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('none', 'replacement', 'persisted', 'admission'):
            current = {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'RecurringJob',
                       'metadata': {'name': 'owned', 'uid': 'original', 'resourceVersion': 'current'},
                       'spec': {'cron': '0 0 1 1 *'}}
            after = copy.deepcopy(current)
            if fault == 'replacement': current['metadata']['uid'] = 'foreign'
            if fault == 'persisted': after['spec']['cron'] = '0 1 1 1 *'
            api = Mock()
            product = SimpleNamespace(run=Mock(return_value='{}'), API=Mock(return_value=api))
            def replace(obj):
                self.assertEqual(obj['metadata'], current['metadata'])
                product.run(['kubectl', 'replace', '-f', '-', '-o', 'json'], stdin=json.dumps(obj))
                if fault == 'admission': raise ValueError('native-admission-rejection')
                return obj
            api.replace.side_effect = replace; output = io.StringIO()
            original_run = product.run
            with self.subTest(fault=fault), patch.object(lab, 'load_helper', return_value=product), \
                    patch.object(lab, 'get', side_effect=[current, after]), redirect_stdout(output):
                if fault == 'none':
                    lab.recurring_update_dry_run({'name': 'owned', 'uid': 'original'}, '0 1 1 1 *')
                    self.assertIn('persisted_change=false', output.getvalue())
                    self.assertIn('--dry-run=server', original_run.call_args.args[0])
                else:
                    with self.assertRaises(ValueError):
                        lab.recurring_update_dry_run({'name': 'owned', 'uid': 'original'}, '0 1 1 1 *')
                    self.assertNotIn('VERIFIED', output.getvalue())
                if fault == 'replacement': api.replace.assert_not_called()

    def test_recurring_cleanup_checks_foreign_baseline_before_product_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'longhorn-recurring-qualification.json').write_text(json.dumps(
                {'run_id': 'run', 'cluster_uid': 'cluster', 'name': 'owned', 'uid': 'own-uid', 'baseline': {}}))
            with patch.object(lab, 'DIRECTORY', root), \
                    patch.object(lab, 'owner', return_value={'run_id': 'run', 'cluster_uid': 'cluster'}), \
                    patch.object(lab, 'restore_request_input'), patch.object(lab, 'get', return_value=None), \
                    patch.object(lab, 'recurring_identity', return_value={'foreign': {'uid': 'changed'}}), \
                    patch.object(lab, 'run_requests') as product:
                with self.assertRaisesRegex(ValueError, 'baseline changed before cleanup'):
                    lab.cleanup_recurring_qualification(12)
                product.assert_not_called()

    def test_backup_prepare_writes_real_env_lines_private_and_out_of_argv(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            client = SimpleNamespace(DIRECTORY=Path(directory), core_identity=Mock(),
                owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster'}),
                publication_records=Mock(return_value=([], [{'key': backups.IMAGE, 'platforms': {'linux/amd64': 'manifest'}}])),
                cache_artifacts=Mock(), published_manifest=Mock(return_value={'config': {'digest': 'sha256:published'}}),
                write_private=lab.write_private, tls_fixture=Mock(return_value={'tls.crt': 'certificate', 'tls.key': 'private', 'ca.crt': 'ca'}),
                wait=Mock())
            def command(argv, **kwargs):
                if argv[:3] == ['docker', 'image', 'inspect']: output = '[{"Id":"sha256:published"}]'
                elif argv[:2] == ['docker', 'run']: output = 'owned-container\n'
                else: output = ''
                return subprocess.CompletedProcess(argv, 0, output, '')
            client.execute = Mock(side_effect=command)
            store = backups.Store(client)
            with patch.object(backups.socket, 'socket'), patch.object(store, 'mc'):
                store.prepare()
            server = (store.directory/'server.env').read_text()
            mc = (store.directory/'mc.env').read_text()
            self.assertEqual(len(server.splitlines()), 2)
            self.assertEqual(len(mc.splitlines()), 2)
            self.assertEqual(mc.splitlines()[0], 'MC_CONFIG_DIR=/tmp/longhorn-mc')
            self.assertNotIn('\\n', server + mc)
            for filename in ('server.env', 'mc.env', 'certs/private.key'):
                self.assertEqual((store.directory/filename).stat().st_mode & 0o777, 0o600)
            arguments = [str(arg) for call in client.execute.call_args_list for arg in call.args[0]]
            for line in server.splitlines():
                self.assertFalse(any(line.split('=', 1)[1] in arg for arg in arguments))

    def test_backup_product_binding_restores_only_original_target_and_credential(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('none', 'target-uid', 'namespace-uid', 'target-spec', 'secret-uid', 'secret-data', 'remaining-backup'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                original = {'backupTargetURL': '', 'credentialSecret': '', 'pollInterval': '5m0s', 'syncRequestedAt': None}
                target = {'metadata': {'uid': 'target', 'resourceVersion': 'current'}, 'spec': copy.deepcopy(original),
                          'status': {'available': False}}
                namespace = {'metadata': {'uid': 'namespace'}}; secrets = {}; objects = []
                def get(kind, name='', namespace=''):
                    if kind == 'backuptargets.longhorn.io': return copy.deepcopy(target)
                    if kind == 'namespace': return {'metadata': {'uid': 'foreign' if fault == 'namespace-uid' and secrets else 'namespace'}}
                    if kind == 'secret': return copy.deepcopy(secrets.get(name))
                    if kind in backups.BACKUP_RESOURCES: return {'items': objects}
                    raise AssertionError(kind)
                def command(*argv, stdin):
                    if argv[0] == 'create':
                        obj = json.loads(stdin); obj['metadata']['uid'] = 'secret'
                        secrets[obj['metadata']['name']] = obj
                        return subprocess.CompletedProcess(argv, 0, json.dumps(obj), '')
                    patch_data = json.loads(stdin)
                    self.assertEqual(patch_data[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'target'})
                    self.assertEqual(patch_data[1]['value'], 'current')
                    target['spec'] = patch_data[2]['value']; target['status']['available'] = False
                    return subprocess.CompletedProcess(argv, 0, '{}', '')
                client = SimpleNamespace(DIRECTORY=Path(directory), get=Mock(side_effect=get),
                    write_private=lab.write_private, kubectl=Mock(side_effect=command),
                    wait=Mock(side_effect=lambda label, timeout, ready: self.assertTrue(ready())),
                    remove=Mock(side_effect=lambda obj: secrets.pop(obj['metadata']['name'])))
                store = backups.Store(client); store.directory.mkdir(); (store.directory/'cas').mkdir()
                (store.directory/'server.env').write_text('MINIO_ROOT_USER=private-user\nMINIO_ROOT_PASSWORD=private-password\n')
                certificate = '-----BEGIN CERTIFICATE-----\nprivate-ca\n-----END CERTIFICATE-----'
                (store.directory/'cas/ca.crt').write_text(certificate + '\n')
                store.record.write_text(json.dumps({'run_id': 'run', 'name': 'owned-backup', 'bucket': 'owned-backup'}))
                with patch.object(store, 'receipt', side_effect=lambda: json.loads(store.record.read_text())), redirect_stdout(io.StringIO()):
                    config = store.product_configuration()
                    self.assertEqual(config, {'longhorn_backup_target': 's3://owned-backup@us-east-1/',
                                              'longhorn_backup_secret': 'owned-backup'})
                    self.assertEqual(target['spec'], original, 'activation must remain product-owned')
                    secret_data = secrets['owned-backup']['data']
                    self.assertEqual(base64.b64decode(secret_data['AWS_CERT']).decode(), certificate)
                    row = json.loads(store.record.read_text()); target['spec'] = row['product_binding']['fixture_spec']
                    target['spec']['syncRequestedAt'] = 'native-poller-time'
                    target['status']['available'] = True
                    if fault == 'target-uid': target['metadata']['uid'] = 'replacement'
                    if fault == 'target-spec': target['spec']['credentialSecret'] = 'foreign'
                    if fault == 'secret-uid': secrets['owned-backup']['metadata']['uid'] = 'replacement'
                    if fault == 'secret-data': secrets['owned-backup']['data'] = {}
                    if fault == 'remaining-backup': objects.append({'metadata': {'uid': 'backup'}})
                    client.kubectl.reset_mock()
                    if fault == 'none':
                        store.restore_product()
                        self.assertEqual(backups.target_configuration(target['spec']), backups.target_configuration(original))
                        self.assertEqual(target['spec']['syncRequestedAt'], 'native-poller-time')
                        self.assertFalse(secrets)
                        self.assertNotIn('product_binding', json.loads(store.record.read_text()))
                    else:
                        with self.assertRaises(ValueError): store.restore_product()
                        client.kubectl.assert_not_called(); client.remove.assert_not_called()
                        self.assertIn('product_binding', json.loads(store.record.read_text()))

    def test_backup_product_binding_rejects_in_use_target_before_credential_creation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('url', 'credential', 'backup', 'secret'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                def get(kind, name='', namespace=''):
                    if kind == 'backuptargets.longhorn.io':
                        return {'metadata': {'uid': 'target'}, 'spec': {'backupTargetURL': 's3://foreign/' if fault == 'url' else '',
                                'credentialSecret': 'foreign' if fault == 'credential' else ''}}
                    if kind == 'namespace': return {'metadata': {'uid': 'namespace'}}
                    if kind == 'secret': return {'metadata': {'uid': 'foreign'}} if fault == 'secret' else None
                    return {'items': [{}] if fault == 'backup' else []}
                client = SimpleNamespace(DIRECTORY=Path(directory), get=get, write_private=Mock(), kubectl=Mock())
                store = backups.Store(client)
                with patch.object(store, 'receipt', return_value={'name': 'owned', 'bucket': 'owned'}), self.assertRaises(ValueError):
                    store.product_configuration()
                client.write_private.assert_not_called(); client.kubectl.assert_not_called()

    def test_backup_fixture_qualification_always_recovers_and_preserves_first_failure(self):
        from unittest.mock import Mock
        for fault in ('none', 'prepare', 'qualify', 'cleanup'):
            with self.subTest(fault=fault):
                store = Mock()
                if fault != 'none': getattr(store, fault).side_effect = ValueError(fault)
                with patch.object(lab, 'backup_store', return_value=store):
                    if fault == 'none': lab.qualify_backup_store()
                    else:
                        with self.assertRaisesRegex(ValueError, fault): lab.qualify_backup_store()
                store.cleanup.assert_called_once()
                self.assertEqual(store.qualify.call_count, 0 if fault == 'prepare' else 1)

    def test_external_backup_container_rejects_changed_identity_mounts_and_ports(self):
        directory = Path('/owned/backup')
        receipt = {'name': 'owned-backup', 'run_id': 'run', 'image_id': 'sha256:published', 'container_id': 'original'}
        obj = {'Id': 'original', 'Name': '/owned-backup', 'Image': 'sha256:published',
               'Config': {'Labels': {'kubeauto.io/longhorn-backup-run': 'run'}},
               'Mounts': [{'Type': 'bind', 'Source': str(directory/source), 'Destination': destination, 'RW': source == 'data'}
                          for source, destination in (('data', '/data'), ('certs', '/certs'), ('cas', '/tmp/longhorn-mc/certs/CAs'))],
               'HostConfig': {'PortBindings': {'9000/tcp': [{'HostIp': backups.HOST, 'HostPort': str(backups.PORT)}],
                                             '9001/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(backups.CONSOLE_PORT)}]}}}
        self.assertEqual(backups.validate_container(obj, receipt, directory), 'original')
        for fault in ('id', 'name', 'image', 'label', 'mount', 'extra-mount', 'writable-cert', 'port'):
            changed = copy.deepcopy(obj)
            if fault == 'id': changed['Id'] = 'replacement'
            if fault == 'name': changed['Name'] = '/foreign'
            if fault == 'image': changed['Image'] = 'sha256:foreign'
            if fault == 'label': changed['Config']['Labels'] = {}
            if fault == 'mount': changed['Mounts'][0]['Source'] = '/foreign'
            if fault == 'extra-mount': changed['Mounts'].append(changed['Mounts'][0])
            if fault == 'writable-cert': changed['Mounts'][1]['RW'] = True
            if fault == 'port': changed['HostConfig']['PortBindings']['9001/tcp'][0]['HostIp'] = '0.0.0.0'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                backups.validate_container(changed, receipt, directory)

    def test_external_backup_cleanup_preserves_consumed_replaced_or_symlinked_data(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for fault in ('none', 'consumed', 'replaced', 'symlink', 'wrong-run'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = SimpleNamespace(DIRECTORY=root, owner=Mock(return_value={'run_id': 'run', 'cluster_uid': 'cluster'}),
                                         execute=Mock(), get=Mock(return_value=None))
                store = backups.Store(client); store.directory.mkdir(); identity = store.directory.stat()
                row = {'run_id': 'foreign' if fault == 'wrong-run' else 'run', 'cluster_uid': 'cluster',
                       'name': 'longhorn-backup-run', 'bucket': 'longhorn-backup-run', 'directory_identity': [identity.st_dev, identity.st_ino]}
                store.record.write_text(json.dumps(row)); (store.directory/'original').write_text('data')
                if fault == 'consumed': client.get.return_value = {'spec': {'backupTargetURL': 's3://longhorn-backup-run@us-east-1/'}}
                if fault == 'symlink': (store.directory/'link').symlink_to(root)
                with patch.object(store, 'inspect', side_effect=ValueError('replacement') if fault == 'replaced' else None,
                                  return_value=None):
                    if fault == 'none':
                        store.cleanup(); self.assertFalse(store.record.exists()); self.assertFalse(store.directory.exists())
                    else:
                        with self.assertRaises(ValueError): store.cleanup()
                        self.assertTrue(store.record.exists()); self.assertEqual((store.directory/'original').read_text(), 'data')
                client.execute.assert_not_called()

    def test_external_backup_tls_fixture_includes_jumper_without_changing_grpc_default(self):
        from cryptography import x509
        for extra in ((), (backups.HOST,)):
            certs = lab.tls_fixture(extra_ips=extra)
            cert = x509.load_pem_x509_certificate(certs['tls.crt'].encode())
            ips = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)
            self.assertEqual({str(ip) for ip in ips}, {'127.0.0.1', *extra})

    def test_bootstrap_qualification_uses_only_server_dry_run_and_checks_metadata(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        nodes = {address: {'metadata': {'name': 'node-' + str(i), 'uid': 'uid-' + str(i),
                                      'labels': {}, 'annotations': {}, 'resourceVersion': 'old'}}
                 for i, address in enumerate(lab.NODES)}
        for fault in ('none', 'command', 'metadata'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path = root/'yml/longhorn/input.json'; path.parent.mkdir(parents=True)
                path.write_text('{}')
                api = Mock(prefix=['kubectl', '--request-timeout=30s'])
                def get(kind, name):
                    node = copy.deepcopy(next(n for n in nodes.values() if n['metadata']['name'] == name))
                    node['metadata']['resourceVersion'] = 'new-heartbeat'
                    if fault == 'metadata': node['metadata']['labels']['foreign'] = 'changed'
                    return node
                api.get.side_effect = get
                product = SimpleNamespace(validate=Mock(), API=Mock(return_value=api),
                    node_map=Mock(return_value=nodes), run=Mock(side_effect=RuntimeError('refused') if fault == 'command' else None))
                product.bootstrap_node = lambda c, api, address, node, receipt: api.patch(
                    'node', node['metadata']['name'], {'metadata': {'resourceVersion': 'current'}})
                spec = SimpleNamespace(loader=Mock())
                output = io.StringIO()
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'core_identity'), \
                        patch.object(lab.importlib.util, 'spec_from_file_location', return_value=spec), \
                        patch.object(lab.importlib.util, 'module_from_spec', return_value=product), \
                        patch('sys.stdout', output):
                    if fault == 'none': lab.node_bootstrap_dry_run()
                    else:
                        with self.assertRaises((RuntimeError, ValueError)): lab.node_bootstrap_dry_run()
                self.assertEqual('LONGHORN_NODE_BOOTSTRAP_DRY_RUN_PASS' in output.getvalue(), fault == 'none')
                for call in product.run.call_args_list:
                    self.assertIn('--dry-run=server', call.args[0])
                    self.assertEqual(json.loads(call.kwargs['stdin']), {'metadata': {'resourceVersion': 'current'}})
                self.assertEqual(product.run.call_count, 1 if fault == 'command' else 6)

    def test_lab_get_uses_get_subcommand_namespace_flags(self):
        for namespace, scope in (('*', ['-A']), ('longhorn-system', ['-n', 'longhorn-system']), ('', [])):
            with self.subTest(namespace=namespace), patch.object(lab, 'kubectl', return_value=
                    subprocess.CompletedProcess([], 0, '{"items":[]}', '')) as run:
                self.assertEqual(lab.get('deployments', namespace=namespace), {'items': []})
                self.assertEqual(run.call_args.args, ('get', 'deployments', *scope, '-o', 'json'))

    def test_disk_parent_fixture_creates_only_missing_parent_and_preserves_foreign_state(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); parent = root/'data'; row = {'path': str(parent/'longhorn')}
            def call(action, *, check=True):
                return subprocess.run([sys.executable, '-c', lab.disk_parent_script(), action],
                                      input=json.dumps(row), text=True, capture_output=True, check=check)
            self.assertIsNone(json.loads(call('probe').stdout))
            row.update(parent_created=True, parent_identity=None)
            row['parent_identity'] = json.loads(call('create').stdout)
            self.assertTrue(parent.is_dir()); call('check')
            foreign = parent/'foreign'; foreign.write_text('preserve')
            self.assertNotEqual(call('check', check=False).returncode, 0)
            self.assertNotEqual(call('clean', check=False).returncode, 0)
            self.assertEqual(foreign.read_text(), 'preserve'); foreign.unlink()
            # Even an empty replacement directory cannot acquire ownership.
            parent.rename(root/'original-parent'); parent.mkdir()
            self.assertNotEqual(call('clean', check=False).returncode, 0)
            parent.rmdir(); (root/'original-parent').rename(parent)
            call('clean'); self.assertFalse(parent.exists()); call('clean')
            parent.mkdir(); row.update(parent_created=False, parent_identity=json.loads(call('probe').stdout))
            call('check'); call('clean'); self.assertTrue(parent.is_dir())
            row.update(parent_created=True, parent_identity=None)
            self.assertNotEqual(call('clean', check=False).returncode, 0, 'lost create response must not authorize removal')
            parent.rmdir(); parent.symlink_to(root, target_is_directory=True)
            self.assertNotEqual(call('probe', check=False).returncode, 0)

    def test_core_smoke_requires_real_http_marker_success_cleanup_and_current_identity(self):
        for fault in ('none', 'rc', 'http-marker', 'namespace-left'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); state = {'core_smoke_fingerprint': 'old'}
                output = 'KUBERNETES_PRODUCTION_SMOKE_PASS server_node=a client_node=b\n'
                if fault != 'http-marker': output += 'PRODUCTION_SMOKE_HTTP_RW_OK\n'
                completed = subprocess.CompletedProcess([], 17 if fault == 'rc' else 0, output, '')
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'STATE', root/'owner.json'), \
                        patch.object(lab, 'core_identity'), patch.object(lab, 'owner', return_value=state), \
                        patch.object(lab, 'core_smoke_fingerprint', return_value='current'), patch.object(lab, 'cache_artifacts') as cache, \
                        patch.object(lab, 'get', side_effect=[None, {'metadata': {'uid': 'remaining'}} if fault == 'namespace-left' else None]), \
                        patch.object(lab, 'execute', return_value=completed) as execute:
                    if fault == 'none':
                        lab.core_smoke(); lab.verify_core_smoke()
                        self.assertEqual(state['core_smoke_fingerprint'], 'current')
                    else:
                        with self.assertRaises(ValueError): lab.core_smoke()
                        self.assertNotIn('core_smoke_fingerprint', state)
                        with self.assertRaises(ValueError): lab.verify_core_smoke()
                    cache.assert_called_once_with({'brinnatt/json-mock:v1.3.1'})
                    command = [str(x) for x in execute.call_args.args[0]]
                    self.assertIn('PRODUCTION_SMOKE_SERVER_NODE=longhorn-node-212', command)
                    self.assertIn('PRODUCTION_SMOKE_CLIENT_NODE=longhorn-node-165', command)
                    self.assertTrue(command[-1].endswith('/tests/helpers/kubernetes-production-smoke.sh'))
                    self.assertTrue((root/'core-smoke-result.json').exists())

    def test_retained_rebind_reads_original_bytes_and_rejects_replaced_bindings(self):
        for fault in ('none', 'released-pv', 'wrong-bound-pv', 'native-replaced', 'bad-bytes', 'hook', 'hook-failure'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                journal = {'namespace': 'owned', 'pv': 'source-pv', 'pv_uid': 'pv-uid', 'volume': 'source',
                           'volume_uid': 'volume-uid', 'objects': {'Pod/writer': 'writer-uid', 'PersistentVolumeClaim/data': 'old-claim'}}
                objects = {
                    ('pod', 'writer'): {'metadata': {'uid': 'writer-uid'}},
                    ('pvc', 'data'): {'metadata': {'uid': 'old-claim'}},
                    ('pv', 'source-pv'): {'metadata': {'name': 'source-pv', 'uid': 'foreign' if fault == 'released-pv' else 'pv-uid'},
                        'spec': {'claimRef': {'name': 'data', 'namespace': 'owned', 'uid': 'old-claim'}, 'storageClassName': 'longhorn',
                                 'persistentVolumeReclaimPolicy': 'Retain', 'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'source'}},
                        'status': {'phase': 'Bound'}},
                    ('volumes.longhorn.io', 'source'): {'metadata': {'uid': 'volume-uid'}}}
                patches = []
                def get(kind, name='', namespace=''):
                    if name: return objects.get((kind, name))
                    return {'items': [v for (k, _), v in objects.items() if k == kind]}
                def remove(obj):
                    key = next(k for k, value in objects.items() if value is obj); del objects[key]
                    if key == ('pvc', 'data'): objects['pv', 'source-pv']['status']['phase'] = 'Released'
                def create(obj):
                    kind = 'pvc' if obj['kind'] == 'PersistentVolumeClaim' else 'pod'; name = obj['metadata']['name']
                    obj = copy.deepcopy(obj); obj['metadata']['uid'] = name+'-uid'
                    obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}
                    objects[kind, name] = obj; journal['objects'][obj['kind']+'/'+name] = name+'-uid'; return obj
                def kubectl(*args, **kwargs):
                    if args[0] == 'patch':
                        operations = json.loads(args[-1]); patches.extend(operations)
                        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'pv-uid'})
                        self.assertEqual(operations[1]['value'], 'old-claim')
                        objects['pv', 'source-pv']['spec']['claimRef'] = operations[2]['value']
                        objects['pvc', 'rebound']['status']['phase'] = 'Bound'
                        if fault == 'wrong-bound-pv': objects['pvc', 'rebound']['spec']['volumeName'] = 'foreign'
                        if fault == 'native-replaced': objects['volumes.longhorn.io', 'source']['metadata']['uid'] = 'foreign'
                        return subprocess.CompletedProcess([], 0, '', '')
                    self.assertIn('rebound-reader', args)
                    return subprocess.CompletedProcess([], 0, 'corrupted' if fault == 'bad-bytes' else 'expected-hash', '')
                def wait(label, timeout, predicate):
                    result = predicate(); self.assertTrue(result, label); return result
                def before_rebind():
                    self.assertNotIn(('pvc', 'data'), objects)
                    self.assertNotIn(('pvc', 'rebound'), objects)
                    self.assertEqual(objects['pv', 'source-pv']['status']['phase'], 'Released')
                    self.assertEqual(objects['pv', 'source-pv']['spec']['claimRef']['uid'], 'old-claim')
                    self.assertFalse(patches)
                    if fault == 'hook-failure': raise ValueError('refusal was not proved')
                options = {'before_rebind': before_rebind} if fault in ('hook', 'hook-failure') else {}
                with patch.object(lab, 'DIRECTORY', Path(directory)), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'remove', side_effect=remove), patch.object(lab, 'kubectl', side_effect=kubectl), \
                        patch.object(lab, 'wait', side_effect=wait), patch.object(lab, 'healthy_replicas', return_value=['three']):
                    if fault in ('none', 'hook'):
                        lab.verify_retained_rebind(journal, create, 'payload', 'expected-hash', **options)
                        self.assertEqual(journal['claims']['rebound']['pv_uid'], 'pv-uid')
                    else:
                        with self.assertRaises(ValueError): lab.verify_retained_rebind(journal, create, 'payload', 'expected-hash', **options)
                        if fault == 'released-pv': self.assertFalse(patches)
                        if fault == 'hook-failure': self.assertNotIn(('pvc', 'rebound'), objects)

    def test_reclaim_suite_requires_both_policies_and_exact_class_refusals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            classes = [{'name': 'longhorn', 'reclaim_policy': 'Retain', 'default': False}]
            (root/'config.yml').write_text(lab.yaml.safe_dump({'longhorn_storage_classes': classes}))
            existing = {'other': {'annotations': {'storageclass.kubernetes.io/is-default-class': 'true'}}}
            with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'business') as business, \
                    patch.object(lab, 'class_identity', return_value=existing), patch.object(lab, 'run_requests') as requests, \
                    patch.object(lab, 'installation_health'):
                lab.reclaim_suite(123)
                self.assertEqual(business.call_args_list[0].args, ('reclaim',))
                self.assertEqual(business.call_args_list[1].kwargs, {'storage_class': 'longhorn-delete'})
                self.assertIn('immutable StorageClass', requests.call_args_list[0].kwargs['expected_refusal'])
                self.assertEqual(requests.call_args_list[1].kwargs['expected_refusal'], 'refusing to replace another default StorageClass')
                self.assertEqual(requests.call_args_list[1].kwargs['storage_classes'][0]['default'], True)

    def test_default_class_cleanup_refuses_replacement_and_unknown_create_response(self):
        for identity in ({'uid': 'original'}, {'uid': 'replacement'}, None):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); receipt = root/'longhorn-reclaim-class.json'
                receipt.write_text(json.dumps({'run_id': 'run', 'name': 'longhorn-default-test', 'identity': identity}))
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner', return_value={'run_id': 'run'}), \
                        patch.object(lab, 'get', return_value={'metadata': {'uid': 'original'}}), \
                        patch.object(lab, 'class_identity', return_value={'longhorn-default-test': {'uid': 'original'}}), \
                        patch.object(lab, 'remove') as remove, patch.object(lab, 'wait'):
                    if identity == {'uid': 'original'}:
                        lab.cleanup_reclaim_class(); remove.assert_called_once(); self.assertFalse(receipt.exists())
                    else:
                        with self.assertRaises(ValueError): lab.cleanup_reclaim_class()
                        remove.assert_not_called(); self.assertTrue(receipt.exists())

    def test_native_recovery_stops_before_disk_or_host_changes_on_unmet_prerequisite(self):
        for fault in ('none', 'credential', 'native-failure', 'shared-controller', 'foreign-namespace', 'node-residue',
                      'lease-replacement', 'lease-appeared', 'delayed-pods', 'persistent-pods'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); (root / 'yml/longhorn').mkdir(parents=True)
                (root / 'config.yml').write_text(lab.yaml.safe_dump({'longhorn_storage_classes': [{'name': 'longhorn'}, {'name': 'longhorn-encrypted'}]}))
                state = {'cluster_uid': 'cluster', 'namespace_uid': 'namespace', 'namespace_defaults': ['default'],
                         'tls_uid': 'tls', 'encryption_uid': 'key', 'disks': {'owned': {}}, 'host_state': {'owned': {}}}
                receipt = root / 'yml/longhorn/installation.json'
                receipt.write_text(json.dumps({'cluster_uid': 'cluster', 'namespace_uid': 'namespace'}))
                namespace = {'metadata': {'uid': 'namespace'}}; removed = []; calls = []
                def get(kind, name='', namespace=''):
                    if kind == 'leases.coordination.k8s.io':
                        after = 'native' in calls
                        if fault == 'lease-appeared' and not after: return {'items': []}
                        return {'items': [{'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
                            'metadata': {'name': 'longhorn-manager-webhook-lock', 'namespace': 'longhorn-system',
                                         'uid': 'replacement' if fault == 'lease-replacement' and after else 'native-lease'},
                            'spec': {'holderIdentity': '', 'leaseDurationSeconds': 1, 'leaseTransitions': 1,
                                     'acquireTime': '2026-10-20T00:00:00Z', 'renewTime': '2026-10-20T00:00:00Z'}}]}
                    if kind == 'nodes':
                        return {'items': [{'metadata': {'name': 'worker', 'uid': 'node', 'labels': {'kubeauto.io/component': 'longhorn'}}}]} if fault == 'node-residue' else {'items': []}
                    if kind == 'namespace': return None if removed else {'metadata': {'uid': 'namespace'}}
                    if kind == 'events': return {'items': [{'metadata': {'uid': 'diagnostic-event'}}]}
                    if kind == 'pods':
                        calls.append('pods')
                        return {'items': [{'metadata': {'uid': 'terminating-ui'}}] if
                            fault == 'persistent-pods' or fault == 'delayed-pods' and calls.count('pods') == 1 else []}
                    if kind == 'secret': return {'metadata': {'uid': 'foreign' if fault == 'credential' else ('tls' if name == 'longhorn-grpc-tls' else 'key')}}
                    raise AssertionError((kind, name))
                def native(*args, **kwargs):
                    calls.append('native'); self.assertTrue(kwargs['uninstall_id'].startswith('recovery-'))
                    if fault == 'native-failure': raise ValueError('native uninstall failed')
                def graph(journal):
                    calls.append('graph'); self.assertIn('diagnostic-event', journal['defaults'])
                    self.assertEqual(journal['objects']['Lease/longhorn-manager-webhook-lock'], 'native-lease')
                    if fault == 'foreign-namespace': raise ValueError('foreign object')
                def execute(argv, **kwargs):
                    calls.append('guard' if 'guard' in argv else 'helm')
                    return subprocess.CompletedProcess([], 0, '[]', '')
                def wait(label, timeout, predicate):
                    self.assertEqual(timeout, 180)
                    if not predicate() and not predicate(): raise ValueError('bounded wait expired: '+label)
                with patch.dict(sys.modules, {lab.__name__: lab}), \
                        patch.object(lab, 'BASE', ROOT), patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'STATE', root / 'owner.json'), \
                        patch.object(lab, 'core_identity'), patch.object(lab, 'cleanup_business'), patch.object(lab, 'owner', return_value=state), \
                        patch.object(lab, 'get', side_effect=get), patch.object(lab, 'run_requests', side_effect=native), \
                        patch.object(lab, 'execute', side_effect=execute), \
                        patch.object(lab, 'shared_snapshot_identity', side_effect=[{'original': 1}, {'foreign': 1} if fault == 'shared-controller' else {'original': 1}]), \
                        patch.object(lab, 'check_business_graph', side_effect=graph), \
                        patch.object(lab, 'cleanup_disks', side_effect=lambda: calls.append('disks')) as disks, \
                        patch.object(lab, 'restore_hosts', side_effect=lambda: calls.append('hosts')) as hosts, \
                        patch.object(lab, 'remove', side_effect=lambda obj: removed.append(obj)), \
                        patch.object(lab, 'wait', side_effect=wait):
                    if fault in ('none', 'delayed-pods'):
                        lab.recover_installation(123)
                        self.assertEqual(calls, ['native','guard','helm'] + ['pods'] * (2 if fault == 'delayed-pods' else 1) + ['graph','disks','hosts'])
                        self.assertFalse(receipt.exists()); self.assertTrue(receipt.with_name('installation-uninstalled-namespace.json').exists())
                        self.assertTrue(state['native_uninstalled']); self.assertFalse(state['healthy'])
                        config = lab.yaml.safe_load((root / 'config.yml').read_text())
                        self.assertEqual(config['longhorn_install'], 'no'); self.assertEqual(config['longhorn_storage_classes'], [{'name': 'longhorn'}])
                    else:
                        with self.assertRaises(ValueError): lab.recover_installation(123)
                        disks.assert_not_called(); hosts.assert_not_called()
                        self.assertFalse(removed); self.assertTrue(receipt.exists())

    def test_snapshot_restore_requires_before_after_boundary_and_independent_volume(self):
        for fault in ('none', 'post-snapshot-data', 'reused-source', 'snapshot-error', 'missing-static-size', 'missing-dynamic-size', 'static-size', 'pvc-size', 'pv-size', 'native-size'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); namespace = 'owned'; payload = 'confirmed'
                post_payload = payload + '-after-snapshot'
                digest = hashlib.sha256(payload.encode()*262144).hexdigest()
                post_digest = hashlib.sha256(post_payload.encode()*262144).hexdigest()
                journal = {'namespace': namespace, 'volume': 'source-volume', 'pv': 'source-pv', 'objects': {}}
                objects = {('volumes.longhorn.io', 'source-volume'): {'spec': {'size': str(2*1024**3)}}}; calls = []
                def run_requests(relay, requests, **kwargs):
                    request = requests[0]; calls.append((request, kwargs))
                    objects.setdefault(('snapshots.longhorn.io', request['name']), {'metadata': {'name': request['name'], 'uid': 'native-uid'},
                                       'spec': {'volume': 'source-volume'}, 'status': {'readyToUse': True}})
                def create(obj):
                    obj = copy.deepcopy(obj); kind = obj['kind']; name = obj['metadata']['name']; obj['metadata']['uid'] = name+'-uid'
                    journal['objects'][kind+'/'+name] = name+'-uid'
                    if kind == 'VolumeSnapshot':
                        obj['status'] = {'readyToUse': True, 'restoreSize': '2Gi', 'boundVolumeSnapshotContentName': name+'-content'}
                        if (name == 'native' and fault == 'missing-static-size') or (name == 'csi' and fault == 'missing-dynamic-size'):
                            del obj['status']['restoreSize']
                        if name == 'native' and fault == 'static-size': obj['status']['restoreSize'] = '4Gi'
                        if fault == 'snapshot-error': obj['status']['error'] = {'message': 'fixture error'}
                        objects['volumesnapshots.snapshot.storage.k8s.io', name] = obj
                    elif kind == 'PersistentVolumeClaim':
                        obj['status'] = {'phase': 'Bound', 'capacity': {'storage': '4Gi' if fault == 'pvc-size' else '2Gi'}}; obj['spec']['volumeName'] = name+'-pv'
                        objects['pvc', name] = obj
                        objects['pv', name+'-pv'] = {'metadata': {'uid': name+'-pv-uid'}, 'spec': {
                            'capacity': {'storage': '4Gi' if fault == 'pv-size' else '2Gi'}, 'csi': {'volumeHandle': name+'-volume'}}}
                        objects['volumes.longhorn.io', name+'-volume'] = {'metadata': {'uid': name+'-native'},
                            'spec': {'size': str(4*1024**3 if fault == 'native-size' else 2*1024**3)}}
                    elif kind == 'Pod':
                        obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}; objects['pod', name] = obj
                    return obj
                def bind(journal):
                    for name, row in journal['snapshots'].items(): row.update(content=name+'-content', native='native')
                def bind_volume(journal, record, claim):
                    journal.setdefault('claims', {})[claim] = {'pv_uid': claim+'-pv-uid', 'volume_uid': claim+'-native', 'pv': 'source-pv' if fault == 'reused-source' else claim+'-pv', 'volume': claim+'-volume'}
                def command(*args, **kwargs):
                    if args[0] == 'create':
                        obj = json.loads(kwargs['stdin']); return subprocess.CompletedProcess([], 0, json.dumps({'metadata': {'name': obj['metadata']['name'], 'uid': 'content-uid'}}), '')
                    pod = args[args.index('exec')+1]; program = args[-1]
                    if pod == 'writer': result = post_digest
                    elif program == lab.payload_program(payload, read_only=True): result = post_digest if fault == 'post-snapshot-data' else digest
                    else: result = hashlib.sha256((payload+'-restore-'+pod.removeprefix('restore-')).encode()*262144).hexdigest()
                    return subprocess.CompletedProcess([], 0, result, '')
                def wait(label, timeout, predicate):
                    value = predicate()
                    if not value: raise TimeoutError(label)
                    return value
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'run_requests', side_effect=run_requests), \
                        patch.object(lab, 'get', side_effect=lambda kind, name='', *_: objects.get((kind, name))), \
                        patch.object(lab, 'bind_snapshots', side_effect=bind), patch.object(lab, 'idempotence_snapshot', return_value={'stable': True}), \
                        patch.object(lab, 'bind_business_volume', side_effect=bind_volume), patch.object(lab, 'healthy_replicas', return_value=['a','b','c']), \
                        patch.object(lab, 'wait', side_effect=wait), patch.object(lab, 'kubectl', side_effect=command):
                    if fault in ('none', 'missing-static-size'):
                        self.assertEqual(lab.verify_snapshot(journal, create, 123, payload, digest), (post_payload, post_digest))
                        self.assertEqual(len(journal['claims']), 2)
                    else:
                        with self.assertRaises(ValueError): lab.verify_snapshot(journal, create, 123, payload, digest)
                    self.assertEqual(calls[-1][1]['expected_refusal'], 'immutable Longhorn request differs; use a new name')

    def test_product_request_input_restores_on_failure_and_rejects_foreign_changes(self):
        for outcome in ('success', 'product-failure', 'external-edit', 'foreign-receipt'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path = root / 'config.yml'; original = 'longhorn_requests: []\nlonghorn_install: yes\n'
                path.write_text(original)
                def product(*args, **kwargs):
                    self.assertEqual(lab.yaml.safe_load(path.read_text())['longhorn_requests'], [{'kind': 'snapshot'}])
                    if outcome == 'external-edit': path.write_text('foreign input')
                    if outcome == 'product-failure': raise ValueError('product failed')
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner', return_value={'run_id': 'owned'}), \
                        patch.object(lab, 'supervise_core', side_effect=product) as command:
                    if outcome == 'foreign-receipt':
                        lab.write_private(root / 'longhorn-request-input.json', {'run_id': 'foreign'})
                        with self.assertRaises(ValueError): lab.run_requests(123, [])
                        command.assert_not_called()
                    elif outcome in ('product-failure', 'external-edit'):
                        with self.assertRaises(ValueError): lab.run_requests(123, [{'kind': 'snapshot'}])
                    else: lab.run_requests(123, [{'kind': 'snapshot'}])
                    if outcome == 'external-edit':
                        self.assertEqual(path.read_text(), 'foreign input')
                        self.assertTrue((root / 'longhorn-request-input.json').exists())
                    else: self.assertEqual(path.read_text(), original)
                    if outcome in ('success', 'product-failure'):
                        self.assertFalse((root / 'longhorn-request-input.json').exists())
                        lab.restore_request_input()

    def test_expected_product_refusal_requires_failed_cli_and_exact_owning_diagnostic(self):
        import contextlib
        for rc, reason in ((1, 'immutable Longhorn request differs; use a new name'), (0, 'immutable Longhorn request differs; use a new name'),
                           (1, 'API unavailable')):
            with self.subTest(rc=rc, reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'kubecli.py').write_text('import sys\nprint(' + repr('LONGHORN_PRODUCT_FAIL reason=' + reason) + ')\nsys.exit(' + str(rc) + ')\n')
                identity = ([b'python', str(Path(lab.__file__).resolve()).encode(), b'relay-maintain'], '123')
                output = io.StringIO()
                with patch.object(lab, 'BASE', root), patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner'), \
                        patch.object(lab, 'process_identity', return_value=identity), contextlib.redirect_stdout(output):
                    if rc == 1 and reason.startswith('immutable'):
                        lab.supervise_core(123, 'longhorn-setup', expected_refusal='immutable Longhorn request differs; use a new name')
                        self.assertIn('LONGHORN_EXPECTED_PRODUCT_REFUSAL_VERIFIED', output.getvalue())
                    else:
                        with self.assertRaisesRegex(ValueError, 'owning expected refusal'):
                            lab.supervise_core(123, 'longhorn-setup', expected_refusal='immutable Longhorn request differs; use a new name')
                        self.assertTrue(list(root.glob('unexpected-refusal-*.json')))
                self.assertNotIn('LONGHORN_PRODUCT_FAIL', output.getvalue(), 'expected negative output must remain in its private capture')

    def test_snapshot_cleanup_binds_cluster_content_and_native_uids_before_deleting_any_probe(self):
        for fault in ('none', 'content-uid', 'content-ref', 'foreign-handle', 'native-uid'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); record = root / 'longhorn-install-business.json'; namespace = 'longhorn-business-unit'
                journal = {'namespace': namespace, 'namespace_uid': 'namespace-uid', 'volume': 'volume',
                           'objects': {'VolumeSnapshot/csi': 'snapshot-uid', 'Pod/writer': 'pod-uid'},
                           'snapshots': {'csi': {'content': 'content', 'content_uid': 'content-uid', 'native': 'native'}},
                           'native_snapshots': {'native': {'uid': 'native-uid'}}}
                objects = {
                    ('namespace', namespace): {'metadata': {'name': namespace, 'uid': 'namespace-uid'}},
                    ('pod', 'writer'): {'metadata': {'name': 'writer', 'uid': 'pod-uid'}},
                    ('volumesnapshots.snapshot.storage.k8s.io', 'csi'): {'metadata': {'name': 'csi', 'uid': 'snapshot-uid'}},
                    ('volumesnapshotcontents.snapshot.storage.k8s.io', 'content'): {'metadata': {'name': 'content', 'uid': 'content-uid'}, 'spec': {
                        'driver': 'driver.longhorn.io', 'deletionPolicy': 'Retain', 'volumeSnapshotRef': {'name': 'csi', 'namespace': namespace, 'uid': 'snapshot-uid'},
                        'source': {'snapshotHandle': 'snap://volume/native'}}},
                    ('snapshots.longhorn.io', 'native'): {'metadata': {'name': 'native', 'uid': 'native-uid'}, 'spec': {'volume': 'volume'}}}
                content = objects['volumesnapshotcontents.snapshot.storage.k8s.io', 'content']
                if fault == 'content-uid': content['metadata']['uid'] = 'foreign'
                if fault == 'content-ref': content['spec']['volumeSnapshotRef']['uid'] = 'foreign'
                if fault == 'foreign-handle': content['spec']['source']['snapshotHandle'] = 'snap://foreign/native'
                if fault == 'native-uid': objects['snapshots.longhorn.io', 'native']['metadata']['uid'] = 'foreign'
                record.write_text(json.dumps(journal)); removed = []
                def get(kind, name='', namespace=''):
                    return objects.get((kind, name)) if name else {'items': [obj for (k, _), obj in objects.items() if k == kind]}
                def remove(obj):
                    key = next(key for key, candidate in objects.items() if candidate is obj)
                    removed.append(key); del objects[key]
                def wait(label, timeout, check): self.assertTrue(check(), label)
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'owner', return_value={}), \
                        patch.object(lab, 'get', side_effect=get), patch.object(lab, 'remove', side_effect=remove), \
                        patch.object(lab, 'wait', side_effect=wait), patch.object(lab, 'check_business_graph'):
                    if fault == 'none':
                        lab.cleanup_business(record)
                        self.assertFalse(objects); self.assertFalse(record.exists())
                        self.assertLess(removed.index(('volumesnapshots.snapshot.storage.k8s.io', 'csi')),
                                        removed.index(('volumesnapshotcontents.snapshot.storage.k8s.io', 'content')))
                    else:
                        with self.assertRaises(ValueError): lab.cleanup_business(record)
                        self.assertFalse(removed, 'foreign snapshot identity must preserve every probe for diagnosis')

    def test_expansion_probe_requires_real_capacity_and_fsyncs_new_region(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); device = root / 'device'
            program = lab.expansion_probe_program(block=True).replace(repr('/data-device'), repr(str(device)))
            with device.open('wb') as stream: stream.truncate(4*1024**3)
            observed = json.loads(subprocess.check_output([sys.executable, '-c', program], text=True))
            self.assertEqual(observed['capacity_bytes'], 4*1024**3)
            with device.open('rb') as stream:
                stream.seek(observed['offset'])
                self.assertEqual(hashlib.sha256(stream.read(1024**2)).hexdigest(), observed['sha256'])
            with device.open('wb') as stream: stream.truncate(2*1024**3)
            rejected = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(device.stat().st_size, 2*1024**3)
            filesystem = lab.expansion_probe_program().replace(repr('/data/expanded'), repr(str(root / 'expanded'))).replace(repr('/data'), repr(str(root)))
            measured = json.loads(subprocess.check_output([sys.executable, '-c', filesystem], text=True))
            self.assertGreater(measured['capacity_bytes'], 3*1024**3)
            self.assertEqual((root / 'expanded').stat().st_size, 2*1024**3+2*1024**2)

    def test_resize_uses_original_uid_and_resource_version_and_quantity_units(self):
        claim = {'metadata': {'name': 'data', 'namespace': 'owned', 'uid': 'original', 'resourceVersion': '42'}}
        with patch.object(lab, 'kubectl') as command:
            lab.resize_claim(claim, '4Gi')
        argv = command.call_args.args
        operations = json.loads(argv[argv.index('-p')+1])
        self.assertEqual(operations[:2], [{'op': 'test', 'path': '/metadata/uid', 'value': 'original'},
                                          {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'}])
        for value in ('4Gi', '4096Mi', '4294967296'):
            self.assertEqual(lab.capacity_bytes(value), 4*1024**3)
        with self.assertRaises(ValueError): lab.capacity_bytes('4GB')

    def test_expansion_requires_same_objects_capacity_credential_and_owning_shrink_refusal(self):
        import copy
        namespace = 'owned'; volume = 'volume'; pv = 'pv'
        journal = {'namespace': namespace, 'pv': pv, 'pv_uid': 'pv-uid', 'volume': volume, 'volume_uid': 'volume-uid',
                   'storage_class': 'longhorn-encrypted', 'objects': {'PersistentVolumeClaim/data': 'pvc-uid', 'Pod/writer': 'pod-uid'}}
        for outcome in ('success', 'replacement', 'foreign-baseline', 'capacity-pending', 'missing-key-ref', 'shrink-network', 'shrink-accepted', 'data-corrupt'):
            with self.subTest(outcome=outcome):
                objects = {
                    'pvc': {'metadata': {'uid': 'pvc-uid', 'name': 'data', 'namespace': namespace, 'resourceVersion': '1'},
                            'spec': {'volumeName': pv, 'resources': {'requests': {'storage': '2Gi'}}},
                            'status': {'capacity': {'storage': '2Gi'}}},
                    'pv': {'metadata': {'uid': 'pv-uid', 'name': pv}, 'spec': {'capacity': {'storage': '2Gi'}, 'csi': {
                        'volumeHandle': volume, 'nodeExpandSecretRef': {'name': 'longhorn-volume-key', 'namespace': 'longhorn-system'}}}},
                    'volumes.longhorn.io': {'metadata': {'uid': 'volume-uid', 'name': volume, 'namespace': 'longhorn-system'},
                                           'spec': {'size': str(2*1024**3), 'encrypted': True}},
                    'pod': {'metadata': {'uid': 'pod-uid', 'name': 'writer', 'namespace': namespace}}}
                if outcome == 'missing-key-ref': del objects['pv']['spec']['csi']['nodeExpandSecretRef']
                if outcome == 'foreign-baseline': objects['pv']['metadata']['uid'] = 'foreign'
                def resize(claim, size, **kwargs):
                    if size == '4Gi':
                        objects['pvc']['spec']['resources']['requests']['storage'] = size
                        if outcome != 'capacity-pending':
                            objects['pvc']['status']['capacity']['storage'] = size
                            objects['pv']['spec']['capacity']['storage'] = size
                            objects['volumes.longhorn.io']['spec']['size'] = str(4*1024**3)
                        if outcome == 'replacement': objects['pod']['metadata']['uid'] = 'foreign'
                        return subprocess.CompletedProcess([], 0, '', '')
                    return subprocess.CompletedProcess([], 0 if outcome == 'shrink-accepted' else 1, '',
                        'connection reset' if outcome == 'shrink-network' else
                        'Forbidden: field can not be less than status.capacity')
                def command(*args, **kwargs):
                    result = json.dumps({'capacity_bytes': 4*1024**3-1024**2, 'sha256': 'a'*64}) if 'import hashlib,json,os;' in args[-1] else ('wrong' if outcome == 'data-corrupt' else 'confirmed')
                    return subprocess.CompletedProcess([], 0, result, '')
                def wait(label, timeout, predicate):
                    result = predicate()
                    if not result: raise TimeoutError(label)
                    return result
                with patch.object(lab, 'get', side_effect=lambda kind, *_: copy.deepcopy(objects[kind])), \
                        patch.object(lab, 'resize_claim', side_effect=resize) as resize_mock, \
                        patch.object(lab, 'kubectl', side_effect=command), patch.object(lab, 'wait', side_effect=wait), \
                        patch.object(lab, 'healthy_replicas', return_value=['a', 'b', 'c']):
                    if outcome == 'success': lab.verify_expansion(journal, 'payload', 'confirmed')
                    else:
                        with self.assertRaises((ValueError, TimeoutError)): lab.verify_expansion(journal, 'payload', 'confirmed')
                    if outcome in ('missing-key-ref', 'foreign-baseline'): resize_mock.assert_not_called()

    def test_expansion_suite_stops_at_first_failed_variant(self):
        with patch.object(lab, 'business', side_effect=[None, ValueError('block failed')]) as business:
            with self.assertRaisesRegex(ValueError, 'block failed'): lab.expansion_suite()
        self.assertEqual(business.call_count, 2)

    def test_multiple_retained_claim_cleanup_preserves_identity_and_retires_clone_before_source(self):
        for replaced, policy in ((False, 'Retain'), (True, 'Retain'), (False, 'Delete'), (False, 'Delete-stalled')):
            with self.subTest(replaced=replaced, policy=policy), tempfile.TemporaryDirectory() as directory:
                record = Path(directory) / 'journal.json'
                journal = {'namespace': 'longhorn-business-unit', 'namespace_uid': 'namespace-uid', 'objects': {}, 'claims': {}}
                journal['reclaim_policy'] = 'Delete' if policy.startswith('Delete') else policy
                objects = {('namespace', journal['namespace']): {'metadata': {'name': journal['namespace'], 'uid': 'namespace-uid'}}}
                for name in ('data', 'clone'):
                    journal['objects']['PersistentVolumeClaim/' + name] = name + '-claim'
                    journal['objects']['Pod/' + name] = name + '-pod'
                    objects['pod', name] = {'metadata': {'name': name, 'uid': name + '-pod'}}
                    objects['pvc', name] = {'metadata': {'name': name, 'uid': name + '-claim'}}
                    objects['pv', name + '-pv'] = {'metadata': {'name': name + '-pv', 'uid': name + '-pv-uid'}, 'spec': {
                        'claimRef': {'namespace': journal['namespace'], 'name': name, 'uid': name + '-claim'},
                        'storageClassName': 'longhorn', 'persistentVolumeReclaimPolicy': journal['reclaim_policy'],
                        'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': name + '-volume'}}}
                    objects['volumes.longhorn.io', name + '-volume'] = {'metadata': {'name': name + '-volume', 'uid': name + '-volume-uid'}, 'status': {'state': 'detached'}}
                    receipt = {'pv': name + '-pv', 'pv_uid': name + '-pv-uid', 'volume': name + '-volume', 'volume_uid': name + '-volume-uid'}
                    if name == 'data': journal.update(receipt)
                    else: journal['claims'][name] = receipt
                record.write_text(json.dumps(journal))
                if replaced: objects['pv', 'data-pv']['metadata']['uid'] = 'foreign-replacement'
                removed = []
                def get(kind, object_name='', namespace=''):
                    if object_name: return objects.get((kind, object_name))
                    return {'items': [value for (k, _), value in objects.items() if k == kind]}
                def remove(obj):
                    key = next(key for key, value in objects.items() if value is obj)
                    removed.append(key); del objects[key]
                    if key[0] == 'pvc' and policy == 'Delete':
                        objects.pop(('pv', key[1] + '-pv'))
                        objects.pop(('volumes.longhorn.io', key[1] + '-volume'))
                def wait(label, timeout, check):
                    self.assertTrue(check(), label)
                with patch.object(lab, 'owner', return_value={}), patch.object(lab, 'get', side_effect=get), \
                        patch.object(lab, 'remove', side_effect=remove), patch.object(lab, 'wait', side_effect=wait), \
                        patch.object(lab, 'check_business_graph'), patch.object(lab, 'write_private'):
                    if replaced:
                        with self.assertRaisesRegex(ValueError, 'identity changed'): lab.cleanup_business(record)
                        self.assertFalse(removed, 'a replacement source PV must block deletion of every probe')
                    elif policy == 'Delete-stalled':
                        with self.assertRaisesRegex(AssertionError, 'delete-policy-pv-reclaimed'): lab.cleanup_business(record)
                        self.assertFalse(any(key[0] in ('pv', 'volumes.longhorn.io') for key in removed),
                                         'a stalled Delete controller must not be hidden by explicit cleanup')
                        self.assertIn(('pvc', 'data'), objects, 'a failed clone prerequisite stops source cleanup')
                    else:
                        lab.cleanup_business(record)
                        if policy == 'Retain':
                            self.assertLess(removed.index(('volumes.longhorn.io', 'clone-volume')),
                                            removed.index(('volumes.longhorn.io', 'data-volume')))
                        else:
                            self.assertFalse(any(key[0] in ('pv', 'volumes.longhorn.io') for key in removed))
                        self.assertFalse(objects)
                        self.assertFalse(record.exists())

    def test_initial_credentials_and_classes_use_actual_product_schema_and_never_persist_keys_in_config(self):
        from tests.unit.test_longhorn_delivery import configuration, product
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); c = configuration(); (root / 'config.yml').write_text(yaml.safe_dump(c))
            state = {}; requests = []
            def kubectl(*args, **kwargs):
                obj = json.loads(kwargs['stdin']); requests.append(obj)
                if len(requests) == 2: self.assertIn('namespace_uid', state)
                if len(requests) == 3: self.assertIn('tls_uid', state)
                return subprocess.CompletedProcess([], 0, json.dumps({'metadata': {'uid': obj['metadata']['name'] + '-uid'}}), '')
            with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'STATE', root / 'owner.json'), \
                    patch.object(lab, 'core_identity'), patch.object(lab, 'owner', return_value=state), \
                    patch.object(lab, 'get', return_value=None), patch.object(lab, 'kubectl', side_effect=kubectl), \
                    patch.object(lab, 'namespace_defaults', return_value=['default-serviceaccount', 'default-ca']), \
                    patch.object(lab, 'tls_fixture', return_value={'tls.key': 'private-fixture-material'}):
                lab.prepare_credentials()
            prepared = yaml.safe_load((root / 'config.yml').read_text()); product.validate(prepared)
            self.assertEqual({sc['name'] for sc in prepared['longhorn_storage_classes']},
                             {'longhorn', 'longhorn-encrypted', 'longhorn-delete'})
            self.assertNotIn('private-fixture-material', (root / 'config.yml').read_text())
            key = requests[-1]['stringData']['CRYPTO_KEY_VALUE']
            self.assertGreaterEqual(len(key), 48)
            self.assertNotIn(key, (root / 'config.yml').read_text())
            self.assertNotIn(key, (root / 'owner.json').read_text())
            encrypted = next(sc for sc in prepared['longhorn_storage_classes'] if sc['name'] == 'longhorn-encrypted')
            self.assertEqual(product.storage_class(encrypted)['parameters']['csi.storage.k8s.io/node-expand-secret-name'], 'longhorn-volume-key')

    def test_clone_claim_uses_same_namespace_and_explicit_pvc_source_and_size(self):
        claim = lab.clone_claim('owned-namespace', 'clone', '2Gi')
        self.assertEqual(claim['metadata']['namespace'], 'owned-namespace')
        self.assertEqual(claim['spec']['dataSource'], {'kind': 'PersistentVolumeClaim', 'name': 'data'})
        self.assertEqual(claim['spec']['resources']['requests']['storage'], '2Gi')
        self.assertEqual(claim['spec']['storageClassName'], 'longhorn')

    def test_clone_size_refusal_requires_original_claim_event_and_stops_at_false_success(self):
        journal = {'namespace': 'owned-namespace'}
        for outcome in ('bound', 'foreign-event', 'refused'):
            claim = {'metadata': {'uid': 'wrong-size-uid'}, 'status': {'phase': 'Bound' if outcome == 'bound' else 'Pending'}}
            event = {'involvedObject': {'uid': 'foreign' if outcome == 'foreign-event' else 'wrong-size-uid'},
                     'reason': 'ProvisioningFailed', 'message': 'the requested size (3Gi bytes) is different than the source volume size (2Gi bytes)'}
            removed = []; creates = []
            def create(obj):
                creates.append(obj)
                if obj['metadata']['name'] == 'clone': raise RuntimeError('reached-valid-clone')
                return claim
            def get(kind, *args, **kwargs):
                return {'items': [event]} if kind == 'events' else None if removed else claim
            def wait(label, timeout, check):
                if not check(): raise RuntimeError('refusal-not-proven')
            with self.subTest(outcome=outcome), patch.object(lab, 'get', side_effect=get), \
                    patch.object(lab, 'remove', side_effect=lambda obj: removed.append(obj)), patch.object(lab, 'wait', side_effect=wait):
                expected = 'incorrectly accepted' if outcome == 'bound' else 'refusal-not-proven' if outcome == 'foreign-event' else 'reached-valid-clone'
                with self.assertRaisesRegex((ValueError, RuntimeError), expected):
                    lab.verify_clone(journal, create, 'payload', 'digest')
            self.assertEqual(len(creates), 2 if outcome == 'refused' else 1)
            self.assertEqual(bool(removed), outcome == 'refused')

    def test_rwx_probe_confirms_real_fsync_writes_and_rejects_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            program = lab.rwx_probe_program('writer', duration=2).replace('/data', directory)
            result = subprocess.run([__import__('sys').executable, '-c', program],
                                    capture_output=True, text=True, check=True, timeout=10)
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            self.assertGreaterEqual(rows[-1]['confirmed'], 2)
            self.assertEqual(rows[-1]['errors'], 0)
            self.assertGreaterEqual(rows[-1]['max_latency_s'], 0)
            self.assertEqual((Path(directory) / 'probe-writer').read_bytes(),
                             ('writer:' + str(rows[-1]['confirmed'] - 1)).encode() * 4096)
        with self.assertRaises(ValueError): lab.rwx_probe_program('../../foreign')
        with self.assertRaises(ValueError): lab.rwx_probe_program('reader', duration=0)

    def test_rwx_lock_clients_require_real_exclusion_release_and_eof_cleanup(self):
        import select
        import sys
        with tempfile.TemporaryDirectory() as directory:
            def command(action):
                return [sys.executable, '-u', '-c', lab.rwx_lock_program(action).replace('/data', directory)]
            def invoke(action):
                return subprocess.run(command(action), capture_output=True, text=True, timeout=5)
            for release in ('release\n', ''):
                with self.subTest(release=release):
                    holder = subprocess.Popen(command('hold'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True)
                    try:
                        self.assertTrue(select.select([holder.stdout], [], [], 5)[0])
                        self.assertEqual(holder.stdout.readline().strip(), 'RWX_LOCK_HELD')
                        denied = invoke('refused')
                        self.assertEqual((denied.returncode, denied.stdout.strip()), (0, 'RWX_LOCK_REFUSED'))
                        self.assertNotEqual(invoke('acquire').returncode, 0)
                        stdout, _ = holder.communicate(release, timeout=5)
                        self.assertEqual(holder.returncode == 0, bool(release))
                        self.assertEqual('RWX_LOCK_RELEASED' in stdout, bool(release))
                        acquired = invoke('acquire')
                        self.assertEqual((acquired.returncode, acquired.stdout.strip()), (0, 'RWX_LOCK_ACQUIRED'))
                        self.assertNotEqual(invoke('refused').returncode, 0, 'unheld lock cannot prove exclusion')
                    finally:
                        if holder.poll() is None: holder.kill(); holder.wait(timeout=5)
                        for stream in (holder.stdin, holder.stdout, holder.stderr): stream.close()
        with self.assertRaises(ValueError): lab.rwx_lock_program('foreign')

    def test_rwx_lock_orchestration_rejects_missing_markers_and_reaps_holder(self):
        import sys
        native_popen = subprocess.Popen
        for fault in ('none', 'refused-marker', 'acquire-marker', 'client-error'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                processes = []
                def popen(argv, **kwargs):
                    self.assertIn('-i', argv)
                    self.assertIn('writer', argv)
                    process = native_popen([sys.executable, '-u', '-c', argv[-1].replace('/data', directory)], **kwargs)
                    processes.append(process)
                    return process
                def cli(*args, **kwargs):
                    self.assertIn('reader', args)
                    action = 'refused' if "action='refused'" in args[-1] else 'acquire'
                    if fault == 'client-error': raise RuntimeError('client failed')
                    result = subprocess.run([sys.executable, '-c', args[-1].replace('/data', directory)],
                                            capture_output=True, text=True, timeout=5, check=True)
                    if fault == action+'-marker': result.stdout = 'unexpected marker'
                    return result
                def ready(label, timeout, check):
                    deadline = time.monotonic()+5
                    while not check():
                        if time.monotonic()>deadline: raise TimeoutError(label)
                        time.sleep(0.01)
                with patch.object(lab.subprocess, 'Popen', side_effect=popen), \
                        patch.object(lab, 'kubectl', side_effect=cli), patch.object(lab, 'wait', side_effect=ready), \
                        redirect_stdout(io.StringIO()) as output:
                    # subprocess.run uses the same module; restore native Popen
                    # only inside the short independent competitor invocation.
                    original_cli = cli
                    def competitor(*args, **kwargs):
                        with patch.object(lab.subprocess, 'Popen', native_popen): return original_cli(*args, **kwargs)
                    with patch.object(lab, 'kubectl', side_effect=competitor):
                        if fault == 'none': lab.verify_rwx_locks({'namespace': 'owned'})
                        else:
                            with self.assertRaises((ValueError, RuntimeError)):
                                lab.verify_rwx_locks({'namespace': 'owned'})
                self.assertEqual('LONGHORN_RWX_LOCKS_VERIFIED' in output.getvalue(), fault == 'none')
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(processes[0].stdin.closed)

    def test_share_manager_fault_is_bound_to_original_volume_manager_and_pod_uids(self):
        journal = {'volume': 'owned-volume', 'volume_uid': 'volume-uid'}
        volume = {'metadata': {'uid': 'volume-uid'}, 'spec': {'accessMode': 'rwx'}}
        manager = {'metadata': {'uid': 'manager-uid', 'ownerReferences': [{'kind': 'Volume', 'uid': 'volume-uid'}]}, 'status': {'state': 'running'}}
        pod = {'metadata': {'uid': 'pod-uid', 'ownerReferences': [{'kind': 'ShareManager', 'uid': 'manager-uid', 'controller': True}]},
               'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        def get(kind, *args):
            return {'volumes.longhorn.io': volume, 'sharemanagers.longhorn.io': manager, 'pod': pod}[kind]
        with patch.object(lab, 'get', side_effect=get):
            self.assertEqual(lab.share_manager_identity(journal), pod)
            self.assertIsNone(lab.share_manager_identity(journal, previous_uid='pod-uid'))
            pod['metadata']['uid'] = 'replacement-uid'
            self.assertEqual(lab.share_manager_identity(journal, previous_uid='pod-uid'), pod)
            for metadata in (pod['metadata'], manager['metadata']):
                references = metadata['ownerReferences']; metadata['ownerReferences'] = []
                with self.assertRaises(ValueError): lab.share_manager_identity(journal)
                metadata['ownerReferences'] = references
            volume['metadata']['uid'] = 'foreign'
            with self.assertRaisesRegex(ValueError, 'native volume identity'): lab.share_manager_identity(journal)

    def test_clean_core_rejects_dropins_even_without_a_base_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = lab.unit_override_script()
            for prefix in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
                script = script.replace(prefix, str(root / prefix.lstrip('/')))
            script = script.replace('/var/tmp/kubeauto-longhorn-history', str(root / 'history'))
            def invoke(action):
                with patch('sys.stdin', io.StringIO(json.dumps({'action': action}))), \
                        patch('sys.stdout', new_callable=io.StringIO):
                    exec(script, {})
            invoke('check')
            for folder in ('kube-apiserver.service.d', 'kube-.service.d', 'service.d'):
                parent = root / 'etc/systemd/system' / folder; parent.mkdir(parents=True)
                path = parent / '90-obsolete.conf'; path.write_text('[Service]\nExecStart=\nExecStart=/old-api --service-cluster-ip-range=10.79.0.0/16\n')
                with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, 'foreign systemd overrides'):
                    invoke('check')
                with self.assertRaisesRegex(ValueError, 'unrecognized systemd override'):
                    invoke('recover')
                self.assertTrue(path.exists(), 'unknown override must be preserved')
                path.unlink(); parent.rmdir()

    def test_override_recovery_requires_exact_digest_and_inactive_services_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); content = b'[Service]\nExecStart=\nExecStart=/old-api\n'
            digest = __import__('hashlib').sha256(content).hexdigest()
            script = lab.unit_override_script().replace('99dcd3f9f6440548d5c2e4867e7dc4357df1b180dd0a55f9f8c45c3bc1db9e45', digest)
            for prefix in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
                script = script.replace(prefix, str(root / prefix.lstrip('/')))
            script = script.replace('/var/tmp/kubeauto-longhorn-history', str(root / 'history'))
            path = root / 'etc/systemd/system/kube-apiserver.service.d/90-drafts-skywalking-proxy-cn.conf'
            path.parent.mkdir(parents=True); path.write_bytes(content)
            def invoke(active=False):
                def run(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, 'active\n' if active else 'inactive\n', '')
                with patch('sys.stdin', io.StringIO('{"action":"recover"}')), \
                        patch('sys.stdout', new_callable=io.StringIO), patch('subprocess.run', side_effect=run):
                    exec(script, {})
            with self.assertRaisesRegex(ValueError, 'host services must be clean'):
                invoke(active=True)
            self.assertTrue(path.is_file())
            invoke()
            self.assertFalse(path.exists())
            self.assertEqual((root / ('history/host-unit-' + digest) / 'override.conf').read_bytes(), content)
            invoke()  # Idempotent recovery verifies the original archive.

    def test_confirmed_payload_program_reads_exact_filesystem_and_raw_block_bytes(self):
        payload = str(uuid.uuid4())
        expected = __import__('hashlib').sha256(payload.encode() * 262144).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); filesystem = root / 'confirmed'; device = root / 'device'
            with device.open('wb') as stream:
                stream.write(b'guard-prefix'); stream.truncate(16 * 1024**2)
            for block in (False, True):
                for read_only in (False, True):
                    program = lab.payload_program(payload, block=block, read_only=read_only)
                    program = program.replace("'/data/confirmed'", repr(str(filesystem))).replace("'/data-device'", repr(str(device))).replace("'/data'", repr(str(root)))
                    result = subprocess.run([__import__('sys').executable, '-c', program],
                                            text=True, capture_output=True, check=True, timeout=10)
                    self.assertEqual(result.stdout.strip(), expected)
            self.assertEqual(filesystem.stat().st_size, len(payload) * 262144)
            with device.open('rb') as stream: self.assertEqual(stream.read(12), b'guard-prefix')

    def test_raw_block_client_never_formats_or_mounts_the_device(self):
        pod = lab.client_pod('owned', 'client', 'data', 'approved-node', block=True)
        container = pod['spec']['containers'][0]
        self.assertNotIn('volumeMounts', container)
        self.assertEqual(container['volumeDevices'], [{'name': 'data', 'devicePath': '/data-device'}])
        self.assertEqual(pod['spec']['nodeSelector'], {'kubernetes.io/hostname': 'approved-node'})
        self.assertNotIn('mkfs', json.dumps(pod))
        self.assertNotIn('nodeName', pod['spec'], 'RWOP refusal must reach the scheduler')

    def test_exclusivity_requires_the_owning_kubernetes_refusal_and_preserves_writer(self):
        for access, reason, message in (('ReadWriteOnce', 'FailedAttachVolume', 'Multi-Attach error for volume'),
                                        ('ReadWriteOncePod', 'FailedScheduling', 'uses ReadWriteOncePod PVC')):
            journal = {'namespace': 'probe', 'objects': {}}
            created = {'metadata': {'uid': 'competitor'}}
            event = {'involvedObject': {'uid': 'competitor'}, 'reason': reason, 'message': message}
            calls = []
            def get(kind, *args, **kwargs):
                if kind == 'events': return {'items': [event]}
                calls.append(kind)
                return created if len(calls) <= 2 else None
            def kubectl(*args, **kwargs):
                if args[0] == 'create':
                    pod = json.loads(kwargs['stdin'])
                    expected = lab.WORKERS[0 if access == 'ReadWriteOncePod' else 1].rsplit('.', 1)[1]
                    self.assertTrue(pod['spec']['nodeSelector']['kubernetes.io/hostname'].endswith(expected))
                    return subprocess.CompletedProcess([], 0, json.dumps(created), '')
                self.assertIn('writer', args)
                return subprocess.CompletedProcess([], 0, 'confirmed', '')
            with self.subTest(access=access), patch.object(lab, 'get', side_effect=get), \
                    patch.object(lab, 'kubectl', side_effect=kubectl), patch.object(lab, 'write_private'), \
                    patch.object(lab, 'remove'), patch.object(lab, 'wait', side_effect=lambda label, timeout, check: self.assertTrue(check())):
                lab.exclusive_attachment(journal, access, 'payload', 'confirmed')
            self.assertEqual(journal['objects'], {'Pod/reader': 'competitor'})

    def test_rwo_suite_stops_at_first_unmet_mode(self):
        calls = []
        def business(*args, **kwargs):
            calls.append(kwargs)
            if kwargs['access_mode'] == 'ReadWriteOncePod': raise ValueError('RWOP failed')
        with patch.object(lab, 'business', side_effect=business):
            with self.assertRaisesRegex(ValueError, 'RWOP failed'): lab.rwo_suite()
        self.assertEqual(calls, [{'access_mode': 'ReadWriteOnce', 'block': False},
                                 {'access_mode': 'ReadWriteOncePod', 'block': False}])

    def test_idempotence_observes_secret_changes_without_recording_secret_data(self):
        release = [{'name': 'longhorn', 'status': 'deployed', 'revision': '1', 'chart': 'longhorn-1.12.1', 'app_version': 'v1.12.1'}]
        secret = {'metadata': {'name': 'grpc', 'uid': 'tls-uid'}, 'data': {'tls.key': 'private-material'}}
        pod = {'metadata': {'name': 'manager', 'uid': 'pod-uid', 'generation': 1}, 'spec': {'nodeName': 'approved'}, 'status': {'phase': 'Running'}}
        def get(kind, *, namespace):
            return {'items': [secret if kind == 'secret' else pod] if kind in ('pod', 'secret') else []}
        with patch.object(lab, 'execute', return_value=subprocess.CompletedProcess([], 0, json.dumps(release), '')), \
                patch.object(lab, 'get', side_effect=get):
            before = lab.idempotence_snapshot('probe')
            self.assertNotIn('private-material', json.dumps(before))
            pod['status']['phase'] = 'Pending'
            self.assertEqual(lab.idempotence_snapshot('probe'), before, 'status bookkeeping is not an identity change')
            secret['data']['tls.key'] = 'replacement-material'
            self.assertNotEqual(lab.idempotence_snapshot('probe'), before)

    def test_idempotence_requires_real_product_run_stable_identity_input_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'config.yml').write_text('original-config')
            before = {'helm': {'revision': '1'}, 'objects': {'pod/probe/writer': {'uid': 'original'}}}
            for drift in ('none', 'revision', 'pod', 'config', 'data'):
                after = copy.deepcopy(before)
                if drift == 'revision': after['helm']['revision'] = '2'
                if drift == 'pod': after['objects']['pod/probe/writer']['uid'] = 'replacement'
                def product(pid, step):
                    self.assertEqual((pid, step), (23456, 'longhorn-setup'))
                    if drift == 'config': (root / 'config.yml').write_text('changed-config')
                with self.subTest(drift=drift), patch.object(lab, 'DIRECTORY', root), \
                        patch.object(lab, 'idempotence_snapshot', side_effect=[before, after]), \
                        patch.object(lab, 'write_private'), patch.object(lab, 'supervise_core', side_effect=product) as run, \
                        patch.object(lab, 'installation_health'), patch.object(lab, 'kubectl', return_value=
                        subprocess.CompletedProcess([], 0, 'corrupt' if drift == 'data' else 'confirmed', '')):
                    if drift == 'none': lab.verify_idempotence(23456, {'namespace': 'probe'}, 'confirmed')
                    else:
                        with self.assertRaises(ValueError): lab.verify_idempotence(23456, {'namespace': 'probe'}, 'confirmed')
                    run.assert_called_once()
                (root / 'config.yml').write_text('original-config')

    def test_cache_accepts_only_media_translation_of_identical_pinned_content(self):
        source = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                  'config': {'digest': 'sha256:config', 'size': 12},
                  'layers': [{'digest': 'sha256:layer', 'size': 24}]}
        docker = copy.deepcopy(source);docker['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
        self.assertTrue(lab.equivalent_manifest(docker, source))
        for field in ('config', 'layers'):
            changed = copy.deepcopy(docker)
            descriptor = changed[field][0] if field == 'layers' else changed[field]
            descriptor['digest'] = 'sha256:foreign'
            self.assertFalse(lab.equivalent_manifest(changed, source))
        changed = copy.deepcopy(docker);changed['layers'][0]['size'] += 1
        self.assertFalse(lab.equivalent_manifest(changed, source))
        self.assertFalse(lab.equivalent_manifest(docker | {'mediaType': 'index'}, source))

    def test_published_manifest_falls_back_and_reuses_only_digest_verified_bytes(self):
        raw = '{"schemaVersion":2,"config":{},"layers":[]}\n'
        digest = 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()
        row = {'key': 'brinnatt/fixture:v1', 'talkedu_ref': 'hub.talkedu.cn/kubeauto/fixture:v1',
               'dockerhub_ref': 'brinnatt/fixture:v1'}
        for fault in ('none', 'wrong-digest', 'both-unavailable', 'corrupt-cache', 'symlink-cache'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                responses = [subprocess.CompletedProcess([], 1, '', 'connect: connection refused'),
                    subprocess.CompletedProcess([], 1 if fault == 'both-unavailable' else 0,
                                                raw + (' ' if fault == 'wrong-digest' else ''), '')]
                with patch.object(lab, 'DIRECTORY', root), patch.object(lab, 'execute', side_effect=responses) as run:
                    if fault in ('wrong-digest', 'both-unavailable'):
                        with self.assertRaises(ValueError): lab.published_manifest(row, digest)
                        self.assertFalse(list(root.rglob('*.json')))
                        continue
                    self.assertEqual(lab.published_manifest(row, digest), json.loads(raw))
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(run.call_args_list[0].args[0][-1],
                                     'docker://hub.talkedu.cn/kubeauto/fixture@' + digest)
                    self.assertEqual(run.call_args_list[1].args[0][-1], 'docker://docker.io/brinnatt/fixture@' + digest)
                    cached = next(root.rglob('*.json'))
                    if fault == 'corrupt-cache': cached.write_text(raw + ' ')
                    if fault == 'symlink-cache':
                        cached.unlink(); target = root/'foreign'; target.write_text(raw); cached.symlink_to(target)
                    if fault == 'none': self.assertEqual(lab.published_manifest(row, digest), json.loads(raw))
                    else:
                        with self.assertRaises(ValueError): lab.published_manifest(row, digest)
                    self.assertEqual(run.call_count, 2, 'verified cache needs no remote read; corruption must stop')

    def test_recompressed_cache_verifies_bytes_against_pinned_config_and_ordered_diffids(self):
        import gzip
        def descriptor(raw, media):
            return {'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'mediaType': media}
        payloads = [b'first changeset', b'second changeset']
        config = json.dumps({'architecture': 'amd64', 'os': 'linux', 'rootfs': {'type': 'layers',
            'diff_ids': ['sha256:' + hashlib.sha256(p).hexdigest() for p in payloads]}}).encode()
        source = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': descriptor(config, 'application/vnd.oci.image.config.v1+json'),
            'layers': [descriptor(gzip.compress(p, mtime=1), 'application/vnd.oci.image.layer.v1.tar+gzip') for p in payloads]}
        actual = copy.deepcopy(source)
        actual['mediaType'] = 'application/vnd.docker.distribution.manifest.v2+json'
        actual['config']['mediaType'] = 'application/vnd.docker.container.image.v1+json'
        blobs = {actual['config']['digest']: config}
        actual['layers'] = []
        for payload in payloads:
            raw = gzip.compress(payload, mtime=2)
            layer = descriptor(raw, 'application/vnd.docker.image.rootfs.diff.tar.gzip')
            blobs[layer['digest']] = raw; actual['layers'].append(layer)
        for fault in ('none', 'config', 'config-blob', 'layer-blob', 'size', 'order', 'diffid', 'count', 'media'):
            candidate = copy.deepcopy(actual); data = dict(blobs)
            if fault == 'config': candidate['config']['digest'] = 'sha256:'+'0'*64
            if fault == 'config-blob': data[candidate['config']['digest']] = config+b' '
            if fault == 'layer-blob': data[candidate['layers'][0]['digest']] += b'corrupt'
            if fault == 'size': candidate['layers'][0]['size'] += 1
            if fault == 'order': candidate['layers'].reverse()
            if fault == 'count': candidate['layers'].pop()
            if fault == 'media': candidate['layers'][0]['mediaType'] = 'application/unknown'
            if fault == 'diffid':
                raw = gzip.compress(b'foreign filesystem', mtime=2)
                candidate['layers'][0] = descriptor(raw, actual['layers'][0]['mediaType'])
                data[candidate['layers'][0]['digest']] = raw
            def execute(argv, **kwargs):
                self.assertEqual(argv[0], 'curl')
                self.assertEqual(argv[-1].split('/blobs/')[0], 'http://127.0.0.1:5000/v2/brinnatt/fixture')
                Path(argv[argv.index('--output')+1]).write_bytes(data[argv[-1].rsplit('/', 1)[1]])
                return subprocess.CompletedProcess(argv, 0, '', '')
            with self.subTest(fault=fault), patch.object(lab, 'execute', side_effect=execute) as run:
                if fault == 'none':
                    verified = set()
                    self.assertTrue(lab.verify_recompressed_manifest(candidate, source, 'brinnatt/fixture', verified))
                    self.assertEqual(run.call_count, 3)
                    self.assertTrue(lab.verify_recompressed_manifest(candidate, source, 'brinnatt/fixture', verified))
                    self.assertEqual(run.call_count, 4, 'config must be verified; already hashed layers are reusable')
                else:
                    with self.assertRaises(ValueError):
                        lab.verify_recompressed_manifest(candidate, source, 'brinnatt/fixture', set())

    def test_host_journal_restores_services_and_only_exact_owned_files(self):
        script = lab.host_state_script()
        compile(script, '<host-state>', 'exec')
        with tempfile.TemporaryDirectory() as directory:
            filename = str(Path(directory) / 'owned')
            digest = __import__('hashlib').sha256(b'public-product-file').hexdigest()
            units = {name: {'LoadState': 'loaded', 'ActiveState': 'inactive', 'UnitFileState': 'disabled'}
                     for name in ('iscsid.service', 'iscsid.socket')}
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                if argv[0] == 'iscsiadm': return subprocess.CompletedProcess(argv, 21)
                self.assertEqual(argv[0], 'systemctl')
                if argv[1] == 'show':
                    return subprocess.CompletedProcess(argv, 0, '\n'.join(k+'='+v for k,v in units[argv[2]].items()), '')
                if argv[1] == 'stop': units[argv[2]]['ActiveState'] = 'inactive'
                elif argv[1] == 'disable': units[argv[2]]['UnitFileState'] = 'disabled'
                else: self.fail('unexpected service mutation: ' + str(argv))
                return subprocess.CompletedProcess(argv, 0)
            def invoke(request):
                with patch('sys.stdin', io.StringIO(json.dumps(request))), patch('sys.stdout', new_callable=io.StringIO) as out, \
                        patch('subprocess.run', side_effect=run):
                    try: exec(script, {})
                    except SystemExit as error: self.assertEqual(error.code, 0)
                    return out.getvalue()
            request = {'action': 'capture', 'expected': {filename: digest}}
            before = json.loads(invoke(request))
            self.assertIsNone(before['files'][filename])
            for values in units.values(): values.update(ActiveState='active', UnitFileState='enabled')
            Path(filename).write_text('foreign-data'); calls.clear()
            with self.assertRaisesRegex(AssertionError, 'foreign host file'):
                invoke(request | {'action': 'restore', 'before': before})
            self.assertFalse(calls, 'foreign file must block service changes')
            Path(filename).write_bytes(b'public-product-file')
            invoke(request | {'action': 'restore', 'before': before})
            self.assertFalse(Path(filename).exists())
            for values in units.values(): self.assertEqual(values, before['units']['iscsid.service'])

    def test_host_restore_accepts_product_reentry_only_for_fixture_created_files(self):
        import hashlib
        script = lab.host_state_script()
        units = {name: {'LoadState': 'loaded', 'ActiveState': 'inactive', 'UnitFileState': 'disabled'}
                 for name in ('iscsid.service', 'iscsid.socket')}
        mutations = []
        def run(argv, **kwargs):
            if argv[0] == 'iscsiadm': return subprocess.CompletedProcess(argv, 21)
            self.assertEqual(argv[0], 'systemctl')
            if argv[1] == 'show':
                return subprocess.CompletedProcess(argv, 0, '\n'.join(k+'='+v for k,v in units[argv[2]].items()), '')
            mutations.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        def invoke(request):
            with patch('sys.stdin', io.StringIO(json.dumps(request))), patch('subprocess.run', side_effect=run):
                try: exec(script, {})
                except SystemExit as error: self.assertEqual(error.code, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'checker'
            old = hashlib.sha256(b'original-product-version').hexdigest()
            new = hashlib.sha256(b'updated-product-version').hexdigest()
            request = {'action': 'check', 'expected': {str(path): old}, 'installed': {str(path): new},
                       'before': {'files': {str(path): None}, 'units': units}}
            path.write_bytes(b'updated-product-version')
            # Exact reproduction: the old checker rejected this owned update.
            invoke(request)
            self.assertTrue(path.exists()); self.assertEqual(mutations, [])
            invoke(request | {'action': 'restore'})
            self.assertFalse(path.exists())
            mutations.clear()
            path.write_bytes(b'foreign-data')
            with self.assertRaisesRegex(AssertionError, 'foreign host file'): invoke(request)
            self.assertTrue(path.exists()); self.assertEqual(mutations, [])
            path.write_bytes(b'updated-product-version')
            original = {'sha256': old, 'mode': 0o644, 'uid': path.stat().st_uid, 'gid': path.stat().st_gid}
            request['before']['files'][str(path)] = original
            with self.assertRaisesRegex(AssertionError, 'foreign host file'):
                invoke(request | {'action': 'restore'})
            self.assertEqual(mutations, [])
            path.write_bytes(b'original-product-version')
            invoke(request | {'action': 'restore'})
            self.assertEqual(path.read_bytes(), b'original-product-version')
            mutations.clear(); request['installed']['/foreign-path'] = new
            with self.assertRaisesRegex(AssertionError, 'host journal file set changed'): invoke(request)
            self.assertEqual(mutations, [])

    def test_host_restore_checks_every_host_before_mutation(self):
        before = {'files': {}, 'units': {}}
        state = {'host_state': {'expected': {}, 'nodes': {n: before for n in lab.NODES}}}
        calls = []
        def remote(node, argv, *, stdin):
            request = json.loads(stdin); calls.append((node, request['action']))
            if node == lab.NODES[-1]: raise ValueError('foreign final host')
        with patch.object(lab, 'core_identity'), patch.object(lab, 'owner', return_value=state), \
                patch.object(lab, 'execute'), patch.object(lab, 'host_files', return_value={}), \
                patch.object(lab, 'remote', side_effect=remote):
            with self.assertRaisesRegex(ValueError, 'foreign final host'): lab.restore_hosts()
        self.assertEqual(calls, [(n, 'check') for n in lab.NODES])

    def test_inventory_binds_exact_six_hosts_and_deployment(self):
        text = lab.inventory()
        self.assertIn('cluster_dir="{{ base_dir }}/clusters/longhorn-gate"', text)
        self.assertIn('CLUSTER_CIDR="172.30.0.0/16"', text)
        for address in lab.NODES: self.assertIn(address, text)
        self.assertNotIn('192.168.122.1 ', text)

    def test_disk_plan_selects_one_exact_qualified_disk_per_worker(self):
        run = str(uuid.uuid4()); plan = lab.disk_plan(allowlist(), run)
        self.assertEqual(set(plan), set(lab.WORKERS))
        self.assertEqual(len({r['uuid'] for r in plan.values()}), 3)
        for node, row in plan.items():
            self.assertEqual(row['kernel'], '/dev/vdb')
            self.assertEqual(row['uuid'], str(uuid.uuid5(uuid.UUID(run), node)))
            self.assertEqual(row['phase'], 'planned')

    def test_last_disk_preflight_failure_prevents_all_formats_and_journal_changes(self):
        state = {'run_id': str(uuid.uuid4()), 'disks': {}}
        calls = []
        def check(node, row):
            calls.append(node)
            if node == lab.WORKERS[-1]: raise ValueError('last disk is not blank')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'allowlist';path.write_text(allowlist())
            with patch.object(lab, 'core_identity'), patch.object(lab, 'owner', return_value=state), \
                    patch.object(lab, 'check_blank_disk', side_effect=check), \
                    patch.object(lab, 'write_private', side_effect=AssertionError('premature journal')), \
                    patch.object(lab, 'remote', side_effect=AssertionError('premature format')):
                with self.assertRaisesRegex(ValueError, 'last disk'): lab.prepare_disks(path)
        self.assertEqual(calls, list(lab.WORKERS))
        self.assertEqual(state['disks'], {})

    def test_incomplete_duplicate_and_foreign_disk_plans_reject(self):
        raw = allowlist(); lines = raw.splitlines()
        for bad in ('\n'.join(lines[:-1]), raw.replace(lab.WORKERS[0], '192.168.122.1'),
                    raw.replace('/dev/disk/by-path/pci-0000:09:00.0', '/dev/disk/by-path/pci-0000:08:00.0')):
            with self.assertRaises(ValueError): lab.disk_plan(bad, str(uuid.uuid4()))

    def test_identity_change_or_partitioned_disk_rejects(self):
        row = lab.disk_plan(allowlist(), str(uuid.uuid4()))[lab.WORKERS[0]]
        current = {'path': row['kernel'], 'type': 'disk', 'size': row['size'], 'ro': False}
        lab.verify_device(row, current)
        for delta in ({'path': '/dev/vda'}, {'children': [{'name': 'partition'}]}, {'size': 10}, {'ro': True}):
            with self.assertRaises(ValueError): lab.verify_device(row, current | delta)

    def test_foreign_remote_host_is_never_contacted(self):
        with patch.object(lab, 'execute', side_effect=AssertionError('external command')):
            with self.assertRaisesRegex(ValueError, 'foreign'): lab.remote('192.168.122.1', ['true'])

    def test_remote_scripts_stop_at_the_first_unmet_precondition(self):
        with patch.object(lab, 'execute') as execute:
            lab.remote(lab.WORKERS[0], script='test ! -e /data/fixture; echo mutation')
        self.assertEqual(execute.call_args.args[0][-1], 'set -eu\ntest ! -e /data/fixture; echo mutation')

    def test_cleanup_validates_every_device_before_any_mutation(self):
        state = {'run_id': str(uuid.uuid4())}; state['disks'] = lab.disk_plan(allowlist(), state['run_id'])
        calls = []
        def remote(node, argv=None, **kwargs):
            calls.append((node, argv))
            if argv and argv[0] == 'lsblk':
                row = state['disks'][node]
                current = {'path': row['kernel'], 'type': 'disk', 'size': row['size'], 'ro': False,
                           'uuid': 'foreign-filesystem', 'mountpoints': []}
                return subprocess.CompletedProcess([], 0, json.dumps({'blockdevices': [current]}), '')
            raise AssertionError('mutation before all identities verified')
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'core_identity'), \
                patch.object(lab, 'execute'), patch.object(lab, 'remote', side_effect=remote):
            with self.assertRaisesRegex(ValueError, 'UUID changed'): lab.cleanup_disks()
        self.assertEqual(len(calls), 1)

    def test_cleanup_checks_storage_guard_before_host_changes(self):
        state = {'run_id': str(uuid.uuid4())}; state['disks'] = lab.disk_plan(allowlist(), state['run_id'])
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'core_identity'), \
                patch.object(lab, 'execute', side_effect=ValueError('volumes remain')), \
                patch.object(lab, 'remote', side_effect=AssertionError('host change')):
            with self.assertRaisesRegex(ValueError, 'volumes remain'): lab.cleanup_disks()

    def test_generated_cleanup_python_is_valid_before_reaching_host(self):
        state = {'run_id': str(uuid.uuid4())}; state['disks'] = lab.disk_plan(allowlist(), state['run_id'])
        for row in state['disks'].values(): row['fstab_line'] = 'unit-owned-line'
        scripts = []
        def remote(node, argv=None, **kwargs):
            if argv and argv[0] == 'lsblk':
                row = state['disks'][node]
                return subprocess.CompletedProcess([], 0, json.dumps({'blockdevices': [{
                    'path': row['kernel'], 'type': 'disk', 'size': row['size'], 'ro': False,
                    'uuid': row['uuid'], 'mountpoints': [lab.DATA]}]}), '')
            if argv and argv[0] == 'udevadm':
                return subprocess.CompletedProcess([], 0, 'ID_PATH=' + state['disks'][node]['identity'][5:] + '\n', '')
            if argv and argv[0] == 'python3':
                compile(argv[2], '<generated-cleanup>', 'exec'); scripts.append(argv[2])
            return subprocess.CompletedProcess([], 0, state['disks'][node]['uuid'] if argv and argv[0] == 'blkid' else '', '')
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'core_identity'), \
                patch.object(lab, 'execute'), patch.object(lab, 'remote', side_effect=remote), patch.object(lab, 'write_private'):
            lab.cleanup_disks()
        self.assertEqual(len(scripts), 15)
        self.assertEqual(sum(s == lab.disk_parent_script() for s in scripts), 6)
        self.assertEqual(sum('foreign host receipt' in s and 'foreign kubelet guard' in s for s in scripts), 6)

    def test_foreign_last_host_receipt_blocks_all_disk_mutation(self):
        state = {'run_id': str(uuid.uuid4())}; state['disks'] = lab.disk_plan(allowlist(), state['run_id'])
        def remote(node, argv=None, **kwargs):
            if argv[0] == 'lsblk':
                row = state['disks'][node]
                return subprocess.CompletedProcess([], 0, json.dumps({'blockdevices': [{
                    'path': row['kernel'], 'type': 'disk', 'size': row['size'], 'ro': False,
                    'uuid': row['uuid'], 'mountpoints': [lab.DATA]}]}), '')
            if argv[0] == 'udevadm':
                return subprocess.CompletedProcess([], 0, 'ID_PATH=' + state['disks'][node]['identity'][5:] + '\n', '')
            if argv[0] == 'python3' and argv[-1] == 'check':
                if node == lab.WORKERS[-1]: raise ValueError('foreign host receipt')
                return subprocess.CompletedProcess([], 0, '', '')
            if argv[0] == 'test': return subprocess.CompletedProcess([], 0, '', '')
            raise AssertionError('mutation before every host preflight completed')
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'core_identity'), \
                patch.object(lab, 'execute'), patch.object(lab, 'remote', side_effect=remote):
            with self.assertRaisesRegex(ValueError, 'foreign host receipt'): lab.cleanup_disks()

    def test_cleanup_failure_preserves_first_error_and_interrupt(self):
        for error in (ValueError('primary'), KeyboardInterrupt()):
            with patch('sys.stderr', new_callable=io.StringIO) as output:
                with self.assertRaises(type(error)) as raised:
                    with lab.scoped_cleanup(lambda: (_ for _ in ()).throw(RuntimeError('cleanup'))):
                        raise error
                self.assertIs(raised.exception, error)
                self.assertIn('LONGHORN_CLEANUP_FAIL', output.getvalue())
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            with lab.scoped_cleanup(lambda: (_ for _ in ()).throw(RuntimeError('cleanup'))): pass

    def test_business_object_creation_never_overwrites_an_earlier_uid(self):
        journal = {'objects': {'Pod/reader': 'deleted-reader'}}
        obj = lab.client_pod('owned', 'reader', 'data', 'node')
        with patch.object(lab, 'kubectl') as command, patch.object(lab, 'write_private') as save:
            with self.assertRaisesRegex(ValueError, 'already journaled'):
                lab.create_business_object(journal, Path('/unused'), obj)
            command.assert_not_called(); save.assert_not_called()
        obj['metadata']['name'] = 'remount-reader'
        created = copy.deepcopy(obj); created['metadata']['uid'] = 'new-reader'
        with patch.object(lab, 'kubectl', return_value=subprocess.CompletedProcess([], 0, json.dumps(created), '')), \
                patch.object(lab, 'write_private'):
            lab.create_business_object(journal, Path('/unused'), obj)
        self.assertEqual(journal['objects'], {'Pod/reader': 'deleted-reader', 'Pod/remount-reader': 'new-reader'})

    def test_rwo_and_rwx_use_a_distinct_remount_name_and_retain_event_ownership(self):
        import ast
        tree = ast.parse((ROOT/'tests/helpers/longhorn-lab.py').read_text())
        business = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'business')
        names = [call.args[1].value for call in ast.walk(business) if isinstance(call, ast.Call)
                 and isinstance(call.func, ast.Name) and call.func.id == 'client_pod']
        self.assertEqual(names, ['writer', 'remount-reader'])
        journal = {'namespace': 'owned', 'namespace_uid': 'namespace-uid',
                   'objects': {'Pod/reader': 'earlier-reader', 'Pod/remount-reader': 'new-reader'}}
        event = {'kind': 'Event', 'metadata': {'uid': 'event-uid'}, 'involvedObject': {'uid': 'earlier-reader'}}
        def get(kind, *args, **kwargs):
            return {'metadata': {'uid': 'namespace-uid'}} if kind == 'namespace' else {'items': [event]}
        with patch.object(lab, 'get', side_effect=get), \
                patch.object(lab, 'kubectl', return_value=subprocess.CompletedProcess([], 0, 'events', '')):
            lab.check_business_graph(journal)
            event['involvedObject']['uid'] = 'foreign-reader'
            with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)

    def test_historical_event_recovery_is_bound_to_proven_namespace_objects_and_event(self):
        proof = json.loads((ROOT/'tests/evidence/longhorn-rwo-first-failure-20261019.json').read_text())['event_recovery']
        journal = {key: copy.deepcopy(proof[key]) for key in ('namespace', 'namespace_uid', 'objects')}
        event = proof['events'][0]
        obj = {'kind': 'Event', 'metadata': copy.deepcopy(event['metadata']), 'involvedObject': copy.deepcopy(event['reference'])}
        resource = 'events'
        def get(kind, *args, **kwargs):
            return {'metadata': {'uid': journal['namespace_uid']}} if kind == 'namespace' else {'items': [obj]}
        with patch.object(lab, 'get', side_effect=get), \
                patch.object(lab, 'kubectl', side_effect=lambda *args: subprocess.CompletedProcess([], 0, resource, '')):
            for resource in ('events', 'events.events.k8s.io'): lab.check_business_graph(journal)
            for target, key in ((obj['metadata'], 'uid'), (obj['metadata'], 'name'),
                                (obj['involvedObject'], 'uid'), (obj['involvedObject'], 'namespace')):
                with self.subTest(key=key), patch.dict(target, {key: 'foreign'}):
                    with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)
            for key in ('namespace', 'namespace_uid', 'objects'):
                with self.subTest(identity=key), patch.dict(journal, {key: {} if key == 'objects' else 'foreign'}):
                    with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)
            obj = {'kind': 'Pod', 'metadata': {'name': 'reader', 'uid': event['reference']['uid']}}
            resource = 'pods'
            with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)
            obj['kind'] = 'Event'; obj['involvedObject'] = event['reference']; obj['metadata'] = event['metadata']
            resource = 'foreign.example'
            with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)

    def test_namespace_graph_includes_custom_resources_and_rejects_foreign_uid(self):
        journal = {'namespace': 'longhorn-business-unit', 'namespace_uid': 'namespace-uid',
                   'objects': {'Pod/writer': 'writer-uid'}, 'defaults': ['default-uid', 'rootca-uid']}
        objects = {'pod': [{'kind': 'Pod', 'metadata': {'uid': 'writer-uid'}}],
                   'secret': [], 'example.test': [],
                   'events': [{'kind': 'Event', 'metadata': {'uid': 'event-uid'}, 'involvedObject': {'uid': 'writer-uid'}}]}
        def get(kind, *args, **kwargs):
            return {'metadata': {'uid': 'namespace-uid'}} if kind == 'namespace' else {'items': objects[kind]}
        with patch.object(lab, 'kubectl', return_value=subprocess.CompletedProcess([], 0, '\n'.join(objects), '')), \
                patch.object(lab, 'get', side_effect=get):
            lab.check_business_graph(journal)
            objects['example.test'] = [{'kind': 'Example', 'metadata': {'uid': 'foreign-uid'}}]
            with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)

    def test_namespace_graph_accepts_only_derived_pod_metrics_without_uid(self):
        journal = {'namespace': 'longhorn-business-unit', 'namespace_uid': 'namespace-uid',
                   'objects': {'Pod/writer': 'writer-uid'}}
        metrics = {'apiVersion': 'metrics.k8s.io/v1beta1', 'kind': 'PodMetrics',
                   'metadata': {'name': 'writer', 'namespace': journal['namespace']}}
        objects = {'pods': [{'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'uid': 'writer-uid'}}],
                   'pods.metrics.k8s.io': [metrics]}
        def get(kind, *args, **kwargs):
            return {'metadata': {'uid': 'namespace-uid'}} if kind == 'namespace' else {'items': objects[kind]}
        with patch.object(lab, 'kubectl', side_effect=lambda *args: subprocess.CompletedProcess([], 0, '\n'.join(objects), '')), \
                patch.object(lab, 'get', side_effect=get):
            lab.check_business_graph(journal)
            for field, value in (('apiVersion', 'example.test/v1'), ('kind', 'Example')):
                with self.subTest(field=field), patch.dict(metrics, {field: value}):
                    with self.assertRaisesRegex(ValueError, 'no ownership UID'): lab.check_business_graph(journal)
            objects['example.test'] = [metrics]
            with self.assertRaisesRegex(ValueError, 'no ownership UID'): lab.check_business_graph(journal)
            del objects['example.test']
            objects['pods'][0]['metadata']['uid'] = 'foreign-pod'
            with self.assertRaisesRegex(ValueError, 'foreign resource'): lab.check_business_graph(journal)

    def test_cleanup_binds_retained_volume_before_pvc_bound(self):
        journal = {'namespace': 'longhorn-business-unit', 'objects': {'PersistentVolumeClaim/data': 'claim-uid'}}
        pv = {'metadata': {'name': 'pvc-claim-uid', 'uid': 'pv-uid'}, 'spec': {
            'claimRef': {'namespace': journal['namespace'], 'name': 'data', 'uid': 'claim-uid'},
            'storageClassName': 'longhorn', 'persistentVolumeReclaimPolicy': 'Retain',
            'csi': {'driver': 'driver.longhorn.io', 'volumeHandle': 'native-volume'}}}
        def get(kind, *args, **kwargs):
            return {'items': [pv]} if kind == 'pv' else {'metadata': {'uid': 'volume-uid'}}
        with patch.object(lab, 'get', side_effect=get), patch.object(lab, 'write_private'):
            lab.bind_business_volume(journal, Path('/unused'))
            self.assertEqual(journal['pv_uid'], 'pv-uid')
            self.assertEqual(journal['volume_uid'], 'volume-uid')
            pv['metadata']['uid'] = 'replacement-uid'
            with self.assertRaisesRegex(ValueError, 'identity changed'): lab.bind_business_volume(journal, Path('/unused'))

    def test_relay_journal_refuses_foreign_nodes_paths_and_duplicates_before_calls(self):
        row = {'node': lab.NODES[0], 'socket': str(lab.DIRECTORY / ('longhorn-registry-' + lab.NODES[0] + '.sock'))}
        self.assertEqual(lab.relay_rows({'relays': [row]}), [row])
        for rows in ([dict(row, node='192.168.122.1')], [dict(row, socket='/tmp/foreign.sock')], [row, row]):
            with self.assertRaises(ValueError): lab.relay_rows({'relays': rows})

    def test_stale_listener_recovery_binds_uid_process_port_boot_and_run_window(self):
        listener = {'pid': 1234, 'start_ticks': 23456, 'start_time': 1005,
                    'boot_id': str(uuid.uuid4()), 'uid': 0, 'command': 'sshd-session: root',
                    'binding': '127.0.0.1:23520'}
        lab.validate_recovery_listener(listener, 1000, 1010)
        for delta in ({'uid': 1000}, {'command': 'foreign-server'}, {'binding': '0.0.0.0:23520'},
                      {'start_time': 999}, {'start_time': 1011}, {'boot_id': 'invalid'}, {'pid': 1}):
            with self.assertRaises(ValueError): lab.validate_recovery_listener(listener | delta, 1000, 1010)
        script = lab.listener_script(); compile(script, '<recovery>', 'exec')
        self.assertLess(script.index("assert result==request['expected']"), script.index('os.kill('))

    def test_listener_exit_during_cleanup_probe_is_rechecked_without_signalling(self):
        row = 'LISTEN 0 128 127.0.0.1:23520 0.0.0.0:* users:(("sshd-session",pid=1234,fd=9))\n'
        with patch('sys.stdin', io.StringIO('{"port":23520,"action":"probe"}')), \
                patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('subprocess.check_output', side_effect=[row, '']) as query, \
                patch.object(Path, 'read_bytes', side_effect=FileNotFoundError), patch('os.kill') as kill:
            exec(compile(lab.listener_script(), '<listener-probe>', 'exec'), {})
        self.assertEqual(query.call_count, 2)
        self.assertEqual(output.getvalue().strip(), 'null')
        kill.assert_not_called()

    def test_listener_recovery_refuses_initialized_storage_before_remote_calls(self):
        state = {'cluster_uid': 'cluster', 'disks': {}, 'healthy': False, 'relays': []}
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'remote', side_effect=AssertionError('host access')):
            with self.assertRaisesRegex(ValueError, 'pre-core'): lab.relay_recover()

    def test_dead_registry_supervisor_blocks_product_launch(self):
        with patch.object(lab, 'owner'), patch.object(lab, 'process_identity', return_value=None), \
                patch.object(lab.subprocess, 'Popen', side_effect=AssertionError('product launched')):
            with self.assertRaisesRegex(ValueError, 'prerequisite'): lab.supervise_core(999999, 'setup')

    def test_registry_supervisor_exit_terminates_running_product_before_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); marker = base / 'continued'
            (base / 'kubecli.py').write_text('import pathlib,time\ntime.sleep(20)\npathlib.Path(' + repr(str(marker)) + ').touch()\n')
            relay_pid = 999999; calls = 0; launched = []
            original_identity = lab.process_identity; original_popen = subprocess.Popen
            def identity(pid):
                nonlocal calls
                if pid != relay_pid: return original_identity(pid)
                calls += 1
                return ([b'python', str(Path(lab.__file__).resolve()).encode(), b'relay-maintain'], '123') if calls < 3 else None
            def launch(*args, **kwargs):
                p = original_popen(*args, **kwargs); launched.append(p); return p
            started = time.monotonic()
            with patch.object(lab, 'BASE', base), patch.object(lab, 'owner'), \
                    patch.object(lab, 'process_identity', side_effect=identity), patch.object(lab.subprocess, 'Popen', side_effect=launch):
                with self.assertRaisesRegex(ValueError, 'stopping'): lab.supervise_core(relay_pid, 'setup')
            self.assertLess(time.monotonic() - started, 5)
            self.assertIsNotNone(launched[0].poll())
            self.assertFalse(marker.exists())

    def test_product_cancellation_refuses_replacement_identity(self):
        process = unittest.mock.Mock(pid=1234); process.poll.return_value = None
        with patch.object(lab, 'process_identity', return_value=([b'foreign'], '123')), patch.object(lab.os, 'kill') as kill:
            with self.assertRaisesRegex(ValueError, 'identity'): lab.stop_product(process, ['expected'])
        kill.assert_not_called()

    def test_relay_rule_has_exact_target_port_and_owner(self):
        rule = lab.relay_rule('-A')
        self.assertIn('192.168.47.130/32', rule)
        self.assertIn('kubeauto-longhorn-registry', rule)
        self.assertEqual(rule[-1], '23520')
        with self.assertRaises(ValueError): lab.relay_rule('-F')

    def test_unjournaled_relay_rule_is_not_adopted(self):
        state = {'relays': []}
        with patch.object(lab, 'owner', return_value=state), patch.object(lab, 'relay_probe', return_value=False), \
                patch.object(lab, 'remote', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                patch.object(lab, 'relay_connect', side_effect=AssertionError('new listener')):
            with self.assertRaisesRegex(ValueError, 'foreign'): lab.relay_ensure()

    def test_relay_qualification_finally_cleans_after_setup_failure(self):
        with patch.object(lab, 'relay_ensure', side_effect=ValueError('setup failed')), patch.object(lab, 'relay_cleanup') as cleanup:
            with self.assertRaisesRegex(ValueError, 'setup failed'): lab.relay_qualification()
        cleanup.assert_called_once()

    def test_business_pod_uses_pinned_client_and_exact_node_claim(self):
        pod = lab.client_pod('longhorn-business-unit', 'writer', 'data', 'longhorn-node-212')
        self.assertEqual(pod['spec']['nodeSelector'], {'kubernetes.io/hostname': 'longhorn-node-212'})
        self.assertEqual(pod['spec']['volumes'][0]['persistentVolumeClaim']['claimName'], 'data')
        self.assertIn(':3.11.17-redis6.4.0-cli8.10.2', pod['spec']['containers'][0]['image'])
        self.assertEqual(pod['spec']['restartPolicy'], 'Never')

    def test_replica_gate_requires_all_three_healthy_independent_nodes(self):
        volume = {'status': {'robustness': 'healthy'}}
        replicas = [{'spec': {'volumeName': 'volume', 'nodeID': 'longhorn-node-' + ip.rsplit('.', 1)[1],
                              'healthyAt': '2026-10-19T00:00:00Z', 'failedAt': ''}, 'status': {'currentState': 'running'}} for ip in lab.WORKERS]
        def get(kind, *args, **kwargs): return volume if kind == 'volumes.longhorn.io' else {'items': replicas}
        with patch.object(lab, 'get', side_effect=get):
            self.assertEqual(len(lab.healthy_replicas('volume')), 3)
            replicas[1]['spec']['nodeID'] = replicas[0]['spec']['nodeID']
            self.assertIsNone(lab.healthy_replicas('volume'))
            replicas[1]['spec']['nodeID'] = 'longhorn-node-165'; replicas[0]['spec']['failedAt'] = 'failure'
            self.assertIsNone(lab.healthy_replicas('volume'))

    def test_tls_fixture_passes_pinned_product_certificate_checks(self):
        from tests.unit.test_longhorn_delivery import product, configuration, FakeAPI
        import base64
        api = FakeAPI(); c = configuration(); c['longhorn_grpc_tls_required'] = True
        obj = {'metadata': {'name': 'longhorn-grpc-tls'}, 'data': {
            k: base64.b64encode(v.encode()).decode() for k, v in lab.tls_fixture().items()}}
        api.put('secret', obj, 'longhorn-system')
        product.check_secrets(c, api)



    def test_node_retired_event_recovery_preserves_exact_namespace_objects_and_event_details(self):
        proof = json.loads((ROOT/'tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json').read_text())['event_recovery']
        journal = {key: copy.deepcopy(proof[key]) for key in ('namespace', 'namespace_uid', 'objects')}
        for archived in proof['events']:
            for modern in (False, True):
                with self.subTest(event=archived['metadata']['name'], modern=modern):
                    obj = {'apiVersion': 'events.k8s.io/v1' if modern else 'v1', 'kind': 'Event',
                        'metadata': dict(archived['metadata'], namespace=journal['namespace']),
                        'regarding' if modern else 'involvedObject': copy.deepcopy(archived['reference'])}
                    detail = archived['details']
                    obj.update(reason=detail['reason'], type=detail['type'])
                    obj['deprecatedSource' if modern else 'source'] = copy.deepcopy(detail['source'])
                    obj['reportingController' if modern else 'reportingComponent'] = detail['controller']
                    obj['note' if modern else 'message'] = detail['text']
                    resource = 'events.events.k8s.io' if modern else 'events'
                    def get(kind, *args, **kwargs):
                        return {'metadata': {'uid': journal['namespace_uid']}} if kind == 'namespace' else {'items': [obj]}
                    with patch.object(lab, 'owned_taint_eviction_event', return_value=False, create=True), \
                            patch.object(lab, 'get', side_effect=get), patch.object(lab, 'kubectl',
                            side_effect=lambda *args: subprocess.CompletedProcess([], 0, resource, '')):
                        lab.check_business_graph(journal)
                        for target, key, value in ((obj['metadata'], 'uid', 'foreign'),
                                (obj['metadata'], 'name', 'foreign'), (obj['metadata'], 'creationTimestamp', 'foreign'),
                                (obj, 'reason', 'foreign'), (obj, 'type', 'Warning' if obj['type']=='Normal' else 'Normal'),
                                (obj, 'reportingController' if modern else 'reportingComponent', 'foreign'),
                                (obj, 'note' if modern else 'message', 'foreign'),
                                (obj['regarding' if modern else 'involvedObject'], 'name', 'foreign')):
                            with self.subTest(key=key), patch.dict(target, {key: value}):
                                with self.assertRaisesRegex(ValueError, 'foreign resource'):
                                    lab.check_business_graph(journal)
                        for key in ('namespace', 'namespace_uid', 'objects'):
                            with self.subTest(identity=key), patch.dict(journal, {key: {} if key=='objects' else 'foreign'}):
                                with self.assertRaises(ValueError):lab.check_business_graph(journal)
                        obj['kind'] = 'Pod'; resource = 'pods'
                        with self.assertRaisesRegex(ValueError, 'foreign resource'):lab.check_business_graph(journal)

    def test_official_uidless_taint_eviction_events_require_recorded_pod_and_exact_typed_source(self):
        journal = {'namespace': 'owned', 'namespace_uid': 'ns-uid', 'objects': {'Pod/old': 'old-uid'}}
        for modern in (False, True):
            for prefix in ('Marking for deletion Pod ', 'Cancelling deletion of Pod '):
                reference = {'apiVersion': 'v1', 'kind': 'Pod', 'name': 'old', 'namespace': 'owned'}
                obj = {'apiVersion': 'events.k8s.io/v1' if modern else 'v1', 'kind': 'Event',
                    'metadata': {'uid': 'event-uid', 'namespace': 'owned'},
                    'regarding' if modern else 'involvedObject': reference,
                    'reason': 'TaintManagerEviction', 'type': 'Normal',
                    'deprecatedSource' if modern else 'source': {'component': 'taint-eviction-controller'},
                    'reportingController' if modern else 'reportingComponent': 'taint-eviction-controller',
                    'note' if modern else 'message': prefix+'owned/old'}
                resource = 'events.events.k8s.io' if modern else 'events'
                def get(kind, *args, **kwargs):
                    return {'metadata': {'uid': 'ns-uid'}} if kind=='namespace' else {'items':[obj]}
                with patch.object(lab, 'proven_business_events', return_value=[]), patch.object(lab,'get',side_effect=get), \
                        patch.object(lab,'kubectl',side_effect=lambda *args:subprocess.CompletedProcess([],0,resource,'')):
                    lab.check_business_graph(journal)
                    for target,key,value in ((obj,'apiVersion','foreign/v1'),(obj,'kind','Foreign'),
                            (obj,'reason','Foreign'),(obj,'type','Warning'),
                            (obj,'deprecatedSource' if modern else 'source',{'component':'foreign'}),
                            (obj,'reportingController' if modern else 'reportingComponent','foreign'),
                            (obj,'note' if modern else 'message','foreign'),(reference,'uid','foreign'),
                            (reference,'name','foreign'),(reference,'namespace','foreign'),
                            (reference,'apiVersion','apps/v1'),(reference,'kind','Deployment'),
                            (obj['metadata'],'namespace','foreign')):
                        with self.subTest(modern=modern,key=key),patch.dict(target,{key:value}):
                            with self.assertRaisesRegex(ValueError,'foreign resource'):lab.check_business_graph(journal)
                    resource='foreign.example'
                    with self.assertRaisesRegex(ValueError,'foreign resource'):lab.check_business_graph(journal)
                    resource='events';obj['metadata'].pop('uid')
                    with self.assertRaisesRegex(ValueError,'no ownership UID'):lab.check_business_graph(journal)


class RegistryMaintenanceContracts(unittest.TestCase):

    def setUp(self):
        spec = importlib.util.spec_from_file_location('registry_maintenance', ROOT / 'tests/helpers/longhorn-lab.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.transient = ValueError('Registry transport did not return HTTP success node=' + self.m.NODES[3])

    def run_case(self, ensure, sleep=None):
        handlers = {}
        with patch.object(self.m.signal, 'signal', side_effect=lambda sig, fn: handlers.setdefault(sig, fn)), patch.object(self.m, 'relay_ensure', side_effect=lambda: ensure(handlers)), patch.object(self.m.time, 'sleep', side_effect=lambda delay: sleep(handlers) if sleep else None):
            return self.m.relay_maintain()

    def test_one_failed_http_observation_does_not_abort_before_next_success(self):
        calls = []

        def ensure(handlers):
            calls.append(True)
            if len(calls) == 1:
                raise self.transient
            handlers[self.m.signal.SIGTERM]()
        self.run_case(ensure)
        self.assertEqual(len(calls), 2)

    def test_persistent_http_failure_still_raises_original_exception_after_three_samples(self):
        calls = []

        def ensure(handlers):
            calls.append(True)
            raise self.transient
        with self.assertRaises(ValueError) as error:
            self.run_case(ensure)
        self.assertIs(error.exception, self.transient)
        self.assertEqual(len(calls), 3)

    def test_foreign_listener_or_rule_failure_is_never_retried(self):
        for text in ('foreign relay socket/rule', 'replacement relay listener refuses cleanup', 'Registry transport did not return HTTP success node=unapproved'):
            calls = []

            def ensure(handlers):
                calls.append(True)
                raise ValueError(text)
            with self.assertRaisesRegex(ValueError, text):
                self.run_case(ensure)
            self.assertEqual(len(calls), 1)

    def test_non_http_error_is_not_swallowed(self):

        def ensure(handlers):
            raise TimeoutError('SSH prerequisite timeout')
        with self.assertRaises(TimeoutError):
            self.run_case(ensure)

    def test_signal_during_retry_wait_exits_without_another_probe(self):
        calls = []

        def ensure(handlers):
            calls.append(True)
            raise self.transient
        self.run_case(ensure, lambda handlers: handlers[self.m.signal.SIGTERM]())
        self.assertEqual(len(calls), 1)

    def test_healthy_loop_accepts_normal_shutdown_without_retry(self):
        calls = []

        def ensure(handlers):
            calls.append(True)
            handlers[self.m.signal.SIGTERM]()
        self.run_case(ensure)
        self.assertEqual(len(calls), 1)


class RegistryProbeBudgetContracts(unittest.TestCase):
    def test_connection_and_response_have_separate_bounded_budgets(self):
        with patch.object(lab, 'remote', return_value=subprocess.CompletedProcess([], 0)) as remote:
            self.assertTrue(lab.relay_probe(lab.NODES[0]))
        args, kwargs = remote.call_args
        self.assertEqual(args[0], lab.NODES[0])
        command = args[1]
        self.assertEqual(command[command.index('--connect-timeout') + 1], '5')
        self.assertEqual(command[command.index('--max-time') + 1], '30')
        self.assertEqual(command[-1], 'http://192.168.47.130:5000/v2/')
        self.assertFalse(kwargs['check'])

    def test_http_and_transport_errors_still_refuse_readiness(self):
        for rc in (7, 22, 28, 255):
            with self.subTest(rc=rc), patch.object(lab, 'remote', return_value=subprocess.CompletedProcess([], rc)):
                self.assertFalse(lab.relay_probe(lab.NODES[0]))

    def test_real_response_delayed_beyond_old_five_second_budget_succeeds(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        class DelayedResponse(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(5.2)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), DelayedResponse)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def local_probe(node, argv, **kwargs):
                self.assertEqual(node, lab.NODES[0])
                argv = [*argv[:-1], 'http://127.0.0.1:' + str(server.server_port) + '/v2/']
                return subprocess.run(argv, capture_output=True, timeout=35, **kwargs)

            with patch.object(lab, 'remote', side_effect=local_probe):
                self.assertTrue(lab.relay_probe(lab.NODES[0]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SSHConnectionBudgetContracts(unittest.TestCase):
    def test_owned_remote_uses_bounded_openssh_connection_attempts(self):
        with patch.object(lab, 'execute', return_value=subprocess.CompletedProcess([], 0)) as execute:
            lab.remote(lab.NODES[0], lab.relay_rule('-C'))
        command = execute.call_args.args[0]
        for option in ('BatchMode=yes', 'ConnectTimeout=10', 'ConnectionAttempts=3',
                       'ServerAliveInterval=5', 'ServerAliveCountMax=2'):
            self.assertEqual(command.count(option), 1)
        self.assertEqual(command[-2], 'root@' + lab.NODES[0])

    def test_real_ssh_retries_tcp_connection_but_not_established_handshake(self):
        import socket
        import threading

        # No SSH service or credentials: accept one local TCP connection, then
        # close during identification. This separates TCP retries from KEX/auth.
        reserve = socket.socket()
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
        reserve.close()
        accepted = []
        failures = []

        def delayed_listener():
            try:
                time.sleep(1.2)
                with socket.socket() as server:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server.bind(('127.0.0.1', port))
                    server.listen()
                    server.settimeout(4)
                    with server.accept()[0]:
                        accepted.append(True)
                    server.settimeout(.3)
                    try:
                        with server.accept()[0]:
                            accepted.append(True)
                    except socket.timeout:
                        pass
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=delayed_listener, daemon=True)
        thread.start()
        command = ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=1',
                   '-o', 'ConnectionAttempts=3', '-o', 'IdentityAgent=none',
                   '-o', 'IdentityFile=none', '-p', str(port), '127.0.0.1', 'true']
        single = [option.replace('ConnectionAttempts=3', 'ConnectionAttempts=1') for option in command]
        refused = subprocess.run(single, capture_output=True, text=True, timeout=3)
        self.assertEqual(refused.returncode, 255)
        self.assertIn('Connection refused', refused.stderr)
        result = subprocess.run(command, capture_output=True, text=True, timeout=8)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertEqual(accepted, [True])
        self.assertEqual(result.returncode, 255)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__': unittest.main()
