#!/usr/bin/env python3
"""Planned V2 handoff with source fencing and restore to a new return volume."""
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def fenced(lab, journal, original):
    volume=original(lab,'volumes.longhorn.io',journal['volume'],journal['volume_uid'])
    if (volume['spec'].get('dataEngine')!='v2' or volume['spec'].get('frontend')!='blockdev' or
            volume['spec'].get('disableFrontend') is not True or volume['spec'].get('nodeID') or
            volume.get('status',{}).get('currentNodeID') or volume.get('status',{}).get('state')!='detached'):
        raise ValueError('DR fencing requires disabled frontend and detached source')
    if any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==journal['volume'] for pv in lab.get('pv')['items']):
        raise ValueError('DR fencing source still has a PV')
    if lab.get('pvc','data',journal['namespace']) or lab.get('pod',namespace=journal['namespace'])['items']:
        raise ValueError('DR fencing source still has workloads or its claim')
    if any(va['spec'].get('volume')==journal['volume'] and va['spec'].get('attachmentTickets')
           for va in lab.get('volumeattachments.longhorn.io',namespace='longhorn-system')['items']):
        raise ValueError('DR fencing source still has attachment tickets')
    stopped={}
    for kind in ('engines.longhorn.io','enginefrontends.longhorn.io'):
        for obj in lab.get(kind,namespace='longhorn-system')['items']:
            if obj['spec'].get('volumeName')!=journal['volume']:continue
            if obj['spec'].get('desireState')!='stopped' or obj['status'].get('currentState')!='stopped':
                raise ValueError('DR fencing engine/frontend is not stopped')
            if kind=='enginefrontends.longhorn.io' and obj['status'].get('endpoint'):
                raise ValueError('DR fencing source still exposes a business endpoint')
            stopped[kind+'/'+obj['metadata']['name']]=obj['metadata']['uid']
    return {'cluster_uid':lab.owner()['cluster_uid'],'volume_uid':volume['metadata']['uid'],
        'spec_sha256':hashlib.sha256(json.dumps(volume['spec'],sort_keys=True).encode()).hexdigest(),
        'stopped_instances':stopped}


def fence(lab, journal, parent):
    """Retire only this fixture's writer, claim and Retain PV; never force detach."""
    lab.check_business_graph(journal)
    pod=lab.get('pod','writer',journal['namespace'])
    if pod:
        if pod['metadata']['uid']!=journal['objects']['Pod/writer']:
            raise ValueError('DR original writer changed before fencing')
        lab.remove(pod)
    lab.wait('dr-fence-source-pods-retired',180,lambda:not lab.get('pod',namespace=journal['namespace'])['items'])
    def detached():
        obj=parent.original(lab,'volumes.longhorn.io',journal['volume'],journal['volume_uid'])
        return (obj['status'].get('state')=='detached' and not obj['spec'].get('nodeID') and
                not obj['status'].get('currentNodeID'))
    lab.wait('dr-fence-native-detached',300,detached)
    claim=lab.get('pvc','data',journal['namespace'])
    if claim:
        if claim['metadata']['uid']!=journal['objects']['PersistentVolumeClaim/data']:
            raise ValueError('DR original source claim changed before fencing')
        lab.remove(claim)
    lab.wait('dr-fence-claim-retired',180,lambda:lab.get('pvc','data',journal['namespace']) is None)
    pv=lab.get('pv',journal['pv'])
    if pv:
        if (pv['metadata']['uid']!=journal['pv_uid'] or
                pv['spec'].get('claimRef',{}).get('uid')!=journal['objects']['PersistentVolumeClaim/data'] or
                pv['spec'].get('csi',{}).get('volumeHandle')!=journal['volume'] or
                pv['spec'].get('persistentVolumeReclaimPolicy')!='Retain'):
            raise ValueError('DR original Retain PV binding changed before fencing')
        lab.remove(pv)
    lab.wait('dr-fence-pv-retired',180,lambda:lab.get('pv',journal['pv']) is None)
    current=parent.original(lab,'volumes.longhorn.io',journal['volume'],journal['volume_uid'])
    if not current['spec'].get('disableFrontend'):
        parent.guarded_patch(lab,'volumes.longhorn.io',current,{'disableFrontend':True})
    # A failed prerequisite is a stop, never a forced attach/detach or claim
    # recreation. The coordinator retains its exact original native identity.
    return fenced(lab,journal,parent.original)


