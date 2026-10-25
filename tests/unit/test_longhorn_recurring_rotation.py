"""Native selection and explicitly scoped scheduled credential failures."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from tests.unit import test_longhorn_recurring as existing

recurring = existing.recurring
spec=importlib.util.spec_from_file_location('credentials_current',Path(__file__).resolve().parents[2]/'tests/helpers/longhorn-recurring-rotation.py')
rotation=importlib.util.module_from_spec(spec);spec.loader.exec_module(rotation)


class AdmissionRefusalContracts(unittest.TestCase):
    def fixture(self):
        def owned(name, uid, kind, parent, api='batch/v1', controller=True):
            return {'name': name, 'uid': uid, 'ownerReferences': [
                {'kind': kind, 'uid': parent, 'apiVersion': api, 'controller': controller}]}
        secret = {'metadata': {'name': 'selected-secret', 'uid': 'secret-uid'}, 'data': {'key': 'encoded'}}
        entry = {'name': 'selected-secret', 'uid': 'secret-uid', 'digest': rotation.data_digest(secret['data'])}
        target = {'metadata': {'uid': 'target-uid'}, 'spec': {'credentialSecret': entry['name']},
                  'status': {'available': False, 'conditions': [
                      {'type': 'Unavailable', 'status': 'True', 'message': 'AWS Error: InvalidAccessKeyId invalid key'}]}}
        cron = {'metadata': owned('plan', 'cron-uid', 'RecurringJob', 'plan-uid', 'longhorn.io/v1beta2', False)}
        job = {'metadata': owned('new-job', 'job-uid', 'CronJob', 'cron-uid'), 'status': {'succeeded': 1}}
        pod = {'metadata': owned('new-pod', 'pod-uid', 'Job', 'job-uid'), 'status': {'phase': 'Succeeded'}}
        snapshot = {'metadata': {'name': 'new-snapshot', 'uid': 'snapshot-uid'},
                    'spec': {'volume': 'source', 'labels': {'RecurringJob': 'plan'}}, 'status': {'readyToUse': True}}
        row = {'name': 'plan', 'uid': 'plan-uid', 'volume': 'source', 'cron_uid': 'cron-uid',
               'jobs': {'new-job': 'job-uid'}, 'pods': {'new-pod': 'pod-uid'},
               'snapshots': {'new-snapshot': 'snapshot-uid'}}
        observation = {'row': row, 'cron': cron, 'jobs': [job], 'pods': [pod], 'snapshots': [snapshot], 'backups': []}
        log = ('time=t level=error msg="failed to complete backupAndCleanup: failed to backup snapshot: '
               'admission webhook \\"validator.longhorn.io\\" denied the request: backup target default is not available" '
               'job=plan volumeName=source snapshotName=new-snapshot\n')
        objects = {'backuptargets.longhorn.io': target, 'secret': secret, 'pod': copy.deepcopy(pod),
                   'backups.longhorn.io': {'items': []}}
        lab = Mock()
        lab.get.side_effect = lambda kind, *_args, **_kwargs: copy.deepcopy(objects[kind])
        lab.kubectl.return_value.stdout = log
        return lab, observation, entry, objects

    def test_new_scheduled_work_proves_pre_admission_refusal_without_inventing_backup(self):
        lab, observation, entry, _ = self.fixture()
        result = rotation.admission_refusal(lab, observation, set(), set(), set(), 'target-uid', entry)
        self.assertEqual(result['path'], 'admission')
        self.assertEqual((result['job_uid'], result['pod_uid'], result['snapshot_uid']),
                         ('job-uid', 'pod-uid', 'snapshot-uid'))
        self.assertEqual(result['native_error'], 'InvalidAccessKeyId')
        self.assertNotIn('backup_uid', result)
        self.assertEqual(len(result['log_sha256']), 64)

    def test_other_failures_old_work_partial_logs_and_broken_ownership_cannot_pass(self):
        for fault in ('new-backup', 'old-job', 'old-snapshot', 'target-uid', 'target-secret',
                      'target-available', 'wrong-code', 'secret-uid', 'secret-data', 'cron-parent',
                      'job-parent', 'pod-parent', 'snapshot-source', 'snapshot-plan', 'snapshot-ready',
                      'job-ledger', 'pod-ledger', 'pod-phase', 'no-log', 'wrong-log-source', 'split-log'):
            with self.subTest(fault=fault):
                lab, observation, entry, objects = self.fixture()
                jobs, snapshots = set(), set()
                if fault == 'new-backup': observation['backups'] = [{'metadata': {'uid': 'new-backup'}}]
                elif fault == 'old-job': jobs.add('job-uid')
                elif fault == 'old-snapshot': snapshots.add('snapshot-uid')
                elif fault == 'target-uid': objects['backuptargets.longhorn.io']['metadata']['uid'] = 'replacement'
                elif fault == 'target-secret': objects['backuptargets.longhorn.io']['spec']['credentialSecret'] = 'other'
                elif fault == 'target-available': objects['backuptargets.longhorn.io']['status']['available'] = True
                elif fault == 'wrong-code': objects['backuptargets.longhorn.io']['status']['conditions'][0]['message'] = 'connection refused'
                elif fault == 'secret-uid': objects['secret']['metadata']['uid'] = 'replacement'
                elif fault == 'secret-data': objects['secret']['data']['key'] = 'changed'
                elif fault == 'cron-parent': observation['cron']['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                elif fault == 'job-parent': observation['jobs'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                elif fault == 'pod-parent': observation['pods'][0]['metadata']['ownerReferences'][0]['uid'] = 'foreign'
                elif fault == 'snapshot-source': observation['snapshots'][0]['spec']['volume'] = 'foreign'
                elif fault == 'snapshot-plan': observation['snapshots'][0]['spec']['labels']['RecurringJob'] = 'foreign'
                elif fault == 'snapshot-ready': observation['snapshots'][0]['status']['readyToUse'] = False
                elif fault == 'job-ledger': observation['row']['jobs']['new-job'] = 'replacement'
                elif fault == 'pod-ledger': observation['row']['pods']['new-pod'] = 'replacement'
                elif fault == 'pod-phase': observation['pods'][0]['status']['phase'] = 'Running'
                elif fault == 'no-log': lab.kubectl.return_value.stdout = ''
                elif fault == 'wrong-log-source': lab.kubectl.return_value.stdout = lab.kubectl.return_value.stdout.replace('volumeName=source', 'volumeName=foreign')
                elif fault == 'split-log': lab.kubectl.return_value.stdout = lab.kubectl.return_value.stdout.replace(' job=plan', '\njob=plan')
                try:
                    result = rotation.admission_refusal(lab, observation, set(), jobs, snapshots, 'target-uid', entry)
                except ValueError:
                    continue
                self.assertIsNone(result)

    def test_identity_changes_or_new_backup_during_log_read_fail_closed(self):
        for kind in ('pod', 'secret', 'backuptargets.longhorn.io', 'backups.longhorn.io'):
            with self.subTest(kind=kind):
                lab, observation, entry, objects = self.fixture()
                def logs(*_args, **_kwargs):
                    if kind == 'backups.longhorn.io':
                        objects[kind]['items'] = [{'metadata': {'uid': 'new-backup'},
                                                  'spec': {'labels': {'RecurringJob': 'plan'}}}]
                    else:
                        objects[kind]['metadata']['uid'] = 'replacement'
                    return Mock(stdout=lab.kubectl.return_value.stdout)
                lab.kubectl.side_effect = logs
                with self.assertRaises(ValueError):
                    rotation.admission_refusal(lab, observation, set(), set(), set(), 'target-uid', entry)


class ExpectedFailureContracts(unittest.TestCase):
    def fixture(self,directory):return existing.RecurringContracts().backup_fixture(directory)
    def test_error_callback_is_explicit_exact_and_default_remains_fail_closed(self):
        for fault in ('none','wrong-code','false-completed','source','target','uid','spec','missing-snapshot','false-callback'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                schedule,lab,values,_=self.fixture(directory)
                if fault!='missing-snapshot':schedule.sample()
                else:values['snapshots.longhorn.io']=[]
                point=values['backups.longhorn.io'][0]
                point['status']={'state':'Error','progress':0,'error':'AWS Error: InvalidAccessKeyId Invalid key'}
                if fault=='wrong-code':point['status']['error']='connection refused'
                elif fault=='false-completed':point['status']['progress']=100
                elif fault=='source':point['metadata']['labels']['backup-volume']='foreign'
                elif fault=='target':point['metadata']['labels']['backup-target']='foreign'
                elif fault=='uid':point['metadata']['uid']='foreign'
                elif fault=='spec':point['spec']['snapshotName']='foreign'
                def allow(obj):
                    if fault=='false-callback':return False
                    return bool(rotation.credential_failure({'backups':[obj]},set()))
                validator=Mock(side_effect=allow)
                with self.assertRaises(ValueError):schedule.sample()
                if fault=='none':self.assertEqual(schedule.sample(backup_failure=validator)['backups'][0]['status']['state'],'Error')
                else:
                    with self.assertRaises(ValueError):schedule.sample(backup_failure=validator)
                if fault in ('source','target','uid','spec','missing-snapshot'):validator.assert_not_called()
                self.assertTrue(schedule.record.exists());lab.remove.assert_not_called()
                schedule.sample(stopping=True) if fault in ('none','wrong-code','false-completed','false-callback') else None
    def test_source_deselection_preserves_schedule_and_foreign_labels_and_allows_reselection(self):
        for fault in ('none','missing','changed','source','race','schedule-missing','schedule-uid','schedule-spec'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                schedule,lab,values,_=self.fixture(directory)
                source=values['volumes.longhorn.io'];key='recurring-job-group.longhorn.io/group-owned'
                source['metadata'].update(resourceVersion='7',labels={key:'enabled','foreign':'keep'})
                if fault=='missing':source['metadata']['labels'].pop(key)
                elif fault=='changed':source['metadata']['labels'][key]='changed'
                elif fault=='source':source['metadata']['uid']='foreign'
                elif fault=='schedule-missing':values['recurringjobs.longhorn.io']=None
                elif fault=='schedule-uid':values['recurringjobs.longhorn.io']['metadata']['uid']='foreign'
                elif fault=='schedule-spec':values['recurringjobs.longhorn.io']['spec']['cron']='0 0 1 1 *'
                def native(*args,stdin):
                    patch=json.loads(stdin)
                    self.assertEqual(patch[:2],[{'op':'test','path':'/metadata/uid','value':'source-uid'},
                                              {'op':'test','path':'/metadata/resourceVersion','value':'7'}])
                    if patch[-1]['op']=='remove':
                        self.assertEqual(patch[2],{'op':'test','path':'/metadata/labels/recurring-job-group.longhorn.io~1group-owned','value':'enabled'})
                        source['metadata']['labels'].pop(key)
                        if fault=='race':source['metadata']['uid']='replacement'
                    else:source['metadata']['labels']=patch[-1]['value']
                lab.kubectl=Mock(side_effect=native)
                if fault=='none':
                    schedule.deselect_source();self.assertNotIn(key,source['metadata']['labels'])
                    schedule.select_source();self.assertEqual(source['metadata']['labels'][key],'enabled')
                    self.assertEqual(source['metadata']['labels']['foreign'],'keep')
                    self.assertEqual(values['recurringjobs.longhorn.io']['metadata']['uid'],'schedule-uid')
                else:
                    with self.assertRaises(ValueError):schedule.deselect_source()
                    if fault!='race':lab.kubectl.assert_not_called()
                lab.run_requests.assert_not_called();lab.remove.assert_not_called()
                self.assertTrue(schedule.record.exists())

