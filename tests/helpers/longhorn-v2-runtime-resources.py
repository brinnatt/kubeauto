#!/usr/bin/env python3
"""Remaining LONGHORN-44 runtime resources; reusable by LONGHORN-51.

Reuses the accepted host/page and polling/interrupt evidence. Profiles always
enter through config+kubecli. CPU Manager preparation follows drain, stopped
kubelet, checkpoint removal, and kubelet-generated replacement; no checkpoint
is fabricated. An unsafe anonymous/exclusive combination never launches merely
to discover an expected OOM.
"""
import argparse
import base64
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import signal
import tempfile
import uuid
from types import SimpleNamespace

import yaml

NS = 'longhorn-system'
RECORD = 'longhorn-v2-runtime-resources.json'
BUSINESS = 'longhorn-install-business.json'
COMBINATION_REFUSAL = 'V2 anonymous memory with exclusive CPU requires an instance-manager memory limit covering SPDK allocation'
CPU_POLICY_REFUSAL = 'exclusive CPU allocation requires static CPU policy on every Longhorn node'
CPU_RESERVATION_REFUSAL = 'V2 allocatable CPU and guaranteed reservation must cover SPDK reactor cores'
BUDGET_REFUSAL = 'iobuf pools exhaust the configured SPDK memory budget; increase memory first'
FIELDS = {'longhorn_v2_cpu_mask': 'data-engine-cpu-mask',
          'longhorn_v2_cpu_cores': 'data-engine-number-of-cpu-cores',
          'longhorn_v2_memory_mib': 'data-engine-memory-size',
          'longhorn_v2_hugepages': 'data-engine-hugepage-enabled',
          'longhorn_v2_interrupt_mode': 'data-engine-interrupt-mode-enabled',
          'longhorn_v2_cpu_reservation_percentage': 'guaranteed-instance-manager-cpu',
          'longhorn_v2_iobuf_large_pool_size': 'data-engine-iobuf-large-pool-size',
          'longhorn_v2_iobuf_small_pool_size': 'data-engine-iobuf-small-pool-size'}


def profile(config, name):
    row = {k: config[k] for k in FIELDS}
    if name == 'anonymous-iobuf':
        row.update(longhorn_v2_hugepages=False, longhorn_v2_iobuf_large_pool_size=2048,
                   longhorn_v2_iobuf_small_pool_size=16384)
    elif name in ('exclusive', 'combined'):
        row.update(longhorn_v2_cpu_cores=2, longhorn_v2_cpu_mask='0x1',
                   longhorn_v2_hugepages=name != 'combined')
    elif name != 'original':
        raise ValueError('unknown runtime resource profile')
    return row


def cpus(text):
    if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', text):
        raise ValueError('invalid actual CPU set')
    result = set()
    for part in text.split(','):
        ends = [int(x) for x in part.split('-')]
        if ends[-1] < ends[0]:
            raise ValueError('reversed actual CPU range')
        result.update(range(ends[0], ends[-1]+1))
    return result


def pool_bytes(row):
    return (row['longhorn_v2_iobuf_large_pool_size']*132 + row['longhorn_v2_iobuf_small_pool_size']*8)*1024


def native(lab, original=None):
    state = lab.owner(); namespace = lab.get('namespace', NS)
    if not namespace or namespace['metadata']['uid'] != state['namespace_uid']:
        raise ValueError('runtime resource installation identity changed')
    settings = {}
    for key, name in FIELDS.items():
        setting = lab.get('settings.longhorn.io', name, NS)
        if not setting or (original and setting['metadata']['uid'] != original['settings'][key]['uid']):
            raise ValueError('runtime resource Setting identity changed')
        settings[key] = {'uid': setting['metadata']['uid'], 'value': setting['value']}
    nodes = {'longhorn-node-'+n.rsplit('.', 1)[1] for n in lab.WORKERS}
    managers = lab.get('instancemanagers.longhorn.io', namespace=NS)['items']
    if (len(managers) != 3 or {m['spec'].get('nodeID') for m in managers} != nodes or
            any(m['spec'].get('dataEngine') != 'v2' or m['spec'].get('type') != 'aio' for m in managers)):
        raise ValueError('runtime resources require the original three AIO managers')
    identities = {m['metadata']['name']: {'uid': m['metadata']['uid'], 'spec': m['spec']} for m in managers}
    if original and identities != original['managers']:
        raise ValueError('runtime resource native manager identities changed')
    return {'settings': settings, 'managers': identities}


