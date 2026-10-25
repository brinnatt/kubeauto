import copy
import importlib.util
import json
import hashlib
import socket
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('detached', ROOT/'tests/helpers/longhorn-v2-detached-backup.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def native_log(count=1, snapshot='snap-1'):
    return ('time="2026-10-20T10:00:00Z" level=info msg="Starting volume job" '
            f'executionCount={count} job=schedule snapshotName={snapshot} volumeName=source')


class DetachedBackupTests(unittest.TestCase):
    def test_fixed_branch_keeps_prerequisites_durable_cleanup_and_result_verification(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        shell = (ROOT/'tests/helpers/longhorn-v2-detached-backup.sh').read_text()
        self.assertIn('--longhorn-v2-detached-backup-qualification) longhorn_launch', runner)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-detached-backup.sh" "$MODE"', live)
        for command in ('"$PY" "$CORE" verify-transport', 'qualification-current',
                        '"$PY" "$DETACHED" baseline', '"$PY" "$DETACHED" cleanup',
                        '"$PY" "$DETACHED" business', '"$PY" "$DETACHED" verify --log',
                        'trap detached_exit EXIT', 'LONGHORN_FIRST_FAILURE', 'LONGHORN_CLEANUP_FAIL',
                        '"${marker}_SCOPED_CLEAN_PASS"', '"${marker}_PASS"'):
            self.assertIn(command, shell)
        self.assertLess(shell.index('"$PY" "$DETACHED" cleanup'), shell.index('"$PY" "$CORE" relay-clean'))
        self.assertLess(shell.index('"$PY" "$CORE" relay-clean'), shell.index('"${marker}_PASS"'))

    def test_fixed_recovery_cannot_run_business_or_claim_qualification_pass(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        shell = (ROOT/'tests/helpers/longhorn-v2-detached-backup.sh').read_text()
        self.assertIn('--longhorn-v2-detached-backup-recovery) longhorn_launch', runner)
        self.assertIn('v2-detached-backup-qualification|v2-detached-backup-recovery)', live)
        branch = shell.split('if [[ "${1:-}" == v2-detached-backup-recovery ]]; then', 1)[1].split('fi', 1)[0]
        for fragment in ('recovery-baseline', '"$DETACHED" recover', '"$DETACHED" baseline',
                         '"$CORE" verify-core', 'LONGHORN_V2_DETACHED_BACKUP_RECOVERY_SCOPED_CLEAN_PASS',
                         'LONGHORN_V2_DETACHED_BACKUP_RECOVERY_PASS', 'exit 0'):
            self.assertIn(fragment, branch)
        self.assertNotIn('"$DETACHED" business', branch)
        self.assertNotIn('QUALIFICATION_PASS', branch)

    def test_reads_exact_native_start_and_ignores_other_records(self):
        row = probe.pod_origin('unrelated\n' + native_log() + '\nFinished recurring backup',
                               schedule='schedule', volume='source')
        self.assertEqual({'execution_count': 1, 'snapshot': 'snap-1'}, row)

    def test_rejects_ambiguous_missing_or_foreign_native_records(self):
        for log in ('', native_log() + '\n' + native_log(), native_log(0),
                    native_log().replace('job=schedule', 'job=foreign'),
                    native_log().replace('volumeName=source', 'volumeName=foreign'),
                    native_log().replace(' executionCount=1', ''),
                    native_log() + ' executionCount=2'):
            with self.subTest(log=log), self.assertRaises(ValueError):
                probe.pod_origin(log, schedule='schedule', volume='source')

    def observation(self, count=2, mode='full'):
        return {'row': {'name': 'schedule', 'volume': 'source'},
                'jobs': [{'metadata': {'uid': 'job-uid'}, 'status': {'succeeded': 1}}],
                'pods': [{'metadata': {'name': 'pod', 'uid': 'pod-uid', 'ownerReferences': [
                    {'kind': 'Job', 'uid': 'job-uid', 'controller': True}]}, 'status': {'phase': 'Succeeded'}}],
                'backups': [{'metadata': {'name': 'backup', 'uid': 'backup-uid'},
                    'spec': {'snapshotName': 'snap-1', 'backupMode': mode},
                    'status': {'state': 'Completed', 'progress': 100}}]}

    def test_binds_request_mode_to_successful_pod_job_and_execution_count(self):
        for count, mode in ((1, 'incremental'), (2, 'full'), (3, 'incremental')):
            lab = SimpleNamespace(kubectl=Mock(return_value=SimpleNamespace(stdout=native_log(count))))
            result = probe.bind_modes(lab, self.observation(count, mode), {})
            self.assertEqual(count, result['backup']['execution_count'])
            self.assertEqual('job-uid', result['backup']['job_uid'])
            self.assertEqual('backup-uid', result['backup']['uid'])

    def test_backend_first_full_cannot_prove_periodic_parameter(self):
        lab = SimpleNamespace(kubectl=Mock(return_value=SimpleNamespace(stdout=native_log(1))))
        row = self.observation(mode='incremental'); row['backups'][0]['status']['incremental'] = False
        self.assertEqual('incremental', probe.bind_modes(lab, row, {})['backup']['mode'])
        row['backups'][0]['spec']['backupMode'] = 'full'
        with self.assertRaisesRegex(ValueError, 'executionCount'):
            probe.bind_modes(lab, row, {})

    def test_pending_unowned_or_wrong_snapshot_does_not_grant_mode_evidence(self):
        lab = SimpleNamespace(kubectl=Mock(return_value=SimpleNamespace(stdout=native_log(2))))
        row = self.observation(); row['pods'][0]['status']['phase'] = 'Running'
        self.assertEqual({}, probe.bind_modes(lab, row, {}))
        row = self.observation(); row['pods'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        with self.assertRaises(ValueError):
            probe.bind_modes(lab, row, {})
        row = self.observation(); row['backups'][0]['spec']['snapshotName'] = 'other'
        self.assertEqual({}, probe.bind_modes(lab, row, {}))

    def test_setting_intent_precedes_mutation_and_original_policy_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            current = {'metadata': {'uid': 'setting-uid', 'resourceVersion': '1'}, 'value': 'false'}
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster'},
                                  get=lambda *a, **k: copy.deepcopy(current))
            lab.write_private = lambda path, row: path.write_text(json.dumps(row))
            patches = []
            def kubectl(*args, stdin):
                self.assertTrue((lab.DIRECTORY/probe.RECORD).is_file())
                patch = json.loads(stdin); patches.append(patch)
                self.assertEqual('setting-uid', patch[0]['value'])
                self.assertEqual(current['metadata']['resourceVersion'], patch[1]['value'])
                self.assertEqual(current['value'], patch[2]['value'])
                current['value'] = patch[3]['value']
            lab.kubectl = kubectl
            setting = probe.Setting(lab); setting.enable(); self.assertEqual('true', current['value'])
            setting.restore(); self.assertEqual('false', current['value'])
            self.assertFalse(setting.path.exists()); self.assertEqual(2, len(patches))

    def test_unknown_setting_mutation_retains_journal_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            current = {'metadata': {'uid': 'setting-uid', 'resourceVersion': '1'}, 'value': 'false'}
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster'},
                                  get=lambda *a, **k: copy.deepcopy(current),
                                  write_private=lambda path, row: path.write_text(json.dumps(row)),
                                  kubectl=Mock(side_effect=TimeoutError('lost response')))
            setting = probe.Setting(lab)
            with self.assertRaises(TimeoutError):
                setting.enable()
            self.assertTrue(setting.path.exists())
            current['metadata']['uid'] = 'replacement'
            with self.assertRaisesRegex(ValueError, 'identity'):
                setting.restore()
            self.assertTrue(setting.path.exists())

    def test_cleanup_stops_schedules_before_restoring_setting_and_deleting_business(self):
        order = []
        lab = SimpleNamespace(load_helper=lambda name: SimpleNamespace(Schedule=lambda lab: SimpleNamespace(
            cleanup=lambda pid: order.append('schedule-stop'))))
        from unittest.mock import patch
        with patch.object(probe, 'Setting', return_value=SimpleNamespace(restore=lambda: order.append('setting-restore'))):
            probe.stop(lab, 42)
        self.assertEqual(['schedule-stop', 'setting-restore'], order)

    def test_outer_business_unwind_cannot_remove_store_with_unresolved_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = SimpleNamespace(DIRECTORY=Path(directory)); cleanup = Mock()
            store = probe.protected_store(lab, lambda: SimpleNamespace(cleanup=cleanup))
            path = lab.DIRECTORY/probe.RECORD
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Setting'):
                store.cleanup()
            cleanup.assert_not_called()
            path.unlink(); path.symlink_to('missing-journal')
            with self.assertRaises(ValueError):
                store.cleanup()
            cleanup.assert_not_called()
            path.unlink(); store.cleanup(after_uninstall=False)
            cleanup.assert_called_once_with(after_uninstall=False)

    def recovery_fixture(self, directory):
        base = Path(directory); (base/'tests/evidence').mkdir(parents=True)
        journal = {'namespace': 'longhorn-business-original', 'namespace_uid': 'original',
                   'objects': {'PersistentVolumeClaim/data': 'claim-uid'}}
        record = base/'longhorn-install-business.json'; record.write_text(json.dumps(journal))
        (base/'config.yml').write_text('{}')
        owner = {'run_id': 'run', 'cluster_uid': 'cluster', 'relays': []}
        binding = {'boot_id': 'reboot', 'old_pid': 42, 'owner': owner,
                   'business_sha256': hashlib.sha256(json.dumps(journal, sort_keys=True).encode()).hexdigest(),
                   'store_name': 'original-store', 'directory_identity': [1, 2],
                   'container_id': 'container', 'socket_identity': [3, 4]}
        (base/'tests/evidence/longhorn-v2-detached-backup-network-first-failure-20261020.json').write_text(
            json.dumps({'classification': 'environment/host-reboot', 'recovery_binding': binding}))
        boot = base/'boot'; boot.write_text('reboot'); proc = base/'no-process'
        store = SimpleNamespace(record=base/'store.json', directory=base/'store',
            transport_listener=Mock(return_value=None))
        lab = SimpleNamespace(DIRECTORY=base, owner=lambda: copy.deepcopy(owner), core_identity=Mock(),
            yaml=SimpleNamespace(safe_load=lambda _: {'longhorn_requests': [], 'longhorn_recurring_jobs': []}),
            backup_store=lambda: store, execute=Mock(return_value=SimpleNamespace(stdout='')))
        def get(kind, name=None, namespace=None):
            if kind == 'settings.longhorn.io':
                return {'value': 'false'}
            if kind == 'backuptargets.longhorn.io':
                return {'spec': {'backupTargetURL': '', 'credentialSecret': ''}}
            return None if name else {'items': []}
        lab.get = Mock(side_effect=get)
        def path(*args):
            if args == ('/proc/sys/kernel/random/boot_id',): return boot
            if args == ('/proc', '42'): return proc
            return Path(*args)
        return base, lab, store, record, path

    def test_post_namespace_deletion_recovery_requires_exact_archived_journal_and_empty_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            base, lab, store, record, path = self.recovery_fixture(directory)
            with patch.object(probe, 'BASE', base), patch.object(probe, 'Path', side_effect=path):
                self.assertEqual(record, probe.recovery_boundary(lab)[0])
                journal = json.loads(record.read_text()); journal['namespace_uid'] = 'replacement'
                record.write_text(json.dumps(journal))
                with self.assertRaisesRegex(ValueError, 'journal'):
                    probe.recovery_boundary(lab)
            self.assertTrue(record.exists())

    def test_recovery_rejects_recreated_namespace_storage_consumers_and_unrelated_journals(self):
        for kind in ('namespace', 'volumes.longhorn.io', 'replicas.longhorn.io', 'backups.longhorn.io',
                     'recurringjobs.longhorn.io', 'pv', 'volumesnapshotcontents.snapshot.storage.k8s.io'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                base, lab, store, record, path = self.recovery_fixture(directory)
                original = lab.get.side_effect
                obj = {'metadata': {'uid': 'foreign'}, 'spec': {'csi': {'driver': 'driver.longhorn.io'}}}
                lab.get.side_effect = lambda resource, *a, **k: (obj if resource == 'namespace' else {'items': [obj]}) if resource == kind else original(resource, *a, **k)
                with patch.object(probe, 'BASE', base), patch.object(probe, 'Path', side_effect=path), self.assertRaises(ValueError):
                    probe.recovery_boundary(lab)
                self.assertTrue(record.exists())
        with tempfile.TemporaryDirectory() as directory:
            base, lab, store, record, path = self.recovery_fixture(directory)
            (base/probe.RECORD).symlink_to('absent')
            with patch.object(probe, 'BASE', base), patch.object(probe, 'Path', side_effect=path), self.assertRaisesRegex(ValueError, 'journal'):
                probe.recovery_boundary(lab)

    def test_recovery_rejects_symlink_journal_new_boot_live_old_pid_or_changed_owner(self):
        for change in ('symlink', 'boot', 'pid', 'owner'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                base, lab, store, record, path = self.recovery_fixture(directory)
                if change == 'symlink':
                    record.rename(base/'original.json'); record.symlink_to('original.json')
                elif change == 'boot': (base/'boot').write_text('other-boot')
                elif change == 'pid': (base/'no-process').mkdir()
                else: lab.owner = lambda: {'run_id': 'replacement'}
                with patch.object(probe, 'BASE', base), patch.object(probe, 'Path', side_effect=path), self.assertRaises(ValueError):
                    probe.recovery_boundary(lab)
                self.assertTrue(record.exists())

    def test_recovery_rejects_replacement_store_or_restarted_container(self):
        for change in ('directory', 'container', 'running', 'socket', 'consumer'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                base, lab, store, record, path = self.recovery_fixture(directory)
                store.record.write_text('{}'); store.directory.mkdir()
                row = {'name': 'original-store', 'directory_identity': [1, 2]}
                store.receipt = lambda: row
                store.transport_receipt = lambda _: {'socket_identity': [3, 4] if change != 'socket' else [7, 8]}
                store.inspect = lambda _: {'Id': 'container' if change != 'container' else 'replacement',
                                           'State': {'Running': change == 'running'}}
                if change == 'directory': row['directory_identity'] = [5, 6]
                if change == 'consumer':
                    original = lab.get.side_effect
                    lab.get.side_effect = lambda kind, *a, **k: {'items': [{'metadata': {'name': 'foreign', 'labels': {
                        'kubeauto.io/longhorn-backup-run': 'other'}}}]} if kind == 'pods' else original(kind, *a, **k)
                with patch.object(probe, 'BASE', base), patch.object(probe, 'Path', side_effect=path), self.assertRaises(ValueError):
                    probe.recovery_boundary(lab)
                self.assertTrue(record.exists())

    def test_stale_original_socket_is_retired_but_active_master_and_replaced_receipt_are_refused(self):
        for active in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'transport.sock'
                server = socket.socket(socket.AF_UNIX); server.bind(str(path)); server.listen(1)
                if not active: server.close()
                store = SimpleNamespace(directory=Path(directory), receipt=lambda: {},
                    transport_receipt=lambda _: {'socket_identity': [path.stat().st_dev, path.stat().st_ino]})
                try:
                    if active:
                        with self.assertRaisesRegex(ValueError, 'active'): probe.retire_stale_socket(store)
                        self.assertTrue(path.exists())
                    else:
                        probe.retire_stale_socket(store); self.assertFalse(path.exists())
                finally:
                    server.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'transport.sock'; path.write_text('foreign')
            store = SimpleNamespace(directory=Path(directory), receipt=lambda: {},
                                    transport_receipt=Mock(side_effect=ValueError('identity changed')))
            with self.assertRaisesRegex(ValueError, 'identity'): probe.retire_stale_socket(store)
            self.assertEqual('foreign', path.read_text())

    def test_recovery_retains_exact_journal_on_target_credential_or_store_cleanup_refusal(self):
        for phase in ('restore_product', 'cleanup'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                record = Path(directory)/'business.json'; record.write_text('{}')
                store = SimpleNamespace(record=Path(directory)/'store.json', restore_product=Mock(), cleanup=Mock())
                store.record.write_text('{}'); getattr(store, phase).side_effect = ValueError('identity changed')
                binding = {'business_sha256': hashlib.sha256(json.dumps({}, sort_keys=True).encode()).hexdigest()}
                with patch.object(probe, 'recovery_boundary', return_value=(record, binding, store)), patch.object(probe, 'retire_stale_socket'), self.assertRaises(ValueError):
                    probe.recover(None)
                self.assertTrue(record.exists())

    def result_log(self):
        row = {'volume_uid': 'volume-uid', 'schedule_uid': 'schedule-uid', 'interval': 2,
               'attached_without_workload': True, 'returned_detached': True, 'restore_backup': 'backup-2',
               'snapshot_sha256': 'a'*64, 'source_sha256': 'b'*64, 'modes': {}, 'receipts': {}}
        for count, mode in ((1, 'incremental'), (2, 'full')):
            name = 'backup-' + str(count)
            row['modes'][name] = {'uid': name, 'mode': mode, 'execution_count': count,
                                  'pod_uid': 'pod-'+str(count), 'job_uid': 'job-'+str(count), 'snapshot': 'snap-'+str(count)}
            row['receipts'][name] = {'backup': name, 'size': 2*1024**3, 'volume': 'source',
                                     'objects': {'config': {'size': 64, 'sha256': 'c'*64}}}
        markers = ['LONGHORN_V2_DETACHED_SETTING_RESTORED', 'LONGHORN_RECURRING_BACKUP_SCHEDULES_STOPPED_PASS',
                   'LONGHORN_RECURRING_BACKUP_BUSINESS_PASS', 'LONGHORN_RECURRING_BACKUP_CLEAN_PASS',
                   'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS', 'LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED',
                   'LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS', 'LONGHORN_BACKUP_STORE_CLEAN_PASS',
                   'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact',
                   'LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode=detached backup_sha256='+'a'*64+
                   ' source_sha256='+'b'*64+' independent_volume=restored',
                   'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=123 sha256='+'b'*64+' replica_nodes=one,two,three']
        return row, markers

    def test_verify_requires_modes_restore_data_and_cleanup_together(self):
        row, markers = self.result_log()
        render = lambda: '\n'.join(markers + ['LONGHORN_V2_DETACHED_BACKUP_RESULT '+json.dumps(row)])
        self.assertEqual(row, probe.verify_log(render()))
        for field, value in (('returned_detached', False), ('attached_without_workload', False), ('interval', 3)):
            changed = copy.deepcopy(row); changed[field] = value
            with self.assertRaises(ValueError):
                probe.verify_log('\n'.join(markers + ['LONGHORN_V2_DETACHED_BACKUP_RESULT '+json.dumps(changed)]))
        for marker in markers:
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                probe.verify_log(render().replace(marker, ''))


if __name__ == '__main__':
    unittest.main()
