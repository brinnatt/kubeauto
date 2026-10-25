"""Exact fault ownership, native refusal and lost-response recovery contracts."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


BROKEN = load('longhorn-v2-broken-restore.py')
BACKUPS = load('longhorn-backups.py')
ACCOUNTS = load('longhorn-accounts.py')


class FaultContracts(unittest.TestCase):
    def fixture(self, directory):
        helpers = {'longhorn-backups.py': BACKUPS, 'longhorn-accounts.py': ACCOUNTS}
        lab = SimpleNamespace(DIRECTORY=Path(directory), load_helper=helpers.__getitem__,
            owner=Mock(return_value={'run_id': 'r', 'cluster_uid': 'c'}), get=Mock(return_value=None),
            write_private=lambda p, row: p.write_text(json.dumps(row)), remove=Mock(), kubectl=Mock())
        def wait(stage, timeout, check):
            value = check()
            if value is None or value is False:
                raise TimeoutError(stage)
            return value
        lab.wait = Mock(side_effect=wait)
        prefix = BACKUPS.volume_prefix('source')
        block = prefix+'blocks/aa/aa/'+'a'*64+'.blk'
        digest = {'size': 312, 'sha256': 'b'*64}
        objects = {block: digest.copy()}
        receipt = {'volume': 'source', 'backup': 'point', 'size': 2*1024**3, 'objects': copy.deepcopy(objects)}
        store = SimpleNamespace(directory=Path(directory),
            receipt=Mock(return_value={'run_id': 'r', 'cluster_uid': 'c', 'name': 'store', 'bucket': 'store'}),
            inspect=Mock(return_value={'Id': 'container', 'State': {'Running': True}}),
            object_digest=Mock(side_effect=lambda key: objects[key]),
            backup_receipt=Mock(return_value=copy.deepcopy(receipt)))
        probe = BROKEN.Probe(lab, store)
        events = []
        def command(action, target, *args, **kwargs):
            key = target.removeprefix('lh/store/')
            events.append((action, key))
            if action == 'stat':
                if key in objects:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'success'}))
                failure = {'status': 'error', 'error': {'cause': {'error': {'Code': 'NoSuchKey'}}}}
                return SimpleNamespace(returncode=1, stdout=json.dumps(failure))
            self.assertTrue(probe.record.exists(), 'mutation before durable intent')
            if action == 'cp':
                objects[args[0].removeprefix('lh/store/')] = objects[key].copy()
            elif action == 'rm':
                del objects[key]
            else:
                self.fail('unexpected object mutation')
            return SimpleNamespace(returncode=0, stdout='')
        probe.client.command = Mock(side_effect=command)
        probe.client.fail = Mock(side_effect=ValueError('unclassified object stat'))
        journal = {'volume': 'source', 'backups': {'point': {'uid': 'backup-uid', 'object_receipt': receipt}}}
        return lab, store, probe, journal, objects, block, events

    def test_exact_referenced_block_is_escrowed_and_repaired_before_receipt_release(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            lab, store, probe, journal, objects, block, events = self.fixture(d)
            row = probe.activate(journal, 'point')
            self.assertNotIn(block, objects)
            self.assertEqual(row['object'], objects[row['escrow']])
            probe.cleanup()
            self.assertEqual({block: row['object']}, objects)
            self.assertFalse(probe.record.exists()); lab.remove.assert_not_called()
            self.assertLess(events.index(('cp', block)), events.index(('rm', block)))

    def test_copy_and_delete_lost_responses_are_resolved_from_bytes(self):
        for operation in ('cp', 'rm'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
                lab, store, probe, journal, objects, block, events = self.fixture(d)
                command = probe.client.command.side_effect
                lost = False
                def fail_after(action, *args, **kwargs):
                    nonlocal lost
                    value = command(action, *args, **kwargs)
                    if action == operation and not lost:
                        lost = True
                        raise TimeoutError('response lost')
                    return value
                probe.client.command.side_effect = fail_after
                with self.assertRaises(TimeoutError): probe.activate(journal, 'point')
                self.assertTrue(probe.record.exists())
                probe.cleanup()
                self.assertEqual({block: journal['backups']['point']['object_receipt']['objects'][block]}, objects)
                self.assertFalse(probe.record.exists())

    def test_copy_failure_before_escrow_leaves_original_and_can_release_intent(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            lab, store, probe, journal, objects, block, events = self.fixture(d)
            command = probe.client.command.side_effect
            probe.client.command.side_effect = lambda action, *args, **kw: (
                (_ for _ in ()).throw(TimeoutError()) if action == 'cp' else command(action, *args, **kw))
            with self.assertRaises(TimeoutError): probe.activate(journal, 'point')
            probe.cleanup()
            self.assertEqual([block], list(objects)); self.assertFalse(probe.record.exists())

    def test_unknown_stats_foreign_journal_or_changed_bytes_stop_without_destruction(self):
        for defect in ('stat', 'original', 'escrow', 'both-missing', 'run', 'container', 'path', 'nonce', 'symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
                lab, store, probe, journal, objects, block, events = self.fixture(d)
                row = probe.activate(journal, 'point')
                if defect == 'stat':
                    probe.client.command.side_effect = None
                    probe.client.command.return_value = SimpleNamespace(returncode=1, stdout='permission denied')
                if defect == 'original': objects[block] = {'size': 312, 'sha256': 'f'*64}
                if defect == 'escrow': objects[row['escrow']] = {'size': 312, 'sha256': 'f'*64}
                if defect == 'both-missing': objects.clear()
                if defect == 'run': row['run_id'] = 'other'
                if defect == 'container': store.inspect.return_value['Id'] = 'other'
                if defect == 'path': row['block'] = 'foreign/object'
                if defect == 'nonce': row['nonce'] = '../outside'
                probe.save(row)
                if defect == 'symlink':
                    p = Path(d)/'original.json'; probe.record.rename(p); probe.record.symlink_to(p)
                before = copy.deepcopy(objects)
                with self.assertRaises(ValueError): probe.cleanup()
                self.assertTrue(probe.record.exists()); self.assertEqual(before, objects)
                lab.remove.assert_not_called()

    def test_unconfirmed_create_repairs_source_but_retains_target_and_journal(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            lab, store, probe, journal, objects, block, events = self.fixture(d)
            row = probe.activate(journal, 'point')
            lab.get.return_value = {'metadata': {'uid': 'unknown'}}
            with self.assertRaisesRegex(ValueError, 'create outcome is unknown'): probe.cleanup()
            self.assertIn(block, objects); self.assertIn(row['escrow'], objects)
            self.assertTrue(probe.record.exists()); lab.remove.assert_not_called()

    def native_fixture(self, d, defect=''):
        lab, store, probe, journal, objects, block, events = self.fixture(d)
        probe.activate(journal, 'point')
        native = {}
        def create(*args, **options):
            obj = json.loads(options['stdin']); obj['metadata']['uid'] = 'restore-uid'
            obj['status'] = {'restoreRequired': defect != 'accepted', 'restoreInitiated': True}
            native.update(obj)
            return SimpleNamespace(stdout=json.dumps(obj))
        lab.kubectl.side_effect = create
        def get(kind, name='', namespace=''):
            if kind == 'backups.longhorn.io':
                return {'metadata': {'uid': 'other' if defect == 'backup' else 'backup-uid'}, 'status': {'url': 's3://owned/point'}}
            if kind == 'volumes.longhorn.io': return native
            if kind == 'engines.longhorn.io':
                return {'items': [{'metadata': {'uid': 'engine-uid', 'name': 'restore-engine'},
                    'spec': {'volumeName': native['metadata']['name'], 'requestedBackupRestore': 'other' if defect == 'request' else 'point'},
                    'status': {'lastRestoredBackup': 'point' if defect == 'last-restored' else '',
                        'restoreStatus': {'replica': {'error': ('foreign/key' if defect == 'key' else block)+
                            (' AccessDenied' if defect == 'error' else ' NoSuchKey')}}}}]}
            if kind in ('events', 'replicas.longhorn.io'):
                return {'items': []}
            return None
        lab.get.side_effect = get
        return probe, journal, lab

    def test_new_native_volume_waits_for_restore_initialization_before_completion_assertion(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            probe, journal, lab = self.native_fixture(d)
            wait = lab.wait.side_effect

            def transition(stage, timeout, check):
                native = lab.get('volumes.longhorn.io')
                initialized = native['status']
                for initial in ({}, {'restoreRequired': False, 'restoreInitiated': False}):
                    native['status'] = initial
                    self.assertIsNone(check(), 'uninitialized status is not a completed restore')
                native['status'] = initialized
                return wait(stage, timeout, check)

            lab.wait.side_effect = transition
            result = probe.observe(journal, 'point')
            self.assertTrue(result['restore_required'])
            self.assertEqual('restore-uid', result['volume_uid'])

    def event_fixture(self, directory, defect=''):
        probe, journal, lab = self.native_fixture(directory)
        get = lab.get.side_effect

        def observe(kind, name='', namespace=''):
            if kind == 'engines.longhorn.io':
                result = get(kind, name, namespace)
                result['items'][0]['status'] = {'restoreStatus': {}, 'currentState': 'stopped'}
                if defect == 'backup-request':
                    result['items'][0]['spec']['requestedBackupRestore'] = 'foreign-backup'
                return result
            if kind in ('events', 'replicas.longhorn.io'):
                volume = get('volumes.longhorn.io')
                ref = {'apiVersion': 'longhorn.io/v1beta2', 'kind': 'Volume', 'namespace': 'longhorn-system',
                    'name': volume['metadata']['name'], 'uid': 'restore-uid'}
                if kind == 'replicas.longhorn.io':
                    return {'items': [{'metadata': {'name': 'restore-replica', 'uid': 'replica-uid',
                        'ownerReferences': [dict(ref, uid='foreign' if defect == 'replica-owner' else 'restore-uid')]},
                        'spec': {'volumeName': ref['name'], 'dataEngine': 'v2',
                            'engineName': 'foreign' if defect == 'replica-engine' else 'restore-engine'}}]}
                if defect in ('uid', 'name', 'namespace', 'kind', 'apiVersion'):
                    ref[defect] = 'foreign'
                row = probe.read()
                error = ('foreign/key' if defect == 'key' else row['block'])
                error += ' AccessDenied' if defect == 'error' else ' NoSuchKey'
                event = {'metadata': {'uid': '' if defect == 'event-uid' else 'event-uid'},
                    'involvedObject': ref, 'reason': 'Foreign' if defect == 'reason' else 'FailedRestore',
                    'type': 'Normal' if defect == 'type' else 'Warning',
                    'source': {'component': 'foreign' if defect == 'source' else 'longhorn-volume-controller'},
                    'message': 'replica '+('foreign' if defect == 'replica-name' else 'restore-replica')+
                        ' failed the restore: '+error}
                return {'items': [event]}
            return get(kind, name, namespace)

        lab.get.side_effect = observe
        return probe, journal

    def test_native_failed_restore_event_survives_stopped_engine_status_reset(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            probe, journal = self.event_fixture(d)
            result = probe.observe(journal, 'point')
            self.assertEqual('native-event', result['error_origin'])
            self.assertEqual('event-uid', result['event_uid'])
            self.assertEqual('replica-uid', result['replica_uid'])
            self.assertEqual('engine-uid', result['engine_uid'])
            self.assertTrue(probe.record.exists())

    def test_foreign_or_unrelated_restore_events_never_substitute_for_native_refusal(self):
        for defect in ('uid', 'name', 'namespace', 'kind', 'apiVersion', 'reason', 'type', 'source',
                'event-uid', 'key', 'error', 'replica-name', 'replica-owner', 'replica-engine', 'backup-request'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
                probe, journal = self.event_fixture(d, defect)
                with self.assertRaises(TimeoutError):
                    probe.observe(journal, 'point')
                self.assertTrue(probe.record.exists())

    def test_refusal_requires_exact_backup_native_uid_block_and_native_missing_key(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            probe, journal, lab = self.native_fixture(d)
            result = probe.observe(journal, 'point')
            self.assertTrue(result['restore_required']); self.assertTrue(result['block_absent'])
            self.assertEqual('restore-uid', result['volume_uid']); self.assertEqual('backup-uid', result['backup_uid'])
            self.assertEqual(1000, lab.wait.call_args.args[1])
        for defect in ('accepted', 'backup', 'request', 'last-restored', 'key', 'error'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
                probe, journal, lab = self.native_fixture(d, defect)
                with self.assertRaises((ValueError, TimeoutError)): probe.observe(journal, 'point')
                self.assertTrue(probe.record.exists())

    def test_shell_blocks_store_cleanup_after_unknown_fault_and_recovery_reaches_journal_first(self):
        for fault in ('', 'business-rc', 'missing-marker', 'cleanup-rc', 'cleanup-only', 'baseline-drift', 'pending-fault'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                for name in ('.venv/bin', 'tests/helpers', 'kube-bin'): (root/name).mkdir(parents=True)
                driver = root/'.venv/bin/python'
                driver.write_text('''#!/usr/bin/env python3
import json,os,signal,sys,time
from pathlib import Path
if sys.argv[1]=='-':os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
root=Path(__file__).resolve().parents[2];mode=sys.argv[2];fault=os.environ.get('BROKEN_FAULT','')
with (root/'calls').open('a') as log:log.write(mode+'\\n')
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(0.01)
if mode=='baseline':
 calls=(root/'calls').read_text().splitlines()
 print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256='+('b'*64 if fault=='baseline-drift' and calls.count('baseline')>1 else 'a'*64))
if mode=='business':
 if fault=='business-rc':sys.exit(17)
 if fault!='missing-marker':print('LONGHORN_V2_BROKEN_RESTORE_BUSINESS_PASS')
 for marker in ('LONGHORN_V2_BROKEN_RESTORE_FAULT_CLEAN_PASS','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','LONGHORN_BACKUP_STORE_CLEAN_PASS'):print(marker)
 print('LONGHORN_V2_RESTORE_BLOCK_ISOLATED '+json.dumps({'block':'owned/block','backup':'point'}))
 print('LONGHORN_V2_BROKEN_CHAIN_NATIVE_REFUSED '+json.dumps({'block':'owned/block','backup':'point','volume_uid':'v','engine_uid':'e','backup_uid':'b','restore_required':True,'block_absent':True,'last_restored_backup_matches':False,'error_sha256':'c'*64}))
 print('LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=full backup=first')
 print('LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=incremental backup=point')
 print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=99')
if mode=='clean' and fault=='cleanup-rc':sys.exit(19)
'''); driver.chmod(0o755)
                kubectl = root/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                script = (ROOT/'tests/helpers/longhorn-v2-broken-restore.sh').read_text().replace('BASE=/usr/local/kubeauto', 'BASE='+d)
                path = root/'run.sh'; path.write_text(script)
                if fault == 'pending-fault':
                    journal = root/'clusters/longhorn-v2-gate/longhorn-v2-broken-restore.json'
                    journal.parent.mkdir(parents=True); journal.write_text('{}')
                mode = 'v2-broken-restore-cleanup' if fault == 'cleanup-only' else 'v2-broken-restore-qualification'
                result = subprocess.run(['bash', str(path), mode], text=True, capture_output=True, timeout=10,
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', BROKEN_FAULT=fault))
                calls = (root/'calls').read_text().splitlines()
                if fault == 'pending-fault':
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(['verify-transport'], calls)
                    self.assertTrue(journal.exists())
                    continue
                self.assertIn('clean', calls); self.assertIn('verify-core', calls)
                if fault == 'cleanup-rc':
                    self.assertNotIn('cleanup', calls); self.assertEqual(19, result.returncode)
                else:
                    self.assertLess(calls.index('clean'), calls.index('cleanup'))
                if fault == 'cleanup-only':
                    self.assertNotIn('business', calls); self.assertLess(calls.index('clean'), calls.index('baseline'))
                if fault in ('', 'cleanup-only'):
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('LONGHORN_'+mode.upper().replace('-', '_')+'_PASS', result.stdout)
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertNotIn('LONGHORN_V2_BROKEN_RESTORE_QUALIFICATION_PASS', result.stdout)
                if fault == 'business-rc': self.assertEqual(17, result.returncode)

    def test_fixed_dispatch_static_and_separate_recovery_markers(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        for mode in ('v2-broken-restore-qualification', 'v2-broken-restore-cleanup'):
            self.assertGreaterEqual(runner.count(mode), 4); self.assertIn(mode, live)
        self.assertIn('longhorn-v2-broken-restore.sh', live)
        self.assertIn('tests.unit.test_longhorn_v2_broken_restore', (ROOT/'tests/helpers/longhorn-static.sh').read_text())


if __name__ == '__main__':
    unittest.main()
