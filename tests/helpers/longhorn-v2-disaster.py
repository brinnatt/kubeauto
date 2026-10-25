#!/usr/bin/env python3
"""Native DR tasks on the two config+kubecli-qualified V2 installations."""
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import uuid
from urllib.parse import parse_qs, urlsplit

BASE=Path('/usr/local/kubeauto')


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def load(name):
    spec=importlib.util.spec_from_file_location('v2_dr_'+name.replace('-','_'),BASE/'tests/helpers'/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def guarded_patch(lab, kind, current, changes):
    metadata=current['metadata']
    operations=[{'op':'test','path':'/metadata/uid','value':metadata['uid']},
        {'op':'test','path':'/metadata/resourceVersion','value':metadata['resourceVersion']},
        {'op':'test','path':'/spec','value':current['spec']}]
    operations.extend({'op':'add','path':'/spec/'+key,'value':value} for key,value in changes.items())
    return lab.kubectl('-n','longhorn-system','patch',kind,metadata['name'],
        '--type=json','--patch-file=/dev/stdin','-o','json',stdin=json.dumps(operations))


def original(lab, kind, name, uid):
    current=lab.get(kind,name,'longhorn-system')
    if not uid or not current or current['metadata']['uid']!=uid:
        raise ValueError('DR object identity changed: '+kind+'/'+name)
    return current


def sync_target(lab, binding):
    target=original(lab,'backuptargets.longhorn.io','default',binding['target_uid'])
    backups=load('longhorn-backups.py')
    if backups.target_configuration(target['spec'])!=backups.target_configuration(binding['fixture_spec']):
        raise ValueError('DR backup target declaration changed')
    guarded_patch(lab,'backuptargets.longhorn.io',target,
        {'syncRequestedAt':datetime.now(timezone.utc).isoformat().replace('+00:00','Z')})


def discovered_backup(lab, name, volume, url):
    backup=lab.get('backups.longhorn.io',name,'longhorn-system')
    if not backup:return None
    labels=backup['metadata'].get('labels',{})
    if labels.get('backup-target')!='default' or labels.get('backup-volume')!=volume:
        raise ValueError('DR discovered backup belongs to a different target or volume')
    status=backup.get('status',{})
    if status.get('error'):raise ValueError('DR discovered backup reports an error')
    if status.get('state')!='Completed':return None
    if status.get('url')!=url:
        raise ValueError('DR discovered backup URL differs from the verified source backup')
    return backup


def standby_manifest(lab, name, url):
    return {'apiVersion':'longhorn.io/v1beta2','kind':'Volume',
        'metadata':{'name':name,'namespace':'longhorn-system'},
        'spec':{'size':str(2*1024**3),'accessMode':'rwo','numberOfReplicas':1,
                'fromBackup':url,'Standby':True,'dataEngine':'v2','frontend':'',
                'backupTargetName':'default',
                'image':lab.get('settings.longhorn.io','default-engine-image','longhorn-system')['value']}}


def standby_restored(lab, name, uid, backup, disk_uuid):
    volume=original(lab,'volumes.longhorn.io',name,uid)
    spec=volume['spec'];status=volume.get('status',{})
    if (spec.get('dataEngine')!='v2' or spec.get('numberOfReplicas')!=1 or
            spec.get('Standby') is not True or spec.get('frontend') or not spec.get('fromBackup')):
        raise ValueError('DR standby declaration changed')
    if any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==name for pv in lab.get('pv')['items']):
        raise ValueError('DR standby has a PV consumer before activation')
    engines=[e for e in lab.get('engines.longhorn.io',namespace='longhorn-system')['items']
             if e['spec'].get('volumeName')==name]
    replicas=[r for r in lab.get('replicas.longhorn.io',namespace='longhorn-system')['items']
              if r['spec'].get('volumeName')==name]
    if len(engines)!=1 or len(replicas)!=1:return None
    engine=engines[0];replica=replicas[0]
    if ((replica['spec'].get('nodeID') and replica['spec']['nodeID']!='longhorn-node-149') or
            (replica['spec'].get('diskID') and replica['spec']['diskID']!=disk_uuid) or
            replica['spec'].get('dataEngine')!='v2'):
        raise ValueError('DR standby replica escaped the admitted raw disk')
    restore=engine.get('status',{}).get('restoreStatus') or {}
    if any(row.get('error') for row in restore.values()):
        raise ValueError('DR standby engine reports a restore error')
    if (replica['spec'].get('nodeID')!='longhorn-node-149' or replica['spec'].get('diskID')!=disk_uuid or
            not status.get('isStandby') or status.get('state')!='attached' or status.get('robustness')!='healthy' or
            status.get('lastBackup')!=backup or not status.get('restoreRequired') or
            engine['spec'].get('dataEngine')!='v2' or engine['status'].get('currentState')!='running' or
            engine['spec'].get('requestedBackupRestore')!=backup or
            engine['status'].get('lastRestoredBackup')!=backup or
            any(row.get('isRestoring') for row in restore.values()) or
            replica['status'].get('currentState')!='running' or not replica['spec'].get('healthyAt') or
            replica['spec'].get('failedAt')):
        return None
    frontends=[e for e in lab.get('enginefrontends.longhorn.io',namespace='longhorn-system')['items']
               if e['spec'].get('volumeName')==name]
    if any(e['status'].get('endpoint') or
           (e['spec'].get('frontend') and not e['spec'].get('disableFrontend')) for e in frontends):
        raise ValueError('DR standby unexpectedly exposes a writable frontend')
    return {'volume_uid':uid,'engine_uid':engine['metadata']['uid'],
        'replica_uid':replica['metadata']['uid'],'disk_uuid':disk_uuid,'last_restored_backup':backup,
        'observed_at':datetime.now(timezone.utc).isoformat()}


