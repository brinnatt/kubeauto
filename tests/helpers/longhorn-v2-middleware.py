#!/usr/bin/env python3
"""LONGHORN-51: the existing PXC business on explicit V2 raw-disk classes.

Reuse the accepted SQL, dump/independent restore, member recreation and scoped
cleanup implementation. Only engine, disk identity, admission and the owning
V2 installation differ. This does not qualify PXC GA or physical backup/PITR.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import signal
import sys

import yaml

NS = 'longhorn-system'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
RECORD = 'longhorn-v2-middleware.json'
CLASS = 'longhorn-middleware-v2-three-replica'
MARKER = 'LONGHORN_V2_MIDDLEWARE'


def load(name):
    spec=importlib.util.spec_from_file_location('v2_middleware_'+name.replace('-','_'),Path(__file__).with_name(name))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def monitoring():
    return load('longhorn-v2-monitoring.py')


def disk_rows(lab):
    """Verify original admitted hardware and native node/disk UUIDs read-only."""
    state=lab.owner();path=lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if path.is_symlink():raise ValueError('V2 middleware refuses a symlink native receipt')
    receipt=json.loads(path.read_text())
    if (set(state.get('raw_disks',{}))!=set(NODES) or set(receipt.get('nodes',{}))!=set(NODES) or
            any(receipt.get(k)!=state[k] for k in ('cluster_uid','namespace_uid'))):
        raise ValueError('V2 middleware native receipt belongs to another installation')
    rows={}
    for address,entry in receipt['nodes'].items():
        node=lab.get('nodes.longhorn.io',entry['name'],NS);raw=state['raw_disks'][address]
        if (entry['name']!='longhorn-node-'+address.rsplit('.',1)[1] or not node or
                node['metadata']['uid']!=entry['longhorn_node_uid'] or
                state['node_uids'].get(entry['name'])!=entry['kubernetes_node_uid'] or len(entry.get('disks',{}))!=1):
            raise ValueError('V2 middleware native node identity changed')
        name,disk=next(iter(entry['disks'].items()))
        spec=node['spec'].get('disks',{}).get(name,{});status=node['status'].get('diskStatus',{}).get(name,{})
        if (not disk.get('disk_uuid') or disk['disk_uuid']!=status.get('diskUUID') or
                disk.get('path')!=raw['path'] or disk.get('hardware_identity')!=raw['identity'] or
                disk.get('disk_driver')!='aio' or spec.get('diskType')!='block' or spec.get('diskDriver')!='aio' or
                status.get('diskType')!='block' or status.get('diskDriver')!='aio' or
                spec.get('path')!=raw['path'] or not spec.get('allowScheduling') or not node['spec'].get('allowScheduling')):
            raise ValueError('V2 middleware raw hardware, native UUID or scheduling differs')
        rows[entry['name']]={'disk_uuid':disk['disk_uuid'],'hardware_identity':raw['identity'],'path':raw['path'],
            'maximum':status.get('storageMaximum',0),'available':status.get('storageAvailable',0),
            'reserved':spec.get('storageReserved',0),'scheduled':status.get('storageScheduled',0)}
    if len({r['disk_uuid'] for r in rows.values()})!=len(NODES):
        raise ValueError('V2 middleware raw disk UUIDs overlap')
    return rows


def admission(lab,pid):
    module=monitoring();module.verify(lab,pid)
    config=SHARED.desired((lab.DIRECTORY/'config.yml').read_text(),'longhorn-v2-db-admission','longhorn-v2-db-operator-admission')
    planned=SHARED.planned(config)
    placement=module.placement(lab.get('nodes')['items'],lab.get('pods',namespace='*')['items'],planned)
    hosts=module.host_memory(lab);budgets={address:256*1024**2 for address in NODES}
    for pod in planned:
        node=placement['placement'][pod['name']]
        address=next((a for a in NODES if node=='longhorn-node-'+a.rsplit('.',1)[1]),None)
        if address is None:raise ValueError('V2 middleware placement escaped its enabled source nodes')
        budgets[address]+=module.requests(pod['spec'])['memory']
    for address,budget in budgets.items():
        if hosts[address]['MemAvailable']<budget:
            raise ValueError('V2 middleware available memory is below planned requests plus 256Mi stop budget')
    disks=disk_rows(lab);minimal=int(lab.get('settings.longhorn.io','storage-minimal-available-percentage',NS)['value'])
    if not 0<=minimal<=100:raise ValueError('invalid V2 middleware storage watermark')
    if any(min(r['available']-r['maximum']*minimal/100,r['maximum']-r['reserved']-r['scheduled'])<10*1024**3 for r in disks.values()):
        raise ValueError('V2 middleware lacks three two-GiB replicas plus four-GiB raw-disk stop budget')
    SHARED.emit(MARKER+'_ADMISSION',{'placement':placement,'host_memory':hosts,'required_available_bytes':budgets,
        'data_engine':'v2','disks':disks,'boundary':'Existing monitoring and 2Gi hugepages are already charged; PXC sizing is a bounded integration fixture.'})


SHARED=load('longhorn-v1-middleware.py')
SHARED.DATA_ENGINE='v2';SHARED.STORAGE_NODES=NODES;SHARED.CLASS=CLASS;SHARED.RECORD=RECORD
SHARED.monitor=monitoring;SHARED.disk_rows=disk_rows;SHARED.admission=admission


def class_identity(lab,name,uid,replicas):
    sc=lab.get('storageclass',name)
    if (not sc or sc['metadata']['uid']!=uid or sc.get('provisioner')!='driver.longhorn.io' or
            sc.get('parameters',{}).get('dataEngine')!='v2' or
            sc.get('parameters',{}).get('numberOfReplicas')!=str(replicas) or sc.get('reclaimPolicy')!='Delete'):
        raise ValueError('V2 middleware explicit StorageClass engine or persistent identity differs')
    return uid


def monitoring_bindings(lab,row):
    module=monitoring();monitor=SHARED.monitor_identity(lab,row)
    class_identity(lab,module.CLASS,monitor['storage_class_uid'],1)
    result={}
    if len(monitor.get('claims',{}))!=6:raise ValueError('V2 middleware requires all six product monitoring claims')
    for name,saved in monitor['claims'].items():
        claim=lab.get('pvc',name,monitor['namespace']);pv=lab.get('pv',saved['pv'])
        if (not claim or claim['metadata']['uid']!=saved['uid'] or claim['spec'].get('storageClassName')!=module.CLASS or
                claim.get('status',{}).get('phase')!='Bound' or not pv or pv['metadata']['uid']!=saved['pv_uid'] or
                claim['spec'].get('volumeName')!=saved['pv'] or pv['spec'].get('storageClassName')!=module.CLASS or
                pv['spec'].get('claimRef',{}).get('uid')!=saved['uid'] or pv['spec'].get('persistentVolumeReclaimPolicy')!='Delete' or
                pv['spec'].get('csi',{}).get('driver')!='driver.longhorn.io'):
            raise ValueError('V2 middleware monitoring original PVC/PV/class binding differs')
        handle=pv['spec']['csi']['volumeHandle'];volume=lab.get('volumes.longhorn.io',handle,NS)
        if (not volume or volume['spec'].get('dataEngine')!='v2' or volume['spec'].get('numberOfReplicas')!=1 or
                volume.get('status',{}).get('robustness')!='healthy'):
            raise ValueError('V2 middleware monitoring consumer did not select the healthy V2 engine')
        result[name]=dict(saved,volume=handle,volume_uid=volume['metadata']['uid'],data_engine='v2')
    if len({b['volume_uid'] for b in result.values()})!=6:raise ValueError('V2 middleware monitoring volume identities overlap')
    return result


def identity(lab,row):
    lab.core_identity();lab.installation_health();state=lab.owner();disks=disk_rows(lab)
    for key in ('database_namespace','operator_namespace'):
        if not SHARED.namespace_identity(lab,row,key):raise ValueError('V2 middleware original namespace disappeared')
    obj=SHARED.pxc(lab,row);sts=lab.get('statefulset',SHARED.CLUSTER+'-pxc',row['database_namespace'])
    if not obj or not sts or not any(r.get('uid')==row['pxc_uid'] and r.get('controller') for r in sts['metadata'].get('ownerReferences',[])):
        raise ValueError('V2 middleware original database controller ownership differs')
    bindings=SHARED.bindings(lab,row)
    class_identity(lab,CLASS,row['storage_class_uid'],3)
    return {'data_engine':'v2','core':{k:state[k] for k in ('run_id','cluster_uid','namespace_uid','node_uids','raw_disks')},
        'native_block_receipt_sha256':hashlib.sha256((lab.DIRECTORY/'yml/longhorn/block-disks.json').read_bytes()).hexdigest(),
        'disks':{n:{k:r[k] for k in ('disk_uuid','hardware_identity','path')} for n,r in disks.items()},
        'database_uid':row['pxc_uid'],'database_namespace_uid':row['database_namespace_uid'],
        'operator_namespace_uid':row['operator_namespace_uid'],'statefulset_uid':sts['metadata']['uid'],
        'storage_class_uid':row['storage_class_uid'],'database_bindings':{n:{k:b[k] for k in ('claim_uid','pv','pv_uid','volume','volume_uid')} for n,b in bindings.items()},
        'monitoring_namespace_uid':row['monitoring_identity']['monitor_namespace_uid'],
        'monitoring_class_uid':row['monitoring_identity']['storage_class_uid'],'monitoring_bindings':monitoring_bindings(lab,row)}


def refusal_inputs(config):
    """Reach native guards without static replica/class errors or reconciliation."""
    name=CLASS+'-refusal'
    if any(c['name']==name for c in config['longhorn_storage_classes']):
        raise ValueError('V2 middleware refusal class is not unused')
    storage=dict(config['longhorn_storage_classes'][0],name=name,replicas=3,default=False,
        encrypted=False,encryption_secret='',recurring_jobs=[],data_engine='v1',clone_mode='full-copy')
    engine=json.loads(json.dumps(config))
    engine.update(longhorn_data_engines=['v1'],longhorn_storage_nodes=list(NODES),longhorn_v2_nodes=[],
        longhorn_block_disks={},longhorn_storage_classes=[storage],longhorn_v2_cpu_cores=0,
        longhorn_v2_cpu_isolation_enabled=False,longhorn_v2_rebuild_bandwidth_limit=None)
    node=json.loads(json.dumps(config));address=NODES[-1]
    node['longhorn_v2_nodes'].remove(address);node['longhorn_block_disks'].pop(address)
    node['longhorn_default_replica_count']=2
    node['longhorn_storage_classes']=[dict(storage,data_engine='v2',replicas=2)]
    return [('engine-disable',engine,'cannot disable an engine while its volumes remain'),
        ('active-node-disable',node,'cannot disable a V2 node with active native instances')]


def refusals(lab,pid,row):
    original=(lab.DIRECTORY/'config.yml').read_text();config=yaml.safe_load(original)
    cases=refusal_inputs(config)
    if lab.get('storageclass',CLASS+'-refusal'):raise ValueError('V2 middleware refusal class already exists')
    def readback():
        result={}
        for field in ('schema','restored_schema'):
            schema=row[field]
            if not re.fullmatch('[a-z0-9_]+',schema):raise ValueError('invalid middleware refusal SQL schema')
            text=SHARED.sql(lab,row,'SELECT id,HEX(payload) FROM '+schema+'.marker ORDER BY id;').strip()
            if len(text.splitlines())!=1025:raise ValueError('V2 middleware refusal lacks original SQL rows')
            result[field]={'rows':1025,'sha256':hashlib.sha256(text.encode()).hexdigest()}
        return result
    result={}
    for name,invalid,reason in cases:
        before=identity(lab,row);data=readback();content=yaml.safe_dump(invalid,sort_keys=False)
        SHARED.apply(lab,row,content)
        try:
            lab.supervise_core(pid,'longhorn-setup',expected_refusal=reason)
        finally:
            if (lab.DIRECTORY/'config.yml').read_text()!=content:
                raise ValueError('V2 middleware refused input was changed externally; retain journal')
            SHARED.apply(lab,row,original)
            # A rejected setup has already rendered input.json. Restore both
            # source and derived inputs through the same product entry point.
            lab.supervise_core(pid,'longhorn-setup')
        if identity(lab,row)!=before or readback()!=data or lab.get('storageclass',CLASS+'-refusal'):
            raise ValueError('V2 middleware refusal changed original consumers or SQL data')
        result[name]={'reason':reason,'product_entry':'config.yml+kubecli setup longhorn-v2-gate 09',
            'original_identity_preserved':True,'no_refusal_class_created':True,'sql':data}
    SHARED.emit(MARKER+'_REFUSALS',result)
    return result


def cleanup(lab,pid):
    path=lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink():
        row=SHARED.receipt(lab)
        SHARED.apply(lab,row,row['original_input'])
        # Interrupted refusal runs can retain a V1-only derived input even
        # after config.yml was restored. Reconcile before shared health checks.
        lab.supervise_core(pid,'longhorn-setup')
    SHARED.cleanup(lab,pid)


def business(lab,pid):
    row=SHARED.receipt(lab);before=identity(lab,row)
    SHARED.business(lab,pid)
    refusals(lab,pid,SHARED.receipt(lab))
    after=identity(lab,SHARED.receipt(lab))
    if before!=after:raise ValueError('V2 middleware original core, database or monitoring identities changed during recovery')
    SHARED.emit(MARKER+'_IDENTITY',{'data_engine':'v2','before':before,'after':after})
    print(MARKER+'_BUSINESS_PASS',flush=True)


def verify_log(text):
    """The unique SQL verifier plus explicit V2/raw/controller identity proof."""
    SHARED.verify_log(text)
    lines=text.splitlines()
    def one(marker):
        rows=[json.loads(line[len(marker)+1:]) for line in lines if line.startswith(marker+' ')]
        if len(rows)!=1:raise ValueError('V2 middleware lacks one complete observation: '+marker)
        return rows[0]
    proof=one(MARKER+'_IDENTITY');before=proof.get('before',{});after=proof.get('after',{})
    consumers=one('LONGHORN_MIDDLEWARE_PRODUCT_CONSUMERS');persistence=one('LONGHORN_MIDDLEWARE_MEMBER_PERSISTENCE')
    recovery=one('LONGHORN_MIDDLEWARE_NATIVE_LOGICAL_RECOVERY')
    rejected=one(MARKER+'_REFUSALS')
    expected_refusals={'engine-disable':'cannot disable an engine while its volumes remain',
        'active-node-disable':'cannot disable a V2 node with active native instances'}
    if set(rejected)!=set(expected_refusals):raise ValueError('V2 middleware lacks its two serving-engine native refusals')
    for name,reason in expected_refusals.items():
        r=rejected[name];sql=r.get('sql',{})
        if (r.get('reason')!=reason or r.get('product_entry')!='config.yml+kubecli setup longhorn-v2-gate 09' or
                r.get('original_identity_preserved') is not True or r.get('no_refusal_class_created') is not True or
                set(sql)!={'schema','restored_schema'} or any(v.get('rows')!=1025 or
                not re.fullmatch('[a-f0-9]{64}',v.get('sha256','')) for v in sql.values())):
            raise ValueError('V2 middleware native refusal lacks original identity/SQL restoration')
        for member in persistence['members'].values():
            if (sql['schema']['sha256']!=member['source_sha256'] or sql['restored_schema']['sha256']!=member['restored_sha256']):
                raise ValueError('V2 middleware refusal SQL differs from original three-member readback')
    if (proof.get('data_engine')!='v2' or not before or before!=after or before.get('data_engine')!='v2' or
            before.get('database_uid')!=consumers['database_uid'] or not before.get('storage_class_uid') or
            any(not before.get(k) for k in ('database_namespace_uid','operator_namespace_uid','statefulset_uid','monitoring_namespace_uid','monitoring_class_uid')) or
            before['statefulset_uid']!=persistence['restart']['statefulset_uid'] or
            set(before.get('core',{}).get('raw_disks',{}))!=set(NODES) or
            set(before.get('disks',{}))!={'longhorn-node-'+n.rsplit('.',1)[1] for n in NODES} or
            not re.fullmatch('[a-f0-9]{64}',before.get('native_block_receipt_sha256',''))):
        raise ValueError('V2 middleware lacks its persistent original engine/core/controller boundary')
    expected={n:{k:b[k] for k in ('claim_uid','pv','pv_uid','volume','volume_uid')} for n,b in consumers['database_claims'].items()}
    if before.get('database_bindings')!=expected:raise ValueError('V2 middleware original database binding proof differs')
    monitors=before.get('monitoring_bindings',{})
    if (len(monitors)!=6 or set(monitors)!=set(consumers['monitoring_claims']) or
            len({b.get('volume_uid') for b in monitors.values()})!=6 or
            any(b.get('data_engine')!='v2' or not b.get('volume') or not b.get('volume_uid') or
                any(b.get(k)!=consumers['monitoring_claims'][n].get(k) for k in ('uid','pv','pv_uid')) for n,b in monitors.items())):
        raise ValueError('V2 middleware monitoring claims lack explicit original V2 volume bindings')
    for node,disk in before['disks'].items():
        raw=before['core']['raw_disks'][next(n for n in NODES if node=='longhorn-node-'+n.rsplit('.',1)[1])]
        if not disk.get('disk_uuid') or disk.get('hardware_identity')!=raw.get('identity') or disk.get('path')!=raw.get('path'):
            raise ValueError('V2 middleware original raw hardware/disk UUID proof differs')
    if lines.count(MARKER+'_BUSINESS_PASS')!=1:raise ValueError('V2 middleware business completion is missing')
    result={'data_engine':'v2','database_uid':before['database_uid'],'storage_class_uid':before['storage_class_uid'],
        'bindings':consumers['database_claims'],'monitoring_bindings':monitors,'native_logical_recovery':recovery,
        'member_persistence':persistence,'original_identity':before,'refusals':rejected}
    print(MARKER+'_LOG_VERIFIED',flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('database-empty','baseline','admission','setup','business','cleanup','verify'))
    parser.add_argument('--relay-pid',type=int,default=0);parser.add_argument('--log',type=Path)
    args=parser.parse_args()
    if args.action=='verify':
        if not args.log:parser.error('verify requires the owning business log')
        verify_log(args.log.read_text());return
    if args.relay_pid<=0:parser.error('a supervised fixed V2 Registry relay is required')
    _,lab=load('longhorn-v2-install.py').fixture();lab.require_lease()
    if tuple(lab.NODES)!=NODES or lab.CLUSTER!='longhorn-v2-gate' or lab.DIRECTORY!=lab.BASE/'clusters/longhorn-v2-gate':
        raise ValueError('V2 middleware cannot use another cluster or core directory')
    if args.action=='business':business(lab,args.relay_pid)
    elif args.action=='admission':admission(lab,args.relay_pid)
    else:
        if args.action=='cleanup':cleanup(lab,args.relay_pid)
        else:getattr(SHARED,args.action.replace('-','_'))(lab,args.relay_pid)
        if args.action in ('setup','cleanup'):
            print(MARKER+('_SETUP_PASS' if args.action=='setup' else '_CONSUMERS_SCOPED_CLEAN_PASS'),flush=True)


if __name__=='__main__':
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda signum,frame:sys.exit(128+signum))
    main()
