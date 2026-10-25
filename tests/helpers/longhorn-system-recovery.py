#!/usr/bin/env python3
"""LONGHORN-17: independent V1 system recovery and existing-volume protection.

The one-replica source is deliberate for a one-node recovery target. Neither
the source bundle time nor target provisioning is an atomic data point or HA
claim. Only independently read backup blocks and restored application bytes
identify the accepted recovery point.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import sys

import yaml

NS='longhorn-system'
RECORD='longhorn-system-recovery.json'


def load(name):
    spec=importlib.util.spec_from_file_location('system17_'+name.replace('-','_'),Path(__file__).with_name(name))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def supervise_target_refusal(lab,pid,step,reason):
    """Installed target operations retain the exact product and relay boundary."""
    if step!='longhorn-setup' or not reason or tuple(lab.NODES)!=('192.168.47.149',):
        raise ValueError('refusal supervision is restricted to installed target operations')
    lab.core_identity()
    identity=lab.process_identity(pid)
    target=lab.BASE/'tests/helpers/longhorn-v2-dr-target.py'
    if not identity or identity[0][-2:]!=[str(target).encode(),b'relay-maintain']:
        raise ValueError('installed target operation requires its exact Registry supervisor')
    command=[sys.executable,str(lab.BASE/'kubecli.py'),'setup',lab.CLUSTER,'09']
    with tempfile.TemporaryFile(mode='w+') as output:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,start_new_session=True,
                                 stdout=output,stderr=subprocess.STDOUT)
        started=heartbeat=time.monotonic()
        with lab.scoped_cleanup(lambda:lab.stop_product(process,command)):
            while process.poll() is None:
                if lab.process_identity(pid)!=identity:raise ValueError('target Registry supervisor exited')
                now=time.monotonic()
                if now-started>=7200:raise TimeoutError('installed target product step exceeded two hours')
                if now-heartbeat>=15:
                    print('LONGHORN_V2_DR_TARGET_HEARTBEAT step='+step+' elapsed='+str(int(now-started)),flush=True)
                    heartbeat=now
                time.sleep(1)
            output.seek(0);observed=output.read()
            marker='LONGHORN_PRODUCT_FAIL reason='+reason
            if not process.returncode or marker not in observed:
                diagnostic=lab.DIRECTORY/('unexpected-installed-refusal-'+secrets.token_hex(12)+'.json')
                lab.write_private(diagnostic,{'rc':process.returncode,'output':observed})
                raise ValueError('installed target product did not return its exact expected refusal')
    print('LONGHORN_EXPECTED_PRODUCT_REFUSAL_VERIFIED',flush=True)


def fixture():
    bridge=load('longhorn-v1-upgrade-lifecycle.py')
    source,target,backups,shared=bridge.fixture()
    initial=target.supervise_core
    def step(pid,name,*,expected_refusal=None):
        if expected_refusal is None:return initial(pid,name)
        return supervise_target_refusal(target,pid,name,expected_refusal)
    target.supervise_core=step
    return source,target,backups,shared


def manifest(obj):
    """Copy declarations into another cluster without source metadata/status."""
    result={key:copy.deepcopy(obj[key]) for key in ('apiVersion','kind','spec')}
    result['metadata']={key:obj['metadata'][key] for key in ('name','namespace') if key in obj['metadata']}
    return result


def configuration(holder):
    binding=holder.receipt()['product_binding']
    return {'longhorn_backup_target':binding['fixture_spec']['backupTargetURL'],
            'longhorn_backup_secret':binding['secret_name']}


def validate_skipped(before,current,bundle_url,restore):
    helper=load('longhorn-v2-systemrestore.py')
    for kind,old in before.items():
        obj=current.get(kind)
        if not obj or helper.resource_signature(obj)!=helper.resource_signature(old):
            raise ValueError('existing target '+kind+' identity or declaration changed')
        annotations=obj['metadata'].get('annotations',{})
        if annotations.get('longhorn.io/last-skipped-system-restored')!=bundle_url or not annotations.get('longhorn.io/last-skipped-system-restored-at'):
            raise ValueError('existing target '+kind+' lacks current skipped-rollout provenance')
        if helper.timestamp(annotations['longhorn.io/last-skipped-system-restored-at'])<helper.timestamp(restore['metadata']['creationTimestamp']):
            raise ValueError('existing target skipped annotation predates this restore')
    return {kind:obj['metadata']['uid'] for kind,obj in current.items()}


class Recovery:
    def __init__(self,source,target,backups,shared,source_pid,target_pid):
        self.source,self.target,self.backups=source,target,backups
        self.source_pid,self.target_pid=source_pid,target_pid
        self.upgrade=load('longhorn-upgrade-lifecycle.py');self.restore=load('longhorn-v2-systemrestore.py')
        self.bridge=load('longhorn-v1-upgrade-lifecycle.py')
        self.target_module=load('longhorn-v1-recovery-target.py')
        self.target_fixture=self.target_module.Target(target,load('longhorn-v2-dr-target-install.py'))
        self.probe=self.restore.Probe(source,data_engine='v1',replicas=1)
        self.path=source.DIRECTORY/RECORD;self.probe.record=self.path
        self.store=self.probe.store;self.bundle=self.probe.bundle
        self.consumer=shared.Consumer(target,self.store,backups)
        self.workloads={key:self.upgrade.Workload(source,'system17-source-'+key) for key in ('missing','existing')}
        self.receivers={key:self.upgrade.Workload(target,'system17-target-'+key) for key in ('missing','existing')}
        self.probe.business_record=self.workloads['missing'].path
        self.row=None

    def save(self):self.source.write_private(self.path,self.row)

    def read(self):
        if self.path.is_symlink():raise ValueError('symlink independent system recovery journal')
        self.row=json.loads(self.path.read_text())
        for lab,key in ((self.source,'source_identity'),(self.target,'target_identity')):
            lab.core_identity();state=lab.owner()
            if any(self.row[key][field]!=state[field] for field in ('run_id','cluster_uid','namespace_uid','node_uids')):
                raise ValueError('independent system recovery baseline identity changed')
        return self.row

    def graph(self,lab,journal):
        return {kind:lab.get(kind,name,namespace) for kind,name,namespace in (
            ('Volume',journal['volume'],NS),('PersistentVolume',journal['pv'],''),
            ('PersistentVolumeClaim','data',journal['namespace']))}

    def source_class(self):
        lab=self.source;config=yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
        if config.get('longhorn_data_engines',['v1'])!=['v1'] or lab.get('storageclass',self.target_module.STORAGE):
            raise ValueError('independent source requires its original V1 profile and a fresh dedicated class')
        storage=copy.deepcopy(config['longhorn_storage_classes'][0])
        storage.update(name=self.target_module.STORAGE,data_engine='v1',replicas=1,default=False,
            encrypted=False,encryption_secret='',reclaim_policy='Retain',recurring_jobs=[])
        self.row['source_class']=storage;self.save()
        lab.run_requests(self.source_pid,[],storage_classes=config['longhorn_storage_classes']+[storage])
        current=lab.get('storageclass',storage['name']);product=lab.load_helper('../../roles/longhorn/files/longhorn.py')
        product.require_owned(current)
        if not current or current.get('parameters',{}).get('numberOfReplicas')!='1' or current['parameters'].get('dataEngine')!='v1':
            raise ValueError('source dedicated one-replica V1 class did not converge')
        self.row['source_class_uid']=current['metadata']['uid'];self.save()

    def namespace(self,workload,original):
        lab=self.target;name=original['namespace']
        if lab.get('namespace',name) or workload.path.exists() or workload.path.is_symlink():
            raise ValueError('independent target namespace or business journal already exists')
        row={'namespace':name,'namespace_uid':None,'objects':{},'storage_class':self.target_module.STORAGE,
             'reclaim_policy':'Retain','volume':original['volume'],'pv':original['pv']}
        workload.save(row)
        namespace=json.loads(lab.kubectl('create','-f','-','-o','json',stdin=json.dumps({
            'apiVersion':'v1','kind':'Namespace','metadata':{'name':name}})).stdout)
        row['namespace_uid']=namespace['metadata']['uid'];workload.save(row)
        row['defaults']=lab.wait('system17-target-namespace-defaults',60,lambda:lab.namespace_defaults(name));workload.save(row)
        return row

    def seed_existing(self):
        """Different target bytes/UIDs under the source bundle's existing name."""
        source_journal=self.workloads['existing'].journal();workload=self.receivers['existing'];lab=self.target
        row=self.namespace(workload,source_journal);source_graph=self.graph(self.source,source_journal)
        for kind,obj in source_graph.items():
            if lab.get(kind,obj['metadata']['name'],obj['metadata'].get('namespace','')):
                raise ValueError('target same-name seed has an existing foreign object')
        native=manifest(source_graph['Volume']);native['spec'].update(nodeID='',fromBackup='',numberOfReplicas=1,dataEngine='v1')
        native['spec']['image']=lab.get('settings.longhorn.io','default-engine-image',NS)['value']
        self.row['existing_native_intent']=native['metadata']['name'];self.save()
        obj=json.loads(lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(native)).stdout)
        row['volume_uid']=obj['metadata']['uid'];workload.save(row)
        persistent=manifest(source_graph['PersistentVolume']);persistent['spec'].pop('claimRef',None)
        claim=manifest(source_graph['PersistentVolumeClaim'])
        for item in (persistent,claim):
            created=workload.create(item);row=workload.journal()
            if item['kind']=='PersistentVolume':row['pv_uid']=created['metadata']['uid'];workload.save(row)
        lab.wait('system17-target-existing-bound',600,lambda:(lab.get('pvc','data',row['namespace']) or {}).get('status',{}).get('phase')=='Bound')
        row=workload.journal();lab.bind_business_volume(row,workload.path);workload.mount('writer',0)
        payload=self.row['id']+'-target-existing-owned';self.row['existing_payload']=payload
        self.row['existing_digest']=workload.data(payload);self.save()
        target_graph=self.graph(lab,workload.journal())
        if any(target_graph[k]['metadata']['uid']==source_graph[k]['metadata']['uid'] for k in source_graph):
            raise ValueError('independent existing target adopted a source UID')
        return target_graph

    def discover(self):
        dr=load('longhorn-v2-disaster.py');dr.sync_target(self.target,self.consumer.receipt()['product_binding'])
        name=self.row['bundle_expected']['name']
        def ready():
            obj=self.target.get('systembackups.longhorn.io',name,NS)
            if not obj:return None
            if obj.get('status',{}).get('version')!='v1.12.1':raise ValueError('independent bundle discovery version differs')
            return obj if obj['status'].get('state')=='Ready' else None
        obj=self.target.wait('system17-independent-system-bundle-discovery',600,ready)
        self.row['target_bundle_uid']=obj['metadata']['uid'];self.save()
        backup=self.row['backup_request']
        obj=self.target.wait('system17-independent-volume-backup-discovery',600,lambda:
            dr.discovered_backup(self.target,backup['name'],self.row['original']['Volume']['metadata']['name'],self.row['backup_url']))
        self.row['target_backup_uid']=obj['metadata']['uid'];self.save()

    def refuse_attached(self):
        lab=self.target;workload=self.receivers['existing'];before=workload.identities()
        request={'kind':'system-restore','name':'attached-refusal-'+secrets.token_hex(12),'source':self.row['bundle_expected']['name']}
        lab.run_requests(self.target_pid,[request],backup_configuration=configuration(self.consumer),
            expected_refusal='system restore requires all volumes detached')
        if lab.get('systemrestores.longhorn.io',namespace=NS)['items'] or workload.identities()!=before:
            raise ValueError('attached target restore refusal created a request or changed its binding')
        workload.data(self.row['existing_payload'],read=True)
        self.row['attached_refused']=True;self.save()
        print('LONGHORN_SYSTEM_RESTORE_ATTACHED_REFUSAL_PASS target_uid_unchanged=true',flush=True)

    def verify_materials(self):
        row=self.row
        if (self.probe.read_bundle(row)!=row['bundle_result'] or self.store.backup_receipt(
                row['original']['Volume']['metadata']['name'],row['backup_request']['name'],2*1024**3)!=row['backup_receipt']):
            raise ValueError('independent SystemRestore external recovery material changed')

    def restore_target(self):
        lab=self.target;existing=self.receivers['existing'];existing.detach()
        self.row['skip_before']=self.graph(lab,existing.journal());self.save()
        missing=self.receivers['missing'];self.namespace(missing,self.workloads['missing'].journal())
        if any(self.graph(lab,missing.journal()).values()):raise ValueError('missing target resources exist before system restore')
        self.row.update(restore_request={'kind':'system-restore','name':'independent-'+secrets.token_hex(12),
            'source':self.row['bundle_expected']['name']},stage='restoring');self.save()
        lab.run_requests(self.target_pid,[self.row['restore_request']],backup_configuration=configuration(self.consumer))
        obj=lab.get('systemrestores.longhorn.io',self.row['restore_request']['name'],NS)
        product=lab.load_helper('../../roles/longhorn/files/longhorn.py');product.check_request(obj,product.request_object(self.row['restore_request']))
        if not obj or obj.get('status',{}).get('state')!='Completed':raise ValueError('independent SystemRestore did not complete')
        self.row['restore']=obj;self.save()
        job=lab.get('job','longhorn-system-rollout-'+obj['metadata']['name'],NS)
        pods=[p for p in lab.get('pods',namespace=NS)['items'] if any(o.get('uid')==(job or {}).get('metadata',{}).get('uid') for o in p['metadata'].get('ownerReferences',[]))]
        self.row.update(rollout=self.restore.validate_rollout(obj,job,pods),job=job,rollout_pods=pods);self.save()
        def reconstructed():
            return self.restore.reconstruction_ready(self.row['original'],self.graph(lab,missing.journal()),obj,
                self.row['backup_url'],self.row['bundle_url'],data_engine='v1',replicas=1)
        current=lab.wait('system17-target-reconstructed-binding',900,reconstructed)
        identities=self.restore.validate_reconstruction(self.row['original'],current,obj,
            self.row['backup_url'],self.row['bundle_url'],data_engine='v1',replicas=1)
        journal=missing.journal();journal.update(volume_uid=identities['Volume'],pv_uid=identities['PersistentVolume'])
        journal['objects']['PersistentVolumeClaim/data']=identities['PersistentVolumeClaim'];journal['objects']['PersistentVolume/'+journal['pv']]=identities['PersistentVolume']
        missing.save(journal)
        self.row['reconstructed_uids']=identities
        self.row['skipped_uids']=validate_skipped(self.row['skip_before'],self.graph(lab,existing.journal()),self.row['bundle_url'],obj)
        self.save()
        # Product operation must already have reapplied the target's profile.
        # Reassert checks the same documented product path, never direct Helm.
        self.target_fixture.reassert(self.target_pid)
        for key,payload in (('missing',self.row['payload']),('existing',self.row['existing_payload'])):
            workload=self.receivers[key];workload.mount('recovered-reader',0)
            digest=workload.data(payload,read=True);workload.detach();workload.mount('recovered-remount',0)
            if workload.data(payload,read=True)!=digest:raise ValueError('independent target remount data differs')
            self.row.setdefault('target_readback',{})[key]=digest;self.save()
        for key,workload in self.workloads.items():workload.data(self.row['source_latest'][key],read=True)
        self.verify_materials()
        self.row['stage']='verified';self.save()
        print('LONGHORN_SYSTEM_RESTORE_INDEPENDENT_OBSERVED '+json.dumps({
            'source_cluster_uid':self.row['source_identity']['cluster_uid'],'target_cluster_uid':self.row['target_identity']['cluster_uid'],
            'backup':self.row['backup_request']['name'],'reconstructed_uids':identities,'skipped_uids':self.row['skipped_uids'],
            'application_sha256':self.row['target_readback'],'bundle':self.row['bundle_result'],
            'replicas':1,'target_ha':False,'bundle_is_atomic_data_point':False},sort_keys=True),flush=True)

    def business(self):
        if self.path.exists() or self.path.is_symlink():raise ValueError('recover the existing independent SystemRestore first')
        gate=load('longhorn-v1-upgrade-prepare.py')
        for lab,engine in ((self.source,'v1'),(self.target,'v2')):
            gate.healthy(lab,engine,'1.12.1');gate.empty(lab,self.backups)
        if self.source.owner()['cluster_uid']==self.target.owner()['cluster_uid']:raise ValueError('system recovery target is not independent')
        self.row={'id':secrets.token_hex(16),'source_identity':self.upgrade.retained_identity(self.source,{}),
            'target_identity':self.upgrade.retained_identity(self.target,{}),'stage':'preparing',
            'config_sha256':{side:hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()
                for side,lab in (('source',self.source),('target',self.target))}};self.save()
        self.target_fixture.prepare(self.target_pid);self.source_class()
        self.store.prepare();self.store.qualify()
        source_configuration=self.store.product_configuration()
        self.source.run_requests(self.source_pid,[],backup_configuration=source_configuration)
        self.row.update(self.bundle.identity());self.save()
        for key,workload in self.workloads.items():
            workload.prepare(self.target_module.STORAGE)
            payload=self.row['id']+'-source-'+key;workload.data(payload)
            self.row.setdefault('source_latest',{})[key]=payload;self.save()
        self.probe.protect(self.row,self.workloads['missing'].journal(),self.source_pid,source_configuration)
        self.row['source_latest']['missing']=self.row['post_payload']
        extra=self.graph(self.source,self.workloads['existing'].journal())
        self.row['bundle_expected']['resource_uids'] += [[kind,obj['metadata'].get('namespace',''),obj['metadata']['name'],obj['metadata']['uid']] for kind,obj in extra.items()]
        self.row['bundle_result']=self.probe.read_bundle(self.row);self.save()
        self.consumer.initialize();self.consumer.qualify(self.target_pid);self.discover()
        self.seed_existing();self.refuse_attached();self.restore_target()

    def resume_prepared(self):
        """Reuse only the fully prepared, never-started SystemRestore checkpoint."""
        self.read();row=self.row
        required={'source_class_uid','target_bundle_uid','target_backup_uid','existing_payload',
                  'existing_digest','backup_receipt','bundle_result','source_latest'}
        forbidden={'attached_refused','restore_request','restore','skip_before','reconstructed_uids'}
        if row['stage']!='preparing' or not required<=set(row) or forbidden&set(row):
            raise ValueError('prepared recovery requires the exact pre-refusal checkpoint')
        fixture=self.target_fixture.read();self.target_fixture.ready()
        for lab,side,expected in ((self.source,'source',row['config_sha256']['source']),
                (self.target,'target',fixture['applied_sha256'])):
            lab.installation_health()
            if hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()!=expected:
                raise ValueError('prepared recovery product input changed')
            if side=='target' and fixture['original_sha256']!=row['config_sha256']['target']:
                raise ValueError('prepared recovery original target input changed')
            if lab.get('systemrestores.longhorn.io',namespace=NS)['items']:
                raise ValueError('prepared recovery refuses any started SystemRestore')
        storage=self.source.get('storageclass',row['source_class']['name'])
        if not storage or storage['metadata']['uid']!=row['source_class_uid']:
            raise ValueError('prepared recovery source class changed')
        for kind,name,key in (('systembackups.longhorn.io',row['bundle_expected']['name'],'target_bundle_uid'),
                ('backups.longhorn.io',row['backup_request']['name'],'target_backup_uid')):
            current=self.target.get(kind,name,NS)
            if not current or current['metadata']['uid']!=row[key] or current.get('status',{}).get('state') not in ('Ready','Completed'):
                raise ValueError('prepared recovery target material changed')
        for key,workload in self.workloads.items():
            workload.identities();workload.data(row['source_latest'][key],read=True)
        existing=self.receivers['existing'];existing.identities()
        if existing.data(row['existing_payload'],read=True)!=row['existing_digest']:
            raise ValueError('prepared recovery existing target bytes changed')
        missing=self.receivers['missing']
        if missing.path.exists() or missing.path.is_symlink():
            raise ValueError('prepared recovery missing-target journal already exists')
        expected_source={w.journal()['volume'] for w in self.workloads.values()}
        expected_target={existing.journal()['volume']}
        for lab,expected in ((self.source,expected_source),(self.target,expected_target)):
            if {v['metadata']['name'] for v in lab.get('volumes.longhorn.io',namespace=NS)['items']}!=expected:
                raise ValueError('prepared recovery refuses unowned native volumes')
        self.verify_materials()
        print('LONGHORN_SYSTEM_RESTORE_PREPARED_CHECKPOINT_VERIFIED original_materials=true',flush=True)
        self.refuse_attached();self.restore_target()

    def retire_caches(self,lab,holder,side):
        self.row['points']={'missing':{'name':self.row['backup_request']['name'],'volume':self.row['original']['Volume']['metadata']['name'],
            'backup_uid':self.row['backup']['metadata']['uid'],'target_backup_uid':self.row['target_backup_uid'],
            'receipt':self.row['backup_receipt'],'url':self.row['backup_url']}}
        self.upgrade.Upgrade.retire_caches(self,lab,holder,side)

    def clean_prebusiness(self):
        """Retire only a failed host fixture before any recovery material exists."""
        expected={'id','source_identity','target_identity','stage','config_sha256'}
        if set(self.row)!=expected:
            raise ValueError('prebusiness recovery journal contains later operation intent')
        target=self.target_fixture.read()
        product_intent=target['stage']=='product-intent'
        if product_intent:
            original=base64.b64decode(target['original_b64'],validate=True)
            desired=yaml.safe_dump(self.target_module.desired(original),sort_keys=False).encode()
            if (hashlib.sha256(original).hexdigest()!=self.row['config_sha256']['target'] or
                    target['original_sha256']!=self.row['config_sha256']['target'] or
                    target.get('applied_sha256')!=hashlib.sha256(desired).hexdigest() or
                    self.target_fixture.coexist.native_disks(self.target)!=target['native_disks'] or
                    'v1_disk' in target or 'class_uid' in target):
                raise ValueError('prebusiness recovery target product intent has unverified state')
        elif target['stage']!='host-intent' or 'applied_sha256' in target:
            raise ValueError('prebusiness recovery target has product operation intent')
        gate=load('longhorn-v1-upgrade-prepare.py')
        for lab,engine,side in ((self.source,'v1','source'),(self.target,'v2','target')):
            if side=='source' or not product_intent:gate.healthy(lab,engine,'1.12.1')
            gate.empty(lab,self.backups)
            expected_sha=target['applied_sha256'] if side=='target' and product_intent else self.row['config_sha256'][side]
            if (hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()!=expected_sha or
                    self.upgrade.retained_identity(lab,{})!=self.row[side+'_identity'] or
                    lab.get('storageclass',self.target_module.STORAGE)):
                raise ValueError('prebusiness recovery original configuration or identity changed')
        self.target_fixture.clean(self.target_pid)
        for lab,side in ((self.source,'source'),(self.target,'target')):
            lab.installation_health()
            if (self.upgrade.retained_identity(lab,{})!=self.row[side+'_identity'] or
                    hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()!=self.row['config_sha256'][side]):
                raise ValueError('prebusiness recovery baseline identity was not restored')
        archive=self.path.with_name('longhorn-system-recovery-prebusiness-cleaned-'+self.row['id']+'.json')
        self.source.write_private(archive,self.row);self.path.unlink()
        print('LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS source=retained target_raw=retained site_ha=false business=not-started',flush=True)

    def clean(self):
        if not self.path.exists() and not self.path.is_symlink():return
        self.read()
        if self.row['stage']=='preparing':
            self.clean_prebusiness();return
        if self.row['stage']!='verified':raise ValueError('unverified independent SystemRestore retains all source/target recovery materials')
        target=self.target;restore=self.row['restore'];name=restore['metadata']['name']
        current=target.get('systemrestores.longhorn.io',name,NS)
        if current:
            if current['metadata']['uid']!=restore['metadata']['uid'] or current.get('status',{}).get('state')!='Completed':
                raise ValueError('independent restore identity changed before cleanup')
            job=target.get('job',self.row['job']['metadata']['name'],NS)
            pods=[target.get('pod',p['metadata']['name'],NS) for p in self.row['rollout_pods']]
            if not job or job['metadata']['uid']!=self.row['rollout']['job_uid'] or any(p is None for p in pods):
                raise ValueError('independent restore child identity missing before cleanup')
            self.restore.validate_rollout(current,job,pods)
            target.remove(current)
        for obj in (self.row['job'],*self.row['rollout_pods']):
            def gone(obj=obj):
                current=target.get(obj['kind'],obj['metadata']['name'],NS)
                if current and current['metadata']['uid']!=obj['metadata']['uid']:
                    raise ValueError('independent restore child replaced during cleanup')
                return current is None
            target.wait('system17-rollout-child-clean',300,gone)
        for workload in self.receivers.values():workload.cleanup()
        self.bundle.cleanup()
        cache=target.get('systembackups.longhorn.io',self.row['bundle_expected']['name'],NS)
        if cache:
            if cache['metadata']['uid']!=self.row['target_bundle_uid']:raise ValueError('independent target SystemBackup cache replaced')
            target.remove(cache);target.wait('system17-system-cache-retired',300,lambda:target.get('systembackups.longhorn.io',cache['metadata']['name'],NS) is None)
        self.retire_caches(target,self.consumer,'target');self.consumer.cleanup()
        self.retire_caches(self.source,self.store,'source')
        snapshot=self.row['snapshot'];current=self.source.get('snapshots.longhorn.io',snapshot['metadata']['name'],NS)
        if current:
            if current['metadata']['uid']!=snapshot['metadata']['uid']:raise ValueError('system17 source snapshot replaced')
            self.source.remove(current)
        for workload in self.workloads.values():workload.cleanup()
        self.source.wait('system17-source-snapshot-retired',300,lambda:
            self.source.get('snapshots.longhorn.io',snapshot['metadata']['name'],NS) is None)
        self.store.cleanup()
        storage=self.source.get('storageclass',self.row['source_class']['name'])
        if storage:
            if storage['metadata']['uid']!=self.row['source_class_uid']:raise ValueError('system17 dedicated source class replaced')
            self.source.remove(storage)
            self.source.wait('system17-source-class-retired',120,lambda:self.source.get('storageclass',self.row['source_class']['name']) is None)
        self.target_fixture.clean(self.target_pid)
        for lab,key in ((self.source,'source_identity'),(self.target,'target_identity')):
            lab.installation_health()
            if self.upgrade.retained_identity(lab,{})!=self.row[key]:raise ValueError('system17 original source/target identity changed')
            side='source' if lab is self.source else 'target'
            if hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()!=self.row['config_sha256'][side]:
                raise ValueError('system17 original product configuration changed')
        result=self.path.with_name('longhorn-system-recovery-result-'+self.row['id']+'.json')
        self.row['stage']='complete';self.source.write_private(result,self.row);self.path.unlink()
        print('LONGHORN_SYSTEM_RESTORE_SCOPED_CLEAN_PASS source=retained target_raw=retained site_ha=false',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('business','resume-prepared','clean'))
    parser.add_argument('--source-relay-pid',type=int,required=True);parser.add_argument('--target-relay-pid',type=int,required=True)
    args=parser.parse_args();source,target,backups,shared=fixture();source.require_lease();target.require_lease()
    if min(args.source_relay_pid,args.target_relay_pid)<=0:parser.error('both supervised site relays are required')
    scenario=Recovery(source,target,backups,shared,args.source_relay_pid,args.target_relay_pid)
    if args.mode=='clean':scenario.clean()
    else:
        if args.mode=='resume-prepared':scenario.resume_prepared()
        else:scenario.business()
        scenario.clean();print('LONGHORN_SYSTEM_RESTORE_PASS',flush=True)


if __name__=='__main__':
    try:main()
    except Exception:
        import traceback
        traceback.print_exc();sys.exit(1)
