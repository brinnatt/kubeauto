#!/usr/bin/env python3
"""AIO disk retirement preserves media and releases exact native/host identities.

Pinned manager 8320238 removes DiskStatus even after DiskDelete error. Require
independent SPDK state, never infer successful native release from CR absence.
Pinned engine d19dbb3abcea AIO DiskDelete calls BdevAioDelete, not lvstore wipe.
"""
import copy
import hashlib
import inspect
import json
from pathlib import Path
import re
import tempfile

NS='longhorn-system'


def atomic(path,obj):
    if path.is_symlink(): raise ValueError('symlink block retirement state')
    with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False) as stream:
        json.dump(obj,stream,sort_keys=True);stream.flush()
        import os
        os.fsync(stream.fileno());temporary=Path(stream.name)
    temporary.chmod(0o600);temporary.replace(path)


def validate(c):
    if c.get('longhorn_version')!='1.12.1' or 'v2' not in c.get('longhorn_data_engines',[]):
        raise ValueError('AIO block retirement requires Longhorn 1.12.1 with V2 enabled')
    rows=c.get('longhorn_retire_block_disks',[])
    operation=c.get('longhorn_block_retirement_id','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',operation) or not isinstance(rows,list) or not rows:
        raise ValueError('block retirement requires explicit unique operation ID and disk approvals')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'node','name','disk_uuid'}:
            raise ValueError('each block retirement approval requires node, name and disk_uuid')
        if not all(isinstance(v,str) and v for v in row.values()): raise ValueError('empty block retirement identity')
        key=(row['node'],row['name'])
        if key in seen: raise ValueError('duplicate block retirement approval')
        seen.add(key)
        declared=[d for d in c.get('longhorn_block_disks',{}).get(row['node'],[]) if d['name']==row['name']]
        if (len(declared)!=1 or declared[0]['disk_driver']!='aio' or
                declared[0].get('allow_scheduling') is not False or declared[0].get('eviction_requested') is not True):
            raise ValueError('retirement requires declared AIO disk with scheduling disabled and eviction requested')
    return rows


def rpc_snapshot():
    import json
    import os
    from pathlib import Path
    import socket
    process=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            args=(path/'cmdline').read_bytes().split(b'\0')
            if args and Path(os.fsdecode(args[0])).name=='spdk_tgt':
                process.append({'pid':int(path.name),'start_time':(path/'stat').read_text().rsplit(')',1)[1].split()[19]})
        except FileNotFoundError:continue
    if len(process)!=1:raise ValueError('retirement requires one native SPDK target')
    def query(method,index):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(10);sock.connect('/var/tmp/spdk.sock')
            sock.sendall(json.dumps({'jsonrpc':'2.0','id':index,'method':method}).encode()+b'\n')
            data=b''
            while True:
                part=sock.recv(65536)
                if not part:raise ValueError('SPDK state query closed before response')
                data+=part
                if len(data)>16*1024**2:raise ValueError('SPDK state response exceeds bound')
                try:response=json.loads(data);break
                except json.JSONDecodeError:continue
        if response.get('id')!=index or 'error' in response or not isinstance(response.get('result'),list):
            raise ValueError('SPDK state query failed: '+method)
        return response['result']
    result={'process':process[0],'bdevs':query('bdev_get_bdevs',1),
        'lvstores':query('bdev_lvol_get_lvstores',2),'controllers':query('bdev_nvme_get_controllers',3)}
    for bdev in result['bdevs']:
        aio=bdev.get('driver_specific',{}).get('aio')
        if aio:
            path=aio.get('filename')
            if not path:raise ValueError('AIO bdev lacks its native device filename')
            aio['resolved_filename']=str(Path(path).resolve(strict=True))
    print(json.dumps(result,sort_keys=True))


def manager(api,node):
    matches=[m for m in api.get('instancemanagers.longhorn.io',namespace=NS)['items']
        if m.get('spec',{}).get('nodeID')==node and m['spec'].get('dataEngine')=='v2']
    if len(matches)!=1:raise ValueError('retirement requires one V2 IM on the approved node')
    im=matches[0];pod=api.get('pod',im['metadata']['name'],NS)
    if (im.get('status',{}).get('currentState')!='running' or not pod or pod['metadata'].get('deletionTimestamp') or
            pod['spec'].get('nodeName')!=node or len(pod['spec'].get('containers',[]))!=1 or
            pod['spec']['containers'][0]['image']!=im['spec']['image'] or not any(
                ref.get('kind')=='InstanceManager' and ref.get('uid')==im['metadata']['uid'] and ref.get('controller')
                for ref in pod['metadata'].get('ownerReferences',[]))):raise ValueError('retirement IM Pod ownership, placement or image changed')
    if not any(x.get('type')=='Ready' and x.get('status')=='True' for x in pod.get('status',{}).get('conditions',[])):
        raise ValueError('retirement IM Pod is not ready')
    return {'name':im['metadata']['name'],'im_uid':im['metadata']['uid'],'pod_uid':pod['metadata']['uid'],'image':im['spec']['image']}