class Standby:
    def __init__(self, source, target, backups, shared):
        self.source=source;self.target=target;self.backups=backups;self.shared=shared
        self.path=source.DIRECTORY/'longhorn-v2-dr.json'
        self.store=source.backup_store()
        self.consumer=shared.Consumer(target,self.store,backups)
        self.business_path=source.DIRECTORY/'longhorn-install-business.json'

    def save(self,row):self.source.write_private(self.path,row)

    def initialize(self):
        if self.path.exists() or self.path.is_symlink():raise ValueError('DR fixture already exists')
        row={'id':str(uuid.uuid4()),'identities':{},'intents':{},'volumes':{},'points':{}}
        for key,lab in (('source',self.source),('target',self.target)):
            row['identities'][key]={k:lab.owner()[k] for k in ('run_id','cluster_uid','namespace_uid','node_uids')}
        self.save(row)

    def receipt(self):
        if self.path.is_symlink():raise ValueError('symlink DR journal')
        row=json.loads(self.path.read_text());uuid.UUID(row['id'])
        for key,lab in (('source',self.source),('target',self.target)):
            if any(lab.owner().get(k)!=value for k,value in row['identities'][key].items()):
                raise ValueError('DR fixture belongs to another installation')
        return row

    def create(self,side,obj,journal=None):
        lab=self.source if side=='source' else self.target
        row=self.receipt();meta=obj['metadata'];key=side+'/'+obj['kind']+'/'+meta.get('namespace','')+'/'+meta['name']
        if key in row['intents'] or lab.get(obj['kind'],meta['name'],meta.get('namespace','')):
            raise ValueError('DR object name is already in use')
        row['intents'][key]={'side':side,'kind':obj['kind'],'name':meta['name'],
            'namespace':meta.get('namespace',''),'uid':None}
        self.save(row)
        if journal is None:
            created=json.loads(lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(obj)).stdout)
        else:created=lab.create_business_object(journal,self.business_path,obj)
        row['intents'][key]['uid']=created['metadata']['uid'];self.save(row)
        return created

    def validate_intents(self):
        row=self.receipt()
        for intent in row['intents'].values():
            lab=self.source if intent['side']=='source' else self.target
            current=lab.get(intent['kind'],intent['name'],intent['namespace'])
            if current and (not intent['uid'] or current['metadata']['uid']!=intent['uid']):
                raise ValueError('DR create result is unknown or its object was replaced')

    def source_workload(self):
        lab=self.source;namespace='longhorn-business-dr-'+uuid.uuid4().hex[:16]
        ns=self.create('source',{'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
        journal={'namespace':namespace,'namespace_uid':ns['metadata']['uid'],'objects':{},
            'storage_class':'longhorn','reclaim_policy':'Retain'}
        lab.write_private(self.business_path,journal)
        journal['defaults']=lab.wait('dr-source-namespace-defaults',60,lambda:lab.namespace_defaults(namespace))
        lab.write_private(self.business_path,journal)
        claim=self.create('source',{'apiVersion':'v1','kind':'PersistentVolumeClaim',
            'metadata':{'name':'data','namespace':namespace},'spec':{'storageClassName':'longhorn',
                'accessModes':['ReadWriteOnce'],'resources':{'requests':{'storage':'2Gi'}}}},journal)
        lab.wait('dr-source-claim-bound',600,lambda:(lab.get('pvc','data',namespace) or {}).get('status',{}).get('phase')=='Bound')
        lab.bind_business_volume(journal,self.business_path)
        if not journal.get('volume_uid'):raise ValueError('DR source has no original native volume binding')
        self.create('source',lab.client_pod(namespace,'writer','data','longhorn-node-146'),journal)
        def ready():
            pod=lab.get('pod','writer',namespace)
            if not pod or pod['metadata']['uid']!=journal['objects']['Pod/writer']:
                raise ValueError('DR source writer identity changed')
            return any(c['type']=='Ready' and c['status']=='True' for c in pod.get('status',{}).get('conditions',[]))
        lab.wait('dr-source-writer-ready',600,ready)
        lab.wait('dr-source-three-raw-replicas',600,lambda:lab.healthy_replicas(journal['volume']))
        return journal

    def payload(self,journal,payload,*,read_only=False):
        digest=hashlib.sha256(payload.encode()*262144).hexdigest()
        result=self.source.kubectl('-n',journal['namespace'],'exec','writer','--','python3','-c',
            self.source.payload_program(payload,read_only=read_only)).stdout.strip()
        if result!=digest:raise ValueError('DR source payload hash differs')
        return digest

    def backup_point(self,journal,payload,mode,pid,configuration):
        lab=self.source;name='dr-'+mode+'-'+uuid.uuid4().hex[:20];snapshot='dr-snapshot-'+uuid.uuid4().hex[:20]
        snap_request={'kind':'snapshot','name':snapshot,'volume':journal['volume']}
        request={'kind':'backup','name':name,'snapshot':snapshot,'mode':mode}
        journal.setdefault('native_snapshots',{})[snapshot]={'request':snap_request}
        journal.setdefault('backups',{})[name]={'request':request}
        lab.write_private(self.business_path,journal)
        lab.run_requests(pid,[snap_request,request],backup_configuration=configuration)
        lab.bind_snapshots(journal);lab.bind_backups(journal)
        backup=lab.get('backups.longhorn.io',name,'longhorn-system');status=backup.get('status',{})
        if status.get('state')!='Completed' or status.get('progress')!=100 or status.get('error'):
            raise ValueError('DR source product backup did not complete')
        url=status.get('url','');parts=urlsplit(url);query=parse_qs(parts.query)
        if (parts.scheme!='s3' or parts.netloc!=urlsplit(configuration['longhorn_backup_target']).netloc or
                query.get('backup')!=[name] or query.get('volume')!=[journal['volume']]):
            raise ValueError('DR backup URL is outside its exact source/store')
        receipt=self.store.backup_receipt(journal['volume'],name,2*1024**3)
        if receipt['incremental'] is not (mode=='incremental') or receipt['newly_uploaded_bytes']<=0:
            raise ValueError('DR external backup does not prove its full/incremental data change')
        journal['backups'][name]['object_receipt']=receipt;lab.write_private(self.business_path,journal)
        point={'name':name,'source_volume':journal['volume'],'url':url,'source_backup_uid':backup['metadata']['uid'],
            'receipt':receipt,'sha256':self.payload(journal,payload,read_only=True),
            'payload_bytes':len(payload.encode())*262144,'observed_at':datetime.now(timezone.utc).isoformat()}
        row=self.receipt();row['points'][name]=point;self.save(row)
        print('LONGHORN_V2_DR_BACKUP_POINT '+json.dumps({k:point[k] for k in ('name','source_volume','source_backup_uid','sha256','payload_bytes','receipt','observed_at')},sort_keys=True),flush=True)
        return point

    def discover(self,point):
        sync_target(self.target,self.consumer.receipt()['product_binding'])
        backup=self.target.wait('dr-target-backup-discovery-'+point['name'],600,
            lambda:discovered_backup(self.target,point['name'],point['source_volume'],point['url']))
        row=self.receipt();row['points'][point['name']]['target_backup_uid']=backup['metadata']['uid'];self.save(row)

    def create_standby(self,point):
        self.discover(point);name='dr-standby-'+uuid.uuid4().hex[:20]
        manifest=standby_manifest(self.target,name,point['url'])
        manifest['metadata']['labels']={'kubeauto.io/longhorn-dr-run':self.receipt()['id']}
        row=self.receipt();row['volumes'][name]={'uid':None,'from_backup':point['url']};self.save(row)
        created=self.create('target',manifest)
        row=self.receipt();row['volumes'][name]['uid']=created['metadata']['uid'];self.save(row)
        return name,created['metadata']['uid']

    def restored(self,name,uid,point):
        receipt=json.loads((self.target.DIRECTORY/'yml/longhorn/block-disks.json').read_text())
        disk=receipt['nodes']['192.168.47.149']['disks']['kubeauto-block']['disk_uuid']
        result=self.target.wait('dr-standby-restored-'+point['name'],900,
            lambda:standby_restored(self.target,name,uid,point['name'],disk))
        row=self.receipt();row['points'][point['name']]['restored']=result;self.save(row)
        print('LONGHORN_V2_DR_STANDBY_RESTORED '+json.dumps(result,sort_keys=True),flush=True)

    def retain_caches(self,lab,binding,side):
        row=self.receipt();points=row['points']
        allowed_names=set(points);allowed_volumes={p['source_volume'] for p in points.values()}
        target=original(lab,'backuptargets.longhorn.io','default',binding['target_uid'])
        if self.backups.target_configuration(target['spec']) not in (
                self.backups.target_configuration(binding['original_spec']),
                self.backups.target_configuration(binding['fixture_spec'])):
            raise ValueError('DR cache recovery target declaration changed')
        identities={}
        for kind in self.backups.BACKUP_RESOURCES:
            objects=lab.get(kind,namespace='longhorn-system')['items']
            for obj in objects:
                meta=obj['metadata'];labels=meta.get('labels',{})
                if kind=='backups.longhorn.io':
                    if meta['name'] not in allowed_names or labels.get('backup-target')!='default' or labels.get('backup-volume') not in allowed_volumes:
                        raise ValueError('DR cache recovery refuses foreign backup')
                    expected=points[meta['name']].get(side+'_backup_uid')
                    if not expected or expected!=meta['uid']:raise ValueError('DR cache backup identity changed or was not observed')
                elif kind=='backupvolumes.longhorn.io':
                    if obj['spec'].get('volumeName') not in allowed_volumes or obj['spec'].get('backupTargetName')!='default':
                        raise ValueError('DR cache recovery refuses foreign backup volume')
                else:raise ValueError('DR cache recovery refuses unrelated data-protection operation')
                identities[kind+'/'+meta['name']]=meta['uid']
        previous=row.setdefault('cache_identities',{}).get(side)
        if previous is not None and any(previous.get(k)!=uid for k,uid in identities.items()):
            raise ValueError('DR cache was replaced during recovery')
        row['cache_identities'][side]=identities;self.save(row)
        target=original(lab,'backuptargets.longhorn.io','default',binding['target_uid'])
        if self.backups.target_configuration(target['spec']) not in (
                self.backups.target_configuration(binding['original_spec']),
                self.backups.target_configuration(binding['fixture_spec'])):
            raise ValueError('DR target changed after its cache graph inspection')
        original_spec=dict(binding['original_spec'])
        if 'syncRequestedAt' in target['spec']:original_spec['syncRequestedAt']=target['spec']['syncRequestedAt']
        if self.backups.target_configuration(target['spec'])!=self.backups.target_configuration(original_spec):
            operations=[{'op':'test','path':'/metadata/uid','value':binding['target_uid']},
                {'op':'test','path':'/metadata/resourceVersion','value':target['metadata']['resourceVersion']},
                {'op':'test','path':'/spec','value':target['spec']},{'op':'replace','path':'/spec','value':original_spec}]
            lab.kubectl('-n','longhorn-system','patch','backuptargets.longhorn.io','default',
                '--type=json','--patch-file=/dev/stdin',stdin=json.dumps(operations))
        def retired():
            current=original(lab,'backuptargets.longhorn.io','default',binding['target_uid'])
            if self.backups.target_configuration(current['spec'])!=self.backups.target_configuration(binding['original_spec']):
                raise ValueError('DR target configuration changed during cache retirement')
            for kind in self.backups.BACKUP_RESOURCES:
                for obj in lab.get(kind,namespace='longhorn-system')['items']:
                    if identities.get(kind+'/'+obj['metadata']['name'])!=obj['metadata']['uid']:
                        raise ValueError('DR cache object was replaced during retirement')
            return not current.get('status',{}).get('available') and not any(
                lab.get(kind,namespace='longhorn-system')['items'] for kind in self.backups.BACKUP_RESOURCES)
        lab.wait('dr-'+side+'-cache-retired',600,retired)
        for point in points.values():
            if self.store.backup_receipt(point['source_volume'],point['name'],2*1024**3)!=point['receipt']:
                raise ValueError('DR cache retirement changed external backup data')
        print('LONGHORN_V2_DR_CACHE_RETIRED side='+side+' external_backups=preserved',flush=True)

    def recover_business_journal(self):
        """Close the namespace-create / business-journal crash window by UID.

        Unknown API outcomes remain refused by validate_intents. A known empty
        namespace can reuse the normal graph guard and cleanup; no claim or
        volume may be inferred from a name.
        """
        if self.business_path.exists() or self.business_path.is_symlink():return
        row=self.receipt()
        for intent in row['intents'].values():
            if intent['side']!='source' or intent['kind']!='Namespace':continue
            current=self.source.get('namespace',intent['name'])
            if not current:continue
            if not intent['uid'] or current['metadata']['uid']!=intent['uid']:
                raise ValueError('DR source namespace identity is unknown or replaced')
            journal={'namespace':intent['name'],'namespace_uid':intent['uid'],'objects':{},
                'storage_class':'longhorn','reclaim_policy':'Retain'}
            journal['defaults']=self.source.wait('dr-recovery-namespace-defaults',60,
                lambda:self.source.namespace_defaults(intent['name']))
            self.source.check_business_graph(journal)
            self.source.write_private(self.business_path,journal)

    def cleanup(self):
        if not self.path.exists() and not self.path.is_symlink():return
        self.validate_intents();row=self.receipt()
        self.source.restore_request_input();self.target.restore_request_input()
        for name,record in row['volumes'].items():
            volume=self.target.get('volumes.longhorn.io',name,'longhorn-system')
            if volume:
                if (not record['uid'] or volume['metadata']['uid']!=record['uid'] or
                        volume['spec'].get('fromBackup')!=record['from_backup'] or volume['spec'].get('Standby') is not True or
                        volume['metadata'].get('labels',{}).get('kubeauto.io/longhorn-dr-run')!=row['id'] or
                        any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==name for pv in self.target.get('pv')['items'])):
                    raise ValueError('DR standby cleanup refuses changed identity/declaration or consumer')
                self.target.remove(volume)
            self.target.wait('dr-standby-native-retired',600,lambda:self.target.get('volumes.longhorn.io',name,'longhorn-system') is None)
        for side,holder,lab in (('target',self.consumer,self.target),('source',self.store,self.source)):
            if holder.record.exists() or holder.record.is_symlink():
                binding=holder.receipt().get('product_binding')
                if binding:self.retain_caches(lab,binding,side)
        self.recover_business_journal()
        self.source.cleanup_business()
        for intent in row['intents'].values():
            lab=self.source if intent['side']=='source' else self.target
            if lab.get(intent['kind'],intent['name'],intent['namespace']):
                raise ValueError('DR journal still has a surviving fixture object')
        self.consumer.cleanup();self.store.cleanup()
        self.path.unlink()
        print('LONGHORN_V2_DR_STANDBY_FIXTURES_CLEAN_PASS',flush=True)

    def qualify(self,source_pid,target_pid):
        self.initialize()
        with self.source.scoped_cleanup(self.cleanup):
            self.store.prepare()
            # The target proves independent access before source transport has
            # ever existed. Store owns one transport lifetime per server.
            self.consumer.initialize();self.consumer.qualify(target_pid)
            self.store.qualify()
            journal=self.source_workload();configuration=self.store.product_configuration()
            payload=self.source.owner()['run_id'];self.payload(journal,payload)
            first=self.backup_point(journal,payload,'full',source_pid,configuration)
            name,uid=self.create_standby(first);self.restored(name,uid,first)
            payload+='-second-point';self.payload(journal,payload)
            second=self.backup_point(journal,payload,'incremental',source_pid,configuration)
            self.discover(second);self.restored(name,uid,second)
            self.payload(journal,payload,read_only=True)
            print('LONGHORN_V2_DR_STANDBY_SYNC_VERIFIED points=2 same_volume=true frontend=false pv=false',flush=True)
        print('LONGHORN_V2_DR_STANDBY_BUSINESS_PASS',flush=True)


def qualification_current():
    evidence=json.loads((evidence_path('tests/evidence/longhorn-v2-dr-store-qualification-20261021.json')).read_text())
    if evidence['status']!='pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('DR standby requires its completed shared-store prerequisite')
    for name in ('roles/longhorn/files/longhorn.py','tests/helpers/longhorn-v2-dr-store.py',
                 'tests/helpers/longhorn-v2-dr-target-install.py','tests/helpers/longhorn-backups.py',
                 'tests/helpers/longhorn-v2-backups.py','tests/helpers/longhorn-lab.py'):
        if evidence['frozen_inputs'].get(name)!=hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('DR shared-store dependency changed: '+name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('qualify','cleanup','qualification-current'))
    parser.add_argument('--source-relay-pid',type=int,default=0)
    parser.add_argument('--target-relay-pid',type=int,default=0)
    args=parser.parse_args()
    shared=load('longhorn-v2-dr-store.py')
    _,_,source,backups,_,_,target=shared.fixture()
    source.require_lease();target.require_lease()
    scenario=Standby(source,target,backups,shared)
    if args.mode=='cleanup':scenario.cleanup()
    elif args.mode=='qualification-current':qualification_current()
    else:
        qualification_current()
        scenario.qualify(args.source_relay_pid,args.target_relay_pid)


if __name__=='__main__':
    try:main()
    except Exception as error:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w',prefix='longhorn-v2-dr-standby-failure-',
                suffix='.txt',dir='/var/tmp',delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_DR_STANDBY_FAIL reason='+type(error).__name__+
                ' private_diagnostic='+diagnostic.name,file=sys.stderr)
        sys.exit(1)
