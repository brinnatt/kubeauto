#!/usr/bin/env python3
"""Explicit single-node V1 filesystem beside the independently retained V2 target.

Only the target's owned loop filesystem is temporary. Its original raw disk,
core, namespace, credentials and StorageClass identities remain authoritative.
This fixture proves recovery functionality, never target HA or media performance.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import uuid

import yaml

HOST='192.168.47.149'
NAME='longhorn-node-149'
NS='longhorn-system'
RECORD='longhorn-v1-recovery-target.json'
STORAGE='longhorn-recovery-v1'
HOST_FUNCTION_SHA256='1904d370fee90df03430289a37c6937f89044ae4c23b6025d2898ee7c195106a'


def load(name):
    spec=importlib.util.spec_from_file_location('v1_recovery_'+name.replace('-','_'),Path(__file__).with_name(name))
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def fixture():
    install=load('longhorn-v2-dr-target-install.py');_,lab=install.fixture()
    return install,lab


def desired(original):
    config=yaml.safe_load(original)
    if (config.get('longhorn_data_engines')!=['v2'] or config.get('longhorn_storage_nodes') or
            config.get('longhorn_requests') or set(config.get('longhorn_block_disks',{}))!={HOST}):
        raise ValueError('recovery target requires its idle original raw/V2 profile')
    config.update(longhorn_data_engines=['v1','v2'],longhorn_allow_engine_coexistence=True,
        longhorn_storage_nodes=[HOST],longhorn_data_path='/data/kubeauto-longhorn-coexist46')
    storage=copy.deepcopy(config['longhorn_storage_classes'][0])
    storage.update(name=STORAGE,data_engine='v1',replicas=1,reclaim_policy='Retain',default=False,
        encrypted=False,encryption_secret='',recurring_jobs=[])
    if any(row['name']==STORAGE for row in config['longhorn_storage_classes']):raise ValueError('recovery V1 class already declared')
    config['longhorn_storage_classes'].append(storage)
    return config


class Target:
    def __init__(self,lab,install):
        self.lab,self.install=lab,install
        self.path=lab.DIRECTORY/RECORD
        self.coexist=load('longhorn-v2-coexist.py')
        if hashlib.sha256(inspect.getsource(self.coexist.host_filesystem).encode()).hexdigest()!=HOST_FUNCTION_SHA256:
            raise ValueError('qualified host filesystem interface changed')
        self.upgrade=load('longhorn-upgrade-lifecycle.py')

    def save(self,row):self.lab.write_private(self.path,row)

    def read(self):
        if self.path.is_symlink():raise ValueError('symlink recovery target journal')
        row=json.loads(self.path.read_text());state=self.lab.owner()
        if any(row['identity'][key]!=state[key] for key in ('run_id','cluster_uid','namespace_uid','node_uids')):
            raise ValueError('recovery target core or namespace changed')
        if row['raw_disks']!=state['raw_disks']:
            raise ValueError('recovery target stable raw-disk identity changed')
        self.lab.core_identity()
        raw=self.lab.DIRECTORY/'yml/longhorn/block-disks.json'
        if raw.is_symlink() or hashlib.sha256(raw.read_bytes()).hexdigest()!=row['raw_receipt_sha256']:
            raise ValueError('recovery target raw receipt changed')
        return row

    def host(self,row,action):
        program=inspect.getsource(self.coexist.host_filesystem)+'\nimport json,sys\nprint(json.dumps(host_filesystem(json.load(sys.stdin),sys.argv[1]),sort_keys=True))'
        result=self.lab.remote(HOST,['python3','-c',program,action],stdin=json.dumps(row['host']))
        return json.loads(result.stdout)

    def apply(self,row,content,pid):
        path=self.lab.DIRECTORY/'config.yml'
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() not in (row['original_sha256'],row.get('applied_sha256')):
            raise ValueError('recovery target input changed outside the fixture')
        row['applied_sha256']=hashlib.sha256(content).hexdigest();self.save(row)
        path.write_bytes(content);self.lab.supervise_core(pid,'longhorn-setup')

    def prepare(self,pid):
        lab=self.lab
        if tuple(lab.NODES)!=(HOST,) or self.path.exists() or self.path.is_symlink():raise ValueError('existing or foreign recovery target fixture')
        self.install.health(lab)
        empty=load('longhorn-v1-upgrade-prepare.py');empty.empty(lab,lab.load_helper('longhorn-backups.py'))
        original=(lab.DIRECTORY/'config.yml').read_bytes();config=desired(original)
        if lab.get('storageclass',STORAGE):raise ValueError('foreign recovery StorageClass')
        state=lab.owner();row={'identity':self.upgrade.retained_identity(lab,{}),
            'raw_disks':copy.deepcopy(state['raw_disks']),
            'raw_receipt_sha256':hashlib.sha256((lab.DIRECTORY/'yml/longhorn/block-disks.json').read_bytes()).hexdigest(),
            'original_b64':base64.b64encode(original).decode(),'original_sha256':hashlib.sha256(original).hexdigest(),
            'native_disks':self.coexist.native_disks(lab),'host':{'id':str(uuid.uuid4()),'uuid':str(uuid.uuid4()),
                'machine_id':state['raw_disks'][HOST]['machine_id'],'size':24*1024**3},'stage':'host-intent'}
        self.save(row);self.host(row,'prepare')
        row['stage']='product-intent';self.save(row)
        self.apply(row,yaml.safe_dump(config,sort_keys=False).encode(),pid)
        self.capture(row);self.ready()
        row['stage']='ready';self.save(row)
        print('LONGHORN_V1_RECOVERY_TARGET_READY replicas=1 site_ha=false media=owned-loop-ext4',flush=True)

    def capture(self,row,*,allow_missing_class=False):
        lab=self.lab;native=lab.get('nodes.longhorn.io',NAME,NS);original=row['native_disks'][NAME]
        if not native or native['metadata']['uid']!=original['uid']:raise ValueError('recovery native Node changed')
        disks=native['spec'].get('disks',{})
        if any(disks.get(name)!=value for name,value in original['disks'].items()) or set(disks)-set(original['disks'])!={'kubeauto-data'}:
            raise ValueError('recovery target original raw declaration or V1 disk set differs')
        disk=disks['kubeauto-data'];status=native.get('status',{}).get('diskStatus',{}).get('kubeauto-data',{})
        hardware=self.host(row,'disk-identity')
        if (disk.get('diskType','filesystem')!='filesystem' or disk.get('path')!='/data/kubeauto-longhorn-coexist46' or
                status.get('diskUUID')!=hardware['disk_uuid']):raise ValueError('recovery filesystem/native disk identity differs')
        saved={'node_uid':native['metadata']['uid'],'spec':disk,'uuid':status['diskUUID']}
        if row.get('v1_disk',saved)!=saved:raise ValueError('recovery filesystem disk replaced')
        storage=lab.get('storageclass',STORAGE);product=lab.load_helper('../../roles/longhorn/files/longhorn.py')
        if not storage:
            if allow_missing_class:
                row['v1_disk']=saved;self.save(row);return
            raise ValueError('recovery V1 StorageClass missing')
        product.require_owned(storage)
        if storage.get('parameters',{}).get('dataEngine')!='v1' or storage['parameters'].get('numberOfReplicas')!='1':
            raise ValueError('recovery V1 class differs')
        if row.get('class_uid',storage['metadata']['uid'])!=storage['metadata']['uid']:raise ValueError('recovery class replaced')
        row.update(v1_disk=saved,class_uid=storage['metadata']['uid']);self.save(row)

    def ready(self):
        row=self.read();self.lab.installation_health();self.capture(row)
        for name,value in (('v1-data-engine','true'),('v2-data-engine','true')):
            if self.lab.get('settings.longhorn.io',name,NS).get('value')!=value:raise ValueError('recovery dual-engine setting differs')
        managers=self.lab.get('instancemanagers.longhorn.io',namespace=NS)['items']
        for engine in ('v1','v2'):
            current=[m for m in managers if m['spec'].get('dataEngine','v1')==engine and not m['metadata'].get('deletionTimestamp')]
            if len(current)!=1 or current[0]['spec']['nodeID']!=NAME or current[0].get('status',{}).get('currentState')!='running':
                raise ValueError('recovery target dual-engine manager topology differs')
        state=self.lab.owner()
        current=self.upgrade.retained_identity(self.lab,{})
        if any(current[key]!=row['identity'][key] for key in ('run_id','cluster_uid','namespace_uid','node_uids','credentials','crd_uids')):
            raise ValueError('recovery target original shared identity changed')
        if state['raw_disks'][HOST]['machine_id']!=row['host']['machine_id']:raise ValueError('recovery target machine changed')
        return {'managers':{m['metadata']['name']:{'uid':m['metadata']['uid'],'engine':m['spec'].get('dataEngine','v1')} for m in managers},'v1_disk_uuid':row['v1_disk']['uuid']}

    def reassert(self,pid):
        row=self.read();config=desired(base64.b64decode(row['original_b64']))
        self.apply(row,yaml.safe_dump(config,sort_keys=False).encode(),pid)
        result=self.ready();print('LONGHORN_V1_RECOVERY_TARGET_REASSERTED '+json.dumps(result,sort_keys=True),flush=True)

    def clean(self,pid):
        if not self.path.exists() and not self.path.is_symlink():return
        row=self.read();lab=self.lab
        if (lab.get('volumes.longhorn.io',namespace=NS)['items'] or lab.get('replicas.longhorn.io',namespace=NS)['items'] or
                lab.get('backingimagedatasources.longhorn.io',namespace=NS)['items']):
            raise ValueError('recovery target still has native data consumers')
        native=lab.get('nodes.longhorn.io',NAME,NS);saved=row.get('v1_disk')
        if not saved and native and set(native['spec'].get('disks',{}))-set(row['native_disks'][NAME]['disks']):
            self.capture(row,allow_missing_class=True);saved=row['v1_disk']
        if saved:
            if not native or native['metadata']['uid']!=saved['node_uid']:raise ValueError('recovery native Node replaced before cleanup')
            disk=native['spec'].get('disks',{}).get('kubeauto-data');status=native.get('status',{}).get('diskStatus',{}).get('kubeauto-data',{})
            if disk:
                if disk not in (saved['spec'],dict(saved['spec'],allowScheduling=False)) or status.get('diskUUID')!=saved['uuid'] or any(status.get(k) for k in ('storageScheduled','scheduledReplica','scheduledBackingImage')):
                    raise ValueError('recovery V1 disk changed or still has scheduled data')
                self.coexist.retire_empty_disk(lab,NAME,saved)
            lab.wait('recovery-target-v1-disk-retired',180,lambda:'kubeauto-data' not in lab.get('nodes.longhorn.io',NAME,NS).get('status',{}).get('diskStatus',{}))
        original=base64.b64decode(row['original_b64'],validate=True)
        if hashlib.sha256(original).hexdigest()!=row['original_sha256']:raise ValueError('recovery target original config checksum differs')
        if row.get('applied_sha256'):
            self.apply(row,original,pid)
        storage=lab.get('storageclass',STORAGE)
        if storage:
            if not row.get('class_uid') or storage['metadata']['uid']!=row['class_uid']:raise ValueError('recovery class cleanup UID differs')
            lab.remove(storage);lab.wait('recovery-target-v1-class-retired',120,lambda:lab.get('storageclass',STORAGE) is None)
        state=lab.owner()
        if row.get('applied_sha256'):
            for action in ('check','apply'):
                lab.remote(HOST,['python3','/usr/local/sbin/kubeauto-longhorn-host-check','--release',action,
                    '--data-path','/data/kubeauto-longhorn-coexist46','--cluster-uid',state['cluster_uid'],
                    '--node-uid',state['node_uids'][NAME]])
        self.host(row,'remove')
        if self.coexist.native_disks(lab)!=row['native_disks'] or self.upgrade.retained_identity(lab,{})!=row['identity']:
            raise ValueError('recovery target did not return to its original raw/shared identities')
        self.install.health(lab);self.path.unlink()
        print('LONGHORN_V1_RECOVERY_TARGET_SCOPED_CLEAN_PASS original_raw=true filesystem_fixture=retired site_ha=false',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=('prepare','ready','reassert','clean'))
    parser.add_argument('--relay-pid',type=int,default=0);args=parser.parse_args()
    install,lab=fixture();lab.require_lease();target=Target(lab,install)
    if args.mode=='ready':target.ready()
    else:
        if args.relay_pid<=0:parser.error('a supervised target relay is required')
        getattr(target,args.mode)(args.relay_pid)


if __name__=='__main__':
    try:main()
    except Exception:
        import traceback
        traceback.print_exc();sys.exit(1)