def runtime(api,node,product,expected=None):
    identity=manager(api,node)
    if expected and identity!=expected:raise ValueError('retirement native IM changed; retain ownership')
    code=inspect.getsource(rpc_snapshot)+'\nrpc_snapshot()\n'
    result=json.loads(product.run(api.prefix+['-n',NS,'exec',identity['name'],'-c','instance-manager','--','python3','-c',code]))
    return dict(result,manager=identity)


def peers(snapshot,targets):
    names={t['name'] for t in targets};uuids={t['disk_uuid'] for t in targets}
    aio=[]
    for bdev in snapshot['bdevs']:
        driver=bdev.get('driver_specific',{}).get('aio')
        if driver and bdev['name'] not in names:
            aio.append({'name':bdev['name'],'uuid':bdev.get('uuid'),'filename':driver['resolved_filename']})
    stores=[{key:s[key] for key in ('uuid','name','base_bdev')} for s in snapshot['lvstores'] if s['uuid'] not in uuids]
    controllers=[]
    for ctrl in snapshot['controllers']:
        if 'ctrlrs' in ctrl:
            controllers.append({'name':ctrl['name'],'ctrlrs':sorted([
                {key:item.get(key) for key in ('cntlid','trid','host')} for item in ctrl['ctrlrs']],
                key=lambda x:json.dumps(x,sort_keys=True))})
        else:
            controllers.append({key:ctrl.get(key) for key in ('name','trid','cntlid')})
    return {'aio':sorted(aio,key=lambda x:x['name']),'lvstores':sorted(stores,key=lambda x:x['uuid']),
        'controllers':sorted(controllers,key=lambda x:json.dumps(x,sort_keys=True))}


def assert_runtime(snapshot,targets,*,removed,before=None):
    for target in targets:
        name=target['name'];uid=target['disk_uuid']
        named=[b for b in snapshot['bdevs'] if b['name']==name]
        stores=[s for s in snapshot['lvstores'] if s['uuid']==uid or s['base_bdev']==name or s['name']==name]
        children=[b for b in snapshot['bdevs'] if b.get('driver_specific',{}).get('lvol',{}).get('lvol_store_uuid')==uid]
        if any(ctrl['name']==name for ctrl in snapshot['controllers']):raise ValueError('AIO disk collides with an NVMe controller')
        if children:raise ValueError('retirement refuses native lvol dependencies')
        if removed:
            aliases=[b for b in snapshot['bdevs'] if b.get('driver_specific',{}).get('aio',{}).get('resolved_filename')==target['resolved_path']]
            if named or stores or aliases:return False
        elif (len(named)!=1 or named[0].get('product_name')!='AIO disk' or len(stores)!=1 or
                stores[0]['uuid']!=uid or stores[0]['base_bdev']!=name or
                named[0].get('driver_specific',{}).get('aio',{}).get('resolved_filename')!=target['resolved_path']):
            raise ValueError('retirement native AIO bdev, lvstore UUID or device path differs')
    if before:
        if snapshot['process']!=before['process'] or peers(snapshot,targets)!=peers(before,targets):
            raise ValueError('retirement changed SPDK process or another disk/controller identity')
    return True


def dependencies(api,targets,product):
    uuids={t['disk_uuid'] for t in targets}
    for kind in ('replicas','orphans','backingimages','backingimagedatasources','shards','shardgroups'):
        if not api.get('crd',kind+'.longhorn.io'):continue
        for obj in api.get(kind+'.longhorn.io',namespace=NS)['items']:
            spec=obj.get('spec',{});status=obj.get('status',{})
            referenced={spec.get('diskID'),spec.get('diskUUID'),(spec.get('parameters') or {}).get('DiskUUID')}
            referenced.update(spec.get('disks') or {});referenced.update(spec.get('diskFileSpecMap') or {})
            referenced.update(status.get('diskFileStatusMap') or {});referenced.add(status.get('v2FirstCopyDisk'))
            if uuids & referenced:raise ValueError('block retirement refuses remaining '+kind+' dependencies')


