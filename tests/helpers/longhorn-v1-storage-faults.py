#!/usr/bin/env python3
"""V1 physical filesystem-disk and exact replica-data TCP faults.

The accepted business/control ledger owns fsync acknowledgements and remount.
Physical loss changes only the admitted SCSI state or independent virtio-blk
function. Its original UUID mount recovers through standard boot fsck when
necessary. Transport loss changes only one original replica data endpoint.
Never force detach, salvage, unmount, format or run fsck on a mounted filesystem.
"""
import argparse
import copy
import hashlib
import importlib.util
import inspect
import ipaddress
import json
from pathlib import Path
import re
import signal
import subprocess
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v1_storage_fault'
INTENT = 'v1_storage_fault_intent'
CASES = ('disk', 'network')


def load(name):
    spec = importlib.util.spec_from_file_location('v1_storage_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def persist(lab, journal, proof):
    journal[KEY] = proof; lab.write_private(lab.DIRECTORY/RECORD, journal)


def disk_transition(current, expected, operation):
    """Compare stable identity even if the failed filesystem became read-only."""
    if operation not in ('offline', 'restore'):
        raise ValueError('unsupported V1 SCSI transition')
    if {k: v for k, v in current.items() if k not in ('state', 'writable')} != {
            k: v for k, v in expected.items() if k not in ('state', 'writable')}:
        raise ValueError('V1 disk machine, boot, hardware, mount or receipt identity changed')
    if current['state'] not in ('running', 'offline'):
        raise ValueError('V1 disk has an unexpected SCSI state')
    if operation == 'offline' and (current != expected or not current['writable'] or current['state'] != 'running'):
        raise ValueError('V1 physical fault cannot replay or adopt an uncertain disk state')
    return 'offline' if operation == 'offline' else 'running'


def mount_identity(raw, target, device):
    """Read kernel mount state without probing a deliberately offline disk."""
    rows = []
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
    for line in raw.splitlines():
        left, right = line.split(' - ', 1)
        fields, filesystem = left.split(), right.split()
        rows.append({'target': unescape(fields[4]), 'device': fields[2],
                     'fstype': filesystem[0], 'options': fields[5].split(',')})
    selected = [row for row in rows if row['target'] == target]
    root = [row for row in rows if row['target'] == '/']
    if (len(selected) != 1 or len(root) != 1 or selected[0]['device'] != device or
            selected[0]['device'] == root[0]['device'] or selected[0]['fstype'] != 'ext4' or
            len([row for row in rows if row['device'] == device]) != 1):
        raise ValueError('physical fault requires the exact independent ext4 mount')
    return {'writable': 'rw' in selected[0]['options']}


def virtio_attachment(device):
    """A virtio block function, never its PCI controller or another disk."""
    from pathlib import Path
    block = Path('/sys/class/block') / device.name
    bus = (block / 'device').resolve(strict=True)
    driver = Path('/sys/bus/virtio/drivers/virtio_blk')
    if (not re.fullmatch(r'virtio[0-9]+', bus.name) or
            (bus / 'subsystem').resolve(strict=True) != Path('/sys/bus/virtio') or
            (bus / 'driver').resolve(strict=True) != driver or
            (bus / 'device').read_text().strip() != '0x0002' or
            {p.name for p in (bus / 'block').iterdir()} != {device.name} or
            list(bus.parent.glob('virtio*')) != [bus] or
            not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', bus.parent.name)):
        raise ValueError('V1 virtio fault requires one independent block-only PCI function')
    return {'kind': 'virtio', 'bus_id': bus.name, 'path': str(bus),
            'pci': str(bus.parent), 'driver': str(driver)}


def restored_disk_identity(current, original, reboot=False):
    ignored = {'state', 'writable'}
    if reboot:
        if current.get('boot_id') == original.get('boot_id'):
            raise ValueError('V1 disk recovery requires an actual new boot')
        ignored.add('boot_id')
    if original.get('attachment', {}).get('kind') == 'virtio':
        ignored.update(('device', 'major_minor'))
    if ({k: v for k, v in current.items() if k not in ignored} !=
            {k: v for k, v in original.items() if k not in ignored} or
            current.get('state') != 'running' or current.get('writable') is not True):
        raise ValueError('V1 restored disk stable identity, mount or receipt changed')


def disk_program():
    return '''import hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path
''' + inspect.getsource(disk_transition) + '\n' + inspect.getsource(mount_identity) + '\n' + inspect.getsource(virtio_attachment) + '''
r=json.load(sys.stdin);d=r['disk']
def command(args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=25).stdout.strip()
def observe():
 marker=Path('/etc/kubeauto/longhorn-disk.json')
 if marker.is_symlink() or not marker.is_file():raise ValueError('V1 physical disk has no regular product receipt')
 raw=marker.read_bytes();receipt=json.loads(raw)
 if any(receipt.get(k)!=v for k,v in {'cluster_uid':r['cluster_uid'],'node_uid':r['node_uid'],'path':d['path'],'uuid':d['uuid']}.items()):
  raise ValueError('V1 mounted disk receipt changed')
 device=Path(d['device']).resolve(strict=True);info=device.stat()
 if not stat.S_ISBLK(info.st_mode):raise ValueError('V1 admitted stable hardware path changed')
 sysfs=Path('/sys/class/block')/device.name
 if (sysfs/'partition').exists() or list(sysfs.glob(device.name+'[0-9]*')) or list((sysfs/'holders').iterdir()):
  raise ValueError('V1 physical fault refuses partitions or holders')
 if int((sysfs/'size').read_text())*512!=d['size']:raise ValueError('V1 physical capacity changed')
 properties=dict(line.split('=',1) for line in command(['udevadm','info','--query=property','--name='+str(device)]).splitlines() if '=' in line)
 identity=('path:'+properties.get('ID_PATH','')) if d['identity'].startswith('path:') else (properties.get('ID_WWN') or properties.get('ID_SERIAL_SHORT') or properties.get('ID_SERIAL'))
 if identity!=d['identity']:raise ValueError('V1 physical hardware identity changed')
 if properties.get('ID_FS_UUID')!=d['uuid']:raise ValueError('V1 cached filesystem UUID changed')
 mount=mount_identity(Path('/proc/self/mountinfo').read_text(),d['path'],str(os.major(info.st_rdev))+':'+str(os.minor(info.st_rdev)))
 attachment={}
 if (sysfs/'device/state').exists():
  state=(sysfs/'device/state').resolve(strict=True)
  if str(device)!=d['kernel'] or not str(state).startswith('/sys/devices/') or state.name!='state':raise ValueError('V1 fault requires the original SCSI state file')
  state_path=str(state);state_value=state.read_text().strip()
 else:
  attachment=virtio_attachment(device);state_path=attachment['path'];state_value='running'
 row={'machine_id':Path('/etc/machine-id').read_text().strip(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
  'receipt_sha256':hashlib.sha256(raw).hexdigest(),'device':str(device),'major_minor':[os.major(info.st_rdev),os.minor(info.st_rdev)],
  'identity':identity,'size_bytes':d['size'],'filesystem_uuid':d['uuid'],'state_path':state_path,'state':state_value,
  'writable':mount['writable']}
 if attachment:row['attachment']=attachment
 return row
current=observe()
if r['operation']!='probe':
 if current.get('attachment'):raise ValueError('V1 virtio requires its separate driver fault transition')
 desired=disk_transition(current,r['expected'],r['operation'])
 if observe()!=current:raise ValueError('V1 disk changed during final transition check')
 if current['state']!=desired:Path(current['state_path']).write_text(desired+'\\n')
 current=observe()
 if current['state']!=desired:raise ValueError('V1 SCSI transition did not converge')
print(json.dumps(current,sort_keys=True))
'''


def disk_call(lab, proof, operation):
    if proof['address'] not in lab.WORKERS or proof['disk'] != lab.owner()['disks'][proof['address']]:
        raise ValueError('V1 physical fault exceeds its admitted filesystem-disk lease')
    request = {'operation': operation, 'cluster_uid': lab.owner()['cluster_uid'],
        'node_uid': proof['node_uid'], 'disk': proof['disk']}
    if operation != 'probe': request['expected'] = proof['original_host']
    return json.loads(lab.remote(proof['address'], ['python3', '-c', disk_program()],
                                 stdin=json.dumps(request), timeout=90).stdout)


def virtio_program():
    return '''import hashlib,json,re,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin);a=r['attachment'];original=r['original']
def observe():
 if Path('/etc/machine-id').read_text().strip()!=original['machine_id'] or Path('/proc/sys/kernel/random/boot_id').read_text().strip()!=original['boot_id']:
  raise ValueError('V1 virtio transition changed original host/boot')
 if hashlib.sha256(Path('/etc/kubeauto/longhorn-disk.json').read_bytes()).hexdigest()!=original['receipt_sha256']:raise ValueError('V1 virtio product receipt changed')
 bus=Path(a['path']);driver=Path(a['driver'])
 if (not re.fullmatch('virtio[0-9]+',a['bus_id']) or bus.name!=a['bus_id'] or
     bus.resolve(strict=True)!=bus or bus.parent!=Path(a['pci']) or
     driver!=Path('/sys/bus/virtio/drivers/virtio_blk') or
     (bus/'subsystem').resolve(strict=True)!=Path('/sys/bus/virtio') or
     (bus/'device').read_text().strip()!='0x0002' or list(bus.parent.glob('virtio*'))!=[bus]):
  raise ValueError('V1 original independent virtio device changed')
 link=bus/'driver';bound=link.exists()
 if bound and link.resolve(strict=True)!=driver:raise ValueError('V1 virtio device acquired another driver')
 blocks=sorted(p.name for p in (bus/'block').iterdir()) if (bus/'block').exists() else []
 if len(blocks)!=(1 if bound else 0):raise ValueError('V1 virtio child block set changed')
 if bound:
  block=Path('/sys/class/block')/blocks[0]
  if (block/'partition').exists() or list(block.glob(blocks[0]+'[0-9]*')) or list((block/'holders').iterdir()):raise ValueError('V1 virtio child gained partitions/holders')
  if int((block/'size').read_text())*512!=r['size']:raise ValueError('V1 virtio capacity changed')
 return {'bound':bound,'blocks':blocks,'attachment':a}
before=observe()
if r['operation']=='unbind':
 if before!={'bound':True,'blocks':[Path(original['device']).name],'attachment':a}:raise ValueError('V1 virtio refuses repeated or changed unbind')
 (Path(a['driver'])/'unbind').write_text(a['bus_id']+'\\n')
elif r['operation']=='bind':
 if not before['bound']:(Path(a['driver'])/'bind').write_text(a['bus_id']+'\\n')
else:raise ValueError('unsupported V1 virtio transition')
after=observe()
if after['bound']!=(r['operation']=='bind'):raise ValueError('V1 virtio transition did not converge')
if after['bound']:
 subprocess.run(['udevadm','settle','--timeout=30'],check=True,capture_output=True,timeout=40)
 device=Path('/dev')/after['blocks'][0];d=r['disk']
 if Path(d['device']).resolve(strict=True)!=device:raise ValueError('V1 re-bound stable hardware path changed')
 output=subprocess.run(['udevadm','info','--query=property','--name='+str(device)],check=True,capture_output=True,text=True,timeout=20).stdout
 props=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
 identity=('path:'+props.get('ID_PATH','')) if d['identity'].startswith('path:') else (props.get('ID_WWN') or props.get('ID_SERIAL_SHORT') or props.get('ID_SERIAL'))
 if identity!=d['identity'] or props.get('ID_FS_UUID')!=d['uuid']:raise ValueError('V1 re-bound hardware or filesystem UUID changed')
print(json.dumps(after,sort_keys=True))
'''


def virtio_call(lab, proof, operation):
    if proof['address'] not in lab.WORKERS or proof['disk'] != lab.owner()['disks'][proof['address']]:
        raise ValueError('V1 virtio transition exceeds its disk lease')
    request = {'operation': operation, 'attachment': proof['original_host']['attachment'],
               'original': proof['original_host'], 'size': proof['disk']['size'], 'disk': proof['disk']}
    return json.loads(lab.remote(proof['address'], ['python3', '-c', virtio_program()],
                                 stdin=json.dumps(request), timeout=90).stdout)


def boot_check_program():
    """Observe original boot policy and the checker; never invoke fsck here."""
    return '''import hashlib,json,re,shlex,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin);d=r['disk'];data=d['path'];uuid=d['uuid']
def run(*args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30).stdout.strip()
def props(unit,fields):return dict(line.split('=',1) for line in run('systemctl','show',unit,'--property='+','.join(fields)).splitlines() if '=' in line)
unit=run('systemd-escape','--path','--template=systemd-fsck@.service','/dev/disk/by-uuid/'+uuid)
mount=run('systemd-escape','--path','--suffix=mount',data)
fs=props(unit,['FragmentPath','DropInPaths','ExecStart','Result','ExecMainStatus','InvocationID','ExecMainStartTimestampMonotonic','ExecMainExitTimestampMonotonic'])
ms=props(mount,['FragmentPath','DropInPaths','What','Where','Requires','After','ActiveEnterTimestampMonotonic'])
fragment=Path(ms['FragmentPath']).read_text()
what=[line.split('=',1)[1] for line in fragment.splitlines() if line.startswith('What=')]
guard=Path('/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf').read_text()
fstab=[line for line in Path('/etc/fstab').read_text().splitlines() if line.strip() and not line.lstrip().startswith('#') and len(line.split())>1 and line.split()[1]==data]
boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
row={'machine_id':Path('/etc/machine-id').read_text().strip(),'boot_id':boot,'unit':unit,'fsck':fs,'mount':ms,
 'mount_what':what,'fstab':fstab,'guard':guard,'cmdline':Path('/proc/cmdline').read_text().split(),
 'override_files':[p for p in ('/fastboot','/forcefsck') if Path(p).exists()],
 'label':run('blkid','-s','LABEL','-o','value',d['device']),
 'checker':Path('/sbin/fsck.ext4').is_file(),'versions':run('rpm','-q','systemd','e2fsprogs','util-linux'),'kernel':run('uname','-r'),
 'fsck_fragment':Path(fs['FragmentPath']).read_text(),'locale':Path('/etc/locale.conf').read_text(),
 'requires':shlex.split(ms['Requires']),'after':shlex.split(ms['After'])}
if r['journal']:
 row['journal']=[json.loads(line) for line in run('journalctl','--quiet','--no-pager','--boot='+boot.replace('-',''),'-u',unit,'-o','json').splitlines()]
if r.get('warnings'):
 row['kernel_warnings']=[json.loads(line) for line in run('journalctl','--quiet','--no-pager','-k','--boot='+boot.replace('-',''),'-p','warning','-n','200','-o','json').splitlines()]
print(json.dumps(row,sort_keys=True))
'''


def boot_policy(row, proof):
    disk, original = proof['disk'], proof['original_host']
    fields = row.get('fstab', [])
    expected_guard = '[Unit]\nRequiresMountsFor='+disk['path']+'\n[Service]\nExecStartPre=/usr/bin/mountpoint -q -- '+disk['path']+'\n'
    if (len(fields) != 1 or fields[0].split()[:6] !=
            ['UUID='+disk['uuid'], disk['path'], 'ext4', 'defaults', '0', '2'] or
            fields[0].split()[6:] != ['#', 'kubeauto-longhorn-lab:'+proof['run_id']] or
            row.get('machine_id') != original['machine_id'] or row.get('guard') != expected_guard or
            row.get('override_files') or any(t.startswith(('fsck.', 'rd.fsck.')) or t in ('fastboot', 'forcefsck') for t in row.get('cmdline', [])) or
            row.get('checker') is not True or row.get('label') != 'kca-longhorn' or
            not re.search(r'^LANG=(?:"?)(?:en_US\.UTF-8|C(?:\.UTF-8)?)(?:"?)$', row.get('locale', ''), re.M)):
        raise ValueError('V1 boot recovery requires the original UUID/pass2/default auto-preen policy')
    fs, mount = row['fsck'], row['mount']
    if (not row.get('unit', '').startswith('systemd-fsck@') or
            fs.get('FragmentPath') != '/usr/lib/systemd/system/systemd-fsck@.service' or fs.get('DropInPaths') or
            mount.get('DropInPaths') or row['unit'] not in row.get('requires', []) or row['unit'] not in row.get('after', []) or
            row.get('mount_what') != ['/dev/disk/by-uuid/'+disk['uuid']] or mount.get('Where') != disk['path'] or
            'argv[]=/usr/lib/systemd/systemd-fsck /dev/disk/by-uuid/'+disk['uuid']+' ;' not in fs.get('ExecStart', '') or
            '\nExecStart=/usr/lib/systemd/systemd-fsck %f\n' not in row.get('fsck_fragment', '') or
            not all(re.search(pattern, row.get('versions', ''), re.M) for pattern in
                    (r'^systemd-252-', r'^e2fsprogs-1\.46\.5-', r'^util-linux-2\.37\.4-'))):
        raise ValueError('V1 original effective filesystem boot-check dependency changed')


def boot_check(row, proof):
    boot_policy(row, proof)
    prior = proof['boot_preflight']
    for key in ('unit', 'fstab', 'guard', 'cmdline', 'versions', 'kernel', 'label', 'fsck_fragment', 'locale'):
        if row[key] != prior[key]:
            raise ValueError('V1 reboot changed its verified boot policy: '+key)
    fs, mount = row['fsck'], row['mount']
    started = int(fs.get('ExecMainStartTimestampMonotonic', 0)); ended = int(fs.get('ExecMainExitTimestampMonotonic', 0))
    if (row['boot_id'] == proof['original_host']['boot_id'] or fs.get('Result') != 'success' or
            fs.get('ExecMainStatus') != '0' or not re.fullmatch('[a-f0-9]{32}', fs.get('InvocationID', '')) or
            not 0 < started <= ended <= int(mount.get('ActiveEnterTimestampMonotonic', 0))):
        raise ValueError('V1 filesystem checker did not succeed before mounting in the new boot')
    rows = row.get('journal', [])
    messages = [r.get('MESSAGE', '') for r in rows]
    if any(not isinstance(message, str) for message in messages):
        raise ValueError('V1 fsck journal has an unsupported message encoding')
    if any(re.search(r'fsck failed|Ignoring error|not checking|does not exist|doesn.t exist|skipp|UNEXPECTED INCONSISTENCY|MANUAL', message, re.I) for message in messages):
        raise ValueError('V1 filesystem checker reported a skipped or failed underlying check')
    terminal = re.compile(r'^'+re.escape(row['label'])+r': (?:clean, )?\d+/\d+ files(?: \(\d+\.\d+% non-contiguous\))?, \d+/\d+ blocks\s*$')
    positive = [r for r in rows if terminal.fullmatch(r.get('MESSAGE', '')) and
                r.get('_BOOT_ID') == row['boot_id'].replace('-', '') and
                r.get('_SYSTEMD_INVOCATION_ID') == fs['InvocationID']]
    if len(positive) != 1:
        raise ValueError('V1 boot fsck lacks its exact current-invocation positive terminal result')
    return {'boot_id': row['boot_id'], 'unit': row['unit'], 'invocation_id': fs['InvocationID'],
            'terminal': positive[0]['MESSAGE'], 'checker_end_us': ended,
            'mount_enter_us': int(mount['ActiveEnterTimestampMonotonic']),
            'journal_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}


def boot_observe(lab, proof, journal=False, warnings=False):
    return json.loads(lab.remote(proof['address'], ['python3', '-c', boot_check_program()],
        stdin=json.dumps({'disk': proof['disk'], 'journal': journal, 'warnings': warnings}), timeout=90).stdout)


def rule_program():
    """Match the real data path before CNI allow chains in the source host."""
    return '''import ipaddress,json,re,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
def host():
 return {'machine_id':Path('/etc/machine-id').read_text().strip(),
         'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
if host()!=r['expected']:raise ValueError('V1 data fault host changed')
if not re.fullmatch('kubeauto-longhorn-v1-[a-f0-9]{24}',r['comment']):raise ValueError('invalid V1 fault owner')
if type(r.get('host_network'))!=bool:raise ValueError('V1 fault requires the original IM network mode')
src=str(ipaddress.IPv4Address(r['source']));dst=str(ipaddress.IPv4Address(r['destination']))
port=r['port']
if type(port)!=int or not 1025<=port<=65534 or src==dst:raise ValueError('invalid V1 data endpoint')
if r['operation'] not in ('probe','add','remove'):raise ValueError('invalid V1 fault operation')
chains=('OUTPUT','INPUT') if r['host_network'] else ('FORWARD','FORWARD')
rules=[[chains[0],'-p','tcp','-s',src+'/32','-d',dst+'/32','--dport',str(port)],
       [chains[1],'-p','tcp','-s',dst+'/32','-d',src+'/32','--sport',str(port)]]
present=[];tails=[rule+['-m','comment','--comment',r['comment'],'-j','DROP'] for rule in rules]
base=['iptables','-w','5','-t','filter'];before=[]
for tail in tails:
 result=subprocess.run(base+['-C']+tail,capture_output=True,text=True,timeout=15)
 if result.returncode not in (0,1):raise ValueError('cannot probe owned V1 data rule')
 before.append(result.returncode)
if r['operation']=='add' and 0 in before:raise ValueError('V1 fault refuses existing rule')
for tail,exists in zip(tails,before):
 if r['operation']=='add':
  subprocess.run(base+['-I',tail[0],'1']+tail[1:],check=True,timeout=15)
 elif r['operation']=='remove' and exists==0:subprocess.run(base+['-D']+tail,check=True,timeout=15)
 after=subprocess.run(base+['-C']+tail,capture_output=True,text=True,timeout=15)
 if after.returncode not in (0,1):raise ValueError('cannot read owned V1 data rule')
 present.append(after.returncode==0)
print(json.dumps({'host':host(),'present':present,'chains':list(chains)},sort_keys=True))
'''


def rule_call(lab, proof, operation):
    if proof['address'] not in lab.WORKERS:
        raise ValueError('V1 transport source is outside the leased workers')
    request = {'operation': operation, 'expected': proof['original_host'],
               **{k: proof[k] for k in ('source', 'destination', 'port', 'comment', 'host_network')}}
    return json.loads(lab.remote(proof['address'], ['python3', '-c', rule_program()],
                                 stdin=json.dumps(request), timeout=45).stdout)


def established_socket(raw, source, destination, port):
    """Parse /proc/net/tcp inside the original engine IM network namespace."""
    def endpoint(value):
        ip, number = value.split(':')
        return socket.inet_ntoa(bytes.fromhex(ip)[::-1]), int(number, 16)
    for line in raw.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[3] == '01':
            local, remote = endpoint(fields[1]), endpoint(fields[2])
            if local[0] == source and remote == (destination, port):
                return True
    return False


def data_endpoint(engine, replica, destination):
    endpoint = engine.get('status', {}).get('currentReplicaAddressMap', {}).get(replica['metadata']['name'], '')
    parsed = urlsplit(endpoint if '://' in endpoint else 'tcp://'+endpoint)
    # Engine 1.12.1 pkg/util.ParseAddresses: base=control, base+1=data,
    # base+2=sync. Isolate only data; do not block unrelated pod/node ports.
    if (parsed.scheme != 'tcp' or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.hostname != str(ipaddress.IPv4Address(destination)) or parsed.port is None or
            parsed.port != replica.get('status', {}).get('port') or not 1024 <= parsed.port < 65534):
        raise ValueError('V1 native replica base address differs from its original host/port')
    return parsed.port+1


def consumer_boundary(lab, journal, case, allowed_consumers):
    """An exact volume allowlist permits only an endpoint-scoped network fault."""
    if (case != 'network' or not isinstance(allowed_consumers, dict) or not allowed_consumers or
            any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in allowed_consumers.items()) or
            allowed_consumers.get(journal['volume']) != journal['volume_uid']):
        raise ValueError('shared V1 network fault requires exact owned consumer identities')
    volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
    current = {v['metadata']['name']: v['metadata']['uid'] for v in volumes}
    if len(current) != len(volumes) or current != allowed_consumers:
        raise ValueError('shared V1 network fault refuses changed or foreign consumers')
    lab.check_business_graph(journal)


def boundary(lab, journal, case, allowed_consumers=None):
    if case not in CASES:
        raise ValueError('unsupported V1 storage fault')
    common = load('longhorn-v1-stability.py'); control = load('longhorn-control-faults.py')
    if allowed_consumers is None:
        engine_source = common.source(lab, journal)
        pod, parent = control.fault_pod(lab, journal, 'instance-manager', engine_source)
    else:
        consumer_boundary(lab, journal, case, allowed_consumers)
        engine_source = control.source(lab, journal)
        candidates = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
            if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') != engine_source['node']]
        if len(candidates) != 2:
            raise ValueError('V1 endpoint fault requires two original remote replicas')
        chosen = sorted(candidates, key=lambda r: r['metadata']['name'])[0]
        native = load('longhorn-faults.py').replica_process(lab, journal, chosen)
        pod = lab.get('pod', native['manager'], NS)
        parent = {'kind': 'InstanceManager', 'name': native['manager'],
                  'uid': native['manager_uid'], 'node': native['node']}
        if not pod or pod['metadata']['uid'] != native['pod_uid']:
            raise ValueError('V1 endpoint fault original replica Pod changed')
    replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
        if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') == parent['node']]
    if len(replicas) != 1:
        raise ValueError('V1 remote storage fault requires one owned replica on its worker')
    replica = replicas[0]
    native = load('longhorn-faults.py').replica_process(lab, journal, replica)
    proof = {'case': case, 'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'],
        'volume_uid': journal['volume_uid'], 'engine': engine_source, 'node': parent['node'],
        'node_uid': lab.get('node', parent['node'])['metadata']['uid'], 'parent': parent,
        'replica': replica['metadata']['name'], 'replica_uid': replica['metadata']['uid'], 'native': native,
        'replicas_before': lab.replica_topology(journal, 3)}
    ip = common.address(lab, parent['node'])
    if case == 'disk':
        proof.update(address=ip, disk=copy.deepcopy(lab.owner()['disks'][ip]))
        node = lab.get('nodes.longhorn.io', parent['node'], NS)
        matches = [(name, spec) for name, spec in node['spec']['disks'].items() if spec.get('path') == lab.DATA]
        if (len(matches) != 1 or matches[0][1].get('diskType', 'filesystem') != 'filesystem' or
                node['status']['diskStatus'][matches[0][0]].get('diskUUID') != replica['spec'].get('diskID') or
                replica['spec'].get('diskPath') != lab.DATA):
            raise ValueError('V1 physical fault native filesystem disk/replica identity changed')
        proof['disk_uuid'] = replica['spec']['diskID']
        proof['original_host'] = disk_call(lab, proof, 'probe')
        if proof['original_host']['state'] != 'running' or not proof['original_host']['writable']:
            raise ValueError('V1 physical disk is not originally running and writable')
        proof['node_policy'] = common.nodes(lab)[proof['node']]
        proof['attachment_uid'] = single_attachment(lab, journal, engine_source['node'])
        proof['boot_preflight'] = boot_observe(lab, proof)
        boot_policy(proof['boot_preflight'], proof)
        if proof['boot_preflight']['boot_id'] != proof['original_host']['boot_id']:
            raise ValueError('V1 boot policy was observed on another boot')
    else:
        source_host = common.address(lab, engine_source['node'])
        source_pod = lab.get('pod', engine_source['manager'], NS)
        source_ip = str(ipaddress.IPv4Address(source_pod['status']['podIP']))
        destination_ip = str(ipaddress.IPv4Address(pod['status']['podIP']))
        engine = lab.get('engines.longhorn.io', engine_source['name'], NS)
        proof.update(address=source_host, source=source_ip, destination=destination_ip,
            port=data_endpoint(engine, replica, destination_ip), original_host=common.host(lab, source_host),
            host_network=bool(source_pod['spec'].get('hostNetwork')), source_pod_uid=source_pod['metadata']['uid'],
            destination_pod_uid=pod['metadata']['uid'],
            comment='kubeauto-longhorn-v1-'+hashlib.sha256((lab.owner()['run_id']+journal['volume_uid']).encode()).hexdigest()[:24])
        sockets = lab.kubectl('-n', NS, 'exec', engine_source['manager'], '-c', 'instance-manager', '--',
            'cat', '/proc/net/tcp', timeout=30).stdout
        if not established_socket(sockets, source_ip, destination_ip, proof['port']):
            raise ValueError('V1 replica data fault has no actual established engine data connection')
        if rule_call(lab, proof, 'probe')['present'] != [False, False]:
            raise ValueError('V1 replica data fault comment already exists; do not adopt')
        proof['rules_absent_before'] = True
    return proof


def volume(lab, journal):
    obj = lab.get('volumes.longhorn.io', journal['volume'], NS)
    if not obj or obj['metadata']['uid'] != journal['volume_uid'] or obj['spec'].get('dataEngine') != 'v1':
        raise ValueError('V1 fault original volume changed')
    return obj


def single_attachment(lab, journal, engine_node):
    attachments = [a for a in lab.get('volumeattachments.storage.k8s.io')['items']
        if a.get('spec', {}).get('source', {}).get('persistentVolumeName') == journal['pv'] and a.get('status', {}).get('attached')]
    if (len(attachments) != 1 or attachments[0]['spec'].get('nodeName') != engine_node or
            attachments[0]['spec'].get('attacher') != 'driver.longhorn.io'):
        raise ValueError('V1 storage fault lacks one safe original CSI attachment')
    return attachments[0]['metadata']['uid']


def inject(lab, journal, case, allowed_consumers=None):
    if KEY in journal:
        raise ValueError('prior V1 storage fault requires recovery, never replay')
    proof = boundary(lab, journal, case, allowed_consumers)
    if boundary(lab, journal, case, allowed_consumers) != proof:
        raise ValueError('V1 storage fault boundary changed before injection')
    if allowed_consumers is not None:
        proof['allowed_consumers'] = dict(allowed_consumers)
    proof['intent'] = True; persist(lab, journal, proof)
    if case == 'disk':
        if proof['original_host'].get('attachment', {}).get('kind') == 'virtio':
            if disk_call(lab, proof, 'probe') != proof['original_host']:
                raise ValueError('V1 original virtio disk changed before unbind')
            proof['injected_driver'] = virtio_call(lab, proof, 'unbind')
            if proof['injected_driver']['bound'] or proof['injected_driver']['blocks']:
                raise ValueError('V1 physical virtio disk remains bound')
        else:
            proof['injected_host'] = disk_call(lab, proof, 'offline')
            if proof['injected_host']['state'] != 'offline':
                raise ValueError('V1 physical disk did not enter an actual offline state')
    else:
        proof['injected_rules'] = rule_call(lab, proof, 'add')
        if proof['injected_rules']['present'] != [True, True]:
            raise ValueError('V1 replica data partition did not install both owned rules')
    persist(lab, journal, proof)
    def degraded():
        engine = lab.get('engines.longhorn.io', proof['engine']['name'], NS)
        if not engine or engine['metadata']['uid'] != proof['engine']['uid']:
            raise ValueError('V1 storage fault changed the original engine')
        replica = lab.get('replicas.longhorn.io', proof['replica'], NS)
        mode = engine.get('status', {}).get('replicaModeMap', {}).get(proof['replica'])
        failed = not replica or replica['metadata']['uid'] != proof['replica_uid'] or bool(replica['spec'].get('failedAt'))
        current = volume(lab, journal)
        if (mode != 'ERR' and not failed) or current.get('status', {}).get('robustness') != 'degraded':
            return None
        return {'replica_mode': mode, 'original_replica_failed': failed,
            'volume_robustness': 'degraded', 'attachment_uid': single_attachment(lab, journal, proof['engine']['node'])}
    proof['degraded'] = lab.wait('v1-'+case+'-native-replica-failure', 180, degraded)
    persist(lab, journal, proof)
    print('LONGHORN_V1_STORAGE_FAULT_INJECTED '+json.dumps(proof, sort_keys=True), flush=True)
    return proof


def disk_survivors(lab, journal, proof):
    state = lab.owner()
    if (proof.get('case') != 'disk' or proof.get('intent') is not True or
            any(proof.get(k) != state[k] for k in ('run_id', 'cluster_uid')) or
            proof.get('volume_uid') != journal['volume_uid'] or proof.get('address') not in lab.WORKERS or
            proof.get('node') != 'longhorn-node-'+proof['address'].rsplit('.', 1)[1] or
            proof.get('node_uid') != state['node_uids'].get(proof['node']) or
            proof.get('disk') != state['disks'].get(proof['address'])):
        raise ValueError('V1 disk recovery differs from its original leased fault')
    before = proof['replicas_before']
    workers = {'longhorn-node-'+ip.rsplit('.', 1)[1] for ip in lab.WORKERS}
    if (len(before) != 3 or {r['node'] for r in before.values()} != workers or
            before.get(proof['replica']) != {'uid': proof['replica_uid'], 'node': proof['node']}):
        raise ValueError('V1 disk recovery lacks its original three-replica boundary')
    lab.check_business_graph(journal)
    namespace = lab.get('namespace', NS)
    current = volume(lab, journal)
    volumes = lab.get('volumes.longhorn.io', namespace=NS)['items']
    if (not namespace or namespace['metadata']['uid'] != state['namespace_uid'] or
            [(v['metadata']['name'], v['metadata']['uid']) for v in volumes] != [(journal['volume'], journal['volume_uid'])] or
            current['metadata'].get('deletionTimestamp') or current['spec'].get('encrypted') or
            current['spec'].get('accessMode') != 'rwo' or current['spec'].get('numberOfReplicas') != 3 or
            current['status'].get('state') != 'attached' or current['status'].get('currentNodeID') not in workers or
            current['status'].get('robustness') not in ('healthy', 'degraded')):
        raise ValueError('V1 disk recovery requires its exclusive original attached volume')
    if (load('longhorn-control-faults.py').engine_identity(lab, journal, current) != proof['engine'] or
            single_attachment(lab, journal, proof['engine']['node']) != proof['attachment_uid']):
        raise ValueError('V1 disk recovery changed the original writer engine/attachment')
    engine = lab.get('engines.longhorn.io', proof['engine']['name'], NS)
    for name, saved in proof['replicas_before'].items():
        if name == proof['replica']:
            continue
        replica = lab.get('replicas.longhorn.io', name, NS)
        if (not replica or replica['metadata']['uid'] != saved['uid'] or
                replica['spec'].get('nodeID') != saved['node'] or replica['spec'].get('failedAt') or
                replica.get('status', {}).get('currentState') != 'running' or
                engine.get('status', {}).get('replicaModeMap', {}).get(name) != 'RW'):
            raise ValueError('V1 disk recovery requires both original healthy surviving replicas')


def cleanup_preflight(lab, case):
    """Admit only the journaled disk victim before its original boot recovery."""
    lab.require_lease()
    record = lab.DIRECTORY/RECORD
    if record.is_symlink() or not record.is_file():
        raise ValueError('V1 recovery requires its regular original business journal')
    journal = json.loads(record.read_text())
    intent = journal.get(INTENT, {})
    if intent.get('run_id') != lab.owner()['run_id'] or intent.get('case') != case:
        raise ValueError('V1 recovery refuses an unrelated fault intent')
    proof = journal.get(KEY)
    if case != 'disk' or not proof:
        lab.core_identity()
        lab.check_business_graph(journal)
    else:
        state = lab.owner()
        nodes = lab.get('nodes')['items']
        expected = {'longhorn-node-'+ip.rsplit('.', 1)[1]: ip for ip in lab.NODES}
        if (lab.get('namespace', 'kube-system')['metadata']['uid'] != state['cluster_uid'] or
                len(nodes) != len(expected) or {n['metadata']['name'] for n in nodes} != set(expected) or
                {n['metadata']['name']: n['metadata']['uid'] for n in nodes} != state['node_uids']):
            raise ValueError('V1 disk recovery core identity changed')
        for node in nodes:
            name = node['metadata']['name']
            if [a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'] != [expected[name]]:
                raise ValueError('V1 disk recovery node address changed')
            if name != proof['node'] and not any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in node['status']['conditions']):
                raise ValueError('V1 disk recovery refuses a second unready node')
        disk_survivors(lab, journal, proof)
    print('LONGHORN_V1_STORAGE_CLEANUP_PREFLIGHT_PASS case='+case, flush=True)


def disk_reboot(lab, journal, proof):
    """Close the removed/aborted filesystem via one standard original-node boot."""
    common = load('longhorn-v1-stability.py')
    completed = bool(proof.get('boot_recovery', {}).get('checker'))
    if not proof.get('boot_recovery'):
        boot_policy(proof['boot_preflight'], proof)
        observed = boot_observe(lab, proof, warnings=True)
        boot_policy(observed, proof)
        for key in ('unit', 'fstab', 'guard', 'cmdline', 'versions', 'kernel', 'label', 'fsck_fragment', 'locale'):
            if observed[key] != proof['boot_preflight'][key]:
                raise ValueError('V1 original boot policy changed before reboot: '+key)
        disk_survivors(lab, journal, proof)
        original = {k: proof['original_host'][k] for k in ('machine_id', 'boot_id')}
        if common.host(lab, proof['address']) != original:
            raise ValueError('V1 worker changed before durable boot-recovery intent')
        proof['boot_recovery'] = {'intent': True, 'original': original, 'pre_reboot': observed}
        persist(lab, journal, proof)
        load('longhorn-v1-reboot-relay.py').reboot(lab, journal[INTENT]['relay_pid'], proof['address'], original)
    def ready():
        current = common.host(lab, proof['address'], unavailable=True)
        if current is None or current['boot_id'] == proof['original_host']['boot_id']:
            return None
        if current['machine_id'] != proof['original_host']['machine_id']:
            raise ValueError('V1 recovery reboot changed the physical machine')
        if not common.node_restored(lab, proof['node'], proof['node_policy']):
            return None
        return current
    lab.wait('v1-disk-original-worker-new-boot', 1200, ready)
    current = disk_call(lab, proof, 'probe')
    restored_disk_identity(current, proof['original_host'], reboot=True)
    observed = boot_observe(lab, proof, journal=True)
    proof['boot_recovery']['checker'] = boot_check(observed, proof)
    proof['boot_recovery']['observed'] = observed
    proof['restored_host'] = current
    if not completed:
        disk_survivors(lab, journal, proof)
    persist(lab, journal, proof)
    return current


def restore_fault(lab, journal, proof):
    if (proof.get('case') not in CASES or any(proof.get(k) != lab.owner()[k] for k in ('run_id', 'cluster_uid')) or
            proof.get('volume_uid') != journal['volume_uid'] or proof.get('intent') is not True):
        raise ValueError('V1 fault restoration journal belongs to another owner')
    common = load('longhorn-v1-stability.py')
    expected_ip = common.address(lab, proof['node'] if proof['case'] == 'disk' else proof['engine']['node'])
    if proof['address'] != expected_ip:
        raise ValueError('V1 fault restoration address differs from its original node')
    if proof['case'] == 'disk':
        if proof.get('boot_recovery'):
            disk_reboot(lab, journal, proof)
        elif proof['original_host'].get('attachment', {}).get('kind') == 'virtio':
            proof['restored_driver'] = virtio_call(lab, proof, 'bind')
            persist(lab, journal, proof)
            disk_reboot(lab, journal, proof)
        else:
            current = disk_call(lab, proof, 'restore')
            if current['state'] != 'running':
                raise ValueError('V1 physical disk remains offline; retain source and recovery journal')
            proof['restored_host'] = current; persist(lab, journal, proof)
            if not current['writable']:
                disk_reboot(lab, journal, proof)
            else:
                restored_disk_identity(current, proof['original_host'])
    else:
        expected_comment = 'kubeauto-longhorn-v1-'+hashlib.sha256((lab.owner()['run_id']+journal['volume_uid']).encode()).hexdigest()[:24]
        if (not proof.get('rules_absent_before') or proof['comment'] != expected_comment or
                not proof.get('source_pod_uid') or not proof.get('destination_pod_uid') or
                str(ipaddress.IPv4Address(proof['destination'])) != proof['destination'] or
                str(ipaddress.IPv4Address(proof['source'])) != proof['source']):
            raise ValueError('V1 transport restoration lacks its exact original rule boundary')
        current = rule_call(lab, proof, 'remove')
        if current['present'] != [False, False]:
            raise ValueError('V1 replica data rules remain after restoration')
        proof['restored_rules'] = current
    proof['fault_restored'] = True; persist(lab, journal, proof)


def recovered(lab, journal, proof, *, emit=True):
    if not proof.get('fault_restored'):
        restore_fault(lab, journal, proof)
    topology = lab.replica_topology(journal, 3)
    if not topology:
        return False
    if any(topology.get(name) != saved for name, saved in proof['replicas_before'].items() if name != proof['replica']):
        raise ValueError('V1 single storage fault replaced a healthy surviving replica')
    if proof['case'] == 'disk':
        replicas = [r for r in lab.get('replicas.longhorn.io', namespace=NS)['items']
                    if r['spec'].get('volumeName') == journal['volume'] and r['spec'].get('nodeID') == proof['node']]
        if (len(replicas) != 1 or replicas[0]['spec'].get('diskID') != proof['disk_uuid'] or
                replicas[0]['spec'].get('diskPath') != lab.DATA):
            raise ValueError('V1 disk rebuilt outside the original native filesystem disk')
    volume(lab, journal)
    proof['recovered'] = {'replicas': topology}; persist(lab, journal, proof)
    if emit:
        print('LONGHORN_V1_STORAGE_FAULT_RECOVERED '+json.dumps(proof, sort_keys=True), flush=True)
    return True


def cleanup(lab, original, record=None, **kwargs):
    if (lab.DIRECTORY/'longhorn-v1-reboot-relay.json').exists():
        load('longhorn-v1-reboot-relay.py').recover_current(lab)
    record = record or lab.DIRECTORY/RECORD
    if record.is_symlink():
        raise ValueError('V1 storage fault refuses symlink cleanup journal')
    if record.exists():
        journal = json.loads(record.read_text()); proof = journal.get(KEY)
        intent = journal.get(INTENT, {})
        if kwargs.get('relay_pid'):
            intent['relay_pid'] = kwargs['relay_pid']
        if (intent.get('run_id') != lab.owner()['run_id'] or intent.get('case') not in CASES):
            raise ValueError('V1 storage fault cleanup refuses an unrelated business journal')
        if proof:
            lab.check_business_graph(journal)
            restore_fault(lab, journal, proof)
            if not proof.get('recovered'):
                lab.wait('v1-storage-fault-restored-before-cleanup', 1200,
                    lambda: recovered(lab, journal, proof, emit=False))
            lab.installation_health()
            print('LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)
    return original(record, **kwargs)


def baseline(lab):
    lab.core_identity()
    lab.installation_health()
    load('longhorn-v2-im-adopt.py').empty(lab)
    for address in lab.WORKERS:
        rules = lab.remote(address, ['iptables-save', '-t', 'filter'], timeout=30).stdout
        if 'kubeauto-longhorn-v1-' in rules:
            raise ValueError('V1 fault baseline retains a storage-fault network rule')
    print('LONGHORN_V1_STORAGE_FAULT_EMPTY_HEALTHY_BASELINE_PASS', flush=True)


def business(lab, pid, case):
    if case not in CASES:
        raise ValueError('unsupported V1 storage fault')
    path = lab.DIRECTORY/RECORD
    if path.exists() or path.is_symlink():
        raise ValueError('V1 storage fault requires an empty business journal baseline')
    original, original_cleanup, original_write = lab.load_helper, lab.cleanup_business, lab.write_private
    intent = {'run_id': lab.owner()['run_id'], 'case': case, 'relay_pid': pid}
    def write(target, row):
        if target == path:
            row[INTENT] = intent
        original_write(target, row)
    lab.write_private = write
    control = original('longhorn-control-faults.py')
    control.inject = lambda current, journal, ignored: inject(current, journal, case)
    control.recovered = recovered
    lab.load_helper = lambda name: control if name == 'longhorn-control-faults.py' else original(name)
    lab.cleanup_business = lambda record=None, **kwargs: cleanup(lab, original_cleanup, record, **kwargs)
    try:
        lab.run_requests(pid, [])
        lab.business('control_failure', relay_pid=pid, storage_class='longhorn-delete', fault_variant='instance-manager')
    finally:
        lab.load_helper, lab.cleanup_business, lab.write_private = original, original_cleanup, original_write
    print('LONGHORN_V1_STORAGE_'+case.upper()+'_BUSINESS_PASS', flush=True)


def verify_log(log, case):
    if case not in CASES or re.search(r'^(LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', log, re.M):
        raise ValueError('V1 storage-fault log has a failure or unsupported scope')
    def one(marker):
        rows = [json.loads(line[len(marker)+1:]) for line in log.splitlines() if line.startswith(marker+' ')]
        if len(rows) != 1:
            raise ValueError('V1 storage fault requires one observation: '+marker)
        return rows[0]
    original, result = one('LONGHORN_V1_STORAGE_FAULT_INJECTED'), one('LONGHORN_V1_STORAGE_FAULT_RECOVERED')
    if (original.get('case') != case or result.get('case') != case or not original.get('volume_uid') or
            original['volume_uid'] != result.get('volume_uid') or not original.get('intent') or
            result.get('fault_restored') is not True or original.get('degraded', {}).get('volume_robustness') != 'degraded' or
            not original['degraded'].get('attachment_uid') or
            original['degraded'].get('replica_mode') != 'ERR' and not original['degraded'].get('original_replica_failed')):
        raise ValueError('V1 storage fault lacks actual native degradation and scoped restoration')
    if case == 'disk':
        host = original.get('original_host', {})
        virtio = host.get('attachment', {}).get('kind') == 'virtio'
        recovery = result.get('boot_recovery')
        if (host.get('writable') is not True or host.get('state') != 'running' or not original.get('disk_uuid')):
            raise ValueError('V1 physical disk loss/restoration proof is incomplete')
        if virtio:
            attachment = host['attachment']
            if (original.get('injected_driver') != {'bound': False, 'blocks': [], 'attachment': attachment} or
                    result.get('restored_driver', {}).get('bound') is not True or
                    result['restored_driver'].get('attachment') != attachment or
                    len(result['restored_driver'].get('blocks', [])) != 1 or not recovery):
                raise ValueError('V1 physical virtio detach/bind recovery proof is incomplete')
        elif original.get('injected_host', {}).get('state') != 'offline':
            raise ValueError('V1 physical SCSI fault lacks its actual offline state')
        restored_disk_identity(result.get('restored_host', {}), host, reboot=bool(recovery))
        if recovery:
            if (recovery.get('intent') is not True or recovery.get('original') !=
                    {k: host[k] for k in ('machine_id', 'boot_id')} or
                    recovery.get('checker') != boot_check(recovery.get('observed', {}), original)):
                raise ValueError('V1 physical disk boot-check evidence differs from its original fault')
    else:
        if (not original.get('rules_absent_before') or original.get('injected_rules', {}).get('present') != [True, True] or
                result.get('restored_rules', {}).get('present') != [False, False] or
                not original.get('source') or original['source'] == original.get('destination') or
                type(original.get('port')) is not int or not 1024 <= original['port'] <= 65534):
            raise ValueError('V1 exact replica data endpoint/rule recovery is incomplete')
    topology = result.get('recovered', {}).get('replicas', {})
    if (len(topology) != 3 or len({r.get('node') for r in topology.values()}) != 3 or
            len({r.get('uid') for r in topology.values() if r.get('uid')}) != 3 or
            any(topology.get(name) != saved for name, saved in original['replicas_before'].items() if name != original['replica'])):
        raise ValueError('V1 storage fault lost its full healthy set or replaced a survivor')
    control, faults = load('longhorn-control-faults.py'), load('longhorn-faults.py')
    result = one('LONGHORN_CONTROL_FAULT_RESULT'); ledger = one('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS')
    if (result.get('variant') != 'instance-manager' or result.get('client_rc') != 0 or ledger.get('client') != 'writer' or
            not ledger.get('token', '').endswith(':instance-manager') or
            ledger['fresh'].get('token') != ledger['token']+':recovered' or
            ledger['fresh'].get('path') != '/data/control-recovered-ledger'):
        raise ValueError('V1 storage fault ledger owner/recovery boundary differs')
    for key, proof in (('confirmed', ledger), ('fresh_confirmed', ledger['fresh'])):
        verified = control.confirmed(proof['records'], proof['token'], faults)['summary']
        if proof.get('summary') != verified or result.get(key) != verified:
            raise ValueError('V1 storage fault acknowledged-write hash prefix differs')
    if one('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED') != {'original': ledger['summary'], 'recovered': ledger['fresh']['summary']}:
        raise ValueError('V1 storage fault cross-node remount differs from confirmed bytes')
    for marker in ('LONGHORN_CONTROL_FAILURE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                   'LONGHORN_V1_STORAGE_FAULT_RESTORED_BEFORE_CLEANUP', 'LONGHORN_V1_STORAGE_'+case.upper()+'_BUSINESS_PASS'):
        if log.splitlines().count(marker) != 1:
            raise ValueError('V1 storage fault terminal/cleanup incomplete: '+marker)
    print('LONGHORN_V1_STORAGE_FAULT_LOG_VERIFIED case='+case, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'cleanup', 'cleanup-preflight', 'verify', 'baseline'))
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log: parser.error('verify requires --log')
        verify_log(args.log.read_text(), args.case)
        return
    lab = load('longhorn-lab.py'); lab.require_lease()
    if args.mode == 'cleanup-preflight':
        cleanup_preflight(lab, args.case)
        return
    if args.mode == 'baseline':
        baseline(lab)
        return
    if args.relay_pid <= 0: parser.error('V1 faults require the supervised Registry relay')
    if args.mode == 'business': business(lab, args.relay_pid, args.case)
    else:
        path = lab.DIRECTORY/RECORD
        if path.exists():
            row = json.loads(path.read_text())
            if (row.get(INTENT, {}).get('case') != args.case or
                    KEY in row and row[KEY].get('case') != args.case):
                raise ValueError('V1 cleanup case differs from its journal owner')
        cleanup(lab, lab.cleanup_business, relay_pid=args.relay_pid)
        print('LONGHORN_V1_STORAGE_'+args.case.upper()+'_SCOPED_CLEAN_PASS', flush=True)


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try: main()
    except Exception:
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v1-storage-fault-', suffix='.txt', dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            print('LONGHORN_V1_STORAGE_FAULT_FAIL private_diagnostic='+stream.name, file=sys.stderr)
        sys.exit(1)