def activate(source,journal,target,name,uid,point,disk_uuid,proof,parent,initial_url):
    if not proof or proof!=fenced(source,journal,parent.original):
        raise ValueError('DR activation requires current source fencing proof')
    if not parent.standby_restored(target,name,uid,point['name'],disk_uuid):
        raise ValueError('DR activation requires the approved latest point restored')
    volume=parent.original(target,'volumes.longhorn.io',name,uid)
    if volume['spec'].get('fromBackup')!=initial_url or volume['spec'].get('size')!=str(2*1024**3):
        raise ValueError('DR standby original restore declaration changed')
    parent.guarded_patch(target,'volumes.longhorn.io',volume,{'Standby':False,'frontend':'blockdev'})


def activated(lab,name,uid,parent):
    obj=parent.original(lab,'volumes.longhorn.io',name,uid)
    spec=obj['spec'];status=obj.get('status',{})
    if (spec.get('dataEngine')!='v2' or spec.get('Standby') is not False or spec.get('frontend')!='blockdev'):
        raise ValueError('DR activated declaration changed')
    return (status.get('restoreInitiated') is True and not status.get('isStandby') and not status.get('restoreRequired') and
            status.get('state')=='detached' and not status.get('currentNodeID'))


def static_manifests(namespace,claim,pv,volume,claim_uid,replicas):
    """Follow pinned manager datastore.NewPVManifest with explicit prebinding."""
    pvc={'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':claim,'namespace':namespace},
        'spec':{'storageClassName':'longhorn','accessModes':['ReadWriteOnce'],
            'volumeName':pv,'resources':{'requests':{'storage':'2Gi'}}}}
    persistent={'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':pv},
        'spec':{'storageClassName':'longhorn','persistentVolumeReclaimPolicy':'Retain',
            'capacity':{'storage':'2Gi'},'accessModes':['ReadWriteOnce'],'volumeMode':'Filesystem',
            'claimRef':{'apiVersion':'v1','kind':'PersistentVolumeClaim','namespace':namespace,'name':claim,'uid':claim_uid},
            'csi':{'driver':'driver.longhorn.io','fsType':'ext4','volumeHandle':volume,
                'volumeAttributes':{'numberOfReplicas':str(replicas),'staleReplicaTimeout':'2880'}}}}
    return pvc,persistent