def plan(c,api,values,product):
    approvals=validate(c);context=product.host_context(c,api);directory=Path(values).parent
    path=directory/('block-retirement-'+c['longhorn_block_retirement_id']+'.json')
    config_identity={'approvals':approvals,'declarations':c['longhorn_block_disks']}
    fingerprint=hashlib.sha256(json.dumps(config_identity,sort_keys=True).encode()).hexdigest()
    if path.is_symlink():raise ValueError('symlink block retirement journal')
    native_path=directory/'block-disks.json'
    if native_path.is_symlink():raise ValueError('symlink native block receipt')
    native=json.loads(native_path.read_text())
    if path.exists():
        row=json.loads(path.read_text())
        if (row['fingerprint']!=fingerprint or row['cluster_uid']!=context['cluster_uid'] or
                row['namespace_uid']!=api.get('namespace',NS)['metadata']['uid']):raise ValueError('block retirement identity or approvals changed')
        if native not in (row['native_before'],row.get('native_after')):raise ValueError('another operation changed the native receipt')
        return path,row
    if (native['cluster_uid']!=context['cluster_uid'] or native['namespace_uid']!=api.get('namespace',NS)['metadata']['uid']):
        raise ValueError('block retirement receipt belongs to another installation')
    row={'id':c['longhorn_block_retirement_id'],'fingerprint':fingerprint,'cluster_uid':native['cluster_uid'],
        'namespace_uid':native['namespace_uid'],'native_before':native,'hosts':{},'phase':'preflight'}
    for approval in approvals:
        address=approval['node'];name=approval['name'];entry=native['nodes'].get(address,{})
        if entry.get('kubernetes_node_uid')!=context['node_uids'][address]:raise ValueError('retirement Kubernetes node UID differs')
        disk=entry.get('disks',{}).get(name,{})
        declared=next(d for d in c['longhorn_block_disks'][address] if d['name']==name)
        if (disk.get('disk_uuid')!=approval['disk_uuid'] or disk.get('path')!=declared['path'] or
                disk.get('hardware_identity')!=declared['identity'] or disk.get('disk_driver')!='aio'):
            raise ValueError('retirement approval differs from native raw identity receipt')
        current=api.get('nodes.longhorn.io',entry['name'],NS)
        if not current or current['metadata']['uid']!=entry['longhorn_node_uid']:raise ValueError('retirement native node UID differs')
        spec=current['spec'].get('disks',{}).get(name,{});status=current.get('status',{}).get('diskStatus',{}).get(name,{})
        if (spec.get('path')!=disk['path'] or spec.get('diskType')!='block' or spec.get('diskDriver')!='aio' or
                spec.get('allowScheduling') is not False or spec.get('evictionRequested') is not True or
                status.get('diskUUID')!=approval['disk_uuid'] or not product.block_disk_drained(api,status)):
            raise ValueError('retirement requires the approved native disk already evacuated with scheduling disabled')
        host=row['hosts'].setdefault(address,{'node':entry['name'],'node_uid':entry['kubernetes_node_uid'],
            'native_uid':entry['longhorn_node_uid'],'spec_before':current['spec']['disks'],'targets':[]})
        # The canonical runtime path is resolved inside the same IM namespace.
        target=dict(approval,path=disk['path'],identity=disk['hardware_identity'])
        host['targets'].append(target)
    dependencies(api,approvals,product)
    return path,row


def host_inputs(c,row):
    supplied=c.get('longhorn_block_retirement_receipts',{})
    if set(supplied)!=set(row['hosts']):raise ValueError('missing full-scope host retirement checks')
    for address,host in row['hosts'].items():
        check=supplied[address]
        if (check.get('cluster_uid')!=row['cluster_uid'] or check.get('node_uid')!=host['node_uid'] or
                check.get('id')!=row['id']):raise ValueError('host retirement receipt identity differs')
        expected=[{k:t[k] for k in ('node','name','disk_uuid')} for t in host['targets']]
        if check.get('targets')!=expected:raise ValueError('host retirement approval list differs')
        host['host_before']=check


def preflight(c,api,values,product,*,record=False):
    path,row=plan(c,api,values,product)
    if row['phase']=='complete':return path,row
    if record:host_inputs(c,row)
    # Complete every target's prerequisites before the first native mutation.
    for host in row['hosts'].values():
        observed=runtime(api,host['node'],product,host.get('runtime_before',{}).get('manager'))
        for target in host['targets']:
            bdev=next((b for b in observed['bdevs'] if b['name']==target['name']),None)
            if 'resolved_path' not in target:
                if not bdev:raise ValueError('retirement target AIO bdev is absent before ownership capture')
                aio=bdev.get('driver_specific',{}).get('aio',{})
                filename=aio.get('filename','')
                if filename!='/host'+target['path']:raise ValueError('native AIO filename differs from the approved stable by-id path')
                target['resolved_path']=aio['resolved_filename']
        if 'runtime_before' not in host:
            assert_runtime(observed,host['targets'],removed=False);host['runtime_before']=observed
        else:
            # Replay never recreates an absent bdev or imports raw media.
            current=api.get('nodes.longhorn.io',host['node'],NS)
            missing=current and all(t['name'] not in current['spec'].get('disks',{}) for t in host['targets'])
            assert_runtime(observed,host['targets'],removed=host.get('removed',False) or bool(missing),before=host['runtime_before'])
    if record:atomic(path,row)
    return path,row


