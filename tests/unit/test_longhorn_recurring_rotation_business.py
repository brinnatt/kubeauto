"""Scheduled key rotation, native rejection and independent restore flow."""
from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recurring_rotation_business', ROOT/'tests/helpers/longhorn-recurring-rotation.py')
rotation = importlib.util.module_from_spec(spec); spec.loader.exec_module(rotation)

class RotationBusinessContracts(unittest.TestCase):
    def test_completed_point_excludes_old_partial_failed_deleted_and_selects_newest(self):
        valid = {'metadata': {'name':'point','uid':'point-uid'},
                 'status': {'state':'Completed','progress':100,'backupCreatedAt':'2026-10-19T00:00:00Z'}}
        self.assertEqual(rotation.completed_point({'backups':[valid]},set()),valid)
        self.assertIsNone(rotation.completed_point({'backups':[valid]},{'point-uid'}))
        for fault in ('state','progress','backupCreatedAt','error','deletionTimestamp'):
            point=copy.deepcopy(valid)
            if fault=='deletionTimestamp':point['metadata'][fault]='2026-10-19T01:00:00Z'
            else:point['status'][fault]={'state':'InProgress','progress':99,'backupCreatedAt':'','error':'native failure'}[fault]
            with self.subTest(fault=fault):self.assertIsNone(rotation.completed_point({'backups':[point]},set()))
        newer=copy.deepcopy(valid);newer['metadata'].update(name='newer',uid='newer-uid');newer['status']['backupCreatedAt']='2026-10-19T00:01:00Z'
        self.assertEqual(rotation.completed_point({'backups':[newer,valid]},set()),newer)

    def fixture(self, directory, fault):
        test=self;root=Path(directory);events=[];objects={};selected={'value':False};target={'key':'root'}
        revoked=set();sequence={'job':0,'backup':0};configurations={}
        journal={'namespace':'business','volume':'source','volume_uid':'source-uid','objects':{'Pod/writer':'writer-uid'}}
        business_record=root/'longhorn-install-business.json';business_record.write_text(json.dumps(journal))
        row={'name':'owned-plan','uid':'plan-uid','cron_uid':'cron-uid','volume':'source',
             'spec':{'cron':'* * * * *'},'backups':{},'completed':[], 'jobs':{},'pods':{},'snapshots':{}}
        native={'jobs':[], 'pods':[], 'snapshots':[], 'log':''}
        secret={'metadata':{'uid':'selected-secret-uid'},'data':{'key':'encoded'}}
        entry={'name':'selected-secret','uid':'selected-secret-uid','digest':rotation.data_digest(secret['data'])}
        cron={'metadata':{'uid':'cron-uid','ownerReferences':[
            {'kind':'RecurringJob','uid':'plan-uid','apiVersion':'longhorn.io/v1beta2'}]}}
        class Schedule:
            record=root/'longhorn-recurring-runtime.json'
            def begin(self, *_args, **_kwargs):self.record.write_text('{}');events.append('begin')
            def read(self):return copy.deepcopy(row)
            def sample(self, *, backup_failure=None):
                for obj in objects.values():
                    row['backups'][obj['metadata']['name']]=obj['metadata']['uid']
                    if obj['status'].get('error'):
                        if backup_failure is None or backup_failure(copy.deepcopy(obj)) is not True:raise ValueError('unapproved native failure')
                return {'row':copy.deepcopy(row),'backups':copy.deepcopy(list(objects.values())),
                        'cron':copy.deepcopy(cron), **{key:copy.deepcopy(native[key]) for key in ('jobs','pods','snapshots')}}
            def select_source(self):
                test.assertFalse(selected['value']);selected['value']=True;events.append('select-source')
            def deselect_source(self):
                test.assertTrue(selected['value']);selected['value']=False;events.append('deselect-source')
            def cleanup(self,_relay):
                if not self.record.exists():return
                events.append('stop');self.record.unlink();selected['value']=False
                saved=json.loads(business_record.read_text())
                saved['backups']={name:{'uid':obj['metadata']['uid'],'spec':obj['spec']} for name,obj in objects.items()}
                business_record.write_text(json.dumps(saved))
        schedule=Schedule()
        class Accounts:
            def command(self,operation,key,*,account=None,allow_revoked=False,check=True):
                if account in revoked:
                    if not allow_revoked:raise ValueError('revoked client requires explicit fixture permission')
                    return SimpleNamespace(returncode=1,stdout='',code='InvalidAccessKeyId')
                name=key.rsplit('/backup_',1)[-1].removesuffix('.cfg')
                exists=name in configurations
                if operation=='stat':return SimpleNamespace(returncode=0 if exists else 1,stdout='',code=None if exists else 'NoSuchKey')
                test.assertEqual(operation,'cat');test.assertTrue(exists)
                raw=configurations[name]
                if fault=='signed-read' and account=='A':raw+='changed'
                return SimpleNamespace(returncode=0,stdout=raw,code=None)
            def revoke(self,account):revoked.add(account);events.append('revoke-'+account)
            def require_refusal(self,result,codes,_stage,**_kwargs):
                if result.returncode==0 or result.code not in codes:raise ValueError('wrong native refusal')
        accounts=Accounts()
        class Credentials:
            live=False;count=0
            def __init__(self,*_args):self.accounts=accounts
            def initialize(self):self.live=True;events.append('credentials')
            def read(self):return {'target_uid':'target-uid','secrets':{'B':entry}}
            def create(self):
                self.count+=1;return ('A','B','C')[self.count-1]
            def select(self,_relay,_schedule,identifier):
                test.assertNotIn(identifier,revoked);target['key']=identifier;events.append('product-'+identifier)
                if identifier=='C':
                    test.assertFalse(selected['value'])
                    if fault=='failed-point-became-completed':
                        for point in objects.values():
                            if point['status'].get('state')=='Error':
                                point['status']={'state':'Completed','progress':100,'backupCreatedAt':'2026-10-19T01:00:00Z'}
            def restore_root(self,_relay):
                if self.live:target['key']='root';events.append('restore-root')
            def cleanup(self):
                if not self.live:return
                test.assertEqual(target['key'],'root');test.assertFalse(schedule.record.exists())
                self.live=False;events.append('retire-credentials')
        class Store:
            def receipt(self):return {'bucket':'owned-bucket'}
            def backup_origin(self,volume,name,snapshot,job):
                metadata=json.loads(configurations[name])
                test.assertEqual((metadata['VolumeName'],metadata['SnapshotName'],metadata['Labels']['RecurringJob']),
                                 (volume,snapshot,job))
                return hashlib.sha256(configurations[name].encode()).hexdigest()
            def backup_receipt(self,volume,name,size):return {'volume':volume,'backup':name,'size':size,'objects':{'verified-block':{'sha256':'a'*64}}}
        store=Store()
        def wait(label,_timeout,check):
            if (fault == 'slow-native-drain' and label == 'credential-source-unselected-cycle'
                    and target['key'] in revoked):
                # Manager 1.12.1 waits 90 * 5 seconds for BackupStatus before
                # the current Job releases CronJob's Forbid slot. Then allow
                # the next one-minute unselected cycle and controller margin.
                test.assertGreaterEqual(_timeout, 90 * 5 + 60 + 30)
            sequence['job']+=1;job='job-'+str(sequence['job']);pod='pod-'+str(sequence['job'])
            row['completed'].append(job);row['jobs'][job]=job;row['pods'][pod]=pod
            native['jobs'].append({'metadata':{'name':job,'uid':job,'ownerReferences':[
                {'kind':'CronJob','uid':'cron-uid','controller':True}]},'status':{'succeeded':1}})
            native['pods'].append({'metadata':{'name':pod,'uid':pod,'ownerReferences':[
                {'kind':'Job','uid':job,'controller':True}]},'status':{'phase':'Succeeded'}})
            if selected['value']:
                sequence['backup']+=1;index=sequence['backup'];name='backup-'+str(index)
                bad=target['key'] in revoked or (fault=='new-error-after-repair' and target['key']=='C')
                status={'state':'Error' if bad else 'Completed','progress':0 if bad else 100}
                if bad:status['error']='AWS Error: InvalidAccessKeyId The AWS Access Key Id does not exist'
                else:status['backupCreatedAt']='2026-10-19T00:00:'+format(index,'02d')+'Z'
                if bad and fault=='false-completed':status.update(state='Completed',progress=100,backupCreatedAt='2026-10-19T01:00:00Z')
                if bad and fault=='wrong-native-error':status['error']='connection refused'
                snapshot='snapshot-'+str(index)
                row['snapshots'][snapshot]='uid-'+snapshot
                native['snapshots'].append({'metadata':{'name':snapshot,'uid':'uid-'+snapshot},
                    'spec':{'volume':'source','labels':{'RecurringJob':'owned-plan'}},'status':{'readyToUse':True}})
                point={'metadata':{'name':name,'uid':'uid-'+name},'spec':{'snapshotName':snapshot,'labels':{'RecurringJob':'owned-plan'}},'status':status}
                if bad and fault=='admission':
                    native['log']=('level=error msg="failed to complete backupAndCleanup: failed to backup snapshot: '
                        'admission webhook \\"validator.longhorn.io\\" denied the request: backup target default is not available" '
                        'job=owned-plan volumeName=source snapshotName='+snapshot)
                else:objects[name]=point
                if not bad or fault=='failed-metadata-exists':
                    configurations[name]=json.dumps({'Name':name,'VolumeName':'source','SnapshotName':snapshot,'Labels':{'RecurringJob':'owned-plan'}})
                events.append('native-'+('error' if bad else 'completed')+'-'+target['key'])
            result=check()
            if not result:raise TimeoutError(label)
            return result
        @contextmanager
        def scoped(clean):
            try:yield
            finally:clean()
        def kubectl(*args,**_kwargs):
            if args[:3]==('-n','longhorn-system','logs'):
                return SimpleNamespace(stdout=native['log'])
            test.assertEqual(args[:5],('-n','business','exec','writer','--'))
            payload=args[-1];return SimpleNamespace(stdout=hashlib.sha256(payload.encode()*262144).hexdigest())
        def restored(*args,**kwargs):
            events.append('restore-independent')
            test.assertEqual(args[2],'rotation');test.assertEqual(args[3],'backup-4')
            test.assertEqual(kwargs,{'source_client':'writer'})
            test.assertEqual(args[5],hashlib.sha256(args[4].encode()*262144).hexdigest())
            test.assertEqual(args[7],hashlib.sha256(args[6].encode()*262144).hexdigest())
        lab=SimpleNamespace(DIRECTORY=root,load_helper=Mock(side_effect=lambda name:
            SimpleNamespace(Schedule=lambda _lab:schedule) if name=='longhorn-recurring.py' else
            SimpleNamespace(volume_prefix=lambda _volume:'owned-prefix/')),
            backup_store=Mock(return_value=store),scoped_cleanup=scoped,wait=wait,
            write_private=lambda path,value:path.write_text(json.dumps(value)),bind_backups=Mock(),
            kubectl=Mock(side_effect=kubectl),payload_program=lambda value:value,verify_backup_restore=Mock(side_effect=restored))
        def get(kind,name=None,_namespace=None,**_kwargs):
            if kind=='pod':return copy.deepcopy(next(obj for obj in native['pods'] if obj['metadata']['name']==name))
            if kind=='secret':return copy.deepcopy(secret)
            if kind=='backups.longhorn.io':return {'items':copy.deepcopy(list(objects.values()))}
            test.assertEqual(kind,'backuptargets.longhorn.io')
            return {'metadata':{'uid':'target-uid'},'spec':{'credentialSecret':entry['name']},
                    'status':{'available':False,'conditions':[
                        {'type':'Unavailable','status':'True','message':'AWS Error: InvalidAccessKeyId invalid key'}]}}
        lab.get=Mock(side_effect=get)
        return lab,journal,events,Credentials,schedule,target

    def test_full_rotation_uses_new_points_and_restores_only_after_stopping_and_retiring(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,journal,events,credentials,schedule,target=self.fixture(directory,'none')
            digest=hashlib.sha256(b'original'*262144).hexdigest()
            with patch.object(rotation,'Credentials',credentials,create=True),redirect_stdout(io.StringIO()) as output:
                result=rotation.verify(lab,journal,Mock(),123,'original',digest,{'owned':'configuration'})
            self.assertEqual(result[0],'original-after-credential-rotation')
            self.assertEqual(events.count('native-error-B'),1)
            for key in ('A','B','C'):self.assertIn('native-completed-'+key,events)
            self.assertLess(events.index('product-B'),events.index('revoke-A'))
            self.assertEqual(events[-4:],['stop','restore-root','retire-credentials','restore-independent'])
            self.assertIn('"failed_metadata_absent": 1',output.getvalue())
            self.assertEqual(target['key'],'root');self.assertFalse(schedule.record.exists())
            lab.verify_backup_restore.assert_called_once()

    def test_each_failed_prerequisite_restores_root_before_stop_and_does_not_restore_data(self):
        for fault in ('signed-read','false-completed','wrong-native-error','new-error-after-repair',
                      'failed-metadata-exists','failed-point-became-completed'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                lab,journal,events,credentials,schedule,target=self.fixture(directory,fault)
                with patch.object(rotation,'Credentials',credentials,create=True),redirect_stdout(io.StringIO()),self.assertRaises(ValueError):
                    rotation.verify(lab,journal,Mock(),123,'original',hashlib.sha256(b'original'*262144).hexdigest(),{'owned':'configuration'})
                self.assertEqual(events[-3:],['restore-root','stop','retire-credentials'])
                self.assertEqual(target['key'],'root');self.assertFalse(schedule.record.exists())
                lab.verify_backup_restore.assert_not_called()

    def test_pre_admission_refusal_repairs_and_restores_without_fabricated_failed_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            lab,journal,events,credentials,schedule,target=self.fixture(directory,'admission')
            with patch.object(rotation,'Credentials',credentials),redirect_stdout(io.StringIO()) as output:
                rotation.verify(lab,journal,Mock(),123,'original',hashlib.sha256(b'original'*262144).hexdigest(),{'owned':'configuration'})
            evidence=json.loads(next(line.split(' ',1)[1] for line in output.getvalue().splitlines()
                if line.startswith('LONGHORN_RECURRING_CREDENTIAL_ROTATION_OBSERVED ')))
            self.assertEqual(evidence['failed_backups'],{})
            self.assertEqual(evidence['failed_metadata_absent'],0)
            self.assertEqual(evidence['revocation']['path'],'admission')
            self.assertEqual(events[-4:],['stop','restore-root','retire-credentials','restore-independent'])
            lab.verify_backup_restore.assert_called_once()

    def test_revoked_backup_drain_allows_native_start_timeout_and_next_unselected_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            lab, journal, events, credentials, schedule, target = self.fixture(directory, 'slow-native-drain')
            with patch.object(rotation, 'Credentials', credentials), redirect_stdout(io.StringIO()):
                rotation.verify(lab, journal, Mock(), 123, 'original',
                                hashlib.sha256(b'original' * 262144).hexdigest(), {'owned': 'configuration'})
            self.assertEqual(events[-4:], ['stop', 'restore-root', 'retire-credentials', 'restore-independent'])
            lab.verify_backup_restore.assert_called_once()


if __name__=='__main__':unittest.main()
