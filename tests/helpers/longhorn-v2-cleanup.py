#!/usr/bin/env python3
"""LONGHORN-54: protected native uninstall, exact host restoration and release.

Only AIO disks are owned here: the kernel driver stays bound throughout. The
manager 8320238242e8c804325d9588ec1db10feb9f36b8 node deletion path invokes
cleanupDisksBeforeNodeDeletion before native uninstall removes the managers.
No raw-device write, forced finalizer removal or inferred ownership is allowed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
HOSTS = {'source': ('192.168.47.146','192.168.47.147','192.168.47.148'), 'target': ('192.168.47.149',)}
MARKER = 'LONGHORN_V2_SOURCE_HOST_RELEASE_PASS'


def load(name):
    spec = importlib.util.spec_from_file_location('retire_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fixture(scope):
    install = load('longhorn-v2-install.py' if scope == 'source' else 'longhorn-v2-dr-target-install.py')
    _, lab = install.fixture()
    if tuple(lab.NODES) != HOSTS[scope]: raise ValueError('retirement scope host identity differs')
    if scope == 'target':
        # Only this gate extends the target supervisor with refusal observation.
        lab.supervise_core = lambda pid, step, expected_refusal=None: supervise(lab, pid, step, expected_refusal)
    return install, lab


def supervise(lab, pid, step, expected_refusal=None):
    identity = lab.process_identity(pid)
    expected = [str(BASE/'tests/helpers/longhorn-v2-dr-target.py').encode(), b'relay-maintain']
    if not identity or identity[0][-2:] != expected: raise ValueError('retirement target Registry supervisor differs')
    argv = [sys.executable, str(BASE/'kubecli.py'), *{
        'longhorn-setup':['setup',lab.CLUSTER,'09'], 'longhorn-destroy':['destroy',lab.CLUSTER]}[step]]
    with tempfile.TemporaryFile(mode='w+') as output:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True,
            stdout=output if expected_refusal else None, stderr=subprocess.STDOUT if expected_refusal else None)
        started = heartbeat = time.monotonic()
        with lab.scoped_cleanup(lambda: lab.stop_product(process, argv)):
            while process.poll() is None:
                if lab.process_identity(pid) != identity: raise ValueError('retirement target Registry supervisor exited')
                now = time.monotonic()
                if now-started > 7200: raise TimeoutError('retirement product step exceeded two hours')
                if now-heartbeat > 15:
                    print('LONGHORN_V2_CLEANUP_HEARTBEAT step='+step, flush=True); heartbeat=now
                time.sleep(1)
            if expected_refusal:
                output.seek(0); text=output.read()
                if not process.returncode or 'LONGHORN_PRODUCT_FAIL reason='+expected_refusal not in text:
                    lab.write_private(lab.DIRECTORY/('retirement-unexpected-refusal-'+uuid.uuid4().hex+'.json'),
                        {'rc':process.returncode,'output':text})
                    raise ValueError('retirement did not observe the exact product refusal')
                print('LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED', flush=True)
            elif process.returncode: raise ValueError('retirement product step returned nonzero')


def host_probe(request, *, root='/'):
    """Read raw identity and process consumers; manage only an exact canary file."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import uuid
    uuid.UUID(request['id'])
    root=Path(root);expected=request['disk'];path=root/expected['path'].lstrip('/')
    if not expected['path'].startswith('/dev/disk/by-id/') or not path.is_symlink(): raise ValueError('retirement requires stable disk alias')
    real=path.resolve(); info=real.stat()
    if root.resolve() not in real.parents: raise ValueError('retirement disk alias escapes host root')
    if not stat.S_ISBLK(info.st_mode): raise ValueError('retirement raw alias is not a block device')
    def run(args): return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout
    machine=(root/'etc/machine-id').read_text().strip()
    disks=json.loads(run(['lsblk','--json','--bytes','--output','NAME,TYPE,SIZE,RO,FSTYPE,MOUNTPOINTS,WWN,SERIAL',str(real)]))['blockdevices']
    if len(disks)!=1: raise ValueError('retirement device tree differs')
    disk=disks[0]
    identity=disk.get('wwn') or disk.get('serial')
    if (machine!=expected['machine_id'] or disk['type']!='disk' or identity!=expected['identity'] or
            int(disk['size'])!=expected['size_bytes'] or disk.get('children') or disk['ro'] or
            disk.get('fstype') or any(disk.get('mountpoints',[]))): raise ValueError('retirement disk identity or usage differs')
    sysfs=root/'sys/class/block'/real.name
    if any((sysfs/'holders').iterdir()): raise ValueError('retirement disk has active holders')
    driver=sysfs/'device/driver'
    driver=str(driver.resolve()) if driver.exists() else None
    signatures=json.loads(run(['wipefs','--no-act','--json',str(real)]))
    consumers=[]
    for process in (root/'proc').iterdir():
        if not process.name.isdigit(): continue
        try:
            argv=(process/'cmdline').read_bytes().split(b'\0')[:-1]
            if argv and Path(os.fsdecode(argv[0])).name in ('spdk_tgt','instance-manager','longhorn'): consumers.append(process.name)
            for fd in (process/'fd').iterdir():
                try: target=fd.stat()
                except FileNotFoundError: continue
                if stat.S_ISBLK(target.st_mode) and target.st_rdev==info.st_rdev: consumers.append(process.name)
        except (FileNotFoundError,ProcessLookupError): continue
    marker=root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
    if request.get('require_clean') and (consumers or signatures.get('signatures') or marker.exists() or marker.is_symlink()):
        raise ValueError('retired disk still has native consumers, signatures or CPU policy marker')
    canary=root/'var/lib'/('kubeauto-longhorn-retirement-'+request['id']+'.canary')
    if request['action']=='create':
        fd=os.open(canary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream: stream.write(request['id']);stream.flush();os.fsync(stream.fileno())
    # A durable remove intent is written before unlink. Only that owning path
    # may finish an interrupted unlink; it still verifies the disk and driver.
    if request['action']=='finish-remove' and not canary.exists() and not canary.is_symlink():
        if not request.get('canary') or not request.get('require_clean'):
            raise ValueError('retirement removal requires its original durable intent')
        return {'machine_id':machine,'disk':dict(expected),'driver':driver,'canary':None,
            'signatures':signatures,'consumers':sorted(set(consumers)),'cpu_policy_marker':marker.exists()}
    if canary.is_symlink() or not canary.is_file(): raise ValueError('retirement foreign canary is absent or replaced')
    data=canary.read_bytes(); cstat=canary.stat()
    receipt={'device':cstat.st_dev,'inode':cstat.st_ino,'sha256':hashlib.sha256(data).hexdigest()}
    if data!=request['id'].encode() or (request.get('canary') and receipt!=request['canary']):
        raise ValueError('retirement foreign canary identity changed')
    result={'machine_id':machine,'disk':dict(expected),'driver':driver,'canary':receipt,
        'signatures':signatures,'consumers':sorted(set(consumers)),'cpu_policy_marker':marker.exists()}
    if request['action'] in ('remove','finish-remove'):
        canary.unlink()
        if canary.exists() or canary.is_symlink(): raise ValueError('retirement canary cleanup incomplete')
    elif request['action'] not in ('create','check'): raise ValueError('unknown retirement canary action')
    return result


def remote(lab,row,node,action,clean=False):
    req={'id':row['id'],'disk':row['raw_disks'][node],'action':action,'require_clean':clean}
    if node in row['canaries']: req['canary']=row['canaries'][node]['canary']
    code=inspect.getsource(host_probe)+'\nimport json,sys\nprint(json.dumps(host_probe(json.load(sys.stdin)),sort_keys=True))\n'
    return json.loads(lab.remote(node,['python3','-c',code],stdin=json.dumps(req),timeout=180).stdout)


def read(journal,scope=None):
    if journal.is_symlink(): raise ValueError('symlink retirement journal')
    row=json.loads(journal.read_text());uuid.UUID(row['id']);uuid.UUID(row['run_id']);uuid.UUID(row['cluster_uid'])
    if row['scope'] not in HOSTS or (scope and row['scope']!=scope) or set(row['raw_disks'])!=set(HOSTS[row['scope']]):
        raise ValueError('foreign retirement journal scope')
    return row


def private_path(path, directory=False):
    """Recovery accepts only regular artifacts owned by this runner account."""
    import stat
    info=path.lstat()
    if (path.is_symlink() or info.st_uid!=os.geteuid() or info.st_mode & (0o077 if directory else 0o022) or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('retirement recovery artifact is not private and owned')


def recovery_plan(journal,scope):
    if (not journal.is_absolute() or journal.name!='retirement.json' or journal.parent.parent!=Path('/var/tmp') or
            not re.fullmatch(r'kubeauto-longhorn-v2-retirement\.[A-Za-z0-9]+',journal.parent.name)):
        raise ValueError('retirement recovery requires an explicit original journal path')
    private_path(journal.parent,True);private_path(journal)
    row=read(journal,scope)
    if row['phase'] not in ('prepared','native-uninstall','storage-clean','core-retiring','core-clean','released'):
        raise ValueError('retirement recovery phase is not resumable')
    if not row.get('guards_passed') or set(row.get('canaries',{}))!=set(HOSTS[scope]):
        raise ValueError('retirement recovery lacks original protection or host receipts')
    if scope=='source' and set(row.get('checkpoint_clean',[]))!={'failure','sigterm'}:
        raise ValueError('retirement recovery lacks the original checkpoint restoration')
    values=[]
    for suffix in ('exit','finalized'):
        path=journal.parent/('retire.'+suffix);private_path(path)
        value=path.read_text().strip()
        if not re.fullmatch(r'\d+',value) or not 0<=int(value)<=255: raise ValueError('invalid original retirement durable state')
        values.append(int(value))
    if values[0]!=values[1] or (values[0]==0 and row['phase']!='released'):
        raise ValueError('original retirement durable state differs from its phase')
    private_path(journal.parent/'retire.log')
    if scope=='target':
        boundary=journal.parent/'source-boundary';private_path(boundary)
        if hashlib.sha256(boundary.read_bytes()).hexdigest()!=row.get('source_boundary_sha256'):
            raise ValueError('retirement original source boundary differs')
    return row['phase']


def completed_recoveries(lab, scope, state):
    """Retained terminal receipts are provenance, not active operations.

    Bind historical records to this run's original machines and immutable
    archives. Later owning runtime/reboot/upgrade operations may change the
    current installation; they must not invalidate completed recovery history.
    Current installation and host health remain the retirement owner's checks.
    """
    nodes = set(HOSTS[scope])
    pages = lab.DIRECTORY/'longhorn-v2-resources-reboot.json'
    if pages.exists() or pages.is_symlink():
        private_path(pages)
        resources = load('longhorn-v2-resources.py')
        private_path(resources.record_path(lab))
        record = resources.read_record(lab)
        reboot = load('longhorn-v2-resources-reboot.py')
        reboot.completed_transition(lab, pages, record)
        row = json.loads(pages.read_text())
        private_path(lab.DIRECTORY/row['archive'])
        original = json.loads((lab.DIRECTORY/row['archive']).read_text())
        if any(original.get(key) != record[key] for key in ('id', 'run_id', 'cluster_uid')):
            raise ValueError('completed page recovery archive belongs to another owner')
    for name in ('longhorn-v2-post-reboot-environment.json',
                 'longhorn-v2-installed-post-reboot-environment.json'):
        path = lab.DIRECTORY/name
        if not path.exists() and not path.is_symlink():
            continue
        private_path(path)
        row = json.loads(path.read_text())
        engine = 'v2' if scope == 'source' else 'v2-dr-target'
        if (row.get('phase') != 'complete' or row.get('run_id') != state['run_id'] or
                row.get('engine') != engine or set(row.get('hosts', {})) != nodes):
            raise ValueError('finish the owning module before final V2 retirement')
        sha = row.get('state_sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise ValueError('completed environment recovery lacks its original digest')
        expected = 'longhorn-v2-host-state-pre-reboot-'+sha[:16]+'.json'
        if row.get('state_archive') != expected:
            raise ValueError('completed environment recovery archive path differs')
        archive = lab.DIRECTORY/expected
        private_path(archive)
        raw = archive.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError('completed environment recovery original bytes changed')
        original = json.loads(raw)
        if any(original.get(key) != state[key] for key in ('run_id', 'cluster_uid', 'raw_disks')):
            raise ValueError('completed environment recovery belongs to another owner')
        for node, host in row['hosts'].items():
            before, after = host['before'], host['after']
            old = original['host_state']['nodes'][node]['multipath']
            if (host['phase'] != 'stopped' or old['phase'] != 'stopped' or
                    before['machine_id'] != state['raw_disks'][node]['machine_id'] or
                    (before['boot_id'] == old['current']['boot_id'] and
                     (name != 'longhorn-v2-installed-post-reboot-environment.json' or
                      before != old['current'] or after != before)) or
                    any(before[key] != old['current'][key] for key in ('dm', 'configs'))):
                raise ValueError('completed environment recovery host boundary differs')
            expected_after = json.loads(json.dumps(before))
            for unit in expected_after['units'].values():
                unit['ActiveState'] = 'inactive'
            if set(before['units']) != {'multipathd.service', 'multipathd.socket'} or after != expected_after:
                raise ValueError('completed environment recovery did not preserve host state')
        if name == 'longhorn-v2-installed-post-reboot-environment.json':
            identity = row.get('installation_before', {}).get('identity', {})
            if any(identity.get(key) != original[key] for key in ('run_id', 'cluster_uid', 'namespace_uid', 'raw_disks')):
                raise ValueError('completed environment recovery installation differs')
        else:
            adoption_sha = row.get('adoption_sha256', '')
            stem = 'longhorn-'+engine+'-im-adopt-rejected-setup-'
            if (not re.fullmatch(r'[0-9a-f]{64}', adoption_sha) or
                    row.get('adoption_archive') != stem+adoption_sha[:16]+'.json'):
                raise ValueError('completed environment recovery adoption archive differs')
            adoption_path = lab.DIRECTORY/row['adoption_archive']
            private_path(adoption_path)
            adoption_raw = adoption_path.read_bytes()
            adoption = json.loads(adoption_raw)
            if (hashlib.sha256(adoption_raw).hexdigest() != adoption_sha or
                    adoption.get('run_id') != state['run_id'] or adoption.get('engine') != engine or
                    adoption.get('phase') != 'setup-intent' or 'new_managers' in adoption):
                raise ValueError('completed environment recovery adoption provenance differs')


def preflight(lab,scope):
    lab.core_identity();state=lab.owner()
    if set(state['raw_disks'])!=set(HOSTS[scope]): raise ValueError('retirement requires exact original raw-disk ownership')
    # Each owning module must finish its own restoration before final retirement.
    active=('longhorn-install-business.json','longhorn-v2-runtime-resources.json','longhorn-v2-monitoring.json',
        'longhorn-v2-im-adopt.json','longhorn-v2-cpu-isolation.json','longhorn-v2-detached-setting.json',
        'longhorn-v2-boot-fixture.json','longhorn-v2-interrupt.json','longhorn-v2-broken-restore.json',
        'longhorn-v2-linked-clones.json','longhorn-v2-linked-lifecycle.json','longhorn-dr-backup-consumer.json',
        'longhorn-systembackup-qualification.json','longhorn-systemrestore-qualification.json','longhorn-systemrestore-business.json')
    if any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in active):
        raise ValueError('finish the owning module before final V2 retirement')
    completed_recoveries(lab, scope, state)
    if lab.get('volumes.longhorn.io',namespace=NS)['items']: raise ValueError('foreign or retained volumes prevent retirement')
    return state


def native_receipt(lab,state):
    path=lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if path.is_symlink(): raise ValueError('symlink native block receipt')
    receipt=json.loads(path.read_text())
    if (any(receipt.get(k)!=state[k] for k in ('cluster_uid','namespace_uid')) or
            set(receipt.get('nodes',{}))!=set(lab.NODES)): raise ValueError('native receipt installation differs')
    for node,entry in receipt['nodes'].items():
        native=lab.get('nodes.longhorn.io',entry['name'],NS);expected=state['raw_disks'][node]
        if (entry['kubernetes_node_uid']!=state['node_uids'][entry['name']] or
                not native or native['metadata']['uid']!=entry['longhorn_node_uid'] or not entry['disks']):
            raise ValueError('native raw-node receipt identity changed')
        for name,disk in entry['disks'].items():
            spec=native['spec'].get('disks',{}).get(name,{});status=native['status'].get('diskStatus',{}).get(name,{})
            if (disk['path']!=expected['path'] or disk['hardware_identity']!=expected['identity'] or
                    disk['disk_driver']!='aio' or spec.get('diskType')!='block' or spec.get('diskDriver')!='aio' or
                    spec.get('path')!=disk['path'] or status.get('diskUUID')!=disk['disk_uuid']):
                raise ValueError('native raw disk UUID, hardware or driver differs')
    return receipt


def prepare(lab,scope,journal):
    if journal.exists() or journal.is_symlink(): raise ValueError('retirement journal already exists; use recovery')
    state=preflight(lab,scope)
    row={'id':str(uuid.uuid4()),'scope':scope,'run_id':state['run_id'],'cluster_uid':state['cluster_uid'],
        'raw_disks':state['raw_disks'],'native_receipt':native_receipt(lab,state),'canaries':{},'phase':'capture'}
    if scope=='target':
        boundary=journal.parent/'source-boundary';private_path(boundary)
        row['source_boundary_sha256']=hashlib.sha256(boundary.read_bytes()).hexdigest()
    lab.write_private(journal,row)
    for node in lab.NODES:
        row['canaries'][node]=remote(lab,row,node,'create');lab.write_private(journal,row)
    row['phase']='prepared';lab.write_private(journal,row)
    print('LONGHORN_V2_CLEANUP_PREPARED scope='+scope,flush=True)


def guard_business(lab,pid,fault=None):
    """Same native PV/volume guard for one-node target and three-node source."""
    lab.installation_health();record=lab.DIRECTORY/'longhorn-install-business.json'
    if record.exists() or record.is_symlink(): raise ValueError('previous guard fixture requires recovery')
    sc=lab.get('storageclass','longhorn')
    if sc.get('provisioner')!='driver.longhorn.io' or sc.get('reclaimPolicy')!='Retain' or sc.get('parameters',{}).get('dataEngine')!='v2':
        raise ValueError('cleanup requires product V2 Retain StorageClass')
    namespace='longhorn-business-retire-'+secrets.token_hex(8)
    ns=json.loads(lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(
        {'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace}})).stdout)
    item={'namespace':namespace,'namespace_uid':ns['metadata']['uid'],'objects':{},'storage_class':'longhorn','reclaim_policy':'Retain'}
    lab.write_private(record,item)
    with lab.scoped_cleanup(lambda:lab.cleanup_business(record,relay_pid=pid)):
        item['defaults']=lab.wait('retirement-namespace-defaults',60,lambda:lab.namespace_defaults(namespace));lab.write_private(record,item)
        create=lambda obj:lab.create_business_object(item,record,obj)
        claim=create({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'data','namespace':namespace},
            'spec':{'accessModes':['ReadWriteOnce'],'storageClassName':'longhorn','resources':{'requests':{'storage':'2Gi'}}}})
        lab.wait('retirement-claim-bound',600,lambda:(p:=lab.get('pvc','data',namespace)) and p.get('status',{}).get('phase')=='Bound')
        lab.bind_business_volume(item,record)
        create(lab.client_pod(namespace,'writer','data','longhorn-node-'+lab.NODES[0].rsplit('.',1)[1]))
        lab.wait('retirement-writer-ready',600,lambda:any(c.get('type')=='Ready' and c.get('status')=='True'
            for c in lab.get('pod','writer',namespace).get('status',{}).get('conditions',[])))
        payload=lab.owner()['run_id'];expected=hashlib.sha256(payload.encode()*262144).hexdigest()
        def digest(read_only):
            actual=lab.kubectl('-n',namespace,'exec','writer','--','python3','-c',lab.payload_program(payload,read_only=read_only)).stdout.strip()
            if actual!=expected: raise ValueError('retirement guard changed acknowledged data')
        digest(False)
        if fault:
            # Interrupt the actual fsynced PVC fixture before any irreversible
            # uninstall. The normal owning context must reclaim it on both exits.
            print('LONGHORN_V2_CLEANUP_CHECKPOINT_ARMED fault='+fault,flush=True)
            if fault=='sigterm': os.kill(os.getpid(),signal.SIGTERM)
            raise SystemExit(86)
        uninstall=load('longhorn-uninstall.py');uninstall.verify_refusal(lab,item,pid,'bound');digest(True)
        lab.remove(lab.get('pod','writer',namespace));lab.wait('retirement-writer-gone',180,lambda:not lab.get('pod','writer',namespace))
        current=lab.get('pvc','data',namespace)
        if current['metadata']['uid']!=claim['metadata']['uid']: raise ValueError('retirement claim identity changed')
        lab.remove(current);lab.wait('retirement-claim-gone',180,lambda:not lab.get('pvc','data',namespace))
        lab.wait('retirement-pv-released',180,lambda:lab.get('pv',item['pv']).get('status',{}).get('phase')=='Released')
        uninstall.verify_refusal(lab,item,pid,'released')
    print('LONGHORN_V2_CLEANUP_PROTECTED_DATA_PASS',flush=True)


def recover_fixture_kubelet(lab, resources):
    """Recover the interrupted zero-page restore after a removed V1 fixture.

    This is not permission to adopt a stopped or rebooted host. Only the exact
    journaled restore-confirmed host with the deleted fixture still cached in
    systemd may have its original active kubelet restored.
    """
    state = lab.owner()
    nodes = lab.get('nodes')['items']
    if (lab.get('namespace', 'kube-system')['metadata']['uid'] != state['cluster_uid'] or
            {n['metadata']['name']: n['metadata']['uid'] for n in nodes} != state['node_uids'] or
            {a['address'] for n in nodes for a in n['status']['addresses'] if a['type'] == 'InternalIP'} != set(lab.NODES) or
            lab.get('namespace', NS) or lab.get('crd', 'volumes.longhorn.io')):
        raise ValueError('retirement kubelet recovery requires its original uninstalled core identities')
    record = resources.read_record(lab)
    for node, saved in record['hosts'].items():
        actual = resources.probe(lab, node)
        if actual['kubelet_active'] == saved['current']['kubelet_active']:
            continue
        keys = ('machine_id', 'boot_id', 'total', 'free', 'reserved')
        if (saved['phase'] != 'restore-confirmed' or saved['original']['total'] != 0 or
                saved['current']['kubelet_active'] != 'active' or actual['kubelet_active'] != 'inactive' or
                any(actual[k] != saved['current'][k] for k in keys) or
                any(actual[k] for k in ('total', 'free', 'reserved'))):
            raise ValueError('retirement kubelet recovery differs from the interrupted empty page-pool restore')
        script = '''import json,os,stat,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
assert Path('/etc/machine-id').read_text().strip()==r['machine_id']
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==r['boot_id']
mount='/data/kubeauto-longhorn-coexist46'
drop='/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'
for name in (drop,'/etc/kubeauto/longhorn-disk.json'):
 p=Path(name);assert not p.exists() and not p.is_symlink(),'fixture host ownership remains'
point=Path(mount);assert not point.is_symlink(),'fixture mountpoint replaced'
if point.exists():
 info=point.lstat()
 assert stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and not any(point.iterdir()),'fixture mountpoint has foreign contents'
 assert subprocess.run(['findmnt','--mountpoint',mount],capture_output=True,timeout=20).returncode==1,'fixture mount is still active'
assert mount not in Path('/etc/fstab').read_text(),'fixture fstab entry remains'
def show():
 raw=subprocess.run(['systemctl','show','kubelet','-p','NeedDaemonReload','-p','DropInPaths','-p','ExecStartPre','-p','ActiveState'],check=True,text=True,capture_output=True,timeout=20).stdout
 return dict(line.split('=',1) for line in raw.splitlines())
before=show()
assert before['ActiveState']=='inactive' and before['NeedDaemonReload']=='yes'
assert before['DropInPaths']==drop and mount in before['ExecStartPre'],'not the removed V1 fixture dependency'
subprocess.run(['systemctl','daemon-reload'],check=True,timeout=30)
after=show()
assert after['NeedDaemonReload']=='no' and mount not in after['ExecStartPre'] and drop not in after['DropInPaths']
if point.exists():
 now=point.lstat()
 assert not point.is_symlink() and (now.st_dev,now.st_ino)==(info.st_dev,info.st_ino),'fixture mountpoint changed during reload'
 point.rmdir()
subprocess.run(['systemctl','start','kubelet'],check=True,timeout=120)
assert show()['ActiveState']=='active'
print('LONGHORN_V2_RETIRED_FIXTURE_KUBELET_RECOVERED')
'''
        lab.remote(node, ['python3', '-c', script], stdin=json.dumps(saved['current']), timeout=180)
        after = resources.probe(lab, node)
        if any(after[k] != saved['current'][k] for k in (*keys, 'kubelet_active')):
            raise ValueError('retirement kubelet recovery did not restore its exact journaled boundary')
        resources.refresh_kubelet(lab, node, saved['original']['total'])


def storage(lab,install,scope,journal,pid,recover=False):
    row=read(journal,scope);state=lab.owner()
    if any(row[k]!=state[k] for k in ('run_id','cluster_uid','raw_disks')): raise ValueError('retirement baseline replaced')
    for node in lab.NODES: remote(lab,row,node,'check')
    if not recover:
        guard_business(lab,pid);row['guards_passed']=True
        row['uninstall_shared']=lab.shared_snapshot_identity();lab.write_private(journal,row)
    if not row.get('guards_passed'): raise ValueError('retirement recovery does not invent protected-data acceptance')
    row['phase']='native-uninstall';lab.write_private(journal,row)
    if state.get('namespace_uid') and recover and not lab.get('namespace',NS):
        complete_uninstall(lab,row,state)
    elif state.get('namespace_uid'): lab.recover_installation(pid)
    else:
        if not state.get('native_uninstalled') or lab.get('namespace',NS) or lab.get('crd','volumes.longhorn.io'):
            raise ValueError('retirement cannot adopt unknown native uninstall state')
        if state.get('host_state'): lab.restore_hosts()
    native=row['native_receipt'];directory=lab.DIRECTORY/'yml/longhorn'
    archived=directory/('block-disks-uninstalled-'+native['namespace_uid']+'.json')
    if ((directory/'block-disks.json').exists() or archived.is_symlink() or json.loads(archived.read_text())!=native):
        raise ValueError('product did not retire the exact native raw-disk receipt')
    if recover:uninstalled_config(lab)
    for node in lab.get('nodes')['items']:
        labels=node['metadata'].get('labels',{})
        if (labels.get('kubeauto.io/component')=='longhorn' or any(key in labels for key in (
                'node.longhorn.io/create-default-disk','node.longhorn.io/disable-v2-data-engine','kubeauto.io/longhorn-client'))):
            raise ValueError('native uninstall left owned bootstrap labels')
    resources=load('longhorn-v2-resources.py')
    # Product host restoration can remove unit drop-ins. Flush their loaded
    # graph before the page-pool owner refreshes kubelet capacity.
    if recover and resources.record_path(lab).exists():
        recover_fixture_kubelet(lab, resources)
    for node in lab.NODES:
        lab.remote(node, ['systemctl', 'daemon-reload'])
    (resources.restore if scope=='source' else install.restore_resources)(lab)
    for node in lab.NODES:
        after=remote(lab,row,node,'check',True)
        if after['driver']!=row['canaries'][node]['driver']: raise ValueError('AIO kernel driver binding changed')
        pages=resources.probe(lab,node)
        if pages['total'] or pages['free'] or pages['reserved']: raise ValueError('original zero page pool did not restore')
    row['phase']='storage-clean';lab.write_private(journal,row)
    print('LONGHORN_V2_CLEANUP_STORAGE_SCOPED_CLEAN_PASS scope='+scope,flush=True)


def complete_uninstall(lab,row,state):
    """Finish only the native namespace-delete/owner-write interruption window."""
    directory=lab.DIRECTORY/'yml/longhorn';native=row['native_receipt'];uid=native['namespace_uid']
    if (state.get('namespace_uid')!=uid or state.get('disks') or lab.get('namespace',NS) or
            lab.get('crd','volumes.longhorn.io') or 'uninstall_shared' not in row or
            lab.shared_snapshot_identity()!=row['uninstall_shared']):
        raise ValueError('retirement cannot reconcile an unknown native uninstall')
    for name,expected in (
            ('block-disks-uninstalled-'+uid+'.json',native),
            ('installation-uninstalled-'+uid+'.json',{'cluster_uid':row['cluster_uid'],'namespace_uid':uid})):
        path=directory/name
        if path.is_symlink() or json.loads(path.read_text())!=expected:
            raise ValueError('retirement interrupted uninstall archive differs')
    for name in ('block-disks.json','installation.json'):
        path=directory/name
        if path.exists() or path.is_symlink(): raise ValueError('retirement native receipt still active')
    lab.execute([sys.executable,BASE/'roles/longhorn/files/longhorn.py','guard',
        '--kubeconfig',lab.DIRECTORY/'kubectl.kubeconfig'])
    if state.get('host_state'):lab.restore_hosts()
    state=lab.owner()
    for key in ('namespace_uid','namespace_defaults','tls_uid','encryption_uid'):state.pop(key,None)
    state.update(healthy=False,native_uninstalled=True);lab.write_private(lab.STATE,state)
    uninstalled_config(lab)


def uninstalled_config(lab):
    # Match the owning installation recovery's final config transition.
    import yaml
    path=lab.DIRECTORY/'config.yml';config=yaml.safe_load(path.read_text())
    config.update(longhorn_install='no',longhorn_action='present',longhorn_uninstall_id='',longhorn_requests=[])
    config['longhorn_storage_classes']=[r for r in config['longhorn_storage_classes'] if r['name']=='longhorn']
    lab.write_private(path,config)


def checkpoint(lab,scope,journal,pid,fault,verify=False):
    row=read(journal,scope)
    if row['phase']!='prepared': raise ValueError('checkpoint requires unchanged pre-uninstall baseline')
    if verify:
        if row.get('fault_intent')!=fault: raise ValueError('checkpoint recovery intent differs')
        preflight(lab,scope);lab.installation_health()
        for node in lab.NODES: remote(lab,row,node,'check')
        row.setdefault('checkpoint_clean',[]).append(fault);row.pop('fault_intent',None)
        lab.write_private(journal,row)
        print('LONGHORN_V2_CLEANUP_CHECKPOINT_SCOPED_CLEAN_PASS fault='+fault,flush=True)
    else:
        if row.get('fault_intent') or fault in row.get('checkpoint_clean',[]): raise ValueError('checkpoint already attempted')
        row['fault_intent']=fault;lab.write_private(journal,row)
        guard_business(lab,pid,fault=fault)
        raise ValueError('checkpoint did not exit through its requested fault')


def core(lab,scope,journal,recover=False):
    row=read(journal,scope)
    if row['phase'] not in (('storage-clean','core-retiring','core-clean','released') if recover else ('storage-clean',)):
        raise ValueError('native storage retirement is a core prerequisite')
    archive=Path('/var/tmp/kubeauto-longhorn-history')/('core-'+row['run_id'])
    if row['phase']=='storage-clean':
        state=lab.owner()
        if any(row[k]!=state[k] for k in ('run_id','cluster_uid','raw_disks')):
            raise ValueError('retirement core identity changed')
        for node in lab.NODES:remote(lab,row,node,'check',True)
        row['core_intent']={'owner_sha256':hashlib.sha256(lab.STATE.read_bytes()).hexdigest(),
            'inventory_sha256':hashlib.sha256((lab.DIRECTORY/'hosts').read_bytes()).hexdigest()}
        row['archive']=str(archive);row['phase']='core-retiring';lab.write_private(journal,row)
    if row.get('archive')!=str(archive):raise ValueError('retirement core archive path differs')
    if lab.DIRECTORY.exists() or lab.DIRECTORY.is_symlink():
        if row['phase']!='core-retiring' or archive.exists() or archive.is_symlink():
            raise ValueError('retirement core active/archive ownership overlaps')
        core_owner(lab,row,lab.DIRECTORY)
        for node in lab.NODES:remote(lab,row,node,'check',True)
        load('longhorn-v2-core.py').cleanup(lab)
    core_owner(lab,row,archive)
    # An archive proves the owning core path reached archive_core. Re-run its
    # read-only fixed verifier on every recovery before releasing any host.
    if recover:
        verified=lab.execute(['bash',BASE/'tests/helpers/lab-wipe-nodes.sh','--verify','--rocky-only',*lab.NODES],timeout=900)
        print(verified.stdout,end='',flush=True)
    row['phase']='core-clean';lab.write_private(journal,row)
    pool=load('longhorn-v2-pool.py');row.setdefault('released',{});row.setdefault('release_intents',{})
    for node in lab.NODES:
        inventory=json.loads(lab.remote(node,['python3','-c',pool.PROBE],timeout=180).stdout)
        observed={'host':node,'inventory':inventory,'assessment':pool.assess(inventory)}
        validate_release(row,node,observed)
        pending=node in row['release_intents']
        if pending and row['release_intents'][node]!=row['canaries'][node]:
            raise ValueError('retirement host removal intent differs')
        if not pending:
            remote(lab,row,node,'check',True)
            row['release_intents'][node]=row['canaries'][node];lab.write_private(journal,row)
        after=remote(lab,row,node,'finish-remove' if pending else 'remove',True)
        if after['driver']!=row['canaries'][node]['driver']: raise ValueError('retired AIO kernel driver differs')
        row['released'][node]=observed;lab.write_private(journal,row)
        print((MARKER if scope=='source' else 'LONGHORN_V2_TARGET_HOST_RELEASE_PASS')+' host='+node,flush=True)
    row['phase']='released';lab.write_private(journal,row)
    print('LONGHORN_V2_CLEANUP_SCOPED_CLEAN_PASS scope='+scope,flush=True)


def core_owner(lab,row,directory):
    """Use the same owner/inventory verifier, without reviving archived state."""
    if directory.is_symlink() or not directory.is_dir():raise ValueError('retirement core archive missing or replaced')
    # kubecli creates active deployment directories as 0755. Require
    # ownership and no foreign writes, while recovery
    # journals retain private_path's stricter 0700 directory requirement.
    info=directory.stat()
    if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
        raise ValueError('retirement core directory is not owned or is writable')
    private_path(directory/'longhorn-lab-owner.json');private_path(directory/'hosts')
    original_directory,original_state=lab.DIRECTORY,lab.STATE
    try:
        lab.DIRECTORY=directory;lab.STATE=directory/'longhorn-lab-owner.json'
        state=lab.owner()
        if (any(state[k]!=row[k] for k in ('run_id','cluster_uid','raw_disks')) or
                state.get('namespace_uid') or state.get('disks') or state.get('relays') or
                hashlib.sha256(lab.STATE.read_bytes()).hexdigest()!=row['core_intent']['owner_sha256'] or
                hashlib.sha256((directory/'hosts').read_bytes()).hexdigest()!=row['core_intent']['inventory_sha256']):
            raise ValueError('retirement core archive identity differs')
    finally:lab.DIRECTORY,lab.STATE=original_directory,original_state


def validate_release(row,node,observed):
    expected=row['raw_disks'][node];host=observed['inventory'];assessment=observed['assessment']
    if (observed['host']!=node or assessment['blockers'] or host['machine_id']!=expected['machine_id'] or
            host['hugepages_2mi']!='0' or len(assessment['candidates'])!=1): raise ValueError('released V2 host is not clean')
    disk=assessment['candidates'][0]
    if (disk['identity']!=expected['identity'] or disk['size_bytes']!=expected['size_bytes'] or expected['path'] not in disk['by_id']):
        raise ValueError('released V2 raw identity differs from original admission')


def finalize(journal,prefix,log,out):
    row=read(journal)
    if row['phase']!='released' or set(row['released'])!=set(row['raw_disks']): raise ValueError('retirement release incomplete')
    if row['scope']=='source' and set(row.get('checkpoint_clean',[]))!={'failure','sigterm'}:
        raise ValueError('source retirement requires actual failure and SIGTERM fixture restoration')
    for suffix in ('exit','finalized'):
        path=Path(str(prefix)+'.'+suffix)
        if path.is_symlink() or path.read_text().strip()!='0': raise ValueError('retirement needs actual durable rc=0')
    if log.is_symlink() or not log.is_absolute(): raise ValueError('release log must be an absolute regular owned artifact')
    text=log.read_text();marker=MARKER if row['scope']=='source' else 'LONGHORN_V2_TARGET_HOST_RELEASE_PASS'
    for node in row['raw_disks']:
        if marker+' host='+node not in text.splitlines(): raise ValueError('release log lacks exact host marker')
        validate_release(row,node,row['released'][node])
    if any(line.startswith(('LONGHORN_FIRST_FAILURE','LONGHORN_CLEANUP_FAIL','LONGHORN_V2_CLEANUP_FAIL')) for line in text.splitlines()):
        raise ValueError('failed retirement log cannot release a host')
    out.mkdir(mode=0o700,parents=True,exist_ok=True)
    for node,disk in row['raw_disks'].items():
        proof={'status':'pass','signed_scope':'v2-'+row['scope']+'-host-release','host':node,
            'durable':{'exit':0,'finalized':0},'cleanup':{'host_clean_verified':True,'raw_disk_clean_verified':True,'page_pool_restored':True},
            'markers':[marker],'owner_run_id':row['run_id'],
            ('source_cluster_uid' if row['scope']=='source' else 'target_cluster_uid'):row['cluster_uid'],
            'released_at':datetime.now(timezone.utc).isoformat(),'original_raw_disk':disk,
            'raw_logs':[{'path':str(log),'sha256':hashlib.sha256(log.read_bytes()).hexdigest()}],
            'scope':'AIO native uninstall; no whole-device zeroing or PCI rebinding','archive':row['archive']}
        path=out/('longhorn-v2-'+row['scope']+'-released-'+node+'.json')
        if path.exists() or path.is_symlink(): raise ValueError('release proof already exists')
        path.write_text(json.dumps(proof,sort_keys=True)+'\n');path.chmod(0o600)
        print('LONGHORN_V2_RELEASE_PROOF path='+str(path),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','guards','checkpoint','checkpoint-verify','storage','recover-storage','core','recover-core','recovery-plan','finalize'))
    parser.add_argument('--scope',choices=tuple(HOSTS),required=True);parser.add_argument('--journal',type=Path,required=True)
    parser.add_argument('--relay-pid',type=int,default=0);parser.add_argument('--status-prefix',type=Path)
    parser.add_argument('--log',type=Path);parser.add_argument('--out',type=Path)
    parser.add_argument('--fault',choices=('failure','sigterm'));args=parser.parse_args()
    if args.action=='recovery-plan':
        print(recovery_plan(args.journal,args.scope));return
    if args.action=='finalize':
        if not all((args.status_prefix,args.log,args.out)): parser.error('finalize requires durable prefix, log and output directory')
        if read(args.journal)['scope']!=args.scope: raise ValueError('finalize scope differs')
        finalize(args.journal,args.status_prefix,args.log,args.out);return
    install,lab=fixture(args.scope);lab.require_lease()
    def interrupted(signum,frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    if args.action=='prepare':prepare(lab,args.scope,args.journal)
    elif args.action=='guards':guard_business(lab,args.relay_pid)
    elif args.action in ('checkpoint','checkpoint-verify'):
        if not args.fault: parser.error('checkpoint requires explicit fault')
        checkpoint(lab,args.scope,args.journal,args.relay_pid,args.fault,verify=args.action=='checkpoint-verify')
    elif args.action in ('core','recover-core'):core(lab,args.scope,args.journal,recover=args.action=='recover-core')
    else:storage(lab,install,args.scope,args.journal,args.relay_pid,recover=args.action=='recover-storage')


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,TypeError,OSError,TimeoutError) as error:
        print('LONGHORN_V2_CLEANUP_FAIL reason='+str(error),file=sys.stderr,flush=True);sys.exit(1)
