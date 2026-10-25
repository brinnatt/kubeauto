"""Pinned native ownership, asynchronous convergence and recurring cleanup."""
import copy
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_recurring', ROOT / 'tests/helpers/longhorn-recurring.py')
recurring = importlib.util.module_from_spec(spec); spec.loader.exec_module(recurring)
spec = importlib.util.spec_from_file_location('longhorn_recurring_backup', ROOT / 'tests/helpers/longhorn-recurring-backup.py')
backup = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup)


def obj(name, uid, kind=None, owner=None):
    value = {'metadata': {'name': name, 'uid': uid}, 'spec': {}}
    if kind:
        ref = {'kind': kind, 'uid': owner, 'apiVersion': 'longhorn.io/v1beta2' if kind == 'RecurringJob' else 'batch/v1'}
        if kind != 'RecurringJob': ref['controller'] = True
        value['metadata']['ownerReferences'] = [ref]
    return value


class RecurringContracts(unittest.TestCase):
    def test_selector_qualification_uses_product_annual_window_and_always_stops(self):
        for unexpected in (None, 'jobs', 'pods', 'snapshots', 'backups'):
            with self.subTest(unexpected=unexpected):
                order = []
                @contextmanager
                def scoped(cleanup):
                    try: yield
                    finally: cleanup()
                observation = {key: [] for key in ('jobs','pods','snapshots','backups')}
                observation['row'] = {'uid':'original-schedule'}
                if unexpected: observation[unexpected] = [{'metadata':{'uid':'unexpected'}}]
                lab = SimpleNamespace(DIRECTORY=Path('/tmp'), scoped_cleanup=scoped)
                schedule = recurring.Schedule(lab)
                schedule.begin = Mock(side_effect=lambda *_args, **_kwargs: order.append('begin'))
                schedule.select_source = Mock(side_effect=lambda: order.append('select'))
                schedule.sample = Mock(return_value=observation)
                schedule.cleanup = Mock(side_effect=lambda _pid: order.append('stop'))
                journal = {'volume_uid':'original-source'}
                if unexpected:
                    with self.assertRaisesRegex(ValueError,'unexpectedly scheduled'): schedule.verify_selector(journal, 123)
                else: schedule.verify_selector(journal, 123)
                schedule.begin.assert_called_once_with(journal, 123, cron='0 0 1 1 *')
                self.assertEqual(order, ['begin','select','stop'])

    def fixture(self, directory):
        source = obj('volume', 'source-uid')
        declaration = obj('snapshot-owned', 'schedule-uid')
        declaration['spec'] = {'cron': '* * * * *', 'task': 'snapshot', 'groups': ['group-owned'],
                               'retain': 2, 'concurrency': 1, 'parameters': {}}
        cron = obj('snapshot-owned', 'cron-uid', 'RecurringJob', 'schedule-uid')
        cron['spec'] = {'schedule': '* * * * *', 'concurrencyPolicy': 'Forbid', 'jobTemplate': {'spec': {
            'backoffLimit': 3, 'template': {'spec': {'restartPolicy': 'OnFailure'}}}}}
        job = obj('owned-job', 'job-uid', 'CronJob', 'cron-uid'); job['status'] = {'succeeded': 1}
        pod = obj('owned-pod', 'pod-uid', 'Job', 'job-uid')
        snapshot = obj('owned-snapshot', 'snapshot-uid')
        snapshot.update(spec={'volume': 'volume', 'labels': {'RecurringJob': 'snapshot-owned'}}, status={'readyToUse': True})
        state = {'run_id': 'run', 'cluster_uid': 'cluster'}
        values = {'volumes.longhorn.io': source, 'recurringjobs.longhorn.io': declaration, 'cronjob': cron,
                  'jobs': [job], 'pods': [pod], 'snapshots.longhorn.io': [snapshot]}
        def get(kind, name=None, namespace=None):
            value = values.get(kind)
            if isinstance(value, list) and name:
                return copy.deepcopy(next((obj for obj in value if obj['metadata']['name'] == name), None))
            return {'items': copy.deepcopy(value)} if isinstance(value, list) else copy.deepcopy(value)
        def save(path, value): path.write_text(json.dumps(value)); path.chmod(0o600)
        lab = SimpleNamespace(DIRECTORY=Path(directory), owner=Mock(side_effect=lambda: state), get=Mock(side_effect=get),
                              write_private=save, recurring_identity=Mock(side_effect=lambda:
                                  {declaration['metadata']['name']: {'uid': declaration['metadata']['uid'],
                                    'spec': declaration['spec'], 'labels': {}}} if values['recurringjobs.longhorn.io'] else {}),
                              run_requests=Mock(), restore_request_input=Mock(), remove=Mock())
        def wait(_label, _timeout, check):
            result = check()
            if not result: raise TimeoutError('fixture condition did not converge')
            return result
        lab.wait = Mock(side_effect=wait)
        schedule = recurring.Schedule(lab)
        schedule.save({'run_id': 'run', 'cluster_uid': 'cluster', 'volume': 'volume', 'volume_uid': 'source-uid',
                       'name': 'snapshot-owned', 'group': 'group-owned', 'uid': 'schedule-uid',
                       'spec': declaration['spec'], 'baseline': {}, 'jobs': {}, 'pods': {}, 'completed': [],
                       'snapshots': {}, 'phase': 'running'})
        save(Path(directory) / 'longhorn-install-business.json', {'volume': 'volume', 'volume_uid': 'source-uid'})
        return schedule, lab, values, state

    def test_longhorn_owner_reference_does_not_require_kubernetes_controller_flag(self):
        cron = obj('cron', 'cron-uid', 'RecurringJob', 'schedule-uid')
        self.assertTrue(recurring.controlled(cron, 'RecurringJob', 'schedule-uid'))
        self.assertFalse(recurring.controlled(cron, 'RecurringJob', 'another-uid'))
        cron['metadata']['ownerReferences'][0]['apiVersion'] = 'foreign/v1'
        self.assertFalse(recurring.controlled(cron, 'RecurringJob', 'schedule-uid'))
        job = obj('job', 'job-uid', 'CronJob', 'cron-uid')
        self.assertTrue(recurring.controlled(job, 'CronJob', 'cron-uid'))
        job['metadata']['ownerReferences'][0].pop('controller')
        self.assertFalse(recurring.controlled(job, 'CronJob', 'cron-uid'))

    def test_native_chain_tracks_completed_uids_and_survives_history_gc(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, _, values, _ = self.fixture(directory)
            first = schedule.sample()['row']
            self.assertEqual(first['completed'], ['job-uid'])
            self.assertEqual(first['pods'], {'owned-pod': 'pod-uid'})
            values['jobs'] = []; values['pods'] = []; values['snapshots.longhorn.io'] = []
            second = schedule.sample()['row']
            self.assertEqual(second['completed'], first['completed'])
            self.assertEqual(second['snapshots'], {'owned-snapshot': 'snapshot-uid'})

    def test_job_pod_and_snapshot_replacement_or_owner_loss_refuses(self):
        for kind in ('jobs', 'pods', 'snapshots.longhorn.io'):
            for mutation in ('uid', 'owner'):
                with self.subTest(kind=kind, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    schedule, _, values, _ = self.fixture(directory)
                    schedule.sample(); value = values[kind][0]
                    if mutation == 'uid': value['metadata']['uid'] = 'foreign'
                    elif kind == 'snapshots.longhorn.io': value['spec']['labels'] = {}
                    else: value['metadata']['ownerReferences'] = []
                    with self.assertRaisesRegex(ValueError, 'identity or owner changed'): schedule.sample()

    def test_native_retry_concurrency_and_selection_contracts_are_not_relaxed(self):
        for defect in ('concurrency', 'backoff', 'restart', 'wrong-volume', 'failed-job'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                schedule, _, values, _ = self.fixture(directory)
                cron = values['cronjob']['spec']
                if defect == 'concurrency': cron['concurrencyPolicy'] = 'Allow'
                elif defect == 'backoff': cron['jobTemplate']['spec']['backoffLimit'] = 0
                elif defect == 'restart': cron['jobTemplate']['spec']['template']['spec']['restartPolicy'] = 'Never'
                elif defect == 'wrong-volume': values['snapshots.longhorn.io'][0]['spec']['volume'] = 'unselected'
                else: values['jobs'][0]['status'] = {'conditions': [{'type': 'Failed', 'status': 'True'}]}
                with self.assertRaises(ValueError): schedule.sample()

    def test_foreign_children_are_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, _, values, _ = self.fixture(directory)
            foreign = obj('foreign', 'foreign-uid', 'CronJob', 'foreign-cron')
            values['jobs'].append(foreign)
            row = schedule.sample()['row']
            self.assertEqual(row['jobs'], {'owned-job': 'job-uid'})

    def test_cron_controller_lag_is_waited_without_losing_sample_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.fixture(directory)
            def apply(*_args, recurring_jobs):
                values['recurringjobs.longhorn.io']['spec']['cron'] = recurring_jobs[0]['cron']
            lab.run_requests.side_effect = apply
            def wait(_label, _timeout, check):
                self.assertIsNone(check())
                values['cronjob']['spec']['schedule'] = '0 0 1 1 *'
                self.assertTrue(check())
            lab.wait.side_effect = wait
            schedule.cadence(123, '0 0 1 1 *')
            row = schedule.read()
            self.assertNotIn('previous_cron', row)
            self.assertEqual(row['completed'], ['job-uid'])
            self.assertEqual(row['spec']['cron'], '0 0 1 1 *')

    def test_cleanup_stops_through_product_and_hands_observed_uids_to_source_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.fixture(directory)
            order = []
            def stop(*_args, recurring_jobs):
                self.assertEqual(recurring_jobs, []); order.append('product')
                values['recurringjobs.longhorn.io'] = None; values['cronjob'] = None
                values['jobs'] = []; values['pods'] = []
            lab.run_requests.side_effect = stop
            schedule.cleanup(123)
            self.assertEqual(order, ['product'])
            lab.remove.assert_not_called()
            business = json.loads((Path(directory) / 'longhorn-install-business.json').read_text())
            self.assertEqual(business['native_snapshots'], {'owned-snapshot': {'uid': 'snapshot-uid'}})
            self.assertFalse(schedule.record.exists())

    def test_successful_jobs_cannot_replace_completed_backup_retention(self):
        row = {'completed': ['a', 'b', 'c'], 'backups': {'old': '1', 'current': '2', 'latest': '3'},
               'pods': {'pod': 'pod-uid'}}
        objects = [{'metadata': {'name': name}, 'status': {'state': 'Completed', 'progress': 100,
                    'backupCreatedAt': '2026-10-19T01:00:00Z'}} for name in ('current', 'latest')]
        observation = {'row': row, 'backups': objects}
        self.assertIs(backup.retained(observation), observation)
        for defect in ('absent', 'only-two-observed', 'pending', 'progress', 'error', 'no-point', 'deleting', 'no-pod'):
            changed = copy.deepcopy(observation)
            if defect == 'absent': changed['backups'] = []
            elif defect == 'only-two-observed': changed['row']['backups'].pop('old')
            elif defect == 'pending': changed['backups'][0]['status']['state'] = 'InProgress'
            elif defect == 'progress': changed['backups'][0]['status']['progress'] = 99
            elif defect == 'error': changed['backups'][0]['status']['error'] = 'native error'
            elif defect == 'no-point': changed['backups'][0]['status'].pop('backupCreatedAt')
            elif defect == 'deleting': changed['backups'][0]['metadata']['deletionTimestamp'] = 'now'
            else: changed['row']['pods'] = {}
            with self.subTest(defect=defect):
                if defect == 'no-pod':
                    with self.assertRaises(ValueError): backup.retained(changed)
                else: self.assertIsNone(backup.retained(changed))

    def test_cleanup_reuses_proven_stopped_boundary_without_repeating_product(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.fixture(directory)
            schedule.sample()
            values.update({'recurringjobs.longhorn.io':None, 'cronjob':None, 'jobs':[], 'pods':[]})
            schedule.cleanup(123)
            lab.run_requests.assert_not_called()
            self.assertFalse(schedule.record.exists())

    def test_explicit_recovery_stops_active_schedule_before_inspecting_orphan_selector(self):
        for active in (False, True):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.fixture(directory)
                schedule.sample(); calls = []
                def stop(*_args, **_kwargs):
                    calls.append('product-stop')
                    values.update({'recurringjobs.longhorn.io': None, 'cronjob': None, 'jobs': [], 'pods': []})
                lab.run_requests.side_effect = stop
                if not active: stop(); calls.clear()
                original = schedule.recover_selector
                def recover():
                    self.assertIsNone(values['recurringjobs.longhorn.io'])
                    self.assertIsNone(values['cronjob'])
                    self.assertEqual(values['jobs'], []); self.assertEqual(values['pods'], [])
                    calls.append('orphan-inspection')
                    original()
                schedule.recover_selector = Mock(side_effect=recover)
                schedule.cleanup(123, recover_selector=True)
                self.assertEqual(calls, ['product-stop', 'orphan-inspection'] if active else ['orphan-inspection'])
                self.assertFalse(schedule.record.exists())
                lab.remove.assert_not_called()

    def test_scheduled_backup_checks_backend_before_independent_restore_and_keeps_source_client(self):
        for invalid_metadata in (False, True):
            with self.subTest(invalid_metadata=invalid_metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); calls = []; phase = {'resumed': False}; payload = 'confirmed'
                digest = hashlib.sha256(payload.encode() * 262144).hexdigest()
                journal = {'namespace': 'business', 'volume': 'source', 'volume_uid': 'source-uid',
                           'storage_class': 'longhorn', 'objects': {'Pod/writer': 'writer-uid'}}
                record = root / 'longhorn-install-business.json'; record.write_text(json.dumps(journal))
                objects = [dict(metadata={'name': name, 'uid': name + '-uid'},
                                status={'state': 'Completed', 'progress': 100,
                                        'backupCreatedAt': '2026-10-19T01:00:0' + str(index) + 'Z'})
                           for index, name in enumerate(('current', 'latest'))]
                def sample():
                    return {'row': {'completed': ['1','2','3'] + (['4','5'] if phase['resumed'] else []),
                                    'backups': {'old':'old-uid','current':'current-uid','latest':'latest-uid'},
                                    'pods': {'job-pod': 'job-pod-uid'}},
                            'backups': objects, 'snapshots': [obj('snapshot', 'snapshot-uid')],
                            'jobs': [{'status': {'succeeded': 1}}]}
                def cleanup(_relay):
                    calls.append('schedule-stopped')
                    value = json.loads(record.read_text())
                    value['backups'] = {name: {'uid': name + '-uid', 'recurring_job': 'job', 'spec': {}}
                                        for name in ('old','current','latest')}
                    record.write_text(json.dumps(value))
                schedule = SimpleNamespace(begin=Mock(), select_source=Mock(), sample=sample,
                    cadence=Mock(side_effect=lambda _relay, cron: phase.update(resumed=cron == '* * * * *')),
                    cleanup=cleanup)
                @contextmanager
                def scoped(clean):
                    try: yield
                    finally: clean()
                pods = {'writer': dict(metadata={'uid':'writer-uid'})}
                def create(value):
                    if value['kind'] == 'Pod':
                        pods[value['metadata']['name']] = dict(metadata={'uid':value['metadata']['name']+'-uid'},
                            status={'conditions':[{'type':'Ready','status':'True'}]})
                    return value
                def get(kind, name, _namespace=None):
                    if kind == 'pvc': return {'status':{'phase':'Bound'}}
                    if kind == 'pod': return pods.get(name)
                    if kind == 'volumes.longhorn.io': return {'status':{'state':'detached'}}
                    raise AssertionError(kind)
                def kubectl(*argv, **_kwargs):
                    text = argv[-1].removeprefix('read:').removeprefix('write:')
                    return SimpleNamespace(stdout=hashlib.sha256(text.encode()*262144).hexdigest())
                def write_private(path, value): path.write_text(json.dumps(value))
                def client(namespace, name, claim, node):
                    return {'kind':'Pod','metadata':{'namespace':namespace,'name':name}}
                listing = '\n'.join(json.dumps({'status':'success','type':'file','key':'backup_'+name+'.cfg'})
                                    for name in (('latest',) if invalid_metadata else ('current','latest')))
                store = SimpleNamespace(backup_receipt=Mock(side_effect=lambda volume, name, size:
                    {'volume':volume,'backup':name,'size':size,'objects':{'verified-block':{'sha256':'a'*64}}}),
                    receipt=Mock(return_value={'bucket':'bucket'}), mc=Mock(return_value=listing))
                lab = SimpleNamespace(DIRECTORY=root, WORKERS=['192.168.122.212','192.168.122.165'],
                    scoped_cleanup=scoped, backup_store=Mock(return_value=store), get=get,
                    client_pod=client, payload_program=lambda text, read_only=False:
                        ('read:' if read_only else 'write:')+text,
                    write_private=write_private, bind_backups=Mock(), kubectl=kubectl,
                    remove=lambda _obj: pods.pop('writer'),
                    wait=lambda _stage, _timeout, check: check(),
                    load_helper=Mock(side_effect=lambda name:
                        SimpleNamespace(Schedule=lambda _lab: schedule) if name=='longhorn-recurring.py'
                        else SimpleNamespace(volume_prefix=lambda _volume:'owned-prefix/')),
                    verify_backup_restore=Mock(side_effect=lambda *_args, **_kwargs: calls.append('restore')))
                if invalid_metadata:
                    with self.assertRaisesRegex(ValueError,'backend retention'):
                        backup.verify(lab,journal,create,123,payload,digest,{'target':'fixture'})
                    lab.verify_backup_restore.assert_not_called()
                    self.assertEqual(calls, ['schedule-stopped'])
                else:
                    actual_payload, actual_digest, client_name = backup.verify(
                        lab,journal,create,123,payload,digest,{'target':'fixture'})
                    self.assertEqual(calls,['schedule-stopped','restore'])
                    self.assertEqual(client_name,'recurring-source-reader')
                    self.assertEqual(actual_digest,hashlib.sha256(actual_payload.encode()*262144).hexdigest())
                    self.assertEqual(lab.verify_backup_restore.call_args.kwargs,{'source_client':client_name})
                    self.assertEqual(lab.verify_backup_restore.call_args.args[3],'latest')
    def test_cleanup_refuses_unconfirmed_or_replaced_creation_and_preserves_journal(self):
        for defect in ('unconfirmed', 'replaced', 'source', 'cluster'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, state = self.fixture(directory)
                if defect == 'unconfirmed':
                    row = schedule.read(); row.pop('uid'); schedule.save(row)
                elif defect == 'replaced': values['recurringjobs.longhorn.io']['metadata']['uid'] = 'foreign'
                elif defect == 'source': values['volumes.longhorn.io']['metadata']['uid'] = 'foreign'
                else: state['cluster_uid'] = 'foreign'
                with self.assertRaises(ValueError): schedule.cleanup(123)
                lab.run_requests.assert_not_called(); lab.remove.assert_not_called()
                self.assertTrue(schedule.record.exists())

    def test_orphan_selector_recovery_requires_absent_schedule_children_and_exact_source(self):
        for fault in ('none', 'absent-label', 'declaration', 'cron', 'job', 'pod', 'source', 'label', 'foreign'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.fixture(directory)
                schedule.sample()
                source = values['volumes.longhorn.io']
                key = 'recurring-job-group.longhorn.io/group-owned'
                source['metadata'].update(resourceVersion='7', labels={key: 'enabled', 'foreign': 'keep'})
                for resource in ('recurringjobs.longhorn.io', 'cronjob', 'jobs', 'pods'):
                    if fault == {'recurringjobs.longhorn.io':'declaration','cronjob':'cron','jobs':'job','pods':'pod'}[resource]:
                        continue
                    values[resource] = [] if isinstance(values[resource], list) else None
                if fault == 'absent-label': source['metadata']['labels'].pop(key)
                elif fault == 'source': source['metadata']['uid'] = 'replacement'
                elif fault == 'label': source['metadata']['labels'][key] = 'changed'
                elif fault == 'foreign': lab.recurring_identity.side_effect = lambda: {'foreign': {'spec': {'groups': ['group-owned']}}}
                def patch_selector(*args, stdin):
                    patch = json.loads(stdin)
                    self.assertEqual(patch[:2], [
                        {'op':'test','path':'/metadata/uid','value':'source-uid'},
                        {'op':'test','path':'/metadata/resourceVersion','value':'7'}])
                    self.assertEqual(patch[2], {'op':'test','path':'/metadata/labels/recurring-job-group.longhorn.io~1group-owned','value':'enabled'})
                    self.assertEqual(patch[3], {'op':'remove','path':patch[2]['path']})
                    source['metadata']['labels'].pop(key)
                lab.kubectl = Mock(side_effect=patch_selector)
                if fault in ('none', 'absent-label'):
                    schedule.recover_selector()
                    self.assertNotIn(key, source['metadata']['labels'])
                    self.assertEqual(source['metadata']['labels']['foreign'], 'keep')
                    self.assertEqual(lab.kubectl.call_count, int(fault == 'none'))
                else:
                    with self.assertRaises(ValueError): schedule.recover_selector()
                    lab.kubectl.assert_not_called()
                lab.run_requests.assert_not_called()
                lab.remove.assert_not_called()
                self.assertTrue(schedule.record.exists())

    def backup_fixture(self, directory):
        schedule, lab, values, state = self.fixture(directory)
        row = schedule.read(); row.update(task='backup-force-create', backups={}, backup_specs={},
                                        backup_configuration={'longhorn_backup_target': 's3://fixture@us-east-1/',
                                                              'longhorn_backup_secret': 'fixture-secret'})
        values['recurringjobs.longhorn.io']['spec']['task'] = row['task']
        row['spec']['task'] = row['task']; schedule.save(row)
        backup = obj('backup-owned', 'backup-uid')
        backup['metadata']['labels'] = {'backup-volume': 'volume', 'backup-target': 'default'}
        backup.update(spec={'snapshotName': 'owned-snapshot', 'labels': {'RecurringJob': 'snapshot-owned'},
                            'backupMode': 'incremental', 'syncRequestedAt': 'first'},
                      status={'state': 'Completed', 'progress': 100, 'backupCreatedAt': '2026-10-19T01:00:00Z'})
        values['backups.longhorn.io'] = [backup]
        return schedule, lab, values, state

    def test_backup_declaration_and_cadence_keep_the_original_product_target(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.backup_fixture(directory)
            row = schedule.read()
            declaration = schedule.declaration(row, '0 0 1 1 *')
            self.assertEqual(declaration['task'], 'backup-force-create')
            schedule.request(row, 123, [declaration])
            lab.run_requests.assert_called_once_with(123, [], recurring_jobs=[declaration],
                                                      backup_configuration=row['backup_configuration'])

    def test_backup_observation_rejects_other_sources_modes_and_native_failures(self):
        for defect in ('source', 'target', 'snapshot', 'error', 'unknown', 'mode', 'uid'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.backup_fixture(directory)
                schedule.sample()
                backup = values['backups.longhorn.io'][0]
                if defect == 'source': backup['metadata']['labels']['backup-volume'] = 'another-volume'
                elif defect == 'target': backup['metadata']['labels']['backup-target'] = 'another-target'
                elif defect == 'snapshot': backup['spec']['snapshotName'] = 'unobserved'
                elif defect == 'error': backup['status']['state'] = 'Error'
                elif defect == 'unknown': backup['status']['state'] = 'Unknown'
                elif defect == 'mode': backup['spec']['backupMode'] = 'full'
                else: backup['metadata']['uid'] = 'replacement'
                with self.assertRaises(ValueError): schedule.sample()
                self.assertTrue(schedule.record.exists())
                lab.remove.assert_not_called()

    def test_backup_poll_timestamp_can_change_and_gc_does_not_grant_replacement_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, _, values, _ = self.backup_fixture(directory)
            schedule.sample()
            original = copy.deepcopy(values['backups.longhorn.io'][0])
            values['backups.longhorn.io'][0]['spec']['syncRequestedAt'] = 'second'
            schedule.sample()
            values['backups.longhorn.io'] = []; schedule.sample()
            row = schedule.read()
            self.assertEqual(row['backups'], {'backup-owned': 'backup-uid'})
            original['metadata']['uid'] = 'recreated'
            values['backups.longhorn.io'] = [original]
            with self.assertRaises(ValueError): schedule.sample()

    def test_backup_collected_snapshot_uses_backend_provenance_without_inventing_snapshot_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.backup_fixture(directory)
            values['snapshots.longhorn.io'] = []
            values['backups.longhorn.io'][0]['status']['snapshotName'] = 'owned-snapshot'
            origin = Mock(return_value='a' * 64)
            lab.backup_store = Mock(return_value=SimpleNamespace(backup_origin=origin))
            row = schedule.sample()['row']
            self.assertEqual(row['snapshots'], {})
            self.assertEqual(row['backup_origins']['backup-owned'],
                             {'uid': 'backup-uid', 'snapshot': 'owned-snapshot', 'sha256': 'a' * 64})
            origin.assert_called_once_with('volume', 'backup-owned', 'owned-snapshot', 'snapshot-owned')
            schedule.sample()
            origin.assert_called_once()
            values['backups.longhorn.io'][0]['metadata']['uid'] = 'replacement'
            with self.assertRaises(ValueError): schedule.sample()
            values['backups.longhorn.io'][0]['metadata']['uid'] = 'backup-uid'
            def stop(*_args, **_kwargs):
                values['recurringjobs.longhorn.io'] = None; values['cronjob'] = None
                values['jobs'] = []; values['pods'] = []
            lab.run_requests.side_effect = stop
            # Recovery hands off the real Backup UID, never a fabricated
            # Snapshot UID for the object already collected by Longhorn.
            schedule.cleanup(123)
            business = json.loads((Path(directory) / 'longhorn-install-business.json').read_text())
            self.assertEqual(business.get('native_snapshots', {}), {})
            self.assertEqual(business['backups']['backup-owned']['uid'], 'backup-uid')

    def test_backup_snapshot_created_between_list_and_get_records_its_actual_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.backup_fixture(directory)
            snapshot = copy.deepcopy(values['snapshots.longhorn.io'][0])
            values['snapshots.longhorn.io'] = []
            original = lab.get.side_effect
            lab.get.side_effect = lambda kind, name=None, namespace=None: (
                copy.deepcopy(snapshot) if kind == 'snapshots.longhorn.io' and name else original(kind, name, namespace))
            lab.backup_store = Mock()
            observation = schedule.sample()
            self.assertEqual(observation['row']['snapshots'], {'owned-snapshot': 'snapshot-uid'})
            self.assertEqual(observation['snapshots'], [snapshot])
            lab.backup_store.assert_not_called()

    def test_unobserved_backup_requires_completed_exact_status_and_backend_origin(self):
        for fault in ('pending', 'progress', 'error', 'created', 'snapshot', 'backend'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.backup_fixture(directory)
                values['snapshots.longhorn.io'] = []
                status = values['backups.longhorn.io'][0]['status']
                status['snapshotName'] = 'owned-snapshot'
                if fault == 'pending': status['state'] = 'InProgress'
                elif fault == 'progress': status['progress'] = 99
                elif fault == 'error': status['error'] = 'native failure'
                elif fault == 'created': status['backupCreatedAt'] = ''
                elif fault == 'snapshot': status['snapshotName'] = 'another-snapshot'
                origin = Mock(side_effect=ValueError('backend origin differs'))
                lab.backup_store = Mock(return_value=SimpleNamespace(backup_origin=origin))
                with self.assertRaises(ValueError): schedule.sample()
                if fault == 'backend': origin.assert_called_once()
                else: origin.assert_not_called()
                self.assertEqual(schedule.read()['snapshots'], {})

    def test_unobserved_backup_rejects_foreign_snapshot_from_exact_get(self):
        for fault in ('name', 'volume', 'job'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.backup_fixture(directory)
                snapshot = copy.deepcopy(values['snapshots.longhorn.io'][0])
                values['snapshots.longhorn.io'] = []
                if fault == 'name': snapshot['metadata']['name'] = 'another-snapshot'
                elif fault == 'volume': snapshot['spec']['volume'] = 'another-volume'
                else: snapshot['spec']['labels']['RecurringJob'] = 'another-job'
                original = lab.get.side_effect
                lab.get.side_effect = lambda kind, name=None, namespace=None: (
                    copy.deepcopy(snapshot) if kind == 'snapshots.longhorn.io' and name else original(kind, name, namespace))
                with self.assertRaises(ValueError): schedule.sample()

    def test_unobserved_retained_backup_waits_for_native_gc_without_becoming_a_restore_point(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.backup_fixture(directory)
            values['snapshots.longhorn.io'] = []
            retiring = values['backups.longhorn.io'][0]
            retiring['metadata']['deletionTimestamp'] = '2026-10-19T01:02:00Z'
            retiring['status'].update(state='Deleting', snapshotName='owned-snapshot')
            lab.backup_store = Mock()
            def wait(label, timeout, check):
                self.assertEqual((label, timeout), ('recurring-unobserved-backup-gc', 600))
                self.assertFalse(check())
                values['backups.longhorn.io'] = []
                self.assertTrue(check())
            lab.wait.side_effect = wait
            observation = schedule.sample()
            self.assertEqual(observation['backups'], [])
            self.assertEqual(observation['row']['backups'], {'backup-owned': 'backup-uid'})
            self.assertEqual(observation['row']['backup_specs']['backup-owned']['snapshotName'], 'owned-snapshot')
            self.assertFalse(observation['row'].get('backup_origins'))
            self.assertEqual(observation['row']['snapshots'], {})
            lab.backup_store.assert_not_called()
            lab.remove.assert_not_called()

    def test_unobserved_backup_gc_refuses_changed_identity_or_incomplete_origin(self):
        for fault in ('no-deletion', 'progress', 'error', 'created', 'snapshot',
                      'uid', 'spec', 'labels', 'state', 'deletion-error', 'cancelled', 'timeout'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                schedule, lab, values, _ = self.backup_fixture(directory)
                values['snapshots.longhorn.io'] = []
                retiring = values['backups.longhorn.io'][0]
                retiring['metadata']['deletionTimestamp'] = '2026-10-19T01:02:00Z'
                retiring['status'].update(state='Deleting', snapshotName='owned-snapshot')
                if fault == 'no-deletion': retiring['metadata'].pop('deletionTimestamp')
                elif fault == 'progress': retiring['status']['progress'] = 99
                elif fault == 'error': retiring['status']['error'] = 'native failure'
                elif fault == 'created': retiring['status']['backupCreatedAt'] = ''
                elif fault == 'snapshot': retiring['status']['snapshotName'] = 'another-snapshot'
                def wait(_label, _timeout, check):
                    if fault == 'timeout': raise TimeoutError('native deletion did not converge')
                    if fault == 'uid': retiring['metadata']['uid'] = 'replacement'
                    elif fault == 'spec': retiring['spec']['snapshotName'] = 'replacement'
                    elif fault == 'labels': retiring['metadata']['labels']['backup-volume'] = 'foreign'
                    elif fault == 'state': retiring['status']['state'] = 'Error'
                    elif fault == 'deletion-error': retiring['status']['error'] = 'remote deletion failed'
                    elif fault == 'cancelled': retiring['metadata'].pop('deletionTimestamp')
                    check()
                lab.wait.side_effect = wait
                lab.backup_store = Mock()
                with self.assertRaises((ValueError, TimeoutError)): schedule.sample()
                lab.backup_store.assert_not_called()
                lab.remove.assert_not_called()

    def test_backup_cleanup_stops_schedule_before_exact_uid_and_spec_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule, lab, values, _ = self.backup_fixture(directory)
            schedule.sample()
            def stop(*_args, recurring_jobs, backup_configuration):
                self.assertEqual(recurring_jobs, [])
                self.assertEqual(backup_configuration['longhorn_backup_secret'], 'fixture-secret')
                values['recurringjobs.longhorn.io'] = None; values['cronjob'] = None
                values['jobs'] = []; values['pods'] = []
            lab.run_requests.side_effect = stop
            schedule.cleanup(123)
            business = json.loads((Path(directory) / 'longhorn-install-business.json').read_text())
            entry = business['backups']['backup-owned']
            self.assertEqual(entry['uid'], 'backup-uid')
            self.assertEqual(entry['recurring_job'], 'snapshot-owned')
            self.assertEqual(entry['spec']['snapshotName'], 'owned-snapshot')
            self.assertNotIn('syncRequestedAt', entry['spec'])
            lab.remove.assert_not_called()


if __name__ == '__main__': unittest.main()