def reconcile(c,api,values,product):
    path,row=preflight(c,api,values,product,record=True)
    if row['phase']=='complete':print('LONGHORN_BLOCK_RETIRE_NATIVE_PASS changed=0');return
    dependencies(api,[t for host in row['hosts'].values() for t in host['targets']],product)
    for host in row['hosts'].values():
        remaining={k:v for k,v in host['spec_before'].items() if k not in {t['name'] for t in host['targets']}}
        for attempt in range(3):
            current=api.get('nodes.longhorn.io',host['node'],NS)
            if not current or current['metadata']['uid']!=host['native_uid']:raise ValueError('retirement native node changed')
            if current['spec']['disks'] not in (host['spec_before'],remaining):raise ValueError('another operation changed native disk declarations')
            if current['spec']['disks']==remaining:break
            for target in host['targets']:
                status=current.get('status',{}).get('diskStatus',{}).get(target['name'],{})
                if status.get('diskUUID')!=target['disk_uuid'] or not product.block_disk_drained(api,status):
                    raise ValueError('disk gained a consumer before native retirement')
            host['intent']='native-disk-delete';atomic(path,row)
            try:
                api.patch('nodes.longhorn.io',host['node'],{'metadata':{'uid':host['native_uid'],
                    'resourceVersion':current['metadata']['resourceVersion']},'spec':{'disks':{t['name']:None for t in host['targets']}}},NS)
                break
            except product.CommandError as error:
                if not error.stderr.startswith('Error from server (Conflict):'):raise
                if attempt==2:raise ValueError('block retirement conflict retry limit reached') from None
        def absent():
            node=api.get('nodes.longhorn.io',host['node'],NS)
            if not node or node['metadata']['uid']!=host['native_uid']:raise ValueError('native node changed while retiring disk')
            if node['spec']['disks']!=remaining:raise ValueError('native disk declarations changed during retirement')
            if any(t['name'] in node.get('status',{}).get('diskStatus',{}) for t in host['targets']):return False
            observed=runtime(api,host['node'],product,host['runtime_before']['manager'])
            if not assert_runtime(observed,host['targets'],removed=True,before=host['runtime_before']):return False
            host['runtime_after']=observed;return True
        product.wait('native AIO disk and lvstore retirement',int(c['longhorn_ready_timeout']),absent)
        host['removed']=True;atomic(path,row)
    row['phase']='native-removed';atomic(path,row)
    print('LONGHORN_BLOCK_RETIRE_NATIVE_PASS changed=1')


def release_proofs(c,api,values,product):
    path,row=plan(c,api,values,product)
    if row['phase'] not in ('native-removed','complete'):raise ValueError('native absence must precede host receipt release')
    return {address:{'id':row['id'],'cluster_uid':row['cluster_uid'],'namespace_uid':row['namespace_uid'],
        'node_uid':host['node_uid'],'targets':host['host_before']['targets'],'before_sha256':host['host_before']['marker_sha256'],
        'native_absent':True} for address,host in row['hosts'].items()}


def finalize(c,api,values,product):
    path,row=plan(c,api,values,product)
    if row['phase'] not in ('native-removed','control-release-intent','complete'):raise ValueError('native retirement has not converged')
    releases=c.get('longhorn_block_retirement_release_receipts',{})
    if set(releases)!=set(row['hosts']):raise ValueError('missing every host receipt release result')
    for address,host in row['hosts'].items():
        result=releases[address]
        if (result.get('id')!=row['id'] or result.get('cluster_uid')!=row['cluster_uid'] or result.get('node_uid')!=host['node_uid'] or
                result.get('before_sha256')!=host['host_before']['marker_sha256'] or result.get('targets')!=host['host_before']['targets'] or
                result.get('data_preserved') is not True):raise ValueError('host receipt release proof differs')
    if row['phase']=='complete':print('LONGHORN_BLOCK_RETIRE_PASS changed=0');return
    after=copy.deepcopy(row['native_before'])
    for address,host in row['hosts'].items():
        for target in host['targets']:del after['nodes'][address]['disks'][target['name']]
        if not after['nodes'][address]['disks']:del after['nodes'][address]
    row.update(native_after=after,phase='control-release-intent');atomic(path,row)
    receipt=Path(values).with_name('block-disks.json');current=json.loads(receipt.read_text())
    if current not in (row['native_before'],after):raise ValueError('native block receipt changed before precision release')
    if current!=after:atomic(receipt,after)
    row['phase']='complete';atomic(path,row)
    print('LONGHORN_BLOCK_RETIRE_PASS changed=1')