class Failover:
    """Coordinate only exact fixtures on two already-qualified installations."""
    def __init__(self,dr,parent,target_install):
        self.dr=dr;self.parent=parent;self.source=dr.source;self.target=dr.target
        self.store=dr.store;self.consumer=dr.consumer;self.target_install=target_install

    def context_path(self,role):
        if role=='target':return self.target.DIRECTORY/'longhorn-install-business.json'
        if role=='failback':return self.source.DIRECTORY/'longhorn-dr-failback-business.json'
        raise ValueError('unknown DR business role')

    def context_lab(self,role):
        return self.target if role=='target' else self.source

    def save_context(self,role,journal):
        self.context_lab(role).write_private(self.context_path(role),journal)

    def create_bound(self,role,journal,obj):
        side='target' if role=='target' else 'source'
        created=self.dr.create(side,obj)
        journal['objects'][obj['kind']+'/'+obj['metadata']['name']]=created['metadata']['uid']
        self.save_context(role,journal)
        return created

    def recover_context(self,role):
        row=self.dr.receipt();record=row.get('contexts',{}).get(role)
        if not record:return None
        lab=self.context_lab(role);path=self.context_path(role)
        if path.is_symlink():raise ValueError('symlink DR context journal')
        namespace=record['namespace'];ns=lab.get('namespace',namespace)
        if not ns:
            if path.exists():raise ValueError('DR context namespace disappeared before receipt retirement')
            return None
        side='target' if role=='target' else 'source'
        intents=[i for i in row['intents'].values() if i['side']==side]
        known=[i for i in intents if i['kind']=='Namespace' and i['name']==namespace]
        if len(known)!=1 or ns['metadata']['uid']!=known[0]['uid']:
            raise ValueError('DR context namespace changed or creation response unknown')
        if path.exists():journal=json.loads(path.read_text())
        else:journal={'namespace':namespace,'namespace_uid':known[0]['uid'],'objects':{},
            'storage_class':'longhorn','reclaim_policy':'Retain'}
        if journal['namespace']!=namespace or journal['namespace_uid']!=known[0]['uid']:
            raise ValueError('DR context journal points to another namespace')
        for intent in intents:
            if intent['namespace']==namespace or (intent['kind']=='PersistentVolume' and intent['name']==record['pv']):
                current=lab.get(intent['kind'],intent['name'],intent['namespace'])
                if current and (not intent['uid'] or current['metadata']['uid']!=intent['uid']):
                    raise ValueError('DR context create outcome unknown or object replaced')
                key=intent['kind']+'/'+intent['name']
                if key in journal['objects'] and journal['objects'][key]!=intent['uid']:
                    raise ValueError('DR context object journal changed')
                if intent['uid']:journal['objects'][key]=intent['uid']
        if not journal.get('defaults'):
            journal['defaults']=lab.wait('dr-context-recovery-defaults',60,lambda:lab.namespace_defaults(namespace))
        lab.check_business_graph(journal)
        self.save_context(role,journal)
        return journal

    def mount(self,role,name,uid,replicas,node):
        import uuid
        lab=self.context_lab(role);path=self.context_path(role)
        if path.exists() or path.is_symlink():raise ValueError('previous DR context needs recovery')
        row=self.dr.receipt();namespace='longhorn-business-'+role+'-'+uuid.uuid4().hex[:16]
        pv='dr-'+role+'-'+uuid.uuid4().hex[:20]
        if role in row.get('contexts',{}):raise ValueError('DR context name already journaled')
        row.setdefault('contexts',{})[role]={'namespace':namespace,'pv':pv,'volume':name,'volume_uid':uid}
        self.dr.save(row)
        self.dr.create('target' if role=='target' else 'source',{'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})
        journal=self.recover_context(role)
        claim,_=static_manifests(namespace,'data',pv,name,'',replicas)
        created=self.create_bound(role,journal,claim)
        _,persistent=static_manifests(namespace,'data',pv,name,created['metadata']['uid'],replicas)
        self.create_bound(role,journal,persistent)
        def bound():
            current=lab.get('pvc','data',namespace)
            if not current or current['metadata']['uid']!=created['metadata']['uid']:
                raise ValueError('DR static claim identity changed')
            return current.get('status',{}).get('phase')=='Bound'
        lab.wait('dr-'+role+'-static-bound',300,bound)
        lab.bind_business_volume(journal,path)
        if journal.get('volume')!=name or journal.get('volume_uid')!=uid:
            raise ValueError('DR static claim bound to another native volume')
        self.create_bound(role,journal,lab.client_pod(namespace,'writer','data',node))
        def ready():
            pod=lab.get('pod','writer',namespace)
            if not pod or pod['metadata']['uid']!=journal['objects']['Pod/writer']:
                raise ValueError('DR restored writer changed identity')
            return any(c['type']=='Ready' and c['status']=='True' for c in pod.get('status',{}).get('conditions',[]))
        lab.wait('dr-'+role+'-writer-ready',600,ready)
        if role=='target':lab.wait('dr-target-raw-replica',600,lambda:self.target_install.replica_ready(lab,journal))
        else:lab.wait('dr-failback-three-raw-replicas',600,lambda:lab.healthy_replicas(name))
        return journal

    def data(self,lab,journal,payload,read_only):
        expected=hashlib.sha256(payload.encode()*262144).hexdigest()
        actual=lab.kubectl('-n',journal['namespace'],'exec','writer','--','python3','-c',
            lab.payload_program(payload,read_only=read_only)).stdout.strip()
        if actual!=expected:raise ValueError('DR recovered business hash differs')
        return expected

    def retire_context(self,role):
        journal=self.recover_context(role)
        if journal is None:return
        lab=self.context_lab(role);row=self.dr.receipt()['contexts'][role]
        lab.check_business_graph(journal)
        for kind in ('Pod','PersistentVolumeClaim','PersistentVolume'):
            for key,uid in journal['objects'].items():
                if not key.startswith(kind+'/'):continue
                name=key.split('/',1)[1];namespace='' if kind=='PersistentVolume' else journal['namespace']
                current=lab.get(kind,name,namespace)
                if current:
                    if current['metadata']['uid']!=uid:raise ValueError('DR cleanup context object replaced')
                    if kind=='PersistentVolume' and (
                            current['spec'].get('csi',{}).get('volumeHandle')!=row['volume'] or
                            current['spec'].get('persistentVolumeReclaimPolicy')!='Retain' or
                            current['spec'].get('claimRef',{}).get('uid')!=journal['objects'].get('PersistentVolumeClaim/data')):
                        raise ValueError('DR cleanup static PV binding changed')
                    lab.remove(current)
                lab.wait('dr-cleanup-'+role+'-'+kind+'-'+name,180,
                    lambda kind=kind,name=name,namespace=namespace:lab.get(kind,name,namespace) is None)
        volume=lab.get('volumes.longhorn.io',row['volume'],'longhorn-system')
        if volume:
            def detached():
                current=self.parent.original(lab,'volumes.longhorn.io',row['volume'],row['volume_uid'])
                if any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==row['volume'] for pv in lab.get('pv')['items']):
                    raise ValueError('DR cleanup still has a PV consumer')
                return current if current['status'].get('state')=='detached' else None
            current=lab.wait('dr-cleanup-'+role+'-native-detached',300,detached)
            lab.remove(current)
            lab.wait('dr-cleanup-'+role+'-native-retired',300,
                lambda:lab.get('volumes.longhorn.io',row['volume'],'longhorn-system') is None)

    def cleanup(self):
        if not self.dr.path.exists() and not self.dr.path.is_symlink():return
        self.dr.validate_intents();row=self.dr.receipt()
        self.source.restore_request_input();self.target.restore_request_input()
        # Retire restored consumers before disconnecting caches or removing any
        # external backup. Partial creation with no confirmed UID stops above.
        for role in ('failback','target'):self.retire_context(role)
        row=self.dr.receipt()
        for name,record in row['volumes'].items():
            volume=self.target.get('volumes.longhorn.io',name,'longhorn-system')
            if not volume:continue
            if (not record['uid'] or volume['metadata']['uid']!=record['uid'] or
                    volume['spec'].get('fromBackup')!=record['from_backup'] or
                    volume['metadata'].get('labels',{}).get('kubeauto.io/longhorn-dr-run')!=row['id'] or
                    any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==name for pv in self.target.get('pv')['items'])):
                raise ValueError('DR cleanup native target identity or consumer changed')
            intent=row.get('activation',{})
            expected_standby=volume['spec'].get('Standby') is True
            expected_active=(intent.get('volume_uid')==record['uid'] and intent.get('volume')==name and
                volume['spec'].get('Standby') is False and volume['spec'].get('frontend')=='blockdev' and
                volume['spec'].get('dataEngine')=='v2' and volume['spec'].get('numberOfReplicas')==1)
            if not expected_standby and not expected_active:raise ValueError('DR cleanup target was activated outside this workflow')
            self.target.remove(volume)
            self.target.wait('dr-cleanup-native-target',600,
                lambda:self.target.get('volumes.longhorn.io',name,'longhorn-system') is None)
        # A new failback native volume can exist before its business namespace.
        row=self.dr.receipt();record=row.get('failback_volume')
        if record:
            current=self.source.get('volumes.longhorn.io',record['name'],'longhorn-system')
            if current:
                if (not record['uid'] or current['metadata']['uid']!=record['uid'] or
                        current['spec'].get('fromBackup')!=record['from_backup'] or
                        current['metadata'].get('labels',{}).get('kubeauto.io/longhorn-dr-run')!=row['id'] or
                        any(pv.get('spec',{}).get('csi',{}).get('volumeHandle')==record['name'] for pv in self.source.get('pv')['items'])):
                    raise ValueError('DR cleanup failback identity or consumer changed')
                self.source.remove(current)
                self.source.wait('dr-cleanup-native-failback',600,
                    lambda:self.source.get('volumes.longhorn.io',record['name'],'longhorn-system') is None)
        for side,holder,lab in (('target',self.consumer,self.target),('source',self.store,self.source)):
            if holder.record.exists() or holder.record.is_symlink():
                binding=holder.receipt().get('product_binding')
                if binding:self.dr.retain_caches(lab,binding,side)
        for role in ('failback','target'):
            journal=self.recover_context(role)
            if journal is not None:self.context_lab(role).cleanup_business(self.context_path(role))
        self.dr.cleanup()
        print('LONGHORN_V2_DR_FAILOVER_FIXTURES_CLEAN_PASS',flush=True)

    def target_backup(self,journal,payload,pid):
        import uuid
        from urllib.parse import parse_qs,urlsplit
        lab=self.target;path=self.context_path('target')
        binding=self.consumer.receipt()['product_binding']
        configuration={'longhorn_backup_target':binding['fixture_spec']['backupTargetURL'],
            'longhorn_backup_secret':binding['secret_name']}
        name='dr-return-'+uuid.uuid4().hex[:20];snapshot='dr-return-snapshot-'+uuid.uuid4().hex[:16]
        snap_request={'kind':'snapshot','name':snapshot,'volume':journal['volume']}
        request={'kind':'backup','name':name,'snapshot':snapshot,'mode':'full'}
        journal.setdefault('native_snapshots',{})[snapshot]={'request':snap_request}
        journal.setdefault('backups',{})[name]={'request':request}
        self.save_context('target',journal)
        lab.run_requests(pid,[snap_request,request],backup_configuration=configuration)
        lab.bind_snapshots(journal);lab.bind_backups(journal)
        backup=lab.get('backups.longhorn.io',name,'longhorn-system');status=backup.get('status',{})
        if status.get('state')!='Completed' or status.get('progress')!=100 or status.get('error'):
            raise ValueError('DR return backup did not complete')
        url=status.get('url','');parts=urlsplit(url);query=parse_qs(parts.query)
        if (parts.scheme!='s3' or parts.netloc!=urlsplit(configuration['longhorn_backup_target']).netloc or
                query.get('volume')!=[journal['volume']] or query.get('backup')!=[name]):
            raise ValueError('DR return backup URL escaped its recovery volume/store')
        receipt=self.store.backup_receipt(journal['volume'],name,2*1024**3)
        if receipt['incremental'] is not False or receipt['newly_uploaded_bytes']<=0:
            raise ValueError('DR return backup does not prove an independent full copy')
        journal['backups'][name]['object_receipt']=receipt;self.save_context('target',journal)
        point={'name':name,'source_volume':journal['volume'],'url':url,
            'target_backup_uid':backup['metadata']['uid'],'receipt':receipt,
            'sha256':self.data(lab,journal,payload,True),'payload_bytes':len(payload.encode())*262144,
            'observed_at':datetime.now(timezone.utc).isoformat()}
        row=self.dr.receipt();row['points'][name]=point;self.dr.save(row)
        print('LONGHORN_V2_DR_RETURN_BACKUP '+json.dumps(point,sort_keys=True),flush=True)
        return point

    def discover_return(self,point):
        binding=self.store.receipt()['product_binding']
        self.parent.sync_target(self.source,binding)
        backup=self.source.wait('dr-source-return-backup-discovery',600,
            lambda:self.parent.discovered_backup(self.source,point['name'],point['source_volume'],point['url']))
        row=self.dr.receipt();row['points'][point['name']]['source_backup_uid']=backup['metadata']['uid'];self.dr.save(row)

    def restore_return(self,point):
        import uuid
        self.discover_return(point);name='dr-failback-'+uuid.uuid4().hex[:20]
        row=self.dr.receipt();row['failback_volume']={'name':name,'uid':None,'from_backup':point['url']};self.dr.save(row)
        manifest=self.parent.standby_manifest(self.source,name,point['url'])
        manifest['spec'].update(Standby=False,frontend='blockdev',numberOfReplicas=3)
        manifest['metadata']['labels']={'kubeauto.io/longhorn-dr-run':row['id']}
        created=self.dr.create('source',manifest)
        row=self.dr.receipt();row['failback_volume']['uid']=created['metadata']['uid'];self.dr.save(row)
        def restored():
            current=self.parent.original(self.source,'volumes.longhorn.io',name,created['metadata']['uid'])
            if current['spec'].get('fromBackup')!=point['url'] or current['spec'].get('numberOfReplicas')!=3:
                raise ValueError('DR return restore source or replica declaration changed')
            return activated(self.source,name,created['metadata']['uid'],self.parent)
        self.source.wait('dr-failback-restored-detached',900,restored)
        return name,created['metadata']['uid']

    def qualify(self,source_pid,target_pid,stage):
        if stage not in ("activation","failback"):raise ValueError("unknown DR stage")
        self.dr.initialize()
        with self.source.scoped_cleanup(self.cleanup):
            self.store.prepare();self.consumer.initialize();self.consumer.qualify(target_pid);self.store.qualify()
            journal=self.dr.source_workload();configuration=self.store.product_configuration()
            payload=self.source.owner()['run_id'];self.dr.payload(journal,payload)
            first=self.dr.backup_point(journal,payload,'full',source_pid,configuration)
            name,uid=self.dr.create_standby(first);self.dr.restored(name,uid,first)
            payload+='-final-source';self.dr.payload(journal,payload)
            last=self.dr.backup_point(journal,payload,'incremental',source_pid,configuration)
            self.dr.discover(last);self.dr.restored(name,uid,last)
            raw=json.loads((self.target.DIRECTORY/'yml/longhorn/block-disks.json').read_text())
            disk=raw['nodes']['192.168.47.149']['disks']['kubeauto-block']['disk_uuid']
            try:activate(self.source,journal,self.target,name,uid,last,disk,None,self.parent,first["url"])
            except ValueError as error:
                if str(error)!='DR activation requires current source fencing proof':raise
            else:raise ValueError('DR workflow accepted an unfenced activation')
            print('LONGHORN_V2_DR_WORKFLOW_UNFENCED_ACTIVATION_REFUSED native_api_not_called=true',flush=True)
            self.dr.payload(journal,payload,read_only=True)
            proof=fence(self.source,journal,self.parent)
            print('LONGHORN_V2_DR_SOURCE_FENCED '+json.dumps(proof,sort_keys=True),flush=True)
            row=self.dr.receipt();row['activation']={'volume':name,'volume_uid':uid,
                'approved_backup':last['name'],'fence':proof};self.dr.save(row)
            activate(self.source,journal,self.target,name,uid,last,disk,proof,self.parent,first["url"])
            self.target.wait('dr-activated-detached',900,lambda:activated(self.target,name,uid,self.parent))
            restored=self.mount('target',name,uid,1,'longhorn-node-149')
            restored_digest=self.data(self.target,restored,payload,True)
            recovery_payload=payload+'-recovery-site-write';digest=self.data(self.target,restored,recovery_payload,False)
            if proof!=fenced(self.source,journal,self.parent.original):
                raise ValueError('DR source fencing changed after recovery-site write')
            print('LONGHORN_V2_DR_ACTIVATED_BUSINESS_VERIFIED '+json.dumps({'volume_uid':uid,
                'restored_backup':last['name'],'restored_sha256':restored_digest,'recovery_sha256':digest,'source_fenced':True},sort_keys=True),flush=True)
            if stage=='failback':
                point=self.target_backup(restored,recovery_payload,target_pid)
                back_name,back_uid=self.restore_return(point)
                if back_uid in (journal['volume_uid'],uid):raise ValueError('DR failback reused an old native identity')
                reverse=fence(self.target,restored,self.parent)
                print('LONGHORN_V2_DR_RECOVERY_FENCED '+json.dumps(reverse,sort_keys=True),flush=True)
                row=self.dr.receipt();row['return_fence']=reverse;self.dr.save(row)
                if proof!=fenced(self.source,journal,self.parent.original):raise ValueError('DR original source fencing changed')
                returned=self.mount('failback',back_name,back_uid,3,'longhorn-node-147')
                self.data(self.source,returned,recovery_payload,True)
                final_payload=recovery_payload+'-return-site-write';final_digest=self.data(self.source,returned,final_payload,False)
                if reverse!=fenced(self.target,restored,self.parent.original):raise ValueError('DR reverse fencing changed')
                if proof!=fenced(self.source,journal,self.parent.original):raise ValueError('DR obsolete original volume became writable')
                print('LONGHORN_V2_DR_FAILBACK_BUSINESS_VERIFIED '+json.dumps({'new_volume_uid':back_uid,
                    'original_source_uid':journal['volume_uid'],'recovery_uid':uid,'return_backup':point['name'],
                    'restored_sha256':point['sha256'],'final_sha256':final_digest,'reverse_fenced':True,
                    'obsolete_original_fenced':True},sort_keys=True),flush=True)
        print('LONGHORN_V2_DR_'+stage.upper()+'_BUSINESS_PASS',flush=True)