def pod_ready(pod):
    return bool(pod and not pod['metadata'].get('deletionTimestamp') and pod.get('status', {}).get('phase') == 'Running' and
                any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', [])))


def components(lab, desired, original=None):
    row = native(lab, original); result = {}
    for key, value in desired.items():
        expected = str(value).lower() if isinstance(value, bool) else str(value)
        if json.loads(row['settings'][key]['value']).get('v2') != expected:
            return None
    for name, identity in row['managers'].items():
        im = lab.get('instancemanagers.longhorn.io', name, NS)
        pod = lab.get('pod', name, NS)
        if not pod_ready(pod) or im.get('status', {}).get('currentState') != 'running':
            return None
        if (pod['spec'].get('nodeName') != identity['spec']['nodeID'] or
                not any(r.get('kind') == 'InstanceManager' and r.get('uid') == identity['uid'] and r.get('controller')
                        for r in pod['metadata'].get('ownerReferences', []))):
            raise ValueError('runtime resource Pod owner or node changed')
        containers = pod['spec']['containers']
        if len(containers) != 1 or containers[0]['name'] != 'instance-manager' or containers[0]['image'] != identity['spec']['image']:
            raise ValueError('runtime resource Pod image changed')
        container = containers[0]; args = container.get('args', [])
        def value(flag):
            if args.count(flag) != 1 or args.index(flag)+1 >= len(args):
                return None
            return args[args.index(flag)+1]
        cores = desired['longhorn_v2_cpu_cores']
        if (value('--spdk-core-number') != str(cores) or value('--spdk-memory-size') != str(desired['longhorn_v2_memory_mib']) or
                value('--spdk-cpumask') != ('' if cores else desired['longhorn_v2_cpu_mask']) or
                ('--spdk-no-hugepage' in args) != (not desired['longhorn_v2_hugepages'])):
            return None
        for field, flag, default in (('longhorn_v2_iobuf_large_pool_size', '--spdk-iobuf-large-pool-size', 1024),
                                      ('longhorn_v2_iobuf_small_pool_size', '--spdk-iobuf-small-pool-size', 8192)):
            expected = str(desired[field]) if desired[field] > default else None
            if value(flag) != expected or (expected is None and flag in args):
                return None
        if any(flag in args for flag in ('--enable-irq-affinity', '--enable-workqueue-affinity', '--enable-rps')):
            raise ValueError('runtime resource gate refuses host CPU-isolation side effects')
        product = lab.load_helper('longhorn-v2-resources.py').product()
        resources = container.get('resources', {})
        if cores:
            if lab.get('nodes.longhorn.io',identity['spec']['nodeID'],NS).get('status',{}).get('cpuPolicy') != 'static':
                return None
            requests = resources.get('requests', {}); limits = resources.get('limits', {})
            cpu = product.quantity(requests.get('cpu', 0))
            if (pod['status'].get('qosClass') != 'Guaranteed' or cpu < cores or cpu != int(cpu) or
                    product.quantity(limits.get('cpu', 0)) != cpu or requests.get('memory') != limits.get('memory') or
                    im.get('status', {}).get('dataEngineStatus', {}).get('v2', {}).get('cpuCoreNumber') != cores):
                return None
        result[name] = {'pod_uid': pod['metadata']['uid'], 'node': identity['spec']['nodeID'], 'args': args,
                        'resources': resources, 'restarts': [c.get('restartCount', 0) for c in pod['status'].get('containerStatuses', [])]}
    return {'native': row, 'pods': result}


def runtime_script():
    """Read actual child argv, effective cgroup and SPDK's live iobuf options."""
    return '''import json,os,re,socket,stat,sys
from pathlib import Path
processes=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  args=[s.decode() for s in (p/'cmdline').read_bytes().split(b'\\0') if s]
  if args and Path(args[0]).name=='spdk_tgt':processes.append((p,args))
 except (OSError,UnicodeError):continue
assert len(processes)==1,'expected one native SPDK process'
p,args=processes[0]
row={'pid':int(p.name),'argv':args,'start_time':(p/'stat').read_text().rsplit(')',1)[1].split()[19]}
status=dict(re.findall(r'^(VmRSS|HugetlbPages):\\s+(\\d+) kB', (p/'status').read_text(),re.M))
row['rss_bytes']=int(status.get('VmRSS','0'))*1024;row['hugetlb_bytes']=int(status.get('HugetlbPages','0'))*1024
# DPDK's --no-huge reserves a memfd mapping; untouched pages need not be resident.
files={}
for descriptor in (p/'fd').iterdir():
 try:
  if os.readlink(descriptor) != '/memfd:nohuge (deleted)':continue
  observed=descriptor.stat()
 except FileNotFoundError:continue
 key=(os.major(observed.st_dev),os.minor(observed.st_dev),observed.st_ino)
 files[key]={'device':('%02x:%02x' % key[:2]),'inode':key[2],'size_bytes':observed.st_size}
mappings=[]
for line in (p/'maps').read_text().splitlines():
 fields=line.split(None,5)
 major,minor=(int(v,16) for v in fields[3].split(':'));inode=int(fields[4])
 if (major,minor,inode) not in files:continue
 start,end=(int(v,16) for v in fields[0].split('-'))
 mappings.append({'device':('%02x:%02x' % (major,minor)),'inode':inode,
                  'offset':int(fields[2],16),'size_bytes':end-start,'perms':fields[1]})
row['nohuge']={'files':list(files.values()),'mappings':mappings}
cgroup=[s.split(':',2)[2] for s in (p/'cgroup').read_text().splitlines() if s.startswith('0::')]
assert len(cgroup)==1,'runtime scope requires cgroup v2'
target=Path('/host/sys/fs/cgroup')
assert (target/'cgroup.controllers').is_file(),'expected wrapper host cgroup v2 mount'
path=(target/cgroup[0].lstrip('/')).resolve(strict=True)
assert target in path.parents,'cgroup escape'
limits=[];current=path
while True:
 control=current/'memory.max'
 if control.exists():
  value=control.read_text().strip()
  if value!='max':limits.append(int(value))
 else:assert current==target,'missing non-root cgroup memory controller'
 if current==target:break
 assert target in current.parents,'cgroup escape'
 current=current.parent
row.update(memory_limit=min(limits) if limits else None,memory_current=int((path/'memory.current').read_text()),
 memory_events={k:int(v) for k,v in (s.split() for s in (path/'memory.events').read_text().splitlines())},
 cpuset=(path/'cpuset.cpus.effective').read_text().strip())
config=Path('/tmp/spdk-startup.json');row['startup_json']=json.loads(config.read_text()) if config.exists() else None
request={'jsonrpc':'2.0','id':1,'method':'framework_get_config','params':{'name':'iobuf'}}
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(10);s.connect('/var/tmp/spdk.sock')
s.sendall(json.dumps(request).encode()+b'\\n');data=b''
while True:
 block=s.recv(65536);assert block,'SPDK closed JSON-RPC before response';data+=block
 try:reply=json.loads(data);break
 except json.JSONDecodeError:assert len(data)<1024**2,'unbounded SPDK response'
s.close();assert reply.get('id')==1 and 'error' not in reply,'SPDK live iobuf query failed'
row['iobuf_runtime']=reply['result']
print(json.dumps(row,sort_keys=True))
'''


def assert_runtime(row, desired, pod):
    memory = desired['longhorn_v2_memory_mib']*1024**2
    if pool_bytes(desired) >= memory:
        raise ValueError('runtime iobuf pools exhaust SPDK budget')
    args = row['argv']
    def value(flag):
        if args.count(flag) != 1 or args.index(flag)+1 >= len(args):
            raise ValueError('runtime SPDK argument missing or duplicated: '+flag)
        return args[args.index(flag)+1]
    if value('--mem-size') != str(desired['longhorn_v2_memory_mib']) or ('--no-huge' in args) != (not desired['longhorn_v2_hugepages']):
        raise ValueError('actual SPDK memory mode differs from product profile')
    if not desired['longhorn_v2_hugepages']:
        if row['memory_limit'] is not None and row['memory_limit'] < memory+128*1024**2:
            raise ValueError('actual cgroup memory limit cannot cover anonymous SPDK plus IM headroom')
        reservation = row.get('nohuge', {})
        files = reservation.get('files', [])
        mappings = reservation.get('mappings', [])
        if len(files) != 1 or files[0]['size_bytes'] != memory or not mappings:
            raise ValueError('anonymous SPDK memfd reservation differs from configured memory')
        identity = files[0]['device'], files[0]['inode']
        offset = 0
        for mapping in sorted(mappings, key=lambda item: item['offset']):
            if ((mapping['device'], mapping['inode']) != identity or
                    mapping['offset'] != offset or mapping['size_bytes'] <= 0 or
                    mapping['perms'] != 'rw-s'):
                raise ValueError('anonymous SPDK mapping has a gap, overlap or foreign identity')
            offset += mapping['size_bytes']
        if offset != memory:
            raise ValueError('anonymous SPDK mapped extent differs from configured reservation')
    elif row['hugetlb_bytes'] < memory*9//10:
        raise ValueError('actual SPDK hugepage allocation is below the configured preallocation')
    if any(row['memory_events'].get(k, 0) for k in ('oom', 'oom_kill')):
        raise ValueError('runtime IM has an OOM event')
    assigned = cpus(row['cpuset'])
    mask_flag = '-m' if '-m' in args else '--cpumask'
    mask = int(value(mask_flag), 16)
    reactors = {n for n in range(mask.bit_length()) if mask & (1 << n)}
    if not reactors <= assigned:
        raise ValueError('actual SPDK CPU mask escapes the container CPU set')
    if desired['longhorn_v2_cpu_cores']:
        if len(reactors) != desired['longhorn_v2_cpu_cores'] or not reactors <= assigned:
            raise ValueError('actual SPDK cores are outside the exclusive container CPU set')
    elif mask != int(desired['longhorn_v2_cpu_mask'], 16):
        raise ValueError('actual SPDK CPU mask differs from product profile')
    entries = row['iobuf_runtime']
    options = [e['params'] for e in entries if e.get('method') == 'iobuf_set_options']
    if len(options) != 1 or any(options[0].get(k) != desired[f] for k, f in
            (('large_pool_count', 'longhorn_v2_iobuf_large_pool_size'), ('small_pool_count', 'longhorn_v2_iobuf_small_pool_size'))):
        raise ValueError('actual SPDK iobuf options differ from product pools')
    if (options[0].get('large_bufsize'),options[0].get('small_bufsize')) != (132*1024,8*1024):
        raise ValueError('actual SPDK buffer sizes differ from the admitted shared memory budget')
    tuned = desired['longhorn_v2_iobuf_large_pool_size'] > 1024 or desired['longhorn_v2_iobuf_small_pool_size'] > 8192
    if ('--json' in args) != tuned:
        raise ValueError('actual SPDK startup flags did not reset to defaults')
    if tuned:
        if value('--json') != '/tmp/spdk-startup.json':
            raise ValueError('runtime SPDK startup JSON path differs')
        startup = [c for s in row['startup_json']['subsystems'] if s['subsystem'] == 'iobuf'
                   for c in s['config'] if c['method'] == 'iobuf_set_options']
        if len(startup) != 1 or any(startup[0]['params'].get(k) != options[0][k]
                                  for k in ('large_pool_count', 'small_pool_count')):
            raise ValueError('SPDK startup JSON does not match live iobuf pools')
    elif row['startup_json'] is not None:
        raise ValueError('default SPDK Pod retained stale tuning JSON')
    if any(pod['restarts']):
        raise ValueError('runtime resource Pod restarted during profile convergence')


def runtime(lab, desired, original):
    current = lab.wait('runtime-resource-native-ready', 1200, lambda: components(lab, desired, original))
    rows = {}
    for name, pod in current['pods'].items():
        row = json.loads(lab.kubectl('-n', NS, 'exec', name, '-c', 'instance-manager', '--',
                                    'python3', '-c', runtime_script(), timeout=60).stdout)
        print('LONGHORN_V2_RUNTIME_OBSERVED '+json.dumps({'pod': name, 'desired': desired, 'state': row}, sort_keys=True), flush=True)
        assert_runtime(row, desired, pod)
        if desired['longhorn_v2_cpu_cores']:
            address = next(n for n in lab.NODES if 'longhorn-node-'+n.rsplit('.',1)[1] == pod['node'])
            host = cpu_remote(lab, {'node':address,'run_id':lab.owner()['run_id']}, 'observe')
            request = load_record(lab)['cpu_hosts'][address]['request']
            if (host['machine_id'] != request['original']['machine_id'] or
                    host['boot_id'] != request['original']['boot_id'] or host['policy'] != 'static' or
                    cpus(host['entries'].get(pod['pod_uid'],{}).get('instance-manager','')) != cpus(row['cpuset'])):
                raise ValueError('actual CPU checkpoint does not bind the owned exclusive IM cpuset')
            row['checkpoint'] = host
        rows[name] = row
    current['runtime'] = rows
    return current


def host_budget(lab, desired):
    script = '''import json,re
from pathlib import Path
info={k:int(v) for k,v in re.findall(r'^(MemAvailable|MemTotal|HugePages_Total|Hugepagesize):\\s+(\\d+)',Path('/proc/meminfo').read_text(),re.M)}
print(json.dumps(info,sort_keys=True))
'''
    rows = {}
    for node in lab.NODES:
        row = json.loads(lab.remote(node, ['python3','-c',script]).stdout)
        if desired['longhorn_v2_hugepages']:
            if row['HugePages_Total']*row['Hugepagesize'] < desired['longhorn_v2_memory_mib']*1024:
                raise ValueError('runtime profile exceeds the original hugepage pool')
            required = 1024**2 + 128*1024
        else:
            required = desired['longhorn_v2_memory_mib']*1024 + 1024**2
        if row['MemAvailable'] < required:
            raise ValueError('runtime resource profile lacks measured memory headroom: '+node)
        rows[node] = row
    return rows


def cpu_action(request, action, root='/'):
    """Stopped-kubelet transition; preserve the real original checkpoint."""
    import base64
    import json
    import os
    from pathlib import Path
    import stat
    import subprocess
    import tempfile
    import uuid
    root = Path(root); run_id = str(uuid.UUID(request['run_id']))
    config = root/'var/lib/kubelet/config.yaml'; checkpoint = root/'var/lib/kubelet/cpu_manager_state'
    backup = checkpoint.with_name('.kubeauto-longhorn-runtime-'+run_id+'.cpu-state')
    def command(args):
        row = subprocess.run(args, text=True, capture_output=True, timeout=60)
        if row.returncode:raise ValueError('runtime CPU fixture command failed: '+args[0])
        return row.stdout.strip()
    def file(path):
        if path.is_symlink() or not path.is_file():raise ValueError('runtime CPU fixture requires regular files')
        s=path.stat();return {'data':base64.b64encode(path.read_bytes()).decode(),'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid}
    def machine():
        return {'machine_id':(root/'etc/machine-id').read_text().strip(),'boot_id':(root/'proc/sys/kernel/random/boot_id').read_text().strip()}
    def state():
        row=file(checkpoint);value=json.loads(base64.b64decode(row['data']))
        if value.get('policyName') not in ('none','static') or value.get('entries'):
            raise ValueError('runtime CPU fixture refuses existing exclusive allocations')
        return row
    def write(data):
        with tempfile.NamedTemporaryFile('wb', dir=config.parent,delete=False) as stream:
            stream.write(base64.b64decode(data['data']));stream.flush();os.fsync(stream.fileno());temp=Path(stream.name)
        os.chmod(temp,data['mode']);os.chown(temp,data['uid'],data['gid']);temp.replace(config)
    if action=='observe':
        value=json.loads(base64.b64decode(file(checkpoint)['data']))
        return dict(machine(),policy=value['policyName'],entries=value.get('entries',{}))
    if action=='capture':
        if backup.exists() or backup.is_symlink():raise ValueError('runtime CPU fixture backup already exists')
        marker=root/'var/lib/longhorn/instance-manager/v2/spdk_cpu_mask'
        if marker.exists() or marker.is_symlink():raise ValueError('runtime CPU fixture refuses historical host CPU isolation')
        return dict(machine(),config=file(config),checkpoint=state(),active=command(['systemctl','is-active','kubelet']))
    if action not in ('install','restore'):raise ValueError('invalid CPU fixture action')
    original=request['original']
    if machine()!={k:original[k] for k in ('machine_id','boot_id')} or original['active']!='active':
        raise ValueError('runtime CPU fixture host or boot identity changed')
    desired=dict(original['config'],data=request['desired'])
    if file(config) not in (original['config'],desired):raise ValueError('runtime CPU fixture config changed externally')
    if action=='install' and file(config)!=original['config']:raise ValueError('runtime CPU fixture was already changed')
    if checkpoint.exists():state()
    command(['systemctl','stop','kubelet'])
    try:
        if action=='install':
            if state()!=original['checkpoint'] or backup.exists() or backup.is_symlink():
                raise ValueError('runtime CPU fixture original checkpoint changed')
            checkpoint.rename(backup);write(desired)
        else:
            if backup.exists():
                if file(backup)!=original['checkpoint']:raise ValueError('runtime CPU fixture foreign checkpoint backup')
                if checkpoint.exists():
                    state();checkpoint.unlink()
                write(original['config']);backup.rename(checkpoint)
            else:
                if file(config)!=original['config'] or state()!=original['checkpoint']:
                    raise ValueError('runtime CPU fixture has no recoverable checkpoint ownership')
    finally:
        command(['systemctl','start','kubelet'])
    return {'restored':action=='restore'}


def cpu_remote(lab, request, action):
    script = inspect.getsource(cpu_action)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(cpu_action(r["request"],r["action"]),sort_keys=True))\n'
    return json.loads(lab.remote(request['node'], ['python3', '-c', script],
                                stdin=json.dumps({'request': request, 'action': action}), timeout=240).stdout)


def desktop_action(request, action, root='/'):
    """Quiesce only the lab login screen; preserve its original active state."""
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    root=Path(root)
    def command(argv):
        row=subprocess.run(argv,text=True,capture_output=True,timeout=60)
        if row.returncode:raise ValueError('runtime desktop fixture command failed: '+argv[0])
        return row.stdout
    def observe():
        props=dict(line.split('=',1) for line in command(['systemctl','show','display-manager.service',
            '--property=Id,LoadState,ActiveState,FragmentPath']).splitlines() if '=' in line)
        machine={'machine_id':(root/'etc/machine-id').read_text().strip(),
                 'boot_id':(root/'proc/sys/kernel/random/boot_id').read_text().strip()}
        if props.get('LoadState')=='not-found':return dict(machine,unit=None,active='inactive',fragment_sha256=None)
        if (props.get('LoadState')!='loaded' or props.get('Id') not in ('gdm.service','gdm3.service') or
                props.get('ActiveState') not in ('active','inactive')):
            raise ValueError('runtime desktop fixture refuses unknown login service state')
        fragment=Path(props['FragmentPath'])
        if not fragment.is_absolute() or '..' in fragment.parts:raise ValueError('invalid desktop unit path')
        fragment=root/str(fragment).lstrip('/')
        if fragment.is_symlink() or not fragment.is_file():raise ValueError('desktop unit is not a regular file')
        return dict(machine,unit=props['Id'],active=props['ActiveState'],
                    fragment_sha256=hashlib.sha256(fragment.read_bytes()).hexdigest())
    def no_users():
        sessions=command(['loginctl','list-sessions','--no-legend','--no-pager']).splitlines()
        for session in sessions:
            if not session.strip():continue
            value=dict(line.split('=',1) for line in command(['loginctl','show-session',session.split()[0],
                '--property=Class,Type']).splitlines() if '=' in line)
            if value.get('Type') in ('x11','wayland') and value.get('Class')!='greeter':
                raise ValueError('runtime desktop fixture refuses an interactive graphical session')
    before=observe()
    if action=='capture':no_users();return before
    if action not in ('stop','restore'):raise ValueError('unknown desktop fixture action')
    original=request['original']
    if any(before.get(k)!=original.get(k) for k in ('machine_id','boot_id','unit','fragment_sha256')):
        raise ValueError('runtime desktop fixture host or unit changed')
    if original['active'] not in ('active','inactive'):raise ValueError('invalid original desktop state')
    no_users()
    desired='inactive' if action=='stop' else original['active']
    if before['active']!=desired:
        command(['systemctl','start' if desired=='active' else 'stop',original['unit']])
    after=observe()
    if any(after.get(k)!=(desired if k=='active' else original.get(k)) for k in original):
        raise ValueError('runtime desktop fixture state did not converge')
    return after


def desktop_remote(lab,request,action):
    script=inspect.getsource(desktop_action)+'\nimport json,sys\nr=json.load(sys.stdin)\nprint(json.dumps(desktop_action(r["request"],r["action"]),sort_keys=True))\n'
    return json.loads(lab.remote(request['node'],['python3','-c',script],
        stdin=json.dumps({'request':request,'action':action}),timeout=180).stdout)


def prepare_desktops(lab):
    row=load_record(lab)
    for node in lab.NODES:
        request={'node':node};original=desktop_remote(lab,request,'capture')
        if original['machine_id']!=lab.owner()['raw_disks'][node]['machine_id']:
            raise ValueError('desktop fixture host machine differs from owned raw disk host')
        request['original']=original
        row.setdefault('desktop_hosts',{})[node]={'request':request,'phase':'planned'}
        lab.write_private(lab.DIRECTORY/RECORD,row)
    for host in row['desktop_hosts'].values():
        host['phase']='stop-intent';lab.write_private(lab.DIRECTORY/RECORD,row)
        host['observed']=desktop_remote(lab,host['request'],'stop');host['phase']='stopped'
        lab.write_private(lab.DIRECTORY/RECORD,row)


def restore_desktops(lab):
    row=load_record(lab)
    for host in reversed(list(row.get('desktop_hosts',{}).values())):
        if host['phase'] in ('planned','restored'):continue
        desktop_remote(lab,host['request'],'restore');host['phase']='restored'
        lab.write_private(lab.DIRECTORY/RECORD,row)


def load_record(lab):
    path = lab.DIRECTORY/RECORD
    if path.is_symlink():raise ValueError('symlink runtime resource journal')
    row = json.loads(path.read_text()); state = lab.owner()
    if any(row[k] != state[k] for k in ('run_id','cluster_uid','namespace_uid')):
        raise ValueError('foreign runtime resource journal')
    if not set(row.get('cpu_hosts',{})) <= set(lab.NODES):
        raise ValueError('runtime CPU fixture journal escapes the leased source pool')
    for node,host in row.get('desktop_hosts',{}).items():
        if (node not in lab.NODES or host['request']['node']!=node or
                host['request']['original']['machine_id']!=state['raw_disks'][node]['machine_id'] or
                host['phase'] not in ('planned','stop-intent','stopped','restored')):
            raise ValueError('runtime desktop fixture journal ownership changed')
    for node,host in row.get('cpu_hosts',{}).items():
        request=host['request'];original=request['original']
        if (request['node']!=node or request['run_id']!=row['run_id'] or
                original['machine_id']!=state['raw_disks'][node]['machine_id'] or
                host['phase'] not in ('planned','drain-intent','write-intent','static','restored')):
            raise ValueError('runtime CPU fixture journal ownership changed')
        intended=yaml.safe_load(base64.b64decode(original['config']['data']))
        intended.update(cpuManagerPolicy='static',reservedSystemCPUs='0')
        if base64.b64decode(request['desired'])!=json.dumps(intended).encode():
            raise ValueError('runtime CPU fixture desired config changed')
    return row


def product(lab, pid, desired, refusal=None):
    row=load_record(lab);path=lab.DIRECTORY/'config.yml';receipt=lab.DIRECTORY/'longhorn-request-input.json'
    if path.is_symlink() or path.read_text()!=row['original_config'] or receipt.exists() or receipt.is_symlink():
        raise ValueError('runtime resource input changed or has an unresolved operation')
    config=yaml.safe_load(row['original_config']);config.update(desired)
    text=yaml.safe_dump(config,sort_keys=False)
    lab.write_private(receipt,{'run_id':row['run_id'],'original':row['original_config'],'desired':text})
    with lab.scoped_cleanup(lab.restore_request_input):
        with tempfile.NamedTemporaryFile('w',dir=lab.DIRECTORY,delete=False) as stream:
            stream.write(text);stream.flush();os.fsync(stream.fileno());temp=Path(stream.name)
        temp.chmod(0o600);temp.replace(path)
        lab.supervise_core(pid,'longhorn-setup',expected_refusal=refusal)


def assert_cpu_refusals(proof, config):
    """Verify both pre-host product refusals and their original process identities."""
    expected = {'exclusive-without-static': (profile(config, 'exclusive'), CPU_POLICY_REFUSAL),
                'under-reserved-mask': (dict(profile(config, 'original'),
                    longhorn_v2_cpu_reservation_percentage=1), CPU_RESERVATION_REFUSAL)}
    if set(proof) != set(expected): raise ValueError('runtime CPU refusal coverage is incomplete')
    for name, (desired, reason) in expected.items():
        row = proof[name]; before = row['before']; after = row['after']
        if (row.get('desired') != desired or row.get('reason') != reason or
                row.get('policies_after') != row.get('policies') or
                row.get('policies') != {'longhorn-node-'+a.rsplit('.',1)[1]: 'none' for a in
                                       ('192.168.47.146','192.168.47.147','192.168.47.148')} or
                before.get('native') != after.get('native') or before.get('pods') != after.get('pods') or
                not row.get('disk_before') or row['disk_before'] != row.get('disk_after') or
                not re.fullmatch(r'[a-f0-9]{64}',row.get('config_before','')) or
                row['config_before'] != row.get('config_after') or row.get('receipt_absent') is not True or
                len(before.get('runtime',{})) != 3 or set(before['runtime']) != set(after.get('runtime',{})) or
                set(before['runtime']) != set(before.get('pods',{}))):
            raise ValueError('runtime CPU refusal changed its original config or native identity')
        for manager, original in before['runtime'].items():
            observed = after['runtime'][manager]
            for actual in (original, observed): assert_runtime(actual, profile(config,'original'), before['pods'][manager])
            keys = ('pid','start_time','argv','cpuset','memory_limit','iobuf_runtime','startup_json')
            if (not original.get('pid') or not original.get('start_time') or
                    any(original[key] != observed[key] for key in keys)):
                raise ValueError('runtime CPU refusal replaced or reconfigured the original SPDK process')
    return proof


def cpu_refusals(lab, pid, config, identity, original_runtime):
    """Exercise the original public config+kubecli path before CPU preparation."""
    proof = {}; before = original_runtime
    policies = {'longhorn-node-'+a.rsplit('.',1)[1]: lab.get('nodes.longhorn.io',
                'longhorn-node-'+a.rsplit('.',1)[1],NS)['status'].get('cpuPolicy') for a in lab.WORKERS}
    if set(policies.values()) != {'none'}:
        raise ValueError('CPU refusal precondition requires the original none policy')
    for name, desired, reason in (
            ('exclusive-without-static',profile(config,'exclusive'),CPU_POLICY_REFUSAL),
            ('under-reserved-mask',dict(profile(config,'original'),
                longhorn_v2_cpu_reservation_percentage=1),CPU_RESERVATION_REFUSAL)):
        path=lab.DIRECTORY/'config.yml'; config_before=hashlib.sha256(path.read_bytes()).hexdigest()
        disk_before=baseline_identity(lab)
        # product() restores the original request in its scoped finally. Its owning
        # supervisor requires a nonzero exit containing this exact product error.
        product(lab,pid,desired,reason)
        after=runtime(lab,profile(config,'original'),identity)
        proof[name]={'desired':desired,'reason':reason,'policies':policies,
            'before':before,'after':after,'policies_after':{n:lab.get('nodes.longhorn.io',n,NS)['status'].get('cpuPolicy') for n in policies},
            'disk_before':disk_before,'disk_after':baseline_identity(lab),
            'config_before':config_before,'config_after':hashlib.sha256(path.read_bytes()).hexdigest(),
            'receipt_absent':not ((lab.DIRECTORY/'longhorn-request-input.json').exists() or
                                  (lab.DIRECTORY/'longhorn-request-input.json').is_symlink())}
        before=after
    assert_cpu_refusals(proof,config)
    print('LONGHORN_V2_RUNTIME_CPU_REFUSALS '+json.dumps(proof,sort_keys=True),flush=True)
    return proof


def baseline_identity(lab):
    state=lab.owner();receipt=lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if receipt.is_symlink():raise ValueError('symlink runtime native disk receipt')
    disks=json.loads(receipt.read_text());result={}
    for address,row in disks['nodes'].items():
        node=lab.get('nodes.longhorn.io',row['name'],NS)
        if not node or node['metadata']['uid']!=row['longhorn_node_uid']:raise ValueError('runtime native node identity changed')
        for name,disk in row['disks'].items():
            actual=node['status'].get('diskStatus',{}).get(name,{}).get('diskUUID')
            if actual!=disk['disk_uuid']:raise ValueError('runtime native disk UUID changed')
            result[address+'/'+name]=actual
    for name,key in (('longhorn-grpc-tls','tls_uid'),('longhorn-volume-key','encryption_uid')):
        if lab.get('secret',name,NS)['metadata']['uid']!=state[key]:raise ValueError('runtime credential identity changed')
    return {'disks':result,'receipt_sha256':hashlib.sha256(receipt.read_bytes()).hexdigest()}


def prepare_cpus(lab):
    row=load_record(lab)
    if lab.get('volumes.longhorn.io',namespace=NS)['items']:
        raise ValueError('CPU policy fixture requires no volumes before drain')
    for node in lab.NODES:
        name='longhorn-node-'+node.rsplit('.',1)[1]
        if node not in row['cpu_hosts']:
            request={'node':node,'run_id':row['run_id']};before=cpu_remote(lab,request,'capture')
            if before['machine_id']!=lab.owner()['raw_disks'][node]['machine_id']:
                raise ValueError('CPU fixture host machine differs from owned raw disk host')
            config=yaml.safe_load(base64.b64decode(before['config']['data']))
            if config.get('cpuManagerPolicy','none')!='none' or config.get('reservedSystemCPUs'):
                raise ValueError('runtime CPU fixture requires unchanged default kubelet policy')
            config.update(cpuManagerPolicy='static',reservedSystemCPUs='0')
            request.update(original=before,desired=base64.b64encode(json.dumps(config).encode()).decode())
            row['cpu_hosts'][node]={'request':request,'phase':'planned','unschedulable':bool(lab.get('node',name)['spec'].get('unschedulable'))}
            lab.write_private(lab.DIRECTORY/RECORD,row)
    # Every host and Pod is admitted before the first drain or file mutation.
    for pod in lab.get('pods',namespace='*')['items']:
        if pod['metadata'].get('namespace') not in ('kube-system','calico-system','longhorn-system'):
            raise ValueError('CPU fixture refuses foreign workload namespaces')
        if pod.get('status',{}).get('qosClass')=='Guaranteed':
            raise ValueError('CPU fixture refuses existing Guaranteed workload allocations')
    for node,host in row['cpu_hosts'].items():
        if host['phase']!='planned':raise ValueError('CPU fixture requires exact recovery before retry')
        name='longhorn-node-'+node.rsplit('.',1)[1]
        host['phase']='drain-intent';lab.write_private(lab.DIRECTORY/RECORD,row)
        lab.kubectl('drain',name,'--ignore-daemonsets','--delete-emptydir-data','--timeout=300s',timeout=330)
        host['phase']='write-intent';lab.write_private(lab.DIRECTORY/RECORD,row)
        cpu_remote(lab,host['request'],'install')
        lab.wait('runtime-static-cpu-policy-'+name,180,lambda: lab.get('nodes.longhorn.io',name,NS).get('status',{}).get('cpuPolicy')=='static')
        if not host['unschedulable']:lab.kubectl('uncordon',name)
        host['phase']='static';lab.write_private(lab.DIRECTORY/RECORD,row)
        print('LONGHORN_V2_RUNTIME_STATIC_READY node='+node,flush=True)
    lab.core_identity()


def restore_cpus(lab):
    row=load_record(lab)
    for node,host in reversed(list(row['cpu_hosts'].items())):
        if host['phase'] in ('planned','restored'):continue
        name='longhorn-node-'+node.rsplit('.',1)[1]
        if lab.get('volumes.longhorn.io',namespace=NS)['items']:
            raise ValueError('runtime CPU restoration requires completed volume cleanup')
        lab.kubectl('drain',name,'--ignore-daemonsets','--delete-emptydir-data','--timeout=300s',timeout=330)
        lab.wait('runtime-exclusive-allocations-released-'+name,180,
                 lambda: not cpu_remote(lab,host['request'],'observe')['entries'])
        cpu_remote(lab,host['request'],'restore')
        lab.wait('runtime-original-cpu-policy-'+name,180,lambda: lab.get('nodes.longhorn.io',name,NS).get('status',{}).get('cpuPolicy')=='none')
        if not host['unschedulable']:lab.kubectl('uncordon',name)
        host['phase']='restored';lab.write_private(lab.DIRECTORY/RECORD,row)
    lab.core_identity()


def only_business(lab, journal):
    row=load_record(lab)
    if any(journal.get(k)!=row.get('business',{}).get(k) for k in ('namespace','namespace_uid','volume_uid')):
        raise ValueError('runtime resource business journal belongs to another module')
    return lab.load_helper('longhorn-v2-interrupt.py').only_business(lab,journal)


def detach(lab,journal):
    only_business(lab,journal)
    lab.load_helper('longhorn-v2-interrupt.py').stop_clients(lab,journal)


def client(lab,journal,name,index):
    lifecycle=lab.load_helper('longhorn-v2-clone-lifecycle.py')
    lab.create_business_object(journal,lab.DIRECTORY/BUSINESS,lab.client_pod(journal['namespace'],name,'data',
                               'longhorn-node-'+lab.WORKERS[index].rsplit('.',1)[1]))
    lab.wait('runtime-client-'+name,600,lambda:pod_ready(lifecycle.current(lab,'pod',name,journal['objects']['Pod/'+name],journal['namespace'])))


def observe_io(lab,journal,name,token):
    lifecycle=lab.load_helper('longhorn-v2-clone-lifecycle.py');lifecycle.io(lab,journal,name,token,read=True)
    faults=lab.load_helper('longhorn-faults.py');control=lab.load_helper('longhorn-control-faults.py')
    ledger_token=token+':'+name
    path='/data/runtime-ledger-'+hashlib.sha256(ledger_token.encode()).hexdigest()
    program=faults.ledger_program(ledger_token,duration=15).replace('stop=ledger_stop_requested','stop=None').replace('/data/fault-ledger',path)
    output=lab.kubectl('-n',journal['namespace'],'exec',name,'--','python3','-u','-c',program,timeout=60).stdout
    ledger=control.confirmed([json.loads(line) for line in output.splitlines()],ledger_token,faults)
    def kubectl(*args,**kwargs):
        args=tuple(arg.replace('/data/fault-ledger',path) if isinstance(arg,str) else arg for arg in args)
        return lab.kubectl(*args,**kwargs)
    faults.verify_client_ledger(SimpleNamespace(kubectl=kubectl),journal,name,ledger)
    return ledger


def business_namespace(lab, *, create=False):
    """Recover a lost namespace-create reply only by our private operation token."""
    row=load_record(lab);intent=row.get('namespace_intent')
    if create:
        if intent:raise ValueError('runtime namespace creation is already journaled')
        name='longhorn-business-'+secrets.token_hex(8)
        if lab.get('namespace',name):raise ValueError('runtime namespace name already exists')
        intent={'name':name,'token':uuid.uuid4().hex}
        row['namespace_intent']=intent;lab.write_private(lab.DIRECTORY/RECORD,row)
        obj={'apiVersion':'v1','kind':'Namespace','metadata':{'name':name,'annotations':{
            'kubeauto.io/runtime-resource-operation':intent['token']}}}
        lab.kubectl('create','-f','-','-o','json',stdin=json.dumps(obj))
    if not intent:return None
    namespace=lab.get('namespace',intent['name'])
    if not namespace:
        if create:raise ValueError('runtime namespace creation did not converge')
        return None
    if namespace['metadata'].get('annotations',{}).get('kubeauto.io/runtime-resource-operation')!=intent['token']:
        raise ValueError('runtime namespace create intent belongs to a foreign object')
    path=lab.DIRECTORY/BUSINESS
    journal={'namespace':intent['name'],'namespace_uid':namespace['metadata']['uid'],
             'objects':{},'storage_class':'longhorn','reclaim_policy':'Retain'}
    if path.exists() or path.is_symlink():
        if path.is_symlink():raise ValueError('symlink runtime business namespace journal')
        journal=json.loads(path.read_text())
        if journal['namespace']!=intent['name'] or journal['namespace_uid']!=namespace['metadata']['uid']:
            raise ValueError('runtime business namespace identity changed')
    row['business']={k:journal[k] for k in ('namespace','namespace_uid')}
    if journal.get('volume_uid'):row['business']['volume_uid']=journal['volume_uid']
    lab.write_private(lab.DIRECTORY/RECORD,row)
    lab.write_private(path,journal)
    return journal


def cleanup(lab,pid,*,failed=False):
    path=lab.DIRECTORY/RECORD
    if not path.exists() and not path.is_symlink():return
    row=load_record(lab);lab.restore_request_input()
    if row.get('namespace_intent'):
        business_namespace(lab)
        row=load_record(lab)
    record=lab.DIRECTORY/BUSINESS
    if record.exists():
        if record.is_symlink():raise ValueError('symlink runtime resource business journal')
        journal=json.loads(record.read_text())
        if any(journal.get(k)!=row.get('business',{}).get(k) for k in ('namespace','namespace_uid','volume_uid')):
            raise ValueError('runtime resource business journal belongs to another module')
        if journal.get('volume_uid'):
            only_business(lab,journal);detach(lab,journal)
        elif lab.get('volumes.longhorn.io',namespace=NS)['items']:
            raise ValueError('partial runtime business requires diagnosed volume ownership')
        if not journal.get('defaults'):
            journal['defaults']=lab.wait('runtime-namespace-defaults-before-cleanup',60,
                                        lambda:lab.namespace_defaults(journal['namespace']))
            lab.write_private(record,journal)
    # Rejected preflight requests also render input.json. Restore it through
    # the original product path before any health check consumes that file.
    product(lab,pid,profile(yaml.safe_load(row['original_config']),'original'))
    runtime(lab,profile(yaml.safe_load(row['original_config']),'original'),row['native'])
    if record.exists():lab.cleanup_business(relay_pid=pid)
    restore_cpus(lab)
    restore_desktops(lab)
    if baseline_identity(lab)!=row['baseline_identity'] or (lab.DIRECTORY/'config.yml').read_text()!=row['original_config']:
        raise ValueError('runtime resource cleanup changed the original disk/input identity')
    def restored_health():
        try:
            lab.installation_health()
        except ValueError as error:
            if str(error) != 'Longhorn control or disk health failed': raise
            return False
        return True
    lab.wait('runtime-restored-installation-health',600,restored_health)
    if failed:
        row=load_record(lab)
        lab.write_private(path.with_name('longhorn-v2-runtime-recovered-'+uuid.uuid4().hex+'.json'),row);path.unlink()
    print('LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS',flush=True)


def business(lab,pid):
    lab.run_requests(pid,[]);lab.installation_health()
    if any((lab.DIRECTORY/n).exists() or (lab.DIRECTORY/n).is_symlink() for n in (RECORD,BUSINESS,'longhorn-v2-interrupt.json')):
        raise ValueError('runtime resource module requires previous scoped cleanup')
    if lab.get('volumes.longhorn.io',namespace=NS)['items']:raise ValueError('runtime resource module refuses foreign volumes')
    original=(lab.DIRECTORY/'config.yml').read_text();config=yaml.safe_load(original)
    if (config['longhorn_data_engines']!=['v2'] or config['longhorn_requests'] or
            config['longhorn_v2_cpu_cores'] or not config['longhorn_v2_hugepages'] or
            config['longhorn_v2_interrupt_mode'] or
            (config['longhorn_v2_iobuf_large_pool_size'],config['longhorn_v2_iobuf_small_pool_size'])!=(1024,8192)):
        raise ValueError('runtime resource module requires the default hugepage/polling/mask/pools baseline')
    canonical_before=load('longhorn-v2-platform-install.py').snapshot(lab)
    identity=native(lab);row={k:lab.owner()[k] for k in ('run_id','cluster_uid','namespace_uid')}
    row.update(original_config=original,native=identity,baseline_identity=baseline_identity(lab),cpu_hosts={},profile_intent=False,results={})
    lab.write_private(lab.DIRECTORY/RECORD,row)
    with lab.scoped_cleanup(lambda:cleanup(lab,pid)):
        original_runtime=runtime(lab,profile(config,'original'),identity)
        cpu_proof=cpu_refusals(lab,pid,config,identity,original_runtime)
        prepare_desktops(lab)
        prepare_cpus(lab)
        row=load_record(lab)
        # CPU preparation uses no source volume and keeps the original lvstores.
        journal=business_namespace(lab,create=True);namespace=journal['namespace']
        row=load_record(lab)
        journal['defaults']=lab.wait('runtime-namespace-defaults',60,lambda:lab.namespace_defaults(namespace));lab.write_private(lab.DIRECTORY/BUSINESS,journal)
        claim=lab.clone_claim(namespace,'data','2Gi');del claim['spec']['dataSource']
        lab.create_business_object(journal,lab.DIRECTORY/BUSINESS,claim)
        def bound():lab.bind_business_volume(journal,lab.DIRECTORY/BUSINESS);return journal.get('volume_uid')
        lab.wait('runtime-source-volume-bound',600,bound)
        row['business']['volume_uid']=journal['volume_uid'];lab.write_private(lab.DIRECTORY/RECORD,row)
        client(lab,journal,'writer',0)
        token=lab.owner()['run_id'];lifecycle=lab.load_helper('longhorn-v2-clone-lifecycle.py')
        digest=lifecycle.io(lab,journal,'writer',token)
        lab.wait('runtime-original-three-replicas',600,lambda:lab.healthy_replicas(journal['volume']))
        topology=lab.replica_topology(journal,3)
        before=components(lab,profile(config,'original'),identity)
        tuned=profile(config,'anonymous-iobuf')
        product(lab,pid,tuned,'V2 resource-setting changes require all V2 volumes detached: data-engine-iobuf-large-pool-size')
        impossible=dict(profile(config,'original'),longhorn_v2_iobuf_large_pool_size=16384)
        product(lab,pid,impossible,BUDGET_REFUSAL)
        if components(lab,profile(config,'original'),identity)!=before:raise ValueError('runtime resource refusal changed native Pods')
        lifecycle.io(lab,journal,'writer',token,read=True)
        print('LONGHORN_V2_RUNTIME_ATTACHED_AND_BUDGET_REFUSAL_PASS',flush=True)
        detach(lab,journal)
        row['profile_intent']=True;lab.write_private(lab.DIRECTORY/RECORD,row)
        previous=original_runtime
        for name,index in (('anonymous-iobuf',1),('exclusive',2),('combined',0),('original',1)):
            desired=profile(config,name)
            if name=='combined':
                limits=[p['resources'].get('limits',{}).get('memory','0') for p in row['results']['exclusive']['state']['pods'].values()]
                product_module=lab.load_helper('longhorn-v2-resources.py').product()
                if any(product_module.quantity(limit)<(desired['longhorn_v2_memory_mib']+128)*1024**2 for limit in limits):
                    test=json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text());test.update(desired)
                    try:product_module.validate(test)
                    except ValueError as error:
                        if str(error)!=COMBINATION_REFUSAL:raise
                    else:raise ValueError('unsafe anonymous/exclusive combination lacks product admission; retained safe exclusive profile will be restored')
                    unchanged=components(lab,profile(config,'exclusive'),identity)
                    product(lab,pid,desired,COMBINATION_REFUSAL)
                    if components(lab,profile(config,'exclusive'),identity)!=unchanged:raise ValueError('combined-resource refusal changed native Pods')
                    row['results'][name]={'safe_refusal':True,'ordinary_limits':limits};lab.write_private(lab.DIRECTORY/RECORD,row)
                    print('LONGHORN_V2_RUNTIME_COMBINATION_SAFE_REFUSAL_PASS',flush=True)
                    continue
            budget=host_budget(lab,desired)
            product(lab,pid,desired)
            state=runtime(lab,desired,identity)
            if any(state['pods'][n]['pod_uid']==previous['pods'][n]['pod_uid'] for n in state['pods']):
                raise ValueError('runtime resource setting did not replace every original IM Pod')
            app='runtime-'+name;client(lab,journal,app,index);ledger=observe_io(lab,journal,app,token)
            after=runtime(lab,desired,identity)
            if after['pods']!=state['pods']:raise ValueError('runtime IM changed or restarted during sustained I/O')
            if lab.replica_topology(journal,3)!=topology:raise ValueError('runtime resource profile changed original replicas')
            row['results'][name]={'state':after,'ledger':ledger,'host_budget':budget};lab.write_private(lab.DIRECTORY/RECORD,row)
            previous=after
            detach(lab,journal)
            print('LONGHORN_V2_RUNTIME_PROFILE_PASS profile='+name,flush=True)
        row['payload_sha256']=digest;row['volume_uid']=journal['volume_uid'];row['replicas']=topology
        row['passed']=True;lab.write_private(lab.DIRECTORY/RECORD,row)
    row=load_record(lab)
    canonical_after=load('longhorn-v2-platform-install.py').snapshot(lab)
    if canonical_before!=canonical_after:raise ValueError('runtime canonical baseline changed after cleanup')
    print('LONGHORN_V2_RUNTIME_RESOURCES_RESULT '+json.dumps({'profiles':row['results'],'payload_sha256':row['payload_sha256'],
          'volume_uid':row['volume_uid'],'replicas':row['replicas'],'cpu_refusals':cpu_proof,
          'baseline_before':canonical_before,'baseline_after':canonical_after},sort_keys=True),flush=True)
    (lab.DIRECTORY/RECORD).unlink()
    print('LONGHORN_V2_RUNTIME_RESOURCES_BUSINESS_PASS',flush=True)


def load(name):
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),Path(__file__).with_name(name));module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('business','cleanup'));parser.add_argument('--relay-pid',type=int,required=True)
    args=parser.parse_args();_,lab=load('longhorn-v2-install.py').fixture();lab.require_lease()
    if args.relay_pid<=0:parser.error('runtime profiles require the owned live relay')
    if args.action=='business':business(lab,args.relay_pid)
    else:cleanup(lab,args.relay_pid,failed=True)


if __name__=='__main__':
    def interrupted(signum,frame):raise SystemExit(128+signum)
    signal.signal(signal.SIGINT,interrupted);signal.signal(signal.SIGTERM,interrupted)
    main()