BASE=Path('/usr/local/kubeauto')


def load(name):
    spec=importlib.util.spec_from_file_location('dr_failover_'+name.replace('-','_'),BASE/'tests/helpers'/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def fixture():
    parent=load('longhorn-v2-disaster.py');shared=load('longhorn-v2-dr-store.py')
    _,_,source,backups,install,_,target=shared.fixture()
    return Failover(parent.Standby(source,target,backups,shared),parent,install)


def qualification_current(scenario):
    scenario.parent.qualification_current()
    evidence=json.loads((evidence_path('tests/evidence/longhorn-v2-dr-standby-qualification-20261021.json')).read_text())
    if evidence['status']!='pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('DR handoff requires completed two-point standby synchronization')
    for name in ('tests/helpers/longhorn-v2-disaster.py','tests/helpers/longhorn-v2-disaster.sh',
                 'tests/helpers/longhorn-lab.py','roles/longhorn/files/longhorn.py'):
        if evidence['frozen_inputs'].get(name)!=hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('DR standby dependency changed: '+name)
    for side,lab in (('source',scenario.source),('target',scenario.target)):
        if any(lab.owner().get(key)!=value for key,value in evidence['baseline_identity'][side].items()):
            raise ValueError('DR installation identity changed')
    for path in (scenario.context_path('failback'),scenario.dr.path):
        if path.exists() or path.is_symlink():raise ValueError('previous DR coordinator needs recovery')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('qualify','cleanup','qualification-current'))
    parser.add_argument('--stage',choices=('activation','failback'),default='activation')
    parser.add_argument('--source-relay-pid',type=int,default=0)
    parser.add_argument('--target-relay-pid',type=int,default=0)
    args=parser.parse_args();scenario=fixture()
    scenario.source.require_lease();scenario.target.require_lease()
    if args.mode=='cleanup':scenario.cleanup()
    elif args.mode=='qualification-current':qualification_current(scenario)
    else:
        qualification_current(scenario)
        scenario.qualify(args.source_relay_pid,args.target_relay_pid,args.stage)


if __name__=='__main__':
    try:main()
    except Exception as error:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w',prefix='longhorn-v2-dr-handoff-failure-',
                suffix='.txt',dir='/var/tmp',delete=False) as diagnostic:
            traceback.print_exc(file=diagnostic)
            print('LONGHORN_V2_DR_HANDOFF_FAIL reason='+type(error).__name__+
                ' private_diagnostic='+diagnostic.name,file=sys.stderr)
        sys.exit(1)
